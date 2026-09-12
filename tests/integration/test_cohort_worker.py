"""Current-cohort worker orchestration with real Sync execution and no native Ray."""

import json
import platform
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from django_ray import maintenance
from django_ray.management.commands import django_ray_worker as worker
from django_ray.models import RayTaskCohortClaim, RayTaskExecution, RayWorkerRetirement
from django_ray.runner import cohort_completion, cohort_dispatch
from django_ray.runner.cohort_claims import ClaimedCohortTask
from django_ray.target.cohort_claim import (
    CohortBindingSpec,
    CohortManagerRuntime,
    CohortPythonVersion,
    CohortRunnerFamily,
)
from tests.integration.test_cohort_claim_storage import _claim
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def add(a, b):
    return a + b


def fail():
    raise ValueError("bounded application failure")


@pytest.fixture
def command(case, monkeypatch):
    from django_ray.runner import cohort_core_submission, cohort_preparation

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.now

    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(cohort_dispatch, "datetime", Clock)
    monkeypatch.setattr(cohort_completion, "_clock", lambda: case.now)
    monkeypatch.setattr(cohort_preparation, "_clock", lambda: (case.now, time.monotonic()))
    monkeypatch.setattr(cohort_core_submission, "_clock", lambda: (case.now, time.monotonic()))
    command = worker.Command()
    command.worker_id = case.owner.worker_id
    command.lease_identity = case.owner
    command.lease = case.lease
    command.execution_mode = "sync"
    return command


