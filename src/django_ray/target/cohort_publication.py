"""Private probe verification and atomic publication for the current cohort.

These entry points obtain observations themselves, outside database locks.
Publication then revalidates the exact live lease, manager-held nonce, current
challenge and optional Jobs reservation before committing all positive records
and challenge consumption together. No producer or worker calls this module.
An explicit manager bootstrap option may activate only a target created within
the same transaction. The published proof/capability remain at draining policy 1;
the new active policy 2 always requires a fresh proof before it grants capacity.
Existing targets, including explicitly drained ones, are never auto-enabled.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Never

from django.db import DEFAULT_DB_ALIAS, DatabaseError, transaction

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayWorkerTargetCapability,
)
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runtime.cohort_job import (
    CohortProbeJobLease,
    CohortProbeJobRequest,
)
from django_ray.target import capabilities, coordination
from django_ray.target.attestation import (
    RayClusterAttestation,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    compare_ray_target_attestation,
    decode_ray_target_expectation,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_claim import (
    CohortJobQualificationProvenance,
    validate_cohort_job_qualification,
)
from django_ray.target.cohort_contract import _package_version
from django_ray.target.cohort_job_control import (
    CohortJobReservationSnapshot,
    InspectedCohortJobReceipt,
    _outside_transactions,
    cohort_probe_entrypoint_digest,
    inspect_reserved_cohort_job,
)
from django_ray.target.cohort_job_receipt_storage import (
    _locked_reservation,
    _validate_reservation,
    _validated_receipt,
    read_cohort_job_reservation,
)
from django_ray.target.cohort_probe import observe_current_cohort_target
from django_ray.target.cohort_probe_challenges import (
    ProbeChallengeReceipt,
    _consume_locked_probe_challenge,
    _digest,
    _lock_probe_challenge_for_completion,
    _locked_expected_policy,
    _locked_probe_lease,
    _nonce_digest,
    _positive,
    _receipt,
)
from django_ray.target.cohort_runtime import _local_runtime
from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

if TYPE_CHECKING:
    from django_ray.runtime.cohort_job_entrypoint import CohortProbeJobLaunch


class CohortPublicationReason(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    RUNTIME_MISMATCH = "runtime_mismatch"
    CHALLENGE_CHANGED = "challenge_changed"
    POLICY_CHANGED = "policy_changed"
    RECEIPT_CHANGED = "receipt_changed"
    CLOCK_REGRESSION = "clock_regression"
    EXPIRED = "expired"
    PROBE_FAILED = "probe_failed"
    PERSISTENCE_REFUSED = "persistence_refused"


class CohortPublicationError(RuntimeError):
    """A fixed refusal that never echoes configuration, nonce or remote data."""

    def __init__(self, reason: CohortPublicationReason) -> None:
        self.reason = reason
        super().__init__(f"Cohort probe publication refused: {reason.value}")


@dataclass(frozen=True, slots=True)
class CoreCohortProbePlan:
    """Trusted manager configuration and an independently issued challenge.

    The manager derives one challenge digest from its process-selected Core
    connection and control settings, independently of its backend-alias allowlist.
    The expected package/runtime tuple remains explicitly bound below. This plan
    is neither a serialized carrier nor authentication of an arbitrary caller.
    The caller owns its existing Core connection; no connection is created here.
    First discovery requires a null target key and session; publication derives
    the key independently from the verified observation before touching a target.
    """

    challenge: ProbeChallengeReceipt
    target_key: str | None
    expected_package_version: str
    expected_runtime: RayRuntimeVersion
    expected_cluster_session: str | None = None
    policy_revision: int = 1


@dataclass(frozen=True, slots=True)
class CohortProbePublication:
    """Published proof identity and an optional policy awaiting a new probe.

    ``activation_policy_id`` never relabels this result's policy, attestation or
    capability. It identifies a newly appended active policy that has no proof.
    ``job_qualification`` retains this configuration's independent observation;
    its receipt can differ from the current shared attestation. Managers may
    cache it only from this successful publisher return, never reconstruct it
    from a consumed challenge alone.
    """

    target_key: str
    desired_state: RayTargetDesiredState
    target_policy_id: int
    attestation_id: int
    capability_id: int
    capability_revision: int
    challenge_id: int
    consumed_at: datetime
    activation_policy_id: int | None = None
    job_qualification: CohortJobQualificationProvenance | None = None


@dataclass(frozen=True, slots=True)
class _ProbePlan:
    challenge: ProbeChallengeReceipt
    target_key: str | None
    package_version: str
    runtime: RayRuntimeVersion
    cluster_session: str | None
    policy_revision: int
    job_request: CohortProbeJobRequest | None = field(default=None, repr=False)

    def expectation(self, observed_session: str) -> RayTargetExpectation:
        from django_ray.target.cohort_probe import derive_cohort_target_key

        return RayTargetExpectation(
            self.target_key
            if self.target_key is not None
            else derive_cohort_target_key(self.challenge.runner_family, observed_session),
            self.challenge.runner_family,
            self.cluster_session or observed_session,
            self.policy_revision,
            self.runtime,
        )


@dataclass(frozen=True, slots=True)
class CoreCohortProbePreparation:
    """Manager-owned authentication context for a supervised local observation.

    Retain this object only in the creating manager. It is not a wire format or
    proof that an arbitrary supplied attestation came from that manager's Ray
    connection. The supervisor owns observation provenance, the connection epoch
    and the external acceptance deadline; only its current result may publish.
    """

    identity: WorkerLeaseIdentity
    plan: _ProbePlan
    began: datetime
    nonce: str = field(repr=False)
    using: str = DEFAULT_DB_ALIAS


@dataclass(frozen=True, slots=True)
class JobsCohortProbePreparation:
    """Parent-only authority paired with one detached reservation snapshot.

    Send only ``snapshot`` to the owned inspection helper. The manager nonce and
    this preparation remain local. Publication must accept the successful result
    of that exact helper operation, not a reconstructed consumed database row.
    """

    identity: WorkerLeaseIdentity
    plan: _ProbePlan
    began: datetime
    nonce: str = field(repr=False)
    snapshot: CohortJobReservationSnapshot = field(repr=False)
    using: str = DEFAULT_DB_ALIAS


def _reject(reason: CohortPublicationReason) -> Never:
    raise CohortPublicationError(reason) from None


def _now() -> datetime:
    return datetime.now(UTC)


def _fresh_time(previous: datetime) -> datetime:
    now = capabilities._now(_now())
    if now < previous:
        _reject(CohortPublicationReason.CLOCK_REGRESSION)
    return now


def _validate_plan(plan: _ProbePlan) -> None:
    challenge = plan.challenge
    if type(challenge) is not ProbeChallengeReceipt:
        _reject(CohortPublicationReason.INVALID_ARGUMENT)
    _positive(challenge.challenge_id)
    _positive(challenge.revision)
    _digest(challenge.configuration_digest)
    _package_version(plan.package_version)
    for value in (challenge.issued_at, challenge.expires_at):
        capabilities._now(value)
    if (
        challenge.consumed_at is not None
        or not timedelta(0) < challenge.expires_at - challenge.issued_at <= timedelta(seconds=600)
        or type(challenge.runner_family) is not RayRunnerFamily
    ):
        _reject(CohortPublicationReason.INVALID_ARGUMENT)
    if challenge.expected_target_policy_id is None:
        if (
            plan.target_key is not None
            or plan.cluster_session is not None
            or plan.policy_revision != 1
        ):
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
    else:
        _positive(challenge.expected_target_policy_id)
        if plan.target_key is None or plan.cluster_session is None:
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
    encode_ray_target_expectation(plan.expectation("session_validation"))


def _actual_runtime(plan: _ProbePlan) -> None:
    import ray

    package, runtime = _local_runtime(ray)
    if (
        package != plan.package_version
        or runtime != plan.runtime
        or ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION
    ):
        _reject(CohortPublicationReason.RUNTIME_MISMATCH)


def _validate_locked_challenge(identity, plan, nonce, *, now, using):
    challenge = plan.challenge
    row = _lock_probe_challenge_for_completion(
        identity,
        challenge.challenge_id,
        configuration_digest=challenge.configuration_digest,
        expected_revision=challenge.revision,
        nonce=nonce,
        now=now,
        using=using,
    )
    if _receipt(row) != challenge:
        _reject(CohortPublicationReason.CHALLENGE_CHANGED)
    lease = _locked_probe_lease(identity, now, using=using)
    if lease.django_ray_version != plan.package_version:
        _reject(CohortPublicationReason.RUNTIME_MISMATCH)
    policy = _locked_expected_policy(
        challenge.expected_target_policy_id, challenge.runner_family, now, using=using
    )
    if policy is not None and decode_ray_target_expectation(policy.expectation_json) != (
        plan.expectation("session_validation")
    ):
        _reject(CohortPublicationReason.POLICY_CHANGED)
    return row, lease


def _authenticate_before_probe(identity, plan, nonce, *, using):
    _outside_transactions()
    identity = capabilities._identity(identity)
    _validate_plan(plan)
    _actual_runtime(plan)
    began = capabilities._now(_now())
    with transaction.atomic(using=using, durable=True):
        _validate_locked_challenge(identity, plan, nonce, now=began, using=using)
        fresh = _fresh_time(began)
        _validate_locked_challenge(identity, plan, nonce, now=fresh, using=using)
    return identity, fresh


def _activate_new_target_locked(target, expectation, *, now, using):
    """Append the manager's explicit bootstrap decision for its new target.

    Only the publisher's actual successful registration result can select this
    branch. It runs in the same transaction as creation, so a crash cannot leave
    an automatically enabled new session looking like an operator-drained one.
    """
    policy, retained, desired = coordination._latest_policy(target, using=using)
    if (
        expectation.policy_revision != 1
        or retained != expectation
        or desired is not RayTargetDesiredState.DRAINING
        or int(policy.revision) != 1
    ):
        _reject(CohortPublicationReason.POLICY_CHANGED)
    active = replace(expectation, policy_revision=2)
    return RayTargetPolicyRevision.objects.using(using).create(
        target=target,
        revision=2,
        desired_state=RayTargetDesiredState.ACTIVE,
        expectation_schema_version=1,
        expectation_json=encode_ray_target_expectation(active),
        expectation_digest=ray_target_expectation_digest(active),
        created_at=now,
    )


def _publish_attestation_locked(
    target, policy, expectation, attestation, *, expected_revision, now, using, is_job
):
    """Keep a slower endpoint observation distinct from newer shared proof.

    Every endpoint still needs its own authenticated, fresh receipt. A newer
    compatible shared observation can supply cluster proof without appending
    the older receipt as a regressed attestation. Caller CAS remains required,
    so a concurrent publication can be retried against the current revisions
    without launching another probe Job.
    """
    if is_job:
        latest = (
            RayTargetAttestationRevision.objects.using(using)
            .filter(policy=policy)
            .order_by("-revision")
            .first()
        )
        if latest is not None and attestation.observed_at <= latest.observed_at:
            shared = capabilities._latest_valid_attestation(
                policy, expectation, expected_revision=expected_revision, now=now, using=using
            )
            if shared.membership_digest != attestation.membership_digest:
                _reject(CohortPublicationReason.PROBE_FAILED)
            return int(shared.revision)
    coordination._record_ray_target_attestation_locked(
        target.pk,
        attestation,
        expected_policy_revision=expectation.policy_revision,
        expected_attestation_revision=expected_revision,
        now=now,
        using=using,
    )
    return expected_revision + 1


def _job_qualification_snapshot(request, reservation, receipt, consumed):
    proof = receipt.attestation
    qualification = CohortJobQualificationProvenance(
        configuration_digest=request.configuration_digest,
        jobs_endpoint=reservation.ray_address,
        challenge_id=request.challenge_id,
        request_revision=request.challenge_revision,
        consumed_challenge_revision=consumed.revision,
        challenge_issued_at=request.issued_at,
        challenge_expires_at=request.expires_at,
        consumed_at=consumed.consumed_at,
        request_digest=reservation.request_digest,
        receipt_digest=reservation.receipt_digest,
        receipt_received_at=reservation.received_at,
        submission_id=receipt.submission_id,
        native_job_id=receipt.native_job_id,
        entrypoint_digest=reservation.entrypoint_digest,
        submitted_control_runtime_env_digest=reservation.submitted_runtime_env_digest,
        endpoint_expectation_digest=proof.expectation_digest,
        endpoint_attestation_digest=proof.attestation_digest,
        endpoint_membership_digest=proof.membership_digest,
        endpoint_observed_at=proof.observed_at,
        endpoint_expires_at=proof.expires_at,
    )
    validate_cohort_job_qualification(qualification)
    return qualification


def _publish_verified(
    identity,
    plan,
    nonce,
    attestation,
    *,
    observed_after,
    expected_attestation_revision,
    expected_capability_revision,
    inspected: InspectedCohortJobReceipt | None,
    activate_new_target: bool,
    using,
):
    capabilities._revision(expected_attestation_revision, allow_zero=True)
    capabilities._revision(expected_capability_revision, allow_zero=True)
    if type(attestation) is not RayClusterAttestation or attestation.observed_at < observed_after:
        _reject(CohortPublicationReason.PROBE_FAILED)
    began = _fresh_time(observed_after)
    expectation = plan.expectation(attestation.expectation.cluster_session)
    compare_ray_target_attestation(expectation, attestation, now=began)
    vendor = capabilities._database_vendor(using=using)
    with transaction.atomic(using=using, durable=True):
        lease = _locked_probe_lease(identity, began, using=using)
        # Replacement holds the same exact lease. Refuse a changed slot before
        # locking the old planned target, so crossed refreshes cannot acquire
        # unrelated target locks in opposite orders.
        preview = (
            RayTargetProbeChallenge.objects.using(using)
            .filter(pk=plan.challenge.challenge_id, lease=lease)
            .first()
        )
        if (
            preview is None
            or _receipt(preview) != plan.challenge
            or not secrets.compare_digest(preview.nonce_digest, _nonce_digest(nonce))
        ):
            _reject(CohortPublicationReason.CHALLENGE_CHANGED)
        if plan.challenge.expected_target_policy_id is None:
            registration = coordination._register_ray_target_locked(
                expectation, now=began, using=using
            )
            created_target = registration.changed
            target = RayTarget.objects.using(using).get(pk=expectation.target_key)
            policy, retained, desired_state = coordination._latest_policy(target, using=using)
        else:
            created_target = False
            policy = _locked_expected_policy(
                plan.challenge.expected_target_policy_id,
                plan.challenge.runner_family,
                began,
                using=using,
            )
            target = policy.target
            policy, retained, desired_state = coordination._latest_policy(target, using=using)
        if retained != expectation:
            _reject(CohortPublicationReason.POLICY_CHANGED)
        challenge, lease = _validate_locked_challenge(identity, plan, nonce, now=began, using=using)
        reservation = None
        if inspected is not None:
            reservation = _locked_reservation(plan.job_request, using=using)
        capabilities._locked_current_capability(lease, target, using=using, vendor=vendor)
        now = _fresh_time(began)
        _actual_runtime(plan)
        challenge, lease = _validate_locked_challenge(identity, plan, nonce, now=now, using=using)
        compare_ray_target_attestation(expectation, attestation, now=now)
        if inspected is not None:
            snapshot = inspected.reservation
            _validate_reservation(
                reservation, plan.job_request, jobs_endpoint=snapshot.jobs_endpoint
            )
            receipt = _validated_receipt(reservation, plan.job_request, now=now)
            if (
                reservation.entrypoint_digest != snapshot.entrypoint_digest
                or reservation.submitted_runtime_env_digest != snapshot.submitted_runtime_env_digest
                or reservation.receipt_json != snapshot.receipt_json
                or reservation.receipt_digest != snapshot.receipt_digest
                or receipt != inspected.receipt
                or not receipt.collected_at <= inspected.inspected_at <= now
            ):
                _reject(CohortPublicationReason.RECEIPT_CHANGED)
        attestation_revision = _publish_attestation_locked(
            target,
            policy,
            expectation,
            attestation,
            expected_revision=expected_attestation_revision,
            now=now,
            using=using,
            is_job=inspected is not None,
        )
        changed = capabilities._advertise_ray_worker_target_capability_locked(
            identity,
            target.pk,
            plan.runtime,
            manager_runner_family=plan.challenge.runner_family,
            expected_policy_revision=plan.policy_revision,
            expected_attestation_revision=attestation_revision,
            expected_capability_revision=expected_capability_revision,
            now=now,
            using=using,
        )
        activation = None
        if created_target and activate_new_target:
            activation = _activate_new_target_locked(target, expectation, now=now, using=using)
        consumed_at = _fresh_time(now)
        challenge, _lease = _validate_locked_challenge(
            identity, plan, nonce, now=consumed_at, using=using
        )
        compare_ray_target_attestation(expectation, attestation, now=consumed_at)
        evidence = capabilities._latest_valid_attestation(
            policy,
            expectation,
            expected_revision=attestation_revision,
            now=consumed_at,
            using=using,
        )
        consumed = _consume_locked_probe_challenge(challenge, now=consumed_at, using=using)
        qualification = (
            _job_qualification_snapshot(plan.job_request, reservation, inspected.receipt, consumed)
            if inspected is not None
            else None
        )
        capability = RayWorkerTargetCapability.objects.using(using).get(lease=lease, target=target)
        return CohortProbePublication(
            str(target.pk),
            desired_state,
            int(policy.pk),
            int(evidence.pk),
            int(capability.pk),
            changed.revision,
            int(challenge.pk),
            consumed_at,
            int(activation.pk) if activation is not None else None,
            qualification,
        )


def prepare_core_cohort_probe(
    identity: WorkerLeaseIdentity,
    plan: CoreCohortProbePlan,
    *,
    nonce: str,
    using: str = DEFAULT_DB_ALIAS,
) -> CoreCohortProbePreparation:
    """Authenticate the exact live challenge before scheduling observation."""
    try:
        if type(plan) is not CoreCohortProbePlan:
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        current = _ProbePlan(
            plan.challenge,
            plan.target_key,
            plan.expected_package_version,
            plan.expected_runtime,
            plan.expected_cluster_session,
            plan.policy_revision,
        )
        if current.challenge.runner_family is not RayRunnerFamily.RAY_CORE:
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        identity, began = _authenticate_before_probe(identity, current, nonce, using=using)
        return CoreCohortProbePreparation(identity, current, began, nonce, using)
    except CohortPublicationError:
        raise
    except DatabaseError:
        _reject(CohortPublicationReason.PERSISTENCE_REFUSED)
    except Exception:
        _reject(CohortPublicationReason.PROBE_FAILED)


def observe_prepared_core_cohort_probe(
    prepared: CoreCohortProbePreparation, *, owned_cleanup: bool = True
) -> RayClusterAttestation:
    """Observe the creating manager's connection without database access.

    A background caller must inherit the same supported Ray connection context.
    Its supervisor keeps the slot until every local call has exited; timeout or
    cancellation does not itself confirm remote cleanup or authorize reconnect.
    """
    try:
        if type(prepared) is not CoreCohortProbePreparation or type(owned_cleanup) is not bool:
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        current = prepared.plan
        began = _fresh_time(prepared.began)
        if began >= current.challenge.expires_at:
            _reject(CohortPublicationReason.EXPIRED)
        options = {"owned_cleanup": True} if owned_cleanup else {}
        return observe_current_cohort_target(
            target_key=current.target_key,
            runner_family=RayRunnerFamily.RAY_CORE,
            expected_django_ray_version=current.package_version,
            expected_runtime=current.runtime,
            expected_cluster_session=current.cluster_session,
            policy_revision=current.policy_revision,
            timeout_seconds=min(30.0, (current.challenge.expires_at - began).total_seconds()),
            max_nodes=64,
            **options,
        )
    except CohortPublicationError:
        raise
    except Exception:
        _reject(CohortPublicationReason.PROBE_FAILED)


def publish_prepared_core_cohort_probe(
    prepared: CoreCohortProbePreparation,
    proof: RayClusterAttestation,
    *,
    expected_attestation_revision: int = 0,
    expected_capability_revision: int = 0,
    activate_new_target: bool = False,
) -> CohortProbePublication:
    """Publish only the supervisor's accepted observation on the manager thread.

    Revalidate current database authority and local package/runtime. These checks
    do not establish observation provenance or a current connection epoch: the
    owning supervisor must reject stale, foreign and post-deadline tickets before
    calling this function. No Ray network call or remote cleanup runs here.
    """
    try:
        if (
            type(prepared) is not CoreCohortProbePreparation
            or type(activate_new_target) is not bool
        ):
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        _outside_transactions()
        _validate_plan(prepared.plan)
        _actual_runtime(prepared.plan)
        return _publish_verified(
            prepared.identity,
            prepared.plan,
            prepared.nonce,
            proof,
            observed_after=prepared.began,
            expected_attestation_revision=expected_attestation_revision,
            expected_capability_revision=expected_capability_revision,
            inspected=None,
            activate_new_target=activate_new_target,
            using=prepared.using,
        )
    except CohortPublicationError:
        raise
    except DatabaseError:
        _reject(CohortPublicationReason.PERSISTENCE_REFUSED)
    except Exception:
        _reject(CohortPublicationReason.PROBE_FAILED)


def publish_core_cohort_probe(
    identity: WorkerLeaseIdentity,
    plan: CoreCohortProbePlan,
    *,
    nonce: str,
    expected_attestation_revision: int = 0,
    expected_capability_revision: int = 0,
    activate_new_target: bool = False,
    using: str = DEFAULT_DB_ALIAS,
) -> CohortProbePublication:
    """Observe the caller's existing Core connection and atomically publish."""
    try:
        if type(activate_new_target) is not bool:
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        capabilities._revision(expected_attestation_revision, allow_zero=True)
        capabilities._revision(expected_capability_revision, allow_zero=True)
        prepared = prepare_core_cohort_probe(identity, plan, nonce=nonce, using=using)
        proof = observe_prepared_core_cohort_probe(prepared, owned_cleanup=False)
        return publish_prepared_core_cohort_probe(
            prepared,
            proof,
            expected_attestation_revision=expected_attestation_revision,
            expected_capability_revision=expected_capability_revision,
            activate_new_target=activate_new_target,
        )
    except CohortPublicationError:
        raise
    except DatabaseError:
        _reject(CohortPublicationReason.PERSISTENCE_REFUSED)
    except Exception:
        _reject(CohortPublicationReason.PROBE_FAILED)


