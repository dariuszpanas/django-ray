"""Private revisioned admission pauses; no remote cleanup or drain inference.

Take the shared barrier before admission's input registry, lease, target and
execution locks. Policy writers own the exclusive barrier and never lock tasks
or leases. A pause affects new admission/generations, not owned completion or
cancellation. Authentication belongs to the future operator boundary: its
explicit authorization acknowledgment cannot be inferred from an actor string.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.models import Exists, F, OuterRef

from django_ray.models import (
    RayMaintenanceAudit,
    RayMaintenancePolicy,
    RayMaintenanceScope,
    RayTarget,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskQuarantine,
    RayWorkerRetirement,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)

_MAX = (1 << 63) - 1
_NAMESPACE = 1_684_697_721
_LOCK_KEY = 368
_MAX_SCOPE_BYTES = 131072
_DOMAIN = b"django-ray:maintenance-scopes:v1\x00"


class MaintenanceReason(StrEnum):
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    PAUSED = "paused"
    BARRIER_REQUIRED = "barrier_required"
    TRANSACTION_REQUIRED = "transaction_required"
    TRANSACTION_OPEN = "transaction_open"
    UNAUTHORIZED = "unauthorized"
    REVISION_CHANGED = "revision_changed"
    CLOCK_REGRESSION = "clock_regression"
    PERSISTENCE_REFUSED = "persistence_refused"
    RETIRING = "retiring"
    QUARANTINED = "quarantined"
    IDENTITY_CHANGED = "identity_changed"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    OWNERSHIP_REMAINS = "ownership_remains"


class MaintenanceAdmissionError(RuntimeError):
    def __init__(self, reason: MaintenanceReason) -> None:
        self.reason = reason
        super().__init__(f"Maintenance admission refused: {reason.value}")


@dataclass(frozen=True, slots=True)
class MaintenanceScope:
    kind: str
    queue_name: str | None = None
    protocol_version: int | None = None
    target_id: str | None = None
    pause_enqueues: bool = False
    pause_claims: bool = True


@dataclass(frozen=True, slots=True)
class MaintenancePolicySnapshot:
    revision: int
    pause_enqueues: bool
    pause_claims: bool
    scopes: tuple[MaintenanceScope, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MaintenancePolicyChange:
    changed: bool
    dry_run: bool
    previous_revision: int
    policy: MaintenancePolicySnapshot


@dataclass(frozen=True, slots=True, eq=False)
class MaintenanceAdmissionBarrier:
    """Opaque active-context token, never a persisted or transferable authority."""

    using: str
    _connection: Any = field(repr=False)
    _outer: Any = field(repr=False)
    _anchor: Any = field(repr=False)


_BARRIERS: ContextVar[tuple[MaintenanceAdmissionBarrier, ...]] = ContextVar(
    "django_ray_maintenance_barriers", default=()
)


def _reject(reason: MaintenanceReason) -> None:
    raise MaintenanceAdmissionError(reason)


def _database(using: str):
    if type(using) is not str:
        _reject(MaintenanceReason.INVALID)
    connection = connections[using]
    if connection.vendor not in {"sqlite", "postgresql"}:
        _reject(MaintenanceReason.UNAVAILABLE)
    return connection


def _integer(value, maximum=_MAX):
    if type(value) is not int or not 1 <= value <= maximum:
        _reject(MaintenanceReason.INVALID)


def _queue(value):
    if (
        type(value) is not str
        or not value.strip()
        or "\x00" in value
        or len(value) > 100
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        _reject(MaintenanceReason.INVALID)


def _text(value, *, maximum=128):
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        _reject(MaintenanceReason.INVALID)


def _wall(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        _reject(MaintenanceReason.INVALID)
    return value.astimezone(UTC)


def _clock():
    return datetime.now(UTC)


def _scopes(values):
    if type(values) is not tuple or len(values) > 64:
        _reject(MaintenanceReason.INVALID)
    keys = set()
    for value in values:
        if (
            type(value) is not MaintenanceScope
            or type(value.kind) is not str
            or type(value.pause_enqueues) is not bool
            or type(value.pause_claims) is not bool
            or not (value.pause_enqueues or value.pause_claims)
        ):
            _reject(MaintenanceReason.INVALID)
        if value.kind == "queue":
            _queue(value.queue_name)
            if value.protocol_version is not None or value.target_id is not None:
                _reject(MaintenanceReason.INVALID)
            key = (value.kind, value.queue_name)
        elif value.kind == "protocol":
            _integer(value.protocol_version, 32767)
            if value.queue_name is not None or value.target_id is not None:
                _reject(MaintenanceReason.INVALID)
            key = (value.kind, value.protocol_version)
        elif value.kind == "target":
            _text(value.target_id, maximum=128)
            if (
                value.queue_name is not None
                or value.protocol_version is not None
                or value.pause_enqueues
                or not value.pause_claims
            ):
                _reject(MaintenanceReason.INVALID)
            key = (value.kind, value.target_id)
        else:
            _reject(MaintenanceReason.INVALID)
        if key in keys:
            _reject(MaintenanceReason.INVALID)
        keys.add(key)
    return tuple(
        sorted(
            values,
            key=lambda scope: (
                scope.kind,
                str(scope.queue_name or scope.protocol_version or scope.target_id),
            ),
        )
    )


def _serialized(scopes):
    raw = json.dumps(
        [asdict(scope) for scope in scopes],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if len(raw.encode("ascii")) > _MAX_SCOPE_BYTES:
        _reject(MaintenanceReason.INVALID)
    return raw, hashlib.sha256(_DOMAIN + raw.encode("ascii")).hexdigest()


def read_maintenance_policy(*, using=DEFAULT_DB_ALIAS) -> MaintenancePolicySnapshot:
    """Read a bounded coherent snapshot; a preflight does not retain a fence."""
    _database(using)
    try:
        policy = RayMaintenancePolicy.objects.using(using).get(singleton_key=1)
        audit = RayMaintenanceAudit.objects.using(using).get(revision=policy.revision)
        if policy.schema_version != 1:
            _reject(MaintenanceReason.UNAVAILABLE)
        _integer(policy.revision)
        rows = list(
            RayMaintenanceScope.objects.using(using)
            .filter(audit_id=policy.revision)
            .order_by("position")[:65]
        )
        scopes = tuple(
            MaintenanceScope(
                row.kind,
                row.queue_name,
                row.protocol_version,
                row.target_id,
                row.pause_enqueues,
                row.pause_claims,
            )
            for row in rows
        )
        normalized = _scopes(scopes)
        raw, digest = _serialized(normalized)
        if (
            normalized != scopes
            or [row.position for row in rows] != list(range(1, len(rows) + 1))
            or audit.scope_count != len(scopes)
            or audit.scopes_json != raw
            or audit.scopes_digest != digest
            or audit.previous_revision != policy.revision - 1
            or audit.pause_enqueues != policy.pause_enqueues
            or audit.pause_claims != policy.pause_claims
            or audit.created_at != policy.updated_at
        ):
            _reject(MaintenanceReason.UNAVAILABLE)
        return MaintenancePolicySnapshot(
            policy.revision,
            policy.pause_enqueues,
            policy.pause_claims,
            scopes,
            _wall(policy.updated_at),
        )
    except (RayMaintenancePolicy.DoesNotExist, RayMaintenanceAudit.DoesNotExist, DatabaseError):
        raise MaintenanceAdmissionError(MaintenanceReason.UNAVAILABLE) from None


def _lock(*, using, exclusive):
    connection = _database(using)
    if connection.vendor == "postgresql":
        function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            if cursor.fetchone()[0] != "read committed":
                _reject(MaintenanceReason.UNAVAILABLE)
            cursor.execute(f"SELECT {function}(%s, %s)", [_NAMESPACE, _LOCK_KEY])
    else:
        changed = (
            RayMaintenancePolicy.objects.using(using)
            .filter(singleton_key=1)
            .update(revision=F("revision"))
        )
        if changed != 1:
            _reject(MaintenanceReason.UNAVAILABLE)


@contextmanager
def maintenance_admission_barrier(*, using=DEFAULT_DB_ALIAS):
    """Retain admission policy through commit; acquire before admission row locks.

    Context tokens are valid only inside this context and the same outer atomic
    block. The database lock itself remains held through the outer transaction.
    Callers may nest inside application transactions before input registration.
    """
    connection = _database(using)
    if not connection.in_atomic_block or not connection.atomic_blocks:
        _reject(MaintenanceReason.TRANSACTION_REQUIRED)
    _lock(using=using, exclusive=False)
    read_maintenance_policy(using=using)
    barrier = MaintenanceAdmissionBarrier(
        using, connection, connection.atomic_blocks[0], connection.atomic_blocks[-1]
    )
    marker = _BARRIERS.set((*_BARRIERS.get(), barrier))
    try:
        yield barrier
    finally:
        _BARRIERS.reset(marker)


def require_maintenance_admission_barrier(barrier=None, *, using=DEFAULT_DB_ALIAS):
    connection = _database(using)
    candidates = _BARRIERS.get()
    if barrier is None:
        barrier = next((item for item in reversed(candidates) if item.using == using), None)
    if (
        type(barrier) is not MaintenanceAdmissionBarrier
        or not any(item is barrier for item in candidates)
        or barrier._connection is not connection
        or barrier.using != using
        or not connection.in_atomic_block
        or not connection.atomic_blocks
        or barrier._outer is not connection.atomic_blocks[0]
        or not any(block is barrier._anchor for block in connection.atomic_blocks)
        or connection.needs_rollback
    ):
        _reject(MaintenanceReason.BARRIER_REQUIRED)
    return barrier


def check_maintenance_admission(
    queue_name: str,
    protocol_version: int,
    *,
    target_id: str | None = None,
    operation: str,
    barrier: MaintenanceAdmissionBarrier | None = None,
    preflight: bool = False,
    using=DEFAULT_DB_ALIAS,
) -> MaintenancePolicySnapshot:
    """Reject exact scope pauses; preflight=True is expressly nonauthoritative."""
    if type(preflight) is not bool:
        _reject(MaintenanceReason.INVALID)
    if not preflight:
        require_maintenance_admission_barrier(barrier, using=using)
    policy = read_maintenance_policy(using=using)
    if not maintenance_admission_allowed(
        policy, queue_name, protocol_version, target_id=target_id, operation=operation
    ):
        _reject(MaintenanceReason.PAUSED)
    return policy


def maintenance_admission_allowed(
    policy: MaintenancePolicySnapshot,
    queue_name: str,
    protocol_version: int,
    *,
    target_id: str | None = None,
    operation: str,
) -> bool:
    """Pure finite selection predicate; a snapshot alone retains no DB fence."""
    if type(policy) is not MaintenancePolicySnapshot:
        _reject(MaintenanceReason.INVALID)
    _queue(queue_name)
    _integer(protocol_version, 32767)
    if target_id is not None:
        _text(target_id, maximum=128)
    if type(operation) is not str or operation not in {"enqueue", "claim"}:
        _reject(MaintenanceReason.INVALID)
    attribute = "pause_enqueues" if operation == "enqueue" else "pause_claims"
    if getattr(policy, attribute):
        return False
    for scope in policy.scopes:
        if getattr(scope, attribute) and (
            scope.kind == "queue"
            and scope.queue_name == queue_name
            or scope.kind == "protocol"
            and scope.protocol_version == protocol_version
            or scope.kind == "target"
            and scope.target_id == target_id
        ):
            return False
    return True


def replace_maintenance_policy(
    scopes: tuple[MaintenanceScope, ...],
    *,
    pause_enqueues: bool,
    pause_claims: bool,
    expected_revision: int,
    actor: str,
    reason: str,
    authorized: bool = False,
    dry_run: bool = False,
    using=DEFAULT_DB_ALIAS,
) -> MaintenancePolicyChange:
    """Publish one reviewed scope set with CAS and audit, without task locks.

    authorized=True acknowledges a permission check by the trusted caller; it
    must never be copied directly from an untrusted request. Dry runs require
    the same permission and revision but create no policy, scope or audit rows.
    """
    if authorized is not True:
        _reject(MaintenanceReason.UNAUTHORIZED)
    if any(type(value) is not bool for value in (pause_enqueues, pause_claims, dry_run)):
        _reject(MaintenanceReason.INVALID)
    _integer(expected_revision)
    _text(actor)
    _text(reason)
    scopes = _scopes(scopes)
    raw, digest = _serialized(scopes)
    connection = _database(using)
    if connection.in_atomic_block or not connection.get_autocommit():
        _reject(MaintenanceReason.TRANSACTION_OPEN)
    try:
        with transaction.atomic(using=using, durable=True):
            _lock(using=using, exclusive=True)
            current = read_maintenance_policy(using=using)
            if current.revision != expected_revision:
                _reject(MaintenanceReason.REVISION_CHANGED)
            now = _wall(_clock())
            if now < current.updated_at:
                _reject(MaintenanceReason.CLOCK_REGRESSION)
            targets = {scope.target_id for scope in scopes if scope.kind == "target"}
            if RayTarget.objects.using(using).filter(pk__in=targets).count() != len(targets):
                _reject(MaintenanceReason.INVALID)
            changed = (current.pause_enqueues, current.pause_claims, current.scopes) != (
                pause_enqueues,
                pause_claims,
                scopes,
            )
            if not changed:
                return MaintenancePolicyChange(False, dry_run, current.revision, current)
            if current.revision == _MAX:
                _reject(MaintenanceReason.INVALID)
            proposed = MaintenancePolicySnapshot(
                current.revision + 1, pause_enqueues, pause_claims, scopes, now
            )
            if dry_run:
                return MaintenancePolicyChange(True, True, current.revision, proposed)
            RayMaintenanceAudit.objects.using(using).create(
                revision=proposed.revision,
                previous_revision=current.revision,
                pause_enqueues=pause_enqueues,
                pause_claims=pause_claims,
                scope_count=len(scopes),
                scopes_json=raw,
                scopes_digest=digest,
                actor=actor,
                reason=reason,
                created_at=now,
            )
            RayMaintenanceScope.objects.using(using).bulk_create(
                [
                    RayMaintenanceScope(audit_id=proposed.revision, position=index, **asdict(scope))
                    for index, scope in enumerate(scopes, 1)
                ]
            )
            updated = (
                RayMaintenancePolicy.objects.using(using)
                .filter(singleton_key=1, revision=current.revision)
                .update(
                    revision=proposed.revision,
                    pause_enqueues=pause_enqueues,
                    pause_claims=pause_claims,
                    updated_at=now,
                )
            )
            if updated != 1:
                _reject(MaintenanceReason.REVISION_CHANGED)
            return MaintenancePolicyChange(
                True, False, current.revision, read_maintenance_policy(using=using)
            )
    except DatabaseError:
        raise MaintenanceAdmissionError(MaintenanceReason.PERSISTENCE_REFUSED) from None


@dataclass(frozen=True, slots=True)
class MaintenanceControlChange:
    """One exact-identity decision; its data do not authenticate remote cleanup."""

    changed: bool
    dry_run: bool
    previous_revision: int
    revision: int
    state: str


def _control_identity(identity):
    from django_ray.target.capabilities import RayWorkerTargetCapabilityError, _identity

    try:
        return _identity(identity)
    except RayWorkerTargetCapabilityError:
        raise MaintenanceAdmissionError(MaintenanceReason.INVALID) from None


def _execution_identity(identity):
    from django_ray.execution_codec import ExecutionIdentity, is_valid_execution_identity

    if type(identity) is not ExecutionIdentity or not is_valid_execution_identity(identity):
        _reject(MaintenanceReason.INVALID)
    return identity


def _task_identity(task):
    from django_ray.execution_codec import ExecutionIdentity

    return _execution_identity(
        ExecutionIdentity(task.pk, task.task_id, task.attempt_number, task.execution_generation)
    )


def _retirements(identity, *, using):
    return RayWorkerRetirement.objects.using(using).filter(**identity.database_filters())


def worker_retirement_requested(identity, *, using=DEFAULT_DB_ALIAS) -> bool:
    """Advisory pre-LIMIT check; a reused worker ID never inherits retirement."""
    identity = _control_identity(identity)
    _database(using)
    return _retirements(identity, using=using).exists()


def check_worker_retirement_admission(identity, *, barrier=None, using=DEFAULT_DB_ALIAS):
    """Check after locking the exact lease; existing ownership must not call this.

    Acquire the shared maintenance barrier before the lease/target/task locks.
    Retirement prohibits new claims and destination adoption, not heartbeats,
    owned completion, cancellation, or cleanup of already retained operations.
    """
    require_maintenance_admission_barrier(barrier, using=using)
    if worker_retirement_requested(identity, using=using):
        _reject(MaintenanceReason.RETIRING)


def task_quarantine_blocked_expression(*, using=DEFAULT_DB_ALIAS):
    """Bounded indexed candidate exclusion, without expanding policy scope lists."""
    later = RayTaskQuarantine.objects.using(using).filter(
        task_execution_pk=OuterRef("task_execution_pk"), revision__gt=OuterRef("revision")
    )
    active = (
        RayTaskQuarantine.objects.using(using)
        .filter(task_execution_pk=OuterRef("pk"), state="QUARANTINED")
        .alias(_has_later=Exists(later))
        .filter(_has_later=False)
    )
    return Exists(active)


def task_quarantine_retry_allowed(current_task, *, barrier=None, using=DEFAULT_DB_ALIAS) -> bool:
    """Reread after the caller locks its current task; never mutate its state.

    The same predicate fences new claim generations. Existing same-generation
    completion/cancel remains valid; a failed completion must become terminal
    when this returns False, preserving its authentic result and original claim.
    """
    require_maintenance_admission_barrier(barrier, using=using)
    identity = _task_identity(current_task)
    current = (
        RayTaskQuarantine.objects.using(using)
        .filter(task_execution_pk=identity.task_execution_pk)
        .order_by("-revision")
        .first()
    )
    return current is None or current.state == "RELEASED"


@contextmanager
def _control_operation(*, expected_revision, actor, reason, authorized, dry_run, using):
    if authorized is not True:
        _reject(MaintenanceReason.UNAUTHORIZED)
    if (
        type(dry_run) is not bool
        or type(expected_revision) is not int
        or not 0 <= expected_revision < _MAX
    ):
        _reject(MaintenanceReason.INVALID)
    _text(actor)
    _text(reason)
    connection = _database(using)
    if connection.in_atomic_block or not connection.get_autocommit():
        _reject(MaintenanceReason.TRANSACTION_OPEN)
    try:
        with transaction.atomic(using=using, durable=True):
            with maintenance_admission_barrier(using=using):
                yield
    except DatabaseError:
        raise MaintenanceAdmissionError(MaintenanceReason.PERSISTENCE_REFUSED) from None


def _control_clock(previous):
    now = _wall(_clock())
    if now < previous:
        _reject(MaintenanceReason.CLOCK_REGRESSION)
    return now


def _locked_retirement(identity, expected_revision, observed, *, using):
    lease = (
        TaskWorkerLease.objects.using(using)
        .select_for_update()
        .filter(**identity.database_filters())
        .first()
    )
    if lease is None:
        _reject(MaintenanceReason.IDENTITY_CHANGED)
    current = _retirements(identity, using=using).order_by("-revision").first()
    revision = current.revision if current else 0
    if revision != expected_revision:
        _reject(MaintenanceReason.REVISION_CHANGED)
    now = _control_clock(
        max(observed, lease.started_at, current.created_at if current else observed)
    )
    return lease, current, now


def request_worker_retirement(
    identity,
    *,
    expected_revision,
    actor,
    reason,
    authorized=False,
    dry_run=False,
    using=DEFAULT_DB_ALIAS,
) -> MaintenanceControlChange:
    """Stop this exact incarnation's new ownership without expiring its live lease."""
    identity = _control_identity(identity)
    observed = _wall(_clock())
    with _control_operation(
        expected_revision=expected_revision,
        actor=actor,
        reason=reason,
        authorized=authorized,
        dry_run=dry_run,
        using=using,
    ):
        _lease, current, now = _locked_retirement(
            identity, expected_revision, observed, using=using
        )
        if current is not None:
            return MaintenanceControlChange(
                False, dry_run, current.revision, current.revision, current.state
            )
        if not dry_run:
            RayWorkerRetirement.objects.using(using).create(
                **identity.database_filters(),
                revision=1,
                state="REQUESTED",
                actor=actor,
                reason=reason,
                created_at=now,
            )
        return MaintenanceControlChange(True, dry_run, 0, 1, "REQUESTED")


