"""Bounded current-table RuntimeEnv verification and envelope-only maintenance.

This module owns its raw database connection. In particular, snapshot/CAS SQL
never passes through Django's debug cursor, which logs parameters when DEBUG is
enabled. Elapsed time is cooperative; driver/OS/crypto/cleanup wall time is not
claimed to be a hard command deadline. No artifact or backup is inspected.
"""

from __future__ import annotations

import importlib
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from django.conf import settings

from django_ray.management.runtime_env_cursor import (
    MAX_PK,
    InvalidRuntimeEnvCursorError,
    RuntimeEnvCursor,
    decode_runtime_env_cursor,
    encode_runtime_env_cursor,
    keyed_fingerprint,
)
from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError, runtime_env_for_execution
from django_ray.runtime.runtime_env_encryption import (
    DJANGO_SECRET_KEY_ID,
    RuntimeEnvEncryptionConfig,
    RuntimeEnvEncryptionError,
    is_runtime_env_encryption_envelope_candidate,
    protect_runtime_env_snapshot,
    unprotect_runtime_env_snapshot,
    validate_runtime_env_encryption_settings,
)

OUTPUT_BYTES = 32768
_MODES = {"dry-run", "check", "rewrite"}
_TEXT_CAPS = {
    "task_id": 255,
    "runtime_env_profile": 100,
    "runtime_env_hash": 64,
    "state": 20,
    "claimed_by_worker": 255,
    "ray_job_id": 255,
    "cancellation_status": 20,
    "cancellation_error": 4096,
}
_NULLABLE = {
    "runtime_env_profile",
    "claimed_by_worker",
    "ray_job_id",
    "cancellation_status",
    "cancellation_error",
}
_CODES = (
    "current_key",
    "plaintext",
    "old_key",
    "rewritten",
    "rewrapped",
    "missing_identity",
    "invalid_snapshot",
    "unavailable_key",
    "row_too_large",
    "changed",
    "missing",
    "encryption_failed",
    "commit_uncertain",
    "database_unavailable",
    "interrupted",
)
_BLOCKERS = {
    "missing_identity",
    "invalid_snapshot",
    "unavailable_key",
    "row_too_large",
    "encryption_failed",
}
_INCOMPLETE = {
    "plaintext",
    "old_key",
    "changed",
    "missing",
    "commit_uncertain",
    "database_unavailable",
    "interrupted",
}


class MaintenanceError(ValueError):
    """A public fixed refusal; details and raw provider exceptions stay private."""


class _BudgetExpiredError(Exception):
    pass


class _ByteBudgetExpiredError(Exception):
    pass


@dataclass(frozen=True)
class MaintenanceLimits:
    """Input/work bounds, with an explicitly cooperative elapsed budget."""

    rows: int = 100
    snapshot_bytes: int = 65536
    total_bytes: int = 1048576
    seconds: float = 10
    details: int = 20

    def __post_init__(self) -> None:
        for value, low, high in (
            (self.rows, 1, 1000),
            (self.snapshot_bytes, 1, 1048576),
            (self.total_bytes, 1, 8388608),
            (self.details, 0, 100),
        ):
            if type(value) is not int or not low <= value <= high:
                raise MaintenanceError("Invalid RuntimeEnv maintenance budget.")
        if type(self.seconds) not in {int, float} or not math.isfinite(self.seconds):
            raise MaintenanceError("Invalid RuntimeEnv maintenance budget.")
        if not 0.1 <= self.seconds <= 60:
            raise MaintenanceError("Invalid RuntimeEnv maintenance budget.")


@dataclass(frozen=True, repr=False)
class _Snapshot:
    pk: int
    values: dict[str, Any]
    size: int
    oversized: bool


def _configuration(config: dict[str, Any], mode: str) -> tuple[dict, RuntimeEnvEncryptionConfig]:
    keys = config.get("RUNTIME_ENV_ENCRYPTION_KEYS", {})
    fallbacks = getattr(settings, "SECRET_KEY_FALLBACKS", ())
    if type(keys) is not dict or len(keys) > 32 or type(fallbacks) not in {tuple, list}:
        raise MaintenanceError("Invalid RuntimeEnv maintenance encryption configuration.")
    if len(fallbacks) > 8 or any(type(item) is not str or len(item) > 4096 for item in fallbacks):
        raise MaintenanceError("Invalid RuntimeEnv maintenance encryption configuration.")
    if any(
        type(key) is not str or len(key) > 64 or type(value) is not str or len(value) != 43
        for key, value in keys.items()
    ):
        raise MaintenanceError("Invalid RuntimeEnv maintenance encryption configuration.")
    try:
        original = validate_runtime_env_encryption_settings(config)
        if mode == "rewrite" and original.mode not in {"encrypted", "required"}:
            raise MaintenanceError("Rewrite requires encrypted or required storage mode.")
        # Maintenance validates plaintext for conversion, never for application execution.
        readable = {**config, "RUNTIME_ENV_STORAGE_MODE": "encrypted"}
        return readable, validate_runtime_env_encryption_settings(readable)
    except RuntimeEnvEncryptionError:
        raise MaintenanceError("Invalid RuntimeEnv maintenance encryption configuration.") from None


