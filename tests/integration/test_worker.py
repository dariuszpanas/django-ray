"""Integration tests for the django-ray worker."""

from __future__ import annotations

import os
import sys
from datetime import UTC
from io import StringIO
from pathlib import Path

import pytest

from django_ray.models import CancellationStatus, RayTaskExecution, TaskState, TaskWorkerLease


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
        cmd.claim_and_process_tasks(queues=["default"], concurrency=10)

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
        cmd.claim_and_process_tasks(queues=["default"], concurrency=10)

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
        cmd.claim_and_process_tasks(queues=["default"], concurrency=10)

        # Verify task is queued for retry
        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2  # Incremented
        assert task.run_after is not None  # Scheduled for future
        assert "This task is designed to fail" in task.error_message

    def test_worker_detects_timed_out_task(self, setup_django_env):
        """Test that the worker detects and fails timed-out tasks."""
        from datetime import datetime, timedelta

        # Create a task that started 10 seconds ago with 5 second timeout
        started_at = datetime.now(UTC) - timedelta(seconds=10)
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
        cmd.claim_and_process_tasks(queues=["other"], concurrency=10)

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
        cmd.claim_and_process_tasks(queues=["default"], concurrency=2)

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

        cmd.claim_and_process_tasks(queues=["default"], concurrency=10)

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED

        import json

        result = json.loads(task.result_data)
        assert result["args"] == ["hello"]
        assert result["kwargs"] == {"key": "value", "number": 42}

    def test_worker_processes_multiple_queues(self, setup_django_env):
        """Test that the worker processes tasks from multiple queues."""
        # Create tasks in different queues
        task_default = RayTaskExecution.objects.create(
            task_id="test-multi-queue-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 1]",
            kwargs_json="{}",
        )
        task_high = RayTaskExecution.objects.create(
            task_id="test-multi-queue-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="high-priority",
            state=TaskState.QUEUED,
            args_json="[2, 2]",
            kwargs_json="{}",
        )
        task_other = RayTaskExecution.objects.create(
            task_id="test-multi-queue-003",
            callable_path="testproject.tasks.add_numbers",
            queue_name="other",
            state=TaskState.QUEUED,
            args_json="[3, 3]",
            kwargs_json="{}",
        )

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        # Process both "default" and "high-priority" queues, but not "other"
        cmd.claim_and_process_tasks(queues=["default", "high-priority"], concurrency=10)

        # Verify tasks from both queues were processed
        task_default.refresh_from_db()
        task_high.refresh_from_db()
        task_other.refresh_from_db()

        assert task_default.state == TaskState.SUCCEEDED
        assert task_default.result_data == "2"
        assert task_high.state == TaskState.SUCCEEDED
        assert task_high.result_data == "4"
        assert task_other.state == TaskState.QUEUED  # Not processed

    def test_worker_processes_high_priority_first(self) -> None:
        """Test that high-priority tasks are processed before default and low-priority."""
        # Create tasks in order: low-priority first, then default, then high-priority
        # They should be processed in reverse order based on priority
        task_low = RayTaskExecution.objects.create(
            task_id="test-priority-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="low-priority",
            state=TaskState.QUEUED,
            args_json="[1, 1]",
            kwargs_json="{}",
        )
        task_default = RayTaskExecution.objects.create(
            task_id="test-priority-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[2, 2]",
            kwargs_json="{}",
        )
        task_high = RayTaskExecution.objects.create(
            task_id="test-priority-003",
            callable_path="testproject.tasks.add_numbers",
            queue_name="high-priority",
            state=TaskState.QUEUED,
            args_json="[3, 3]",
            kwargs_json="{}",
        )

        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "test-worker"
        cmd.active_tasks = {}

        # Process only 1 task at a time to verify order
        cmd.claim_and_process_tasks(
            queues=["default", "high-priority", "low-priority"], concurrency=1
        )

        # High-priority should be processed first
        task_high.refresh_from_db()
        task_default.refresh_from_db()
        task_low.refresh_from_db()

        assert task_high.state == TaskState.SUCCEEDED, "High-priority should be first"
        assert task_default.state == TaskState.QUEUED, "Default should still be queued"
        assert task_low.state == TaskState.QUEUED, "Low-priority should still be queued"

        # Process next task
        cmd.claim_and_process_tasks(
            queues=["default", "high-priority", "low-priority"], concurrency=1
        )

        task_default.refresh_from_db()
        task_low.refresh_from_db()

        assert task_default.state == TaskState.SUCCEEDED, "Default should be second"
        assert task_low.state == TaskState.QUEUED, "Low-priority should still be queued"

        # Process last task
        cmd.claim_and_process_tasks(
            queues=["default", "high-priority", "low-priority"], concurrency=1
        )

        task_low.refresh_from_db()
        assert task_low.state == TaskState.SUCCEEDED, "Low-priority should be last"


