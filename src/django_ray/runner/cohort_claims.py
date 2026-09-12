"""Bounded protocol-3 selection with qualification before priority and LIMIT.

The worker supplies its current declarations and its own accepted qualification
cache. These data classes are not authentication and database rows cannot seed
that cache. Selection is advisory; each claim owns a separate transaction with
the maintenance barrier before lease/proof/task locks. No task callable, Ray
operation, input loading or RuntimeEnv filesystem resolution runs here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Exists, OuterRef, Q, Subquery

from django_ray.execution_codec import ExecutionIdentity
from django_ray.maintenance import (
    MaintenanceAdmissionError,
    maintenance_admission_allowed,
    maintenance_admission_barrier,
    read_maintenance_policy,
    task_quarantine_blocked_expression,
    worker_retirement_requested,
)
from django_ray.models import (
    RayTargetAttestationRevision,
    RayTargetPolicyRevision,
    RayTargetProbeChallenge,
    RayTargetProbeJobReceipt,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayWorkerTargetCapability,
    TaskState,
    TaskWorkerLease,
)
from django_ray.runner.cohort_qualification import EligibleCohortAlias
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.target.attestation import compare_ray_target_attestation
from django_ray.target.capabilities import _identity, _now
from django_ray.target.cohort_claim import (
    CohortBindingSpec,
    CohortManagerRuntime,
    CohortRunnerFamily,
    cohort_task_runtime_env_snapshot_digest,
    validate_cohort_python,
)
from django_ray.target.cohort_claim_storage import (
    CohortClaimRecord,
    CohortClaimStorageError,
    claim_cohort_execution,
)
from django_ray.target.cohort_contract import _digest, _package_version
from django_ray.target.cohort_intent import CohortSelectionPolicy, cohort_intent_digest
from django_ray.target.cohort_intent_storage import read_cohort_intent
from django_ray.target.cohort_job_cleanup import job_cleanup_blocked_expression


class CohortSelectionError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort selection refused")


@dataclass(frozen=True, slots=True)
class CohortClaimAlias:
    alias: str
    declaration_digest: str
    selection_policy: CohortSelectionPolicy
    queues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimedCohortTask:
    execution: RayTaskExecution
    claim: CohortClaimRecord
    qualification: EligibleCohortAlias | None


def _clock():
    return datetime.now(UTC)


def _fresh(previous):
    current = _now(_clock())
    if current < previous:
        raise CohortSelectionError
    return current


def _validate(aliases, qualifications, family, manager, identity, limit, using):
    if (
        using != DEFAULT_DB_ALIAS
        or connections[using].in_atomic_block
        or not connections[using].get_autocommit()
        or type(family) is not CohortRunnerFamily
        or type(manager) is not CohortManagerRuntime
        or type(limit) is not int
        or not 1 <= limit <= 100
        or isinstance(aliases, str | bytes)
        or not isinstance(aliases, Sequence)
        or len(aliases) > 64
        or isinstance(qualifications, str | bytes)
        or not isinstance(qualifications, Sequence)
        or len(qualifications) > 64
    ):
        raise CohortSelectionError
    _identity(identity)
    _package_version(manager.package_version)
    validate_cohort_python(manager.python)
    by_alias = {}
    for alias in aliases:
        if (
            type(alias) is not CohortClaimAlias
            or type(alias.alias) is not str
            or not 0 < len(alias.alias) <= 128
            or any(not 33 <= ord(char) <= 126 for char in alias.alias)
            or alias.alias in by_alias
            or type(alias.selection_policy) is not CohortSelectionPolicy
            or type(alias.queues) is not tuple
            or not 1 <= len(alias.queues) <= 64
            or len(set(alias.queues)) != len(alias.queues)
            or any(
                type(queue) is not str or not queue.strip() or len(queue) > 100 or "\x00" in queue
                for queue in alias.queues
            )
        ):
            raise CohortSelectionError
        _digest(alias.declaration_digest)
        by_alias[alias.alias] = alias
    qualified = {}
    for item in qualifications:
        if type(item) is not EligibleCohortAlias:
            raise CohortSelectionError
        declaration = by_alias.get(item.configuration.alias)
        if (
            declaration is None
            or declaration.alias in qualified
            or declaration.declaration_digest != item.configuration.declaration_digest
            or declaration.selection_policy != item.configuration.selection_policy
            or declaration.queues != item.configuration.queues
            or item.shared.package_version != manager.package_version
            or item.shared.attestation.expectation.runner_family.value != family.value
            or item.shared.lease.worker_id != identity.worker_id
            or item.shared.lease.hostname != identity.hostname
            or item.shared.lease.pid != identity.pid
            or item.shared.lease.started_at != identity.started_at
        ):
            raise CohortSelectionError
        qualified[declaration.alias] = item
    if family is CohortRunnerFamily.SYNC and (qualifications or manager.ray_version is not None):
        raise CohortSelectionError
    return by_alias, qualified


def _lease_query(identity, package, now, *, using):
    return TaskWorkerLease.objects.using(using).filter(
        worker_id=identity.worker_id,
        hostname=identity.hostname,
        pid=identity.pid,
        started_at=identity.started_at,
        started_at__lte=now,
        last_heartbeat_at__gt=now - get_lease_duration(),
        last_heartbeat_at__lte=now,
        is_active=True,
        capability_schema_version=1,
        legacy_admission_token__isnull=True,
        django_ray_version=package,
        min_supported_execution_protocol_version=3,
        max_supported_execution_protocol_version=3,
    )


def _proof_predicate(item, now, *, using):
    """Filter stale current rows without reconstructing positive authority."""
    shared = item.shared
    compare_ray_target_attestation(shared.attestation.expectation, shared.attestation, now=now)
    if shared.activation_policy_id is not None or shared.desired_state not in {
        "active",
        "draining",
    }:
        return Q(pk__in=[])
    latest_policy = (
        RayTargetPolicyRevision.objects.using(using)
        .filter(target_id=OuterRef("target_id"))
        .order_by("-revision")
    )
    latest_attestation = (
        RayTargetAttestationRevision.objects.using(using)
        .filter(policy_id=OuterRef("target_policy_id"))
        .order_by("-revision")
    )
    cap = (
        RayWorkerTargetCapability.objects.using(using)
        .filter(
            pk=shared.capability_id,
            revision=shared.capability_revision,
            target_id=shared.attestation.expectation.target_key,
            target_policy_id=shared.target_policy_id,
            attestation_id=shared.attestation_id,
            target_policy__desired_state=shared.desired_state,
            attestation__observed_at__lte=now,
            attestation__expires_at__gt=now,
            advertised_at__lte=now,
            lease_id=shared.lease.worker_id,
            lease_hostname=shared.lease.hostname,
            lease_pid=shared.lease.pid,
            lease_started_at=shared.lease.started_at,
        )
        .filter(
            target_policy_id=Subquery(latest_policy.values("pk")[:1]),
            attestation_id=Subquery(latest_attestation.values("pk")[:1]),
        )
    )
    predicate = Q(Exists(cap))
    job = item.job_qualification
    if shared.attestation.expectation.runner_family.value == "ray_job":
        if job is None or not (
            job.consumed_at <= now < min(job.challenge_expires_at, job.endpoint_expires_at)
        ):
            return Q(pk__in=[])
        challenge = RayTargetProbeChallenge.objects.using(using).filter(
            pk=job.challenge_id,
            lease_id=shared.lease.worker_id,
            revision=job.consumed_challenge_revision,
            configuration_digest=item.configuration.declaration_digest,
            issued_at=job.challenge_issued_at,
            expires_at=job.challenge_expires_at,
            consumed_at=job.consumed_at,
        )
        receipt = RayTargetProbeJobReceipt.objects.using(using).filter(
            pk=job.challenge_id,
            request_digest=job.request_digest,
            receipt_digest=job.receipt_digest,
            received_at=job.receipt_received_at,
            ray_address=job.jobs_endpoint,
        )
        predicate &= Q(Exists(challenge)) & Q(Exists(receipt))
    elif job is not None:
        raise CohortSelectionError
    return predicate


def _candidates(aliases, qualified, family, manager, identity, now, policy, *, using):
    admitted = Q(pk__in=[])
    for alias in aliases.values():
        if (
            family is not CohortRunnerFamily.RAY_JOB
            and alias.selection_policy is not CohortSelectionPolicy.WORKER_SELECTED
        ):
            continue
        proof = qualified.get(alias.alias)
        if family is not CohortRunnerFamily.SYNC and proof is None:
            continue
        target = proof.shared.attestation.expectation.target_key if proof else None
        queues = tuple(
            queue
            for queue in alias.queues
            if maintenance_admission_allowed(policy, queue, 3, target_id=target, operation="claim")
        )
        if not queues:
            continue
        clause = Q(
            cohort_intent__schema_version=2,
            cohort_intent__package_version=manager.package_version,
            cohort_intent__backend_alias=alias.alias,
            cohort_intent__configuration_digest=alias.declaration_digest,
            cohort_intent__selection_policy=alias.selection_policy.value,
            queue_name__in=queues,
        )
        binding = Q(
            ray_target_binding__schema_version=2,
            ray_target_binding__runner_family=family.value,
            ray_target_binding__package_version=manager.package_version,
        )
        if proof:
            binding &= Q(ray_target_binding__target_policy__target_id=target)
            try:
                clause &= _proof_predicate(proof, now, using=using)
            except ValueError:
                continue
            if proof.shared.desired_state == "draining":
                clause &= binding & Q(_cohort_prior=True)
            else:
                clause &= Q(ray_target_binding__isnull=True) | binding
        else:
            python = manager.python
            binding &= Q(
                ray_target_binding__target_policy__isnull=True,
                ray_target_binding__sync_python_implementation=python.implementation,
                ray_target_binding__sync_python_major=python.major,
                ray_target_binding__sync_python_minor=python.minor,
                ray_target_binding__sync_python_patch=python.patch,
            )
            clause &= Q(ray_target_binding__isnull=True) | binding
        admitted |= clause
    prior = RayTaskCohortClaim.objects.using(using).filter(binding_id=OuterRef("pk"))
    return (
        RayTaskExecution.objects.using(using)
        .alias(
            _cohort_prior=Exists(prior),
            _cohort_unresolved=Exists(prior.exclude(disposition="RESOLVED")),
            _cohort_quarantined=task_quarantine_blocked_expression(using=using),
            _cohort_cleanup_pending=job_cleanup_blocked_expression(using=using),
        )
        .filter(
            admitted,
            Exists(_lease_query(identity, manager.package_version, now, using=using)),
            state=TaskState.QUEUED,
            execution_protocol_version=3,
            created_with_django_ray_version=manager.package_version,
            _cohort_unresolved=False,
            _cohort_quarantined=False,
            _cohort_cleanup_pending=False,
        )
        .filter(
            Q(run_after__isnull=True) | Q(run_after__lte=now),
            Q(queue_deadline_at__isnull=True) | Q(queue_deadline_at__gt=now),
        )
    )


def claim_cohort_tasks(
    identity: WorkerLeaseIdentity,
    *,
    aliases: Sequence[CohortClaimAlias],
    qualifications: Sequence[EligibleCohortAlias],
    runner_family: CohortRunnerFamily,
    manager_runtime: CohortManagerRuntime,
    limit: int,
    now: datetime,
    using: str = DEFAULT_DB_ALIAS,
) -> tuple[ClaimedCohortTask, ...]:
    """Select a finite batch; lock proof before each task in separate transactions.

    No automatic rescan grows the requested batch after contention. The next
    worker tick can reconsider still-queued work using newly owned evidence.
    """
    by_alias, qualified = _validate(
        aliases, qualifications, runner_family, manager_runtime, identity, limit, using
    )
    now = _now(now)
    with transaction.atomic(using=using):
        with maintenance_admission_barrier(using=using):
            now = _fresh(now)
            if worker_retirement_requested(identity, using=using):
                return ()
            policy = read_maintenance_policy(using=using)
            candidates = list(
                _candidates(
                    by_alias,
                    qualified,
                    runner_family,
                    manager_runtime,
                    identity,
                    now,
                    policy,
                    using=using,
                )
                .order_by("-priority", "created_at", "pk")
                .values_list("pk", flat=True)[:limit]
            )
    claimed = []
    for pk in candidates:
        now = _fresh(now)
        try:
            with transaction.atomic(using=using):
                with maintenance_admission_barrier(using=using) as barrier:
                    now = _fresh(now)
                    if worker_retirement_requested(identity, using=using):
                        break
                    policy = read_maintenance_policy(using=using)
                    task = (
                        _candidates(
                            by_alias,
                            qualified,
                            runner_family,
                            manager_runtime,
                            identity,
                            now,
                            policy,
                            using=using,
                        )
                        .select_related("cohort_intent", "ray_target_binding")
                        .filter(pk=pk)
                        .first()
                    )
                    if task is None:
                        continue
                    intent = read_cohort_intent(task.pk, using=using)
                    proof = qualified.get(intent.backend_alias)
                    binding = getattr(task, "ray_target_binding", None)
                    specification = CohortBindingSpec(
                        runner_family,
                        manager_runtime.package_version,
                        binding.target_policy_id
                        if binding
                        else (proof.shared.target_policy_id if proof else None),
                        manager_runtime.python
                        if runner_family is CohortRunnerFamily.SYNC
                        else None,
                    )
                    record = claim_cohort_execution(
                        identity,
                        expected_identity=ExecutionIdentity(
                            task.pk, task.task_id, task.attempt_number, task.execution_generation
                        ),
                        binding_spec=specification,
                        manager_runtime=manager_runtime,
                        expected_intent_digest=cohort_intent_digest(intent),
                        expected_runtime_env_snapshot_digest=cohort_task_runtime_env_snapshot_digest(
                            profile=task.runtime_env_profile,
                            serialized=task.runtime_env_json,
                            digest=task.runtime_env_hash,
                        ),
                        now=now,
                        capability_id=proof.shared.capability_id if proof else None,
                        capability_revision=proof.shared.capability_revision if proof else None,
                        job_qualification=proof.job_qualification if proof else None,
                        admission_barrier=barrier,
                        skip_locked=True,
                        expected_queue_name=task.queue_name,
                        using=using,
                    )
                    RayTaskExecution.objects.using(using).filter(pk=pk).update(
                        progress_data=None,
                        workflow_progress_summary_json=None,
                        workflow_run_id=None,
                        workflow_plan_selection=None,
                    )
                    task.refresh_from_db(using=using)
                    claimed.append(ClaimedCohortTask(task, record, proof))
        except (CohortClaimStorageError, MaintenanceAdmissionError):
            continue
    return tuple(claimed)
