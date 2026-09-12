"""Real Core submission seams with a resource-free SDK and owned thread gates."""

import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread, get_ident
from types import SimpleNamespace

import pytest
from django.db import connections

from django_ray.runner import cohort_core, cohort_core_submission, ray_core
from django_ray.runner.cohort_connection import CoreConnectionTicket
from django_ray.runtime import runtime_env
from django_ray.target.cohort_transport import prepare_cohort_execution
from tests.unit.test_cohort_execution import _contract
from tests.unit.test_ray_core_runner import _task_execution


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "owned callback did not reach its test stage"
        time.sleep(0.005)


@pytest.fixture
def case(monkeypatch, request, tmp_path):
    contract = _contract()
    code = tmp_path / "code"
    code.mkdir()
    (code / "application.py").write_text("original content", encoding="utf-8")
    environment = runtime_env.normalize_runtime_env(
        {"working_dir": str(code)} if getattr(request, "param", None) == "local" else {}
    )
    task = _task_execution(
        contract.identity.task_execution_pk,
        task_id=contract.identity.task_id,
        attempt_number=contract.identity.attempt_number,
        execution_generation=contract.identity.execution_generation,
        execution_protocol_version=3,
        claimed_by_worker="owned-worker",
        callable_path="testproject.tasks.add_numbers",
        runtime_env_json=environment.serialized,
        runtime_env_hash=environment.digest,
    )
    prepared = prepare_cohort_execution(task, contract=contract, transport="direct-ray-core")
    value = SimpleNamespace(
        task=task,
        code=code,
        contract=contract,
        prepared=prepared,
        connection=CoreConnectionTicket(1, "sha256:" + "a" * 64, 10.0),
        wall=datetime(2026, 1, 1, tzinfo=UTC),
        mono=10.0,
        snapshot_gate=Event(),
        upload_gate=Event(),
        remote_gate=Event(),
        diagnostic_gate=Event(),
        cleanup_gate=Event(),
        events=[],
        requests=[],
        fail_cleanup=False,
        fail_upload=False,
        fail_remote=False,
        connected=False,
        initialized=True,
        default=True,
        contexts=0,
    )
    for name in ("snapshot_gate", "upload_gate", "remote_gate", "diagnostic_gate", "cleanup_gate"):
        getattr(value, name).set()
    client_context = SimpleNamespace(client_worker=object())
    value.direct_context = SimpleNamespace(core_worker=object())
    value.client_context = client_context
    client = SimpleNamespace(
        ray=SimpleNamespace(
            is_connected=lambda: value.connected,
            is_default=lambda: value.default,
            get_context=lambda: value.client_context,
        ),
        num_connected_contexts=lambda: value.contexts,
    )
    reference = SimpleNamespace(hex=lambda: "b" * 56)

    class Remote:
        def options(self, **kwargs):
            value.events.append(("options", get_ident()))
            return self

        def remote(self, request, **bindings):
            value.events.append(("remote", get_ident()))
            value.remote_gate.wait(3)
            if value.fail_remote:
                raise RuntimeError("private SDK detail")
            value.requests.append((request, bindings))
            return reference

    def runtime_context():
        value.events.append(("diagnostic", get_ident()))
        value.diagnostic_gate.wait(3)
        return SimpleNamespace(get_job_id=lambda: "01000000")

    value.ray = SimpleNamespace(
        __version__="2.58.0",
        util=SimpleNamespace(client=client),
        is_initialized=lambda: value.initialized,
        init=lambda **_: pytest.fail("Core submission cannot initialize Ray"),
        shutdown=lambda **_: pytest.fail("Core submission cannot disconnect Ray"),
        remote=lambda function: Remote(),
        get_runtime_context=runtime_context,
    )
    monkeypatch.setitem(sys.modules, "ray", value.ray)
    monkeypatch.setitem(sys.modules, "ray.util", value.ray.util)
    monkeypatch.setitem(sys.modules, "ray.util.client", client)
    monkeypatch.setitem(
        sys.modules, "ray._private.worker", SimpleNamespace(global_worker=value.direct_context)
    )
    monkeypatch.setattr(
        cohort_core,
        "_local_runtime",
        lambda _: (contract.expected_django_ray_version, contract.target_expectation.runtime),
    )
    monkeypatch.setattr(ray_core, "_execute_cohort_task_remote_cached", None)
    monkeypatch.setattr(cohort_core_submission, "_clock", lambda: (value.wall, value.mono))
    value.db = SimpleNamespace(in_atomic_block=False, connection=object(), autocommit=True)
    monkeypatch.setattr(connections, "all", lambda **_: [value.db])

    @contextmanager
    def snapshot(runtime):
        value.events.append(("snapshot", get_ident()))
        value.snapshot_gate.wait(3)
        try:
            yield runtime
        finally:
            value.events.append(("cleanup", get_ident()))
            value.cleanup_gate.wait(3)
            if value.fail_cleanup:
                raise OSError("private cleanup detail")

    def upload(runtime):
        value.events.append(("upload", get_ident()))
        value.upload_gate.wait(3)
        if value.fail_upload:
            raise OSError("private upload detail")
        return runtime.spec

    monkeypatch.setattr(runtime_env, "snapshot_local_runtime_env", snapshot)
    monkeypatch.setattr(runtime_env, "prepare_runtime_env_for_ray_core", upload)
    value.runner = ray_core.RayCoreRunner._from_existing_connection()
    value.controller = cohort_core_submission.CohortCoreSubmissionController()
    yield value
    if value.controller._operation is not None:
        value.controller.abort(value.controller._operation.ticket)
    for name in ("snapshot_gate", "upload_gate", "remote_gate", "diagnostic_gate", "cleanup_gate"):
        getattr(value, name).set()
    operation = value.controller._operation
    if operation is not None and operation.thread is not None:
        operation.thread.join(4)
        assert not operation.thread.is_alive()


