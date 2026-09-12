"""Activate the current cohort only after a coordinated stopped-writer upgrade.

No data is converted or deleted to manufacture a drain. Operators stop old
writers and independently confirm remote cleanup before applying this migration;
the database checks below are necessary preconditions, not remote cleanup proof.
"""

import django.core.validators
from django.db import migrations, models
from django.utils import timezone

_MAX = 9223372036854775807
_NONTERMINAL = ("QUEUED", "RUNNING", "CANCELLING")
_TABLE_MODELS = (
    "TaskWorkerLease",
    "RayTaskExecution",
    "RayTaskCohortClaim",
    "RayCohortJobCleanup",
    "TaskAttempt",
    "TaskExecutionProtocolPolicy",
    "LegacyWorkerAdmissionToken",
)
_SNAPSHOT = "_django_ray_activation_sqlite_triggers"


def _tables(apps, editor):
    return {
        name: editor.quote_name(apps.get_model("django_ray", name)._meta.db_table)
        for name in _TABLE_MODELS
    }


def _lock(apps, editor):
    tables = _tables(apps, editor)
    vendor = editor.connection.vendor
    if vendor == "postgresql":
        # Existing admission is maintenance-barrier -> lease -> policy. This
        # stopped-writer DDL transaction is not an online epoch coordinator.
        editor.execute("SELECT pg_advisory_xact_lock(1684697721, 368)", params=None)
        editor.execute("SELECT pg_advisory_xact_lock(1684697721, 1)", params=None)
        editor.execute(
            "LOCK TABLE " + ", ".join(tables.values()) + " IN ACCESS EXCLUSIVE MODE",
            params=None,
        )
    elif vendor == "sqlite":
        editor.execute(
            'UPDATE "django_ray_raymaintenancepolicy" SET revision=revision WHERE singleton_key=1',
            params=None,
        )
    else:
        raise RuntimeError("Current-cohort activation requires SQLite or PostgreSQL")


def _policy(apps, editor, *, active):
    using = editor.connection.alias
    policies = apps.get_model("django_ray", "TaskExecutionProtocolPolicy").objects.using(using)
    tokens = apps.get_model("django_ray", "LegacyWorkerAdmissionToken").objects.using(using)
    rows = list(policies.all())
    if len(rows) != 1:
        raise RuntimeError("Current-cohort activation requires one coherent protocol policy")
    row = rows[0]
    expected_tokens = [1] if row.legacy_worker_admission_enabled else []
    if (
        row.singleton_key != 1
        or row.schema_version != 1
        or row.active_write_protocol_version != active
        or type(row.revision) is not int
        or not 1 <= row.revision < _MAX
        or (active == 3 and row.legacy_worker_admission_enabled)
        or list(tokens.values_list("singleton_key", flat=True)) != expected_tokens
    ):
        raise RuntimeError("Current-cohort activation requires one coherent protocol policy")
    return row


def _pending(apps, editor):
    using = editor.connection.alias
    executions = apps.get_model("django_ray", "RayTaskExecution").objects.using(using)
    leases = apps.get_model("django_ray", "TaskWorkerLease").objects.using(using)
    if (
        executions.filter(state__in=_NONTERMINAL)
        .exclude(execution_protocol_version=3, metadata_schema_version=1)
        .exists()
    ):
        raise RuntimeError("Current-cohort activation refuses unsupported nonterminal work")
    if (
        leases.filter(is_active=True)
        .exclude(
            capability_schema_version=1,
            legacy_admission_token__isnull=True,
            min_supported_execution_protocol_version=3,
            max_supported_execution_protocol_version=3,
        )
        .exists()
    ):
        raise RuntimeError("Current-cohort activation refuses unsupported active worker leases")
    if (
        apps.get_model("django_ray", "RayTaskCohortClaim")
        .objects.using(using)
        .exclude(disposition="RESOLVED")
        .exists()
    ):
        raise RuntimeError("Current-cohort activation refuses unresolved cohort claims")
    if (
        apps.get_model("django_ray", "RayCohortJobCleanup")
        .objects.using(using)
        .filter(state="OPEN")
        .exists()
    ):
        raise RuntimeError("Current-cohort activation refuses pending Jobs cleanup")


