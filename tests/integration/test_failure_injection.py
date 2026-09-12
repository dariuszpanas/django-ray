"""Released direct worker failure injection on the actual preactivation schema.

Current-cohort failure/cleanup ownership has separate cohort worker coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from io import StringIO
from types import SimpleNamespace

import pytest

from django_ray import __version__ as django_ray_version
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runner.ray_core import RayCoreCompletion, RayCoreHandle
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from tests.migration_cleanup import preactivation_protocol_schema as preactivation_protocol_schema
from tests.workflow_progress_summary_helpers import workflow_progress_summary

LEGACY_PROTOCOLS = ExecutionProtocolRange(1, 1)


class HistoricalCommand(Command):
    def _handle_task_failure(self, *args, **kwargs):
        kwargs.setdefault("supported_protocols", LEGACY_PROTOCOLS)
        return super()._handle_task_failure(*args, **kwargs)

    def _store_and_succeed_task(self, *args, **kwargs):
        kwargs.setdefault("supported_protocols", LEGACY_PROTOCOLS)
        return super()._store_and_succeed_task(*args, **kwargs)


@pytest.fixture
def historical_worker(preactivation_protocol_schema, monkeypatch):
    from django_ray import lifecycle
    from django_ray.management.commands import django_ray_worker as worker
    from django_ray.runner import reconciliation

    for name in ("record_failure", "record_lost"):
        monkeypatch.setattr(
            reconciliation,
            name,
            partial(getattr(lifecycle, name), supported_protocols=LEGACY_PROTOCOLS),
        )
    for name in ("finalize_cancellation", "cancel_task"):
        monkeypatch.setattr(
            worker, name, partial(lifecycle.cancel_task, supported_protocols=LEGACY_PROTOCOLS)
        )

    def retry(execution, **options):
        assert lifecycle.retry_task(execution, **options) is None
        return lifecycle._request_task_retry(
            execution, supported_protocols=LEGACY_PROTOCOLS, **options
        )[1]

    monkeypatch.setattr(worker, "retry_task", retry)


def _historical_task(**fields):
    return RayTaskExecution.objects.create(execution_protocol_version=1, **fields)


def _historical_lease(**fields):
    return TaskWorkerLease.objects.create(
        capability_schema_version=1,
        django_ray_version=django_ray_version,
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
        **fields,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("historical_worker")
class TestFailureInjection:
    """Deterministic failure-injection scenarios for worker behavior."""

    @staticmethod
    def _make_command(worker_id: str = "failure-worker"):
        import os
        import socket

        cmd = HistoricalCommand()
        cmd.stdout = StringIO()
        cmd.style = cmd.style
        cmd.worker_id = worker_id
        cmd.execution_mode = "local"
        cmd.sync_mode = False
        cmd.active_tasks = {}
        cmd.ray_core_runner = None
        now = datetime.now(UTC)
        cmd.lease = _historical_lease(
            worker_id=worker_id,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            queue_name="default",
            started_at=now,
            last_heartbeat_at=now,
        )
        cmd.lease_identity = WorkerLeaseIdentity(worker_id, cmd.lease.hostname, cmd.lease.pid, now)
        return cmd

    def test_ray_disconnect_retries_pending_ray_core_tasks(self, monkeypatch):
        """If Ray disconnects, pending Ray Core tasks should go through retry policy."""
        cmd = self._make_command()
        task = _historical_task(
            task_id="test-fi-disconnect-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            claimed_by_worker=cmd.worker_id,
        )

        pending = {
            task.pk: RayCoreHandle(
                task_pk=task.pk,
                object_ref=object(),
                submitted_at=datetime.now(UTC),
                task_name="test",
                attempt_number=task.attempt_number,
                execution_generation=task.execution_generation,
            )
        }

        def retire_pending_handle(handle: RayCoreHandle) -> bool:
            if pending.get(handle.task_pk) is not handle:
                return False
            pending.pop(handle.task_pk)
            return True

        runner = SimpleNamespace(
            _pending_tasks=pending,
            pending_count=len(pending),
            pending_task_ids=tuple(pending),
            pending_task_handles=tuple(pending.values()),
            retire_pending_handle=retire_pending_handle,
        )

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
        cmd = self._make_command()
        task = _historical_task(
            task_id="test-fi-stopped-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[3, 4]",
            kwargs_json="{}",
            attempt_number=1,
            workflow_run_id="00000000-0000-0000-0000-000000000125",
            ray_job_id="raysubmit_stopped_001",
            ray_address="ray://cluster:10001",
            claimed_by_worker=cmd.worker_id,
        )
        task.workflow_progress_summary_json = serialize_workflow_progress_summary(
            workflow_progress_summary(task, state="CANCELLED")
        )
        task.save(update_fields=["workflow_progress_summary_json"])

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

        cmd.active_tasks = {task.pk: "raysubmit_stopped_001"}
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert task.pk not in cmd.active_tasks
        attempt = TaskAttempt.objects.get(execution=task, attempt_number=1)
        assert attempt.workflow_progress_summary_json == task.workflow_progress_summary_json

    def test_cancellation_race_prefers_cancelled_over_completed_result(self, monkeypatch):
        """If cancellation arrives before poll processing, task should finalize CANCELLED."""
        cmd = self._make_command()
        task = _historical_task(
            task_id="test-fi-cancel-race-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[5, 6]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(seconds=5),
            claimed_by_worker=cmd.worker_id,
        )

        class FakeRunner:
            def __init__(self):
                self._pending_tasks = {task.pk: object()}

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return (
                    RayCoreHandle(
                        task_pk=task.pk,
                        object_ref=self._pending_tasks[task.pk],
                        submitted_at=datetime.now(UTC),
                        task_name="test",
                        attempt_number=task.attempt_number,
                        execution_generation=task.execution_generation,
                    ),
                )

            def poll_completed(self, handles=None):
                self._pending_tasks.clear()
                return [
                    RayCoreCompletion(
                        task_pk=task.pk,
                        attempt_number=task.attempt_number,
                        execution_generation=task.execution_generation,
                        result_json='{"success": true, "result": 11}',
                    )
                ]

        cmd.ray_core_runner = FakeRunner()

        monkeypatch.setattr("ray.is_initialized", lambda: True)

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert task.result_data is None

    def test_expired_worker_heartbeat_recovers_orphaned_running_task(self):
        """Tasks owned by workers with expired heartbeats should be recovered."""
        _historical_lease(
            worker_id="expired-worker",
            hostname="host-expired",
            pid=9999,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
            is_active=True,
        )

        task = _historical_task(
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
