"""Failure-injection integration tests for worker reliability paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace

import pytest

from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease


@pytest.mark.django_db
class TestFailureInjection:
    """Deterministic failure-injection scenarios for worker behavior."""

    @staticmethod
    def _make_command(worker_id: str = "failure-worker"):
        from django_ray.management.commands.django_ray_worker import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.worker_id = worker_id
        cmd.execution_mode = "local"
        cmd.sync_mode = False
        cmd.active_tasks = {}
        cmd.ray_core_runner = None
        return cmd

    def test_ray_disconnect_retries_pending_ray_core_tasks(self, monkeypatch):
        """If Ray disconnects, pending Ray Core tasks should go through retry policy."""
        task = RayTaskExecution.objects.create(
            task_id="test-fi-disconnect-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            claimed_by_worker="failure-worker",
        )

        pending = {task.pk: object()}
        runner = SimpleNamespace(
            _pending_tasks=pending,
            pending_count=len(pending),
        )

        cmd = self._make_command()
        cmd.ray_core_runner = runner

        monkeypatch.setattr("ray.is_initialized", lambda: False)

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert "Ray connection lost" in (task.error_message or "")
        assert runner._pending_tasks == {}

    def test_ray_job_stopped_marks_task_cancelled(self, monkeypatch):
        """A STOPPED Ray Job result should become CANCELLED in reconciliation."""
        task = RayTaskExecution.objects.create(
            task_id="test-fi-stopped-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[3, 4]",
            kwargs_json="{}",
            attempt_number=1,
            ray_job_id="raysubmit_stopped_001",
            ray_address="ray://cluster:10001",
        )

        class FakeRunner:
            def get_status(self, handle):
                from django_ray.runner.base import JobInfo, JobStatus

                return JobInfo(
                    job_id=handle.ray_job_id,
                    status=JobStatus.STOPPED,
                    message="stopped by operator",
                )

            def get_logs(self, handle):
                return ""

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd = self._make_command()
        cmd.active_tasks = {task.pk: "raysubmit_stopped_001"}
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert task.pk not in cmd.active_tasks

    def test_cancellation_race_prefers_cancelled_over_completed_result(self, monkeypatch):
        """If cancellation arrives before poll processing, task should finalize CANCELLED."""
        task = RayTaskExecution.objects.create(
            task_id="test-fi-cancel-race-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[5, 6]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(seconds=5),
            claimed_by_worker="failure-worker",
        )

        class FakeRunner:
            def __init__(self):
                self._pending_tasks = {task.pk: object()}

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

            def poll_completed(self):
                self._pending_tasks.clear()
                return [(task.pk, '{"success": true, "result": 11}')]

        cmd = self._make_command()
        cmd.ray_core_runner = FakeRunner()

        monkeypatch.setattr("ray.is_initialized", lambda: True)

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert task.result_data is None

    def test_expired_worker_heartbeat_recovers_orphaned_running_task(self):
        """Tasks owned by workers with expired heartbeats should be recovered."""
        TaskWorkerLease.objects.create(
            worker_id="expired-worker",
            hostname="host-expired",
            pid=9999,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
            is_active=True,
        )

        task = RayTaskExecution.objects.create(
            task_id="test-fi-heartbeat-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[7, 8]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            claimed_by_worker="expired-worker",
        )

        cmd = self._make_command()
        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.claimed_by_worker is None
