"""Integration tests for the Django Ninja API."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from django_ray.models import RayTaskExecution, TaskState


@pytest.fixture
def client():
    """Django test client."""
    return Client()


@pytest.mark.django_db
class TestTasksAPI:
    """Test the /api/tasks endpoints."""

    def test_list_tasks_empty(self, client):
        """Test listing tasks when none exist."""
        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["tasks"] == []

    def test_list_tasks_with_data(self, client):
        """Test listing tasks with existing tasks."""
        # Create some tasks
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

        response = client.get("/api/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["queued"] == 1
        assert data["succeeded"] == 1
        assert len(data["tasks"]) == 2

    def test_list_tasks_filter_by_state(self, client):
        """Test filtering tasks by state."""
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

        response = client.get("/api/tasks?state=QUEUED")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["state"] == "QUEUED"

    def test_get_task(self, client):
        """Test getting a specific task."""
        task = RayTaskExecution.objects.create(
            task_id="test-get",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
        )

        response = client.get(f"/api/tasks/{task.pk}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == task.pk
        assert data["callable_path"] == "testproject.tasks.add_numbers"

    def test_get_task_not_found(self, client):
        """Test getting a non-existent task."""
        response = client.get("/api/tasks/99999")
        assert response.status_code == 404

    def test_create_task(self, client):
        """Test creating a new task."""
        response = client.post(
            "/api/tasks",
            data=json.dumps(
                {
                    "callable_path": "testproject.tasks.add_numbers",
                    "args": [1, 2],
                    "kwargs": {},
                    "queue": "default",
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200
        data = response.json()
        assert data["callable_path"] == "testproject.tasks.add_numbers"
        assert data["state"] == "QUEUED"

        # Verify in database
        task = RayTaskExecution.objects.get(pk=data["id"])
        assert task.args_json == "[1, 2]"

    def test_delete_task(self, client):
        """Test deleting a task."""
        task = RayTaskExecution.objects.create(
            task_id="test-delete",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        response = client.delete(f"/api/tasks/{task.pk}")
        assert response.status_code == 200

        # Verify deleted
        assert not RayTaskExecution.objects.filter(pk=task.pk).exists()

    def test_cancel_queued_task(self, client):
        """Test cancelling a queued task."""
        task = RayTaskExecution.objects.create(
            task_id="test-cancel",
            callable_path="test.task",
            state=TaskState.QUEUED,
        )

        response = client.post(f"/api/tasks/{task.pk}/cancel")
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "CANCELLED"

    def test_retry_failed_task(self, client):
        """Test retrying a failed task."""
        task = RayTaskExecution.objects.create(
            task_id="test-retry",
            callable_path="test.task",
            state=TaskState.FAILED,
            error_message="Some error",
            attempt_number=1,
        )

        response = client.post(f"/api/tasks/{task.pk}/retry")
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "QUEUED"
        assert data["attempt_number"] == 2

    def test_reset_tasks(self, client):
        """Test resetting stuck tasks."""
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

        response = client.post("/api/tasks/reset")
        assert response.status_code == 200
        data = response.json()
        assert "2" in data["message"]

        # Verify all reset to QUEUED
        assert RayTaskExecution.objects.filter(state=TaskState.QUEUED).count() == 2

    def test_get_stats(self, client):
        """Test getting task statistics."""
        RayTaskExecution.objects.create(
            task_id="test-1", callable_path="test", state=TaskState.QUEUED
        )
        RayTaskExecution.objects.create(
            task_id="test-2", callable_path="test", state=TaskState.SUCCEEDED
        )
        RayTaskExecution.objects.create(
            task_id="test-3", callable_path="test", state=TaskState.FAILED
        )

        response = client.get("/api/tasks/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["queued"] == 1
        assert data["succeeded"] == 1
        assert data["failed"] == 1


@pytest.mark.django_db
class TestQuickTasksAPI:
    """Test the quick task endpoints."""

    def test_enqueue_add(self, client):
        """Test enqueueing an add_numbers task."""
        response = client.post("/api/enqueue/add/10/20")
        assert response.status_code == 200
        data = response.json()
        assert data["callable_path"] == "testproject.tasks.add_numbers"
        assert data["state"] == "QUEUED"

        # Verify args
        task = RayTaskExecution.objects.get(pk=data["id"])
        assert task.args_json == "[10, 20]"

    def test_enqueue_slow(self, client):
        """Test enqueueing a slow_task."""
        response = client.post("/api/enqueue/slow/5")
        assert response.status_code == 200
        data = response.json()
        assert data["callable_path"] == "testproject.tasks.slow_task"

        # Verify kwargs
        task = RayTaskExecution.objects.get(pk=data["id"])
        assert '"seconds": 5' in task.kwargs_json or '"seconds":5' in task.kwargs_json

    def test_enqueue_fail(self, client):
        """Test enqueueing a failing_task."""
        response = client.post("/api/enqueue/fail")
        assert response.status_code == 200
        data = response.json()
        assert data["callable_path"] == "testproject.tasks.failing_task"
        assert data["state"] == "QUEUED"
