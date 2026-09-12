"""Real worker cancellation reservations with owned, resource-free callbacks."""

import hashlib
from types import SimpleNamespace

import pytest
from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from django_ray import maintenance
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.management.commands import django_ray_worker as worker
from django_ray.models import (
    RayCohortJobCleanup,
    RayTaskCohortClaim,
    RayTaskExecution,
    RayWorkerRetirement,
)
from django_ray.ray_job_protocol import _build_cohort_job_metadata
from django_ray.ray_job_request_storage import (
    _prepare_ray_job_request,
    _register_and_attach_ray_job_request,
)
from django_ray.runner import cohort_cancel_request as cancellation_request
from django_ray.runner import cohort_cancellation, cohort_dispatch
from django_ray.runner import cohort_job_execution_control as control
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.runner.ray_job import RayJobRunner
from django_ray.target import cohort_job_cleanup as cleanup
from django_ray.target.cohort_claim import CohortRunnerFamily
from django_ray.target.cohort_transport import encode_cohort_execution_result
from tests.integration.test_cohort_claim_storage import _claim
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_completion import _apply, _result, _started
from tests.integration.test_cohort_recovery import _arguments, _extra_started, _qualify_adopter
from tests.integration.test_cohort_worker import command as command
from tests.unit.test_cohort_execution_jobs import details

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _store_request(value, directory):
    endpoint = value.claim.facts.job_qualification.jobs_endpoint
    handle = RayJobRunner().cohort_submission_handle(value.execution, jobs_endpoint=endpoint)
    stored = _prepare_ray_job_request(
        value.prepared.request_json,
        {"INPUT_STORAGE_BACKEND": "filesystem", "INPUT_STORAGE_FILESYSTEM_PATH": str(directory)},
        supported_protocols=ExecutionProtocolRange(3, 3),
    )
    _register_and_attach_ray_job_request(
        stored,
        task_execution=value.execution,
        submission_handle=handle,
        supported_protocols=ExecutionProtocolRange(3, 3),
    )
    metadata = _build_cohort_job_metadata(value.prepared, stored.reference, stored.encoded_locator)
    metadata.update(
        cohort_jobs_endpoint=endpoint,
        cohort_submitted_runtime_env_digest="sha256:" + hashlib.sha256(b"{}").hexdigest(),
    )
    physical = details(
        SimpleNamespace(submission_id=handle.ray_job_id, metadata=metadata, stored=stored)
    )
    return handle, physical


@pytest.fixture
def jobs_control(case, command, monkeypatch, tmp_path):
    value = _started(case, "ray_job")
    handle, physical = _store_request(value, tmp_path)
    RayTaskExecution.objects.filter(pk=value.execution.pk).update(state="CANCELLING")
    value.execution.refresh_from_db()
    command.execution_mode = "ray"
    command._remember_cohort_value(value)
    command._cohort_job_handles[value.execution.pk] = handle
    command._cohort_controller = SimpleNamespace(
        invalidate=lambda: None,
        request_retirement=lambda: None,
        retirement_ready=True,
        poll_stopped_cleanup=lambda: None,
        lifecycle=None,
        adapter=None,
        connection=None,
        connection_ticket=None,
    )
    monkeypatch.setattr(cancellation_request, "datetime", worker.datetime)
    monkeypatch.setattr(cohort_cancellation, "_clock", lambda: case.now)
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    state = SimpleNamespace(
        command=command,
        value=value,
        physical=physical,
        threads=[],
        fetches=[],
        stops=[],
        starts=[],
        in_callback=False,
        monotonic=100.0,
    )
    monkeypatch.setattr(control, "time", SimpleNamespace(monotonic=lambda: state.monotonic))
    execute = CursorWrapper.execute

    def parent_sql(cursor, *args, **kwargs):
        assert state.in_callback is False, "Owned network callback accessed Django SQL"
        return execute(cursor, *args, **kwargs)

    monkeypatch.setattr(CursorWrapper, "execute", parent_sql)

    class OwnedThread:
        def __init__(self, *, target, daemon, name):
            assert daemon is True and name == "django-ray-cohort-job-control"
            self.target, self.alive = target, False
            state.threads.append(self)

        def start(self):
            assert not connection.in_atomic_block and connection.get_autocommit()
            row = RayTaskExecution.objects.get(pk=value.execution.pk)
            state.starts.append((row.state, row.cancellation_status))
            self.alive = True

        def is_alive(self):
            return self.alive

        def finish(self):
            state.in_callback = True
            try:
                self.target()
            finally:
                state.in_callback = self.alive = False

    def fetch(endpoint, submission_id):
        assert state.in_callback and not connection.in_atomic_block
        state.fetches.append((endpoint, submission_id))
        return state.physical

    def stop(expected):
        assert state.in_callback and not connection.in_atomic_block
        state.stops.append(expected)
        return True

    monkeypatch.setattr(control, "Thread", OwnedThread)
    monkeypatch.setattr(control, "_fetch_execution_job", fetch)
    monkeypatch.setattr(control, "_stop_execution_job", stop)
    yield state
    for thread in state.threads:
        if thread.alive:
            thread.finish()
    if command._cohort_job_control is not None:
        assert command._cohort_job_control.busy is False


