"""Adding terminal counters preserves existing runs without fabricated evidence."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_reporting_diagnostics_migration_preserves_history_and_old_inserts():
    old = [("django_ray", "0026_ray_task_target_execution_evidence")]
    new = [("django_ray", "0027_workflow_reporting_diagnostics")]
    executor = MigrationExecutor(connection)
    executor.migrate(old)
    try:
        apps = executor.loader.project_state(old).apps
        execution_model = apps.get_model("django_ray", "RayTaskExecution")
        run_model = apps.get_model("django_ray", "WorkflowProgressRunStorage")
        execution = execution_model.objects.create(
            task_id="reporting-diagnostics-upgrade",
            callable_path="fixture.task",
            state="SUCCEEDED",
            progress_data='{"schema_version":2}',
            workflow_progress_summary_json='{"schema_version":3}',
        )
        run = run_model.objects.create(
            execution=execution,
            attempt_number=1,
            execution_generation=1,
            run_id="00000000-0000-0000-0000-000000000027",
        )
        MigrationExecutor(connection).migrate(new)
        new_apps = MigrationExecutor(connection).loader.project_state(new).apps
        new_run_model = new_apps.get_model("django_ray", "WorkflowProgressRunStorage")
        assert new_run_model.objects.get(pk=run.pk).reporting_diagnostics_json is None
        execution.refresh_from_db()
        assert execution.progress_data == '{"schema_version":2}'
        assert execution.workflow_progress_summary_json == '{"schema_version":3}'
        # The column remains nullable so an old model can finish an admitted run
        # during the drain window without pretending to write new diagnostics.
        late = run_model.objects.create(
            execution=execution,
            attempt_number=2,
            execution_generation=2,
            run_id="00000000-0000-0000-0000-000000000028",
        )
        assert new_run_model.objects.get(pk=late.pk).reporting_diagnostics_json is None
        MigrationExecutor(connection).migrate(old)
        assert run_model.objects.filter(execution=execution).count() == 2
        execution.refresh_from_db()
        assert execution.progress_data == '{"schema_version":2}'
    finally:
        MigrationExecutor(connection).migrate(new)
