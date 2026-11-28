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
REQUIRED_SECTIONS = SECTIONS[:7]
UNAVAILABLE_SECTIONS = SECTIONS[7:]
OUTCOMES = frozenset({"observed", "blocker", "unavailable", "unsupported", "budget_exhausted"})
STATUSES = frozenset({"ok", "blocker", "incomplete"})
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
        "lease_policy_invalid",
        "lease_policy_mismatch",
        "lease_policy_unavailable",
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
DETAIL_CODES = frozenset(
    {
        "invalid_django_ray_mapping",
        "invalid_runner",
        "invalid_runtime_env_storage_mode",
        "invalid_input_storage_backend",
        "invalid_result_storage_backend",
        "missing_input_storage_location",
        "missing_result_storage_location",
        "invalid_encryption_keys",
        "invalid_encryption_active_key",
        "invalid_secret_fallback",
        "invalid_secret_key",
        "invalid_encryption_settings",
        "invalid_worker_lease_seconds",
        "invalid_stuck_task_timeout_seconds",
        "missing_database_alias",
        "unsupported_timezone",
        "unsupported_engine",
        "invalid_database_options",
        "unsupported_sqlite_options",
        "invalid_sqlite_path",
        "unsupported_postgresql_options",
        "invalid_postgresql_settings",
    }
)
REMEDIATIONS: dict[str, str] = {
    "invalid_django_ray_mapping": ("Set settings.DJANGO_RAY to a dict with at most 256 entries."),
    "invalid_runner": "Set DJANGO_RAY['RUNNER'] to 'ray_core' or 'ray_job'.",
    "invalid_runtime_env_storage_mode": (
        "Set DJANGO_RAY['RUNTIME_ENV_STORAGE_MODE'] to 'plaintext', 'encrypted', or 'required'."
    ),
    "invalid_input_storage_backend": (
        "Set DJANGO_RAY['INPUT_STORAGE_BACKEND'] to None, 'filesystem', 's3', or 'gcs'."
    ),
    "invalid_result_storage_backend": (
        "Set DJANGO_RAY['RESULT_STORAGE_BACKEND'] to 'digest', 'filesystem', 's3', or 'gcs'."
    ),
    "missing_input_storage_location": (
        "Set INPUT_STORAGE_FILESYSTEM_PATH, INPUT_STORAGE_S3_BUCKET, or INPUT_STORAGE_GCS_BUCKET "
        "to match INPUT_STORAGE_BACKEND."
    ),
    "missing_result_storage_location": (
        "Set RESULT_STORAGE_FILESYSTEM_PATH, RESULT_STORAGE_S3_BUCKET, or "
        "RESULT_STORAGE_GCS_BUCKET to match RESULT_STORAGE_BACKEND."
    ),
    "invalid_encryption_keys": (
        "Configure DJANGO_RAY['RUNTIME_ENV_ENCRYPTION_KEYS'] as a dict of <=32 key IDs to "
        "43-char base64url 32-byte keys."
    ),
    "invalid_encryption_active_key": (
        "Set DJANGO_RAY['RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY'] to None or a valid <=64-char key ID."
    ),
    "invalid_secret_fallback": (
        "Set DJANGO_RAY['RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK'] to a boolean."
    ),
    "invalid_secret_key": (
        "Configure non-empty SECRET_KEY (and <=8 SECRET_KEY_FALLBACKS entries) when secret "
        "fallback is enabled."
    ),
    "invalid_encryption_settings": (
        "Ensure RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY references a configured key when "
        "RUNTIME_ENV_STORAGE_MODE is 'encrypted' or 'required'."
    ),
    "invalid_worker_lease_seconds": (
        "Set DJANGO_RAY['WORKER_LEASE_SECONDS'] to an integer between 1 and 86400."
    ),
    "invalid_stuck_task_timeout_seconds": (
        "Set DJANGO_RAY['STUCK_TASK_TIMEOUT_SECONDS'] to an integer between 1 and 86400."
    ),
    "missing_database_alias": (
        "Verify --database names an existing dict entry in settings.DATABASES."
    ),
    "unsupported_timezone": (
        "Set settings.USE_TZ = True and leave DATABASES[alias]['TIME_ZONE'] unset or 'UTC'."
    ),
    "unsupported_engine": (
        "Use django.db.backends.postgresql or django.db.backends.sqlite3 for django-ray."
    ),
    "invalid_database_options": (
        "Set DATABASES[alias]['OPTIONS'] to a dict with at most 32 supported options."
    ),
    "unsupported_sqlite_options": (
        "Remove unsupported SQLite OPTIONS keys (only 'timeout' is permitted for doctor)."
    ),
    "invalid_sqlite_path": ("Point SQLite DATABASES[alias]['NAME'] to an existing file path."),
    "unsupported_postgresql_options": (
        "Remove unsupported PostgreSQL OPTIONS keys for the read-only doctor probe."
    ),
    "invalid_postgresql_settings": (
        "Ensure PostgreSQL NAME/USER/PASSWORD/HOST/PORT settings are bounded strings or ints."
    ),
    "configuration_invalid": (
        "Inspect settings.DJANGO_RAY and run 'python manage.py check' to fix invalid settings."
    ),
    "settings_unavailable": (
        "Verify DJANGO_SETTINGS_MODULE can be imported cleanly in a fresh Python process."
    ),
    "settings_not_reproducible": (
        "Pass an importable --settings module instead of runtime settings.configure()."
    ),
    "database_unavailable": (
        "Verify database connectivity, credentials, and file/network reachability."
    ),
    "database_unsupported": (
        "Verify DATABASES[alias] uses supported PostgreSQL or file-backed SQLite with USE_TZ=True."
    ),
    "memory_database_unsupported": (
        "Doctor requires a persistent file-backed SQLite or PostgreSQL database, not :memory:."
    ),
    "migrations_missing": (
        "Run 'python manage.py migrate django_ray --database <alias>' to apply pending migrations."
    ),
    "migrations_unavailable": (
        "Ensure django_migrations exists and is readable by the database role."
    ),
    "protocol_invalid": (
        "Run 'python manage.py django_ray_protocol_status' to inspect protocol policy state."
    ),
    "lease_shape_invalid": (
        "Inspect malformed TaskWorkerLease rows; verify schema constraints and migrations."
    ),
    "lease_policy_invalid": (
        "Verify TaskWorkerLeasePolicy row exists (apply migration 0028) and check WORKER_LEASE_SECONDS."
    ),
    "lease_policy_mismatch": (
        "Align DJANGO_RAY['WORKER_LEASE_SECONDS'] with the database authority or update policy "
        "during a drained window with 'python manage.py django_ray_lease_policy --set-seconds ...'."
    ),
    "lease_policy_unavailable": ("Ensure TaskWorkerLeasePolicy table is migrated and readable."),
    "work_blockers": (
        "Inspect active/queued tasks via 'python manage.py django_ray_protocol_status' or Django Admin."
    ),
    "cleanup_errors": (
        "Re-run 'python manage.py django_ray_purge_inputs' and check external storage connectivity."
    ),
    "registry_shape_invalid": (
        "Inspect TaskInputPayload rows for unexpected state values outside ACTIVE/PURGED."
    ),
    "query_budget_exhausted": (
        "Doctor hit its SQL statement ceiling; inspect database load or slow triggers."
    ),
    "row_budget_exhausted": (
        "Doctor hit its returned-row ceiling; inspect migration or schema state."
    ),
    "probe_timeout": (
        "Increase --probe-seconds (3-20) and --total-seconds (5-60) or check DB responsiveness."
    ),
    "output_budget_exhausted": (
        "Doctor probe emitted output exceeding its safety ceiling; check for unexpected stdout/stderr."
    ),
    "probe_failed": (
        "Run 'python -m django_ray.doctor --settings <module>' to check isolated process startup."
    ),
    "probe_protocol_invalid": (
        "Ensure settings import does not print to stdout before doctor emits JSON frames."
    ),
    "interrupted": "Re-run django_ray_doctor without interrupting the process.",
    "cleanup_unconfirmed": ("Check for orphaned probe processes; re-run on an idle Linux host."),
    "linux_required": (
        "Run django_ray_doctor on Linux where isolated process-group supervision is supported."
    ),
    "supervision_unavailable": (
        "Invoke django_ray_doctor from the main thread with default SIGCHLD handling on Linux."
    ),
    "total_budget_exhausted": (
        "Increase --total-seconds (up to 60) so both configuration and database probes complete."
    ),
}
VALUE_KEYS = frozenset(
    {
        "detail",
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
        "lease_policy_seconds",
        "lease_policy_revision",
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
TEXT_VALUES = (
    frozenset(
        {
            "ray_core",
            "ray_job",
            "sync",
            "plaintext",
            "encrypted",
            "required",
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
    | DETAIL_CODES
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
        for flag, value, low, high in (
            ("--total-seconds", self.total_seconds, 5, 60),
            ("--probe-seconds", self.probe_seconds, 3, 20),
        ):
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError(
                    f"Invalid doctor time budget for {flag}: "
                    f"expected a finite number between {low} and {high}."
                )
        if self.probe_seconds > self.total_seconds - CLEANUP_SECONDS:
            raise ValueError(
                f"Invalid doctor time budget: --probe-seconds ({self.probe_seconds}) "
                f"must be <= --total-seconds - {int(CLEANUP_SECONDS)} "
                f"(reserve {int(CLEANUP_SECONDS)}s for outer cleanup)."
            )

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


def remediation_for(code: str, detail: str | None = None) -> str | None:
    """Return a fixed, secret-free remediation hint for a non-observed check."""
    if detail and detail in REMEDIATIONS:
        return REMEDIATIONS[detail]
    return REMEDIATIONS.get(code)


def result(sections: dict, limits: Limits, elapsed: float, cleanup: bool) -> dict:
    if set(sections) != set(SECTIONS):
        raise ValueError("Invalid doctor sections")
    for value in sections.values():
        validate_observation(value)
    deferred_sections = [
        name
        for name in UNAVAILABLE_SECTIONS
        if sections[name]["outcome"] == "unavailable"
        and sections[name]["code"] == "not_implemented"
    ]
    required_incomplete = any(sections[name]["outcome"] != "observed" for name in REQUIRED_SECTIONS)
    optional_unexpected = any(
        sections[name]["outcome"] != "observed" and name not in deferred_sections
        for name in UNAVAILABLE_SECTIONS
    )
    if not cleanup:
        exit_code = 2
    elif any(value["outcome"] == "blocker" for value in sections.values()):
        exit_code = 1
    elif required_incomplete or optional_unexpected:
        exit_code = 2
    else:
        exit_code = 0

    status = "ok" if exit_code == 0 else ("blocker" if exit_code == 1 else "incomplete")
    remediations: dict[str, str] = {}
    for name in SECTIONS:
        item = sections[name]
        if item["outcome"] == "observed" or name in deferred_sections:
            continue
        detail = item["values"].get("detail")
        hint = remediation_for(item["code"], detail if isinstance(detail, str) else None)
        if hint is not None:
            remediations[name] = hint
    if not cleanup and "cleanup" not in remediations:
        cleanup_hint = remediation_for("cleanup_unconfirmed")
        if cleanup_hint is not None:
            remediations["cleanup"] = cleanup_hint

    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "observed_at": timestamp(),
        "elapsed_milliseconds": max(0, min(120_000, int(elapsed * 1000))),
        "limits": limits.as_dict(),
        "sections": sections,
        "deferred_sections": deferred_sections,
        "remediations": remediations,
        "cleanup_confirmed": cleanup,
        "complete": exit_code == 0,
        "exit_code": exit_code,
    }
    # Eleven independently bounded observations fit below 32 KiB by construction.
    encode(report, limit=OUTPUT_BYTES)
    return report


_OUTCOME_LABELS: dict[str, str] = {
    "observed": "OK",
    "blocker": "BLOCKER",
    "unavailable": "UNAVAILABLE",
    "unsupported": "UNSUPPORTED",
    "budget_exhausted": "BUDGET",
}


def _format_value_lines(values: dict[str, object], *, per_line: int = 4) -> list[str]:
    if not values:
        return []
    pairs = [f"{key}={value}" for key, value in sorted(values.items())]
    return [
        "    " + "  ".join(pairs[index : index + per_line])
        for index in range(0, len(pairs), per_line)
    ]


def render(report: dict, *, as_json: bool) -> str:
    if as_json:
        return encode(report, limit=OUTPUT_BYTES).decode().rstrip("\n")
    status = str(report.get("status", "ok" if report["exit_code"] == 0 else "incomplete")).upper()
    lines = [
        f"{SCHEMA} v{SCHEMA_VERSION}  status={status}  "
        f"complete={str(report['complete']).lower()}  "
        f"cleanup_confirmed={str(report['cleanup_confirmed']).lower()}  "
        f"exit={report['exit_code']}",
        "",
        "Required checks:",
    ]
    for name in REQUIRED_SECTIONS:
        item = report["sections"][name]
        tag = f"[{_OUTCOME_LABELS.get(item['outcome'], item['outcome'].upper())}]"
        lines.append(f"  {tag:<13} {name:<21} {item['outcome']} ({item['code']})")
        lines.extend(_format_value_lines(item["values"]))

    lines.extend(["", "Optional checks (deferred / out-of-scope):"])
    for name in UNAVAILABLE_SECTIONS:
        item = report["sections"][name]
        if item["outcome"] == "unavailable" and item["code"] == "not_implemented":
            tag = "[SKIPPED]"
        else:
            tag = f"[{_OUTCOME_LABELS.get(item['outcome'], item['outcome'].upper())}]"
        lines.append(f"  {tag:<13} {name:<21} {item['outcome']} ({item['code']})")
        lines.extend(_format_value_lines(item["values"]))

    remediations = report.get("remediations") or {}
    if remediations:
        lines.extend(["", "Remediation:"])
        for section_name, hint in remediations.items():
            lines.append(f"  - {section_name}: {hint}")

    text = "\n".join(lines)
    if len(text.encode()) + 1 > OUTPUT_BYTES:
        raise ValueError("Doctor output budget exhausted")
    return text
