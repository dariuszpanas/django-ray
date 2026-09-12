"""Durable post-completion Jobs cleanup, separate from result authority.

These private services trust their source-owned caller's physical inspection,
not a dataclass or status string. No function performs remote work. Original
completion and claim facts remain untouched by cleanup ownership or closure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.db.models import Exists, OuterRef

from django_ray.execution_codec import ExecutionIdentity
from django_ray.models import (
    RayCohortJobCleanup,
    RayTaskCohortClaim,
    RayTaskExecution,
    TaskWorkerLease,
)
from django_ray.runner.leasing import WorkerLeaseIdentity, get_lease_duration
from django_ray.target import capabilities
from django_ray.target import cohort_claim_storage as claims
from django_ray.target.cohort_claim import CohortRunnerFamily, cohort_claim_facts_digest
from django_ray.target.cohort_contract import _digest, cohort_execution_contract_digest

EXPECTATION_MAX_BYTES = 16 * 1024
_MAX = (1 << 63) - 1


class CohortJobCleanupError(RuntimeError):
    def __init__(self, reason="refused"):
        self.reason = reason
        super().__init__(f"Cohort Job cleanup {reason}")


def _reject(reason="refused"):
    raise CohortJobCleanupError(reason)


def _clock():
    return datetime.now(UTC)


def _fresh(previous):
    now = capabilities._now(_clock())
    if now < previous:
        _reject("clock_regression")
    return now


def _positive(value):
    if type(value) is not int or not 1 <= value <= _MAX:
        _reject("invalid")
    return value


def _inside(using):
    connection = connections[using]
    if not connection.in_atomic_block:
        _reject("transaction_required")


def _outside(using):
    connection = connections[using]
    if connection.in_atomic_block or not connection.get_autocommit():
        _reject("transaction_open")


def _hash(domain, value):
    return "sha256:" + hashlib.sha256(domain + b"\x00" + value.encode("utf-8")).hexdigest()


def encode_cohort_job_cleanup_expectation(value):
    """Canonical immutable inspection inputs; no request/input hydration."""
    from django_ray.runner.cohort_job_execution_control import _validate

    try:
        _validate(value)
        if any(
            type(getattr(value, name)) is not str
            for name in (
                "jobs_endpoint",
                "submission_id",
                "request_digest",
                "contract_digest",
                "request_reference",
                "raw_request_sha256",
            )
        ):
            _reject("invalid_expectation")
        serialized = json.dumps(
            asdict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        if len(serialized.encode("utf-8")) > EXPECTATION_MAX_BYTES:
            _reject("invalid_expectation")
        return serialized
    except Exception:
        raise CohortJobCleanupError("invalid_expectation") from None


def cohort_job_cleanup_expectation_digest(value):
    return _hash(
        b"django-ray/cohort-job-cleanup-expectation/v1",
        encode_cohort_job_cleanup_expectation(value),
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _reject("invalid_expectation")
        result[key] = value
    return result


def decode_cohort_job_cleanup_expectation(serialized, *, expected_digest=None):
    from django_ray.runner.cohort_job_execution_control import CohortJobExecutionExpectation

    try:
        if type(serialized) is not str or len(serialized.encode("utf-8")) > EXPECTATION_MAX_BYTES:
            _reject("invalid_expectation")
        data = json.loads(serialized, object_pairs_hook=_pairs)
        if type(data) is not dict or type(data.get("identity")) is not dict:
            _reject("invalid_expectation")
        data["identity"] = ExecutionIdentity(**data["identity"])
        result = CohortJobExecutionExpectation(**data)
        if encode_cohort_job_cleanup_expectation(result) != serialized:
            _reject("invalid_expectation")
        if expected_digest is not None and cohort_job_cleanup_expectation_digest(result) != _digest(
            expected_digest
        ):
            _reject("invalid_expectation")
        return result
    except Exception:
        raise CohortJobCleanupError("invalid_expectation") from None


@dataclass(frozen=True, slots=True)
class CohortJobCleanupRecord:
    cleanup_id: int
    execution_id: int
    queue_name: str
    claim_facts_digest: str
    request_digest: str
    contract_digest: str
    completion_digest: str
    expectation_json: str | None
    expectation_digest: str | None
    missing_expectation_reason: str | None
    created_at: datetime
    updated_at: datetime
    owner: WorkerLeaseIdentity
    revision: int
    state: str

    @property
    def claim_id(self):
        return self.cleanup_id

    @property
    def expectation(self):
        if self.expectation_json is None:
            return None
        return decode_cohort_job_cleanup_expectation(
            self.expectation_json, expected_digest=self.expectation_digest
        )


def cleanup_record(row):
    return CohortJobCleanupRecord(
        row.pk,
        row.execution_id,
        row.queue_name,
        row.claim_facts_digest,
        row.request_digest,
        row.contract_digest,
        row.completion_digest,
        row.expectation_json,
        row.expectation_digest,
        row.missing_expectation_reason,
        row.created_at,
        row.updated_at,
        WorkerLeaseIdentity(
            row.owner_lease_id,
            row.owner_lease_hostname,
            row.owner_lease_pid,
            row.owner_lease_started_at,
        ),
        row.revision,
        row.state,
    )


def _retained(value):
    """Do not let Python's bool/int equality weaken retained snapshot CAS."""
    try:
        if type(value) is not CohortJobCleanupRecord:
            _reject("invalid")
        for counter in (value.cleanup_id, value.execution_id, value.revision):
            _positive(counter)
        capabilities._identity(value.owner)
        if (
            type(value.queue_name) is not str
            or not value.queue_name.strip()
            or len(value.queue_name) > 100
            or "\x00" in value.queue_name
            or type(value.state) is not str
            or value.state not in {"OPEN", "CLOSED"}
            or capabilities._now(value.updated_at) < capabilities._now(value.created_at)
        ):
            _reject("invalid")
        for digest in (
            value.claim_facts_digest,
            value.request_digest,
            value.contract_digest,
            value.completion_digest,
        ):
            _digest(digest)
        if value.expectation_json is None:
            if (
                value.expectation_digest is not None
                or type(value.missing_expectation_reason) is not str
                or value.missing_expectation_reason != "missing_expectation"
            ):
                _reject("invalid")
        else:
            _digest(value.expectation_digest)
            if value.missing_expectation_reason is not None:
                _reject("invalid")
            decode_cohort_job_cleanup_expectation(
                value.expectation_json, expected_digest=value.expectation_digest
            )
        return value
    except Exception:
        raise CohortJobCleanupError("invalid") from None


