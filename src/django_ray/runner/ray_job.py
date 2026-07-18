"""Ray Job Submission API runner implementation."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django_ray.conf.settings import get_settings
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle
from django_ray.runtime.runtime_env import runtime_env_for_execution
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
        runtime_env = runtime_env_for_execution(task_execution)

        # Transport payload as urlsafe base64 to avoid shell quoting/injection issues.
        payload = {
            "callable_path": callable_path,
            "serialized_args": serialized_args,
            "serialized_kwargs": serialized_kwargs,
            "task_execution_pk": task_execution.pk,
            "attempt_number": task_execution.attempt_number,
            "execution_generation": task_execution.execution_generation,
            "runtime_env_profile": runtime_env.profile,
            "runtime_env_hash": runtime_env.digest,
        }
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")

        entrypoint = f"python -m django_ray.runtime.entrypoint --payload-b64 {payload_b64}"

        job_id = client.submit_job(
            entrypoint=entrypoint,
            runtime_env=runtime_env.spec,
            metadata={
                "django_ray_task_id": str(task_execution.pk),
                "django_ray_attempt_number": str(task_execution.attempt_number),
                "django_ray_execution_generation": str(task_execution.execution_generation),
                "callable_path": callable_path,
                "runtime_env_profile": runtime_env.profile or "",
                "runtime_env_hash": runtime_env.digest,
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
