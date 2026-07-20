"""Bounded actor protocol for coordinator-free workflow map results."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

RESULT_BUFFER_PROTOCOL = "django-ray.workflow-map-result-buffer"
RESULT_BUFFER_PROTOCOL_VERSION = 1
RESULT_BUFFER_CODEC = "ray.cloudpickle"
RESULT_BUFFER_CODEC_VERSION = 1
RESULT_BUFFER_PICKLE_PROTOCOL = 5
RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS = 2
RESULT_BUFFER_ACTOR_MAX_CONCURRENCY = 1

_MAX_ACTOR_OPTIONS = 16
_MAX_RESOURCE_OPTIONS = 32
_MAX_OPTION_STRING_CHARS = 256
_ALLOWED_ACTOR_OPTIONS = frozenset(
    {
        "memory",
        "num_cpus",
        "resources",
        "scheduling_strategy",
    }
)


class ResultBufferError(RuntimeError):
    """Base exception for the bounded map result-buffer protocol."""


class ResultBufferOverflowError(ResultBufferError):
    """Raised before an item would exceed a configured retained-data limit."""


class ResultBufferProtocolError(ResultBufferError):
    """Raised when a result-buffer actor violates its bounded protocol."""


def _bounded_string(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or len(value) > _MAX_OPTION_STRING_CHARS:
        raise ValueError(f"{name} must contain 1 to {_MAX_OPTION_STRING_CHARS} characters")
    return value


def _finite_number(
    value: Any,
    *,
    name: str,
    positive: bool,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if (positive and value <= 0) or (not positive and value < 0):
        relation = "greater than zero" if positive else "non-negative"
        raise ValueError(f"{name} must be {relation}")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _normalized_resources(value: Any) -> dict[str, int | float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("actor_options.resources must be a mapping")
    if len(value) > _MAX_RESOURCE_OPTIONS:
        raise ValueError(
            f"actor_options.resources must contain at most {_MAX_RESOURCE_OPTIONS} entries"
        )
    normalized: dict[str, int | float] = {}
    for key, item in value.items():
        resource = _bounded_string(key, name="actor_options.resources key")
        normalized[resource] = _finite_number(
            item,
            name=f"actor_options.resources.{resource}",
            positive=True,
        )
    return dict(sorted(normalized.items()))


def normalize_result_buffer_actor_options(
    actor_options: Mapping[str, Any],
    *,
    max_serialized_bytes: int,
) -> dict[str, Any]:
    """Return the canonical, bounded v1 actor scheduling contract."""
    if (
        isinstance(max_serialized_bytes, bool)
        or not isinstance(max_serialized_bytes, int)
        or max_serialized_bytes < 1
    ):
        raise ValueError("max_serialized_bytes must be a positive integer")
    if not isinstance(actor_options, Mapping):
        raise TypeError("actor_options must be a mapping")
    if len(actor_options) > _MAX_ACTOR_OPTIONS:
        raise ValueError(f"actor_options must contain at most {_MAX_ACTOR_OPTIONS} entries")
    unknown = set(actor_options) - _ALLOWED_ACTOR_OPTIONS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise ValueError(f"actor_options contains unsupported fields: {fields}")
    if "num_cpus" not in actor_options:
        raise ValueError("actor_options must explicitly set num_cpus > 0")
    if "memory" not in actor_options:
        raise ValueError("actor_options must explicitly set memory")

    num_cpus = _finite_number(
        actor_options["num_cpus"],
        name="actor_options.num_cpus",
        positive=True,
    )
    memory = actor_options["memory"]
    if isinstance(memory, bool) or not isinstance(memory, int):
        raise TypeError("actor_options.memory must be an integer byte count")
    if memory < max_serialized_bytes:
        raise ValueError(
            f"actor_options.memory must be at least max_serialized_bytes ({max_serialized_bytes})"
        )

    resources = _normalized_resources(actor_options.get("resources"))
    scheduling_strategy = actor_options.get("scheduling_strategy", "DEFAULT")
    if not isinstance(scheduling_strategy, str):
        raise TypeError("actor_options.scheduling_strategy must be a string")
    if scheduling_strategy not in {"DEFAULT", "SPREAD"}:
        raise ValueError("actor_options.scheduling_strategy must be 'DEFAULT' or 'SPREAD'")

    return {
        "num_cpus": num_cpus,
        "memory": memory,
        "resources": resources,
        "scheduling_strategy": scheduling_strategy,
        "lifetime": "non_detached",
        "max_restarts": 0,
        "max_task_retries": 0,
        "max_concurrency": RESULT_BUFFER_ACTOR_MAX_CONCURRENCY,
        "max_pending_calls": RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS,
    }


def result_buffer_ray_actor_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Translate canonical protocol options to Ray actor options."""
    ray_options = {
        "num_cpus": options["num_cpus"],
        "memory": options["memory"],
        "resources": dict(options["resources"]),
        "scheduling_strategy": options["scheduling_strategy"],
        "lifetime": None,
        "max_restarts": options["max_restarts"],
        "max_task_retries": options["max_task_retries"],
        "max_concurrency": options["max_concurrency"],
        "max_pending_calls": options["max_pending_calls"],
    }
    return ray_options