def test_worker_commits_reservation_before_callback_and_ack_remains_nonterminal(jobs_control):
    state = jobs_control
    assert state.command._poll_cohort_jobs_control() == 1
    ticket = state.command._cohort_job_control_ticket
    assert state.starts == [("CANCELLING", "INDETERMINATE")]
    assert not state.fetches and not state.stops
    assert state.command._poll_cohort_jobs_control(start_new=False) == 0
    state.command.send_heartbeat()
    assert not state.fetches and not state.stops
    state.threads[0].finish()
    assert state.command._poll_cohort_jobs_control(start_new=False) == 1
    task = RayTaskExecution.objects.get(pk=state.value.execution.pk)
    assert (task.state, task.cancellation_status) == ("CANCELLING", "REQUESTED")
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"
    assert state.command.tasks_processed_count == 0
    assert state.command._cohort_job_control.begin_stop(ticket.expectation) is ticket
    assert len(state.stops) == len(state.fetches) == 1


def test_worker_exact_stopped_inspection_atomically_resolves_original_claim(jobs_control):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    state = jobs_control
    state.command._poll_cohort_jobs_control()
    state.threads[0].finish()
    state.command._poll_cohort_jobs_control(start_new=False)
    # The cursor wraps before a fresh inspection of the retained original Job.
    assert state.command._poll_cohort_jobs_control() == 0
    assert state.command._poll_cohort_jobs_control() == 1
    state.physical.status = JobStatus.STOPPED
    state.threads[1].finish()
    assert state.command._poll_cohort_jobs_control(start_new=False) == 2
    task = RayTaskExecution.objects.get(pk=state.value.execution.pk)
    claim = RayTaskCohortClaim.objects.get(pk=state.value.claim.claim_id)
    assert task.state == "CANCELLED" and claim.resolution_kind == "verified_cancelled"
    assert task.execution_generation == state.value.claim.facts.identity.execution_generation
    assert not state.command._cohort_job_handles and not state.command._cohort_dispatches
    assert state.command.tasks_processed_count == 1 and len(state.stops) == 1


@pytest.mark.parametrize("crossed", ["submission", "request", "endpoint"])
def test_worker_crossed_stopped_record_stays_held_without_remote_stop(jobs_control, crossed):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    state = jobs_control
    state.physical.status = JobStatus.STOPPED
    if crossed == "submission":
        state.physical.submission_id += "wrong"
    elif crossed == "request":
        state.physical.metadata["cohort_request_digest"] = "sha256:" + "f" * 64
    else:
        state.physical.metadata["cohort_jobs_endpoint"] = "http://other.invalid:8265"
    assert state.command._poll_cohort_jobs_control() == 1
    state.threads[0].finish()
    assert state.command._poll_cohort_jobs_control(start_new=False) == 1
    task = RayTaskExecution.objects.get(pk=state.value.execution.pk)
    claim = RayTaskCohortClaim.objects.get(pk=state.value.claim.claim_id)
    assert (task.state, task.cancellation_status) == ("CANCELLING", "INDETERMINATE")
    assert claim.disposition == "HELD" and claim.hold_application_invoked is None
    assert not state.stops and state.command.tasks_processed_count == 0


