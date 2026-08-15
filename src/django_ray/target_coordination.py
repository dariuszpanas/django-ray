"""Dormant persistence services for revisioned Ray target intent and proof.

This module owns only the database synchronization boundary.  Callers must
collect a :class:`~django_ray.target_attestation.RayClusterAttestation` before
calling it; no probe, Ray connection, or remote effect runs inside one of the
durable transactions below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    connections,
    transaction,
)
from django.db.models import F
from django.db.utils import ConnectionDoesNotExist

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
)
from django_ray.target_attestation import (
    RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayClusterAttestation,
    RayRunnerFamily,
    RayTargetAttestationError,
    RayTargetAttestationRejection,
    RayTargetExpectation,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    decode_ray_target_expectation,
    encode_ray_cluster_attestation,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)

_SUPPORTED_DATABASE_VENDORS = frozenset({"postgresql", "sqlite"})
_TARGET_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_MAX_REVISION = RAY_TARGET_ATTESTATION_MAX_COUNTER


class RayTargetCoordinationError(RuntimeError):
    """Base class for a fixed, redacted target-persistence refusal."""


class UnsupportedRayTargetDatabaseError(RayTargetCoordinationError):
    """The selected database cannot serialize target coordination."""

    def __init__(self) -> None:
        super().__init__("Ray target coordination supports only SQLite and PostgreSQL")


class NestedRayTargetTransactionError(RayTargetCoordinationError):
    """A caller-owned transaction would weaken the durable boundary."""

    def __init__(self) -> None:
        super().__init__("Ray target coordination must own the outermost database transaction")


class InvalidRayTargetArgumentError(RayTargetCoordinationError):
    """A public argument is not an exact bounded coordination value."""

    def __init__(self) -> None:
        super().__init__("Ray target coordination received an invalid argument")


class RayTargetNotFoundError(RayTargetCoordinationError):
    """No durable target exists for the requested canonical key."""

    def __init__(self) -> None:
        super().__init__("Ray target coordination could not find the target")


class RayTargetRegistrationConflictError(RayTargetCoordinationError):
    """The requested target identity is already registered differently."""

    def __init__(self) -> None:
        super().__init__("Ray target registration conflicts with durable state")


class RayTargetPolicyStateError(RayTargetCoordinationError):
    """Retained target policy state is absent, corrupt, or noncanonical."""

    def __init__(self) -> None:
        super().__init__("Ray target policy state is unavailable or invalid")


class RayTargetPolicyRevisionConflictError(RayTargetCoordinationError):
    """The caller's reviewed target policy revision is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__("Ray target policy revision changed")


class RayTargetPolicyRevisionExhaustedError(RayTargetCoordinationError):
    """The target policy cannot append another signed-bigint revision."""

    def __init__(self) -> None:
        super().__init__("Ray target policy revision is exhausted")


class InvalidRayTargetDesiredStateError(RayTargetCoordinationError):
    """The requested lifecycle transition is not active-to-draining or inverse."""

    def __init__(self) -> None:
        super().__init__("Ray target desired-state transition is invalid")


class RayTargetRetirementReservedError(RayTargetCoordinationError):
    """Retirement remains reserved for the future drain adapter in issue 368."""

    def __init__(self) -> None:
        super().__init__("Ray target retirement is not available in this coordination service")


class RayJobTargetPersistenceUnsupportedError(RayTargetCoordinationError):
    """Ray Job lacks the authenticated pre-Django proof channel this slice needs."""

    def __init__(self) -> None:
        super().__init__("Ray Job target persistence is not supported")


