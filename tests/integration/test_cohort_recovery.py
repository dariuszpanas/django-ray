"""Restart ownership preserves the original Jobs completion and never replays it."""

from dataclasses import replace
from datetime import timedelta

import pytest
from django.db import connection, transaction

from django_ray import maintenance
from django_ray.models import (
    RayTargetAttestationRevision,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayWorkerTargetCapability,
)
from django_ray.runner import cohort_completion as completion
from django_ray.runner import cohort_dispatch as dispatch
from django_ray.runner import cohort_recovery as recovery
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.runner.ray_job import RayJobRunner
from django_ray.target import capabilities, coordination
from django_ray.target.attestation import RayRunnerFamily, decode_ray_target_expectation
from django_ray.target.cohort_claim import CohortHoldReason
from django_ray.target.cohort_transport import encode_cohort_execution_result
from tests.integration.test_cohort_claim_storage import (
    _claim,
    _fresh_draining_ray_arguments,
    _lease,
    _published_job_qualification,
    _ray_arguments,
)
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import _result, _started
from tests.integration.test_cohort_completion import (
    isolated_completion_controls as isolated_completion_controls,
)
from tests.integration.test_cohort_selection import _alias, _clone, _qualified
from tests.integration.test_ray_worker_target_capabilities import _attestation

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _arguments(value):
    return {
        "binding_spec": value.claim.facts.binding,
        "manager_runtime": value.claim.facts.manager,
        "capability_id": value.claim.facts.capability.capability_id,
        "capability_revision": value.claim.facts.capability.revision,
        "job_qualification": value.claim.facts.job_qualification,
    }


def _qualify_adopter(case, value, *, name="adopter", stop_source=True, queues=("default",)):
    """Obtain fresh endpoint proof through the actual publisher's fake HTTP seam."""
    old_lease, old_owner = case.lease, case.owner
    if stop_source:
        old_lease.is_active = False
        old_lease.stopped_at = case.now
        old_lease.save(update_fields=["is_active", "stopped_at"])
    original = RayWorkerTargetCapability.objects.select_related("target_policy", "attestation").get(
        pk=value.claim.facts.capability.capability_id
    )
    policy = original.target.policy_revisions.order_by("-revision").first()
    assert policy is not None
    expectation = decode_ray_target_expectation(policy.expectation_json)
    case.lease, case.owner = _lease(name)
    case.now += timedelta(seconds=2)
    case.lease.last_heartbeat_at = case.now
    case.lease.save(update_fields=["last_heartbeat_at"])
    latest = (
        RayTargetAttestationRevision.objects.filter(policy=policy).order_by("-revision").first()
    )
    assert latest is not None
    with transaction.atomic():
        capabilities._locked_exact_lease(case.owner, using="default", vendor=connection.vendor)
        capabilities._locked_capability_target(
            target_key=expectation.target_key, using="default", vendor=connection.vendor
        )
        observed = coordination._record_ray_target_attestation_locked(
            expectation.target_key,
            _attestation(
                expectation, observed_at=case.now, expires_at=case.now + timedelta(seconds=25)
            ),
            expected_policy_revision=policy.revision,
            expected_attestation_revision=latest.revision,
            now=case.now,
        )
        capabilities._advertise_ray_worker_target_capability_locked(
            case.owner,
            expectation.target_key,
            expectation.runtime,
            manager_runner_family=RayRunnerFamily.RAY_JOB,
            expected_policy_revision=policy.revision,
            expected_attestation_revision=observed.revision,
            expected_capability_revision=0,
            now=case.now,
        )
    cap = RayWorkerTargetCapability.objects.select_related("attestation").get(
        lease_id=case.owner.worker_id
    )
    arguments = _arguments(value) | _published_job_qualification(
        case,
        expectation,
        policy,
        cap,
        challenge_ttl=60,
        native_job_id="02000000",
        probe_started=case.now,
    )
    case.monkeypatch.setattr(recovery, "_clock", lambda: case.now)
    item = _qualified(case, arguments, alias=_alias(case, queues=queues))
    return item, old_lease, old_owner


def _recover(case, value, item, **kwargs):
    return recovery.recover_cohort_jobs(
        case.owner,
        qualifications=(item,),
        manager_runtime=value.claim.facts.manager,
        now=case.now,
        **kwargs,
    )


