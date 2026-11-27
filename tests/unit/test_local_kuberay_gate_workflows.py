"""Unit tests for local KubeRay workflow, showcase, and recovery gates."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import urlencode

import pytest

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    EXPECTED_POLL_ATTEMPT_ERROR_MAX_BYTES,
    LocalKubeRayGate,
)
from tests.unit._local_kuberay_gate_helpers import (
    FAILED_WORKFLOW_RUN_ID,
    FAILED_WORKFLOW_SHOWCASE_RUN_ID,
    FAILED_WORKFLOW_SHOWCASE_TASK_ID,
    FAILED_WORKFLOW_TASK_ID,
    TERMINAL_ONLY_FAILED_WORKFLOW_RUN_ID,
    TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
    TERMINAL_ONLY_WORKFLOW_RUN_ID,
    TERMINAL_ONLY_WORKFLOW_TASK_ID,
    WORKFLOW_RECOVERY_RUN_IDS,
    WORKFLOW_RECOVERY_TASK_ID,
    WORKFLOW_RUN_ID,
    WORKFLOW_SHOWCASE_RUN_ID,
    WORKFLOW_SHOWCASE_TASK_ID,
    WORKFLOW_TASK_ID,
    _bounded_poll_metadata,
    _config,
)


def _complex_workflow_gate_responses() -> dict[str, dict[str, Any]]:
    page_query = f"?limit={gate_module.WORKFLOW_PROGRESS_PAGE_LIMIT}"

    def run_responses(
        *,
        enqueue_path: str,
        enqueue_kwargs: dict[str, object],
        task_id: str,
        run_id: str,
        state: str,
        node_states: dict[str, str],
        edges: list[dict[str, str]],
        leaf_tasks: int,
    ) -> dict[str, dict[str, Any]]:
        run_identity = {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_number": 1,
            "execution_generation": 1,
        }
        publication = {
            "summary_revision": 9,
            "topology_version": 8,
            "detail_revision": 1,
        }

        def envelope() -> dict[str, Any]:
            return {
                "schema_version": 1,
                "task_id": task_id,
                "run_identity": dict(run_identity),
                "publication": dict(publication),
                "availability": "AVAILABLE",
                "complete": True,
            }

        def page(collection: str, items: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                **envelope(),
                "schema": "django-ray.workflow-progress-page",
                "collection": collection,
                "returned_count": len(items),
                "items": items,
                "next_cursor": None,
            }

        state_counts = {
            node_state: sum(value == node_state for value in node_states.values())
            for node_state in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
        }
        summary = {
            "schema_version": 3,
            "run_identity": dict(run_identity),
            "reporting_policy": "full",
            "selected_strategy": "dynamic_tasks",
            "plan_fingerprint": f"sha256:{'a' * 64}",
            **publication,
            "state": state,
            "node_counts": {
                "declared": None,
                "discovered": len(node_states),
                "retained_topology": len(node_states),
                "retained_detail": len(node_states),
                "pending": state_counts["PENDING"],
                "running": state_counts["RUNNING"],
                "succeeded": state_counts["SUCCEEDED"],
                "failed": state_counts["FAILED"],
            },
            "edge_counts": {
                "declared": None,
                "discovered": len(edges),
                "retained_topology": len(edges),
            },
            "detail": {
                "availability": "AVAILABLE",
                "complete": True,
                "truncation_reasons": [],
            },
        }
        poll_result: dict[str, Any] | None = None
        poll_error: str | None = gate_module.COMPLEX_WORKFLOW_FAILURE_MESSAGE
        if state == "SUCCEEDED":
            poll_result = {
                "shape": "chain(group(chain(map), chain(map)), step)",
                "durability_boundary": "single RayTaskExecution",
                "total_leaf_tasks": leaf_tasks,
            }
            poll_error = None
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        return {
            enqueue_path: {
                "task_id": task_id,
                "status": "READY",
                "args": [],
                "kwargs": dict(enqueue_kwargs),
            },
            f"/api/cluster/complex-workflow/{task_id}": {
                "task_id": task_id,
                "state": state,
                "result": poll_result,
                "error": poll_error,
                **_bounded_poll_metadata(),
            },
            f"/api/executions?{execution_query}": {
                "tasks": [
                    {
                        "task_id": task_id,
                        "state": state,
                        "callable_path": (
                            "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
                        ),
                        "attempt_number": 1,
                        "execution_generation": 1,
                        "workflow_run_id": run_id,
                        "error_message": poll_error,
                    }
                ]
            },
            f"/api/cluster/workflows/{task_id}": {
                **envelope(),
                "schema": "django-ray.workflow-progress-summary",
                "source_schema_version": 3,
                "summary": summary,
            },
            f"/api/cluster/workflows/{task_id}/topology/nodes{page_query}": page(
                "topology_nodes",
                [{"node_id": node_id, "kind": "step"} for node_id in node_states],
            ),
            f"/api/cluster/workflows/{task_id}/topology/edges{page_query}": page(
                "topology_edges",
                edges,
            ),
            f"/api/cluster/workflows/{task_id}/nodes{page_query}": page(
                "node_details",
                [
                    {"node_id": node_id, "state": node_state}
                    for node_id, node_state in node_states.items()
                ],
            ),
        }

    def terminal_only_responses(
        *,
        enqueue_path: str,
        enqueue_kwargs: dict[str, object],
        task_id: str,
        run_id: str,
        state: str,
    ) -> dict[str, dict[str, Any]]:
        run_identity = {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_number": 1,
            "execution_generation": 1,
        }
        publication = {
            "summary_revision": 1,
            "topology_version": None,
            "detail_revision": None,
        }

        def envelope() -> dict[str, Any]:
            return {
                "schema_version": 1,
                "task_id": task_id,
                "run_identity": dict(run_identity),
                "publication": dict(publication),
                "availability": "OMITTED_BY_POLICY",
                "complete": False,
            }

        def empty_page(collection: str) -> dict[str, Any]:
            return {
                **envelope(),
                "schema": "django-ray.workflow-progress-page",
                "collection": collection,
                "returned_count": 0,
                "items": [],
                "next_cursor": None,
            }

        finished_at = "2026-07-29T12:00:02Z"
        summary = {
            "schema_version": 3,
            "storage_protocol_version": 1,
            "run_identity": dict(run_identity),
            "reporting_policy": "terminal_only",
            "selected_strategy": "dynamic_tasks",
            "plan_fingerprint": f"sha256:{'a' * 64}",
            "limits_profile": "v1",
            **publication,
            "state": state,
            "node_counts": {
                "declared": 13,
                "discovered": 0,
                "retained_topology": 0,
                "retained_detail": 0,
                "pending": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
            },
            "edge_counts": {
                "declared": 13,
                "discovered": 0,
                "retained_topology": 0,
            },
            "progress_percent": 100.0 if state == "SUCCEEDED" else 0.0,
            "timestamps": {
                "started_at": "2026-07-29T12:00:00Z",
                "updated_at": finished_at,
                "finished_at": finished_at,
            },
            "detail": {
                "availability": "OMITTED_BY_POLICY",
                "complete": False,
                "truncation_reasons": [],
            },
            "storage": {"kind": "database", "manifest_id": None},
            "retention": {"detail_days": 7, "detail_expires_at": None},
            "terminal": {"outcome": state, "finished_at": finished_at},
        }
        poll_result: dict[str, Any] | None = None
        poll_error: str | None = gate_module.COMPLEX_WORKFLOW_FAILURE_MESSAGE
        if state == "SUCCEEDED":
            poll_result = {
                "shape": "chain(group(chain(map), chain(map)), step)",
                "durability_boundary": "single RayTaskExecution",
                "total_leaf_tasks": 3,
            }
            poll_error = None
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        return {
            enqueue_path: {
                "task_id": task_id,
                "status": "READY",
                "args": [],
                "kwargs": dict(enqueue_kwargs),
            },
            f"/api/cluster/complex-workflow/{task_id}": {
                "task_id": task_id,
                "state": state,
                "result": poll_result,
                "error": poll_error,
                **_bounded_poll_metadata(),
            },
            f"/api/executions?{execution_query}": {
                "tasks": [
                    {
                        "task_id": task_id,
                        "state": state,
                        "callable_path": (
                            "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
                        ),
                        "attempt_number": 1,
                        "execution_generation": 1,
                        "workflow_run_id": run_id,
                        "error_message": poll_error,
                    }
                ]
            },
            f"/api/cluster/workflows/{task_id}": {
                **envelope(),
                "schema": "django-ray.workflow-progress-summary",
                "source_schema_version": 3,
                "summary": summary,
            },
            f"/api/cluster/workflows/{task_id}/topology/nodes{page_query}": (
                empty_page("topology_nodes")
            ),
            f"/api/cluster/workflows/{task_id}/topology/edges{page_query}": (
                empty_page("topology_edges")
            ),
            f"/api/cluster/workflows/{task_id}/nodes{page_query}": empty_page("node_details"),
        }

    success_nodes = {
        "0.0": "SUCCEEDED",
        "0.1.g0.0": "SUCCEEDED",
        "0.2": "SUCCEEDED",
    }
    failure_nodes = {
        "0.0": "SUCCEEDED",
        "0.1.g0.0": "SUCCEEDED",
        "0.1.g0.1": "FAILED",
        "0.1.g1.0": "RUNNING",
        "0.2": "PENDING",
    }
    responses = run_responses(
        enqueue_path=gate_module.COMPLEX_WORKFLOW_ENQUEUE_PATH,
        enqueue_kwargs=gate_module.COMPLEX_WORKFLOW_ENQUEUE_KWARGS,
        task_id=WORKFLOW_TASK_ID,
        run_id=WORKFLOW_RUN_ID,
        state="SUCCEEDED",
        node_states=success_nodes,
        edges=[
            {"source": "0.0", "target": "0.1.g0.0"},
            {"source": "0.1.g0.0", "target": "0.2"},
        ],
        leaf_tasks=3,
    )
    responses.update(
        run_responses(
            enqueue_path=gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH,
            enqueue_kwargs=gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_KWARGS,
            task_id=FAILED_WORKFLOW_TASK_ID,
            run_id=FAILED_WORKFLOW_RUN_ID,
            state="FAILED",
            node_states=failure_nodes,
            edges=[
                {"source": "0.0", "target": "0.1.g0.0"},
                {"source": "0.0", "target": "0.1.g0.1"},
                {"source": "0.0", "target": "0.1.g1.0"},
                {"source": "0.1.g0.0", "target": "0.2"},
                {"source": "0.1.g0.1", "target": "0.2"},
                {"source": "0.1.g1.0", "target": "0.2"},
            ],
            leaf_tasks=3,
        )
    )
    responses.update(
        terminal_only_responses(
            enqueue_path=gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH,
            enqueue_kwargs=gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_KWARGS,
            task_id=TERMINAL_ONLY_WORKFLOW_TASK_ID,
            run_id=TERMINAL_ONLY_WORKFLOW_RUN_ID,
            state="SUCCEEDED",
        )
    )
    responses.update(
        terminal_only_responses(
            enqueue_path=(gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_PATH),
            enqueue_kwargs=(gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_KWARGS),
            task_id=TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
            run_id=TERMINAL_ONLY_FAILED_WORKFLOW_RUN_ID,
            state="FAILED",
        )
    )
    return responses


def test_complex_workflow_gate_requires_terminal_consistent_schema_v3_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _complex_workflow_gate_responses()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(gate, "_secret_token", lambda: token)
    demo_token = "distinct-demo-token-that-is-not-an-operator-123456"
    monkeypatch.setattr(
        gate,
        "_component_secret_data",
        lambda name: {
            "DJANGO_DEMO_TOKEN": base64.b64encode(demo_token.encode()).decode(),
        },
    )

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        expected_token = demo_token if method == "POST" else token
        assert headers == {"Authorization": f"Bearer {expected_token}"}
        calls.append((path, method))
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    gate._verify_complex_workflow_progress()

    def run_calls(enqueue_path: str, task_id: str) -> list[tuple[str, str]]:
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        return [
            (enqueue_path, "POST"),
            (f"/api/cluster/complex-workflow/{task_id}", "GET"),
            (f"/api/executions?{execution_query}", "GET"),
            (f"/api/cluster/workflows/{task_id}", "GET"),
            (
                f"/api/cluster/workflows/{task_id}/topology/nodes?limit=16",
                "GET",
            ),
            (
                f"/api/cluster/workflows/{task_id}/topology/edges?limit=16",
                "GET",
            ),
            (f"/api/cluster/workflows/{task_id}/nodes?limit=16", "GET"),
        ]

    assert calls == run_calls(
        gate_module.COMPLEX_WORKFLOW_ENQUEUE_PATH,
        WORKFLOW_TASK_ID,
    ) + run_calls(
        gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH,
        FAILED_WORKFLOW_TASK_ID,
    ) + run_calls(
        gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH,
        TERMINAL_ONLY_WORKFLOW_TASK_ID,
    ) + run_calls(
        gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_FAILURE_ENQUEUE_PATH,
        TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
    )
    assert gate.evidence.workflow_task_id == WORKFLOW_TASK_ID
    assert gate.evidence.workflow_task_state == "SUCCEEDED"
    assert gate.evidence.workflow_attempt_number == 1
    assert gate.evidence.workflow_schema_version == 3
    assert gate.evidence.workflow_availability == "AVAILABLE"
    assert gate.evidence.workflow_topology_nodes == 3
    assert gate.evidence.workflow_topology_edges == 2
    assert gate.evidence.workflow_node_details == 3
    assert gate.evidence.workflow_leaf_tasks == 3
    assert gate.evidence.workflow_failure_task_id == FAILED_WORKFLOW_TASK_ID
    assert gate.evidence.workflow_failure_task_state == "FAILED"
    assert gate.evidence.workflow_failure_attempt_number == 1
    assert gate.evidence.workflow_failure_schema_version == 3
    assert gate.evidence.workflow_failure_availability == "AVAILABLE"
    assert gate.evidence.workflow_failure_topology_nodes == 5
    assert gate.evidence.workflow_failure_topology_edges == 6
    assert gate.evidence.workflow_failure_node_details == 5
    assert gate.evidence.workflow_failure_leaf_tasks == 3
    assert gate.evidence.workflow_failure_pending_nodes == 1
    assert gate.evidence.workflow_failure_running_nodes == 1
    assert gate.evidence.workflow_failure_succeeded_nodes == 2
    assert gate.evidence.workflow_failure_failed_nodes == 1
    assert gate.evidence.workflow_terminal_only_task_id == TERMINAL_ONLY_WORKFLOW_TASK_ID
    assert gate.evidence.workflow_terminal_only_task_state == "SUCCEEDED"
    assert gate.evidence.workflow_terminal_only_attempt_number == 1
    assert gate.evidence.workflow_terminal_only_schema_version == 3
    assert gate.evidence.workflow_terminal_only_summary_revision == 1
    assert gate.evidence.workflow_terminal_only_declared_nodes == 13
    assert gate.evidence.workflow_terminal_only_declared_edges == 13
    assert (
        gate.evidence.workflow_terminal_only_failure_task_id
        == TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    )
    assert gate.evidence.workflow_terminal_only_failure_task_state == "FAILED"
    assert gate.evidence.workflow_terminal_only_failure_attempt_number == 1
    assert gate.evidence.workflow_terminal_only_failure_schema_version == 3
    assert gate.evidence.workflow_terminal_only_failure_summary_revision == 1
    assert gate.evidence.workflow_terminal_only_failure_declared_nodes == 13
    assert gate.evidence.workflow_terminal_only_failure_declared_edges == 13


@pytest.mark.parametrize(
    "failure",
    [
        "terminal_failure",
        "legacy_summary",
        "not_available",
        "empty_nodes",
        "unknown_edge_node",
        "missing_detail",
        "publication_mismatch",
        "count_mismatch",
        "failed_error",
        "failed_retry_attempt",
        "missing_failed_enqueue_kwargs",
        "forged_failed_enqueue_kwargs",
    ],
)
def test_complex_workflow_gate_rejects_incomplete_or_inconsistent_api_evidence(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _complex_workflow_gate_responses()
    summary_path = f"/api/cluster/workflows/{WORKFLOW_TASK_ID}"
    page_query = f"?limit={gate_module.WORKFLOW_PROGRESS_PAGE_LIMIT}"
    node_path = f"{summary_path}/topology/nodes{page_query}"
    edge_path = f"{summary_path}/topology/edges{page_query}"
    detail_path = f"{summary_path}/nodes{page_query}"
    poll_path = f"/api/cluster/complex-workflow/{WORKFLOW_TASK_ID}"
    failed_poll_path = f"/api/cluster/complex-workflow/{FAILED_WORKFLOW_TASK_ID}"
    failed_execution_path = (
        f"/api/executions?{urlencode({'task_id': FAILED_WORKFLOW_TASK_ID, 'limit': 1})}"
    )
    failed_enqueue_path = gate_module.COMPLEX_WORKFLOW_FAILURE_ENQUEUE_PATH

    if failure == "terminal_failure":
        responses[poll_path]["state"] = "FAILED"
        responses[poll_path]["result"] = None
    elif failure == "legacy_summary":
        responses[summary_path]["source_schema_version"] = 2
    elif failure == "not_available":
        responses[summary_path]["availability"] = "NOT_REPORTED"
        responses[summary_path]["complete"] = False
    elif failure == "empty_nodes":
        responses[node_path]["items"] = []
        responses[node_path]["returned_count"] = 0
    elif failure == "unknown_edge_node":
        responses[edge_path]["items"][0]["target"] = "missing-node"
    elif failure == "missing_detail":
        responses[detail_path]["items"].pop()
        responses[detail_path]["returned_count"] -= 1
    elif failure == "publication_mismatch":
        responses[edge_path]["publication"]["summary_revision"] += 1
    elif failure == "count_mismatch":
        responses[summary_path]["summary"]["node_counts"]["discovered"] -= 1
    elif failure == "failed_error":
        responses[failed_poll_path]["error"] = "unexpected failure"
    elif failure == "failed_retry_attempt":
        responses[failed_execution_path]["tasks"][0]["attempt_number"] = 2
    elif failure == "missing_failed_enqueue_kwargs":
        responses[failed_enqueue_path].pop("kwargs")
    elif failure == "forged_failed_enqueue_kwargs":
        responses[failed_enqueue_path]["kwargs"]["slow_items"] = 2

    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError):
        gate._verify_complex_workflow_progress()

    assert gate.evidence.workflow_task_id == ""
    assert gate.evidence.workflow_topology_nodes == 0


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_policy",
        "extra_summary_revision",
        "topology_revision",
        "discovered_nodes",
        "retained_page",
        "declared_count_mismatch",
        "failed_retry_attempt",
        "forged_enqueue_policy",
    ],
)
def test_terminal_only_workflow_gate_rejects_detail_or_inconsistent_summary(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _complex_workflow_gate_responses()
    task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    summary_path = f"/api/cluster/workflows/{task_id}"
    page_query = f"?limit={gate_module.WORKFLOW_PROGRESS_PAGE_LIMIT}"
    node_path = f"{summary_path}/topology/nodes{page_query}"
    execution_path = f"/api/executions?{urlencode({'task_id': task_id, 'limit': 1})}"
    enqueue_path = gate_module.COMPLEX_WORKFLOW_TERMINAL_ONLY_ENQUEUE_PATH

    if failure == "wrong_policy":
        responses[summary_path]["summary"]["reporting_policy"] = "full"
    elif failure == "extra_summary_revision":
        responses[summary_path]["summary"]["summary_revision"] = 2
        responses[summary_path]["publication"]["summary_revision"] = 2
    elif failure == "topology_revision":
        responses[summary_path]["summary"]["topology_version"] = 1
        responses[summary_path]["publication"]["topology_version"] = 1
    elif failure == "discovered_nodes":
        responses[summary_path]["summary"]["node_counts"]["discovered"] = 1
    elif failure == "retained_page":
        responses[node_path]["items"] = [{"node_id": "0.0"}]
        responses[node_path]["returned_count"] = 1
    elif failure == "declared_count_mismatch":
        responses[summary_path]["summary"]["node_counts"]["declared"] = 4
    elif failure == "failed_retry_attempt":
        failed_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
        failed_execution_path = f"/api/executions?{urlencode({'task_id': failed_id, 'limit': 1})}"
        responses[failed_execution_path]["tasks"][0]["attempt_number"] = 2
    elif failure == "forged_enqueue_policy":
        responses[enqueue_path]["kwargs"]["reporting_policy"] = "full"

    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError):
        gate._verify_complex_workflow_progress()

    assert responses[execution_path]["tasks"][0]["attempt_number"] == 1
    assert gate.evidence.workflow_terminal_only_task_id == ""


def _workflow_showcase_gate_responses() -> dict[str, dict[str, Any]]:
    page_query = f"?limit={gate_module.WORKFLOW_SHOWCASE_PAGE_LIMIT}"
    node_ids = sorted(set().union(*gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS))
    all_edges = [
        {"source": source, "target": target}
        for source, target in sorted(gate_module.WORKFLOW_SHOWCASE_EDGES)
    ]

    def run_responses(
        *,
        enqueue_path: str,
        enqueue_kwargs: dict[str, object],
        task_id: str,
        run_id: str,
        state: str,
        node_states: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        node_ids = sorted(node_states)
        edges = [
            edge
            for edge in all_edges
            if edge["source"] in node_states and edge["target"] in node_states
        ]
        run_identity = {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_number": 1,
            "execution_generation": 1,
        }
        publication = {
            "summary_revision": 9,
            "topology_version": 8,
            "detail_revision": 7,
        }

        def envelope() -> dict[str, Any]:
            return {
                "schema_version": 1,
                "task_id": task_id,
                "run_identity": dict(run_identity),
                "publication": dict(publication),
                "availability": "AVAILABLE",
                "complete": True,
            }

        def page(collection: str, items: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                **envelope(),
                "schema": "django-ray.workflow-progress-page",
                "collection": collection,
                "returned_count": len(items),
                "items": items,
                "next_cursor": None,
            }

        node_details = [
            {
                "node_id": node_id,
                "state": node_states[node_id],
                "label": node_id,
            }
            for node_id in node_ids
        ]
        details_by_node = {detail["node_id"]: detail for detail in node_details}
        details_by_node[gate_module.WORKFLOW_SHOWCASE_VALIDATION_NODE_ID]["output_preview"] = {
            "schema_version": 1,
            "availability": "NOT_REQUESTED",
            "value": None,
        }
        details_by_node[gate_module.WORKFLOW_SHOWCASE_PROJECTOR_FAILURE_NODE_ID][
            "output_preview"
        ] = {
            "schema_version": 1,
            "availability": "FAILED",
            "value": None,
        }
        details_by_node[gate_module.WORKFLOW_SHOWCASE_FAILURE_NODE_ID]["output_preview"] = {
            "schema_version": 1,
            "availability": "NOT_REQUESTED" if state == "SUCCEEDED" else "UNAVAILABLE",
            "value": None,
        }
        details_by_node[gate_module.WORKFLOW_SHOWCASE_INPUT_PREVIEW_NODE_ID]["output_preview"] = {
            "schema_version": 1,
            "availability": "AVAILABLE",
            "value": {"collection_size": 1},
        }
        state_counts = {
            node_state: sum(value == node_state for value in node_states.values())
            for node_state in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
        }
        summary = {
            "schema_version": 3,
            "run_identity": dict(run_identity),
            "reporting_policy": "full",
            "selected_strategy": "dynamic_tasks",
            "plan_fingerprint": f"sha256:{'a' * 64}",
            **publication,
            "state": state,
            "node_counts": {
                "declared": 31,
                "discovered": len(node_ids),
                "retained_topology": len(node_ids),
                "retained_detail": len(node_ids),
                "pending": state_counts["PENDING"],
                "running": state_counts["RUNNING"],
                "succeeded": state_counts["SUCCEEDED"],
                "failed": state_counts["FAILED"],
            },
            "edge_counts": {
                "declared": 38,
                "discovered": len(edges),
                "retained_topology": len(edges),
            },
            "detail": {
                "availability": "AVAILABLE",
                "complete": True,
                "truncation_reasons": [],
            },
        }
        poll_result: dict[str, Any] | None = None
        poll_error: str | None = gate_module.WORKFLOW_SHOWCASE_FAILURE_MESSAGE
        if state == "SUCCEEDED":
            poll_result = json.loads(json.dumps(gate_module.WORKFLOW_SHOWCASE_SUCCESS_RESULT))
            poll_error = None
        execution_query = urlencode({"task_id": task_id, "limit": 1})
        responses = {
            enqueue_path: {
                "task_id": task_id,
                "status": "READY",
                "args": [],
                "kwargs": dict(enqueue_kwargs),
            },
            f"/api/cluster/workflow-showcase/{task_id}": {
                "task_id": task_id,
                "state": state,
                "result": poll_result,
                "error": poll_error,
                **_bounded_poll_metadata(),
            },
            f"/api/executions?{execution_query}": {
                "tasks": [
                    {
                        "task_id": task_id,
                        "state": state,
                        "callable_path": gate_module.WORKFLOW_SHOWCASE_CALLABLE,
                        "attempt_number": 1,
                        "execution_generation": 1,
                        "workflow_run_id": run_id,
                        "error_message": poll_error,
                    }
                ]
            },
            f"/api/cluster/workflows/{task_id}": {
                **envelope(),
                "schema": "django-ray.workflow-progress-summary",
                "source_schema_version": 3,
                "summary": summary,
            },
            f"/api/cluster/workflows/{task_id}/topology/nodes{page_query}": page(
                "topology_nodes",
                [{"node_id": node_id, "kind": "step"} for node_id in node_ids],
            ),
            f"/api/cluster/workflows/{task_id}/topology/edges{page_query}": page(
                "topology_edges",
                edges,
            ),
            f"/api/cluster/workflows/{task_id}/nodes{page_query}": page(
                "node_details",
                node_details,
            ),
        }
        for detail in node_details:
            query = urlencode(
                {
                    "node_id": detail["node_id"],
                    "attempt_number": 1,
                }
            )
            responses[f"/api/cluster/workflows/{task_id}/node-detail?{query}"] = {
                **envelope(),
                "schema": "django-ray.workflow-progress-node",
                "found": True,
                "item": dict(detail),
            }
        return responses

    success_states = dict.fromkeys(node_ids, "SUCCEEDED")
    failure_states = dict.fromkeys(gate_module.WORKFLOW_SHOWCASE_FAILURE_NODE_IDS, "SUCCEEDED")
    failure_states[gate_module.WORKFLOW_SHOWCASE_FAILURE_NODE_ID] = "FAILED"

    responses = run_responses(
        enqueue_path=gate_module.WORKFLOW_SHOWCASE_ENQUEUE_PATH,
        enqueue_kwargs=gate_module.WORKFLOW_SHOWCASE_ENQUEUE_KWARGS,
        task_id=WORKFLOW_SHOWCASE_TASK_ID,
        run_id=WORKFLOW_SHOWCASE_RUN_ID,
        state="SUCCEEDED",
        node_states=success_states,
    )
    responses.update(
        run_responses(
            enqueue_path=gate_module.WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_PATH,
            enqueue_kwargs=gate_module.WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_KWARGS,
            task_id=FAILED_WORKFLOW_SHOWCASE_TASK_ID,
            run_id=FAILED_WORKFLOW_SHOWCASE_RUN_ID,
            state="FAILED",
            node_states=failure_states,
        )
    )
    return responses


def test_workflow_showcase_gate_requires_layered_success_and_isolated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _workflow_showcase_gate_responses()
    indexed_paths: set[str] = set()
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert headers == {"Authorization": f"Bearer {token}"}
        assert method in {"GET", "POST"}
        if "/node-detail?" in path:
            indexed_paths.add(path)
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    gate._verify_workflow_showcase_progress()

    assert len(set().union(*gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS)) == 21
    assert len(gate_module.WORKFLOW_SHOWCASE_EDGES) == 28
    assert len(gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS) == 12
    assert len(indexed_paths) == 37
    assert gate.evidence.workflow_showcase_task_id == WORKFLOW_SHOWCASE_TASK_ID
    assert gate.evidence.workflow_showcase_task_state == "SUCCEEDED"
    assert gate.evidence.workflow_showcase_attempt_number == 1
    assert gate.evidence.workflow_showcase_topology_nodes == 21
    assert gate.evidence.workflow_showcase_topology_edges == 28
    assert gate.evidence.workflow_showcase_longest_path_layers == 12
    assert gate.evidence.workflow_showcase_detail_links == 21
    assert gate.evidence.workflow_showcase_failure_task_id == FAILED_WORKFLOW_SHOWCASE_TASK_ID
    assert gate.evidence.workflow_showcase_failure_task_state == "FAILED"
    assert gate.evidence.workflow_showcase_failure_attempt_number == 1
    assert gate.evidence.workflow_showcase_failure_failed_nodes == 1
    assert gate.evidence.workflow_showcase_failure_pending_descendants == 0
    assert gate.evidence.workflow_showcase_failure_running_nodes == 0
    assert gate.evidence.workflow_showcase_failure_succeeded_nodes == 15
    assert gate.evidence.workflow_showcase_failure_path_nodes == 16
    assert gate.evidence.workflow_showcase_failure_detail_links == 16


def test_workflow_progress_layer_includes_compatibility_and_showcase_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    verified: list[str] = []
    monkeypatch.setattr(
        gate,
        "_verify_complex_workflow_progress",
        lambda: verified.append("compatibility"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_workflow_showcase_progress",
        lambda: verified.append("showcase"),
    )
    monkeypatch.setattr(
        gate,
        "_verify_workflow_recovery_progress",
        lambda: verified.append("recovery"),
    )

    gate._verify_workflow_progress()

    assert verified == ["compatibility", "showcase", "recovery"]


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_result",
        "partial_topology",
        "wrong_edge",
        "missing_detail_link",
        "failed_error",
        "failed_retry_attempt",
        "wrong_failed_root",
        "undiscovered_descendant",
        "required_prerequisite_not_succeeded",
        "forged_enqueue_kwargs",
    ],
)
def test_workflow_showcase_gate_rejects_incomplete_or_misleading_graph_evidence(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _workflow_showcase_gate_responses()
    success_summary_path = f"/api/cluster/workflows/{WORKFLOW_SHOWCASE_TASK_ID}"
    failed_summary_path = f"/api/cluster/workflows/{FAILED_WORKFLOW_SHOWCASE_TASK_ID}"
    page_query = f"?limit={gate_module.WORKFLOW_SHOWCASE_PAGE_LIMIT}"
    success_nodes_path = f"{success_summary_path}/topology/nodes{page_query}"
    success_edges_path = f"{success_summary_path}/topology/edges{page_query}"
    failed_details_path = f"{failed_summary_path}/nodes{page_query}"
    success_poll_path = f"/api/cluster/workflow-showcase/{WORKFLOW_SHOWCASE_TASK_ID}"
    failed_poll_path = f"/api/cluster/workflow-showcase/{FAILED_WORKFLOW_SHOWCASE_TASK_ID}"
    failed_execution_path = (
        f"/api/executions?{urlencode({'task_id': FAILED_WORKFLOW_SHOWCASE_TASK_ID, 'limit': 1})}"
    )

    if failure == "wrong_result":
        responses[success_poll_path]["result"]["status"] = "REVIEW"
    elif failure == "partial_topology":
        responses[success_nodes_path]["items"].pop()
        responses[success_nodes_path]["returned_count"] -= 1
    elif failure == "wrong_edge":
        responses[success_edges_path]["items"][0]["target"] = "0.6"
    elif failure == "missing_detail_link":
        node_id = sorted(set().union(*gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS))[0]
        query = urlencode({"node_id": node_id, "attempt_number": 1})
        indexed_path = f"/api/cluster/workflows/{WORKFLOW_SHOWCASE_TASK_ID}/node-detail?{query}"
        responses[indexed_path]["found"] = False
        responses[indexed_path]["item"] = None
    elif failure == "failed_error":
        responses[failed_poll_path]["error"] = "unexpected failure"
    elif failure == "failed_retry_attempt":
        responses[failed_execution_path]["tasks"][0]["attempt_number"] = 2
    elif failure == "wrong_failed_root":
        details = responses[failed_details_path]["items"]
        by_node = {item["node_id"]: item for item in details}
        by_node[gate_module.WORKFLOW_SHOWCASE_FAILURE_NODE_ID]["state"] = "SUCCEEDED"
        by_node["0.3.g1.0.g0"]["state"] = "FAILED"
    elif failure == "undiscovered_descendant":
        details = responses[failed_details_path]["items"]
        descendant = sorted(gate_module.WORKFLOW_SHOWCASE_FAILURE_DESCENDANTS)[0]
        details.append({"node_id": descendant, "state": "RUNNING"})
        responses[failed_details_path]["returned_count"] += 1
    elif failure == "required_prerequisite_not_succeeded":
        details = responses[failed_details_path]["items"]
        by_node = {item["node_id"]: item for item in details}
        by_node["0.3.g1.0.g0"]["state"] = "RUNNING"
    elif failure == "forged_enqueue_kwargs":
        responses[gate_module.WORKFLOW_SHOWCASE_FAILURE_ENQUEUE_PATH]["kwargs"]["failure_item"] = 1

    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError):
        gate._verify_workflow_showcase_progress()

    assert gate.evidence.workflow_showcase_task_id == ""
    assert gate.evidence.workflow_showcase_topology_nodes == 0


def _workflow_recovery_gate_responses() -> dict[str, dict[str, Any]]:
    page_query = lambda attempt: urlencode(  # noqa: E731 - mirrors gate query order
        {
            "limit": gate_module.WORKFLOW_SHOWCASE_PAGE_LIMIT,
            "attempt_number": attempt,
        }
    )
    fingerprint = f"sha256:{'c' * 64}"

    early_states = dict.fromkeys(gate_module.WORKFLOW_RECOVERY_EARLY_NODE_IDS, "PENDING")
    early_states[gate_module.WORKFLOW_RECOVERY_EARLY_FAILURE_NODE_ID] = "FAILED"
    mid_states = dict.fromkeys(gate_module.WORKFLOW_RECOVERY_MID_NODE_IDS, "PENDING")
    mid_states.update(dict.fromkeys(gate_module.WORKFLOW_RECOVERY_MID_SUCCEEDED_NODES, "SUCCEEDED"))
    mid_states[gate_module.WORKFLOW_RECOVERY_MID_FAILURE_NODE_ID] = "FAILED"
    success_node_ids = frozenset().union(*gate_module.WORKFLOW_SHOWCASE_NODE_LAYERS)
    success_states = dict.fromkeys(success_node_ids, "SUCCEEDED")
    attempts = (
        (
            1,
            "FAILED",
            early_states,
            gate_module.WORKFLOW_RECOVERY_EARLY_EDGES,
            gate_module.WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE,
        ),
        (
            2,
            "FAILED",
            mid_states,
            gate_module.WORKFLOW_RECOVERY_MID_EDGES,
            gate_module.WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE,
        ),
        (3, "SUCCEEDED", success_states, gate_module.WORKFLOW_SHOWCASE_EDGES, None),
    )
    responses: dict[str, dict[str, Any]] = {
        gate_module.WORKFLOW_RECOVERY_ENQUEUE_PATH: {
            "task_id": WORKFLOW_RECOVERY_TASK_ID,
            "status": "READY",
            "args": [],
            "kwargs": dict(gate_module.WORKFLOW_RECOVERY_ENQUEUE_KWARGS),
        },
        f"{gate_module.WORKFLOW_RECOVERY_POLL_PATH}/{WORKFLOW_RECOVERY_TASK_ID}": {
            "task_id": WORKFLOW_RECOVERY_TASK_ID,
            "state": "SUCCEEDED",
            "attempt_number": 3,
            "runtime_env_profile": "recovery-showcase",
            "attempts": [
                {
                    "attempt_number": 1,
                    "state": "FAILED",
                    "error": gate_module.WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE,
                    "error_omission_reason": None,
                },
                {
                    "attempt_number": 2,
                    "state": "FAILED",
                    "error": gate_module.WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE,
                    "error_omission_reason": None,
                },
                {
                    "attempt_number": 3,
                    "state": "SUCCEEDED",
                    "error": None,
                    "error_omission_reason": None,
                },
            ],
            "attempt_error_max_bytes": EXPECTED_POLL_ATTEMPT_ERROR_MAX_BYTES,
            "result": json.loads(json.dumps(gate_module.WORKFLOW_RECOVERY_SUCCESS_RESULT)),
            "error": None,
            **_bounded_poll_metadata(),
        },
        ("/api/executions?" + urlencode({"task_id": WORKFLOW_RECOVERY_TASK_ID, "limit": 1})): {
            "tasks": [
                {
                    "task_id": WORKFLOW_RECOVERY_TASK_ID,
                    "state": "SUCCEEDED",
                    "callable_path": gate_module.WORKFLOW_RECOVERY_CALLABLE,
                    "attempt_number": 3,
                    "execution_generation": 3,
                    "workflow_run_id": WORKFLOW_RECOVERY_RUN_IDS[2],
                }
            ]
        },
    }

    for attempt_number, state, node_states, edges, expected_error in attempts:
        run_identity = {
            "schema_version": 1,
            "run_id": WORKFLOW_RECOVERY_RUN_IDS[attempt_number - 1],
            "attempt_number": attempt_number,
            "execution_generation": attempt_number,
        }
        publication = {
            "summary_revision": 9,
            "topology_version": 8,
            "detail_revision": 7,
        }

        def envelope(
            run_identity: dict[str, Any] = run_identity,
            publication: dict[str, int] = publication,
        ) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "task_id": WORKFLOW_RECOVERY_TASK_ID,
                "run_identity": dict(run_identity),
                "publication": dict(publication),
                "availability": "AVAILABLE",
                "complete": True,
            }

        def page(collection: str, items: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                **envelope(),
                "schema": "django-ray.workflow-progress-page",
                "collection": collection,
                "returned_count": len(items),
                "items": items,
                "next_cursor": None,
            }

        node_ids = sorted(node_states)
        topology_edges = [{"source": source, "target": target} for source, target in sorted(edges)]
        node_details = [
            {
                "node_id": node_id,
                "state": node_states[node_id],
                "error": (expected_error if node_states[node_id] == "FAILED" else None),
            }
            for node_id in node_ids
        ]
        state_counts = {
            node_state: sum(value == node_state for value in node_states.values())
            for node_state in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
        }
        summary = {
            "schema_version": 3,
            "run_identity": dict(run_identity),
            "reporting_policy": "full",
            "selected_strategy": "dynamic_tasks",
            "plan_fingerprint": fingerprint,
            **publication,
            "state": state,
            "node_counts": {
                "declared": None,
                "discovered": len(node_ids),
                "retained_topology": len(node_ids),
                "retained_detail": len(node_ids),
                "pending": state_counts["PENDING"],
                "running": state_counts["RUNNING"],
                "succeeded": state_counts["SUCCEEDED"],
                "failed": state_counts["FAILED"],
            },
            "edge_counts": {
                "declared": None,
                "discovered": len(topology_edges),
                "retained_topology": len(topology_edges),
            },
            "detail": {
                "availability": "AVAILABLE",
                "complete": True,
                "truncation_reasons": [],
            },
        }
        attempt_query = urlencode({"attempt_number": attempt_number})
        summary_path = f"/api/cluster/workflows/{WORKFLOW_RECOVERY_TASK_ID}?{attempt_query}"
        responses[summary_path] = {
            **envelope(),
            "schema": "django-ray.workflow-progress-summary",
            "source_schema_version": 3,
            "summary": summary,
        }
        query = page_query(attempt_number)
        root = f"/api/cluster/workflows/{WORKFLOW_RECOVERY_TASK_ID}"
        responses[f"{root}/topology/nodes?{query}"] = page(
            "topology_nodes",
            [{"node_id": node_id, "kind": "step"} for node_id in node_ids],
        )
        responses[f"{root}/topology/edges?{query}"] = page(
            "topology_edges",
            topology_edges,
        )
        responses[f"{root}/nodes?{query}"] = page("node_details", node_details)
        for detail in node_details:
            detail_query = urlencode(
                {
                    "node_id": detail["node_id"],
                    "attempt_number": attempt_number,
                }
            )
            responses[f"{root}/node-detail?{detail_query}"] = {
                **envelope(),
                "schema": "django-ray.workflow-progress-node",
                "found": True,
                "item": dict(detail),
            }
    return responses


def test_workflow_recovery_gate_requires_two_failures_then_current_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _workflow_recovery_gate_responses()
    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    gate._verify_workflow_recovery_progress()

    assert len(gate_module.WORKFLOW_RECOVERY_EARLY_NODE_IDS) == 2
    assert len(gate_module.WORKFLOW_RECOVERY_MID_NODE_IDS) == 15
    assert len(gate_module.WORKFLOW_RECOVERY_MID_SUCCEEDED_NODES) == 7
    assert len(gate_module.WORKFLOW_RECOVERY_MID_PENDING_NODES) == 7
    assert gate.evidence.workflow_recovery_task_id == WORKFLOW_RECOVERY_TASK_ID
    assert gate.evidence.workflow_recovery_task_state == "SUCCEEDED"
    assert gate.evidence.workflow_recovery_attempt_number == 3
    assert gate.evidence.workflow_recovery_attempt_count == 3
    assert gate.evidence.workflow_recovery_distinct_runs is True
    assert gate.evidence.workflow_recovery_early_topology_nodes == 2
    assert gate.evidence.workflow_recovery_early_topology_edges == 1
    assert gate.evidence.workflow_recovery_early_pending_nodes == 1
    assert gate.evidence.workflow_recovery_early_succeeded_nodes == 0
    assert gate.evidence.workflow_recovery_early_failed_nodes == 1
    assert gate.evidence.workflow_recovery_early_detail_links == 2
    assert gate.evidence.workflow_recovery_mid_topology_nodes == 15
    assert gate.evidence.workflow_recovery_mid_topology_edges == len(
        gate_module.WORKFLOW_RECOVERY_MID_EDGES
    )
    assert gate.evidence.workflow_recovery_mid_pending_nodes == 7
    assert gate.evidence.workflow_recovery_mid_succeeded_nodes == 7
    assert gate.evidence.workflow_recovery_mid_failed_nodes == 1
    assert gate.evidence.workflow_recovery_mid_detail_links == 15
    assert gate.evidence.workflow_recovery_success_topology_nodes == 21
    assert gate.evidence.workflow_recovery_success_topology_edges == 28
    assert gate.evidence.workflow_recovery_success_succeeded_nodes == 21
    assert gate.evidence.workflow_recovery_success_detail_links == 21


@pytest.mark.parametrize(
    "failure",
    [
        "fourth_attempt",
        "wrong_result",
        "stale_current_error",
        "early_node_succeeded",
        "mid_prerequisite_pending",
        "plan_drift",
        "duplicate_run",
        "stale_generation",
        "declared_node_underflow",
        "declared_edge_underflow",
    ],
)
def test_workflow_recovery_gate_rejects_unfenced_or_misleading_history(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    gate = LocalKubeRayGate(_config())
    token = "local-token-that-must-never-be-printed-123456"
    responses = _workflow_recovery_gate_responses()
    poll_path = f"{gate_module.WORKFLOW_RECOVERY_POLL_PATH}/{WORKFLOW_RECOVERY_TASK_ID}"
    early_summary_path = (
        f"/api/cluster/workflows/{WORKFLOW_RECOVERY_TASK_ID}?{urlencode({'attempt_number': 1})}"
    )
    mid_summary_path = (
        f"/api/cluster/workflows/{WORKFLOW_RECOVERY_TASK_ID}?{urlencode({'attempt_number': 2})}"
    )
    page_query = urlencode({"limit": gate_module.WORKFLOW_SHOWCASE_PAGE_LIMIT, "attempt_number": 1})
    early_details_path = f"/api/cluster/workflows/{WORKFLOW_RECOVERY_TASK_ID}/nodes?{page_query}"
    mid_page_query = urlencode(
        {"limit": gate_module.WORKFLOW_SHOWCASE_PAGE_LIMIT, "attempt_number": 2}
    )
    mid_details_path = f"/api/cluster/workflows/{WORKFLOW_RECOVERY_TASK_ID}/nodes?{mid_page_query}"

    if failure == "fourth_attempt":
        responses[poll_path]["attempts"].append(
            {"attempt_number": 4, "state": "FAILED", "error": "unexpected"}
        )
    elif failure == "wrong_result":
        responses[poll_path]["result"]["recovery"]["attempt_number"] = 2
    elif failure == "stale_current_error":
        responses[poll_path]["error"] = gate_module.WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE
    elif failure == "early_node_succeeded":
        details = responses[early_details_path]["items"]
        next(
            item
            for item in details
            if item["node_id"] == gate_module.WORKFLOW_RECOVERY_EARLY_FAILURE_NODE_ID
        )["state"] = "SUCCEEDED"
    elif failure == "mid_prerequisite_pending":
        details = responses[mid_details_path]["items"]
        next(
            item
            for item in details
            if item["node_id"] in gate_module.WORKFLOW_RECOVERY_MID_SUCCEEDED_NODES
        )["state"] = "PENDING"
    elif failure == "plan_drift":
        responses[mid_summary_path]["summary"]["plan_fingerprint"] = f"sha256:{'d' * 64}"
    elif failure == "duplicate_run":
        responses[mid_summary_path]["run_identity"]["run_id"] = WORKFLOW_RECOVERY_RUN_IDS[0]
        responses[mid_summary_path]["summary"]["run_identity"]["run_id"] = (
            WORKFLOW_RECOVERY_RUN_IDS[0]
        )
    elif failure == "stale_generation":
        responses[early_summary_path]["run_identity"]["execution_generation"] = 2
        responses[early_summary_path]["summary"]["run_identity"]["execution_generation"] = 2
    elif failure == "declared_node_underflow":
        responses[early_summary_path]["summary"]["node_counts"]["declared"] = 1
    elif failure == "declared_edge_underflow":
        responses[early_summary_path]["summary"]["edge_counts"]["declared"] = 0

    monkeypatch.setattr(gate, "_secret_token", lambda: token)

    def request(
        path: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        assert method in {"GET", "POST"}
        assert headers == {"Authorization": f"Bearer {token}"}
        return 200, json.dumps(responses[path]).encode()

    monkeypatch.setattr(gate, "_http", request)

    with pytest.raises(ValueError):
        gate._verify_workflow_recovery_progress()

    assert gate.evidence.workflow_recovery_task_id == ""
    assert gate.evidence.workflow_recovery_attempt_count == 0