def result_buffer_plan_contract(
    *,
    max_items: int,
    max_concurrency: int,
    max_serialized_bytes: int,
    actor_options: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the complete fingerprinted result-buffer v1 contract."""
    return {
        "protocol": RESULT_BUFFER_PROTOCOL,
        "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION,
        "codec": {
            "name": RESULT_BUFFER_CODEC,
            "version": RESULT_BUFFER_CODEC_VERSION,
            "pickle_protocol": RESULT_BUFFER_PICKLE_PROTOCOL,
            "measurement": "retained_serialized_bytes",
        },
        "bounds": {
            "maximum_items": max_items,
            "maximum_in_flight_leaves": max_concurrency,
            "maximum_serialized_bytes": max_serialized_bytes,
            "maximum_pending_actor_calls": RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS,
        },
        "actor": dict(actor_options),
        "placement": {
            "scheduling_strategy": actor_options["scheduling_strategy"],
            "custom_resources": dict(actor_options["resources"]),
        },
        "lifetime": {
            "kind": "non_detached",
            "owner": "workflow_coordinator",
            "cleanup": "best_effort",
            "node_loss_recovery": False,
        },
        "restart": {
            "max_restarts": actor_options["max_restarts"],
            "max_task_retries": actor_options["max_task_retries"],
        },
        "result": {
            "ordered": True,
            "finalize_returns": 2,
            "payload_ref_resolved_by_coordinator": False,
        },
    }


def _ack_base(state: str) -> dict[str, Any]:
    return {
        "protocol": RESULT_BUFFER_PROTOCOL,
        "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION,
        "codec": RESULT_BUFFER_CODEC,
        "codec_version": RESULT_BUFFER_CODEC_VERSION,
        "state": state,
    }


def validate_result_buffer_ack(
    value: Any,
    *,
    state: str,
    expected_index: int | None = None,
    expected_items: int | None = None,
) -> dict[str, Any]:
    """Validate the small acknowledgement that the coordinator may decode."""
    if not isinstance(value, dict):
        raise ResultBufferProtocolError("Result-buffer acknowledgement must be a mapping")
    expected_keys = {
        "protocol",
        "protocol_version",
        "codec",
        "codec_version",
        "state",
    }
    if state in {"retained", "finalized", "discarded"}:
        expected_keys.update({"item_count", "retained_bytes"})
    if state == "retained":
        expected_keys.add("index")
    if set(value) != expected_keys:
        raise ResultBufferProtocolError(
            "Result-buffer acknowledgement contains an unexpected payload"
        )
    expected_base = _ack_base(state)
    if any(value.get(key) != item for key, item in expected_base.items()):
        raise ResultBufferProtocolError("Result-buffer acknowledgement protocol mismatch")
    for key in ("index", "item_count", "retained_bytes"):
        if key in value and (
            isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
        ):
            raise ResultBufferProtocolError(
                f"Result-buffer acknowledgement {key} must be a non-negative integer"
            )
    if expected_index is not None and value.get("index") != expected_index:
        raise ResultBufferProtocolError("Result-buffer acknowledgement index mismatch")
    if expected_items is not None and value.get("item_count") != expected_items:
        raise ResultBufferProtocolError("Result-buffer acknowledgement item count mismatch")
    return value


class WorkflowMapResultBuffer:
    """One non-detached actor retaining bounded serialized map results."""

    def __init__(self, max_items: int, max_serialized_bytes: int) -> None:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        if (
            isinstance(max_serialized_bytes, bool)
            or not isinstance(max_serialized_bytes, int)
            or max_serialized_bytes < 1
        ):
            raise ValueError("max_serialized_bytes must be a positive integer")
        self.max_items = max_items
        self.max_serialized_bytes = max_serialized_bytes
        self.retained_bytes = 0
        self._serialized: dict[int, bytes] = {}
        self._finalized = False

    def ready(self) -> dict[str, Any]:
        """Confirm actor scheduling before the coordinator admits leaf effects."""
        return _ack_base("ready")

    def append(self, index: int, value: Any) -> dict[str, Any]:
        """Serialize and retain one item without exceeding either declared bound."""
        if self._finalized:
            raise ResultBufferProtocolError("Cannot append after result-buffer finalization")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ResultBufferProtocolError("Result-buffer index must be a non-negative integer")
        if index >= self.max_items:
            raise ResultBufferOverflowError(
                f"Result-buffer item index {index} exceeds max_items={self.max_items}"
            )
        if index in self._serialized:
            raise ResultBufferProtocolError(f"Result-buffer index {index} was appended twice")

        import ray.cloudpickle as cloudpickle

        serialized = cloudpickle.dumps(value, protocol=RESULT_BUFFER_PICKLE_PROTOCOL)
        next_count = len(self._serialized) + 1
        next_bytes = self.retained_bytes + len(serialized)
        if next_count > self.max_items:
            raise ResultBufferOverflowError(
                f"Result-buffer item count exceeds max_items={self.max_items}"
            )
        if next_bytes > self.max_serialized_bytes:
            raise ResultBufferOverflowError(
                "Result-buffer retained serialization exceeds "
                f"max_serialized_bytes={self.max_serialized_bytes}"
            )

        self._serialized[index] = serialized
        self.retained_bytes = next_bytes
        return {
            **_ack_base("retained"),
            "index": index,
            "item_count": next_count,
            "retained_bytes": next_bytes,
        }

    def finalize(self, expected_items: int) -> tuple[list[Any], dict[str, Any]]:
        """Materialize ordered values as a direct Ray return plus a small acknowledgement."""
        if self._finalized:
            raise ResultBufferProtocolError("Result buffer was already finalized")
        if (
            isinstance(expected_items, bool)
            or not isinstance(expected_items, int)
            or expected_items < 0
            or expected_items > self.max_items
        ):
            raise ResultBufferProtocolError("Invalid result-buffer final item count")
        expected_indices = list(range(expected_items))
        if sorted(self._serialized) != expected_indices:
            raise ResultBufferProtocolError(
                "Result-buffer finalization found missing or unexpected item indices"
            )

        import ray.cloudpickle as cloudpickle

        retained_bytes = self.retained_bytes
        values = [cloudpickle.loads(self._serialized[index]) for index in expected_indices]
        self._serialized.clear()
        self.retained_bytes = 0
        self._finalized = True
        return values, {
            **_ack_base("finalized"),
            "item_count": expected_items,
            "retained_bytes": retained_bytes,
        }

    def discard(self) -> dict[str, Any]:
        """Drop retained state during best-effort cleanup."""
        item_count = len(self._serialized)
        retained_bytes = self.retained_bytes
        self._serialized.clear()
        self.retained_bytes = 0
        self._finalized = True
        return {
            **_ack_base("discarded"),
            "item_count": item_count,
            "retained_bytes": retained_bytes,
        }


__all__ = [
    "RESULT_BUFFER_ACTOR_MAX_CONCURRENCY",
    "RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS",
    "RESULT_BUFFER_CODEC",
    "RESULT_BUFFER_CODEC_VERSION",
    "RESULT_BUFFER_PICKLE_PROTOCOL",
    "RESULT_BUFFER_PROTOCOL",
    "RESULT_BUFFER_PROTOCOL_VERSION",
    "ResultBufferError",
    "ResultBufferOverflowError",
    "ResultBufferProtocolError",
    "WorkflowMapResultBuffer",
    "normalize_result_buffer_actor_options",
    "result_buffer_plan_contract",
    "result_buffer_ray_actor_options",
    "validate_result_buffer_ack",
]
