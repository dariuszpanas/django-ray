"""Physical cancellation proof never comes from an unbound terminal status."""

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from django.db import connections
from ray.dashboard.modules import dashboard_sdk
from ray.dashboard.modules.job.pydantic_models import JobStatus, JobType

from django_ray.runner import cohort_job_execution_control as control
from tests.unit.test_cohort_execution_jobs import details  # noqa: F401
from tests.unit.test_cohort_execution_jobs import job_request as job_request
from tests.unit.test_cohort_job_http import transport as transport


@pytest.fixture(autouse=True)
def outside_database_transaction(monkeypatch):
    # Other modules may leave an initialized connection behind. Keep this pure
    # control test's transaction state explicit without opening a real database.
    connection = SimpleNamespace(
        in_atomic_block=False, connection=object(), get_autocommit=lambda: True
    )
    monkeypatch.setattr(connections, "all", lambda: [connection])


@pytest.fixture
def inspection(job_request, monkeypatch):
    job = job_request
    expected = control.CohortJobExecutionExpectation(
        job.prepared.identity,
        job.endpoint,
        job.submission_id,
        job.prepared.request_digest,
        job.prepared.contract_digest,
        job.stored.reference,
        job.stored.digest,
        job.stored.size_bytes,
    )
    state = SimpleNamespace(expected=expected, details=details(job), calls=[], job=job)

    def fetch(endpoint, submission):
        assert not connections["default"].in_atomic_block
        state.calls.append((endpoint, submission))
        return state.details

    monkeypatch.setattr(control, "_fetch_execution_job", fetch)
    return state


@pytest.mark.parametrize("status", list(JobStatus))
def test_only_bound_stopped_submission_is_outer_driver_terminal(inspection, status):
    inspection.details.status = status
    result = control.inspect_cohort_job_execution(inspection.expected)
    assert result.expectation is inspection.expected
    assert result.status == status.value
    assert result.outer_driver_terminal is (status is JobStatus.STOPPED)
    assert result.native_job_id == "01000000"
    assert inspection.calls == [
        (inspection.expected.jobs_endpoint, inspection.expected.submission_id)
    ]


@pytest.mark.parametrize(
    "change",
    [
        "type",
        "submission",
        "command",
        "locator",
        "request",
        "contract",
        "endpoint",
        "reference",
        "coordination",
        "protocol",
        "runtime_env",
        "extra",
        "missing",
        "driver",
        "native_type",
        "partial_driver",
    ],
)
def test_crossed_physical_record_never_authorizes_terminal_cleanup(inspection, change):
    record = inspection.details
    record.status = JobStatus.STOPPED
    if change == "type":
        record.type = JobType.DRIVER
    elif change == "submission":
        record.submission_id = "raysubmit_django_ray_rq2_" + "f" * 64
    elif change == "command":
        record.entrypoint = "python application.py"
    elif change == "locator":
        record.entrypoint += " --extra"
    elif change == "request":
        record.metadata["cohort_request_digest"] = "sha256:" + "f" * 64
    elif change == "contract":
        record.metadata["cohort_contract_digest"] = "sha256:" + "f" * 64
    elif change == "endpoint":
        record.metadata["cohort_jobs_endpoint"] = "http://other.example.test:8265"
    elif change == "reference":
        record.metadata["django_ray_request_reference_sha256"] = "f" * 64
    elif change == "coordination":
        record.metadata["django_ray_coordination_sha256"] = "f" * 64
    elif change == "protocol":
        record.metadata["django_ray_execution_protocol_version"] = "1"
    elif change == "runtime_env":
        record.runtime_env = {"env_vars": {"PRIVATE": "do-not-print"}}
    elif change == "extra":
        record.metadata["job_submission_id"] = record.submission_id
    elif change == "missing":
        del record.metadata["django_ray_request_size_bytes"]
    elif change == "driver":
        record.driver_info.id = "02000000"
    elif change == "native_type":
        record.job_id = "not-native"
    else:
        record.job_id = None
    with pytest.raises(control.CohortJobExecutionControlError) as error:
        control.inspect_cohort_job_execution(inspection.expected)
    assert str(error.value) == "Cohort Job control inspection unavailable"
    assert len(inspection.calls) == 1


def test_stopped_before_driver_creation_preserves_absent_native_identity(inspection):
    inspection.details.status = JobStatus.STOPPED
    inspection.details.job_id = None
    inspection.details.driver_info = None
    result = control.inspect_cohort_job_execution(inspection.expected)
    assert result.outer_driver_terminal and result.native_job_id is None


