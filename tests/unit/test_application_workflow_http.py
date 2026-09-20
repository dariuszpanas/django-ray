"""HTTP workflow assertions must bind every response to one durable attempt."""

import copy
import json
from unittest.mock import Mock

import pytest

from django_ray.workflow.admin_graph import (
    build_admin_workflow_graph,
    inspect_admin_workflow_graph_summary,
)
from qualification.application.workflow_http import read_full_workflow_graph
from tests.integration.test_admin_workflow_graph import _graph_case

TASK_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def observation():
    summary, pages = _graph_case()
    summary["task_id"] = TASK_ID
    summary["summary"]["reporting_policy"] = "full"
    for page in pages.values():
        page["task_id"] = TASK_ID
    graph = build_admin_workflow_graph(inspect_admin_workflow_graph_summary(summary), **pages)
    root = f"/api/cluster/workflows/{TASK_ID}"
    values = {
        f"{root}?attempt_number=1": summary,
        f"{root}/topology/nodes?limit=100&attempt_number=1": pages["topology_nodes"],
        f"{root}/topology/edges?limit=100&attempt_number=1": pages["topology_edges"],
        f"{root}/nodes?limit=100&attempt_number=1": pages["node_details"],
        "/admin/django_ray/raytaskexecution/7/workflow/graph/?attempt_number=1": graph,
    }

    def response(path, **_kwargs):
        value = values[path]
        return (200, value) if isinstance(value, bytes) else (200, json.dumps(value).encode())

    request = Mock(side_effect=response)
    arguments = {
        "task_id": TASK_ID,
        "execution_pk": 7,
        "run_identity": copy.deepcopy(summary["run_identity"]),
        "expected_state": "SUCCEEDED",
        "token": "api-test-credential",
        "admin_cookie": "sessionid=admin-test-credential",
    }
    return request, arguments, values


def test_reads_pinned_surfaces_with_separate_credentials(observation):
    request, arguments, _ = observation
    receipt = read_full_workflow_graph(request, **arguments)
    assert receipt["api_admin_graph_match"] is True
    assert receipt["complete_workflow_gate"] is False
    assert receipt["counts"] == {"nodes": 3, "edges": 2}
    assert "credential" not in json.dumps(receipt)
    assert request.call_count == 5
    for call in request.call_args_list:
        assert call.kwargs["method"] == "GET"
        assert call.kwargs["response_limit"] <= 256 * 1024
        assert call.kwargs["required_cache_directives"] == (
            frozenset({"no-store"}) if call.args[0].startswith("/admin/") else frozenset()
        )
        headers = call.kwargs["headers"]
        assert set(headers) == (
            {"Cookie"} if call.args[0].startswith("/admin/") else {"Authorization"}
        )


@pytest.mark.parametrize("collection", ["topology/nodes", "topology/edges", "nodes"])
def test_rejects_cross_attempt_page(observation, collection):
    request, arguments, values = observation
    page = values[f"/api/cluster/workflows/{TASK_ID}/{collection}?limit=100&attempt_number=1"]
    page["run_identity"]["attempt_number"] = 2
    with pytest.raises(ValueError, match="different workflow run"):
        read_full_workflow_graph(request, **arguments)


def test_rejects_different_admin_graph(observation):
    request, arguments, values = observation
    values["/admin/django_ray/raytaskexecution/7/workflow/graph/?attempt_number=1"]["nodes"] = []
    with pytest.raises(ValueError, match="Admin graph differs"):
        read_full_workflow_graph(request, **arguments)


@pytest.mark.parametrize("body", [b'{"schema":1,"schema":2}', b'{"value":NaN}', b"[]"])
def test_rejects_ambiguous_json_before_followup_requests(observation, body):
    request, arguments, values = observation
    values[f"/api/cluster/workflows/{TASK_ID}?attempt_number=1"] = body
    with pytest.raises(ValueError):
        read_full_workflow_graph(request, **arguments)
    assert request.call_count == 1


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
def test_non_success_http_stops_observation(observation, status):
    request, arguments, _ = observation
    request.side_effect = lambda *_args, **_kwargs: (status, b"private-response")
    with pytest.raises(ValueError, match="non-success HTTP"):
        read_full_workflow_graph(request, **arguments)
    assert request.call_count == 1


def test_rejects_changed_terminal_outcome(observation):
    request, arguments, _ = observation
    arguments["expected_state"] = "FAILED"
    with pytest.raises(ValueError, match="expected full-reporting outcome"):
        read_full_workflow_graph(request, **arguments)