def _complete(case, carrier, serialized, *, callback=None):
    RayTaskExecution.objects.filter(pk=carrier.execution.pk).update(completion_data=serialized)

    def terminal(task, decoded, *, retry_admitted):
        assert decoded.completion.identity == carrier.claim.facts.identity
        assert not retry_admitted
        task.state = "SUCCEEDED"
        task.save(update_fields=["state"])
        return True

    return completion.apply_recovered_cohort_job_completion(
        carrier,
        serialized,
        apply_completion=terminal if callback is None else callback,
        now=case.now,
    )


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("draining", [False, True])
def test_restart_adopts_same_job_and_completes_with_original_facts(case, held, draining):
    value = _started(case, "ray_job")
    serialized = encode_cohort_execution_result(_result(value))
    if held:
        value = dispatch.hold_cohort_dispatch(
            value, reason=CohortHoldReason.DISPATCH_UNCERTAIN, now=case.now
        )
    before = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    if draining:
        _fresh_draining_ray_arguments(case, _arguments(value))
    item, _, old_owner = _qualify_adopter(case, value)
    (carrier,) = _recover(case, value, item)
    assert carrier.claim.owner == case.owner != old_owner
    assert carrier.claim.facts == value.claim.facts
    assert carrier.claim.facts_digest == value.claim.facts_digest
    assert carrier.request_digest == value.prepared.request_digest
    assert carrier.contract_digest == value.prepared.contract_digest
    assert carrier.handle.ray_job_id == value.execution.ray_job_id
    assert not hasattr(carrier, "prepared") and not hasattr(carrier, "request_json")
    assert _recover(case, value, item) == ()
    result = _complete(case, carrier, serialized)
    after = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    assert result.applied and result.dispatch.execution.state == "SUCCEEDED"
    assert (after.facts_json, after.held_at, after.hold_evidence_digest) == (
        before.facts_json,
        before.held_at,
        before.hold_evidence_digest,
    )


def test_recovery_never_replans_or_reads_inputs_or_resubmits_unknown_job(case, monkeypatch):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    monkeypatch.setattr(
        dispatch,
        "prepare_claimed_cohort_dispatch",
        lambda *a, **k: pytest.fail("Replanned old request"),
    )
    (carrier,) = _recover(case, value, item)
    assert carrier.request_reference is None
    assert carrier.execution.completion_data is None
    assert carrier.execution.state == "RUNNING"
    assert carrier.claim.disposition.value == "OPEN"
    assert carrier.claim.facts.identity == value.claim.facts.identity


def test_admission_pause_does_not_block_same_generation_adoption_or_success(case):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    maintenance.replace_maintenance_policy(
        (),
        pause_enqueues=True,
        pause_claims=True,
        expected_revision=maintenance.read_maintenance_policy().revision,
        actor="test",
        reason="coordinated-drain",
        authorized=True,
    )
    (carrier,) = _recover(case, value, item)
    assert _complete(case, carrier, encode_cohort_execution_result(_result(value))).applied


def test_original_expired_proof_and_removed_old_probe_do_not_erase_completion(case):
    value = _started(case, "ray_job")
    original_qualification = value.claim.facts.job_qualification
    assert original_qualification is not None
    # Publish the adopter late enough that the original endpoint proof has expired.
    case.now += timedelta(seconds=30)
    item, old_lease, _ = _qualify_adopter(case, value)
    old_lease.delete()
    assert original_qualification.endpoint_expires_at < case.now
    (carrier,) = _recover(case, value, item)
    assert _complete(case, carrier, encode_cohort_execution_result(_result(value))).applied


@pytest.mark.parametrize("mode", ["live-owner", "future-owner", "wrong-queue", "expired-endpoint"])
def test_current_unqualified_or_live_work_is_not_adopted(case, mode):
    value = _started(case, "ray_job")
    item, old_lease, _ = _qualify_adopter(
        case,
        value,
        stop_source=mode not in {"live-owner", "future-owner"},
        queues=("other",) if mode == "wrong-queue" else ("default",),
    )
    if mode == "future-owner":
        old_lease.last_heartbeat_at = case.now + timedelta(seconds=100)
        old_lease.save(update_fields=["last_heartbeat_at"])
    if mode == "expired-endpoint":
        case.now = item.job_qualification.endpoint_expires_at
        case.lease.last_heartbeat_at = case.now
        case.lease.save(update_fields=["last_heartbeat_at"])
    assert _recover(case, value, item) == ()
    assert RayTaskCohortClaim.objects.get().owner_lease_id == value.claim.owner.worker_id


def test_fresh_proof_is_rechecked_after_adoption_and_rolls_back_on_expiry(case, monkeypatch):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    original = recovery.storage.adopt_cohort_claim

    def expires(*args, **kwargs):
        result = original(*args, **kwargs)
        case.now = item.job_qualification.endpoint_expires_at
        return result

    monkeypatch.setattr(recovery.storage, "adopt_cohort_claim", expires)
    assert _recover(case, value, item) == ()
    row = RayTaskCohortClaim.objects.get()
    assert (row.owner_lease_id, row.revision) == (value.claim.owner.worker_id, value.claim.revision)
    assert RayTaskExecution.objects.get().claimed_by_worker == value.claim.owner.worker_id


