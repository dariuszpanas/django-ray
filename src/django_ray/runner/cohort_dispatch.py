"""Prepare and fence one claimed attempt before its owner submits application work.

This module never invokes a task or reconstructs positive target qualification.
Original immutable admission evidence remains valid for constructing the claimed
request after its admission window expires. A prepared request is data, not proof
that a remote submission occurred or that an application did not run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from django.db import connections, transaction

from django_ray.execution_codec import ExecutionIdentity
from django_ray.models import RayTargetAttestationRevision, RayTaskExecution
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.target.attestation import (
    decode_ray_cluster_attestation,
    encode_ray_cluster_attestation,
)
from django_ray.target.cohort_claim import (
    CohortClaimDisposition,
    CohortHoldBoundary,
    CohortHoldReason,
    CohortRunnerFamily,
    cohort_claim_facts_digest,
    cohort_task_runtime_env_snapshot_digest,
)
from django_ray.target.cohort_claim_storage import (
    CohortClaimRecord,
    hold_cohort_claim,
    mark_cohort_claim_dispatched,
    prepare_cohort_claim,
)
from django_ray.target.cohort_contract import CohortExecutionContract
from django_ray.target.cohort_sync import CohortSyncContract
from django_ray.target.cohort_transport import (
    PreparedCohortExecution,
    _prepare_cohort_execution_from_environment,
    validate_prepared_cohort_execution,
)


class CohortDispatchError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort dispatch refused")


@dataclass(frozen=True, slots=True)
class PreparedCohortDispatch:
    """One owner's retained request and current ledger revision."""

    execution: RayTaskExecution
    claim: CohortClaimRecord
    prepared: PreparedCohortExecution


@dataclass(frozen=True, slots=True)
class _PreparationTask:
    pk: int
    task_id: str
    attempt_number: int
    execution_generation: int
    execution_protocol_version: int
    state: str
    claimed_by_worker: str
    created_with_django_ray_version: str
    callable_path: str
    args_json: str = field(repr=False)
    kwargs_json: str = field(repr=False)
    input_reference: str | None = field(repr=False)
    runtime_env_profile: str | None
    runtime_env_json: str = field(repr=False)
    runtime_env_hash: str


@dataclass(frozen=True, slots=True)
class CohortPreparationInput:
    """Immutable scalar input for one fixed callback, never an ORM object."""

    task: _PreparationTask = field(repr=False)
    claim: CohortClaimRecord
    contract: CohortExecutionContract | CohortSyncContract
    transport: str | None
    environment_profile: str | None
    environment_json: str = field(repr=False)
    environment_digest: str
    trust_identity_json: str = field(repr=False)


def _preparation_task(task):
    # Any deferred fields are materialized here, on the owning database thread.
    # Only independent scalars cross into the filesystem preparation callback.
    return _PreparationTask(
        **{name: getattr(task, name) for name in _PreparationTask.__dataclass_fields__}
    )


def _outside_transaction():
    if any(
        connection.in_atomic_block
        or (connection.connection is not None and connection.autocommit is not True)
        for connection in connections.all(initialized_only=True)
    ):
        raise CohortDispatchError


def _snapshot(task, record):
    facts = record.facts
    if (
        cohort_claim_facts_digest(facts) != record.facts_digest
        or facts.identity
        != ExecutionIdentity(task.pk, task.task_id, task.attempt_number, task.execution_generation)
        or task.execution_protocol_version != 3
        or task.state != "RUNNING"
        or task.claimed_by_worker != record.owner.worker_id
        or task.created_with_django_ray_version != facts.binding.package_version
        or task.runtime_env_profile != facts.runtime_env_profile
        or task.runtime_env_hash != facts.runtime_env_hash
        or cohort_task_runtime_env_snapshot_digest(
            profile=task.runtime_env_profile,
            serialized=task.runtime_env_json,
            digest=task.runtime_env_hash,
        )
        != facts.runtime_env_snapshot_digest
    ):
        raise CohortDispatchError


