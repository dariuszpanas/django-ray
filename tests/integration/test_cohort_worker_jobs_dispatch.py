"""Compose the worker, Jobs runner and durable request store with a fake SDK."""

import hashlib
import time
import zipfile
from contextlib import contextmanager
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.backends.utils import CursorWrapper
from django.db.models.query import QuerySet

from django_ray import ray_job_request_storage as request_storage
from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.models import (
    RayTaskCohortClaim,
    RayTaskExecution,
    TaskInputPayload,
    TaskWorkerLease,
)
from django_ray.ray_job_request_storage import _load_ray_job_request
from django_ray.runner.ray_job import RayJobRunner
from django_ray.runtime.runtime_env import normalize_runtime_env
from tests.integration.test_cohort_claim_storage import _hold
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    closed_legacy_admission as closed_legacy_admission,
)
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_dispatch import _claimed
from tests.integration.test_cohort_worker import command as command
from tests.unit.test_ray_job import FakeJobClient

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _finish_initial_preparation(command):
    """Wait only in the harness, then let the parent commit the fixed result."""
    for ticket in tuple(command._cohort_preparation_tickets.values()):
        operation = command._cohort_preparation._current(ticket)
        if operation.thread is not None:
            operation.thread.join(3)
            assert not operation.thread.is_alive(), "Initial preparation callback did not exit"
    command._poll_cohort_preparations()


@pytest.fixture
def staged(case, command, settings, tmp_path, monkeypatch):
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "requests"),
    }
    network = []
    cleanup_gate = Event()
    cleanup_gate.set()
    prepare = RayJobRunner._prepare_cohort_submission

    @contextmanager
    def owned_snapshot(runner, source):
        with prepare(runner, source) as value:
            try:
                yield value
            finally:
                assert cleanup_gate.wait(3), "owned fixture snapshot cleanup did not exit"

    monkeypatch.setattr(RayJobRunner, "_prepare_cohort_submission", owned_snapshot)

    def forbidden_client(*args, **kwargs):
        network.append(True)
        raise AssertionError("unapproved SDK call")

    monkeypatch.setattr(RayJobRunner, "_get_client", forbidden_client)
    command._dispatch_cohort_task(_claimed(case, "ray_job"))
    _finish_initial_preparation(command)
    controller = command._cohort_job_submission
    ticket = command._cohort_job_submission_tickets[case.task.pk]
    try:
        deadline = time.monotonic() + 3
        while controller.poll(ticket).stage != "prepared":
            assert time.monotonic() < deadline, "request preparation did not finish"
            time.sleep(0.005)
        yield SimpleNamespace(
            controller=controller,
            ticket=ticket,
            operation=controller._owned(ticket),
            dispatch=command._cohort_dispatches[case.task.pk],
            network=network,
            cleanup_gate=cleanup_gate,
        )
    finally:
        cleanup_gate.set()
        if id(ticket) in controller._operations:
            controller.abort(ticket)
        for operation in tuple(controller._operations.values()):
            if operation.thread is not None:
                operation.thread.join(3)
                assert not operation.thread.is_alive()


