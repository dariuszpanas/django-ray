"""Stopped-writer activation preserves history and rejects incompatible writers."""

from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from django_ray.execution_protocol import (
    EXECUTION_PROTOCOL_VERSION,
    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
)
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskWorkerLease,
)
from django_ray.protocol_coordination import (
    ProtocolPolicyStateError,
    close_legacy_worker_admission,
    reopen_legacy_worker_admission,
)
from tests.integration.test_cohort_claim_storage import _claim, _hold
from tests.integration.test_cohort_claim_storage import case as case
from tests.integration.test_cohort_claim_storage import (
    isolated_sqlite_ledger_maintenance as isolated_sqlite_ledger_maintenance,
)
from tests.integration.test_cohort_claim_storage import ledger_database as ledger_database
from tests.integration.test_cohort_job_cleanup import _close, _completed

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("ledger_database")]
BEFORE = [("django_ray", "0034_cohort_timeouts")]
LATEST = [("django_ray", "0035_activate_current_cohort")]
MIGRATION = import_module("django_ray.migrations.0035_activate_current_cohort")


def _migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    return executor.loader.project_state(target).apps


@pytest.fixture
def historical():
    old = _migrate(BEFORE)
    try:
        yield old
    finally:
        # Only rows created by this stopped migration fixture are removed. No
        # product transition converts or discards unsupported queued work.
        old.get_model("django_ray", "RayTaskExecution").objects.all().delete()
        old.get_model("django_ray", "TaskWorkerLease").objects.all().delete()
        policy = old.get_model("django_ray", "TaskExecutionProtocolPolicy")
        if not policy.objects.exists():
            policy.objects.create(
                singleton_key=1,
                schema_version=1,
                active_write_protocol_version=1,
                legacy_worker_admission_enabled=False,
                revision=1,
            )
        _migrate(LATEST)


def _execution(model=RayTaskExecution, **changes):
    return model.objects.create(
        **{"task_id": "activation-task", "callable_path": "testproject.tasks.add_numbers"} | changes
    )


def _lease(model=TaskWorkerLease, **changes):
    return model.objects.create(
        **{
            "worker_id": "activation-worker",
            "hostname": "host",
            "pid": 1,
            "capability_schema_version": 1,
            "django_ray_version": "0.5.0",
            "min_supported_execution_protocol_version": 3,
            "max_supported_execution_protocol_version": 3,
            "legacy_admission_token_id": None,
        }
        | changes
    )


def _refused(callback):
    with pytest.raises(DatabaseError), transaction.atomic():
        callback()


def test_fresh_graph_and_current_model_defaults_select_exact_current_cohort():
    assert EXECUTION_PROTOCOL_VERSION == 3
    assert MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION == MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION == 3
    policy = TaskExecutionProtocolPolicy.objects.get()
    assert policy.active_write_protocol_version == 3
    assert not policy.legacy_worker_admission_enabled
    assert not LegacyWorkerAdmissionToken.objects.exists()
    assert (
        RayTaskExecution().execution_protocol_version
        == TaskAttempt().execution_protocol_version
        == 3
    )
    assert _execution().execution_protocol_version == 3
    assert _lease().min_supported_execution_protocol_version == 3
    call_command("makemigrations", "django_ray", check=True, dry_run=True, verbosity=0)


@pytest.mark.parametrize("protocol", [1, 2, 4])
@pytest.mark.parametrize("state", ["QUEUED", "SUCCEEDED"])
def test_unsupported_new_execution_is_refused_even_if_it_claims_to_be_history(protocol, state):
    _refused(lambda: _execution(execution_protocol_version=protocol, state=state))
    assert not RayTaskExecution.objects.exists()


def test_legacy_metadata_default_is_not_upgraded_into_current_authority():
    _refused(lambda: _execution(metadata_schema_version=0))
    table = connection.ops.quote_name(RayTaskExecution._meta.db_table)
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute(f"PRAGMA table_info({table})")
            defaults = {row[1]: row[4] for row in cursor.fetchall()}
        else:
            cursor.execute(
                "SELECT column_name,column_default FROM information_schema.columns "
                "WHERE table_name=%s",
                [RayTaskExecution._meta.db_table],
            )
            defaults = dict(cursor.fetchall())
    assert str(defaults["metadata_schema_version"]).startswith("0")
    assert str(defaults["execution_protocol_version"]).startswith("3")


@pytest.mark.parametrize("minimum,maximum", [(1, 1), (1, 3), (3, 4), (4, 4)])
def test_old_or_broad_active_lease_insert_and_reactivation_are_refused(minimum, maximum):
    values = {
        "min_supported_execution_protocol_version": minimum,
        "max_supported_execution_protocol_version": maximum,
    }
    _refused(lambda: _lease(**values))
    row = _lease(is_active=False, **values)
    _refused(lambda: TaskWorkerLease.objects.filter(pk=row.pk).update(is_active=True))
    row.refresh_from_db()
    assert not row.is_active


