"""Owned completion and its claim resolve together without renewing admission."""

import hashlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from django.db import connection, transaction

from django_ray import lifecycle, maintenance
from django_ray.execution_codec import (
    ExecutionCompletion,
    ExecutionIdentity,
    _encode_execution_completion_for_protocols,
)
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.models import (
    RayCohortJobCleanup,
    RayMaintenancePolicy,
    RayTargetPolicyRevision,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayTaskQuarantine,
    RayWorkerRetirement,
    TaskAttempt,
    TaskWorkerLease,
)
from django_ray.runner import cohort_completion as completion
from django_ray.runner import cohort_dispatch as dispatch
from django_ray.runner.ray_job import RayJobRunner
from django_ray.target.attestation import (
    decode_ray_target_expectation,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)
from django_ray.target.cohort_claim import CohortHoldReason
from django_ray.target.cohort_claim_storage import CohortClaimStorageError
from django_ray.target.cohort_transport import CohortExecutionResult, encode_cohort_execution_result
from tests.integration.test_cohort_claim_storage import _lease
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_dispatch import _claimed

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


@pytest.fixture(autouse=True)
def isolated_completion_controls(isolated_sqlite_ledger_maintenance):
    """Release audited fixture quarantine before stopped task-first teardown.

    Exact fixture lease removal leaves retirement history orphaned for flush;
    neither removal nor test cleanup claims successful production retirement.
    """
    yield
    if connection.vendor == "sqlite":
        for task in RayTaskExecution.objects.filter(
            pk__in=RayTaskQuarantine.objects.values("task_execution_pk")
        ):
            latest = RayTaskQuarantine.objects.filter(task_execution_pk=task.pk).latest("revision")
            if latest.state == "QUARANTINED":
                maintenance.set_task_quarantine(
                    ExecutionIdentity(
                        task.pk, task.task_id, task.attempt_number, task.execution_generation
                    ),
                    quarantined=False,
                    expected_revision=latest.revision,
                    actor="isolated-test-teardown",
                    reason="release-fixture",
                    authorized=True,
                )
        for row in RayWorkerRetirement.objects.all():
            TaskWorkerLease.objects.filter(
                worker_id=row.worker_id,
                hostname=row.hostname,
                pid=row.pid,
                started_at=row.started_at,
            ).delete()


def _started(case, family="sync"):
    from django_ray.target import cohort_job_cleanup

    case.monkeypatch.setattr(cohort_job_cleanup, "_clock", lambda: case.now)
    value = dispatch.prepare_claimed_cohort_dispatch(
        _claimed(case, family), transport="ray-job" if family == "ray_job" else None, now=case.now
    )
    handle = None
    if family == "ray_job":
        handle = RayJobRunner().cohort_submission_handle(
            case.task, jobs_endpoint=value.claim.facts.job_qualification.jobs_endpoint
        )
    return dispatch.mark_cohort_dispatch_started(value, jobs_handle=handle, now=case.now)


def _stored_request_reference(value):
    """Retain only exact request content identity; no filesystem artifact needed."""
    raw = value.prepared.request_json.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    reference = (
        f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json&bytes={len(raw)}"
    )
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(
        ray_job_request_reference=reference
    )
    return reference


@pytest.mark.parametrize("reference", ["present", "missing", "invalid", "wrong-content"])
def test_jobs_completion_records_cleanup_before_callback_from_locked_reference(case, reference):
    from django_ray.target.cohort_job_cleanup import cleanup_record

    value = _started(case, "ray_job")
    # The original retained instance predates submission; use the locked row.
    assert value.execution.ray_job_request_reference is None
    if reference == "present":
        expected = _stored_request_reference(value)
    elif reference != "missing":
        expected = "unusable-reference"
        if reference == "wrong-content":
            expected = "resultfs://sha256/" + "a" * 64 + "?rel=aa/aa/" + "a" * 64 + ".json&bytes=1"
        RayTaskExecution.objects.filter(pk=value.execution.pk).update(
            ray_job_request_reference=expected
        )

    def terminal(task, decoded, *, retry_admitted):
        pending = cleanup_record(RayCohortJobCleanup.objects.get())
        assert pending.state == "OPEN" and pending.owner == value.claim.owner
        assert pending.queue_name == task.queue_name
        assert pending.request_digest == value.prepared.request_digest
        assert pending.contract_digest == value.prepared.contract_digest
        assert pending.completion_digest == completion._digest(
            encode_cohort_execution_result(_result(value))
        )
        assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"
        if reference == "present":
            assert pending.expectation is not None
            assert pending.expectation.request_reference == expected
            assert pending.expectation.submission_id == task.ray_job_id
        else:
            assert (
                pending.expectation is None
                and pending.missing_expectation_reason == "missing_expectation"
            )
        task.state = "SUCCEEDED"
        task.save(update_fields=["state"])
        return True

    assert _apply(value, case, callback=terminal).applied
    assert RayCohortJobCleanup.objects.get().state == "OPEN"


