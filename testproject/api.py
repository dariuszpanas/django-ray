"""Django Ninja API for django-ray task management.

This API uses Django 6's native task framework integration with Ray.
Tasks are defined using @task decorator and enqueued using .enqueue().
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.tasks import task_backends
from django.tasks.exceptions import InvalidTaskBackend
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.security import HttpBearer
from pydantic import Field, field_validator

from django_ray import __version__ as django_ray_version
from django_ray.lifecycle import (
    request_task_cancellation,
    retry_task,
)
from django_ray.metrics import render_prometheus_metrics
from django_ray.models import RayTaskExecution, TaskState
from django_ray.observability import (
    WorkflowObservabilityError,
    get_workflow_node_snapshot,
    get_workflow_progress,
)
from django_ray.redaction import redact_text, redact_value, safe_json_dumps
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    resolve_runtime_env_profile,
)
from django_ray.workflow_plans import WorkflowPlanValidationError, runtime_env_plan_identity
from django_ray.workflow_progress_reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)

# Import tasks that use Django 6's @task decorator
from testproject import tasks
from testproject.apps.cluster_tasks import tasks as cluster_tasks
from testproject.apps.local_ray import tasks as local_tasks
from testproject.apps.ml_pipeline import tasks as ml_tasks
from testproject.apps.sync_tasks import tasks as sync_tasks


def _workflow_observability_executions():
    """Avoid loading either durable progress payload before the bounded reader."""
    return RayTaskExecution.objects.defer(
        "progress_data",
        "workflow_progress_summary_json",
        "runtime_env_json",
    )


_WORKFLOW_OBSERVABILITY_CALLABLES = frozenset(
    {
        "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark",
        "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task",
        "testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark",
        "testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task",
    }
)


def _authorize_example_workflow(execution: RayTaskExecution) -> bool:
    """Apply the sample's explicit object policy after bearer authentication."""
    return execution.callable_path in _WORKFLOW_OBSERVABILITY_CALLABLES


def _bounded_workflow_observability_execution(task_id: str) -> RayTaskExecution:
    """Resolve only bounded identity fields before the package reader reloads the row."""
    try:
        return RayTaskExecution.objects.only("pk", "callable_path").get(task_id=task_id)
    except RayTaskExecution.DoesNotExist as error:
        raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.NOT_FOUND) from error


def _require_example_workflow_access(execution: RayTaskExecution) -> None:
    """Authorize before parsing operation-specific workflow read arguments."""
    if not _authorize_example_workflow(execution):
        raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.ACCESS_DENIED)


