"""Install the candidate offline and prove its PostgreSQL receipt contract."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree

from qualification.docker import scenario as wheel
from qualification.transactions.contract import (
    CASES,
    DEFINITION_PATH,
    MAX_PROBE_BYTES,
    WORKLOAD,
    validate_probe,
)

ROOT = Path(__file__).resolve().parents[2]
TARGET = Path("/tmp/django-ray-transaction-qualification/target")
PROBE_TIMEOUT_SECONDS = 180


def junit(receipt, *, expected_module, failure):
    if failure is None:
        validate_probe(receipt, expected_module=expected_module)
    names = ("contract",) if failure else CASES
    suite = ElementTree.Element(
        "testsuite",
        name=WORKLOAD,
        tests=str(len(names)),
        failures="1" if failure else "0",
        errors="0",
        skipped="0",
    )
    for name in names:
        case = ElementTree.SubElement(suite, "testcase", classname=WORKLOAD, name=name)
        if failure:
            ElementTree.SubElement(case, "failure", message=failure)
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"


def execute(
    *,
    source_root=ROOT,
    evidence_root=Path("/evidence"),
    target=TARGET,
    wheel_directory=Path("/opt/django-ray-wheels"),
    uv_executable=Path("/usr/local/bin/uv"),
) -> int:
    started = time.monotonic()
    candidate = dependencies = target_metadata = receipt = fixture_root = None
    execution_protocol_version = None
    failure = None
    expected_module = str((target / "django_ray/__init__.py").resolve())
    wheel._ensure_evidence_root(evidence_root)
    print(f"qualification={WORKLOAD} phase=started", flush=True)
    try:
        wheel._require_non_root()
        wheel._require_linux_process_groups()
        selected_wheel = wheel._select_wheel(wheel_directory)
        wheel._prepare_install_target(target)
        wheel._install_wheel(selected_wheel, target, uv_executable)
        candidate = wheel._inspect_candidate(selected_wheel, target, source_root)
        dependencies = wheel._dependency_manifest(candidate)
        target_metadata = wheel._target_manifest(candidate, dependencies)
        with tempfile.TemporaryDirectory(prefix="tx-", dir=target.parent) as directory:
            fixture_root = Path(directory)
            fixture = fixture_root / "fixture"
            fixture.mkdir()
            receipt_path = fixture_root / "receipt.json"
            result = wheel._run_bounded_command(
                (
                    sys.executable,
                    "-P",
                    "-m",
                    "qualification.transactions.probe",
                    str(fixture),
                    expected_module,
                    str(receipt_path),
                ),
                cwd=source_root,
                env=wheel._subprocess_environment(install_target=target, source_root=source_root),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            for output in (result.stdout, result.stderr):
                if output:
                    print(output.decode("utf-8", errors="replace"), flush=True)
            if result.returncode != 0:
                raise wheel.QualificationError("transaction-probe-failed")
            receipt = json.loads(
                wheel._bounded_regular_bytes(receipt_path, maximum=MAX_PROBE_BYTES)
            )
            validate_probe(receipt, expected_module=expected_module)
            execution_protocol_version = receipt["execution_protocol_version"]
        if (
            wheel._package_tree_digest(target / "django_ray")
            != candidate.installed_package_tree_sha256
        ):
            raise wheel.QualificationError("installed-package-changed-during-probe")
    except Exception as error:
        failure = (
            error.code if isinstance(error, wheel.QualificationError) else type(error).__name__
        )
        if isinstance(error, wheel.BoundedProcessError):
            wheel._emit_process_failure("transaction-probe", error)
        print(f"qualification={WORKLOAD} phase=failed code={failure}", flush=True)
    fixture_cleanup = fixture_root is not None and not fixture_root.exists()
    if failure is None and not fixture_cleanup:
        failure = "transaction-fixtures-not-cleaned"
    manifest = {
        "schema": "django-ray.transaction-qualification",
        "schema_version": 1,
        "workload": WORKLOAD,
        "definition_path": DEFINITION_PATH,
        "candidate": None if candidate is None else candidate.as_manifest(),
        "dependencies": dependencies,
        "target": target_metadata,
        "outcome": "passed" if failure is None else "failed",
        "failure": failure,
        "elapsed_seconds": time.monotonic() - started,
        "execution_protocol_version": execution_protocol_version,
        "observations": receipt,
        "fixture_cleanup": fixture_cleanup,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 512 * 1024:
        raise wheel.QualificationError("transaction-manifest-too-large")
    wheel._write_manifest(
        evidence_root / "junit.xml",
        junit(
            receipt,
            expected_module=expected_module,
            failure=failure,
        ),
    )
    wheel._write_manifest(evidence_root / "execution-manifest.json", encoded + b"\n")
    print(f"qualification={WORKLOAD} phase=finished outcome={manifest['outcome']}", flush=True)
    return 0 if failure is None else 1


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("transaction qualification does not accept arguments")
    raise SystemExit(execute())
