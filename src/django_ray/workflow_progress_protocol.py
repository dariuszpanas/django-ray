"""Canonical bounded wire protocol for live workflow-progress events.

This module is deliberately independent from Django models and database
machinery. Producers normalize, redact, and bound an event here before invoking
Ray; the collector decodes the same canonical bytes and revalidates the complete
workflow-run fence before mutating state.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from django_ray.redaction import REDACTED, redact_text
from django_ray.workflow_progress_limits import (
    WORKFLOW_PROGRESS_LIMITS_PROFILE,
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressLimits,
)

WORKFLOW_PROGRESS_EVENT_SCHEMA_VERSION = 1
WORKFLOW_PROGRESS_EVENT_ENCODING = "identity"

_ENVELOPE_KEYS = frozenset(
    {
        "encoding",
        "kind",
        "limits_profile",
        "occurred_at",
        "payload",
        "run_identity",
        "schema_version",
        "truncated",
    }
)
_RUN_IDENTITY_KEYS = frozenset(
    {
        "attempt_number",
        "execution_generation",
        "run_id",
        "schema_version",
        "task_execution_pk",
    }
)
_PLAN_SUMMARY_KEYS = frozenset(
    {
        "definition_name",
        "definition_revision",
        "fingerprint",
        "node_count",
        "plan_format",
        "plan_format_version",
        "topology_class",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "assigned_resources",
        "ray_job_id",
        "ray_node_id",
        "ray_task_id",
        "ray_worker_id",
    }
)
_OMITTED_OVERSIZED = "<omitted:oversized>"
_PROTOCOL_ERROR_REASONS = frozenset(
    {
        "fence_mismatch",
        "limit_exceeded",
        "protocol_error",
    }
)

WorkflowProgressProtocolReason = Literal[
    "fence_mismatch",
    "limit_exceeded",
    "protocol_error",
]


class WorkflowProgressEventKind(StrEnum):
    """Supported protocol-v1 workflow-progress mutations."""

    INITIALIZED = "initialized"
    NODE_REGISTERED = "node_registered"
    EDGES_REGISTERED = "edges_registered"
    MAP_REGISTERED = "map_registered"
    SUBMITTED = "submitted"
    STARTED = "started"
    APPLICATION_PROGRESS = "application_progress"
    MAP_PROGRESS = "map_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowProgressProtocolError(ValueError):
    """Raised before an invalid event can cross or mutate the live boundary."""

    def __init__(
        self,
        message: str,
        *,
        reason: WorkflowProgressProtocolReason = "protocol_error",
    ) -> None:
        if reason not in _PROTOCOL_ERROR_REASONS:
            raise ValueError("workflow progress protocol reason is unsupported")
        super().__init__(message)
        self.reason = reason


class WorkflowProgressProtocolLimitError(WorkflowProgressProtocolError):
    """Raised when one event exceeds an immutable protocol-v1 limit."""

    def __init__(
        self,
        message: str,
        *,
        reason: WorkflowProgressProtocolReason = "limit_exceeded",
    ) -> None:
        super().__init__(message, reason=reason)


@dataclass(frozen=True)
class WorkflowProgressEvent:
    """One detached canonical event accepted by the live collector."""

    kind: WorkflowProgressEventKind
    run_identity: dict[str, Any]
    occurred_at: str
    payload: dict[str, Any]
    truncated: bool


@dataclass
class _MetadataBudget:
    remaining: int

    def consume(self, amount: int, name: str) -> None:
        if amount < 0 or amount > self.remaining:
            raise WorkflowProgressProtocolLimitError(f"{name} exceeds the metadata byte budget")
        self.remaining -= amount


class _DuplicateKeyError(ValueError):
    pass


def canonical_workflow_progress_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON without non-finite number extensions."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise WorkflowProgressProtocolError(
            "workflow progress value is not canonical JSON"
        ) from error


def _exact_mapping(value: Any, keys: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkflowProgressProtocolError(f"{name} must contain the exact protocol fields")
    return value


def _utf8_bytes(value: Any, name: str) -> bytes:
    if not isinstance(value, str):
        raise WorkflowProgressProtocolError(f"{name} must be text")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorkflowProgressProtocolError(f"{name} must contain valid UTF-8") from error


def _bounded_identity_text(
    value: Any,
    name: str,
    *,
    maximum: int,
    nullable: bool = False,
) -> str | None:
    if nullable and value is None:
        return None
    encoded = _utf8_bytes(value, name)
    if not encoded:
        raise WorkflowProgressProtocolError(f"{name} cannot be empty")
    if len(encoded) > maximum:
        raise WorkflowProgressProtocolLimitError(f"{name} exceeds {maximum} UTF-8 bytes")
    if redact_text(value) == REDACTED:
        raise WorkflowProgressProtocolError(f"{name} resembles sensitive data")
    return value


def _bounded_redacted_text(
    value: Any,
    name: str,
    *,
    maximum: int,
    nullable: bool = False,
) -> tuple[str | None, bool]:
    if nullable and value is None:
        return None, False
    encoded = _utf8_bytes(value, name)
    if len(encoded) > maximum:
        return _OMITTED_OVERSIZED, True
    normalized = redact_text(value)
    normalized_bytes = _utf8_bytes(normalized, name)
    if len(normalized_bytes) > maximum:
        return _OMITTED_OVERSIZED, True
    return normalized, normalized != value


def _bounded_int(
    value: Any,
    name: str,
    *,
    limits: WorkflowProgressLimits,
    minimum: int = 0,
    nullable: bool = False,
) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or value < minimum or value > limits.identity_max_integer:
        raise WorkflowProgressProtocolError(f"{name} must be an integer in the durable range")
    return value


def _finite_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WorkflowProgressProtocolError(f"{name} must be a finite number")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise WorkflowProgressProtocolError(f"{name} must be a finite number") from error
    if not math.isfinite(normalized) or (minimum is not None and normalized < minimum):
        raise WorkflowProgressProtocolError(f"{name} must be a finite number")
    return normalized


def _normalize_run_identity(
    value: Any,
    *,
    limits: WorkflowProgressLimits,
) -> dict[str, Any]:
    identity = _exact_mapping(value, _RUN_IDENTITY_KEYS, "run_identity")
    if type(identity["schema_version"]) is not int or identity["schema_version"] != 1:
        raise WorkflowProgressProtocolError("run_identity schema_version is unsupported")
    task_execution_pk = _bounded_int(
        identity["task_execution_pk"],
        "run_identity.task_execution_pk",
        limits=limits,
        minimum=1,
    )
    attempt_number = _bounded_int(
        identity["attempt_number"],
        "run_identity.attempt_number",
        limits=limits,
        minimum=1,
    )
    execution_generation = _bounded_int(
        identity["execution_generation"],
        "run_identity.execution_generation",
        limits=limits,
    )
    run_id = identity["run_id"]
    if not isinstance(run_id, str):
        raise WorkflowProgressProtocolError("run_identity.run_id must be a canonical UUID")
    try:
        if str(UUID(run_id)) != run_id:
            raise WorkflowProgressProtocolError("run_identity.run_id must be a canonical UUID")
    except (AttributeError, ValueError) as error:
        raise WorkflowProgressProtocolError(
            "run_identity.run_id must be a canonical UUID"
        ) from error
    return {
        "attempt_number": attempt_number,
        "execution_generation": execution_generation,
        "run_id": run_id,
        "schema_version": 1,
        "task_execution_pk": task_execution_pk,
    }


def _normalize_timestamp(value: Any, *, producer: bool) -> str:
    if producer and value is None:
        value = datetime.now(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkflowProgressProtocolError("occurred_at must be timezone-aware")
        value = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 32:
        raise WorkflowProgressProtocolError("occurred_at must be a bounded canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(UTC)
    except ValueError as error:
        raise WorkflowProgressProtocolError(
            "occurred_at must be a bounded canonical UTC timestamp"
        ) from error
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise WorkflowProgressProtocolError("occurred_at must use canonical UTC encoding")
    return canonical


def _normalize_metadata(
    value: Any,
    name: str,
    *,
    limits: WorkflowProgressLimits,
    depth: int = 0,
    budget: _MetadataBudget | None = None,
) -> tuple[Any, bool]:
    if budget is None:
        budget = _MetadataBudget(limits.record_max_encoded_bytes)
    if depth > limits.value_max_depth:
        raise WorkflowProgressProtocolLimitError(f"{name} exceeds the metadata nesting limit")
    if value is None:
        budget.consume(4, name)
        return None, False
    if isinstance(value, bool):
        budget.consume(5, name)
        return value, False
    if isinstance(value, int):
        if not -(1 << 63) <= value <= limits.identity_max_integer:
            raise WorkflowProgressProtocolError(f"{name} integer is outside the durable range")
        budget.consume(24, name)
        return value, False
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowProgressProtocolError(f"{name} must contain finite numbers")
        budget.consume(24, name)
        return value, False
    if isinstance(value, str):
        encoded = _utf8_bytes(value, name)
        if len(encoded) > limits.record_max_encoded_bytes:
            budget.consume(len(_OMITTED_OVERSIZED) + 2, name)
            return _OMITTED_OVERSIZED, True
        normalized = redact_text(value)
        normalized_bytes = _utf8_bytes(normalized, name)
        budget.consume(len(normalized_bytes) + 2, name)
        return normalized, normalized != value
    if isinstance(value, Mapping):
        budget.consume(2, name)
        normalized_mapping: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowProgressProtocolError(f"{name} object keys must be text")
            key_bytes = _utf8_bytes(key, f"{name} key")
            if not key_bytes or len(key_bytes) > limits.record_max_encoded_bytes:
                raise WorkflowProgressProtocolLimitError(f"{name} contains an oversized key")
            budget.consume(len(key_bytes) + 4, name)
            if redact_text(key) == REDACTED:
                truncated = True
                continue
            normalized_item, item_truncated = _normalize_metadata(
                item,
                f"{name}.{key}",
                limits=limits,
                depth=depth + 1,
                budget=budget,
            )
            normalized_mapping[key] = normalized_item
            truncated = truncated or item_truncated
        return normalized_mapping, truncated
    if isinstance(value, list | tuple):
        budget.consume(2, name)
        normalized_items: list[Any] = []
        truncated = False
        for index, item in enumerate(value):
            budget.consume(1, name)
            normalized_item, item_truncated = _normalize_metadata(
                item,
                f"{name}[{index}]",
                limits=limits,
                depth=depth + 1,
                budget=budget,
            )
            normalized_items.append(normalized_item)
            truncated = truncated or item_truncated
        return normalized_items, truncated
    raise WorkflowProgressProtocolError(f"{name} must contain only JSON-compatible values")


def _normalize_metadata_object(
    value: Any,
    name: str,
    *,
    limits: WorkflowProgressLimits,
) -> tuple[dict[str, Any], bool]:
    normalized, truncated = _normalize_metadata(value, name, limits=limits)
    if not isinstance(normalized, dict):
        raise WorkflowProgressProtocolError(f"{name} must be an object")
    return normalized, truncated


def _bounded_sha256(value: Any, name: str) -> str:
    normalized = _bounded_identity_text(value, name, maximum=71)
    if (
        normalized is None
        or len(normalized) != 71
        or not normalized.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in normalized[7:])
    ):
        raise WorkflowProgressProtocolError(f"{name} must be a canonical SHA-256 identity")
    return normalized


def _normalize_plan_summary(
    value: Any,
    *,
    limits: WorkflowProgressLimits,
) -> tuple[dict[str, Any], bool]:
    plan = _exact_mapping(value, _PLAN_SUMMARY_KEYS, "initialized.plan")
    if plan["plan_format"] != "django-ray.workflow-plan":
        raise WorkflowProgressProtocolError("initialized.plan plan_format is unsupported")
    if type(plan["plan_format_version"]) is not int or plan["plan_format_version"] != 1:
        raise WorkflowProgressProtocolError("initialized.plan plan_format_version is unsupported")
    definition_name_bytes = _utf8_bytes(
        plan["definition_name"],
        "initialized.plan.definition_name",
    )
    topology_class_bytes = _utf8_bytes(
        plan["topology_class"],
        "initialized.plan.topology_class",
    )
    if not definition_name_bytes or not topology_class_bytes:
        raise WorkflowProgressProtocolError(
            "initialized.plan descriptive identities cannot be empty"
        )
    definition_name, definition_name_truncated = _bounded_redacted_text(
        plan["definition_name"],
        "initialized.plan.definition_name",
        maximum=limits.label_max_bytes,
    )
    topology_class, topology_class_truncated = _bounded_redacted_text(
        plan["topology_class"],
        "initialized.plan.topology_class",
        maximum=limits.node_id_max_bytes,
    )
    node_count = _bounded_int(
        plan["node_count"],
        "initialized.plan.node_count",
        limits=limits,
    )
    if node_count is None:
        raise AssertionError("non-null initialized plan node_count normalized to None")
    return (
        {
            "definition_name": definition_name,
            "definition_revision": _bounded_sha256(
                plan["definition_revision"],
                "initialized.plan.definition_revision",
            ),
            "fingerprint": _bounded_sha256(
                plan["fingerprint"],
                "initialized.plan.fingerprint",
            ),
            "node_count": node_count,
            "plan_format": "django-ray.workflow-plan",
            "plan_format_version": 1,
            "topology_class": topology_class,
        },
        definition_name_truncated or topology_class_truncated,
    )


def _normalize_metrics(
    value: Any,
    name: str,
    *,
    limits: WorkflowProgressLimits,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, Mapping) or len(value) > limits.metrics_max_items:
        raise WorkflowProgressProtocolLimitError(f"{name} exceeds the metrics item limit")
    keys = list(value)
    if any(not isinstance(key, str) for key in keys):
        raise WorkflowProgressProtocolError(f"{name} keys must be text")
    normalized: dict[str, Any] = {}
    truncated = False
    for key in sorted(keys):
        key_bytes = _utf8_bytes(key, f"{name} key")
        if not key_bytes or len(key_bytes) > limits.metric_key_max_bytes:
            raise WorkflowProgressProtocolLimitError(f"{name} contains an oversized key")
        if redact_text(key) == REDACTED:
            truncated = True
            continue
        item = value[key]
        if item is None or isinstance(item, bool):
            normalized[key] = item
        elif isinstance(item, int):
            if not -(1 << 63) <= item <= limits.identity_max_integer:
                raise WorkflowProgressProtocolError(
                    f"{name}.{key} integer is outside the durable range"
                )
            normalized[key] = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise WorkflowProgressProtocolError(f"{name}.{key} must be finite")
            normalized[key] = item
        elif isinstance(item, str):
            encoded = _utf8_bytes(item, f"{name}.{key}")
            if len(encoded) > limits.metric_string_max_bytes:
                normalized[key] = _OMITTED_OVERSIZED
                truncated = True
            else:
                redacted = redact_text(item)
                normalized[key] = redacted
                truncated = truncated or redacted != item
        else:
            raise WorkflowProgressProtocolError(f"{name}.{key} must be a scalar")
    encoded = canonical_workflow_progress_json_bytes(normalized)
    if len(encoded) <= limits.metrics_max_encoded_bytes:
        return normalized, truncated
    return {"_omitted": f"sha256:{hashlib.sha256(encoded).hexdigest()}"}, True


def _normalize_execution(
    value: Any,
    *,
    limits: WorkflowProgressLimits,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value) <= _EXECUTION_KEYS:
        raise WorkflowProgressProtocolError("started.execution contains unsupported fields")
    assigned_value = value.get("assigned_resources", {})
    if not isinstance(assigned_value, Mapping) or len(assigned_value) > limits.metrics_max_items:
        raise WorkflowProgressProtocolLimitError(
            "started.execution assigned_resources exceeds its item limit"
        )
    assigned: dict[str, float] = {}
    for key in sorted(assigned_value):
        key_bytes = _utf8_bytes(key, "assigned resource key")
        if not key_bytes or len(key_bytes) > limits.metric_key_max_bytes:
            raise WorkflowProgressProtocolLimitError(
                "started.execution contains an oversized resource key"
            )
        if redact_text(key) == REDACTED:
            raise WorkflowProgressProtocolError(
                "started.execution contains a sensitive-looking resource key"
            )
        assigned[key] = _finite_number(
            assigned_value[key],
            f"started.execution.assigned_resources.{key}",
            minimum=0.0,
        )
    if len(canonical_workflow_progress_json_bytes(assigned)) > limits.metrics_max_encoded_bytes:
        raise WorkflowProgressProtocolLimitError(
            "started.execution assigned_resources exceeds its byte limit"
        )
    normalized: dict[str, Any] = {
        "assigned_resources": assigned,
    }
    for name in ("ray_job_id", "ray_node_id", "ray_task_id", "ray_worker_id"):
        normalized[name] = _bounded_identity_text(
            value.get(name),
            f"started.execution.{name}",
            maximum=limits.node_id_max_bytes,
            nullable=True,
        )
    return normalized


def _payload_mapping(
    value: Any,
    keys: frozenset[str],
    kind: WorkflowProgressEventKind,
) -> Mapping[str, Any]:
    return _exact_mapping(value, keys, f"{kind.value} payload")


def _normalize_payload(
    kind: WorkflowProgressEventKind,
    value: Any,
    *,
    limits: WorkflowProgressLimits,
) -> tuple[dict[str, Any], bool]:
    truncated = False

    if kind is WorkflowProgressEventKind.INITIALIZED:
        payload = _payload_mapping(value, frozenset({"plan"}), kind)
        plan, truncated = _normalize_plan_summary(
            payload["plan"],
            limits=limits,
        )
        normalized = {"plan": plan}
    elif kind is WorkflowProgressEventKind.NODE_REGISTERED:
        payload = _payload_mapping(
            value,
            frozenset(
                {
                    "callable_path",
                    "label",
                    "node_id",
                    "ray_options",
                    "runtime_env",
                }
            ),
            kind,
        )
        node_id = _bounded_identity_text(
            payload["node_id"],
            "node_registered.node_id",
            maximum=limits.node_id_max_bytes,
        )
        label, label_truncated = _bounded_redacted_text(
            payload["label"],
            "node_registered.label",
            maximum=limits.label_max_bytes,
        )
        callable_path, path_truncated = _bounded_redacted_text(
            payload["callable_path"],
            "node_registered.callable_path",
            maximum=limits.label_max_bytes,
            nullable=True,
        )
        runtime_env, runtime_truncated = _normalize_metadata_object(
            payload["runtime_env"],
            "node_registered.runtime_env",
            limits=limits,
        )
        ray_options, options_truncated = _normalize_metadata_object(
            payload["ray_options"],
            "node_registered.ray_options",
            limits=limits,
        )
        normalized = {
            "callable_path": callable_path,
            "label": label,
            "node_id": node_id,
            "ray_options": ray_options,
            "runtime_env": runtime_env,
        }
        truncated = label_truncated or path_truncated or runtime_truncated or options_truncated
    elif kind is WorkflowProgressEventKind.EDGES_REGISTERED:
        payload = _payload_mapping(value, frozenset({"edges"}), kind)
        edge_values = payload["edges"]
        if (
            not isinstance(edge_values, list | tuple)
            or not edge_values
            or len(edge_values) > limits.edge_batch_max_items
        ):
            raise WorkflowProgressProtocolLimitError(
                "edges_registered.edges must contain one bounded batch"
            )
        edges: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for edge_value in edge_values:
            edge = _exact_mapping(
                edge_value,
                frozenset({"source", "target"}),
                "workflow progress edge",
            )
            source = _bounded_identity_text(
                edge["source"],
                "workflow progress edge source",
                maximum=limits.node_id_max_bytes,
            )
            target = _bounded_identity_text(
                edge["target"],
                "workflow progress edge target",
                maximum=limits.node_id_max_bytes,
            )
            pair = (source, target)
            if pair in seen:
                raise WorkflowProgressProtocolError("edges_registered.edges contains a duplicate")
            seen.add(pair)
            edges.append({"source": source, "target": target})
        normalized = {"edges": edges}
    elif kind is WorkflowProgressEventKind.MAP_REGISTERED:
        payload = _payload_mapping(
            value,
            frozenset({"label", "max_concurrency", "max_items", "node_id"}),
            kind,
        )
        node_id = _bounded_identity_text(
            payload["node_id"],
            "map_registered.node_id",
            maximum=limits.node_id_max_bytes,
        )
        label, truncated = _bounded_redacted_text(
            payload["label"],
            "map_registered.label",
            maximum=limits.label_max_bytes,
        )
        normalized = {
            "label": label,
            "max_concurrency": _bounded_int(
                payload["max_concurrency"],
                "map_registered.max_concurrency",
                limits=limits,
                minimum=1,
                nullable=True,
            ),
            "max_items": _bounded_int(
                payload["max_items"],
                "map_registered.max_items",
                limits=limits,
                minimum=1,
                nullable=True,
            ),
            "node_id": node_id,
        }
    elif kind is WorkflowProgressEventKind.SUBMITTED:
        payload = _payload_mapping(value, frozenset({"label", "node_id", "ray_task_id"}), kind)
        label, truncated = _bounded_redacted_text(
            payload["label"],
            "submitted.label",
            maximum=limits.label_max_bytes,
        )
        normalized = {
            "label": label,
            "node_id": _bounded_identity_text(
                payload["node_id"],
                "submitted.node_id",
                maximum=limits.node_id_max_bytes,
            ),
            "ray_task_id": _bounded_identity_text(
                payload["ray_task_id"],
                "submitted.ray_task_id",
                maximum=limits.node_id_max_bytes,
            ),
        }
    elif kind is WorkflowProgressEventKind.STARTED:
        payload = _payload_mapping(value, frozenset({"execution", "label", "node_id"}), kind)
        label, truncated = _bounded_redacted_text(
            payload["label"],
            "started.label",
            maximum=limits.label_max_bytes,
        )
        normalized = {
            "execution": _normalize_execution(
                payload["execution"],
                limits=limits,
            ),
            "label": label,
            "node_id": _bounded_identity_text(
                payload["node_id"],
                "started.node_id",
                maximum=limits.node_id_max_bytes,
            ),
        }
    elif kind is WorkflowProgressEventKind.APPLICATION_PROGRESS:
        payload = _payload_mapping(
            value,
            frozenset({"current", "message", "metrics", "node_id", "total"}),
            kind,
        )
        current = _finite_number(
            payload["current"],
            "application_progress.current",
            minimum=0.0,
        )
        total = _finite_number(
            payload["total"],
            "application_progress.total",
            minimum=0.0,
        )
        if total <= 0.0 or current > total:
            raise WorkflowProgressProtocolError("application_progress counters are inconsistent")
        message, message_truncated = _bounded_redacted_text(
            payload["message"],
            "application_progress.message",
            maximum=limits.message_max_bytes,
            nullable=True,
        )
        metrics, metrics_truncated = _normalize_metrics(
            payload["metrics"],
            "application_progress.metrics",
            limits=limits,
        )
        normalized = {
            "current": current,
            "message": message,
            "metrics": metrics,
            "node_id": _bounded_identity_text(
                payload["node_id"],
                "application_progress.node_id",
                maximum=limits.node_id_max_bytes,
            ),
            "total": total,
        }
        truncated = message_truncated or metrics_truncated
    elif kind is WorkflowProgressEventKind.MAP_PROGRESS:
        payload = _payload_mapping(
            value,
            frozenset(
                {
                    "completed",
                    "input_exhausted",
                    "label",
                    "node_id",
                    "submitted",
                }
            ),
            kind,
        )
        submitted = _bounded_int(
            payload["submitted"],
            "map_progress.submitted",
            limits=limits,
        )
        completed = _bounded_int(
            payload["completed"],
            "map_progress.completed",
            limits=limits,
        )
        if submitted is None or completed is None:
            raise AssertionError("non-null map progress counter normalized to None")
        if completed > submitted:
            raise WorkflowProgressProtocolError("map_progress completed exceeds submitted")
        if not isinstance(payload["input_exhausted"], bool):
            raise WorkflowProgressProtocolError("map_progress.input_exhausted must be a boolean")
        label, truncated = _bounded_redacted_text(
            payload["label"],
            "map_progress.label",
            maximum=limits.label_max_bytes,
        )
        normalized = {
            "completed": completed,
            "input_exhausted": payload["input_exhausted"],
            "label": label,
            "node_id": _bounded_identity_text(
                payload["node_id"],
                "map_progress.node_id",
                maximum=limits.node_id_max_bytes,
            ),
            "submitted": submitted,
        }
    elif kind is WorkflowProgressEventKind.COMPLETED:
        payload = _payload_mapping(value, frozenset({"label", "node_id"}), kind)
        label, truncated = _bounded_redacted_text(
            payload["label"],
            "completed.label",
            maximum=limits.label_max_bytes,
        )
        normalized = {
            "label": label,
            "node_id": _bounded_identity_text(
                payload["node_id"],
                "completed.node_id",
                maximum=limits.node_id_max_bytes,
            ),
        }
    elif kind is WorkflowProgressEventKind.FAILED:
        payload = _payload_mapping(value, frozenset({"error", "label", "node_id"}), kind)
        error, error_truncated = _bounded_redacted_text(
            payload["error"],
            "failed.error",
            maximum=limits.message_max_bytes,
        )
        label, label_truncated = _bounded_redacted_text(
            payload["label"],
            "failed.label",
            maximum=limits.label_max_bytes,
        )
        normalized = {
            "error": error,
            "label": label,
            "node_id": _bounded_identity_text(
                payload["node_id"],
                "failed.node_id",
                maximum=limits.node_id_max_bytes,
            ),
        }
        truncated = error_truncated or label_truncated
    else:  # pragma: no cover - enum exhaustiveness guard
        raise WorkflowProgressProtocolError("workflow progress event kind is unsupported")

    payload_bytes = canonical_workflow_progress_json_bytes(normalized)
    if len(payload_bytes) > limits.event_payload_max_bytes:
        raise WorkflowProgressProtocolLimitError(
            "workflow progress event payload exceeds its byte limit"
        )
    return normalized, truncated


def _event_kind(value: Any) -> WorkflowProgressEventKind:
    if not isinstance(value, str):
        raise WorkflowProgressProtocolError("workflow progress event kind must be text")
    try:
        return WorkflowProgressEventKind(value)
    except ValueError as error:
        raise WorkflowProgressProtocolError(
            "workflow progress event kind is unsupported"
        ) from error


def _canonical_envelope(
    *,
    identity: dict[str, Any],
    kind: WorkflowProgressEventKind,
    occurred_at: str,
    payload: dict[str, Any],
    truncated: bool,
) -> dict[str, Any]:
    return {
        "encoding": WORKFLOW_PROGRESS_EVENT_ENCODING,
        "kind": kind.value,
        "limits_profile": WORKFLOW_PROGRESS_LIMITS_PROFILE,
        "occurred_at": occurred_at,
        "payload": payload,
        "run_identity": identity,
        "schema_version": WORKFLOW_PROGRESS_EVENT_SCHEMA_VERSION,
        "truncated": truncated,
    }


def prepare_workflow_progress_event(
    run_identity: Mapping[str, Any],
    kind: WorkflowProgressEventKind | str,
    payload: Mapping[str, Any],
    *,
    occurred_at: datetime | None = None,
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
) -> bytes:
    """Normalize and bound one event before any Ray invocation."""
    normalized_identity = _normalize_run_identity(run_identity, limits=limits)
    normalized_kind = _event_kind(kind)
    normalized_payload, truncated = _normalize_payload(
        normalized_kind,
        payload,
        limits=limits,
    )
    timestamp = _normalize_timestamp(occurred_at, producer=True)
    envelope = _canonical_envelope(
        identity=normalized_identity,
        kind=normalized_kind,
        occurred_at=timestamp,
        payload=normalized_payload,
        truncated=truncated,
    )
    wire = canonical_workflow_progress_json_bytes(envelope)
    if len(wire) > limits.event_decoded_max_bytes:
        raise WorkflowProgressProtocolLimitError(
            "workflow progress event exceeds its decoded byte limit"
        )
    if len(wire) > limits.event_wire_max_bytes:
        raise WorkflowProgressProtocolLimitError(
            "workflow progress event exceeds its wire byte limit"
        )
    return wire


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError(key)
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"unsupported JSON constant {value}")


def _decode_json(wire: bytes) -> Any:
    try:
        text = wire.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowProgressProtocolError("workflow progress event must be UTF-8 JSON") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        _DuplicateKeyError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        raise WorkflowProgressProtocolError(
            "workflow progress event contains invalid JSON"
        ) from error


def decode_workflow_progress_event(
    wire: bytes,
    *,
    expected_run_identity: Mapping[str, Any] | None = None,
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
) -> WorkflowProgressEvent:
    """Decode canonical identity JSON after enforcing wire and run fences."""
    if type(wire) is not bytes:
        raise WorkflowProgressProtocolError("workflow progress event must use exact bytes")
    if len(wire) > limits.event_wire_max_bytes:
        raise WorkflowProgressProtocolLimitError(
            "workflow progress event exceeds its wire byte limit"
        )
    if not wire:
        raise WorkflowProgressProtocolError("workflow progress event cannot be empty")
    decoded = _decode_json(wire)
    envelope = _exact_mapping(decoded, _ENVELOPE_KEYS, "workflow progress event")
    if (
        type(envelope["schema_version"]) is not int
        or envelope["schema_version"] != WORKFLOW_PROGRESS_EVENT_SCHEMA_VERSION
    ):
        raise WorkflowProgressProtocolError("workflow progress event schema_version is unsupported")
    if envelope["encoding"] != WORKFLOW_PROGRESS_EVENT_ENCODING:
        raise WorkflowProgressProtocolError("workflow progress event encoding is unsupported")
    if envelope["limits_profile"] != WORKFLOW_PROGRESS_LIMITS_PROFILE:
        raise WorkflowProgressProtocolError("workflow progress event limits_profile is unsupported")
    kind = _event_kind(envelope["kind"])
    identity = _normalize_run_identity(envelope["run_identity"], limits=limits)
    if expected_run_identity is not None:
        expected = _normalize_run_identity(expected_run_identity, limits=limits)
        if identity != expected:
            raise WorkflowProgressProtocolError(
                "workflow progress event does not match the complete run fence",
                reason="fence_mismatch",
            )
    occurred_at = _normalize_timestamp(envelope["occurred_at"], producer=False)
    payload, normalized_truncated = _normalize_payload(
        kind,
        envelope["payload"],
        limits=limits,
    )
    stored_truncated = envelope["truncated"]
    if type(stored_truncated) is not bool or (normalized_truncated and not stored_truncated):
        raise WorkflowProgressProtocolError(
            "workflow progress event truncation evidence is invalid"
        )
    canonical = canonical_workflow_progress_json_bytes(
        _canonical_envelope(
            identity=identity,
            kind=kind,
            occurred_at=occurred_at,
            payload=payload,
            truncated=stored_truncated,
        )
    )
    if len(canonical) > limits.event_decoded_max_bytes:
        raise WorkflowProgressProtocolLimitError(
            "workflow progress event exceeds its decoded byte limit"
        )
    if canonical != wire:
        raise WorkflowProgressProtocolError(
            "workflow progress event must use canonical identity JSON"
        )
    return WorkflowProgressEvent(
        kind=kind,
        run_identity=deepcopy(identity),
        occurred_at=occurred_at,
        payload=deepcopy(payload),
        truncated=stored_truncated,
    )


def send_workflow_progress_event(
    actor: Any,
    run_identity: Mapping[str, Any],
    kind: WorkflowProgressEventKind | str,
    payload: Mapping[str, Any],
    *,
    occurred_at: datetime | None = None,
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
) -> bytes:
    """Prepare completely, then make exactly one ingest-only actor call."""
    wire = prepare_workflow_progress_event(
        run_identity,
        kind,
        payload,
        occurred_at=occurred_at,
        limits=limits,
    )
    actor.ingest.remote(wire)
    return wire


__all__ = [
    "WORKFLOW_PROGRESS_EVENT_ENCODING",
    "WORKFLOW_PROGRESS_EVENT_SCHEMA_VERSION",
    "WORKFLOW_PROGRESS_LIMITS_V1",
    "WorkflowProgressEvent",
    "WorkflowProgressEventKind",
    "WorkflowProgressLimits",
    "WorkflowProgressProtocolError",
    "WorkflowProgressProtocolLimitError",
    "canonical_workflow_progress_json_bytes",
    "decode_workflow_progress_event",
    "prepare_workflow_progress_event",
    "send_workflow_progress_event",
]
