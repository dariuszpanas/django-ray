"""Two bounded cooperative callables; only the no-cancel control is released."""

import re
import time

from django.conf import settings
from django.tasks import task

from django_ray.runtime.context import get_current_task_context
from qualification.cancellation.process_witness import write_start


@task(backend="cancellation", queue_name="cancellation")
def controlled(case, nonce):
    if case not in {"control", "cancel"} or not re.fullmatch("[0-9a-f]{32}", nonce):
        raise ValueError("Unknown cancellation fixture")
    context = get_current_task_context()
    if context is None:
        raise ValueError("Missing durable cancellation context")
    root = settings.ROOT / case
    execution = [
        context.task_pk,
        context.task_id,
        context.attempt_number,
        context.execution_generation,
    ]
    write_start(root / "started.json", nonce=nonce, case=case, execution=execution)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with (root / "progress").open("ab") as stream:
            stream.write(b"x")
        if (root / "release").exists():
            if case != "control":
                raise ValueError("Cancellation target was released")
            (root / "completed").touch(exist_ok=False)
            return {"control": "completed"}
        time.sleep(0.1)
    (root / "expired").touch(exist_ok=False)
    raise TimeoutError("Owned cancellation fixture expired")
