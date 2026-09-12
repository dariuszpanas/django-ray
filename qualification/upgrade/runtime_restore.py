"""Restore an owned backup into an already created empty scratch database.

The host must independently own and reap the previous scratch observer before
calling this helper. This module cannot create/drop databases, terminate other
sessions, restore primary, or authenticate resource ownership. A failed attempt
remains reserved. Success still needs a fresh released-history observer.
"""

from __future__ import annotations

import hashlib
import platform
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from qualification.upgrade import runtime_artifacts as artifacts
from qualification.upgrade import runtime_database as database
from qualification.upgrade import runtime_steps as steps

RESTORE_TIMEOUT = 60
_EMPTY_SQL = (
    "SELECT "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_namespace "
    "WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema')), "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
    "ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'), "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n "
    "ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'), "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n "
    "ON n.oid=t.typnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'), "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_extension WHERE extname<>'plpgsql'), "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_event_trigger), "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_foreign_server), "
    "NOT EXISTS (SELECT FROM pg_catalog.pg_stat_activity "
    "WHERE datname=pg_catalog.current_database() AND pid<>pg_catalog.pg_backend_pid())"
)
_NO_OTHER_SESSIONS_SQL = (
    "SELECT NOT EXISTS (SELECT FROM pg_catalog.pg_stat_activity "
    "WHERE datname=pg_catalog.current_database() AND pid<>pg_catalog.pg_backend_pid())"
)


class DatabaseRestoreError(ValueError):
    """Fixed refusals without SQL diagnostics, connection strings, or payloads."""


def _require(condition: bool) -> None:
    if not condition:
        raise DatabaseRestoreError("invalid-upgrade-database-restore")


def _query(connection, directory: Path, environment: dict[str, str], sql: str) -> bytes:
    return database._run_client(
        (
            str(database.POSTGRES_BIN / "psql"),
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--field-separator=\t",
            "--command=" + sql,
            *database._connection_arguments(connection),
        ),
        directory=directory,
        environment=environment,
        timeout=database.IDENTITY_TIMEOUT,
        maximum=1024,
    )


def _backup_and_clone(
    point: str, run_digest: str, artifact_digest: str, dump_digest: str, connection, identity: dict
) -> tuple[Path, dict]:
    root = artifacts._bound_root(run_digest)
    backup_point = "final" if point == "rollback" else point
    backup = root / ".upgrade-backups" / backup_point
    artifacts._directory(backup)
    _require(
        {path.name for path in backup.iterdir()}
        == {"artifacts", "artifacts.json", "database.dump", "database.json"}
    )
    inventory = artifacts._inventory(backup / "artifacts")
    _require(hashlib.sha256(steps._json(inventory)).hexdigest() == artifact_digest)
    _require(
        steps._read(backup / "artifacts.json")
        == {
            "schema": 1,
            "run_digest": run_digest,
            "backup_point": backup_point,
            "inventory": inventory,
            "artifacts_sha256": artifact_digest,
        }
    )
    size, digest = database._dump_identity(backup / "database.dump")
    _require(digest == dump_digest)
    expected = {
        "schema": 1,
        "run_digest": run_digest,
        "backup_point": backup_point,
        "artifacts_sha256": artifact_digest,
        "postgresql": identity,
        "database": connection.database,
        "format": "custom",
        "dump_bytes": size,
        "dump_sha256": digest,
        "complete_upgrade_gate": False,
    }
    _require(steps._read(backup / "database.json") == expected)
    _require(
        steps._read(root / ".upgrade-control" / (point + "-artifacts-restored.json"))
        == {
            "schema": 1,
            "run_digest": run_digest,
            "restore_point": point,
            "backup_point": backup_point,
            "artifacts_sha256": artifact_digest,
            "independent_artifact_copy": True,
            "complete_upgrade_gate": False,
        }
    )
    _require(artifacts._inventory(root / ".upgrade-restores" / point) == inventory)
    # Primary artifacts may contain newer writes during rollback. They must not
    # replace the immutable final backup or the independently copied subtree.
    return backup / "database.dump", expected


