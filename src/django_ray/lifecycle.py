"""Race-safe task lifecycle transitions and attempt history."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django.db import transaction

from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

if TYPE_CHECKING:
    from collections.abc import Iterable


class LifecycleConflictError(Exception):
    """Raised when a transition's expected current state no longer matches."""


def _record_attempt(execution: RayTaskExecution) -> None:
    """Persist the current execution snapshot before it is overwritten."""
    TaskAttempt.objects.update_or_create(
        execution=execution,
        attempt_number=execution.attempt_number,
        defaults={
            "state": execution.state,
            "started_at": execution.started_at,
            "finished_at": execution.finished_at,
            "error_message": execution.error_message,
            "error_traceback": execution.error_traceback,
            "result_data": execution.result_data,
            "result_reference": execution.result_reference,
        },
    )


def retry_task(
    execution: RayTaskExecution | int,
    *,
    allowed_states: Iterable[str] = (TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST),
) -> RayTaskExecution | None:
    """Queue a failed execution for its next one-based attempt.

    The row lock makes retries from the admin, API, and workers mutually
    exclusive. ``None`` means another transition won the race or the current
    state is not retryable.
    """
    execution_id = execution.pk if isinstance(execution, RayTaskExecution) else execution
    allowed = tuple(allowed_states)
    with transaction.atomic():
        current = RayTaskExecution.objects.select_for_update().filter(pk=execution_id).first()
        if current is None or current.state not in allowed:
            return None
        _record_attempt(current)
        current.state = TaskState.QUEUED
        current.attempt_number = int(current.attempt_number) + 1
        current.execution_generation = int(current.execution_generation) + 1
        current.result_data = None
        current.result_reference = None
        current.progress_data = None
        current.completion_data = None
        current.error_message = None
        current.error_traceback = None
        current.started_at = None
        current.finished_at = None
        current.last_heartbeat_at = None
        current.claimed_by_worker = None
        current.ray_job_id = None
        current.ray_address = None
        current.cancellation_status = None
        current.cancellation_error = None
        current.save(
            update_fields=[
                "state",
                "attempt_number",
                "execution_generation",
                "result_data",
                "result_reference",
                "progress_data",
                "completion_data",
                "error_message",
                "error_traceback",
                "started_at",
                "finished_at",
                "last_heartbeat_at",
                "claimed_by_worker",
                "ray_job_id",
                "ray_address",
                "cancellation_status",
                "cancellation_error",
            ]
        )
        return current


def record_failure(
    execution: RayTaskExecution,
    *,
    error_message: str,
    error_traceback: str | None = None,
    retry: bool,
    next_attempt_at: Any | None = None,
    expected_ray_job_id: str | None = None,
    expected_execution_generation: int | None = None,
) -> bool:
    """Persist a failure and optionally queue the next attempt atomically."""
    with transaction.atomic():
        filters: dict[str, Any] = {"pk": execution.pk, "state": TaskState.RUNNING}
        if expected_ray_job_id is not None:
            filters["ray_job_id"] = expected_ray_job_id
        if expected_execution_generation is not None:
            filters["execution_generation"] = expected_execution_generation
        current = RayTaskExecution.objects.select_for_update().filter(**filters).first()
        if current is None:
            return False

        current.error_message = error_message
        current.error_traceback = error_traceback
        current.finished_at = None if retry else datetime.now(UTC)
        current.state = TaskState.FAILED
        _record_attempt(current)
        if retry:
            current.state = TaskState.QUEUED
            current.attempt_number = int(current.attempt_number) + 1
            current.run_after = next_attempt_at
            current.started_at = None
            current.claimed_by_worker = None
            current.progress_data = None
            current.completion_data = None
        else:
            current.state = TaskState.FAILED
        current.save(
            update_fields=[
                "state",
                "attempt_number",
                "run_after",
                "error_message",
                "error_traceback",
                "started_at",
                "finished_at",
                "claimed_by_worker",
                "progress_data",
                "completion_data",
            ]
        )
        execution.__dict__.update(current.__dict__)
        return True


def succeed_task(
    execution: RayTaskExecution,
    *,
    result_data: str | None,
    result_reference: str | None,
    expected_ray_job_id: str | None = None,
    expected_execution_generation: int | None = None,
) -> bool:
    """Persist a successful terminal transition with stale-write protection."""
    filters: dict[str, Any] = {"pk": execution.pk, "state": TaskState.RUNNING}
    if expected_ray_job_id is not None:
        filters["ray_job_id"] = expected_ray_job_id
    if expected_execution_generation is not None:
        filters["execution_generation"] = expected_execution_generation
    with transaction.atomic():
        current = RayTaskExecution.objects.select_for_update().filter(**filters).first()
        if current is None:
            return False
        current.state = TaskState.SUCCEEDED
        current.finished_at = datetime.now(UTC)
        current.result_data = result_data
        current.result_reference = result_reference
        current.error_message = None
        current.error_traceback = None
        _record_attempt(current)
        current.save(
            update_fields=[
                "state",
                "finished_at",
                "result_data",
                "result_reference",
                "error_message",
                "error_traceback",
            ]
        )
        execution.__dict__.update(current.__dict__)
        return True


def cancel_task(
    execution: RayTaskExecution,
    *,
    expected_worker_id: str | None = None,
    cancellation_status: str | None = None,
    cancellation_error: str | None = None,
) -> bool:
    """Finalize a cancellation and preserve the cancelled attempt."""
    with transaction.atomic():
        filters: dict[str, Any] = {"pk": execution.pk, "state": TaskState.CANCELLING}
        if expected_worker_id is not None:
            filters["claimed_by_worker"] = expected_worker_id
        current = RayTaskExecution.objects.select_for_update().filter(**filters).first()
        if current is None:
            return False
        current.state = TaskState.CANCELLED
        current.finished_at = datetime.now(UTC)
        if cancellation_status is not None:
            current.cancellation_status = cancellation_status
        if cancellation_error is not None or cancellation_status is not None:
            current.cancellation_error = cancellation_error
        _record_attempt(current)
        current.save(
            update_fields=[
                "state",
                "finished_at",
                "cancellation_status",
                "cancellation_error",
            ]
        )
        execution.__dict__.update(current.__dict__)
        return True
