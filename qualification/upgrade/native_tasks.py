"""Real remote work with bounded, observable release and failure controls."""

import time

from django.conf import settings
from django.tasks import task


@task
def controlled(case, payload):
    if case not in {"success", "failure", "retry", "cancelled"}:
        raise ValueError("unknown qualification case")
    if case == "cancelled":
        raise AssertionError("cancelled queued task was invoked")
    (settings.ROOT / f"started-{case}").touch()
    deadline = time.monotonic() + 90
    while not (settings.ROOT / f"release-{case}").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("qualification task was not released")
        time.sleep(0.05)
    if case == "failure" or (case == "retry" and not (settings.ROOT / "retry-again").exists()):
        raise ValueError("expected qualification failure")
    return {"value": 42, "payload": payload}
