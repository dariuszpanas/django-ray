"""Real remote work with bounded, observable release and failure controls."""

import importlib
import os
import time
from pathlib import Path

from django.conf import settings
from django.tasks import task


@task
def controlled(case, payload):
    import ray

    import django_ray

    assert ray.__version__ == ("2.56.0" if django_ray.__version__ == "0.4.0" else "2.58.0")
    if settings.RUNNER == "ray_job":
        assert os.environ.get("QUALIFICATION_RUNTIME_MARKER") == "delivered"
        bundle = importlib.import_module("upgrade_bundle")
        assert Path(bundle.__file__).resolve().is_relative_to(Path.cwd())
        assert bundle.marker() == "delivered-upgrade-artifact"
    if case not in {"success", "failure", "retry", "cancelled"}:
        raise ValueError("unknown qualification case")
    if case == "cancelled":
        raise AssertionError("cancelled queued task was invoked")
    if settings.CRASH_MANAGER and case == "success":
        with (settings.ROOT / "success-invocations").open("a") as marker:
            marker.write("x")
    (settings.ROOT / f"started-{case}").touch()
    deadline = time.monotonic() + 90
    while not (settings.ROOT / f"release-{case}").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("qualification task was not released")
        time.sleep(0.05)
    if case == "failure" or (case == "retry" and not (settings.ROOT / "retry-again").exists()):
        raise ValueError("expected qualification failure")
    if case == "success":
        from qualification.upgrade.native_workflow import run

        return run(payload)
    return {"value": 42, "payload": payload}
