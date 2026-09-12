"""Current-cohort operations preserve inert history and pending cleanup."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from threading import Event
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, router, transaction

from django_ray import lifecycle, maintenance, protocol_status
from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION
from django_ray.lifecycle import TaskRetryRequestStatus
from django_ray.management.commands.django_ray_purge_inputs import Command as Purger
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayCohortJobCleanup,
    RayMaintenancePolicy,
    RayTaskCohortClaim,
    RayTaskCohortIntent,
    RayTaskExecution,
    RayTaskTargetBinding,
    TaskAttempt,
)
from tests.integration.test_cohort_activation_migration import LATEST, _migrate
from tests.integration.test_cohort_activation_migration import historical as historical
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
    _pause,
    _result,
    _started,
    _stored_request_reference,
)
from tests.integration.test_cohort_completion import (
    isolated_completion_controls as isolated_completion_controls,
)
from tests.unit.test_backends import _make_backend
from tests.unit.test_purge_inputs_command import _payload, _reference

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]


def _failed(case, family="sync"):
    assert EXECUTION_PROTOCOL_VERSION == 3
    value = _started(case, family)
    _apply(value, case, _result(value, success=False))
    case.task.refresh_from_db()
    return value


def _snapshot(task):
    return RayTaskExecution.objects.filter(pk=task.pk).values().get()


def _no_hydration(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("A refused retry must not hydrate RuntimeEnv or import a task callable")

    monkeypatch.setattr(lifecycle, "runtime_env_for_execution", refuse)
    monkeypatch.setattr(lifecycle, "_load_locked_execution_fields", refuse)


@pytest.mark.parametrize("metadata,protocol", [(0, 1), (1, 1), (1, 2)])
@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED"])
def test_old_terminal_results_remain_readable_and_retry_is_inert(
    historical, monkeypatch, metadata, protocol, state
):
    old_model = historical.get_model("django_ray", "RayTaskExecution")
    if metadata == 0:
        from django_ray.protocol_coordination import reopen_legacy_worker_admission

        policy = historical.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.get()
        reopen_legacy_worker_admission(expected_revision=policy.revision)
    old = old_model.objects.create(
        task_id=f"old-{metadata}-{protocol}-{state}",
        callable_path="removed_application.tasks.no_longer_importable",
        metadata_schema_version=metadata,
        execution_protocol_version=protocol,
        state=state,
        args_json="[2,3]",
        kwargs_json="{}",
        result_data="5" if state == "SUCCEEDED" else None,
        error_message=None if state == "SUCCEEDED" else "historical failure",
    )
    _migrate(LATEST)
    task = RayTaskExecution.objects.get(pk=old.pk)
    _no_hydration(monkeypatch)
    before = _snapshot(task)
    # Historical retry refusal does not depend on current maintenance policy.
    RayMaintenancePolicy.objects.all().delete()
    outcome = lifecycle.request_task_retry(task.pk)
    assert outcome.status is TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL
    result = _make_backend().get_result(task.task_id)
    assert result.args == [2, 3]
    if state == "SUCCEEDED":
        assert result.return_value == 5
    else:
        assert result.errors[0].traceback == "historical failure"
    assert _snapshot(task) == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.parametrize("family", ["sync", "ray_core", "ray_job"])
def test_current_retry_preserves_original_intent_queue_and_binding(case, family):
    if family == "ray_job":
        from tests.integration.test_cohort_job_cleanup import _close, _completed

        value, cleanup = _completed(case, success=False)
        _close(case, cleanup)
    else:
        value = _failed(case, family)
    before = _snapshot(case.task)
    intent = RayTaskCohortIntent.objects.values().get()
    binding = list(RayTaskTargetBinding.objects.values())
    claim = RayTaskCohortClaim.objects.values().get()

    def no_legacy_promotion(*args, **kwargs):
        pytest.fail("Current-cohort retry cannot promote a legacy mutable target")

    case.monkeypatch.setattr(lifecycle, "promote_legacy_ray_target", no_legacy_promotion)
    result = lifecycle.request_task_retry(case.task.pk)
    assert result.status is TaskRetryRequestStatus.ACCEPTED
    after = _snapshot(case.task)
    assert after["state"] == "QUEUED"
    assert after["attempt_number"] == before["attempt_number"] + 1
    assert after["execution_generation"] == before["execution_generation"] + 1
    assert after["queue_name"] == before["queue_name"]
    assert after["execution_protocol_version"] == 3
    assert RayTaskCohortIntent.objects.values().get() == intent
    assert list(RayTaskTargetBinding.objects.values()) == binding
    assert RayTaskCohortClaim.objects.values().get() == claim
    assert TaskAttempt.objects.get(execution_id=case.task.pk).execution_protocol_version == 3
    assert value.claim.facts.identity.execution_generation == before["execution_generation"]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("paused", TaskRetryRequestStatus.MAINTENANCE_PAUSED),
        ("claim-only", TaskRetryRequestStatus.MAINTENANCE_PAUSED),
        ("target", TaskRetryRequestStatus.MAINTENANCE_PAUSED),
        ("queue", TaskRetryRequestStatus.MAINTENANCE_PAUSED),
        ("protocol", TaskRetryRequestStatus.MAINTENANCE_PAUSED),
        ("missing", TaskRetryRequestStatus.MAINTENANCE_UNAVAILABLE),
        ("barrier-error", TaskRetryRequestStatus.MAINTENANCE_UNAVAILABLE),
        ("quarantine", TaskRetryRequestStatus.QUARANTINED),
        ("cleanup", TaskRetryRequestStatus.CLEANUP_PENDING),
    ],
)
def test_retry_blockers_are_bounded_before_hydration_and_leave_history(case, mode, expected):
    family = "ray_job" if mode == "cleanup" else "ray_core" if mode == "target" else "sync"
    value = _failed(case, family)
    if mode == "paused":
        _pause(enqueues=True)
    elif mode == "claim-only":
        _pause(claims=True)
    elif mode in {"queue", "protocol", "target"}:
        scope = maintenance.MaintenanceScope(
            mode,
            queue_name=case.task.queue_name if mode == "queue" else None,
            protocol_version=3 if mode == "protocol" else None,
            target_id=RayTaskTargetBinding.objects.get().target_policy.target_id
            if mode == "target"
            else None,
            pause_enqueues=mode != "target",
            pause_claims=mode == "target",
        )
        maintenance.replace_maintenance_policy(
            (scope,),
            pause_enqueues=False,
            pause_claims=False,
            expected_revision=maintenance.read_maintenance_policy().revision,
            actor="activation-test",
            reason="refuse-new-attempt",
            authorized=True,
        )
    elif mode == "missing":
        RayMaintenancePolicy.objects.all().delete()
    elif mode == "barrier-error":

        @contextmanager
        def unavailable(**kwargs):
            raise DatabaseError("untrusted database diagnostics")
            yield

        case.monkeypatch.setattr(maintenance, "maintenance_admission_barrier", unavailable)
    elif mode == "quarantine":
        maintenance.set_task_quarantine(
            value.claim.facts.identity,
            quarantined=True,
            expected_revision=0,
            actor="activation-test",
            reason="refuse-new-attempt",
            authorized=True,
        )
    before = _snapshot(case.task)
    claims = list(RayTaskCohortClaim.objects.values())
    _no_hydration(case.monkeypatch)
    result = lifecycle.request_task_retry(case.task.pk)
    assert result.status is expected
    assert _snapshot(case.task) == before
    assert list(RayTaskCohortClaim.objects.values()) == claims
    assert not TaskAttempt.objects.filter(execution=case.task).exists()
    # A failed barrier must not leave the connection in a broken transaction.
    with transaction.atomic():
        assert RayTaskExecution.objects.filter(pk=case.task.pk).exists()


def test_retry_acquires_maintenance_barrier_before_task_lock_even_inside_caller_transaction(case):
    _failed(case)
    original = lifecycle._locked_execution

    def checked(*args, **kwargs):
        maintenance.require_maintenance_admission_barrier()
        return original(*args, **kwargs)

    case.monkeypatch.setattr(lifecycle, "_locked_execution", checked)
    with transaction.atomic():
        result = lifecycle.request_task_retry(case.task.pk)
        assert result.accepted


def test_retry_preview_and_locked_checks_stay_on_write_database(case):
    _failed(case)

    class PrimaryReplica:
        def db_for_read(self, model, **hints):
            if model is RayTaskExecution:
                pytest.fail("Retry cannot preview an execution from a read replica")
            return "default"

        def db_for_write(self, model, **hints):
            return "default"

    with case.monkeypatch.context() as patch:
        patch.setattr(router, "routers", [PrimaryReplica()])
        assert lifecycle.request_task_retry(case.task.pk).accepted


def test_status_uses_current_closed_policy_without_claiming_runtime_serviceability(case):
    def no_remote(*args, **kwargs):
        pytest.fail("Protocol metadata diagnostics cannot import a task or contact Ray")

    case.monkeypatch.setattr("socket.create_connection", no_remote)
    case.monkeypatch.setattr("django.utils.module_loading.import_string", no_remote)
    report = protocol_status.build_protocol_status(observed_at=case.now)
    assert report.policy.active_write_protocol_version == 3
    assert not report.policy.legacy_worker_admission_enabled
    assert not report.policy.legacy_admission_token_present
    assert report.schema_version == 1
    assert report.queue_capacity_attested is False
    assert all(
        blocker.code is not protocol_status.ProtocolStatusBlockerCode.HISTORICAL_WRITE_POLICY
        for blocker in report.blockers
    )
    task = protocol_status.annotate_execution_protocol_availability(
        RayTaskExecution.objects.filter(pk=case.task.pk), observed_at=case.now
    ).get()
    assert task.protocol_compatible_worker_available is True
    output = StringIO()
    call_command("django_ray_protocol_status", "--json", stdout=output)
    payload = json.loads(output.getvalue())
    assert payload["policy"]["active_write_protocol_version"] == 3
    assert payload["queue_capacity_attested"] is False


def test_status_reports_historical_policy_as_pre_activation_metadata(historical):
    before = (
        historical.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.values().get()
    )
    report = protocol_status.build_protocol_status()
    payload = protocol_status.protocol_status_to_dict(report)
    assert payload["policy"]["active_write_protocol_version"] == 1
    assert payload["queue_capacity_attested"] is False
    assert {
        "code": "historical_write_policy",
        "scope": "package_execution_protocol_3",
        "count": 1,
    } in payload["blockers"]
    assert "package_execution_protocol_3" in protocol_status.render_protocol_status_text(report)
    assert (
        historical.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.values().get()
        == before
    )


@pytest.mark.parametrize("version", [2, 4])
def test_status_never_treats_other_policy_epochs_as_historical_read_support(historical, version):
    policies = historical.get_model("django_ray", "TaskExecutionProtocolPolicy").objects
    original = policies.get()
    try:
        policies.update(active_write_protocol_version=version)
        with pytest.raises(
            protocol_status.ProtocolStatusError, match="write version is unsupported"
        ):
            protocol_status.build_protocol_status()
    finally:
        policies.update(active_write_protocol_version=original.active_write_protocol_version)


def test_status_rejects_current_policy_with_legacy_admission_before_any_fallback(historical):
    policies = historical.get_model("django_ray", "TaskExecutionProtocolPolicy").objects
    original = policies.get()
    try:
        policies.update(active_write_protocol_version=3, legacy_worker_admission_enabled=True)
        with pytest.raises(
            protocol_status.ProtocolStatusError, match="legacy admission is incompatible"
        ):
            protocol_status.build_protocol_status()
    finally:
        policies.update(
            active_write_protocol_version=original.active_write_protocol_version,
            legacy_worker_admission_enabled=original.legacy_worker_admission_enabled,
        )


@pytest.mark.parametrize("reference_present", [True, False])
@pytest.mark.parametrize("delete", [True, False])
def test_open_cleanup_retains_original_or_unattributed_request_payload(
    case, reference_present, delete
):
    value = _started(case, "ray_job")
    reference = _stored_request_reference(value) if reference_present else _reference("e")
    _apply(value, case, _result(value, success=False))
    payload = _payload(reference, payload_kind=InputPayloadKind.RAY_JOB_REQUEST)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(
        ray_job_request_reference=None,
        finished_at=case.now - timedelta(days=60),
    )
    cleanup = RayCohortJobCleanup.objects.get()
    assert (cleanup.expectation_json is not None) is reference_present
    assert cleanup.state == "OPEN"
    output = StringIO()
    call_command("django_ray_purge_inputs", retention_days=30, delete=delete, stdout=output)
    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert "0 eligible, 0 purged, 0 failed" in output.getvalue()
    assert "Retained 1 payload(s) for pending Ray Job cleanup" in output.getvalue()
    assert reference not in output.getvalue()


def test_known_cleanup_does_not_retain_unrelated_payload(case):
    value = _started(case, "ray_job")
    _stored_request_reference(value)
    _apply(value, case)
    reference = _reference("f")
    payload = _payload(reference, payload_kind=InputPayloadKind.RAY_JOB_REQUEST)
    assert (
        Purger()._process_reference(reference, cutoff=payload.last_used_at, delete=False)
        == "eligible"
    )


@pytest.mark.parametrize("reference_present", [True, False])
def test_purge_cleanup_retention_never_consults_a_read_replica(case, reference_present):
    value = _started(case, "ray_job")
    reference = _stored_request_reference(value) if reference_present else _reference("e")
    _apply(value, case, _result(value, success=False))
    payload = _payload(reference, payload_kind=InputPayloadKind.RAY_JOB_REQUEST)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(ray_job_request_reference=None)

    class PrimaryReplica:
        def db_for_read(self, model, **hints):
            # A replica may not yet contain the committed OPEN obligation. None
            # of the command's retention decisions may be routed through it.
            pytest.fail(f"Purge consulted read routing for {model.__name__}")

        def db_for_write(self, model, **hints):
            return "default"

    output = StringIO()
    with case.monkeypatch.context() as patch:
        patch.setattr(router, "routers", [PrimaryReplica()])
        call_command("django_ray_purge_inputs", retention_days=30, delete=True, stdout=output)

    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert "Retained 1 payload(s) for pending Ray Job cleanup" in output.getvalue()


def test_uninspectable_cleanup_does_not_retain_unrelated_task_input(case):
    value = _started(case, "ray_job")
    _apply(value, case)
    reference = _reference("f")
    payload = _payload(reference)
    assert (
        Purger()._process_reference(reference, cutoff=payload.last_used_at, delete=False)
        == "eligible"
    )


def test_current_cleanup_retains_its_task_input(case):
    value = _started(case, "ray_job")
    _apply(value, case)
    reference = _reference("a")
    payload = _payload(reference)
    RayTaskExecution.objects.filter(pk=case.task.pk).update(
        input_reference=reference, finished_at=payload.last_used_at
    )
    assert (
        Purger()._process_reference(reference, cutoff=payload.last_used_at, delete=False)
        == "cleanup-pending"
    )


@pytest.mark.parametrize(
    "status",
    [
        TaskRetryRequestStatus.MAINTENANCE_PAUSED,
        TaskRetryRequestStatus.MAINTENANCE_UNAVAILABLE,
        TaskRetryRequestStatus.QUARANTINED,
        TaskRetryRequestStatus.CLEANUP_PENDING,
        TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL,
    ],
)
def test_retry_api_conflicts_offer_no_legacy_writer_fallback(monkeypatch, status):
    from testproject import api

    monkeypatch.setattr(api, "_NINJA_STATUS", None)
    response = api._retry_execution_outcome(
        lifecycle.TaskRetryRequestResult(status, 123, "FAILED", 1, 1), status_code=409
    )
    assert isinstance(response, tuple)
    assert response[0] == 409
    payload = response[1]
    assert isinstance(payload, dict)
    assert payload["code"] == status.value
    assert payload["message"] != "The retry request was not accepted."
    assert "Route this execution" not in payload["next_action"]
    assert len(json.dumps(payload).encode()) < 4096


def test_cancellation_api_offers_no_legacy_writer_fallback():
    from testproject import api

    result = lifecycle.TaskCancellationRequestResult(
        lifecycle.TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL, 123, "SUCCEEDED", 1, 1
    )
    response = api._cancellation_execution_outcome(SimpleNamespace(), result, status_code=409)
    assert response.status_code == 409
    payload = json.loads(response.content)
    assert "Route this execution" not in payload["next_action"]
    assert "Keep historical executions unchanged" in payload["next_action"]


def test_admin_retry_reports_admission_refusal_without_claiming_race(case, settings, admin_user):
    from django.template.response import TemplateResponse

    from tests.integration.test_admin import (
        _retry_confirmation_context,
        _retry_request,
        _task_admin,
    )

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    _failed(case)
    admin = _task_admin()
    queryset = RayTaskExecution.objects.filter(pk=case.task.pk)
    confirmation = admin.retry_tasks(_retry_request(user=admin_user), queryset)
    assert isinstance(confirmation, TemplateResponse)
    confirmation.render()
    context = _retry_confirmation_context(confirmation)
    _pause(enqueues=True)
    messages = []
    case.monkeypatch.setattr(
        admin, "message_user", lambda request, message: messages.append(message)
    )
    admin.retry_tasks(
        _retry_request(
            user=admin_user,
            data={"post": "yes", "retry_confirmation_token": context["confirmation_token"]},
        ),
        queryset,
    )
    assert len(messages) == 1
    assert "Queued 0 task(s)" in messages[0]
    assert "retry admission is paused or unavailable" in messages[0]
    assert "changed after confirmation" not in messages[0]


@pytest.mark.postgresql
def test_waiting_retry_observes_pause_before_taking_task_lock(case):
    """An independently committed pause wins over the earlier task preview."""
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL shared/exclusive admission locks")
    _failed(case)
    before = _snapshot(case.task)
    locked, release, retry_connected = Event(), Event(), Event()
    original_lock = maintenance._lock
    retry_pid = []

    def retained_pause(*, using, exclusive):
        result = original_lock(using=using, exclusive=exclusive)
        if exclusive:
            locked.set()
            assert release.wait(timeout=8), "Retry did not reach the admission barrier"
        return result

    case.monkeypatch.setattr(maintenance, "_lock", retained_pause)

    def worker(operation, *, identify=False):
        connection.close()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '8s'")
                cursor.execute("SET lock_timeout = '8s'")
                if identify:
                    cursor.execute("SELECT pg_backend_pid()")
                    retry_pid.append(cursor.fetchone()[0])
                    retry_connected.set()
            return operation()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        pause = executor.submit(worker, lambda: _pause(enqueues=True))
        retry = None
        try:
            assert locked.wait(timeout=5)
            retry = executor.submit(
                worker, lambda: lifecycle.request_task_retry(case.task.pk), identify=True
            )
            assert retry_connected.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE pid=%s AND locktype='advisory' AND NOT granted)",
                        [retry_pid[0]],
                    )
                    if cursor.fetchone()[0]:
                        break
                time.sleep(0.01)
            else:
                pytest.fail("Retry never waited on the independent maintenance writer")
            # A waiting retry must not already own the execution row.
            with transaction.atomic():
                RayTaskExecution.objects.select_for_update(nowait=True).get(pk=case.task.pk)
            release.set()
            pause.result(timeout=10)
            assert retry.result(timeout=10).status is TaskRetryRequestStatus.MAINTENANCE_PAUSED
        finally:
            release.set()
            pause.result(timeout=10)
            if retry is not None:
                retry.result(timeout=10)
    assert _snapshot(case.task) == before
    assert not TaskAttempt.objects.filter(execution=case.task).exists()