@pytest.mark.parametrize("mode", ["false", "exception", "truthy", "no-mutation"])
def test_jobs_callback_rollback_also_removes_cleanup_obligation(case, mode):
    value = _started(case, "ray_job")
    _stored_request_reference(value)

    def failed(task, decoded, *, retry_admitted):
        assert RayCohortJobCleanup.objects.get().state == "OPEN"
        if mode != "no-mutation":
            task.state = "SUCCEEDED"
            task.save(update_fields=["state"])
        if mode == "exception":
            raise RuntimeError("callback refused")
        return {"false": False, "truthy": 1}.get(mode, True)

    with pytest.raises(RuntimeError):
        _apply(value, case, callback=failed)
    assert not RayCohortJobCleanup.objects.exists()
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
    assert RayTaskExecution.objects.get().state == "RUNNING"


def test_cleanup_persistence_failure_cannot_commit_claim_resolution_or_callback(case, monkeypatch):
    from django_ray.target import cohort_job_cleanup

    value = _started(case, "ray_job")
    original = cohort_job_cleanup.record_cohort_job_cleanup_locked

    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise cohort_job_cleanup.CohortJobCleanupError("persistence_refused")

    monkeypatch.setattr(cohort_job_cleanup, "record_cohort_job_cleanup_locked", failed)
    with pytest.raises(cohort_job_cleanup.CohortJobCleanupError):
        _apply(value, case, callback=lambda *a, **k: pytest.fail("Missing durable cleanup"))
    assert not RayCohortJobCleanup.objects.exists()
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
    assert RayTaskExecution.objects.get().state == "RUNNING"


@pytest.mark.parametrize("family", ["sync", "ray_core"])
def test_direct_completion_does_not_create_jobs_cleanup(case, family):
    assert _apply(_started(case, family), case).applied
    assert not RayCohortJobCleanup.objects.exists()


def test_jobs_retry_retains_original_cleanup_before_mutating_current_handle(case):
    value = _started(case, "ray_job")
    reference = _stored_request_reference(value)

    def retry(task, decoded, *, retry_admitted):
        assert retry_admitted and not decoded.completion.success
        task.state = "FAILED"
        task.save(update_fields=["state"])
        task.state = "QUEUED"
        task.attempt_number += 1
        task.ray_job_id = task.ray_address = task.ray_job_request_reference = None
        task.completion_data = None
        task.save(
            update_fields=[
                "state",
                "attempt_number",
                "ray_job_id",
                "ray_address",
                "ray_job_request_reference",
                "completion_data",
            ]
        )
        return True

    result = _apply(value, case, _result(value, success=False), callback=retry)
    from django_ray.target.cohort_job_cleanup import cleanup_record

    pending = cleanup_record(RayCohortJobCleanup.objects.get())
    assert result.applied and result.dispatch.execution.state == "QUEUED"
    assert pending.expectation is not None
    assert pending.expectation.request_reference == reference
    assert pending.expectation.identity == value.claim.facts.identity
    assert pending.state == "OPEN" and pending.execution_id == value.execution.pk


def _result(value, *, success=True, **changes):
    body = ExecutionCompletion(
        value.claim.facts.identity,
        3,
        "0.5.0",
        success,
        5 if success else None,
        None,
        None if success else "application failed",
        None,
        None if success else "ValueError",
        None if success else True,
    )
    return replace(
        CohortExecutionResult(
            value.prepared.identity,
            value.prepared.request_digest,
            value.prepared.contract_digest,
            _encode_execution_completion_for_protocols(body, ExecutionProtocolRange(3, 3)),
            application_invoked=True,
        ),
        **changes,
    )


