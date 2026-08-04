"""
Locust load testing for django-ray task execution API.

This file provides load testing scenarios to evaluate how Ray handles
concurrent task submissions and execution across different modes:
- Sync Tasks: Simple synchronous execution (--sync mode)
- Local Ray: Local Ray cluster execution (--local mode)
- Cluster Tasks: Distributed Ray cluster execution (--cluster mode)
- ML Pipeline: Machine learning workloads (ml queue)
- Stress Tests: Push the system to its limits

Usage:
    # Supply the protected testproject API token without putting it on the
    # command line, then run the resource-bounded observability demo.
    export DJANGO_API_TOKEN="<local demo token>"
    locust -f locustfile.py --host=http://localhost:30080 \
        --headless -u 1 -r 1 -t 5m --stop-timeout 150 \
        --require-complete-tour ObservabilityDemoUser

    # Run the same one-user scenario with the web UI on http://localhost:8089.
    locust -f locustfile.py --host=http://localhost:30080 \
        --stop-timeout 150 ObservabilityDemoUser

    # Run only the bounded order-fulfillment workflow showcase. This class is
    # intentionally absent from default Locust discovery unless named here.
    locust -f locustfile.py --host=http://localhost:30080 \
        --headless -u 1 -r 1 -t 5m --stop-timeout 150 WorkflowShowcaseUser

Scenarios:
    - BasicTaskUser: Submits quick add_numbers/multiply tasks
    - SyncTaskUser: Uses sync mode tasks (simple calculations)
    - LocalRayUser: Uses local Ray mode (fibonacci, workload)
    - ClusterTaskUser: Tests distributed computing features
    - WorkflowUser: Exercises fan-out and nested workflow composition (0.3.0)
    - RuntimeEnvUser: Exercises named RuntimeEnv profiles and cache benchmarks (0.3.0)
    - MLPipelineUser: Tests ML pipeline tasks
    - StressTestUser: Pushes system to limits
    - MonitoringUser: Monitors task statistics and health
    - ObservabilityDemoUser: Rotates through lightweight task families one at a time
    - WorkflowShowcaseUser: Runs one bounded successful workflow showcase at a time

Metrics to watch:
    - Response time for task creation (should be fast, just DB insert)
    - Task throughput (tasks created per second)
    - Ray Dashboard for task execution backlog
    - Task completion rate (check /api/executions/stats)
"""

import logging
import os
import random
import sys
import time
from typing import Any, Literal

from locust import HttpUser, between, events, task
from locust.exception import StopTest

_API_TOKEN_ENV = "DJANGO_API_TOKEN"
_LOGGER = logging.getLogger(__name__)
_INCOMPLETE_DEMO_TOUR_MESSAGE = "Observability demo ended before one complete task-family tour."
_TERMINAL_SUCCESS_STATES = frozenset({"SUCCESSFUL"})
_TASK_STATUS_TERMINAL_FAILURE_STATES = frozenset({"FAILED", "CANCELLED", "LOST", "EXPIRED"})
_TASK_STATUS_ACTIVE_STATES = frozenset({"QUEUED", "RUNNING", "CANCELLING"})
_TASK_STATUS_BY_STATE = {
    "QUEUED": "READY",
    "RUNNING": "RUNNING",
    "SUCCEEDED": "SUCCESSFUL",
    "FAILED": "FAILED",
    "CANCELLED": "FAILED",
    "CANCELLING": "RUNNING",
    "LOST": "FAILED",
    "EXPIRED": "FAILED",
}
_TASK_STATUS_INPUT_MAX_BYTES = 16 * 1024
_TASK_STATUS_RESPONSE_MAX_BYTES = 64 * 1024
_TASK_STATUS_INPUT_OMISSION_REASONS = frozenset(
    {
        None,
        "external_input_not_loaded",
        "stored_input_exceeds_status_limit",
        "malformed_inline_input",
        "encoded_response_limit",
    }
)
_REQUEST_TIMEOUT_SECONDS = 10.0
_WorkflowReportingPolicy = Literal["full", "terminal_only", "disabled"]
_WORKFLOW_POLICY_DETAIL_EXPECTATIONS: dict[
    _WorkflowReportingPolicy,
    tuple[str, bool],
] = {
    "full": ("AVAILABLE", True),
    "terminal_only": ("OMITTED_BY_POLICY", False),
    "disabled": ("DISABLED", False),
}
_WORKFLOW_SHOWCASE_EXPECTED_RESULT = {
    "engine": "django-ray-workflow",
    "workflow": "order-fulfillment-showcase",
    "durability_boundary": "single RayTaskExecution",
    "order_id": "showcase-order-0001",
    "status": "FULFILLED",
    "item_count": 3,
    "reserved_units": 4,
    "currency": "USD",
    "total_cents": 4_400,
    "risk": "LOW",
    "recommendation": "PRIORITY_FULFILLMENT",
    "decision": "APPROVED",
    "sinks": {
        "primary": "WRITTEN",
        "audit": "WRITTEN",
        "notification": "SENT",
    },
}
_EXPLICIT_ONLY_USER_CLASSES = frozenset(
    {
        "BurstTaskUser",
        "DistributedComputingUser",
        "StressTestUser",
        "SustainedLoadUser",
        "WorkflowShowcaseUser",
    }
)


