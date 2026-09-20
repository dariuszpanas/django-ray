"""Verify retained API detail when a fixed workflow exceeds Admin's node limit."""

import json
from typing import Any
from uuid import UUID

from qualification.application.run_api import ApplicationHttp
from qualification.application.workflow_envelopes import validate_workflow_envelope
from qualification.application.workflow_http import decode_workflow_object
from qualification.application.workflow_pagination import read_display_limit_pages


def read_admin_display_limit(
    request: ApplicationHttp,
    *,
    task_id: str,
    execution_pk: int,
    run_identity: dict[str, Any],
    token: str,
    admin_cookie: str,
) -> dict[str, Any]:
    """Require complete serial API detail and an empty, explained Admin graph."""
    if str(UUID(task_id)) != task_id or type(execution_pk) is not int or execution_pk < 1:
        raise ValueError("Display-limit observation requires valid task identities")
    attempt = run_identity.get("attempt_number")
    if type(attempt) is not int or attempt < 1 or not token or not admin_cookie:
        raise ValueError("Display-limit observation requires an attempt and credentials")
    status, body = request(
        f"/api/cluster/workflows/{task_id}?attempt_number={attempt}",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
        response_limit=256 * 1024,
        required_response_headers={"X-Content-Type-Options": "nosniff"},
    )
    if status != 200:
        raise ValueError("Display-limit summary returned a non-success HTTP status")
    summary = decode_workflow_object(body)
    _, publication = validate_workflow_envelope(
        summary,
        task_id=task_id,
        endpoint="workflow summary",
        schema="django-ray.workflow-progress-summary",
        expected_run_identity=run_identity,
    )
    detail = summary.get("summary")
    if (
        not isinstance(detail, dict)
        or detail.get("state") != "SUCCEEDED"
        or detail.get("reporting_policy") != "full"
    ):
        raise ValueError("Display-limit fixture did not retain successful full reporting")
    for key, expected_counts in (
        (
            "node_counts",
            {
                "discovered": 101,
                "retained_topology": 101,
                "retained_detail": 101,
                "pending": 0,
                "running": 0,
                "succeeded": 101,
                "failed": 0,
            },
        ),
        ("edge_counts", {"discovered": 100, "retained_topology": 100}),
    ):
        counts = detail.get(key)
        if not isinstance(counts, dict) or set(counts) != set(expected_counts) | {"declared"}:
            raise ValueError("Display-limit summary has invalid retained counts")
        if any(
            type(counts[k]) is not int or counts[k] != value for k, value in expected_counts.items()
        ):
            raise ValueError("Display-limit summary disagrees with its fixed workflow")
        declared = counts["declared"]
        if declared is not None and (
            type(declared) is not int or declared != expected_counts["discovered"]
        ):
            raise ValueError("Display-limit summary has inconsistent declared counts")
    pages = {
        collection: read_display_limit_pages(
            request,
            task_id=task_id,
            run_identity=run_identity,
            publication=dict(publication),
            token=token,
            collection=collection,
        )
        for collection in ("topology_nodes", "topology_edges", "node_details")
    }
    expected_nodes = {f"0.{i}" for i in range(101)}
    if (
        {n.get("node_id") for n in pages["topology_nodes"]} != expected_nodes
        or {n.get("node_id"): n.get("state") for n in pages["node_details"]}
        != dict.fromkeys(expected_nodes, "SUCCEEDED")
        or {(e.get("source"), e.get("target")) for e in pages["topology_edges"]}
        != {(f"0.{i}", f"0.{i + 1}") for i in range(100)}
    ):
        raise ValueError("Display-limit API lost or changed executed workflow detail")
    status, body = request(
        f"/admin/django_ray/raytaskexecution/{execution_pk}/workflow/graph/?attempt_number={attempt}",
        method="GET",
        headers={"Cookie": admin_cookie},
        response_limit=128 * 1024,
        required_response_headers={"X-Content-Type-Options": "nosniff"},
        required_cache_directives=frozenset({"no-store"}),
    )
    if status != 200:
        raise ValueError("Display-limit Admin returned a non-success HTTP status")
    graph = decode_workflow_object(body)
    expected = {
        "schema": "django-ray.admin-workflow-graph",
        "schema_version": 2,
        "status": "LIMIT_EXCEEDED",
        "complete": False,
        "counts": {"nodes": 0, "edges": 0},
        "nodes": [],
        "edges": [],
        "limits": {"nodes": 100, "edges": 256, "details": 100, "response_bytes": 128 * 1024},
        "message": "This workflow exceeds Admin's display limits (100 nodes, 256 edges or 128 KiB). "
        "Inspect the paginated workflow API for retained details.",
    }
    if json.dumps(graph, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("Display-limit Admin did not return the bounded, explained limit outcome")
    return {
        "task_id": task_id,
        "run_identity": dict(run_identity),
        "publication": dict(publication),
        "state": "SUCCEEDED",
        "reporting_policy": "full",
        "api_counts": {"nodes": 101, "edges": 100, "details": 101},
        "admin_status": "LIMIT_EXCEEDED",
        "fixture_graph_verified": True,
        "complete_workflow_gate": False,
    }
