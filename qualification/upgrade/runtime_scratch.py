"""Create an absent scratch database once within an already admitted fixture.

The host owns the namespace, PostgreSQL instance, and observer sequencing.
This helper corroborates identities; it cannot authenticate that ownership or
fence an external administrator. An uncertain creation stays reserved. There is
no adoption, drop, reset, or automatic retry of an existing database.
"""

from __future__ import annotations

import platform
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from qualification.upgrade import runtime_artifacts as artifacts
from qualification.upgrade import runtime_database as database
from qualification.upgrade import runtime_restore as restore
from qualification.upgrade import runtime_steps as steps


class ScratchCreationError(ValueError):
    """A fixed refusal without connection details or provider diagnostics."""


def _require(condition: bool) -> None:
    if not condition:
        raise ScratchCreationError("upgrade-scratch-creation-refused")


def create_scratch(
    point: str,
    *,
    run_digest: str,
    expected_system_identifier: str,
    expected_primary_database_oid: int,
) -> dict:
    """Reserve, create, and observe a new scratch incarnation, never primary."""
    try:
        _require(platform.system() == "Linux")
        steps.store_cli_args(
            "create-scratch",
            steps.RuntimeStoreArguments(
                run_digest=run_digest,
                point=point,
                system_identifier=expected_system_identifier,
                primary_database_oid=expected_primary_database_oid,
            ),
        )
        root = artifacts._bound_root(run_digest)
        primary = database._connection()
        # Keep libpq connection-string syntax and SQL quoting out of this path,
        # even if the configuration loader is changed independently later.
        _require(
            all(
                re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) is not None
                for name in (primary.database, primary.scratch, primary.user)
            )
            and primary.database != primary.scratch
            and primary.scratch not in {"postgres", "template0", "template1"}
        )
        control = root / ".upgrade-control"
        reservation_path = control / (point + "-scratch-create-reserved.json")
        completion_path = control / (point + "-scratch-created.json")
        _require(not reservation_path.exists() and not completion_path.exists())
        scratch = replace(primary, database=primary.scratch)
        with tempfile.TemporaryDirectory(prefix="upgrade-scratch-", dir="/tmp") as raw:
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
            absent_sql = (
                "SELECT NOT EXISTS (SELECT FROM pg_catalog.pg_database WHERE datname='"
                + primary.scratch
                + "')"
            )
            _require(restore._query(primary, primary_dir, primary_env, absent_sql) == b"t\n")
            reservation = {
                "schema": 1,
                "run_digest": run_digest,
                "restore_point": point,
                "postgresql": identity,
                "scratch_database": scratch.database,
            }
            steps._write_once(reservation_path, reservation)
            writable = primary_env | {
                "PGOPTIONS": "-c default_transaction_read_only=off -c statement_timeout=30000 -c lock_timeout=5000"
            }
            # createdb has no --dbname; explicitly select the already observed
            # primary as its maintenance connection and the sole scratch target.
            database._run_client(
                (
                    str(database.POSTGRES_BIN / "createdb"),
                    "--host=" + primary.host,
                    "--port=" + primary.port,
                    "--username=" + primary.user,
                    "--maintenance-db=" + primary.database,
                    "--no-password",
                    "--template=template0",
                    "--encoding=UTF8",
                    "--owner=" + primary.user,
                    "--",
                    scratch.database,
                ),
                directory=primary_dir,
                environment=writable,
                timeout=60,
                maximum=database.MAX_DIAGNOSTIC_BYTES,
            )
            _require(database._identity(primary, primary_dir, primary_env) == identity)
            observed = database._identity(scratch, scratch_dir, scratch_env)
            oid = observed["primary_database_oid"]
            _require(
                type(oid) is int
                and 0 < oid < 2**32
                and oid != expected_primary_database_oid
                and observed == identity | {"primary_database_oid": oid}
            )
            _require(
                restore._query(scratch, scratch_dir, scratch_env, restore._EMPTY_SQL)
                == b"t\tt\tt\tt\tt\tt\tt\tt\n"
            )
            owner_sql = (
                "SELECT datdba=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user) "
                "FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database()"
            )
            _require(restore._query(scratch, scratch_dir, scratch_env, owner_sql) == b"t\n")
            _require(database._identity(scratch, scratch_dir, scratch_env) == observed)
        receipt = reservation | {
            "scratch_database_oid": oid,
            "empty_scratch_observed": True,
            "complete_upgrade_gate": False,
        }
        steps._write_once(completion_path, receipt)
        return receipt
    except Exception:
        raise ScratchCreationError("upgrade-scratch-creation-refused") from None
