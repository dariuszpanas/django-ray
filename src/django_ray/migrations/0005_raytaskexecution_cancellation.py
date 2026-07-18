from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ray", "0004_raytaskexecution_execution_generation"),
    ]

    operations = [
        migrations.AddField(
            model_name="raytaskexecution",
            name="cancellation_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("REQUESTED", "Stop requested"),
                    ("FAILED", "Stop request failed"),
                    ("INDETERMINATE", "Stop request indeterminate"),
                    ("NOT_APPLICABLE", "No remote job to stop"),
                ],
                help_text="Outcome of the most recent remote cancellation request",
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="raytaskexecution",
            name="cancellation_error",
            field=models.TextField(
                blank=True,
                help_text="Details when remote cancellation failed or was indeterminate",
                null=True,
            ),
        ),
    ]
