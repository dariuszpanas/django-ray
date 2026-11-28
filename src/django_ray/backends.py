"""Django 6 Task Backend implementation for Ray execution.

This module provides a Django 6 Tasks-compatible backend that executes
tasks on Ray clusters. It implements the `BaseTaskBackend` interface
to integrate with Django's native task framework.

Usage in Django settings:
    TASKS = {
        "default": {
            "BACKEND": "django_ray.backends.RayTaskBackend",
            "QUEUES": ["default", "high-priority"],
            "OPTIONS": {
                "RAY_ADDRESS": "auto",  # or "ray://host:port" for cluster
            },
        }
    }

Then use Django's standard task API:
    from django.tasks import task

    @task
    def my_task(arg1, arg2):
        return result

    # Enqueue for execution
    result = my_task.enqueue(arg1, arg2)
"""

from __future__ import annotations

import builtins
import json
import re
import sys
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connection, connections, router, transaction
from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TASK_REFRESH_ATTRS, TaskContext, TaskError
from django.tasks.base import Task as DjangoTask
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist
from django.tasks.signals import task_enqueued

from django_ray._result_tasks import (
    _project_result_task,
    _register_validated_task,
    _require_executable_task,
)
from django_ray._version import __version__ as django_ray_version
from django_ray.conf.defaults import QUEUE_TIMEOUT_SECONDS_MAX
from django_ray.conf.settings import get_settings
from django_ray.decorators import get_ray_task_contract
from django_ray.execution_protocol import (
    EXECUTION_METADATA_SCHEMA_VERSION,
    EXECUTION_PROTOCOL_VERSION,
)
from django_ray.input_storage import (
    EXTERNAL_INPUT_PLACEHOLDER,
    InputPayloadError,
    load_task_input,
    prepare_task_input,
    register_task_input,
)
from django_ray.logging import get_backend_logger
from django_ray.models import RayTaskExecution, TaskInputPayload, TaskState
from django_ray.redaction import redact_text
from django_ray.runtime.runtime_env import (
    resolve_runtime_env_profile,
    runtime_env_for_storage,
)
from django_ray.tracing import LifecycleTraceEvent, emit_task_lifecycle_span

if TYPE_CHECKING:
    from django.dispatch import Signal
    from django.tasks.base import Task

_OVERSIZE_REF_RE = re.compile(
    r"^oversize://sha256/(?P<digest>[0-9a-f]{64})\?bytes=(?P<bytes>0|[1-9][0-9]*)$"
)
_GET_RESULT_DEFERRED_FIELDS: tuple[str, ...] = (
    "runtime_env_json",
    "progress_data",
    "workflow_progress_summary_json",
    "workflow_plan_json",
    "workflow_plan_selection",
    "completion_data",
)


