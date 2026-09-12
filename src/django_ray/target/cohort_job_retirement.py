"""Private retirement of one independently cleaned-up Jobs probe reservation.

The source-owned parent must first evict this alias's qualification and confirm
terminal cleanup of its exact reserved remote Job outside every database lock.
``cleanup_confirmed`` acknowledges that prerequisite; neither this boolean nor
caller-created launch data authenticates cleanup. Keep the parent's one remote
operation until that independent confirmation. Never infer it from a local
helper exit, a stored receipt, expired proof, or an arbitrary Jobs status.

This service only CAS-retires the ephemeral challenge/receipt and issues fresh
discovery for the same declaration. It does not contact Ray, renew a lease,
withdraw sibling capabilities, change target policies, or alter task history.
The ordinary same-configuration replacement restriction remains unchanged.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Never

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction

from django_ray.models import RayTargetPolicyRevision, RayTargetProbeChallenge
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runtime.cohort_job import CohortProbeJobLease
from django_ray.runtime.cohort_job_entrypoint import (
    CohortProbeJobLaunch,
    decode_probe_job_launch,
    encode_probe_job_launch,
    probe_job_launch_entrypoint,
)
from django_ray.target import capabilities, coordination
from django_ray.target.attestation import (
    RayRunnerFamily,
    RayTargetExpectation,
    decode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_job_control import cohort_probe_entrypoint_digest
from django_ray.target.cohort_job_receipt import decode_cohort_job_receipt
from django_ray.target.cohort_job_receipt_storage import (
    CohortJobStorageError,
    _locked_reservation,
    _validate_reservation,
)
from django_ray.target.cohort_probe_challenges import (
    DEFAULT_PROBE_CHALLENGE_TTL_SECONDS,
    IssuedProbeChallenge,
    ProbeChallengeError,
    ProbeChallengeRejection,
    _digest,
    _expires_at,
    _issue_row,
    _locked_probe_lease,
    _locked_slot,
    _nonce_digest,
    _positive,
)


class CohortJobRetirementReason(StrEnum):
    INVALID = "invalid"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
    TRANSACTION_OPEN = "transaction_open"
    LEASE_UNAVAILABLE = "lease_unavailable"
    TARGET_CHANGED = "target_changed"
    CHALLENGE_CHANGED = "challenge_changed"
    NONCE_CHANGED = "nonce_changed"
    RESERVATION_CHANGED = "reservation_changed"
    CLOCK_REGRESSION = "clock_regression"
    EXPIRED = "expired"
    PERSISTENCE_REFUSED = "persistence_refused"


class CohortJobRetirementError(RuntimeError):
    def __init__(self, reason: CohortJobRetirementReason):
        self.reason = reason
        super().__init__(f"Cohort Job retirement refused: {reason.value}")


def _reject(reason: CohortJobRetirementReason) -> Never:
    raise CohortJobRetirementError(reason) from None


def _clock() -> datetime:
    return datetime.now(UTC)


def _fresh(previous: datetime) -> datetime:
    try:
        current = capabilities._now(_clock())
        if current < previous:
            raise ValueError
        return current
    except Exception:
        _reject(CohortJobRetirementReason.CLOCK_REGRESSION)


def _lease(identity, request, now, *, using):
    lease = _locked_probe_lease(identity, now, using=using)
    if lease.django_ray_version != request.expected_package_version:
        _reject(CohortJobRetirementReason.LEASE_UNAVAILABLE)
    return lease


def _lock_original_target(request, *, using, vendor):
    if request.expected_target_policy_id is None:
        return
    policy = (
        RayTargetPolicyRevision.objects.using(using)
        .filter(pk=request.expected_target_policy_id)
        .first()
    )
    if policy is None:
        _reject(CohortJobRetirementReason.TARGET_CHANGED)
    target = coordination._locked_target(target_key=policy.target_id, using=using, vendor=vendor)
    # Retirement accepts an obsolete/expired policy. It must still identify the
    # immutable original target; no current policy or drain state is modified.
    expected = RayTargetExpectation(
        request.target_key,
        RayRunnerFamily.RAY_JOB,
        request.expected_cluster_session,
        request.policy_revision,
        request.expected_runtime,
    )
    if (
        target.target_key != request.target_key
        or target.runner_family != RayRunnerFamily.RAY_JOB.value
        or target.cluster_session != request.expected_cluster_session
        or decode_ray_target_expectation(policy.expectation_json) != expected
        or policy.expectation_digest != ray_target_expectation_digest(expected)
    ):
        _reject(CohortJobRetirementReason.TARGET_CHANGED)


def retire_and_reissue_cohort_job_probe(
    identity: WorkerLeaseIdentity,
    launch: CohortProbeJobLaunch,
    *,
    expected_challenge_revision: int,
    nonce: str,
    expected_reserved_at: datetime,
    expected_receipt_digest: str | None,
    expected_received_at: datetime | None,
    cleanup_confirmed: bool,
    cleanup_confirmed_at: datetime,
    now: datetime,
    ttl_seconds: int = DEFAULT_PROBE_CHALLENGE_TTL_SECONDS,
    using: str = DEFAULT_DB_ALIAS,
) -> IssuedProbeChallenge:
    """Reissue discovery only after trusted, independently confirmed cleanup.

    The parent supplies its retained canonical launch and exact reservation
    snapshot from the operation it cleaned up. ``expected_challenge_revision``
    is the current pending or post-consumption revision, never a guessed latest
    row. Nullable receipt fields must match together. Old proof may be expired;
    its bytes still require canonical request/digest and chronology validation.

    This function owns an outer transaction: lease -> original target ->
    challenge -> receipt. Do not call it while holding task or unrelated locks.
    A lost return is uncertain: do not reconstruct the new bearer nonce from
    database state, recreate old requests, or automatically resubmit a Job.
    """
    if cleanup_confirmed is not True:
        _reject(CohortJobRetirementReason.CLEANUP_UNCONFIRMED)
    try:
        identity = capabilities._identity(identity)
        launch = decode_probe_job_launch(encode_probe_job_launch(launch))
        request = launch.request
        if request.lease != CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ):
            raise ValueError
        expected_challenge_revision = _positive(expected_challenge_revision)
        nonce_digest = _nonce_digest(nonce)
        now = capabilities._now(now)
        expected_reserved_at = capabilities._now(expected_reserved_at)
        cleanup_confirmed_at = capabilities._now(cleanup_confirmed_at)
        if (expected_receipt_digest is None) != (expected_received_at is None):
            raise ValueError
        if expected_receipt_digest is not None:
            _digest(expected_receipt_digest)
            expected_received_at = capabilities._now(expected_received_at)
        if not (
            request.issued_at <= expected_reserved_at < request.expires_at
            and expected_reserved_at
            <= (expected_received_at or expected_reserved_at)
            <= cleanup_confirmed_at
            <= now
        ):
            _reject(CohortJobRetirementReason.CLOCK_REGRESSION)
        _expires_at(now, ttl_seconds)
        vendor = capabilities._database_vendor(using=using)
        if connections[using].in_atomic_block or not connections[using].get_autocommit():
            _reject(CohortJobRetirementReason.TRANSACTION_OPEN)
    except CohortJobRetirementError:
        raise
    except Exception:
        _reject(CohortJobRetirementReason.INVALID)

    try:
        began = _fresh(now)
        with transaction.atomic(using=using, durable=True):
            lease = _lease(identity, request, began, using=using)
            _lock_original_target(request, using=using, vendor=vendor)
            current = _locked_slot(lease, request.challenge_id, using=using)
            if current is None or (
                current.schema_version != 1
                or current.runner_family != RayRunnerFamily.RAY_JOB.value
                or current.lease_hostname != identity.hostname
                or current.lease_pid != identity.pid
                or current.lease_started_at != identity.started_at
                or current.configuration_digest != request.configuration_digest
                or current.expected_target_policy_id != request.expected_target_policy_id
                or current.issued_at != request.issued_at
                or current.expires_at != request.expires_at
                or current.revision != expected_challenge_revision
                or current.revision
                != request.challenge_revision + int(current.consumed_at is not None)
            ):
                _reject(CohortJobRetirementReason.CHALLENGE_CHANGED)
            if not secrets.compare_digest(current.nonce_digest, nonce_digest):
                _reject(CohortJobRetirementReason.NONCE_CHANGED)
            if current.consumed_at is not None and not (
                request.issued_at <= current.consumed_at < request.expires_at
                and current.consumed_at <= began
            ):
                _reject(CohortJobRetirementReason.CLOCK_REGRESSION)
            reservation = _locked_reservation(request, using=using)
            _validate_reservation(reservation, request, jobs_endpoint=launch.jobs_endpoint)
            if (
                reservation.reserved_at != expected_reserved_at
                or reservation.receipt_digest != expected_receipt_digest
                or reservation.received_at != expected_received_at
                or reservation.entrypoint_digest
                != cohort_probe_entrypoint_digest(probe_job_launch_entrypoint(launch))
                or reservation.submitted_runtime_env_digest != launch.submitted_runtime_env_digest
            ):
                _reject(CohortJobRetirementReason.RESERVATION_CHANGED)
            if expected_receipt_digest is None:
                if reservation.receipt_json is not None:
                    _reject(CohortJobRetirementReason.RESERVATION_CHANGED)
            else:
                assert expected_received_at is not None
                receipt = decode_cohort_job_receipt(
                    reservation.receipt_json,
                    expected_request=request,
                    expected_request_digest=launch.request_digest,
                    expected_submission_id=reservation.submission_id,
                    expected_receipt_digest=expected_receipt_digest,
                )
                if not (
                    expected_reserved_at
                    <= receipt.attestation.observed_at
                    <= receipt.collected_at
                    <= expected_received_at
                    <= cleanup_confirmed_at
                ):
                    _reject(CohortJobRetirementReason.CLOCK_REGRESSION)
            # Validate the clock and lease again after all locks and bounded
            # receipt decoding, before removing either immutable old row.
            fresh = _fresh(began)
            _lease(identity, request, fresh, using=using)
            retired_id = current.pk
            deleted, counts = (
                RayTargetProbeChallenge.objects.using(using)
                .filter(
                    pk=retired_id, revision=expected_challenge_revision, nonce_digest=nonce_digest
                )
                .delete()
            )
            if deleted != 2 or counts.get(RayTargetProbeChallenge._meta.label) != 1:
                _reject(CohortJobRetirementReason.PERSISTENCE_REFUSED)
            replacement = RayTargetProbeChallenge(
                lease=lease,
                lease_hostname=identity.hostname,
                lease_pid=identity.pid,
                lease_started_at=identity.started_at,
                runner_family=RayRunnerFamily.RAY_JOB.value,
                revision=1,
            )
            issued = _issue_row(
                replacement,
                configuration_digest=request.configuration_digest,
                policy=None,
                now=fresh,
                expires_at=_expires_at(fresh, ttl_seconds),
                using=using,
            )
            # SQLite AUTOINCREMENT / PostgreSQL sequences supply fresh IDs.
            # Refuse allocator regression or repeated entropy rather than
            # allow an old request or bearer nonce to become meaningful again.
            if issued.receipt.challenge_id <= retired_id or secrets.compare_digest(
                issued.nonce, nonce
            ):
                _reject(CohortJobRetirementReason.PERSISTENCE_REFUSED)
            finished = _fresh(fresh)
            _lease(identity, request, finished, using=using)
            if finished >= issued.receipt.expires_at:
                _reject(CohortJobRetirementReason.EXPIRED)
            return issued
    except CohortJobRetirementError:
        raise
    except ProbeChallengeError as error:
        reason = {
            ProbeChallengeRejection.LEASE_UNAVAILABLE: CohortJobRetirementReason.LEASE_UNAVAILABLE,
            ProbeChallengeRejection.CLOCK_REGRESSION: CohortJobRetirementReason.CLOCK_REGRESSION,
        }.get(error.classification, CohortJobRetirementReason.PERSISTENCE_REFUSED)
        _reject(reason)
    except CohortJobStorageError:
        _reject(CohortJobRetirementReason.RESERVATION_CHANGED)
    except coordination.RayTargetCoordinationError:
        _reject(CohortJobRetirementReason.TARGET_CHANGED)
    except (DatabaseError, capabilities.RayWorkerTargetCapabilityError):
        _reject(CohortJobRetirementReason.PERSISTENCE_REFUSED)
    except Exception:
        _reject(CohortJobRetirementReason.RESERVATION_CHANGED)