def _task_status_contract_error(
    response: Any,
    payload: dict[str, Any],
    *,
    task_id: str,
) -> str | None:
    """Return a fixed error when one status response violates its bounded contract."""
    content = getattr(response, "content", None)
    if not isinstance(content, bytes | bytearray):
        return "status response did not expose bounded response bytes"
    if len(content) > _TASK_STATUS_RESPONSE_MAX_BYTES:
        return "status response exceeded its 64 KiB byte limit"

    headers = getattr(response, "headers", None)
    if not hasattr(headers, "get"):
        return "status response did not expose its cache-safety headers"
    if headers.get("Cache-Control") != "no-store":
        return "status response did not disable caching"
    if headers.get("X-Content-Type-Options") != "nosniff":
        return "status response did not advertise nosniff"

    if payload.get("task_id") != task_id:
        return "status response returned a mismatched task ID"
    state = payload.get("state")
    expected_status = _TASK_STATUS_BY_STATE.get(state)
    if expected_status is None or payload.get("status") != expected_status:
        return "status response returned an inconsistent state/status pair"
    attempt_number = payload.get("attempt_number")
    if type(attempt_number) is not int or attempt_number < 1:
        return "status response returned an invalid attempt number"
    execution_generation = payload.get("execution_generation")
    if type(execution_generation) is not int or execution_generation < 0:
        return "status response returned an invalid execution generation"
    if payload.get("input_max_bytes") != _TASK_STATUS_INPUT_MAX_BYTES:
        return "status response changed its input byte limit"
    if payload.get("response_max_bytes") != _TASK_STATUS_RESPONSE_MAX_BYTES:
        return "status response changed its encoded byte limit"

    if "input_omission_reason" not in payload:
        return "status response omitted its input omission reason"
    omission_reason = payload.get("input_omission_reason")
    if omission_reason is not None and (
        not isinstance(omission_reason, str)
        or omission_reason not in _TASK_STATUS_INPUT_OMISSION_REASONS
    ):
        return "status response returned an unknown input omission reason"
    args = payload.get("args")
    kwargs = payload.get("kwargs")
    if omission_reason is None:
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return "status response omitted inline input without a reason"
    elif args is not None or kwargs is not None:
        return "status response returned input together with an omission reason"
    return None


@events.init_command_line_parser.add_listener
def _add_django_ray_loadtest_arguments(parser: Any, **_kwargs: Any) -> None:
    """Register opt-in assertions used by deterministic headless demos."""

    parser.add_argument(
        "--require-complete-tour",
        action="store_true",
        default=False,
        help=("Fail when ObservabilityDemoUser stops before completing every task family once"),
    )


def _configured_api_token() -> str | None:
    token = os.environ.get(_API_TOKEN_ENV)
    if token is None or not token.strip():
        return None
    return token.strip()


def _missing_api_token_error() -> RuntimeError:
    return RuntimeError(
        "DJANGO_API_TOKEN must be set before running Locust against protected task routes"
    )


def _explicit_user_selected(class_name: str) -> bool:
    """Return whether Locust was invoked with one exact user class name."""

    return any(argument == class_name for argument in sys.argv[1:])


def _workflow_policy_summary_matches(
    data: dict[str, Any],
    *,
    task_id: str,
    expected_policy: _WorkflowReportingPolicy,
) -> bool:
    """Validate one terminal bounded-summary response without exposing its payload."""
    expected_availability, expected_complete = _WORKFLOW_POLICY_DETAIL_EXPECTATIONS[expected_policy]
    if (
        data.get("task_id") != task_id
        or data.get("availability") != expected_availability
        or data.get("complete") is not expected_complete
    ):
        return False

    summary = data.get("summary")
    if expected_policy == "disabled":
        return data.get("source_schema_version") is None and summary is None
    if data.get("source_schema_version") != 3 or not isinstance(summary, dict):
        return False
    detail = summary.get("detail")
    return (
        summary.get("reporting_policy") == expected_policy
        and summary.get("state") == "SUCCEEDED"
        and isinstance(detail, dict)
        and detail.get("availability") == expected_availability
        and detail.get("complete") is expected_complete
    )


def _workflow_showcase_result_matches(
    data: dict[str, Any],
    *,
    task_id: str,
) -> bool:
    """Require both the compact result and its complete bounded publication."""
    progress = data.get("progress")
    summary = progress.get("summary") if isinstance(progress, dict) else None
    node_counts = summary.get("node_counts") if isinstance(summary, dict) else None
    edge_counts = summary.get("edge_counts") if isinstance(summary, dict) else None
    run_identity = progress.get("run_identity") if isinstance(progress, dict) else None
    publication = progress.get("publication") if isinstance(progress, dict) else None
    identity_matches = (
        isinstance(run_identity, dict)
        and run_identity.get("schema_version") == 1
        and run_identity.get("attempt_number") == 1
        and type(run_identity.get("execution_generation")) is int
        and run_identity["execution_generation"] >= 1
        and isinstance(run_identity.get("run_id"), str)
        and bool(run_identity["run_id"])
        and isinstance(summary, dict)
        and summary.get("run_identity") == run_identity
    )
    publication_matches = isinstance(publication, dict) and all(
        type(publication.get(field_name)) is int
        and publication[field_name] >= 1
        and isinstance(summary, dict)
        and summary.get(field_name) == publication[field_name]
        for field_name in ("summary_revision", "topology_version", "detail_revision")
    )
    return (
        data.get("task_id") == task_id
        and data.get("state") == "SUCCEEDED"
        and data.get("error") is None
        and data.get("result") == _WORKFLOW_SHOWCASE_EXPECTED_RESULT
        and isinstance(progress, dict)
        and progress.get("schema") == "django-ray.workflow-progress-summary"
        and progress.get("schema_version") == 1
        and identity_matches
        and publication_matches
        and _workflow_policy_summary_matches(
            progress,
            task_id=task_id,
            expected_policy="full",
        )
        and isinstance(node_counts, dict)
        and node_counts.get("discovered") == 25
        and node_counts.get("retained_topology") == 25
        and node_counts.get("retained_detail") == 25
        and node_counts.get("pending") == 0
        and node_counts.get("running") == 0
        and node_counts.get("succeeded") == 25
        and node_counts.get("failed") == 0
        and isinstance(edge_counts, dict)
        and edge_counts.get("discovered") == 36
        and edge_counts.get("retained_topology") == 36
    )