def _suspend_sqlite(editor):
    if editor.connection.vendor != "sqlite":
        return
    # Altering a SQLite column default remakes its table. External triggers can
    # refer to that temporarily absent table, and attached triggers would be
    # lost. Preserve their exact source, including deployment-owned triggers,
    # inside this one transactional schema editor; no runtime bypass is added.
    with editor.connection.cursor() as cursor:
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
        saved = tuple(cursor.fetchall())
    setattr(editor, _SNAPSHOT, saved)
    for name, _sql in saved:
        editor.execute(f"DROP TRIGGER {editor.quote_name(name)}", params=None)


def _restore_sqlite(editor):
    if editor.connection.vendor != "sqlite":
        return
    for _name, sql in getattr(editor, _SNAPSHOT):
        editor.execute(sql, params=None)
    delattr(editor, _SNAPSHOT)


def _activate(apps, editor):
    _lock(apps, editor)
    policy = _policy(apps, editor, active=1)
    _pending(apps, editor)
    using = editor.connection.alias
    apps.get_model("django_ray", "TaskWorkerLease").objects.using(using).filter(
        is_active=False, legacy_admission_token_id=1
    ).update(legacy_admission_token_id=None)
    policy.active_write_protocol_version = 3
    policy.legacy_worker_admission_enabled = False
    policy.revision += 1
    policy.updated_at = timezone.now()
    policy.save(
        using=using,
        update_fields=(
            "active_write_protocol_version",
            "legacy_worker_admission_enabled",
            "revision",
            "updated_at",
        ),
    )
    apps.get_model("django_ray", "LegacyWorkerAdmissionToken").objects.using(using).all().delete()
    _suspend_sqlite(editor)


def _trigger(apps, editor, suffix, model, event, condition):
    name = f"ray_current_{suffix}_0035"
    table = _tables(apps, editor)[model]
    if editor.connection.vendor == "postgresql":
        editor.execute(
            f"""CREATE FUNCTION django_ray_{name}() RETURNS trigger AS $$ BEGIN
            IF {condition} THEN
              RAISE EXCEPTION 'current-cohort activation fence rejected' USING ERRCODE='23514';
            END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql""",
            params=None,
        )
        editor.execute(
            f"CREATE TRIGGER {name} BEFORE {event} ON {table} FOR EACH ROW EXECUTE FUNCTION django_ray_{name}()",
            params=None,
        )
    else:
        editor.execute(
            f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT, 'current-cohort activation fence rejected'); END",
            params=None,
        )


def _install(apps, editor):
    _restore_sqlite(editor)
    tables = _tables(apps, editor)
    pg = editor.connection.vendor == "postgresql"
    unequal = "IS DISTINCT FROM" if pg else "IS NOT"
    false = "FALSE" if pg else "0"
    true = "TRUE" if pg else "1"
    policy = tables["TaskExecutionProtocolPolicy"]
    token = tables["LegacyWorkerAdmissionToken"]
    unavailable = f"""NOT EXISTS (SELECT 1 FROM {policy} p
      WHERE p.singleton_key=1 AND p.schema_version=1 AND p.active_write_protocol_version=3
      AND p.legacy_worker_admission_enabled={false} AND p.revision>=1)
      OR EXISTS(SELECT 1 FROM {token})"""
    wrong_execution = (
        f"NEW.execution_protocol_version {unequal} 3 OR NEW.metadata_schema_version {unequal} 1"
    )
    _trigger(
        apps,
        editor,
        "execution_insert",
        "RayTaskExecution",
        "INSERT",
        f"({unavailable}) OR {wrong_execution}",
    )
    new_work = f"""NEW.state IN ('QUEUED','RUNNING','CANCELLING')
      OR NEW.attempt_number {unequal} OLD.attempt_number
      OR NEW.execution_generation {unequal} OLD.execution_generation"""
    admission = f"""OLD.state NOT IN ('QUEUED','RUNNING','CANCELLING')
      OR NEW.attempt_number {unequal} OLD.attempt_number
      OR NEW.execution_generation {unequal} OLD.execution_generation
      OR (NEW.state IN ('RUNNING','CANCELLING') AND
          (OLD.state='QUEUED' OR NEW.claimed_by_worker {unequal} OLD.claimed_by_worker))"""
    _trigger(
        apps,
        editor,
        "execution_update",
        "RayTaskExecution",
        "UPDATE",
        f"(({new_work}) AND ({wrong_execution})) OR (({new_work}) AND ({admission}) AND ({unavailable}))",
    )
    wrong_lease = f"""NEW.capability_schema_version {unequal} 1
      OR NEW.min_supported_execution_protocol_version {unequal} 3
      OR NEW.max_supported_execution_protocol_version {unequal} 3
      OR NEW.legacy_admission_token_id IS NOT NULL"""
    for suffix, event in (("insert", "INSERT"), ("update", "UPDATE")):
        _trigger(
            apps,
            editor,
            f"lease_{suffix}",
            "TaskWorkerLease",
            event,
            f"NEW.is_active={true} AND (({wrong_lease}) OR ({unavailable}))",
        )
    _trigger(
        apps,
        editor,
        "policy_insert",
        "TaskExecutionProtocolPolicy",
        "INSERT",
        f"NEW.active_write_protocol_version {unequal} 3 OR NEW.legacy_worker_admission_enabled {unequal} {false} OR NEW.revision NOT BETWEEN 1 AND {_MAX} OR EXISTS(SELECT 1 FROM {token})"
        + (" OR typeof(NEW.revision)!='integer'" if not pg else ""),
    )
    _trigger(
        apps,
        editor,
        "policy_update",
        "TaskExecutionProtocolPolicy",
        "UPDATE",
        " OR ".join(
            f"NEW.{field} {unequal} OLD.{field}"
            for field in (
                "singleton_key",
                "schema_version",
                "active_write_protocol_version",
                "legacy_worker_admission_enabled",
                "revision",
                "updated_at",
            )
        ),
    )
    _trigger(apps, editor, "token_insert", "LegacyWorkerAdmissionToken", "INSERT", true)


