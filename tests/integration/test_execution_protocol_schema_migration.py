"""Migration and dormant-fence coverage for durable execution protocol metadata."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATE_FROM = [("django_ray", "0018_workflow_run_allocation")]
MIGRATE_TO = [("django_ray", "0019_execution_protocol_schema")]

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


def _assert_current_schema_retains_protocol_triggers() -> None:
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        elif connection.vendor == "postgresql":
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        else:
            raise AssertionError(f"unsupported database vendor: {connection.vendor}")
        trigger_names = {str(row[0]) for row in cursor.fetchall()}

    assert EXPECTED_PROTOCOL_TRIGGERS <= trigger_names


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
        MigrationExecutor(connection).migrate(latest)

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
