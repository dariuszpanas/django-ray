"""Worker-mode receipt, budget, CLI and owned-cleanup refusal boundaries."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.upgrade import worker_modes


def test_mode_task_imports_with_real_native_settings_without_database_or_ray(tmp_path):
    environment = dict(os.environ)
    environment.update(
        DJANGO_SETTINGS_MODULE="qualification.upgrade.native_settings",
        DJANGO_RAY_UPGRADE_ROOT=str(tmp_path),
        DJANGO_RAY_UPGRADE_CONFIG=json.dumps(
            {
                "database": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
                "artifacts": str(tmp_path),
                "runner": "ray_core",
                "worker_mode_probe": True,
            }
        ),
    )
    script = """
import sys
import django
django.setup()
from django.db import connection
from qualification.upgrade.mode_tasks import mode_value
assert mode_value.using(queue_name='qualification-sync').queue_name == 'qualification-sync'
assert mode_value.using(queue_name='qualification-core').queue_name == 'qualification-core'
assert connection.connection is None
assert 'ray' not in sys.modules
print('mode task settings accepted without database or Ray')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "mode task settings accepted without database or Ray"


@pytest.mark.parametrize("mode", worker_modes.MODES)
def test_cli_selects_exact_mode_queue_and_single_slot(mode):
    args = worker_modes.command(mode)
    assert args[:4] == [sys.executable, "-m", "django", "django_ray_worker"]
    assert args[-2:] == [f"--queue=qualification-{mode}", "--concurrency=1"]
    assert args[4:-2] == (["--sync"] if mode == "sync" else ["--cluster", "auto"])


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        worker_modes.command("unknown")


@pytest.mark.parametrize("mode", worker_modes.MODES)
@pytest.mark.parametrize(
    "field,value",
    [
        ("value", True),
        ("value", 43),
        ("pid", True),
        ("pid", -1),
        ("module", "other"),
        ("ray_initialized", 1),
    ],
)
def test_task_result_refuses_wrong_or_loosely_typed_witnesses(mode, field, value):
    result = {
        "value": 42,
        "pid": 11 if mode == "sync" else 12,
        "module": "installed",
        "ray_initialized": mode == "core",
    }
    result[field] = value
    with pytest.raises(ValueError):
        worker_modes.validate_result(result, mode=mode, manager_pid=11, module="installed")


@pytest.mark.parametrize("mode", worker_modes.MODES)
def test_wrong_process_identity_is_rejected(mode):
    result = {
        "value": 42,
        "pid": 12 if mode == "sync" else 11,
        "module": "installed",
        "ray_initialized": mode == "core",
    }
    with pytest.raises(ValueError):
        worker_modes.validate_result(result, mode=mode, manager_pid=11, module="installed")


@pytest.mark.parametrize("fact", sorted(worker_modes.FACTS))
def test_incomplete_receipt_never_passes(fact):
    receipt = {
        "schema_version": 1,
        "modes": {mode: dict.fromkeys(worker_modes.FACTS, True) for mode in worker_modes.MODES},
        "deployment_equivalence": "core-mode-only-not-all-manager-deployments",
    }
    receipt["modes"]["core"][fact] = 1
    with pytest.raises(ValueError):
        worker_modes.validate_receipt(receipt)


def test_stop_never_signals_reaped_child(monkeypatch):
    process = Mock(returncode=130)
    process.poll.return_value = 130
    worker_modes.stop(process, 0)
    process.send_signal.assert_not_called()
    process.kill.assert_not_called()


