"""Fixed harmless work for the installed worker-mode observation."""

import os
from pathlib import Path

from django.tasks import task


@task
def mode_value():
    import ray

    import django_ray

    return {
        "value": 42,
        "pid": os.getpid(),
        "module": str(Path(django_ray.__file__).resolve()),
        "ray_initialized": ray.is_initialized(),
    }
