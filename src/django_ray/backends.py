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

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.exceptions import TaskResultDoesNotExist

from django_ray.logging import get_backend_logger
from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.serialization import serialize_args

if TYPE_CHECKING:
    from django.tasks.base import Task

# Module-level logger
logger = get_backend_logger()


# Map our internal TaskState to Django's TaskResultStatus
STATE_TO_STATUS: dict[str, TaskResultStatus] = {
    TaskState.QUEUED: TaskResultStatus.READY,
    TaskState.RUNNING: TaskResultStatus.RUNNING,
    TaskState.SUCCEEDED: TaskResultStatus.SUCCESSFUL,
    TaskState.FAILED: TaskResultStatus.FAILED,
    TaskState.CANCELLED: TaskResultStatus.FAILED,
    TaskState.CANCELLING: TaskResultStatus.RUNNING,
    TaskState.LOST: TaskResultStatus.FAILED,
}


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
        - RAY_ADDRESS: Ray cluster address (default: "auto")
        - RAY_RUNTIME_ENV: Runtime environment for Ray workers
    """

    # Backend capabilities
    supports_defer = True  # We support run_after via the database
    supports_async_task = False  # Not yet implemented
    supports_get_result = True  # We track results in the database
    supports_priority = False  # Not yet implemented

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        """Initialize the Ray task backend.

        Args:
            alias: The backend alias (e.g., "default")
            params: Configuration parameters from TASKS setting
        """
        super().__init__(alias, params)

        # Extract Ray-specific options
        options = params.get("OPTIONS", {})
        self.ray_address = options.get("RAY_ADDRESS", "auto")
        self.ray_runtime_env = options.get("RAY_RUNTIME_ENV", {})

    def enqueue(
        self,
        task: Task,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> TaskResult:
        """Queue up a task to be executed on Ray.

        This method creates a RayTaskExecution record in the database
        which will be picked up by the django_ray_worker process and
        submitted to Ray for execution.

        Args:
            task: The Django Task object to enqueue
            args: Positional arguments for the task function
            kwargs: Keyword arguments for the task function

        Returns:
            TaskResult object with task status and metadata
        """
        # Generate a unique task ID
        task_id = str(uuid.uuid4())

        # Get the callable path for the task function
        callable_path = task.module_path

        # Serialize arguments
        args_json = serialize_args(list(args))
        kwargs_json = serialize_args(kwargs)

        # Create the task execution record
        now = datetime.now(timezone.utc)
        execution = RayTaskExecution.objects.create(
            task_id=task_id,
            callable_path=callable_path,
            queue_name=task.queue_name,
            state=TaskState.QUEUED,
            args_json=args_json,
            kwargs_json=kwargs_json,
            run_after=task.run_after,
            ray_address=self.ray_address,
            created_at=now,
        )

        logger.info(
            "Task enqueued",
            extra={
                "task_id": task_id,
                "callable_path": callable_path,
                "queue_name": task.queue_name,
                "run_after": str(task.run_after) if task.run_after else None,
            },
        )

        # Return a TaskResult object
        return self._execution_to_result(execution, task)

    def get_result(self, result_id: str) -> TaskResult:
        """Retrieve a task result by ID.

        Args:
            result_id: The unique task result ID

        Returns:
            TaskResult object with current task status

        Raises:
            TaskResultDoesNotExist: If no task with the given ID exists
        """
        try:
            execution = RayTaskExecution.objects.get(task_id=result_id)
        except RayTaskExecution.DoesNotExist:
            raise TaskResultDoesNotExist(f"Task result {result_id} does not exist")

        # Reconstruct the Task object from the execution record
        task = self._reconstruct_task(execution)

        return self._execution_to_result(execution, task)

    def _execution_to_result(
        self,
        execution: RayTaskExecution,
        task: Task,
    ) -> TaskResult:
        """Convert a RayTaskExecution to a Django TaskResult.

        Args:
            execution: The database execution record
            task: The Django Task object

        Returns:
            TaskResult object
        """
        from django.tasks.base import TaskError

        # Parse errors if task failed
        errors: list[TaskError] = []
        if execution.error_message:
            # Extract exception class from traceback or use generic Exception
            exception_class_path = "builtins.Exception"
            if execution.error_traceback:
                # Try to extract actual exception class from traceback
                lines = execution.error_traceback.strip().split("\n")
                if lines:
                    last_line = lines[-1]
                    if ":" in last_line:
                        exception_class_path = last_line.split(":")[0].strip()
                        # Handle common exception format like "ValueError: message"
                        if "." not in exception_class_path:
                            exception_class_path = f"builtins.{exception_class_path}"

            errors.append(
                TaskError(
                    exception_class_path=exception_class_path,
                    traceback=execution.error_traceback or execution.error_message,
                )
            )

        # Parse args/kwargs from JSON
        try:
            args = json.loads(execution.args_json)
        except (json.JSONDecodeError, TypeError):
            args = []

        try:
            kwargs = json.loads(execution.kwargs_json)
        except (json.JSONDecodeError, TypeError):
            kwargs = {}

        # Get worker IDs
        worker_ids: list[str] = []
        if execution.claimed_by_worker:
            worker_ids.append(execution.claimed_by_worker)

        # Map state to status
        status = STATE_TO_STATUS.get(execution.state, TaskResultStatus.READY)

        # Create the result object
        result = TaskResult(
            task=task,
            id=execution.task_id,
            status=status,
            enqueued_at=execution.created_at,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            last_attempted_at=execution.started_at,
            args=args,
            kwargs=kwargs,
            backend=self.alias,
            errors=errors,
            worker_ids=worker_ids,
        )

        # Set return value if task succeeded
        if execution.state == TaskState.SUCCEEDED and execution.result_data:
            try:
                return_value = json.loads(execution.result_data)
                object.__setattr__(result, "_return_value", return_value)
            except (json.JSONDecodeError, TypeError):
                pass

        return result

    def _reconstruct_task(self, execution: RayTaskExecution) -> Task:
        """Reconstruct a Django Task object from an execution record.

        Args:
            execution: The database execution record

        Returns:
            Task object
        """
        from django.tasks.base import DEFAULT_TASK_PRIORITY, Task

        # Import the function from the callable path
        from django_ray.runtime.import_utils import import_callable

        func = import_callable(execution.callable_path)

        return Task(
            priority=DEFAULT_TASK_PRIORITY,
            func=func,
            backend=self.alias,
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
                    hint="Install ray with: pip install ray",
                    id="django_ray.E001",
                )
            )

        return errors