def _claim_task(case, *, failing=False):
    RayTaskExecution.objects.filter(pk=case.task.pk).update(
        callable_path=__name__ + (".fail" if failing else ".add"),
        args_json="[]" if failing else "[1,2]",
        kwargs_json="{}",
    )
    case.task.refresh_from_db()
    python = CohortPythonVersion(
        platform.python_implementation().lower(),
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    record = _claim(
        case,
        binding_spec=CohortBindingSpec(CohortRunnerFamily.SYNC, "0.5.0", sync_python=python),
        manager_runtime=CohortManagerRuntime("0.5.0", python),
    )
    case.task.refresh_from_db()
    return ClaimedCohortTask(case.task, record, None)


def test_real_sync_worker_resolves_claim_and_publishes_result_atomically(case, command):
    command._dispatch_cohort_task(_claim_task(case))
    case.task.refresh_from_db()
    assert case.task.state == "SUCCEEDED", case.task.error_message
    assert json.loads(case.task.result_data) == 3
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"
    assert not command._cohort_dispatches
    assert command.tasks_processed_count == 1


@pytest.fixture
def core_sdk(monkeypatch, command):
    """Exercise the real runner while replacing only the external Ray SDK."""
    from django_ray.runner import cohort_core, cohort_core_submission, cohort_preparation, ray_core
    from django_ray.runner.cohort_connection import CoreConnectionTicket
    from django_ray.target.attestation import RayRuntimeVersion
    from tests.unit.test_cohort_preparation import FakeThread

    state = SimpleNamespace(connected=False, initialized=True, calls=[], before_remote=None)
    reference = SimpleNamespace(hex=lambda: "a" * 56)

    class Remote:
        def options(self, **options):
            return self

        def remote(self, request_json, **bindings):
            from django.db import connection

            assert not connection.in_atomic_block
            if state.before_remote is not None:
                state.before_remote(request_json, bindings)
            state.calls.append((request_json, bindings))
            return reference

    client_context = SimpleNamespace(client_worker=object())
    client = SimpleNamespace(
        ray=SimpleNamespace(
            is_connected=lambda: state.connected,
            is_default=lambda: True,
            get_context=lambda: client_context,
        ),
        num_connected_contexts=lambda: int(state.connected is True),
    )
    sdk = SimpleNamespace(
        __version__="2.58.0",
        is_initialized=lambda: state.initialized,
        init=lambda **kwargs: pytest.fail("Dispatch must never initialize Ray"),
        util=SimpleNamespace(client=client),
        remote=lambda function: Remote(),
        get_runtime_context=lambda: SimpleNamespace(get_job_id=lambda: "01000000"),
    )
    monkeypatch.setitem(sys.modules, "ray", sdk)
    monkeypatch.setitem(sys.modules, "ray.util", sdk.util)
    monkeypatch.setitem(sys.modules, "ray.util.client", client)
    monkeypatch.setitem(
        sys.modules,
        "ray._private.worker",
        SimpleNamespace(global_worker=SimpleNamespace(core_worker=object())),
    )
    monkeypatch.setattr(
        cohort_core,
        "_local_runtime",
        lambda _: ("0.5.0", RayRuntimeVersion(2, 58, 0, "cpython", 3, 12, 14)),
    )

    def thread(*, target, args=(), name, daemon):
        return FakeThread(target=target, args=args, name=name, daemon=daemon)

    monkeypatch.setattr(cohort_preparation, "Thread", thread)
    monkeypatch.setattr(cohort_core_submission, "Thread", thread)
    monkeypatch.setattr(ray_core, "_execute_cohort_task_remote_cached", None)
    state.runner = ray_core.RayCoreRunner()
    command.execution_mode = "cluster"
    command.cluster_address = "ray://selected.example:10001"
    command._cohort_controller = SimpleNamespace(
        connected=True,
        connection_ticket=CoreConnectionTicket(1, "sha256:" + "a" * 64, 100.0),
        family=CohortRunnerFamily.RAY_CORE,
    )
    return state


def _advance_core(command):
    """Run each owned callback explicitly; never rely on scheduler timing."""
    for _ in range(4):
        for ticket in tuple(command._cohort_preparation_tickets.values()):
            operation = command._cohort_preparation._current(ticket)
            if operation.thread is not None and operation.thread.is_alive():
                operation.thread.run()
        command._poll_cohort_preparations()
        controller = command._cohort_core_submission
        if controller is not None and controller._operation is not None:
            thread = controller._operation.thread
            if thread is not None and thread.is_alive():
                thread.run()
        command._poll_cohort_submissions()
        if not command._cohort_preparation_tickets and not command._cohort_core_submission_tickets:
            return
    pytest.fail("Controlled Core callbacks were not retired")


@pytest.fixture
def blocked_core(case, command, core_sdk, monkeypatch):
    """A real owned thread, with finite events at external-work boundaries."""
    from contextlib import contextmanager
    from threading import Event, Thread, get_ident

    from django.db.backends.utils import CursorWrapper

    from django_ray.runner import cohort_core_submission, cohort_preparation
    from django_ray.runtime import runtime_env
    from django_ray.workflow import plans

    value = SimpleNamespace(stage=None, entered=Event(), release=Event(), threads=[])
    parent = get_ident()
    execute = CursorWrapper.execute

    def parent_sql(cursor, *args, **kwargs):
        assert get_ident() == parent, "Owned callback must not use Django SQL"
        return execute(cursor, *args, **kwargs)

    def gate(stage):
        assert get_ident() != parent, "Filesystem and SDK work belongs to the callback"
        if value.stage == stage:
            value.entered.set()
            assert value.release.wait(4), "Bounded callback fixture was not released"

    def thread(**kwargs):
        owned = Thread(**kwargs)
        value.threads.append(owned)
        return owned

    plan = plans.runtime_env_plan_identity
    upload = runtime_env.prepare_runtime_env_for_ray_core
    snapshot = runtime_env.snapshot_local_runtime_env

    def manifest(*args, **kwargs):
        gate("manifest")
        return plan(*args, **kwargs)

    def prepare(runtime):
        gate("upload")
        return upload(runtime)

    @contextmanager
    def owned_snapshot(runtime):
        with snapshot(runtime) as prepared:
            try:
                yield prepared
            finally:
                gate("cleanup")

    sdk = sys.modules["ray"]
    context = sdk.get_runtime_context

    def diagnostic():
        gate("diagnostic")
        return context()

    core_sdk.before_remote = lambda *_: gate("remote")
    monkeypatch.setattr(sdk, "get_runtime_context", diagnostic)
    monkeypatch.setattr(plans, "runtime_env_plan_identity", manifest)
    monkeypatch.setattr(runtime_env, "prepare_runtime_env_for_ray_core", prepare)
    monkeypatch.setattr(runtime_env, "snapshot_local_runtime_env", owned_snapshot)
    monkeypatch.setattr(CursorWrapper, "execute", parent_sql)
    monkeypatch.setattr(cohort_preparation, "Thread", thread)
    monkeypatch.setattr(cohort_core_submission, "Thread", thread)
    command.ray_core_runner = core_sdk.runner
    yield value
    value.release.set()
    for owned in value.threads:
        owned.join(4)
        assert not owned.is_alive(), "Fixture must finish its exact callback before SQL teardown"
    command._poll_cohort_preparations(allow_dispatch=False)
    command._poll_cohort_submissions(allow_submit=False)


def _begin_blocked_core(case, command, blocked, stage):
    from tests.integration.test_cohort_dispatch import _claimed

    blocked.stage = stage
    claimed = _claimed(case, "ray_core")
    command._dispatch_cohort_task(claimed)
    if stage != "manifest":
        initial = blocked.threads[0]
        initial.join(3)
        assert not initial.is_alive()
        command._poll_cohort_preparations()
    assert blocked.entered.wait(2)
    return claimed


@pytest.mark.parametrize("stage", ["manifest", "upload", "remote", "diagnostic", "cleanup"])
def test_owned_work_keeps_heartbeat_and_blocks_claims_and_retirement(
    case, command, blocked_core, stage
):
    claimed = _begin_blocked_core(case, command, blocked_core, stage)
    command._cohort_controller.claim = lambda **_: pytest.fail(
        "Busy callback must stop another claim"
    )
    command._cohort_controller.retirement_ready = True
    command._cohort_retirement_requested = True
    for _ in range(3):
        case.now += timedelta(microseconds=1)
        command.send_heartbeat()
        command._poll_cohort_preparations()
        command._poll_cohort_submissions()
        assert command._claim_and_process_cohort_tasks(10) == 0
        command._finish_cohort_retirement_admission()
        assert not command.shutdown_requested
    case.task.refresh_from_db()
    assert case.task.last_heartbeat_at == case.now
    assert not command._cohort_core_handles
    assert command._owned_cohort_cleanup_count() == 0  # Active SQL already counts this identity.
    blocked_core.release.set()
    for owned in tuple(blocked_core.threads):
        owned.join(3)
        assert not owned.is_alive()
    command._poll_cohort_preparations()
    # Initial preparation can now start the distinct submission callback.
    for owned in tuple(blocked_core.threads):
        owned.join(3)
        assert not owned.is_alive()
    command._poll_cohort_submissions()
    assert claimed.execution.pk in command._cohort_core_handles
    assert not command._cohort_preparation_tickets and not command._cohort_core_submission_tickets


@pytest.mark.parametrize("change", ["cancel", "connection", "shutdown"])
def test_initial_preparation_change_holds_original_claim_without_sdk(
    case, command, blocked_core, core_sdk, change
):
    from dataclasses import replace

    claimed = _begin_blocked_core(case, command, blocked_core, "manifest")
    if change == "cancel":
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")
    elif change == "connection":
        command._cohort_controller.connection_ticket = replace(
            command._cohort_controller.connection_ticket
        )
    else:
        command.shutdown_requested = True
    command._poll_cohort_preparations()
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.disposition == "HELD" and row.hold_application_invoked is None
    assert row.prepared_request_digest is None and row.dispatched_at is None
    assert command._cohort_preparation_tickets and command._cohort_submission_busy()
    blocked_core.release.set()
    blocked_core.threads[0].join(3)
    assert not blocked_core.threads[0].is_alive()
    command._poll_cohort_preparations()
    assert not command._cohort_preparation_tickets and not core_sdk.calls
    command._dispatch_cohort_task(claimed)
    assert not command._cohort_preparation_tickets and not core_sdk.calls


def test_replaced_connection_during_remote_keeps_exited_uncertain_handle(
    case, command, blocked_core, core_sdk
):
    from dataclasses import replace

    claimed = _begin_blocked_core(case, command, blocked_core, "remote")
    command._cohort_controller.connection_ticket = replace(
        command._cohort_controller.connection_ticket
    )
    command._poll_cohort_submissions()
    assert not command._cohort_core_handles
    assert RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id).disposition == "HELD"
    blocked_core.release.set()
    blocked_core.threads[-1].join(3)
    assert not blocked_core.threads[-1].is_alive()
    command._poll_cohort_submissions()
    assert len(core_sdk.calls) == 1
    handle = command._cohort_core_handles[case.task.pk]
    assert handle is core_sdk.runner._pending_tasks[case.task.pk]
    assert not command._cohort_core_submission_tickets
    command._dispatch_cohort_task(claimed)
    assert len(core_sdk.calls) == 1


