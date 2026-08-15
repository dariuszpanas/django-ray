"""Migration and database-fence coverage for dormant Ray target routes."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from threading import Event
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import (
    DatabaseError,
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
    RAY_TASK_TARGET_ROUTE_SELECTION_SCHEMA_VERSION,
    RayTarget,
    RayTargetAttestationRevision,
    RayTargetDesiredState,
    RayTargetPolicyRevision,
    RayTargetRoute,
    RayTargetRouteRevision,
    RayTaskExecution,
    RayTaskTargetBinding,
    RayTaskTargetRouteSelection,
)
from django_ray.target_attestation import (
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayRunnerFamily,
)

MIGRATE_FROM = [("django_ray", "0023_ray_task_target_binding")]
MIGRATE_TO = [("django_ray", "0024_ray_target_routes")]
LATEST = [("django_ray", "0026_ray_task_target_execution_evidence")]

_DIGEST = f"sha256:{'a' * 64}"
_POSTGRESQL_TRIGGERS = {
    "ray_troute_guard_0024",
    "ray_troute_rev_guard_0024",
    "ray_trsel_guard_0024",
}
_SQLITE_TRIGGERS = {
    "ray_troute_insert_0024",
    "ray_troute_update_0024",
    "ray_troute_rev_insert_0024",
    "ray_troute_rev_update_0024",
    "ray_trsel_insert_0024",
    "ray_trsel_update_0024",
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
        "RayTargetRoute": "django_ray_raytargetroute",
        "RayTargetRouteRevision": "django_ray_raytargetrouterevision",
        "RayTaskTargetRouteSelection": "django_ray_raytasktargetrouteselection",
        "RayTaskTargetBinding": "django_ray_raytasktargetbinding",
    }

    @classmethod
    def get_model(cls, app_label: str, model_name: str) -> object:
        assert app_label == "django_ray"
        return SimpleNamespace(
            _meta=SimpleNamespace(db_table=cls._TABLES[model_name]),
            objects=_ExistingRows(),
        )


def _create_target_and_policy(
    *,
    target_key: str = "route-primary",
    policy_revision: int = 1,
) -> tuple[RayTarget, RayTargetPolicyRevision]:
    target = RayTarget.objects.create(
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
    policy = RayTargetPolicyRevision.objects.create(
        target=target,
        revision=policy_revision,
        desired_state=RayTargetDesiredState.DRAINING,
        expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        expectation_json='{"schema":"expectation"}',
        expectation_digest=_DIGEST,
    )
    return target, policy


def _create_binding(
    policy: RayTargetPolicyRevision,
    *,
    task_id: str = "task-target-route",
) -> RayTaskTargetBinding:
    execution = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
    )
    return RayTaskTargetBinding.objects.create(
        execution=execution,
        target_policy=policy,
    )


def _create_route_revision(
    policy: RayTargetPolicyRevision,
    *,
    backend_alias: str = "default",
    revision: int = 1,
) -> tuple[RayTargetRoute, RayTargetRouteRevision]:
    route = RayTargetRoute.objects.create(backend_alias=backend_alias)
    route_revision = RayTargetRouteRevision.objects.create(
        route=route,
        revision=revision,
        target_policy=policy,
    )
    return route, route_revision


def _database_trigger_names() -> set[str]:
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        elif connection.vendor == "postgresql":
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        else:
            raise AssertionError(f"unsupported database vendor: {connection.vendor}")
        return {str(row[0]) for row in cursor.fetchall()}


def _clear_route_tables() -> None:
    table_names = set(connection.introspection.table_names())
    if RayTaskTargetRouteSelection._meta.db_table in table_names:
        RayTaskTargetRouteSelection.objects.all().delete()
    if RayTargetRouteRevision._meta.db_table in table_names:
        RayTargetRouteRevision.objects.all().delete()
    if RayTargetRoute._meta.db_table in table_names:
        RayTargetRoute.objects.all().delete()
    RayTaskTargetBinding.objects.all().delete()
    RayTaskExecution.objects.all().delete()
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


def _raw_delete(model: type[models.Model], *, column: str, value: object) -> None:
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {quote(model._meta.db_table)} WHERE {quote(column)} = %s",
            [value],
        )


def _rejects_database_error(
    operation: Callable[[], object],
    error_type: type[DatabaseError] = DatabaseError,
) -> None:
    with pytest.raises(error_type), transaction.atomic():
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


def test_models_expose_only_the_dormant_route_provenance_contract() -> None:
    assert RayTargetRoute._meta.pk.name == "backend_alias"
    assert RayTargetRoute._meta.get_field("backend_alias").max_length == 128
    assert all(field.editable is False for field in RayTargetRoute._meta.fields)
    assert all(
        field.auto_created or field.editable is False
        for field in RayTargetRouteRevision._meta.fields
    )
    assert all(
        field.auto_created or field.editable is False
        for field in RayTaskTargetRouteSelection._meta.fields
    )

    route = RayTargetRoute(backend_alias="worker_gpu")
    revision = RayTargetRouteRevision(route_id="worker_gpu", revision=7, target_policy_id=11)
    selection = RayTaskTargetRouteSelection(binding_id=13, route_revision_id=17)
    assert str(route) == "worker_gpu"
    assert str(revision) == "worker_gpu route revision 7"
    assert str(selection) == "binding 13 route revision 17"

    invalid = RayTargetRoute(backend_alias="NOT-CANONICAL")
    with pytest.raises(ValidationError):
        invalid.full_clean(validate_unique=False, validate_constraints=False)

    route_fk = RayTargetRouteRevision._meta.get_field("route")
    policy_fk = RayTargetRouteRevision._meta.get_field("target_policy")
    binding_fk = RayTaskTargetRouteSelection._meta.get_field("binding")
    revision_fk = RayTaskTargetRouteSelection._meta.get_field("route_revision")
    assert route_fk.remote_field.on_delete is models.PROTECT
    assert route_fk.remote_field.related_name == "revisions"
    assert policy_fk.remote_field.on_delete is models.PROTECT
    assert policy_fk.remote_field.related_name == "route_revisions"
    assert binding_fk.primary_key is True
    assert binding_fk.remote_field.on_delete is models.PROTECT
    assert binding_fk.remote_field.related_name == "route_selection"
    assert revision_fk.remote_field.on_delete is models.PROTECT
    assert revision_fk.remote_field.related_name == "task_selections"
    assert RayTaskTargetRouteSelection._meta.get_field("schema_version").default == 1
    assert RayTaskTargetRouteSelection._meta.get_field("schema_version").db_default == 1
    assert {constraint.name for constraint in RayTargetRouteRevision._meta.constraints} == {
        "ray_troute_id_valid",
        "ray_troute_revision_valid",
        "ray_troute_route_rev_uniq",
    }
    assert {constraint.name for constraint in RayTaskTargetRouteSelection._meta.constraints} == {
        "ray_trsel_schema_valid"
    }
    assert RAY_TASK_TARGET_ROUTE_SELECTION_SCHEMA_VERSION == 1


def test_migration_fence_sql_is_typed_cross_bound_reversible_and_fail_closed() -> None:
    migration = importlib.import_module("django_ray.migrations.0024_ray_target_routes")
    postgresql = _RecordingSchemaEditor("postgresql")

    migration._install_target_routing_fences(django_apps, postgresql)
    installed_sql = "\n".join(postgresql.statements)
    for trigger in _POSTGRESQL_TRIGGERS:
        assert f"CREATE TRIGGER {trigger}" in installed_sql
    assert "NEW.revision NOT BETWEEN 1 AND 9223372036854775807" in installed_sql
    assert "NOT isfinite(NEW.created_at)" in installed_sql
    assert "binding.target_policy_id = route_revision.target_policy_id" in installed_sql
    assert "TG_OP = 'UPDATE' AND NEW IS DISTINCT FROM OLD" in installed_sql

    postgresql.statements.clear()
    migration._remove_target_routing_fences(django_apps, postgresql)
    removed_sql = "\n".join(postgresql.statements)
    for trigger in _POSTGRESQL_TRIGGERS:
        assert f'DROP TRIGGER IF EXISTS "{trigger}" ON' in removed_sql
    assert removed_sql.count("DROP FUNCTION IF EXISTS") == 3

    sqlite = _RecordingSchemaEditor("sqlite")
    migration._install_target_routing_fences(django_apps, sqlite)
    sqlite_sql = "\n".join(sqlite.statements)
    for trigger in _SQLITE_TRIGGERS:
        assert f"CREATE TRIGGER {trigger}" in sqlite_sql
    assert "typeof(NEW.backend_alias) != 'text'" in sqlite_sql
    assert "instr(NEW.backend_alias, char(0)) != 0" in sqlite_sql
    assert "NEW.backend_alias GLOB '*[^a-z0-9_.-]*'" in sqlite_sql
    assert "typeof(NEW.revision) != 'integer'" in sqlite_sql
    assert "strftime('%Y-%m-%d %H:%M:%S'" in sqlite_sql
    assert "binding.target_policy_id = route_revision.target_policy_id" in sqlite_sql
    assert "NEW.route_revision_id IS NOT OLD.route_revision_id" in sqlite_sql

    sqlite.statements.clear()
    migration._remove_target_routing_fences(django_apps, sqlite)
    removed_sql = "\n".join(sqlite.statements)
    for trigger in _SQLITE_TRIGGERS:
        assert f'DROP TRIGGER IF EXISTS "{trigger}"' in removed_sql

    unsupported = _RecordingSchemaEditor("mysql")
    message = "Ray target routing supports only SQLite and PostgreSQL"
    with pytest.raises(RuntimeError, match=message):
        migration._install_target_routing_fences(django_apps, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._remove_target_routing_fences(django_apps, unsupported)
    with pytest.raises(RuntimeError, match=message):
        migration._guard_empty_target_routing(_RollbackGuardApps(), unsupported)
    assert unsupported.statements == []

    operations = migration.Migration.operations
    assert operations[-1].reverse_code is migration._guard_empty_target_routing
    assert operations[-2].code is migration._install_target_routing_fences

    postgresql.statements.clear()
    with pytest.raises(RuntimeError, match="rollback requires all routing tables to be empty"):
        migration._guard_empty_target_routing(_RollbackGuardApps(), postgresql)
    assert postgresql.statements == [
        'LOCK TABLE "django_ray_raytargetroute", '
        '"django_ray_raytargetrouterevision", '
        '"django_ray_raytasktargetrouteselection" IN ACCESS EXCLUSIVE MODE'
    ]


def _assert_database_constraints_and_fences() -> None:
    expected_triggers = _SQLITE_TRIGGERS if connection.vendor == "sqlite" else _POSTGRESQL_TRIGGERS
    assert expected_triggers <= _database_trigger_names()

    with connection.cursor() as cursor:
        revision_constraints = connection.introspection.get_constraints(
            cursor,
            RayTargetRouteRevision._meta.db_table,
        )
        selection_constraints = connection.introspection.get_constraints(
            cursor,
            RayTaskTargetRouteSelection._meta.db_table,
        )
    assert revision_constraints["ray_troute_revision_valid"]["check"] is True
    assert revision_constraints["ray_troute_id_valid"]["check"] is True
    assert revision_constraints["ray_troute_route_rev_uniq"]["unique"] is True
    assert revision_constraints["ray_troute_latest_idx"]["index"] is True
    assert selection_constraints["ray_trsel_schema_valid"]["check"] is True
    assert sum(bool(item["primary_key"]) for item in selection_constraints.values()) == 1

    target, policy = _create_target_and_policy()
    other_target, other_policy = _create_target_and_policy(target_key="route-secondary")
    route, route_revision = _create_route_revision(policy)
    assert isinstance(route_revision.pk, int)
    assert route_revision.pk >= 1
    binding = _create_binding(policy)

    max_alias = "a" * 128
    max_route = RayTargetRoute.objects.create(backend_alias=max_alias)
    assert len(max_route.backend_alias.encode("ascii")) == 128
    _rejects_database_error(
        lambda: RayTargetRoute.objects.create(backend_alias="a" * 129),
    )
    _rejects_database_error(
        lambda: RayTargetRoute.objects.create(backend_alias="UPPER"),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: RayTargetRoute.objects.create(backend_alias="route\x00suffix"),
    )

    _rejects_database_error(
        lambda: RayTargetRouteRevision.objects.create(
            route=route,
            revision=0,
            target_policy=policy,
        ),
        IntegrityError,
    )
    with pytest.raises((DatabaseError, OverflowError)), transaction.atomic():
        RayTargetRouteRevision.objects.create(
            route=route,
            revision=RAY_TARGET_ATTESTATION_MAX_COUNTER + 1,
            target_policy=policy,
        )
    _rejects_database_error(
        lambda: RayTargetRouteRevision.objects.create(
            route=route,
            revision=1,
            target_policy=policy,
        ),
        IntegrityError,
    )

    selection = RayTaskTargetRouteSelection.objects.create(
        binding=binding,
        route_revision=route_revision,
    )
    assert selection.pk == binding.pk
    assert selection.schema_version == 1

    _rejects_database_error(
        lambda: RayTaskTargetRouteSelection.objects.create(
            binding=binding,
            route_revision=route_revision,
        ),
        IntegrityError,
    )
    mismatched_binding = _create_binding(other_policy, task_id="task-target-route-mismatch")
    _rejects_database_error(
        lambda: RayTaskTargetRouteSelection.objects.create(
            binding=mismatched_binding,
            route_revision=route_revision,
        ),
        IntegrityError,
    )
    assert not RayTaskTargetRouteSelection.objects.filter(pk=mismatched_binding.pk).exists()

    assert (
        RayTargetRoute.objects.filter(pk=route.pk).update(
            backend_alias=models.F("backend_alias"),
            created_at=models.F("created_at"),
        )
        == 1
    )
    assert (
        RayTargetRouteRevision.objects.filter(pk=route_revision.pk).update(
            route_id=models.F("route_id"),
            revision=models.F("revision"),
            target_policy_id=models.F("target_policy_id"),
            created_at=models.F("created_at"),
        )
        == 1
    )
    assert (
        RayTaskTargetRouteSelection.objects.filter(pk=selection.pk).update(
            binding_id=models.F("binding_id"),
            route_revision_id=models.F("route_revision_id"),
            schema_version=models.F("schema_version"),
            created_at=models.F("created_at"),
        )
        == 1
    )

    _rejects_database_error(
        lambda: RayTargetRoute.objects.filter(pk=route.pk).update(
            created_at=route.created_at.replace(microsecond=0)
        ),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: RayTargetRouteRevision.objects.filter(pk=route_revision.pk).update(revision=2),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: RayTaskTargetRouteSelection.objects.filter(pk=selection.pk).update(
            schema_version=2
        ),
        IntegrityError,
    )

    with pytest.raises(ProtectedError):
        binding.delete()
    with pytest.raises(ProtectedError):
        route_revision.delete()
    with pytest.raises(ProtectedError):
        route.delete()
    with pytest.raises(ProtectedError):
        policy.delete()

    _rejects_database_error(
        lambda: _raw_delete(
            RayTaskTargetBinding,
            column="execution_id",
            value=binding.pk,
        ),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: _raw_delete(
            RayTargetRouteRevision,
            column="id",
            value=route_revision.pk,
        ),
        IntegrityError,
    )
    _rejects_database_error(
        lambda: _raw_delete(
            RayTargetRoute,
            column="backend_alias",
            value=route.pk,
        ),
        IntegrityError,
    )

    selection.delete()
    binding.delete()
    route_revision.delete()
    route.delete()
    max_route.delete()
    mismatched_binding.delete()
    policy.delete()
    target.delete()
    other_policy.delete()
    other_target.delete()


@pytest.mark.django_db(transaction=True)
def test_sqlite_route_constraints_cross_policy_fences_and_parent_protection() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_database_constraints_and_fences()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_route_constraints_cross_policy_fences_and_parent_protection() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_database_constraints_and_fences()


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


def _assert_raw_storage_guards(*, sqlite_dynamic_types: bool) -> None:
    now = timezone.now()
    stored_now = now.astimezone(UTC).replace(tzinfo=None) if connection.vendor == "sqlite" else now
    target, policy = _create_target_and_policy(target_key="route-raw")

    route_values: dict[str, object] = {
        "backend_alias": "raw_route",
        "created_at": stored_now,
    }
    route_mutations: list[tuple[str, object]] = [
        ("backend_alias", ""),
        ("backend_alias", "a" * 129),
        ("backend_alias", "UPPER"),
        ("backend_alias", "bad/alias"),
        ("backend_alias", "route\x00suffix"),
        ("backend_alias", b"raw_route"),
        ("backend_alias", "routé"),
        ("created_at", None),
        ("created_at", "infinity"),
        ("created_at", "2026-08-15 20:00:00\x00suffix"),
        ("created_at", b"2026-08-15 20:00:00"),
    ]
    if sqlite_dynamic_types:
        route_mutations.extend(("created_at", value) for value in _invalid_sqlite_datetimes())
    for field, value in route_mutations:
        invalid = {**route_values, field: value}
        _rejects_database_error(lambda invalid=invalid: _raw_insert(RayTargetRoute, invalid))
        assert RayTargetRoute.objects.count() == 0

    max_values = {**route_values, "backend_alias": "a" * 128}
    _raw_insert(RayTargetRoute, max_values)
    assert RayTargetRoute.objects.get().backend_alias == "a" * 128
    RayTargetRoute.objects.all().delete()
    route = RayTargetRoute.objects.create(backend_alias="raw_route")

    revision_values: dict[str, object] = {
        "id": 1001,
        "route_id": route.pk,
        "revision": 1,
        "target_policy_id": policy.pk,
        "created_at": stored_now,
    }
    revision_mutations: list[tuple[str, object]] = [
        ("id", 0),
        ("id", -1),
        ("id", -2),
        ("id", "1.5"),
        ("id", b"1"),
        ("route_id", f"{route.pk}\x00suffix"),
        ("route_id", str(route.pk).encode()),
        ("revision", 0),
        ("revision", "1.5"),
        ("revision", b"1"),
        ("target_policy_id", 0),
        ("target_policy_id", "1.5"),
        ("target_policy_id", b"1"),
        ("created_at", None),
        ("created_at", "infinity"),
        ("created_at", "2026-08-15 20:00:00\x00suffix"),
        ("created_at", b"2026-08-15 20:00:00"),
    ]
    if sqlite_dynamic_types:
        revision_mutations.extend(
            (
                ("id", 1.5),
                ("route_id", 2.5),
                ("revision", 1.5),
                ("target_policy_id", 1.5),
            )
        )
        revision_mutations.extend(("created_at", value) for value in _invalid_sqlite_datetimes())
    for field, value in revision_mutations:
        invalid = {**revision_values, field: value}
        _rejects_database_error(
            lambda invalid=invalid: _raw_insert(RayTargetRouteRevision, invalid)
        )
        assert RayTargetRouteRevision.objects.count() == 0

    _raw_insert(RayTargetRouteRevision, revision_values)
    route_revision = RayTargetRouteRevision.objects.get(pk=1001)

    binding = _create_binding(policy, task_id="task-target-route-raw")
    selection_values: dict[str, object] = {
        "binding_id": binding.pk,
        "route_revision_id": route_revision.pk,
        "schema_version": 1,
        "created_at": stored_now,
    }
    selection_mutations: list[tuple[str, object]] = [
        ("binding_id", 0),
        ("binding_id", "1.5"),
        ("binding_id", b"1"),
        ("route_revision_id", 0),
        ("route_revision_id", "1.5"),
        ("route_revision_id", b"1"),
        ("schema_version", 0),
        ("schema_version", 2),
        ("schema_version", "1.5"),
        ("schema_version", b"1"),
        ("created_at", None),
        ("created_at", "infinity"),
        ("created_at", "2026-08-15 20:00:00\x00suffix"),
        ("created_at", b"2026-08-15 20:00:00"),
    ]
    if sqlite_dynamic_types:
        selection_mutations.extend(
            (
                ("binding_id", 1.5),
                ("route_revision_id", 1.5),
                ("schema_version", 1.5),
            )
        )
        selection_mutations.extend(("created_at", value) for value in _invalid_sqlite_datetimes())
    for field, value in selection_mutations:
        invalid = {**selection_values, field: value}
        _rejects_database_error(
            lambda invalid=invalid: _raw_insert(RayTaskTargetRouteSelection, invalid)
        )
        assert RayTaskTargetRouteSelection.objects.count() == 0

    _raw_insert(RayTaskTargetRouteSelection, selection_values)
    assert RayTaskTargetRouteSelection.objects.get().route_revision_id == route_revision.pk
    RayTaskTargetRouteSelection.objects.all().delete()

    other_target, other_policy = _create_target_and_policy(target_key="route-raw-other")
    mismatched_binding = _create_binding(
        other_policy,
        task_id="task-target-route-raw-mismatch",
    )
    mismatch = {**selection_values, "binding_id": mismatched_binding.pk}
    _rejects_database_error(
        lambda: _raw_insert(RayTaskTargetRouteSelection, mismatch),
        IntegrityError,
    )

    route_revision.delete()
    route.delete()
    binding.delete()
    mismatched_binding.delete()
    policy.delete()
    target.delete()
    other_policy.delete()
    other_target.delete()


@pytest.mark.django_db(transaction=True)
def test_sqlite_raw_routes_reject_blob_nul_real_non_utf8_and_malformed_dates() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_raw_storage_guards(sqlite_dynamic_types=True)

    quote = connection.ops.quote_name
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {quote(RayTargetRoute._meta.db_table)} "
            f"({quote('backend_alias')}, {quote('created_at')}) "
            "VALUES (CAST(X'80' AS TEXT), %s)",
            [timezone.now().astimezone(UTC).replace(tzinfo=None)],
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_raw_routes_reject_applicable_invalid_storage_and_policy_mismatch() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_raw_storage_guards(sqlite_dynamic_types=False)


def _assert_migration_round_trip_and_reverse_guard() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(MIGRATE_FROM)
    try:
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        old_execution = old_apps.get_model("django_ray", "RayTaskExecution")
        old_target = old_apps.get_model("django_ray", "RayTarget")
        old_policy = old_apps.get_model("django_ray", "RayTargetPolicyRevision")
        old_binding = old_apps.get_model("django_ray", "RayTaskTargetBinding")

        execution = old_execution.objects.create(
            task_id="route-before-migration",
            callable_path="testproject.tasks.add_numbers",
        )
        target = old_target.objects.create(
            target_key="route-round-trip",
            runner_family="ray_core",
            cluster_session="session_route_round_trip",
            ray_major=2,
            ray_minor=56,
            ray_patch=0,
            python_implementation="cpython",
            python_major=3,
            python_minor=12,
            python_patch=12,
        )
        policy = old_policy.objects.create(
            target=target,
            revision=1,
            desired_state="draining",
            expectation_schema_version=1,
            expectation_json='{"schema":"expectation"}',
            expectation_digest=_DIGEST,
        )
        binding = old_binding.objects.create(
            execution_id=execution.pk,
            target_policy_id=policy.pk,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        route_model = new_apps.get_model("django_ray", "RayTargetRoute")
        revision_model = new_apps.get_model("django_ray", "RayTargetRouteRevision")
        selection_model = new_apps.get_model("django_ray", "RayTaskTargetRouteSelection")
        assert route_model.objects.count() == 0
        assert revision_model.objects.count() == 0
        assert selection_model.objects.count() == 0
        assert old_binding.objects.filter(pk=binding.pk).exists()

        rolling_execution = old_execution.objects.create(
            task_id="route-old-writer",
            callable_path="testproject.tasks.add_numbers",
        )
        rolling_binding = old_binding.objects.create(
            execution_id=rolling_execution.pk,
            target_policy_id=policy.pk,
        )
        assert route_model.objects.count() == 0
        assert old_binding.objects.filter(pk=rolling_binding.pk).exists()

        route = route_model.objects.create(backend_alias="default")
        revision = revision_model.objects.create(
            route_id=route.pk,
            revision=1,
            target_policy_id=policy.pk,
        )
        selection_model.objects.create(
            binding_id=binding.pk,
            route_revision_id=revision.pk,
        )
        with pytest.raises(
            RuntimeError,
            match="rollback requires all routing tables to be empty",
        ):
            MigrationExecutor(connection).migrate(MIGRATE_FROM)
        assert selection_model.objects.filter(pk=binding.pk).exists()
        assert route_model._meta.db_table in connection.introspection.table_names()
        expected_triggers = (
            _SQLITE_TRIGGERS if connection.vendor == "sqlite" else _POSTGRESQL_TRIGGERS
        )
        assert expected_triggers <= _database_trigger_names()

        selection_model.objects.all().delete()
        revision_model.objects.all().delete()
        route_model.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        reverted_apps = executor.loader.project_state(MIGRATE_FROM).apps
        for model_name in (
            "RayTargetRoute",
            "RayTargetRouteRevision",
            "RayTaskTargetRouteSelection",
        ):
            with pytest.raises(LookupError):
                reverted_apps.get_model("django_ray", model_name)
        reverted_binding = reverted_apps.get_model("django_ray", "RayTaskTargetBinding")
        assert reverted_binding.objects.filter(pk__in=(binding.pk, rolling_binding.pk)).count() == 2
    finally:
        table_names = set(connection.introspection.table_names())
        if RayTaskTargetRouteSelection._meta.db_table in table_names:
            RayTaskTargetRouteSelection.objects.all().delete()
        if RayTargetRouteRevision._meta.db_table in table_names:
            RayTargetRouteRevision.objects.all().delete()
        if RayTargetRoute._meta.db_table in table_names:
            RayTargetRoute.objects.all().delete()
        MigrationExecutor(connection).migrate(LATEST)


@pytest.mark.django_db(transaction=True)
def test_sqlite_routes_are_additive_unseeded_old_writer_safe_and_reverse_guarded() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")
    _assert_migration_round_trip_and_reverse_guard()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_routes_are_additive_unseeded_old_writer_safe_and_reverse_guarded() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")
    _assert_migration_round_trip_and_reverse_guard()


def _create_held_route(writer_inserted: Event, release_writer: Event) -> None:
    close_old_connections()
    try:
        with transaction.atomic():
            RayTargetRoute.objects.create(backend_alias="route-concurrent-writer")
            writer_inserted.set()
            if not release_writer.wait(timeout=20):
                raise TimeoutError("test did not release the route writer")
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
        raise AssertionError("rollback unexpectedly removed Ray target routing tables")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
def test_sqlite_active_route_writer_cannot_partially_reverse_schema() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("requires the default SQLite test database")

    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(_create_held_route, writer_inserted, release_writer)
            assert writer_inserted.wait(timeout=10)
            reverse = executor.submit(
                _attempt_sqlite_reverse_with_zero_busy_timeout,
                reverse_started,
            )
            assert reverse_started.wait(timeout=10)
            try:
                outcome = reverse.result(timeout=10)
            finally:
                release_writer.set()
            writer.result(timeout=20)

        assert outcome in {"writer-locked", "history-refused"}
        assert RayTargetRoute.objects.filter(pk="route-concurrent-writer").exists()
        assert RayTargetRoute._meta.db_table in connection.introspection.table_names()
        assert _SQLITE_TRIGGERS <= _database_trigger_names()
    finally:
        release_writer.set()
        MigrationExecutor(connection).migrate(LATEST)
        _clear_route_tables()


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
    raise TimeoutError("rollback did not wait on the route writer lock")


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
        raise AssertionError("rollback unexpectedly removed Ray target routing tables")
    finally:
        _close_owned_thread_connection()


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
def test_postgresql_route_writer_serializes_before_reverse_guard() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")

    writer_inserted = Event()
    release_writer = Event()
    reverse_started = Event()
    backend_pid: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(_create_held_route, writer_inserted, release_writer)
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

        assert RayTargetRoute.objects.filter(pk="route-concurrent-writer").exists()
        assert RayTargetRoute._meta.db_table in connection.introspection.table_names()
        assert _POSTGRESQL_TRIGGERS <= _database_trigger_names()
    finally:
        release_writer.set()
        MigrationExecutor(connection).migrate(LATEST)
        _clear_route_tables()