def _owner_fields(identity):
    identity = capabilities._identity(identity)
    return {
        "owner_lease_id": identity.worker_id,
        "owner_lease_hostname": identity.hostname,
        "owner_lease_pid": identity.pid,
        "owner_lease_started_at": identity.started_at,
    }


def job_cleanup_blocked_expression(*, using=DEFAULT_DB_ALIAS):
    return Exists(
        RayCohortJobCleanup.objects.using(using).filter(execution_id=OuterRef("pk"), state="OPEN")
    )


def check_no_pending_job_cleanup(current_task, *, using=DEFAULT_DB_ALIAS):
    """Caller holds its task row; a queued retry is not execution eligibility."""
    _inside(using)
    if (
        RayCohortJobCleanup.objects.using(using)
        .filter(execution_id=current_task.pk, state="OPEN")
        .exists()
    ):
        _reject("pending")


def owned_job_cleanup_pending(identity, *, using=DEFAULT_DB_ALIAS):
    return (
        RayCohortJobCleanup.objects.using(using)
        .filter(**_owner_fields(identity), state="OPEN")
        .exists()
    )


def record_cohort_job_cleanup_locked(
    current_task,
    claim_record,
    canonical_expectation,
    *,
    completion_evidence_digest,
    now,
    using=DEFAULT_DB_ALIAS,
):
    """Insert before authenticated resolution in its existing outer transaction.

    Caller already holds exact lease -> task -> claim locks. It must resolve the
    claim and apply the authentic result in this same transaction. An unavailable
    reference records an uninspectable OPEN obligation; it never erases the result.
    """
    from django_ray.runner.cohort_dispatch import _contract

    _inside(using)
    try:
        _digest(completion_evidence_digest)
        facts = claim_record.facts
        identity = facts.identity
        row = (
            RayTaskCohortClaim.objects.using(using)
            .select_for_update()
            .get(pk=claim_record.claim_id)
        )
        if (
            claims._record(row) != claim_record
            or facts.binding.runner_family is not CohortRunnerFamily.RAY_JOB
            or facts.job_qualification is None
            or cohort_claim_facts_digest(facts) != claim_record.facts_digest
            or row.disposition not in {"OPEN", "HELD"}
            or row.dispatched_at is None
            or claims._identity(current_task) != identity
            or current_task.state not in {"RUNNING", "CANCELLING"}
            or current_task.claimed_by_worker != claim_record.owner.worker_id
        ):
            _reject("claim_changed")
        contract_digest = cohort_execution_contract_digest(_contract(claim_record))
        encoded = digest = None
        if canonical_expectation is not None:
            encoded = encode_cohort_job_cleanup_expectation(canonical_expectation)
            digest = cohort_job_cleanup_expectation_digest(canonical_expectation)
            if (
                canonical_expectation.identity != identity
                or canonical_expectation.jobs_endpoint != facts.job_qualification.jobs_endpoint
                or canonical_expectation.submission_id != current_task.ray_job_id
                or canonical_expectation.request_reference != current_task.ray_job_request_reference
                or canonical_expectation.request_digest != row.prepared_request_digest
                or canonical_expectation.contract_digest != contract_digest
            ):
                _reject("expectation_changed")
        created = _fresh(max(capabilities._now(now), row.dispatched_at))
        result = RayCohortJobCleanup.objects.using(using).create(
            claim_id=row.pk,
            execution_id=current_task.pk,
            queue_name=current_task.queue_name,
            claim_facts_digest=row.facts_digest,
            request_digest=row.prepared_request_digest,
            contract_digest=contract_digest,
            completion_digest=completion_evidence_digest,
            expectation_json=encoded,
            expectation_digest=digest,
            missing_expectation_reason="missing_expectation" if encoded is None else None,
            created_at=created,
            updated_at=created,
            **_owner_fields(claim_record.owner),
        )
        return cleanup_record(result)
    except CohortJobCleanupError:
        raise
    except Exception:
        raise CohortJobCleanupError("persistence_refused") from None


