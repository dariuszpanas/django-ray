"""Internal coordination services for execution-protocol rollout.

The policy and legacy-admission token are a database synchronization boundary,
not ordinary mutable settings. Keep every transition in this module so later
operator commands can reuse one transactionally proven implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, transaction
from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from django_ray.execution_protocol import (
    LEGACY_EXECUTION_PROTOCOL_VERSION,
    LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
    PROTOCOL_POLICY_SCHEMA_VERSION,
)
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)

_POLICY_KEY = 1
_LEGACY_TOKEN_KEY = 1
_MAX_POLICY_REVISION = (1 << 63) - 1
# Stable signed-int namespace derived from the ASCII bytes ``djry``.
_POSTGRESQL_COORDINATION_LOCK_NAMESPACE = 1_684_697_721
_POSTGRESQL_COORDINATION_LOCK_KEY = 1
_SUPPORTED_DATABASE_VENDORS = frozenset({"postgresql", "sqlite"})
_NONTERMINAL_TASK_STATES = (
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.CANCELLING,
)


class ProtocolCoordinationError(RuntimeError):
    """Base error for a refused or inconsistent protocol-policy transition."""


class UnsupportedProtocolDatabaseError(ProtocolCoordinationError):
    """The configured database cannot enforce the protocol transition."""


class ProtocolPolicyStateError(ProtocolCoordinationError):
    """The singleton policy/token state is absent, corrupt, or unsupported."""


class InvalidProtocolRevisionError(ProtocolCoordinationError):
    """The caller supplied a value that cannot identify a policy revision."""


class ProtocolRevisionExhaustedError(ProtocolCoordinationError):
    """The policy revision cannot advance within its durable field range."""


class NestedProtocolTransitionError(ProtocolCoordinationError):
    """A transition was requested inside a caller-owned database transaction."""


class ProtocolRevisionConflictError(ProtocolCoordinationError):
    """The caller's reviewed policy revision is no longer current."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "execution-protocol policy revision changed "
            f"(expected {expected_revision}, found {actual_revision})"
        )


class LegacyProducerRetirementRequiredError(ProtocolCoordinationError):
    """The caller did not assert retirement of capability-unaware producers."""


class LegacyWorkerAdmissionBlockedError(ProtocolCoordinationError):
    """Active legacy task managers still prevent closing admission."""

    def __init__(self, active_legacy_worker_count: int) -> None:
        self.active_legacy_worker_count = active_legacy_worker_count
        super().__init__(
            "legacy worker admission remains open because "
            f"{active_legacy_worker_count} active legacy worker lease(s) remain"
        )


class LegacyWorkerRollbackBlockedError(ProtocolCoordinationError):
    """Retained nonterminal work prevents reopening legacy admission."""

    def __init__(self, incompatible_nonterminal_execution_count: int) -> None:
        self.incompatible_nonterminal_execution_count = incompatible_nonterminal_execution_count
        super().__init__(
            "legacy worker admission cannot reopen because "
            f"{incompatible_nonterminal_execution_count} nonterminal execution(s) "
            "use a protocol other than v1"
        )


class LegacyAdmissionRaceError(ProtocolCoordinationError):
    """A new token-linked legacy row appeared while closure was acquiring locks."""


@dataclass(frozen=True)
class LegacyWorkerAdmissionTransition:
    """Bounded result of one serialized legacy-admission transition."""

    enabled: bool
    changed: bool
    active_write_protocol_version: int
    previous_revision: int
    revision: int
    updated_at: datetime
    detached_inactive_legacy_leases: int


def _database_vendor(*, using: str) -> str:
    vendor = connections[using].vendor
    if vendor not in _SUPPORTED_DATABASE_VENDORS:
        raise UnsupportedProtocolDatabaseError(
            "execution-protocol coordination supports only SQLite and PostgreSQL"
        )
    return vendor


def _validate_policy(policy: TaskExecutionProtocolPolicy) -> None:
    if policy.schema_version != PROTOCOL_POLICY_SCHEMA_VERSION:
        raise ProtocolPolicyStateError("the execution-protocol policy schema is unsupported")
    if int(policy.active_write_protocol_version) != LEGACY_EXECUTION_PROTOCOL_VERSION:
        raise ProtocolPolicyStateError("this rollout service requires active write protocol v1")