def test_worker_deadline_keeps_busy_slot_and_unknown_marker_without_replay(jobs_control):
    state = jobs_control
    state.command._poll_cohort_jobs_control()
    ticket = state.command._cohort_job_control_ticket
    state.monotonic += 13
    assert state.command._poll_cohort_jobs_control() == 1
    assert state.command._cohort_job_control.busy
    assert state.command._cohort_job_control_ticket is ticket
    assert state.command._poll_cohort_jobs_control() == 0
    task = RayTaskExecution.objects.get(pk=state.value.execution.pk)
    assert (task.state, task.cancellation_status) == ("CANCELLING", "INDETERMINATE")
    assert RayTaskCohortClaim.objects.get().disposition == "HELD"
    assert len(state.threads) == 1 and not state.fetches and not state.stops
    state.threads[0].finish()
    state.command._poll_cohort_jobs_control(start_new=False)
    assert state.command._cohort_job_control_ticket is None
    assert state.command._cohort_job_control.begin_stop(ticket.expectation) is ticket
    assert not state.command._cohort_job_control.busy


def test_worker_late_stop_ack_is_discarded_and_never_replayed(jobs_control, monkeypatch):
    state = jobs_control

    def late_stop(expected):
        assert state.in_callback
        state.stops.append(expected)
        state.monotonic += 13
        return True

    monkeypatch.setattr(control, "_stop_execution_job", late_stop)
    state.command._poll_cohort_jobs_control()
    ticket = state.command._cohort_job_control_ticket
    state.threads[0].finish()
    assert state.command._poll_cohort_jobs_control(start_new=False) == 1
    task = RayTaskExecution.objects.get(pk=state.value.execution.pk)
    assert (task.state, task.cancellation_status) == ("CANCELLING", "INDETERMINATE")
    assert RayTaskCohortClaim.objects.get().disposition == "HELD"
    assert state.command._cohort_job_control.begin_stop(ticket.expectation) is ticket
    assert len(state.stops) == len(state.fetches) == len(state.threads) == 1


def test_worker_capacity_none_releases_only_unstarted_reservation(jobs_control):
    state = jobs_control
    state.command._cohort_job_control = control.CohortJobExecutionController(max_pending=1)
    assert state.command._poll_cohort_jobs_control() == 0
    task = RayTaskExecution.objects.get(pk=state.value.execution.pk)
    assert (task.state, task.cancellation_status, task.cancellation_error) == (
        "CANCELLING",
        None,
        None,
    )
    assert not state.threads and not state.stops and not state.fetches
    assert RayTaskCohortClaim.objects.get().disposition == "OPEN"


def test_worker_begin_exception_retains_unknown_reservation(jobs_control, monkeypatch):
    state = jobs_control

    def failed(*_args, **_kwargs):
        raise RuntimeError("unknown callback startup")

    monkeypatch.setattr(control.CohortJobExecutionController, "begin_stop", failed)
    with pytest.raises(RuntimeError):
        state.command._poll_cohort_jobs_control()
    task = RayTaskExecution.objects.get(pk=state.value.execution.pk)
    assert (task.state, task.cancellation_status) == ("CANCELLING", "INDETERMINATE")
    assert not state.threads and not state.stops


def _close_with_independent_owned_inspection(state, case):
    """Obtain new source-owned evidence without consuming the retained callback."""
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    retained = cleanup.cleanup_record(RayCohortJobCleanup.objects.get())
    controller = control.CohortJobExecutionController()
    original_status = state.physical.status
    state.physical.status = JobStatus.SUCCEEDED
    ticket = controller.begin_inspection(retained.expectation)
    assert ticket is not None
    state.threads[-1].finish()
    result = controller.poll(ticket)
    assert result is not None and result.uncertainty is None and result.ticket is ticket
    closed = cleanup.close_cohort_job_cleanup(
        case.owner,
        retained,
        inspection=result.inspection,
        inspection_began_at=case.now,
        observed_at=case.now,
    )
    assert closed.state == "CLOSED" and controller.retire_execution(ticket.expectation.identity)
    state.physical.status = original_status


