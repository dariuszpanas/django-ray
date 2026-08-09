"""Migration and dormant-fence coverage for durable execution protocol metadata."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

import django_ray.protocol_coordination as protocol_coordination
from django_ray.lifecycle import retry_task
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskExecutionProtocolPolicy,
    TaskState,
)
from django_ray.protocol_coordination import (
    LegacyWorkerRollbackBlockedError,
    close_legacy_worker_admission,
    reopen_legacy_worker_admission,
)

MIGRATE_FROM = [("django_ray", "0018_workflow_run_allocation")]
MIGRATE_TO = [("django_ray", "0019_execution_protocol_schema")]
ROLLBACK_FENCE_FROM = MIGRATE_TO
ROLLBACK_FENCE_TO = [("django_ray", "0020_legacy_open_rollback_fence")]

EXPECTED_PROTOCOL_TRIGGERS = {
    "ray_attempt_immutable_0019",
    "ray_exec_immutable_0019",
    "ray_exec_legacy_insert_0019",
    "ray_exec_owner_update_0019",
    "ray_lease_admission_insert_0019",
    "ray_lease_admission_update_0019",
    "ray_lease_capability_immutable_0019",
}

EXPECTED_PROTOCOL_FUNCTIONS = {
    "django_ray_guard_attempt_immutable_0019",
    "django_ray_guard_execution_immutable_0019",
    "django_ray_guard_lease_capability_0019",
    "django_ray_guard_legacy_execution_0019",
    "django_ray_guard_legacy_lease_0019",
    "django_ray_guard_task_owner_0019",
}

EXPECTED_ROLLBACK_FENCE_TRIGGERS = {
    "ray_exec_legacy_open_insert_0020",
    "ray_exec_legacy_open_retry_0020",
}

EXPECTED_ROLLBACK_FENCE_FUNCTIONS = {
    "django_ray_guard_legacy_open_execution_0020",
}


class _RecordingSchemaEditor:
    def __init__(self, vendor: str) -> None:
        self.connection = SimpleNamespace(vendor=vendor)
        self.statements: list[str] = []

    @staticmethod
    def quote_name(name: str) -> str:
        return f'"{name}"'

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def _rejects_integrity_error(operation: Callable[[], object]) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        operation()


def test_protocol_fence_vendor_dispatch_is_reversible_and_fail_closed() -> None:
    migration = importlib.import_module("django_ray.migrations.0019_execution_protocol_schema")
    postgresql = _RecordingSchemaEditor("postgresql")

    migration._install_protocol_fences(None, postgresql)

    installed_sql = "\n".join(postgresql.statements)
    for trigger_name in EXPECTED_PROTOCOL_TRIGGERS:
        assert f"CREATE TRIGGER {trigger_name}" in installed_sql
    for function_name in EXPECTED_PROTOCOL_FUNCTIONS:
        assert f"CREATE FUNCTION {function_name}" in installed_sql

    postgresql.statements.clear()
    migration._remove_protocol_fences(None, postgresql)

    removed_sql = "\n".join(postgresql.statements)
    for trigger_name in EXPECTED_PROTOCOL_TRIGGERS:
        assert f'DROP TRIGGER IF EXISTS "{trigger_name}" ON' in removed_sql
    for function_name in EXPECTED_PROTOCOL_FUNCTIONS:
        assert f'DROP FUNCTION IF EXISTS "{function_name}"()' in removed_sql

    unsupported = _RecordingSchemaEditor("mysql")
    message = "execution-protocol fencing supports only SQLite and PostgreSQL"
    with pytest.raises(RuntimeError, match=message):
        migration._install_protocol_fences(None, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._remove_protocol_fences(None, unsupported)
    assert unsupported.statements == []


def test_legacy_open_rollback_fence_sql_is_reversible_and_fail_closed() -> None:
    migration = importlib.import_module("django_ray.migrations.0020_legacy_open_rollback_fence")
    postgresql = _RecordingSchemaEditor("postgresql")

    migration._lock_migration_boundary(postgresql)
    migration._install_postgresql_fence(postgresql)

    installed_sql = "\n".join(postgresql.statements)
    assert "LOCK TABLE" in installed_sql
    assert "SHARE ROW EXCLUSIVE MODE" in installed_sql
    assert "FOR SHARE" in installed_sql
    for trigger_name in EXPECTED_ROLLBACK_FENCE_TRIGGERS:
        assert f"CREATE TRIGGER {trigger_name}" in installed_sql
    for function_name in EXPECTED_ROLLBACK_FENCE_FUNCTIONS:
        assert f"CREATE FUNCTION {function_name}" in installed_sql

    postgresql.statements.clear()
    migration._remove_legacy_open_rollback_fence(None, postgresql)

    removed_sql = "\n".join(postgresql.statements)
    for trigger_name in EXPECTED_ROLLBACK_FENCE_TRIGGERS:
        assert f'DROP TRIGGER IF EXISTS "{trigger_name}" ON' in removed_sql
    for function_name in EXPECTED_ROLLBACK_FENCE_FUNCTIONS:
        assert f'DROP FUNCTION IF EXISTS "{function_name}"()' in removed_sql

    sqlite = _RecordingSchemaEditor("sqlite")
    migration._lock_migration_boundary(sqlite)
    migration._install_sqlite_fence(sqlite)
    sqlite_sql = "\n".join(sqlite.statements)
    assert sqlite_sql.lstrip().startswith("UPDATE")
    for trigger_name in EXPECTED_ROLLBACK_FENCE_TRIGGERS:
        assert f"CREATE TRIGGER {trigger_name}" in sqlite_sql
    assert "RAISE(" in sqlite_sql

    unsupported = _RecordingSchemaEditor("mysql")
    message = "legacy-open rollback fencing supports only SQLite and PostgreSQL"
    with pytest.raises(RuntimeError, match=message):
        migration._lock_migration_boundary(unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._remove_legacy_open_rollback_fence(None, unsupported)
    assert unsupported.statements == []


def _database_trigger_names() -> set[str]:
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        elif connection.vendor == "postgresql":
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        else:
            raise AssertionError(f"unsupported database vendor: {connection.vendor}")
        return {str(row[0]) for row in cursor.fetchall()}


def _assert_current_schema_retains_protocol_triggers() -> None:
    assert EXPECTED_PROTOCOL_TRIGGERS <= _database_trigger_names()


def _assert_execution_protocol_schema_migration_round_trip() -> None:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("django_ray")
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        old_attempt = old_apps.get_model("django_ray", "TaskAttempt")
        old_lease = old_apps.get_model("django_ray", "TaskWorkerLease")

        existing_legacy_lease = old_lease.objects.create(
            worker_id="migration-existing-legacy",
            hostname="legacy-host",
            pid=1001,
            queue_name="default",
            is_active=True,
        )
        existing_inactive_lease = old_lease.objects.create(
            worker_id="migration-existing-inactive",
            hostname="legacy-host",
            pid=1002,
            queue_name="default",
            is_active=False,
        )
        queued = old_execution.objects.create(
            task_id="protocol-migration-queued",
            callable_path="tests.unit.test_workflows.increment",
            state="QUEUED",
        )
        running = old_execution.objects.create(
            task_id="protocol-migration-running",
            callable_path="tests.unit.test_workflows.increment",
            state="RUNNING",
            claimed_by_worker=existing_legacy_lease.worker_id,
        )
        terminal = old_execution.objects.create(
            task_id="protocol-migration-terminal",
            callable_path="tests.unit.test_workflows.increment",
            state="SUCCEEDED",
        )
        archived = old_attempt.objects.create(
            execution=terminal,
            attempt_number=1,
            state="SUCCEEDED",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        new_attempt = new_apps.get_model("django_ray", "TaskAttempt")
        new_lease = new_apps.get_model("django_ray", "TaskWorkerLease")
        policy_model = new_apps.get_model("django_ray", "TaskExecutionProtocolPolicy")
        token_model = new_apps.get_model("django_ray", "LegacyWorkerAdmissionToken")

        for execution_id in (queued.pk, running.pk, terminal.pk):
            migrated = new_execution.objects.get(pk=execution_id)
            assert migrated.metadata_schema_version == 0
            assert migrated.execution_protocol_version == 1
            assert migrated.created_with_django_ray_version is None
            assert migrated.managed_with_django_ray_version is None
            assert migrated.executor_django_ray_version is None

        migrated_attempt = new_attempt.objects.get(pk=archived.pk)
        assert migrated_attempt.execution_protocol_version == 1
        assert migrated_attempt.managed_with_django_ray_version is None
        assert migrated_attempt.executor_django_ray_version is None

        for worker_id in (existing_legacy_lease.pk, existing_inactive_lease.pk):
            migrated_lease = new_lease.objects.get(pk=worker_id)
            assert migrated_lease.capability_schema_version == 0
            assert migrated_lease.django_ray_version is None
            assert migrated_lease.min_supported_execution_protocol_version is None
            assert migrated_lease.max_supported_execution_protocol_version is None
            assert migrated_lease.legacy_admission_token_id == 1

        policy = policy_model.objects.get(singleton_key=1)
        assert policy_model.objects.count() == 1
        assert policy.schema_version == 1
        assert policy.active_write_protocol_version == 1
        assert policy.legacy_worker_admission_enabled is True
        assert policy.revision == 1
        assert policy.updated_at is not None
        assert list(token_model.objects.values_list("singleton_key", flat=True)) == [1]

        # These classes still describe migration 0018. Their INSERT statements omit
        # every new column and therefore exercise persistent database defaults.
        rolling_execution = old_execution.objects.create(
            task_id="protocol-migration-rolling-execution",
            callable_path="tests.unit.test_workflows.increment",
            state="QUEUED",
        )
        rolling_attempt = old_attempt.objects.create(
            execution=rolling_execution,
            attempt_number=1,
            state="FAILED",
        )
        rolling_lease = old_lease.objects.create(
            worker_id="migration-rolling-legacy",
            hostname="rolling-host",
            pid=1003,
            queue_name="default",
            is_active=True,
        )
        rolling_migrated = new_execution.objects.get(pk=rolling_execution.pk)
        assert rolling_migrated.metadata_schema_version == 0
        assert rolling_migrated.execution_protocol_version == 1
        assert rolling_migrated.created_with_django_ray_version is None
        assert new_attempt.objects.get(pk=rolling_attempt.pk).execution_protocol_version == 1
        rolling_migrated_lease = new_lease.objects.get(pk=rolling_lease.pk)
        assert rolling_migrated_lease.capability_schema_version == 0
        assert rolling_migrated_lease.django_ray_version is None
        assert rolling_migrated_lease.min_supported_execution_protocol_version is None
        assert rolling_migrated_lease.max_supported_execution_protocol_version is None
        assert rolling_migrated_lease.legacy_admission_token_id == 1

        heartbeat_at = timezone.now()
        assert (
            old_lease.objects.filter(pk=rolling_lease.pk).update(last_heartbeat_at=heartbeat_at)
            == 1
        )
        rolling_migrated_lease.refresh_from_db()
        assert rolling_migrated_lease.last_heartbeat_at == heartbeat_at

        with connection.cursor() as cursor:
            execution_constraints = connection.introspection.get_constraints(
                cursor,
                new_execution._meta.db_table,
            )
            attempt_constraints = connection.introspection.get_constraints(
                cursor,
                new_attempt._meta.db_table,
            )
            lease_constraints = connection.introspection.get_constraints(
                cursor,
                new_lease._meta.db_table,
            )
        assert execution_constraints["ray_task_metadata_schema_known"]["check"] is True
        assert execution_constraints["ray_task_protocol_positive"]["check"] is True
        assert attempt_constraints["ray_attempt_protocol_positive"]["check"] is True
        assert lease_constraints["ray_worker_capability_valid"]["check"] is True
        assert lease_constraints["ray_worker_protocol_idx"]["index"] is True
        assert any(
            details["index"] and details["columns"] == ["execution_protocol_version"]
            for details in execution_constraints.values()
        )

        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=queued.pk).update(execution_protocol_version=2)
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=queued.pk).update(metadata_schema_version=1)
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=queued.pk).update(
                created_with_django_ray_version="0.4.0"
            )
        )
        _rejects_integrity_error(
            lambda: new_attempt.objects.filter(pk=archived.pk).update(
                managed_with_django_ray_version="0.5.0"
            )
        )
        _rejects_integrity_error(
            lambda: new_lease.objects.filter(pk=rolling_lease.pk).update(
                capability_schema_version=1
            )
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.create(
                task_id="protocol-migration-zero-protocol",
                callable_path="tests.unit.test_workflows.increment",
                execution_protocol_version=0,
            )
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.create(
                task_id="protocol-migration-unknown-metadata",
                callable_path="tests.unit.test_workflows.increment",
                metadata_schema_version=2,
            )
        )
        _rejects_integrity_error(
            lambda: new_attempt.objects.create(
                execution_id=queued.pk,
                attempt_number=99,
                state="FAILED",
                execution_protocol_version=0,
            )
        )
        _rejects_integrity_error(
            lambda: new_lease.objects.create(
                worker_id="migration-invalid-range",
                hostname="range-host",
                pid=2000,
                capability_schema_version=1,
                min_supported_execution_protocol_version=2,
                max_supported_execution_protocol_version=1,
                legacy_admission_token=None,
            )
        )
        _rejects_integrity_error(
            lambda: new_lease.objects.create(
                worker_id="migration-missing-minimum",
                hostname="range-host",
                pid=2003,
                capability_schema_version=1,
                min_supported_execution_protocol_version=None,
                max_supported_execution_protocol_version=1,
                legacy_admission_token=None,
            )
        )
        _rejects_integrity_error(
            lambda: new_lease.objects.create(
                worker_id="migration-missing-maximum",
                hostname="range-host",
                pid=2004,
                capability_schema_version=1,
                min_supported_execution_protocol_version=1,
                max_supported_execution_protocol_version=None,
                legacy_admission_token=None,
            )
        )

        v1_worker = new_lease.objects.create(
            worker_id="migration-v1-worker",
            hostname="protocol-host",
            pid=2001,
            capability_schema_version=1,
            django_ray_version="0.5.0",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
        )
        v1_v2_worker = new_lease.objects.create(
            worker_id="migration-v1-v2-worker",
            hostname="protocol-host",
            pid=2002,
            capability_schema_version=1,
            django_ray_version=None,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=2,
            legacy_admission_token=None,
        )
        _rejects_integrity_error(
            lambda: new_lease.objects.filter(pk=v1_worker.pk).update(
                max_supported_execution_protocol_version=2
            )
        )

        legacy_v1 = new_execution.objects.create(
            task_id="protocol-owner-legacy-v1",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=1,
        )
        assert (
            new_execution.objects.filter(pk=legacy_v1.pk).update(
                state="RUNNING",
                claimed_by_worker=existing_legacy_lease.pk,
            )
            == 1
        )

        legacy_v2 = new_execution.objects.create(
            task_id="protocol-owner-legacy-v2",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=legacy_v2.pk).update(
                state="RUNNING",
                claimed_by_worker=existing_legacy_lease.pk,
            )
        )
        legacy_v2.refresh_from_db()
        assert legacy_v2.state == "QUEUED"
        assert legacy_v2.claimed_by_worker is None

        explicit_v1 = new_execution.objects.create(
            task_id="protocol-owner-explicit-v1",
            callable_path="tests.unit.test_workflows.increment",
        )
        assert (
            new_execution.objects.filter(pk=explicit_v1.pk).update(
                state="RUNNING",
                claimed_by_worker=v1_worker.pk,
            )
            == 1
        )
        explicit_v2 = new_execution.objects.create(
            task_id="protocol-owner-explicit-v2",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=explicit_v2.pk).update(
                state="RUNNING",
                claimed_by_worker=v1_worker.pk,
            )
        )
        assert (
            new_execution.objects.filter(pk=explicit_v2.pk).update(
                state="RUNNING",
                claimed_by_worker=v1_v2_worker.pk,
            )
            == 1
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=explicit_v2.pk).update(
                claimed_by_worker=v1_worker.pk
            )
        )
        explicit_v2.refresh_from_db()
        assert explicit_v2.claimed_by_worker == v1_v2_worker.pk
        assert new_execution.objects.filter(pk=explicit_v2.pk).update(claimed_by_worker=None) == 1

        missing_owner = new_execution.objects.create(
            task_id="protocol-owner-missing",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=missing_owner.pk).update(
                state="RUNNING",
                claimed_by_worker="missing-worker",
            )
        )
        new_lease.objects.filter(pk=v1_worker.pk).update(is_active=False)
        inactive_owner = new_execution.objects.create(
            task_id="protocol-owner-inactive",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=inactive_owner.pk).update(
                state="RUNNING",
                claimed_by_worker=v1_worker.pk,
            )
        )

        # A lease can expire after accepting work. Moving that unchanged owner to a
        # terminal state remains available for the existing lifecycle completion fence.
        new_execution.objects.filter(pk=explicit_v2.pk).update(
            state="RUNNING",
            claimed_by_worker=v1_v2_worker.pk,
        )
        new_lease.objects.filter(pk=v1_v2_worker.pk).update(is_active=False)
        assert new_execution.objects.filter(pk=explicit_v2.pk).update(state="SUCCEEDED") == 1

        # Protocol v1 remains behaviorally dormant while legacy admission is open,
        # including recovery fixtures that intentionally preserve a stale owner.
        dormant_v1 = new_execution.objects.create(
            task_id="protocol-owner-dormant-v1",
            callable_path="tests.unit.test_workflows.increment",
        )
        assert (
            new_execution.objects.filter(pk=dormant_v1.pk).update(
                state="RUNNING",
                claimed_by_worker="stale-v1-worker",
            )
            == 1
        )

        # Missing policy state fails closed at every new ownership boundary and on
        # legacy heartbeats, while explicit capability rows remain durable.
        policy_model.objects.all().delete()
        assert new_execution.objects.filter(pk=dormant_v1.pk).update(state="CANCELLING") == 1
        policy_missing = new_execution.objects.create(
            task_id="protocol-owner-policy-missing",
            callable_path="tests.unit.test_workflows.increment",
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=policy_missing.pk).update(
                state="RUNNING",
                claimed_by_worker=v1_worker.pk,
            )
        )
        _rejects_integrity_error(
            lambda: old_lease.objects.filter(pk=rolling_lease.pk).update(
                last_heartbeat_at=timezone.now()
            )
        )
        policy_model.objects.create(
            singleton_key=1,
            schema_version=1,
            active_write_protocol_version=1,
            legacy_worker_admission_enabled=True,
            revision=1,
            updated_at=timezone.now(),
        )

        # Retiring every active legacy lease permits the token and policy latch to
        # close. Historical 0018 writers then fail without relying on Python defaults.
        new_lease.objects.filter(capability_schema_version=0).update(
            is_active=False,
            stopped_at=timezone.now(),
        )
        new_lease.objects.filter(capability_schema_version=0).update(legacy_admission_token=None)
        policy_model.objects.filter(singleton_key=1).update(
            legacy_worker_admission_enabled=False,
            revision=2,
            updated_at=timezone.now(),
        )
        assert token_model.objects.filter(singleton_key=1).delete()[0] == 1
        _rejects_integrity_error(
            lambda: old_lease.objects.create(
                worker_id="migration-closed-legacy",
                hostname="closed-host",
                pid=3001,
                is_active=True,
            )
        )
        _rejects_integrity_error(
            lambda: old_execution.objects.create(
                task_id="protocol-migration-closed-legacy-execution",
                callable_path="tests.unit.test_workflows.increment",
            )
        )
        closed_explicit = new_lease.objects.create(
            worker_id="migration-closed-explicit",
            hostname="closed-host",
            pid=3002,
            capability_schema_version=1,
            django_ray_version="0.5.0",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
        )
        closed_v1 = new_execution.objects.create(
            task_id="protocol-owner-closed-v1",
            callable_path="tests.unit.test_workflows.increment",
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=closed_v1.pk).update(
                state="RUNNING",
                claimed_by_worker="missing-closed-worker",
            )
        )
        assert (
            new_execution.objects.filter(pk=closed_v1.pk).update(
                state="RUNNING",
                claimed_by_worker=closed_explicit.pk,
            )
            == 1
        )
        _rejects_integrity_error(
            lambda: new_execution.objects.filter(pk=closed_v1.pk).update(
                claimed_by_worker=existing_legacy_lease.pk,
            )
        )
        new_lease.objects.filter(pk=closed_explicit.pk).update(is_active=False)
        assert new_execution.objects.filter(pk=closed_v1.pk).update(state="CANCELLING") == 1

        token_model.objects.create(singleton_key=1)
        policy_model.objects.filter(singleton_key=1).update(
            legacy_worker_admission_enabled=True,
            revision=3,
            updated_at=timezone.now(),
        )
        reopened_legacy = old_lease.objects.create(
            worker_id="migration-reopened-legacy",
            hostname="reopened-host",
            pid=3003,
            is_active=True,
        )
        assert new_lease.objects.get(pk=reopened_legacy.pk).legacy_admission_token_id == 1

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        reverted_attempt = reverted_apps.get_model("django_ray", "TaskAttempt")
        reverted_lease = reverted_apps.get_model("django_ray", "TaskWorkerLease")
        assert reverted_execution.objects.filter(pk=queued.pk).exists()
        assert reverted_attempt.objects.filter(pk=archived.pk).exists()
        assert reverted_lease.objects.filter(pk=existing_legacy_lease.pk).exists()
        with pytest.raises(FieldDoesNotExist):
            reverted_execution._meta.get_field("execution_protocol_version")
        with pytest.raises(FieldDoesNotExist):
            reverted_attempt._meta.get_field("execution_protocol_version")
        with pytest.raises(FieldDoesNotExist):
            reverted_lease._meta.get_field("capability_schema_version")
        with pytest.raises(LookupError):
            reverted_apps.get_model("django_ray", "TaskExecutionProtocolPolicy")
        with pytest.raises(LookupError):
            reverted_apps.get_model("django_ray", "LegacyWorkerAdmissionToken")
    finally:
        _cleanup_rollback_fence_test_state(latest)

    _assert_current_schema_retains_protocol_triggers()


@pytest.mark.django_db(transaction=True)
def test_execution_protocol_schema_migration_is_legacy_safe_and_reversible() -> None:
    _assert_execution_protocol_schema_migration_round_trip()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_execution_protocol_schema_migration_uses_production_database() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_execution_protocol_schema_migration_round_trip()


def _normalize_open_v1_policy(apps) -> None:
    policy_model = apps.get_model("django_ray", "TaskExecutionProtocolPolicy")
    token_model = apps.get_model("django_ray", "LegacyWorkerAdmissionToken")
    policy_model.objects.update_or_create(
        singleton_key=1,
        defaults={
            "schema_version": 1,
            "active_write_protocol_version": 1,
            "legacy_worker_admission_enabled": True,
            "revision": 1,
            "updated_at": timezone.now(),
        },
    )
    token_model.objects.get_or_create(singleton_key=1)


def _cleanup_rollback_fence_test_state(latest: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(ROLLBACK_FENCE_FROM)
    apps = executor.loader.project_state(ROLLBACK_FENCE_FROM).apps
    execution_model = apps.get_model("django_ray", "RayTaskExecution")
    execution_model.objects.filter(state__in=("QUEUED", "RUNNING", "CANCELLING")).exclude(
        execution_protocol_version=1
    ).delete()
    _normalize_open_v1_policy(apps)
    MigrationExecutor(connection).migrate(latest)


def _assert_legacy_open_rollback_fence_round_trip() -> None:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("django_ray")
    executor.migrate(ROLLBACK_FENCE_FROM)
    try:
        old_apps = executor.loader.project_state(ROLLBACK_FENCE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        _normalize_open_v1_policy(old_apps)
        terminal_v2 = old_execution.objects.create(
            task_id="rollback-fence-terminal-v2",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state="SUCCEEDED",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(ROLLBACK_FENCE_TO)
        new_apps = executor.loader.project_state(ROLLBACK_FENCE_TO).apps
        new_execution = new_apps.get_model("django_ray", "RayTaskExecution")
        policy_model = new_apps.get_model("django_ray", "TaskExecutionProtocolPolicy")
        token_model = new_apps.get_model("django_ray", "LegacyWorkerAdmissionToken")

        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS <= _database_trigger_names()

        for state in ("QUEUED", "RUNNING", "CANCELLING"):
            _rejects_integrity_error(
                lambda state=state: new_execution.objects.create(
                    task_id=f"rollback-fence-open-v2-{state.lower()}",
                    callable_path="tests.unit.test_workflows.increment",
                    execution_protocol_version=2,
                    state=state,
                )
            )

        for state in ("QUEUED", "RUNNING", "CANCELLING"):
            _rejects_integrity_error(
                lambda state=state: new_execution.objects.filter(pk=terminal_v2.pk).update(
                    state=state
                )
            )
        assert new_execution.objects.get(pk=terminal_v2.pk).state == "SUCCEEDED"

        v1 = new_execution.objects.create(
            task_id="rollback-fence-open-v1-control",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=1,
            state="FAILED",
        )
        assert new_execution.objects.filter(pk=v1.pk).update(state="QUEUED") == 1
        terminal_control = new_execution.objects.create(
            task_id="rollback-fence-open-terminal-control",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state="FAILED",
        )
        assert new_execution.objects.filter(pk=terminal_control.pk).update(state="LOST") == 1

        policy = policy_model.objects.get(singleton_key=1)
        policy.delete()
        _rejects_integrity_error(
            lambda: new_execution.objects.create(
                task_id="rollback-fence-missing-policy",
                callable_path="tests.unit.test_workflows.increment",
                execution_protocol_version=2,
                state="QUEUED",
            )
        )
        policy_model.objects.create(
            singleton_key=1,
            schema_version=1,
            active_write_protocol_version=1,
            legacy_worker_admission_enabled=True,
            revision=1,
            updated_at=timezone.now(),
        )

        policy_model.objects.filter(singleton_key=1).update(active_write_protocol_version=2)
        _rejects_integrity_error(
            lambda: new_execution.objects.create(
                task_id="rollback-fence-corrupt-open-policy",
                callable_path="tests.unit.test_workflows.increment",
                execution_protocol_version=2,
                state="QUEUED",
            )
        )
        policy_model.objects.filter(singleton_key=1).update(
            active_write_protocol_version=1,
            legacy_worker_admission_enabled=False,
            revision=2,
            updated_at=timezone.now(),
        )
        assert token_model.objects.filter(singleton_key=1).delete()[0] == 1

        closed_insert = new_execution.objects.create(
            task_id="rollback-fence-closed-v2-insert",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state="QUEUED",
        )
        assert closed_insert.state == "QUEUED"
        assert new_execution.objects.filter(pk=terminal_v2.pk).update(state="RUNNING") == 1

        executor = MigrationExecutor(connection)
        executor.migrate(ROLLBACK_FENCE_FROM)
        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS.isdisjoint(_database_trigger_names())
        reverted_apps = executor.loader.project_state(ROLLBACK_FENCE_FROM).apps
        _normalize_open_v1_policy(reverted_apps)
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        unfenced = reverted_execution.objects.create(
            task_id="rollback-fence-reversed-v2",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state="QUEUED",
        )
        assert unfenced.state == "QUEUED"
    finally:
        _cleanup_rollback_fence_test_state(latest)


@pytest.mark.django_db(transaction=True)
def test_legacy_open_rollback_fence_is_database_authoritative_and_reversible() -> None:
    _assert_legacy_open_rollback_fence_round_trip()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_legacy_open_rollback_fence_uses_production_database() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_legacy_open_rollback_fence_round_trip()


def _assert_rollback_fence_installation_diagnostic() -> None:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("django_ray")
    executor.migrate(ROLLBACK_FENCE_FROM)
    try:
        old_apps = executor.loader.project_state(ROLLBACK_FENCE_FROM).apps
        _normalize_open_v1_policy(old_apps)
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        policy_model = old_apps.get_model("django_ray", "TaskExecutionProtocolPolicy")
        token_model = old_apps.get_model("django_ray", "LegacyWorkerAdmissionToken")
        blocker = old_execution.objects.create(
            task_id="rollback-fence-installation-blocker",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state="QUEUED",
        )
        old_execution.objects.create(
            task_id="rollback-fence-installation-terminal-control",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state="SUCCEEDED",
        )

        message = "legacy admission is open with 1 non-v1 nonterminal execution"
        with pytest.raises(RuntimeError, match=message):
            MigrationExecutor(connection).migrate(ROLLBACK_FENCE_TO)
        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS.isdisjoint(_database_trigger_names())

        policy_model.objects.filter(singleton_key=1).update(
            legacy_worker_admission_enabled=False,
            revision=2,
            updated_at=timezone.now(),
        )
        assert token_model.objects.filter(singleton_key=1).delete()[0] == 1
        MigrationExecutor(connection).migrate(ROLLBACK_FENCE_TO)
        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS <= _database_trigger_names()
        assert old_execution.objects.filter(pk=blocker.pk).exists()
    finally:
        _cleanup_rollback_fence_test_state(latest)


@pytest.mark.django_db(transaction=True)
def test_rollback_fence_migration_diagnoses_incompatible_open_work() -> None:
    _assert_rollback_fence_installation_diagnostic()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_rollback_fence_migration_diagnoses_incompatible_open_work() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_rollback_fence_installation_diagnostic()


def _assert_rollback_fence_migration_rejects_corrupt_policy_state() -> None:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("django_ray")
    executor.migrate(ROLLBACK_FENCE_FROM)
    try:
        old_apps = executor.loader.project_state(ROLLBACK_FENCE_FROM).apps
        policy_model = old_apps.get_model("django_ray", "TaskExecutionProtocolPolicy")
        token_model = old_apps.get_model("django_ray", "LegacyWorkerAdmissionToken")
        _normalize_open_v1_policy(old_apps)

        policy_model.objects.get(singleton_key=1).delete()
        with pytest.raises(RuntimeError, match="exactly one consistent"):
            MigrationExecutor(connection).migrate(ROLLBACK_FENCE_TO)
        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS.isdisjoint(_database_trigger_names())

        policy_model.objects.create(
            singleton_key=1,
            schema_version=1,
            active_write_protocol_version=1,
            legacy_worker_admission_enabled=True,
            revision=1,
            updated_at=timezone.now(),
        )
        token_model.objects.get(singleton_key=1).delete()
        with pytest.raises(RuntimeError, match="exactly one consistent"):
            MigrationExecutor(connection).migrate(ROLLBACK_FENCE_TO)
        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS.isdisjoint(_database_trigger_names())

        token_model.objects.create(singleton_key=1)
        policy_model.objects.filter(singleton_key=1).update(
            legacy_worker_admission_enabled=False,
            revision=2,
            updated_at=timezone.now(),
        )
        with pytest.raises(RuntimeError, match="exactly one consistent"):
            MigrationExecutor(connection).migrate(ROLLBACK_FENCE_TO)
        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS.isdisjoint(_database_trigger_names())

        token_model.objects.get(singleton_key=1).delete()
        MigrationExecutor(connection).migrate(ROLLBACK_FENCE_TO)
        assert EXPECTED_ROLLBACK_FENCE_TRIGGERS <= _database_trigger_names()
    finally:
        _cleanup_rollback_fence_test_state(latest)


@pytest.mark.django_db(transaction=True)
def test_rollback_fence_migration_rejects_corrupt_policy_state() -> None:
    _assert_rollback_fence_migration_rejects_corrupt_policy_state()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_rollback_fence_migration_rejects_corrupt_policy_state() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    _assert_rollback_fence_migration_rejects_corrupt_policy_state()


def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def _postgresql_backend_pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


def _wait_for_postgresql_lock(backend_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT wait_event_type
                FROM pg_stat_activity
                WHERE pid = %s
                """,
                [backend_pid],
            )
            row = cursor.fetchone()
        if row is not None and row[0] == "Lock":
            return
        time.sleep(0.01)
    raise AssertionError("the PostgreSQL backend did not enter a lock wait")