def _validate_revision(
    policy: TaskExecutionProtocolPolicy,
    *,
    expected_revision: int,
) -> None:
    actual_revision = int(policy.revision)
    if actual_revision != expected_revision:
        raise ProtocolRevisionConflictError(
            expected_revision=expected_revision,
            actual_revision=actual_revision,
        )


def _validate_expected_revision_value(expected_revision: int) -> None:
    if (
        type(expected_revision) is not int
        or expected_revision < 1
        or expected_revision > _MAX_POLICY_REVISION
    ):
        raise InvalidProtocolRevisionError(
            "expected_revision must be an exact integer within the positive bigint range"
        )


def _require_revision_capacity(policy: TaskExecutionProtocolPolicy) -> None:
    if int(policy.revision) >= _MAX_POLICY_REVISION:
        raise ProtocolRevisionExhaustedError(
            "execution-protocol policy revision is exhausted and cannot advance"
        )


def _require_outermost_transaction(*, using: str) -> None:
    database_connection = connections[using]
    if database_connection.in_atomic_block or not database_connection.get_autocommit():
        raise NestedProtocolTransitionError(
            "execution-protocol transitions must own the outermost database transaction"
        )


def _validate_token_state(
    policy: TaskExecutionProtocolPolicy,
    *,
    token_exists: bool,
) -> None:
    if policy.legacy_worker_admission_enabled and not token_exists:
        raise ProtocolPolicyStateError("legacy admission is open but its token is missing")
    if not policy.legacy_worker_admission_enabled and token_exists:
        raise ProtocolPolicyStateError("legacy admission is closed but its token still exists")


def _sqlite_lock_policy(*, using: str) -> TaskExecutionProtocolPolicy:
    """Obtain SQLite's writer fence before reading any transition state."""

    updated = (
        TaskExecutionProtocolPolicy.objects.using(using)
        .filter(singleton_key=_POLICY_KEY)
        .update(revision=F("revision"))
    )
    if updated != 1:
        raise ProtocolPolicyStateError("the execution-protocol policy singleton is unavailable")
    return TaskExecutionProtocolPolicy.objects.using(using).get(singleton_key=_POLICY_KEY)


def _postgresql_lock_policy(*, using: str) -> TaskExecutionProtocolPolicy:
    try:
        return (
            TaskExecutionProtocolPolicy.objects.using(using)
            .select_for_update()
            .get(singleton_key=_POLICY_KEY)
        )
    except TaskExecutionProtocolPolicy.DoesNotExist as error:
        raise ProtocolPolicyStateError(
            "the execution-protocol policy singleton is unavailable"
        ) from error


def _postgresql_lock_transition(*, using: str) -> None:
    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [
                _POSTGRESQL_COORDINATION_LOCK_NAMESPACE,
                _POSTGRESQL_COORDINATION_LOCK_KEY,
            ],
        )


def _postgresql_legacy_admission_is_open(*, using: str) -> bool:
    try:
        return bool(
            TaskExecutionProtocolPolicy.objects.using(using)
            .values_list("legacy_worker_admission_enabled", flat=True)
            .get(singleton_key=_POLICY_KEY)
        )
    except TaskExecutionProtocolPolicy.DoesNotExist as error:
        raise ProtocolPolicyStateError(
            "the execution-protocol policy singleton is unavailable"
        ) from error


def _postgresql_lock_execution_writers(*, using: str) -> None:
    database_connection = connections[using]
    quoted_table = database_connection.ops.quote_name(RayTaskExecution._meta.db_table)
    with database_connection.cursor() as cursor:
        cursor.execute(f"LOCK TABLE {quoted_table} IN SHARE ROW EXCLUSIVE MODE")


def _postgresql_lock_legacy_token(*, using: str) -> LegacyWorkerAdmissionToken:
    try:
        return (
            LegacyWorkerAdmissionToken.objects.using(using)
            .select_for_update()
            .get(singleton_key=_LEGACY_TOKEN_KEY)
        )
    except LegacyWorkerAdmissionToken.DoesNotExist as error:
        raise LegacyAdmissionRaceError(
            "the legacy admission token disappeared while admission was closing"
        ) from error


