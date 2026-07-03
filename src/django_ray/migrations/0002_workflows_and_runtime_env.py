from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ray", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="raytaskexecution",
            name="progress_data",
            field=models.TextField(
                blank=True,
                help_text="JSON workflow progress snapshot for the durable outer task",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="raytaskexecution",
            name="runtime_env_profile",
            field=models.CharField(
                blank=True,
                help_text="Named RuntimeEnv profile selected when this task was enqueued",
                max_length=100,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="raytaskexecution",
            name="runtime_env_json",
            field=models.TextField(
                default="{}",
                help_text="Immutable JSON snapshot of the Ray RuntimeEnv used for this task",
            ),
        ),
        migrations.AddField(
            model_name="raytaskexecution",
            name="runtime_env_hash",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="SHA-256 identity of the RuntimeEnv snapshot",
                max_length=64,
            ),
        ),
    ]
