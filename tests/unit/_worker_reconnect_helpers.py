"""Shared command fakes, payloads, and lease mixin for worker reconnect tests."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from typing import Any

import pytest

from django_ray.execution_codec import (
    ExecutionCompletion,
    ExecutionIdentity,
    ExecutionRequest,
    encode_execution_completion,
)
from django_ray.execution_protocol import (
    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    WORKER_CAPABILITY_SCHEMA_VERSION,
)
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import (
    RayTaskExecution,
    TaskState,
    TaskWorkerLease,
)
from django_ray.ray_job_protocol import (
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    STRICT_RAY_JOB_SUBMISSION_ID_PREFIX,
    build_ray_job_request_metadata,
    build_ray_job_request_reference_metadata,
)
from django_ray.ray_job_request_storage import (
    RayJobRequestLocator,
    encode_ray_job_request_locator,
    ray_job_request_reference_content_identity,
)
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runner.ray_core import RayCoreCompletion, RayCoreHandle

_USE_REAL_COMMAND_LEASES = False


def _make_command(
    worker_id: str = "worker-coverage",
    *,
    claim_ownerless_tasks: bool = True,
) -> Command:
    """Build a command and, in DB tests, its exact pre-existing worker lease."""
    cmd = Command()
    cmd.stdout = StringIO()
    cmd.style = cmd.style
    cmd.worker_id = worker_id
    cmd.sync_mode = False
    cmd.execution_mode = "local"
    cmd.cluster_address = None
    cmd.active_tasks = {}
    cmd.ray_core_runner = None
    if _USE_REAL_COMMAND_LEASES:
        now = datetime.now(UTC)
        lease, _ = TaskWorkerLease.objects.get_or_create(
            worker_id=worker_id,
            defaults={
                "hostname": "worker-coverage-host",
                "pid": 12345,
                "queue_name": "default",
                "capability_schema_version": WORKER_CAPABILITY_SCHEMA_VERSION,
                "django_ray_version": "test",
                "min_supported_execution_protocol_version": (
                    MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION
                ),
                "max_supported_execution_protocol_version": (
                    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION
                ),
                "legacy_admission_token": None,
                "started_at": now,
                "last_heartbeat_at": now,
                "is_active": True,
            },
        )
        cmd.lease = lease
        cmd.lease_identity = WorkerLeaseIdentity(
            worker_id=str(lease.worker_id),
            hostname=str(lease.hostname),
            pid=int(lease.pid),
            started_at=lease.started_at,
        )
        if claim_ownerless_tasks:
            RayTaskExecution.objects.filter(
                state__in=(TaskState.RUNNING, TaskState.CANCELLING),
                claimed_by_worker__isnull=True,
            ).update(claimed_by_worker=worker_id)
    return cmd


def _pending_handle(
    task: RayTaskExecution,
    *,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> RayCoreHandle:
    return RayCoreHandle(
        task_pk=task.pk,
        object_ref=object(),
        submitted_at=datetime.now(UTC),
        task_name="test",
        attempt_number=task.attempt_number if attempt_number is None else attempt_number,
        execution_generation=(
            task.execution_generation if execution_generation is None else execution_generation
        ),
    )


def _ray_job_handle(
    task: RayTaskExecution,
    job_id: str,
    address: str = "ray://cluster:10001",
) -> SubmissionHandle:
    return SubmissionHandle(
        ray_job_id=job_id,
        ray_address=address,
        submitted_at=datetime.now(UTC),
    )


def _completion(
    task: RayTaskExecution,
    result_json: str,
    *,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> RayCoreCompletion:
    return RayCoreCompletion(
        task_pk=task.pk,
        attempt_number=task.attempt_number if attempt_number is None else attempt_number,
        execution_generation=(
            task.execution_generation if execution_generation is None else execution_generation
        ),
        result_json=result_json,
    )


def _versioned_completion_json(
    task: RayTaskExecution,
    *,
    task_id: str | None = None,
    success: bool = True,
    result: Any = 3,
    error: str | None = None,
    retryable: bool | None = None,
    executor_version: str = "0.5.0-executor",
) -> str:
    assert task.pk is not None
    return encode_execution_completion(
        ExecutionCompletion(
            identity=ExecutionIdentity(
                task_execution_pk=int(task.pk),
                task_id=task_id or str(task.task_id),
                attempt_number=int(task.attempt_number),
                execution_generation=int(task.execution_generation),
            ),
            execution_protocol_version=int(task.execution_protocol_version),
            executor_django_ray_version=executor_version,
            success=success,
            result=result if success else None,
            result_reference=None,
            error=error if not success else None,
            traceback=None,
            exception_type="builtins.RuntimeError" if not success else None,
            retryable=retryable if not success else None,
        )
    )


def _strict_ray_job_id(suffix: str = "a") -> str:
    """Return one canonical strict submission ID for focused worker tests."""
    assert len(suffix) == 1 and suffix in "0123456789abcdef"
    return f"{STRICT_RAY_JOB_SUBMISSION_ID_PREFIX}{suffix * 64}"


def _rq2_ray_job_id(suffix: str = "a") -> str:
    """Return one canonical request-reference submission ID."""
    assert len(suffix) == 1 and suffix in "0123456789abcdef"
    return f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{suffix * 64}"


def _rq2_ray_job_id_for_metadata(metadata: dict[str, str]) -> str:
    """Return the canonical rq2 submission ID bound by strict metadata."""
    return (
        f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}"
        f"{metadata['django_ray_coordination_sha256']}"
    )


def _rq2_request_reference(digest: str, *, size_bytes: int = 128) -> str:
    """Return one canonical retrievable filesystem request reference."""
    assert len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)
    relative_path = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
    return f"resultfs://sha256/{digest}?rel={relative_path}&bytes={size_bytes}"


def _strict_ray_job_metadata(
    task: RayTaskExecution,
    *,
    task_id: str | None = None,
    execution_protocol_version: int | None = None,
) -> dict[str, str]:
    """Build an independently parseable strict request binding for ``task``."""
    assert task.pk is not None
    request = ExecutionRequest(
        identity=ExecutionIdentity(
            task_execution_pk=int(task.pk),
            task_id=task_id or str(task.task_id),
            attempt_number=int(task.attempt_number),
            execution_generation=int(task.execution_generation),
        ),
        execution_protocol_version=(
            int(task.execution_protocol_version)
            if execution_protocol_version is None
            else execution_protocol_version
        ),
        callable_path=task.callable_path,
        transport_version=2 if task.input_reference else 1,
        serialized_args=task.args_json,
        serialized_kwargs=task.kwargs_json,
        input_reference=task.input_reference,
        runtime_env_profile=task.runtime_env_profile,
        runtime_env_hash=task.runtime_env_hash,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )
    return build_ray_job_request_metadata(request, '{"bounded":"request"}')


def _rq2_ray_job_metadata(
    task: RayTaskExecution,
    *,
    request_reference: str | None = None,
    task_id: str | None = None,
) -> dict[str, str]:
    """Build rq2 metadata bound to one durable execution and request reference."""
    assert task.pk is not None
    reference = request_reference or task.ray_job_request_reference
    assert reference is not None
    request = ExecutionRequest(
        identity=ExecutionIdentity(
            task_execution_pk=int(task.pk),
            task_id=task_id or str(task.task_id),
            attempt_number=int(task.attempt_number),
            execution_generation=int(task.execution_generation),
        ),
        execution_protocol_version=int(task.execution_protocol_version),
        callable_path=task.callable_path,
        transport_version=2 if task.input_reference else 1,
        serialized_args=task.args_json,
        serialized_kwargs=task.kwargs_json,
        input_reference=task.input_reference,
        runtime_env_profile=task.runtime_env_profile,
        runtime_env_hash=task.runtime_env_hash,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )
    request_digest, request_size_bytes = ray_job_request_reference_content_identity(reference)
    locator = RayJobRequestLocator(
        backend="filesystem",
        reference=reference,
        digest=request_digest,
        size_bytes=request_size_bytes,
        filesystem_path="/var/lib/django-ray/requests",
    )
    metadata = build_ray_job_request_reference_metadata(
        request,
        '{"bounded":"request"}',
        reference,
        encode_ray_job_request_locator(locator),
    )
    metadata["django_ray_request_sha256"] = request_digest
    metadata["django_ray_request_size_bytes"] = str(request_size_bytes)
    return metadata


class RealCommandLeaseTestCase:
    """Make DB-backed command fixtures follow the production lease boundary."""

    @pytest.fixture(autouse=True)
    def _use_real_command_leases(self) -> Any:
        global _USE_REAL_COMMAND_LEASES

        previous = _USE_REAL_COMMAND_LEASES
        _USE_REAL_COMMAND_LEASES = True
        try:
            yield
        finally:
            _USE_REAL_COMMAND_LEASES = previous