def complete_worker_retirement(
    identity,
    *,
    expected_revision,
    independently_confirmed_cleanup,
    cleanup_evidence_digest,
    cleanup_confirmed_at,
    actor,
    reason,
    authorized=False,
    dry_run=False,
    using=DEFAULT_DB_ALIAS,
) -> MaintenanceControlChange:
    """Finalize only after trusted caller-owned task/probe/callback cleanup.

    The strict acknowledgment and digest record independently verified cleanup;
    neither a dataclass, zero SQL rows, expired lease, nor shutdown ACK proves it.
    The caller obtains that evidence outside this transaction. Finalization marks
    the exact lease inactive atomically; it never claims deployment-wide DRAINED.
    """
    from django_ray.target.cohort_contract import CohortContractError, _digest
    from django_ray.target.cohort_job_cleanup import owned_job_cleanup_pending

    identity = _control_identity(identity)
    if independently_confirmed_cleanup is not True:
        _reject(MaintenanceReason.CLEANUP_UNCONFIRMED)
    try:
        _digest(cleanup_evidence_digest)
    except CohortContractError:
        raise MaintenanceAdmissionError(MaintenanceReason.INVALID) from None
    confirmed = _wall(cleanup_confirmed_at)
    observed = _wall(_clock())
    with _control_operation(
        expected_revision=expected_revision,
        actor=actor,
        reason=reason,
        authorized=authorized,
        dry_run=dry_run,
        using=using,
    ):
        lease, current, now = _locked_retirement(identity, expected_revision, observed, using=using)
        if current is None or current.state != "REQUESTED":
            _reject(MaintenanceReason.REVISION_CHANGED)
        if not current.created_at <= confirmed <= now:
            _reject(MaintenanceReason.CLOCK_REGRESSION)
        if (
            owned_job_cleanup_pending(identity, using=using)
            or RayTaskExecution.objects.using(using)
            .filter(
                claimed_by_worker=identity.worker_id,
                state__in=("RUNNING", "CANCELLING"),
            )
            .exists()
            or RayTaskCohortClaim.objects.using(using)
            .filter(
                owner_lease_id=identity.worker_id,
                owner_lease_hostname=identity.hostname,
                owner_lease_pid=identity.pid,
                owner_lease_started_at=identity.started_at,
            )
            .exclude(disposition="RESOLVED")
            .exists()
            or RayWorkerTargetCapability.objects.using(using)
            .filter(lease_id=identity.worker_id)
            .exists()
        ):
            _reject(MaintenanceReason.OWNERSHIP_REMAINS)
        if not dry_run:
            RayWorkerRetirement.objects.using(using).create(
                **identity.database_filters(),
                revision=2,
                state="RETIRED",
                actor=actor,
                reason=reason,
                created_at=now,
                cleanup_confirmed_at=confirmed,
                cleanup_evidence_digest=cleanup_evidence_digest,
            )
            lease.is_active = False
            lease.stopped_at = now
            lease.save(using=using, update_fields=("is_active", "stopped_at"))
        return MaintenanceControlChange(True, dry_run, 1, 2, "RETIRED")