@pytest.mark.parametrize("status", ["RUNNING", "STOPPED"])
def test_old_callback_cannot_acknowledge_or_cancel_new_generation(
    jobs_control, case, tmp_path, status
):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    state = jobs_control
    state.command._poll_cohort_jobs_control()
    original_ticket = state.command._cohort_job_control_ticket
    original = state.command._cohort_value(case.task.pk)
    # Resolve through the real completion service, then create a valid new
    # generation while deliberately retaining the old process-local callback.
    # Its eventual payload must not derive authority from the reused task PK.
    _apply(original, case, _result(original, success=False))
    _close_with_independent_owned_inspection(state, case)
    assert state.threads[0].alive
    with transaction.atomic():
        RayTaskExecution.objects.filter(pk=case.task.pk).update(
            state="QUEUED",
            attempt_number=original.claim.facts.identity.attempt_number + 1,
            completion_data=None,
            cancellation_status=None,
            cancellation_error=None,
            ray_job_request_reference=None,
        )
    case.task.refresh_from_db()
    record = _claim(case, **_arguments(original))
    case.task.refresh_from_db()
    value = cohort_dispatch.prepare_claimed_cohort_dispatch(
        ClaimedCohortTask(case.task, record, None), transport="ray-job", now=case.now
    )
    handle = RayJobRunner().cohort_submission_handle(
        value.execution, jobs_endpoint=record.facts.job_qualification.jobs_endpoint
    )
    value = cohort_dispatch.mark_cohort_dispatch_started(value, jobs_handle=handle, now=case.now)
    handle, _new_physical = _store_request(value, tmp_path)
    reservation = cancellation_request.reserve_cohort_cancellation(
        value, request=True, now=case.now
    )
    assert reservation.should_request
    value = reservation.dispatch
    state.command._remember_cohort_value(value)
    state.command._cohort_job_handles[case.task.pk] = handle
    assert original_ticket.expectation.identity != value.claim.facts.identity
    state.physical.status = JobStatus(status)
    state.threads[0].finish()
    assert state.command._poll_cohort_jobs_control(start_new=False) == 1
    task = RayTaskExecution.objects.get(pk=case.task.pk)
    latest = RayTaskCohortClaim.objects.get(pk=value.claim.claim_id)
    assert task.execution_generation == value.claim.facts.identity.execution_generation
    assert (task.state, task.cancellation_status) == ("CANCELLING", "INDETERMINATE")
    assert latest.disposition == "OPEN" and not latest.resolution_kind
    assert state.command._cohort_value(task.pk) is value
    assert state.command.tasks_processed_count == 0
    assert all(item.identity == original_ticket.expectation.identity for item in state.stops)


def _complete_in_worker(state):
    serialized = encode_cohort_execution_result(_result(state.value))
    RayTaskExecution.objects.filter(pk=state.value.execution.pk).update(completion_data=serialized)
    assert (
        state.command._apply_cohort_result(
            state.value, serialized, provenance="durable_job_completion"
        )
        == 1
    )
    assert RayCohortJobCleanup.objects.get(pk=state.value.claim.claim_id).state == "OPEN"
    assert not state.command._cohort_dispatches and not state.command._cohort_job_handles


def test_precompletion_callback_cannot_close_new_durable_cleanup(jobs_control):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    state = jobs_control
    state.command._poll_cohort_jobs_control()
    old = state.command._cohort_job_control_ticket
    _complete_in_worker(state)
    assert state.command._cohort_job_control.busy
    state.physical.status = JobStatus.STOPPED
    state.threads[0].finish()
    state.command._poll_cohort_jobs_control(start_new=False)
    assert RayCohortJobCleanup.objects.get().state == "OPEN"
    assert state.command._cohort_job_control_ticket is None
    assert old.expectation.identity not in state.command._cohort_job_control_retired
    assert state.command._cohort_job_cleanup and not state.stops
    assert RayTaskExecution.objects.get().state == "SUCCEEDED"
    assert state.command.tasks_processed_count == 1


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "STOPPED"])
def test_fresh_terminal_cleanup_read_closes_only_original_obligation(jobs_control, status):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    state = jobs_control
    _complete_in_worker(state)
    task_before = RayTaskExecution.objects.values().get()
    claim_before = RayTaskCohortClaim.objects.values().get()
    assert state.command._poll_cohort_jobs_control() == 1
    ticket = state.command._cohort_job_control_ticket
    binding = state.command._cohort_job_cleanup_binding
    assert binding[0] is ticket and ticket.operation == "inspect"
    assert binding[1].cleanup_id == state.value.claim.claim_id
    state.physical.status = JobStatus(status)
    state.threads[-1].finish()
    assert state.command._poll_cohort_jobs_control(start_new=False) == 1
    row = RayCohortJobCleanup.objects.get()
    assert (row.state, row.terminal_status, row.native_job_id) == ("CLOSED", status, "01000000")
    assert not state.command._cohort_job_cleanup and not state.command._cohort_job_control.busy
    assert RayTaskExecution.objects.values().get() == task_before
    assert RayTaskCohortClaim.objects.values().get() == claim_before
    assert not state.stops and len(state.fetches) == 1
    assert state.command.tasks_processed_count == 1


