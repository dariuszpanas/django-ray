"""Qualify installed-wheel historical reads in three isolated Django processes."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from qualification.docker import scenario as wheel
from qualification.results.contract import (
    CASES,
    DEFINITION_PATH,
    MAX_PROBE_BYTES,
    WORKLOAD,
    junit,
    validate_probe,
)

ROOT = Path(__file__).resolve().parents[2]
TARGET = Path("/tmp/django-ray-result-qualification/target")
PROBE_TIMEOUT_SECONDS = 45


def execute(
    *,
    source_root: Path = ROOT,
    evidence_root: Path = Path("/evidence"),
    target: Path = TARGET,
    wheel_directory: Path = Path("/opt/django-ray-wheels"),
    uv_executable: Path = Path("/usr/local/bin/uv"),
) -> int:
    """Install once, verify each child import, and remove only owned fixtures."""
    started = time.monotonic()
    candidate = None
    dependencies = None
    target_metadata = None
    receipts: list[dict] = []
    failure = None
    fixture_root = None
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
        environment = wheel._subprocess_environment(install_target=target, source_root=source_root)
        with tempfile.TemporaryDirectory(prefix="result-reads-", dir=target.parent) as directory:
            fixture_root = Path(directory)
            for kind in CASES:
                fixture = fixture_root / kind
                fixture.mkdir()
                print(f"qualification={WORKLOAD} phase=case-started case={kind}", flush=True)
                result = wheel._run_bounded_command(
                    (
                        sys.executable,
                        "-P",
                        "-m",
                        "qualification.results.probe",
                        str(fixture),
                        kind,
                        expected_module,
                    ),
                    cwd=source_root,
                    env=environment,
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
                if result.returncode != 0:
                    for output in (result.stdout, result.stderr):
                        print(output.decode("utf-8", errors="replace"), flush=True)
                    raise wheel.QualificationError("result-read-probe-failed")
                if len(result.stdout) > MAX_PROBE_BYTES:
                    raise wheel.QualificationError("result-read-receipt-too-large")
                receipt = validate_probe(
                    json.loads(result.stdout),
                    kind=kind,
                    expected_module=expected_module,
                )
                receipts.append(receipt)
                print(f"qualification={WORKLOAD} phase=case-passed case={kind}", flush=True)
        if (
            wheel._package_tree_digest(target / "django_ray")
            != candidate.installed_package_tree_sha256
        ):
            raise wheel.QualificationError("installed-package-changed-during-probes")
    except Exception as error:
        failure = (
            error.code if isinstance(error, wheel.QualificationError) else type(error).__name__
        )
        if isinstance(error, wheel.BoundedProcessError):
            wheel._emit_process_failure("result-read-probe", error)
        print(f"qualification={WORKLOAD} phase=failed code={failure}", flush=True)
    fixture_cleanup = fixture_root is not None and not fixture_root.exists()
    if failure is None and not fixture_cleanup:
        failure = "result-read-fixtures-not-cleaned"
    manifest = {
        "schema": "django-ray.result-read-qualification",
        "schema_version": 1,
        "workload": WORKLOAD,
        "definition_path": DEFINITION_PATH,
        "candidate": None if candidate is None else candidate.as_manifest(),
        "dependencies": dependencies,
        "target": target_metadata,
        "outcome": "passed" if failure is None else "failed",
        "failure": failure,
        "elapsed_seconds": time.monotonic() - started,
        "cases": receipts,
        "fixture_cleanup": fixture_cleanup,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 512 * 1024:
        raise wheel.QualificationError("result-read-manifest-too-large")
    wheel._write_manifest(
        evidence_root / "junit.xml",
        junit(receipts, expected_module=expected_module, failure=failure),
    )
    wheel._write_manifest(evidence_root / "execution-manifest.json", encoded + b"\n")
    print(f"qualification={WORKLOAD} phase=finished outcome={manifest['outcome']}", flush=True)
    return 0 if failure is None else 1


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("result-read qualification does not accept arguments")
    raise SystemExit(execute())
