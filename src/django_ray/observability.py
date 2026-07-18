"""Workflow graph parsing and opt-in Ray live observability."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured

from django_ray.conf.settings import get_settings
from django_ray.redaction import redact_text, redact_value

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


class WorkflowObservabilityError(RuntimeError):
    """Raised when workflow or Ray observability data cannot be retrieved."""


def get_workflow_progress(execution: RayTaskExecution) -> dict[str, Any] | None:
    """Decode the latest durable workflow progress snapshot."""
    if not execution.progress_data:
        return None
    try:
        progress = json.loads(execution.progress_data)
    except (TypeError, json.JSONDecodeError) as error:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} contains invalid workflow progress JSON"
        ) from error
    if not isinstance(progress, dict):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow progress must be a JSON object"
        )
    return redact_value(progress)


def get_workflow_graph(execution: RayTaskExecution) -> dict[str, Any] | None:
    """Return the UI-ready graph from a durable workflow snapshot."""
    progress = get_workflow_progress(execution)
    if progress is None:
        return None
    graph = progress.get("graph")
    if isinstance(graph, dict):
        return graph

    # Compatibility with the first progress schema, which exposed only nodes.
    nodes = progress.get("nodes", [])
    if not isinstance(nodes, list):
        nodes = []
    return {
        "nodes": nodes,
        "edges": [
            {"source": dependency, "target": node.get("node_id")}
            for node in nodes
            if isinstance(node, dict)
            for dependency in node.get("dependencies", [])
        ],
    }


def get_workflow_node(
    execution: RayTaskExecution,
    node_id: str,
) -> dict[str, Any] | None:
    """Find one node in the latest workflow graph."""
    graph = get_workflow_graph(execution)
    if graph is None:
        return None
    return next(
        (
            node
            for node in graph.get("nodes", [])
            if isinstance(node, dict) and node.get("node_id") == node_id
        ),
        None,
    )


def get_ray_task_state(
    ray_task_id: str,
    *,
    address: str | None = None,
) -> list[dict[str, Any]]:
    """Query Ray's live State API for all attempts of a workflow node."""
    try:
        from ray.util.state import get_task

        result = get_task(
            id=ray_task_id,
            address=_state_api_address(address),
            timeout=_state_api_timeout(),
        )
    except Exception as error:
        raise WorkflowObservabilityError(
            f"Unable to retrieve Ray state for task {ray_task_id}: {error}"
        ) from error

    if result is None:
        return []
    attempts = result if isinstance(result, list) else [result]
    return [
        redact_value(attempt.asdict() if hasattr(attempt, "asdict") else _attempt_to_dict(attempt))
        for attempt in attempts
    ]


def _attempt_to_dict(attempt: Any) -> dict[str, Any]:
    try:
        return dict(attempt)
    except TypeError:
        return vars(attempt) if hasattr(attempt, "__dict__") else {"raw": str(attempt)}


def get_ray_task_logs(
    ray_task_id: str,
    *,
    address: str | None = None,
    tail: int = 200,
) -> dict[str, str]:
    """Retrieve bounded stdout and stderr tails for one live Ray task."""
    if not 1 <= tail <= 1000:
        raise ValueError("tail must be between 1 and 1000")
    try:
        from ray.util.state import get_log

        state_address = _state_api_address(address)
        return {
            suffix: redact_text(
                "".join(
                    get_log(
                        address=state_address,
                        task_id=ray_task_id,
                        tail=tail,
                        suffix=suffix,
                        timeout=_state_api_timeout(),
                        filter_ansi_code=True,
                    )
                )
            )
            for suffix in ("out", "err")
        }
    except Exception as error:
        raise WorkflowObservabilityError(
            f"Unable to retrieve Ray logs for task {ray_task_id}: {error}"
        ) from error


def _state_api_address(address: str | None) -> str | None:
    if address is not None:
        return address
    configured = get_settings().get("RAY_STATE_API_ADDRESS")
    if configured:
        return str(configured)

    try:
        import ray

        if ray.is_initialized():
            return None
    except ImportError:
        pass

    raise ImproperlyConfigured(
        "django-ray: RAY_STATE_API_ADDRESS is required when the current process "
        "is not connected to Ray"
    )


def _state_api_timeout() -> int:
    return int(get_settings().get("RAY_STATE_API_TIMEOUT_SECONDS", 5))


__all__ = [
    "WorkflowObservabilityError",
    "get_ray_task_logs",
    "get_ray_task_state",
    "get_workflow_graph",
    "get_workflow_node",
    "get_workflow_progress",
]