def _workflow_integer_argument(value: str | None, *, default: int | None = None) -> int | None:
    """Normalize one integer query argument into the package service contract."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.INVALID_ARGUMENT) from error


def _workflow_limit_argument(value: str | None) -> int:
    """Return the default or parsed page limit with a concrete integer type."""
    normalized = _workflow_integer_argument(value, default=100)
    if normalized is None:  # pragma: no cover - the non-None default is invariant
        raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
    return normalized


class ApiTokenAuth(HttpBearer):
    """Require the configured bearer token for every operational API endpoint."""

    def authenticate(self, request, token: str):
        expected = getattr(settings, "DJANGO_API_TOKEN", None)
        if expected and secrets.compare_digest(token, expected):
            return "django-ray-testproject-operator"
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


def _configured_task_queues() -> tuple[str, ...]:
    """Return the testproject's explicit queue allowlist for metrics labels."""
    return tuple(
        sorted(
            {
                queue_name
                for backend in settings.TASKS.values()
                for queue_name in backend.get("QUEUES", ())
            }
        )
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

    @field_validator("args", mode="before")
    @classmethod
    def redact_args(cls, value):
        return redact_value(value)

    @field_validator("kwargs", mode="before")
    @classmethod
    def redact_kwargs(cls, value):
        return redact_value(value)


class TaskExecutionSchema(Schema):
    """Schema for task execution details (internal model)."""

    id: int
    task_id: str
    callable_path: str
    queue_name: str
    state: str
    attempt_number: int
    execution_generation: int
    workflow_run_id: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result_data: str | None
    runtime_env_profile: str | None
    runtime_env_hash: str
    error_message: str | None

    @field_validator("result_data", mode="before")
    @classmethod
    def redact_json_fields(cls, value):
        if value is None:
            return None
        try:
            return safe_json_dumps(json.loads(value))
        except (TypeError, json.JSONDecodeError):
            return redact_text(value)

    @field_validator("error_message", mode="before")
    @classmethod
    def redact_error(cls, value):
        return redact_text(value) if value is not None else None


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

    @field_validator("progress", "result", mode="before")
    @classmethod
    def redact_workflow_payload(cls, value):
        return redact_value(value)

    @field_validator("error", mode="before")
    @classmethod
    def redact_workflow_error(cls, value):
        return redact_text(value) if value is not None else None


class WorkflowRecoveryAttemptSchema(Schema):
    """One bounded terminal attempt in the recovery showcase."""

    attempt_number: int
    state: str
    error: str | None

    @field_validator("error", mode="before")
    @classmethod
    def redact_attempt_error(cls, value):
        return redact_text(value) if value is not None else None


class WorkflowRecoveryResultSchema(WorkflowResultSchema):
    """Current recovery result plus its three bounded attempt outcomes."""

    attempt_number: int
    runtime_env_profile: str
    attempts: list[WorkflowRecoveryAttemptSchema]


class WorkflowNodeSchema(Schema):
    """Durable node metadata enriched with optional live Ray data."""

    task_id: str
    node: dict
    ray_state: list[dict] | None
    logs: dict[str, str] | None
    observability_error: str | None

    @field_validator("node", "ray_state", mode="before")
    @classmethod
    def redact_node_payload(cls, value):
        return redact_value(value)

    @field_validator("logs", mode="before")
    @classmethod
    def redact_logs(cls, value):
        return redact_value(value)

    @field_validator("observability_error", mode="before")
    @classmethod
    def redact_observability_error(cls, value):
        return redact_text(value) if value is not None else None


class WorkflowProgressSummaryReadSchema(Schema):
    """Bounded package summary adapted to the example HTTP API."""

    schema_name: str = Field(alias="schema")
    schema_version: int
    generated_at: str
    task_id: str
    run_identity: dict | None
    publication: dict
    availability: str
    complete: bool
    source_schema_version: int | None
    summary: dict | None


class WorkflowProgressPageReadSchema(Schema):
    """One bounded topology or normalized-detail page."""

    schema_name: str = Field(alias="schema")
    schema_version: int
    generated_at: str
    task_id: str
    run_identity: dict | None
    publication: dict
    availability: str
    complete: bool
    collection: str
    returned_count: int
    items: list[dict]
    next_cursor: str | None


class WorkflowProgressNodeReadSchema(Schema):
    """One indexed normalized-node lookup result."""

    schema_name: str = Field(alias="schema")
    schema_version: int
    generated_at: str
    task_id: str
    run_identity: dict | None
    publication: dict
    availability: str
    complete: bool
    found: bool
    item: dict | None


class WorkflowProgressReadErrorSchema(Schema):
    """Stable bounded package-read error."""

    code: str
    message: str


_WORKFLOW_READ_RESPONSES = {
    200: WorkflowProgressPageReadSchema,
    400: WorkflowProgressReadErrorSchema,
    403: WorkflowProgressReadErrorSchema,
    404: WorkflowProgressReadErrorSchema,
    409: WorkflowProgressReadErrorSchema,
    503: WorkflowProgressReadErrorSchema,
}


def _workflow_read_error_response(
    error: WorkflowProgressReadError,
) -> tuple[int, dict[str, str]]:
    status_by_code = {
        WorkflowProgressReadErrorCode.ACCESS_DENIED: 403,
        WorkflowProgressReadErrorCode.NOT_FOUND: 404,
        WorkflowProgressReadErrorCode.INVALID_ARGUMENT: 400,
        WorkflowProgressReadErrorCode.INVALID_CURSOR: 400,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH: 409,
        WorkflowProgressReadErrorCode.MISSING: 409,
        WorkflowProgressReadErrorCode.CORRUPT: 503,
    }
    # django-ninja 1.5.1 supports status tuples but does not export Status.
    return (
        status_by_code[error.code],
        {
            "code": error.code.value,
            "message": str(error),
        },
    )


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

    @field_validator("result", mode="before")
    @classmethod
    def redact_result(cls, value):
        return redact_value(value)

    @field_validator("error", mode="before")
    @classmethod
    def redact_error(cls, value):
        return redact_text(value) if value is not None else None


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
    """Adapt the package metrics renderer behind testproject bearer auth."""
    return HttpResponse(
        render_prometheus_metrics(queue_names=_configured_task_queues()),
        content_type="text/plain; version=0.0.4; charset=utf-8",
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
    task_obj = tasks.intermittent_task.using(queue_name=queue)
    result = task_obj.enqueue(fail_until_attempt=fail_until_attempt)

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
    task_id: str | None = None,
    limit: int = 50,
):
    """List task executions with optional filtering.

    This provides visibility into the internal execution tracking.
    """
    queryset = _workflow_observability_executions()

    if state:
        queryset = queryset.filter(state=state.upper())
    if queue:
        queryset = queryset.filter(queue_name=queue)
    if task_id:
        queryset = queryset.filter(task_id=task_id)

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
    state: Literal["FAILED", "CANCELLED", "LOST"] | None = None,
):
    """Retry terminal task executions through the locked lifecycle service."""
    retryable_states = (TaskState.FAILED, TaskState.CANCELLED, TaskState.LOST)
    if state:
        queryset = RayTaskExecution.objects.filter(state=state.upper())
    else:
        queryset = RayTaskExecution.objects.filter(state__in=retryable_states)

    execution_ids = list(queryset.values_list("pk", flat=True))
    count = 0
    blocked = 0
    for execution_id in execution_ids:
        execution = (
            RayTaskExecution.objects.only(
                "pk",
                "state",
                "attempt_number",
                "execution_generation",
            )
            .filter(pk=execution_id)
            .first()
        )
        if execution is None:
            continue
        try:
            reset = (
                retry_task(
                    execution.pk,
                    expected_attempt_number=execution.attempt_number,
                    expected_execution_generation=execution.execution_generation,
                )
                is not None
            )
        except RuntimeEnvSnapshotError:
            blocked += 1
            continue
        count += int(reset)

    message = f"Reset {count} execution(s) to QUEUED state"
    if blocked:
        message += (
            f"; blocked {blocked} execution(s) because their persisted RuntimeEnv "
            "snapshots failed validation"
        )
    return {"message": message}


