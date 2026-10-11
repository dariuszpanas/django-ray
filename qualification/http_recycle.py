"""Bounded, Linux-only demonstration of a single-worker recycle serving gap.

The invoking test owns the outer process group and twenty-second deadline.
This demonstrates one mechanism; it does not attribute historical failures.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from qualification.application.run_api import ApplicationHttp, ApplicationHttpError

EVENTS = frozenset({"ready", "listener-disabled", "hold-enter", "trigger-enter", "probe-enter"})


def cleanup_group(process) -> None:
    """Retain the leader identity through the last signal; lost ownership fails."""
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        raise RuntimeError("Owned group leader identity is unavailable") from None
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=1)


def record_failure(observation: dict, name: str, error: Exception) -> None:
    observation["failure_phase"] = name
    observation["failure_kind"] = next(
        (
            label
            for kind, label in (
                (TimeoutError, "timeout"),
                (AssertionError, "assertion"),
                (OSError, "os_error"),
                (ValueError, "invalid_value"),
                (subprocess.SubprocessError, "process_error"),
            )
            if isinstance(error, kind)
        ),
        "unexpected",
    )


@contextmanager
def phase(observation: dict, name: str):
    if name not in {
        "startup",
        "hold",
        "trigger",
        "drain",
        "probe",
        "release",
        "replacement",
        "cleanup",
        "finalize",
    }:
        raise ValueError("Invalid experiment phase")
    observation["phase"] = name
    try:
        yield
    except Exception as error:
        record_failure(observation, name, error)
        raise


def read_events(root: Path) -> list[tuple[str, int]]:
    try:
        with (root / "events").open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return []
    if len(raw) > 4096 or (raw and not raw.endswith(b"\n")):
        raise ValueError("Incomplete or oversized owned events")
    rows = []
    for line in raw.decode("ascii").splitlines():
        name, pid = line.split(" ")
        if name not in EVENTS or not pid.isdigit() or not 0 < int(pid) <= 2**31 - 1:
            raise ValueError("Invalid owned event")
        rows.append((name, int(pid)))
    return rows


def wait_for(predicate, deadline: float):
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise TimeoutError("Owned recycle experiment exhausted its deadline")


def run_case(root: Path, *, recycle: bool, deadline: float, observation: dict) -> dict:
    root.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GUNICORN", "DJANGO_RAY_QUALIFICATION_HTTP"))
    }
    environment["DJANGO_RAY_HTTP_RECYCLE_FIXTURE"] = str(root)
    process = None
    held = None
    held_result = []
    facts = {"recycle": recycle, "held_request_served": False, "cleanup": False}
    try:
        with phase(observation, "startup"):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "gunicorn",
                    "qualification.http_recycle_fixture:application",
                    "--worker-class",
                    "qualification.http_recycle_fixture.ObservedThreadWorker",
                    "--bind",
                    f"fd://{listener.fileno()}",
                    "--workers",
                    "1",
                    "--threads",
                    "2",
                    "--max-requests",
                    "2" if recycle else "0",
                    "--max-requests-jitter",
                    "0",
                    "--timeout",
                    "10",
                    "--graceful-timeout",
                    "2",
                    "--keep-alive",
                    "5",
                    "--error-logfile",
                    os.devnull,
                    "--no-control-socket",
                ],
                pass_fds=(listener.fileno(),),
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            listener.close()
            initial = wait_for(
                lambda: [pid for name, pid in read_events(root) if name == "ready"], deadline
            )[0]

        def hold():
            try:
                held_result.append(
                    ApplicationHttp(origin, request_timeout=5)("/hold", method="GET")
                )
            except Exception:
                held_result.append(None)

        with phase(observation, "hold"):
            held = threading.Thread(target=hold)
            held.start()
            wait_for(lambda: ("hold-enter", initial) in read_events(root), deadline)
        with phase(observation, "trigger"):
            assert ApplicationHttp(origin, request_timeout=1)("/trigger", method="GET") == (
                200,
                b"ok",
            )
        if recycle:
            with phase(observation, "drain"):
                wait_for(lambda: ("listener-disabled", initial) in read_events(root), deadline)
        with phase(observation, "probe"):
            client = ApplicationHttp(origin, request_timeout=0.25, retry_read_disconnects=True)
            try:
                observed = client("/probe", method="GET")
            except ApplicationHttpError as error:
                # Only fixed pre-existing failure categories, never dependency text.
                diagnostic = error.diagnostic()
                assert recycle
                assert diagnostic["code"] == "response_headers_failed"
                assert diagnostic["transport_failure"] == "timeout"
                assert diagnostic["status"] is None
                assert ("probe-enter", initial) not in read_events(root)
                facts["fresh_get_timed_out_before_handler"] = True
            else:
                assert not recycle and observed == (200, b"ok")
                assert ("probe-enter", initial) in read_events(root)
                assert ("listener-disabled", initial) not in read_events(root)
                facts["fresh_get_served_while_held"] = True
            assert client.requests == 1
            assert all(value == 0 for value in client.transport_recoveries.values())
            facts["timeout_not_retried"] = True
        with phase(observation, "release"):
            (root / "release").touch()
            held.join(timeout=1)
            assert not held.is_alive() and held_result == [(200, b"ok")]
            facts["held_request_served"] = True
        if recycle:
            with phase(observation, "replacement"):
                wait_for(
                    lambda: any(
                        name == "ready" and pid != initial for name, pid in read_events(root)
                    ),
                    deadline,
                )
                assert ApplicationHttp(origin, request_timeout=1)("/probe", method="GET") == (
                    200,
                    b"ok",
                )
                facts["replacement_served"] = True
    finally:
        with phase(observation, "cleanup"):
            listener.close()
            (root / "release").touch(exist_ok=True)
            if held is not None:
                held.join(timeout=1)
            if process is not None:
                # Do not poll/reap before signaling; this child retains its PID.
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                    raise RuntimeError("Owned server required forced cleanup") from None
            if held is not None and held.is_alive():
                raise RuntimeError("Owned client thread did not stop")
            facts["cleanup"] = True
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("The recycle experiment is Linux-only")
    deadline = time.monotonic() + 14
    receipt = {"status": "failed", "historical_cause_proven": False}
    observation = {}
    receipt["observation"] = observation
    try:
        receipt["stage"] = "recycle"
        recycle = run_case(
            args.root / "recycle", recycle=True, deadline=deadline, observation=observation
        )
        receipt["stage"] = "control"
        observation.clear()
        control = run_case(
            args.root / "control", recycle=False, deadline=deadline, observation=observation
        )
        receipt["cases"] = [recycle, control]
        with phase(observation, "finalize"):
            assert time.monotonic() < deadline
            receipt["gunicorn_version"] = importlib.metadata.version("gunicorn")
        receipt["stage"] = "complete"
        receipt["status"] = "passed"
    except Exception as error:
        if "failure_phase" not in observation:
            record_failure(observation, "startup", error)
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
