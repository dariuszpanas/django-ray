"""Entrypoint for Ray to execute Django Tasks.

This module bootstraps Django and executes the task callable.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import errno
import json
import math
import os
import sys
import time
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from inspect import isawaitable, iscoroutine, iscoroutinefunction
from typing import TYPE_CHECKING, Any

import django
from django.apps import apps
from django.db import DatabaseError, InterfaceError, OperationalError, close_old_connections

from django_ray.execution_codec import (
    EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES,
    EXECUTION_REQUEST_MAX_BYTES,
)
from django_ray.logging import get_logger
from django_ray.ray_job_protocol import (
    RAY_JOB_CONFIG_JSON_ENV_VAR,
    RAY_JOB_REQUEST_REJECTED_EXIT_CODE,
    RayJobRequestBindingError,
    RayJobRequestBindingRejection,
    RayJobRequestExpectation,
    RayJobRequestReferenceExpectation,
    load_ray_job_request_expectation,
    validate_ray_job_request_expectation,
    validate_ray_job_request_reference_expectation,
)
from django_ray.redaction import materialize_exception_message, redact_text

if TYPE_CHECKING:
    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequestRejection,
    )

logger = get_logger(__name__)

_MAX_RAY_JOB_PAYLOAD_B64_BYTES = 4 * ((EXECUTION_REQUEST_MAX_BYTES + 2) // 3)
_COMPLETION_ENCODING_MESSAGE = (
    "Task completion could not be encoded. Application effects may "
    "have occurred; assess them before manually retrying."
)
RAY_JOB_COMPLETION_PERSISTENCE_FAILED_EXIT_CODE = 75
_COMPLETION_PERSIST_MAX_ATTEMPTS = 5
_COMPLETION_PERSIST_BACKOFF_SECONDS = (0.1, 0.25, 0.5, 1.0)
_RESULT_STORAGE_STORE_MAX_ATTEMPTS = 3
_RESULT_STORAGE_STORE_BACKOFF_SECONDS = (0.1, 0.25)
_DIAGNOSTIC_EXCEPTION_TYPE_MAX_BYTES = 512
_NON_TRANSIENT_STORAGE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


class _ResultEncodingError(ValueError):
    """Mark a result encoding failure after the callable has returned."""

    def __init__(self, *, offending_type: str | None = None) -> None:
        self.offending_type = offending_type
        super().__init__(_completion_encoding_message(offending_type))


class ResultStoragePersistenceError(RuntimeError):
    """Raised when configured external result storage fails after execution."""


class _EncodedCompletionResult(str):
    """Serialized completion JSON carrying lightweight status metadata."""

    completion_success: bool
    completion_error: str | None

    def __new__(
        cls,
        value: str,
        *,
        completion_success: bool,
        completion_error: str | None,
    ) -> _EncodedCompletionResult:
        obj = super().__new__(cls, value)
        obj.completion_success = completion_success
        obj.completion_error = completion_error
        return obj

    def __reduce__(self) -> tuple[type[str], tuple[str]]:
        """Serialize as a plain string across pickle/cloudpickle boundaries."""
        return (str, (str(self),))


class _CompletionPersistenceFailedResult(_EncodedCompletionResult):
    """Mark one completion whose durable DB write failed after retries."""


def _completion_encoding_message(offending_type: str | None = None) -> str:
    if not offending_type:
        return _COMPLETION_ENCODING_MESSAGE
    return (
        f"Task completion could not be encoded ({offending_type}). "
        "Application effects may have occurred; assess them before manually retrying."
    )


def _offending_non_json_type(value: Any, *, max_depth: int = 64) -> str | None:
    """Identify the first non-JSON-serializable type without retaining values."""
    ancestors: set[int] = set()
    stack: list[tuple[Any, int, int | None]] = [(value, 0, None)]
    visited = 0
    while stack:
        current, depth, container_id = stack.pop()
        if container_id is not None:
            ancestors.discard(container_id)
            continue
        visited += 1
        if visited > 100_000:
            return None
        if current is None or type(current) in (bool, int):
            continue
        if type(current) is float:
            if not math.isfinite(current):
                return "non-finite float"
            continue
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:
                return "invalid Unicode surrogate"
            continue
        if isinstance(current, dict):
            if depth >= max_depth:
                return "nesting depth exceeded"
            obj_id = id(current)
            if obj_id in ancestors:
                return "circular reference"
            for key in current:
                if not isinstance(key, str):
                    return (
                        f"non-string dict key of type "
                        f"{type(key).__module__}.{type(key).__qualname__}"
                    )
            ancestors.add(obj_id)
            stack.append((None, depth, obj_id))
            for item_val in current.values():
                stack.append((item_val, depth + 1, None))
            continue
        if isinstance(current, (list, tuple)):
            if depth >= max_depth:
                return "nesting depth exceeded"
            obj_id = id(current)
            if obj_id in ancestors:
                return "circular reference"
            ancestors.add(obj_id)
            stack.append((None, depth, obj_id))
            for item_val in current:
                stack.append((item_val, depth + 1, None))
            continue
        return f"non-JSON-serializable value of type {type(current).__module__}.{type(current).__qualname__}"
    return None


def _truncate_utf8_prefix(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _truncate_utf8_suffix(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _sanitize_and_bound_diagnostic(
    value: str | None,
    *,
    max_bytes: int,
    marker: str = "\n... [truncated] ...\n",
    preserve_tail: bool = True,
) -> str | None:
    """Sanitize NUL/surrogates and bound UTF-8 size while preserving diagnostics."""
    if value is None:
        return None
    cleaned = value.replace("\x00", "\\x00")
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        return _truncate_utf8_prefix(cleaned, max_bytes)
    budget = max_bytes - len(marker_bytes)
    if not preserve_tail:
        return _truncate_utf8_prefix(cleaned, budget) + marker
    # Keep more of the tail (final traceback frames & exception line) than head.
    head_budget = budget // 3
    tail_budget = budget - head_budget
    return (
        _truncate_utf8_prefix(cleaned, head_budget)
        + marker
        + _truncate_utf8_suffix(cleaned, tail_budget)
    )


def _close_stale_db_connections() -> None:
    """Close expired or broken Django DB connections outside atomic blocks."""
    if not apps.ready:
        return
    try:
        from django.db import close_old_connections as django_close_old_connections
        from django.db import connections

        if close_old_connections is django_close_old_connections:
            initialized = connections.all(initialized_only=True)
            if any(conn.in_atomic_block for conn in initialized):
                for conn in initialized:
                    if not conn.in_atomic_block:
                        conn.close_if_unusable_or_obsolete()
                return
        close_old_connections()
    except Exception:
        pass


def get_settings() -> dict[str, Any]:
    """Load runtime settings only after strict request validation."""
    from django_ray.conf.settings import get_settings as load_settings

    return load_settings()


def load_task_input(
    *,
    args_json: str,
    kwargs_json: str,
    input_reference: str | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Hydrate task input only after strict request validation and Django setup."""
    from django_ray.input_storage import load_task_input as hydrate_task_input

    return hydrate_task_input(
        args_json=args_json,
        kwargs_json=kwargs_json,
        input_reference=input_reference,
    )