@pytest.mark.parametrize("change", ["request", "contract", "identity", "stale-owner"])
def test_crossed_recovered_completion_is_refused_without_application(case, change):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    (carrier,) = _recover(case, value, item)
    result = _result(value)
    if change == "stale-owner":
        carrier = replace(carrier, claim=value.claim)
        with pytest.raises(RuntimeError):
            _complete(case, carrier, encode_cohort_execution_result(result))
    else:
        changes = {
            "request": {"request_digest": "sha256:" + "d" * 64},
            "contract": {"contract_digest": "sha256:" + "d" * 64},
            "identity": {
                "identity": replace(result.identity, execution_generation=2),
                "completion_json": None,
                "refusal": "runtime_mismatch",
                "boundary": "nested",
                "application_invoked": None,
            },
        }
        applied = _complete(
            case,
            carrier,
            encode_cohort_execution_result(replace(result, **changes[change])),
            callback=lambda *a, **k: pytest.fail("Crossed completion reached application"),
        )
        assert not applied.applied
        assert (
            RayTaskCohortClaim.objects.get().hold_reason
            == CohortHoldReason.INVALID_COMPLETION.value
        )
    assert RayTaskExecution.objects.get().state == "RUNNING"


def test_recovered_callback_false_rolls_back_resolution(case):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    (carrier,) = _recover(case, value, item)
    with pytest.raises(completion.CohortCompletionError):
        _complete(
            case,
            carrier,
            encode_cohort_execution_result(_result(value)),
            callback=lambda *a, **k: False,
        )
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


@pytest.mark.parametrize("version", [(2, 58, False), (2, 58, 1), None, [2, 58, 0]])
def test_adopter_runtime_requires_exact_primitive_tuple(case, version):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    with pytest.raises(recovery.CohortRecoveryError):
        recovery.recover_cohort_jobs(
            case.owner,
            qualifications=(item,),
            manager_runtime=replace(value.claim.facts.manager, ray_version=version),
            now=case.now,
        )


def _extra_started(case, value, *, name, queue="default", item=None):
    case.task = _clone(case, name=name, priority=100, queue=queue)
    arguments = _arguments(value)
    if item is not None:
        arguments.update(
            capability_id=item.shared.capability_id,
            capability_revision=item.shared.capability_revision,
            job_qualification=item.job_qualification,
        )
    claim = _claim(case, **arguments)
    case.task.refresh_from_db()
    prepared = dispatch.prepare_claimed_cohort_dispatch(
        ClaimedCohortTask(case.task, claim, None),
        transport="ray-job",
        now=case.now,
    )
    handle = RayJobRunner().cohort_submission_handle(
        case.task,
        jobs_endpoint=claim.facts.job_qualification.jobs_endpoint,
    )
    return dispatch.mark_cohort_dispatch_started(prepared, jobs_handle=handle, now=case.now)


@pytest.mark.parametrize("ineligible", ["live-owner", "queue", "recreated-owner"])
def test_ineligible_higher_priority_claim_is_excluded_before_limit(case, ineligible):
    value = _started(case, "ray_job")
    original_lease, original_owner = case.lease, case.owner
    if ineligible in {"live-owner", "recreated-owner"}:
        other_item, _, _ = _qualify_adopter(case, value, name="other-owner", stop_source=False)
        high = _extra_started(case, value, name="higher", item=other_item)
        if ineligible == "recreated-owner":
            case.lease.delete()
            _lease("other-owner", started=case.now)
    else:
        high = _extra_started(case, value, name="higher", queue="unserved")
    case.task, case.lease, case.owner = value.execution, original_lease, original_owner
    item, _, _ = _qualify_adopter(case, value)
    (carrier,) = _recover(case, value, item, limit=1)
    assert carrier.execution.pk == value.execution.pk
    high.execution.refresh_from_db()
    assert high.execution.claimed_by_worker == high.claim.owner.worker_id
    assert high.execution.execution_generation == high.claim.facts.identity.execution_generation


def test_retiring_destination_is_excluded_before_limit_but_owned_completion_continues(
    case, monkeypatch
):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    (carrier,) = _recover(case, value, item)
    maintenance.request_worker_retirement(
        case.owner,
        expected_revision=0,
        actor="test",
        reason="retire-destination",
        authorized=True,
    )
    monkeypatch.setattr(
        recovery, "_candidates", lambda *a, **k: pytest.fail("Retired candidate scan")
    )
    assert _recover(case, value, item) == ()
    assert _complete(case, carrier, encode_cohort_execution_result(_result(value))).applied


