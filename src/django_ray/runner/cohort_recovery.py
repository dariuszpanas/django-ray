"""Recover ownership and completion expectations, never a submittable request.

The caller supplies its own accepted current Jobs qualification. Database rows
cannot seed that positive cache. Original claim/request digests and protected
attestation rows reconstruct only completion expectations; no task input, stored
request payload, RuntimeEnv, filesystem plan, or remote status is read here.
Unknown work remains the same attempt, including across proof expiry or drain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from django.db import connections, transaction
from django.db.models import Exists, F, OuterRef, Q

from django_ray.execution_codec import ExecutionIdentity
from django_ray.maintenance import (
    MaintenanceAdmissionError,
    check_worker_retirement_admission,
    maintenance_admission_barrier,
    worker_retirement_requested,
)
from django_ray.models import RayTaskCohortClaim, RayTaskExecution, TaskWorkerLease
from django_ray.ray_job_protocol import (
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    coordination_sha256,
)
from django_ray.ray_job_request_storage import ray_job_request_reference_content_identity
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cohort_claims import _lease_query, _proof_predicate
from django_ray.runner.cohort_dispatch import _contract
from django_ray.runner.cohort_qualification import EligibleCohortAlias
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.target import capabilities
from django_ray.target import cohort_claim_storage as storage
from django_ray.target.attestation import RayTargetAttestationError
from django_ray.target.cohort_claim import (
    CohortClaimDisposition,
    CohortManagerRuntime,
    CohortRunnerFamily,
    cohort_claim_facts_digest,
    validate_cohort_job_qualification,
    validate_cohort_python,
)
from django_ray.target.cohort_contract import (
    _digest,
    _package_version,
    cohort_execution_contract_digest,
)


class CohortRecoveryError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort Jobs recovery refused")


@dataclass(frozen=True, slots=True)
class RecoveredCohortJobCompletion:
    """Completion-only identity; deliberately has no prepared request to submit."""

    execution: RayTaskExecution
    claim: storage.CohortClaimRecord
    request_digest: str
    contract_digest: str
    handle: SubmissionHandle = field(repr=False)
    request_reference: str | None = field(repr=False)


def _clock():
    return datetime.now(UTC)


def _fresh(previous):
    current = capabilities._now(_clock())
    if current < previous:
        raise CohortRecoveryError
    return current


def _identity(task):
    return ExecutionIdentity(task.pk, task.task_id, task.attempt_number, task.execution_generation)


def _handle(task, record):
    qualification = record.facts.job_qualification
    if qualification is None or record.dispatched_at is None:
        raise CohortRecoveryError
    expected = STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX + coordination_sha256(
        record.facts.identity
    )
    if task.ray_job_id != expected or task.ray_address != qualification.jobs_endpoint:
        raise CohortRecoveryError
    if task.ray_job_request_reference is not None:
        ray_job_request_reference_content_identity(task.ray_job_request_reference)
    return SubmissionHandle(expected, qualification.jobs_endpoint, record.dispatched_at)


def validate_recovered_cohort_job_completion(value, *, current=None, record=None):
    """Validate existing supported-writer expectations, not transport provenance."""
    if type(value) is not RecoveredCohortJobCompletion:
        raise CohortRecoveryError
    retained = value.claim
    record = retained if record is None else record
    current = value.execution if current is None else current
    if (
        record != retained
        or record.facts.binding.runner_family is not CohortRunnerFamily.RAY_JOB
        or record.facts_digest != cohort_claim_facts_digest(record.facts)
        or record.disposition not in {CohortClaimDisposition.OPEN, CohortClaimDisposition.HELD}
        or record.prepared_request_digest != value.request_digest
        or record.dispatched_at is None
        or _identity(current) != record.facts.identity
        or current.execution_protocol_version != 3
        or current.state not in {"RUNNING", "CANCELLING"}
        or current.claimed_by_worker != record.owner.worker_id
        or current.created_with_django_ray_version != record.facts.binding.package_version
        or current.ray_job_request_reference != value.request_reference
        or _handle(current, record) != value.handle
        or cohort_execution_contract_digest(_contract(record)) != value.contract_digest
    ):
        raise CohortRecoveryError
    _digest(value.request_digest)
    _digest(value.contract_digest)


def _qualified(identity, qualifications, manager, now):
    if type(qualifications) not in (tuple, list) or len(qualifications) > 64:
        raise CohortRecoveryError
    result = {}
    for item in qualifications:
        if type(item) is not EligibleCohortAlias or item.job_qualification is None:
            raise CohortRecoveryError
        proof = item.shared
        expected = proof.attestation.expectation
        runtime = expected.runtime
        job = item.job_qualification
        validate_cohort_job_qualification(job)
        if (
            (proof.lease.worker_id, proof.lease.hostname, proof.lease.pid, proof.lease.started_at)
            != (identity.worker_id, identity.hostname, identity.pid, identity.started_at)
            or proof.package_version != manager.package_version
            or expected.runner_family.value != "ray_job"
            or manager.ray_version != (runtime.ray_major, runtime.ray_minor, runtime.ray_patch)
            or (
                manager.python.implementation,
                manager.python.major,
                manager.python.minor,
                manager.python.patch,
            )
            != (
                runtime.python_implementation,
                runtime.python_major,
                runtime.python_minor,
                runtime.python_patch,
            )
            or type(item.configuration.queues) is not tuple
            or not 1 <= len(item.configuration.queues) <= 64
            or any(
                type(queue) is not str or not queue.strip() or len(queue) > 100 or "\x00" in queue
                for queue in item.configuration.queues
            )
        ):
            raise CohortRecoveryError
        # Verify supplied cache candidates; this query never constructs one.
        try:
            predicate = _proof_predicate(item, now, using="default")
        except RayTargetAttestationError:
            continue
        if RayTaskCohortClaim.objects.filter(predicate).exists():
            result.setdefault((expected.target_key, job.jobs_endpoint), []).append(item)
    return result


def _candidates(identity, qualified, package, now):
    exact_owner = TaskWorkerLease.objects.filter(
        worker_id=OuterRef("owner_lease_id"),
        hostname=OuterRef("owner_lease_hostname"),
        pid=OuterRef("owner_lease_pid"),
        started_at=OuterRef("owner_lease_started_at"),
    )
    live_owner = exact_owner.filter(
        is_active=True, stopped_at__isnull=True, last_heartbeat_at__gte=now - get_lease_duration()
    )
    recreated_owner = TaskWorkerLease.objects.filter(
        worker_id=OuterRef("owner_lease_id"), is_active=True
    ).exclude(
        hostname=OuterRef("owner_lease_hostname"),
        pid=OuterRef("owner_lease_pid"),
        started_at=OuterRef("owner_lease_started_at"),
    )
    allowed = Q(pk__in=[])
    for (target, endpoint), items in qualified.items():
        queues = {queue for item in items for queue in item.configuration.queues}
        allowed |= Q(
            target_policy__target_id=target,
            binding__execution__ray_address=endpoint,
            binding__execution__queue_name__in=queues,
        )
    return RayTaskCohortClaim.objects.filter(
        allowed,
        ~Q(owner_lease_id=identity.worker_id),
        ~Q(Exists(live_owner)),
        ~Q(Exists(recreated_owner)),
        disposition__in=("OPEN", "HELD"),
        prepared_request_digest__isnull=False,
        dispatched_at__isnull=False,
        binding__runner_family="ray_job",
        binding__package_version=package,
        binding__execution__execution_protocol_version=3,
        binding__execution__state__in=("RUNNING", "CANCELLING"),
        binding__execution__claimed_by_worker=F("owner_lease_id"),
        binding__execution__attempt_number=F("attempt_number"),
        binding__execution__execution_generation=F("execution_generation"),
        binding__execution__ray_job_id__regex=r"^raysubmit_django_ray_rq2_[0-9a-f]{64}$",
    ).order_by("-binding__execution__priority", "claimed_at", "pk")


def _recover_one(identity, record, item, manager, now):
    with transaction.atomic(), maintenance_admission_barrier() as barrier:
        for lease_identity in sorted((identity, record.owner), key=lambda value: value.worker_id):
            capabilities._locked_exact_lease(
                lease_identity, using="default", vendor=connections["default"].vendor
            )
        now = _fresh(now)
        lease = storage._claim_lease(identity, now, using="default")
        check_worker_retirement_admission(identity, barrier=barrier)
        proof_args = {
            "lease": lease,
            "spec": record.facts.binding,
            "manager": manager,
            "capability_id": item.shared.capability_id,
            "capability_revision": item.shared.capability_revision,
            "job_qualification": item.job_qualification,
            "using": "default",
        }
        storage._proof(now=now, **proof_args)
        adopted = storage.adopt_cohort_claim(
            identity,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            expected_owner=record.owner,
            now=now,
        )
        current = RayTaskExecution.objects.get(pk=record.facts.identity.task_execution_pk)
        if (
            adopted.facts != record.facts
            or adopted.facts_digest != record.facts_digest
            or current.queue_name not in item.configuration.queues
            or current.ray_address != item.job_qualification.jobs_endpoint
        ):
            raise CohortRecoveryError
        if adopted.prepared_request_digest is None:
            raise CohortRecoveryError
        result = RecoveredCohortJobCompletion(
            current,
            adopted,
            adopted.prepared_request_digest,
            cohort_execution_contract_digest(_contract(adopted)),
            _handle(current, adopted),
            current.ray_job_request_reference,
        )
        validate_recovered_cohort_job_completion(result)
        now = _fresh(now)
        storage._claim_lease(identity, now, using="default")
        storage._proof(now=now, **proof_args)
        return result, now


def recover_cohort_jobs(
    identity: WorkerLeaseIdentity,
    *,
    qualifications,
    manager_runtime: CohortManagerRuntime,
    limit: int = 10,
    now: datetime | None = None,
) -> tuple[RecoveredCohortJobCompletion, ...]:
    """Adopt a bounded set of stale Jobs claims behind current qualification.

    ACTIVE and DRAINING qualifications may manage the original target/endpoint.
    Pausing new admission does not block ownership/completion of this generation.
    No status, timeout or missing request payload authorizes replay or resolution.
    """
    if any(
        connection.in_atomic_block or not connection.get_autocommit()
        for connection in connections.all()
    ):
        raise CohortRecoveryError
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(manager_runtime) is not CohortManagerRuntime
    ):
        raise CohortRecoveryError
    identity = capabilities._identity(identity)
    _package_version(manager_runtime.package_version)
    validate_cohort_python(manager_runtime.python)
    version = manager_runtime.ray_version
    if (
        type(version) is not tuple
        or len(version) != 3
        or any(type(part) is not int or not 0 <= part <= (1 << 63) - 1 for part in version)
        or version[0] == 0
    ):
        raise CohortRecoveryError
    now = capabilities._now(_clock() if now is None else now)
    now = _fresh(now)
    if not _lease_query(identity, manager_runtime.package_version, now, using="default").exists():
        return ()
    if worker_retirement_requested(identity):
        return ()
    qualified = _qualified(identity, qualifications, manager_runtime, now)
    candidates = list(
        _candidates(identity, qualified, manager_runtime.package_version, now).select_related(
            "target_policy"
        )[:limit]
    )
    recovered = []
    for row in candidates:
        record = storage._record(row)
        original = record.facts.job_qualification
        if original is None:
            continue
        items = qualified.get((row.target_policy.target_id, original.jobs_endpoint), ())
        for item in items:
            try:
                now = _fresh(now)
                result, now = _recover_one(identity, record, item, manager_runtime, now)
                recovered.append(result)
                break
            except (
                CohortRecoveryError,
                storage.CohortClaimStorageError,
                capabilities.RayWorkerTargetCapabilityError,
                MaintenanceAdmissionError,
            ):
                continue
    return tuple(recovered)
