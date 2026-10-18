"""Database-scoped lease authority and fenced, quiescent policy changes.

The shared guard precedes lease and task locks and follows their existing
authoritative transaction. Existing cancellation and storage effects retain that
ownership fence; this module does not move them or widen transaction boundaries.
PostgreSQL SHARE permits peers while an exclusive NOWAIT transition refuses
contention. Read-only observations never acquire this guard.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.db import DatabaseError, connections, transaction
from django.db.models import BooleanField, F
from django.db.models.expressions import RawSQL
from django.utils import timezone

from django_ray.conf.settings import get_settings

MAX_REVISION = (1 << 63) - 1
REASONS = frozenset(
    {
        "lease_policy_unavailable",
        "lease_policy_invalid",
        "lease_policy_mismatch",
        "lease_policy_changed",
        "lease_policy_busy",
        "lease_policy_transaction_required",
        "lease_policy_transition_invalid",
        "lease_policy_active_leases",
        "lease_policy_active_work",
        "lease_policy_revision_exhausted",
    }
)


class LeasePolicyError(RuntimeError):
    """Fixed, secret-free refusal; never an observation of zero healthy workers."""

    def __init__(self, reason: str = "lease_policy_unavailable") -> None:
        if type(reason) is not str or reason not in REASONS:
            raise ValueError("Unsupported lease policy diagnostic")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class LeasePolicy:
    seconds: int
    revision: int

    @property
    def duration(self) -> timedelta:
        return timedelta(seconds=self.seconds)


def configured_lease_seconds() -> int:
    value = get_settings().get("WORKER_LEASE_SECONDS", 60)
    if type(value) is not int or not 1 <= value <= 86400:
        raise LeasePolicyError("lease_policy_invalid")
    return value


def policy_rows(*, using: str = "default", matching: bool = True, revision: int | None = None):
    """Build a lazy, SQL-validated authority predicate on the selected database."""
    from django_ray.models import TaskWorkerLeasePolicy

    rows = TaskWorkerLeasePolicy.objects.using(using).filter(
        singleton_key=1,
        schema_version=1,
        lease_seconds__gte=1,
        lease_seconds__lte=86400,
        revision__gte=1,
        revision__lte=MAX_REVISION,
    )
    vendor = connections[using].vendor
    if vendor not in {"sqlite", "postgresql"}:
        raise LeasePolicyError("lease_policy_unavailable")
    if vendor == "sqlite":
        rows = rows.alias(
            _lease_policy_types=RawSQL(
                "typeof(singleton_key) = 'integer' AND typeof(schema_version) = 'integer' "
                "AND typeof(lease_seconds) = 'integer' AND typeof(revision) = 'integer'",
                (),
                output_field=BooleanField(),
            )
        ).filter(_lease_policy_types=True)
    if matching:
        rows = rows.filter(lease_seconds=configured_lease_seconds())
    if revision is not None:
        rows = rows.filter(revision=revision)
    return rows


def read_lease_policy(*, using: str = "default", matching: bool = True) -> LeasePolicy:
    """Read at most one bounded scalar row; never initialize or repair authority."""
    try:
        row = (
            policy_rows(using=using, matching=False)
            .values_list("lease_seconds", "revision")
            .first()
        )
    except DatabaseError:
        raise LeasePolicyError() from None
    if row is None:
        raise LeasePolicyError("lease_policy_invalid")
    seconds, revision = row
    if type(seconds) is not int or type(revision) is not int:
        raise LeasePolicyError("lease_policy_invalid")
    if matching and seconds != configured_lease_seconds():
        raise LeasePolicyError("lease_policy_mismatch")
    return LeasePolicy(seconds, revision)


@contextmanager
def lease_policy_observation(*, using: str = "default") -> Generator[LeasePolicy]:
    """Discard a multi-query observation if its authority changed; no write locks."""
    before = read_lease_policy(using=using)
    yield before
    if read_lease_policy(using=using) != before:
        raise LeasePolicyError("lease_policy_changed")


def lock_lease_policy(*, using: str = "default", exclusive: bool = False) -> LeasePolicy:
    """Acquire authority before any lease/task locks inside an existing transaction."""
    from django_ray.models import TaskWorkerLeasePolicy

    connection = connections[using]
    if not connection.in_atomic_block:
        raise LeasePolicyError("lease_policy_transaction_required")
    try:
        if connection.vendor == "postgresql":
            table = connection.ops.quote_name(TaskWorkerLeasePolicy._meta.db_table)
            lock = "UPDATE" if exclusive else "SHARE"
            with connection.cursor() as cursor:
                # NOWAIT bounds contention and SHARE permits independent workers.
                cursor.execute(
                    f"SELECT singleton_key FROM {table} WHERE singleton_key = %s FOR {lock} NOWAIT",
                    [1],
                )
                if cursor.fetchone() is None:
                    raise LeasePolicyError("lease_policy_invalid")
        elif connection.vendor == "sqlite":
            # SQLite has no row locks. Fence before reading policy or freshness.
            updated = (
                TaskWorkerLeasePolicy.objects.using(using)
                .filter(singleton_key=1)
                .update(revision=F("revision"))
            )
            if updated != 1:
                raise LeasePolicyError("lease_policy_invalid")
        else:
            raise LeasePolicyError()
        return read_lease_policy(using=using, matching=not exclusive)
    except DatabaseError:
        raise LeasePolicyError("lease_policy_busy") from None


@contextmanager
def lease_policy_guard(*, using: str = "default") -> Generator[LeasePolicy]:
    """Guard only a bounded database mutation, never network or user code."""
    with transaction.atomic(using=using):
        yield lock_lease_policy(using=using)


def set_lease_policy(
    *,
    seconds: int,
    expected_seconds: int,
    expected_revision: int,
    all_lease_users_stopped: bool,
    using: str = "default",
) -> LeasePolicy:
    """CAS a policy only after explicit and database-observed quiescence.

    The assertion is operational: SQL cannot prove that old binaries or remote
    side effects have stopped. Active rows, even stale ones, are never retired.
    """
    from django_ray.models import (
        RayTaskExecution,
        TaskState,
        TaskWorkerLease,
        TaskWorkerLeasePolicy,
    )

    if (
        type(seconds) is not int
        or not 1 <= seconds <= 86400
        or type(expected_seconds) is not int
        or not 1 <= expected_seconds <= 86400
        or type(expected_revision) is not int
        or not 1 <= expected_revision <= MAX_REVISION
        or all_lease_users_stopped is not True
    ):
        raise LeasePolicyError("lease_policy_transition_invalid")
    heartbeat = get_settings().get("WORKER_HEARTBEAT_SECONDS", 15)
    if type(heartbeat) is not int or not 1 <= heartbeat < seconds:
        raise LeasePolicyError("lease_policy_invalid")
    connection = connections[using]
    if connection.in_atomic_block or not connection.get_autocommit():
        raise LeasePolicyError("lease_policy_transaction_required")

    # A transition owns its connection's short lock budget, restoring SQLite's
    # prior setting on every path. PostgreSQL row locking above uses NOWAIT.
    prior_busy = None
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout")
            prior_busy = cursor.fetchone()[0]
            cursor.execute("PRAGMA busy_timeout = 250")
    try:
        with transaction.atomic(using=using, durable=True):
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '250ms'")
                    cursor.execute("SET LOCAL statement_timeout = '1000ms'")
            policy = lock_lease_policy(using=using, exclusive=True)
            if policy != LeasePolicy(expected_seconds, expected_revision):
                raise LeasePolicyError("lease_policy_changed")
            if TaskWorkerLease.objects.using(using).filter(is_active=True).exists():
                raise LeasePolicyError("lease_policy_active_leases")
            if (
                RayTaskExecution.objects.using(using)
                .filter(state__in=(TaskState.RUNNING, TaskState.CANCELLING))
                .exists()
            ):
                raise LeasePolicyError("lease_policy_active_work")
            if policy.seconds == seconds:
                return policy
            if policy.revision == MAX_REVISION:
                raise LeasePolicyError("lease_policy_revision_exhausted")
            count = (
                TaskWorkerLeasePolicy.objects.using(using)
                .filter(singleton_key=1, lease_seconds=expected_seconds, revision=expected_revision)
                .update(
                    lease_seconds=seconds, revision=F("revision") + 1, updated_at=timezone.now()
                )
            )
            if count != 1:
                raise LeasePolicyError("lease_policy_changed")
            return LeasePolicy(seconds, expected_revision + 1)
    except DatabaseError:
        raise LeasePolicyError("lease_policy_unavailable") from None
    finally:
        if prior_busy is not None:
            with connection.cursor() as cursor:
                cursor.execute(f"PRAGMA busy_timeout = {int(prior_busy)}")
