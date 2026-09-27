"""Run one generation's fixed assertions with separate, non-transferable budgets."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

FIRST_SECONDS = 180
EXISTING_SECONDS = 600
TERMINATE_SECONDS = 2
KILL_WAIT_SECONDS = 2


def generation_commands(generation: str, base_url: str, token_file: Path) -> tuple:
    """Keep the first pair ahead of probes and retain the original assertion order."""
    api = ["--base-url", base_url, "--token-file", str(token_file)]

    def command(module: str, args: list[str]) -> list[str]:
        return [sys.executable, "-m", f"qualification.application.{module}", *args]

    nodes = [
        "--address",
        "ray://ray-head:10001",
        "--source-archive",
        "/runtime/project.zip",
        "--recovery-archive",
        "/runtime/recovery.zip",
        "--remote-source",
        "/app/src/django_ray/runtime/remote.py",
        "--receipt",
        f"/receipts/{generation}-nodes.json",
    ]
    if generation == "after":
        nodes += ["--previous-receipt", "/receipts/before-nodes.json"]
    return (
        (
            "first",
            command(
                "run_first_workflows", [*api, "--receipt", f"/receipts/{generation}-first.json"]
            ),
        ),
        ("nodes", command("generic_nodes", nodes)),
        ("core", command("run_core", [*api, "--receipt", f"/receipts/{generation}-core.json"])),
        (
            "workflows",
            command("run_workflows", [*api, "--receipt", f"/receipts/{generation}-workflows.json"]),
        ),
    )


def _stop_owned_process(process: subprocess.Popen) -> None:
    """Escalate the owned group even if its leader exits before descendants."""
    # A reaped leader no longer reserves its PID/PGID. Container teardown owns
    # residual descendants after a normal exit; never signal a recycled group.
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Do not reap the leader before escalation: its PID reserves the group
        # identity while a descendant may still be handling or ignoring TERM.
        time.sleep(TERMINATE_SECONDS)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # Execution is Linux-only; this keeps resource-free host tests portable.
        process.kill()
    process.wait(timeout=KILL_WAIT_SECONDS)


def run_command(argv: list[str], timeout: float) -> int:
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(argv, start_new_session=os.name == "posix")
    try:
        return process.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        try:
            _stop_owned_process(process)
        except (OSError, subprocess.SubprocessError):
            _marker("cleanup", "failed", reason="process_group_stop_incomplete")
        raise


def _marker(phase: str, status: str, **fields: object) -> None:
    # No recognized receipt layer, arguments, tokens or exception text.
    print(json.dumps({"generation_phase": phase, "status": status, **fields}), flush=True)


def run_phases(commands: tuple) -> int:
    for phase, selected, seconds in (
        ("first", commands[:1], FIRST_SECONDS),
        ("existing", commands[1:], EXISTING_SECONDS),
    ):
        started = time.monotonic()
        deadline = started + seconds
        _marker(phase, "started", budget_seconds=seconds)
        for name, argv in selected:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, seconds)
                code = run_command(argv, remaining)
            except subprocess.TimeoutExpired:
                _marker(phase, "failed", command=name, reason="timeout")
                return 124
            except (OSError, subprocess.SubprocessError):
                _marker(phase, "failed", command=name, reason="process_error")
                return 1
            if code:
                _marker(phase, "failed", command=name, reason="nonzero_exit", returncode=code)
                if 0 < code <= 255:
                    return code
                return min(255, 128 - code) if code < 0 else 1
        _marker(phase, "passed", elapsed_seconds=round(time.monotonic() - started, 3))
    return 0


def _terminated(signum, _frame):
    _marker("generation", "failed", reason="terminated")
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True, choices=("before", "after"))
    parser.add_argument("--base-url", default="http://django-web:8000")
    parser.add_argument("--token-file", required=True, type=Path)
    args = parser.parse_args(argv)
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        return run_phases(generation_commands(args.generation, args.base_url, args.token_file))
    except KeyboardInterrupt:
        _marker("generation", "failed", reason="interrupted")
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
