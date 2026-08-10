"""Execution-protocol policy transition and cross-version race coverage."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO
from threading import Barrier, Event
from typing import Any, cast

import pytest
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import django_ray.protocol_coordination as protocol_coordination
from django_ray import __version__ as django_ray_version
from django_ray.execution_protocol import ExecutionProtocolRange, explicit_worker_protocol_range
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    TaskRetryRequestStatus,
    cancel_task,
    expire_queued_tasks,
    record_failure,
    record_lost,
    request_task_cancellation,
    request_task_retry,
    succeed_task,
)
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_coordination import (
    InvalidProtocolRevisionError,
    LegacyAdmissionRaceError,
    LegacyProducerRetirementRequiredError,
    LegacyWorkerAdmissionBlockedError,
    LegacyWorkerRollbackBlockedError,
    NestedProtocolTransitionError,
    ProtocolPolicyStateError,
    ProtocolRevisionConflictError,
    ProtocolRevisionExhaustedError,
    UnsupportedProtocolDatabaseError,
    close_legacy_worker_admission,
    reopen_legacy_worker_admission,
)
from django_ray.runner.base import JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.leasing import WorkerLeaseIdentity

pytestmark = pytest.mark.django_db(transaction=True)

MIGRATE_FROM = [("django_ray", "0018_workflow_run_allocation")]


def _historical_models():
    apps = MigrationExecutor(connection).loader.project_state(MIGRATE_FROM).apps
    return (
        apps.get_model("django_ray", "RayTaskExecution"),
        apps.get_model("django_ray", "TaskWorkerLease"),
    )


def _legacy_lease(worker_id: str, *, is_active: bool) -> TaskWorkerLease:
    return TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="legacy-host",
        pid=1001,
        is_active=is_active,
        stopped_at=None if is_active else timezone.now(),
    )


def _explicit_v1_lease(worker_id: str) -> TaskWorkerLease:
    return TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="v1-host",
        pid=1002,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
    )


def _rejects_integrity_error(operation) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        operation()


def _policy() -> TaskExecutionProtocolPolicy:
    return TaskExecutionProtocolPolicy.objects.get(singleton_key=1)


def _close(expected_revision: int = 1):
    return close_legacy_worker_admission(
        expected_revision=expected_revision,
        legacy_producers_retired=True,
    )


def _reopen(expected_revision: int = 2):
    return reopen_legacy_worker_admission(expected_revision=expected_revision)


def _explicit_v1_worker(worker_id: str) -> Command:
    worker = Command()
    worker.stdout = StringIO()
    worker._set_worker_id(worker_id)
    worker._create_lease("default")
    return worker


def _explicit_range_worker(
    worker_id: str,
    *,
    minimum: int,
    maximum: int,
) -> Command:
    """Build one command around an exact synthetic capability cohort."""
    worker = Command()
    worker.stdout = StringIO()
    worker._set_worker_id(worker_id)
    now = timezone.now()
    lease = TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="range-host",
        pid=1003,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=minimum,
        max_supported_execution_protocol_version=maximum,
        legacy_admission_token=None,
        started_at=now,
        last_heartbeat_at=now,
    )
    worker.lease = lease
    worker.lease_identity = WorkerLeaseIdentity(
        worker_id=worker_id,
        hostname=str(lease.hostname),
        pid=int(lease.pid),
        started_at=lease.started_at,
    )
    return worker


def test_explicit_worker_protocol_range_is_bounded_and_fail_closed() -> None:
    supported = explicit_worker_protocol_range(
        capability_schema_version=1,
        legacy_admission_token_present=False,
        minimum=1,
        maximum=2,
    )

    assert supported == ExecutionProtocolRange(minimum=1, maximum=2)
    assert supported.supports(1) is True
    assert supported.supports(2) is True
    assert supported.supports(3) is False

    invalid_advertisements: tuple[tuple[int, bool, int | None, int | None], ...] = (
        (0, True, None, None),
        (1, True, 1, 1),
        (1, False, None, 1),
        (1, False, 0, 1),
        (1, False, 2, 1),
    )
    for schema, token_present, minimum, maximum in invalid_advertisements:
        assert (
            explicit_worker_protocol_range(
                capability_schema_version=schema,
                legacy_admission_token_present=token_present,
                minimum=minimum,
                maximum=maximum,
            )
            is None
        )


def _assert_worker_claim_and_expiry_use_exact_explicit_protocol_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _close()
    now = timezone.now()
    overdue_v2 = RayTaskExecution.objects.create(
        task_id="coordination-worker-overdue-v2",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        queue_deadline_at=now - timedelta(seconds=2),
    )
    overdue_v1 = RayTaskExecution.objects.create(
        task_id="coordination-worker-overdue-v1",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=1,
        queue_deadline_at=now - timedelta(seconds=1),
    )
    queued_v2 = RayTaskExecution.objects.create(
        task_id="coordination-worker-queued-v2",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
    )
    queued_v1 = RayTaskExecution.objects.create(
        task_id="coordination-worker-queued-v1",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=1,
    )
    worker = _explicit_v1_worker("coordination-v1-claim-worker")
    processed: list[int] = []
    monkeypatch.setattr(worker, "process_task", lambda task: processed.append(int(task.pk)))

    assert worker.claim_and_process_tasks(["default"], concurrency=1) == 2

    for row in (overdue_v1, overdue_v2, queued_v1, queued_v2):
        row.refresh_from_db()
    assert overdue_v1.state == TaskState.EXPIRED
    assert TaskAttempt.objects.filter(execution=overdue_v1, state=TaskState.EXPIRED).exists()
    assert queued_v1.state == TaskState.RUNNING
    assert queued_v1.claimed_by_worker == worker.worker_id
    assert queued_v1.created_with_django_ray_version is None
    assert queued_v1.managed_with_django_ray_version == django_ray_version
    assert processed == [queued_v1.pk]
    for unsupported in (overdue_v2, queued_v2):
        assert unsupported.state == TaskState.QUEUED
        assert unsupported.started_at is None
        assert unsupported.claimed_by_worker is None
        assert not TaskAttempt.objects.filter(execution=unsupported).exists()


def test_worker_claim_and_expiry_use_its_exact_explicit_protocol_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_worker_claim_and_expiry_use_exact_explicit_protocol_range(monkeypatch)


@pytest.mark.postgresql
def test_postgresql_worker_claim_and_expiry_use_exact_explicit_protocol_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _assert_worker_claim_and_expiry_use_exact_explicit_protocol_range(monkeypatch)


def _assert_v1_and_v1_v2_workers_obey_their_exact_protocol_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _close()
    stale_at = timezone.now() - timedelta(hours=1)
    old_v2_owner = TaskWorkerLease.objects.create(
        worker_id="coordination-dual-cohort-old-v2-owner",
        hostname="v2-host",
        pid=2001,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=2,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        last_heartbeat_at=stale_at,
    )
    orphaned_v2 = RayTaskExecution.objects.create(
        task_id="coordination-dual-cohort-orphaned-v2",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=old_v2_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
        ray_job_id="raysubmit_coordination_dual_cohort_v2",
        ray_address="ray://v2-cluster:10001",
        managed_with_django_ray_version="0.5.0-v2-manager",
    )
    queued_v2 = RayTaskExecution.objects.create(
        task_id="coordination-dual-cohort-queued-v2",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
    )
    queued_v1 = RayTaskExecution.objects.create(
        task_id="coordination-dual-cohort-queued-v1",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=1,
    )
    status_calls: list[str] = []

    class PendingRunner:
        def get_status(self, handle: SubmissionHandle) -> JobInfo:
            status_calls.append(str(handle.ray_job_id))
            return JobInfo(job_id=str(handle.ray_job_id), status=JobStatus.PENDING)

    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", PendingRunner)

    v1_worker = _explicit_range_worker(
        "coordination-dual-cohort-v1-worker",
        minimum=1,
        maximum=1,
    )
    v1_processed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        v1_worker,
        "process_task",
        lambda task: v1_processed.append((int(task.pk), int(task.execution_protocol_version))),
    )

    assert v1_worker.claim_and_process_tasks(["default"], concurrency=2) == 1
    assert v1_worker.reconcile_tasks() == 0

    queued_v1.refresh_from_db()
    queued_v2.refresh_from_db()
    orphaned_v2.refresh_from_db()
    old_v2_owner.refresh_from_db()
    assert v1_processed == [(queued_v1.pk, 1)]
    assert queued_v1.state == TaskState.RUNNING
    assert queued_v1.claimed_by_worker == v1_worker.worker_id
    assert queued_v2.state == TaskState.QUEUED
    assert queued_v2.started_at is None
    assert queued_v2.claimed_by_worker is None
    assert orphaned_v2.state == TaskState.RUNNING
    assert orphaned_v2.claimed_by_worker == old_v2_owner.worker_id
    assert orphaned_v2.managed_with_django_ray_version == "0.5.0-v2-manager"
    assert old_v2_owner.is_active is True
    assert status_calls == []

    second_queued_v1 = RayTaskExecution.objects.create(
        task_id="coordination-dual-cohort-second-queued-v1",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=1,
    )
    v1_v2_worker = _explicit_range_worker(
        "coordination-dual-cohort-v1-v2-worker",
        minimum=1,
        maximum=2,
    )
    v1_v2_processed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        v1_v2_worker,
        "process_task",
        lambda task: v1_v2_processed.append((int(task.pk), int(task.execution_protocol_version))),
    )

    assert v1_v2_worker.claim_and_process_tasks(["default"], concurrency=2) == 2
    assert v1_v2_processed == [(queued_v2.pk, 2), (second_queued_v1.pk, 1)]
    assert v1_v2_worker.reconcile_tasks() == 1

    queued_v2.refresh_from_db()
    second_queued_v1.refresh_from_db()
    orphaned_v2.refresh_from_db()
    old_v2_owner.refresh_from_db()
    for claimed in (queued_v2, second_queued_v1):
        assert claimed.state == TaskState.RUNNING
        assert claimed.claimed_by_worker == v1_v2_worker.worker_id
        assert claimed.managed_with_django_ray_version == django_ray_version
    assert status_calls == ["raysubmit_coordination_dual_cohort_v2"]
    assert orphaned_v2.state == TaskState.RUNNING
    assert orphaned_v2.claimed_by_worker == v1_v2_worker.worker_id
    assert orphaned_v2.managed_with_django_ray_version == django_ray_version
    assert orphaned_v2.pk in v1_v2_worker.active_tasks
    assert old_v2_owner.is_active is False


def test_v1_and_v1_v2_workers_obey_their_exact_protocol_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_v1_and_v1_v2_workers_obey_their_exact_protocol_ranges(monkeypatch)


@pytest.mark.postgresql
def test_postgresql_v1_and_v1_v2_workers_obey_their_exact_protocol_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _assert_v1_and_v1_v2_workers_obey_their_exact_protocol_ranges(monkeypatch)


def _assert_worker_ownership_paths_leave_unsupported_inflight_rows_untouched() -> None:
    _close()
    stale_at = timezone.now() - timedelta(hours=1)
    old_owner = TaskWorkerLease.objects.create(
        worker_id="coordination-v2-old-owner",
        hostname="v2-host",
        pid=2001,
        capability_schema_version=1,
        django_ray_version="0.5.0",
        min_supported_execution_protocol_version=2,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        last_heartbeat_at=stale_at,
    )
    reconcile = RayTaskExecution.objects.create(
        task_id="coordination-v2-reconcile",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
        ray_job_id="raysubmit_coordination_v2_reconcile",
    )
    stuck = RayTaskExecution.objects.create(
        task_id="coordination-v2-stuck",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
    )
    cancelling = RayTaskExecution.objects.create(
        task_id="coordination-v2-cancelling",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.CANCELLING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
    )
    TaskWorkerLease.objects.filter(pk=old_owner.pk).update(
        is_active=False,
        stopped_at=timezone.now(),
    )
    worker = _explicit_v1_worker("coordination-v1-recovery-worker")

    assert worker.reconcile_tasks() == 0
    assert worker.detect_stuck_tasks() == 0
    assert worker.process_cancellations() == 0

    for row in (reconcile, stuck, cancelling):
        row.refresh_from_db()
        assert row.execution_protocol_version == 2
        assert row.claimed_by_worker == old_owner.worker_id
        assert row.finished_at is None
        assert not TaskAttempt.objects.filter(execution=row).exists()
    assert reconcile.state == TaskState.RUNNING
    assert stuck.state == TaskState.RUNNING
    assert cancelling.state == TaskState.CANCELLING
    assert worker.active_tasks == {}
    assert worker.shutdown_requested is False
    assert worker.lease_ownership_lost is False


def test_worker_ownership_paths_leave_unsupported_inflight_rows_untouched() -> None:
    _assert_worker_ownership_paths_leave_unsupported_inflight_rows_untouched()


@pytest.mark.postgresql
def test_postgresql_worker_ownership_paths_leave_unsupported_inflight_rows_untouched() -> None:
    _require_postgresql()
    _assert_worker_ownership_paths_leave_unsupported_inflight_rows_untouched()


def _assert_direct_lifecycle_paths_leave_unsupported_rows_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _close()
    now = timezone.now()
    queued = RayTaskExecution.objects.create(
        task_id="coordination-direct-v2-queued",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.QUEUED,
        queue_deadline_at=now - timedelta(seconds=1),
        attempt_number=3,
        execution_generation=7,
    )
    failed = RayTaskExecution.objects.create(
        task_id="coordination-direct-v2-failed",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.FAILED,
        attempt_number=3,
        execution_generation=7,
        error_message="retained failure",
    )
    running = RayTaskExecution.objects.create(
        task_id="coordination-direct-v2-running",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        attempt_number=3,
        execution_generation=7,
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=1),
    )
    cancelling = RayTaskExecution.objects.create(
        task_id="coordination-direct-v2-cancelling",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.CANCELLING,
        attempt_number=3,
        execution_generation=7,
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=1),
    )
    rows = (queued, failed, running, cancelling)
    before = {row.pk: RayTaskExecution.objects.filter(pk=row.pk).values().get() for row in rows}

    def unexpected_effect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unsupported lifecycle path reached hydration or archival")

    monkeypatch.setattr("django_ray.lifecycle.runtime_env_for_execution", unexpected_effect)
    monkeypatch.setattr("django_ray.lifecycle._record_attempt", unexpected_effect)

    queued_cancel = request_task_cancellation(
        queued.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
    )
    retry = request_task_retry(
        failed.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
    )
    running_cancel = request_task_cancellation(
        running.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
    )

    assert queued_cancel.status is TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL
    assert retry.status is TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL
    assert running_cancel.status is TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL
    assert expire_queued_tasks(["default"], now=now) == ()
    assert not record_failure(running, error_message="replacement", retry=True)
    assert not record_lost(running, error_message="replacement")
    assert not succeed_task(running, result_data="replacement", result_reference=None)
    assert not cancel_task(cancelling)

    for row in rows:
        assert RayTaskExecution.objects.filter(pk=row.pk).values().get() == before[row.pk]
        assert not TaskAttempt.objects.filter(execution=row).exists()


def test_direct_lifecycle_paths_leave_unsupported_rows_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_direct_lifecycle_paths_leave_unsupported_rows_untouched(monkeypatch)


@pytest.mark.postgresql
def test_postgresql_direct_lifecycle_paths_leave_unsupported_rows_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _assert_direct_lifecycle_paths_leave_unsupported_rows_untouched(monkeypatch)


def _assert_global_lifecycle_scans_exclude_unsupported_protocols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _close()
    stale_at = timezone.now() - timedelta(hours=1)
    old_owner = TaskWorkerLease.objects.create(
        worker_id="coordination-v2-scan-owner",
        hostname="v2-host",
        pid=2002,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=2,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        last_heartbeat_at=stale_at,
    )
    reconcile = RayTaskExecution.objects.create(
        task_id="coordination-v2-scan-reconcile",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
        ray_job_id="raysubmit_coordination_v2_scan",
        ray_address="ray://v2-cluster:10001",
    )
    stuck = RayTaskExecution.objects.create(
        task_id="coordination-v2-scan-stuck",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
        timeout_seconds=1,
    )
    cancelling = RayTaskExecution.objects.create(
        task_id="coordination-v2-scan-cancelling",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.CANCELLING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
        ray_job_id="raysubmit_coordination_v2_cancel",
        ray_address="ray://v2-cluster:10001",
    )
    worker = _explicit_v1_worker("coordination-v1-scan-worker")
    remote_calls: list[str] = []

    class RejectingRunner:
        def get_status(self, _handle: object) -> JobInfo:
            remote_calls.append("status")
            raise AssertionError("unsupported Ray Job must be filtered before status RPC")

        def get_logs(self, _handle: object) -> str | None:
            remote_calls.append("logs")
            raise AssertionError("unsupported Ray Job must be filtered before log RPC")

    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", RejectingRunner)
    monkeypatch.setattr(
        worker,
        "_request_timeout_cancellation",
        lambda _task: (_ for _ in ()).throw(
            AssertionError("unsupported timeout must be filtered before stop RPC")
        ),
    )
    monkeypatch.setattr(
        worker,
        "_request_cancellation_for_task",
        lambda _task: (_ for _ in ()).throw(
            AssertionError("unsupported cancellation must be filtered before stop RPC")
        ),
    )

    assert worker.reconcile_tasks() == 0
    assert worker.detect_stuck_tasks() == 0
    assert worker.process_cancellations() == 0

    assert remote_calls == []
    old_owner.refresh_from_db()
    assert old_owner.is_active is True
    for row, state in (
        (reconcile, TaskState.RUNNING),
        (stuck, TaskState.RUNNING),
        (cancelling, TaskState.CANCELLING),
    ):
        row.refresh_from_db()
        assert row.state == state
        assert row.claimed_by_worker == old_owner.worker_id
        assert row.finished_at is None
        assert not TaskAttempt.objects.filter(execution=row).exists()


def test_global_lifecycle_scans_exclude_unsupported_protocols_before_remote_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_global_lifecycle_scans_exclude_unsupported_protocols(monkeypatch)


@pytest.mark.postgresql
def test_postgresql_global_lifecycle_scans_exclude_unsupported_protocols_before_remote_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _assert_global_lifecycle_scans_exclude_unsupported_protocols(monkeypatch)


def test_unsupported_active_ray_job_tracking_is_retired_before_status_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _close()
    worker_id = "coordination-replaced-range-worker"
    original = _explicit_range_worker(worker_id, minimum=2, maximum=2)
    task = RayTaskExecution.objects.create(
        task_id="coordination-replaced-range-active",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=worker_id,
        started_at=timezone.now(),
        last_heartbeat_at=timezone.now(),
        ray_job_id="raysubmit_coordination_replaced_range",
        ray_address="ray://v2-cluster:10001",
        attempt_number=3,
        execution_generation=7,
        managed_with_django_ray_version="0.4.0-manager",
    )
    assert original.lease_identity is not None
    TaskWorkerLease.objects.filter(**original.lease_identity.database_filters()).delete()
    worker = _explicit_range_worker(worker_id, minimum=1, maximum=1)
    worker.active_tasks[task.pk] = str(task.ray_job_id)
    worker.active_task_identities[task.pk] = (
        int(task.attempt_number),
        int(task.execution_generation),
    )
    status_calls: list[str] = []

    class RejectingRunner:
        def get_status(self, _handle: object) -> JobInfo:
            status_calls.append("status")
            raise AssertionError("unsupported active tracking reached Ray")

    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", RejectingRunner)

    assert worker.reconcile_tasks() == 1

    task.refresh_from_db()
    assert status_calls == []
    assert worker.active_tasks == {}
    assert worker.active_task_identities == {}
    assert task.state == TaskState.RUNNING
    assert task.claimed_by_worker == worker_id
    assert task.managed_with_django_ray_version == "0.4.0-manager"
    assert task.finished_at is None
    assert worker.shutdown_requested is False


def test_global_reconciliation_uses_exact_locked_lease_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _close()
    stale_at = timezone.now() - timedelta(hours=1)
    old_owner = TaskWorkerLease.objects.create(
        worker_id="coordination-v2-compatible-owner",
        hostname="v2-host",
        pid=2003,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=2,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        last_heartbeat_at=stale_at,
    )
    task = RayTaskExecution.objects.create(
        task_id="coordination-v2-compatible-reconcile",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
        ray_job_id="raysubmit_coordination_v2_compatible",
        ray_address="ray://v2-cluster:10001",
        attempt_number=3,
        execution_generation=7,
        managed_with_django_ray_version="0.4.0-manager",
    )
    worker = _explicit_range_worker(
        "coordination-v2-compatible-reconciler",
        minimum=1,
        maximum=2,
    )
    status_calls: list[str] = []

    class PendingRunner:
        def get_status(self, handle: SubmissionHandle) -> JobInfo:
            status_calls.append(str(handle.ray_job_id))
            return JobInfo(
                job_id=str(handle.ray_job_id),
                status=JobStatus.PENDING,
            )

    monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", PendingRunner)

    assert worker.reconcile_tasks() == 1

    task.refresh_from_db()
    old_owner.refresh_from_db()
    assert status_calls == ["raysubmit_coordination_v2_compatible"]
    assert task.state == TaskState.RUNNING
    assert task.claimed_by_worker == worker.worker_id
    assert task.managed_with_django_ray_version == django_ray_version
    assert task.pk in worker.active_tasks
    assert old_owner.is_active is False


def _assert_protocol_mismatch_precedes_stale_source_lease_retirement() -> None:
    _close()
    stale_at = timezone.now() - timedelta(hours=1)
    old_owner = TaskWorkerLease.objects.create(
        worker_id="coordination-v2-central-owner",
        hostname="v2-host",
        pid=2004,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=2,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        last_heartbeat_at=stale_at,
    )
    task = RayTaskExecution.objects.create(
        task_id="coordination-v2-central-mismatch",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=old_owner.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
        managed_with_django_ray_version="0.4.0-manager",
    )
    worker = _explicit_v1_worker("coordination-v1-central-worker")

    assert worker._take_over_task_if_owner_stale(task, now=timezone.now()) is None

    old_owner.refresh_from_db()
    task.refresh_from_db()
    assert old_owner.is_active is True
    assert old_owner.stopped_at is None
    assert task.claimed_by_worker == old_owner.worker_id
    assert task.managed_with_django_ray_version == "0.4.0-manager"
    assert task.state == TaskState.RUNNING
    assert worker.shutdown_requested is False
    assert worker.lease_ownership_lost is False


def test_protocol_mismatch_precedes_stale_source_lease_retirement() -> None:
    _assert_protocol_mismatch_precedes_stale_source_lease_retirement()


@pytest.mark.postgresql
def test_postgresql_protocol_mismatch_precedes_stale_source_lease_retirement() -> None:
    _require_postgresql()
    _assert_protocol_mismatch_precedes_stale_source_lease_retirement()


def test_expired_candidate_is_lease_loss_before_protocol_mismatch() -> None:
    _close()
    stale_at = timezone.now() - timedelta(hours=1)
    source = TaskWorkerLease.objects.create(
        worker_id="coordination-v2-expired-candidate-source",
        hostname="v2-host",
        pid=2005,
        capability_schema_version=1,
        django_ray_version="0.5.0-test",
        min_supported_execution_protocol_version=2,
        max_supported_execution_protocol_version=2,
        legacy_admission_token=None,
        last_heartbeat_at=stale_at,
    )
    task = RayTaskExecution.objects.create(
        task_id="coordination-v2-expired-candidate",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.RUNNING,
        claimed_by_worker=source.worker_id,
        started_at=stale_at,
        last_heartbeat_at=stale_at,
    )
    worker = _explicit_v1_worker("coordination-v1-expired-candidate")
    assert worker.lease_identity is not None
    TaskWorkerLease.objects.filter(**worker.lease_identity.database_filters()).update(
        last_heartbeat_at=stale_at
    )

    assert worker._take_over_task_if_owner_stale(task, now=timezone.now()) is None

    source.refresh_from_db()
    task.refresh_from_db()
    assert worker.shutdown_requested is True
    assert worker.lease_ownership_lost is True
    assert source.is_active is True
    assert task.claimed_by_worker == source.worker_id


def test_close_detaches_inactive_legacy_rows_and_rejects_historical_writers() -> None:
    old_execution, old_lease = _historical_models()
    inactive = _legacy_lease("coordination-inactive-legacy", is_active=False)
    explicit = _explicit_v1_lease("coordination-explicit-v1")
    before = _policy()

    result = _close()

    assert result.enabled is False
    assert result.changed is True
    assert result.active_write_protocol_version == 1
    assert result.revision == int(before.revision) + 1
    assert result.detached_inactive_legacy_leases == 1
    assert not LegacyWorkerAdmissionToken.objects.exists()
    inactive.refresh_from_db()
    explicit.refresh_from_db()
    assert inactive.legacy_admission_token_id is None
    assert explicit.is_active is True

    _rejects_integrity_error(
        lambda: old_lease.objects.create(
            worker_id="coordination-closed-old-worker",
            hostname="old-host",
            pid=1003,
            is_active=True,
        )
    )
    _rejects_integrity_error(
        lambda: old_lease.objects.create(
            worker_id="coordination-closed-old-inactive-worker",
            hostname="old-host",
            pid=1005,
            is_active=False,
        )
    )
    _rejects_integrity_error(
        lambda: old_execution.objects.create(
            task_id="coordination-closed-old-execution",
            callable_path="testproject.tasks.add_numbers",
        )
    )

    unchanged = _close(result.revision)
    assert unchanged.enabled is False
    assert unchanged.changed is False
    assert unchanged.revision == result.revision
    assert unchanged.detached_inactive_legacy_leases == 0


@pytest.mark.parametrize("heartbeat_age", [timedelta(), timedelta(hours=2)])
def test_close_rolls_back_retirement_while_an_active_legacy_lease_remains(
    heartbeat_age: timedelta,
) -> None:
    inactive = _legacy_lease("coordination-blocked-inactive", is_active=False)
    active = _legacy_lease(
        f"coordination-blocking-active-{int(heartbeat_age.total_seconds())}",
        is_active=True,
    )
    TaskWorkerLease.objects.filter(pk=active.pk).update(
        last_heartbeat_at=timezone.now() - heartbeat_age
    )
    _explicit_v1_lease(f"coordination-nonblocking-v1-{int(heartbeat_age.total_seconds())}")

    with pytest.raises(LegacyWorkerAdmissionBlockedError) as captured:
        _close()

    assert captured.value.active_legacy_worker_count == 1
    policy = _policy()
    inactive.refresh_from_db()
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == 1
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()
    assert inactive.legacy_admission_token_id == 1


def test_reopen_requires_only_v1_nonterminal_work_and_does_not_revive_old_leases() -> None:
    old_execution, old_lease = _historical_models()
    inactive = _legacy_lease("coordination-retired-before-reopen", is_active=False)
    closed = _close()
    incompatible = [
        RayTaskExecution.objects.create(
            task_id=f"coordination-v2-blocker-{state.lower()}",
            callable_path="testproject.tasks.add_numbers",
            execution_protocol_version=2,
            state=state,
        )
        for state in (TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING)
    ]
    RayTaskExecution.objects.create(
        task_id="coordination-terminal-v2-control",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.SUCCEEDED,
    )

    with pytest.raises(LegacyWorkerRollbackBlockedError) as captured:
        _reopen(closed.revision)

    assert captured.value.incompatible_nonterminal_execution_count == 3
    assert _policy().revision == closed.revision
    assert not LegacyWorkerAdmissionToken.objects.exists()

    RayTaskExecution.objects.filter(pk__in=[row.pk for row in incompatible]).update(
        state=TaskState.SUCCEEDED
    )
    reopened = _reopen(closed.revision)

    assert reopened.enabled is True
    assert reopened.changed is True
    assert reopened.revision == closed.revision + 1
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()
    inactive.refresh_from_db()
    assert inactive.legacy_admission_token_id is None

    old_row = old_execution.objects.create(
        task_id="coordination-reopened-old-execution",
        callable_path="testproject.tasks.add_numbers",
    )
    old_worker = old_lease.objects.create(
        worker_id="coordination-reopened-old-worker",
        hostname="old-host",
        pid=1004,
        is_active=True,
    )
    migrated_row = RayTaskExecution.objects.get(pk=old_row.pk)
    migrated_worker = TaskWorkerLease.objects.get(pk=old_worker.pk)
    assert migrated_row.metadata_schema_version == 0
    assert migrated_row.execution_protocol_version == 1
    assert migrated_worker.capability_schema_version == 0
    assert migrated_worker.legacy_admission_token_id == 1

    unchanged = _reopen(reopened.revision)
    assert unchanged.enabled is True
    assert unchanged.changed is False
    assert unchanged.revision == reopened.revision


def test_policy_and_token_corruption_fail_closed() -> None:
    LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()
    with pytest.raises(ProtocolPolicyStateError, match="open but its token is missing"):
        _close()
    assert _policy().legacy_worker_admission_enabled is True

    LegacyWorkerAdmissionToken.objects.create(singleton_key=1)
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=False,
        revision=2,
    )
    with pytest.raises(ProtocolPolicyStateError, match="closed but its token still exists"):
        _close(2)


def test_reopen_rejects_a_non_v1_active_write_policy() -> None:
    _close()
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        active_write_protocol_version=2
    )

    with pytest.raises(ProtocolPolicyStateError, match="requires active write protocol v1"):
        _reopen()

    assert not LegacyWorkerAdmissionToken.objects.exists()
    assert _policy().legacy_worker_admission_enabled is False


def test_missing_policy_fails_before_any_transition() -> None:
    TaskExecutionProtocolPolicy.objects.get(singleton_key=1).delete()

    with pytest.raises(ProtocolPolicyStateError, match="policy singleton is unavailable"):
        _close()

    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


@pytest.mark.parametrize("retirement_assertion", [False, 0, 1, "true", object()])
def test_close_requires_an_exact_legacy_producer_retirement_assertion(
    retirement_assertion: object,
) -> None:
    before = _policy()

    with pytest.raises(LegacyProducerRetirementRequiredError):
        close_legacy_worker_admission(
            expected_revision=int(before.revision),
            legacy_producers_retired=cast(Any, retirement_assertion),
        )

    after = _policy()
    assert after.legacy_worker_admission_enabled is True
    assert after.revision == before.revision
    assert after.updated_at == before.updated_at
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


@pytest.mark.parametrize(
    "invalid_revision",
    [
        True,
        False,
        0,
        -1,
        1.0,
        "1",
        None,
        pytest.param(1 << 63, id="above-positive-bigint"),
        pytest.param(1 << 20_000, id="unbounded-python-int"),
    ],
)
def test_transition_revision_requires_an_exact_positive_integer(
    invalid_revision: object,
) -> None:
    before = _policy()
    value = cast(Any, invalid_revision)

    with pytest.raises(InvalidProtocolRevisionError, match="positive bigint range"):
        close_legacy_worker_admission(
            expected_revision=value,
            legacy_producers_retired=True,
        )
    with pytest.raises(InvalidProtocolRevisionError, match="positive bigint range"):
        reopen_legacy_worker_admission(expected_revision=value)

    after = _policy()
    assert after.legacy_worker_admission_enabled is True
    assert after.revision == before.revision
    assert after.updated_at == before.updated_at


@pytest.mark.parametrize("transition_state", ["open", "closed"])
def test_revision_exhaustion_refuses_a_changing_transition(transition_state: str) -> None:
    maximum_revision = (1 << 63) - 1
    if transition_state == "closed":
        LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()
        TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
            legacy_worker_admission_enabled=False,
            revision=maximum_revision,
        )
        with pytest.raises(ProtocolRevisionExhaustedError, match="exhausted"):
            _reopen(maximum_revision)
    else:
        TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
            revision=maximum_revision
        )
        with pytest.raises(ProtocolRevisionExhaustedError, match="exhausted"):
            _close(maximum_revision)

    policy = _policy()
    assert policy.revision == maximum_revision
    assert policy.legacy_worker_admission_enabled is (transition_state == "open")
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists() is (
        transition_state == "open"
    )


def test_unsupported_database_and_policy_schema_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_connection = connections["default"]
    original_vendor = database_connection.vendor
    monkeypatch.setattr(database_connection, "vendor", "mysql")
    with pytest.raises(UnsupportedProtocolDatabaseError, match="only SQLite and PostgreSQL"):
        _close()

    monkeypatch.setattr(database_connection, "vendor", original_vendor)
    policy = _policy()
    policy.schema_version = 2
    with pytest.raises(ProtocolPolicyStateError, match="policy schema is unsupported"):
        protocol_coordination._validate_policy(policy)


def test_transition_revision_conflicts_and_idempotency_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _policy()
    closed_at = cast(datetime, initial.updated_at) + timedelta(seconds=1)
    monkeypatch.setattr(protocol_coordination.timezone, "now", lambda: closed_at)
    closed = _close(int(initial.revision))

    assert closed.previous_revision == int(initial.revision)
    assert closed.revision == int(initial.revision) + 1
    assert closed.updated_at == closed_at
    with pytest.raises(ProtocolRevisionConflictError) as stale_close:
        _close(int(initial.revision))
    assert stale_close.value.expected_revision == int(initial.revision)
    assert stale_close.value.actual_revision == closed.revision

    closed_again = _close(closed.revision)
    assert closed_again.changed is False
    assert closed_again.revision == closed.revision
    assert closed_again.updated_at == closed.updated_at

    with pytest.raises(ProtocolRevisionConflictError):
        _reopen(int(initial.revision))
    reopened_at = closed_at + timedelta(seconds=1)
    monkeypatch.setattr(protocol_coordination.timezone, "now", lambda: reopened_at)
    reopened = _reopen(closed.revision)
    assert reopened.previous_revision == closed.revision
    assert reopened.revision == closed.revision + 1
    assert reopened.updated_at == reopened_at

    with pytest.raises(ProtocolRevisionConflictError):
        _reopen(closed.revision)
    reopened_again = _reopen(reopened.revision)
    assert reopened_again.changed is False
    assert reopened_again.revision == reopened.revision
    assert reopened_again.updated_at == reopened.updated_at


def test_reopen_rechecks_rollback_safety_when_admission_is_already_open() -> None:
    closed = _close()
    RayTaskExecution.objects.create(
        task_id="coordination-open-v2-blocker",
        callable_path="testproject.tasks.add_numbers",
        execution_protocol_version=2,
        state=TaskState.QUEUED,
    )
    LegacyWorkerAdmissionToken.objects.create(singleton_key=1)
    corrupted_open_revision = closed.revision + 1
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=True,
        revision=corrupted_open_revision,
    )

    with pytest.raises(LegacyWorkerRollbackBlockedError) as captured:
        _reopen(corrupted_open_revision)

    assert captured.value.incompatible_nonterminal_execution_count == 1
    policy = _policy()
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == corrupted_open_revision
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


@pytest.mark.parametrize("transition", [_close, _reopen])
def test_transition_refuses_a_caller_owned_outer_transaction(
    transition: Callable[[], object],
) -> None:
    with transaction.atomic():
        with pytest.raises(NestedProtocolTransitionError, match="outermost"):
            transition()

    policy = _policy()
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == 1


def test_transition_refuses_manually_disabled_autocommit() -> None:
    transaction.set_autocommit(False)
    try:
        with pytest.raises(NestedProtocolTransitionError, match="outermost"):
            _close()
    finally:
        transaction.rollback()
        transaction.set_autocommit(True)

    policy = _policy()
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == 1


def test_missing_token_during_postgresql_lock_is_a_bounded_race_error() -> None:
    LegacyWorkerAdmissionToken.objects.get(singleton_key=1).delete()

    with transaction.atomic():
        with pytest.raises(LegacyAdmissionRaceError, match="disappeared"):
            protocol_coordination._postgresql_lock_legacy_token(using="default")


def test_postgresql_coordination_helpers_emit_exact_bounded_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[tuple[str, object | None]] = []

    class RecordingCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def execute(self, sql: str, params: object | None = None) -> None:
            statements.append((sql, params))

    database_connection = connections["default"]
    monkeypatch.setattr(database_connection, "cursor", lambda: RecordingCursor())

    protocol_coordination._postgresql_lock_transition(using="default")
    protocol_coordination._postgresql_lock_execution_writers(using="default")

    assert statements == [
        (
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [
                protocol_coordination._POSTGRESQL_COORDINATION_LOCK_NAMESPACE,
                protocol_coordination._POSTGRESQL_COORDINATION_LOCK_KEY,
            ],
        ),
        (
            'LOCK TABLE "django_ray_raytaskexecution" IN SHARE ROW EXCLUSIVE MODE',
            None,
        ),
    ]


def test_postgresql_policy_helpers_translate_a_missing_singleton() -> None:
    with transaction.atomic():
        assert protocol_coordination._postgresql_lock_policy(using="default").singleton_key == 1
    assert protocol_coordination._postgresql_legacy_admission_is_open(using="default") is True

    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=False
    )
    assert protocol_coordination._postgresql_legacy_admission_is_open(using="default") is False
    TaskExecutionProtocolPolicy.objects.get(singleton_key=1).delete()

    with transaction.atomic():
        with pytest.raises(ProtocolPolicyStateError, match="policy singleton is unavailable"):
            protocol_coordination._postgresql_lock_policy(using="default")
    with pytest.raises(ProtocolPolicyStateError, match="policy singleton is unavailable"):
        protocol_coordination._postgresql_legacy_admission_is_open(using="default")


def test_reopen_token_creation_collision_rolls_back_as_a_bounded_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = _close()

    def collide(*args, **kwargs) -> None:
        del args, kwargs
        raise IntegrityError("injected token collision")

    monkeypatch.setattr(LegacyWorkerAdmissionToken, "save", collide)
    with pytest.raises(LegacyAdmissionRaceError, match="appeared"):
        _reopen(closed.revision)

    policy = _policy()
    assert policy.legacy_worker_admission_enabled is False
    assert policy.revision == closed.revision
    assert not LegacyWorkerAdmissionToken.objects.exists()


def test_sqlite_close_takes_the_write_fence_before_reading_legacy_state() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("SQLite query-order contract")

    with CaptureQueriesContext(connection) as queries:
        _close()

    statements = [
        query["sql"]
        for query in queries.captured_queries
        if not query["sql"].upper().startswith(("BEGIN", "COMMIT", "SAVEPOINT", "RELEASE"))
    ]
    assert statements
    assert statements[0].startswith('UPDATE "django_ray_taskexecutionprotocolpolicy"')
    legacy_reads = [
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT") and "django_ray_taskworkerlease" in statement
    ]
    assert legacy_reads
    assert min(legacy_reads) > 0


def test_close_failure_after_legacy_detachment_rolls_back_every_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inactive = _legacy_lease("coordination-close-failure-inactive", is_active=False)

    def fail_policy_save(*args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("injected close failure")

    monkeypatch.setattr(TaskExecutionProtocolPolicy, "save", fail_policy_save)
    with pytest.raises(RuntimeError, match="injected close failure"):
        _close()

    inactive.refresh_from_db()
    policy = _policy()
    assert inactive.legacy_admission_token_id == 1
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == 1
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


def test_reopen_failure_after_token_creation_rolls_back_every_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = _close()

    def fail_policy_save(*args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("injected reopen failure")

    monkeypatch.setattr(TaskExecutionProtocolPolicy, "save", fail_policy_save)
    with pytest.raises(RuntimeError, match="injected reopen failure"):
        _reopen(closed.revision)

    policy = _policy()
    assert policy.legacy_worker_admission_enabled is False
    assert policy.revision == closed.revision
    assert not LegacyWorkerAdmissionToken.objects.exists()


@pytest.mark.parametrize("delete_outcome", ["protected", "zero"])
def test_close_token_delete_anomalies_roll_back_every_change(
    monkeypatch: pytest.MonkeyPatch,
    delete_outcome: str,
) -> None:
    inactive = _legacy_lease(
        f"coordination-token-delete-{delete_outcome}",
        is_active=False,
    )

    def anomalous_delete(*args, **kwargs):
        del args, kwargs
        if delete_outcome == "protected":
            raise ProtectedError("injected token reference", {inactive})
        return (0, {})

    monkeypatch.setattr(LegacyWorkerAdmissionToken, "delete", anomalous_delete)
    error = LegacyAdmissionRaceError if delete_outcome == "protected" else ProtocolPolicyStateError
    with pytest.raises(error):
        _close()

    inactive.refresh_from_db()
    policy = _policy()
    assert inactive.legacy_admission_token_id == 1
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == 1
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def _postgresql_backend_pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


def _wait_for_postgresql_lock(backend_pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT wait_event_type
                FROM pg_stat_activity
                WHERE pid = %s
                """,
                [backend_pid],
            )
            row = cursor.fetchone()
        if row is not None and row[0] == "Lock":
            return
        time.sleep(0.01)
    raise AssertionError("the PostgreSQL backend did not enter a lock wait")


