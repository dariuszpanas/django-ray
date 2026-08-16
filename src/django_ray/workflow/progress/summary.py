"""Bounded schema-v3 workflow-progress summary contract."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, cast, overload
from uuid import UUID

from django_ray.runtime.context import (
    WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION,
    WorkflowRunIdentity,
)
from django_ray.workflow._compat import preserve_legacy_module_identity

WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION = 3
WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION = 1
WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES = 16 * 1024
WORKFLOW_PROGRESS_LEGACY_MAX_BYTES = 64 * 1024 * 1024
WORKFLOW_PROGRESS_SUMMARY_LIMITS_PROFILE = "v1"
WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS = 30
_MAX_PROTOCOL_TIMESTAMP = datetime.max.replace(tzinfo=UTC) - timedelta(
    days=WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS
)

_MAX_COUNTER = (1 << 63) - 1
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MANIFEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PLAN_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUMMARY_KEYS = frozenset(
    {
        "schema_version",
        "storage_protocol_version",
        "run_identity",
        "reporting_policy",
        "selected_strategy",
        "plan_fingerprint",
        "limits_profile",
        "summary_revision",
        "topology_version",
        "detail_revision",
        "state",
        "node_counts",
        "edge_counts",
        "progress_percent",
        "timestamps",
        "detail",
        "storage",
        "retention",
        "terminal",
    }
)
_RUN_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "task_execution_pk",
        "attempt_number",
        "execution_generation",
    }
)
_NODE_COUNT_KEYS = frozenset(
    {
        "declared",
        "discovered",
        "retained_topology",
        "retained_detail",
        "pending",
        "running",
        "succeeded",
        "failed",
    }
)
_EDGE_COUNT_KEYS = frozenset({"declared", "discovered", "retained_topology"})
_TIMESTAMP_KEYS = frozenset({"started_at", "updated_at", "finished_at"})
_DETAIL_KEYS = frozenset({"availability", "complete", "truncation_reasons"})
_STORAGE_KEYS = frozenset({"kind", "manifest_id"})
_RETENTION_KEYS = frozenset({"detail_days", "detail_expires_at"})
_TERMINAL_KEYS = frozenset({"outcome", "finished_at"})


class WorkflowProgressSummaryError(ValueError):
    """Raised when a schema-v3 summary violates the bounded protocol."""


def _utf8_length(value: str, name: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise WorkflowProgressSummaryError(f"{name} must contain valid UTF-8 text") from error


class WorkflowProgressDetailAvailability(StrEnum):
    """Exact durable detail-availability vocabulary from ADR-0004."""

    NOT_REPORTED = "NOT_REPORTED"
    AVAILABLE = "AVAILABLE"
    TRUNCATED = "TRUNCATED"
    OMITTED_BY_POLICY = "OMITTED_BY_POLICY"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"


class WorkflowProgressTruncationReason(StrEnum):
    """Protocol-v1 reasons that make retained detail incomplete or last-observed."""

    NODE_COUNT_LIMIT = "node_count_limit"
    EDGE_COUNT_LIMIT = "edge_count_limit"
    TOPOLOGY_ENCODED_BYTES = "topology_encoded_bytes"
    TOPOLOGY_DECODED_BYTES = "topology_decoded_bytes"
    DETAIL_COUNT_LIMIT = "detail_count_limit"
    DETAIL_ENCODED_BYTES = "detail_encoded_bytes"
    DETAIL_DECODED_BYTES = "detail_decoded_bytes"
    RECORD_SIZE_LIMIT = "record_size_limit"
    REPORTING_POLICY = "reporting_policy"
    TERMINAL_STATE_UNREPORTED = "terminal_state_unreported"


WORKFLOW_PROGRESS_REPORTING_POLICIES = frozenset({"full", "sampled", "terminal_only", "disabled"})
WORKFLOW_PROGRESS_STATES = frozenset(
    {"RUNNING", "CANCELLING", "SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"}
)
WORKFLOW_PROGRESS_TERMINAL_STATES = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"}
)


def workflow_progress_detail_is_last_observed(value: Any) -> bool:
    """Return whether lifecycle success retained pre-terminal node states."""
    if not isinstance(value, dict):
        return False
    detail = value.get("detail")
    terminal = value.get("terminal")
    return (
        value.get("state") == "SUCCEEDED"
        and value.get("detail_revision") is not None
        and isinstance(detail, dict)
        and detail.get("availability") == WorkflowProgressDetailAvailability.TRUNCATED.value
        and detail.get("complete") is False
        and isinstance(detail.get("truncation_reasons"), list)
        and WorkflowProgressTruncationReason.TERMINAL_STATE_UNREPORTED.value
        in detail["truncation_reasons"]
        and isinstance(terminal, dict)
        and terminal.get("outcome") == "SUCCEEDED"
    )


def _exact_object(value: Any, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkflowProgressSummaryError(f"{name} must contain the exact protocol fields")
    return value


@overload
def _bounded_int(value: Any, name: str, *, nullable: Literal[False] = False) -> int: ...


@overload
def _bounded_int(value: Any, name: str, *, nullable: Literal[True]) -> int | None: ...


def _bounded_int(value: Any, name: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_COUNTER:
        raise WorkflowProgressSummaryError(f"{name} must be a bounded non-negative integer")
    return value


def _bounded_identifier(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise WorkflowProgressSummaryError(f"{name} must be a bounded protocol identifier")
    return value


def _canonical_utc_timestamp(
    value: Any,
    name: str,
    *,
    nullable: bool = False,
    retention_expiry: bool = False,
) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _utf8_length(value, name) > 32 or not value.endswith("Z"):
        raise WorkflowProgressSummaryError(f"{name} must be a bounded UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise WorkflowProgressSummaryError(f"{name} must be a bounded UTC timestamp") from error
    maximum = datetime.max.replace(tzinfo=UTC) if retention_expiry else _MAX_PROTOCOL_TIMESTAMP
    if parsed > maximum:
        raise WorkflowProgressSummaryError(f"{name} must be a bounded UTC timestamp")
    canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise WorkflowProgressSummaryError(f"{name} must use canonical UTC encoding")
    return value


def _normalize_run_identity(
    value: Any,
    *,
    expected_identity: WorkflowRunIdentity | None,
) -> dict[str, Any]:
    identity = _exact_object(value, _RUN_IDENTITY_KEYS, "run_identity")
    if (
        type(identity["schema_version"]) is not int
        or identity["schema_version"] != WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION
    ):
        raise WorkflowProgressSummaryError("run_identity has an unsupported schema version")
    task_execution_pk = _bounded_int(identity["task_execution_pk"], "task_execution_pk")
    attempt_number = _bounded_int(identity["attempt_number"], "attempt_number")
    execution_generation = _bounded_int(
        identity["execution_generation"],
        "execution_generation",
    )
    if task_execution_pk == 0 or attempt_number == 0:
        raise WorkflowProgressSummaryError(
            "run_identity requires positive task and attempt numbers"
        )
    run_id = identity["run_id"]
    if not isinstance(run_id, str):
        raise WorkflowProgressSummaryError("run_id must be a canonical UUID")
    try:
        normalized_run_id = str(UUID(run_id))
    except (ValueError, AttributeError) as error:
        raise WorkflowProgressSummaryError("run_id must be a canonical UUID") from error
    if run_id != normalized_run_id:
        raise WorkflowProgressSummaryError("run_id must be a canonical UUID")

    normalized = {
        "schema_version": WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION,
        "run_id": run_id,
        "task_execution_pk": task_execution_pk,
        "attempt_number": attempt_number,
        "execution_generation": execution_generation,
    }
    if expected_identity is not None and normalized != expected_identity.as_dict():
        raise WorkflowProgressSummaryError("summary identity does not match its workflow run")
    return normalized


def _normalize_counts(
    value: Any,
    keys: frozenset[str],
    name: str,
) -> dict[str, int | None]:
    counts = _exact_object(value, keys, name)
    return {
        key: _bounded_int(counts[key], f"{name}.{key}", nullable=key == "declared")
        for key in sorted(keys)
    }


def _normalize_detail(value: Any, *, detail_revision: int | None) -> dict[str, Any]:
    detail = _exact_object(value, _DETAIL_KEYS, "detail")
    try:
        availability = WorkflowProgressDetailAvailability(detail["availability"])
    except (TypeError, ValueError) as error:
        raise WorkflowProgressSummaryError("detail availability is unsupported") from error
    if not isinstance(detail["complete"], bool):
        raise WorkflowProgressSummaryError("detail completeness must be a boolean")
    reasons = detail["truncation_reasons"]
    if not isinstance(reasons, list) or len(reasons) > len(WorkflowProgressTruncationReason):
        raise WorkflowProgressSummaryError("detail truncation reasons exceed the protocol bound")
    try:
        normalized_reasons = [WorkflowProgressTruncationReason(reason).value for reason in reasons]
    except (TypeError, ValueError) as error:
        raise WorkflowProgressSummaryError("detail truncation reason is unsupported") from error
    if normalized_reasons != sorted(set(normalized_reasons)):
        raise WorkflowProgressSummaryError("detail truncation reasons must be unique and sorted")

    published = availability in {
        WorkflowProgressDetailAvailability.AVAILABLE,
        WorkflowProgressDetailAvailability.TRUNCATED,
        WorkflowProgressDetailAvailability.EXPIRED,
        WorkflowProgressDetailAvailability.MISSING,
        WorkflowProgressDetailAvailability.CORRUPT,
    }
    if published != (detail_revision is not None):
        raise WorkflowProgressSummaryError("detail revision and availability are inconsistent")
    if detail["complete"] != (availability is WorkflowProgressDetailAvailability.AVAILABLE):
        raise WorkflowProgressSummaryError("detail completeness and availability are inconsistent")
    if (availability is WorkflowProgressDetailAvailability.TRUNCATED) != bool(normalized_reasons):
        raise WorkflowProgressSummaryError("truncation reasons require TRUNCATED availability")
    return {
        "availability": availability.value,
        "complete": detail["complete"],
        "truncation_reasons": normalized_reasons,
    }


def _normalize_storage(value: Any, *, topology_version: int | None) -> dict[str, Any]:
    storage = _exact_object(value, _STORAGE_KEYS, "storage")
    if storage["kind"] != "database":
        raise WorkflowProgressSummaryError("storage kind must be database for protocol v1")
    manifest_id = storage["manifest_id"]
    if manifest_id is not None and (
        not isinstance(manifest_id, str) or _MANIFEST_ID_RE.fullmatch(manifest_id) is None
    ):
        raise WorkflowProgressSummaryError("manifest identity must be a bounded opaque identifier")
    if (manifest_id is not None) != (topology_version is not None):
        raise WorkflowProgressSummaryError(
            "manifest identity and topology version are inconsistent"
        )
    return {"kind": "database", "manifest_id": manifest_id}


def _normalize_timestamps(value: Any) -> tuple[dict[str, str | None], tuple[datetime, ...]]:
    timestamps = _exact_object(value, _TIMESTAMP_KEYS, "timestamps")
    started_at = _canonical_utc_timestamp(timestamps["started_at"], "timestamps.started_at")
    updated_at = _canonical_utc_timestamp(timestamps["updated_at"], "timestamps.updated_at")
    finished_at = _canonical_utc_timestamp(
        timestamps["finished_at"],
        "timestamps.finished_at",
        nullable=True,
    )
    parsed = tuple(
        datetime.fromisoformat(item[:-1] + "+00:00")
        for item in (started_at, updated_at, finished_at)
        if item is not None
    )
    if tuple(sorted(parsed)) != parsed:
        raise WorkflowProgressSummaryError("summary timestamps must be monotonic")
    return (
        {"started_at": started_at, "updated_at": updated_at, "finished_at": finished_at},
        parsed,
    )


def _normalize_terminal(value: Any, *, state: str, finished_at: str | None) -> dict[str, Any]:
    terminal = _exact_object(value, _TERMINAL_KEYS, "terminal")
    outcome = terminal["outcome"]
    terminal_finished_at = _canonical_utc_timestamp(
        terminal["finished_at"],
        "terminal.finished_at",
        nullable=True,
    )
    if outcome is None:
        if (
            terminal_finished_at is not None
            or finished_at is not None
            or state in WORKFLOW_PROGRESS_TERMINAL_STATES
        ):
            raise WorkflowProgressSummaryError(
                "terminal metadata is inconsistent with summary state"
            )
    elif (
        not isinstance(outcome, str)
        or outcome not in WORKFLOW_PROGRESS_TERMINAL_STATES
        or outcome != state
        or terminal_finished_at is None
        or terminal_finished_at != finished_at
    ):
        raise WorkflowProgressSummaryError("terminal metadata is inconsistent with summary state")
    return {"outcome": outcome, "finished_at": terminal_finished_at}


def normalize_workflow_progress_summary(
    value: Any,
    *,
    expected_identity: WorkflowRunIdentity | None = None,
) -> dict[str, Any]:
    """Validate and detach one fixed-shape schema-v3 summary."""
    summary = _exact_object(value, _SUMMARY_KEYS, "workflow progress summary")
    if (
        type(summary["schema_version"]) is not int
        or summary["schema_version"] != WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION
    ):
        raise WorkflowProgressSummaryError("workflow progress summary has an unsupported schema")
    if (
        type(summary["storage_protocol_version"]) is not int
        or summary["storage_protocol_version"] != WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION
    ):
        raise WorkflowProgressSummaryError("workflow progress storage protocol is unsupported")

    identity = _normalize_run_identity(summary["run_identity"], expected_identity=expected_identity)
    reporting_policy = summary["reporting_policy"]
    if (
        not isinstance(reporting_policy, str)
        or reporting_policy not in WORKFLOW_PROGRESS_REPORTING_POLICIES
    ):
        raise WorkflowProgressSummaryError("workflow reporting policy is unsupported")
    selected_strategy = _bounded_identifier(
        summary["selected_strategy"],
        "selected_strategy",
        nullable=True,
    )
    plan_fingerprint = summary["plan_fingerprint"]
    if plan_fingerprint is not None and (
        not isinstance(plan_fingerprint, str)
        or _PLAN_FINGERPRINT_RE.fullmatch(plan_fingerprint) is None
    ):
        raise WorkflowProgressSummaryError("plan fingerprint must be a canonical SHA-256 identity")
    limits_profile = _bounded_identifier(summary["limits_profile"], "limits_profile")

    summary_revision = _bounded_int(summary["summary_revision"], "summary_revision")
    topology_version = _bounded_int(
        summary["topology_version"],
        "topology_version",
        nullable=True,
    )
    detail_revision = _bounded_int(
        summary["detail_revision"],
        "detail_revision",
        nullable=True,
    )
    if topology_version == 0 or detail_revision == 0:
        raise WorkflowProgressSummaryError(
            "published topology and detail revisions must be positive"
        )
    state = summary["state"]
    if not isinstance(state, str) or state not in WORKFLOW_PROGRESS_STATES:
        raise WorkflowProgressSummaryError("workflow summary state is unsupported")
    if summary_revision == _MAX_COUNTER and state not in WORKFLOW_PROGRESS_TERMINAL_STATES:
        raise WorkflowProgressSummaryError(
            "nonterminal summary revision must reserve the terminal transition"
        )

    node_counts = _normalize_counts(summary["node_counts"], _NODE_COUNT_KEYS, "node_counts")
    edge_counts = _normalize_counts(summary["edge_counts"], _EDGE_COUNT_KEYS, "edge_counts")
    retained_topology_nodes = cast(int, node_counts["retained_topology"])
    retained_detail_nodes = cast(int, node_counts["retained_detail"])
    discovered_nodes = cast(int, node_counts["discovered"])
    retained_topology_edges = cast(int, edge_counts["retained_topology"])
    discovered_edges = cast(int, edge_counts["discovered"])
    declared_nodes = node_counts["declared"]
    declared_edges = edge_counts["declared"]
    if declared_nodes is not None and discovered_nodes > declared_nodes:
        raise WorkflowProgressSummaryError("discovered node count exceeds declared nodes")
    if declared_edges is not None and discovered_edges > declared_edges:
        raise WorkflowProgressSummaryError("discovered edge count exceeds declared edges")
    if retained_topology_nodes > discovered_nodes:
        raise WorkflowProgressSummaryError("retained topology node count exceeds discovered nodes")
    if retained_detail_nodes > retained_topology_nodes:
        raise WorkflowProgressSummaryError("retained detail node count exceeds retained topology")
    if retained_topology_edges > discovered_edges:
        raise WorkflowProgressSummaryError("retained topology edge count exceeds discovered edges")
    if topology_version is None and (retained_topology_nodes or retained_topology_edges):
        raise WorkflowProgressSummaryError(
            "retained topology counts require a published topology version"
        )
    if detail_revision is None and retained_detail_nodes:
        raise WorkflowProgressSummaryError(
            "retained detail counts require a published detail revision"
        )
    state_total = sum(
        cast(int, node_counts[key]) for key in ("pending", "running", "succeeded", "failed")
    )
    if state_total != discovered_nodes:
        raise WorkflowProgressSummaryError("node state counts must equal discovered nodes")

    progress_percent = summary["progress_percent"]
    if not isinstance(progress_percent, (int, float)) or isinstance(progress_percent, bool):
        raise WorkflowProgressSummaryError(
            "progress_percent must be finite and between zero and 100"
        )
    if isinstance(progress_percent, float) and not math.isfinite(progress_percent):
        raise WorkflowProgressSummaryError(
            "progress_percent must be finite and between zero and 100"
        )
    if not 0 <= progress_percent <= 100:
        raise WorkflowProgressSummaryError(
            "progress_percent must be finite and between zero and 100"
        )
    progress_percent = float(progress_percent)
    if state == "SUCCEEDED" and (
        cast(int, node_counts["pending"]) != 0
        or cast(int, node_counts["running"]) != 0
        or cast(int, node_counts["failed"]) != 0
        or node_counts["succeeded"] != node_counts["discovered"]
        or progress_percent != 100.0
    ):
        raise WorkflowProgressSummaryError(
            "successful workflow summary must report every discovered node succeeded"
        )

    timestamps, _ = _normalize_timestamps(summary["timestamps"])
    detail = _normalize_detail(summary["detail"], detail_revision=detail_revision)
    availability = detail["availability"]
    if (reporting_policy == "disabled") != (
        availability == WorkflowProgressDetailAvailability.DISABLED
    ):
        raise WorkflowProgressSummaryError(
            "workflow reporting policy and detail availability are inconsistent"
        )
    if (
        availability == WorkflowProgressDetailAvailability.OMITTED_BY_POLICY
        and reporting_policy not in {"sampled", "terminal_only"}
    ):
        raise WorkflowProgressSummaryError(
            "workflow reporting policy and detail availability are inconsistent"
        )
    storage = _normalize_storage(summary["storage"], topology_version=topology_version)
    if (
        detail["availability"] == WorkflowProgressDetailAvailability.EXPIRED
        and state not in WORKFLOW_PROGRESS_TERMINAL_STATES
    ):
        raise WorkflowProgressSummaryError(
            "expired workflow detail requires a terminal summary state"
        )
    if detail_revision is not None and topology_version is None:
        raise WorkflowProgressSummaryError("published detail requires a topology version")
    if (
        detail["availability"] == WorkflowProgressDetailAvailability.AVAILABLE
        and node_counts["retained_detail"] != node_counts["discovered"]
    ):
        raise WorkflowProgressSummaryError("available detail must retain every discovered node")

    retention = _exact_object(summary["retention"], _RETENTION_KEYS, "retention")
    detail_days = _bounded_int(retention["detail_days"], "retention.detail_days")
    if detail_days > WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS:
        raise WorkflowProgressSummaryError("detail retention exceeds the protocol range")
    detail_expires_at = _canonical_utc_timestamp(
        retention["detail_expires_at"],
        "retention.detail_expires_at",
        nullable=True,
        retention_expiry=True,
    )
    if detail_revision is None and detail_expires_at is not None:
        raise WorkflowProgressSummaryError("detail expiration requires a published detail revision")
    terminal = _normalize_terminal(
        summary["terminal"],
        state=state,
        finished_at=timestamps["finished_at"],
    )
    terminal_state_unreported = (
        WorkflowProgressTruncationReason.TERMINAL_STATE_UNREPORTED.value
        in detail["truncation_reasons"]
    )
    if terminal_state_unreported and not (
        state == "SUCCEEDED"
        and detail_revision is not None
        and availability == WorkflowProgressDetailAvailability.TRUNCATED
        and terminal["outcome"] == "SUCCEEDED"
    ):
        raise WorkflowProgressSummaryError(
            "unreported terminal node state requires truncated successful detail"
        )
    if detail_revision is not None and state in WORKFLOW_PROGRESS_TERMINAL_STATES:
        finished = datetime.fromisoformat(cast(str, terminal["finished_at"])[:-1] + "+00:00")
        expected_expiry = (
            (finished + timedelta(days=detail_days)).isoformat().replace("+00:00", "Z")
        )
        if detail_expires_at != expected_expiry:
            raise WorkflowProgressSummaryError(
                "terminal published detail expiration must match its retention policy"
            )
    elif detail_expires_at is not None:
        raise WorkflowProgressSummaryError(
            "active workflow detail cannot have a terminal expiration"
        )

    normalized = {
        "schema_version": WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
        "storage_protocol_version": WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
        "run_identity": identity,
        "reporting_policy": reporting_policy,
        "selected_strategy": selected_strategy,
        "plan_fingerprint": plan_fingerprint,
        "limits_profile": limits_profile,
        "summary_revision": summary_revision,
        "topology_version": topology_version,
        "detail_revision": detail_revision,
        "state": state,
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "progress_percent": progress_percent,
        "timestamps": timestamps,
        "detail": detail,
        "storage": storage,
        "retention": {
            "detail_days": detail_days,
            "detail_expires_at": detail_expires_at,
        },
        "terminal": terminal,
    }
    serialized = _canonical_json(normalized)
    if _utf8_length(serialized, "workflow progress summary") > WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES:
        raise WorkflowProgressSummaryError(
            "workflow progress summary exceeds the 16 KiB byte limit"
        )
    return normalized


def _canonical_json(value: dict[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise WorkflowProgressSummaryError(
            "workflow progress summary is not canonical JSON"
        ) from error


def serialize_workflow_progress_summary(
    value: Any,
    *,
    expected_identity: WorkflowRunIdentity | None = None,
) -> str:
    """Return the canonical bounded JSON representation for one summary."""
    return _canonical_json(
        normalize_workflow_progress_summary(value, expected_identity=expected_identity)
    )


def deserialize_workflow_progress_summary(
    serialized: Any,
    *,
    expected_identity: WorkflowRunIdentity | None = None,
) -> dict[str, Any]:
    """Decode one summary only after enforcing its UTF-8 byte boundary."""
    if not isinstance(serialized, str):
        raise WorkflowProgressSummaryError("workflow progress summary must be JSON text")
    if _utf8_length(serialized, "workflow progress summary") > WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES:
        raise WorkflowProgressSummaryError(
            "workflow progress summary exceeds the 16 KiB byte limit"
        )
    try:
        value = json.loads(serialized)
    except (ValueError, RecursionError) as error:
        raise WorkflowProgressSummaryError(
            "workflow progress summary contains invalid JSON"
        ) from error
    return normalize_workflow_progress_summary(value, expected_identity=expected_identity)


def public_workflow_progress_summary(value: dict[str, Any]) -> dict[str, Any]:
    """Return a detached summary without database or manifest identifiers."""
    public = deepcopy(value)
    public["run_identity"].pop("task_execution_pk", None)
    public["storage"]["manifest_id"] = None
    return public


__all__ = [
    "WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS",
    "WORKFLOW_PROGRESS_LEGACY_MAX_BYTES",
    "WORKFLOW_PROGRESS_REPORTING_POLICIES",
    "WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION",
    "WORKFLOW_PROGRESS_SUMMARY_LIMITS_PROFILE",
    "WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES",
    "WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION",
    "WORKFLOW_PROGRESS_TERMINAL_STATES",
    "WorkflowProgressDetailAvailability",
    "WorkflowProgressSummaryError",
    "WorkflowProgressTruncationReason",
    "deserialize_workflow_progress_summary",
    "normalize_workflow_progress_summary",
    "public_workflow_progress_summary",
    "serialize_workflow_progress_summary",
    "workflow_progress_detail_is_last_observed",
]

preserve_legacy_module_identity(
    globals(),
    exports=__all__,
    legacy_module="django_ray.workflow_progress_summary",
)
