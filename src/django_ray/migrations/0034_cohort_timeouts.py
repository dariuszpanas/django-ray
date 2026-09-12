"""Retain an elapsed timeout request without inventing terminal cancellation."""

import django.db.models.deletion
from django.db import migrations, models


def _tables(apps, editor):
    return {
        name: editor.quote_name(apps.get_model("django_ray", name)._meta.db_table)
        for name in (
            "RayTaskCohortTimeout",
            "RayTaskCohortClaim",
            "RayTaskExecution",
            "TaskWorkerLease",
        )
    }


def _install(apps, editor):
    tables = _tables(apps, editor)
    timeout, claim, task, lease = (
        tables[name]
        for name in (
            "RayTaskCohortTimeout",
            "RayTaskCohortClaim",
            "RayTaskExecution",
            "TaskWorkerLease",
        )
    )
    pg = editor.connection.vendor == "postgresql"
    if not pg and editor.connection.vendor != "sqlite":
        raise RuntimeError("Cohort timeouts require SQLite or PostgreSQL")
    true = "TRUE" if pg else "1"
    elapsed = (
        "NEW.deadline_at=NEW.started_at + NEW.timeout_seconds * INTERVAL '1 second'"
        if pg
        else "datetime(NEW.deadline_at)=datetime(NEW.started_at, '+' || NEW.timeout_seconds || ' seconds') AND substr(NEW.deadline_at,20)=substr(NEW.started_at,20)"
    )
    shape = (
        "NEW.timeout_seconds NOT BETWEEN 1 AND 2147483647 OR NEW.requested_at<=NEW.deadline_at OR NOT COALESCE(("
        + elapsed
        + ("), FALSE)" if pg else "), 0)")
    )
    if pg:
        shape += " OR NOT isfinite(NEW.started_at) OR NOT isfinite(NEW.deadline_at) OR NOT isfinite(NEW.requested_at)"
    else:
        shape += " OR typeof(NEW.timeout_seconds)!='integer'"
        for field in ("started_at", "deadline_at", "requested_at"):
            shape += f" OR typeof(NEW.{field})!='text' OR instr(NEW.{field},char(0))!=0 OR julianday(NEW.{field}) IS NULL"
    insert = f"""{shape} OR NOT EXISTS(
        SELECT 1 FROM {claim} c JOIN {task} e ON e.id=c.binding_id
        JOIN {lease} l ON l.worker_id=c.owner_lease_id
        WHERE c.id=NEW.claim_id AND c.disposition IN ('OPEN','HELD') AND c.dispatched_at IS NOT NULL
        AND NEW.requested_at>=c.dispatched_at AND e.execution_protocol_version=3 AND e.state='RUNNING'
        AND e.attempt_number=c.attempt_number AND e.execution_generation=c.execution_generation
        AND e.claimed_by_worker=c.owner_lease_id AND e.completion_data IS NULL AND e.cancellation_status IS NULL
        AND e.started_at=NEW.started_at AND e.timeout_seconds=NEW.timeout_seconds
        AND l.hostname=c.owner_lease_hostname AND l.pid=c.owner_lease_pid
        AND l.started_at=c.owner_lease_started_at AND l.started_at<=NEW.requested_at
        AND l.last_heartbeat_at<=NEW.requested_at AND l.is_active={true} AND l.stopped_at IS NULL
        AND l.min_supported_execution_protocol_version<=3 AND l.max_supported_execution_protocol_version>=3)"""
    protected = f"""EXISTS(SELECT 1 FROM {claim} c JOIN {task} e ON e.id=c.binding_id
        WHERE c.id=OLD.claim_id AND c.disposition!='RESOLVED'
        AND e.attempt_number=c.attempt_number AND e.execution_generation=c.execution_generation
        AND e.state IN ('RUNNING','CANCELLING'))"""
    for name, event, condition in (
        ("insert", "INSERT", insert),
        ("update", "UPDATE", true),
        ("delete", "DELETE", protected),
    ):
        name = f"ray_cohort_timeout_{name}_0034"
        if pg:
            editor.execute(
                f"""CREATE FUNCTION django_ray_{name}() RETURNS trigger AS $$ BEGIN
                IF {condition} THEN RAISE EXCEPTION 'cohort timeout rejected' USING ERRCODE='23514'; END IF;
                RETURN {"OLD" if event == "DELETE" else "NEW"}; END; $$ LANGUAGE plpgsql""",
                params=None,
            )
            editor.execute(
                f"CREATE TRIGGER {name} BEFORE {event} ON {timeout} FOR EACH ROW EXECUTE FUNCTION django_ray_{name}()",
                params=None,
            )
        else:
            editor.execute(
                f"CREATE TRIGGER {name} BEFORE {event} ON {timeout} WHEN {condition} BEGIN SELECT RAISE(ABORT, 'cohort timeout rejected'); END",
                params=None,
            )
    name = "ray_cohort_timeout_parent_delete_0034"
    if pg:
        editor.execute(
            f"CREATE FUNCTION django_ray_{name}() RETURNS trigger AS $$ BEGIN DELETE FROM {timeout} WHERE claim_id=OLD.id; RETURN OLD; END; $$ LANGUAGE plpgsql",
            params=None,
        )
        editor.execute(
            f"CREATE TRIGGER {name} AFTER DELETE ON {claim} FOR EACH ROW EXECUTE FUNCTION django_ray_{name}()",
            params=None,
        )
    else:
        editor.execute(
            f"CREATE TRIGGER {name} AFTER DELETE ON {claim} BEGIN DELETE FROM {timeout} WHERE claim_id=OLD.id; END",
            params=None,
        )


