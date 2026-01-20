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
    # Install locust
    pip install locust

    # Run with web UI (default)
    locust -f locustfile.py --host=http://localhost:8000

    # Run headless
    locust -f locustfile.py --host=http://localhost:8000 --headless -u 100 -r 10 -t 60s

    # Run with specific user count and spawn rate
    locust -f locustfile.py --host=http://localhost:8000 --headless -u 50 -r 5 -t 120s

    # Run specific test class only
    locust -f locustfile.py --host=http://localhost:8000 -u 10 -r 2 ClusterTaskUser

Scenarios:
    - BasicTaskUser: Submits quick add_numbers/multiply tasks
    - SyncTaskUser: Uses sync mode tasks (simple calculations)
    - LocalRayUser: Uses local Ray mode (fibonacci, workload)
    - ClusterTaskUser: Tests distributed computing features
    - MLPipelineUser: Tests ML pipeline tasks
    - StressTestUser: Pushes system to limits
    - MonitoringUser: Monitors task statistics and health

Metrics to watch:
    - Response time for task creation (should be fast, just DB insert)
    - Task throughput (tasks created per second)
    - Ray Dashboard for task execution backlog
    - Task completion rate (check /api/executions/stats)
"""

import random
import time
from typing import Any

from locust import HttpUser, between, task


class TaskCreationMixin:
    """Mixin providing common task creation and monitoring methods."""

    def _post_task(
        self, endpoint: str, name: str | None = None, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Generic task creation helper."""
        name = name or endpoint
        kwargs = {"name": name, "catch_response": True}
        if payload:
            kwargs["json"] = payload

        with self.client.post(endpoint, **kwargs) as response:
            if response.status_code == 200:
                response.success()
                return response.json()
            else:
                response.failure(f"Failed to create task: {response.status_code}")
                return None

    def _get(self, endpoint: str, name: str | None = None) -> dict[str, Any] | None:
        """Generic GET helper."""
        name = name or endpoint
        with self.client.get(endpoint, name=name, catch_response=True) as response:
            if response.status_code == 200:
                response.success()
                return response.json()
            else:
                response.failure(f"GET failed: {response.status_code}")
                return None

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
        with self.client.get("/api/metrics", name="/api/metrics", catch_response=True) as response:
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


class BasicTaskUser(HttpUser, TaskCreationMixin):
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


class SyncTaskUser(HttpUser, TaskCreationMixin):
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


class LocalRayUser(HttpUser, TaskCreationMixin):
    """
    User that tests local Ray mode tasks.

    Requires worker running with: --local
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


class ClusterTaskUser(HttpUser, TaskCreationMixin):
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


class MLPipelineUser(HttpUser, TaskCreationMixin):
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


class StressTestUser(HttpUser, TaskCreationMixin):
    """
    Aggressive stress test user for finding system limits.

    Use with caution - can overwhelm the system!

    Usage:
        locust -f locustfile.py --host=http://localhost:8000 -u 50 -r 10 -t 60s StressTestUser
    """

    wait_time = between(0.5, 2.0)
    weight = 0  # Disabled by default

    @task(3)
    def stress_cpu(self):
        self.stress_cpu(duration=random.uniform(2.0, 5.0))

    @task(2)
    def stress_memory(self):
        self.stress_memory(size_mb=random.randint(100, 300))

    @task(2)
    def stress_compute(self):
        self.stress_compute(depth=random.randint(8, 12), width=random.randint(80, 120))

    @task(2)
    def stress_primes(self):
        self.stress_primes(start=random.randint(500000, 2000000), count=random.randint(50, 150))

    @task(1)
    def stress_json(self):
        self.stress_json(size_kb=random.randint(100, 500), depth=random.randint(4, 8))


class MonitoringUser(HttpUser, TaskCreationMixin):
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
                print(f"⚠️  High queue depth: {queued} queued, {running} running")

    @task(3)
    def health_check(self):
        self._check_health()

    @task(2)
    def fetch_metrics(self):
        self._get_metrics()

    @task(2)
    def list_tasks(self):
        self._get("/api/executions?limit=20", "/api/executions")


class BurstTaskUser(HttpUser, TaskCreationMixin):
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


class DistributedComputingUser(HttpUser, TaskCreationMixin):
    """
    Focused testing of distributed computing capabilities.

    Specifically tests the cluster's ability to parallelize work.
    """

    wait_time = between(3, 8)
    weight = 0  # Enable explicitly

    @task(5)
    def heavy_cpu_benchmark(self):
        """Heavy CPU benchmark to test scaling."""
        self.cluster_cpu_benchmark(num_items=16, seconds_per_item=2.0)

    @task(3)
    def wide_search(self):
        """Search across many sources."""
        sources = [f"source_{i}" for i in range(10)]
        self.cluster_search(pattern="test", sources=sources)


class SustainedLoadUser(HttpUser, TaskCreationMixin):
    """
    User for sustained load testing over longer periods.

    Simulates steady-state production load.

    Usage:
        locust -f locustfile.py --host=http://localhost:8000 -u 20 -r 2 -t 600s SustainedLoadUser
    """

    wait_time = between(1, 3)
    weight = 0  # Enable explicitly

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