def _apply(value, case, result=None, callback=None):
    def terminal(task, decoded, *, retry_admitted):
        assert decoded.completion.identity == value.claim.facts.identity
        assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"
        task.state = "SUCCEEDED" if decoded.completion.success else "FAILED"
        task.save(update_fields=["state"])
        return True

    observed = _result(value) if result is None else result
    if value.claim.facts.binding.runner_family == "ray_job":
        serialized = (
            encode_cohort_execution_result(observed)
            if type(observed) is CohortExecutionResult
            else observed
        )
        RayTaskExecution.objects.filter(pk=value.execution.pk).update(completion_data=serialized)
    return completion.apply_cohort_completion(
        value,
        observed,
        provenance="durable_job_completion"
        if value.claim.facts.binding.runner_family == "ray_job"
        else "owned_direct",
        apply_completion=terminal if callback is None else callback,
        now=case.now,
    )


def test_durable_job_observation_must_equal_locked_current_completion(case):
    value = _started(case, "ray_job")
    observed = encode_cohort_execution_result(_result(value))
    newer = encode_cohort_execution_result(_result(value, success=False))
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(completion_data=newer)
    with pytest.raises(completion.CohortCompletionError):
        completion.apply_cohort_completion(
            value,
            observed,
            provenance="durable_job_completion",
            apply_completion=lambda *args, **kwargs: pytest.fail("Stale result reached callback"),
            now=case.now,
        )
    row = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    assert (row.disposition, row.revision) == ("OPEN", value.claim.revision)
    assert RayTaskExecution.objects.get(pk=value.execution.pk).completion_data == newer


def test_quarantine_requires_terminal_failure_even_when_enqueue_policy_is_open(case, monkeypatch):
    value = _started(case)
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    maintenance.set_task_quarantine(
        value.claim.facts.identity,
        quarantined=True,
        expected_revision=0,
        actor="test",
        reason="hold-retry",
        authorized=True,
    )
    observed = []

    def terminal(task, decoded, *, retry_admitted):
        observed.append(retry_admitted)
        assert not decoded.completion.success
        task.state = "FAILED"
        task.save(update_fields=["state"])
        return True

    assert _apply(value, case, _result(value, success=False), callback=terminal).applied
    assert observed == [False]
    assert RayTaskCohortClaim.objects.get().resolution_kind == "application_completed"


def test_cancelling_authentic_failure_never_admits_retry(case):
    value = _started(case)
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(state="CANCELLING")
    observed = []

    def terminal(task, decoded, *, retry_admitted):
        observed.append(retry_admitted)
        assert task.state == "CANCELLING" and not decoded.completion.success
        task.state = "FAILED"
        task.save(update_fields=["state"])
        return True

    assert _apply(value, case, _result(value, success=False), callback=terminal).applied
    assert observed == [False]


@pytest.mark.parametrize("family", ["sync", "ray_core", "ray_job"])
@pytest.mark.parametrize("serialized", [False, True])
def test_authentic_owned_completion_resolves_and_applies_atomically(case, family, serialized):
    value = _started(case, family)
    result = _result(value)
    applied = _apply(value, case, encode_cohort_execution_result(result) if serialized else result)
    row = RayTaskCohortClaim.objects.get()
    assert applied.applied and not applied.retry_admitted
    assert applied.dispatch.execution.state == "SUCCEEDED"
    assert row.disposition == "RESOLVED" and row.resolution_kind == "application_completed"
    assert row.resolution_digest.startswith("sha256:") and len(row.resolution_digest) == 71
    with pytest.raises(CohortClaimStorageError):
        _apply(value, case)


@pytest.mark.parametrize("mode", ["false", "exception", "truthy", "no-mutation"])
def test_callback_failure_or_missing_transition_rolls_back_resolution_and_task(case, mode):
    value = _started(case)

    def apply(task, decoded, *, retry_admitted):
        if mode != "no-mutation":
            task.state = "SUCCEEDED"
            task.save(update_fields=["state"])
        if mode == "exception":
            raise RuntimeError("callback failure")
        return {"false": False, "truthy": 1}.get(mode, True)

    with pytest.raises(RuntimeError):
        _apply(value, case, callback=apply)
    row = RayTaskCohortClaim.objects.get()
    case.task.refresh_from_db()
    assert row.disposition == "OPEN" and row.revision == value.claim.revision
    assert row.resolved_at is None and case.task.state == "RUNNING"


def _pause(**changes):
    return maintenance.replace_maintenance_policy(
        (),
        pause_enqueues=changes.get("enqueues", False),
        pause_claims=changes.get("claims", False),
        expected_revision=maintenance.read_maintenance_policy().revision,
        actor="test-operator",
        reason="cohort-completion",
        authorized=True,
    )


