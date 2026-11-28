"""PostgreSQL-only tests for contested worker leasing, deletion, and completion races."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.management import CommandError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_ray import __version__ as django_ray_version
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    request_task_cancellation,
)
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    finalize_cancellation,
)
from django_ray.runner.leasing import get_active_workers
from django_ray.runner.ray_core import RayCoreHandle
from django_ray.runner.reconciliation import mark_task_lost
from tests.integration._postgresql_coordination_helpers import (
    _claim_command,
    _execution,
    _ray_core_runner_with_handle,
    _run_concurrently,
    _run_contended_recovery,
    _synchronize_first_predicate_call,
    _wait_for_postgresql_lock,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    """Keep the default SQLite suite fast while making this gate explicit."""
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def test_projected_attempt_archive_preserves_one_contested_cancellation_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = RayTaskExecution.objects.create(
        task_id="projected-attempt-cancellation-contention",
        callable_path="synthetic.task",
        state=TaskState.CANCELLING,
        attempt_number=2,
        execution_generation=4,
        error_traceback="current protected evidence",
    )
    original_created = datetime(2026, 1, 1, tzinfo=UTC)
    TaskAttempt.objects.create(
        execution=task,
        attempt_number=2,
        state=TaskState.FAILED,
        created_at=original_created,
        error_traceback="old-protected-" + "x" * 65536,
        workflow_progress_summary_json="retained summary",
    )
    original_from_db = TaskAttempt.from_db
    projections: list[set[str]] = []

    def guarded_from_db(cls, database, field_names, values, **kwargs):
        selected = set(field_names)
        assert selected == {"id", "created_at"}
        projections.append(selected)
        return original_from_db(database, field_names, values, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(TaskAttempt, "from_db", classmethod(guarded_from_db))
        outcomes = _run_concurrently(
            lambda: finalize_cancellation(
                task, expected_attempt_number=2, expected_execution_generation=4
            ),
            lambda: finalize_cancellation(
                task, expected_attempt_number=2, expected_execution_generation=4
            ),
        )

    assert sorted(outcomes) == [False, True]
    assert projections == [{"id", "created_at"}]
    archive = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert archive.state == TaskState.CANCELLED
    assert archive.error_traceback == "current protected evidence"
    assert archive.created_at == original_created
    assert archive.workflow_progress_summary_json == "retained summary"
    assert TaskAttempt.objects.filter(execution=task, attempt_number=2).count() == 1


def test_incompatible_candidate_does_not_wait_for_locked_source_lease() -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    now = datetime.now(UTC)
    source = TaskWorkerLease.objects.create(
        worker_id="postgres-z-v2-locked-source",
        hostname="v2-source-host",
        pid=1099,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=2,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        last_heartbeat_at=now - timedelta(hours=1),
    )
    task = _execution(
        "postgres-incompatible-source-lock-001",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=source.worker_id,
        started_at=now - timedelta(hours=1),
        last_heartbeat_at=now - timedelta(hours=1),
        managed_with_django_ray_version="0.4.0-manager",
    )
    candidate = _claim_command("postgres-a-v1-candidate", [])
    source_locked = Event()
    release_source = Event()

    def hold_source_lease() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                TaskWorkerLease.objects.select_for_update().get(worker_id=source.worker_id)
                source_locked.set()
                if not release_source.wait(timeout=10):
                    raise TimeoutError("test did not release source lease")
        finally:
            close_old_connections()

    def attempt_incompatible_takeover() -> RayTaskExecution | None:
        close_old_connections()
        try:
            return candidate._take_over_task_if_owner_stale(task, now=datetime.now(UTC))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_source_lease)
        assert source_locked.wait(timeout=10)
        takeover = executor.submit(attempt_incompatible_takeover)
        try:
            assert takeover.result(timeout=5) is None
        finally:
            release_source.set()
        holder.result(timeout=10)

    source.refresh_from_db()
    task.refresh_from_db()
    assert source.is_active is True
    assert source.stopped_at is None
    assert task.state == TaskState.RUNNING
    assert task.claimed_by_worker == source.worker_id
    assert candidate.shutdown_requested is False


def test_nested_compatible_takeover_preserves_global_lease_lock_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    source = TaskWorkerLease.objects.create(
        worker_id="postgres-a-v1-lock-source",
        hostname="v1-source-host",
        pid=1100,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
        last_heartbeat_at=now - timedelta(hours=1),
    )
    task = _execution(
        "postgres-compatible-lock-order-001",
        execution_protocol_version=1,
        state=TaskState.RUNNING,
        claimed_by_worker=source.worker_id,
        started_at=now - timedelta(hours=1),
        last_heartbeat_at=now - timedelta(hours=1),
        managed_with_django_ray_version="0.4.0-manager",
    )
    candidate = _claim_command("postgres-z-v1-lock-candidate", [])
    source_locked = Event()
    preflight_read = Event()
    request_candidate_lock = Event()
    candidate_locked = Event()
    release_locks = Event()

    original_preflight = candidate._preliminary_takeover_protocol_range

    def observe_preflight():
        supported = original_preflight()
        preflight_read.set()
        return supported

    monkeypatch.setattr(candidate, "_preliminary_takeover_protocol_range", observe_preflight)

    def hold_source_then_candidate() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                TaskWorkerLease.objects.select_for_update().get(worker_id=source.worker_id)
                source_locked.set()
                if not request_candidate_lock.wait(timeout=10):
                    raise TimeoutError("test did not request the candidate lock")
                TaskWorkerLease.objects.select_for_update().get(worker_id=candidate.worker_id)
                candidate_locked.set()
                if not release_locks.wait(timeout=10):
                    raise TimeoutError("test did not release the ordered lease locks")
        finally:
            close_old_connections()

    def attempt_compatible_takeover() -> RayTaskExecution | None:
        close_old_connections()
        try:
            # The caller-owned transaction would retain a locking preflight and
            # invert source-before-candidate order. The advisory preflight must
            # remain nonlocking until the final sorted acquisition.
            with transaction.atomic():
                return candidate._take_over_task_if_owner_stale(task, now=datetime.now(UTC))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_source_then_candidate)
        assert source_locked.wait(timeout=10)
        takeover = executor.submit(attempt_compatible_takeover)
        assert preflight_read.wait(timeout=10)
        request_candidate_lock.set()
        try:
            assert candidate_locked.wait(timeout=5)
        finally:
            release_locks.set()
        holder.result(timeout=10)
        adopted = takeover.result(timeout=10)

    assert adopted is not None
    task.refresh_from_db()
    source.refresh_from_db()
    assert task.claimed_by_worker == candidate.worker_id
    assert task.managed_with_django_ray_version == django_ray_version
    assert source.is_active is False


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

    ownership = {
        task_pk: (owner, manager)
        for task_pk, owner, manager in RayTaskExecution.objects.values_list(
            "pk",
            "claimed_by_worker",
            "managed_with_django_ray_version",
        )
    }
    assert {ownership[task_id] for task_id in claimed_a} == {
        ("postgres-worker-a", django_ray_version)
    }
    assert {ownership[task_id] for task_id in claimed_b} == {
        ("postgres-worker-b", django_ray_version)
    }


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

    ordering = {
        cast(int, task.pk): (
            -int(task.priority),
            cast(datetime, task.created_at),
            cast(int, task.pk),
        )
        for task in all_tasks
    }
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


def test_ray_core_cancellation_committed_after_ready_result_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _claim_command("postgres-ray-core-cancellation-owner", [])
    command.last_task_monitor_heartbeat = time.monotonic()
    command.task_monitor_heartbeat_interval = 3600
    observed_at = datetime.now(UTC)
    task = _execution(
        "postgres-ray-core-cancellation-after-ready-001",
        state=TaskState.RUNNING,
        claimed_by_worker=command.worker_id,
        attempt_number=2,
        execution_generation=5,
        started_at=observed_at,
        last_heartbeat_at=observed_at,
    )
    object_ref = object()
    handle = RayCoreHandle(
        task_pk=int(task.pk),
        object_ref=object_ref,
        submitted_at=observed_at,
        task_name="postgres cancellation race",
        attempt_number=2,
        execution_generation=5,
    )
    runner = _ray_core_runner_with_handle(handle)
    command.ray_core_runner = runner

    cancellation_locked = Event()
    release_cancellation = Event()
    result_returned = Event()
    poll_connected = Event()
    poll_backend_pid: list[int] = []

    def ready_wait(refs, *, num_returns: int, timeout: int):
        assert refs == [object_ref]
        assert num_returns == 1
        assert timeout == 0
        return [object_ref], []

    def ready_get(ref):
        assert ref is object_ref
        result_returned.set()
        return '{"success": true, "result": 3}'

    monkeypatch.setattr("ray.is_initialized", lambda: True)
    monkeypatch.setattr("ray.wait", ready_wait)
    monkeypatch.setattr("ray.get", ready_get)
    monkeypatch.setattr(
        command,
        "_store_task_result",
        lambda *_args, **_kwargs: pytest.fail(
            "a committed cancellation must prevent Ray Core result storage"
        ),
    )

    def hold_accepted_cancellation() -> object:
        close_old_connections()
        try:
            with transaction.atomic():
                cancellation = request_task_cancellation(
                    task.pk,
                    expected_attempt_number=2,
                    expected_execution_generation=5,
                )
                assert cancellation.status is TaskCancellationRequestStatus.ACCEPTED
                cancellation_locked.set()
                if not release_cancellation.wait(timeout=10):
                    raise TimeoutError("test did not release the cancellation transaction")
                return cancellation
        finally:
            close_old_connections()

    def poll_ready_result() -> int:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                poll_backend_pid.append(int(cursor.fetchone()[0]))
            poll_connected.set()
            return command.poll_ray_core_tasks()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancellation_future = executor.submit(hold_accepted_cancellation)
        assert cancellation_locked.wait(timeout=10)
        poll_future = executor.submit(poll_ready_result)
        try:
            assert poll_connected.wait(timeout=10)
            assert result_returned.wait(timeout=10)
            _wait_for_postgresql_lock(poll_backend_pid[0])
        finally:
            release_cancellation.set()
        cancellation = cancellation_future.result(timeout=20)
        assert poll_future.result(timeout=20) == 1

    assert cancellation.status is TaskCancellationRequestStatus.ACCEPTED
    task.refresh_from_db()
    assert task.state == TaskState.CANCELLED
    assert task.result_data is None
    assert task.result_reference is None
    assert task.finished_at is not None
    attempt = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert attempt.state == TaskState.CANCELLED
    assert attempt.result_data is None
    assert attempt.result_reference is None
    assert runner.pending_count == 0


@pytest.mark.parametrize(
    "result_json",
    [
        '{"success": true, "result": 3}',
        (
            '{"success": false, "result": null, "error": "boom", '
            '"traceback": "tb", "exception_type": "RuntimeError", "retryable": true}'
        ),
    ],
    ids=("success", "failure"),
)
def test_ray_core_exact_lease_loss_after_get_is_a_durable_noop(
    monkeypatch: pytest.MonkeyPatch,
    result_json: str,
) -> None:
    command = _claim_command("postgres-ray-core-post-get-lease-owner", [])
    command.last_task_monitor_heartbeat = time.monotonic()
    command.task_monitor_heartbeat_interval = 3600
    observed_at = datetime.now(UTC)
    task = _execution(
        f"postgres-ray-core-post-get-lease-loss-{json.loads(result_json)['success']}",
        state=TaskState.RUNNING,
        claimed_by_worker=command.worker_id,
        attempt_number=3,
        execution_generation=7,
        started_at=observed_at,
        last_heartbeat_at=observed_at,
    )
    task_before = RayTaskExecution.objects.values().get(pk=task.pk)
    object_ref = object()
    handle = RayCoreHandle(
        task_pk=int(task.pk),
        object_ref=object_ref,
        submitted_at=observed_at,
        task_name="postgres post-get lease loss",
        attempt_number=3,
        execution_generation=7,
    )
    runner = _ray_core_runner_with_handle(handle)
    command.ray_core_runner = runner
    get_entered = Event()
    release_get = Event()

    def ready_wait(refs, *, num_returns: int, timeout: int):
        assert refs == [object_ref]
        assert num_returns == 1
        assert timeout == 0
        return [object_ref], []

    def blocking_get(ref):
        assert ref is object_ref
        get_entered.set()
        if not release_get.wait(timeout=10):
            raise TimeoutError("test did not release Ray result retrieval")
        return result_json

    def forbidden_lifecycle_effect(*_args, **_kwargs):
        pytest.fail("post-get lease loss must fence every durable lifecycle effect")

    monkeypatch.setattr("ray.is_initialized", lambda: True)
    monkeypatch.setattr("ray.wait", ready_wait)
    monkeypatch.setattr("ray.get", blocking_get)
    monkeypatch.setattr(command, "_store_task_result", forbidden_lifecycle_effect)
    monkeypatch.setattr(command, "_handle_task_failure", forbidden_lifecycle_effect)

    def poll_ready_result() -> int:
        close_old_connections()
        try:
            return command.poll_ray_core_tasks()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        poll_future = executor.submit(poll_ready_result)
        try:
            assert get_entered.wait(timeout=10)
            assert command.lease_identity is not None
            TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).delete()
            replacement = TaskWorkerLease.objects.create(
                worker_id=command.worker_id,
                hostname="postgres-ray-core-replacement-host",
                pid=9876,
                queue_name="default",
            )
        finally:
            release_get.set()
        assert poll_future.result(timeout=20) == 1

    assert RayTaskExecution.objects.values().get(pk=task.pk) == task_before
    assert not TaskAttempt.objects.filter(execution=task).exists()
    replacement.refresh_from_db()
    assert replacement.is_active is True
    assert replacement.hostname == "postgres-ray-core-replacement-host"
    assert command.shutdown_requested is True
    assert command.lease_ownership_lost is True
    assert runner.pending_count == 0


def test_enriched_ray_core_completion_blocked_by_task_replacement_is_a_durable_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.execution_codec import (
        ExecutionCompletion,
        ExecutionCompletionSource,
        ExecutionIdentity,
        decode_execution_completion,
        encode_execution_completion,
    )

    command = _claim_command("postgres-enriched-completion-owner", [])
    command.last_task_monitor_heartbeat = time.monotonic()
    command.task_monitor_heartbeat_interval = 3600
    observed_at = datetime.now(UTC)
    task = _execution(
        "postgres-enriched-completion-replacement-001",
        state=TaskState.RUNNING,
        claimed_by_worker=command.worker_id,
        attempt_number=3,
        execution_generation=7,
        started_at=observed_at,
        last_heartbeat_at=observed_at,
    )
    assert task.pk is not None
    completion_identity = ExecutionIdentity(
        task_execution_pk=int(task.pk),
        task_id=str(task.task_id),
        attempt_number=3,
        execution_generation=7,
    )
    result_json = encode_execution_completion(
        ExecutionCompletion(
            identity=completion_identity,
            execution_protocol_version=int(task.execution_protocol_version),
            executor_django_ray_version="0.5.0-stale-executor",
            success=True,
            result={"stale": "x" * 128},
            result_reference=None,
            error=None,
            traceback=None,
            exception_type=None,
            retryable=None,
        )
    )
    object_ref = object()
    handle = RayCoreHandle(
        task_pk=int(task.pk),
        object_ref=object_ref,
        submitted_at=observed_at,
        task_name="postgres enriched completion replacement",
        attempt_number=3,
        execution_generation=7,
    )
    runner = _ray_core_runner_with_handle(handle)
    command.ray_core_runner = runner

    completion_ready = Event()
    central_authority_entered = Event()
    release_central_authority = Event()
    replacement_locked = Event()
    release_replacement = Event()
    poll_connected = Event()
    poll_backend_pid: list[int] = []
    original_authority = command._authoritative_task_owner

    def ready_wait(refs, *, num_returns: int, timeout: int):
        assert refs == [object_ref]
        assert num_returns == 1
        assert timeout == 0
        return [object_ref], []

    def ready_get(ref):
        assert ref is object_ref
        decoded = decode_execution_completion(
            result_json,
            expected_identity=completion_identity,
            expected_execution_protocol_version=int(task.execution_protocol_version),
        )
        assert decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
        completion_ready.set()
        return result_json

    @contextmanager
    def delayed_central_authority(snapshot, **kwargs):
        central_authority_entered.set()
        if not release_central_authority.wait(timeout=10):
            raise TimeoutError("test did not release central completion authority")
        with original_authority(snapshot, **kwargs) as owned:
            yield owned

    monkeypatch.setattr("ray.is_initialized", lambda: True)
    monkeypatch.setattr("ray.wait", ready_wait)
    monkeypatch.setattr("ray.get", ready_get)
    monkeypatch.setattr(command, "_authoritative_task_owner", delayed_central_authority)
    monkeypatch.setattr(
        command,
        "_store_task_result",
        lambda *_args, **_kwargs: pytest.fail(
            "a stale enriched completion must not reach result storage"
        ),
    )

    def replace_task_identity() -> dict[str, Any]:
        close_old_connections()
        try:
            assert command.lease_identity is not None
            with transaction.atomic():
                TaskWorkerLease.objects.select_for_update().get(
                    **command.lease_identity.database_filters()
                )
                current = RayTaskExecution.objects.select_for_update().get(pk=task.pk)
                current.attempt_number = 4
                current.execution_generation = 8
                current.started_at = observed_at + timedelta(seconds=1)
                current.last_heartbeat_at = observed_at + timedelta(seconds=1)
                current.managed_with_django_ray_version = "0.6.0-replacement-manager"
                current.executor_django_ray_version = "0.6.0-replacement-executor"
                current.save(
                    update_fields=[
                        "attempt_number",
                        "execution_generation",
                        "started_at",
                        "last_heartbeat_at",
                        "managed_with_django_ray_version",
                        "executor_django_ray_version",
                    ]
                )
                replacement = RayTaskExecution.objects.values().get(pk=task.pk)
                replacement_locked.set()
                if not release_replacement.wait(timeout=10):
                    raise TimeoutError("test did not release the replacement transaction")
                return replacement
        finally:
            close_old_connections()

    def poll_ready_result() -> int:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                poll_backend_pid.append(int(cursor.fetchone()[0]))
            poll_connected.set()
            return command.poll_ray_core_tasks()
        finally:
            close_old_connections()

    replacement_future = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        poll_future = executor.submit(poll_ready_result)
        try:
            assert poll_connected.wait(timeout=10)
            assert completion_ready.wait(timeout=10)
            assert central_authority_entered.wait(timeout=10)
            replacement_future = executor.submit(replace_task_identity)
            assert replacement_locked.wait(timeout=10)
            release_central_authority.set()
            _wait_for_postgresql_lock(poll_backend_pid[0])
        finally:
            release_central_authority.set()
            release_replacement.set()

        assert replacement_future is not None
        replacement = replacement_future.result(timeout=20)
        assert poll_future.result(timeout=20) == 1

    assert RayTaskExecution.objects.values().get(pk=task.pk) == replacement
    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.attempt_number == 4
    assert task.execution_generation == 8
    assert task.result_data is None
    assert task.result_reference is None
    assert task.executor_django_ray_version == "0.6.0-replacement-executor"
    assert not TaskAttempt.objects.filter(execution=task).exists()
    assert runner.pending_count == 0


def test_strict_ray_job_completion_blocked_by_task_replacement_is_a_durable_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.execution_codec import (
        ExecutionCompletion,
        ExecutionIdentity,
        ExecutionRequest,
        encode_execution_completion,
        encode_execution_request,
    )
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_SUBMISSION_ID_PREFIX,
        build_ray_job_request_metadata,
    )
    from django_ray.runner.base import JobInfo, JobStatus

    command = _claim_command("postgres-strict-ray-job-completion-owner", [])
    replacement_command = _claim_command("postgres-strict-ray-job-replacement-owner", [])
    observed_at = datetime.now(UTC)
    ray_job_id = f"{STRICT_RAY_JOB_SUBMISSION_ID_PREFIX}{'a' * 64}"
    replacement_ray_job_id = f"{STRICT_RAY_JOB_SUBMISSION_ID_PREFIX}{'b' * 64}"
    task = _execution(
        "postgres-strict-ray-job-completion-replacement-001",
        state=TaskState.RUNNING,
        claimed_by_worker=command.worker_id,
        attempt_number=3,
        execution_generation=7,
        started_at=observed_at,
        last_heartbeat_at=observed_at,
        ray_job_id=ray_job_id,
        ray_address="http://ray-dashboard:8265",
        managed_with_django_ray_version="0.5.0-stale-manager",
    )
    assert task.pk is not None
    completion_identity = ExecutionIdentity(
        task_execution_pk=int(task.pk),
        task_id=str(task.task_id),
        attempt_number=3,
        execution_generation=7,
    )
    completion_data = encode_execution_completion(
        ExecutionCompletion(
            identity=completion_identity,
            execution_protocol_version=int(task.execution_protocol_version),
            executor_django_ray_version="0.5.0-stale-executor",
            success=True,
            result={"stale": "strict-ray-job-result"},
            result_reference=None,
            error=None,
            traceback=None,
            exception_type=None,
            retryable=None,
        )
    )
    request = ExecutionRequest(
        identity=completion_identity,
        execution_protocol_version=int(task.execution_protocol_version),
        callable_path=task.callable_path,
        transport_version=1,
        serialized_args=task.args_json,
        serialized_kwargs=task.kwargs_json,
        input_reference=None,
        runtime_env_profile=None,
        runtime_env_hash="0" * 64,
        runtime_env_plan_identity={},
        compiled_graph_submission_transport="ray-job",
    )
    serialized_request = encode_execution_request(request)
    metadata = build_ray_job_request_metadata(request, serialized_request)

    completion_published = Event()
    central_authority_entered = Event()
    release_central_authority = Event()
    replacement_locked = Event()
    release_replacement = Event()
    reconcile_connected = Event()
    reconcile_backend_pid: list[int] = []
    original_authority = command._authoritative_task_owner

    class PublishingRunner:
        def get_status(self, _handle) -> JobInfo:
            updated = RayTaskExecution.objects.filter(
                pk=task.pk,
                state=TaskState.RUNNING,
                claimed_by_worker=command.worker_id,
                attempt_number=3,
                execution_generation=7,
                ray_job_id=ray_job_id,
            ).update(completion_data=completion_data)
            assert updated == 1
            completion_published.set()
            return JobInfo(
                job_id=ray_job_id,
                status=JobStatus.RUNNING,
                metadata=metadata,
            )

        def cancel_with_status(self, _handle) -> CancellationOutcome:
            pytest.fail("an accepted completion race must not stop the replacement Ray Job")

        def get_logs(self, _handle) -> str | None:
            pytest.fail("an accepted completion race must not retrieve Ray Job logs")

    @contextmanager
    def delayed_central_authority(snapshot, **kwargs):
        central_authority_entered.set()
        if not release_central_authority.wait(timeout=10):
            raise TimeoutError("test did not release strict completion authority")
        with original_authority(snapshot, **kwargs) as owned:
            yield owned

    monkeypatch.setattr(command, "_authoritative_task_owner", delayed_central_authority)
    monkeypatch.setattr(
        command,
        "_store_task_result",
        lambda *_args, **_kwargs: pytest.fail(
            "a stale strict Ray Job completion must not reach result storage"
        ),
    )

    def replace_task_identity() -> dict[str, Any]:
        close_old_connections()
        try:
            assert command.lease_identity is not None
            assert replacement_command.lease_identity is not None
            with transaction.atomic():
                list(
                    TaskWorkerLease.objects.select_for_update()
                    .filter(worker_id__in=[command.worker_id, replacement_command.worker_id])
                    .order_by("worker_id")
                )
                current = RayTaskExecution.objects.select_for_update().get(pk=task.pk)
                assert current.completion_data == completion_data
                current.claimed_by_worker = replacement_command.worker_id
                current.attempt_number = 4
                current.execution_generation = 8
                current.ray_job_id = replacement_ray_job_id
                current.completion_data = None
                current.started_at = observed_at + timedelta(seconds=1)
                current.last_heartbeat_at = observed_at + timedelta(seconds=1)
                current.managed_with_django_ray_version = "0.6.0-replacement-manager"
                current.executor_django_ray_version = "0.6.0-replacement-executor"
                current.save(
                    update_fields=[
                        "claimed_by_worker",
                        "attempt_number",
                        "execution_generation",
                        "ray_job_id",
                        "completion_data",
                        "started_at",
                        "last_heartbeat_at",
                        "managed_with_django_ray_version",
                        "executor_django_ray_version",
                    ]
                )
                replacement = RayTaskExecution.objects.values().get(pk=task.pk)
                replacement_locked.set()
                if not release_replacement.wait(timeout=10):
                    raise TimeoutError("test did not release the strict replacement transaction")
                return replacement
        finally:
            close_old_connections()

    def reconcile_strict_completion() -> None:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                reconcile_backend_pid.append(int(cursor.fetchone()[0]))
            reconcile_connected.set()
            current = RayTaskExecution.objects.get(pk=task.pk)
            command.active_tasks = {int(task.pk): ray_job_id}
            command.active_task_identities = {int(task.pk): (3, 7)}
            command._reconcile_ray_job_task(
                current,
                PublishingRunner(),
                ray_job_id=ray_job_id,
                completed_tasks=[],
                orphaned=False,
                tracked_identity=(3, 7),
            )
        finally:
            close_old_connections()

    replacement_future = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        reconcile_future = executor.submit(reconcile_strict_completion)
        try:
            assert reconcile_connected.wait(timeout=10)
            assert completion_published.wait(timeout=10)
            assert central_authority_entered.wait(timeout=10)
            replacement_future = executor.submit(replace_task_identity)
            assert replacement_locked.wait(timeout=10)
            release_central_authority.set()
            _wait_for_postgresql_lock(reconcile_backend_pid[0])
        finally:
            release_central_authority.set()
            release_replacement.set()

        assert replacement_future is not None
        replacement = replacement_future.result(timeout=20)
        assert reconcile_future.result(timeout=20) is None

    assert RayTaskExecution.objects.values().get(pk=task.pk) == replacement
    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.claimed_by_worker == replacement_command.worker_id
    assert task.attempt_number == 4
    assert task.execution_generation == 8
    assert task.ray_job_id == replacement_ray_job_id
    assert task.completion_data is None
    assert task.result_data is None
    assert task.result_reference is None
    assert task.managed_with_django_ray_version == "0.6.0-replacement-manager"
    assert task.executor_django_ray_version == "0.6.0-replacement-executor"
    assert not TaskAttempt.objects.filter(execution=task).exists()


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
        managed_with_django_ray_version="0.4.0-manager",
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
    assert task.managed_with_django_ray_version == django_ray_version
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
            # Keep the real seeded 60-second common authority. Only the clock
            # advances after the waiter is observed at the lease-row lock.
            current_time = early + timedelta(seconds=61)
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