def test_cancel_during_core_upload_aborts_before_remote_without_releasing_owned_slot(
    case, command, blocked_core, core_sdk
):
    claimed = _begin_blocked_core(case, command, blocked_core, "upload")
    RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")
    command._poll_cohort_submissions()
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.disposition == "HELD" and row.dispatched_at is not None
    assert row.hold_application_invoked is None
    assert command._cohort_core_submission_tickets and command._cohort_submission_busy()
    blocked_core.release.set()
    blocked_core.threads[-1].join(3)
    assert not blocked_core.threads[-1].is_alive()
    command._poll_cohort_submissions()
    assert not core_sdk.calls and not command._cohort_core_submission_tickets
    case.task.refresh_from_db()
    assert case.task.state == "CANCELLING" and case.task.cancellation_status is None


def test_terminal_sql_keeps_pending_core_capacity_until_final_handle_is_retired(
    case, command, blocked_core, core_sdk
):
    from tests.integration.test_cohort_completion import _result

    _begin_blocked_core(case, command, blocked_core, "diagnostic")
    value = command._cohort_dispatches[case.task.pk]
    assert len(core_sdk.calls) == 1 and not command._cohort_core_handles
    # An authentic result can arrive independently of the delayed diagnostic
    # response; terminal SQL must not authorize forgetting its local callback.
    assert command._apply_cohort_result(value, _result(value), provenance="owned_direct") == 1
    case.task.refresh_from_db()
    assert case.task.state == "SUCCEEDED"
    assert command._cohort_core_submission_tickets
    assert command._owned_cohort_cleanup_count() == 1
    assert command.tasks_processed_count == 0
    blocked_core.release.set()
    blocked_core.threads[-1].join(3)
    assert not blocked_core.threads[-1].is_alive()
    command._poll_cohort_submissions()
    assert not command._cohort_core_submission_tickets and not core_sdk.runner._pending_tasks
    assert not command._cohort_core_handles and not command._cohort_dispatches
    assert command._owned_cohort_cleanup_count() == 0
    assert command.tasks_processed_count == 1


