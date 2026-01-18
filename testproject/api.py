"""Django Ninja API for django-ray task management.

This API demonstrates Django 6's native task framework integration with Ray.
Tasks are defined using @task decorator and enqueued using .enqueue().
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema

from django_ray.models import RayTaskExecution, TaskState

# Import tasks that use Django 6's @task decorator
from testproject import tasks

api = NinjaAPI(
    title="Django Ray API",
    version="0.1.0",
    description="API for managing and monitoring Ray tasks using Django 6's native task framework",
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


class Django6TaskResponseSchema(Schema):
    """Schema for Django 6 task result response."""

    task_id: str
    status: str
    enqueued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    args: list
    kwargs: dict


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
# Task Endpoints (Legacy - direct model access)
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


@api.post("/tasks", response=TaskResponseSchema, tags=["Tasks"], deprecated=True)
def create_task(request, payload: TaskCreateSchema):
    """[DEPRECATED] Create a task using legacy direct model creation.

    Use Django 6's native @task decorator and .enqueue() API instead.
    """
    from django_ray.runtime.serialization import serialize_args

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
# Django 6 Task Endpoints (using @task decorator and .enqueue())
# ============================================================================


@api.post("/v2/enqueue/add/{a}/{b}", response=Django6TaskResponseSchema, tags=["Django 6 Tasks"])
def enqueue_add_v2(request, a: int, b: int, queue: str = "default"):
    """Enqueue add_numbers task using Django 6's native .enqueue() API.

    This is the recommended way to enqueue tasks in Django 6.
    """
    # Use Django 6's native task API with optional queue override
    task_obj = tasks.add_numbers.using(queue_name=queue)
    result = task_obj.enqueue(a, b)

    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/v2/enqueue/multiply/{a}/{b}", response=Django6TaskResponseSchema, tags=["Django 6 Tasks"])
def enqueue_multiply_v2(request, a: int, b: int, queue: str = "default"):
    """Enqueue multiply_numbers task using Django 6's native .enqueue() API."""
    task_obj = tasks.multiply_numbers.using(queue_name=queue)
    result = task_obj.enqueue(a, b)

    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/v2/enqueue/slow/{seconds}", response=Django6TaskResponseSchema, tags=["Django 6 Tasks"])
def enqueue_slow_v2(request, seconds: float, queue: str = "default"):
    """Enqueue slow_task using Django 6's native .enqueue() API."""
    task_obj = tasks.slow_task.using(queue_name=queue)
    result = task_obj.enqueue(seconds=seconds)

    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/v2/enqueue/fail", response=Django6TaskResponseSchema, tags=["Django 6 Tasks"])
def enqueue_fail_v2(request, queue: str = "default"):
    """Enqueue failing_task using Django 6's native .enqueue() API."""
    task_obj = tasks.failing_task.using(queue_name=queue)
    result = task_obj.enqueue()

    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/v2/enqueue/cpu/{n}", response=Django6TaskResponseSchema, tags=["Django 6 Tasks"])
def enqueue_cpu_v2(request, n: int, queue: str = "default"):
    """Enqueue cpu_intensive_task using Django 6's native .enqueue() API."""
    task_obj = tasks.cpu_intensive_task.using(queue_name=queue)
    result = task_obj.enqueue(n=n)

    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.get("/v2/tasks/{task_id}", response=Django6TaskResponseSchema, tags=["Django 6 Tasks"])
def get_task_v2(request, task_id: str):
    """Get task status using Django 6's native get_result() API.

    This retrieves a task result by its UUID and returns the current status.
    """
    from django.tasks import task_backends

    # Access the default backend
    backend = task_backends["default"]
    result = backend.get_result(task_id)

    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


# ============================================================================
# Legacy Quick Task Endpoints (for backward compatibility)
# ============================================================================


@api.post("/enqueue/add/{a}/{b}", response=TaskResponseSchema, tags=["Legacy Tasks"], deprecated=True)
def enqueue_add(request, a: int, b: int, queue: str = "default"):
    """[DEPRECATED] Use /v2/enqueue/add/{a}/{b} instead.

    Quick endpoint to enqueue an add_numbers task using legacy direct model creation.
    """
    from django_ray.runtime.serialization import serialize_args

    task = RayTaskExecution.objects.create(
        task_id=f"api-add-{RayTaskExecution.objects.count() + 1}",
        callable_path="testproject.tasks.add_numbers",
        queue_name=queue,
        state=TaskState.QUEUED,
        args_json=serialize_args([a, b]),
        kwargs_json="{}",
    )
    return task


@api.post("/enqueue/slow/{seconds}", response=TaskResponseSchema, tags=["Legacy Tasks"], deprecated=True)
def enqueue_slow(request, seconds: float, queue: str = "default"):
    """[DEPRECATED] Use /v2/enqueue/slow/{seconds} instead.

    Quick endpoint to enqueue a slow_task using legacy direct model creation.
    """
    from django_ray.runtime.serialization import serialize_args

    task = RayTaskExecution.objects.create(
        task_id=f"api-slow-{RayTaskExecution.objects.count() + 1}",
        callable_path="testproject.tasks.slow_task",
        queue_name=queue,
        state=TaskState.QUEUED,
        args_json="[]",
        kwargs_json=serialize_args({"seconds": seconds}),
    )
    return task


@api.post("/enqueue/fail", response=TaskResponseSchema, tags=["Legacy Tasks"], deprecated=True)
def enqueue_fail(request, queue: str = "default"):
    """[DEPRECATED] Use /v2/enqueue/fail instead.

    Quick endpoint to enqueue a failing_task using legacy direct model creation.
    """
    task = RayTaskExecution.objects.create(
        task_id=f"api-fail-{RayTaskExecution.objects.count() + 1}",
        callable_path="testproject.tasks.failing_task",
        queue_name=queue,
        state=TaskState.QUEUED,
        args_json="[]",
        kwargs_json="{}",
    )
    return task


@api.post("/enqueue/{task_name}", response=TaskResponseSchema, tags=["Legacy Tasks"], deprecated=True)
def enqueue_task(request, task_name: str, payload: TaskEnqueueSchema | None = None):
    """[DEPRECATED] Use specific /v2/enqueue/* endpoints instead.

    Enqueue a testproject task by name using legacy direct model creation.

    Available tasks:
    - add_numbers: args=[a, b] -> returns a + b
    - multiply_numbers: args=[a, b] -> returns a * b
    - slow_task: kwargs={seconds: N} -> sleeps for N seconds
    - failing_task: always fails
    - echo_task: returns {args, kwargs}
    - cpu_intensive_task: kwargs={n: N} -> CPU intensive loop
    """
    from django_ray.runtime.serialization import serialize_args

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

