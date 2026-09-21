"""Observe pytest return and interpreter exit without suppressing either failure.

Markers contain only fixed stage names and numeric exit metadata. The atexit
marker is a checkpoint, not proof that interpreter finalization completed.
The workflow's existing job timeout remains the execution deadline.
"""

from __future__ import annotations

import atexit
import json
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
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    return observe([sys.executable, "-u", str(Path(__file__).resolve()), "--child", *arguments])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
