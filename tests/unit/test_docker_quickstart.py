"""Contracts for the tracked Docker Compose quickstart."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.bounded_redact import read_redacted_bounded, redact_and_bound
from testproject import docker_smoke

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_TASK_ID = "25200000-0000-4000-8000-000000000002"


class _Response:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._body = io.BytesIO(json.dumps(payload).encode())

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _TextResponse(_Response):
    def __init__(
        self,
        value: str,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = io.BytesIO(value.encode())


def _compose() -> dict[str, Any]:
    return yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_application_services_share_required_postgresql_configuration() -> None:
    compose = _compose()
    services = compose["services"]
    required_environment = {
        "DATABASE_ENGINE": "django.db.backends.postgresql",
        "DATABASE_NAME": "django_ray",
        "DATABASE_USER": "django_ray",
        "DATABASE_PASSWORD": ("${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD before running Compose}"),
        "DATABASE_HOST": "postgres",
        "DATABASE_PORT": "5432",
    }

    for service_name in ("migrate", "web", "worker", "smoke"):
        environment = services[service_name]["environment"]
        assert {name: environment[name] for name in required_environment} == required_environment

    assert services["web"]["environment"]["DJANGO_API_TOKEN"].startswith("${DJANGO_API_TOKEN:?")
    assert services["smoke"]["environment"]["DJANGO_API_TOKEN"].startswith("${DJANGO_API_TOKEN:?")
    assert "DJANGO_API_TOKEN" not in services["migrate"]["environment"]
    assert "DJANGO_API_TOKEN" not in services["worker"]["environment"]
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD"].startswith(
        "${POSTGRES_PASSWORD:?"
    )
    assert "secret" not in json.dumps(compose).lower()


def test_migrations_are_a_single_ordered_service() -> None:
    services = _compose()["services"]

    assert services["migrate"]["command"] == ["migrate"]
    assert services["migrate"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    for service_name in ("web", "worker"):
        assert services[service_name]["depends_on"]["migrate"] == {
            "condition": "service_completed_successfully"
        }
    assert "migrate" not in services["web"]["command"]
    assert "migrate" not in services["worker"]["command"]


def test_smoke_is_opt_in_and_waits_for_web_and_worker() -> None:
    smoke = _compose()["services"]["smoke"]

    assert smoke["profiles"] == ["smoke"]
    assert smoke["depends_on"] == {
        "web": {"condition": "service_healthy"},
        "worker": {"condition": "service_started"},
    }
    assert smoke["command"][:3] == ["python", "-m", "testproject.docker_smoke"]


def test_compose_smoke_is_a_blocking_ci_job() -> None:
    workflow = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    smoke = jobs["docker-compose-smoke"]
    smoke_commands = "\n".join(
        step.get("run", "") for step in smoke["steps"] if isinstance(step, dict)
    )

    assert "docker compose up --build --detach web worker" in smoke_commands
    assert "docker compose --profile smoke run --rm --no-deps smoke" in smoke_commands
    assert "scripts/bounded_redact.py" in smoke_commands
    assert "--max-chars 65536" in smoke_commands
    assert "docker-compose-smoke" in jobs["build"]["needs"]
    assert "docker-compose-smoke" in jobs["ci-gate"]["needs"]


def test_runtime_image_seeds_pip_after_the_final_uv_sync() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")

    ensurepip = "/app/.venv/bin/python -m ensurepip --upgrade --default-pip"
    assert ensurepip in dockerfile
    assert dockerfile.index(ensurepip) > dockerfile.rindex("uv sync --frozen")


def test_ci_diagnostics_redact_before_a_marker_inclusive_hard_bound() -> None:
    secret = "operator-token-that-must-not-leak"
    output = redact_and_bound(
        f"{'x' * 80}{secret}{'y' * 80}",
        secrets=[secret],
        max_chars=100,
        source_truncated=True,
    )

    assert secret not in output
    assert "[diagnostics truncated; output capped at 100 characters]" in output
    assert len(output) <= 100


def test_ci_diagnostics_redact_secrets_split_across_stream_chunks() -> None:
    secret = "split-secret-value"
    output = read_redacted_bounded(
        io.StringIO(f"before-{secret}-after-{'z' * 200}"),
        secrets=[secret],
        max_chars=80,
        chunk_chars=5,
    )

    assert secret not in output
    assert "split-" not in output
    assert "[diagnostics truncated; output capped at 80 characters]" in output
    assert len(output) <= 80


def test_request_json_sends_bearer_token_without_putting_it_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Any = None

    def open_request(request, *, timeout):
        nonlocal captured_request
        captured_request = request
        assert timeout == docker_smoke._REQUEST_TIMEOUT_SECONDS
        return _Response({"status": "ok"})

    monkeypatch.setattr(docker_smoke.urllib.request, "urlopen", open_request)

    payload = docker_smoke._request_json(
        "http://web:8000",
        "/api/executions",
        token="private-token",
    )

    assert payload == {"status": "ok"}
    assert captured_request is not None
    assert captured_request.full_url == "http://web:8000/api/executions"
    assert captured_request.get_header("Authorization") == "Bearer private-token"
    assert "private-token" not in captured_request.full_url


def test_admin_text_request_keeps_session_cookie_out_of_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_request: Any = None

    def open_request(request, *, timeout):
        nonlocal captured_request
        captured_request = request
        assert timeout == docker_smoke._REQUEST_TIMEOUT_SECONDS
        return _TextResponse("<html>django-ray</html>")

    monkeypatch.setattr(docker_smoke.urllib.request, "urlopen", open_request)

    body = docker_smoke._request_text(
        "http://web:8000",
        "/admin/",
        headers={"Cookie": "sessionid=private-session"},
    )

    assert body == "<html>django-ray</html>"
    assert captured_request is not None
    assert captured_request.full_url == "http://web:8000/admin/"
    assert captured_request.get_header("Cookie") == "sessionid=private-session"
    assert "private-session" not in captured_request.full_url


@pytest.mark.parametrize("path", ["admin/", "https://example.com/admin/"])
def test_admin_text_request_rejects_nonlocal_paths(path: str) -> None:
    with pytest.raises(docker_smoke.DockerSmokeError, match="local absolute path"):
        docker_smoke._request_text("http://web:8000", path)


@pytest.mark.parametrize(
    ("base_url", "task_id"),
    [
        ("http://web:8000", WORKFLOW_TASK_ID),
        ("https://127.0.0.1:8000", WORKFLOW_TASK_ID),
        ("http://127.0.0.1:8000/admin", WORKFLOW_TASK_ID),
        ("http://127.0.0.1:8000", "not-a-task-id"),
    ],
)
def test_existing_workflow_mode_requires_loopback_and_canonical_task(
    base_url: str,
    task_id: str,
) -> None:
    with pytest.raises(
        docker_smoke.DockerSmokeError,
        match="canonical UUIDv4 task ID and loopback base URL",
    ):
        docker_smoke._validate_existing_workflow_mode(
            base_url=base_url,
            task_id=task_id,
        )


def test_existing_workflow_mode_accepts_explicit_loopback() -> None:
    assert (
        docker_smoke._validate_existing_workflow_mode(
            base_url="http://127.0.0.1:8000",
            task_id=WORKFLOW_TASK_ID,
        )
        == WORKFLOW_TASK_ID
    )


def test_existing_workflow_main_needs_no_api_token_and_prints_scalar_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = SimpleNamespace(
        base_url="http://127.0.0.1:8000",
        timeout=45.0,
        existing_workflow_task_id=WORKFLOW_TASK_ID,
    )
    expected = {
        "admin_workflow": "verified",
        "task_id": WORKFLOW_TASK_ID,
        "task_state": "SUCCEEDED",
        "attempt_number": 1,
        "admin_routes": 6,
        "admin_actions": 3,
        "topology_nodes": 3,
        "topology_edges": 2,
        "node_details": 3,
        "graph_status": "AVAILABLE",
        "graph_nodes": 3,
        "graph_edges": 2,
        "graph_pending_nodes": 0,
        "graph_running_nodes": 0,
        "graph_succeeded_nodes": 3,
        "graph_failed_nodes": 0,
        "graph_failure_path_nodes": 0,
        "graph_failure_origins": 0,
        "graph_incoming_failure_edges": 0,
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
    }
    calls: list[dict[str, object]] = []
    monkeypatch.delenv("DJANGO_API_TOKEN", raising=False)
    monkeypatch.setattr(
        docker_smoke,
        "_parser",
        lambda: SimpleNamespace(parse_args=lambda: args),
    )
    monkeypatch.setattr(
        docker_smoke,
        "_run_existing_workflow_admin_smoke",
        lambda **kwargs: calls.append(kwargs) or expected,
    )

    assert docker_smoke.main() == 0

    assert calls == [
        {
            "base_url": "http://127.0.0.1:8000",
            "task_id": WORKFLOW_TASK_ID,
            "timeout_seconds": 45.0,
        }
    ]
    assert json.loads(capsys.readouterr().out) == expected


def _admin_workflow_responses(
    execution: SimpleNamespace,
) -> tuple[str, dict[str, dict[str, Any]]]:
    root = f"/admin/django_ray/raytaskexecution/{execution.pk}"
    diagnostics_path = f"{root}/workflow/diagnostics/"
    graph_path = f"{root}/workflow/graph/"
    node_detail_path = f"{root}/workflow/node/"
    collection_paths = {
        "topology_nodes": f"{root}/workflow/topology/nodes/",
        "topology_edges": f"{root}/workflow/topology/edges/",
        "node_details": f"{root}/workflow/nodes/",
    }
    attempt_query = f"?attempt_number={execution.attempt_number}"
    change_html = "".join(
        (
            '<section id="django-ray-workflow-diagnostics" ',
            f'data-diagnostics-url="{diagnostics_path}" ',
            f'data-graph-url="{graph_path}{attempt_query}" ',
            f'data-topology-nodes-url="{collection_paths["topology_nodes"]}{attempt_query}" ',
            f'data-topology-edges-url="{collection_paths["topology_edges"]}{attempt_query}" ',
            f'data-node-details-url="{collection_paths["node_details"]}{attempt_query}" ',
            f'data-node-detail-url="{node_detail_path}{attempt_query}"></section>',
        )
    )
    node_ids = ("0.0", "0.1", "0.2")

    def page(collection: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema": "django-ray.workflow-progress-page",
            "schema_version": 1,
            "task_id": execution.task_id,
            "collection": collection,
            "availability": "AVAILABLE",
            "complete": True,
            "returned_count": len(items),
            "items": items,
            "next_cursor": None,
        }

    page_query = f"{attempt_query}&limit={docker_smoke._WORKFLOW_PAGE_LIMIT}"
    responses = {
        diagnostics_path: {
            "schema": "django-ray.admin-workflow-diagnostics",
            "schema_version": 1,
            "plan": {"status": "AVAILABLE"},
            "progress": {
                "state": "AVAILABLE",
                "availability": "AVAILABLE",
                "complete": True,
                "actions": {
                    "topology_nodes": True,
                    "topology_edges": True,
                    "node_details": True,
                },
            },
        },
        f"{collection_paths['topology_nodes']}{page_query}": page(
            "topology_nodes",
            [{"node_id": node_id} for node_id in node_ids],
        ),
        f"{collection_paths['topology_edges']}{page_query}": page(
            "topology_edges",
            [
                {"source": node_ids[0], "target": node_ids[1]},
                {"source": node_ids[1], "target": node_ids[2]},
            ],
        ),
        f"{collection_paths['node_details']}{page_query}": page(
            "node_details",
            [{"node_id": node_id, "state": "SUCCEEDED"} for node_id in node_ids],
        ),
        f"{graph_path}{attempt_query}": {
            "schema": "django-ray.admin-workflow-graph",
            "schema_version": 1,
            "status": "AVAILABLE",
            "message": "Bounded terminal workflow graph is available.",
            "complete": True,
            "counts": {"nodes": 3, "edges": 2},
            "limits": dict(docker_smoke._WORKFLOW_GRAPH_LIMITS),
            "nodes": [
                {
                    "id": node_id,
                    "label": f"Step {index}",
                    "kind": "task",
                    "state": "SUCCEEDED",
                    "message": None,
                    "error": None,
                    "failure_path": False,
                }
                for index, node_id in enumerate(node_ids)
            ],
            "edges": [
                {"source": node_ids[0], "target": node_ids[1]},
                {"source": node_ids[1], "target": node_ids[2]},
            ],
        },
    }
    return change_html, responses


def test_existing_workflow_admin_reads_real_routes_and_returns_scalar_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = SimpleNamespace(
        pk=42,
        task_id=WORKFLOW_TASK_ID,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id="35200000-0000-4000-8000-000000000003",
        state="SUCCEEDED",
    )
    change_html, responses = _admin_workflow_responses(execution)
    cookie = "sessionid=private-admin-session"
    requested_paths: list[str] = []
    cleanup_events: list[str] = []

    @contextmanager
    def admin_headers():
        try:
            yield {"Cookie": cookie}
        finally:
            cleanup_events.append("session-and-user-cleaned")

    def request_text(
        base_url: str,
        path: str,
        *,
        headers: dict[str, str],
        deadline: float,
        **_kwargs: object,
    ) -> str:
        assert base_url == "http://127.0.0.1:8000"
        assert headers == {"Cookie": cookie}
        assert deadline > 0
        requested_paths.append(path)
        return change_html

    def request_json(
        base_url: str,
        path: str,
        *,
        headers: dict[str, str],
        deadline: float,
    ) -> dict[str, Any]:
        assert base_url == "http://127.0.0.1:8000"
        assert headers == {"Cookie": cookie}
        assert deadline > 0
        requested_paths.append(path)
        return responses[path]

    storage_calls: list[dict[str, object]] = []

    def storage_contract(**kwargs: object) -> dict[str, int]:
        storage_calls.append(kwargs)
        return {
            "current_manifests": 1,
            "pending_manifests": 0,
            "unlinked_pages": 0,
        }

    monkeypatch.setattr(docker_smoke, "_disposable_admin_headers", admin_headers)
    monkeypatch.setattr(docker_smoke, "_request_text", request_text)
    monkeypatch.setattr(docker_smoke, "_request_admin_json", request_json)
    monkeypatch.setattr(
        docker_smoke,
        "_verify_existing_workflow_storage_contract",
        storage_contract,
    )

    evidence = docker_smoke._verify_existing_workflow_admin_contract(
        base_url="http://127.0.0.1:8000",
        deadline=100.0,
        execution=execution,
    )

    root = f"/admin/django_ray/raytaskexecution/{execution.pk}"
    assert requested_paths == [
        f"{root}/change/",
        f"{root}/workflow/diagnostics/",
        f"{root}/workflow/topology/nodes/?attempt_number=1&limit=16",
        f"{root}/workflow/topology/edges/?attempt_number=1&limit=16",
        f"{root}/workflow/nodes/?attempt_number=1&limit=16",
        f"{root}/workflow/graph/?attempt_number=1",
    ]
    assert storage_calls == [
        {
            "execution": execution,
            "topology_nodes": 3,
            "topology_edges": 2,
            "node_details": 3,
            "pending_nodes": 0,
            "running_nodes": 0,
            "failed_nodes": 0,
        }
    ]
    assert evidence == {
        "admin_workflow": "verified",
        "task_id": WORKFLOW_TASK_ID,
        "task_state": "SUCCEEDED",
        "attempt_number": 1,
        "admin_routes": 6,
        "admin_actions": 3,
        "topology_nodes": 3,
        "topology_edges": 2,
        "node_details": 3,
        "graph_status": "AVAILABLE",
        "graph_nodes": 3,
        "graph_edges": 2,
        "graph_pending_nodes": 0,
        "graph_running_nodes": 0,
        "graph_succeeded_nodes": 3,
        "graph_failed_nodes": 0,
        "graph_failure_path_nodes": 0,
        "graph_failure_origins": 0,
        "graph_incoming_failure_edges": 0,
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
    }
    assert cleanup_events == ["session-and-user-cleaned"]
    assert cookie not in json.dumps(evidence)
    assert all(type(value) in {int, str} for value in evidence.values())


@pytest.mark.parametrize(
    "failure",
    [
        "unavailable_diagnostics",
        "empty_nodes",
    ],
)
def test_existing_workflow_admin_rejects_unavailable_or_inconsistent_routes(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    execution = SimpleNamespace(
        pk=42,
        task_id=WORKFLOW_TASK_ID,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id="35200000-0000-4000-8000-000000000003",
        state="SUCCEEDED",
    )
    change_html, responses = _admin_workflow_responses(execution)
    root = f"/admin/django_ray/raytaskexecution/{execution.pk}"
    query = f"?attempt_number=1&limit={docker_smoke._WORKFLOW_PAGE_LIMIT}"
    diagnostics_path = f"{root}/workflow/diagnostics/"
    node_path = f"{root}/workflow/topology/nodes/{query}"
    if failure == "unavailable_diagnostics":
        responses[diagnostics_path]["progress"]["state"] = "MISSING"
        responses[diagnostics_path]["progress"]["availability"] = "MISSING"
    else:
        responses[node_path]["items"] = []
        responses[node_path]["returned_count"] = 0

    @contextmanager
    def admin_headers():
        yield {"Cookie": "sessionid=private-admin-session"}

    monkeypatch.setattr(docker_smoke, "_disposable_admin_headers", admin_headers)
    monkeypatch.setattr(
        docker_smoke,
        "_request_text",
        lambda *args, **kwargs: change_html,
    )
    monkeypatch.setattr(
        docker_smoke,
        "_request_admin_json",
        lambda _base_url, path, **_kwargs: responses[path],
    )
    monkeypatch.setattr(
        docker_smoke,
        "_verify_existing_workflow_storage_contract",
        lambda **_kwargs: {
            "current_manifests": 1,
            "pending_manifests": 0,
            "unlinked_pages": 0,
        },
    )

    with pytest.raises(docker_smoke.DockerSmokeError):
        docker_smoke._verify_existing_workflow_admin_contract(
            base_url="http://127.0.0.1:8000",
            deadline=100.0,
            execution=execution,
        )


def _failed_admin_graph_fixture() -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    node_ids = ("0.0", "0.1", "0.2", "0.3")
    states = {
        "0.0": "SUCCEEDED",
        "0.1": "FAILED",
        "0.2": "FAILED",
        "0.3": "SUCCEEDED",
    }
    topology_nodes = [{"node_id": node_id} for node_id in node_ids]
    topology_edges = [
        {"source": "0.0", "target": "0.1"},
        {"source": "0.1", "target": "0.2"},
        {"source": "0.0", "target": "0.3"},
    ]
    node_details = [{"node_id": node_id, "state": state} for node_id, state in states.items()]
    graph = {
        "schema": "django-ray.admin-workflow-graph",
        "schema_version": 1,
        "status": "AVAILABLE",
        "message": "Bounded terminal workflow graph is available.",
        "complete": True,
        "counts": {"nodes": 4, "edges": 3},
        "limits": dict(docker_smoke._WORKFLOW_GRAPH_LIMITS),
        "nodes": [
            {
                "id": node_id,
                "label": f"Step {index}",
                "kind": "task",
                "state": states[node_id],
                "message": None,
                "error": (
                    "Intentional complex workflow fixture failure"
                    if states[node_id] == "FAILED"
                    else None
                ),
                "failure_path": node_id in {"0.0", "0.1"},
            }
            for index, node_id in enumerate(node_ids)
        ],
        "edges": list(topology_edges),
    }
    return graph, topology_nodes, topology_edges, node_details


def test_failed_admin_graph_retains_incoming_failure_path_and_sibling_context() -> None:
    graph, topology_nodes, topology_edges, node_details = _failed_admin_graph_fixture()

    assert docker_smoke._workflow_admin_graph_evidence(
        graph,
        execution_state="FAILED",
        topology_nodes=topology_nodes,
        topology_edges=topology_edges,
        node_details=node_details,
    ) == {
        "graph_status": "AVAILABLE",
        "graph_nodes": 4,
        "graph_edges": 3,
        "graph_pending_nodes": 0,
        "graph_running_nodes": 0,
        "graph_succeeded_nodes": 2,
        "graph_failed_nodes": 2,
        "graph_failure_path_nodes": 2,
        "graph_failure_origins": 1,
        "graph_incoming_failure_edges": 1,
    }


def test_failed_admin_graph_accepts_unfinished_downstream_nodes() -> None:
    graph, topology_nodes, topology_edges, node_details = _failed_admin_graph_fixture()
    extra_states = {"0.4": "PENDING", "0.5": "RUNNING"}
    for index, (node_id, state) in enumerate(extra_states.items(), start=4):
        topology_nodes.append({"node_id": node_id})
        node_details.append({"node_id": node_id, "state": state})
        graph["nodes"].append(
            {
                "id": node_id,
                "label": f"Step {index}",
                "kind": "task",
                "state": state,
                "message": None,
                "error": None,
                "failure_path": False,
            }
        )
    extra_edges = [
        {"source": "0.3", "target": "0.4"},
        {"source": "0.4", "target": "0.5"},
    ]
    topology_edges.extend(extra_edges)
    graph["edges"].extend(extra_edges)
    graph["counts"] = {"nodes": 6, "edges": 5}

    evidence = docker_smoke._workflow_admin_graph_evidence(
        graph,
        execution_state="FAILED",
        topology_nodes=topology_nodes,
        topology_edges=topology_edges,
        node_details=node_details,
    )

    assert evidence["graph_pending_nodes"] == 1
    assert evidence["graph_running_nodes"] == 1
    assert evidence["graph_succeeded_nodes"] == 2
    assert evidence["graph_failed_nodes"] == 2


def test_failed_admin_graph_does_not_treat_a_succeeded_ancestor_as_sibling() -> None:
    graph, topology_nodes, topology_edges, node_details = _failed_admin_graph_fixture()
    graph["nodes"][3]["state"] = "PENDING"
    graph["nodes"][3]["error"] = None
    node_details[3]["state"] = "PENDING"

    with pytest.raises(
        docker_smoke.DockerSmokeError,
        match="successful sibling context",
    ):
        docker_smoke._workflow_admin_graph_evidence(
            graph,
            execution_state="FAILED",
            topology_nodes=topology_nodes,
            topology_edges=topology_edges,
            node_details=node_details,
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "private_field",
        "wrong_failure_path",
        "no_successful_sibling",
        "no_incoming_origin",
    ],
)
def test_failed_admin_graph_rejects_private_or_inconsistent_failure_evidence(
    corruption: str,
) -> None:
    graph, topology_nodes, topology_edges, node_details = _failed_admin_graph_fixture()
    if corruption == "private_field":
        graph["nodes"][0]["runtime_env"] = {"env_vars": {"SECRET": "forbidden"}}
    elif corruption == "wrong_failure_path":
        graph["nodes"][0]["failure_path"] = False
    elif corruption == "no_successful_sibling":
        for node in graph["nodes"]:
            node["state"] = "FAILED"
            node["error"] = "failure"
        for detail in node_details:
            detail["state"] = "FAILED"
    else:
        graph["edges"] = [
            edge
            for edge in graph["edges"]
            if not (edge["source"] == "0.0" and edge["target"] == "0.1")
        ]
        topology_edges[:] = list(graph["edges"])
        graph["counts"]["edges"] = len(graph["edges"])

    with pytest.raises(docker_smoke.DockerSmokeError):
        docker_smoke._workflow_admin_graph_evidence(
            graph,
            execution_state="FAILED",
            topology_nodes=topology_nodes,
            topology_edges=topology_edges,
            node_details=node_details,
        )


def _stored_workflow_admin_execution():
    import hashlib

    from django.utils import timezone

    from django_ray.models import (
        RayTaskExecution,
        TaskState,
        WorkflowProgressRunStorage,
        WorkflowProgressTopologyCollection,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyManifestPage,
        WorkflowProgressTopologyPage,
        WorkflowProgressTopologySlot,
    )

    execution = RayTaskExecution.objects.create(
        task_id=WORKFLOW_TASK_ID,
        callable_path="testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark",
        state=TaskState.SUCCEEDED,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id="35200000-0000-4000-8000-000000000003",
    )
    detail_payload = b'{"nodes":3}'
    run_storage = WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=execution.workflow_run_id,
        detail_revision=1,
        detail_node_count=3,
        detail_succeeded_count=3,
        detail_encoded_bytes=len(detail_payload),
        detail_decoded_bytes=len(detail_payload),
    )
    manifest_payload = b'{"pages":2}'
    manifest = WorkflowProgressTopologyManifest.objects.create(
        run_storage=run_storage,
        topology_version=1,
        slot=WorkflowProgressTopologySlot.CURRENT,
        manifest_digest=hashlib.sha256(manifest_payload).hexdigest(),
        payload=manifest_payload,
        node_count=3,
        edge_count=2,
        node_page_count=1,
        edge_page_count=1,
        encoded_bytes=len(manifest_payload),
        decoded_bytes=len(manifest_payload),
        published_at=timezone.now(),
    )
    for collection, payload, item_count in (
        (WorkflowProgressTopologyCollection.NODE, b'{"nodes":3}', 3),
        (WorkflowProgressTopologyCollection.EDGE, b'{"edges":2}', 2),
    ):
        page = WorkflowProgressTopologyPage.objects.create(
            run_storage=run_storage,
            digest=hashlib.sha256(payload).hexdigest(),
            collection=collection,
            payload=payload,
            item_count=item_count,
            encoded_bytes=len(payload),
            decoded_bytes=len(payload),
        )
        WorkflowProgressTopologyManifestPage.objects.create(
            manifest=manifest,
            page=page,
            collection=collection,
            page_index=0,
        )
    return execution, run_storage


@pytest.mark.django_db
def test_existing_workflow_storage_requires_one_clean_current_publication() -> None:
    execution, _run_storage = _stored_workflow_admin_execution()

    assert docker_smoke._verify_existing_workflow_storage_contract(
        execution=execution,
        topology_nodes=3,
        topology_edges=2,
        node_details=3,
    ) == {
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
    }


@pytest.mark.django_db
def test_existing_failed_workflow_storage_accepts_terminal_mixed_detail() -> None:
    execution, run_storage = _stored_workflow_admin_execution()
    execution.state = "FAILED"
    execution.save(update_fields=["state"])
    run_storage.detail_succeeded_count = 2
    run_storage.detail_failed_count = 1
    run_storage.save(update_fields=["detail_succeeded_count", "detail_failed_count"])

    assert docker_smoke._verify_existing_workflow_storage_contract(
        execution=execution,
        topology_nodes=3,
        topology_edges=2,
        node_details=3,
        failed_nodes=1,
    ) == {
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
    }


@pytest.mark.django_db
def test_existing_failed_workflow_storage_accepts_unfinished_detail() -> None:
    execution, run_storage = _stored_workflow_admin_execution()
    execution.state = "FAILED"
    execution.save(update_fields=["state"])
    run_storage.detail_pending_count = 1
    run_storage.detail_running_count = 1
    run_storage.detail_succeeded_count = 0
    run_storage.detail_failed_count = 1
    run_storage.save(
        update_fields=[
            "detail_pending_count",
            "detail_running_count",
            "detail_succeeded_count",
            "detail_failed_count",
        ]
    )

    assert docker_smoke._verify_existing_workflow_storage_contract(
        execution=execution,
        topology_nodes=3,
        topology_edges=2,
        node_details=3,
        pending_nodes=1,
        running_nodes=1,
        failed_nodes=1,
    ) == {
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("residue", ["pending_manifest", "unlinked_page"])
def test_existing_workflow_storage_rejects_transitional_residue(residue: str) -> None:
    import hashlib

    from django_ray.models import (
        WorkflowProgressTopologyCollection,
        WorkflowProgressTopologyManifest,
        WorkflowProgressTopologyPage,
        WorkflowProgressTopologySlot,
    )

    execution, run_storage = _stored_workflow_admin_execution()
    payload = residue.encode()
    if residue == "pending_manifest":
        WorkflowProgressTopologyManifest.objects.create(
            run_storage=run_storage,
            topology_version=2,
            slot=WorkflowProgressTopologySlot.PENDING,
            manifest_digest=hashlib.sha256(payload).hexdigest(),
            payload=payload,
            node_count=0,
            edge_count=0,
            node_page_count=0,
            edge_page_count=0,
            encoded_bytes=len(payload),
            decoded_bytes=len(payload),
        )
    else:
        WorkflowProgressTopologyPage.objects.create(
            run_storage=run_storage,
            digest=hashlib.sha256(payload).hexdigest(),
            collection=WorkflowProgressTopologyCollection.NODE,
            payload=payload,
            item_count=1,
            encoded_bytes=len(payload),
            decoded_bytes=len(payload),
        )

    with pytest.raises(
        docker_smoke.DockerSmokeError,
        match="pending, duplicate-current, or unlinked topology storage",
    ):
        docker_smoke._verify_existing_workflow_storage_contract(
            execution=execution,
            topology_nodes=3,
            topology_edges=2,
            node_details=3,
        )


def test_unfold_stylesheet_match_accepts_manifest_hash() -> None:
    match = docker_smoke._UNFOLD_STYLESHEET_RE.search(
        '<link href="/static/unfold/css/styles.0123456789ab.css" rel="stylesheet">'
    )

    assert match is not None
    assert match.group("path") == "/static/unfold/css/styles.0123456789ab.css"


def test_django_ray_admin_assets_accept_manifest_hashes() -> None:
    stylesheet_match = docker_smoke._DJANGO_RAY_STYLESHEET_RE.search(
        '<link href="/static/testproject/admin.0123456789ab.css" rel="stylesheet">'
    )
    icon_match = docker_smoke._DJANGO_RAY_ICON_RE.search(
        '<img src="/static/testproject/django-ray.abcdef012345.svg" alt="Home">'
    )

    assert stylesheet_match is not None
    assert stylesheet_match.group("path") == "/static/testproject/admin.0123456789ab.css"
    assert icon_match is not None
    assert icon_match.group("path") == "/static/testproject/django-ray.abcdef012345.svg"


def test_admin_text_request_uses_remaining_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 100.0

    def open_request(_request, *, timeout):
        assert timeout == pytest.approx(0.25)
        return _TextResponse("body", content_type="text/css; charset=utf-8")

    monkeypatch.setattr(docker_smoke.time, "monotonic", lambda: current_time)
    monkeypatch.setattr(docker_smoke.urllib.request, "urlopen", open_request)

    assert (
        docker_smoke._request_text(
            "http://web:8000",
            "/static/unfold/css/styles.0123456789ab.css",
            expected_content_type="text/css",
            deadline=current_time + 0.25,
        )
        == "body"
    )


@pytest.mark.parametrize(
    "content_type",
    ("text/javascript; charset=utf-8", "application/javascript"),
)
def test_admin_text_request_accepts_standard_javascript_content_types(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
) -> None:
    monkeypatch.setattr(
        docker_smoke.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _TextResponse(
            "window.djangoRay = true;",
            content_type=content_type,
        ),
    )

    assert (
        docker_smoke._request_text(
            "http://web:8000",
            "/static/django_ray/admin/workflow_diagnostics.js",
            expected_content_type=("text/javascript", "application/javascript"),
        )
        == "window.djangoRay = true;"
    )


def test_admin_text_request_rejects_expired_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_smoke.time, "monotonic", lambda: 100.0)

    with pytest.raises(docker_smoke.DockerSmokeError, match="deadline expired"):
        docker_smoke._request_text(
            "http://web:8000",
            "/admin/",
            deadline=99.0,
        )


def test_admin_text_request_rejects_wrong_static_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docker_smoke.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _TextResponse("<html>redirected</html>"),
    )

    with pytest.raises(docker_smoke.DockerSmokeError, match="expected text/css"):
        docker_smoke._request_text(
            "http://web:8000",
            "/static/unfold/css/styles.0123456789ab.css",
            expected_content_type="text/css",
        )


def test_admin_smoke_cleanup_attempts_user_delete_when_session_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django.contrib.auth
    import django.contrib.sessions.backends.db

    cleanup_events: list[str] = []

    class FakeUser:
        pk = 1

        def __init__(self, **_kwargs: object) -> None:
            pass

        def set_unusable_password(self) -> None:
            pass

        def save(self) -> None:
            cleanup_events.append("user-save")

        def delete(self) -> None:
            cleanup_events.append("user-delete")

        def get_session_auth_hash(self) -> str:
            return "session-hash"

    class FakeSession(dict[str, str]):
        session_key = "session-key"

        def save(self) -> None:
            cleanup_events.append("session-save")

        def delete(self, session_key: str) -> None:
            assert session_key == self.session_key
            cleanup_events.append("session-delete")
            raise RuntimeError("session cleanup failed")

    def fail_request(*_args: object, **_kwargs: object) -> str:
        raise docker_smoke.DockerSmokeError("admin request failed")

    monkeypatch.setattr(django.contrib.auth, "get_user_model", lambda: FakeUser)
    monkeypatch.setattr(django.contrib.sessions.backends.db, "SessionStore", FakeSession)
    monkeypatch.setattr(docker_smoke, "_request_text", fail_request)

    with pytest.raises(RuntimeError, match="session cleanup failed"):
        docker_smoke._verify_unfold_admin_contract(
            base_url="http://web:8000",
            deadline=docker_smoke.time.monotonic() + 5,
            execution=SimpleNamespace(pk=1, state="QUEUED"),
            attempt=SimpleNamespace(pk=1, attempt_number=1, state="SUCCEEDED"),
        )

    assert cleanup_events == [
        "user-save",
        "session-save",
        "session-delete",
        "user-delete",
    ]


def test_response_json_rejects_oversized_payload() -> None:
    response = _Response({"value": "x" * docker_smoke._MAX_RESPONSE_BYTES})

    with pytest.raises(docker_smoke.DockerSmokeError, match="byte limit"):
        docker_smoke._response_json(response)
