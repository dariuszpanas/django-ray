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
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

_MAX_RESPONSE_BYTES = 1_048_576
_REQUEST_TIMEOUT_SECONDS = 5.0
_WORKFLOW_PAGE_LIMIT = 16
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
) -> dict[str, Any]:
    """Read one authenticated, byte-bounded admin JSON response."""

    body = _request_text(
        base_url,
        path,
        headers={"Accept": "application/json", **headers},
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
        workflow_paths = {
            "data-topology-nodes-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/topology/nodes/"
            ),
            "data-topology-edges-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/topology/edges/"
            ),
            "data-node-details-url": (
                f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/nodes/"
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


def _verify_existing_workflow_storage_contract(
    *,
    execution: Any,
    topology_nodes: int,
    topology_edges: int,
    node_details: int,
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
            if (
                current.node_count != topology_nodes
                or current.edge_count != topology_edges
                or run_storage.detail_node_count != node_details
                or run_storage.detail_pending_count != 0
                or run_storage.detail_running_count != 0
                or run_storage.detail_succeeded_count != node_details
                or run_storage.detail_failed_count != 0
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
    collection_paths = {
        "topology_nodes": f"{root}/workflow/topology/nodes/",
        "topology_edges": f"{root}/workflow/topology/edges/",
        "node_details": f"{root}/workflow/nodes/",
    }

    with _disposable_admin_headers() as headers:
        change_html = _request_text(
            base_url,
            f"{root}/change/",
            headers=headers,
            deadline=deadline,
        )
        expected_attributes = {
            "data-diagnostics-url": diagnostics_path,
            "data-topology-nodes-url": collection_paths["topology_nodes"],
            "data-topology-edges-url": collection_paths["topology_edges"],
            "data-node-details-url": collection_paths["node_details"],
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
            diagnostics_path,
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
                f"{path}?limit={_WORKFLOW_PAGE_LIMIT}",
                headers=headers,
                deadline=deadline,
            )
            for collection, path in collection_paths.items()
        }

    counts = {
        collection: _workflow_admin_page_count(
            pages[collection],
            task_id=task_id,
            collection=collection,
        )
        for collection in collection_paths
    }

    storage = _verify_existing_workflow_storage_contract(
        execution=execution,
        topology_nodes=counts["topology_nodes"],
        topology_edges=counts["topology_edges"],
        node_details=counts["node_details"],
    )
    return {
        "admin_workflow": "verified",
        "task_id": task_id,
        "admin_routes": 5,
        "admin_actions": len(expected_actions),
        "topology_nodes": counts["topology_nodes"],
        "topology_edges": counts["topology_edges"],
        "node_details": counts["node_details"],
        **storage,
    }


def _run_existing_workflow_admin_smoke(
    *,
    base_url: str,
    task_id: str,
    timeout_seconds: float,
) -> dict[str, str | int]:
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
    return _verify_existing_workflow_admin_contract(
        base_url=base_url,
        deadline=time.monotonic() + timeout_seconds,
        execution=execution,
    )


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