class _StrictRequestRejectionResult(str):
    """Mark one fixed preflight rejection for the CLI exit contract."""


class _PayloadDecodeError(ValueError):
    """Reject one bounded payload without retaining its command-line value."""

    def __init__(self, *, resource_limit: bool) -> None:
        self.resource_limit = resource_limit
        super().__init__("Ray Job payload is invalid")


@dataclass
class TaskResult:
    """Structured result from task execution."""

    success: bool
    result: Any | None = None
    result_reference: str | None = None
    error: str | None = None
    traceback: str | None = None
    exception_type: str | None = None


def _serialize_completion(
    *,
    success: bool,
    result: Any,
    result_reference: str | None,
    error: str | None,
    error_traceback: str | None,
    exception_type: str | None,
    retryable: bool | None,
    completion_identity: ExecutionIdentity | None = None,
    execution_protocol_version: int | None = None,
    strict_encoding: bool = False,
) -> str:
    """Keep bound outcomes versioned even when their body cannot be encoded."""
    payload = {
        "success": success,
        "result": result,
        "result_reference": result_reference,
        "error": error,
        "traceback": error_traceback,
        "exception_type": exception_type,
        "retryable": retryable,
    }
    if completion_identity is not None and execution_protocol_version is not None:
        from django_ray import __version__
        from django_ray.execution_codec import ExecutionCompletion, encode_execution_completion

        try:
            encoded = encode_execution_completion(
                ExecutionCompletion(
                    identity=completion_identity,
                    execution_protocol_version=execution_protocol_version,
                    executor_django_ray_version=__version__,
                    success=success,
                    result=result,
                    result_reference=result_reference,
                    error=error,
                    traceback=error_traceback,
                    exception_type=exception_type,
                    retryable=retryable,
                )
            )
            return _EncodedCompletionResult(
                encoded,
                completion_success=success,
                completion_error=error,
            )
        except (TypeError, ValueError):
            # The callable may already have produced effects. Encoding failure
            # must neither discard its identity nor authorize automatic replay.
            # Do not recurse: invalid identity/protocol still fails closed.
            fallback_error = _completion_encoding_message(
                _offending_non_json_type(result) if success else None
            )
            encoded = encode_execution_completion(
                ExecutionCompletion(
                    identity=completion_identity,
                    execution_protocol_version=execution_protocol_version,
                    executor_django_ray_version=__version__,
                    success=False,
                    result=None,
                    result_reference=None,
                    error=fallback_error,
                    traceback=None,
                    exception_type="RayExecutionCompletionEncodingError",
                    retryable=False,
                )
            )
            return _EncodedCompletionResult(
                encoded,
                completion_success=False,
                completion_error=fallback_error,
            )
    if strict_encoding:
        try:
            encoded = json.dumps(payload, allow_nan=False)
            return _EncodedCompletionResult(
                encoded,
                completion_success=success,
                completion_error=error,
            )
        except (TypeError, ValueError):
            fallback_error = _completion_encoding_message(
                _offending_non_json_type(result) if success else None
            )
            encoded = json.dumps(
                {
                    "success": False,
                    "result": None,
                    "result_reference": None,
                    "error": fallback_error,
                    "traceback": None,
                    "exception_type": "RayExecutionCompletionEncodingError",
                    "retryable": False,
                },
                allow_nan=False,
            )
            return _EncodedCompletionResult(
                encoded,
                completion_success=False,
                completion_error=fallback_error,
            )
    encoded = json.dumps(payload)
    return _EncodedCompletionResult(
        encoded,
        completion_success=success,
        completion_error=error,
    )


