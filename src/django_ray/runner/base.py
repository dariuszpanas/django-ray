"""Base runner interface for task execution backends."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


class JobStatus(Enum):
    """Status of a Ray job."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


@dataclass
class SubmissionHandle:
    """Handle for a submitted task execution."""

    ray_job_id: str
    ray_address: str
    submitted_at: datetime


@dataclass
class JobInfo:
    """Information about a Ray job."""

    job_id: str
    status: JobStatus
    message: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    metadata: dict[str, str] | None = None
    driver_exit_code: int | None = None


class BaseRunner(ABC):
    """Abstract base class for task execution runners.

    All concrete execution backends (``RayJobRunner``, ``RayCoreRunner``, and
    ``SyncRunner``) inherit this contract so worker orchestration can dispatch
    persisted executions through ``submit_durable()`` and share uniform
    status, cancellation, and shutdown hooks.
    """

    runner_family: str = "custom"
    supports_remote_reconciliation: bool = True

    @abstractmethod
    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit a task for execution.

        Args:
            task_execution: The persisted task execution model instance. The
                Ray Job runner requires an already claimed RUNNING row before
                it can reserve an external submission safely.
            callable_path: Dotted path to the task callable.
            args: Positional arguments for the task.
            kwargs: Keyword arguments for the task.

        Returns:
            SubmissionHandle with job tracking information.
        """

    def submit_durable(self, task_execution: RayTaskExecution) -> SubmissionHandle:
        """Submit a persisted execution row using its stored serialized inputs.

        Runners that support external input references or opaque payload
        forwarding (``RayJobRunner``, ``RayCoreRunner``, ``SyncRunner``)
        override this method directly. Subclasses that only implement
        ``submit()`` inherit this inline-JSON adapter automatically.
        """
        if getattr(task_execution, "input_reference", None):
            raise NotImplementedError(
                f"{type(self).__name__}.submit_durable() must be overridden to "
                "support external input_reference payloads"
            )
        raw_args = json.loads(str(getattr(task_execution, "args_json", "[]") or "[]"))
        raw_kwargs = json.loads(str(getattr(task_execution, "kwargs_json", "{}") or "{}"))
        if not isinstance(raw_args, list) or not isinstance(raw_kwargs, dict):
            raise ValueError("Task execution args_json/kwargs_json must decode to list/dict")
        return self.submit(
            task_execution=task_execution,
            callable_path=str(task_execution.callable_path),
            args=tuple(raw_args),
            kwargs=dict(raw_kwargs),
        )

    @abstractmethod
    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get the status of a submitted job.

        Args:
            handle: The submission handle from submit().

        Returns:
            JobInfo with current status.
        """

    @abstractmethod
    def cancel(self, handle: SubmissionHandle) -> bool:
        """Cancel a running job.

        Args:
            handle: The submission handle from submit().

        Returns:
            True if cancellation was initiated successfully.
        """

    @abstractmethod
    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a job execution.

        Args:
            handle: The submission handle from submit().

        Returns:
            Log content or None if unavailable.
        """

    def prepare_shutdown(self) -> None:
        """Release runner-local resources during graceful worker shutdown."""
        return None
