"""Private database-only challenges for current-cohort target discovery.

The caller supplies a digest covering trusted endpoint/backend, runner family,
package and exact Ray/Python expectations. Initial discovery has no invented
cluster session. Issuance and consumption never advertise target eligibility.

Standalone operations own durable transactions. Private locked helpers allow a
future positive completion service to consume and publish attestation/capability
atomically. Lock exact lease before optional existing target and challenge.
Never perform network operations or renew a worker heartbeat here.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import NoReturn

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.models import F

from django_ray.models import (
    RAY_JOB_WORKER_TARGET_CAPABILITY_LIMIT,
    RAY_TARGET_PROBE_CHALLENGE_MAX_TTL_SECONDS,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayTargetProbeJobReceipt,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.target.attestation import RAY_TARGET_ATTESTATION_MAX_COUNTER, RayRunnerFamily
from django_ray.target.capabilities import (
    RayWorkerTargetCapabilityError,
    _database_vendor,
    _identity,
    _locked_exact_lease,
    _now,
    _require_advertising_lease,
    _require_outermost_transaction,
)
from django_ray.target.coordination import (
    RayTargetCoordinationError,
    _latest_policy,
    _locked_target,
)

DEFAULT_PROBE_CHALLENGE_TTL_SECONDS = 300
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{64}")


class ProbeChallengeRejection(StrEnum):
    """Fixed, secret-free failure classifications for the private seam."""

    INVALID_ARGUMENT = "invalid_argument"
    LEASE_UNAVAILABLE = "lease_unavailable"
    TARGET_UNAVAILABLE = "target_unavailable"
    POLICY_CHANGED = "policy_changed"
    CHALLENGE_CHANGED = "challenge_changed"
    CONFIGURATION_CHANGED = "configuration_changed"
    NONCE_CHANGED = "nonce_changed"
    ALREADY_CONSUMED = "already_consumed"
    EXPIRED = "expired"
    CLOCK_REGRESSION = "clock_regression"
    REVISION_EXHAUSTED = "revision_exhausted"
    LIMIT_EXCEEDED = "limit_exceeded"
    PERSISTENCE_REFUSED = "persistence_refused"


class ProbeChallengeError(RuntimeError):
    """A bounded refusal without configuration, endpoint or nonce values."""

    def __init__(self, classification: ProbeChallengeRejection) -> None:
        self.classification = classification
        super().__init__(f"Ray target probe challenge rejected: {classification.value}")


@dataclass(frozen=True, slots=True)
class ProbeChallengeReceipt:
    """Current slot identity; contains no bearer nonce or fabricated target."""

    challenge_id: int
    configuration_digest: str
    runner_family: RayRunnerFamily
    expected_target_policy_id: int | None
    revision: int
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class IssuedProbeChallenge:
    """Issuance result; the raw nonce is intentionally absent from repr."""

    receipt: ProbeChallengeReceipt
    nonce: str = field(repr=False)


def _reject(classification: ProbeChallengeRejection) -> NoReturn:
    raise ProbeChallengeError(classification)


def _positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= RAY_TARGET_ATTESTATION_MAX_COUNTER:
        _reject(ProbeChallengeRejection.INVALID_ARGUMENT)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _reject(ProbeChallengeRejection.INVALID_ARGUMENT)
    return value


def _nonce_digest(value: object) -> str:
    if type(value) is not str or _NONCE.fullmatch(value) is None:
        _reject(ProbeChallengeRejection.INVALID_ARGUMENT)
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _canonical_identity_time(identity, now):
    try:
        return _identity(identity), _now(now)
    except RayWorkerTargetCapabilityError:
        raise ProbeChallengeError(ProbeChallengeRejection.INVALID_ARGUMENT) from None


def _require_transaction(*, using: str) -> str:
    vendor = _database_vendor(using=using)
    if not connections[using].in_atomic_block:
        _reject(ProbeChallengeRejection.PERSISTENCE_REFUSED)
    return vendor


def _locked_probe_lease(
    identity: WorkerLeaseIdentity, now: datetime, *, using: str
) -> TaskWorkerLease:
    """Lock the exact live lease first inside the caller's outer transaction."""
    vendor = _require_transaction(using=using)
    try:
        lease = _require_advertising_lease(
            _locked_exact_lease(identity, using=using, vendor=vendor), now=now
        )
    except RayWorkerTargetCapabilityError:
        raise ProbeChallengeError(ProbeChallengeRejection.LEASE_UNAVAILABLE) from None
    if now < lease.started_at or now < lease.last_heartbeat_at:
        _reject(ProbeChallengeRejection.CLOCK_REGRESSION)
    return lease


