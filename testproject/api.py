"""Django Ninja API for django-ray task management.

This API uses Django 6's native task framework integration with Ray.
Tasks are defined using @task decorator and enqueued using .enqueue().
"""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Iterator
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Any, Literal
from uuid import UUID

import ninja.responses as ninja_responses
from django.conf import settings
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.models import (
    BooleanField,
    Case,
    Count,
    F,
    Func,
    IntegerField,
    Q,
    QuerySet,
    TextField,
    Value,
    When,
)
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.tasks import TaskResultStatus
from django.tasks.exceptions import InvalidTaskBackend
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.security import HttpBearer
from pydantic import Field, field_validator

from django_ray import __version__ as django_ray_version
from django_ray.lifecycle import (
    TaskCancellationRequestResult,
    TaskCancellationRequestStatus,
    TaskRetryRequestResult,
    TaskRetryRequestStatus,
    request_task_cancellation,
    request_task_retry,
)
from django_ray.metrics import render_prometheus_metrics
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.redaction import REDACTED, redact_text, redact_value, safe_json_dumps
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


_EXECUTION_LIST_MIN_LIMIT = 1
_EXECUTION_LIST_DEFAULT_LIMIT = 50
_EXECUTION_LIST_MAX_LIMIT = 100
_EXECUTION_LIST_CURSOR_MAX_CHARACTERS = 512
_EXECUTION_LIST_CURSOR_SALT = "testproject.execution-list.v1"
_EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES = 4_096
_EXECUTION_LIST_RESPONSE_MAX_BYTES = 256 * 1_024
_EXECUTION_LIST_DIAGNOSTIC_OMISSION_REASON = "stored_value_exceeds_list_limit"
_EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES = 64 * 1_024
_EXECUTION_DETAIL_RESPONSE_MAX_BYTES = 256 * 1_024
_EXECUTION_RESULT_JSON_MAX_DEPTH = 20
_EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS = 4_096
_EXECUTION_DETAIL_DIAGNOSTIC_OMISSION_REASON = "stored_value_exceeds_detail_limit"
_EXECUTION_DETAIL_EXTERNAL_RESULT_OMISSION_REASON = "external_result_not_loaded"
_EXECUTION_DETAIL_RESPONSE_OMISSION_REASON = "response_size_limit"
_EXECUTION_PROJECTION_DATABASE_VENDORS = frozenset({"postgresql", "sqlite"})
_TASK_STATUS_INPUT_MAX_BYTES = 16 * 1024
_TASK_STATUS_RESPONSE_MAX_BYTES = 64 * 1024
_POLL_DIAGNOSTIC_MAX_BYTES = 16 * 1024
_POLL_ATTEMPT_ERROR_MAX_BYTES = 4 * 1024
_POLL_ATTEMPT_MAX_COUNT = 4
_POLL_RESPONSE_MAX_BYTES = 64 * 1024
_CANCELLATION_RESPONSE_MAX_BYTES = 4 * 1024

_TASK_STATUS_BY_STATE = {
    TaskState.QUEUED: TaskResultStatus.READY.value,
    TaskState.RUNNING: TaskResultStatus.RUNNING.value,
    TaskState.SUCCEEDED: TaskResultStatus.SUCCESSFUL.value,
    TaskState.FAILED: TaskResultStatus.FAILED.value,
    TaskState.CANCELLED: TaskResultStatus.FAILED.value,
    TaskState.CANCELLING: TaskResultStatus.RUNNING.value,
    TaskState.LOST: TaskResultStatus.FAILED.value,
    TaskState.EXPIRED: TaskResultStatus.FAILED.value,
}


def _reject_non_finite_json_constant(_value: str) -> float:
    """Reject Python's non-standard NaN and infinity JSON extensions."""
    raise ValueError("non-finite JSON constants are not supported")


