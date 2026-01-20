"""Worker leasing for distributed coordination."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django_ray.conf.settings import get_settings

if TYPE_CHECKING:
    from django_ray.models import TaskWorkerLease


def generate_worker_id() -> str:
    """Generate a unique worker ID."""
    return str(uuid.uuid4())


def get_lease_duration() -> timedelta:
    """Get the worker lease duration."""
    settings = get_settings()
    seconds = settings.get("WORKER_LEASE_SECONDS", 60)
    return timedelta(seconds=seconds)


def get_heartbeat_interval() -> timedelta:
    """Get the heartbeat interval for workers."""
    settings = get_settings()
    seconds = settings.get("WORKER_HEARTBEAT_SECONDS", 15)
    return timedelta(seconds=seconds)


def is_lease_expired(lease: TaskWorkerLease) -> bool:
    """Check if a worker lease has expired based on heartbeat.

    Args:
        lease: The lease to check.

    Returns:
        True if the lease has expired (no heartbeat within lease duration).
    """
    # If already marked inactive, it's expired
    if not lease.is_active:
        return True

    now = datetime.now(UTC)
    duration = get_lease_duration()
    last_heartbeat: datetime = lease.last_heartbeat_at  # type: ignore[assignment]
    return (now - last_heartbeat) > duration


def mark_expired_leases_inactive() -> int:
    """Mark expired worker leases as inactive.

    This should be called periodically by workers or a management command
    to mark stale lease records from workers that have crashed.

    Returns:
        Number of leases marked inactive.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(UTC)
    duration = get_lease_duration()
    cutoff = now - duration

    # Mark leases inactive that haven't had a heartbeat within the lease duration
    updated_count = TaskWorkerLease.objects.filter(
        is_active=True,
        last_heartbeat_at__lt=cutoff,
    ).update(
        is_active=False,
        stopped_at=now,
    )

    return updated_count


# Keep old name as alias for backward compatibility
def cleanup_expired_leases() -> int:
    """Mark expired worker leases as inactive.

    This is an alias for mark_expired_leases_inactive() for backward compatibility.

    Returns:
        Number of leases marked inactive.
    """
    return mark_expired_leases_inactive()


def get_active_worker_count() -> int:
    """Get the count of currently active workers.

    Returns:
        Number of workers with active leases and recent heartbeats.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(UTC)
    duration = get_lease_duration()
    cutoff = now - duration

    return TaskWorkerLease.objects.filter(
        is_active=True,
        last_heartbeat_at__gte=cutoff,
    ).count()


def get_active_workers() -> list[TaskWorkerLease]:
    """Get all currently active workers.

    Returns:
        List of workers with active leases and recent heartbeats.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(UTC)
    duration = get_lease_duration()
    cutoff = now - duration

    return list(
        TaskWorkerLease.objects.filter(
            is_active=True,
            last_heartbeat_at__gte=cutoff,
        )
    )


def release_lease(worker_id: str) -> bool:
    """Release a worker lease (called during graceful shutdown).

    Marks the lease as inactive rather than deleting it.

    Args:
        worker_id: The worker ID to release.

    Returns:
        True if the lease was released.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(UTC)

    try:
        updated_count = TaskWorkerLease.objects.filter(
            worker_id=worker_id,
            is_active=True,
        ).update(
            is_active=False,
            stopped_at=now,
        )
        return updated_count > 0
    except Exception:
        return False