@pytest.mark.parametrize("stage", ["upload", "diagnostic", "cleanup"])
def test_shutdown_never_disconnects_an_owned_live_core_callback(
    case, command, blocked_core, monkeypatch, stage
):
    from django_ray.runner import cohort_core_submission
    from django_ray.runner.cohort_connection import CoreConnectionPhase

    claimed = _begin_blocked_core(case, command, blocked_core, stage)
    connection = SimpleNamespace(
        poll=lambda _: SimpleNamespace(callback_running=False, phase=CoreConnectionPhase.CONNECTED),
        begin_cleanup=lambda *_, **__: pytest.fail("Must not disconnect a retained callback"),
    )
    owner = command._cohort_controller
    owner.connection = connection
    owner.lifecycle = SimpleNamespace(outstanding=None)
    owner.adapter = None
    owner.invalidate = lambda: None
    owner.poll_stopped_cleanup = lambda: None
    clock = [0.0]
    monkeypatch.setattr(cohort_core_submission, "_clock", lambda: (case.now, 10.0))
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 1))
    command.shutdown_requested = True
    command._shutdown_cohort()
    assert command.shutdown_exit_code == 1
    assert command._cohort_core_submission_tickets
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.disposition == "HELD" and row.dispatched_at is not None
    assert row.owner_lease_id == claimed.claim.owner.worker_id
    assert command._cohort_core_submission.busy


@pytest.mark.parametrize("connected,transport", [(False, "direct-ray-core"), (True, "ray-client")])
def test_real_core_runner_dispatch_uses_observed_transport(
    case, command, core_sdk, connected, transport
):
    from django_ray.target.cohort_transport import validate_prepared_cohort_execution
    from tests.integration.test_cohort_dispatch import _claimed

    claimed = _claimed(case, "ray_core")
    core_sdk.connected = connected
    command.ray_core_runner = core_sdk.runner

    def before_remote(request_json, bindings):
        row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
        assert row.prepared_request_digest == bindings["expected_request_digest"]
        assert row.dispatched_at is not None
        assert row.disposition == "OPEN"

    core_sdk.before_remote = before_remote
    command._dispatch_cohort_task(claimed)
    _advance_core(command)
    assert len(core_sdk.calls) == 1
    value = command._cohort_dispatches[case.task.pk]
    request, _ = validate_prepared_cohort_execution(value.prepared, task=value.execution)
    assert request.compiled_graph_submission_transport == transport
    assert core_sdk.calls[0][0] == value.prepared.request_json
    assert (
        command._cohort_core_handles[case.task.pk] is core_sdk.runner._pending_tasks[case.task.pk]
    )
    assert RayTaskCohortClaim.objects.get(pk=value.claim.claim_id).disposition == "OPEN"
    case.task.refresh_from_db()
    assert case.task.ray_job_id == "01000000:" + "a" * 48
    assert case.task.ray_address == command.cluster_address
    assert value.execution.ray_job_id == case.task.ray_job_id
    assert value.execution.ray_address == case.task.ray_address


@pytest.mark.parametrize("connected,initialized", [(None, True), (False, False)])
def test_core_unavailable_transport_is_held_before_preparation(
    case, command, core_sdk, connected, initialized
):
    from tests.integration.test_cohort_dispatch import _claimed

    claimed = _claimed(case, "ray_core")
    command.ray_core_runner = core_sdk.runner
    core_sdk.connected, core_sdk.initialized = connected, initialized
    command._dispatch_cohort_task(claimed)
    _advance_core(command)
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.disposition == "HELD"
    assert row.prepared_request_digest is None
    assert row.dispatched_at is None
    assert not core_sdk.calls


