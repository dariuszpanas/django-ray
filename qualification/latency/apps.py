"""Observe the real Job's autocommitted completion write without changing it."""

import json
import time

from django.apps import AppConfig
from django.db.backends.signals import connection_created

observed_task_pk = None


def observe_completion(execute, sql, params, many, context):
    write_started_ns = time.monotonic_ns()
    result = execute(sql, params, many, context)
    write_returned_ns = time.monotonic_ns()
    # The production writer updates only this column. Do not retain SQL/params.
    if observed_task_pk is not None and sql.startswith(
        'UPDATE "django_ray_raytaskexecution" SET "completion_data" ='
    ):
        from django.conf import settings

        assert context["connection"].get_autocommit()
        path = settings.ROOT / f"completion-{observed_task_pk}.json"
        with path.open("x") as stream:
            json.dump(
                {"write_started_ns": write_started_ns, "committed_ns": write_returned_ns}, stream
            )
    return result


def connect_observer(sender, connection, **kwargs):
    connection.execute_wrappers.append(observe_completion)


class ProbeConfig(AppConfig):
    name = "qualification.latency"

    def ready(self):
        connection_created.connect(connect_observer, weak=False)