@pytest.mark.parametrize(
    "field",
    [
        "submission_id",
        "request_reference",
        "raw_request_sha256",
        "request_size_bytes",
        "jobs_endpoint",
    ],
)
def test_invalid_independently_held_expectation_refuses_before_http(inspection, field):
    value = 0 if field == "request_size_bytes" else "wrong"
    expected = replace(inspection.expected, **{field: value})
    with pytest.raises(control.CohortJobExecutionControlError):
        control.inspect_cohort_job_execution(expected)
    assert not inspection.calls


def test_network_refusal_is_fixed_and_does_not_retry(inspection, monkeypatch):
    calls = []

    def fail(*args):
        calls.append(args)
        raise ConnectionError("private response detail")

    monkeypatch.setattr(control, "_fetch_execution_job", fail)
    with pytest.raises(control.CohortJobExecutionControlError) as error:
        control.inspect_cohort_job_execution(inspection.expected)
    assert len(calls) == 1 and "private" not in str(error.value)


@pytest.mark.parametrize("manual", [False, True])
def test_database_transaction_refuses_before_remote_read(inspection, monkeypatch, manual):
    connection = SimpleNamespace(
        in_atomic_block=not manual, connection=object(), get_autocommit=lambda: False
    )
    monkeypatch.setattr(connections, "all", lambda: [connection])
    with pytest.raises(control.CohortJobExecutionControlError):
        control.inspect_cohort_job_execution(inspection.expected)
    assert not inspection.calls


def test_inspection_does_not_read_input_or_plan_runtime_environment(inspection, monkeypatch):
    from django_ray import ray_job_request_storage
    from django_ray.models import TaskInputPayload
    from django_ray.workflow import plans

    def forbidden(*_args, **_kwargs):
        pytest.fail("Inspection accessed task input or RuntimeEnv")

    monkeypatch.setattr(ray_job_request_storage, "_load_ray_job_request", forbidden)
    monkeypatch.setattr(plans, "runtime_env_plan_identity", forbidden)
    monkeypatch.setattr(TaskInputPayload.objects, "get", forbidden)
    inspection.details.status = JobStatus.STOPPED
    assert control.inspect_cohort_job_execution(inspection.expected).outer_driver_terminal


def test_recovered_carrier_uses_original_reference_without_request_loading(inspection, monkeypatch):
    from django_ray.runner import cohort_recovery
    from django_ray.runner.base import SubmissionHandle

    expected = inspection.expected
    record = SimpleNamespace(facts=SimpleNamespace(identity=expected.identity))
    recovered = cohort_recovery.RecoveredCohortJobCompletion(
        None,
        record,
        expected.request_digest,
        expected.contract_digest,
        SubmissionHandle(
            expected.submission_id, expected.jobs_endpoint, inspection.job.contract.claimed_at
        ),
        expected.request_reference,
    )
    calls = []
    monkeypatch.setattr(
        cohort_recovery,
        "validate_recovered_cohort_job_completion",
        lambda value: calls.append(value),
    )
    assert control.build_cohort_job_execution_expectation(recovered) == expected
    assert calls == [recovered]
    with pytest.raises(control.CohortJobExecutionControlError):
        control.build_cohort_job_execution_expectation(replace(recovered, request_reference=None))
    assert not inspection.calls


def test_unrecognized_carrier_cannot_create_cancellation_authority(inspection):
    with pytest.raises(control.CohortJobExecutionControlError):
        control.build_cohort_job_execution_expectation(inspection.expected)


@pytest.fixture
def retained_dispatch(inspection, monkeypatch):
    from django_ray.runner.cohort_dispatch import PreparedCohortDispatch
    from django_ray.target import cohort_claim

    job = inspection.job
    claim, request = job.contract, job.stored.request
    facts = SimpleNamespace(
        identity=claim.identity,
        binding_id=claim.target_binding_id,
        claimed_at=claim.claimed_at,
        binding=SimpleNamespace(
            runner_family=cohort_claim.CohortRunnerFamily.RAY_JOB,
            package_version=claim.expected_django_ray_version,
        ),
        job_qualification=SimpleNamespace(jobs_endpoint=job.endpoint),
    )
    record = SimpleNamespace(
        facts=facts,
        facts_digest=claim.cohort_evidence_digest,
        claim_id=claim.cohort_evidence_id,
        disposition=cohort_claim.CohortClaimDisposition.OPEN,
        dispatched_at=claim.claimed_at,
        owner=SimpleNamespace(worker_id="owner"),
        prepared_request_digest=job.prepared.request_digest,
    )
    task = SimpleNamespace(
        pk=request.identity.task_execution_pk,
        task_id=request.identity.task_id,
        attempt_number=request.identity.attempt_number,
        execution_generation=request.identity.execution_generation,
        execution_protocol_version=3,
        callable_path=request.callable_path,
        args_json=request.serialized_args,
        kwargs_json=request.serialized_kwargs,
        input_reference=request.input_reference,
        state="CANCELLING",
        claimed_by_worker="owner",
        ray_address=job.endpoint,
        ray_job_id=job.submission_id,
        ray_job_request_reference=job.stored.reference,
    )
    # The ledger's canonical facts codec is tested separately. This fixture
    # isolates control assembly and leaves actual request/contract decoding on.
    monkeypatch.setattr(
        cohort_claim, "cohort_claim_facts_digest", lambda _facts: claim.cohort_evidence_digest
    )
    return PreparedCohortDispatch(task, record, job.prepared)


