"""Resource-free ownership checks for one staged Jobs submission callback."""

import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from django_ray.runner import cohort_job_submission as submission
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runner.ray_job import RayJobRunner
from django_ray.target.attestation import RayRunnerFamily
from django_ray.target.cohort_claim import CohortClaimDisposition, CohortRunnerFamily
from django_ray.target.cohort_claim_storage import CohortClaimRecord
from django_ray.target.cohort_transport import prepare_cohort_execution
from tests.unit.test_cohort_claim import facts
from tests.unit.test_cohort_execution import _contract
from tests.unit.test_ray_core_runner import _task_execution


def _wait(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "owned callback did not reach its bounded test stage"
        time.sleep(0.005)


@pytest.fixture
def case(monkeypatch):
    parent = _contract(family=RayRunnerFamily.RAY_JOB)
    task = _task_execution(
        parent.identity.task_execution_pk,
        task_id=parent.identity.task_id,
        attempt_number=parent.identity.attempt_number,
        execution_generation=parent.identity.execution_generation,
        execution_protocol_version=3,
        claimed_by_worker="owned-worker",
        callable_path="testproject.tasks.add_numbers",
    )
    prepared = prepare_cohort_execution(task, contract=parent, transport="ray-job")
    claim = CohortClaimRecord(
        parent.cohort_evidence_id,
        replace(facts(family=CohortRunnerFamily.RAY_JOB), identity=parent.identity),
        parent.cohort_evidence_digest,
        3,
        CohortClaimDisposition.OPEN,
        WorkerLeaseIdentity("owned-worker", "host", 100, parent.claimed_at),
        prepared.request_digest,
        parent.claimed_at,
    )
    handle = RayJobRunner().cohort_submission_handle(
        task, jobs_endpoint="https://qualified.example.test:8265/prefix"
    )
    controller = submission.CohortJobSubmissionController(max_pending=2)
    value = SimpleNamespace(
        task=task,
        prepared=prepared,
        claim=claim,
        handle=handle,
        controller=controller,
        wall=datetime(2026, 1, 1, tzinfo=UTC),
        mono=10.0,
        prepare_gate=Event(),
        submit_gate=Event(),
        cleanup_gate=Event(),
        prepared_sources=[],
        attached=[],
        submitted=[],
        cleaned=[],
        calls=[],
        fail_prepare=False,
        fail_attach=False,
        fail_submit=False,
        fail_cleanup=False,
    )
    value.prepare_gate.set()
    value.submit_gate.set()
    value.cleanup_gate.set()
    monkeypatch.setattr(submission, "_clock", lambda: (value.wall, value.mono))

    @contextmanager
    def prepare(runner, source):
        value.calls.append(("prepare", get_ident()))
        value.prepare_gate.wait(2)
        if value.fail_prepare:
            raise ValueError("private preparation detail")
        value.prepared_sources.append(source)
        try:
            yield SimpleNamespace(source=source)
        finally:
            value.cleanup_gate.wait(2)
            if value.fail_cleanup:
                raise ValueError("private cleanup detail")
            value.cleaned.append(source)

    def attach(runner, staged, *, task_execution, expected_claim):
        value.calls.append(("attach", get_ident()))
        if value.fail_attach:
            raise ValueError("private attachment detail")
        assert task_execution is task
        assert expected_claim is claim
        value.attached.append(staged)

    def submit(runner, staged, *, check_pending=None):
        value.calls.append(("submit", get_ident()))
        assert check_pending is not None
        check_pending()
        value.submitted.append(staged)
        value.submit_gate.wait(2)
        if value.fail_submit:
            raise TimeoutError("private lost acknowledgement")
        check_pending()
        return staged.source.handle()

    monkeypatch.setattr(RayJobRunner, "_prepare_cohort_submission", prepare)
    monkeypatch.setattr(RayJobRunner, "_attach_cohort_submission", attach)
    monkeypatch.setattr(RayJobRunner, "_submit_prepared_cohort_submission", submit)
    yield value
    for operation in tuple(controller._operations.values()):
        controller.abort(operation.ticket)
    value.prepare_gate.set()
    value.submit_gate.set()
    value.cleanup_gate.set()
    for operation in tuple(controller._operations.values()):
        if operation.thread is not None:
            operation.thread.join(3)
            assert not operation.thread.is_alive()


def _begin(case, **kwargs):
    return case.controller.begin(
        case.task, prepared=case.prepared, handle=case.handle, claim=case.claim, **kwargs
    )


def _prepared(case, ticket):
    _wait(lambda: case.controller.poll(ticket).stage == "prepared")


def _finished(case, ticket):
    _wait(lambda: not case.controller.busy)
    return case.controller.poll(ticket)


def test_one_callback_waits_for_parent_attachment_and_snapshots_mutable_inputs(case):
    parent_thread = get_ident()
    ticket = _begin(case)
    _prepared(case, ticket)
    assert case.controller.busy
    assert case.submitted == case.attached == []
    assert _begin(case) is ticket
    source = case.prepared_sources[0]
    original_json = source.runtime.runtime_env_json
    case.task.runtime_env_json = "changed after capture"
    case.handle.ray_address = "https://unrelated.invalid"
    assert source.runtime.runtime_env_json == original_json
    assert source.jobs_endpoint == ticket.jobs_endpoint
    assert case.controller.authorize(ticket, task_execution=case.task)
    result = _finished(case, ticket)
    assert result.accepted and result.local_cleanup_complete
    assert result.uncertainty is None
    assert case.attached == case.submitted
    assert len(case.cleaned) == 1
    assert dict(case.calls)["attach"] == parent_thread
    assert dict(case.calls)["prepare"] == dict(case.calls)["submit"] != parent_thread
    assert case.controller.retire(ticket)


@pytest.mark.parametrize("phase", ["prepare", "submit"])
def test_deadline_retains_busy_until_real_exit_and_never_accepts_late_result(case, phase):
    if phase == "prepare":
        case.prepare_gate.clear()
    else:
        case.submit_gate.clear()
    ticket = _begin(case, timeout_seconds=5)
    if phase == "submit":
        _prepared(case, ticket)
        assert case.controller.authorize(ticket, task_execution=case.task)
        _wait(lambda: bool(case.submitted))
    else:
        _wait(lambda: bool(case.calls))
    case.mono += 6
    case.wall += timedelta(seconds=6)
    assert case.controller.poll(ticket).uncertainty == "submission_deadline"
    assert case.controller.busy and not case.controller.retire(ticket)
    assert not case.controller.authorize(ticket, task_execution=case.task)
    assert _begin(case, timeout_seconds=5) is ticket
    case.prepare_gate.set()
    case.submit_gate.set()
    result = _finished(case, ticket)
    assert not result.accepted
    assert result.local_cleanup_complete
    assert len(case.submitted) == (phase == "submit")


@pytest.mark.parametrize("fault", ["wall-regressed", "mono-regressed", "nan", "raises"])
def test_invalid_parent_clock_is_sticky_and_callback_can_still_exit(case, monkeypatch, fault):
    case.cleanup_gate.clear()
    ticket = _begin(case)
    _prepared(case, ticket)
    if fault == "wall-regressed":
        case.wall -= timedelta(seconds=1)
    elif fault == "mono-regressed":
        case.mono -= 1
    elif fault == "nan":
        case.mono = float("nan")
    else:

        def raising():
            raise ValueError("private clock detail")

        monkeypatch.setattr(submission, "_clock", raising)
    assert case.controller.poll(ticket).uncertainty is not None
    assert not case.controller.authorize(ticket, task_execution=case.task)
    assert case.controller.busy
    case.cleanup_gate.set()
    result = _finished(case, ticket)
    assert not result.accepted and result.local_cleanup_complete
    assert case.submitted == []


@pytest.mark.parametrize("failure", ["prepare", "attach", "submit", "cleanup"])
def test_stage_failure_never_becomes_success_or_replays(case, failure):
    setattr(case, "fail_" + failure, True)
    ticket = _begin(case)
    if failure != "prepare":
        _prepared(case, ticket)
        assert case.controller.authorize(ticket, task_execution=case.task) == (failure != "attach")
    result = _finished(case, ticket)
    assert result.uncertainty is not None and not result.accepted
    assert _begin(case) is ticket
    assert len(case.submitted) <= 1
    assert result.local_cleanup_complete == (failure not in {"prepare", "cleanup"})


def test_cleanup_exit_remains_owned_after_successful_sdk_acknowledgement(case):
    case.cleanup_gate.clear()
    ticket = _begin(case)
    _prepared(case, ticket)
    assert case.controller.authorize(ticket, task_execution=case.task)
    _wait(lambda: bool(case.submitted))
    assert case.controller.busy
    assert not case.controller.poll(ticket).accepted
    assert not case.controller.retire(ticket)
    case.controller.abort(ticket)
    case.cleanup_gate.set()
    assert not _finished(case, ticket).accepted


def test_forged_ticket_and_changed_request_cannot_reuse_owned_operation(case):
    ticket = _begin(case)
    _prepared(case, ticket)
    with pytest.raises(submission.CohortJobSubmissionError):
        case.controller.poll(replace(ticket))
    with pytest.raises(submission.CohortJobSubmissionError):
        case.controller.authorize(replace(ticket), task_execution=case.task)
    case.task.runtime_env_hash = "a" * 64
    with pytest.raises(submission.CohortJobSubmissionError):
        _begin(case)
    assert case.submitted == []


def test_thread_start_failure_retains_identity_before_start_and_never_replays(case, monkeypatch):
    starts = []

    class UnstartedThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            assert len(case.controller._operations) == 1
            assert len(case.controller._by_identity) == 1
            starts.append(True)
            raise RuntimeError("private startup detail")

        def is_alive(self):
            return False

        def join(self, timeout):
            pass

    monkeypatch.setattr(submission, "Thread", UnstartedThread)
    ticket = _begin(case)
    result = case.controller.poll(ticket)
    assert result.uncertainty == "callback_start_failed"
    assert not result.accepted and result.local_cleanup_complete
    assert _begin(case) is ticket
    assert starts == [True] and not case.submitted


def test_retained_uncleaned_callback_consumes_bounded_capacity_after_exit(case):
    case.controller._max_pending = 1
    case.fail_cleanup = True
    ticket = _begin(case)
    _prepared(case, ticket)
    case.controller.abort(ticket)
    result = _finished(case, ticket)
    assert not result.local_cleanup_complete
    assert not case.controller.retire(ticket)
    # A different valid identity must not displace the failed cleanup owner.
    identity = replace(case.prepared.identity, task_execution_pk=case.task.pk + 1)
    parent = replace(_contract(family=RayRunnerFamily.RAY_JOB), identity=identity)
    task = _task_execution(
        identity.task_execution_pk,
        task_id=identity.task_id,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        execution_protocol_version=3,
        claimed_by_worker="owned-worker",
        callable_path="testproject.tasks.add_numbers",
    )
    prepared = prepare_cohort_execution(task, contract=parent, transport="ray-job")
    claim = replace(
        case.claim,
        facts=replace(case.claim.facts, identity=identity),
        prepared_request_digest=prepared.request_digest,
    )
    handle = RayJobRunner().cohort_submission_handle(task, jobs_endpoint=ticket.jobs_endpoint)
    assert case.controller.begin(task, prepared=prepared, handle=handle, claim=claim) is None
    assert len(case.controller._operations) == 1
