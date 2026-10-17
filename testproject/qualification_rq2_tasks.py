"""Two fixed, bounded effects used only by the application Jobs qualification."""

import json
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
    if case == "success":
        from testproject.qualification_process import observe_driver

        try:
            observed = observe_driver(marker)
            with (root / "success-process.json").open("x") as stream:
                stream.write(json.dumps(observed, sort_keys=True))
        except Exception:
            # No private argv, canonical request or DB exception enters task logs.
            raise ValueError("Owned live process qualification failed") from None
    with (root / f"{case}-effects").open("a") as stream:
        stream.write("x")
    if case == "success":
        deadline = time.monotonic() + 90
        while not (root / "success-release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Jobs qualification release was not observed")
            time.sleep(0.1)
    return {"value": 42, "effect": "once"}
