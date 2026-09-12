"""Application receipt contracts on SQLite and the hosted PostgreSQL lane."""

from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection, models, router, transaction
from django.tasks import TaskResultStatus, task
from django.tasks.exceptions import TaskResultDoesNotExist

from django_ray.input_storage import InputPayloadError
from django_ray.models import RayTaskCohortIntent, RayTaskExecution, TaskInputPayload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=[3], autouse=True)
def enqueue_protocol(request, _restore_execution_protocol_rollout_seed):
    """Exercise the active producer against the real closed0035 admission policy."""
    from django_ray.backends import EXECUTION_PROTOCOL_VERSION
    from django_ray.models import TaskExecutionProtocolPolicy

    policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
    assert request.param == EXECUTION_PROTOCOL_VERSION == 3
    assert policy.active_write_protocol_version == 3
    assert not policy.legacy_worker_admission_enabled
    return request.param


def _assert_intents_match_executions(protocol):
    expected = (
        set(RayTaskExecution.objects.values_list("pk", flat=True)) if protocol == 3 else set()
    )
    assert set(RayTaskCohortIntent.objects.values_list("execution_id", flat=True)) == expected


def _application_task(value):
    raise AssertionError("receipt tests must never execute an application task")


class ApplicationReceipt(models.Model):
    application_key = models.CharField(max_length=64, unique=True)
    task_id = models.CharField(max_length=64, unique=True)
    backend_alias = models.CharField(max_length=64)

    class Meta:
        app_label = "receipt_contract_tests"


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def receipt_model(request, transactional_db):
    if connection.vendor != request.param:
        pytest.skip(f"requires {request.param}")
    with connection.schema_editor() as editor:
        editor.create_model(ApplicationReceipt)
    try:
        yield ApplicationReceipt
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(ApplicationReceipt)


def _enqueue_receipt(receipt_model, key, *, value=42, declared=None):
    declared = declared if declared is not None else task(_application_task)
    result = declared.enqueue(value)
    receipt = receipt_model.objects.using("default").create(
        application_key=key,
        task_id=result.id,
        backend_alias=result.backend,
    )
    return result, receipt


def _external_inputs(settings, tmp_path):
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }


class ReceiptFailureError(Exception):
    pass


def test_commit_persists_generated_id_and_receipt_on_one_connection(
    receipt_model, enqueue_protocol
):
    committed = []
    with transaction.atomic(using="default"):
        result, receipt = _enqueue_receipt(receipt_model, "commit")
        transaction.on_commit(lambda: committed.append(result.id), using="default")
        assert not committed
        assert result.status == TaskResultStatus.READY
        assert result.started_at is None
        assert receipt.task_id == result.id
        assert receipt.backend_alias == result.backend
        assert RayTaskExecution.objects.using("default").filter(task_id=result.id).exists()
    assert committed == [result.id]
    assert receipt_model.objects.using("default").get(application_key="commit").task_id == result.id
    _assert_intents_match_executions(enqueue_protocol)


def test_outer_rollback_removes_task_receipt_and_commit_callback(receipt_model):
    committed = []
    with pytest.raises(ReceiptFailureError), transaction.atomic(using="default"):
        result, _ = _enqueue_receipt(receipt_model, "rollback")
        transaction.on_commit(lambda: committed.append(result.id), using="default")
        raise ReceiptFailureError
    assert not committed
    assert not RayTaskExecution.objects.using("default").exists()
    assert not RayTaskCohortIntent.objects.exists()
    assert not receipt_model.objects.using("default").exists()
    # The earlier enqueue-time snapshot is still present, but its row is gone.
    assert result.status == TaskResultStatus.READY
    with pytest.raises(TaskResultDoesNotExist):
        result.refresh()


def test_nested_savepoint_failure_preserves_outer_receipts(receipt_model, enqueue_protocol):
    with transaction.atomic(using="default"):
        first, _ = _enqueue_receipt(receipt_model, "before")
        with pytest.raises(ReceiptFailureError), transaction.atomic(using="default"):
            lost, _ = _enqueue_receipt(receipt_model, "savepoint")
            raise ReceiptFailureError
        last, _ = _enqueue_receipt(receipt_model, "after")
    assert set(receipt_model.objects.values_list("task_id", flat=True)) == {first.id, last.id}
    assert set(RayTaskExecution.objects.values_list("task_id", flat=True)) == {first.id, last.id}
    assert not RayTaskExecution.objects.filter(task_id=lost.id).exists()
    _assert_intents_match_executions(enqueue_protocol)