def _begin(case, **kwargs):
    return case.controller.begin(
        case.runner, case.task, prepared=case.prepared, connection_ticket=case.connection, **kwargs
    )


def _poll(case, ticket):
    return case.controller.poll(ticket, connection_ticket=case.connection)


def _finished(case, ticket):
    operation = case.controller._operation
    _wait(lambda: operation.completed.is_set() and not operation.thread.is_alive())
    return _poll(case, ticket)


@pytest.mark.parametrize("client", [False, True])
def test_real_submission_runs_only_on_owned_callback_and_binds_exact_context(case, client):
    if client:
        case.connected, case.contexts = True, 1
        case.prepared = prepare_cohort_execution(
            case.task, contract=case.contract, transport="ray-client"
        )
    parent = get_ident()
    ticket = _begin(case)
    result = _finished(case, ticket)
    assert result.accepted and result.local_cleanup_complete
    assert result.handle is case.runner._pending_tasks[case.task.pk]
    assert result.handle.ray_job_id == "01000000"
    assert result.handle.cohort_prepared is case.prepared
    assert case.requests[0][0] == case.prepared.request_json
    assert case.requests[0][1]["expected_request_digest"] == ticket.request_digest
    assert all(owner != parent for _, owner in case.events)
    assert len({owner for _, owner in case.events}) == 1
    assert case.controller.busy
    assert case.controller.retire(ticket)
    assert not case.controller.busy
    with pytest.raises(cohort_core_submission.CohortCoreSubmissionError):
        _begin(case)  # The original pending ObjectRef cannot be submitted again.


def test_capture_keeps_scalar_snapshot_when_original_task_changes(case):
    case.snapshot_gate.clear()
    ticket = _begin(case)
    _wait(lambda: any(name == "snapshot" for name, _ in case.events))
    captured = case.controller._operation.source
    original = captured.task.runtime_env_json
    case.task.runtime_env_json = "changed after capture"
    case.task.args_json = "[999]"
    assert captured.task.runtime_env_json == original
    case.snapshot_gate.set()
    assert _finished(case, ticket).accepted
    assert case.requests[0][0] == case.prepared.request_json


@pytest.mark.parametrize("case", ["local"], indirect=True)
@pytest.mark.parametrize("mutation", ["content", "trust"])
def test_changed_prepared_manifest_refuses_before_upload(case, mutation, monkeypatch):
    from django_ray.conf import settings

    if mutation == "content":
        (case.code / "application.py").write_text("changed content", encoding="utf-8")
    else:
        configuration = settings.get_settings()
        monkeypatch.setattr(
            settings,
            "get_settings",
            lambda: {**configuration, "WORKFLOW_PLAN_TRUST_IDENTITY": {"deployment": "changed"}},
        )
    ticket = _begin(case)
    result = _finished(case, ticket)
    assert result.uncertainty is not None and not result.accepted
    assert not any(name in {"upload", "remote"} for name, _ in case.events)
    assert not case.requests