@pytest.mark.parametrize("change", ["inactive-lease", "reincarnated-lease", "held-claim"])
def test_final_attachment_rechecks_ownership_after_existing_registry_lock(
    case, command, staged, monkeypatch, change
):
    operation = staged.operation
    current = command._cohort_submission_task(staged.dispatch, staged.ticket)
    operation.runner._attach_cohort_submission(
        operation.staged, task_execution=current, expected_claim=staged.dispatch.claim
    )
    current.refresh_from_db()
    original_reference = current.ray_job_request_reference
    get_or_create = QuerySet.get_or_create
    registry_waits = []

    def changed_after_registry_wait(query, *args, **kwargs):
        result = get_or_create(query, *args, **kwargs)
        if query.model is TaskInputPayload:
            assert connection.in_atomic_block
            assert not result[1] and result[0].reference == original_reference
            registry_waits.append(True)
            if change == "held-claim":
                _hold(case, staged.dispatch.claim)
            else:
                fields = (
                    {"is_active": False, "stopped_at": case.now}
                    if change == "inactive-lease"
                    else {"pid": case.lease.pid + 1}
                )
                TaskWorkerLease.objects.filter(pk=case.lease.pk).update(**fields)
        return result

    monkeypatch.setattr(QuerySet, "get_or_create", changed_after_registry_wait)
    # The worker's earlier read passes. Only the check after the registry wait
    # can reject this same-reference attachment before waking the callback.
    command._poll_cohort_submissions()
    assert registry_waits == [True]
    assert not operation.authorized
    assert operation.uncertainty == "attachment_unconfirmed"
    assert staged.network == []
    current.refresh_from_db()
    assert current.ray_job_request_reference == original_reference
    assert current.ray_job_id == staged.ticket.submission_id
    assert current.ray_address == staged.ticket.jobs_endpoint
    assert current.state == "RUNNING" and current.completion_data is None
    assert RayTaskCohortClaim.objects.get(pk=staged.dispatch.claim.claim_id).disposition == "HELD"


def test_staged_submission_refuses_purged_registry_without_storage_restore(
    case, command, staged, monkeypatch
):
    prepared = staged.operation.staged.stored_request
    TaskInputPayload.objects.create(
        reference=prepared.reference,
        payload_kind="ray_job_request",
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state="PURGED",
    )
    restored = []

    def forbidden_restore(*args, **kwargs):
        restored.append(True)
        raise AssertionError("staged attachment attempted storage restoration")

    monkeypatch.setattr(request_storage, "_restore_purged_request", forbidden_restore)
    command._poll_cohort_submissions()
    assert restored == staged.network == []
    assert not staged.operation.authorized
    assert TaskInputPayload.objects.get(reference=prepared.reference).state == "PURGED"
    case.task.refresh_from_db()
    assert case.task.ray_job_request_reference is None
    assert case.task.ray_job_id == staged.ticket.submission_id


def test_staged_submission_manual_transaction_cannot_attach_or_start(command, staged, monkeypatch):
    from django_ray.runner.cohort_job_submission import CohortJobSubmissionError

    current = command._cohort_submission_task(staged.dispatch, staged.ticket)
    connection.set_autocommit(False)
    try:
        with pytest.raises(CohortJobSubmissionError):
            staged.controller.authorize(staged.ticket, task_execution=current)
        with pytest.raises(CohortJobSubmissionError):
            staged.operation.runner._attach_cohort_submission(
                staged.operation.staged,
                task_execution=current,
                expected_claim=staged.dispatch.claim,
            )
        with pytest.raises(CohortJobSubmissionError):
            staged.controller.begin(
                current,
                prepared=staged.dispatch.prepared,
                handle=staged.operation.source.handle(),
                claim=staged.dispatch.claim,
            )
        assert not staged.operation.authorized
        assert staged.network == []
    finally:
        connection.rollback()
        connection.set_autocommit(True)


