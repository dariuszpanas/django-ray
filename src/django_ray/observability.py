"""Versioned durable and opt-in live observability services."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count, Min, Q

from django_ray.conf.settings import get_settings
from django_ray.redaction import redact_text, redact_value
from django_ray.workflow_plans import (
    PLAN_DOMAIN_SEPARATOR,
    PLAN_FORMAT,
    PLAN_FORMAT_VERSION,
    WorkflowPlanValidationError,
    validate_plan_selection_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django_ray.models import RayTaskExecution


OBSERVABILITY_SCHEMA_VERSION = 1
DEFAULT_DIAGNOSTIC_MAX_CHARS = 4096
DEFAULT_RAY_LOG_MAX_BYTES = 64 * 1024
MAX_RAY_LOG_MAX_BYTES = 1024 * 1024


class WorkflowObservabilityError(RuntimeError):
    """Raised when workflow or Ray observability data cannot be retrieved."""


def _isoformat(value: datetime | None) -> str | None:
    """Return a stable UTC ISO-8601 representation for a model timestamp."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _versioned(schema: str, *, generated_at: datetime | None = None) -> dict[str, Any]:
    """Build the common versioned response envelope."""
    return {
        "schema": f"django-ray.{schema}",
        "schema_version": OBSERVABILITY_SCHEMA_VERSION,
        "generated_at": _isoformat(generated_at or datetime.now(UTC)),
    }


