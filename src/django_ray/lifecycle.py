"""Race-safe task lifecycle transitions and attempt history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction

from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_progress_summary import (
    WORKFLOW_PROGRESS_TERMINAL_STATES,
    WorkflowProgressDetailAvailability,
    WorkflowProgressSummaryError,
    WorkflowProgressTruncationReason,
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class LifecycleConflictError(Exception):
    """Raised when a transition's expected current state no longer matches."""


def promote_legacy_ray_target(execution: RayTaskExecution) -> bool:
    """Preserve an old Ray Job route without adopting Ray Core handle metadata.

    Call this while the execution row is locked and before clearing its mutable
    submission handle. A missing job ID identifies a never-submitted legacy row;
    submitted Ray Job IDs use Ray's package-owned ``raysubmit_`` prefix.
    """
    if execution.ray_target_address:
        return False
    address = execution.ray_address
    if not address or address == "auto":
        return False
    job_id = execution.ray_job_id
    if job_id and not str(job_id).startswith("raysubmit_"):
        return False
    execution.ray_target_address = address
    return True


def _canonical_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _attempt_workflow_progress_summary(execution: RayTaskExecution) -> str | None:
    """Return or derive one canonical terminal summary owned by this attempt."""
    serialized = execution.workflow_progress_summary_json
    if serialized is None or execution.workflow_run_id is None:
        return None
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=str(execution.workflow_run_id),
    )
    try:
        summary = deserialize_workflow_progress_summary(
            serialized,
            expected_identity=identity,
        )
        canonical = serialize_workflow_progress_summary(summary, expected_identity=identity)
        if canonical != serialized:
            return None
        terminal_summary_matches = (
            summary["state"] == execution.state
            and summary["terminal"]["outcome"] == execution.state
        )
        if terminal_summary_matches:
            if summary["detail_revision"] is None or execution.finished_at is None:
                return cast(str, serialized)
            reported_finished_at = datetime.fromisoformat(
                summary["terminal"]["finished_at"][:-1] + "+00:00"
            )
            outer_finished_at = execution.finished_at
            if outer_finished_at.tzinfo is None:
                outer_finished_at = outer_finished_at.replace(tzinfo=UTC)
            if outer_finished_at.astimezone(UTC) <= reported_finished_at:
                return cast(str, serialized)
        if execution.state not in WORKFLOW_PROGRESS_TERMINAL_STATES:
            return None

        terminal_at = execution.finished_at or datetime.now(UTC)
        previous_updated_at = datetime.fromisoformat(
            summary["timestamps"]["updated_at"][:-1] + "+00:00"
        )
        if terminal_at.tzinfo is None:
            terminal_at = terminal_at.replace(tzinfo=UTC)
        terminal_at = max(terminal_at.astimezone(UTC), previous_updated_at)
        terminal_timestamp = _canonical_utc(terminal_at)
        previous_state = summary["state"]
        summary["summary_revision"] = int(summary["summary_revision"]) + 1
        summary["state"] = execution.state
        if execution.state == TaskState.SUCCEEDED:
            discovered = int(summary["node_counts"]["discovered"])
            summary["node_counts"].update(
                pending=0,
                running=0,
                succeeded=discovered,
                failed=0,
            )
            summary["progress_percent"] = 100.0
            detail = summary["detail"]
            if (
                previous_state != TaskState.SUCCEEDED
                and summary["detail_revision"] is not None
                and detail["availability"]
                in {
                    WorkflowProgressDetailAvailability.AVAILABLE.value,
                    WorkflowProgressDetailAvailability.TRUNCATED.value,
                }
            ):
                reasons = set(detail["truncation_reasons"])
                reasons.add(WorkflowProgressTruncationReason.TERMINAL_STATE_UNREPORTED.value)
                summary["detail"] = {
                    "availability": WorkflowProgressDetailAvailability.TRUNCATED.value,
                    "complete": False,
                    "truncation_reasons": sorted(reasons),
                }
        summary["timestamps"]["updated_at"] = terminal_timestamp
        summary["timestamps"]["finished_at"] = terminal_timestamp
        summary["terminal"] = {
            "outcome": execution.state,
            "finished_at": terminal_timestamp,
        }
        if summary["detail_revision"] is not None:
            summary["retention"]["detail_expires_at"] = _canonical_utc(
                terminal_at + timedelta(days=int(summary["retention"]["detail_days"]))
            )
        derived = serialize_workflow_progress_summary(summary, expected_identity=identity)
        execution.workflow_progress_summary_json = derived
        if execution.finished_at is None:
            execution.finished_at = terminal_at
        execution.save(update_fields=["workflow_progress_summary_json"])
        return derived
    except (OverflowError, WorkflowProgressSummaryError):
        return None