def test_retained_dispatch_uses_original_cancelling_attempt(inspection, retained_dispatch):
    assert control.build_cohort_job_execution_expectation(retained_dispatch) == inspection.expected
    assert not inspection.calls


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "owner",
        "state",
        "endpoint",
        "reference",
        "digest",
        "claim_id",
        "claim_facts",
        "prepared_digest",
        "dispatch_marker",
    ],
)
def test_crossed_retained_dispatch_cannot_create_physical_stop_expectation(
    inspection, retained_dispatch, change
):
    task, record = retained_dispatch.execution, retained_dispatch.claim
    if change == "attempt":
        task.attempt_number += 1
    elif change == "owner":
        task.claimed_by_worker = "new-owner"
    elif change == "state":
        task.state = "QUEUED"
    elif change == "endpoint":
        task.ray_address = "http://other.example.test:8265"
    elif change == "reference":
        task.ray_job_request_reference = None
    elif change == "digest":
        retained_dispatch = replace(
            retained_dispatch,
            prepared=replace(retained_dispatch.prepared, contract_digest="sha256:" + "f" * 64),
        )
    elif change == "claim_id":
        record.claim_id += 1
    elif change == "claim_facts":
        record.facts_digest = "sha256:" + "f" * 64
    elif change == "prepared_digest":
        record.prepared_request_digest = "sha256:" + "f" * 64
    else:
        record.dispatched_at = None
    with pytest.raises(control.CohortJobExecutionControlError):
        control.build_cohort_job_execution_expectation(retained_dispatch)
    assert not inspection.calls


