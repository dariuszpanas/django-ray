"""Transfer stale Jobs cleanup ownership without changing application history.

Only the caller's accepted current qualification authorizes this bounded scan.
The immutable cleanup queue and original claim target/endpoint remain relevant
after a truthful completion requeues the task or clears its live submission.
These carriers cannot submit work. Missing inspection inputs remain explicitly
OPEN; neither adoption nor a terminal task frees the cleanup obligation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import connections, transaction
from django.db.models import Exists, F, JSONField, OuterRef, Q
from django.db.models.functions import Cast

from django_ray.maintenance import (
    MaintenanceAdmissionError,
    check_worker_retirement_admission,
    maintenance_admission_barrier,
    worker_retirement_requested,
)
from django_ray.models import (
    RayCohortJobCleanup,
    RayTaskCohortClaim,
    RayTaskExecution,
    TaskWorkerLease,
)
from django_ray.runner.cohort_claims import _lease_query
from django_ray.runner.cohort_recovery import CohortRecoveryError, _qualified
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.target import capabilities
from django_ray.target import cohort_claim_storage as claims
from django_ray.target.cohort_claim import CohortManagerRuntime, validate_cohort_python
from django_ray.target.cohort_contract import _package_version
from django_ray.target.cohort_job_cleanup import (
    CohortJobCleanupError,
    CohortJobCleanupRecord,
    adopt_cohort_job_cleanup_locked,
    cleanup_record,
)


class CohortCleanupRecoveryError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort Jobs cleanup recovery refused")


@dataclass(frozen=True, slots=True)
class RecoveredCohortJobCleanup:
    """Cleanup-only ownership; an unavailable expectation grants no closure."""

    cleanup: CohortJobCleanupRecord

    @property
    def expectation(self):
        return self.cleanup.expectation


def _clock():
    return datetime.now(UTC)


def _fresh(previous):
    current = capabilities._now(_clock())
    if current < previous:
        raise CohortCleanupRecoveryError
    return current


def _candidates(identity, qualified, package, now):
    exact_owner = TaskWorkerLease.objects.filter(
        worker_id=OuterRef("owner_lease_id"),
        hostname=OuterRef("owner_lease_hostname"),
        pid=OuterRef("owner_lease_pid"),
        started_at=OuterRef("owner_lease_started_at"),
    )
    unavailable_owner = exact_owner.filter(
        Q(last_heartbeat_at__gt=now)
        | Q(is_active=True, last_heartbeat_at__gte=now - get_lease_duration())
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
            claim__target_policy__target_id=target,
            original_facts__job_qualification__jobs_endpoint=endpoint,
            queue_name__in=queues,
        )
    return (
        RayCohortJobCleanup.objects.alias(original_facts=Cast(F("claim__facts_json"), JSONField()))
        .filter(
            allowed,
            ~Q(owner_lease_id=identity.worker_id),
            ~Q(Exists(unavailable_owner)),
            ~Q(Exists(recreated_owner)),
            state="OPEN",
            claim__disposition="RESOLVED",
            claim__resolution_kind="application_completed",
            claim__resolution_digest=F("completion_digest"),
            claim__facts_digest=F("claim_facts_digest"),
            claim__binding__runner_family="ray_job",
            claim__binding__package_version=package,
        )
        .order_by("created_at", "pk")
    )


def _recover_one(identity, retained, original, item, manager, now):
    with transaction.atomic(), maintenance_admission_barrier() as barrier:
        # Lock even a replacement incarnation under the old worker id. It must
        # not become stale-owner evidence merely because the exact row vanished.
        for worker_id in sorted((identity.worker_id, retained.owner.worker_id)):
            TaskWorkerLease.objects.select_for_update().filter(worker_id=worker_id).first()
        now = _fresh(now)
        lease = claims._claim_lease(identity, now, using="default")
        check_worker_retirement_admission(identity, barrier=barrier)
        previous = TaskWorkerLease.objects.filter(worker_id=retained.owner.worker_id).first()
        if (
            previous is not None
            and previous.is_active
            and (
                previous.hostname,
                previous.pid,
                previous.started_at,
            )
            != (retained.owner.hostname, retained.owner.pid, retained.owner.started_at)
        ):
            raise CohortCleanupRecoveryError
        proof_args = {
            "lease": lease,
            "spec": original.facts.binding,
            "manager": manager,
            "capability_id": item.shared.capability_id,
            "capability_revision": item.shared.capability_revision,
            "job_qualification": item.job_qualification,
            "using": "default",
        }
        claims._proof(now=now, **proof_args)
        # Current application fields may belong to a later queued attempt. Do
        # not hydrate their input, RuntimeEnv, result or mutable handle here.
        task = (
            RayTaskExecution.objects.select_for_update(skip_locked=True)
            .only("pk")
            .filter(pk=retained.execution_id)
            .first()
        )
        if task is None:
            raise CohortCleanupRecoveryError
        row = RayTaskCohortClaim.objects.select_for_update().get(pk=retained.claim_id)
        if (
            claims._record(row) != original
            or retained.queue_name not in item.configuration.queues
            or original.facts.job_qualification is None
            or original.facts.job_qualification.jobs_endpoint
            != item.job_qualification.jobs_endpoint
        ):
            raise CohortCleanupRecoveryError
        now = _fresh(now)
        result = adopt_cohort_job_cleanup_locked(
            task, retained, identity, expected_revision=retained.revision, now=now
        )
        now = _fresh(now)
        claims._claim_lease(identity, now, using="default")
        claims._proof(now=now, **proof_args)
        return RecoveredCohortJobCleanup(result), now


def recover_cohort_job_cleanups(
    identity: WorkerLeaseIdentity,
    *,
    qualifications,
    manager_runtime: CohortManagerRuntime,
    limit: int = 10,
    now: datetime | None = None,
) -> tuple[RecoveredCohortJobCleanup, ...]:
    """Adopt OPEN cleanup under current same-target/endpoint qualification.

    ACTIVE or DRAINING may manage the original Job. Admission pauses and task
    quarantine do not block this ownership transfer; destination retirement does.
    No remote work, payload read, original-claim adoption or result update occurs.
    """
    if any(
        connection.in_atomic_block or not connection.get_autocommit()
        for connection in connections.all()
    ):
        raise CohortCleanupRecoveryError
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(manager_runtime) is not CohortManagerRuntime
    ):
        raise CohortCleanupRecoveryError
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
        raise CohortCleanupRecoveryError
    now = _fresh(capabilities._now(_clock() if now is None else now))
    if not _lease_query(identity, manager_runtime.package_version, now, using="default").exists():
        return ()
    if worker_retirement_requested(identity):
        return ()
    qualified = _qualified(identity, qualifications, manager_runtime, now)
    candidates = list(
        _candidates(identity, qualified, manager_runtime.package_version, now).select_related(
            "claim", "claim__target_policy"
        )[:limit]
    )
    recovered = []
    for row in candidates:
        retained, original = cleanup_record(row), claims._record(row.claim)
        job = original.facts.job_qualification
        if job is None:
            continue
        items = qualified.get((row.claim.target_policy.target_id, job.jobs_endpoint), ())
        for item in items:
            try:
                now = _fresh(now)
                result, now = _recover_one(identity, retained, original, item, manager_runtime, now)
                recovered.append(result)
                break
            except (
                CohortCleanupRecoveryError,
                CohortRecoveryError,
                CohortJobCleanupError,
                claims.CohortClaimStorageError,
                capabilities.RayWorkerTargetCapabilityError,
                MaintenanceAdmissionError,
            ):
                continue
    return tuple(recovered)
