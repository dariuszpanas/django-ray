"""Tiny held tasks let the observer separate startup from completion latency."""

import json
import time
from pathlib import Path

from django.conf import settings
from django.tasks import task

import django_ray
from django_ray.runtime.context import get_current_task_execution_pk
from qualification.latency import apps


@task
def held_result(*, fail=False):
    task_pk = get_current_task_execution_pk()
    assert type(task_pk) is int
    apps.observed_task_pk = task_pk
    with (settings.ROOT / f"started-{task_pk}.json").open("x") as stream:
        json.dump(
            {"started_ns": time.monotonic_ns(), "module": str(Path(django_ray.__file__).resolve())},
            stream,
        )
    deadline = time.monotonic() + 60
    while not (settings.ROOT / f"release-{task_pk}").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("qualification task was not released")
        time.sleep(0.02)
    if fail:
        raise ValueError("qualification expected failure")
    return {"value": 42, "task_pk": task_pk}
