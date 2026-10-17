"""Independent fixed expectations for the one-attempt reservation failure."""

from collections.abc import Mapping
from typing import Any

from qualification.application.workflow_fixtures import (
    WORKFLOW_SHOWCASE_EDGES,
    WORKFLOW_SHOWCASE_NODE_LAYERS,
)

FAILURE_MESSAGE = "Intentional workflow showcase reserve_inventory failure at item 0"
FAILED_NODE = "0.5"
ABSENT_NODES = frozenset({"0.6", "0.7.g0", "0.7.g1", "0.7.g2", "0.8"})
NODE_IDS = frozenset().union(*WORKFLOW_SHOWCASE_NODE_LAYERS[:9])
EDGES = frozenset((a, b) for a, b in WORKFLOW_SHOWCASE_EDGES if a in NODE_IDS and b in NODE_IDS)
FACTS = (
    "exact_failure_graph",
    "unsubmitted_descendants_absent",
    "fixed_previews",
    "indexed_details",
)


def verify_graph(graph: Mapping[str, Any], details: list[dict]) -> None:
    """Reject coherent but wrong failure origins, fabricated descendants or previews."""
    nodes, edges = graph.get("nodes"), graph.get("edges")
    states = {node: "FAILED" if node == FAILED_NODE else "SUCCEEDED" for node in NODE_IDS}
    if (
        not isinstance(nodes, list)
        or len(nodes) != 16
        or any(not isinstance(node, dict) for node in nodes)
        or {node.get("id"): node.get("state") for node in nodes} != states
        or graph.get("counts") != {"nodes": 16, "edges": 21}
        or not isinstance(edges, list)
        or len(edges) != 21
        or any(not isinstance(edge, dict) for edge in edges)
        or {(edge.get("source"), edge.get("target")) for edge in edges} != EDGES
    ):
        raise ValueError("Showcase failure changed its fixed node/edge boundary")
    by_id = {node["id"]: node for node in nodes}
    if (
        {node["id"] for node in nodes if node.get("kind") == "map"} != {"0.1.g0.1", FAILED_NODE}
        or not isinstance(details, list)
        or len(details) != 16
        or any(not isinstance(detail, dict) for detail in details)
        or {detail.get("node_id"): detail.get("state") for detail in details} != states
    ):
        raise ValueError("Showcase failure lost its maps or retained details")
    expected_previews = {
        "0.4": ("AVAILABLE", {"collection_size": 1}),
        "0.1.g0.1": ("NOT_REQUESTED", None),
        "0.1.g1.0.g1": ("FAILED", None),
        FAILED_NODE: ("UNAVAILABLE", None),
    }
    detail_by_id = {detail["node_id"]: detail for detail in details}
    for node_id, (availability, value) in expected_previews.items():
        expected = {"schema_version": 1, "availability": availability, "value": value}
        if (
            by_id[node_id].get("output_preview") != expected
            or detail_by_id[node_id].get("output_preview") != expected
        ):
            raise ValueError("Showcase failure changed its exact safe preview contract")


def validate_observation(observation: object) -> None:
    """Require complete graph, Admin, browser and one-attempt evidence in receipts."""
    if not isinstance(observation, dict):
        raise ValueError("Missing showcase failure observation")
    fixed = {
        "state": "FAILED",
        "reporting_policy": "full",
        "counts": {"nodes": 16, "edges": 21},
        "api_admin_graph_match": True,
        "fixture_graph_verified": True,
        "complete_workflow_gate": False,
        "showcase_failure": dict.fromkeys(FACTS, True),
    }
    if any(
        type(observation.get(k)) is not type(v) or observation[k] != v for k, v in fixed.items()
    ):
        raise ValueError("Incomplete showcase failure graph receipt")
    facts = observation["showcase_failure"]
    if any(facts[key] is not True for key in FACTS):
        raise ValueError("Showcase failure facts must be strict booleans")
    identity = observation.get("run_identity")
    if (
        not isinstance(identity, dict)
        or type(identity.get("attempt_number")) is not int
        or identity["attempt_number"] != 1
    ):
        raise ValueError("Showcase failure was not the single first attempt")
    admin = observation.get("admin_contract")
    expected_admin = {
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
    if not isinstance(admin, dict) or any(
        type(admin.get(k)) is not type(v) or admin[k] != v for k, v in expected_admin.items()
    ):
        raise ValueError("Showcase failure lacks its exact Admin presentation")
    browser = observation.get("browser_contract")
    expected_browser = {
        "status": "passed",
        "attempts_rendered": 1,
        "policy": "full",
        "javascript_errors": 0,
        "diagnostics_preserved": True,
    }
    if not isinstance(browser, dict) or any(
        type(browser.get(k)) is not type(v) or browser[k] != v for k, v in expected_browser.items()
    ):
        raise ValueError("Showcase failure lacks browser/preservation evidence")
