"""Worker leasing for distributed coordination."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
import uuid

from django_ray.conf.settings import get_settings

if TYPE_CHECKING:
    from django_ray.models import RayWorkerLease


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


def is_lease_expired(lease: RayWorkerLease) -> bool:
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