def set_task_quarantine(
    identity,
    *,
    quarantined,
    expected_revision,
    actor,
    reason,
    authorized=False,
    dry_run=False,
    using=DEFAULT_DB_ALIAS,
) -> MaintenanceControlChange:
    """Append an exact-generation admission/retry decision, never task disposition."""
    identity = _execution_identity(identity)
    if type(quarantined) is not bool:
        _reject(MaintenanceReason.INVALID)
    observed = _wall(_clock())
    with _control_operation(
        expected_revision=expected_revision,
        actor=actor,
        reason=reason,
        authorized=authorized,
        dry_run=dry_run,
        using=using,
    ):
        task = (
            RayTaskExecution.objects.using(using)
            .select_for_update()
            .filter(
                pk=identity.task_execution_pk,
            )
            .first()
        )
        if task is None or _task_identity(task) != identity:
            _reject(MaintenanceReason.IDENTITY_CHANGED)
        current = (
            RayTaskQuarantine.objects.using(using)
            .filter(
                task_execution_pk=task.pk,
            )
            .order_by("-revision")
            .first()
        )
        revision = current.revision if current else 0
        if revision != expected_revision:
            _reject(MaintenanceReason.REVISION_CHANGED)
        now = _control_clock(
            max(observed, task.created_at, current.created_at if current else observed)
        )
        state = "QUARANTINED" if quarantined else "RELEASED"
        if (current is None and not quarantined) or (
            current is not None and current.state == state
        ):
            return MaintenanceControlChange(False, dry_run, revision, revision, state)
        if not dry_run:
            RayTaskQuarantine.objects.using(using).create(
                task_execution_pk=identity.task_execution_pk,
                task_id=identity.task_id,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
                revision=revision + 1,
                state=state,
                actor=actor,
                reason=reason,
                created_at=now,
            )
        return MaintenanceControlChange(True, dry_run, revision, revision + 1, state)