@pytest.mark.parametrize("mode,address", [("sync", None), ("cluster", None), ("local", "other")])
def test_core_invalid_selected_connection_is_held_before_preparation(
    case, command, core_sdk, mode, address
):
    from tests.integration.test_cohort_dispatch import _claimed

    claimed = _claimed(case, "ray_core")
    command.ray_core_runner = core_sdk.runner
    command.execution_mode, command.cluster_address = mode, address
    command._dispatch_cohort_task(claimed)
    _advance_core(command)
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.disposition == "HELD"
    assert row.prepared_request_digest is None and row.dispatched_at is None
    assert not core_sdk.calls


@pytest.mark.parametrize("connected,initialized", [(True, True), (None, True), (False, False)])
def test_core_transport_change_after_prepare_refuses_before_remote(
    case, command, core_sdk, monkeypatch, connected, initialized
):
    from tests.integration.test_cohort_dispatch import _claimed

    original = cohort_dispatch.mark_cohort_dispatch_started

    def changed_transport(*args, **kwargs):
        value = original(*args, **kwargs)
        core_sdk.connected, core_sdk.initialized = connected, initialized
        return value

    monkeypatch.setattr(cohort_dispatch, "mark_cohort_dispatch_started", changed_transport)
    claimed = _claimed(case, "ray_core")
    command.ray_core_runner = core_sdk.runner
    command._dispatch_cohort_task(claimed)
    _advance_core(command)
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.disposition == "HELD"
    assert row.dispatched_at is not None
    assert row.prepared_request_digest is not None
    assert not core_sdk.calls
    assert not core_sdk.runner._pending_tasks


def test_core_local_diagnostics_ignore_ambient_ray_address(case, command, core_sdk, monkeypatch):
    from tests.integration.test_cohort_dispatch import _claimed

    monkeypatch.setenv("RAY_ADDRESS", "ray://unrelated.example:10001")
    command.execution_mode, command.cluster_address = "local", None
    command.ray_core_runner = core_sdk.runner
    command._dispatch_cohort_task(_claimed(case, "ray_core"))
    _advance_core(command)
    case.task.refresh_from_db()
    assert len(core_sdk.calls) == 1
    assert case.task.ray_address == "local"


@pytest.mark.parametrize("failure", ["write", "lease", "claim", "cancel", "existing"])
def test_core_diagnostic_failure_retains_owned_submission(
    case, command, core_sdk, monkeypatch, failure
):
    from django.db import DatabaseError, transaction

    from django_ray.target.cohort_claim import CohortHoldBoundary, CohortHoldReason
    from django_ray.target.cohort_claim_storage import hold_cohort_claim
    from tests.integration.test_cohort_dispatch import _claimed

    claimed = _claimed(case, "ray_core")
    command.ray_core_runner = core_sdk.runner
    original_save = RayTaskExecution.save

    def save(task, *args, **kwargs):
        if kwargs.get("update_fields") == ["ray_job_id", "ray_address"]:
            assert command._cohort_core_handles[task.pk] is core_sdk.runner._pending_tasks[task.pk]
            raise DatabaseError("diagnostic write unavailable")
        return original_save(task, *args, **kwargs)

    def before_remote(*_):
        if failure == "write":
            monkeypatch.setattr(RayTaskExecution, "save", save)
        elif failure == "lease":
            type(case.lease).objects.filter(pk=case.lease.pk).update(is_active=False)
        elif failure == "claim":
            record = command._cohort_dispatches[case.task.pk].claim
            with transaction.atomic():
                hold_cohort_claim(
                    record.owner,
                    record.claim_id,
                    expected_identity=record.facts.identity,
                    expected_revision=record.revision,
                    reason=CohortHoldReason.DISPATCH_UNCERTAIN,
                    boundary=CohortHoldBoundary.CONTROL,
                    evidence_digest=record.facts_digest,
                    application_invoked=None,
                    now=case.now,
                )
        elif failure == "existing":
            RayTaskExecution.objects.filter(pk=case.task.pk).update(
                ray_job_id="retained-diagnostic", ray_address="local"
            )
        else:
            RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")

    core_sdk.before_remote = before_remote
    command._dispatch_cohort_task(claimed)
    _advance_core(command)
    assert len(core_sdk.calls) == 1
    assert (
        command._cohort_core_handles[case.task.pk] is core_sdk.runner._pending_tasks[case.task.pk]
    )
    case.task.refresh_from_db()
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.dispatched_at is not None
    if failure == "cancel":
        assert case.task.state == "CANCELLING"
        assert case.task.ray_job_id == "01000000:" + "a" * 48
        assert row.disposition == "HELD"
        assert row.hold_application_invoked is None
    else:
        assert case.task.ray_job_id == ("retained-diagnostic" if failure == "existing" else None)
        assert row.disposition == ("OPEN" if failure == "lease" else "HELD")
    # Retrying the original retained claim cannot issue another remote call.
    command._dispatch_cohort_task(claimed)
    assert len(core_sdk.calls) == 1
    if failure == "write":
        from tests.integration.test_cohort_completion import _result

        monkeypatch.setattr(RayTaskExecution, "save", original_save)
        retained = command._cohort_dispatches[case.task.pk]
        assert (
            command._apply_cohort_result(retained, _result(retained), provenance="owned_direct")
            == 1
        )
        case.task.refresh_from_db()
        assert case.task.state == "SUCCEEDED"
        assert case.task.ray_job_id is None


