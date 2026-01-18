"""
Locust load testing for django-ray task execution API.

This file provides load testing scenarios to evaluate how Ray handles
concurrent task submissions and execution.

Usage:
    # Install locust
    pip install locust

    # Run with web UI (default)
    locust -f locustfile.py --host=http://localhost:8000

    # Run headless
    locust -f locustfile.py --host=http://localhost:8000 --headless -u 100 -r 10 -t 60s

    # Run with specific user count and spawn rate
    locust -f locustfile.py --host=http://localhost:8000 --headless -u 50 -r 5 -t 120s

Scenarios:
    - FastTaskUser: Submits quick add_numbers tasks (instant execution)
    - SlowTaskUser: Submits slow_task that take 1-3 seconds
    - MixedTaskUser: Mixed workload of fast and slow tasks
    - BurstTaskUser: Submits bursts of many tasks at once
    - MonitoringUser: Monitors task statistics and health

Metrics to watch:
    - Response time for task creation (should be fast, just DB insert)
    - Task throughput (tasks created per second)
    - Ray Dashboard for task execution backlog
    - Task completion rate (check /api/executions/stats)
"""

import random
import time

from locust import HttpUser, TaskSet, between, task


class TaskCreationMixin:
    """Mixin providing common task creation methods."""

    def create_add_task(self, a: int = None, b: int = None):
        """Create an add_numbers task."""
        a = a or random.randint(1, 1000)
        b = b or random.randint(1, 1000)
        with self.client.post(
            f"/api/enqueue/add/{a}/{b}",
            name="/api/enqueue/add/[a]/[b]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                return response.json()
            else:
                response.failure(f"Failed to create task: {response.status_code}")
                return None

    def create_slow_task(self, seconds: float = None):
        """Create a slow_task that sleeps for a duration."""
        seconds = seconds or random.uniform(0.5, 2.0)
        with self.client.post(
            f"/api/enqueue/slow/{seconds:.1f}",
            name="/api/enqueue/slow/[seconds]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                return response.json()
            else:
                response.failure(f"Failed to create task: {response.status_code}")
                return None

    def create_multiply_task(self, a: int = None, b: int = None):
        """Create a multiply_numbers task."""
        a = a or random.randint(1, 100)
        b = b or random.randint(1, 100)
        with self.client.post(
            f"/api/enqueue/multiply/{a}/{b}",
            name="/api/enqueue/multiply/[a]/[b]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                return response.json()
            else:
                response.failure(f"Failed to create task: {response.status_code}")
                return None

    def create_cpu_intensive_task(self, n: int = None):
        """Create a CPU-intensive task."""
        n = n or random.randint(100000, 500000)
        with self.client.post(
            f"/api/enqueue/cpu/{n}",
            name="/api/enqueue/cpu/[n]",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                return response.json()
            else:
                response.failure(f"Failed to create task: {response.status_code}")
                return None

    def get_task_stats(self):
        """Get task statistics."""
        with self.client.get(
            "/api/executions/stats",
            name="/api/executions/stats",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
                return response.json()
            else:
                response.failure(f"Failed to get stats: {response.status_code}")
                return None

    def _check_health(self):
        """Check API health."""
        with self.client.get(
            "/api/health",
            name="/api/health",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "healthy":
                    response.success()
                else:
                    response.failure(f"Unhealthy: {data}")
            else:
                response.failure(f"Health check failed: {response.status_code}")


# ============================================================================
# User Classes
# ============================================================================


class FastTaskUser(HttpUser, TaskCreationMixin):
    """
    User that rapidly submits fast-executing tasks.

    This simulates high-throughput task submission where tasks complete quickly.
    Good for testing task creation overhead and queue throughput.
    """

    wait_time = between(0.1, 0.5)  # Very short wait between tasks
    weight = 3  # Higher weight = more users of this type

    @task(10)
    def submit_add_task(self):
        """Submit an add_numbers task (very fast execution)."""
        self.create_add_task()

    @task(5)
    def submit_multiply_task(self):
        """Submit a multiply_numbers task."""
        self.create_multiply_task()

    @task(1)
    def check_stats(self):
        """Occasionally check task stats."""
        self.get_task_stats()


class SlowTaskUser(HttpUser, TaskCreationMixin):
    """
    User that submits slower-running tasks.

    This simulates workloads with longer-running tasks that accumulate
    in the queue, testing Ray's ability to handle backpressure.
    """

    wait_time = between(1, 3)  # Slower submission rate
    weight = 1

    @task(5)
    def submit_slow_task(self):
        """Submit a slow task (1-3 seconds)."""
        self.create_slow_task(seconds=random.uniform(1.0, 3.0))

    @task(2)
    def submit_cpu_task(self):
        """Submit a CPU-intensive task."""
        self.create_cpu_intensive_task(n=random.randint(500000, 1000000))

    @task(1)
    def check_stats(self):
        """Check task stats."""
        self.get_task_stats()


class MixedTaskUser(HttpUser, TaskCreationMixin):
    """
    User with a mixed workload of fast and slow tasks.

    This simulates realistic production workloads with varying task types.
    """

    wait_time = between(0.5, 2)
    weight = 2

    @task(5)
    def submit_fast_task(self):
        """Submit a fast add task."""
        self.create_add_task()

    @task(3)
    def submit_medium_task(self):
        """Submit a task with moderate execution time."""
        self.create_slow_task(seconds=random.uniform(0.5, 1.5))

    @task(1)
    def submit_slow_task(self):
        """Submit a slower task."""
        self.create_slow_task(seconds=random.uniform(2.0, 5.0))

    @task(1)
    def check_health(self):
        """Check API health."""
        self._check_health()


class BurstTaskUser(HttpUser, TaskCreationMixin):
    """
    User that submits bursts of many tasks at once.

    This tests how well the system handles sudden spikes in task submission.
    """

    wait_time = between(5, 10)  # Long wait between bursts
    weight = 1

    @task
    def submit_task_burst(self):
        """Submit a burst of 10-50 tasks rapidly."""
        burst_size = random.randint(10, 50)

        for _ in range(burst_size):
            task_type = random.choice(["add", "multiply", "slow"])
            if task_type == "add":
                self.create_add_task()
            elif task_type == "multiply":
                self.create_multiply_task()
            else:
                self.create_slow_task(seconds=random.uniform(0.1, 0.5))

            # Tiny delay within burst to avoid overwhelming
            time.sleep(0.01)


class MonitoringUser(HttpUser, TaskCreationMixin):
    """
    User that primarily monitors the system.

    This simulates dashboard/monitoring traffic during load testing.
    """

    wait_time = between(2, 5)
    weight = 1

    @task(5)
    def check_stats(self):
        """Check task statistics."""
        stats = self.get_task_stats()
        if stats:
            # Log queue depth for visibility
            queued = stats.get("queued", 0)
            running = stats.get("running", 0)
            if queued > 100:
                print(f"⚠️  High queue depth: {queued} queued, {running} running")

    @task(3)
    def check_health(self):
        """Check API health."""
        self._check_health()

    @task(2)
    def list_recent_tasks(self):
        """List recent tasks."""
        with self.client.get(
            "/api/executions?limit=20",
            name="/api/executions",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Failed to list tasks: {response.status_code}")


# ============================================================================
# Specialized Test Scenarios
# ============================================================================


class StressTestUser(HttpUser, TaskCreationMixin):
    """
    Aggressive stress test user for finding system limits.

    Use with caution - this can overwhelm the system!

    Usage:
        locust -f locustfile.py --host=http://localhost:8000 -u 200 -r 50 -t 60s
    """

    wait_time = between(0.01, 0.1)  # Extremely aggressive
    weight = 0  # Set to 0 by default, enable explicitly if needed

    @task
    def rapid_fire_tasks(self):
        """Submit tasks as fast as possible."""
        self.create_add_task()


class SustainedLoadUser(HttpUser, TaskCreationMixin):
    """
    User for sustained load testing over longer periods.

    This simulates steady-state production load.

    Usage:
        locust -f locustfile.py --host=http://localhost:8000 -u 20 -r 2 -t 600s
    """

    wait_time = between(1, 3)
    weight = 0  # Set to 0 by default, enable explicitly

    @task(10)
    def normal_task(self):
        """Submit a normal task."""
        self.create_add_task()

    @task(3)
    def slow_task(self):
        """Submit a slow task."""
        self.create_slow_task(seconds=random.uniform(1.0, 2.0))

    @task(1)
    def monitor(self):
        """Monitor the system."""
        self.get_task_stats()


# ============================================================================
# Custom Events and Reporting
# ============================================================================

# You can add custom event listeners for more detailed reporting
# from locust import events
#
# @events.request.add_listener
# def on_request(request_type, name, response_time, response_length, **kwargs):
#     if "enqueue" in name:
#         print(f"Task created in {response_time}ms")

