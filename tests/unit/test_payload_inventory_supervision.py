"""Fake-process coverage of inventory supervision, receipts, and entry validation."""

from __future__ import annotations

import io
import json
import signal
import subprocess
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_ray.payload_inventory import contracts, probe, process


def summary():
    return {
        "kind": "summary",
        "code": "observed",
        "entries": 0,
        "objects": 0,
        "counts": dict.fromkeys(contracts.OUTCOMES, 0),
    }


@pytest.fixture
def supervisor(monkeypatch):
    handlers = {}
    monkeypatch.setattr(process.sys, "platform", "linux")
    monkeypatch.setattr(signal, "SIGCHLD", 17, raising=False)
    monkeypatch.setattr(
        process.signal, "signal", lambda number, handler: handlers.update({number: handler})
    )
    monkeypatch.setattr(process, "_enable_subreaper", lambda: True)
    child = SimpleNamespace(pid=12345, stdout=io.BytesIO(), stderr=io.BytesIO(), wait=Mock())
    launch = Mock(return_value=child)
    cleanup = Mock(return_value=True)
    monkeypatch.setattr(process.subprocess, "Popen", launch)
    monkeypatch.setattr(process, "cleanup_probe", cleanup)
    frames = []
    return SimpleNamespace(
        child=child, launch=launch, cleanup=cleanup, frames=frames, handlers=handlers
    )


@pytest.mark.parametrize(
    "result,send_summary,expected",
    [
        ("observed", True, "observed"),
        ("observed", False, "probe_protocol_invalid"),
        ("probe_failed", True, "probe_failed"),
        ("probe_timeout", False, "probe_timeout"),
    ],
)
def test_supervisor_requires_complete_protocol_and_successful_child_exit(
    supervisor, monkeypatch, result, send_summary, expected
):
    def read(child, deadline, receive, interrupted):
        assert child is supervisor.child and deadline == 108
        assert not interrupted()
        if send_summary:
            receive(json.dumps(summary()).encode())
        return result

    monkeypatch.setattr(process, "read_frames", read)
    monkeypatch.setattr(process.time, "monotonic", lambda: 100)
    status = process.supervise(
        ["owned-probe"], deadline=110, limits=contracts.Limits(), emit=supervisor.frames.append
    )
    assert status == (0 if expected == "observed" else 2)
    assert supervisor.frames[0] == {"kind": "started", "pid": 12345}
    assert supervisor.frames[-1] == {"kind": "done", "code": expected, "cleanup_confirmed": True}
    assert len(supervisor.frames) == (3 if send_summary else 2)
    supervisor.cleanup.assert_called_once_with(supervisor.child, 102)
    assert supervisor.child.stdout.closed and supervisor.child.stderr.closed
    assert supervisor.launch.call_args.kwargs["start_new_session"] is True


@pytest.mark.parametrize("failure", ["launch", "protocol", "cleanup", "stream"])
def test_supervisor_failures_are_fixed_and_cleanup_failure_takes_precedence(
    supervisor, monkeypatch, failure
):
    def read(_child, _deadline, receive, _interrupted):
        receive(json.dumps({"kind": "private-child-text"}).encode())
        pytest.fail("Invalid child protocol was accepted")

    monkeypatch.setattr(process, "read_frames", read)
    if failure == "launch":
        supervisor.launch.side_effect = OSError("private executable path")
    elif failure == "cleanup":
        supervisor.cleanup.side_effect = OSError("private cleanup detail")
    elif failure == "stream":
        supervisor.child.stdout = SimpleNamespace(close=Mock(side_effect=OSError("private stream")))
    assert (
        process.supervise(
            ["owned-probe"], deadline=110, limits=contracts.Limits(), emit=supervisor.frames.append
        )
        == 2
    )
    cleanup_failed = failure in {"cleanup", "stream"}
    assert supervisor.frames[-1] == {
        "kind": "done",
        "code": "cleanup_unconfirmed" if cleanup_failed else "probe_failed",
        "cleanup_confirmed": not cleanup_failed,
    }
    assert "private" not in json.dumps(supervisor.frames)
    if failure == "launch":
        supervisor.cleanup.assert_not_called()
    else:
        supervisor.cleanup.assert_called_once()
        assert supervisor.child.stderr.closed


def test_supervisor_interrupt_is_observed_without_skipping_cleanup(supervisor, monkeypatch):
    def read(_child, _deadline, _receive, interrupted):
        supervisor.handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert interrupted()
        return "interrupted"

    monkeypatch.setattr(process, "read_frames", read)
    assert (
        process.supervise(
            ["owned-probe"], deadline=110, limits=contracts.Limits(), emit=supervisor.frames.append
        )
        == 2
    )
    assert supervisor.frames[-1]["code"] == "interrupted"
    assert supervisor.frames[-1]["cleanup_confirmed"]
    supervisor.cleanup.assert_called_once()