class RayTargetAttestationRevisionConflictError(RayTargetCoordinationError):
    """The caller's reviewed attestation revision is stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__("Ray target attestation revision changed")


class RayTargetAttestationRevisionExhaustedError(RayTargetCoordinationError):
    """The current policy cannot append another attestation revision."""

    def __init__(self) -> None:
        super().__init__("Ray target attestation revision is exhausted")


class RayTargetAttestationRegressionError(RayTargetCoordinationError):
    """A new proof would make an older observation authoritative again."""

    def __init__(self) -> None:
        super().__init__("Ray target attestation time regressed")


class RayTargetAttestationRejectedError(RayTargetCoordinationError):
    """The supplied canonical proof does not verify the current target policy."""

    def __init__(self, classification: RayTargetAttestationRejection) -> None:
        self.classification = classification
        super().__init__("Ray target attestation does not verify the current policy")


class RayTargetPersistenceRaceError(RayTargetCoordinationError):
    """A database race or invariant prevented an append-only mutation."""

    def __init__(self) -> None:
        super().__init__("Ray target persistence could not serialize the mutation")


@dataclass(frozen=True, slots=True)
class RayTargetPolicyChange:
    """Bounded result of registration or one desired-state transition."""

    target_key: str
    desired_state: RayTargetDesiredState
    changed: bool
    previous_revision: int
    revision: int
    expectation: RayTargetExpectation


@dataclass(frozen=True, slots=True)
class RayTargetAttestationRecord:
    """Bounded result of one verified attestation append."""

    target_key: str
    policy_revision: int
    previous_revision: int
    revision: int
    attestation: RayClusterAttestation
    recorded_at: datetime


def _database_vendor(*, using: str) -> str:
    if type(using) is not str or not using:
        raise UnsupportedRayTargetDatabaseError
    try:
        vendor = connections[using].vendor
    except (ConnectionDoesNotExist, TypeError, ValueError):
        raise UnsupportedRayTargetDatabaseError from None
    if vendor not in _SUPPORTED_DATABASE_VENDORS:
        raise UnsupportedRayTargetDatabaseError
    return vendor


def _require_outermost_transaction(*, using: str) -> None:
    database_connection = connections[using]
    if database_connection.in_atomic_block or not database_connection.get_autocommit():
        raise NestedRayTargetTransactionError


def _target_key(value: object) -> str:
    if type(value) is not str or _TARGET_KEY.fullmatch(value) is None:
        raise InvalidRayTargetArgumentError
    return value


def _positive_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise InvalidRayTargetArgumentError
    return value


def _attestation_revision(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_REVISION:
        raise InvalidRayTargetArgumentError
    return value


def _desired_state(value: object) -> RayTargetDesiredState:
    if type(value) not in {str, RayTargetDesiredState}:
        raise InvalidRayTargetArgumentError
    try:
        return RayTargetDesiredState(value)
    except ValueError:
        raise InvalidRayTargetArgumentError from None


def _now(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise InvalidRayTargetArgumentError
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(UTC)
        is_utc = offset is not None and offset.total_seconds() == 0
    except Exception:
        raise InvalidRayTargetArgumentError from None
    if not is_utc:
        raise InvalidRayTargetArgumentError
    return normalized


def _canonical_expectation(
    value: object,
) -> tuple[RayTargetExpectation, str, str]:
    if type(value) is not RayTargetExpectation:
        raise InvalidRayTargetArgumentError
    try:
        encoded = encode_ray_target_expectation(value)
        expectation = decode_ray_target_expectation(encoded)
        digest = ray_target_expectation_digest(expectation)
    except Exception:
        raise InvalidRayTargetArgumentError from None
    return expectation, encoded, digest


def _canonical_attestation(value: object) -> tuple[RayClusterAttestation, str]:
    if type(value) is not RayClusterAttestation:
        raise InvalidRayTargetArgumentError
    try:
        encoded = encode_ray_cluster_attestation(value)
        attestation = decode_ray_cluster_attestation(encoded)
    except Exception:
        raise InvalidRayTargetArgumentError from None
    return attestation, encoded


def _require_ray_core(expectation: RayTargetExpectation) -> None:
    if expectation.runner_family is not RayRunnerFamily.RAY_CORE:
        raise RayJobTargetPersistenceUnsupportedError


def _target_matches_expectation(target: RayTarget, expectation: RayTargetExpectation) -> bool:
    runtime = expectation.runtime
    return (
        target.target_key == expectation.target_key
        and target.runner_family == expectation.runner_family.value
        and target.cluster_session == expectation.cluster_session
        and int(target.ray_major) == runtime.ray_major
        and int(target.ray_minor) == runtime.ray_minor
        and int(target.ray_patch) == runtime.ray_patch
        and target.python_implementation == runtime.python_implementation
        and int(target.python_major) == runtime.python_major
        and int(target.python_minor) == runtime.python_minor
        and int(target.python_patch) == runtime.python_patch
    )


def _sqlite_target_writer_fence(*, target_key: str, using: str) -> int:
    """Make SQLite's first transaction statement a database writer fence."""

    return (
        RayTarget.objects.using(using)
        .filter(target_key=target_key)
        .update(target_key=F("target_key"))
    )