def test_cohort_loop_lost_connection_does_not_initialize_ray(case, command, core_sdk, monkeypatch):
    from tests.integration.test_cohort_dispatch import _claimed

    claimed = _claimed(case, "ray_core")
    core_sdk.initialized = False
    command.ray_core_runner = None
    command._cohort_controller = SimpleNamespace(
        check_configuration=lambda **_: None,
        tick=lambda: None,
        poll_stopped_cleanup=lambda: None,
        connected=True,
        reason=None,
    )
    events = []
    monkeypatch.setattr(command, "send_heartbeat", lambda: events.append("heartbeat"))
    monkeypatch.setattr(command, "_poll_cohort_completions", lambda: events.append("completion"))
    for method in (
        "_observe_cohort_retirement",
        "_finish_cohort_retirement_admission",
        "_expire_cohort_tasks",
        "_poll_cohort_timeouts",
        "_poll_cohort_cancellations",
    ):
        monkeypatch.setattr(command, method, lambda: 0)
    monkeypatch.setattr(command, "_recover_cohort_jobs", lambda _: 0)
    monkeypatch.setattr(
        command, "_claim_and_process_cohort_tasks", lambda _: command._dispatch_cohort_task(claimed)
    )
    monkeypatch.setattr(
        command, "_wait_for_poll_deadline", lambda _: setattr(command, "shutdown_requested", True)
    )
    command._run_cohort_loop(["default"], 1, 1)
    assert events == ["heartbeat", "completion"]
    assert command.ray_core_runner is not None
    assert not command.ray_core_runner._pending_tasks
    assert not core_sdk.calls
    row = RayTaskCohortClaim.objects.get(pk=claimed.claim.claim_id)
    assert row.disposition == "HELD" and row.dispatched_at is None


def test_real_sync_failure_resolves_old_claim_before_queueing_retry(case, command):
    command._dispatch_cohort_task(_claim_task(case, failing=True))
    case.task.refresh_from_db()
    assert case.task.state == "QUEUED", case.task.error_message
    assert case.task.error_message == "bounded application failure"
    assert case.task.attempt_number == 2
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"


def test_retry_pause_preserves_truthful_failure_and_resolves_claim(case, command):
    claimed = _claim_task(case, failing=True)
    maintenance.replace_maintenance_policy(
        (),
        pause_enqueues=True,
        pause_claims=False,
        expected_revision=1,
        actor="worker-test",
        reason="pause-retry",
        authorized=True,
    )
    command._dispatch_cohort_task(claimed)
    case.task.refresh_from_db()
    assert case.task.state == "FAILED", case.task.error_message
    assert case.task.error_message == "bounded application failure"
    assert case.task.attempt_number == 1
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"


@pytest.mark.parametrize("cancelling", [False, True])
def test_cohort_heartbeat_updates_only_exact_retained_live_claim(case, command, cancelling):
    from django_ray.runner.cohort_dispatch import PreparedCohortDispatch
    from tests.integration.test_cohort_completion import _started

    value: PreparedCohortDispatch = _started(case, "ray_core")
    command._remember_cohort_value(value)
    command._cohort_controller = SimpleNamespace()
    if cancelling:
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")
    case.now += timedelta(seconds=5)
    command.send_heartbeat()
    case.task.refresh_from_db()
    assert case.task.last_heartbeat_at == case.now
    assert not command.shutdown_requested
    # A local cache entry is not enough when this exact owner has gone stale.
    case.lease.is_active = False
    case.lease.save(update_fields=["is_active"])
    case.now += timedelta(seconds=1)
    command.send_heartbeat()
    case.task.refresh_from_db()
    assert case.task.last_heartbeat_at == case.now - timedelta(seconds=1)
    assert command.shutdown_requested