@pytest.mark.postgresql
def test_postgresql_historical_insert_first_blocks_closure() -> None:
    _require_postgresql()
    _, old_lease = _historical_models()
    inserted = Event()
    release_insert = Event()
    close_started = Event()
    close_backend_pid: list[int] = []

    def insert_first() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                old_lease.objects.create(
                    worker_id="postgres-insert-first-legacy",
                    hostname="old-host",
                    pid=2001,
                    is_active=True,
                )
                inserted.set()
                if not release_insert.wait(timeout=10):
                    raise TimeoutError("test did not release the historical insert")
        finally:
            close_old_connections()

    def close_after_insert() -> int:
        close_old_connections()
        try:
            close_backend_pid.append(_postgresql_backend_pid())
            close_started.set()
            try:
                _close()
            except LegacyWorkerAdmissionBlockedError as error:
                return error.active_legacy_worker_count
            raise AssertionError("closure unexpectedly passed an active legacy lease")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        insertion = executor.submit(insert_first)
        assert inserted.wait(timeout=10)
        closure = executor.submit(close_after_insert)
        assert close_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(close_backend_pid[0])
        finally:
            release_insert.set()
        insertion.result(timeout=20)
        assert closure.result(timeout=20) == 1

    assert _policy().legacy_worker_admission_enabled is True
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()
    assert TaskWorkerLease.objects.filter(worker_id="postgres-insert-first-legacy").exists()


