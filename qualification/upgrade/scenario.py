"""Rehearse database preservation using two wheels and independent restored databases."""

from __future__ import annotations

import contextlib
import importlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from qualification.docker import scenario as wheel
from qualification.transactions.probe import POSTGRES_BIN, stop_server
from qualification.upgrade.contract import (
    BACKENDS,
    BASELINE_COMMIT,
    BASELINE_VERSION,
    CANDIDATE_VERSION,
    MAX_RECEIPT_BYTES,
    MISSING,
    PHASES,
    junit,
    validate_backend,
)
from qualification.upgrade.prepare import verify_archive

ROOT = Path(__file__).resolve().parents[2]
RELEASED = Path("/opt/released-source")
RELEASED_PYTHON = Path("/opt/released-venv/bin/python")
RELEASED_WHEELS = Path("/opt/released-wheels")
UV = Path("/usr/local/bin/uv")


def _run(command, *, cwd, environment=None, timeout=60):
    result = wheel._run_bounded_command(
        tuple(map(str, command)),
        cwd=cwd,
        env=wheel._subprocess_environment() if environment is None else environment,
        timeout=timeout,
    )
    if result.returncode:
        wheel._emit_failure_output("upgrade-stdout", result.stdout)
        wheel._emit_failure_output("upgrade-stderr", result.stderr)
        raise wheel.QualificationError("upgrade-child-failed")
    return result


def _identity(python, selected_wheel, target, source):
    # Verification runs without the install target on sys.path; the shared helper
    # rejects any preinstalled django-ray and inserts only the verified target.
    environment = wheel._subprocess_environment()
    environment["PYTHONPATH"] = str(ROOT)
    program = (
        "import json,platform,sys; from pathlib import Path; from importlib import metadata; "
        "from qualification.docker import scenario as w; "
        "c=w._inspect_candidate(*map(Path,sys.argv[1:])); "
        "d={'python':platform.python_version(),'django':metadata.version('django'),"
        "'ray':metadata.version('ray'),'installed_distributions':w._installed_distributions()}; "
        "print(json.dumps({'package':c.as_manifest(),'dependencies':d}))"
    )
    result = _run(
        [python, "-P", "-c", program, selected_wheel, target, source],
        cwd=ROOT,
        environment=environment,
    )
    return json.loads(result.stdout)


