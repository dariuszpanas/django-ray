"""Integration tests for the Django Ninja API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from datetime import UTC, datetime, timedelta
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
from django_ray.redaction import REDACTED, redact_text
from django_ray.workflow_progress_summary import serialize_workflow_progress_summary
from testproject import api as testproject_api
from tests.workflow_progress_summary_helpers import workflow_progress_summary

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(settings) -> Client:
    """Django test client."""
    token = settings.DJANGO_API_TOKEN
    assert isinstance(token, str) and token
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


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
        assert "survives reloads" in content
        assert 'id="stat-succeeded">1</strong>' in content
        assert 'id="stat-expired">1</strong>' in content

    def test_browser_auth_contract_does_not_embed_or_leak_token(self, settings):
        """The browser session retains its credential without server-side leakage."""
        configured_token = "configured-browser-token-must-not-leak-issue-162"
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
        assert 'const sessionCredentialKey = "django-ray.testproject.api-token.v1"' in script
        assert "window.sessionStorage.getItem(sessionCredentialKey)" in script
        assert "window.sessionStorage.setItem(sessionCredentialKey, token)" in script
        assert "window.sessionStorage.removeItem(sessionCredentialKey)" in script
        for endpoint in (
            "/api/executions/stats",
            "/api/enqueue/add/2/3",
            "/api/metrics",
            "/api/executions",
        ):
            assert endpoint in script
        for leak_path in (
            "localStorage",
            "document.cookie",
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
        assert data["limit"] == testproject_api._EXECUTION_LIST_DEFAULT_LIMIT
        assert data["returned_count"] == 0
        assert data["has_more"] is False
        assert data["next_cursor"] is None
        assert data["truncated"] is False
        assert data["truncation_reason"] is None
        assert data["diagnostic_max_bytes"] == (
            testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES
        )
        assert data["response_max_bytes"] == (testproject_api._EXECUTION_LIST_RESPONSE_MAX_BYTES)

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
        deadline = datetime.now(UTC) - timedelta(seconds=1)
        RayTaskExecution.objects.create(
            task_id="test-expired-list",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.EXPIRED,
            queue_timeout_seconds=60,
            queue_deadline_at=deadline,
        )

        response = client.get("/api/executions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["queued"] == 1
        assert data["succeeded"] == 1
        assert data["expired"] == 1
        expired = next(task for task in data["tasks"] if task["state"] == TaskState.EXPIRED)
        assert expired["queue_timeout_seconds"] == 60
        assert abs(datetime.fromisoformat(expired["queue_deadline_at"]) - deadline) < timedelta(
            milliseconds=1
        )

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

    def test_list_executions_filter_by_exact_task_id(self, client):
        """A caller can poll one execution without fetching the recent history."""
        RayTaskExecution.objects.create(
            task_id="gate-target",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data="5",
        )
        RayTaskExecution.objects.create(
            task_id="unrelated",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data="99",
        )

        response = client.get("/api/executions?task_id=gate-target&limit=1")

        assert response.status_code == 200
        assert [task["task_id"] for task in response.json()["tasks"]] == ["gate-target"]

    @pytest.mark.parametrize(
        "limit",
        (
            "not-an-integer",
            "0",
            "-1",
            str(testproject_api._EXECUTION_LIST_MAX_LIMIT + 1),
        ),
    )
    def test_list_executions_rejects_invalid_page_sizes(self, client, limit):
        response = client.get("/api/executions", {"limit": limit})

        assert response.status_code == 422

    @pytest.mark.parametrize(
        "cursor",
        (
            "not-a-signed-cursor",
            "x" * (testproject_api._EXECUTION_LIST_CURSOR_MAX_CHARACTERS + 1),
        ),
    )
    def test_list_executions_rejects_invalid_cursors(self, client, cursor):
        response = client.get("/api/executions", {"cursor": cursor})

        assert response.status_code == 422

    def test_execution_list_openapi_documents_page_bounds(self, client):
        operation = client.get("/api/openapi.json").json()["paths"]["/api/executions"]["get"]
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

        limit = parameters["limit"]["schema"]
        assert limit["minimum"] == testproject_api._EXECUTION_LIST_MIN_LIMIT
        assert limit["maximum"] == testproject_api._EXECUTION_LIST_MAX_LIMIT
        assert limit["default"] == testproject_api._EXECUTION_LIST_DEFAULT_LIMIT

        cursor = parameters["cursor"]["schema"]
        cursor_string = next(item for item in cursor["anyOf"] if item.get("type") == "string")
        assert cursor_string["maxLength"] == (testproject_api._EXECUTION_LIST_CURSOR_MAX_CHARACTERS)
        assert parameters["cursor"]["required"] is False

    def test_list_executions_paginates_with_stable_continuation_metadata(self, client):
        tasks = RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"paged-{index}",
                    callable_path="test.task",
                    state=TaskState.QUEUED,
                )
                for index in range(5)
            ]
        )

        first = client.get("/api/executions?limit=2")
        first_data = first.json()
        inserted = RayTaskExecution.objects.create(
            task_id="paged-concurrent-insert",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )
        second = client.get(
            "/api/executions",
            {"limit": 2, "cursor": first_data["next_cursor"]},
        )
        second_data = second.json()
        third = client.get(
            "/api/executions",
            {"limit": 2, "cursor": second_data["next_cursor"]},
        )

        assert first.status_code == second.status_code == third.status_code == 200
        third_data = third.json()
        assert first_data["total"] == 5
        assert second_data["total"] == third_data["total"] == 6
        assert first_data["returned_count"] == second_data["returned_count"] == 2
        assert first_data["has_more"] is second_data["has_more"] is True
        assert isinstance(first_data["next_cursor"], str)
        assert isinstance(second_data["next_cursor"], str)
        assert first_data["next_cursor"] != second_data["next_cursor"]
        assert first_data["truncation_reason"] == "page_limit"
        assert second_data["truncation_reason"] == "page_limit"
        assert third_data["returned_count"] == 1
        assert third_data["has_more"] is False
        assert third_data["next_cursor"] is None
        assert third_data["truncation_reason"] is None
        returned_ids = {
            item["id"] for page in (first_data, second_data, third_data) for item in page["tasks"]
        }
        assert returned_ids == {task.pk for task in tasks}
        assert inserted.pk not in returned_ids

    def test_execution_list_cursor_is_tamper_evident_and_filter_bound(self, client):
        for index in range(2):
            RayTaskExecution.objects.create(
                task_id=f"cursor-queued-{index}",
                callable_path="test.task",
                state=TaskState.QUEUED,
            )
        RayTaskExecution.objects.create(
            task_id="cursor-succeeded",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
        )
        first = client.get("/api/executions", {"state": "queued", "limit": 1})
        cursor = first.json()["next_cursor"]
        assert isinstance(cursor, str)

        tamper_index = len(cursor) // 2
        replacement = "A" if cursor[tamper_index] != "A" else "B"
        tampered = cursor[:tamper_index] + replacement + cursor[tamper_index + 1 :]

        assert (
            client.get(
                "/api/executions",
                {"state": "queued", "cursor": tampered},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/executions",
                {"state": "SUCCEEDED", "cursor": cursor},
            ).status_code
            == 422
        )

    def test_list_executions_omits_oversized_diagnostics_in_the_database_query(
        self,
        client,
    ):
        oversized = "x\x00" + "y" * testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES
        task = RayTaskExecution.objects.create(
            task_id="oversized-list-diagnostics",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=oversized,
            error_message=oversized,
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/executions?task_id=oversized-list-diagnostics")

        assert response.status_code == 200
        item = response.json()["tasks"][0]
        assert item["result_data"] is None
        assert item["error_message"] is None
        assert item["result_data_omission_reason"] == "stored_value_exceeds_list_limit"
        assert item["error_message_omission_reason"] == "stored_value_exceeds_list_limit"
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 2
        guarded_select = next(query for query in task_selects if "CASE WHEN" in query)
        assert "LENGTH(CAST(" in guarded_select or "OCTET_LENGTH(" in guarded_select
        assert 'THEN "django_ray_raytaskexecution"."result_data"' in guarded_select
        assert 'THEN "django_ray_raytaskexecution"."error_message"' in guarded_select

        detail = client.get(f"/api/executions/{task.pk}")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert detail_payload["result_data"] == REDACTED
        assert detail_payload["error_message"] == redact_text(oversized)
        assert detail_payload["result_data_omission_reason"] is None
        assert detail_payload["error_message_omission_reason"] is None

    def test_execution_list_fails_clearly_on_an_unsupported_database(self, monkeypatch):
        monkeypatch.setattr(connection, "vendor", "oracle")

        with pytest.raises(ImproperlyConfigured, match="only SQLite and PostgreSQL"):
            testproject_api._bounded_execution_list_rows(
                RayTaskExecution.objects.all(),
                limit=1,
            )

    def test_execution_detail_fails_clearly_on_an_unsupported_database(self, monkeypatch):
        monkeypatch.setattr(connection, "vendor", "oracle")

        with pytest.raises(
            ImproperlyConfigured,
            match="execution detail supports only SQLite and PostgreSQL",
        ):
            testproject_api._bounded_execution_detail_row(1)

    def test_list_executions_enforces_the_aggregate_encoded_response_bound(
        self,
        client,
        monkeypatch,
    ):
        diagnostic = "x" * (testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES - 102)
        serialized_result = json.dumps(diagnostic)
        assert len(serialized_result.encode()) == (
            testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES - 100
        )
        tasks = RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"aggregate-bound-{index}",
                    callable_path="test.task",
                    state=TaskState.FAILED,
                    result_data=serialized_result,
                    error_message=diagnostic,
                )
                for index in range(40)
            ]
        )
        validation_calls = {"item": 0, "result": 0, "error": 0}
        real_execution_list_item = testproject_api._execution_list_item
        real_safe_json_dumps = testproject_api.safe_json_dumps
        real_redact_text = testproject_api.redact_text

        def counted_execution_list_item(row):
            validation_calls["item"] += 1
            return real_execution_list_item(row)

        def counted_safe_json_dumps(value, **kwargs):
            validation_calls["result"] += 1
            return real_safe_json_dumps(value, **kwargs)

        def counted_redact_text(value, **kwargs):
            validation_calls["error"] += 1
            return real_redact_text(value, **kwargs)

        monkeypatch.setattr(
            testproject_api,
            "_execution_list_item",
            counted_execution_list_item,
        )
        monkeypatch.setattr(testproject_api, "safe_json_dumps", counted_safe_json_dumps)
        monkeypatch.setattr(testproject_api, "redact_text", counted_redact_text)

        first = client.get(
            "/api/executions",
            {"limit": testproject_api._EXECUTION_LIST_MAX_LIMIT},
        )
        repeated = client.get(
            "/api/executions",
            {"limit": testproject_api._EXECUTION_LIST_MAX_LIMIT},
        )

        assert first.status_code == 200
        assert first.content == repeated.content
        assert len(first.content) <= testproject_api._EXECUTION_LIST_RESPONSE_MAX_BYTES
        first_data = first.json()
        assert 0 < first_data["returned_count"] < len(tasks)
        assert first_data["has_more"] is True
        assert first_data["truncated"] is True
        assert first_data["truncation_reason"] == "response_size_limit"
        assert isinstance(first_data["next_cursor"], str)
        assert all(
            item["result_data_omission_reason"] is None
            and item["error_message_omission_reason"] is None
            for item in first_data["tasks"]
        )

        second = client.get(
            "/api/executions",
            {
                "limit": testproject_api._EXECUTION_LIST_MAX_LIMIT,
                "cursor": first_data["next_cursor"],
            },
        )
        assert second.status_code == 200
        assert len(second.content) <= testproject_api._EXECUTION_LIST_RESPONSE_MAX_BYTES
        second_data = second.json()
        first_ids = {item["id"] for item in first_data["tasks"]}
        second_ids = {item["id"] for item in second_data["tasks"]}
        assert first_ids.isdisjoint(second_ids)
        assert first_ids | second_ids == {task.pk for task in tasks}
        assert validation_calls["item"] > 0
        assert validation_calls["result"] == validation_calls["item"]
        assert validation_calls["error"] == validation_calls["item"]

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
        assert data["result_data_omission_reason"] is None
        assert data["error_message_omission_reason"] is None
        assert data["diagnostic_max_bytes"] == (
            testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        )
        assert data["response_max_bytes"] == (testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_execution_detail_uses_one_public_projection_without_loading_external_result(
        self,
        client,
        monkeypatch,
    ):
        protected_marker = "protected-execution-detail-marker"
        task = RayTaskExecution.objects.create(
            task_id="test-detail-public-projection",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            args_json=f'["{protected_marker}-args"]',
            kwargs_json=f'{{"secret":"{protected_marker}-kwargs"}}',
            input_reference=f"digest:{protected_marker}-input",
            result_reference=f"digest:{protected_marker}-result",
            runtime_env_json=f'{{"secret":"{protected_marker}-runtime"}}',
            progress_data=f'{{"secret":"{protected_marker}-progress"}}',
            workflow_progress_summary_json=(f'{{"secret":"{protected_marker}-summary"}}'),
            workflow_plan_json=f'{{"secret":"{protected_marker}-plan"}}',
            workflow_plan_selection=f'{{"secret":"{protected_marker}-selection"}}',
            completion_data=f'{{"secret":"{protected_marker}-completion"}}',
            cancellation_error=f"{protected_marker}-cancellation",
            error_traceback=f"{protected_marker}-traceback",
        )

        def reject_external_load(*args, **kwargs):
            raise AssertionError("execution detail must not load external result storage")

        monkeypatch.setattr(
            "django_ray.result_storage.load_result_reference",
            reject_external_load,
        )
        captured_rows = []
        original_item = testproject_api._execution_detail_item

        def capture_item(row):
            captured_rows.append(dict(row))
            return original_item(row)

        monkeypatch.setattr(testproject_api, "_execution_detail_item", capture_item)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["result_data"] is None
        assert payload["result_data_omission_reason"] == "external_result_not_loaded"
        assert protected_marker not in response.content.decode()
        assert len(captured_rows) == 1
        assert set(captured_rows[0]) == set(testproject_api._EXECUTION_DETAIL_VALUE_FIELDS)
        assert captured_rows[0]["_detail_has_result_reference"] is True
        assert "result_reference" not in captured_rows[0]

        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 1
        detail_sql = task_selects[0]
        assert "result_reference" in detail_sql
        assert "FOR UPDATE" not in detail_sql.upper()
        for protected_field in (
            "args_json",
            "kwargs_json",
            "input_reference",
            "runtime_env_json",
            "progress_data",
            "workflow_progress_summary_json",
            "workflow_plan_json",
            "workflow_plan_selection",
            "completion_data",
            "cancellation_error",
            "error_traceback",
        ):
            assert protected_field not in detail_sql

    def test_execution_detail_enforces_embedded_nul_byte_boundary_before_transfer(
        self,
        client,
        monkeypatch,
    ):
        limit = testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        exact_value = "x\x00" + "y" * (limit - 2)
        protected_marker = "oversized-detail-secret-marker"
        oversized_value = "x\x00" + "y" * (limit - len(protected_marker)) + protected_marker
        assert len(exact_value.encode()) == limit
        assert len(oversized_value.encode()) == limit + 2
        exact = RayTaskExecution.objects.create(
            task_id="test-detail-exact-byte-boundary",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=exact_value,
            error_message=exact_value,
        )
        oversized = RayTaskExecution.objects.create(
            task_id="test-detail-over-byte-boundary",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=oversized_value,
            error_message=oversized_value,
        )

        exact_response = client.get(f"/api/executions/{exact.pk}")

        assert exact_response.status_code == 200
        exact_payload = exact_response.json()
        assert exact_payload["result_data"] == REDACTED
        assert exact_payload["error_message"] == redact_text(exact_value)
        assert exact_payload["result_data_omission_reason"] is None
        assert exact_payload["error_message_omission_reason"] is None

        captured_rows = []
        original_item = testproject_api._execution_detail_item

        def capture_item(row):
            captured_rows.append(dict(row))
            return original_item(row)

        monkeypatch.setattr(testproject_api, "_execution_detail_item", capture_item)
        with CaptureQueriesContext(connection) as queries:
            oversized_response = client.get(f"/api/executions/{oversized.pk}")

        assert oversized_response.status_code == 200
        oversized_payload = oversized_response.json()
        assert oversized_payload["result_data"] is None
        assert oversized_payload["error_message"] is None
        assert (
            oversized_payload["result_data_omission_reason"] == "stored_value_exceeds_detail_limit"
        )
        assert (
            oversized_payload["error_message_omission_reason"]
            == "stored_value_exceeds_detail_limit"
        )
        assert protected_marker not in oversized_response.content.decode()
        assert captured_rows[0]["_detail_result_data"] is None
        assert captured_rows[0]["_detail_error_message"] is None
        assert captured_rows[0]["_detail_result_data_bytes"] == limit + 2
        assert captured_rows[0]["_detail_error_message_bytes"] == limit + 2

        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 1
        guarded_sql = task_selects[0]
        assert "CASE WHEN" in guarded_sql
        assert "LENGTH(CAST(" in guarded_sql
        assert 'THEN "django_ray_raytaskexecution"."result_data"' in guarded_sql
        assert 'THEN "django_ray_raytaskexecution"."error_message"' in guarded_sql

    def test_execution_detail_omits_result_first_at_aggregate_response_limit(
        self,
        client,
    ):
        limit = testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        diagnostic = "\\" * limit
        serialized_result = json.dumps("\\" * ((limit - 2) // 2))
        assert len(serialized_result.encode()) == limit
        task = RayTaskExecution.objects.create(
            task_id="test-detail-aggregate-response-boundary",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data=serialized_result,
            error_message=diagnostic,
        )

        response = client.get(f"/api/executions/{task.pk}")
        repeated = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == repeated.status_code == 200
        assert response.content == repeated.content
        assert len(response.content) <= testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        payload = response.json()
        assert payload["result_data"] is None
        assert payload["result_data_omission_reason"] == "response_size_limit"
        assert payload["error_message"] == diagnostic
        assert payload["error_message_omission_reason"] is None

    def test_execution_detail_returns_fixed_bounded_failure_when_metadata_cannot_fit(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-detail-fixed-response-limit-failure",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data="stored-result-must-not-leak",
            error_message="stored-error-must-not-leak",
        )

        monkeypatch.setattr(
            testproject_api,
            "_try_encode_execution_detail_response",
            lambda request, response: (
                b"x" * (testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES + 1)
            ),
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 503
        assert len(response.content) <= testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        assert response.json() == {
            "code": "execution_detail_response_limit",
            "message": "Execution detail exceeds its fixed response limit.",
            "response_max_bytes": testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
        }
        assert b"stored-result" not in response.content
        assert b"stored-error" not in response.content
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize(
        ("failure_mode", "expected_statuses"),
        (
            ("initial", [200, 503]),
            ("fallback", [200, 200, 503]),
            ("fixed", [200, 503]),
        ),
    )
    def test_execution_detail_renderer_exceptions_return_fixed_bounded_failure(
        self,
        client,
        monkeypatch,
        failure_mode,
        expected_statuses,
    ):
        task = RayTaskExecution.objects.create(
            task_id=f"test-detail-renderer-{failure_mode}",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data="stored-renderer-result-must-not-leak",
            error_message="stored-renderer-error-must-not-leak",
        )
        original_render = testproject_api.api.renderer.render
        statuses = []

        def render(request, data, *, response_status):
            statuses.append(response_status)
            if failure_mode == "fallback" and len(statuses) == 1:
                return b"x" * (testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES + 1)
            if failure_mode == "fixed" or response_status == 200:
                raise RuntimeError("custom renderer failed")
            return original_render(request, data, response_status=response_status)

        monkeypatch.setattr(testproject_api.api.renderer, "render", render)

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 503
        assert statuses == expected_statuses
        assert response.json() == {
            "code": "execution_detail_response_limit",
            "message": "Execution detail exceeds its fixed response limit.",
            "response_max_bytes": testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
        }
        assert len(response.content) <= testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        assert b"stored-renderer-result" not in response.content
        assert b"stored-renderer-error" not in response.content
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize("exception_type", (SystemExit, KeyboardInterrupt))
    def test_execution_detail_renderer_does_not_swallow_process_control(
        self,
        client,
        monkeypatch,
        exception_type,
    ):
        task = RayTaskExecution.objects.create(
            task_id=f"test-detail-renderer-{exception_type.__name__}",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data="5",
        )

        def render(request, data, *, response_status):
            raise exception_type("process control")

        monkeypatch.setattr(testproject_api.api.renderer, "render", render)

        with pytest.raises(exception_type, match="process control"):
            client.get(f"/api/executions/{task.pk}")

    @pytest.mark.parametrize("exception_type", (SystemExit, KeyboardInterrupt))
    def test_execution_detail_fixed_renderer_does_not_swallow_process_control(
        self,
        monkeypatch,
        exception_type,
    ):
        def render(request, data, *, response_status):
            raise exception_type("process control")

        monkeypatch.setattr(testproject_api.api.renderer, "render", render)

        with pytest.raises(exception_type, match="process control"):
            testproject_api._execution_detail_unavailable_response(object())

    def test_execution_detail_decodes_valid_json_escapes_before_redaction(
        self,
        client,
        settings,
    ):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        task = RayTaskExecution.objects.create(
            task_id="test-detail-valid-escaped-key",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=r'{"\u0070assword":"CANARY_VALID_ESCAPED_KEY"}',
        )

        detail = client.get(f"/api/executions/{task.pk}")
        listing = client.get("/api/executions", {"task_id": task.task_id})

        assert detail.status_code == listing.status_code == 200
        detail_result = detail.json()["result_data"]
        list_result = listing.json()["tasks"][0]["result_data"]
        assert json.loads(detail_result) == {"password": REDACTED}
        assert json.loads(list_result) == {"password": REDACTED}
        assert "CANARY_VALID_ESCAPED_KEY" not in detail.content.decode()
        assert "CANARY_VALID_ESCAPED_KEY" not in listing.content.decode()
        assert detail.json()["result_data_omission_reason"] is None
        assert listing.json()["tasks"][0]["result_data_omission_reason"] is None

    @pytest.mark.parametrize(
        ("case_name", "result_data"),
        (
            (
                "escaped-key",
                r'{"\u0070assword":"CANARY_ESCAPED_KEY",',
            ),
            (
                "escaped-value",
                r'{"safe":"\u0043ANARY_ESCAPED_VALUE",',
            ),
            (
                "mixed-escapes",
                r'{"pass\u0077ord":"CAN\u0041RY_MIXED",',
            ),
            (
                "unicode-key",
                r'{"p\u00e4ssword":"CANARY_UNICODE_KEY",',
            ),
            (
                "truncated-escape",
                r'{"safe":"CANARY_TRUNCATED\u00',
            ),
            (
                "embedded-control",
                '{"safe":"CANARY_CONTROL\x00"',
            ),
        ),
    )
    def test_execution_detail_and_list_malformed_json_fail_closed(
        self,
        client,
        settings,
        case_name,
        result_data,
    ):
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password", r"pässword"]}
        task = RayTaskExecution.objects.create(
            task_id=f"test-detail-malformed-{case_name}",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=result_data,
        )

        detail = client.get(f"/api/executions/{task.pk}")
        listing = client.get("/api/executions", {"task_id": task.task_id})

        assert detail.status_code == listing.status_code == 200
        assert detail.json()["result_data"] == REDACTED
        assert listing.json()["tasks"][0]["result_data"] == REDACTED
        assert detail.json()["result_data_omission_reason"] is None
        assert listing.json()["tasks"][0]["result_data_omission_reason"] is None
        assert "CANARY" not in detail.content.decode()
        assert "CANARY" not in listing.content.decode()

    def test_execution_detail_deep_json_conversion_fails_closed(
        self,
        client,
    ):
        result_data = "[" * 10_000 + '"CANARY_DEEP"' + "]" * 10_000
        task = RayTaskExecution.objects.create(
            task_id="test-detail-deep-json-conversion",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=result_data,
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        assert response.json()["result_data"] == REDACTED
        assert response.json()["result_data_omission_reason"] is None
        assert "CANARY_DEEP" not in response.content.decode()

    def test_execution_detail_and_list_json_depth_boundary_is_stable(
        self,
        client,
    ):
        accepted_value = "[" * 20 + '"visible-depth-boundary"' + "]" * 20
        rejected_value = "[" * 21 + '"CANARY_OVER_DEPTH"' + "]" * 21
        accepted = RayTaskExecution.objects.create(
            task_id="test-detail-json-depth-accepted",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=accepted_value,
        )
        rejected = RayTaskExecution.objects.create(
            task_id="test-detail-json-depth-rejected",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=rejected_value,
        )

        accepted_detail = client.get(f"/api/executions/{accepted.pk}")
        accepted_list = client.get("/api/executions", {"task_id": accepted.task_id})
        rejected_detail = client.get(f"/api/executions/{rejected.pk}")
        rejected_list = client.get("/api/executions", {"task_id": rejected.task_id})

        assert accepted_detail.status_code == accepted_list.status_code == 200
        assert rejected_detail.status_code == rejected_list.status_code == 200
        assert json.loads(accepted_detail.json()["result_data"]) == json.loads(accepted_value)
        assert json.loads(accepted_list.json()["tasks"][0]["result_data"]) == json.loads(
            accepted_value
        )
        assert rejected_detail.json()["result_data"] == REDACTED
        assert rejected_list.json()["tasks"][0]["result_data"] == REDACTED
        assert "CANARY_OVER_DEPTH" not in rejected_detail.content.decode()
        assert "CANARY_OVER_DEPTH" not in rejected_list.content.decode()

    def test_execution_detail_depth_scan_exhaustion_is_fixed_redaction(
        self,
        client,
    ):
        nested_safe_value = "[" * 21 + '"CANARY_AFTER_SENSITIVE_WIDTH"' + "]" * 21
        result_data = (
            '{"password":'
            + json.dumps([0] * testproject_api._EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS)
            + ',"safe":'
            + nested_safe_value
            + "}"
        )
        task = RayTaskExecution.objects.create(
            task_id="test-detail-json-depth-budget-exhausted",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=result_data,
        )

        response = client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 200
        assert response.json()["result_data"] == REDACTED
        assert "<max-depth>" not in response.content.decode()
        assert "CANARY_AFTER_SENSITIVE_WIDTH" not in response.content.decode()

    def test_execution_json_depth_preflight_has_fixed_width_work(self):
        traversed_items = 0

        class CountingList(list):
            def __iter__(self):
                nonlocal traversed_items
                for item in super().__iter__():
                    traversed_items += 1
                    yield item

        value = CountingList(
            [None] * (testproject_api._EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS + 100)
        )

        assert testproject_api._json_value_requires_fixed_redaction(value) is True
        assert traversed_items == testproject_api._EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS

    def test_execution_detail_unicode_conversion_failure_is_fixed_redaction(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-detail-unicode-conversion-failure",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data='{"safe":"CANARY_UNICODE_CONVERSION"}',
        )

        def reject_conversion(value):
            raise UnicodeError("forced conversion failure")

        monkeypatch.setattr(testproject_api, "safe_json_dumps", reject_conversion)

        detail = client.get(f"/api/executions/{task.pk}")
        listing = client.get("/api/executions", {"task_id": task.task_id})

        assert detail.status_code == listing.status_code == 200
        assert detail.json()["result_data"] == REDACTED
        assert listing.json()["tasks"][0]["result_data"] == REDACTED
        assert detail.json()["result_data_omission_reason"] is None
        assert listing.json()["tasks"][0]["result_data_omission_reason"] is None
        assert "CANARY_UNICODE_CONVERSION" not in detail.content.decode()
        assert "CANARY_UNICODE_CONVERSION" not in listing.content.decode()

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
            error_message="pass\x1b[31mword=error-secret",
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

    def test_get_execution_requires_authentication_before_database_access(
        self,
        unauthenticated_client,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-detail-auth-before-query",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        with CaptureQueriesContext(connection) as queries:
            response = unauthenticated_client.get(f"/api/executions/{task.pk}")

        assert response.status_code == 401
        assert not [
            query
            for query in queries.captured_queries
            if "django_ray_raytaskexecution" in query["sql"]
        ]

    def test_delete_execution_is_not_a_supported_lifecycle_operation(self, client):
        """A REST client cannot erase the durable row behind active Ray work."""
        task = RayTaskExecution.objects.create(
            task_id="test-delete",
            callable_path="test.task",
            state=TaskState.RUNNING,
        )

        response = client.delete(f"/api/executions/{task.pk}")
        assert response.status_code == 405
        assert RayTaskExecution.objects.filter(pk=task.pk, state=TaskState.RUNNING).exists()

    def test_openapi_does_not_advertise_execution_deletion(self, client):
        """The reusable sample advertises only bounded lifecycle operations."""
        schema_response = client.get("/api/openapi.json")
        assert schema_response.status_code == 200
        schema = schema_response.json()
        execution_path = schema["paths"]["/api/executions/{execution_id}"]
        assert set(execution_path) == {"get"}
        responses = execution_path["get"]["responses"]
        assert responses["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/TaskExecutionDetailSchema"
        }
        assert responses["503"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/TaskExecutionDetailUnavailableSchema"
        }
        detail_schema = schema["components"]["schemas"]["TaskExecutionDetailSchema"]
        detail_properties = detail_schema["properties"]
        assert detail_properties["diagnostic_max_bytes"]["const"] == (
            testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        )
        assert detail_properties["response_max_bytes"]["const"] == (
            testproject_api._EXECUTION_DETAIL_RESPONSE_MAX_BYTES
        )
        result_reason = next(
            item
            for item in detail_properties["result_data_omission_reason"]["anyOf"]
            if "enum" in item
        )
        assert result_reason["enum"] == [
            "stored_value_exceeds_detail_limit",
            "external_result_not_loaded",
            "response_size_limit",
        ]
        assert "/api/executions/reset" not in schema["paths"]
        assert "/api/cluster/workflows/{task_id}/nodes/{node_id}" not in schema["paths"]
        assert "MessageSchema" not in schema["components"]["schemas"]
        assert "WorkflowNodeSchema" not in schema["components"]["schemas"]
        assert set(schema["paths"]["/api/executions/{execution_id}/retry"]) == {"post"}
        assert set(schema["paths"]["/api/executions/{execution_id}/cancel"]) == {"post"}
        assert set(schema["paths"]["/api/cluster/workflows/{task_id}/node-detail"]) == {"get"}
        status_properties = schema["components"]["schemas"]["TaskStatusSchema"]["properties"]
        assert status_properties["input_max_bytes"]["const"] == (
            testproject_api._TASK_STATUS_INPUT_MAX_BYTES
        )
        assert status_properties["response_max_bytes"]["const"] == (
            testproject_api._TASK_STATUS_RESPONSE_MAX_BYTES
        )
        status_reason = next(
            item for item in status_properties["input_omission_reason"]["anyOf"] if "enum" in item
        )
        assert status_reason["enum"] == [
            "external_input_not_loaded",
            "stored_input_exceeds_status_limit",
            "malformed_inline_input",
            "encoded_response_limit",
        ]

    def test_cancel_queued_execution(self, client):
        """Queued cancellation returns a small accepted outcome and archives once."""
        task = RayTaskExecution.objects.create(
            task_id="test-cancel",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        response = client.post(f"/api/executions/{task.pk}/cancel")
        assert response.status_code == 202
        data = response.json()
        assert data == {
            "code": "ACCEPTED",
            "message": "Cancellation was accepted.",
            "execution_id": task.pk,
            "state": "CANCELLED",
            "attempt_number": 1,
            "execution_generation": 0,
            "next_action": "The queued attempt is cancelled; retain its archived history.",
            "response_max_bytes": testproject_api._CANCELLATION_RESPONSE_MAX_BYTES,
        }
        assert len(response.content) <= testproject_api._CANCELLATION_RESPONSE_MAX_BYTES
        assert response["Cache-Control"] == "no-store"
        assert response["X-Content-Type-Options"] == "nosniff"
        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
        assert TaskAttempt.objects.get(execution=task).state == TaskState.CANCELLED

    def test_cancel_running_execution_requests_worker_cancellation(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-cancel-running",
            callable_path="test.task",
            state=TaskState.RUNNING,
            execution_generation=4,
        )

        response = client.post(f"/api/executions/{task.pk}/cancel")

        assert response.status_code == 202
        data = response.json()
        assert data["code"] == "ACCEPTED"
        assert data["state"] == "CANCELLING"
        assert data["execution_generation"] == 4
        assert data["next_action"] == (
            "Poll until the worker records a terminal cancellation outcome."
        )
        assert "finished_at" not in data
        assert "cancellation_error" not in data

    def test_cancel_terminal_execution_is_an_explicit_conflict(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-cancel-terminal",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            execution_generation=2,
        )

        first = client.post(f"/api/executions/{task.pk}/cancel")
        second = client.post(f"/api/executions/{task.pk}/cancel")

        assert first.status_code == 409
        assert second.status_code == 409
        assert first.json()["code"] == "ALREADY_TERMINAL"
        assert second.json()["code"] == "ALREADY_TERMINAL"
        assert first.json()["state"] == "SUCCEEDED"
        assert second.json()["state"] == "SUCCEEDED"

    def test_cancel_terminal_execution_never_selects_or_returns_diagnostics(self, client):
        protected = "cancel-response-protected-marker"
        task = RayTaskExecution.objects.create(
            task_id="test-cancel-terminal-wide-result",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            result_data=json.dumps({"protected": protected}),
            result_reference=f"digest:{protected}",
            error_message=protected,
            error_traceback=protected,
            cancellation_error=protected,
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.post(f"/api/executions/{task.pk}/cancel")

        assert response.status_code == 409
        assert response.json()["code"] == "ALREADY_TERMINAL"
        assert response.json()["state"] == "SUCCEEDED"
        assert protected not in response.content.decode()
        assert set(response.json()) == {
            "code",
            "message",
            "execution_id",
            "state",
            "attempt_number",
            "execution_generation",
            "next_action",
            "response_max_bytes",
        }
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 2
        for sql in task_selects:
            for excluded in (
                "result_data",
                "result_reference",
                "error_message",
                "error_traceback",
                "cancellation_error",
                "runtime_env_json",
                "progress_data",
            ):
                assert excluded not in sql

    def test_cancel_duplicate_missing_and_post_preflight_race_are_distinct(
        self,
        client,
        monkeypatch,
    ):
        duplicate = RayTaskExecution.objects.create(
            task_id="test-cancel-duplicate",
            callable_path="test.task",
            state=TaskState.CANCELLING,
            attempt_number=2,
            execution_generation=3,
        )

        duplicate_response = client.post(f"/api/executions/{duplicate.pk}/cancel")
        missing_response = client.post("/api/executions/999999/cancel")

        assert duplicate_response.status_code == 409
        assert duplicate_response.json()["code"] == "ALREADY_REQUESTED"
        assert missing_response.status_code == 404
        assert missing_response.json()["code"] == "NOT_FOUND"

        observed: dict[str, int] = {}

        def raced(execution_id, *, expected_attempt_number, expected_execution_generation):
            observed.update(
                execution_id=execution_id,
                attempt_number=expected_attempt_number,
                execution_generation=expected_execution_generation,
            )
            return TaskCancellationRequestResult(
                status=TaskCancellationRequestStatus.STALE_ATTEMPT,
                execution_id=execution_id,
                state=TaskState.RUNNING,
                attempt_number=3,
                execution_generation=4,
            )

        monkeypatch.setattr(testproject_api, "request_task_cancellation", raced)
        raced_response = client.post(f"/api/executions/{duplicate.pk}/cancel")

        assert raced_response.status_code == 409
        assert raced_response.json()["code"] == "STALE_ATTEMPT"
        assert observed == {
            "execution_id": duplicate.pk,
            "attempt_number": 2,
            "execution_generation": 3,
        }

    def test_retry_failed_execution(self, client):
        """Test retrying a failed execution."""
        task = RayTaskExecution.objects.create(
            task_id="test-retry",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="Some error",
            attempt_number=1,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            response = client.post(f"/api/executions/{task.pk}/retry")
        assert response.status_code == 202
        data = response.json()
        assert data["code"] == "ACCEPTED"
        assert data["message"] == "A new task attempt was queued."
        assert data["execution_id"] == task.pk
        assert data["state"] == "QUEUED"
        assert data["attempt_number"] == 2
        assert data["execution_generation"] == 1
        assert data["next_action"] == "Poll or inspect the newly queued attempt."
        assert "result_data" not in data
        assert "error_message" not in data
        assert TaskAttempt.objects.get(execution=task).state == TaskState.FAILED

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            duplicate = client.post(f"/api/executions/{task.pk}/retry")
        assert duplicate.status_code == 409
        duplicate_data = duplicate.json()
        assert duplicate_data["code"] == "NOT_RETRYABLE"
        assert duplicate_data["state"] == "QUEUED"
        assert duplicate_data["attempt_number"] == 2
        assert duplicate_data["execution_generation"] == 1
        assert "FAILED, CANCELLED, LOST, or EXPIRED" in duplicate_data["next_action"]
        assert TaskAttempt.objects.filter(execution=task).count() == 1

    def test_retry_succeeded_execution_guides_a_fresh_enqueue_without_mutation(self, client):
        result_marker = "successful-result-must-not-leak-issue-321"
        reference_marker = "successful-reference-must-not-leak-issue-321"
        task = RayTaskExecution.objects.create(
            task_id="test-retry-succeeded-conflict",
            callable_path="test.task",
            state=TaskState.SUCCEEDED,
            attempt_number=3,
            execution_generation=3,
            workflow_run_id="00000000-0000-0000-0000-000000000321",
            result_data=json.dumps({"marker": result_marker}),
            result_reference=f"digest:{reference_marker}",
            finished_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 409
        data = response.json()
        assert data == {
            "code": "NOT_RETRYABLE",
            "message": "The execution is not retryable from its current state.",
            "execution_id": task.pk,
            "state": "SUCCEEDED",
            "attempt_number": 3,
            "execution_generation": 3,
            "next_action": (
                "Enqueue a new task under the application's authorization and idempotency "
                "policy; keep this successful execution as completed history."
            ),
        }
        serialized = response.content.decode()
        assert result_marker not in serialized
        assert reference_marker not in serialized
        assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_retry_raced_identity_returns_only_bounded_current_identity(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-raced-identity",
            callable_path="test.task",
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=4,
            workflow_run_id="00000000-0000-0000-0000-000000000322",
            workflow_plan_fingerprint="sha256:" + ("a" * 64),
            result_data='{"marker":"raced-result-must-not-leak"}',
            error_message="raced-error-must-not-leak",
        )

        def raced_retry(execution_id, **kwargs):
            assert execution_id == task.pk
            assert kwargs["expected_attempt_number"] == 2
            assert kwargs["expected_execution_generation"] == 4
            assert kwargs["expected_workflow_identity"] == (
                "00000000-0000-0000-0000-000000000322",
                "sha256:" + ("a" * 64),
            )
            return TaskRetryRequestResult(
                status=TaskRetryRequestStatus.STALE_ATTEMPT,
                execution_id=task.pk,
                state=TaskState.QUEUED,
                attempt_number=3,
                execution_generation=5,
            )

        monkeypatch.setattr("testproject.api.request_task_retry", raced_retry)

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 409
        assert response.json() == {
            "code": "STALE_ATTEMPT",
            "message": "The execution attempt changed before the retry could be applied.",
            "execution_id": task.pk,
            "state": "QUEUED",
            "attempt_number": 3,
            "execution_generation": 5,
            "next_action": (
                "Refresh and re-authorize the current attempt before deciding whether to retry."
            ),
        }
        serialized = response.content.decode()
        assert "raced-result-must-not-leak" not in serialized
        assert "raced-error-must-not-leak" not in serialized

    def test_retry_missing_execution_returns_a_structured_not_found(self, client):
        response = client.post("/api/executions/99999/retry")

        assert response.status_code == 404
        assert response.json() == {
            "code": "NOT_FOUND",
            "message": "The execution was not found.",
            "execution_id": 99999,
            "state": None,
            "attempt_number": None,
            "execution_generation": None,
            "next_action": "Verify the execution identifier and object authorization.",
        }

    def test_retry_raced_deletion_returns_structured_not_found(
        self,
        client,
        monkeypatch,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-raced-deletion",
            callable_path="test.task",
            state=TaskState.FAILED,
            result_data='{"marker":"deleted-race-result-must-not-leak"}',
            error_message="deleted-race-error-must-not-leak",
        )

        def raced_retry(execution_id, **_kwargs):
            assert execution_id == task.pk
            return TaskRetryRequestResult(
                status=TaskRetryRequestStatus.NOT_FOUND,
                execution_id=task.pk,
                state=None,
                attempt_number=None,
                execution_generation=None,
            )

        monkeypatch.setattr("testproject.api.request_task_retry", raced_retry)

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 404
        assert response.json() == {
            "code": "NOT_FOUND",
            "message": "The execution was not found.",
            "execution_id": task.pk,
            "state": None,
            "attempt_number": None,
            "execution_generation": None,
            "next_action": "Verify the execution identifier and object authorization.",
        }
        serialized = response.content.decode()
        assert "deleted-race-result-must-not-leak" not in serialized
        assert "deleted-race-error-must-not-leak" not in serialized

    def test_retry_execution_requires_bearer_authorization(
        self,
        unauthenticated_client,
    ):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-unauthorized",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="authorization must run before retry",
        )

        response = unauthenticated_client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 401
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_retry_rejects_corrupt_runtime_env_without_disclosure(self, client):
        task = RayTaskExecution.objects.create(
            task_id="test-retry-runtime-env-corrupt",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="original failure",
            attempt_number=2,
            execution_generation=4,
            runtime_env_json=('{"env_vars":{"VALUE":"arbitrary-customer-marker-7cf3"}}'),
            runtime_env_hash="0" * 64,
        )

        response = client.post(f"/api/executions/{task.pk}/retry")

        assert response.status_code == 409
        assert response.json() == {"detail": "Persisted RuntimeEnv snapshot failed validation"}
        assert "arbitrary-customer-marker-7cf3" not in response.content.decode()
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 2
        assert task.execution_generation == 4
        assert task.error_message == "original failure"
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_bulk_reset_route_is_removed_without_mutating_retryable_rows(self, client):
        failed = RayTaskExecution.objects.create(
            task_id="test-retired-bulk-reset",
            callable_path="test.task",
            state=TaskState.FAILED,
            progress_data='{"revision":4}',
            workflow_run_id="00000000-0000-0000-0000-000000000126",
            error_message="retryable",
            attempt_number=2,
            execution_generation=4,
        )
        terminal = serialize_workflow_progress_summary(
            workflow_progress_summary(failed, state="FAILED")
        )
        failed.workflow_progress_summary_json = terminal
        failed.save(update_fields=["workflow_progress_summary_json"])
        before = RayTaskExecution.objects.filter(pk=failed.pk).values().get()

        response = client.post("/api/executions/reset")

        assert response.status_code in {404, 405}
        assert RayTaskExecution.objects.filter(pk=failed.pk).values().get() == before
        assert not TaskAttempt.objects.filter(execution=failed).exists()
        schema = client.get("/api/openapi.json").json()
        assert "/api/executions/reset" not in schema["paths"]
        assert set(schema["paths"]["/api/executions/{execution_id}/retry"]) == {"post"}

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
        RayTaskExecution.objects.create(
            task_id="test-4", callable_path="test", state=TaskState.EXPIRED
        )

        response = client.get("/api/executions/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 4
        assert data["queued"] == 1
        assert data["succeeded"] == 1
        assert data["failed"] == 1
        assert data["expired"] == 1

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