def _database_settings(using: str) -> dict[str, Any]:
    databases = settings.DATABASES
    if type(using) is not str or not 1 <= len(using) <= 100:
        raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
    if type(databases) is not dict or type(databases.get(using)) is not dict:
        raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
    source = databases[using]
    engine = source.get("ENGINE")
    options = source.get("OPTIONS", {})
    if type(options) is not dict or len(options) > 16:
        raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
    if engine == "django.db.backends.sqlite3":
        name = source.get("NAME")
        if type(name) not in {str, type(Path())} or len(str(name)) > 4096:
            raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
        if str(name) == ":memory:" or str(name).startswith("file:") or set(options) - {"timeout"}:
            raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
        try:
            path = Path(name).resolve(strict=True)
            if not path.is_file():
                raise OSError
        except (OSError, ValueError):
            raise MaintenanceError("RuntimeEnv maintenance database is unavailable.") from None
        return {"engine": "sqlite", "name": str(path), "using": using}
    if engine != "django.db.backends.postgresql":
        raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
    allowed = {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "application_name",
        "options",
        "connect_timeout",
    }
    if set(options) - allowed:
        raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
    result: dict[str, Any] = {"engine": "postgresql", "using": using, "options": dict(options)}
    for field in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
        value = source.get(field, "")
        if type(value) not in {str, int} or len(str(value)) > 4096:
            raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
        result[field.lower()] = str(value)
    if any(type(value) not in {str, int} or len(str(value)) > 4096 for value in options.values()):
        raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
    if type(options.get("options", "")) is not str:
        raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
    return result


