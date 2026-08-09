from __future__ import annotations

from django.db import migrations

_NONTERMINAL_STATES = ("QUEUED", "RUNNING", "CANCELLING")
_POLICY_STATE_ERROR = (
    "django-ray migration 0020 requires exactly one consistent execution-protocol "
    "policy and legacy-admission token state"
)


def _lock_migration_boundary(schema_editor) -> None:
    quote = schema_editor.quote_name
    execution_table = quote("django_ray_raytaskexecution")
    policy_table = quote("django_ray_taskexecutionprotocolpolicy")
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(f"LOCK TABLE {execution_table} IN SHARE ROW EXCLUSIVE MODE")
        schema_editor.execute(f"LOCK TABLE {policy_table} IN SHARE MODE")
        schema_editor.execute(
            f"LOCK TABLE {quote('django_ray_legacyworkeradmissiontoken')} IN SHARE MODE"
        )
    elif vendor == "sqlite":
        # Take SQLite's database-wide writer fence before inspecting either the
        # policy or retained executions. A zero-row UPDATE still starts a write.
        schema_editor.execute(
            f"UPDATE {policy_table} SET revision = revision WHERE singleton_key = 1"
        )
    else:
        raise RuntimeError(
            "django-ray legacy-open rollback fencing supports only SQLite and PostgreSQL"
        )


def _validate_installation_state(apps, schema_editor) -> None:
    protocol_policy = apps.get_model("django_ray", "TaskExecutionProtocolPolicy")
    legacy_token = apps.get_model("django_ray", "LegacyWorkerAdmissionToken")
    execution = apps.get_model("django_ray", "RayTaskExecution")
    using = schema_editor.connection.alias

    policy_rows = list(
        protocol_policy.objects.using(using).values(
            "singleton_key",
            "schema_version",
            "active_write_protocol_version",
            "legacy_worker_admission_enabled",
            "revision",
        )
    )
    token_keys = list(legacy_token.objects.using(using).values_list("singleton_key", flat=True))
    if len(policy_rows) != 1:
        raise RuntimeError(_POLICY_STATE_ERROR)

    policy = policy_rows[0]
    legacy_open = policy["legacy_worker_admission_enabled"]
    active_protocol = policy["active_write_protocol_version"]
    valid_policy = (
        policy["singleton_key"] == 1
        and policy["schema_version"] == 1
        and isinstance(active_protocol, int)
        and active_protocol >= 1
        and isinstance(policy["revision"], int)
        and policy["revision"] >= 1
        and isinstance(legacy_open, bool)
        and (not legacy_open or active_protocol == 1)
    )
    valid_token_state = token_keys == ([1] if legacy_open else [])
    if not valid_policy or not valid_token_state:
        raise RuntimeError(_POLICY_STATE_ERROR)

    if not legacy_open:
        return

    incompatible_count = (
        execution.objects.using(using)
        .filter(state__in=_NONTERMINAL_STATES)
        .exclude(execution_protocol_version=1)
        .count()
    )
    if incompatible_count:
        raise RuntimeError(
            "django-ray migration 0020 cannot install the legacy-open rollback fence: "
            f"legacy admission is open with {incompatible_count} non-v1 nonterminal "
            "execution(s); return those rows to a terminal state or close legacy "
            "admission through the rollout coordinator before retrying"
        )


def _install_postgresql_fence(schema_editor) -> None:
    quote = schema_editor.quote_name
    execution_table = quote("django_ray_raytaskexecution")
    policy_table = quote("django_ray_taskexecutionprotocolpolicy")
    token_table = quote("django_ray_legacyworkeradmissiontoken")
    nonterminal_states = ", ".join(f"'{state}'" for state in _NONTERMINAL_STATES)

    statements = (
        f"""
        CREATE FUNCTION django_ray_guard_legacy_open_execution_0020()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $django_ray$
        DECLARE
            policy_schema smallint;
            active_protocol smallint;
            legacy_open boolean;
            policy_revision bigint;
            token_present boolean;
        BEGIN
            SELECT schema_version,
                   active_write_protocol_version,
                   legacy_worker_admission_enabled,
                   revision
            INTO policy_schema,
                 active_protocol,
                 legacy_open,
                 policy_revision
            FROM {policy_table}
            WHERE singleton_key = 1
            FOR SHARE;

            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'django-ray execution-protocol policy is unavailable or corrupt'
                    USING ERRCODE = '23514';
            END IF;

            SELECT EXISTS (
                SELECT 1 FROM {token_table} WHERE singleton_key = 1
            ) INTO token_present;

            IF policy_schema IS DISTINCT FROM 1
               OR active_protocol IS NULL
               OR active_protocol < 1
               OR policy_revision IS NULL
               OR policy_revision < 1
               OR (legacy_open IS TRUE AND active_protocol IS DISTINCT FROM 1)
               OR legacy_open IS DISTINCT FROM token_present THEN
                RAISE EXCEPTION
                    'django-ray execution-protocol policy is unavailable or corrupt'
                    USING ERRCODE = '23514';
            END IF;

            IF legacy_open THEN
                RAISE EXCEPTION
                    'django-ray legacy admission rejects non-v1 nonterminal work'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $django_ray$
        """,
        f"""
        CREATE TRIGGER ray_exec_legacy_open_insert_0020
        BEFORE INSERT ON {execution_table}
        FOR EACH ROW
        WHEN (
            NEW.execution_protocol_version IS DISTINCT FROM 1
            AND NEW.state IN ({nonterminal_states})
        )
        EXECUTE FUNCTION django_ray_guard_legacy_open_execution_0020()
        """,
        f"""
        CREATE TRIGGER ray_exec_legacy_open_retry_0020
        BEFORE UPDATE OF state ON {execution_table}
        FOR EACH ROW
        WHEN (
            NEW.execution_protocol_version IS DISTINCT FROM 1
            AND OLD.state NOT IN ({nonterminal_states})
            AND NEW.state IN ({nonterminal_states})
        )
        EXECUTE FUNCTION django_ray_guard_legacy_open_execution_0020()
        """,
    )
    for statement in statements:
        schema_editor.execute(statement)


