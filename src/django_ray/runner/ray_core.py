"""Ray Core runner implementation for high-throughput scenarios."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import BoundedSemaphore, Event, Thread
from typing import TYPE_CHECKING, Any

from django_ray.redaction import materialize_exception_message, safe_exception_type_name
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution
    from django_ray.runner.cancellation import CancellationOutcome


@dataclass(frozen=True)
class RayCoreHandle:
    """Handle for tracking Ray Core task execution."""

    task_pk: int
    object_ref: Any  # ray.ObjectRef
    submitted_at: datetime
    task_name: str
    ray_job_id: str = ""  # The worker's Ray job ID (e.g., "02000000")
    ray_task_id: str = ""  # The task's Ray ID (e.g., "67a2e8cfa5a06db3ffff...")
    attempt_number: int = field(kw_only=True)
    execution_generation: int = field(kw_only=True)


@dataclass(frozen=True)
class RayCoreCompletion:
    """Result envelope tied to the exact durable execution submitted to Ray."""

    task_pk: int
    attempt_number: int
    execution_generation: int
    result_json: str


@dataclass
class _RayCoreSubmissionHandle(SubmissionHandle):
    """Submission handle carrying an in-memory capability for one exact task."""

    pending_handle: RayCoreHandle


# Global remote function cache to prevent Ray GCS memory leaks.
# Ray caches remote function definitions, so dynamically decorating functions
# inside hot paths like submit_task or _RayExecutor.__init__ causes OOMs.

_execute_django_task_remote_cached = None

# Ray Client's terminate-task RPC has no caller-visible timeout in Ray 2.56.
# Keep the worker's database ownership locks bounded while matching the Ray Job
# control-request budget used by the other execution backend.
_RAY_CORE_CANCEL_TIMEOUT_SECONDS = 5.0
_RAY_CORE_CANCEL_SLOT = BoundedSemaphore(value=1)


def _ray_id_to_string(ray_id: Any) -> str:
    """Return the stable hexadecimal representation preferred by Ray APIs."""
    if ray_id is None:
        return ""
    hex_method = getattr(ray_id, "hex", None)
    if callable(hex_method):
        return str(hex_method())
    return str(ray_id)


def _get_remote_execute_django_task() -> Any:
    global _execute_django_task_remote_cached
    if _execute_django_task_remote_cached is None:
        import ray

        from django_ray.runtime.remote import execute_django_task_remote

        _execute_django_task_remote_cached = ray.remote(execute_django_task_remote)
    return _execute_django_task_remote_cached


def _discard_remote_execute_django_task() -> None:
    """Discard a Ray Client definition that failed before submission."""
    global _execute_django_task_remote_cached
    _execute_django_task_remote_cached = None


def _compiled_graph_submission_transport(ray: Any) -> str | None:
    """Describe the live Ray connection without trusting a configured address."""
    from django_ray.runtime.compiled_graph import CompiledGraphSubmissionTransport

    try:
        client_connected = ray.util.client.ray.is_connected()
    except Exception:
        return None
    if client_connected is True:
        return CompiledGraphSubmissionTransport.RAY_CLIENT.value
    if client_connected is not False:
        return None
    try:
        ray_initialized = ray.is_initialized()
    except Exception:
        return None
    if ray_initialized is True:
        return CompiledGraphSubmissionTransport.DIRECT_RAY_CORE.value
    return None


class RayCoreRunner(BaseRunner):
    """Runner that uses Ray Core remote functions.

    This runner is designed for high-throughput scenarios where
    the overhead of Ray Job Submission is too high. It uses
    `ray.remote` to execute tasks directly on Ray workers.

    Unlike Ray Job API which provides process isolation, Ray Core
    runs tasks in shared worker processes for lower latency.
    """

    # Class-level tracking of pending tasks
    _pending_tasks: dict[int, RayCoreHandle] = field(default_factory=dict)
    _ray_initialized: bool = False

    def __init__(self) -> None:
        """Initialize the Ray Core runner."""
        self._pending_tasks: dict[int, RayCoreHandle] = {}
        self._ensure_ray_initialized()

    def _ensure_ray_initialized(self) -> None:
        """Ensure Ray is initialized."""
        import ray

        if not ray.is_initialized():
            # Get address from environment or use auto
            address = os.environ.get("RAY_ADDRESS", "auto")
            ray.init(address=address, ignore_reinit_error=True)

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit a task via Ray Core remote function.

        Args:
            task_execution: The task execution model instance.
            callable_path: Dotted path to the callable.
            args: Positional arguments for the task.
            kwargs: Keyword arguments for the task.

        Returns:
            SubmissionHandle for tracking the task.
        """
        existing_handle = self._pending_tasks.get(task_execution.pk)
        if existing_handle is not None:
            raise RuntimeError(
                f"Task execution {task_execution.pk} already has a pending Ray Core "
                f"submission for attempt {existing_handle.attempt_number}, generation "
                f"{existing_handle.execution_generation}"
            )

        import ray

        from django_ray.runtime.serialization import serialize_args

        input_reference = getattr(task_execution, "input_reference", None)
        if input_reference:
            args_json = task_execution.args_json
            kwargs_json = task_execution.kwargs_json
        else:
            args_json = serialize_args(list(args))
            kwargs_json = serialize_args(kwargs)

        # Extract task name for Ray dashboard visibility
        task_name = callable_path.split(".")[-1] if callable_path else "task"

        from django_ray.runtime.runtime_env import (
            prepare_runtime_env_for_ray_core,
            runtime_env_for_execution,
            snapshot_local_runtime_env,
        )

        # Keep the executor importable at module scope so Ray can reuse worker
        # processes without serializing a new nested function for every task.
        runtime_env = runtime_env_for_execution(task_execution)
        from django_ray.conf.settings import get_settings
        from django_ray.workflow_plans import runtime_env_plan_identity

        trust_identity = get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
        plan_runtime_env_identity = runtime_env_plan_identity(
            runtime_env,
            trust_identity=trust_identity,
        )
        with snapshot_local_runtime_env(runtime_env) as immutable_snapshot:
            snapshot_runtime_env_identity = runtime_env_plan_identity(
                immutable_snapshot,
                trust_identity=trust_identity,
            )
            if (
                snapshot_runtime_env_identity.manifest["digest"]
                != plan_runtime_env_identity.manifest["digest"]
            ):
                from django_ray.workflow_plans import WorkflowPlanMismatchError

                raise WorkflowPlanMismatchError(
                    "Outer RuntimeEnv immutable snapshot differs from its effective plan"
                )
            submitted_runtime_env = prepare_runtime_env_for_ray_core(immutable_snapshot)
        verified_runtime_env_identity = runtime_env_plan_identity(
            runtime_env,
            trust_identity=trust_identity,
        )
        if (
            verified_runtime_env_identity.manifest["digest"]
            != plan_runtime_env_identity.manifest["digest"]
        ):
            from django_ray.workflow_plans import WorkflowPlanMismatchError

            raise WorkflowPlanMismatchError(
                "Outer RuntimeEnv local content changed while it was being packaged"
            )
        # Ray Client deserializes the remote function on the server before its
        # task-level RuntimeEnv exists. Ship this small bootstrap function by
        # value so a generic Ray head does not need django-ray installed.
        cloudpickle = getattr(ray, "cloudpickle", None)
        if cloudpickle is not None:
            import django_ray.runtime.remote as remote_module

            cloudpickle.register_pickle_by_value(remote_module)

        remote_options: dict[str, Any] = {"name": f"django_ray:{task_name}"}
        if submitted_runtime_env:
            remote_options["runtime_env"] = submitted_runtime_env

        execute_django_task = _get_remote_execute_django_task().options(**remote_options)

        # Submit to Ray (non-blocking)
        submitted_at = datetime.now(UTC)
        submitted_attempt_number = int(task_execution.attempt_number)
        submitted_execution_generation = int(task_execution.execution_generation)
        try:
            object_ref = execute_django_task.remote(
                callable_path,
                args_json,
                kwargs_json,
                task_execution.pk,
                runtime_env.profile,
                runtime_env.digest,
                input_reference,
                attempt_number=submitted_attempt_number,
                execution_generation=submitted_execution_generation,
                runtime_env_plan_identity=snapshot_runtime_env_identity.as_transport_dict(),
                compiled_graph_submission_transport=_compiled_graph_submission_transport(ray),
            )
        except Exception:
            # Ray Client leaves a failed ClientRemoteFunc in an in-progress
            # state. Reusing it turns the original serialization error into an
            # unrelated InProgressSentinel failure on the next attempt.
            _discard_remote_execute_django_task()
            raise

        # Get Ray job ID (the worker's client connection job ID)
        ray_job_id = ""
        ray_task_id = ""
        try:
            # Get the current job ID from Ray runtime context
            ctx = ray.get_runtime_context()
            ray_job_id = _ray_id_to_string(ctx.get_job_id())
            # Get the task ID from the ObjectRef
            # The hex() returns 56 chars but Ray Dashboard uses only first 48
            full_hex = object_ref.hex()
            ray_task_id = full_hex[:48] if len(full_hex) >= 48 else full_hex
        except Exception:
            pass

        # Track the pending task
        handle = RayCoreHandle(
            task_pk=task_execution.pk,
            object_ref=object_ref,
            submitted_at=submitted_at,
            task_name=task_name,
            attempt_number=submitted_attempt_number,
            execution_generation=submitted_execution_generation,
            ray_job_id=ray_job_id,
            ray_task_id=ray_task_id,
        )
        self._pending_tasks[task_execution.pk] = handle

        # Build a composite ID that includes both job and task IDs for dashboard linking
        # Format: job_id:task_id (e.g., "02000000:67a2e8cfa5a06db3ffff...")
        composite_id = (
            f"{ray_job_id}:{ray_task_id}"
            if ray_job_id and ray_task_id
            else f"ray_core:{task_execution.pk}"
        )

        # Return a SubmissionHandle for compatibility with BaseRunner interface
        return _RayCoreSubmissionHandle(
            ray_job_id=composite_id,
            ray_address=os.environ.get("RAY_ADDRESS", "auto"),
            submitted_at=submitted_at,
            pending_handle=handle,
        )

    @staticmethod
    def _build_composite_id(handle: RayCoreHandle) -> str | None:
        """Build dashboard-friendly composite ID for a pending Ray Core handle."""
        if handle.ray_job_id and handle.ray_task_id:
            return f"{handle.ray_job_id}:{handle.ray_task_id}"
        return None

    def _resolve_task_pk(self, handle_id: str) -> int | None:
        """Resolve task PK from either legacy or composite Ray Core handle ID."""
        if handle_id.startswith("ray_core:"):
            try:
                return int(handle_id.split(":", 1)[1])
            except (TypeError, ValueError):
                return None

        # Composite format: job_id:task_id used for dashboard deep-linking.
        if ":" in handle_id and not handle_id.startswith("raysubmit_"):
            for task_pk, pending in self._pending_tasks.items():
                composite_id = self._build_composite_id(pending)
                if composite_id and composite_id == handle_id:
                    return task_pk

        return None

    @staticmethod
    def _is_canonical_handle_id(handle_id: str) -> bool:
        """Return whether an ID has the supported Ray job/task composite shape."""
        if handle_id.startswith(("ray_core:", "raysubmit_")):
            return False
        job_id, separator, task_id = handle_id.partition(":")
        return bool(separator and job_id and task_id)

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get status of a Ray Core task.

        Args:
            handle: The submission handle from submit().

        Returns:
            JobInfo with current task status.
        """
        import ray

        if isinstance(handle, _RayCoreSubmissionHandle):
            core_handle = handle.pending_handle
            current_handle = self._pending_tasks.get(core_handle.task_pk)
            if current_handle is not core_handle:
                return JobInfo(
                    job_id=handle.ray_job_id,
                    status=JobStatus.UNKNOWN,
                    message=(
                        "Submission handle is no longer tracked"
                        if current_handle is None
                        else "Submission handle no longer identifies the pending task"
                    ),
                )
            task_pk = core_handle.task_pk
        else:
            task_pk = self._resolve_task_pk(handle.ray_job_id)
            if task_pk is None:
                if self._is_canonical_handle_id(handle.ray_job_id):
                    return JobInfo(
                        job_id=handle.ray_job_id,
                        status=JobStatus.UNKNOWN,
                        message="Submission handle is not tracked by this runner",
                    )
                return JobInfo(
                    job_id=handle.ray_job_id,
                    status=JobStatus.FAILED,
                    message="Invalid handle format",
                )

            if handle.ray_job_id.startswith("ray_core:"):
                if task_pk not in self._pending_tasks:
                    return JobInfo(
                        job_id=handle.ray_job_id,
                        status=JobStatus.UNKNOWN,
                        message="Legacy handle lacks exact submission identity",
                    )
                return JobInfo(
                    job_id=handle.ray_job_id,
                    status=JobStatus.UNKNOWN,
                    message="Legacy handle lacks exact submission identity",
                )
            core_handle = self._pending_tasks[task_pk]

        # Check if task is ready (non-blocking)
        ready, _ = ray.wait([core_handle.object_ref], timeout=0)

        if not ready:
            return JobInfo(job_id=handle.ray_job_id, status=JobStatus.RUNNING)

        # Task is ready - get result and determine status
        try:
            result_json = ray.get(core_handle.object_ref)
            result = json.loads(result_json)

            # Remove from pending
            if self._pending_tasks.get(task_pk) is core_handle:
                self._pending_tasks.pop(task_pk, None)

            if result.get("success"):
                return JobInfo(
                    job_id=handle.ray_job_id, status=JobStatus.SUCCEEDED, message=result_json
                )
            else:
                return JobInfo(
                    job_id=handle.ray_job_id,
                    status=JobStatus.FAILED,
                    message=result.get("error", "Task failed"),
                )
        except Exception as e:
            # Remove from pending on error
            if self._pending_tasks.get(task_pk) is core_handle:
                self._pending_tasks.pop(task_pk, None)
            return JobInfo(
                job_id=handle.ray_job_id,
                status=JobStatus.FAILED,
                message=materialize_exception_message(e),
            )

    def cancel(self, handle: SubmissionHandle) -> bool:
        """Cancel a Ray Core task.

        Uses graceful cancellation (force=False) which raises TaskCancelledError
        in the task. This allows the task to clean up and doesn't kill the worker.

        Args:
            handle: The submission handle from submit().

        Returns:
            True if cancellation was initiated.
        """
        if isinstance(handle, _RayCoreSubmissionHandle):
            return self.cancel_pending(handle.pending_handle)

        task_pk = self._resolve_task_pk(handle.ray_job_id)
        if task_pk is None:
            return False

        # A persisted legacy ID names only a database row, not one exact
        # in-memory Ray submission. Durable task controls resolve the current
        # attempt and generation through get_pending_handle()/cancel_pending().
        if handle.ray_job_id.startswith("ray_core:"):
            return False

        if task_pk not in self._pending_tasks:
            return False

        return self.cancel_pending(self._pending_tasks[task_pk])

    def cancel_pending(self, handle: RayCoreHandle) -> bool:
        """Cancel one exact pending handle within the bounded control window."""
        from django_ray.runner.cancellation import CancellationOutcomeStatus

        outcome = self.cancel_pending_with_status(handle)
        return outcome.status == CancellationOutcomeStatus.REQUESTED

    def cancel_pending_with_status(
        self,
        handle: RayCoreHandle,
        *,
        timeout_seconds: float | None = None,
    ) -> CancellationOutcome:
        """Bound cancellation of one immutable ObjectRef and report uncertainty.

        Ray Client can wait indefinitely inside ``ray.cancel``. Run only that
        exact remote effect in a daemon thread, then retire the matching local
        handle from the caller thread on every result, including timeout. A
        late daemon completion cannot mutate tracking or target a replacement.
        """
        import ray
        from ray.exceptions import RayTaskError

        from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus

        if self._pending_tasks.get(handle.task_pk) is not handle:
            return CancellationOutcome(CancellationOutcomeStatus.NOT_APPLICABLE)

        timeout = (
            _RAY_CORE_CANCEL_TIMEOUT_SECONDS
            if timeout_seconds is None
            else max(float(timeout_seconds), 0.0)
        )
        if not _RAY_CORE_CANCEL_SLOT.acquire(blocking=False):
            if self._pending_tasks.get(handle.task_pk) is handle:
                self._pending_tasks.pop(handle.task_pk, None)
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                "Ray Core cancellation capacity is occupied; the exact stop was not attempted",
            )

        completed = Event()
        result = [
            CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                "Ray Core cancellation ended without a known outcome",
            )
        ]

        def request_exact_cancellation() -> None:
            try:
                # Graceful cancellation raises TaskCancelledError in the task
                # instead of killing its shared Ray worker process.
                ray.cancel(handle.object_ref, force=False)
                result[0] = CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
            except (RuntimeError, RayTaskError):
                # The task may already have completed or failed.
                result[0] = CancellationOutcome(
                    CancellationOutcomeStatus.FAILED,
                    "Ray Core cancellation was rejected or the task was already terminal",
                )
            except Exception as exc:  # pragma: no cover - defensive backend boundary
                result[0] = CancellationOutcome(
                    CancellationOutcomeStatus.INDETERMINATE,
                    f"Ray Core cancellation raised {safe_exception_type_name(exc)}",
                )
            finally:
                _RAY_CORE_CANCEL_SLOT.release()
                completed.set()

        request_thread = Thread(
            target=request_exact_cancellation,
            name=(
                f"django-ray-core-cancel-{handle.task_pk}-"
                f"{handle.attempt_number}-{handle.execution_generation}"
            ),
            daemon=True,
        )
        try:
            request_thread.start()
        except RuntimeError:
            _RAY_CORE_CANCEL_SLOT.release()
            if self._pending_tasks.get(handle.task_pk) is handle:
                self._pending_tasks.pop(handle.task_pk, None)
            return CancellationOutcome(
                CancellationOutcomeStatus.FAILED,
                "Ray Core cancellation worker could not start",
            )

        finished = completed.wait(timeout=timeout)
        if self._pending_tasks.get(handle.task_pk) is handle:
            self._pending_tasks.pop(handle.task_pk, None)
        if not finished:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                "Ray Core cancellation timed out; the exact stop may complete later",
            )
        return result[0]

    def get_pending_handle(
        self,
        task_pk: int,
        *,
        attempt_number: int,
        execution_generation: int,
    ) -> RayCoreHandle | None:
        """Return a pending handle only when its full execution identity matches."""
        handle = self._pending_tasks.get(task_pk)
        if (
            handle is None
            or handle.attempt_number != attempt_number
            or handle.execution_generation != execution_generation
        ):
            return None
        return handle

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a Ray Core task.

        Ray Core doesn't have centralized logs like Job API.
        Logs are written to stdout/stderr on the Ray worker.

        Returns:
            None (logs not available through this interface).
        """
        return None

    def retire_pending_handle(self, handle: RayCoreHandle) -> bool:
        """Forget one exact local capability without contacting Ray."""
        if self._pending_tasks.get(handle.task_pk) is not handle:
            return False
        self._pending_tasks.pop(handle.task_pk, None)
        return True

    def poll_completed(
        self,
        handles: tuple[RayCoreHandle, ...] | None = None,
    ) -> list[RayCoreCompletion]:
        """Poll for completed tasks and return their results.

        This is a convenience method for the worker to efficiently
        check multiple pending tasks at once. When ``handles`` is provided,
        only exact capabilities that remain current are allowed to cross the
        Ray boundary; a replacement or newly submitted handle is not polled.

        Returns:
            Completion envelopes carrying the exact submitted execution identity.
        """
        import ray

        if handles is None:
            selected_handles = tuple(self._pending_tasks.values())
        else:
            selected_by_task = {
                handle.task_pk: handle
                for handle in handles
                if self._pending_tasks.get(handle.task_pk) is handle
            }
            selected_handles = tuple(selected_by_task.values())

        if not selected_handles:
            return []

        # Get all pending object refs
        refs = [handle.object_ref for handle in selected_handles]
        handle_by_ref = {handle.object_ref: handle for handle in selected_handles}

        # Check for completed tasks (non-blocking)
        ready, _ = ray.wait(refs, num_returns=len(refs), timeout=0)

        completed = []
        for ref in ready:
            handle = handle_by_ref[ref]
            try:
                result_json = ray.get(ref)
                completed.append(
                    RayCoreCompletion(
                        task_pk=handle.task_pk,
                        attempt_number=handle.attempt_number,
                        execution_generation=handle.execution_generation,
                        result_json=result_json,
                    )
                )
            except Exception as e:
                # Return error as JSON
                error_result = json.dumps(
                    {
                        "success": False,
                        "result": None,
                        "error": materialize_exception_message(e),
                        "traceback": None,
                        "exception_type": type(e).__module__ + "." + type(e).__name__,
                    }
                )
                completed.append(
                    RayCoreCompletion(
                        task_pk=handle.task_pk,
                        attempt_number=handle.attempt_number,
                        execution_generation=handle.execution_generation,
                        result_json=error_result,
                    )
                )

            # Remove from pending
            self.retire_pending_handle(handle)

        return completed

    @property
    def pending_count(self) -> int:
        """Get the number of pending tasks."""
        return len(self._pending_tasks)

    @property
    def pending_task_ids(self) -> tuple[int, ...]:
        """Return a stable snapshot of task IDs currently owned by this runner.

        Worker orchestration must not depend on the runner's private tracking
        dictionary.  This read-only snapshot is intentionally detached so a
        poll or shutdown operation can safely mutate the runner afterward.
        """
        return tuple(self._pending_tasks)

    @property
    def pending_task_handles(self) -> tuple[RayCoreHandle, ...]:
        """Return pending handles with their immutable submission identities."""
        return tuple(self._pending_tasks.values())

    def clear_pending_tasks(self) -> None:
        """Forget all locally tracked tasks after a connection loss or handoff."""
        self._pending_tasks.clear()