def _policy(database: dict, encryption: RuntimeEnvEncryptionConfig, observed: dict) -> str:
    namespace = {key: value for key, value in database.items() if key != "password"}
    value = {
        "namespace": namespace,
        "observed": observed,
        "active": encryption.active_key_id,
        "keys": {key: value.hex() for key, value in sorted(encryption.keys.items())},
        "django_keys": [key.hex() for key in encryption.django_secret_keys],
    }
    return keyed_fingerprint(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


class _Database:
    """One raw driver connection with no Django debug query/parameter logging."""

    def __init__(self, config: dict, *, rewrite: bool, deadline: float, limits: MaintenanceLimits):
        self.config = config
        self.rewrite = rewrite
        self.deadline = deadline
        self.limits = limits
        self.connection: Any = None
        self.statements = 0
        self.placeholder = "?" if config["engine"] == "sqlite" else "%s"
        # Only a package-owned identifier is interpolated; values are parameters.
        self.table = '"django_ray_raytaskexecution"'

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise _BudgetExpiredError

    def open(self) -> None:
        self.check()
        if self.config["engine"] == "sqlite":
            mode = "rw" if self.rewrite else "ro"
            uri = Path(self.config["name"]).as_uri() + f"?mode={mode}"
            self.connection = sqlite3.connect(uri, uri=True, timeout=0.25, isolation_level=None)
            self.connection.set_progress_handler(lambda: time.monotonic() >= self.deadline, 1000)
            if not self.rewrite:
                self.query("PRAGMA query_only=ON")
        else:
            options = dict(self.config["options"])
            options["connect_timeout"] = 3
            options["options"] = options.get("options", "") + (
                " -c statement_timeout=1000 -c lock_timeout=250"
                + (" -c default_transaction_read_only=on" if not self.rewrite else "")
            )
            # Use Django's supported driver selection, but only its raw module.
            # Neither psycopg generation goes through CursorDebugWrapper here.
            driver = importlib.import_module("django.db.backends.postgresql.base").Database
            self.connection = driver.connect(
                dbname=self.config["name"],
                user=self.config["user"],
                password=self.config["password"],
                host=self.config["host"],
                port=self.config["port"],
                **options,
            )
            self.connection.autocommit = True

    def query(self, sql: str, parameters: tuple = (), *, fetch: str = "none") -> Any:
        self.check()
        self.statements += 1
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, parameters)
            if fetch == "one":
                return cursor.fetchone()
            if fetch == "all":
                return cursor.fetchall()
            return cursor.rowcount
        finally:
            cursor.close()

    def high_water(self) -> int:
        row = self.query(f"SELECT id FROM {self.table} ORDER BY id DESC LIMIT 1", fetch="one")
        value = row[0] if row else 0
        if type(value) is not int or not 0 <= value <= MAX_PK:
            raise MaintenanceError("Invalid RuntimeEnv maintenance row identity.")
        return value

    def binding(self) -> dict:
        """Bind the effective database/table, including libpq and schema defaults."""
        if self.config["engine"] == "sqlite":
            stat = Path(self.config["name"]).stat()
            return {"device": stat.st_dev, "inode": stat.st_ino}
        row = self.query(
            "SELECT n.nspname,c.oid FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.oid=to_regclass('django_ray_raytaskexecution') AND c.relkind IN ('r','p')",
            fetch="one",
        )
        if (
            row is None
            or type(row[0]) is not str
            or len(row[0].encode()) > 63
            or type(row[1]) is not int
        ):
            raise MaintenanceError("RuntimeEnv maintenance table is unavailable.")
        # Pin subsequent reads/updates to the exact resolved table namespace.
        self.table = '"' + row[0].replace('"', '""') + '"."django_ray_raytaskexecution"'
        info = self.connection.info
        effective = {name: getattr(info, name) for name in ("dbname", "user", "host", "port")}
        if any(
            type(value) not in {str, int} or len(str(value)) > 4096 for value in effective.values()
        ):
            raise MaintenanceError("Unsupported RuntimeEnv maintenance database.")
        return {**effective, "schema": row[0], "relation": row[1]}

    def page(self, after: int, high_water: int) -> list[int]:
        marker = self.placeholder
        rows = self.query(
            f"SELECT id FROM {self.table} WHERE id>{marker} AND id<={marker} "
            f"ORDER BY id LIMIT {marker}",
            (after, high_water, self.limits.rows + 1),
            fetch="all",
        )
        ids = [row[0] for row in rows]
        if any(type(pk) is not int or not after < pk <= high_water for pk in ids):
            raise MaintenanceError("Invalid RuntimeEnv maintenance row identity.")
        return ids

    def read(self, pk: int, remaining: int) -> _Snapshot | None:
        caps = {**_TEXT_CAPS, "runtime_env_json": self.limits.snapshot_bytes}
        lengths = {
            name: f"length(CAST({name} AS BLOB))"
            if self.config["engine"] == "sqlite"
            else f"octet_length({name})"
            for name in caps
        }
        total = "+".join(f"COALESCE({length},0)" for length in lengths.values())
        pieces = [
            f"CASE WHEN typeof({name})='integer' THEN {name} ELSE NULL END"
            if self.config["engine"] == "sqlite"
            else name
            for name in ("attempt_number", "execution_generation")
        ]
        for name, cap in caps.items():
            length = lengths[name]
            pieces.extend(
                [
                    f"CASE WHEN ({total})<={remaining} AND {length}<={cap} THEN {name} ELSE NULL END",
                    length,
                ]
            )
        row = self.query(
            f"SELECT {','.join(pieces)} FROM {self.table} WHERE id={self.placeholder}",
            (pk,),
            fetch="one",
        )
        if row is None:
            return None
        values = {"attempt_number": row[0], "execution_generation": row[1]}
        size, declared_size, oversized = 0, 0, False
        for index, (name, cap) in enumerate(caps.items()):
            value, length = row[2 + index * 2 : 4 + index * 2]
            if length is not None and (type(length) is not int or not 0 <= length <= cap):
                oversized = True
            if type(length) is int and length > 0:
                declared_size += length
            if value is not None:
                if type(value) is not str:
                    oversized = True
                else:
                    size += len(value.encode("utf-8"))
            values[name] = value
        if not oversized and declared_size > remaining:
            raise _ByteBudgetExpiredError
        return _Snapshot(pk, values, size, oversized)

    def replace(self, snapshot: _Snapshot, envelope: str) -> str:
        marker = self.placeholder
        predicates, parameters = [f"id={marker}"], [envelope, snapshot.pk]
        for name, value in snapshot.values.items():
            if value is None:
                predicates.append(f"{name} IS NULL")
            else:
                predicates.append(f"{name}={marker}")
                parameters.append(value)
        self.query("BEGIN")
        committed = False
        try:
            updated = self.query(
                f"UPDATE {self.table} SET runtime_env_json={marker} WHERE "
                + " AND ".join(predicates),
                tuple(parameters),
            )
            self.check()
            # A failure after asking for COMMIT is uncertain, even if rollback succeeds.
            try:
                self.connection.commit()
                committed = True
            except Exception:
                return "commit_uncertain"
            return "updated" if updated == 1 else "changed"
        finally:
            if not committed:
                try:
                    self.connection.rollback()
                except Exception:
                    # Preserve an already classified uncertain COMMIT. close()
                    # separately reports cleanup refusal without raw exceptions.
                    pass

    def close(self) -> bool:
        if self.connection is None:
            return True
        confirmed = True
        try:
            self.connection.rollback()
            if self.config["engine"] == "sqlite":
                self.connection.set_progress_handler(None, 0)
        except Exception:
            confirmed = False
        try:
            self.connection.close()
        except Exception:
            confirmed = False
        return confirmed


