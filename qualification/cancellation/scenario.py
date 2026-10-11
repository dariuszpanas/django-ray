"""Install one candidate and supervise its bounded PostgreSQL/Jobs experiment."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from qualification.cancellation.contract import (
    CHILD_SECONDS,
    MAX_RECEIPT_BYTES,
    OUTER_SECONDS,
    validate_public_receipt,
    validate_receipt,
)
from qualification.docker import scenario as wheel
from qualification.upgrade.scenario import _postgres

ROOT = Path(__file__).resolve().parents[2]


def execute(
    *,
    source_root=ROOT,
    evidence_root=Path("/evidence"),
    target=Path("/tmp/cancellation/target"),
    wheel_directory=Path("/opt/django-ray-wheels"),
):
    started = time.monotonic()
    candidate = receipt = fixture_root = None
    failure = None
    cleanup_failed = False
    server_stopped = False
    wheel._ensure_evidence_root(evidence_root)
    try:
        wheel._require_non_root()
        wheel._require_linux_process_groups()
        selected = wheel._select_wheel(wheel_directory)
        wheel._prepare_install_target(target)
        wheel._install_wheel(selected, target, Path("/usr/local/bin/uv"))
        candidate = wheel._inspect_candidate(selected, target, source_root)
        with tempfile.TemporaryDirectory(prefix="jobs-", dir=target.parent) as directory:
            fixture_root = Path(directory)
            receipt_path = fixture_root / "receipt.json"
            environment = wheel._subprocess_environment(
                install_target=target, source_root=source_root
            )
            environment.update(
                DJANGO_SETTINGS_MODULE="qualification.cancellation.settings",
                DJANGO_RAY_CANCELLATION_CONFIG=json.dumps({"root": str(fixture_root)}),
                PATH=str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", ""),
            )
            try:
                with _postgres(fixture_root):
                    try:
                        result = wheel._run_bounded_command(
                            (
                                sys.executable,
                                "-P",
                                "-m",
                                "qualification.cancellation.probe",
                                str((target / "django_ray/__init__.py").resolve()),
                                str(receipt_path),
                            ),
                            cwd=source_root,
                            env=environment,
                            timeout=CHILD_SECONDS,
                        )
                        # Ray and manager output is diagnostic input, never a public receipt.
                        observed = json.loads(
                            wheel._bounded_regular_bytes(receipt_path, maximum=MAX_RECEIPT_BYTES)
                        )
                        # Do not retain arbitrary input even when the child failed.
                        validate_public_receipt(observed)
                        receipt = observed
                        if result.returncode:
                            failure = "probe_failed"
                        else:
                            validate_receipt(receipt)
                    except Exception:
                        failure = "probe_failed"
                server_stopped = True
            except Exception:
                if failure is None:
                    failure = "postgres_failed"
                else:
                    cleanup_failed = True
        if (
            wheel._package_tree_digest(target / "django_ray")
            != candidate.installed_package_tree_sha256
        ):
            failure = failure or "installed_package_changed"
    except Exception:
        failure = failure or "fixture_failed"
    fixture_removed = fixture_root is not None and not fixture_root.exists()
    if not fixture_removed or not server_stopped:
        cleanup_failed = True
        failure = failure or "cleanup_failed"
    if time.monotonic() - started >= OUTER_SECONDS:
        failure = failure or "outer_budget_exceeded"
    manifest = {
        "schema": "django-ray.cancellation-qualification",
        "schema_version": 1,
        "scope": "owned-ray-job-driver",
        "outcome": "passed" if failure is None else "failed",
        "failure": failure,
        "cleanup_failed": cleanup_failed,
        "server_stopped": server_stopped,
        "fixture_removed": fixture_removed,
        "candidate": None if candidate is None else candidate.as_manifest(),
        "observations": receipt,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "child_budget_seconds": CHILD_SECONDS,
        "outer_budget_seconds": OUTER_SECONDS,
    }
    wheel._write_manifest(
        evidence_root / "execution-manifest.json",
        json.dumps(manifest, sort_keys=True).encode() + b"\n",
    )
    print(
        json.dumps(
            {
                "qualification": "cancellation",
                "outcome": manifest["outcome"],
                "failure": failure,
                "cleanup_failed": cleanup_failed,
            }
        ),
        flush=True,
    )
    return 0 if failure is None else 1


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("Cancellation qualification does not accept arguments")
    raise SystemExit(execute())
