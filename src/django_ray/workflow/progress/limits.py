"""Shared protocol-v1 limits for live and durable workflow progress."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any

from django_ray.workflow._compat import preserve_legacy_module_identity

WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION = 1
WORKFLOW_PROGRESS_LIMITS_PROFILE = "v1"

WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS = 256
WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES = 256 * 1024
WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES = 1024 * 1024
WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES = 16 * 1024
WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS = 25_000
WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS = 100_000
WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES = 16 * 1024 * 1024
WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES = 32 * 1024 * 1024
WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS = 25_000
WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES = 8 * 1024 * 1024
WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES = 16 * 1024 * 1024
WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES = 32 * 1024 * 1024
WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES = 64 * 1024 * 1024
WORKFLOW_PROGRESS_VALUE_MAX_DEPTH = 6
WORKFLOW_PROGRESS_METRICS_MAX_ITEMS = 32
WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES = 4 * 1024
WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES = 64
WORKFLOW_PROGRESS_METRIC_STRING_MAX_BYTES = 256
WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES = 256
WORKFLOW_PROGRESS_LABEL_MAX_BYTES = 512
WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES = 2 * 1024
WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS = 32
WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES = 1024
WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES = 256 * 1024
WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER = (1 << 63) - 1

WORKFLOW_PROGRESS_EVENT_PAYLOAD_MAX_BYTES = 16 * 1024
WORKFLOW_PROGRESS_EVENT_WIRE_MAX_BYTES = 32 * 1024
WORKFLOW_PROGRESS_EVENT_DECODED_MAX_BYTES = 32 * 1024
WORKFLOW_PROGRESS_EDGE_BATCH_MAX_ITEMS = 32


def canonical_workflow_progress_retained_size(value: Any) -> int:
    """Return the canonical encoded size used by the live progress collector."""
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


_WORKFLOW_PROGRESS_RETAINED_STATE_FRAME_BYTES = canonical_workflow_progress_retained_size(
    {
        "edges": [],
        "nodes": [],
        "plan": None,
        "recent_events": [],
    }
) - len(b"null")


def workflow_progress_retained_state_size(
    *,
    plan_bytes: int,
    node_bytes: int,
    node_count: int,
    edge_bytes: int,
    edge_count: int,
    event_bytes: int,
    event_count: int,
) -> int:
    """Return the exact canonical size of one retained actor state frame."""
    return (
        _WORKFLOW_PROGRESS_RETAINED_STATE_FRAME_BYTES
        + plan_bytes
        + node_bytes
        + max(0, node_count - 1)
        + edge_bytes
        + max(0, edge_count - 1)
        + event_bytes
        + max(0, event_count - 1)
    )


@dataclass(frozen=True)
class WorkflowProgressLimits:
    """One immutable set of caps that may only narrow the protocol-v1 profile."""

    topology_page_max_items: int = WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS
    topology_page_max_encoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
    topology_page_max_decoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
    record_max_encoded_bytes: int = WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES
    topology_node_max_items: int = WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS
    topology_edge_max_items: int = WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS
    topology_max_encoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
    topology_max_decoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
    detail_max_items: int = WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS
    detail_max_encoded_bytes: int = WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
    detail_max_decoded_bytes: int = WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
    combined_max_encoded_bytes: int = WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
    combined_max_decoded_bytes: int = WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
    value_max_depth: int = WORKFLOW_PROGRESS_VALUE_MAX_DEPTH
    metrics_max_items: int = WORKFLOW_PROGRESS_METRICS_MAX_ITEMS
    metrics_max_encoded_bytes: int = WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES
    metric_key_max_bytes: int = WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES
    metric_string_max_bytes: int = WORKFLOW_PROGRESS_METRIC_STRING_MAX_BYTES
    node_id_max_bytes: int = WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES
    label_max_bytes: int = WORKFLOW_PROGRESS_LABEL_MAX_BYTES
    message_max_bytes: int = WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES
    recent_event_max_items: int = WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS
    recent_event_max_encoded_bytes: int = WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES
    topology_manifest_max_encoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES
    identity_max_integer: int = WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
    event_payload_max_bytes: int = WORKFLOW_PROGRESS_EVENT_PAYLOAD_MAX_BYTES
    event_wire_max_bytes: int = WORKFLOW_PROGRESS_EVENT_WIRE_MAX_BYTES
    event_decoded_max_bytes: int = WORKFLOW_PROGRESS_EVENT_DECODED_MAX_BYTES
    edge_batch_max_items: int = WORKFLOW_PROGRESS_EDGE_BATCH_MAX_ITEMS

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            hard_limit = getattr(_WORKFLOW_PROGRESS_LIMITS_V1_VALUES, item.name)
            if type(value) is not int or not 1 <= value <= hard_limit:
                raise ValueError(
                    f"{item.name} must be an integer between 1 and its protocol-v1 cap"
                )


@dataclass(frozen=True)
class _WorkflowProgressLimitsV1Values:
    topology_page_max_items: int = WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS
    topology_page_max_encoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
    topology_page_max_decoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
    record_max_encoded_bytes: int = WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES
    topology_node_max_items: int = WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS
    topology_edge_max_items: int = WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS
    topology_max_encoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
    topology_max_decoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
    detail_max_items: int = WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS
    detail_max_encoded_bytes: int = WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
    detail_max_decoded_bytes: int = WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
    combined_max_encoded_bytes: int = WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
    combined_max_decoded_bytes: int = WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
    value_max_depth: int = WORKFLOW_PROGRESS_VALUE_MAX_DEPTH
    metrics_max_items: int = WORKFLOW_PROGRESS_METRICS_MAX_ITEMS
    metrics_max_encoded_bytes: int = WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES
    metric_key_max_bytes: int = WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES
    metric_string_max_bytes: int = WORKFLOW_PROGRESS_METRIC_STRING_MAX_BYTES
    node_id_max_bytes: int = WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES
    label_max_bytes: int = WORKFLOW_PROGRESS_LABEL_MAX_BYTES
    message_max_bytes: int = WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES
    recent_event_max_items: int = WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS
    recent_event_max_encoded_bytes: int = WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES
    topology_manifest_max_encoded_bytes: int = WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES
    identity_max_integer: int = WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
    event_payload_max_bytes: int = WORKFLOW_PROGRESS_EVENT_PAYLOAD_MAX_BYTES
    event_wire_max_bytes: int = WORKFLOW_PROGRESS_EVENT_WIRE_MAX_BYTES
    event_decoded_max_bytes: int = WORKFLOW_PROGRESS_EVENT_DECODED_MAX_BYTES
    edge_batch_max_items: int = WORKFLOW_PROGRESS_EDGE_BATCH_MAX_ITEMS


_WORKFLOW_PROGRESS_LIMITS_V1_VALUES = _WorkflowProgressLimitsV1Values()
WORKFLOW_PROGRESS_LIMITS_V1 = WorkflowProgressLimits()

# The default-off publication pilot deliberately narrows aggregate retention
# while preserving the protocol-v1 per-event and pagination ceilings.
WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS_PROFILE = "schema-v3-pilot-v1"
WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS = WorkflowProgressLimits(
    topology_node_max_items=512,
    topology_edge_max_items=2_048,
    topology_max_encoded_bytes=2 * 1024 * 1024,
    topology_max_decoded_bytes=2 * 1024 * 1024,
    detail_max_items=512,
    detail_max_encoded_bytes=1024 * 1024,
    detail_max_decoded_bytes=1024 * 1024,
    combined_max_encoded_bytes=4 * 1024 * 1024,
    combined_max_decoded_bytes=4 * 1024 * 1024,
)


__all__ = [
    "WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS",
    "WORKFLOW_PROGRESS_EDGE_BATCH_MAX_ITEMS",
    "WORKFLOW_PROGRESS_EVENT_DECODED_MAX_BYTES",
    "WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_EVENT_PAYLOAD_MAX_BYTES",
    "WORKFLOW_PROGRESS_EVENT_WIRE_MAX_BYTES",
    "WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER",
    "WORKFLOW_PROGRESS_LABEL_MAX_BYTES",
    "WORKFLOW_PROGRESS_LIMITS_PROFILE",
    "WORKFLOW_PROGRESS_LIMITS_V1",
    "WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES",
    "WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_METRICS_MAX_ITEMS",
    "WORKFLOW_PROGRESS_METRIC_KEY_MAX_BYTES",
    "WORKFLOW_PROGRESS_METRIC_STRING_MAX_BYTES",
    "WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES",
    "WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS",
    "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS",
    "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS_PROFILE",
    "WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION",
    "WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS",
    "WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS",
    "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES",
    "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS",
    "WORKFLOW_PROGRESS_VALUE_MAX_DEPTH",
    "WorkflowProgressLimits",
    "canonical_workflow_progress_retained_size",
    "workflow_progress_retained_state_size",
]

preserve_legacy_module_identity(
    globals(),
    exports=__all__,
    legacy_module="django_ray.workflow_progress_limits",
)
