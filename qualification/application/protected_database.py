"""Read the fixed private fixture without bootstrapping the application registry."""

from __future__ import annotations

import os

from qualification.application.protected_runtime import (
    MAX_BODY,
    protected_values_from_rows,
    refused_task_ids_from_rows,
    validate_row_ids,
)

# These are the same projections used by protected_runtime's ORM observer.
# Only bounded envelope/task values cross the database connection; arbitrary
# row content, settings, credentials and diagnostics never become receipts.
ENVELOPES_SQL = """
SELECT id,
       CASE WHEN OCTET_LENGTH(runtime_env_json) <= %s THEN runtime_env_json ELSE NULL END,
       (runtime_env_profile = %s AND callable_path = %s),
       (state = %s), (state = %s)
FROM django_ray_raytaskexecution
WHERE id IN (%s, %s, %s)
"""
TASKS_SQL = """
SELECT id, SUBSTR(task_id, 1, 37), (task_id = SUBSTR(task_id, 1, 37))
FROM django_ray_raytaskexecution
WHERE id IN (%s, %s) AND state = %s
"""


def _connection_options() -> dict:
    # This observer only belongs to core.yaml's disposable PostgreSQL service.
    # Do not silently use libpq defaults, a DSN, or a different configured DB.
    expected = {
        "DATABASE_ENGINE": "django.db.backends.postgresql",
        "DATABASE_HOST": "postgres",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "django_ray",
        "DATABASE_USER": "django_ray",
    }
    if any(os.environ.get(name) != value for name, value in expected.items()):
        raise ValueError("Protected database observation requires its owned database")
    password = os.environ.get("DATABASE_PASSWORD")
    if not password:
        raise ValueError("Protected database observation requires its owned credentials")
    return {
        "host": expected["DATABASE_HOST"],
        "port": int(expected["DATABASE_PORT"]),
        "dbname": expected["DATABASE_NAME"],
        "user": expected["DATABASE_USER"],
        "password": password,
        "connect_timeout": 2,
        "autocommit": True,
    }


def read_owned_rows(row_ids: list[int], *, status_only: bool) -> tuple[list[str], list[str]]:
    """Close the read-only connection before returning values for HTTP/log checks.

    The caller's existing child alarm also covers imports and connection setup.
    The status and log phases each reread their exact current owned rows.
    """
    validate_row_ids(row_ids)
    if type(status_only) is not bool:
        raise ValueError("Protected database observation requires an explicit phase")
    options = _connection_options()
    import importlib

    psycopg = importlib.import_module("psycopg")

    with psycopg.connect(**options) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '1000ms'")
                cursor.execute("SET LOCAL lock_timeout = '1000ms'")
                cursor.execute(
                    ENVELOPES_SQL,
                    (
                        MAX_BODY,
                        "thin",
                        "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
                        "SUCCEEDED",
                        "FAILED",
                        *row_ids,
                    ),
                )
                values = protected_values_from_rows(row_ids, cursor.fetchmany(4))
                tasks = []
                if status_only:
                    cursor.execute(TASKS_SQL, (*row_ids[1:], "FAILED"))
                    tasks = refused_task_ids_from_rows(row_ids, cursor.fetchmany(3))
    return values, tasks
