"""Race-safe task lifecycle transitions and attempt history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction

from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.runtime.runtime_env import runtime_env_for_execution
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


QUEUE_EXPIRED_ERROR = "Task expired before execution after exceeding its queued-wait deadline"


def _refresh_queue_deadline(
    execution: RayTaskExecution,
    *,
    queued_at: datetime,
    run_after: datetime | None,
) -> None:
    """Give a newly queued attempt its full snapshotted wait budget."""
    if execution.queue_timeout_seconds is None:
        execution.queue_deadline_at = None
        return
    eligibility_at = max(queued_at, run_after) if run_after is not None else queued_at
    execution.queue_deadline_at = eligibility_at + timedelta(
        seconds=int(execution.queue_timeout_seconds)
    )


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


class TaskCancellationRequestStatus(StrEnum):
    """Bounded result of requesting cancellation for one durable execution."""

    ACCEPTED = "ACCEPTED"
    ALREADY_REQUESTED = "ALREADY_REQUESTED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    COMPLETION_PENDING = "COMPLETION_PENDING"
    NOT_FOUND = "NOT_FOUND"
    STALE_ATTEMPT = "STALE_ATTEMPT"
    STALE_GENERATION = "STALE_GENERATION"
    INVALID_STATE = "INVALID_STATE"


@dataclass(frozen=True)
class TaskCancellationRequestResult:
    """Stable cancellation-request result without authorization policy."""

    status: TaskCancellationRequestStatus
    execution_id: int
    state: str | None
    attempt_number: int | None
    execution_generation: int | None

    @property
    def accepted(self) -> bool:
        """Return whether this call owns the cancellation transition."""
        return self.status is TaskCancellationRequestStatus.ACCEPTED


class TaskRetryRequestStatus(StrEnum):
    """Bounded result of requesting one manual durable-task retry."""

    ACCEPTED = "ACCEPTED"
    NOT_RETRYABLE = "NOT_RETRYABLE"
    NOT_FOUND = "NOT_FOUND"
    STALE_ATTEMPT = "STALE_ATTEMPT"
    STALE_GENERATION = "STALE_GENERATION"
    STALE_WORKFLOW_IDENTITY = "STALE_WORKFLOW_IDENTITY"


@dataclass(frozen=True)
class TaskRetryRequestResult:
    """Stable manual-retry result without authorization or execution payloads."""

    status: TaskRetryRequestStatus
    execution_id: int
    state: str | None
    attempt_number: int | None
    execution_generation: int | None

    @property
    def accepted(self) -> bool:
        """Return whether this call queued the replacement attempt."""
        return self.status is TaskRetryRequestStatus.ACCEPTED


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


def _prepare_terminal_only_workflow_progress_summary(
    execution: RayTaskExecution,
) -> str | None:
    """Prepare one summary at the accepted durable success or failure transition."""
    if (
        execution.state not in {TaskState.SUCCEEDED, TaskState.FAILED}
        or execution.workflow_progress_summary_json is not None
        or execution.workflow_run_id is None
        or not isinstance(execution.workflow_plan_fingerprint, str)
        or not isinstance(execution.workflow_plan_json, str)
        or not isinstance(execution.workflow_plan_selection, str)
    ):
        return None

    try:
        from django_ray.conf.settings import get_settings
        from django_ray.workflow_plans import (
            PLAN_DOMAIN_SEPARATOR,
            PLAN_FORMAT,
            PLAN_FORMAT_VERSION,
            effective_plan_selection_reporting_policy,
            validate_plan_selection_manifest,
        )
        from django_ray.workflow_progress_publication import (
            prepare_terminal_only_workflow_progress_summary,
        )

        selection = validate_plan_selection_manifest(json.loads(execution.workflow_plan_selection))
        if effective_plan_selection_reporting_policy(selection) != "terminal_only":
            return None
        selected_strategy = selection["selected_strategy"]
        if not isinstance(selected_strategy, str):
            return None

        expected_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                PLAN_DOMAIN_SEPARATOR + execution.workflow_plan_json.encode("utf-8")
            ).hexdigest()
        )
        if execution.workflow_plan_fingerprint != expected_fingerprint:
            return None

        plan = json.loads(execution.workflow_plan_json)
        if (
            not isinstance(plan, dict)
            or plan.get("plan_format") != PLAN_FORMAT
            or plan.get("plan_format_version") != PLAN_FORMAT_VERSION
        ):
            return None
        nodes = plan.get("nodes")
        edges = plan.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            return None
        snapshot = plan.get("snapshot")
        if isinstance(snapshot, dict):
            declared_nodes = snapshot.get("observed_node_count")
            declared_edges = snapshot.get("observed_edge_count")
        else:
            declared_nodes = None
            declared_edges = None
        if (
            type(declared_nodes) is not int
            or declared_nodes < 0
            or type(declared_edges) is not int
            or declared_edges < 0
        ):
            declared_nodes = len(nodes)
            declared_edges = len(edges)

        finished_at = execution.finished_at or datetime.now(UTC)
        started_at = execution.started_at or finished_at
        if started_at > finished_at:
            started_at = finished_at
        identity = WorkflowRunIdentity(
            task_execution_pk=execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
            run_id=str(execution.workflow_run_id),
        )
        summary = prepare_terminal_only_workflow_progress_summary(
            identity,
            plan_fingerprint=execution.workflow_plan_fingerprint,
            selected_strategy=selected_strategy,
            declared_node_count=declared_nodes,
            declared_edge_count=declared_edges,
            outcome=execution.state,
            started_at=started_at.timestamp(),
            finished_at=finished_at.timestamp(),
            detail_days=int(get_settings().get("WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS", 7)),
        )
        return serialize_workflow_progress_summary(
            summary,
            expected_identity=identity,
        )
    except BaseException:
        return None


def _persist_terminal_only_workflow_progress_summary(
    execution: RayTaskExecution,
    *,
    serialized_summary: str,
    attempt_number: int,
    execution_generation: int,
    run_id: str,
    outcome: str,
    retain_as_current: bool,
) -> bool:
    """Attach one logical summary without weakening the core lifecycle write."""
    try:
        with transaction.atomic():
            archived = TaskAttempt.objects.filter(
                execution_id=execution.pk,
                attempt_number=attempt_number,
                state=outcome,
                workflow_progress_summary_json__isnull=True,
            ).update(workflow_progress_summary_json=serialized_summary)
            if archived != 1:
                raise RuntimeError("terminal-only attempt summary fence was rejected")
            if retain_as_current:
                current = RayTaskExecution.objects.filter(
                    pk=execution.pk,
                    state=outcome,
                    attempt_number=attempt_number,
                    execution_generation=execution_generation,
                    workflow_run_id=run_id,
                    workflow_progress_summary_json__isnull=True,
                ).update(workflow_progress_summary_json=serialized_summary)
                if current != 1:
                    raise RuntimeError("terminal-only current summary fence was rejected")
    except BaseException:
        return False
    if retain_as_current:
        execution.workflow_progress_summary_json = serialized_summary
    return True


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


def request_task_cancellation(
    execution_id: int,
    *,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
) -> TaskCancellationRequestResult:
    """Request cancellation under the durable execution row lock.

    Authorization belongs to the caller. A queued execution is cancelled and
    archived immediately. A running execution moves to ``CANCELLING`` so its
    worker can request best-effort backend interruption and finalize it. The
    request is rejected as ``COMPLETION_PENDING`` when the Ray Job entrypoint
    has already published its terminal envelope for worker reconciliation. The
    optional attempt and generation fences prevent a stale caller from
    controlling either a replacement attempt or a replacement execution.
    """
    with transaction.atomic():
        current = RayTaskExecution.objects.select_for_update().filter(pk=execution_id).first()
        if current is None:
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.NOT_FOUND,
                execution_id=execution_id,
                state=None,
                attempt_number=None,
                execution_generation=None,
            )

        state = str(current.state)
        attempt_number = int(current.attempt_number)
        generation = int(current.execution_generation)
        if expected_attempt_number is not None and attempt_number != expected_attempt_number:
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.STALE_ATTEMPT,
                execution_id=execution_id,
                state=state,
                attempt_number=attempt_number,
                execution_generation=generation,
            )
        if (
            expected_execution_generation is not None
            and generation != expected_execution_generation
        ):
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.STALE_GENERATION,
                execution_id=execution_id,
                state=state,
                attempt_number=attempt_number,
                execution_generation=generation,
            )

        if current.state == TaskState.QUEUED:
            current.state = TaskState.CANCELLED
            current.finished_at = datetime.now(UTC)
            _record_attempt(current)
            current.save(update_fields=["state", "finished_at"])
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.ACCEPTED,
                execution_id=execution_id,
                state=TaskState.CANCELLED,
                attempt_number=attempt_number,
                execution_generation=generation,
            )

        if current.state == TaskState.RUNNING:
            if current.completion_data is not None:
                return TaskCancellationRequestResult(
                    status=TaskCancellationRequestStatus.COMPLETION_PENDING,
                    execution_id=execution_id,
                    state=TaskState.RUNNING,
                    attempt_number=attempt_number,
                    execution_generation=generation,
                )
            current.state = TaskState.CANCELLING
            current.save(update_fields=["state"])
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.ACCEPTED,
                execution_id=execution_id,
                state=TaskState.CANCELLING,
                attempt_number=attempt_number,
                execution_generation=generation,
            )

        if current.state == TaskState.CANCELLING:
            status = TaskCancellationRequestStatus.ALREADY_REQUESTED
        elif current.state in {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.LOST,
            TaskState.EXPIRED,
        }:
            status = TaskCancellationRequestStatus.ALREADY_TERMINAL
        else:
            status = TaskCancellationRequestStatus.INVALID_STATE
        return TaskCancellationRequestResult(
            status=status,
            execution_id=execution_id,
            state=state,
            attempt_number=attempt_number,
            execution_generation=generation,
        )


def _request_task_retry(
    execution: RayTaskExecution | int,
    *,
    allowed_states: Iterable[str] = (
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.LOST,
        TaskState.EXPIRED,
    ),
    next_attempt_at: Any | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_workflow_identity: tuple[str | None, str | None] | None = None,
) -> tuple[TaskRetryRequestResult, RayTaskExecution | None]:
    """Apply one retry request and retain its bounded transition outcome."""
    execution_id = execution.pk if isinstance(execution, RayTaskExecution) else execution
    allowed = tuple(allowed_states)
    with transaction.atomic():
        current = RayTaskExecution.objects.select_for_update().filter(pk=execution_id).first()
        if current is None:
            return (
                TaskRetryRequestResult(
                    status=TaskRetryRequestStatus.NOT_FOUND,
                    execution_id=execution_id,
                    state=None,
                    attempt_number=None,
                    execution_generation=None,
                ),
                None,
            )
        state = str(current.state)
        attempt_number = int(current.attempt_number)
        generation = int(current.execution_generation)
        current_workflow_identity = (
            str(current.workflow_run_id) if current.workflow_run_id is not None else None,
            (
                str(current.workflow_plan_fingerprint)
                if current.workflow_plan_fingerprint is not None
                else None
            ),
        )
        if expected_attempt_number is not None and attempt_number != expected_attempt_number:
            return (
                TaskRetryRequestResult(
                    status=TaskRetryRequestStatus.STALE_ATTEMPT,
                    execution_id=execution_id,
                    state=state,
                    attempt_number=attempt_number,
                    execution_generation=generation,
                ),
                None,
            )
        if expected_execution_generation is not None and generation != (
            expected_execution_generation
        ):
            return (
                TaskRetryRequestResult(
                    status=TaskRetryRequestStatus.STALE_GENERATION,
                    execution_id=execution_id,
                    state=state,
                    attempt_number=attempt_number,
                    execution_generation=generation,
                ),
                None,
            )
        if (
            expected_workflow_identity is not None
            and current_workflow_identity != expected_workflow_identity
        ):
            return (
                TaskRetryRequestResult(
                    status=TaskRetryRequestStatus.STALE_WORKFLOW_IDENTITY,
                    execution_id=execution_id,
                    state=state,
                    attempt_number=attempt_number,
                    execution_generation=generation,
                ),
                None,
            )
        if current.state not in allowed:
            return (
                TaskRetryRequestResult(
                    status=TaskRetryRequestStatus.NOT_RETRYABLE,
                    execution_id=execution_id,
                    state=state,
                    attempt_number=attempt_number,
                    execution_generation=generation,
                ),
                None,
            )
        runtime_env_for_execution(current)
        _record_attempt(current)
        current.state = TaskState.QUEUED
        current.attempt_number = attempt_number + 1
        current.execution_generation = generation + 1
        current.run_after = next_attempt_at
        _refresh_queue_deadline(
            current,
            queued_at=datetime.now(UTC),
            run_after=next_attempt_at,
        )
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
                "queue_deadline_at",
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
        return (
            TaskRetryRequestResult(
                status=TaskRetryRequestStatus.ACCEPTED,
                execution_id=execution_id,
                state=TaskState.QUEUED,
                attempt_number=int(current.attempt_number),
                execution_generation=int(current.execution_generation),
            ),
            current,
        )


def request_task_retry(
    execution: RayTaskExecution | int,
    *,
    allowed_states: Iterable[str] = (
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.LOST,
        TaskState.EXPIRED,
    ),
    next_attempt_at: Any | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_workflow_identity: tuple[str | None, str | None] | None = None,
) -> TaskRetryRequestResult:
    """Request a retry and return a bounded reason for the locked outcome.

    Authorization belongs to the caller. The optional attempt, generation, and
    workflow identity fences distinguish a stale request from a current row that
    is not retryable. RuntimeEnv verification failures remain exceptions so an
    adapter can map them to one fixed redaction-safe response.
    """
    result, _execution = _request_task_retry(
        execution,
        allowed_states=allowed_states,
        next_attempt_at=next_attempt_at,
        expected_attempt_number=expected_attempt_number,
        expected_execution_generation=expected_execution_generation,
        expected_workflow_identity=expected_workflow_identity,
    )
    return result


def retry_task(
    execution: RayTaskExecution | int,
    *,
    allowed_states: Iterable[str] = (
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.LOST,
        TaskState.EXPIRED,
    ),
    next_attempt_at: Any | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_workflow_identity: tuple[str | None, str | None] | None = None,
) -> RayTaskExecution | None:
    """Queue a failed execution while retaining the historical return contract.

    The row lock makes retries from the admin, API, and workers mutually
    exclusive. ``None`` means another transition won the race, the current
    state is not retryable, or an optional attempt/generation/workflow fence
    is stale. New adapters that need the bounded rejection reason should use
    :func:`request_task_retry`. The workflow identity tuple contains the run
    ID followed by the plan fingerprint; pass ``(None, None)`` to fence an
    execution that has no workflow identity.
    """
    _result, retried = _request_task_retry(
        execution,
        allowed_states=allowed_states,
        next_attempt_at=next_attempt_at,
        expected_attempt_number=expected_attempt_number,
        expected_execution_generation=expected_execution_generation,
        expected_workflow_identity=expected_workflow_identity,
    )
    return retried


def record_failure(
    execution: RayTaskExecution,
    *,
    error_message: str,
    error_traceback: str | None = None,
    retry: bool,
    next_attempt_at: Any | None = None,
    expected_ray_job_id: str | None = None,
    expected_claimed_by_worker: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_completion_data: str | None = None,
    require_completion_data_match: bool = False,
    cancellation_status: str | None = None,
    cancellation_error: str | None = None,
) -> bool:
    """Persist a failure and optionally queue the next attempt atomically."""
    with transaction.atomic():
        filters: dict[str, Any] = {"pk": execution.pk, "state": TaskState.RUNNING}
        if expected_ray_job_id is not None:
            filters["ray_job_id"] = expected_ray_job_id
        if expected_claimed_by_worker is not None:
            filters["claimed_by_worker"] = expected_claimed_by_worker
        if expected_attempt_number is not None:
            filters["attempt_number"] = expected_attempt_number
        if expected_execution_generation is not None:
            filters["execution_generation"] = expected_execution_generation
        if require_completion_data_match:
            filters["completion_data"] = expected_completion_data
        current = RayTaskExecution.objects.select_for_update().filter(**filters).first()
        if current is None:
            return False
        if retry:
            runtime_env_for_execution(current)

        current.error_message = error_message
        current.error_traceback = error_traceback
        current.cancellation_status = cancellation_status
        current.cancellation_error = cancellation_error
        current.finished_at = datetime.now(UTC)
        current.state = TaskState.FAILED
        terminal_only_summary = _prepare_terminal_only_workflow_progress_summary(current)
        terminal_attempt_number = int(current.attempt_number)
        terminal_execution_generation = int(current.execution_generation)
        terminal_run_id = str(current.workflow_run_id)
        _record_attempt(current)
        if retry:
            current.state = TaskState.QUEUED
            current.attempt_number = int(current.attempt_number) + 1
            current.run_after = next_attempt_at
            _refresh_queue_deadline(
                current,
                queued_at=datetime.now(UTC),
                run_after=next_attempt_at,
            )
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
                "queue_deadline_at",
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
        if terminal_only_summary is not None:
            _persist_terminal_only_workflow_progress_summary(
                current,
                serialized_summary=terminal_only_summary,
                attempt_number=terminal_attempt_number,
                execution_generation=terminal_execution_generation,
                run_id=terminal_run_id,
                outcome=TaskState.FAILED,
                retain_as_current=not retry,
            )
        execution.__dict__.update(current.__dict__)
        return True


def expire_queued_tasks(
    queue_names: Iterable[str],
    *,
    now: datetime,
    limit: int = 100,
) -> tuple[int, ...]:
    """Terminalize one bounded locked batch whose queue deadline is due."""
    if limit <= 0:
        return ()
    with transaction.atomic():
        rows = list(
            RayTaskExecution.objects.select_for_update(skip_locked=True)
            .filter(
                state=TaskState.QUEUED,
                queue_name__in=tuple(queue_names),
                queue_deadline_at__isnull=False,
                queue_deadline_at__lte=now,
            )
            .order_by("queue_deadline_at", "pk")[:limit]
        )
        expired: list[int] = []
        for current in rows:
            current.state = TaskState.EXPIRED
            current.finished_at = now
            current.error_message = QUEUE_EXPIRED_ERROR
            current.error_traceback = None
            _record_attempt(current)
            current.save(update_fields=["state", "finished_at", "error_message", "error_traceback"])
            if current.pk is not None:
                expired.append(current.pk)
        return tuple(expired)


def record_lost(
    execution: RayTaskExecution,
    *,
    error_message: str,
    expected_completion_data: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
) -> bool:
    """Persist a LOST transition and its bounded terminal attempt summary."""
    filters: dict[str, Any] = {
        "pk": execution.pk,
        "state": TaskState.RUNNING,
        # LOST recovery is based on an observed lack of activity. Revalidate
        # that exact observation after acquiring the row lock so a stale
        # detector cannot overwrite a concurrent heartbeat or orphan adoption.
        "claimed_by_worker": execution.claimed_by_worker,
        "ray_job_id": execution.ray_job_id,
        "last_heartbeat_at": execution.last_heartbeat_at,
        "completion_data": expected_completion_data,
    }
    if expected_attempt_number is not None:
        filters["attempt_number"] = expected_attempt_number
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
    expected_claimed_by_worker: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_completion_data: str | None = None,
    require_completion_data_match: bool = False,
) -> bool:
    """Persist a successful terminal transition with stale-write protection."""
    filters: dict[str, Any] = {"pk": execution.pk, "state": TaskState.RUNNING}
    if expected_ray_job_id is not None:
        filters["ray_job_id"] = expected_ray_job_id
    if expected_claimed_by_worker is not None:
        filters["claimed_by_worker"] = expected_claimed_by_worker
    if expected_attempt_number is not None:
        filters["attempt_number"] = expected_attempt_number
    if expected_execution_generation is not None:
        filters["execution_generation"] = expected_execution_generation
    if require_completion_data_match:
        filters["completion_data"] = expected_completion_data
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
        terminal_only_summary = _prepare_terminal_only_workflow_progress_summary(current)
        terminal_attempt_number = int(current.attempt_number)
        terminal_execution_generation = int(current.execution_generation)
        terminal_run_id = str(current.workflow_run_id)
        _record_attempt(current)
        current.save(
            update_fields=[
                "state",
                "finished_at",
                "result_data",
                "result_reference",
                "error_message",
                "error_traceback",
                "workflow_progress_summary_json",
            ]
        )
        if terminal_only_summary is not None:
            _persist_terminal_only_workflow_progress_summary(
                current,
                serialized_summary=terminal_only_summary,
                attempt_number=terminal_attempt_number,
                execution_generation=terminal_execution_generation,
                run_id=terminal_run_id,
                outcome=TaskState.SUCCEEDED,
                retain_as_current=True,
            )
        execution.__dict__.update(current.__dict__)
        return True


def cancel_task(
    execution: RayTaskExecution,
    *,
    expected_worker_id: str | None = None,
    expected_ray_job_id: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    allowed_states: Iterable[str] = (TaskState.CANCELLING,),
    expected_completion_data: str | None = None,
    require_completion_data_match: bool = False,
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
        if expected_attempt_number is not None:
            filters["attempt_number"] = expected_attempt_number
        if expected_execution_generation is not None:
            filters["execution_generation"] = expected_execution_generation
        if require_completion_data_match:
            filters["completion_data"] = expected_completion_data
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