@contextlib.contextmanager
def _postgres(root):
    """Same private socket, process ownership and resource limits as receipt qualification."""
    psycopg = importlib.import_module("psycopg")

    data, socket = root / "postgres", root / "socket"
    socket.mkdir(mode=0o700)
    _run(
        [
            POSTGRES_BIN / "initdb",
            "-D",
            data,
            "-U",
            "qualification",
            "--auth-local=trust",
            "--auth-host=reject",
            "--no-sync",
            "--no-locale",
            "-E",
            "UTF8",
        ],
        cwd=root,
    )
    server = subprocess.Popen(
        tuple(
            map(
                str,
                [
                    POSTGRES_BIN / "postgres",
                    "-D",
                    data,
                    "-k",
                    socket,
                    "-h",
                    "",
                    "-c",
                    "max_connections=10",
                    "-c",
                    "shared_buffers=16MB",
                    "-c",
                    "work_mem=1MB",
                    "-c",
                    "temp_file_limit=32768",
                    "-c",
                    "max_wal_size=64MB",
                    "-c",
                    "min_wal_size=32MB",
                ],
            )
        ),
        env=wheel._subprocess_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            if server.poll() is not None:
                raise wheel.QualificationError("upgrade-postgres-startup-failed")
            try:
                with psycopg.connect(
                    host=str(socket),
                    user="qualification",
                    dbname="postgres",
                    connect_timeout=1,
                    autocommit=True,
                ) as connection:
                    assert connection.info.server_version // 10000 == 17
                    assert connection.execute("SHOW listen_addresses").fetchone()[0] == ""
                    for name in ("baseline", "restored", "rollback"):
                        connection.execute(
                            psycopg.sql.SQL("CREATE DATABASE {}").format(
                                psycopg.sql.Identifier(name),
                            )
                        )
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise wheel.QualificationError("upgrade-postgres-readiness-timeout") from None
                time.sleep(0.1)
        yield
    finally:
        stop_server(server)


def _backup(root, backend):
    backup = root / "backup"
    if backend == "sqlite":
        with sqlite3.connect(root / "baseline.sqlite3") as source:
            with sqlite3.connect(backup) as destination:
                source.backup(destination)
    else:
        _run(
            [
                POSTGRES_BIN / "pg_dump",
                "-h",
                root / "socket",
                "-U",
                "qualification",
                "-d",
                "baseline",
                "--format=custom",
                "--file",
                backup,
            ],
            cwd=root,
        )
    if not backup.is_file() or backup.stat().st_size > 64 * 1024 * 1024:
        raise wheel.QualificationError("invalid-upgrade-backup-size")
    return wheel._sha256(backup)


def _restore(root, backend, database):
    if database not in ("restored", "rollback"):
        raise wheel.QualificationError("invalid-upgrade-restore-destination")
    if backend == "sqlite":
        with sqlite3.connect(root / "backup") as source:
            with sqlite3.connect(root / f"{database}.sqlite3") as destination:
                source.backup(destination)
                assert destination.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    else:
        _run(
            [
                POSTGRES_BIN / "pg_restore",
                "-h",
                root / "socket",
                "-U",
                "qualification",
                "-d",
                database,
                "--exit-on-error",
                "--no-owner",
                root / "backup",
            ],
            cwd=root,
        )


def _phase(root, backend, phase, target, *, released, database, artifacts):
    python = RELEASED_PYTHON if released else Path(sys.executable)
    expected_module = str((target / "django_ray/__init__.py").resolve())
    print(f"qualification=beta-data-upgrade backend={backend} phase={phase}", flush=True)
    result = _run(
        [
            python,
            "-P",
            "-m",
            "qualification.upgrade.step",
            root,
            backend,
            database,
            phase,
            expected_module,
            artifacts,
        ],
        cwd=ROOT,
        environment=wheel._subprocess_environment(install_target=target, source_root=ROOT),
    )
    if len(result.stdout) > MAX_RECEIPT_BYTES:
        raise wheel.QualificationError("upgrade-phase-output-too-large")
    value = json.loads(result.stdout)
    if value.get("phase") != phase or value.get("module") != expected_module:
        raise wheel.QualificationError("upgrade-phase-source-mismatch")
    return value


def _backend(parent, backend, released_target, candidate_target):
    phases = []
    with tempfile.TemporaryDirectory(prefix=f"{backend}-", dir=parent) as directory:
        root = Path(directory)
        (root / "owned-fixture").touch()
        artifacts = root / "artifacts"
        artifacts.mkdir()
        manager = _postgres(root) if backend == "postgresql" else contextlib.nullcontext()
        with manager:
            for phase in PHASES[:3]:
                phases.append(
                    _phase(
                        root,
                        backend,
                        phase,
                        released_target,
                        released=True,
                        database="baseline",
                        artifacts=artifacts,
                    )
                )
            backup_digest = _backup(root, backend)
            artifacts_digest = wheel._package_tree_digest(artifacts)
            shutil.copytree(artifacts, root / "artifact-backup")
            _restore(root, backend, "restored")
            shutil.copytree(root / "artifact-backup", root / "restored-artifacts")
            restored = root / "restored-artifacts"
            assert wheel._package_tree_digest(restored) == artifacts_digest
            phases.append(
                _phase(
                    root,
                    backend,
                    "restored-baseline-read",
                    released_target,
                    released=True,
                    database="restored",
                    artifacts=restored,
                )
            )
            phases.append(
                _phase(
                    root,
                    backend,
                    "candidate-migrate-read",
                    candidate_target,
                    released=False,
                    database="restored",
                    artifacts=restored,
                )
            )
            assert wheel._package_tree_digest(restored) == artifacts_digest
            phases.append(
                _phase(
                    root,
                    backend,
                    "candidate-new-write",
                    candidate_target,
                    released=False,
                    database="restored",
                    artifacts=restored,
                )
            )
            # An independent second restore proves what reverting to this backup
            # would lose; it never replaces the database receiving candidate writes.
            _restore(root, backend, "rollback")
            shutil.copytree(root / "artifact-backup", root / "rollback-artifacts")
            phases.append(
                _phase(
                    root,
                    backend,
                    "backup-rollback-read",
                    released_target,
                    released=True,
                    database="rollback",
                    artifacts=root / "rollback-artifacts",
                )
            )
            assert wheel._sha256(root / "backup") == backup_digest
            assert wheel._package_tree_digest(root / "artifact-backup") == artifacts_digest
    receipt = {
        "backend": backend,
        "phases": phases,
        "backup_sha256": backup_digest,
        "artifacts_sha256": artifacts_digest,
        "fixture_cleanup": not root.exists(),
        "server_stopped": True,
        "complete_upgrade_gate": False,
        "missing_acceptance": list(MISSING),
    }
    return validate_backend(receipt, backend=backend)


def execute():
    started = time.monotonic()
    evidence = Path("/evidence")
    wheel._ensure_evidence_root(evidence)
    identities, receipts, failure = {}, [], None
    fixture = None
    baseline_archive_digest = candidate_source_digest = None
    try:
        wheel._require_non_root()
        wheel._require_linux_process_groups()
        verify_archive(Path("/opt/released-source.tar"))
        baseline_archive_digest = wheel._sha256(Path("/opt/released-source.tar"))
        candidate_source_digest = wheel._package_tree_digest(ROOT)
        with tempfile.TemporaryDirectory(prefix="django-ray-upgrade-") as directory:
            fixture = Path(directory)
            targets = {}
            for name, source, wheels, python in (
                ("released", RELEASED, RELEASED_WHEELS, RELEASED_PYTHON),
                ("candidate", ROOT, Path("/opt/django-ray-wheels"), Path(sys.executable)),
            ):
                selected_wheel = wheel._select_wheel(wheels)
                target = fixture / name
                wheel._prepare_install_target(target)
                wheel._install_wheel(selected_wheel, target, UV)
                identities[name] = _identity(python, selected_wheel, target, source)
                targets[name] = target
            if identities["released"]["package"]["version"] != BASELINE_VERSION:
                raise wheel.QualificationError("wrong-upgrade-baseline-version")
            if identities["candidate"]["package"]["version"] != CANDIDATE_VERSION:
                raise wheel.QualificationError("wrong-upgrade-candidate-version")
            for backend in BACKENDS:
                receipts.append(
                    _backend(fixture, backend, targets["released"], targets["candidate"])
                )
            for name, target in targets.items():
                if (
                    wheel._package_tree_digest(target / "django_ray")
                    != identities[name]["package"]["installed_package_tree_sha256"]
                ):
                    raise wheel.QualificationError("upgrade-package-changed")
    except Exception as error:
        failure = (
            error.code if isinstance(error, wheel.QualificationError) else type(error).__name__
        )
        if isinstance(error, wheel.BoundedProcessError):
            wheel._emit_process_failure("upgrade", error)
        print(f"qualification=beta-data-upgrade phase=failed code={failure}", flush=True)
    cleanup = fixture is not None and not fixture.exists()
    if failure is None and not cleanup:
        failure = "upgrade-fixture-cleanup-failed"
    manifest = {
        "schema": "django-ray.coordinated-beta-data-upgrade",
        "schema_version": 1,
        "outcome": "passed" if failure is None else "failed",
        "failure": failure,
        "baseline_commit": BASELINE_COMMIT,
        "baseline_archive_sha256": baseline_archive_digest,
        "candidate_source_files_sha256": candidate_source_digest,
        "identities": identities,
        "backends": receipts,
        "fixture_cleanup": cleanup,
        "complete_upgrade_gate": False,
        "missing_acceptance": list(MISSING),
        "elapsed_seconds": time.monotonic() - started,
    }
    encoded = json.dumps(manifest, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise wheel.QualificationError("upgrade-manifest-too-large")
    wheel._write_manifest(evidence / "execution-manifest.json", encoded + b"\n")
    wheel._write_manifest(evidence / "junit.xml", junit(receipts, failure=failure))
    print(
        f"qualification=beta-data-upgrade phase=finished outcome={manifest['outcome']}", flush=True
    )
    return 0 if failure is None else 1


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("upgrade qualification accepts no arguments")
    raise SystemExit(execute())
