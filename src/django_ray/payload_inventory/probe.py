"""Disposable filesystem/registry observer, with no application initialization."""

from __future__ import annotations

import time
from collections.abc import Callable

from django_ray.doctor.probe import (
    ProbeUnavailableError,
    bounded_config,
    database_error_code,
    readonly_database,
)
from django_ray.payload_inventory.contracts import OUTCOMES, Limits
from django_ray.payload_inventory.filesystem import Scan, registry_lookup


def run(*, settings_module: str, using: str, deadline: float, limits: Limits, emit: Callable):
    from django.conf import Settings

    code = "settings_unavailable"
    connection = None
    scan = None
    emitted_summary = False

    def publish(value):
        nonlocal emitted_summary
        emitted_summary = value["kind"] == "summary"
        emit(value)

    try:
        if time.monotonic() >= deadline:
            raise ProbeUnavailableError("probe_timeout")
        selected = Settings(settings_module)
        code = "configuration_invalid"
        config = bounded_config(selected)
        if config.get("INPUT_STORAGE_BACKEND") != "filesystem":
            raise ProbeUnavailableError("filesystem_required")
        path = config.get("INPUT_STORAGE_FILESYSTEM_PATH")
        if type(path) is not str or not 1 <= len(path) <= 4096:
            raise ProbeUnavailableError("configuration_invalid")
        from django_ray.result_storage import validate_storage_configuration

        validate_storage_configuration(config)
        code = "database_unavailable"
        database = readonly_database(selected, using, deadline)
        import django
        from django.conf import settings
        from django.db import DatabaseError, connections, transaction

        settings.configure(
            INSTALLED_APPS=["django_ray.doctor.probe.DoctorAppConfig"],
            DATABASES={"default": database},
            DATABASE_ROUTERS=[],
            DJANGO_RAY={},
            SECRET_KEY="inventory-model-registry-only",
            USE_TZ=True,
            LOGGING_CONFIG=None,
        )
        django.setup(set_prefix=False)
        connection = connections["default"]
        scan = Scan(limits=limits, deadline=deadline, emit=publish)
        statements = 0

        def execute(execute, sql, params, many, context):
            nonlocal statements
            scan.check()
            statements += 1
            if many or statements > limits.max_objects + 8:
                raise ProbeUnavailableError("query_budget_exhausted")
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(execute):
                connection.ensure_connection()
                if connection.vendor == "sqlite":
                    connection.connection.set_progress_handler(
                        lambda: time.monotonic() >= deadline, 1000
                    )
                    with connection.cursor() as cursor:
                        cursor.execute("PRAGMA query_only=ON")
                with transaction.atomic():
                    if connection.vendor == "postgresql":
                        with connection.cursor() as cursor:
                            cursor.execute("SET TRANSACTION READ ONLY")
                    scan.run(path, registry_lookup)
        except DatabaseError as error:
            raise ProbeUnavailableError(database_error_code(error, deadline)) from None
    except Exception as error:
        if emitted_summary:
            return 2
        code = error.code if isinstance(error, ProbeUnavailableError) else code
        if scan is not None:
            scan.finish(code)
        else:
            emit(
                {
                    "kind": "summary",
                    "code": code,
                    "entries": 0,
                    "objects": 0,
                    "counts": dict.fromkeys(OUTCOMES, 0),
                }
            )
    finally:
        if connection is not None:
            if connection.vendor == "sqlite" and connection.connection is not None:
                connection.connection.set_progress_handler(None, 0)
            connection.close()
    return 0