def _locked_expected_policy(policy_id, family, now, *, using):
    if policy_id is None:
        return None
    _positive(policy_id)
    selected = RayTargetPolicyRevision.objects.using(using).filter(pk=policy_id).first()
    if selected is None:
        _reject(ProbeChallengeRejection.TARGET_UNAVAILABLE)
    try:
        target = _locked_target(
            target_key=str(selected.target_id), using=using, vendor=_database_vendor(using=using)
        )
        policy, expectation, state = _latest_policy(target, using=using)
    except RayTargetCoordinationError:
        raise ProbeChallengeError(ProbeChallengeRejection.TARGET_UNAVAILABLE) from None
    if policy.pk != policy_id:
        _reject(ProbeChallengeRejection.POLICY_CHANGED)
    if expectation.runner_family is not family or state not in {
        RayTargetDesiredState.ACTIVE,
        RayTargetDesiredState.DRAINING,
    }:
        _reject(ProbeChallengeRejection.TARGET_UNAVAILABLE)
    if now < policy.created_at:
        _reject(ProbeChallengeRejection.CLOCK_REGRESSION)
    return policy


def _locked_slot(lease, challenge_id, *, using):
    queryset = RayTargetProbeChallenge.objects.using(using).filter(pk=challenge_id, lease=lease)
    if connections[using].vendor == "sqlite":
        queryset.update(revision=F("revision"))
        return queryset.first()
    return queryset.select_for_update().first()


def _lock_probe_challenge_for_completion(
    identity: WorkerLeaseIdentity,
    challenge_id: int,
    *,
    configuration_digest: str,
    expected_revision: int,
    nonce: str,
    now: datetime,
    using: str = DEFAULT_DB_ALIAS,
    allow_consumed_or_expired: bool = False,
) -> RayTargetProbeChallenge:
    """Revalidate and lock an exact challenge inside an existing transaction.

    Retain that transaction through consumption and positive proof publication.
    This helper does not authenticate observations. Replacement may inspect an
    expired/consumed slot; ordinary completion may not.
    """
    identity, now = _canonical_identity_time(identity, now)
    challenge_id = _positive(challenge_id)
    expected_revision = _positive(expected_revision)
    configuration_digest = _digest(configuration_digest)
    nonce_digest = _nonce_digest(nonce)
    lease = _locked_probe_lease(identity, now, using=using)
    preview = (
        RayTargetProbeChallenge.objects.using(using).filter(pk=challenge_id, lease=lease).first()
    )
    if preview is None:
        _reject(ProbeChallengeRejection.CHALLENGE_CHANGED)
    if not allow_consumed_or_expired:
        _locked_expected_policy(
            preview.expected_target_policy_id,
            RayRunnerFamily(preview.runner_family),
            now,
            using=using,
        )
    current = _locked_slot(lease, challenge_id, using=using)
    if (
        current is None
        or current.revision != expected_revision
        or (
            current.lease_hostname != identity.hostname
            or current.lease_pid != identity.pid
            or current.lease_started_at != identity.started_at
            or current.expected_target_policy_id != preview.expected_target_policy_id
        )
    ):
        _reject(ProbeChallengeRejection.CHALLENGE_CHANGED)
    if current.configuration_digest != configuration_digest:
        _reject(ProbeChallengeRejection.CONFIGURATION_CHANGED)
    if not secrets.compare_digest(current.nonce_digest, nonce_digest):
        _reject(ProbeChallengeRejection.NONCE_CHANGED)
    if current.revision >= RAY_TARGET_ATTESTATION_MAX_COUNTER:
        _reject(ProbeChallengeRejection.REVISION_EXHAUSTED)
    if now < current.issued_at or (current.consumed_at is not None and now < current.consumed_at):
        _reject(ProbeChallengeRejection.CLOCK_REGRESSION)
    if not allow_consumed_or_expired:
        if current.consumed_at is not None:
            _reject(ProbeChallengeRejection.ALREADY_CONSUMED)
        if now >= current.expires_at:
            _reject(ProbeChallengeRejection.EXPIRED)
    return current


