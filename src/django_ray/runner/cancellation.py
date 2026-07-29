"""Task cancellation handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from django_ray.lifecycle import request_task_cancellation
from django_ray.models import TaskState

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


@dataclass(frozen=True)
class PreparedRemoteCancellation:
    """A backend-specific cancellation capability resolved before a row lock."""

    supported: bool
    capability: object | None = None
    error: CancellationOutcome | None = None


def prepare_remote_cancellation(
    runner: BaseRunner,
    handle: object,
) -> PreparedRemoteCancellation:
    """Resolve a backend control client before entering a database row lock."""
    prepare = getattr(runner, "prepare_cancellation", None)
    if not callable(prepare):
        return PreparedRemoteCancellation(supported=False)
    try:
        return PreparedRemoteCancellation(supported=True, capability=prepare(handle))
    except Exception as exc:
        return PreparedRemoteCancellation(
            supported=True,
            error=CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Cancellation preparation raised {type(exc).__name__}: {exc}",
            ),
        )


def request_remote_cancellation(
    runner: BaseRunner,
    handle: object,
    *,
    prepared: PreparedRemoteCancellation | None = None,
) -> CancellationOutcome:
    """Request a remote stop and preserve whether the result is known."""
    if prepared is not None and prepared.supported:
        if prepared.error is not None:
            return prepared.error
        cancel_prepared = getattr(runner, "cancel_prepared_with_status", None)
        if not callable(cancel_prepared):
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                "Backend prepared cancellation but cannot execute the prepared capability",
            )
        try:
            return cancel_prepared(handle, prepared.capability)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Prepared cancellation request raised {type(exc).__name__}: {exc}",
            )

    cancel_with_status = getattr(runner, "cancel_with_status", None)
    try:
        if callable(cancel_with_status):
            return cancel_with_status(handle)
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
    execution_id = task_execution.pk
    if execution_id is None:
        return False
    result = request_task_cancellation(
        execution_id,
        expected_attempt_number=task_execution.attempt_number,
        expected_execution_generation=task_execution.execution_generation,
    )
    if not result.accepted:
        return False

    assert result.state is not None
    task_execution.state = result.state
    if result.state != TaskState.CANCELLING:
        return True

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
            if str(task_execution.ray_job_id).startswith("ray_core:"):
                get_pending_handle = getattr(runner, "get_pending_handle", None)
                cancel_pending = getattr(runner, "cancel_pending", None)
                if callable(get_pending_handle) and callable(cancel_pending):
                    pending_handle = get_pending_handle(
                        execution_id,
                        attempt_number=task_execution.attempt_number,
                        execution_generation=task_execution.execution_generation,
                    )
                    if pending_handle is not None:
                        cancel_pending(pending_handle)
            else:
                runner.cancel(handle)
        except (RuntimeError, ConnectionError, TimeoutError):
            # Best effort - cancellation may fail due to Ray connection issues
            pass

    return True


def finalize_cancellation(
    task_execution: RayTaskExecution,
    *,
    expected_worker_id: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
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
    from django_ray.lifecycle import cancel_task

    return cancel_task(
        task_execution,
        expected_worker_id=expected_worker_id,
        expected_attempt_number=expected_attempt_number,
        expected_execution_generation=expected_execution_generation,
        cancellation_status=cancellation_status,
        cancellation_error=cancellation_error,
    )
