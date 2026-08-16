"""Private coordination for dormant Ray worker target advertisements.

An advertisement is an ephemeral, CAS-revisioned statement made by one exact
task-manager lease incarnation.  It is not durable task provenance and its
presence alone never authorizes a claim.  A future consumer must still
revalidate the lease, current target policy, and attestation at its own
decision boundary.

Every mutation owns an outermost durable transaction and takes locks in the
fixed order ``exact lease -> target -> capability``.  SQLite uses exact no-op
updates as writer fences; PostgreSQL uses row locks.  No operation probes Ray,
renews a worker heartbeat, selects a task, or performs a remote effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.models import F
from django.db.utils import ConnectionDoesNotExist

from django_ray.execution_protocol import explicit_worker_protocol_range
from django_ray.models import (
    RAY_WORKER_TARGET_CAPABILITY_SCHEMA_VERSION,
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.target.attestation import (
    RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetAttestationError,
    RayTargetAttestationRejection,
    RayTargetExpectation,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    decode_ray_target_expectation,
    encode_ray_target_expectation,
)
from django_ray.target.coordination import (
    RayTargetCoordinationError,
    _latest_policy,
    _locked_target,
)

_SUPPORTED_DATABASE_VENDORS = frozenset({"postgresql", "sqlite"})
_TARGET_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_MAX_REVISION = RAY_TARGET_ATTESTATION_MAX_COUNTER
_MAX_PID = (1 << 31) - 1
_MAX_SMALLINT = (1 << 15) - 1


class RayWorkerTargetCapabilityError(RuntimeError):
    """Base class for a fixed, redacted capability-coordination refusal."""


class UnsupportedRayWorkerTargetCapabilityDatabaseError(RayWorkerTargetCapabilityError):
    """The selected database cannot serialize capability coordination."""

    def __init__(self) -> None:
        super().__init__(
            "Ray worker target capability coordination supports only SQLite and PostgreSQL"
        )


class NestedRayWorkerTargetCapabilityTransactionError(RayWorkerTargetCapabilityError):
    """A caller-owned transaction would weaken the durable boundary."""

    def __init__(self) -> None:
        super().__init__(
            "Ray worker target capability coordination must own the outermost database transaction"
        )


class InvalidRayWorkerTargetCapabilityArgumentError(RayWorkerTargetCapabilityError):
    """A caller supplied an unbounded or noncanonical coordination value."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability coordination received an invalid argument")


class RayWorkerTargetCapabilityLeaseError(RayWorkerTargetCapabilityError):
    """The exact lease is absent, inactive, stale, or capability-unaware."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability lease is unavailable or invalid")


class RayWorkerTargetCapabilityTargetStateError(RayWorkerTargetCapabilityError):
    """The target or its latest policy is absent, corrupt, or not usable."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability target state is unavailable or invalid")


class RayWorkerTargetCapabilityPolicyRevisionConflictError(RayWorkerTargetCapabilityError):
    """The caller's reviewed latest target-policy revision is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__("Ray worker target capability policy revision changed")


class RayWorkerTargetCapabilityAttestationStateError(RayWorkerTargetCapabilityError):
    """The latest attestation is missing, corrupt, noncanonical, or expired."""

    def __init__(
        self,
        classification: RayTargetAttestationRejection | None = None,
    ) -> None:
        self.classification = classification
        super().__init__("Ray worker target capability attestation is unavailable or invalid")


class RayWorkerTargetCapabilityAttestationRevisionConflictError(RayWorkerTargetCapabilityError):
    """The caller's reviewed latest attestation revision is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__("Ray worker target capability attestation revision changed")


class RayWorkerTargetCapabilityRuntimeMismatchError(RayWorkerTargetCapabilityError):
    """The actual manager family or runtime differs from the verified target."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability runtime does not match the target")


class RayJobWorkerTargetCapabilityUnsupportedError(RayWorkerTargetCapabilityError):
    """Ray Job lacks the authenticated pre-Django proof channel required here."""

    def __init__(self) -> None:
        super().__init__("Ray Job worker target capability is not supported")


class RayWorkerTargetCapabilityRevisionConflictError(RayWorkerTargetCapabilityError):
    """The caller's expected current capability revision is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__("Ray worker target capability revision changed")


