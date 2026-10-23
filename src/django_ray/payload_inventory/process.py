"""Inventory-owned Linux processes using the doctor's unchanged cleanup primitives."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

from django_ray.doctor.contracts import (
    CLEANUP_SECONDS,
    database_alias_valid,
    encode,
    settings_module_valid,
)
from django_ray.doctor.runner import _caller_sigchld_default, _stop_supervisor
from django_ray.doctor.supervisor import _enable_subreaper, cleanup_probe, peek_status, read_frames
from django_ray.payload_inventory.contracts import CODES, Frames, Limits, report


def supervise(command: list[str], *, deadline: float, limits: Limits, emit: Callable) -> int:
    if not sys.platform.startswith("linux") or not _enable_subreaper():
        emit({"kind": "done", "code": "supervision_unavailable", "cleanup_confirmed": True})
        return 2
    interrupted = False

    def interrupt(_number, _frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    frames = Frames(limits)

    def receive(raw):
        emit(frames.receive(raw))

    process = None
    cleanup = True
    code = "probe_failed"
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        emit({"kind": "started", "pid": process.pid})
        code = read_frames(process, deadline - CLEANUP_SECONDS, receive, lambda: interrupted)
        if code == "observed" and frames.summary is None:
            code = "probe_protocol_invalid"
    except (OSError, ValueError):
        code = "probe_failed"
    finally:
        if process is not None:
            try:
                cleanup = cleanup_probe(process, min(deadline, time.monotonic() + CLEANUP_SECONDS))
            except OSError:
                cleanup = False
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    cleanup = False
    if not cleanup:
        code = "cleanup_unconfirmed"
    emit({"kind": "done", "code": code, "cleanup_confirmed": cleanup})
    return 0 if code == "observed" else 2


def _run(
    *,
    settings_module: str,
    using: str,
    deadline: float,
    limits: Limits,
    frames: Frames,
    interrupted: Callable,
) -> tuple[str, bool]:
    started = False
    done = None

    def receive(raw):
        nonlocal started, done
        value = json.loads(raw)
        if type(value) is not dict or done is not None:
            raise ValueError("Invalid inventory supervisor frame")
        if value.get("kind") == "started":
            if (
                set(value) != {"kind", "pid"}
                or started
                or type(value["pid"]) is not int
                or not 1 < value["pid"] <= (1 << 31) - 1
            ):
                raise ValueError("Invalid inventory child identity")
            started = True
        elif value.get("kind") == "done":
            if (
                set(value) != {"kind", "code", "cleanup_confirmed"}
                or value["code"] not in CODES
                or type(value["cleanup_confirmed"]) is not bool
            ):
                raise ValueError("Invalid inventory cleanup result")
            done = value
        elif started:
            frames.receive(raw)
        else:
            raise ValueError("Inventory child was not observed")

    process = None
    code = "probe_failed"
    cleanup = True
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "django_ray.payload_inventory.process",
                "--mode=supervisor",
                "--settings=" + settings_module,
                "--database=" + using,
                "--deadline=" + str(deadline),
                "--limits=" + json.dumps(asdict(limits)),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        code = read_frames(process, deadline, receive, interrupted)
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
    if done is not None and (
        code == "observed" or (code == "probe_failed" and done["code"] != "observed")
    ):
        code = done["code"]
    return code, cleanup


def inventory(
    *, settings_module: str | None, using: str = "default", limits: Limits | None = None
) -> dict:
    limits = limits or Limits()
    frames = Frames(limits)
    started = time.monotonic()
    if not sys.platform.startswith("linux"):
        return report(frames, code="linux_required", cleanup=True, elapsed=0)
    if not settings_module_valid(settings_module) or not database_alias_valid(using):
        return report(frames, code="settings_not_reproducible", cleanup=True, elapsed=0)
    if threading.current_thread() is not threading.main_thread() or not _caller_sigchld_default():
        return report(frames, code="supervision_unavailable", cleanup=True, elapsed=0)
    interrupted = False
    previous = {}

    def interrupt(_number, _frame):
        nonlocal interrupted
        interrupted = True

    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, interrupt)
        code, cleanup = _run(
            settings_module=settings_module,
            using=using,
            deadline=started + limits.total_seconds - CLEANUP_SECONDS,
            limits=limits,
            frames=frames,
            interrupted=lambda: interrupted,
        )
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return report(frames, code=code, cleanup=cleanup, elapsed=time.monotonic() - started)


def main() -> int:
    if not sys.platform.startswith("linux"):
        return 2
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("supervisor", "probe"), required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--limits", required=True)
    arguments = parser.parse_args()
    try:
        limits = Limits(**json.loads(arguments.limits))
    except (TypeError, ValueError):
        return 2
    if (
        not settings_module_valid(arguments.settings)
        or not database_alias_valid(arguments.database)
        or not 0 < arguments.deadline - time.monotonic() <= 60
    ):
        return 2

    def emit(value):
        sys.stdout.buffer.write(encode(value))
        sys.stdout.buffer.flush()

    if arguments.mode == "probe":
        from django_ray.payload_inventory.probe import run

        return run(
            settings_module=arguments.settings,
            using=arguments.database,
            deadline=arguments.deadline,
            limits=limits,
            emit=emit,
        )
    return supervise(
        [
            sys.executable,
            "-m",
            "django_ray.payload_inventory.process",
            "--mode=probe",
            "--settings=" + arguments.settings,
            "--database=" + arguments.database,
            "--deadline=" + str(arguments.deadline - CLEANUP_SECONDS),
            "--limits=" + arguments.limits,
        ],
        deadline=arguments.deadline,
        limits=limits,
        emit=emit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
