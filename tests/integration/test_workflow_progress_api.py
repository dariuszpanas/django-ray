"""HTTP-adapter coverage for bounded workflow-progress read services."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from django_ray.models import RayTaskExecution, TaskState
from django_ray.workflow.progress.reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
)
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from tests.workflow_progress_summary_helpers import (
    terminal_only_workflow_progress_summary,
)


@pytest.fixture
def client(settings) -> Client:
    token = settings.DJANGO_API_TOKEN
    assert isinstance(token, str) and token
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


def _publication() -> dict[str, int]:
    return {
        "summary_revision": 2,
        "topology_version": 3,
        "detail_revision": 4,
    }


def _common(execution: RayTaskExecution) -> dict[str, Any]:
    return {
        "schema": "django-ray.workflow-progress-page",
        "schema_version": 1,
        "generated_at": "2026-07-20T12:00:00Z",
        "task_id": execution.task_id,
        "run_identity": {
            "schema_version": 1,
            "run_id": str(execution.workflow_run_id),
            "attempt_number": execution.attempt_number,
            "execution_generation": execution.execution_generation,
        },
        "publication": _publication(),
        "availability": "AVAILABLE",
        "complete": True,
    }


@pytest.fixture
def workflow_execution(db) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id="bounded-workflow-api-001",
        callable_path="testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark",
        queue_name="default",
        state=TaskState.RUNNING,
        workflow_run_id="00000000-0000-0000-0000-000000000127",
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/api/cluster/workflows/task-id",
        "/api/cluster/workflows/task-id/topology/nodes",
        "/api/cluster/workflows/task-id/topology/edges",
        "/api/cluster/workflows/task-id/nodes",
        "/api/cluster/workflows/task-id/node-detail?node_id=node-id",
    ],
)
def test_bounded_workflow_routes_require_bearer_authentication(path: str) -> None:
    response = Client().get(path)

    assert response.status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/topology/nodes",
        "/topology/edges",
        "/nodes",
        "/node-detail?node_id=node-id",
    ],
)
def test_bounded_workflow_routes_apply_object_policy(client: Client, suffix: str) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="non-workflow-api-object",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
    )

    response = client.get(f"/api/cluster/workflows/{execution.task_id}{suffix}")

    assert response.status_code == 403
    assert response.json()["code"] == "ACCESS_DENIED"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/topology/nodes",
        "/topology/edges",
        "/nodes",
        "/node-detail?node_id=node-id",
    ],
)
def test_bounded_workflow_routes_map_unknown_tasks_to_declared_error(
    client: Client,
    suffix: str,
) -> None:
    response = client.get(f"/api/cluster/workflows/missing-workflow{suffix}")

    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "message": "Workflow progress subject was not found.",
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "suffix",
    [
        "?attempt_number=not-an-integer",
        "/topology/nodes?limit=not-an-integer",
        "/topology/edges?attempt_number=not-an-integer",
        "/nodes?limit=not-an-integer",
        "/node-detail",
        "/node-detail?node_id=node-id&attempt_number=not-an-integer",
    ],
)
def test_bounded_workflow_routes_normalize_query_errors_to_declared_error(
    client: Client,
    workflow_execution: RayTaskExecution,
    suffix: str,
) -> None:
    response = client.get(f"/api/cluster/workflows/{workflow_execution.task_id}{suffix}")

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_ARGUMENT",
        "message": "Workflow progress read arguments are invalid.",
    }


@pytest.mark.django_db
def test_legacy_live_node_route_is_unavailable(
    client: Client,
    workflow_execution: RayTaskExecution,
) -> None:
    response = client.get(
        f"/api/cluster/workflows/{workflow_execution.task_id}/nodes/node-a",
        {"include_logs": "true", "tail": "17"},
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_bounded_workflow_routes_delegate_to_package_services(
    client: Client,
    workflow_execution: RayTaskExecution,
    monkeypatch,
) -> None:
    execution = workflow_execution
    calls: list[tuple[str, dict[str, Any]]] = []

    def assert_authorized(
        candidate: RayTaskExecution,
        authorize: Callable[[RayTaskExecution], bool],
    ) -> None:
        assert candidate.pk == execution.pk
        assert authorize(candidate) is True

    def summary(candidate, *, authorize, **kwargs):
        assert_authorized(candidate, authorize)
        calls.append(("summary", kwargs))
        return {
            **_common(execution),
            "schema": "django-ray.workflow-progress-summary",
            "source_schema_version": 3,
            "summary": {
                "schema_version": 3,
                "state": "RUNNING",
                "node_counts": {"discovered": 1},
            },
        }

    def page(collection):
        def read(candidate, *, authorize, **kwargs):
            assert_authorized(candidate, authorize)
            calls.append((collection, kwargs))
            return {
                **_common(execution),
                "collection": collection,
                "returned_count": 1,
                "items": [{"node_id": "node-a"}],
                "next_cursor": None,
            }

        return read

    def node(candidate, node_id, *, authorize, **kwargs):
        assert_authorized(candidate, authorize)
        calls.append(("node", {"node_id": node_id, **kwargs}))
        return {
            **_common(execution),
            "schema": "django-ray.workflow-progress-node",
            "found": True,
            "item": {"node_id": node_id, "state": "RUNNING"},
        }

    monkeypatch.setattr("testproject.api.get_workflow_progress_summary", summary)
    monkeypatch.setattr(
        "testproject.api.list_workflow_topology_nodes",
        page("topology_nodes"),
    )
    monkeypatch.setattr(
        "testproject.api.list_workflow_topology_edges",
        page("topology_edges"),
    )
    monkeypatch.setattr(
        "testproject.api.list_workflow_node_details",
        page("node_details"),
    )
    monkeypatch.setattr("testproject.api.get_workflow_node_detail", node)

    indexed_node_id = "namespace/naïve pod?kind=Deployment#blue"
    with CaptureQueriesContext(connection) as queries:
        summary_response = client.get(
            f"/api/cluster/workflows/{execution.task_id}?attempt_number=1"
        )
        nodes_response = client.get(
            f"/api/cluster/workflows/{execution.task_id}/topology/nodes?attempt_number=1&limit=12"
        )
        edges_response = client.get(
            f"/api/cluster/workflows/{execution.task_id}/topology/edges?attempt_number=1&limit=13"
        )
        details_response = client.get(
            f"/api/cluster/workflows/{execution.task_id}/nodes"
            "?attempt_number=1&state=RUNNING&limit=14"
        )
        node_response = client.get(
            f"/api/cluster/workflows/{execution.task_id}/node-detail",
            {"node_id": indexed_node_id, "attempt_number": "1"},
        )

    assert summary_response.status_code == 200
    assert summary_response.json()["source_schema_version"] == 3
    assert nodes_response.json()["collection"] == "topology_nodes"
    assert edges_response.json()["collection"] == "topology_edges"
    assert details_response.json()["collection"] == "node_details"
    assert node_response.json()["item"]["node_id"] == indexed_node_id
    assert calls == [
        ("summary", {"include_legacy": True, "attempt_number": 1}),
        ("topology_nodes", {"attempt_number": 1, "cursor": None, "limit": 12}),
        ("topology_edges", {"attempt_number": 1, "cursor": None, "limit": 13}),
        (
            "node_details",
            {"attempt_number": 1, "state": "RUNNING", "cursor": None, "limit": 14},
        ),
        ("node", {"node_id": indexed_node_id, "attempt_number": 1}),
    ]
    task_selects = [
        query["sql"].lower()
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
        and "django_ray_raytaskexecution" in query["sql"]
    ]
    assert len(task_selects) == 5
    payload_fields = (
        "runtime_env_json",
        "args_json",
        "kwargs_json",
        "result_data",
        "progress_data",
        "workflow_progress_summary_json",
        "workflow_plan_json",
        "workflow_plan_selection",
        "completion_data",
        "cancellation_error",
        "error_message",
        "error_traceback",
    )
    assert all(field not in query for query in task_selects for field in payload_fields)


@pytest.mark.django_db
@pytest.mark.parametrize("workflow_state", [TaskState.SUCCEEDED, TaskState.FAILED])
def test_terminal_only_api_exposes_summary_without_detail_or_graph_links(
    client: Client,
    workflow_state: str,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id=f"terminal-only-api-{workflow_state.lower()}",
        callable_path="testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark",
        queue_name="default",
        state=workflow_state,
        workflow_run_id=(
            "00000000-0000-0000-0000-000000000227"
            if workflow_state == TaskState.SUCCEEDED
            else "00000000-0000-0000-0000-000000000228"
        ),
    )
    summary = terminal_only_workflow_progress_summary(
        execution,
        state=workflow_state,
        declared_node_count=12,
        declared_edge_count=11,
    )
    summary["selected_strategy"] = "dynamic_tasks"
    summary["plan_fingerprint"] = "sha256:" + ("a" * 64)
    execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
    execution.save(update_fields=["workflow_progress_summary_json"])

    summary_response = client.get(f"/api/cluster/workflows/{execution.task_id}")
    summary_payload = summary_response.json()

    assert summary_response.status_code == 200
    assert summary_payload["availability"] == "OMITTED_BY_POLICY"
    assert summary_payload["complete"] is False
    assert summary_payload["publication"] == {
        "summary_revision": 1,
        "topology_version": None,
        "detail_revision": None,
    }
    assert summary_payload["summary"]["reporting_policy"] == "terminal_only"
    assert summary_payload["summary"]["state"] == workflow_state
    assert summary_payload["summary"]["terminal"]["outcome"] == workflow_state
    assert summary_payload["summary"]["node_counts"] == {
        "declared": 12,
        "discovered": 0,
        "retained_topology": 0,
        "retained_detail": 0,
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert summary_payload["summary"]["edge_counts"] == {
        "declared": 11,
        "discovered": 0,
        "retained_topology": 0,
    }
    assert summary_payload["summary"]["detail"] == {
        "availability": "OMITTED_BY_POLICY",
        "complete": False,
        "truncation_reasons": [],
    }
    assert summary_payload["summary"]["storage"] == {
        "kind": "database",
        "manifest_id": None,
    }
    serialized_payload = json.dumps(summary_payload)
    assert "/topology/" not in serialized_payload
    assert "/nodes" not in serialized_payload
    assert "/graph" not in serialized_payload

    detail_responses = [
        client.get(f"/api/cluster/workflows/{execution.task_id}/topology/nodes"),
        client.get(f"/api/cluster/workflows/{execution.task_id}/topology/edges"),
        client.get(f"/api/cluster/workflows/{execution.task_id}/nodes"),
        client.get(
            f"/api/cluster/workflows/{execution.task_id}/node-detail",
            {"node_id": "not-collected"},
        ),
    ]
    for response in detail_responses:
        assert response.status_code == 200
        payload = response.json()
        assert payload["availability"] == "OMITTED_BY_POLICY"
        if "items" in payload:
            assert payload["items"] == []
            assert payload["returned_count"] == 0
        else:
            assert payload["found"] is False
            assert payload["item"] is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (WorkflowProgressReadErrorCode.INVALID_ARGUMENT, 400),
        (WorkflowProgressReadErrorCode.INVALID_CURSOR, 400),
        (WorkflowProgressReadErrorCode.ACCESS_DENIED, 403),
        (WorkflowProgressReadErrorCode.NOT_FOUND, 404),
        (WorkflowProgressReadErrorCode.CURSOR_MISMATCH, 409),
        (WorkflowProgressReadErrorCode.MISSING, 409),
        (WorkflowProgressReadErrorCode.CORRUPT, 503),
    ],
)
def test_workflow_read_errors_keep_distinct_bounded_codes(
    client: Client,
    workflow_execution: RayTaskExecution,
    monkeypatch,
    code: WorkflowProgressReadErrorCode,
    expected_status: int,
) -> None:
    def fail(*args, **kwargs):
        del args, kwargs
        raise WorkflowProgressReadError(code)

    monkeypatch.setattr("testproject.api.get_workflow_progress_summary", fail)

    response = client.get(f"/api/cluster/workflows/{workflow_execution.task_id}")

    assert response.status_code == expected_status
    assert response.json() == {
        "code": code.value,
        "message": str(WorkflowProgressReadError(code)),
    }


@pytest.mark.django_db
def test_removed_legacy_graph_contract_is_absent_from_http_and_openapi(
    client: Client,
    workflow_execution: RayTaskExecution,
) -> None:
    response = client.get(f"/api/cluster/workflows/{workflow_execution.task_id}/graph")
    schema_response = client.get("/api/openapi.json")

    assert response.status_code == 404
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert "/api/cluster/workflows/{task_id}/graph" not in schema["paths"]
    assert "WorkflowGraphSchema" not in schema["components"]["schemas"]


@pytest.mark.django_db
def test_legacy_row_remains_unchanged_and_aggregate_readable(
    client: Client,
    workflow_execution: RayTaskExecution,
) -> None:
    legacy_payload = (
        '{"schema_version":1,"revision":1,"state":"RUNNING",'
        '"total_nodes":2,"completed_nodes":1,"failed_nodes":0,'
        '"running_nodes":1,"pending_nodes":0,"progress_percent":50.0,'
        '"updated_at":123.5,"graph":{"nodes":[{"secret":"not-returned"}],'
        '"edges":[{"source":"0.0","target":"0.1"}]},'
        '"recent_events":[]}'
    )
    workflow_execution.progress_data = legacy_payload
    workflow_execution.save(update_fields=["progress_data"])

    response = client.get(f"/api/cluster/workflows/{workflow_execution.task_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_schema_version"] == 1
    assert payload["availability"] == "NOT_REPORTED"
    assert payload["complete"] is False
    assert payload["summary"]["node_counts"] == {
        "declared": 2,
        "discovered": 2,
        "retained_topology": 0,
        "retained_detail": 0,
        "pending": 0,
        "running": 1,
        "succeeded": 1,
        "failed": 0,
    }
    assert payload["summary"]["edge_counts"] == {
        "declared": 1,
        "discovered": 1,
        "retained_topology": 0,
    }
    assert "graph" not in payload["summary"]
    assert "secret" not in json.dumps(payload)

    workflow_execution.refresh_from_db()
    assert workflow_execution.progress_data == legacy_payload
