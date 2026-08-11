"""Ray Job Submission API runner implementation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured

from django_ray.conf.settings import get_settings
from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionRequestEncodeError,
    ExecutionRequestRejection,
    encode_execution_request,
)
from django_ray.ray_job_protocol import (
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    RayJobRequestBindingError,
    RayJobRequestBindingRejection,
    build_ray_job_request_reference_metadata,
    coordination_sha256,
    fixed_safe_ray_job_metadata,
)
from django_ray.ray_job_request_storage import (
    RayJobRequestStorageError,
    prepare_ray_job_request,
    register_and_attach_ray_job_request,
    release_ray_job_request_reservation,
)
from django_ray.redaction import materialize_exception_message, materialize_exception_text
from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.runner.errors import (
    RayJobRequestPreparationError,
    RayJobRequestPreparationRejection,
    RayJobSubmissionUncertainError,
)
from django_ray.runtime.runtime_env import (
    normalize_runtime_env,
    runtime_env_for_execution,
    snapshot_local_runtime_env,
)
from django_ray.runtime.serialization import serialize_args

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution


_CONTROL_REQUEST_TIMEOUT_SECONDS = 5.0
_REQUEST_TIMEOUT_ATTRIBUTE = "_django_ray_request_timeout_seconds"

_STORAGE_PREPARATION_REJECTIONS = {
    "invalid_locator": RayJobRequestPreparationRejection.INVALID_REQUEST,
    "resource_limit": RayJobRequestPreparationRejection.RESOURCE_LIMIT,
    "configuration": RayJobRequestPreparationRejection.CONFIGURATION,
    "storage_unavailable": RayJobRequestPreparationRejection.STORAGE_UNAVAILABLE,
    "integrity_mismatch": RayJobRequestPreparationRejection.INTEGRITY_MISMATCH,
    "invalid_request": RayJobRequestPreparationRejection.INVALID_REQUEST,
    "registry_mismatch": RayJobRequestPreparationRejection.REGISTRY_MISMATCH,
    "binding_mismatch": RayJobRequestPreparationRejection.BINDING_MISMATCH,
}


def _fixed_preparation_rejection(
    error: ExecutionRequestEncodeError | RayJobRequestBindingError | Exception,
) -> RayJobRequestPreparationRejection:
    """Map a fixed lower-layer classification without inspecting exception text."""
    classification = getattr(error, "classification", None)
    value = getattr(classification, "value", None)
    if isinstance(error, ExecutionRequestEncodeError):
        if classification is ExecutionRequestRejection.RESOURCE_LIMIT:
            return RayJobRequestPreparationRejection.RESOURCE_LIMIT
        return RayJobRequestPreparationRejection.INVALID_REQUEST
    if isinstance(error, RayJobRequestBindingError):
        if classification is RayJobRequestBindingRejection.RESOURCE_LIMIT:
            return RayJobRequestPreparationRejection.RESOURCE_LIMIT
        return RayJobRequestPreparationRejection.INVALID_REQUEST
    return _STORAGE_PREPARATION_REJECTIONS.get(
        value,
        RayJobRequestPreparationRejection.INVALID_REQUEST,
    )


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
        """Initialize Ray Job control without requiring submission storage."""
        try:
            settings = get_settings()
            ray_address = settings.get("RAY_ADDRESS")
            if type(ray_address) is not str or not ray_address.strip():
                raise ImproperlyConfigured("django-ray: RAY_ADDRESS must be a non-empty string")
        except ImproperlyConfigured as error:
            raise RayJobRequestPreparationError(
                RayJobRequestPreparationRejection.CONFIGURATION
            ) from error
        self.ray_address = ray_address

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
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.INVALID_REQUEST)
        try:
            digest = coordination_sha256(
                ExecutionIdentity(
                    task_execution_pk=int(task_execution.pk),
                    task_id=str(task_execution.task_id),
                    attempt_number=int(task_execution.attempt_number),
                    execution_generation=int(task_execution.execution_generation),
                )
            )
        except (TypeError, ValueError, OverflowError, RayJobRequestBindingError) as error:
            raise RayJobRequestPreparationError(
                RayJobRequestPreparationRejection.INVALID_REQUEST
            ) from error
        return f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{digest}"

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

    def _reserve_public_submission(
        self,
        task_execution: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        callable_path: str,
        args_json: str,
        kwargs_json: str,
        input_reference: str | None,
    ) -> bool:
        """Reserve rq2 for the public API under an exact claimed-row CAS."""
        from django.db import transaction

        from django_ray.models import RayTaskExecution, TaskState

        if not isinstance(task_execution, RayTaskExecution) or task_execution.pk is None:
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)
        if handle.ray_job_id != self.submission_id(task_execution):
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)
        database = task_execution._state.db or "default"
        expected_worker = task_execution.claimed_by_worker
        expected_runtime_profile = task_execution.runtime_env_profile
        expected_runtime_hash = task_execution.runtime_env_hash
        if type(expected_worker) is not str or not expected_worker:
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)

        with transaction.atomic(using=database):
            current = (
                RayTaskExecution.objects.using(database)
                .select_for_update()
                .filter(pk=task_execution.pk)
                .first()
            )
            selected_address = (
                getattr(current, "ray_target_address", None)
                or getattr(current, "ray_address", None)
                or self.ray_address
            )
            exact_row = current is not None and (
                current.state == TaskState.RUNNING
                and current.task_id == task_execution.task_id
                and current.attempt_number == task_execution.attempt_number
                and current.execution_generation == task_execution.execution_generation
                and current.execution_protocol_version == task_execution.execution_protocol_version
                and current.claimed_by_worker == expected_worker
                and current.callable_path == callable_path
                and current.args_json == args_json
                and current.kwargs_json == kwargs_json
                and current.input_reference == input_reference
                and current.runtime_env_profile == expected_runtime_profile
                and current.runtime_env_hash == expected_runtime_hash
                and selected_address == handle.ray_address
            )
            if not exact_row:
                raise RayJobRequestPreparationError(
                    RayJobRequestPreparationRejection.BINDING_MISMATCH
                )
            assert current is not None

            newly_reserved = current.ray_job_id is None
            if newly_reserved:
                if current.ray_address is not None or current.ray_job_request_reference is not None:
                    raise RayJobRequestPreparationError(
                        RayJobRequestPreparationRejection.BINDING_MISMATCH
                    )
                current.ray_job_id = handle.ray_job_id
                current.ray_address = handle.ray_address
                current.save(update_fields=["ray_job_id", "ray_address"], using=database)
            elif (
                current.ray_job_id != handle.ray_job_id or current.ray_address != handle.ray_address
            ):
                raise RayJobRequestPreparationError(
                    RayJobRequestPreparationRejection.BINDING_MISMATCH
                )

            task_execution.__dict__.update(current.__dict__)
            return newly_reserved

    @staticmethod
    def _release_public_submission(
        task_execution: RayTaskExecution,
        handle: SubmissionHandle,
    ) -> None:
        """Release one definitely unsubmitted public reservation exactly."""
        try:
            released = release_ray_job_request_reservation(
                task_execution,
                handle,
                expected_reference=getattr(
                    task_execution,
                    "ray_job_request_reference",
                    None,
                ),
            )
        except RayJobRequestStorageError as error:
            raise RayJobRequestPreparationError(_fixed_preparation_rejection(error)) from error
        if not released:
            raise RayJobRequestPreparationError(RayJobRequestPreparationRejection.BINDING_MISMATCH)

    def submit(
        self,
        task_execution: RayTaskExecution,
        callable_path: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> SubmissionHandle:
        """Submit from a persisted, RUNNING, manager-claimed execution row.

        The public path reserves its rq2 ID and address with an exact row CAS.
        It never submits an unclaimed row or falls back to the rq1 transport.
        An existing exact reservation raises an uncertain-acceptance result for
        reconciliation without issuing or releasing a second remote submission.
        """
        input_reference = getattr(task_execution, "input_reference", None)
        if input_reference:
            args_json = task_execution.args_json
            kwargs_json = task_execution.kwargs_json
        else:
            args_json = serialize_args(list(args))
            kwargs_json = serialize_args(kwargs)
        handle = self.submission_handle(task_execution)
        newly_reserved = self._reserve_public_submission(
            task_execution,
            handle,
            callable_path=callable_path,
            args_json=args_json,
            kwargs_json=kwargs_json,
            input_reference=input_reference,
        )
        if not newly_reserved:
            # Another caller owns the durable tuple but may not yet have crossed
            # the remote submission boundary. Never report definite submission,
            # submit it again, or release the other caller's reservation.
            raise RayJobSubmissionUncertainError(
                handle.ray_job_id,
                "durable submission reservation already exists",
            )
        try:
            return self._submit_serialized_request(
                task_execution=task_execution,
                callable_path=callable_path,
                args_json=args_json,
                kwargs_json=kwargs_json,
                input_reference=input_reference,
            )
        except RayJobSubmissionUncertainError:
            raise
        except Exception:
            if newly_reserved:
                self._release_public_submission(task_execution, handle)
            raise

    def submit_durable(self, task_execution: RayTaskExecution) -> SubmissionHandle:
        """Submit one already-reserved task-manager row using its opaque inputs."""
        return self._submit_serialized_request(
            task_execution=task_execution,
            callable_path=task_execution.callable_path,
            args_json=task_execution.args_json,
            kwargs_json=task_execution.kwargs_json,
            input_reference=getattr(task_execution, "input_reference", None),
        )

    def _submit_serialized_request(
        self,
        *,
        task_execution: RayTaskExecution,
        callable_path: str,
        args_json: str,
        kwargs_json: str,
        input_reference: str | None,
    ) -> SubmissionHandle:
        """Submit one canonical request without hydrating application input."""
        handle = self.submission_handle(task_execution)
        runtime_env = runtime_env_for_execution(task_execution)
        from django_ray.workflow_plans import runtime_env_plan_identity

        settings = get_settings()
        trust_identity = settings.get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
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
                try:
                    request_identity = ExecutionIdentity(
                        task_execution_pk=int(task_execution.pk),
                        task_id=str(task_execution.task_id),
                        attempt_number=int(task_execution.attempt_number),
                        execution_generation=int(task_execution.execution_generation),
                    )
                    execution_request = ExecutionRequest(
                        identity=request_identity,
                        execution_protocol_version=int(task_execution.execution_protocol_version),
                        callable_path=callable_path,
                        transport_version=2 if input_reference else 1,
                        serialized_args=args_json,
                        serialized_kwargs=kwargs_json,
                        input_reference=input_reference,
                        runtime_env_profile=runtime_env.profile,
                        runtime_env_hash=runtime_env.digest,
                        runtime_env_plan_identity=(
                            snapshot_runtime_env_identity.as_transport_dict()
                        ),
                        compiled_graph_submission_transport="ray-job",
                    )
                    payload_json = encode_execution_request(execution_request)
                except (TypeError, ValueError, OverflowError) as error:
                    rejection = (
                        _fixed_preparation_rejection(error)
                        if isinstance(error, ExecutionRequestEncodeError)
                        else RayJobRequestPreparationRejection.INVALID_REQUEST
                    )
                    raise RayJobRequestPreparationError(rejection) from error

                try:
                    prepared_request = prepare_ray_job_request(payload_json, settings)
                    metadata = build_ray_job_request_reference_metadata(
                        execution_request,
                        payload_json,
                        prepared_request.reference,
                        prepared_request.encoded_locator,
                    )
                    attached_reference = register_and_attach_ray_job_request(
                        prepared_request,
                        task_execution=task_execution,
                        submission_handle=handle,
                    )
                except (RayJobRequestStorageError, RayJobRequestBindingError) as error:
                    raise RayJobRequestPreparationError(
                        _fixed_preparation_rejection(error)
                    ) from error
                if attached_reference != prepared_request.reference:
                    raise RayJobRequestPreparationError(
                        RayJobRequestPreparationRejection.INTEGRITY_MISMATCH
                    )

                entrypoint = (
                    "python -m django_ray.runtime.entrypoint --request-ref-b64 "
                    f"{prepared_request.encoded_locator}"
                )

                # The canonical request and its durable binding are complete
                # before a Ray client is opened or any RuntimeEnv is uploaded.
                client = self._get_client(handle.ray_address)
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

                request_started = True
                returned_submission_id = client.submit_job(
                    entrypoint=entrypoint,
                    runtime_env=submitted_runtime_env.spec,
                    submission_id=handle.ray_job_id,
                    metadata=metadata,
                )
        except Exception as exc:
            if request_started:
                raise RayJobSubmissionUncertainError(
                    handle.ray_job_id,
                    f"submission request or post-request cleanup raised "
                    f"{materialize_exception_text(exc)}",
                ) from exc
            raise

        if (
            type(returned_submission_id) is not str
            or len(returned_submission_id) != len(handle.ray_job_id)
            or returned_submission_id != handle.ray_job_id
        ):
            raise RayJobSubmissionUncertainError(
                handle.ray_job_id,
                "submit_job returned an unexpected submission ID",
                observed_submission_id=None,
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
                metadata=fixed_safe_ray_job_metadata(getattr(info, "metadata", None)),
                driver_exit_code=(
                    getattr(info, "driver_exit_code", None)
                    if type(getattr(info, "driver_exit_code", None)) is int
                    else None
                ),
            )
        except Exception as e:
            return JobInfo(
                job_id=handle.ray_job_id,
                status=JobStatus.UNKNOWN,
                message=materialize_exception_message(e),
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
                f"Ray Job stop request raised {materialize_exception_text(exc)}",
            )

    def cancel_with_status(self, handle: SubmissionHandle) -> CancellationOutcome:
        """Request a Ray Job stop while preserving an indeterminate API result."""
        try:
            client = self.prepare_cancellation(handle)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Ray Job stop request raised {materialize_exception_text(exc)}",
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