def _contract(record):
    facts = record.facts
    common = {
        "identity": facts.identity,
        "expected_django_ray_version": facts.binding.package_version,
        "target_binding_id": facts.binding_id,
        "cohort_evidence_id": record.claim_id,
        "cohort_evidence_digest": record.facts_digest,
        "claimed_at": facts.claimed_at,
    }
    if facts.binding.runner_family is CohortRunnerFamily.SYNC:
        return CohortSyncContract(**common, python=facts.binding.sync_python)
    # Read the exact protected admission row. Latest policy/proof is deliberately
    # irrelevant to this already-claimed attempt, including after a target drain.
    row = (
        RayTargetAttestationRevision.objects.select_related("policy")
        .filter(pk=facts.claim_attestation_id, policy_id=facts.target_policy_id)
        .first()
    )
    if row is None:
        raise CohortDispatchError
    attestation = decode_ray_cluster_attestation(row.attestation_json)
    if (
        encode_ray_cluster_attestation(attestation) != row.attestation_json
        or attestation.attestation_digest != facts.claim_attestation_digest
        or row.attestation_digest != facts.claim_attestation_digest
        or attestation.expectation_digest != facts.target_expectation_digest
        or row.expectation_digest != facts.target_expectation_digest
        or row.policy.expectation_digest != facts.target_expectation_digest
        or attestation.expectation.runner_family.value != facts.binding.runner_family.value
        or row.policy.target_id != attestation.expectation.target_key
        or row.membership_digest != attestation.membership_digest
        or row.observed_at != attestation.observed_at
        or row.expires_at != attestation.expires_at
    ):
        raise CohortDispatchError
    return CohortExecutionContract(
        **common,
        target_expectation=attestation.expectation,
        target_expectation_digest=facts.target_expectation_digest,
        claim_attestation=attestation,
        claim_attestation_digest=facts.claim_attestation_digest,
    )


def prepare_claimed_cohort_dispatch(
    claimed: ClaimedCohortTask, *, transport=None, now: datetime | None = None
) -> PreparedCohortDispatch:
    """Prepare outside locks, then persist its digest under the exact live owner."""
    source = capture_claimed_cohort_preparation(claimed, transport=transport)
    prepared = prepare_captured_cohort_execution(source)
    return commit_claimed_cohort_preparation(claimed, source, prepared, now=now)


def capture_claimed_cohort_preparation(claimed, *, transport=None) -> CohortPreparationInput:
    """Capture database/configuration inputs without scanning RuntimeEnv files."""
    from django_ray.conf.settings import get_settings
    from django_ray.execution_codec import _bounded_json_dumps
    from django_ray.runtime.runtime_env import runtime_env_for_execution

    _outside_transaction()
    if type(claimed) is not ClaimedCohortTask:
        raise CohortDispatchError
    record = claimed.claim
    if record.disposition is not CohortClaimDisposition.OPEN or record.dispatched_at is not None:
        raise CohortDispatchError
    task = _preparation_task(claimed.execution)
    _snapshot(task, record)
    settings = get_settings()
    environment = runtime_env_for_execution(task, config=settings)
    return CohortPreparationInput(
        task,
        record,
        _contract(record),
        transport,
        environment.profile,
        environment.serialized,
        environment.digest,
        _bounded_json_dumps(settings.get("WORKFLOW_PLAN_TRUST_IDENTITY", {}), sort_keys=True),
    )


def prepare_captured_cohort_execution(source) -> PreparedCohortExecution:
    """Scan files only in the owned callback using independent captured inputs."""
    from django_ray.runtime.runtime_env import normalize_runtime_env

    if type(source) is not CohortPreparationInput:
        raise CohortDispatchError
    environment = normalize_runtime_env(
        json.loads(source.environment_json), profile=source.environment_profile
    )
    if environment.digest != source.environment_digest:
        raise CohortDispatchError
    return _prepare_cohort_execution_from_environment(
        source.task,
        contract=source.contract,
        transport=source.transport,
        environment=environment,
        trust_identity=json.loads(source.trust_identity_json),
    )


