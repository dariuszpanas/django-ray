"""Seed an explicit common lease policy without rewriting retained ownership."""

import django.utils.timezone
from django.db import migrations, models


def seed_policy(apps, schema_editor):
    policy = apps.get_model("django_ray", "TaskWorkerLeasePolicy")
    policy.objects.using(schema_editor.connection.alias).create(
        singleton_key=1, schema_version=1, lease_seconds=60, revision=1
    )


def require_quiescent_reverse(apps, schema_editor):
    using = schema_editor.connection.alias
    leases = apps.get_model("django_ray", "TaskWorkerLease")
    tasks = apps.get_model("django_ray", "RayTaskExecution")
    if (
        leases.objects.using(using).filter(is_active=True).exists()
        or tasks.objects.using(using).filter(state__in=("RUNNING", "CANCELLING")).exists()
    ):
        raise RuntimeError("lease_policy_active_cohort")


class Migration(migrations.Migration):
    dependencies = [("django_ray", "0027_workflow_reporting_diagnostics")]

    operations = [
        migrations.CreateModel(
            name="TaskWorkerLeasePolicy",
            fields=[
                (
                    "singleton_key",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("schema_version", models.PositiveSmallIntegerField(default=1, editable=False)),
                ("lease_seconds", models.PositiveIntegerField(default=60, editable=False)),
                ("revision", models.PositiveBigIntegerField(default=1, editable=False)),
                (
                    "updated_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(singleton_key=1), name="ray_lease_policy_singleton"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(schema_version=1), name="ray_lease_policy_schema"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(lease_seconds__gte=1, lease_seconds__lte=86400),
                        name="ray_lease_policy_seconds",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(revision__gte=1, revision__lte=9223372036854775807),
                        name="ray_lease_policy_revision",
                    ),
                ]
            },
        ),
        migrations.RunPython(seed_policy, migrations.RunPython.noop),
        migrations.RunPython(migrations.RunPython.noop, require_quiescent_reverse),
    ]
