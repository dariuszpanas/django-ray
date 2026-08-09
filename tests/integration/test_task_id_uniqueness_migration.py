"""Migration coverage for globally unique Django task result identities."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from django_ray.models import RayTaskExecution

MIGRATE_FROM = [("django_ray", "0014_raytaskexecution_ray_target_address")]
MIGRATE_TO = [("django_ray", "0015_raytaskexecution_task_id_unique")]
LATEST = [("django_ray", "0020_legacy_open_rollback_fence")]


def _assert_task_id_uniqueness_migration_round_trip() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        duplicate_id = "task-id-uniqueness-migration-duplicate"
        first = old_execution.objects.create(
            task_id=duplicate_id,
            callable_path="testproject.tasks.add_numbers",
        )
        second = old_execution.objects.create(
            task_id=duplicate_id,
            callable_path="testproject.tasks.add_numbers",
        )

        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError) as caught:
            executor.migrate(MIGRATE_TO)

        message = str(caught.value)
        assert "cannot enforce unique task result IDs" in message
        assert "1 duplicate identity group(s)" in message
        assert f"first_pk={first.pk} rows=2" in message
        assert "no owner was selected and no rows were changed" in message
        assert duplicate_id not in message

        old_execution.objects.filter(pk=second.pk).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        task_id_field = new_execution._meta.get_field("task_id")

        assert task_id_field.unique is False
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor,
                new_execution._meta.db_table,
            )
        assert constraints["ray_task_id_unique"]["unique"] is True
        assert constraints["ray_task_id_unique"]["columns"] == ["task_id"]
        assert new_execution.objects.get(pk=first.pk).task_id == duplicate_id
        with pytest.raises(IntegrityError), transaction.atomic():
            new_execution.objects.create(
                task_id=duplicate_id,
                callable_path="testproject.tasks.add_numbers",
            )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        assert reverted_execution._meta.get_field("task_id").unique is False
        reverted_execution.objects.create(
            task_id=duplicate_id,
            callable_path="testproject.tasks.add_numbers",
        )
        assert reverted_execution.objects.filter(task_id=duplicate_id).count() == 2
        reverted_execution.objects.filter(task_id=duplicate_id).exclude(pk=first.pk).delete()
    finally:
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.django_db(transaction=True)
def test_task_id_uniqueness_migration_fails_closed_and_is_reversible() -> None:
    _assert_task_id_uniqueness_migration_round_trip()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_task_id_uniqueness_migration_uses_the_production_database() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_task_id_uniqueness_migration_round_trip()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_task_id_constraint_arbitrates_concurrent_writers() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    task_id = "postgresql-concurrent-task-id-collision"
    barrier = Barrier(2)

    def create_execution(index: int) -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            RayTaskExecution.objects.create(
                task_id=task_id,
                callable_path=f"testproject.tasks.concurrent_{index}",
            )
        except IntegrityError as error:
            diagnostics = getattr(error.__cause__, "diag", None)
            return str(getattr(diagnostics, "constraint_name", ""))
        finally:
            close_old_connections()
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=20)
            for future in (
                executor.submit(create_execution, 1),
                executor.submit(create_execution, 2),
            )
        ]

    assert sorted(outcomes) == ["created", "ray_task_id_unique"]
    assert RayTaskExecution.objects.filter(task_id=task_id).count() == 1