def test_restart_adopts_cleanup_only_and_closes_with_fresh_owned_read(
    jobs_control, case, monkeypatch
):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    from django_ray.runner import cohort_cleanup_recovery

    state = jobs_control
    _complete_in_worker(state)
    item, _old_lease, old_owner = _qualify_adopter(case, state.value)
    task_before = RayTaskExecution.objects.values().get()
    claim_before = RayTaskCohortClaim.objects.values().get()
    restarted = worker.Command()
    restarted.worker_id, restarted.lease_identity, restarted.lease = (
        case.owner.worker_id,
        case.owner,
        case.lease,
    )
    restarted.execution_mode = "ray"
    restarted._cohort_controller = SimpleNamespace(
        family=CohortRunnerFamily.RAY_JOB,
        runtime=state.value.claim.facts.manager,
        qualifications=lambda: (item,),
    )
    monkeypatch.setattr(cohort_cleanup_recovery, "_clock", lambda: case.now)
    assert restarted._recover_cohort_jobs(concurrency=1) == 1
    assert not restarted._cohort_recovered and not restarted._cohort_dispatches
    assert not restarted._cohort_job_handles
    retained = restarted._cohort_job_cleanup[state.value.claim.claim_id]
    assert retained.owner == case.owner != old_owner
    assert retained.expectation.identity == state.value.claim.facts.identity
    assert restarted._poll_cohort_jobs_control() == 1
    state.physical.status = JobStatus.SUCCEEDED
    state.threads[-1].finish()
    assert restarted._poll_cohort_jobs_control(start_new=False) == 1
    assert RayCohortJobCleanup.objects.get().state == "CLOSED"
    assert not restarted._cohort_job_cleanup and not restarted._cohort_job_control.busy
    assert RayTaskExecution.objects.values().get() == task_before
    assert RayTaskCohortClaim.objects.values().get() == claim_before
    assert not state.stops and len(state.fetches) == 1
    assert restarted.tasks_processed_count == 0


def test_lost_cleanup_close_response_is_pruned_by_authoritative_reload(jobs_control, monkeypatch):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    state = jobs_control
    _complete_in_worker(state)
    task_before = RayTaskExecution.objects.values().get()
    claim_before = RayTaskCohortClaim.objects.values().get()
    assert state.command._poll_cohort_jobs_control() == 1
    ticket = state.command._cohort_job_control_ticket
    original_close = cleanup.close_cohort_job_cleanup
    closes = []

    def committed_but_lost(*args, **kwargs):
        result = original_close(*args, **kwargs)
        assert result.state == "CLOSED" and not connection.in_atomic_block
        closes.append(result)
        # Interrupt before the worker sees the close result or evicts its cache.
        raise RuntimeError("cleanup close response lost after commit")

    monkeypatch.setattr(cleanup, "close_cohort_job_cleanup", committed_but_lost)
    state.physical.status = JobStatus.SUCCEEDED
    state.threads[-1].finish()
    with pytest.raises(RuntimeError, match="close response lost"):
        state.command._poll_cohort_jobs_control(start_new=False)
    assert len(closes) == 1 and RayCohortJobCleanup.objects.get().state == "CLOSED"
    assert state.command._cohort_job_cleanup[closes[0].cleanup_id].state == "OPEN"
    assert len(state.fetches) == len(state.threads) == 1
    state.command._load_owned_cohort_job_cleanups()
    assert (
        not state.command._cohort_job_cleanup and state.command._owned_cohort_cleanup_count() == 0
    )
    assert state.command._poll_cohort_jobs_control() == 0
    assert len(state.fetches) == len(state.threads) == 1 and not state.stops
    assert state.command._cohort_job_control_ticket is None
    assert ticket.expectation.identity not in state.command._cohort_job_control_retired
    assert RayTaskExecution.objects.values().get() == task_before
    assert RayTaskCohortClaim.objects.values().get() == claim_before
    assert state.command.tasks_processed_count == 1


