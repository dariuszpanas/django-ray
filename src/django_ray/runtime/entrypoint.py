"""Entrypoint for Ray to execute Django Tasks.

This module bootstraps Django and executes the task callable.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import sys
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

import django
from django.apps import apps

from django_ray.execution_codec import EXECUTION_REQUEST_MAX_BYTES
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
from django_ray.redaction import redact_text

if TYPE_CHECKING:
    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequestRejection,
    )

logger = get_logger(__name__)

_MAX_RAY_JOB_PAYLOAD_B64_BYTES = 4 * ((EXECUTION_REQUEST_MAX_BYTES + 2) // 3)


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
) -> str:
    """Serialize an enriched v1 outcome with a released-v1 fallback."""
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
            return encode_execution_completion(
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
        except (TypeError, ValueError):
            # Protocol v1 deliberately retains the released JSON surface for
            # producer-emittable values outside the strict enriched schema.
            pass
    return json.dumps(payload)


def _serialize_error(
    e: Exception,
    *,
    completion_identity: ExecutionIdentity | None = None,
    execution_protocol_version: int | None = None,
) -> str:
    """Serialize an exception as a task result JSON string."""
    from django_ray.execution_codec import (
        NestedExecutionRequestRejected,
        find_nested_execution_request_rejection,
    )
    from django_ray.input_storage import InputPayloadValidationError
    from django_ray.workflow_plans import WorkflowPlanMismatchError

    nested_rejection = find_nested_execution_request_rejection(e)
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
        )

    return _serialize_completion(
        success=False,
        result=None,
        result_reference=None,
        error=str(e),
        error_traceback=traceback.format_exc(),
        exception_type=type(e).__module__ + "." + type(e).__name__,
        retryable=not isinstance(
            e,
            (InputPayloadValidationError, WorkflowPlanMismatchError),
        ),
        completion_identity=completion_identity,
        execution_protocol_version=execution_protocol_version,
    )


def bootstrap_django() -> None:
    """Bootstrap Django environment for task execution."""
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings_module:
        raise RuntimeError("DJANGO_SETTINGS_MODULE environment variable is not set")

    if not apps.ready:
        django.setup()


def _invoke_task_callable(
    callable_obj: Any,
    args: list[Any],
    kwargs: dict[str, Any],
) -> Any:
    """Invoke a task callable at the executor's synchronous boundary."""
    if not iscoroutinefunction(callable_obj):
        return callable_obj(*args, **kwargs)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "django-ray cannot execute a coroutine task from a thread "
            "that already has a running event loop"
        )

    return asyncio.run(callable_obj(*args, **kwargs))


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

    Returns:
        JSON-serialized TaskResult.
    """
    completion_task_execution_pk = task_execution_pk if ray_job_driver is not False else None
    try:
        bootstrap_django()

        from django_ray.runtime.import_utils import import_callable

        args, kwargs = load_task_input(
            args_json=serialized_args,
            kwargs_json=serialized_kwargs,
            input_reference=input_reference,
        )
        callable_obj = import_callable(callable_path)

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

        with execution_context:
            result = _invoke_task_callable(callable_obj, args, kwargs)

        result_value, result_reference = _prepare_completion_result(
            result,
            task_execution_pk=completion_task_execution_pk,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
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
        )

    except Exception as e:
        result_json = _serialize_error(
            e,
            completion_identity=_completion_identity,
            execution_protocol_version=_execution_protocol_version,
        )

    _persist_task_completion(
        completion_task_execution_pk,
        attempt_number,
        execution_generation,
        result_json,
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


def _execute_legacy_payload(payload_json: str) -> str:
    """Retain the released unversioned protocol-v1 payload adapter."""
    from django_ray.input_storage import InputPayloadValidationError

    try:
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
            task_id=payload.get("task_id"),
            attempt_number=payload.get("attempt_number"),
            execution_generation=payload.get("execution_generation"),
            runtime_env_profile=payload.get("runtime_env_profile"),
            runtime_env_hash=payload.get("runtime_env_hash", ""),
            runtime_env_plan_identity=payload.get("runtime_env_plan_identity"),
            input_reference=payload.get("input_reference"),
        )
    except Exception as error:
        return _serialize_error(error)


def execute_task_from_payload(payload_b64: str) -> str:
    """Fence a strict request or execute the released protocol-v1 payload."""
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
        if expectation is None and not error.resource_limit:
            return _serialize_error(error)
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
        if expectation is not None or error.attempted_versioned:
            return _strict_request_rejection(expectation, error.classification)
        if not error.allows_legacy_fallback:
            return _strict_request_rejection(expectation, error.classification)
        return _execute_legacy_payload(payload_json)

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
        help="URL-safe base64 encoded task payload",
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
    if isinstance(result_json, _StrictRequestRejectionResult):
        return RAY_JOB_REQUEST_REJECTED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