def _create_legacy_token(*, using: str) -> LegacyWorkerAdmissionToken:
    try:
        return LegacyWorkerAdmissionToken.objects.using(using).create(
            singleton_key=_LEGACY_TOKEN_KEY
        )
    except IntegrityError as error:
        raise LegacyAdmissionRaceError(
            "the legacy admission token appeared while admission was reopening"
        ) from error


def _transition_result(
    policy: TaskExecutionProtocolPolicy,
    *,
    changed: bool,
    previous_revision: int,
    detached_inactive_legacy_leases: int,
) -> LegacyWorkerAdmissionTransition:
    return LegacyWorkerAdmissionTransition(
        enabled=bool(policy.legacy_worker_admission_enabled),
        changed=changed,
        active_write_protocol_version=int(policy.active_write_protocol_version),
        previous_revision=previous_revision,
        revision=int(policy.revision),
        updated_at=policy.updated_at,
        detached_inactive_legacy_leases=detached_inactive_legacy_leases,
    )


def close_legacy_worker_admission(
    *,
    expected_revision: int,
    legacy_producers_retired: bool,
    using: str = DEFAULT_DB_ALIAS,
) -> LegacyWorkerAdmissionTransition:
    """Close capability-unaware admission at a serialized database boundary.

    ``legacy_producers_retired`` is an explicit operational assertion because
    worker leases cannot discover old web/API producer processes. Active legacy
    task managers remain a separately enforced database precondition.
    """

    if legacy_producers_retired is not True:
        raise LegacyProducerRetirementRequiredError(
            "closing legacy admission requires an explicit assertion that "
            "capability-unaware producers have been retired"
        )
    _validate_expected_revision_value(expected_revision)

    vendor = _database_vendor(using=using)
    _require_outermost_transaction(using=using)
    with transaction.atomic(using=using, durable=True):
        initially_locked_legacy_worker_count = 0
        if vendor == "postgresql":
            _postgresql_lock_transition(using=using)
            # Heartbeats lock their lease row before their 0019 trigger takes
            # shared policy/token locks. Preserve that ordering to avoid a
            # policy-to-lease deadlock during activation.
            locked_legacy_workers = (
                TaskWorkerLease.objects.using(using)
                .select_for_update()
                .filter(
                    capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
                    legacy_admission_token_id=_LEGACY_TOKEN_KEY,
                )
                .order_by("worker_id")
                .values_list("worker_id", flat=True)
            )
            for _worker_id in locked_legacy_workers.iterator(chunk_size=1_000):
                initially_locked_legacy_worker_count += 1
            policy = _postgresql_lock_policy(using=using)
        else:
            # SQLite's first database statement is the exact no-op write fence.
            policy = _sqlite_lock_policy(using=using)

        _validate_policy(policy)
        _validate_revision(policy, expected_revision=expected_revision)
        token_queryset = LegacyWorkerAdmissionToken.objects.using(using).filter(
            singleton_key=_LEGACY_TOKEN_KEY
        )
        token_exists = token_queryset.exists()
        _validate_token_state(policy, token_exists=token_exists)
        previous_revision = int(policy.revision)
        if not policy.legacy_worker_admission_enabled:
            return _transition_result(
                policy,
                changed=False,
                previous_revision=previous_revision,
                detached_inactive_legacy_leases=0,
            )

        if vendor == "postgresql":
            token = _postgresql_lock_legacy_token(using=using)
            current_legacy_worker_count = (
                TaskWorkerLease.objects.using(using)
                .filter(
                    capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
                    legacy_admission_token_id=_LEGACY_TOKEN_KEY,
                )
                .count()
            )
            if current_legacy_worker_count != initially_locked_legacy_worker_count:
                active_count = (
                    TaskWorkerLease.objects.using(using)
                    .filter(
                        capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
                        legacy_admission_token_id=_LEGACY_TOKEN_KEY,
                        is_active=True,
                    )
                    .count()
                )
                if active_count:
                    raise LegacyWorkerAdmissionBlockedError(active_count)
                raise LegacyAdmissionRaceError(
                    "a token-linked legacy lease appeared while admission was closing"
                )
        else:
            token = token_queryset.get()

        active_legacy_worker_count = (
            TaskWorkerLease.objects.using(using)
            .filter(
                capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
                is_active=True,
            )
            .count()
        )
        if active_legacy_worker_count:
            raise LegacyWorkerAdmissionBlockedError(active_legacy_worker_count)

        _require_revision_capacity(policy)

        detached_inactive_legacy_leases = (
            TaskWorkerLease.objects.using(using)
            .filter(
                capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
                is_active=False,
                legacy_admission_token_id=_LEGACY_TOKEN_KEY,
            )
            .update(legacy_admission_token=None)
        )
        policy.legacy_worker_admission_enabled = False
        policy.revision = previous_revision + 1
        policy.updated_at = timezone.now()
        policy.save(
            using=using,
            update_fields=(
                "legacy_worker_admission_enabled",
                "revision",
                "updated_at",
            ),
        )

        try:
            deleted_count, _ = token.delete(using=using)
        except ProtectedError as error:
            raise LegacyAdmissionRaceError(
                "legacy admission token gained a reference while admission was closing"
            ) from error
        if deleted_count != 1:
            raise ProtocolPolicyStateError(
                "legacy admission token deletion affected an unexpected row count"
            )

        return _transition_result(
            policy,
            changed=True,
            previous_revision=previous_revision,
            detached_inactive_legacy_leases=detached_inactive_legacy_leases,
        )