@pytest.mark.parametrize("inspectable_prefix", [False, True])
def test_cleanup_window_cannot_starve_later_owned_inspection(
    jobs_control, case, tmp_path, inspectable_prefix
):
    from ray.dashboard.modules.job.pydantic_models import JobStatus

    state = jobs_control
    original = state.value
    physical_by_claim = {original.claim.claim_id: state.physical}
    if not inspectable_prefix:
        RayTaskExecution.objects.filter(pk=original.execution.pk).update(
            ray_job_request_reference=None
        )
    _complete_in_worker(state)

    def terminal(task, _decoded, *, retry_admitted):
        task.state = "SUCCEEDED"
        task.save(update_fields=["state"])
        return True

    for index in range(99):
        earlier = _extra_started(case, original, name=f"earlier-cleanup-{index}")
        if inspectable_prefix:
            _handle, physical_by_claim[earlier.claim.claim_id] = _store_request(earlier, tmp_path)
        assert _apply(earlier, case, callback=terminal).applied
    assert RayCohortJobCleanup.objects.filter(state="OPEN", expectation_json=None).count() == (
        0 if inspectable_prefix else 100
    )
    later = _extra_started(case, original, name="inspectable-after-full-prefix")
    handle, state.physical = _store_request(later, tmp_path)
    physical_by_claim[later.claim.claim_id] = state.physical
    state.physical.status = JobStatus.SUCCEEDED
    state.value = later
    state.command._remember_cohort_value(later)
    state.command._cohort_job_handles[later.execution.pk] = handle
    _complete_in_worker(state)
    state.command._load_owned_cohort_job_cleanups()
    if inspectable_prefix:
        assert len(state.command._cohort_job_cleanup) == 100
        assert later.claim.claim_id not in state.command._cohort_job_cleanup
    else:
        assert set(state.command._cohort_job_cleanup) == {later.claim.claim_id}
    assert state.command._owned_cohort_cleanup_count() == 101
    assert cleanup.owned_job_cleanup_pending(case.owner)
    task_before = list(RayTaskExecution.objects.order_by("pk").values())
    claim_before = list(RayTaskCohortClaim.objects.order_by("pk").values())
    inspected = []
    for _index in range(101):
        state.command._load_owned_cohort_job_cleanups()
        assert state.command._poll_cohort_jobs_control() == 1
        retained = state.command._cohort_job_cleanup_binding[1]
        inspected.append(retained.cleanup_id)
        state.physical = physical_by_claim[retained.cleanup_id]
        state.threads[-1].finish()
        assert state.command._poll_cohort_jobs_control(start_new=False) == 1
        if retained.cleanup_id == later.claim.claim_id:
            break
        assert RayCohortJobCleanup.objects.get(pk=retained.cleanup_id).state == "OPEN"
        assert retained.cleanup_id not in state.command._cohort_job_cleanup
        assert state.command._owned_cohort_cleanup_count() == 101
    assert inspected[-1] == later.claim.claim_id
    assert len(inspected) == (101 if inspectable_prefix else 1)
    assert len(set(inspected)) == len(inspected)
    assert RayCohortJobCleanup.objects.get(pk=later.claim.claim_id).state == "CLOSED"
    assert state.command._owned_cohort_cleanup_count() == 100
    assert cleanup.owned_job_cleanup_pending(case.owner)
    assert later.claim.claim_id not in state.command._cohort_job_cleanup and not state.stops
    assert len(state.fetches) == len(state.threads) == len(inspected)
    assert list(RayTaskExecution.objects.order_by("pk").values()) == task_before
    assert list(RayTaskCohortClaim.objects.order_by("pk").values()) == claim_before


def test_worker_shutdown_with_owned_callback_cannot_finish_retirement(
    jobs_control, case, monkeypatch
):
    state = jobs_control
    state.command._poll_cohort_jobs_control()
    maintenance.request_worker_retirement(
        case.owner, expected_revision=0, actor="test", reason="retire", authorized=True
    )
    state.command._observe_cohort_retirement()
    state.command._finish_cohort_retirement_admission()
    assert state.command._cohort_retirement_finishing is False
    clock = [100.0]
    monkeypatch.setattr(
        worker,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            time=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    try:
        state.command.shutdown()
        assert state.command.shutdown_exit_code == 1
        assert state.command._cohort_job_control.busy
        assert RayWorkerRetirement.objects.latest("revision").state == "REQUESTED"
        assert len(state.threads) == 1 and not state.stops and not state.fetches
        assert RayTaskCohortClaim.objects.get().disposition == "HELD"
    finally:
        # Exact lease deletion leaves immutable retirement history orphaned for
        # the isolated test flush; it does not assert successful retirement.
        case.lease.delete()