def restore_database(
    point: str,
    *,
    run_digest: str,
    expected_artifacts_sha256: str,
    expected_dump_sha256: str,
    expected_system_identifier: str,
    expected_primary_database_oid: int,
    expected_scratch_database_oid: int,
) -> dict:
    """Restore once into the supplied observed scratch incarnation, never primary."""
    try:
        _require(platform.system() == "Linux")
        _require(type(point) is str and point in {"blocked", "final", "rollback"})
        for value in (run_digest, expected_artifacts_sha256, expected_dump_sha256):
            artifacts._digest(value)
        _require(
            type(expected_system_identifier) is str
            and re.fullmatch(r"[1-9][0-9]{0,19}", expected_system_identifier) is not None
            and int(expected_system_identifier) < 2**64
        )
        for oid in (expected_primary_database_oid, expected_scratch_database_oid):
            _require(type(oid) is int and 0 < oid < 2**32)
        _require(expected_primary_database_oid != expected_scratch_database_oid)
        primary = database._connection()  # Validates actual baseline + primary settings.
        _require(primary.database != primary.scratch)
        scratch = replace(primary, database=primary.scratch)
        root = artifacts._bound_root(run_digest)
        with tempfile.TemporaryDirectory(prefix="django-ray-upgrade-restore-", dir="/tmp") as raw:
            directory = Path(raw)
            primary_dir, scratch_dir = directory / "primary", directory / "scratch"
            primary_dir.mkdir(mode=0o700)
            scratch_dir.mkdir(mode=0o700)
            primary_env = database._client_environment(primary, primary_dir)
            scratch_env = database._client_environment(scratch, scratch_dir)
            identity = database._identity(primary, primary_dir, primary_env)
            _require(
                identity["system_identifier"] == expected_system_identifier
                and identity["primary_database_oid"] == expected_primary_database_oid
            )
            scratch_identity = database._identity(scratch, scratch_dir, scratch_env)
            _require(
                scratch_identity
                == identity | {"primary_database_oid": expected_scratch_database_oid}
            )
            dump, backup = _backup_and_clone(
                point,
                run_digest,
                expected_artifacts_sha256,
                expected_dump_sha256,
                primary,
                identity,
            )
            _require(
                _query(scratch, scratch_dir, scratch_env, _EMPTY_SQL) == b"t\tt\tt\tt\tt\tt\tt\tt\n"
            )
            reservation = {
                "run_digest": run_digest,
                "restore_point": point,
                "dump_sha256": expected_dump_sha256,
                "scratch_database_oid": expected_scratch_database_oid,
            }
            steps._write_once(
                root / ".upgrade-control" / (point + "-database-restore-reserved.json"), reservation
            )
            writable = scratch_env | {
                "PGOPTIONS": "-c default_transaction_read_only=off -c statement_timeout=30000 -c lock_timeout=5000"
            }
            database._run_client(
                (
                    str(database.POSTGRES_BIN / "pg_restore"),
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--no-tablespaces",
                    *database._connection_arguments(scratch),
                    str(dump),
                ),
                directory=scratch_dir,
                environment=writable,
                timeout=RESTORE_TIMEOUT,
                maximum=database.MAX_DIAGNOSTIC_BYTES,
            )
            _require(database._identity(primary, primary_dir, primary_env) == identity)
            _require(database._identity(scratch, scratch_dir, scratch_env) == scratch_identity)
            _require(_query(scratch, scratch_dir, scratch_env, _NO_OTHER_SESSIONS_SQL) == b"t\n")
            _require(
                _backup_and_clone(
                    point,
                    run_digest,
                    expected_artifacts_sha256,
                    expected_dump_sha256,
                    primary,
                    identity,
                )
                == (dump, backup)
            )
        receipt = {
            "schema": 1,
            **reservation,
            "backup_point": backup["backup_point"],
            "system_identifier": expected_system_identifier,
            "scratch_database": scratch.database,
            "artifacts_sha256": expected_artifacts_sha256,
            "transactional_restore_completed": True,
            "released_history_read_verified": False,
            "complete_upgrade_gate": False,
        }
        steps._write_once(root / ".upgrade-control" / (point + "-database-restored.json"), receipt)
        return receipt
    except Exception as error:
        if isinstance(error, DatabaseRestoreError):
            raise error from None
        raise DatabaseRestoreError("upgrade-database-restore-refused") from None