def _record_attempt(execution: RayTaskExecution) -> None:
    """Persist the current execution snapshot before it is overwritten."""
    defaults = {
        "state": execution.state,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "error_message": execution.error_message,
        "error_traceback": execution.error_traceback,
        "result_data": execution.result_data,
        "result_reference": execution.result_reference,
    }
    workflow_progress_summary = _attempt_workflow_progress_summary(execution)
    from django_ray.workflow_progress_storage import (
        stamp_workflow_progress_detail_expiry_locked,
    )

    stamp_workflow_progress_detail_expiry_locked(
        execution,
        workflow_progress_summary,
    )
    if workflow_progress_summary is not None:
        defaults["workflow_progress_summary_json"] = workflow_progress_summary
    TaskAttempt.objects.update_or_create(
        execution=execution,
        attempt_number=execution.attempt_number,
        defaults=defaults,
    )


def retry_task(
    execution: RayTaskExecution | int,
    *,
    allowed_states: Iterable[str] = (TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST),
    next_attempt_at: Any | None = None,
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
        current.run_after = next_attempt_at
        current.result_data = None
        current.result_reference = None
        current.progress_data = None
        current.workflow_progress_summary_json = None
        current.workflow_run_id = None
        current.workflow_plan_selection = None
        current.completion_data = None
        current.error_message = None
        current.error_traceback = None
        current.started_at = None
        current.finished_at = None
        current.last_heartbeat_at = None
        current.claimed_by_worker = None
        promote_legacy_ray_target(current)
        current.ray_job_id = None
        current.ray_address = None
        current.cancellation_status = None
        current.cancellation_error = None
        current.save(
            update_fields=[
                "state",
                "attempt_number",
                "execution_generation",
                "run_after",
                "result_data",
                "result_reference",
                "progress_data",
                "workflow_progress_summary_json",
                "workflow_run_id",
                "workflow_plan_selection",
                "completion_data",
                "error_message",
                "error_traceback",
                "started_at",
                "finished_at",
                "last_heartbeat_at",
                "claimed_by_worker",
                "ray_job_id",
                "ray_target_address",
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
    cancellation_status: str | None = None,
    cancellation_error: str | None = None,
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
        current.cancellation_status = cancellation_status
        current.cancellation_error = cancellation_error
        current.finished_at = datetime.now(UTC)
        current.state = TaskState.FAILED
        _record_attempt(current)
        if retry:
            current.state = TaskState.QUEUED
            current.attempt_number = int(current.attempt_number) + 1
            current.run_after = next_attempt_at
            current.started_at = None
            current.finished_at = None
            current.claimed_by_worker = None
            current.progress_data = None
            current.workflow_progress_summary_json = None
            current.workflow_run_id = None
            current.workflow_plan_selection = None
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
                "cancellation_status",
                "cancellation_error",
                "started_at",
                "finished_at",
                "claimed_by_worker",
                "progress_data",
                "workflow_progress_summary_json",
                "workflow_run_id",
                "workflow_plan_selection",
                "completion_data",
            ]
        )
        execution.__dict__.update(current.__dict__)
        return True


def record_lost(
    execution: RayTaskExecution,
    *,
    error_message: str,
    expected_execution_generation: int | None = None,
) -> bool:
    """Persist a LOST transition and its bounded terminal attempt summary."""
    filters: dict[str, Any] = {"pk": execution.pk, "state": TaskState.RUNNING}
    if expected_execution_generation is not None:
        filters["execution_generation"] = expected_execution_generation
    with transaction.atomic():
        current = RayTaskExecution.objects.select_for_update().filter(**filters).first()
        if current is None:
            return False
        current.state = TaskState.LOST
        current.finished_at = datetime.now(UTC)
        current.error_message = error_message
        _record_attempt(current)
        current.save(update_fields=["state", "finished_at", "error_message"])
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
    expected_ray_job_id: str | None = None,
    expected_execution_generation: int | None = None,
    allowed_states: Iterable[str] = (TaskState.CANCELLING,),
    cancellation_status: str | None = None,
    cancellation_error: str | None = None,
) -> bool:
    """Finalize a cancellation and preserve the cancelled attempt."""
    with transaction.atomic():
        filters: dict[str, Any] = {
            "pk": execution.pk,
            "state__in": tuple(allowed_states),
        }
        if expected_worker_id is not None:
            filters["claimed_by_worker"] = expected_worker_id
        if expected_ray_job_id is not None:
            filters["ray_job_id"] = expected_ray_job_id
        if expected_execution_generation is not None:
            filters["execution_generation"] = expected_execution_generation
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