def test_forced_cleanup_cannot_be_success(monkeypatch):
    monkeypatch.setattr(worker_modes.time, "monotonic", lambda: 50)
    process = Mock(returncode=-9)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("worker", 10), -9]
    with pytest.raises(RuntimeError, match="forced cleanup"):
        worker_modes.stop(process, 60)
    process.send_signal.assert_called_once_with(signal.SIGINT)
    process.kill.assert_called_once()
    assert process.wait.call_args_list[-1].kwargs == {"timeout": 5}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "corruption",
    [None, "sentinel", "module", "generation", "attempt", "result", "budget", "launch-budget"],
)
def test_observer_preserves_history_and_cleans_owned_rows_on_refusal(monkeypatch, corruption):
    """A simulated child tests the observer, not native worker qualification."""
    import django_ray
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskWorkerLease

    historical = RayTaskExecution.objects.create(
        task_id="preserved-history",
        callable_path="removed.callable",
        state="SUCCEEDED",
        execution_generation=1,
    )
    old_lease = TaskWorkerLease.objects.create(
        worker_id="preserved-worker", hostname="old", pid=999, is_active=False
    )
    historical_before = RayTaskExecution.objects.filter(pk=historical.pk).values().get()
    lease_before = TaskWorkerLease.objects.filter(pk=old_lease.pk).values().get()
    counter = iter(range(1, 10))
    clock = [0.0]
    monkeypatch.setattr(worker_modes.time, "monotonic", lambda: clock[0])

    class Task:
        def using(self, *, queue_name):
            def enqueue():
                row = RayTaskExecution.objects.create(
                    task_id=f"owned-{next(counter)}",
                    callable_path="qualification.upgrade.mode_tasks.mode_value",
                    queue_name=queue_name,
                )
                if corruption == "launch-budget":
                    clock[0] = 50
                return SimpleNamespace(id=row.task_id)

            return SimpleNamespace(enqueue=enqueue)

    monkeypatch.setitem(
        sys.modules, "qualification.upgrade.mode_tasks", SimpleNamespace(mode_value=Task())
    )
    processes = []

    class Process:
        returncode = None

        def __init__(self, args, **kwargs):
            assert kwargs == {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            assert all(item.returncode == 130 for item in processes)
            self.mode = "sync" if "--sync" in args else "core"
            self.pid = 100 + len(processes)
            self.worker = f"owned-worker-{self.mode}"
            processes.append(self)
            row = RayTaskExecution.objects.get(queue_name=f"qualification-{self.mode}")
            result = {
                "value": 42,
                "pid": self.pid if self.mode == "sync" else 900,
                "module": str(Path(django_ray.__file__).resolve()),
                "ray_initialized": self.mode == "core",
            }
            if corruption == "module":
                result["module"] = "wrong"
            row.state = "SUCCEEDED"
            row.execution_generation = 2 if corruption == "generation" else 1
            row.claimed_by_worker = self.worker
            row.result_data = "x" * 4097 if corruption == "result" else json.dumps(result)
            row.save()
            TaskAttempt.objects.create(execution=row, attempt_number=1, state="SUCCEEDED")
            if corruption == "attempt":
                TaskAttempt.objects.create(execution=row, attempt_number=2, state="FAILED")
            TaskWorkerLease.objects.create(
                worker_id=self.worker,
                hostname="owned",
                pid=self.pid,
                queue_name=f"qualification-{self.mode}",
            )
            if corruption == "sentinel":
                RayTaskExecution.objects.filter(queue_name="qualification-unselected").update(
                    error_message="changed"
                )

        def poll(self):
            return self.returncode

        def send_signal(self, value):
            assert value == signal.SIGINT
            TaskWorkerLease.objects.filter(pk=self.worker).update(is_active=False)
            self.returncode = 130

        def wait(self, *, timeout):
            assert 0 < timeout <= 10
            if corruption == "budget":
                clock[0] = 50
            return self.returncode

    monkeypatch.setattr(worker_modes.subprocess, "Popen", Process)

    def snapshot():
        return list(RayTaskExecution.objects.order_by("pk").values())

    if corruption is None:
        receipt = worker_modes.run(snapshot)
        worker_modes.validate_receipt(receipt)
        assert len(processes) == 2
    else:
        with pytest.raises((AssertionError, ValueError, TimeoutError)):
            worker_modes.run(snapshot)
        assert len(processes) == (0 if corruption == "launch-budget" else 1)
    assert RayTaskExecution.objects.count() == 1
    assert RayTaskExecution.objects.filter(pk=historical.pk).values().get() == historical_before
    assert TaskWorkerLease.objects.count() == 1
    assert TaskWorkerLease.objects.filter(pk=old_lease.pk).values().get() == lease_before
    assert not TaskAttempt.objects.exists()
