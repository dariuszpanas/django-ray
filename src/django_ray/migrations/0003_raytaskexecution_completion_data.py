from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ray", "0002_workflows_and_runtime_env"),
    ]

    operations = [
        migrations.AddField(
            model_name="raytaskexecution",
            name="completion_data",
            field=models.TextField(
                blank=True,
                help_text="JSON completion envelope durably written by the Ray Job driver",
                null=True,
            ),
        ),
    ]