class ResultUnavailableError(ValueError):
    """Raised when a succeeded task's payload cannot be retrieved.

    Examples include digest-only ``oversize://`` stubs produced under
    ``RESULT_STORAGE_BACKEND='digest'``, external result-storage retrieval
    failures, integrity mismatches, and undecodable stored payloads.
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        digest: str | None = None,
        size_bytes: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.digest = digest
        self.size_bytes = size_bytes
        self.reason = reason


class InputPayloadUnavailableError(ResultUnavailableError, InputPayloadError):
    """Raised when a task result's durable input payload cannot be loaded."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RayTaskResult(TaskResult):
    """Django 6 TaskResult subclass that fails loud on unavailable payloads.

    ``get_result()`` always returns a valid ``RayTaskResult`` (keeping admin
    views and status pollers safe), while accessing ``.return_value`` raises
    :class:`ResultUnavailableError` instead of silently returning ``None`` when
    a result is a digest-only stub or failed to load/decode. Similarly,
    accessing ``.args`` or ``.kwargs`` raises :class:`InputPayloadUnavailableError`
    instead of silently fabricating empty inputs when input loading failed.
    """

    _result_unavailable_error: ResultUnavailableError | None = field(
        init=False, default=None, repr=False, compare=False
    )
    _input_unavailable_error: InputPayloadUnavailableError | None = field(
        init=False, default=None, repr=False, compare=False
    )
    _pending_input_spec: tuple[str, str, str, str] | None = field(
        init=False, default=None, repr=False, compare=False
    )

    def _resolve_pending_input(self) -> None:
        """Lazily load external task inputs on first ``.args`` or ``.kwargs`` access."""
        pending = object.__getattribute__(self, "_pending_input_spec")
        if pending is None:
            return
        object.__setattr__(self, "_pending_input_spec", None)
        task_id, args_json, kwargs_json, input_reference = pending
        try:
            resolved_args, resolved_kwargs = load_task_input(
                args_json=args_json,
                kwargs_json=kwargs_json,
                input_reference=input_reference,
            )
        except InputPayloadError as error:
            safe_error = redact_text(str(error))
            if input_reference and input_reference in safe_error:
                safe_error = "durable task input could not be loaded"
            logger.warning(
                "Failed to load durable task input",
                extra={
                    "task_id": task_id,
                    "input_external": True,
                    "error": safe_error,
                },
            )
            object.__setattr__(
                self,
                "_input_unavailable_error",
                InputPayloadUnavailableError(
                    f"Task input for {task_id} is unavailable: {safe_error}.",
                    task_id=task_id,
                    reason="input_load_error",
                ),
            )
            return
        object.__setattr__(self, "args", resolved_args)
        object.__setattr__(self, "kwargs", resolved_kwargs)

    def __getattribute__(self, name: str) -> Any:
        if name in ("args", "kwargs"):
            pending = object.__getattribute__(self, "_pending_input_spec")
            if pending is not None:
                object.__getattribute__(self, "_resolve_pending_input")()
            input_error = object.__getattribute__(self, "_input_unavailable_error")
            if input_error is not None:
                raise input_error
        return object.__getattribute__(self, name)

    def __copy__(self) -> RayTaskResult:
        copied = type(self).__new__(type(self))
        for cls in type(self).__mro__:
            slots = getattr(cls, "__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                try:
                    value = object.__getattribute__(self, slot)
                except AttributeError:
                    continue
                object.__setattr__(copied, slot, value)
        return copied

    def __repr__(self) -> str:
        input_error = object.__getattribute__(self, "_input_unavailable_error")
        pending_input = object.__getattribute__(self, "_pending_input_spec")
        if input_error is None and pending_input is None:
            return super(RayTaskResult, self).__repr__()
        placeholder = "<unavailable>" if input_error is not None else "<external>"
        return (
            f"{type(self).__name__}("
            f"task={object.__getattribute__(self, 'task')!r}, "
            f"id={object.__getattribute__(self, 'id')!r}, "
            f"status={object.__getattribute__(self, 'status')!r}, "
            f"enqueued_at={object.__getattribute__(self, 'enqueued_at')!r}, "
            f"started_at={object.__getattribute__(self, 'started_at')!r}, "
            f"finished_at={object.__getattribute__(self, 'finished_at')!r}, "
            f"last_attempted_at={object.__getattribute__(self, 'last_attempted_at')!r}, "
            f"args={placeholder}, "
            f"kwargs={placeholder}, "
            f"backend={object.__getattribute__(self, 'backend')!r}, "
            f"errors={object.__getattribute__(self, 'errors')!r}, "
            f"worker_ids={object.__getattribute__(self, 'worker_ids')!r})"
        )

    @property
    def return_value(self) -> Any:
        """Return the task's return value or raise ResultUnavailableError."""
        if self.status == TaskResultStatus.SUCCESSFUL:
            if self._result_unavailable_error is not None:
                raise self._result_unavailable_error
            return self._return_value
        return super(RayTaskResult, self).return_value

    def _apply_refreshed(self, refreshed: TaskResult) -> None:
        for attr in TASK_REFRESH_ATTRS:
            object.__setattr__(self, attr, object.__getattribute__(refreshed, attr))
        object.__setattr__(self, "args", object.__getattribute__(refreshed, "args"))
        object.__setattr__(self, "kwargs", object.__getattribute__(refreshed, "kwargs"))
        object.__setattr__(
            self,
            "_result_unavailable_error",
            getattr(refreshed, "_result_unavailable_error", None),
        )
        object.__setattr__(
            self,
            "_input_unavailable_error",
            getattr(refreshed, "_input_unavailable_error", None),
        )
        object.__setattr__(
            self,
            "_pending_input_spec",
            getattr(refreshed, "_pending_input_spec", None),
        )

    def refresh(self) -> None:
        """Reload the cached task data from the task store."""
        refreshed_task = self.task.get_backend().get_result(self.id)
        self._apply_refreshed(refreshed_task)

    async def arefresh(self) -> None:
        """Reload the cached task data asynchronously from the task store."""
        refreshed_task = await self.task.get_backend().aget_result(self.id)
        self._apply_refreshed(refreshed_task)


def _bounded_redacted_task_diagnostic(value: str) -> str:
    """Return the ordinary, fail-closed TaskResult diagnostic projection."""
    return redact_text(value)


def _resolve_exception_class_path(
    traceback_text: str | None,
    *,
    default: str = "builtins.Exception",
) -> str:
    """Resolve a built-in or already-loaded exception class path without importing.

    Preserves custom exception class paths that are already loaded in
    ``sys.modules`` while never dynamically importing modules or invoking
    module ``__getattr__`` hooks from stored diagnostics.
    """
    if not traceback_text:
        return default
    lines = traceback_text.strip().splitlines()
    if not lines:
        return default
    last_line = lines[-1].strip()
    candidate, _separator, _message = last_line.partition(":")
    name = candidate.strip()
    if not name or len(name) > 256:
        return default

    if "." not in name:
        if not name.isidentifier():
            return default
        exception_type = getattr(builtins, name, None)
        if (
            isinstance(exception_type, type)
            and issubclass(exception_type, BaseException)
            and exception_type.__module__ == "builtins"
        ):
            return f"builtins.{name}"
        return default

    parts = name.split(".")
    if not all(part.isidentifier() for part in parts):
        return default

    for split_idx in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split_idx])
        module = sys.modules.get(module_name)
        if module is None:
            continue
        current: object = module
        for attr in parts[split_idx:]:
            namespace = getattr(current, "__dict__", None)
            if namespace is None or attr not in namespace:
                current = None
                break
            current = namespace[attr]
        if isinstance(current, type) and issubclass(current, BaseException):
            return f"{current.__module__}.{current.__qualname__}"

    return default


def _builtin_exception_class_path(traceback_text: str | None) -> str:
    """Backward-compatible alias resolving loaded exception paths import-free."""
    return _resolve_exception_class_path(traceback_text)