@pytest.mark.parametrize("phase", ["snapshot", "upload", "remote", "diagnostic", "cleanup"])
def test_deadline_keeps_callback_slot_and_returned_handle_until_actual_exit(case, phase):
    getattr(case, phase + "_gate").clear()
    ticket = _begin(case, timeout_seconds=5)
    _wait(lambda: any(name == phase for name, _ in case.events))
    case.wall += timedelta(seconds=6)
    case.mono += 6
    assert _poll(case, ticket).uncertainty == "submission_deadline"
    assert case.controller.busy and not case.controller.retire(ticket)
    assert _poll(case, ticket).handle is None
    if phase == "diagnostic":
        assert case.runner._pending_tasks[case.task.pk].object_ref is not None
    getattr(case, phase + "_gate").set()
    result = _finished(case, ticket)
    assert not result.accepted and result.local_cleanup_complete
    assert bool(case.requests) == (phase in {"remote", "diagnostic"})
    assert (result.handle is not None) == (phase in {"remote", "diagnostic"})
    assert case.controller.retire(ticket)


@pytest.mark.parametrize("fault", ["worker", "context", "transport", "initialized"])
def test_connection_change_while_preparing_prevents_remote_submission(case, fault):
    case.upload_gate.clear()
    ticket = _begin(case)
    _wait(lambda: any(name == "upload" for name, _ in case.events))
    if fault == "worker":
        case.direct_context.core_worker = object()
    elif fault == "context":
        case.default = False
    elif fault == "transport":
        case.connected, case.contexts = True, 1
    else:
        case.initialized = False
    case.upload_gate.set()
    result = _finished(case, ticket)
    assert result.uncertainty is not None and not result.accepted
    assert case.requests == []
    assert case.controller.retire(ticket)


@pytest.mark.parametrize("fault", ["multiple", "nondefault", "version", "uninitialized"])
def test_unsupported_connection_is_rejected_before_callback(case, fault):
    if fault == "multiple":
        case.contexts = 2
    elif fault == "nondefault":
        case.default = False
    elif fault == "version":
        case.ray.__version__ = "2.57.0"
    else:
        case.initialized = False
    with pytest.raises(RuntimeError):
        _begin(case)
    assert not case.controller.busy and not case.events


@pytest.mark.parametrize("fault", ["wall", "mono", "nan", "naive"])
def test_parent_clock_failure_remains_sticky_after_callback_cleanup(case, fault):
    case.upload_gate.clear()
    ticket = _begin(case)
    _wait(lambda: any(name == "upload" for name, _ in case.events))
    if fault == "wall":
        case.wall -= timedelta(seconds=1)
    elif fault == "mono":
        case.mono -= 1
    elif fault == "nan":
        case.mono = float("nan")
    else:
        case.wall = case.wall.replace(tzinfo=None)
    assert _poll(case, ticket).uncertainty == "submission_clock_unavailable"
    case.wall, case.mono = datetime(2026, 1, 1, tzinfo=UTC), 10.0
    case.upload_gate.set()
    result = _finished(case, ticket)
    assert not result.accepted and result.uncertainty == "submission_clock_unavailable"
    assert not case.requests


@pytest.mark.parametrize("failure", ["upload", "remote", "cleanup"])
def test_failed_phase_retains_uncertainty_and_cannot_hide_cleanup_failure(case, failure):
    setattr(case, "fail_" + failure, True)
    ticket = _begin(case)
    result = _finished(case, ticket)
    assert not result.accepted and result.uncertainty is not None
    assert _begin(case) is ticket
    assert case.controller.retire(ticket) == (failure != "cleanup")