def _serialize_error(
    e: BaseException,
    *,
    completion_identity: ExecutionIdentity | None = None,
    execution_protocol_version: int | None = None,
    strict_encoding: bool = False,
) -> str:
    """Serialize an exception as a task result JSON string."""
    from django_ray.execution_codec import (
        NestedExecutionRequestRejected,
        find_nested_execution_request_rejection,
    )
    from django_ray.input_storage import InputPayloadValidationError
    from django_ray.workflow.plans import WorkflowPlanMismatchError

    if isinstance(e, ResultStoragePersistenceError):
        return _serialize_completion(
            success=False,
            result=None,
            result_reference=None,
            error=_sanitize_and_bound_diagnostic(
                materialize_exception_message(e),
                max_bytes=EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES,
            ),
            error_traceback=None,
            exception_type="ResultStoragePersistenceError",
            retryable=False,
            completion_identity=completion_identity,
            execution_protocol_version=execution_protocol_version,
            strict_encoding=strict_encoding,
        )

    if isinstance(e, _ResultEncodingError):
        return _serialize_completion(
            success=False,
            result=None,
            result_reference=None,
            error=_completion_encoding_message(e.offending_type),
            error_traceback=None,
            exception_type="RayExecutionCompletionEncodingError",
            retryable=False,
            completion_identity=completion_identity,
            execution_protocol_version=execution_protocol_version,
            strict_encoding=strict_encoding,
        )

    nested_rejection = (
        find_nested_execution_request_rejection(e) if isinstance(e, Exception) else None
    )
    if nested_rejection is not None:
        return _serialize_completion(
            success=False,
            result=None,
            result_reference=None,
            error=str(nested_rejection),
            error_traceback=None,
            exception_type=(
                f"{NestedExecutionRequestRejected.__module__}."
                f"{NestedExecutionRequestRejected.__name__}"
            ),
            retryable=False,
            completion_identity=completion_identity,
            execution_protocol_version=execution_protocol_version,
            strict_encoding=strict_encoding,
        )

    raw_error = materialize_exception_message(e)
    raw_traceback = traceback.format_exc()
    raw_exception_type = type(e).__module__ + "." + type(e).__name__

    bounded_error = _sanitize_and_bound_diagnostic(
        raw_error,
        max_bytes=EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES,
        marker="\n... [error truncated to fit 64 KiB limit] ...\n",
    )
    bounded_traceback = _sanitize_and_bound_diagnostic(
        raw_traceback,
        max_bytes=EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES,
        marker="\n... [traceback truncated to fit 64 KiB limit] ...\n",
    )
    bounded_exception_type = _sanitize_and_bound_diagnostic(
        raw_exception_type,
        max_bytes=_DIAGNOSTIC_EXCEPTION_TYPE_MAX_BYTES,
        marker="",
        preserve_tail=False,
    )

    return _serialize_completion(
        success=False,
        result=None,
        result_reference=None,
        error=bounded_error or raw_exception_type,
        error_traceback=bounded_traceback,
        exception_type=bounded_exception_type,
        retryable=not isinstance(
            e,
            (
                InputPayloadValidationError,
                WorkflowPlanMismatchError,
                asyncio.CancelledError,
            ),
        ),
        completion_identity=completion_identity,
        execution_protocol_version=execution_protocol_version,
        strict_encoding=strict_encoding,
    )


