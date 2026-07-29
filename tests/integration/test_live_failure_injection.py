"""Live fault-injection tests against a real Ray cluster.

These tests are intentionally opt-in and skipped unless
DJANGO_RAY_LIVE_CLUSTER_TESTS is enabled.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from io import StringIO

import pytest

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner


def _truthy(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes"}


LIVE_CLUSTER_ENABLED = _truthy(os.environ.get("DJANGO_RAY_LIVE_CLUSTER_TESTS"))
LIVE_RAY_ADDRESS = os.environ.get("DJANGO_RAY_LIVE_RAY_ADDRESS") or os.environ.get(
    "RAY_ADDRESS", "auto"
)
LIVE_MIN_NODES = int(os.environ.get("DJANGO_RAY_LIVE_MIN_NODES", "2"))
LIVE_WORKING_DIR_URI = os.environ.get("DJANGO_RAY_LIVE_WORKING_DIR_URI")

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.live_cluster,
]
if not LIVE_CLUSTER_ENABLED:
    pytestmark.append(
        pytest.mark.skip(
            reason=("live cluster tests disabled; set DJANGO_RAY_LIVE_CLUSTER_TESTS=1 to enable")
        )
    )


@pytest.fixture()
def live_ray_cluster():
    """Connect to a live Ray cluster and ensure minimum node count."""
    import ray

    if ray.is_initialized():
        raise RuntimeError("Required live Ray fixture found an initialized driver")

    try:
        ray.init(address=LIVE_RAY_ADDRESS)
    except Exception as exc:  # pragma: no cover - environment-dependent
        ray.shutdown()
        raise RuntimeError(f"Required live Ray connection failed at {LIVE_RAY_ADDRESS}") from exc

    try:
        alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
        if len(alive_nodes) < LIVE_MIN_NODES:
            raise RuntimeError(
                "Required live Ray cluster has "
                f"{len(alive_nodes)} alive node(s); requires at least {LIVE_MIN_NODES}"
            )
        yield ray
    finally:
        ray.shutdown()


def _make_live_command(worker_id: str = "live-failure-worker") -> Command:
    cmd = Command()
    cmd.stdout = StringIO()
    cmd.style = cmd.style
    cmd.worker_id = worker_id
    cmd.execution_mode = "local" if LIVE_RAY_ADDRESS == "auto" else "cluster"
    cmd.cluster_address = None if LIVE_RAY_ADDRESS == "auto" else LIVE_RAY_ADDRESS
    cmd.sync_mode = False
    cmd.active_tasks = {}
    cmd.ray_core_runner = RayCoreRunner()
    return cmd


def _submit_live_sleep_task(ray_module, sleep_seconds: int):
    """Submit a long-running task directly to Ray for live fault tests."""

    @ray_module.remote(name=f"django_ray_live_sleep_{time.time_ns()}")
    def _live_sleep(seconds: int) -> str:
        import time as _time

        _time.sleep(seconds)
        return json.dumps(
            {
                "success": True,
                "result": f"slept-{seconds}",
                "error": None,
                "traceback": None,
                "exception_type": None,
            }
        )

    return _live_sleep.remote(sleep_seconds)


class TestLiveFailureInjection:
    """Live cluster fault-injection scenarios."""

    @pytest.mark.skipif(
        not LIVE_WORKING_DIR_URI,
        reason="DJANGO_RAY_LIVE_WORKING_DIR_URI is required for the submission smoke test",
    )
    def test_ray_core_runner_submits_project_code_to_generic_cluster(self, live_ray_cluster):
        """The package-free Ray Client head must accept django-ray's bootstrap."""
        from django_ray.runtime.runtime_env import normalize_runtime_env

        assert LIVE_WORKING_DIR_URI is not None
        runtime_env = normalize_runtime_env(
            {
                "working_dir": LIVE_WORKING_DIR_URI,
                "pip": ["django>=6.0"],
                "env_vars": {
                    "DATABASE_ENGINE": "django.db.backends.sqlite3",
                    "DJANGO_SETTINGS_MODULE": "testproject.settings",
                    "PYTHONPATH": "src",
                },
            },
            profile="live-project",
        )
        task = RayTaskExecution.objects.create(
            task_id=f"live-ray-core-submit-{time.time_ns()}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[2, 3]",
            kwargs_json="{}",
            runtime_env_profile=runtime_env.profile,
            runtime_env_json=runtime_env.serialized,
            runtime_env_hash=runtime_env.digest,
        )

        runner = RayCoreRunner()
        runner.submit(
            task_execution=task,
            callable_path=task.callable_path,
            args=(2, 3),
            kwargs={},
        )
        payload = live_ray_cluster.get(
            runner._pending_tasks[task.pk].object_ref,
            timeout=120,
        )

        assert json.loads(payload) == {
            "success": True,
            "result": 5,
            "result_reference": None,
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        }

    def test_disconnect_retries_pending_ray_core_task(self, live_ray_cluster):
        """Client disconnect should trigger retry path for tracked pending tasks."""
        task = RayTaskExecution.objects.create(
            task_id="live-fi-disconnect-001",
            callable_path="time.sleep",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[30]",
            kwargs_json="{}",
            attempt_number=1,
            claimed_by_worker="live-failure-worker",
        )

        cmd = _make_live_command()
        object_ref = _submit_live_sleep_task(live_ray_cluster, sleep_seconds=30)
        cmd.ray_core_runner._pending_tasks[task.pk] = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object_ref,
            submitted_at=datetime.now(UTC),
            task_name="live_sleep",
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )

        live_ray_cluster.shutdown()
        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert "Ray connection lost" in (task.error_message or "")
        assert cmd.ray_core_runner._pending_tasks == {}

    def test_cancellation_finalizes_live_pending_task(self, live_ray_cluster):
        """Cancelling a live pending Ray Core task should finalize CANCELLED state."""
        task = RayTaskExecution.objects.create(
            task_id="live-fi-cancel-001",
            callable_path="time.sleep",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[30]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC),
            claimed_by_worker="live-failure-worker",
        )

        cmd = _make_live_command()
        object_ref = _submit_live_sleep_task(live_ray_cluster, sleep_seconds=30)
        cmd.ray_core_runner._pending_tasks[task.pk] = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object_ref,
            submitted_at=datetime.now(UTC),
            task_name="live_sleep",
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )
        cmd.active_tasks[task.pk] = f"ray_core:{task.pk}"

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.ray_core_runner._pending_tasks
