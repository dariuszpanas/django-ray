"""Integration tests for the Django Ninja API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from django_ray import __version__ as django_ray_version
from django_ray.lifecycle import (
    TaskCancellationRequestResult,
    TaskCancellationRequestStatus,
    TaskRetryRequestResult,
    TaskRetryRequestStatus,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from testproject import api as testproject_api
from tests.integration._api_helpers import build_demo_client
from tests.workflow_progress_summary_helpers import workflow_progress_summary

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(settings, db) -> Client:
    """Explicitly opted-in demo client for existing positive route coverage."""
    return build_demo_client(settings)


@pytest.fixture
def unauthenticated_client() -> Client:
    return Client()


def test_retry_outcome_supports_django_ninja_without_status_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(testproject_api, "_NINJA_STATUS", None)

    response = testproject_api._retry_execution_outcome(
        TaskRetryRequestResult(
            status=TaskRetryRequestStatus.NOT_FOUND,
            execution_id=321,
            state=None,
            attempt_number=None,
            execution_generation=None,
        ),
        status_code=404,
    )

    assert response == (
        404,
        {
            "code": "NOT_FOUND",
            "message": "The execution was not found.",
            "execution_id": 321,
            "state": None,
            "attempt_number": None,
            "execution_generation": None,
            "next_action": "Verify the execution identifier and object authorization.",
        },
    )


def test_cancellation_outcome_is_a_fixed_bounded_http_response() -> None:
    request = SimpleNamespace()
    response = testproject_api._cancellation_execution_outcome(
        request,
        TaskCancellationRequestResult(
            status=TaskCancellationRequestStatus.STALE_GENERATION,
            execution_id=321,
            state=TaskState.QUEUED,
            attempt_number=2,
            execution_generation=4,
        ),
        status_code=409,
    )

    assert response.status_code == 409
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert len(response.content) <= testproject_api._CANCELLATION_RESPONSE_MAX_BYTES
    assert json.loads(response.content) == {
        "code": "STALE_GENERATION",
        "message": "The execution generation changed before cancellation could be applied.",
        "execution_id": 321,
        "state": TaskState.QUEUED,
        "attempt_number": 2,
        "execution_generation": 4,
        "next_action": "Refresh and re-authorize the current attempt before cancelling.",
        "response_max_bytes": testproject_api._CANCELLATION_RESPONSE_MAX_BYTES,
    }


def test_retry_outcome_uses_a_fixed_fail_closed_message_for_unmapped_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(testproject_api, "_NINJA_STATUS", None)
    monkeypatch.delitem(
        testproject_api._RETRY_EXECUTION_MESSAGES,
        TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL,
    )

    response = testproject_api._retry_execution_outcome(
        TaskRetryRequestResult(
            status=TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL,
            execution_id=321,
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=4,
        ),
        status_code=409,
    )

    assert isinstance(response, tuple)
    assert response[0] == 409
    assert isinstance(response[1], dict)
    assert response[1]["message"] == "The retry request was not accepted."


def test_cancellation_outcome_uses_a_fixed_fail_closed_message_for_unmapped_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(
        testproject_api._CANCELLATION_EXECUTION_MESSAGES,
        TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL,
    )

    response = testproject_api._cancellation_execution_outcome(
        SimpleNamespace(),
        TaskCancellationRequestResult(
            status=TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL,
            execution_id=321,
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=4,
        ),
        status_code=409,
    )

    assert response.status_code == 409
    assert json.loads(response.content)["message"] == ("The cancellation request was not accepted.")


def test_browser_auth_javascript_executes_credentialed_actions() -> None:
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
        RayTaskExecution.objects.create(
            task_id="landing-test-expired",
            callable_path="test.task",
            queue_name="default",
            state=TaskState.EXPIRED,
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
        assert "Reloading starts unauthenticated" in content
        assert 'id="stat-succeeded">1</strong>' in content
        assert 'id="stat-expired">1</strong>' in content

    def test_browser_auth_contract_does_not_embed_or_leak_token(self, settings):
        """The loaded page retains its credential without persistence or leakage."""
        configured_token = "configured-browser-token-must-not-leak-issue-162"
        settings.DJANGO_API_TOKEN = configured_token

        response = Client().get("/")
        assert response.status_code == 200

        content = response.content.decode("utf-8")

        # Check the actual JS output target, not another live region on the page.
        class ActionStatusParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.outputs = []

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if attributes.get("id") == "output":
                    self.outputs.append(attributes)

        parser = ActionStatusParser()
        parser.feed(content)
        parser.close()
        assert len(parser.outputs) == 1
        status = parser.outputs[0]
        assert status.get("role") == "status"
        assert status.get("aria-live", "polite") == "polite"
        assert status.get("aria-atomic", "true") == "true"

        script_path = REPOSITORY_ROOT / "testproject/static/testproject/landing.js"
        script = script_path.read_text(encoding="utf-8")

        assert configured_token not in content
        assert configured_token not in script
        assert "DJANGO_API_TOKEN" not in response.context
        assert 'href="/api/metrics"' not in content
        assert 'href="/api/executions"' not in content
        assert script.count("window.fetch(") == 1
        assert 'headers.set("Authorization", `Bearer ${requestToken}`)' in script
        assert 'let apiToken = "";' in script
        assert "let credentialGeneration = 0;" in script
        assert "restoreCredential" not in script
        assert 'const legacySessionCredentialKey = "django-ray.testproject.api-token.v1";' in script
        assert "window.sessionStorage.removeItem(legacySessionCredentialKey)" in script
        assert "window.sessionStorage.getItem" not in script
        assert "window.sessionStorage.setItem" not in script
        for endpoint in (
            "/api/executions/stats",
            "/api/enqueue/add/2/3",
            "/api/metrics",
            "/api/executions",
        ):
            assert endpoint in script
        for leak_path in (
            "localStorage",
            "indexedDB",
            "document.cookie",
            "window.name",
            "window.location",
            "URLSearchParams",
        ):
            assert leak_path not in script

        token_bytes = configured_token.encode()
        static_root = REPOSITORY_ROOT / "testproject/static/testproject"
        for static_asset in static_root.rglob("*"):
            if static_asset.is_file():
                assert token_bytes not in static_asset.read_bytes()

    def test_browser_actions_reject_invalid_credentials(self):
        """Every dashboard action rejects a present but invalid bearer token."""
        # Successful counterparts stay in the dedicated stats, enqueue-add,
        # Prometheus-metrics, and execution-listing behavior tests below.
        browser_client = Client(
            HTTP_AUTHORIZATION="Bearer invalid-browser-token",
        )

        responses = (
            browser_client.get("/api/executions/stats"),
            browser_client.post("/api/enqueue/add/2/3"),
            browser_client.get("/api/metrics"),
            browser_client.get("/api/executions"),
        )

        assert [response.status_code for response in responses] == [401, 401, 401, 401]


@pytest.mark.django_db
class TestHealthAPI:
    """Test the /api/health endpoint."""

    def test_liveness_check(self, unauthenticated_client):
        """Test the lightweight liveness endpoint."""
        response = unauthenticated_client.get("/api/livez")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
        assert data["version"] == django_ray_version

    def test_readiness_check(self, unauthenticated_client):
        """Test the readiness endpoint."""
        response = unauthenticated_client.get("/api/readyz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"
        assert data["version"] == django_ray_version

    def test_health_check(self, unauthenticated_client):
        """Test the health check endpoint."""
        response = unauthenticated_client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"
        assert data["version"] == django_ray_version

    def test_readiness_check_returns_503_when_database_fails(
        self, unauthenticated_client, monkeypatch
    ):
        """Readiness probe returns HTTP 503 when database check fails while health stays 200."""
        from django.db import connection

        def broken_cursor():
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(connection, "cursor", broken_cursor)

        ready_response = unauthenticated_client.get("/api/readyz")
        assert ready_response.status_code == 503
        assert ready_response.json() == {
            "status": "degraded",
            "database": "error",
            "version": django_ray_version,
        }

        health_response = unauthenticated_client.get("/api/health")
        assert health_response.status_code == 200
        assert health_response.json()["status"] == "degraded"

        live_response = unauthenticated_client.get("/api/livez")
        assert live_response.status_code == 200
        assert live_response.json()["status"] == "alive"

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

    def test_operational_routes_require_bearer_token(self, unauthenticated_client):
        """All dashboard actions require auth while every health probe stays public."""
        protected = (
            unauthenticated_client.get("/api/executions/stats"),
            unauthenticated_client.post("/api/enqueue/add/2/3"),
            unauthenticated_client.get("/api/metrics"),
            unauthenticated_client.get("/api/executions"),
        )
        assert [response.status_code for response in protected] == [401, 401, 401, 401]

        public = (
            unauthenticated_client.get("/api/livez"),
            unauthenticated_client.get("/api/readyz"),
            unauthenticated_client.get("/api/health"),
        )
        assert [response.status_code for response in public] == [200, 200, 200]


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

    def test_observability_demo_routes_keep_bounded_queue_contract(
        self,
        client,
        settings,
    ):
        """The fixed demo payload stays small and reaches every intended queue."""
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RUNTIME_ENV_PROFILES": {"thin": {}},
        }
        settings.TASKS = {
            **settings.TASKS,
            "thin": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": "auto",
                    "RUNTIME_ENV_PROFILE": "thin",
                },
            },
        }
        ml_samples = [{"features": [index / 10, (index + 1) / 10]} for index in range(12)]
        search_payload = {
            "pattern": "demo",
            "data_sources": [
                "demo-source-a",
                "other-source",
                "demo-source-b",
            ],
            "case_sensitive": False,
        }
        ml_payload = {
            "model_id": "locust-demo-model",
            "samples": ml_samples,
        }
        requests = [
            (
                client.post("/api/enqueue/add/21/21"),
                "testproject.tasks.add_numbers",
                "default",
                0,
                [21, 21],
                {},
                None,
            ),
            (
                client.post("/api/enqueue/slow/1.5"),
                "testproject.tasks.slow_task",
                "default",
                0,
                [],
                {"seconds": 1.5},
                None,
            ),
            (
                client.post("/api/local/urgent?message=locust-observability-demo"),
                "testproject.apps.local_ray.tasks.urgent_task",
                "high-priority",
                100,
                ["locust-observability-demo"],
                {},
                None,
            ),
            (
                client.post("/api/sync/calculate?a=42&b=6&operation=divide"),
                "testproject.apps.sync_tasks.tasks.simple_calculation",
                "sync",
                0,
                [42, 6],
                {"operation": "divide"},
                None,
            ),
            (
                client.post(
                    "/api/cluster/search",
                    data=json.dumps(search_payload),
                    content_type="application/json",
                ),
                "testproject.apps.cluster_tasks.tasks.distributed_search",
                "default",
                0,
                [],
                search_payload,
                None,
            ),
            (
                client.post("/api/cluster/workflow-benchmark?num_items=3&seconds_per_item=0.25"),
                "testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark",
                "default",
                0,
                [],
                {"num_items": 3, "seconds_per_item": 0.25},
                None,
            ),
            (
                client.post("/api/cluster/runtime-env/probe?profile=thin"),
                "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
                "default",
                0,
                [],
                {"package": None},
                "thin",
            ),
            (
                client.post(
                    "/api/ml/inference",
                    data=json.dumps(ml_payload),
                    content_type="application/json",
                ),
                "testproject.apps.ml_pipeline.tasks.batch_inference",
                "ml",
                0,
                [],
                ml_payload,
                None,
            ),
        ]

        for (
            response,
            callable_path,
            queue_name,
            priority,
            args,
            kwargs,
            runtime_env_profile,
        ) in requests:
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "READY"
            execution = RayTaskExecution.objects.get(task_id=data["task_id"])
            assert execution.callable_path == callable_path
            assert execution.queue_name == queue_name
            assert execution.priority == priority
            assert json.loads(execution.args_json) == args
            assert json.loads(execution.kwargs_json) == kwargs
            assert execution.runtime_env_profile == runtime_env_profile

        assert RayTaskExecution.objects.count() == 8
        assert client.get("/api/executions/stats").status_code == 200
        assert client.get("/api/metrics").status_code == 200

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
        expected_kwargs = {
            "fast_items": 3,
            "slow_items": 2,
            "fast_seconds": 0.01,
            "slow_seconds": 0.05,
        }
        assert data["kwargs"] == expected_kwargs
        assert json.loads(execution.kwargs_json) == expected_kwargs
        assert RayTaskExecution.objects.count() == 1

    def test_enqueue_complex_workflow_accepts_deterministic_failure(self, client):
        response = client.post(
            "/api/cluster/complex-workflow"
            "?fast_items=3&slow_items=2&fast_seconds=0.01&slow_seconds=0.05"
            "&failure_branch=slow&failure_item=1"
        )

        assert response.status_code == 200
        data = response.json()
        expected_kwargs = {
            "fast_items": 3,
            "slow_items": 2,
            "fast_seconds": 0.01,
            "slow_seconds": 0.05,
            "failure_branch": "slow",
            "failure_item": 1,
        }
        assert data["kwargs"] == expected_kwargs
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert json.loads(execution.kwargs_json) == expected_kwargs
        assert execution.attempt_number == 1
        assert RayTaskExecution.objects.count() == 1

    @pytest.mark.parametrize("reporting_policy", ["full", "terminal_only", "disabled"])
    def test_enqueue_complex_workflow_accepts_explicit_reporting_policy(
        self,
        client,
        reporting_policy,
    ):
        response = client.post(
            "/api/cluster/complex-workflow"
            "?fast_items=2&slow_items=1&fast_seconds=0.01&slow_seconds=0.02"
            f"&reporting_policy={reporting_policy}"
        )

        assert response.status_code == 200
        data = response.json()
        expected_kwargs = {
            "fast_items": 2,
            "slow_items": 1,
            "fast_seconds": 0.01,
            "slow_seconds": 0.02,
            "reporting_policy": reporting_policy,
        }
        assert data["kwargs"] == expected_kwargs
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert json.loads(execution.kwargs_json) == expected_kwargs
        assert RayTaskExecution.objects.count() == 1

    @pytest.mark.parametrize(
        "query",
        [
            "failure_branch=fast",
            "failure_item=0",
            "failure_branch=other&failure_item=0",
            "failure_branch=fast&failure_item=-1",
            "fast_items=2&failure_branch=fast&failure_item=2",
            "slow_items=2&failure_branch=slow&failure_item=2",
            "reporting_policy=sampled",
        ],
    )
    def test_enqueue_complex_workflow_rejects_invalid_failure_controls(
        self,
        client,
        query,
    ):
        response = client.post(f"/api/cluster/complex-workflow?{query}")

        assert response.status_code == 422
        assert RayTaskExecution.objects.count() == 0

    def test_enqueue_workflow_showcase_creates_one_durable_task(self, client):
        response = client.post("/api/cluster/workflow-showcase?item_count=3&work_seconds=0.05")

        assert response.status_code == 200
        data = response.json()
        expected_kwargs = {
            "item_count": 3,
            "work_seconds": 0.05,
        }
        assert data["kwargs"] == expected_kwargs
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert execution.callable_path == (
            "testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task"
        )
        assert json.loads(execution.kwargs_json) == expected_kwargs
        assert RayTaskExecution.objects.count() == 1

    def test_enqueue_workflow_showcase_accepts_reservation_failure(self, client):
        response = client.post(
            "/api/cluster/workflow-showcase"
            "?item_count=3&work_seconds=0.05"
            "&failure_stage=reserve_inventory&failure_item=1"
        )

        assert response.status_code == 200
        data = response.json()
        expected_kwargs = {
            "item_count": 3,
            "work_seconds": 0.05,
            "failure_stage": "reserve_inventory",
            "failure_item": 1,
        }
        assert data["kwargs"] == expected_kwargs
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert json.loads(execution.kwargs_json) == expected_kwargs
        assert execution.attempt_number == 1
        assert RayTaskExecution.objects.count() == 1

    def test_enqueue_workflow_recovery_showcase_keeps_failure_plan_internal(
        self,
        client,
        settings,
    ):
        recovery_runtime_env = {
            "working_dir": f"gcs://_ray_pkg_{'a' * 40}.zip",
        }
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RUNTIME_ENV_PROFILES": {"recovery-showcase": recovery_runtime_env},
        }
        settings.TASKS = {
            **settings.TASKS,
            "recovery-showcase": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": "auto",
                    "RUNTIME_ENV_PROFILE": "recovery-showcase",
                },
            },
        }
        response = client.post(
            "/api/cluster/workflow-recovery-showcase?item_count=1&work_seconds=0.01"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["kwargs"] == {"item_count": 1, "work_seconds": 0.01}
        execution = RayTaskExecution.objects.get(task_id=data["task_id"])
        assert execution.callable_path == (
            "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task"
        )
        assert json.loads(execution.kwargs_json) == data["kwargs"]
        assert execution.attempt_number == 1
        assert execution.runtime_env_profile == "recovery-showcase"
        assert json.loads(execution.runtime_env_json) == recovery_runtime_env
        assert RayTaskExecution.objects.count() == 1

    def test_enqueue_workflow_recovery_showcase_fails_closed_without_recovery_backend(
        self,
        client,
        settings,
    ):
        settings.TASKS = {
            name: backend for name, backend in settings.TASKS.items() if name != "recovery-showcase"
        }

        response = client.post(
            "/api/cluster/workflow-recovery-showcase?item_count=1&work_seconds=0.01"
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": (
                "Workflow recovery showcase requires a valid 'recovery-showcase' task "
                "backend and RuntimeEnv profile."
            )
        }
        assert RayTaskExecution.objects.count() == 0

    def test_enqueue_workflow_recovery_showcase_fails_closed_without_recovery_profile(
        self,
        client,
        settings,
    ):
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RUNTIME_ENV_PROFILES": {},
        }
        settings.TASKS = {
            **settings.TASKS,
            "recovery-showcase": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": "auto",
                    "RUNTIME_ENV_PROFILE": "recovery-showcase",
                },
            },
        }

        response = client.post(
            "/api/cluster/workflow-recovery-showcase?item_count=1&work_seconds=0.01"
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": (
                "Workflow recovery showcase requires a valid 'recovery-showcase' task "
                "backend and RuntimeEnv profile."
            )
        }
        assert RayTaskExecution.objects.count() == 0

    def test_enqueue_workflow_recovery_showcase_rejects_retry_unsafe_profile(
        self,
        client,
        settings,
    ):
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RUNTIME_ENV_PROFILES": {
                "recovery-showcase": {
                    "working_dir": "https://example.invalid/mutable-code.zip",
                    "pip": ["django>=6.0"],
                }
            },
        }

        response = client.post(
            "/api/cluster/workflow-recovery-showcase?item_count=1&work_seconds=0.01"
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": (
                "Workflow recovery showcase requires a valid 'recovery-showcase' task "
                "backend and RuntimeEnv profile."
            )
        }
        assert RayTaskExecution.objects.count() == 0

    @pytest.mark.parametrize(
        "query",
        [
            "item_count=0",
            "item_count=9",
            "work_seconds=-0.01",
            "work_seconds=1.01",
            "failure_stage=reserve_inventory",
            "failure_item=0",
            "failure_stage=other&failure_item=0",
            "item_count=3&failure_stage=reserve_inventory&failure_item=3",
        ],
    )
    def test_enqueue_workflow_showcase_rejects_invalid_bounds(
        self,
        client,
        query,
    ):
        response = client.post(f"/api/cluster/workflow-showcase?{query}")

        assert response.status_code == 422
        assert RayTaskExecution.objects.count() == 0

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
        """A compatible inline input stays visible in the bounded status shape."""
        response = client.post("/api/enqueue/add/1/1")
        assert response.status_code == 200
        task_id = response.json()["task_id"]

        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == task_id
        assert data["status"] == "READY"
        assert data["state"] == TaskState.QUEUED
        assert data["attempt_number"] == 1
        assert data["execution_generation"] == 0
        assert data["execution_protocol_version"] == 1
        assert data["created_with_django_ray_version"] == django_ray_version
        assert data["managed_with_django_ray_version"] is None
        assert data["executor_django_ray_version"] is None
        assert data["protocol_compatible_worker_available"] is False
        assert data["queue_capacity_attested"] is False
        assert data["args"] == [1, 1]
        assert data["kwargs"] == {}
        assert data["input_omission_reason"] is None
        assert data["input_max_bytes"] == testproject_api._TASK_STATUS_INPUT_MAX_BYTES
        assert data["response_max_bytes"] == testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES
        assert len(response.content) <= testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES
        assert response["Cache-Control"] == "no-store"
        assert response["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize(
        ("state", "status"),
        [
            (TaskState.QUEUED, "READY"),
            (TaskState.RUNNING, "RUNNING"),
            (TaskState.SUCCEEDED, "SUCCESSFUL"),
            (TaskState.FAILED, "FAILED"),
            (TaskState.CANCELLED, "FAILED"),
            (TaskState.CANCELLING, "RUNNING"),
            (TaskState.LOST, "FAILED"),
            (TaskState.EXPIRED, "FAILED"),
        ],
    )
    def test_task_status_preserves_django_status_and_exact_execution_identity(
        self,
        client,
        state,
        status,
    ):
        task = RayTaskExecution.objects.create(
            task_id=f"custom-status-{state.lower()}",
            callable_path="missing.module.callable",
            state=state,
            attempt_number=3,
            execution_generation=7,
            args_json='["visible"]',
            kwargs_json='{"key":"value"}',
            result_data='{"must":"not-load"}',
            error_traceback="must-not-load",
        )

        response = client.get(f"/api/tasks/{task.task_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == status
        assert payload["state"] == state
        assert payload["attempt_number"] == 3
        assert payload["execution_generation"] == 7
        assert payload["args"] == ["visible"]
        assert payload["kwargs"] == {"key": "value"}
        assert "must-not-load" not in response.content.decode()

    def test_task_status_missing_is_fixed_and_task_id_is_length_bounded(self, client):
        maximum_task_id = "x" * 255
        RayTaskExecution.objects.create(
            task_id=maximum_task_id,
            callable_path="missing.module.callable",
        )

        missing = client.get("/api/tasks/custom-historical-id")
        maximum = client.get(f"/api/tasks/{maximum_task_id}")

        assert missing.status_code == 404
        assert missing.json() == {
            "code": "task_status_not_found",
            "message": "Task status was not found.",
            "response_max_bytes": testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES,
        }
        assert missing["Cache-Control"] == "no-store"
        assert maximum.status_code == 200
        assert maximum.json()["task_id"] == maximum_task_id
        assert client.get(f"/api/tasks/{'x' * 256}").status_code == 422

    def test_task_status_never_loads_external_or_unrelated_task_data(
        self,
        client,
        monkeypatch,
    ):
        protected = "status-protected-reference-marker"
        task = RayTaskExecution.objects.create(
            task_id="bounded-status-external-input",
            callable_path="missing.module.callable",
            state=TaskState.SUCCEEDED,
            input_reference=f"digest:{protected}",
            args_json='["external-placeholder"]',
            kwargs_json='{"external":"placeholder"}',
            result_data=f'{{"protected":"{protected}"}}',
            result_reference=f"digest:{protected}",
            error_message=protected,
            error_traceback=protected,
            runtime_env_json=f'{{"protected":"{protected}"}}',
            progress_data=protected,
            workflow_progress_summary_json=protected,
        )

        def fail(*args, **kwargs):
            pytest.fail("bounded status polling must not call application-data loaders")

        monkeypatch.setattr("django_ray.runtime.import_utils.import_callable", fail)
        monkeypatch.setattr("django_ray.input_storage.load_task_input", fail)
        monkeypatch.setattr("django_ray.result_storage.load_result_reference", fail)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/api/tasks/{task.task_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["args"] is None
        assert payload["kwargs"] is None
        assert payload["input_omission_reason"] == "external_input_not_loaded"
        assert protected not in response.content.decode()
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 1
        sql = task_selects[0]
        assert "LENGTH(CAST(" in sql or "OCTET_LENGTH(" in sql
        for excluded in (
            "callable_path",
            "result_data",
            "result_reference",
            "error_message",
            "error_traceback",
            "runtime_env_json",
            "progress_data",
            "workflow_progress_summary_json",
        ):
            assert excluded not in sql
        assert "FOR UPDATE" not in sql.upper()

    def test_task_status_classifies_oversized_and_malformed_inline_inputs(self, client):
        oversized_args = json.dumps(
            ["\u00e9" * testproject_api._TASK_STATUS_INPUT_MAX_BYTES],
            ensure_ascii=False,
        )
        oversized = RayTaskExecution.objects.create(
            task_id="bounded-status-oversized-input",
            callable_path="test.task",
            args_json=oversized_args,
            kwargs_json="{}",
        )
        malformed = RayTaskExecution.objects.create(
            task_id="bounded-status-malformed-input",
            callable_path="test.task",
            args_json='{"not":"a-list"}',
            kwargs_json="{}",
        )

        oversized_response = client.get(f"/api/tasks/{oversized.task_id}")
        malformed_response = client.get(f"/api/tasks/{malformed.task_id}")

        assert oversized_response.status_code == 200
        assert oversized_response.json()["input_omission_reason"] == (
            "stored_input_exceeds_status_limit"
        )
        assert oversized_response.json()["args"] is None
        assert oversized_args not in oversized_response.content.decode()
        assert malformed_response.status_code == 200
        assert malformed_response.json()["input_omission_reason"] == "malformed_inline_input"
        assert malformed_response.json()["args"] is None

    @pytest.mark.parametrize(
        "args_json",
        [
            "[NaN,Infinity,-Infinity]",
            "[1e999]",
        ],
    )
    def test_task_status_rejects_non_finite_json_numbers(self, client, args_json):
        task = RayTaskExecution.objects.create(
            task_id=f"bounded-status-non-finite-{len(args_json)}",
            callable_path="test.task",
            args_json=args_json,
            kwargs_json="{}",
        )

        response = client.get(f"/api/tasks/{task.task_id}")

        assert response.status_code == 200
        assert response.json()["args"] is None
        assert response.json()["input_omission_reason"] == "malformed_inline_input"
        json.loads(
            response.content,
            parse_constant=lambda value: pytest.fail(
                f"strict client received non-finite JSON constant {value}"
            ),
        )

    def test_task_status_enforces_the_encoded_response_ceiling(self, client, monkeypatch):
        task = RayTaskExecution.objects.create(
            task_id="bounded-status-render-limit",
            callable_path="test.task",
            args_json='["visible"]',
            kwargs_json="{}",
        )
        real_encoder = testproject_api._encode_api_schema_response

        def force_first_render_over_limit(request, response, *, status_code):
            if isinstance(response, testproject_api.TaskStatusSchema) and response.args is not None:
                return b"x" * (testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES + 1)
            return real_encoder(request, response, status_code=status_code)

        monkeypatch.setattr(
            testproject_api,
            "_encode_api_schema_response",
            force_first_render_over_limit,
        )

        response = client.get(f"/api/tasks/{task.task_id}")

        assert response.status_code == 200
        assert response.json()["args"] is None
        assert response.json()["kwargs"] is None
        assert response.json()["input_omission_reason"] == "encoded_response_limit"
        assert len(response.content) <= testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES

    def test_task_status_fails_clearly_on_an_unsupported_database(self, monkeypatch):
        monkeypatch.setattr(connection, "vendor", "oracle")

        with pytest.raises(ImproperlyConfigured, match="task status supports only"):
            testproject_api._bounded_task_status_row("task-id")

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
        assert data["progress"]["source_schema_version"] == 1
        assert data["progress"]["summary"]["node_counts"]["succeeded"] == 7
        assert data["progress"]["summary"]["progress_percent"] == 100.0
        assert "graph" not in data["progress"]["summary"]
        assert data["result_omission_reason"] is None
        assert data["error_omission_reason"] is None
        assert data["diagnostic_max_bytes"] == testproject_api._POLL_DIAGNOSTIC_MAX_BYTES
        assert data["response_max_bytes"] == testproject_api._POLL_RESPONSE_MAX_BYTES
        assert response["Cache-Control"] == "no-store"

    def test_get_workflow_showcase_result(self, client):
        result = {
            "engine": "django-ray-workflow",
            "workflow": "order-fulfillment-showcase",
            "durability_boundary": "single RayTaskExecution",
            "order_id": "showcase-order-0001",
            "status": "FULFILLED",
            "item_count": 1,
            "reserved_units": 1,
            "currency": "USD",
            "total_cents": 1_000,
            "risk": "LOW",
            "recommendation": "PRIORITY_FULFILLMENT",
            "decision": "APPROVED",
            "sinks": {
                "primary": "WRITTEN",
                "audit": "WRITTEN",
                "notification": "SENT",
            },
        }
        execution = RayTaskExecution.objects.create(
            task_id="workflow-showcase-result-001",
            callable_path=("testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task"),
            queue_name="default",
            state=TaskState.SUCCEEDED,
            result_data=json.dumps(result),
        )

        response = client.get(f"/api/cluster/workflow-showcase/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == TaskState.SUCCEEDED
        assert data["result"] == result
        assert data["error"] is None
        assert data["result_omission_reason"] is None
        assert data["error_omission_reason"] is None
        assert data["progress"]["schema"] == "django-ray.workflow-progress-summary"
        assert data["progress"]["availability"] == "NOT_REPORTED"
        assert data["progress"]["complete"] is False
        assert data["progress"]["source_schema_version"] is None
        assert data["progress"]["summary"] is None

    def test_get_workflow_showcase_failure(self, client):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-showcase-result-002",
            callable_path=("testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task"),
            queue_name="default",
            state=TaskState.FAILED,
            error_message=(
                "\x1b[31mIntentional workflow showcase\x1b[39m\rreserve_inventory failure at item 0"
            ),
        )

        response = client.get(f"/api/cluster/workflow-showcase/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == TaskState.FAILED
        assert data["result"] is None
        assert data["error"] == (
            "Intentional workflow showcase\nreserve_inventory failure at item 0"
        )
        assert data["result_omission_reason"] is None
        assert data["error_omission_reason"] is None
        assert data["progress"]["schema"] == "django-ray.workflow-progress-summary"
        assert data["progress"]["availability"] == "NOT_REPORTED"
        assert data["progress"]["complete"] is False

    def test_get_workflow_recovery_showcase_marks_attempt_three_current(self, client):
        result = {
            "status": "FULFILLED",
            "recovery": {
                "scenario": "three-attempt-recovery",
                "attempt_number": 3,
                "outcome": "SUCCEEDED",
            },
        }
        execution = RayTaskExecution.objects.create(
            task_id="workflow-recovery-showcase-result-001",
            callable_path=(
                "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task"
            ),
            queue_name="default",
            state=TaskState.SUCCEEDED,
            attempt_number=3,
            execution_generation=3,
            runtime_env_profile="recovery-showcase",
            result_data=json.dumps(result),
        )
        TaskAttempt.objects.bulk_create(
            [
                TaskAttempt(
                    execution=execution,
                    attempt_number=1,
                    state=TaskState.FAILED,
                    error_message=(
                        "Intentional workflow recovery failure at build_order_batch "
                        "on durable attempt 1"
                    ),
                ),
                TaskAttempt(
                    execution=execution,
                    attempt_number=2,
                    state=TaskState.FAILED,
                    error_message=(
                        "Intentional workflow recovery failure at join_order_inputs "
                        "on durable attempt 2"
                    ),
                ),
                TaskAttempt(
                    execution=execution,
                    attempt_number=3,
                    state=TaskState.SUCCEEDED,
                    result_data=json.dumps(result),
                ),
            ]
        )

        response = client.get(f"/api/cluster/workflow-recovery-showcase/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == TaskState.SUCCEEDED
        assert data["attempt_number"] == 3
        assert data["runtime_env_profile"] == "recovery-showcase"
        assert data["result"] == result
        assert data["error"] is None
        assert data["attempts"] == [
            {
                "attempt_number": 1,
                "state": TaskState.FAILED,
                "error": (
                    "Intentional workflow recovery failure at build_order_batch "
                    "on durable attempt 1"
                ),
                "error_omission_reason": None,
            },
            {
                "attempt_number": 2,
                "state": TaskState.FAILED,
                "error": (
                    "Intentional workflow recovery failure at join_order_inputs "
                    "on durable attempt 2"
                ),
                "error_omission_reason": None,
            },
            {
                "attempt_number": 3,
                "state": TaskState.SUCCEEDED,
                "error": None,
                "error_omission_reason": None,
            },
        ]
        assert data["attempt_error_max_bytes"] == testproject_api._POLL_ATTEMPT_ERROR_MAX_BYTES
        assert data["progress"]["availability"] == "NOT_REPORTED"

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

        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/api/cluster/runtime-env/{execution.task_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["runtime_env_profile"] == "numpy-2-3"
        assert data["runtime_env_hash"] == "a" * 64
        assert data["result"]["package_version"] == "2.3.5"
        assert data["result_omission_reason"] is None
        assert data["error_omission_reason"] is None
        assert data["diagnostic_max_bytes"] == testproject_api._POLL_DIAGNOSTIC_MAX_BYTES
        assert data["response_max_bytes"] == testproject_api._POLL_RESPONSE_MAX_BYTES
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if "django_ray_raytaskexecution" in query["sql"]
            and query["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert task_selects
        assert all("runtime_env_json" not in query for query in task_selects)

    @pytest.mark.parametrize(
        ("task_id", "callable_path", "endpoint"),
        [
            (
                "workflow-result-external-002",
                "testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark",
                "/api/cluster/workflow-benchmark/{task_id}",
            ),
            (
                "complex-workflow-result-external-002",
                "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark",
                "/api/cluster/complex-workflow/{task_id}",
            ),
            (
                "workflow-showcase-result-external-002",
                "testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task",
                "/api/cluster/workflow-showcase/{task_id}",
            ),
            (
                "workflow-recovery-result-external-002",
                ("testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task"),
                "/api/cluster/workflow-recovery-showcase/{task_id}",
            ),
            (
                "runtime-env-result-external-002",
                "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
                "/api/cluster/runtime-env/{task_id}",
            ),
        ],
    )
    def test_pollers_never_resolve_external_results_or_task_application_data(
        self,
        client,
        monkeypatch,
        task_id,
        callable_path,
        endpoint,
    ):
        protected = "polling-external-result-marker"
        execution = RayTaskExecution.objects.create(
            task_id=task_id,
            callable_path=callable_path,
            queue_name="default",
            state=TaskState.SUCCEEDED,
            runtime_env_profile="numpy-2-3",
            runtime_env_hash="c" * 64,
            result_data=None,
            result_reference=f"digest:{protected}",
            args_json=f'["{protected}"]',
            kwargs_json=f'{{"protected":"{protected}"}}',
            input_reference=f"digest:{protected}",
            error_traceback=protected,
            runtime_env_json=f'{{"protected":"{protected}"}}',
            completion_data=protected,
        )

        def fail(*args, **kwargs):
            pytest.fail("bounded polling must not load task application data")

        monkeypatch.setattr("django_ray.runtime.import_utils.import_callable", fail)
        monkeypatch.setattr("django_ray.input_storage.load_task_input", fail)
        monkeypatch.setattr("django_ray.result_storage.load_result_reference", fail)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(endpoint.format(task_id=execution.task_id))

        assert response.status_code == 200
        data = response.json()
        assert data["result"] is None
        assert data["result_omission_reason"] == "external_result_not_loaded"
        assert protected not in response.content.decode()
        assert len(response.content) <= testproject_api._POLL_RESPONSE_MAX_BYTES
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        for sql in task_selects:
            for excluded in (
                "args_json",
                "kwargs_json",
                "input_reference",
                "error_traceback",
                "runtime_env_json",
                "completion_data",
            ):
                assert excluded not in sql

    def test_bounded_v3_summary_is_public_without_loading_legacy_graph(self, client):
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
        assert progress["schema_version"] == 1
        assert progress["source_schema_version"] == 3
        assert "task_execution_pk" not in progress["run_identity"]
        assert progress["summary"]["storage"]["manifest_id"] is None
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 2
        assert all("progress_data" not in query for query in task_selects)
        assert "workflow_progress_summary_json" not in task_selects[0]

    def test_legacy_live_workflow_node_route_is_removed(self, client):
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

        assert response.status_code == 404
        schema = client.get("/api/openapi.json").json()
        assert "/api/cluster/workflows/{task_id}/nodes/{node_id}" not in schema["paths"]
        assert "WorkflowNodeSchema" not in schema["components"]["schemas"]
        assert set(schema["paths"]["/api/cluster/workflows/{task_id}/node-detail"]) == {"get"}

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

    def test_pollers_classify_oversized_and_malformed_inline_diagnostics(self, client):
        oversized_value = "\u00e9" * testproject_api._POLL_DIAGNOSTIC_MAX_BYTES
        oversized = RayTaskExecution.objects.create(
            task_id="runtime-env-poll-oversized",
            callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            state=TaskState.FAILED,
            runtime_env_hash="d" * 64,
            result_data=json.dumps({"value": oversized_value}, ensure_ascii=False),
            error_message=oversized_value,
        )
        malformed = RayTaskExecution.objects.create(
            task_id="runtime-env-poll-malformed",
            callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            state=TaskState.SUCCEEDED,
            runtime_env_hash="e" * 64,
            result_data='["not-a-result-object"]',
        )

        oversized_response = client.get(f"/api/cluster/runtime-env/{oversized.task_id}")
        malformed_response = client.get(f"/api/cluster/runtime-env/{malformed.task_id}")

        assert oversized_response.status_code == 200
        oversized_payload = oversized_response.json()
        assert oversized_payload["result"] is None
        assert oversized_payload["error"] is None
        assert oversized_payload["result_omission_reason"] == ("stored_result_exceeds_poll_limit")
        assert oversized_payload["error_omission_reason"] == "stored_error_exceeds_poll_limit"
        assert oversized_value not in oversized_response.content.decode()
        assert malformed_response.status_code == 200
        assert malformed_response.json()["result"] is None
        assert malformed_response.json()["result_omission_reason"] == ("malformed_inline_result")

    @pytest.mark.parametrize(
        "result_data",
        [
            '{"value":NaN}',
            '{"value":Infinity}',
            '{"value":-Infinity}',
            '{"value":1e999}',
        ],
    )
    def test_pollers_reject_non_finite_json_numbers(self, client, result_data):
        execution = RayTaskExecution.objects.create(
            task_id=f"runtime-env-poll-non-finite-{len(result_data)}",
            callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            state=TaskState.SUCCEEDED,
            runtime_env_hash="9" * 64,
            result_data=result_data,
        )

        response = client.get(f"/api/cluster/runtime-env/{execution.task_id}")

        assert response.status_code == 200
        assert response.json()["result"] is None
        assert response.json()["result_omission_reason"] == "malformed_inline_result"
        json.loads(
            response.content,
            parse_constant=lambda value: pytest.fail(
                f"strict client received non-finite JSON constant {value}"
            ),
        )

    def test_recovery_attempt_errors_are_byte_guarded_and_attempt_fenced(self, client):
        protected = "r" * (testproject_api._POLL_ATTEMPT_ERROR_MAX_BYTES + 1)
        execution = RayTaskExecution.objects.create(
            task_id="workflow-recovery-bounded-attempt-errors",
            callable_path=(
                "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task"
            ),
            state=TaskState.SUCCEEDED,
            attempt_number=3,
            execution_generation=3,
            runtime_env_profile="recovery-showcase",
            result_data='{"status":"FULFILLED"}',
        )
        TaskAttempt.objects.bulk_create(
            [
                TaskAttempt(
                    execution=execution,
                    attempt_number=1,
                    state=TaskState.FAILED,
                    error_message=protected,
                ),
                TaskAttempt(
                    execution=execution,
                    attempt_number=2,
                    state=TaskState.FAILED,
                    error_message="bounded",
                ),
                TaskAttempt(
                    execution=execution,
                    attempt_number=4,
                    state=TaskState.FAILED,
                    error_message="newer-attempt-must-not-mix",
                ),
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/api/cluster/workflow-recovery-showcase/{execution.task_id}")

        assert response.status_code == 200
        attempts = response.json()["attempts"]
        assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
        assert attempts[0]["error"] is None
        assert attempts[0]["error_omission_reason"] == "stored_error_exceeds_attempt_limit"
        assert attempts[1]["error"] == "bounded"
        assert attempts[1]["error_omission_reason"] is None
        assert protected not in response.content.decode()
        assert "newer-attempt-must-not-mix" not in response.content.decode()
        attempt_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_taskattempt" in query["sql"]
        ]
        assert len(attempt_selects) == 1
        assert "LENGTH(CAST(" in attempt_selects[0] or "OCTET_LENGTH(" in attempt_selects[0]
        assert "result_data" not in attempt_selects[0]
        assert "error_traceback" not in attempt_selects[0]

    def test_workflow_poll_rejects_a_mixed_generation_summary(self, client, monkeypatch):
        execution = RayTaskExecution.objects.create(
            task_id="workflow-poll-raced-summary",
            callable_path="testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=3,
        )

        observed: dict[str, object] = {}

        def raced_summary(*args, **kwargs):
            observed.update(kwargs)
            return {
                "run_identity": {
                    "run_id": "00000000-0000-0000-0000-000000000999",
                    "attempt_number": 3,
                    "execution_generation": 4,
                }
            }

        monkeypatch.setattr(
            testproject_api,
            "get_workflow_progress_summary",
            raced_summary,
        )

        response = client.get(f"/api/cluster/workflow-benchmark/{execution.task_id}")

        assert response.status_code == 200
        assert response.json()["progress"] is None
        assert observed["attempt_number"] == 2

    def test_poll_response_enforces_the_encoded_response_ceiling(self, client, monkeypatch):
        execution = RayTaskExecution.objects.create(
            task_id="runtime-env-poll-render-limit",
            callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            state=TaskState.SUCCEEDED,
            runtime_env_hash="f" * 64,
            result_data='{"value":"visible"}',
            error_message="visible error",
        )
        real_encoder = testproject_api._encode_api_schema_response

        def force_diagnostics_over_limit(request, response, *, status_code):
            if isinstance(response, testproject_api.RuntimeEnvResultSchema) and (
                response.result is not None or response.error is not None
            ):
                return b"x" * (testproject_api._POLL_RESPONSE_MAX_BYTES + 1)
            return real_encoder(request, response, status_code=status_code)

        monkeypatch.setattr(
            testproject_api,
            "_encode_api_schema_response",
            force_diagnostics_over_limit,
        )

        response = client.get(f"/api/cluster/runtime-env/{execution.task_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["result"] is None
        assert payload["error"] is None
        assert payload["result_omission_reason"] == "encoded_response_limit"
        assert payload["error_omission_reason"] == "encoded_response_limit"
        assert len(response.content) <= testproject_api._POLL_RESPONSE_MAX_BYTES

    def test_poll_projection_fails_clearly_on_an_unsupported_database(self, monkeypatch):
        monkeypatch.setattr(connection, "vendor", "oracle")

        with pytest.raises(ImproperlyConfigured, match="polling supports only"):
            testproject_api._bounded_poll_execution_row(
                "task-id",
                callable_paths=("test.task",),
            )
