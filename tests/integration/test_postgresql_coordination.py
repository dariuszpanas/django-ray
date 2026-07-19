"""PostgreSQL-only tests for contested worker coordination paths."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Barrier, Event

import pytest
from django.db import close_old_connections, connection, transaction

from django_ray.lifecycle import record_failure, succeed_task
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.cancellation import finalize_cancellation, request_cancellation
from django_ray.runner.leasing import get_active_workers
from django_ray.runner.reconciliation import mark_task_timed_out

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

    class Runner:
        def cancel(self, _handle: object) -> bool:
            return True

    results = _run_concurrently(
        lambda: request_cancellation(cancellation, Runner()),
        lambda: succeed_task(
            completion,
            result_data="3",
            result_reference=None,
            expected_execution_generation=3,
        ),
    )

    assert results.count(True) == 1
    task.refresh_from_db()
    if task.state == TaskState.CANCELLING:
        assert finalize_cancellation(task, expected_worker_id="cancellation-owner") is True
        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.result_data is None
    else:
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        assert request_cancellation(cancellation, Runner()) is False
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED


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
