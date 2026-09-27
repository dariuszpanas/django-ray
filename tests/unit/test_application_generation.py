"""Resource-free checks of the fixed generation phase and process boundaries."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.application import run_generation as runner


def test_generation_cli_starts_without_site_packages(tmp_path):
    script = """
import runpy
import sys
sys.path.insert(0, sys.argv[1])
sys.argv = ["qualification.application.run_generation", "--help"]
try:
    runpy.run_module("qualification.application.run_generation", run_name="__main__")
except SystemExit as error:
    assert error.code == 0
assert not any(name in sys.modules for name in ("django", "django_ray", "ray"))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(Path(__file__).resolve().parents[2])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_sigterm_preserves_signal_exit_and_cleans_active_child(monkeypatch, capsys):
    process = Mock(returncode=None)
    process.wait.side_effect = lambda **_: runner._terminated(15, None)
    stop = Mock()
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner, "_stop_owned_process", stop)
    with pytest.raises(SystemExit) as caught:
        runner.run_command(["child"], 10)
    assert caught.value.code == 143
    stop.assert_called_once_with(process)
    assert json.loads(capsys.readouterr().out)["reason"] == "terminated"


@pytest.mark.parametrize("generation", ["before", "after"])
def test_generation_retains_fixed_order_receipts_and_previous_generation(generation):
    commands = runner.generation_commands(
        generation, "http://django-web:8000", Path("/credentials/token")
    )
    assert [name for name, _ in commands] == ["first", "nodes", "core", "workflows"]
    assert [argv[2] for _, argv in commands] == [
        "qualification.application.run_first_workflows",
        "qualification.application.generic_nodes",
        "qualification.application.run_core",
        "qualification.application.run_workflows",
    ]
    for name, argv in commands:
        assert argv[:2] == [sys.executable, "-m"]
        assert argv[argv.index("--receipt") + 1] == f"/receipts/{generation}-{name}.json"
        assert not any("&&" in arg for arg in argv)
    nodes = commands[1][1]
    assert ("--previous-receipt" in nodes) == (generation == "after")
    if generation == "after":
        assert nodes[nodes.index("--previous-receipt") + 1] == "/receipts/before-nodes.json"


def _commands():
    return tuple((name, [name]) for name in ("first", "nodes", "core", "workflows"))


def test_existing_commands_share_one_budget_without_unused_first_time(monkeypatch, capsys):
    now = [0.0]
    observed = []
    durations = iter([30, 200, 100, 250])
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    def execute(argv, timeout):
        observed.append((argv[0], timeout))
        now[0] += next(durations)
        return 0

    monkeypatch.setattr(runner, "run_command", execute)
    assert runner.run_phases(_commands()) == 0
    assert observed == [("first", 180), ("nodes", 600), ("core", 400), ("workflows", 300)]
    markers = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(m["generation_phase"], m["status"]) for m in markers] == [
        ("first", "started"),
        ("first", "passed"),
        ("existing", "started"),
        ("existing", "passed"),
    ]
    assert markers[-1]["elapsed_seconds"] == 550


def test_first_failure_stops_all_remaining_work(monkeypatch, capsys):
    execute = Mock(return_value=7)
    monkeypatch.setattr(runner, "run_command", execute)
    assert runner.run_phases(_commands()) == 7
    assert execute.call_count == 1
    markers = capsys.readouterr().out
    assert '"returncode": 7' in markers
    assert '"existing"' not in markers
    assert '"passed"' not in markers


@pytest.mark.parametrize(("code", "expected"), [(126, 126), (143, 143), (255, 255), (-15, 143)])
def test_child_exit_status_is_preserved(monkeypatch, capsys, code, expected):
    execute = Mock(return_value=code)
    monkeypatch.setattr(runner, "run_command", execute)
    assert runner.run_phases(_commands()) == expected
    assert execute.call_count == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["returncode"] == code


def test_already_reaped_leader_never_signals_a_process_group(monkeypatch):
    process = Mock(returncode=7)
    stop = Mock()
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="posix", killpg=stop))
    runner._stop_owned_process(process)
    stop.assert_not_called()
    process.wait.assert_not_called()


