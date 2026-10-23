"""Linux-only capture regressions using short, isolated Python children."""

import json
import os
import select
import signal
import subprocess
import sys
import time

import pytest

from qualification.application import worker_readiness_diagnostics as readiness

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Owned capture requires Linux")


@pytest.fixture
def captured_processes(monkeypatch):
    """Retain real Popen handles and clean up even if the capture assertion fails."""
    processes = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(readiness.subprocess, "Popen", start)
    try:
        yield processes
    finally:
        for process in processes:
            if process.returncode is None:
                try:
                    # Do not signal a numeric group after losing its leader.
                    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    pass
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()


@pytest.mark.parametrize("scenario", ["success", "hung", "overflow"])
def test_actual_capture_bounds_output_and_reaps_owned_leader(
    captured_processes, tmp_path, scenario
):
    value = {
        "schema_version": 1,
        "scope": "worker_lease",
        "status": "ready",
        "reason": "live_lease",
    }
    raw = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    # The independent child alarm bounds test cleanup if capture regresses.
    code = "import os, signal, time; signal.alarm(6); "
    if scenario == "success":
        code += f"os.write(2, b'discarded diagnostic'); os.write(1, {raw!r})"
    elif scenario == "hung":
        code += "open(__import__('sys').argv[1], 'wb').close(); time.sleep(60)"
    else:
        code += f"os.write(1, b'x' * {readiness.MAX_BYTES + 1}); time.sleep(60)"
    started = time.monotonic()
    argv = [sys.executable, "-I", "-S", "-c", code, str(tmp_path / "started")]
    if scenario == "success":
        assert readiness._capture(argv, environment={}, deadline=started + 3) == (raw, 0)
    else:
        error = TimeoutError if scenario == "hung" else ValueError
        match = "timed out" if scenario == "hung" else "exceeded its bound"
        with pytest.raises(error, match=match):
            readiness._capture(argv, environment={}, deadline=started + 3)
    # Allow a scheduling margin without accepting the independent watchdog exit.
    assert time.monotonic() - started < 5
    assert len(captured_processes) == 1
    process = captured_processes[0]
    assert process.returncode == (0 if scenario == "success" else -signal.SIGKILL)
    assert process.stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)
    if scenario == "hung":
        assert (tmp_path / "started").exists()


@pytest.mark.parametrize(
    "scenario",
    [
        "milestone_hang",
        "partial_hang",
        "report_hang",
        "report_success",
        "report_nonzero",
        "descendant_pipe",
        "progress_overflow",
    ],
)
def test_actual_progress_capture_preserves_timeout_and_owned_cleanup(
    monkeypatch, captured_processes, tmp_path, scenario
):
    milestones = b"".join(readiness.MILESTONES)
    report = readiness._report_frame("ready", "live_lease")
    raw = readiness.MILESTONES[0]
    expected_phase = "helper_entry"
    if scenario == "partial_hang":
        raw += readiness.MILESTONES[1][:9]
    elif scenario in {"report_hang", "report_success", "report_nonzero", "descendant_pipe"}:
        raw = milestones + report
        expected_phase = "worker_ready"
        if scenario == "report_success":
            expected_phase = "complete"
    elif scenario == "progress_overflow":
        raw += b"x" * 513
        expected_phase = "unknown"
    code = f"import os, signal, time; signal.alarm(6); os.write(1, {raw!r}); "
    if scenario == "report_success":
        code += "raise SystemExit(0)"
    elif scenario == "report_nonzero":
        code += "raise SystemExit(2)"
    elif scenario == "descendant_pipe":
        code += "child = os.fork(); "
        code += "os._exit(0) if child else None; "
        code += "signal.alarm(6); open(__import__('sys').argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
    else:
        code += "time.sleep(60)"
    started = time.monotonic()
    argv = [sys.executable, "-I", "-S", "-c", code, str(tmp_path / "descendant-started")]
    capture = readiness._capture

    def actual_capture(_argv, *, environment, deadline):
        return capture(argv, environment=environment, deadline=deadline)

    monkeypatch.setattr(readiness, "_capture", actual_capture)
    value = readiness.observe(prefix=[], pod="unused", environment={}, deadline=started + 6)
    assert time.monotonic() - started < 6
    assert readiness.valid(value)
    assert value["phase"] == expected_phase
    if scenario == "report_success":
        assert (value["status"], value["reason"]) == ("ready", "live_lease")
    else:
        assert value["status"] == value["reason"] == "unknown"
    expected_outcome = (
        "observed"
        if scenario == "report_success"
        else "incomplete"
        if scenario == "report_nonzero"
        else "error"
        if scenario == "progress_overflow"
        else "timeout"
    )
    assert value["outcome"] == expected_outcome
    process = captured_processes[0]
    assert process.returncode == (
        2
        if scenario == "report_nonzero"
        else 0
        if scenario in {"descendant_pipe", "report_success"}
        else -signal.SIGKILL
    )
    assert process.stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)
    if scenario == "descendant_pipe":
        child_pid = int((tmp_path / "descendant-started").read_text())
        try:
            descriptor = os.pidfd_open(child_pid)
        except ProcessLookupError:
            pass
        else:
            try:
                poller = select.poll()
                poller.register(descriptor, select.POLLIN)
                assert poller.poll(500), "Owned descendant survived group cleanup"
            finally:
                os.close(descriptor)
