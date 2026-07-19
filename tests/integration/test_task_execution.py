"""Integration tests for django-ray task execution.

These tests require a running Ray cluster and execute actual tasks.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest
import ray

pytestmark = pytest.mark.real_ray

# Get project root
PROJECT_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture(scope="module")
def ray_cluster():
    """Start a local Ray cluster for testing."""
    if not ray.is_initialized():
        try:
            ray.init(ignore_reinit_error=True, include_dashboard=True)
        except Exception as e:
            pytest.skip(f"Local Ray startup failed for integration test fixture: {e}")
    yield
    # Shutdown Ray to avoid polluting other tests
    if ray.is_initialized():
        ray.shutdown()


@pytest.fixture
def django_settings_env():
    """Set up Django settings environment variable."""
    old_value = os.environ.get("DJANGO_SETTINGS_MODULE")
    os.environ["DJANGO_SETTINGS_MODULE"] = "testproject.settings"

    # Ensure paths are set up
    src_path = str(PROJECT_ROOT / "src")
    root_path = str(PROJECT_ROOT)

    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    if root_path not in sys.path:
        sys.path.insert(0, root_path)

    yield

    if old_value:
        os.environ["DJANGO_SETTINGS_MODULE"] = old_value
    else:
        os.environ.pop("DJANGO_SETTINGS_MODULE", None)


class TestEntrypointExecution:
    """Test the task entrypoint execution directly."""

    def test_execute_simple_task(self, django_settings_env):
        """Test executing a simple task through the entrypoint."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.add_numbers",
            serialized_args="[2, 3]",
            serialized_kwargs="{}",
        )

        result = json.loads(result_json)
        assert result["success"] is True
        assert result["result"] == 5
        assert result["error"] is None

    def test_execute_task_with_kwargs(self, django_settings_env):
        """Test executing a task with keyword arguments."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.echo_task",
            serialized_args='["hello"]',
            serialized_kwargs='{"key": "value"}',
        )

        result = json.loads(result_json)
        assert result["success"] is True
        assert result["result"]["args"] == ["hello"]
        assert result["result"]["kwargs"] == {"key": "value"}

    def test_execute_async_task_value_and_event_loop_cleanup(self, django_settings_env):
        """The entrypoint awaits a coroutine and closes its per-task event loop."""
        from django_ray.runtime.entrypoint import execute_task

        result = json.loads(
            execute_task(
                callable_path="testproject.tasks.async_add_numbers",
                serialized_args="[20, 22]",
                serialized_kwargs="{}",
            )
        )

        assert result["success"] is True
        assert result["result"] == 42
        assert result["exception_type"] is None
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    def test_execute_async_task_preserves_underlying_exception(self, django_settings_env):
        """Retry classification sees the coroutine's exception rather than a wrapper."""
        from django_ray.runtime.entrypoint import execute_task

        result = json.loads(
            execute_task(
                callable_path="testproject.tasks.async_failing_task",
                serialized_args="[]",
                serialized_kwargs="{}",
            )
        )

        assert result["success"] is False
        assert result["error"] == "Async task requested a retryable failure"
        assert result["exception_type"] == "builtins.ValueError"
        assert result["retryable"] is True

    def test_async_parent_cancellation_cleans_up_pending_child(
        self,
        django_settings_env,
        monkeypatch,
    ):
        """Closing the cancelled task loop awaits pending-child cleanup."""
        from django_ray.runtime import import_utils
        from django_ray.runtime.entrypoint import execute_task

        events: list[str] = []

        async def pending_child() -> None:
            events.append("child-started")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("child-cleaned")

        async def cancelled_parent() -> None:
            asyncio.create_task(pending_child())
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        monkeypatch.setattr(import_utils, "import_callable", lambda _path: cancelled_parent)

        with pytest.raises(asyncio.CancelledError):
            execute_task("testproject.tasks.cancelled_parent", "[]", "{}")

        assert events == ["child-started", "child-cleaned"]
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    @pytest.mark.django_db(transaction=True)
    def test_execute_async_task_preserves_context_and_supports_async_orm(
        self,
        django_settings_env,
    ):
        """ContextVars cross awaits and Django's async ORM can read durable state."""
        from django_ray.models import RayTaskExecution, TaskState
        from django_ray.runtime.entrypoint import execute_task

        execution = RayTaskExecution.objects.create(
            task_id="async-context-entrypoint-001",
            callable_path="testproject.tasks.async_context_probe",
            state=TaskState.RUNNING,
        )

        result = json.loads(
            execute_task(
                callable_path=execution.callable_path,
                serialized_args='["entrypoint"]',
                serialized_kwargs='{"load_execution": true}',
                task_execution_pk=execution.pk,
                ray_job_driver=False,
            )
        )

        assert result["success"] is True
        assert result["result"] == {
            "value": "entrypoint",
            "execution_id_before": execution.pk,
            "execution_id_after": execution.pk,
            "ray_job_driver_before": False,
            "ray_job_driver_after": False,
            "task_id": execution.task_id,
            "active_task_count": 1,
            "loop_running": True,
        }

    def test_execute_failing_task(self, django_settings_env):
        """Test that failing tasks return error information."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.failing_task",
            serialized_args="[]",
            serialized_kwargs="{}",
        )

        result = json.loads(result_json)
        assert result["success"] is False
        assert "This task is designed to fail" in result["error"]
        assert result["traceback"] is not None

    def test_execute_nonexistent_task(self, django_settings_env):
        """Test executing a task that doesn't exist."""
        from django_ray.runtime.entrypoint import execute_task

        result_json = execute_task(
            callable_path="testproject.tasks.nonexistent_task",
            serialized_args="[]",
            serialized_kwargs="{}",
        )

        result = json.loads(result_json)
        assert result["success"] is False
        assert "nonexistent_task" in result["error"]