@api.get("/executions/{execution_id}", response=TaskExecutionSchema, tags=["Admin"])
def get_execution(request, execution_id: int):
    """Get detailed execution record by internal ID."""
    task = get_object_or_404(_workflow_observability_executions(), pk=execution_id)
    return task


@api.post("/executions/{execution_id}/cancel", response=TaskExecutionSchema, tags=["Admin"])
def cancel_execution(request, execution_id: int):
    """Cancel a queued or running task execution."""
    task = get_object_or_404(_workflow_observability_executions(), pk=execution_id)

    request_task_cancellation(
        task.pk,
        expected_attempt_number=task.attempt_number,
        expected_execution_generation=task.execution_generation,
    )
    task.refresh_from_db()
    return task


@api.post("/executions/{execution_id}/retry", response=TaskExecutionSchema, tags=["Admin"])
def retry_execution(request, execution_id: int):
    """Retry a failed, cancelled, or lost task execution."""
    task = get_object_or_404(_workflow_observability_executions(), pk=execution_id)
    try:
        retried = retry_task(
            task.pk,
            expected_attempt_number=task.attempt_number,
            expected_execution_generation=task.execution_generation,
        )
    except RuntimeEnvSnapshotError:
        raise HttpError(
            409,
            "Persisted RuntimeEnv snapshot failed validation",
        ) from None
    if retried is not None:
        return retried
    task.refresh_from_db()
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
    """Priority-100 urgent task on its workload-isolation queue."""
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
        _workflow_observability_executions(),
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
    failure_branch: Literal["fast", "slow"] | None = None,
    failure_item: int | None = None,
    reporting_policy: Literal["full", "terminal_only", "disabled"] | None = None,
):
    """Run nested branches with an optional explicit workflow reporting policy."""
    try:
        cluster_tasks.validate_complex_workflow_failure_controls(
            fast_items=fast_items,
            slow_items=slow_items,
            failure_branch=failure_branch,
            failure_item=failure_item,
        )
    except ValueError as error:
        raise HttpError(422, str(error)) from error
    workflow_options: dict[str, Any] = {
        "fast_items": fast_items,
        "slow_items": slow_items,
        "fast_seconds": fast_seconds,
        "slow_seconds": slow_seconds,
    }
    if failure_branch is not None:
        workflow_options.update(
            failure_branch=failure_branch,
            failure_item=failure_item,
        )
    if reporting_policy is not None:
        workflow_options["reporting_policy"] = reporting_policy
    result = cluster_tasks.complex_workflow_benchmark.enqueue(**workflow_options)
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
        _workflow_observability_executions(),
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