def _inspect(
    snapshot: _Snapshot, config: dict, encryption: RuntimeEnvEncryptionConfig
) -> tuple[str, str | None]:
    values = snapshot.values
    if snapshot.oversized:
        return "row_too_large", None
    if any(type(values[name]) is not str for name in _TEXT_CAPS if name not in _NULLABLE):
        return "invalid_snapshot", None
    if type(values["attempt_number"]) is not int or not 1 <= values["attempt_number"] <= MAX_PK:
        return "invalid_snapshot", None
    if (
        type(values["execution_generation"]) is not int
        or not 0 <= values["execution_generation"] <= MAX_PK
    ):
        return "invalid_snapshot", None
    if values["state"] not in {
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "CANCELLING",
        "LOST",
        "EXPIRED",
    }:
        return "invalid_snapshot", None
    if (
        not values["runtime_env_hash"]
        or values["runtime_env_profile"] == ""
        or not values["task_id"]
    ):
        return "missing_identity", None
    if any(not 33 <= ord(character) <= 126 for character in values["task_id"]):
        return "missing_identity", None
    try:
        parsed = json.loads(values["runtime_env_json"])
        encrypted = is_runtime_env_encryption_envelope_candidate(parsed)
        if encrypted:
            key = parsed.get("key_id")
            if (
                type(key) is str
                and key not in encryption.keys
                and not (key == DJANGO_SECRET_KEY_ID and encryption.django_secret_keys)
            ):
                return "unavailable_key", None
        execution = SimpleNamespace(pk=snapshot.pk, **values)
        resolved = runtime_env_for_execution(execution, config=config)
        if not encrypted:
            return "plaintext", resolved.serialized
        if parsed["key_id"] != encryption.active_key_id:
            return "old_key", resolved.serialized
        if parsed["key_id"] == DJANGO_SECRET_KEY_ID:
            current = replace(encryption, django_secret_keys=encryption.django_secret_keys[:1])
            try:
                unprotect_runtime_env_snapshot(
                    values["runtime_env_json"],
                    task_id=values["task_id"],
                    profile=values["runtime_env_profile"],
                    digest=values["runtime_env_hash"],
                    encryption=current,
                )
            except RuntimeEnvEncryptionError:
                return "old_key", resolved.serialized
        return "current_key", resolved.serialized
    except (
        RuntimeEnvSnapshotError,
        RuntimeEnvEncryptionError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        return "invalid_snapshot", None


def maintain_runtime_env(
    *,
    using: str,
    config: dict,
    mode: str = "dry-run",
    confirm: bool = False,
    cursor: str | None = None,
    limits: MaintenanceLimits | None = None,
) -> dict:
    """Observe or rewrite one finite range; never authorize backup key retirement."""
    if mode not in _MODES or type(confirm) is not bool or (mode == "rewrite" and not confirm):
        raise MaintenanceError("RuntimeEnv rewrite requires --mode rewrite and --confirm.")
    if confirm and mode != "rewrite":
        raise MaintenanceError("Confirmation is valid only for RuntimeEnv rewrite.")
    limits = limits or MaintenanceLimits()
    readable, encryption = _configuration(config, mode)
    database_config = _database_settings(using)
    try:
        boundary = decode_runtime_env_cursor(cursor) if cursor is not None else None
        if boundary is not None and boundary.mode != mode:
            raise InvalidRuntimeEnvCursorError
    except InvalidRuntimeEnvCursorError:
        raise MaintenanceError(
            "Invalid RuntimeEnv maintenance cursor; start a fresh pass."
        ) from None
    started = time.monotonic()
    database = _Database(
        database_config, rewrite=mode == "rewrite", deadline=started + limits.seconds, limits=limits
    )
    counts = dict.fromkeys(_CODES, 0)
    policy = None
    details: list[dict] = []
    processed, consumed = 0, 0
    after = boundary.after if boundary else 0
    high_water = boundary.high_water if boundary else 0
    boundary_known = boundary is not None
    more, exhausted, stop_code = True, None, None
    cleanup = False
    try:
        database.open()
        policy = _policy(database_config, encryption, database.binding())
        if boundary is not None and boundary.policy != policy:
            raise MaintenanceError("Invalid RuntimeEnv maintenance cursor; start a fresh pass.")
        if boundary is None:
            high_water = database.high_water()
            boundary_known = True
        ids = database.page(after, high_water)
        more = len(ids) > limits.rows
        for pk in ids[: limits.rows]:
            database.check()
            snapshot = database.read(pk, limits.total_bytes - consumed)
            if snapshot is None:
                code = "missing"
            else:
                if consumed + snapshot.size > limits.total_bytes:
                    exhausted, more = "total_bytes", True
                    break
                consumed += snapshot.size
                code, plaintext = _inspect(snapshot, readable, encryption)
                database.check()
                if mode == "rewrite" and code in {"plaintext", "old_key"}:
                    assert plaintext is not None
                    # Reserve a conservative envelope bound before crypto or a write.
                    estimate = (len(plaintext.encode()) + 16) * 4 // 3 + 512
                    if consumed + estimate > limits.total_bytes:
                        exhausted, more = "total_bytes", True
                        break
                    try:
                        envelope = protect_runtime_env_snapshot(
                            plaintext,
                            task_id=snapshot.values["task_id"],
                            profile=snapshot.values["runtime_env_profile"],
                            digest=snapshot.values["runtime_env_hash"],
                            encryption=encryption,
                        )
                    except RuntimeEnvEncryptionError:
                        code = "encryption_failed"
                    else:
                        consumed += len(envelope.encode())
                        if len(envelope.encode()) > limits.snapshot_bytes:
                            code = "row_too_large"
                        else:
                            result = database.replace(snapshot, envelope)
                            code = (
                                ("rewritten" if code == "plaintext" else "rewrapped")
                                if result == "updated"
                                else result
                            )
            counts[code] += 1
            processed += 1
            if len(details) < limits.details:
                details.append({"pk": pk, "outcome": code})
            if code == "commit_uncertain":
                more, stop_code = True, code
                break
            after = pk
        else:
            if not more:
                after = high_water
    except _BudgetExpiredError:
        exhausted, more = "elapsed", True
    except _ByteBudgetExpiredError:
        exhausted, more = "total_bytes", True
    except KeyboardInterrupt:
        counts["interrupted"] += 1
        more, stop_code = True, "interrupted"
    except MaintenanceError:
        raise
    except Exception:
        counts["database_unavailable"] += 1
        more, stop_code = True, "database_unavailable"
    finally:
        cleanup = database.close()
    next_cursor = None
    if more and boundary_known and policy is not None:
        try:
            next_cursor = encode_runtime_env_cursor(
                RuntimeEnvCursor(after, high_water, policy, mode)
            )
        except InvalidRuntimeEnvCursorError:
            stop_code = "cursor_unavailable"
    blockers = sum(counts[code] for code in _BLOCKERS)
    incomplete = (
        more or exhausted is not None or any(counts[code] for code in _INCOMPLETE) or not cleanup
    )
    exit_code = 1 if blockers else 2 if incomplete else 0
    return {
        "schema": "django-ray.runtime-env-maintenance",
        "schema_version": 1,
        "mode": mode,
        "counts": counts,
        "processed": processed,
        "processed_bytes": consumed,
        "details": details,
        "details_omitted": processed - len(details),
        "after": after,
        "high_water": high_water,
        "has_more": more,
        "next_cursor": next_cursor,
        "budget_exhausted": exhausted,
        "stop_code": stop_code,
        "cleanup_confirmed": cleanup,
        "range_complete": not more,
        "fresh_pass_required": True,
        "current_table_only": True,
        "backup_obligations": "not_verified",
        "key_retirement_authorized": False,
        "exit_code": exit_code,
        "limits": {
            **asdict(limits),
            "output_bytes": OUTPUT_BYTES,
            "time_contract": "cooperative",
            "connect_seconds": 3,
            "statement_milliseconds": 1000,
            "lock_milliseconds": 250,
        },
        "sql_statements": database.statements,
    }
