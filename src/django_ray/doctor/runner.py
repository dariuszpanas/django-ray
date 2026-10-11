"""Bounded parent interface; application initialization belongs to owned probes."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from django_ray.doctor.contracts import (
    CLEANUP_SECONDS,
    CODES,
    DATABASE_SECTIONS,
    Limits,
    database_alias_valid,
    initial_sections,
    observation,
    parse_section,
    result,
    settings_module_valid,
)
from django_ray.doctor.supervisor import peek_status, read_frames, signal_group


class DoctorInterrupted(BaseException):
    """Private interruption signal with no caller or application text."""

    def __init__(self, cleanup: bool = True):
        self.cleanup = cleanup


def _caller_sigchld_default() -> bool:
    return hasattr(signal, "SIGCHLD") and signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL


def _stop_supervisor(
    process: subprocess.Popen, deadline: float, receive: Callable[[bytes], None]
) -> bool:
    """Stop only the still-pinned supervisor; its final frame owns inner proof."""
    peek_status(process)
    signal_group(process.pid, signal.SIGTERM)
    grace = min(deadline, time.monotonic() + 1.5)
    read_frames(process, grace, receive)
    stopped = peek_status(process) is not None
    if not stopped:
        # The probe is a separate group whose leader the supervisor may already
        # have reaped. A cached numeric probe PGID is never safe to signal here.
        signal_group(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return stopped


def _run_probe(
    probe: str,
    settings_module: str,
    using: str,
    deadline: float,
    sections: dict,
    interrupted: Callable[[], bool] = lambda: False,
) -> bool:
    expected = {"configuration"} if probe == "configuration" else set(DATABASE_SECTIONS)
    worker_pid = None
    done = None
    received: set[str] = set()

    def receive(raw: bytes) -> None:
        nonlocal worker_pid, done
        value = json.loads(raw)
        if type(value) is not dict:
            raise ValueError("Invalid supervisor frame")
        if value.get("kind") == "started":
            if (
                set(value) != {"kind", "pid"}
                or worker_pid is not None
                or done is not None
                or type(value["pid"]) is not int
                or not 1 < value["pid"] <= (1 << 31) - 1
            ):
                raise ValueError("Invalid owned process identity")
            worker_pid = value["pid"]
        elif value.get("kind") == "done":
            if (
                set(value) != {"kind", "code", "cleanup_confirmed"}
                or done is not None
                or value["code"] not in CODES
                or type(value["cleanup_confirmed"]) is not bool
            ):
                raise ValueError("Invalid cleanup result")
            done = value
        else:
            name, item = parse_section(raw)
            if name not in expected or name in received or done is not None or worker_pid is None:
                raise ValueError("Invalid probe observation")
            received.add(name)
            sections[name] = item

    process = None
    code = "probe_failed"
    cleanup = True
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "django_ray.doctor.supervisor",
                "--probe",
                probe,
                "--settings",
                settings_module,
                "--database=" + using,
                "--deadline",
                str(deadline),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        code = read_frames(process, deadline, receive, interrupted)
    except DoctorInterrupted:
        code = "interrupted"
    except (OSError, ValueError):
        code = "probe_failed"
    finally:
        if process is not None:
            try:
                if done is None or peek_status(process) is None:
                    stopped = _stop_supervisor(process, deadline + CLEANUP_SECONDS, receive)
                    cleanup = stopped and done is not None and done["cleanup_confirmed"]
                else:
                    process.wait(timeout=max(0, deadline + CLEANUP_SECONDS - time.monotonic()))
                    cleanup = done["cleanup_confirmed"]
            except (OSError, subprocess.TimeoutExpired):
                cleanup = False
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    cleanup = False
    if done is not None and code in {"observed", "probe_failed"}:
        code = done["code"]
        cleanup = cleanup and done["cleanup_confirmed"]
    if code == "observed" and (received != expected or done is None):
        code = "probe_protocol_invalid"
    if code != "observed":
        outcome = (
            "budget_exhausted"
            if code in {"probe_timeout", "output_budget_exhausted", "total_budget_exhausted"}
            else "unavailable"
        )
        for name in expected - received:
            sections[name] = observation(outcome, code)
        # A late failure (including connection cleanup) must not disappear merely
        # because every section was already emitted.
        if received == expected and cleanup:
            sections[probe] = observation(outcome, code)
    if not cleanup:
        # Completed dated observations remain evidence even when owned cleanup
        # cannot be confirmed. The top-level cleanup flag forces exit 2.
        for name in expected - received:
            sections[name] = observation("unavailable", "cleanup_unconfirmed")
    if code == "interrupted":
        raise DoctorInterrupted(cleanup)
    return cleanup


def diagnose(
    *, settings_module: str | None, using: str = "default", limits: Limits | None = None
) -> dict:
    limits = limits or Limits()
    started = time.monotonic()
    sections = initial_sections()
    if not sys.platform.startswith("linux"):
        return result(initial_sections("linux_required", "unsupported"), limits, 0, True)
    if not settings_module_valid(settings_module) or not database_alias_valid(using):
        return result(initial_sections("settings_not_reproducible", "unsupported"), limits, 0, True)
    if threading.current_thread() is not threading.main_thread() or not _caller_sigchld_default():
        return result(initial_sections("supervision_unavailable", "unsupported"), limits, 0, True)
    deadline = started + limits.total_seconds
    cleanup = True
    previous = {}
    interrupted = False

    def interrupt(_signum, _frame):
        nonlocal interrupted
        # Never raise through Popen: interruption between process creation and
        # obtaining its handle would lose the only reliable ownership identity.
        interrupted = True

    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, interrupt)
        for probe in ("configuration", "database"):
            if interrupted:
                raise DoctorInterrupted(cleanup)
            available = deadline - time.monotonic() - CLEANUP_SECONDS
            if available <= CLEANUP_SECONDS:
                for name, value in sections.items():
                    if value["code"] == "not_observed":
                        sections[name] = observation("budget_exhausted", "total_budget_exhausted")
                break
            probe_deadline = time.monotonic() + min(limits.probe_seconds, available)
            cleanup = (
                _run_probe(
                    probe, settings_module, using, probe_deadline, sections, lambda: interrupted
                )
                and cleanup
            )
            if not cleanup:
                break
    except DoctorInterrupted as error:
        cleanup = cleanup and error.cleanup
        for name, value in sections.items():
            if value["code"] == "not_observed":
                sections[name] = observation("unavailable", "interrupted")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return result(sections, limits, time.monotonic() - started, cleanup)
