"""Shared concurrency and fixture helpers for PostgreSQL coordination tests."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from threading import Barrier, Event, Lock, get_ident
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext

from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.input_storage import (
    prepare_task_input,
)
from django_ray.models import (
    RayTaskExecution,
    TaskState,
)
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
from django_ray.runtime.context import DurableTaskContext, WorkflowRunIdentity
from django_ray.workflow.progress.runs import (
    allocate_workflow_run,
)

_SYNTHETIC_V1_V2_PROTOCOLS = ExecutionProtocolRange(minimum=1, maximum=2)


def _run_concurrently(*operations: Callable[[], object]) -> list[object]:
    """Run database operations on independent connections after one barrier."""
    barrier = Barrier(len(operations))

    def invoke(operation: Callable[[], object]) -> object:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return operation()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=20) for future in futures]


def _wait_for_postgresql_lock(backend_pid: int) -> tuple[str | None, str | None]:
    """Require one recovery backend to block on the winner's transaction lock."""
    import time

    lock_wait: tuple[str | None, str | None] | None = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT wait_event_type, wait_event
                FROM pg_stat_activity
                WHERE pid = %s
                """,
                [backend_pid],
            )
            lock_wait = cursor.fetchone()
        if lock_wait is not None and lock_wait[0] == "Lock":
            break
        time.sleep(0.01)

    assert lock_wait is not None
    assert lock_wait[0] == "Lock"
    return lock_wait


def _synchronize_first_predicate_call(
    predicate: Callable[[RayTaskExecution], bool],
    barrier: Barrier,
) -> Callable[[RayTaskExecution], bool]:
    """Make both recoverers evaluate the same stale snapshot before locking."""
    seen_threads: set[int] = set()
    guard = Lock()

    def synchronized(task: RayTaskExecution) -> bool:
        result = predicate(task)
        thread_id = get_ident()
        with guard:
            first_call = thread_id not in seen_threads
            seen_threads.add(thread_id)
        if first_call:
            barrier.wait(timeout=10)
        return result

    return synchronized


def _run_contended_recovery(
    recoverers: list[Any],
    operation: Callable[[Any], object],
    *,
    effect_started: Event,
    release_effect: Event,
    effect_workers: list[str],
) -> list[object]:
    """Hold the winner's effect until the loser demonstrably waits on its lock."""
    backend_pids: dict[str, int] = {}
    backend_guard = Lock()

    def invoke(recoverer: Any) -> object:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                backend_pid = int(cursor.fetchone()[0])
            with backend_guard:
                backend_pids[recoverer.worker_id] = backend_pid
            return operation(recoverer)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke, recoverer) for recoverer in recoverers]
        try:
            assert effect_started.wait(timeout=10)
            assert len(effect_workers) == 1
            winner = effect_workers[0]
            loser = next(
                recoverer.worker_id for recoverer in recoverers if recoverer.worker_id != winner
            )
            with backend_guard:
                loser_pid = backend_pids[loser]
            _wait_for_postgresql_lock(loser_pid)
        finally:
            release_effect.set()
        return [future.result(timeout=20) for future in futures]


def _claim_command(worker_id: str, claimed: list[int]):
    from django_ray.management.commands.django_ray_worker import Command

    command = Command()
    command.stdout = StringIO()
    command.worker_id = worker_id
    command.execution_mode = "local"
    command.shutdown_requested = False
    command.active_tasks = {}
    command.ray_core_runner = None
    command._is_ray_core_initialized = lambda: True
    command.process_task = lambda task: claimed.append(task.pk)
    command._create_lease("default")
    return command


def _execution(task_id: str, **overrides: object) -> RayTaskExecution:
    values: dict[str, object] = {
        "task_id": task_id,
        "callable_path": "testproject.tasks.add_numbers",
        "queue_name": "default",
        "state": TaskState.QUEUED,
        "args_json": "[1, 2]",
        "kwargs_json": "{}",
        "runtime_env_hash": hashlib.sha256(b"{}").hexdigest(),
    }
    values.update(overrides)
    return RayTaskExecution.objects.create(**values)


def _rq2_reserved_execution(
    task_id: str,
    *,
    worker_id: str = "postgres-rq2-worker",
) -> tuple[RayTaskExecution, Any]:
    """Create one exact pre-submit rq2 reservation and its submission handle."""
    from django_ray.execution_codec import ExecutionIdentity
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
        coordination_sha256,
    )
    from django_ray.runner.base import SubmissionHandle

    address = "http://postgres-ray-head:8265"
    execution = _execution(
        task_id,
        state=TaskState.RUNNING,
        claimed_by_worker=worker_id,
        attempt_number=1,
        execution_generation=2,
        execution_protocol_version=1,
        ray_address=address,
        args_json="[1]",
        kwargs_json='{"flag":true}',
        runtime_env_profile=None,
        runtime_env_hash=hashlib.sha256(b"{}").hexdigest(),
    )
    assert execution.pk is not None
    identity = ExecutionIdentity(
        task_execution_pk=int(execution.pk),
        task_id=execution.task_id,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
    )
    job_id = (
        f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{coordination_sha256(identity)}"
    )
    execution.ray_job_id = job_id
    execution.save(update_fields=["ray_job_id"])
    return execution, SubmissionHandle(
        ray_job_id=job_id,
        ray_address=address,
        submitted_at=datetime.now(UTC),
    )


