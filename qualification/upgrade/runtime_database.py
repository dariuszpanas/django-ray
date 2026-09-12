"""Bounded PostgreSQL backups for the admitted, source-owned upgrade fixture.

Only the released baseline's primary database can be backed up. The caller must
already have observed the PostgreSQL system identifier and database OID. Checks
here corroborate those identities and the local artifact run binding; they do
not authenticate resources, stop writers, or create an atomic cross-store
snapshot. A failed dump remains reserved, without a completion receipt.
"""

from __future__ import annotations

import hashlib
import math
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO

from qualification.upgrade import runtime_artifacts as artifacts
from qualification.upgrade import runtime_steps as steps

POSTGRES_BIN = Path("/usr/lib/postgresql/17/bin")
MAX_DUMP_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 16 * 1024
DUMP_TIMEOUT = 60
IDENTITY_TIMEOUT = 10
CLEANUP_TIMEOUT = 5
_IDENTITY_SQL = (
    "SELECT system_identifier::text, "
    "(SELECT oid::text FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database()), "
    "pg_catalog.current_database(), pg_catalog.current_setting('server_version_num'), "
    "pg_catalog.pg_is_in_recovery(), current_user "
    "FROM pg_catalog.pg_control_system()"
)


class DatabaseBackupError(ValueError):
    """Fixed refusals without provider output, connection strings or secrets."""


def _require(condition: bool) -> None:
    if not condition:
        raise DatabaseBackupError("invalid-upgrade-database-backup")


@dataclass(frozen=True)
class _Connection:
    host: str
    port: str
    user: str
    database: str
    scratch: str
    password: str = field(repr=False)


def _connection() -> _Connection:
    # Import only after the Linux/argument checks. The settings loader performs
    # local validation, with no Django setup, database connection or Ray import.
    from django_ray import __version__
    from qualification.upgrade.runtime_settings import build_runtime_settings

    environment = dict(os.environ)
    _require(
        environment.get("DJANGO_RAY_UPGRADE_BUILD") == "baseline"
        and environment.get("DJANGO_RAY_UPGRADE_DATABASE") == "primary"
        and __version__ == version("django-ray") == "0.4.0"
        and version("ray") == "2.56.0"
        and platform.python_implementation() == "CPython"
    )
    config = build_runtime_settings(
        environment,
        package_version=__version__,
        ray_version=version("ray"),
        python_version=platform.python_version(),
    )
    database = config["DATABASES"]["default"]
    primary = environment["DJANGO_RAY_UPGRADE_PRIMARY_DATABASE"]
    scratch = environment["DJANGO_RAY_UPGRADE_SCRATCH_DATABASE"]
    _require(database["NAME"] == primary and primary != scratch)
    return _Connection(
        database["HOST"], database["PORT"], database["USER"], primary, scratch, database["PASSWORD"]
    )