def _build_worker_ids(
    *,
    state: str,
    attempt_number: int,
    claimed_by_worker: str | None,
    started_at: datetime | None,
) -> list[str]:
    """Build worker_ids so Django 6 ``TaskResult.attempts`` matches executions."""
    normalized_attempt = max(1, int(attempt_number))
    if state == TaskState.QUEUED:
        attempt_count = normalized_attempt - 1
    elif (
        state in (TaskState.EXPIRED, TaskState.CANCELLED)
        and started_at is None
        and not claimed_by_worker
    ):
        attempt_count = normalized_attempt - 1
    elif state in (
        TaskState.RUNNING,
        TaskState.CANCELLING,
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.LOST,
        TaskState.CANCELLED,
    ):
        attempt_count = normalized_attempt
    else:
        attempt_count = 0

    if attempt_count <= 0:
        return []
    prior = [f"attempt-{index}" for index in range(1, attempt_count)]
    if claimed_by_worker:
        return [*prior, claimed_by_worker]
    return [*prior, f"attempt-{attempt_count}"]


def _build_task_errors(
    *,
    state: str,
    error_message: str | None,
    error_traceback: str | None,
    cancellation_status: str | None = None,
    cancellation_error: str | None = None,
) -> list[TaskError]:
    """Construct redacted Django 6 TaskError entries for terminal failures."""
    if state in (TaskState.FAILED, TaskState.LOST, TaskState.EXPIRED):
        if not error_message:
            return []
        redacted_message = _bounded_redacted_task_diagnostic(error_message)
        redacted_traceback = (
            _bounded_redacted_task_diagnostic(error_traceback) if error_traceback else None
        )
        exception_class_path = _resolve_exception_class_path(
            redacted_traceback,
            default="builtins.Exception",
        )
        return [
            TaskError(
                exception_class_path=exception_class_path,
                traceback=redacted_traceback or redacted_message,
            )
        ]

    if state == TaskState.CANCELLED:
        raw_diagnostic = (
            error_message
            or cancellation_error
            or (
                f"Task was cancelled ({cancellation_status})"
                if cancellation_status
                else "Task was cancelled"
            )
        )
        redacted_message = _bounded_redacted_task_diagnostic(raw_diagnostic)
        redacted_traceback = (
            _bounded_redacted_task_diagnostic(error_traceback) if error_traceback else None
        )
        exception_class_path = _resolve_exception_class_path(
            redacted_traceback,
            default="asyncio.CancelledError",
        )
        return [
            TaskError(
                exception_class_path=exception_class_path,
                traceback=redacted_traceback or redacted_message,
            )
        ]

    return []


def _resolve_backend_alias_for_queue(queue_name: str) -> str:
    """Resolve the configured RayTaskBackend alias owning ``queue_name``."""
    tasks_cfg = getattr(django_settings, "TASKS", None)
    if isinstance(tasks_cfg, Mapping):
        fallback_alias: str | None = None
        for alias, backend_cfg in tasks_cfg.items():
            if not isinstance(alias, str) or not isinstance(backend_cfg, Mapping):
                continue
            backend_path = str(backend_cfg.get("BACKEND", ""))
            if "RayTaskBackend" not in backend_path and not backend_path.startswith("django_ray."):
                continue
            if fallback_alias is None:
                fallback_alias = alias
            queues = backend_cfg.get("QUEUES")
            if queues is None or queue_name in queues:
                return alias
        if fallback_alias is not None:
            return fallback_alias
    return "default"


def _emit_task_signal(
    signal: Signal,
    *,
    sender: type[Any],
    task_result: TaskResult,
    event_name: str,
) -> None:
    """Emit a Django 6 task lifecycle signal with full receiver isolation."""
    try:
        responses = signal.send_robust(sender=sender, task_result=task_result)
    except Exception as error:
        logger.warning(
            "Task lifecycle signal dispatch failed",
            extra={
                "event": event_name,
                "task_id": getattr(task_result, "id", None),
                "error": redact_text(str(error)),
            },
        )
        return

    for receiver_fn, response in responses:
        if isinstance(response, Exception):
            logger.warning(
                "Task lifecycle signal receiver raised an exception",
                extra={
                    "event": event_name,
                    "task_id": getattr(task_result, "id", None),
                    "receiver": getattr(receiver_fn, "__qualname__", repr(receiver_fn)),
                    "error_type": type(response).__name__,
                    "error": redact_text(str(response)),
                },
            )


def _safe_emit_lifecycle_span(event: LifecycleTraceEvent, execution: Any) -> None:
    """Invoke optional tracing without ever impacting task state transitions."""
    try:
        emit_task_lifecycle_span(event, execution)
    except Exception:
        return


