"""Retry policy implementation for failed tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from django_ray.conf.settings import get_settings

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


@dataclass
class RetryDecision:
    """Decision about whether and when to retry a task."""

    should_retry: bool
    next_attempt_at: datetime | None = None
    reason: str | None = None


def get_max_attempts() -> int:
    """Get the maximum number of task attempts."""
    settings = get_settings()
    return settings.get("MAX_TASK_ATTEMPTS", 3)


def get_base_backoff_seconds() -> int:
    """Get the base backoff duration in seconds."""
    settings = get_settings()
    return settings.get("RETRY_BACKOFF_SECONDS", 60)


def calculate_backoff(attempt_number: int) -> timedelta:
    """Calculate exponential backoff for retry.

    Args:
        attempt_number: The current attempt number (1-based).

    Returns:
        Backoff duration.
    """
    base = get_base_backoff_seconds()
    # Exponential backoff with jitter could be added here
    backoff_seconds = base * (2 ** (attempt_number - 1))
    # Cap at 1 hour
    backoff_seconds = min(backoff_seconds, 3600)
    return timedelta(seconds=backoff_seconds)


def should_retry(
    task_execution: RayTaskExecution,
    exception_type: str | None = None,
) -> RetryDecision:
    """Determine if a failed task should be retried.

    Args:
        task_execution: The failed task execution.
        exception_type: The type of exception that caused the failure.

    Returns:
        RetryDecision with retry information.
    """
    max_attempts = get_max_attempts()
    attempt_number: int = task_execution.attempt_number  # type: ignore[assignment]

    if attempt_number >= max_attempts:
        return RetryDecision(
            should_retry=False,
            reason=f"Max attempts ({max_attempts}) reached",
        )

    # TODO: Check exception allowlist/denylist
    settings = get_settings()
    denylist = settings.get("RETRY_EXCEPTION_DENYLIST", [])

    if exception_type and exception_type in denylist:
        return RetryDecision(
            should_retry=False,
            reason=f"Exception type '{exception_type}' is in denylist",
        )

    next_attempt = attempt_number + 1
    backoff = calculate_backoff(next_attempt)
    next_attempt_at = datetime.now(timezone.utc) + backoff

    return RetryDecision(
        should_retry=True,
        next_attempt_at=next_attempt_at,
    )
