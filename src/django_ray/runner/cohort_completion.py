"""Apply one independently owned completion with its exact durable claim.

The caller must retain the dispatch and authenticate the transport observation.
Canonical data, a Jobs status/log, or a reconstructed dispatch is not provenance.
This service never fetches results, imports a callable, or refreshes admission
proof. Original proof expiry/drain does not invalidate an authentic completion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from django.db import DatabaseError, connections, transaction

from django_ray.execution_codec import DecodedExecutionCompletion, decode_execution_completion
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.maintenance import (
    MaintenanceAdmissionError,
    maintenance_admission_allowed,
    maintenance_admission_barrier,
    read_maintenance_policy,
    task_quarantine_retry_allowed,
)
from django_ray.models import RayTaskExecution
from django_ray.ray_job_request_storage import (
    RayJobRequestStorageError,
    ray_job_request_reference_content_identity,
)
from django_ray.runner.cohort_dispatch import PreparedCohortDispatch
from django_ray.runner.cohort_recovery import (
    RecoveredCohortJobCompletion,
    validate_recovered_cohort_job_completion,
)
from django_ray.target import cohort_claim_storage as storage
from django_ray.target.cohort_claim import (
    CohortClaimDisposition,
    CohortHoldBoundary,
    CohortHoldReason,
    CohortResolutionKind,
    CohortRunnerFamily,
    cohort_claim_facts_digest,
)
from django_ray.target.cohort_transport import (
    CohortExecutionResult,
    decode_cohort_execution_result,
    encode_cohort_execution_result,
    validate_prepared_cohort_execution,
)

_PROTOCOLS = ExecutionProtocolRange(3, 3)
_COMPATIBILITY_REASONS = {
    reason.value: reason
    for reason in (
        CohortHoldReason.PACKAGE_MISMATCH,
        CohortHoldReason.RUNTIME_MISMATCH,
        CohortHoldReason.SESSION_MISMATCH,
        CohortHoldReason.MEMBERSHIP_MISMATCH,
    )
}


class CohortCompletionError(RuntimeError):
    def __init__(self):
        super().__init__("Current-cohort completion refused")


@dataclass(frozen=True, slots=True)
class CohortCompletionApplication[T]:
    dispatch: T
    applied: bool
    retry_admitted: bool


def _clock():
    return datetime.now(UTC)


def _digest(raw: str):
    return (
        "sha256:"
        + hashlib.sha256(
            b"django-ray/cohort-completion-evidence/v1\x00" + raw.encode("utf-8")
        ).hexdigest()
    )


def _prepared(dispatch):
    if type(dispatch) is not PreparedCohortDispatch:
        raise CohortCompletionError
    record = dispatch.claim
    _, contract = validate_prepared_cohort_execution(dispatch.prepared)
    if (
        record.facts.identity != dispatch.prepared.identity
        or record.facts_digest != cohort_claim_facts_digest(record.facts)
        or record.prepared_request_digest != dispatch.prepared.request_digest
        or record.dispatched_at is None
        or record.disposition not in {CohortClaimDisposition.OPEN, CohortClaimDisposition.HELD}
        or contract.cohort_evidence_id != record.claim_id
        or contract.cohort_evidence_digest != record.facts_digest
        or contract.expected_django_ray_version != record.facts.binding.package_version
        or dispatch.execution.pk != record.facts.identity.task_execution_pk
    ):
        raise CohortCompletionError


def _expectations(dispatch):
    if type(dispatch) is RecoveredCohortJobCompletion:
        return dispatch.claim.facts.identity, dispatch.request_digest, dispatch.contract_digest
    return (
        dispatch.prepared.identity,
        dispatch.prepared.request_digest,
        dispatch.prepared.contract_digest,
    )


def _cleanup_expectation(current, record, dispatch):
    """Capture the original Jobs carrier under its task lock, without payload I/O.

    An unusable request reference leaves an explicit unresolved obligation.
    Unrelated validation failures must not silently become missing evidence.
    """
    from django_ray.runner.cohort_job_execution_control import (
        CohortJobExecutionExpectation,
        _validate,
    )

    reference = current.ray_job_request_reference
    try:
        raw_sha, size = ray_job_request_reference_content_identity(reference)
    except RayJobRequestStorageError:
        return None
    if type(dispatch) is PreparedCohortDispatch:
        raw = dispatch.prepared.request_json.encode("utf-8")
        if (raw_sha, size) != (hashlib.sha256(raw).hexdigest(), len(raw)):
            return None
    identity, request_digest, contract_digest = _expectations(dispatch)
    expectation = CohortJobExecutionExpectation(
        identity,
        current.ray_address,
        current.ray_job_id,
        request_digest,
        contract_digest,
        reference,
        raw_sha,
        size,
    )
    if record.facts.identity != identity:
        raise CohortCompletionError
    _validate(expectation)
    return expectation


def _result(dispatch, serialized, provenance):
    identity, request_digest, contract_digest = _expectations(dispatch)
    decoded = decode_cohort_execution_result(
        serialized,
        expected_identity=identity,
        expected_request_digest=request_digest,
        expected_cohort_contract_digest=contract_digest,
    )
    if (
        provenance == "durable_job_completion"
        and decoded.completion_json is None
        and (decoded.boundary != "nested" or decoded.application_invoked is not None)
    ):
        raise CohortCompletionError
    completion: DecodedExecutionCompletion | None = None
    if decoded.completion_json is not None:
        completion = decode_execution_completion(
            decoded.completion_json,
            expected_identity=identity,
            expected_execution_protocol_version=3,
            supported_protocols=_PROTOCOLS,
        )
        if (
            completion.completion.executor_django_ray_version
            != dispatch.claim.facts.binding.package_version
        ):
            raise CohortCompletionError
    return decoded, completion, _digest(serialized)


def _retry_policy(queue_name, retained):
    # A committed nested savepoint keeps its shared transaction lock until the
    # outer completion commits. If maintenance is missing/corrupt, roll this
    # savepoint back before continuing with terminal-only completion.
    try:
        with ExitStack() as pending:
            pending.enter_context(transaction.atomic())
            pending.enter_context(maintenance_admission_barrier())
            policy = read_maintenance_policy()
            maintenance_admission_allowed(policy, queue_name, 3, operation="enqueue")
            retained.enter_context(pending.pop_all())
            return policy
    except (MaintenanceAdmissionError, DatabaseError):
        return None


def _current(dispatch, now):
    retained = dispatch.claim
    row, now = storage._locked_claim(
        retained.owner,
        retained.claim_id,
        retained.facts.identity,
        retained.revision,
        now,
        using="default",
    )
    record = storage._record(row)
    if (
        record.facts != retained.facts
        or record.facts_digest != retained.facts_digest
        or record.prepared_request_digest != _expectations(dispatch)[1]
        or record.dispatched_at != retained.dispatched_at
        or record.disposition != retained.disposition
    ):
        raise CohortCompletionError
    current = RayTaskExecution.objects.get(pk=record.facts.identity.task_execution_pk)
    if type(dispatch) is RecoveredCohortJobCompletion:
        validate_recovered_cohort_job_completion(dispatch, current=current, record=record)
    else:
        validate_prepared_cohort_execution(dispatch.prepared, task=current)
    return current, record, now


def _held(dispatch, current, record, result, evidence_digest, now):
    if record.disposition is CohortClaimDisposition.HELD:
        # Preserve the original immutable uncertainty observation. Another
        # refusal cannot overwrite it or convert non-invocation into resolution.
        return CohortCompletionApplication(
            replace(dispatch, execution=current, claim=record), False, False
        )
    if result is None:
        reason, boundary, invoked = (
            CohortHoldReason.INVALID_COMPLETION,
            CohortHoldBoundary.CONTROL,
            None,
        )
    else:
        reason = _COMPATIBILITY_REASONS.get(result.refusal, CohortHoldReason.TRANSPORT_UNCERTAIN)
        boundary = CohortHoldBoundary(result.boundary)
        invoked = (
            False
            if boundary is CohortHoldBoundary.OUTER
            and result.refusal in _COMPATIBILITY_REASONS
            and result.application_invoked is False
            else None
        )
    held = storage.hold_cohort_claim(
        record.owner,
        record.claim_id,
        expected_identity=record.facts.identity,
        expected_revision=record.revision,
        reason=reason,
        boundary=boundary,
        evidence_digest=evidence_digest,
        application_invoked=invoked,
        now=now,
    )
    return CohortCompletionApplication(
        replace(dispatch, execution=current, claim=held), False, False
    )


def _apply_cohort_completion(
    dispatch: PreparedCohortDispatch | RecoveredCohortJobCompletion,
    result: CohortExecutionResult | str,
    *,
    provenance: str,
    apply_completion: Callable[..., bool],
    now: datetime | None = None,
) -> CohortCompletionApplication:
    """Resolve and apply atomically, or retain a bounded immutable HELD reason.

    ``apply_completion(current_task, decoded_completion, *, retry_admitted)``
    receives a DecodedExecutionCompletion. It must apply existing fenced lifecycle
    transitions inside this transaction and return strict True. A paused or
    unavailable retry policy requires truthful terminal failure. False, exception,
    or leaving an unresolved task state rolls back the claim resolution as well.
    The callback must never perform remote work or treat these data as origin proof.

    ``owned_direct`` requires the retained Core/Sync transport operation;
    ``durable_job_completion`` preserves the existing supported Jobs driver's
    database-writer trust after its exact native/submission/attempt fences.
    Neither a source string nor these data authenticates a malicious writer.
    Durable Jobs data cannot establish outer refusal or non-invocation.
    """
    if any(
        connection.in_atomic_block or not connection.get_autocommit()
        for connection in connections.all()
    ):
        raise CohortCompletionError
    if not callable(apply_completion):
        raise CohortCompletionError
    if type(dispatch) is RecoveredCohortJobCompletion:
        validate_recovered_cohort_job_completion(dispatch)
    else:
        _prepared(dispatch)
    if (
        type(provenance) is not str
        or provenance not in {"owned_direct", "durable_job_completion"}
        or (provenance == "durable_job_completion")
        != (dispatch.claim.facts.binding.runner_family is CohortRunnerFamily.RAY_JOB)
    ):
        raise CohortCompletionError
    serialized = result
    try:
        if type(result) is CohortExecutionResult:
            serialized = encode_cohort_execution_result(result)
        decoded, completion, evidence = _result(dispatch, serialized, provenance)
    except (ValueError, TypeError, KeyError, AttributeError, CohortCompletionError):
        decoded, completion = None, None
        # Do not hash/stringify unbounded or malformed payloads. Correlate the
        # refusal using only the independently retained bounded request digest.
        evidence = _digest("invalid-completion:" + _expectations(dispatch)[1])
    with transaction.atomic(), ExitStack() as retry_context:
        policy = None
        if completion is not None and not completion.completion.success:
            policy = _retry_policy(dispatch.execution.queue_name, retry_context)
        observed = storage.capabilities._now(_clock() if now is None else now)
        current, record, observed = _current(dispatch, observed)
        if provenance == "durable_job_completion" and (
            type(serialized) is not str or current.completion_data != serialized
        ):
            raise CohortCompletionError
        if completion is None:
            return _held(dispatch, current, record, decoded, evidence, observed)
        retry_admitted = bool(
            not completion.completion.success
            and current.state == "RUNNING"
            and policy is not None
            and maintenance_admission_allowed(policy, current.queue_name, 3, operation="enqueue")
            and task_quarantine_retry_allowed(current)
        )
        if record.facts.binding.runner_family is CohortRunnerFamily.RAY_JOB:
            from django_ray.target.cohort_job_cleanup import record_cohort_job_cleanup_locked

            record_cohort_job_cleanup_locked(
                current,
                record,
                _cleanup_expectation(current, record, dispatch),
                completion_evidence_digest=evidence,
                now=observed,
            )
        resolved = storage.resolve_cohort_claim(
            record.owner,
            record.claim_id,
            expected_identity=record.facts.identity,
            expected_revision=record.revision,
            kind=CohortResolutionKind.APPLICATION_COMPLETED,
            evidence_digest=evidence,
            now=observed,
        )
        if apply_completion(current, completion, retry_admitted=retry_admitted) is not True:
            raise CohortCompletionError
        current.refresh_from_db()
        if current.state not in {"SUCCEEDED", "FAILED", "CANCELLED"} and not (
            retry_admitted and current.state == "QUEUED"
        ):
            raise CohortCompletionError
        return CohortCompletionApplication(
            replace(dispatch, execution=current, claim=resolved), True, retry_admitted
        )


def apply_cohort_completion(
    dispatch: PreparedCohortDispatch,
    result: CohortExecutionResult | str,
    *,
    provenance: str,
    apply_completion: Callable[..., bool],
    now: datetime | None = None,
) -> CohortCompletionApplication[PreparedCohortDispatch]:
    """Apply the retained original dispatch; data alone is not origin evidence.

    Durable Jobs results must exactly equal the locked completion_data field.
    Core/Sync direct observations are independent of that durable Jobs channel.
    See the internal transaction contract for callback and retry semantics.
    """
    if type(dispatch) is not PreparedCohortDispatch:
        raise CohortCompletionError
    return _apply_cohort_completion(
        dispatch, result, provenance=provenance, apply_completion=apply_completion, now=now
    )


def apply_recovered_cohort_job_completion(
    recovered: RecoveredCohortJobCompletion,
    serialized_result: str,
    *,
    apply_completion: Callable[..., bool],
    now: datetime | None = None,
) -> CohortCompletionApplication[RecoveredCohortJobCompletion]:
    """Apply only durable completion expectations; never reconstruct a request.

    The fixed driver's existing DB-writer trust and exact current observation
    fence apply. This carrier cannot submit, and status/logs cannot resolve it.
    """
    if type(recovered) is not RecoveredCohortJobCompletion or type(serialized_result) is not str:
        raise CohortCompletionError
    return _apply_cohort_completion(
        recovered,
        serialized_result,
        provenance="durable_job_completion",
        apply_completion=apply_completion,
        now=now,
    )
