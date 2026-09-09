"""Qualify the installed wheel's fixed real-Ray fanout workload on Linux."""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from xml.etree import ElementTree

from qualification.docker import scenario as wheel
from qualification.runtime.contract import (
    DEFINITION_PATH,
    MAX_RECEIPT_BYTES,
    PYTEST_ARGUMENTS,
    WORKLOAD,
    success_junit,
    validate_receipt,
)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path("/evidence")
TARGET = Path("/tmp/django-ray-runtime-qualification/target")
RECEIPT = TARGET.parent / "pytest-receipt.json"
PYTEST_TIMEOUT_SECONDS = 300


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise wheel.QualificationError("nonfinite-runtime-receipt")
    return number


def _failure_junit(code: str) -> bytes:
    suite = ElementTree.Element(
        "testsuite", name=WORKLOAD, tests="1", failures="1", errors="0", skipped="0"
    )
    case = ElementTree.SubElement(
        suite, "testcase", classname="qualification.runtime", name="contract"
    )
    ElementTree.SubElement(case, "failure", message=code)
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"


def execute(
    *,
    source_root: Path = ROOT,
    evidence_root: Path = EVIDENCE,
    target: Path = TARGET,
    receipt_path: Path = RECEIPT,
    wheel_directory: Path = Path("/opt/django-ray-wheels"),
    uv_executable: Path = Path("/usr/local/bin/uv"),
) -> int:
    """Retain bounded candidate, phase and timing evidence on success or failure."""
    started = time.monotonic()
    candidate = None
    receipt = None
    dependencies = None
    target_metadata = None
    failure = None
    # Never overwrite another attempt's evidence, including on a preflight failure.
    wheel._ensure_evidence_root(evidence_root)
    print(f"qualification={WORKLOAD} phase=started", flush=True)
    try:
        wheel._require_non_root()
        wheel._require_linux_process_groups()
        if receipt_path.exists() or receipt_path.is_symlink():
            raise wheel.QualificationError("runtime-receipt-already-exists")
        selected_wheel = wheel._select_wheel(wheel_directory)
        wheel._prepare_install_target(target)
        wheel._install_wheel(selected_wheel, target, uv_executable)
        candidate = wheel._inspect_candidate(selected_wheel, target, source_root)
        dependencies = wheel._dependency_manifest(candidate)
        target_metadata = wheel._target_manifest(candidate, dependencies)
        print(f"qualification={WORKLOAD} phase=running-tests", flush=True)
        result = wheel._run_bounded_command(
            (sys.executable, "-P", "-m", "qualification.runtime.driver"),
            cwd=source_root,
            env=wheel._subprocess_environment(install_target=target, source_root=source_root),
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
        for output in (result.stdout, result.stderr):
            if output:
                print(output.decode("utf-8", errors="replace"), flush=True)
        payload = wheel._bounded_regular_bytes(receipt_path, maximum=MAX_RECEIPT_BYTES)
        receipt = json.loads(payload, parse_float=_finite_number, parse_constant=_finite_number)
        if result.returncode != 0:
            raise wheel.QualificationError("runtime-pytest-failed")
        validate_receipt(receipt)
        if (
            wheel._package_tree_digest(target / "django_ray")
            != candidate.installed_package_tree_sha256
        ):
            raise wheel.QualificationError("installed-package-changed-during-tests")
    except Exception as error:
        failure = (
            error.code if isinstance(error, wheel.QualificationError) else type(error).__name__
        )
        if isinstance(error, wheel.BoundedProcessError):
            wheel._emit_process_failure("runtime-pytest", error)
        print(f"qualification={WORKLOAD} phase=failed code={failure}", flush=True)
    manifest = {
        "schema": "django-ray.runtime-qualification",
        "schema_version": 1,
        "workload": WORKLOAD,
        "definition_path": DEFINITION_PATH,
        "selection": list(PYTEST_ARGUMENTS),
        "candidate": None if candidate is None else candidate.as_manifest(),
        "dependencies": dependencies,
        "target": target_metadata,
        "outcome": "passed" if failure is None else "failed",
        "failure": failure,
        "elapsed_seconds": time.monotonic() - started,
        "tests": receipt,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 512 * 1024:
        raise wheel.QualificationError("runtime-manifest-too-large")
    junit = success_junit(receipt) if failure is None else _failure_junit(failure)
    wheel._write_manifest(evidence_root / "junit.xml", junit)
    wheel._write_manifest(evidence_root / "execution-manifest.json", encoded + b"\n")
    print(f"qualification={WORKLOAD} phase=finished outcome={manifest['outcome']}", flush=True)
    return 0 if failure is None else 1


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("runtime qualification does not accept arguments")
    raise SystemExit(execute())
