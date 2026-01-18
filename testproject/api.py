"""Django Ninja API for django-ray task management."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.serialization import serialize_args

api = NinjaAPI(
    title="Django Ray API",
    version="0.1.0",
    description="API for managing and monitoring Ray tasks",
)


# ============================================================================
# Schemas
# ============================================================================


class TaskCreateSchema(Schema):
    """Schema for creating a new task."""

    callable_path: str
    args: list = []
    kwargs: dict = {}
    queue: str = "default"


class TaskResponseSchema(Schema):
    """Schema for task response."""

    id: int
    task_id: str
    callable_path: str
    queue_name: str
    state: str
    attempt_number: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result_data: str | None
    error_message: str | None


class TaskListResponseSchema(Schema):
    """Schema for task list response."""

    tasks: list[TaskResponseSchema]
    total: int
    queued: int
    running: int
    succeeded: int
    failed: int


class TaskEnqueueSchema(Schema):
    """Schema for enqueueing a predefined task."""

    task_name: str
    args: list = []
    kwargs: dict = {}
    queue: str = "default"


class MessageSchema(Schema):
    """Simple message response."""

    message: str


class HealthSchema(Schema):
    """Health check response schema."""

    status: str
    database: str
    version: str


class StatsSchema(Schema):
    """Task statistics schema."""

    total: int
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int
    lost: int


# ============================================================================
# Health Endpoints
# ============================================================================


@api.get("/health", response=HealthSchema, tags=["Health"])
def health_check(request):
    """Health check endpoint for Kubernetes probes."""
    from django.db import connection

    # Check database connectivity
    db_status = "ok"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        db_status = "error"

    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "database": db_status,
        "version": "0.1.0",
    }


# ============================================================================
# Task Endpoints
# ============================================================================


@api.get("/tasks", response=TaskListResponseSchema, tags=["Tasks"])
def list_tasks(
    request,
    state: str | None = None,
    queue: str | None = None,
    limit: int = 50,
):
    """List all tasks with optional filtering."""
    queryset = RayTaskExecution.objects.all()

    if state:
        queryset = queryset.filter(state=state.upper())
    if queue:
        queryset = queryset.filter(queue_name=queue)

    queryset = queryset.order_by("-created_at")[:limit]

    # Get counts
    all_tasks = RayTaskExecution.objects.all()

    return {
        "tasks": list(queryset),
        "total": all_tasks.count(),
        "queued": all_tasks.filter(state=TaskState.QUEUED).count(),
        "running": all_tasks.filter(state=TaskState.RUNNING).count(),
        "succeeded": all_tasks.filter(state=TaskState.SUCCEEDED).count(),
        "failed": all_tasks.filter(state=TaskState.FAILED).count(),
    }


@api.get("/tasks/stats", response=StatsSchema, tags=["Tasks"])
def get_stats(request):
    """Get task statistics."""
    all_tasks = RayTaskExecution.objects.all()

    return {
        "total": all_tasks.count(),
        "queued": all_tasks.filter(state=TaskState.QUEUED).count(),
        "running": all_tasks.filter(state=TaskState.RUNNING).count(),
        "succeeded": all_tasks.filter(state=TaskState.SUCCEEDED).count(),
        "failed": all_tasks.filter(state=TaskState.FAILED).count(),
        "cancelled": all_tasks.filter(state=TaskState.CANCELLED).count(),
        "lost": all_tasks.filter(state=TaskState.LOST).count(),
    }


@api.post("/tasks/reset", response=MessageSchema, tags=["Tasks"])
def reset_tasks(
    request,
    state: Literal["RUNNING", "FAILED", "LOST"] | None = None,
):
    """Reset tasks to QUEUED state."""
    if state:
        queryset = RayTaskExecution.objects.filter(state=state.upper())
    else:
        queryset = RayTaskExecution.objects.exclude(
            state__in=[TaskState.SUCCEEDED, TaskState.QUEUED]
        )

    count = queryset.count()
    queryset.update(
        state=TaskState.QUEUED,
        started_at=None,
        finished_at=None,
        claimed_by_worker=None,
        ray_job_id=None,
        error_message=None,
        error_traceback=None,
    )

    return {"message": f"Reset {count} task(s) to QUEUED state"}


@api.get("/tasks/{task_id}", response=TaskResponseSchema, tags=["Tasks"])
def get_task(request, task_id: int):
    """Get a specific task by ID."""
    task = get_object_or_404(RayTaskExecution, pk=task_id)
    return task


@api.post("/tasks", response=TaskResponseSchema, tags=["Tasks"])
def create_task(request, payload: TaskCreateSchema):
    """Create and enqueue a new task."""
    task = RayTaskExecution.objects.create(
        task_id=f"api-{RayTaskExecution.objects.count() + 1}",
        callable_path=payload.callable_path,
        queue_name=payload.queue,
        state=TaskState.QUEUED,
        args_json=serialize_args(payload.args),
        kwargs_json=serialize_args(payload.kwargs),
    )
    return task


@api.delete("/tasks/{task_id}", response=MessageSchema, tags=["Tasks"])
def delete_task(request, task_id: int):
    """Delete a task."""
    task = get_object_or_404(RayTaskExecution, pk=task_id)
    task.delete()
    return {"message": f"Task {task_id} deleted"}


@api.post("/tasks/{task_id}/cancel", response=TaskResponseSchema, tags=["Tasks"])
def cancel_task(request, task_id: int):
    """Cancel a queued or running task."""
    task = get_object_or_404(RayTaskExecution, pk=task_id)

    if task.state == TaskState.QUEUED:
        task.state = TaskState.CANCELLED
        task.save(update_fields=["state"])
    elif task.state == TaskState.RUNNING:
        task.state = TaskState.CANCELLING
        task.save(update_fields=["state"])

    return task


@api.post("/tasks/{task_id}/retry", response=TaskResponseSchema, tags=["Tasks"])
def retry_task(request, task_id: int):
    """Retry a failed task."""
    task = get_object_or_404(RayTaskExecution, pk=task_id)

    if task.state in [TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST]:
        task.state = TaskState.QUEUED
        task.attempt_number += 1
        task.started_at = None
        task.finished_at = None
        task.error_message = None
        task.error_traceback = None
        task.ray_job_id = None
        task.claimed_by_worker = None
        task.save()

    return task


# ============================================================================
# Quick Task Endpoints (shortcuts for testproject tasks)
# ============================================================================


@api.post("/enqueue/add/{a}/{b}", response=TaskResponseSchema, tags=["Quick Tasks"])
def enqueue_add(request, a: int, b: int, queue: str = "default"):
    """Quick endpoint to enqueue an add_numbers task."""
    task = RayTaskExecution.objects.create(
        task_id=f"api-add-{RayTaskExecution.objects.count() + 1}",
        callable_path="testproject.tasks.add_numbers",
        queue_name=queue,
        state=TaskState.QUEUED,
        args_json=serialize_args([a, b]),
        kwargs_json="{}",
    )
    return task


@api.post("/enqueue/slow/{seconds}", response=TaskResponseSchema, tags=["Quick Tasks"])
def enqueue_slow(request, seconds: float, queue: str = "default"):
    """Quick endpoint to enqueue a slow_task."""
    task = RayTaskExecution.objects.create(
        task_id=f"api-slow-{RayTaskExecution.objects.count() + 1}",
        callable_path="testproject.tasks.slow_task",
        queue_name=queue,
        state=TaskState.QUEUED,
        args_json="[]",
        kwargs_json=serialize_args({"seconds": seconds}),
    )
    return task


@api.post("/enqueue/fail", response=TaskResponseSchema, tags=["Quick Tasks"])
def enqueue_fail(request, queue: str = "default"):
    """Quick endpoint to enqueue a failing_task."""
    task = RayTaskExecution.objects.create(
        task_id=f"api-fail-{RayTaskExecution.objects.count() + 1}",
        callable_path="testproject.tasks.failing_task",
        queue_name=queue,
        state=TaskState.QUEUED,
        args_json="[]",
        kwargs_json="{}",
    )
    return task


@api.post("/enqueue/{task_name}", response=TaskResponseSchema, tags=["Quick Tasks"])
def enqueue_task(request, task_name: str, payload: TaskEnqueueSchema | None = None):
    """Enqueue a testproject task by name.

    Available tasks:
    - add_numbers: args=[a, b] -> returns a + b
    - multiply_numbers: args=[a, b] -> returns a * b
    - slow_task: kwargs={seconds: N} -> sleeps for N seconds
    - failing_task: always fails
    - echo_task: returns {args, kwargs}
    - cpu_intensive_task: kwargs={n: N} -> CPU intensive loop
    """
    if payload is None:
        payload = TaskEnqueueSchema(task_name=task_name)

    callable_path = f"testproject.tasks.{task_name}"

    task = RayTaskExecution.objects.create(
        task_id=f"api-{task_name}-{RayTaskExecution.objects.count() + 1}",
        callable_path=callable_path,
        queue_name=payload.queue,
        state=TaskState.QUEUED,
        args_json=serialize_args(payload.args),
        kwargs_json=serialize_args(payload.kwargs),
    )

    return task

