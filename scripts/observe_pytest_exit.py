"""Observe pytest return and interpreter exit without suppressing either failure.

Markers contain only fixed stage names and numeric metadata. A parent-owned
1 KiB journal retains the last pytest phase without per-test console output.
The atexit marker is a checkpoint, not proof that finalization completed.
Opt-in libc exit tracing observes callers before their frames disappear;
direct syscalls can bypass it, and it remains diagnostic-only.
The workflow's existing job timeout remains the execution deadline.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

PHASE_SLOT_BYTES = 512
# Tests can replace json.dumps process-wide until fixture teardown completes.
# Use an observer-owned encoder so phase hooks do not enter the tested logger.
_encode_record = json.JSONEncoder(sort_keys=True).encode
PHASES = frozenset(
    {
        "child_started",
        "pytest_returned",
        "atexit_checkpoint",
        "pytest_raised",
        "session_start",
        "session_started",
        "collection_start",
        "collection_finished",
        "runtest_loop_start",
        "runtest_loop_finished",
        "test_protocol_start",
        "test_protocol_finished",
        "test_setup",
        "test_call",
        "test_teardown",
        "session_finish_start",
        "session_finish_finished",
        "terminal_summary_start",
        "terminal_summary_finished",
        "unconfigure_start",
        "unconfigure_finished",
    }
)


def emit(stage: str, **values: Any) -> None:
    """Write a small, immediately visible diagnostic record."""
    print(_encode_record({"pytest_exit_observer": stage, **values}), flush=True)


class PhaseJournal:
    """Overwrite two bounded slots, retaining a valid predecessor on torn writes."""

    def __init__(self, path: Path) -> None:
        self.fd = os.open(path, os.O_WRONLY)
        os.set_inheritable(self.fd, False)
        self.sequence = 0
        self.ordinal = 0

    def record(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError("Unknown observer phase")
        if self.fd < 0:
            return
        self.sequence += 1
        try:
            payload = _encode_record(
                {"sequence": self.sequence, "phase": phase, "ordinal": self.ordinal}
            ).encode("ascii")
            record = f"{zlib.crc32(payload):08x} ".encode("ascii") + payload
            if len(record) > PHASE_SLOT_BYTES:
                raise ValueError("Observer phase exceeded its fixed slot")
            os.lseek(self.fd, (self.sequence % 2) * PHASE_SLOT_BYTES, os.SEEK_SET)
            if os.write(self.fd, record.ljust(PHASE_SLOT_BYTES, b"\0")) != PHASE_SLOT_BYTES:
                self.close()
        except Exception:
            # Diagnostics must not replace the test's original outcome.
            self.close()

    def close(self) -> None:
        if self.fd >= 0:
            descriptor = self.fd
            self.fd = -1
            try:
                os.close(descriptor)
            except Exception:
                pass


def read_phase(path: Path) -> dict[str, Any] | None:
    """Read only the two fixed slots and reject partial or untrusted records."""
    records = []
    try:
        with path.open("rb") as stream:
            content = stream.read(PHASE_SLOT_BYTES * 2)
    except OSError:
        return None
    for offset in (0, PHASE_SLOT_BYTES):
        slot = content[offset : offset + PHASE_SLOT_BYTES].rstrip(b"\0")
        checksum, _, payload = slot.partition(b" ")
        try:
            if checksum != f"{zlib.crc32(payload):08x}".encode("ascii"):
                continue
            record = json.loads(payload)
            if (
                isinstance(record, dict)
                and set(record) == {"sequence", "phase", "ordinal"}
                and isinstance(record["phase"], str)
                and record["phase"] in PHASES
                and type(record["sequence"]) is int
                and record["sequence"] > 0
                and type(record["ordinal"]) is int
                and record["ordinal"] >= 0
            ):
                records.append(record)
        except (ValueError, UnicodeError):
            continue
    return max(records, key=lambda record: record["sequence"]) if records else None


def observe(command: list[str]) -> int:
    """Wait for the child, retaining failure and distinguishing POSIX signals."""
    result = subprocess.run(command, check=False)
    code = result.returncode
    # Pytest progress can end without a newline when the process exits abruptly.
    print(flush=True)
    emit("process_exit", returncode=code, signal=-code if code < 0 else 0)
    return 128 - code if code < 0 else code


def observe_native(command: list[str], *, trace_exits: bool = False) -> int:
    """Use an owned Linux debugger to retain native shutdown failure evidence."""
    debugger = shutil.which("gdb")
    if sys.platform != "linux" or debugger is None:
        emit("native_debugger_unavailable")
        return 2
    if trace_exits and (platform.machine() != "x86_64" or struct.calcsize("P") != 8):
        emit("native_exit_trace_unsupported_abi")
        return 2

    # The debugger and inferior inherit this limit; never leave a process image.
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    helper = Path(__file__).with_name("pytest_exit_gdb.py").resolve()
    result = subprocess.run(
        [
            debugger,
            "--batch",
            "--quiet",
            "--nx",
            "--nh",
            "-eiex",
            "set python ignore-environment on",
            "-iex",
            "set auto-load off",
            "-iex",
            "set debuginfod enabled off",
            *(["-iex", "set $django_ray_trace_exits = 1"] if trace_exits else []),
            "-x",
            str(helper),
            "--args",
            *command,
        ],
        check=False,
    )
    code = result.returncode
    # This is the debugger's exit. The helper separately records the inferior.
    print(flush=True)
    emit("debugger_exit", returncode=code, signal=-code if code < 0 else 0)
    return 128 - code if code < 0 else code


def run_pytest(arguments: list[str], phase_path: Path | None = None) -> int:
    """Observe pytest and late Python exit without altering test selection."""
    # sys.__stdout__ still refers to fd 1, which pytest's FD capture replaces.
    # A separate non-inheritable descriptor keeps fixed lifecycle records visible.
    diagnostic_fd = os.dup(1)
    os.set_inheritable(diagnostic_fd, False)
    journal = PhaseJournal(phase_path) if phase_path is not None else None
    owner_pid = os.getpid()

    def close_fork_descriptors() -> None:
        nonlocal diagnostic_fd
        if journal is not None:
            journal.close()
        if diagnostic_fd >= 0:
            os.close(diagnostic_fd)
            diagnostic_fd = -1

    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=close_fork_descriptors)

    def checkpoint(stage: str, **values: Any) -> None:
        if os.getpid() != owner_pid:
            return
        if journal is not None:
            journal.record(stage)
        try:
            payload = _encode_record({"pytest_exit_observer": stage, **values})
            os.write(diagnostic_fd, ("\n" + payload + "\n").encode("ascii"))
        except Exception:
            pass

    def final_checkpoint() -> None:
        if os.getpid() != owner_pid:
            return
        checkpoint("atexit_checkpoint")
        if journal is not None:
            journal.close()
        os.close(diagnostic_fd)

    checkpoint("child_started")
    atexit.register(final_checkpoint)
    import pytest

    class PhaseObserver:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_sessionstart(self):
            checkpoint("session_start")
            try:
                return (yield)
            finally:
                checkpoint("session_started")

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_collection(self):
            checkpoint("collection_start")
            try:
                return (yield)
            finally:
                checkpoint("collection_finished")

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtestloop(self):
            checkpoint("runtest_loop_start")
            try:
                return (yield)
            finally:
                checkpoint("runtest_loop_finished")

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self):
            if journal is not None:
                journal.ordinal += 1
                journal.record("test_protocol_start")
            try:
                return (yield)
            finally:
                if journal is not None:
                    journal.record("test_protocol_finished")

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(self):
            if journal is not None:
                journal.record("test_setup")

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_call(self):
            if journal is not None:
                journal.record("test_call")

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_teardown(self):
            if journal is not None:
                journal.record("test_teardown")

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_sessionfinish(self):
            checkpoint("session_finish_start")
            try:
                return (yield)
            finally:
                checkpoint("session_finish_finished")

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_terminal_summary(self):
            checkpoint("terminal_summary_start")
            try:
                return (yield)
            finally:
                checkpoint("terminal_summary_finished")

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_unconfigure(self):
            checkpoint("unconfigure_start")
            try:
                return (yield)
            finally:
                checkpoint("unconfigure_finished")

    try:
        code = int(pytest.main(arguments, plugins=[PhaseObserver()]))
    except BaseException as error:
        # Never format exception messages, which can include user data.
        checkpoint("pytest_raised", exception=type(error).__name__[:80])
        raise
    checkpoint("pytest_returned", returncode=code)
    return code


def main(arguments: list[str]) -> int:
    """Supervise the same interpreter used to launch this script."""
    if arguments[:1] == ["--child"]:
        arguments = arguments[1:]
        phase_path = None
        if arguments[:1] == ["--phase-file"]:
            phase_path = Path(arguments[1])
            arguments = arguments[2:]
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return run_pytest(arguments, phase_path)
    native = False
    trace_exits = False
    retained_phase_path = None
    while arguments and arguments[0] in {
        "--native-debug",
        "--trace-native-exit",
        "--phase-journal",
    }:
        if arguments[0] == "--phase-journal":
            retained_phase_path = Path(arguments[1])
            arguments = arguments[2:]
        else:
            native = True
            trace_exits |= arguments[0] == "--trace-native-exit"
            arguments = arguments[1:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    with tempfile.TemporaryDirectory(prefix="pytest-exit-observer-") as directory:
        phase_path = retained_phase_path or Path(directory) / "phase.bin"
        # An optional supervisor-owned path survives a timeout of this wrapper.
        # Never overwrite an existing file supplied by the caller.
        with phase_path.open("xb") as stream:
            stream.write(b"\0" * (PHASE_SLOT_BYTES * 2))
        command = [
            sys.executable,
            "-u",
            str(Path(__file__).resolve()),
            "--child",
            "--phase-file",
            str(phase_path),
            "--",
            *arguments,
        ]
        try:
            return observe_native(command, trace_exits=trace_exits) if native else observe(command)
        finally:
            record = read_phase(phase_path)
            emit("last_pytest_phase", **record) if record else emit("pytest_phase_unavailable")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
