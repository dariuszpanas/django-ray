"""Package-owned probes: scalar evidence through isolated read-only connections.

Only this disposable child imports the selected trusted settings module. It does
not initialize application apps, routers, task decorators, storage, or Ray.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.apps import AppConfig

from django_ray.doctor.contracts import (
    CONNECT_SECONDS,
    DATABASE_SECTIONS,
    LOCK_MILLISECONDS,
    RETURNED_ROWS,
    SQL_STATEMENTS,
    STATEMENT_MILLISECONDS,
    database_alias_valid,
    encode,
    observation,
    section_frame,
    settings_module_valid,
)


class ProbeUnavailableError(Exception):
    """A fixed, public code rather than application exception text."""

    def __init__(self, code: str):
        self.code = code


class DoctorAppConfig(AppConfig):
    """Load only package models, with no application ready hooks."""

    name = "django_ray"
    default_auto_field = "django.db.models.BigAutoField"


def bounded_config(settings: object) -> dict:
    from django_ray.conf.defaults import DEFAULTS

    raw = getattr(settings, "DJANGO_RAY", {})
    if type(raw) is not dict or len(raw) > 256:
        raise ProbeUnavailableError("configuration_invalid")
    return {**DEFAULTS, **raw}


def configuration(settings: object) -> dict:
    """Check finite configured shapes; never instantiate a storage backend."""
    from django_ray import __version__
    from django_ray.runtime.runtime_env_encryption import (
        RuntimeEnvEncryptionError,
        validate_runtime_env_encryption_settings,
    )

    config = bounded_config(settings)
    values = {}
    for key, name, choices in (
        ("RUNNER", "runner", {"ray_core", "ray_job"}),
        ("RUNTIME_ENV_STORAGE_MODE", "storage_mode", {"plaintext", "encrypted"}),
        ("INPUT_STORAGE_BACKEND", "input_backend", {None, "filesystem", "s3", "gcs"}),
        ("RESULT_STORAGE_BACKEND", "result_backend", {"digest", "filesystem", "s3", "gcs"}),
    ):
        value = config.get(key)
        if type(value) not in {str, type(None)} or value not in choices:
            raise ProbeUnavailableError("configuration_invalid")
        values[name] = "none" if value is None else value
    for prefix in ("INPUT", "RESULT"):
        backend = config.get(f"{prefix}_STORAGE_BACKEND")
        suffix = {"filesystem": "FILESYSTEM_PATH", "s3": "S3_BUCKET", "gcs": "GCS_BUCKET"}.get(
            backend
        )
        if suffix:
            location = config.get(f"{prefix}_STORAGE_{suffix}")
            if type(location) is not str or not 1 <= len(location) <= 4096:
                raise ProbeUnavailableError("configuration_invalid")
    keys = config["RUNTIME_ENV_ENCRYPTION_KEYS"]
    if type(keys) is not dict or len(keys) > 32:
        raise ProbeUnavailableError("configuration_invalid")
    for key, material in keys.items():
        if (
            type(key) is not str
            or len(key) > 64
            or type(material) is not str
            or len(material) != 43
        ):
            raise ProbeUnavailableError("configuration_invalid")
    active = config["RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY"]
    if active is not None and (type(active) is not str or len(active) > 64):
        raise ProbeUnavailableError("configuration_invalid")
    fallback = config["RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK"]
    if type(fallback) is not bool:
        raise ProbeUnavailableError("configuration_invalid")
    secret = getattr(settings, "SECRET_KEY", "") if fallback else "unused"
    fallbacks = getattr(settings, "SECRET_KEY_FALLBACKS", ()) if fallback else ()
    if type(fallbacks) not in {tuple, list} or len(fallbacks) > 8:
        raise ProbeUnavailableError("configuration_invalid")
    for item in (secret, *fallbacks):
        if type(item) not in {str, bytes} or not 1 <= len(item) <= 4096:
            raise ProbeUnavailableError("configuration_invalid")
    try:
        validate_runtime_env_encryption_settings(
            config, django_secret_key=secret, django_secret_key_fallbacks=fallbacks
        )
    except RuntimeEnvEncryptionError:
        raise ProbeUnavailableError("configuration_invalid") from None
    return observation(
        "observed",
        "observed",
        package_version=__version__,
        **values,
        key_count=len(keys),
        active_key_configured=active is not None,
        secret_fallback_configured=fallback,
        key_material_shape_valid=True,
    )


def readonly_database(settings: object, using: str, deadline: float) -> dict:
    """Copy supported plain settings; never alter an existing wrapper/config."""
    databases = getattr(settings, "DATABASES", None)
    if type(databases) is not dict or using not in databases or type(databases[using]) is not dict:
        raise ProbeUnavailableError("database_unsupported")
    source = databases[using]
    if getattr(settings, "USE_TZ", True) is not True or source.get("TIME_ZONE") not in (
        None,
        "UTC",
    ):
        raise ProbeUnavailableError("database_unsupported")
    engine = source.get("ENGINE")
    if type(engine) is not str or engine not in {
        "django.db.backends.sqlite3",
        "django.db.backends.postgresql",
    }:
        raise ProbeUnavailableError("database_unsupported")
    options = source.get("OPTIONS", {})
    if type(options) is not dict or len(options) > 32:
        raise ProbeUnavailableError("database_unsupported")
    clone = {"ENGINE": engine, "CONN_MAX_AGE": 0, "CONN_HEALTH_CHECKS": False}
    if engine.endswith("sqlite3"):
        name = source.get("NAME")
        if type(name) not in {str, type(Path())}:
            raise ProbeUnavailableError("database_unsupported")
        name = str(name)
        if not name or len(name) > 4096 or name == ":memory:" or name.startswith("file:"):
            raise ProbeUnavailableError("memory_database_unsupported")
        if set(options) - {"timeout"}:
            raise ProbeUnavailableError("database_unsupported")
        try:
            path = Path(name).resolve(strict=True)
            if not path.is_file():
                raise OSError
        except (OSError, ValueError):
            raise ProbeUnavailableError("database_unavailable") from None
        clone.update(NAME=path.as_uri() + "?mode=ro", OPTIONS={"uri": True, "timeout": 0.25})
        return clone
    for field in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
        value = source.get(field, "")
        if type(value) not in {str, int} or len(str(value)) > 4096:
            raise ProbeUnavailableError("database_unsupported")
        clone[field] = value
    allowed = {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "application_name",
        "options",
        "connect_timeout",
    }
    if set(options) - allowed:
        raise ProbeUnavailableError("database_unsupported")
    for value in options.values():
        if type(value) not in {str, int} or len(str(value)) > 4096:
            raise ProbeUnavailableError("database_unsupported")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeUnavailableError("probe_timeout")
    existing = options.get("options", "")
    if type(existing) is not str:
        raise ProbeUnavailableError("database_unsupported")
    clone["OPTIONS"] = {
        **options,
        "connect_timeout": max(1, min(CONNECT_SECONDS, math.ceil(remaining))),
        "options": existing
        + " -c default_transaction_read_only=on"
        + f" -c statement_timeout={STATEMENT_MILLISECONDS} -c lock_timeout={LOCK_MILLISECONDS}",
    }
    return clone


class QueryBudget:
    def __init__(self, deadline: float):
        self.deadline = deadline
        self.statements = 0
        self.rows = 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ProbeUnavailableError("probe_timeout")

    def execute(self, execute, sql, params, many, context):
        self.check()
        self.statements += 1
        if self.statements > SQL_STATEMENTS or many:
            raise ProbeUnavailableError("query_budget_exhausted")
        return execute(sql, params, many, context)

    def returned(self, value, count: int = 1):
        self.rows += count
        if self.rows > RETURNED_ROWS:
            raise ProbeUnavailableError("row_budget_exhausted")
        self.check()
        return value


def database_error_code(error: Exception, deadline: float) -> str:
    """Classify only driver codes; never inspect or forward exception text."""
    cause = error.__cause__ or error
    if (
        time.monotonic() >= deadline
        or getattr(cause, "sqlstate", None) in {"57014", "55P03"}
        or getattr(cause, "sqlite_errorcode", None) in {5, 6, 9}
    ):
        return "probe_timeout"
    return "database_unavailable"


def collect_database(*, using: str, budget: QueryBudget, config: dict, emit: Callable) -> None:
    """All database results are fixed scalar counts or bounded policy integers."""
    from django.db import DatabaseError, connections, transaction
    from django.db.migrations.recorder import MigrationRecorder
    from django.db.models import BooleanField, Count, Exists, OuterRef, Q, Value
    from django.db.models.expressions import RawSQL

    from django_ray.execution_protocol import (
        EXECUTION_METADATA_SCHEMA_VERSION,
        MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
        MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    )
    from django_ray.models import RayTaskExecution, TaskInputPayload, TaskState, TaskWorkerLease
    from django_ray.protocol_status import _valid_protocol_policy, _valid_worker_lease_shape

    connection = connections[using]
    try:
        with connection.execute_wrapper(budget.execute):
            connection.ensure_connection()
            if connection.vendor == "sqlite":
                connection.connection.set_progress_handler(
                    lambda: time.monotonic() >= budget.deadline, 1000
                )
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA query_only=ON")
            with transaction.atomic(using=using):
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                emit(
                    "database",
                    observation("observed", "observed", vendor=connection.vendor, read_only=True),
                )
                expected = []
                directory = Path(__file__).resolve().parent.parent / "migrations"
                for path in directory.iterdir():
                    if path.suffix == ".py" and path.name[:1].isdigit():
                        expected.append(path.stem)
                        if len(expected) > 64:
                            raise ProbeUnavailableError("row_budget_exhausted")
                migrations = MigrationRecorder.Migration.objects.using(using).filter(
                    app="django_ray"
                )
                try:
                    counts = budget.returned(
                        migrations.aggregate(
                            applied=Count("name", filter=Q(name__in=expected), distinct=True),
                            unknown=Count("pk", filter=~Q(name__in=expected)),
                        )
                    )
                except DatabaseError as error:
                    code = database_error_code(error, budget.deadline)
                    raise ProbeUnavailableError(
                        "migrations_unavailable" if code == "database_unavailable" else code
                    ) from None
                missing = len(expected) - counts["applied"]
                if missing < 0:
                    raise ProbeUnavailableError("database_unavailable")
                emit(
                    "migrations",
                    observation(
                        "blocker" if missing or counts["unknown"] else "observed",
                        "migrations_missing" if missing or counts["unknown"] else "observed",
                        expected=len(expected),
                        missing=missing,
                        **counts,
                    ),
                )
                if missing or counts["unknown"]:
                    for name in DATABASE_SECTIONS[2:]:
                        emit(name, observation("unavailable", "migrations_missing"))
                    return
                policies = list(
                    _valid_protocol_policy(using=using).values_list(
                        "active_write_protocol_version", "legacy_worker_admission_enabled"
                    )[:2]
                )
                budget.returned(policies, len(policies))
                valid_policy = len(policies) == 1
                emit(
                    "protocol",
                    observation(
                        "observed" if valid_policy else "blocker",
                        "observed" if valid_policy else "protocol_invalid",
                        write_version=policies[0][0] if valid_policy else None,
                        legacy_admission=policies[0][1] if valid_policy else None,
                        minimum_supported=MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
                        maximum_supported=MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
                    ),
                )
                lease_seconds = config.get("WORKER_LEASE_SECONDS")
                if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86400:
                    raise ProbeUnavailableError("configuration_invalid")
                from django_ray.lease_policy import LeasePolicyError, read_lease_policy

                try:
                    lease_policy = read_lease_policy(using=using, matching=False)
                except LeasePolicyError as error:
                    raise ProbeUnavailableError(error.reason) from None
                budget.returned(lease_policy, 1)
                if lease_policy.seconds != lease_seconds:
                    raise ProbeUnavailableError("lease_policy_mismatch")
                observed = datetime.now(UTC)
                cutoff = observed - timedelta(seconds=lease_seconds)
                fresh = Q(
                    is_active=True,
                    stopped_at__isnull=True,
                    last_heartbeat_at__gte=cutoff,
                    last_heartbeat_at__lte=observed,
                    started_at__lte=observed,
                )
                shape = _valid_worker_lease_shape()
                leases = TaskWorkerLease.objects.using(using)
                if connection.vendor == "sqlite":
                    # SQLite permits text in integer columns. Compare types in
                    # SQL before trusting range comparisons; never hydrate the
                    # possibly oversized values into the probe process.
                    leases = leases.alias(
                        _numeric_shape=RawSQL(
                            "typeof(capability_schema_version) = 'integer' "
                            "AND typeof(is_active) = 'integer' AND is_active IN (0, 1) "
                            "AND ((capability_schema_version = 0 "
                            "AND typeof(min_supported_execution_protocol_version) = 'null' "
                            "AND typeof(max_supported_execution_protocol_version) = 'null') "
                            "OR (capability_schema_version = 1 "
                            "AND typeof(min_supported_execution_protocol_version) = 'integer' "
                            "AND typeof(max_supported_execution_protocol_version) = 'integer'))",
                            (),
                            output_field=BooleanField(),
                        )
                    )
                    shape &= Q(_numeric_shape=True)
                lease_counts = budget.returned(
                    leases.aggregate(
                        total=Count("pk"),
                        active=Count("pk", filter=Q(is_active=True)),
                        live=Count("pk", filter=fresh & shape),
                        stale_active=Count("pk", filter=Q(is_active=True) & ~fresh),
                        malformed=Count("pk", filter=~shape),
                        legacy_active=Count(
                            "pk", filter=Q(is_active=True, capability_schema_version=0)
                        ),
                    )
                )
                emit(
                    "leases",
                    observation(
                        "blocker" if lease_counts["malformed"] else "observed",
                        "lease_shape_invalid" if lease_counts["malformed"] else "observed",
                        lease_policy_seconds=lease_policy.seconds,
                        lease_policy_revision=lease_policy.revision,
                        **lease_counts,
                    ),
                )
                capacity = (
                    leases.filter(fresh & shape)
                    .alias(_legacy_version=Value(0))
                    .filter(
                        Q(
                            capability_schema_version=1,
                            min_supported_execution_protocol_version__lte=OuterRef(
                                "execution_protocol_version"
                            ),
                            max_supported_execution_protocol_version__gte=OuterRef(
                                "execution_protocol_version"
                            ),
                        )
                        | Q(
                            capability_schema_version=0,
                            _legacy_version=OuterRef("execution_protocol_version"),
                        )
                    )
                    if valid_policy
                    else leases.none()
                )
                # An invalid capability advertisement blocks the shared rollout contract.
                if valid_policy and not policies[0][1]:
                    capacity = capacity.exclude(capability_schema_version=0)
                if lease_counts["malformed"]:
                    capacity = leases.none()
                nonterminal = Q(
                    state__in=(TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING)
                )
                unsupported = ~Q(
                    execution_protocol_version__gte=MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
                    execution_protocol_version__lte=MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
                    metadata_schema_version=EXECUTION_METADATA_SCHEMA_VERSION,
                )
                executions = RayTaskExecution.objects.using(using).alias(_capacity=Exists(capacity))
                work = budget.returned(
                    executions.aggregate(
                        queued=Count("pk", filter=Q(state=TaskState.QUEUED)),
                        running=Count("pk", filter=Q(state=TaskState.RUNNING)),
                        cancelling=Count("pk", filter=Q(state=TaskState.CANCELLING)),
                        terminal=Count(
                            "pk",
                            filter=Q(
                                state__in=(
                                    TaskState.SUCCEEDED,
                                    TaskState.FAILED,
                                    TaskState.CANCELLED,
                                    TaskState.LOST,
                                    TaskState.EXPIRED,
                                )
                            ),
                        ),
                        invalid_state=Count("pk", filter=~Q(state__in=TaskState.values)),
                        unsupported_protocol=Count("pk", filter=nonterminal & unsupported),
                        queued_without_protocol_lease=Count(
                            "pk", filter=Q(state=TaskState.QUEUED, _capacity=False)
                        ),
                        active_without_heartbeat=Count(
                            "pk",
                            filter=Q(state__in=(TaskState.RUNNING, TaskState.CANCELLING))
                            & (
                                Q(last_heartbeat_at__isnull=True)
                                | Q(last_heartbeat_at__lt=cutoff)
                                | Q(last_heartbeat_at__gt=observed)
                            ),
                        ),
                        queue_expired=Count(
                            "pk", filter=Q(state=TaskState.QUEUED, queue_deadline_at__lte=observed)
                        ),
                    )
                )
                blocked = any(
                    work[key]
                    for key in (
                        "invalid_state",
                        "unsupported_protocol",
                        "queued_without_protocol_lease",
                        "active_without_heartbeat",
                        "queue_expired",
                    )
                )
                emit(
                    "work",
                    observation(
                        "blocker" if blocked else "observed",
                        "work_blockers" if blocked else "observed",
                        **work,
                    ),
                )
                registry = budget.returned(
                    TaskInputPayload.objects.using(using).aggregate(
                        total=Count("pk"),
                        active=Count("pk", filter=Q(state="ACTIVE")),
                        purged=Count("pk", filter=Q(state="PURGED")),
                        invalid_state=Count("pk", filter=~Q(state__in=("ACTIVE", "PURGED"))),
                        cleanup_errors=Count("pk", filter=~Q(cleanup_error="")),
                    )
                )
                blocked = registry["invalid_state"] or registry["cleanup_errors"]
                emit(
                    "registered_cleanup",
                    observation(
                        "blocker" if blocked else "observed",
                        "registry_shape_invalid"
                        if registry["invalid_state"]
                        else "cleanup_errors"
                        if blocked
                        else "observed",
                        **registry,
                    ),
                )
    except DatabaseError as error:
        raise ProbeUnavailableError(database_error_code(error, budget.deadline)) from None
    finally:
        # This wrapper is owned only by this probe. No original callback exists.
        if connection.vendor == "sqlite" and connection.connection is not None:
            connection.connection.set_progress_handler(None, 0)
        connection.close()


def run(*, probe: str, settings_module: str, using: str, deadline: float, emit: Callable) -> int:
    from django.conf import Settings

    expected = ("configuration",) if probe == "configuration" else DATABASE_SECTIONS
    seen = set()
    failure_code = "settings_unavailable"

    def publish(name: str, value: dict) -> None:
        seen.add(name)
        emit(section_frame(name, value))

    try:
        QueryBudget(deadline).check()
        selected = Settings(settings_module)
        failure_code = (
            "configuration_invalid" if probe == "configuration" else "database_unavailable"
        )
        if probe == "configuration":
            publish("configuration", configuration(selected))
        else:
            import django
            from django.conf import settings

            config = bounded_config(selected)
            database = readonly_database(selected, using, deadline)
            settings.configure(
                INSTALLED_APPS=["django_ray.doctor.probe.DoctorAppConfig"],
                DATABASES={"default": database},
                DATABASE_ROUTERS=[],
                DJANGO_RAY={},
                SECRET_KEY="doctor-model-registry-only",
                USE_TZ=True,
                LOGGING_CONFIG=None,
            )
            django.setup(set_prefix=False)
            collect_database(
                using="default", budget=QueryBudget(deadline), config=config, emit=publish
            )
        return 0
    except Exception as error:
        code = error.code if isinstance(error, ProbeUnavailableError) else failure_code
        if code == "configuration_invalid":
            outcome = "blocker"
        elif code in {"probe_timeout", "query_budget_exhausted", "row_budget_exhausted"}:
            outcome = "budget_exhausted"
        elif code in {"database_unsupported", "memory_database_unsupported"}:
            outcome = "unsupported"
        else:
            outcome = "unavailable"
        all_emitted = len(seen) == len(expected)
        for name in expected:
            if name not in seen:
                publish(name, observation(outcome, code))
        # All frames can have been emitted before connection-close failed. The
        # supervisor must expose that late failure as well.
        return 2 if all_emitted else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", choices=("configuration", "database"), required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--deadline", type=float, required=True)
    arguments = parser.parse_args()
    if (
        not settings_module_valid(arguments.settings)
        or not database_alias_valid(arguments.database)
        or not 0 < arguments.deadline - time.monotonic() <= 20
    ):
        return 2

    def emit(value: dict) -> None:
        sys.stdout.buffer.write(encode(value))
        sys.stdout.buffer.flush()

    return run(
        probe=arguments.probe,
        settings_module=arguments.settings,
        using=arguments.database,
        deadline=arguments.deadline,
        emit=emit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
