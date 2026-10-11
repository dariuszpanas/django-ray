"""Finite parent handling and actual Linux process-group lifetime contracts."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from coverage import Coverage

from django_ray.doctor import contracts, runner, supervisor


def fake_process(*, returncode=0):
    return SimpleNamespace(
        pid=12345,
        stdout=io.BytesIO(),
        stderr=io.BytesIO(),
        returncode=None,
        status=returncode,
        poll=Mock(side_effect=AssertionError("Leader must not be polled/reaped before escalation")),
        wait=Mock(return_value=returncode),
    )


@pytest.mark.parametrize("done_cleanup", [True, False])
def test_parent_preserves_cleanup_result_and_sanitized_sections(monkeypatch, done_cleanup):
    monkeypatch.setattr(runner, "peek_status", lambda process: process.status)
    process = fake_process()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def read(_process, _deadline, receive, _interrupted):
        for frame in (
            {"kind": "started", "pid": 12346},
            contracts.section_frame("configuration", contracts.observation("observed", "observed")),
            {"kind": "done", "code": "observed", "cleanup_confirmed": done_cleanup},
        ):
            receive(contracts.encode(frame))
        return "observed"

    monkeypatch.setattr(runner, "read_frames", read)
    sections = contracts.initial_sections()
    assert (
        runner._run_probe(
            "configuration", "owned.settings", "default", time.monotonic() + 3, sections
        )
        is done_cleanup
    )
    assert sections["configuration"]["code"] == "observed"
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize("all_received", [False, True])
def test_unconfirmed_cleanup_preserves_dated_counts_and_forces_incomplete_report(
    monkeypatch, all_received
):
    monkeypatch.setattr(runner, "peek_status", lambda process: process.status)
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *_args, **_kwargs: fake_process(returncode=2)
    )
    names = contracts.DATABASE_SECTIONS if all_received else contracts.DATABASE_SECTIONS[:-1]
    completed = {name: contracts.observation("observed", "observed") for name in names}
    completed["work"] = contracts.observation("blocker", "work_blockers", queued=2)

    def read(_process, _deadline, receive, _interrupted):
        receive(contracts.encode({"kind": "started", "pid": 12346}))
        for name, value in completed.items():
            receive(contracts.encode(contracts.section_frame(name, value)))
        receive(
            contracts.encode(
                {"kind": "done", "code": "cleanup_unconfirmed", "cleanup_confirmed": False}
            )
        )
        return "probe_failed"

    monkeypatch.setattr(runner, "read_frames", read)
    sections = contracts.initial_sections()
    cleanup = runner._run_probe(
        "database", "owned.settings", "default", time.monotonic() + 3, sections
    )
    assert not cleanup
    assert {name: sections[name] for name in completed} == completed
    if not all_received:
        assert sections["registered_cleanup"]["code"] == "cleanup_unconfirmed"
    report = contracts.result(sections, contracts.Limits(), 0, cleanup)
    assert not report["complete"] and not report["cleanup_confirmed"]
    assert report["exit_code"] == 2
    for as_json in (False, True):
        assert "cleanup_confirmed" in contracts.render(report, as_json=as_json)


def test_parent_exposes_failure_after_all_sections_were_emitted(monkeypatch):
    monkeypatch.setattr(runner, "peek_status", lambda process: process.status)
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *_args, **_kwargs: fake_process(returncode=2)
    )

    def read(_process, _deadline, receive, _interrupted):
        receive(contracts.encode({"kind": "started", "pid": 12346}))
        for name in contracts.DATABASE_SECTIONS:
            receive(
                contracts.encode(
                    contracts.section_frame(name, contracts.observation("observed", "observed"))
                )
            )
        receive(
            contracts.encode({"kind": "done", "code": "probe_failed", "cleanup_confirmed": True})
        )
        return "probe_failed"

    monkeypatch.setattr(runner, "read_frames", read)
    sections = contracts.initial_sections()
    assert runner._run_probe(
        "database", "owned.settings", "default", time.monotonic() + 3, sections
    )
    assert sections["database"]["code"] == "probe_failed"
    assert sections["work"]["code"] == "observed"


@pytest.mark.parametrize("cleanup", [False, PermissionError("private cleanup details")])
def test_parent_interruption_preserves_unconfirmed_cleanup_and_restores_handlers(
    monkeypatch, cleanup
):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_caller_sigchld_default", lambda: True)
    handlers = {}

    def install(number, handler):
        previous = handlers.get(number, "original")
        handlers[number] = handler
        return previous

    monkeypatch.setattr(runner.signal, "signal", install)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: fake_process())

    def read(*_args):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return "interrupted"

    def stop(*_args):
        # A second interrupt must not abort the first interrupt's cleanup.
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        if isinstance(cleanup, Exception):
            raise cleanup
        return cleanup

    monkeypatch.setattr(runner, "read_frames", read)
    monkeypatch.setattr(runner, "_stop_supervisor", stop)
    report = runner.diagnose(settings_module="owned.settings")
    assert not report["cleanup_confirmed"] and report["exit_code"] == 2
    assert report["sections"]["configuration"]["code"] == "cleanup_unconfirmed"
    assert all(value == "original" for value in handlers.values())
    assert "private" not in json.dumps(report)


def test_missing_cleanup_frame_cannot_be_claimed_clean(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: fake_process())
    monkeypatch.setattr(runner, "read_frames", lambda *_args: "probe_failed")
    monkeypatch.setattr(runner, "_stop_supervisor", lambda *_args: True)
    sections = contracts.initial_sections()
    assert not runner._run_probe(
        "configuration", "owned.settings", "default", time.monotonic() + 1, sections
    )
    assert sections["configuration"]["code"] == "cleanup_unconfirmed"


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        {"kind": "started", "pid": True},
        {"kind": "started", "pid": 12346, "secret": "private"},
        {"kind": "done", "code": "private", "cleanup_confirmed": True},
        contracts.section_frame("work", contracts.observation("observed", "observed")),
    ],
)
def test_parent_refuses_malformed_or_wrong_probe_frames_without_publishing_them(
    monkeypatch, invalid
):
    monkeypatch.setattr(runner, "peek_status", lambda process: process.status)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: fake_process())

    def read(_process, _deadline, receive, _interrupted):
        with pytest.raises(ValueError):
            receive(json.dumps(invalid).encode())
        receive(
            contracts.encode(
                {"kind": "done", "code": "probe_protocol_invalid", "cleanup_confirmed": True}
            )
        )
        return "probe_protocol_invalid"

    monkeypatch.setattr(runner, "read_frames", read)
    sections = contracts.initial_sections()
    assert runner._run_probe(
        "configuration", "owned.settings", "default", time.monotonic() + 3, sections
    )
    assert sections["configuration"]["code"] == "probe_protocol_invalid"
    assert sections["work"]["code"] == "not_observed"
    assert "private" not in json.dumps(sections)


def test_parent_process_creation_failure_is_sanitized_and_owns_no_cleanup(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(side_effect=OSError("private launcher")))
    monkeypatch.setattr(runner, "_stop_supervisor", Mock(side_effect=AssertionError("No child")))
    sections = contracts.initial_sections()
    assert runner._run_probe(
        "configuration", "owned.settings", "default", time.monotonic() + 3, sections
    )
    assert sections["configuration"]["code"] == "probe_failed"
    assert "private" not in json.dumps(sections)


def test_total_deadline_prevents_another_probe_and_retains_completed_observations(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "_caller_sigchld_default", lambda: True)
    monkeypatch.setattr(runner.signal, "signal", lambda *_args: signal.SIG_DFL)
    launched = []

    def run(name, _settings, _alias, deadline, sections, _interrupted):
        launched.append(name)
        assert deadline == 5
        sections[name] = contracts.observation("observed", "observed")
        # Simulate the caller regaining execution after its total budget expired.
        clock.now = 16.0
        return True

    monkeypatch.setattr(runner, "_run_probe", run)
    report = runner.diagnose(settings_module="owned.settings")
    assert launched == ["configuration"]
    assert report["sections"]["configuration"]["code"] == "observed"
    assert all(
        report["sections"][name]["code"] == "total_budget_exhausted"
        for name in contracts.DATABASE_SECTIONS
    )
    assert report["exit_code"] == 2 and report["cleanup_confirmed"]


def test_leading_dash_database_alias_remains_one_value_at_both_child_hops(monkeypatch):
    assert contracts.database_alias_valid("-archive")
    commands = []

    def start(command, **_kwargs):
        commands.append(command)
        raise OSError("owned startup unavailable")

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    runner._run_probe(
        "database", "owned.settings", "-archive", time.monotonic() + 5, contracts.initial_sections()
    )
    assert "--database=-archive" in commands[0] and "-archive" not in commands[0]
    monkeypatch.setattr(
        supervisor.sys,
        "argv",
        [
            "supervisor",
            "--probe",
            "database",
            "--settings",
            "owned.settings",
            "--database=-archive",
            "--deadline",
            str(time.monotonic() + 5),
        ],
    )

    def supervise(command, **_kwargs):
        commands.append(command)
        return 2

    monkeypatch.setattr(supervisor, "supervise", supervise)
    assert supervisor.main() == 2
    assert "--database=-archive" in commands[1] and "-archive" not in commands[1]


def test_exited_supervisor_is_signaled_only_before_reap(monkeypatch):
    signals = []
    process = fake_process()
    monkeypatch.setattr(runner, "peek_status", lambda _process: 0)
    monkeypatch.setattr(runner, "read_frames", lambda *_args: "probe_failed")

    def send(*args):
        assert not process.wait.called
        signals.append(args)

    monkeypatch.setattr(runner, "signal_group", send)
    assert runner._stop_supervisor(process, time.monotonic() + 1, lambda _raw: None)
    assert signals == [(12345, signal.SIGTERM)]
    process.wait.assert_called_once()


def test_failed_supervisor_reap_signals_only_pinned_supervisor_and_reports_unconfirmed(monkeypatch):
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    process = fake_process(returncode=None)
    process.wait.side_effect = subprocess.TimeoutExpired("private", 1)
    signals = []
    monkeypatch.setattr(runner, "peek_status", lambda _process: None)
    monkeypatch.setattr(runner, "read_frames", lambda *_args: "probe_timeout")
    monkeypatch.setattr(runner, "signal_group", lambda *args: signals.append(args))
    assert not runner._stop_supervisor(process, time.monotonic() + 0.01, lambda _raw: None)
    assert signals == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]


def test_already_reaped_leader_is_never_signaled(monkeypatch):
    process = fake_process()
    process.returncode = 0
    monkeypatch.setattr(
        supervisor, "signal_group", lambda *_args: pytest.fail("Recycled group signaled")
    )
    with pytest.raises(ChildProcessError):
        supervisor.cleanup_probe(process, time.monotonic() + 1)


def test_caller_sigchld_reaping_policy_is_refused_without_mutating_it(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(signal, "SIGCHLD", 17, raising=False)
    monkeypatch.setattr(signal, "getsignal", lambda _number: signal.SIG_IGN)
    monkeypatch.setattr(signal, "signal", lambda *_args: pytest.fail("Caller signal state changed"))
    report = runner.diagnose(settings_module="owned.settings")
    assert report["sections"]["database"]["code"] == "supervision_unavailable"
    assert report["exit_code"] == 2


def test_probe_leader_is_held_until_final_escalation(monkeypatch):
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    process = fake_process()
    events = []
    monkeypatch.setattr(supervisor, "peek_status", lambda _process: 0)
    monkeypatch.setattr(supervisor, "group_exists", lambda _pid: not process.wait.called)

    def send(_pid, value):
        assert not process.wait.called
        events.append(value)

    monkeypatch.setattr(supervisor, "signal_group", send)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _duration: None)

    def no_children(*_args):
        raise ChildProcessError

    monkeypatch.setattr(supervisor.os, "waitpid", no_children, raising=False)
    monkeypatch.setattr(supervisor.os, "waitid", no_children, raising=False)
    for name, value in {"P_ALL": 0, "WEXITED": 4, "WNOHANG": 1, "WNOWAIT": 16777216}.items():
        monkeypatch.setattr(supervisor.os, name, value, raising=False)
    assert supervisor.cleanup_probe(process, time.monotonic() + 1)
    assert events == [signal.SIGTERM, signal.SIGKILL]
    process.wait.assert_called_once()


def test_supervisor_cleanup_failure_is_fixed_and_does_not_escape(monkeypatch):
    monkeypatch.setattr(signal, "SIGCHLD", 17, raising=False)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "_enable_subreaper", lambda: True)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: fake_process())
    monkeypatch.setattr(supervisor, "read_frames", lambda *_args: "probe_failed")
    monkeypatch.setattr(supervisor, "cleanup_probe", Mock(side_effect=PermissionError("private")))
    frames = []
    assert (
        supervisor.supervise(
            ["owned"], probe="configuration", deadline=time.monotonic() + 3, emit=frames.append
        )
        == 2
    )
    assert frames[-1] == {"kind": "done", "code": "cleanup_unconfirmed", "cleanup_confirmed": False}
    assert "private" not in json.dumps(frames)


LINUX = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Actual owned process groups require Linux"
)


def measured_supervisor(source):
    """Measure only this test-owned supervisor, never arbitrary subprocesses.

    pytest-cov combines these parallel files with its parent measurements. The
    wrapper saves after the real supervisor returns, including error results;
    deliberately killed probe children do not need instrumentation.
    """
    active = Coverage.current()
    if active is None:
        return source
    options = {
        "data_file": str(Path(active.config.data_file).resolve()),
        "data_suffix": True,
        "config_file": False,
        "source": [str(Path(supervisor.__file__).parent)],
        "branch": active.config.branch,
    }
    return (
        "from coverage import Coverage\n"
        f"measurement=Coverage(**{options!r})\n"
        "measurement.start()\n"
        "try:\n"
        f" exec({source!r})\n"
        "finally:\n"
        " measurement.stop()\n"
        " measurement.save()\n"
    )


def launch_supervisor(command, deadline_seconds):
    source = (
        "import json,sys,time;from django_ray.doctor.supervisor import supervise;"
        "sys.exit(supervise(json.loads(sys.argv[1]),probe='configuration',"
        "deadline=time.monotonic()+float(sys.argv[2]),emit=lambda x:print(json.dumps(x),flush=True)))"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            measured_supervisor(source),
            json.dumps(command),
            str(deadline_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


@LINUX
@pytest.mark.parametrize("exit_code", [0, 1])
def test_supervisor_preserves_real_completed_frame_on_probe_success_or_failure(exit_code):
    observation = contracts.observation("observed", "observed", key_count=0)
    frame = contracts.section_frame("configuration", observation)
    source = f"import sys;print({json.dumps(frame)!r},flush=True);sys.exit({exit_code})"
    process = launch_supervisor([sys.executable, "-c", source], 4)
    try:
        stdout, stderr = process.communicate(timeout=6)
        frames = [json.loads(line) for line in stdout.splitlines()]
        assert frames[1] == frame
        assert frames[-1] == {
            "kind": "done",
            "code": "observed" if exit_code == 0 else "probe_failed",
            "cleanup_confirmed": True,
        }
        assert process.returncode == (0 if exit_code == 0 else 2) and not stderr
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


@LINUX
def test_frame_reader_keeps_exited_leader_waitable_without_reaping():
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert (
            supervisor.read_frames(process, time.monotonic() + 3, lambda _raw: None) == "observed"
        )
        assert process.returncode is None
        status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert status.si_pid == process.pid and status.si_status == 0
    finally:
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


@LINUX
@pytest.mark.parametrize(
    "leader_exits", [False, True], ids=["blocked-leader", "leader-exits-first"]
)
def test_blocked_probe_and_sigterm_ignoring_descendant_are_killed_and_reaped(
    tmp_path, leader_exits
):
    pid_file = tmp_path / "descendant.pid"
    ready_file = tmp_path / "descendant.ready"
    child_source = (
        "import pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready_file)!r}).write_text('ready');time.sleep(30)"
    )
    source = (
        "import subprocess,sys,signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"p=subprocess.Popen([sys.executable,'-c',{child_source!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
        f"while not pathlib.Path({str(ready_file)!r}).exists(): time.sleep(0.01)\n"
        + ("sys.exit(0)" if leader_exits else "time.sleep(30)")
    )
    process = launch_supervisor([sys.executable, "-c", source], 3)
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=6)
        frames = [json.loads(line) for line in stdout.splitlines()]
        assert frames[-1] == {
            "kind": "done",
            "code": "probe_protocol_invalid" if leader_exits else "probe_timeout",
            "cleanup_confirmed": True,
        }
        assert process.returncode == 2 and not stderr
        assert time.monotonic() - started < 5
        for pid in (frames[0]["pid"], int(pid_file.read_text())):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


@LINUX
@pytest.mark.parametrize(
    "stream,size", [("stdout", contracts.STDOUT_BYTES + 1), ("stderr", contracts.STDERR_BYTES + 1)]
)
def test_child_output_flood_is_bounded_and_secret_free(stream, size):
    source = (
        f"import sys,time;sys.{stream}.write('private'*{size});sys.{stream}.flush();time.sleep(30)"
    )
    process = launch_supervisor([sys.executable, "-c", source], 4)
    try:
        stdout, stderr = process.communicate(timeout=6)
        frames = [json.loads(line) for line in stdout.splitlines()]
        assert frames[-1] == {
            "kind": "done",
            "code": "output_budget_exhausted",
            "cleanup_confirmed": True,
        }
        assert len(stdout) < 1024 and not stderr and b"private" not in stdout
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


@LINUX
def test_supervisor_interrupt_confirms_group_cleanup(tmp_path):
    process = launch_supervisor([sys.executable, "-c", "import time;time.sleep(30)"], 5)
    try:
        first = json.loads(process.stdout.readline())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=4)
        assert json.loads(stdout.splitlines()[-1]) == {
            "kind": "done",
            "code": "interrupted",
            "cleanup_confirmed": True,
        }
        assert not stderr
        with pytest.raises(ProcessLookupError):
            os.kill(first["pid"], 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


@LINUX
def test_escaped_adopted_descendant_reports_unconfirmed_and_fixture_reaps_it(tmp_path):
    pid_file = tmp_path / "escaped.pid"
    child = "import time;time.sleep(30)"
    probe_source = (
        "import pathlib,subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    # The isolated fixture supervisor owns the escaped child after adoption.
    # It observes the doctor result first, then kills/reaps that exact still-
    # waitable child without signaling a cached or recycled process-group ID.
    harness = (
        "import json,os,pathlib,signal,sys,time\n"
        "from django_ray.doctor.supervisor import supervise\n"
        "try:\n"
        " result=supervise(json.loads(sys.argv[1]),probe='configuration',deadline=time.monotonic()+4,emit=lambda x:print(json.dumps(x),flush=True))\n"
        "finally:\n"
        " if pathlib.Path(sys.argv[2]).exists():\n"
        "  pid=int(pathlib.Path(sys.argv[2]).read_text())\n"
        "  try: os.waitid(os.P_PID,pid,os.WEXITED|os.WNOHANG|os.WNOWAIT)\n"
        "  except ChildProcessError: pass\n"
        "  else:\n"
        "   os.kill(pid,signal.SIGKILL)\n"
        "   os.waitpid(pid,0)\n"
        "sys.exit(result)\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            measured_supervisor(harness),
            json.dumps([sys.executable, "-c", probe_source]),
            str(pid_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=7)
        frames = [json.loads(line) for line in stdout.splitlines()]
        assert frames[-1] == {
            "kind": "done",
            "code": "cleanup_unconfirmed",
            "cleanup_confirmed": False,
        }
        assert process.returncode == 2 and not stderr
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
