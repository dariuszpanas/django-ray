"""Request-before-dispatch durability without starting an application or Ray."""

from dataclasses import replace
from datetime import timedelta

import pytest
from django.db import connection, transaction

from django_ray.models import RayTaskCohortClaim, RayTaskExecution
from django_ray.runner import cohort_dispatch as dispatch
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_claim import CohortHoldReason
from django_ray.target.cohort_claim_storage import CohortClaimStorageError
from django_ray.target.cohort_transport import (
    prepare_cohort_execution,
    validate_prepared_cohort_execution,
)
from tests.integration.test_cohort_claim_storage import _claim, _ray_arguments
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _claimed(case, family="sync"):
    arguments = {} if family == "sync" else _ray_arguments(case, RayRunnerFamily(family))
    record = _claim(case, **arguments)
    case.task.refresh_from_db()
    return ClaimedCohortTask(case.task, record, None)


@pytest.mark.parametrize("family", ["sync", "ray_core", "ray_job"])
def test_preparation_persists_exact_request_without_dispatch(case, family):
    claimed = _claimed(case, family)
    value = dispatch.prepare_claimed_cohort_dispatch(
        claimed, transport="ray-job" if family == "ray_job" else None, now=case.now
    )
    request, contract = validate_prepared_cohort_execution(value.prepared, task=case.task)
    row = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    assert row.prepared_request_digest == value.prepared.request_digest
    assert row.dispatched_at is None
    assert contract.cohort_evidence_digest == claimed.claim.facts_digest
    assert contract.cohort_evidence_id == row.pk
    assert request.identity == claimed.claim.facts.identity
    with pytest.raises(CohortClaimStorageError):
        dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)


def test_sync_dispatch_is_committed_once_before_application(case):
    value = dispatch.prepare_claimed_cohort_dispatch(_claimed(case), now=case.now)
    started = dispatch.mark_cohort_dispatch_started(value, now=case.now)
    assert started.claim.dispatched_at == case.now
    assert RayTaskCohortClaim.objects.get().dispatched_at == case.now
    with pytest.raises(CohortClaimStorageError):
        dispatch.mark_cohort_dispatch_started(value, now=case.now)
    held = dispatch.hold_cohort_dispatch(
        started, reason=CohortHoldReason.DISPATCH_UNCERTAIN, now=case.now
    )
    row = RayTaskCohortClaim.objects.get()
    assert row.disposition == "HELD" and row.hold_application_invoked is None
    assert held.claim.dispatched_at == case.now
    with pytest.raises(CohortClaimStorageError):
        dispatch.mark_cohort_dispatch_started(held, now=case.now)


def test_jobs_exact_handle_is_persisted_atomically_before_remote_call(case):
    from django_ray.runner.ray_job import RayJobRunner

    value = dispatch.prepare_claimed_cohort_dispatch(
        _claimed(case, "ray_job"), transport="ray-job", now=case.now
    )
    endpoint = value.claim.facts.job_qualification.jobs_endpoint
    handle = RayJobRunner().cohort_submission_handle(case.task, jobs_endpoint=endpoint)
    started = dispatch.mark_cohort_dispatch_started(value, jobs_handle=handle, now=case.now)
    case.task.refresh_from_db()
    assert (case.task.ray_job_id, case.task.ray_address) == (handle.ray_job_id, endpoint)
    assert (started.execution.ray_job_id, started.execution.ray_address) == (
        handle.ray_job_id,
        endpoint,
    )
    assert RayTaskCohortClaim.objects.get().dispatched_at == started.claim.dispatched_at


@pytest.mark.parametrize("wrong", ["endpoint", "submission", "missing"])
def test_jobs_rejects_an_unqualified_or_missing_handle_without_dispatch(case, wrong):
    from django_ray.runner.ray_job import RayJobRunner

    value = dispatch.prepare_claimed_cohort_dispatch(
        _claimed(case, "ray_job"), transport="ray-job", now=case.now
    )
    endpoint = value.claim.facts.job_qualification.jobs_endpoint
    handle = RayJobRunner().cohort_submission_handle(case.task, jobs_endpoint=endpoint)
    if wrong == "endpoint":
        handle.ray_address = "http://different.invalid:8265"
    elif wrong == "submission":
        handle.ray_job_id += "extra"
    else:
        handle = None
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.mark_cohort_dispatch_started(value, jobs_handle=handle, now=case.now)
    assert RayTaskCohortClaim.objects.get().dispatched_at is None


