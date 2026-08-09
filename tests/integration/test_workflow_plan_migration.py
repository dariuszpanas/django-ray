"""Migration compatibility for durable effective workflow plans."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_existing_and_rolling_writer_rows_gain_nullable_plan_fields() -> None:
    migrate_from = [("django_ray", "0010_raytaskexecution_workflow_run_id")]
    migrate_to = [("django_ray", "0011_raytaskexecution_workflow_plan")]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        old_apps = executor.loader.project_state(migrate_from).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        old_execution.objects.create(
            task_id="workflow-plan-before-migration",
            callable_path="testproject.tasks.add_numbers",
            created_at=datetime.now(UTC),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        migrated = new_execution.objects.get(task_id="workflow-plan-before-migration")

        assert migrated.workflow_plan_fingerprint is None
        assert migrated.workflow_plan_pinned_attempt is None
        assert migrated.workflow_plan_json is None
        assert migrated.workflow_plan_selection is None
        assert new_execution._meta.get_field("workflow_plan_fingerprint").null is True

        # A process still running the 0010 model omits all four new columns.
        # Nullable phase-one fields keep that rolling-upgrade insert valid.
        old_execution.objects.create(
            task_id="workflow-plan-rolling-writer",
            callable_path="testproject.tasks.add_numbers",
            created_at=datetime.now(UTC),
        )
        rolling = new_execution.objects.get(task_id="workflow-plan-rolling-writer")
        assert rolling.workflow_plan_fingerprint is None
        assert rolling.workflow_plan_pinned_attempt is None

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        reverted_apps = executor.loader.project_state(migrate_from).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        assert "workflow_plan_fingerprint" not in {
            field.name for field in reverted_execution._meta.get_fields()
        }
        assert "workflow_plan_pinned_attempt" not in {
            field.name for field in reverted_execution._meta.get_fields()
        }
    finally:
        MigrationExecutor(connection).migrate([("django_ray", "0019_execution_protocol_schema")])
