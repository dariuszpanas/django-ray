"""Private reservation/receipt storage; no probe consumption or claim authority.

Every mutation owns an outer durable transaction. Locks follow the existing
lease, optional target, challenge, receipt order; freshness is rechecked after
all locks. Nothing here contacts Ray or initializes Django.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Never
from urllib.parse import urlsplit

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction

from django_ray.models import RayTargetProbeChallenge, RayTargetProbeJobReceipt
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runtime.cohort_job import (
    CohortProbeJobError,
    CohortProbeJobLease,
    CohortProbeJobRequest,
    decode_probe_job_request,
    encode_probe_job_request,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.target.attestation import (
    RayRunnerFamily,
    compare_ray_target_attestation,
    decode_ray_target_expectation,
)
from django_ray.target.capabilities import (
    RayWorkerTargetCapabilityError,
    _database_vendor,
    _identity,
    _require_outermost_transaction,
)
from django_ray.target.capabilities import (
    _now as _canonical_time,
)
from django_ray.target.cohort_intent import _digest, _endpoint
from django_ray.target.cohort_job_control import (
    CohortJobReservationSnapshot,
    cohort_probe_entrypoint_digest,
    cohort_probe_submitted_runtime_env_digest,
)
from django_ray.target.cohort_job_receipt import (
    CohortJobReceipt,
    cohort_job_receipt_digest,
    decode_cohort_job_receipt,
    encode_cohort_job_receipt,
)
from django_ray.target.cohort_probe_challenges import (
    ProbeChallengeError,
    _lock_probe_challenge_for_completion,
    _locked_expected_policy,
    _locked_probe_lease,
    _locked_slot,
)


class CohortJobStorageRejection(StrEnum):
    INVALID = "invalid"
    TRANSACTION_OPEN = "transaction_open"
    CHALLENGE_UNAVAILABLE = "challenge_unavailable"
    RESERVATION_UNAVAILABLE = "reservation_unavailable"
    RESERVATION_MISMATCH = "reservation_mismatch"
    RECEIPT_MISMATCH = "receipt_mismatch"
    CLOCK_REGRESSION = "clock_regression"
    EXPIRED = "expired"
    PERSISTENCE_REFUSED = "persistence_refused"


class CohortJobStorageError(RuntimeError):
    """Fixed diagnostics never echo nonce, request, endpoint or RuntimeEnv."""

    def __init__(self, classification: CohortJobStorageRejection) -> None:
        self.classification = classification
        super().__init__(f"Cohort Job storage rejected: {classification.value}")


@dataclass(frozen=True, slots=True)
class CohortJobReservationChange:
    changed: bool
    challenge_id: int
    challenge_revision: int
    request_digest: str
    submission_id: str
    reserved_at: datetime


def _reject(classification: CohortJobStorageRejection) -> Never:
    raise CohortJobStorageError(classification) from None


def _now() -> datetime:
    return datetime.now(UTC)


def _fresh_time(previous: datetime) -> datetime:
    now = _canonical_time(_now())
    if now < previous:
        _reject(CohortJobStorageRejection.CLOCK_REGRESSION)
    return now


def _preflight(identity, request, *, using):
    try:
        identity = _identity(identity)
        canonical = decode_probe_job_request(encode_probe_job_request(request))
        if canonical.lease != CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ):
            _reject(CohortJobStorageRejection.RESERVATION_MISMATCH)
        _database_vendor(using=using)
        if connections[using].in_atomic_block:
            _reject(CohortJobStorageRejection.TRANSACTION_OPEN)
        _require_outermost_transaction(using=using)
        return identity, canonical
    except (CohortProbeJobError, RayWorkerTargetCapabilityError, TypeError, ValueError):
        _reject(CohortJobStorageRejection.INVALID)


def _jobs_endpoint(value: object) -> str:
    try:
        endpoint = _endpoint(value)
        parsed = urlsplit(endpoint)
        if (
            len(endpoint) > 2048
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(marker in endpoint for marker in ("?", "#", "%", "\\"))
            or any(not 33 <= ord(character) <= 126 for character in endpoint)
        ):
            raise ValueError
        # Accessing the property also rejects malformed/out-of-range ports.
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        return endpoint
    except (TypeError, ValueError):
        _reject(CohortJobStorageRejection.INVALID)


def _validate_probe(probe, identity, request, *, now):
    if probe is None or (
        probe.lease_id != identity.worker_id
        or probe.lease_hostname != identity.hostname
        or probe.lease_pid != identity.pid
        or probe.lease_started_at != identity.started_at
        or probe.revision != request.challenge_revision
        or probe.configuration_digest != request.configuration_digest
        or probe.runner_family != RayRunnerFamily.RAY_JOB.value
        or probe.expected_target_policy_id != request.expected_target_policy_id
        or probe.issued_at != request.issued_at
        or probe.expires_at != request.expires_at
        or probe.consumed_at is not None
    ):
        _reject(CohortJobStorageRejection.CHALLENGE_UNAVAILABLE)
    if now < probe.issued_at:
        _reject(CohortJobStorageRejection.CLOCK_REGRESSION)
    if now >= probe.expires_at:
        _reject(CohortJobStorageRejection.EXPIRED)


def _validate_refresh_policy(request, *, now, using):
    policy = _locked_expected_policy(
        request.expected_target_policy_id, RayRunnerFamily.RAY_JOB, now, using=using
    )
    if policy is not None:
        expectation = decode_ray_target_expectation(policy.expectation_json)
        if (
            expectation.target_key != request.target_key
            or expectation.policy_revision != request.policy_revision
            or expectation.cluster_session != request.expected_cluster_session
            or expectation.runtime != request.expected_runtime
        ):
            _reject(CohortJobStorageRejection.RESERVATION_MISMATCH)


def _lock_pending_request(identity, request, *, now, using):
    lease = _locked_probe_lease(identity, now, using=using)
    preview = (
        RayTargetProbeChallenge.objects.using(using)
        .filter(pk=request.challenge_id, lease=lease)
        .first()
    )
    _validate_probe(preview, identity, request, now=now)
    _validate_refresh_policy(request, now=now, using=using)
    current = _locked_slot(lease, request.challenge_id, using=using)
    _validate_probe(current, identity, request, now=now)
    return current


def _locked_reservation(request, *, using):
    # SQLite already holds the exact lease's writer fence. Receipt no-op
    # updates are intentionally forbidden by the immutable row trigger.
    query = RayTargetProbeJobReceipt.objects.using(using).filter(pk=request.challenge_id)
    return query.select_for_update().first()


def _validate_reservation(row, request, *, jobs_endpoint=None):
    if row is None:
        _reject(CohortJobStorageRejection.RESERVATION_UNAVAILABLE)
    try:
        if (
            row.challenge_revision != request.challenge_revision
            or row.request_json != encode_probe_job_request(request)
            or row.request_digest != probe_job_request_digest(request)
            or row.submission_id != probe_job_submission_id(request)
            or _jobs_endpoint(row.ray_address) != row.ray_address
            or (jobs_endpoint is not None and row.ray_address != jobs_endpoint)
        ):
            _reject(CohortJobStorageRejection.RESERVATION_MISMATCH)
        _digest(row.entrypoint_digest)
        _digest(row.submitted_runtime_env_digest)
        _canonical_time(row.reserved_at)
        if not request.issued_at <= row.reserved_at < request.expires_at:
            _reject(CohortJobStorageRejection.RESERVATION_MISMATCH)
    except (TypeError, ValueError, RayWorkerTargetCapabilityError):
        _reject(CohortJobStorageRejection.RESERVATION_MISMATCH)


def _validated_receipt(row, request, *, now):
    try:
        receipt = decode_cohort_job_receipt(
            row.receipt_json,
            expected_request=request,
            expected_request_digest=row.request_digest,
            expected_submission_id=row.submission_id,
            expected_receipt_digest=row.receipt_digest,
        )
        received_at = _canonical_time(row.received_at)
        if (
            not row.reserved_at
            <= receipt.attestation.observed_at
            <= receipt.collected_at
            <= received_at
            <= now
        ):
            _reject(CohortJobStorageRejection.CLOCK_REGRESSION)
        compare_ray_target_attestation(
            receipt.attestation.expectation, receipt.attestation, now=now
        )
        return receipt
    except (TypeError, ValueError, RayWorkerTargetCapabilityError):
        _reject(CohortJobStorageRejection.RECEIPT_MISMATCH)


def reserve_cohort_job_probe(
    identity: WorkerLeaseIdentity,
    request: CohortProbeJobRequest,
    *,
    nonce: str,
    jobs_endpoint: str,
    entrypoint: str,
    submitted_runtime_env: dict,
    using: str = DEFAULT_DB_ALIAS,
) -> CohortJobReservationChange:
    """Reserve exact manager-owned submission facts, without submitting a Job."""
    identity, request = _preflight(identity, request, using=using)
    endpoint = _jobs_endpoint(jobs_endpoint)
    try:
        entrypoint_digest = cohort_probe_entrypoint_digest(entrypoint)
        environment_digest = cohort_probe_submitted_runtime_env_digest(submitted_runtime_env)
        began = _canonical_time(_now())
        with transaction.atomic(using=using, durable=True):
            probe = _lock_probe_challenge_for_completion(
                identity,
                request.challenge_id,
                configuration_digest=request.configuration_digest,
                expected_revision=request.challenge_revision,
                nonce=nonce,
                now=began,
                using=using,
            )
            _validate_probe(probe, identity, request, now=began)
            _validate_refresh_policy(request, now=began, using=using)
            row = _locked_reservation(request, using=using)
            now = _fresh_time(began)
            _locked_probe_lease(identity, now, using=using)
            _validate_probe(probe, identity, request, now=now)
            changed = row is None
            if row is None:
                row = RayTargetProbeJobReceipt.objects.using(using).create(
                    challenge=probe,
                    challenge_revision=request.challenge_revision,
                    request_json=encode_probe_job_request(request),
                    request_digest=probe_job_request_digest(request),
                    ray_address=endpoint,
                    submission_id=probe_job_submission_id(request),
                    entrypoint_digest=entrypoint_digest,
                    submitted_runtime_env_digest=environment_digest,
                    reserved_at=now,
                )
            else:
                _validate_reservation(row, request, jobs_endpoint=endpoint)
                if (
                    row.entrypoint_digest != entrypoint_digest
                    or row.submitted_runtime_env_digest != environment_digest
                ):
                    _reject(CohortJobStorageRejection.RESERVATION_MISMATCH)
                if now < row.reserved_at:
                    _reject(CohortJobStorageRejection.CLOCK_REGRESSION)
            return CohortJobReservationChange(
                changed,
                request.challenge_id,
                request.challenge_revision,
                row.request_digest,
                row.submission_id,
                row.reserved_at,
            )
    except ProbeChallengeError:
        _reject(CohortJobStorageRejection.CHALLENGE_UNAVAILABLE)
    except DatabaseError:
        _reject(CohortJobStorageRejection.PERSISTENCE_REFUSED)
    except (TypeError, ValueError, RayWorkerTargetCapabilityError, RuntimeError) as error:
        if isinstance(error, CohortJobStorageError):
            raise
        _reject(CohortJobStorageRejection.INVALID)


def write_cohort_job_receipt(receipt: CohortJobReceipt, *, using: str = DEFAULT_DB_ALIAS) -> bool:
    """Store one canonical driver observation; the manager nonce is never read."""
    try:
        serialized = encode_cohort_job_receipt(receipt)
        digest = cohort_job_receipt_digest(receipt)
        lease = receipt.request.lease
        identity, request = _preflight(
            WorkerLeaseIdentity(lease.worker_id, lease.hostname, lease.pid, lease.started_at),
            receipt.request,
            using=using,
        )
        began = _canonical_time(_now())
        with transaction.atomic(using=using, durable=True):
            probe = _lock_pending_request(identity, request, now=began, using=using)
            row = _locked_reservation(request, using=using)
            now = _fresh_time(began)
            _locked_probe_lease(identity, now, using=using)
            _validate_probe(probe, identity, request, now=now)
            _validate_reservation(row, request)
            if row.receipt_json is not None:
                _validated_receipt(row, request, now=now)
                if row.receipt_json != serialized or row.receipt_digest != digest:
                    _reject(CohortJobStorageRejection.RECEIPT_MISMATCH)
                return False
            if (
                not row.reserved_at
                <= receipt.attestation.observed_at
                <= receipt.collected_at
                <= now
            ):
                _reject(CohortJobStorageRejection.CLOCK_REGRESSION)
            compare_ray_target_attestation(
                receipt.attestation.expectation, receipt.attestation, now=now
            )
            changed = (
                RayTargetProbeJobReceipt.objects.using(using)
                .filter(
                    pk=row.pk,
                    challenge_revision=request.challenge_revision,
                    request_digest=row.request_digest,
                    receipt_json__isnull=True,
                    receipt_digest__isnull=True,
                    received_at__isnull=True,
                )
                .update(receipt_json=serialized, receipt_digest=digest, received_at=now)
            )
            if changed != 1:
                _reject(CohortJobStorageRejection.RECEIPT_MISMATCH)
            return True
    except ProbeChallengeError:
        _reject(CohortJobStorageRejection.CHALLENGE_UNAVAILABLE)
    except DatabaseError:
        _reject(CohortJobStorageRejection.PERSISTENCE_REFUSED)
    except (TypeError, ValueError, RayWorkerTargetCapabilityError):
        _reject(CohortJobStorageRejection.RECEIPT_MISMATCH)


def read_cohort_job_reservation(
    identity: WorkerLeaseIdentity,
    request: CohortProbeJobRequest,
    *,
    jobs_endpoint: str,
    using: str = DEFAULT_DB_ALIAS,
) -> CohortJobReservationSnapshot | None:
    """Detach a validated received reservation, or return None while pending.

    This snapshot is not publication authority. The manager must corroborate
    the actual Job outside this transaction and later revalidate its rows.
    """
    identity, request = _preflight(identity, request, using=using)
    endpoint = _jobs_endpoint(jobs_endpoint)
    try:
        began = _canonical_time(_now())
        with transaction.atomic(using=using, durable=True):
            probe = _lock_pending_request(identity, request, now=began, using=using)
            row = _locked_reservation(request, using=using)
            now = _fresh_time(began)
            _locked_probe_lease(identity, now, using=using)
            _validate_probe(probe, identity, request, now=now)
            _validate_reservation(row, request, jobs_endpoint=endpoint)
            if row.receipt_json is None:
                if row.receipt_digest is not None or row.received_at is not None:
                    _reject(CohortJobStorageRejection.RECEIPT_MISMATCH)
                return None
            _validated_receipt(row, request, now=now)
            return CohortJobReservationSnapshot(
                request=request,
                request_digest=row.request_digest,
                jobs_endpoint=row.ray_address,
                entrypoint_digest=row.entrypoint_digest,
                submitted_runtime_env_digest=row.submitted_runtime_env_digest,
                receipt_json=row.receipt_json,
                receipt_digest=row.receipt_digest,
            )
    except ProbeChallengeError:
        _reject(CohortJobStorageRejection.CHALLENGE_UNAVAILABLE)
    except DatabaseError:
        _reject(CohortJobStorageRejection.PERSISTENCE_REFUSED)
    except (TypeError, ValueError, RayWorkerTargetCapabilityError):
        _reject(CohortJobStorageRejection.INVALID)