class RayWorkerTargetCapabilityRevisionExhaustedError(RayWorkerTargetCapabilityError):
    """The current capability cannot advance another signed-bigint revision."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability revision is exhausted")


class RayWorkerTargetCapabilityAdvertisementRegressionError(RayWorkerTargetCapabilityError):
    """A renewal would make an older advertisement time authoritative."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability advertisement time regressed")


class RayWorkerTargetCapabilityLimitError(RayWorkerTargetCapabilityError):
    """A Ray Core lease already advertises its one connected target."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability limit is exceeded")


class RayWorkerTargetCapabilityStateError(RayWorkerTargetCapabilityError):
    """The retained current capability is corrupt or changed unexpectedly."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability state is unavailable or invalid")


class RayWorkerTargetCapabilityPersistenceRaceError(RayWorkerTargetCapabilityError):
    """A database race or invariant prevented the requested mutation."""

    def __init__(self) -> None:
        super().__init__("Ray worker target capability could not serialize the mutation")


@dataclass(frozen=True, slots=True)
class RayWorkerTargetCapabilityChange:
    """Bounded result of one advertisement creation, replay, or renewal."""

    target_key: str
    target_policy_revision: int
    attestation_revision: int
    manager_runner_family: RayRunnerFamily
    manager_runtime: RayRuntimeVersion
    changed: bool
    previous_revision: int
    revision: int
    advertised_at: datetime


def _database_vendor(*, using: str) -> str:
    if type(using) is not str or not using:
        raise UnsupportedRayWorkerTargetCapabilityDatabaseError
    try:
        vendor = connections[using].vendor
    except (ConnectionDoesNotExist, TypeError, ValueError):
        raise UnsupportedRayWorkerTargetCapabilityDatabaseError from None
    if vendor not in _SUPPORTED_DATABASE_VENDORS:
        raise UnsupportedRayWorkerTargetCapabilityDatabaseError
    return vendor


def _require_outermost_transaction(*, using: str) -> None:
    database_connection = connections[using]
    if database_connection.in_atomic_block or not database_connection.get_autocommit():
        raise NestedRayWorkerTargetCapabilityTransactionError


def _target_key(value: object) -> str:
    if type(value) is not str or _TARGET_KEY.fullmatch(value) is None:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    return value


