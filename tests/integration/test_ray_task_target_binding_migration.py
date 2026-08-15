"""Migration and database-fence coverage for dormant task target bindings."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    models,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTaskExecution,
    RayTaskTargetBinding,
)
from django_ray.target_attestation import (
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayRunnerFamily,
)

MIGRATE_FROM = [("django_ray", "0022_ray_target_persistence")]
MIGRATE_TO = [("django_ray", "0023_ray_task_target_binding")]
LATEST = MIGRATE_TO

_DIGEST = f"sha256:{'a' * 64}"
_POSTGRESQL_TRIGGER = "ray_tbinding_guard_0023"
_SQLITE_TRIGGERS = {
    "ray_tbinding_insert_0023",
    "ray_tbinding_update_0023",
}


class _RecordingSchemaEditor:
    def __init__(self, vendor: str) -> None:
        self.connection = SimpleNamespace(vendor=vendor, alias="default")
        self.statements: list[str] = []

    @staticmethod
    def quote_name(name: str) -> str:
        return f'"{name}"'

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class _ExistingRows:
    def using(self, alias: str) -> _ExistingRows:
        del alias
        return self

    @staticmethod
    def exists() -> bool:
        return True


class _RollbackGuardApps:
    @staticmethod
    def get_model(app_label: str, model_name: str) -> object:
        assert app_label == "django_ray"
        assert model_name == "RayTaskTargetBinding"
        return SimpleNamespace(
            _meta=SimpleNamespace(db_table="django_ray_raytasktargetbinding"),
            objects=_ExistingRows(),
        )


def _create_target_and_policy(
    *,
    target_key: str = "binding-primary",
    policy_revision: int = 1,
) -> tuple[RayTarget, RayTargetPolicyRevision]:
    target = RayTarget.objects.create(
        target_key=target_key,
        runner_family=RayRunnerFamily.RAY_CORE,
        cluster_session=f"session_{target_key}",
        ray_major=2,
        ray_minor=56,
        ray_patch=0,
        python_implementation="cpython",
        python_major=3,
        python_minor=12,
        python_patch=12,
    )
    policy = RayTargetPolicyRevision.objects.create(
        target=target,
        revision=policy_revision,
        desired_state=RayTargetDesiredState.DRAINING,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json='{"schema":"expectation"}',
        expectation_digest=_DIGEST,
    )
    return target, policy


def _create_execution(*, task_id: str = "task-target-binding") -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
    )


def _database_trigger_names() -> set[str]:
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        elif connection.vendor == "postgresql":
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        else:
            raise AssertionError(f"unsupported database vendor: {connection.vendor}")
        return {str(row[0]) for row in cursor.fetchall()}


def _clear_binding_tables() -> None:
    table_names = set(connection.introspection.table_names())
    if RayTaskTargetBinding._meta.db_table in table_names:
        RayTaskTargetBinding.objects.all().delete()
    RayTaskExecution.objects.all().delete()
    RayTargetAttestationRevision.objects.all().delete()
    RayTargetPolicyRevision.objects.all().delete()
    RayTarget.objects.all().delete()


def _raw_insert(model: type[models.Model], values: dict[str, object]) -> None:
    quote = connection.ops.quote_name
    columns = ", ".join(quote(column) for column in values)
    placeholders = ", ".join(["%s"] * len(values))
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {quote(model._meta.db_table)} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )


def _raw_delete_by_pk(model: type[models.Model], value: object) -> None:
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {quote(model._meta.db_table)} WHERE {quote(model._meta.pk.column)} = %s",
            [value],
        )


def _rejects_database_error(
    operation: Callable[[], object],
    error_type: type[DatabaseError],
) -> None:
    with pytest.raises(error_type), transaction.atomic():
        operation()


def _close_owned_thread_connection() -> None:
    thread_connection = connections["default"]
    raw_connection = thread_connection.connection
    if raw_connection is None:
        return
    try:
        raw_connection.rollback()
    finally:
        raw_connection.close()
        thread_connection.connection = None


def test_model_exposes_only_the_dormant_task_target_binding_contract() -> None:
    assert [field.name for field in RayTaskTargetBinding._meta.fields] == [
        "execution",
        "target_policy",
        "schema_version",
        "created_at",
    ]
    execution = RayTaskTargetBinding._meta.get_field("execution")
    target_policy = RayTaskTargetBinding._meta.get_field("target_policy")
    schema_version = RayTaskTargetBinding._meta.get_field("schema_version")
    created_at = RayTaskTargetBinding._meta.get_field("created_at")

    assert isinstance(execution, models.OneToOneField)
    assert execution.primary_key is True
    assert execution.remote_field.model is RayTaskExecution
    assert execution.remote_field.on_delete is models.PROTECT
    assert execution.remote_field.related_name == "ray_target_binding"
    assert isinstance(target_policy, models.ForeignKey)
    assert target_policy.remote_field.model is RayTargetPolicyRevision
    assert target_policy.remote_field.on_delete is models.PROTECT
    assert target_policy.remote_field.related_name == "task_target_bindings"
    assert schema_version.default == 1
    assert schema_version.db_default == 1
    assert schema_version.editable is False
    assert created_at.editable is False
    assert {constraint.name for constraint in RayTaskTargetBinding._meta.constraints} == {
        "ray_tbinding_schema_valid"
    }
    assert (
        str(RayTaskTargetBinding(execution_id=17, target_policy_id=23))
        == "execution 17 target policy 23"
    )


def test_migration_fence_sql_is_reversible_typed_and_fail_closed() -> None:
    migration = importlib.import_module("django_ray.migrations.0023_ray_task_target_binding")
    postgresql = _RecordingSchemaEditor("postgresql")

    migration._install_task_target_binding_fences(django_apps, postgresql)
    installed_sql = "\n".join(postgresql.statements)
    assert "CREATE FUNCTION django_ray_guard_tbinding_0023()" in installed_sql
    assert f"CREATE TRIGGER {_POSTGRESQL_TRIGGER}" in installed_sql
    assert "django-ray Ray task target binding is invalid" in installed_sql
    assert "django-ray Ray task target binding is immutable" in installed_sql
    assert "NEW.schema_version <> 1" in installed_sql
    assert "TG_OP = 'UPDATE' AND NEW IS DISTINCT FROM OLD" in installed_sql

    postgresql.statements.clear()
    migration._remove_task_target_binding_fences(django_apps, postgresql)
    removed_sql = "\n".join(postgresql.statements)
    assert f'DROP TRIGGER IF EXISTS "{_POSTGRESQL_TRIGGER}" ON' in removed_sql
    assert 'DROP FUNCTION IF EXISTS "django_ray_guard_tbinding_0023"()' in removed_sql

    sqlite = _RecordingSchemaEditor("sqlite")
    migration._install_task_target_binding_fences(django_apps, sqlite)
    sqlite_sql = "\n".join(sqlite.statements)
    for trigger in _SQLITE_TRIGGERS:
        assert f"CREATE TRIGGER {trigger}" in sqlite_sql
    assert "typeof(NEW.execution_id) != 'integer'" in sqlite_sql
    assert "typeof(NEW.target_policy_id) != 'integer'" in sqlite_sql
    assert "typeof(NEW.schema_version) != 'integer'" in sqlite_sql
    assert "strftime('%Y-%m-%d %H:%M:%S'" in sqlite_sql
    assert "NEW.execution_id IS NOT OLD.execution_id" in sqlite_sql
    assert "NEW.target_policy_id IS NOT OLD.target_policy_id" in sqlite_sql

    sqlite.statements.clear()
    migration._remove_task_target_binding_fences(django_apps, sqlite)
    removed_sql = "\n".join(sqlite.statements)
    for trigger in _SQLITE_TRIGGERS:
        assert f'DROP TRIGGER IF EXISTS "{trigger}"' in removed_sql

    unsupported = _RecordingSchemaEditor("mysql")
    message = "Ray task target binding supports only SQLite and PostgreSQL"
    with pytest.raises(RuntimeError, match=message):
        migration._install_task_target_binding_fences(django_apps, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._remove_task_target_binding_fences(django_apps, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._guard_empty_task_target_binding(_RollbackGuardApps(), unsupported)
    assert unsupported.statements == []

    operations = migration.Migration.operations
    assert operations[-1].reverse_code is migration._guard_empty_task_target_binding
    assert operations[-2].code is migration._install_task_target_binding_fences

    postgresql.statements.clear()
    with pytest.raises(RuntimeError, match="rollback requires the binding table to be empty"):
        migration._guard_empty_task_target_binding(_RollbackGuardApps(), postgresql)
    assert postgresql.statements == [
        'LOCK TABLE "django_ray_raytasktargetbinding" IN ACCESS EXCLUSIVE MODE'
    ]


def _assert_database_constraints_and_fences() -> None:
    expected_triggers = _SQLITE_TRIGGERS if connection.vendor == "sqlite" else {_POSTGRESQL_TRIGGER}
    assert expected_triggers <= _database_trigger_names()

    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor,
            RayTaskTargetBinding._meta.db_table,
        )
    assert constraints["ray_tbinding_schema_valid"]["check"] is True
    assert sum(bool(details["primary_key"]) for details in constraints.values()) == 1
    foreign_keys = {
        details["columns"][0]: details["foreign_key"]
        for details in constraints.values()
        if details["foreign_key"] is not None
    }
    assert foreign_keys == {
        "execution_id": (RayTaskExecution._meta.db_table, "id"),
        "target_policy_id": (RayTargetPolicyRevision._meta.db_table, "id"),
    }

    target, policy = _create_target_and_policy()
    execution = _create_execution()
    binding = RayTaskTargetBinding.objects.create(
        execution=execution,
        target_policy=policy,
    )
    assert binding.pk == execution.pk
    assert binding.schema_version == 1

    with pytest.raises(IntegrityError), transaction.atomic():
        RayTaskTargetBinding.objects.create(
            execution=execution,
            target_policy=policy,
        )

    other_execution = _create_execution(task_id="task-target-binding-other")
    other_binding = RayTaskTargetBinding.objects.create(
        execution=other_execution,
        target_policy=policy,
    )
    assert other_binding.target_policy_id == policy.pk

    assert (
        RayTaskTargetBinding.objects.filter(pk=binding.pk).update(
            execution_id=models.F("execution_id"),
            target_policy_id=models.F("target_policy_id"),
            schema_version=models.F("schema_version"),
            created_at=models.F("created_at"),
        )
        == 1
    )
    _, other_policy = _create_target_and_policy(target_key="binding-secondary")
    spare_execution = _create_execution(task_id="task-target-binding-spare")
    _rejects_database_error(
        lambda: RayTaskTargetBinding.objects.filter(pk=binding.pk).update(
            execution_id=spare_execution.pk
        ),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: RayTaskTargetBinding.objects.filter(pk=binding.pk).update(
            target_policy=other_policy
        ),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: RayTaskTargetBinding.objects.filter(pk=binding.pk).update(
            created_at=binding.created_at + timedelta(seconds=1)
        ),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: RayTaskTargetBinding.objects.filter(pk=binding.pk).update(schema_version=2),
        IntegrityError,
    )

    with pytest.raises(ProtectedError):
        execution.delete()
    with pytest.raises(ProtectedError):
        policy.delete()
    _rejects_database_error(
        lambda: _raw_delete_by_pk(RayTaskExecution, execution.pk),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: _raw_delete_by_pk(RayTargetPolicyRevision, policy.pk),
        IntegrityError,
    )

    binding.delete()
    execution.delete()
    other_binding.delete()
    other_execution.delete()
    spare_execution.delete()
    policy.delete()
    target.delete()
    other_policy.delete()
    RayTarget.objects.filter(target_key="binding-secondary").delete()


@pytest.mark.django_db(transaction=True)
def test_sqlite_task_target_binding_constraints_and_fences() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_database_constraints_and_fences()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_task_target_binding_constraints_and_fences() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_database_constraints_and_fences()


def _assert_raw_storage_guards(
    error_type: type[DatabaseError],
    *,
    sqlite_dynamic_types: bool,
) -> None:
    _target, policy = _create_target_and_policy(target_key="binding-raw")
    execution = _create_execution(task_id="task-target-binding-raw")
    now = timezone.now()
    stored_now = now.astimezone(UTC).replace(tzinfo=None) if connection.vendor == "sqlite" else now
    valid: dict[str, object] = {
        "execution_id": execution.pk,
        "target_policy_id": policy.pk,
        "schema_version": 1,
        "created_at": stored_now,
    }
    mutations: list[tuple[str, object]] = [
        ("execution_id", 0),
        ("execution_id", "1.5"),
        ("execution_id", b"1"),
        ("target_policy_id", 0),
        ("target_policy_id", "1.5"),
        ("target_policy_id", b"1"),
        ("schema_version", 0),
        ("schema_version", 2),
        ("schema_version", "1.5"),
        ("schema_version", b"1"),
        ("created_at", None),
        ("created_at", "infinity"),
        ("created_at", "2026-08-15 20:00:00\x00suffix"),
        ("created_at", b"2026-08-15 20:00:00"),
        ("created_at", 2.5),
    ]
    if sqlite_dynamic_types:
        mutations.extend(
            (
                ("execution_id", 1.5),
                ("target_policy_id", 1.5),
                ("schema_version", 1.5),
            )
        )
        mutations.extend(
            ("created_at", value)
            for value in (
                "now",
                "0000-01-01 00:00:00",
                "2026-01-01 24:00:00",
                "2026-02-30 20:00:00",
                "2026-08-15T20:00:00",
                "2026-08-15 20:00:00.12345",
            )
        )

    for field, value in mutations:
        invalid = {**valid, field: value}
        _rejects_database_error(
            lambda invalid=invalid: _raw_insert(RayTaskTargetBinding, invalid),
            error_type,
        )
        assert RayTaskTargetBinding.objects.count() == 0

    _raw_insert(RayTaskTargetBinding, valid)
    stored = RayTaskTargetBinding.objects.get(pk=execution.pk)
    assert stored.target_policy_id == policy.pk
    assert stored.schema_version == 1
    stored.delete()


@pytest.mark.django_db(transaction=True)
def test_sqlite_raw_binding_storage_rejects_wrong_types_and_datetimes() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_raw_storage_guards(IntegrityError, sqlite_dynamic_types=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_raw_binding_storage_rejects_applicable_invalid_values() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_raw_storage_guards(DatabaseError, sqlite_dynamic_types=False)


def _assert_migration_round_trip_and_reverse_guard() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        old_target = old_apps.get_model("django_ray", "RayTarget")
        old_policy = old_apps.get_model("django_ray", "RayTargetPolicyRevision")
        existing = old_execution.objects.create(
            task_id="binding-before-migration",
            callable_path="testproject.tasks.add_numbers",
        )
        target = old_target.objects.create(
            target_key="binding-round-trip",
            runner_family="ray_core",
            cluster_session="session_binding_round_trip",
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
        policy = old_policy.objects.create(
            target=target,
            revision=1,
            desired_state="draining",
            expectation_schema_version=1,
            expectation_json='{"schema":"expectation"}',
            expectation_digest=_DIGEST,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        binding_model = new_apps.get_model("django_ray", "RayTaskTargetBinding")
        assert binding_model.objects.count() == 0
        assert old_execution.objects.filter(pk=existing.pk).exists()

        rolling = old_execution.objects.create(
            task_id="binding-old-writer",
            callable_path="testproject.tasks.add_numbers",
        )
        assert binding_model.objects.count() == 0
        assert old_execution.objects.filter(pk=rolling.pk).exists()

        binding_model.objects.create(execution_id=existing.pk, target_policy_id=policy.pk)
        with pytest.raises(
            RuntimeError,
            match="rollback requires the binding table to be empty",
        ):
            MigrationExecutor(connection).migrate(MIGRATE_FROM)
        assert binding_model.objects.filter(pk=existing.pk).exists()
        assert binding_model._meta.db_table in connection.introspection.table_names()
        expected_triggers = (
            _SQLITE_TRIGGERS if connection.vendor == "sqlite" else {_POSTGRESQL_TRIGGER}
        )
        assert expected_triggers <= _database_trigger_names()

        binding_model.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        with pytest.raises(LookupError):
            reverted_apps.get_model("django_ray", "RayTaskTargetBinding")
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        assert reverted_execution.objects.filter(pk__in=(existing.pk, rolling.pk)).count() == 2
    finally:
        table_names = set(connection.introspection.table_names())
        if RayTaskTargetBinding._meta.db_table in table_names:
            RayTaskTargetBinding.objects.all().delete()
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.django_db(transaction=True)
def test_sqlite_binding_is_additive_unseeded_old_writer_safe_and_reverse_guarded() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_migration_round_trip_and_reverse_guard()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_binding_is_additive_unseeded_old_writer_safe_and_reverse_guarded() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_migration_round_trip_and_reverse_guard()


def _create_held_binding(writer_inserted: Event, release_writer: Event) -> None:
    close_old_connections()
    try:
        with transaction.atomic():
            execution = RayTaskExecution.objects.get(task_id="binding-concurrent-writer")
            policy = RayTargetPolicyRevision.objects.get(target_id="binding-concurrent")
            RayTaskTargetBinding.objects.create(
                execution=execution,
                target_policy=policy,
            )
            writer_inserted.set()
            if not release_writer.wait(timeout=20):
                raise TimeoutError("test did not release the binding writer")
    finally:
        _close_owned_thread_connection()


def _attempt_sqlite_reverse_with_zero_busy_timeout(reverse_started: Event) -> str:
    close_old_connections()
    try:
        thread_connection = connections["default"]
        with thread_connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout = 0")
        reverse_started.set()
        try:
            MigrationExecutor(thread_connection).migrate(MIGRATE_FROM)
        except OperationalError:
            return "writer-locked"
        except RuntimeError:
            return "history-refused"
        raise AssertionError("rollback unexpectedly removed task target binding")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
def test_sqlite_active_binding_writer_cannot_partially_reverse_schema() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")

    _create_target_and_policy(target_key="binding-concurrent")
    _create_execution(task_id="binding-concurrent-writer")
    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(_create_held_binding, writer_inserted, release_writer)
            assert writer_inserted.wait(timeout=10)
            reverse = executor.submit(
                _attempt_sqlite_reverse_with_zero_busy_timeout,
                reverse_started,
            )
            assert reverse_started.wait(timeout=10)
            try:
                outcome = reverse.result(timeout=10)
            finally:
                release_writer.set()
            writer.result(timeout=20)

        assert outcome in {"writer-locked", "history-refused"}
        execution = RayTaskExecution.objects.get(task_id="binding-concurrent-writer")
        assert RayTaskTargetBinding.objects.filter(pk=execution.pk).exists()
        assert RayTaskTargetBinding._meta.db_table in connection.introspection.table_names()
        assert _SQLITE_TRIGGERS <= _database_trigger_names()
    finally:
        release_writer.set()
        _clear_binding_tables()
        MigrationExecutor(connection).migrate(LATEST)


def _postgresql_backend_pid() -> int:
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _wait_for_postgresql_lock(backend_pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                [backend_pid],
            )
            row = cursor.fetchone()
        if row is not None and row[0] == "Lock":
            return
        time.sleep(0.05)
    raise TimeoutError("rollback did not wait on the binding writer lock")


def _attempt_postgresql_reverse(
    reverse_started: Event,
    backend_pid: list[int],
) -> str:
    close_old_connections()
    try:
        thread_connection = connections["default"]
        backend_pid.append(_postgresql_backend_pid())
        reverse_started.set()
        try:
            MigrationExecutor(thread_connection).migrate(MIGRATE_FROM)
        except RuntimeError:
            return "history-refused"
        raise AssertionError("rollback unexpectedly removed task target binding")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_binding_writer_serializes_before_reverse_guard() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _create_target_and_policy(target_key="binding-concurrent")
    _create_execution(task_id="binding-concurrent-writer")
    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    backend_pid: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(_create_held_binding, writer_inserted, release_writer)
            assert writer_inserted.wait(timeout=10)
            reverse = executor.submit(
                _attempt_postgresql_reverse,
                reverse_started,
                backend_pid,
            )
            assert reverse_started.wait(timeout=10)
            try:
                _wait_for_postgresql_lock(backend_pid[0])
            finally:
                release_writer.set()
            writer.result(timeout=20)
            assert reverse.result(timeout=20) == "history-refused"

        execution = RayTaskExecution.objects.get(task_id="binding-concurrent-writer")
        assert RayTaskTargetBinding.objects.filter(pk=execution.pk).exists()
        assert RayTaskTargetBinding._meta.db_table in connection.introspection.table_names()
        assert {_POSTGRESQL_TRIGGER} <= _database_trigger_names()
    finally:
        release_writer.set()
        _clear_binding_tables()
        MigrationExecutor(connection).migrate(LATEST)