def _locked_target(*, target_key: str, using: str, vendor: str) -> RayTarget:
    if vendor == "sqlite":
        if _sqlite_target_writer_fence(target_key=target_key, using=using) != 1:
            raise RayTargetNotFoundError
        try:
            return RayTarget.objects.using(using).get(target_key=target_key)
        except RayTarget.DoesNotExist:
            raise RayTargetNotFoundError from None
    try:
        return RayTarget.objects.using(using).select_for_update().get(target_key=target_key)
    except RayTarget.DoesNotExist:
        raise RayTargetNotFoundError from None


def _locked_or_absent_target(*, target_key: str, using: str, vendor: str) -> RayTarget | None:
    if vendor == "sqlite":
        _sqlite_target_writer_fence(target_key=target_key, using=using)
        return RayTarget.objects.using(using).filter(target_key=target_key).first()
    return RayTarget.objects.using(using).select_for_update().filter(target_key=target_key).first()


def _policy_expectation(
    target: RayTarget,
    policy: RayTargetPolicyRevision,
    *,
    using: str,
) -> tuple[RayTargetExpectation, RayTargetDesiredState]:
    revision = int(policy.revision)
    history = RayTargetPolicyRevision.objects.using(using).filter(target_id=target.pk)
    if (
        revision < 1
        or revision > _MAX_REVISION
        or history.count() != revision
        or not history.filter(
            revision=1,
            desired_state=RayTargetDesiredState.DRAINING,
        ).exists()
        or int(policy.expectation_schema_version) != RAY_TARGET_EXPECTATION_SCHEMA_VERSION
    ):
        raise RayTargetPolicyStateError
    try:
        desired_state = RayTargetDesiredState(policy.desired_state)
        expectation = decode_ray_target_expectation(policy.expectation_json)
    except (ValueError, RayTargetAttestationError):
        raise RayTargetPolicyStateError from None
    if (
        expectation.policy_revision != revision
        or policy.expectation_digest != ray_target_expectation_digest(expectation)
        or not _target_matches_expectation(target, expectation)
    ):
        raise RayTargetPolicyStateError
    return expectation, desired_state


def _latest_policy(
    target: RayTarget,
    *,
    using: str,
) -> tuple[RayTargetPolicyRevision, RayTargetExpectation, RayTargetDesiredState]:
    policy = (
        RayTargetPolicyRevision.objects.using(using)
        .filter(target_id=target.pk)
        .order_by("-revision")
        .first()
    )
    if policy is None:
        raise RayTargetPolicyStateError
    expectation, desired_state = _policy_expectation(target, policy, using=using)
    return policy, expectation, desired_state


def _validate_policy_revision(policy: RayTargetPolicyRevision, *, expected_revision: int) -> None:
    actual_revision = int(policy.revision)
    if actual_revision != expected_revision:
        raise RayTargetPolicyRevisionConflictError(
            expected_revision=expected_revision,
            actual_revision=actual_revision,
        )


def _latest_attestation_head(
    policy: RayTargetPolicyRevision,
    expectation: RayTargetExpectation,
    *,
    using: str,
) -> tuple[int, datetime | None, datetime | None]:
    revisions = RayTargetAttestationRevision.objects.using(using).filter(policy_id=policy.pk)
    latest = revisions.order_by("-revision").first()
    if latest is None:
        return 0, None, None
    revision = int(latest.revision)
    try:
        retained = decode_ray_cluster_attestation(latest.attestation_json)
    except RayTargetAttestationError:
        raise RayTargetPolicyStateError from None
    if (
        revision < 1
        or revision > _MAX_REVISION
        or revisions.count() != revision
        or int(latest.attestation_schema_version) != RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION
        or retained.expectation != expectation
        or latest.expectation_digest != retained.expectation_digest
        or latest.membership_digest != retained.membership_digest
        or latest.attestation_digest != retained.attestation_digest
        or latest.observed_at != retained.observed_at
        or latest.expires_at != retained.expires_at
        or latest.recorded_at < retained.observed_at
        or latest.recorded_at >= retained.expires_at
    ):
        raise RayTargetPolicyStateError
    return revision, latest.observed_at, latest.recorded_at


