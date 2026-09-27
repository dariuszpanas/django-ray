"""The diagnostic sampler stays finite and never hides a failed observation."""

from __future__ import annotations

import io
import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import investigate_pytest_shutdown as investigation


def test_fixed_plan_samples_fresh_processes_and_pairs_shutdown_policies():
    plan = investigation.probes()
    assert len(plan) == 53
    assert len({probe.name for probe in plan}) == 53
    assert Counter(probe.stage for probe in plan) == {"imports": 40, "collection": 5, "ray": 8}
    assert Counter(probe.recipe for probe in plan) == {
        "grpc": 10,
        "ray": 10,
        "grpc-ray": 10,
        "ray-grpc": 10,
        "collect": 5,
        "ray-bare": 4,
        "ray-wait": 4,
    }
    assert [probe.recipe for probe in plan[-8:]] == ["ray-bare", "ray-wait"] * 4


def test_first_failed_process_stops_sampling_and_retains_receipt(tmp_path):
    attempted = []

    def run(probe, timeout, output):
        attempted.append(probe.name)
        return {"name": probe.name, "passed": False, "returncode": 139}

    result = investigation.investigate(tmp_path, runner=run)
    assert len(attempted) == 1
    assert result["outcome"] == "failed-observation"
    assert result["intent"] == "diagnostic-only"
    assert json.loads((tmp_path / "receipt.json").read_text()) == result


def test_stage_deadline_includes_cleanup_reserve_and_fails_closed(tmp_path):
    now = [0.0]
    budgets = []

    def run(probe, timeout, output):
        budgets.append(timeout)
        now[0] += 295
        return {"name": probe.name, "passed": True}

    result = investigation.investigate(tmp_path, runner=run, clock=lambda: now[0])
    assert budgets == [30]
    assert result["outcome"] == "deadline-exhausted"
    assert len(result["observations"]) == 1


def test_complete_sampling_is_reported_only_as_no_reproduction(tmp_path, capsys):
    def run(probe, timeout, output):
        return {
            "name": probe.name,
            "recipe": probe.recipe,
            "stage": probe.stage,
            "returncode": 0,
            "passed": True,
            "timed_out": False,
            "interrupted": False,
            "cleanup_complete": True,
            "descendants_terminated": 0,
            "cleanup_needed_after_exit": False,
            "capture_complete": True,
            "elapsed_seconds": 0.1,
            "output_bytes": 42,
            "retained_bytes": 42,
        }

    result = investigation.investigate(tmp_path, runner=run)
    assert len(result["observations"]) == 53
    assert result["outcome"] == "complete-no-reproduction"
    assert result["intent"] == "diagnostic-only"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    finished = [event for event in events if event["shutdown_investigation"] == "probe_finished"]
    assert len(finished) == 53
    assert [event["stage"] for event in finished] == [
        probe.stage for probe in investigation.probes()
    ]


def test_cleaned_bare_shutdown_leftovers_are_recorded_without_losing_comparison(tmp_path):
    def run(probe, timeout, output):
        return {
            "name": probe.name,
            "passed": True,
            "cleanup_needed_after_exit": probe.recipe == "ray-bare",
        }

    result = investigation.investigate(tmp_path, runner=run)
    assert result["outcome"] == "complete-no-reproduction"
    assert result["completed"] == 53
    assert result["cleanup_interventions"] == 4


@pytest.mark.parametrize("recipe", list(investigation.IMPORTS))
def test_import_recipes_preserve_order_without_initializing_ray(monkeypatch, recipe):
    imported = []
    monkeypatch.setattr(investigation, "require_linux", lambda: None)
    monkeypatch.setattr(investigation, "import_module", imported.append)
    monkeypatch.setattr(investigation.atexit, "register", lambda *args, **kwargs: None)
    assert investigation.run_child(recipe) == 0
    assert imported == list(investigation.IMPORTS[recipe])


@pytest.mark.parametrize("recipe, expected_wait", [("ray-bare", False), ("ray-wait", True)])
def test_ray_recipes_keep_work_equal_and_change_only_shutdown_wait(
    monkeypatch, recipe, expected_wait
):
    from tests import local_ray

    initializations = []
    shutdowns = []
    fake_ray = SimpleNamespace(
        remote=lambda function: SimpleNamespace(remote=function),
        get=lambda value, timeout: value,
        shutdown=lambda **kwargs: shutdowns.append(kwargs),
    )
    monkeypatch.setitem(investigation.sys.modules, "ray", fake_ray)
    monkeypatch.setattr(investigation, "require_linux", lambda: None)
    monkeypatch.setattr(investigation.atexit, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        local_ray, "init_local_ray", lambda **kwargs: initializations.append(kwargs)
    )
    assert investigation.run_child(recipe) == 0
    assert initializations == [{"num_cpus": 2, "include_dashboard": False}] * 3
    assert shutdowns == [{"wait_for_processes": expected_wait}] * 3