@pytest.mark.parametrize(
    "field,value",
    [
        ("active_write_protocol_version", 1),
        ("active_write_protocol_version", 2),
        ("legacy_worker_admission_enabled", True),
        ("revision", 90),
        ("updated_at", timezone.now()),
    ],
)
def test_fixed_policy_cannot_be_mutated_or_reopened(field, value):
    before = TaskExecutionProtocolPolicy.objects.values().get()
    _refused(lambda: TaskExecutionProtocolPolicy.objects.update(**{field: value}))
    assert TaskExecutionProtocolPolicy.objects.values().get() == before


def test_legacy_service_and_token_recreation_are_inert_after_activation():
    revision = TaskExecutionProtocolPolicy.objects.get().revision
    for operation in (
        lambda: close_legacy_worker_admission(
            expected_revision=revision, legacy_producers_retired=True
        ),
        lambda: reopen_legacy_worker_admission(expected_revision=revision),
    ):
        with pytest.raises(ProtocolPolicyStateError, match="requires active write protocol v1"):
            operation()
    _refused(lambda: LegacyWorkerAdmissionToken.objects.create(singleton_key=1))
    assert not LegacyWorkerAdmissionToken.objects.exists()


def test_missing_policy_fails_closed_and_cannot_be_replaced_by_legacy_policy():
    TaskExecutionProtocolPolicy.objects.all().delete()
    _refused(_execution)
    _refused(_lease)
    _refused(lambda: TaskExecutionProtocolPolicy.objects.create(active_write_protocol_version=1))
    TaskExecutionProtocolPolicy.objects.create()
    assert _execution().execution_protocol_version == 3


@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "CANCELLING"])
def test_migration_refuses_unsupported_nonterminal_without_altering_row_or_policy(
    historical, protocol, state
):
    task = historical.get_model("django_ray", "RayTaskExecution")
    policy = historical.get_model("django_ray", "TaskExecutionProtocolPolicy")
    row = _execution(task, execution_protocol_version=protocol, state=state)
    before = task.objects.values().get()
    policy_before = policy.objects.values().get()
    with pytest.raises(RuntimeError, match="unsupported nonterminal"):
        _migrate(LATEST)
    assert task.objects.values().get() == before
    assert policy.objects.values().get() == policy_before
    row.delete()


@pytest.mark.parametrize("minimum,maximum", [(1, 1), (1, 3), (3, 4)])
def test_migration_refuses_active_unsupported_advertisement_without_retiring_it(
    historical, minimum, maximum
):
    lease = historical.get_model("django_ray", "TaskWorkerLease")
    row = _lease(
        lease,
        min_supported_execution_protocol_version=minimum,
        max_supported_execution_protocol_version=maximum,
    )
    before = lease.objects.values().get()
    with pytest.raises(RuntimeError, match="unsupported active worker"):
        _migrate(LATEST)
    assert lease.objects.values().get() == before
    row.delete()


def test_migration_preserves_historical_rows_and_existing_trigger_definitions(historical):
    task = historical.get_model("django_ray", "RayTaskExecution")
    attempt = historical.get_model("django_ray", "TaskAttempt")
    rows = []
    for protocol in (1, 2):
        row = _execution(
            task,
            task_id=f"history-{protocol}",
            execution_protocol_version=protocol,
            state="SUCCEEDED",
            result_data='{"retained": true}',
        )
        rows.append(row.pk)
        attempt.objects.create(
            execution_id=row.pk,
            attempt_number=1,
            execution_protocol_version=protocol,
            state="SUCCEEDED",
        )
    before = list(task.objects.values().order_by("pk"))
    before_attempts = list(attempt.objects.values().order_by("pk"))
    triggers = {}
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
            triggers = dict(cursor.fetchall())
    previous_revision = (
        historical.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.get().revision
    )
    _migrate(LATEST)
    assert list(task.objects.values().order_by("pk")) == before
    assert list(attempt.objects.values().order_by("pk")) == before_attempts
    assert TaskExecutionProtocolPolicy.objects.get().revision == previous_revision + 1
    if triggers:
        with connection.cursor() as cursor:
            cursor.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
            after = dict(cursor.fetchall())
        assert all(after[name] == sql for name, sql in triggers.items())
    for pk in rows:
        _refused(lambda pk=pk: task.objects.filter(pk=pk).update(state="QUEUED"))
        _refused(lambda pk=pk: task.objects.filter(pk=pk).update(execution_generation=1))
        task.objects.filter(pk=pk).update(result_data='{"readable": true}')
    task.objects.all().delete()


def test_empty_reverse_stays_closed_and_refuses_retained_current_history():
    row = _execution(state="SUCCEEDED")
    with pytest.raises(RuntimeError, match="retained cohort history"):
        _migrate(BEFORE)
    row.delete()
    try:
        old = _migrate(BEFORE)
        policy = old.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.get()
        assert (
            policy.active_write_protocol_version == 1 and not policy.legacy_worker_admission_enabled
        )
        assert not LegacyWorkerAdmissionToken.objects.exists()
    finally:
        _migrate(LATEST)


