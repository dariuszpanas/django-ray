"""Migration compatibility for bounded workflow-progress summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_summary_fields_are_additive_nullable_and_reversible() -> None:
    migrate_from = [("django_ray", "0011_raytaskexecution_workflow_plan")]
    migrate_to = [("django_ray", "0012_workflow_progress_summary")]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        old_apps = executor.loader.project_state(migrate_from).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        old_attempt = old_apps.get_model("django_ray", "TaskAttempt")
        legacy_progress = json.dumps({"schema_version": 1, "revision": 4})
        execution = old_execution.objects.create(
            task_id="workflow-summary-before-migration",
            callable_path="testproject.tasks.add_numbers",
            progress_data=legacy_progress,
            created_at=datetime.now(UTC),
        )
        old_attempt.objects.create(
            execution=execution,
            attempt_number=1,
            state="FAILED",
            created_at=datetime.now(UTC),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        new_attempt = new_apps.get_model("django_ray", "TaskAttempt")
        migrated = new_execution.objects.get(task_id="workflow-summary-before-migration")
        migrated_attempt = new_attempt.objects.get(execution=migrated, attempt_number=1)

        for model in (new_execution, new_attempt):
            field = model._meta.get_field("workflow_progress_summary_json")
            assert field.null is True
            assert field.blank is True
            assert field.editable is False
        assert migrated.workflow_progress_summary_json is None
        assert migrated_attempt.workflow_progress_summary_json is None
        assert migrated.progress_data == legacy_progress

        # A process still using the 0011 model can omit both nullable columns.
        rolling = old_execution.objects.create(
            task_id="workflow-summary-rolling-writer",
            callable_path="testproject.tasks.add_numbers",
            created_at=datetime.now(UTC),
        )
        old_attempt.objects.create(
            execution=rolling,
            attempt_number=1,
            state="SUCCEEDED",
            created_at=datetime.now(UTC),
        )
        rolling_new = new_execution.objects.get(task_id="workflow-summary-rolling-writer")
        assert rolling_new.workflow_progress_summary_json is None
        assert (
            new_attempt.objects.get(
                execution=rolling_new,
                attempt_number=1,
            ).workflow_progress_summary_json
            is None
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        reverted_apps = executor.loader.project_state(migrate_from).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        reverted_attempt = reverted_apps.get_model("django_ray", "TaskAttempt")
        assert "workflow_progress_summary_json" not in {
            field.name for field in reverted_execution._meta.get_fields()
        }
        assert "workflow_progress_summary_json" not in {
            field.name for field in reverted_attempt._meta.get_fields()
        }
        assert (
            reverted_execution.objects.get(
                task_id="workflow-summary-before-migration"
            ).progress_data
            == legacy_progress
        )
    finally:
        MigrationExecutor(connection).migrate([("django_ray", "0020_legacy_open_rollback_fence")])
