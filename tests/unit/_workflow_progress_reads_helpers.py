"""Shared helpers for bounded authorized workflow-progress read tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.core import signing

import django_ray.workflow.progress.reads as reads
import django_ray.workflow.progress.storage as storage
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.workflow.plans import (
    PLAN_SELECTION_FORMAT,
    PLAN_SELECTION_FORMAT_VERSION,
)
from django_ray.workflow.progress.preparation import prepare_workflow_progress_topology
from django_ray.workflow.progress.reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
)
from django_ray.workflow.progress.storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    stage_workflow_progress_topology,
)
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from tests.workflow_progress_storage_helpers import (
    PublishedWorkflow,
    workflow_detail,
    workflow_node,
    workflow_node_id,
    workflow_summary,
)
from tests.workflow_progress_summary_helpers import workflow_progress_summary


def _allow(_execution: RayTaskExecution) -> bool:
    return True


def _plan_selection_manifest(
    *,
    version: int = PLAN_SELECTION_FORMAT_VERSION,
    selected_strategy: str = "dynamic_tasks",
    reporting_policy: str = "full",
) -> dict[str, Any]:
    selection: dict[str, Any] = {
        "plan_selection_format": PLAN_SELECTION_FORMAT,
        "plan_selection_format_version": version,
        "requested_policy": "auto",
        "selected_strategy": selected_strategy,
        "eligible_strategies": [selected_strategy],
        "rejections": [],
        "total_rejections": 0,
        "rejections_truncated": False,
    }
    if version == PLAN_SELECTION_FORMAT_VERSION:
        selection["reporting_policy"] = reporting_policy
    return selection


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _wire_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")


def _error_code(operation) -> WorkflowProgressReadErrorCode:
    with pytest.raises(WorkflowProgressReadError) as raised:
        operation()
    assert len(str(raised.value).encode("utf-8")) < 256
    return raised.value.code


def _signed_cursor(cursor: str, **updates: Any) -> str:
    payload = signing.loads(cursor, salt=reads._CURSOR_SALT)
    assert isinstance(payload, dict)
    payload.update(updates)
    return signing.dumps(payload, salt=reads._CURSOR_SALT, compress=False)


def _decoded_cursor(cursor: str) -> dict[str, Any]:
    payload = signing.loads(cursor, salt=reads._CURSOR_SALT)
    assert isinstance(payload, dict)
    return payload


def _store_summary(execution: RayTaskExecution, summary: dict[str, Any]) -> None:
    execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
    execution.save(update_fields=["workflow_progress_summary_json"])


def _stored_summary(execution: RayTaskExecution) -> dict[str, Any]:
    execution.refresh_from_db(fields=["workflow_progress_summary_json"])
    assert execution.workflow_progress_summary_json is not None
    value = json.loads(execution.workflow_progress_summary_json)
    assert isinstance(value, dict)
    return value


def _summary_only_execution(
    availability: str,
    *,
    case_id: int,
) -> RayTaskExecution:
    execution = RayTaskExecution.objects.create(
        task_id=f"summary-only-{availability.lower()}-{case_id}",
        callable_path="tests.summary_only.workflow",
        state=TaskState.RUNNING,
        workflow_run_id=f"00000000-0000-0000-0000-{case_id:012d}",
    )
    summary = workflow_progress_summary(execution)
    if availability == "OMITTED_BY_POLICY":
        summary["reporting_policy"] = "sampled"
    elif availability == "DISABLED":
        summary["reporting_policy"] = "disabled"
    summary["detail"]["availability"] = availability
    _store_summary(execution, summary)
    return execution


def _publish_workflow_with_edges(
    node_count: int,
    edge_count: int,
    *,
    case_id: int,
) -> PublishedWorkflow:
    run_value = node_count * 100_000 + edge_count + case_id
    run_id = f"00000000-0000-0000-0000-{run_value:012d}"
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-read-{node_count}-{edge_count}-{case_id}",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=run_id,
    )
    identity = reads.WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=1,
        execution_generation=1,
        run_id=run_id,
    )
    topology = prepare_workflow_progress_topology(
        identity,
        1,
        (workflow_node(workflow_node_id(index)) for index in range(node_count)),
        (
            {
                "source": workflow_node_id(index % node_count),
                "target": workflow_node_id((index + 1) % node_count),
            }
            for index in range(edge_count)
        ),
    )
    prepared_detail = prepare_workflow_progress_detail(
        (workflow_detail(workflow_node_id(index)) for index in range(node_count)),
        topology=topology,
    )
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    summary = workflow_summary(
        identity,
        summary_revision=1,
        node_count=node_count,
        running_count=0,
    )
    summary["edge_counts"].update(declared=edge_count, discovered=edge_count)
    result = persist_workflow_progress_publication(
        identity,
        summary,
        manifest_id=manifest_id,
        prepared_topology=topology,
        prepared_detail=prepared_detail,
    )
    assert result.accepted
    return PublishedWorkflow(execution, identity, topology, manifest_id)


def _publish_with_legacy_terminal_redaction(
    monkeypatch: pytest.MonkeyPatch,
    *,
    case_id: int,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    details: list[dict[str, Any]],
) -> PublishedWorkflow:
    """Prepare protocol-v1 bytes using the pre-terminal-normalization policy."""
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-legacy-terminal-{case_id}",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=f"00000000-0000-0000-0000-{case_id:012d}",
    )
    identity = reads.WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=1,
        execution_generation=1,
        run_id=str(execution.workflow_run_id),
    )
    current_redact_text = storage.redact_text

    def legacy_redact_text(value: Any) -> str:
        if isinstance(value, str) and "\x1b" in value:
            return value
        return current_redact_text(value)

    monkeypatch.setattr(storage, "redact_text", legacy_redact_text)
    topology = prepare_workflow_progress_topology(identity, 1, nodes, edges)
    prepared_detail = prepare_workflow_progress_detail(details, topology=topology)
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    summary = workflow_summary(
        identity,
        summary_revision=1,
        node_count=len(nodes),
        running_count=sum(detail.get("state") == "RUNNING" for detail in details),
    )
    edge_counts = summary["edge_counts"]
    assert isinstance(edge_counts, dict)
    edge_counts.update(declared=len(edges), discovered=len(edges))
    result = persist_workflow_progress_publication(
        identity,
        summary,
        manifest_id=manifest_id,
        prepared_topology=topology,
        prepared_detail=prepared_detail,
    )
    assert result.accepted
    monkeypatch.setattr(storage, "redact_text", current_redact_text)
    return PublishedWorkflow(execution, identity, topology, manifest_id)


def _legacy_ansi_workflow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    case_id: int,
) -> PublishedWorkflow:
    first_id = workflow_node_id(0)
    second_id = workflow_node_id(1)
    first_node = workflow_node(first_id)
    first_node.update(
        {
            "label": "\x1b[31mPrepare\x1b[0m",
            "runtime_env": {"\x1b[36mprofile\x1b[0m": "default"},
        }
    )
    first_detail = workflow_detail(first_id, state="RUNNING")
    first_detail["progress"] = {
        "current": 1,
        "total": 2,
        "percent": 50,
        "message": "\x1b[33mWorking\x1b[0m",
        "metrics": {"\x1b[35mrows\x1b[0m": 1},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    first_detail["recent_events"] = [
        {
            "event": "STATE_CHANGE",
            "state": "RUNNING",
            "label": "\x1b[34mStarted\x1b[0m",
            "timestamp": "2026-07-20T12:00:00Z",
        }
    ]
    return _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=case_id,
        nodes=[first_node, workflow_node(second_id)],
        edges=[{"source": first_id, "target": second_id}],
        details=[first_detail, workflow_detail(second_id)],
    )


def _archive_workflow_attempt(published: PublishedWorkflow, *, next_run: int) -> int:
    published.execution.refresh_from_db()
    summary = published.execution.workflow_progress_summary_json
    assert summary is not None
    TaskAttempt.objects.create(
        execution=published.execution,
        attempt_number=1,
        state=TaskState.SUCCEEDED,
        workflow_progress_summary_json=summary,
    )
    published.execution.attempt_number = 2
    published.execution.execution_generation = 2
    published.execution.workflow_run_id = f"00000000-0000-0000-0000-{next_run:012d}"
    published.execution.workflow_progress_summary_json = None
    published.execution.save(
        update_fields=[
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_progress_summary_json",
        ]
    )
    return 1
