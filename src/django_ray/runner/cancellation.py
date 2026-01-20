"""Task cancellation handling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution
    from django_ray.runner.base import BaseRunner


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


def finalize_cancellation(task_execution: RayTaskExecution) -> None:
    """Finalize a cancelled task execution.

    Args:
        task_execution: The task execution to finalize.
    """
    task_execution.state = "CANCELLED"
    task_execution.finished_at = datetime.now(UTC)
    task_execution.save(update_fields=["state", "finished_at"])
