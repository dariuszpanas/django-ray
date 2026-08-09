"""PostgreSQL-only tests for contested worker coordination paths."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Barrier, Event, Lock, get_ident
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

import django_ray.admin as django_ray_admin
import django_ray.workflow_progress as workflow_progress_module
from django_ray.input_storage import (
    EXTERNAL_INPUT_PLACEHOLDER,
    load_task_input,
    prepare_task_input,
    register_task_input,
)
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    record_failure,
    request_task_cancellation,
    retry_task,
    succeed_task,
)
from django_ray.management.commands.django_ray_purge_inputs import Command as PurgeInputsCommand
from django_ray.models import (
    CancellationStatus,
    InputPayloadState,
    RayTaskExecution,
    TaskAttempt,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressRunStorage,
)
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    finalize_cancellation,
)
from django_ray.runner.leasing import WorkerLeaseIdentity, get_active_workers
from django_ray.runner.reconciliation import mark_task_lost, mark_task_timed_out
from django_ray.runtime.context import DurableTaskContext, WorkflowRunIdentity
from django_ray.workflow_progress import (
    WorkflowProgressDiagnosticCode,
    allocate_workflow_run,
    persist_workflow_progress,
    persist_workflow_progress_summary,
    read_workflow_progress,
)
from django_ray.workflow_progress_summary import (
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)
from testproject import api as testproject_api
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
    }
    values.update(overrides)
    return RayTaskExecution.objects.create(**values)


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


def test_concurrent_worker_lease_collision_allocates_two_exact_owners(monkeypatch) -> None:
    from django_ray.management.commands.django_ray_worker import Command

    worker_a = Command()
    worker_a.stdout = StringIO()
    worker_a._set_worker_id("postgres-shared-candidate")
    worker_b = Command()
    worker_b.stdout = StringIO()
    worker_b._set_worker_id("postgres-shared-candidate")
    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.generate_worker_id",
        lambda: "postgres-regenerated-worker",
    )

    _run_concurrently(
        lambda: worker_a._create_lease("default"),
        lambda: worker_b._create_lease("default"),
    )

    assert {worker_a.worker_id, worker_b.worker_id} == {
        "postgres-shared-candidate",
        "postgres-regenerated-worker",
    }
    assert worker_a.lease_identity is not None
    assert worker_b.lease_identity is not None
    assert worker_a.lease_identity != worker_b.lease_identity
    assert (
        TaskWorkerLease.objects.filter(
            **worker_a.lease_identity.database_filters(),
            is_active=True,
        ).count()
        == 1
    )
    assert (
        TaskWorkerLease.objects.filter(
            **worker_b.lease_identity.database_filters(),
            is_active=True,
        ).count()
        == 1
    )
    assert (
        TaskWorkerLease.objects.filter(
            worker_id__in=("postgres-shared-candidate", "postgres-regenerated-worker")
        ).count()
        == 2
    )


def test_unrelated_postgresql_integrity_error_does_not_regenerate_identity(
    monkeypatch,
) -> None:
    from django_ray.management.commands.django_ray_worker import Command

    command = Command()
    command.stdout = StringIO()
    command._set_worker_id("postgres-unrelated-constraint")
    generated: list[str] = []
    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.generate_worker_id",
        lambda: generated.append("unexpected-regeneration") or "unexpected-regeneration",
    )

    class OtherConstraintViolationError(Exception):
        pass

    def fail_with_other_constraint(**_kwargs) -> None:
        cause = OtherConstraintViolationError("unrelated constraint")
        cause.diag = SimpleNamespace(constraint_name="unrelated_constraint")  # type: ignore[attr-defined]
        try:
            raise cause
        except OtherConstraintViolationError as driver_error:
            raise IntegrityError("unrelated lease constraint") from driver_error

    monkeypatch.setattr(TaskWorkerLease.objects, "create", fail_with_other_constraint)

    with pytest.raises(CommandError, match="Could not create worker lease"):
        command._create_lease("default")

    assert generated == []
    assert command.lease_identity is None


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

    assert {str(lease.worker_id) for lease in get_active_workers()} == {
        "adopter-a",
        "adopter-b",
        "healthy-owner",
    }
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
    expired_lease = TaskWorkerLease.objects.get(worker_id="expired-owner")
    assert expired_lease.is_active is False


def test_admin_bulk_deactivation_locks_leases_in_worker_id_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.contrib.admin import AdminSite
    from django.http import HttpRequest

    from django_ray.admin import TaskWorkerLeaseAdmin

    for worker_id in ("postgres-admin-lock-b", "postgres-admin-lock-a"):
        TaskWorkerLease.objects.create(
            worker_id=worker_id,
            hostname=f"{worker_id}-host",
            pid=1000,
            queue_name="default",
            is_active=True,
        )
    admin_object = TaskWorkerLeaseAdmin(TaskWorkerLease, AdminSite())
    monkeypatch.setattr(admin_object, "message_user", lambda *_args, **_kwargs: None)
    selected = TaskWorkerLease.objects.filter(
        worker_id__startswith="postgres-admin-lock-"
    ).order_by("-worker_id")

    with CaptureQueriesContext(connection) as queries:
        admin_object.mark_inactive(HttpRequest(), selected)

    lock_queries = [
        query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"].upper()
    ]
    assert len(lock_queries) == 1
    assert "ORDER BY" in lock_queries[0].upper()
    assert "worker_id" in lock_queries[0]
    assert not TaskWorkerLease.objects.filter(
        worker_id__startswith="postgres-admin-lock-",
        is_active=True,
    ).exists()


def test_admin_bulk_deletion_locks_inactive_leases_in_worker_id_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.contrib.admin import AdminSite
    from django.http import HttpRequest

    from django_ray.admin import TaskWorkerLeaseAdmin

    for worker_id in ("postgres-admin-delete-b", "postgres-admin-delete-a"):
        TaskWorkerLease.objects.create(
            worker_id=worker_id,
            hostname=f"{worker_id}-host",
            pid=1000,
            queue_name="default",
            is_active=False,
        )
    admin_object = TaskWorkerLeaseAdmin(TaskWorkerLease, AdminSite())
    monkeypatch.setattr(admin_object, "message_user", lambda *_args, **_kwargs: None)
    selected = TaskWorkerLease.objects.filter(
        worker_id__startswith="postgres-admin-delete-"
    ).order_by("-worker_id")

    with CaptureQueriesContext(connection) as queries:
        admin_object.delete_inactive(HttpRequest(), selected)

    lock_queries = [
        query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"].upper()
    ]
    assert len(lock_queries) == 1
    assert "ORDER BY" in lock_queries[0].upper()
    assert "worker_id" in lock_queries[0]
    assert not TaskWorkerLease.objects.filter(
        worker_id__startswith="postgres-admin-delete-"
    ).exists()


def test_postgresql_lease_freshness_is_measured_after_waiting_for_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.management.commands.django_ray_worker as worker_module

    command = _claim_command("postgres-clock-candidate", [])
    assert command.lease_identity is not None
    early = datetime.now(UTC)
    initial_heartbeat = early - timedelta(seconds=1)
    TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).update(
        last_heartbeat_at=initial_heartbeat
    )
    task = _execution(
        "postgres-post-lock-freshness-001",
        state=TaskState.RUNNING,
        claimed_by_worker=command.worker_id,
        started_at=early,
        last_heartbeat_at=early,
    )
    snapshot = RayTaskExecution.objects.get(pk=task.pk)
    current_time = early

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time if tz is not None else current_time.replace(tzinfo=None)

    monkeypatch.setattr(worker_module, "datetime", ControlledDateTime)
    monkeypatch.setattr(worker_module, "get_lease_duration", lambda: timedelta(seconds=2))
    lease_locked = Event()
    release_lease = Event()
    recovery_started = Event()
    recovery_backend_pid: list[int] = []

    def hold_candidate_lease() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                TaskWorkerLease.objects.select_for_update().get(
                    **command.lease_identity.database_filters()
                )
                lease_locked.set()
                if not release_lease.wait(timeout=10):
                    raise TimeoutError("test did not release candidate lease")
        finally:
            close_old_connections()

    def attempt_authoritative_mutation() -> bool:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                recovery_backend_pid.append(int(cursor.fetchone()[0]))
            recovery_started.set()
            with command._authoritative_task_owner(
                snapshot,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
            ) as owned:
                if owned is None:
                    return False
                owned.execution.error_message = "stale clock admitted mutation"
                owned.execution.save(update_fields=["error_message"])
                return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_candidate_lease)
        assert lease_locked.wait(timeout=10)
        recovery = executor.submit(attempt_authoritative_mutation)
        assert recovery_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(recovery_backend_pid[0])
            current_time = early + timedelta(seconds=3)
        finally:
            release_lease.set()
        holder.result(timeout=20)
        assert recovery.result(timeout=20) is False

    task.refresh_from_db()
    lease = TaskWorkerLease.objects.get(**command.lease_identity.database_filters())
    assert task.error_message is None
    assert lease.last_heartbeat_at == initial_heartbeat
    assert command.lease_ownership_lost is True


def test_concurrent_timeout_recovery_issues_one_stop_and_one_terminal_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.management.commands.django_ray_worker as worker_module

    now = datetime.now(UTC)
    source_lease = TaskWorkerLease.objects.create(
        worker_id="postgres-timeout-source",
        hostname="expired-host",
        pid=1101,
        queue_name="default",
        last_heartbeat_at=now - timedelta(hours=1),
        is_active=True,
    )
    recoverers = [
        _claim_command("postgres-timeout-recoverer-a", []),
        _claim_command("postgres-timeout-recoverer-b", []),
    ]
    task = _execution(
        "postgres-concurrent-timeout-recovery-001",
        state=TaskState.RUNNING,
        claimed_by_worker=source_lease.worker_id,
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=5),
        timeout_seconds=1,
        attempt_number=3,
        execution_generation=7,
    )
    cancellation_calls: list[int] = []
    effect_workers: list[str] = []
    effect_started = Event()
    release_effect = Event()
    snapshot_barrier = Barrier(2)
    monkeypatch.setattr(
        worker_module,
        "is_task_timed_out",
        _synchronize_first_predicate_call(worker_module.is_task_timed_out, snapshot_barrier),
    )

    for recoverer in recoverers:

        def request_stop(
            current: RayTaskExecution,
            *,
            worker_id: str = recoverer.worker_id,
        ) -> CancellationOutcome:
            cancellation_calls.append(current.pk)
            effect_workers.append(worker_id)
            effect_started.set()
            if not release_effect.wait(timeout=10):
                raise TimeoutError("test did not release timeout recovery")
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr(recoverer, "_request_timeout_cancellation", request_stop)

    results = _run_contended_recovery(
        recoverers,
        lambda recoverer: recoverer.detect_stuck_tasks(),
        effect_started=effect_started,
        release_effect=release_effect,
        effect_workers=effect_workers,
    )

    task.refresh_from_db()
    source_lease.refresh_from_db()
    assert sorted(results) == [0, 1]
    assert cancellation_calls == [task.pk]
    assert task.state == TaskState.FAILED
    assert task.claimed_by_worker in {recoverer.worker_id for recoverer in recoverers}
    assert task.cancellation_status == CancellationStatus.REQUESTED
    assert task.finished_at is not None
    assert source_lease.is_active is False
    assert TaskAttempt.objects.filter(execution=task, attempt_number=3).count() == 1


def test_concurrent_lost_recovery_has_one_owner_and_one_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.management.commands.django_ray_worker as worker_module

    now = datetime.now(UTC)
    source_lease = TaskWorkerLease.objects.create(
        worker_id="postgres-lost-source",
        hostname="expired-host",
        pid=1201,
        queue_name="default",
        last_heartbeat_at=now - timedelta(hours=1),
        is_active=True,
    )
    recoverers = [
        _claim_command("postgres-lost-recoverer-a", []),
        _claim_command("postgres-lost-recoverer-b", []),
    ]
    task = _execution(
        "postgres-concurrent-lost-recovery-001",
        state=TaskState.RUNNING,
        claimed_by_worker=source_lease.worker_id,
        started_at=now - timedelta(minutes=10),
        last_heartbeat_at=now - timedelta(minutes=10),
        attempt_number=3,
        execution_generation=7,
    )
    effect_workers: list[str] = []
    effect_started = Event()
    release_effect = Event()
    snapshot_barrier = Barrier(2)
    monkeypatch.setattr(
        worker_module,
        "is_task_stuck",
        _synchronize_first_predicate_call(worker_module.is_task_stuck, snapshot_barrier),
    )
    original_mark_task_lost = worker_module.mark_task_lost

    def hold_mark_task_lost(current: RayTaskExecution) -> bool:
        effect_workers.append(str(current.claimed_by_worker))
        effect_started.set()
        if not release_effect.wait(timeout=10):
            raise TimeoutError("test did not release LOST recovery")
        return original_mark_task_lost(current)

    monkeypatch.setattr(worker_module, "mark_task_lost", hold_mark_task_lost)

    results = _run_contended_recovery(
        recoverers,
        lambda recoverer: recoverer.detect_stuck_tasks(),
        effect_started=effect_started,
        release_effect=release_effect,
        effect_workers=effect_workers,
    )

    task.refresh_from_db()
    source_lease.refresh_from_db()
    assert sorted(results) == [0, 1]
    assert task.state == TaskState.LOST
    assert task.claimed_by_worker in {recoverer.worker_id for recoverer in recoverers}
    assert task.finished_at is not None
    assert source_lease.is_active is False
    assert TaskAttempt.objects.filter(execution=task, attempt_number=3).count() == 1


def test_concurrent_cancellation_recovery_issues_one_stop_and_one_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    source_lease = TaskWorkerLease.objects.create(
        worker_id="postgres-cancellation-source",
        hostname="expired-host",
        pid=1301,
        queue_name="default",
        last_heartbeat_at=now - timedelta(hours=1),
        is_active=True,
    )
    recoverers = [
        _claim_command("postgres-cancellation-recoverer-a", []),
        _claim_command("postgres-cancellation-recoverer-b", []),
    ]
    task = _execution(
        "postgres-concurrent-cancellation-recovery-001",
        state=TaskState.CANCELLING,
        claimed_by_worker=source_lease.worker_id,
        ray_job_id="raysubmit_postgres_cancellation_recovery",
        ray_address="ray://cluster:10001",
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=5),
        attempt_number=3,
        execution_generation=7,
    )
    cancellation_calls: list[int] = []
    effect_workers: list[str] = []
    effect_started = Event()
    release_effect = Event()
    authority_barrier = Barrier(2)

    for recoverer in recoverers:
        original_authority = recoverer._authoritative_task_owner

        @contextmanager
        def synchronized_authority(
            *args: object,
            _original: Callable[..., Any] = original_authority,
            **kwargs: object,
        ):
            authority_barrier.wait(timeout=10)
            with _original(*args, **kwargs) as owned:
                yield owned

        def request_stop(
            current: RayTaskExecution,
            *,
            worker_id: str = recoverer.worker_id,
        ) -> CancellationOutcome:
            cancellation_calls.append(current.pk)
            effect_workers.append(worker_id)
            effect_started.set()
            if not release_effect.wait(timeout=10):
                raise TimeoutError("test did not release cancellation recovery")
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr(recoverer, "_authoritative_task_owner", synchronized_authority)
        monkeypatch.setattr(recoverer, "_request_cancellation_for_task", request_stop)

    results = _run_contended_recovery(
        recoverers,
        lambda recoverer: recoverer.process_cancellations(),
        effect_started=effect_started,
        release_effect=release_effect,
        effect_workers=effect_workers,
    )

    task.refresh_from_db()
    source_lease.refresh_from_db()
    assert sorted(results) == [0, 1]
    assert cancellation_calls == [task.pk]
    assert task.state == TaskState.CANCELLED
    assert task.claimed_by_worker in {recoverer.worker_id for recoverer in recoverers}
    assert task.cancellation_status == CancellationStatus.REQUESTED
    assert task.finished_at is not None
    assert source_lease.is_active is False
    assert TaskAttempt.objects.filter(execution=task, attempt_number=3).count() == 1


def test_orphan_adoption_invalidates_waiting_stale_lost_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    lost_backend_pid: list[int] = []
    original_authority = adopter._authoritative_task_owner

    @contextmanager
    def hold_normal_authoritative_locks(*args: object, **kwargs: object):
        with original_authority(*args, **kwargs) as owned:
            if owned is not None:
                adoption_locked.set()
                if not release_adoption.wait(timeout=10):
                    raise TimeoutError("test did not release orphan adoption")
            yield owned

    monkeypatch.setattr(adopter, "_authoritative_task_owner", hold_normal_authoritative_locks)

    def adopt_while_holding_authoritative_locks() -> bool:
        close_old_connections()
        try:
            current = RayTaskExecution.objects.get(pk=task.pk)
            return adopter._adopt_orphaned_ray_job_task(current, now=now)
        finally:
            close_old_connections()

    def mark_stale_snapshot_lost() -> bool:
        close_old_connections()
        try:
            if not adoption_locked.wait(timeout=10):
                raise TimeoutError("test did not acquire orphan adoption lock")
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                lost_backend_pid.append(int(cursor.fetchone()[0]))
            lost_started.set()
            return mark_task_lost(stale_lost_snapshot)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        adoption_future = executor.submit(adopt_while_holding_authoritative_locks)
        assert adoption_locked.wait(timeout=10)
        lost_future = executor.submit(mark_stale_snapshot_lost)
        assert lost_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(lost_backend_pid[0])
        finally:
            release_adoption.set()
        assert adoption_future.result(timeout=20) is True
        assert lost_future.result(timeout=20) is False

    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.claimed_by_worker == "replacement-owner"
    assert task.last_heartbeat_at == stale_lost_snapshot.last_heartbeat_at
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
    identity = _allocate_workflow_identity(task)
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


def test_postgresql_lifecycle_locks_exclude_oversized_unrelated_payloads() -> None:
    unrelated_marker = "postgres-unrelated-lifecycle-" + ("x" * 131_072)
    required_result = "postgres-required-result-" + ("y" * 65_536)
    digest = "d" * 64
    input_reference = f"s3://task-inputs/django-ray/inputs/dd/dd/{digest}.json?bytes=131072"
    payload = TaskInputPayload.objects.create(
        reference=input_reference,
        backend="s3",
        digest=digest,
        size_bytes=131_072,
        envelope_version=1,
    )
    result_reference = "digest:" + ("e" * 64)
    task = _execution(
        "postgres-projected-manual-retry-001",
        state=TaskState.FAILED,
        attempt_number=3,
        execution_generation=7,
        args_json=EXTERNAL_INPUT_PLACEHOLDER,
        kwargs_json=EXTERNAL_INPUT_PLACEHOLDER,
        input_reference=input_reference,
        result_data=required_result,
        result_reference=result_reference,
        progress_data=unrelated_marker,
        workflow_plan_json=unrelated_marker,
        workflow_plan_selection=unrelated_marker,
        completion_data=unrelated_marker,
        cancellation_error=unrelated_marker,
        error_message="retryable failure",
        error_traceback="RetryError: retryable failure",
    )

    with CaptureQueriesContext(connection) as retry_queries:
        retried = retry_task(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=7,
        )

    assert retried is not None
    retry_projections = _execution_select_projections(retry_queries)
    assert [fields for fields, _sql in retry_projections] == [
        {
            "id",
            "state",
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_plan_fingerprint",
        },
        {
            "task_id",
            "started_at",
            "finished_at",
            "error_message",
            "error_traceback",
            "result_data",
            "result_reference",
            "workflow_progress_summary_json",
            "queue_timeout_seconds",
            "ray_target_address",
            "ray_job_id",
            "ray_address",
            "runtime_env_profile",
            "runtime_env_json",
            "runtime_env_hash",
        },
    ]
    assert "FOR UPDATE" in retry_projections[0][1].upper()
    forbidden_payload_columns = {
        "args_json",
        "kwargs_json",
        "input_reference",
        "progress_data",
        "workflow_plan_json",
        "workflow_plan_selection",
        "completion_data",
        "cancellation_error",
    }
    assert all(fields.isdisjoint(forbidden_payload_columns) for fields, _sql in retry_projections)
    task.refresh_from_db()
    assert task.args_json == EXTERNAL_INPUT_PLACEHOLDER
    assert task.kwargs_json == EXTERNAL_INPUT_PLACEHOLDER
    assert task.input_reference == input_reference
    assert task.result_data is None
    assert task.result_reference is None
    archived = TaskAttempt.objects.get(execution=task, attempt_number=3)
    assert archived.result_data == required_result
    assert archived.result_reference == result_reference
    assert archived.error_message == "retryable failure"
    assert archived.error_traceback == "RetryError: retryable failure"
    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE

    cancellation_result_reference = "digest:" + ("f" * 64)
    queued = _execution(
        "postgres-projected-queued-cancellation-001",
        state=TaskState.QUEUED,
        attempt_number=4,
        execution_generation=9,
        args_json=unrelated_marker,
        kwargs_json=unrelated_marker,
        result_data=required_result,
        result_reference=cancellation_result_reference,
        error_message="queued cancellation",
        error_traceback="CancellationError: queued cancellation",
        runtime_env_json=unrelated_marker,
        progress_data=unrelated_marker,
        workflow_plan_json=unrelated_marker,
        completion_data=unrelated_marker,
        cancellation_error=unrelated_marker,
    )

    with CaptureQueriesContext(connection) as queued_cancellation_queries:
        queued_cancellation = request_task_cancellation(
            queued.pk,
            expected_attempt_number=4,
            expected_execution_generation=9,
        )

    assert queued_cancellation.status is TaskCancellationRequestStatus.ACCEPTED
    queued_cancellation_projections = _execution_select_projections(queued_cancellation_queries)
    assert [fields for fields, _sql in queued_cancellation_projections] == [
        {
            "id",
            "state",
            "attempt_number",
            "execution_generation",
        },
        {
            "started_at",
            "finished_at",
            "error_message",
            "error_traceback",
            "result_data",
            "result_reference",
            "workflow_progress_summary_json",
            "workflow_run_id",
        },
    ]
    assert "FOR UPDATE" in queued_cancellation_projections[0][1].upper()
    assert all(
        fields.isdisjoint(forbidden_payload_columns)
        for fields, _sql in queued_cancellation_projections
    )
    queued_attempt = TaskAttempt.objects.get(execution=queued, attempt_number=4)
    assert queued_attempt.result_data == required_result
    assert queued_attempt.result_reference == cancellation_result_reference
    assert queued_attempt.error_message == "queued cancellation"
    assert queued_attempt.error_traceback == "CancellationError: queued cancellation"

    completion_marker = "postgres-unrelated-completion-" + ("z" * 131_072)
    running = _execution(
        "postgres-projected-cancellation-001",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
        args_json=unrelated_marker,
        kwargs_json=unrelated_marker,
        result_data=unrelated_marker,
        error_traceback=unrelated_marker,
        completion_data=completion_marker,
    )

    with CaptureQueriesContext(connection) as cancellation_queries:
        cancellation = request_task_cancellation(running.pk)

    assert cancellation.status is TaskCancellationRequestStatus.COMPLETION_PENDING
    cancellation_projections = _execution_select_projections(cancellation_queries)
    assert [fields for fields, _sql in cancellation_projections] == [
        {
            "id",
            "state",
            "attempt_number",
            "execution_generation",
        },
        set(),
    ]
    assert "FOR UPDATE" in cancellation_projections[0][1].upper()
    assert all(
        fields.isdisjoint(forbidden_payload_columns) for fields, _sql in cancellation_projections
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

    owner_lease = TaskWorkerLease.objects.create(
        worker_id="postgres-unknown-worker",
        hostname="unknown-worker-host",
        pid=1401,
        queue_name="default",
        capability_schema_version=1,
        django_ray_version="test",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
        last_heartbeat_at=datetime.now(UTC),
    )
    lease_identity = WorkerLeaseIdentity(
        worker_id=str(owner_lease.worker_id),
        hostname=owner_lease.hostname,
        pid=owner_lease.pid,
        started_at=owner_lease.started_at,
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
            command.lease_identity = lease_identity
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
    identity = _allocate_workflow_identity(task)
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
    identity = _allocate_workflow_identity(task)
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
    identity = _allocate_workflow_identity(task)
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
    identity = _allocate_workflow_identity(task)
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
    identity = _allocate_workflow_identity(task)
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


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
def test_postgresql_sensitive_admin_bounds_unicode_before_transfer(
    client,
    monkeypatch,
) -> None:
    limit = django_ray_admin.ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES
    exact_value = "\U0001f642" * (limit // 4)
    assert len(exact_value.encode("utf-8")) == limit
    task = _execution(
        "postgres-sensitive-admin-byte-boundary-001",
        state=TaskState.FAILED,
        error_traceback=exact_value,
    )
    user = get_user_model().objects.create_superuser(
        username="postgres-sensitive-admin-byte-boundary",
    )
    client.force_login(user)
    sensitive_url = reverse(
        "admin:django_ray_raytaskexecution_sensitive_data",
        args=[task.pk],
    )

    exact_response = client.get(sensitive_url)

    assert exact_response.status_code == 200
    assert exact_value in exact_response.content.decode("utf-8")

    captured_rows: list[dict[str, Any]] = []
    original_sections = django_ray_admin._sensitive_sections

    def capture_sensitive_sections(
        row: dict[str, Any],
        section_specs: Any,
    ) -> list[dict[str, Any]]:
        captured_rows.append(dict(row))
        return original_sections(row, section_specs)

    monkeypatch.setattr(
        django_ray_admin,
        "_sensitive_sections",
        capture_sensitive_sections,
    )
    oversized_value = exact_value + "\u00e9"
    RayTaskExecution.objects.filter(pk=task.pk).update(
        error_traceback=oversized_value,
    )

    with CaptureQueriesContext(connection) as queries:
        oversized_response = client.get(sensitive_url)

    assert oversized_response.status_code == 200
    oversized_content = oversized_response.content.decode("utf-8")
    assert exact_value not in oversized_content
    assert oversized_value not in oversized_content
    assert f"{limit + 2} bytes" in oversized_content
    assert captured_rows
    assert captured_rows[-1]["admin_sensitive_error_traceback_bytes"] == limit + 2
    assert captured_rows[-1]["admin_sensitive_error_traceback_value"] is None

    bounded_queries = [
        query["sql"]
        for query in queries.captured_queries
        if "admin_sensitive_error_traceback_value" in query["sql"]
    ]
    assert len(bounded_queries) == 1
    bounded_sql = bounded_queries[0].upper()
    assert "OCTET_LENGTH" in bounded_sql
    assert "CASE WHEN" in bounded_sql
    assert "LENGTH(CAST(" not in bounded_sql
    assert "AS BLOB" not in bounded_sql


def test_postgresql_ordinary_admin_details_bound_unicode_before_transfer() -> None:
    limit = django_ray_admin.ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES
    exact_value = "\U0001f642" * (limit // 4)
    oversized_value = "\u00e9" * ((limit // 2) + 1)
    assert len(exact_value.encode("utf-8")) == limit
    assert len(oversized_value.encode("utf-8")) == limit + 2
    execution = _execution(
        "postgres-ordinary-admin-byte-boundary-001",
        state=TaskState.FAILED,
        result_data=exact_value,
        error_message=oversized_value,
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        result_data=exact_value,
        error_message=oversized_value,
    )
    user = get_user_model().objects.create_superuser(
        username="postgres-ordinary-admin-byte-boundary",
    )
    execution_url = reverse(
        "admin:django_ray_raytaskexecution_change",
        args=[execution.pk],
    )
    execution_request = RequestFactory().get(execution_url)
    execution_request.user = user
    execution_request.resolver_match = resolve(execution_url)
    attempt_url = reverse(
        "admin:django_ray_taskattempt_change",
        args=[attempt.pk],
    )
    attempt_request = RequestFactory().get(attempt_url)
    attempt_request.user = user
    attempt_request.resolver_match = resolve(attempt_url)

    with CaptureQueriesContext(connection) as execution_queries:
        loaded_execution = (
            django_ray_admin.RayTaskExecutionAdmin(
                RayTaskExecution,
                admin.site,
            )
            .get_queryset(execution_request)
            .get(pk=execution.pk)
        )
    with CaptureQueriesContext(connection) as attempt_queries:
        loaded_attempt = (
            django_ray_admin.TaskAttemptAdmin(
                TaskAttempt,
                admin.site,
            )
            .get_queryset(attempt_request)
            .get(pk=attempt.pk)
        )
    inline_request = RequestFactory().get("/admin/")
    inline_request.user = user
    with CaptureQueriesContext(connection) as inline_queries:
        inline_attempt = (
            django_ray_admin.TaskAttemptInline(
                TaskAttempt,
                admin.site,
            )
            .get_queryset(inline_request)
            .get(pk=attempt.pk)
        )

    for loaded in (loaded_execution, loaded_attempt):
        assert django_ray_admin._bounded_admin_text_value(loaded, "result_data") == (
            exact_value,
            "value",
        )
        assert django_ray_admin._bounded_admin_text_value(loaded, "error_message") == (
            None,
            "oversized",
        )
        assert {"result_data", "error_message"}.issubset(loaded.get_deferred_fields())
    assert django_ray_admin._bounded_admin_text_value(
        inline_attempt,
        "error_message",
        max_bytes=django_ray_admin.ADMIN_ATTEMPT_INLINE_MAX_BYTES,
        max_chars=django_ray_admin.ADMIN_ATTEMPT_INLINE_MAX_CHARS,
        namespace="inline",
    ) == (None, "oversized")
    assert inline_attempt.__dict__["admin_inline_total"] == 1
    bounded_queries = [
        query["sql"]
        for captured in (execution_queries, attempt_queries, inline_queries)
        for query in captured.captured_queries
        if "admin_detail_error_message_value" in query["sql"]
        or "admin_inline_error_message_value" in query["sql"]
    ]
    assert len(bounded_queries) == 3
    for query in bounded_queries:
        bounded_sql = query.upper()
        assert "OCTET_LENGTH" in bounded_sql
        assert "CASE WHEN" in bounded_sql
        assert "LENGTH(CAST(" not in bounded_sql
        assert "AS BLOB" not in bounded_sql


def test_postgresql_execution_list_bounds_unicode_before_transfer() -> None:
    limit = testproject_api._EXECUTION_LIST_DIAGNOSTIC_MAX_BYTES
    exact_value = "\U0001f642" * (limit // 4)
    oversized_value = exact_value + "\u00e9"
    assert len(exact_value.encode("utf-8")) == limit
    assert len(oversized_value.encode("utf-8")) == limit + 2
    task = _execution(
        "postgres-execution-list-byte-boundary-001",
        state=TaskState.FAILED,
        result_data=oversized_value,
        error_message=oversized_value,
    )

    with CaptureQueriesContext(connection) as queries:
        rows = testproject_api._bounded_execution_list_rows(
            RayTaskExecution.objects.filter(pk=task.pk),
            limit=1,
        )

    assert len(rows) == 1
    row = rows[0]
    assert row["_list_result_data_bytes"] == limit + 2
    assert row["_list_error_message_bytes"] == limit + 2
    assert row["_list_result_data"] is None
    assert row["_list_error_message"] is None
    item = testproject_api._execution_list_item(row)
    assert item.result_data is None
    assert item.error_message is None
    assert item.result_data_omission_reason == "stored_value_exceeds_list_limit"
    assert item.error_message_omission_reason == "stored_value_exceeds_list_limit"

    selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
        and "django_ray_raytaskexecution" in query["sql"]
    ]
    assert len(selects) == 1
    bounded_sql = selects[0].upper()
    assert "OCTET_LENGTH" in bounded_sql
    assert "CASE WHEN" in bounded_sql
    assert "LENGTH(CAST(" not in bounded_sql
    assert "AS BLOB" not in bounded_sql


def test_postgresql_execution_detail_bounds_unicode_before_transfer() -> None:
    limit = testproject_api._EXECUTION_DETAIL_DIAGNOSTIC_MAX_BYTES
    exact_value = "\U0001f642" * (limit // 4)
    oversized_value = exact_value + "\u00e9"
    assert len(exact_value.encode("utf-8")) == limit
    assert len(oversized_value.encode("utf-8")) == limit + 2
    exact = _execution(
        "postgres-execution-detail-exact-boundary-001",
        state=TaskState.FAILED,
        result_data=exact_value,
        error_message=exact_value,
    )
    oversized = _execution(
        "postgres-execution-detail-over-boundary-001",
        state=TaskState.FAILED,
        result_data=oversized_value,
        error_message=oversized_value,
    )

    exact_row = testproject_api._bounded_execution_detail_row(exact.pk)
    exact_item = testproject_api._execution_detail_item(exact_row)

    assert exact_row["_detail_result_data_bytes"] == limit
    assert exact_row["_detail_error_message_bytes"] == limit
    assert exact_row["_detail_result_data"] == exact_value
    assert exact_row["_detail_error_message"] == exact_value
    assert exact_item.result_data_omission_reason is None
    assert exact_item.error_message_omission_reason is None

    with CaptureQueriesContext(connection) as queries:
        oversized_row = testproject_api._bounded_execution_detail_row(oversized.pk)

    assert oversized_row["_detail_result_data_bytes"] == limit + 2
    assert oversized_row["_detail_error_message_bytes"] == limit + 2
    assert oversized_row["_detail_result_data"] is None
    assert oversized_row["_detail_error_message"] is None
    oversized_item = testproject_api._execution_detail_item(oversized_row)
    assert oversized_item.result_data_omission_reason == "stored_value_exceeds_detail_limit"
    assert oversized_item.error_message_omission_reason == "stored_value_exceeds_detail_limit"

    selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
        and "django_ray_raytaskexecution" in query["sql"]
    ]
    assert len(selects) == 1
    bounded_sql = selects[0].upper()
    assert "OCTET_LENGTH" in bounded_sql
    assert "CASE WHEN" in bounded_sql
    assert "LENGTH(CAST(" not in bounded_sql
    assert "AS BLOB" not in bounded_sql


def test_workflow_progress_cancellation_race_disables_late_writer() -> None:
    task = _execution(
        "postgres-workflow-cancel-race-001",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
        started_at=datetime.now(UTC),
    )
    identity = _allocate_workflow_identity(task)
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
    identity = _allocate_workflow_identity(task)
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
    identity = _allocate_workflow_identity(task)
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
    old = _allocate_workflow_identity(task)
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
        lambda: _allocate_workflow_identity(task),
        read_progress_pair,
    )

    replacement = results[0]
    assert isinstance(replacement, WorkflowRunIdentity)
    reader_result = results[1]
    assert isinstance(reader_result, tuple)
    observed_run_id, snapshot_run_id = reader_result
    assert snapshot_run_id is None or observed_run_id == snapshot_run_id
    task.refresh_from_db()
    assert str(task.workflow_run_id) == replacement.run_id
    assert task.progress_data is None
    assert persist_workflow_progress(old, _workflow_snapshot(old, 2)) is False


def test_concurrent_fresh_allocations_serialize_sequence_reservations() -> None:
    task = _execution(
        "postgres-workflow-storage-replacement-race-001",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    old = _allocate_workflow_identity(task)
    unrelated_run_id = "00000000-0000-0000-0000-000000000134"
    for run_id, attempt_number, execution_generation in (
        (old.run_id, 2, 5),
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
        lambda: _allocate_workflow_identity(task),
        lambda: _allocate_workflow_identity(task),
    )

    assert all(isinstance(result, WorkflowRunIdentity) for result in results)
    replacement_ids = {
        result.run_id for result in results if isinstance(result, WorkflowRunIdentity)
    }
    assert len(replacement_ids) == 2
    task.refresh_from_db()
    final_run_id = str(task.workflow_run_id)
    assert final_run_id in replacement_ids
    assert task.workflow_run_sequence == 3
    current_run_ids = {
        str(run_id)
        for run_id in WorkflowProgressRunStorage.objects.filter(
            execution=task,
            attempt_number=2,
            execution_generation=5,
        ).values_list("run_id", flat=True)
    }
    assert current_run_ids == {unrelated_run_id}
    assert WorkflowProgressRunStorage.objects.filter(
        execution=task,
        attempt_number=1,
        execution_generation=4,
        run_id=old.run_id,
    ).exists()


def test_workflow_namespace_collision_retries_under_postgresql_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collided_namespace = 0x1317A23B01FD4215
    replacement_namespace = 0x2317A23B01FD4215
    first_task = _execution(
        "postgres-workflow-namespace-collision-001",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    second_task = _execution(
        "postgres-workflow-namespace-collision-002",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    candidate_barrier = Barrier(2)
    candidate_guard = Lock()
    candidate_calls = 0

    def collide_then_replace(_bits: int) -> int:
        nonlocal candidate_calls
        with candidate_guard:
            candidate_calls += 1
            allocation_call = candidate_calls
        if allocation_call <= 2:
            candidate_barrier.wait(timeout=10)
            return collided_namespace
        return replacement_namespace

    monkeypatch.setattr(workflow_progress_module, "randbits", collide_then_replace)

    results = _run_concurrently(
        lambda: _allocate_workflow_identity(first_task),
        lambda: _allocate_workflow_identity(second_task),
    )

    first_task.refresh_from_db()
    second_task.refresh_from_db()
    assert all(isinstance(result, WorkflowRunIdentity) for result in results)
    run_ids = {result.run_id for result in results if isinstance(result, WorkflowRunIdentity)}
    assert len(run_ids) == 2
    assert candidate_calls == 3
    assert {
        first_task.workflow_run_namespace,
        second_task.workflow_run_namespace,
    } == {collided_namespace, replacement_namespace}


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