def test_cohort_heartbeat_does_not_overwrite_future_lease_evidence(case, command):
    command._cohort_controller = SimpleNamespace()
    future = case.now + timedelta(seconds=1)
    type(case.lease).objects.filter(pk=case.lease.pk).update(last_heartbeat_at=future)
    command.send_heartbeat()
    case.lease.refresh_from_db()
    assert case.lease.last_heartbeat_at == future
    assert command.shutdown_requested


@pytest.mark.parametrize("family", ["ray_core", "ray_job"])
def test_worker_timeout_holds_until_exact_terminal_then_fails_without_retry(
    case, command, monkeypatch, family
):
    from django_ray.models import RayTaskCohortTimeout, TaskAttempt
    from django_ray.runner import cohort_cancellation, cohort_timeout
    from tests.integration.test_cohort_completion import _started

    value = _started(case, family)
    command._cohort_controller = SimpleNamespace(family=CohortRunnerFamily(family))
    command._remember_cohort_value(value)
    monkeypatch.setattr(cohort_timeout, "_clock", lambda: case.now)
    monkeypatch.setattr(cohort_cancellation, "_clock", lambda: case.now)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(timeout_seconds=1)
    case.now += timedelta(seconds=2)
    assert command._poll_cohort_timeouts() == 1
    assert command._poll_cohort_timeouts() == 0
    case.task.refresh_from_db()
    assert (case.task.state, case.task.cancellation_status, case.task.attempt_number) == (
        "CANCELLING",
        None,
        1,
    )
    assert RayTaskCohortClaim.objects.get().disposition == "HELD"
    assert RayTaskCohortTimeout.objects.get().timeout_seconds == 1
    # Display and finalization use the retained deadline even if the mutable
    # task settings change while its stop outcome is still unknown.
    RayTaskExecution.objects.filter(pk=case.task.pk).update(timeout_seconds=99)
    retained = command._cohort_value(case.task.pk)
    assert (
        command._apply_cohort_cancelled(
            retained, "owned_core_terminal" if family == "ray_core" else "exact_jobs_stopped"
        )
        == 1
    )
    case.task.refresh_from_db()
    assert (case.task.state, case.task.attempt_number) == ("FAILED", 1)
    assert case.task.error_message == "Task timed out after 1 seconds"
    assert TaskAttempt.objects.get().state == "FAILED"
    assert RayTaskCohortClaim.objects.get().resolution_kind == "verified_cancelled"
    assert not command._cohort_dispatches


@pytest.mark.parametrize("success", [False, True])
def test_worker_authentic_completion_after_timeout_preserves_result_without_retry(
    case, command, monkeypatch, success
):
    from django_ray.runner import cohort_timeout
    from tests.integration.test_cohort_completion import _result, _started

    value = _started(case, "ray_core")
    command._cohort_controller = SimpleNamespace(family=CohortRunnerFamily.RAY_CORE)
    command._remember_cohort_value(value)
    monkeypatch.setattr(cohort_timeout, "_clock", lambda: case.now)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(timeout_seconds=1)
    case.now += timedelta(seconds=2)
    assert command._poll_cohort_timeouts() == 1
    retained = command._cohort_value(case.task.pk)
    assert (
        command._apply_cohort_result(
            retained, _result(value, success=success), provenance="owned_direct"
        )
        == 1
    )
    case.task.refresh_from_db()
    assert (case.task.state, case.task.attempt_number) == ("SUCCEEDED" if success else "FAILED", 1)
    assert RayTaskCohortClaim.objects.get().resolution_kind == "application_completed"
    assert not (case.task.error_message or "").startswith("Task timed out")


def test_owned_runtime_refusal_is_held_without_application_or_retry(case, command, monkeypatch):
    from django_ray.runtime import cohort_execution

    monkeypatch.setattr(
        cohort_execution, "verify_cohort_sync_runtime", lambda _: "package_mismatch"
    )
    command._dispatch_cohort_task(_claim_task(case))
    case.task.refresh_from_db()
    row = RayTaskCohortClaim.objects.get()
    assert (row.disposition, row.hold_reason, row.hold_application_invoked) == (
        "HELD",
        "package_mismatch",
        False,
    )
    assert case.task.state == "RUNNING" and case.task.result_data is None
    assert command.tasks_processed_count == 0