def _close_current_legacy_admission():
    return close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
@pytest.mark.parametrize("write_kind", ["insert", "retry"])
def test_postgresql_incompatible_writer_first_blocks_reopen(write_kind: str) -> None:
    _require_postgresql()
    closed = _close_current_legacy_admission()
    terminal = None
    if write_kind == "retry":
        terminal = RayTaskExecution.objects.create(
            task_id="rollback-race-writer-first-terminal",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state=TaskState.FAILED,
        )

    write_complete = Event()
    release_writer = Event()
    reopen_started = Event()
    reopen_backend_pid: list[int] = []

    def write_first() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                if write_kind == "insert":
                    RayTaskExecution.objects.create(
                        task_id="rollback-race-writer-first-insert",
                        callable_path="tests.unit.test_workflows.increment",
                        execution_protocol_version=2,
                        state=TaskState.QUEUED,
                    )
                else:
                    assert terminal is not None
                    assert retry_task(terminal.pk) is not None
                write_complete.set()
                if not release_writer.wait(timeout=10):
                    raise TimeoutError("test did not release the incompatible writer")
        finally:
            close_old_connections()

    def reopen_after_write() -> int:
        close_old_connections()
        try:
            reopen_backend_pid.append(_postgresql_backend_pid())
            reopen_started.set()
            try:
                reopen_legacy_worker_admission(expected_revision=closed.revision)
            except LegacyWorkerRollbackBlockedError as error:
                return error.incompatible_nonterminal_execution_count
            raise AssertionError("reopen unexpectedly admitted incompatible work")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(write_first)
        assert write_complete.wait(timeout=10)
        reopening = executor.submit(reopen_after_write)
        assert reopen_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(reopen_backend_pid[0])
        finally:
            release_writer.set()
        writer.result(timeout=20)
        assert reopening.result(timeout=20) == 1

    policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
    assert policy.legacy_worker_admission_enabled is False
    assert policy.revision == closed.revision
    assert not LegacyWorkerAdmissionToken.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
