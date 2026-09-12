"""Worker control uses real persisted Jobs claims without any native execution."""

import json
from types import SimpleNamespace

import pytest

from django_ray.management.commands import django_ray_worker as worker
from django_ray.models import RayTaskCohortClaim, RayTaskExecution
from django_ray.runner import cohort_cancel_request as cancellation_request
from django_ray.runner import cohort_cancellation, cohort_dispatch
from django_ray.runner.ray_core import RayCoreCohortCancellationStatus
from django_ray.runner.ray_job import RayJobRunner
from django_ray.target.cohort_claim import CohortHoldReason, CohortRunnerFamily
from django_ray.target.cohort_transport import encode_cohort_execution_result
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import _result, _started
from tests.integration.test_cohort_recovery import _extra_started, _qualify_adopter
from tests.integration.test_cohort_worker import command as command

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _adopting_worker(case, command, value):
    item, _, _ = _qualify_adopter(case, value)
    command.worker_id = case.owner.worker_id
    command.lease_identity = case.owner
    command.lease = case.lease
    command._cohort_controller = SimpleNamespace(
        family=CohortRunnerFamily.RAY_JOB,
        runtime=value.claim.facts.manager,
        qualifications=lambda: (item,),
    )
    return item


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("cancelling", [False, True])
def test_worker_recovers_and_completes_original_jobs_without_preparation_or_submission(
    case,
    command,
    monkeypatch,
    held,
    cancelling,
):
    value = _started(case, "ray_job")
    result = encode_cohort_execution_result(_result(value))
    if held:
        value = cohort_dispatch.hold_cohort_dispatch(
            value,
            reason=CohortHoldReason.DISPATCH_UNCERTAIN,
            now=case.now,
        )
    before = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    _adopting_worker(case, command, value)
    if cancelling:
        RayTaskExecution.objects.filter(pk=value.execution.pk).update(state="CANCELLING")
    monkeypatch.setattr(
        cohort_dispatch,
        "prepare_claimed_cohort_dispatch",
        lambda *a, **k: pytest.fail("Restart cannot prepare an old request"),
    )
    monkeypatch.setattr(
        RayJobRunner,
        "submit_cohort_task",
        lambda *a, **k: pytest.fail("Restart cannot resubmit"),
    )
    assert command._recover_cohort_jobs(4) == 1
    assert not command._cohort_dispatches
    assert command._cohort_job_handles[value.execution.pk].ray_job_id == value.execution.ray_job_id
    assert command._poll_cohort_completions() == 0
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(completion_data=result)
    assert command._poll_cohort_completions() == 1
    task = RayTaskExecution.objects.get(pk=value.execution.pk)
    after = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    assert task.state == "SUCCEEDED" and json.loads(task.result_data) == 5
    assert task.execution_generation == value.claim.facts.identity.execution_generation
    assert (after.facts_json, after.held_at, after.hold_evidence_digest) == (
        before.facts_json,
        before.held_at,
        before.hold_evidence_digest,
    )
    assert not command._cohort_recovered and not command._cohort_job_handles
    assert command.tasks_processed_count == 1


def test_worker_adopted_cancelling_failure_is_terminal_and_never_retried(case, command):
    value = _started(case, "ray_job")
    result = encode_cohort_execution_result(_result(value, success=False))
    _adopting_worker(case, command, value)
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(state="CANCELLING")
    assert command._recover_cohort_jobs(4) == 1
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(completion_data=result)
    assert command._poll_cohort_completions() == 1
    task = RayTaskExecution.objects.get(pk=value.execution.pk)
    assert (task.state, task.attempt_number, task.error_message) == (
        "FAILED",
        1,
        "application failed",
    )
    assert RayTaskCohortClaim.objects.get().resolution_kind == "application_completed"


@pytest.mark.parametrize("acknowledged", [False, True])
def test_recovered_cancel_request_remains_one_shot_and_ack_never_finishes_task(
    case,
    command,
    acknowledged,
):
    value = _started(case, "ray_job")
    reservation = cancellation_request.reserve_cohort_cancellation(
        value, request=True, now=case.now
    )
    assert reservation.should_request
    value = cancellation_request.acknowledge_cohort_cancellation(
        reservation.dispatch,
        requested=acknowledged,
        now=case.now,
    )
    _adopting_worker(case, command, value)
    assert command._recover_cohort_jobs(4) == 1
    recovered = command._cohort_value(value.execution.pk)
    reservation = cancellation_request.reserve_cohort_cancellation(
        recovered, request=True, now=case.now
    )
    assert not reservation.should_request
    assert command._poll_cohort_completions() == 0
    task = RayTaskExecution.objects.get(pk=value.execution.pk)
    assert task.state == "CANCELLING"
    assert task.cancellation_status == ("REQUESTED" if acknowledged else "INDETERMINATE")
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
    assert command.tasks_processed_count == 0