def bootstrap_django() -> None:
    """Bootstrap Django environment for task execution."""
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings_module:
        raise RuntimeError("DJANGO_SETTINGS_MODULE environment variable is not set")

    if not apps.ready:
        django.setup()


def _unwrap_django_task_callable(callable_obj: Any) -> Any:
    """Unwrap a Django 6 Task instance without unwrapping functools.partial."""
    try:
        from django.tasks.base import Task
    except ImportError:
        return callable_obj
    if isinstance(callable_obj, Task):
        return callable_obj.func
    return callable_obj


def _invoke_task_callable(
    callable_obj: Any,
    args: list[Any],
    kwargs: dict[str, Any],
) -> Any:
    """Invoke a task callable at the executor's synchronous boundary."""
    task_cls: type[Any] | None
    try:
        from django.tasks.base import Task as task_cls  # noqa: N813
    except ImportError:
        task_cls = None

    if task_cls is not None and isinstance(callable_obj, task_cls) and callable_obj.takes_context:
        from django_ray.backends import build_task_context_for_active_execution

        args = [
            build_task_context_for_active_execution(
                callable_obj,
                args=args,
                kwargs=kwargs,
            ),
            *args,
        ]
    target = _unwrap_django_task_callable(callable_obj)
    if iscoroutinefunction(target):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "django-ray cannot execute a coroutine task from a thread "
                "that already has a running event loop"
            )
        try:
            return asyncio.run(target(*args, **kwargs))
        finally:
            _close_stale_db_connections()

    outcome = target(*args, **kwargs)
    if not isawaitable(outcome):
        return outcome

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        if iscoroutine(outcome):
            outcome.close()
        raise RuntimeError(
            "django-ray cannot execute a coroutine task from a thread "
            "that already has a running event loop"
        )
    try:
        return asyncio.run(outcome)
    finally:
        _close_stale_db_connections()