def prepare_cohort_job_probe(
    identity: WorkerLeaseIdentity,
    launch: CohortProbeJobLaunch,
    *,
    nonce: str,
    using: str = DEFAULT_DB_ALIAS,
) -> JobsCohortProbePreparation | None:
    """Authenticate in the parent and detach its exact pending receipt, if any."""
    try:
        from django_ray.runtime.cohort_job_entrypoint import (
            decode_probe_job_launch,
            encode_probe_job_launch,
            probe_job_launch_entrypoint,
        )

        launch = decode_probe_job_launch(encode_probe_job_launch(launch))
        request = launch.request
        if request.lease != CohortProbeJobLease(
            identity.worker_id, identity.hostname, identity.pid, identity.started_at
        ):
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        plan = _ProbePlan(
            ProbeChallengeReceipt(
                request.challenge_id,
                request.configuration_digest,
                request.runner_family,
                request.expected_target_policy_id,
                request.challenge_revision,
                request.issued_at,
                request.expires_at,
                None,
            ),
            request.target_key,
            request.expected_package_version,
            request.expected_runtime,
            request.expected_cluster_session,
            request.policy_revision,
            request,
        )
        identity, began = _authenticate_before_probe(identity, plan, nonce, using=using)
        snapshot = read_cohort_job_reservation(
            identity, request, jobs_endpoint=launch.jobs_endpoint, using=using
        )
        if snapshot is None:
            return None
        if (
            snapshot.entrypoint_digest
            != cohort_probe_entrypoint_digest(probe_job_launch_entrypoint(launch))
            or snapshot.submitted_runtime_env_digest != launch.submitted_runtime_env_digest
        ):
            _reject(CohortPublicationReason.RECEIPT_CHANGED)
        return JobsCohortProbePreparation(identity, plan, began, nonce, snapshot, using)
    except CohortPublicationError:
        raise
    except DatabaseError:
        _reject(CohortPublicationReason.PERSISTENCE_REFUSED)
    except Exception:
        _reject(CohortPublicationReason.PROBE_FAILED)


