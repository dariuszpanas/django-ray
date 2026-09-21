"""Retain bounded counters with their exact published workflow run."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("django_ray", "0026_ray_task_target_execution_evidence")]

    operations = [
        migrations.AddField(
            model_name="workflowprogressrunstorage",
            name="reporting_diagnostics_json",
            field=models.CharField(blank=True, editable=False, max_length=16384, null=True),
        ),
    ]
