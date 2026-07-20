"""Ray Job Submission API runner implementation."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django_ray.conf.settings import get_settings
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.runtime.runtime_env import (
    normalize_runtime_env,
    runtime_env_for_execution,
    snapshot_local_runtime_env,
)
from django_ray.runtime.serialization import serialize_args

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


class RayJobRunner(BaseRunner):
    """Runner that uses Ray Job Submission API."""

    def __init__(self) -> None:
        """Initialize the Ray Job runner."""
        settings = get_settings()
        self.ray_address = settings["RAY_ADDRESS"]

    def _get_client(self, ray_address: str | None = None) -> Any:
        """Get a Ray JobSubmissionClient for the requested cluster.

        ``RayTaskExecution.ray_address`` is persisted when a task is queued so
        backend aliases can target different Ray clusters.  Keep the process
        setting as a fallback for callers that do not have a persisted address
        (for example, direct runner usage and older records).
        """
        from ray.job_submission import JobSubmissionClient

        return JobSubmissionClient(ray_address or self.ray_address)

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit a task via Ray Job Submission API."""
        ray_address = getattr(task_execution, "ray_address", None) or self.ray_address
        client = self._get_client(ray_address)

        runtime_env = runtime_env_for_execution(task_execution)
        from django_ray.conf.settings import get_settings
        from django_ray.workflow_plans import runtime_env_plan_identity

        trust_identity = get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
        source_runtime_env_identity = runtime_env_plan_identity(
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
                != source_runtime_env_identity.manifest["digest"]
            ):
                from django_ray.workflow_plans import WorkflowPlanMismatchError

                raise WorkflowPlanMismatchError(
                    "Outer RuntimeEnv immutable snapshot differs from its effective plan"
                )
            submitted_spec = json.loads(immutable_snapshot.serialized)
            client._upload_working_dir_if_needed(submitted_spec)
            client._upload_py_modules_if_needed(submitted_spec)
            from ray.runtime_env import RuntimeEnv

            submitted_runtime_env = normalize_runtime_env(
                RuntimeEnv(**submitted_spec).to_dict(),
                profile=runtime_env.profile,
                source=f"prepared Ray Job RuntimeEnv for task {task_execution.pk}",
            )
            verified_source_identity = runtime_env_plan_identity(
                runtime_env,
                trust_identity=trust_identity,
            )
            if (
                verified_source_identity.manifest["digest"]
                != source_runtime_env_identity.manifest["digest"]
            ):
                from django_ray.workflow_plans import WorkflowPlanMismatchError

                raise WorkflowPlanMismatchError(
                    "Outer RuntimeEnv local content changed while it was being snapshotted"
                )

            # Inline tasks retain the unversioned v1 transport during rolling
            # upgrades. Referenced tasks use v2 so the command line remains small.
            payload: dict[str, Any] = {
                "callable_path": callable_path,
                "task_execution_pk": task_execution.pk,
                "attempt_number": task_execution.attempt_number,
                "execution_generation": task_execution.execution_generation,
                "runtime_env_profile": runtime_env.profile,
                "runtime_env_hash": runtime_env.digest,
                "runtime_env_plan_identity": snapshot_runtime_env_identity.as_transport_dict(),
            }
            input_reference = getattr(task_execution, "input_reference", None)
            if input_reference:
                payload.update(
                    {
                        "transport_version": 2,
                        "input_reference": input_reference,
                    }
                )
            else:
                payload.update(
                    {
                        "serialized_args": serialize_args(list(args)),
                        "serialized_kwargs": serialize_args(kwargs),
                    }
                )
            payload_json = json.dumps(payload, separators=(",", ":"))
            payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")

            entrypoint = f"python -m django_ray.runtime.entrypoint --payload-b64 {payload_b64}"

            job_id = client.submit_job(
                entrypoint=entrypoint,
                runtime_env=submitted_runtime_env.spec,
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
            ray_address=ray_address,
            submitted_at=datetime.now(UTC),
        )

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get status of a Ray job."""
        client = self._get_client(handle.ray_address)

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
        return self.cancel_with_status(handle).status == CancellationOutcomeStatus.REQUESTED

    def cancel_with_status(self, handle: SubmissionHandle) -> CancellationOutcome:
        """Request a Ray Job stop while preserving an indeterminate API result."""
        client = self._get_client(handle.ray_address)

        try:
            client.stop_job(handle.ray_job_id)
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Ray Job stop request raised {type(exc).__name__}: {exc}",
            )

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a Ray job."""
        client = self._get_client(handle.ray_address)

        try:
            return client.get_job_logs(handle.ray_job_id)
        except Exception:
            return None
