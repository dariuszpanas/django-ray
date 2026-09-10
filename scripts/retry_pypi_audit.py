"""Run the pinned scanner with one retry for PyPI advisory read timeouts."""

from __future__ import annotations

import importlib.metadata
import sys
import time
import traceback

import requests

from scripts.audit_runtime_dependencies import PIP_AUDIT_VERSION


def main() -> None:
    """Preserve CLI failures; retry only a timeout from the actual PyPI query."""
    installed = importlib.metadata.version("pip-audit")
    if installed != PIP_AUDIT_VERSION:
        raise RuntimeError(f"expected pip-audit=={PIP_AUDIT_VERSION}, found {installed}")

    # These private entry points are deliberately coupled to the checked pin.
    # Re-entering the CLI recreates its source, service and result accumulator.
    from pip_audit._cli import audit
    from pip_audit._service.pypi import PyPIService

    for attempt in range(2):
        try:
            audit()
            return
        except requests.ReadTimeout as error:
            frames = traceback.walk_tb(error.__traceback__)
            from_pypi_query = any(frame.f_code is PyPIService.query.__code__ for frame, _ in frames)
            if attempt or not from_pypi_query:
                raise
            traceback.print_exception(error)
            print(
                "PyPI advisory read timed out; retrying the complete audit once in 2 seconds.",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(2)


if __name__ == "__main__":
    main()