def _remove(apps, editor):
    _lock(apps, editor)
    _policy(apps, editor, active=3)
    using = editor.connection.alias
    # An explicit stopped, empty-cohort reversal may restore the prior dormant
    # schema, but cannot reinterpret retained current work or execution history.
    for model, filters in (
        ("RayTaskExecution", {"execution_protocol_version": 3}),
        ("TaskAttempt", {"execution_protocol_version": 3}),
        ("TaskWorkerLease", {"is_active": True}),
        ("RayTaskCohortClaim", {}),
        ("RayCohortJobCleanup", {}),
    ):
        if apps.get_model("django_ray", model).objects.using(using).filter(**filters).exists():
            raise RuntimeError(
                "Cannot reverse current-cohort activation with retained cohort history or active writers"
            )
    tables = _tables(apps, editor)
    for suffix, model in (
        ("execution_insert", "RayTaskExecution"),
        ("execution_update", "RayTaskExecution"),
        ("lease_insert", "TaskWorkerLease"),
        ("lease_update", "TaskWorkerLease"),
        ("policy_insert", "TaskExecutionProtocolPolicy"),
        ("policy_update", "TaskExecutionProtocolPolicy"),
        ("token_insert", "LegacyWorkerAdmissionToken"),
    ):
        name = f"ray_current_{suffix}_0035"
        if editor.connection.vendor == "postgresql":
            editor.execute(f"DROP TRIGGER {name} ON {tables[model]}", params=None)
            editor.execute(f"DROP FUNCTION django_ray_{name}()", params=None)
        else:
            editor.execute(f"DROP TRIGGER {name}", params=None)
    _suspend_sqlite(editor)


def _deactivate(apps, editor):
    _restore_sqlite(editor)
    policy = _policy(apps, editor, active=3)
    policy.active_write_protocol_version = 1
    policy.revision += 1
    policy.updated_at = timezone.now()
    policy.save(
        using=editor.connection.alias,
        update_fields=("active_write_protocol_version", "revision", "updated_at"),
    )
    # Legacy admission deliberately stays closed. Reopening requires the prior
    # explicit stopped-producer rollout service after this empty reversal.


class Migration(migrations.Migration):
    dependencies = [("django_ray", "0034_cohort_timeouts")]
    operations = [
        migrations.RunPython(_activate, _deactivate),
        migrations.AlterField(
            model_name="raytaskexecution",
            name="execution_protocol_version",
            field=models.PositiveSmallIntegerField(
                db_default=3,
                db_index=True,
                default=3,
                editable=False,
                help_text="Immutable durable execution protocol selected when the task was created",
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="taskattempt",
            name="execution_protocol_version",
            field=models.PositiveSmallIntegerField(
                db_default=3,
                default=3,
                editable=False,
                help_text="Durable execution protocol archived for this attempt",
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="taskexecutionprotocolpolicy",
            name="active_write_protocol_version",
            field=models.PositiveSmallIntegerField(
                db_default=3,
                default=3,
                editable=False,
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="taskexecutionprotocolpolicy",
            name="legacy_worker_admission_enabled",
            field=models.BooleanField(db_default=False, default=False, editable=False),
        ),
        migrations.RunPython(_install, _remove),
    ]