def test_terminal_sql_and_verified_remote_cleanup_do_not_release_local_snapshot_capacity(
    case, command, staged, monkeypatch
):
    from django_ray.models import RayCohortJobCleanup
    from django_ray.runner import cohort_job_execution_control as control
    from django_ray.target import cohort_job_cleanup as cleanup
    from tests.integration.test_cohort_completion import _apply

    staged.cleanup_gate.clear()
    client = FakeJobClient()
    monkeypatch.setattr(RayJobRunner, "_get_client", lambda *_args: client)
    current = command._cohort_submission_task(staged.dispatch, staged.ticket)
    assert staged.controller.authorize(staged.ticket, task_execution=current)
    deadline = time.monotonic() + 3
    while not client.submissions:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert staged.controller.busy
    assert command._owned_cohort_cleanup_count() == 0  # RUNNING already consumes capacity.
    monkeypatch.setattr(cleanup, "_clock", lambda: case.now)
    _apply(staged.dispatch, case)
    command._retire_cohort_execution(case.task.pk)
    record = cleanup.cleanup_record(RayCohortJobCleanup.objects.get())
    assert command._owned_cohort_cleanup_count() == 1  # One remote/local identity.

    # A separate owned control callback corroborates the terminal driver. It
    # cannot close the still-running submission callback or its snapshot.
    monkeypatch.setattr(
        control,
        "inspect_cohort_job_execution",
        lambda expected: control.CohortJobExecutionInspection(
            expected, "SUCCEEDED", True, "01000000"
        ),
    )
    inspector = control.CohortJobExecutionController()
    ticket = inspector.begin_inspection(record.expectation)
    try:
        deadline = time.monotonic() + 3
        while inspector.busy:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        result = inspector.poll(ticket)
        assert result is not None and result.uncertainty is None
        cleanup.close_cohort_job_cleanup(
            case.owner,
            record,
            inspection=result.inspection,
            inspection_began_at=case.now,
            observed_at=case.now,
        )
    finally:
        for operation in inspector._operations.values():
            if operation.thread is not None:
                operation.thread.join(3)
                assert not operation.thread.is_alive()
    assert RayCohortJobCleanup.objects.get().state == "CLOSED"
    assert command._owned_cohort_cleanup_count() == 1
    assert staged.controller.busy and case.task.pk in command._cohort_job_submission_tickets
    staged.cleanup_gate.set()
    deadline = time.monotonic() + 3
    while staged.controller.busy:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    command._poll_cohort_submissions()
    assert command._owned_cohort_cleanup_count() == 0
    assert not command._cohort_job_submission_tickets