def publish_prepared_cohort_job_probe(
    prepared: JobsCohortProbePreparation,
    inspected: InspectedCohortJobReceipt,
    *,
    expected_attestation_revision: int = 0,
    expected_capability_revision: int = 0,
    activate_new_target: bool = False,
) -> CohortProbePublication:
    """Atomically publish only the current owned helper's accepted inspection.

    No HTTP or cleanup runs here. The manager must fence the helper's operation,
    configuration epoch and external deadline before this call. Dataclass fields
    and the stored receipt alone do not authenticate a successful endpoint query.
    The original reservation, challenge, nonce, lease and all freshness windows
    are revalidated under the existing publication transaction.
    """
    try:
        if (
            type(prepared) is not JobsCohortProbePreparation
            or type(inspected) is not InspectedCohortJobReceipt
            or type(activate_new_target) is not bool
        ):
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        _outside_transactions()
        _validate_plan(prepared.plan)
        _actual_runtime(prepared.plan)
        if inspected.reservation != prepared.snapshot:
            _reject(CohortPublicationReason.RECEIPT_CHANGED)
        now = _fresh_time(prepared.began)
        if not prepared.began <= capabilities._now(inspected.inspected_at) <= now:
            _reject(CohortPublicationReason.CLOCK_REGRESSION)
        return _publish_verified(
            prepared.identity,
            prepared.plan,
            prepared.nonce,
            inspected.receipt.attestation,
            observed_after=prepared.plan.challenge.issued_at,
            expected_attestation_revision=expected_attestation_revision,
            expected_capability_revision=expected_capability_revision,
            inspected=inspected,
            activate_new_target=activate_new_target,
            using=prepared.using,
        )
    except CohortPublicationError:
        raise
    except DatabaseError:
        _reject(CohortPublicationReason.PERSISTENCE_REFUSED)
    except Exception:
        _reject(CohortPublicationReason.PROBE_FAILED)


