"""Migration compatibility for workflow progress run ownership."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_existing_progress_gains_nullable_run_identity_and_reverses() -> None:
    migrate_from = [("django_ray", "0009_taskinputpayload_and_input_reference")]
    migrate_to = [("django_ray", "0010_raytaskexecution_workflow_run_id")]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        old_apps = executor.loader.project_state(migrate_from).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        legacy_progress = json.dumps({"schema_version": 1, "revision": 3})
        old_execution.objects.create(
            task_id="workflow-progress-migration",
            callable_path="testproject.tasks.add_numbers",
            progress_data=legacy_progress,
            created_at=datetime.now(UTC),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        run_field = new_execution._meta.get_field("workflow_run_id")
        migrated = new_execution.objects.get(task_id="workflow-progress-migration")

        assert run_field.null is True
        assert run_field.blank is True
        assert run_field.editable is False
        assert migrated.workflow_run_id is None
        assert migrated.progress_data == legacy_progress

        migrated.workflow_run_id = "00000000-0000-0000-0000-000000000041"
        migrated.save(update_fields=["workflow_run_id"])

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        reverted_apps = executor.loader.project_state(migrate_from).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        assert "workflow_run_id" not in {
            field.name for field in reverted_execution._meta.get_fields()
        }
        reverted = reverted_execution.objects.get(task_id="workflow-progress-migration")
        assert reverted.progress_data == legacy_progress
    finally:
        MigrationExecutor(connection).migrate([("django_ray", "0019_execution_protocol_schema")])
