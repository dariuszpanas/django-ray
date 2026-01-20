"""Task reconciliation logic for detecting stuck and lost tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django_ray.conf.settings import get_settings

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


def get_stuck_timeout() -> timedelta:
    """Get the timeout for considering a task stuck."""
    settings = get_settings()
    seconds = settings.get("STUCK_TASK_TIMEOUT_SECONDS", 300)
    return timedelta(seconds=seconds)


def is_task_stuck(task_execution: RayTaskExecution) -> bool:
    """Check if a task execution is stuck.

    A task is considered stuck if:
    - It is in RUNNING state
    - No heartbeat has been received beyond the timeout

    Args:
        task_execution: The task execution to check.

    Returns:
        True if the task appears to be stuck.
    """
    if task_execution.state != "RUNNING":
        return False

    timeout = get_stuck_timeout()
    now = datetime.now(UTC)

    last_activity = task_execution.last_heartbeat_at or task_execution.started_at

    if last_activity is None:
        return False

    last_activity_dt: datetime = last_activity  # type: ignore[assignment]
    return (now - last_activity_dt) > timeout


def is_task_timed_out(task_execution: RayTaskExecution) -> bool:
    """Check if a task execution has exceeded its timeout.

    A task is considered timed out if:
    - It is in RUNNING state
    - It has a timeout_seconds set
    - It has been running longer than timeout_seconds

    Args:
        task_execution: The task execution to check.

    Returns:
        True if the task has exceeded its timeout.
    """
    if task_execution.state != "RUNNING":
        return False

    if not task_execution.timeout_seconds:
        return False

    if not task_execution.started_at:
        return False

    now = datetime.now(UTC)
    started_at: datetime = task_execution.started_at  # type: ignore[assignment]
    elapsed = now - started_at

    return elapsed.total_seconds() > task_execution.timeout_seconds


def mark_task_lost(task_execution: RayTaskExecution) -> None:
    """Mark a task execution as LOST.

    Args:
        task_execution: The task execution to mark as lost.
    """
    task_execution.state = "LOST"
    task_execution.finished_at = datetime.now(UTC)
    task_execution.error_message = "Task marked as LOST due to missing heartbeats"
    task_execution.save(update_fields=["state", "finished_at", "error_message"])


def mark_task_timed_out(task_execution: RayTaskExecution) -> None:
    """Mark a task execution as FAILED due to timeout.

    Args:
        task_execution: The task execution to mark as timed out.
    """
    timeout = task_execution.timeout_seconds or 0
    task_execution.state = "FAILED"
    task_execution.finished_at = datetime.now(UTC)
    task_execution.error_message = f"Task timed out after {timeout} seconds"
    task_execution.save(update_fields=["state", "finished_at", "error_message"])
