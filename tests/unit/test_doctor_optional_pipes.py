"""Resource-free optional-pipe cleanup checks; no OS process or signal calls."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_ray.doctor import contracts, runner, supervisor


def fake_process():
    return SimpleNamespace(
        pid=12345,
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
        returncode=None,
        status=0,
        wait=Mock(return_value=0),
    )


@pytest.mark.parametrize("missing_stream", [None, "stdout", "stderr"])
def test_parent_missing_pipe_preserves_observation_but_refuses_confirmed_cleanup(
    monkeypatch, missing_stream
):
    process = fake_process()
    if missing_stream is not None:
        getattr(process, missing_stream).close()
        setattr(process, missing_stream, None)
    monkeypatch.setattr(runner, "peek_status", lambda child: child.status)
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: 10))
    monkeypatch.setattr(
        runner, "_stop_supervisor", Mock(side_effect=AssertionError("completed supervisor stopped"))
    )
    monkeypatch.setattr(
        runner, "signal_group", Mock(side_effect=AssertionError("unexpected process signal"))
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    completed = contracts.observation("observed", "observed")

    def read(_process, _deadline, receive, _interrupted):
        for frame in (
            {"kind": "started", "pid": 12346},
            contracts.section_frame("configuration", completed),
            {"kind": "done", "code": "observed", "cleanup_confirmed": True},
        ):
            receive(contracts.encode(frame))
        return "observed"

    monkeypatch.setattr(runner, "read_frames", read)
    sections = contracts.initial_sections()
    cleanup = runner._run_probe("configuration", "owned.settings", "default", 13, sections)

    assert cleanup is (missing_stream is None)
    assert sections["configuration"] == completed
    process.wait.assert_called_once()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            assert stream.closed
    report = contracts.result(sections, contracts.Limits(), 0, cleanup)
    assert report["cleanup_confirmed"] is cleanup
    if missing_stream is not None:
        assert report["exit_code"] == 2 and not report["complete"]


@pytest.mark.parametrize("missing_stream", [None, "stdout", "stderr"])
def test_supervisor_missing_pipe_closes_other_pipe_and_retains_unconfirmed_cleanup(
    monkeypatch, missing_stream
):
    process = fake_process()
    if missing_stream is not None:
        getattr(process, missing_stream).close()
        setattr(process, missing_stream, None)
    monkeypatch.setattr(supervisor, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: 10))
    monkeypatch.setattr(supervisor, "os", SimpleNamespace())
    monkeypatch.setattr(
        supervisor, "signal_group", Mock(side_effect=AssertionError("unexpected process signal"))
    )
    monkeypatch.setattr(
        supervisor,
        "signal",
        SimpleNamespace(SIGCHLD=1, SIG_DFL=0, SIGTERM=2, SIGINT=3, signal=Mock()),
    )
    monkeypatch.setattr(supervisor, "_enable_subreaper", lambda: True)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: process)
    cleanup = Mock(return_value=True)
    monkeypatch.setattr(supervisor, "cleanup_probe", cleanup)
    completed = contracts.observation("observed", "observed")

    def read(_process, _deadline, receive, _interrupted):
        receive(contracts.encode(contracts.section_frame("configuration", completed)))
        return "observed"

    monkeypatch.setattr(supervisor, "read_frames", read)
    frames = []
    status = supervisor.supervise(
        ["owned-probe"], probe="configuration", deadline=13, emit=frames.append
    )

    cleanup.assert_called_once()
    assert cleanup.call_args.args[0] is process
    assert frames[1] == contracts.section_frame("configuration", completed)
    assert frames[-1] == {
        "kind": "done",
        "code": "observed" if missing_stream is None else "cleanup_unconfirmed",
        "cleanup_confirmed": missing_stream is None,
    }
    assert status == (0 if missing_stream is None else 2)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            assert stream.closed
