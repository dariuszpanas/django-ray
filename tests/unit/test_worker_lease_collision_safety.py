"""Collision and ownership-fence tests for task-manager worker leases."""

from __future__ import annotations

import random
import signal
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import CommandError
from django.db import IntegrityError, OperationalError

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
)
from django_ray.runner.base import JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
)
from django_ray.runner.leasing import get_active_worker_count


class CapturingStdout:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str = "", ending: str = "\n") -> None:
        self.messages.append(f"{message}{ending}")

    def flush(self) -> None:
        return

    def getvalue(self) -> str:
        return "".join(self.messages)


def _make_command(worker_id: str) -> Command:
    command = Command()
    command.stdout = CapturingStdout()
    command.worker_id = worker_id
    command.logger = SimpleNamespace(error=lambda _message: None)
    command.poll_base_interval = 1.0
    command.poll_max_interval = 2.0
    command.polling_policy = command._new_polling_policy()
    command.execution_mode = "sync"
    command.sync_mode = False
    return command


def _invalidate_exact_lease(command: Command, invalid_lease: str) -> None:
    """Invalidate one acquired identity without changing its durable task owner."""
    assert command.lease_identity is not None
    identity = command.lease_identity
    if invalid_lease == "inactive":
        TaskWorkerLease.objects.filter(**identity.database_filters()).update(
            is_active=False,
            stopped_at=datetime.now(UTC),
        )
        return
    if invalid_lease == "expired":
        TaskWorkerLease.objects.filter(**identity.database_filters()).update(
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        return
    if invalid_lease == "replaced":
        TaskWorkerLease.objects.filter(**identity.database_filters()).delete()
        TaskWorkerLease.objects.create(
            worker_id=identity.worker_id,
            hostname="replacement-host",
            pid=222,
            queue_name="default",
        )
        return
    raise AssertionError(f"unsupported invalid lease fixture: {invalid_lease}")


@pytest.mark.django_db
class TestWorkerLeaseCollisionSafety:
    def test_claim_without_acquired_identity_fails_before_row_selection(self, monkeypatch) -> None:
        command = _make_command("unacquired-worker")
        queued = RayTaskExecution.objects.create(
            task_id="unacquired-claim",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        monkeypatch.setattr(
            TaskWorkerLease.objects,
            "select_for_update",
            lambda: pytest.fail("lease or task selection ran without an acquired identity"),
        )
        monkeypatch.setattr(
            RayTaskExecution.objects,
            "select_for_update",
            lambda: pytest.fail("task selection ran without an acquired identity"),
        )

        assert command.claim_and_process_tasks(["default"], concurrency=1) == 0

        queued.refresh_from_db()
        assert queued.state == TaskState.QUEUED
        assert queued.claimed_by_worker is None
        assert command.shutdown_requested is True

    def test_primary_key_collision_regenerates_identity_logger_and_jitter(
        self, monkeypatch
    ) -> None:
        existing = TaskWorkerLease.objects.create(
            worker_id="colliding-worker",
            hostname="foreign-host",
            pid=111,
            queue_name="default",
        )
        command = _make_command("colliding-worker")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: "replacement-worker",
        )

        command._create_lease("default")

        assert command.worker_id == "replacement-worker"
        assert command.lease_identity is not None
        assert command.lease_identity.worker_id == "replacement-worker"
        assert dict(command.logger.extra or {})["worker_id"] == "replacement-worker"
        expected_delay = 1.0 * (1.0 - 0.2 * random.Random("replacement-worker").random())
        assert command.polling_policy.next_delay(activity=False) == pytest.approx(expected_delay)
        assert get_active_worker_count() == 2
        existing.refresh_from_db()
        assert existing.hostname == "foreign-host"
        assert existing.pid == 111
        assert command.stdout.getvalue() == ""

    def test_two_collisions_allocate_third_identity_and_preserve_foreign_rows(
        self,
        monkeypatch,
    ) -> None:
        foreign_rows = [
            TaskWorkerLease.objects.create(
                worker_id=worker_id,
                hostname=f"{worker_id}-host",
                pid=111,
                queue_name="default",
            )
            for worker_id in ("collision-a", "collision-b")
        ]
        command = _make_command("collision-a")
        generated = iter(("collision-b", "allocated-c"))
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: next(generated),
        )

        command._create_lease("default")

        assert command.worker_id == "allocated-c"
        assert command.lease_identity is not None
        assert command.lease_identity.worker_id == "allocated-c"
        assert dict(command.logger.extra or {})["worker_id"] == "allocated-c"
        expected_delay = 1.0 * (1.0 - 0.2 * random.Random("allocated-c").random())
        assert command.polling_policy.next_delay(activity=False) == pytest.approx(expected_delay)
        assert get_active_worker_count() == 3
        for foreign in foreign_rows:
            foreign.refresh_from_db()
            assert foreign.hostname == f"{foreign.worker_id}-host"
            assert foreign.pid == 111

    def test_collision_recovery_preserves_claim_and_reconciliation_boundaries(
        self, monkeypatch
    ) -> None:
        TaskWorkerLease.objects.create(
            worker_id="shared-candidate",
            hostname="foreign-host",
            pid=111,
            queue_name="default",
        )
        command = _make_command("shared-candidate")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: "allocated-worker",
        )
        command._create_lease("default")

        queued = RayTaskExecution.objects.create(
            task_id="collision-safe-claim",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        processed: list[int] = []
        monkeypatch.setattr(command, "process_task", lambda task: processed.append(task.pk))

        assert command.claim_and_process_tasks(["default"], concurrency=1) == 1

        queued.refresh_from_db()
        assert queued.claimed_by_worker == "allocated-worker"
        assert processed == [queued.pk]

        foreign_running = RayTaskExecution.objects.create(
            task_id="collision-safe-reconcile",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="shared-candidate",
            ray_job_id="raysubmit_collision_safe_reconcile",
            started_at=datetime.now(UTC),
        )
        reconciled: list[int] = []
        monkeypatch.setattr(
            "django_ray.runner.ray_job.RayJobRunner",
            lambda: object(),
        )
        monkeypatch.setattr(
            command,
            "_reconcile_ray_job_task",
            lambda task, *_args, **_kwargs: reconciled.append(task.pk),
        )

        assert command.reconcile_tasks() == 0
        assert foreign_running.pk not in reconciled

    @pytest.mark.parametrize(
        ("execution_mode", "expected_dispatch"),
        [
            ("sync", "sync"),
            ("local", "ray-core"),
            ("ray", "ray-job"),
        ],
    )
    def test_collision_recovery_fences_claims_and_expiry_in_every_mode(
        self,
        monkeypatch,
        execution_mode: str,
        expected_dispatch: str,
    ) -> None:
        TaskWorkerLease.objects.create(
            worker_id=f"shared-{execution_mode}",
            hostname="foreign-host",
            pid=111,
            queue_name="default",
        )
        command = _make_command(f"shared-{execution_mode}")
        command.execution_mode = execution_mode
        allocated_worker = f"allocated-{execution_mode}"
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: allocated_worker,
        )
        command._create_lease("default")

        queued = RayTaskExecution.objects.create(
            task_id=f"mode-claim-{execution_mode}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        dispatched: list[tuple[str, int, str | None]] = []
        monkeypatch.setattr(
            command,
            "execute_task_sync",
            lambda task: dispatched.append(("sync", task.pk, task.claimed_by_worker)),
        )
        monkeypatch.setattr(
            command,
            "submit_task_to_ray_core",
            lambda task: dispatched.append(("ray-core", task.pk, task.claimed_by_worker)),
        )
        monkeypatch.setattr(
            command,
            "submit_task_to_ray",
            lambda task: dispatched.append(("ray-job", task.pk, task.claimed_by_worker)),
        )

        assert command.claim_and_process_tasks(["default"], concurrency=1) == 1

        queued.refresh_from_db()
        assert queued.claimed_by_worker == allocated_worker
        assert dispatched == [(expected_dispatch, queued.pk, allocated_worker)]
        assert get_active_worker_count() == 2

        assert command.lease_identity is not None
        TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).delete()
        replacement = TaskWorkerLease.objects.create(
            worker_id=allocated_worker,
            hostname="replacement-host",
            pid=222,
            queue_name="default",
        )
        overdue = RayTaskExecution.objects.create(
            task_id=f"mode-expiry-{execution_mode}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
            queue_deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        )

        assert command.claim_and_process_tasks(["default"], concurrency=0) == 0

        overdue.refresh_from_db()
        replacement.refresh_from_db()
        assert overdue.state == TaskState.QUEUED
        assert overdue.claimed_by_worker is None
        assert replacement.is_active is True
        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 1

    def test_deleted_lease_id_remains_reserved_by_inflight_task(self, monkeypatch) -> None:
        orphaned = RayTaskExecution.objects.create(
            task_id="deleted-lease-inflight-owner",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="deleted-lease-worker",
            ray_job_id="raysubmit_deleted_lease_inflight_owner",
            started_at=datetime.now(UTC),
        )
        command = _make_command("deleted-lease-worker")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: "fresh-worker",
        )

        command._create_lease("default")

        assert command.worker_id == "fresh-worker"
        assert command.lease_identity is not None
        assert command.lease_identity.worker_id == "fresh-worker"
        assert not TaskWorkerLease.objects.filter(worker_id="deleted-lease-worker").exists()
        assert TaskWorkerLease.objects.filter(worker_id="fresh-worker").exists()

        reconciled: list[int] = []
        monkeypatch.setattr(
            "django_ray.runner.ray_job.RayJobRunner",
            lambda: object(),
        )
        monkeypatch.setattr(
            command,
            "_reconcile_ray_job_task",
            lambda task, *_args, **_kwargs: reconciled.append(task.pk),
        )

        assert command.reconcile_tasks() == 1
        assert reconciled == [orphaned.pk]
        orphaned.refresh_from_db()
        assert orphaned.claimed_by_worker == "fresh-worker"

    def test_cancelling_task_also_reserves_deleted_lease_id(self, monkeypatch) -> None:
        RayTaskExecution.objects.create(
            task_id="deleted-lease-cancelling-owner",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="deleted-cancelling-worker",
            started_at=datetime.now(UTC),
        )
        command = _make_command("deleted-cancelling-worker")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: "fresh-cancellation-worker",
        )

        command._create_lease("default")

        assert command.worker_id == "fresh-cancellation-worker"
        assert not TaskWorkerLease.objects.filter(worker_id="deleted-cancelling-worker").exists()

    def test_inflight_owner_reservation_exhaustion_is_bounded(self, monkeypatch) -> None:
        RayTaskExecution.objects.create(
            task_id="retained-owner-exhaustion",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="reserved-worker",
            started_at=datetime.now(UTC),
        )
        command = _make_command("reserved-worker")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: "reserved-worker",
        )

        with pytest.raises(CommandError, match="bounded retries") as captured:
            command._create_lease("default")

        assert captured.value.__cause__ is None
        assert "reserved-worker" not in str(captured.value)
        assert command.lease_identity is None
        assert not TaskWorkerLease.objects.exists()

    def test_collision_exhaustion_fails_without_adopting_or_disclosing_candidates(
        self, monkeypatch
    ) -> None:
        for worker_id in ("collision-a", "collision-b"):
            TaskWorkerLease.objects.create(
                worker_id=worker_id,
                hostname="foreign-host",
                pid=111,
                queue_name="default",
            )
        command = _make_command("collision-a")
        generated: list[str] = []
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: generated.append("collision-b") or "collision-b",
        )

        with pytest.raises(CommandError, match="bounded retries") as captured:
            command._create_lease("default")

        assert captured.value.__cause__ is None
        assert "collision-a" not in str(captured.value)
        assert "collision-b" not in str(captured.value)
        assert command.lease is None
        assert command.lease_identity is None
        assert TaskWorkerLease.objects.count() == 2
        assert generated == ["collision-b", "collision-b"]
        assert command.stdout.getvalue() == ""

    def test_unrelated_integrity_error_does_not_regenerate_existing_candidate(
        self, monkeypatch
    ) -> None:
        TaskWorkerLease.objects.create(
            worker_id="existing-candidate",
            hostname="foreign-host",
            pid=111,
            queue_name="default",
        )
        command = _make_command("existing-candidate")
        generated: list[str] = []
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.generate_worker_id",
            lambda: generated.append("unexpected") or "unexpected",
        )

        def fail_with_unrelated_integrity_error(**_kwargs: Any) -> None:
            try:
                raise sqlite3.IntegrityError(
                    "NOT NULL constraint failed: django_ray_taskworkerlease.hostname"
                )
            except sqlite3.IntegrityError as cause:
                raise IntegrityError("unrelated lease constraint") from cause

        monkeypatch.setattr(TaskWorkerLease.objects, "create", fail_with_unrelated_integrity_error)

        with pytest.raises(CommandError, match="Could not create worker lease"):
            command._create_lease("default")

        assert generated == []
        assert command.lease_identity is None

    def test_inactive_exact_owner_cannot_reactivate(self) -> None:
        command = _make_command("inactive-owner")
        command._create_lease("default")
        assert command.lease_identity is not None
        TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).update(
            is_active=False, stopped_at=datetime.now(UTC)
        )

        assert command._recreate_lease() is False

        lease = TaskWorkerLease.objects.get(**command.lease_identity.database_filters())
        assert lease.is_active is False
        assert lease.stopped_at is not None
        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 1

    def test_stale_owner_cannot_resume_after_ray_job_adoption(self, monkeypatch) -> None:
        stale_owner = _make_command("stale-owner")
        stale_owner.execution_mode = "ray"
        stale_owner._create_lease("default")
        assert stale_owner.lease_identity is not None
        stale_at = datetime.now(UTC) - timedelta(minutes=5)
        TaskWorkerLease.objects.filter(**stale_owner.lease_identity.database_filters()).update(
            last_heartbeat_at=stale_at
        )
        task = RayTaskExecution.objects.create(
            task_id="stale-owner-adoption",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=stale_owner.worker_id,
            ray_job_id="raysubmit_stale_owner_adoption",
            started_at=stale_at,
            last_heartbeat_at=stale_at,
        )
        stale_owner.active_tasks = {task.pk: str(task.ray_job_id)}
        stale_owner.active_task_identities = {
            task.pk: (task.attempt_number, task.execution_generation)
        }

        adopter = _make_command("adopter")
        adopter.execution_mode = "ray"
        adopter._create_lease("default")
        snapshot = RayTaskExecution.objects.get(pk=task.pk)

        assert (
            adopter._adopt_orphaned_ray_job_task(
                snapshot,
                now=datetime.now(UTC),
            )
            is True
        )

        source_lease = TaskWorkerLease.objects.get(worker_id=stale_owner.worker_id)
        assert source_lease.is_active is False
        task.refresh_from_db()
        assert task.claimed_by_worker == adopter.worker_id

        stale_owner.send_heartbeat()
        assert stale_owner.shutdown_requested is True
        assert stale_owner.shutdown_exit_code == 1

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", object)
        assert stale_owner.reconcile_tasks() == 0
        assert task.pk not in stale_owner.active_tasks

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == adopter.worker_id

    @pytest.mark.parametrize("invalid_lease", ["inactive", "expired", "replaced"])
    def test_invalid_exact_lease_cannot_adopt_or_mutate_stale_owner(
        self,
        invalid_lease: str,
    ) -> None:
        stale_at = datetime.now(UTC) - timedelta(minutes=5)
        source_lease = TaskWorkerLease.objects.create(
            worker_id="invalid-adoption-source",
            hostname="source-host",
            pid=111,
            queue_name="default",
            last_heartbeat_at=stale_at,
        )
        task = RayTaskExecution.objects.create(
            task_id=f"invalid-adopter-{invalid_lease}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=source_lease.worker_id,
            ray_job_id=f"raysubmit_invalid_adopter_{invalid_lease}",
            started_at=stale_at,
            last_heartbeat_at=stale_at,
            attempt_number=2,
            execution_generation=7,
        )
        adopter = _make_command(f"invalid-adopter-{invalid_lease}")
        adopter.execution_mode = "ray"
        adopter._create_lease("default")
        assert adopter.lease_identity is not None
        identity = adopter.lease_identity

        if invalid_lease == "inactive":
            TaskWorkerLease.objects.filter(**identity.database_filters()).update(
                is_active=False,
                stopped_at=datetime.now(UTC),
            )
        elif invalid_lease == "expired":
            TaskWorkerLease.objects.filter(**identity.database_filters()).update(
                last_heartbeat_at=stale_at
            )
        else:
            TaskWorkerLease.objects.filter(**identity.database_filters()).delete()
            TaskWorkerLease.objects.create(
                worker_id=identity.worker_id,
                hostname="replacement-host",
                pid=222,
                queue_name="default",
            )

        snapshot = RayTaskExecution.objects.get(pk=task.pk)
        assert adopter._adopt_orphaned_ray_job_task(snapshot, now=datetime.now(UTC)) is False

        task.refresh_from_db()
        source_lease.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == source_lease.worker_id
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert source_lease.is_active is True
        assert adopter.active_tasks == {}
        assert adopter.active_task_identities == {}
        assert adopter.shutdown_requested is True
        assert adopter.shutdown_exit_code == 1
        assert not TaskAttempt.objects.filter(execution=task).exists()

    @pytest.mark.parametrize("recovery_kind", ["timeout", "lost"])
    def test_invalid_exact_lease_cannot_recover_running_task(
        self,
        monkeypatch,
        recovery_kind: str,
    ) -> None:
        stale_at = datetime.now(UTC) - timedelta(minutes=10)
        command = _make_command(f"invalid-{recovery_kind}-owner")
        command._create_lease("default")
        assert command.lease_identity is not None
        task = RayTaskExecution.objects.create(
            task_id=f"invalid-{recovery_kind}-recovery",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            started_at=stale_at,
            last_heartbeat_at=stale_at,
            timeout_seconds=1 if recovery_kind == "timeout" else None,
            attempt_number=2,
            execution_generation=7,
        )
        TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).update(
            is_active=False,
            stopped_at=datetime.now(UTC),
        )
        monkeypatch.setattr(
            command,
            "_request_timeout_cancellation",
            lambda _task: pytest.fail("an invalid worker must not issue a timeout stop"),
        )

        assert command.detect_stuck_tasks() == 0

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == command.worker_id
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.error_message is None
        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 1
        assert not TaskAttempt.objects.filter(execution=task).exists()

    @pytest.mark.parametrize("invalid_lease", ["inactive", "expired", "replaced"])
    @pytest.mark.parametrize(
        ("reconciliation_case", "job_status"),
        [
            ("completion", None),
            ("failed", JobStatus.FAILED),
            ("stopped", JobStatus.STOPPED),
            ("pending", JobStatus.PENDING),
            ("running", JobStatus.RUNNING),
        ],
    )
    def test_invalid_exact_lease_cannot_apply_ray_job_reconciliation_effects(
        self,
        monkeypatch: pytest.MonkeyPatch,
        invalid_lease: str,
        reconciliation_case: str,
        job_status: JobStatus | None,
    ) -> None:
        command = _make_command(f"invalid-ray-job-{invalid_lease}-{reconciliation_case}")
        command.execution_mode = "ray"
        command._create_lease("default")
        initial_activity = datetime.now(UTC) - timedelta(seconds=1)
        completion_data = (
            '{"success": true, "result": 3}' if reconciliation_case == "completion" else None
        )
        task = RayTaskExecution.objects.create(
            task_id=f"invalid-ray-job-{invalid_lease}-{reconciliation_case}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            ray_job_id=f"raysubmit_invalid_{invalid_lease}_{reconciliation_case}",
            ray_address="ray://cluster:10001",
            started_at=initial_activity,
            last_heartbeat_at=initial_activity,
            completion_data=completion_data,
            attempt_number=2,
            execution_generation=7,
        )
        command.active_tasks = {task.pk: str(task.ray_job_id)}
        command.active_task_identities = {task.pk: (2, 7)}
        _invalidate_exact_lease(command, invalid_lease)

        if reconciliation_case == "completion":
            monkeypatch.setattr(
                command,
                "_store_and_succeed_task",
                lambda *_args, **_kwargs: pytest.fail(
                    "an invalid lease must not persist or complete a result"
                ),
            )
        elif reconciliation_case == "failed":
            monkeypatch.setattr(
                command,
                "_handle_task_failure",
                lambda *_args, **_kwargs: pytest.fail(
                    "an invalid lease must not apply a Ray Job failure"
                ),
            )
        elif reconciliation_case == "stopped":
            monkeypatch.setattr(
                "django_ray.management.commands.django_ray_worker.cancel_task",
                lambda *_args, **_kwargs: pytest.fail(
                    "an invalid lease must not finalize a stopped Ray Job"
                ),
            )
        else:
            monkeypatch.setattr(
                command,
                "_mark_task_monitor_heartbeat",
                lambda *_args, **_kwargs: pytest.fail(
                    "an invalid lease must not refresh a monitor heartbeat"
                ),
            )

        class FakeRunner:
            def get_status(self, _handle: SubmissionHandle) -> JobInfo:
                if job_status is None:
                    pytest.fail("a durable completion must be considered before the status RPC")
                return JobInfo(
                    job_id=str(task.ray_job_id),
                    status=job_status,
                    message="diagnostic status",
                )

            def get_logs(self, _handle: SubmissionHandle) -> str:
                return "diagnostic logs"

            def cancel(self, _handle: SubmissionHandle) -> bool:
                pytest.fail("reconciliation must not issue a remote stop")

        completed_tasks: list[int] = []
        command._reconcile_ray_job_task(
            task,
            FakeRunner(),
            ray_job_id=str(task.ray_job_id),
            completed_tasks=completed_tasks,
            orphaned=False,
            tracked_identity=(2, 7),
        )

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == command.worker_id
        assert task.last_heartbeat_at == initial_activity
        assert task.completion_data == completion_data
        assert task.result_data is None
        assert task.error_message is None
        assert task.cancellation_status is None
        assert completed_tasks == []
        assert command.active_tasks == {}
        assert command.active_task_identities == {}
        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 1
        assert command.lease_ownership_lost is True
        assert not TaskAttempt.objects.filter(execution=task).exists()

    @pytest.mark.parametrize("invalid_lease", ["inactive", "expired", "replaced"])
    def test_invalid_exact_lease_quarantines_only_observed_mismatched_ray_job(
        self,
        invalid_lease: str,
    ) -> None:
        command = _make_command(f"invalid-mismatch-{invalid_lease}")
        command.execution_mode = "ray"
        command._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id=f"invalid-mismatch-{invalid_lease}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            ray_job_id=f"raysubmit_reserved_{invalid_lease}",
            ray_address="ray://cluster:10001",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            attempt_number=2,
            execution_generation=7,
        )
        reserved_handle = SubmissionHandle(
            ray_job_id=str(task.ray_job_id),
            ray_address=str(task.ray_address),
            submitted_at=datetime.now(UTC),
        )
        observed_handle = SubmissionHandle(
            ray_job_id=f"raysubmit_observed_{invalid_lease}",
            ray_address=str(task.ray_address),
            submitted_at=datetime.now(UTC),
        )
        command.active_tasks = {task.pk: reserved_handle.ray_job_id}
        command.active_task_identities = {task.pk: (2, 7)}
        _invalidate_exact_lease(command, invalid_lease)
        prepared: list[str] = []
        remote_stops: list[str] = []

        class FakeRunner:
            def prepare_cancellation(self, handle: SubmissionHandle) -> str:
                prepared.append(handle.ray_job_id)
                return handle.ray_job_id

            def cancel_prepared_with_status(
                self,
                handle: SubmissionHandle,
                capability: object,
            ) -> CancellationOutcome:
                assert capability == handle.ray_job_id
                remote_stops.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        command._handle_mismatched_ray_job_submission(
            task,
            FakeRunner(),
            reserved_handle,
            observed_handle,
            expected_worker_id=command.worker_id,
            expected_attempt_number=2,
            expected_execution_generation=7,
            error_message="Ray returned another identity",
            exception_type="RayJobSubmissionIdentityMismatch",
        )

        task.refresh_from_db()
        assert prepared == [reserved_handle.ray_job_id, observed_handle.ray_job_id]
        assert remote_stops == [observed_handle.ray_job_id]
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == command.worker_id
        assert task.error_message is None
        assert task.cancellation_status is None
        assert command.active_tasks == {}
        assert command.active_task_identities == {}
        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 1
        assert command.lease_ownership_lost is True
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_authoritative_freshness_is_measured_after_sqlite_write_fence(
        self,
        monkeypatch,
    ) -> None:
        from django.db.models.query import QuerySet

        command = _make_command("write-fence-expiry")
        command._create_lease("default")
        assert command.lease_identity is not None
        early = datetime.now(UTC)
        initial_heartbeat = early - timedelta(seconds=1)
        TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).update(
            last_heartbeat_at=initial_heartbeat
        )
        task = RayTaskExecution.objects.create(
            task_id="write-fence-expiry",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            started_at=early,
            last_heartbeat_at=early,
        )
        current_time = early

        class ControlledDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return current_time if tz is not None else current_time.replace(tzinfo=None)

        original_update = QuerySet.update
        update_count = 0

        def advance_clock_before_first_write(queryset, **kwargs):
            nonlocal current_time, update_count
            update_count += 1
            if update_count == 1:
                current_time = early + timedelta(seconds=3)
            return original_update(queryset, **kwargs)

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.datetime",
            ControlledDateTime,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_lease_duration",
            lambda: timedelta(seconds=2),
        )
        monkeypatch.setattr(QuerySet, "update", advance_clock_before_first_write)

        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
        ) as owned:
            assert owned is None

        task.refresh_from_db()
        lease = TaskWorkerLease.objects.get(**command.lease_identity.database_filters())
        assert task.claimed_by_worker == command.worker_id
        assert lease.last_heartbeat_at == initial_heartbeat
        assert command.lease_ownership_lost is True
        assert command.shutdown_requested is True

    def test_timeout_owner_transfer_during_stop_blocks_terminal_write(
        self,
        monkeypatch,
    ) -> None:
        stale_at = datetime.now(UTC) - timedelta(minutes=10)
        command = _make_command("timeout-transfer-owner")
        command._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="timeout-owner-transfer-during-stop",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            started_at=stale_at,
            last_heartbeat_at=stale_at,
            timeout_seconds=1,
            attempt_number=2,
            execution_generation=7,
        )
        cancellation_calls: list[int] = []

        def transfer_owner_during_stop(current: RayTaskExecution) -> CancellationOutcome:
            cancellation_calls.append(current.pk)
            RayTaskExecution.objects.filter(pk=current.pk).update(
                claimed_by_worker="replacement-worker"
            )
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr(
            command,
            "_request_timeout_cancellation",
            transfer_owner_during_stop,
        )

        assert command.detect_stuck_tasks() == 0

        task.refresh_from_db()
        assert cancellation_calls == [task.pk]
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == "replacement-worker"
        assert task.cancellation_status is None
        assert task.finished_at is None
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_expired_cancellation_owner_cannot_duplicate_replacement_stop(
        self,
        monkeypatch,
    ) -> None:
        stale_at = datetime.now(UTC) - timedelta(minutes=5)
        stale_owner = _make_command("expired-cancellation-owner")
        stale_owner._create_lease("default")
        assert stale_owner.lease_identity is not None
        TaskWorkerLease.objects.filter(**stale_owner.lease_identity.database_filters()).update(
            last_heartbeat_at=stale_at
        )
        task = RayTaskExecution.objects.create(
            task_id="expired-cancellation-owner-handoff",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=stale_owner.worker_id,
            ray_job_id="raysubmit_expired_cancellation_owner",
            started_at=stale_at,
            last_heartbeat_at=stale_at,
            attempt_number=2,
            execution_generation=7,
        )
        monkeypatch.setattr(
            stale_owner,
            "_request_cancellation_for_task",
            lambda _task: pytest.fail("the expired owner must not repeat the remote stop"),
        )

        assert stale_owner.process_cancellations() == 0

        replacement = _make_command("replacement-cancellation-owner")
        replacement._create_lease("default")
        cancellation_calls: list[int] = []

        def request_once(current: RayTaskExecution) -> CancellationOutcome:
            cancellation_calls.append(current.pk)
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr(replacement, "_request_cancellation_for_task", request_once)

        assert replacement.process_cancellations() == 1

        task.refresh_from_db()
        source_lease = TaskWorkerLease.objects.get(worker_id=stale_owner.worker_id)
        assert cancellation_calls == [task.pk]
        assert task.state == TaskState.CANCELLED
        assert task.claimed_by_worker == replacement.worker_id
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert task.finished_at is not None
        assert source_lease.is_active is False
        assert stale_owner.shutdown_requested is True
        assert TaskAttempt.objects.filter(execution=task, attempt_number=2).count() == 1

    def test_deleted_exact_lease_cannot_recreate(self) -> None:
        command = _make_command("deleted-exact-owner")
        command._create_lease("default")
        assert command.lease_identity is not None
        identity = command.lease_identity
        TaskWorkerLease.objects.filter(**identity.database_filters()).delete()

        assert command._recreate_lease() is False

        assert not TaskWorkerLease.objects.filter(**identity.database_filters()).exists()
        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 1

    def test_deleted_exact_lease_with_inflight_reservation_fails_closed(self) -> None:
        command = _make_command("ambiguous-recreated-owner")
        command._create_lease("default")
        assert command.lease_identity is not None
        identity = command.lease_identity
        TaskWorkerLease.objects.filter(**identity.database_filters()).delete()
        RayTaskExecution.objects.create(
            task_id="ambiguous-recreated-owner-task",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=identity.worker_id,
            started_at=datetime.now(UTC),
        )

        assert command._recreate_lease() is False

        assert not TaskWorkerLease.objects.filter(worker_id=identity.worker_id).exists()
        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 1
        assert identity.worker_id not in command.stdout.getvalue()

    def test_deleted_lease_replaced_by_foreign_owner_is_never_adopted_or_released(
        self,
    ) -> None:
        command = _make_command("reused-worker")
        command.execution_mode = "ray"
        command._create_lease("default")
        assert command.lease_identity is not None
        original_identity = command.lease_identity
        TaskWorkerLease.objects.filter(**original_identity.database_filters()).delete()
        replacement = TaskWorkerLease.objects.create(
            worker_id=original_identity.worker_id,
            hostname="replacement-host",
            pid=222,
            queue_name="default",
        )
        task = RayTaskExecution.objects.create(
            task_id="reused-worker-inflight",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            ray_job_id="raysubmit_reused_worker_inflight",
            started_at=datetime.now(UTC),
        )
        command.active_tasks = {task.pk: str(task.ray_job_id)}
        command.active_task_identities = {task.pk: (task.attempt_number, task.execution_generation)}

        command.handle_shutdown_signal(signal.SIGTERM, None)
        assert command.lease_ownership_lost is False
        command.shutdown()

        assert command.shutdown_requested is True
        assert command.shutdown_exit_code == 128 + signal.SIGTERM
        assert command.lease_ownership_lost is True
        assert command.active_tasks == {}
        assert command.active_task_identities == {}
        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == command.worker_id
        replacement.refresh_from_db()
        assert replacement.is_active is True
        assert replacement.hostname == "replacement-host"

    def test_signal_handoff_revalidates_expired_lease_before_ray_core_stop(self) -> None:
        command = _make_command("expired-signal-handoff")
        command.execution_mode = "local"
        command._create_lease("default")
        assert command.lease_identity is not None
        TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).update(
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=5)
        )
        task = RayTaskExecution.objects.create(
            task_id="expired-signal-handoff",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            started_at=datetime.now(UTC),
        )
        pending_handle = SimpleNamespace(
            task_pk=task.pk,
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )
        command.ray_core_runner = SimpleNamespace(
            pending_task_handles=(pending_handle,),
            cancel_pending=lambda _handle: pytest.fail(
                "expired signal handler must not issue a Ray Core stop"
            ),
        )

        command.handle_shutdown_signal(signal.SIGTERM, None)
        command._prepare_shutdown_handoff()

        task.refresh_from_db()
        assert command.lease_ownership_lost is True
        assert command.shutdown_exit_code == 128 + signal.SIGTERM
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == command.worker_id

    def test_claim_fails_closed_when_owned_row_was_replaced(self) -> None:
        command = _make_command("claim-fence-worker")
        command._create_lease("default")
        assert command.lease_identity is not None
        TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).delete()
        TaskWorkerLease.objects.create(
            worker_id=command.worker_id,
            hostname="replacement-host",
            pid=222,
            queue_name="default",
        )
        queued = RayTaskExecution.objects.create(
            task_id="claim-fence-task",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        assert command.claim_and_process_tasks(["default"], concurrency=1) == 0

        queued.refresh_from_db()
        assert queued.state == TaskState.QUEUED
        assert queued.claimed_by_worker is None
        assert command.shutdown_requested is True

    def test_pre_execution_lease_loss_does_not_handoff_by_reused_worker_id(
        self,
        monkeypatch,
    ) -> None:
        command = _make_command("pre-execution-reused-worker")
        command.execution_mode = "ray"
        command._create_lease("default")
        assert command.lease_identity is not None
        task = RayTaskExecution.objects.create(
            task_id="pre-execution-reused-worker",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=command.worker_id,
            started_at=datetime.now(UTC),
        )
        TaskWorkerLease.objects.filter(**command.lease_identity.database_filters()).delete()
        TaskWorkerLease.objects.create(
            worker_id=command.worker_id,
            hostname="replacement-host",
            pid=222,
            queue_name="default",
        )
        monkeypatch.setattr(
            command,
            "_handoff_unsubmitted_task",
            lambda _task: pytest.fail("lease-lost worker must not mutate task handoff state"),
        )

        command.process_task(task)

        task.refresh_from_db()
        assert command.lease_ownership_lost is True
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == command.worker_id

    def test_shutdown_continues_cleanup_after_handoff_database_failure(
        self,
        monkeypatch,
    ) -> None:
        command = _make_command("database-outage-worker")
        command.execution_mode = "local"
        command._create_lease("default")
        assert command.lease_identity is not None
        released: list[str] = []
        ray_events: list[str] = []
        log_messages: list[tuple[str, bool]] = []
        command.logger = SimpleNamespace(
            error=lambda message, **kwargs: log_messages.append(
                (str(message), bool(kwargs.get("exc_info")))
            )
        )
        monkeypatch.setattr(
            command,
            "_prepare_shutdown_handoff",
            lambda: (_ for _ in ()).throw(OperationalError("database unavailable")),
        )
        monkeypatch.setattr(
            "django_ray.runner.leasing.release_lease",
            lambda identity: released.append(identity.worker_id) or True,
        )
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(
                is_initialized=lambda: True,
                shutdown=lambda: ray_events.append("shutdown"),
            ),
        )

        command.shutdown()

        assert released == ["database-outage-worker"]
        assert ray_events == ["shutdown"]
        assert log_messages == [("worker shutdown handoff failed", True)]
        assert command.shutdown_exit_code == 1
        assert "Failed to prepare task handoff; continuing cleanup" in command.stdout.getvalue()
        assert "shut down with errors" in command.stdout.getvalue()

    def test_shutdown_distinguishes_release_database_failure_from_fence_miss(
        self,
        monkeypatch,
    ) -> None:
        command = _make_command("release-database-outage-worker")
        command.execution_mode = "local"
        command._create_lease("default")
        log_messages: list[tuple[str, bool]] = []
        ray_events: list[str] = []
        command.logger = SimpleNamespace(
            error=lambda message, **kwargs: log_messages.append(
                (str(message), bool(kwargs.get("exc_info")))
            )
        )
        monkeypatch.setattr(command, "_prepare_shutdown_handoff", lambda: None)
        monkeypatch.setattr(
            "django_ray.runner.leasing.release_lease",
            lambda _identity: (_ for _ in ()).throw(
                OperationalError("database offline secret detail")
            ),
        )
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(
                is_initialized=lambda: True,
                shutdown=lambda: ray_events.append("shutdown"),
            ),
        )

        command.shutdown()

        output = command.stdout.getvalue()
        assert log_messages == [("worker lease release failed", True)]
        assert "Failed to release lease; see worker logs" in output
        assert "ownership fence did not match" not in output
        assert "database offline secret detail" not in output
        assert ray_events == ["shutdown"]
        assert command.shutdown_exit_code == 1


def test_handle_acquires_lease_before_initializing_ray(monkeypatch) -> None:
    command = _make_command("foreign-candidate")
    events: list[str] = []
    monkeypatch.setattr(
        "django_ray.management.commands.django_ray_worker.get_settings",
        lambda: {"DEFAULT_CONCURRENCY": 1},
    )
    monkeypatch.setattr(command, "setup_signal_handlers", lambda: None)
    monkeypatch.setattr(
        command,
        "_create_lease",
        lambda _queue: (_ for _ in ()).throw(CommandError("lease unavailable")),
    )
    monkeypatch.setattr(command, "_init_local_ray", lambda: events.append("ray"))

    with pytest.raises(CommandError, match="lease unavailable"):
        command.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=1,
            sync=False,
            local=True,
            cluster=None,
        )

    assert events == []
    assert "foreign-candidate" not in command.stdout.getvalue()