@events.test_start.add_listener
def _require_api_token_before_load(environment: Any, **_kwargs: Any) -> None:
    """Abort before spawning task users when the protected API token is absent."""
    if _configured_api_token() is None:
        environment.process_exit_code = 2
        raise StopTest(str(_missing_api_token_error()))


@events.test_start.add_listener
def _reject_accidental_broad_capacity_mix(environment: Any, **_kwargs: Any) -> None:
    """Require burst, sustained, distributed, and stress users to run alone."""
    selected = {user_class.__name__ for user_class in environment.user_classes}
    explicit_only = selected & _EXPLICIT_ONLY_USER_CLASSES
    if explicit_only and len(selected) != 1:
        environment.process_exit_code = 2
        names = ", ".join(sorted(explicit_only))
        raise StopTest(f"{names} must be selected explicitly without other Locust user classes")


class TaskCreationMixin:
    """Mixin providing common task creation and monitoring methods."""

    def _post_task(
        self, endpoint: str, name: str | None = None, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Generic task creation helper."""
        name = name or endpoint
        kwargs = {
            "name": name,
            "catch_response": True,
            "timeout": _REQUEST_TIMEOUT_SECONDS,
        }
        if payload:
            kwargs["json"] = payload

        with self.client.post(endpoint, **kwargs) as response:
            if response.status_code != 200:
                response.failure(f"Failed to create task: {response.status_code}")
                return None
            try:
                data = response.json()
            except ValueError:
                response.failure("Task response was not valid JSON")
                return None
            if not isinstance(data, dict) or not isinstance(data.get("task_id"), str):
                response.failure("Task response did not contain a task_id")
                return None
            response.success()
            return data

    def _get(self, endpoint: str, name: str | None = None) -> dict[str, Any] | None:
        """Generic GET helper."""
        name = name or endpoint
        with self.client.get(
            endpoint,
            name=name,
            catch_response=True,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code != 200:
                response.failure(f"GET failed: {response.status_code}")
                return None
            try:
                data = response.json()
            except ValueError:
                response.failure("GET response was not valid JSON")
                return None
            if not isinstance(data, dict):
                response.failure("GET response was not an object")
                return None
            response.success()
            return data

    def _poll_task_to_terminal(
        self,
        task_id: str,
        *,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 1.0,
        scenario_name: str = "task",
    ) -> dict[str, Any] | None:
        """Poll one durable task to a bounded terminal result."""
        deadline = time.monotonic() + timeout_seconds
        request_name = f"/api/tasks/[task_id] ({scenario_name})"

        while True:
            remaining_seconds = max(deadline - time.monotonic(), 0.1)
            with self.client.get(
                f"/api/tasks/{task_id}",
                name=request_name,
                catch_response=True,
                timeout=min(_REQUEST_TIMEOUT_SECONDS, remaining_seconds),
            ) as response:
                if response.status_code != 200:
                    response.failure(f"{scenario_name} status read failed: {response.status_code}")
                    return None
                try:
                    data = response.json()
                except ValueError:
                    response.failure(f"{scenario_name} status response was not valid JSON")
                    return None
                if not isinstance(data, dict):
                    response.failure(f"{scenario_name} status response was not an object")
                    return None

                contract_error = _task_status_contract_error(
                    response,
                    data,
                    task_id=task_id,
                )
                if contract_error is not None:
                    response.failure(f"{scenario_name} {contract_error}")
                    return None

                state = data["state"]
                if state == "SUCCEEDED":
                    response.success()
                    return data
                if state in _TASK_STATUS_TERMINAL_FAILURE_STATES:
                    response.failure(f"{scenario_name} reached terminal state {state}")
                    return data
                if state not in _TASK_STATUS_ACTIVE_STATES:
                    response.failure(f"{scenario_name} returned unknown task state")
                    return None
                if time.monotonic() >= deadline:
                    response.failure(
                        f"{scenario_name} did not reach a terminal state within "
                        f"{timeout_seconds:.0f}s"
                    )
                    return None
                response.success()

            time.sleep(poll_interval_seconds)

    # ========== Health & Monitoring ==========

    def _check_health(self):
        """Check API health."""
        data = self._get("/api/health")
        return data and data.get("status") == "healthy"

    def _get_stats(self):
        """Get task execution statistics."""
        return self._get("/api/executions/stats")

    def _get_metrics(self):
        """Get Prometheus metrics."""
        with self.client.get(
            "/api/metrics",
            name="/api/metrics",
            catch_response=True,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code == 200:
                response.success()
                return response.text
            else:
                response.failure(f"Metrics failed: {response.status_code}")
                return None

    # ========== Basic Tasks (default queue) ==========

    def create_add_task(self, a: int | None = None, b: int | None = None) -> dict[str, Any] | None:
        """Create an add_numbers task."""
        a = a or random.randint(1, 1000)
        b = b or random.randint(1, 1000)
        return self._post_task(f"/api/enqueue/add/{a}/{b}", "/api/enqueue/add/[a]/[b]")

    def create_multiply_task(
        self, a: int | None = None, b: int | None = None
    ) -> dict[str, Any] | None:
        """Create a multiply_numbers task."""
        a = a or random.randint(1, 100)
        b = b or random.randint(1, 100)
        return self._post_task(f"/api/enqueue/multiply/{a}/{b}", "/api/enqueue/multiply/[a]/[b]")

    def create_slow_task(self, seconds: float | None = None) -> dict[str, Any] | None:
        """Create a slow_task that sleeps for a duration."""
        seconds = seconds or random.uniform(0.5, 2.0)
        return self._post_task(f"/api/enqueue/slow/{seconds:.1f}", "/api/enqueue/slow/[seconds]")

    def create_cpu_task(self, n: int | None = None) -> dict[str, Any] | None:
        """Create a CPU-intensive task."""
        n = n or random.randint(100000, 500000)
        return self._post_task(f"/api/enqueue/cpu/{n}", "/api/enqueue/cpu/[n]")

    def create_fail_task(self):
        """Create a task that always fails."""
        return self._post_task("/api/enqueue/fail")

    # ========== Sync Tasks (sync queue) ==========

    def sync_calculate(
        self, a: int | None = None, b: int | None = None, operation: str | None = None
    ) -> dict[str, Any] | None:
        """Create a sync calculation task."""
        a = a or random.randint(1, 100)
        b = b or random.randint(1, 100)
        operation = operation or random.choice(["add", "subtract", "multiply", "divide"])
        return self._post_task(
            f"/api/sync/calculate?a={a}&b={b}&operation={operation}", "/api/sync/calculate"
        )

    def sync_validate_email(self, email: str | None = None) -> dict[str, Any] | None:
        """Validate an email address."""
        email = email or f"user{random.randint(1, 1000)}@example.com"
        return self._post_task(
            f"/api/sync/validate-email?email={email}", "/api/sync/validate-email"
        )

    # ========== Local Ray Tasks (default queue) ==========

    def local_fibonacci(self, n: int | None = None) -> dict[str, Any] | None:
        """Calculate fibonacci number."""
        n = n or random.randint(10, 30)
        return self._post_task(f"/api/local/fibonacci/{n}", "/api/local/fibonacci/[n]")

    def local_workload(
        self, iterations: int | None = None, sleep_ms: int | None = None
    ) -> dict[str, Any] | None:
        """Simulate CPU workload."""
        iterations = iterations or random.randint(100000, 1000000)
        sleep_ms = sleep_ms or random.randint(0, 100)
        return self._post_task(
            f"/api/local/workload?iterations={iterations}&sleep_ms={sleep_ms}",
            "/api/local/workload",
        )

    def local_urgent(self, message: str | None = None) -> dict[str, Any] | None:
        """High-priority urgent task."""
        message = message or f"Urgent-{random.randint(1, 1000)}"
        return self._post_task(f"/api/local/urgent?message={message}", "/api/local/urgent")

    # ========== Cluster Tasks (distributed) ==========

    def cluster_process_chunk(
        self, data: list[int] | None = None, chunk_id: int | None = None
    ) -> dict[str, Any] | None:
        """Process a data chunk on cluster."""
        data = data or [random.randint(1, 100) for _ in range(random.randint(10, 50))]
        chunk_id = chunk_id or random.randint(1, 100)
        return self._post_task(
            "/api/cluster/process-chunk", payload={"data": data, "chunk_id": chunk_id}
        )

    def cluster_batch_http(
        self, urls: list[str] | None = None, timeout: int = 30
    ) -> dict[str, Any] | None:
        """Simulate batch HTTP requests."""
        urls = urls or [f"https://example.com/api/{i}" for i in range(random.randint(3, 10))]
        return self._post_task(
            "/api/cluster/batch-http", payload={"urls": urls, "timeout_seconds": timeout}
        )

    def cluster_search(
        self, pattern: str | None = None, sources: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Distributed search across data sources."""
        pattern = pattern or random.choice(["test", "data", "user", "api", "error"])
        sources = sources or [
            f"source{i}_{pattern if random.random() > 0.5 else 'other'}"
            for i in range(random.randint(3, 8))
        ]
        return self._post_task(
            "/api/cluster/search",
            payload={"pattern": pattern, "data_sources": sources, "case_sensitive": False},
        )

    def cluster_cpu_benchmark(
        self, num_items: int | None = None, seconds_per_item: float | None = None
    ) -> dict[str, Any] | None:
        """Benchmark distributed CPU work."""
        num_items = num_items or random.randint(4, 16)
        seconds_per_item = seconds_per_item or random.uniform(1.0, 3.0)
        return self._post_task(
            f"/api/cluster/cpu-benchmark?num_items={num_items}&seconds_per_item={seconds_per_item}",
            "/api/cluster/cpu-benchmark",
        )

    # ========== Workflows (0.3.0) ==========

    def workflow_fanout_benchmark(
        self, num_items: int | None = None, seconds_per_item: float | None = None
    ) -> dict[str, Any] | None:
        """Enqueue a fan-out workflow benchmark."""
        num_items = num_items or random.randint(4, 12)
        seconds_per_item = seconds_per_item or random.uniform(0.1, 0.5)
        return self._post_task(
            f"/api/cluster/workflow-benchmark"
            f"?num_items={num_items}&seconds_per_item={seconds_per_item:.2f}",
            "/api/cluster/workflow-benchmark",
        )

    def workflow_fanout_result(self, task_id: str) -> dict[str, Any] | None:
        """Poll fan-out workflow result."""
        return self._get(
            f"/api/cluster/workflow-benchmark/{task_id}",
            "/api/cluster/workflow-benchmark/[task_id]",
        )

    def complex_workflow(
        self,
        fast_items: int | None = None,
        slow_items: int | None = None,
        reporting_policy: _WorkflowReportingPolicy | None = None,
    ) -> dict[str, Any] | None:
        """Enqueue a nested group/chain workflow."""
        fast_items = fast_items or random.randint(4, 10)
        slow_items = slow_items or random.randint(2, 6)
        endpoint = (
            f"/api/cluster/complex-workflow"
            f"?fast_items={fast_items}&slow_items={slow_items}"
            f"&fast_seconds=0.02&slow_seconds=0.3"
        )
        request_name = "/api/cluster/complex-workflow"
        if reporting_policy is not None:
            endpoint += f"&reporting_policy={reporting_policy}"
            request_name += f" ({reporting_policy})"
        return self._post_task(
            endpoint,
            request_name,
        )

    def complex_workflow_result(self, task_id: str) -> dict[str, Any] | None:
        """Poll complex workflow result."""
        return self._get(
            f"/api/cluster/complex-workflow/{task_id}",
            "/api/cluster/complex-workflow/[task_id]",
        )

    def workflow_showcase(
        self,
        *,
        item_count: int,
        work_seconds: float,
    ) -> dict[str, Any] | None:
        """Enqueue the successful order-fulfillment workflow showcase."""
        return self._post_task(
            f"/api/cluster/workflow-showcase?item_count={item_count}&work_seconds={work_seconds:g}",
            "/api/cluster/workflow-showcase",
        )

    def workflow_showcase_result(self, task_id: str) -> dict[str, Any] | None:
        """Read the terminal showcase result and its bounded progress summary."""
        return self._get(
            f"/api/cluster/workflow-showcase/{task_id}",
            "/api/cluster/workflow-showcase/[task_id]",
        )

    def workflow_policy_summary(
        self,
        task_id: str,
        *,
        expected_policy: _WorkflowReportingPolicy,
    ) -> dict[str, Any] | None:
        """Read and validate one policy's terminal bounded workflow summary."""
        request_name = f"/api/cluster/workflows/[task_id] ({expected_policy})"
        with self.client.get(
            f"/api/cluster/workflows/{task_id}",
            name=request_name,
            catch_response=True,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code != 200:
                response.failure(
                    f"{expected_policy} workflow summary read failed: {response.status_code}"
                )
                return None
            try:
                data = response.json()
            except ValueError:
                response.failure(f"{expected_policy} workflow summary response was not valid JSON")
                return None
            if not isinstance(data, dict) or not _workflow_policy_summary_matches(
                data,
                task_id=task_id,
                expected_policy=expected_policy,
            ):
                response.failure(
                    f"{expected_policy} workflow summary did not match its bounded policy contract"
                )
                return None
            response.success()
            return data

    # ========== Runtime Environments (0.3.0) ==========

    def runtime_env_probe(self, profile: str | None = None) -> dict[str, Any] | None:
        """Enqueue a task through a named RuntimeEnv profile."""
        profile = profile or random.choice(["project", "thin"])
        return self._post_task(
            f"/api/cluster/runtime-env/probe?profile={profile}",
            "/api/cluster/runtime-env/probe",
        )

    def runtime_env_benchmark(
        self, profile: str | None = None, repeats: int | None = None
    ) -> dict[str, Any] | None:
        """Time repeated workflow leaves to compare cold vs cached env setup."""
        profile = profile or random.choice(["thin", "numpy-2-2"])
        repeats = repeats or random.randint(2, 4)
        return self._post_task(
            f"/api/cluster/runtime-env/benchmark?profile={profile}&repeats={repeats}",
            "/api/cluster/runtime-env/benchmark",
        )

    def runtime_env_result(self, task_id: str) -> dict[str, Any] | None:
        """Fetch a runtime-env probe or benchmark result."""
        return self._get(
            f"/api/cluster/runtime-env/{task_id}",
            "/api/cluster/runtime-env/[task_id]",
        )

    # ========== Stress Tests ==========

    def stress_cpu(self, duration: float | None = None) -> dict[str, Any] | None:
        """CPU burn stress test."""
        duration = duration or random.uniform(1.0, 5.0)
        return self._post_task(f"/api/stress/cpu?duration_seconds={duration}", "/api/stress/cpu")

    def stress_memory(self, size_mb: int | None = None) -> dict[str, Any] | None:
        """Memory allocation stress test."""
        size_mb = size_mb or random.randint(50, 200)
        return self._post_task(f"/api/stress/memory?size_mb={size_mb}", "/api/stress/memory")

    def stress_compute(
        self, depth: int | None = None, width: int | None = None
    ) -> dict[str, Any] | None:
        """Nested computation stress test."""
        depth = depth or random.randint(5, 12)
        width = width or random.randint(50, 150)
        return self._post_task(
            f"/api/stress/compute?depth={depth}&width={width}", "/api/stress/compute"
        )

    def stress_primes(
        self, start: int | None = None, count: int | None = None
    ) -> dict[str, Any] | None:
        """Prime number search stress test."""
        start = start or random.randint(100000, 1000000)
        count = count or random.randint(10, 100)
        return self._post_task(
            f"/api/stress/primes?start={start}&count={count}", "/api/stress/primes"
        )

    def stress_json(
        self, size_kb: int | None = None, depth: int | None = None
    ) -> dict[str, Any] | None:
        """Large JSON structure stress test."""
        size_kb = size_kb or random.randint(50, 200)
        depth = depth or random.randint(3, 7)
        return self._post_task(
            f"/api/stress/json?size_kb={size_kb}&depth={depth}", "/api/stress/json"
        )

    def stress_throughput(
        self, task_count: int | None = None, duration_ms: int | None = None
    ) -> dict[str, Any] | None:
        """Throughput simulation stress test."""
        task_count = task_count or random.randint(50, 200)
        duration_ms = duration_ms or random.randint(5, 50)
        return self._post_task(
            f"/api/stress/throughput?task_count={task_count}&task_duration_ms={duration_ms}",
            "/api/stress/throughput",
        )

    # ========== ML Pipeline ==========

    def ml_train(
        self, dataset_id: str | None = None, epochs: int | None = None
    ) -> dict[str, Any] | None:
        """Train a model."""
        dataset_id = dataset_id or f"dataset-{random.randint(1, 100)}"
        epochs = epochs or random.randint(5, 20)
        return self._post_task(
            "/api/ml/train", payload={"dataset_id": dataset_id, "epochs": epochs}
        )

    def ml_inference(
        self, model_id: str | None = None, samples: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        """Run batch inference."""
        model_id = model_id or f"model-{random.randint(1, 10)}"
        samples = samples or [
            {"features": [random.random() for _ in range(10)]} for _ in range(random.randint(5, 20))
        ]
        return self._post_task(
            "/api/ml/inference", payload={"model_id": model_id, "samples": samples}
        )

    def ml_hyperparam_search(self, dataset_id: str | None = None) -> dict[str, Any] | None:
        """Run hyperparameter grid search."""
        dataset_id = dataset_id or f"dataset-{random.randint(1, 100)}"
        param_grid = {
            "learning_rate": [0.001, 0.01, 0.1],
            "batch_size": [16, 32, 64],
            "hidden_size": [64, 128, 256],
        }
        return self._post_task(
            "/api/ml/hyperparam-search",
            payload={"dataset_id": dataset_id, "param_grid": param_grid, "metric": "accuracy"},
        )


# ============================================================================
# User Classes - Different Usage Patterns
# ============================================================================


class AuthenticatedTaskUser(TaskCreationMixin, HttpUser):
    """Abstract Locust user that authenticates the protected sample API."""

    abstract = True

    def on_start(self) -> None:
        token = _configured_api_token()
        if token is None:
            raise _missing_api_token_error()
        self.client.headers.update({"Authorization": f"Bearer {token}"})


class BasicTaskUser(AuthenticatedTaskUser):
    """
    User that submits basic math tasks.

    Good for testing basic task creation overhead and queue throughput.
    """

    wait_time = between(0.1, 0.5)
    weight = 3

    @task(10)
    def submit_add(self):
        self.create_add_task()

    @task(5)
    def submit_multiply(self):
        self.create_multiply_task()

    @task(2)
    def submit_slow(self):
        self.create_slow_task(seconds=random.uniform(0.5, 1.5))

    @task(1)
    def check_stats(self):
        self._get_stats()


class SyncTaskUser(AuthenticatedTaskUser):
    """
    User that tests sync mode tasks.

    Requires worker running with: --sync --queue=sync
    """

    wait_time = between(0.5, 1.5)
    weight = 1

    @task(5)
    def calculate(self):
        self.sync_calculate()

    @task(3)
    def validate_email(self):
        self.sync_validate_email()

    @task(1)
    def health_check(self):
        self._check_health()


class LocalRayUser(AuthenticatedTaskUser):
    """
    User that tests the historical ``/local`` sample endpoints.

    These routes use the default queue and run through whichever Ray-backed
    worker mode serves it. The local KubeRay stack uses cluster mode.
    """

    wait_time = between(0.5, 2.0)
    weight = 2

    @task(5)
    def fibonacci(self):
        self.local_fibonacci(n=random.randint(10, 25))

    @task(3)
    def workload(self):
        self.local_workload(iterations=random.randint(100000, 500000))

    @task(2)
    def urgent(self):
        self.local_urgent()

    @task(1)
    def check_stats(self):
        self._get_stats()


class ClusterTaskUser(AuthenticatedTaskUser):
    """
    User that tests distributed cluster tasks.

    Requires worker running with: --cluster ray://head:10001
    Tests real distributed computing features.
    """

    wait_time = between(1, 3)
    weight = 2

    @task(4)
    def cpu_benchmark(self):
        """Test parallel CPU work distribution."""
        self.cluster_cpu_benchmark(
            num_items=random.randint(4, 12), seconds_per_item=random.uniform(1.0, 2.0)
        )

    @task(3)
    def distributed_search(self):
        """Test parallel search across sources."""
        self.cluster_search()

    @task(2)
    def process_chunk(self):
        """Test data chunk processing."""
        self.cluster_process_chunk()

    @task(1)
    def batch_http(self):
        """Test batch HTTP simulation."""
        self.cluster_batch_http()


class WorkflowUser(AuthenticatedTaskUser):
    """
    User that exercises Ray-native workflow composition (0.3.0).

    Tests fan-out benchmarks and nested group/chain workflows.
    Requires worker running with: --cluster ray://head:10001
    """

    wait_time = between(2, 5)
    weight = 2

    @task(4)
    def fanout_workflow(self):
        """Submit a fan-out workflow and poll for its result."""
        result = self.workflow_fanout_benchmark(
            num_items=random.randint(4, 10),
            seconds_per_item=random.uniform(0.1, 0.3),
        )
        if result:
            self.workflow_fanout_result(result["task_id"])

    @task(3)
    def complex_nested_workflow(self):
        """Submit a nested group/chain workflow and poll progress."""
        result = self.complex_workflow(
            fast_items=random.randint(4, 8),
            slow_items=random.randint(2, 4),
        )
        if result:
            self.complex_workflow_result(result["task_id"])

    @task(1)
    def check_stats(self):
        self._get_stats()


class RuntimeEnvUser(AuthenticatedTaskUser):
    """
    User that exercises RuntimeEnv profiles (0.3.0).

    Tests named profile probes and repeated cold/cached benchmarks.
    Requires worker running with: --cluster ray://head:10001
    """

    wait_time = between(3, 8)
    weight = 1

    @task(4)
    def probe_profile(self):
        """Enqueue a probe task through a RuntimeEnv profile and fetch the result."""
        result = self.runtime_env_probe(profile=random.choice(["project", "thin"]))
        if result:
            self.runtime_env_result(result["task_id"])

    @task(2)
    def benchmark_cache(self):
        """Benchmark cold vs cached env startup and fetch timing summary."""
        result = self.runtime_env_benchmark(
            profile=random.choice(["thin", "numpy-2-2"]),
            repeats=random.randint(2, 3),
        )
        if result:
            self.runtime_env_result(result["task_id"])


class ObservabilityDemoUser(AuthenticatedTaskUser):
    """Run one lightweight, deterministic tour of the local task topology."""

    wait_time = between(2, 4)
    weight = 1
    _SCENARIOS = (
        "show_basic_add",
        "show_slow_task",
        "show_priority_task",
        "show_sync_task",
        "show_cluster_search",
        "show_workflow_full",
        "show_workflow_terminal_only",
        "show_workflow_disabled",
        "show_runtime_env",
        "show_ml_inference",
        "show_monitoring",
    )

    def on_start(self) -> None:
        super().on_start()
        self._scenario_index = 0

    def on_stop(self) -> None:
        """Fail an explicitly complete demo when no whole tour finished."""

        parsed_options = getattr(self.environment, "parsed_options", None)
        if not getattr(parsed_options, "require_complete_tour", False):
            return
        if getattr(self, "_scenario_index", 0) >= len(self._SCENARIOS):
            return
        self.environment.process_exit_code = 1
        _LOGGER.error(_INCOMPLETE_DEMO_TOUR_MESSAGE)

    def _submit_and_follow(
        self,
        result: dict[str, Any] | None,
        *,
        scenario_name: str,
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        if result is None:
            self.environment.process_exit_code = 1
            raise StopTest(f"{scenario_name} could not be enqueued")
        terminal_result = self._poll_task_to_terminal(
            result["task_id"],
            scenario_name=scenario_name,
            timeout_seconds=timeout_seconds,
        )
        if terminal_result is None:
            self.environment.process_exit_code = 1
            raise StopTest(
                f"{scenario_name} had an indeterminate result; stopping before the next task"
            )
        terminal_status = str(terminal_result.get("status", "")).upper()
        if terminal_status not in _TERMINAL_SUCCESS_STATES:
            self.environment.process_exit_code = 1
            raise StopTest(
                f"{scenario_name} reached unsuccessful terminal state "
                f"{terminal_status}; stopping before the next task"
            )
        return terminal_result

    @task
    def run_next_scenario(self) -> None:
        """Run each task family once before starting the sequence again."""
        scenario = self._SCENARIOS[self._scenario_index % len(self._SCENARIOS)]
        getattr(self, scenario)()
        self._scenario_index += 1

    def show_basic_add(self) -> None:
        self._submit_and_follow(
            self.create_add_task(a=21, b=21),
            scenario_name="basic add",
        )

    def show_slow_task(self) -> None:
        self._submit_and_follow(
            self.create_slow_task(seconds=1.5),
            scenario_name="slow task",
        )

    def show_priority_task(self) -> None:
        self._submit_and_follow(
            self.local_urgent(message="locust-observability-demo"),
            scenario_name="priority task",
        )

    def show_sync_task(self) -> None:
        self._submit_and_follow(
            self.sync_calculate(a=42, b=6, operation="divide"),
            scenario_name="sync task",
        )

    def show_cluster_search(self) -> None:
        self._submit_and_follow(
            self.cluster_search(
                pattern="demo",
                sources=["demo-source-a", "other-source", "demo-source-b"],
            ),
            scenario_name="cluster search",
        )

    def _show_workflow_policy(
        self,
        reporting_policy: _WorkflowReportingPolicy,
    ) -> None:
        terminal_result = self._submit_and_follow(
            self.complex_workflow(
                fast_items=2,
                slow_items=1,
                reporting_policy=reporting_policy,
            ),
            scenario_name=f"workflow {reporting_policy}",
        )
        summary = self.workflow_policy_summary(
            terminal_result["task_id"],
            expected_policy=reporting_policy,
        )
        if summary is None:
            self.environment.process_exit_code = 1
            raise StopTest(
                f"workflow {reporting_policy} summary was indeterminate; "
                "stopping before the next task"
            )

    def show_workflow_full(self) -> None:
        self._show_workflow_policy("full")

    def show_workflow_terminal_only(self) -> None:
        self._show_workflow_policy("terminal_only")

    def show_workflow_disabled(self) -> None:
        self._show_workflow_policy("disabled")

    def show_runtime_env(self) -> None:
        self._submit_and_follow(
            self.runtime_env_probe(profile="thin"),
            scenario_name="RuntimeEnv probe",
            timeout_seconds=120.0,
        )

    def show_ml_inference(self) -> None:
        samples = [{"features": [index / 10, (index + 1) / 10]} for index in range(12)]
        self._submit_and_follow(
            self.ml_inference(model_id="locust-demo-model", samples=samples),
            scenario_name="ML inference",
        )

    def show_monitoring(self) -> None:
        self._get_stats()
        self._get_metrics()


class WorkflowShowcaseUser(AuthenticatedTaskUser):
    """Run one bounded successful workflow showcase at a deliberately low rate."""

    # Locust filters abstract users during module loading, before applying the
    # positional class selector. Keeping this conditional makes the showcase
    # opt-in while preserving normal ``... WorkflowShowcaseUser`` selection.
    abstract = not _explicit_user_selected("WorkflowShowcaseUser")
    fixed_count = 1
    wait_time = between(8, 12)

    @task
    def run_workflow_showcase(self) -> None:
        """Submit, await, and verify one three-item success before repeating."""
        enqueued = self.workflow_showcase(item_count=3, work_seconds=0.05)
        if enqueued is None:
            self.environment.process_exit_code = 1
            raise StopTest("workflow showcase could not be enqueued")

        task_id = enqueued["task_id"]
        terminal = self._poll_task_to_terminal(
            task_id,
            timeout_seconds=120.0,
            scenario_name="workflow showcase",
        )
        terminal_status = (
            str(terminal.get("status", "")).upper() if isinstance(terminal, dict) else ""
        )
        if terminal_status not in _TERMINAL_SUCCESS_STATES:
            self.environment.process_exit_code = 1
            raise StopTest("workflow showcase did not reach a successful terminal state")

        detail = self.workflow_showcase_result(task_id)
        if not isinstance(detail, dict) or not _workflow_showcase_result_matches(
            detail,
            task_id=task_id,
        ):
            self.environment.process_exit_code = 1
            raise StopTest("workflow showcase result or bounded graph publication was incomplete")


class MLPipelineUser(AuthenticatedTaskUser):
    """
    User that tests ML pipeline tasks.

    Requires worker running with: --local --queue=ml
    """

    wait_time = between(2, 5)
    weight = 1

    @task(3)
    def train_model(self):
        self.ml_train(epochs=random.randint(3, 10))

    @task(5)
    def batch_inference(self):
        self.ml_inference()

    @task(1)
    def hyperparam_search(self):
        self.ml_hyperparam_search()


class StressTestUser(AuthenticatedTaskUser):
    """
    Aggressive stress test user for finding system limits.

    Use with caution - can overwhelm the system!

    Usage:
        locust -f locustfile.py --host=http://localhost:8000 -u 50 -r 10 -t 60s StressTestUser
    """

    wait_time = between(0.5, 2.0)
    weight = 1  # Guarded by _reject_accidental_broad_capacity_mix

    @task(3)
    def submit_stress_cpu(self):
        self.stress_cpu(duration=random.uniform(2.0, 5.0))

    @task(2)
    def submit_stress_memory(self):
        self.stress_memory(size_mb=random.randint(100, 300))

    @task(2)
    def submit_stress_compute(self):
        self.stress_compute(depth=random.randint(8, 12), width=random.randint(80, 120))

    @task(2)
    def submit_stress_primes(self):
        self.stress_primes(start=random.randint(500000, 2000000), count=random.randint(50, 150))

    @task(1)
    def submit_stress_json(self):
        self.stress_json(size_kb=random.randint(100, 500), depth=random.randint(4, 8))


class MonitoringUser(AuthenticatedTaskUser):
    """
    User that primarily monitors the system.

    Simulates dashboard/monitoring traffic during load testing.
    """

    wait_time = between(2, 5)
    weight = 1

    @task(5)
    def check_stats(self):
        stats = self._get_stats()
        if stats:
            queued = stats.get("queued", 0)
            running = stats.get("running", 0)
            if queued > 100:
                print(f"WARNING: high queue depth: {queued} queued, {running} running")

    @task(3)
    def health_check(self):
        self._check_health()

    @task(2)
    def fetch_metrics(self):
        self._get_metrics()

    @task(2)
    def list_tasks(self):
        self._get("/api/executions?limit=20", "/api/executions")


class BurstTaskUser(AuthenticatedTaskUser):
    """
    User that submits bursts of tasks at once.

    Tests how well the system handles sudden spikes.
    """

    wait_time = between(5, 10)
    weight = 1

    @task
    def submit_burst(self):
        """Submit a burst of 10-30 tasks rapidly."""
        burst_size = random.randint(10, 30)

        for _ in range(burst_size):
            task_type = random.choice(["add", "multiply", "fibonacci", "slow"])
            if task_type == "add":
                self.create_add_task()
            elif task_type == "multiply":
                self.create_multiply_task()
            elif task_type == "fibonacci":
                self.local_fibonacci(n=random.randint(10, 20))
            else:
                self.create_slow_task(seconds=random.uniform(0.1, 0.5))

            time.sleep(0.01)  # Tiny delay within burst


# ============================================================================
# Specialized Scenarios
# ============================================================================


class DistributedComputingUser(AuthenticatedTaskUser):
    """
    Focused testing of distributed computing capabilities.

    Specifically tests the cluster's ability to parallelize work.
    """

    wait_time = between(3, 8)
    weight = 1  # Guarded by _reject_accidental_broad_capacity_mix

    @task(5)
    def heavy_cpu_benchmark(self):
        """Heavy CPU benchmark to test scaling."""
        self.cluster_cpu_benchmark(num_items=16, seconds_per_item=2.0)

    @task(3)
    def wide_search(self):
        """Search across many sources."""
        sources = [f"source_{i}" for i in range(10)]
        self.cluster_search(pattern="test", sources=sources)


class SustainedLoadUser(AuthenticatedTaskUser):
    """
    User for sustained load testing over longer periods.

    Simulates steady-state production load.

    Usage:
        locust -f locustfile.py --host=http://localhost:8000 -u 20 -r 2 -t 600s SustainedLoadUser
    """

    wait_time = between(1, 3)
    weight = 1  # Guarded by _reject_accidental_broad_capacity_mix

    @task(10)
    def normal_task(self):
        self.create_add_task()

    @task(5)
    def local_task(self):
        self.local_fibonacci(n=random.randint(15, 25))

    @task(3)
    def slow_task(self):
        self.create_slow_task(seconds=random.uniform(1.0, 2.0))

    @task(1)
    def monitor(self):
        self._get_stats()


# ============================================================================
# Custom Events and Reporting
# ============================================================================

# Uncomment to enable detailed request logging:
# from locust import events
#
# @events.request.add_listener
# def on_request(request_type, name, response_time, response_length, **kwargs):
#     if "enqueue" in name or "cluster" in name:
#         print(f"[{request_type}] {name}: {response_time:.0f}ms")