def _locked_row(retained, *, using):
    from django_ray.runner.cohort_dispatch import _contract

    retained = _retained(retained)
    row = (
        RayCohortJobCleanup.objects.using(using)
        .select_for_update()
        .filter(pk=_positive(retained.cleanup_id))
        .first()
    )
    if row is None or cleanup_record(row) != retained or row.state != "OPEN":
        _reject("changed")
    claim = RayTaskCohortClaim.objects.using(using).get(pk=row.claim_id)
    if (
        claim.disposition != "RESOLVED"
        or claim.resolution_kind != "application_completed"
        or claim.resolution_digest != row.completion_digest
        or claim.facts_digest != row.claim_facts_digest
    ):
        _reject("completion_unconfirmed")
    original = claims._record(claim)
    if (
        original.facts.binding.runner_family is not CohortRunnerFamily.RAY_JOB
        or original.facts.job_qualification is None
        or original.facts.identity.task_execution_pk != row.execution_id
        or original.prepared_request_digest != row.request_digest
        or cohort_execution_contract_digest(_contract(original)) != row.contract_digest
    ):
        _reject("completion_unconfirmed")
    expected = retained.expectation
    if expected is not None and (
        expected.identity != original.facts.identity
        or expected.jobs_endpoint != original.facts.job_qualification.jobs_endpoint
        or expected.request_digest != row.request_digest
        or expected.contract_digest != row.contract_digest
    ):
        _reject("expectation_changed")
    return row, claim