@pytest.mark.parametrize("acknowledgement", ["accepted", "lost", "wrong-id"])
def test_worker_jobs_dispatch_registers_exact_request_before_sdk_and_retains_uncertainty(
    case, command, settings, tmp_path, monkeypatch, acknowledgement
):
    source = tmp_path / "application.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("owned_marker.py", "VALUE = 1\n")
    runtime = normalize_runtime_env(
        {"working_dir": str(source), "env_vars": {"OWNED_MARKER": "fixture"}},
        profile="retained-job-profile",
    )
    RayTaskExecution.objects.filter(pk=case.task.pk).update(
        runtime_env_json=runtime.serialized,
        runtime_env_hash=runtime.digest,
        runtime_env_profile=runtime.profile,
    )
    case.task.refresh_from_db()
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RAY_ADDRESS": "ray://unrelated.invalid:10001",
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path / "requests"),
    }
    monkeypatch.setenv("RAY_API_SERVER_ADDRESS", "https://unrelated.invalid:8265")
    claimed = _claimed(case, "ray_job")
    endpoint = claimed.claim.facts.job_qualification.jobs_endpoint
    calls = []
    attached = []
    parent_thread = get_ident()
    execute = CursorWrapper.execute

    def parent_only_execute(cursor, *args, **kwargs):
        assert get_ident() == parent_thread, "submission callback attempted ORM access"
        return execute(cursor, *args, **kwargs)

    monkeypatch.setattr(CursorWrapper, "execute", parent_only_execute)

    def registered():
        assert not connection.in_atomic_block
        retained = command._cohort_dispatches[case.task.pk]
        row = RayTaskExecution.objects.get(pk=case.task.pk)
        claim = RayTaskCohortClaim.objects.get(pk=retained.claim.claim_id)
        assert claim.dispatched_at == case.now
        assert claim.prepared_request_digest == retained.prepared.request_digest
        assert claim.disposition == "OPEN"
        assert row.ray_address == endpoint
        assert row.ray_job_id == command._cohort_job_handles[row.pk].ray_job_id
        payload = TaskInputPayload.objects.get(reference=row.ray_job_request_reference)
        assert payload.payload_kind == "ray_job_request"
        assert payload.state == "ACTIVE"
        assert payload.digest == hashlib.sha256(retained.prepared.request_json.encode()).hexdigest()
        return retained, row

    class Client(FakeJobClient):
        def _upload_working_dir_if_needed(self, runtime_env):
            assert attached
            calls.append("upload-working-dir")
            super()._upload_working_dir_if_needed(runtime_env)

        def _upload_py_modules_if_needed(self, runtime_env):
            assert attached
            calls.append("upload-modules")
            super()._upload_py_modules_if_needed(runtime_env)

        def submit_job(self, **kwargs):
            retained, row = attached[0]
            calls.append("submit")
            locator = kwargs["entrypoint"].removeprefix(
                "python -m django_ray.runtime.cohort_entrypoint --request-ref-b64 "
            )
            loaded = _load_ray_job_request(
                locator,
                expected_identity=retained.prepared.identity,
                expected_execution_protocol_version=3,
                supported_protocols=ExecutionProtocolRange(3, 3),
            )
            assert loaded.serialized_request == retained.prepared.request_json
            assert loaded.request.compiled_graph_submission_transport == "ray-job"
            assert loaded.request.runtime_env_profile == "retained-job-profile"
            assert loaded.request.runtime_env_hash == runtime.digest
            assert kwargs["submission_id"] == row.ray_job_id
            metadata = kwargs["metadata"]
            assert metadata["cohort_jobs_endpoint"] == endpoint
            assert metadata["cohort_request_digest"] == retained.prepared.request_digest
            assert metadata["cohort_contract_digest"] == retained.prepared.contract_digest
            submitted = kwargs["runtime_env"]
            assert submitted["working_dir"].startswith("gcs://_ray_pkg_")
            assert submitted["env_vars"] == {"OWNED_MARKER": "fixture"}
            assert metadata["cohort_submitted_runtime_env_digest"] == (
                "sha256:" + normalize_runtime_env(submitted).digest
            )
            result = super().submit_job(**kwargs)
            if acknowledgement == "lost":
                raise TimeoutError("fixed simulated lost response")
            return result if acknowledgement == "accepted" else "different-submission"

    client = Client()

    def get_client(runner, address=None):
        assert type(runner) is RayJobRunner
        assert attached and get_ident() != parent_thread
        assert address == endpoint
        calls.append("client")
        return client

    monkeypatch.setattr(RayJobRunner, "_get_client", get_client)
    original_attach = RayJobRunner._attach_cohort_submission

    def attach(runner, staged, *, task_execution, expected_claim):
        assert get_ident() == parent_thread
        original_attach(
            runner, staged, task_execution=task_execution, expected_claim=expected_claim
        )
        attached.append(registered())

    monkeypatch.setattr(RayJobRunner, "_attach_cohort_submission", attach)
    command._dispatch_cohort_task(claimed)
    _finish_initial_preparation(command)
    try:
        deadline = time.monotonic() + 3
        while command._cohort_job_submission_tickets:
            assert time.monotonic() < deadline, "owned submission did not finish"
            command._poll_cohort_submissions()
            time.sleep(0.005)
    finally:
        controller = command._cohort_job_submission
        for operation in tuple(controller._operations.values()):
            controller.abort(operation.ticket)
            if operation.thread is not None:
                operation.thread.join(3)
                assert not operation.thread.is_alive()

    assert calls == ["client", "upload-working-dir", "upload-modules", "submit"]
    assert len(client.submissions) == 1
    case.task.refresh_from_db()
    retained = command._cohort_dispatches[case.task.pk]
    claim = RayTaskCohortClaim.objects.get(pk=retained.claim.claim_id)
    assert claim.disposition == ("OPEN" if acknowledgement == "accepted" else "HELD")
    assert case.task.state == "RUNNING"
    assert case.task.completion_data is None
    assert case.task.ray_address == endpoint
    assert case.task.ray_job_request_reference
    assert case.task.ray_job_id == command._cohort_job_handles[case.task.pk].ray_job_id
    assert claim.hold_application_invoked is None
    command._dispatch_cohort_task(claimed)
    assert len(client.submissions) == 1
    assert calls == ["client", "upload-working-dir", "upload-modules", "submit"]