def test_worker_recovery_respects_actual_owned_capacity(case, command):
    value = _started(case, "ray_job")
    _adopting_worker(case, command, value)
    assert command._recover_cohort_jobs(1) == 1
    assert command._recover_cohort_jobs(1) == 0
    assert len(command._cohort_recovered) == 1


def test_completion_cursor_reaches_later_result_past_one_hundred_held_rows(case, command):
    first = _started(case, "ray_job")
    values = [first]
    for index in range(100):
        values.append(_extra_started(case, first, name=f"cursor-{index}"))
    for value in values:
        command._remember_cohort_value(value)
        command._cohort_job_handles[value.execution.pk] = RayJobRunner().cohort_submission_handle(
            value.execution,
            jobs_endpoint=value.claim.facts.job_qualification.jobs_endpoint,
        )
    RayTaskExecution.objects.filter(pk__in=[value.execution.pk for value in values[:-1]]).update(
        completion_data="malformed-owned-result",
    )
    last = values[-1]
    RayTaskExecution.objects.filter(pk=last.execution.pk).update(
        completion_data=encode_cohort_execution_result(_result(last)),
    )
    assert command._poll_cohort_completions() == 0
    assert RayTaskCohortClaim.objects.filter(disposition="HELD").count() == 100
    assert command._poll_cohort_completions() == 1
    assert RayTaskExecution.objects.get(pk=last.execution.pk).state == "SUCCEEDED"
    assert command.tasks_processed_count == 1
    assert command._poll_cohort_completions() == 0
    assert command._cohort_completion_cursor == 0


@pytest.mark.parametrize("acknowledged", [False, True])
def test_worker_core_cancel_ack_is_not_terminal_and_never_replays(
    case, command, monkeypatch, acknowledged
):
    value = _started(case, "ray_core")
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(state="CANCELLING")
    handle = SimpleNamespace(task_pk=value.execution.pk)
    ticket = object()
    calls = []
    command._remember_cohort_value(value)
    command._cohort_core_handles[value.execution.pk] = handle
    monkeypatch.setattr(cancellation_request, "datetime", worker.datetime)

    def begin(observed):
        assert observed is handle
        task = RayTaskExecution.objects.get(pk=value.execution.pk)
        assert task.cancellation_status == "INDETERMINATE"
        assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
        calls.append(observed)
        return ticket

    def poll(observed):
        assert observed is ticket
        return SimpleNamespace(
            status=RayCoreCohortCancellationStatus.REQUESTED
            if acknowledged
            else RayCoreCohortCancellationStatus.UNCERTAIN,
        )

    command.ray_core_runner = SimpleNamespace(
        cohort_cancellation_busy=False,
        begin_cohort_cancellation=begin,
        poll_cohort_cancellation=poll,
    )
    assert command._poll_cohort_cancellations() == 1
    assert command._poll_cohort_cancellations() == 1
    assert command._poll_cohort_cancellations() == 0
    assert calls == [handle]
    task = RayTaskExecution.objects.get(pk=value.execution.pk)
    assert (task.state, task.cancellation_status) == (
        "CANCELLING",
        "REQUESTED" if acknowledged else "INDETERMINATE",
    )
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
    assert command.tasks_processed_count == 0


def test_worker_exact_core_terminal_cancel_uses_real_atomic_service(case, command, monkeypatch):
    value = _started(case, "ray_core")
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(state="CANCELLING")
    handle = SimpleNamespace(task_pk=value.execution.pk)
    retired = []
    command._remember_cohort_value(value)
    command._cohort_core_handles[value.execution.pk] = handle
    monkeypatch.setattr(cohort_cancellation, "_clock", lambda: case.now)
    command.ray_core_runner = SimpleNamespace(
        poll_cohort_completed=lambda handles: [
            SimpleNamespace(
                handle=handle,
                terminal_cancelled=True,
                result=None,
            )
        ],
        retire_pending_handle=retired.append,
    )
    assert command._poll_cohort_completions() == 1
    assert RayTaskExecution.objects.get().state == "CANCELLED"
    assert RayTaskCohortClaim.objects.get().resolution_kind == "verified_cancelled"
    assert retired == [handle]
    assert command.tasks_processed_count == 1
    assert not command._cohort_dispatches and not command._cohort_core_handles