@pytest.mark.postgresql
def test_postgresql_inactive_historical_insert_during_closure_rolls_back_as_a_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _, old_lease = _historical_models()
    initial_scan_complete = Event()
    insertion_complete = Event()
    original_lock = protocol_coordination._postgresql_lock_policy

    def insert_first() -> None:
        close_old_connections()
        try:
            if not initial_scan_complete.wait(timeout=10):
                raise TimeoutError("closure did not finish its initial legacy-lease scan")
            old_lease.objects.create(
                worker_id="postgres-inactive-insert-during-close",
                hostname="old-host",
                pid=2011,
                is_active=False,
            )
            insertion_complete.set()
        finally:
            close_old_connections()

    def wait_for_inactive_insert(*, using: str) -> TaskExecutionProtocolPolicy:
        initial_scan_complete.set()
        if not insertion_complete.wait(timeout=10):
            raise TimeoutError("test did not commit the inactive historical insert")
        return original_lock(using=using)

    monkeypatch.setattr(
        protocol_coordination,
        "_postgresql_lock_policy",
        wait_for_inactive_insert,
    )

    def close_during_insert() -> str:
        close_old_connections()
        try:
            try:
                _close()
            except LegacyAdmissionRaceError:
                return "race"
            raise AssertionError("closure did not detect the inactive historical insert")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        insertion = executor.submit(insert_first)
        closure = executor.submit(close_during_insert)
        insertion.result(timeout=20)
        assert closure.result(timeout=20) == "race"

    policy = _policy()
    lease = TaskWorkerLease.objects.get(worker_id="postgres-inactive-insert-during-close")
    assert policy.legacy_worker_admission_enabled is True
    assert policy.revision == 1
    assert lease.is_active is False
    assert lease.legacy_admission_token_id == 1
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


