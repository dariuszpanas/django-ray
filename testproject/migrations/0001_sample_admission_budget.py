"""Create the sample's single shared admission budget."""

import django.utils.timezone
from django.db import migrations, models


def seed_budget(apps, schema_editor):
    apps.get_model("testproject", "SampleAdmissionBudget").objects.using(
        schema_editor.connection.alias
    ).create(id=1, window_started_at=django.utils.timezone.now(), request_count=0)


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="SampleAdmissionBudget",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("window_started_at", models.DateTimeField()),
                ("request_count", models.PositiveIntegerField(default=0)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(id=1), name="sample_admission_singleton"
                    )
                ]
            },
        ),
        migrations.RunPython(seed_budget, migrations.RunPython.noop),
    ]
