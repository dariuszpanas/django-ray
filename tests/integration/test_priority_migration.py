"""Migration coverage for persisted Django task priority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor


def test_postgresql_constraint_uses_resumable_staged_sql() -> None:
    migration = import_module("django_ray.migrations.0008_raytaskexecution_priority_constraint")
    constraint_states = [None, (False,), (False,), (True,)]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _sql: str, _params: list[str]) -> None:
            self.result = constraint_states.pop(0)

        def fetchone(self):
            return self.result

    class Connection:
        vendor = "postgresql"

        def cursor(self):
            return Cursor()

    class SchemaEditor:
        connection = Connection()

        def __init__(self) -> None:
            self.statements: list[str] = []

        @staticmethod
        def quote_name(value: str) -> str:
            return f'"{value}"'

        def execute(self, sql: str) -> None:
            self.statements.append(sql)

    apps = SimpleNamespace(
        get_model=lambda *_args: SimpleNamespace(
            _meta=SimpleNamespace(db_table="django_ray_raytaskexecution")
        )
    )
    schema_editor = SchemaEditor()

    migration.add_priority_constraint(apps, schema_editor)
    migration.add_priority_constraint(apps, schema_editor)
    migration.validate_priority_constraint(apps, schema_editor)
    migration.validate_priority_constraint(apps, schema_editor)
    migration.remove_priority_constraint(apps, schema_editor)

    assert len(schema_editor.statements) == 3
    assert (
        'CHECK ("priority" >= -100 AND "priority" <= 100) NOT VALID'
        in (schema_editor.statements[0])
    )
    assert schema_editor.statements[1].endswith(
        'VALIDATE CONSTRAINT "ray_task_priority_valid_range"'
    )
    assert schema_editor.statements[2].endswith(
        'DROP CONSTRAINT IF EXISTS "ray_task_priority_valid_range"'
    )


def test_other_databases_add_priority_constraint_with_schema_editor() -> None:
    migration = import_module("django_ray.migrations.0008_raytaskexecution_priority_constraint")
    model = SimpleNamespace(_meta=SimpleNamespace(db_table="ray_task_execution"))
    apps = SimpleNamespace(get_model=lambda *_args: model)

    class SchemaEditor:
        connection = SimpleNamespace(vendor="mysql")

        def __init__(self) -> None:
            self.added: list[tuple[object, object]] = []

        def add_constraint(self, target: object, constraint: object) -> None:
            self.added.append((target, constraint))

    schema_editor = SchemaEditor()

    migration.add_priority_constraint(apps, schema_editor)

    assert len(schema_editor.added) == 1
    assert schema_editor.added[0][0] is model
    assert schema_editor.added[0][1].name == "ray_task_priority_valid_range"


def _assert_priority_migration_round_trip() -> None:
    migrate_from = [("django_ray", "0006_taskattempt")]
    migrate_to = [("django_ray", "0008_raytaskexecution_priority_constraint")]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        old_apps = executor.loader.project_state(migrate_from).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        created_at = datetime.now(UTC)
        old_execution.objects.create(
            task_id="priority-migration-older",
            callable_path="testproject.tasks.add_numbers",
            created_at=created_at - timedelta(seconds=1),
        )
        old_execution.objects.create(
            task_id="priority-migration-newer",
            callable_path="testproject.tasks.add_numbers",
            created_at=created_at,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        priority_field = new_execution._meta.get_field("priority")

        assert priority_field.default == 0
        assert priority_field.db_index is False
        assert {constraint.name for constraint in new_execution._meta.constraints} >= {
            "ray_task_priority_valid_range"
        }
        rows = list(
            new_execution.objects.filter(task_id__startswith="priority-migration-")
            .order_by("created_at", "pk")
            .values_list("task_id", "priority")
        )
        assert rows == [
            ("priority-migration-older", 0),
            ("priority-migration-newer", 0),
        ]
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor,
                new_execution._meta.db_table,
            )
        assert constraints["ray_task_priority_valid_range"]["check"] is True

        with pytest.raises(IntegrityError), transaction.atomic():
            new_execution.objects.create(
                task_id="priority-migration-invalid",
                callable_path="testproject.tasks.add_numbers",
                priority=101,
            )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        reverted_apps = executor.loader.project_state(migrate_from).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")

        assert "priority" not in {field.name for field in reverted_execution._meta.get_fields()}
        assert list(
            reverted_execution.objects.filter(task_id__startswith="priority-migration-")
            .order_by("created_at", "pk")
            .values_list("task_id", flat=True)
        ) == ["priority-migration-older", "priority-migration-newer"]
    finally:
        MigrationExecutor(connection).migrate([("django_ray", "0021_ray_job_request_reference")])


@pytest.mark.django_db(transaction=True)
def test_existing_executions_migrate_to_default_priority_without_reordering() -> None:
    _assert_priority_migration_round_trip()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_priority_migration_uses_the_production_constraint_path() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_priority_migration_round_trip()
