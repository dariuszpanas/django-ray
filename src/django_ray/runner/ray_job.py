"""Ray Job Submission API runner implementation."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django_ray.conf.settings import get_settings
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.runner.errors import RayJobSubmissionUncertainError
from django_ray.runtime.runtime_env import (
    normalize_runtime_env,
    runtime_env_for_execution,
    snapshot_local_runtime_env,
)
from django_ray.runtime.serialization import serialize_args

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


_SUBMISSION_ID_PREFIX = "raysubmit_django_ray_v1_"
_CONTROL_REQUEST_TIMEOUT_SECONDS = 5.0
_REQUEST_TIMEOUT_ATTRIBUTE = "_django_ray_request_timeout_seconds"


def _find_auto_ray_address() -> str:
    """Resolve ``auto`` without allowing ``RAY_ADDRESS`` to replace it."""
    from ray._private import services

    gcs_addresses = services.find_gcs_addresses()
    bootstrap_address = services.find_bootstrap_address(None)

    # Match Ray's ``auto`` discovery order while deliberately omitting its
    # environment-variable precedence. An explicit backend target must remain
    # authoritative even when the task-manager process has global Ray settings.
    if len(gcs_addresses) > 1 and bootstrap_address is not None:
        return bootstrap_address
    if gcs_addresses:
        return next(iter(gcs_addresses))
    if bootstrap_address is not None:
        return bootstrap_address
    raise ConnectionError("Could not find any running Ray instance for the explicit 'auto' target.")


def _resolve_submission_address(ray_address: str) -> str:
    """Resolve one selected Ray target without consulting ambient Ray addresses."""
    from ray.dashboard.modules.dashboard_sdk import split_address
    from ray.dashboard.utils import (
        ray_address_to_api_server_url,
        ray_client_address_to_api_server_url,
    )

    if "://" in ray_address:
        module_name, _ = split_address(ray_address)
        if module_name == "ray":
            return ray_client_address_to_api_server_url(ray_address)
        return ray_address

    if ray_address == "auto":
        ray_address = _find_auto_ray_address()
    return ray_address_to_api_server_url(ray_address)


def _address_pinned_job_client(ray_address: str) -> Any:
    """Build a JobSubmissionClient whose explicit address cannot be overridden.

    Ray's public ``JobSubmissionClient`` constructor intentionally gives
    ``RAY_API_SERVER_ADDRESS`` and ``RAY_ADDRESS`` precedence over its address
    argument. That conflicts with django-ray's durable backend routing contract,
    so initialize the inherited HTTP submission client with an independently
    resolved endpoint instead.
    """
    import ray
    from ray.dashboard.modules.dashboard_sdk import SubmissionClient
    from ray.job_submission import JobSubmissionClient

    class _BoundedJobSubmissionClient(JobSubmissionClient):
        def _do_request(
            self,
            method: str,
            endpoint: str,
            *,
            data: bytes | None = None,
            json_data: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            timeout = getattr(self, _REQUEST_TIMEOUT_ATTRIBUTE, None)
            if timeout is not None:
                kwargs.setdefault("timeout", timeout)
            return super()._do_request(
                method,
                endpoint,
                data=data,
                json_data=json_data,
                **kwargs,
            )

    api_server_url = _resolve_submission_address(ray_address)
    client = _BoundedJobSubmissionClient.__new__(_BoundedJobSubmissionClient)
    client._client_ray_version = ray.__version__
    SubmissionClient.__init__(client, address=api_server_url)
    setattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, _CONTROL_REQUEST_TIMEOUT_SECONDS)
    try:
        client._check_connection_and_version(
            min_version="2.0",
            version_error_message=(
                f"Client Ray version {client._client_ray_version} is not compatible "
                "with the Ray cluster. Please ensure the cluster is running Ray 2.0 "
                "or higher or downgrade the client Ray version."
            ),
        )
    finally:
        setattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, None)
    return client


@contextmanager
def _bounded_control_requests(client: Any) -> Iterator[None]:
    """Bound Ray dashboard control calls that may run under a database row lock."""
    previous_timeout = getattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, None)
    setattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, _CONTROL_REQUEST_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        setattr(client, _REQUEST_TIMEOUT_ATTRIBUTE, previous_timeout)


class RayJobRunner(BaseRunner):
    """Runner that uses Ray Job Submission API."""

    def __init__(self) -> None:
        """Initialize the Ray Job runner."""
        settings = get_settings()
        self.ray_address = settings["RAY_ADDRESS"]

    def _get_client(self, ray_address: str | None = None) -> Any:
        """Get a Ray JobSubmissionClient for the requested cluster.

        ``RayTaskExecution.ray_target_address`` snapshots an explicit backend
        alias target. Keep the process setting as a fallback when an alias did
        not select one, and do not let Ray's process environment replace either
        selected address.
        """
        return _address_pinned_job_client(ray_address or self.ray_address)

    @staticmethod
    def submission_id(task_execution: RayTaskExecution) -> str:
        """Return the stable Ray submission ID for one execution generation."""
        if task_execution.pk is None:
            raise ValueError("Ray Job submission requires a persisted task execution")

        identity = json.dumps(
            {
                "attempt_number": int(task_execution.attempt_number),
                "execution_generation": int(task_execution.execution_generation),
                "task_execution_pk": int(task_execution.pk),
                "task_id": str(task_execution.task_id),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{_SUBMISSION_ID_PREFIX}{digest}"

    def submission_handle(self, task_execution: RayTaskExecution) -> SubmissionHandle:
        """Build the exact handle that :meth:`submit` will give to Ray."""
        ray_address = (
            getattr(task_execution, "ray_target_address", None)
            or getattr(task_execution, "ray_address", None)
            or self.ray_address
        )
        return SubmissionHandle(
            ray_job_id=self.submission_id(task_execution),
            ray_address=ray_address,
            submitted_at=datetime.now(UTC),
        )

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit a task via Ray Job Submission API."""
        handle = self.submission_handle(task_execution)
        client = self._get_client(handle.ray_address)

        runtime_env = runtime_env_for_execution(task_execution)
        from django_ray.conf.settings import get_settings
        from django_ray.workflow_plans import runtime_env_plan_identity

        trust_identity = get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
        source_runtime_env_identity = runtime_env_plan_identity(
            runtime_env,
            trust_identity=trust_identity,
        )
        request_started = False
        try:
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

                request_started = True
                returned_submission_id = client.submit_job(
                    entrypoint=entrypoint,
                    runtime_env=submitted_runtime_env.spec,
                    submission_id=handle.ray_job_id,
                    metadata={
                        "django_ray_task_id": str(task_execution.pk),
                        "django_ray_attempt_number": str(task_execution.attempt_number),
                        "django_ray_execution_generation": str(task_execution.execution_generation),
                        "callable_path": callable_path,
                        "runtime_env_profile": runtime_env.profile or "",
                        "runtime_env_hash": runtime_env.digest,
                    },
                )
        except Exception as exc:
            if request_started:
                raise RayJobSubmissionUncertainError(
                    handle.ray_job_id,
                    f"submission request or post-request cleanup raised "
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            raise

        if returned_submission_id != handle.ray_job_id:
            raise RayJobSubmissionUncertainError(
                handle.ray_job_id,
                f"submit_job returned the unexpected ID {returned_submission_id!r}",
                observed_submission_id=returned_submission_id,
            )
        return handle

    def get_status(self, handle: SubmissionHandle) -> JobInfo:
        """Get status of a Ray job."""
        try:
            client = self._get_client(handle.ray_address)
            with _bounded_control_requests(client):
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

    def prepare_cancellation(self, handle: SubmissionHandle) -> Any:
        """Resolve the address-pinned control client before a caller takes a row lock."""
        return self._get_client(handle.ray_address)

    def cancel_prepared_with_status(
        self,
        handle: SubmissionHandle,
        client: Any,
    ) -> CancellationOutcome:
        """Stop a Ray Job through a previously resolved, address-pinned client."""
        try:
            with _bounded_control_requests(client):
                stopped = client.stop_job(handle.ray_job_id)
            if stopped:
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
            return CancellationOutcome(
                CancellationOutcomeStatus.NOT_APPLICABLE,
                "Ray Job was not running when the stop request arrived",
            )
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Ray Job stop request raised {type(exc).__name__}: {exc}",
            )

    def cancel_with_status(self, handle: SubmissionHandle) -> CancellationOutcome:
        """Request a Ray Job stop while preserving an indeterminate API result."""
        try:
            client = self.prepare_cancellation(handle)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Ray Job stop request raised {type(exc).__name__}: {exc}",
            )
        return self.cancel_prepared_with_status(handle, client)

    def get_logs(self, handle: SubmissionHandle) -> str | None:
        """Get logs from a Ray job."""
        try:
            client = self._get_client(handle.ray_address)
            with _bounded_control_requests(client):
                return client.get_job_logs(handle.ray_job_id)
        except Exception:
            return None