@pytest.mark.parametrize("write_kind", ["insert", "retry"])
def test_postgresql_reopen_first_rejects_incompatible_writer(
    write_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    closed = _close_current_legacy_admission()
    terminal = None
    if write_kind == "retry":
        terminal = RayTaskExecution.objects.create(
            task_id="rollback-race-reopen-first-terminal",
            callable_path="tests.unit.test_workflows.increment",
            execution_protocol_version=2,
            state=TaskState.FAILED,
        )

    policy_locked = Event()
    release_reopen = Event()
    writer_started = Event()
    writer_backend_pid: list[int] = []
    original_lock = protocol_coordination._postgresql_lock_policy

    def hold_policy_lock(*, using: str) -> TaskExecutionProtocolPolicy:
        policy = original_lock(using=using)
        policy_locked.set()
        if not release_reopen.wait(timeout=10):
            raise TimeoutError("test did not release the reopen transition")
        return policy

    monkeypatch.setattr(protocol_coordination, "_postgresql_lock_policy", hold_policy_lock)

    def reopen_first():
        close_old_connections()
        try:
            return reopen_legacy_worker_admission(expected_revision=closed.revision)
        finally:
            close_old_connections()

    def write_after_reopen_lock() -> str:
        close_old_connections()
        try:
            writer_backend_pid.append(_postgresql_backend_pid())
            writer_started.set()
            try:
                if write_kind == "insert":
                    RayTaskExecution.objects.create(
                        task_id="rollback-race-reopen-first-insert",
                        callable_path="tests.unit.test_workflows.increment",
                        execution_protocol_version=2,
                        state=TaskState.QUEUED,
                    )
                else:
                    assert terminal is not None
                    retry_task(terminal.pk)
            except IntegrityError:
                return "rejected"
            return "written"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reopening = executor.submit(reopen_first)
        assert policy_locked.wait(timeout=10)
        writer = executor.submit(write_after_reopen_lock)
        assert writer_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(writer_backend_pid[0])
        finally:
            release_reopen.set()
        reopened = reopening.result(timeout=20)
        assert writer.result(timeout=20) == "rejected"

    policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
    assert reopened.changed is True
    assert reopened.enabled is True
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == closed.revision + 1
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()
    if terminal is not None:
        terminal.refresh_from_db()
        assert terminal.state == TaskState.FAILED
