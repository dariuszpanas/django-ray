"""Entrypoint for Ray to execute Django Tasks.

This module bootstraps Django and executes the task callable.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

import django
from django.apps import apps

from django_ray.conf.settings import get_settings
from django_ray.input_storage import InputPayloadValidationError, load_task_input
from django_ray.redaction import redact_text

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """Structured result from task execution."""

    success: bool
    result: Any | None = None
    result_reference: str | None = None
    error: str | None = None
    traceback: str | None = None
    exception_type: str | None = None


def _serialize_error(e: Exception) -> str:
    """Serialize an exception as a task result JSON string."""
    return json.dumps(
        {
            "success": False,
            "result": None,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "exception_type": type(e).__module__ + "." + type(e).__name__,
            "retryable": not isinstance(e, InputPayloadValidationError),
        }
    )


def bootstrap_django() -> None:
    """Bootstrap Django environment for task execution."""
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings_module:
        raise RuntimeError("DJANGO_SETTINGS_MODULE environment variable is not set")

    if not apps.ready:
        django.setup()


def _persist_task_completion(
    task_execution_pk: int | None,
    attempt_number: int | None,
    execution_generation: int | None,
    completion_data: str,
) -> None:
    """Persist the structured completion envelope for Ray Job reconciliation.

    The update is conditional on the task still being RUNNING (and, when
    available, on the attempt number) so a stale Ray Job cannot overwrite a
    newer retry. Failure to write the channel is intentionally logged only;
    the worker will keep the task non-terminal when the envelope is absent.
    """
    if task_execution_pk is None or attempt_number is None or execution_generation is None:
        return

    try:
        from django_ray.models import RayTaskExecution, TaskState

        filters: dict[str, Any] = {
            "pk": task_execution_pk,
            "state": TaskState.RUNNING,
        }
        if attempt_number is not None:
            filters["attempt_number"] = attempt_number
        filters["execution_generation"] = execution_generation
        updated = RayTaskExecution.objects.filter(**filters).update(
            completion_data=completion_data,
        )
        if not updated:
            logger.warning(
                "Could not persist completion envelope for task %s (stale or non-running attempt)",
                task_execution_pk,
            )
    except Exception:
        logger.exception("Failed to persist completion envelope for task %s", task_execution_pk)


def _prepare_completion_result(
    result: Any,
    *,
    task_execution_pk: int | None,
    attempt_number: int | None,
    execution_generation: int | None,
) -> tuple[Any | None, str | None]:
    """Keep the durable completion envelope bounded for oversized results."""
    if task_execution_pk is None or attempt_number is None or execution_generation is None:
        return result, None

    serialized_result = json.dumps(result)
    settings = get_settings()
    max_result_size = int(settings.get("MAX_RESULT_SIZE_BYTES", 1024 * 1024))
    if len(serialized_result.encode("utf-8")) <= max_result_size:
        return result, None

    from django_ray.result_storage import (
        DigestResultStorage,
        ResultStorageError,
        get_result_storage_backend,
    )

    try:
        result_reference = get_result_storage_backend(settings).store(
            serialized_result=serialized_result
        )
    except ResultStorageError as error:
        logger.warning(
            "Result storage backend failed for task %s (%s); using digest-only reference",
            task_execution_pk,
            error,
        )
        result_reference = DigestResultStorage().store(serialized_result=serialized_result)

    return None, result_reference


def execute_task(
    callable_path: str,
    serialized_args: str,
    serialized_kwargs: str,
    task_execution_pk: int | None = None,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
    runtime_env_profile: str | None = None,
    runtime_env_hash: str = "",
    input_reference: str | None = None,
    ray_job_driver: bool | None = None,
) -> str:
    """Execute a Django Task and return JSON result.

    Args:
        callable_path: Dotted path to the task callable.
        serialized_args: JSON-serialized positional arguments.
        serialized_kwargs: JSON-serialized keyword arguments.
        task_execution_pk: Durable task execution primary key, when running via
            the Ray Job API.
        attempt_number: Current retry attempt, used to prevent stale writes.
        execution_generation: Monotonic execution token, used to isolate manual retries.
        input_reference: Durable combined-input reference for external payloads.
        ray_job_driver: Override execution-context mode for synchronous workers.

    Returns:
        JSON-serialized TaskResult.
    """
    from django_ray.runtime.import_utils import import_callable

    try:
        bootstrap_django()

        args, kwargs = load_task_input(
            args_json=serialized_args,
            kwargs_json=serialized_kwargs,
            input_reference=input_reference,
        )
        callable_obj = import_callable(callable_path)

        if task_execution_pk is None:
            execution_context = nullcontext()
        else:
            from django_ray.runtime.context import durable_task_execution

            execution_context = durable_task_execution(
                task_execution_pk,
                runtime_env_profile=runtime_env_profile,
                runtime_env_hash=runtime_env_hash,
                ray_job_driver=True if ray_job_driver is None else ray_job_driver,
            )

        with execution_context:
            result = callable_obj(*args, **kwargs)

        result_value, result_reference = _prepare_completion_result(
            result,
            task_execution_pk=task_execution_pk,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
        )
        result_json = json.dumps(
            {
                "success": True,
                "result": result_value,
                "result_reference": result_reference,
                "error": None,
                "traceback": None,
                "exception_type": None,
                "retryable": None,
            }
        )

    except Exception as e:
        result_json = _serialize_error(e)

    _persist_task_completion(
        task_execution_pk,
        attempt_number,
        execution_generation,
        result_json,
    )
    return result_json


def execute_task_from_payload(payload_b64: str) -> str:
    """Decode payload and execute the task.

    Args:
        payload_b64: URL-safe base64 JSON payload containing:
            - callable_path
            - serialized_args
            - serialized_kwargs
            - task_execution_pk (optional)
            - attempt_number (optional)
            - execution_generation (optional)

    Returns:
        JSON-serialized TaskResult.
    """
    try:
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        payload = json.loads(payload_json)
        transport_version = payload.get("transport_version", 1)
        if transport_version not in (1, 2):
            raise InputPayloadValidationError(
                f"Unsupported Ray Job input transport version: {transport_version}"
            )
        if transport_version == 2 and not payload.get("input_reference"):
            raise InputPayloadValidationError(
                "Ray Job input transport version 2 requires input_reference"
            )
        return execute_task(
            callable_path=payload["callable_path"],
            serialized_args=payload.get("serialized_args", "null"),
            serialized_kwargs=payload.get("serialized_kwargs", "null"),
            task_execution_pk=payload.get("task_execution_pk"),
            attempt_number=payload.get("attempt_number"),
            execution_generation=payload.get("execution_generation"),
            runtime_env_profile=payload.get("runtime_env_profile"),
            runtime_env_hash=payload.get("runtime_env_hash", ""),
            input_reference=payload.get("input_reference"),
        )
    except Exception as e:
        return _serialize_error(e)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Ray Job execution."""
    parser = argparse.ArgumentParser(description="Execute a django-ray task payload")
    parser.add_argument(
        "--payload-b64",
        required=True,
        help="URL-safe base64 encoded task payload",
    )
    args = parser.parse_args(argv)

    # The durable completion envelope is persisted in the database by
    # ``execute_task``.  Do not print it: Ray Job logs are operational output
    # and must not become an accidental copy of a task's return value.
    result_json = execute_task_from_payload(args.payload_b64)
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
            print(
                f"django-ray task failed: {redact_text(result.get('error'))}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
