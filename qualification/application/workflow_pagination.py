"""Bounded attempt-pinned page reads for the fixed Admin display-limit case."""

from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from qualification.application.run_api import ApplicationHttp
from qualification.application.workflow_envelopes import validate_workflow_envelope
from qualification.application.workflow_http import decode_workflow_object


def read_display_limit_pages(
    request: ApplicationHttp,
    *,
    task_id: str,
    run_identity: dict[str, Any],
    publication: dict[str, Any],
    token: str,
    collection: str,
) -> list[dict[str, Any]]:
    """Read at most four bounded pages; never follow a response-provided URL."""
    routes = {
        "topology_nodes": ("topology/nodes", 101),
        "topology_edges": ("topology/edges", 100),
        "node_details": ("nodes", 101),
    }
    if collection not in routes or not token:
        raise ValueError("Display-limit observation requires a known collection and credential")
    if str(UUID(task_id)) != task_id:
        raise ValueError("Display-limit task identity must be canonical")
    attempt = run_identity.get("attempt_number")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Display-limit observation requires a positive attempt")
    suffix, expected_count = routes[collection]
    items: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    cursor = None
    for _ in range(4):
        query: dict[str, int | str] = {"limit": 100, "attempt_number": attempt}
        if cursor is not None:
            query["cursor"] = cursor
        status, body = request(
            f"/api/cluster/workflows/{task_id}/{suffix}?{urlencode(query)}",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
            response_limit=256 * 1024,
            required_response_headers={"X-Content-Type-Options": "nosniff"},
        )
        if status != 200:
            raise ValueError("Display-limit page returned a non-success HTTP status")
        page = decode_workflow_object(body)
        validate_workflow_envelope(
            page,
            task_id=task_id,
            endpoint=collection,
            schema="django-ray.workflow-progress-page",
            expected_run_identity=run_identity,
            expected_publication=publication,
        )
        batch = page.get("items")
        if (
            page.get("collection") != collection
            or not isinstance(batch, list)
            or not 1 <= len(batch) <= 100
            or any(not isinstance(item, dict) for item in batch)
            or type(page.get("returned_count")) is not int
            or page["returned_count"] != len(batch)
            or len(items) + len(batch) > expected_count
        ):
            raise ValueError("Display-limit page returned invalid or excessive items")
        items.extend(batch)
        cursor = page.get("next_cursor")
        if cursor is None:
            if len(items) != expected_count:
                raise ValueError("Display-limit pagination omitted retained records")
            return items
        if (
            not isinstance(cursor, str)
            or not 1 <= len(cursor.encode()) <= 4096
            or cursor in seen_cursors
            or len(items) == expected_count
        ):
            raise ValueError("Display-limit pagination returned an invalid or repeated cursor")
        seen_cursors.add(cursor)
    raise ValueError("Display-limit pagination exceeded its fixed request budget")
