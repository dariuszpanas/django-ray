from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ray", "0003_raytaskexecution_completion_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="raytaskexecution",
            name="execution_generation",
            field=models.PositiveBigIntegerField(
                default=0,
                help_text="Monotonic token identifying the current execution generation",
            ),
        ),
    ]