@pytest.mark.postgresql
def test_postgresql_historical_execution_insert_first_linearizes_before_closure() -> None:
    _require_postgresql()
    old_execution, _ = _historical_models()
    inserted = Event()
    release_insert = Event()
    close_started = Event()
    close_backend_pid: list[int] = []
    inserted_pk: list[int] = []

    def insert_first() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                row = old_execution.objects.create(
                    task_id="postgres-insert-first-execution",
                    callable_path="testproject.tasks.add_numbers",
                )
                inserted_pk.append(int(row.pk))
                inserted.set()
                if not release_insert.wait(timeout=10):
                    raise TimeoutError("test did not release the historical execution insert")
        finally:
            close_old_connections()

    def close_after_insert():
        close_old_connections()
        try:
            close_backend_pid.append(_postgresql_backend_pid())
            close_started.set()
            return _close()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        insertion = executor.submit(insert_first)
        assert inserted.wait(timeout=10)
        closure = executor.submit(close_after_insert)
        assert close_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(close_backend_pid[0])
        finally:
            release_insert.set()
        insertion.result(timeout=20)
        result = closure.result(timeout=20)

    row = RayTaskExecution.objects.get(pk=inserted_pk[0])
    assert result.changed is True
    assert result.enabled is False
    assert row.metadata_schema_version == 0
    assert row.execution_protocol_version == 1


