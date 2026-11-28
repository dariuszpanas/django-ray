"""Add DB-clock lease expiration, policy revision, and partial hot-path indexes."""

from __future__ import annotations

from datetime import timedelta

import django.core.validators
from django.db import migrations, models
from django.db.models import DateTimeField, ExpressionWrapper, F, Value

_CAPTURED_SQLITE_TRIGGERS: dict[tuple[str, str], list[tuple[str, str]]] = {}
_LEASE_TABLE = "django_ray_taskworkerlease"


def _drop_and_capture_sqlite_lease_triggers(apps, schema_editor) -> None:
    """Capture and drop SQLite triggers on or referencing TaskWorkerLease before table rebuild."""
    del apps
    connection = schema_editor.connection
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL "
            "AND (tbl_name = %s OR LOWER(sql) LIKE %s) "
            "ORDER BY name",
            [_LEASE_TABLE, f"%{_LEASE_TABLE}%"],
        )
        rows = [(str(name), str(sql)) for name, sql in cursor.fetchall()]
        quote = schema_editor.quote_name
        for name, _sql in rows:
            cursor.execute(f"DROP TRIGGER IF EXISTS {quote(name)}")
    _CAPTURED_SQLITE_TRIGGERS[(connection.alias, _LEASE_TABLE)] = rows


def _restore_sqlite_lease_triggers(apps, schema_editor) -> None:
    """Restore SQLite triggers captured around TaskWorkerLease table rebuild DDL."""
    del apps
    connection = schema_editor.connection
    if connection.vendor != "sqlite":
        return
    captured = _CAPTURED_SQLITE_TRIGGERS.pop((connection.alias, _LEASE_TABLE), None)
    if not captured:
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        existing = {str(row[0]) for row in cursor.fetchall()}
        for name, sql in captured:
            if name not in existing:
                cursor.execute(sql)


def _backfill_lease_engine_fields(apps, schema_editor) -> None:
    """Populate lease_expires_at and lease_policy_revision for existing leases."""
    using = schema_editor.connection.alias
    policy_model = apps.get_model("django_ray", "TaskWorkerLeasePolicy")
    lease_model = apps.get_model("django_ray", "TaskWorkerLease")
    row = (
        policy_model.objects.using(using)
        .filter(singleton_key=1)
        .values_list("lease_seconds", "revision")
        .first()
    )
    lease_seconds, revision = (int(row[0]), int(row[1])) if row is not None else (60, 1)
    lease_model.objects.using(using).filter(lease_expires_at__isnull=True).update(
        lease_expires_at=ExpressionWrapper(
            F("last_heartbeat_at") + Value(timedelta(seconds=lease_seconds)),
            output_field=DateTimeField(),
        ),
        lease_policy_revision=revision,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("django_ray", "0028_worker_lease_policy"),
    ]

    operations = [
        migrations.RunPython(
            _drop_and_capture_sqlite_lease_triggers,
            _restore_sqlite_lease_triggers,
        ),
        migrations.AddField(
            model_name="taskworkerlease",
            name="lease_expires_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Database-clock expiration timestamp for this worker lease (unindexed for PG HOT)",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="taskworkerlease",
            name="lease_policy_revision",
            field=models.PositiveBigIntegerField(
                db_default=1,
                default=1,
                help_text="Lease policy revision stamped when this worker lease was acquired",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(9223372036854775807),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="taskworkerlease",
            name="queue_name",
            field=models.CharField(
                db_index=True,
                default="default",
                help_text="Queue(s) this worker is processing (informational only)",
                max_length=1024,
            ),
        ),
        migrations.RunPython(
            _restore_sqlite_lease_triggers,
            _drop_and_capture_sqlite_lease_triggers,
        ),
        migrations.RunPython(
            _backfill_lease_engine_fields,
            migrations.RunPython.noop,
        ),
        migrations.AddIndex(
            model_name="taskworkerlease",
            index=models.Index(
                condition=models.Q(("is_active", True)),
                fields=["worker_id"],
                name="ray_lease_active_worker_idx",
            ),
        ),
        migrations.RemoveIndex(
            model_name="raytaskexecution",
            name="ray_task_heartbeat_idx",
        ),
        migrations.RemoveIndex(
            model_name="raytaskexecution",
            name="ray_task_claimable_idx",
        ),
        migrations.RemoveIndex(
            model_name="raytaskexecution",
            name="ray_task_expiry_idx",
        ),
        migrations.AddIndex(
            model_name="raytaskexecution",
            index=models.Index(
                condition=models.Q(("state", "QUEUED")),
                fields=["queue_name", "-priority", "created_at", "id"],
                name="ray_task_claim_hot_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="raytaskexecution",
            index=models.Index(
                condition=models.Q(("state__in", ("RUNNING", "CANCELLING"))),
                fields=["state", "id", "queue_name"],
                name="ray_task_active_maint_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="raytaskexecution",
            index=models.Index(
                condition=models.Q(("queue_deadline_at__isnull", False), ("state", "QUEUED")),
                fields=["queue_name", "queue_deadline_at", "id"],
                name="ray_task_queued_exp_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="raytaskexecution",
            index=models.Index(
                condition=models.Q(
                    ("finished_at__isnull", False),
                    (
                        "state__in",
                        ("SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"),
                    ),
                ),
                fields=["finished_at", "id"],
                name="ray_task_term_purge_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="raytaskexecution",
            index=models.Index(
                condition=models.Q(("result_reference__isnull", False)),
                fields=["result_reference"],
                name="ray_task_result_ref_idx",
            ),
        ),
        migrations.RemoveIndex(
            model_name="taskattempt",
            name="django_ray__executi_31dfd4_idx",
        ),
        migrations.AddIndex(
            model_name="taskattempt",
            index=models.Index(
                fields=["state"],
                name="ray_attempt_state_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="taskattempt",
            index=models.Index(
                condition=models.Q(("result_reference__isnull", False)),
                fields=["result_reference"],
                name="ray_attempt_result_ref_idx",
            ),
        ),
        migrations.RemoveIndex(
            model_name="raytargetpolicyrevision",
            name="ray_tpolicy_latest_idx",
        ),
        migrations.RemoveIndex(
            model_name="raytargetattestationrevision",
            name="ray_tattest_latest_idx",
        ),
        migrations.RemoveIndex(
            model_name="raytargetrouterevision",
            name="ray_troute_latest_idx",
        ),
    ]
