"""PostgreSQL-only tests for contested worker coordination paths."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Barrier, Event

import pytest
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import django_ray.workflow_progress as workflow_progress_module
from django_ray.input_storage import load_task_input, prepare_task_input, register_task_input
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    record_failure,
    request_task_cancellation,
    retry_task,
    succeed_task,
)
from django_ray.management.commands.django_ray_purge_inputs import Command as PurgeInputsCommand
from django_ray.models import (
    InputPayloadState,
    RayTaskExecution,
    TaskAttempt,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressRunStorage,
)
from django_ray.runner.cancellation import finalize_cancellation
from django_ray.runner.leasing import get_active_workers
from django_ray.runner.reconciliation import mark_task_lost, mark_task_timed_out
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_progress import (
    WorkflowProgressDiagnosticCode,
    claim_workflow_run,
    persist_workflow_progress,
    persist_workflow_progress_summary,
    read_workflow_progress,
)
from django_ray.workflow_progress_summary import (
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)
from tests.workflow_progress_summary_helpers import workflow_progress_summary

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    """Keep the default SQLite suite fast while making this gate explicit."""
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


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


def _claim_command(worker_id: str, claimed: list[int]):
    from django_ray.management.commands.django_ray_worker import Command

    command = Command()
    command.stdout = StringIO()
    command.worker_id = worker_id
    command.execution_mode = "local"
    command.shutdown_requested = False
    command.active_tasks = {}
    command.ray_core_runner = None
    command.process_task = lambda task: claimed.append(task.pk)
    return command


def _execution(task_id: str, **overrides: object) -> RayTaskExecution:
    values: dict[str, object] = {
        "task_id": task_id,
        "callable_path": "testproject.tasks.add_numbers",
        "queue_name": "default",
        "state": TaskState.QUEUED,
        "args_json": "[1, 2]",
        "kwargs_json": "{}",
    }
    values.update(overrides)
    return RayTaskExecution.objects.create(**values)


def test_expiry_wins_concurrent_claim_at_exact_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = datetime.now(UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline if tz is not None else deadline.replace(tzinfo=None)

    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.datetime",
        FrozenDateTime,
    )
    task = _execution(
        "postgres-expiry-claim-race-001",
        queue_timeout_seconds=60,
        queue_deadline_at=deadline,
    )
    first_claimed: list[int] = []
    second_claimed: list[int] = []
    first = _claim_command("expiry-worker-a", first_claimed)
    second = _claim_command("expiry-worker-b", second_claimed)

    _run_concurrently(
        lambda: first.claim_and_process_tasks(["default"], concurrency=1),
        lambda: second.claim_and_process_tasks(["default"], concurrency=1),
    )

    task.refresh_from_db()
    assert task.state == TaskState.EXPIRED
    assert first_claimed == []
    assert second_claimed == []
    assert TaskAttempt.objects.filter(execution=task, state=TaskState.EXPIRED).count() == 1


def _workflow_identity(
    execution: RayTaskExecution,
    run_id: str,
) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=run_id,
    )


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


def test_two_workers_claim_each_execution_exactly_once() -> None:
    tasks = [_execution(f"postgres-claim-{index:02d}") for index in range(12)]
    claimed_a: list[int] = []
    claimed_b: list[int] = []
    worker_a = _claim_command("postgres-worker-a", claimed_a)
    worker_b = _claim_command("postgres-worker-b", claimed_b)

    _run_concurrently(
        lambda: worker_a.claim_and_process_tasks(["default"], concurrency=6),
        lambda: worker_b.claim_and_process_tasks(["default"], concurrency=6),
    )

    expected_ids = {task.pk for task in tasks}
    assert set(claimed_a).isdisjoint(claimed_b)
    assert set(claimed_a) | set(claimed_b) == expected_ids
    assert len(claimed_a) == len(claimed_b) == 6

    owners = dict(RayTaskExecution.objects.values_list("pk", "claimed_by_worker"))
    assert {owners[task_id] for task_id in claimed_a} == {"postgres-worker-a"}
    assert {owners[task_id] for task_id in claimed_b} == {"postgres-worker-b"}


def test_two_workers_claim_global_priority_frontier_with_fifo_ties() -> None:
    created_at = datetime.now(UTC) - timedelta(minutes=1)
    highest = _execution(
        "postgres-priority-highest",
        priority=100,
        created_at=created_at,
    )
    equal_priority = [
        _execution(
            f"postgres-priority-tie-{index}",
            priority=90,
            created_at=created_at + timedelta(seconds=index + 1),
        )
        for index in range(6)
    ]
    lower_priority = [
        _execution(
            f"postgres-priority-lower-{index}",
            priority=priority,
            created_at=created_at + timedelta(seconds=10 + index),
        )
        for index, priority in enumerate((80, 50, 0, -100))
    ]
    all_tasks = [highest, *equal_priority, *lower_priority]
    expected_frontier = {highest.pk, *(task.pk for task in equal_priority[:5])}
    claimed_a: list[int] = []
    claimed_b: list[int] = []
    worker_a = _claim_command("priority-worker-a", claimed_a)
    worker_b = _claim_command("priority-worker-b", claimed_b)

    _run_concurrently(
        lambda: worker_a.claim_and_process_tasks(["default"], concurrency=3),
        lambda: worker_b.claim_and_process_tasks(["default"], concurrency=3),
    )

    assert len(claimed_a) == len(claimed_b) == 3
    assert set(claimed_a).isdisjoint(claimed_b)
    assert set(claimed_a) | set(claimed_b) == expected_frontier

    ordering = {task.pk: (-int(task.priority), task.created_at, task.pk) for task in all_tasks}
    assert claimed_a == sorted(claimed_a, key=ordering.__getitem__)
    assert claimed_b == sorted(claimed_b, key=ordering.__getitem__)

    owners = dict(RayTaskExecution.objects.values_list("pk", "claimed_by_worker"))
    assert {owners[task_id] for task_id in claimed_a} == {"priority-worker-a"}
    assert {owners[task_id] for task_id in claimed_b} == {"priority-worker-b"}
    assert owners[equal_priority[-1].pk] is None
    assert {owners[task.pk] for task in lower_priority} == {None}


def test_skip_locked_claims_available_row_then_locked_row_without_starvation() -> None:
    locked_task = _execution("postgres-skip-locked-001")
    available_task = _execution("postgres-skip-locked-002")
    lock_acquired = Event()
    release_lock = Event()

    def hold_first_row_lock() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                RayTaskExecution.objects.select_for_update().get(pk=locked_task.pk)
                lock_acquired.set()
                if not release_lock.wait(timeout=10):
                    raise TimeoutError("test did not release the PostgreSQL row lock")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        lock_future = executor.submit(hold_first_row_lock)
        assert lock_acquired.wait(timeout=10)
        try:
            first_claims: list[int] = []
            first_worker = _claim_command("skip-locked-worker", first_claims)
            first_worker.claim_and_process_tasks(["default"], concurrency=2)
            assert first_claims == [available_task.pk]
        finally:
            release_lock.set()
        lock_future.result(timeout=10)

    second_claims: list[int] = []
    second_worker = _claim_command("post-lock-worker", second_claims)
    second_worker.claim_and_process_tasks(["default"], concurrency=2)

    assert second_claims == [locked_task.pk]
    assert RayTaskExecution.objects.filter(state=TaskState.QUEUED).count() == 0


def test_external_result_storage_and_cancellation_share_the_execution_lock(
    settings, tmp_path, monkeypatch
) -> None:
    from django_ray.management.commands.django_ray_worker import Command
    from django_ray.result_storage import FilesystemResultStorage, load_result_reference

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "MAX_RESULT_SIZE_BYTES": 1,
        "RESULT_STORAGE_BACKEND": "filesystem",
        "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    task = _execution(
        "postgres-result-storage-cancellation-race-001",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    store_started = Event()
    release_store = Event()
    original_store = FilesystemResultStorage.store

    def blocking_store(
        storage: FilesystemResultStorage,
        *,
        serialized_result: str,
    ) -> str:
        store_started.set()
        if not release_store.wait(timeout=10):
            raise TimeoutError("test did not release external result storage")
        return original_store(storage, serialized_result=serialized_result)

    monkeypatch.setattr(FilesystemResultStorage, "store", blocking_store)

    def publish_result() -> bool:
        close_old_connections()
        try:
            current = RayTaskExecution.objects.get(pk=task.pk)
            command = Command()
            command.stdout = StringIO()
            return command._store_and_succeed_task(
                current,
                {"message": "x" * 128},
                expected_attempt_number=2,
                expected_execution_generation=5,
            )
        finally:
            close_old_connections()

    def cancel_result() -> object:
        close_old_connections()
        try:
            return request_task_cancellation(
                task.pk,
                expected_attempt_number=2,
                expected_execution_generation=5,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        publish_future = executor.submit(publish_result)
        assert store_started.wait(timeout=10)
        cancel_future = executor.submit(cancel_result)
        release_store.set()
        assert publish_future.result(timeout=20) is True
        cancellation = cancel_future.result(timeout=20)

    task.refresh_from_db()
    assert cancellation.status == TaskCancellationRequestStatus.ALREADY_TERMINAL
    assert task.state == TaskState.SUCCEEDED
    assert task.result_data is None
    assert task.result_reference is not None
    stored_result = load_result_reference(task.result_reference)
    assert stored_result is not None
    assert json.loads(stored_result) == {"message": "x" * 128}


def test_expired_lease_allows_exactly_one_orphan_adopter() -> None:
    now = datetime.now(UTC)
    TaskWorkerLease.objects.create(
        worker_id="expired-owner",
        hostname="expired-host",
        pid=1001,
        queue_name="default",
        last_heartbeat_at=now - timedelta(hours=1),
        is_active=True,
    )
    TaskWorkerLease.objects.create(
        worker_id="healthy-owner",
        hostname="healthy-host",
        pid=1002,
        queue_name="default",
        last_heartbeat_at=now,
        is_active=True,
    )
    task = _execution(
        "postgres-orphan-adoption-001",
        state=TaskState.RUNNING,
        claimed_by_worker="expired-owner",
        ray_job_id="raysubmit_postgres_orphan",
        ray_address="ray://cluster:10001",
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=5),
    )
    snapshots = [RayTaskExecution.objects.get(pk=task.pk) for _ in range(2)]
    adopters = [
        _claim_command("adopter-a", []),
        _claim_command("adopter-b", []),
    ]

    assert {str(lease.worker_id) for lease in get_active_workers()} == {"healthy-owner"}
    results = _run_concurrently(
        lambda: adopters[0]._adopt_orphaned_ray_job_task(snapshots[0], now=now),
        lambda: adopters[1]._adopt_orphaned_ray_job_task(snapshots[1], now=now),
    )

    assert sorted(results) == [False, True]
    task.refresh_from_db()
    winner = str(task.claimed_by_worker)
    assert winner in {"adopter-a", "adopter-b"}
    winning_command = adopters[0] if winner == "adopter-a" else adopters[1]
    assert winning_command.active_tasks == {task.pk: "raysubmit_postgres_orphan"}


def test_orphan_adoption_invalidates_waiting_stale_lost_transition() -> None:
    now = datetime.now(UTC)
    task = _execution(
        "postgres-orphan-adoption-lost-race-001",
        state=TaskState.RUNNING,
        claimed_by_worker="expired-owner",
        ray_job_id="raysubmit_postgres_orphan_lost_race",
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=5),
        attempt_number=2,
        execution_generation=7,
    )
    stale_lost_snapshot = RayTaskExecution.objects.get(pk=task.pk)
    adopter = _claim_command("replacement-owner", [])
    adoption_locked = Event()
    release_adoption = Event()
    lost_started = Event()

    def adopt_while_holding_execution_lock() -> bool:
        close_old_connections()
        try:
            with transaction.atomic():
                current = RayTaskExecution.objects.select_for_update().get(pk=task.pk)
                adopted = adopter._adopt_orphaned_ray_job_task(current, now=now)
                adoption_locked.set()
                if not release_adoption.wait(timeout=10):
                    raise TimeoutError("test did not release orphan adoption")
                return adopted
        finally:
            close_old_connections()

    def mark_stale_snapshot_lost() -> bool:
        close_old_connections()
        try:
            if not adoption_locked.wait(timeout=10):
                raise TimeoutError("test did not acquire orphan adoption lock")
            lost_started.set()
            return mark_task_lost(stale_lost_snapshot)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        adoption_future = executor.submit(adopt_while_holding_execution_lock)
        assert adoption_locked.wait(timeout=10)
        lost_future = executor.submit(mark_stale_snapshot_lost)
        assert lost_started.wait(timeout=10)
        release_adoption.set()
        assert adoption_future.result(timeout=20) is True
        assert lost_future.result(timeout=20) is False

    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.claimed_by_worker == "replacement-owner"
    assert task.last_heartbeat_at == now
    assert task.attempt_number == 2
    assert task.execution_generation == 7
    assert not TaskAttempt.objects.filter(execution=task).exists()


def test_completion_publication_invalidates_waiting_stale_lost_transition() -> None:
    from django_ray.runtime.entrypoint import _persist_task_completion

    now = datetime.now(UTC)
    task = _execution(
        "postgres-completion-publication-lost-race-001",
        state=TaskState.RUNNING,
        claimed_by_worker="expired-owner",
        ray_job_id="raysubmit_postgres_completed_lost_race",
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=5),
        attempt_number=2,
        execution_generation=7,
    )
    stale_lost_snapshot = RayTaskExecution.objects.get(pk=task.pk)
    completion_data = '{"success": true, "result": 3}'
    publication_locked = Event()
    release_publication = Event()
    lost_started = Event()

    def publish_while_holding_execution_lock() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                RayTaskExecution.objects.select_for_update().get(pk=task.pk)
                _persist_task_completion(
                    task.pk,
                    task.attempt_number,
                    task.execution_generation,
                    completion_data,
                )
                publication_locked.set()
                if not release_publication.wait(timeout=10):
                    raise TimeoutError("test did not release completion publication")
        finally:
            close_old_connections()

    def mark_stale_snapshot_lost() -> bool:
        close_old_connections()
        try:
            if not publication_locked.wait(timeout=10):
                raise TimeoutError("test did not acquire completion publication lock")
            lost_started.set()
            return mark_task_lost(stale_lost_snapshot)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        publication_future = executor.submit(publish_while_holding_execution_lock)
        assert publication_locked.wait(timeout=10)
        lost_future = executor.submit(mark_stale_snapshot_lost)
        assert lost_started.wait(timeout=10)
        release_publication.set()
        publication_future.result(timeout=20)
        assert lost_future.result(timeout=20) is False

    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.completion_data == completion_data
    assert task.attempt_number == 2
    assert task.execution_generation == 7
    assert not TaskAttempt.objects.filter(execution=task).exists()


def test_progress_publication_invalidates_waiting_stale_lost_transition() -> None:
    now = datetime.now(UTC)
    task = _execution(
        "postgres-progress-publication-lost-race-001",
        state=TaskState.RUNNING,
        claimed_by_worker="expired-owner",
        ray_job_id="raysubmit_postgres_progress_lost_race",
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=5),
        attempt_number=2,
        execution_generation=7,
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-00000000012b")
    assert claim_workflow_run(identity) is True
    stale_lost_snapshot = RayTaskExecution.objects.get(pk=task.pk)
    progress_snapshot = _workflow_snapshot(identity, 1)
    publication_locked = Event()
    release_publication = Event()
    lost_started = Event()

    def publish_while_holding_execution_lock() -> bool:
        close_old_connections()
        try:
            with transaction.atomic():
                RayTaskExecution.objects.select_for_update().get(pk=task.pk)
                published = persist_workflow_progress(identity, progress_snapshot)
                publication_locked.set()
                if not release_publication.wait(timeout=10):
                    raise TimeoutError("test did not release workflow progress publication")
                return published
        finally:
            close_old_connections()

    def mark_stale_snapshot_lost() -> bool:
        close_old_connections()
        try:
            if not publication_locked.wait(timeout=10):
                raise TimeoutError("test did not acquire workflow progress publication lock")
            lost_started.set()
            return mark_task_lost(stale_lost_snapshot)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        publication_future = executor.submit(publish_while_holding_execution_lock)
        assert publication_locked.wait(timeout=10)
        lost_future = executor.submit(mark_stale_snapshot_lost)
        assert lost_started.wait(timeout=10)
        release_publication.set()
        assert publication_future.result(timeout=20) is True
        assert lost_future.result(timeout=20) is False

    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert json.loads(task.progress_data or "{}") == progress_snapshot
    assert task.last_heartbeat_at is not None
    assert task.last_heartbeat_at > stale_lost_snapshot.last_heartbeat_at
    assert task.attempt_number == 2
    assert task.execution_generation == 7
    assert not TaskAttempt.objects.filter(execution=task).exists()


def test_completion_retry_and_timeout_race_has_one_winner() -> None:
    task = _execution(
        "postgres-terminal-race-001",
        state=TaskState.RUNNING,
        claimed_by_worker="race-owner",
        execution_generation=7,
        started_at=datetime.now(UTC) - timedelta(minutes=5),
        timeout_seconds=1,
    )
    completion = RayTaskExecution.objects.get(pk=task.pk)
    retry = RayTaskExecution.objects.get(pk=task.pk)
    timeout = RayTaskExecution.objects.get(pk=task.pk)

    results = _run_concurrently(
        lambda: succeed_task(
            completion,
            result_data="3",
            result_reference=None,
            expected_execution_generation=7,
        ),
        lambda: record_failure(
            retry,
            error_message="transient database race",
            retry=True,
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
            expected_execution_generation=7,
        ),
        lambda: mark_task_timed_out(timeout, expected_execution_generation=7),
    )

    assert results.count(True) == 1
    task.refresh_from_db()
    assert task.state in {TaskState.SUCCEEDED, TaskState.QUEUED, TaskState.FAILED}
    if task.state == TaskState.SUCCEEDED:
        assert task.result_data == "3"
    elif task.state == TaskState.QUEUED:
        assert task.attempt_number == 2
        assert task.run_after is not None
    else:
        assert "timed out" in str(task.error_message).lower()


def test_cancellation_and_completion_race_cannot_overwrite_winner() -> None:
    task = _execution(
        "postgres-cancellation-race-001",
        state=TaskState.RUNNING,
        claimed_by_worker="cancellation-owner",
        execution_generation=3,
        started_at=datetime.now(UTC),
    )
    cancellation = RayTaskExecution.objects.get(pk=task.pk)
    completion = RayTaskExecution.objects.get(pk=task.pk)

    results = _run_concurrently(
        lambda: request_task_cancellation(
            cancellation.pk,
            expected_attempt_number=1,
            expected_execution_generation=3,
        ),
        lambda: succeed_task(
            completion,
            result_data="3",
            result_reference=None,
            expected_execution_generation=3,
        ),
    )

    cancellation_result = results[0]
    completion_result = results[1]
    task.refresh_from_db()
    if cancellation_result.accepted:
        assert completion_result is False
        assert task.state == TaskState.CANCELLING
        assert finalize_cancellation(task, expected_worker_id="cancellation-owner") is True
        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.result_data is None
    else:
        assert cancellation_result.status is TaskCancellationRequestStatus.ALREADY_TERMINAL
        assert completion_result is True
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        duplicate = request_task_cancellation(
            cancellation.pk,
            expected_attempt_number=1,
            expected_execution_generation=3,
        )
        assert duplicate.status is TaskCancellationRequestStatus.ALREADY_TERMINAL
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED


def test_cancellation_and_entrypoint_publication_preserve_lock_winner() -> None:
    from django_ray.runtime.entrypoint import _persist_task_completion

    task = _execution(
        "postgres-cancellation-publication-race-001",
        state=TaskState.RUNNING,
        claimed_by_worker="cancellation-owner",
        attempt_number=2,
        execution_generation=3,
        started_at=datetime.now(UTC),
    )
    completion_data = '{"success": true, "result": 3}'

    results = _run_concurrently(
        lambda: request_task_cancellation(
            task.pk,
            expected_attempt_number=2,
            expected_execution_generation=3,
        ),
        lambda: _persist_task_completion(
            task.pk,
            2,
            3,
            completion_data,
        ),
    )

    cancellation_result = results[0]
    task.refresh_from_db()
    if cancellation_result.accepted:
        assert cancellation_result.state == TaskState.CANCELLING
        assert task.state == TaskState.CANCELLING
        assert task.completion_data is None
    else:
        assert cancellation_result.status is TaskCancellationRequestStatus.COMPLETION_PENDING
        assert task.state == TaskState.RUNNING
        assert task.completion_data == completion_data
    assert not TaskAttempt.objects.filter(execution=task).exists()


def test_concurrent_manual_retry_advances_attempt_and_generation_once() -> None:
    task = _execution(
        "postgres-manual-retry-race-001",
        state=TaskState.FAILED,
        attempt_number=3,
        execution_generation=7,
        error_message="retryable failure",
    )

    results = _run_concurrently(
        lambda: retry_task(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=7,
        ),
        lambda: retry_task(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=7,
        ),
    )

    assert sum(result is not None for result in results) == 1
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 4
    assert task.execution_generation == 8
    history = TaskAttempt.objects.get(execution=task, attempt_number=3)
    assert history.state == TaskState.FAILED
    assert history.error_message == "retryable failure"
    assert (
        retry_task(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=7,
        )
        is None
    )


def test_stale_unknown_stop_holds_execution_lock_until_outcome_is_durable(
    monkeypatch,
) -> None:
    import time
    from threading import get_ident

    import django_ray.lifecycle as lifecycle_module
    from django_ray.management.commands.django_ray_worker import Command
    from django_ray.runner.base import JobInfo, JobStatus
    from django_ray.runner.cancellation import (
        CancellationOutcome,
        CancellationOutcomeStatus,
    )

    task = _execution(
        "postgres-unknown-stop-retry-race-001",
        state=TaskState.RUNNING,
        claimed_by_worker="postgres-unknown-worker",
        ray_job_id="raysubmit_postgres_unknown_stop_retry",
        ray_address="http://ray-dashboard:8265",
        started_at=datetime.now(UTC) - timedelta(minutes=10),
        last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        attempt_number=2,
        execution_generation=7,
    )
    stop_started = Event()
    release_stop = Event()
    retry_started = Event()
    retry_backend_pid: list[int] = []
    retry_thread_id: list[int] = []
    retry_observed_cancellation_status: list[str | None] = []
    original_record_attempt = lifecycle_module._record_attempt

    def capture_retry_snapshot(execution: RayTaskExecution) -> None:
        if retry_thread_id and get_ident() == retry_thread_id[0]:
            retry_observed_cancellation_status.append(execution.cancellation_status)
        original_record_attempt(execution)

    monkeypatch.setattr(lifecycle_module, "_record_attempt", capture_retry_snapshot)

    class BlockingRunner:
        def get_status(self, _handle) -> JobInfo:
            return JobInfo(
                job_id=task.ray_job_id or "",
                status=JobStatus.UNKNOWN,
                message="status unavailable",
            )

        def cancel_with_status(self, _handle) -> CancellationOutcome:
            stop_started.set()
            if not release_stop.wait(timeout=10):
                raise TimeoutError("test did not release the exact stop request")
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

    def reconcile_unknown() -> None:
        close_old_connections()
        try:
            current = RayTaskExecution.objects.get(pk=task.pk)
            command = Command()
            command.stdout = StringIO()
            command.worker_id = "postgres-unknown-worker"
            command.active_tasks = {task.pk: task.ray_job_id or ""}
            command.active_task_identities = {
                task.pk: (task.attempt_number, task.execution_generation)
            }
            command._reconcile_ray_job_task(
                current,
                BlockingRunner(),
                ray_job_id=task.ray_job_id or "",
                completed_tasks=[],
                orphaned=False,
                tracked_identity=(task.attempt_number, task.execution_generation),
            )
        finally:
            close_old_connections()

    def retry_lost() -> RayTaskExecution | None:
        close_old_connections()
        try:
            retry_thread_id.append(get_ident())
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                retry_backend_pid.append(int(cursor.fetchone()[0]))
            retry_started.set()
            return retry_task(
                task.pk,
                allowed_states=(TaskState.LOST,),
                expected_attempt_number=2,
                expected_execution_generation=7,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reconciliation_future = executor.submit(reconcile_unknown)
        assert stop_started.wait(timeout=10)
        retry_future = executor.submit(retry_lost)
        assert retry_started.wait(timeout=10)

        try:
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
                        [retry_backend_pid[0]],
                    )
                    lock_wait = cursor.fetchone()
                if lock_wait is not None and lock_wait[0] == "Lock":
                    break
                time.sleep(0.01)

            assert lock_wait is not None
            assert lock_wait[0] == "Lock"
            assert retry_future.done() is False
            visible = RayTaskExecution.objects.get(pk=task.pk)
            assert visible.state == TaskState.RUNNING
            assert visible.cancellation_status is None
        finally:
            release_stop.set()

        reconciliation_future.result(timeout=20)
        retried = retry_future.result(timeout=20)

    assert retried is not None
    assert retry_observed_cancellation_status == ["REQUESTED"]
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 3
    assert task.execution_generation == 8
    assert task.cancellation_status is None


def test_automatic_retry_and_stale_cancellation_cannot_control_same_attempt() -> None:
    task = _execution(
        "postgres-auto-retry-cancel-race-001",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=7,
    )
    retry_execution = RayTaskExecution.objects.get(pk=task.pk)

    results = _run_concurrently(
        lambda: record_failure(
            retry_execution,
            error_message="automatic retry",
            retry=True,
            expected_execution_generation=7,
        ),
        lambda: request_task_cancellation(
            task.pk,
            expected_attempt_number=2,
            expected_execution_generation=7,
        ),
    )

    retry_result = results[0]
    cancellation_result = results[1]
    task.refresh_from_db()
    if retry_result:
        assert cancellation_result.status is TaskCancellationRequestStatus.STALE_ATTEMPT
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 3
        assert task.execution_generation == 7
        assert TaskAttempt.objects.get(execution=task, attempt_number=2).state == TaskState.FAILED
    else:
        assert cancellation_result.status is TaskCancellationRequestStatus.ACCEPTED
        assert task.state == TaskState.CANCELLING
        assert task.attempt_number == 2


def test_stale_generation_cannot_write_over_replacement_execution() -> None:
    stale = _execution(
        "postgres-generation-guard-001",
        state=TaskState.RUNNING,
        claimed_by_worker="old-worker",
        execution_generation=4,
        ray_job_id="raysubmit_old",
        started_at=datetime.now(UTC),
    )
    RayTaskExecution.objects.filter(pk=stale.pk).update(
        claimed_by_worker="new-worker",
        execution_generation=5,
        ray_job_id="raysubmit_new",
        result_data=None,
    )

    assert (
        succeed_task(
            stale,
            result_data='"stale"',
            result_reference=None,
            expected_ray_job_id="raysubmit_old",
            expected_execution_generation=4,
        )
        is False
    )
    assert (
        record_failure(
            stale,
            error_message="stale failure",
            retry=False,
            expected_ray_job_id="raysubmit_old",
            expected_execution_generation=4,
        )
        is False
    )

    stale.refresh_from_db()
    assert stale.state == TaskState.RUNNING
    assert stale.claimed_by_worker == "new-worker"
    assert stale.execution_generation == 5
    assert stale.ray_job_id == "raysubmit_new"
    assert stale.result_data is None


def test_workflow_progress_retry_race_cannot_resurrect_cleared_snapshot() -> None:
    task = _execution(
        "postgres-workflow-retry-race-001",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=7,
        started_at=datetime.now(UTC),
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-000000000021")
    assert claim_workflow_run(identity) is True
    assert persist_workflow_progress(identity, _workflow_snapshot(identity, 1)) is True
    retry = RayTaskExecution.objects.get(pk=task.pk)

    results = _run_concurrently(
        lambda: record_failure(
            retry,
            error_message="retry workflow",
            retry=True,
            next_attempt_at=datetime.now(UTC),
            expected_execution_generation=7,
        ),
        lambda: persist_workflow_progress(identity, _workflow_snapshot(identity, 2)),
    )

    assert results[0] is True
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 2
    assert task.workflow_run_id is None
    assert task.progress_data is None
    assert persist_workflow_progress(identity, _workflow_snapshot(identity, 3)) is False


def test_v3_summary_retry_race_cannot_resurrect_cleared_summary() -> None:
    task = _execution(
        "postgres-v3-summary-retry-race-001",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=7,
        started_at=datetime.now(UTC),
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-000000000125")
    assert claim_workflow_run(identity)
    task.refresh_from_db(fields=["workflow_run_id"])
    assert persist_workflow_progress_summary(identity, workflow_progress_summary(task))
    retry = RayTaskExecution.objects.get(pk=task.pk)
    late_summary = workflow_progress_summary(task, summary_revision=3)
    terminal_summary = workflow_progress_summary(
        task,
        summary_revision=2,
        state="FAILED",
    )
    terminal_serialized = serialize_workflow_progress_summary(terminal_summary)

    results = _run_concurrently(
        lambda: record_failure(
            retry,
            error_message="retry bounded summary",
            retry=True,
            next_attempt_at=datetime.now(UTC),
            expected_execution_generation=7,
        ),
        lambda: persist_workflow_progress_summary(
            identity,
            terminal_summary,
        ),
    )

    assert results[0] is True
    assert results[1] in {True, False}
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 2
    assert task.workflow_run_id is None
    assert task.workflow_progress_summary_json is None
    attempt = TaskAttempt.objects.get(execution=task, attempt_number=1)
    if results[1] is True:
        assert attempt.workflow_progress_summary_json == terminal_serialized
    else:
        archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
        assert archived["state"] == "FAILED"
        assert archived["terminal"]["outcome"] == "FAILED"
    assert (
        persist_workflow_progress_summary(
            identity,
            late_summary,
        )
        is False
    )


def test_v3_summary_terminal_race_leaves_terminal_state_authoritative() -> None:
    task = _execution(
        "postgres-v3-summary-terminal-race-001",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=8,
        started_at=datetime.now(UTC),
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-000000000126")
    assert claim_workflow_run(identity)
    task.refresh_from_db(fields=["workflow_run_id"])
    assert persist_workflow_progress_summary(identity, workflow_progress_summary(task))
    completion = RayTaskExecution.objects.get(pk=task.pk)
    terminal_summary = workflow_progress_summary(
        task,
        summary_revision=2,
        state="SUCCEEDED",
    )
    terminal_serialized = serialize_workflow_progress_summary(terminal_summary)

    results = _run_concurrently(
        lambda: succeed_task(
            completion,
            result_data="3",
            result_reference=None,
            expected_execution_generation=8,
        ),
        lambda: persist_workflow_progress_summary(
            identity,
            terminal_summary,
        ),
    )

    assert results[0] is True
    assert results[1] in {True, False}
    task.refresh_from_db()
    assert task.state == TaskState.SUCCEEDED
    attempt = TaskAttempt.objects.get(execution=task, attempt_number=1)
    if results[1] is True:
        assert attempt.workflow_progress_summary_json == terminal_serialized
    else:
        archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
        assert archived["state"] == "SUCCEEDED"
        assert archived["terminal"]["outcome"] == "SUCCEEDED"
    assert (
        persist_workflow_progress_summary(
            identity,
            workflow_progress_summary(task, summary_revision=3),
        )
        is False
    )


def test_v3_conflicting_terminal_writer_cannot_override_lost_outcome() -> None:
    task = _execution(
        "postgres-v3-conflicting-terminal-race-001",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=9,
        started_at=datetime.now(UTC),
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-00000000012a")
    assert claim_workflow_run(identity)
    task.refresh_from_db(fields=["workflow_run_id"])
    assert persist_workflow_progress_summary(identity, workflow_progress_summary(task))
    lost = RayTaskExecution.objects.get(pk=task.pk)
    succeeded = workflow_progress_summary(
        task,
        summary_revision=2,
        state="SUCCEEDED",
    )

    results = _run_concurrently(
        lambda: mark_task_lost(lost),
        lambda: persist_workflow_progress_summary(identity, succeeded),
    )

    assert results in ([True, False], [False, True])
    if results[0] is False:
        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert mark_task_lost(task) is True

    task.refresh_from_db()
    attempt = TaskAttempt.objects.get(execution=task, attempt_number=1)
    archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
    assert task.state == TaskState.LOST
    assert archived["state"] == "LOST"
    assert archived["terminal"]["outcome"] == "LOST"
    assert archived["summary_revision"] == (3 if results[1] is True else 2)
    assert persist_workflow_progress_summary(identity, succeeded) is False


def test_v3_summary_writer_rolls_back_with_owning_transaction() -> None:
    task = _execution(
        "postgres-v3-summary-rollback-001",
        state=TaskState.RUNNING,
        execution_generation=2,
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-000000000127")
    assert claim_workflow_run(identity)
    task.refresh_from_db(fields=["workflow_run_id"])

    with pytest.raises(RuntimeError, match="roll back summary"):
        with transaction.atomic():
            assert persist_workflow_progress_summary(identity, workflow_progress_summary(task))
            raise RuntimeError("roll back summary")

    task.refresh_from_db()
    assert task.workflow_progress_summary_json is None


def test_postgresql_reader_bounds_legacy_text_before_transfer(monkeypatch) -> None:
    task = _execution(
        "postgres-bounded-legacy-reader-001",
        state=TaskState.RUNNING,
        progress_data=json.dumps({"schema_version": 1, "message": "x" * 1_000}),
    )
    monkeypatch.setattr(workflow_progress_module, "WORKFLOW_PROGRESS_LEGACY_MAX_BYTES", 128)

    with CaptureQueriesContext(connection) as queries:
        result = read_workflow_progress(task)

    assert result.diagnostic_code is WorkflowProgressDiagnosticCode.LEGACY_OVERSIZED
    assert result.payload is None
    selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 1
    assert "OCTET_LENGTH" in selects[0].upper()


def test_workflow_progress_cancellation_race_disables_late_writer() -> None:
    task = _execution(
        "postgres-workflow-cancel-race-001",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
        started_at=datetime.now(UTC),
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-000000000022")
    assert claim_workflow_run(identity) is True
    cancellation = RayTaskExecution.objects.get(pk=task.pk)

    results = _run_concurrently(
        lambda: request_task_cancellation(
            cancellation.pk,
            expected_attempt_number=2,
            expected_execution_generation=4,
        ),
        lambda: persist_workflow_progress(identity, _workflow_snapshot(identity, 1)),
    )

    assert results[0].accepted is True
    task.refresh_from_db()
    assert task.state == TaskState.CANCELLING
    assert persist_workflow_progress(identity, _workflow_snapshot(identity, 2)) is False


def test_workflow_progress_timeout_race_cannot_write_after_terminal_state() -> None:
    task = _execution(
        "postgres-workflow-timeout-race-001",
        state=TaskState.RUNNING,
        attempt_number=3,
        execution_generation=8,
        started_at=datetime.now(UTC) - timedelta(minutes=5),
        timeout_seconds=1,
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-000000000023")
    assert claim_workflow_run(identity) is True
    timeout = RayTaskExecution.objects.get(pk=task.pk)

    results = _run_concurrently(
        lambda: mark_task_timed_out(timeout, expected_execution_generation=8),
        lambda: persist_workflow_progress(identity, _workflow_snapshot(identity, 1)),
    )

    assert results[0] is True
    task.refresh_from_db()
    assert task.state == TaskState.FAILED
    assert persist_workflow_progress(identity, _workflow_snapshot(identity, 2)) is False


def test_workflow_progress_lost_recovery_clears_obsolete_run() -> None:
    task = _execution(
        "postgres-workflow-lost-race-001",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=5,
        started_at=datetime.now(UTC),
    )
    identity = _workflow_identity(task, "00000000-0000-0000-0000-000000000024")
    assert claim_workflow_run(identity) is True
    lost = RayTaskExecution.objects.get(pk=task.pk)

    def recover_lost() -> bool:
        mark_task_lost(lost)
        return (
            retry_task(
                lost.pk,
                allowed_states=(TaskState.LOST,),
                expected_attempt_number=lost.attempt_number,
                expected_execution_generation=lost.execution_generation,
            )
            is not None
        )

    results = _run_concurrently(
        recover_lost,
        lambda: persist_workflow_progress(identity, _workflow_snapshot(identity, 1)),
    )

    # Either exact fence may win: a fresh progress publication invalidates the
    # stale LOST snapshot, while a committed LOST/retry invalidates the writer.
    assert results in ([True, False], [False, True])
    if results[0] is False:
        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert json.loads(task.progress_data or "{}") == _workflow_snapshot(identity, 1)
        assert mark_task_lost(task) is True
        assert (
            retry_task(
                task.pk,
                allowed_states=(TaskState.LOST,),
                expected_attempt_number=task.attempt_number,
                expected_execution_generation=task.execution_generation,
            )
            is not None
        )

    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 2
    assert task.execution_generation == 6
    assert task.workflow_run_id is None
    assert task.progress_data is None
    assert persist_workflow_progress(identity, _workflow_snapshot(identity, 2)) is False


def test_workflow_run_claim_is_consistent_for_concurrent_reader() -> None:
    task = _execution(
        "postgres-workflow-reader-race-001",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=2,
    )
    old = _workflow_identity(task, "00000000-0000-0000-0000-000000000025")
    replacement = _workflow_identity(task, "00000000-0000-0000-0000-000000000026")
    assert claim_workflow_run(old) is True
    assert persist_workflow_progress(old, _workflow_snapshot(old, 1)) is True

    def read_progress_pair() -> tuple[str | None, str | None]:
        current = RayTaskExecution.objects.get(pk=task.pk)
        current_run_id = (
            str(current.workflow_run_id) if current.workflow_run_id is not None else None
        )
        if current.progress_data is None:
            return current_run_id, None
        snapshot_run_id = json.loads(current.progress_data)["run_identity"]["run_id"]
        return current_run_id, snapshot_run_id

    results = _run_concurrently(
        lambda: claim_workflow_run(replacement),
        read_progress_pair,
    )

    assert results[0] is True
    reader_result = results[1]
    assert isinstance(reader_result, tuple)
    observed_run_id, snapshot_run_id = reader_result
    assert snapshot_run_id is None or observed_run_id == snapshot_run_id
    task.refresh_from_db()
    assert str(task.workflow_run_id) == replacement.run_id
    assert task.progress_data is None
    assert persist_workflow_progress(old, _workflow_snapshot(old, 2)) is False


def test_concurrent_replacement_claims_delete_only_superseded_run_storage() -> None:
    task = _execution(
        "postgres-workflow-storage-replacement-race-001",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    old = _workflow_identity(task, "00000000-0000-0000-0000-000000000131")
    replacement_a = _workflow_identity(task, "00000000-0000-0000-0000-000000000132")
    replacement_b = _workflow_identity(task, "00000000-0000-0000-0000-000000000133")
    unrelated_run_id = "00000000-0000-0000-0000-000000000134"
    assert claim_workflow_run(old) is True
    for run_id, attempt_number, execution_generation in (
        (old.run_id, 2, 5),
        (replacement_a.run_id, 2, 5),
        (replacement_b.run_id, 2, 5),
        (unrelated_run_id, 2, 5),
        (old.run_id, 1, 4),
    ):
        WorkflowProgressRunStorage.objects.create(
            execution=task,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
            run_id=run_id,
        )

    results = _run_concurrently(
        lambda: claim_workflow_run(replacement_a),
        lambda: claim_workflow_run(replacement_b),
    )

    assert results == [True, True]
    task.refresh_from_db()
    final_run_id = str(task.workflow_run_id)
    assert final_run_id in {replacement_a.run_id, replacement_b.run_id}
    current_run_ids = {
        str(run_id)
        for run_id in WorkflowProgressRunStorage.objects.filter(
            execution=task,
            attempt_number=2,
            execution_generation=5,
        ).values_list("run_id", flat=True)
    }
    assert current_run_ids == {final_run_id, unrelated_run_id}
    assert WorkflowProgressRunStorage.objects.filter(
        execution=task,
        attempt_number=1,
        execution_generation=4,
        run_id=old.run_id,
    ).exists()


def test_input_cleanup_racing_reenqueue_preserves_shared_payload(settings, tmp_path) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    large_value = "x" * 2048
    prepared = prepare_task_input((large_value,), {})
    assert prepared.input_reference is not None

    with transaction.atomic():
        payload = register_task_input(prepared)
        assert payload is not None
        old_execution = _execution(
            "postgres-input-cleanup-race-old",
            state=TaskState.SUCCEEDED,
            args_json=prepared.args_json,
            kwargs_json=prepared.kwargs_json,
            input_reference=prepared.input_reference,
            finished_at=timezone.now() - timedelta(days=60),
        )

    old_timestamp = timezone.now() - timedelta(days=60)
    TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=old_timestamp)
    cutoff = timezone.now() - timedelta(days=30)

    def purge() -> object:
        return PurgeInputsCommand()._process_reference(
            prepared.input_reference,
            cutoff=cutoff,
            delete=True,
        )

    def reenqueue() -> object:
        with transaction.atomic():
            register_task_input(prepared)
            return _execution(
                "postgres-input-cleanup-race-new",
                args_json=prepared.args_json,
                kwargs_json=prepared.kwargs_json,
                input_reference=prepared.input_reference,
            ).pk

    _run_concurrently(purge, reenqueue)

    payload.refresh_from_db()
    old_execution.refresh_from_db()
    replacement = RayTaskExecution.objects.get(task_id="postgres-input-cleanup-race-new")
    assert payload.state == InputPayloadState.ACTIVE
    assert replacement.state == TaskState.QUEUED
    assert replacement.input_reference == prepared.input_reference
    assert old_execution.input_reference == prepared.input_reference
    assert load_task_input(
        args_json=replacement.args_json,
        kwargs_json=replacement.kwargs_json,
        input_reference=replacement.input_reference,
    ) == ([large_value], {})