def test_unsupported_database_refuses_before_any_activation_sql():
    statements = []
    editor = SimpleNamespace(
        connection=SimpleNamespace(vendor="mysql"),
        quote_name=lambda name: name,
        execute=statements.append,
    )
    with pytest.raises(RuntimeError, match="SQLite or PostgreSQL"):
        MIGRATION._lock(apps, editor)
    assert not statements


@pytest.mark.parametrize("held", [False, True])
def test_activation_precondition_refuses_real_unresolved_claim_without_rewriting_it(case, held):
    from django_ray.models import RayTaskCohortClaim

    record = _claim(case)
    if held:
        _hold(case, record)
    before = RayTaskCohortClaim.objects.values().get()
    with pytest.raises(RuntimeError, match="unresolved cohort claims"):
        MIGRATION._pending(apps, SimpleNamespace(connection=connection))
    assert RayTaskCohortClaim.objects.values().get() == before


@pytest.mark.parametrize("inspectable", [True, False])
def test_authentic_terminal_result_is_insufficient_until_exact_jobs_cleanup_closes(
    case, inspectable
):
    from django_ray.models import RayCohortJobCleanup

    _value, record = _completed(case, reference=inspectable)
    before = RayCohortJobCleanup.objects.values().get()
    with pytest.raises(RuntimeError, match="pending Jobs cleanup"):
        MIGRATION._pending(apps, SimpleNamespace(connection=connection))
    assert RayCohortJobCleanup.objects.values().get() == before
    if inspectable:
        _close(case, record)
        MIGRATION._pending(apps, SimpleNamespace(connection=connection))


def test_open_v1_policy_closes_and_detaches_only_inactive_legacy_token_refs(historical):
    policy_model = historical.get_model("django_ray", "TaskExecutionProtocolPolicy")
    token_model = historical.get_model("django_ray", "LegacyWorkerAdmissionToken")
    lease_model = historical.get_model("django_ray", "TaskWorkerLease")
    token_model.objects.create(singleton_key=1)
    policy_model.objects.update(legacy_worker_admission_enabled=True)
    lease = lease_model.objects.create(worker_id="legacy", hostname="host", pid=1, is_active=False)
    previous = policy_model.objects.get().revision
    _migrate(LATEST)
    lease.refresh_from_db()
    policy = policy_model.objects.get()
    assert not lease.is_active and lease.legacy_admission_token_id is None
    assert policy.revision == previous + 1 and policy.active_write_protocol_version == 3
    assert not policy.legacy_worker_admission_enabled and not token_model.objects.exists()


def test_installation_failure_rolls_back_policy_defaults_and_preserved_triggers(
    historical, monkeypatch
):
    before = (
        historical.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.values().get()
    )

    def failed(*args, **kwargs):
        raise RuntimeError("injected schema failure")

    with monkeypatch.context() as patch:
        patch.setattr(MIGRATION, "_trigger", failed)
        with pytest.raises(RuntimeError, match="injected schema failure"):
            _migrate(LATEST)
    assert TaskExecutionProtocolPolicy.objects.values().get() == before
    assert not MigrationExecutor(connection).loader.applied_migrations.get(
        ("django_ray", LATEST[0][1])
    )
    # The exact same graph can be successfully applied after the transaction
    # rolls back; this also detects missing restored trigger definitions.
    _migrate(LATEST)


@pytest.mark.parametrize("corruption", ["missing", "wrong_epoch", "exhausted_revision"])
def test_migration_refuses_missing_or_unsupported_policy_before_any_relabel(historical, corruption):
    policy = historical.get_model("django_ray", "TaskExecutionProtocolPolicy")
    if corruption == "missing":
        policy.objects.all().delete()
    else:
        fields = (
            {"active_write_protocol_version": 2}
            if corruption == "wrong_epoch"
            else {"revision": (1 << 63) - 1}
        )
        policy.objects.update(**fields)
    before = list(policy.objects.values())
    try:
        with pytest.raises(RuntimeError, match="one coherent protocol policy"):
            _migrate(LATEST)
        assert list(policy.objects.values()) == before
    finally:
        if corruption != "missing":
            policy.objects.update(active_write_protocol_version=1, revision=1)


@pytest.mark.parametrize("ledger_database", [pytest.param("sqlite", id="sqlite")], indirect=True)
def test_sqlite_raw_fractional_policy_revision_cannot_create_a_new_admission_latch():
    TaskExecutionProtocolPolicy.objects.all().delete()
    table = connection.ops.quote_name(TaskExecutionProtocolPolicy._meta.db_table)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (singleton_key,schema_version,active_write_protocol_version,"
            "legacy_worker_admission_enabled,revision,updated_at) VALUES (1,1,3,0,1.5,%s)",
            [timezone.now()],
        )
    TaskExecutionProtocolPolicy.objects.create()
