"""Integration coverage for the bounded Django-admin workflow graph facade."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django_ray.admin import RayTaskExecutionAdmin
from django_ray.admin_workflow_graph import (
    ADMIN_WORKFLOW_GRAPH_MAX_DETAILS,
    ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
    ADMIN_WORKFLOW_GRAPH_MAX_NODES,
    ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    WorkflowProgressNodeDetail,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_progress_reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
)
from django_ray.workflow_progress_storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    prepare_workflow_progress_topology,
    stage_workflow_progress_topology,
)
from tests.workflow_progress_storage_helpers import (
    workflow_detail,
    workflow_node,
    workflow_summary,
)

_RUN_ID = "00000000-0000-0000-0000-000000000219"
_IDENTITY = {
    "schema_version": 1,
    "run_id": _RUN_ID,
    "attempt_number": 1,
    "execution_generation": 1,
}
_PUBLICATION = {
    "summary_revision": 4,
    "topology_version": 2,
    "detail_revision": 3,
}
_ROOT_FIELDS = {
    "schema",
    "schema_version",
    "status",
    "message",
    "complete",
    "counts",
    "limits",
    "nodes",
    "edges",
}
_NODE_FIELDS = {
    "id",
    "label",
    "kind",
    "state",
    "message",
    "error",
    "failure_path",
    "output_preview",
}


def _task_admin() -> RayTaskExecutionAdmin:
    return RayTaskExecutionAdmin(RayTaskExecution, admin.site)


def _summary_envelope(
    states: list[str],
    edges: list[tuple[str, str]],
    *,
    task_id: str = "graph-task-internal-sentinel",
    workflow_state: str | None = None,
) -> dict[str, Any]:
    state_counts = {
        state: sum(item == state for item in states)
        for state in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
    }
    selected_state = workflow_state or ("FAILED" if state_counts["FAILED"] else "SUCCEEDED")
    finished_at = "2026-07-29T12:00:04Z"
    node_count = len(states)
    edge_count = len(edges)
    summary = {
        "schema_version": 3,
        "run_identity": copy.deepcopy(_IDENTITY),
        "summary_revision": _PUBLICATION["summary_revision"],
        "topology_version": _PUBLICATION["topology_version"],
        "detail_revision": _PUBLICATION["detail_revision"],
        "state": selected_state,
        "node_counts": {
            "declared": node_count,
            "discovered": node_count,
            "retained_topology": node_count,
            "retained_detail": node_count,
            "pending": state_counts["PENDING"],
            "running": state_counts["RUNNING"],
            "succeeded": state_counts["SUCCEEDED"],
            "failed": state_counts["FAILED"],
        },
        "edge_counts": {
            "declared": edge_count,
            "discovered": edge_count,
            "retained_topology": edge_count,
        },
        "timestamps": {
            "started_at": "2026-07-29T12:00:00Z",
            "updated_at": finished_at,
            "finished_at": finished_at,
        },
        "detail": {
            "availability": "AVAILABLE",
            "complete": True,
            "truncation_reasons": [],
        },
        "terminal": {"outcome": selected_state, "finished_at": finished_at},
    }
    return {
        "schema": "django-ray.workflow-progress-summary",
        "schema_version": 1,
        "generated_at": "2026-07-29T12:00:05Z",
        "task_id": task_id,
        "run_identity": copy.deepcopy(_IDENTITY),
        "publication": copy.deepcopy(_PUBLICATION),
        "availability": "AVAILABLE",
        "complete": True,
        "source_schema_version": 3,
        "summary": summary,
    }


def _topology_node(
    node_id: str,
    *,
    kind: str = "task",
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": kind,
        "label": label if label is not None else f"Node {node_id}",
        "callable_path": "raw-callable-path-must-not-leak",
        "runtime_env": {"raw-runtime-env-must-not-leak": "sentinel"},
        "ray_options": {"raw-ray-options-must-not-leak": "sentinel"},
    }


def _node_detail(
    node_id: str,
    state: str,
    *,
    message: str | None = None,
    error: str | None = None,
    fanout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = None
    if message is not None:
        progress = {
            "current": 1.0,
            "total": 2.0,
            "percent": 50.0,
            "message": message,
            "metrics": {"raw-metric-must-not-leak": "sentinel"},
            "updated_at": "2026-07-29T12:00:02Z",
        }
    return {
        "schema_version": 1,
        "node_id": node_id,
        "invocation_identity": {"raw-invocation-must-not-leak": "sentinel"},
        "state": state,
        "progress": progress,
        "execution": {"raw-execution-must-not-leak": "sentinel"},
        "fanout": fanout,
        "started_at": "2026-07-29T12:00:00Z",
        "finished_at": ("2026-07-29T12:00:03Z" if state in {"SUCCEEDED", "FAILED"} else None),
        "error": error,
        "recent_events": [{"raw-event-must-not-leak": "sentinel"}],
        "truncated": False,
    }


def _page(
    collection: str,
    items: list[dict[str, Any]],
    *,
    task_id: str = "graph-task-internal-sentinel",
) -> dict[str, Any]:
    return {
        "schema": "django-ray.workflow-progress-page",
        "schema_version": 1,
        "generated_at": "2026-07-29T12:00:06Z",
        "task_id": task_id,
        "run_identity": copy.deepcopy(_IDENTITY),
        "publication": copy.deepcopy(_PUBLICATION),
        "availability": "AVAILABLE",
        "complete": True,
        "collection": collection,
        "returned_count": len(items),
        "items": items,
        "next_cursor": None,
    }


def _graph_case(
    *,
    states: list[str] | None = None,
    edges: list[tuple[str, str]] | None = None,
    kinds: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    selected_states = states or ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    node_ids = [
        chr(ord("a") + index) if index < 26 else f"node-{index:03d}"
        for index in range(len(selected_states))
    ]
    selected_edges = edges if edges is not None else list(zip(node_ids, node_ids[1:], strict=False))
    selected_kinds = kinds or {}
    topology = [
        _topology_node(node_id, kind=selected_kinds.get(node_id, "task"))
        for node_id in reversed(node_ids)
    ]
    details = [
        _node_detail(
            node_id,
            state,
            error=f"bounded failure for {node_id}" if state == "FAILED" else None,
            fanout=(
                {
                    "max_concurrency": 4,
                    "max_items": 8,
                    "submitted_items": 3,
                    "completed_items": 2,
                    "in_flight_items": 1,
                    "input_exhausted": False,
                }
                if selected_kinds.get(node_id) == "map"
                else None
            ),
        )
        for node_id, state in reversed(list(zip(node_ids, selected_states, strict=True)))
    ]
    summary = _summary_envelope(selected_states, selected_edges)
    pages = {
        "topology_nodes": _page("topology_nodes", topology),
        "topology_edges": _page(
            "topology_edges",
            [{"source": source, "target": target} for source, target in selected_edges],
        ),
        "node_details": _page("node_details", details),
    }
    return summary, pages


def _install_graph_readers(
    monkeypatch: pytest.MonkeyPatch,
    summary: dict[str, Any],
    pages: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_summary(candidate, *, authorize, **kwargs):
        assert authorize(candidate) is True
        calls.append(("summary", kwargs))
        return copy.deepcopy(summary)

    def page_reader(collection: str):
        def read(candidate, *, authorize, **kwargs):
            assert authorize(candidate) is True
            calls.append((collection, kwargs))
            return copy.deepcopy(pages[collection])

        return read

    monkeypatch.setattr("django_ray.admin.get_workflow_progress_summary", fake_summary)
    monkeypatch.setattr(
        "django_ray.admin.list_workflow_topology_nodes",
        page_reader("topology_nodes"),
    )
    monkeypatch.setattr(
        "django_ray.admin.list_workflow_topology_edges",
        page_reader("topology_edges"),
    )
    monkeypatch.setattr(
        "django_ray.admin.list_workflow_node_details",
        page_reader("node_details"),
    )
    return calls


def _execution(**overrides: Any) -> RayTaskExecution:
    values = {
        "task_id": "graph-task-internal-sentinel",
        "callable_path": "raw-model-callable-must-not-leak",
        "state": TaskState.FAILED,
        "attempt_number": 1,
        "execution_generation": 1,
        "workflow_run_id": _RUN_ID,
        "args_json": json.dumps(["raw-args-must-not-leak"]),
        "kwargs_json": json.dumps({"raw-kwargs-must-not-leak": True}),
        "result_data": json.dumps({"raw-result-must-not-leak": True}),
        "runtime_env_json": json.dumps({"raw-model-runtime-env-must-not-leak": True}),
        "progress_data": json.dumps({"raw-legacy-progress-must-not-leak": True}),
        "workflow_plan_json": json.dumps({"raw-plan-must-not-leak": True}),
        "workflow_plan_selection": json.dumps({"raw-selection-must-not-leak": True}),
    }
    values.update(overrides)
    return RayTaskExecution.objects.create(**values)


def _json(response) -> dict[str, Any]:
    return json.loads(response.content)


def _assert_empty_graph(payload: dict[str, Any], status: str) -> None:
    assert set(payload) == _ROOT_FIELDS
    assert payload["status"] == status
    assert payload["complete"] is False
    assert payload["counts"] == {"nodes": 0, "edges": 0}
    assert payload["nodes"] == []
    assert payload["edges"] == []


@pytest.mark.django_db
def test_graph_endpoint_projects_one_coherent_first_page_without_raw_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edges = [("a", "b"), ("b", "c"), ("a", "d")]
    summary, pages = _graph_case(
        states=["SUCCEEDED", "FAILED", "FAILED", "SUCCEEDED"],
        edges=edges,
        kinds={"b": "map"},
    )
    pages["topology_nodes"]["items"][3]["label"] = "password=label-secret"
    detail_by_id = {item["node_id"]: item for item in pages["node_details"]["items"]}
    detail_by_id["a"]["progress"] = {
        "current": 1.0,
        "total": 2.0,
        "percent": 50.0,
        "message": "password=message-secret",
        "metrics": {"raw-metric-must-not-leak": "sentinel"},
        "updated_at": "2026-07-29T12:00:02Z",
    }
    detail_by_id["a"]["schema_version"] = 2
    detail_by_id["a"]["output_preview"] = {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"item_count": 3, "status": "ready"},
    }
    detail_by_id["b"]["error"] = "password=origin-secret"
    detail_by_id["c"]["error"] = "\x1b[31mbounded failure for c\x1b[39m\rnext line"
    calls = _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution()
    user = get_user_model().objects.create_superuser(username="workflow-graph-admin")
    request = RequestFactory().get(
        "/admin/workflow/graph/?attempt_number=1&ignored=raw-query-sentinel"
    )
    request.user = user

    with CaptureQueriesContext(connection) as queries:
        response = _task_admin().workflow_graph_view(request, str(execution.pk))

    payload = _json(response)
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert set(payload) == _ROOT_FIELDS
    assert payload["schema"] == "django-ray.admin-workflow-graph"
    assert payload["schema_version"] == 2
    assert payload["status"] == "AVAILABLE"
    assert payload["complete"] is True
    assert payload["counts"] == {"nodes": 4, "edges": 3}
    assert payload["limits"] == {
        "nodes": 100,
        "edges": 256,
        "details": 100,
        "response_bytes": 131072,
    }
    assert len(response.content) <= ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES
    assert [node["id"] for node in payload["nodes"]] == ["a", "b", "c", "d"]
    by_id = {node["id"]: node for node in payload["nodes"]}
    assert set(by_id["a"]) == _NODE_FIELDS
    assert by_id["a"]["label"] == "[REDACTED]"
    assert by_id["a"]["message"] == "[REDACTED]"
    assert by_id["a"]["output_preview"] == {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"item_count": 3, "status": "ready"},
    }
    assert by_id["b"]["error"] == "[REDACTED]"
    assert by_id["b"]["output_preview"]["availability"] == "UNAVAILABLE"
    assert by_id["c"]["error"] == "bounded failure for c\nnext line"
    assert "\x1b" not in json.dumps(payload)
    assert by_id["b"]["fanout"] == {
        "submitted_items": 3,
        "completed_items": 2,
        "in_flight_items": 1,
        "input_exhausted": False,
    }
    assert by_id["a"]["failure_path"] is True
    assert by_id["b"]["failure_path"] is True
    assert by_id["c"]["failure_path"] is False
    assert by_id["d"]["failure_path"] is False
    assert calls == [
        (
            "summary",
            {
                "include_legacy": False,
                "infer_current_reporting_policy": False,
                "attempt_number": 1,
            },
        ),
        (
            "topology_nodes",
            {
                "attempt_number": 1,
                "limit": ADMIN_WORKFLOW_GRAPH_MAX_NODES,
            },
        ),
        (
            "topology_edges",
            {
                "attempt_number": 1,
                "limit": ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
            },
        ),
        (
            "node_details",
            {
                "attempt_number": 1,
                "limit": ADMIN_WORKFLOW_GRAPH_MAX_DETAILS,
            },
        ),
    ]

    content = response.content.decode("utf-8")
    forbidden_markers = (
        "label-secret",
        "message-secret",
        "origin-secret",
        "raw-callable-path-must-not-leak",
        "raw-runtime-env-must-not-leak",
        "raw-ray-options-must-not-leak",
        "raw-invocation-must-not-leak",
        "raw-execution-must-not-leak",
        "raw-metric-must-not-leak",
        "raw-event-must-not-leak",
        "raw-args-must-not-leak",
        "raw-kwargs-must-not-leak",
        "raw-result-must-not-leak",
        "raw-model-runtime-env-must-not-leak",
        "raw-legacy-progress-must-not-leak",
        "raw-plan-must-not-leak",
        "raw-selection-must-not-leak",
        "raw-query-sentinel",
        "graph-task-internal-sentinel",
    )
    assert all(marker not in content for marker in forbidden_markers)
    task_selects = [
        query["sql"].lower()
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
        and "django_ray_raytaskexecution" in query["sql"]
    ]
    assert task_selects
    forbidden_columns = (
        "args_json",
        "kwargs_json",
        "result_data",
        "runtime_env_json",
        "progress_data",
        "workflow_progress_summary_json",
        "workflow_plan_json",
        "workflow_plan_selection",
    )
    assert all(column not in query for query in task_selects for column in forbidden_columns)


@pytest.mark.django_db
def test_graph_endpoint_is_private_get_only_and_authorizes_before_query_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution(task_id="graph-authorization")
    admin_obj = _task_admin()
    called = False

    def unexpected_summary(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called = True
        raise AssertionError("denied graph requests must not reach summary storage")

    monkeypatch.setattr(
        "django_ray.admin.get_workflow_progress_summary",
        unexpected_summary,
    )
    denied_user = get_user_model().objects.create_user(
        username="workflow-graph-denied",
        is_staff=True,
    )
    denied = RequestFactory().get("/admin/workflow/graph/?attempt_number=invalid&cursor=raw-secret")
    denied.user = denied_user
    with pytest.raises(PermissionDenied):
        admin_obj.workflow_graph_view(denied, str(execution.pk))
    assert called is False

    superuser = get_user_model().objects.create_superuser(username="workflow-graph-method-admin")
    post = RequestFactory().post("/admin/workflow/graph/")
    post.user = superuser
    response = admin_obj.workflow_graph_view(post, str(execution.pk))
    assert response.status_code == 405
    assert response["Allow"] == "GET"
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert called is False

    invalid_attempt = RequestFactory().get("/admin/workflow/graph/?attempt_number=not-an-integer")
    invalid_attempt.user = superuser
    invalid_response = admin_obj.workflow_graph_view(
        invalid_attempt,
        str(execution.pk),
    )
    assert invalid_response.status_code == 503
    _assert_empty_graph(_json(invalid_response), "CORRUPT")
    assert called is False

    url = reverse(
        "admin:django_ray_raytaskexecution_workflow_graph",
        args=[execution.pk],
    )
    anonymous_request = RequestFactory().get(url)
    anonymous_request.user = AnonymousUser()
    anonymous = admin_obj.admin_site.admin_view(admin_obj.workflow_graph_view)(
        anonymous_request,
        str(execution.pk),
    )
    assert anonymous.status_code == 302
    assert "/admin/login/" in anonymous["Location"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("missing-v3", "NOT_REPORTED"),
        ("legacy", "UNSUPPORTED"),
        ("running", "NOT_REPORTED"),
        ("truncated", "TRUNCATED"),
        ("expired", "UNAVAILABLE"),
    ],
)
def test_graph_summary_degrades_without_reading_any_collection(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_status: str,
) -> None:
    summary, pages = _graph_case()
    if mode == "missing-v3":
        summary["source_schema_version"] = None
        summary["summary"] = None
    elif mode == "legacy":
        summary["source_schema_version"] = 2
    elif mode == "running":
        summary["summary"]["state"] = "RUNNING"
    elif mode == "truncated":
        summary["availability"] = "TRUNCATED"
        summary["complete"] = False
    else:
        summary["availability"] = "EXPIRED"
        summary["complete"] = False
    calls = _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution(task_id=f"graph-summary-{mode}")
    user = get_user_model().objects.create_superuser(username=f"graph-summary-{mode}")
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == 200
    _assert_empty_graph(_json(response), expected_status)
    assert [name for name, _kwargs in calls] == ["summary"]


@pytest.mark.django_db
@pytest.mark.parametrize(("node_count", "edge_count"), [(101, 0), (1, 257)])
def test_graph_summary_limits_fail_before_collection_reads(
    monkeypatch: pytest.MonkeyPatch,
    node_count: int,
    edge_count: int,
) -> None:
    states = ["SUCCEEDED"] * node_count
    edges = [("a", "a")] * edge_count
    summary = _summary_envelope(states, edges)
    pages = {
        "topology_nodes": _page("topology_nodes", []),
        "topology_edges": _page("topology_edges", []),
        "node_details": _page("node_details", []),
    }
    calls = _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution(task_id=f"graph-limit-{node_count}-{edge_count}")
    user = get_user_model().objects.create_superuser(
        username=f"graph-limit-{node_count}-{edge_count}"
    )
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == 200
    _assert_empty_graph(_json(response), "LIMIT_EXCEEDED")
    assert [name for name, _kwargs in calls] == ["summary"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    [
        "missing-node-declared",
        "extra-edge-count",
    ],
)
def test_graph_summary_requires_exact_canonical_count_fields(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    summary, pages = _graph_case()
    if case == "missing-node-declared":
        summary["summary"]["node_counts"].pop("declared")
    else:
        summary["summary"]["edge_counts"]["raw_count_must_not_be_accepted"] = 3
    calls = _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution(task_id=f"graph-count-fields-{case}")
    user = get_user_model().objects.create_superuser(username=f"graph-count-fields-{case}")
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == 503
    _assert_empty_graph(_json(response), "CORRUPT")
    assert [name for name, _kwargs in calls] == ["summary"]


@pytest.mark.django_db
def test_graph_summary_accepts_present_nullable_declared_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary, pages = _graph_case()
    summary["summary"]["node_counts"]["declared"] = None
    summary["summary"]["edge_counts"]["declared"] = None
    _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution(task_id="graph-nullable-declared-counts")
    user = get_user_model().objects.create_superuser(username="graph-nullable-declared-counts")
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == 200
    assert _json(response)["status"] == "AVAILABLE"


@pytest.mark.django_db
def test_graph_summary_rejects_empty_available_publication_before_collection_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "graph-empty-available"
    summary = _summary_envelope([], [], task_id=task_id)
    pages = {
        collection: _page(collection, [], task_id=task_id)
        for collection in ("topology_nodes", "topology_edges", "node_details")
    }
    calls = _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution(task_id=task_id)
    user = get_user_model().objects.create_superuser(username=task_id)
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == 503
    _assert_empty_graph(_json(response), "CORRUPT")
    assert [name for name, _kwargs in calls] == ["summary"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("error_code", "expected_status", "expected_http"),
    [
        (WorkflowProgressReadErrorCode.MISSING, "UNAVAILABLE", 200),
        (WorkflowProgressReadErrorCode.CORRUPT, "CORRUPT", 503),
    ],
)
def test_graph_read_failures_use_fixed_safe_degradations(
    monkeypatch: pytest.MonkeyPatch,
    error_code: WorkflowProgressReadErrorCode,
    expected_status: str,
    expected_http: int,
) -> None:
    execution = _execution(task_id=f"graph-read-error-{error_code.value.lower()}")
    user = get_user_model().objects.create_superuser(
        username=f"graph-read-error-{error_code.value.lower()}"
    )

    def failed_summary(candidate, *, authorize, **kwargs):
        del kwargs
        assert authorize(candidate) is True
        raise WorkflowProgressReadError(error_code)

    monkeypatch.setattr(
        "django_ray.admin.get_workflow_progress_summary",
        failed_summary,
    )
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == expected_http
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    _assert_empty_graph(_json(response), expected_status)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("case", "expected_status", "expected_http"),
    [
        ("publication-mismatch", "CORRUPT", 503),
        ("count-mismatch", "CORRUPT", 503),
        ("duplicate-node", "CORRUPT", 503),
        ("unknown-edge", "CORRUPT", 503),
        ("self-edge", "CORRUPT", 503),
        ("duplicate-edge", "CORRUPT", 503),
        ("cycle", "CORRUPT", 503),
        ("unsupported-kind", "CORRUPT", 503),
        ("unsupported-state", "CORRUPT", 503),
        ("malformed-node", "CORRUPT", 503),
        ("malformed-preview", "CORRUPT", 503),
        ("unsafe-node-identity", "CORRUPT", 503),
        ("truncated-record", "TRUNCATED", 200),
        ("next-cursor", "TRUNCATED", 200),
    ],
)
def test_graph_validation_never_returns_partial_data(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_status: str,
    expected_http: int,
) -> None:
    summary, pages = _graph_case()
    if case == "publication-mismatch":
        pages["topology_nodes"]["publication"]["detail_revision"] += 1
    elif case == "count-mismatch":
        pages["node_details"]["returned_count"] -= 1
    elif case == "duplicate-node":
        pages["topology_nodes"]["items"][1]["node_id"] = "a"
    elif case == "unknown-edge":
        pages["topology_edges"]["items"][1]["target"] = "unknown"
    elif case == "self-edge":
        pages["topology_edges"]["items"][0] = {"source": "a", "target": "a"}
    elif case == "duplicate-edge":
        pages["topology_edges"]["items"][1] = copy.deepcopy(pages["topology_edges"]["items"][0])
    elif case == "cycle":
        pages["topology_edges"]["items"].append({"source": "c", "target": "a"})
        pages["topology_edges"]["returned_count"] += 1
        for counts in (summary["summary"]["edge_counts"],):
            counts["declared"] += 1
            counts["discovered"] += 1
            counts["retained_topology"] += 1
    elif case == "unsupported-kind":
        pages["topology_nodes"]["items"][0]["kind"] = "group"
    elif case == "unsupported-state":
        pages["node_details"]["items"][0]["state"] = "BLOCKED"
    elif case == "malformed-node":
        pages["topology_nodes"]["items"][0].pop("label")
    elif case == "malformed-preview":
        pages["node_details"]["items"][0].update(
            {
                "schema_version": 2,
                "output_preview": {
                    "schema_version": 1,
                    "availability": "AVAILABLE",
                    "value": {"api_key": "must not render"},
                },
            }
        )
    elif case == "unsafe-node-identity":
        unsafe_id = "a\x1b[31m"
        topology_node = next(
            item for item in pages["topology_nodes"]["items"] if item["node_id"] == "a"
        )
        detail_node = next(
            item for item in pages["node_details"]["items"] if item["node_id"] == "a"
        )
        topology_node["node_id"] = unsafe_id
        detail_node["node_id"] = unsafe_id
        for edge in pages["topology_edges"]["items"]:
            if edge["source"] == "a":
                edge["source"] = unsafe_id
            if edge["target"] == "a":
                edge["target"] = unsafe_id
    elif case == "truncated-record":
        pages["node_details"]["items"][0]["truncated"] = True
    else:
        pages["topology_nodes"]["next_cursor"] = "must-not-follow"
    _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution(task_id=f"graph-invalid-{case}")
    user = get_user_model().objects.create_superuser(username=f"graph-invalid-{case}")
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == expected_http
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    _assert_empty_graph(_json(response), expected_status)


@pytest.mark.django_db
def test_graph_response_byte_ceiling_degrades_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = ["FAILED"] * ADMIN_WORKFLOW_GRAPH_MAX_NODES
    summary, pages = _graph_case(states=states, edges=[])
    for item in pages["topology_nodes"]["items"]:
        item["label"] = "l" * 512
    for item in pages["node_details"]["items"]:
        item["error"] = "e" * 2048
    _install_graph_readers(monkeypatch, summary, pages)
    execution = _execution(task_id="graph-response-byte-limit")
    user = get_user_model().objects.create_superuser(username="graph-response-byte-limit")
    request = RequestFactory().get("/admin/workflow/graph/")
    request.user = user

    response = _task_admin().workflow_graph_view(request, str(execution.pk))

    assert response.status_code == 200
    assert len(response.content) <= ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES
    _assert_empty_graph(_json(response), "LIMIT_EXCEEDED")


@pytest.mark.django_db
def test_graph_endpoint_reads_real_terminal_schema_v3_storage(settings) -> None:
    execution = _execution(
        task_id="graph-real-schema-v3",
        state=TaskState.RUNNING,
        result_data=json.dumps({"real-result-sentinel": True}),
        runtime_env_json=json.dumps({"real-runtime-sentinel": True}),
        workflow_plan_json=None,
        workflow_plan_selection=None,
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=1,
        execution_generation=1,
        run_id=_RUN_ID,
    )
    node_ids = ("real-a", "real-b")
    nodes = []
    for node_id in node_ids:
        node = workflow_node(node_id)
        node["callable_path"] = "real-callable-sentinel"
        node["runtime_env"] = {"plain_runtime_marker": "real-runtime-node-sentinel"}
        node["ray_options"] = {"plain_ray_marker": "real-ray-option-sentinel"}
        nodes.append(node)
    topology = prepare_workflow_progress_topology(
        identity,
        1,
        nodes,
        ({"source": node_ids[0], "target": node_ids[1]},),
    )
    details = []
    for node_id in node_ids:
        detail = workflow_detail(node_id)
        detail.update(
            state="SUCCEEDED",
            started_at="2026-07-29T12:00:00Z",
            finished_at="2026-07-29T12:00:01Z",
        )
        if node_id == "real-a":
            detail.update(
                schema_version=2,
                output_preview={
                    "schema_version": 1,
                    "availability": "AVAILABLE",
                    "value": {"item_count": 2, "status": "persisted"},
                },
            )
        details.append(detail)
    prepared_detail = prepare_workflow_progress_detail(details, topology=topology)
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    summary = workflow_summary(
        identity,
        summary_revision=4,
        node_count=len(node_ids),
        running_count=0,
    )
    summary["state"] = "SUCCEEDED"
    summary["node_counts"].update(pending=0, succeeded=len(node_ids))
    summary["edge_counts"].update(declared=1, discovered=1)
    summary["progress_percent"] = 100.0
    summary["timestamps"].update(
        updated_at="2026-07-29T12:00:02Z",
        finished_at="2026-07-29T12:00:02Z",
    )
    summary["terminal"].update(
        outcome="SUCCEEDED",
        finished_at="2026-07-29T12:00:02Z",
    )
    publication = persist_workflow_progress_publication(
        identity,
        summary,
        manifest_id=manifest_id,
        prepared_topology=topology,
        prepared_detail=prepared_detail,
    )
    assert publication.accepted is True
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.SUCCEEDED)
    execution.refresh_from_db()
    terminal_summary = execution.workflow_progress_summary_json
    assert terminal_summary is not None
    stored_preview = WorkflowProgressNodeDetail.objects.get(node_id="real-a")
    stored_payload = bytes(stored_preview.payload)
    stored_digest = stored_preview.digest
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"persisted"],
    }
    user = get_user_model().objects.create_superuser(username="graph-real-schema-v3")
    admin_obj = _task_admin()
    current_request = RequestFactory().get("/admin/workflow/graph/?attempt_number=1")
    current_request.user = user

    current_response = admin_obj.workflow_graph_view(current_request, str(execution.pk))

    current_payload = _json(current_response)
    assert current_response.status_code == 200
    assert current_payload["status"] == "AVAILABLE"
    assert current_payload["nodes"][0]["output_preview"] == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": "[REDACTED]",
    }
    TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.SUCCEEDED,
        workflow_progress_summary_json=terminal_summary,
    )
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        state=TaskState.QUEUED,
        attempt_number=2,
        execution_generation=2,
        workflow_run_id="00000000-0000-0000-0000-000000000220",
        workflow_progress_summary_json=None,
    )
    request = RequestFactory().get("/admin/workflow/graph/?attempt_number=1")
    request.user = user

    response = admin_obj.workflow_graph_view(request, str(execution.pk))

    payload = _json(response)
    assert response.status_code == 200
    assert payload["status"] == "AVAILABLE"
    assert payload["counts"] == {"nodes": 2, "edges": 1}
    assert [node["id"] for node in payload["nodes"]] == ["real-a", "real-b"]
    assert all(node["state"] == "SUCCEEDED" for node in payload["nodes"])
    assert payload["nodes"][0]["output_preview"] == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": "[REDACTED]",
    }
    assert payload["nodes"][1]["output_preview"]["availability"] == "UNAVAILABLE"
    content = response.content.decode("utf-8")
    assert "real-result-sentinel" not in content
    assert "real-runtime-sentinel" not in content
    assert "real-runtime-node-sentinel" not in content
    assert "real-ray-option-sentinel" not in content
    assert "real-callable-sentinel" not in content

    detail_request = RequestFactory().get("/admin/workflow/node/?attempt_number=1&node_id=real-a")
    detail_request.user = user
    detail_response = admin_obj.workflow_node_detail_view(
        detail_request,
        str(execution.pk),
    )
    detail_payload = _json(detail_response)
    assert detail_response.status_code == 200
    assert detail_payload["run_identity"]["attempt_number"] == 1
    assert detail_payload["found"] is True
    assert detail_payload["item"]["node_id"] == "real-a"
    assert detail_payload["item"]["output_preview"] == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": "[REDACTED]",
    }
    stored_preview.refresh_from_db()
    assert bytes(stored_preview.payload) == stored_payload
    assert stored_preview.digest == stored_digest
