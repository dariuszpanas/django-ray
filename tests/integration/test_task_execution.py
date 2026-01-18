"""Integration tests for django-ray task execution.

These tests require a running Ray cluster and execute actual tasks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import ray

# Get project root
PROJECT_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture(scope="module")
def ray_cluster():
    """Start a local Ray cluster for testing."""
    if not ray.is_initialized():
        ray.init(ignore_reinit_error=True)
    yield
    # Don't shutdown - let other tests use it


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

    def test_execute_simple_task(self, django_settings_env, ray_cluster):
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

    def test_execute_task_with_kwargs(self, django_settings_env, ray_cluster):
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

    def test_execute_failing_task(self, django_settings_env, ray_cluster):
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

    def test_execute_nonexistent_task(self, django_settings_env, ray_cluster):
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


@pytest.mark.django_db
class TestModelIntegration:
    """Test task execution with Django models."""

    @pytest.fixture
    def task_execution(self, django_settings_env):
        """Create a RayTaskExecution model instance."""
        import django

        if not django.apps.apps.ready:
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