def _revision(value: object, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= _MAX_REVISION:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    return value


def _now(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(UTC)
        is_utc = offset is not None and offset.total_seconds() == 0
    except Exception:
        raise InvalidRayWorkerTargetCapabilityArgumentError from None
    if not is_utc:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    return normalized


def _identity(value: object) -> WorkerLeaseIdentity:
    if type(value) is not WorkerLeaseIdentity:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    if (
        type(value.worker_id) is not str
        or not 1 <= len(value.worker_id) <= 255
        or "\x00" in value.worker_id
        or type(value.hostname) is not str
        or not 1 <= len(value.hostname) <= 255
        or "\x00" in value.hostname
        or type(value.pid) is not int
        or not 1 <= value.pid <= _MAX_PID
        or type(value.started_at) is not datetime
        or value.started_at.tzinfo is None
    ):
        raise InvalidRayWorkerTargetCapabilityArgumentError
    try:
        offset = value.started_at.utcoffset()
        normalized = value.started_at.astimezone(UTC)
    except Exception:
        raise InvalidRayWorkerTargetCapabilityArgumentError from None
    if offset is None or not 1 <= normalized.year <= 9999:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    return WorkerLeaseIdentity(
        worker_id=value.worker_id,
        hostname=value.hostname,
        pid=value.pid,
        started_at=normalized,
    )


def _runner_family(value: object) -> RayRunnerFamily:
    if type(value) is not RayRunnerFamily:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    if value is RayRunnerFamily.RAY_JOB:
        raise RayJobWorkerTargetCapabilityUnsupportedError
    if value is not RayRunnerFamily.RAY_CORE:  # pragma: no cover - enum is closed above
        raise InvalidRayWorkerTargetCapabilityArgumentError
    return value


def _runtime(value: object) -> RayRuntimeVersion:
    if type(value) is not RayRuntimeVersion:
        raise InvalidRayWorkerTargetCapabilityArgumentError
    try:
        encoded = encode_ray_target_expectation(
            RayTargetExpectation(
                target_key="runtime-validation",
                runner_family=RayRunnerFamily.RAY_CORE,
                cluster_session="session_runtime-validation",
                policy_revision=1,
                runtime=value,
            )
        )
        return decode_ray_target_expectation(encoded).runtime
    except Exception:
        raise InvalidRayWorkerTargetCapabilityArgumentError from None


def _locked_exact_lease(
    identity: WorkerLeaseIdentity,
    *,
    using: str,
    vendor: str,
) -> TaskWorkerLease | None:
    filters = identity.database_filters()
    if vendor == "sqlite":
        updated = (
            TaskWorkerLease.objects.using(using).filter(**filters).update(worker_id=F("worker_id"))
        )
        if updated == 0:
            return None
        if updated != 1:
            raise RayWorkerTargetCapabilityStateError
        try:
            return TaskWorkerLease.objects.using(using).get(**filters)
        except TaskWorkerLease.DoesNotExist:
            raise RayWorkerTargetCapabilityStateError from None
    return TaskWorkerLease.objects.using(using).select_for_update().filter(**filters).first()


def _require_advertising_lease(lease: TaskWorkerLease | None, *, now: datetime) -> TaskWorkerLease:
    if lease is None or not lease.is_active or lease.stopped_at is not None:
        raise RayWorkerTargetCapabilityLeaseError
    try:
        heartbeat_fresh = lease.last_heartbeat_at >= now - get_lease_duration()
        capability_schema_version = lease.capability_schema_version
        minimum = lease.min_supported_execution_protocol_version
        maximum = lease.max_supported_execution_protocol_version
        if (
            type(capability_schema_version) is not int
            or type(minimum) is not int
            or type(maximum) is not int
            or not 1 <= minimum <= _MAX_SMALLINT
            or not minimum <= maximum <= _MAX_SMALLINT
        ):
            raise ValueError
        protocol_range = explicit_worker_protocol_range(
            capability_schema_version=capability_schema_version,
            legacy_admission_token_present=lease.legacy_admission_token_id is not None,
            minimum=minimum,
            maximum=maximum,
        )
    except Exception:
        raise RayWorkerTargetCapabilityLeaseError from None
    if not heartbeat_fresh or protocol_range is None:
        raise RayWorkerTargetCapabilityLeaseError
    return lease


def _locked_capability_target(*, target_key: str, using: str, vendor: str) -> RayTarget:
    try:
        return _locked_target(target_key=target_key, using=using, vendor=vendor)
    except RayTargetCoordinationError:
        raise RayWorkerTargetCapabilityTargetStateError from None


def _latest_usable_policy(
    target: RayTarget,
    *,
    expected_revision: int,
    using: str,
) -> tuple[RayTargetPolicyRevision, RayTargetExpectation]:
    try:
        policy, expectation, desired_state = _latest_policy(target, using=using)
    except RayTargetCoordinationError:
        raise RayWorkerTargetCapabilityTargetStateError from None
    actual_revision = int(policy.revision)
    if actual_revision != expected_revision:
        raise RayWorkerTargetCapabilityPolicyRevisionConflictError(
            expected_revision=expected_revision,
            actual_revision=actual_revision,
        )
    if expectation.runner_family is RayRunnerFamily.RAY_JOB:
        raise RayJobWorkerTargetCapabilityUnsupportedError
    if expectation.runner_family is not RayRunnerFamily.RAY_CORE or desired_state not in {
        RayTargetDesiredState.ACTIVE,
        RayTargetDesiredState.DRAINING,
    }:
        raise RayWorkerTargetCapabilityTargetStateError
    return policy, expectation


def _latest_valid_attestation(
    policy: RayTargetPolicyRevision,
    expectation: RayTargetExpectation,
    *,
    expected_revision: int,
    now: datetime,
    using: str,
) -> RayTargetAttestationRevision:
    revisions = RayTargetAttestationRevision.objects.using(using).filter(policy_id=policy.pk)
    latest = revisions.order_by("-revision").first()
    if latest is None:
        raise RayWorkerTargetCapabilityAttestationStateError
    actual_revision = int(latest.revision)
    if (
        actual_revision < 1
        or actual_revision > _MAX_REVISION
        or revisions.count() != actual_revision
        or int(latest.attestation_schema_version) != RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION
    ):
        raise RayWorkerTargetCapabilityAttestationStateError
    try:
        attestation = decode_ray_cluster_attestation(latest.attestation_json)
        compare_ray_target_attestation(expectation, attestation, now=now)
    except RayTargetAttestationError as error:
        raise RayWorkerTargetCapabilityAttestationStateError(error.classification) from None
    if (
        attestation.expectation != expectation
        or latest.expectation_digest != attestation.expectation_digest
        or latest.membership_digest != attestation.membership_digest
        or latest.attestation_digest != attestation.attestation_digest
        or latest.observed_at != attestation.observed_at
        or latest.expires_at != attestation.expires_at
        or latest.recorded_at < attestation.observed_at
        or latest.recorded_at >= attestation.expires_at
        or latest.recorded_at > now
    ):
        raise RayWorkerTargetCapabilityAttestationStateError
    if actual_revision != expected_revision:
        raise RayWorkerTargetCapabilityAttestationRevisionConflictError(
            expected_revision=expected_revision,
            actual_revision=actual_revision,
        )
    return latest


def _locked_current_capability(
    lease: TaskWorkerLease,
    target: RayTarget,
    *,
    using: str,
    vendor: str,
) -> RayWorkerTargetCapability | None:
    filters = {"lease_id": lease.pk, "target_id": target.pk}
    if vendor == "sqlite":
        updated = (
            RayWorkerTargetCapability.objects.using(using)
            .filter(**filters)
            .update(revision=F("revision"))
        )
        if updated == 0:
            return None
        if updated != 1:
            raise RayWorkerTargetCapabilityStateError
        try:
            return RayWorkerTargetCapability.objects.using(using).get(**filters)
        except RayWorkerTargetCapability.DoesNotExist:
            raise RayWorkerTargetCapabilityStateError from None
    return (
        RayWorkerTargetCapability.objects.using(using).select_for_update().filter(**filters).first()
    )


def _capability_runtime(capability: RayWorkerTargetCapability) -> RayRuntimeVersion:
    try:
        return _runtime(
            RayRuntimeVersion(
                ray_major=int(capability.manager_ray_major),
                ray_minor=int(capability.manager_ray_minor),
                ray_patch=int(capability.manager_ray_patch),
                python_implementation=capability.manager_python_implementation,
                python_major=int(capability.manager_python_major),
                python_minor=int(capability.manager_python_minor),
                python_patch=int(capability.manager_python_patch),
            )
        )
    except InvalidRayWorkerTargetCapabilityArgumentError:
        raise RayWorkerTargetCapabilityStateError from None


def _validate_current_capability(
    capability: RayWorkerTargetCapability,
    *,
    lease: TaskWorkerLease,
    target: RayTarget,
    using: str,
) -> tuple[int, RayRuntimeVersion]:
    revision = int(capability.revision)
    runtime = _capability_runtime(capability)
    if (
        revision < 1
        or revision > _MAX_REVISION
        or int(capability.schema_version) != RAY_WORKER_TARGET_CAPABILITY_SCHEMA_VERSION
        or capability.lease_id != lease.pk
        or capability.lease_hostname != lease.hostname
        or int(capability.lease_pid) != int(lease.pid)
        or capability.lease_started_at != lease.started_at
        or capability.target_id != target.pk
        or capability.runner_family != target.runner_family
        or capability.runner_family != RayRunnerFamily.RAY_CORE.value
        or runtime
        != RayRuntimeVersion(
            ray_major=int(target.ray_major),
            ray_minor=int(target.ray_minor),
            ray_patch=int(target.ray_patch),
            python_implementation=target.python_implementation,
            python_major=int(target.python_major),
            python_minor=int(target.python_minor),
            python_patch=int(target.python_patch),
        )
        or capability.advertised_at < capability.created_at
        or not RayTargetPolicyRevision.objects.using(using)
        .filter(pk=capability.target_policy_id, target_id=target.pk)
        .exists()
        or not RayTargetAttestationRevision.objects.using(using)
        .filter(pk=capability.attestation_id, policy_id=capability.target_policy_id)
        .exists()
    ):
        raise RayWorkerTargetCapabilityStateError
    return revision, runtime


def _change(
    *,
    expectation: RayTargetExpectation,
    attestation: RayTargetAttestationRevision,
    manager_runner_family: RayRunnerFamily,
    manager_runtime: RayRuntimeVersion,
    changed: bool,
    previous_revision: int,
    revision: int,
    advertised_at: datetime,
) -> RayWorkerTargetCapabilityChange:
    return RayWorkerTargetCapabilityChange(
        target_key=expectation.target_key,
        target_policy_revision=expectation.policy_revision,
        attestation_revision=int(attestation.revision),
        manager_runner_family=manager_runner_family,
        manager_runtime=manager_runtime,
        changed=changed,
        previous_revision=previous_revision,
        revision=revision,
        advertised_at=advertised_at,
    )


def advertise_ray_worker_target_capability(
    identity: WorkerLeaseIdentity,
    target_key: str,
    actual_runtime: RayRuntimeVersion,
    *,
    manager_runner_family: RayRunnerFamily,
    expected_policy_revision: int,
    expected_attestation_revision: int,
    expected_capability_revision: int,
    now: datetime,
    using: str = DEFAULT_DB_ALIAS,
) -> RayWorkerTargetCapabilityChange:
    """Create, exactly replay, or CAS-renew one dormant Ray Core capability."""

    identity = _identity(identity)
    target_key = _target_key(target_key)
    actual_runtime = _runtime(actual_runtime)
    manager_runner_family = _runner_family(manager_runner_family)
    expected_policy_revision = _revision(expected_policy_revision, allow_zero=False)
    expected_attestation_revision = _revision(
        expected_attestation_revision,
        allow_zero=False,
    )
    expected_capability_revision = _revision(
        expected_capability_revision,
        allow_zero=True,
    )
    now = _now(now)
    try:
        vendor = _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            lease = _require_advertising_lease(
                _locked_exact_lease(identity, using=using, vendor=vendor),
                now=now,
            )
            target = _locked_capability_target(
                target_key=target_key,
                using=using,
                vendor=vendor,
            )
            policy, expectation = _latest_usable_policy(
                target,
                expected_revision=expected_policy_revision,
                using=using,
            )
            attestation = _latest_valid_attestation(
                policy,
                expectation,
                expected_revision=expected_attestation_revision,
                now=now,
                using=using,
            )
            if (
                manager_runner_family is not expectation.runner_family
                or actual_runtime != expectation.runtime
            ):
                raise RayWorkerTargetCapabilityRuntimeMismatchError

            current = _locked_current_capability(
                lease,
                target,
                using=using,
                vendor=vendor,
            )
            if current is None:
                if expected_capability_revision != 0:
                    raise RayWorkerTargetCapabilityRevisionConflictError(
                        expected_revision=expected_capability_revision,
                        actual_revision=0,
                    )
                if (
                    RayWorkerTargetCapability.objects.using(using)
                    .filter(lease_id=lease.pk)
                    .exists()
                ):
                    raise RayWorkerTargetCapabilityLimitError
                runtime = actual_runtime
                RayWorkerTargetCapability.objects.using(using).create(
                    lease=lease,
                    lease_hostname=lease.hostname,
                    lease_pid=lease.pid,
                    lease_started_at=lease.started_at,
                    target=target,
                    target_policy=policy,
                    attestation=attestation,
                    runner_family=manager_runner_family.value,
                    manager_ray_major=runtime.ray_major,
                    manager_ray_minor=runtime.ray_minor,
                    manager_ray_patch=runtime.ray_patch,
                    manager_python_implementation=runtime.python_implementation,
                    manager_python_major=runtime.python_major,
                    manager_python_minor=runtime.python_minor,
                    manager_python_patch=runtime.python_patch,
                    schema_version=RAY_WORKER_TARGET_CAPABILITY_SCHEMA_VERSION,
                    revision=1,
                    created_at=now,
                    advertised_at=now,
                )
                return _change(
                    expectation=expectation,
                    attestation=attestation,
                    manager_runner_family=manager_runner_family,
                    manager_runtime=actual_runtime,
                    changed=True,
                    previous_revision=0,
                    revision=1,
                    advertised_at=now,
                )

            actual_capability_revision, retained_runtime = _validate_current_capability(
                current,
                lease=lease,
                target=target,
                using=using,
            )
            exact_replay = (
                current.target_policy_id == policy.pk
                and current.attestation_id == attestation.pk
                and current.runner_family == manager_runner_family.value
                and retained_runtime == actual_runtime
                and current.advertised_at == now
            )
            if exact_replay and expected_capability_revision in {
                actual_capability_revision,
                actual_capability_revision - 1,
            }:
                return _change(
                    expectation=expectation,
                    attestation=attestation,
                    manager_runner_family=manager_runner_family,
                    manager_runtime=actual_runtime,
                    changed=False,
                    previous_revision=actual_capability_revision,
                    revision=actual_capability_revision,
                    advertised_at=current.advertised_at,
                )
            if expected_capability_revision != actual_capability_revision:
                raise RayWorkerTargetCapabilityRevisionConflictError(
                    expected_revision=expected_capability_revision,
                    actual_revision=actual_capability_revision,
                )
            if now <= current.advertised_at:
                raise RayWorkerTargetCapabilityAdvertisementRegressionError
            if actual_capability_revision >= _MAX_REVISION:
                raise RayWorkerTargetCapabilityRevisionExhaustedError

            next_revision = actual_capability_revision + 1
            updated = (
                RayWorkerTargetCapability.objects.using(using)
                .filter(pk=current.pk, revision=actual_capability_revision)
                .update(
                    target_policy=policy,
                    attestation=attestation,
                    revision=next_revision,
                    advertised_at=now,
                )
            )
            if updated != 1:
                raise RayWorkerTargetCapabilityStateError
            return _change(
                expectation=expectation,
                attestation=attestation,
                manager_runner_family=manager_runner_family,
                manager_runtime=actual_runtime,
                changed=True,
                previous_revision=actual_capability_revision,
                revision=next_revision,
                advertised_at=now,
            )
    except RayWorkerTargetCapabilityError:
        raise
    except DatabaseError:
        raise RayWorkerTargetCapabilityPersistenceRaceError from None


def withdraw_ray_worker_target_capability(
    identity: WorkerLeaseIdentity,
    target_key: str,
    *,
    expected_capability_revision: int,
    using: str = DEFAULT_DB_ALIAS,
) -> bool:
    """CAS-delete one current capability; already-absent state is idempotent."""

    identity = _identity(identity)
    target_key = _target_key(target_key)
    expected_capability_revision = _revision(
        expected_capability_revision,
        allow_zero=True,
    )
    try:
        vendor = _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            lease = _locked_exact_lease(identity, using=using, vendor=vendor)
            if lease is None:
                return False
            try:
                target = _locked_capability_target(
                    target_key=target_key,
                    using=using,
                    vendor=vendor,
                )
            except RayWorkerTargetCapabilityTargetStateError:
                return False
            current = _locked_current_capability(
                lease,
                target,
                using=using,
                vendor=vendor,
            )
            if current is None:
                return False
            actual_revision = int(current.revision)
            if actual_revision != expected_capability_revision:
                raise RayWorkerTargetCapabilityRevisionConflictError(
                    expected_revision=expected_capability_revision,
                    actual_revision=actual_revision,
                )
            _total, deleted = (
                RayWorkerTargetCapability.objects.using(using)
                .filter(pk=current.pk, revision=actual_revision)
                .delete()
            )
            if deleted.get(RayWorkerTargetCapability._meta.label, 0) != 1:
                raise RayWorkerTargetCapabilityStateError
            return True
    except RayWorkerTargetCapabilityError:
        raise
    except DatabaseError:
        raise RayWorkerTargetCapabilityPersistenceRaceError from None


def withdraw_all_ray_worker_target_capabilities(
    identity: WorkerLeaseIdentity,
    *,
    using: str = DEFAULT_DB_ALIAS,
) -> int:
    """Delete all current capabilities for one exact lease incarnation."""

    identity = _identity(identity)
    try:
        vendor = _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            lease = _locked_exact_lease(identity, using=using, vendor=vendor)
            if lease is None:
                return 0
            target_keys = list(
                RayWorkerTargetCapability.objects.using(using)
                .filter(lease_id=lease.pk)
                .order_by("target_id")
                .values_list("target_id", flat=True)
            )
            locked: list[RayWorkerTargetCapability] = []
            targets = [
                _locked_capability_target(
                    target_key=target_key,
                    using=using,
                    vendor=vendor,
                )
                for target_key in target_keys
            ]
            for target in targets:
                capability = _locked_current_capability(
                    lease,
                    target,
                    using=using,
                    vendor=vendor,
                )
                if capability is None:
                    raise RayWorkerTargetCapabilityStateError
                locked.append(capability)
            if not locked:
                return 0
            primary_keys = [capability.pk for capability in locked]
            _total, deleted = (
                RayWorkerTargetCapability.objects.using(using).filter(pk__in=primary_keys).delete()
            )
            removed = deleted.get(RayWorkerTargetCapability._meta.label, 0)
            if removed != len(primary_keys):
                raise RayWorkerTargetCapabilityStateError
            return removed
    except RayWorkerTargetCapabilityError:
        raise
    except DatabaseError:
        raise RayWorkerTargetCapabilityPersistenceRaceError from None


__all__ = [
    "InvalidRayWorkerTargetCapabilityArgumentError",
    "NestedRayWorkerTargetCapabilityTransactionError",
    "RayJobWorkerTargetCapabilityUnsupportedError",
    "RayWorkerTargetCapabilityAdvertisementRegressionError",
    "RayWorkerTargetCapabilityAttestationRevisionConflictError",
    "RayWorkerTargetCapabilityAttestationStateError",
    "RayWorkerTargetCapabilityChange",
    "RayWorkerTargetCapabilityError",
    "RayWorkerTargetCapabilityLeaseError",
    "RayWorkerTargetCapabilityLimitError",
    "RayWorkerTargetCapabilityPersistenceRaceError",
    "RayWorkerTargetCapabilityPolicyRevisionConflictError",
    "RayWorkerTargetCapabilityRevisionConflictError",
    "RayWorkerTargetCapabilityRevisionExhaustedError",
    "RayWorkerTargetCapabilityRuntimeMismatchError",
    "RayWorkerTargetCapabilityStateError",
    "RayWorkerTargetCapabilityTargetStateError",
    "UnsupportedRayWorkerTargetCapabilityDatabaseError",
    "advertise_ray_worker_target_capability",
    "withdraw_all_ray_worker_target_capabilities",
    "withdraw_ray_worker_target_capability",
]
