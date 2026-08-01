"""Worker leasing for distributed coordination."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django_ray.conf.settings import get_settings

if TYPE_CHECKING:
    from django_ray.models import TaskWorkerLease


@dataclass(frozen=True, slots=True)
class WorkerLeaseIdentity:
    """Immutable database identity for one worker lease acquisition.

    ``worker_id`` selects the lease row, while the remaining fields fence an
    individual process from a row that was deleted and recreated with the same
    identifier. A worker must retain this exact snapshot for live heartbeats
    and release; expired or inactive ownership cannot be reactivated.
    """

    worker_id: str
    hostname: str
    pid: int
    started_at: datetime

    def database_filters(self) -> dict[str, object]:
        """Return the complete ownership fence for ORM updates."""
        return {
            "worker_id": self.worker_id,
            "hostname": self.hostname,
            "pid": self.pid,
            "started_at": self.started_at,
        }


def generate_worker_id() -> str:
    """Generate a unique worker ID."""
    return str(uuid.uuid4())


def is_worker_id_primary_key_collision(error: Exception) -> bool:
    """Return whether a supported database reported this lease PK collision.

    Allocation retries are restricted to the exact primary-key violation. An
    unrelated integrity failure must remain visible even when a row happens to
    exist for the candidate worker ID.
    """
    from django.db import connection

    from django_ray.models import TaskWorkerLease

    cause = error.__cause__
    table_name = TaskWorkerLease._meta.db_table
    if connection.vendor == "sqlite":
        import sqlite3

        return bool(
            cause is not None
            and getattr(cause, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY
            and str(cause) == f"UNIQUE constraint failed: {table_name}.worker_id"
        )
    if connection.vendor == "postgresql":
        diagnostic = getattr(cause, "diag", None)
        return bool(
            diagnostic is not None
            and getattr(diagnostic, "constraint_name", None) == f"{table_name}_pkey"
        )
    return False


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
    from django.db import connection, transaction

    from django_ray.models import TaskWorkerLease

    now = datetime.now(UTC)
    duration = get_lease_duration()
    cutoff = now - duration

    stale_filters = {
        "is_active": True,
        "last_heartbeat_at__lt": cutoff,
    }
    if connection.features.has_select_for_update:
        # Take the same deterministic lease-lock order as task takeover. Skip
        # rows currently protecting an ownership decision and retry them on the
        # next cleanup pass.
        with transaction.atomic():
            stale_worker_ids = list(
                TaskWorkerLease.objects.select_for_update(
                    skip_locked=connection.features.has_select_for_update_skip_locked
                )
                .filter(**stale_filters)
                .order_by("worker_id")
                .values_list("worker_id", flat=True)
            )
            updated_count = TaskWorkerLease.objects.filter(
                worker_id__in=stale_worker_ids,
                **stale_filters,
            ).update(
                is_active=False,
                stopped_at=now,
            )
    else:
        updated_count = TaskWorkerLease.objects.filter(**stale_filters).update(
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


def release_lease(identity: WorkerLeaseIdentity) -> bool:
    """Release a worker lease (called during graceful shutdown).

    Marks the lease as inactive rather than deleting it.

    Args:
        identity: The immutable identity captured when this process acquired
            the lease. A row recreated under the same worker ID is not released.

    Returns:
        True if the lease was released.
    """
    from django_ray.models import TaskWorkerLease

    now = datetime.now(UTC)

    updated_count = TaskWorkerLease.objects.filter(
        **identity.database_filters(),
        is_active=True,
    ).update(
        is_active=False,
        stopped_at=now,
    )
    return updated_count > 0
