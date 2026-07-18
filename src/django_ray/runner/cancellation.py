"""Task cancellation handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution
    from django_ray.runner.base import BaseRunner


class CancellationOutcomeStatus(StrEnum):
    """Result of asking the remote execution backend to stop a task."""

    REQUESTED = "REQUESTED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class CancellationOutcome:
    """Observable result of a remote cancellation request."""

    status: CancellationOutcomeStatus
    message: str | None = None


def request_remote_cancellation(runner: BaseRunner, handle: object) -> CancellationOutcome:
    """Request a remote stop and preserve whether the result is known."""
    cancel_with_status = getattr(runner, "cancel_with_status", None)
    if callable(cancel_with_status):
        return cancel_with_status(handle)

    try:
        accepted = runner.cancel(handle)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover - exercised by backend implementations
        return CancellationOutcome(
            CancellationOutcomeStatus.INDETERMINATE,
            f"Cancellation request raised {type(exc).__name__}: {exc}",
        )

    if accepted:
        return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
    return CancellationOutcome(
        CancellationOutcomeStatus.FAILED,
        "Cancellation API rejected the stop request",
    )


def request_cancellation(
    task_execution: RayTaskExecution,
    runner: BaseRunner,
) -> bool:
    """Request cancellation of a task execution.

    This is a best-effort operation. The task may complete
    before the cancellation takes effect.

    Args:
        task_execution: The task execution to cancel.
        runner: The runner to use for cancellation.

    Returns:
        True if cancellation was initiated.
    """
    if task_execution.state not in ("QUEUED", "RUNNING"):
        return False

    # Mark as cancellation requested
    task_execution.state = "CANCELLING"
    task_execution.save(update_fields=["state"])

    # If we have a Ray job ID, try to stop it
    if task_execution.ray_job_id:
        from django_ray.runner.base import SubmissionHandle

        started = task_execution.started_at
        handle = SubmissionHandle(
            ray_job_id=str(task_execution.ray_job_id),
            ray_address=str(task_execution.ray_address or ""),
            submitted_at=started if isinstance(started, datetime) else datetime.now(UTC),
        )

        try:
            runner.cancel(handle)
        except (RuntimeError, ConnectionError, TimeoutError):
            # Best effort - cancellation may fail due to Ray connection issues
            pass

    return True


def finalize_cancellation(
    task_execution: RayTaskExecution,
    *,
    expected_worker_id: str | None = None,
    cancellation_status: str | None = None,
    cancellation_error: str | None = None,
) -> bool:
    """Finalize a cancelled task execution.

    Args:
        task_execution: The task execution to finalize.

    Returns:
        True when this call transitioned the row to ``CANCELLED``. False when
        another worker or completion path changed the row first.
    """
    finished_at = datetime.now(UTC)
    filters: dict[str, object] = {
        "pk": task_execution.pk,
        "state": "CANCELLING",
    }
    if expected_worker_id is not None:
        filters["claimed_by_worker"] = expected_worker_id

    update_values: dict[str, object] = {
        "state": "CANCELLED",
        "finished_at": finished_at,
    }
    if cancellation_status is not None:
        update_values["cancellation_status"] = cancellation_status
    if cancellation_error is not None or cancellation_status is not None:
        update_values["cancellation_error"] = cancellation_error

    updated = type(task_execution).objects.filter(**filters).update(**update_values)
    if not updated:
        return False

    task_execution.state = "CANCELLED"
    task_execution.finished_at = finished_at
    if cancellation_status is not None:
        task_execution.cancellation_status = cancellation_status
    if cancellation_error is not None or cancellation_status is not None:
        task_execution.cancellation_error = cancellation_error
    return True
