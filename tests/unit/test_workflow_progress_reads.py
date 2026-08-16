"""Contract tests for bounded authorized workflow-progress reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo
from typing import Any

import pytest
from django.core import signing
from django.db import connection
from django.test.utils import CaptureQueriesContext

import django_ray.workflow_progress_reads as reads
import django_ray.workflow_progress_storage as storage
from django_ray.lifecycle import succeed_task
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
)
from django_ray.redaction import REDACTED
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from django_ray.workflow_plans import (
    PLAN_SELECTION_FORMAT,
    PLAN_SELECTION_FORMAT_VERSION,
    PLAN_SELECTION_LEGACY_FORMAT_VERSION,
)
from django_ray.workflow_progress import (
    WorkflowProgressDiagnosticCode,
    WorkflowProgressReadResult,
    WorkflowProgressReadSource,
    read_workflow_progress,
)
from django_ray.workflow_progress_reads import (
    WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES,
    WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES,
    WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES,
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)
from django_ray.workflow_progress_storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    prepare_workflow_progress_node_detail,
    prepare_workflow_progress_topology,
    stage_workflow_progress_topology,
)
from tests.workflow_progress_storage_helpers import (
    PublishedWorkflow,
    publish_initial_workflow,
    workflow_detail,
    workflow_node,
    workflow_node_id,
    workflow_summary,
)
from tests.workflow_progress_summary_helpers import workflow_progress_summary

pytestmark = pytest.mark.django_db


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


def test_summary_pages_and_indexed_node_use_public_bounded_contract() -> None:
    published = publish_initial_workflow(257, case_id=127_001)

    summary = get_workflow_progress_summary(published.execution, authorize=_allow)
    topology = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=100,
    )
    topology_next = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        cursor=topology["next_cursor"],
        limit=100,
    )
    details = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=999,
    )
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(150),
        authorize=_allow,
    )

    assert summary["source_schema_version"] == 3
    assert summary["availability"] == "AVAILABLE"
    assert summary["summary"]["storage"]["manifest_id"] is None
    assert "task_execution_pk" not in summary["run_identity"]
    assert topology["returned_count"] == 100
    assert topology["items"][0]["node_id"] == workflow_node_id(0)
    assert topology_next["items"][0]["node_id"] == workflow_node_id(100)
    assert details["returned_count"] == 256
    assert node["found"] is True
    assert node["item"]["node_id"] == workflow_node_id(150)
    assert "task_execution_pk" not in json.dumps(node)
    assert len(_canonical_bytes(details)) <= WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES
    assert len(details["next_cursor"].encode()) <= WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES


def test_authorization_is_repeated_before_cursor_or_storage_disclosure() -> None:
    published = publish_initial_workflow(101, case_id=127_002)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=100)
    calls: list[int] = []

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    with pytest.raises(WorkflowProgressReadError) as denied:
        list_workflow_node_details(
            published.execution,
            authorize=deny,
            cursor=first["next_cursor"],
            limit=100,
        )

    assert denied.value.code is WorkflowProgressReadErrorCode.ACCESS_DENIED
    assert calls == [published.execution.pk]


def test_cursor_tampering_and_request_mismatch_fail_explicitly() -> None:
    published = publish_initial_workflow(101, case_id=127_003)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=100)
    cursor = first["next_cursor"]
    assert cursor is not None

    with pytest.raises(WorkflowProgressReadError) as tampered:
        list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=f"{cursor[:-1]}x",
            limit=100,
        )
    with pytest.raises(WorkflowProgressReadError) as wrong_limit:
        list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            limit=99,
        )
    with pytest.raises(WorkflowProgressReadError) as wrong_filter:
        list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            state="PENDING",
            limit=100,
        )

    assert tampered.value.code is WorkflowProgressReadErrorCode.INVALID_CURSOR
    assert wrong_limit.value.code is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    assert wrong_filter.value.code is WorkflowProgressReadErrorCode.CURSOR_MISMATCH


def test_summary_update_expires_detail_and_topology_cursors() -> None:
    published = publish_initial_workflow(151, case_id=127_004)
    topology_first = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=100,
    )
    detail_first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=100,
    )
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(150), state="RUNNING"),
        identity=published.identity,
    )
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=151,
            running_count=1,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted

    topology_next = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        cursor=topology_first["next_cursor"],
        limit=100,
    )
    detail_next = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        cursor=detail_first["next_cursor"],
        limit=100,
    )

    assert topology_next["availability"] == "EXPIRED"
    assert topology_next["publication"]["summary_revision"] == 1
    assert topology_next["items"] == []
    assert topology_next["next_cursor"] is None
    assert detail_next["availability"] == "EXPIRED"
    assert detail_next["items"] == []
    assert detail_next["next_cursor"] is None


def test_state_filter_and_empty_single_node_preserve_availability() -> None:
    published = publish_initial_workflow(4, case_id=127_005)

    pending = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="pending",
    )
    running = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="RUNNING",
    )
    unknown = get_workflow_node_detail(
        published.execution,
        "unknown-node",
        authorize=_allow,
    )

    assert pending["returned_count"] == 4
    assert running["returned_count"] == 0
    assert running["availability"] == "AVAILABLE"
    assert unknown["found"] is False
    assert unknown["availability"] == "AVAILABLE"


def test_missing_storage_is_a_bounded_error_without_fallback() -> None:
    published = publish_initial_workflow(1, case_id=127_006)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).delete()

    with pytest.raises(WorkflowProgressReadError) as missing:
        list_workflow_topology_nodes(published.execution, authorize=_allow)

    assert missing.value.code is WorkflowProgressReadErrorCode.MISSING
    assert str(missing.value) == "Referenced workflow progress storage is missing."


def test_no_v3_summary_is_not_reported_and_legacy_compatibility_is_aggregate_only() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="legacy-read-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
        workflow_run_id="00000000-0000-0000-0000-000000127007",
    )
    execution.progress_data = json.dumps(
        {
            "schema_version": 2,
            "run_identity": {
                "schema_version": 1,
                "run_id": str(execution.workflow_run_id),
                "task_execution_pk": execution.pk,
                "attempt_number": execution.attempt_number,
                "execution_generation": execution.execution_generation,
            },
            "revision": 3,
            "state": "RUNNING",
            "total_nodes": 2,
            "completed_nodes": 1,
            "failed_nodes": 0,
            "running_nodes": 1,
            "pending_nodes": 0,
            "progress_percent": 50.0,
            "graph": {"nodes": [{"secret": "not-returned"}], "edges": []},
        }
    )
    execution.save(update_fields=["progress_data"])

    current_only = get_workflow_progress_summary(execution, authorize=_allow)
    compatibility = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    page = list_workflow_node_details(execution, authorize=_allow)

    assert current_only["availability"] == "NOT_REPORTED"
    assert current_only["summary"] is None
    assert compatibility["source_schema_version"] == 2
    assert compatibility["availability"] == "NOT_REPORTED"
    assert compatibility["complete"] is False
    assert compatibility["run_identity"] == compatibility["summary"]["run_identity"]
    assert compatibility["summary"]["node_counts"]["discovered"] == 2
    assert compatibility["summary"]["node_counts"]["retained_topology"] == 0
    assert compatibility["summary"]["node_counts"]["retained_detail"] == 0
    assert compatibility["summary"]["edge_counts"]["retained_topology"] == 0
    assert "graph" not in compatibility["summary"]
    assert "secret" not in json.dumps(compatibility)
    assert page["availability"] == "NOT_REPORTED"


@pytest.mark.parametrize(
    ("selection_version", "selected_strategy"),
    [
        (PLAN_SELECTION_FORMAT_VERSION, "dynamic_tasks"),
        (PLAN_SELECTION_LEGACY_FORMAT_VERSION, "local"),
    ],
)
def test_disabled_plan_selection_is_a_bounded_summary_only_signal(
    selection_version: int,
    selected_strategy: str,
) -> None:
    selection = _plan_selection_manifest(
        version=selection_version,
        selected_strategy=selected_strategy,
        reporting_policy="disabled",
    )
    execution = RayTaskExecution.objects.create(
        task_id=f"disabled-selection-{selection_version}-{selected_strategy}",
        callable_path="tests.disabled.workflow",
        state=TaskState.RUNNING,
        workflow_plan_selection=json.dumps(selection),
    )

    with CaptureQueriesContext(connection) as queries:
        response = get_workflow_progress_summary(execution, authorize=_allow)

    statements = "\n".join(query["sql"] for query in queries.captured_queries).lower()
    assert response["source_schema_version"] is None
    assert response["summary"] is None
    assert response["availability"] == "DISABLED"
    assert response["complete"] is False
    assert "progress_data" not in statements
    assert "workflowprogressnodedetail" not in statements
    assert "workflowprogresstopolog" not in statements
    assert "case" in statements
    assert "length" in statements
    assert len(queries) == 2


def test_dynamic_legacy_selection_without_a_summary_remains_not_reported() -> None:
    selection = _plan_selection_manifest(version=PLAN_SELECTION_LEGACY_FORMAT_VERSION)
    execution = RayTaskExecution.objects.create(
        task_id="full-legacy-selection",
        callable_path="tests.full.workflow",
        state=TaskState.RUNNING,
        workflow_plan_selection=json.dumps(selection),
    )

    response = get_workflow_progress_summary(execution, authorize=_allow)

    assert response["summary"] is None
    assert response["availability"] == "NOT_REPORTED"


def test_terminal_full_selection_without_v3_publication_is_missing() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="terminal-full-selection",
        callable_path="tests.full.workflow",
        state=TaskState.SUCCEEDED,
        workflow_plan_selection=json.dumps(_plan_selection_manifest()),
    )

    summary = get_workflow_progress_summary(execution, authorize=_allow)

    assert summary["summary"] is None
    assert summary["availability"] == "MISSING"
    with pytest.raises(WorkflowProgressReadError) as missing:
        list_workflow_topology_nodes(execution, authorize=_allow)
    assert missing.value.code is WorkflowProgressReadErrorCode.MISSING


def test_disabled_selection_query_is_fenced_against_a_new_workflow_run(
    monkeypatch,
) -> None:
    old_run_id = "00000000-0000-0000-0000-000000127230"
    new_run_id = "00000000-0000-0000-0000-000000127231"
    execution = RayTaskExecution.objects.create(
        task_id="selection-race",
        callable_path="tests.racing.workflow",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=old_run_id,
        workflow_plan_selection=json.dumps(_plan_selection_manifest()),
    )

    def advance_run(_execution, **_kwargs):
        RayTaskExecution.objects.filter(pk=execution.pk).update(
            attempt_number=2,
            execution_generation=2,
            workflow_run_id=new_run_id,
            workflow_plan_selection=json.dumps(
                _plan_selection_manifest(reporting_policy="disabled")
            ),
        )
        return None

    monkeypatch.setattr(reads, "_read_schema_v3_summary", advance_run)

    response = get_workflow_progress_summary(execution, authorize=_allow)

    execution.refresh_from_db()
    assert execution.attempt_number == 2
    assert response["availability"] == "NOT_REPORTED"
    assert response["summary"] is None


def test_legacy_summary_clamps_untrusted_primitives_and_bounds_the_response() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="legacy-malformed-primitives-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
    )
    huge_counter = 10**1_000
    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "run_identity": {
                "run_id": "x" * 100_000,
                "attempt_number": True,
                "execution_generation": -1,
            },
            "revision": huge_counter,
            "state": "x" * 100_000,
            "total_nodes": huge_counter,
            "completed_nodes": -1,
            "failed_nodes": True,
            "running_nodes": "not-an-integer" * 10_000,
            "pending_nodes": huge_counter,
            "progress_percent": float("inf"),
            "updated_at": {"untrusted": "value" * 10_000},
            "graph": {"nodes": [], "edges": [{"source": "a", "target": "b"}]},
            "diagnostic": "private" * 10_000,
        }
    )
    execution.save(update_fields=["progress_data"])

    response = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    summary = response["summary"]

    assert response["availability"] == "NOT_REPORTED"
    assert response["complete"] is False
    assert response["run_identity"] is None
    assert summary["run_identity"] is None
    assert summary["revision"] == (1 << 63) - 1
    assert summary["state"] == "RUNNING"
    assert summary["node_counts"] == {
        "declared": (1 << 63) - 1,
        "discovered": (1 << 63) - 1,
        "retained_topology": 0,
        "retained_detail": 0,
        "pending": (1 << 63) - 1,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert summary["edge_counts"]["retained_topology"] == 0
    assert summary["progress_percent"] == 0.0
    assert summary["updated_at"] == 0.0
    assert "private" not in json.dumps(response)
    assert len(_wire_bytes(response)) <= WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES


def test_retained_attempt_reads_authorize_the_owner_after_current_task_advances() -> None:
    published = publish_initial_workflow(3, case_id=127_008)
    published.execution.refresh_from_db()
    terminal_summary = published.execution.workflow_progress_summary_json
    assert terminal_summary is not None
    TaskAttempt.objects.create(
        execution=published.execution,
        attempt_number=1,
        state=TaskState.SUCCEEDED,
        workflow_progress_summary_json=terminal_summary,
    )
    published.execution.attempt_number = 2
    published.execution.execution_generation = 2
    published.execution.workflow_run_id = "00000000-0000-0000-0000-000000127009"
    published.execution.workflow_progress_summary_json = None
    published.execution.save(
        update_fields=[
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_progress_summary_json",
        ]
    )
    calls: list[int] = []

    def authorize_owner(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return True

    summary = get_workflow_progress_summary(
        published.execution,
        authorize=authorize_owner,
        attempt_number=1,
    )
    detail = list_workflow_node_details(
        published.execution,
        authorize=authorize_owner,
        attempt_number=1,
    )

    assert summary["run_identity"]["attempt_number"] == 1
    assert detail["returned_count"] == 3
    assert calls == [published.execution.pk, published.execution.pk]


def test_archived_attempt_summary_uses_one_bounded_attempt_projection() -> None:
    published = publish_initial_workflow(1, case_id=127_133)
    terminal_summary = _stored_summary(published.execution)
    TaskAttempt.objects.create(
        execution=published.execution,
        attempt_number=1,
        state=TaskState.SUCCEEDED,
        workflow_progress_summary_json=serialize_workflow_progress_summary(terminal_summary),
    )
    published.execution.attempt_number = 2
    published.execution.execution_generation = 2
    published.execution.workflow_run_id = "00000000-0000-0000-0000-000000127134"
    published.execution.workflow_progress_summary_json = None
    published.execution.save(
        update_fields=[
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_progress_summary_json",
        ]
    )

    with CaptureQueriesContext(connection) as queries:
        response = get_workflow_progress_summary(
            published.execution,
            authorize=_allow,
            attempt_number=1,
        )

    selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
    ]
    attempt_selects = [sql for sql in selects if "django_ray_taskattempt" in sql.lower()]
    assert response["run_identity"]["attempt_number"] == 1
    assert len(selects) == 2
    assert len(attempt_selects) == 1
    assert "CASE" in attempt_selects[0].upper()
    assert "LENGTH" in attempt_selects[0].upper()


def test_empty_topology_edges_return_a_complete_bounded_page() -> None:
    published = publish_initial_workflow(3, case_id=127_010)

    edges = list_workflow_topology_edges(published.execution, authorize=_allow)

    assert edges["availability"] == "AVAILABLE"
    assert edges["complete"] is True
    assert edges["returned_count"] == 0
    assert edges["next_cursor"] is None


@pytest.mark.parametrize(
    "availability",
    ["NOT_REPORTED", "OMITTED_BY_POLICY", "DISABLED"],
)
def test_summary_only_availability_states_return_empty_detail_contracts(
    availability: str,
) -> None:
    execution = _summary_only_execution(
        availability,
        case_id={
            "NOT_REPORTED": 127_101,
            "OMITTED_BY_POLICY": 127_102,
            "DISABLED": 127_103,
        }[availability],
    )

    summary = get_workflow_progress_summary(execution, authorize=_allow)
    page = list_workflow_node_details(execution, authorize=_allow)
    node = get_workflow_node_detail(execution, "node-00000", authorize=_allow)

    assert summary["availability"] == availability
    assert summary["summary"]["detail"]["availability"] == availability
    assert summary["complete"] is False
    assert page["availability"] == availability
    assert page["items"] == []
    assert page["next_cursor"] is None
    assert node["availability"] == availability
    assert node["found"] is False


def test_truncated_detail_remains_readable_without_claiming_completeness() -> None:
    published = publish_initial_workflow(3, case_id=127_104)
    summary = _stored_summary(published.execution)
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(published.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_truncation_reasons="detail_count_limit"
    )

    envelope = get_workflow_progress_summary(published.execution, authorize=_allow)
    topology = list_workflow_topology_nodes(published.execution, authorize=_allow)
    details = list_workflow_node_details(published.execution, authorize=_allow)

    assert envelope["availability"] == "TRUNCATED"
    assert envelope["complete"] is False
    assert topology["returned_count"] == 3
    assert topology["complete"] is False
    assert details["returned_count"] == 3
    assert details["complete"] is False


@pytest.mark.parametrize(
    ("availability", "expected_code"),
    [
        ("MISSING", WorkflowProgressReadErrorCode.MISSING),
        ("CORRUPT", WorkflowProgressReadErrorCode.CORRUPT),
    ],
)
def test_fault_availability_keeps_summary_readable_but_blocks_detail(
    availability: str,
    expected_code: WorkflowProgressReadErrorCode,
) -> None:
    published = publish_initial_workflow(
        1,
        case_id=127_105 if availability == "MISSING" else 127_106,
    )
    summary = _stored_summary(published.execution)
    summary["detail"] = {
        "availability": availability,
        "complete": False,
        "truncation_reasons": [],
    }
    _store_summary(published.execution, summary)

    envelope = get_workflow_progress_summary(published.execution, authorize=_allow)

    assert envelope["availability"] == availability
    assert envelope["summary"] is not None
    assert (
        _error_code(lambda: list_workflow_topology_nodes(published.execution, authorize=_allow))
        is expected_code
    )
    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is expected_code
    )
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is expected_code
    )


def test_terminal_expiry_overrides_available_storage_without_reading_it(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_107)
    summary = _stored_summary(published.execution)
    finished_at = "2026-07-20T12:00:02Z"
    summary.update(state="SUCCEEDED", progress_percent=100.0)
    summary["node_counts"].update(pending=0, succeeded=1)
    summary["timestamps"].update(updated_at=finished_at, finished_at=finished_at)
    summary["retention"].update(detail_days=0, detail_expires_at=finished_at)
    summary["terminal"] = {"outcome": "SUCCEEDED", "finished_at": finished_at}
    _store_summary(published.execution, summary)
    after_expiry = datetime(2026, 7, 20, 12, 0, 3, tzinfo=UTC)

    class _AfterExpiry(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_expiry if tz is not None else after_expiry.replace(tzinfo=None)

    envelope = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        generated_at=after_expiry,
    )
    monkeypatch.setattr(reads, "datetime", _AfterExpiry)
    page = list_workflow_node_details(published.execution, authorize=_allow)

    assert envelope["availability"] == "EXPIRED"
    assert envelope["summary"]["detail"]["availability"] == "EXPIRED"
    assert page["availability"] == "EXPIRED"
    assert page["items"] == []
    assert page["next_cursor"] is None


@pytest.mark.parametrize(
    "summary_case",
    ["malformed", "oversized", "noncanonical", "cross_run"],
)
def test_invalid_v3_summary_never_falls_back_to_legacy(
    summary_case: str,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id=f"invalid-v3-{summary_case}",
        callable_path="tests.invalid.workflow",
        state=TaskState.RUNNING,
        workflow_run_id="00000000-0000-0000-0000-000000127108",
    )
    valid = workflow_progress_summary(execution)
    if summary_case == "malformed":
        stored = "{"
    elif summary_case == "oversized":
        stored = "x" * (reads.WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES + 1)
    elif summary_case == "noncanonical":
        stored = json.dumps(valid, indent=2)
    else:
        other = RayTaskExecution.objects.create(
            task_id="cross-run-summary-owner",
            callable_path="tests.invalid.workflow",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000127109",
        )
        stored = serialize_workflow_progress_summary(workflow_progress_summary(other))
    execution.workflow_progress_summary_json = stored
    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "revision": 99,
            "graph": {"nodes": [{"secret": "must-not-fallback"}], "edges": []},
        }
    )
    execution.save(update_fields=["workflow_progress_summary_json", "progress_data"])

    with pytest.raises(WorkflowProgressReadError) as raised:
        get_workflow_progress_summary(execution, authorize=_allow, include_legacy=True)

    assert raised.value.code is WorkflowProgressReadErrorCode.CORRUPT
    assert "must-not-fallback" not in str(raised.value)


def test_every_operation_repeats_object_authorization() -> None:
    published = publish_initial_workflow(101, case_id=127_110)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=100)
    cursor = first["next_cursor"]
    assert cursor is not None
    calls: list[int] = []

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    operations = [
        lambda: get_workflow_progress_summary(published.execution, authorize=deny),
        lambda: list_workflow_topology_nodes(published.execution, authorize=deny),
        lambda: list_workflow_node_details(published.execution, authorize=deny),
        lambda: list_workflow_node_details(
            published.execution,
            authorize=deny,
            cursor=cursor,
            limit=100,
        ),
        lambda: get_workflow_node_detail(
            published.execution,
            workflow_node_id(0),
            authorize=deny,
        ),
    ]

    assert [_error_code(operation) for operation in operations] == [
        WorkflowProgressReadErrorCode.ACCESS_DENIED
    ] * len(operations)
    assert calls == [published.execution.pk] * len(operations)


def test_authorization_policy_exceptions_are_bounded_and_redacted() -> None:
    published = publish_initial_workflow(1, case_id=127_111)

    def fail_policy(_execution: RayTaskExecution) -> bool:
        raise RuntimeError("private policy implementation detail")

    with pytest.raises(WorkflowProgressReadError) as raised:
        get_workflow_progress_summary(published.execution, authorize=fail_policy)

    assert raised.value.code is WorkflowProgressReadErrorCode.ACCESS_DENIED
    assert str(raised.value) == "Workflow progress access is denied."
    assert "private" not in str(raised.value)


def test_authorization_precedes_oversized_cursor_validation() -> None:
    published = publish_initial_workflow(1, case_id=127_112)
    calls: list[int] = []

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    code = _error_code(
        lambda: list_workflow_node_details(
            published.execution,
            authorize=deny,
            cursor="x" * (WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES + 1),
        )
    )

    assert code is WorkflowProgressReadErrorCode.ACCESS_DENIED
    assert calls == [published.execution.pk]


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        (
            lambda execution, authorize: get_workflow_progress_summary(
                execution,
                authorize=authorize,
                include_legacy="yes",
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: get_workflow_progress_summary(
                execution,
                authorize=authorize,
                generated_at="not-a-datetime",
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: list_workflow_topology_nodes(
                execution,
                authorize=authorize,
                limit=0,
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: list_workflow_node_details(
                execution,
                authorize=authorize,
                state=object(),
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: get_workflow_node_detail(
                execution,
                "",
                authorize=authorize,
            ),
            WorkflowProgressReadErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda execution, authorize: list_workflow_node_details(
                execution,
                authorize=authorize,
                cursor="x" * (WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES + 1),
            ),
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
    ],
)
def test_saved_execution_authorizes_once_before_request_validation(
    operation,
    expected_code: WorkflowProgressReadErrorCode,
) -> None:
    published = publish_initial_workflow(1, case_id=127_123)
    calls: list[int] = []

    def allow(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return True

    assert _error_code(lambda: operation(published.execution, allow)) is expected_code
    assert calls == [published.execution.pk]

    calls.clear()

    def deny(execution: RayTaskExecution) -> bool:
        calls.append(execution.pk)
        return False

    assert (
        _error_code(lambda: operation(published.execution, deny))
        is WorkflowProgressReadErrorCode.ACCESS_DENIED
    )
    assert calls == [published.execution.pk]


def test_cursor_rejects_oversize_schema_owner_collection_and_order_mismatch() -> None:
    published = publish_initial_workflow(101, case_id=127_113)
    other = publish_initial_workflow(101, case_id=127_114)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=100)
    cursor = first["next_cursor"]
    assert cursor is not None

    cases = {
        "oversized": (
            "x" * (WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES + 1),
            published.execution,
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
        "schema": (
            _signed_cursor(cursor, v=2),
            published.execution,
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
        "owner": (
            cursor,
            other.execution,
            WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        ),
        "collection": (
            _signed_cursor(cursor, collection="topology_nodes"),
            published.execution,
            WorkflowProgressReadErrorCode.INVALID_CURSOR,
        ),
        "order": (
            _signed_cursor(cursor, order="node_id_desc"),
            published.execution,
            WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        ),
    }

    for value, execution, expected in cases.values():
        assert (
            _error_code(
                lambda value=value, execution=execution: list_workflow_node_details(
                    execution,
                    authorize=_allow,
                    cursor=value,
                    limit=100,
                )
            )
            is expected
        )


def test_detail_cursor_carries_strict_publication_identity_and_cumulative_seen() -> None:
    published = publish_initial_workflow(250, case_id=127_124)
    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=100,
    )
    first_cursor = first["next_cursor"]
    assert first_cursor is not None
    first_payload = _decoded_cursor(first_cursor)

    second = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        cursor=first_cursor,
        limit=100,
    )
    second_cursor = second["next_cursor"]
    assert second_cursor is not None
    second_payload = _decoded_cursor(second_cursor)

    assert set(first_payload) == reads._CURSOR_FIELDS
    assert first_payload["run_identity"] == first["run_identity"]
    assert first_payload["summary_revision"] == first["publication"]["summary_revision"]
    assert first_payload["seen"] == first["returned_count"] == 100
    assert second_payload["seen"] == 200
    assert second_payload["run_identity"] == first_payload["run_identity"]
    assert second_payload["summary_revision"] == first_payload["summary_revision"]


def test_topology_cursor_binds_summary_but_not_detail_revision() -> None:
    published = publish_initial_workflow(101, case_id=127_125)

    first = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=100,
    )
    cursor = first["next_cursor"]
    assert cursor is not None
    payload = _decoded_cursor(cursor)

    assert payload["run_identity"] == first["run_identity"]
    assert payload["summary_revision"] == first["publication"]["summary_revision"]
    assert payload["topology_version"] == first["publication"]["topology_version"]
    assert payload["detail_revision"] is None
    assert payload["seen"] == 100


def test_cursor_rejects_invalid_bound_identity_revision_and_seen_shapes() -> None:
    published = publish_initial_workflow(101, case_id=127_126)
    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=100,
    )
    cursor = first["next_cursor"]
    assert cursor is not None
    payload = _decoded_cursor(cursor)
    identity = payload["run_identity"]
    assert isinstance(identity, dict)
    invalid_identities = [
        {},
        {**identity, "task_execution_pk": published.execution.pk},
        {**identity, "schema_version": 2},
        {**identity, "schema_version": True},
        {**identity, "run_id": "not-a-uuid"},
        {**identity, "attempt_number": 0},
        {**identity, "attempt_number": True},
        {**identity, "execution_generation": -1},
        {**identity, "execution_generation": True},
    ]
    invalid_updates = [{"run_identity": value} for value in invalid_identities] + [
        {"summary_revision": 0},
        {"summary_revision": True},
        {"summary_revision": "1"},
        {"seen": -1},
        {"seen": True},
        {"seen": "100"},
    ]

    for update in invalid_updates:
        invalid = _signed_cursor(cursor, **update)
        try:
            list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=invalid,
                limit=100,
            )
        except WorkflowProgressReadError as error:
            assert error.code is WorkflowProgressReadErrorCode.INVALID_CURSOR
        else:
            pytest.fail(f"cursor accepted invalid bound fields: {update!r}")


@pytest.mark.parametrize("collection", ["detail", "topology"])
def test_expired_cursor_preserves_its_original_public_run_and_publication(
    collection: str,
) -> None:
    published = publish_initial_workflow(
        101,
        case_id=127_127 if collection == "detail" else 127_128,
    )
    if collection == "detail":
        first = list_workflow_node_details(
            published.execution,
            authorize=_allow,
            limit=100,
        )
    else:
        first = list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            limit=100,
        )
    cursor = first["next_cursor"]
    assert cursor is not None
    original_identity = first["run_identity"]
    original_publication = {
        **first["publication"],
        "detail_revision": (
            first["publication"]["detail_revision"] if collection == "detail" else None
        ),
    }

    published.execution.execution_generation = 2
    published.execution.workflow_run_id = "00000000-0000-0000-0000-000000127129"
    published.execution.save(update_fields=["execution_generation", "workflow_run_id"])
    _store_summary(
        published.execution,
        workflow_progress_summary(published.execution),
    )

    if collection == "detail":
        retired = list_workflow_node_details(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            limit=100,
        )
    else:
        retired = list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            cursor=cursor,
            limit=100,
        )

    assert retired["availability"] == "EXPIRED"
    assert retired["complete"] is False
    assert retired["run_identity"] == original_identity
    assert retired["publication"] == original_publication
    assert retired["items"] == []
    assert retired["next_cursor"] is None


@pytest.mark.parametrize("corruption", ["manifest", "page", "node"])
def test_corrupted_storage_metadata_returns_only_the_bounded_error(
    corruption: str,
) -> None:
    published = publish_initial_workflow(
        1,
        case_id={"manifest": 127_115, "page": 127_116, "node": 127_117}[corruption],
    )
    if corruption == "manifest":
        WorkflowProgressTopologyManifest.objects.filter(pk=published.manifest_id).update(
            manifest_digest="f" * 64
        )
    elif corruption == "page":
        WorkflowProgressTopologyPage.objects.filter(
            run_storage__execution=published.execution
        ).update(digest="e" * 64)
    else:
        WorkflowProgressNodeDetail.objects.filter(
            run_storage__execution=published.execution,
            node_id=workflow_node_id(0),
        ).update(digest="d" * 64)

    def operation() -> dict[str, Any]:
        if corruption in {"manifest", "page"}:
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
            )
        return get_workflow_node_detail(
            published.execution,
            workflow_node_id(0),
            authorize=_allow,
        )

    with pytest.raises(WorkflowProgressReadError) as raised:
        operation()

    assert raised.value.code is WorkflowProgressReadErrorCode.CORRUPT
    assert str(raised.value) == "Workflow progress storage failed validation."
    assert "digest" not in str(raised.value).lower()


def test_sparse_publication_accepts_unchanged_rows_with_older_epochs() -> None:
    published = publish_initial_workflow(3, case_id=127_118)
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(2), state="RUNNING"),
        identity=published.identity,
    )
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=3,
            running_count=1,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted
    unchanged_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=workflow_node_id(0),
    )
    changed_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=workflow_node_id(2),
    )

    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
    )

    assert unchanged_row.last_detail_revision == 1
    assert changed_row.last_detail_revision == 2
    assert node["publication"]["detail_revision"] == 2
    assert node["found"] is True
    assert node["item"]["state"] == "PENDING"


def test_detail_final_page_reports_a_missing_retained_child() -> None:
    published = publish_initial_workflow(4, case_id=127_130)
    last_row = (
        WorkflowProgressNodeDetail.objects.filter(run_storage__execution=published.execution)
        .order_by("-node_key")
        .first()
    )
    assert last_row is not None
    last_row.delete()

    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=2,
    )
    assert first["returned_count"] == 2
    assert first["next_cursor"] is not None

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=first["next_cursor"],
                limit=2,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_final_page_reports_an_unaccounted_extra_child() -> None:
    published = publish_initial_workflow(4, case_id=127_131)
    run_storage = WorkflowProgressRunStorage.objects.get(execution=published.execution)
    extra = prepare_workflow_progress_node_detail(
        workflow_detail("unaccounted-extra-node"),
        identity=published.identity,
    )
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=extra.node_key,
        node_id=extra.node_id,
        invocation_id=extra.invocation_id,
        state=extra.state,
        event_count=extra.event_count,
        truncated=extra.truncated,
        payload=extra.payload,
        digest=extra.digest,
        encoded_bytes=extra.encoded_bytes,
        decoded_bytes=extra.decoded_bytes,
        last_topology_version=1,
        last_detail_revision=1,
    )

    cursor = None
    observed_code = None
    for _ in range(4):
        try:
            page = list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=cursor,
                limit=2,
            )
        except WorkflowProgressReadError as error:
            observed_code = error.code
            break
        cursor = page["next_cursor"]
        assert cursor is not None, "extra retained row was silently accepted"

    assert observed_code is WorkflowProgressReadErrorCode.CORRUPT


def test_filtered_detail_final_page_uses_the_expected_state_count() -> None:
    published = publish_initial_workflow(5, case_id=127_132)
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(0), state="RUNNING"),
        identity=published.identity,
    )
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=5,
            running_count=1,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted
    running = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="RUNNING",
        limit=2,
    )
    assert running["returned_count"] == 1
    assert running["next_cursor"] is None

    last_pending = (
        WorkflowProgressNodeDetail.objects.filter(
            run_storage__execution=published.execution,
            state="PENDING",
        )
        .order_by("-node_key")
        .first()
    )
    assert last_pending is not None
    last_pending.delete()
    first = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        state="PENDING",
        limit=2,
    )
    assert first["returned_count"] == 2
    assert first["next_cursor"] is not None

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                state="PENDING",
                cursor=first["next_cursor"],
                limit=2,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_explicit_current_attempt_needs_no_archived_attempt_row() -> None:
    published = publish_initial_workflow(1, case_id=127_119)
    assert not TaskAttempt.objects.filter(execution=published.execution).exists()

    summary = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        attempt_number=published.execution.attempt_number,
    )
    page = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        attempt_number=published.execution.attempt_number,
    )

    assert summary["run_identity"]["attempt_number"] == 1
    assert page["returned_count"] == 1


@pytest.mark.parametrize(
    "operation",
    [
        lambda execution: get_workflow_progress_summary(
            execution,
            authorize=_allow,
            attempt_number=99,
        ),
        lambda execution: list_workflow_node_details(
            execution,
            authorize=_allow,
            attempt_number=99,
        ),
        lambda execution: get_workflow_node_detail(
            execution,
            workflow_node_id(0),
            authorize=_allow,
            attempt_number=99,
        ),
    ],
)
def test_missing_archived_attempt_is_not_found(operation) -> None:
    published = publish_initial_workflow(1, case_id=127_120)

    assert (
        _error_code(lambda: operation(published.execution))
        is WorkflowProgressReadErrorCode.NOT_FOUND
    )


def test_page_construction_obeys_decoded_response_and_cursor_caps(monkeypatch) -> None:
    published = publish_initial_workflow(50, case_id=127_121)
    decoded_limit = 700
    response_limit = 2_048
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES", decoded_limit)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", response_limit)

    page = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        limit=256,
    )

    assert 0 < page["returned_count"] < 50
    assert sum(len(_canonical_bytes(item)) for item in page["items"]) <= decoded_limit
    assert len(_wire_bytes(page)) <= response_limit
    assert page["next_cursor"] is not None
    assert len(page["next_cursor"].encode("utf-8")) <= WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES
    assert decoded_limit < WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES


def test_v3_summary_only_sql_never_references_legacy_or_detail_storage() -> None:
    published = publish_initial_workflow(1, case_id=127_122)
    deferred = RayTaskExecution.objects.only("pk").get(pk=published.execution.pk)

    with CaptureQueriesContext(connection) as queries:
        response = get_workflow_progress_summary(
            deferred,
            authorize=_allow,
            include_legacy=False,
        )

    statements = "\n".join(query["sql"] for query in queries.captured_queries).lower()
    assert response["source_schema_version"] == 3
    assert "progress_data" not in statements
    assert "workflowprogressnodedetail" not in statements
    assert "workflowprogresstopolog" not in statements
    assert "workflowprogressrunstorage" not in statements
    assert len(queries) <= 2


def test_compatibility_reader_routes_through_the_execution_database_alias(
    monkeypatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="compatibility-reader-database-alias",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
        progress_data=json.dumps({"schema_version": 1, "revision": 1}),
    )
    manager = RayTaskExecution.objects
    original_using = manager.using
    aliases: list[str] = []

    def routed_using(alias: str):
        aliases.append(alias)
        return original_using("default")

    monkeypatch.setattr(manager, "using", routed_using)
    execution._state.db = "workflow-progress-replica"

    result = read_workflow_progress(execution)

    assert result.ok
    assert result.schema_version == 1
    assert aliases == ["workflow-progress-replica"]


def test_invalid_execution_attempt_authorizer_and_deleted_owner_are_bounded() -> None:
    unsaved = RayTaskExecution(
        task_id="unsaved-workflow-read",
        callable_path="tests.invalid.workflow",
    )
    assert (
        _error_code(lambda: get_workflow_progress_summary(unsaved, authorize=_allow))
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    unsaved.pk = 1 << 63
    assert (
        _error_code(lambda: get_workflow_progress_summary(unsaved, authorize=_allow))
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )

    published = publish_initial_workflow(1, case_id=127_201)
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
                attempt_number=0,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
                attempt_number=1 << 63,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=None,  # type: ignore[arg-type]
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )

    stale = published.execution
    RayTaskExecution.objects.filter(pk=stale.pk).delete()
    assert (
        _error_code(lambda: get_workflow_progress_summary(stale, authorize=_allow))
        is WorkflowProgressReadErrorCode.NOT_FOUND
    )


def test_current_summary_must_match_the_current_run_generation() -> None:
    published = publish_initial_workflow(1, case_id=127_202)
    summary = _stored_summary(published.execution)
    summary["run_identity"]["execution_generation"] = 2
    _store_summary(published.execution, summary)

    assert (
        _error_code(lambda: get_workflow_progress_summary(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_invalid_persisted_current_attempt_fails_closed() -> None:
    published = publish_initial_workflow(1, case_id=127_2021)
    RayTaskExecution.objects.filter(pk=published.execution.pk).update(attempt_number=0)
    published.execution.refresh_from_db()

    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_naive_generated_at_is_normalized_before_terminal_expiry_comparison() -> None:
    published = publish_initial_workflow(1, case_id=127_203)
    summary = _stored_summary(published.execution)
    finished_at = "2026-07-20T12:00:02Z"
    summary.update(state="SUCCEEDED", progress_percent=100.0)
    summary["node_counts"].update(pending=0, succeeded=1)
    summary["timestamps"].update(updated_at=finished_at, finished_at=finished_at)
    summary["retention"].update(detail_days=0, detail_expires_at=finished_at)
    summary["terminal"] = {"outcome": "SUCCEEDED", "finished_at": finished_at}
    _store_summary(published.execution, summary)

    response = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        generated_at=datetime(2026, 7, 20, 12, 0, 3),
    )

    assert response["generated_at"] == "2026-07-20T12:00:03Z"
    assert response["availability"] == "EXPIRED"


def test_invalid_protocol_expiry_is_ignored_without_exposing_storage(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_204)
    original = reads.public_workflow_progress_summary

    def invalid_expiry(summary: dict[str, Any]) -> dict[str, Any]:
        public = original(summary)
        public["retention"]["detail_expires_at"] = "not-a-protocol-time"
        return public

    monkeypatch.setattr(reads, "public_workflow_progress_summary", invalid_expiry)

    response = get_workflow_progress_summary(published.execution, authorize=_allow)

    assert response["availability"] == "AVAILABLE"
    assert response["summary"]["storage"]["manifest_id"] is None

    def naive_expiry(summary: dict[str, Any]) -> dict[str, Any]:
        public = original(summary)
        public["retention"]["detail_expires_at"] = "2099-07-20T12:00:00"
        return public

    monkeypatch.setattr(reads, "public_workflow_progress_summary", naive_expiry)
    still_available = get_workflow_progress_summary(
        published.execution,
        authorize=_allow,
        generated_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )
    assert still_available["availability"] == "AVAILABLE"


def test_invalid_generated_at_timezone_is_a_bounded_argument() -> None:
    published = publish_initial_workflow(1, case_id=127_239)

    class _BrokenTimezone(tzinfo):
        def utcoffset(self, _value):
            raise ValueError("untrusted timezone implementation detail")

        def dst(self, _value):
            return None

    generated_at = datetime(2026, 7, 20, tzinfo=_BrokenTimezone())

    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                published.execution,
                authorize=_allow,
                generated_at=generated_at,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )


def test_legacy_numeric_fields_are_bounded_across_finite_and_overflow_inputs() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="legacy-numeric-bounds-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
    )
    huge = 10**1_000
    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "progress_percent": huge,
            "updated_at": 123.25,
        }
    )
    execution.save(update_fields=["progress_data"])

    finite = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    assert finite["summary"]["progress_percent"] == 0.0
    assert finite["summary"]["updated_at"] == 123.25

    execution.progress_data = json.dumps(
        {
            "schema_version": 1,
            "progress_percent": 25,
            "updated_at": huge,
        }
    )
    execution.save(update_fields=["progress_data"])
    overflow = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    assert overflow["summary"]["progress_percent"] == 25.0
    assert overflow["summary"]["updated_at"] == 0.0


def test_compatibility_reader_diagnostics_and_summary_race_map_to_public_contract(
    monkeypatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="compatibility-result-mapping-127",
        callable_path="tests.legacy.workflow",
        state=TaskState.RUNNING,
        workflow_run_id="00000000-0000-0000-0000-000000127205",
    )
    cases = [
        (
            WorkflowProgressReadResult(
                source=WorkflowProgressReadSource.NONE,
                diagnostic_code=WorkflowProgressDiagnosticCode.ROW_MISSING,
            ),
            WorkflowProgressReadErrorCode.NOT_FOUND,
        ),
        (
            WorkflowProgressReadResult(
                source=WorkflowProgressReadSource.LEGACY,
                diagnostic_code=WorkflowProgressDiagnosticCode.MALFORMED_JSON,
            ),
            WorkflowProgressReadErrorCode.CORRUPT,
        ),
    ]
    for result, expected in cases:
        monkeypatch.setattr(reads, "read_workflow_progress", lambda _execution, r=result: r)
        assert (
            _error_code(
                lambda: get_workflow_progress_summary(
                    execution,
                    authorize=_allow,
                    include_legacy=True,
                )
            )
            is expected
        )

    summary = workflow_progress_summary(execution)
    monkeypatch.setattr(
        reads,
        "read_workflow_progress",
        lambda _execution: WorkflowProgressReadResult(
            source=WorkflowProgressReadSource.SUMMARY,
            payload=summary,
            schema_version=3,
        ),
    )
    recovered = get_workflow_progress_summary(
        execution,
        authorize=_allow,
        include_legacy=True,
    )
    assert recovered["source_schema_version"] == 3
    assert recovered["summary"]["schema_version"] == 3

    mismatched_legacy = {
        "schema_version": 2,
        "run_identity": {
            "schema_version": 1,
            "run_id": "00000000-0000-0000-0000-000000127999",
            "task_execution_pk": execution.pk,
            "attempt_number": execution.attempt_number,
            "execution_generation": execution.execution_generation,
        },
    }
    monkeypatch.setattr(
        reads,
        "read_workflow_progress",
        lambda _execution: WorkflowProgressReadResult(
            source=WorkflowProgressReadSource.LEGACY,
            payload=mismatched_legacy,
            schema_version=2,
        ),
    )
    assert (
        _error_code(
            lambda: get_workflow_progress_summary(
                execution,
                authorize=_allow,
                include_legacy=True,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_cursor_transport_rejects_empty_nontext_nonencodable_and_nonobject_values() -> None:
    published = publish_initial_workflow(2, case_id=127_206)
    invalid_cursors = [
        "",
        object(),
        "\ud800",
        signing.dumps([], salt=reads._CURSOR_SALT, compress=False),
        signing.dumps({"unexpected": True}, salt=reads._CURSOR_SALT, compress=False),
    ]

    for cursor in invalid_cursors:
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_node_details(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,  # type: ignore[arg-type]
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )

    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                cursor="",
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_CURSOR
    )


def test_resigned_cursor_rejects_malformed_bound_fields_and_positions() -> None:
    published = publish_initial_workflow(2, case_id=127_207)
    detail = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    topology = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=1)
    detail_cursor = detail["next_cursor"]
    topology_cursor = topology["next_cursor"]
    assert detail_cursor is not None
    assert topology_cursor is not None

    identity = _decoded_cursor(detail_cursor)["run_identity"]
    malformed_detail = [
        _signed_cursor(detail_cursor, position=[0, 1]),
        _signed_cursor(detail_cursor, after="g" * 64),
        _signed_cursor(detail_cursor, run="g" * 64),
        _signed_cursor(detail_cursor, run="0" * 64),
        _signed_cursor(
            detail_cursor,
            run_identity={**identity, "run_id": "z" * 36},
        ),
    ]
    for cursor in malformed_detail:
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_node_details(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )

    malformed_topology = [
        _signed_cursor(topology_cursor, position=None),
        _signed_cursor(topology_cursor, after=""),
        _signed_cursor(topology_cursor, detail_revision=1),
    ]
    for cursor in malformed_topology:
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_topology_nodes(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )

    for malformed_after in (None, ["only-one"], ["", "target"]):
        cursor = _signed_cursor(
            topology_cursor,
            collection="topology_edges",
            order="source_target_asc",
            after=malformed_after,
        )
        assert (
            _error_code(
                lambda cursor=cursor: list_workflow_topology_edges(
                    published.execution,
                    authorize=_allow,
                    cursor=cursor,
                    limit=1,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_CURSOR
        )


def test_genuine_cross_endpoint_cursors_report_request_mismatch() -> None:
    published = publish_initial_workflow(2, case_id=127_208)
    detail = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    topology = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=1)
    assert detail["next_cursor"] is not None
    assert topology["next_cursor"] is not None

    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                cursor=detail["next_cursor"],
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=topology["next_cursor"],
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )


def test_cursor_without_a_current_summary_returns_its_original_expired_envelope() -> None:
    published = publish_initial_workflow(2, case_id=127_209)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    cursor = first["next_cursor"]
    assert cursor is not None
    RayTaskExecution.objects.filter(pk=published.execution.pk).update(
        workflow_progress_summary_json=None
    )

    expired = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        cursor=cursor,
        limit=1,
    )

    assert expired["availability"] == "EXPIRED"
    assert expired["run_identity"] == first["run_identity"]
    assert expired["publication"] == first["publication"]


@pytest.mark.parametrize("collection", ["detail", "topology"])
def test_matching_cursor_epoch_uses_expired_page_after_time_retention(
    collection: str,
    monkeypatch,
) -> None:
    published = publish_initial_workflow(
        2,
        case_id=127_210 if collection == "detail" else 127_211,
    )
    operation = (
        list_workflow_node_details if collection == "detail" else list_workflow_topology_nodes
    )
    first = operation(published.execution, authorize=_allow, limit=1)
    cursor = first["next_cursor"]
    assert cursor is not None
    summary = _stored_summary(published.execution)
    finished_at = "2026-07-20T12:00:02Z"
    summary.update(state="SUCCEEDED", progress_percent=100.0)
    summary["node_counts"].update(pending=0, succeeded=2)
    summary["timestamps"].update(updated_at=finished_at, finished_at=finished_at)
    summary["retention"].update(detail_days=0, detail_expires_at=finished_at)
    summary["terminal"] = {"outcome": "SUCCEEDED", "finished_at": finished_at}
    _store_summary(published.execution, summary)
    after_expiry = datetime(2026, 7, 20, 12, 0, 3, tzinfo=UTC)

    class _AfterExpiry(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_expiry if tz is not None else after_expiry.replace(tzinfo=None)

    monkeypatch.setattr(reads, "datetime", _AfterExpiry)
    expired = operation(
        published.execution,
        authorize=_allow,
        cursor=cursor,
        limit=1,
    )

    assert expired["availability"] == "EXPIRED"
    assert expired["run_identity"] == first["run_identity"]
    expected_publication = dict(first["publication"])
    if collection == "topology":
        expected_publication["detail_revision"] = None
    assert expired["publication"] == expected_publication
    assert expired["items"] == []


def test_read_size_caps_fail_closed_for_cursor_summary_empty_and_expired_pages(
    monkeypatch,
) -> None:
    cursor_workflow = publish_initial_workflow(2, case_id=127_212)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES", 1)
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                cursor_workflow.execution,
                authorize=_allow,
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )
    monkeypatch.setattr(
        reads,
        "WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES",
        WORKFLOW_PROGRESS_READ_MAX_CURSOR_BYTES,
    )

    empty_execution = _summary_only_execution("NOT_REPORTED", case_id=127_213)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)
    assert (
        _error_code(lambda: list_workflow_topology_nodes(empty_execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )
    assert (
        _error_code(lambda: get_workflow_progress_summary(empty_execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    monkeypatch.setattr(
        reads,
        "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES",
        WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES,
    )
    expired_workflow = publish_initial_workflow(2, case_id=127_214)
    first = list_workflow_node_details(
        expired_workflow.execution,
        authorize=_allow,
        limit=1,
    )
    assert first["next_cursor"] is not None
    RayTaskExecution.objects.filter(pk=expired_workflow.execution.pk).update(
        workflow_progress_summary_json=None
    )
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                expired_workflow.execution,
                authorize=_allow,
                cursor=first["next_cursor"],
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_topology_response_shrinks_to_the_wire_cap(monkeypatch) -> None:
    published = publish_initial_workflow(10, case_id=127_215)
    full = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=10)
    full_size = len(_wire_bytes(full))
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", full_size - 1)

    bounded = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=10)

    assert 0 < bounded["returned_count"] < 10
    assert bounded["next_cursor"] is not None
    assert len(_wire_bytes(bounded)) <= full_size - 1


def test_topology_pages_keep_stable_node_and_edge_order_across_cursors() -> None:
    published = _publish_workflow_with_edges(257, 257, case_id=127_217)
    node_first = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=256,
    )
    assert node_first["next_cursor"] is not None
    node_second = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        cursor=node_first["next_cursor"],
        limit=256,
    )
    assert node_second["items"][0]["node_id"] == workflow_node_id(256)

    edge_first = list_workflow_topology_edges(
        published.execution,
        authorize=_allow,
        limit=256,
    )
    assert edge_first["next_cursor"] is not None
    edge_second = list_workflow_topology_edges(
        published.execution,
        authorize=_allow,
        cursor=edge_first["next_cursor"],
        limit=256,
    )
    first_key = [edge_first["items"][0]["source"], edge_first["items"][0]["target"]]
    second_key = [edge_second["items"][0]["source"], edge_second["items"][0]["target"]]
    assert first_key < second_key


def test_topology_cursor_positions_are_checked_against_authenticated_pages() -> None:
    published = publish_initial_workflow(257, case_id=127_218)
    first = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=100)
    boundary = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        limit=256,
    )
    cursor = first["next_cursor"]
    boundary_cursor = boundary["next_cursor"]
    assert cursor is not None
    assert boundary_cursor is not None

    cases = [
        (_signed_cursor(cursor, position=[99, 0], seen=0), 100),
        (_signed_cursor(cursor, seen=99), 100),
        (_signed_cursor(cursor, position=[0, 999], seen=999), 100),
        (_signed_cursor(cursor, after="wrong-stable-key"), 100),
        (_signed_cursor(boundary_cursor, after="zzzzzzzz"), 256),
    ]
    expected_codes = [
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH,
        WorkflowProgressReadErrorCode.CORRUPT,
    ]
    for (value, limit), expected in zip(cases, expected_codes, strict=True):
        assert (
            _error_code(
                lambda value=value, limit=limit: list_workflow_topology_nodes(
                    published.execution,
                    authorize=_allow,
                    cursor=value,
                    limit=limit,
                )
            )
            is expected
        )

    edge_cursor = _signed_cursor(
        cursor,
        collection="topology_edges",
        order="source_target_asc",
        after=[workflow_node_id(99), workflow_node_id(100)],
    )
    assert (
        _error_code(
            lambda: list_workflow_topology_edges(
                published.execution,
                authorize=_allow,
                cursor=edge_cursor,
                limit=100,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )


def test_topology_missing_manifest_link_and_page_are_bounded_failures() -> None:
    missing_manifest = publish_initial_workflow(1, case_id=127_219)
    WorkflowProgressTopologyManifest.objects.filter(pk=missing_manifest.manifest_id).delete()
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                missing_manifest.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )

    missing_link = publish_initial_workflow(1, case_id=127_220)
    WorkflowProgressTopologyManifestPage.objects.filter(
        manifest_id=missing_link.manifest_id,
        collection="NODE",
        page_index=0,
    ).delete()
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                missing_link.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_topology_rejects_summary_count_and_unexpected_link_mismatches() -> None:
    count_mismatch = publish_initial_workflow(2, case_id=127_221)
    summary = _stored_summary(count_mismatch.execution)
    summary["node_counts"].update(retained_topology=1, retained_detail=1)
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(count_mismatch.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=count_mismatch.execution).update(
        detail_truncation_reasons="detail_count_limit"
    )
    assert (
        _error_code(
            lambda: list_workflow_topology_nodes(
                count_mismatch.execution,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    extra_link = publish_initial_workflow(1, case_id=127_222)
    foreign = publish_initial_workflow(1, case_id=127_223)
    foreign_page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=foreign.execution,
        collection="NODE",
    )
    WorkflowProgressTopologyManifestPage.objects.create(
        manifest_id=extra_link.manifest_id,
        page=foreign_page,
        collection="NODE",
        page_index=1,
    )
    assert (
        _error_code(lambda: list_workflow_topology_nodes(extra_link.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_topology_decoded_record_budget_rejects_an_unreadable_first_item(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_224)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES", 0)

    assert (
        _error_code(lambda: list_workflow_topology_nodes(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_topology_decoded_budget_stops_before_the_second_item(monkeypatch) -> None:
    published = publish_initial_workflow(2, case_id=127_225)
    full = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=2)
    first_item_bytes = len(_canonical_bytes(full["items"][0]))
    monkeypatch.setattr(
        reads,
        "WORKFLOW_PROGRESS_READ_MAX_DECODED_RECORD_BYTES",
        first_item_bytes,
    )

    bounded = list_workflow_topology_nodes(published.execution, authorize=_allow, limit=2)

    assert bounded["returned_count"] == 1
    assert bounded["next_cursor"] is not None


def test_persisted_truncation_reason_corruption_is_rejected_by_both_page_families() -> None:
    published = publish_initial_workflow(1, case_id=127_226)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_truncation_reasons="not-a-protocol-reason"
    )

    assert (
        _error_code(lambda: list_workflow_topology_nodes(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )
    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_detail_summary_and_run_epoch_counts_must_agree() -> None:
    published = publish_initial_workflow(1, case_id=127_227)
    summary = _stored_summary(published.execution)
    summary["node_counts"]["retained_detail"] = 0
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(published.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_truncation_reasons="detail_count_limit"
    )

    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_missing_detail_epoch_is_reported_for_page_and_single_node_reads() -> None:
    published = publish_initial_workflow(1, case_id=127_228)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).delete()

    assert (
        _error_code(lambda: list_workflow_node_details(published.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.MISSING
    )
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_cursor_seen_and_after_must_match_the_retained_epoch() -> None:
    published = publish_initial_workflow(3, case_id=127_229)
    first = list_workflow_node_details(published.execution, authorize=_allow, limit=1)
    cursor = first["next_cursor"]
    assert cursor is not None

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=_signed_cursor(cursor, seen=4),
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CURSOR_MISMATCH
    )
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                cursor=_signed_cursor(cursor, after="f" * 64),
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_metadata_and_payload_races_fail_closed(monkeypatch) -> None:
    malformed = publish_initial_workflow(1, case_id=127_230)
    WorkflowProgressNodeDetail.objects.filter(run_storage__execution=malformed.execution).update(
        node_key="short"
    )
    assert (
        _error_code(lambda: list_workflow_node_details(malformed.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    disappeared = publish_initial_workflow(1, case_id=127_231)
    monkeypatch.setattr(reads, "_detail_payload_rows", lambda _query, _keys: [])
    assert (
        _error_code(lambda: list_workflow_node_details(disappeared.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.MISSING
    )


def test_detail_list_verifies_payload_digest_and_final_retained_count() -> None:
    corrupted = publish_initial_workflow(1, case_id=127_232)
    WorkflowProgressNodeDetail.objects.filter(run_storage__execution=corrupted.execution).update(
        digest="d" * 64
    )
    assert (
        _error_code(lambda: list_workflow_node_details(corrupted.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    extra = publish_initial_workflow(1, case_id=127_233)
    run_storage = WorkflowProgressRunStorage.objects.get(execution=extra.execution)
    prepared = prepare_workflow_progress_node_detail(
        workflow_detail("unaccounted-final-node"),
        identity=extra.identity,
    )
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=prepared.node_key,
        node_id=prepared.node_id,
        invocation_id=prepared.invocation_id,
        state=prepared.state,
        event_count=prepared.event_count,
        truncated=prepared.truncated,
        payload=prepared.payload,
        digest=prepared.digest,
        encoded_bytes=prepared.encoded_bytes,
        decoded_bytes=prepared.decoded_bytes,
        last_topology_version=1,
        last_detail_revision=1,
    )
    assert (
        _error_code(lambda: list_workflow_node_details(extra.execution, authorize=_allow))
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_detail_response_too_small_for_one_cursor_page_fails_closed(monkeypatch) -> None:
    published = publish_initial_workflow(2, case_id=127_234)
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)

    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                limit=1,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_public_detail_redacts_private_execution_pk_from_invocation_identity() -> None:
    published = publish_initial_workflow(1, case_id=127_235)
    detail = workflow_detail(workflow_node_id(0))
    detail["invocation_identity"] = {
        **published.identity.as_dict(),
        "invocation_id": "bounded-public-invocation",
    }
    changed = prepare_workflow_progress_node_detail(detail, identity=published.identity)
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=1,
            running_count=0,
        ),
        manifest_id=published.manifest_id,
        prepared_topology=published.topology,
        detail_records=(changed,),
    )
    assert result.accepted

    page = list_workflow_node_details(published.execution, authorize=_allow)
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
    )

    assert page["items"][0]["invocation_identity"]["invocation_id"] == ("bounded-public-invocation")
    assert "task_execution_pk" not in page["items"][0]["invocation_identity"]
    assert "task_execution_pk" not in node["item"]["invocation_identity"]


def test_public_detail_exposes_only_normalized_metric_and_resource_keys() -> None:
    published = publish_initial_workflow(1, case_id=127_250)
    node_input = workflow_node(workflow_node_id(0))
    node_input["runtime_env"] = {"\x1b[31mprofile\x1b[0m": "default"}
    node_input["ray_options"] = {
        "metadata": {"\x9dsafe\x18queue": "ordinary"},
    }
    topology = prepare_workflow_progress_topology(
        published.identity,
        2,
        (node_input,),
        (),
    )
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    detail = workflow_detail(workflow_node_id(0), state="RUNNING")
    detail["progress"] = {
        "current": 1,
        "total": 2,
        "percent": 50,
        "message": None,
        "metrics": {"\x1b[32mrows\x1b[0m": 12},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    detail["execution"] = {
        "ray_task_id": None,
        "ray_job_id": None,
        "ray_node_id": None,
        "ray_worker_id": None,
        "assigned_resources": {"\x1b[33mCPU\x1b[0m": 1.0},
    }
    changed = prepare_workflow_progress_node_detail(detail, identity=published.identity)
    result = persist_workflow_progress_publication(
        published.identity,
        workflow_summary(
            published.identity,
            summary_revision=2,
            node_count=1,
            running_count=1,
        ),
        manifest_id=manifest_id,
        prepared_topology=topology,
        detail_records=(changed,),
    )
    assert result.accepted

    topology_page = list_workflow_topology_nodes(published.execution, authorize=_allow)
    page = list_workflow_node_details(published.execution, authorize=_allow)
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
    )

    assert topology_page["items"][0]["runtime_env"] == {"profile": "default"}
    assert topology_page["items"][0]["ray_options"] == {"metadata": {"queue": "ordinary"}}
    assert page["items"][0]["progress"]["metrics"] == {"rows": 12}
    assert page["items"][0]["execution"]["assigned_resources"] == {"CPU": 1.0}
    assert node["item"]["progress"]["metrics"] == {"rows": 12}
    assert node["item"]["execution"]["assigned_resources"] == {"CPU": 1.0}
    assert "\x1b" not in repr(topology_page)
    assert "\x1b" not in repr(page)
    assert "\x1b" not in repr(node)


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
def test_authenticated_legacy_ansi_records_are_normalized_only_for_presentation(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
) -> None:
    published = _legacy_ansi_workflow(monkeypatch, case_id=127_251)
    node_page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=published.execution,
        collection="NODE",
    )
    detail_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=workflow_node_id(0),
    )
    original_node_payload = bytes(node_page.payload)
    original_node_digest = node_page.digest
    original_detail_payload = bytes(detail_row.payload)
    original_detail_digest = detail_row.digest
    assert b"\\u001b" in original_node_payload
    assert b"\\u001b" in original_detail_payload
    attempt_number = _archive_workflow_attempt(published, next_run=127_252) if archived else None

    topology = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    edges = list_workflow_topology_edges(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    details = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    node = get_workflow_node_detail(
        published.execution,
        workflow_node_id(0),
        authorize=_allow,
        attempt_number=attempt_number,
    )

    topology_by_id = {item["node_id"]: item for item in topology["items"]}
    detail_by_id = {item["node_id"]: item for item in details["items"]}
    assert topology_by_id[workflow_node_id(0)]["label"] == "Prepare"
    assert topology_by_id[workflow_node_id(0)]["runtime_env"] == {"profile": "default"}
    assert edges["items"] == [{"source": workflow_node_id(0), "target": workflow_node_id(1)}]
    assert detail_by_id[workflow_node_id(0)]["progress"]["message"] == "Working"
    assert detail_by_id[workflow_node_id(0)]["progress"]["metrics"] == {"rows": 1}
    assert detail_by_id[workflow_node_id(0)]["recent_events"][0]["label"] == "Started"
    assert node["item"] == detail_by_id[workflow_node_id(0)]
    assert "\x1b" not in repr((topology, edges, details, node))

    node_page.refresh_from_db()
    detail_row.refresh_from_db()
    assert bytes(node_page.payload) == original_node_payload
    assert node_page.digest == original_node_digest
    assert bytes(detail_row.payload) == original_detail_payload
    assert detail_row.digest == original_detail_digest


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
def test_authenticated_records_follow_new_redaction_policy_only_in_presentation(
    monkeypatch: pytest.MonkeyPatch,
    settings,
    archived: bool,
) -> None:
    node_id = workflow_node_id(0)
    node = workflow_node(node_id)
    node.update(
        {
            "label": "newly-sensitive-label",
            "runtime_env": {
                "ordinary": "newly-sensitive-runtime-value",
                "newly-sensitive-runtime-key": "omitted",
            },
        }
    )
    detail = workflow_detail(node_id, state="RUNNING")
    detail["progress"] = {
        "current": 1.0,
        "total": 2.0,
        "percent": 50.0,
        "message": "newly-sensitive-message",
        "metrics": {
            "ordinary": "newly-sensitive-metric-value",
            "newly-sensitive-metric-key": 1,
        },
        "updated_at": "2026-07-20T12:00:00Z",
    }
    detail["execution"] = {
        "ray_task_id": None,
        "ray_job_id": None,
        "ray_node_id": None,
        "ray_worker_id": None,
        "assigned_resources": {
            "CPU": 1.0,
            "newly-sensitive-resource": 2.0,
        },
    }
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_259,
        nodes=[node],
        edges=[],
        details=[detail],
    )
    node_page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=published.execution,
        collection="NODE",
    )
    detail_row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=node_id,
    )
    original_node_payload = bytes(node_page.payload)
    original_node_digest = node_page.digest
    original_detail_payload = bytes(detail_row.payload)
    original_detail_digest = detail_row.digest
    attempt_number = _archive_workflow_attempt(published, next_run=127_260) if archived else None

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"newly-sensitive"],
    }

    topology = list_workflow_topology_nodes(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )
    details = list_workflow_node_details(
        published.execution,
        authorize=_allow,
        attempt_number=attempt_number,
    )

    assert topology["items"][0]["label"] == REDACTED
    assert topology["items"][0]["runtime_env"] == {"ordinary": REDACTED}
    assert details["items"][0]["progress"] == {
        "current": 1.0,
        "message": REDACTED,
        "metrics": {"ordinary": REDACTED},
        "percent": 50.0,
        "total": 2.0,
        "updated_at": "2026-07-20T12:00:00Z",
    }
    assert details["items"][0]["execution"]["assigned_resources"] == {"CPU": 1.0}
    assert details["items"][0]["truncated"] is True

    node_page.refresh_from_db()
    detail_row.refresh_from_db()
    assert bytes(node_page.payload) == original_node_payload
    assert node_page.digest == original_node_digest
    assert bytes(detail_row.payload) == original_detail_payload
    assert detail_row.digest == original_detail_digest


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["node", "edge", "detail"])
def test_legacy_identity_that_normalizes_is_rejected_without_remapping(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
    surface: str,
) -> None:
    unsafe_id = "\x1b[31mnode-source\x1b[0m"
    safe_id = "node-source"
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_253,
        nodes=[workflow_node(unsafe_id), workflow_node(safe_id)],
        edges=[{"source": unsafe_id, "target": safe_id}],
        details=[workflow_detail(unsafe_id), workflow_detail(safe_id)],
    )
    attempt_number = _archive_workflow_attempt(published, next_run=127_254) if archived else None
    operations = {
        "node": lambda: list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        ),
        "edge": lambda: list_workflow_topology_edges(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        ),
        "detail": lambda: list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        ),
    }

    assert storage.redact_text(unsafe_id) == safe_id
    assert _error_code(operations[surface]) is WorkflowProgressReadErrorCode.CORRUPT


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["topology", "detail"])
def test_new_redaction_policy_cannot_remap_an_authenticated_identity(
    monkeypatch: pytest.MonkeyPatch,
    settings,
    archived: bool,
    surface: str,
) -> None:
    node_id = "newly-sensitive-node-identity"
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_261,
        nodes=[workflow_node(node_id)],
        edges=[],
        details=[workflow_detail(node_id)],
    )
    attempt_number = _archive_workflow_attempt(published, next_run=127_262) if archived else None
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"newly-sensitive"],
    }

    def operation() -> dict[str, Any]:
        if surface == "topology":
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                attempt_number=attempt_number,
            )
        return list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )

    assert _error_code(operation) is WorkflowProgressReadErrorCode.CORRUPT


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["topology", "detail"])
def test_legacy_display_key_collisions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
    surface: str,
) -> None:
    node_id = workflow_node_id(0)
    node = workflow_node(node_id)
    node["runtime_env"] = {
        "profile": "first",
        "\x1b[31mprofile\x1b[0m": "second",
    }
    detail = workflow_detail(node_id, state="RUNNING")
    detail["progress"] = {
        "current": 1,
        "total": 2,
        "percent": 50,
        "message": None,
        "metrics": {"rows": 1, "\x1b[32mrows\x1b[0m": 2},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_255,
        nodes=[node],
        edges=[],
        details=[detail],
    )
    attempt_number = _archive_workflow_attempt(published, next_run=127_256) if archived else None

    def operation() -> dict[str, Any]:
        if surface == "topology":
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                attempt_number=attempt_number,
            )
        return list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )

    assert _error_code(operation) is WorkflowProgressReadErrorCode.CORRUPT


@pytest.mark.parametrize(
    "tampering",
    ["numeric_coercion", "oversized_message", "event_order", "event_retention"],
)
def test_recomputed_detail_digest_cannot_authenticate_a_noncanonical_record(
    monkeypatch: pytest.MonkeyPatch,
    tampering: str,
) -> None:
    node_id = workflow_node_id(0)
    detail = workflow_detail(node_id, state="RUNNING")
    detail["progress"] = {
        "current": 1.0,
        "total": 2.0,
        "percent": 50.0,
        "message": "working",
        "metrics": {},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    detail["recent_events"] = [
        {
            "event": "STATE_CHANGE",
            "state": "PENDING",
            "label": "queued",
            "timestamp": "2026-07-20T11:59:59Z",
        },
        {
            "event": "STATE_CHANGE",
            "state": "RUNNING",
            "label": "started",
            "timestamp": "2026-07-20T12:00:00Z",
        },
    ]
    published = _publish_with_legacy_terminal_redaction(
        monkeypatch,
        case_id=127_263,
        nodes=[workflow_node(node_id)],
        edges=[],
        details=[detail],
    )
    row = WorkflowProgressNodeDetail.objects.get(
        run_storage__execution=published.execution,
        node_id=node_id,
    )
    value = json.loads(bytes(row.payload))
    if tampering == "numeric_coercion":
        value["progress"]["current"] = 1
    elif tampering == "oversized_message":
        value["progress"]["message"] = "x" * (storage.WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES + 1)
    elif tampering == "event_order":
        value["recent_events"].reverse()
    else:
        value["recent_events"] = [
            {
                "event": "STATE_CHANGE",
                "state": "RUNNING",
                "label": f"event-{index:02d}",
                "timestamp": f"2026-07-20T12:00:{index:02d}Z",
            }
            for index in range(storage.WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS + 1)
        ]
        value["truncated"] = True
    payload = _canonical_bytes(value)
    updates: dict[str, Any] = {
        "payload": payload,
        "digest": storage._digest(storage._DETAIL_DOMAIN, payload),
        "encoded_bytes": len(payload),
        "decoded_bytes": len(payload),
    }
    if tampering == "event_retention":
        updates.update(
            event_count=storage.WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS,
            truncated=True,
        )
    WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(**updates)

    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                node_id,
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_recomputed_topology_digest_cannot_authenticate_an_oversized_label() -> None:
    published = publish_initial_workflow(1, case_id=127_264)
    page = WorkflowProgressTopologyPage.objects.get(
        run_storage__execution=published.execution,
        collection="NODE",
    )
    value = json.loads(bytes(page.payload))
    value["records"][0]["label"] = "x" * (storage.WORKFLOW_PROGRESS_LABEL_MAX_BYTES + 1)
    payload = _canonical_bytes(value)
    digest = storage._digest(storage._PAGE_DOMAIN, payload)
    descriptor = {
        "collection": "NODE",
        "decoded_bytes": len(payload),
        "digest": digest,
        "encoding": "identity",
        "encoded_bytes": len(payload),
        "item_count": 1,
        "page_index": 0,
    }
    row = {
        "collection": "NODE",
        "page_index": 0,
        "page__run_storage_id": page.run_storage_id,
        "page__collection": "NODE",
        "page__encoding": "identity",
        "page__digest": digest,
        "page__item_count": 1,
        "page__encoded_bytes": len(payload),
        "page__decoded_bytes": len(payload),
        "_payload_octets": len(payload),
        "_bounded_payload": payload,
    }

    with pytest.raises(storage.WorkflowProgressStorageIntegrityError):
        storage.verify_workflow_progress_topology_page_record(
            row,
            descriptor=descriptor,
            expected_run_storage_id=page.run_storage_id,
        )


@pytest.mark.parametrize("archived", [False, True], ids=["current", "archived"])
@pytest.mark.parametrize("surface", ["topology", "detail"])
def test_legacy_payload_digest_authenticates_original_bytes_before_presentation(
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
    surface: str,
) -> None:
    published = _legacy_ansi_workflow(monkeypatch, case_id=127_257)
    attempt_number = _archive_workflow_attempt(published, next_run=127_258) if archived else None
    if surface == "topology":
        row = WorkflowProgressTopologyPage.objects.get(
            run_storage__execution=published.execution,
            collection="NODE",
        )
        original = bytes(row.payload)
        tampered = original.replace(b"\\u001b[31m", b"\\u001b[32m", 1)
    else:
        row = WorkflowProgressNodeDetail.objects.get(
            run_storage__execution=published.execution,
            node_id=workflow_node_id(0),
        )
        original = bytes(row.payload)
        tampered = original.replace(b"\\u001b[33m", b"\\u001b[32m", 1)

    def operation() -> dict[str, Any]:
        if surface == "topology":
            return list_workflow_topology_nodes(
                published.execution,
                authorize=_allow,
                attempt_number=attempt_number,
            )
        return list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )

    assert tampered != original
    assert len(tampered) == len(original)
    type(row).objects.filter(pk=row.pk).update(payload=tampered)
    assert storage.redact_text("\x1b[31mPrepare\x1b[0m") == storage.redact_text(
        "\x1b[32mPrepare\x1b[0m"
    )

    assert _error_code(operation) is WorkflowProgressReadErrorCode.CORRUPT


def test_invalid_state_and_utf8_node_identifiers_are_bounded_arguments() -> None:
    published = publish_initial_workflow(1, case_id=127_236)
    assert (
        _error_code(
            lambda: list_workflow_node_details(
                published.execution,
                authorize=_allow,
                state="not-a-workflow-state",
            )
        )
        is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
    )
    for node_id in ("\ud800", "\U0001f600" * 256):
        assert (
            _error_code(
                lambda node_id=node_id: get_workflow_node_detail(
                    published.execution,
                    node_id,
                    authorize=_allow,
                )
            )
            is WorkflowProgressReadErrorCode.INVALID_ARGUMENT
        )


def test_single_node_read_rejects_duplicate_mismatched_and_oversized_results(
    monkeypatch,
) -> None:
    published = publish_initial_workflow(1, case_id=127_237)
    original_rows = reads._detail_payload_rows
    original_verify = reads.verify_workflow_progress_node_detail_record

    def duplicate_rows(query, keys):
        rows = original_rows(query, keys)
        return [*rows, *rows]

    monkeypatch.setattr(reads, "_detail_payload_rows", duplicate_rows)
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    monkeypatch.setattr(reads, "_detail_payload_rows", original_rows)

    def mismatched_node(*args, **kwargs):
        value = original_verify(*args, **kwargs)
        return {**value, "node_id": "different-node"}

    monkeypatch.setattr(reads, "verify_workflow_progress_node_detail_record", mismatched_node)
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )

    monkeypatch.setattr(
        reads,
        "verify_workflow_progress_node_detail_record",
        original_verify,
    )
    monkeypatch.setattr(reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", 1)
    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_single_node_read_validates_run_epoch_counters() -> None:
    published = publish_initial_workflow(1, case_id=127_238)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_node_count=2,
        detail_pending_count=2,
    )

    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_single_node_rejects_a_row_when_authenticated_epoch_count_is_zero() -> None:
    published = publish_initial_workflow(1, case_id=127_240)
    summary = _stored_summary(published.execution)
    summary["node_counts"]["retained_detail"] = 0
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["detail_count_limit"],
    }
    _store_summary(published.execution, summary)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_node_count=0,
        detail_pending_count=0,
        detail_event_count=0,
        detail_encoded_bytes=0,
        detail_decoded_bytes=0,
        detail_truncation_reasons="detail_count_limit",
    )

    assert (
        _error_code(
            lambda: get_workflow_node_detail(
                published.execution,
                workflow_node_id(0),
                authorize=_allow,
            )
        )
        is WorkflowProgressReadErrorCode.CORRUPT
    )


def test_lifecycle_success_keeps_preterminal_retained_detail_readable() -> None:
    published = publish_initial_workflow(1, case_id=127_241)

    assert succeed_task(
        published.execution,
        result_data='{"ok":true}',
        result_reference=None,
    )

    for attempt_number in (None, 1):
        summary = get_workflow_progress_summary(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )
        topology = list_workflow_topology_nodes(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )
        details = list_workflow_node_details(
            published.execution,
            authorize=_allow,
            attempt_number=attempt_number,
        )
        node = get_workflow_node_detail(
            published.execution,
            workflow_node_id(0),
            authorize=_allow,
            attempt_number=attempt_number,
        )

        for response in (summary, topology, details, node):
            assert response["availability"] == "TRUNCATED"
            assert response["complete"] is False
        assert summary["summary"]["state"] == "SUCCEEDED"
        assert summary["summary"]["detail"]["truncation_reasons"] == ["terminal_state_unreported"]
        assert topology["returned_count"] == 1
        assert details["items"][0]["state"] == "PENDING"
        assert node["found"] is True
        assert node["item"]["state"] == "PENDING"