@pytest.mark.parametrize("platform,subreaper", [("win32", True), ("linux", False)])
def test_unavailable_supervision_has_no_child_or_signal_side_effects(
    supervisor, monkeypatch, platform, subreaper
):
    monkeypatch.setattr(process.sys, "platform", platform)
    monkeypatch.setattr(process, "_enable_subreaper", lambda: subreaper)
    assert (
        process.supervise(
            ["owned-probe"], deadline=110, limits=contracts.Limits(), emit=supervisor.frames.append
        )
        == 2
    )
    assert supervisor.frames == [
        {"kind": "done", "code": "supervision_unavailable", "cleanup_confirmed": True}
    ]
    supervisor.launch.assert_not_called()
    assert supervisor.handlers == {}


@pytest.mark.parametrize("failure", ["wait", "stream"])
def test_parent_reap_or_stream_failure_cannot_claim_cleanup(supervisor, monkeypatch, failure):
    def read(_child, _deadline, receive, _interrupted):
        for value in (
            {"kind": "started", "pid": 12346},
            summary(),
            {"kind": "done", "code": "observed", "cleanup_confirmed": True},
        ):
            receive(json.dumps(value).encode())
        return "observed"

    monkeypatch.setattr(process, "read_frames", read)
    monkeypatch.setattr(process, "peek_status", lambda _child: 0)
    if failure == "wait":
        supervisor.child.wait.side_effect = subprocess.TimeoutExpired("private command", 1)
    else:
        supervisor.child.stderr = SimpleNamespace(close=Mock(side_effect=OSError("private stream")))
    frames = contracts.Frames(contracts.Limits())
    code, cleanup = process._run(
        settings_module="owned.settings",
        using="default",
        deadline=110,
        limits=frames.limits,
        frames=frames,
        interrupted=lambda: False,
    )
    report = contracts.report(frames, code=code, cleanup=cleanup, elapsed=1)
    assert report["code"] == "cleanup_unconfirmed" and report["exit_code"] == 2
    assert report["counts_complete"] and "private" not in json.dumps(report)


@pytest.mark.parametrize("mode", ["probe", "supervisor"])
def test_private_entry_preserves_alias_deadline_and_framed_output(monkeypatch, capsys, mode):
    monkeypatch.setattr(process.sys, "platform", "linux")
    monkeypatch.setattr(process.time, "monotonic", lambda: 100)
    limits = contracts.Limits(max_objects=7, max_details=0, shard="abcd")
    monkeypatch.setattr(
        process.sys,
        "argv",
        [
            "inventory",
            "--mode=" + mode,
            "--settings=owned.settings",
            "--database=-archive",
            "--deadline=110",
            "--limits=" + json.dumps(asdict(limits)),
        ],
    )

    def run(**options):
        assert options["using"] == "-archive" and options["settings_module"] == "owned.settings"
        assert options["limits"] == limits and options["deadline"] == 110
        options["emit"](summary())
        return 0

    def supervise(command, **options):
        assert "--database=-archive" in command and "-archive" not in command
        assert "--mode=probe" in command and "--deadline=108.0" in command
        assert options["limits"] == limits and options["deadline"] == 110
        options["emit"]({"kind": "done", "code": "observed", "cleanup_confirmed": True})
        return 0

    monkeypatch.setattr(probe, "run", run)
    monkeypatch.setattr(process, "supervise", supervise)
    assert process.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["kind"] == ("summary" if mode == "probe" else "done")
    assert not output.err


@pytest.mark.parametrize(
    "argument",
    [
        "--limits=[]",
        "--limits={",
        '--limits={"max_objects":0}',
        "--settings=invalid/private",
        "--database=invalid/private",
        "--deadline=100",
        "--deadline=161",
        "--deadline=nan",
    ],
)
def test_private_entry_refuses_invalid_input_before_any_probe(monkeypatch, argument):
    monkeypatch.setattr(process.sys, "platform", "linux")
    monkeypatch.setattr(process.time, "monotonic", lambda: 100)
    monkeypatch.setattr(
        process.sys,
        "argv",
        [
            "inventory",
            "--mode=probe",
            "--settings=owned.settings",
            "--database=default",
            "--deadline=110",
            "--limits={}",
            argument,
        ],
    )
    run = Mock(side_effect=AssertionError("Probe must not start"))
    monkeypatch.setattr(probe, "run", run)
    assert process.main() == 2
    run.assert_not_called()
