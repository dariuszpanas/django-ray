"""Test-only builders for the fixed-shape workflow-progress summary protocol."""

from __future__ import annotations

from typing import Any

from django_ray.runtime.context import WorkflowRunIdentity


def workflow_progress_summary(
    execution: Any,
    *,
    summary_revision: int = 1,
    published_detail: bool = False,
    state: str = "RUNNING",
) -> dict[str, Any]:
    """Build one valid schema-v3 summary owned by ``execution``."""
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=str(execution.workflow_run_id),
    )
    terminal = state in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"}
    finished_at = "2026-07-20T12:00:02Z" if terminal else None
    return {
        "schema_version": 3,
        "storage_protocol_version": 1,
        "run_identity": identity.as_dict(),
        "reporting_policy": "full",
        "selected_strategy": None,
        "plan_fingerprint": None,
        "limits_profile": "v1",
        "summary_revision": summary_revision,
        "topology_version": 1 if published_detail else None,
        "detail_revision": 1 if published_detail else None,
        "state": state,
        "node_counts": {
            "declared": 1,
            "discovered": 1,
            "retained_topology": 1 if published_detail else 0,
            "retained_detail": 1 if published_detail else 0,
            "pending": 0 if terminal else 1,
            "running": 0,
            "succeeded": 1 if state == "SUCCEEDED" else 0,
            "failed": 1 if state in {"FAILED", "CANCELLED", "LOST", "EXPIRED"} else 0,
        },
        "edge_counts": {
            "declared": 0,
            "discovered": 0,
            "retained_topology": 0,
        },
        "progress_percent": 100.0 if terminal else 0.0,
        "timestamps": {
            "started_at": "2026-07-20T12:00:00Z",
            "updated_at": finished_at or "2026-07-20T12:00:01Z",
            "finished_at": finished_at,
        },
        "detail": {
            "availability": "AVAILABLE" if published_detail else "NOT_REPORTED",
            "complete": published_detail,
            "truncation_reasons": [],
        },
        "storage": {
            "kind": "database",
            "manifest_id": "manifest_125" if published_detail else None,
        },
        "retention": {
            "detail_days": 7,
            "detail_expires_at": (
                "2026-07-27T12:00:02Z" if terminal and published_detail else None
            ),
        },
        "terminal": {"outcome": state if terminal else None, "finished_at": finished_at},
    }


def terminal_only_workflow_progress_summary(
    execution: Any,
    *,
    state: str = "SUCCEEDED",
    declared_node_count: int = 1,
    declared_edge_count: int = 0,
) -> dict[str, Any]:
    """Build the summary-only terminal contract without fabricated node evidence."""
    if state not in {"SUCCEEDED", "FAILED"}:
        raise ValueError("terminal-only test summaries require success or failure")
    summary = workflow_progress_summary(execution, state=state)
    summary["reporting_policy"] = "terminal_only"
    summary["node_counts"] = {
        "declared": declared_node_count,
        "discovered": 0,
        "retained_topology": 0,
        "retained_detail": 0,
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    summary["edge_counts"] = {
        "declared": declared_edge_count,
        "discovered": 0,
        "retained_topology": 0,
    }
    summary["progress_percent"] = 100.0 if state == "SUCCEEDED" else 0.0
    summary["detail"] = {
        "availability": "OMITTED_BY_POLICY",
        "complete": False,
        "truncation_reasons": [],
    }
    return summary