def test_preparing_original_claim_does_not_renew_or_require_expired_admission(case):
    claimed = _claimed(case, "ray_core")
    case.now += timedelta(seconds=30)
    case.lease.last_heartbeat_at = case.now
    case.lease.save(update_fields=["last_heartbeat_at"])
    value = dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
    _, contract = validate_prepared_cohort_execution(value.prepared)
    assert contract.claim_attestation.expires_at < case.now
    assert contract.claimed_at == claimed.claim.facts.claimed_at
    assert contract.claim_attestation_digest == claimed.claim.facts.claim_attestation_digest


def test_input_change_during_preparation_rolls_back_prepared_marker(case, monkeypatch):
    claimed = _claimed(case)
    original = dispatch.prepare_captured_cohort_execution

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        RayTaskExecution.objects.filter(pk=case.task.pk).update(args_json="[9,9]")
        return result

    monkeypatch.setattr(dispatch, "prepare_captured_cohort_execution", changed)
    with pytest.raises(ValueError):
        dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
    assert RayTaskCohortClaim.objects.get().prepared_at is None


def test_capture_freezes_inputs_and_trust_before_filesystem_preparation(
    case, monkeypatch, settings
):
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.workflow import plans

    claimed = _claimed(case)
    settings.DJANGO_RAY = {"WORKFLOW_PLAN_TRUST_IDENTITY": {"trust_domain": "captured"}}
    expected = plans.runtime_env_plan_identity(
        normalize_runtime_env({}), trust_identity={"trust_domain": "captured"}
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Capture must not scan files, and callback preparation must not read SQL")

    with monkeypatch.context() as capture_patch:
        capture_patch.setattr(plans, "runtime_env_plan_identity", forbidden)
        source = dispatch.capture_claimed_cohort_preparation(claimed)
    original_args = claimed.execution.args_json
    claimed.execution.args_json = "[8,9]"
    settings.DJANGO_RAY["WORKFLOW_PLAN_TRUST_IDENTITY"]["trust_domain"] = "changed"
    with connection.execute_wrapper(forbidden):
        prepared = dispatch.prepare_captured_cohort_execution(source)
    request, _ = validate_prepared_cohort_execution(prepared)
    assert request.serialized_args == original_args
    assert request.runtime_env_plan_identity == expected.as_transport_dict()
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.commit_claimed_cohort_preparation(claimed, source, prepared, now=case.now)
    assert RayTaskCohortClaim.objects.get().prepared_at is None
    claimed.execution.refresh_from_db()
    committed = dispatch.commit_claimed_cohort_preparation(claimed, source, prepared, now=case.now)
    assert committed.claim.prepared_request_digest == prepared.request_digest


@pytest.mark.parametrize("mutation", ["contract", "environment", "profile", "transport", "trust"])
def test_constructed_preparation_cannot_replace_captured_claim_controls(case, mutation):
    from django_ray.runtime.runtime_env import normalize_runtime_env

    claimed = _claimed(case, "ray_core")
    source = dispatch.capture_claimed_cohort_preparation(claimed, transport="direct-ray-core")
    if mutation == "contract":
        forged = replace(
            source, contract=replace(source.contract, cohort_evidence_id=source.claim.claim_id + 1)
        )
    elif mutation == "environment":
        environment = normalize_runtime_env({"env_vars": {"DIFFERENT": "yes"}})
        forged = replace(
            source, environment_json=environment.serialized, environment_digest=environment.digest
        )
    elif mutation == "profile":
        forged = replace(source, environment_profile="another-profile")
    elif mutation == "transport":
        forged = replace(source, transport="ray-client")
    else:
        forged = replace(source, trust_identity_json='{"trust_domain":"crossed"}')
    prepared = dispatch.prepare_captured_cohort_execution(forged)
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.commit_claimed_cohort_preparation(claimed, source, prepared, now=case.now)
    assert RayTaskCohortClaim.objects.get().prepared_at is None


def test_cancellation_before_dispatch_rolls_back_dispatch_marker(case):
    value = dispatch.prepare_claimed_cohort_dispatch(_claimed(case), now=case.now)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.mark_cohort_dispatch_started(value, now=case.now)
    assert RayTaskCohortClaim.objects.get().dispatched_at is None


def test_constructed_claim_facts_do_not_replace_ledger_facts(case):
    from django_ray.target.cohort_claim import cohort_claim_facts_digest

    claimed = _claimed(case)
    facts = replace(
        claimed.claim.facts, claimed_at=claimed.claim.facts.claimed_at - timedelta(seconds=1)
    )
    forged = replace(claimed.claim, facts=facts, facts_digest=cohort_claim_facts_digest(facts))
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.prepare_claimed_cohort_dispatch(replace(claimed, claim=forged), now=case.now)
    assert RayTaskCohortClaim.objects.get().prepared_at is None


def test_preparation_requires_outside_transaction_and_live_exact_owner(case):
    claimed = _claimed(case)
    with transaction.atomic(), pytest.raises(dispatch.CohortDispatchError):
        dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
    case.lease.is_active = False
    case.lease.save(update_fields=["is_active"])
    with pytest.raises(CohortClaimStorageError):
        dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
    assert RayTaskCohortClaim.objects.get().prepared_at is None


def test_replacement_canonical_request_cannot_mark_the_original_request_dispatched(case):
    value = dispatch.prepare_claimed_cohort_dispatch(_claimed(case), now=case.now)
    _, contract = validate_prepared_cohort_execution(value.prepared)
    replacement = prepare_cohort_execution(
        value.execution,
        contract=replace(contract, cohort_evidence_id=contract.cohort_evidence_id + 1),
    )
    forged = replace(
        value,
        prepared=replacement,
        claim=replace(value.claim, prepared_request_digest=replacement.request_digest),
    )
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.mark_cohort_dispatch_started(forged, now=case.now)
    row = RayTaskCohortClaim.objects.get()
    assert row.prepared_request_digest == value.prepared.request_digest
    assert row.dispatched_at is None
    assert row.revision == value.claim.revision


def test_constructed_claim_facts_cannot_cross_the_dispatch_marker(case):
    from django_ray.target.cohort_claim import cohort_claim_facts_digest

    value = dispatch.prepare_claimed_cohort_dispatch(_claimed(case), now=case.now)
    facts = replace(
        value.claim.facts, claimed_at=value.claim.facts.claimed_at - timedelta(seconds=1)
    )
    forged = replace(
        value,
        claim=replace(value.claim, facts=facts, facts_digest=cohort_claim_facts_digest(facts)),
    )
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.mark_cohort_dispatch_started(forged, now=case.now)
    row = RayTaskCohortClaim.objects.get()
    assert row.facts_digest == value.claim.facts_digest
    assert row.dispatched_at is None
    assert row.revision == value.claim.revision


def test_preparation_runs_without_transaction_and_cancellation_rolls_back_marker(case, monkeypatch):
    claimed = _claimed(case)
    original = dispatch.prepare_captured_cohort_execution
    observations = []

    def cancelled(*args, **kwargs):
        observations.append((connection.in_atomic_block, connection.get_autocommit()))
        result = original(*args, **kwargs)
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")
        return result

    monkeypatch.setattr(dispatch, "prepare_captured_cohort_execution", cancelled)
    with pytest.raises(dispatch.CohortDispatchError):
        dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
    row = RayTaskCohortClaim.objects.get()
    case.task.refresh_from_db()
    assert observations == [(False, True)]
    assert case.task.state == "CANCELLING"
    assert row.prepared_at is None and row.dispatched_at is None
    assert row.revision == claimed.claim.revision


@pytest.mark.parametrize("operation", ["prepare", "dispatch", "hold"])
def test_manual_transaction_refuses_before_preparation_or_ledger_mutation(
    case, monkeypatch, operation
):
    claimed = _claimed(case)
    value = None
    if operation != "prepare":
        value = dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
    if operation == "hold":
        value = dispatch.mark_cohort_dispatch_started(value, now=case.now)
    original_revision = RayTaskCohortClaim.objects.get().revision

    def forbidden(*args, **kwargs):
        pytest.fail("Input preparation ran inside a manually managed transaction")

    monkeypatch.setattr(dispatch, "prepare_captured_cohort_execution", forbidden)
    connection.set_autocommit(False)
    try:
        assert not connection.in_atomic_block
        with pytest.raises(dispatch.CohortDispatchError):
            if operation == "prepare":
                dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
            elif operation == "dispatch":
                dispatch.mark_cohort_dispatch_started(value, now=case.now)
            else:
                dispatch.hold_cohort_dispatch(
                    value, reason=CohortHoldReason.DISPATCH_UNCERTAIN, now=case.now
                )
    finally:
        connection.rollback()
        connection.set_autocommit(True)
    assert RayTaskCohortClaim.objects.get().revision == original_revision


@pytest.mark.parametrize("held_during_preparation", [False, True])
def test_owned_preparation_keeps_parent_heartbeat_and_rechecks_held_claim(
    case, monkeypatch, held_during_preparation
):
    from threading import Event, get_ident
    from time import monotonic, sleep

    from django.db.backends.base.base import BaseDatabaseWrapper

    from django_ray.models import TaskWorkerLease
    from django_ray.runner import cohort_preparation as preparation
    from django_ray.workflow import plans
    from tests.integration.test_cohort_claim_storage import _hold

    claimed = _claimed(case)
    # Deferred ORM attributes must be materialized by the parent capture.
    deferred = RayTaskExecution.objects.only("pk").get(pk=case.task.pk)
    claimed = replace(claimed, execution=deferred)
    entered, release = Event(), Event()
    owner_thread = get_ident()
    original_plan = plans.runtime_env_plan_identity
    original_cursor = BaseDatabaseWrapper.cursor
    planned = []

    def bounded_manifest(*args, **kwargs):
        assert get_ident() != owner_thread, "Parent must not build the filesystem manifest"
        planned.append(get_ident())
        entered.set()
        assert release.wait(3), "Parent must release its owned test callback"
        return original_plan(*args, **kwargs)

    def parent_only_cursor(self):
        assert get_ident() == owner_thread, "Preparation callback must not touch Django SQL"
        return original_cursor(self)

    monkeypatch.setattr(plans, "runtime_env_plan_identity", bounded_manifest)
    monkeypatch.setattr(BaseDatabaseWrapper, "cursor", parent_only_cursor)
    monkeypatch.setattr(preparation, "_clock", lambda: (case.now, monotonic()))
    controller = preparation.CohortPreparationController()
    ticket = controller.begin(claimed)
    assert ticket is not None
    try:
        assert entered.wait(2)
        captured_fields = set(dispatch._PreparationTask.__dataclass_fields__) - {"pk"}
        assert not (captured_fields & deferred.get_deferred_fields())
        for _ in range(3):
            case.now += timedelta(microseconds=1)
            assert (
                TaskWorkerLease.objects.filter(pk=case.lease.pk).update(last_heartbeat_at=case.now)
                == 1
            )
            assert controller.poll(ticket).stage == "preparing"
        if held_during_preparation:
            held = _hold(case, claimed.claim)
            assert held.disposition.value == "HELD"
    finally:
        release.set()
        deadline = monotonic() + 3
        while controller.busy:
            assert monotonic() < deadline, "Owned callback did not exit"
            sleep(0.005)

    try:
        assert controller.poll(ticket).stage == "ready"
        if held_during_preparation:
            for _ in range(2):
                with pytest.raises(preparation.CohortPreparationError) as error:
                    controller.commit(ticket, now=case.now)
                assert error.value.reason is preparation.CohortPreparationReason.COMMIT_FAILED
            row = RayTaskCohortClaim.objects.get(pk=ticket.claim.claim_id)
            assert row.disposition == "HELD" and row.revision == held.revision
            assert row.prepared_request_digest is None and row.dispatched_at is None
            assert ticket.claim.disposition.value == "OPEN"
        else:
            result = controller.commit(ticket, now=case.now)
            row = RayTaskCohortClaim.objects.get(pk=result.claim.claim_id)
            assert row.prepared_request_digest == result.prepared.request_digest
            assert row.revision == result.claim.revision > ticket.claim.revision
            assert row.dispatched_at is None
        assert len(planned) == 1
    finally:
        controller.abort(ticket)
        assert controller.retire(ticket) is True
