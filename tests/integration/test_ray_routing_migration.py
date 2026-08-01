"""Migration compatibility for immutable Ray routing targets."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_ray_target_address_is_additive_nullable_and_reversible() -> None:
    migrate_from = [("django_ray", "0013_workflow_progress_detail_storage")]
    migrate_to = [("django_ray", "0014_raytaskexecution_ray_target_address")]
    latest = [("django_ray", "0015_raytaskexecution_task_id_unique")]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        old_apps = executor.loader.project_state(migrate_from).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        legacy = old_execution.objects.create(
            task_id="routing-before-migration",
            callable_path="testproject.tasks.add_numbers",
            ray_address="ray://legacy:10001",
            created_at=datetime.now(UTC),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        target_field = new_execution._meta.get_field("ray_target_address")

        assert target_field.max_length == 255
        assert target_field.null is True
        assert target_field.blank is True
        migrated = new_execution.objects.get(pk=legacy.pk)
        assert migrated.ray_target_address is None
        assert migrated.ray_address == "ray://legacy:10001"

        rolling = old_execution.objects.create(
            task_id="routing-old-writer",
            callable_path="testproject.tasks.add_numbers",
            ray_address="ray://rolling:10001",
            created_at=datetime.now(UTC),
        )
        assert new_execution.objects.get(pk=rolling.pk).ray_target_address is None

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        reverted_apps = executor.loader.project_state(migrate_from).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        assert "ray_target_address" not in {
            field.name for field in reverted_execution._meta.get_fields()
        }
        assert reverted_execution.objects.get(pk=legacy.pk).ray_address == "ray://legacy:10001"
        assert reverted_execution.objects.get(pk=rolling.pk).ray_address == "ray://rolling:10001"
    finally:
        MigrationExecutor(connection).migrate(latest)
