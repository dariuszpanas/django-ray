"""Fixed event validation and an explicitly selected, bounded Linux experiment."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification import http_recycle as probe
from qualification.http_recycle import read_events


def failure_summary(path: Path) -> str:
    """Expose only finite diagnostic labels, including for malformed receipts."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return "unavailable"
        receipt = json.loads(raw)
        stage = receipt["stage"]
        observation = receipt["observation"]
        phase = observation["failure_phase"]
        kind = observation["failure_kind"]
        if (
            stage not in ("recycle", "control", "complete")
            or phase
            not in (
                "startup",
                "hold",
                "trigger",
                "drain",
                "probe",
                "release",
                "replacement",
                "cleanup",
                "finalize",
            )
            or kind
            not in (
                "timeout",
                "assertion",
                "os_error",
                "invalid_value",
                "process_error",
                "unexpected",
            )
        ):
            return "unavailable"
        return f"stage={stage}, phase={phase}, type={kind}"
    except (OSError, ValueError, TypeError, KeyError):
        return "unavailable"


@pytest.mark.parametrize("invalid", [None, "stage", "failure_phase", "failure_kind", "oversized"])
def test_failure_summary_is_bounded_and_whitelisted(tmp_path, invalid):
    receipt = {
        "stage": "recycle",
        "observation": {"failure_phase": "replacement", "failure_kind": "timeout"},
    }
    if invalid == "stage":
        receipt["stage"] = "private data"
    elif invalid in ("failure_phase", "failure_kind"):
        receipt["observation"][invalid] = ["private data"]
    elif invalid == "oversized":
        receipt["private"] = "x" * 4097
    path = tmp_path / "receipt"
    path.write_text(json.dumps(receipt))
    assert failure_summary(path) == (
        "stage=recycle, phase=replacement, type=timeout" if invalid is None else "unavailable"
    )


@pytest.mark.parametrize("raw", [b"private text", b"[]", b"null", b"{}", b"\xff"])
def test_failure_summary_refuses_malformed_receipts(tmp_path, raw):
    path = tmp_path / "receipt"
    assert failure_summary(path) == "unavailable"
    path.write_bytes(raw)
    assert failure_summary(path) == "unavailable"


@pytest.mark.parametrize(
    "content", [b"unknown 10\n", b"ready -1\n", b"ready 0\n", b"ready 10", b"x" * 4097]
)
def test_owned_events_fail_closed(tmp_path, content):
    (tmp_path / "events").write_bytes(content)
    with pytest.raises(ValueError):
        read_events(tmp_path)


def test_owned_events_are_fixed_and_bounded(tmp_path):
    assert read_events(tmp_path) == []
    (tmp_path / "events").write_bytes(b"ready 10\nhold-enter 10\nlistener-disabled 10\n")
    assert read_events(tmp_path) == [("ready", 10), ("hold-enter", 10), ("listener-disabled", 10)]


def test_unexpectedly_reaped_group_leader_is_never_signalled(monkeypatch):
    system = SimpleNamespace(
        P_PID=1,
        WEXITED=2,
        WNOHANG=4,
        WNOWAIT=8,
        waitid=Mock(side_effect=ChildProcessError),
        killpg=Mock(),
    )
    process = Mock(pid=10)
    monkeypatch.setattr(probe, "os", system)
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        probe.cleanup_group(process)
    system.killpg.assert_not_called()
    process.wait.assert_not_called()


@pytest.mark.parametrize(
    "error,kind",
    [(TimeoutError, "timeout"), (ValueError, "invalid_value"), (RuntimeError, "unexpected")],
)
def test_failure_phase_and_type_omit_exception_text(error, kind):
    observation = {}
    with pytest.raises(error):
        with probe.phase(observation, "replacement"):
            raise error("private exception payload")
    # A succeeding finally-cleanup observation preserves the original failure.
    with probe.phase(observation, "cleanup"):
        pass
    assert observation == {"phase": "cleanup", "failure_phase": "replacement", "failure_kind": kind}


def test_setup_failure_receipt_has_fixed_attribution(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--root", str(tmp_path)])
    monkeypatch.setattr(probe, "run_case", Mock(side_effect=OSError("private setup text")))
    assert probe.main() == 1
    output = capsys.readouterr().out
    assert "private" not in output
    assert json.loads(output)["observation"] == {
        "failure_phase": "startup",
        "failure_kind": "os_error",
    }


def test_single_worker_recycle_gap_and_no_recycle_control(tmp_path):
    required = os.environ.get("REQUIRE_HTTP_RECYCLE_PROOF") == "1"
    project = Path(__file__).resolve().parents[2]
    locked = next(
        package["version"]
        for package in tomllib.loads((project / "uv.lock").read_text())["package"]
        if package["name"] == "gunicorn"
    )
    available = (
        sys.platform == "linux"
        and importlib.util.find_spec("gunicorn") is not None
        and importlib.metadata.version("gunicorn") == locked
    )
    if not available:
        if required:
            pytest.fail("Required Linux experiment needs the locked sample Gunicorn")
        pytest.skip("Selected Linux sample lane runs the locked Gunicorn experiment")
    assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
    output = tmp_path / "receipt.json"
    process = None
    started = time.monotonic()
    status = None
    with output.open("wb") as stream:
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "qualification.http_recycle", "--root", str(tmp_path)],
                cwd=project,
                start_new_session=True,
                stdout=stream,
                stderr=subprocess.DEVNULL,
            )
            # WNOWAIT keeps the group leader reserved through the last signal.
            # Never poll(), wait(), or communicate() before clearing its group.
            while time.monotonic() - started < 18:
                status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if status is not None:
                    break
                time.sleep(0.01)
        finally:
            if process is not None:
                probe.cleanup_group(process)
    assert time.monotonic() - started < 20
    assert status is not None and status.si_code == os.CLD_EXITED and status.si_status == 0, (
        failure_summary(output)
    )
    assert output.stat().st_size <= 4096
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "passed"
    assert receipt["historical_cause_proven"] is False
    assert receipt["gunicorn_version"] == locked
    recycle, control = receipt["cases"]
    assert recycle == {
        "recycle": True,
        "held_request_served": True,
        "cleanup": True,
        "fresh_get_timed_out_before_handler": True,
        "replacement_served": True,
        "timeout_not_retried": True,
    }
    assert control == {
        "recycle": False,
        "held_request_served": True,
        "cleanup": True,
        "fresh_get_served_while_held": True,
        "timeout_not_retried": True,
    }
    print("Verified locked Gunicorn single-worker recycle gap and no-recycle control")