@api.post("/cluster/workflow-showcase", response=TaskResultSchema, tags=["Workflows"])
def cluster_workflow_showcase(
    request,
    item_count: int = 3,
    work_seconds: float = 0.05,
    failure_stage: Literal["reserve_inventory"] | None = None,
    failure_item: int | None = None,
):
    """Enqueue the full-reporting repeated split/join workflow showcase."""
    try:
        cluster_tasks.validate_order_fulfillment_showcase_inputs(
            item_count=item_count,
            work_seconds=work_seconds,
            failure_stage=failure_stage,
            failure_item=failure_item,
        )
    except ValueError as error:
        raise HttpError(422, str(error)) from error
    workflow_options: dict[str, Any] = {
        "item_count": item_count,
        "work_seconds": work_seconds,
    }
    if failure_stage is not None:
        workflow_options.update(
            failure_stage=failure_stage,
            failure_item=failure_item,
        )
    result = cluster_tasks.order_fulfillment_showcase_task.enqueue(**workflow_options)
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
    "/cluster/workflow-showcase/{task_id}",
    response=WorkflowResultSchema,
    tags=["Workflows"],
)
def get_cluster_workflow_showcase(request, task_id: str):
    """Return a bounded progress summary and the compact result or failure."""
    execution = get_object_or_404(
        _workflow_observability_executions(),
        task_id=task_id,
        callable_path=("testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task"),
    )
    try:
        progress = get_workflow_progress_summary(
            execution,
            authorize=_authorize_example_workflow,
            include_legacy=False,
            infer_current_reporting_policy=False,
        )
    except WorkflowProgressReadError:
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


@api.post(
    "/cluster/workflow-recovery-showcase",
    response=TaskResultSchema,
    tags=["Workflows"],
)
def cluster_workflow_recovery_showcase(
    request,
    item_count: int = 3,
    work_seconds: float = 0.05,
):
    """Enqueue the fixed early-fail, mid-fail, successful recovery sequence."""
    try:
        cluster_tasks.validate_order_fulfillment_showcase_inputs(
            item_count=item_count,
            work_seconds=work_seconds,
            failure_stage=None,
            failure_item=None,
        )
    except ValueError as error:
        raise HttpError(422, str(error)) from error
    try:
        recovery_runtime_env = resolve_runtime_env_profile(
            "recovery-showcase",
            config=settings.DJANGO_RAY,
        )
        recovery_identity = runtime_env_plan_identity(
            recovery_runtime_env,
            trust_identity=settings.DJANGO_RAY.get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
        )
        if not recovery_identity.retry_safe:
            raise ImproperlyConfigured(
                "recovery-showcase RuntimeEnv has no immutable retry identity"
            )
        result = cluster_tasks.order_fulfillment_recovery_showcase_task.using(
            backend="recovery-showcase"
        ).enqueue(
            item_count=item_count,
            work_seconds=work_seconds,
        )
    except (ImproperlyConfigured, InvalidTaskBackend, WorkflowPlanValidationError) as error:
        raise HttpError(
            503,
            "Workflow recovery showcase requires a valid 'recovery-showcase' task backend "
            "and RuntimeEnv profile.",
        ) from error
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
    "/cluster/workflow-recovery-showcase/{task_id}",
    response=WorkflowRecoveryResultSchema,
    tags=["Workflows"],
)
def get_cluster_workflow_recovery_showcase(request, task_id: str):
    """Return the current result and the complete bounded attempt sequence."""
    execution = get_object_or_404(
        _workflow_observability_executions(),
        task_id=task_id,
        callable_path=(
            "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task"
        ),
    )
    try:
        progress = get_workflow_progress_summary(
            execution,
            authorize=_authorize_example_workflow,
            include_legacy=False,
            infer_current_reporting_policy=False,
        )
    except WorkflowProgressReadError:
        progress = None
    attempt_rows = execution.attempts.order_by("attempt_number").values(
        "attempt_number",
        "state",
        "error_message",
    )[:4]
    return {
        "task_id": execution.task_id,
        "state": execution.state,
        "attempt_number": execution.attempt_number,
        "runtime_env_profile": execution.runtime_env_profile,
        "attempts": [
            {
                "attempt_number": attempt["attempt_number"],
                "state": attempt["state"],
                "error": attempt["error_message"],
            }
            for attempt in attempt_rows
        ],
        "created_at": execution.created_at,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "progress": progress,
        "result": _result_value_for_execution(execution),
        "error": execution.error_message,
    }


@api.get(
    "/cluster/workflows/{task_id}",
    response={
        200: WorkflowProgressSummaryReadSchema,
        400: WorkflowProgressReadErrorSchema,
        403: WorkflowProgressReadErrorSchema,
        404: WorkflowProgressReadErrorSchema,
        409: WorkflowProgressReadErrorSchema,
        503: WorkflowProgressReadErrorSchema,
    },
    tags=["Workflows"],
    by_alias=True,
)
def get_cluster_workflow_summary(
    request,
    task_id: str,
    attempt_number: str | None = None,
):
    """Return the bounded package summary after per-object authorization."""
    try:
        execution = _bounded_workflow_observability_execution(task_id)
        _require_example_workflow_access(execution)
        return get_workflow_progress_summary(
            execution,
            authorize=_authorize_example_workflow,
            include_legacy=True,
            attempt_number=_workflow_integer_argument(attempt_number),
        )
    except WorkflowProgressReadError as error:
        return _workflow_read_error_response(error)


