"""Schema-v3 publication for bounded workflow progress policies."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    WorkflowRunIdentity,
)
from django_ray.workflow_plans import (
    effective_plan_selection_reporting_policy,
    validate_plan_selection_manifest,
)
from django_ray.workflow_progress_limits import (
    WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS_PROFILE,
    WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
    WorkflowProgressLimits,
    canonical_workflow_progress_retained_size,
    workflow_progress_retained_state_size,
)
from django_ray.workflow_progress_storage import (
    PreparedWorkflowProgressDetail,
    PreparedWorkflowProgressTopology,
    WorkflowProgressStorageError,
    discard_workflow_progress_topology_candidate,
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    prepare_workflow_progress_topology,
    stage_workflow_progress_topology,
)
from django_ray.workflow_progress_summary import (
    WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS,
    WORKFLOW_PROGRESS_SUMMARY_LIMITS_PROFILE,
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    WorkflowProgressSummaryError,
    serialize_workflow_progress_summary,
)

_SNAPSHOT_KEYS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "run_identity",
        "plan",
        "revision",
        "state",
        "total_nodes",
        "completed_nodes",
        "failed_nodes",
        "running_nodes",
        "pending_nodes",
        "progress_percent",
        "started_at",
        "updated_at",
        "graph",
        "recent_events",
        "ingress",
    }
)
_GRAPH_KEYS = frozenset({"nodes", "edges"})
_EDGE_KEYS = frozenset({"source", "target"})
_PLAN_KEYS = frozenset(
    {
        "plan_format",
        "plan_format_version",
        "fingerprint",
        "definition_name",
        "definition_revision",
        "topology_class",
        "node_count",
    }
)
_NODE_KEYS = frozenset(
    {
        "node_id",
        "kind",
        "label",
        "callable_path",
        "dependencies",
        "runtime_env",
        "ray_options",
        "state",
        "progress",
        "execution",
        "started_at",
        "finished_at",
        "error",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "ray_task_id",
        "ray_job_id",
        "ray_node_id",
        "ray_worker_id",
        "assigned_resources",
    }
)
_EVENT_KEYS = frozenset({"node_id", "event", "state", "label", "timestamp"})
_INGRESS_KEYS = frozenset(
    {
        "accepted",
        "rejected",
        "truncated",
        "accepted_by_kind",
        "rejected_by_reason",
        "retained_bytes",
        "retained_nodes",
        "retained_edges",
    }
)
_INGRESS_COST_KEYS = frozenset(
    {
        "schema_version",
        "saturated",
        "initialization",
        "ingest",
        "delivery_delay",
        "snapshot",
    }
)
_INGRESS_COST_INITIALIZATION_KEYS = frozenset(
    {
        "wire_bytes",
        "handler_wall_ns",
        "handler_cpu_ns",
    }
)
_INGRESS_COST_INGEST_KEYS = frozenset(
    {
        "calls_received",
        "wire_bytes_received",
        "decoded_calls",
        "post_disable_calls",
        "decoded_by_kind",
        "handler_wall_ns_total",
        "handler_wall_ns_max",
        "handler_cpu_ns_total",
        "handler_cpu_ns_max",
    }
)
_INGRESS_COST_DELIVERY_DELAY_KEYS = frozenset(
    {
        "samples",
        "total_us",
        "max_us",
        "negative_clock_samples",
    }
)
_INGRESS_COST_SNAPSHOT_KEYS = frozenset(
    {
        "calls",
        "build_wall_ns_total",
        "build_wall_ns_max",
        "build_cpu_ns_total",
        "build_cpu_ns_max",
    }
)
_ACCEPTED_EVENT_KINDS = frozenset(
    {
        "initialized",
        "node_registered",
        "edges_registered",
        "map_registered",
        "submitted",
        "started",
        "application_progress",
        "map_progress",
        "completed",
        "failed",
    }
)
_REJECTED_EVENT_REASONS = frozenset(
    {
        "protocol_error",
        "fence_mismatch",
        "unexpected_initialized",
        "node_limit",
        "edge_limit",
        "retained_bytes_limit",
    }
)
_NODE_STATES = frozenset({"PENDING", "RUNNING", "SUCCEEDED", "FAILED"})
_TERMINAL_WORKFLOW_STATES = frozenset({"SUCCEEDED", "FAILED"})
_MAX_COUNTER = (1 << 63) - 1


class WorkflowProgressPilotReason(StrEnum):
    """Stable secret-free outcomes for one terminal pilot publication."""

    PUBLISHED = "published"
    STALE_FENCE = "stale_fence"
    PILOT_DISABLED = "pilot_disabled"
    INVALID_SELECTION = "invalid_selection"
    INVALID_SNAPSHOT = "invalid_snapshot"
    INGRESS_REJECTED = "ingress_rejected"
    INGRESS_TRUNCATED = "ingress_truncated"
    ADMISSION_LIMIT = "admission_limit"
    PREPARATION_TRUNCATED = "preparation_truncated"
    PUBLICATION_FAILED = "publication_failed"
    CANDIDATE_CLEANUP_FAILED = "candidate_cleanup_failed"


class WorkflowProgressPilotError(ValueError):
    """Reject a snapshot using one bounded operator-facing reason."""

    def __init__(self, reason: WorkflowProgressPilotReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class PreparedWorkflowProgressPilotPublication:
    """Prepared immutable topology, initial detail, and producer summary."""

    topology: PreparedWorkflowProgressTopology
    detail: PreparedWorkflowProgressDetail
    summary: dict[str, Any]


@dataclass(frozen=True)
class WorkflowProgressPilotPublicationResult:
    """One bounded publication outcome that never retains producer payloads."""

    accepted: bool
    reason: WorkflowProgressPilotReason
    summary: dict[str, Any] | None = None


@dataclass(frozen=True)
class _PinnedWorkflowPlan:
    fingerprint: str
    selected_strategy: str
    reporting_policy: str


def _exact_mapping(
    value: Any,
    keys: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    return value


def _counter(
    value: Any,
    *,
    maximum: int = _MAX_COUNTER,
) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    return value


def _utc_timestamp(value: Any) -> str:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    try:
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT) from error
    return parsed.isoformat().replace("+00:00", "Z")


def _pinned_workflow_plan(
    identity: WorkflowRunIdentity,
    *,
    using: str,
) -> _PinnedWorkflowPlan:
    execution = (
        RayTaskExecution.objects.using(using)
        .only(
            "pk",
            "state",
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_plan_fingerprint",
            "workflow_plan_selection",
        )
        .filter(
            pk=identity.task_execution_pk,
            state=TaskState.RUNNING,
            attempt_number=identity.attempt_number,
            execution_generation=identity.execution_generation,
            workflow_run_id=identity.run_id,
        )
        .first()
    )
    if execution is None:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.STALE_FENCE)
    fingerprint = execution.workflow_plan_fingerprint
    serialized_selection = execution.workflow_plan_selection
    if not isinstance(fingerprint, str) or not isinstance(serialized_selection, str):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SELECTION)
    try:
        selection = validate_plan_selection_manifest(json.loads(serialized_selection))
        reporting_policy = effective_plan_selection_reporting_policy(selection)
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SELECTION) from error
    selected_strategy = selection["selected_strategy"]
    if not isinstance(selected_strategy, str):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SELECTION)
    if reporting_policy != "full":
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.PILOT_DISABLED)
    return _PinnedWorkflowPlan(
        fingerprint=fingerprint,
        selected_strategy=selected_strategy,
        reporting_policy=reporting_policy,
    )


def _validate_ingress_envelope(
    ingress_value: Any,
    *,
    revision: int,
    limits: WorkflowProgressLimits,
) -> Mapping[str, Any]:
    if not isinstance(ingress_value, Mapping):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    ingress_keys = frozenset(ingress_value)
    if ingress_keys == _INGRESS_KEYS:
        ingress = ingress_value
    elif ingress_keys == _INGRESS_KEYS | {"cost"}:
        ingress = ingress_value
    else:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    accepted_by_kind = _exact_mapping(
        ingress["accepted_by_kind"],
        _ACCEPTED_EVENT_KINDS,
    )
    rejected_by_reason = _exact_mapping(
        ingress["rejected_by_reason"],
        _REJECTED_EVENT_REASONS,
    )
    counter_max = limits.identity_max_integer
    accepted_counts = {
        name: _counter(accepted_by_kind[name], maximum=counter_max)
        for name in _ACCEPTED_EVENT_KINDS
    }
    rejected_counts = [
        _counter(value, maximum=counter_max) for value in rejected_by_reason.values()
    ]
    accepted = _counter(ingress["accepted"], maximum=counter_max)
    rejected = _counter(ingress["rejected"], maximum=counter_max)
    truncated = _counter(ingress["truncated"], maximum=counter_max)
    if (
        accepted != sum(accepted_counts.values())
        or accepted_counts["initialized"] != 1
        or revision != accepted - 1
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if rejected != sum(rejected_counts):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if rejected:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INGRESS_REJECTED)
    if truncated:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INGRESS_TRUNCATED)
    if "cost" in ingress:
        _validate_ingress_cost(
            ingress["cost"],
            accepted=accepted,
            rejected=rejected,
            accepted_by_kind=accepted_counts,
            limits=limits,
        )
    return ingress


def _validate_ingress_cost(
    value: Any,
    *,
    accepted: int,
    rejected: int,
    accepted_by_kind: Mapping[str, int],
    limits: WorkflowProgressLimits,
) -> None:
    cost = _exact_mapping(value, _INGRESS_COST_KEYS)
    if type(cost["schema_version"]) is not int or cost["schema_version"] != 1:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    saturated = cost["saturated"]
    if type(saturated) is not bool:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)

    initialization = _exact_mapping(
        cost["initialization"],
        _INGRESS_COST_INITIALIZATION_KEYS,
    )
    initialization_values = {
        name: _counter(
            initialization[name],
            maximum=limits.identity_max_integer,
        )
        for name in _INGRESS_COST_INITIALIZATION_KEYS
    }
    if not 0 < initialization_values["wire_bytes"] <= limits.event_wire_max_bytes:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)

    ingest = _exact_mapping(
        cost["ingest"],
        _INGRESS_COST_INGEST_KEYS,
    )
    decoded_by_kind = _exact_mapping(
        ingest["decoded_by_kind"],
        _ACCEPTED_EVENT_KINDS,
    )
    decoded_counts = {
        name: _counter(
            decoded_by_kind[name],
            maximum=limits.identity_max_integer,
        )
        for name in _ACCEPTED_EVENT_KINDS
    }
    ingest_values = {
        name: _counter(
            ingest[name],
            maximum=limits.identity_max_integer,
        )
        for name in _INGRESS_COST_INGEST_KEYS
        if name != "decoded_by_kind"
    }

    delivery = _exact_mapping(
        cost["delivery_delay"],
        _INGRESS_COST_DELIVERY_DELAY_KEYS,
    )
    delivery_values = {
        name: _counter(
            delivery[name],
            maximum=limits.identity_max_integer,
        )
        for name in _INGRESS_COST_DELIVERY_DELAY_KEYS
    }

    snapshot = _exact_mapping(
        cost["snapshot"],
        _INGRESS_COST_SNAPSHOT_KEYS,
    )
    snapshot_values = {
        name: _counter(
            snapshot[name],
            maximum=limits.identity_max_integer,
        )
        for name in _INGRESS_COST_SNAPSHOT_KEYS
    }

    if (
        not _valid_sample_aggregate(
            count=ingest_values["calls_received"],
            total=ingest_values["handler_wall_ns_total"],
            maximum=ingest_values["handler_wall_ns_max"],
            counter_max=limits.identity_max_integer,
        )
        or not _valid_sample_aggregate(
            count=ingest_values["calls_received"],
            total=ingest_values["handler_cpu_ns_total"],
            maximum=ingest_values["handler_cpu_ns_max"],
            counter_max=limits.identity_max_integer,
        )
        or not _valid_sample_aggregate(
            count=delivery_values["samples"],
            total=delivery_values["total_us"],
            maximum=delivery_values["max_us"],
            counter_max=limits.identity_max_integer,
        )
        or snapshot_values["calls"] == 0
        or not _valid_sample_aggregate(
            count=snapshot_values["calls"],
            total=snapshot_values["build_wall_ns_total"],
            maximum=snapshot_values["build_wall_ns_max"],
            counter_max=limits.identity_max_integer,
        )
        or not _valid_sample_aggregate(
            count=snapshot_values["calls"],
            total=snapshot_values["build_cpu_ns_total"],
            maximum=snapshot_values["build_cpu_ns_max"],
            counter_max=limits.identity_max_integer,
        )
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)

    expected_decoded_by_kind = {
        name: 0 if name == "initialized" else accepted_by_kind[name]
        for name in _ACCEPTED_EVENT_KINDS
    }
    expected_decoded = accepted - 1
    expected_received = _saturating_counter_sum(
        limits.identity_max_integer,
        expected_decoded,
        rejected,
        ingest_values["post_disable_calls"],
    )
    minimum_wire_bytes = expected_decoded
    maximum_wire_bytes = (
        _saturating_counter_product(
            limits.identity_max_integer,
            expected_decoded,
            limits.event_wire_max_bytes,
        )
        if rejected == 0 and ingest_values["post_disable_calls"] == 0
        else limits.identity_max_integer
    )
    if (
        ingest_values["calls_received"] != expected_received
        or ingest_values["decoded_calls"] != expected_decoded
        or decoded_counts != expected_decoded_by_kind
        or _saturating_counter_sum(
            limits.identity_max_integer,
            delivery_values["samples"],
            delivery_values["negative_clock_samples"],
        )
        != expected_decoded
        or not minimum_wire_bytes <= ingest_values["wire_bytes_received"] <= maximum_wire_bytes
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if saturated:
        numeric_values = [
            *initialization_values.values(),
            *ingest_values.values(),
            *decoded_counts.values(),
            *delivery_values.values(),
            *snapshot_values.values(),
        ]
        if limits.identity_max_integer not in numeric_values:
            raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)


def _saturating_counter_sum(counter_max: int, *values: int) -> int:
    return min(counter_max, sum(values))


def _saturating_counter_product(
    counter_max: int,
    left: int,
    right: int,
) -> int:
    return min(counter_max, left * right)


def _valid_sample_aggregate(
    *,
    count: int,
    total: int,
    maximum: int,
    counter_max: int,
) -> bool:
    if count == 0:
        return total == maximum == 0
    return maximum <= total and total <= _saturating_counter_product(
        counter_max,
        count,
        maximum,
    )


def _validate_ingress_retention(
    ingress: Mapping[str, Any],
    *,
    node_count: int,
    edge_count: int,
    expected_retained_bytes: int,
    limits: WorkflowProgressLimits,
) -> None:
    if (
        _counter(
            ingress["retained_nodes"],
            maximum=limits.topology_node_max_items,
        )
        != node_count
        or _counter(
            ingress["retained_edges"],
            maximum=limits.topology_edge_max_items,
        )
        != edge_count
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    retained_bytes = _counter(
        ingress["retained_bytes"],
        maximum=limits.combined_max_decoded_bytes,
    )
    if retained_bytes == 0 or retained_bytes != expected_retained_bytes:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)


def _actor_retained_state_size(
    plan: Mapping[str, Any],
    graph: Mapping[str, Any],
    recent_events: list[Any],
) -> int:
    """Recompute actor accounting without snapshot-derived dependencies."""
    nodes = []
    for value in graph["nodes"]:
        node = dict(value)
        node["dependencies"] = []
        nodes.append(node)
    edges = [dict(value) for value in graph["edges"]]
    events = [dict(value) for value in recent_events]
    try:
        return workflow_progress_retained_state_size(
            plan_bytes=canonical_workflow_progress_retained_size(plan),
            node_bytes=sum(canonical_workflow_progress_retained_size(node) for node in nodes),
            node_count=len(nodes),
            edge_bytes=sum(canonical_workflow_progress_retained_size(edge) for edge in edges),
            edge_count=len(edges),
            event_bytes=sum(canonical_workflow_progress_retained_size(event) for event in events),
            event_count=len(events),
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT) from error


def _normalize_execution(value: Any) -> dict[str, Any] | None:
    if value == {}:
        return None
    if not isinstance(value, Mapping) or not set(value) <= _EXECUTION_KEYS:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    assigned_resources = value.get("assigned_resources", {})
    if not isinstance(assigned_resources, Mapping):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    return {
        "ray_task_id": value.get("ray_task_id"),
        "ray_job_id": value.get("ray_job_id"),
        "ray_node_id": value.get("ray_node_id"),
        "ray_worker_id": value.get("ray_worker_id"),
        "assigned_resources": dict(assigned_resources),
    }


def _normalize_graph(
    graph_value: Any,
    events_value: Any,
    *,
    limits: WorkflowProgressLimits,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    graph = _exact_mapping(graph_value, _GRAPH_KEYS)
    nodes_value = graph["nodes"]
    edges_value = graph["edges"]
    if not isinstance(nodes_value, list) or not isinstance(edges_value, list):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if (
        len(nodes_value) > limits.topology_node_max_items
        or len(nodes_value) > limits.detail_max_items
        or len(edges_value) > limits.topology_edge_max_items
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.ADMISSION_LIMIT)

    topology_nodes: list[dict[str, Any]] = []
    node_values: dict[str, Mapping[str, Any]] = {}
    for value in nodes_value:
        if not isinstance(value, Mapping):
            raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
        kind = value.get("kind")
        expected_keys = _NODE_KEYS | ({"fanout"} if kind == "map" else set())
        node = _exact_mapping(value, frozenset(expected_keys))
        node_id = node["node_id"]
        if not isinstance(node_id, str) or node_id in node_values:
            raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
        if kind not in {"task", "map"}:
            raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
        node_values[node_id] = node
        topology_nodes.append(
            {
                "node_id": node_id,
                "kind": kind,
                "label": node["label"],
                "callable_path": node["callable_path"],
                "runtime_env": node["runtime_env"],
                "ray_options": node["ray_options"],
            }
        )

    topology_edges: list[dict[str, str]] = []
    inbound: dict[str, list[str]] = defaultdict(list)
    seen_edges: set[tuple[str, str]] = set()
    for value in edges_value:
        edge = _exact_mapping(value, _EDGE_KEYS)
        source = edge["source"]
        target = edge["target"]
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in node_values
            or target not in node_values
            or (source, target) in seen_edges
        ):
            raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
        seen_edges.add((source, target))
        inbound[target].append(source)
        topology_edges.append({"source": source, "target": target})

    if not isinstance(events_value, list):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if len(events_value) > limits.recent_event_max_items:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.ADMISSION_LIMIT)
    events_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in events_value:
        event = _exact_mapping(value, _EVENT_KEYS)
        node_id = event["node_id"]
        if not isinstance(node_id, str) or node_id not in node_values:
            raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
        events_by_node[node_id].append(
            {
                "event": event["event"],
                "state": event["state"],
                "label": event["label"],
                "timestamp": event["timestamp"],
            }
        )

    detail: list[dict[str, Any]] = []
    for node_id, node in node_values.items():
        dependencies = node["dependencies"]
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) for item in dependencies)
            or sorted(dependencies) != sorted(inbound.get(node_id, []))
        ):
            raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
        detail.append(
            {
                "schema_version": WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
                "node_id": node_id,
                "invocation_identity": None,
                "state": node["state"],
                "progress": node["progress"],
                "execution": _normalize_execution(node["execution"]),
                "fanout": node.get("fanout") if node["kind"] == "map" else None,
                "started_at": node["started_at"],
                "finished_at": node["finished_at"],
                "error": node["error"],
                "recent_events": events_by_node.get(node_id, []),
            }
        )
    return topology_nodes, topology_edges, detail


def prepare_terminal_workflow_progress_publication(
    identity: WorkflowRunIdentity,
    snapshot_value: Any,
    *,
    plan_fingerprint: str,
    selected_strategy: str,
    reporting_policy: str,
    detail_days: int,
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
) -> PreparedWorkflowProgressPilotPublication:
    """Adapt one complete actor snapshot into bounded schema-v3 publication input."""
    snapshot = _exact_mapping(snapshot_value, _SNAPSHOT_KEYS)
    if (
        type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != WORKFLOW_PROGRESS_SCHEMA_VERSION
        or snapshot["run_identity"] != identity.as_dict()
        or snapshot["workflow_id"] != f"django-ray:{identity.task_execution_pk}"
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if (
        type(detail_days) is not int
        or not 0 <= detail_days <= WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS
        or reporting_policy != "full"
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SELECTION)

    revision = _counter(
        snapshot["revision"],
        maximum=limits.identity_max_integer,
    )
    if revision == 0 or revision == limits.identity_max_integer:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    ingress = _validate_ingress_envelope(
        snapshot["ingress"],
        revision=revision,
        limits=limits,
    )
    state = snapshot["state"]
    if state not in _TERMINAL_WORKFLOW_STATES:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)

    plan = _exact_mapping(snapshot["plan"], _PLAN_KEYS)
    if plan["fingerprint"] != plan_fingerprint:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SELECTION)
    _counter(
        plan["node_count"],
        maximum=limits.topology_node_max_items,
    )

    graph = _exact_mapping(snapshot["graph"], _GRAPH_KEYS)
    recent_events = snapshot["recent_events"]
    topology_nodes, topology_edges, detail_records = _normalize_graph(
        graph,
        recent_events,
        limits=limits,
    )
    node_count = len(topology_nodes)
    edge_count = len(topology_edges)
    state_counts = {
        state_name: sum(record["state"] == state_name for record in detail_records)
        for state_name in _NODE_STATES
    }
    supplied_counts = {
        "PENDING": _counter(
            snapshot["pending_nodes"],
            maximum=limits.topology_node_max_items,
        ),
        "RUNNING": _counter(
            snapshot["running_nodes"],
            maximum=limits.topology_node_max_items,
        ),
        "SUCCEEDED": _counter(
            snapshot["completed_nodes"],
            maximum=limits.topology_node_max_items,
        ),
        "FAILED": _counter(
            snapshot["failed_nodes"],
            maximum=limits.topology_node_max_items,
        ),
    }
    total_nodes = _counter(
        snapshot["total_nodes"],
        maximum=limits.topology_node_max_items,
    )
    if (
        node_count == 0
        or total_nodes != node_count
        or supplied_counts != state_counts
        or sum(state_counts.values()) != node_count
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    terminal_nodes = state_counts["SUCCEEDED"] + state_counts["FAILED"]
    expected_percent = round(terminal_nodes / node_count * 100, 1)
    progress_percent = snapshot["progress_percent"]
    if (
        not isinstance(progress_percent, int | float)
        or isinstance(progress_percent, bool)
        or not math.isfinite(progress_percent)
        or float(progress_percent) != expected_percent
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if state == "SUCCEEDED" and state_counts != {
        "PENDING": 0,
        "RUNNING": 0,
        "SUCCEEDED": node_count,
        "FAILED": 0,
    }:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if state == "FAILED" and state_counts["FAILED"] == 0:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)

    _validate_ingress_retention(
        ingress,
        node_count=node_count,
        edge_count=edge_count,
        expected_retained_bytes=_actor_retained_state_size(
            plan,
            graph,
            recent_events,
        ),
        limits=limits,
    )
    started_at = _utc_timestamp(snapshot["started_at"])
    updated_at = _utc_timestamp(snapshot["updated_at"])
    if datetime.fromisoformat(updated_at[:-1] + "+00:00") < datetime.fromisoformat(
        started_at[:-1] + "+00:00"
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)

    try:
        topology = prepare_workflow_progress_topology(
            identity,
            1,
            topology_nodes,
            topology_edges,
        )
        detail = prepare_workflow_progress_detail(
            detail_records,
            topology=topology,
            reporting_policy=reporting_policy,
        )
    except WorkflowProgressStorageError as error:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT) from error
    if topology.truncation_reasons or detail.truncation_reasons:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.PREPARATION_TRUNCATED)
    if (
        topology.retained_node_count != node_count
        or topology.retained_edge_count != edge_count
        or len(detail.records) != node_count
        or topology.encoded_bytes > limits.topology_max_encoded_bytes
        or topology.decoded_bytes > limits.topology_max_decoded_bytes
        or detail.encoded_bytes > limits.detail_max_encoded_bytes
        or detail.decoded_bytes > limits.detail_max_decoded_bytes
        or topology.encoded_bytes + detail.encoded_bytes > limits.combined_max_encoded_bytes
        or topology.decoded_bytes + detail.decoded_bytes > limits.combined_max_decoded_bytes
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.ADMISSION_LIMIT)

    finished_at = updated_at
    summary = {
        "schema_version": WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
        "storage_protocol_version": WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
        "run_identity": identity.as_dict(),
        "reporting_policy": reporting_policy,
        "selected_strategy": selected_strategy,
        "plan_fingerprint": plan_fingerprint,
        "limits_profile": WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS_PROFILE,
        "summary_revision": 1,
        "topology_version": None,
        "detail_revision": None,
        "state": state,
        "node_counts": {
            "declared": None,
            "discovered": node_count,
            "retained_topology": 0,
            "retained_detail": 0,
            "pending": state_counts["PENDING"],
            "running": state_counts["RUNNING"],
            "succeeded": state_counts["SUCCEEDED"],
            "failed": state_counts["FAILED"],
        },
        "edge_counts": {
            "declared": None,
            "discovered": edge_count,
            "retained_topology": 0,
        },
        "progress_percent": expected_percent,
        "timestamps": {
            "started_at": started_at,
            "updated_at": updated_at,
            "finished_at": finished_at,
        },
        "detail": {
            "availability": "NOT_REPORTED",
            "complete": False,
            "truncation_reasons": [],
        },
        "storage": {"kind": "database", "manifest_id": None},
        "retention": {
            "detail_days": detail_days,
            "detail_expires_at": None,
        },
        "terminal": {
            "outcome": state,
            "finished_at": finished_at,
        },
    }
    try:
        serialize_workflow_progress_summary(
            summary,
            expected_identity=identity,
        )
    except WorkflowProgressSummaryError as error:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT) from error
    return PreparedWorkflowProgressPilotPublication(
        topology=topology,
        detail=detail,
        summary=summary,
    )


def prepare_terminal_only_workflow_progress_summary(
    identity: WorkflowRunIdentity,
    *,
    plan_fingerprint: str,
    selected_strategy: str,
    declared_node_count: int,
    declared_edge_count: int,
    outcome: str,
    started_at: int | float,
    finished_at: int | float,
    detail_days: int,
) -> dict[str, Any]:
    """Prepare one terminal summary without claiming any node discovery."""
    if outcome not in _TERMINAL_WORKFLOW_STATES:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)
    if (
        type(detail_days) is not int
        or not 0 <= detail_days <= WORKFLOW_PROGRESS_DETAIL_RETENTION_MAX_DAYS
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SELECTION)
    declared_nodes = _counter(declared_node_count)
    declared_edges = _counter(declared_edge_count)
    started_timestamp = _utc_timestamp(started_at)
    finished_timestamp = _utc_timestamp(finished_at)
    if datetime.fromisoformat(finished_timestamp[:-1] + "+00:00") < datetime.fromisoformat(
        started_timestamp[:-1] + "+00:00"
    ):
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT)

    summary = {
        "schema_version": WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
        "storage_protocol_version": WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
        "run_identity": identity.as_dict(),
        "reporting_policy": "terminal_only",
        "selected_strategy": selected_strategy,
        "plan_fingerprint": plan_fingerprint,
        "limits_profile": WORKFLOW_PROGRESS_SUMMARY_LIMITS_PROFILE,
        "summary_revision": 1,
        "topology_version": None,
        "detail_revision": None,
        "state": outcome,
        "node_counts": {
            "declared": declared_nodes,
            "discovered": 0,
            "retained_topology": 0,
            "retained_detail": 0,
            "pending": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
        },
        "edge_counts": {
            "declared": declared_edges,
            "discovered": 0,
            "retained_topology": 0,
        },
        "progress_percent": 100.0 if outcome == "SUCCEEDED" else 0.0,
        "timestamps": {
            "started_at": started_timestamp,
            "updated_at": finished_timestamp,
            "finished_at": finished_timestamp,
        },
        "detail": {
            "availability": "OMITTED_BY_POLICY",
            "complete": False,
            "truncation_reasons": [],
        },
        "storage": {"kind": "database", "manifest_id": None},
        "retention": {
            "detail_days": detail_days,
            "detail_expires_at": None,
        },
        "terminal": {
            "outcome": outcome,
            "finished_at": finished_timestamp,
        },
    }
    try:
        serialize_workflow_progress_summary(
            summary,
            expected_identity=identity,
        )
    except WorkflowProgressSummaryError as error:
        raise WorkflowProgressPilotError(WorkflowProgressPilotReason.INVALID_SNAPSHOT) from error
    return summary


def publish_terminal_workflow_progress(
    identity: WorkflowRunIdentity,
    snapshot: Any,
    *,
    detail_days: int,
    using: str = "default",
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
) -> WorkflowProgressPilotPublicationResult:
    """Publish one terminal actor snapshot without weakening application success."""
    manifest_id: str | None = None
    try:
        pinned = _pinned_workflow_plan(identity, using=using)
        prepared = prepare_terminal_workflow_progress_publication(
            identity,
            snapshot,
            plan_fingerprint=pinned.fingerprint,
            selected_strategy=pinned.selected_strategy,
            reporting_policy=pinned.reporting_policy,
            detail_days=detail_days,
            limits=limits,
        )
        manifest_id = stage_workflow_progress_topology(
            prepared.topology,
            using=using,
        )
        if manifest_id is None:
            result = WorkflowProgressPilotPublicationResult(
                accepted=False,
                reason=WorkflowProgressPilotReason.STALE_FENCE,
            )
        else:
            publication = persist_workflow_progress_publication(
                identity,
                prepared.summary,
                manifest_id=manifest_id,
                prepared_topology=prepared.topology,
                prepared_detail=prepared.detail,
                using=using,
            )
            result = WorkflowProgressPilotPublicationResult(
                accepted=publication.accepted,
                reason=(
                    WorkflowProgressPilotReason.PUBLISHED
                    if publication.accepted
                    else WorkflowProgressPilotReason.STALE_FENCE
                ),
                summary=publication.summary,
            )
    except WorkflowProgressPilotError as error:
        result = WorkflowProgressPilotPublicationResult(
            accepted=False,
            reason=error.reason,
        )
    except (WorkflowProgressStorageError, WorkflowProgressSummaryError):
        result = WorkflowProgressPilotPublicationResult(
            accepted=False,
            reason=WorkflowProgressPilotReason.PUBLICATION_FAILED,
        )
    except BaseException:
        result = WorkflowProgressPilotPublicationResult(
            accepted=False,
            reason=WorkflowProgressPilotReason.PUBLICATION_FAILED,
        )

    if manifest_id is not None and not result.accepted:
        try:
            discard_workflow_progress_topology_candidate(
                identity,
                manifest_id=manifest_id,
                using=using,
            )
        except BaseException:
            if not result.accepted:
                return WorkflowProgressPilotPublicationResult(
                    accepted=False,
                    reason=WorkflowProgressPilotReason.CANDIDATE_CLEANUP_FAILED,
                )
    return result


__all__ = [
    "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS",
    "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS_PROFILE",
    "PreparedWorkflowProgressPilotPublication",
    "WorkflowProgressPilotError",
    "WorkflowProgressPilotPublicationResult",
    "WorkflowProgressPilotReason",
    "prepare_terminal_only_workflow_progress_summary",
    "prepare_terminal_workflow_progress_publication",
    "publish_terminal_workflow_progress",
]
