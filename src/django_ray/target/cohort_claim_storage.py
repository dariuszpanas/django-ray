"""Dormant database-only claim ledger; no worker or producer calls this seam.

Callers own the transaction and acquire leases, sorted qualified targets,
Jobs challenges/receipts, then capabilities BEFORE execution selection/LIMIT.
Do not call this service for an
unrelated target after locking tasks. No networking, RuntimeEnv decryption or
filesystem resolution occurs here. Resolution records a caller's independently
verified evidence; a digest or this database API does not authenticate a result.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.models import F

from django_ray.execution_codec import ExecutionIdentity, is_valid_execution_identity
from django_ray.models import (
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskTargetBinding,
    RayWorkerTargetCapability,
    TaskState,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.runtime.cohort_job import CohortProbeJobLease, decode_probe_job_request
from django_ray.target import capabilities
from django_ray.target.cohort_claim import (
    CohortBindingSpec,
    CohortCapabilitySnapshot,
    CohortClaimDisposition,
    CohortClaimFacts,
    CohortHoldBoundary,
    CohortHoldReason,
    CohortJobQualificationProvenance,
    CohortManagerRuntime,
    CohortPythonVersion,
    CohortResolutionKind,
    CohortRunnerFamily,
    cohort_claim_facts_digest,
    cohort_task_runtime_env_snapshot_digest,
    decode_cohort_claim_facts,
    encode_cohort_claim_facts,
    validate_cohort_binding,
    validate_cohort_job_qualification,
)
from django_ray.target.cohort_contract import _digest, _positive
from django_ray.target.cohort_intent import cohort_intent_digest, encode_cohort_intent
from django_ray.target.cohort_intent_storage import read_cohort_intent
from django_ray.target.cohort_job_receipt_storage import (
    CohortJobStorageError,
    _validate_reservation,
    _validated_receipt,
)
from django_ray.target.cohort_probe_challenges import _locked_probe_lease, _locked_slot


class CohortClaimStorageReason(StrEnum):
    INVALID = "invalid"
    TRANSACTION_REQUIRED = "transaction_required"
    LEASE_UNAVAILABLE = "lease_unavailable"
    EXECUTION_CHANGED = "execution_changed"
    BINDING_CHANGED = "binding_changed"
    PROOF_UNAVAILABLE = "proof_unavailable"
    CLAIM_CHANGED = "claim_changed"
    CLOCK_REGRESSION = "clock_regression"
    PERSISTENCE_REFUSED = "persistence_refused"


class CohortClaimStorageError(RuntimeError):
    def __init__(self, reason: CohortClaimStorageReason) -> None:
        self.reason = reason
        super().__init__(f"Cohort claim storage rejected: {reason.value}")


@dataclass(frozen=True, slots=True)
class CohortClaimRecord:
    claim_id: int
    facts: CohortClaimFacts
    facts_digest: str
    revision: int
    disposition: CohortClaimDisposition
    owner: WorkerLeaseIdentity
    prepared_request_digest: str | None
    dispatched_at: datetime | None


def _clock() -> datetime:
    return datetime.now(UTC)


def _reject(reason: CohortClaimStorageReason) -> None:
    raise CohortClaimStorageError(reason)


def _fresh(previous: datetime) -> datetime:
    now = capabilities._now(_clock())
    if now < previous:
        _reject(CohortClaimStorageReason.CLOCK_REGRESSION)
    return now


@contextmanager
def _operation(using: str):
    if using != DEFAULT_DB_ALIAS or capabilities._database_vendor(using=using) not in {
        "sqlite",
        "postgresql",
    }:
        _reject(CohortClaimStorageReason.INVALID)
    if not connections[using].in_atomic_block:
        _reject(CohortClaimStorageReason.TRANSACTION_REQUIRED)
    try:
        with transaction.atomic(using=using):
            yield
    except CohortClaimStorageError:
        raise
    except DatabaseError:
        raise CohortClaimStorageError(CohortClaimStorageReason.PERSISTENCE_REFUSED) from None
    except (ValueError, TypeError, AttributeError, capabilities.RayWorkerTargetCapabilityError):
        raise CohortClaimStorageError(CohortClaimStorageReason.INVALID) from None


def _lease(identity, now, *, using):
    try:
        lease = _locked_probe_lease(identity, now, using=using)
    except RuntimeError:
        raise CohortClaimStorageError(CohortClaimStorageReason.LEASE_UNAVAILABLE) from None
    if (
        not lease.min_supported_execution_protocol_version
        <= 3
        <= lease.max_supported_execution_protocol_version
    ):
        _reject(CohortClaimStorageReason.LEASE_UNAVAILABLE)
    return lease


def _identity(execution) -> ExecutionIdentity:
    return ExecutionIdentity(
        execution.pk, execution.task_id, execution.attempt_number, execution.execution_generation
    )


def _owner(row) -> WorkerLeaseIdentity:
    return WorkerLeaseIdentity(
        row.owner_lease_id,
        row.owner_lease_hostname,
        row.owner_lease_pid,
        row.owner_lease_started_at,
    )


def _record(row) -> CohortClaimRecord:
    facts = decode_cohort_claim_facts(row.facts_json, expected_digest=row.facts_digest)
    return CohortClaimRecord(
        row.pk,
        facts,
        row.facts_digest,
        row.revision,
        CohortClaimDisposition(row.disposition),
        _owner(row),
        row.prepared_request_digest,
        row.dispatched_at,
    )


def _binding_spec(row) -> CohortBindingSpec:
    python = None
    if row.runner_family == "sync":
        python = CohortPythonVersion(
            row.sync_python_implementation,
            row.sync_python_major,
            row.sync_python_minor,
            row.sync_python_patch,
        )
    spec = CohortBindingSpec(
        CohortRunnerFamily(row.runner_family), row.package_version, row.target_policy_id, python
    )
    validate_cohort_binding(spec)
    return spec


def _task_snapshot(task) -> str:
    return cohort_task_runtime_env_snapshot_digest(
        profile=task.runtime_env_profile,
        serialized=task.runtime_env_json,
        digest=task.runtime_env_hash,
    )


def _lock_task(pk, *, using):
    query = RayTaskExecution.objects.using(using).filter(pk=pk)
    if connections[using].vendor == "sqlite":
        query.update(task_id=F("task_id"))
        return query.first()
    return query.select_for_update().first()


def _job_proof(lease, qualification, manager, policy, expectation, attestation, now, *, using):
    """Revalidate a retained publisher result, never reconstruct a positive cache.

    Lease and target must already be locked; take the exact challenge and its
    immutable receipt before capability/execution locks. A later call under
    those same retained locks checks freshness again without switching slots.
    """
    validate_cohort_job_qualification(qualification)
    challenge = _locked_slot(lease, qualification.challenge_id, using=using)
    if challenge is None or (
        challenge.lease_id != lease.pk
        or challenge.lease_hostname != lease.hostname
        or challenge.lease_pid != lease.pid
        or challenge.lease_started_at != lease.started_at
        or challenge.runner_family != "ray_job"
        or challenge.configuration_digest != qualification.configuration_digest
        or challenge.revision != qualification.consumed_challenge_revision
        or challenge.issued_at != qualification.challenge_issued_at
        or challenge.expires_at != qualification.challenge_expires_at
        or challenge.consumed_at != qualification.consumed_at
        or not qualification.consumed_at
        <= now
        < min(qualification.challenge_expires_at, qualification.endpoint_expires_at)
    ):
        _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
    try:
        # Read by the already locked exact slot; canonical request parsing then
        # permits reuse of the reservation's independent binding validators.
        from django_ray.models import RayTargetProbeJobReceipt

        reservation = (
            RayTargetProbeJobReceipt.objects.using(using)
            .select_for_update()
            .filter(pk=challenge.pk)
            .first()
        )
        if reservation is None:
            _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
        request = decode_probe_job_request(reservation.request_json)
        _validate_reservation(reservation, request, jobs_endpoint=qualification.jobs_endpoint)
        receipt = _validated_receipt(reservation, request, now=now)
        if (
            request.challenge_id != challenge.pk
            or request.challenge_revision + 1 != challenge.revision
            or request.lease
            != CohortProbeJobLease(lease.pk, lease.hostname, lease.pid, lease.started_at)
            or request.configuration_digest != challenge.configuration_digest
            or request.issued_at != challenge.issued_at
            or request.expires_at != challenge.expires_at
            or request.expected_target_policy_id != challenge.expected_target_policy_id
            or (
                request.expected_target_policy_id is not None
                and request.expected_target_policy_id != policy.pk
            )
            or request.expected_package_version != manager.package_version
            or request.expected_runtime != expectation.runtime
            or receipt.attestation.expectation != expectation
            or receipt.attestation.membership_digest != attestation.membership_digest
            or qualification
            != CohortJobQualificationProvenance(
                configuration_digest=request.configuration_digest,
                jobs_endpoint=reservation.ray_address,
                challenge_id=challenge.pk,
                request_revision=request.challenge_revision,
                consumed_challenge_revision=challenge.revision,
                challenge_issued_at=challenge.issued_at,
                challenge_expires_at=challenge.expires_at,
                consumed_at=challenge.consumed_at,
                request_digest=reservation.request_digest,
                receipt_digest=reservation.receipt_digest,
                submission_id=receipt.submission_id,
                native_job_id=receipt.native_job_id,
                entrypoint_digest=reservation.entrypoint_digest,
                submitted_control_runtime_env_digest=reservation.submitted_runtime_env_digest,
                endpoint_expectation_digest=receipt.attestation.expectation_digest,
                endpoint_attestation_digest=receipt.attestation.attestation_digest,
                endpoint_membership_digest=receipt.attestation.membership_digest,
                endpoint_observed_at=receipt.attestation.observed_at,
                endpoint_expires_at=receipt.attestation.expires_at,
                receipt_received_at=reservation.received_at,
            )
        ):
            _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
    except (CohortJobStorageError, ValueError, TypeError, AttributeError):
        raise CohortClaimStorageError(CohortClaimStorageReason.PROOF_UNAVAILABLE) from None


def _proof(
    lease, spec, manager, capability_id, capability_revision, now, *, job_qualification, using
):
    if spec.runner_family is CohortRunnerFamily.SYNC:
        if (
            capability_id is not None
            or capability_revision is not None
            or manager.ray_version is not None
        ):
            _reject(CohortClaimStorageReason.INVALID)
        return None
    _positive(capability_id)
    _positive(capability_revision)
    preview = (
        RayWorkerTargetCapability.objects.using(using).filter(pk=capability_id, lease=lease).first()
    )
    selected = RayTargetPolicyRevision.objects.using(using).filter(pk=spec.target_policy_id).first()
    if preview is None or selected is None or preview.target_id != selected.target_id:
        _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
    vendor = capabilities._database_vendor(using=using)
    target = capabilities._locked_capability_target(
        target_key=preview.target_id, using=using, vendor=vendor
    )
    policy, expectation = capabilities._latest_usable_policy(
        target, expected_revision=preview.target_policy.revision, using=using, allow_ray_job=True
    )
    attestation = capabilities._latest_valid_attestation(
        policy, expectation, expected_revision=preview.attestation.revision, now=now, using=using
    )
    if spec.runner_family is CohortRunnerFamily.RAY_JOB:
        _job_proof(
            lease, job_qualification, manager, policy, expectation, attestation, now, using=using
        )
    current = capabilities._locked_current_capability(lease, target, using=using, vendor=vendor)
    if current is None or current.pk != capability_id or current.revision != capability_revision:
        _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
    revision, runtime = capabilities._validate_current_capability(
        current, lease=lease, target=target, using=using, allow_ray_job=True
    )
    if (
        current.target_policy_id != policy.pk
        or current.attestation_id != attestation.pk
        or (
            current.runner_family != spec.runner_family.value
            or manager.ray_version != (runtime.ray_major, runtime.ray_minor, runtime.ray_patch)
            or manager.python
            != CohortPythonVersion(
                runtime.python_implementation,
                runtime.python_major,
                runtime.python_minor,
                runtime.python_patch,
            )
        )
    ):
        _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
    return (
        policy,
        expectation,
        attestation,
        CohortCapabilitySnapshot(
            current.pk, current.schema_version, revision, current.advertised_at
        ),
    )


def claim_cohort_execution(
    lease_identity: WorkerLeaseIdentity,
    *,
    expected_identity: ExecutionIdentity,
    binding_spec: CohortBindingSpec,
    manager_runtime: CohortManagerRuntime,
    expected_intent_digest: str,
    expected_runtime_env_snapshot_digest: str,
    now: datetime,
    capability_id: int | None = None,
    capability_revision: int | None = None,
    job_qualification: CohortJobQualificationProvenance | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> CohortClaimRecord:
    """Claim a queued identity, increasing generation once in the caller transaction.

    Caller qualification supplies the actual manager observation and selects a
    finite admitted intent. This service checks it against durable capability
    proof; it does not inspect local files or authenticate caller-created data.
    A Jobs qualification must be retained from the authenticated publisher
    result. A consumed database slot or caller-created dataclass cannot seed
    a positive manager cache. All qualification locks precede task selection.
    """
    with _operation(using):
        if type(expected_identity) is not ExecutionIdentity or not is_valid_execution_identity(
            expected_identity
        ):
            _reject(CohortClaimStorageReason.INVALID)
        validate_cohort_binding(binding_spec)
        if binding_spec.runner_family is CohortRunnerFamily.RAY_JOB:
            validate_cohort_job_qualification(job_qualification)
        elif job_qualification is not None:
            _reject(CohortClaimStorageReason.INVALID)
        _digest(expected_intent_digest)
        _digest(expected_runtime_env_snapshot_digest)
        now = capabilities._now(now)
        lease_identity = capabilities._identity(lease_identity)
        lease = _lease(lease_identity, now, using=using)
        proof = _proof(
            lease,
            binding_spec,
            manager_runtime,
            capability_id,
            capability_revision,
            now,
            job_qualification=job_qualification,
            using=using,
        )
        task = _lock_task(expected_identity.task_execution_pk, using=using)
        now = _fresh(now)
        lease = _lease(lease_identity, now, using=using)
        if (
            task is None
            or _identity(task) != expected_identity
            or task.state != TaskState.QUEUED
            or task.execution_protocol_version != 3
            or task.execution_generation >= (1 << 63) - 1
        ):
            _reject(CohortClaimStorageReason.EXECUTION_CHANGED)
        if (
            task.run_after is not None
            and task.run_after > now
            or task.queue_deadline_at is not None
            and task.queue_deadline_at <= now
        ):
            _reject(CohortClaimStorageReason.EXECUTION_CHANGED)
        intent = read_cohort_intent(task.pk, using=using)
        if (
            cohort_intent_digest(intent) != expected_intent_digest
            or (
                job_qualification is not None
                and job_qualification.configuration_digest != intent.configuration_digest
            )
            or _task_snapshot(task) != expected_runtime_env_snapshot_digest
        ):
            _reject(CohortClaimStorageReason.EXECUTION_CHANGED)
        # Raw RuntimeEnv bytes have no storage cap. Re-read time after hashing
        # them, before relying on lease liveness or the retained proof TTL.
        now = _fresh(now)
        lease = _lease(lease_identity, now, using=using)
        if (
            manager_runtime.package_version != binding_spec.package_version
            or lease.django_ray_version != manager_runtime.package_version
        ):
            _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
        binding = RayTaskTargetBinding.objects.using(using).filter(execution=task).first()
        if binding is not None and (
            binding.schema_version != 2 or _binding_spec(binding) != binding_spec
        ):
            _reject(CohortClaimStorageReason.BINDING_CHANGED)
        if (
            RayTaskCohortClaim.objects.using(using)
            .filter(binding_id=task.pk)
            .exclude(disposition="RESOLVED")
            .exists()
        ):
            _reject(CohortClaimStorageReason.CLAIM_CHANGED)
        if proof is not None:
            # All relevant rows are already locked; this fresh read verifies TTL
            # and revision after a potentially long wait for the execution row.
            proof = _proof(
                lease,
                binding_spec,
                manager_runtime,
                capability_id,
                capability_revision,
                now,
                job_qualification=job_qualification,
                using=using,
            )
            if (
                not RayTaskCohortClaim.objects.using(using).filter(binding_id=task.pk).exists()
                and proof[0].desired_state != RayTargetDesiredState.ACTIVE
            ):
                _reject(CohortClaimStorageReason.PROOF_UNAVAILABLE)
        if binding is None:
            python = binding_spec.sync_python
            binding = RayTaskTargetBinding.objects.using(using).create(
                execution=task,
                schema_version=2,
                target_policy_id=binding_spec.target_policy_id,
                runner_family=binding_spec.runner_family.value,
                package_version=binding_spec.package_version,
                sync_python_implementation=python.implementation if python else None,
                sync_python_major=python.major if python else None,
                sync_python_minor=python.minor if python else None,
                sync_python_patch=python.patch if python else None,
                created_at=now,
            )
        claimed_identity = replace(
            expected_identity, execution_generation=expected_identity.execution_generation + 1
        )
        facts = CohortClaimFacts(
            identity=claimed_identity,
            binding_id=binding.pk,
            binding=binding_spec,
            manager=manager_runtime,
            worker_lease_id=lease_identity.worker_id,
            worker_lease_hostname=lease_identity.hostname,
            worker_lease_pid=lease_identity.pid,
            worker_lease_started_at=lease_identity.started_at,
            intent_json=encode_cohort_intent(intent),
            intent_digest=expected_intent_digest,
            runtime_env_profile=task.runtime_env_profile,
            runtime_env_hash=task.runtime_env_hash,
            runtime_env_snapshot_digest=expected_runtime_env_snapshot_digest,
            claimed_at=now,
            target_policy_id=proof[0].pk if proof else None,
            claim_attestation_id=proof[2].pk if proof else None,
            target_expectation_digest=proof[0].expectation_digest if proof else None,
            claim_attestation_digest=proof[2].attestation_digest if proof else None,
            capability=proof[3] if proof else None,
            job_qualification=job_qualification,
        )
        row = RayTaskCohortClaim.objects.using(using).create(
            binding=binding,
            attempt_number=claimed_identity.attempt_number,
            execution_generation=claimed_identity.execution_generation,
            facts_json=encode_cohort_claim_facts(facts),
            facts_digest=cohort_claim_facts_digest(facts),
            target_policy_id=facts.target_policy_id,
            claim_attestation_id=facts.claim_attestation_id,
            claimed_at=now,
            owner_lease_id=lease_identity.worker_id,
            owner_lease_hostname=lease_identity.hostname,
            owner_lease_pid=lease_identity.pid,
            owner_lease_started_at=lease_identity.started_at,
        )
        task.state = TaskState.RUNNING
        task.execution_generation = claimed_identity.execution_generation
        task.claimed_by_worker = lease_identity.worker_id
        task.managed_with_django_ray_version = manager_runtime.package_version
        task.started_at = task.last_heartbeat_at = now
        task.finished_at = task.completion_data = task.ray_job_id = task.ray_address = (
            task.ray_job_request_reference
        ) = None
        task.save(
            using=using,
            update_fields=(
                "state",
                "execution_generation",
                "claimed_by_worker",
                "managed_with_django_ray_version",
                "started_at",
                "last_heartbeat_at",
                "finished_at",
                "completion_data",
                "ray_job_id",
                "ray_address",
                "ray_job_request_reference",
            ),
        )
        return _record(row)


def _locked_claim(identity, claim_id, expected_identity, expected_revision, now, *, using):
    _positive(claim_id)
    _positive(expected_revision)
    if type(expected_identity) is not ExecutionIdentity or not is_valid_execution_identity(
        expected_identity
    ):
        _reject(CohortClaimStorageReason.INVALID)
    identity = capabilities._identity(identity)
    lease = _lease(identity, now, using=using)
    task = _lock_task(expected_identity.task_execution_pk, using=using)
    row = (
        RayTaskCohortClaim.objects.using(using)
        .select_for_update()
        .filter(pk=claim_id, binding_id=expected_identity.task_execution_pk)
        .first()
    )
    now = _fresh(now)
    lease = _lease(identity, now, using=using)
    if (
        task is None
        or row is None
        or _identity(task) != expected_identity
        or task.state not in {TaskState.RUNNING, TaskState.CANCELLING}
        or task.execution_protocol_version != 3
        or task.claimed_by_worker != identity.worker_id
    ):
        _reject(CohortClaimStorageReason.EXECUTION_CHANGED)
    record = _record(row)
    if lease.django_ray_version != record.facts.binding.package_version:
        _reject(CohortClaimStorageReason.LEASE_UNAVAILABLE)
    if (
        record.facts.identity != expected_identity
        or record.owner != identity
        or row.revision != expected_revision
        or row.disposition == "RESOLVED"
    ):
        _reject(CohortClaimStorageReason.CLAIM_CHANGED)
    _event_chronology(row, now)
    return row, now


def _event_chronology(row, now):
    if any(
        event is not None and now < event
        for event in (
            row.claimed_at,
            row.held_at,
            row.prepared_at,
            row.dispatched_at,
            row.resolved_at,
        )
    ):
        _reject(CohortClaimStorageReason.CLOCK_REGRESSION)


def _save(row, values: dict[str, Any], *, using) -> CohortClaimRecord:
    if row.revision >= (1 << 63) - 1:
        _reject(CohortClaimStorageReason.CLAIM_CHANGED)
    values["revision"] = row.revision + 1
    changed = (
        RayTaskCohortClaim.objects.using(using)
        .filter(pk=row.pk, revision=row.revision)
        .update(**values)
    )
    if changed != 1:
        _reject(CohortClaimStorageReason.CLAIM_CHANGED)
    row.refresh_from_db(using=using)
    return _record(row)


def prepare_cohort_claim(
    lease_identity,
    claim_id,
    *,
    expected_identity,
    expected_revision,
    request_digest,
    now,
    using=DEFAULT_DB_ALIAS,
):
    with _operation(using):
        _digest(request_digest)
        row, now = _locked_claim(
            lease_identity,
            claim_id,
            expected_identity,
            expected_revision,
            capabilities._now(now),
            using=using,
        )
        if row.disposition != "OPEN" or row.prepared_request_digest is not None:
            _reject(CohortClaimStorageReason.CLAIM_CHANGED)
        return _save(
            row, {"prepared_request_digest": request_digest, "prepared_at": now}, using=using
        )


def mark_cohort_claim_dispatched(
    lease_identity, claim_id, *, expected_identity, expected_revision, now, using=DEFAULT_DB_ALIAS
):
    with _operation(using):
        row, now = _locked_claim(
            lease_identity,
            claim_id,
            expected_identity,
            expected_revision,
            capabilities._now(now),
            using=using,
        )
        if row.disposition != "OPEN" or row.prepared_at is None or row.dispatched_at is not None:
            _reject(CohortClaimStorageReason.CLAIM_CHANGED)
        return _save(row, {"dispatched_at": now}, using=using)


def hold_cohort_claim(
    lease_identity,
    claim_id,
    *,
    expected_identity,
    expected_revision,
    reason,
    boundary,
    evidence_digest,
    application_invoked,
    now,
    using=DEFAULT_DB_ALIAS,
):
    with _operation(using):
        if (
            type(reason) is not CohortHoldReason
            or type(boundary) is not CohortHoldBoundary
            or application_invoked is not None
            and type(application_invoked) is not bool
        ):
            _reject(CohortClaimStorageReason.INVALID)
        if application_invoked is False and (
            boundary is not CohortHoldBoundary.OUTER
            or reason
            not in {
                CohortHoldReason.PACKAGE_MISMATCH,
                CohortHoldReason.RUNTIME_MISMATCH,
                CohortHoldReason.SESSION_MISMATCH,
                CohortHoldReason.MEMBERSHIP_MISMATCH,
            }
        ):
            _reject(CohortClaimStorageReason.INVALID)
        _digest(evidence_digest)
        row, now = _locked_claim(
            lease_identity,
            claim_id,
            expected_identity,
            expected_revision,
            capabilities._now(now),
            using=using,
        )
        if row.disposition != "OPEN":
            _reject(CohortClaimStorageReason.CLAIM_CHANGED)
        return _save(
            row,
            {
                "disposition": "HELD",
                "hold_reason": reason.value,
                "hold_boundary": boundary.value,
                "hold_application_invoked": application_invoked,
                "hold_evidence_digest": evidence_digest,
                "held_at": now,
            },
            using=using,
        )


def resolve_cohort_claim(
    lease_identity,
    claim_id,
    *,
    expected_identity,
    expected_revision,
    kind,
    evidence_digest,
    now,
    using=DEFAULT_DB_ALIAS,
):
    """Record authenticated resolution before lifecycle mutation in this transaction.

    The caller must establish provenance outside this service. Initial held
    evidence remains unchanged. Original proof expiry is not an execution TTL.
    """
    with _operation(using):
        if type(kind) is not CohortResolutionKind:
            _reject(CohortClaimStorageReason.INVALID)
        _digest(evidence_digest)
        row, now = _locked_claim(
            lease_identity,
            claim_id,
            expected_identity,
            expected_revision,
            capabilities._now(now),
            using=using,
        )
        if kind is CohortResolutionKind.APPLICATION_COMPLETED and row.dispatched_at is None:
            _reject(CohortClaimStorageReason.CLAIM_CHANGED)
        return _save(
            row,
            {
                "disposition": "RESOLVED",
                "resolution_kind": kind.value,
                "resolution_digest": evidence_digest,
                "resolved_at": now,
            },
            using=using,
        )


def adopt_cohort_claim(
    lease_identity,
    claim_id,
    *,
    expected_identity,
    expected_revision,
    expected_owner,
    now,
    using=DEFAULT_DB_ALIAS,
):
    """Transfer only current ownership after fencing an exact stale owner."""
    with _operation(using):
        _positive(claim_id)
        _positive(expected_revision)
        if type(expected_identity) is not ExecutionIdentity or not is_valid_execution_identity(
            expected_identity
        ):
            _reject(CohortClaimStorageReason.INVALID)
        lease_identity = capabilities._identity(lease_identity)
        expected_owner = capabilities._identity(expected_owner)
        if lease_identity == expected_owner or lease_identity.worker_id == expected_owner.worker_id:
            _reject(CohortClaimStorageReason.INVALID)
        now = capabilities._now(now)
        # Match the worker's sorted lease-before-execution lock order.
        leases = {}
        for identity in sorted((lease_identity, expected_owner), key=lambda item: item.worker_id):
            leases[identity] = capabilities._locked_exact_lease(
                identity, using=using, vendor=connections[using].vendor
            )
        now = _fresh(now)
        _lease(lease_identity, now, using=using)
        source = leases[expected_owner]
        if (
            source is not None
            and source.is_active
            and source.stopped_at is None
            and source.last_heartbeat_at >= now - get_lease_duration()
        ):
            _reject(CohortClaimStorageReason.LEASE_UNAVAILABLE)
        # A recreated active same-ID lease is not stale ownership evidence.
        if (
            TaskWorkerLease.objects.using(using)
            .filter(worker_id=expected_owner.worker_id, is_active=True)
            .exclude(**expected_owner.database_filters())
            .exists()
        ):
            _reject(CohortClaimStorageReason.LEASE_UNAVAILABLE)
        task = _lock_task(expected_identity.task_execution_pk, using=using)
        row = (
            RayTaskCohortClaim.objects.using(using)
            .select_for_update()
            .filter(pk=claim_id, binding_id=expected_identity.task_execution_pk)
            .first()
        )
        now = _fresh(now)
        _lease(lease_identity, now, using=using)
        if (
            row is None
            or task is None
            or row.revision != expected_revision
            or _identity(task) != expected_identity
            or _record(row).facts.identity != expected_identity
            or _owner(row) != expected_owner
            or task.claimed_by_worker != expected_owner.worker_id
            or task.state not in {TaskState.RUNNING, TaskState.CANCELLING}
            or row.disposition == "RESOLVED"
        ):
            _reject(CohortClaimStorageReason.CLAIM_CHANGED)
        if leases[lease_identity].django_ray_version != _record(row).facts.binding.package_version:
            _reject(CohortClaimStorageReason.LEASE_UNAVAILABLE)
        _event_chronology(row, now)
        if source is not None and source.is_active:
            source.is_active = False
            source.stopped_at = now
            source.save(using=using, update_fields=("is_active", "stopped_at"))
        task.claimed_by_worker = lease_identity.worker_id
        task.managed_with_django_ray_version = leases[lease_identity].django_ray_version
        task.save(
            using=using, update_fields=("claimed_by_worker", "managed_with_django_ray_version")
        )
        return _save(
            row,
            {
                "owner_lease_id": lease_identity.worker_id,
                "owner_lease_hostname": lease_identity.hostname,
                "owner_lease_pid": lease_identity.pid,
                "owner_lease_started_at": lease_identity.started_at,
            },
            using=using,
        )