def _client_environment(connection: _Connection, directory: Path) -> dict[str, str]:
    # Escape libpq's password-file delimiters; no wildcard connection fields.
    password = connection.password.replace("\\", "\\\\").replace(":", "\\:")
    passfile = directory / "pgpass"
    descriptor = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(
            (
                f"{connection.host}:{connection.port}:{connection.database}:"
                f"{connection.user}:{password}\n"
            ).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
    # Do not copy os.environ. This also excludes PG service/routing overrides,
    # loader injection, unrelated credentials and caller-supplied locale paths.
    return {
        "HOME": str(directory),
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "PGPASSFILE": str(passfile),
        "PGCONNECT_TIMEOUT": "5",
        "PGCLIENTENCODING": "UTF8",
        "PGSSLMODE": "disable",  # The fixed private fixture has no TLS listener.
        "PGGSSENCMODE": "disable",
        "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=5000",
    }


def _connection_arguments(connection: _Connection) -> tuple[str, ...]:
    return (
        "--host=" + connection.host,
        "--port=" + connection.port,
        "--username=" + connection.user,
        "--dbname=" + connection.database,
        "--no-password",
    )


def _kill_and_reap(process: subprocess.Popen) -> None:
    # The fixed nonparallel clients do not spawn worker processes. Own their
    # session as well, and never wait indefinitely or report an uncertain reap
    # as a completed backup. No poll() reaps the PID before this kill attempt.
    signalled = True
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        signalled = False
    try:
        process.wait(timeout=CLEANUP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        raise DatabaseBackupError("upgrade-database-cleanup-unconfirmed") from None
    if not signalled:
        raise DatabaseBackupError("upgrade-database-cleanup-unconfirmed")


def _run_client(
    command: tuple[str, ...],
    *,
    directory: Path,
    environment: dict[str, str],
    timeout: int,
    maximum: int,
    output: BinaryIO | None = None,
) -> bytes:
    """Drain nonblocking pipes with byte/deadline limits; never retain stderr."""
    began = previous = time.monotonic()
    _require(math.isfinite(began))

    def remaining() -> float:
        nonlocal previous
        current = time.monotonic()
        _require(math.isfinite(current) and current >= previous)
        previous = current
        if current >= began + timeout:
            raise DatabaseBackupError("upgrade-database-client-timeout")
        return began + timeout - current

    process = subprocess.Popen(
        command,
        cwd=directory,
        env=environment,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        close_fds=True,
        start_new_session=True,
    )
    reaped = False
    captured = bytearray()
    totals = {"stdout": 0, "stderr": 0}
    try:
        _require(process.stdout is not None and process.stderr is not None)
        with selectors.DefaultSelector() as selected:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selected.register(stream, selectors.EVENT_READ, name)
            while selected.get_map():
                for key, _events in selected.select(min(0.1, remaining())):
                    remaining()
                    try:
                        block = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selected.unregister(key.fileobj)
                        continue
                    name = key.data
                    totals[name] += len(block)
                    limit = maximum if name == "stdout" else MAX_DIAGNOSTIC_BYTES
                    if totals[name] > limit:
                        raise DatabaseBackupError("upgrade-database-client-output-limit")
                    if name == "stdout":
                        if output is None:
                            captured.extend(block)
                        else:
                            output.write(block)
        code = process.wait(timeout=remaining())
        reaped = True
        remaining()
        # Warnings are not silently accepted or copied into receipts/logs.
        if code != 0 or totals["stderr"]:
            raise DatabaseBackupError("upgrade-database-client-failed")
        return bytes(captured)
    finally:
        try:
            if not reaped:
                _kill_and_reap(process)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _identity(connection: _Connection, directory: Path, environment: dict[str, str]) -> dict:
    raw = _run_client(
        (
            str(POSTGRES_BIN / "psql"),
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--field-separator=\t",
            "--command=" + _IDENTITY_SQL,
            *_connection_arguments(connection),
        ),
        directory=directory,
        environment=environment,
        timeout=IDENTITY_TIMEOUT,
        maximum=1024,
    )
    try:
        fields = raw.decode("ascii").removesuffix("\n").split("\t")
        _require(len(fields) == 6)
        system, oid, name, server, recovery, user = fields
        _require(re.fullmatch(r"[1-9][0-9]{0,19}", system) is not None)
        _require(int(system) < 2**64 and re.fullmatch(r"[1-9][0-9]{0,9}", oid) is not None)
        _require(int(oid) < 2**32 and re.fullmatch(r"17[0-9]{4}", server) is not None)
        _require(name == connection.database and user == connection.user and recovery == "f")
    except (UnicodeError, ValueError):
        raise DatabaseBackupError("upgrade-database-identity-refused") from None
    return {"system_identifier": system, "primary_database_oid": int(oid), "server_version": server}


def _artifact_backup(point: str, run_digest: str, expected_digest: str) -> tuple[Path, dict]:
    root = artifacts._bound_root(run_digest)
    backup = root / ".upgrade-backups" / point
    artifacts._directory(backup)
    inventory = artifacts._inventory(backup / "artifacts")
    expected = {
        "schema": 1,
        "run_digest": run_digest,
        "backup_point": point,
        "inventory": inventory,
        "artifacts_sha256": hashlib.sha256(steps._json(inventory)).hexdigest(),
    }
    _require(steps._read(backup / "artifacts.json") == expected)
    _require(expected["artifacts_sha256"] == expected_digest)
    _require(artifacts._inventory(root) == inventory)
    return backup, expected


def observe_database(*, run_digest: str) -> dict:
    """Read primary identity for an admitted host without setup or mutation.

    The host must attribute this observer to its owned PostgreSQL service.
    Reading a system identifier does not independently prove that ownership.
    """
    try:
        _require(platform.system() == "Linux")
        steps.store_cli_args("observe-database", steps.RuntimeStoreArguments(run_digest=run_digest))
        artifacts._bound_root(run_digest)
        connection = _connection()
        with tempfile.TemporaryDirectory(prefix="upgrade-database-observe-", dir="/tmp") as raw:
            directory = Path(raw)
            environment = _client_environment(connection, directory)
            identity = _identity(connection, directory, environment)
            _require(_identity(connection, directory, environment) == identity)
        return {
            "schema": 1,
            "run_digest": run_digest,
            "postgresql": identity,
            "complete_upgrade_gate": False,
        }
    except Exception:
        raise DatabaseBackupError("upgrade-database-observation-refused") from None


def _dump_identity(path: Path) -> tuple[int, str]:
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
    _require(5 < before.st_size <= MAX_DUMP_BYTES)
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        _require(
            stat.S_ISREG(opened.st_mode)
            and opened.st_nlink == 1
            and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino)
        )
        _require(stream.read(5) == b"PGDMP")
        stream.seek(0)
        while block := stream.read(65536):
            total += len(block)
            _require(total <= MAX_DUMP_BYTES)
            digest.update(block)
        after = os.fstat(stream.fileno())
    latest = path.lstat()
    _require(
        total == before.st_size == after.st_size == latest.st_size
        and before.st_mtime_ns == after.st_mtime_ns == latest.st_mtime_ns
        and (before.st_dev, before.st_ino)
        == (after.st_dev, after.st_ino)
        == (latest.st_dev, latest.st_ino)
        and stat.S_ISREG(after.st_mode)
        and stat.S_ISREG(latest.st_mode)
        and after.st_nlink == latest.st_nlink == 1
    )
    return total, digest.hexdigest()


def backup_database(
    point: str,
    *,
    run_digest: str,
    expected_artifacts_sha256: str,
    expected_system_identifier: str,
    expected_primary_database_oid: int,
) -> dict:
    """Back up one fresh artifact point; observer inputs are not self-authenticating.

    Only an actually observed, source-owned PostgreSQL identity may be supplied.
    Success describes the dump and corroborating checks, never stopped writers,
    an atomic database/artifact snapshot, restore usability or native task proof.
    """
    try:
        _require(platform.system() == "Linux")
        _require(type(point) is str and point in {"blocked", "final"})
        artifacts._digest(run_digest)
        artifacts._digest(expected_artifacts_sha256)
        _require(
            type(expected_system_identifier) is str
            and re.fullmatch(r"[1-9][0-9]{0,19}", expected_system_identifier) is not None
            and int(expected_system_identifier) < 2**64
        )
        _require(
            type(expected_primary_database_oid) is int and 0 < expected_primary_database_oid < 2**32
        )
        connection = _connection()
        backup, artifact_receipt = _artifact_backup(point, run_digest, expected_artifacts_sha256)
        _require({path.name for path in backup.iterdir()} == {"artifacts", "artifacts.json"})
        _require(shutil.disk_usage(backup).free >= MAX_DUMP_BYTES + artifacts.FREE_RESERVE_BYTES)
        with tempfile.TemporaryDirectory(prefix="django-ray-upgrade-database-", dir="/tmp") as raw:
            directory = Path(raw)
            environment = _client_environment(connection, directory)
            identity = _identity(connection, directory, environment)
            _require(
                identity["system_identifier"] == expected_system_identifier
                and identity["primary_database_oid"] == expected_primary_database_oid
            )
            dump = backup / "database.dump"
            descriptor = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                _run_client(
                    (
                        str(POSTGRES_BIN / "pg_dump"),
                        "--format=custom",
                        "--encoding=UTF8",
                        "--lock-wait-timeout=5000",
                        *_connection_arguments(connection),
                    ),
                    directory=directory,
                    environment=environment,
                    timeout=DUMP_TIMEOUT,
                    maximum=MAX_DUMP_BYTES,
                    output=output,
                )
                output.flush()
                os.fsync(output.fileno())
            size, digest = _dump_identity(dump)
            _require(_identity(connection, directory, environment) == identity)
            current_backup, current_receipt = _artifact_backup(
                point, run_digest, expected_artifacts_sha256
            )
            _require(current_backup == backup and current_receipt == artifact_receipt)
        receipt = {
            "schema": 1,
            "run_digest": run_digest,
            "backup_point": point,
            "artifacts_sha256": expected_artifacts_sha256,
            "postgresql": identity,
            "database": connection.database,
            "format": "custom",
            "dump_bytes": size,
            "dump_sha256": digest,
            "complete_upgrade_gate": False,
        }
        steps._write_once(backup / "database.json", receipt)
        return receipt
    except Exception as error:
        # Never expose subprocess/driver/path/Secret exception strings or repr.
        if isinstance(error, DatabaseBackupError):
            raise error from None
        raise DatabaseBackupError("upgrade-database-backup-refused") from None