def _sqlite_policy_guard(*, policy_table: str, token_table: str) -> str:
    return f"""
        SELECT RAISE(
            ABORT,
            'django-ray execution-protocol policy is unavailable or corrupt'
        )
        WHERE NOT EXISTS (
            SELECT 1
            FROM {policy_table} AS policy
            WHERE policy.singleton_key = 1
              AND policy.schema_version = 1
              AND policy.active_write_protocol_version >= 1
              AND policy.revision >= 1
              AND (
                  (
                      policy.legacy_worker_admission_enabled = 1
                      AND policy.active_write_protocol_version = 1
                      AND EXISTS (
                          SELECT 1
                          FROM {token_table} AS token
                          WHERE token.singleton_key = 1
                      )
                  )
                  OR (
                      policy.legacy_worker_admission_enabled = 0
                      AND NOT EXISTS (
                          SELECT 1
                          FROM {token_table} AS token
                          WHERE token.singleton_key = 1
                      )
                  )
              )
        );

        SELECT RAISE(
            ABORT,
            'django-ray legacy admission rejects non-v1 nonterminal work'
        )
        WHERE EXISTS (
            SELECT 1
            FROM {policy_table}
            WHERE singleton_key = 1
              AND legacy_worker_admission_enabled = 1
        );
    """


def _install_sqlite_fence(schema_editor) -> None:
    quote = schema_editor.quote_name
    execution_table = quote("django_ray_raytaskexecution")
    policy_table = quote("django_ray_taskexecutionprotocolpolicy")
    token_table = quote("django_ray_legacyworkeradmissiontoken")
    nonterminal_states = ", ".join(f"'{state}'" for state in _NONTERMINAL_STATES)
    policy_guard = _sqlite_policy_guard(
        policy_table=policy_table,
        token_table=token_table,
    )

    statements = (
        f"""
        CREATE TRIGGER ray_exec_legacy_open_insert_0020
        BEFORE INSERT ON {execution_table}
        FOR EACH ROW
        WHEN NEW.execution_protocol_version IS NOT 1
         AND NEW.state IN ({nonterminal_states})
        BEGIN
            {policy_guard}
        END
        """,
        f"""
        CREATE TRIGGER ray_exec_legacy_open_retry_0020
        BEFORE UPDATE OF state ON {execution_table}
        FOR EACH ROW
        WHEN NEW.execution_protocol_version IS NOT 1
         AND OLD.state NOT IN ({nonterminal_states})
         AND NEW.state IN ({nonterminal_states})
        BEGIN
            {policy_guard}
        END
        """,
    )
    for statement in statements:
        schema_editor.execute(statement)


def _install_legacy_open_rollback_fence(apps, schema_editor) -> None:
    _lock_migration_boundary(schema_editor)
    _validate_installation_state(apps, schema_editor)
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        _install_postgresql_fence(schema_editor)
    elif vendor == "sqlite":
        _install_sqlite_fence(schema_editor)
    else:  # pragma: no cover - rejected by _lock_migration_boundary
        raise AssertionError(f"unexpected database vendor: {vendor}")


def _remove_legacy_open_rollback_fence(apps, schema_editor) -> None:
    del apps
    quote = schema_editor.quote_name
    execution_table = quote("django_ray_raytaskexecution")
    triggers = (
        "ray_exec_legacy_open_insert_0020",
        "ray_exec_legacy_open_retry_0020",
    )
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        for trigger in triggers:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {quote(trigger)} ON {execution_table}")
        schema_editor.execute(
            f"DROP FUNCTION IF EXISTS {quote('django_ray_guard_legacy_open_execution_0020')}()"
        )
    elif vendor == "sqlite":
        for trigger in triggers:
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {quote(trigger)}")
    else:
        raise RuntimeError(
            "django-ray legacy-open rollback fencing supports only SQLite and PostgreSQL"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("django_ray", "0019_execution_protocol_schema"),
    ]

    operations = [
        migrations.RunPython(
            _install_legacy_open_rollback_fence,
            _remove_legacy_open_rollback_fence,
        ),
    ]