def _build_signal_task_result(
    execution_snapshot: Mapping[str, Any],
    *,
    backend_alias: str | None = None,
) -> RayTaskResult:
    """Construct a ``RayTaskResult`` from an in-memory field mapping.

    Strictly avoids deferred ORM queries and external storage I/O so lifecycle
    signals remain fast, safe inside transaction hooks, and non-blocking.
    """
    task_id = str(execution_snapshot.get("task_id") or execution_snapshot.get("id") or "")
    callable_path = str(execution_snapshot.get("callable_path") or "")
    queue_name = str(execution_snapshot.get("queue_name") or "default")
    priority = int(execution_snapshot.get("priority") or 0)
    run_after = execution_snapshot.get("run_after")
    state = str(execution_snapshot.get("state") or TaskState.QUEUED)
    alias = backend_alias or _resolve_backend_alias_for_queue(queue_name)

    task = _project_result_task(
        alias=alias,
        callable_path=callable_path,
        priority=priority,
        queue_name=queue_name,
        run_after=run_after,
    )

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    signal_input_error: InputPayloadUnavailableError | None = None
    args_json = execution_snapshot.get("args_json")
    kwargs_json = execution_snapshot.get("kwargs_json")
    input_reference = execution_snapshot.get("input_reference")
    if input_reference is not None:
        signal_input_error = InputPayloadUnavailableError(
            f"Task input for {task_id} is stored externally and deferred in lifecycle "
            f"signals; call backend.get_result({task_id!r}) to retrieve it.",
            task_id=task_id,
            reason="external_input_deferred_in_signal",
        )
    elif (
        isinstance(args_json, str)
        and isinstance(kwargs_json, str)
        and args_json != EXTERNAL_INPUT_PLACEHOLDER
        and kwargs_json != EXTERNAL_INPUT_PLACEHOLDER
    ):
        try:
            decoded_args = json.loads(args_json)
            decoded_kwargs = json.loads(kwargs_json)
            if isinstance(decoded_args, list) and isinstance(decoded_kwargs, dict):
                args = decoded_args
                kwargs = decoded_kwargs
        except (json.JSONDecodeError, TypeError):
            pass

    errors = _build_task_errors(
        state=state,
        error_message=execution_snapshot.get("error_message"),
        error_traceback=execution_snapshot.get("error_traceback"),
        cancellation_status=execution_snapshot.get("cancellation_status"),
        cancellation_error=execution_snapshot.get("cancellation_error"),
    )
    worker_ids = _build_worker_ids(
        state=state,
        attempt_number=int(execution_snapshot.get("attempt_number") or 1),
        claimed_by_worker=execution_snapshot.get("claimed_by_worker"),
        started_at=execution_snapshot.get("started_at"),
    )
    status = STATE_TO_STATUS.get(state, TaskResultStatus.READY)
    result = RayTaskResult(
        task=task,
        id=task_id,
        status=status,
        enqueued_at=execution_snapshot.get("created_at"),
        started_at=execution_snapshot.get("started_at"),
        finished_at=execution_snapshot.get("finished_at"),
        last_attempted_at=execution_snapshot.get("started_at"),
        args=args,
        kwargs=kwargs,
        backend=alias,
        errors=errors,
        worker_ids=worker_ids,
    )
    if signal_input_error is not None:
        object.__setattr__(result, "_input_unavailable_error", signal_input_error)

    if state == TaskState.SUCCEEDED:
        result_data = execution_snapshot.get("result_data")
        result_reference = execution_snapshot.get("result_reference")
        if result_data:
            try:
                object.__setattr__(result, "_return_value", json.loads(str(result_data)))
            except (json.JSONDecodeError, TypeError):
                object.__setattr__(
                    result,
                    "_result_unavailable_error",
                    ResultUnavailableError(
                        f"Task result for {task_id} is unavailable: stored result payload "
                        "could not be decoded as JSON.",
                        task_id=task_id,
                        reason="invalid_json",
                    ),
                )
        elif result_reference:
            ref_str = str(result_reference)
            oversize_match = _OVERSIZE_REF_RE.fullmatch(ref_str)
            if oversize_match is not None:
                digest = oversize_match.group("digest")
                size_bytes = int(oversize_match.group("bytes"))
                object.__setattr__(
                    result,
                    "_result_unavailable_error",
                    ResultUnavailableError(
                        f"Task result for {task_id} is unavailable: result ({size_bytes} bytes, "
                        f"sha256={digest}) exceeded MAX_RESULT_SIZE_BYTES and was stored as a "
                        "digest-only stub under RESULT_STORAGE_BACKEND='digest'. Configure "
                        "RESULT_STORAGE_BACKEND to 'filesystem', 's3', or 'gcs' for retrievable "
                        "oversized results.",
                        task_id=task_id,
                        digest=digest,
                        size_bytes=size_bytes,
                        reason="digest_stub",
                    ),
                )
            else:
                object.__setattr__(
                    result,
                    "_result_unavailable_error",
                    ResultUnavailableError(
                        f"Task result for {task_id} is stored externally and deferred in "
                        f"lifecycle signals; call backend.get_result({task_id!r}) to retrieve it.",
                        task_id=task_id,
                        reason="external_result_deferred_in_signal",
                    ),
                )

    return result