def _receipt(row):
    return ProbeChallengeReceipt(
        challenge_id=int(row.pk),
        configuration_digest=row.configuration_digest,
        runner_family=RayRunnerFamily(row.runner_family),
        expected_target_policy_id=row.expected_target_policy_id,
        revision=row.revision,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
    )


def _expires_at(now, ttl_seconds):
    if (
        type(ttl_seconds) is not int
        or not 1 <= ttl_seconds <= RAY_TARGET_PROBE_CHALLENGE_MAX_TTL_SECONDS
    ):
        _reject(ProbeChallengeRejection.INVALID_ARGUMENT)
    try:
        return now + timedelta(seconds=ttl_seconds)
    except OverflowError:
        raise ProbeChallengeError(ProbeChallengeRejection.INVALID_ARGUMENT) from None


def _issue_row(row, *, configuration_digest, policy, now, expires_at, using):
    nonce = secrets.token_hex(32)
    row.nonce_digest = _nonce_digest(nonce)
    row.configuration_digest = configuration_digest
    row.expected_target_policy = policy
    row.issued_at = now
    row.expires_at = expires_at
    row.consumed_at = None
    row.save(using=using)
    return IssuedProbeChallenge(_receipt(row), nonce)


def issue_ray_target_probe_challenge(
    identity: WorkerLeaseIdentity,
    configuration_digest: str,
    *,
    runner_family: RayRunnerFamily,
    now: datetime,
    expected_target_policy_id: int | None = None,
    ttl_seconds: int = DEFAULT_PROBE_CHALLENGE_TTL_SECONDS,
    using: str = DEFAULT_DB_ALIAS,
) -> IssuedProbeChallenge:
    """Issue a first-discovery or registered-target refresh challenge."""
    identity, now = _canonical_identity_time(identity, now)
    configuration_digest = _digest(configuration_digest)
    if type(runner_family) is not RayRunnerFamily:
        _reject(ProbeChallengeRejection.INVALID_ARGUMENT)
    expires_at = _expires_at(now, ttl_seconds)
    try:
        _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            lease = _locked_probe_lease(identity, now, using=using)
            policy = _locked_expected_policy(
                expected_target_policy_id, runner_family, now, using=using
            )
            slots = RayTargetProbeChallenge.objects.using(using).filter(lease=lease)
            if slots.filter(configuration_digest=configuration_digest).exists():
                _reject(ProbeChallengeRejection.CHALLENGE_CHANGED)
            if (
                (runner_family is RayRunnerFamily.RAY_CORE and slots.exists())
                or slots.exclude(runner_family=runner_family.value).exists()
                or slots.count() >= RAY_JOB_WORKER_TARGET_CAPABILITY_LIMIT
            ):
                _reject(ProbeChallengeRejection.LIMIT_EXCEEDED)
            row = RayTargetProbeChallenge(
                lease=lease,
                lease_hostname=identity.hostname,
                lease_pid=identity.pid,
                lease_started_at=identity.started_at,
                runner_family=runner_family.value,
                revision=1,
            )
            return _issue_row(
                row,
                configuration_digest=configuration_digest,
                policy=policy,
                now=now,
                expires_at=expires_at,
                using=using,
            )
    except (DatabaseError, RayWorkerTargetCapabilityError):
        raise ProbeChallengeError(ProbeChallengeRejection.PERSISTENCE_REFUSED) from None


