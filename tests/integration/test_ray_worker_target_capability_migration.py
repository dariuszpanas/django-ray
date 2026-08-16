"""Migration and database-fence coverage for dormant worker target capacity."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import (
    DatabaseError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    models,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from django_ray.execution_protocol import WORKER_CAPABILITY_SCHEMA_VERSION
from django_ray.models import (
    RAY_JOB_WORKER_TARGET_CAPABILITY_LIMIT,
    RAY_WORKER_TARGET_CAPABILITY_SCHEMA_VERSION,
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayWorkerTargetCapability,
    TaskWorkerLease,
)
from django_ray.target.attestation import (
    RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayRunnerFamily,
)

MIGRATE_FROM = [("django_ray", "0024_ray_target_routes")]
MIGRATE_TO = [("django_ray", "0025_ray_worker_target_capabilities")]
LATEST = [("django_ray", "0026_ray_task_target_execution_evidence")]

_DIGEST = f"sha256:{'a' * 64}"
_POSTGRESQL_TRIGGERS = {
    "ray_wtcap_guard_0025",
    "ray_wtcap_lease_guard_0025",
}
_SQLITE_TRIGGERS = {
    "ray_wtcap_insert_0025",
    "ray_wtcap_update_0025",
    "ray_wtcap_lease_update_0025",
}


class _RecordingSchemaEditor:
    def __init__(self, vendor: str) -> None:
        self.connection = SimpleNamespace(vendor=vendor, alias="default")
        self.statements: list[str] = []

    @staticmethod
    def quote_name(name: str) -> str:
        return f'"{name}"'

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class _ExistingRows:
    def using(self, alias: str) -> _ExistingRows:
        del alias
        return self

    @staticmethod
    def exists() -> bool:
        return True


class _RollbackGuardApps:
    _TABLES = {
        "RayWorkerTargetCapability": "django_ray_rayworkertargetcapability",
        "TaskWorkerLease": "django_ray_taskworkerlease",
        "RayTarget": "django_ray_raytarget",
        "RayTargetPolicyRevision": "django_ray_raytargetpolicyrevision",
        "RayTargetAttestationRevision": "django_ray_raytargetattestationrevision",
    }

    @classmethod
    def get_model(cls, app_label: str, model_name: str) -> object:
        assert app_label == "django_ray"
        return SimpleNamespace(
            _meta=SimpleNamespace(db_table=cls._TABLES[model_name]),
            objects=_ExistingRows(),
        )


def _create_lease(*, worker_id: str = "target-capability-worker") -> TaskWorkerLease:
    now = timezone.now()
    return TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="worker.example",
        pid=1234,
        queue_name="default",
        capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
        django_ray_version="0.5.dev",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
        started_at=now,
        last_heartbeat_at=now,
        is_active=True,
        stopped_at=None,
    )


def _create_target_history(
    *,
    target_key: str = "capacity-primary",
    runner_family: RayRunnerFamily = RayRunnerFamily.RAY_CORE,
    policy_revision: int = 1,
    desired_state: RayTargetDesiredState = RayTargetDesiredState.ACTIVE,
    attestation_revision: int = 1,
) -> tuple[RayTarget, RayTargetPolicyRevision, RayTargetAttestationRevision]:
    target = RayTarget.objects.create(
        target_key=target_key,
        runner_family=runner_family,
        cluster_session=f"session_{target_key}",
        ray_major=2,
        ray_minor=56,
        ray_patch=0,
        python_implementation="cpython",
        python_major=3,
        python_minor=12,
        python_patch=12,
    )
    policy = RayTargetPolicyRevision.objects.create(
        target=target,
        revision=policy_revision,
        desired_state=desired_state,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json='{"schema":"expectation"}',
        expectation_digest=_DIGEST,
    )
    observed_at = timezone.now() - timedelta(seconds=2)
    attestation = RayTargetAttestationRevision.objects.create(
        policy=policy,
        revision=attestation_revision,
        attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        attestation_json='{"schema":"attestation"}',
        expectation_digest=_DIGEST,
        membership_digest=_DIGEST,
        attestation_digest=_DIGEST,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=60),
        recorded_at=observed_at + timedelta(seconds=1),
    )
    return target, policy, attestation


def _append_policy_attestation(
    target: RayTarget,
    *,
    revision: int,
    desired_state: RayTargetDesiredState,
) -> tuple[RayTargetPolicyRevision, RayTargetAttestationRevision]:
    policy = RayTargetPolicyRevision.objects.create(
        target=target,
        revision=revision,
        desired_state=desired_state,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json=f'{{"schema":"expectation-{revision}"}}',
        expectation_digest=_DIGEST,
    )
    observed_at = timezone.now() - timedelta(seconds=2)
    attestation = RayTargetAttestationRevision.objects.create(
        policy=policy,
        revision=1,
        attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        attestation_json=f'{{"schema":"attestation-{revision}"}}',
        expectation_digest=_DIGEST,
        membership_digest=_DIGEST,
        attestation_digest=_DIGEST,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=60),
        recorded_at=observed_at + timedelta(seconds=1),
    )
    return policy, attestation


def _create_capability(
    lease: TaskWorkerLease,
    target: RayTarget,
    policy: RayTargetPolicyRevision,
    attestation: RayTargetAttestationRevision,
    *,
    revision: int = 1,
    advertised_at=None,
) -> RayWorkerTargetCapability:
    now = advertised_at or timezone.now()
    return RayWorkerTargetCapability.objects.create(
        lease=lease,
        lease_hostname=lease.hostname,
        lease_pid=lease.pid,
        lease_started_at=lease.started_at,
        target=target,
        target_policy=policy,
        attestation=attestation,
        runner_family=target.runner_family,
        manager_ray_major=target.ray_major,
        manager_ray_minor=target.ray_minor,
        manager_ray_patch=target.ray_patch,
        manager_python_implementation=target.python_implementation,
        manager_python_major=target.python_major,
        manager_python_minor=target.python_minor,
        manager_python_patch=target.python_patch,
        revision=revision,
        created_at=now,
        advertised_at=now,
    )


def _database_trigger_names() -> set[str]:
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        elif connection.vendor == "postgresql":
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        else:
            raise AssertionError(f"unsupported database vendor: {connection.vendor}")
        return {str(row[0]) for row in cursor.fetchall()}


def _clear_capability_tables() -> None:
    table_names = set(connection.introspection.table_names())
    if RayWorkerTargetCapability._meta.db_table in table_names:
        RayWorkerTargetCapability.objects.all().delete()
    TaskWorkerLease.objects.all().delete()
    RayTargetAttestationRevision.objects.all().delete()
    RayTargetPolicyRevision.objects.all().delete()
    RayTarget.objects.all().delete()


def _raw_insert(model: type[models.Model], values: dict[str, object]) -> None:
    quote = connection.ops.quote_name
    columns = ", ".join(quote(column) for column in values)
    placeholders = ", ".join(["%s"] * len(values))
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {quote(model._meta.db_table)} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )


def _rejects_database_error(operation: Callable[[], object]) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        operation()


def _close_owned_thread_connection() -> None:
    thread_connection = connections["default"]
    raw_connection = thread_connection.connection
    if raw_connection is None:
        return
    try:
        raw_connection.rollback()
    finally:
        raw_connection.close()
        thread_connection.connection = None


def test_model_exposes_ephemeral_cas_capability_contract() -> None:
    assert RAY_WORKER_TARGET_CAPABILITY_SCHEMA_VERSION == 1
    assert RAY_JOB_WORKER_TARGET_CAPABILITY_LIMIT == 64
    assert all(
        field.auto_created or field.editable is False
        for field in RayWorkerTargetCapability._meta.fields
    )
    assert (
        RayWorkerTargetCapability._meta.get_field("lease").remote_field.on_delete is models.CASCADE
    )
    for field_name in ("target", "target_policy", "attestation"):
        assert (
            RayWorkerTargetCapability._meta.get_field(field_name).remote_field.on_delete
            is models.PROTECT
        )
    assert RayWorkerTargetCapability._meta.get_field("schema_version").default == 1
    assert RayWorkerTargetCapability._meta.get_field("schema_version").db_default == 1
    assert {constraint.name for constraint in RayWorkerTargetCapability._meta.constraints} == {
        "ray_wtcap_id_valid",
        "ray_wtcap_lease_pid_valid",
        "ray_wtcap_runner_valid",
        "ray_wtcap_runtime_valid",
        "ray_wtcap_schema_valid",
        "ray_wtcap_revision_valid",
        "ray_wtcap_time_valid",
        "ray_wtcap_lease_target_uniq",
    }
    assert (
        str(RayWorkerTargetCapability(lease_id="worker", target_id="blue", revision=3))
        == "lease worker target blue revision 3"
    )

    invalid = RayWorkerTargetCapability(
        lease_pid=0,
        runner_family="unsupported",
        manager_ray_major=0,
        manager_ray_minor=0,
        manager_ray_patch=0,
        manager_python_implementation="CPython",
        manager_python_major=0,
        manager_python_minor=0,
        manager_python_patch=0,
        revision=0,
    )
    with pytest.raises(ValidationError):
        invalid.full_clean(
            exclude={"lease", "target", "target_policy", "attestation"},
            validate_constraints=False,
        )


def test_migration_sql_installs_cross_table_cas_cardinality_and_reverse_fences() -> None:
    migration = importlib.import_module("django_ray.migrations.0025_ray_worker_target_capabilities")
    postgresql = _RecordingSchemaEditor("postgresql")
    migration._install_capability_fences(django_apps, postgresql)
    postgresql_sql = "\n".join(postgresql.statements)
    for trigger in _POSTGRESQL_TRIGGERS:
        assert f"CREATE TRIGGER {trigger}" in postgresql_sql
    assert "FOR UPDATE" in postgresql_sql
    assert "FOR SHARE" in postgresql_sql
    assert "policy.desired_state IN ('active', 'draining')" in postgresql_sql
    assert "NEW.revision <> OLD.revision + 1" in postgresql_sql
    assert "existing_count >= 64" in postgresql_sql
    assert "attestation.policy_id = policy.id" in postgresql_sql

    postgresql.statements.clear()
    migration._remove_capability_fences(django_apps, postgresql)
    removed_sql = "\n".join(postgresql.statements)
    for trigger in _POSTGRESQL_TRIGGERS:
        assert f'DROP TRIGGER IF EXISTS "{trigger}" ON' in removed_sql
    assert removed_sql.count("DROP FUNCTION IF EXISTS") == 2

    sqlite = _RecordingSchemaEditor("sqlite")
    migration._install_capability_fences(django_apps, sqlite)
    sqlite_sql = "\n".join(sqlite.statements)
    for trigger in _SQLITE_TRIGGERS:
        assert f"CREATE TRIGGER {trigger}" in sqlite_sql
    assert "typeof(NEW.id) != 'integer'" in sqlite_sql
    assert "NEW.id != -1 AND NEW.id < 1" in sqlite_sql
    assert "instr(NEW.lease_hostname, char(0)) != 0" in sqlite_sql
    assert "strftime('%Y-%m-%d %H:%M:%S'" in sqlite_sql
    assert "policy.desired_state IN ('active', 'draining')" in sqlite_sql
    assert "NEW.revision != OLD.revision + 1" in sqlite_sql
    assert ") >= 64" in sqlite_sql

    unsupported = _RecordingSchemaEditor("mysql")
    message = "capabilities support only SQLite and PostgreSQL"
    with pytest.raises(RuntimeError, match=message):
        migration._install_capability_fences(django_apps, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._remove_capability_fences(django_apps, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._guard_empty_capabilities(_RollbackGuardApps(), unsupported)

    operations = migration.Migration.operations
    assert operations[-1].reverse_code is migration._guard_empty_capabilities
    assert operations[-2].code is migration._install_capability_fences
    postgresql.statements.clear()
    with pytest.raises(RuntimeError, match="rollback requires the table to be empty"):
        migration._guard_empty_capabilities(_RollbackGuardApps(), postgresql)
    assert postgresql.statements == [
        'LOCK TABLE "django_ray_rayworkertargetcapability" IN ACCESS EXCLUSIVE MODE'
    ]


def _assert_database_fences() -> None:
    expected = _SQLITE_TRIGGERS if connection.vendor == "sqlite" else _POSTGRESQL_TRIGGERS
    assert expected <= _database_trigger_names()

    lease = _create_lease()
    target, policy, attestation = _create_target_history()
    capability = _create_capability(lease, target, policy, attestation)
    assert capability.pk >= 1
    assert capability.revision == 1

    snapshot_lease = _create_lease(worker_id="target-capability-snapshot-mismatch")
    snapshot_values = {
        "lease": snapshot_lease,
        "lease_hostname": snapshot_lease.hostname,
        "lease_pid": snapshot_lease.pid,
        "lease_started_at": snapshot_lease.started_at,
        "target": target,
        "target_policy": policy,
        "attestation": attestation,
        "runner_family": target.runner_family,
        "manager_ray_major": target.ray_major,
        "manager_ray_minor": target.ray_minor,
        "manager_ray_patch": target.ray_patch,
        "manager_python_implementation": target.python_implementation,
        "manager_python_major": target.python_major,
        "manager_python_minor": target.python_minor,
        "manager_python_patch": target.python_patch,
        "revision": 1,
    }
    for field, value in (
        ("lease_hostname", "replacement.example"),
        ("lease_pid", cast(int, snapshot_lease.pid) + 1),
        (
            "lease_started_at",
            cast(datetime, snapshot_lease.started_at) + timedelta(microseconds=1),
        ),
    ):
        invalid_snapshot = {**snapshot_values, field: value}
        _rejects_database_error(
            lambda invalid_snapshot=invalid_snapshot: RayWorkerTargetCapability.objects.create(
                **invalid_snapshot
            )
        )

    assert (
        RayWorkerTargetCapability.objects.filter(pk=capability.pk).update(
            revision=models.F("revision"),
            advertised_at=models.F("advertised_at"),
        )
        == 1
    )
    later = cast(datetime, capability.advertised_at) + timedelta(microseconds=1)
    assert (
        RayWorkerTargetCapability.objects.filter(pk=capability.pk, revision=1).update(
            revision=2,
            advertised_at=later,
        )
        == 1
    )
    capability.refresh_from_db()
    assert capability.revision == 2
    assert capability.advertised_at == later

    _rejects_database_error(
        lambda: RayWorkerTargetCapability.objects.filter(pk=capability.pk).update(revision=4)
    )
    _rejects_database_error(
        lambda: RayWorkerTargetCapability.objects.filter(pk=capability.pk).update(
            manager_ray_patch=1,
            revision=3,
            advertised_at=later + timedelta(microseconds=1),
        )
    )
    _rejects_database_error(
        lambda: TaskWorkerLease.objects.filter(pk=lease.pk).update(hostname="replacement")
    )

    other_target, other_policy, other_attestation = _create_target_history(
        target_key="capacity-secondary"
    )
    _rejects_database_error(
        lambda: _create_capability(
            lease,
            other_target,
            other_policy,
            other_attestation,
        )
    )
    job_target, job_policy, job_attestation = _create_target_history(
        target_key="capacity-secondary-job",
        runner_family=RayRunnerFamily.RAY_JOB,
    )
    _rejects_database_error(
        lambda: _create_capability(lease, job_target, job_policy, job_attestation)
    )
    stopped_lease = _create_lease(worker_id="target-capability-stopped")
    stopped_lease.stopped_at = timezone.now()
    stopped_lease.save(update_fields=["stopped_at"])
    _rejects_database_error(lambda: _create_capability(stopped_lease, target, policy, attestation))
    mismatch_lease = _create_lease(worker_id="target-capability-mismatch")
    _rejects_database_error(
        lambda: RayWorkerTargetCapability.objects.create(
            lease=mismatch_lease,
            lease_hostname=mismatch_lease.hostname,
            lease_pid=mismatch_lease.pid,
            lease_started_at=mismatch_lease.started_at,
            target=target,
            target_policy=other_policy,
            attestation=attestation,
            runner_family=target.runner_family,
            manager_ray_major=target.ray_major,
            manager_ray_minor=target.ray_minor,
            manager_ray_patch=target.ray_patch,
            manager_python_implementation=target.python_implementation,
            manager_python_major=target.python_major,
            manager_python_minor=target.python_minor,
            manager_python_patch=target.python_patch,
            revision=1,
        )
    )

    retired_policy = RayTargetPolicyRevision.objects.create(
        target=other_target,
        revision=2,
        desired_state=RayTargetDesiredState.RETIRED,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json='{"schema":"retired"}',
        expectation_digest=_DIGEST,
    )
    retired_observed = timezone.now() - timedelta(seconds=2)
    retired_attestation = RayTargetAttestationRevision.objects.create(
        policy=retired_policy,
        revision=1,
        attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        attestation_json='{"schema":"retired-attestation"}',
        expectation_digest=_DIGEST,
        membership_digest=_DIGEST,
        attestation_digest=_DIGEST,
        observed_at=retired_observed,
        expires_at=retired_observed + timedelta(seconds=60),
        recorded_at=retired_observed + timedelta(seconds=1),
    )
    _rejects_database_error(
        lambda: _create_capability(
            mismatch_lease,
            other_target,
            retired_policy,
            retired_attestation,
        )
    )

    with pytest.raises(ProtectedError):
        target.delete()
    with pytest.raises(ProtectedError):
        policy.delete()
    with pytest.raises(ProtectedError):
        attestation.delete()

    def raw_delete_lease() -> None:
        table = connection.ops.quote_name(TaskWorkerLease._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE worker_id = %s", [lease.pk])

    _rejects_database_error(raw_delete_lease)
    assert TaskWorkerLease.objects.filter(pk=lease.pk).exists()
    assert RayWorkerTargetCapability.objects.filter(pk=capability.pk).exists()

    lease.delete()
    assert not RayWorkerTargetCapability.objects.filter(pk=capability.pk).exists()
    snapshot_lease.delete()


@pytest.mark.django_db(transaction=True)
def test_sqlite_capability_cross_table_cas_cardinality_and_parent_fences() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_database_fences()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_capability_cross_table_cas_cardinality_and_parent_fences() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_database_fences()


def _assert_latest_history_draining_window_and_job_limit() -> None:
    lease = _create_lease(worker_id="capacity-history-worker")
    stale_target, stale_policy, stale_attestation = _create_target_history(
        target_key="capacity-stale-policy"
    )
    current_policy, current_attestation = _append_policy_attestation(
        stale_target,
        revision=2,
        desired_state=RayTargetDesiredState.DRAINING,
    )
    _rejects_database_error(
        lambda: _create_capability(lease, stale_target, stale_policy, stale_attestation)
    )
    draining = _create_capability(
        lease,
        stale_target,
        current_policy,
        current_attestation,
    )
    assert draining.target_policy.desired_state == RayTargetDesiredState.DRAINING
    draining.delete()

    attestation_target, attestation_policy, old_attestation = _create_target_history(
        target_key="capacity-stale-attestation"
    )
    observed_at = timezone.now() - timedelta(seconds=2)
    latest_attestation = RayTargetAttestationRevision.objects.create(
        policy=attestation_policy,
        revision=2,
        attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        attestation_json='{"schema":"attestation-2"}',
        expectation_digest=_DIGEST,
        membership_digest=_DIGEST,
        attestation_digest=_DIGEST,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=60),
        recorded_at=observed_at + timedelta(seconds=1),
    )
    _rejects_database_error(
        lambda: _create_capability(
            lease,
            attestation_target,
            attestation_policy,
            old_attestation,
        )
    )
    _rejects_database_error(
        lambda: _create_capability(
            lease,
            attestation_target,
            attestation_policy,
            latest_attestation,
            advertised_at=cast(datetime, latest_attestation.recorded_at)
            - timedelta(microseconds=1),
        )
    )
    _rejects_database_error(
        lambda: _create_capability(
            lease,
            attestation_target,
            attestation_policy,
            latest_attestation,
            advertised_at=latest_attestation.expires_at,
        )
    )
    fresh = _create_capability(
        lease,
        attestation_target,
        attestation_policy,
        latest_attestation,
    )
    fresh.delete()

    inactive = _create_lease(worker_id="capacity-inactive-worker")
    TaskWorkerLease.objects.filter(pk=inactive.pk).update(is_active=False)
    _rejects_database_error(
        lambda: _create_capability(
            inactive,
            attestation_target,
            attestation_policy,
            latest_attestation,
        )
    )
    legacy = TaskWorkerLease.objects.create(
        worker_id="capacity-legacy-worker",
        hostname="worker.example",
        pid=1235,
        queue_name="default",
    )
    _rejects_database_error(
        lambda: _create_capability(
            legacy,
            attestation_target,
            attestation_policy,
            latest_attestation,
        )
    )
    mismatch = _create_lease(worker_id="capacity-runtime-mismatch")
    _rejects_database_error(
        lambda: RayWorkerTargetCapability.objects.create(
            lease=mismatch,
            lease_hostname=mismatch.hostname,
            lease_pid=mismatch.pid,
            lease_started_at=mismatch.started_at,
            target=attestation_target,
            target_policy=attestation_policy,
            attestation=latest_attestation,
            runner_family=attestation_target.runner_family,
            manager_ray_major=2,
            manager_ray_minor=56,
            manager_ray_patch=1,
            manager_python_implementation="cpython",
            manager_python_major=3,
            manager_python_minor=12,
            manager_python_patch=12,
            revision=1,
        )
    )

    job_lease = _create_lease(worker_id="capacity-job-worker")
    job_capabilities: list[RayWorkerTargetCapability] = []
    for index in range(RAY_JOB_WORKER_TARGET_CAPABILITY_LIMIT):
        target, policy, attestation = _create_target_history(
            target_key=f"capacity-job-{index:02d}",
            runner_family=RayRunnerFamily.RAY_JOB,
        )
        job_capabilities.append(_create_capability(job_lease, target, policy, attestation))
    assert len(job_capabilities) == 64
    overflow_target, overflow_policy, overflow_attestation = _create_target_history(
        target_key="capacity-job-overflow",
        runner_family=RayRunnerFamily.RAY_JOB,
    )
    _rejects_database_error(
        lambda: _create_capability(
            job_lease,
            overflow_target,
            overflow_policy,
            overflow_attestation,
        )
    )
    core_target, core_policy, core_attestation = _create_target_history(
        target_key="capacity-core-mixed"
    )
    _rejects_database_error(
        lambda: _create_capability(job_lease, core_target, core_policy, core_attestation)
    )


@pytest.mark.django_db(transaction=True)
def test_sqlite_capability_requires_latest_draining_or_active_proof_and_bounds_jobs() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_latest_history_draining_window_and_job_limit()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_capability_requires_latest_draining_or_active_proof_and_bounds_jobs() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_latest_history_draining_window_and_job_limit()


def _invalid_sqlite_datetimes() -> tuple[object, ...]:
    return (
        None,
        "now",
        "0000-01-01 00:00:00",
        "2026-01-01 24:00:00",
        "2026-02-30 20:00:00",
        "2026-08-15T20:00:00",
        "2026-08-15 20:00:00.12345",
        "2026-08-15 20:00:00\x00suffix",
        b"2026-08-15 20:00:00",
        2.5,
    )


def _raw_capability_values(
    lease: TaskWorkerLease,
    target: RayTarget,
    policy: RayTargetPolicyRevision,
    attestation: RayTargetAttestationRevision,
) -> dict[str, object]:
    now = timezone.now()
    stored_now = now.astimezone(UTC).replace(tzinfo=None) if connection.vendor == "sqlite" else now
    stored_started_at = (
        lease.started_at.astimezone(UTC).replace(tzinfo=None)
        if connection.vendor == "sqlite"
        else lease.started_at
    )
    return {
        "id": 1001,
        "lease_id": lease.pk,
        "lease_hostname": lease.hostname,
        "lease_pid": lease.pid,
        "lease_started_at": stored_started_at,
        "target_id": target.pk,
        "target_policy_id": policy.pk,
        "attestation_id": attestation.pk,
        "runner_family": target.runner_family,
        "manager_ray_major": target.ray_major,
        "manager_ray_minor": target.ray_minor,
        "manager_ray_patch": target.ray_patch,
        "manager_python_implementation": target.python_implementation,
        "manager_python_major": target.python_major,
        "manager_python_minor": target.python_minor,
        "manager_python_patch": target.python_patch,
        "schema_version": 1,
        "revision": 1,
        "created_at": stored_now,
        "advertised_at": stored_now,
    }


def _assert_raw_storage_guards(*, sqlite_dynamic_types: bool) -> None:
    lease = _create_lease(worker_id="capacity-raw-worker")
    target, policy, attestation = _create_target_history(target_key="capacity-raw")
    values = _raw_capability_values(lease, target, policy, attestation)
    mutations: list[tuple[str, object]] = [
        ("id", 0),
        ("id", -1),
        ("id", -2),
        ("id", "1.5"),
        ("lease_id", ""),
        ("lease_id", "w" * 256),
        ("lease_id", f"{lease.pk}\x00suffix"),
        ("lease_id", str(lease.pk).encode()),
        ("lease_hostname", ""),
        ("lease_hostname", "h" * 256),
        ("lease_hostname", f"{lease.hostname}\x00suffix"),
        ("lease_hostname", lease.hostname.encode()),
        ("lease_pid", 0),
        ("lease_pid", 1 << 31),
        ("target_id", "a" * 129),
        ("target_id", f"{target.pk}\x00suffix"),
        ("target_id", str(target.pk).encode()),
        ("target_policy_id", 0),
        ("target_policy_id", "1.5"),
        ("attestation_id", 0),
        ("attestation_id", "1.5"),
        ("runner_family", "ray_core\x00suffix"),
        ("runner_family", b"ray_core"),
        ("manager_python_implementation", "CPython"),
        ("manager_python_implementation", "a" * 65),
        ("manager_python_implementation", "cpython\x00suffix"),
        ("manager_python_implementation", b"cpython"),
        ("schema_version", 0),
        ("schema_version", 2),
        ("schema_version", "1.5"),
        ("revision", 0),
        ("revision", 2),
        ("revision", "1.5"),
    ]
    for field in (
        "manager_ray_major",
        "manager_ray_minor",
        "manager_ray_patch",
        "manager_python_major",
        "manager_python_minor",
        "manager_python_patch",
    ):
        mutations.extend(((field, -1), (field, "1.5"), (field, b"1")))
        if sqlite_dynamic_types:
            mutations.append((field, 1.5))
    if sqlite_dynamic_types:
        mutations.extend(
            (
                ("id", 1.5),
                ("lease_id", 2.5),
                ("lease_hostname", 2.5),
                ("lease_pid", 1234.5),
                ("target_id", 2.5),
                ("target_policy_id", 1.5),
                ("attestation_id", 1.5),
                ("runner_family", 2.5),
                ("manager_python_implementation", 2.5),
                ("schema_version", 1.5),
                ("revision", 1.5),
            )
        )
    for datetime_field in ("lease_started_at", "created_at", "advertised_at"):
        mutations.extend(
            (datetime_field, value)
            for value in (
                None,
                "-infinity",
                "infinity",
                "0000-01-01 00:00:00",
                "10000-01-01 00:00:00",
                "2026-08-15 20:00:00\x00suffix",
                b"2026-08-15 20:00:00",
            )
        )
        if sqlite_dynamic_types:
            mutations.extend((datetime_field, value) for value in _invalid_sqlite_datetimes())

    for field, value in mutations:
        invalid = {**values, field: value}
        try:
            with transaction.atomic():
                _raw_insert(RayWorkerTargetCapability, invalid)
        except DatabaseError:
            pass
        else:
            pytest.fail(f"raw capability {field}={value!r} was accepted")
        assert RayWorkerTargetCapability.objects.count() == 0

    _raw_insert(RayWorkerTargetCapability, values)
    assert RayWorkerTargetCapability.objects.get(pk=1001).revision == 1
    RayWorkerTargetCapability.objects.all().delete()


@pytest.mark.django_db(transaction=True)
def test_sqlite_raw_capability_rejects_blob_nul_real_and_malformed_dates() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_raw_storage_guards(sqlite_dynamic_types=True)

    lease = _create_lease(worker_id="capacity-invalid-utf8")
    target, policy, attestation = _create_target_history(target_key="capacity-invalid-utf8")
    values = _raw_capability_values(lease, target, policy, attestation)
    quote = connection.ops.quote_name
    columns = ", ".join(quote(column) for column in values)
    expressions = ["%s"] * len(values)
    hostname_index = tuple(values).index("lease_hostname")
    expressions[hostname_index] = "CAST(X'80' AS TEXT)"
    parameters = [value for name, value in values.items() if name != "lease_hostname"]
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {quote(RayWorkerTargetCapability._meta.db_table)} "
            f"({columns}) VALUES ({', '.join(expressions)})",
            parameters,
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_raw_capability_rejects_applicable_invalid_storage() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_raw_storage_guards(sqlite_dynamic_types=False)


def _assert_migration_round_trip_and_reverse_guard() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_lease = old_apps.get_model("django_ray", "TaskWorkerLease")
        old_target = old_apps.get_model("django_ray", "RayTarget")
        old_policy = old_apps.get_model("django_ray", "RayTargetPolicyRevision")
        old_attestation = old_apps.get_model("django_ray", "RayTargetAttestationRevision")

        now = timezone.now()
        legacy_lease = old_lease.objects.create(
            worker_id="capacity-legacy-writer",
            hostname="legacy.example",
            pid=4321,
            queue_name="default",
            started_at=now,
            last_heartbeat_at=now,
            is_active=True,
        )
        capable_lease = old_lease.objects.create(
            worker_id="capacity-round-trip",
            hostname="worker.example",
            pid=1234,
            queue_name="default",
            capability_schema_version=1,
            django_ray_version="0.5.dev",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token_id=None,
            started_at=now,
            last_heartbeat_at=now,
            is_active=True,
        )
        target = old_target.objects.create(
            target_key="capacity-round-trip",
            runner_family="ray_core",
            cluster_session="session_capacity_round_trip",
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
        policy = old_policy.objects.create(
            target_id=target.pk,
            revision=1,
            desired_state="draining",
            expectation_schema_version=1,
            expectation_json='{"schema":"expectation"}',
            expectation_digest=_DIGEST,
        )
        observed_at = now - timedelta(seconds=2)
        attestation = old_attestation.objects.create(
            policy_id=policy.pk,
            revision=1,
            attestation_schema_version=1,
            attestation_json='{"schema":"attestation"}',
            expectation_digest=_DIGEST,
            membership_digest=_DIGEST,
            attestation_digest=_DIGEST,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=60),
            recorded_at=observed_at + timedelta(seconds=1),
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        capability = new_apps.get_model("django_ray", "RayWorkerTargetCapability")
        assert capability.objects.count() == 0
        assert old_lease.objects.filter(pk=legacy_lease.pk).exists()

        rolling_lease = old_lease.objects.create(
            worker_id="capacity-old-writer-after-0025",
            hostname="legacy.example",
            pid=4322,
            queue_name="default",
            started_at=now + timedelta(microseconds=1),
            last_heartbeat_at=now + timedelta(microseconds=1),
            is_active=True,
        )
        assert capability.objects.count() == 0
        assert old_lease.objects.filter(pk=rolling_lease.pk).exists()

        capability_row = capability.objects.create(
            lease_id=capable_lease.pk,
            lease_hostname=capable_lease.hostname,
            lease_pid=capable_lease.pid,
            lease_started_at=capable_lease.started_at,
            target_id=target.pk,
            target_policy_id=policy.pk,
            attestation_id=attestation.pk,
            runner_family="ray_core",
            manager_ray_major=2,
            manager_ray_minor=56,
            manager_ray_patch=0,
            manager_python_implementation="cpython",
            manager_python_major=3,
            manager_python_minor=12,
            manager_python_patch=12,
            revision=1,
            created_at=now,
            advertised_at=now,
        )
        with pytest.raises(RuntimeError, match="rollback requires the table to be empty"):
            MigrationExecutor(connection).migrate(MIGRATE_FROM)
        assert capability.objects.filter(pk=capability_row.pk).exists()
        assert capability._meta.db_table in connection.introspection.table_names()
        expected = _SQLITE_TRIGGERS if connection.vendor == "sqlite" else _POSTGRESQL_TRIGGERS
        assert expected <= _database_trigger_names()

        capability.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        with pytest.raises(LookupError):
            reverted_apps.get_model("django_ray", "RayWorkerTargetCapability")
        reverted_lease = reverted_apps.get_model("django_ray", "TaskWorkerLease")
        assert (
            reverted_lease.objects.filter(
                pk__in=(legacy_lease.pk, capable_lease.pk, rolling_lease.pk)
            ).count()
            == 3
        )
    finally:
        MigrationExecutor(connection).migrate(LATEST)
        _clear_capability_tables()


@pytest.mark.django_db(transaction=True)
def test_sqlite_capabilities_are_additive_old_writer_safe_and_reverse_guarded() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_migration_round_trip_and_reverse_guard()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_capabilities_are_additive_old_writer_safe_and_reverse_guarded() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_migration_round_trip_and_reverse_guard()


def _create_held_capability(
    *,
    lease_id: str,
    target_id: str,
    writer_inserted: Event,
    release_writer: Event,
) -> None:
    close_old_connections()
    try:
        with transaction.atomic():
            lease = TaskWorkerLease.objects.get(pk=lease_id)
            target = RayTarget.objects.get(pk=target_id)
            policy = RayTargetPolicyRevision.objects.get(target=target)
            attestation = RayTargetAttestationRevision.objects.get(policy=policy)
            _create_capability(lease, target, policy, attestation)
            writer_inserted.set()
            if not release_writer.wait(timeout=20):
                raise TimeoutError("test did not release the capability writer")
    finally:
        _close_owned_thread_connection()


def _attempt_sqlite_reverse_with_zero_busy_timeout(reverse_started: Event) -> str:
    close_old_connections()
    try:
        thread_connection = connections["default"]
        with thread_connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout = 0")
        reverse_started.set()
        try:
            MigrationExecutor(thread_connection).migrate(MIGRATE_FROM)
        except OperationalError:
            return "writer-locked"
        except RuntimeError:
            return "history-refused"
        raise AssertionError("rollback unexpectedly removed worker target capabilities")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
def test_sqlite_active_capability_writer_cannot_partially_reverse_schema() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")

    lease = _create_lease(worker_id="capacity-concurrent-writer")
    target, _policy, _attestation = _create_target_history(target_key="capacity-concurrent-writer")
    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(
            _create_held_capability,
            lease_id=lease.pk,
            target_id=target.pk,
            writer_inserted=writer_inserted,
            release_writer=release_writer,
        )
        assert writer_inserted.wait(timeout=20)
        reverse = pool.submit(
            _attempt_sqlite_reverse_with_zero_busy_timeout,
            reverse_started,
        )
        assert reverse_started.wait(timeout=20)
        outcome = reverse.result(timeout=20)
        release_writer.set()
        writer.result(timeout=20)

    assert outcome in {"writer-locked", "history-refused"}
    assert RayWorkerTargetCapability._meta.db_table in connection.introspection.table_names()
    assert (
        RayWorkerTargetCapability.objects.filter(
            lease_id=lease.pk,
            target_id=target.pk,
        ).count()
        == 1
    )
    assert _SQLITE_TRIGGERS <= _database_trigger_names()


def _postgresql_backend_pid() -> int:
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _wait_for_postgresql_lock(backend_pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                [backend_pid],
            )
            row = cursor.fetchone()
        if row is not None and row[0] == "Lock":
            return
        time.sleep(0.05)
    raise TimeoutError("rollback did not wait on the capability writer lock")


def _attempt_postgresql_reverse(
    reverse_started: Event,
    backend_pid: list[int],
) -> str:
    close_old_connections()
    try:
        thread_connection = connections["default"]
        backend_pid.append(_postgresql_backend_pid())
        reverse_started.set()
        try:
            MigrationExecutor(thread_connection).migrate(MIGRATE_FROM)
        except RuntimeError:
            return "history-refused"
        raise AssertionError("rollback unexpectedly removed worker target capabilities")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_capability_writer_serializes_before_reverse_guard() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    lease = _create_lease(worker_id="capacity-concurrent-writer")
    target, _policy, _attestation = _create_target_history(target_key="capacity-concurrent-writer")
    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    backend_pid: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(
                _create_held_capability,
                lease_id=lease.pk,
                target_id=target.pk,
                writer_inserted=writer_inserted,
                release_writer=release_writer,
            )
            assert writer_inserted.wait(timeout=10)
            reverse = pool.submit(
                _attempt_postgresql_reverse,
                reverse_started,
                backend_pid,
            )
            assert reverse_started.wait(timeout=10)
            try:
                _wait_for_postgresql_lock(backend_pid[0])
            finally:
                release_writer.set()
            writer.result(timeout=20)
            assert reverse.result(timeout=20) == "history-refused"

        assert RayWorkerTargetCapability.objects.filter(
            lease_id=lease.pk,
            target_id=target.pk,
        ).exists()
        assert RayWorkerTargetCapability._meta.db_table in connection.introspection.table_names()
        assert _POSTGRESQL_TRIGGERS <= _database_trigger_names()
    finally:
        release_writer.set()
        MigrationExecutor(connection).migrate(LATEST)
        _clear_capability_tables()
