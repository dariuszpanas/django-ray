"""Shared fixtures for workflow-progress storage integration and scaling tests."""

from __future__ import annotations

from dataclasses import dataclass

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_progress_storage import (
    PreparedWorkflowProgressTopology,
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    prepare_workflow_progress_topology,
    stage_workflow_progress_topology,
)


@dataclass(frozen=True)
class PublishedWorkflow:
    execution: RayTaskExecution
    identity: WorkflowRunIdentity
    topology: PreparedWorkflowProgressTopology
    manifest_id: str


def workflow_node_id(index: int) -> str:
    return f"node-{index:05d}"


def workflow_node(node_id: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "kind": "task",
        "label": f"Node {node_id}",
        "callable_path": "app.jobs.sync_resource",
        "runtime_env": {},
        "ray_options": {},
    }


def workflow_detail(node_id: str, *, state: str = "PENDING") -> dict[str, object]:
    return {
        "schema_version": 1,
        "node_id": node_id,
        "invocation_identity": None,
        "state": state,
        "progress": None,
        "execution": None,
        "fanout": None,
        "started_at": "2026-07-20T12:00:00Z" if state == "RUNNING" else None,
        "finished_at": None,
        "error": None,
        "recent_events": [],
    }


def workflow_summary(
    identity: WorkflowRunIdentity,
    *,
    summary_revision: int,
    node_count: int,
    running_count: int,
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "storage_protocol_version": 1,
        "run_identity": identity.as_dict(),
        "reporting_policy": "full",
        "selected_strategy": None,
        "plan_fingerprint": None,
        "limits_profile": "v1",
        "summary_revision": summary_revision,
        "topology_version": None,
        "detail_revision": None,
        "state": "RUNNING",
        "node_counts": {
            "declared": node_count,
            "discovered": node_count,
            "retained_topology": 0,
            "retained_detail": 0,
            "pending": node_count - running_count,
            "running": running_count,
            "succeeded": 0,
            "failed": 0,
        },
        "edge_counts": {
            "declared": 0,
            "discovered": 0,
            "retained_topology": 0,
        },
        "progress_percent": 0.0,
        "timestamps": {
            "started_at": "2026-07-20T12:00:00Z",
            "updated_at": f"2026-07-20T12:00:0{summary_revision}Z",
            "finished_at": None,
        },
        "detail": {
            "availability": "NOT_REPORTED",
            "complete": False,
            "truncation_reasons": [],
        },
        "storage": {"kind": "database", "manifest_id": None},
        "retention": {"detail_days": 7, "detail_expires_at": None},
        "terminal": {"outcome": None, "finished_at": None},
    }


def publish_initial_workflow(
    node_count: int,
    *,
    case_id: int = 0,
) -> PublishedWorkflow:
    run_value = node_count * 1_000 + case_id
    run_id = f"00000000-0000-0000-0000-{run_value:012d}"
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-storage-{node_count}-{case_id}",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=run_id,
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=1,
        execution_generation=1,
        run_id=run_id,
    )
    topology = prepare_workflow_progress_topology(
        identity,
        1,
        (workflow_node(workflow_node_id(index)) for index in range(node_count)),
        (),
    )
    prepared_detail = prepare_workflow_progress_detail(
        (workflow_detail(workflow_node_id(index)) for index in range(node_count)),
        topology=topology,
    )
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    result = persist_workflow_progress_publication(
        identity,
        workflow_summary(
            identity,
            summary_revision=1,
            node_count=node_count,
            running_count=0,
        ),
        manifest_id=manifest_id,
        prepared_topology=topology,
        prepared_detail=prepared_detail,
    )
    assert result.accepted
    return PublishedWorkflow(execution, identity, topology, manifest_id)


__all__ = [
    "PublishedWorkflow",
    "publish_initial_workflow",
    "workflow_detail",
    "workflow_node",
    "workflow_node_id",
    "workflow_summary",
]
