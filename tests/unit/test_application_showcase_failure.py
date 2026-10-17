"""The added catalogue case must prove the exact single-attempt boundary."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from django_ray.workflow.admin_graph import (
    build_admin_workflow_graph,
    inspect_admin_workflow_graph_summary,
)
from qualification.application import run_showcase_failure as runner
from qualification.application import showcase_failure as contract
from qualification.application.run_workflows import showcase_failure_case, workflow_cases
from qualification.application.workflow_http import read_full_workflow_graph
from tests.integration.test_admin_workflow_graph import (
    _node_detail,
    _page,
    _summary_envelope,
    _topology_node,
)

TASK_ID = "00000000-0000-4000-8000-000000000015"


@pytest.fixture
def surfaces():
    # Literal boundary from the original full-script catalogue, independently
    # represented here rather than copied from the new verifier's constants.
    nodes = [
        "0.0",
        "0.1.g0.0",
        "0.1.g0.1",
        "0.1.g1.0.g0",
        "0.1.g1.0.g1",
        "0.1.g1.1",
        "0.1.g2",
        "0.2",
        "0.3.g0",
        "0.3.g1.0.g0",
        "0.3.g1.0.g1.0.g0",
        "0.3.g1.0.g1.0.g1",
        "0.3.g1.0.g1.1",
        "0.3.g1.1",
        "0.4",
        "0.5",
    ]
    edges = [
        ("0.0", "0.1.g0.0"),
        ("0.0", "0.1.g1.0.g0"),
        ("0.0", "0.1.g1.0.g1"),
        ("0.0", "0.1.g2"),
        ("0.1.g0.0", "0.1.g0.1"),
        ("0.1.g0.1", "0.2"),
        ("0.1.g1.0.g0", "0.1.g1.1"),
        ("0.1.g1.0.g1", "0.1.g1.1"),
        ("0.1.g1.1", "0.2"),
        ("0.1.g2", "0.2"),
        ("0.2", "0.3.g0"),
        ("0.2", "0.3.g1.0.g0"),
        ("0.2", "0.3.g1.0.g1.0.g0"),
        ("0.2", "0.3.g1.0.g1.0.g1"),
        ("0.3.g0", "0.4"),
        ("0.3.g1.0.g0", "0.3.g1.1"),
        ("0.3.g1.0.g1.0.g0", "0.3.g1.0.g1.1"),
        ("0.3.g1.0.g1.0.g1", "0.3.g1.0.g1.1"),
        ("0.3.g1.0.g1.1", "0.3.g1.1"),
        ("0.3.g1.1", "0.4"),
        ("0.4", "0.5"),
    ]
    states = ["SUCCEEDED"] * 15 + ["FAILED"]
    details = [_node_detail(node, state) for node, state in zip(nodes, states, strict=True)]
    by_id = {detail["node_id"]: detail for detail in details}
    previews = {
        "0.4": ("AVAILABLE", {"collection_size": 1}),
        "0.1.g0.1": ("NOT_REQUESTED", None),
        "0.1.g1.0.g1": ("FAILED", None),
        "0.5": ("UNAVAILABLE", None),
    }
    for node_id, (availability, value) in previews.items():
        by_id[node_id]["schema_version"] = 2
        by_id[node_id]["output_preview"] = {
            "schema_version": 1,
            "availability": availability,
            "value": value,
        }
    for node_id in ("0.1.g0.1", "0.5"):
        by_id[node_id]["fanout"] = {
            "max_concurrency": 1,
            "max_items": 1,
            "submitted_items": 1,
            "completed_items": 1 if node_id == "0.1.g0.1" else 0,
            "in_flight_items": 0 if node_id == "0.1.g0.1" else 1,
            "input_exhausted": True,
        }
    by_id["0.5"]["error"] = contract.FAILURE_MESSAGE
    summary = _summary_envelope(states, edges, task_id=TASK_ID)
    summary["summary"]["reporting_policy"] = "full"
    pages = {
        "topology_nodes": _page(
            "topology_nodes",
            [
                _topology_node(node, kind="map" if node in {"0.1.g0.1", "0.5"} else "task")
                for node in nodes
            ],
            task_id=TASK_ID,
        ),
        "topology_edges": _page(
            "topology_edges", [{"source": a, "target": b} for a, b in edges], task_id=TASK_ID
        ),
        "node_details": _page("node_details", details, task_id=TASK_ID),
    }
    graph = build_admin_workflow_graph(inspect_admin_workflow_graph_summary(summary), **pages)
    root = f"/api/cluster/workflows/{TASK_ID}"
    values = {
        f"{root}?attempt_number=1": summary,
        f"{root}/topology/nodes?limit=100&attempt_number=1": pages["topology_nodes"],
        f"{root}/topology/edges?limit=100&attempt_number=1": pages["topology_edges"],
        f"{root}/nodes?limit=100&attempt_number=1": pages["node_details"],
        "/admin/django_ray/raytaskexecution/7/workflow/graph/?attempt_number=1": graph,
    }
    for detail in details:
        query = urlencode({"node_id": detail["node_id"], "attempt_number": 1})
        values[f"{root}/node-detail?{query}"] = {
            **{
                key: summary[key]
                for key in (
                    "schema_version",
                    "task_id",
                    "run_identity",
                    "publication",
                    "availability",
                    "complete",
                )
            },
            "schema": "django-ray.workflow-progress-node",
            "found": True,
            "item": detail,
        }
    arguments = {
        "task_id": TASK_ID,
        "execution_pk": 7,
        "run_identity": copy.deepcopy(summary["run_identity"]),
        "expected_state": "FAILED",
        "token": "fixture-credential",
        "admin_cookie": "sessionid=fixture-session",
        "fixture": "showcase-failure",
    }
    request = Mock(side_effect=lambda path, **_: (200, json.dumps(values[path]).encode()))
    return request, arguments, values, graph, details


def test_exact_case_stays_outside_original_matrix_and_has_one_failure():
    case = showcase_failure_case()
    assert case not in workflow_cases()
    assert len(workflow_cases()) == 12
    assert sum(len(case.states) for case in workflow_cases()) == 16
    assert case.states == ("FAILED",) and case.policy == "full"
    assert dict(case.options) == {
        "item_count": 1,
        "work_seconds": 0.01,
        "failure_stage": "reserve_inventory",
        "failure_item": 0,
    }


def test_http_proves_exact_graph_and_every_attempt_pinned_indexed_detail(surfaces):
    request, arguments, _, _, _ = surfaces
    observation = read_full_workflow_graph(request, **arguments)
    assert observation["counts"] == {"nodes": 16, "edges": 21}
    assert observation["showcase_failure"] == dict.fromkeys(contract.FACTS, True)
    assert request.call_count == 21
    indexed = [call for call in request.call_args_list if "/node-detail?" in call.args[0]]
    assert len(indexed) == 16 and len({call.args[0] for call in indexed}) == 16
    assert all("attempt_number=1" in call.args[0] for call in indexed)
    assert all(
        call.kwargs["headers"] == {"Authorization": "Bearer fixture-credential"} for call in indexed
    )
    assert all(call.kwargs["response_limit"] <= 256 * 1024 for call in indexed)
    assert "fixture-credential" not in json.dumps(observation)


@pytest.mark.parametrize(
    "change", ["origin", "running", "duplicate-node", "edge", "map", "detail", "preview"]
)
def test_wrong_graph_or_preview_is_rejected_before_indexed_requests(surfaces, change):
    _, _, _, graph, details = surfaces
    if change == "origin":
        graph["nodes"][0]["state"] = "FAILED"
    elif change == "running":
        next(node for node in graph["nodes"] if node["id"] == "0.4")["state"] = "RUNNING"
    elif change == "duplicate-node":
        graph["nodes"][-1] = graph["nodes"][0]
    elif change == "edge":
        graph["edges"][0]["target"] = "0.5"
    elif change == "map":
        next(node for node in graph["nodes"] if node["id"] == "0.5")["kind"] = "task"
    elif change == "detail":
        details.pop()
    else:
        next(node for node in graph["nodes"] if node["id"] == "0.4")["output_preview"]["value"] = {
            "collection_size": 3
        }
    with pytest.raises(ValueError):
        contract.verify_graph(graph, details)


@pytest.mark.parametrize("node_id", ["0.6", "0.7.g0", "0.7.g1", "0.7.g2", "0.8"])
def test_every_never_submitted_descendant_is_absent_not_fabricated(surfaces, node_id):
    _, _, _, graph, details = surfaces
    graph["nodes"].append({"id": node_id, "state": "PENDING"})
    with pytest.raises(ValueError, match="boundary"):
        contract.verify_graph(graph, details)


@pytest.mark.parametrize(
    "change", ["missing", "different", "identity", "publication", "nonboolean"]
)
def test_each_indexed_detail_must_match_its_complete_publication(surfaces, change):
    request, arguments, values, _, _ = surfaces
    path = next(path for path in values if "/node-detail?" in path)
    value = copy.deepcopy(values[path])
    values[path] = value
    if change == "missing":
        value["found"] = False
    elif change == "nonboolean":
        value["found"] = 1
    elif change == "different":
        value["item"]["state"] = "PENDING"
    elif change == "identity":
        value["run_identity"]["attempt_number"] = 2
    else:
        value["publication"]["detail_revision"] += 1
    with pytest.raises(ValueError):
        read_full_workflow_graph(request, **arguments)


def complete_observation(surfaces):
    request, arguments, _, _, _ = surfaces
    value = read_full_workflow_graph(request, **arguments)
    value["admin_contract"] = {
        "topology_nodes": 16,
        "topology_edges": 21,
        "node_details": 16,
        "graph_nodes": 16,
        "graph_edges": 21,
        "graph_pending_nodes": 0,
        "graph_running_nodes": 0,
        "graph_succeeded_nodes": 15,
        "graph_failed_nodes": 1,
        "graph_failure_path_nodes": 16,
        "graph_failure_origins": 1,
        "graph_incoming_failure_edges": 1,
        "graph_available_previews": 13,
        "graph_failed_previews": 1,
        "graph_unavailable_previews": 1,
        "graph_preview_contract": "showcase-failed-verified",
        "diagnostics_preserved": True,
    }
    value["browser_contract"] = {
        "status": "passed",
        "attempts_rendered": 1,
        "policy": "full",
        "javascript_errors": 0,
        "diagnostics_preserved": True,
    }
    return value


@pytest.mark.parametrize(
    "failure", [None, "missing", "boolean", "extra", "attempt", "admin", "browser", "oversized"]
)
def test_collector_refuses_partial_or_ambiguous_success(surfaces, failure):
    value = {
        "schema_version": 1,
        "layer": runner.LAYER,
        "generation": "after",
        "status": "passed",
        "complete_application_gate": False,
        "complete_workflow_gate": False,
        "failed_stage": None,
        "observation": complete_observation(surfaces),
    }
    observation = value["observation"]
    if failure == "missing":
        del observation["showcase_failure"][contract.FACTS[0]]
    elif failure == "boolean":
        observation["showcase_failure"][contract.FACTS[0]] = 1
    elif failure == "extra":
        observation["showcase_failure"]["invented"] = True
    elif failure == "attempt":
        observation["run_identity"]["attempt_number"] = 2
    elif failure == "admin":
        observation["admin_contract"]["graph_pending_nodes"] = False
    elif failure == "browser":
        observation["browser_contract"]["diagnostics_preserved"] = False
    elif failure == "oversized":
        value["unbounded"] = "x" * runner.RECEIPT_MAX_BYTES
    from qualification.application.run_chainsaw import parse_receipts

    if failure:
        with pytest.raises(ValueError):
            parse_receipts(json.dumps(value).encode(), ("after-showcase",))
    else:
        runner.validate_receipt(value)
        parse_receipts(json.dumps(value).encode(), ("after-showcase",))
        with pytest.raises(ValueError, match="another Ray generation"):
            parse_receipts(json.dumps(value).encode(), ("before-showcase",))


@pytest.mark.parametrize("failure", [None, "observe", "timeout", "cleanup", "oversized", "policy"])
def test_child_preserves_deadline_failure_and_never_exports_exceptions(
    surfaces, monkeypatch, tmp_path, capsys, failure
):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    alarms = []
    monkeypatch.setattr(
        runner,
        "signal",
        SimpleNamespace(
            SIGALRM=14,
            signal=lambda *_: 0,
            alarm=alarms.append,
        ),
    )
    monkeypatch.setattr("django.setup", lambda: None)
    monkeypatch.setattr(
        "django_ray.conf.settings.get_settings",
        lambda: {
            "WORKFLOW_PROGRESS_REPORTING_POLICY": "full",
            "RETRY_EXCEPTION_DENYLIST": []
            if failure == "policy"
            else ["testproject.apps.cluster_tasks.workflows.WorkflowShowcaseFixtureError"],
        },
    )
    monkeypatch.setattr(runner, "read_token", lambda _: "private-token")
    observation = complete_observation(surfaces)
    if failure == "oversized":
        observation["bounded-diagnostic"] = "x" * runner.RECEIPT_MAX_BYTES
    observe = Mock(return_value=observation)
    if failure in {"observe", "cleanup"}:
        observe.side_effect = ValueError("private failure detail")
    elif failure == "timeout":
        observe.side_effect = TimeoutError("private failure detail")
    monkeypatch.setattr(runner, "observe", observe)
    destination = tmp_path / "receipt.json"
    assert runner.main(
        [
            "--generation",
            "before",
            "--token-file",
            str(tmp_path / "token"),
            "--receipt",
            str(destination),
        ]
    ) == (0 if failure is None else 1)
    output = capsys.readouterr().out
    assert "private" not in output
    value = json.loads(output)
    assert alarms == [150, 0]
    assert destination.exists() is (failure is None)
    if failure:
        assert "observation" not in value and value["status"] == "failed"
    else:
        runner.validate_receipt(value)
        assert json.loads(destination.read_text()) == value


def test_work_alarm_does_not_convert_expiry_to_success():
    with pytest.raises(TimeoutError):
        runner._expired(14, None)


@pytest.mark.parametrize(
    "key, wrong",
    [
        ("status", "failed"),
        ("attempts_rendered", 2),
        ("attempts_rendered", True),
        ("policy", "terminal_only"),
        ("javascript_errors", 1),
        ("javascript_errors", False),
        ("diagnostics_preserved", False),
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_browser_facts_cannot_be_missing_false_or_from_another_attempt(
    surfaces, key, wrong, missing
):
    observation = complete_observation(surfaces)
    browser = observation["browser_contract"]
    if missing:
        del browser[key]
    else:
        browser[key] = wrong
    with pytest.raises(ValueError, match="browser/preservation"):
        contract.validate_observation(observation)


def test_wrong_failure_origin_is_refused_before_graph_or_payload_reads(monkeypatch):
    from qualification.application import run_workflows

    case = showcase_failure_case()
    manager = Mock()
    manager.only.return_value.get.return_value = SimpleNamespace(
        pk=15, state="FAILED", attempt_number=1, callable_path=case.callable_path
    )
    manager.filter.return_value.exists.return_value = False
    monkeypatch.setattr("django_ray.models.RayTaskExecution.objects", manager)
    monkeypatch.setattr(
        run_workflows, "validate_task_status_payload", lambda *_args, **_kw: "FAILED"
    )
    graph = Mock()
    monkeypatch.setattr(run_workflows, "read_full_workflow_graph", graph)
    terminal = Mock()
    request = Mock(
        side_effect=[
            (
                200,
                json.dumps({"task_id": TASK_ID, "args": [], "kwargs": dict(case.options)}).encode(),
            ),
            (200, b"{}"),
        ]
    )
    with pytest.raises(ValueError, match="fixed reservation boundary"):
        run_workflows.execute_case(request, token="fixed-token", case=case, on_terminal=terminal)
    manager.filter.assert_called_once_with(pk=15, error_message=contract.FAILURE_MESSAGE)
    assert "error_message" not in manager.only.call_args.args
    terminal.assert_not_called()
    graph.assert_not_called()
