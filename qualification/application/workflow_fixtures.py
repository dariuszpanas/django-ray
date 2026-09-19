"""Independent expected topology for the fixed public workflow fixtures."""

from collections.abc import Mapping
from typing import Any

WORKFLOW_SHOWCASE_NODE_LAYERS = (
    frozenset({"0.0"}),
    frozenset(
        {
            "0.1.g0.0",
            "0.1.g1.0.g0",
            "0.1.g1.0.g1",
            "0.1.g2",
        }
    ),
    frozenset({"0.1.g0.1", "0.1.g1.1"}),
    frozenset({"0.2"}),
    frozenset(
        {
            "0.3.g0",
            "0.3.g1.0.g0",
            "0.3.g1.0.g1.0.g0",
            "0.3.g1.0.g1.0.g1",
        }
    ),
    frozenset({"0.3.g1.0.g1.1"}),
    frozenset({"0.3.g1.1"}),
    frozenset({"0.4"}),
    frozenset({"0.5"}),
    frozenset({"0.6"}),
    frozenset({"0.7.g0", "0.7.g1", "0.7.g2"}),
    frozenset({"0.8"}),
)


WORKFLOW_SHOWCASE_EDGES = frozenset(
    {
        ("0.0", "0.1.g0.0"),
        ("0.1.g0.0", "0.1.g0.1"),
        ("0.0", "0.1.g1.0.g0"),
        ("0.0", "0.1.g1.0.g1"),
        ("0.1.g1.0.g0", "0.1.g1.1"),
        ("0.1.g1.0.g1", "0.1.g1.1"),
        ("0.0", "0.1.g2"),
        ("0.1.g0.1", "0.2"),
        ("0.1.g1.1", "0.2"),
        ("0.1.g2", "0.2"),
        ("0.2", "0.3.g0"),
        ("0.2", "0.3.g1.0.g0"),
        ("0.2", "0.3.g1.0.g1.0.g0"),
        ("0.2", "0.3.g1.0.g1.0.g1"),
        ("0.3.g1.0.g1.0.g0", "0.3.g1.0.g1.1"),
        ("0.3.g1.0.g1.0.g1", "0.3.g1.0.g1.1"),
        ("0.3.g1.0.g0", "0.3.g1.1"),
        ("0.3.g1.0.g1.1", "0.3.g1.1"),
        ("0.3.g0", "0.4"),
        ("0.3.g1.1", "0.4"),
        ("0.4", "0.5"),
        ("0.5", "0.6"),
        ("0.6", "0.7.g0"),
        ("0.6", "0.7.g1"),
        ("0.6", "0.7.g2"),
        ("0.7.g0", "0.8"),
        ("0.7.g1", "0.8"),
        ("0.7.g2", "0.8"),
    }
)


def recovery_expectation(attempt: int) -> tuple[dict[str, str], frozenset[tuple[str, str]]]:
    """Describe each deterministic failure boundary without inspecting actual output."""
    if type(attempt) is not int or attempt not in {1, 2, 3}:
        raise ValueError("Unsupported recovery fixture attempt")
    if attempt == 1:
        return {"0.0": "FAILED", "0.1.g0.0": "PENDING"}, frozenset({("0.0", "0.1.g0.0")})
    if attempt == 2:
        states = dict.fromkeys(frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS[:8]), "PENDING")
        states.update(
            dict.fromkeys(frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS[:3]), "SUCCEEDED")
        )
        states["0.2"] = "FAILED"
        return states, frozenset(
            (a, b) for a, b in WORKFLOW_SHOWCASE_EDGES if a in states and b in states
        )
    return dict.fromkeys(
        frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS), "SUCCEEDED"
    ), WORKFLOW_SHOWCASE_EDGES


def verify_recovery_graph(graph: Mapping[str, Any], *, attempt: int) -> None:
    """Reject coherent but incorrect graphs, including duplicated or missing records."""
    expected_states, expected_edges = recovery_expectation(attempt)
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("Recovery fixture graph has no node or edge list")
    if (
        len(nodes) != len(expected_states)
        or any(not isinstance(node, dict) for node in nodes)
        or {node.get("id"): node.get("state") for node in nodes} != expected_states
    ):
        raise ValueError("Recovery fixture graph has unexpected node membership or states")
    if (
        len(edges) != len(expected_edges)
        or any(not isinstance(edge, dict) for edge in edges)
        or {(edge.get("source"), edge.get("target")) for edge in edges} != expected_edges
    ):
        raise ValueError("Recovery fixture graph has unexpected dependency edges")


COMPLEX_EDGES = frozenset(
    {
        ("0.0", "0.1.g0.0"),
        ("0.0", "0.1.g1.0"),
        ("0.1.g0.0", "0.1.g0.1"),
        ("0.1.g0.1", "0.1.g0.2"),
        ("0.1.g1.0", "0.1.g1.1"),
        ("0.1.g1.1", "0.1.g1.2"),
        ("0.1.g0.2", "0.2"),
        ("0.1.g1.2", "0.2"),
    }
)
COMPLEX_NODES = frozenset(node for edge in COMPLEX_EDGES for node in edge)


def verify_complex_graph(graph: Mapping[str, Any], *, failed: bool) -> None:
    """Check the fixed split/join shape, allowing scheduling variation on failure."""
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("Complex fixture graph has no node or edge list")
    states = {node["id"]: node["state"] for node in nodes}
    actual_edges = {(edge["source"], edge["target"]) for edge in edges}
    if len(states) != len(nodes) or len(actual_edges) != len(edges):
        raise ValueError("Complex fixture graph contains duplicate records")
    if not failed:
        if states != dict.fromkeys(COMPLEX_NODES, "SUCCEEDED") or actual_edges != COMPLEX_EDGES:
            raise ValueError("Complex fixture success changed its split/join graph")
        return
    if (
        not states.keys() <= COMPLEX_NODES
        or "0.2" in states
        or "0.1.g1.2" in states
        or states.get("0.0") != "SUCCEEDED"
        or states.get("0.1.g1.0") != "SUCCEEDED"
        or states.get("0.1.g1.1") != "FAILED"
        or any(
            state not in {"PENDING", "RUNNING", "SUCCEEDED"}
            for node, state in states.items()
            if node != "0.1.g1.1"
        )
        or actual_edges
        != frozenset((a, b) for a, b in COMPLEX_EDGES if a in states and b in states)
    ):
        raise ValueError("Complex fixture failure changed its observed branch boundary")
