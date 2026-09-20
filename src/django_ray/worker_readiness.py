"""Bounded, read-only readiness for one Django task-manager worker lease."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connections
from django.db.models import IntegerField, Value
from django.utils import timezone
from django.utils.connection import ConnectionDoesNotExist

from django_ray.conf.settings import get_settings, validate_settings


@dataclass(frozen=True, slots=True)
class WorkerLeaseReadiness:
    """Fixed, secret-free observation; never execution or capacity authority."""

    status: Literal["ready", "not_ready", "error"]
    reason: str

    @property
    def exit_code(self) -> int:
        """Return 0 for ready, 1 for not ready, and 2 for invalid observations."""
        return {"ready": 0, "not_ready": 1, "error": 2}[self.status]

    def as_dict(self) -> dict[str, int | str]:
        """Return the closed JSON report without echoing coordinates or rows."""
        return {
            "schema_version": 1,
            "scope": "worker_lease",
            "status": self.status,
            "reason": self.reason,
        }


def _valid_coordinate(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value == value.strip()
        and value.isprintable()
    )


def check_worker_lease_readiness(
    *,
    queue: str,
    hostname: str,
    using: str = "default",
    worker_id: str | None = None,
) -> WorkerLeaseReadiness:
    """Observe exactly one active, fresh lease matching the supplied coordinates.

    One captured time defines freshness, including the lease-duration boundary.
    At most two constant values cross the database boundary. No lease is updated.
    An explicit worker ID narrows the selection; otherwise duplicate live leases
    at the same queue and hostname are ambiguous. No Ray connection is attempted.
    """
    if not (
        _valid_coordinate(using, 128)
        and _valid_coordinate(queue, 100)
        and _valid_coordinate(hostname, 255)
        and (worker_id is None or _valid_coordinate(worker_id, 255))
    ):
        return WorkerLeaseReadiness("error", "invalid_coordinates")
    try:
        connection = connections[using]
    except (ConnectionDoesNotExist, ImproperlyConfigured):
        return WorkerLeaseReadiness("error", "invalid_database")
    try:
        config = get_settings()
        validate_settings(config)
        duration = timedelta(seconds=config["WORKER_LEASE_SECONDS"])
    except (ImproperlyConfigured, TypeError, ValueError):
        return WorkerLeaseReadiness("error", "invalid_settings")

    from django_ray.models import TaskWorkerLease

    try:
        if connection.in_atomic_block or not connection.get_autocommit():
            return WorkerLeaseReadiness("error", "transaction_active")
        observed_at = timezone.now()
        leases = TaskWorkerLease.objects.using(using).filter(
            queue_name=queue,
            hostname=hostname,
            is_active=True,
            stopped_at__isnull=True,
            last_heartbeat_at__gte=observed_at - duration,
            last_heartbeat_at__lte=observed_at,
            started_at__lte=observed_at,
        )
        if worker_id is not None:
            leases = leases.filter(worker_id=worker_id)
        matches = list(
            leases.order_by().values_list(Value(1, output_field=IntegerField()), flat=True)[:2]
        )
    except DatabaseError:
        return WorkerLeaseReadiness("error", "database_unavailable")
    if not matches:
        return WorkerLeaseReadiness("not_ready", "no_live_lease")
    if len(matches) != 1:
        return WorkerLeaseReadiness("not_ready", "ambiguous_lease")
    return WorkerLeaseReadiness("ready", "live_lease")
