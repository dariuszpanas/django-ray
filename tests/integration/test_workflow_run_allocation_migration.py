"""Migration coverage for database-authoritative workflow-run allocation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.context import DurableTaskContext, WorkflowRunIdentity
from django_ray.workflow_progress import (
    _workflow_run_id,
    allocate_workflow_run,
    reclaim_workflow_run,
)

MIGRATE_FROM = [("django_ray", "0017_raytaskexecution_sensitive_data_permission")]
MIGRATE_TO = [("django_ray", "0018_workflow_run_allocation")]
LATEST = [("django_ray", "0026_ray_task_target_execution_evidence")]


def _assert_workflow_run_allocation_migration_round_trip() -> None:
    namespace = 0x123456789ABCDEF
    legacy_run_id = _workflow_run_id(namespace, 1)
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        legacy = old_execution.objects.create(
            task_id="workflow-run-allocation-migration",
            callable_path="tests.unit.test_workflows.increment",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=5,
            workflow_run_id=legacy_run_id,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        migrated = new_execution.objects.get(pk=legacy.pk)
        assert migrated.workflow_run_namespace is None
        assert migrated.workflow_run_sequence == 0
        assert str(migrated.workflow_run_id) == legacy_run_id

        # A schema-first deployment can keep an old enqueue process running. Its
        # historical model omits both allocation columns, so the persistent database
        # default must supply the non-null sequence rather than relying on Python code.
        rolling = old_execution.objects.create(
            task_id="workflow-run-allocation-rolling-writer",
            callable_path="tests.unit.test_workflows.increment",
            state=TaskState.QUEUED,
        )
        rolling_migrated = new_execution.objects.get(pk=rolling.pk)
        assert rolling_migrated.workflow_run_namespace is None
        assert rolling_migrated.workflow_run_sequence == 0

        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor,
                new_execution._meta.db_table,
            )
        assert constraints["ray_task_wf_run_seq_cap"]["check"] is True
        assert set(constraints["ray_task_wf_run_seq_cap"]["columns"]) == {"workflow_run_sequence"}
        assert constraints["ray_task_wf_run_ns_uniq"]["unique"] is True
        assert set(constraints["ray_task_wf_run_ns_uniq"]["columns"]) == {"workflow_run_namespace"}
        assert constraints["ray_task_wf_run_ns_range"]["check"] is True
        assert set(constraints["ray_task_wf_run_ns_range"]["columns"]) == {"workflow_run_namespace"}

        # Restore the current schema before using the imported current model.
        # The historical assertions above deliberately run at the 0018 boundary.
        MigrationExecutor(connection).migrate(LATEST)
        current = RayTaskExecution.objects.get(pk=legacy.pk)
        legacy_identity = WorkflowRunIdentity(
            task_execution_pk=current.pk,
            attempt_number=current.attempt_number,
            execution_generation=current.execution_generation,
            run_id=legacy_run_id,
        )
        assert reclaim_workflow_run(legacy_identity) is True
        current.refresh_from_db()
        assert current.workflow_run_namespace is None
        assert current.workflow_run_sequence == 0

        with patch("django_ray.workflow_progress.randbits", return_value=namespace):
            fresh_identity = allocate_workflow_run(
                DurableTaskContext(
                    task_pk=current.pk,
                    attempt_number=current.attempt_number,
                    execution_generation=current.execution_generation,
                )
            )
        assert fresh_identity is not None
        assert fresh_identity.run_id != legacy_identity.run_id
        current.refresh_from_db()
        assert current.workflow_run_namespace == namespace
        assert current.workflow_run_sequence == 2
        assert str(current.workflow_run_id) == fresh_identity.run_id
        assert reclaim_workflow_run(legacy_identity) is False
        assert reclaim_workflow_run(fresh_identity) is True

        RayTaskExecution.objects.filter(pk=current.pk).update(state=TaskState.SUCCEEDED)
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        reverted = reverted_execution.objects.get(pk=legacy.pk)
        assert str(reverted.workflow_run_id) == fresh_identity.run_id
        with pytest.raises(FieldDoesNotExist):
            reverted_execution._meta.get_field("workflow_run_namespace")
        with pytest.raises(FieldDoesNotExist):
            reverted_execution._meta.get_field("workflow_run_sequence")
    finally:
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.django_db(transaction=True)
def test_workflow_run_allocation_migration_preserves_legacy_reclaim_and_is_reversible() -> None:
    _assert_workflow_run_allocation_migration_round_trip()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_workflow_run_allocation_migration_uses_production_database() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_workflow_run_allocation_migration_round_trip()