@pytest.fixture
def async_control(inspection, monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(control, "time", SimpleNamespace(monotonic=lambda: clock.value))
    threads, stops = [], []

    class Thread:
        def __init__(self, *, target, daemon, name):
            assert daemon is True and name == "django-ray-cohort-job-control"
            self.target, self.alive = target, False
            threads.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def finish(self):
            try:
                self.target()
            finally:
                self.alive = False

    monkeypatch.setattr(control, "Thread", Thread)
    monkeypatch.setattr(
        control, "_stop_execution_job", lambda expected: stops.append(expected) or True
    )
    manager = control.CohortJobExecutionController()
    state = SimpleNamespace(
        manager=manager,
        clock=clock,
        threads=threads,
        stops=stops,
        expected=inspection.expected,
        inspection=inspection,
    )
    yield state
    for thread in threads:
        if thread.is_alive():
            thread.finish()
    assert not manager.busy


def test_async_stop_validates_original_record_then_requests_once(async_control):
    state = async_control
    ticket = state.manager.begin_stop(state.expected)
    assert not state.inspection.calls and not state.stops
    assert state.manager.poll(ticket) is None and state.manager.busy
    assert state.manager.begin_stop(replace(state.expected)) is ticket
    with pytest.raises(control.CohortJobExecutionControlError):
        state.manager.poll(replace(ticket))
    state.threads[0].finish()
    result = state.manager.poll(ticket)
    assert result.ticket is ticket and result.stop_requested is True
    assert result.inspection.expectation is state.expected and result.uncertainty is None
    assert state.stops == [state.expected] and len(state.inspection.calls) == 1
    assert state.manager.begin_stop(state.expected) is ticket and len(state.threads) == 1
    with pytest.raises(control.CohortJobExecutionControlError):
        state.manager.retire_inspection(ticket)


def test_async_crossed_inspection_never_reaches_stop(async_control):
    state = async_control
    state.inspection.details.metadata["cohort_request_digest"] = "sha256:" + "f" * 64
    ticket = state.manager.begin_stop(state.expected)
    state.threads[0].finish()
    result = state.manager.poll(ticket)
    assert result.uncertainty == "control_unconfirmed" and result.inspection is None
    assert not state.stops


@pytest.mark.parametrize("status", [JobStatus.STOPPED, JobStatus.SUCCEEDED, JobStatus.FAILED])
def test_existing_terminal_record_never_issues_another_stop(async_control, status):
    state = async_control
    state.inspection.details.status = status
    ticket = state.manager.begin_stop(state.expected)
    state.threads[0].finish()
    result = state.manager.poll(ticket)
    assert result.inspection.outer_driver_terminal is (status is JobStatus.STOPPED)
    assert result.stop_requested is None and not state.stops


def test_late_stop_response_stays_unknown_and_shutdown_retains_callback(async_control, monkeypatch):
    state = async_control
    ticket = state.manager.begin_stop(state.expected, timeout_seconds=1)

    def stop(expected):
        state.stops.append(expected)
        state.clock.value = 102.0
        return True

    monkeypatch.setattr(control, "_stop_execution_job", stop)
    assert state.manager.retire_execution(state.expected.identity) is False
    state.threads[0].finish()
    result = state.manager.poll(ticket)
    assert result.uncertainty == "deadline" and result.stop_requested is None
    assert result.inspection is None and not state.manager.busy
    assert state.manager.begin_stop(state.expected) is ticket and len(state.stops) == 1
    state.clock.value = 100.5
    assert state.manager.poll(ticket).uncertainty == "deadline"


@pytest.mark.parametrize("bad_time", [99.0, float("nan"), True, "100"])
def test_invalid_parent_clock_never_hides_callback_or_allows_stop_replay(async_control, bad_time):
    state = async_control
    ticket = state.manager.begin_stop(state.expected)
    state.clock.value = bad_time
    assert state.manager.poll(ticket).uncertainty == "clock_unavailable"
    assert state.manager.busy and not state.manager.retire_execution(state.expected.identity)
    state.clock.value = 100.1
    state.threads[0].finish()
    assert not state.manager.busy and not state.stops
    assert state.manager.poll(ticket).uncertainty == "clock_unavailable"
    assert state.manager.begin_stop(state.expected) is ticket


def test_late_inspection_can_be_replaced_only_after_callback_exit_and_retirement(async_control):
    state = async_control
    ticket = state.manager.begin_inspection(state.expected, timeout_seconds=1)
    state.clock.value = 102.0
    assert state.manager.poll(ticket).uncertainty == "deadline"
    assert state.manager.begin_inspection(state.expected) is ticket
    assert state.manager.retire_inspection(ticket) is False
    state.threads[0].finish()
    assert state.manager.retire_inspection(ticket)
    next_ticket = state.manager.begin_inspection(state.expected)
    assert next_ticket is not ticket and len(state.threads) == 2
    state.threads[1].finish()
    assert state.manager.poll(next_ticket).uncertainty is None
    assert not state.stops


def test_async_controller_enforces_single_callback_and_retained_ticket_capacity(async_control):
    state = async_control
    state.manager = control.CohortJobExecutionController(max_pending=1)
    ticket = state.manager.begin_inspection(state.expected)
    assert state.manager.begin_stop(state.expected) is None
    state.threads[0].finish()
    assert state.manager.begin_stop(state.expected) is None
    assert state.manager.retire_inspection(ticket)
    assert state.manager.begin_stop(state.expected) is None
    assert state.manager.begin_inspection(state.expected) is not None


def test_full_stop_budget_keeps_terminal_inspection_capacity(async_control):
    state = async_control
    state.manager = control.CohortJobExecutionController(max_pending=2)
    stop = state.manager.begin_stop(state.expected)
    state.threads[0].finish()
    assert state.manager.poll(stop).stop_requested is True
    other_identity = replace(
        state.expected.identity,
        execution_generation=state.expected.identity.execution_generation + 1,
    )
    other = replace(
        state.expected,
        identity=other_identity,
        submission_id=control.STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX
        + control.coordination_sha256(other_identity),
    )
    assert state.manager.begin_stop(other) is None and len(state.threads) == 1
    assert state.manager.begin_stop(state.expected) is stop
    state.inspection.details.status = JobStatus.STOPPED
    terminal = state.manager.begin_inspection(state.expected)
    assert terminal is not None
    state.threads[1].finish()
    assert state.manager.poll(terminal).inspection.outer_driver_terminal
    assert state.manager.retire_inspection(terminal)
    assert state.manager.retire_execution(state.expected.identity)
    assert state.manager.begin_stop(other) is not None


def test_actual_thread_owns_http_while_parent_polls_and_cannot_drop_live_callback(
    inspection, monkeypatch
):
    monkeypatch.setattr(control, "Thread", threading.Thread)
    entered, release = threading.Event(), threading.Event()
    calls = []
    parent = threading.get_ident()

    def fetch(*args):
        calls.append((threading.get_ident(), args))
        entered.set()
        assert release.wait(2)
        return inspection.details

    monkeypatch.setattr(control, "_fetch_execution_job", fetch)
    manager = control.CohortJobExecutionController()
    ticket = manager.begin_inspection(inspection.expected)
    callback = manager._active.thread
    try:
        assert entered.wait(2)
        assert manager.busy and manager.poll(ticket) is None
        assert manager.retire_inspection(ticket) is False
        assert manager.retire_execution(inspection.expected.identity) is False
    finally:
        release.set()
        callback.join(2)
    assert not callback.is_alive() and not manager.busy
    assert calls[0][0] != parent and len(calls) == 1
    assert manager.poll(ticket).uncertainty is None
    assert manager.retire_execution(inspection.expected.identity)


@pytest.fixture
def stop_transport(transport, monkeypatch):
    """Exercise the real bounded body reader with the transport's socket stub."""
    session = requests.Session
    monkeypatch.setattr(session, "post", session.get, raising=False)
    transport.body = b'{"stopped":true}'
    return transport


def test_stop_post_uses_original_endpoint_auth_and_bounded_reader(inspection, stop_transport):
    expected = inspection.expected
    assert control._stop_execution_job(expected) is True
    assert stop_transport.initializations == [expected.jobs_endpoint]
    assert stop_transport.requests == [
        (
            expected.jobs_endpoint + "/api/jobs/" + expected.submission_id + "/stop",
            {
                "headers": {
                    "Authorization": "Bearer fixed-test-value",
                    "Accept-Encoding": "identity",
                },
                "cookies": {"session": "fixed-cookie"},
                "verify": "fixed-ca-path",
                "proxies": {},
                "stream": True,
                "allow_redirects": False,
                "timeout": (5.0, 5.0),
            },
        )
    ]
    assert stop_transport.reads == len(stop_transport.body) + 1
    assert stop_transport.timeout_updates == stop_transport.reads
    assert stop_transport.closed_responses == stop_transport.closed_sessions == 1


@pytest.mark.parametrize(
    "body",
    [b'{"stopped":1}', b'{"stopped":true,"stopped":false}', b'{"stopped":true,"extra":1}'],
)
def test_stop_response_requires_exact_boolean_mapping(inspection, stop_transport, body):
    stop_transport.body = body
    with pytest.raises((control.CohortJobExecutionControlError, ValueError)):
        control._stop_execution_job(inspection.expected)
    assert len(stop_transport.requests) == 1
    assert stop_transport.closed_responses == stop_transport.closed_sessions == 1


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_stop_post_never_follows_or_retries_http_refusal(inspection, stop_transport, status):
    stop_transport.response.status_code = status
    with pytest.raises(control.CohortJobExecutionControlError):
        control._stop_execution_job(inspection.expected)
    assert len(stop_transport.requests) == 1 and not stop_transport.reads
    assert stop_transport.closed_responses == stop_transport.closed_sessions == 1


def test_stop_sdk_configuration_cannot_substitute_an_endpoint(
    inspection, stop_transport, monkeypatch
):
    monkeypatch.setattr(
        dashboard_sdk,
        "SubmissionClient",
        lambda **_kwargs: SimpleNamespace(_address="https://unrelated.invalid"),
    )
    with pytest.raises(control.CohortJobExecutionControlError):
        control._stop_execution_job(inspection.expected)
    assert not stop_transport.requests


def test_false_stop_acknowledgement_is_unknown_and_cannot_be_replayed(async_control, monkeypatch):
    state = async_control

    def refuse(expected):
        state.stops.append(expected)
        return False

    monkeypatch.setattr(control, "_stop_execution_job", refuse)
    ticket = state.manager.begin_stop(state.expected)
    state.threads[0].finish()
    result = state.manager.poll(ticket)
    assert result.uncertainty == "control_unconfirmed"
    assert result.stop_requested is None and result.inspection is None
    assert state.manager.begin_stop(state.expected) is ticket and len(state.stops) == 1


def test_callback_start_failure_does_not_lose_retained_stop_identity(async_control, monkeypatch):
    state = async_control

    def fail_start(_self):
        raise RuntimeError("private native thread detail")

    monkeypatch.setattr(control.Thread, "start", fail_start)
    ticket = state.manager.begin_stop(state.expected)
    result = state.manager.poll(ticket)
    assert result.uncertainty == "callback_start_failed"
    assert not state.manager.busy and result.inspection is None
    assert state.manager.begin_stop(state.expected) is ticket
    assert len(state.threads) == 1 and not state.inspection.calls and not state.stops
