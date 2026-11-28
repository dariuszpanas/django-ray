"""Phase 3 adversarial fault-injection matrix for Wave 1 and Wave 2 invariants.

Exercises control-plane recovery, completion persistence, async cancellation,
fail-loud result retrieval, transactional signals, Ray Core cancellation and
post-commit handle acknowledgement, stale filesystem locks, idempotent blob
deletion, synchronous concurrency clamping, and retry hygiene on SQLite without
native Ray.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from django.db import InterfaceError, OperationalError, transaction
from django.db.models.query import QuerySet
from django.tasks import TaskResult, TaskResultStatus, task
from django.tasks.signals import task_enqueued

from django_ray._blob_storage import (
    BlobDeleteOutcome,
    acquire_stale_safe_lock_dir,
    delete_blob_verified,
    install_temporary_payload_atomic,
    release_stale_safe_lock_dir,
)
from django_ray.backends import RayTaskBackend, RayTaskResult, ResultUnavailableError
from django_ray.execution_codec import (
    ExecutionCompletion,
    ExecutionIdentity,
    ExecutionRequest,
    decode_execution_completion,
    encode_execution_completion,
    encode_execution_request,
)
from django_ray.lifecycle import record_failure, request_task_cancellation
from django_ray.management.commands import django_ray_worker as worker_module
from django_ray.management.commands.django_ray_worker import Command as WorkerCommand
from django_ray.models import CancellationStatus, RayTaskExecution, TaskAttempt, TaskState
from django_ray.ray_job_protocol import (
    RAY_JOB_CONFIG_JSON_ENV_VAR,
    build_ray_job_request_reference_metadata,
)
from django_ray.ray_job_request_storage import prepare_ray_job_request
from django_ray.result_storage import (
    FilesystemResultStorage,
    GCSResultStorage,
    ResultStorageError,
    S3ResultStorage,
)
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
from django_ray.runtime import entrypoint
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.workflow.plans import runtime_env_plan_identity


@task(queue_name="default")
def _fault_matrix_sync_task(value: int) -> dict[str, int]:
    return {"doubled": value * 2}


@task(queue_name="sync")
def _fault_matrix_sync_queue_task(item: int) -> dict[str, int]:
    return {"item": item}


@task(queue_name="default")
async def _fault_matrix_cancelled_async_task() -> None:
    await asyncio.sleep(0)
    raise asyncio.CancelledError("inner async cancellation")


@task(queue_name="default")
def _fault_matrix_oversize_task(length: int) -> dict[str, str]:
    return {"payload": "x" * length}


_EMPTY_RUNTIME_ENV = normalize_runtime_env({})


def _make_worker(*, worker_id: str, mode: str = "sync") -> WorkerCommand:
    cmd = WorkerCommand(stdout=StringIO(), stderr=StringIO())
    cmd.worker_id = worker_id
    cmd.execution_mode = mode
    cmd.sync_mode = mode == "sync"
    cmd.active_tasks = {}
    cmd.active_task_identities = {}
    cmd.poll_base_interval = 0.01
    cmd.poll_max_interval = 0.05
    cmd.polling_policy = cmd._new_polling_policy()
    return cmd


def _make_backend(*, queues: list[str] | None = None) -> RayTaskBackend:
    return RayTaskBackend(
        "default",
        {
            "QUEUES": queues if queues is not None else ["default"],
            "OPTIONS": {"RAY_ADDRESS": "auto"},
        },
    )


class _FakeProviderError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"provider error {status} {code}")
        self.code = status
        self.status_code = status
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


@pytest.mark.django_db
class TestWorkerTransientDatabaseRecovery:
    """F-001: worker loop and heartbeat recover transient DB drops within lease TTL."""

    def test_run_loop_recovers_transient_db_errors_and_completes_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "WORKER_LEASE_SECONDS": 60}
        task_row = RayTaskExecution.objects.create(
            task_id="fault-f001-recover-1",
            callable_path=_fault_matrix_sync_task.module_path,
            queue_name="default",
            args_json="[21]",
            kwargs_json="{}",
            runtime_env_json="{}",
            runtime_env_hash=_EMPTY_RUNTIME_ENV.digest,
            state=TaskState.QUEUED,
        )

        cmd = _make_worker(worker_id="fault-f001-worker", mode="sync")
        cmd._create_lease("default")

        close_calls = 0
        original_close = worker_module.close_old_connections

        def counting_close() -> None:
            nonlocal close_calls
            close_calls += 1
            original_close()

        monkeypatch.setattr(worker_module, "close_old_connections", counting_close)
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda _timeout: None)

        heartbeat_attempts = 0
        original_lock_leases = cmd._lock_authoritative_leases

        def flaky_lock_leases(*args: Any, **kwargs: Any) -> Any:
            nonlocal heartbeat_attempts
            if kwargs.get("renew_heartbeat", True):
                heartbeat_attempts += 1
                if heartbeat_attempts == 1:
                    raise OperationalError("temporary connection drop during heartbeat")
            return original_lock_leases(*args, **kwargs)

        monkeypatch.setattr(cmd, "_lock_authoritative_leases", flaky_lock_leases)

        claim_attempts = 0
        original_claim = cmd.claim_and_process_tasks

        def flaky_claim(queues: Any, concurrency: int) -> int:
            nonlocal claim_attempts
            claim_attempts += 1
            if claim_attempts == 1:
                raise OperationalError("temporary connection drop during claim")
            processed = original_claim(queues, concurrency)
            cmd.shutdown_requested = True
            return processed

        monkeypatch.setattr(cmd, "claim_and_process_tasks", flaky_claim)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=15.0)

        task_row.refresh_from_db()
        assert close_calls >= 3
        assert heartbeat_attempts >= 1
        assert claim_attempts == 2
        assert cmd.lease_ownership_lost is False
        assert cmd.shutdown_exit_code is None
        assert task_row.state == TaskState.SUCCEEDED
        assert json.loads(str(task_row.result_data)) == {"doubled": 42}

    def test_transient_db_error_past_lease_ttl_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "WORKER_LEASE_SECONDS": 60}
        cmd = _make_worker(worker_id="fault-f001-expired-worker", mode="sync")
        cmd._create_lease("default")
        cmd._last_successful_heartbeat_monotonic -= 120.0

        monkeypatch.setattr(
            cmd,
            "_lock_authoritative_leases",
            lambda *_a, **_kw: (_ for _ in ()).throw(InterfaceError("connection closed")),
        )
        cmd.send_heartbeat()

        assert cmd.lease_ownership_lost is True
        assert cmd.shutdown_requested is True
        assert cmd.shutdown_exit_code == 1


@pytest.mark.django_db
class TestEntrypointCompletionPersistenceAndAsyncCancellation:
    """F-096 & F-099: completion persistence retries/exit-75 and async CancelledError."""

    def test_persist_task_completion_retries_transient_db_errors_and_recovers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="fault-f096-retry-1",
            callable_path=_fault_matrix_sync_task.module_path,
            queue_name="default",
            args_json="[5]",
            kwargs_json="{}",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )

        attempts = 0
        closed = 0
        original_update = QuerySet.update

        def flaky_update(self: Any, **kwargs: Any) -> int:
            nonlocal attempts
            if self.model is RayTaskExecution and "completion_data" in kwargs:
                attempts += 1
                if attempts <= 2:
                    raise OperationalError("transient database outage")
            return original_update(self, **kwargs)

        def counting_close() -> None:
            nonlocal closed
            closed += 1

        monkeypatch.setattr(QuerySet, "update", flaky_update)
        monkeypatch.setattr(entrypoint, "close_old_connections", counting_close)
        monkeypatch.setattr(entrypoint, "_COMPLETION_PERSIST_BACKOFF_SECONDS", (0.0, 0.0, 0.0, 0.0))

        payload = '{"success":true,"result":{"doubled":10}}'
        persisted = entrypoint._persist_task_completion(
            execution.pk,
            1,
            1,
            payload,
        )

        execution.refresh_from_db()
        assert persisted is True
        assert attempts == 3
        assert closed >= 5
        assert execution.completion_data == payload

    def test_main_returns_exit_code_75_when_completion_persistence_exhausts_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        storage_dir = tmp_path / "req-storage"
        storage_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(storage_dir),
        }

        execution = RayTaskExecution.objects.create(
            task_id="fault-f096-exit75-1",
            callable_path=_fault_matrix_sync_task.module_path,
            queue_name="default",
            args_json="[7]",
            kwargs_json="{}",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )

        runtime_env = normalize_runtime_env({})
        plan_identity = runtime_env_plan_identity(runtime_env, trust_identity={})
        identity = ExecutionIdentity(
            task_execution_pk=int(execution.pk),
            task_id=str(execution.task_id),
            attempt_number=1,
            execution_generation=1,
        )
        request = ExecutionRequest(
            identity=identity,
            execution_protocol_version=1,
            callable_path=_fault_matrix_sync_task.module_path,
            transport_version=1,
            serialized_args="[7]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=runtime_env.profile,
            runtime_env_hash=runtime_env.digest,
            runtime_env_plan_identity=plan_identity.as_transport_dict(),
            compiled_graph_submission_transport="ray-job",
        )
        payload_json = encode_execution_request(request)
        prepared = prepare_ray_job_request(payload_json, settings.DJANGO_RAY)
        metadata = build_ray_job_request_reference_metadata(
            request,
            payload_json,
            prepared.reference,
            prepared.encoded_locator,
        )
        monkeypatch.setenv(RAY_JOB_CONFIG_JSON_ENV_VAR, json.dumps({"metadata": metadata}))

        persist_attempts = 0
        original_update = QuerySet.update

        def always_fail_completion_update(self: Any, **kwargs: Any) -> int:
            nonlocal persist_attempts
            if self.model is RayTaskExecution and "completion_data" in kwargs:
                persist_attempts += 1
                raise OperationalError("database unreachable")
            return original_update(self, **kwargs)

        monkeypatch.setattr(QuerySet, "update", always_fail_completion_update)
        monkeypatch.setattr(entrypoint, "_COMPLETION_PERSIST_MAX_ATTEMPTS", 3)
        monkeypatch.setattr(entrypoint, "_COMPLETION_PERSIST_BACKOFF_SECONDS", (0.0, 0.0))

        exit_code = entrypoint.main(["--request-ref-b64", prepared.encoded_locator])

        execution.refresh_from_db()
        assert exit_code == entrypoint.RAY_JOB_COMPLETION_PERSISTENCE_FAILED_EXIT_CODE == 75
        assert persist_attempts == 3
        assert execution.completion_data is None

    def test_asyncio_cancelled_error_is_captured_as_non_retryable_completion(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        execution = RayTaskExecution.objects.create(
            task_id="fault-f099-async-cancel-1",
            callable_path=_fault_matrix_cancelled_async_task.module_path,
            queue_name="default",
            args_json="[]",
            kwargs_json="{}",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )
        identity = ExecutionIdentity(
            task_execution_pk=int(execution.pk),
            task_id=str(execution.task_id),
            attempt_number=1,
            execution_generation=1,
        )

        close_calls = 0
        original_close = entrypoint.close_old_connections

        def counting_close() -> None:
            nonlocal close_calls
            close_calls += 1
            original_close()

        monkeypatch.setattr(entrypoint, "close_old_connections", counting_close)

        raw_envelope = entrypoint.execute_task(
            callable_path=_fault_matrix_cancelled_async_task.module_path,
            serialized_args="[]",
            serialized_kwargs="{}",
            task_execution_pk=int(execution.pk),
            task_id=str(execution.task_id),
            attempt_number=1,
            execution_generation=1,
            ray_job_driver=True,
            _completion_identity=identity,
            _execution_protocol_version=1,
            _strict_execution_request=True,
        )

        decoded = decode_execution_completion(
            raw_envelope,
            expected_identity=identity,
            expected_execution_protocol_version=1,
        )
        completion = decoded.completion
        execution.refresh_from_db()
        assert completion.success is False
        assert completion.retryable is False
        assert completion.exception_type == (
            f"{asyncio.CancelledError.__module__}.{asyncio.CancelledError.__qualname__}"
        )
        assert "inner async cancellation" in (completion.error or "")
        assert execution.completion_data == str(raw_envelope)
        assert close_calls >= 3


@pytest.mark.django_db
class TestFailLoudOversizeResults:
    """F-004: get_result() stays safe while return_value fails loud and write refuses downgrade."""

    def test_get_result_succeeds_and_return_value_raises_on_oversize_digest_stub(self) -> None:
        digest = "b" * 64
        execution = RayTaskExecution.objects.create(
            task_id="fault-f004-digest-1",
            callable_path=_fault_matrix_oversize_task.module_path,
            queue_name="default",
            args_json="[2048]",
            kwargs_json="{}",
            state=TaskState.SUCCEEDED,
            result_data=None,
            result_reference=f"oversize://sha256/{digest}?bytes=2048",
        )

        result = _make_backend().get_result(str(execution.task_id))

        assert isinstance(result, RayTaskResult)
        assert isinstance(result, TaskResult)
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.is_finished is True
        with pytest.raises(ResultUnavailableError) as exc_info:
            _ = result.return_value
        assert exc_info.value.reason == "digest_stub"
        assert exc_info.value.digest == digest
        assert exc_info.value.size_bytes == 2048

    def test_external_result_store_error_fails_task_instead_of_digest_downgrade(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        settings: Any,
    ) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        result_dir = tmp_path / "results"
        result_dir.mkdir()
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "MAX_RESULT_SIZE_BYTES": 32,
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(result_dir),
        }

        def fail_erofs(*_args: Any, **_kwargs: Any) -> str:
            raise ResultStorageError("read-only filesystem") from OSError(
                errno.EROFS, "Read-only file system"
            )

        monkeypatch.setattr(FilesystemResultStorage, "store", fail_erofs)
        monkeypatch.setattr(entrypoint, "_RESULT_STORAGE_STORE_BACKOFF_SECONDS", (0.0, 0.0))

        identity = ExecutionIdentity(
            task_execution_pk=901,
            task_id="fault-f004-erofs-1",
            attempt_number=1,
            execution_generation=1,
        )
        raw = entrypoint.execute_task(
            callable_path=_fault_matrix_oversize_task.module_path,
            serialized_args="[256]",
            serialized_kwargs="{}",
            ray_job_driver=False,
            _completion_identity=identity,
            _execution_protocol_version=1,
            allow_result_offload=True,
        )

        decoded = decode_execution_completion(
            raw,
            expected_identity=identity,
            expected_execution_protocol_version=1,
        )
        completion = decoded.completion
        assert completion.success is False
        assert completion.result_reference is None
        assert completion.retryable is False
        assert completion.exception_type is not None
        assert "ResultStoragePersistenceError" in completion.exception_type


@pytest.mark.django_db(transaction=True)
@pytest.mark.postgresql
@pytest.mark.sqlite_compatible
class TestTransactionalSignalsAndIsolation:
    """F-031 & F-100: task_enqueued suppresses on rollback, fires on commit, isolates errors."""

    def test_task_enqueued_suppressed_on_rollback_emitted_on_commit_and_isolated(
        self,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto"}
        backend = _make_backend()
        received_ids: list[str] = []
        failing_calls = 0

        def failing_receiver(sender: Any, task_result: TaskResult, **_kwargs: Any) -> None:
            nonlocal failing_calls
            del sender, task_result
            failing_calls += 1
            raise RuntimeError("receiver telemetry outage")

        def healthy_receiver(sender: Any, task_result: TaskResult, **_kwargs: Any) -> None:
            del sender
            received_ids.append(task_result.id)

        task_enqueued.connect(failing_receiver)
        task_enqueued.connect(healthy_receiver)
        try:
            # 1. Rolled-back atomic block must suppress all signal delivery.
            with pytest.raises(RuntimeError, match="abort transaction"):
                with transaction.atomic():
                    backend.enqueue(_fault_matrix_sync_task, (10,), {})
                    raise RuntimeError("abort transaction")

            assert failing_calls == 0
            assert received_ids == []
            assert RayTaskExecution.objects.count() == 0

            # 2. Committed atomic block emits after commit and isolates receiver failures.
            with transaction.atomic():
                enqueued = backend.enqueue(_fault_matrix_sync_task, (11,), {})
                assert received_ids == []
                assert failing_calls == 0

            assert failing_calls == 1
            assert received_ids == [enqueued.id]
            assert RayTaskExecution.objects.filter(task_id=enqueued.id).exists()
        finally:
            task_enqueued.disconnect(failing_receiver)
            task_enqueued.disconnect(healthy_receiver)


@pytest.mark.django_db
class TestRayCoreCancelSaturationAndPostCommitAck:
    """F-013 & F-022: non-bricking cancel pool and post-commit Ray Core handle ack."""

    def test_cancel_pool_survives_hung_cancel_and_preserves_handle_on_saturation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        unblock_1 = threading.Event()
        unblock_2 = threading.Event()
        ref_1 = object()
        ref_2 = object()
        ref_3 = object()
        cancelled_refs: list[object] = []

        class _FakeRayTaskError(Exception):
            pass

        def fake_wait(_refs: Any, **_kwargs: Any) -> tuple[list[Any], list[Any]]:
            return [], list(_refs)

        def fake_cancel(ref: object, *, force: bool = False) -> None:
            del force
            cancelled_refs.append(ref)
            if ref is ref_1:
                unblock_1.wait(timeout=5.0)
            elif ref is ref_2:
                unblock_2.wait(timeout=5.0)

        fake_ray = ModuleType("ray")
        fake_ray.is_initialized = lambda: True  # type: ignore[attr-defined]
        fake_ray.wait = fake_wait  # type: ignore[attr-defined]
        fake_ray.cancel = fake_cancel  # type: ignore[attr-defined]
        fake_exceptions = ModuleType("ray.exceptions")
        fake_exceptions.RayTaskError = _FakeRayTaskError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setitem(sys.modules, "ray.exceptions", fake_exceptions)

        runner = RayCoreRunner(address="auto", max_concurrent_cancels=2)
        h1 = RayCoreHandle(
            task_pk=1,
            task_name="test.task",
            object_ref=ref_1,
            submitted_at=datetime.now(UTC),
            attempt_number=1,
            execution_generation=1,
        )
        h2 = RayCoreHandle(
            task_pk=2,
            task_name="test.task",
            object_ref=ref_2,
            submitted_at=datetime.now(UTC),
            attempt_number=1,
            execution_generation=1,
        )
        h3 = RayCoreHandle(
            task_pk=3,
            task_name="test.task",
            object_ref=ref_3,
            submitted_at=datetime.now(UTC),
            attempt_number=1,
            execution_generation=1,
        )
        runner._pending_tasks = {1: h1, 2: h2, 3: h3}

        try:
            # Slot 1 hangs past timeout -> INDETERMINATE, but slot 2 remains operational.
            outcome_1 = runner.cancel_pending_with_status(h1, timeout_seconds=0.05)
            assert outcome_1.status == CancellationOutcomeStatus.INDETERMINATE
            assert 1 not in runner._pending_tasks

            # Slot 2 succeeds immediately when unblocked -> NOT bricked by slot 1.
            unblock_2.set()
            outcome_2 = runner.cancel_pending_with_status(h2, timeout_seconds=0.5)
            assert outcome_2.status == CancellationOutcomeStatus.REQUESTED
            assert ref_2 in cancelled_refs

            # Saturate both slots (slot 1 still hung; slot 2 hung on replacement handle h2b).
            unblock_2.clear()
            h2b = RayCoreHandle(
                task_pk=20,
                task_name="test.task",
                object_ref=ref_2,
                submitted_at=datetime.now(UTC),
                attempt_number=1,
                execution_generation=1,
            )
            runner._pending_tasks[20] = h2b
            outcome_2b = runner.cancel_pending_with_status(h2b, timeout_seconds=0.05)
            assert outcome_2b.status == CancellationOutcomeStatus.INDETERMINATE

            # With both slots saturated, h3 must return INDETERMINATE WITHOUT being popped.
            outcome_3 = runner.cancel_pending_with_status(h3, timeout_seconds=0.05)
            assert outcome_3.status == CancellationOutcomeStatus.INDETERMINATE
            assert "occupied" in (outcome_3.message or "")
            assert runner._pending_tasks.get(3) is h3
        finally:
            unblock_1.set()
            unblock_2.set()
            time.sleep(0.05)

        # Once hung threads exit, slots recover and h3 cancels cleanly.
        outcome_3_retry = runner.cancel_pending_with_status(h3, timeout_seconds=0.5)
        assert outcome_3_retry.status == CancellationOutcomeStatus.REQUESTED
        assert 3 not in runner._pending_tasks

    def test_poll_ray_core_tasks_retains_handle_on_db_error_and_commits_on_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "WORKER_LEASE_SECONDS": 60}
        cmd = _make_worker(worker_id="fault-f022-worker", mode="local")
        cmd._create_lease("default")

        task_row = RayTaskExecution.objects.create(
            task_id="fault-f022-task-1",
            callable_path=_fault_matrix_sync_task.module_path,
            queue_name="default",
            args_json="[9]",
            kwargs_json="{}",
            runtime_env_json="{}",
            runtime_env_hash=_EMPTY_RUNTIME_ENV.digest,
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
        )

        identity = ExecutionIdentity(
            task_execution_pk=int(task_row.pk),
            task_id=str(task_row.task_id),
            attempt_number=1,
            execution_generation=1,
        )
        completion_json = encode_execution_completion(
            ExecutionCompletion(
                identity=identity,
                execution_protocol_version=1,
                executor_django_ray_version="0.8.0",
                success=True,
                result={"doubled": 18},
                result_reference=None,
                error=None,
                traceback=None,
                exception_type=None,
                retryable=None,
            )
        )

        ready_ref = object()
        get_calls = 0
        fake_ray = ModuleType("ray")
        fake_ray.is_initialized = lambda: True  # type: ignore[attr-defined]
        fake_ray.wait = lambda refs, **_kw: (list(refs), [])  # type: ignore[attr-defined]

        def fake_get(_ref: object) -> str:
            nonlocal get_calls
            get_calls += 1
            return completion_json

        fake_ray.get = fake_get  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        runner = RayCoreRunner(address="auto")
        handle = RayCoreHandle(
            task_pk=int(task_row.pk),
            task_name=_fault_matrix_sync_task.module_path,
            object_ref=ready_ref,
            submitted_at=datetime.now(UTC),
            attempt_number=1,
            execution_generation=1,
        )
        runner._pending_tasks[int(task_row.pk)] = handle
        cmd.ray_core_runner = runner

        # First poll hits transient OperationalError during completion persistence.
        persist_attempts = 0
        original_store = cmd._store_and_succeed_task

        def flaky_store(*args: Any, **kwargs: Any) -> bool:
            nonlocal persist_attempts
            persist_attempts += 1
            if persist_attempts == 1:
                raise OperationalError("database lock timeout during completion")
            return original_store(*args, **kwargs)

        monkeypatch.setattr(cmd, "_store_and_succeed_task", flaky_store)

        first_processed = cmd.poll_ray_core_tasks()
        task_row.refresh_from_db()
        assert first_processed == 0
        assert runner.pending_count == 1
        assert task_row.state == TaskState.RUNNING

        # Second poll replays cached completion without re-calling ray.get and retires handle.
        second_processed = cmd.poll_ray_core_tasks()
        task_row.refresh_from_db()
        assert second_processed == 1
        assert runner.pending_count == 0
        assert get_calls == 1
        assert task_row.state == TaskState.SUCCEEDED
        assert json.loads(str(task_row.result_data)) == {"doubled": 18}


class TestStaleLockRecoveryAndIdempotentBlobDelete:
    """F-015 & F-017: stale .install-lock / dead-PID recovery and 404-idempotent S3/GCS delete."""

    def test_stale_legacy_install_lock_dir_and_dead_pid_lock_dir_recover(
        self,
        tmp_path: Path,
    ) -> None:
        # 1. Legacy .install-lock dir on hardlink-unsupported filesystem is reaped when stale.
        storage_dir = tmp_path / "payloads"
        storage_dir.mkdir()
        temp_file = storage_dir / ".temp.json"
        temp_file.write_text('{"ok":true}', encoding="utf-8")
        target_file = storage_dir / "abc.json"
        legacy_lock = storage_dir / f".{target_file.name}.install-lock"
        legacy_lock.mkdir()
        stale_ts = time.time() - 120.0
        os.utime(legacy_lock, (stale_ts, stale_ts))

        fake_os = SimpleNamespace(
            link=lambda _src, _dst: (_ for _ in ()).throw(
                OSError(errno.EOPNOTSUPP, "hard links unsupported")
            ),
            replace=os.replace,
        )
        installed = install_temporary_payload_atomic(temp_file, target_file, os_module=fake_os)
        assert installed is True
        assert target_file.exists()
        assert not legacy_lock.exists()

        # 2. Cross-process lock directory owned by a simulated dead PID is reclaimed immediately.
        lock_dir = tmp_path / "spill.lock"
        lock_dir.mkdir()
        (lock_dir / "owner.json").write_text(
            json.dumps(
                {
                    "creator_pid": 999_999,
                    "process_start_token": "dead-proc-token",
                    "created_at_ns": time.time_ns(),
                }
            ),
            encoding="utf-8",
        )
        acquired = acquire_stale_safe_lock_dir(
            lock_dir,
            timeout_seconds=1.0,
            stale_grace_seconds=60.0,
            pid_alive_fn=lambda _pid: False,
            process_start_token_fn=lambda _pid: "live-token",
        )
        try:
            assert acquired == lock_dir
            owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
            assert owner["creator_pid"] == os.getpid()
        finally:
            release_stale_safe_lock_dir(lock_dir)
        assert not lock_dir.exists()

    def test_s3_and_gcs_blob_delete_are_404_idempotent_without_body_download(self) -> None:
        digest = "c" * 64
        s3_ref = (
            f"s3://test-bucket/django-ray/results/{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=12"
        )
        gcs_ref = (
            f"gs://test-bucket/django-ray/results/{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=12"
        )

        class _MissingS3Client:
            def __init__(self) -> None:
                self.head_calls = 0
                self.get_calls = 0
                self.delete_calls = 0

            def head_object(self, **_kwargs: Any) -> dict[str, Any]:
                self.head_calls += 1
                raise _FakeProviderError(404, "NoSuchKey")

            def get_object(self, **_kwargs: Any) -> dict[str, Any]:
                self.get_calls += 1
                raise AssertionError("delete must never download full object body")

            def delete_object(self, **_kwargs: Any) -> dict[str, Any]:
                self.delete_calls += 1
                return {}

        s3_client = _MissingS3Client()
        s3_backend = S3ResultStorage(
            bucket="test-bucket",
            prefix="django-ray/results",
            client=s3_client,
        )
        s3_outcome = delete_blob_verified(s3_backend, s3_ref, ignore_missing=True)
        assert s3_outcome == BlobDeleteOutcome(
            deleted=False,
            already_missing=True,
            reference=s3_ref,
            backend="s3",
        )
        assert s3_client.head_calls == 1
        assert s3_client.get_calls == 0

        class _CompatS3Client:
            def __init__(self) -> None:
                self.delete_kwargs: list[dict[str, Any]] = []

            def head_object(self, **_kwargs: Any) -> dict[str, Any]:
                return {"ContentLength": 12, "ETag": '"etag-123"'}

            def get_object(self, **_kwargs: Any) -> dict[str, Any]:
                raise AssertionError("delete must never download full object body")

            def delete_object(self, **kwargs: Any) -> dict[str, Any]:
                self.delete_kwargs.append(dict(kwargs))
                if "IfMatch" in kwargs:
                    raise _FakeProviderError(501, "NotImplemented")
                return {}

        compat_client = _CompatS3Client()
        compat_backend = S3ResultStorage(
            bucket="test-bucket",
            prefix="django-ray/results",
            endpoint_url="http://minio.local:9000",
            client=compat_client,
        )
        compat_outcome = delete_blob_verified(compat_backend, s3_ref, ignore_missing=True)
        assert compat_outcome.deleted is True
        assert compat_outcome.already_missing is False
        assert len(compat_client.delete_kwargs) == 2
        assert "IfMatch" in compat_client.delete_kwargs[0]
        assert "IfMatch" not in compat_client.delete_kwargs[1]

        class _MissingGCSBlob:
            size = 12
            generation = 1

            def reload(self) -> None:
                raise _FakeProviderError(404, "NotFound")

            def download_as_bytes(self) -> bytes:
                raise AssertionError("delete must never download full object body")

            def delete(self, **_kwargs: Any) -> None:
                raise AssertionError("missing GCS blob should short-circuit on reload 404")

        class _MissingGCSClient:
            def bucket(self, _name: str) -> Any:
                return SimpleNamespace(blob=lambda _key: _MissingGCSBlob())

        gcs_backend = GCSResultStorage(
            bucket="test-bucket",
            prefix="django-ray/results",
            client=_MissingGCSClient(),
        )
        gcs_outcome = delete_blob_verified(gcs_backend, gcs_ref, ignore_missing=True)
        assert gcs_outcome == BlobDeleteOutcome(
            deleted=False,
            already_missing=True,
            reference=gcs_ref,
            backend="gcs",
        )


@pytest.mark.django_db
class TestSyncConcurrencyClampAndRetryHygiene:
    """F-020 & F-029: --sync claims at most 1 task and retry wipes stale attempt fields."""

    def test_sync_mode_clamps_claim_batch_to_one_even_with_high_concurrency(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Any,
    ) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "WORKER_LEASE_SECONDS": 60}
        rows = [
            RayTaskExecution.objects.create(
                task_id=f"fault-f020-sync-{idx}",
                callable_path=_fault_matrix_sync_queue_task.module_path,
                queue_name="sync",
                args_json=f"[{idx}]",
                kwargs_json="{}",
                runtime_env_json="{}",
                runtime_env_hash=_EMPTY_RUNTIME_ENV.digest,
                state=TaskState.QUEUED,
            )
            for idx in range(5)
        ]

        cmd = _make_worker(worker_id="fault-f020-sync-worker", mode="sync")
        cmd._create_lease("sync")

        claimed = cmd.claim_and_process_tasks(["sync"], concurrency=20)
        assert claimed == 1

        refreshed = [RayTaskExecution.objects.get(pk=row.pk) for row in rows]
        succeeded = [row for row in refreshed if row.state == TaskState.SUCCEEDED]
        still_queued = [row for row in refreshed if row.state == TaskState.QUEUED]
        assert len(succeeded) == 1
        assert len(still_queued) == 4
        assert all(row.started_at is None for row in still_queued)
        assert all(row.claimed_by_worker is None for row in still_queued)

    def test_record_failure_retry_wipes_stale_fields_bumps_generation_and_preserves_clean_cancel(
        self,
    ) -> None:
        now = datetime.now(UTC)
        execution = RayTaskExecution.objects.create(
            task_id="fault-f029-retry-1",
            callable_path=_fault_matrix_sync_task.module_path,
            queue_name="default",
            args_json="[1]",
            kwargs_json="{}",
            runtime_env_json="{}",
            runtime_env_hash=_EMPTY_RUNTIME_ENV.digest,
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=2,
            started_at=now - timedelta(seconds=5),
            last_heartbeat_at=now,
            claimed_by_worker="worker-1",
            ray_job_id="raysubmit_old_attempt_1",
            ray_job_request_reference="rq2ref:v1:s3:old",
            ray_address="http://ray-head:8265",
            error_message="stale prior error",
            error_traceback="Traceback (prior)",
            completion_data='{"stale":true}',
            cancellation_status=CancellationStatus.REQUESTED,
            cancellation_error="stale cancel err",
        )

        next_at = now + timedelta(seconds=30)
        persisted = record_failure(
            execution,
            error_message="attempt 1 transient failure",
            error_traceback="Traceback: ValueError('attempt 1')",
            retry=True,
            next_attempt_at=next_at,
            expected_claimed_by_worker="worker-1",
            expected_attempt_number=1,
            expected_execution_generation=2,
        )
        assert persisted is True

        execution.refresh_from_db()
        assert execution.state == TaskState.QUEUED
        assert execution.attempt_number == 2
        assert execution.execution_generation == 3
        assert execution.ray_job_id is None
        assert execution.ray_job_request_reference is None
        assert execution.ray_address is None
        assert execution.error_message is None
        assert execution.error_traceback is None
        assert execution.completion_data is None
        assert execution.cancellation_status is None
        assert execution.cancellation_error is None
        assert execution.started_at is None
        assert execution.finished_at is None
        assert execution.claimed_by_worker is None

        attempt_1 = TaskAttempt.objects.get(execution=execution, attempt_number=1)
        assert attempt_1.state == TaskState.FAILED
        assert attempt_1.error_message == "attempt 1 transient failure"
        assert attempt_1.error_traceback == "Traceback: ValueError('attempt 1')"

        cancel_result = request_task_cancellation(
            int(execution.pk),
            expected_attempt_number=2,
            expected_execution_generation=3,
        )
        assert cancel_result.accepted is True

        attempt_2 = TaskAttempt.objects.get(execution=execution, attempt_number=2)
        assert attempt_2.state == TaskState.CANCELLED
        assert attempt_2.error_message is None
        assert attempt_2.error_traceback is None