class TestRayRemoteExecution:
    """Test executing tasks as Ray remote functions."""

    def test_ray_remote_task(self, django_settings_env, ray_cluster):
        """Test running a task via Ray remote."""

        @ray.remote
        def remote_add(a: int, b: int) -> int:
            # Setup Django before importing tasks with @task decorator
            import os

            import django

            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
            django.setup()

            # Import inside the remote function
            from testproject.tasks import add_numbers

            # add_numbers is a Django Task object, use .call() to execute
            return add_numbers.call(a, b)

        result = ray.get(remote_add.remote(10, 20))
        assert result == 30

    def test_ray_remote_entrypoint(self, django_settings_env, ray_cluster):
        """Test running the entrypoint via Ray remote."""

        @ray.remote
        def remote_execute(callable_path: str, args: str, kwargs: str) -> str:
            import os
            import sys

            # Set up environment
            os.environ["DJANGO_SETTINGS_MODULE"] = "testproject.settings"

            # Add paths
            project_root = Path(__file__).parent.parent.parent
            src_path = str(project_root / "src")
            root_path = str(project_root)

            if src_path not in sys.path:
                sys.path.insert(0, src_path)
            if root_path not in sys.path:
                sys.path.insert(0, root_path)

            from django_ray.runtime.entrypoint import execute_task

            return execute_task(callable_path, args, kwargs)

        result_json = ray.get(
            remote_execute.remote(
                "testproject.tasks.multiply_numbers",
                "[7, 6]",
                "{}",
            )
        )

        result = json.loads(result_json)
        assert result["success"] is True
        assert result["result"] == 42

    def test_ray_core_runs_async_task_through_package_entrypoint(
        self,
        django_settings_env,
        ray_cluster,
    ):
        """The package's real Ray Core wrapper awaits async tasks and preserves context."""
        from django_ray.runtime.remote import execute_django_task_remote

        remote_entrypoint = ray.remote(execute_django_task_remote)
        result = json.loads(
            ray.get(
                remote_entrypoint.remote(
                    "testproject.tasks.async_context_probe",
                    '["ray-core"]',
                    "{}",
                    4242,
                )
            )
        )

        assert result["success"] is True
        assert result["result"] == {
            "value": "ray-core",
            "execution_id_before": 4242,
            "execution_id_after": 4242,
            "ray_job_driver_before": False,
            "ray_job_driver_after": False,
            "task_id": None,
            "active_task_count": 1,
            "loop_running": True,
        }

        failure = json.loads(
            ray.get(
                remote_entrypoint.remote(
                    "testproject.tasks.async_failing_task",
                    "[]",
                    "{}",
                    4243,
                )
            )
        )
        assert failure["success"] is False
        assert failure["error"] == "Async task requested a retryable failure"
        assert failure["exception_type"] == "builtins.ValueError"

    def test_ray_job_runs_async_task_through_cli_entrypoint(
        self,
        django_settings_env,
        ray_cluster,
    ):
        """A local Ray Job driver completes an encoded async-task payload."""
        from ray.job_submission import JobSubmissionClient

        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "callable_path": "testproject.tasks.async_add_numbers",
                    "serialized_args": "[20, 22]",
                    "serialized_kwargs": "{}",
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        python_path = os.pathsep.join(
            path
            for path in (
                str(PROJECT_ROOT / "src"),
                str(PROJECT_ROOT),
                os.environ.get("PYTHONPATH", ""),
            )
            if path
        )
        client = JobSubmissionClient("http://127.0.0.1:8265")
        job_id = client.submit_job(
            entrypoint=(f"python -m django_ray.runtime.entrypoint --payload-b64 {payload}"),
            runtime_env={
                "env_vars": {
                    "DJANGO_SETTINGS_MODULE": "testproject.settings",
                    "PYTHONPATH": python_path,
                }
            },
        )
        deadline = time.monotonic() + 30
        status = "PENDING"
        try:
            while time.monotonic() < deadline:
                observed = client.get_job_status(job_id)
                status = str(getattr(observed, "value", observed))
                if status in {"SUCCEEDED", "FAILED", "STOPPED"}:
                    break
                time.sleep(0.2)
            logs = client.get_job_logs(job_id)
        finally:
            if status not in {"SUCCEEDED", "FAILED", "STOPPED"}:
                client.stop_job(job_id)

        assert status == "SUCCEEDED", logs
        assert "django-ray task completed successfully" in logs
        assert "was never awaited" not in logs


@pytest.mark.django_db
class TestModelIntegration:
    """Test task execution with Django models."""

    @pytest.fixture
    def task_execution(self, django_settings_env):
        """Create a RayTaskExecution model instance."""
        import django
        from django.apps import apps

        if not apps.ready:
            django.setup()

        from django_ray.models import RayTaskExecution, TaskState

        task = RayTaskExecution.objects.create(
            task_id="test-task-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
        )
        yield task
        task.delete()

    def test_create_task_execution(self, task_execution):
        """Test creating a task execution record."""
        from django_ray.models import TaskState

        assert task_execution.pk is not None
        assert task_execution.state == TaskState.QUEUED
        assert task_execution.attempt_number == 1

    def test_update_task_state(self, task_execution):
        """Test updating task state."""
        from django_ray.models import TaskState

        task_execution.state = TaskState.RUNNING
        task_execution.save()
        task_execution.refresh_from_db()

        assert task_execution.state == TaskState.RUNNING
