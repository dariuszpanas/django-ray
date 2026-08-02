from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("django_ray", "0016_raytaskexecution_queue_expiration")]

    operations = [
        migrations.AlterModelOptions(
            name="raytaskexecution",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    (
                        "view_sensitive_task_data",
                        "Can view unredacted task data",
                    )
                ],
                "verbose_name": "Ray Task Execution",
                "verbose_name_plural": "Ray Task Executions",
            },
        ),
    ]
