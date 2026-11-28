"""Unit tests for lifecycle retry hygiene and batch expiry (F-029, F-077)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    expire_queued_tasks,
    record_failure,
    request_task_cancellation,
)
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
)
from django_ray.runtime.runtime_env import normalize_runtime_env


@pytest.mark.django_db
class TestLifecycleRetryHygiene:
    """Verify automatic retry matches manual retry hygiene and generation fencing."""

    def test_record_failure_retry_clears_stale_fields_and_bumps_generation(self) -> None:
        task = RayTaskExecution.objects.create(
            runtime_env_hash=normalize_runtime_env({}).digest,
            task_id="auto-retry-hygiene-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=5,
            claimed_by_worker="worker-a",
            managed_with_django_ray_version="0.8.0-manager",
            executor_django_ray_version="0.8.0-executor",
            ray_job_id="raysubmit_previous_attempt",
            ray_job_request_reference="inputfs://sha256/previous-request?bytes=64",
            ray_address="ray://legacy-target:10001",
            result_data='{"stale": true}',
            result_reference="digest:" + ("a" * 64),
            error_message="prior stale message",
            error_traceback="prior stale traceback",
            cancellation_status=CancellationStatus.INDETERMINATE,
            cancellation_error="prior stop uncertain",
            workflow_plan_selection='{"selected_strategy":"dynamic_tasks"}',
        )

        next_attempt_at = datetime.now(UTC) + timedelta(seconds=30)
        assert (
            record_failure(
                task,
                error_message="transient boom",
                error_traceback="RuntimeError: transient boom",
                retry=True,
                next_attempt_at=next_attempt_at,
                expected_attempt_number=2,
                expected_execution_generation=5,
            )
            is True
        )

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 3
        assert task.execution_generation == 6
        assert task.run_after == next_attempt_at
        assert task.error_message is None
        assert task.error_traceback is None
        assert task.ray_job_id is None
        assert task.ray_job_request_reference is None
        assert task.ray_address is None
        assert task.ray_target_address == "ray://legacy-target:10001"
        assert task.result_data is None
        assert task.result_reference is None
        assert task.claimed_by_worker is None
        assert task.managed_with_django_ray_version is None
        assert task.executor_django_ray_version is None
        assert task.cancellation_status is None
        assert task.cancellation_error is None
        assert task.workflow_plan_selection is None

        archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
        assert archived.state == TaskState.FAILED
        assert archived.error_message == "transient boom"
        assert archived.error_traceback == "RuntimeError: transient boom"
        assert archived.managed_with_django_ray_version == "0.8.0-manager"
        assert archived.executor_django_ray_version == "0.8.0-executor"

    def test_queued_cancel_after_auto_retry_never_leaks_prior_error_into_attempt(self) -> None:
        task = RayTaskExecution.objects.create(
            runtime_env_hash=normalize_runtime_env({}).digest,
            task_id="auto-retry-cancel-clean-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
            claimed_by_worker="worker-a",
            ray_job_id="raysubmit_attempt_1",
            ray_address="ray://cluster:10001",
        )

        assert record_failure(
            task,
            error_message="attempt 1 crashed",
            error_traceback="Traceback: attempt 1",
            retry=True,
        )

        cancel_result = request_task_cancellation(
            task.pk,
            expected_attempt_number=2,
            expected_execution_generation=2,
        )
        assert cancel_result.status is TaskCancellationRequestStatus.ACCEPTED
        assert cancel_result.state == TaskState.CANCELLED

        attempt_1 = TaskAttempt.objects.get(execution=task, attempt_number=1)
        attempt_2 = TaskAttempt.objects.get(execution=task, attempt_number=2)
        assert attempt_1.state == TaskState.FAILED
        assert attempt_1.error_message == "attempt 1 crashed"
        assert attempt_1.error_traceback == "Traceback: attempt 1"
        assert attempt_2.state == TaskState.CANCELLED
        assert attempt_2.error_message is None
        assert attempt_2.error_traceback is None

    def test_expire_queued_tasks_batches_multiple_overdue_rows(self) -> None:
        now = datetime.now(UTC)
        deadline = now - timedelta(seconds=5)
        tasks = [
            RayTaskExecution.objects.create(
                task_id=f"batch-expire-{idx}",
                callable_path="testproject.tasks.add_numbers",
                queue_name="default",
                state=TaskState.QUEUED,
                queue_timeout_seconds=60,
                queue_deadline_at=deadline,
            )
            for idx in range(3)
        ]

        expired_pks = expire_queued_tasks(["default"], now=now)

        assert set(expired_pks) == {task.pk for task in tasks}
        for task in tasks:
            task.refresh_from_db()
            assert task.state == TaskState.EXPIRED
            assert TaskAttempt.objects.filter(
                execution=task, attempt_number=1, state=TaskState.EXPIRED
            ).exists()
