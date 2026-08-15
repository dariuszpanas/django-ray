"""Migration compatibility for durable external task-input references."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_existing_inline_execution_round_trips_through_input_payload_migration() -> None:
    migrate_from = [("django_ray", "0008_raytaskexecution_priority_constraint")]
    migrate_to = [("django_ray", "0009_taskinputpayload_and_input_reference")]
    executor = MigrationExecutor(connection)
    executor.migrate(migrate_from)
    try:
        old_apps = executor.loader.project_state(migrate_from).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        old_execution.objects.create(
            task_id="input-migration-inline",
            callable_path="testproject.tasks.add_numbers",
            args_json="[1, 2]",
            kwargs_json='{"scale": 3}',
            created_at=datetime.now(UTC),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        new_apps = executor.loader.project_state(migrate_to).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        payload_model = new_apps.get_model("django_ray", "TaskInputPayload")
        input_field = new_execution._meta.get_field("input_reference")

        assert input_field.max_length == 500
        assert input_field.null is True
        assert input_field.blank is True
        assert input_field.db_index is True
        migrated = new_execution.objects.get(task_id="input-migration-inline")
        assert migrated.args_json == "[1, 2]"
        assert migrated.kwargs_json == '{"scale": 3}'
        assert migrated.input_reference is None

        reference = f"inputfs://sha256/{'a' * 64}?bytes=32"
        payload_model.objects.create(
            reference=reference,
            backend="filesystem",
            digest="a" * 64,
            size_bytes=32,
            envelope_version=1,
        )
        migrated.input_reference = reference
        migrated.save(update_fields=["input_reference"])
        assert payload_model._meta.pk.name == "reference"
        assert {index.name for index in payload_model._meta.indexes} == {"ray_input_cleanup_idx"}

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        reverted_apps = executor.loader.project_state(migrate_from).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        assert "input_reference" not in {
            field.name for field in reverted_execution._meta.get_fields()
        }
        assert "taskinputpayload" not in {
            model._meta.model_name for model in reverted_apps.get_models()
        }
        reverted = reverted_execution.objects.get(task_id="input-migration-inline")
        assert reverted.args_json == "[1, 2]"
        assert reverted.kwargs_json == '{"scale": 3}'
    finally:
        MigrationExecutor(connection).migrate([("django_ray", "0023_ray_task_target_binding")])