def test_normal_nonzero_exit_does_not_run_group_cleanup(monkeypatch):
    process = Mock(returncode=7)
    process.wait.return_value = 7
    stop = Mock()
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner, "_stop_owned_process", stop)
    assert runner.run_command(["child"], 1) == 7
    stop.assert_not_called()


@pytest.mark.parametrize("phase", ["first", "existing"])
def test_timeout_stops_later_commands_and_never_emits_success(monkeypatch, capsys, phase):
    calls = []

    def execute(argv, timeout):
        calls.append(argv[0])
        if argv[0] == ("first" if phase == "first" else "core"):
            raise subprocess.TimeoutExpired(argv, timeout)
        return 0

    monkeypatch.setattr(runner, "run_command", execute)
    assert runner.run_phases(_commands()) == 124
    assert calls == (["first"] if phase == "first" else ["first", "nodes", "core"])
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last == {
        "generation_phase": phase,
        "status": "failed",
        "command": calls[-1],
        "reason": "timeout",
    }


def test_exhausted_shared_budget_does_not_launch_another_command(monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    def execute(argv, timeout):
        calls.append(argv[0])
        now[0] += 601 if argv[0] == "nodes" else 1
        return 0

    monkeypatch.setattr(runner, "run_command", execute)
    assert runner.run_phases(_commands()) == 124
    assert calls == ["first", "nodes"]


def test_timeout_escalates_owned_group_even_when_leader_exits_on_term(monkeypatch):
    events = []
    process = Mock(pid=123, returncode=None)
    process.wait.side_effect = [subprocess.TimeoutExpired(["child"], 1), 0]
    popen = Mock(return_value=process)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(
        runner,
        "os",
        SimpleNamespace(name="posix", killpg=lambda pid, sig: events.append((pid, sig))),
    )
    monkeypatch.setattr(runner, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: events.append(("grace", seconds)))
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_command(["child"], 1)
    assert popen.call_args.kwargs == {"start_new_session": True}
    assert events == [(123, 15), ("grace", 2), (123, 9)]
    assert process.wait.call_args.kwargs == {"timeout": 2}


def test_cleanup_error_does_not_replace_original_timeout(monkeypatch):
    process = Mock()
    original = subprocess.TimeoutExpired(["child"], 1)
    process.wait.side_effect = original
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner, "_stop_owned_process", Mock(side_effect=OSError("private error")))
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        runner.run_command(["child"], 1)
    assert caught.value is original


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux process-group and kernel-owned descendant cleanup boundary",
)
def test_owned_group_stop_kills_ready_descendant_that_ignores_term(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"p=Path({str(pid_file)!r}); "
        "p.with_suffix('.ready').write_text(str(os.getpid())); "
        "p.with_suffix('.ready').replace(p); time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
    descendant_fd = None
    try:
        ready_deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < ready_deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "The descendant must signal readiness before cleanup"
        pid = int(pid_file.read_text())
        descendant_fd = os.pidfd_open(pid)
        runner._stop_owned_process(process)
        assert select.select([descendant_fd], [], [], 2)[0] == [descendant_fd]
    finally:
        # Keep the leader unreaped until group escalation, including failures
        # during the readiness handshake. Never signal a reaped/reused PID.
        try:
            runner._stop_owned_process(process)
        finally:
            if descendant_fd is not None:
                try:
                    signal.pidfd_send_signal(descendant_fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(descendant_fd)


def test_phase_markers_do_not_change_receipt_collection(monkeypatch, capsys):
    from qualification.application.run_chainsaw import parse_receipts

    monkeypatch.setattr(runner, "run_command", Mock(return_value=0))
    assert runner.run_phases(_commands()) == 0
    raw = capsys.readouterr().out.encode()
    raw += json.dumps(
        {
            "schema_version": 1,
            "layer": "application_core",
            "status": "passed",
            "complete_application_gate": False,
        }
    ).encode()
    assert set(parse_receipts(raw, ("before-core",))) == {"before-core"}
