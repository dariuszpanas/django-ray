"""Two fixed, bounded effects used only by the application Jobs qualification."""

import time

from django.conf import settings
from django.tasks import task


@task
def controlled(case: str, marker: str) -> dict:
    """Expose a single invocation while a replacement manager adopts its Job."""
    if (
        case not in {"success", "missing"}
        or len(marker) != 32
        or not all(char in "0123456789abcdef" for char in marker)
    ):
        raise ValueError("Unknown Jobs qualification fixture")
    root = settings.RQ2_ROOT
    with (root / f"{case}-effects").open("a") as stream:
        stream.write("x")
    if case == "success":
        deadline = time.monotonic() + 90
        while not (root / "success-release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Jobs qualification release was not observed")
            time.sleep(0.1)
    return {"value": 42, "effect": "once"}
