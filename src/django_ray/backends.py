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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection, transaction
from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.exceptions import TaskResultDoesNotExist

from django_ray.conf.defaults import QUEUE_TIMEOUT_SECONDS_MAX
from django_ray.conf.settings import get_settings
from django_ray.input_storage import (
    InputPayloadError,
    load_task_input,
    prepare_task_input,
    register_task_input,
)
from django_ray.logging import get_backend_logger
from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.runtime_env import (
    resolve_runtime_env_profile,
    runtime_env_for_storage,
)

if TYPE_CHECKING:
    from django.tasks.base import Task

# Module-level logger
logger = get_backend_logger()

_TASK_ID_ALLOCATION_ATTEMPTS = 3
_TASK_ID_UNIQUE_CONSTRAINT = "ray_task_id_unique"
_SQLITE_TASK_ID_UNIQUE_ERROR = "UNIQUE constraint failed: django_ray_raytaskexecution.task_id"


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
        self.ray_address = options.get("RAY_ADDRESS", "auto")
        ray_target_address = options.get(
            "RAY_ADDRESS",
            get_settings()["RAY_ADDRESS"],
        )
        if not isinstance(ray_target_address, str) or not ray_target_address.strip():
            raise ImproperlyConfigured(
                "django-ray: TASKS backend OPTIONS['RAY_ADDRESS'] must be a non-empty string"
            )
        self.ray_target_address = ray_target_address
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
        # The database is the authority for uniqueness. UUIDv4 keeps collisions
        # vanishingly rare, while the bounded retry below makes a collision a
        # recoverable allocation event instead of an ambiguous durable identity.
        task_id = str(uuid.uuid4())

        # Get the callable path for the task function
        callable_path = task.module_path

        runtime_env = resolve_runtime_env_profile(
            self.runtime_env_profile,
            inline_spec=self.inline_runtime_env,
        )
        stored_runtime_env = runtime_env_for_storage(runtime_env, task_id=task_id)
        prepared_input = prepare_task_input(list(args), kwargs)

        now = datetime.now(UTC)
        eligible_at = max(now, task.run_after) if task.run_after is not None else now
        queue_deadline_at = (
            eligible_at + timedelta(seconds=self.queue_timeout_seconds)
            if self.queue_timeout_seconds is not None
            else None
        )
        with transaction.atomic():
            register_task_input(prepared_input)
            for allocation_attempt in range(1, _TASK_ID_ALLOCATION_ATTEMPTS + 1):
                try:
                    # Keep the expected unique violation inside a savepoint so
                    # PostgreSQL leaves the outer input-registration transaction
                    # usable for a replacement candidate.
                    with transaction.atomic():
                        execution = RayTaskExecution.objects.create(
                            task_id=task_id,
                            callable_path=callable_path,
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
                            timeout_seconds=self.timeout_seconds,
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
                break

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
                "timeout_seconds": self.timeout_seconds,
                "input_external": prepared_input.input_reference is not None,
                "input_size_bytes": prepared_input.size_bytes,
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
            execution = RayTaskExecution.objects.defer("runtime_env_json").get(task_id=result_id)
        except RayTaskExecution.DoesNotExist:
            raise TaskResultDoesNotExist(f"Task result {result_id} does not exist") from None

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
        if (
            execution.state in (TaskState.FAILED, TaskState.LOST, TaskState.EXPIRED)
            and execution.error_message
        ):
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

        try:
            args, kwargs = load_task_input(
                args_json=execution.args_json,
                kwargs_json=execution.kwargs_json,
                input_reference=execution.input_reference,
            )
        except InputPayloadError as error:
            logger.warning(
                "Failed to load durable task input",
                extra={
                    "task_id": execution.task_id,
                    "input_external": execution.input_reference is not None,
                    "error": str(error),
                },
            )
            args = []
            kwargs = {}

        # Get worker IDs
        worker_ids: list[str] = []
        if execution.claimed_by_worker:
            worker_ids.append(execution.claimed_by_worker)

        # Map state to status
        status = STATE_TO_STATUS.get(str(execution.state), TaskResultStatus.READY)

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
        if execution.state == TaskState.SUCCEEDED:
            serialized_result = execution.result_data
            if not serialized_result and execution.result_reference:
                try:
                    from django_ray.result_storage import ResultStorageError, load_result_reference

                    serialized_result = load_result_reference(str(execution.result_reference))
                except ResultStorageError as e:
                    logger.warning(
                        "Failed to load external task result",
                        extra={
                            "task_id": execution.task_id,
                            "error": str(e),
                        },
                    )

            if serialized_result:
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

        return result

    def _reconstruct_task(self, execution: RayTaskExecution) -> Task:
        """Reconstruct a Django Task object from an execution record.

        Args:
            execution: The database execution record

        Returns:
            Task object
        """
        from django.tasks.base import Task

        # Import the function from the callable path
        from django_ray.runtime.import_utils import import_callable

        func = import_callable(execution.callable_path)

        return Task(
            priority=execution.priority,
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
