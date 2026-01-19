"""Integration tests for the Django Ninja API."""

from __future__ import annotations


import pytest
from django.test import Client

from django_ray.models import RayTaskExecution, TaskState


@pytest.fixture
def client():
    """Django test client."""
    return Client()


@pytest.mark.django_db
class TestHealthAPI:
    """Test the /api/health endpoint."""

    def test_health_check(self, client):
        """Test the health check endpoint."""
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"
        assert data["version"] == "0.2.0"

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
        assert response["Content-Type"] == "text/plain; charset=utf-8"

        content = response.content.decode("utf-8")
        # Check for expected metric names
        assert "django_ray_tasks_total" in content
        assert "django_ray_tasks_queued" in content
        assert "django_ray_tasks_running" in content
        assert "django_ray_queue_depth" in content
        # Check for state labels
        assert 'state="QUEUED"' in content
        assert 'state="RUNNING"' in content
        # Check for queue labels
        assert 'queue="default"' in content
        assert 'queue="high-priority"' in content


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
        RayTaskExecution.objects.create(
            task_id="test-running",
            callable_path="test.task",
            state=TaskState.RUNNING,
        )
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
