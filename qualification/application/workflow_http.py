"""Compare a tiny workflow's API publication with its Admin graph presentation.

The caller supplies independently observed durable identities and an authenticated
Admin session. This read-only step neither enqueues work nor certifies a full gate.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from qualification.application.run_api import ApplicationHttp
from qualification.application.workflow_envelopes import validate_workflow_envelope

COLLECTIONS = {
    "topology_nodes": "topology/nodes",
    "topology_edges": "topology/edges",
    "node_details": "nodes",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Workflow response contains duplicate JSON fields")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("Workflow response contains a non-JSON numeric constant")


def decode_workflow_object(body: bytes | str) -> dict[str, Any]:
    """Decode caller-bounded JSON without duplicate keys or numeric extensions."""
    value = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Workflow observation must be a JSON object")
    return value


def read_full_workflow_graph(
    request: ApplicationHttp,
    *,
    task_id: str,
    execution_pk: int,
    run_identity: dict[str, Any],
    expected_state: str,
    token: str,
    admin_cookie: str,
    reporting_policy: str = "full",
) -> dict[str, Any]:
    """Read attempt-pinned public surfaces and require their graph to agree.

    Returned facts intentionally exclude node labels, results, errors and cookies.
    An enclosing Job must bound the overall deadline and own session cleanup.
    """
    from django_ray.workflow.admin_graph import (
        build_admin_workflow_graph,
        inspect_admin_workflow_graph_summary,
    )

    if str(UUID(task_id)) != task_id:
        raise ValueError("Workflow task identity must be a canonical UUID")
    if type(execution_pk) is not int or execution_pk < 1:
        raise ValueError("Workflow execution primary key must be positive")
    if expected_state not in {"SUCCEEDED", "FAILED"}:
        raise ValueError("Workflow observation requires a terminal outcome")
    if reporting_policy not in {"full", "terminal_only"}:
        raise ValueError("Workflow observation requires a supported reporting policy")
    full = reporting_policy == "full"
    attempt = run_identity.get("attempt_number")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Workflow observation requires a positive attempt")
    if not token or not admin_cookie:
        raise ValueError("Workflow observation requires both API and Admin credentials")

    def read(path: str, *, admin: bool = False) -> dict[str, Any]:
        status, body = request(
            path,
            method="GET",
            headers={"Cookie": admin_cookie} if admin else {"Authorization": f"Bearer {token}"},
            response_limit=128 * 1024 if admin else 256 * 1024,
            required_response_headers={
                "X-Content-Type-Options": "nosniff",
            },
            required_cache_directives=frozenset({"no-store"}),
        )
        if status != 200:
            raise ValueError("Workflow observation returned a non-success HTTP status")
        return decode_workflow_object(body)

    root = f"/api/cluster/workflows/{task_id}"
    summary = read(f"{root}?attempt_number={attempt}")
    _, publication = validate_workflow_envelope(
        summary,
        task_id=task_id,
        endpoint="workflow summary",
        schema="django-ray.workflow-progress-summary",
        expected_run_identity=run_identity,
        expected_availability="AVAILABLE" if full else "OMITTED_BY_POLICY",
        expected_complete=full,
        expect_detail_revisions=full,
    )
    detail = summary.get("summary")
    if (
        not isinstance(detail, dict)
        or detail.get("state") != expected_state
        or detail.get("reporting_policy") != reporting_policy
    ):
        raise ValueError("Workflow summary does not match the expected full-reporting outcome")
    expectation = inspect_admin_workflow_graph_summary(summary) if full else None
    if not full and (
        detail.get("run_identity") != run_identity
        or type(detail.get("schema_version")) is not int
        or detail.get("schema_version") != 3
        or detail.get("summary_revision") != publication["summary_revision"]
        or detail.get("topology_version") is not None
        or detail.get("detail_revision") is not None
    ):
        raise ValueError("Terminal-only summary has inconsistent identity or detail revisions")
    if not full:
        for key, fields in (
            (
                "node_counts",
                {
                    "discovered",
                    "retained_topology",
                    "retained_detail",
                    "pending",
                    "running",
                    "succeeded",
                    "failed",
                },
            ),
            ("edge_counts", {"discovered", "retained_topology"}),
        ):
            counts = detail.get(key)
            if (
                not isinstance(counts, dict)
                or set(counts) != fields | {"declared"}
                or type(counts["declared"]) is not int
                or counts["declared"] < 1
                or any(type(counts[field]) is not int or counts[field] != 0 for field in fields)
            ):
                raise ValueError("Terminal-only summary claimed observed graph detail")
        if (
            detail.get("storage") != {"kind": "database", "manifest_id": None}
            or detail.get("detail")
            != {"availability": "OMITTED_BY_POLICY", "complete": False, "truncation_reasons": []}
            or publication["summary_revision"] != 1
        ):
            raise ValueError("Terminal-only summary claimed graph storage or repeated publication")
    pages = {}
    for collection, suffix in COLLECTIONS.items():
        page = read(f"{root}/{suffix}?limit=100&attempt_number={attempt}")
        validate_workflow_envelope(
            page,
            task_id=task_id,
            endpoint=collection,
            schema="django-ray.workflow-progress-page",
            expected_run_identity=run_identity,
            expected_publication=publication,
            expected_availability="AVAILABLE" if full else "OMITTED_BY_POLICY",
            expected_complete=full,
            expect_detail_revisions=full,
        )
        if not full and (
            page.get("collection") != collection
            or type(page.get("returned_count")) is not int
            or page.get("returned_count") != 0
            or page.get("items") != []
            or page.get("next_cursor") is not None
        ):
            raise ValueError("Terminal-only API exposed retained detail")
        pages[collection] = page
    expected_graph = (
        build_admin_workflow_graph(expectation, **pages) if expectation is not None else None
    )
    graph = read(
        f"/admin/django_ray/raytaskexecution/{execution_pk}/workflow/graph/"
        f"?attempt_number={attempt}",
        admin=True,
    )
    if full and graph != expected_graph:
        raise ValueError("Authenticated Admin graph differs from the pinned API publication")
    if not full and (
        graph.get("schema") != "django-ray.admin-workflow-graph"
        or type(graph.get("schema_version")) is not int
        or graph.get("schema_version") != 2
        or graph.get("status") != "UNAVAILABLE"
        or graph.get("complete") is not False
        or graph.get("nodes") != []
        or graph.get("edges") != []
        or graph.get("counts") != {"nodes": 0, "edges": 0}
    ):
        raise ValueError("Terminal-only Admin exposed a graph")
    return {
        "task_id": task_id,
        "run_identity": dict(run_identity),
        "publication": dict(publication),
        "state": expected_state,
        "reporting_policy": reporting_policy,
        "counts": graph["counts"],
        "api_admin_graph_match": True,
        "complete_workflow_gate": False,
    }