def _prepared_rq2_request(execution: RayTaskExecution, root: Path) -> Any:
    """Persist the canonical request bytes for one reserved execution."""
    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequest,
        encode_execution_request,
    )
    from django_ray.ray_job_request_storage import prepare_ray_job_request

    assert execution.pk is not None
    request = ExecutionRequest(
        identity=ExecutionIdentity(
            task_execution_pk=int(execution.pk),
            task_id=execution.task_id,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
        ),
        execution_protocol_version=int(execution.execution_protocol_version),
        callable_path=execution.callable_path,
        transport_version=1,
        serialized_args=execution.args_json,
        serialized_kwargs=execution.kwargs_json,
        input_reference=None,
        runtime_env_profile=execution.runtime_env_profile,
        runtime_env_hash=execution.runtime_env_hash,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )
    return prepare_ray_job_request(
        encode_execution_request(request),
        {
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(root),
        },
    )


def _rq2_request_path(prepared: Any) -> Path:
    locator = prepared.locator
    assert locator.filesystem_path is not None
    return (
        Path(locator.filesystem_path)
        / locator.digest[:2]
        / locator.digest[2:4]
        / f"{locator.digest}.json"
    )


class _RecordingRayJobClient:
    """Record the bounded Ray Job request without opening a Ray connection."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    @staticmethod
    def _upload_working_dir_if_needed(_runtime_env: dict[str, object]) -> None:
        return None

    @staticmethod
    def _upload_py_modules_if_needed(_runtime_env: dict[str, object]) -> None:
        return None

    def submit_job(self, **kwargs: object) -> str:
        self.submissions.append(kwargs)
        return str(kwargs["submission_id"])


def _ray_core_runner_with_handle(handle: RayCoreHandle) -> RayCoreRunner:
    """Build a real runner around one already-submitted ObjectRef capability."""
    runner = object.__new__(RayCoreRunner)
    runner._pending_tasks = {handle.task_pk: handle}
    return runner


def _execution_select_projections(
    queries: CaptureQueriesContext,
) -> list[tuple[set[str], str]]:
    table = RayTaskExecution._meta.db_table
    table_pattern = re.escape(table)
    selected: list[tuple[set[str], str]] = []
    for query in queries.captured_queries:
        sql = " ".join(query["sql"].split())
        if not sql.upper().startswith("SELECT") or f'FROM "{table}"' not in sql:
            continue
        select_clause = re.split(r"\s+FROM\s+", sql, maxsplit=1, flags=re.IGNORECASE)[0]
        fields = set(
            re.findall(
                rf'"{table_pattern}"\."([^"]+)"',
                select_clause,
            )
        )
        selected.append((fields, sql))
    return selected


def _allocate_workflow_identity(execution: RayTaskExecution) -> WorkflowRunIdentity:
    identity = allocate_workflow_run(
        DurableTaskContext(
            task_pk=execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
        )
    )
    assert identity is not None
    return identity


def _workflow_snapshot(
    identity: WorkflowRunIdentity,
    revision: int,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "workflow_id": f"django-ray:{identity.task_execution_pk}",
        "run_identity": identity.as_dict(),
        "revision": revision,
        "state": "RUNNING",
    }


def _prepared_cloud_alias_payload(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    *,
    serialized_request: str | None = None,
) -> SimpleNamespace:
    """Use one physical fake object for its authorized raw and encoded URIs."""
    import django_ray.input_storage as input_storage_module
    import django_ray.ray_job_request_storage as request_storage_module

    bucket = "postgres-payloads"
    prefix = "tenant alpha/résults+100%"
    scheme = "s3" if backend == "s3" else "gs"
    config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": backend,
        f"INPUT_STORAGE_{backend.upper()}_BUCKET": bucket,
        f"INPUT_STORAGE_{backend.upper()}_PREFIX": prefix,
    }
    objects: dict[str, str] = {}
    writes: list[str] = []

    class MemoryObjectStorage:
        def __init__(self, **location: Any) -> None:
            assert location["bucket"] == bucket
            assert location["prefix"] == prefix

        def store_payload(self, *, serialized_payload: str) -> str:
            payload = serialized_payload.encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            objects[digest] = serialized_payload
            writes.append(digest)
            key = f"{prefix}/{digest[:2]}/{digest[2:4]}/{digest}.json"
            return f"{scheme}://{bucket}/{quote(key, safe='/-._~')}?bytes={len(payload)}"

    storage_name = "S3ResultStorage" if backend == "s3" else "GCSResultStorage"
    monkeypatch.setattr(input_storage_module, storage_name, MemoryObjectStorage)
    monkeypatch.setattr(request_storage_module, storage_name, MemoryObjectStorage)
    if serialized_request is None:
        prepared = prepare_task_input(("physical alias input",), {}, config)
        reference = prepared.input_reference
    else:
        prepared = request_storage_module.prepare_ray_job_request(serialized_request, config)
        reference = prepared.reference
    assert reference is not None
    native = reference.removeprefix(request_storage_module.RAY_JOB_REQUEST_REFERENCE_PREFIX)
    legacy_native = native.replace(quote(prefix, safe="/-._~"), prefix, 1)
    assert native != legacy_native
    return SimpleNamespace(
        config=config,
        prepared=prepared,
        reference=reference,
        legacy_native=legacy_native,
        objects=objects,
        writes=writes,
    )
