"""Base runner interface for task execution backends."""

from __future__ import annotations

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


class BaseRunner(ABC):
    """Abstract base class for task execution runners."""

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
            task_execution: The task execution model instance.
            callable_path: Dotted path to the task callable.
            args: Positional arguments for the task.
            kwargs: Keyword arguments for the task.

        Returns:
            SubmissionHandle with job tracking information.
        """

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