def test_abort_and_connection_ticket_replacement_are_sticky(case):
    case.remote_gate.clear()
    ticket = _begin(case)
    _wait(lambda: any(name == "remote" for name, _ in case.events))
    replacement = replace(case.connection)
    result = case.controller.poll(ticket, connection_ticket=replacement)
    assert result.uncertainty == "connection_changed"
    case.controller.abort(ticket)
    case.remote_gate.set()
    result = _finished(case, ticket)
    assert result.uncertainty == "connection_changed" and result.handle is not None
    assert not result.accepted
    with pytest.raises(cohort_core_submission.CohortCoreSubmissionError):
        case.controller.poll(replace(ticket), connection_ticket=case.connection)


@pytest.mark.parametrize("manual", [False, True])
def test_begin_rejects_parent_transactions_before_callback(case, manual):
    if manual:
        case.db.autocommit = False
    else:
        case.db.in_atomic_block = True
    with pytest.raises(cohort_core_submission.CohortCoreSubmissionError):
        _begin(case)
    assert not case.controller.busy and not case.events


def test_transaction_guard_reads_initialized_state_without_connection_io(case):
    def connect():
        pytest.fail("Transaction guard must not initialize a database connection")

    case.db.get_autocommit = connect
    case.db.ensure_connection = connect
    cohort_core_submission._outside_transaction()
    case.db.autocommit = None
    with pytest.raises(cohort_core_submission.CohortCoreSubmissionError):
        cohort_core_submission._outside_transaction()


def test_only_creating_parent_can_operate_controller(case):
    errors = []

    def foreign():
        try:
            _begin(case)
        except cohort_core_submission.CohortCoreSubmissionError:
            errors.append("wrong owner")

    thread = Thread(target=foreign)
    thread.start()
    thread.join(2)
    assert errors == ["wrong owner"]
    assert not case.controller.busy and not case.events


def test_one_slot_rejects_other_work_until_owned_cleanup_exit(case):
    case.cleanup_gate.clear()
    ticket = _begin(case)
    _wait(lambda: any(name == "cleanup" for name, _ in case.events))
    identity = replace(case.prepared.identity, task_execution_pk=900, task_id="another-task")
    task = SimpleNamespace(**vars(case.task))
    task.pk, task.task_id = identity.task_execution_pk, identity.task_id
    prepared = prepare_cohort_execution(
        task, contract=replace(case.contract, identity=identity), transport="direct-ray-core"
    )
    assert (
        case.controller.begin(
            case.runner, task, prepared=prepared, connection_ticket=case.connection
        )
        is None
    )
    assert _begin(case) is ticket
    assert not case.controller.retire(ticket)
    case.cleanup_gate.set()
    assert _finished(case, ticket).accepted
    assert case.controller.retire(ticket)


@pytest.mark.parametrize("timeout", [True, 0, -1, 61, float("nan"), float("inf")])
def test_invalid_deadline_never_starts_a_callback(case, timeout):
    with pytest.raises(cohort_core_submission.CohortCoreSubmissionError):
        _begin(case, timeout_seconds=timeout)
    assert not case.controller.busy and not case.events


@pytest.mark.parametrize(
    "changes",
    [
        {"epoch": True},
        {"epoch": 0},
        {"configuration_digest": "invalid"},
        {"deadline": float("nan")},
    ],
)
def test_malformed_connection_ticket_never_starts_a_callback(case, changes):
    case.connection = replace(case.connection, **changes)
    with pytest.raises(cohort_core_submission.CohortCoreSubmissionError):
        _begin(case)
    assert not case.controller.busy and not case.events


def test_callback_start_failure_is_owned_until_parent_retires(case, monkeypatch):
    def unavailable(**_):
        raise RuntimeError("private thread detail")

    monkeypatch.setattr(cohort_core_submission, "Thread", unavailable)
    ticket = _begin(case)
    result = _poll(case, ticket)
    assert result.uncertainty == "callback_start_failed"
    assert result.local_cleanup_complete and not result.accepted
    assert case.controller.busy
    assert case.controller.retire(ticket)
    assert not case.events


def test_client_worker_replacement_during_upload_cannot_submit(case):
    case.connected, case.contexts = True, 1
    case.prepared = prepare_cohort_execution(
        case.task, contract=case.contract, transport="ray-client"
    )
    case.upload_gate.clear()
    ticket = _begin(case)
    _wait(lambda: any(name == "upload" for name, _ in case.events))
    case.client_context.client_worker = object()
    case.upload_gate.set()
    result = _finished(case, ticket)
    assert not result.accepted and result.uncertainty is not None
    assert not case.requests
