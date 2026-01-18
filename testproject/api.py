"""Django Ninja API for django-ray task management.

This API uses Django 6's native task framework integration with Ray.
Tasks are defined using @task decorator and enqueued using .enqueue().
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from django.shortcuts import get_object_or_404
from django.tasks import task_backends
from ninja import NinjaAPI, Schema

from django_ray.models import RayTaskExecution, TaskState

# Import tasks that use Django 6's @task decorator
from testproject import tasks

api = NinjaAPI(
    title="Django Ray API",
    version="0.2.0",
    description="API for managing Ray tasks using Django 6's native task framework",
)


# ============================================================================
# Schemas
# ============================================================================


class TaskResultSchema(Schema):
    """Schema for Django 6 task result response."""

    task_id: str
    status: str
    enqueued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    args: list
    kwargs: dict


class TaskExecutionSchema(Schema):
    """Schema for task execution details (internal model)."""

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

    tasks: list[TaskExecutionSchema]
    total: int
    queued: int
    running: int
    succeeded: int
    failed: int


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

    db_status = "ok"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        db_status = "error"

    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "database": db_status,
        "version": "0.2.0",
    }


@api.get("/metrics", tags=["Health"])
def prometheus_metrics(request):
    """Prometheus metrics endpoint.

    Returns metrics in Prometheus text format for scraping.
    """
    from django.http import HttpResponse

    from django_ray.models import RayTaskExecution, TaskState

    # Build metrics from database state
    lines = [
        "# HELP django_ray_tasks_total Total tasks by state",
        "# TYPE django_ray_tasks_total gauge",
    ]

    # Count tasks by state
    for state in TaskState:
        count = RayTaskExecution.objects.filter(state=state).count()
        lines.append(f'django_ray_tasks_total{{state="{state}"}} {count}')

    lines.extend([
        "",
        "# HELP django_ray_tasks_queued Current queued tasks",
        "# TYPE django_ray_tasks_queued gauge",
        f"django_ray_tasks_queued {RayTaskExecution.objects.filter(state=TaskState.QUEUED).count()}",
        "",
        "# HELP django_ray_tasks_running Current running tasks",
        "# TYPE django_ray_tasks_running gauge",
        f"django_ray_tasks_running {RayTaskExecution.objects.filter(state=TaskState.RUNNING).count()}",
    ])

    # Queue depths
    queues = RayTaskExecution.objects.filter(
        state=TaskState.QUEUED
    ).values_list('queue_name', flat=True).distinct()

    if queues:
        lines.extend([
            "",
            "# HELP django_ray_queue_depth Tasks queued per queue",
            "# TYPE django_ray_queue_depth gauge",
        ])
        for queue in queues:
            depth = RayTaskExecution.objects.filter(
                state=TaskState.QUEUED,
                queue_name=queue,
            ).count()
            lines.append(f'django_ray_queue_depth{{queue="{queue}"}} {depth}')

    return HttpResponse(
        "\n".join(lines) + "\n",
        content_type="text/plain; charset=utf-8",
    )


# ============================================================================
# Task Enqueueing Endpoints (Django 6 Native)
# ============================================================================


@api.post("/enqueue/add/{a}/{b}", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_add(request, a: int, b: int, queue: str = "default"):
    """Enqueue add_numbers task.

    Uses Django 6's native .enqueue() API for task submission.
    """
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


@api.post("/enqueue/multiply/{a}/{b}", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_multiply(request, a: int, b: int, queue: str = "default"):
    """Enqueue multiply_numbers task."""
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


@api.post("/enqueue/slow/{seconds}", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_slow(request, seconds: float, queue: str = "default"):
    """Enqueue slow_task that sleeps for specified seconds."""
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


@api.post("/enqueue/fail", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_fail(request, queue: str = "default"):
    """Enqueue failing_task that always raises an exception."""
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


@api.post("/enqueue/cpu/{n}", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_cpu(request, n: int, queue: str = "default"):
    """Enqueue cpu_intensive_task for load testing."""
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


@api.post("/enqueue/echo", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_echo(request, queue: str = "default"):
    """Enqueue echo_task that returns its arguments."""
    task_obj = tasks.echo_task.using(queue_name=queue)
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


# ============================================================================
# Task Result Endpoints
# ============================================================================


@api.get("/tasks/{task_id}", response=TaskResultSchema, tags=["Tasks"])
def get_task(request, task_id: str):
    """Get task status by task ID (UUID).

    Uses Django 6's native get_result() API.
    """
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
# Task Management Endpoints (Admin/Monitoring)
# ============================================================================


@api.get("/executions", response=TaskListResponseSchema, tags=["Admin"])
def list_executions(
    request,
    state: str | None = None,
    queue: str | None = None,
    limit: int = 50,
):
    """List task executions with optional filtering.

    This provides visibility into the internal execution tracking.
    """
    queryset = RayTaskExecution.objects.all()

    if state:
        queryset = queryset.filter(state=state.upper())
    if queue:
        queryset = queryset.filter(queue_name=queue)

    queryset = queryset.order_by("-created_at")[:limit]

    all_tasks = RayTaskExecution.objects.all()

    return {
        "tasks": list(queryset),
        "total": all_tasks.count(),
        "queued": all_tasks.filter(state=TaskState.QUEUED).count(),
        "running": all_tasks.filter(state=TaskState.RUNNING).count(),
        "succeeded": all_tasks.filter(state=TaskState.SUCCEEDED).count(),
        "failed": all_tasks.filter(state=TaskState.FAILED).count(),
    }


@api.get("/executions/stats", response=StatsSchema, tags=["Admin"])
def get_stats(request):
    """Get task execution statistics."""
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


@api.post("/executions/reset", response=MessageSchema, tags=["Admin"])
def reset_executions(
    request,
    state: Literal["RUNNING", "FAILED", "LOST"] | None = None,
):
    """Reset task executions to QUEUED state."""
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

    return {"message": f"Reset {count} execution(s) to QUEUED state"}


@api.get("/executions/{execution_id}", response=TaskExecutionSchema, tags=["Admin"])
def get_execution(request, execution_id: int):
    """Get detailed execution record by internal ID."""
    task = get_object_or_404(RayTaskExecution, pk=execution_id)
    return task


@api.delete("/executions/{execution_id}", response=MessageSchema, tags=["Admin"])
def delete_execution(request, execution_id: int):
    """Delete an execution record."""
    task = get_object_or_404(RayTaskExecution, pk=execution_id)
    task.delete()
    return {"message": f"Execution {execution_id} deleted"}


@api.post("/executions/{execution_id}/cancel", response=TaskExecutionSchema, tags=["Admin"])
def cancel_execution(request, execution_id: int):
    """Cancel a queued or running task execution."""
    task = get_object_or_404(RayTaskExecution, pk=execution_id)

    if task.state == TaskState.QUEUED:
        task.state = TaskState.CANCELLED
        task.save(update_fields=["state"])
    elif task.state == TaskState.RUNNING:
        task.state = TaskState.CANCELLING
        task.save(update_fields=["state"])

    return task


@api.post("/executions/{execution_id}/retry", response=TaskExecutionSchema, tags=["Admin"])
def retry_execution(request, execution_id: int):
    """Retry a failed task execution."""
    task = get_object_or_404(RayTaskExecution, pk=execution_id)

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