def test_released_inner_savepoint_does_not_survive_outer_rollback(receipt_model):
    with pytest.raises(ReceiptFailureError), transaction.atomic(using="default"):
        with transaction.atomic(using="default"):
            _enqueue_receipt(receipt_model, "inner-success")
        raise ReceiptFailureError
    assert not RayTaskExecution.objects.exists()
    assert not receipt_model.objects.exists()


def test_enqueue_failure_rolls_back_application_receipt_work(receipt_model):
    with pytest.raises(InputPayloadError), transaction.atomic(using="default"):
        receipt_model.objects.create(
            application_key="pending", task_id="pending", backend_alias="default"
        )
        task(_application_task).enqueue(object())
    assert not RayTaskExecution.objects.exists()
    assert not receipt_model.objects.exists()


def test_receipt_constraint_failure_rolls_back_its_task_only(receipt_model):
    with transaction.atomic(using="default"):
        retained, _ = _enqueue_receipt(receipt_model, "same-domain-action")
    with pytest.raises(IntegrityError), transaction.atomic(using="default"):
        _enqueue_receipt(receipt_model, "same-domain-action")
    assert list(RayTaskExecution.objects.values_list("task_id", flat=True)) == [retained.id]
    assert list(receipt_model.objects.values_list("task_id", flat=True)) == [retained.id]


def test_task_backend_alias_does_not_select_a_database(receipt_model, settings):
    settings.TASKS = {
        **settings.TASKS,
        "receipt-lane": {"BACKEND": "django_ray.backends.RayTaskBackend", "QUEUES": ["default"]},
    }
    with transaction.atomic(using="default"):
        result, receipt = _enqueue_receipt(
            receipt_model,
            "alias",
            declared=task(_application_task, backend="receipt-lane"),
        )
    assert result.backend == receipt.backend_alias == "receipt-lane"
    assert RayTaskExecution.objects.using("default").get(task_id=result.id)._state.db == "default"


def test_external_input_object_survives_database_rollback(receipt_model, settings, tmp_path):
    _external_inputs(settings, tmp_path)
    with pytest.raises(ReceiptFailureError), transaction.atomic(using="default"):
        result, _ = _enqueue_receipt(receipt_model, "external")
        reference = RayTaskExecution.objects.get(task_id=result.id).input_reference
        assert reference
        assert TaskInputPayload.objects.filter(reference=reference).exists()
        raise ReceiptFailureError
    assert not RayTaskExecution.objects.exists()
    assert not receipt_model.objects.exists()
    assert not TaskInputPayload.objects.exists()
    assert not RayTaskCohortIntent.objects.exists()
    # The filesystem is not a transaction participant. This remains orphan evidence.
    assert len(list(tmp_path.rglob("*.json"))) == 1


@pytest.mark.parametrize("model", [RayTaskExecution, TaskInputPayload])
@pytest.mark.parametrize("operation", ["db_for_read", "db_for_write"])
def test_unsupported_routes_fail_before_payload_publication(
    receipt_model,
    monkeypatch,
    model,
    operation,
):
    declared = task(_application_task)
    monkeypatch.setattr(
        router,
        operation,
        lambda candidate, **hints: "private-other" if candidate is model else "default",
    )
    monkeypatch.setattr(
        "django_ray.backends.prepare_task_input",
        lambda *args, **kwargs: pytest.fail("routing must be rejected before publishing input"),
    )
    with pytest.raises(ImproperlyConfigured, match="default database") as caught:
        declared.enqueue(42)
    assert "private-other" not in str(caught.value)
    assert not RayTaskExecution.objects.using("default").exists()


def test_router_errors_are_contained_before_enqueue(receipt_model, monkeypatch):
    def broken_router(*args, **kwargs):
        raise RuntimeError("private-router-credential")

    monkeypatch.setattr(router, "db_for_write", broken_router)
    with pytest.raises(ImproperlyConfigured, match="default database") as caught:
        task(_application_task).enqueue(42)
    assert "private-router-credential" not in "".join(traceback.format_exception(caught.value))
    assert not RayTaskExecution.objects.using("default").exists()


