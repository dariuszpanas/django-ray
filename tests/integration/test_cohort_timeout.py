"""Timeout requests preserve uncertainty until exact owned terminal evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from threading import Barrier

import pytest
from django.db import IntegrityError, connection, transaction

from django_ray import lifecycle
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.models import (
    RayTaskCohortClaim,
    RayTaskCohortTimeout,
    RayTaskExecution,
    TaskAttempt,
)
from django_ray.runner import cohort_cancel_request as cancel_request
from django_ray.runner import cohort_cancellation as cancellation
from django_ray.runner import cohort_timeout as timeout
from django_ray.target import cohort_claim_storage as claims
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import _apply, _result, _started
from tests.integration.test_cohort_completion import (
    isolated_completion_controls as isolated_completion_controls,
)
from tests.integration.test_cohort_recovery import _qualify_adopter, _recover

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _due(case, *, family="ray_core", seconds=10, elapsed=11):
    value = _started(case, family)
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(timeout_seconds=seconds)
    case.now += timedelta(seconds=elapsed)
    case.lease.last_heartbeat_at = case.now
    case.lease.save(update_fields=["last_heartbeat_at"])
    case.monkeypatch.setattr(timeout, "_clock", lambda: case.now)
    return value


def _request(case, value):
    return timeout.request_cohort_timeout(value, now=case.now)


@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_timeout_atomically_retains_snapshot_and_unknown_current_generation(case, family):
    value = _due(case, family=family)
    original = RayTaskCohortClaim.objects.get()
    result = _request(case, value)
    row = RayTaskCohortTimeout.objects.get()
    task = RayTaskExecution.objects.get()
    assert result.requested and result.dispatch.claim.disposition.value == "HELD"
    assert row.claim_id == value.claim.claim_id and row.requested_at == case.now
    assert row.started_at == task.started_at and row.timeout_seconds == 10
    assert row.deadline_at == row.started_at + timedelta(seconds=10)
    assert task.state == "CANCELLING" and task.cancellation_status is None
    assert (
        task.attempt_number == original.attempt_number
        and task.execution_generation == original.execution_generation
    )
    assert not TaskAttempt.objects.exists()
    current = RayTaskCohortClaim.objects.get()
    assert current.facts_json == original.facts_json and current.hold_application_invoked is None
    assert not _request(case, result.dispatch).requested


@pytest.mark.parametrize("seconds,elapsed", [(None, 100), (0, 100), (10, 10), (10, 9)])
def test_disabled_or_not_strictly_elapsed_timeout_is_unchanged(case, seconds, elapsed):
    value = _due(case, seconds=seconds, elapsed=elapsed)
    assert not _request(case, value).requested
    assert not RayTaskCohortTimeout.objects.exists()
    assert RayTaskExecution.objects.get().state == "RUNNING"
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "CANCELLING"),
        ("completion_data", "{}"),
        ("cancellation_status", "INDETERMINATE"),
        ("started_at", None),
    ],
)
def test_existing_operator_cancel_or_completion_is_not_reclassified(case, field, value):
    started = _due(case)
    RayTaskExecution.objects.filter(pk=started.execution.pk).update(**{field: value})
    assert not _request(case, started).requested
    assert not RayTaskCohortTimeout.objects.exists()


def test_timeout_preserves_original_held_observation(case):
    from django_ray.runner.cohort_dispatch import hold_cohort_dispatch
    from django_ray.target.cohort_claim import CohortHoldReason

    value = _due(case)
    value = hold_cohort_dispatch(value, reason=CohortHoldReason.DISPATCH_UNCERTAIN, now=case.now)
    before = RayTaskCohortClaim.objects.values().get()
    assert _request(case, value).requested
    after = RayTaskCohortClaim.objects.values().get()
    assert after == before


@pytest.mark.parametrize("ack", [True, False])
def test_remote_reservation_and_ack_never_make_timeout_terminal(case, ack):
    value = _request(case, _due(case, family="ray_job")).dispatch
    reserved = cancel_request.reserve_cohort_cancellation(value, now=case.now)
    assert reserved.should_request
    acknowledged = cancel_request.acknowledge_cohort_cancellation(
        reserved.dispatch, requested=ack, now=case.now
    )
    assert not cancel_request.reserve_cohort_cancellation(acknowledged, now=case.now).should_request
    assert RayTaskExecution.objects.get().state == "CANCELLING"
    assert RayTaskCohortClaim.objects.get().disposition == "HELD"
    assert not TaskAttempt.objects.exists()


@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_verified_timeout_cancellation_records_failed_attempt_without_retry(case, family):
    value = _request(case, _due(case, family=family)).dispatch
    snapshot = RayTaskCohortTimeout.objects.values().get()
    # Only the original immutable snapshot chooses timeout semantics now.
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(timeout_seconds=None)

    def fail(task):
        return lifecycle.record_failure(
            task,
            error_message="Task timed out after 10s",
            retry=False,
            expected_claimed_by_worker=value.claim.owner.worker_id,
            expected_attempt_number=value.claim.facts.identity.attempt_number,
            expected_execution_generation=value.claim.facts.identity.execution_generation,
            supported_protocols=ExecutionProtocolRange(3, 3),
            _allow_cancelling_completion=True,
        )

    applied = cancellation.apply_cohort_cancellation(
        value,
        evidence_kind="owned_core_terminal" if family == "ray_core" else "exact_jobs_stopped",
        apply_cancel=lambda task: pytest.fail("Timeout is a truthful failure"),
        apply_timeout=fail,
        now=case.now,
    )
    assert applied.applied and not applied.retry_admitted
    assert applied.dispatch.execution.state == "FAILED"
    assert TaskAttempt.objects.get().state == "FAILED"
    assert RayTaskCohortClaim.objects.get().resolution_kind == "verified_cancelled"
    assert RayTaskCohortTimeout.objects.values().get() == snapshot


@pytest.mark.parametrize("success", [True, False])
def test_authentic_late_completion_wins_over_timeout_and_never_retries(case, success):
    value = _request(case, _due(case, family="ray_job")).dispatch
    observed = []

    def complete(task, decoded, *, retry_admitted):
        observed.append(retry_admitted)
        task.state = "SUCCEEDED" if decoded.completion.success else "FAILED"
        task.save(update_fields=["state"])
        return True

    assert _apply(value, case, _result(value, success=success), callback=complete).applied
    assert observed == [False]
    assert RayTaskCohortClaim.objects.get().resolution_kind == "application_completed"
    assert RayTaskCohortTimeout.objects.exists()


def test_jobs_restart_preserves_timeout_intent_and_recovered_terminal_failure(case):
    value = _request(case, _due(case, family="ray_job")).dispatch
    item, _, _ = _qualify_adopter(case, value)
    (recovered,) = _recover(case, value, item)

    def failed(task):
        task.state = "FAILED"
        task.save(update_fields=["state"])
        return True

    result = cancellation.apply_cohort_cancellation(
        recovered,
        evidence_kind="exact_jobs_stopped",
        apply_cancel=lambda task: pytest.fail("Timeout intent survived restart"),
        apply_timeout=failed,
        now=case.now,
    )
    assert result.applied and result.dispatch.execution.state == "FAILED"
    assert RayTaskCohortTimeout.objects.get().claim_id == recovered.claim.claim_id


def test_recovered_jobs_owner_can_request_original_elapsed_timeout(case):
    value = _due(case, family="ray_job")
    item, _, _ = _qualify_adopter(case, value)
    (recovered,) = _recover(case, value, item)
    result = _request(case, recovered)
    assert result.requested and result.dispatch.claim.owner == case.owner
    assert result.dispatch.claim.facts == value.claim.facts
    row = RayTaskCohortTimeout.objects.get()
    assert row.claim_id == value.claim.claim_id and row.started_at == value.execution.started_at


@pytest.mark.parametrize("failure", ["clock", "date-overflow"])
def test_prewrite_clock_or_unrepresentable_deadline_refuses_without_intent(
    case, monkeypatch, failure
):
    value = _due(case)
    if failure == "clock":
        monkeypatch.setattr(timeout, "_clock", lambda: case.now - timedelta(seconds=1))
    else:
        RayTaskExecution.objects.filter(pk=value.execution.pk).update(
            started_at=datetime(9990, 1, 1, tzinfo=UTC), timeout_seconds=2147483647
        )
    with pytest.raises(timeout.CohortTimeoutError):
        _request(case, value)
    assert not RayTaskCohortTimeout.objects.exists()
    assert RayTaskExecution.objects.get().state == "RUNNING"
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


@pytest.mark.parametrize("failure", ["missing", "false", "exception", "wrong-state", "truthy"])
def test_timeout_callback_requires_truthful_failed_transition_or_rolls_back(case, failure):
    value = _request(case, _due(case)).dispatch

    def failed(task):
        task.state = "CANCELLED" if failure == "wrong-state" else "FAILED"
        task.save(update_fields=["state"])
        if failure == "exception":
            raise RuntimeError("callback failure")
        return {"false": False, "truthy": 1}.get(failure, True)

    with pytest.raises(RuntimeError):
        cancellation.apply_cohort_cancellation(
            value,
            evidence_kind="owned_core_terminal",
            apply_cancel=lambda task: pytest.fail("Timeout callback is required"),
            apply_timeout=None if failure == "missing" else failed,
            now=case.now,
        )
    assert RayTaskCohortClaim.objects.get().disposition == "HELD"
    assert RayTaskExecution.objects.get().state == "CANCELLING"


@pytest.mark.parametrize("failure", ["hold", "clock", "lease"])
def test_timeout_failure_after_insert_rolls_back_intent_state_and_hold(case, monkeypatch, failure):
    value = _due(case)
    original = claims.hold_cohort_claim

    def fail(*args, **kwargs):
        result = original(*args, **kwargs)
        if failure == "hold":
            raise RuntimeError("hold failed")
        case.now += timedelta(seconds=-1) if failure == "clock" else timedelta(days=1)
        return result

    monkeypatch.setattr(claims, "hold_cohort_claim", fail)
    with pytest.raises(RuntimeError):
        _request(case, value)
    assert not RayTaskCohortTimeout.objects.exists()
    assert RayTaskExecution.objects.get().state == "RUNNING"
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


@pytest.mark.parametrize("cross", ["owner", "revision", "generation"])
def test_timeout_requires_exact_owned_claim(case, cross):
    value = _due(case)
    record = value.claim
    if cross == "owner":
        record = replace(record, owner=replace(record.owner, pid=record.owner.pid + 1))
    elif cross == "revision":
        record = replace(record, revision=record.revision + 1)
    else:
        record = replace(
            record,
            facts=replace(
                record.facts,
                identity=replace(
                    record.facts.identity,
                    execution_generation=record.facts.identity.execution_generation + 1,
                ),
            ),
        )
    with pytest.raises(RuntimeError):
        _request(case, replace(value, claim=record))
    assert not RayTaskCohortTimeout.objects.exists()


@pytest.mark.parametrize(
    "invalid",
    ["seconds", "deadline", "started", "equal", "state", "completion", "lease", "overflow"],
)
def test_direct_sql_timeout_insert_requires_exact_elapsed_running_snapshot(case, invalid):
    value = _due(case)
    current = RayTaskExecution.objects.get()
    assert current.started_at is not None
    fields = {
        "claim_id": value.claim.claim_id,
        "started_at": current.started_at,
        "timeout_seconds": 10,
        "deadline_at": current.started_at + timedelta(seconds=10),
        "requested_at": case.now,
    }
    if invalid == "seconds":
        fields["timeout_seconds"] = 0
    elif invalid == "deadline":
        fields["deadline_at"] = current.started_at + timedelta(seconds=10, microseconds=1)
    elif invalid == "started":
        fields["started_at"] = current.started_at - timedelta(seconds=1)
    elif invalid == "equal":
        fields["requested_at"] = fields["deadline_at"]
    elif invalid == "state":
        RayTaskExecution.objects.filter(pk=current.pk).update(state="CANCELLING")
    elif invalid == "completion":
        RayTaskExecution.objects.filter(pk=current.pk).update(completion_data="")
    elif invalid == "overflow":
        fields.update(
            started_at=datetime(9990, 1, 1, tzinfo=UTC),
            timeout_seconds=2147483647,
            deadline_at=datetime(9995, 1, 1, tzinfo=UTC),
            requested_at=datetime(9999, 1, 1, tzinfo=UTC),
        )
        RayTaskExecution.objects.filter(pk=current.pk).update(
            started_at=fields["started_at"], timeout_seconds=2147483647
        )
    else:
        case.lease.is_active = False
        case.lease.save(update_fields=["is_active"])
    with transaction.atomic(), pytest.raises(IntegrityError):
        RayTaskCohortTimeout.objects.create(**fields)


def test_timeout_sql_preserves_fractional_started_time_across_second_boundary(case):
    value = _due(case)
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(
        started_at=value.execution.started_at.replace(microsecond=999999)
    )
    assert _request(case, value).requested
    row = RayTaskCohortTimeout.objects.get()
    assert row.started_at.microsecond == row.deadline_at.microsecond == 999999


def test_timeout_is_immutable_while_unresolved_and_empty_reverse_refuses_history(case):
    value = _request(case, _due(case)).dispatch
    with transaction.atomic(), pytest.raises(IntegrityError):
        RayTaskCohortTimeout.objects.filter(claim_id=value.claim.claim_id).update(
            timeout_seconds=11
        )
    with transaction.atomic(), pytest.raises(IntegrityError):
        RayTaskCohortTimeout.objects.filter(claim_id=value.claim.claim_id).delete()
    migration = import_module("django_ray.migrations.0034_cohort_timeouts")
    from django.apps import apps

    with connection.schema_editor(atomic=False) as editor:
        with transaction.atomic(), pytest.raises(RuntimeError, match="retained history"):
            migration._remove(apps, editor)


def test_timeout_service_refuses_outer_transaction(case):
    value = _due(case)
    with transaction.atomic(), pytest.raises(timeout.CohortTimeoutError):
        _request(case, value)


def test_sync_has_no_owned_remote_timeout_cancellation(case):
    value = _due(case, family="sync")
    with pytest.raises(timeout.CohortTimeoutError):
        _request(case, value)


@pytest.mark.parametrize("mode", ["orm", "raw"])
def test_resolved_timeout_history_can_be_purged_with_original_claim(case, mode):
    value = _request(case, _due(case)).dispatch

    def failed(task):
        task.state = "FAILED"
        task.save(update_fields=["state"])
        return True

    assert cancellation.apply_cohort_cancellation(
        value,
        evidence_kind="owned_core_terminal",
        apply_cancel=lambda task: pytest.fail("Timeout expected"),
        apply_timeout=failed,
        now=case.now,
    ).applied
    if mode == "orm":
        RayTaskCohortClaim.objects.filter(pk=value.claim.claim_id).delete()
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM django_ray_raytaskcohortclaim WHERE id=%s", [value.claim.claim_id]
            )
    assert not RayTaskCohortTimeout.objects.exists()


@pytest.mark.postgresql
@pytest.mark.parametrize("ledger_database", ["postgresql"], indirect=True)
def test_postgresql_concurrent_timeout_requests_keep_one_original_intent(case):
    value = _due(case)
    ready = Barrier(2)

    def request():
        connection.close()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '8s'")
            ready.wait(timeout=5)
            try:
                return "requested" if _request(case, value).requested else "not-requested"
            except claims.CohortClaimStorageError as error:
                if error.reason is claims.CohortClaimStorageReason.CLAIM_CHANGED:
                    return "stale-claim"
                raise
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(request) for _ in range(2)]
        assert sorted(result.result(timeout=10) for result in results) == [
            "requested",
            "stale-claim",
        ]
    assert RayTaskCohortTimeout.objects.count() == 1
    assert RayTaskExecution.objects.get().state == "CANCELLING"
    assert RayTaskCohortClaim.objects.get().disposition == "HELD"
