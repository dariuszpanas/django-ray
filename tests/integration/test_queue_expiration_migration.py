"""Migration safety for adopting bounded queued-wait deadlines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = [("django_ray", "0015_raytaskexecution_task_id_unique")]
MIGRATE_TO = [("django_ray", "0016_raytaskexecution_queue_expiration")]
LATEST = [("django_ray", "0021_ray_job_request_reference")]


def _assert_existing_queued_rows_get_deadline_from_latest_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJANGO_RAY_EXISTING_QUEUED_UNLIMITED", raising=False)
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        created_at = datetime.now(UTC) - timedelta(days=14)
        run_after = created_at + timedelta(days=2)
        legacy = old_execution.objects.create(
            task_id="queue-expiry-before-migration",
            callable_path="testproject.tasks.add_numbers",
            state="QUEUED",
            created_at=created_at,
            run_after=run_after,
        )
        running = old_execution.objects.create(
            task_id="queue-expiry-running-before-migration",
            callable_path="testproject.tasks.add_numbers",
            state="RUNNING",
            created_at=created_at,
        )
        failed = old_execution.objects.create(
            task_id="queue-expiry-failed-before-migration",
            callable_path="testproject.tasks.add_numbers",
            state="FAILED",
            created_at=created_at,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        migrated = new_execution.objects.get(pk=legacy.pk)
        assert migrated.queue_timeout_seconds == 86400
        assert migrated.queue_deadline_at == run_after + timedelta(days=1)
        migrated_running = new_execution.objects.get(pk=running.pk)
        migrated_failed = new_execution.objects.get(pk=failed.pk)
        assert migrated_running.queue_timeout_seconds == 86400
        assert migrated_running.queue_deadline_at is None
        assert migrated_failed.queue_timeout_seconds == 86400
        assert migrated_failed.queue_deadline_at is None
        assert "ray_task_id_unique" in {
            constraint.name for constraint in new_execution._meta.constraints
        }
    finally:
        MigrationExecutor(connection).migrate(LATEST)


def _assert_existing_queued_rows_support_explicit_unlimited_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        legacy = old_apps.get_model("django_ray", "RayTaskExecution").objects.create(
            task_id="queue-expiry-unlimited-adoption",
            callable_path="testproject.tasks.add_numbers",
            state="QUEUED",
            created_at=datetime.now(UTC) - timedelta(days=14),
        )
        failed = old_apps.get_model("django_ray", "RayTaskExecution").objects.create(
            task_id="queue-expiry-failed-unlimited-adoption",
            callable_path="testproject.tasks.add_numbers",
            state="FAILED",
            created_at=datetime.now(UTC) - timedelta(days=14),
        )
        monkeypatch.setenv("DJANGO_RAY_EXISTING_QUEUED_UNLIMITED", "1")

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        migrated = (
            executor.loader.project_state(MIGRATE_TO)
            .apps.get_model("django_ray", "RayTaskExecution")
            .objects.get(pk=legacy.pk)
        )
        assert migrated.queue_timeout_seconds is None
        assert migrated.queue_deadline_at is None
        migrated_failed = (
            executor.loader.project_state(MIGRATE_TO)
            .apps.get_model("django_ray", "RayTaskExecution")
            .objects.get(pk=failed.pk)
        )
        assert migrated_failed.queue_timeout_seconds == 86400
        assert migrated_failed.queue_deadline_at is None
    finally:
        monkeypatch.delenv("DJANGO_RAY_EXISTING_QUEUED_UNLIMITED", raising=False)
        MigrationExecutor(connection).migrate(LATEST)


def _assert_reverse_migration_keeps_expired_executions_terminal() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_TO)
    try:
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        new_attempt = new_apps.get_model("django_ray", "TaskAttempt")
        expired = new_execution.objects.create(
            task_id="queue-expiry-reverse-terminal",
            callable_path="testproject.tasks.add_numbers",
            state="EXPIRED",
            queue_timeout_seconds=86400,
            queue_deadline_at=datetime.now(UTC) - timedelta(days=1),
        )
        new_attempt.objects.create(
            execution_id=expired.pk,
            attempt_number=1,
            state="EXPIRED",
            error_message="Task expired before execution after exceeding its queued-wait deadline",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        reverted = old_execution.objects.get(pk=expired.pk)
        reverted_attempt = old_apps.get_model("django_ray", "TaskAttempt").objects.get(
            execution_id=expired.pk,
            attempt_number=1,
        )
        assert reverted.state == "FAILED"
        assert reverted_attempt.state == "FAILED"
        assert "expired before execution" in reverted_attempt.error_message
        assert "ray_task_id_unique" in {
            constraint.name for constraint in old_execution._meta.constraints
        }
    finally:
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.django_db(transaction=True)
def test_existing_queued_rows_get_deadline_from_latest_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_existing_queued_rows_get_deadline_from_latest_eligibility(monkeypatch)


@pytest.mark.django_db(transaction=True)
def test_existing_queued_rows_support_explicit_unlimited_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_existing_queued_rows_support_explicit_unlimited_adoption(monkeypatch)


@pytest.mark.django_db(transaction=True)
def test_reverse_migration_keeps_expired_executions_terminal() -> None:
    _assert_reverse_migration_keeps_expired_executions_terminal()


def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_queue_deadline_backfill_uses_production_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _assert_existing_queued_rows_get_deadline_from_latest_eligibility(monkeypatch)


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_queue_deadline_unlimited_opt_out_uses_production_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _assert_existing_queued_rows_support_explicit_unlimited_adoption(monkeypatch)


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_queue_deadline_reverse_preserves_terminal_state() -> None:
    _require_postgresql()
    _assert_reverse_migration_keeps_expired_executions_terminal()