@pytest.mark.postgresql
def test_postgresql_closure_first_rejects_historical_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    old_execution, old_lease = _historical_models()
    policy_locked = Event()
    release_closure = Event()
    lease_insert_started = Event()
    execution_insert_started = Event()
    lease_insert_backend_pid: list[int] = []
    execution_insert_backend_pid: list[int] = []
    original_lock = protocol_coordination._postgresql_lock_policy

    def hold_policy_lock(*, using: str) -> TaskExecutionProtocolPolicy:
        policy = original_lock(using=using)
        policy_locked.set()
        if not release_closure.wait(timeout=10):
            raise TimeoutError("test did not release the policy transition")
        return policy

    monkeypatch.setattr(protocol_coordination, "_postgresql_lock_policy", hold_policy_lock)

    def close_first():
        close_old_connections()
        try:
            return _close()
        finally:
            close_old_connections()

    def insert_after_lock() -> str:
        close_old_connections()
        try:
            lease_insert_backend_pid.append(_postgresql_backend_pid())
            lease_insert_started.set()
            try:
                old_lease.objects.create(
                    worker_id="postgres-close-first-legacy",
                    hostname="old-host",
                    pid=2002,
                    is_active=True,
                )
            except IntegrityError:
                return "rejected"
            return "inserted"
        finally:
            close_old_connections()

    def insert_execution_after_lock() -> str:
        close_old_connections()
        try:
            execution_insert_backend_pid.append(_postgresql_backend_pid())
            execution_insert_started.set()
            try:
                old_execution.objects.create(
                    task_id="postgres-close-first-execution",
                    callable_path="testproject.tasks.add_numbers",
                )
            except IntegrityError:
                return "rejected"
            return "inserted"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as executor:
        closure = executor.submit(close_first)
        assert policy_locked.wait(timeout=10)
        lease_insertion = executor.submit(insert_after_lock)
        execution_insertion = executor.submit(insert_execution_after_lock)
        assert lease_insert_started.wait(timeout=10)
        assert execution_insert_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(lease_insert_backend_pid[0])
            _wait_for_postgresql_lock(execution_insert_backend_pid[0])
        finally:
            release_closure.set()
        result = closure.result(timeout=20)
        assert lease_insertion.result(timeout=20) == "rejected"
        assert execution_insertion.result(timeout=20) == "rejected"

    assert result.changed is True
    assert result.enabled is False
    assert not LegacyWorkerAdmissionToken.objects.exists()
    assert not TaskWorkerLease.objects.filter(worker_id="postgres-close-first-legacy").exists()
    assert not RayTaskExecution.objects.filter(task_id="postgres-close-first-execution").exists()


