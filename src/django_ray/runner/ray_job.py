"""Ray Job Submission API runner implementation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django_ray.conf.settings import get_settings
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle
from django_ray.runtime.serialization import serialize_args

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


class RayJobRunner(BaseRunner):
    """Runner that uses Ray Job Submission API."""

    def __init__(self) -> None:
        """Initialize the Ray Job runner."""
        settings = get_settings()
        self.ray_address = settings["RAY_ADDRESS"]

    def _get_client(self) -> Any:
        """Get Ray JobSubmissionClient."""
        from ray.job_submission import JobSubmissionClient

        return JobSubmissionClient(self.ray_address)

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit a task via Ray Job Submission API."""
        client = self._get_client()

        serialized_args = serialize_args(list(args))
        serialized_kwargs = serialize_args(kwargs)

        # Build entrypoint command
        entrypoint = (
            f'python -c "'
            f"from django_ray.runtime.entrypoint import execute_task; "
            f"print(execute_task('{callable_path}', '{serialized_args}', '{serialized_kwargs}'))"
            f'"'
        )

        # Get runtime environment settings
        settings = get_settings()
        runtime_env = settings.get("RAY_RUNTIME_ENV", {})

        job_id = client.submit_job(
            entrypoint=entrypoint,
            runtime_env=runtime_env,
            metadata={
                "django_ray_task_id": str(task_execution.pk),
                "callable_path": callable_path,
            },
        )

        return SubmissionHandle(
            ray_job_id=job_id,
            ray_address=self.ray_address,
            submitted_at=datetime.now(UTC),
        )

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get status of a Ray job."""
        client = self._get_client()

        try:
            status = client.get_job_status(handle.ray_job_id)
            info = client.get_job_info(handle.ray_job_id)

            status_map = {
                "PENDING": JobStatus.PENDING,
                "RUNNING": JobStatus.RUNNING,
                "SUCCEEDED": JobStatus.SUCCEEDED,
                "FAILED": JobStatus.FAILED,
                "STOPPED": JobStatus.STOPPED,
            }

            return JobInfo(
                job_id=handle.ray_job_id,
                status=status_map.get(str(status), JobStatus.UNKNOWN),
                message=getattr(info, "message", None),
                start_time=getattr(info, "start_time", None),
                end_time=getattr(info, "end_time", None),
            )
        except Exception as e:
            return JobInfo(
                job_id=handle.ray_job_id,
                status=JobStatus.UNKNOWN,
                message=str(e),
            )

    def cancel(self, handle: SubmissionHandle) -> bool:
        """Cancel a Ray job."""
        client = self._get_client()

        try:
            client.stop_job(handle.ray_job_id)
            return True
        except Exception:
            return False

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a Ray job."""
        client = self._get_client()

        try:
            return client.get_job_logs(handle.ray_job_id)
        except Exception:
            return None
