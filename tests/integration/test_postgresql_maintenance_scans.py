"""PostgreSQL races across bounded worker maintenance candidate pages."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Barrier, Event
from typing import Any

import pytest
from django.db import close_old_connections, connection

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
)
from django_ray.runner.base import JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def _command(worker_id: str) -> Command:
    command = Command()
    command.stdout = StringIO()
    command.worker_id = worker_id
    command.execution_mode = "local"
    command.sync_mode = False
    command._create_lease("default")
    return command


def _expired_owner() -> str:
    owner = "maintenance-expired-source"
    TaskWorkerLease.objects.create(
        worker_id=owner,
        hostname="maintenance-expired-host",
        pid=1201,
        queue_name="default",
        last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
        is_active=True,
    )
    return owner


def _execution(task_id: str, *, state: str, owner: str) -> RayTaskExecution:
    now = datetime.now(UTC)
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        queue_name="default",
        state=state,
        claimed_by_worker=owner,
        attempt_number=2,
        execution_generation=7,
        ray_job_id="raysubmit_maintenance_" + task_id,
        ray_address="http://maintenance-ray-head:8265",
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=5),
        timeout_seconds=1,
    )


def _independent(operation: Callable[[], int]) -> int:
    close_old_connections()
    try:
        return operation()
    finally:
        close_old_connections()


def _record_remote_effects(monkeypatch: pytest.MonkeyPatch, command: Command) -> list[str]:
    effects: list[str] = []

    def stop(_task: RayTaskExecution) -> CancellationOutcome:
        effects.append("stop")
        return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

    class RecordingRunner:
        def get_status(self, handle: SubmissionHandle) -> JobInfo:
            effects.append("status")
            return JobInfo(job_id=handle.ray_job_id, status=JobStatus.RUNNING)

    monkeypatch.setattr(command, "_request_cancellation_for_task", stop)
    monkeypatch.setattr(command, "_request_timeout_cancellation", stop)
    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", RecordingRunner)
    return effects


@pytest.mark.parametrize("maintenance", ["orphan", "timeout", "cancellation"])
@pytest.mark.parametrize(
    "change", ["terminal", "replacement_identity", "replacement_owner", "deleted"]
)
def test_selected_maintenance_candidate_cannot_control_a_concurrent_replacement(
    monkeypatch: pytest.MonkeyPatch, maintenance: str, change: str
) -> None:
    """A committed change after page selection invalidates the old candidate."""
    command = _command("maintenance-page-observer")
    replacement = _command("maintenance-replacement-owner")
    expired_owner = _expired_owner()
    task = _execution(
        "page-race-" + maintenance,
        state=TaskState.CANCELLING if maintenance == "cancellation" else TaskState.RUNNING,
        owner=command.worker_id if maintenance == "timeout" else expired_owner,
    )
    execution_pk = task.pk
    effects = _record_remote_effects(monkeypatch, command)
    selected, release = Event(), Event()
    page_sizes: list[int] = []
    original_page = command._maintenance_scan_page

    def pause_selected_page(*args: Any, **kwargs: Any):
        page = original_page(*args, **kwargs)
        page_sizes.append(len(page))
        selected.set()
        if not release.wait(timeout=10):
            raise TimeoutError("maintenance candidate page was not released")
        return page

    monkeypatch.setattr(command, "_maintenance_scan_page", pause_selected_page)
    operation = {
        "orphan": command.reconcile_tasks,
        "timeout": command.detect_stuck_tasks,
        "cancellation": command.process_cancellations,
    }[maintenance]
    fields = (
        "state",
        "claimed_by_worker",
        "attempt_number",
        "execution_generation",
        "ray_job_id",
        "completion_data",
        "result_data",
        "finished_at",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_independent, lambda: operation(("default",)))
        try:
            assert selected.wait(timeout=10)
            assert page_sizes == [1]
            if change == "deleted":
                task.delete()
                expected = None
            else:
                updates = {
                    "terminal": {
                        "state": TaskState.SUCCEEDED,
                        "result_data": "42",
                        "finished_at": datetime.now(UTC),
                    },
                    "replacement_identity": {"attempt_number": 3, "execution_generation": 8},
                    "replacement_owner": {"claimed_by_worker": replacement.worker_id},
                }[change]
                assert RayTaskExecution.objects.filter(pk=task.pk).update(**updates) == 1
                expected = RayTaskExecution.objects.filter(pk=task.pk).values(*fields).get()
        finally:
            release.set()
        assert future.result(timeout=20) == 0

    assert effects == []
    assert not command.active_tasks
    assert not TaskAttempt.objects.filter(execution_id=execution_pk).exists()
    if expected is None:
        assert not RayTaskExecution.objects.filter(task_id="page-race-" + maintenance).exists()
    else:
        assert RayTaskExecution.objects.filter(pk=task.pk).values(*fields).get() == expected
    assert TaskWorkerLease.objects.get(worker_id=expired_owner).is_active


def test_competing_selected_cancellation_pages_stop_and_archive_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real connections may select the same page but cannot share authority."""
    owner = _expired_owner()
    task = _execution("contended-cancel-page", state=TaskState.CANCELLING, owner=owner)
    commands = [_command("maintenance-adopter-a"), _command("maintenance-adopter-b")]
    selected = Barrier(2)
    effects: list[int] = []

    for command in commands:
        original_page = command._maintenance_scan_page

        def synchronized_page(*args: Any, _original=original_page, **kwargs: Any):
            page = _original(*args, **kwargs)
            assert len(page) == 1
            selected.wait(timeout=10)
            return page

        def stop(current: RayTaskExecution) -> CancellationOutcome:
            effects.append(current.pk)
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr(command, "_maintenance_scan_page", synchronized_page)
        monkeypatch.setattr(command, "_request_cancellation_for_task", stop)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_independent, lambda current=command: current.process_cancellations())
            for command in commands
        ]
        results = [future.result(timeout=20) for future in futures]

    assert sorted(results) == [0, 1]
    assert effects == [task.pk]
    task.refresh_from_db()
    assert task.state == TaskState.CANCELLED
    assert task.cancellation_status == CancellationStatus.REQUESTED
    assert task.attempt_number == 2 and task.execution_generation == 7
    assert task.claimed_by_worker in {command.worker_id for command in commands}
    assert TaskAttempt.objects.filter(execution=task, attempt_number=2).count() == 1
    assert not TaskWorkerLease.objects.get(worker_id=owner).is_active