def _finite_json_float(value: str) -> float:
    """Parse a JSON number only when its binary float remains finite."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number exceeds the finite float range")
    return parsed


def _strict_json_loads(value: Any) -> Any:
    """Decode interoperable JSON without accepting non-finite numbers."""
    return json.loads(
        value,
        parse_constant=_reject_non_finite_json_constant,
        parse_float=_finite_json_float,
    )


_EXECUTION_LIST_VALUE_FIELDS = (
    "id",
    "task_id",
    "callable_path",
    "queue_name",
    "state",
    "attempt_number",
    "execution_generation",
    "workflow_run_id",
    "created_at",
    "started_at",
    "finished_at",
    "runtime_env_profile",
    "runtime_env_hash",
    "queue_timeout_seconds",
    "queue_deadline_at",
    "_list_result_data",
    "_list_result_data_bytes",
    "_list_error_message",
    "_list_error_message_bytes",
)

_EXECUTION_DETAIL_VALUE_FIELDS = (
    "id",
    "task_id",
    "callable_path",
    "queue_name",
    "state",
    "attempt_number",
    "execution_generation",
    "workflow_run_id",
    "created_at",
    "started_at",
    "finished_at",
    "runtime_env_profile",
    "runtime_env_hash",
    "queue_timeout_seconds",
    "queue_deadline_at",
    "_detail_result_data",
    "_detail_result_data_bytes",
    "_detail_error_message",
    "_detail_error_message_bytes",
    "_detail_has_result_reference",
)

_TASK_STATUS_VALUE_FIELDS = (
    "task_id",
    "state",
    "attempt_number",
    "execution_generation",
    "created_at",
    "started_at",
    "finished_at",
    "_status_args_json",
    "_status_kwargs_json",
    "_status_input_bytes",
    "_status_has_input_reference",
)

_POLL_EXECUTION_VALUE_FIELDS = (
    "id",
    "task_id",
    "state",
    "attempt_number",
    "execution_generation",
    "workflow_run_id",
    "runtime_env_profile",
    "runtime_env_hash",
    "created_at",
    "started_at",
    "finished_at",
    "_poll_result_data",
    "_poll_result_data_bytes",
    "_poll_error_message",
    "_poll_error_message_bytes",
    "_poll_has_result_reference",
)


class _DatabaseByteLength(Func):
    """Return stored text bytes without SQLite's embedded-NUL text shortcut."""

    function = "OCTET_LENGTH"
    output_field = IntegerField()

    def as_sqlite(self, compiler: Any, connection: Any, **extra_context: Any) -> Any:
        return self.as_sql(
            compiler,
            connection,
            template="LENGTH(CAST(%(expressions)s AS BLOB))",
            **extra_context,
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


class TaskStatusSchema(Schema):
    """Bounded monitoring projection for one durable task."""

    task_id: str
    status: str
    state: str
    attempt_number: int
    execution_generation: int
    enqueued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    args: list | None
    kwargs: dict | None
    input_omission_reason: (
        Literal[
            "external_input_not_loaded",
            "stored_input_exceeds_status_limit",
            "malformed_inline_input",
            "encoded_response_limit",
        ]
        | None
    )
    input_max_bytes: Literal[16384]
    response_max_bytes: Literal[65536]

    @field_validator("args", "kwargs", mode="before")
    @classmethod
    def redact_inputs(cls, value):
        return redact_value(value) if value is not None else None


class TaskStatusNotFoundSchema(Schema):
    """Fixed response for a missing bounded task-status row."""

    code: Literal["task_status_not_found"]
    message: Literal["Task status was not found."]
    response_max_bytes: Literal[65536]


def _json_value_requires_fixed_redaction(value: Any) -> bool:
    """Detect excessive JSON depth or indeterminate depth within fixed work."""
    pending: list[tuple[Iterator[Any], int]] = [(iter((value,)), 0)]
    inspected_items = 0
    while pending:
        iterator, depth = pending[-1]
        try:
            item = next(iterator)
        except StopIteration:
            pending.pop()
            continue
        if inspected_items >= _EXECUTION_RESULT_JSON_DEPTH_SCAN_MAX_ITEMS:
            return True
        inspected_items += 1
        if depth > _EXECUTION_RESULT_JSON_MAX_DEPTH:
            return True
        if isinstance(item, dict):
            pending.append((iter(item.values()), depth + 1))
        elif isinstance(item, list):
            pending.append((iter(item), depth + 1))
    return False


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
    queue_timeout_seconds: int | None
    queue_deadline_at: datetime | None

    @field_validator("result_data", mode="before")
    @classmethod
    def redact_json_fields(cls, value):
        """Decode valid JSON before redaction and hide undecodable source text."""
        if value is None:
            return None
        try:
            parsed = json.loads(value)
            if _json_value_requires_fixed_redaction(parsed):
                return REDACTED
            return safe_json_dumps(parsed)
        except (RecursionError, TypeError, ValueError, UnicodeError):
            return REDACTED

    @field_validator("error_message", mode="before")
    @classmethod
    def redact_error(cls, value):
        return redact_text(value) if value is not None else None


class TaskExecutionListItemSchema(TaskExecutionSchema):
    """Execution summary with explicit list-only diagnostic omissions."""

    result_data_omission_reason: Literal["stored_value_exceeds_list_limit"] | None
    error_message_omission_reason: Literal["stored_value_exceeds_list_limit"] | None


class TaskExecutionDetailSchema(TaskExecutionSchema):
    """Exact execution projection with explicit diagnostic read bounds."""

    result_data_omission_reason: (
        Literal[
            "stored_value_exceeds_detail_limit",
            "external_result_not_loaded",
            "response_size_limit",
        ]
        | None
    )
    error_message_omission_reason: (
        Literal["stored_value_exceeds_detail_limit", "response_size_limit"] | None
    )
    diagnostic_max_bytes: Literal[65536]
    response_max_bytes: Literal[262144]


class TaskExecutionDetailUnavailableSchema(Schema):
    """Fixed response when even bounded detail metadata cannot be rendered safely."""

    code: Literal["execution_detail_response_limit"]
    message: Literal["Execution detail exceeds its fixed response limit."]
    response_max_bytes: Literal[262144]


class RetryExecutionOutcomeSchema(Schema):
    """Bounded outcome of requesting one manual retry."""

    code: TaskRetryRequestStatus
    message: str
    execution_id: int
    state: str | None
    attempt_number: int | None
    execution_generation: int | None
    next_action: str


class RetryExecutionRuntimeEnvConflictSchema(Schema):
    """Fixed redaction-safe RuntimeEnv retry conflict."""

    detail: Literal["Persisted RuntimeEnv snapshot failed validation"]


_RETRY_EXECUTION_RESPONSES = {
    202: RetryExecutionOutcomeSchema,
    404: RetryExecutionOutcomeSchema,
    409: RetryExecutionOutcomeSchema | RetryExecutionRuntimeEnvConflictSchema,
}


class CancellationExecutionOutcomeSchema(Schema):
    """Bounded outcome of requesting one execution cancellation."""

    code: TaskCancellationRequestStatus
    message: str
    execution_id: int
    state: str | None
    attempt_number: int | None
    execution_generation: int | None
    next_action: str
    response_max_bytes: Literal[4096]


_CANCELLATION_EXECUTION_RESPONSES = {
    202: CancellationExecutionOutcomeSchema,
    404: CancellationExecutionOutcomeSchema,
    409: CancellationExecutionOutcomeSchema,
}
# Django Ninja 1.5 uses the tuple contract; 1.6 adds Status to avoid its deprecation.
_NINJA_STATUS = getattr(ninja_responses, "Status", None)


def _ninja_status(status_code: int, value: Any) -> object:
    if _NINJA_STATUS is None:
        return status_code, value
    return _NINJA_STATUS(status_code, value)


def _retry_execution_outcome(
    result: TaskRetryRequestResult,
    *,
    status_code: int,
) -> object:
    messages = {
        TaskRetryRequestStatus.ACCEPTED: "A new task attempt was queued.",
        TaskRetryRequestStatus.NOT_RETRYABLE: (
            "The execution is not retryable from its current state."
        ),
        TaskRetryRequestStatus.NOT_FOUND: "The execution was not found.",
        TaskRetryRequestStatus.STALE_ATTEMPT: (
            "The execution attempt changed before the retry could be applied."
        ),
        TaskRetryRequestStatus.STALE_GENERATION: (
            "The execution generation changed before the retry could be applied."
        ),
        TaskRetryRequestStatus.STALE_WORKFLOW_IDENTITY: (
            "The workflow identity changed before the retry could be applied."
        ),
    }
    if result.status is TaskRetryRequestStatus.ACCEPTED:
        next_action = "Poll or inspect the newly queued attempt."
    elif (
        result.status is TaskRetryRequestStatus.NOT_RETRYABLE
        and result.state == TaskState.SUCCEEDED
    ):
        next_action = (
            "Enqueue a new task under the application's authorization and idempotency "
            "policy; keep this successful execution as completed history."
        )
    elif result.status is TaskRetryRequestStatus.NOT_RETRYABLE:
        next_action = (
            "Refresh the execution and retry only a FAILED, CANCELLED, LOST, or EXPIRED state."
        )
    elif result.status is TaskRetryRequestStatus.NOT_FOUND:
        next_action = "Verify the execution identifier and object authorization."
    else:
        next_action = (
            "Refresh and re-authorize the current attempt before deciding whether to retry."
        )
    payload: dict[str, str | int | None] = {
        "code": result.status.value,
        "message": messages[result.status],
        "execution_id": result.execution_id,
        "state": result.state,
        "attempt_number": result.attempt_number,
        "execution_generation": result.execution_generation,
        "next_action": next_action,
    }
    return _ninja_status(status_code, payload)


def _cancellation_execution_outcome(
    request: Any,
    result: TaskCancellationRequestResult,
    *,
    status_code: int,
) -> HttpResponse:
    """Render one fixed cancellation result without execution diagnostics."""
    messages = {
        TaskCancellationRequestStatus.ACCEPTED: "Cancellation was accepted.",
        TaskCancellationRequestStatus.ALREADY_REQUESTED: (
            "Cancellation was already requested for this execution."
        ),
        TaskCancellationRequestStatus.ALREADY_TERMINAL: (
            "The execution is already terminal and cannot be cancelled."
        ),
        TaskCancellationRequestStatus.COMPLETION_PENDING: (
            "A terminal completion is awaiting durable reconciliation."
        ),
        TaskCancellationRequestStatus.NOT_FOUND: "The execution was not found.",
        TaskCancellationRequestStatus.STALE_ATTEMPT: (
            "The execution attempt changed before cancellation could be applied."
        ),
        TaskCancellationRequestStatus.STALE_GENERATION: (
            "The execution generation changed before cancellation could be applied."
        ),
        TaskCancellationRequestStatus.INVALID_STATE: (
            "The execution is not cancellable from its current state."
        ),
    }
    if result.status is TaskCancellationRequestStatus.ACCEPTED:
        if result.state == TaskState.CANCELLED:
            next_action = "The queued attempt is cancelled; retain its archived history."
        else:
            next_action = "Poll until the worker records a terminal cancellation outcome."
    elif result.status is TaskCancellationRequestStatus.ALREADY_REQUESTED:
        next_action = "Poll the current attempt instead of submitting a duplicate request."
    elif result.status is TaskCancellationRequestStatus.COMPLETION_PENDING:
        next_action = "Poll until reconciliation records the terminal state."
    elif result.status is TaskCancellationRequestStatus.NOT_FOUND:
        next_action = "Verify the execution identifier and object authorization."
    elif result.status in {
        TaskCancellationRequestStatus.STALE_ATTEMPT,
        TaskCancellationRequestStatus.STALE_GENERATION,
    }:
        next_action = "Refresh and re-authorize the current attempt before cancelling."
    else:
        next_action = "Leave the current execution unchanged and inspect its lifecycle state."
    response = CancellationExecutionOutcomeSchema.model_validate(
        {
            "code": result.status,
            "message": messages[result.status],
            "execution_id": result.execution_id,
            "state": result.state,
            "attempt_number": result.attempt_number,
            "execution_generation": result.execution_generation,
            "next_action": next_action,
            "response_max_bytes": _CANCELLATION_RESPONSE_MAX_BYTES,
        }
    )
    encoded = _encode_api_schema_response(request, response, status_code=status_code)
    if len(encoded) > _CANCELLATION_RESPONSE_MAX_BYTES:
        raise ImproperlyConfigured("Cancellation response exceeds its byte bound")
    return _bounded_api_http_response(encoded, status_code=status_code)


class TaskListResponseSchema(Schema):
    """Schema for task list response."""

    tasks: list[TaskExecutionListItemSchema]
    total: int
    queued: int
    running: int
    succeeded: int
    failed: int
    expired: int
    limit: int
    returned_count: int
    has_more: bool
    next_cursor: Annotated[
        str | None,
        Field(max_length=_EXECUTION_LIST_CURSOR_MAX_CHARACTERS),
    ]
    truncated: bool
    truncation_reason: Literal["page_limit", "response_size_limit"] | None
    diagnostic_max_bytes: Literal[4096]
    response_max_bytes: Literal[262144]


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
    expired: int
    cancelled: int
    lost: int


class WorkflowResultSchema(Schema):
    """Bounded status and diagnostics for a Ray-native workflow task."""

    task_id: str
    state: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: dict | None
    result: dict | None
    error: str | None
    result_omission_reason: (
        Literal[
            "external_result_not_loaded",
            "stored_result_exceeds_poll_limit",
            "malformed_inline_result",
            "encoded_response_limit",
        ]
        | None
    )
    error_omission_reason: (
        Literal["stored_error_exceeds_poll_limit", "encoded_response_limit"] | None
    )
    diagnostic_max_bytes: Literal[16384]
    response_max_bytes: Literal[65536]

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
    error_omission_reason: (
        Literal["stored_error_exceeds_attempt_limit", "encoded_response_limit"] | None
    )

    @field_validator("error", mode="before")
    @classmethod
    def redact_attempt_error(cls, value):
        return redact_text(value) if value is not None else None


class WorkflowRecoveryResultSchema(WorkflowResultSchema):
    """Current recovery result plus its three bounded attempt outcomes."""

    attempt_number: int
    runtime_env_profile: str
    attempts: list[WorkflowRecoveryAttemptSchema]
    attempt_error_max_bytes: Literal[4096]


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
    """Bounded task state plus its immutable RuntimeEnv identity."""

    task_id: str
    state: str
    runtime_env_profile: str | None
    runtime_env_hash: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict | None
    error: str | None
    result_omission_reason: (
        Literal[
            "external_result_not_loaded",
            "stored_result_exceeds_poll_limit",
            "malformed_inline_result",
            "encoded_response_limit",
        ]
        | None
    )
    error_omission_reason: (
        Literal["stored_error_exceeds_poll_limit", "encoded_response_limit"] | None
    )
    diagnostic_max_bytes: Literal[16384]
    response_max_bytes: Literal[65536]

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
    2. Open its FAILED execution detail in Admin
    3. Use "Retry task..." and confirm the side-effect warning
    4. Task runs again (and fails again, but you can observe the retry)

    The execution-list "Retry selected tasks..." action provides the same
    confirmation for bulk recovery.
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
    2. Open the execution detail and confirm "Retry task..."
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


@api.get(
    "/tasks/{task_id}",
    response={200: TaskStatusSchema, 404: TaskStatusNotFoundSchema},
    tags=["Tasks"],
)
def get_task(
    request,
    task_id: Annotated[str, Field(max_length=255)],
):
    """Return one bounded monitoring projection without loading a TaskResult."""
    row = _bounded_task_status_row(task_id)
    if row is None:
        return _task_status_not_found_response(request)
    return _bounded_task_status_response(request, row)


# ============================================================================
# Task Management Endpoints (Admin/Monitoring)
# ============================================================================


def _execution_list_filter_fingerprint(
    *,
    state: str | None,
    queue: str | None,
    task_id: str | None,
) -> str:
    """Bind an opaque continuation to the exact normalized filters."""
    encoded = json.dumps(
        {"queue": queue, "state": state, "task_id": task_id},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _encode_execution_list_cursor(
    row: dict[str, Any],
    *,
    filter_fingerprint: str,
) -> str:
    """Sign one keyset position without exposing filter values."""
    created_at = row.get("created_at")
    execution_id = row.get("id")
    if not isinstance(created_at, datetime) or not isinstance(execution_id, int):
        raise ImproperlyConfigured("Execution list row has no cursor position")
    cursor = signing.Signer(salt=_EXECUTION_LIST_CURSOR_SALT).sign_object(
        {
            "created_at": created_at.isoformat(),
            "filter": filter_fingerprint,
            "id": execution_id,
            "version": 1,
        },
        compress=False,
    )
    if len(cursor) > _EXECUTION_LIST_CURSOR_MAX_CHARACTERS:
        raise ImproperlyConfigured("Execution list cursor exceeds its character bound")
    return cursor


def _decode_execution_list_cursor(
    cursor: str,
    *,
    filter_fingerprint: str,
) -> tuple[datetime, int]:
    """Authenticate and validate one filter-bound keyset position."""
    try:
        payload = signing.Signer(salt=_EXECUTION_LIST_CURSOR_SALT).unsign_object(cursor)
        if not isinstance(payload, dict) or set(payload) != {
            "created_at",
            "filter",
            "id",
            "version",
        }:
            raise ValueError
        created_at_text = payload["created_at"]
        stored_fingerprint = payload["filter"]
        execution_id = payload["id"]
        version = payload["version"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != 1
            or not isinstance(created_at_text, str)
            or len(created_at_text) > 64
            or not isinstance(stored_fingerprint, str)
            or len(stored_fingerprint) != 64
            or not isinstance(execution_id, int)
            or isinstance(execution_id, bool)
            or execution_id < 1
            or not secrets.compare_digest(stored_fingerprint, filter_fingerprint)
        ):
            raise ValueError
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
    except (signing.BadSignature, TypeError, ValueError) as error:
        raise HttpError(422, "Invalid execution-list cursor.") from error
    return created_at, execution_id


def _guarded_execution_diagnostics(
    queryset: QuerySet[RayTaskExecution],
    *,
    annotation_prefix: str,
    max_bytes: int,
    surface: str,
) -> QuerySet[RayTaskExecution]:
    """Project inline diagnostics only when their stored byte length is bounded."""
    if connection.vendor not in _EXECUTION_PROJECTION_DATABASE_VENDORS:
        raise ImproperlyConfigured(
            f"The testproject execution {surface} supports only SQLite and PostgreSQL byte guards"
        )
    result_bytes_field = f"_{annotation_prefix}_result_data_bytes"
    error_bytes_field = f"_{annotation_prefix}_error_message_bytes"
    result_value_field = f"_{annotation_prefix}_result_data"
    error_value_field = f"_{annotation_prefix}_error_message"
    return queryset.annotate(
        **{
            result_bytes_field: _DatabaseByteLength("result_data"),
            error_bytes_field: _DatabaseByteLength("error_message"),
        }
    ).annotate(
        **{
            result_value_field: Case(
                When(
                    Q(**{f"{result_bytes_field}__lte": max_bytes}),
                    then=F("result_data"),
                ),
                default=Value(None),
                output_field=TextField(),
            ),
            error_value_field: Case(
                When(
                    Q(**{f"{error_bytes_field}__lte": max_bytes}),
                    then=F("error_message"),
                ),
                default=Value(None),
                output_field=TextField(),
            ),
        }
    )


def _bounded_execution_list_rows(
    queryset: QuerySet[RayTaskExecution],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Load one bounded page without transferring oversized diagnostics."""
    guarded = _guarded_execution_diagnostics(
        queryset,
        annotation_prefix="list",
        max_bytes=_EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES,
        surface="list",
    )
    return list(
        guarded.order_by("-created_at", "-pk").values(*_EXECUTION_LIST_VALUE_FIELDS)[: limit + 1]
    )


def _execution_list_item(row: dict[str, Any]) -> TaskExecutionListItemSchema:
    """Convert one guarded database projection into its redacted public shape."""
    values = dict(row)
    result_bytes = values.pop("_list_result_data_bytes")
    error_bytes = values.pop("_list_error_message_bytes")
    values["result_data"] = values.pop("_list_result_data")
    values["error_message"] = values.pop("_list_error_message")
    values["result_data_omission_reason"] = (
        _EXECUTION_LIST_DIAGNOSTIC_OMISSION_REASON
        if result_bytes is not None and result_bytes > _EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES
        else None
    )
    values["error_message_omission_reason"] = (
        _EXECUTION_LIST_DIAGNOSTIC_OMISSION_REASON
        if error_bytes is not None and error_bytes > _EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES
        else None
    )
    return TaskExecutionListItemSchema.model_validate(values)


def _execution_list_payload(
    *,
    tasks: list[TaskExecutionListItemSchema],
    task_counts: dict[str, int],
    limit: int,
    filter_fingerprint: str,
    continuation_row: dict[str, Any] | None,
    page_limited: bool,
    response_size_limited: bool,
) -> TaskListResponseSchema:
    """Build fixed pagination and truncation metadata for one response."""
    has_more = page_limited or response_size_limited
    if response_size_limited:
        truncation_reason = "response_size_limit"
    elif page_limited:
        truncation_reason = "page_limit"
    else:
        truncation_reason = None
    returned_count = len(tasks)
    if has_more and continuation_row is None:
        raise ImproperlyConfigured("Execution list cannot advance past an omitted first row")
    return TaskListResponseSchema.model_validate(
        {
            "tasks": tasks,
            "total": sum(task_counts.values()),
            "queued": task_counts.get(TaskState.QUEUED, 0),
            "running": task_counts.get(TaskState.RUNNING, 0),
            "succeeded": task_counts.get(TaskState.SUCCEEDED, 0),
            "failed": task_counts.get(TaskState.FAILED, 0),
            "expired": task_counts.get(TaskState.EXPIRED, 0),
            "limit": limit,
            "returned_count": returned_count,
            "has_more": has_more,
            "next_cursor": (
                _encode_execution_list_cursor(
                    continuation_row,
                    filter_fingerprint=filter_fingerprint,
                )
                if continuation_row is not None and has_more
                else None
            ),
            "truncated": has_more,
            "truncation_reason": truncation_reason,
            "diagnostic_max_bytes": _EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES,
            "response_max_bytes": _EXECUTION_LIST_RESPONSE_MAX_BYTES,
        }
    )


def _encode_api_schema_response(
    request: Any,
    response: Schema,
    *,
    status_code: int,
) -> bytes:
    """Render with the configured API encoder so the byte bound is exact."""
    rendered = api.renderer.render(
        request,
        response.model_dump(),
        response_status=status_code,
    )
    if isinstance(rendered, str):
        return rendered.encode(api.renderer.charset)
    return bytes(rendered)


def _bounded_api_http_response(encoded: bytes, *, status_code: int) -> HttpResponse:
    """Return one rendered API response with monitoring-safe cache headers."""
    response = HttpResponse(
        encoded,
        status=status_code,
        content_type=api.get_content_type(),
    )
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _require_projection_database(*, surface: str) -> None:
    if connection.vendor not in _EXECUTION_PROJECTION_DATABASE_VENDORS:
        raise ImproperlyConfigured(
            f"The testproject {surface} supports only SQLite and PostgreSQL byte guards"
        )


def _bounded_task_status_row(task_id: str) -> dict[str, Any] | None:
    """Read one task status through an unlocked, byte-guarded SQL projection."""
    _require_projection_database(surface="task status")
    guarded = (
        RayTaskExecution.objects.annotate(
            _status_args_bytes=_DatabaseByteLength("args_json"),
            _status_kwargs_bytes=_DatabaseByteLength("kwargs_json"),
        )
        .annotate(
            _status_input_bytes=F("_status_args_bytes") + F("_status_kwargs_bytes"),
        )
        .annotate(
            _status_args_json=Case(
                When(
                    (Q(input_reference__isnull=True) | Q(input_reference=""))
                    & Q(_status_input_bytes__lte=_TASK_STATUS_INPUT_MAX_BYTES),
                    then=F("args_json"),
                ),
                default=Value(None),
                output_field=TextField(),
            ),
            _status_kwargs_json=Case(
                When(
                    (Q(input_reference__isnull=True) | Q(input_reference=""))
                    & Q(_status_input_bytes__lte=_TASK_STATUS_INPUT_MAX_BYTES),
                    then=F("kwargs_json"),
                ),
                default=Value(None),
                output_field=TextField(),
            ),
            _status_has_input_reference=Case(
                When(
                    Q(input_reference__isnull=False) & ~Q(input_reference=""),
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )
        .filter(task_id=task_id)
        .values(*_TASK_STATUS_VALUE_FIELDS)
    )
    return guarded.first()


def _task_status_item(row: dict[str, Any]) -> TaskStatusSchema:
    """Decode only a bounded inline input pair into the public status schema."""
    input_bytes = _validated_diagnostic_byte_count(row.get("_status_input_bytes"))
    has_input_reference = row.get("_status_has_input_reference") is True
    args_json = row.get("_status_args_json")
    kwargs_json = row.get("_status_kwargs_json")
    args: list[Any] | None = None
    kwargs: dict[str, Any] | None = None

    if has_input_reference:
        omission_reason = "external_input_not_loaded"
    elif input_bytes is not None and input_bytes > _TASK_STATUS_INPUT_MAX_BYTES:
        omission_reason = "stored_input_exceeds_status_limit"
    else:
        try:
            parsed_args = _strict_json_loads(args_json)
            parsed_kwargs = _strict_json_loads(kwargs_json)
            if not isinstance(parsed_args, list) or not isinstance(parsed_kwargs, dict):
                raise ValueError
        except (RecursionError, TypeError, ValueError, UnicodeError):
            omission_reason = "malformed_inline_input"
        else:
            args = parsed_args
            kwargs = parsed_kwargs
            omission_reason = None

    state = str(row["state"])
    return TaskStatusSchema.model_validate(
        {
            "task_id": row["task_id"],
            "status": _TASK_STATUS_BY_STATE.get(state, TaskResultStatus.READY.value),
            "state": state,
            "attempt_number": row["attempt_number"],
            "execution_generation": row["execution_generation"],
            "enqueued_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "args": args,
            "kwargs": kwargs,
            "input_omission_reason": omission_reason,
            "input_max_bytes": _TASK_STATUS_INPUT_MAX_BYTES,
            "response_max_bytes": _TASK_STATUS_RESPONSE_MAX_BYTES,
        }
    )


def _bounded_task_status_response(request: Any, row: dict[str, Any]) -> HttpResponse:
    """Render one task-status row under its exact aggregate response ceiling."""
    response = _task_status_item(row)
    encoded = _encode_api_schema_response(request, response, status_code=200)
    if len(encoded) > _TASK_STATUS_RESPONSE_MAX_BYTES:
        response = response.model_copy(
            update={
                "args": None,
                "kwargs": None,
                "input_omission_reason": "encoded_response_limit",
            }
        )
        encoded = _encode_api_schema_response(request, response, status_code=200)
    if len(encoded) > _TASK_STATUS_RESPONSE_MAX_BYTES:
        raise ImproperlyConfigured("Task status response metadata exceeds its byte bound")
    return _bounded_api_http_response(encoded, status_code=200)


def _task_status_not_found_response(request: Any) -> HttpResponse:
    """Return a fixed missing-row response independent of stored task data."""
    response = TaskStatusNotFoundSchema.model_validate(
        {
            "code": "task_status_not_found",
            "message": "Task status was not found.",
            "response_max_bytes": _TASK_STATUS_RESPONSE_MAX_BYTES,
        }
    )
    encoded = _encode_api_schema_response(request, response, status_code=404)
    if len(encoded) > _TASK_STATUS_RESPONSE_MAX_BYTES:
        raise ImproperlyConfigured("Task status missing response exceeds its byte bound")
    return _bounded_api_http_response(encoded, status_code=404)


def _encode_execution_list_response(
    request: Any,
    response: TaskListResponseSchema,
) -> bytes:
    return _encode_api_schema_response(request, response, status_code=200)


def _bounded_execution_list_response(
    request: Any,
    *,
    rows: list[dict[str, Any]],
    task_counts: dict[str, int],
    limit: int,
    filter_fingerprint: str,
) -> HttpResponse:
    """Return only complete items that fit the fixed aggregate byte budget."""
    page_limited = len(rows) > limit
    selected_rows = rows[:limit]
    tasks: list[TaskExecutionListItemSchema] = []
    response_size_limited = False

    for index, row in enumerate(selected_rows):
        item = _execution_list_item(row)
        candidate_tasks = [*tasks, item]
        remaining_selected_rows = index + 1 < len(selected_rows)
        candidate = _execution_list_payload(
            tasks=candidate_tasks,
            task_counts=task_counts,
            limit=limit,
            filter_fingerprint=filter_fingerprint,
            continuation_row=row,
            page_limited=page_limited and not remaining_selected_rows,
            response_size_limited=remaining_selected_rows,
        )
        if len(_encode_execution_list_response(request, candidate)) > (
            _EXECUTION_LIST_RESPONSE_MAX_BYTES
        ):
            response_size_limited = True
            break
        tasks.append(item)

    response = _execution_list_payload(
        tasks=tasks,
        task_counts=task_counts,
        limit=limit,
        filter_fingerprint=filter_fingerprint,
        continuation_row=selected_rows[len(tasks) - 1] if tasks else None,
        page_limited=page_limited,
        response_size_limited=response_size_limited,
    )
    encoded = _encode_execution_list_response(request, response)
    if len(encoded) > _EXECUTION_LIST_RESPONSE_MAX_BYTES:
        raise ImproperlyConfigured("Execution list response metadata exceeds its byte bound")
    return HttpResponse(encoded, status=200, content_type=api.get_content_type())


def _bounded_execution_detail_row(execution_id: int) -> dict[str, Any]:
    """Read one exact execution through a single guarded values projection."""
    guarded = _guarded_execution_diagnostics(
        RayTaskExecution.objects.all(),
        annotation_prefix="detail",
        max_bytes=_EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES,
        surface="detail",
    ).annotate(
        _detail_has_result_reference=Case(
            When(
                Q(result_reference__isnull=False) & ~Q(result_reference=""),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        )
    )
    return get_object_or_404(
        guarded.values(*_EXECUTION_DETAIL_VALUE_FIELDS),
        pk=execution_id,
    )


def _validated_diagnostic_byte_count(value: Any) -> int | None:
    """Reject an impossible database length instead of weakening an omission decision."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ImproperlyConfigured("Execution detail database byte length is invalid")
    return value


def _execution_detail_item(row: dict[str, Any]) -> TaskExecutionDetailSchema:
    """Convert one exact guarded row into its redacted public response."""
    values = dict(row)
    result_bytes = _validated_diagnostic_byte_count(values.pop("_detail_result_data_bytes"))
    error_bytes = _validated_diagnostic_byte_count(values.pop("_detail_error_message_bytes"))
    result_data = values.pop("_detail_result_data")
    error_message = values.pop("_detail_error_message")
    has_result_reference = values.pop("_detail_has_result_reference") is True

    values["result_data"] = result_data
    values["error_message"] = error_message
    if result_bytes is not None and result_bytes > _EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES:
        result_omission_reason = _EXECUTION_DETAIL_DIAGNOSTIC_OMISSION_REASON
    elif result_data is None and has_result_reference:
        result_omission_reason = _EXECUTION_DETAIL_EXTERNAL_RESULT_OMISSION_REASON
    else:
        result_omission_reason = None
    values["result_data_omission_reason"] = result_omission_reason
    values["error_message_omission_reason"] = (
        _EXECUTION_DETAIL_DIAGNOSTIC_OMISSION_REASON
        if error_bytes is not None and error_bytes > _EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
        else None
    )
    values["diagnostic_max_bytes"] = _EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
    values["response_max_bytes"] = _EXECUTION_DETAIL_RESPONSE_MAX_BYTES
    return TaskExecutionDetailSchema.model_validate(values)


def _try_encode_execution_detail_response(
    request: Any,
    response: TaskExecutionDetailSchema,
) -> bytes | None:
    """Return encoded detail or a fail-closed marker for unsafe renderer failures."""
    try:
        return _encode_api_schema_response(request, response, status_code=200)
    except Exception:
        # Operational renderer failures fail closed; process control must propagate.
        return None


def _execution_detail_http_response(encoded: bytes, *, status_code: int) -> HttpResponse:
    response = HttpResponse(
        encoded,
        status=status_code,
        content_type=api.get_content_type(),
    )
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _execution_detail_unavailable_response(request: Any) -> HttpResponse:
    """Return a fixed, diagnostic-free failure that is independent of stored values."""
    unavailable = TaskExecutionDetailUnavailableSchema.model_validate(
        {
            "code": "execution_detail_response_limit",
            "message": "Execution detail exceeds its fixed response limit.",
            "response_max_bytes": _EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
        }
    )
    try:
        encoded = _encode_api_schema_response(request, unavailable, status_code=503)
    except Exception:
        # The raw fixed body remains available without swallowing BaseException.
        encoded = b""
    if not encoded or len(encoded) > _EXECUTION_DETAIL_RESPONSE_MAX_BYTES:
        encoded = (
            b'{"code":"execution_detail_response_limit",'
            b'"message":"Execution detail exceeds its fixed response limit.",'
            b'"response_max_bytes":262144}'
        )
    return _execution_detail_http_response(encoded, status_code=503)


def _bounded_execution_detail_response(
    request: Any,
    row: dict[str, Any],
) -> HttpResponse:
    """Render one exact detail while omitting diagnostics that exceed the total cap."""
    response = _execution_detail_item(row)
    encoded = _try_encode_execution_detail_response(request, response)
    if encoded is None:
        return _execution_detail_unavailable_response(request)
    if len(encoded) <= _EXECUTION_DETAIL_RESPONSE_MAX_BYTES:
        return _execution_detail_http_response(encoded, status_code=200)

    for field_name, reason_field_name in (
        ("result_data", "result_data_omission_reason"),
        ("error_message", "error_message_omission_reason"),
    ):
        if getattr(response, field_name) is None:
            continue
        response = response.model_copy(
            update={
                field_name: None,
                reason_field_name: _EXECUTION_DETAIL_RESPONSE_OMISSION_REASON,
            }
        )
        encoded = _try_encode_execution_detail_response(request, response)
        if encoded is None:
            return _execution_detail_unavailable_response(request)
        if len(encoded) <= _EXECUTION_DETAIL_RESPONSE_MAX_BYTES:
            return _execution_detail_http_response(encoded, status_code=200)

    return _execution_detail_unavailable_response(request)


@api.get("/executions", response=TaskListResponseSchema, tags=["Admin"])
def list_executions(
    request,
    state: Annotated[str | None, Field(max_length=20)] = None,
    queue: Annotated[str | None, Field(max_length=100)] = None,
    task_id: Annotated[str | None, Field(max_length=255)] = None,
    limit: Annotated[
        int,
        Field(
            ge=_EXECUTION_LIST_MIN_LIMIT,
            le=_EXECUTION_LIST_MAX_LIMIT,
        ),
    ] = _EXECUTION_LIST_DEFAULT_LIMIT,
    cursor: Annotated[
        str | None,
        Field(max_length=_EXECUTION_LIST_CURSOR_MAX_CHARACTERS),
    ] = None,
):
    """List task executions with optional filtering.

    This provides visibility into the internal execution tracking.
    """
    queryset = _workflow_observability_executions()

    normalized_state = state.upper() if state else None
    normalized_queue = queue if queue else None
    normalized_task_id = task_id if task_id else None
    if normalized_state:
        queryset = queryset.filter(state=normalized_state)
    if normalized_queue:
        queryset = queryset.filter(queue_name=normalized_queue)
    if normalized_task_id:
        queryset = queryset.filter(task_id=normalized_task_id)

    filter_fingerprint = _execution_list_filter_fingerprint(
        state=normalized_state,
        queue=normalized_queue,
        task_id=normalized_task_id,
    )
    if cursor is not None:
        cursor_created_at, cursor_id = _decode_execution_list_cursor(
            cursor,
            filter_fingerprint=filter_fingerprint,
        )
        queryset = queryset.filter(
            Q(created_at__lt=cursor_created_at) | Q(created_at=cursor_created_at, pk__lt=cursor_id)
        )

    rows = _bounded_execution_list_rows(queryset, limit=limit)
    task_counts = _task_state_counts()
    return _bounded_execution_list_response(
        request,
        rows=rows,
        task_counts=task_counts,
        limit=limit,
        filter_fingerprint=filter_fingerprint,
    )


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
        "expired": task_counts.get(TaskState.EXPIRED, 0),
        "cancelled": task_counts.get(TaskState.CANCELLED, 0),
        "lost": task_counts.get(TaskState.LOST, 0),
    }


@api.get(
    "/executions/{execution_id}",
    response={
        200: TaskExecutionDetailSchema,
        503: TaskExecutionDetailUnavailableSchema,
    },
    tags=["Admin"],
)
def get_execution(request, execution_id: int):
    """Get detailed execution record by internal ID."""
    row = _bounded_execution_detail_row(execution_id)
    return _bounded_execution_detail_response(request, row)


@api.post(
    "/executions/{execution_id}/cancel",
    response=_CANCELLATION_EXECUTION_RESPONSES,
    tags=["Admin"],
)
def cancel_execution(request, execution_id: int):
    """Request cancellation and return only the bounded lifecycle outcome."""
    authorized_identity = (
        RayTaskExecution.objects.filter(pk=execution_id)
        .values("pk", "attempt_number", "execution_generation")
        .first()
    )
    if authorized_identity is None:
        outcome = TaskCancellationRequestResult(
            status=TaskCancellationRequestStatus.NOT_FOUND,
            execution_id=execution_id,
            state=None,
            attempt_number=None,
            execution_generation=None,
        )
    else:
        outcome = request_task_cancellation(
            execution_id,
            expected_attempt_number=authorized_identity["attempt_number"],
            expected_execution_generation=authorized_identity["execution_generation"],
        )
    if outcome.accepted:
        status_code = 202
    elif outcome.status is TaskCancellationRequestStatus.NOT_FOUND:
        status_code = 404
    else:
        status_code = 409
    return _cancellation_execution_outcome(request, outcome, status_code=status_code)


@api.post(
    "/executions/{execution_id}/retry",
    response=_RETRY_EXECUTION_RESPONSES,
    tags=["Admin"],
)
def retry_execution(request, execution_id: int):
    """Retry a failed, cancelled, lost, or expired task execution."""
    task = _workflow_observability_executions().filter(pk=execution_id).first()
    if task is None:
        return _retry_execution_outcome(
            TaskRetryRequestResult(
                status=TaskRetryRequestStatus.NOT_FOUND,
                execution_id=execution_id,
                state=None,
                attempt_number=None,
                execution_generation=None,
            ),
            status_code=404,
        )
    try:
        outcome = request_task_retry(
            task.pk,
            expected_attempt_number=task.attempt_number,
            expected_execution_generation=task.execution_generation,
            expected_workflow_identity=(
                str(task.workflow_run_id) if task.workflow_run_id is not None else None,
                task.workflow_plan_fingerprint,
            ),
        )
    except RuntimeEnvSnapshotError:
        raise HttpError(
            409,
            "Persisted RuntimeEnv snapshot failed validation",
        ) from None
    if outcome.accepted:
        status_code = 202
    elif outcome.status is TaskRetryRequestStatus.NOT_FOUND:
        status_code = 404
    else:
        status_code = 409
    return _retry_execution_outcome(outcome, status_code=status_code)


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
def get_cluster_workflow_benchmark(
    request,
    task_id: Annotated[str, Field(max_length=255)],
):
    """Return one bounded polling snapshot for the fan-out example."""
    row = _bounded_poll_execution_row(
        task_id,
        callable_paths=("testproject.apps.cluster_tasks.tasks.workflow_fanout_benchmark",),
    )
    return _bounded_poll_response(
        request,
        schema_type=WorkflowResultSchema,
        payload=_workflow_poll_payload(
            row,
            progress=_bounded_workflow_poll_progress(row),
        ),
    )


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
def get_cluster_complex_workflow(
    request,
    task_id: Annotated[str, Field(max_length=255)],
):
    """Return one bounded polling snapshot for the nested workflow example."""
    row = _bounded_poll_execution_row(
        task_id,
        callable_paths=("testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark",),
    )
    return _bounded_poll_response(
        request,
        schema_type=WorkflowResultSchema,
        payload=_workflow_poll_payload(
            row,
            progress=_bounded_workflow_poll_progress(row),
        ),
    )


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
def get_cluster_workflow_showcase(
    request,
    task_id: Annotated[str, Field(max_length=255)],
):
    """Return a bounded progress summary and the compact result or failure."""
    row = _bounded_poll_execution_row(
        task_id,
        callable_paths=("testproject.apps.cluster_tasks.tasks.order_fulfillment_showcase_task",),
    )
    return _bounded_poll_response(
        request,
        schema_type=WorkflowResultSchema,
        payload=_workflow_poll_payload(
            row,
            progress=_bounded_workflow_poll_progress(row),
        ),
    )


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
def get_cluster_workflow_recovery_showcase(
    request,
    task_id: Annotated[str, Field(max_length=255)],
):
    """Return the current result and the complete bounded attempt sequence."""
    row = _bounded_poll_execution_row(
        task_id,
        callable_paths=(
            "testproject.apps.cluster_tasks.tasks.order_fulfillment_recovery_showcase_task",
        ),
    )
    payload = _workflow_poll_payload(
        row,
        progress=_bounded_workflow_poll_progress(row),
    )
    payload.update(
        attempt_number=row["attempt_number"],
        runtime_env_profile=row["runtime_env_profile"],
        attempts=_bounded_recovery_attempts(row),
        attempt_error_max_bytes=_POLL_ATTEMPT_ERROR_MAX_BYTES,
    )
    return _bounded_poll_response(
        request,
        schema_type=WorkflowRecoveryResultSchema,
        payload=payload,
    )


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


def _bounded_poll_execution_row(
    task_id: str,
    *,
    callable_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Load one polling snapshot without transferring unrelated task payloads."""
    guarded = _guarded_execution_diagnostics(
        RayTaskExecution.objects.filter(
            task_id=task_id,
            callable_path__in=callable_paths,
        ),
        annotation_prefix="poll",
        max_bytes=_POLL_DIAGNOSTIC_MAX_BYTES,
        surface="polling",
    ).annotate(
        _poll_has_result_reference=Case(
            When(
                Q(result_reference__isnull=False) & ~Q(result_reference=""),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        )
    )
    return get_object_or_404(
        guarded.values(*_POLL_EXECUTION_VALUE_FIELDS),
    )


def _poll_result_and_error(
    row: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None, str | None]:
    """Decode only guarded inline poll diagnostics and classify every omission."""
    result_bytes = _validated_diagnostic_byte_count(row.get("_poll_result_data_bytes"))
    error_bytes = _validated_diagnostic_byte_count(row.get("_poll_error_message_bytes"))
    result_data = row.get("_poll_result_data")
    error = row.get("_poll_error_message")
    has_result_reference = row.get("_poll_has_result_reference") is True

    result: dict[str, Any] | None = None
    if result_bytes is not None and result_bytes > _POLL_DIAGNOSTIC_MAX_BYTES:
        result_omission_reason = "stored_result_exceeds_poll_limit"
    elif result_data is not None:
        try:
            parsed_result = _strict_json_loads(result_data)
            if not isinstance(parsed_result, dict):
                raise ValueError
        except (RecursionError, TypeError, ValueError, UnicodeError):
            result_omission_reason = "malformed_inline_result"
        else:
            result = parsed_result
            result_omission_reason = None
    elif has_result_reference:
        result_omission_reason = "external_result_not_loaded"
    else:
        result_omission_reason = None

    error_omission_reason = (
        "stored_error_exceeds_poll_limit"
        if error_bytes is not None and error_bytes > _POLL_DIAGNOSTIC_MAX_BYTES
        else None
    )
    return result, error, result_omission_reason, error_omission_reason


def _bounded_workflow_poll_progress(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return only the package's bounded aggregate workflow summary."""
    execution = RayTaskExecution(pk=row["id"])
    try:
        progress = get_workflow_progress_summary(
            execution,
            authorize=_authorize_example_workflow,
            include_legacy=True,
            infer_current_reporting_policy=False,
            attempt_number=row["attempt_number"],
        )
    except WorkflowProgressReadError:
        return None

    identity = progress.get("run_identity")
    expected_run_id = str(row["workflow_run_id"]) if row["workflow_run_id"] is not None else None
    if isinstance(identity, dict):
        if (
            identity.get("attempt_number") != row["attempt_number"]
            or identity.get("execution_generation") != row["execution_generation"]
            or identity.get("run_id") != expected_run_id
        ):
            return None
    elif expected_run_id is not None:
        return None
    return progress


def _bounded_recovery_attempts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Read only archived attempt identities and guarded error text."""
    _require_projection_database(surface="workflow recovery attempts")
    attempts = (
        TaskAttempt.objects.filter(
            execution_id=row["id"],
            attempt_number__lte=row["attempt_number"],
        )
        .annotate(_poll_attempt_error_bytes=_DatabaseByteLength("error_message"))
        .annotate(
            _poll_attempt_error=Case(
                When(
                    _poll_attempt_error_bytes__lte=_POLL_ATTEMPT_ERROR_MAX_BYTES,
                    then=F("error_message"),
                ),
                default=Value(None),
                output_field=TextField(),
            )
        )
        .order_by("attempt_number")
        .values(
            "attempt_number",
            "state",
            "_poll_attempt_error",
            "_poll_attempt_error_bytes",
        )[:_POLL_ATTEMPT_MAX_COUNT]
    )
    rows: list[dict[str, Any]] = []
    for attempt in attempts:
        error_bytes = _validated_diagnostic_byte_count(attempt["_poll_attempt_error_bytes"])
        rows.append(
            {
                "attempt_number": attempt["attempt_number"],
                "state": attempt["state"],
                "error": attempt["_poll_attempt_error"],
                "error_omission_reason": (
                    "stored_error_exceeds_attempt_limit"
                    if error_bytes is not None and error_bytes > _POLL_ATTEMPT_ERROR_MAX_BYTES
                    else None
                ),
            }
        )
    return rows


def _bounded_poll_response(
    request: Any,
    *,
    schema_type: Any,
    payload: dict[str, Any],
) -> HttpResponse:
    """Render a polling payload while dropping only diagnostics that exceed the cap."""
    mutable = dict(payload)

    def render() -> bytes:
        response = schema_type.model_validate(mutable)
        return _encode_api_schema_response(request, response, status_code=200)

    encoded = render()
    for value_field, reason_field in (
        ("result", "result_omission_reason"),
        ("error", "error_omission_reason"),
    ):
        if len(encoded) <= _POLL_RESPONSE_MAX_BYTES:
            break
        if mutable.get(value_field) is None:
            continue
        mutable[value_field] = None
        mutable[reason_field] = "encoded_response_limit"
        encoded = render()

    if len(encoded) > _POLL_RESPONSE_MAX_BYTES:
        attempts = mutable.get("attempts")
        if isinstance(attempts, list):
            for attempt in attempts:
                if len(encoded) <= _POLL_RESPONSE_MAX_BYTES:
                    break
                if isinstance(attempt, dict) and attempt.get("error") is not None:
                    attempt["error"] = None
                    attempt["error_omission_reason"] = "encoded_response_limit"
                    encoded = render()

    if len(encoded) > _POLL_RESPONSE_MAX_BYTES:
        raise ImproperlyConfigured("Polling response metadata exceeds its byte bound")
    return _bounded_api_http_response(encoded, status_code=200)


def _workflow_poll_payload(
    row: dict[str, Any],
    *,
    progress: dict[str, Any] | None,
) -> dict[str, Any]:
    result, error, result_reason, error_reason = _poll_result_and_error(row)
    return {
        "task_id": row["task_id"],
        "state": row["state"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "progress": progress,
        "result": result,
        "error": error,
        "result_omission_reason": result_reason,
        "error_omission_reason": error_reason,
        "diagnostic_max_bytes": _POLL_DIAGNOSTIC_MAX_BYTES,
        "response_max_bytes": _POLL_RESPONSE_MAX_BYTES,
    }


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
def get_cluster_runtime_env_result(
    request,
    task_id: Annotated[str, Field(max_length=255)],
):
    """Return a bounded probe result with the durable environment identity."""
    row = _bounded_poll_execution_row(
        task_id,
        callable_paths=(
            "testproject.apps.cluster_tasks.tasks.runtime_env_probe",
            "testproject.apps.cluster_tasks.tasks.runtime_env_benchmark",
        ),
    )
    result, error, result_reason, error_reason = _poll_result_and_error(row)
    return _bounded_poll_response(
        request,
        schema_type=RuntimeEnvResultSchema,
        payload={
            "task_id": row["task_id"],
            "state": row["state"],
            "runtime_env_profile": row["runtime_env_profile"],
            "runtime_env_hash": row["runtime_env_hash"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "result": result,
            "error": error,
            "result_omission_reason": result_reason,
            "error_omission_reason": error_reason,
            "diagnostic_max_bytes": _POLL_DIAGNOSTIC_MAX_BYTES,
            "response_max_bytes": _POLL_RESPONSE_MAX_BYTES,
        },
    )


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