@pytest.mark.parametrize("mode", ["paused", "missing", "claim-only", "available"])
def test_failure_retry_admission_controls_requeue_without_losing_truthful_completion(case, mode):
    value = _started(case)
    if mode == "paused":
        _pause(enqueues=True)
    elif mode == "missing":
        RayMaintenancePolicy.objects.all().delete()
    elif mode == "claim-only":
        _pause(claims=True)
    received = []

    def apply(task, decoded, *, retry_admitted):
        received.append(retry_admitted)
        assert not decoded.completion.success
        # Terminal truth is recorded before an optional new enqueue boundary.
        task.state = "FAILED"
        task.save(update_fields=["state"])
        if retry_admitted:
            task.state = "QUEUED"
            task.attempt_number += 1
            task.save(update_fields=["state", "attempt_number"])
        return True

    applied = _apply(value, case, _result(value, success=False), callback=apply)
    allowed = mode in {"claim-only", "available"}
    assert received == [allowed] and applied.retry_admitted is allowed
    assert applied.dispatch.execution.state == ("QUEUED" if allowed else "FAILED")
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"


def test_success_never_reads_maintenance_or_acquires_its_barrier(case, monkeypatch):
    value = _started(case)
    RayMaintenancePolicy.objects.all().delete()
    monkeypatch.setattr(
        completion, "_retry_policy", lambda *args: pytest.fail("Success needs no admission")
    )
    assert _apply(value, case).dispatch.execution.state == "SUCCEEDED"


def test_retry_barrier_is_acquired_before_any_lease_or_task_lock(case, monkeypatch):
    value = _started(case)
    original_barrier, original_locked = (
        completion.maintenance_admission_barrier,
        completion.storage._locked_claim,
    )
    events = []

    def barrier(*args, **kwargs):
        events.append("barrier")
        return original_barrier(*args, **kwargs)

    def locked(*args, **kwargs):
        events.append("claim-locks")
        assert events[0] == "barrier"
        return original_locked(*args, **kwargs)

    monkeypatch.setattr(completion, "maintenance_admission_barrier", barrier)
    monkeypatch.setattr(completion.storage, "_locked_claim", locked)
    assert _apply(value, case, _result(value, success=False)).applied


@pytest.mark.parametrize(
    "refusal", ["package_mismatch", "runtime_mismatch", "session_mismatch", "membership_mismatch"]
)
def test_outer_compatibility_refusal_is_held_not_completed_or_retried(case, refusal):
    value = _started(case)
    result = CohortExecutionResult(
        value.prepared.identity,
        value.prepared.request_digest,
        value.prepared.contract_digest,
        refusal=refusal,
        application_invoked=False,
    )
    held = _apply(
        value,
        case,
        result,
        callback=lambda *args, **kwargs: pytest.fail("Refusal is not application completion"),
    )
    row = RayTaskCohortClaim.objects.get()
    assert not held.applied and row.disposition == "HELD"
    assert row.hold_reason == refusal and row.hold_boundary == "outer"
    assert row.hold_application_invoked is False and row.resolved_at is None
    assert RayTaskExecution.objects.get().state == "RUNNING"


@pytest.mark.parametrize(
    "refusal,boundary,invoked",
    [
        ("nested_refusal", "nested", None),
        ("runtime_mismatch", "nested", None),
        ("observation_unavailable", "outer", None),
        ("runtime_mismatch", "outer", None),
    ],
)
def test_uncertain_or_nested_observation_never_infers_outer_noninvocation(
    case, refusal, boundary, invoked
):
    value = _started(case)
    result = CohortExecutionResult(
        value.prepared.identity,
        value.prepared.request_digest,
        value.prepared.contract_digest,
        refusal=refusal,
        boundary=boundary,
        application_invoked=invoked,
    )
    assert not _apply(value, case, result).applied
    row = RayTaskCohortClaim.objects.get()
    assert row.disposition == "HELD" and row.hold_application_invoked is None
    assert row.resolved_at is None


