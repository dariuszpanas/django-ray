"""Migration and database-fence coverage for dormant Ray target persistence."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import (
    DatabaseError,
    DataError,
    IntegrityError,
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

from django_ray.models import (
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
)
from django_ray.target_attestation import (
    RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayRunnerFamily,
)

MIGRATE_FROM = [("django_ray", "0021_ray_job_request_reference")]
MIGRATE_TO = [("django_ray", "0022_ray_target_persistence")]
LATEST = [("django_ray", "0023_ray_task_target_binding")]

_DIGEST = f"sha256:{'a' * 64}"
_TARGET_TRIGGER_NAMES = {
    "ray_target_guard_0022",
    "ray_tpolicy_guard_0022",
    "ray_tattest_guard_0022",
}
_SQLITE_TRIGGER_NAMES = {
    "ray_target_insert_0022",
    "ray_target_update_0022",
    "ray_tpolicy_insert_0022",
    "ray_tpolicy_update_0022",
    "ray_tattest_insert_0022",
    "ray_tattest_update_0022",
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
    @staticmethod
    def get_model(app_label: str, model_name: str) -> object:
        assert app_label == "django_ray"
        table_names = {
            "RayTarget": "django_ray_raytarget",
            "RayTargetPolicyRevision": "django_ray_raytargetpolicyrevision",
            "RayTargetAttestationRevision": "django_ray_raytargetattestationrevision",
        }
        return SimpleNamespace(
            _meta=SimpleNamespace(db_table=table_names[model_name]),
            objects=_ExistingRows(),
        )


def _rejects_integrity_error(operation: Callable[[], object]) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        operation()


def _create_target(*, target_key: str = "primary") -> RayTarget:
    return RayTarget.objects.create(
        target_key=target_key,
        runner_family=RayRunnerFamily.RAY_CORE,
        cluster_session=f"session_{target_key}",
        ray_major=2,
        ray_minor=56,
        ray_patch=0,
        python_implementation="cpython",
        python_major=3,
        python_minor=12,
        python_patch=12,
    )


def _create_policy(
    target: RayTarget,
    *,
    revision: int = 1,
    expectation_json: str = '{"schema":"expectation"}',
    expectation_digest: str = _DIGEST,
) -> RayTargetPolicyRevision:
    return RayTargetPolicyRevision.objects.create(
        target=target,
        revision=revision,
        desired_state=RayTargetDesiredState.ACTIVE,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json=expectation_json,
        expectation_digest=expectation_digest,
    )


def _create_attestation(
    policy: RayTargetPolicyRevision,
    *,
    revision: int = 1,
    attestation_json: str = '{"schema":"attestation"}',
    expectation_digest: str = _DIGEST,
    membership_digest: str = _DIGEST,
    attestation_digest: str = _DIGEST,
) -> RayTargetAttestationRevision:
    observed_at = timezone.now()
    return RayTargetAttestationRevision.objects.create(
        policy=policy,
        revision=revision,
        attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        attestation_json=attestation_json,
        expectation_digest=expectation_digest,
        membership_digest=membership_digest,
        attestation_digest=attestation_digest,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=30),
        recorded_at=observed_at + timedelta(seconds=1),
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


def _clear_target_tables() -> None:
    table_names = set(connection.introspection.table_names())
    if RayTarget._meta.db_table not in table_names:
        return
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


def _rejects_database_error(
    operation: Callable[[], object],
    error_type: type[DatabaseError],
) -> None:
    with pytest.raises(error_type), transaction.atomic():
        operation()


def _assert_raw_text_storage_guards(
    error_type: type[DatabaseError],
    *,
    sqlite_dynamic_types: bool,
) -> None:
    now = timezone.now()
    stored_now = now.astimezone(UTC).replace(tzinfo=None) if connection.vendor == "sqlite" else now
    target_values: dict[str, object] = {
        "target_key": "raw-target",
        "runner_family": "ray_core",
        "cluster_session": "session_raw_target",
        "ray_major": 2,
        "ray_minor": 56,
        "ray_patch": 0,
        "python_implementation": "cpython",
        "python_major": 3,
        "python_minor": 12,
        "python_patch": 12,
        "created_at": stored_now,
    }
    target_mutations: list[tuple[str, object]] = [
        ("target_key", "raw-target\x00suffix"),
        ("target_key", b"raw-target"),
        ("runner_family", "ray_core\x00suffix"),
        ("runner_family", b"ray_core"),
        ("cluster_session", "session_raw_target\x00suffix"),
        ("cluster_session", b"session_raw_target"),
        ("python_implementation", "cpython\x00suffix"),
        ("python_implementation", b"cpython"),
        ("created_at", "2026-08-15 20:00:00\x00suffix"),
        ("created_at", b"2026-08-15 20:00:00"),
        ("created_at", 2.5),
    ]
    for field in (
        "ray_major",
        "ray_minor",
        "ray_patch",
        "python_major",
        "python_minor",
        "python_patch",
    ):
        if sqlite_dynamic_types:
            target_mutations.append((field, 2.5))
        target_mutations.extend(((field, "2.5"), (field, b"2")))
    if sqlite_dynamic_types:
        target_mutations.extend(
            ("created_at", value)
            for value in (
                "now",
                "0000-01-01 00:00:00",
                "2026-01-01 24:00:00",
                "2026-02-30 20:00:00",
                "2026-08-15T20:00:00",
                "2026-08-15 20:00:00.12345",
            )
        )
    for field, value in target_mutations:
        invalid = {**target_values, field: value}
        _rejects_database_error(
            lambda invalid=invalid: _raw_insert(RayTarget, invalid),
            error_type,
        )
        assert RayTarget.objects.count() == 0

    target = _create_target(target_key="raw-parent")
    policy_values: dict[str, object] = {
        "target_id": target.pk,
        "revision": 1,
        "desired_state": "active",
        "expectation_schema_version": RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        "expectation_json": "{}",
        "expectation_digest": _DIGEST,
        "created_at": stored_now,
    }
    policy_mutations: list[tuple[str, object]] = [
        ("target_id", f"{target.pk}\x00suffix"),
        ("target_id", str(target.pk).encode()),
        ("revision", "1.5"),
        ("revision", b"1"),
        ("expectation_schema_version", "1.5"),
        ("expectation_schema_version", b"1"),
        ("desired_state", "active\x00suffix"),
        ("desired_state", b"active"),
        ("expectation_json", "{}\x00suffix"),
        ("expectation_digest", f"{_DIGEST}\x00suffix"),
        ("expectation_digest", _DIGEST.encode()),
        ("created_at", "2026-08-15 20:00:00\x00suffix"),
        ("created_at", b"2026-08-15 20:00:00"),
        ("created_at", 2.5),
    ]
    if sqlite_dynamic_types:
        policy_mutations.extend(
            (
                ("revision", 1.5),
                ("expectation_schema_version", 1.5),
                ("expectation_json", b"{}"),
            )
        )
        policy_mutations.extend(
            ("created_at", value)
            for value in (
                "now",
                "0000-01-01 00:00:00",
                "2026-01-01 24:00:00",
                "2026-02-30 20:00:00",
                "2026-08-15T20:00:00",
                "2026-08-15 20:00:00.12345",
            )
        )
    for field, value in policy_mutations:
        invalid = {**policy_values, field: value}
        _rejects_database_error(
            lambda invalid=invalid: _raw_insert(RayTargetPolicyRevision, invalid),
            error_type,
        )
        assert RayTargetPolicyRevision.objects.count() == 0

    policy = _create_policy(target)
    observed_at = now
    attestation_values: dict[str, object] = {
        "policy_id": policy.pk,
        "revision": 1,
        "attestation_schema_version": RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        "attestation_json": "{}",
        "expectation_digest": _DIGEST,
        "membership_digest": _DIGEST,
        "attestation_digest": _DIGEST,
        "observed_at": observed_at,
        "expires_at": observed_at + timedelta(seconds=30),
        "recorded_at": observed_at + timedelta(seconds=1),
    }
    attestation_mutations: list[tuple[str, object]] = [
        ("policy_id", "1.5"),
        ("policy_id", b"1"),
        ("revision", "1.5"),
        ("revision", b"1"),
        ("attestation_schema_version", "1.5"),
        ("attestation_schema_version", b"1"),
        ("attestation_json", "{}\x00suffix"),
        ("expectation_digest", f"{_DIGEST}\x00suffix"),
        ("expectation_digest", _DIGEST.encode()),
        ("membership_digest", f"{_DIGEST}\x00suffix"),
        ("membership_digest", _DIGEST.encode()),
        ("attestation_digest", f"{_DIGEST}\x00suffix"),
        ("attestation_digest", _DIGEST.encode()),
        ("observed_at", "2026-08-15 20:00:00\x00suffix"),
        ("observed_at", b"2026-08-15 20:00:00"),
        ("observed_at", 2.5),
        ("expires_at", "2026-08-15 20:00:30\x00suffix"),
        ("expires_at", b"2026-08-15 20:00:30"),
        ("expires_at", 2.5),
        ("recorded_at", "2026-08-15 20:00:01\x00suffix"),
        ("recorded_at", b"2026-08-15 20:00:01"),
        ("recorded_at", 2.5),
    ]
    if sqlite_dynamic_types:
        attestation_mutations.extend(
            (
                ("policy_id", 1.5),
                ("revision", 1.5),
                ("attestation_schema_version", 1.5),
                ("attestation_json", b"{}"),
            )
        )
        for field in ("observed_at", "expires_at", "recorded_at"):
            attestation_mutations.extend(
                (field, value)
                for value in (
                    "now",
                    "0000-01-01 00:00:00",
                    "2026-01-01 24:00:00",
                    "2026-02-30 20:00:00",
                    "2026-08-15T20:00:00",
                    "2026-08-15 20:00:00.12345",
                )
            )
    for field, value in attestation_mutations:
        invalid = {**attestation_values, field: value}
        _rejects_database_error(
            lambda invalid=invalid: _raw_insert(RayTargetAttestationRevision, invalid),
            error_type,
        )
        assert RayTargetAttestationRevision.objects.count() == 0

    policy.delete()
    target.delete()


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


def test_models_expose_only_the_frozen_dormant_persistence_contract() -> None:
    assert RayTarget._meta.pk.name == "target_key"
    assert dict(RayTarget._meta.get_field("runner_family").choices) == {
        "ray_core": "ray_core",
        "ray_job": "ray_job",
    }
    assert RayTargetDesiredState.values == ["active", "draining", "retired"]
    assert all(field.editable is False for field in RayTarget._meta.fields)
    assert all(
        field.auto_created or field.editable is False
        for field in RayTargetPolicyRevision._meta.fields
    )
    assert all(
        field.auto_created or field.editable is False
        for field in RayTargetAttestationRevision._meta.fields
    )

    target_fk = RayTargetPolicyRevision._meta.get_field("target")
    policy_fk = RayTargetAttestationRevision._meta.get_field("policy")
    assert target_fk.remote_field.on_delete is models.PROTECT
    assert target_fk.remote_field.related_name == "policy_revisions"
    assert policy_fk.remote_field.on_delete is models.PROTECT
    assert policy_fk.remote_field.related_name == "attestation_revisions"

    assert {constraint.name for constraint in RayTarget._meta.constraints} == {
        "ray_target_runner_valid",
        "ray_target_runtime_valid",
        "ray_target_instance_uniq",
    }
    assert {constraint.name for constraint in RayTargetPolicyRevision._meta.constraints} == {
        "ray_tpolicy_schema_valid",
        "ray_tpolicy_state_valid",
        "ray_tpolicy_revision_valid",
        "ray_tpolicy_target_rev_uniq",
    }
    assert {constraint.name for constraint in RayTargetAttestationRevision._meta.constraints} == {
        "ray_tattest_schema_valid",
        "ray_tattest_revision_valid",
        "ray_tattest_window_valid",
        "ray_tattest_policy_rev_uniq",
    }

    invalid = RayTarget(
        target_key="NOT-CANONICAL",
        runner_family=RayRunnerFamily.RAY_CORE,
        cluster_session="not-a-session",
        ray_major=2,
        ray_minor=56,
        ray_patch=0,
        python_implementation="CPython",
        python_major=3,
        python_minor=12,
        python_patch=12,
    )
    with pytest.raises(ValidationError):
        invalid.full_clean(validate_unique=False, validate_constraints=False)


def test_migration_fence_sql_is_bounded_reversible_and_fail_closed() -> None:
    migration = importlib.import_module("django_ray.migrations.0022_ray_target_persistence")
    postgresql = _RecordingSchemaEditor("postgresql")

    migration._install_target_persistence_fences(django_apps, postgresql)
    installed_sql = "\n".join(postgresql.statements)
    for trigger_name in _TARGET_TRIGGER_NAMES:
        assert f"CREATE TRIGGER {trigger_name}" in installed_sql
    assert "octet_length(NEW.expectation_json) NOT BETWEEN 1 AND 16384" in installed_sql
    assert "octet_length(NEW.attestation_json) NOT BETWEEN 1 AND 1048576" in installed_sql
    assert "substring(NEW.expectation_digest FROM 8) ~ '[^0-9a-f]'" in installed_sql
    assert "TG_OP = 'UPDATE' AND NEW IS DISTINCT FROM OLD" in installed_sql

    postgresql.statements.clear()
    migration._remove_target_persistence_fences(django_apps, postgresql)
    removed_sql = "\n".join(postgresql.statements)
    for trigger_name in _TARGET_TRIGGER_NAMES:
        assert f'DROP TRIGGER IF EXISTS "{trigger_name}" ON' in removed_sql
    assert removed_sql.count("DROP FUNCTION IF EXISTS") == 3

    sqlite = _RecordingSchemaEditor("sqlite")
    migration._install_target_persistence_fences(django_apps, sqlite)
    sqlite_sql = "\n".join(sqlite.statements)
    for trigger_name in _SQLITE_TRIGGER_NAMES:
        assert f"CREATE TRIGGER {trigger_name}" in sqlite_sql
    assert (
        "length(CAST(NEW.expectation_json AS BLOB))\n             NOT BETWEEN 1 AND 16384"
        in sqlite_sql
    )
    assert (
        "length(CAST(NEW.attestation_json AS BLOB))\n             NOT BETWEEN 1 AND 1048576"
        in sqlite_sql
    )
    assert "*[^0-9a-f]*" in sqlite_sql
    assert "typeof(NEW.target_key) != 'text'" in sqlite_sql
    assert "instr(NEW.target_key, char(0)) != 0" in sqlite_sql
    assert "typeof(NEW.expectation_json) != 'text'" in sqlite_sql
    assert "instr(NEW.attestation_json, char(0)) != 0" in sqlite_sql
    assert "typeof(NEW.ray_major) != 'integer'" in sqlite_sql
    assert "typeof(NEW.revision) != 'integer'" in sqlite_sql
    assert "strftime('%Y-%m-%d %H:%M:%S'" in sqlite_sql
    assert "NEW.target_key IS NOT OLD.target_key" in sqlite_sql

    sqlite.statements.clear()
    migration._remove_target_persistence_fences(django_apps, sqlite)
    removed_sql = "\n".join(sqlite.statements)
    for trigger_name in _SQLITE_TRIGGER_NAMES:
        assert f'DROP TRIGGER IF EXISTS "{trigger_name}"' in removed_sql

    unsupported = _RecordingSchemaEditor("mysql")
    message = "Ray target persistence supports only SQLite and PostgreSQL"
    with pytest.raises(RuntimeError, match=message):
        migration._install_target_persistence_fences(django_apps, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._remove_target_persistence_fences(django_apps, unsupported)
    assert unsupported.statements == []

    operations = migration.Migration.operations
    assert operations[-1].reverse_code is migration._guard_empty_target_persistence
    assert operations[-2].code is migration._install_target_persistence_fences

    postgresql.statements.clear()
    with pytest.raises(RuntimeError, match="rollback requires"):
        migration._guard_empty_target_persistence(_RollbackGuardApps(), postgresql)
    assert postgresql.statements[0].startswith(
        'LOCK TABLE "django_ray_raytarget", '
        '"django_ray_raytargetpolicyrevision", '
        '"django_ray_raytargetattestationrevision" '
    )


def _assert_database_constraints_and_fences() -> None:
    trigger_names = _database_trigger_names()
    expected_triggers = (
        _SQLITE_TRIGGER_NAMES if connection.vendor == "sqlite" else _TARGET_TRIGGER_NAMES
    )
    assert expected_triggers <= trigger_names

    with connection.cursor() as cursor:
        target_constraints = connection.introspection.get_constraints(
            cursor, RayTarget._meta.db_table
        )
        policy_constraints = connection.introspection.get_constraints(
            cursor, RayTargetPolicyRevision._meta.db_table
        )
        attestation_constraints = connection.introspection.get_constraints(
            cursor, RayTargetAttestationRevision._meta.db_table
        )
    assert target_constraints["ray_target_runner_valid"]["check"] is True
    assert target_constraints["ray_target_runtime_valid"]["check"] is True
    assert target_constraints["ray_target_instance_uniq"]["unique"] is True
    assert policy_constraints["ray_tpolicy_target_rev_uniq"]["unique"] is True
    assert policy_constraints["ray_tpolicy_schema_valid"]["check"] is True
    assert policy_constraints["ray_tpolicy_state_valid"]["check"] is True
    assert policy_constraints["ray_tpolicy_revision_valid"]["check"] is True
    assert attestation_constraints["ray_tattest_policy_rev_uniq"]["unique"] is True
    assert attestation_constraints["ray_tattest_schema_valid"]["check"] is True
    assert attestation_constraints["ray_tattest_revision_valid"]["check"] is True
    assert attestation_constraints["ray_tattest_window_valid"]["check"] is True

    _rejects_integrity_error(
        lambda: RayTarget.objects.create(
            target_key="UPPER",
            runner_family=RayRunnerFamily.RAY_CORE,
            cluster_session="session_upper",
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
    )
    _rejects_integrity_error(
        lambda: RayTarget.objects.create(
            target_key="newline\n",
            runner_family=RayRunnerFamily.RAY_CORE,
            cluster_session="session_newline",
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
    )
    _rejects_integrity_error(
        lambda: RayTarget.objects.create(
            target_key="zero-major",
            runner_family=RayRunnerFamily.RAY_CORE,
            cluster_session="session_zero_major",
            ray_major=0,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
    )

    target = _create_target()
    _rejects_integrity_error(
        lambda: RayTarget.objects.create(
            target_key="duplicate-instance",
            runner_family=target.runner_family,
            cluster_session=target.cluster_session,
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
    )
    with pytest.raises((DataError, IntegrityError, OverflowError)), transaction.atomic():
        RayTarget.objects.create(
            target_key="overflow",
            runner_family=RayRunnerFamily.RAY_CORE,
            cluster_session="session_overflow",
            ray_major=RAY_TARGET_ATTESTATION_MAX_COUNTER + 1,
            ray_minor=0,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )

    _rejects_integrity_error(lambda: _create_policy(target, expectation_json=""))
    _rejects_integrity_error(lambda: _create_policy(target, expectation_json="é" * 8193))
    _rejects_integrity_error(
        lambda: _create_policy(target, expectation_digest=f"sha256:{'A' * 64}")
    )
    _rejects_integrity_error(
        lambda: RayTargetPolicyRevision.objects.create(
            target=target,
            revision=0,
            desired_state=RayTargetDesiredState.ACTIVE,
            expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
            expectation_json="{}",
            expectation_digest=_DIGEST,
        )
    )
    policy = _create_policy(target)
    _rejects_integrity_error(lambda: _create_policy(target))

    _rejects_integrity_error(lambda: _create_attestation(policy, attestation_json=""))
    _rejects_integrity_error(lambda: _create_attestation(policy, attestation_json="é" * 524289))
    _rejects_integrity_error(
        lambda: _create_attestation(policy, membership_digest=f"sha256:{'g' * 64}")
    )
    observed_at = timezone.now()
    _rejects_integrity_error(
        lambda: RayTargetAttestationRevision.objects.create(
            policy=policy,
            revision=1,
            attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
            attestation_json="{}",
            expectation_digest=_DIGEST,
            membership_digest=_DIGEST,
            attestation_digest=_DIGEST,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(seconds=3601),
            recorded_at=observed_at + timedelta(seconds=1),
        )
    )
    attestation = _create_attestation(policy)
    _rejects_integrity_error(lambda: _create_attestation(policy))

    assert RayTarget.objects.filter(pk=target.pk).update(target_key=models.F("target_key")) == 1
    assert (
        RayTargetPolicyRevision.objects.filter(pk=policy.pk).update(
            desired_state=models.F("desired_state")
        )
        == 1
    )
    assert (
        RayTargetAttestationRevision.objects.filter(pk=attestation.pk).update(
            expires_at=models.F("expires_at")
        )
        == 1
    )
    _rejects_integrity_error(lambda: RayTarget.objects.filter(pk=target.pk).update(ray_patch=1))
    _rejects_integrity_error(
        lambda: RayTargetPolicyRevision.objects.filter(pk=policy.pk).update(
            desired_state=RayTargetDesiredState.DRAINING
        )
    )
    _rejects_integrity_error(
        lambda: RayTargetAttestationRevision.objects.filter(pk=attestation.pk).update(
            attestation_digest=f"sha256:{'b' * 64}"
        )
    )

    with pytest.raises(ProtectedError):
        policy.delete()
    with pytest.raises(ProtectedError):
        target.delete()

    attestation.delete()
    policy.delete()
    target.delete()


@pytest.mark.django_db(transaction=True)
def test_sqlite_target_persistence_constraints_and_fences() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_database_constraints_and_fences()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_target_persistence_constraints_and_fences() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_database_constraints_and_fences()


@pytest.mark.django_db(transaction=True)
def test_sqlite_raw_text_storage_rejects_nul_and_blob_values() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_raw_text_storage_guards(IntegrityError, sqlite_dynamic_types=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_raw_text_storage_rejects_nul_and_applicable_binary_values() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    # PostgreSQL assignment-casts bytea JSON into text before a trigger can
    # observe its source type. Canonical decoding owns that content boundary;
    # unlike SQLite, PostgreSQL cannot retain a BLOB in a TEXT column.
    _assert_raw_text_storage_guards(DatabaseError, sqlite_dynamic_types=False)


def _assert_migration_round_trip_and_reverse_guard() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        existing = old_execution.objects.create(
            task_id="target-persistence-existing-task",
            callable_path="testproject.tasks.add_numbers",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        target_model = new_apps.get_model("django_ray", "RayTarget")
        policy_model = new_apps.get_model("django_ray", "RayTargetPolicyRevision")
        attestation_model = new_apps.get_model("django_ray", "RayTargetAttestationRevision")
        assert target_model.objects.count() == 0
        assert policy_model.objects.count() == 0
        assert attestation_model.objects.count() == 0
        assert old_execution.objects.filter(pk=existing.pk).exists()

        target_model.objects.create(
            target_key="rollback-fence",
            runner_family="ray_core",
            cluster_session="session_rollback_fence",
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
        with pytest.raises(
            RuntimeError,
            match="rollback requires all target tables to be empty",
        ):
            MigrationExecutor(connection).migrate(MIGRATE_FROM)
        assert target_model.objects.filter(pk="rollback-fence").exists()
        assert {
            target_model._meta.db_table,
            policy_model._meta.db_table,
            attestation_model._meta.db_table,
        } <= set(connection.introspection.table_names())
        expected_triggers = (
            _SQLITE_TRIGGER_NAMES if connection.vendor == "sqlite" else _TARGET_TRIGGER_NAMES
        )
        assert expected_triggers <= _database_trigger_names()

        target_model.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        with pytest.raises(LookupError):
            reverted_apps.get_model("django_ray", "RayTarget")
        reverted_execution = reverted_apps.get_model("django_ray", "RayTaskExecution")
        assert reverted_execution.objects.filter(pk=existing.pk).exists()
    finally:
        _clear_target_tables()
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.django_db(transaction=True)
def test_sqlite_target_persistence_is_additive_unseeded_and_reverse_guarded() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_migration_round_trip_and_reverse_guard()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_target_persistence_is_additive_unseeded_and_reverse_guarded() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_migration_round_trip_and_reverse_guard()


def _create_held_target(writer_inserted: Event, release_writer: Event) -> None:
    close_old_connections()
    try:
        with transaction.atomic():
            _create_target(target_key="concurrent-rollback")
            writer_inserted.set()
            if not release_writer.wait(timeout=20):
                raise TimeoutError("test did not release the target writer")
    finally:
        _close_owned_thread_connection()


def _attempt_reverse_with_zero_busy_timeout(reverse_started: Event) -> str:
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
        raise AssertionError("rollback unexpectedly removed target persistence")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
def test_sqlite_active_target_writer_cannot_partially_reverse_schema() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")

    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(
                _create_held_target,
                writer_inserted,
                release_writer,
            )
            assert writer_inserted.wait(timeout=10)
            reverse = executor.submit(
                _attempt_reverse_with_zero_busy_timeout,
                reverse_started,
            )
            assert reverse_started.wait(timeout=10)
            try:
                outcome = reverse.result(timeout=10)
            finally:
                release_writer.set()
            writer.result(timeout=20)

        assert outcome in {"writer-locked", "history-refused"}
        assert RayTarget.objects.filter(pk="concurrent-rollback").exists()
        assert {
            RayTarget._meta.db_table,
            RayTargetPolicyRevision._meta.db_table,
            RayTargetAttestationRevision._meta.db_table,
        } <= set(connection.introspection.table_names())
        assert _SQLITE_TRIGGER_NAMES <= _database_trigger_names()
    finally:
        release_writer.set()
        _clear_target_tables()
        MigrationExecutor(connection).migrate(LATEST)


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
    raise TimeoutError("rollback did not wait on the target writer lock")


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
        raise AssertionError("rollback unexpectedly removed target persistence")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_target_writer_serializes_before_reverse_guard() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    backend_pid: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(
                _create_held_target,
                writer_inserted,
                release_writer,
            )
            assert writer_inserted.wait(timeout=10)
            reverse = executor.submit(
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

        assert RayTarget.objects.filter(pk="concurrent-rollback").exists()
        assert {
            RayTarget._meta.db_table,
            RayTargetPolicyRevision._meta.db_table,
            RayTargetAttestationRevision._meta.db_table,
        } <= set(connection.introspection.table_names())
        assert _TARGET_TRIGGER_NAMES <= _database_trigger_names()
    finally:
        release_writer.set()
        _clear_target_tables()
        MigrationExecutor(connection).migrate(LATEST)
