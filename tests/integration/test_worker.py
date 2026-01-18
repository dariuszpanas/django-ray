"""Integration tests for the django-ray worker."""

from __future__ import annotations

import os
import sys
from io import StringIO
from pathlib import Path

import pytest

from django_ray.models import RayTaskExecution, TaskState


@pytest.fixture(autouse=True)
def setup_django_env():
    """Ensure Django settings are available for entrypoint."""
    # Set the environment variable
    os.environ["DJANGO_SETTINGS_MODULE"] = "testproject.settings"

    # Ensure paths are set up
    project_root = Path(__file__).parent.parent.parent
    src_path = str(project_root / "src")
    root_path = str(project_root)

    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    if root_path not in sys.path:
        sys.path.insert(0, root_path)

    yield


@pytest.mark.django_db
class TestWorkerSync:
    """Test the worker in synchronous mode."""

    def test_worker_processes_simple_task(self, setup_django_env):
        """Test that the worker processes a simple task correctly."""
        # Create a task
        task = RayTaskExecution.objects.create(
            task_id="test-worker-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[5, 3]",
            kwargs_json="{}",
        )

        # Run worker for one iteration (we'll simulate by calling the methods directly)
        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style  # Use default style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        # Process the task
        cmd.claim_and_process_tasks(queue="default", concurrency=10)

        # Verify task was processed
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "8"
        assert task.error_message is None
        assert task.finished_at is not None
        assert task.claimed_by_worker == "test-worker"

    def test_worker_processes_failing_task(self, setup_django_env):
        """Test that the worker handles failing tasks correctly."""
        # Create a failing task
        task = RayTaskExecution.objects.create(
            task_id="test-worker-002",
            callable_path="testproject.tasks.failing_task",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
            attempt_number=3,  # Start at max attempts so it fails permanently
        )

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        # Process the task
        cmd.claim_and_process_tasks(queue="default", concurrency=10)

        # Verify task failed permanently (no more retries)
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert "This task is designed to fail" in task.error_message
        assert task.error_traceback is not None
        assert task.finished_at is not None

    def test_worker_retries_failing_task(self, setup_django_env):
        """Test that the worker schedules retry for a failing task."""
        task = RayTaskExecution.objects.create(
            task_id="test-retry-001",
            callable_path="testproject.tasks.failing_task",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
            attempt_number=1,  # First attempt
        )

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        # Process the task
        cmd.claim_and_process_tasks(queue="default", concurrency=10)

        # Verify task is queued for retry
        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2  # Incremented
        assert task.run_after is not None  # Scheduled for future
        assert "This task is designed to fail" in task.error_message

    def test_worker_detects_timed_out_task(self, setup_django_env):
        """Test that the worker detects and fails timed-out tasks."""
        from datetime import datetime, timedelta, timezone

        # Create a task that started 10 seconds ago with 5 second timeout
        started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        task = RayTaskExecution.objects.create(
            task_id="test-timeout-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 60}',
            timeout_seconds=5,  # 5 second timeout
            started_at=started_at,
            claimed_by_worker="test-worker",
        )

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}
        cmd.local_ray_tasks = {}
        cmd.last_reconciliation = 0

        # Run stuck task detection (which also checks timeouts)
        cmd.detect_stuck_tasks()

        # Verify task is marked as FAILED due to timeout
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert "timed out" in task.error_message.lower()

    def test_worker_respects_queue_filter(self, setup_django_env):
        """Test that the worker only processes tasks from the specified queue."""
        # Create tasks in different queues
        task_default = RayTaskExecution.objects.create(
            task_id="test-queue-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 1]",
            kwargs_json="{}",
        )
        task_other = RayTaskExecution.objects.create(
            task_id="test-queue-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="other",
            state=TaskState.QUEUED,
            args_json="[2, 2]",
            kwargs_json="{}",
        )

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        # Process only "other" queue
        cmd.claim_and_process_tasks(queue="other", concurrency=10)

        # Verify only the "other" task was processed
        task_default.refresh_from_db()
        task_other.refresh_from_db()

        assert task_default.state == TaskState.QUEUED  # Not processed
        assert task_other.state == TaskState.SUCCEEDED  # Processed
        assert task_other.result_data == "4"

    def test_worker_respects_concurrency_limit(self, setup_django_env):
        """Test that the worker respects concurrency limits."""
        # Create multiple tasks
        tasks = []
        for i in range(5):
            task = RayTaskExecution.objects.create(
                task_id=f"test-concurrency-{i}",
                callable_path="testproject.tasks.add_numbers",
                queue_name="default",
                state=TaskState.QUEUED,
                args_json=f"[{i}, {i}]",
                kwargs_json="{}",
            )
            tasks.append(task)

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        # Process with concurrency of 2
        cmd.claim_and_process_tasks(queue="default", concurrency=2)

        # Count processed tasks
        processed = 0
        for task in tasks:
            task.refresh_from_db()
            if task.state == TaskState.SUCCEEDED:
                processed += 1

        # Should have processed exactly 2 tasks
        assert processed == 2

    def test_worker_handles_task_with_kwargs(self, setup_django_env):
        """Test that the worker correctly passes kwargs to tasks."""
        task = RayTaskExecution.objects.create(
            task_id="test-kwargs-001",
            callable_path="testproject.tasks.echo_task",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json='["hello"]',
            kwargs_json='{"key": "value", "number": 42}',
        )

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        cmd.claim_and_process_tasks(queue="default", concurrency=10)

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED

        import json

        result = json.loads(task.result_data)
        assert result["args"] == ["hello"]
        assert result["kwargs"] == {"key": "value", "number": 42}