@pytest.mark.parametrize(
    "invalid", ["not-json with secret", "{}", False, "identity", "request", "contract", "package"]
)
def test_malformed_crossed_or_wrong_package_result_holds_bounded_invalid_evidence(case, invalid):
    value = _started(case)
    result = invalid
    if invalid == "identity":
        result = _result(
            value,
            identity=replace(value.prepared.identity, task_execution_pk=value.execution.pk + 1),
        )
    elif invalid == "request":
        result = _result(value, request_digest="sha256:" + "e" * 64)
    elif invalid == "contract":
        result = _result(value, contract_digest="sha256:" + "e" * 64)
    elif invalid == "package":
        result = replace(
            _result(value),
            completion_json=_result(value).completion_json.replace('"0.5.0"', '"0.4.0"'),
        )
    held = _apply(value, case, result)
    row = RayTaskCohortClaim.objects.get()
    assert not held.applied and row.hold_reason == "invalid_completion"
    assert row.hold_application_invoked is None and len(row.hold_evidence_digest) == 71
    assert "secret" not in row.hold_evidence_digest


@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_authentic_late_held_completion_preserves_expired_original_proof(case, family):
    value = _started(case, family)
    held = dispatch.hold_cohort_dispatch(
        value, reason=CohortHoldReason.TRANSPORT_UNCERTAIN, now=case.now
    )
    initial = RayTaskCohortClaim.objects.values().get()
    old_policy = RayTargetPolicyRevision.objects.get(pk=value.claim.facts.target_policy_id)
    drained_expectation = replace(
        decode_ray_target_expectation(old_policy.expectation_json),
        policy_revision=old_policy.revision + 1,
    )
    with transaction.atomic():
        RayTargetPolicyRevision.objects.create(
            target_id=old_policy.target_id,
            revision=drained_expectation.policy_revision,
            desired_state="draining",
            expectation_schema_version=1,
            expectation_json=encode_ray_target_expectation(drained_expectation),
            expectation_digest=ray_target_expectation_digest(drained_expectation),
            created_at=case.now,
        )
    case.now += timedelta(seconds=120)
    case.lease.last_heartbeat_at = case.now
    case.lease.save(update_fields=["last_heartbeat_at"])
    applied = _apply(held, case)
    final = RayTaskCohortClaim.objects.values().get()
    assert applied.applied and final["disposition"] == "RESOLVED"
    for name in (
        "facts_json",
        "facts_digest",
        "held_at",
        "hold_reason",
        "hold_evidence_digest",
        "claim_attestation_id",
    ):
        assert final[name] == initial[name]


def test_repeated_refusal_preserves_original_hold_under_exact_owner_revision(case):
    value = _started(case)
    first = _apply(value, case, "invalid secret")
    original = RayTaskCohortClaim.objects.values().get()
    second = _apply(first.dispatch, case, "another invalid secret")
    assert not second.applied and RayTaskCohortClaim.objects.values().get() == original
    with pytest.raises(CohortClaimStorageError):
        _apply(value, case, "stale invalid secret")


@pytest.mark.parametrize("cross", ["prepared", "owner", "revision", "claim-facts"])
def test_crossed_retained_dispatch_cannot_resolve_or_hold_current_claim(case, cross):
    value = _started(case)
    if cross == "prepared":
        value = replace(
            value, claim=replace(value.claim, prepared_request_digest="sha256:" + "e" * 64)
        )
    elif cross == "owner":
        value = replace(
            value,
            claim=replace(
                value.claim, owner=replace(value.claim.owner, pid=value.claim.owner.pid + 1)
            ),
        )
    elif cross == "revision":
        value = replace(value, claim=replace(value.claim, revision=value.claim.revision + 1))
    else:
        value = replace(value, claim=replace(value.claim, facts_digest="sha256:" + "e" * 64))
    with pytest.raises((completion.CohortCompletionError, CohortClaimStorageError)):
        _apply(value, case)
    row = RayTaskCohortClaim.objects.get()
    assert row.disposition == "OPEN" and row.resolved_at is None


def test_existing_outer_transaction_is_refused_before_any_resolution(case):
    value = _started(case)
    with transaction.atomic(), pytest.raises(completion.CohortCompletionError):
        _apply(value, case)
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


@pytest.mark.parametrize("boundary,invoked", [("outer", False), ("outer", None), ("nested", True)])
def test_durable_jobs_refusal_cannot_establish_outer_noninvocation(case, boundary, invoked):
    value = _started(case, "ray_job")
    result = CohortExecutionResult(
        value.prepared.identity,
        value.prepared.request_digest,
        value.prepared.contract_digest,
        refusal="runtime_mismatch",
        boundary=boundary,
        application_invoked=invoked,
    )
    held = _apply(value, case, result)
    row = RayTaskCohortClaim.objects.get()
    assert not held.applied and row.hold_reason == "invalid_completion"
    assert row.hold_application_invoked is None and row.disposition == "HELD"