def adopt_cohort_job_cleanup_locked(
    current_task, row, destination_identity, *, expected_revision, now, using=DEFAULT_DB_ALIAS
):
    """Transfer cleanup only after caller-owned current endpoint qualification.

    Caller retains shared maintenance barrier -> sorted old/new leases -> original
    qualified target/probe/receipt/capability -> task -> claim -> cleanup ordering.
    Neither current proof nor this transfer changes the original resolved claim.
    """
    from django_ray.maintenance import check_worker_retirement_admission

    _inside(using)
    row = _retained(row)
    expected_revision = _positive(expected_revision)
    destination_identity = capabilities._identity(destination_identity)
    if (
        row.revision != expected_revision
        or row.owner == destination_identity
        or row.owner.worker_id == destination_identity.worker_id
    ):
        _reject("changed")
    if current_task.pk != row.execution_id:
        _reject("changed")
    current, claim = _locked_row(row, using=using)
    observed = _fresh(max(capabilities._now(now), row.updated_at))
    source = TaskWorkerLease.objects.using(using).filter(worker_id=row.owner.worker_id).first()
    duration = get_lease_duration()
    if source is not None and (
        source.last_heartbeat_at > observed
        or (
            source.is_active
            and WorkerLeaseIdentity(
                source.worker_id, source.hostname, source.pid, source.started_at
            )
            != row.owner
        )
        or (source.is_active and source.last_heartbeat_at >= observed - duration)
    ):
        _reject("owner_live")
    lease = claims._lease(destination_identity, observed, using=using)
    record = claims._record(claim)
    if (
        lease.django_ray_version != record.facts.binding.package_version
        or lease.min_supported_execution_protocol_version != 3
        or lease.max_supported_execution_protocol_version != 3
    ):
        _reject("lease_unavailable")
    check_worker_retirement_admission(destination_identity, using=using)
    if source is not None and source.is_active:
        source.is_active = False
        source.stopped_at = observed
        source.save(using=using, update_fields=("is_active", "stopped_at"))
    current.revision += 1
    current.updated_at = observed
    for key, value in _owner_fields(destination_identity).items():
        setattr(current, key, value)
    current.save(
        using=using, update_fields=("revision", "updated_at", *_owner_fields(destination_identity))
    )
    return cleanup_record(current)


def close_cohort_job_cleanup(
    identity, retained, *, inspection, inspection_began_at, observed_at, using=DEFAULT_DB_ALIAS
):
    """Close only a caller-authenticated fresh owned terminal inspection.

    The caller binds the committed cleanup id/revision/digest to its exact source
    controller ticket, and accepts only that owned callback after its deadline
    checks. An arbitrary inspection dataclass is not provenance. No status affects
    the authentic result, retry decision, original claim, or descendant drain.
    """
    from django_ray.runner.cohort_job_execution_control import CohortJobExecutionInspection
    from django_ray.target.cohort_job_receipt import is_canonical_native_ray_job_id

    _outside(using)
    retained = _retained(retained)
    identity = capabilities._identity(identity)
    if retained.owner != identity:
        _reject("changed")
    began, observed = capabilities._now(inspection_began_at), capabilities._now(observed_at)
    if began < retained.updated_at or observed < began:
        _reject("clock_regression")
    expected = retained.expectation
    if (
        expected is None
        or type(inspection) is not CohortJobExecutionInspection
        or inspection.expectation != expected
        or inspection.status not in {"SUCCEEDED", "FAILED", "STOPPED"}
        or not is_canonical_native_ray_job_id(inspection.native_job_id)
    ):
        _reject("inspection_unconfirmed")
    try:
        with transaction.atomic(using=using):
            lease = claims._lease(identity, _fresh(observed), using=using)
            RayTaskExecution.objects.using(using).select_for_update().get(pk=retained.execution_id)
            claim = (
                RayTaskCohortClaim.objects.using(using)
                .select_for_update()
                .get(pk=retained.cleanup_id)
            )
            row, claim = _locked_row(retained, using=using)
            finished = _fresh(max(observed, claim.resolved_at))
            lease = claims._lease(identity, finished, using=using)
            if lease.django_ray_version != claims._record(claim).facts.binding.package_version:
                _reject("lease_unavailable")
            evidence = json.dumps(
                {
                    "cleanup_id": row.pk,
                    "revision": row.revision,
                    "expectation_digest": row.expectation_digest,
                    "began_at": began.isoformat(),
                    "observed_at": observed.isoformat(),
                    "status": inspection.status,
                    "native_job_id": inspection.native_job_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            row.revision += 1
            row.updated_at = finished
            row.state = "CLOSED"
            row.inspection_began_at, row.closed_at = began, finished
            row.terminal_status, row.native_job_id = inspection.status, inspection.native_job_id
            row.observation_digest = _hash(
                b"django-ray/cohort-job-cleanup-observation/v1", evidence
            )
            row.save(
                using=using,
                update_fields=(
                    "revision",
                    "updated_at",
                    "state",
                    "inspection_began_at",
                    "closed_at",
                    "terminal_status",
                    "native_job_id",
                    "observation_digest",
                ),
            )
            return cleanup_record(row)
    except DatabaseError:
        raise CohortJobCleanupError("persistence_refused") from None