def test_retirement_committed_after_advisory_scan_blocks_destination_adoption(case, monkeypatch):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    actual = recovery._candidates

    def retirement_wins(*args, **kwargs):
        rows = actual(*args, **kwargs)
        maintenance.request_worker_retirement(
            case.owner,
            expected_revision=0,
            actor="test",
            reason="retire-race",
            authorized=True,
        )
        return rows

    monkeypatch.setattr(recovery, "_candidates", retirement_wins)
    assert _recover(case, value, item) == ()
    assert RayTaskCohortClaim.objects.get().owner_lease_id == value.claim.owner.worker_id


def test_quarantined_claim_keeps_same_generation_adoption_and_authentic_completion(
    case, monkeypatch
):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    maintenance.set_task_quarantine(
        value.claim.facts.identity,
        quarantined=True,
        expected_revision=0,
        actor="test",
        reason="preserve-unknown",
        authorized=True,
    )
    (carrier,) = _recover(case, value, item)
    assert _complete(case, carrier, encode_cohort_execution_result(_result(value))).applied


def test_recovery_lock_order_is_barrier_sorted_leases_then_proof_then_task(case, monkeypatch):
    value = _started(case, "ray_job")
    item, _, old_owner = _qualify_adopter(case, value, name="zzz-adopter")
    events = []
    original_barrier = recovery.maintenance_admission_barrier
    original_lease = capabilities._locked_exact_lease
    original_proof = recovery.storage._proof
    original_task = recovery.storage._lock_task

    def barrier(*args, **kwargs):
        events.append("barrier")
        return original_barrier(*args, **kwargs)

    def lease(identity, **kwargs):
        events.append(identity.worker_id)
        return original_lease(identity, **kwargs)

    def proof(**kwargs):
        events.append("proof")
        return original_proof(**kwargs)

    def task(*args, **kwargs):
        events.append("task")
        return original_task(*args, **kwargs)

    monkeypatch.setattr(recovery, "maintenance_admission_barrier", barrier)
    monkeypatch.setattr(capabilities, "_locked_exact_lease", lease)
    monkeypatch.setattr(recovery.storage, "_proof", proof)
    monkeypatch.setattr(recovery.storage, "_lock_task", task)
    assert len(_recover(case, value, item)) == 1
    assert events[:3] == ["barrier", old_owner.worker_id, case.owner.worker_id]
    assert events.index("proof") < events.index("task")


def test_newly_qualified_session_cannot_replace_the_original_bound_target(case):
    value = _started(case, "ray_job")
    case.lease.is_active = False
    case.lease.stopped_at = case.now
    case.lease.save(update_fields=["is_active", "stopped_at"])
    case.lease, case.owner = _lease("adopter")
    arguments = _ray_arguments(
        case,
        RayRunnerFamily.RAY_JOB,
        target_key="new-session-target",
        cluster_session="session_new",
        observed_at=case.now,
    )
    item = _qualified(case, arguments)
    case.monkeypatch.setattr(recovery, "_clock", lambda: case.now)
    assert _recover(case, value, item) == ()
    assert RayTaskCohortClaim.objects.get().owner_lease_id == value.claim.owner.worker_id


def test_current_endpoint_qualification_cannot_authorize_another_endpoint(case):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    crossed = replace(
        item,
        job_qualification=replace(item.job_qualification, jobs_endpoint="http://other:8265"),
    )
    assert _recover(case, value, crossed) == ()
    assert RayTaskCohortClaim.objects.get().owner_lease_id == value.claim.owner.worker_id


def test_recovered_completion_observation_is_fenced_against_current_durable_bytes(case):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    (carrier,) = _recover(case, value, item)
    observed = encode_cohort_execution_result(_result(value))
    latest = encode_cohort_execution_result(_result(value, success=False))
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(completion_data=latest)
    with pytest.raises(completion.CohortCompletionError):
        completion.apply_recovered_cohort_job_completion(
            carrier,
            observed,
            apply_completion=lambda *a, **k: pytest.fail("Stale completion"),
            now=case.now,
        )
    assert RayTaskCohortClaim.objects.get().revision == carrier.claim.revision


@pytest.mark.parametrize("limit", [False, 0, 101, 1.0])
def test_recovery_requires_a_finite_integer_limit(case, limit):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    with pytest.raises(recovery.CohortRecoveryError):
        _recover(case, value, item, limit=limit)


def test_clock_regression_after_lease_lock_refuses_without_ownership_transfer(case, monkeypatch):
    value = _started(case, "ray_job")
    item, _, _ = _qualify_adopter(case, value)
    original = capabilities._locked_exact_lease

    def regresses(*args, **kwargs):
        result = original(*args, **kwargs)
        case.now -= timedelta(microseconds=1)
        return result

    monkeypatch.setattr(capabilities, "_locked_exact_lease", regresses)
    assert _recover(case, value, item) == ()
    assert RayTaskCohortClaim.objects.get().owner_lease_id == value.claim.owner.worker_id
