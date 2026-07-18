"""Django Ninja API for django-ray task management.

This API uses Django 6's native task framework integration with Ray.
Tasks are defined using @task decorator and enqueued using .enqueue().
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Literal

from django.conf import settings
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.tasks import task_backends
from django.tasks.exceptions import InvalidTaskBackend
from ninja import NinjaAPI, Schema
from ninja.security import HttpBearer

from django_ray import __version__ as django_ray_version
from django_ray.models import RayTaskExecution, TaskState
from django_ray.observability import (
    WorkflowObservabilityError,
    get_ray_task_logs,
    get_ray_task_state,
    get_workflow_node,
    get_workflow_progress,
)

# Import tasks that use Django 6's @task decorator
from testproject import tasks
from testproject.apps.cluster_tasks import tasks as cluster_tasks
from testproject.apps.local_ray import tasks as local_tasks
from testproject.apps.ml_pipeline import tasks as ml_tasks
from testproject.apps.sync_tasks import tasks as sync_tasks


class ApiTokenAuth(HttpBearer):
    """Require the configured bearer token for every operational API endpoint."""

    def authenticate(self, request, token: str):
        expected = getattr(settings, "DJANGO_API_TOKEN", None)
        if expected and secrets.compare_digest(token, expected):
            return token
        return None


api = NinjaAPI(
    title="Django Ray API",
    version=django_ray_version,
    description="API for managing Ray tasks using Django 6's native task framework",
    auth=ApiTokenAuth(),
)


def _task_state_counts() -> dict[str, int]:
    """Return task counts grouped by state with a single query."""
    return {
        row["state"]: row["count"]
        for row in RayTaskExecution.objects.values("state").annotate(count=Count("id"))
    }


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
    progress_data: str | None
    runtime_env_profile: str | None
    runtime_env_hash: str
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


class LivenessSchema(Schema):
    """Lightweight process liveness response schema."""

    status: str
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


class WorkflowResultSchema(Schema):
    """Status and result for a Ray-native workflow task."""

    task_id: str
    state: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: dict | None
    result: dict | None
    error: str | None


class WorkflowGraphSchema(Schema):
    """UI-ready durable workflow graph and aggregate state."""

    task_id: str
    task_state: str
    schema_version: int
    revision: int
    workflow_state: str
    total_nodes: int
    completed_nodes: int
    failed_nodes: int
    running_nodes: int
    pending_nodes: int
    progress_percent: float
    updated_at: float
    graph: dict
    recent_events: list[dict]


class WorkflowNodeSchema(Schema):
    """Durable node metadata enriched with optional live Ray data."""

    task_id: str
    node: dict
    ray_state: list[dict] | None
    logs: dict[str, str] | None
    observability_error: str | None


class RuntimeEnvResultSchema(Schema):
    """Durable task state plus its immutable RuntimeEnv identity."""

    task_id: str
    state: str
    runtime_env_profile: str | None
    runtime_env_hash: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict | None
    error: str | None


# ============================================================================
# Health Endpoints
# ============================================================================


def _database_health_payload() -> dict[str, str]:
    """Return a database-backed health payload."""
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
        "version": django_ray_version,
    }


@api.get("/livez", response=LivenessSchema, tags=["Health"], auth=None)
def liveness_check(request):
    """Cheap liveness endpoint for kubelet probes."""
    return {
        "status": "alive",
        "version": django_ray_version,
    }


@api.get("/readyz", response=HealthSchema, tags=["Health"], auth=None)
def readiness_check(request):
    """Readiness endpoint that verifies database access."""
    return _database_health_payload()


@api.get("/health", response=HealthSchema, tags=["Health"], auth=None)
def health_check(request):
    """Backward-compatible health endpoint for external checks."""
    return _database_health_payload()


@api.get("/metrics", tags=["Health"])
def prometheus_metrics(request):
    """Prometheus metrics endpoint.

    Returns metrics in Prometheus text format for scraping.
    """
    from django.http import HttpResponse

    from django_ray.models import RayTaskExecution, TaskState

    task_counts = _task_state_counts()
    queued_depths = {
        row["queue_name"]: row["count"]
        for row in (
            RayTaskExecution.objects.filter(state=TaskState.QUEUED)
            .values("queue_name")
            .annotate(count=Count("id"))
            .order_by()
        )
    }

    # Build metrics from database state
    lines = [
        "# HELP django_ray_tasks_total Total tasks by state",
        "# TYPE django_ray_tasks_total gauge",
    ]

    # Count tasks by state
    for state in TaskState:
        count = task_counts.get(state, 0)
        lines.append(f'django_ray_tasks_total{{state="{state}"}} {count}')

    lines.extend(
        [
            "",
            "# HELP django_ray_tasks_queued Current queued tasks",
            "# TYPE django_ray_tasks_queued gauge",
            f"django_ray_tasks_queued {task_counts.get(TaskState.QUEUED, 0)}",
            "",
            "# HELP django_ray_tasks_running Current running tasks",
            "# TYPE django_ray_tasks_running gauge",
            f"django_ray_tasks_running {task_counts.get(TaskState.RUNNING, 0)}",
        ]
    )

    if queued_depths:
        lines.extend(
            [
                "",
                "# HELP django_ray_queue_depth Tasks queued per queue",
                "# TYPE django_ray_queue_depth gauge",
            ]
        )
        for queue, depth in sorted(queued_depths.items()):
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
    """Enqueue failing_task that always raises an exception.

    This task WILL be auto-retried based on MAX_TASK_ATTEMPTS setting.
    """
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


@api.post("/enqueue/fail-no-retry", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_fail_no_retry(request, queue: str = "default"):
    """Enqueue failing_task_no_retry that fails without auto-retry.

    Use this to test manual retry via Django admin:
    1. Call this endpoint - task will fail
    2. Go to admin, see task in FAILED state
    3. Select task and use "Retry selected tasks" action
    4. Task runs again (and fails again, but you can observe the retry)
    """
    task_obj = tasks.failing_task_no_retry.using(queue_name=queue)
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


@api.post("/enqueue/intermittent", response=TaskResultSchema, tags=["Enqueue"])
def enqueue_intermittent(request, fail_until_attempt: int = 3, queue: str = "default"):
    """Enqueue intermittent_task that fails until Nth attempt then succeeds.

    Useful for testing retry functionality:
    1. Task fails on attempt 1
    2. Use admin "Retry selected tasks" action
    3. Task fails on attempt 2 (if fail_until_attempt > 2)
    4. Keep retrying until attempt >= fail_until_attempt - task succeeds

    Args:
        fail_until_attempt: Number of attempts before success (default: 3)
        queue: Queue name (default: "default")
    """
    import json

    from django_ray.models import RayTaskExecution

    task_obj = tasks.intermittent_task.using(queue_name=queue)
    # Enqueue with placeholder execution_id=0
    result = task_obj.enqueue(execution_id=0, fail_until_attempt=fail_until_attempt)

    # Update kwargs_json with the actual execution_id
    try:
        execution = RayTaskExecution.objects.get(task_id=result.id)
        execution.kwargs_json = json.dumps(
            {
                "execution_id": execution.pk,
                "fail_until_attempt": fail_until_attempt,
            }
        )
        execution.save(update_fields=["kwargs_json"])
    except RayTaskExecution.DoesNotExist:
        pass

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

    task_counts = _task_state_counts()

    return {
        "tasks": list(queryset),
        "total": sum(task_counts.values()),
        "queued": task_counts.get(TaskState.QUEUED, 0),
        "running": task_counts.get(TaskState.RUNNING, 0),
        "succeeded": task_counts.get(TaskState.SUCCEEDED, 0),
        "failed": task_counts.get(TaskState.FAILED, 0),
    }


@api.get("/executions/stats", response=StatsSchema, tags=["Admin"])
def get_stats(request):
    """Get task execution statistics."""
    task_counts = _task_state_counts()

    return {
        "total": sum(task_counts.values()),
        "queued": task_counts.get(TaskState.QUEUED, 0),
        "running": task_counts.get(TaskState.RUNNING, 0),
        "succeeded": task_counts.get(TaskState.SUCCEEDED, 0),
        "failed": task_counts.get(TaskState.FAILED, 0),
        "cancelled": task_counts.get(TaskState.CANCELLED, 0),
        "lost": task_counts.get(TaskState.LOST, 0),
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
        progress_data=None,
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
        task.progress_data = None
        task.save()

    return task


# ============================================================================
# Example App Endpoints - Sync Tasks (--sync mode)
# ============================================================================


@api.post("/sync/calculate", response=TaskResultSchema, tags=["Sync Tasks"])
def sync_calculate(
    request,
    a: int,
    b: int,
    operation: str = "add",
):
    """Enqueue a simple calculation (sync queue).

    Run with: python manage.py django_ray_worker --sync --queue=sync
    """
    result = sync_tasks.simple_calculation.enqueue(a, b, operation=operation)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/sync/validate-email", response=TaskResultSchema, tags=["Sync Tasks"])
def sync_validate_email(request, email: str):
    """Validate an email address (sync queue)."""
    result = sync_tasks.validate_email.enqueue(email)
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
# Example App Endpoints - Local Ray (--local mode)
# ============================================================================


@api.post("/local/fibonacci/{n}", response=TaskResultSchema, tags=["Local Ray"])
def local_fibonacci(request, n: int):
    """Calculate fibonacci number (default queue).

    Run with: python manage.py django_ray_worker --local
    """
    result = local_tasks.fibonacci.enqueue(n)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/local/workload", response=TaskResultSchema, tags=["Local Ray"])
def local_workload(request, iterations: int = 1000000, sleep_ms: int = 0):
    """Simulate CPU workload (default queue)."""
    result = local_tasks.simulate_workload.enqueue(iterations=iterations, sleep_ms=sleep_ms)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/local/urgent", response=TaskResultSchema, tags=["Local Ray"])
def local_urgent(request, message: str):
    """High-priority urgent task (high-priority queue)."""
    result = local_tasks.urgent_task.enqueue(message)
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
# Stress Test Endpoints - Push the system to its limits
# ============================================================================


@api.post("/stress/cpu", response=TaskResultSchema, tags=["Stress Tests"])
def stress_cpu(request, duration_seconds: float = 5.0):
    """CPU stress test - burns CPU for specified duration.

    Args:
        duration_seconds: How long to burn CPU (default: 5s)
    """
    result = local_tasks.stress_cpu.enqueue(duration_seconds=duration_seconds)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/stress/memory", response=TaskResultSchema, tags=["Stress Tests"])
def stress_memory(request, size_mb: int = 100):
    """Memory stress test - allocates and processes large data.

    Args:
        size_mb: Amount of memory to allocate in MB (default: 100)
    """
    result = local_tasks.stress_memory.enqueue(size_mb=size_mb)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/stress/compute", response=TaskResultSchema, tags=["Stress Tests"])
def stress_compute(request, depth: int = 10, width: int = 100):
    """Nested computation stress test.

    Args:
        depth: Depth of nested loops (max 15)
        width: Width of each loop level
    """
    result = local_tasks.stress_nested_compute.enqueue(depth=depth, width=width)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/stress/primes", response=TaskResultSchema, tags=["Stress Tests"])
def stress_primes(request, start: int = 1000000, count: int = 100):
    """Prime number search - CPU intensive.

    Args:
        start: Starting number to search from
        count: How many primes to find
    """
    result = local_tasks.stress_prime_search.enqueue(start=start, count=count)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/stress/json", response=TaskResultSchema, tags=["Stress Tests"])
def stress_json(request, size_kb: int = 100, depth: int = 5):
    """Large JSON structure stress test.

    Args:
        size_kb: Target size in KB
        depth: Nesting depth
    """
    result = local_tasks.stress_json_payload.enqueue(size_kb=size_kb, depth=depth)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/stress/throughput", response=TaskResultSchema, tags=["Stress Tests"])
def stress_throughput(request, task_count: int = 100, task_duration_ms: int = 10):
    """Throughput simulation - many small tasks.

    Args:
        task_count: Number of simulated tasks
        task_duration_ms: Duration of each task in ms
    """
    result = local_tasks.stress_concurrent_simulation.enqueue(
        task_count=task_count, task_duration_ms=task_duration_ms
    )
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
# Example App Endpoints - Cluster Tasks (--cluster mode)
# ============================================================================


class ChunkDataSchema(Schema):
    """Schema for chunk data input."""

    data: list
    chunk_id: int = 0


@api.post("/cluster/process-chunk", response=TaskResultSchema, tags=["Cluster Tasks"])
def cluster_process_chunk(request, payload: ChunkDataSchema):
    """Process a data chunk (default queue).

    Run with: python manage.py django_ray_worker --cluster ray://head:10001
    """
    result = cluster_tasks.process_chunk.enqueue(data=payload.data, chunk_id=payload.chunk_id)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


class BatchUrlsSchema(Schema):
    """Schema for batch URL requests."""

    urls: list[str]
    timeout_seconds: int = 30


@api.post("/cluster/batch-http", response=TaskResultSchema, tags=["Cluster Tasks"])
def cluster_batch_http(request, payload: BatchUrlsSchema):
    """Simulate batch HTTP requests (default queue)."""
    result = cluster_tasks.batch_http_requests.enqueue(
        urls=payload.urls,
        timeout_seconds=payload.timeout_seconds,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


class DistributedSearchSchema(Schema):
    """Schema for distributed search request."""

    pattern: str
    data_sources: list[str]
    case_sensitive: bool = False


@api.post("/cluster/search", response=TaskResultSchema, tags=["Cluster Tasks"])
def cluster_distributed_search(request, payload: DistributedSearchSchema):
    """Search for a pattern across multiple data sources IN PARALLEL.

    This is a TRUE distributed search - when running on a Ray cluster,
    each data source is searched on a different worker simultaneously.

    Example:
        {
            "pattern": "test",
            "data_sources": ["source1_test", "source2", "test_source3", "source4"],
            "case_sensitive": false
        }

    The response will show cluster info including speedup from parallelization.
    """
    result = cluster_tasks.distributed_search.enqueue(
        pattern=payload.pattern,
        data_sources=payload.data_sources,
        case_sensitive=payload.case_sensitive,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/cluster/cpu-benchmark", response=TaskResultSchema, tags=["Cluster Tasks"])
def cluster_cpu_benchmark(request, num_items: int = 10, seconds_per_item: float = 2.0):
    """Benchmark distributed CPU work across the cluster.

    This spawns num_items Ray tasks, each burning CPU for seconds_per_item.
    With a cluster, these run in parallel showing real speedup.

    Understanding Ray CPUs vs Physical Cores:
    - Ray reports "logical CPUs" which may include hyperthreads
    - A Ryzen 7 5800X (8 cores/16 threads) may show 12 CPUs in Ray
    - Only physical cores (8) can do full parallel CPU work
    - Extra "CPUs" are useful for I/O-bound tasks, not CPU-bound

    Example with 8 physical cores:
    - num_items=8, seconds_per_item=2 → ~2s (8 parallel, 1 batch)
    - num_items=16, seconds_per_item=2 → ~4s (8 parallel × 2 batches)
    - num_items=24, seconds_per_item=2 → ~6s (8 parallel × 3 batches)

    Speedup = (num_items × seconds_per_item) / actual_time
    Efficiency = speedup / physical_cores × 100%

    Args:
        num_items: Number of parallel tasks (default: 10)
        seconds_per_item: CPU time per task in seconds (default: 2.0)
    """
    result = cluster_tasks.distributed_cpu_benchmark.enqueue(
        num_items=num_items,
        seconds_per_item=seconds_per_item,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post("/cluster/workflow-benchmark", response=TaskResultSchema, tags=["Workflows"])
def cluster_workflow_benchmark(
    request,
    num_items: int = 8,
    seconds_per_item: float = 0.25,
):
    """Enqueue one durable task that fans out Ray-native workflow leaves.

    Poll ``GET /api/cluster/workflow-benchmark/{task_id}`` for the result.
    Only the outer task creates a database execution row.
    """
    result = cluster_tasks.workflow_fanout_benchmark.enqueue(
        num_items=num_items,
        seconds_per_item=seconds_per_item,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.get(
    "/cluster/workflow-benchmark/{task_id}",
    response=WorkflowResultSchema,
    tags=["Workflows"],
)
def get_cluster_workflow_benchmark(request, task_id: str):
    """Return workflow state, timing summary, leaf details, or failure."""
    execution = get_object_or_404(
        RayTaskExecution,
        task_id=task_id,
        callable_path=("testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark"),
    )
    result = _result_value_for_execution(execution)
    try:
        progress = get_workflow_progress(execution)
    except WorkflowObservabilityError:
        progress = None
    return {
        "task_id": execution.task_id,
        "state": execution.state,
        "created_at": execution.created_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "progress": progress,
        "result": result,
        "error": execution.error_message,
    }


@api.post("/cluster/complex-workflow", response=TaskResultSchema, tags=["Workflows"])
def cluster_complex_workflow(
    request,
    fast_items: int = 8,
    slow_items: int = 4,
    fast_seconds: float = 0.02,
    slow_seconds: float = 0.5,
):
    """Run nested fast and slow branches to demonstrate groups and chains."""
    result = cluster_tasks.complex_workflow_benchmark.enqueue(
        fast_items=fast_items,
        slow_items=slow_items,
        fast_seconds=fast_seconds,
        slow_seconds=slow_seconds,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.get(
    "/cluster/complex-workflow/{task_id}",
    response=WorkflowResultSchema,
    tags=["Workflows"],
)
def get_cluster_complex_workflow(request, task_id: str):
    """Return live progress and results for the nested workflow example."""
    execution = get_object_or_404(
        RayTaskExecution,
        task_id=task_id,
        callable_path=("testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"),
    )
    try:
        progress = get_workflow_progress(execution)
    except WorkflowObservabilityError:
        progress = None
    return {
        "task_id": execution.task_id,
        "state": execution.state,
        "created_at": execution.created_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "progress": progress,
        "result": _result_value_for_execution(execution),
        "error": execution.error_message,
    }


@api.get(
    "/cluster/workflows/{task_id}/graph",
    response=WorkflowGraphSchema,
    tags=["Workflows"],
)
def get_cluster_workflow_graph(request, task_id: str):
    """Return a versioned node/edge graph suitable for custom tracking UIs."""
    execution = get_object_or_404(RayTaskExecution, task_id=task_id)
    try:
        progress = get_workflow_progress(execution)
    except WorkflowObservabilityError as exc:
        raise Http404(str(exc)) from exc
    if progress is None:
        raise Http404("Workflow graph is not available yet")
    return {
        "task_id": execution.task_id,
        "task_state": execution.state,
        "schema_version": progress.get("schema_version", 1),
        "revision": progress.get("revision", 0),
        "workflow_state": progress.get("state", "RUNNING"),
        "total_nodes": progress.get("total_nodes", 0),
        "completed_nodes": progress.get("completed_nodes", 0),
        "failed_nodes": progress.get("failed_nodes", 0),
        "running_nodes": progress.get("running_nodes", 0),
        "pending_nodes": progress.get("pending_nodes", 0),
        "progress_percent": progress.get("progress_percent", 0.0),
        "updated_at": progress.get("updated_at", 0.0),
        "graph": progress.get(
            "graph",
            {"nodes": progress.get("nodes", []), "edges": []},
        ),
        "recent_events": progress.get("recent_events", []),
    }


@api.get(
    "/cluster/workflows/{task_id}/nodes/{node_id}",
    response=WorkflowNodeSchema,
    tags=["Workflows"],
)
def get_cluster_workflow_node(
    request,
    task_id: str,
    node_id: str,
    include_logs: bool = False,
    tail: int = 200,
):
    """Return durable node metadata plus live Ray state and optional log tails."""
    execution = get_object_or_404(RayTaskExecution, task_id=task_id)
    node = get_workflow_node(execution, node_id)
    if node is None:
        raise Http404("Workflow node was not found")

    ray_state = None
    logs = None
    observability_error = None
    ray_task_id = node.get("execution", {}).get("ray_task_id")
    if ray_task_id:
        try:
            ray_state = get_ray_task_state(ray_task_id)
            if include_logs:
                logs = get_ray_task_logs(ray_task_id, tail=tail)
        except Exception as error:
            observability_error = str(error)

    return {
        "task_id": execution.task_id,
        "node": node,
        "ray_state": ray_state,
        "logs": logs,
        "observability_error": observability_error,
    }


_RUNTIME_ENV_BACKENDS = {
    "project": "default",
    "thin": "thin",
    "numpy-2-2": "numpy-2-2",
    "numpy-2-3": "numpy-2-3",
}


def _result_backend_alias_for_execution(execution: RayTaskExecution) -> str:
    profile = execution.runtime_env_profile or "project"
    return _RUNTIME_ENV_BACKENDS.get(profile, "default")


def _result_value_for_execution(execution: RayTaskExecution) -> object:
    """Resolve durable task return values, including externally stored payloads."""
    if execution.state != TaskState.SUCCEEDED:
        return json.loads(execution.result_data) if execution.result_data else None

    backend_alias = _result_backend_alias_for_execution(execution)
    try:
        backend = task_backends[backend_alias]
    except (KeyError, InvalidTaskBackend):
        backend = task_backends["default"]
    return backend.get_result(execution.task_id).return_value


@api.post("/cluster/runtime-env/probe", response=TaskResultSchema, tags=["Runtime Environments"])
def cluster_runtime_env_probe(
    request,
    profile: Literal["project", "thin", "numpy-2-2", "numpy-2-3"] = "thin",
    package: str | None = None,
):
    """Enqueue a task through a backend bound to the selected RuntimeEnv profile."""
    task_obj = cluster_tasks.runtime_env_probe.using(backend=_RUNTIME_ENV_BACKENDS[profile])
    result = task_obj.enqueue(package=package)
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.post(
    "/cluster/runtime-env/benchmark",
    response=TaskResultSchema,
    tags=["Runtime Environments"],
)
def cluster_runtime_env_benchmark(
    request,
    profile: Literal["thin", "numpy-2-2", "numpy-2-3"] = "thin",
    repeats: int = 2,
    package: str | None = None,
):
    """Time repeated workflow leaves to compare cold and cached environment setup."""
    result = cluster_tasks.runtime_env_benchmark.enqueue(
        profile=profile,
        repeats=repeats,
        package=package,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


@api.get(
    "/cluster/runtime-env/{task_id}",
    response=RuntimeEnvResultSchema,
    tags=["Runtime Environments"],
)
def get_cluster_runtime_env_result(request, task_id: str):
    """Return a probe or benchmark result with the durable environment identity."""
    execution = get_object_or_404(
        RayTaskExecution,
        task_id=task_id,
        callable_path__in=[
            "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            "testproject.apps.cluster_tasks.tasks.runtime_env_benchmark",
        ],
    )
    return {
        "task_id": execution.task_id,
        "state": execution.state,
        "runtime_env_profile": execution.runtime_env_profile,
        "runtime_env_hash": execution.runtime_env_hash,
        "created_at": execution.created_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "result": _result_value_for_execution(execution),
        "error": execution.error_message,
    }


# ============================================================================
# Example App Endpoints - ML Pipeline
# ============================================================================


class TrainModelSchema(Schema):
    """Schema for model training request."""

    dataset_id: str
    hyperparams: dict | None = None
    epochs: int = 10


@api.post("/ml/train", response=TaskResultSchema, tags=["ML Pipeline"])
def ml_train_model(request, payload: TrainModelSchema):
    """Train a model (ml queue).

    Run with: python manage.py django_ray_worker --local --queue=ml
    """
    result = ml_tasks.train_model.enqueue(
        dataset_id=payload.dataset_id,
        hyperparams=payload.hyperparams,
        epochs=payload.epochs,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


class BatchInferenceSchema(Schema):
    """Schema for batch inference request."""

    model_id: str
    samples: list[dict]


@api.post("/ml/inference", response=TaskResultSchema, tags=["ML Pipeline"])
def ml_batch_inference(request, payload: BatchInferenceSchema):
    """Run batch inference (ml queue)."""
    result = ml_tasks.batch_inference.enqueue(
        model_id=payload.model_id,
        samples=payload.samples,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }


class HyperparamSearchSchema(Schema):
    """Schema for hyperparameter search request."""

    dataset_id: str
    param_grid: dict[str, list]
    metric: str = "accuracy"


@api.post("/ml/hyperparam-search", response=TaskResultSchema, tags=["ML Pipeline"])
def ml_hyperparam_search(request, payload: HyperparamSearchSchema):
    """Run hyperparameter grid search (ml queue)."""
    result = ml_tasks.hyperparameter_search.enqueue(
        dataset_id=payload.dataset_id,
        param_grid=payload.param_grid,
        metric=payload.metric,
    )
    return {
        "task_id": result.id,
        "status": result.status.value,
        "enqueued_at": result.enqueued_at,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "args": result.args,
        "kwargs": result.kwargs,
    }