def reopen_legacy_worker_admission(
    *,
    expected_revision: int,
    using: str = DEFAULT_DB_ALIAS,
) -> LegacyWorkerAdmissionTransition:
    """Reopen legacy-v1 admission after database-enforced rollback preconditions."""

    _validate_expected_revision_value(expected_revision)
    vendor = _database_vendor(using=using)
    _require_outermost_transaction(using=using)
    with transaction.atomic(using=using, durable=True):
        if vendor == "postgresql":
            _postgresql_lock_transition(using=using)
            if not _postgresql_legacy_admission_is_open(using=using):
                # A changing reopen inspects executions before admitting old
                # writers. Take the table writer fence before policy so
                # in-flight writes finish first and later writes wait for the
                # persistent rollback trigger installed by migration 0020.
                _postgresql_lock_execution_writers(using=using)
            policy = _postgresql_lock_policy(using=using)
        else:
            policy = _sqlite_lock_policy(using=using)

        _validate_policy(policy)
        _validate_revision(policy, expected_revision=expected_revision)
        token_queryset = LegacyWorkerAdmissionToken.objects.using(using).filter(
            singleton_key=_LEGACY_TOKEN_KEY
        )
        token_exists = token_queryset.exists()
        _validate_token_state(policy, token_exists=token_exists)
        previous_revision = int(policy.revision)
        incompatible_nonterminal_execution_count = (
            RayTaskExecution.objects.using(using)
            .filter(state__in=_NONTERMINAL_TASK_STATES)
            .exclude(execution_protocol_version=LEGACY_EXECUTION_PROTOCOL_VERSION)
            .count()
        )
        if incompatible_nonterminal_execution_count:
            raise LegacyWorkerRollbackBlockedError(incompatible_nonterminal_execution_count)

        if policy.legacy_worker_admission_enabled:
            return _transition_result(
                policy,
                changed=False,
                previous_revision=previous_revision,
                detached_inactive_legacy_leases=0,
            )

        _require_revision_capacity(policy)
        _create_legacy_token(using=using)
        policy.legacy_worker_admission_enabled = True
        policy.revision = previous_revision + 1
        policy.updated_at = timezone.now()
        policy.save(
            using=using,
            update_fields=(
                "legacy_worker_admission_enabled",
                "revision",
                "updated_at",
            ),
        )
        return _transition_result(
            policy,
            changed=True,
            previous_revision=previous_revision,
            detached_inactive_legacy_leases=0,
        )