def terminal_only(observation):
    request, arguments, values = observation
    arguments["reporting_policy"] = "terminal_only"
    for path, value in values.items():
        if path.startswith("/admin/"):
            value.update(
                status="UNAVAILABLE",
                complete=False,
                nodes=[],
                edges=[],
                counts={"nodes": 0, "edges": 0},
                message=(
                    "This attempt used terminal-only reporting, which saves a summary without "
                    "graph details. Use supported full reporting for future runs if you need a graph."
                ),
            )
            continue
        value.update(availability="OMITTED_BY_POLICY", complete=False)
        value["publication"].update(summary_revision=1, topology_version=None, detail_revision=None)
        if "summary" in value:
            value["summary"].update(
                reporting_policy="terminal_only",
                summary_revision=1,
                topology_version=None,
                detail_revision=None,
                storage={"kind": "database", "manifest_id": None},
                detail={
                    "availability": "OMITTED_BY_POLICY",
                    "complete": False,
                    "truncation_reasons": [],
                },
            )
            for key in ("node_counts", "edge_counts"):
                counts = value["summary"][key]
                for field in counts.keys() - {"declared"}:
                    counts[field] = 0
        else:
            value.update(returned_count=0, items=[], next_cursor=None)
    return request, arguments, values


def test_terminal_only_requires_empty_api_and_admin_detail(observation):
    request, arguments, _ = terminal_only(observation)
    receipt = read_full_workflow_graph(request, **arguments)
    assert receipt["reporting_policy"] == "terminal_only"
    assert receipt["counts"] == {"nodes": 0, "edges": 0}
    assert request.call_count == 5


@pytest.mark.parametrize(
    "message",
    [
        "Graph details are unavailable for this attempt.",
        "This workflow has not finished. Check again after it finishes.",
        "Workflow reporting was disabled for this attempt, so no graph was saved.",
        None,
    ],
)
def test_terminal_only_rejects_missing_or_incorrect_policy_explanation(observation, message):
    request, arguments, values = terminal_only(observation)
    values["/admin/django_ray/raytaskexecution/7/workflow/graph/?attempt_number=1"]["message"] = (
        message
    )
    with pytest.raises(ValueError, match="did not explain the observed reporting policy"):
        read_full_workflow_graph(request, **arguments)


@pytest.mark.parametrize("surface", ["nodes", "admin"])
def test_terminal_only_rejects_leaked_detail(observation, surface):
    request, arguments, values = terminal_only(observation)
    if surface == "admin":
        values["/admin/django_ray/raytaskexecution/7/workflow/graph/?attempt_number=1"]["nodes"] = [
            {}
        ]
    else:
        values[f"/api/cluster/workflows/{TASK_ID}/nodes?limit=100&attempt_number=1"]["items"] = [{}]
    with pytest.raises(ValueError, match="Terminal-only"):
        read_full_workflow_graph(request, **arguments)


@pytest.mark.parametrize(
    "field,value", [("retained_detail", 1), ("running", True), ("discovered", 0.0)]
)
def test_terminal_only_rejects_false_summary_counts(observation, field, value):
    request, arguments, values = terminal_only(observation)
    values[f"/api/cluster/workflows/{TASK_ID}?attempt_number=1"]["summary"]["node_counts"][
        field
    ] = value
    with pytest.raises(ValueError, match="claimed observed graph detail"):
        read_full_workflow_graph(request, **arguments)


@pytest.mark.parametrize(
    "corruption", [None, "identity", "publication", "policy", "graph", "message"]
)
def test_disabled_workflow_requires_absent_publication_and_honest_graph(corruption):
    from qualification.application.workflow_http import read_disabled_workflow_graph

    summary = {
        "schema": "django-ray.workflow-progress-summary",
        "schema_version": 1,
        "task_id": "disabled-task",
        "availability": "DISABLED",
        "complete": False,
        "source_schema_version": None,
        "summary": None,
        "run_identity": None,
        "publication": {
            "summary_revision": None,
            "topology_version": None,
            "detail_revision": None,
        },
    }
    graph = {
        "schema": "django-ray.admin-workflow-graph",
        "schema_version": 2,
        "status": "UNAVAILABLE",
        "complete": False,
        "nodes": [],
        "edges": [],
        "counts": {"nodes": 0, "edges": 0},
        "message": "Workflow reporting was disabled for this attempt, so no graph was saved. "
        "Use supported full reporting for future runs if you need a graph.",
    }
    if corruption == "identity":
        summary["run_identity"] = {"attempt_number": 2}
    elif corruption == "publication":
        summary["publication"]["summary_revision"] = 1
    elif corruption == "policy":
        summary["availability"] = "NOT_REPORTED"
    elif corruption == "graph":
        graph["nodes"] = [{"id": "invented"}]
    elif corruption == "message":
        graph["message"] = "Wait for a graph"
    request = Mock(side_effect=[(200, json.dumps(value).encode()) for value in (summary, graph)])
    arguments = {
        "task_id": "disabled-task",
        "execution_pk": 12,
        "expected_state": "SUCCEEDED",
        "token": "credential",
        "admin_cookie": "session",
    }
    if corruption:
        with pytest.raises(ValueError, match="invented publication"):
            read_disabled_workflow_graph(request, **arguments)
    else:
        receipt = read_disabled_workflow_graph(request, **arguments)
        assert receipt["publication"] is None and receipt["run_identity"] is None
        assert "credential" not in json.dumps(receipt)
        assert request.call_count == 2
        assert request.call_args_list[0].kwargs["headers"] == {"Authorization": "Bearer credential"}
        assert request.call_args_list[1].kwargs["headers"] == {"Cookie": "session"}