def _bounded_diagnostic(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    redacted = redact_text(value)
    if len(redacted) <= DEFAULT_DIAGNOSTIC_MAX_CHARS:
        return redacted, False
    marker = "... [truncated]"
    return redacted[: DEFAULT_DIAGNOSTIC_MAX_CHARS - len(marker)] + marker, True


def get_task_summary(
    execution: RayTaskExecution,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a redacted operational summary without task payloads or topology."""
    workflow_revision = None
    try:
        progress = get_workflow_progress(execution)
        if progress is not None and isinstance(progress.get("revision"), int):
            workflow_revision = progress["revision"]
    except WorkflowObservabilityError:
        pass

    try:
        plan_selection = _workflow_plan_selection(execution)
    except WorkflowObservabilityError:
        plan_selection = None
    error_message, error_message_truncated = _bounded_diagnostic(execution.error_message)
    return {
        **_versioned("task-summary", generated_at=generated_at),
        "id": execution.pk,
        "task_id": execution.task_id,
        "callable_path": execution.callable_path,
        "queue_name": execution.queue_name,
        "priority": execution.priority,
        "state": execution.state,
        "attempt_number": execution.attempt_number,
        "execution_generation": execution.execution_generation,
        "workflow_run_id": (
            str(execution.workflow_run_id) if execution.workflow_run_id is not None else None
        ),
        "created_at": _isoformat(execution.created_at),
        "run_after": _isoformat(execution.run_after),
        "started_at": _isoformat(execution.started_at),
        "finished_at": _isoformat(execution.finished_at),
        "last_heartbeat_at": _isoformat(execution.last_heartbeat_at),
        "claimed_by_worker": execution.claimed_by_worker,
        "ray_job_id": execution.ray_job_id,
        "runtime_env_profile": execution.runtime_env_profile,
        "runtime_env_hash": execution.runtime_env_hash,
        "workflow_plan_fingerprint": execution.workflow_plan_fingerprint or None,
        "workflow_plan_pinned_attempt": execution.workflow_plan_pinned_attempt,
        "workflow_selected_strategy": (
            plan_selection.get("selected_strategy") if plan_selection is not None else None
        ),
        "workflow_revision": workflow_revision,
        "error_message": error_message,
        "error_message_truncated": error_message_truncated,
    }


def get_workflow_plan(execution: RayTaskExecution) -> dict[str, Any] | None:
    """Return the verified, secret-free effective plan and selection metadata."""
    fingerprint = execution.workflow_plan_fingerprint
    serialized = execution.workflow_plan_json
    if not fingerprint and not serialized:
        return None
    if not fingerprint or not serialized:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} has an incomplete workflow plan snapshot"
        )
    expected = (
        f"sha256:{hashlib.sha256(PLAN_DOMAIN_SEPARATOR + serialized.encode('utf-8')).hexdigest()}"
    )
    if fingerprint != expected:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan fingerprint does not match its snapshot"
        )
    try:
        manifest = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as error:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} contains invalid workflow plan JSON"
        ) from error
    if not isinstance(manifest, dict):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan must be a JSON object"
        )
    if (
        manifest.get("plan_format") != PLAN_FORMAT
        or manifest.get("plan_format_version") != PLAN_FORMAT_VERSION
    ):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan has an unsupported format version"
        )
    return redact_value(
        {
            "fingerprint": fingerprint,
            "manifest": manifest,
            "selection": _workflow_plan_selection(execution),
        }
    )


def _workflow_plan_selection(execution: RayTaskExecution) -> dict[str, Any] | None:
    if not execution.workflow_plan_selection:
        return None
    try:
        selection = json.loads(execution.workflow_plan_selection)
    except (TypeError, json.JSONDecodeError) as error:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} contains invalid workflow plan selection JSON"
        ) from error
    if not isinstance(selection, dict):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan selection must be a JSON object"
        )
    try:
        validated = validate_plan_selection_manifest(selection)
    except WorkflowPlanValidationError as error:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan selection has an invalid schema"
        ) from error
    return redact_value(validated)


def get_queue_depths(*, generated_at: datetime | None = None) -> dict[str, Any]:
    """Return ready, delayed, and running counts for every observed queue."""
    from django_ray.models import RayTaskExecution, TaskState

    observed_at = generated_at or datetime.now(UTC)
    rows = (
        RayTaskExecution.objects.filter(state__in=[TaskState.QUEUED, TaskState.RUNNING])
        .values("queue_name")
        .annotate(
            queued=Count("pk", filter=Q(state=TaskState.QUEUED)),
            ready=Count(
                "pk",
                filter=Q(state=TaskState.QUEUED)
                & (Q(run_after__isnull=True) | Q(run_after__lte=observed_at)),
            ),
            delayed=Count(
                "pk",
                filter=Q(state=TaskState.QUEUED, run_after__gt=observed_at),
            ),
            running=Count("pk", filter=Q(state=TaskState.RUNNING)),
            oldest_queued_at=Min("created_at", filter=Q(state=TaskState.QUEUED)),
        )
        .order_by("queue_name")
    )
    queues = [
        {
            "queue_name": row["queue_name"],
            "queued": row["queued"],
            "ready": row["ready"],
            "delayed": row["delayed"],
            "running": row["running"],
            "oldest_queued_at": _isoformat(row["oldest_queued_at"]),
        }
        for row in rows
    ]
    return {
        **_versioned("queue-depths", generated_at=observed_at),
        "queues": queues,
    }


def _duration_seconds(started_at: datetime | None, finished_at: datetime | None) -> float | None:
    if started_at is None or finished_at is None:
        return None
    return max(0.0, (finished_at - started_at).total_seconds())


def _attempt_summary(attempt: Any, *, current: bool) -> dict[str, Any]:
    error_message, error_message_truncated = _bounded_diagnostic(attempt.error_message)
    return {
        "attempt_number": attempt.attempt_number,
        "state": attempt.state,
        "started_at": _isoformat(attempt.started_at),
        "finished_at": _isoformat(attempt.finished_at),
        "duration_seconds": _duration_seconds(attempt.started_at, attempt.finished_at),
        "error_message": error_message,
        "error_message_truncated": error_message_truncated,
        "current": current,
    }


def get_attempt_history(
    execution: RayTaskExecution,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return archived attempts plus the unarchived current execution snapshot."""
    attempts = list(execution.attempts.all())
    archived_numbers = {attempt.attempt_number for attempt in attempts}
    summaries = [_attempt_summary(attempt, current=False) for attempt in attempts]
    if execution.attempt_number not in archived_numbers:
        summaries.append(_attempt_summary(execution, current=True))
    summaries.sort(key=lambda attempt: attempt["attempt_number"])
    return {
        **_versioned("attempt-history", generated_at=generated_at),
        "task_id": execution.task_id,
        "current_attempt_number": execution.attempt_number,
        "attempts": summaries,
    }


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
    schema_version = progress.get("schema_version", 1)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow progress has an invalid schema version"
        )
    if schema_version >= 2:
        identity = progress.get("run_identity")
        if not isinstance(identity, dict):
            raise WorkflowObservabilityError(
                f"Task {execution.task_id} workflow progress must contain a run identity"
            )
        expected_identity = {
            "run_id": (
                str(execution.workflow_run_id) if execution.workflow_run_id is not None else None
            ),
            "task_execution_pk": execution.pk,
            "attempt_number": execution.attempt_number,
            "execution_generation": execution.execution_generation,
        }
        if any(identity.get(key) != value for key, value in expected_identity.items()):
            raise WorkflowObservabilityError(
                f"Task {execution.task_id} workflow progress belongs to another run"
            )
    return redact_value(progress)


def get_workflow_graph(execution: RayTaskExecution) -> dict[str, Any] | None:
    """Return the UI-ready graph from a durable workflow snapshot."""
    progress = get_workflow_progress(execution)
    if progress is None:
        return None
    graph = progress.get("graph")
    if isinstance(graph, dict):
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise WorkflowObservabilityError(
                f"Task {execution.task_id} workflow graph must contain node and edge lists"
            )
        return {**graph, "nodes": nodes, "edges": edges}

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
            for dependency in (
                node.get("dependencies", [])
                if isinstance(node.get("dependencies", []), list)
                else []
            )
        ],
    }


