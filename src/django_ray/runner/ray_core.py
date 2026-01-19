"""Ray Core runner implementation for high-throughput scenarios."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


@dataclass
class RayCoreHandle:
    """Handle for tracking Ray Core task execution."""

    task_pk: int
    object_ref: Any  # ray.ObjectRef
    submitted_at: datetime
    task_name: str


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
        import ray

        from django_ray.runtime.serialization import serialize_args

        # Serialize arguments
        args_json = serialize_args(list(args))
        kwargs_json = serialize_args(kwargs)

        # Extract task name for Ray dashboard visibility
        task_name = callable_path.split(".")[-1] if callable_path else "task"

        # Define the remote function
        @ray.remote(name=f"django_ray:{task_name}")
        def execute_django_task(
            callable_path: str,
            args_json: str,
            kwargs_json: str,
            task_id: int,
        ) -> str:
            """Execute a Django task on a Ray worker."""
            import json

            print(f"[Task {task_id}] Starting: {callable_path}", flush=True)

            from django_ray.runtime.entrypoint import execute_task

            result = execute_task(callable_path, args_json, kwargs_json)

            # Print result for visibility in Ray dashboard
            parsed = json.loads(result)
            if parsed.get("success"):
                print(f"[Task {task_id}] SUCCESS: {parsed.get('result')}", flush=True)
            else:
                print(
                    f"[Task {task_id}] FAILED: {parsed.get('error')}",
                    file=sys.stderr,
                    flush=True,
                )

            return result

        # Submit to Ray (non-blocking)
        submitted_at = datetime.now(UTC)
        object_ref = execute_django_task.remote(
            callable_path,
            args_json,
            kwargs_json,
            task_execution.pk,
        )

        # Track the pending task
        handle = RayCoreHandle(
            task_pk=task_execution.pk,
            object_ref=object_ref,
            submitted_at=submitted_at,
            task_name=task_name,
        )
        self._pending_tasks[task_execution.pk] = handle

        # Return a SubmissionHandle for compatibility with BaseRunner interface
        return SubmissionHandle(
            ray_job_id=f"ray_core:{task_execution.pk}",
            ray_address=os.environ.get("RAY_ADDRESS", "auto"),
            submitted_at=submitted_at,
        )

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get status of a Ray Core task.

        Args:
            handle: The submission handle from submit().

        Returns:
            JobInfo with current task status.
        """
        import ray

        # Extract task_pk from handle
        if not handle.ray_job_id.startswith("ray_core:"):
            return JobInfo(status=JobStatus.FAILED, message="Invalid handle format")

        task_pk = int(handle.ray_job_id.split(":")[1])

        if task_pk not in self._pending_tasks:
            # Task not tracked - might be completed and removed
            return JobInfo(status=JobStatus.SUCCEEDED)

        core_handle = self._pending_tasks[task_pk]

        # Check if task is ready (non-blocking)
        ready, _ = ray.wait([core_handle.object_ref], timeout=0)

        if not ready:
            return JobInfo(status=JobStatus.RUNNING)

        # Task is ready - get result and determine status
        try:
            result_json = ray.get(core_handle.object_ref)
            result = json.loads(result_json)

            # Remove from pending
            del self._pending_tasks[task_pk]

            if result.get("success"):
                return JobInfo(status=JobStatus.SUCCEEDED, message=result_json)
            else:
                return JobInfo(
                    status=JobStatus.FAILED,
                    message=result.get("error", "Task failed"),
                )
        except Exception as e:
            # Remove from pending on error
            self._pending_tasks.pop(task_pk, None)
            return JobInfo(status=JobStatus.FAILED, message=str(e))

    def cancel(self, handle: SubmissionHandle) -> bool:
        """Cancel a Ray Core task.

        Args:
            handle: The submission handle from submit().

        Returns:
            True if cancellation was initiated.
        """
        import ray

        if not handle.ray_job_id.startswith("ray_core:"):
            return False

        task_pk = int(handle.ray_job_id.split(":")[1])

        if task_pk not in self._pending_tasks:
            return False

        core_handle = self._pending_tasks[task_pk]

        try:
            ray.cancel(core_handle.object_ref, force=True)
            del self._pending_tasks[task_pk]
            return True
        except Exception:
            return False

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a Ray Core task.

        Ray Core doesn't have centralized logs like Job API.
        Logs are written to stdout/stderr on the Ray worker.

        Returns:
            None (logs not available through this interface).
        """
        return None

    def poll_completed(self) -> list[tuple[int, str]]:
        """Poll for completed tasks and return their results.

        This is a convenience method for the worker to efficiently
        check multiple pending tasks at once.

        Returns:
            List of (task_pk, result_json) tuples for completed tasks.
        """
        import ray

        if not self._pending_tasks:
            return []

        # Get all pending object refs
        refs = [h.object_ref for h in self._pending_tasks.values()]
        pk_by_ref = {h.object_ref: h.task_pk for h in self._pending_tasks.values()}

        # Check for completed tasks (non-blocking)
        ready, _ = ray.wait(refs, num_returns=len(refs), timeout=0)

        completed = []
        for ref in ready:
            task_pk = pk_by_ref[ref]
            try:
                result_json = ray.get(ref)
                completed.append((task_pk, result_json))
            except Exception as e:
                # Return error as JSON
                error_result = json.dumps(
                    {
                        "success": False,
                        "result": None,
                        "error": str(e),
                        "traceback": None,
                        "exception_type": type(e).__module__ + "." + type(e).__name__,
                    }
                )
                completed.append((task_pk, error_result))

            # Remove from pending
            del self._pending_tasks[task_pk]

        return completed

    @property
    def pending_count(self) -> int:
        """Get the number of pending tasks."""
        return len(self._pending_tasks)