def _remove(apps, editor):
    tables = _tables(apps, editor)
    timeout = tables["RayTaskCohortTimeout"]
    if editor.connection.vendor == "postgresql":
        editor.execute(f"LOCK TABLE {timeout} IN ACCESS EXCLUSIVE MODE", params=None)
    else:
        editor.execute(f"DELETE FROM {timeout} WHERE 1=0", params=None)
    if (
        apps.get_model("django_ray", "RayTaskCohortTimeout")
        .objects.using(editor.connection.alias)
        .exists()
    ):
        raise RuntimeError("Cannot reverse cohort timeouts with retained history")
    for suffix in ("insert", "update", "delete", "parent_delete"):
        name = f"ray_cohort_timeout_{suffix}_0034"
        if editor.connection.vendor == "postgresql":
            table = tables["RayTaskCohortClaim"] if suffix == "parent_delete" else timeout
            editor.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}", params=None)
            editor.execute(f"DROP FUNCTION IF EXISTS django_ray_{name}()", params=None)
        else:
            editor.execute(f"DROP TRIGGER IF EXISTS {name}", params=None)


class Migration(migrations.Migration):
    dependencies = [("django_ray", "0033_cohort_job_cleanup")]
    operations = [
        migrations.CreateModel(
            name="RayTaskCohortTimeout",
            fields=[
                (
                    "claim",
                    models.OneToOneField(
                        editable=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="timeout_intent",
                        serialize=False,
                        to="django_ray.raytaskcohortclaim",
                    ),
                ),
                ("requested_at", models.DateTimeField(editable=False)),
                ("started_at", models.DateTimeField(editable=False)),
                ("timeout_seconds", models.PositiveIntegerField(editable=False)),
                ("deadline_at", models.DateTimeField(editable=False)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(timeout_seconds__gte=1, timeout_seconds__lte=2147483647),
                        name="ray_cohort_timeout_seconds",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(requested_at__gt=models.F("deadline_at"))
                        & models.Q(deadline_at__gt=models.F("started_at")),
                        name="ray_cohort_timeout_chronology",
                    ),
                ]
            },
        ),
        migrations.RunPython(_install, _remove),
    ]