def test_durable_jobs_nested_refusal_remains_unknown(case):
    value = _started(case, "ray_job")
    result = CohortExecutionResult(
        value.prepared.identity,
        value.prepared.request_digest,
        value.prepared.contract_digest,
        refusal="nested_refusal",
        boundary="nested",
        application_invoked=None,
    )
    assert not _apply(value, case, result).applied
    row = RayTaskCohortClaim.objects.get()
    assert row.hold_boundary == "nested" and row.hold_application_invoked is None


@pytest.mark.parametrize(
    "family,provenance",
    [
        ("sync", "durable_job_completion"),
        ("ray_core", "durable_job_completion"),
        ("ray_job", "owned_direct"),
        ("sync", "logs"),
        ("sync", True),
    ],
)
def test_caller_must_select_the_exact_supported_transport_origin(case, family, provenance):
    value = _started(case, family)
    with pytest.raises(completion.CohortCompletionError):
        completion.apply_cohort_completion(
            value,
            _result(value),
            provenance=provenance,
            apply_completion=lambda *args, **kwargs: pytest.fail("Wrong transport origin"),
            now=case.now,
        )
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


@pytest.mark.parametrize("mode", ["success", "retry", "claim-pause", "enqueue-pause", "missing"])
def test_real_lifecycle_callback_preserves_attempt_history_and_explicit_protocol_three(case, mode):
    value = _started(case)
    if mode == "claim-pause":
        _pause(claims=True)
    elif mode == "enqueue-pause":
        _pause(enqueues=True)
    elif mode == "missing":
        RayMaintenancePolicy.objects.all().delete()

    def apply(task, decoded, *, retry_admitted):
        fences = {
            "expected_claimed_by_worker": value.claim.owner.worker_id,
            "expected_attempt_number": value.prepared.identity.attempt_number,
            "expected_execution_generation": value.prepared.identity.execution_generation,
            "supported_protocols": ExecutionProtocolRange(3, 3),
        }
        if decoded.completion.success:
            return lifecycle.succeed_task(task, result_data="5", result_reference=None, **fences)
        return lifecycle.record_failure(
            task, error_message=decoded.completion.error, retry=retry_admitted, **fences
        )

    applied = _apply(value, case, _result(value, success=mode == "success"), callback=apply)
    expected = (
        "SUCCEEDED"
        if mode == "success"
        else "QUEUED"
        if mode in {"retry", "claim-pause"}
        else "FAILED"
    )
    assert applied.dispatch.execution.state == expected
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"
    attempt = TaskAttempt.objects.get(execution=case.task)
    assert attempt.state == ("SUCCEEDED" if mode == "success" else "FAILED")
    assert attempt.attempt_number == value.prepared.identity.attempt_number


def test_broken_maintenance_query_rolls_back_savepoint_before_terminal_completion(
    case, monkeypatch
):
    value = _started(case)

    @contextmanager
    def missing_table():
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM django_ray_missing_maintenance_table")
        yield

    monkeypatch.setattr(completion, "maintenance_admission_barrier", missing_table)
    result = _apply(value, case, _result(value, success=False))
    assert result.applied and not result.retry_admitted
    assert result.dispatch.execution.state == "FAILED"
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"


def test_late_completion_requires_current_adopted_owner_but_preserves_original_claim(case):
    value = _started(case)
    held = dispatch.hold_cohort_dispatch(
        value, reason=CohortHoldReason.TRANSPORT_UNCERTAIN, now=case.now
    )
    case.lease.is_active = False
    case.lease.stopped_at = case.now
    case.lease.save(update_fields=["is_active", "stopped_at"])
    _, adopter = _lease("adopter")
    with transaction.atomic():
        adopted = completion.storage.adopt_cohort_claim(
            adopter,
            held.claim.claim_id,
            expected_identity=held.claim.facts.identity,
            expected_revision=held.claim.revision,
            expected_owner=held.claim.owner,
            now=case.now,
        )
    with pytest.raises(CohortClaimStorageError):
        _apply(held, case)
    assert RayTaskCohortClaim.objects.get().disposition == "HELD"
    assert _apply(replace(held, claim=adopted), case).applied
    final = RayTaskCohortClaim.objects.get()
    assert final.disposition == "RESOLVED" and final.owner_lease_id == adopter.worker_id
    assert (
        completion.storage._record(final).facts.worker_lease_id == held.claim.facts.worker_lease_id
    )