def commit_claimed_cohort_preparation(claimed, source, prepared, *, now=None):
    """Commit the owned callback result after exact current database rechecks."""
    from django.core.exceptions import ImproperlyConfigured

    from django_ray.workflow.plans import runtime_env_plan_identity_from_transport

    _outside_transaction()
    if type(claimed) is not ClaimedCohortTask or type(source) is not CohortPreparationInput:
        raise CohortDispatchError
    record = claimed.claim
    request, contract = validate_prepared_cohort_execution(prepared, task=source.task)
    try:
        runtime_env_plan_identity_from_transport(
            request.runtime_env_plan_identity,
            trust_identity=json.loads(source.trust_identity_json),
        )
    except (ValueError, ImproperlyConfigured):
        raise CohortDispatchError from None
    if (
        source.claim != record
        or source.task != _preparation_task(claimed.execution)
        or contract != source.contract
        or contract != _contract(record)
        or (
            source.task.runtime_env_hash
            and (
                source.environment_digest != source.task.runtime_env_hash
                or source.environment_profile != (source.task.runtime_env_profile or None)
            )
        )
        or request.runtime_env_profile != source.environment_profile
        or request.runtime_env_hash != source.environment_digest
        or request.compiled_graph_submission_transport != source.transport
    ):
        raise CohortDispatchError
    _snapshot(source.task, record)
    with transaction.atomic():
        record = prepare_cohort_claim(
            record.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            request_digest=prepared.request_digest,
            now=datetime.now(UTC) if now is None else now,
        )
        if record.facts != claimed.claim.facts or record.facts_digest != claimed.claim.facts_digest:
            raise CohortDispatchError
        current = RayTaskExecution.objects.get(pk=record.facts.identity.task_execution_pk)
        _snapshot(current, record)
        validate_prepared_cohort_execution(prepared, task=current)
    return PreparedCohortDispatch(current, record, prepared)


def mark_cohort_dispatch_started(
    dispatch: PreparedCohortDispatch,
    *,
    jobs_handle: SubmissionHandle | None = None,
    now: datetime | None = None,
) -> PreparedCohortDispatch:
    """Commit the uncertain-dispatch boundary before any remote call.

    Jobs' exact qualified endpoint and submission identity are persisted in the
    same transaction. The caller owns that handle before making one submission.
    A committed marker followed by a lost acknowledgement must never be replayed.
    """
    _outside_transaction()
    if type(dispatch) is not PreparedCohortDispatch:
        raise CohortDispatchError
    record = dispatch.claim
    _snapshot(dispatch.execution, record)
    validate_prepared_cohort_execution(dispatch.prepared, task=dispatch.execution)
    if record.prepared_request_digest != dispatch.prepared.request_digest:
        raise CohortDispatchError
    jobs = record.facts.binding.runner_family is CohortRunnerFamily.RAY_JOB
    if jobs != (jobs_handle is not None):
        raise CohortDispatchError
    if jobs:
        from django_ray.runner.ray_job import RayJobRunner

        proof = record.facts.job_qualification
        if (
            type(jobs_handle) is not SubmissionHandle
            or proof is None
            or jobs_handle.ray_address != proof.jobs_endpoint
            or jobs_handle.ray_job_id != RayJobRunner.submission_id(dispatch.execution)
        ):
            raise CohortDispatchError
    with transaction.atomic():
        record = mark_cohort_claim_dispatched(
            record.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            now=datetime.now(UTC) if now is None else now,
        )
        if (
            record.facts != dispatch.claim.facts
            or record.facts_digest != dispatch.claim.facts_digest
            or record.prepared_request_digest != dispatch.prepared.request_digest
        ):
            raise CohortDispatchError
        current = RayTaskExecution.objects.get(pk=record.facts.identity.task_execution_pk)
        _snapshot(current, record)
        validate_prepared_cohort_execution(dispatch.prepared, task=current)
        if jobs_handle is not None:
            changed = RayTaskExecution.objects.filter(
                pk=record.facts.identity.task_execution_pk,
                state="RUNNING",
                ray_job_id__isnull=True,
                ray_address__isnull=True,
            ).update(ray_job_id=jobs_handle.ray_job_id, ray_address=jobs_handle.ray_address)
            if changed != 1:
                raise CohortDispatchError
            current.ray_job_id = jobs_handle.ray_job_id
            current.ray_address = jobs_handle.ray_address
    return replace(dispatch, execution=current, claim=record)


def hold_cohort_dispatch(
    dispatch: PreparedCohortDispatch,
    *,
    reason: CohortHoldReason,
    now: datetime | None = None,
) -> PreparedCohortDispatch:
    """Record uncertainty without making a claim about application invocation."""
    _outside_transaction()
    if type(dispatch) is not PreparedCohortDispatch:
        raise CohortDispatchError
    record = dispatch.claim
    if record.disposition is CohortClaimDisposition.HELD:
        return dispatch
    with transaction.atomic():
        record = hold_cohort_claim(
            record.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            reason=reason,
            boundary=CohortHoldBoundary.CONTROL,
            evidence_digest=dispatch.prepared.request_digest,
            application_invoked=None,
            now=datetime.now(UTC) if now is None else now,
        )
    return replace(dispatch, claim=record)