def replace_ray_target_probe_challenge(
    identity: WorkerLeaseIdentity,
    challenge_id: int,
    *,
    expected_configuration_digest: str,
    configuration_digest: str,
    expected_revision: int,
    expected_nonce: str,
    now: datetime,
    expected_target_policy_id: int | None = None,
    ttl_seconds: int = DEFAULT_PROBE_CHALLENGE_TTL_SECONDS,
    using: str = DEFAULT_DB_ALIAS,
) -> IssuedProbeChallenge:
    """CAS-replace a slot, rotating nonce even for unchanged configuration.

    An unchanged configuration cannot forget or swap a known target. Explicitly
    replacing the trusted configuration may begin first discovery again.
    """
    identity, now = _canonical_identity_time(identity, now)
    challenge_id = _positive(challenge_id)
    configuration_digest = _digest(configuration_digest)
    expires_at = _expires_at(now, ttl_seconds)
    try:
        _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            lease = _locked_probe_lease(identity, now, using=using)
            preview = (
                RayTargetProbeChallenge.objects.using(using)
                .filter(pk=challenge_id, lease=lease)
                .first()
            )
            if preview is None:
                _reject(ProbeChallengeRejection.CHALLENGE_CHANGED)
            policy = _locked_expected_policy(
                expected_target_policy_id,
                RayRunnerFamily(preview.runner_family),
                now,
                using=using,
            )
            current = _lock_probe_challenge_for_completion(
                identity,
                challenge_id,
                configuration_digest=expected_configuration_digest,
                expected_revision=expected_revision,
                nonce=expected_nonce,
                now=now,
                using=using,
                allow_consumed_or_expired=True,
            )
            if (
                current.configuration_digest == configuration_digest
                and current.expected_target_policy_id
            ):
                old_policy = RayTargetPolicyRevision.objects.using(using).get(
                    pk=current.expected_target_policy_id
                )
                if policy is None or old_policy.target_id != policy.target_id:
                    _reject(ProbeChallengeRejection.TARGET_UNAVAILABLE)
            reservation = (
                RayTargetProbeJobReceipt.objects.using(using)
                .select_for_update()
                .filter(challenge=current)
                .first()
            )
            if reservation is not None:
                if now < (reservation.received_at or reservation.reserved_at):
                    _reject(ProbeChallengeRejection.CLOCK_REGRESSION)
                reservation.delete(using=using)
            current.revision = int(current.revision) + 1
            return _issue_row(
                current,
                configuration_digest=configuration_digest,
                policy=policy,
                now=now,
                expires_at=expires_at,
                using=using,
            )
    except (DatabaseError, RayWorkerTargetCapabilityError):
        raise ProbeChallengeError(ProbeChallengeRejection.PERSISTENCE_REFUSED) from None


def _consume_locked_probe_challenge(
    current: RayTargetProbeChallenge, *, now: datetime, using: str = DEFAULT_DB_ALIAS
) -> ProbeChallengeReceipt:
    """Consume immediately after locked validation within the same transaction.

    A surrounding failure rolls consumption back alongside proof publication.
    """
    _require_transaction(using=using)
    try:
        now = _now(now)
    except RayWorkerTargetCapabilityError:
        raise ProbeChallengeError(ProbeChallengeRejection.INVALID_ARGUMENT) from None
    if current.consumed_at is not None:
        _reject(ProbeChallengeRejection.ALREADY_CONSUMED)
    if now < current.issued_at:
        _reject(ProbeChallengeRejection.CLOCK_REGRESSION)
    if now >= current.expires_at:
        _reject(ProbeChallengeRejection.EXPIRED)
    changed = (
        RayTargetProbeChallenge.objects.using(using)
        .filter(
            pk=current.pk,
            lease_id=current.lease_id,
            revision=current.revision,
            configuration_digest=current.configuration_digest,
            nonce_digest=current.nonce_digest,
            consumed_at__isnull=True,
        )
        .update(consumed_at=now, revision=F("revision") + 1)
    )
    if changed != 1:
        _reject(ProbeChallengeRejection.CHALLENGE_CHANGED)
    current.consumed_at = now
    current.revision += 1
    return _receipt(current)


def consume_ray_target_probe_challenge(
    identity: WorkerLeaseIdentity,
    challenge_id: int,
    *,
    configuration_digest: str,
    expected_revision: int,
    nonce: str,
    now: datetime,
    using: str = DEFAULT_DB_ALIAS,
) -> ProbeChallengeReceipt:
    """Consume one exact, unexpired challenge once; never publish capacity."""
    try:
        _database_vendor(using=using)
        _require_outermost_transaction(using=using)
        with transaction.atomic(using=using, durable=True):
            current = _lock_probe_challenge_for_completion(
                identity,
                challenge_id,
                configuration_digest=configuration_digest,
                expected_revision=expected_revision,
                nonce=nonce,
                now=now,
                using=using,
            )
            return _consume_locked_probe_challenge(current, now=now, using=using)
    except (DatabaseError, RayWorkerTargetCapabilityError):
        raise ProbeChallengeError(ProbeChallengeRejection.PERSISTENCE_REFUSED) from None
