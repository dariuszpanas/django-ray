"""Worker leasing for distributed coordination."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
import uuid

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
    """Check if a worker lease has expired.

    Args:
        lease: The lease to check.

    Returns:
        True if the lease has expired.
    """
    now = datetime.now(timezone.utc)
    duration = get_lease_duration()
    last_heartbeat: datetime = lease.last_heartbeat_at  # type: ignore[assignment]
    return (now - last_heartbeat) > duration


def cleanup_expired_leases() -> int:
    """Clean up expired worker leases from the database.

    This should be called periodically by workers or a management command
    to remove stale lease records from workers that have crashed.

    Returns:
        Number of leases deleted.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(timezone.utc)
    duration = get_lease_duration()
    cutoff = now - duration

    # Delete leases that haven't had a heartbeat within the lease duration
    deleted_count, _ = TaskWorkerLease.objects.filter(
        last_heartbeat_at__lt=cutoff
    ).delete()

    return deleted_count


def get_active_worker_count() -> int:
    """Get the count of currently active workers.

    Returns:
        Number of workers with non-expired leases.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(timezone.utc)
    duration = get_lease_duration()
    cutoff = now - duration

    return TaskWorkerLease.objects.filter(
        last_heartbeat_at__gte=cutoff
    ).count()


def get_active_workers() -> list[TaskWorkerLease]:
    """Get all currently active workers.

    Returns:
        List of workers with non-expired leases.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(timezone.utc)
    duration = get_lease_duration()
    cutoff = now - duration

    return list(TaskWorkerLease.objects.filter(
        last_heartbeat_at__gte=cutoff
    ))


def release_lease(worker_id: str) -> bool:
    """Release a worker lease (called during graceful shutdown).

    Args:
        worker_id: The worker ID to release.

    Returns:
        True if the lease was released.
    """
    from django_ray.models import TaskWorkerLease

    try:
        deleted_count, _ = TaskWorkerLease.objects.filter(
            worker_id=worker_id
        ).delete()
        return deleted_count > 0
    except Exception:
        return False