def _persist_task_completion(
    task_execution_pk: int | None,
    attempt_number: int | None,
    execution_generation: int | None,
    completion_data: str,
) -> bool:
    """Persist the structured completion envelope for Ray Job reconciliation.

    The update is conditional on the task still being RUNNING (and, when
    available, on the attempt number) so a stale Ray Job cannot overwrite a
    newer retry. Transient database errors are retried with bounded backoff
    after closing stale connections. Returns ``True`` when the DB update ran
    cleanly (or no persistence was requested), and ``False`` if persistence
    failed after all retry attempts.
    """
    if task_execution_pk is None or attempt_number is None or execution_generation is None:
        return True

    from django_ray.models import RayTaskExecution, TaskState

    filters: dict[str, Any] = {
        "pk": task_execution_pk,
        "state": TaskState.RUNNING,
    }
    if attempt_number is not None:
        filters["attempt_number"] = attempt_number
    filters["execution_generation"] = execution_generation

    max_attempts = max(1, int(_COMPLETION_PERSIST_MAX_ATTEMPTS))
    for attempt in range(max_attempts):
        _close_stale_db_connections()
        try:
            updated = RayTaskExecution.objects.filter(**filters).update(
                completion_data=completion_data,
            )
            if not updated:
                logger.warning(
                    "Could not persist completion envelope for task %s (stale or non-running attempt)",
                    task_execution_pk,
                )
            return True
        except (OperationalError, InterfaceError, DatabaseError):
            _close_stale_db_connections()
            if attempt + 1 < max_attempts:
                delay = _COMPLETION_PERSIST_BACKOFF_SECONDS[
                    min(attempt, len(_COMPLETION_PERSIST_BACKOFF_SECONDS) - 1)
                ]
                logger.warning(
                    "Transient DB error persisting completion envelope for task %s "
                    "(attempt %s/%s); retrying",
                    task_execution_pk,
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(delay)
                continue
            logger.exception("Failed to persist completion envelope for task %s", task_execution_pk)
            return False
        except Exception:
            logger.exception("Failed to persist completion envelope for task %s", task_execution_pk)
            return False
    return False


def _is_non_transient_storage_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return True
        if isinstance(current, OSError) and current.errno in _NON_TRANSIENT_STORAGE_ERRNOS:
            return True
        current = current.__cause__ or current.__context__
    return False


def _prepare_completion_result(
    result: Any,
    *,
    task_execution_pk: int | None,
    attempt_number: int | None,
    execution_generation: int | None,
    strict_encoding: bool = False,
    allow_result_offload: bool = False,
) -> tuple[Any | None, str | None]:
    """Keep the durable completion envelope bounded for oversized results."""
    should_offload = allow_result_offload or (
        task_execution_pk is not None
        and attempt_number is not None
        and execution_generation is not None
    )
    if not should_offload:
        if strict_encoding:
            try:
                json.dumps(result, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise _ResultEncodingError(
                    offending_type=_offending_non_json_type(result)
                ) from None
        return result, None

    try:
        serialized_result = json.dumps(result, allow_nan=not strict_encoding)
    except (TypeError, ValueError, RecursionError):
        if strict_encoding:
            raise _ResultEncodingError(offending_type=_offending_non_json_type(result)) from None
        raise
    settings = get_settings()
    max_result_size = int(settings.get("MAX_RESULT_SIZE_BYTES", 1024 * 1024))
    if len(serialized_result.encode("utf-8")) <= max_result_size:
        return result, None

    from django_ray.result_storage import (
        ResultStorageError,
        get_result_storage_backend,
    )

    backend = get_result_storage_backend(settings)
    max_attempts = max(1, int(_RESULT_STORAGE_STORE_MAX_ATTEMPTS))
    last_error: ResultStorageError | None = None
    for attempt in range(max_attempts):
        try:
            result_reference = backend.store(serialized_result=serialized_result)
            return None, result_reference
        except ResultStorageError as error:
            last_error = error
            if _is_non_transient_storage_error(error) or attempt + 1 >= max_attempts:
                break
            delay = _RESULT_STORAGE_STORE_BACKOFF_SECONDS[
                min(attempt, len(_RESULT_STORAGE_STORE_BACKOFF_SECONDS) - 1)
            ]
            time.sleep(delay)

    assert last_error is not None
    redacted_detail = redact_text(materialize_exception_message(last_error))
    error_name = type(last_error).__name__
    logger.error(
        "Result storage backend failed for task %s after %s attempt(s) (%s: %s); "
        "refusing silent digest downgrade",
        task_execution_pk,
        max_attempts,
        error_name,
        redacted_detail,
    )
    raise ResultStoragePersistenceError(
        f"Failed to persist oversized task result to configured result storage "
        f"({error_name}): {redacted_detail}"
    ) from last_error


def execute_task(
    callable_path: str,
    serialized_args: str,
    serialized_kwargs: str,
    task_execution_pk: int | None = None,
    task_id: str | None = None,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
    runtime_env_profile: str | None = None,
    runtime_env_hash: str = "",
    runtime_env_plan_identity: dict[str, Any] | None = None,
    input_reference: str | None = None,
    ray_job_driver: bool | None = None,
    _completion_identity: ExecutionIdentity | None = None,
    _execution_protocol_version: int | None = None,
    _strict_execution_request: bool = False,
    *,
    allow_result_offload: bool = False,
    _strict_encoding: bool | None = None,
) -> str:
    """Execute a Django Task and return JSON result.

    Args:
        callable_path: Dotted path to the task callable.
        serialized_args: JSON-serialized positional arguments.
        serialized_kwargs: JSON-serialized keyword arguments.
        task_execution_pk: Durable task execution primary key, when running via
            the Ray Job API.
        task_id: Durable Django task identifier, when running through a task manager.
        attempt_number: Current retry attempt, used to prevent stale writes.
        execution_generation: Monotonic execution token, used to isolate manual retries.
        input_reference: Durable combined-input reference for external payloads.
        ray_job_driver: Override execution-context mode for synchronous workers.
        allow_result_offload: Enable executor-side ``result_storage`` spillover
            without writing Ray Job ``completion_data`` rows to PostgreSQL.

    Returns:
        JSON-serialized TaskResult.
    """
    completion_task_execution_pk = task_execution_pk if ray_job_driver is not False else None
    effective_strict_encoding = (
        _strict_encoding
        if _strict_encoding is not None
        else (
            (_completion_identity is not None and _execution_protocol_version is not None)
            or ray_job_driver is False
        )
    )
    try:
        bootstrap_django()
        _close_stale_db_connections()

        import django_ray.runtime.import_utils as import_utils

        resolver = getattr(import_utils, "resolve_task_callable", import_utils.import_callable)
        args, kwargs = load_task_input(
            args_json=serialized_args,
            kwargs_json=serialized_kwargs,
            input_reference=input_reference,
        )
        callable_obj = resolver(callable_path)

        if task_execution_pk is None:
            execution_context = nullcontext()
        else:
            from django_ray.runtime.compiled_graph import (
                CompiledGraphSubmissionTransport,
            )
            from django_ray.runtime.context import durable_task_execution

            effective_ray_job_driver = True if ray_job_driver is None else ray_job_driver

            execution_context = durable_task_execution(
                task_execution_pk,
                task_id=task_id,
                execution_protocol_version=_execution_protocol_version,
                attempt_number=attempt_number,
                execution_generation=execution_generation,
                runtime_env_profile=runtime_env_profile,
                runtime_env_hash=runtime_env_hash,
                runtime_env_plan_identity=runtime_env_plan_identity,
                ray_job_driver=effective_ray_job_driver,
                compiled_graph_submission_transport=(
                    CompiledGraphSubmissionTransport.RAY_JOB.value
                    if effective_ray_job_driver
                    else None
                ),
                strict_execution_request=_strict_execution_request,
            )

        try:
            with execution_context:
                result = _invoke_task_callable(callable_obj, args, kwargs)
        finally:
            _close_stale_db_connections()

        result_value, result_reference = _prepare_completion_result(
            result,
            task_execution_pk=completion_task_execution_pk,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
            strict_encoding=effective_strict_encoding,
            allow_result_offload=allow_result_offload,
        )
        result_json = _serialize_completion(
            success=True,
            result=result_value,
            result_reference=result_reference,
            error=None,
            error_traceback=None,
            exception_type=None,
            retryable=None,
            completion_identity=_completion_identity,
            execution_protocol_version=_execution_protocol_version,
            strict_encoding=effective_strict_encoding,
        )

    except (Exception, asyncio.CancelledError) as e:
        result_json = _serialize_error(
            e,
            completion_identity=_completion_identity,
            execution_protocol_version=_execution_protocol_version,
            strict_encoding=effective_strict_encoding,
        )

    persisted = _persist_task_completion(
        completion_task_execution_pk,
        attempt_number,
        execution_generation,
        result_json,
    )
    if completion_task_execution_pk is not None and persisted is False:
        return _CompletionPersistenceFailedResult(
            result_json,
            completion_success=getattr(result_json, "completion_success", False),
            completion_error=getattr(result_json, "completion_error", None),
        )
    return result_json


def _decode_payload_b64(payload_b64: str) -> str:
    """Decode one bounded URL-safe payload without retaining rejected bytes."""
    if type(payload_b64) is not str:
        raise _PayloadDecodeError(resource_limit=False) from None
    # Base64's decoder first creates an ASCII byte copy for ``str`` input.
    # Reject by the allocation-free character count before crossing that
    # boundary.  ASCII then makes the character and encoded-byte ceilings
    # identical for every accepted candidate.
    if len(payload_b64) > _MAX_RAY_JOB_PAYLOAD_B64_BYTES:
        raise _PayloadDecodeError(resource_limit=True) from None
    if not payload_b64.isascii():
        raise _PayloadDecodeError(resource_limit=False) from None
    try:
        decoded = base64.b64decode(payload_b64, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise _PayloadDecodeError(resource_limit=False) from None
    if len(decoded) > EXECUTION_REQUEST_MAX_BYTES:
        raise _PayloadDecodeError(resource_limit=True) from None
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        raise _PayloadDecodeError(resource_limit=False) from None


def _binding_rejection_classification(
    classification: RayJobRequestBindingRejection,
) -> ExecutionRequestRejection:
    """Map control-plane failures to the fixed execution rejection vocabulary."""
    from django_ray.execution_codec import ExecutionRequestRejection

    mapping = {
        RayJobRequestBindingRejection.RESOURCE_LIMIT: ExecutionRequestRejection.RESOURCE_LIMIT,
        RayJobRequestBindingRejection.IDENTITY_MISMATCH: (
            ExecutionRequestRejection.IDENTITY_MISMATCH
        ),
        RayJobRequestBindingRejection.PROTOCOL_MISMATCH: (
            ExecutionRequestRejection.PROTOCOL_MISMATCH
        ),
        RayJobRequestBindingRejection.TRANSPORT_MISMATCH: (
            ExecutionRequestRejection.UNSUPPORTED_TRANSPORT
        ),
    }
    return mapping.get(classification, ExecutionRequestRejection.INVALID_VERSIONED)


def _request_storage_rejection_classification(classification: object) -> ExecutionRequestRejection:
    """Map every opaque-reference failure to the fixed request vocabulary."""
    from django_ray.execution_codec import ExecutionRequestRejection

    if getattr(classification, "value", None) == "resource_limit":
        return ExecutionRequestRejection.RESOURCE_LIMIT
    return ExecutionRequestRejection.INVALID_VERSIONED


def _fixed_unbound_request_rejection(classification: ExecutionRequestRejection) -> str:
    """Return a fixed legacy-shaped diagnostic when no identity is trusted."""
    return json.dumps(
        {
            "success": False,
            "result": None,
            "result_reference": None,
            "error": f"execution request rejected: {classification.value}",
            "traceback": None,
            "exception_type": "RayExecutionRequestIncompatible",
            "retryable": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_request_rejection(
    expectation: RayJobRequestExpectation | None,
    classification: ExecutionRequestRejection,
) -> _StrictRequestRejectionResult:
    """Encode one secret-free rejection before Django or application setup."""
    if expectation is not None:
        from django_ray import __version__
        from django_ray.execution_codec import (
            ExecutionRequestEncodeError,
            encode_execution_request_rejection,
        )

        try:
            return _StrictRequestRejectionResult(
                encode_execution_request_rejection(
                    expected_identity=expectation.identity,
                    expected_execution_protocol_version=(expectation.execution_protocol_version),
                    executor_django_ray_version=__version__,
                    classification=classification,
                )
            )
        except ExecutionRequestEncodeError:
            pass
    return _StrictRequestRejectionResult(_fixed_unbound_request_rejection(classification))


def execute_task_from_payload(payload_b64: str) -> str:
    """Classify retired inline requests without crossing the application boundary."""
    from django_ray.execution_codec import (
        ExecutionRequestDecodeError,
        ExecutionRequestRejection,
        decode_execution_request,
    )

    try:
        expectation = load_ray_job_request_expectation(os.environ.get(RAY_JOB_CONFIG_JSON_ENV_VAR))
    except RayJobRequestBindingError as error:
        return _strict_request_rejection(
            None,
            _binding_rejection_classification(error.classification),
        )
    if expectation is not None and not isinstance(expectation, RayJobRequestExpectation):
        return _strict_request_rejection(
            None,
            ExecutionRequestRejection.UNSUPPORTED_TRANSPORT,
        )

    try:
        payload_json = _decode_payload_b64(payload_b64)
    except _PayloadDecodeError as error:
        classification = (
            ExecutionRequestRejection.RESOURCE_LIMIT
            if error.resource_limit
            else ExecutionRequestRejection.INVALID_VERSIONED
        )
        return _strict_request_rejection(expectation, classification)

    try:
        request = decode_execution_request(
            payload_json,
            expected_identity=(expectation.identity if expectation is not None else None),
            expected_execution_protocol_version=(
                expectation.execution_protocol_version if expectation is not None else None
            ),
        )
    except ExecutionRequestDecodeError as error:
        return _strict_request_rejection(expectation, error.classification)

    # A versioned payload without an independent control-plane expectation is
    # never allowed to make its own identity trustworthy.
    if expectation is None:
        return _strict_request_rejection(
            None,
            ExecutionRequestRejection.INVALID_VERSIONED,
        )

    if request.compiled_graph_submission_transport != "ray-job":
        return _strict_request_rejection(
            expectation,
            ExecutionRequestRejection.UNSUPPORTED_TRANSPORT,
        )
    try:
        validate_ray_job_request_expectation(
            expectation,
            expected_identity=request.identity,
            expected_execution_protocol_version=request.execution_protocol_version,
            serialized_request=payload_json,
            expected_submission_transport="ray-job",
        )
    except RayJobRequestBindingError as error:
        return _strict_request_rejection(
            expectation,
            _binding_rejection_classification(error.classification),
        )

    # Independently bound rq1 payloads are retired too. Preserve strict-family
    # classification above, but only the stored rq2 reference path may execute.
    return _strict_request_rejection(
        expectation,
        ExecutionRequestRejection.UNSUPPORTED_TRANSPORT,
    )


def execute_task_from_reference(encoded_locator: str) -> str:
    """Load and bind one rq2 request before crossing the Django boundary."""
    from django_ray.execution_codec import ExecutionRequestRejection
    from django_ray.ray_job_request_storage import (
        RayJobRequestStorageError,
        decode_ray_job_request_locator,
        load_ray_job_request,
    )

    try:
        expectation = load_ray_job_request_expectation(os.environ.get(RAY_JOB_CONFIG_JSON_ENV_VAR))
    except RayJobRequestBindingError as error:
        return _strict_request_rejection(
            None,
            _binding_rejection_classification(error.classification),
        )
    if expectation is None:
        return _strict_request_rejection(
            None,
            ExecutionRequestRejection.INVALID_VERSIONED,
        )
    if not isinstance(expectation, RayJobRequestReferenceExpectation):
        return _strict_request_rejection(
            expectation if isinstance(expectation, RayJobRequestExpectation) else None,
            ExecutionRequestRejection.UNSUPPORTED_TRANSPORT,
        )

    try:
        validate_ray_job_request_reference_expectation(
            expectation,
            request_locator=encoded_locator,
        )
    except RayJobRequestBindingError as error:
        return _strict_request_rejection(
            None,
            _binding_rejection_classification(error.classification),
        )

    try:
        locator = decode_ray_job_request_locator(encoded_locator)
    except RayJobRequestStorageError as error:
        return _strict_request_rejection(
            None,
            _request_storage_rejection_classification(error.classification),
        )

    try:
        validate_ray_job_request_reference_expectation(
            expectation,
            expected_request_sha256=locator.digest,
            expected_request_size_bytes=locator.size_bytes,
            request_reference=locator.reference,
        )
    except RayJobRequestBindingError as error:
        return _strict_request_rejection(
            None,
            _binding_rejection_classification(error.classification),
        )

    try:
        loaded = load_ray_job_request(locator)
    except RayJobRequestStorageError as error:
        return _strict_request_rejection(
            None,
            _request_storage_rejection_classification(error.classification),
        )

    request = loaded.request
    if request.compiled_graph_submission_transport != "ray-job":
        return _strict_request_rejection(
            None,
            ExecutionRequestRejection.UNSUPPORTED_TRANSPORT,
        )
    try:
        validate_ray_job_request_reference_expectation(
            expectation,
            expected_identity=request.identity,
            expected_execution_protocol_version=request.execution_protocol_version,
            expected_request_sha256=loaded.digest,
            expected_request_size_bytes=loaded.size_bytes,
            serialized_request=loaded.serialized_request,
            request_reference=loaded.reference,
        )
    except RayJobRequestBindingError as error:
        return _strict_request_rejection(
            None,
            _binding_rejection_classification(error.classification),
        )

    return execute_task(
        callable_path=request.callable_path,
        serialized_args=request.serialized_args,
        serialized_kwargs=request.serialized_kwargs,
        task_execution_pk=request.identity.task_execution_pk,
        task_id=request.identity.task_id,
        attempt_number=request.identity.attempt_number,
        execution_generation=request.identity.execution_generation,
        runtime_env_profile=request.runtime_env_profile,
        runtime_env_hash=request.runtime_env_hash,
        runtime_env_plan_identity=request.runtime_env_plan_identity,
        input_reference=request.input_reference,
        ray_job_driver=True,
        _completion_identity=request.identity,
        _execution_protocol_version=request.execution_protocol_version,
        _strict_execution_request=True,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Ray Job execution."""
    parser = argparse.ArgumentParser(description="Execute a django-ray task payload")
    request_source = parser.add_mutually_exclusive_group(required=True)
    request_source.add_argument(
        "--payload-b64",
        help="Retired inline carrier; always refuses execution",
    )
    request_source.add_argument(
        "--request-ref-b64",
        help="Bounded URL-safe base64 encoded durable request locator",
    )
    args = parser.parse_args(argv)

    # The durable completion envelope is persisted in the database by
    # ``execute_task``.  Do not print it: Ray Job logs are operational output
    # and must not become an accidental copy of a task's return value.
    if args.request_ref_b64 is not None:
        result_json = execute_task_from_reference(args.request_ref_b64)
    else:
        assert args.payload_b64 is not None
        result_json = execute_task_from_payload(args.payload_b64)
    strict_rejection = isinstance(result_json, _StrictRequestRejectionResult)
    persistence_failed = isinstance(result_json, _CompletionPersistenceFailedResult)
    try:
        result = json.loads(result_json)
    except (TypeError, json.JSONDecodeError):
        print("django-ray task produced an invalid completion envelope", file=sys.stderr)
    else:
        if not isinstance(result, dict):
            print("django-ray task produced an invalid completion envelope", file=sys.stderr)
        elif result.get("success"):
            print("django-ray task completed successfully")
        else:
            # A strict refusal contains only fixed rejection vocabulary. Its
            # diagnostic must not import application settings to load custom
            # redaction patterns before the request is accepted.
            error = redact_text(result.get("error"), patterns=() if strict_rejection else None)
            print(
                f"django-ray task failed: {error}",
                file=sys.stderr,
            )
    if strict_rejection:
        return RAY_JOB_REQUEST_REJECTED_EXIT_CODE
    if persistence_failed:
        return RAY_JOB_COMPLETION_PERSISTENCE_FAILED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
