"""Linux process-group supervision, isolated from the caller's process state."""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable

from django_ray.doctor.contracts import (
    CLEANUP_SECONDS,
    DATABASE_SECTIONS,
    FRAME_BYTES,
    STDERR_BYTES,
    STDOUT_BYTES,
    encode,
    parse_section,
    section_frame,
)


def peek_status(process: subprocess.Popen) -> int | None:
    """Observe without reaping: the leader pins its numeric PID/group identity.

    Only the owner may call wait() after its final group signal. An externally
    reaped leader raises ChildProcessError; numeric identity is no longer safe.
    """
    if process.returncode is not None:
        raise ChildProcessError("Owned leader identity was already released")
    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if status is None:
        return None
    return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status


def read_frames(
    process: subprocess.Popen,
    deadline: float,
    receive: Callable[[bytes], None],
    interrupted: Callable[[], bool] = lambda: False,
) -> str:
    """Read finite lines and discard stderr while enforcing both byte ceilings."""
    counts = {"stdout": 0, "stderr": 0}
    pending = bytearray()
    with selectors.DefaultSelector() as selector:
        for name in counts:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map() or peek_status(process) is None:
            if interrupted():
                return "interrupted"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "probe_timeout"
            if not selector.get_map():
                time.sleep(min(0.02, remaining))
                continue
            for key, _events in selector.select(min(0.05, remaining)):
                name = key.data
                maximum = STDOUT_BYTES if name == "stdout" else STDERR_BYTES
                chunk = os.read(key.fd, min(4096, maximum - counts[name] + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                counts[name] += len(chunk)
                if counts[name] > maximum:
                    return "output_budget_exhausted"
                if name == "stderr":
                    continue
                # A read is capped at 4 KiB and each pending frame at 2 KiB.
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    if len(line) + 1 > FRAME_BYTES:
                        return "output_budget_exhausted"
                    pending = bytearray(rest)
                    try:
                        receive(bytes(line))
                    except (ValueError, TypeError, KeyError, RecursionError):
                        return "probe_protocol_invalid"
                if len(pending) >= FRAME_BYTES:
                    return "output_budget_exhausted"
    if pending:
        return "probe_protocol_invalid"
    return "observed" if peek_status(process) == 0 else "probe_failed"


def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def signal_group(pgid: int, value: int) -> None:
    try:
        os.killpg(pgid, value)
    except ProcessLookupError:
        pass


def _enable_subreaper() -> bool:
    # This executes only in the dedicated supervisor, never the Django caller.
    import ctypes

    try:
        library = ctypes.CDLL(None, use_errno=True)
        return library.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def cleanup_probe(process: subprocess.Popen, deadline: float) -> bool:
    """Terminate and reap the owned group, including adopted group descendants.

    Descendants that deliberately leave this process group are outside this
    contract. A kernel-uninterruptible process yields unconfirmed cleanup.
    """
    peek_status(process)  # Refuse any signal if another owner already reaped it.
    if group_exists(process.pid):
        signal_group(process.pid, signal.SIGTERM)
        grace = min(deadline, time.monotonic() + 0.5)
        while group_exists(process.pid) and time.monotonic() < grace:
            peek_status(process)
            time.sleep(min(0.01, max(0, grace - time.monotonic())))
        signal_group(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    while time.monotonic() < deadline:
        try:
            pid, _status = os.waitpid(-process.pid, os.WNOHANG)
        except ChildProcessError:
            # Escaped adopted descendants are outside the group-signal scope.
            # Their presence still prevents a claim of confirmed cleanup.
            try:
                os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return not group_exists(process.pid)
            return False
        if pid == 0:
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return False


def supervise(
    command: list[str], *, probe: str, deadline: float, emit: Callable[[dict], None]
) -> int:
    """Run a package-owned probe; retain completed sections on partial failure."""
    if not sys.platform.startswith("linux") or not _enable_subreaper():
        emit({"kind": "done", "code": "supervision_unavailable", "cleanup_confirmed": True})
        return 2
    interrupted = [False]
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    def interrupt(_signum, _frame):
        interrupted[0] = True

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    allowed = {"configuration"} if probe == "configuration" else set(DATABASE_SECTIONS)
    seen: set[str] = set()

    def receive(raw: bytes) -> None:
        section, value = parse_section(raw)
        if section not in allowed or section in seen:
            raise ValueError("Invalid probe section")
        seen.add(section)
        emit(section_frame(section, value))

    process = None
    code = "probe_failed"
    cleanup = True
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        emit({"kind": "started", "pid": process.pid})
        code = read_frames(process, deadline - CLEANUP_SECONDS, receive, lambda: interrupted[0])
        if code == "observed" and seen != allowed:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", choices=("configuration", "database"), required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--deadline", type=float, required=True)
    arguments = parser.parse_args()
    from django_ray.doctor.contracts import database_alias_valid, settings_module_valid

    remaining = arguments.deadline - time.monotonic()
    if not (
        settings_module_valid(arguments.settings)
        and database_alias_valid(arguments.database)
        and CLEANUP_SECONDS < remaining <= 20
    ):
        return 2
    command = [
        sys.executable,
        "-m",
        "django_ray.doctor.probe",
        "--probe",
        arguments.probe,
        "--settings",
        arguments.settings,
        "--database=" + arguments.database,
        "--deadline",
        str(arguments.deadline - CLEANUP_SECONDS),
    ]

    def emit(value: dict) -> None:
        sys.stdout.buffer.write(encode(value))
        sys.stdout.buffer.flush()

    return supervise(command, probe=arguments.probe, deadline=arguments.deadline, emit=emit)


if __name__ == "__main__":
    raise SystemExit(main())
