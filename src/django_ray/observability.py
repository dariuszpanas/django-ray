"""Versioned durable and opt-in live observability services."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count, Min, Q

from django_ray.conf.settings import get_settings
from django_ray.redaction import redact_text, redact_value
from django_ray.workflow.plans import (
    PLAN_DOMAIN_SEPARATOR,
    PLAN_FORMAT,
    PLAN_FORMAT_VERSION,
    WorkflowPlanValidationError,
    effective_plan_selection_reporting_policy,
    validate_plan_selection_manifest,
)
from django_ray.workflow.progress.runs import WorkflowProgressReadSource, read_workflow_progress
from django_ray.workflow.progress.summary import (
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    public_workflow_progress_summary,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django_ray.models import RayTaskExecution


OBSERVABILITY_SCHEMA_VERSION = 1
DEFAULT_DIAGNOSTIC_MAX_CHARS = 4096
DEFAULT_RAY_LOG_MAX_BYTES = 64 * 1024
MAX_RAY_LOG_MAX_BYTES = 1024 * 1024
_STABLE_REJECTION_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class WorkflowObservabilityError(RuntimeError):
    """Raised when workflow or Ray observability data cannot be retrieved."""


class _WorkflowProgressNotRead:
    """Sentinel distinguishing an absent summary from one not read yet."""


_WORKFLOW_PROGRESS_NOT_READ = _WorkflowProgressNotRead()


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
    include_workflow_plan_selection: bool = True,
    workflow_progress: dict[str, Any] | None | _WorkflowProgressNotRead = (
        _WORKFLOW_PROGRESS_NOT_READ
    ),
) -> dict[str, Any]:
    """Return a redacted operational summary without task payloads or topology.

    Callers that already performed the bounded progress read may pass its result to
    avoid a second database round trip. High-frequency callers may also skip plan
    selection fields so a deferred raw selection snapshot is never loaded.
    """
    workflow_revision = None
    if isinstance(workflow_progress, _WorkflowProgressNotRead):
        try:
            progress = get_workflow_progress(execution)
        except WorkflowObservabilityError:
            progress = None
    else:
        progress = workflow_progress
    if progress is not None:
        revision_field = (
            "summary_revision"
            if progress.get("schema_version") == WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION
            else "revision"
        )
        if isinstance(progress.get(revision_field), int):
            workflow_revision = progress[revision_field]

    plan_selection = None
    if include_workflow_plan_selection:
        try:
            plan_selection = _validated_workflow_plan_selection(execution)
        except WorkflowObservabilityError:
            plan_selection = None
    selected_strategy = (
        redact_value(plan_selection.get("selected_strategy"))
        if plan_selection is not None
        else None
    )
    reporting_policy = (
        redact_value(effective_plan_selection_reporting_policy(plan_selection))
        if plan_selection is not None
        else None
    )
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
        "workflow_selected_strategy": selected_strategy,
        "workflow_reporting_policy": reporting_policy,
        "workflow_revision": workflow_revision,
        "error_message": error_message,
        "error_message_truncated": error_message_truncated,
    }


def _validated_workflow_plan_snapshot(
    execution: RayTaskExecution,
) -> dict[str, Any] | None:
    """Return verified unredacted plan data for internal comparisons."""
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
    except (TypeError, RecursionError, json.JSONDecodeError) as error:
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
    return {
        "fingerprint": fingerprint,
        "manifest": manifest,
        "selection": _validated_workflow_plan_selection(execution),
    }


def get_workflow_plan(execution: RayTaskExecution) -> dict[str, Any] | None:
    """Return the verified, secret-free effective plan and selection metadata."""
    snapshot = _validated_workflow_plan_snapshot(execution)
    return redact_value(snapshot) if snapshot is not None else None


def _empty_workflow_plan_diagnostics(status: str) -> dict[str, Any]:
    """Return the fixed compact workflow-plan presentation shape."""
    return {
        "status": status,
        "definition_name": None,
        "definition_revision": None,
        "topology_class": None,
        "declared_node_count": None,
        "retry_safe": None,
        "fingerprint": None,
        "fingerprint_compact": None,
        "requested_policy": None,
        "selected_strategy": None,
        "reporting_policy": None,
        "eligible_strategies": [],
        "rejection_counts": {},
        "retained_rejections": 0,
        "total_rejections": 0,
        "unretained_rejections": 0,
    }


def get_workflow_plan_diagnostics(execution: RayTaskExecution) -> dict[str, Any]:
    """Return compact verified plan diagnostics without rejection paths or messages."""
    stored_components = (
        execution.workflow_plan_fingerprint,
        execution.workflow_plan_json,
        execution.workflow_plan_selection,
    )
    if all(component is None for component in stored_components):
        return _empty_workflow_plan_diagnostics("NOT_RECORDED")
    if any(not isinstance(component, str) or not component for component in stored_components):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} has an incomplete workflow plan snapshot"
        )

    workflow_plan = _validated_workflow_plan_snapshot(execution)
    if workflow_plan is None:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} has an incomplete workflow plan snapshot"
        )

    manifest = workflow_plan.get("manifest")
    selection = workflow_plan.get("selection")
    fingerprint = workflow_plan.get("fingerprint")
    if not isinstance(manifest, dict) or selection is None or not isinstance(fingerprint, str):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} has an incomplete workflow plan snapshot"
        )

    definition = manifest.get("definition")
    topology = manifest.get("topology")
    nodes = manifest.get("nodes")
    retry_safety = manifest.get("retry_safety")
    if (
        not isinstance(definition, dict)
        or not isinstance(definition.get("name"), str)
        or not definition["name"]
        or not isinstance(definition.get("revision"), str)
        or not definition["revision"]
        or not isinstance(topology, dict)
        or not isinstance(topology.get("class"), str)
        or not topology["class"]
        or not isinstance(nodes, list)
        or not isinstance(retry_safety, dict)
        or not isinstance(retry_safety.get("retry_safe"), bool)
    ):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan lacks compact diagnostic fields"
        )

    snapshot = manifest.get("snapshot")
    if snapshot is None:
        declared_node_count = len(nodes)
    elif (
        isinstance(snapshot, dict)
        and type(snapshot.get("observed_node_count")) is int
        and snapshot["observed_node_count"] >= 0
    ):
        declared_node_count = snapshot["observed_node_count"]
    else:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan has invalid node-count metadata"
        )

    rejections = selection.get("rejections")
    if not isinstance(rejections, list):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan selection lacks rejection metadata"
        )
    rejection_codes: list[str] = []
    for rejection in rejections:
        code = rejection.get("code") if isinstance(rejection, dict) else None
        if not isinstance(code, str) or _STABLE_REJECTION_CODE.fullmatch(code) is None:
            raise WorkflowObservabilityError(
                f"Task {execution.task_id} workflow plan selection has an invalid rejection code"
            )
        presented_code = redact_value(code)
        if not isinstance(presented_code, str):
            raise WorkflowObservabilityError(
                f"Task {execution.task_id} workflow plan selection has an invalid rejection code"
            )
        rejection_codes.append(presented_code)

    total_rejections = selection.get("total_rejections")
    eligible_strategies = selection.get("eligible_strategies")
    if (
        type(total_rejections) is not int
        or total_rejections < len(rejections)
        or not isinstance(eligible_strategies, list)
    ):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} workflow plan selection has inconsistent diagnostics"
        )
    compact_fingerprint = redact_value(f"sha256:{fingerprint.removeprefix('sha256:')[:12]}")
    rejection_counts = dict(sorted(Counter(rejection_codes).items()))
    return {
        "status": "AVAILABLE",
        "definition_name": redact_value(definition["name"]),
        "definition_revision": redact_value(definition["revision"]),
        "topology_class": redact_value(topology["class"]),
        "declared_node_count": declared_node_count,
        "retry_safe": retry_safety["retry_safe"],
        "fingerprint": redact_value(fingerprint),
        "fingerprint_compact": compact_fingerprint,
        "requested_policy": redact_value(selection.get("requested_policy")),
        "selected_strategy": redact_value(selection.get("selected_strategy")),
        "reporting_policy": redact_value(effective_plan_selection_reporting_policy(selection)),
        "eligible_strategies": redact_value(eligible_strategies),
        "rejection_counts": rejection_counts,
        "retained_rejections": len(rejections),
        "total_rejections": total_rejections,
        "unretained_rejections": total_rejections - len(rejections),
    }


def get_workflow_plan_binding(execution: RayTaskExecution) -> dict[str, str]:
    """Return validated unredacted values used only to bind progress to its plan."""
    workflow_plan = _validated_workflow_plan_snapshot(execution)
    if workflow_plan is None:
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} has an incomplete workflow plan snapshot"
        )
    selection = workflow_plan.get("selection")
    fingerprint = workflow_plan.get("fingerprint")
    if not isinstance(selection, dict) or not isinstance(fingerprint, str):
        raise WorkflowObservabilityError(
            f"Task {execution.task_id} has an incomplete workflow plan snapshot"
        )
    return {
        "fingerprint": fingerprint,
        "selected_strategy": selection["selected_strategy"],
        "reporting_policy": effective_plan_selection_reporting_policy(selection),
    }


def _validated_workflow_plan_selection(
    execution: RayTaskExecution,
) -> dict[str, Any] | None:
    """Return validated selection metadata before presentation redaction."""
    if not execution.workflow_plan_selection:
        return None
    try:
        selection = json.loads(execution.workflow_plan_selection)
    except (TypeError, RecursionError, json.JSONDecodeError) as error:
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
    return validated


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
    """Decode bounded schema v1/v2/v3 durable workflow progress."""
    result = read_workflow_progress(execution)
    if result.diagnostic_code is not None:
        task_id = str(getattr(execution, "task_id", "unknown"))[:255]
        message = result.diagnostic_message or "workflow progress is unavailable"
        raise WorkflowObservabilityError(f"Task {task_id} {message}")
    if result.payload is None:
        return None
    progress = result.payload
    if result.source is WorkflowProgressReadSource.SUMMARY:
        progress = public_workflow_progress_summary(progress)
    return redact_value(progress)


def get_workflow_graph(execution: RayTaskExecution) -> dict[str, Any] | None:
    """Return the UI-ready graph from a durable workflow snapshot."""
    progress = get_workflow_progress(execution)
    if progress is None:
        return None
    if progress.get("schema_version") == WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION:
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
    "get_workflow_plan_binding",
    "get_workflow_plan",
    "get_workflow_plan_diagnostics",
    "get_workflow_progress",
    "get_workflow_snapshot",
]