def _policy_result(
    *,
    expectation: RayTargetExpectation,
    desired_state: RayTargetDesiredState,
    changed: bool,
    previous_revision: int,
) -> RayTargetPolicyChange:
    return RayTargetPolicyChange(
        target_key=expectation.target_key,
        desired_state=desired_state,
        changed=changed,
        previous_revision=previous_revision,
        revision=expectation.policy_revision,
        expectation=expectation,
    )


def register_ray_target(
    expectation: RayTargetExpectation,
    *,
    using: str = DEFAULT_DB_ALIAS,
) -> RayTargetPolicyChange:
    """Register one exact Ray Core target in draining policy revision 1."""

    expectation, expectation_json, expectation_digest = _canonical_expectation(expectation)
    if expectation.policy_revision != 1:
        raise InvalidRayTargetArgumentError
    _require_ray_core(expectation)
    vendor = _database_vendor(using=using)
    try:
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            target = _locked_or_absent_target(
                target_key=expectation.target_key,
                using=using,
                vendor=vendor,
            )
            if target is not None:
                policy, retained, desired_state = _latest_policy(target, using=using)
                _require_ray_core(retained)
                if (
                    int(policy.revision) == 1
                    and desired_state is RayTargetDesiredState.DRAINING
                    and retained == expectation
                ):
                    return _policy_result(
                        expectation=retained,
                        desired_state=desired_state,
                        changed=False,
                        previous_revision=1,
                    )
                raise RayTargetRegistrationConflictError

            runtime = expectation.runtime
            target = RayTarget.objects.using(using).create(
                target_key=expectation.target_key,
                runner_family=expectation.runner_family.value,
                cluster_session=expectation.cluster_session,
                ray_major=runtime.ray_major,
                ray_minor=runtime.ray_minor,
                ray_patch=runtime.ray_patch,
                python_implementation=runtime.python_implementation,
                python_major=runtime.python_major,
                python_minor=runtime.python_minor,
                python_patch=runtime.python_patch,
            )
            RayTargetPolicyRevision.objects.using(using).create(
                target=target,
                revision=1,
                desired_state=RayTargetDesiredState.DRAINING,
                expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
                expectation_json=expectation_json,
                expectation_digest=expectation_digest,
            )
            return _policy_result(
                expectation=expectation,
                desired_state=RayTargetDesiredState.DRAINING,
                changed=True,
                previous_revision=0,
            )
    except RayTargetCoordinationError:
        raise
    except DatabaseError:
        raise RayTargetPersistenceRaceError from None


def transition_ray_target_desired_state(
    target_key: str,
    desired_state: RayTargetDesiredState | str,
    *,
    expected_policy_revision: int,
    using: str = DEFAULT_DB_ALIAS,
) -> RayTargetPolicyChange:
    """CAS-append an active/draining policy transition for one target."""

    target_key = _target_key(target_key)
    desired_state = _desired_state(desired_state)
    expected_policy_revision = _positive_revision(expected_policy_revision)
    if desired_state is RayTargetDesiredState.RETIRED:
        raise RayTargetRetirementReservedError
    vendor = _database_vendor(using=using)
    try:
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            target = _locked_target(target_key=target_key, using=using, vendor=vendor)
            policy, expectation, current_state = _latest_policy(target, using=using)
            _require_ray_core(expectation)
            if current_state is RayTargetDesiredState.RETIRED:
                raise RayTargetRetirementReservedError
            _validate_policy_revision(policy, expected_revision=expected_policy_revision)
            previous_revision = int(policy.revision)
            if current_state is desired_state:
                return _policy_result(
                    expectation=expectation,
                    desired_state=current_state,
                    changed=False,
                    previous_revision=previous_revision,
                )
            if {current_state, desired_state} != {
                RayTargetDesiredState.ACTIVE,
                RayTargetDesiredState.DRAINING,
            }:
                raise InvalidRayTargetDesiredStateError
            if previous_revision >= _MAX_REVISION:
                raise RayTargetPolicyRevisionExhaustedError

            next_expectation, expectation_json, expectation_digest = _canonical_expectation(
                replace(expectation, policy_revision=previous_revision + 1)
            )
            RayTargetPolicyRevision.objects.using(using).create(
                target=target,
                revision=next_expectation.policy_revision,
                desired_state=desired_state,
                expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
                expectation_json=expectation_json,
                expectation_digest=expectation_digest,
            )
            return _policy_result(
                expectation=next_expectation,
                desired_state=desired_state,
                changed=True,
                previous_revision=previous_revision,
            )
    except RayTargetCoordinationError:
        raise
    except DatabaseError:
        raise RayTargetPersistenceRaceError from None


