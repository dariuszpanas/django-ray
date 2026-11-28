"""Synchronous in-process runner aligned with the BaseRunner contract."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django_ray.conf import settings as conf_settings
from django_ray.execution_codec import ExecutionIdentity
from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.runtime import entrypoint as runtime_entrypoint
from django_ray.runtime import runtime_env as runtime_env_module
from django_ray.runtime.serialization import serialize_args
from django_ray.workflow import plans as workflow_plans

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


_MAX_TRACKED_HANDLES = 256


@dataclass
class SyncSubmissionHandle(SubmissionHandle):
    """Submission handle returned by ``SyncRunner`` with its inline result envelope."""

    result_json: str
    status: JobStatus
    error_message: str | None = None


def _classify_sync_result(result_json: str) -> tuple[JobStatus, str | None]:
    """Derive a coarse JobStatus from an encoded execution completion envelope."""
    try:
        decoded = json.loads(result_json)
    except Exception:
        return JobStatus.FAILED, "Malformed synchronous completion envelope"
    if not isinstance(decoded, Mapping):
        return JobStatus.FAILED, "Unexpected synchronous completion payload"
    payload = (
        decoded.get("completion") if isinstance(decoded.get("completion"), Mapping) else decoded
    )
    if not isinstance(payload, Mapping):
        return JobStatus.FAILED, "Unexpected synchronous completion payload"
    if payload.get("success") is True:
        return JobStatus.SUCCEEDED, None
    raw_error = payload.get("error")
    return JobStatus.FAILED, str(raw_error) if raw_error is not None else "Task execution failed"


class SyncRunner(BaseRunner):
    """Execute tasks synchronously in-process without Ray.

    Implements the ``BaseRunner`` contract alongside ``RayJobRunner`` and
    ``RayCoreRunner`` so synchronous execution shares identical runtime-env
    validation, plan-identity binding, and strict completion encoding.
    """

    runner_family = "sync"
    supports_remote_reconciliation = False

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Mapping[str, Any]] | None = None,
        execute_fn: Callable[..., str] | None = None,
        runtime_env_resolver: Callable[[Any], Any] | None = None,
        max_tracked_handles: int = _MAX_TRACKED_HANDLES,
    ) -> None:
        self._settings_provider = settings_provider
        self._execute_fn = execute_fn
        self._runtime_env_resolver = runtime_env_resolver
        self._max_tracked_handles = max(1, int(max_tracked_handles))
        self._completed: OrderedDict[str, JobInfo] = OrderedDict()

    def _record_completion(self, handle: SyncSubmissionHandle) -> None:
        finished_at = datetime.now(UTC)
        self._completed[handle.ray_job_id] = JobInfo(
            job_id=handle.ray_job_id,
            status=handle.status,
            message=handle.error_message,
            start_time=handle.submitted_at,
            end_time=finished_at,
        )
        self._completed.move_to_end(handle.ray_job_id)
        while len(self._completed) > self._max_tracked_handles:
            self._completed.popitem(last=False)

    def _execute_serialized(
        self,
        *,
        task_execution: RayTaskExecution,
        callable_path: str,
        args_json: str,
        kwargs_json: str,
        input_reference: str | None,
    ) -> SyncSubmissionHandle:
        submitted_at = datetime.now(UTC)
        raw_pk = getattr(task_execution, "pk", None)
        task_pk = int(raw_pk) if raw_pk is not None else None
        raw_task_id = getattr(task_execution, "task_id", None)
        durable_task_id = str(raw_task_id) if raw_task_id is not None else None
        attempt_number = int(getattr(task_execution, "attempt_number", 1))
        execution_generation = int(getattr(task_execution, "execution_generation", 0))

        resolve_runtime_env = (
            self._runtime_env_resolver
            if self._runtime_env_resolver is not None
            else runtime_env_module.runtime_env_for_execution
        )
        settings_fn = (
            self._settings_provider
            if self._settings_provider is not None
            else conf_settings.get_settings
        )
        execute_fn = (
            self._execute_fn if self._execute_fn is not None else runtime_entrypoint.execute_task
        )

        runtime_env = resolve_runtime_env(task_execution)
        trust_identity = settings_fn().get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
        plan_runtime_env_identity = workflow_plans.runtime_env_plan_identity(
            runtime_env,
            trust_identity=trust_identity,
        )

        raw_protocol = getattr(task_execution, "execution_protocol_version", None)
        execution_protocol_version = (
            int(raw_protocol) if raw_protocol is not None else EXECUTION_PROTOCOL_VERSION
        )
        completion_identity = (
            ExecutionIdentity(
                task_execution_pk=task_pk,
                task_id=durable_task_id,
                attempt_number=attempt_number,
                execution_generation=execution_generation,
            )
            if task_pk is not None and durable_task_id is not None
            else None
        )

        result_json = execute_fn(
            callable_path=callable_path,
            serialized_args=args_json,
            serialized_kwargs=kwargs_json,
            task_execution_pk=task_pk,
            task_id=durable_task_id,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
            runtime_env_profile=runtime_env.profile,
            runtime_env_hash=runtime_env.digest,
            runtime_env_plan_identity=plan_runtime_env_identity.as_transport_dict(),
            input_reference=input_reference,
            ray_job_driver=False,
            _completion_identity=completion_identity,
            _execution_protocol_version=execution_protocol_version,
            _strict_execution_request=True,
        )
        status, error_message = _classify_sync_result(result_json)
        job_id = (
            f"sync:{task_pk}:{attempt_number}:{execution_generation}"
            if task_pk is not None
            else f"sync:adhoc:{attempt_number}:{execution_generation}"
        )
        handle = SyncSubmissionHandle(
            ray_job_id=job_id,
            ray_address="sync://local",
            submitted_at=submitted_at,
            result_json=result_json,
            status=status,
            error_message=error_message,
        )
        self._record_completion(handle)
        return handle

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SyncSubmissionHandle:
        """Execute a task synchronously using explicit Python arguments."""
        input_reference = getattr(task_execution, "input_reference", None)
        if input_reference:
            args_json = str(getattr(task_execution, "args_json", "[]"))
            kwargs_json = str(getattr(task_execution, "kwargs_json", "{}"))
        else:
            args_json = serialize_args(list(args))
            kwargs_json = serialize_args(kwargs)
        return self._execute_serialized(
            task_execution=task_execution,
            callable_path=callable_path,
            args_json=args_json,
            kwargs_json=kwargs_json,
            input_reference=input_reference,
        )

    def submit_durable(self, task_execution: RayTaskExecution) -> SyncSubmissionHandle:
        """Execute a persisted execution row using its stored opaque inputs."""
        return self._execute_serialized(
            task_execution=task_execution,
            callable_path=str(task_execution.callable_path),
            args_json=str(getattr(task_execution, "args_json", "[]")),
            kwargs_json=str(getattr(task_execution, "kwargs_json", "{}")),
            input_reference=getattr(task_execution, "input_reference", None),
        )

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Return the observed completion status of a synchronous execution."""
        if isinstance(handle, SyncSubmissionHandle):
            return JobInfo(
                job_id=handle.ray_job_id,
                status=handle.status,
                message=handle.error_message,
                start_time=handle.submitted_at,
            )
        recorded = self._completed.get(handle.ray_job_id)
        if recorded is not None:
            return recorded
        return JobInfo(
            job_id=handle.ray_job_id,
            status=JobStatus.UNKNOWN,
            message="Synchronous execution handle is not tracked by this runner instance",
        )

    def cancel(self, handle: SubmissionHandle) -> bool:
        """Return False because synchronous executions finish inline before returning."""
        del handle
        return False

    def cancel_with_status(self, handle: SubmissionHandle) -> CancellationOutcome:
        """Report remote cancellation as not applicable for inline synchronous tasks."""
        del handle
        return CancellationOutcome(
            CancellationOutcomeStatus.NOT_APPLICABLE,
            "Synchronous executions run inline and cannot be cancelled remotely",
        )

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Return None because synchronous executions log directly to the worker stream."""
        del handle
        return None

    def prepare_shutdown(self) -> None:
        """Drop tracked completion metadata during shutdown."""
        self._completed.clear()