def build_task_context(
    execution: RayTaskExecution,
    *,
    task: Task | None = None,
    backend_alias: str | None = None,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> TaskContext:
    """Construct a Django 6 ``TaskContext`` for a ``RayTaskExecution``."""
    alias = backend_alias or (
        task.backend
        if task is not None
        else _resolve_backend_alias_for_queue(str(execution.queue_name))
    )
    resolved_task = task or _project_result_task(
        alias=alias,
        callable_path=str(execution.callable_path),
        priority=int(execution.priority),
        queue_name=str(execution.queue_name),
        run_after=execution.run_after,
    )
    if args is not None and kwargs is not None:
        resolved_args = list(args)
        resolved_kwargs = dict(kwargs)
    else:
        resolved_args, resolved_kwargs = load_task_input(
            args_json=str(execution.args_json),
            kwargs_json=str(execution.kwargs_json),
            input_reference=execution.input_reference,
        )
    worker_ids = _build_worker_ids(
        state=str(execution.state),
        attempt_number=int(execution.attempt_number),
        claimed_by_worker=execution.claimed_by_worker,
        started_at=execution.started_at,
    )
    status = STATE_TO_STATUS.get(str(execution.state), TaskResultStatus.RUNNING)
    task_result = RayTaskResult(
        task=resolved_task,
        id=str(execution.task_id),
        status=status,
        enqueued_at=execution.created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        last_attempted_at=execution.started_at,
        args=resolved_args,
        kwargs=resolved_kwargs,
        backend=alias,
        errors=_build_task_errors(
            state=str(execution.state),
            error_message=execution.error_message,
            error_traceback=execution.error_traceback,
            cancellation_status=execution.cancellation_status,
            cancellation_error=execution.cancellation_error,
        ),
        worker_ids=worker_ids,
    )
    return TaskContext(task_result=task_result)


def build_task_context_for_active_execution(
    task: Task,
    *,
    args: list[Any],
    kwargs: dict[str, Any],
) -> TaskContext:
    """Build a Django 6 ``TaskContext`` for a running ``@task(takes_context=True)``.

    Uses active ``durable_task_execution`` context metadata in memory without
    requiring a database round-trip inside remote Ray workers.
    """
    from django_ray.runtime.context import get_current_task_context

    active = get_current_task_context()
    now = datetime.now(UTC)
    attempt_number = (
        int(active.attempt_number)
        if active is not None and active.attempt_number is not None and active.attempt_number > 0
        else 1
    )
    task_id = (
        str(active.task_id)
        if active is not None and active.task_id is not None
        else (str(active.task_pk) if active is not None else str(uuid.uuid4()))
    )
    worker_ids = _build_worker_ids(
        state=TaskState.RUNNING,
        attempt_number=attempt_number,
        claimed_by_worker=None,
        started_at=now,
    )
    task_result = RayTaskResult(
        task=task,
        id=task_id,
        status=TaskResultStatus.RUNNING,
        enqueued_at=now,
        started_at=now,
        finished_at=None,
        last_attempted_at=now,
        args=list(args),
        kwargs=dict(kwargs),
        backend=task.backend,
        errors=[],
        worker_ids=worker_ids,
    )
    return TaskContext(task_result=task_result)


# Module-level logger
logger = get_backend_logger()

_TASK_ID_ALLOCATION_ATTEMPTS = 3
_TASK_ID_UNIQUE_CONSTRAINT = "ray_task_id_unique"
_SQLITE_TASK_ID_UNIQUE_ERROR = "UNIQUE constraint failed: django_ray_raytaskexecution.task_id"


def _require_default_enqueue_database() -> str:
    """Validate the supported connection before preparing external task input."""
    try:
        supported = all(
            route(model) == DEFAULT_DB_ALIAS
            for model in (RayTaskExecution, TaskInputPayload)
            for route in (router.db_for_read, router.db_for_write)
        )
    except Exception:
        supported = False
    if not supported:
        raise ImproperlyConfigured(
            "django-ray: enqueue requires default database routing for task and input rows"
        ) from None
    return DEFAULT_DB_ALIAS


# Map our internal TaskState to Django's TaskResultStatus
STATE_TO_STATUS: dict[str, TaskResultStatus] = {
    TaskState.QUEUED: TaskResultStatus.READY,
    TaskState.RUNNING: TaskResultStatus.RUNNING,
    TaskState.SUCCEEDED: TaskResultStatus.SUCCESSFUL,
    TaskState.FAILED: TaskResultStatus.FAILED,
    TaskState.CANCELLED: TaskResultStatus.FAILED,
    TaskState.CANCELLING: TaskResultStatus.RUNNING,
    TaskState.LOST: TaskResultStatus.FAILED,
    TaskState.EXPIRED: TaskResultStatus.FAILED,
}


class TaskResultIdAllocationError(RuntimeError):
    """Raised when bounded task-result ID allocation cannot find a free ID."""


def _is_task_id_unique_violation(error: IntegrityError) -> bool:
    """Identify only the database constraint that owns the public task ID."""
    cause = error.__cause__
    diagnostics = getattr(cause, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name == _TASK_ID_UNIQUE_CONSTRAINT

    if connection.vendor != "sqlite" or cause is None:
        return False
    return (
        getattr(cause, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"
        and str(cause) == _SQLITE_TASK_ID_UNIQUE_ERROR
    )


class RayTaskBackend(BaseTaskBackend):
    """Django 6 Task Backend that executes tasks on Ray.

    This backend integrates with Django's native task framework while
    leveraging Ray for distributed task execution.

    Features:
        - Supports deferred execution (run_after)
        - Supports result retrieval
        - Uses database for task state tracking
        - Executes tasks on Ray cluster

    Configuration options (in OPTIONS dict):
        - RAY_ADDRESS: Optional Ray Job cluster target (defaults to DJANGO_RAY)
        - RAY_RUNTIME_ENV: Runtime environment for Ray workers
        - TIMEOUT_SECONDS: Optional positive per-task execution timeout
        - QUEUE_TIMEOUT_SECONDS: Positive queued-wait budget or None for unlimited
        - RAY_JOB_ONLY: Restrict declared queues to Ray Job workers (default False)
    """

    # Backend capabilities
    supports_defer = True  # We support run_after via the database
    supports_async_task = True
    supports_get_result = True  # We track results in the database
    supports_priority = True

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        """Initialize the Ray task backend.

        Args:
            alias: The backend alias (e.g., "default")
            params: Configuration parameters from TASKS setting
        """
        super().__init__(alias, params)

        # Extract Ray-specific options
        options = params.get("OPTIONS", {})
        if "RAY_ADDRESS" in options:
            explicit_address = options["RAY_ADDRESS"]
            if not isinstance(explicit_address, str) or not explicit_address.strip():
                raise ImproperlyConfigured(
                    "django-ray: TASKS backend OPTIONS['RAY_ADDRESS'] must be a non-empty string"
                )
            ray_target_address: str | None = explicit_address
        else:
            global_address = get_settings().get("RAY_ADDRESS")
            if global_address is None:
                ray_target_address = None
            elif not isinstance(global_address, str) or not global_address.strip():
                raise ImproperlyConfigured(
                    "django-ray: DJANGO_RAY['RAY_ADDRESS'] must be a non-empty string or None"
                )
            else:
                ray_target_address = global_address
        self.ray_target_address = ray_target_address
        self.ray_address = ray_target_address
        self.runtime_env_profile = options.get("RUNTIME_ENV_PROFILE")
        self.inline_runtime_env = (
            options["RAY_RUNTIME_ENV"] if "RAY_RUNTIME_ENV" in options else None
        )
        self.timeout_seconds = options.get("TIMEOUT_SECONDS")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds <= 0
        ):
            raise ImproperlyConfigured(
                "django-ray: TASKS backend OPTIONS['TIMEOUT_SECONDS'] must be a positive integer"
            )
        self.queue_timeout_seconds = options.get(
            "QUEUE_TIMEOUT_SECONDS", get_settings()["QUEUE_TIMEOUT_SECONDS"]
        )
        if self.queue_timeout_seconds is not None and (
            type(self.queue_timeout_seconds) is not int
            or self.queue_timeout_seconds <= 0
            or self.queue_timeout_seconds > QUEUE_TIMEOUT_SECONDS_MAX
        ):
            raise ImproperlyConfigured(
                "django-ray: TASKS backend OPTIONS['QUEUE_TIMEOUT_SECONDS'] must be None "
                f"or an integer between 1 and {QUEUE_TIMEOUT_SECONDS_MAX}"
            )
        self.ray_job_only = options.get("RAY_JOB_ONLY", False)
        if type(self.ray_job_only) is not bool:
            raise ImproperlyConfigured(
                "django-ray: TASKS backend OPTIONS['RAY_JOB_ONLY'] must be a boolean"
            )

    def validate_task(self, task: Task) -> None:
        """Validate an application declaration before trusting its identity."""
        _require_executable_task(task)
        if not isinstance(task, DjangoTask):
            raise InvalidTask("Task must be a django.tasks.base.Task instance.")
        super().validate_task(task)
        _register_validated_task(self.alias, task)

    def build_task_context(
        self,
        execution: RayTaskExecution,
        *,
        task: Task | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> TaskContext:
        """Build a Django 6 ``TaskContext`` bound to this backend alias."""
        return build_task_context(
            execution,
            task=task,
            backend_alias=self.alias,
            args=args,
            kwargs=kwargs,
        )

    def _create_execution_with_collision_retry(
        self,
        *,
        using: str,
        task: Task,
        task_id: str,
        callable_path: str,
        runtime_env: Any,
        stored_runtime_env: Any,
        prepared_input: Any,
        now: datetime,
        queue_deadline_at: datetime | None,
        use_savepoint: bool,
        timeout_seconds: int | None = None,
    ) -> tuple[RayTaskExecution, str]:
        for allocation_attempt in range(1, _TASK_ID_ALLOCATION_ATTEMPTS + 1):
            attempt_ctx = transaction.atomic(using=using) if use_savepoint else nullcontext()
            try:
                with attempt_ctx:
                    execution = RayTaskExecution.objects.using(using).create(
                        task_id=task_id,
                        callable_path=callable_path,
                        metadata_schema_version=EXECUTION_METADATA_SCHEMA_VERSION,
                        execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
                        created_with_django_ray_version=django_ray_version,
                        queue_name=task.queue_name,
                        priority=task.priority,
                        state=TaskState.QUEUED,
                        args_json=prepared_input.args_json,
                        kwargs_json=prepared_input.kwargs_json,
                        input_reference=prepared_input.input_reference,
                        run_after=task.run_after,
                        ray_target_address=self.ray_target_address,
                        runtime_env_profile=stored_runtime_env.profile,
                        runtime_env_json=stored_runtime_env.serialized,
                        runtime_env_hash=stored_runtime_env.digest,
                        timeout_seconds=timeout_seconds,
                        queue_timeout_seconds=self.queue_timeout_seconds,
                        queue_deadline_at=queue_deadline_at,
                        created_at=now,
                    )
            except IntegrityError as error:
                if not _is_task_id_unique_violation(error):
                    raise
                if allocation_attempt == _TASK_ID_ALLOCATION_ATTEMPTS:
                    logger.error(
                        "Task result ID allocation exhausted its collision budget",
                        extra={
                            "allocation_attempt": allocation_attempt,
                            "allocation_limit": _TASK_ID_ALLOCATION_ATTEMPTS,
                        },
                    )
                    raise TaskResultIdAllocationError(
                        "django-ray: could not allocate a unique task result ID "
                        f"after {_TASK_ID_ALLOCATION_ATTEMPTS} attempts"
                    ) from None
                logger.warning(
                    "Generated task result ID collided; retrying allocation",
                    extra={
                        "allocation_attempt": allocation_attempt,
                        "allocation_limit": _TASK_ID_ALLOCATION_ATTEMPTS,
                    },
                )
                task_id = str(uuid.uuid4())
                stored_runtime_env = runtime_env_for_storage(runtime_env, task_id=task_id)
                continue
            return execution, task_id
        raise TaskResultIdAllocationError(
            "django-ray: could not allocate a unique task result ID "
            f"after {_TASK_ID_ALLOCATION_ATTEMPTS} attempts"
        )

    def _schedule_enqueue_notifications(
        self,
        *,
        using: str,
        execution: RayTaskExecution,
        result: RayTaskResult,
    ) -> None:
        sender = type(self)

        def _post_commit() -> None:
            _emit_task_signal(
                task_enqueued,
                sender=sender,
                task_result=result,
                event_name="enqueued",
            )
            _safe_emit_lifecycle_span("enqueued", execution)

        transaction.on_commit(_post_commit, using=using)

    def enqueue(
        self,
        task: Task,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> RayTaskResult:
        """Queue up a task to be executed on Ray.

        This method creates a RayTaskExecution record in the database
        which will be picked up by the django_ray_worker process and
        submitted to Ray for execution.

        Args:
            task: The Django Task object to enqueue
            args: Positional arguments for the task function
            kwargs: Keyword arguments for the task function

        Returns:
            RayTaskResult object with task status and metadata
        """
        self.validate_task(task)
        using = _require_default_enqueue_database()
        # The database is the authority for uniqueness. UUIDv4 keeps collisions
        # vanishingly rare, while the bounded retry below makes a collision a
        # recoverable allocation event instead of an ambiguous durable identity.
        task_id = str(uuid.uuid4())

        # Get the callable path for the task function
        callable_path = task.module_path
        contract = get_ray_task_contract(task)
        if contract is not None and contract.runtime_env_profile is not None:
            effective_runtime_env_profile: str | None = contract.runtime_env_profile
            effective_inline_runtime_env = None
        else:
            effective_runtime_env_profile = self.runtime_env_profile
            effective_inline_runtime_env = self.inline_runtime_env
        effective_timeout_seconds = (
            contract.timeout_seconds
            if contract is not None and contract.timeout_seconds is not None
            else self.timeout_seconds
        )

        runtime_env = resolve_runtime_env_profile(
            effective_runtime_env_profile,
            inline_spec=effective_inline_runtime_env,
        )
        stored_runtime_env = runtime_env_for_storage(runtime_env, task_id=task_id)
        args_list = list(args)
        kwargs_dict = dict(kwargs)
        prepared_input = prepare_task_input(args_list, kwargs_dict)

        now = datetime.now(UTC)
        eligible_at = max(now, task.run_after) if task.run_after is not None else now
        queue_deadline_at = (
            eligible_at + timedelta(seconds=self.queue_timeout_seconds)
            if self.queue_timeout_seconds is not None
            else None
        )

        in_outer_tx = connections[using].in_atomic_block
        register_task_input(prepared_input, using=using)
        execution, task_id = self._create_execution_with_collision_retry(
            using=using,
            task=task,
            task_id=task_id,
            callable_path=callable_path,
            runtime_env=runtime_env,
            stored_runtime_env=stored_runtime_env,
            prepared_input=prepared_input,
            now=now,
            queue_deadline_at=queue_deadline_at,
            use_savepoint=in_outer_tx,
            timeout_seconds=effective_timeout_seconds,
        )
        result = self._execution_to_result(
            execution,
            task,
            args=args_list,
            kwargs=kwargs_dict,
        )
        self._schedule_enqueue_notifications(
            using=using,
            execution=execution,
            result=result,
        )

        logger.info(
            "Task enqueued",
            extra={
                "task_id": task_id,
                "callable_path": callable_path,
                "queue_name": task.queue_name,
                "priority": task.priority,
                "run_after": str(task.run_after) if task.run_after else None,
                "runtime_env_profile": runtime_env.profile,
                "runtime_env_hash": runtime_env.digest,
                "timeout_seconds": effective_timeout_seconds,
                "input_external": prepared_input.input_reference is not None,
                "input_size_bytes": prepared_input.size_bytes,
            },
        )

        return result

    def get_result(self, result_id: str) -> RayTaskResult:
        """Retrieve a task result by ID.

        Args:
            result_id: The unique task result ID

        Returns:
            RayTaskResult object with current task status

        Raises:
            TaskResultDoesNotExist: If no task with the given ID exists
        """
        try:
            execution = RayTaskExecution.objects.defer(*_GET_RESULT_DEFERRED_FIELDS).get(
                task_id=result_id
            )
        except RayTaskExecution.DoesNotExist:
            raise TaskResultDoesNotExist(f"Task result {result_id} does not exist") from None

        # Project identity without importing application code selected by a row.
        task = self._reconstruct_task(execution)

        return self._execution_to_result(execution, task)

    def _execution_to_result(
        self,
        execution: RayTaskExecution,
        task: Task,
        *,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> RayTaskResult:
        """Convert a RayTaskExecution to a Django 6-compatible RayTaskResult.

        Args:
            execution: The database execution record
            task: The Django Task object
            args: Optional in-memory positional arguments (avoids re-downloading
                external task inputs immediately after enqueue).
            kwargs: Optional in-memory keyword arguments.

        Returns:
            RayTaskResult object
        """
        errors = _build_task_errors(
            state=str(execution.state),
            error_message=execution.error_message,
            error_traceback=execution.error_traceback,
            cancellation_status=execution.cancellation_status,
            cancellation_error=execution.cancellation_error,
        )

        input_unavailable_error: InputPayloadUnavailableError | None = None
        pending_input_spec: tuple[str, str, str, str] | None = None
        if args is not None and kwargs is not None:
            resolved_args = list(args)
            resolved_kwargs = dict(kwargs)
        elif execution.input_reference is not None:
            # Defer external storage I/O until the caller actually reads .args
            # or .kwargs so status/result polling never downloads external inputs.
            resolved_args = []
            resolved_kwargs = {}
            pending_input_spec = (
                str(execution.task_id),
                str(execution.args_json),
                str(execution.kwargs_json),
                str(execution.input_reference),
            )
        else:
            try:
                resolved_args, resolved_kwargs = load_task_input(
                    args_json=execution.args_json,
                    kwargs_json=execution.kwargs_json,
                    input_reference=None,
                )
            except InputPayloadError as error:
                safe_error = redact_text(str(error))
                logger.warning(
                    "Failed to load durable task input",
                    extra={
                        "task_id": execution.task_id,
                        "input_external": False,
                        "error": safe_error,
                    },
                )
                resolved_args = []
                resolved_kwargs = {}
                input_unavailable_error = InputPayloadUnavailableError(
                    f"Task input for {execution.task_id} is unavailable: {safe_error}.",
                    task_id=execution.task_id,
                    reason="input_load_error",
                )

        worker_ids = _build_worker_ids(
            state=str(execution.state),
            attempt_number=int(execution.attempt_number),
            claimed_by_worker=execution.claimed_by_worker,
            started_at=execution.started_at,
        )

        # Map state to status
        status = STATE_TO_STATUS.get(str(execution.state), TaskResultStatus.READY)

        # Create the fail-loud result object
        result = RayTaskResult(
            task=task,
            id=execution.task_id,
            status=status,
            enqueued_at=execution.created_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            last_attempted_at=execution.started_at,
            args=resolved_args,
            kwargs=resolved_kwargs,
            backend=self.alias,
            errors=errors,
            worker_ids=worker_ids,
        )
        if input_unavailable_error is not None:
            object.__setattr__(result, "_input_unavailable_error", input_unavailable_error)
        if pending_input_spec is not None:
            object.__setattr__(result, "_pending_input_spec", pending_input_spec)

        # Set return value or unavailable marker if task succeeded
        if execution.state == TaskState.SUCCEEDED:
            if execution.result_data:
                try:
                    return_value = json.loads(execution.result_data)
                    object.__setattr__(result, "_return_value", return_value)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to decode stored task result payload",
                        extra={
                            "task_id": execution.task_id,
                        },
                    )
                    object.__setattr__(
                        result,
                        "_result_unavailable_error",
                        ResultUnavailableError(
                            f"Task result for {execution.task_id} is unavailable: "
                            "stored result payload could not be decoded as JSON.",
                            task_id=execution.task_id,
                            reason="invalid_json",
                        ),
                    )
            elif execution.result_reference:
                ref_str = str(execution.result_reference)
                oversize_match = _OVERSIZE_REF_RE.fullmatch(ref_str)
                if oversize_match is not None:
                    digest = oversize_match.group("digest")
                    size_bytes = int(oversize_match.group("bytes"))
                    object.__setattr__(
                        result,
                        "_result_unavailable_error",
                        ResultUnavailableError(
                            f"Task result for {execution.task_id} is unavailable: result "
                            f"({size_bytes} bytes, sha256={digest}) exceeded "
                            "MAX_RESULT_SIZE_BYTES and was stored as a digest-only stub under "
                            "RESULT_STORAGE_BACKEND='digest'. Configure RESULT_STORAGE_BACKEND "
                            "to 'filesystem', 's3', or 'gcs' for retrievable oversized results.",
                            task_id=execution.task_id,
                            digest=digest,
                            size_bytes=size_bytes,
                            reason="digest_stub",
                        ),
                    )
                else:
                    serialized_result: str | None = None
                    load_failed = False
                    try:
                        from django_ray.result_storage import (
                            ResultStorageError,
                            load_result_reference,
                        )

                        serialized_result = load_result_reference(ref_str)
                    except ResultStorageError as error:
                        load_failed = True
                        safe_error = redact_text(str(error))
                        if ref_str in safe_error:
                            safe_error = "external result storage error"
                        logger.warning(
                            "Failed to load external task result",
                            extra={
                                "task_id": execution.task_id,
                                "error": safe_error,
                            },
                        )
                        object.__setattr__(
                            result,
                            "_result_unavailable_error",
                            ResultUnavailableError(
                                f"Task result for {execution.task_id} is unavailable: "
                                f"failed to load external task result ({safe_error}).",
                                task_id=execution.task_id,
                                reason="storage_load_error",
                            ),
                        )

                    if not load_failed:
                        if serialized_result is None:
                            object.__setattr__(
                                result,
                                "_result_unavailable_error",
                                ResultUnavailableError(
                                    f"Task result for {execution.task_id} is unavailable: "
                                    "external result storage returned no payload.",
                                    task_id=execution.task_id,
                                    reason="empty_external_payload",
                                ),
                            )
                        else:
                            try:
                                return_value = json.loads(serialized_result)
                                object.__setattr__(result, "_return_value", return_value)
                            except (json.JSONDecodeError, TypeError):
                                logger.warning(
                                    "Failed to decode stored task result payload",
                                    extra={
                                        "task_id": execution.task_id,
                                    },
                                )
                                object.__setattr__(
                                    result,
                                    "_result_unavailable_error",
                                    ResultUnavailableError(
                                        f"Task result for {execution.task_id} is unavailable: "
                                        "stored result payload could not be decoded as JSON.",
                                        task_id=execution.task_id,
                                        reason="invalid_json",
                                    ),
                                )

        return result

    def _reconstruct_task(self, execution: RayTaskExecution) -> Task:
        """Build a read-only Task while preserving trusted function identity."""
        return _project_result_task(
            alias=self.alias,
            callable_path=execution.callable_path,
            priority=execution.priority,
            queue_name=execution.queue_name,
            run_after=execution.run_after,
        )

    def check(self, **kwargs: Any) -> list[Any]:
        """Run system checks for the backend.

        Returns:
            List of any check errors/warnings
        """
        errors = []

        # Check if Ray is reachable (optional, non-blocking)
        try:
            import ray

            if not ray.is_initialized():
                # Don't try to initialize, just note it's not connected
                pass
        except ImportError:
            from django.core.checks import Error

            errors.append(
                Error(
                    "Ray is not installed",
                    hint="Install django-ray with: pip install django-ray",
                    id="django_ray.E001",
                )
            )

        return errors