def record_ray_target_attestation(
    target_key: str,
    attestation: RayClusterAttestation,
    *,
    expected_policy_revision: int,
    expected_attestation_revision: int,
    now: datetime,
    using: str = DEFAULT_DB_ALIAS,
) -> RayTargetAttestationRecord:
    """CAS-append one already-produced, currently valid Ray Core proof."""

    target_key = _target_key(target_key)
    expected_policy_revision = _positive_revision(expected_policy_revision)
    expected_attestation_revision = _attestation_revision(expected_attestation_revision)
    now = _now(now)
    attestation, attestation_json = _canonical_attestation(attestation)
    vendor = _database_vendor(using=using)
    try:
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            target = _locked_target(target_key=target_key, using=using, vendor=vendor)
            policy, expectation, desired_state = _latest_policy(target, using=using)
            _require_ray_core(expectation)
            if desired_state is RayTargetDesiredState.RETIRED:
                raise RayTargetRetirementReservedError
            _validate_policy_revision(policy, expected_revision=expected_policy_revision)
            try:
                compare_ray_target_attestation(expectation, attestation, now=now)
            except RayTargetAttestationError as error:
                raise RayTargetAttestationRejectedError(error.classification) from None

            (
                actual_attestation_revision,
                latest_observed_at,
                latest_recorded_at,
            ) = _latest_attestation_head(
                policy,
                expectation,
                using=using,
            )
            if actual_attestation_revision != expected_attestation_revision:
                raise RayTargetAttestationRevisionConflictError(
                    expected_revision=expected_attestation_revision,
                    actual_revision=actual_attestation_revision,
                )
            if (
                latest_observed_at is not None
                and latest_recorded_at is not None
                and (attestation.observed_at < latest_observed_at or now < latest_recorded_at)
            ):
                raise RayTargetAttestationRegressionError
            if actual_attestation_revision >= _MAX_REVISION:
                raise RayTargetAttestationRevisionExhaustedError

            next_revision = actual_attestation_revision + 1
            RayTargetAttestationRevision.objects.using(using).create(
                policy=policy,
                revision=next_revision,
                attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
                attestation_json=attestation_json,
                expectation_digest=attestation.expectation_digest,
                membership_digest=attestation.membership_digest,
                attestation_digest=attestation.attestation_digest,
                observed_at=attestation.observed_at,
                expires_at=attestation.expires_at,
                recorded_at=now,
            )
            return RayTargetAttestationRecord(
                target_key=target_key,
                policy_revision=int(policy.revision),
                previous_revision=actual_attestation_revision,
                revision=next_revision,
                attestation=attestation,
                recorded_at=now,
            )
    except RayTargetCoordinationError:
        raise
    except DatabaseError:
        raise RayTargetPersistenceRaceError from None


__all__ = [
    "InvalidRayTargetArgumentError",
    "InvalidRayTargetDesiredStateError",
    "NestedRayTargetTransactionError",
    "RayJobTargetPersistenceUnsupportedError",
    "RayTargetAttestationRecord",
    "RayTargetAttestationRegressionError",
    "RayTargetAttestationRejectedError",
    "RayTargetAttestationRevisionConflictError",
    "RayTargetAttestationRevisionExhaustedError",
    "RayTargetCoordinationError",
    "RayTargetNotFoundError",
    "RayTargetPersistenceRaceError",
    "RayTargetPolicyChange",
    "RayTargetPolicyRevisionConflictError",
    "RayTargetPolicyRevisionExhaustedError",
    "RayTargetPolicyStateError",
    "RayTargetRegistrationConflictError",
    "RayTargetRetirementReservedError",
    "UnsupportedRayTargetDatabaseError",
    "record_ray_target_attestation",
    "register_ray_target",
    "transition_ray_target_desired_state",
]