def test_non_linux_entrypoint_rejects_before_starting_any_process(monkeypatch, tmp_path):
    monkeypatch.setattr(investigation.sys, "platform", "win32")
    with pytest.raises(SystemExit, match="requires Linux"):
        investigation.main(["--output", str(tmp_path)])
    assert not list(tmp_path.iterdir())


@pytest.fixture
def cleanup_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(investigation.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(investigation.time, "sleep", sleep)
    monkeypatch.setattr(investigation.os, "WNOHANG", 1, raising=False)
    return now


def test_cleanup_targets_only_owned_descendants(monkeypatch, cleanup_clock):
    child = Mock(spec=investigation.psutil.Process)
    monkeypatch.setattr(
        investigation.psutil,
        "Process",
        lambda: SimpleNamespace(children=lambda recursive: [child]),
    )

    def waitpid(pid, flags):
        assert (pid, flags) == (-1, investigation.os.WNOHANG)
        if child.kill.called:
            raise ChildProcessError
        return 0, 0

    monkeypatch.setattr(investigation.os, "waitpid", waitpid)
    process = SimpleNamespace(wait=lambda timeout: 0)
    assert investigation.cleanup_owned(process) == (True, 1)
    child.terminate.assert_called_once_with()
    child.kill.assert_called_once_with()
    assert 3 <= cleanup_clock[0] < 6


def test_empty_snapshot_cannot_hide_live_adopted_child(monkeypatch, cleanup_clock):
    child = Mock(spec=investigation.psutil.Process)
    inventories = iter([[], [child]])
    monkeypatch.setattr(
        investigation.psutil,
        "Process",
        lambda: SimpleNamespace(children=lambda recursive: next(inventories)),
    )
    kernel_results = iter([(0, 0), (42, 0), ChildProcessError()])

    def waitpid(pid, flags):
        result = next(kernel_results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(investigation.os, "waitpid", waitpid)
    process = SimpleNamespace(wait=lambda timeout: 0)
    assert investigation.cleanup_owned(process) == (True, 1)
    child.terminate.assert_called_once_with()
    child.kill.assert_not_called()
    assert cleanup_clock[0] > 0


def test_missing_descendant_until_deadline_cannot_be_reported_clean(monkeypatch, cleanup_clock):
    monkeypatch.setattr(
        investigation.psutil, "Process", lambda: SimpleNamespace(children=lambda recursive: [])
    )
    monkeypatch.setattr(investigation.os, "waitpid", lambda pid, flags: (0, 0))
    process = SimpleNamespace(wait=lambda timeout: 0)
    assert investigation.cleanup_owned(process) == (False, 0)
    assert cleanup_clock[0] == 6


def test_kernel_reaping_waits_for_popen_to_preserve_launcher_exit(monkeypatch, cleanup_clock):
    monkeypatch.setattr(
        investigation.psutil, "Process", lambda: SimpleNamespace(children=lambda recursive: [])
    )
    process = SimpleNamespace(returncode=None)
    waits = []

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            raise investigation.subprocess.TimeoutExpired("owned-probe", timeout)
        process.returncode = 139
        return process.returncode

    def waitpid(pid, flags):
        assert process.returncode == 139
        assert len(waits) == 2
        raise ChildProcessError

    process.wait = wait
    monkeypatch.setattr(investigation.os, "waitpid", waitpid)
    assert investigation.cleanup_owned(process) == (True, 0)
    assert process.returncode == 139


def test_child_timeout_cleans_up_and_cannot_be_reported_as_success(monkeypatch, tmp_path):
    actions = []

    def wait(timeout):
        raise investigation.subprocess.TimeoutExpired("owned-probe", timeout)

    process = SimpleNamespace(
        pid=42, stdout=io.BytesIO(b"bounded child output\n"), returncode=0, wait=wait
    )
    monkeypatch.setattr(investigation.subprocess, "Popen", lambda *args, **kwargs: process)

    def cleanup(owned):
        assert owned is process
        actions.append("cleanup")
        return True, 1

    monkeypatch.setattr(investigation, "cleanup_owned", cleanup)
    observation = investigation.run_probe(investigation.probes()[0], 0.01, tmp_path)
    assert observation["timed_out"] is True
    assert observation["passed"] is False
    assert observation["returncode"] is None
    assert observation["cleanup_complete"] is True
    assert actions == ["cleanup"]
    assert (tmp_path / "imports-grpc-00.log").read_bytes() == b"bounded child output\n"
