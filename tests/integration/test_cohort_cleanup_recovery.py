"""Completed Jobs retain independent cleanup ownership through retry/restart."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from django.db import connection, transaction

from django_ray import maintenance
from django_ray.models import RayCohortJobCleanup, RayTaskCohortClaim, RayTaskExecution
from django_ray.runner import cohort_cleanup_recovery as recovery
from django_ray.runner import cohort_dispatch as dispatch
from django_ray.target import cohort_claim_storage as claims
from django_ray.target import cohort_job_cleanup as cleanup
from django_ray.target.cohort_claim import CohortHoldReason
from tests.integration.test_cohort_claim_storage import _fresh_draining_ray_arguments, _lease
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import (
    _apply,
    _result,
    _started,
    _stored_request_reference,
)
from tests.integration.test_cohort_completion import (
    isolated_completion_controls as isolated_completion_controls,
)
from tests.integration.test_cohort_recovery import _arguments, _extra_started, _qualify_adopter

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _completed(case, *, missing=False, retry=False, held=False):
    value = _started(case, "ray_job")
    if not missing:
        _stored_request_reference(value)
    if held:
        value = dispatch.hold_cohort_dispatch(
            value, reason=CohortHoldReason.TRANSPORT_UNCERTAIN, now=case.now
        )

    def apply(task, decoded, *, retry_admitted):
        task.state = "FAILED" if retry else "SUCCEEDED"
        task.save(update_fields=["state"])
        if retry:
            assert retry_admitted
            task.state = "QUEUED"
            task.attempt_number += 1
            task.ray_address = task.ray_job_id = task.ray_job_request_reference = None
            task.completion_data = None
            task.queue_name = "changed-after-completion"
            task.save(
                update_fields=[
                    "state",
                    "attempt_number",
                    "ray_address",
                    "ray_job_id",
                    "ray_job_request_reference",
                    "completion_data",
                    "queue_name",
                ]
            )
        return True

    assert _apply(value, case, _result(value, success=not retry), callback=apply).applied
    return value, cleanup.cleanup_record(RayCohortJobCleanup.objects.get(pk=value.claim.claim_id))


def _recover(case, value, item, **kwargs):
    case.monkeypatch.setattr(recovery, "_clock", lambda: case.now)
    return recovery.recover_cohort_job_cleanups(
        case.owner,
        qualifications=(item,),
        manager_runtime=value.claim.facts.manager,
        now=case.now,
        **kwargs,
    )


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("draining", [False, True])
def test_recovery_transfers_only_cleanup_preserving_original_queue_and_claim(
    case, missing, retry, draining
):
    value, before = _completed(case, missing=missing, retry=retry, held=True)
    if draining:
        _fresh_draining_ray_arguments(case, _arguments(value))
    item, _, old_owner = _qualify_adopter(case, value)
    task_before = RayTaskExecution.objects.values().get(pk=value.execution.pk)
    claim_before = RayTaskCohortClaim.objects.values().get(pk=value.claim.claim_id)
    (recovered,) = _recover(case, value, item)
    after = recovered.cleanup
    assert after.owner == case.owner != old_owner
    assert after.revision == before.revision + 1 and after.state == "OPEN"
    assert after.updated_at == case.now
    assert (
        replace(after, owner=before.owner, revision=before.revision, updated_at=before.updated_at)
        == before
    )
    assert (recovered.expectation is None) is missing
    assert RayTaskExecution.objects.values().get(pk=value.execution.pk) == task_before
    assert RayTaskCohortClaim.objects.values().get(pk=value.claim.claim_id) == claim_before
    assert claim_before["owner_lease_id"] == old_owner.worker_id
    assert not hasattr(recovered, "prepared") and not hasattr(recovered, "request_json")
    assert _recover(case, value, item) == ()


@pytest.mark.parametrize(
    "mode",
    [
        "live-owner",
        "future-owner",
        "wrong-queue",
        "expired-endpoint",
        "crossed-endpoint",
        "no-qualification",
    ],
)
def test_unqualified_or_live_cleanup_is_filtered_before_adoption(case, mode):
    value, before = _completed(case)
    item, old_lease, _ = _qualify_adopter(
        case,
        value,
        stop_source=mode != "live-owner",
        queues=("wrong",) if mode == "wrong-queue" else ("default",),
    )
    if mode == "future-owner":
        old_lease.last_heartbeat_at = case.now + timedelta(seconds=1)
        old_lease.save(update_fields=["last_heartbeat_at"])
    elif mode == "expired-endpoint":
        case.now = item.job_qualification.endpoint_expires_at
        case.lease.last_heartbeat_at = case.now
        case.lease.save(update_fields=["last_heartbeat_at"])
    elif mode == "crossed-endpoint":
        item = replace(
            item,
            job_qualification=replace(item.job_qualification, jobs_endpoint="http://other:8265"),
        )
    if mode == "no-qualification":
        case.monkeypatch.setattr(recovery, "_clock", lambda: case.now)
        result = recovery.recover_cohort_job_cleanups(
            case.owner, qualifications=(), manager_runtime=value.claim.facts.manager, now=case.now
        )
    else:
        result = _recover(case, value, item)
    assert result == ()
    assert cleanup.cleanup_record(RayCohortJobCleanup.objects.get()) == before


def test_completion_cleanup_uses_expired_original_proof_and_survives_deleted_source_lease(case):
    value, before = _completed(case)
    case.now += timedelta(seconds=30)
    item, old_lease, _ = _qualify_adopter(case, value)
    old_lease.delete()
    assert value.claim.facts.job_qualification.endpoint_expires_at < case.now
    (recovered,) = _recover(case, value, item)
    assert recovered.cleanup.expectation_digest == before.expectation_digest
    assert recovered.cleanup.state == "OPEN"


@pytest.mark.parametrize("missing", [False, True])
def test_retiring_destination_cannot_adopt_even_an_uninspectable_obligation(case, missing):
    value, before = _completed(case, missing=missing)
    item, _, _ = _qualify_adopter(case, value)
    case.monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    maintenance.request_worker_retirement(
        case.owner, expected_revision=0, actor="test", reason="retire", authorized=True
    )
    assert _recover(case, value, item) == ()
    assert cleanup.cleanup_record(RayCohortJobCleanup.objects.get()) == before


def test_pause_and_quarantine_do_not_erase_same_generation_cleanup(case):
    value, _ = _completed(case)
    item, _, _ = _qualify_adopter(case, value)
    maintenance.replace_maintenance_policy(
        (),
        pause_enqueues=True,
        pause_claims=True,
        expected_revision=maintenance.read_maintenance_policy().revision,
        actor="test",
        reason="paused",
        authorized=True,
    )
    maintenance.set_task_quarantine(
        value.claim.facts.identity,
        quarantined=True,
        expected_revision=0,
        actor="test",
        reason="quarantine",
        authorized=True,
    )
    assert len(_recover(case, value, item)) == 1


@pytest.mark.parametrize("mode", ["clock", "proof-expiry", "lease-expiry", "cleanup-cas"])
def test_late_failure_rolls_back_only_cleanup_transfer(case, monkeypatch, mode):
    value, before = _completed(case)
    item, _, _ = _qualify_adopter(case, value)
    original = recovery.adopt_cohort_job_cleanup_locked

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        if mode == "clock":
            case.now -= timedelta(seconds=1)
        elif mode == "proof-expiry":
            case.now = item.job_qualification.endpoint_expires_at
        elif mode == "lease-expiry":
            case.now += timedelta(days=1)
        else:
            raise cleanup.CohortJobCleanupError("changed")
        return result

    monkeypatch.setattr(recovery, "adopt_cohort_job_cleanup_locked", late)
    assert _recover(case, value, item) == ()
    assert cleanup.cleanup_record(RayCohortJobCleanup.objects.get()) == before


def test_wrong_original_queue_is_excluded_before_limit(case):
    value = _started(case, "ray_job")
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(queue_name="unserved")
    other = _extra_started(case, value, name="eligible-cleanup")

    def terminal(task, decoded, *, retry_admitted):
        task.state = "SUCCEEDED"
        task.save(update_fields=["state"])
        return True

    _apply(value, case, callback=terminal)
    _apply(other, case, callback=terminal)
    item, _, _ = _qualify_adopter(case, value)
    (recovered,) = _recover(case, value, item, limit=1)
    assert recovered.cleanup.cleanup_id == other.claim.claim_id
    assert (
        RayCohortJobCleanup.objects.get(pk=value.claim.claim_id).owner_lease_id
        != case.owner.worker_id
    )


def test_recovery_never_hydrates_current_payload_or_reconstructs_submission(case, monkeypatch):
    value, _ = _completed(case, retry=True)
    item, _, _ = _qualify_adopter(case, value)
    loaded = RayTaskExecution.from_db

    def from_db(cls, db, names, values, **kwargs):
        assert not (
            {"runtime_env_json", "input_payload_json", "completion_data", "result_json"}
            & set(names)
        )
        return loaded(db, names, values, **kwargs)

    monkeypatch.setattr(
        dispatch,
        "prepare_claimed_cohort_dispatch",
        lambda *a, **k: pytest.fail("Cleanup is not submission"),
    )
    monkeypatch.setattr(
        claims,
        "adopt_cohort_claim",
        lambda *a, **k: pytest.fail("Resolved claim ownership is immutable history"),
    )
    with monkeypatch.context() as patch:
        patch.setattr(RayTaskExecution, "from_db", classmethod(from_db))
        assert len(_recover(case, value, item)) == 1


def test_retirement_after_advisory_scan_refuses_cleanup_transfer(case, monkeypatch):
    value, before = _completed(case, missing=True)
    item, _, _ = _qualify_adopter(case, value)
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    original = recovery._candidates

    def retire(*args, **kwargs):
        result = original(*args, **kwargs)
        maintenance.request_worker_retirement(
            case.owner, expected_revision=0, actor="test", reason="race", authorized=True
        )
        return result

    monkeypatch.setattr(recovery, "_candidates", retire)
    assert _recover(case, value, item) == ()
    assert cleanup.cleanup_record(RayCohortJobCleanup.objects.get()) == before


@pytest.mark.parametrize("mode", ["heartbeat", "recreated", "revision"])
def test_owner_or_cleanup_change_after_candidate_scan_refuses_transfer(case, monkeypatch, mode):
    value, before = _completed(case)
    item, old_lease, old_owner = _qualify_adopter(case, value, stop_source=False)
    old_lease.last_heartbeat_at = case.now - timedelta(days=1)
    old_lease.save(update_fields=["last_heartbeat_at"])
    original = recovery._recover_one

    def change(identity, retained, *args):
        if mode == "heartbeat":
            old_lease.last_heartbeat_at = case.now
            old_lease.save(update_fields=["last_heartbeat_at"])
        elif mode == "recreated":
            old_lease.delete()
            _lease(old_owner.worker_id, started=case.now)
        else:
            retained = replace(retained, revision=retained.revision + 1)
        return original(identity, retained, *args)

    monkeypatch.setattr(recovery, "_recover_one", change)
    assert _recover(case, value, item) == ()
    assert cleanup.cleanup_record(RayCohortJobCleanup.objects.get()) == before


@pytest.mark.postgresql
@pytest.mark.parametrize("ledger_database", ["postgresql"], indirect=True)
def test_postgresql_cleanup_recovery_skips_locked_task_within_bounded_candidates(case):
    earlier, before = _completed(case)
    other = _extra_started(case, earlier, name="unlocked-cleanup")

    def terminal(task, decoded, *, retry_admitted):
        task.state = "SUCCEEDED"
        task.save(update_fields=["state"])
        return True

    _apply(other, case, callback=terminal)
    item, _, _ = _qualify_adopter(case, earlier)
    locked, release = Event(), Event()
    holder_pid = []

    def hold():
        connection.close()
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '2s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    holder_pid.append(cursor.fetchone()[0])
                RayTaskExecution.objects.select_for_update().get(pk=earlier.execution.pk)
                locked.set()
                if not release.wait(8):
                    raise TimeoutError("Cleanup recovery waited for an unrelated locked task")
        finally:
            connection.close()

    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        original_timeout = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('statement_timeout', '2s', false), pg_backend_pid()")
        adopter_pid = cursor.fetchone()[1]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(hold)
            try:
                assert locked.wait(5)
                assert holder_pid[0] != adopter_pid
                (adopted,) = _recover(case, earlier, item, limit=2)
                assert adopted.cleanup.cleanup_id == other.claim.claim_id
                assert not holder.done() and not release.is_set()
                assert (
                    cleanup.cleanup_record(RayCohortJobCleanup.objects.get(pk=before.cleanup_id))
                    == before
                )
            finally:
                release.set()
                holder.result(timeout=3)
    finally:
        release.set()
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, false)", [original_timeout])


@pytest.mark.parametrize("limit", [False, 0, 101, 1.5])
def test_cleanup_recovery_requires_finite_strict_limit(case, limit):
    value, _ = _completed(case)
    item, _, _ = _qualify_adopter(case, value)
    with pytest.raises(recovery.CohortCleanupRecoveryError):
        _recover(case, value, item, limit=limit)


def test_cleanup_recovery_refuses_existing_transaction(case):
    value, _ = _completed(case)
    item, _, _ = _qualify_adopter(case, value)
    with transaction.atomic(), pytest.raises(recovery.CohortCleanupRecoveryError):
        _recover(case, value, item)
