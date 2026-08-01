"""Fail-closed projection of bounded workflow progress for the Django admin."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

from django_ray.redaction import redact_text
from django_ray.workflow_output_previews import (
    WorkflowOutputPreviewAvailability,
    WorkflowOutputPreviewError,
    unavailable_workflow_output_preview,
    validate_workflow_output_preview,
)
from django_ray.workflow_progress_limits import (
    WORKFLOW_PROGRESS_LABEL_MAX_BYTES,
    WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES,
    WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
)
from django_ray.workflow_progress_summary import (
    WORKFLOW_PROGRESS_STATES,
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    WORKFLOW_PROGRESS_TERMINAL_STATES,
)

ADMIN_WORKFLOW_GRAPH_SCHEMA = "django-ray.admin-workflow-graph"
ADMIN_WORKFLOW_GRAPH_SCHEMA_VERSION = 2
ADMIN_WORKFLOW_GRAPH_MAX_NODES = 100
ADMIN_WORKFLOW_GRAPH_MAX_EDGES = 256
ADMIN_WORKFLOW_GRAPH_MAX_DETAILS = 100
ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES = 128 * 1024
ADMIN_WORKFLOW_GRAPH_ROOT_MESSAGE_MAX_BYTES = 256

_READ_SCHEMA_VERSION = 1
_SUPPORTED_NODE_KINDS = frozenset({"task", "map"})
_SUPPORTED_NODE_STATES = frozenset({"PENDING", "RUNNING", "SUCCEEDED", "FAILED"})
_EXPECTED_TOPOLOGY_NODE_FIELDS = frozenset(
    {
        "node_id",
        "kind",
        "label",
        "callable_path",
        "runtime_env",
        "ray_options",
    }
)
_EXPECTED_TOPOLOGY_EDGE_FIELDS = frozenset({"source", "target"})
_EXPECTED_DETAIL_FIELDS_V1 = frozenset(
    {
        "schema_version",
        "node_id",
        "invocation_identity",
        "state",
        "progress",
        "execution",
        "fanout",
        "started_at",
        "finished_at",
        "error",
        "recent_events",
        "truncated",
    }
)
_EXPECTED_DETAIL_FIELDS_V2 = _EXPECTED_DETAIL_FIELDS_V1 | {"output_preview"}
_EXPECTED_PROGRESS_FIELDS = frozenset(
    {"current", "total", "percent", "message", "metrics", "updated_at"}
)
_EXPECTED_FANOUT_FIELDS = frozenset(
    {
        "max_concurrency",
        "max_items",
        "submitted_items",
        "completed_items",
        "in_flight_items",
        "input_exhausted",
    }
)
_PUBLIC_IDENTITY_FIELDS = frozenset(
    {"schema_version", "run_id", "attempt_number", "execution_generation"}
)
_PUBLICATION_FIELDS = frozenset({"summary_revision", "topology_version", "detail_revision"})
_STATUS_MESSAGES = {
    "AVAILABLE": "Bounded terminal workflow graph is available.",
    "NOT_REPORTED": "A terminal schema-v3 workflow publication is not available yet.",
    "UNSUPPORTED": "Only an unsupported or legacy workflow publication is available.",
    "TRUNCATED": "Workflow detail is incomplete, so no partial graph is shown.",
    "UNAVAILABLE": "Bounded workflow detail is unavailable, so no graph is shown.",
    "LIMIT_EXCEEDED": (
        "Workflow graph exceeds the admin display limits, so no partial graph is shown."
    ),
    "CORRUPT": "Workflow graph data failed validation, so no graph is shown.",
}
_DEGRADED_STATUSES = frozenset(_STATUS_MESSAGES) - {"AVAILABLE"}


class AdminWorkflowGraphError(ValueError):
    """A fixed-shape graph degradation that is safe to expose to operators."""

    def __init__(self, status: str, *, http_status: int = 200) -> None:
        if status not in _DEGRADED_STATUSES:
            raise ValueError("Unsupported admin workflow graph status")
        self.status = status
        self.http_status = http_status
        super().__init__(_STATUS_MESSAGES[status])


@dataclass(frozen=True)
class AdminWorkflowGraphExpectation:
    """Coherence values selected from one terminal schema-v3 summary."""

    task_id: str
    run_identity: dict[str, Any]
    publication: dict[str, int]
    workflow_state: str
    node_count: int
    edge_count: int
    state_counts: dict[str, int]


def _limits() -> dict[str, int]:
    return {
        "nodes": ADMIN_WORKFLOW_GRAPH_MAX_NODES,
        "edges": ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
        "details": ADMIN_WORKFLOW_GRAPH_MAX_DETAILS,
        "response_bytes": ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES,
    }


def degraded_admin_workflow_graph(status: str) -> dict[str, Any]:
    """Return one explicit empty graph instead of any partially verified data."""
    if status not in _DEGRADED_STATUSES:
        raise ValueError("Unsupported degraded admin workflow graph status")
    message = _STATUS_MESSAGES[status]
    if len(message.encode("utf-8")) > ADMIN_WORKFLOW_GRAPH_ROOT_MESSAGE_MAX_BYTES:
        raise AssertionError("Admin workflow graph status message exceeds its fixed bound")
    return {
        "schema": ADMIN_WORKFLOW_GRAPH_SCHEMA,
        "schema_version": ADMIN_WORKFLOW_GRAPH_SCHEMA_VERSION,
        "status": status,
        "message": message,
        "complete": False,
        "counts": {"nodes": 0, "edges": 0},
        "limits": _limits(),
        "nodes": [],
        "edges": [],
    }


def _corrupt() -> NoReturn:
    raise AdminWorkflowGraphError("CORRUPT", http_status=503)


def _counter(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _corrupt()
    return value


def _identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PUBLIC_IDENTITY_FIELDS:
        _corrupt()
    run_id = value.get("run_id")
    try:
        canonical_run_id = str(UUID(run_id)) if isinstance(run_id, str) else None
    except ValueError:
        canonical_run_id = None
    if (
        value.get("schema_version") != 1
        or canonical_run_id != run_id
        or _counter(value.get("attempt_number"), minimum=1) < 1
        or _counter(value.get("execution_generation")) < 0
    ):
        _corrupt()
    return dict(value)


def _publication(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != _PUBLICATION_FIELDS:
        _corrupt()
    normalized = {field: _counter(value.get(field), minimum=1) for field in _PUBLICATION_FIELDS}
    return normalized


def _count_mapping(
    value: Any,
    fields: tuple[str, ...],
) -> tuple[dict[str, int], int | None]:
    expected_fields = frozenset((*fields, "declared"))
    if not isinstance(value, dict) or set(value) != expected_fields:
        _corrupt()
    declared = value["declared"]
    if declared is not None:
        declared = _counter(declared)
    return ({field: _counter(value[field]) for field in fields}, declared)


def inspect_admin_workflow_graph_summary(
    envelope: Any,
) -> AdminWorkflowGraphExpectation:
    """Validate graph readiness without touching topology or node-detail storage."""
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != "django-ray.workflow-progress-summary"
        or envelope.get("schema_version") != _READ_SCHEMA_VERSION
    ):
        _corrupt()

    source_schema = envelope.get("source_schema_version")
    if source_schema is None:
        raise AdminWorkflowGraphError("NOT_REPORTED")
    if source_schema != WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION:
        raise AdminWorkflowGraphError("UNSUPPORTED")

    summary = envelope.get("summary")
    if not isinstance(summary, dict):
        _corrupt()
    workflow_state = summary.get("state")
    if not isinstance(workflow_state, str) or workflow_state not in WORKFLOW_PROGRESS_STATES:
        _corrupt()
    if workflow_state not in WORKFLOW_PROGRESS_TERMINAL_STATES:
        raise AdminWorkflowGraphError("NOT_REPORTED")

    availability = envelope.get("availability")
    if availability == "TRUNCATED":
        raise AdminWorkflowGraphError("TRUNCATED")
    if availability in {
        "NOT_REPORTED",
    }:
        raise AdminWorkflowGraphError("NOT_REPORTED")
    if availability in {
        "DISABLED",
        "OMITTED_BY_POLICY",
        "EXPIRED",
        "MISSING",
    }:
        raise AdminWorkflowGraphError("UNAVAILABLE")
    if availability != "AVAILABLE":
        _corrupt()

    detail = summary.get("detail")
    if not isinstance(detail, dict):
        _corrupt()
    truncation_reasons = detail.get("truncation_reasons")
    if (
        envelope.get("complete") is not True
        or detail.get("availability") != "AVAILABLE"
        or detail.get("complete") is not True
        or not isinstance(truncation_reasons, list)
        or any(not isinstance(reason, str) for reason in truncation_reasons)
    ):
        _corrupt()
    if truncation_reasons:
        raise AdminWorkflowGraphError("TRUNCATED")

    task_id = envelope.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        _corrupt()
    run_identity = _identity(envelope.get("run_identity"))
    if summary.get("run_identity") != run_identity:
        _corrupt()
    publication = _publication(envelope.get("publication"))
    if any(summary.get(field) != value for field, value in publication.items()):
        _corrupt()
    timestamps = summary.get("timestamps")
    if (
        not isinstance(timestamps, dict)
        or summary.get("schema_version") != WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION
        or summary.get("terminal")
        != {
            "outcome": workflow_state,
            "finished_at": timestamps.get("finished_at"),
        }
    ):
        _corrupt()

    node_counts, declared_nodes = _count_mapping(
        summary.get("node_counts"),
        (
            "discovered",
            "retained_topology",
            "retained_detail",
            "pending",
            "running",
            "succeeded",
            "failed",
        ),
    )
    edge_counts, declared_edges = _count_mapping(
        summary.get("edge_counts"),
        ("discovered", "retained_topology"),
    )
    node_count = node_counts["discovered"]
    edge_count = edge_counts["discovered"]
    if node_count < 1:
        _corrupt()
    if sum(node_counts[state] for state in ("pending", "running", "succeeded", "failed")) != (
        node_count
    ):
        _corrupt()
    if (
        node_counts["retained_topology"] != node_count
        or node_counts["retained_detail"] != node_count
        or edge_counts["retained_topology"] != edge_count
    ):
        raise AdminWorkflowGraphError("TRUNCATED")

    for declared, discovered in (
        (declared_nodes, node_count),
        (declared_edges, edge_count),
    ):
        if declared is not None:
            if declared < discovered:
                _corrupt()
            if declared != discovered:
                raise AdminWorkflowGraphError("TRUNCATED")

    if (
        node_count > ADMIN_WORKFLOW_GRAPH_MAX_NODES
        or node_count > ADMIN_WORKFLOW_GRAPH_MAX_DETAILS
        or edge_count > ADMIN_WORKFLOW_GRAPH_MAX_EDGES
    ):
        raise AdminWorkflowGraphError("LIMIT_EXCEEDED")

    return AdminWorkflowGraphExpectation(
        task_id=task_id,
        run_identity=run_identity,
        publication=publication,
        workflow_state=workflow_state,
        node_count=node_count,
        edge_count=edge_count,
        state_counts={
            state.upper(): node_counts[state]
            for state in ("pending", "running", "succeeded", "failed")
        },
    )


def _page_items(
    page: Any,
    expectation: AdminWorkflowGraphExpectation,
    *,
    collection: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    if (
        not isinstance(page, dict)
        or page.get("schema") != "django-ray.workflow-progress-page"
        or page.get("schema_version") != _READ_SCHEMA_VERSION
        or page.get("collection") != collection
        or page.get("task_id") != expectation.task_id
        or page.get("run_identity") != expectation.run_identity
        or page.get("publication") != expectation.publication
    ):
        _corrupt()
    availability = page.get("availability")
    if availability == "TRUNCATED" or page.get("next_cursor") is not None:
        raise AdminWorkflowGraphError("TRUNCATED")
    if availability in {
        "NOT_REPORTED",
        "DISABLED",
        "OMITTED_BY_POLICY",
        "EXPIRED",
        "MISSING",
    }:
        raise AdminWorkflowGraphError("UNAVAILABLE")
    if availability != "AVAILABLE" or page.get("complete") is not True:
        _corrupt()
    items = page.get("items")
    returned_count = page.get("returned_count")
    if (
        type(returned_count) is not int
        or not isinstance(items, list)
        or returned_count != len(items)
        or returned_count != expected_count
        or any(not isinstance(item, dict) for item in items)
    ):
        _corrupt()
    return items


def _bounded_redacted_text(
    value: Any,
    *,
    max_bytes: int,
    nullable: bool = False,
) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        _corrupt()
    try:
        normalized = redact_text(value)
        encoded = normalized.encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        _corrupt()
    if len(encoded) <= max_bytes:
        return normalized
    suffix = "... [truncated]"
    prefix_bytes = encoded[: max_bytes - len(suffix.encode("utf-8"))]
    return prefix_bytes.decode("utf-8", errors="ignore") + suffix


def _node_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _corrupt()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _corrupt()
    if len(encoded) > WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES:
        _corrupt()
    return value


def _safe_fanout(value: Any) -> dict[str, int | bool]:
    if not isinstance(value, dict) or set(value) != _EXPECTED_FANOUT_FIELDS:
        _corrupt()
    submitted = _counter(value.get("submitted_items"))
    completed = _counter(value.get("completed_items"))
    in_flight = _counter(value.get("in_flight_items"))
    input_exhausted = value.get("input_exhausted")
    if (
        type(input_exhausted) is not bool
        or completed > submitted
        or in_flight != submitted - completed
    ):
        _corrupt()
    return {
        "submitted_items": submitted,
        "completed_items": completed,
        "in_flight_items": in_flight,
        "input_exhausted": input_exhausted,
    }


def _project_nodes(
    topology_items: list[dict[str, Any]],
    detail_items: list[dict[str, Any]],
    expectation: AdminWorkflowGraphExpectation,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    topology_by_id: dict[str, dict[str, Any]] = {}
    for item in topology_items:
        if set(item) != _EXPECTED_TOPOLOGY_NODE_FIELDS:
            _corrupt()
        node_id = _node_id(item.get("node_id"))
        kind = item.get("kind")
        if kind not in _SUPPORTED_NODE_KINDS or node_id in topology_by_id:
            _corrupt()
        topology_by_id[node_id] = item

    detail_by_id: dict[str, dict[str, Any]] = {}
    state_by_id: dict[str, str] = {}
    for item in detail_items:
        item_fields = frozenset(item)
        if item_fields not in {_EXPECTED_DETAIL_FIELDS_V1, _EXPECTED_DETAIL_FIELDS_V2}:
            _corrupt()
        node_id = _node_id(item.get("node_id"))
        state = item.get("state")
        if (
            state not in _SUPPORTED_NODE_STATES
            or node_id in detail_by_id
            or type(item.get("truncated")) is not bool
        ):
            _corrupt()
        if item["truncated"]:
            raise AdminWorkflowGraphError("TRUNCATED")
        if not (
            item.get("invocation_identity") is None
            or isinstance(item.get("invocation_identity"), dict)
        ):
            _corrupt()
        if not (item.get("execution") is None or isinstance(item.get("execution"), dict)):
            _corrupt()
        if not isinstance(item.get("recent_events"), list):
            _corrupt()
        detail_by_id[node_id] = item
        state_by_id[node_id] = state

    if set(topology_by_id) != set(detail_by_id):
        _corrupt()
    actual_state_counts = {
        state: sum(item_state == state for item_state in state_by_id.values())
        for state in _SUPPORTED_NODE_STATES
    }
    if actual_state_counts != expectation.state_counts:
        _corrupt()

    projected: dict[str, dict[str, Any]] = {}
    for node_id, topology in topology_by_id.items():
        detail = detail_by_id[node_id]
        detail_schema_version = detail["schema_version"]
        if (
            type(detail_schema_version) is int
            and detail_schema_version == 1
            and frozenset(detail) == _EXPECTED_DETAIL_FIELDS_V1
        ):
            output_preview = unavailable_workflow_output_preview(
                WorkflowOutputPreviewAvailability.UNAVAILABLE
            )
        elif (
            type(detail_schema_version) is int
            and detail_schema_version == 2
            and frozenset(detail) == _EXPECTED_DETAIL_FIELDS_V2
        ):
            try:
                output_preview = validate_workflow_output_preview(detail["output_preview"])
            except WorkflowOutputPreviewError:
                _corrupt()
        else:
            _corrupt()
        progress = detail.get("progress")
        if progress is not None and (
            not isinstance(progress, dict) or set(progress) != _EXPECTED_PROGRESS_FIELDS
        ):
            _corrupt()
        if isinstance(progress, dict) and not isinstance(progress.get("metrics"), dict):
            _corrupt()
        message = (
            None
            if progress is None
            else _bounded_redacted_text(
                progress.get("message"),
                max_bytes=WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES,
                nullable=True,
            )
        )
        error = _bounded_redacted_text(
            detail.get("error"),
            max_bytes=WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES,
            nullable=True,
        )
        state = state_by_id[node_id]
        if (state == "FAILED") != (error is not None):
            _corrupt()

        kind = topology["kind"]
        fanout = detail.get("fanout")
        if (kind == "map") != (fanout is not None):
            _corrupt()
        node: dict[str, Any] = {
            "id": node_id,
            "label": _bounded_redacted_text(
                topology.get("label"),
                max_bytes=WORKFLOW_PROGRESS_LABEL_MAX_BYTES,
            ),
            "kind": kind,
            "state": state,
            "message": message,
            "error": error,
            "failure_path": False,
            "output_preview": output_preview,
        }
        if kind == "map":
            node["fanout"] = _safe_fanout(fanout)
        projected[node_id] = node
    return projected, state_by_id


def _project_edges(
    items: list[dict[str, Any]],
    *,
    known_nodes: set[str],
) -> tuple[list[dict[str, str]], dict[str, set[str]], dict[str, set[str]]]:
    edges: list[dict[str, str]] = []
    predecessors = {node_id: set() for node_id in known_nodes}
    successors = {node_id: set() for node_id in known_nodes}
    seen: set[tuple[str, str]] = set()
    for item in items:
        if set(item) != _EXPECTED_TOPOLOGY_EDGE_FIELDS:
            _corrupt()
        source = _node_id(item.get("source"))
        target = _node_id(item.get("target"))
        key = (source, target)
        if (
            source == target
            or source not in known_nodes
            or target not in known_nodes
            or key in seen
        ):
            _corrupt()
        seen.add(key)
        successors[source].add(target)
        predecessors[target].add(source)
        edges.append({"source": source, "target": target})
    return edges, predecessors, successors


def _topological_order(
    node_ids: set[str],
    predecessors: dict[str, set[str]],
    successors: dict[str, set[str]],
) -> list[str]:
    indegree = {node_id: len(predecessors[node_id]) for node_id in node_ids}
    ready = [node_id for node_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        ordered.append(node_id)
        for target in sorted(successors[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    if len(ordered) != len(node_ids):
        _corrupt()
    return ordered


def _failure_path(
    state_by_id: dict[str, str],
    predecessors: dict[str, set[str]],
) -> set[str]:
    failed = {node_id for node_id, state in state_by_id.items() if state == "FAILED"}
    origins = {
        node_id
        for node_id in failed
        if not any(parent in failed for parent in predecessors[node_id])
    }
    path: set[str] = set()
    pending = list(origins)
    while pending:
        node_id = pending.pop()
        if node_id in path:
            continue
        path.add(node_id)
        pending.extend(predecessors[node_id])
    return path


def build_admin_workflow_graph(
    expectation: AdminWorkflowGraphExpectation,
    *,
    topology_nodes: Any,
    topology_edges: Any,
    node_details: Any,
) -> dict[str, Any]:
    """Build one atomic allowlisted graph from exactly three first-page reads."""
    topology_items = _page_items(
        topology_nodes,
        expectation,
        collection="topology_nodes",
        expected_count=expectation.node_count,
    )
    edge_items = _page_items(
        topology_edges,
        expectation,
        collection="topology_edges",
        expected_count=expectation.edge_count,
    )
    detail_items = _page_items(
        node_details,
        expectation,
        collection="node_details",
        expected_count=expectation.node_count,
    )
    projected, state_by_id = _project_nodes(topology_items, detail_items, expectation)
    edges, predecessors, successors = _project_edges(
        edge_items,
        known_nodes=set(projected),
    )
    ordered_ids = _topological_order(set(projected), predecessors, successors)
    failure_path = _failure_path(state_by_id, predecessors)
    for node_id in failure_path:
        projected[node_id]["failure_path"] = True

    return {
        "schema": ADMIN_WORKFLOW_GRAPH_SCHEMA,
        "schema_version": ADMIN_WORKFLOW_GRAPH_SCHEMA_VERSION,
        "status": "AVAILABLE",
        "message": _STATUS_MESSAGES["AVAILABLE"],
        "complete": True,
        "counts": {
            "nodes": expectation.node_count,
            "edges": expectation.edge_count,
        },
        "limits": _limits(),
        "nodes": [projected[node_id] for node_id in ordered_ids],
        "edges": sorted(edges, key=lambda edge: (edge["source"], edge["target"])),
    }


__all__ = [
    "ADMIN_WORKFLOW_GRAPH_MAX_DETAILS",
    "ADMIN_WORKFLOW_GRAPH_MAX_EDGES",
    "ADMIN_WORKFLOW_GRAPH_MAX_NODES",
    "ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES",
    "ADMIN_WORKFLOW_GRAPH_SCHEMA",
    "ADMIN_WORKFLOW_GRAPH_SCHEMA_VERSION",
    "AdminWorkflowGraphExpectation",
    "AdminWorkflowGraphError",
    "build_admin_workflow_graph",
    "degraded_admin_workflow_graph",
    "inspect_admin_workflow_graph_summary",
]
