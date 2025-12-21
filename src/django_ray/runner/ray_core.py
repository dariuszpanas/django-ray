"""Ray Core runner implementation for high-throughput scenarios."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django_ray.runner.base import BaseRunner, JobInfo, SubmissionHandle

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


class RayCoreRunner(BaseRunner):
    """Runner that uses Ray Core remote functions.

    This runner is designed for high-throughput scenarios where
    the overhead of Ray Job Submission is too high.

    Note: This is a Phase 3 feature and is currently a stub.
    """

    def __init__(self) -> None:
        """Initialize the Ray Core runner."""
        # TODO: Initialize Ray connection and actor pool
        pass

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit a task via Ray Core remote function."""
        # TODO: Implement Ray Core submission
        raise NotImplementedError("Ray Core runner is not yet implemented")

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get status of a Ray Core task."""
        # TODO: Implement status tracking
        raise NotImplementedError("Ray Core runner is not yet implemented")

    def cancel(self, handle: SubmissionHandle) -> bool:
        """Cancel a Ray Core task."""
        # TODO: Implement cancellation
        raise NotImplementedError("Ray Core runner is not yet implemented")

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a Ray Core task."""
        # Ray Core doesn't have centralized logs like Job API
        return None
