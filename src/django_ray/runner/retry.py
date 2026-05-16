"""Retry policy implementation for failed tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


def _normalize_exception_name(exception_name: str) -> set[str]:
    """Return comparable variants of an exception class path/name."""
    name = exception_name.strip()
    if not name:
        return set()

    short_name = name.rsplit(".", 1)[-1]
    variants = {name, short_name}

    # Common case where runtime emits fully qualified builtins but users configure short names.
    if "." not in name:
        variants.add(f"builtins.{name}")

    return variants


def _match_denylist_entry(exception_type: str, denylist: list[object]) -> str | None:
    """Return the matched denylist entry, if any."""
    exception_variants = _normalize_exception_name(exception_type)

    for entry in denylist:
        if not isinstance(entry, str):
            continue
        if exception_variants & _normalize_exception_name(entry):
            return entry

    return None


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

    settings = get_settings()
    denylist = settings.get("RETRY_EXCEPTION_DENYLIST", [])

    matched_entry = (
        _match_denylist_entry(exception_type, denylist) if exception_type is not None else None
    )
    if matched_entry is not None:
        return RetryDecision(
            should_retry=False,
            reason=f"Exception type '{exception_type}' matched denylist entry '{matched_entry}'",
        )

    next_attempt = attempt_number + 1
    backoff = calculate_backoff(next_attempt)
    next_attempt_at = datetime.now(UTC) + backoff

    return RetryDecision(
        should_retry=True,
        next_attempt_at=next_attempt_at,
    )