@pytest.mark.django_db
class TestWorkerRayJobFailureHandling:
    """Test Ray Job mode failure paths use unified retry handling."""

    @staticmethod
    def _make_command():
        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.worker_id = "test-worker"
        cmd.sync_mode = False
        cmd.active_tasks = {}
        return cmd

    def test_submit_task_to_ray_retries_on_submission_error(self, monkeypatch):
        """Submission errors should go through retry policy."""
        task = RayTaskExecution.objects.create(
            task_id="test-ray-submit-retry-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
        )

        class FailingRunner:
            def submit(self, **kwargs):
                raise RuntimeError("submission exploded")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FailingRunner)

        cmd = self._make_command()
        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert "Failed to submit to Ray: submission exploded" in task.error_message
        assert task.finished_at is None
        assert task.pk not in cmd.active_tasks

    def test_reconcile_failed_job_retries_when_attempts_remain(self, monkeypatch):
        """Ray FAILED status should trigger retry path."""
        task = RayTaskExecution.objects.create(
            task_id="test-ray-reconcile-retry-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[3, 4]",
            kwargs_json="{}",
            attempt_number=1,
            ray_job_id="raysubmit_retry_001",
            ray_address="ray://cluster:10001",
        )

        class FakeRunner:
            def get_status(self, handle):
                from django_ray.runner.base import JobInfo, JobStatus

                return JobInfo(
                    job_id=handle.ray_job_id, status=JobStatus.FAILED, message="ray failed"
                )

            def get_logs(self, handle):
                return "traceback-log-content"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd = self._make_command()
        cmd.active_tasks = {task.pk: "raysubmit_retry_001"}
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.error_message == "ray failed"
        assert task.error_traceback == "traceback-log-content"
        assert task.pk not in cmd.active_tasks

    def test_reconcile_succeeded_job_with_failure_payload_retries(self, monkeypatch):
        """A SUCCEEDED Ray job with success=false payload should use retry logic."""
        task = RayTaskExecution.objects.create(
            task_id="test-ray-reconcile-retry-002",
            callable_path="testproject.tasks.failing_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
            attempt_number=1,
            ray_job_id="raysubmit_retry_002",
            ray_address="ray://cluster:10001",
            completion_data=(
                '{"success": false, "result": null, "error": "task failed", '
                '"traceback": "tb", "exception_type": "builtins.ValueError"}'
            ),
        )

        class FakeRunner:
            def get_status(self, handle):
                from django_ray.runner.base import JobInfo, JobStatus

                return JobInfo(
                    job_id=handle.ray_job_id,
                    status=JobStatus.SUCCEEDED,
                    message="completed",
                )

            def get_logs(self, handle):
                return "arbitrary application stdout"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd = self._make_command()
        cmd.active_tasks = {task.pk: "raysubmit_retry_002"}
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.error_message == "task failed"
        assert task.error_traceback == "tb"
        assert task.pk not in cmd.active_tasks

    def test_reconcile_failed_job_marks_failed_at_max_attempts(self, monkeypatch):
        """Ray FAILED status should become terminal when max attempts is reached."""
        task = RayTaskExecution.objects.create(
            task_id="test-ray-reconcile-failed-001",
            callable_path="testproject.tasks.failing_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
            attempt_number=3,  # MAX_TASK_ATTEMPTS in test settings
            ray_job_id="raysubmit_failed_001",
            ray_address="ray://cluster:10001",
        )

        class FakeRunner:
            def get_status(self, handle):
                from django_ray.runner.base import JobInfo, JobStatus

                return JobInfo(
                    job_id=handle.ray_job_id, status=JobStatus.FAILED, message="ray final"
                )

            def get_logs(self, handle):
                return "terminal-traceback"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd = self._make_command()
        cmd.active_tasks = {task.pk: "raysubmit_failed_001"}
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 3
        assert task.finished_at is not None
        assert task.error_message == "ray final"
        assert task.error_traceback == "terminal-traceback"
        assert task.pk not in cmd.active_tasks


