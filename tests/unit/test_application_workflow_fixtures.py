"""A coherent API/Admin projection must still match its independently fixed case."""

import copy

import pytest

from qualification.application.workflow_fixtures import verify_complex_graph, verify_recovery_graph
from scripts import local_kuberay_gate as legacy


def recovery_graph(attempt):
    if attempt == 1:
        states = dict.fromkeys(legacy.WORKFLOW_RECOVERY_EARLY_NODE_IDS, "PENDING")
        states[legacy.WORKFLOW_RECOVERY_EARLY_FAILURE_NODE_ID] = "FAILED"
        edges = legacy.WORKFLOW_RECOVERY_EARLY_EDGES
    elif attempt == 2:
        states = dict.fromkeys(legacy.WORKFLOW_RECOVERY_MID_NODE_IDS, "PENDING")
        states.update(dict.fromkeys(legacy.WORKFLOW_RECOVERY_MID_SUCCEEDED_NODES, "SUCCEEDED"))
        states[legacy.WORKFLOW_RECOVERY_MID_FAILURE_NODE_ID] = "FAILED"
        edges = legacy.WORKFLOW_RECOVERY_MID_EDGES
    else:
        states = dict.fromkeys(
            frozenset().union(*legacy.WORKFLOW_SHOWCASE_NODE_LAYERS), "SUCCEEDED"
        )
        edges = legacy.WORKFLOW_SHOWCASE_EDGES
    return {
        "nodes": [{"id": key, "state": value} for key, value in sorted(states.items())],
        "edges": [{"source": a, "target": b} for a, b in sorted(edges)],
    }


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_recovery_fixture_accepts_retained_local_gate_contract(attempt):
    verify_recovery_graph(recovery_graph(attempt), attempt=attempt)


@pytest.mark.parametrize("attempt", [1, 2, 3])
@pytest.mark.parametrize(
    "change", ["state", "missing-node", "duplicate-node", "missing-edge", "substituted-edge"]
)
def test_recovery_fixture_rejects_coherent_but_wrong_graph(attempt, change):
    graph = recovery_graph(attempt)
    if change == "state":
        graph["nodes"][0]["state"] = "RUNNING"
    elif change == "missing-node":
        graph["nodes"].pop()
    elif change == "duplicate-node":
        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
    elif change == "missing-edge":
        graph["edges"].pop()
    else:
        graph["edges"][0]["source"] = "another-valid-node"
    with pytest.raises(ValueError, match="Recovery fixture graph"):
        verify_recovery_graph(graph, attempt=attempt)


def test_complex_failure_refuses_a_successful_join_after_failed_map():
    graph = {
        "nodes": [
            {"id": "0.0", "state": "SUCCEEDED"},
            {"id": "0.1.g1.0", "state": "SUCCEEDED"},
            {"id": "0.1.g1.1", "state": "FAILED"},
        ],
        "edges": [
            {"source": "0.0", "target": "0.1.g1.0"},
            {"source": "0.1.g1.0", "target": "0.1.g1.1"},
        ],
    }
    verify_complex_graph(graph, failed=True)
    graph["nodes"].append({"id": "0.2", "state": "SUCCEEDED"})
    with pytest.raises(ValueError, match="failure changed"):
        verify_complex_graph(graph, failed=True)


def test_complex_success_refuses_an_empty_successful_projection():
    with pytest.raises(ValueError, match="split/join graph"):
        verify_complex_graph({"nodes": [], "edges": []}, failed=False)
