"""PostgreSQL-only tests for workflow progress publication, execution lists, and input aliases."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

import django_ray.admin as django_ray_admin
import django_ray.workflow.progress.runs as workflow_progress_module
from django_ray.input_storage import (
    EXTERNAL_INPUT_PLACEHOLDER,
    load_task_input,
    prepare_task_input,
    register_task_input,
)
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    _request_task_cancellation,
    _request_task_retry,
    record_failure,
    request_task_cancellation,
    retry_task,
    succeed_task,
)
from django_ray.management.commands.django_ray_purge_inputs import Command as PurgeInputsCommand
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskAttempt,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressRunStorage,
)
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.runner.cancellation import (
    CancellationOutcome,
    finalize_cancellation,
)
from django_ray.runner.leasing import WorkerLeaseIdentity
from django_ray.runner.reconciliation import mark_task_lost, mark_task_timed_out
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.runs import (
    WorkflowProgressDiagnosticCode,
    persist_workflow_progress,
    persist_workflow_progress_summary,
    read_workflow_progress,
)
from django_ray.workflow.progress.summary import (
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)
from testproject import api as testproject_api
from tests.integration._postgresql_coordination_helpers import (
    _SYNTHETIC_V1_V2_PROTOCOLS,
    _allocate_workflow_identity,
    _execution,
    _execution_select_projections,
    _prepared_cloud_alias_payload,
    _prepared_rq2_request,
    _rq2_reserved_execution,
    _run_concurrently,
    _wait_for_postgresql_lock,
    _workflow_snapshot,
)
from tests.workflow_progress_summary_helpers import workflow_progress_summary

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    """Keep the default SQLite suite fast while making this gate explicit."""
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


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
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
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
        ray_job_id="raysubmit_postgres-projected-retry",
        ray_job_request_reference=("inputfs://sha256/postgres-projected-request?bytes=131072"),
        error_message="retryable failure",
        error_traceback="RetryError: retryable failure",
        execution_protocol_version=2,
        created_with_django_ray_version="0.4.0-creator",
        managed_with_django_ray_version="0.5.0-manager",
        executor_django_ray_version="0.5.0-executor",
    )

    with CaptureQueriesContext(connection) as retry_queries:
        _result, retried = _request_task_retry(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=7,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )

    assert retried is not None
    retry_projections = _execution_select_projections(retry_queries)
    assert [fields for fields, _sql in retry_projections] == [
        {
            "id",
            "state",
            "attempt_number",
            "execution_generation",
            "execution_protocol_version",
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
            "execution_protocol_version",
            "managed_with_django_ray_version",
            "executor_django_ray_version",
        },
    ]
    assert "FOR UPDATE" in retry_projections[0][1].upper()
    forbidden_payload_columns = {
        "args_json",
        "kwargs_json",
        "input_reference",
        "ray_job_request_reference",
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
    assert task.ray_job_id is None
    assert task.ray_job_request_reference is None
    assert task.result_data is None
    assert task.result_reference is None
    assert task.execution_protocol_version == 2
    assert task.created_with_django_ray_version == "0.4.0-creator"
    assert task.managed_with_django_ray_version is None
    assert task.executor_django_ray_version is None
    archived = TaskAttempt.objects.get(execution=task, attempt_number=3)
    assert archived.result_data == required_result
    assert archived.result_reference == result_reference
    assert archived.error_message == "retryable failure"
    assert archived.error_traceback == "RetryError: retryable failure"
    assert archived.execution_protocol_version == 2
    assert archived.managed_with_django_ray_version == "0.5.0-manager"
    assert archived.executor_django_ray_version == "0.5.0-executor"
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
        execution_protocol_version=2,
        managed_with_django_ray_version="0.5.0-manager",
        executor_django_ray_version=None,
        runtime_env_json=unrelated_marker,
        progress_data=unrelated_marker,
        workflow_plan_json=unrelated_marker,
        completion_data=unrelated_marker,
        cancellation_error=unrelated_marker,
    )

    with CaptureQueriesContext(connection) as queued_cancellation_queries:
        queued_cancellation = _request_task_cancellation(
            queued.pk,
            expected_attempt_number=4,
            expected_execution_generation=9,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )

    assert queued_cancellation.status is TaskCancellationRequestStatus.ACCEPTED
    queued_cancellation_projections = _execution_select_projections(queued_cancellation_queries)
    assert [fields for fields, _sql in queued_cancellation_projections] == [
        {
            "id",
            "state",
            "attempt_number",
            "execution_generation",
            "execution_protocol_version",
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
            "execution_protocol_version",
            "managed_with_django_ray_version",
            "executor_django_ray_version",
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
    assert queued_attempt.execution_protocol_version == 2
    assert queued_attempt.managed_with_django_ray_version == "0.5.0-manager"
    assert queued_attempt.executor_django_ray_version is None

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
            "execution_protocol_version",
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


@pytest.mark.parametrize("backend", ["s3", "gcs"])
@pytest.mark.parametrize("writer_uses_legacy", [False, True])
def test_physical_input_alias_pair_locks_other_row_and_refuses_without_mutation(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    writer_uses_legacy: bool,
) -> None:
    """Both existing-row directions wait for the alias and preserve its keeper."""
    import django_ray.input_storage as input_storage_module

    case = _prepared_cloud_alias_payload(monkeypatch, backend)
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, **case.config}
    old_timestamp = timezone.now() - timedelta(days=60)
    cutoff = timezone.now() - timedelta(days=30)
    references = (case.reference, case.legacy_native)
    writer_reference = case.legacy_native if writer_uses_legacy else case.reference
    for index, reference in enumerate(references):
        TaskInputPayload.objects.create(
            reference=reference,
            payload_kind=InputPayloadKind.TASK_INPUT,
            backend=case.prepared.backend,
            digest=case.prepared.digest,
            size_bytes=case.prepared.size_bytes,
            envelope_version=case.prepared.envelope_version,
            state=InputPayloadState.ACTIVE,
        )
        _execution(
            f"postgres-physical-alias-keeper-{index}",
            state=TaskState.QUEUED if reference == writer_reference else TaskState.SUCCEEDED,
            finished_at=None if reference == writer_reference else old_timestamp,
            input_reference=reference,
            args_json=case.prepared.args_json,
            kwargs_json=case.prepared.kwargs_json,
        )
    TaskInputPayload.objects.filter(reference__in=references).update(last_used_at=old_timestamp)
    registry_before = list(TaskInputPayload.objects.order_by("reference").values())
    executions_before = list(RayTaskExecution.objects.order_by("pk").values())
    objects_before = dict(case.objects)
    writes_before = list(case.writes)
    deletes: list[str] = []
    monkeypatch.setattr(
        input_storage_module, "delete_input_reference", lambda reference: deletes.append(reference)
    )
    prepared = (
        replace(case.prepared, input_reference=case.legacy_native)
        if writer_uses_legacy
        else case.prepared
    )
    other_reference = case.reference if writer_uses_legacy else case.legacy_native
    writer_connected = Event()
    writer_pid: list[int] = []

    def reenqueue() -> None:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.append(int(cursor.fetchone()[0]))
            writer_connected.set()
            with pytest.raises(
                input_storage_module.InputPayloadValidationError,
                match="another physical registry reference",
            ):
                with transaction.atomic():
                    register_task_input(prepared, case.config)
                    _execution(
                        "postgres-physical-alias-forbidden-new",
                        input_reference=prepared.input_reference,
                    )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            TaskInputPayload.objects.select_for_update().get(reference=other_reference)
            writer_future = executor.submit(reenqueue)
            assert writer_connected.wait(timeout=10)
            _wait_for_postgresql_lock(writer_pid[0])
        writer_future.result(timeout=20)

    for reference in references:
        assert (
            PurgeInputsCommand()._process_reference(reference, cutoff=cutoff, delete=True)
            == "incomplete"
        )
    assert list(TaskInputPayload.objects.order_by("reference").values()) == registry_before
    assert list(RayTaskExecution.objects.order_by("pk").values()) == executions_before
    assert case.objects == objects_before
    assert case.writes == writes_before
    assert deletes == []


@pytest.mark.parametrize("backend", ["s3", "gcs"])
@pytest.mark.parametrize(
    "payload_kind", [InputPayloadKind.TASK_INPUT, InputPayloadKind.RAY_JOB_REQUEST]
)
def test_canonical_payload_writer_waits_for_legacy_physical_alias_purge_then_refuses(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    payload_kind: str,
) -> None:
    """A raw native tombstone blocks new native and rq2 canonical identities."""
    import django_ray.input_storage as input_storage_module
    from django_ray.ray_job_request_storage import (
        RayJobRequestStorageError,
        RayJobRequestStorageRejection,
        register_and_attach_ray_job_request,
    )

    execution, handle = _rq2_reserved_execution("postgres-physical-alias-canonical-writer")
    serialized_request = (
        _prepared_rq2_request(execution, tmp_path).serialized_request
        if payload_kind == InputPayloadKind.RAY_JOB_REQUEST
        else None
    )
    case = _prepared_cloud_alias_payload(
        monkeypatch, backend, serialized_request=serialized_request
    )
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, **case.config}
    payload = TaskInputPayload.objects.create(
        reference=case.legacy_native,
        payload_kind=InputPayloadKind.TASK_INPUT,
        backend=case.prepared.backend,
        digest=case.prepared.digest,
        size_bytes=case.prepared.size_bytes,
        envelope_version=case.prepared.envelope_version,
        state=InputPayloadState.ACTIVE,
    )
    old_timestamp = timezone.now() - timedelta(days=60)
    TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=old_timestamp)
    old_execution = _execution(
        "postgres-physical-alias-old-keeper",
        state=TaskState.SUCCEEDED,
        finished_at=old_timestamp,
        input_reference=case.legacy_native,
    )
    execution_before = RayTaskExecution.objects.values().get(pk=execution.pk)
    keeper_before = RayTaskExecution.objects.values().get(pk=old_execution.pk)
    writes_before = list(case.writes)
    cutoff = timezone.now() - timedelta(days=30)
    deletion_started, release_deletion, writer_connected = Event(), Event(), Event()
    writer_pid: list[int] = []
    deletes: list[str] = []

    def blocking_delete(reference: str) -> None:
        assert reference == case.legacy_native
        deletes.append(reference)
        del case.objects[case.prepared.digest]
        deletion_started.set()
        assert release_deletion.wait(timeout=10)

    monkeypatch.setattr(input_storage_module, "delete_input_reference", blocking_delete)

    def purge() -> str:
        close_old_connections()
        try:
            return PurgeInputsCommand()._process_reference(
                case.legacy_native, cutoff=cutoff, delete=True, max_attachments=1
            )
        finally:
            close_old_connections()

    def register() -> None:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.append(int(cursor.fetchone()[0]))
            writer_connected.set()
            if payload_kind == InputPayloadKind.TASK_INPUT:
                with pytest.raises(
                    input_storage_module.InputPayloadValidationError,
                    match="another physical registry reference",
                ):
                    with transaction.atomic():
                        register_task_input(case.prepared, case.config)
                        _execution(
                            "postgres-physical-alias-forbidden-new",
                            input_reference=case.reference,
                        )
            else:
                with pytest.raises(RayJobRequestStorageError) as caught:
                    register_and_attach_ray_job_request(
                        case.prepared, task_execution=execution, submission_handle=handle
                    )
                assert (
                    caught.value.classification == RayJobRequestStorageRejection.REGISTRY_MISMATCH
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        purge_future = executor.submit(purge)
        try:
            assert deletion_started.wait(timeout=10)
            assert case.objects == {}
            writer_future = executor.submit(register)
            assert writer_connected.wait(timeout=10)
            _wait_for_postgresql_lock(writer_pid[0])
        finally:
            release_deletion.set()
        assert purge_future.result(timeout=20) == "purged"
        writer_future.result(timeout=20)

    payload.refresh_from_db()
    assert payload.state == InputPayloadState.PURGED
    assert payload.purged_at is not None
    assert payload.reference == case.legacy_native
    assert payload.last_used_at == old_timestamp
    assert TaskInputPayload.objects.count() == 1
    assert not TaskInputPayload.objects.filter(reference=case.reference).exists()
    assert RayTaskExecution.objects.values().get(pk=execution.pk) == execution_before
    assert RayTaskExecution.objects.values().get(pk=old_execution.pk) == keeper_before
    assert not RayTaskExecution.objects.filter(
        task_id="postgres-physical-alias-forbidden-new"
    ).exists()
    assert case.objects == {}
    assert case.writes == writes_before
    assert deletes == [case.legacy_native]


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


def test_input_reenqueue_waits_for_purge_then_restores_shared_payload(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force purge-first input restoration through independent connections."""
    import django_ray.input_storage as input_storage_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    prepared = prepare_task_input(("restored input",), {})
    assert prepared.input_reference is not None
    assert prepared.digest is not None
    reference = prepared.input_reference
    with transaction.atomic():
        payload = register_task_input(prepared)
    assert payload is not None
    old_timestamp = timezone.now() - timedelta(days=60)
    TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=old_timestamp)
    old_execution = _execution(
        "bounded-input-purge-first-old",
        state=TaskState.SUCCEEDED,
        finished_at=old_timestamp,
        input_reference=reference,
    )
    path = tmp_path / prepared.digest[:2] / prepared.digest[2:4] / f"{prepared.digest}.json"
    original_bytes = path.read_bytes()
    cutoff = timezone.now() - timedelta(days=30)
    deletion_started, release_deletion, writer_connected = Event(), Event(), Event()
    writer_pid: list[int] = []
    original_delete = input_storage_module.delete_input_reference

    def blocking_delete(reference: str) -> None:
        original_delete(reference)
        deletion_started.set()
        assert release_deletion.wait(timeout=10)

    monkeypatch.setattr(input_storage_module, "delete_input_reference", blocking_delete)

    def purge() -> str:
        close_old_connections()
        try:
            return PurgeInputsCommand()._process_reference(
                reference, cutoff=cutoff, delete=True, max_attachments=1
            )
        finally:
            close_old_connections()

    def reenqueue() -> int:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.append(int(cursor.fetchone()[0]))
            writer_connected.set()
            with transaction.atomic():
                register_task_input(prepared)
                return int(
                    _execution(
                        "bounded-input-purge-first-new",
                        args_json=prepared.args_json,
                        kwargs_json=prepared.kwargs_json,
                        input_reference=reference,
                    ).pk
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        purge_future = executor.submit(purge)
        try:
            assert deletion_started.wait(timeout=10)
            assert not path.exists()
            writer_future = executor.submit(reenqueue)
            assert writer_connected.wait(timeout=10)
            _wait_for_postgresql_lock(writer_pid[0])
        finally:
            release_deletion.set()
        assert purge_future.result(timeout=20) == "purged"
        replacement = RayTaskExecution.objects.get(pk=writer_future.result(timeout=20))

    payload.refresh_from_db()
    old_execution.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.purged_at is None
    assert payload.last_used_at > cutoff
    assert old_execution.input_reference == reference
    assert replacement.state == TaskState.QUEUED
    assert replacement.input_reference == reference
    assert path.read_bytes() == original_bytes
    assert load_task_input(
        args_json=replacement.args_json,
        kwargs_json=replacement.kwargs_json,
        input_reference=replacement.input_reference,
    ) == (["restored input"], {})