@pytest.mark.django_db
class TestWorkerOrphanRecovery:
    """Test recovery of tasks owned by expired/missing workers."""

    @staticmethod
    def _make_command(worker_id: str = "recovery-worker"):
        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = worker_id
        cmd.active_tasks = {}
        cmd.ray_core_runner = None
        return cmd

    def test_recovers_stuck_task_from_expired_worker_lease(self):
        """A task from an expired worker should be recovered by another worker."""
        from datetime import datetime, timedelta

        TaskWorkerLease.objects.create(
            worker_id="dead-worker",
            hostname="host-a",
            pid=1111,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
            is_active=True,
        )

        task = RayTaskExecution.objects.create(
            task_id="test-orphan-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            claimed_by_worker="dead-worker",
        )

        cmd = self._make_command()
        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.claimed_by_worker is None
        assert task.started_at is None
        assert task.finished_at is None

    def test_recovers_stuck_task_from_missing_worker_lease(self):
        """A task with no corresponding lease should also be recovered."""
        from datetime import datetime, timedelta

        task = RayTaskExecution.objects.create(
            task_id="test-orphan-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[10, 20]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            claimed_by_worker="missing-worker",
        )

        cmd = self._make_command()
        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.claimed_by_worker is None

    def test_does_not_recover_task_from_active_other_worker(self):
        """Tasks owned by healthy workers should not be recovered by this worker."""
        from datetime import datetime, timedelta

        TaskWorkerLease.objects.create(
            worker_id="live-worker",
            hostname="host-b",
            pid=2222,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC),
            is_active=True,
        )

        task = RayTaskExecution.objects.create(
            task_id="test-orphan-003",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[5, 5]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            claimed_by_worker="live-worker",
        )

        cmd = self._make_command()
        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 1
        assert task.claimed_by_worker == "live-worker"

    def test_marks_orphan_timed_out_task_as_failed(self):
        """Timed-out tasks from inactive workers should be failed during recovery."""
        from datetime import datetime, timedelta

        TaskWorkerLease.objects.create(
            worker_id="dead-worker-timeout",
            hostname="host-c",
            pid=3333,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
            is_active=True,
        )

        task = RayTaskExecution.objects.create(
            task_id="test-orphan-004",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 60}',
            attempt_number=1,
            timeout_seconds=5,
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            claimed_by_worker="dead-worker-timeout",
        )

        cmd = self._make_command()
        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert "timed out" in (task.error_message or "").lower()

    def test_timeout_requests_remote_ray_job_stop_before_failure(self, monkeypatch):
        """A timed-out Ray Job is stopped before the timeout is persisted."""
        from datetime import datetime, timedelta

        task = RayTaskExecution.objects.create(
            task_id="test-timeout-ray-job-success-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 60}',
            timeout_seconds=5,
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            claimed_by_worker="recovery-worker",
            ray_job_id="raysubmit_timeout_success_001",
            ray_address="ray://cluster:10001",
        )
        stop_calls: list[str] = []

        class FakeRunner:
            def cancel(self, handle):
                stop_calls.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        self._make_command().detect_stuck_tasks()

        task.refresh_from_db()
        assert stop_calls == ["raysubmit_timeout_success_001"]
        assert task.state == TaskState.FAILED
        assert task.cancellation_status == CancellationStatus.REQUESTED

    def test_timeout_records_remote_ray_job_cancellation_failure(self, monkeypatch):
        """A rejected stop request remains visible on the timed-out task."""
        from datetime import datetime, timedelta

        task = RayTaskExecution.objects.create(
            task_id="test-timeout-ray-job-failure-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 60}',
            timeout_seconds=5,
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            claimed_by_worker="recovery-worker",
            ray_job_id="raysubmit_timeout_failure_001",
        )

        class FakeRunner:
            def cancel(self, _handle):
                return False

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        self._make_command().detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.cancellation_status == CancellationStatus.FAILED
        assert "cancellation failed" in (task.error_message or "").lower()
        assert task.cancellation_error == "Cancellation API rejected the stop request"

    def test_timeout_does_not_overwrite_completion_race(self, monkeypatch):
        """A completion winning during stop is not replaced by timeout failure."""
        from datetime import datetime, timedelta

        task = RayTaskExecution.objects.create(
            task_id="test-timeout-ray-job-race-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 60}',
            timeout_seconds=5,
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            claimed_by_worker="recovery-worker",
            ray_job_id="raysubmit_timeout_race_001",
        )

        class FakeRunner:
            def cancel(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    state=TaskState.SUCCEEDED,
                    finished_at=datetime.now(UTC),
                    completion_data='{"success": true, "result": 42}',
                )
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        self._make_command().detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.cancellation_status is None

    def test_timeout_cancels_ray_core_task_without_ray_job_request(self):
        """Ray Core timeout handling must not call the Ray Job API."""
        from datetime import datetime, timedelta

        task = RayTaskExecution.objects.create(
            task_id="test-timeout-ray-core-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 60}',
            timeout_seconds=5,
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            claimed_by_worker="recovery-worker",
            ray_job_id="02000000:01000000",
        )
        cancelled: list[str] = []

        class FakeCoreRunner:
            _pending_tasks = {task.pk: object()}

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        cmd = self._make_command()
        cmd.ray_core_runner = FakeCoreRunner()
        cmd.active_tasks = {task.pk: "02000000:01000000"}

        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert cancelled == [f"ray_core:{task.pk}"]
        assert task.state == TaskState.FAILED
        assert task.cancellation_status == CancellationStatus.NOT_APPLICABLE
        assert task.pk not in cmd.active_tasks


