"""Bounded end-to-end smoke for the tracked Docker Compose quickstart."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

_MAX_RESPONSE_BYTES = 1_048_576
_REQUEST_TIMEOUT_SECONDS = 5.0
# One complete smoke page covers the 25-node/36-edge showcase while
# remaining below the Admin graph's package-enforced limits.
_WORKFLOW_PAGE_LIMIT = 64
_WORKFLOW_GRAPH_LIMITS = {
    "nodes": 100,
    "edges": 256,
    "details": 100,
    "response_bytes": 131_072,
}
_WORKFLOW_GRAPH_ROOT_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "status",
        "message",
        "complete",
        "counts",
        "limits",
        "nodes",
        "edges",
    }
)
_WORKFLOW_GRAPH_NODE_FIELDS = frozenset(
    {"id", "label", "kind", "state", "message", "error", "failure_path"}
)
_WORKFLOW_GRAPH_FANOUT_FIELDS = frozenset(
    {
        "submitted_items",
        "completed_items",
        "in_flight_items",
        "input_exhausted",
    }
)
_WORKFLOW_GRAPH_FORBIDDEN_FIELDS = frozenset(
    {
        "task_id",
        "run_identity",
        "publication",
        "callable_path",
        "runtime_env",
        "ray_options",
        "execution",
        "recent_events",
        "traceback",
        "result",
        "metrics",
        "started_at",
        "finished_at",
        "raw",
    }
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_UNFOLD_STYLESHEET_RE = re.compile(
    r"""href=["'](?P<path>/static/unfold/css/styles[^"']*\.css)["']"""
)
_DJANGO_RAY_STYLESHEET_RE = re.compile(
    r"""href=["'](?P<path>/static/testproject/admin[^"']*\.css)["']"""
)
_TASK_LIVE_STYLESHEET_RE = re.compile(
    r"""href=["'](?P<path>/static/django_ray/admin/task_live[^"']*\.css)["']"""
)
_TASK_LIVE_SCRIPT_RE = re.compile(
    r"""src=["'](?P<path>/static/django_ray/admin/task_live[^"']*\.js)["']"""
)
_WORKFLOW_DIAGNOSTICS_SCRIPT_RE = re.compile(
    r"""src=["'](?P<path>/static/django_ray/admin/workflow_diagnostics[^"']*\.js)["']"""
)
_DJANGO_RAY_ICON_RE = re.compile(
    r"""(?:href|src)=["'](?P<path>/static/testproject/django-ray[^"']*\.svg)["']"""
)


class DockerSmokeError(RuntimeError):
    """Report a bounded quickstart contract failure without exposing credentials."""


def _response_json(response: Any) -> dict[str, Any]:
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise DockerSmokeError("HTTP response exceeded the smoke byte limit")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DockerSmokeError("HTTP response was not valid JSON") from error
    if not isinstance(payload, dict):
        raise DockerSmokeError("HTTP response must be a JSON object")
    return payload


def _response_text(response: Any) -> str:
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise DockerSmokeError("HTTP response exceeded the smoke byte limit")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DockerSmokeError("HTTP response was not valid UTF-8") from error


def _request_timeout(deadline: float | None) -> float:
    if deadline is None:
        return _REQUEST_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DockerSmokeError("the shared smoke deadline expired before the admin request")
    return min(_REQUEST_TIMEOUT_SECONDS, remaining)


def _request_text(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
    expected_content_type: str | tuple[str, ...] | None = None,
    deadline: float | None = None,
) -> str:
    if not path.startswith("/") or "://" in path:
        raise DockerSmokeError("admin smoke path must be a local absolute path")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "text/html", **(headers or {})},
        method="GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=_request_timeout(deadline))
    except urllib.error.HTTPError as error:
        response = error
    except (TimeoutError, urllib.error.URLError) as error:
        raise DockerSmokeError(f"request to {path} failed") from error

    with response:
        if response.status != expected_status:
            raise DockerSmokeError(
                f"request to {path} returned HTTP {response.status}; "
                f"expected HTTP {expected_status}"
            )
        if expected_content_type is not None:
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip()
            accepted_content_types = (
                (expected_content_type,)
                if isinstance(expected_content_type, str)
                else expected_content_type
            )
            if content_type not in accepted_content_types:
                expected_label = " or ".join(accepted_content_types)
                raise DockerSmokeError(
                    f"request to {path} returned content type {content_type or 'missing'}; "
                    f"expected {expected_label}"
                )
        body = _response_text(response)
    if deadline is not None and time.monotonic() > deadline:
        raise DockerSmokeError(f"the shared smoke deadline expired during the request to {path}")
    return body


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers=headers,
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as error:
        response = error
    except urllib.error.URLError as error:
        raise DockerSmokeError(f"request to {path} failed") from error

    with response:
        if response.status != expected_status:
            raise DockerSmokeError(
                f"request to {path} returned HTTP {response.status}; "
                f"expected HTTP {expected_status}"
            )
        return _response_json(response)


def _request_admin_json(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str],
    deadline: float,
    expected_status: int = 200,
) -> dict[str, Any]:
    """Read one authenticated, byte-bounded admin JSON response."""

    body = _request_text(
        base_url,
        path,
        headers={"Accept": "application/json", **headers},
        expected_status=expected_status,
        expected_content_type="application/json",
        deadline=deadline,
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise DockerSmokeError(f"admin request to {path} did not return valid JSON") from error
    if not isinstance(payload, dict):
        raise DockerSmokeError(f"admin request to {path} did not return a JSON object")
    return payload


def _wait_until(
    description: str,
    deadline: float,
    probe: Callable[[], bool],
    *,
    interval_seconds: float = 0.5,
) -> None:
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(interval_seconds)
    raise DockerSmokeError(f"timed out waiting for {description}")


def _verify_database_contract() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    if connection.vendor != "postgresql":
        raise DockerSmokeError("quickstart smoke requires the shared PostgreSQL database")
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    if executor.migration_plan(targets):
        raise DockerSmokeError("the one-shot migration service left unapplied migrations")


def _validate_existing_workflow_mode(*, base_url: str, task_id: str) -> str:
    """Require a canonical task identity and an actual loopback web endpoint."""

    parsed_url = urlsplit(base_url)
    try:
        parsed_task_id = UUID(task_id)
        port = parsed_url.port
    except (TypeError, ValueError) as error:
        raise DockerSmokeError(
            "existing workflow verification requires a canonical UUIDv4 task ID "
            "and loopback base URL"
        ) from error
    if (
        parsed_task_id.version != 4
        or str(parsed_task_id) != task_id
        or parsed_url.scheme != "http"
        or parsed_url.hostname not in _LOOPBACK_HOSTS
        or parsed_url.username is not None
        or parsed_url.password is not None
        or port is None
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise DockerSmokeError(
            "existing workflow verification requires a canonical UUIDv4 task ID "
            "and loopback base URL"
        )
    return str(parsed_task_id)


@contextmanager
def _disposable_admin_headers() -> Iterator[dict[str, str]]:
    """Yield a disposable authenticated session without returning its cookie."""

    from django.conf import settings
    from django.contrib.auth import (
        BACKEND_SESSION_KEY,
        HASH_SESSION_KEY,
        SESSION_KEY,
        get_user_model,
    )
    from django.contrib.sessions.backends.db import SessionStore

    user_model = get_user_model()
    user = user_model(
        username=f"django-ray-smoke-{secrets.token_hex(8)}",
        is_staff=True,
        is_superuser=True,
    )
    user.set_unusable_password()
    user.save()

    session: Any | None = None
    try:
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.save()
        if session.session_key is None:
            raise DockerSmokeError("could not create a disposable admin session")
        yield {
            "Cookie": f"{settings.SESSION_COOKIE_NAME}={session.session_key}",
        }
    finally:
        try:
            if session is not None and session.session_key is not None:
                session.delete(session.session_key)
        finally:
            user.delete()


def _verify_unfold_admin_contract(
    *,
    base_url: str,
    deadline: float,
    execution: Any,
    attempt: Any,
) -> dict[str, str]:
    with _disposable_admin_headers() as headers:
        cookie = headers["Cookie"]
        index_html = _request_text(
            base_url,
            "/admin/",
            headers=headers,
            deadline=deadline,
        )
        login_html = _request_text(
            base_url,
            "/admin/login/",
            deadline=deadline,
        )
        changelist_html = _request_text(
            base_url,
            "/admin/django_ray/raytaskexecution/",
            headers=headers,
            deadline=deadline,
        )
        change_html = _request_text(
            base_url,
            f"/admin/django_ray/raytaskexecution/{execution.pk}/change/",
            headers=headers,
            deadline=deadline,
        )
        attempt_detail_path = f"/admin/django_ray/taskattempt/{attempt.pk}/change/"
        attempt_detail_html = _request_text(
            base_url,
            attempt_detail_path,
            headers=headers,
            deadline=deadline,
        )
        observability_text = _request_text(
            base_url,
            f"/admin/django_ray/raytaskexecution/{execution.pk}/observability/",
            headers={"Accept": "application/json", "Cookie": cookie},
            deadline=deadline,
        )

        if "django-ray" not in index_html:
            raise DockerSmokeError("admin index did not render django-ray branding")
        if "testproject/landing-graph-bg" not in login_html:
            raise DockerSmokeError("admin login did not render the branded graph")
        if "/admin/django_ray/taskattempt/" in index_html:
            raise DockerSmokeError("admin index exposed standalone attempt navigation")
        if "retry_tasks" not in changelist_html or "cancel_tasks" not in changelist_html:
            raise DockerSmokeError("admin changelist did not render task controls")
        compact_columns = [
            "column-id",
            "column-state_display",
            "column-task_display",
            "column-queue_display",
            "column-priority",
            "column-attempt_display",
            "column-ray_dashboard_link",
            "column-created_display",
            "column-started_display",
            "column-finished_display",
        ]
        positions = [changelist_html.find(marker) for marker in compact_columns]
        if -1 in positions or positions != sorted(positions):
            raise DockerSmokeError("admin changelist did not render the compact column order")
        if any(
            marker in changelist_html
            for marker in (
                "column-execution_generation",
                "column-workflow_run_id",
                "column-workflow_plan_fingerprint",
                "column-workflow_plan_pinned_attempt",
            )
        ):
            raise DockerSmokeError("admin changelist exposed detail-only workflow identity")
        if (
            "django-ray-live-observability" not in change_html
            or _TASK_LIVE_STYLESHEET_RE.search(change_html) is None
            or _TASK_LIVE_SCRIPT_RE.search(change_html) is None
            or _WORKFLOW_DIAGNOSTICS_SCRIPT_RE.search(change_html) is None
            or 'aria-labelledby="django-ray-live-heading"' not in change_html
            or change_html.count('role="status"') != 2
            or "django-ray-live__grid" not in change_html
            or "django-ray-workflow-diagnostics" not in change_html
            or "Workflow execution" not in change_html
        ):
            raise DockerSmokeError("admin change view did not render live task diagnostics")
        workflow_attempt_query = f"?attempt_number={int(execution.attempt_number)}"
        workflow_paths = {
            "data-graph-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/graph/"
                f"{workflow_attempt_query}"
            ),
            "data-topology-nodes-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/topology/nodes/"
                f"{workflow_attempt_query}"
            ),
            "data-topology-edges-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/topology/edges/"
                f"{workflow_attempt_query}"
            ),
            "data-node-details-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/nodes/"
                f"{workflow_attempt_query}"
            ),
            "data-node-detail-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/node/"
                f"{workflow_attempt_query}"
            ),
        }
        if any(
            f'{attribute}="{path}"' not in change_html or f'href="{path}"' in change_html
            for attribute, path in workflow_paths.items()
        ):
            raise DockerSmokeError(
                "admin change view did not keep workflow actions behind lazy diagnostics"
            )
        if "Runtime env json" in change_html or "field-runtime_env_json" in change_html:
            raise DockerSmokeError("admin change view exposed the raw RuntimeEnv snapshot")
        if (
            "Execution metadata is read-only" not in change_html
            or 'name="_save"' in change_html
            or 'name="_continue"' in change_html
            or (f"/admin/django_ray/raytaskexecution/{execution.pk}/delete/" in change_html)
            or any(
                f'name="{field}"' in change_html
                for field in (
                    "priority",
                    "queue_name",
                    "state",
                    "attempt_number",
                    "execution_generation",
                    "claimed_by_worker",
                )
            )
        ):
            raise DockerSmokeError("admin change view exposed direct execution editing")
        if (
            "Attempt history" not in change_html
            or change_html.count(attempt_detail_path) != 1
            or str(attempt) in change_html
        ):
            raise DockerSmokeError("admin change view did not render contextual attempt history")
        if (
            str(attempt.attempt_number) not in attempt_detail_html
            or str(attempt.state) not in attempt_detail_html
        ):
            raise DockerSmokeError("admin attempt detail did not render the archived attempt")

        stylesheet_match = _UNFOLD_STYLESHEET_RE.search(index_html)
        if stylesheet_match is None:
            raise DockerSmokeError("admin index did not load the Unfold stylesheet")
        stylesheet_path = html.unescape(stylesheet_match.group("path"))
        stylesheet = _request_text(
            base_url,
            stylesheet_path,
            expected_content_type="text/css",
            deadline=deadline,
        )
        if not stylesheet.strip():
            raise DockerSmokeError("Unfold stylesheet response was empty")

        custom_stylesheet_match = _DJANGO_RAY_STYLESHEET_RE.search(index_html)
        if custom_stylesheet_match is None:
            raise DockerSmokeError("admin index did not load the django-ray stylesheet")
        custom_stylesheet_path = html.unescape(custom_stylesheet_match.group("path"))
        custom_stylesheet = _request_text(
            base_url,
            custom_stylesheet_path,
            expected_content_type="text/css",
            deadline=deadline,
        )
        if "--django-ray-admin-accent" not in custom_stylesheet:
            raise DockerSmokeError("django-ray stylesheet did not contain theme tokens")

        live_stylesheet_match = _TASK_LIVE_STYLESHEET_RE.search(change_html)
        if live_stylesheet_match is None:
            raise DockerSmokeError("admin change view did not load the live status stylesheet")
        live_stylesheet_path = html.unescape(live_stylesheet_match.group("path"))
        live_stylesheet = _request_text(
            base_url,
            live_stylesheet_path,
            expected_content_type="text/css",
            deadline=deadline,
        )
        if (
            "#django-ray-live-observability" not in live_stylesheet
            or ".django-ray-workflow__summary" not in live_stylesheet
            or "grid-template-columns: repeat(4, minmax(0, 1fr))" not in live_stylesheet
            or ".django-ray-workflow__chip" not in live_stylesheet
            or ":focus-visible" not in live_stylesheet
        ):
            raise DockerSmokeError("live status stylesheet response was invalid")

        workflow_script_match = _WORKFLOW_DIAGNOSTICS_SCRIPT_RE.search(change_html)
        if workflow_script_match is None:
            raise DockerSmokeError("admin change view did not load workflow diagnostics JavaScript")
        workflow_script = _request_text(
            base_url,
            html.unescape(workflow_script_match.group("path")),
            expected_content_type=("text/javascript", "application/javascript"),
            deadline=deadline,
        )
        if (
            "django-ray-workflow-diagnostics" not in workflow_script
            or 'credentials: "same-origin"' not in workflow_script
            or "innerHTML" in workflow_script
        ):
            raise DockerSmokeError("workflow diagnostics script response was invalid")

        icon_match = _DJANGO_RAY_ICON_RE.search(index_html)
        if icon_match is None:
            raise DockerSmokeError("admin index did not render the django-ray icon")
        icon_path = html.unescape(icon_match.group("path"))
        icon = _request_text(
            base_url,
            icon_path,
            expected_content_type="image/svg+xml",
            deadline=deadline,
        )
        if 'aria-label="django-ray"' not in icon:
            raise DockerSmokeError("django-ray admin icon response was invalid")

        try:
            observability = json.loads(observability_text)
        except json.JSONDecodeError as error:
            raise DockerSmokeError("admin observability response was not valid JSON") from error
        if (
            not isinstance(observability, dict)
            or observability.get("id") != execution.pk
            or observability.get("state") != execution.state
        ):
            raise DockerSmokeError("admin observability returned the wrong execution")

    return {
        "admin": "unfold-authenticated",
        "admin_attempt_detail": "verified",
        "admin_attempt_history": "verified",
        "admin_branding": "verified",
        "admin_layout": "verified",
        "admin_observability": "verified",
        "admin_static": "served",
    }


def _workflow_admin_page_count(
    payload: dict[str, Any],
    *,
    task_id: str,
    collection: str,
) -> int:
    """Validate one complete bounded admin reader page without retaining it."""

    items = payload.get("items")
    returned_count = payload.get("returned_count")
    if (
        payload.get("schema") != "django-ray.workflow-progress-page"
        or payload.get("schema_version") != 1
        or payload.get("task_id") != task_id
        or payload.get("collection") != collection
        or payload.get("availability") != "AVAILABLE"
        or payload.get("complete") is not True
        or not isinstance(items, list)
        or not items
        or type(returned_count) is not int
        or returned_count != len(items)
        or returned_count > _WORKFLOW_PAGE_LIMIT
        or payload.get("next_cursor") is not None
        or not all(isinstance(item, dict) for item in items)
    ):
        raise DockerSmokeError(
            f"admin {collection} route did not return one complete nonempty AVAILABLE page"
        )
    return returned_count


def _workflow_graph_contains_forbidden_field(value: Any) -> bool:
    """Detect private workflow fields anywhere in the admin graph projection."""
    if isinstance(value, dict):
        if set(value) & _WORKFLOW_GRAPH_FORBIDDEN_FIELDS:
            return True
        return any(_workflow_graph_contains_forbidden_field(item) for item in value.values())
    if isinstance(value, list):
        return any(_workflow_graph_contains_forbidden_field(item) for item in value)
    return False


def _workflow_admin_graph_evidence(
    payload: dict[str, Any],
    *,
    execution_state: str,
    topology_nodes: list[dict[str, Any]],
    topology_edges: list[dict[str, Any]],
    node_details: list[dict[str, Any]],
) -> dict[str, str | int]:
    """Validate one sanitized graph against the same bounded admin pages."""
    if _workflow_graph_contains_forbidden_field(payload):
        raise DockerSmokeError("admin workflow graph exposed a forbidden private field")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    counts = payload.get("counts")
    message = payload.get("message")
    if (
        set(payload) != _WORKFLOW_GRAPH_ROOT_FIELDS
        or payload.get("schema") != "django-ray.admin-workflow-graph"
        or payload.get("schema_version") != 1
        or payload.get("status") != "AVAILABLE"
        or payload.get("complete") is not True
        or not isinstance(message, str)
        or not message
        or len(message.encode("utf-8")) > 256
        or not isinstance(counts, dict)
        or set(counts) != {"nodes", "edges"}
        or payload.get("limits") != _WORKFLOW_GRAPH_LIMITS
        or not isinstance(nodes, list)
        or not isinstance(edges, list)
        or counts.get("nodes") != len(nodes)
        or counts.get("edges") != len(edges)
        or len(nodes) != len(topology_nodes)
        or len(edges) != len(topology_edges)
        or len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
        > _WORKFLOW_GRAPH_LIMITS["response_bytes"]
    ):
        raise DockerSmokeError("admin workflow graph did not match its bounded root contract")

    topology_ids = {
        item.get("node_id") for item in topology_nodes if isinstance(item.get("node_id"), str)
    }
    detail_states = {
        item.get("node_id"): item.get("state")
        for item in node_details
        if isinstance(item.get("node_id"), str) and isinstance(item.get("state"), str)
    }
    if len(topology_ids) != len(topology_nodes) or len(detail_states) != len(node_details):
        raise DockerSmokeError("admin workflow pages contained invalid or duplicate node IDs")

    graph_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for item in nodes:
        if not isinstance(item, dict):
            raise DockerSmokeError("admin workflow graph node was not an object")
        node_id = item.get("id")
        kind = item.get("kind")
        expected_fields = set(_WORKFLOW_GRAPH_NODE_FIELDS)
        if kind == "map":
            expected_fields.add("fanout")
        if (
            set(item) != expected_fields
            or not isinstance(node_id, str)
            or not node_id
            or node_id in graph_by_id
            or not isinstance(item.get("label"), str)
            or not item["label"]
            or kind not in {"task", "map"}
            or item.get("state") not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"}
            or not (item.get("message") is None or isinstance(item.get("message"), str))
            or not (item.get("error") is None or isinstance(item.get("error"), str))
            or type(item.get("failure_path")) is not bool
        ):
            raise DockerSmokeError("admin workflow graph node failed its allowlist")
        if kind == "map":
            fanout = item.get("fanout")
            if (
                not isinstance(fanout, dict)
                or set(fanout) != _WORKFLOW_GRAPH_FANOUT_FIELDS
                or any(
                    type(fanout.get(field)) is not int or fanout[field] < 0
                    for field in (
                        "submitted_items",
                        "completed_items",
                        "in_flight_items",
                    )
                )
                or type(fanout.get("input_exhausted")) is not bool
                or fanout["completed_items"] > fanout["submitted_items"]
                or fanout["in_flight_items"]
                != fanout["submitted_items"] - fanout["completed_items"]
            ):
                raise DockerSmokeError("admin workflow graph map fanout failed validation")
        if (item["state"] == "FAILED") != (item["error"] is not None):
            raise DockerSmokeError("admin workflow graph failure error was inconsistent")
        graph_by_id[node_id] = item
        ordered_ids.append(node_id)

    if set(graph_by_id) != topology_ids or any(
        graph_by_id[node_id]["state"] != state for node_id, state in detail_states.items()
    ):
        raise DockerSmokeError("admin workflow graph nodes differed from bounded pages")

    expected_edges = {(item.get("source"), item.get("target")) for item in topology_edges}
    graph_edges: set[tuple[str, str]] = set()
    positions = {node_id: index for index, node_id in enumerate(ordered_ids)}
    for item in edges:
        if not isinstance(item, dict) or set(item) != {"source", "target"}:
            raise DockerSmokeError("admin workflow graph edge failed its allowlist")
        source = item.get("source")
        target = item.get("target")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in graph_by_id
            or target not in graph_by_id
            or source == target
            or (source, target) in graph_edges
            or positions[source] >= positions[target]
        ):
            raise DockerSmokeError("admin workflow graph edge was invalid or not topological")
        graph_edges.add((source, target))
    if graph_edges != expected_edges:
        raise DockerSmokeError("admin workflow graph edges differed from bounded pages")

    failed = {node_id for node_id, item in graph_by_id.items() if item["state"] == "FAILED"}
    pending_nodes = {node_id for node_id, item in graph_by_id.items() if item["state"] == "PENDING"}
    running_nodes = {node_id for node_id, item in graph_by_id.items() if item["state"] == "RUNNING"}
    succeeded = {node_id for node_id, item in graph_by_id.items() if item["state"] == "SUCCEEDED"}
    predecessors = {node_id: set() for node_id in graph_by_id}
    for source, target in graph_edges:
        predecessors[target].add(source)
    origins = {
        node_id
        for node_id in failed
        if not any(parent in failed for parent in predecessors[node_id])
    }
    expected_failure_path: set[str] = set()
    pending = list(origins)
    while pending:
        node_id = pending.pop()
        if node_id in expected_failure_path:
            continue
        expected_failure_path.add(node_id)
        pending.extend(predecessors[node_id])
    observed_failure_path = {
        node_id for node_id, item in graph_by_id.items() if item["failure_path"]
    }
    incoming_failure_edges = sum(target in origins for _source, target in graph_edges)
    if observed_failure_path != expected_failure_path:
        raise DockerSmokeError("admin workflow graph failure path was inconsistent")
    if execution_state == "SUCCEEDED":
        if failed or observed_failure_path or len(succeeded) != len(graph_by_id):
            raise DockerSmokeError("successful admin workflow graph was not fully succeeded")
    elif execution_state == "FAILED":
        if (
            len(origins) != 1
            or not (succeeded & observed_failure_path)
            or not observed_failure_path
            or incoming_failure_edges < 1
        ):
            raise DockerSmokeError(
                "failed admin workflow graph lacked one incoming failed path "
                "and successful ancestor context"
            )
    else:
        raise DockerSmokeError("admin workflow graph execution was not terminal")

    return {
        "graph_status": "AVAILABLE",
        "graph_nodes": len(graph_by_id),
        "graph_edges": len(graph_edges),
        "graph_pending_nodes": len(pending_nodes),
        "graph_running_nodes": len(running_nodes),
        "graph_succeeded_nodes": len(succeeded),
        "graph_failed_nodes": len(failed),
        "graph_failure_path_nodes": len(observed_failure_path),
        "graph_failure_origins": len(origins),
        "graph_incoming_failure_edges": incoming_failure_edges,
    }


def _workflow_admin_degraded_graph_evidence(
    payload: dict[str, Any],
    *,
    expected_status: str,
) -> str:
    """Validate an empty degraded graph with the same bounded root contract."""
    if _workflow_graph_contains_forbidden_field(payload):
        raise DockerSmokeError("admin workflow graph exposed a forbidden private field")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    counts = payload.get("counts")
    message = payload.get("message")
    if (
        set(payload) != _WORKFLOW_GRAPH_ROOT_FIELDS
        or payload.get("schema") != "django-ray.admin-workflow-graph"
        or payload.get("schema_version") != 1
        or payload.get("status") != expected_status
        or payload.get("complete") is not False
        or not isinstance(message, str)
        or not message
        or len(message.encode("utf-8")) > 256
        or counts != {"nodes": 0, "edges": 0}
        or payload.get("limits") != _WORKFLOW_GRAPH_LIMITS
        or nodes != []
        or edges != []
        or len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
        > _WORKFLOW_GRAPH_LIMITS["response_bytes"]
    ):
        raise DockerSmokeError("admin workflow graph did not match its degraded root contract")
    return expected_status


def _verify_existing_workflow_storage_contract(
    *,
    execution: Any,
    topology_nodes: int,
    topology_edges: int,
    node_details: int,
    pending_nodes: int = 0,
    running_nodes: int = 0,
    failed_nodes: int = 0,
) -> dict[str, int]:
    """Directly prove that the published run has no transitional storage residue."""

    from django.db import transaction

    from django_ray.models import (
        WorkflowProgressRunStorage,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyPage,
        WorkflowProgressTopologySlot,
    )

    try:
        with transaction.atomic():
            run_storage = WorkflowProgressRunStorage.objects.select_for_update().get(
                execution=execution,
                attempt_number=execution.attempt_number,
                execution_generation=execution.execution_generation,
                run_id=execution.workflow_run_id,
            )
            manifests = WorkflowProgressTopologyManifest.objects.filter(run_storage=run_storage)
            current_manifests = manifests.filter(slot=WorkflowProgressTopologySlot.CURRENT).count()
            pending_manifests = manifests.filter(slot=WorkflowProgressTopologySlot.PENDING).count()
            unlinked_pages = WorkflowProgressTopologyPage.objects.filter(
                run_storage=run_storage,
                manifest_links__isnull=True,
            ).count()
            if current_manifests != 1 or pending_manifests != 0 or unlinked_pages != 0:
                raise DockerSmokeError(
                    "existing workflow retained pending, duplicate-current, "
                    "or unlinked topology storage"
                )

            current = manifests.get(slot=WorkflowProgressTopologySlot.CURRENT)
            state_counts = (pending_nodes, running_nodes, failed_nodes)
            if (
                any(type(value) is not int or value < 0 for value in state_counts)
                or sum(state_counts) > node_details
                or current.node_count != topology_nodes
                or current.edge_count != topology_edges
                or run_storage.detail_node_count != node_details
                or run_storage.detail_pending_count != pending_nodes
                or run_storage.detail_running_count != running_nodes
                or run_storage.detail_succeeded_count
                != node_details - pending_nodes - running_nodes - failed_nodes
                or run_storage.detail_failed_count != failed_nodes
            ):
                raise DockerSmokeError(
                    "existing workflow storage counts did not match terminal admin reader evidence"
                )
    except (
        WorkflowProgressRunStorage.DoesNotExist,
        WorkflowProgressRunStorage.MultipleObjectsReturned,
    ) as error:
        raise DockerSmokeError(
            "existing workflow did not retain exactly one run-scoped progress record"
        ) from error
    return {
        "current_manifests": current_manifests,
        "pending_manifests": pending_manifests,
        "unlinked_pages": unlinked_pages,
    }


def _verify_existing_workflow_admin_contract(
    *,
    base_url: str,
    deadline: float,
    execution: Any,
) -> dict[str, str | int]:
    """Exercise every advertised admin workflow reader for one existing run."""

    task_id = str(execution.task_id)
    root = f"/admin/django_ray/raytaskexecution/{execution.pk}"
    diagnostics_path = f"{root}/workflow/diagnostics/"
    graph_path = f"{root}/workflow/graph/"
    node_detail_path = f"{root}/workflow/node/"
    collection_paths = {
        "topology_nodes": f"{root}/workflow/topology/nodes/",
        "topology_edges": f"{root}/workflow/topology/edges/",
        "node_details": f"{root}/workflow/nodes/",
    }
    attempt_query = f"attempt_number={int(execution.attempt_number)}"
    diagnostics_read_path = f"{diagnostics_path}?{attempt_query}"
    graph_read_path = f"{graph_path}?{attempt_query}"
    collection_read_paths = {
        collection: f"{path}?{attempt_query}" for collection, path in collection_paths.items()
    }

    with _disposable_admin_headers() as headers:
        change_html = _request_text(
            base_url,
            f"{root}/change/",
            headers=headers,
            deadline=deadline,
        )
        expected_attributes = {
            "data-diagnostics-url": diagnostics_read_path,
            "data-graph-url": graph_read_path,
            "data-topology-nodes-url": collection_read_paths["topology_nodes"],
            "data-topology-edges-url": collection_read_paths["topology_edges"],
            "data-node-details-url": collection_read_paths["node_details"],
            "data-node-detail-url": f"{node_detail_path}?{attempt_query}",
        }
        if "django-ray-workflow-diagnostics" not in change_html or any(
            f'{attribute}="{path}"' not in change_html
            for attribute, path in expected_attributes.items()
        ):
            raise DockerSmokeError(
                "existing workflow admin change page did not advertise its bounded readers"
            )

        diagnostics = _request_admin_json(
            base_url,
            diagnostics_read_path,
            headers=headers,
            deadline=deadline,
        )
        plan = diagnostics.get("plan")
        progress = diagnostics.get("progress")
        expected_actions = {
            "topology_nodes": True,
            "topology_edges": True,
            "node_details": True,
        }
        if (
            diagnostics.get("schema") != "django-ray.admin-workflow-diagnostics"
            or diagnostics.get("schema_version") != 1
            or not isinstance(plan, dict)
            or plan.get("status") != "AVAILABLE"
            or not isinstance(progress, dict)
            or progress.get("state") != "AVAILABLE"
            or progress.get("availability") != "AVAILABLE"
            or progress.get("complete") is not True
            or progress.get("actions") != expected_actions
        ):
            raise DockerSmokeError(
                "existing workflow admin diagnostics did not advertise AVAILABLE readers"
            )

        pages = {
            collection: _request_admin_json(
                base_url,
                f"{path}&limit={_WORKFLOW_PAGE_LIMIT}",
                headers=headers,
                deadline=deadline,
            )
            for collection, path in collection_read_paths.items()
        }
        graph = _request_admin_json(
            base_url,
            graph_read_path,
            headers=headers,
            deadline=deadline,
        )

    counts = {
        collection: _workflow_admin_page_count(
            pages[collection],
            task_id=task_id,
            collection=collection,
        )
        for collection in collection_paths
    }
    graph_evidence = _workflow_admin_graph_evidence(
        graph,
        execution_state=str(execution.state),
        topology_nodes=pages["topology_nodes"]["items"],
        topology_edges=pages["topology_edges"]["items"],
        node_details=pages["node_details"]["items"],
    )

    storage = _verify_existing_workflow_storage_contract(
        execution=execution,
        topology_nodes=counts["topology_nodes"],
        topology_edges=counts["topology_edges"],
        node_details=counts["node_details"],
        pending_nodes=cast(int, graph_evidence["graph_pending_nodes"]),
        running_nodes=cast(int, graph_evidence["graph_running_nodes"]),
        failed_nodes=cast(int, graph_evidence["graph_failed_nodes"]),
    )
    return {
        "admin_workflow": "verified",
        "task_id": task_id,
        "task_state": str(execution.state),
        "attempt_number": int(execution.attempt_number),
        "admin_routes": 6,
        "admin_actions": len(expected_actions),
        "topology_nodes": counts["topology_nodes"],
        "topology_edges": counts["topology_edges"],
        "node_details": counts["node_details"],
        **graph_evidence,
        **storage,
    }


def _verify_existing_terminal_only_storage_contract(
    *,
    execution: Any,
) -> dict[str, bool | int | str]:
    """Prove one terminal summary exists without legacy or normalized detail."""

    from django_ray.models import (
        TaskAttempt,
        WorkflowProgressNodeDetail,
        WorkflowProgressRunStorage,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyManifestPage,
        WorkflowProgressTopologyPage,
    )
    from django_ray.workflow_plans import (
        WorkflowPlanValidationError,
        effective_plan_selection_reporting_policy,
        validate_plan_selection_manifest,
    )
    from django_ray.workflow_progress_summary import (
        WorkflowProgressSummaryError,
        deserialize_workflow_progress_summary,
    )

    if (
        execution.state not in {"SUCCEEDED", "FAILED"}
        or execution.attempt_number != 1
        or execution.workflow_run_id is None
        or execution.progress_data is not None
        or not isinstance(execution.workflow_plan_json, str)
        or not execution.workflow_plan_json
        or not isinstance(execution.workflow_progress_summary_json, str)
        or not execution.workflow_progress_summary_json
    ):
        raise DockerSmokeError(
            "terminal-only workflow lacked one first-attempt summary or wrote legacy progress"
        )
    attempts = TaskAttempt.objects.filter(execution=execution)
    if attempts.count() != 1:
        raise DockerSmokeError("terminal-only workflow did not archive exactly one task attempt")
    attempt = attempts.get()
    if (
        attempt.attempt_number != execution.attempt_number
        or attempt.state != execution.state
        or attempt.workflow_progress_summary_json != execution.workflow_progress_summary_json
    ):
        raise DockerSmokeError(
            "terminal-only workflow attempt did not archive the exact terminal summary"
        )
    try:
        plan = json.loads(execution.workflow_plan_json)
        summary = deserialize_workflow_progress_summary(execution.workflow_progress_summary_json)
        selection = validate_plan_selection_manifest(
            json.loads(execution.workflow_plan_selection or "null")
        )
    except (
        TypeError,
        json.JSONDecodeError,
        WorkflowPlanValidationError,
        WorkflowProgressSummaryError,
    ) as error:
        raise DockerSmokeError(
            "terminal-only workflow retained invalid summary or plan selection"
        ) from error
    if not isinstance(plan, dict):
        raise DockerSmokeError("terminal-only workflow retained an invalid materialized plan")
    snapshot = plan.get("snapshot")
    if isinstance(snapshot, dict):
        plan_declared_nodes = snapshot.get("observed_node_count")
        plan_declared_edges = snapshot.get("observed_edge_count")
    else:
        plan_declared_nodes = None
        plan_declared_edges = None
    if (
        type(plan_declared_nodes) is not int
        or plan_declared_nodes < 0
        or type(plan_declared_edges) is not int
        or plan_declared_edges < 0
    ):
        plan_nodes = plan.get("nodes")
        plan_edges = plan.get("edges")
        if not isinstance(plan_nodes, list) or not isinstance(plan_edges, list):
            raise DockerSmokeError(
                "terminal-only workflow retained an invalid materialized plan topology"
            )
        plan_declared_nodes = len(plan_nodes)
        plan_declared_edges = len(plan_edges)

    identity = summary["run_identity"]
    fingerprint = summary["plan_fingerprint"]
    node_counts = summary["node_counts"]
    edge_counts = summary["edge_counts"]
    timestamps = summary["timestamps"]
    finished_at = timestamps["finished_at"]
    declared_nodes = node_counts["declared"]
    declared_edges = edge_counts["declared"]
    if (
        effective_plan_selection_reporting_policy(selection) != "terminal_only"
        or selection.get("selected_strategy") != "dynamic_tasks"
        or summary["reporting_policy"] != "terminal_only"
        or summary["selected_strategy"] != "dynamic_tasks"
        or summary["summary_revision"] != 1
        or summary["topology_version"] is not None
        or summary["detail_revision"] is not None
        or summary["state"] != execution.state
        or fingerprint != execution.workflow_plan_fingerprint
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
        or execution.workflow_plan_pinned_attempt != 1
        or identity["task_execution_pk"] != execution.pk
        or identity["attempt_number"] != 1
        or identity["execution_generation"] != execution.execution_generation
        or identity["run_id"] != str(execution.workflow_run_id)
        or summary["detail"]
        != {
            "availability": "OMITTED_BY_POLICY",
            "complete": False,
            "truncation_reasons": [],
        }
        or summary["storage"] != {"kind": "database", "manifest_id": None}
        or summary["retention"]["detail_expires_at"] is not None
        or summary["terminal"] != {"outcome": execution.state, "finished_at": finished_at}
        or not isinstance(finished_at, str)
        or not finished_at
        or type(declared_nodes) is not int
        or declared_nodes < 1
        or declared_nodes != plan_declared_nodes
        or type(declared_edges) is not int
        or declared_edges < 1
        or declared_edges != plan_declared_edges
        or any(
            node_counts[field_name] != 0
            for field_name in (
                "discovered",
                "retained_topology",
                "retained_detail",
                "pending",
                "running",
                "succeeded",
                "failed",
            )
        )
        or edge_counts["discovered"] != 0
        or edge_counts["retained_topology"] != 0
        or summary["progress_percent"] != (100.0 if execution.state == "SUCCEEDED" else 0.0)
    ):
        raise DockerSmokeError(
            "terminal-only workflow summary claimed detail or mismatched its durable plan"
        )

    run_storage_rows = WorkflowProgressRunStorage.objects.filter(execution=execution).count()
    topology_manifests = WorkflowProgressTopologyManifest.objects.filter(
        run_storage__execution=execution
    ).count()
    topology_pages = WorkflowProgressTopologyPage.objects.filter(
        run_storage__execution=execution
    ).count()
    manifest_links = WorkflowProgressTopologyManifestPage.objects.filter(
        manifest__run_storage__execution=execution
    ).count()
    node_details = WorkflowProgressNodeDetail.objects.filter(
        run_storage__execution=execution
    ).count()
    if any(
        value != 0
        for value in (
            run_storage_rows,
            topology_manifests,
            topology_pages,
            manifest_links,
            node_details,
        )
    ):
        raise DockerSmokeError("terminal-only workflow retained normalized detail storage")
    return {
        "summary_revision": 1,
        "reporting_policy": "terminal_only",
        "detail_availability": "OMITTED_BY_POLICY",
        "declared_nodes": declared_nodes,
        "declared_edges": declared_edges,
        "legacy_progress_null": True,
        "attempt_summary_matches": True,
        "storage_rows": run_storage_rows,
        "topology_manifests": topology_manifests,
        "topology_pages": topology_pages,
        "manifest_links": manifest_links,
        "node_details": node_details,
    }


def _verify_existing_terminal_only_admin_contract(
    *,
    base_url: str,
    deadline: float,
    execution: Any,
) -> dict[str, bool | int | str]:
    """Prove admin presents the summary without advertising detail actions."""

    root = f"/admin/django_ray/raytaskexecution/{execution.pk}"
    diagnostics_path = f"{root}/workflow/diagnostics/"
    graph_path = f"{root}/workflow/graph/"
    attempt_query = f"attempt_number={int(execution.attempt_number)}"
    diagnostics_read_path = f"{diagnostics_path}?{attempt_query}"
    graph_read_path = f"{graph_path}?{attempt_query}"
    detail_paths = (
        graph_read_path,
        f"{root}/workflow/topology/nodes/?{attempt_query}",
        f"{root}/workflow/topology/edges/?{attempt_query}",
        f"{root}/workflow/nodes/?{attempt_query}",
        f"{root}/workflow/node/?{attempt_query}",
    )

    with _disposable_admin_headers() as headers:
        change_html = _request_text(
            base_url,
            f"{root}/change/",
            headers=headers,
            deadline=deadline,
        )
        if (
            "django-ray-workflow-diagnostics" not in change_html
            or f'data-diagnostics-url="{diagnostics_read_path}"' not in change_html
            or any(f'href="{path}"' in change_html for path in detail_paths)
        ):
            raise DockerSmokeError(
                "terminal-only workflow admin advertised a visible detail action"
            )
        diagnostics = _request_admin_json(
            base_url,
            diagnostics_read_path,
            headers=headers,
            deadline=deadline,
        )
        graph = _request_admin_json(
            base_url,
            graph_read_path,
            headers=headers,
            deadline=deadline,
        )

    plan = diagnostics.get("plan")
    progress = diagnostics.get("progress")
    no_actions = {
        "topology_nodes": False,
        "topology_edges": False,
        "node_details": False,
    }
    if (
        diagnostics.get("schema") != "django-ray.admin-workflow-diagnostics"
        or diagnostics.get("schema_version") != 1
        or not isinstance(plan, dict)
        or plan.get("status") != "AVAILABLE"
        or plan.get("reporting_policy") != "terminal_only"
        or not isinstance(progress, dict)
        or progress.get("state") != "TERMINAL_ONLY"
        or progress.get("workflow_state") != execution.state
        or progress.get("availability") != "OMITTED_BY_POLICY"
        or progress.get("complete") is not False
        or progress.get("actions") != no_actions
    ):
        raise DockerSmokeError(
            "terminal-only workflow admin did not retain its summary-only boundary"
        )
    graph_status = _workflow_admin_degraded_graph_evidence(
        graph,
        expected_status="UNAVAILABLE",
    )

    return {
        "admin_workflow": "terminal-summary-verified",
        "task_id": str(execution.task_id),
        "task_state": str(execution.state),
        "attempt_number": int(execution.attempt_number),
        "admin_actions": 0,
        "graph_advertised": False,
        "graph_status": graph_status,
        **_verify_existing_terminal_only_storage_contract(execution=execution),
    }


def _run_existing_workflow_admin_smoke(
    *,
    base_url: str,
    task_id: str,
    timeout_seconds: float,
    expected_reporting_policy: str = "full",
) -> dict[str, bool | int | str]:
    """Verify one already-terminal workflow through loopback admin and PostgreSQL."""

    import django

    task_id = _validate_existing_workflow_mode(base_url=base_url, task_id=task_id)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
    django.setup()
    _verify_database_contract()

    from django_ray.models import RayTaskExecution

    try:
        execution = RayTaskExecution.objects.get(task_id=task_id)
    except (
        RayTaskExecution.DoesNotExist,
        RayTaskExecution.MultipleObjectsReturned,
    ) as error:
        raise DockerSmokeError(
            "existing workflow task ID did not resolve to exactly one execution"
        ) from error
    deadline = time.monotonic() + timeout_seconds
    if expected_reporting_policy == "full":
        return _verify_existing_workflow_admin_contract(
            base_url=base_url,
            deadline=deadline,
            execution=execution,
        )
    if expected_reporting_policy == "terminal_only":
        return _verify_existing_terminal_only_admin_contract(
            base_url=base_url,
            deadline=deadline,
            execution=execution,
        )
    raise DockerSmokeError("unsupported expected workflow reporting policy")


def _run_smoke(*, base_url: str, token: str, timeout_seconds: float) -> dict[str, Any]:
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testproject.settings")
    django.setup()

    from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease

    _verify_database_contract()
    deadline = time.monotonic() + timeout_seconds

    def web_is_ready() -> bool:
        try:
            payload = _request_json(base_url, "/api/readyz")
        except DockerSmokeError:
            return False
        return payload.get("status") == "healthy" and payload.get("database") == "ok"

    _wait_until("the web service database readiness check", deadline, web_is_ready)
    _wait_until(
        "an active default-queue worker lease in PostgreSQL",
        deadline,
        lambda: TaskWorkerLease.objects.filter(
            is_active=True,
            queue_name__contains="default",
        ).exists(),
    )

    _request_json(
        base_url,
        "/api/enqueue/add/20/22",
        method="POST",
        expected_status=401,
    )
    _request_json(
        base_url,
        "/api/enqueue/add/20/22",
        method="POST",
        token="invalid-django-ray-smoke-token",
        expected_status=401,
    )
    enqueued = _request_json(
        base_url,
        "/api/enqueue/add/20/22",
        method="POST",
        token=token,
    )
    task_id = enqueued.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise DockerSmokeError("authenticated enqueue returned no task ID")

    terminal_status: str | None = None

    def task_succeeded() -> bool:
        nonlocal terminal_status
        payload = _request_json(base_url, f"/api/tasks/{task_id}", token=token)
        status = payload.get("status")
        if not isinstance(status, str):
            raise DockerSmokeError("task result endpoint returned no status")
        terminal_status = status
        if status in {"FAILED", "CANCELLED"}:
            raise DockerSmokeError(f"worker execution reached terminal status {status}")
        return status == "SUCCESSFUL"

    _wait_until("worker execution and API result refresh", deadline, task_succeeded)

    execution_payload = _request_json(
        base_url,
        f"/api/executions?task_id={task_id}&limit=1",
        token=token,
    )
    tasks = execution_payload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1:
        raise DockerSmokeError("execution API did not return exactly the enqueued task")
    api_execution = tasks[0]
    if not isinstance(api_execution, dict):
        raise DockerSmokeError("execution API returned an invalid task record")
    try:
        api_result = json.loads(api_execution["result_data"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise DockerSmokeError("execution API returned an invalid result payload") from error
    if api_execution.get("state") != TaskState.SUCCEEDED or api_result != 42:
        raise DockerSmokeError("execution API did not return the expected successful result")

    database_execution = RayTaskExecution.objects.get(task_id=task_id)
    try:
        database_result = json.loads(database_execution.result_data or "null")
    except json.JSONDecodeError as error:
        raise DockerSmokeError("PostgreSQL stored an invalid result payload") from error
    if database_execution.state != TaskState.SUCCEEDED or database_result != 42:
        raise DockerSmokeError("worker did not persist the expected result in shared PostgreSQL")
    try:
        database_attempt = TaskAttempt.objects.get(
            execution=database_execution,
            attempt_number=database_execution.attempt_number,
        )
    except TaskAttempt.DoesNotExist as error:
        raise DockerSmokeError("worker did not archive the successful task attempt") from error
    if database_attempt.state != TaskState.SUCCEEDED:
        raise DockerSmokeError("worker archived the task attempt with the wrong state")

    admin_contract = _verify_unfold_admin_contract(
        base_url=base_url,
        deadline=deadline,
        execution=database_execution,
        attempt=database_attempt,
    )

    return {
        **admin_contract,
        "database": "postgresql",
        "migrations": "applied",
        "authentication": "fail-closed-and-valid-token-accepted",
        "worker_lease": "observed",
        "task_id": task_id,
        "task_status": terminal_status,
        "result": database_result,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://web:8000")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--existing-workflow-task-id",
        help=(
            "Verify one existing workflow through loopback admin readers and "
            "run-scoped storage without enqueueing another task"
        ),
    )
    parser.add_argument(
        "--expected-workflow-reporting-policy",
        choices=("full", "terminal_only"),
        default="full",
        help="Expected policy for --existing-workflow-task-id (default: full)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.timeout <= 0:
        raise DockerSmokeError("--timeout must be positive")
    if args.existing_workflow_task_id is not None:
        result = _run_existing_workflow_admin_smoke(
            base_url=args.base_url,
            task_id=args.existing_workflow_task_id,
            timeout_seconds=args.timeout,
            expected_reporting_policy=args.expected_workflow_reporting_policy,
        )
    else:
        token = os.environ.get("DJANGO_API_TOKEN")
        if not token:
            raise DockerSmokeError("DJANGO_API_TOKEN must be set")
        result = _run_smoke(
            base_url=args.base_url,
            token=token,
            timeout_seconds=args.timeout,
        )
    if any(type(value) not in {bool, float, int, str} for value in result.values()):
        raise DockerSmokeError("smoke evidence must contain scalar JSON values only")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
