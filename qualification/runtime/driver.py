"""Run the fixed pytest selection and retain each observed execution phase."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from qualification.docker.scenario import QualificationError
from qualification.runtime.contract import (
    MAX_RECEIPT_BYTES,
    MAX_TESTS,
    PHASES,
    PYTEST_ARGUMENTS,
    validate_receipt,
    validate_selection,
)

ROOT = Path(__file__).resolve().parents[2]
TARGET = Path("/tmp/django-ray-runtime-qualification/target")
RECEIPT = Path("/tmp/django-ray-runtime-qualification/pytest-receipt.json")


class Observations:
    """Record bounded pytest events; omitted or repeated events never imply success."""

    def __init__(self) -> None:
        self.selected: list[str] = []
        self.reports: dict[str, dict[str, dict]] = {}
        self.errors: list[str] = []

    def reject(self, code: str) -> None:
        if len(self.errors) < 16:
            self.errors.append(code)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        try:
            self.selected = validate_selection(sorted(item.nodeid for item in session.items))
        except QualificationError:
            raise pytest.UsageError("runtime qualification selection failed") from None
        if any(item.get_closest_marker("real_ray") is None for item in session.items):
            raise pytest.UsageError("runtime qualification requires real-Ray assertions")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid not in self.selected or report.when not in PHASES:
            self.reject("unexpected-test-report")
            return
        if len(self.reports) >= MAX_TESTS and report.nodeid not in self.reports:
            self.reject("report-limit")
            return
        phases = self.reports.setdefault(report.nodeid, {})
        if report.when in phases:
            self.reject("duplicate-phase")
            return
        phases[report.when] = {
            "outcome": "xfail" if hasattr(report, "wasxfail") else report.outcome,
            "seconds": report.duration,
        }

    def receipt(self, exit_code: int, *, ray_shutdown: bool) -> dict:
        return {
            "schema_version": 1,
            "selected": self.selected,
            "reports": self.reports,
            "errors": self.errors,
            "exit_code": exit_code,
            "ray_shutdown": ray_shutdown,
        }


def run(*, root: Path, target: Path, receipt_path: Path) -> int:
    """Only the installed candidate may supply package imports to this interpreter."""
    import ray

    import django_ray

    if Path(str(django_ray.__file__)).resolve() != (target / "django_ray/__init__.py").resolve():
        raise RuntimeError("runtime-driver-import-outside-installed-target")
    if Path.cwd().resolve() != root.resolve() or ray.is_initialized():
        raise RuntimeError("runtime-driver-not-isolated")
    observer = Observations()
    exit_code = int(
        pytest.main(
            [
                *PYTEST_ARGUMENTS,
                "-o",
                "addopts=",
                "-q",
                "--tb=short",
                "--color=no",
                "--maxfail=1",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:xdist",
            ],
            plugins=[observer],
        )
    )
    receipt = observer.receipt(exit_code, ray_shutdown=not ray.is_initialized())
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise RuntimeError("runtime-receipt-too-large")
    with receipt_path.open("xb") as stream:
        stream.write(encoded)
    validate_receipt(receipt)
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("runtime driver does not accept arguments")
    raise SystemExit(run(root=ROOT, target=TARGET, receipt_path=RECEIPT))
