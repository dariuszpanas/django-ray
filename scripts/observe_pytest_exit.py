"""Observe pytest return and interpreter exit without suppressing either failure.

Markers contain only fixed stage names and numeric exit metadata. The atexit
marker is a checkpoint, not proof that interpreter finalization completed.
The workflow's existing job timeout remains the execution deadline.
"""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import sys
from pathlib import Path


def emit(stage: str, **values: int) -> None:
    """Write a small, immediately visible diagnostic record."""
    print(json.dumps({"pytest_exit_observer": stage, **values}, sort_keys=True), flush=True)


def observe(command: list[str]) -> int:
    """Wait for the child, retaining failure and distinguishing POSIX signals."""
    result = subprocess.run(command, check=False)
    code = result.returncode
    emit("process_exit", returncode=code, signal=-code if code < 0 else 0)
    return 128 - code if code < 0 else code


def observe_native(command: list[str]) -> int:
    """Use an owned Linux debugger to retain native shutdown failure evidence."""
    debugger = shutil.which("gdb")
    if sys.platform != "linux" or debugger is None:
        emit("native_debugger_unavailable")
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
            "-x",
            str(helper),
            "--args",
            *command,
        ],
        check=False,
    )
    code = result.returncode
    # This is the debugger's exit. The helper separately records the inferior.
    emit("debugger_exit", returncode=code, signal=-code if code < 0 else 0)
    return 128 - code if code < 0 else code


def run_pytest(arguments: list[str]) -> int:
    """Observe pytest and late Python exit without altering test selection."""
    emit("child_started")
    atexit.register(emit, "atexit_checkpoint")
    import pytest

    code = int(pytest.main(arguments))
    emit("pytest_returned", returncode=code)
    return code


def main(arguments: list[str]) -> int:
    """Supervise the same interpreter used to launch this script."""
    if arguments[:1] == ["--child"]:
        return run_pytest(arguments[1:])
    native = arguments[:1] == ["--native-debug"]
    if native:
        arguments = arguments[1:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "--child", *arguments]
    return observe_native(command) if native else observe(command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
