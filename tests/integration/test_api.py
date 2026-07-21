"""Integration tests for the Django Ninja API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from django_ray import __version__ as django_ray_version
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.workflow_progress_summary import serialize_workflow_progress_summary
from tests.workflow_progress_summary_helpers import workflow_progress_summary

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    """Django test client."""
    return Client(HTTP_AUTHORIZATION="Bearer test-api-token-for-pytest")


@pytest.mark.django_db
class TestLandingPage:
    """Test the sample project landing page."""

    def test_landing_page(self):
        """Test the root page renders links and task stats."""
        RayTaskExecution.objects.create(
            task_id="landing-test-1",
            callable_path="test.task",
            queue_name="default",
            state=TaskState.SUCCEEDED,
        )

        response = Client().get("/")
        assert response.status_code == 200

        content = response.content.decode("utf-8")
        assert "django-ray" in content
        assert f"v{django_ray_version} / debug" in content
        assert "/static/testproject/django-ray.svg" in content
        assert "/static/testproject/landing-graph-bg.png" in content
        assert "bundled testproject" in content
        assert "/api/docs" in content
        assert "/admin/" in content
        assert "https://github.com/dariuszpanas/django-ray" in content
        assert "https://django-ray.readthedocs.io/en/latest/" in content
        assert "https://pypi.org/project/django-ray/" in content
        assert "/static/testproject/landing.js" in content
        assert 'id="api-token"' in content
        assert 'type="password"' in content
        assert 'name="api-token"' not in content
        assert 'id="use-token"' in content
        assert 'id="view-metrics"' in content
        assert 'id="view-executions"' in content
        assert 'id="stat-succeeded">1</strong>' in content

    def test_browser_auth_contract_does_not_embed_or_persist_token(self, settings):
        """The browser supplies its credential without server or browser persistence."""
        configured_token = "configured-browser-token-must-not-leak-issue-144"
        settings.DJANGO_API_TOKEN = configured_token

        response = Client().get("/")
        assert response.status_code == 200

        content = response.content.decode("utf-8")
        script_path = REPOSITORY_ROOT / "testproject/static/testproject/landing.js"
        script = script_path.read_text(encoding="utf-8")

        assert configured_token not in content
        assert configured_token not in script
        assert "DJANGO_API_TOKEN" not in response.context
        assert 'href="/api/metrics"' not in content
        assert 'href="/api/executions"' not in content
        assert script.count("window.fetch(") == 1
        assert 'headers.set("Authorization", `Bearer ${requestToken}`)' in script
        for endpoint in (
            "/api/executions/stats",
            "/api/enqueue/add/2/3",
            "/api/metrics",
            "/api/executions",
        ):
            assert endpoint in script
        for browser_store in ("localStorage", "sessionStorage", "document.cookie"):
            assert browser_store not in script

        token_bytes = configured_token.encode()
        static_root = REPOSITORY_ROOT / "testproject/static/testproject"
        for static_asset in static_root.rglob("*"):
            if static_asset.is_file():
                assert token_bytes not in static_asset.read_bytes()

    def test_browser_auth_javascript_executes_credentialed_actions(self):
        """Exercise event wiring, bearer headers, and stale credential responses."""
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI"):
                pytest.fail("Node.js is required for the dashboard browser contract in CI")
            pytest.skip("Node.js is unavailable for the dashboard browser contract")

        result = subprocess.run(
            [node, "--test", "tests/javascript/landing_auth.test.mjs"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "authorization",
        [None, "Bearer invalid-browser-token"],
        ids=["missing", "invalid"],
    )
    def test_browser_actions_reject_missing_and_invalid_credentials(self, authorization):
        """Every dashboard action remains protected for missing or invalid tokens."""
        client_kwargs = {}
        if authorization is not None:
            client_kwargs["HTTP_AUTHORIZATION"] = authorization
        browser_client = Client(**client_kwargs)

        responses = (
            browser_client.get("/api/executions/stats"),
            browser_client.post("/api/enqueue/add/2/3"),
            browser_client.get("/api/metrics"),
            browser_client.get("/api/executions"),
        )

        assert [response.status_code for response in responses] == [401, 401, 401, 401]

    def test_browser_actions_accept_valid_credentials(self, settings):
        """The shared browser bearer flow can use every protected dashboard action."""
        browser_client = Client(
            HTTP_AUTHORIZATION=f"Bearer {settings.DJANGO_API_TOKEN}",
        )

        stats_response = browser_client.get("/api/executions/stats")
        enqueue_response = browser_client.post("/api/enqueue/add/2/3")
        metrics_response = browser_client.get("/api/metrics")
        executions_response = browser_client.get("/api/executions")

        assert stats_response.status_code == 200
        assert enqueue_response.status_code == 200
        assert enqueue_response.json()["status"] == "READY"
        assert metrics_response.status_code == 200
        assert executions_response.status_code == 200


@pytest.mark.django_db
class TestHealthAPI:
    """Test the /api/health endpoint."""

    def test_liveness_check(self, client):
        """Test the lightweight liveness endpoint."""
        response = client.get("/api/livez")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
        assert data["version"] == django_ray_version

    def test_readiness_check(self, client):
        """Test the readiness endpoint."""
        response = client.get("/api/readyz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"
        assert data["version"] == django_ray_version

    def test_health_check(self, client):
        """Test the health check endpoint."""
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"
        assert data["version"] == django_ray_version

    def test_prometheus_metrics(self, client):
        """Test the Prometheus metrics endpoint."""
        # Create some tasks to have metrics data
        RayTaskExecution.objects.create(
            task_id="metrics-test-1",
            callable_path="test.task",
            queue_name="default",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="metrics-test-2",
            callable_path="test.task",
            queue_name="default",
            state=TaskState.RUNNING,
        )
        RayTaskExecution.objects.create(
            task_id="metrics-test-3",
            callable_path="test.task",
            queue_name="high-priority",
            state=TaskState.QUEUED,
        )

        response = client.get("/api/metrics")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"

        content = response.content.decode("utf-8")
        # Check for expected metric names
        assert "django_ray_tasks_total" in content
        assert "django_ray_tasks_queued" in content
        assert "django_ray_tasks_running" in content
        assert "django_ray_queue_depth" in content
        assert "django_ray_queue_wait_seconds_count" in content
        assert "django_ray_worker_leases" in content
        # Check for state labels
        assert 'state="QUEUED"' in content
        assert 'state="RUNNING"' in content
        # Check for queue labels
        assert 'queue="default"' in content
        assert 'queue="high-priority"' in content

    def test_prometheus_metrics_query_count_is_bounded(self, client):
        """Metrics scrape cost stays constant as states and queues grow."""
        RayTaskExecution.objects.create(
            task_id="metrics-query-test-1",
            callable_path="test.task",
            queue_name="default",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="metrics-query-test-2",
            callable_path="test.task",
            queue_name="high-priority",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="metrics-query-test-3",
            callable_path="test.task",
            queue_name="default",
            state=TaskState.RUNNING,
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/metrics")

        assert response.status_code == 200
        assert len(queries) <= 8

    def test_operational_routes_require_bearer_token(self):
        """Only health probes are public; task/metrics data needs explicit auth."""
        unauthenticated_client = Client()

        response = unauthenticated_client.get("/api/executions/stats")
        assert response.status_code == 401

        response = unauthenticated_client.get("/api/metrics")
        assert response.status_code == 401

        response = unauthenticated_client.get("/api/health")
        assert response.status_code == 200


@pytest.mark.django_db
class TestEnqueueAPI:
    """Test the /api/enqueue/* endpoints using Django 6 native task API."""

    def test_enqueue_add(self, client):
        """Test enqueueing an add_numbers task."""
        response = client.post("/api/enqueue/add/10/20")
        assert response.status_code == 200
        data = response.json()
        # Django 6 API returns TaskResult
        assert "task_id" in data
        assert data["status"] == "READY"  # Our backend returns READY for queued
        assert data["args"] == [10, 20]

        # Verify in database
        task = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert task.callable_path == "testproject.tasks.add_numbers"
        assert task.state == TaskState.QUEUED

    def test_enqueue_multiply(self, client):
        """Test enqueueing a multiply_numbers task."""
        response = client.post("/api/enqueue/multiply/5/6")
        assert response.status_code == 200
        data = response.json()
        assert data["args"] == [5, 6]
        assert data["status"] == "READY"

    def test_enqueue_slow(self, client):
        """Test enqueueing a slow_task."""
        response = client.post("/api/enqueue/slow/2.5")
        assert response.status_code == 200
        data = response.json()
        assert data["kwargs"] == {"seconds": 2.5}
        assert data["status"] == "READY"

    def test_enqueue_fail(self, client):
        """Test enqueueing a failing_task."""
        response = client.post("/api/enqueue/fail")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "READY"  # Not executed yet, just queued

    def test_enqueue_cpu(self, client):
        """Test enqueueing a cpu_intensive_task."""
        response = client.post("/api/enqueue/cpu/1000")
        assert response.status_code == 200
        data = response.json()
        assert data["kwargs"] == {"n": 1000}
        assert data["status"] == "READY"

    def test_enqueue_with_queue(self, client):
        """Test enqueueing with a specific queue."""
        response = client.post("/api/enqueue/add/1/2?queue=high-priority")
        assert response.status_code == 200

        # Verify queue name
        data = response.json()
        task = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert task.queue_name == "high-priority"

    def test_enqueue_workflow_benchmark_creates_one_durable_task(self, client):
        response = client.post("/api/cluster/workflow-benchmark?num_items=6&seconds_per_item=0.1")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "READY"
        assert data["kwargs"] == {
            "num_items": 6,
            "seconds_per_item": 0.1,
        }
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert execution.callable_path == (
            "testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark"
        )
        assert RayTaskExecution.objects.count() == 1

    def test_enqueue_complex_workflow_creates_one_durable_task(self, client):
        response = client.post(
            "/api/cluster/complex-workflow"
            "?fast_items=3&slow_items=2&fast_seconds=0.01&slow_seconds=0.05"
        )

        assert response.status_code == 200
        data = response.json()
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert execution.callable_path == (
            "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
        )
        assert RayTaskExecution.objects.count() == 1

    def test_enqueue_runtime_env_benchmark_creates_one_durable_task(self, client):
        response = client.post(
            "/api/cluster/runtime-env/benchmark?profile=numpy-2-3&package=numpy&repeats=3"
        )

        assert response.status_code == 200
        data = response.json()
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert execution.callable_path == (
            "testproject.apps.cluster_tasks.tasks.runtime_env_benchmark"
        )
        assert json.loads(execution.kwargs_json) == {
            "package": "numpy",
            "profile": "numpy-2-3",
            "repeats": 3,
        }
        assert RayTaskExecution.objects.count() == 1


@pytest.mark.django_db
class TestTasksAPI:
    """Test the /api/tasks/{task_id} endpoint for retrieving task results."""

    def test_get_task_by_uuid(self, client):
        """Test getting a task by its UUID."""
        # First enqueue a task
        response = client.post("/api/enqueue/add/1/1")
        assert response.status_code == 200
        task_id = response.json()["task_id"]

        # Now retrieve it
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == task_id
        assert data["args"] == [1, 1]

    def test_get_workflow_benchmark_result(self, client):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-result-001",
            callable_path=("testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark"),
            queue_name="default",
            state=TaskState.SUCCEEDED,
            result_data=(
                '{"engine": "django-ray-workflow", "leaf_tasks": 4, "effective_parallelism": 3.5}'
            ),
            progress_data=(
                '{"state": "SUCCEEDED", "total_nodes": 7, '
                '"completed_nodes": 7, "progress_percent": 100.0}'
            ),
        )

        response = client.get(f"/api/cluster/workflow-benchmark/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == TaskState.SUCCEEDED
        assert data["result"]["leaf_tasks"] == 4
        assert data["result"]["effective_parallelism"] == 3.5
        assert data["progress"]["completed_nodes"] == 7
        assert data["progress"]["progress_percent"] == 100.0

    def test_get_runtime_env_result_includes_environment_identity(self, client):
        execution = RayTaskExecution.objects.create(
            task_id="runtime-env-result-001",
            callable_path=("testproject.apps.cluster_tasks.tasks.runtime_env_probe"),
            queue_name="default",
            state=TaskState.SUCCEEDED,
            runtime_env_profile="numpy-2-3",
            runtime_env_hash="a" * 64,
            runtime_env_json='{"pip":["numpy==2.3.5"]}',
            result_data=(
                '{"profile_marker": "numpy-2-3", "package": "numpy", "package_version": "2.3.5"}'
            ),
        )

        response = client.get(f"/api/cluster/runtime-env/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["runtime_env_profile"] == "numpy-2-3"
        assert data["runtime_env_hash"] == "a" * 64
        assert data["result"]["package_version"] == "2.3.5"

    def test_get_workflow_benchmark_resolves_backend_result(self, client, monkeypatch):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-result-002",
            callable_path=("testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark"),
            queue_name="default",
            state=TaskState.SUCCEEDED,
            runtime_env_profile="project",
            runtime_env_hash="b" * 64,
            result_data=None,
            progress_data='{"state": "SUCCEEDED", "total_nodes": 1, "completed_nodes": 1}',
        )

        class _Backend:
            def __init__(self, value):
                self.value = value

            def get_result(self, task_id):
                assert task_id == "workflow-result-002"
                return SimpleNamespace(return_value=self.value)

        monkeypatch.setattr(
            "testproject.api.task_backends",
            {"default": _Backend({"leaf_tasks": 99})},
        )

        response = client.get(f"/api/cluster/workflow-benchmark/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["result"]["leaf_tasks"] == 99

    def test_get_runtime_env_result_resolves_profile_backend_result(self, client, monkeypatch):
        execution = RayTaskExecution.objects.create(
            task_id="runtime-env-result-002",
            callable_path=("testproject.apps.cluster_tasks.tasks.runtime_env_probe"),
            queue_name="default",
            state=TaskState.SUCCEEDED,
            runtime_env_profile="numpy-2-3",
            runtime_env_hash="c" * 64,
            result_data=None,
        )

        class _Backend:
            def __init__(self, value):
                self.value = value

            def get_result(self, task_id):
                assert task_id == "runtime-env-result-002"
                return SimpleNamespace(return_value=self.value)

        monkeypatch.setattr(
            "testproject.api.task_backends",
            {
                "default": _Backend({"source": "default"}),
                "numpy-2-3": _Backend({"source": "numpy-2-3"}),
            },
        )

        response = client.get(f"/api/cluster/runtime-env/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["result"]["source"] == "numpy-2-3"

    def test_get_workflow_graph_returns_ui_ready_nodes_and_edges(self, client):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-graph-001",
            callable_path=("testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"),
            queue_name="default",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000000033",
        )
        execution.progress_data = json.dumps(
            {
                "schema_version": 2,
                "run_identity": {
                    "schema_version": 1,
                    "run_id": str(execution.workflow_run_id),
                    "task_execution_pk": execution.pk,
                    "attempt_number": execution.attempt_number,
                    "execution_generation": execution.execution_generation,
                },
                "revision": 8,
                "state": "RUNNING",
                "total_nodes": 2,
                "completed_nodes": 1,
                "failed_nodes": 0,
                "running_nodes": 1,
                "pending_nodes": 0,
                "progress_percent": 50.0,
                "updated_at": 123.5,
                "graph": {
                    "nodes": [
                        {
                            "node_id": "0.0",
                            "state": "SUCCEEDED",
                            "dependencies": [],
                            "execution": {"ray_task_id": "ray-1"},
                        },
                        {
                            "node_id": "0.1",
                            "state": "RUNNING",
                            "dependencies": ["0.0"],
                            "execution": {"ray_task_id": "ray-2"},
                        },
                    ],
                    "edges": [{"source": "0.0", "target": "0.1"}],
                },
                "recent_events": [],
            }
        )
        execution.save(update_fields=["progress_data"])

        response = client.get(f"/api/cluster/workflows/{execution.task_id}/graph")

        assert response.status_code == 200
        data = response.json()
        assert data["revision"] == 8
        assert data["run_identity"]["run_id"] == str(execution.workflow_run_id)
        assert data["graph"]["edges"] == [{"source": "0.0", "target": "0.1"}]
        assert data["graph"]["nodes"][1]["execution"]["ray_task_id"] == "ray-2"

    def test_bounded_v3_summary_is_public_but_legacy_graph_route_is_unavailable(self, client):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-v3-api-001",
            callable_path=("testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark"),
            queue_name="default",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000000125",
            progress_data="legacy-graph" * 1_000,
        )
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
            workflow_progress_summary(execution, published_detail=True)
        )
        execution.save(update_fields=["workflow_progress_summary_json"])

        with CaptureQueriesContext(connection) as queries:
            progress_response = client.get(f"/api/cluster/workflow-benchmark/{execution.task_id}")

        assert progress_response.status_code == 200
        progress = progress_response.json()["progress"]
        assert progress["schema_version"] == 3
        assert "task_execution_pk" not in progress["run_identity"]
        assert progress["storage"]["manifest_id"] is None
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 2
        assert "progress_data" not in task_selects[0]
        assert "workflow_progress_summary_json" not in task_selects[0]

        graph_response = client.get(f"/api/cluster/workflows/{execution.task_id}/graph")
        assert graph_response.status_code == 404

    def test_get_workflow_node_returns_durable_metadata_without_ray_id(self, client):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-node-001",
            callable_path=("testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark"),
            queue_name="default",
            state=TaskState.RUNNING,
            progress_data=json.dumps(
                {
                    "graph": {
                        "nodes": [
                            {
                                "node_id": "0.0",
                                "label": "prepare",
                                "dependencies": [],
                                "execution": {},
                            }
                        ],
                        "edges": [],
                    }
                }
            ),
        )

        response = client.get(f"/api/cluster/workflows/{execution.task_id}/nodes/0.0")

        assert response.status_code == 200
        data = response.json()
        assert data["node"]["label"] == "prepare"
        assert data["ray_state"] is None
        assert data["logs"] is None

    def test_indexed_workflow_node_does_not_scan_legacy_graph(self, client):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-indexed-node-001",
            callable_path=("testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark"),
            queue_name="default",
            state=TaskState.RUNNING,
            progress_data=json.dumps(
                {
                    "graph": {
                        "nodes": [
                            {
                                "node_id": "namespace/apply",
                                "label": "prepare",
                                "dependencies": [],
                                "execution": {},
                            }
                        ],
                        "edges": [],
                    }
                }
            ),
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.get(
                f"/api/cluster/workflows/{execution.task_id}/node-detail",
                {"node_id": "namespace/apply"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["availability"] == "NOT_REPORTED"
        assert data["found"] is False
        assert data["item"] is None
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        assert all("progress_data" not in query for query in task_selects)


@pytest.mark.django_db
class TestExecutionsAPI:
    """Test the /api/executions/* admin endpoints."""

    def test_list_executions_empty(self, client):
        """Test listing executions when none exist."""
        response = client.get("/api/executions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["tasks"] == []

    def test_list_executions_with_data(self, client):
        """Test listing executions with existing tasks."""
        RayTaskExecution.objects.create(
            task_id="test-1",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="test-2",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
        )

        response = client.get("/api/executions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["queued"] == 1
        assert data["succeeded"] == 1

    def test_list_executions_filter_by_state(self, client):
        """Test filtering executions by state."""
        RayTaskExecution.objects.create(
            task_id="test-1",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="test-2",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
        )

        response = client.get("/api/executions?state=QUEUED")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["state"] == "QUEUED"

    def test_get_execution(self, client):
        """Test getting a specific execution by internal ID."""
        task = RayTaskExecution.objects.create(
            task_id="test-get",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
        )

        response = client.get(f"/api/executions/{task.pk}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == task.pk
        assert data["callable_path"] == "testproject.tasks.add_numbers"

    def test_execution_payload_redacts_sensitive_fields(self, client, settings):
        """Operator-facing execution details must not echo stored secrets."""
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        task = RayTaskExecution.objects.create(
            task_id="test-redacted-api",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            args_json='[{"password":"api-secret"}]',
            kwargs_json='{"safe":"visible"}',
            result_data='{"password":"result-secret","safe":1}',
            progress_data='{"safe":"visible"}',
            error_message="password=error-secret",
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        payload = response.json()
        serialized = str(payload)
        assert "api-secret" not in serialized
        assert "result-secret" not in serialized
        assert "error-secret" not in serialized
        assert "[REDACTED]" in serialized
        assert "progress_data" not in payload

    def test_execution_monitoring_reads_defer_complete_progress_payloads(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-bounded-execution-api",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="legacy-graph" * 10_000,
            workflow_progress_summary_json="summary" * 10_000,
        )

        with CaptureQueriesContext(connection) as queries:
            detail = client.get(f"/api/executions/{task.pk}")
            listing = client.get("/api/executions")

        assert detail.status_code == 200
        assert listing.status_code == 200
        assert "progress_data" not in detail.json()
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        assert all("progress_data" not in query for query in task_selects)
        assert all("workflow_progress_summary_json" not in query for query in task_selects)

    def test_get_execution_not_found(self, client):
        """Test getting a non-existent execution."""
        response = client.get("/api/executions/99999")
        assert response.status_code == 404

    def test_delete_execution(self, client):
        """Test deleting an execution."""
        task = RayTaskExecution.objects.create(
            task_id="test-delete",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        response = client.delete(f"/api/executions/{task.pk}")
        assert response.status_code == 200

        # Verify deleted
        assert not RayTaskExecution.objects.filter(pk=task.pk).exists()

    def test_cancel_queued_execution(self, client):
        """Test cancelling a queued execution."""
        task = RayTaskExecution.objects.create(
            task_id="test-cancel",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        response = client.post(f"/api/executions/{task.pk}/cancel")
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "CANCELLED"

    def test_retry_failed_execution(self, client):
        """Test retrying a failed execution."""
        task = RayTaskExecution.objects.create(
            task_id="test-retry",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="Some error",
            attempt_number=1,
        )

        response = client.post(f"/api/executions/{task.pk}/retry")
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "QUEUED"
        assert data["attempt_number"] == 2

    def test_reset_executions(self, client):
        """Test resetting stuck executions."""
        running = RayTaskExecution.objects.create(
            task_id="test-running",
            callable_path="test.task",
            state=TaskState.RUNNING,
            progress_data='{"revision":9}',
            workflow_run_id="00000000-0000-0000-0000-000000000125",
        )
        terminal = serialize_workflow_progress_summary(
            workflow_progress_summary(running, state="FAILED")
        )
        running.workflow_progress_summary_json = terminal
        running.save(update_fields=["workflow_progress_summary_json"])
        RayTaskExecution.objects.create(
            task_id="test-failed",
            callable_path="test.task",
            state=TaskState.FAILED,
        )

        response = client.post("/api/executions/reset")
        assert response.status_code == 200
        data = response.json()
        assert "2" in data["message"]

        # Verify all reset to QUEUED
        assert RayTaskExecution.objects.filter(state=TaskState.QUEUED).count() == 2
        running.refresh_from_db()
        assert running.progress_data is None
        assert running.workflow_progress_summary_json is None
        assert running.attempt_number == 2
        assert (
            TaskAttempt.objects.get(
                execution=running,
                attempt_number=1,
            ).workflow_progress_summary_json
            == terminal
        )

    def test_get_stats(self, client):
        """Test getting execution statistics."""
        RayTaskExecution.objects.create(
            task_id="test-1", callable_path="test", state=TaskState.QUEUED
        )
        RayTaskExecution.objects.create(
            task_id="test-2", callable_path="test", state=TaskState.SUCCEEDED
        )
        RayTaskExecution.objects.create(
            task_id="test-3", callable_path="test", state=TaskState.FAILED
        )

        response = client.get("/api/executions/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["queued"] == 1
        assert data["succeeded"] == 1
        assert data["failed"] == 1

    def test_get_stats_uses_single_grouped_query(self, client):
        """Stats endpoint should aggregate states in one query."""
        RayTaskExecution.objects.create(
            task_id="stats-query-test-1",
            callable_path="test",
            state=TaskState.QUEUED,
        )
        RayTaskExecution.objects.create(
            task_id="stats-query-test-2",
            callable_path="test",
            state=TaskState.SUCCEEDED,
        )
        RayTaskExecution.objects.create(
            task_id="stats-query-test-3",
            callable_path="test",
            state=TaskState.FAILED,
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/executions/stats")

        assert response.status_code == 200
        assert len(queries) == 1