@pytest.mark.postgresql
def test_postgresql_legacy_heartbeat_cannot_deadlock_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_postgresql()
    _, old_lease = _historical_models()
    legacy = old_lease.objects.create(
        worker_id="postgres-heartbeat-legacy",
        hostname="old-host",
        pid=2003,
        is_active=True,
    )
    policy_locked = Event()
    release_closure = Event()
    heartbeat_started = Event()
    heartbeat_backend_pid: list[int] = []
    original_lock = protocol_coordination._postgresql_lock_policy

    def hold_policy_lock(*, using: str) -> TaskExecutionProtocolPolicy:
        policy = original_lock(using=using)
        policy_locked.set()
        if not release_closure.wait(timeout=10):
            raise TimeoutError("test did not release the policy transition")
        return policy

    monkeypatch.setattr(protocol_coordination, "_postgresql_lock_policy", hold_policy_lock)

    def blocked_closure() -> int:
        close_old_connections()
        try:
            try:
                _close()
            except LegacyWorkerAdmissionBlockedError as error:
                return error.active_legacy_worker_count
            raise AssertionError("closure unexpectedly passed an active legacy lease")
        finally:
            close_old_connections()

    def heartbeat_after_lock() -> int:
        close_old_connections()
        try:
            heartbeat_backend_pid.append(_postgresql_backend_pid())
            heartbeat_started.set()
            return old_lease.objects.filter(pk=legacy.pk, is_active=True).update(
                last_heartbeat_at=timezone.now()
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        closure = executor.submit(blocked_closure)
        assert policy_locked.wait(timeout=10)
        heartbeat = executor.submit(heartbeat_after_lock)
        assert heartbeat_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(heartbeat_backend_pid[0])
        finally:
            release_closure.set()
        assert closure.result(timeout=20) == 1
        assert heartbeat.result(timeout=20) == 1

    assert _policy().legacy_worker_admission_enabled is True
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


@pytest.mark.postgresql
def test_postgresql_legacy_heartbeat_first_blocks_closure_without_deadlock() -> None:
    _require_postgresql()
    _, old_lease = _historical_models()
    legacy = old_lease.objects.create(
        worker_id="postgres-heartbeat-first-legacy",
        hostname="old-host",
        pid=2012,
        is_active=True,
    )
    heartbeat_updated = Event()
    release_heartbeat = Event()
    close_started = Event()
    close_backend_pid: list[int] = []

    def heartbeat_first() -> int:
        close_old_connections()
        try:
            with transaction.atomic():
                updated = old_lease.objects.filter(pk=legacy.pk, is_active=True).update(
                    last_heartbeat_at=timezone.now()
                )
                heartbeat_updated.set()
                if not release_heartbeat.wait(timeout=10):
                    raise TimeoutError("test did not release the legacy heartbeat")
                return updated
        finally:
            close_old_connections()

    def close_after_heartbeat() -> int:
        close_old_connections()
        try:
            close_backend_pid.append(_postgresql_backend_pid())
            close_started.set()
            try:
                _close()
            except LegacyWorkerAdmissionBlockedError as error:
                return error.active_legacy_worker_count
            raise AssertionError("closure unexpectedly passed an active legacy heartbeat")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        heartbeat = executor.submit(heartbeat_first)
        assert heartbeat_updated.wait(timeout=10)
        closure = executor.submit(close_after_heartbeat)
        assert close_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(close_backend_pid[0])
        finally:
            release_heartbeat.set()
        assert heartbeat.result(timeout=20) == 1
        assert closure.result(timeout=20) == 1

    assert _policy().legacy_worker_admission_enabled is True
    assert LegacyWorkerAdmissionToken.objects.filter(singleton_key=1).exists()


@pytest.mark.postgresql
def test_postgresql_close_and_reopen_restore_historical_writer_admission() -> None:
    _require_postgresql()
    old_execution, old_lease = _historical_models()

    closed = _close()
    reopened = _reopen(closed.revision)

    old_row = old_execution.objects.create(
        task_id="postgres-reopened-historical-execution",
        callable_path="testproject.tasks.add_numbers",
    )
    active = old_lease.objects.create(
        worker_id="postgres-reopened-active-legacy",
        hostname="old-host",
        pid=2013,
        is_active=True,
    )
    inactive = old_lease.objects.create(
        worker_id="postgres-reopened-inactive-legacy",
        hostname="old-host",
        pid=2014,
        is_active=False,
    )
    heartbeat_count = old_lease.objects.filter(pk=active.pk, is_active=True).update(
        last_heartbeat_at=timezone.now()
    )

    migrated_row = RayTaskExecution.objects.get(pk=old_row.pk)
    active_lease = TaskWorkerLease.objects.get(pk=active.pk)
    inactive_lease = TaskWorkerLease.objects.get(pk=inactive.pk)
    assert reopened.changed is True
    assert reopened.enabled is True
    assert reopened.revision == closed.revision + 1
    assert heartbeat_count == 1
    assert migrated_row.metadata_schema_version == 0
    assert migrated_row.execution_protocol_version == 1
    assert active_lease.capability_schema_version == 0
    assert active_lease.legacy_admission_token_id == 1
    assert inactive_lease.capability_schema_version == 0
    assert inactive_lease.legacy_admission_token_id == 1


@pytest.mark.postgresql
def test_postgresql_idempotent_reopen_does_not_deadlock_an_admitted_legacy_worker() -> None:
    _require_postgresql()
    old_execution, old_lease = _historical_models()
    legacy = old_lease.objects.create(
        worker_id="postgres-idempotent-reopen-legacy",
        hostname="old-host",
        pid=2015,
        is_active=True,
    )
    execution = old_execution.objects.create(
        task_id="postgres-idempotent-reopen-execution",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.QUEUED,
    )
    worker_holds_policy_and_execution = Event()
    release_worker_update = Event()
    reopen_started = Event()
    reopen_backend_pid: list[int] = []

    def legacy_worker_transaction() -> int:
        close_old_connections()
        try:
            with transaction.atomic():
                assert (
                    old_lease.objects.filter(pk=legacy.pk, is_active=True).update(
                        last_heartbeat_at=timezone.now()
                    )
                    == 1
                )
                old_execution.objects.select_for_update().get(pk=execution.pk)
                worker_holds_policy_and_execution.set()
                if not release_worker_update.wait(timeout=10):
                    raise TimeoutError("test did not release the admitted legacy worker")
                return old_execution.objects.filter(pk=execution.pk).update(
                    error_message="legacy worker update completed"
                )
        finally:
            close_old_connections()

    def idempotent_reopen():
        close_old_connections()
        try:
            reopen_backend_pid.append(_postgresql_backend_pid())
            reopen_started.set()
            return _reopen(1)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(legacy_worker_transaction)
        assert worker_holds_policy_and_execution.wait(timeout=10)
        reopening = executor.submit(idempotent_reopen)
        assert reopen_started.wait(timeout=10)
        try:
            _wait_for_postgresql_lock(reopen_backend_pid[0])
        finally:
            release_worker_update.set()
        assert worker.result(timeout=20) == 1
        reopened = reopening.result(timeout=20)

    assert reopened.changed is False
    assert reopened.enabled is True
    current = RayTaskExecution.objects.get(pk=execution.pk)
    assert current.error_message == "legacy worker update completed"


@pytest.mark.postgresql
def test_postgresql_concurrent_closers_apply_one_expected_revision() -> None:
    _require_postgresql()
    start = Barrier(2)

    def close_once() -> tuple[str, int]:
        close_old_connections()
        try:
            start.wait(timeout=10)
            try:
                result = _close(1)
            except ProtocolRevisionConflictError as error:
                return ("conflict", error.actual_revision)
            return ("changed", result.revision)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=20)
            for future in [
                executor.submit(close_once),
                executor.submit(close_once),
            ]
        ]

    assert sorted(outcomes) == [("changed", 2), ("conflict", 2)]
    policy = _policy()
    assert policy.legacy_worker_admission_enabled is False
    assert policy.revision == 2
    assert not LegacyWorkerAdmissionToken.objects.exists()