@pytest.mark.parametrize("enqueue_protocol", [3], indirect=True)
@pytest.mark.parametrize("operation", ["db_for_read", "db_for_write"])
def test_cohort_intent_routes_fail_before_payload_publication(
    receipt_model, monkeypatch, operation
):
    monkeypatch.setattr(
        router,
        operation,
        lambda model, **hints: "private-other" if model is RayTaskCohortIntent else "default",
    )
    monkeypatch.setattr(
        "django_ray.backends.prepare_task_input",
        lambda *a, **k: pytest.fail("intent routing must precede external input"),
    )
    with pytest.raises(ImproperlyConfigured, match="default database"):
        task(_application_task).enqueue(42)
    assert (
        not RayTaskExecution.objects.using("default").exists()
        and not RayTaskCohortIntent.objects.using("default").exists()
    )


def test_validated_connection_pins_registry_updates_and_task_inserts(
    receipt_model,
    settings,
    tmp_path,
    monkeypatch,
):
    _external_inputs(settings, tmp_path)
    with transaction.atomic(using="default"):
        _enqueue_receipt(receipt_model, "initial")
    # An instance-dependent router must not redirect a reused input registry row.
    # The transaction binding also wins over a later no-hints routing decision.
    observed = {}

    def changing_router(model, **hints):
        key = model._meta.label_lower
        observed[key] = observed.get(key, 0) + 1
        return "default" if observed[key] == 1 and not hints else "unconfigured-other"

    monkeypatch.setattr(router, "db_for_write", changing_router)
    with transaction.atomic(using="default"):
        result, receipt = _enqueue_receipt(receipt_model, "reused")
    assert receipt._state.db == "default"
    assert RayTaskExecution.objects.using("default").filter(task_id=result.id).exists()
    assert TaskInputPayload.objects.using("default").count() == 1


@pytest.mark.postgresql
@pytest.mark.parametrize("commit", [False, True])
def test_postgresql_observer_sees_task_and_receipt_only_after_outer_commit(
    transactional_db,
    commit,
    record_property,
    enqueue_protocol,
):
    if connection.vendor != "postgresql":
        pytest.skip("requires postgresql")
    with connection.schema_editor() as editor:
        editor.create_model(ApplicationReceipt)
    published = Event()
    release = Event()
    identity = {}

    def writer():
        connection.close()
        try:
            try:
                with transaction.atomic(using="default"):
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                        cursor.execute("SELECT pg_backend_pid()")
                        identity["writer_pid"] = cursor.fetchone()[0]
                    result, _ = _enqueue_receipt(ApplicationReceipt, "visibility")
                    identity["task_id"] = result.id
                    published.set()
                    if not release.wait(timeout=10):
                        raise TimeoutError("observer did not release writer")
                    if not commit:
                        raise ReceiptFailureError
            except ReceiptFailureError:
                pass
        finally:
            connection.close()

    def observe():
        task_table = connection.ops.quote_name(RayTaskExecution._meta.db_table)
        receipt_table = connection.ops.quote_name(ApplicationReceipt._meta.db_table)
        intent_table = connection.ops.quote_name(RayTaskCohortIntent._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT (SELECT COUNT(*) FROM {task_table} WHERE task_id = %s), "
                f"(SELECT COUNT(*) FROM {receipt_table} WHERE task_id = %s), "
                f"(SELECT COUNT(*) FROM {intent_table} i JOIN {task_table} t "
                "ON i.execution_id = t.id WHERE t.task_id = %s), pg_backend_pid()",
                [identity["task_id"], identity["task_id"], identity["task_id"]],
            )
            return cursor.fetchone()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(writer)
            try:
                assert published.wait(timeout=10), "writer did not publish its uncommitted identity"
                before_task, before_receipt, before_intent, observer_pid = observe()
                assert observer_pid != identity["writer_pid"]
                assert (before_task, before_receipt, before_intent) == (0, 0, 0)
            finally:
                release.set()
            future.result(timeout=15)
        after_task, after_receipt, after_intent, _ = observe()
        assert (after_task, after_receipt) == ((1, 1) if commit else (0, 0))
        assert after_intent == int(commit and enqueue_protocol == 3)
        record_property("writer_pid", identity["writer_pid"])
        record_property("observer_pid", observer_pid)
        record_property("before", [before_task, before_receipt, before_intent])
        record_property("after", [after_task, after_receipt, after_intent])
    finally:
        release.set()
        with connection.schema_editor() as editor:
            editor.delete_model(ApplicationReceipt)