@pytest.mark.parametrize("failing", [False, True])
def test_authentic_completion_settles_cancelling_execution_without_retry(
    case, command, monkeypatch, failing
):
    from django_ray.runtime import cohort_execution

    original = cohort_execution.execute_cohort_request

    def cancelled_before_result(*args, **kwargs):
        result = original(*args, **kwargs)
        RayTaskExecution.objects.filter(pk=case.task.pk).update(state="CANCELLING")
        return result

    monkeypatch.setattr(cohort_execution, "execute_cohort_request", cancelled_before_result)
    command._dispatch_cohort_task(_claim_task(case, failing=failing))
    case.task.refresh_from_db()
    assert case.task.state == ("FAILED" if failing else "SUCCEEDED"), case.task.error_message
    assert case.task.attempt_number == 1
    assert RayTaskCohortClaim.objects.get().disposition == "RESOLVED"
    assert command.tasks_processed_count == 1
    if failing:
        assert case.task.error_message == "bounded application failure"
    else:
        assert json.loads(case.task.result_data) == 3


def test_cohort_loop_polls_completions_before_qualification_and_keeps_heartbeat(
    command, monkeypatch
):
    events = []
    clock = [0.0]
    command._cohort_controller = SimpleNamespace(
        check_configuration=lambda **_: events.append("config"),
        tick=lambda: events.append("qualification"),
        poll_stopped_cleanup=lambda: None,
        connected=False,
        reason=None,
    )
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(command, "send_heartbeat", lambda: events.append("heartbeat"))
    monkeypatch.setattr(command, "_poll_cohort_completions", lambda: events.append("completion"))
    monkeypatch.setattr(command, "_poll_cohort_timeouts", lambda: 0)
    monkeypatch.setattr(command, "_recover_cohort_jobs", lambda _: 0)
    monkeypatch.setattr(command, "_expire_cohort_tasks", lambda: 0)
    monkeypatch.setattr(command, "_poll_cohort_cancellations", lambda: 0)
    monkeypatch.setattr(
        command, "_claim_and_process_cohort_tasks", lambda _: events.append("claim")
    )

    def wait(_seconds):
        clock[0] += 1
        if clock[0] == 3:
            command.shutdown_requested = True

    monkeypatch.setattr(command, "_wait_for_poll_deadline", wait)
    command.run_loop(["default"], 1, 1.0)
    assert events == ["heartbeat", "config", "completion", "qualification", "claim"] * 3


def test_shutdown_retains_claim_history_and_releases_lease_if_cleanup_fails(case, command):
    claimed = _claim_task(case)
    value = cohort_dispatch.prepare_claimed_cohort_dispatch(claimed, now=case.now)
    value = cohort_dispatch.mark_cohort_dispatch_started(value, now=case.now)
    command._cohort_dispatches[case.task.pk] = value

    def failed_cleanup():
        raise RuntimeError("owned cleanup failed")

    command._cohort_controller = SimpleNamespace(invalidate=failed_cleanup)
    command.shutdown()
    row = RayTaskCohortClaim.objects.get()
    case.lease.refresh_from_db()
    case.task.refresh_from_db()
    assert row.disposition == "HELD" and row.hold_reason == "owner_lost"
    assert row.owner_lease_id == case.owner.worker_id
    assert case.task.claimed_by_worker == case.owner.worker_id
    assert case.task.state == "RUNNING" and not case.lease.is_active
    assert command.shutdown_exit_code == 1


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_worker_retirement_requires_actual_owned_cleanup_before_final_audit(
    case, command, monkeypatch, cleanup_fails
):
    monkeypatch.setattr(maintenance, "_clock", lambda: case.now)
    maintenance.request_worker_retirement(
        case.owner,
        expected_revision=0,
        actor="worker-test",
        reason="retire-worker",
        authorized=True,
    )

    def cleanup():
        if cleanup_fails:
            raise RuntimeError("cleanup unconfirmed")

    command._cohort_controller = SimpleNamespace(
        request_retirement=lambda: None,
        retirement_ready=True,
        invalidate=lambda: None,
        poll_stopped_cleanup=cleanup,
        lifecycle=None,
        adapter=None,
        connection=None,
        connection_ticket=None,
    )
    try:
        command._observe_cohort_retirement()
        command._finish_cohort_retirement_admission()
        assert command.shutdown_requested
        assert RayWorkerRetirement.objects.latest("revision").state == "REQUESTED"
        command.shutdown()
        assert RayWorkerRetirement.objects.latest("revision").state == (
            "REQUESTED" if cleanup_fails else "RETIRED"
        )
        case.lease.refresh_from_db()
        assert not case.lease.is_active
        assert command.shutdown_exit_code == (1 if cleanup_fails else None)
    finally:
        case.lease.delete()
