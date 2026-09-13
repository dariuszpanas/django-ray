"""Sequential real old/current Core execution using existing backup helpers."""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from qualification.docker import scenario as wheel
from qualification.upgrade import scenario as data
from qualification.upgrade.contract import BACKENDS, BASELINE_COMMIT, MISSING
from qualification.upgrade.prepare import verify_archive


def phase(root, backend, name, target, *, database, artifacts):
    released = name.startswith("baseline")
    python = data.RELEASED_PYTHON if released else Path(sys.executable)
    db = (
        {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(root / f"{database}.sqlite3"),
            "OPTIONS": {"timeout": 20},
        }
        if backend == "sqlite"
        else {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": database,
            "USER": "qualification",
            "HOST": str(root / "socket"),
            "PORT": "5432",
            "OPTIONS": {"connect_timeout": 5, "options": "-c statement_timeout=10000"},
        }
    )
    env = wheel._subprocess_environment(install_target=target, source_root=data.ROOT)
    env.update(
        DJANGO_SETTINGS_MODULE="qualification.upgrade.native_settings",
        DJANGO_RAY_UPGRADE_ROOT=str(root),
        DJANGO_RAY_UPGRADE_CONFIG=json.dumps({"database": db, "artifacts": str(artifacts)}),
        RAY_USAGE_STATS_ENABLED="0",
    )
    receipt = root / f"{name}.json"
    module = str((target / "django_ray/__init__.py").resolve())
    print(f"qualification=beta-native-upgrade backend={backend} phase={name}", flush=True)
    data._run(
        [python, "-P", "-m", "qualification.upgrade.native_probe", name, module, receipt],
        cwd=data.ROOT,
        environment=env,
        timeout=240,
    )
    value = json.loads(wheel._bounded_regular_bytes(receipt, maximum=65536))
    assert value["phase"] == name and value["module"] == module
    assert value["version"] == ("0.4.0" if released else "0.5.0")
    return value


def backend(parent, name, targets):
    with tempfile.TemporaryDirectory(prefix=f"native-{name}-", dir=parent) as directory:
        root = Path(directory)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        with data._postgres(root) if name == "postgresql" else contextlib.nullcontext():
            phases = [
                phase(
                    root,
                    name,
                    "baseline-run",
                    targets["released"],
                    database="baseline",
                    artifacts=artifacts,
                )
            ]
            backup_digest = data._backup(root, name)
            artifact_digest = wheel._package_tree_digest(artifacts)
            shutil.copytree(artifacts, root / "artifact-backup")
            data._restore(root, name, "restored")
            restored = root / "restored-artifacts"
            shutil.copytree(root / "artifact-backup", restored)
            for step, version in (
                ("baseline-read", "released"),
                ("candidate-read", "candidate"),
                ("candidate-run", "candidate"),
            ):
                phases.append(
                    phase(
                        root, name, step, targets[version], database="restored", artifacts=restored
                    )
                )
            assert wheel._sha256(root / "backup") == backup_digest
            assert wheel._package_tree_digest(root / "artifact-backup") == artifact_digest
    return {
        "backend": name,
        "phases": phases,
        "fixture_cleanup": not root.exists(),
        "backup_sha256": backup_digest,
        "artifacts_sha256": artifact_digest,
    }


def execute():
    evidence = Path("/evidence")
    wheel._ensure_evidence_root(evidence)
    wheel._require_non_root()
    wheel._require_linux_process_groups()
    identities, results, failure = {}, [], None
    fixture = None
    try:
        verify_archive(Path("/opt/released-source.tar"))
        with tempfile.TemporaryDirectory(prefix="native-upgrade-") as directory:
            fixture = Path(directory)
            targets = {}
            for name, source, wheels, python in (
                ("released", data.RELEASED, data.RELEASED_WHEELS, data.RELEASED_PYTHON),
                ("candidate", data.ROOT, Path("/opt/django-ray-wheels"), Path(sys.executable)),
            ):
                selected = wheel._select_wheel(wheels)
                target = fixture / name
                wheel._prepare_install_target(target)
                wheel._install_wheel(selected, target, data.UV)
                identities[name] = data._identity(python, selected, target, source)
                targets[name] = target
            for name in BACKENDS:
                results.append(backend(fixture, name, targets))
            for name, target in targets.items():
                assert (
                    wheel._package_tree_digest(target / "django_ray")
                    == identities[name]["package"]["installed_package_tree_sha256"]
                )
    except Exception as error:
        failure = type(error).__name__
        if isinstance(error, wheel.BoundedProcessError):
            wheel._emit_process_failure("native-upgrade", error)
        print(f"qualification=beta-native-upgrade failure={failure}", flush=True)
    manifest = {
        "schema": "django-ray.coordinated-beta-native-upgrade",
        "schema_version": 1,
        "outcome": "passed" if failure is None else "failed",
        "failure": failure,
        "baseline_commit": BASELINE_COMMIT,
        "baseline_archive_sha256": wheel._sha256(Path("/opt/released-source.tar")),
        "candidate_source_files_sha256": wheel._package_tree_digest(data.ROOT),
        "identities": identities,
        "backends": results,
        "fixture_cleanup": fixture is not None and not fixture.exists(),
        "complete_upgrade_gate": False,
        "missing_acceptance": list(MISSING),
    }
    wheel._write_manifest(evidence / "execution-manifest.json", json.dumps(manifest).encode())
    print(f"qualification=beta-native-upgrade outcome={manifest['outcome']}", flush=True)
    return 0 if failure is None else 1


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("native upgrade qualification accepts no arguments")
    raise SystemExit(execute())
