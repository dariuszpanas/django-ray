"""Finite, secret-free contracts shared by the doctor and its owned processes."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

SCHEMA = "django-ray.doctor"
SCHEMA_VERSION = 1
OUTPUT_BYTES = 32 * 1024
FRAME_BYTES = 2048
STDOUT_BYTES = 64 * 1024
STDERR_BYTES = 4096
CLEANUP_SECONDS = 2.0
SQL_STATEMENTS = 32
RETURNED_ROWS = 64
CONNECT_SECONDS = 3
STATEMENT_MILLISECONDS = 1000
LOCK_MILLISECONDS = 250
EXTERNAL_REQUESTS = 0

SECTIONS = (
    "configuration",
    "database",
    "migrations",
    "protocol",
    "leases",
    "work",
    "registered_cleanup",
    "target",
    "queue_capacity",
    "artifact_availability",
    "orphan_inventory",
)
DATABASE_SECTIONS = SECTIONS[1:7]
UNAVAILABLE_SECTIONS = SECTIONS[7:]
OUTCOMES = frozenset({"observed", "blocker", "unavailable", "unsupported", "budget_exhausted"})
CODES = frozenset(
    {
        "observed",
        "not_implemented",
        "not_observed",
        "configuration_invalid",
        "settings_unavailable",
        "settings_not_reproducible",
        "database_unavailable",
        "database_unsupported",
        "memory_database_unsupported",
        "migrations_missing",
        "migrations_unavailable",
        "protocol_invalid",
        "lease_shape_invalid",
        "work_blockers",
        "cleanup_errors",
        "registry_shape_invalid",
        "query_budget_exhausted",
        "row_budget_exhausted",
        "probe_timeout",
        "output_budget_exhausted",
        "probe_failed",
        "probe_protocol_invalid",
        "interrupted",
        "cleanup_unconfirmed",
        "linux_required",
        "supervision_unavailable",
        "total_budget_exhausted",
    }
)
VALUE_KEYS = frozenset(
    {
        "package_version",
        "runner",
        "storage_mode",
        "input_backend",
        "result_backend",
        "key_count",
        "active_key_configured",
        "secret_fallback_configured",
        "key_material_shape_valid",
        "vendor",
        "read_only",
        "expected",
        "applied",
        "missing",
        "unknown",
        "write_version",
        "minimum_supported",
        "maximum_supported",
        "legacy_admission",
        "total",
        "active",
        "live",
        "stale_active",
        "malformed",
        "legacy_active",
        "queued",
        "running",
        "cancelling",
        "terminal",
        "invalid_state",
        "unsupported_protocol",
        "queued_without_protocol_lease",
        "active_without_heartbeat",
        "queue_expired",
        "purged",
        "cleanup_errors",
        "sql_statements",
        "returned_rows",
    }
)
TEXT_VALUES = frozenset(
    {
        "ray_core",
        "ray_job",
        "sync",
        "plaintext",
        "encrypted",
        "none",
        "digest",
        "filesystem",
        "s3",
        "gcs",
        "postgresql",
        "sqlite",
        "unsupported",
    }
)
_MODULE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*\Z")
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){1,3}(?:[a-z0-9.+-]{0,32})\Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z\Z")


@dataclass(frozen=True)
class Limits:
    """Timeouts include the two-second cleanup reserve, not just query time."""

    total_seconds: float = 15
    probe_seconds: float = 5

    def __post_init__(self) -> None:
        for value, low, high in ((self.total_seconds, 5, 60), (self.probe_seconds, 3, 20)):
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError("Invalid doctor time budget")
        if self.probe_seconds > self.total_seconds - CLEANUP_SECONDS:
            raise ValueError("Doctor total budget must reserve outer cleanup")

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "cleanup_seconds": CLEANUP_SECONDS,
            "connect_seconds": CONNECT_SECONDS,
            "statement_milliseconds": STATEMENT_MILLISECONDS,
            "lock_milliseconds": LOCK_MILLISECONDS,
            "sql_statements_per_probe": SQL_STATEMENTS,
            "returned_rows_per_probe": RETURNED_ROWS,
            "external_requests": EXTERNAL_REQUESTS,
            "active_supervisors": 1,
            "active_probes": 1,
            "stdout_bytes_per_probe": STDOUT_BYTES,
            "stderr_bytes_per_probe": STDERR_BYTES,
            "output_bytes": OUTPUT_BYTES,
        }


def settings_module_valid(value: object) -> bool:
    return type(value) is str and len(value) <= 255 and _MODULE.fullmatch(value) is not None


def database_alias_valid(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9_.-]+", value) is not None
    )


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def encode(value: dict, *, limit: int = FRAME_BYTES) -> bytes:
    """Bound construction inputs before encoding; no unbounded fallback rendering."""
    raw = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(raw) > limit:
        raise ValueError("Doctor output budget exhausted")
    return raw


def observation(outcome: str, code: str, **values) -> dict:
    value = {"outcome": outcome, "code": code, "observed_at": timestamp(), "values": values}
    validate_observation(value)
    return value


def validate_observation(value: object) -> None:
    if type(value) is not dict or set(value) != {"outcome", "code", "observed_at", "values"}:
        raise ValueError("Invalid doctor observation")
    if (
        type(value["outcome"]) is not str
        or type(value["code"]) is not str
        or value["outcome"] not in OUTCOMES
        or value["code"] not in CODES
    ):
        raise ValueError("Invalid doctor observation")
    observed = value["observed_at"]
    if type(observed) is not str or len(observed) > 40 or _TIMESTAMP.fullmatch(observed) is None:
        raise ValueError("Invalid doctor observation")
    values = value["values"]
    if type(values) is not dict or len(values) > 20 or not set(values) <= VALUE_KEYS:
        raise ValueError("Invalid doctor values")
    for key, item in values.items():
        if type(item) is bool or item is None:
            continue
        if type(item) is int and 0 <= item <= (1 << 63) - 1:
            continue
        if type(item) is str and len(item) <= 64:
            if key == "package_version" and _VERSION.fullmatch(item):
                continue
            if key != "package_version" and item in TEXT_VALUES:
                continue
        raise ValueError("Invalid doctor value")
    encode(value)


def section_frame(name: str, value: dict) -> dict:
    if type(name) is not str or name not in SECTIONS:
        raise ValueError("Invalid doctor section")
    validate_observation(value)
    frame = {"kind": "section", "section": name, "observation": value}
    encode(frame)
    return frame


def parse_section(raw: bytes) -> tuple[str, dict]:
    if len(raw) > FRAME_BYTES:
        raise ValueError("Doctor frame too large")
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {"kind", "section", "observation"}:
        raise ValueError("Invalid doctor frame")
    if value["kind"] != "section":
        raise ValueError("Invalid doctor frame")
    section_frame(value["section"], value["observation"])
    return value["section"], value["observation"]


def initial_sections(code: str = "not_observed", outcome: str = "unavailable") -> dict:
    return {
        name: observation("unavailable", "not_implemented")
        if name in UNAVAILABLE_SECTIONS
        else observation(outcome, code)
        for name in SECTIONS
    }


def result(sections: dict, limits: Limits, elapsed: float, cleanup: bool) -> dict:
    if set(sections) != set(SECTIONS):
        raise ValueError("Invalid doctor sections")
    for value in sections.values():
        validate_observation(value)
    if not cleanup:
        exit_code = 2
    elif any(value["outcome"] == "blocker" for value in sections.values()):
        exit_code = 1
    elif any(value["outcome"] != "observed" for value in sections.values()):
        exit_code = 2
    else:
        exit_code = 0
    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "observed_at": timestamp(),
        "elapsed_milliseconds": max(0, min(120_000, int(elapsed * 1000))),
        "limits": limits.as_dict(),
        "sections": sections,
        "cleanup_confirmed": cleanup,
        "complete": exit_code == 0,
        "exit_code": exit_code,
    }
    # Eleven independently bounded observations fit below 32 KiB by construction.
    encode(report, limit=OUTPUT_BYTES)
    return report


def render(report: dict, *, as_json: bool) -> str:
    if as_json:
        return encode(report, limit=OUTPUT_BYTES).decode().rstrip("\n")
    lines = [f"{SCHEMA} v{SCHEMA_VERSION} complete={str(report['complete']).lower()}"]
    for name in SECTIONS:
        item = report["sections"][name]
        fields = " ".join(f"{key}={value}" for key, value in sorted(item["values"].items()))
        lines.append(f"{name}: {item['outcome']} ({item['code']}) {fields}".rstrip())
    lines.append(
        f"cleanup_confirmed={str(report['cleanup_confirmed']).lower()} exit={report['exit_code']}"
    )
    text = "\n".join(lines)
    if len(text.encode()) + 1 > OUTPUT_BYTES:
        raise ValueError("Doctor output budget exhausted")
    return text
