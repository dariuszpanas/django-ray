import os
from datetime import timedelta

from django.db import migrations, models

DEFAULT_QUEUE_TIMEOUT_SECONDS = 24 * 60 * 60
EXISTING_UNLIMITED_ENV = "DJANGO_RAY_EXISTING_QUEUED_UNLIMITED"


def snapshot_existing_queued_deadlines(apps, schema_editor) -> None:
    execution = apps.get_model("django_ray", "RayTaskExecution")
    alias = schema_editor.connection.alias
    executions = execution.objects.using(alias)
    queued = executions.filter(state="QUEUED")

    # Every legacy execution needs an explicit retry policy, including RUNNING
    # work that can fail and requeue after the upgrade and terminal work that an
    # operator can retry later. The opt-out below is intentionally narrower: it
    # preserves only the backlog that is already queued at migration time.
    executions.update(queue_timeout_seconds=DEFAULT_QUEUE_TIMEOUT_SECONDS)
    if os.environ.get(EXISTING_UNLIMITED_ENV) == "1":
        queued.update(queue_timeout_seconds=None, queue_deadline_at=None)
        return

    batch = []
    for row in queued.only("pk", "created_at", "run_after").order_by("pk").iterator(chunk_size=500):
        eligibility_at = max(row.created_at, row.run_after) if row.run_after else row.created_at
        row.queue_deadline_at = eligibility_at + timedelta(seconds=DEFAULT_QUEUE_TIMEOUT_SECONDS)
        batch.append(row)
        if len(batch) == 500:
            executions.bulk_update(batch, ["queue_deadline_at"], batch_size=500)
            batch.clear()
    if batch:
        executions.bulk_update(batch, ["queue_deadline_at"], batch_size=500)


def restore_legacy_terminal_states(apps, schema_editor) -> None:
    """Keep expired work terminal for code that predates the EXPIRED state."""
    alias = schema_editor.connection.alias
    apps.get_model("django_ray", "TaskAttempt").objects.using(alias).filter(state="EXPIRED").update(
        state="FAILED"
    )
    apps.get_model("django_ray", "RayTaskExecution").objects.using(alias).filter(
        state="EXPIRED"
    ).update(state="FAILED")


class Migration(migrations.Migration):
    dependencies = [("django_ray", "0015_raytaskexecution_task_id_unique")]

    operations = [
        migrations.AlterField(
            model_name="raytaskexecution",
            name="state",
            field=models.CharField(
                choices=[
                    ("QUEUED", "Queued"),
                    ("RUNNING", "Running"),
                    ("SUCCEEDED", "Succeeded"),
                    ("FAILED", "Failed"),
                    ("CANCELLED", "Cancelled"),
                    ("CANCELLING", "Cancelling"),
                    ("LOST", "Lost"),
                    ("EXPIRED", "Expired"),
                ],
                db_index=True,
                default="QUEUED",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="taskattempt",
            name="state",
            field=models.CharField(
                choices=[
                    ("QUEUED", "Queued"),
                    ("RUNNING", "Running"),
                    ("SUCCEEDED", "Succeeded"),
                    ("FAILED", "Failed"),
                    ("CANCELLED", "Cancelled"),
                    ("CANCELLING", "Cancelling"),
                    ("LOST", "Lost"),
                    ("EXPIRED", "Expired"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="raytaskexecution",
            name="queue_deadline_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Absolute instant at which queued work expires",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="raytaskexecution",
            name="queue_timeout_seconds",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Snapshotted queued-wait budget in seconds (None = unlimited)",
                null=True,
            ),
        ),
        migrations.RunPython(snapshot_existing_queued_deadlines, restore_legacy_terminal_states),
        migrations.AddIndex(
            model_name="raytaskexecution",
            index=models.Index(
                fields=["state", "queue_name", "queue_deadline_at"],
                name="ray_task_expiry_idx",
            ),
        ),
    ]