def get_workflow_snapshot(
    execution: RayTaskExecution,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Wrap the latest durable workflow snapshot in the package service schema."""
    return {
        **_versioned("workflow-snapshot", generated_at=generated_at),
        "task_id": execution.task_id,
        "task_state": execution.state,
        "attempt_number": execution.attempt_number,
        "execution_generation": execution.execution_generation,
        "workflow_run_id": (
            str(execution.workflow_run_id) if execution.workflow_run_id is not None else None
        ),
        "plan": get_workflow_plan(execution),
        "workflow": get_workflow_progress(execution),
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
        raise WorkflowObservabilityError("Ray State API is unavailable") from error

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
        return vars(attempt) if hasattr(attempt, "__dict__") else {"record": "unsupported"}


def _bounded_log_text(chunks: Iterable[str], *, max_bytes: int) -> tuple[str, bool]:
    """Consume at most ``max_bytes`` of one Ray log stream."""
    content = bytearray()
    for chunk in chunks:
        encoded = str(chunk).encode("utf-8", errors="replace")
        remaining = max_bytes + 1 - len(content)
        if len(encoded) > remaining:
            content.extend(encoded[:remaining])
            break
        content.extend(encoded)
        if len(content) > max_bytes:
            break
    truncated = len(content) > max_bytes
    text = content[:max_bytes].decode("utf-8", errors="ignore")
    # A truncated prefix may split a secret-bearing token before the redaction
    # expression can recognize it. Do not expose partial content in that case.
    redacted = "[TRUNCATED]" if truncated else redact_text(text)
    redacted_bytes = redacted.encode("utf-8")
    if len(redacted_bytes) > max_bytes:
        redacted = redacted_bytes[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True
    return redacted, truncated


def _validate_log_bounds(*, tail: int, max_bytes: int) -> None:
    if not 1 <= tail <= 1000:
        raise ValueError("tail must be between 1 and 1000")
    if not 1 <= max_bytes <= MAX_RAY_LOG_MAX_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_RAY_LOG_MAX_BYTES}")


def _get_ray_task_logs_with_metadata(
    ray_task_id: str,
    *,
    address: str | None,
    tail: int,
    max_bytes: int,
) -> tuple[dict[str, str], dict[str, bool]]:
    from ray.util.state import get_log

    state_address = _state_api_address(address)
    logs: dict[str, str] = {}
    truncated: dict[str, bool] = {}
    for suffix in ("out", "err"):
        logs[suffix], truncated[suffix] = _bounded_log_text(
            get_log(
                address=state_address,
                task_id=ray_task_id,
                tail=tail,
                suffix=suffix,
                timeout=_state_api_timeout(),
                filter_ansi_code=True,
            ),
            max_bytes=max_bytes,
        )
    return logs, truncated


def get_ray_task_logs(
    ray_task_id: str,
    *,
    address: str | None = None,
    tail: int = 200,
    max_bytes: int = DEFAULT_RAY_LOG_MAX_BYTES,
) -> dict[str, str]:
    """Retrieve redacted stdout and stderr bounded by lines and UTF-8 bytes."""
    _validate_log_bounds(tail=tail, max_bytes=max_bytes)
    try:
        logs, _truncated = _get_ray_task_logs_with_metadata(
            ray_task_id,
            address=address,
            tail=tail,
            max_bytes=max_bytes,
        )
        return logs
    except Exception as error:
        raise WorkflowObservabilityError("Ray Log API is unavailable") from error


def get_workflow_node_snapshot(
    execution: RayTaskExecution,
    node_id: str,
    *,
    include_live: bool = False,
    include_logs: bool = False,
    tail: int = 200,
    max_log_bytes: int = DEFAULT_RAY_LOG_MAX_BYTES,
    generated_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return durable node data first and add best-effort live Ray details."""
    node = get_workflow_node(execution, node_id)
    if node is None:
        return None

    progress = get_workflow_progress(execution) or {}
    live: dict[str, Any] = {
        "status": "not_requested",
        "reason": None,
        "ray_state": None,
        "logs": None,
        "logs_truncated": None,
    }
    if include_live or include_logs:
        if include_logs:
            _validate_log_bounds(tail=tail, max_bytes=max_log_bytes)
        execution_metadata = node.get("execution")
        ray_task_id = (
            execution_metadata.get("ray_task_id") if isinstance(execution_metadata, dict) else None
        )
        if not ray_task_id:
            live.update(status="unavailable", reason="ray_task_id_unavailable")
        else:
            try:
                live["ray_state"] = get_ray_task_state(str(ray_task_id))
                live["status"] = "available"
            except Exception:
                live.update(status="unavailable", reason="state_api_unavailable")
            if include_logs and live["status"] == "available":
                try:
                    logs, truncated = _get_ray_task_logs_with_metadata(
                        str(ray_task_id),
                        address=None,
                        tail=tail,
                        max_bytes=max_log_bytes,
                    )
                    live["logs"] = logs
                    live["logs_truncated"] = truncated
                except Exception:
                    live.update(status="partial", reason="log_api_unavailable")

    return {
        **_versioned("workflow-node-snapshot", generated_at=generated_at),
        "task_id": execution.task_id,
        "task_state": execution.state,
        "attempt_number": execution.attempt_number,
        "execution_generation": execution.execution_generation,
        "workflow_run_id": (
            str(execution.workflow_run_id) if execution.workflow_run_id is not None else None
        ),
        "workflow_revision": progress.get("revision", 0),
        "node": node,
        "live": live,
    }


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
    "DEFAULT_DIAGNOSTIC_MAX_CHARS",
    "DEFAULT_RAY_LOG_MAX_BYTES",
    "MAX_RAY_LOG_MAX_BYTES",
    "OBSERVABILITY_SCHEMA_VERSION",
    "WorkflowObservabilityError",
    "get_attempt_history",
    "get_queue_depths",
    "get_ray_task_logs",
    "get_ray_task_state",
    "get_task_summary",
    "get_workflow_graph",
    "get_workflow_node",
    "get_workflow_node_snapshot",
    "get_workflow_plan",
    "get_workflow_progress",
    "get_workflow_snapshot",
]