@api.get(
    "/cluster/workflows/{task_id}/topology/nodes",
    response=_WORKFLOW_READ_RESPONSES,
    tags=["Workflows"],
    by_alias=True,
)
def get_cluster_workflow_topology_nodes(
    request,
    task_id: str,
    attempt_number: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
):
    """Return one deterministic bounded topology-node page."""
    try:
        execution = _bounded_workflow_observability_execution(task_id)
        _require_example_workflow_access(execution)
        applied_limit = _workflow_limit_argument(limit)
        return list_workflow_topology_nodes(
            execution,
            authorize=_authorize_example_workflow,
            attempt_number=_workflow_integer_argument(attempt_number),
            cursor=cursor,
            limit=applied_limit,
        )
    except WorkflowProgressReadError as error:
        return _workflow_read_error_response(error)


@api.get(
    "/cluster/workflows/{task_id}/topology/edges",
    response=_WORKFLOW_READ_RESPONSES,
    tags=["Workflows"],
    by_alias=True,
)
def get_cluster_workflow_topology_edges(
    request,
    task_id: str,
    attempt_number: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
):
    """Return one deterministic bounded topology-edge page."""
    try:
        execution = _bounded_workflow_observability_execution(task_id)
        _require_example_workflow_access(execution)
        applied_limit = _workflow_limit_argument(limit)
        return list_workflow_topology_edges(
            execution,
            authorize=_authorize_example_workflow,
            attempt_number=_workflow_integer_argument(attempt_number),
            cursor=cursor,
            limit=applied_limit,
        )
    except WorkflowProgressReadError as error:
        return _workflow_read_error_response(error)


@api.get(
    "/cluster/workflows/{task_id}/nodes",
    response=_WORKFLOW_READ_RESPONSES,
    tags=["Workflows"],
    by_alias=True,
)
def get_cluster_workflow_node_details(
    request,
    task_id: str,
    attempt_number: str | None = None,
    state: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
):
    """Return one bounded latest-state node-detail page."""
    try:
        execution = _bounded_workflow_observability_execution(task_id)
        _require_example_workflow_access(execution)
        applied_limit = _workflow_limit_argument(limit)
        return list_workflow_node_details(
            execution,
            authorize=_authorize_example_workflow,
            attempt_number=_workflow_integer_argument(attempt_number),
            state=state,
            cursor=cursor,
            limit=applied_limit,
        )
    except WorkflowProgressReadError as error:
        return _workflow_read_error_response(error)


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
    execution = get_object_or_404(_workflow_observability_executions(), task_id=task_id)
    snapshot = get_workflow_node_snapshot(
        execution,
        node_id,
        include_live=True,
        include_logs=include_logs,
        tail=tail,
    )
    if snapshot is None:
        raise Http404("Workflow node was not found")

    live = snapshot["live"]

    return {
        "task_id": execution.task_id,
        "node": snapshot["node"],
        "ray_state": live["ray_state"],
        "logs": live["logs"],
        "observability_error": live["reason"],
    }


@api.get(
    "/cluster/workflows/{task_id}/node-detail",
    response={
        200: WorkflowProgressNodeReadSchema,
        400: WorkflowProgressReadErrorSchema,
        403: WorkflowProgressReadErrorSchema,
        404: WorkflowProgressReadErrorSchema,
        409: WorkflowProgressReadErrorSchema,
        503: WorkflowProgressReadErrorSchema,
    },
    tags=["Workflows"],
    by_alias=True,
)
def get_cluster_workflow_node_detail(
    request,
    task_id: str,
    node_id: str | None = None,
    attempt_number: str | None = None,
):
    """Return one indexed durable node record without scanning the graph."""
    try:
        execution = _bounded_workflow_observability_execution(task_id)
        _require_example_workflow_access(execution)
        if node_id is None:
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.INVALID_ARGUMENT)
        return get_workflow_node_detail(
            execution,
            node_id,
            authorize=_authorize_example_workflow,
            attempt_number=_workflow_integer_argument(attempt_number),
        )
    except WorkflowProgressReadError as error:
        return _workflow_read_error_response(error)


_RUNTIME_ENV_BACKENDS = {
    "project": "default",
    "recovery-showcase": "recovery-showcase",
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
        RayTaskExecution.objects.defer("runtime_env_json"),
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