@pytest.mark.django_db
class TestWorkerResultStorage:
    """Test result size enforcement and reference fallback."""

    @staticmethod
    def _make_command():
        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.execution_mode = "sync"
        cmd.worker_id = "result-worker"
        cmd.active_tasks = {}
        cmd.sync_mode = False
        return cmd

    def test_sync_mode_stores_oversized_result_as_reference(self, monkeypatch):
        """Large sync result should not be persisted inline in result_data."""
        import json

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 64},
        )

        large_text = "x" * 256
        task = RayTaskExecution.objects.create(
            task_id="test-result-001",
            callable_path="testproject.tasks.echo_task",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json=json.dumps([large_text]),
            kwargs_json="{}",
        )

        cmd = self._make_command()
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data is None
        assert task.result_reference is not None
        assert str(task.result_reference).startswith("oversize://sha256/")

    def test_sync_mode_small_result_stays_inline_and_clears_reference(self, monkeypatch):
        """Small result should remain inline and clear any stale reference."""
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 64},
        )

        task = RayTaskExecution.objects.create(
            task_id="test-result-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_reference="oversize://sha256/stale?bytes=999",
        )

        cmd = self._make_command()
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        assert task.result_reference is None

    def test_sync_mode_oversized_result_uses_filesystem_backend(
        self, monkeypatch, tmp_path
    ) -> None:
        """Large results should be persisted externally when filesystem backend is configured."""
        import json

        from django_ray.result_storage import FilesystemResultStorage

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "MAX_RESULT_SIZE_BYTES": 64,
                "RESULT_STORAGE_BACKEND": "filesystem",
                "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
            },
        )

        large_text = "x" * 256
        task = RayTaskExecution.objects.create(
            task_id="test-result-004",
            callable_path="testproject.tasks.echo_task",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json=json.dumps([large_text]),
            kwargs_json="{}",
        )

        cmd = self._make_command()
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data is None
        assert task.result_reference is not None
        assert str(task.result_reference).startswith("resultfs://sha256/")

        stored_payload = FilesystemResultStorage(tmp_path).load(
            reference=str(task.result_reference)
        )
        assert stored_payload is not None
        stored_result = json.loads(stored_payload)
        assert stored_result["args"] == [large_text]

    def test_sync_mode_storage_backend_error_falls_back_to_digest_reference(
        self, monkeypatch
    ) -> None:
        """Storage backend errors should not fail successful task execution."""
        import json

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "MAX_RESULT_SIZE_BYTES": 64,
                "RESULT_STORAGE_BACKEND": "filesystem",
                # Missing RESULT_STORAGE_FILESYSTEM_PATH forces backend resolution failure.
            },
        )

        task = RayTaskExecution.objects.create(
            task_id="test-result-005",
            callable_path="testproject.tasks.echo_task",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json=json.dumps(["x" * 256]),
            kwargs_json="{}",
        )

        cmd = self._make_command()
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data is None
        assert task.result_reference is not None
        assert str(task.result_reference).startswith("oversize://sha256/")

    def test_reconcile_succeeded_job_oversized_result_uses_reference(self, monkeypatch):
        """Ray Job reconcile should also enforce result size limits."""
        import json

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 64},
        )

        task = RayTaskExecution.objects.create(
            task_id="test-result-003",
            callable_path="testproject.tasks.echo_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
            attempt_number=1,
            ray_job_id="raysubmit_result_003",
            ray_address="ray://cluster:10001",
            completion_data=json.dumps(
                {
                    "success": True,
                    "result": None,
                    "result_reference": "oversize://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?bytes=256",
                }
            ),
        )

        class FakeRunner:
            def get_status(self, handle):
                from django_ray.runner.base import JobInfo, JobStatus

                return JobInfo(
                    job_id=handle.ray_job_id,
                    status=JobStatus.SUCCEEDED,
                    message="completed",
                )

            def get_logs(self, handle):
                return "prefix\nnot-a-completion-payload"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd = self._make_command()
        cmd.active_tasks = {task.pk: "raysubmit_result_003"}
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data is None
        assert (
            task.result_reference
            == "oversize://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?bytes=256"
        )
        assert task.pk not in cmd.active_tasks
