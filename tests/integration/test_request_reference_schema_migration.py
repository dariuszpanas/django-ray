"""Migration compatibility for durable Ray Job request references."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATE_FROM = [("django_ray", "0020_legacy_open_rollback_fence")]
MIGRATE_TO = [("django_ray", "0021_ray_job_request_reference")]
LATEST = MIGRATE_TO


def _insert_legacy_payload_row(payload_model, *, reference: str) -> None:
    """Issue the exact pre-0021 column shape without invoking a Python default."""
    columns = (
        "reference",
        "backend",
        "digest",
        "size_bytes",
        "envelope_version",
        "state",
        "created_at",
        "last_used_at",
        "purged_at",
        "cleanup_error",
    )
    quote = connection.ops.quote_name
    column_sql = ", ".join(quote(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    now = timezone.now()
    values = (
        reference,
        "filesystem",
        "b" * 64,
        64,
        1,
        "ACTIVE",
        now,
        now,
        None,
        "",
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {quote(payload_model._meta.db_table)} "
            f"({column_sql}) VALUES ({placeholders})",
            values,
        )


def _assert_request_reference_schema_migration_round_trip() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        old_payload = old_apps.get_model("django_ray", "TaskInputPayload")
        existing_execution = old_execution.objects.create(
            task_id="request-reference-before-migration",
            callable_path="testproject.tasks.add_numbers",
        )
        existing_payload = old_payload.objects.create(
            reference="inputfs://sha256/existing?bytes=32",
            backend="filesystem",
            digest="a" * 64,
            size_bytes=32,
            envelope_version=1,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        new_payload = new_apps.get_model("django_ray", "TaskInputPayload")

        request_field = new_execution._meta.get_field("ray_job_request_reference")
        assert request_field.max_length == 500
        assert request_field.null is True
        assert request_field.blank is True
        assert request_field.db_index is True
        assert new_execution.objects.get(pk=existing_execution.pk).ray_job_request_reference is None

        kind_field = new_payload._meta.get_field("payload_kind")
        assert kind_field.max_length == 32
        assert kind_field.default == "task_input"
        assert kind_field.db_default == "task_input"
        assert dict(kind_field.choices) == {
            "task_input": "Task input",
            "ray_job_request": "Ray Job request",
        }
        assert new_payload.objects.get(pk=existing_payload.pk).payload_kind == "task_input"

        # A released writer uses the pre-0021 model and omits the nullable request
        # reference. Its INSERT must remain valid after the schema-first deploy.
        rolling_execution = old_execution.objects.create(
            task_id="request-reference-rolling-execution",
            callable_path="testproject.tasks.add_numbers",
        )
        rolling_row = new_execution.objects.get(pk=rolling_execution.pk)
        assert rolling_row.ray_job_request_reference is None

        # Exercise the literal released TaskInputPayload INSERT column list. The
        # database, rather than a current-model Python default, supplies the kind.
        raw_reference = "inputfs://sha256/rolling?bytes=64"
        _insert_legacy_payload_row(old_payload, reference=raw_reference)
        assert new_payload.objects.get(pk=raw_reference).payload_kind == "task_input"

        arbitrary_reference = "requestfs://sha256/opaque?bytes=128"
        rolling_row.ray_job_request_reference = arbitrary_reference
        rolling_row.save(update_fields=["ray_job_request_reference"])
        rolling_row.refresh_from_db()
        assert rolling_row.ray_job_id is None
        assert rolling_row.ray_job_request_reference == arbitrary_reference

        with connection.cursor() as cursor:
            execution_constraints = connection.introspection.get_constraints(
                cursor,
                new_execution._meta.db_table,
            )
            payload_constraints = connection.introspection.get_constraints(
                cursor,
                new_payload._meta.db_table,
            )
        assert any(
            details["index"] and details["columns"] == ["ray_job_request_reference"]
            for details in execution_constraints.values()
        )
        assert payload_constraints["ray_input_payload_kind_valid"]["check"] is True
        assert payload_constraints["ray_input_payload_kind_valid"]["columns"] == ["payload_kind"]

        with pytest.raises(IntegrityError), transaction.atomic():
            new_payload.objects.filter(pk=raw_reference).update(payload_kind="unknown")

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        reverted_payload = reverted_apps.get_model("django_ray", "TaskInputPayload")
        assert "ray_job_request_reference" not in {
            field.name for field in reverted_execution._meta.get_fields()
        }
        assert "payload_kind" not in {field.name for field in reverted_payload._meta.get_fields()}
        assert reverted_payload.objects.filter(pk=raw_reference).exists()
    finally:
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.django_db(transaction=True)
def test_request_reference_schema_is_additive_strict_and_reversible() -> None:
    _assert_request_reference_schema_migration_round_trip()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_request_reference_schema_uses_persistent_database_default() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_request_reference_schema_migration_round_trip()
