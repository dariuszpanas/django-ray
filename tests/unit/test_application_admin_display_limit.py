"""A display limit must not hide missing, changed or cross-attempt API detail."""

import copy
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from django_ray.workflow.admin_graph import degraded_admin_workflow_graph
from qualification.application.workflow_display_limit import read_admin_display_limit
from tests.integration.test_admin_workflow_graph import _graph_case, _page

TASK_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def observation():
    summary, _ = _graph_case(states=["SUCCEEDED"] * 101)
    summary["task_id"] = TASK_ID
    summary["summary"]["reporting_policy"] = "full"
    nodes = [{"node_id": f"0.{i}"} for i in range(101)]
    collections = {
        "topology/nodes": ("topology_nodes", nodes),
        "topology/edges": (
            "topology_edges",
            [{"source": f"0.{i}", "target": f"0.{i + 1}"} for i in range(100)],
        ),
        "nodes": ("node_details", [{**n, "state": "SUCCEEDED"} for n in nodes]),
    }
    graph = degraded_admin_workflow_graph("LIMIT_EXCEEDED")
    values = {"summary": summary, "graph": graph, "collections": collections}

    def respond(path, **_kwargs):
        if path.startswith("/admin/"):
            value = values["graph"]
        else:
            suffix = urlsplit(path).path.split(TASK_ID)[1].lstrip("/")
            if not suffix:
                value = summary
            else:
                name, items = collections[suffix]
                offset = 100 if "cursor" in parse_qs(urlsplit(path).query) else 0
                value = _page(name, copy.deepcopy(items[offset : offset + 100]), task_id=TASK_ID)
                value["run_identity"] = copy.deepcopy(summary["run_identity"])
                value["publication"] = copy.deepcopy(summary["publication"])
                value["next_cursor"] = "last-page" if offset + 100 < len(items) else None
        return 200, json.dumps(value).encode()

    request = Mock(side_effect=respond)
    arguments = {
        "task_id": TASK_ID,
        "execution_pk": 7,
        "run_identity": copy.deepcopy(summary["run_identity"]),
        "token": "api-credential",
        "admin_cookie": "admin-credential",
    }
    return request, arguments, values


def test_proves_api_detail_and_explained_empty_admin_limit(observation):
    request, arguments, _ = observation
    receipt = read_admin_display_limit(request, **arguments)
    assert receipt["api_counts"] == {"nodes": 101, "edges": 100, "details": 101}
    assert receipt["admin_status"] == "LIMIT_EXCEEDED"
    assert receipt["complete_workflow_gate"] is False
    assert "credential" not in json.dumps(receipt)
    assert request.call_count == 7
    assert request.call_args.kwargs["headers"] == {"Cookie": "admin-credential"}
    assert request.call_args.kwargs["required_cache_directives"] == frozenset({"no-store"})


@pytest.mark.parametrize(
    "fault",
    ["missing", "duplicate", "state", "edge", "admin-status", "admin-complete", "admin-count"],
)
def test_refuses_incomplete_or_forged_display_limit(observation, fault):
    request, arguments, values = observation
    if fault == "missing":
        values["collections"]["topology/nodes"][1].pop()
    elif fault == "duplicate":
        values["collections"]["topology/nodes"][1][-1] = {"node_id": "0.0"}
    elif fault == "state":
        values["collections"]["nodes"][1][-1]["state"] = "RUNNING"
    elif fault == "edge":
        values["collections"]["topology/edges"][1][0]["target"] = "0.2"
    elif fault == "admin-status":
        values["graph"]["status"] = "UNAVAILABLE"
    elif fault == "admin-complete":
        values["graph"]["complete"] = 0
    else:
        values["graph"]["counts"]["nodes"] = False
    with pytest.raises(ValueError):
        read_admin_display_limit(request, **arguments)


def test_rejects_summary_that_disagrees_with_retained_graph(observation):
    request, arguments, values = observation
    values["summary"]["summary"]["node_counts"]["succeeded"] = 100
    with pytest.raises(ValueError, match="disagrees"):
        read_admin_display_limit(request, **arguments)
    assert request.call_count == 1
