"""One bounded, private follow-up to a failed manager readiness inventory."""

from __future__ import annotations

import io
import json
import os
import selectors
import signal
import subprocess
import sys
import time

MAX_BYTES = 512
SECONDS = 10
REASONS = {
    "ready": frozenset({"live_lease"}),
    "not_ready": frozenset({"no_live_lease", "ambiguous_lease"}),
    "error": frozenset(
        {
            "invalid_coordinates",
            "invalid_database",
            "invalid_settings",
            "transaction_active",
            "database_unavailable",
            "lease_policy_unavailable",
            "lease_policy_invalid",
            "lease_policy_mismatch",
            "lease_policy_changed",
        }
    ),
}
EXITS = {"ready": 0, "not_ready": 1, "error": 2}
CHECKPOINTS = ("helper_entry", "django_setup", "worker_ready")
PHASES = frozenset({"unknown", "complete", *CHECKPOINTS})
MILESTONES = tuple(
    json.dumps({"v": 1, "phase": phase}, separators=(",", ":")).encode() + b"\n"
    for phase in CHECKPOINTS
)


def _report_frame(status: str, reason: str) -> bytes:
    return (
        json.dumps(
            {"schema_version": 1, "scope": "worker_lease", "status": status, "reason": reason},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


REPORTS = {
    _report_frame(status, reason): {"status": status, "reason": reason}
    for status, reasons in REASONS.items()
    for reason in reasons
}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate readiness field")
        value[key] = item
    return value


def parse_report(raw: bytes) -> dict | None:
    """Validate only the closed worker report; no process exit is implied."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if (
            type(value) is not dict
            or set(value) != {"schema_version", "scope", "status", "reason"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["scope"] != "worker_lease"
            or type(value["status"]) is not str
            or value["status"] not in REASONS
            or type(value["reason"]) is not str
            or value["reason"] not in REASONS[value["status"]]
        ):
            return None
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    return {"status": value["status"], "reason": value["reason"]}


def parse(raw: bytes, exit_code: int) -> dict | None:
    """Accept exactly the existing worker-ready JSON and matching command exit."""
    report = parse_report(raw)
    if report is None or type(exit_code) is not int or exit_code != EXITS[report["status"]]:
        return None
    return report


def _progress(raw: bytes, *, interrupted: bool) -> tuple[str, dict | None]:
    """Accept only ordered fixed frames or an interrupted prefix of the next."""
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ValueError("Invalid readiness progress")
    phase = "unknown"
    for name, frame in zip(CHECKPOINTS, MILESTONES, strict=True):
        if not raw:
            return phase, None
        if not raw.startswith(frame):
            if interrupted and frame.startswith(raw):
                return phase, None
            raise ValueError("Invalid readiness progress")
        phase = name
        raw = raw[len(frame) :]
    if not raw:
        return phase, None
    if raw in REPORTS:
        return phase, REPORTS[raw].copy()
    if interrupted and any(frame.startswith(raw) for frame in REPORTS):
        return phase, None
    raise ValueError("Invalid readiness progress")


class _CaptureDeadlineError(TimeoutError):
    """Private bounded bytes survive only successful owned-process cleanup."""

    def __init__(self, raw: bytes):
        super().__init__("Readiness command timed out")
        self.raw = raw


def empty(outcome: str = "incomplete") -> dict:
    return {
        "outcome": outcome,
        "phase": "unknown",
        "status": "unknown",
        "reason": "unknown",
        "exit_code": None,
        "elapsed_ms": 0,
    }


def valid(value: object) -> bool:
    if (
        type(value) is not dict
        or set(value) != set(empty())
        or type(value["outcome"]) is not str
        or value["outcome"] not in {"observed", "incomplete", "error", "timeout"}
        or type(value["phase"]) is not str
        or value["phase"] not in PHASES
        or type(value["elapsed_ms"]) is not int
        or not 0 <= value["elapsed_ms"] <= SECONDS * 1000
        or (value["exit_code"] is not None and type(value["exit_code"]) is not int)
    ):
        return False
    if value["outcome"] == "observed":
        return (
            value["phase"] == "complete"
            and type(value["status"]) is str
            and value["status"] in REASONS
            and type(value["reason"]) is str
            and value["reason"] in REASONS[value["status"]]
            and value["exit_code"] == EXITS[value["status"]]
        )
    return (
        value["phase"] != "complete"
        and (value["outcome"] != "error" or value["phase"] == "unknown")
        and value["status"] == "unknown"
        and value["reason"] == "unknown"
        and value["exit_code"] in {None, 0, 1, 2, 3, 124, 137}
    )


def _peek(process):
    # Keep the group leader unreaped until the last signal, preventing PID reuse.
    if process.returncode is not None:
        raise ChildProcessError("Readiness command ownership was lost")
    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if status is None:
        return None
    return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status


def _capture(argv, *, environment, deadline):
    """Retain at most 513 bytes; reserve one second to stop and reap kubectl."""
    process = subprocess.Popen(
        argv,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        bufsize=0,
    )
    try:
        if process.stdout is None:
            raise OSError("Readiness output unavailable")
        raw = bytearray()
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or _peek(process) is None:
                remaining = deadline - 1 - time.monotonic()
                if remaining <= 0:
                    raise _CaptureDeadlineError(bytes(raw))
                if not selector.get_map():
                    time.sleep(min(0.02, remaining))
                    continue
                for key, _ in selector.select(min(0.05, remaining)):
                    chunk = os.read(key.fd, MAX_BYTES + 1 - len(raw))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Readiness output exceeded its bound")
        return bytes(raw), _peek(process)
    finally:
        try:
            _peek(process)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=max(0, min(1, deadline - time.monotonic())))
        finally:
            if process.stdout is not None:
                process.stdout.close()


def observe(*, prefix: list[str], pod: str, environment: dict, deadline: float) -> dict:
    """Never turn this follow-up into acceptance or extend the outer deadline."""
    value = empty()
    started = time.monotonic()
    allowance = min(SECONDS, deadline - started - 1)
    if sys.platform != "linux" or allowance < 4:
        return value
    try:
        raw, exit_code = _capture(
            [
                *prefix,
                "exec",
                pod,
                "-c",
                "manager",
                "--",
                # This separate owner bounds Python startup as well as Django
                # setup, and keeps running if the host exec transport is lost.
                "timeout",
                "--signal=KILL",
                f"{int(allowance - 2)}s",
                "python",
                "-m",
                "qualification.application.worker_readiness_diagnostics",
            ],
            environment=environment,
            deadline=started + allowance,
        )
        if type(exit_code) is not int:
            raise ValueError("Invalid readiness command exit")
        value["exit_code"] = exit_code if exit_code in {0, 1, 2, 3, 124, 137} else None
        phase, report = _progress(raw, interrupted=exit_code in {124, 137})
        value["phase"] = phase
        if report is not None and exit_code == EXITS[report["status"]]:
            value.update(outcome="observed", phase="complete", **report)
        else:
            value["outcome"] = "timeout" if exit_code in {124, 137} else "incomplete"
    except _CaptureDeadlineError as error:
        value = empty("timeout")
        try:
            value["phase"], _ = _progress(error.raw, interrupted=True)
        except (ValueError, TypeError):
            value = empty("error")
    except (TimeoutError, subprocess.TimeoutExpired):
        value = empty("timeout")
    except (OSError, ValueError, TypeError):
        value = empty("error")
    value["elapsed_ms"] = min(SECONDS * 1000, max(0, int((time.monotonic() - started) * 1000)))
    return value


class _Output(io.StringIO):
    def write(self, text):
        if type(text) is not str or len(text.encode()) + len(self.getvalue().encode()) > MAX_BYTES:
            raise ValueError("Readiness output exceeded its bound")
        return super().write(text)


def _command(checkpoint):
    import django

    checkpoint("django_setup")
    django.setup()
    from django.core.management import CommandError, call_command

    output = _Output()
    exit_code = 0
    checkpoint("worker_ready")
    try:
        call_command(
            "django_ray_worker_ready",
            queue=os.environ.get("DJANGO_RAY_QUEUES", ""),
            hostname=os.environ.get("DJANGO_RAY_POD_NAME", ""),
            as_json=True,
            stdout=output,
            stderr=output,
        )
    except CommandError as error:
        exit_code = error.returncode
    return output.getvalue().encode(), exit_code


def main() -> int:
    """Private child: suppress every ambient byte before initializing Django."""
    if (
        sys.platform != "linux"
        or os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
    ):
        return 3
    destination = os.dup(1)

    def write(raw):
        if os.write(destination, raw) != len(raw):
            raise OSError("Readiness progress unavailable")

    def checkpoint(name):
        write(MILESTONES[CHECKPOINTS.index(name)])

    try:
        with open(os.devnull, "wb") as hidden:
            os.dup2(hidden.fileno(), 1)
            os.dup2(hidden.fileno(), 2)
            try:
                checkpoint("helper_entry")
                raw, exit_code = _command(checkpoint)
                report = parse(raw, exit_code)
                if report is None:
                    return 3
                write(_report_frame(**report))
                return exit_code
            except Exception:
                return 3
    finally:
        os.close(destination)


if __name__ == "__main__":
    raise SystemExit(main())
