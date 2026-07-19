from django.db import migrations, models

CONSTRAINT_NAME = "ray_task_priority_valid_range"


def _priority_constraint():
    return models.CheckConstraint(
        condition=models.Q(priority__gte=-100, priority__lte=100),
        name=CONSTRAINT_NAME,
    )


def _postgres_constraint_state(schema_editor, model):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT convalidated
            FROM pg_constraint
            WHERE conname = %s AND conrelid = to_regclass(%s)
            """,
            [CONSTRAINT_NAME, model._meta.db_table],
        )
        return cursor.fetchone()


def add_priority_constraint(apps, schema_editor):
    model = apps.get_model("django_ray", "RayTaskExecution")
    constraint = _priority_constraint()
    if schema_editor.connection.vendor != "postgresql":
        # SQLite rebuilds the table from model state when adding a check. The
        # database operation intentionally runs before the separate state operation,
        # so expose the constraint only for the duration of that rebuild.
        if schema_editor.connection.vendor == "sqlite":
            model._meta.constraints.append(constraint)
            try:
                schema_editor.add_constraint(model, constraint)
            finally:
                model._meta.constraints.remove(constraint)
        else:
            schema_editor.add_constraint(model, constraint)
        return

    state = _postgres_constraint_state(schema_editor, model)
    if state is not None:
        return
    table = schema_editor.quote_name(model._meta.db_table)
    name = schema_editor.quote_name(CONSTRAINT_NAME)
    column = schema_editor.quote_name("priority")
    schema_editor.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} "
        f"CHECK ({column} >= -100 AND {column} <= 100) NOT VALID"
    )


def validate_priority_constraint(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    model = apps.get_model("django_ray", "RayTaskExecution")
    state = _postgres_constraint_state(schema_editor, model)
    if state is None or state[0]:
        return
    table = schema_editor.quote_name(model._meta.db_table)
    name = schema_editor.quote_name(CONSTRAINT_NAME)
    schema_editor.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def remove_priority_constraint(apps, schema_editor):
    model = apps.get_model("django_ray", "RayTaskExecution")
    if schema_editor.connection.vendor == "postgresql":
        table = schema_editor.quote_name(model._meta.db_table)
        name = schema_editor.quote_name(CONSTRAINT_NAME)
        schema_editor.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        return
    schema_editor.remove_constraint(model, _priority_constraint())


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("django_ray", "0007_raytaskexecution_priority"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    add_priority_constraint,
                    remove_priority_constraint,
                ),
                migrations.RunPython(
                    validate_priority_constraint,
                    migrations.RunPython.noop,
                ),
            ],
            state_operations=[
                migrations.AddConstraint(
                    model_name="raytaskexecution",
                    constraint=_priority_constraint(),
                )
            ],
        ),
    ]