def publish_cohort_job_probe(
    identity: WorkerLeaseIdentity,
    launch: CohortProbeJobLaunch,
    *,
    nonce: str,
    expected_attestation_revision: int = 0,
    expected_capability_revision: int = 0,
    activate_new_target: bool = False,
    using: str = DEFAULT_DB_ALIAS,
) -> CohortProbePublication | None:
    """Inspect one independently expected reservation, then publish atomically."""
    try:
        if type(activate_new_target) is not bool:
            _reject(CohortPublicationReason.INVALID_ARGUMENT)
        capabilities._revision(expected_attestation_revision, allow_zero=True)
        capabilities._revision(expected_capability_revision, allow_zero=True)
        prepared = prepare_cohort_job_probe(identity, launch, nonce=nonce, using=using)
        if prepared is None:
            return None
        inspected = inspect_reserved_cohort_job(prepared.snapshot)
        if inspected is None:
            return None
        return publish_prepared_cohort_job_probe(
            prepared,
            inspected,
            expected_attestation_revision=expected_attestation_revision,
            expected_capability_revision=expected_capability_revision,
            activate_new_target=activate_new_target,
        )
    except CohortPublicationError:
        raise
    except DatabaseError:
        _reject(CohortPublicationReason.PERSISTENCE_REFUSED)
    except Exception:
        _reject(CohortPublicationReason.PROBE_FAILED)