def test_completion_publication_after_running_snapshot_prevents_timeout_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compact NULL read cannot authorize overwriting a later publication."""
    command = _command("maintenance-completion-observer")
    task = _execution(
        "running-completion-publication", state=TaskState.RUNNING, owner=command.worker_id
    )
    effects = _record_remote_effects(monkeypatch, command)
    snapshot_ready, release = Event(), Event()
    original_snapshot = command._maintenance_snapshot

    def pause_running_snapshot(*args: Any, **kwargs: Any):
        snapshot = original_snapshot(*args, **kwargs)
        assert snapshot is not None and snapshot.completion_data is None
        snapshot_ready.set()
        if not release.wait(timeout=10):
            raise TimeoutError("running maintenance snapshot was not released")
        return snapshot

    monkeypatch.setattr(command, "_maintenance_snapshot", pause_running_snapshot)
    # The detector must preserve a published value without interpreting it;
    # terminal completion validation belongs to the reconciliation path.
    completion_data = '{"success":true,"result":42}'
    fields = (
        "state",
        "claimed_by_worker",
        "attempt_number",
        "execution_generation",
        "ray_job_id",
        "last_heartbeat_at",
        "completion_data",
        "result_data",
        "finished_at",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_independent, lambda: command.detect_stuck_tasks(("default",)))
        try:
            assert snapshot_ready.wait(timeout=10)
            assert (
                RayTaskExecution.objects.filter(pk=task.pk).update(completion_data=completion_data)
                == 1
            )
            expected = RayTaskExecution.objects.filter(pk=task.pk).values(*fields).get()
        finally:
            release.set()
        assert future.result(timeout=20) == 0

    assert effects == []
    assert RayTaskExecution.objects.filter(pk=task.pk).values(*fields).get() == expected
    assert expected["state"] == TaskState.RUNNING
    assert expected["completion_data"] == completion_data
    assert not TaskAttempt.objects.filter(execution=task).exists()


def test_cancellation_pages_finish_a_frozen_cycle_before_concurrent_inserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later insert cannot extend the old cycle or hide its remaining tail."""
    owner = _expired_owner()
    command = _command("maintenance-paged-cancellation")
    tasks = [
        _execution(f"cancel-page-{index}", state=TaskState.CANCELLING, owner=owner)
        for index in range(35)
    ]
    stopped: list[int] = []

    def stop(current: RayTaskExecution) -> CancellationOutcome:
        stopped.append(current.pk)
        return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

    monkeypatch.setattr(command, "_request_cancellation_for_task", stop)
    assert command.process_cancellations(("default",)) == 32
    assert stopped == [task.pk for task in tasks[:32]]
    late = _execution("cancel-page-late", state=TaskState.CANCELLING, owner=owner)
    assert command.process_cancellations(("default",)) == 3
    assert stopped == [task.pk for task in tasks]
    assert RayTaskExecution.objects.get(pk=late.pk).state == TaskState.CANCELLING
    assert command.process_cancellations(("default",)) == 1
    assert stopped == [task.pk for task in tasks] + [late.pk]
    assert RayTaskExecution.objects.filter(state=TaskState.CANCELLING).count() == 0
    assert TaskAttempt.objects.filter(state=TaskState.CANCELLED).count() == 36
