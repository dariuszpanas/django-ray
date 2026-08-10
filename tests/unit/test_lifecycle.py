"""Tests for race-safe lifecycle transitions and attempt history."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import DatabaseError, connection, router
from django.test.utils import CaptureQueriesContext

from django_ray.execution_protocol import ExecutionProtocolRange
from django_ray.input_storage import EXTERNAL_INPUT_PLACEHOLDER
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    TaskRetryRequestStatus,
    _request_task_cancellation,
    _request_task_retry,
    cancel_task,
    expire_queued_tasks,
    record_failure,
    record_lost,
    request_task_cancellation,
    request_task_retry,
    retry_task,
    succeed_task,
)
from django_ray.models import (
    InputPayloadState,
    RayTaskExecution,
    TaskAttempt,
    TaskInputPayload,
    TaskState,
)
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.redaction import REDACTED, normalize_terminal_text, redact_text
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    normalize_runtime_env,
    runtime_env_for_storage,
)
from django_ray.workflow_progress_summary import (
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)
from tests.workflow_progress_summary_helpers import workflow_progress_summary

_SYNTHETIC_V1_V2_PROTOCOLS = ExecutionProtocolRange(minimum=1, maximum=2)
_LOCK_PROJECTION = {
    "id",
    "state",
    "attempt_number",
    "execution_generation",
    "execution_protocol_version",
}
_ATTEMPT_ARCHIVE_READ_PROJECTION = {
    "execution_protocol_version",
    "managed_with_django_ray_version",
    "executor_django_ray_version",
    "started_at",
    "finished_at",
    "error_message",
    "error_traceback",
    "result_data",
    "result_reference",
    "workflow_progress_summary_json",
    "workflow_run_id",
}
_RETRY_LOCK_PROJECTION = _LOCK_PROJECTION | {
    "workflow_plan_fingerprint",
    "workflow_run_id",
}
_RETRY_ACCEPTED_READ_PROJECTION = (_ATTEMPT_ARCHIVE_READ_PROJECTION - {"workflow_run_id"}) | {
    "task_id",
    "queue_timeout_seconds",
    "ray_target_address",
    "ray_job_id",
    "ray_address",
    "runtime_env_profile",
    "runtime_env_json",
    "runtime_env_hash",
}
_REJECTED_PATH_PAYLOAD_COLUMNS = {
    "args_json",
    "kwargs_json",
    "input_reference",
    "result_data",
    "result_reference",
    "error_message",
    "error_traceback",
    "runtime_env_json",
    "progress_data",
    "workflow_plan_json",
    "workflow_plan_selection",
    "completion_data",
    "cancellation_error",
}


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


@pytest.mark.django_db
def test_retry_task_uses_one_based_counter_and_preserves_attempt() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=4,
        metadata_schema_version=0,
        execution_protocol_version=1,
        created_with_django_ray_version=None,
        managed_with_django_ray_version="0.4.0-manager",
        executor_django_ray_version="0.4.0-executor",
        input_reference="s3://inputs/django-ray/inputs/immutable.json?bytes=42",
        workflow_plan_selection='{"selected_strategy":"dynamic_tasks"}',
        error_message="boom",
        error_traceback="RuntimeError: boom",
        ray_target_address="ray://target:10001",
        ray_address="ray://submitted:10001",
    )

    retried = retry_task(task)

    assert retried is not None
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 3
    assert task.execution_generation == 5
    assert task.metadata_schema_version == 0
    assert task.execution_protocol_version == 1
    assert task.created_with_django_ray_version is None
    assert task.managed_with_django_ray_version is None
    assert task.executor_django_ray_version is None
    assert task.input_reference == "s3://inputs/django-ray/inputs/immutable.json?bytes=42"
    assert task.workflow_plan_selection is None
    assert task.error_message is None
    assert task.ray_target_address == "ray://target:10001"
    assert task.ray_address is None
    history = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert history.state == TaskState.FAILED
    assert history.error_message == "boom"
    assert history.execution_protocol_version == 1
    assert history.managed_with_django_ray_version == "0.4.0-manager"
    assert history.executor_django_ray_version == "0.4.0-executor"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "transition",
    [
        "queued_cancel",
        "manual_retry",
        "automatic_retry",
        "final_failure",
        "expiry",
        "lost",
        "success",
        "finalized_cancel",
    ],
)
@pytest.mark.parametrize(
    "executor_version",
    [None, "0.5.0-executor"],
    ids=["executor-unknown", "executor-reported"],
)
def test_attempt_archival_copies_exact_protocol_and_provenance(
    transition: str,
    executor_version: str | None,
) -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    now = datetime.now(UTC)
    initial_state = {
        "queued_cancel": TaskState.QUEUED,
        "manual_retry": TaskState.FAILED,
        "automatic_retry": TaskState.RUNNING,
        "final_failure": TaskState.RUNNING,
        "expiry": TaskState.QUEUED,
        "lost": TaskState.RUNNING,
        "success": TaskState.RUNNING,
        "finalized_cancel": TaskState.CANCELLING,
    }[transition]
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-provenance-{transition}-{executor_version or 'unknown'}",
        callable_path="testproject.tasks.add_numbers",
        metadata_schema_version=1,
        execution_protocol_version=2,
        created_with_django_ray_version="0.4.0-creator",
        managed_with_django_ray_version="0.5.0-manager",
        executor_django_ray_version=executor_version,
        queue_name="default",
        state=initial_state,
        attempt_number=3,
        execution_generation=7,
        queue_deadline_at=now - timedelta(seconds=1),
        started_at=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(minutes=1),
        error_message="previous diagnostic",
    )

    if transition == "queued_cancel":
        result = _request_task_cancellation(
            task.pk,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )
        assert result.status is TaskCancellationRequestStatus.ACCEPTED
    elif transition == "manual_retry":
        _result, retried = _request_task_retry(
            task.pk,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )
        assert retried is not None
    elif transition == "automatic_retry":
        assert record_failure(
            task,
            error_message="retryable failure",
            retry=True,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )
    elif transition == "final_failure":
        assert record_failure(
            task,
            error_message="terminal failure",
            retry=False,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )
    elif transition == "expiry":
        assert expire_queued_tasks(
            ["default"],
            now=now,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        ) == (task.pk,)
    elif transition == "lost":
        assert record_lost(
            task,
            error_message="worker lost",
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )
    elif transition == "success":
        assert succeed_task(
            task,
            result_data="3",
            result_reference=None,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )
    else:
        assert cancel_task(task, supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS)

    task.refresh_from_db()
    archived = TaskAttempt.objects.get(execution=task, attempt_number=3)
    expected_state = {
        "queued_cancel": TaskState.CANCELLED,
        "manual_retry": TaskState.FAILED,
        "automatic_retry": TaskState.FAILED,
        "final_failure": TaskState.FAILED,
        "expiry": TaskState.EXPIRED,
        "lost": TaskState.LOST,
        "success": TaskState.SUCCEEDED,
        "finalized_cancel": TaskState.CANCELLED,
    }[transition]
    assert archived.state == expected_state
    assert archived.execution_protocol_version == 2
    assert archived.managed_with_django_ray_version == "0.5.0-manager"
    assert archived.executor_django_ray_version == executor_version
    assert task.metadata_schema_version == 1
    assert task.execution_protocol_version == 2
    assert task.created_with_django_ray_version == "0.4.0-creator"
    if transition in {"manual_retry", "automatic_retry"}:
        assert task.state == TaskState.QUEUED
        assert task.managed_with_django_ray_version is None
        assert task.executor_django_ray_version is None
    else:
        assert task.state == expected_state
        assert task.managed_with_django_ray_version == "0.5.0-manager"
        assert task.executor_django_ray_version == executor_version


@pytest.mark.django_db
@pytest.mark.parametrize("transition", ["success", "final_failure"])
def test_enriched_completion_stamps_executor_provenance_on_terminal_outcome(
    transition: str,
) -> None:
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-enriched-completion-{transition}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        executor_django_ray_version="0.4.0-previous",
    )

    if transition == "success":
        accepted = succeed_task(
            task,
            result_data="3",
            result_reference=None,
            _executor_django_ray_version="0.5.0-executor",
        )
        expected_state = TaskState.SUCCEEDED
    else:
        accepted = record_failure(
            task,
            error_message="terminal failure",
            retry=False,
            _executor_django_ray_version="0.5.0-executor",
        )
        expected_state = TaskState.FAILED

    assert accepted
    task.refresh_from_db()
    assert task.state == expected_state
    assert task.executor_django_ray_version == "0.5.0-executor"
    archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert archived.state == expected_state
    assert archived.executor_django_ray_version == "0.5.0-executor"


@pytest.mark.django_db
def test_enriched_completion_archives_then_clears_executor_provenance_on_retry() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-enriched-completion-retry",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        executor_django_ray_version="0.4.0-previous",
    )

    assert record_failure(
        task,
        error_message="retryable failure",
        retry=True,
        _executor_django_ray_version="0.5.0-executor",
    )

    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 3
    assert task.executor_django_ray_version is None
    archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert archived.state == TaskState.FAILED
    assert archived.executor_django_ray_version == "0.5.0-executor"


@pytest.mark.django_db
@pytest.mark.parametrize("transition", ["success", "final_failure"])
def test_legacy_completion_omission_preserves_existing_executor_provenance(
    transition: str,
) -> None:
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-legacy-completion-{transition}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        executor_django_ray_version="0.4.0-existing",
    )

    if transition == "success":
        accepted = succeed_task(task, result_data="3", result_reference=None)
        expected_state = TaskState.SUCCEEDED
    else:
        accepted = record_failure(
            task,
            error_message="terminal failure",
            retry=False,
        )
        expected_state = TaskState.FAILED

    assert accepted
    task.refresh_from_db()
    assert task.executor_django_ray_version == "0.4.0-existing"
    archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert archived.state == expected_state
    assert archived.executor_django_ray_version == "0.4.0-existing"


@pytest.mark.django_db
@pytest.mark.parametrize("transition", ["success", "failure"])
def test_rejected_completion_does_not_mutate_executor_provenance(transition: str) -> None:
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-rejected-completion-provenance-{transition}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
        executor_django_ray_version="0.4.0-existing",
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

    if transition == "success":
        accepted = succeed_task(
            task,
            result_data="3",
            result_reference=None,
            expected_execution_generation=3,
            _executor_django_ray_version="0.5.0-rejected",
        )
    else:
        accepted = record_failure(
            task,
            error_message="stale failure",
            retry=False,
            expected_execution_generation=3,
            _executor_django_ray_version="0.5.0-rejected",
        )

    assert not accepted
    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    assert task.executor_django_ray_version == "0.4.0-existing"
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("operation", "initial_state"),
    [
        ("queued_cancel", TaskState.QUEUED),
        ("running_cancel", TaskState.RUNNING),
        ("manual_retry_request", TaskState.FAILED),
        ("manual_retry_legacy", TaskState.FAILED),
        ("automatic_retry", TaskState.RUNNING),
        ("final_failure", TaskState.RUNNING),
        ("expiry", TaskState.QUEUED),
        ("lost", TaskState.RUNNING),
        ("success", TaskState.RUNNING),
        ("finalized_cancel", TaskState.CANCELLING),
    ],
)
def test_package_lifecycle_rejects_unsupported_protocol_before_effects(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    initial_state: str,
) -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    now = datetime.now(UTC)
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-unsupported-{operation}",
        callable_path="testproject.tasks.add_numbers",
        state=initial_state,
        attempt_number=3,
        execution_generation=7,
        execution_protocol_version=2,
        queue_deadline_at=now - timedelta(seconds=1),
        started_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=1) if initial_state == TaskState.FAILED else None,
        last_heartbeat_at=now - timedelta(minutes=1),
        managed_with_django_ray_version="0.5.0-manager",
        executor_django_ray_version="0.5.0-executor",
        result_data='{"retained":true}',
        error_message="retained diagnostic",
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

    def unexpected_effect(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("unsupported lifecycle work reached payload hydration or archival")

    monkeypatch.setattr("django_ray.lifecycle.runtime_env_for_execution", unexpected_effect)
    monkeypatch.setattr("django_ray.lifecycle._record_attempt", unexpected_effect)

    if operation in {"queued_cancel", "running_cancel"}:
        result = request_task_cancellation(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=7,
        )
        assert result.status is TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL
    elif operation == "manual_retry_request":
        result = request_task_retry(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=7,
        )
        assert result.status is TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL
    elif operation == "manual_retry_legacy":
        assert retry_task(task.pk) is None
    elif operation == "automatic_retry":
        assert not record_failure(task, error_message="replacement", retry=True)
    elif operation == "final_failure":
        assert not record_failure(task, error_message="replacement", retry=False)
    elif operation == "expiry":
        assert expire_queued_tasks(["default"], now=now) == ()
    elif operation == "lost":
        assert not record_lost(task, error_message="replacement")
    elif operation == "success":
        assert not succeed_task(task, result_data="replacement", result_reference=None)
    else:
        assert not cancel_task(task)

    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db(transaction=True)
def test_stale_identity_precedes_unsupported_protocol_and_protocol_precedes_state() -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-unsupported-precedence",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.QUEUED,
        attempt_number=3,
        execution_generation=7,
        execution_protocol_version=2,
    )

    stale_retry = request_task_retry(
        task.pk,
        expected_attempt_number=2,
        expected_execution_generation=7,
    )
    stale_retry_generation = request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=6,
    )
    stale_retry_workflow = request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
        expected_workflow_identity=("00000000-0000-0000-0000-000000000361", None),
    )
    stale_cancel_attempt = request_task_cancellation(
        task.pk,
        expected_attempt_number=2,
        expected_execution_generation=7,
    )
    stale_cancel_generation = request_task_cancellation(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=6,
    )
    unsupported_retry = request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
    )
    unsupported_cancel = request_task_cancellation(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
    )

    assert stale_retry.status is TaskRetryRequestStatus.STALE_ATTEMPT
    assert stale_retry_generation.status is TaskRetryRequestStatus.STALE_GENERATION
    assert stale_retry_workflow.status is TaskRetryRequestStatus.STALE_WORKFLOW_IDENTITY
    assert stale_cancel_attempt.status is TaskCancellationRequestStatus.STALE_ATTEMPT
    assert stale_cancel_generation.status is TaskCancellationRequestStatus.STALE_GENERATION
    assert unsupported_retry.status is TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL
    assert unsupported_cancel.status is TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db(transaction=True)
def test_explicit_supported_protocol_override_allows_v2_retry() -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-explicit-v2-retry",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=3,
        execution_generation=7,
        execution_protocol_version=2,
        error_message="retained failure",
    )

    result, retried = _request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
        supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
    )

    assert result.status is TaskRetryRequestStatus.ACCEPTED
    assert retried is not None
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.execution_protocol_version == 2
    assert TaskAttempt.objects.get(execution=task, attempt_number=3).execution_protocol_version == 2


@pytest.mark.django_db
def test_explicit_retry_of_expired_task_gets_fresh_deadline() -> None:
    old_deadline = datetime.now(UTC) - timedelta(days=1)
    task = RayTaskExecution.objects.create(
        task_id="retry-expired-queue-deadline-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.EXPIRED,
        queue_timeout_seconds=120,
        queue_deadline_at=old_deadline,
        finished_at=old_deadline,
        error_message="Task expired before execution after exceeding its queued-wait deadline",
    )
    not_before = datetime.now(UTC) + timedelta(hours=1)

    retried = retry_task(task, next_attempt_at=not_before)

    assert retried is not None
    assert retried.state == TaskState.QUEUED
    assert retried.queue_timeout_seconds == 120
    assert retried.queue_deadline_at == not_before + timedelta(seconds=120)
    assert TaskAttempt.objects.get(execution=task, attempt_number=1).state == TaskState.EXPIRED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage_mode", ["inline", "external"])
def test_retry_uses_exact_projection_and_preserves_input_and_result_storage(
    storage_mode: str,
) -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    unrelated_marker = f"unrelated-{storage_mode}-" + ("x" * 65_536)
    input_reference: str | None = None
    result_data: str | None = '{"result":"inline"}'
    result_reference: str | None = None
    args_json = '["inline"]'
    kwargs_json = '{"mode":"inline"}'
    payload: TaskInputPayload | None = None
    if storage_mode == "external":
        digest = "a" * 64
        input_reference = f"s3://task-inputs/django-ray/inputs/aa/aa/{digest}.json?bytes=42"
        args_json = EXTERNAL_INPUT_PLACEHOLDER
        kwargs_json = EXTERNAL_INPUT_PLACEHOLDER
        result_data = None
        result_reference = "digest:" + ("b" * 64)
        payload = TaskInputPayload.objects.create(
            reference=input_reference,
            backend="s3",
            digest=digest,
            size_bytes=42,
            envelope_version=1,
        )
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-projected-retry-{storage_mode}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=4,
        metadata_schema_version=1,
        execution_protocol_version=2,
        created_with_django_ray_version="0.4.0-creator",
        managed_with_django_ray_version="0.5.0-manager",
        executor_django_ray_version="0.5.0-executor",
        args_json=args_json,
        kwargs_json=kwargs_json,
        input_reference=input_reference,
        result_data=result_data,
        result_reference=result_reference,
        progress_data=unrelated_marker,
        workflow_plan_json=unrelated_marker,
        workflow_plan_selection=unrelated_marker,
        completion_data=unrelated_marker,
        cancellation_error=unrelated_marker,
        error_message="retryable failure",
        error_traceback="RetryError: retryable failure",
    )

    with CaptureQueriesContext(connection) as queries:
        _result, retried = _request_task_retry(
            task.pk,
            expected_attempt_number=2,
            expected_execution_generation=4,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )

    assert retried is not None
    projections = _execution_select_projections(queries)
    assert [fields for fields, _sql in projections] == [
        _RETRY_LOCK_PROJECTION,
        _RETRY_ACCEPTED_READ_PROJECTION,
    ]
    current = RayTaskExecution.objects.get(pk=task.pk)
    assert current.state == TaskState.QUEUED
    assert current.attempt_number == 3
    assert current.execution_generation == 5
    assert current.metadata_schema_version == 1
    assert current.execution_protocol_version == 2
    assert current.created_with_django_ray_version == "0.4.0-creator"
    assert current.managed_with_django_ray_version is None
    assert current.executor_django_ray_version is None
    assert current.args_json == args_json
    assert current.kwargs_json == kwargs_json
    assert current.input_reference == input_reference
    assert current.result_data is None
    assert current.result_reference is None
    archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert archived.result_data == result_data
    assert archived.result_reference == result_reference
    assert archived.error_message == "retryable failure"
    assert archived.error_traceback == "RetryError: retryable failure"
    assert archived.execution_protocol_version == 2
    assert archived.managed_with_django_ray_version == "0.5.0-manager"
    assert archived.executor_django_ray_version == "0.5.0-executor"
    if payload is not None:
        payload.refresh_from_db()
        assert payload.state == InputPayloadState.ACTIVE

    assert {"args_json", "kwargs_json", "input_reference", "workflow_plan_json"}.issubset(
        retried.get_deferred_fields()
    )
    with CaptureQueriesContext(connection) as caller_queries:
        assert retried.input_reference == input_reference
    assert len(caller_queries) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    (
        "state",
        "expected_attempt",
        "expected_generation",
        "workflow_fence",
        "expected_status",
    ),
    [
        (TaskState.QUEUED, 3, 8, None, TaskRetryRequestStatus.NOT_RETRYABLE),
        (TaskState.SUCCEEDED, 3, 8, None, TaskRetryRequestStatus.NOT_RETRYABLE),
        (TaskState.FAILED, 2, 8, None, TaskRetryRequestStatus.STALE_ATTEMPT),
        (TaskState.FAILED, 3, 7, None, TaskRetryRequestStatus.STALE_GENERATION),
        (
            TaskState.FAILED,
            3,
            8,
            ("00000000-0000-0000-0000-000000000999", "sha256:" + ("f" * 64)),
            TaskRetryRequestStatus.STALE_WORKFLOW_IDENTITY,
        ),
    ],
)
def test_retry_noop_and_stale_paths_use_one_bounded_lock(
    state: str,
    expected_attempt: int,
    expected_generation: int,
    workflow_fence: tuple[str | None, str | None] | None,
    expected_status: TaskRetryRequestStatus,
) -> None:
    marker = "unrelated-retry-noop-" + ("q" * 65_536)
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-projected-retry-noop-{state.lower()}-{expected_attempt}",
        callable_path="testproject.tasks.add_numbers",
        state=state,
        attempt_number=3,
        execution_generation=8,
        args_json=marker,
        kwargs_json=marker,
        input_reference="s3://task-inputs/example.json?bytes=42",
        progress_data=marker,
        workflow_plan_json=marker,
        workflow_plan_selection=marker,
        completion_data=marker,
        cancellation_error=marker,
        result_data=marker,
        error_message=marker,
        error_traceback=marker,
        runtime_env_json=marker,
        runtime_env_hash=marker,
        workflow_run_id="00000000-0000-0000-0000-000000000335",
        workflow_plan_fingerprint="sha256:" + ("a" * 64),
    )

    with CaptureQueriesContext(connection) as queries:
        result = request_task_retry(
            task.pk,
            expected_attempt_number=expected_attempt,
            expected_execution_generation=expected_generation,
            expected_workflow_identity=workflow_fence,
        )

    assert result.status is expected_status
    projections = _execution_select_projections(queries)
    assert [fields for fields, _sql in projections] == [_RETRY_LOCK_PROJECTION]
    assert projections[0][0].isdisjoint(_REJECTED_PATH_PAYLOAD_COLUMNS)
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db(transaction=True)
def test_cancellation_uses_state_specific_projections_without_payload_reload() -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    unrelated_marker = "unrelated-cancel-" + ("x" * 65_536)
    queued = RayTaskExecution.objects.create(
        task_id="lifecycle-projected-cancel-queued",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.QUEUED,
        attempt_number=2,
        execution_generation=4,
        args_json=unrelated_marker,
        kwargs_json=unrelated_marker,
        input_reference="s3://task-inputs/example.json?bytes=42",
        runtime_env_json=unrelated_marker,
        progress_data=unrelated_marker,
        workflow_plan_json=unrelated_marker,
        completion_data=unrelated_marker,
        cancellation_error=unrelated_marker,
        result_data='{"cancelled":"result"}',
        result_reference="digest:" + ("c" * 64),
        error_message="queued cancellation",
        error_traceback="CancellationError: queued cancellation",
        workflow_run_id="00000000-0000-0000-0000-000000000335",
        execution_protocol_version=2,
        managed_with_django_ray_version="0.5.0-manager",
        executor_django_ray_version=None,
    )
    summary = workflow_progress_summary(queued)
    RayTaskExecution.objects.filter(pk=queued.pk).update(
        workflow_progress_summary_json=serialize_workflow_progress_summary(summary)
    )

    with CaptureQueriesContext(connection) as queued_queries:
        queued_result = _request_task_cancellation(
            queued.pk,
            expected_attempt_number=2,
            expected_execution_generation=4,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
        )

    assert queued_result.status is TaskCancellationRequestStatus.ACCEPTED
    queued_projections = _execution_select_projections(queued_queries)
    assert [fields for fields, _sql in queued_projections] == [
        _LOCK_PROJECTION,
        _ATTEMPT_ARCHIVE_READ_PROJECTION,
    ]
    archived = TaskAttempt.objects.get(execution=queued, attempt_number=2)
    assert archived.workflow_progress_summary_json is not None
    assert (
        deserialize_workflow_progress_summary(archived.workflow_progress_summary_json)["state"]
        == TaskState.CANCELLED
    )
    assert archived.result_data == '{"cancelled":"result"}'
    assert archived.result_reference == "digest:" + ("c" * 64)
    assert archived.error_message == "queued cancellation"
    assert archived.error_traceback == "CancellationError: queued cancellation"
    assert archived.execution_protocol_version == 2
    assert archived.managed_with_django_ray_version == "0.5.0-manager"
    assert archived.executor_django_ray_version is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("completion_pending", "expected_status", "expected_state"),
    [
        (False, TaskCancellationRequestStatus.ACCEPTED, TaskState.CANCELLING),
        (True, TaskCancellationRequestStatus.COMPLETION_PENDING, TaskState.RUNNING),
    ],
)
def test_running_cancellation_uses_presence_projection_without_diagnostics(
    completion_pending: bool,
    expected_status: TaskCancellationRequestStatus,
    expected_state: str,
) -> None:
    unrelated_marker = "unrelated-running-cancel-" + ("y" * 65_536)
    running = RayTaskExecution.objects.create(
        task_id=f"lifecycle-projected-cancel-running-{completion_pending}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=3,
        execution_generation=7,
        args_json=unrelated_marker,
        kwargs_json=unrelated_marker,
        completion_data=unrelated_marker if completion_pending else None,
        result_data=unrelated_marker,
        error_message=unrelated_marker,
        error_traceback=unrelated_marker,
    )

    with CaptureQueriesContext(connection) as running_queries:
        running_result = request_task_cancellation(running.pk)

    assert running_result.status is expected_status
    running_projections = _execution_select_projections(running_queries)
    assert [fields for fields, _sql in running_projections] == [
        _LOCK_PROJECTION,
        set(),
    ]
    assert all(
        fields.isdisjoint(_REJECTED_PATH_PAYLOAD_COLUMNS) for fields, _sql in running_projections
    )
    running.refresh_from_db()
    assert running.state == expected_state


@pytest.mark.django_db
def test_running_cancellation_keeps_presence_read_on_locked_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-cancel-database-affinity",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
    )

    class PrimaryReplicaProbe:
        read_calls = 0
        write_calls = 0

        def db_for_read(self, model: type[RayTaskExecution], **_hints: Any) -> str:
            if model is RayTaskExecution:
                self.read_calls += 1
            return "unconfigured-replica"

        def db_for_write(self, model: type[RayTaskExecution], **_hints: Any) -> str:
            if model is RayTaskExecution:
                self.write_calls += 1
            return "default"

    probe = PrimaryReplicaProbe()
    monkeypatch.setattr(router, "routers", [probe])

    result = request_task_cancellation(task.pk)

    assert result.status is TaskCancellationRequestStatus.ACCEPTED
    assert probe.read_calls == 0
    assert probe.write_calls >= 1
    task.refresh_from_db(using="default")
    assert task.state == TaskState.CANCELLING


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("state", "expected_attempt", "expected_generation", "expected_status"),
    [
        (
            TaskState.CANCELLING,
            3,
            8,
            TaskCancellationRequestStatus.ALREADY_REQUESTED,
        ),
        (
            TaskState.SUCCEEDED,
            3,
            8,
            TaskCancellationRequestStatus.ALREADY_TERMINAL,
        ),
        (
            TaskState.RUNNING,
            2,
            8,
            TaskCancellationRequestStatus.STALE_ATTEMPT,
        ),
        (
            TaskState.RUNNING,
            3,
            7,
            TaskCancellationRequestStatus.STALE_GENERATION,
        ),
    ],
)
def test_cancellation_duplicate_terminal_and_stale_paths_use_one_bounded_lock(
    state: str,
    expected_attempt: int,
    expected_generation: int,
    expected_status: TaskCancellationRequestStatus,
) -> None:
    marker = "unrelated-noop-" + ("z" * 65_536)
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-projected-cancel-{state.lower()}",
        callable_path="testproject.tasks.add_numbers",
        state=state,
        attempt_number=3,
        execution_generation=8,
        args_json=marker,
        kwargs_json=marker,
        completion_data=marker,
        result_data=marker,
        error_message=marker,
        error_traceback=marker,
    )

    with CaptureQueriesContext(connection) as queries:
        result = request_task_cancellation(
            task.pk,
            expected_attempt_number=expected_attempt,
            expected_execution_generation=expected_generation,
        )

    assert result.status is expected_status
    projections = _execution_select_projections(queries)
    assert [fields for fields, _sql in projections] == [_LOCK_PROJECTION]
    assert projections[0][0].isdisjoint(_REJECTED_PATH_PAYLOAD_COLUMNS)


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["retry", "cancel"])
def test_attempt_storage_failure_rolls_back_projected_lifecycle_transition(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    state = TaskState.FAILED if operation == "retry" else TaskState.QUEUED
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-projected-storage-failure-{operation}",
        callable_path="testproject.tasks.add_numbers",
        state=state,
        attempt_number=2,
        execution_generation=4,
        error_message="retained failure",
        result_reference="digest:" + ("c" * 64),
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

    def fail_attempt_storage(*_args: Any, **_kwargs: Any) -> None:
        raise DatabaseError("attempt storage unavailable")

    monkeypatch.setattr(TaskAttempt.objects, "update_or_create", fail_attempt_storage)
    transition = retry_task if operation == "retry" else request_task_cancellation

    with pytest.raises(DatabaseError, match="attempt storage unavailable"):
        transition(task.pk)

    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db(transaction=True)
def test_automatic_retry_attempt_archive_failure_rolls_back_provenance_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_legacy_worker_admission(
        expected_revision=1,
        legacy_producers_retired=True,
    )
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-auto-retry-provenance-archive-failure",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=4,
        execution_protocol_version=2,
        created_with_django_ray_version="0.4.0-creator",
        managed_with_django_ray_version="0.5.0-manager",
        executor_django_ray_version="0.5.0-executor",
        error_message="retained failure",
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

    def fail_attempt_storage(*_args: Any, **_kwargs: Any) -> None:
        raise DatabaseError("attempt provenance archive unavailable")

    monkeypatch.setattr(TaskAttempt.objects, "update_or_create", fail_attempt_storage)

    with pytest.raises(DatabaseError, match="attempt provenance archive unavailable"):
        record_failure(
            task,
            error_message="current failure",
            retry=True,
            supported_protocols=_SYNTHETIC_V1_V2_PROTOCOLS,
            _executor_django_ray_version="0.6.0-rejected-by-rollback",
        )

    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    assert task.executor_django_ray_version == "0.5.0-executor"
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_failure_preserves_raw_redaction_evidence_for_current_and_archived_attempts() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-terminal-diagnostic-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
    )

    raw_error = "pass\x1b[31\x0eword=do-not-expose"
    raw_traceback = (
        '\x1b[36mray::task()\x1b[39m\r\nFile "/app/task.py", line 1\nRuntimeError: failed'
    )
    raw_cancellation = "\x1b[33mstop not confirmed\x1b[39m\rretry blocked"

    accepted = record_failure(
        task,
        error_message=raw_error,
        error_traceback=raw_traceback,
        cancellation_status="INDETERMINATE",
        cancellation_error=raw_cancellation,
        retry=False,
    )

    assert accepted
    task.refresh_from_db()
    assert task.error_message == raw_error
    assert normalize_terminal_text(task.error_message) == "passord=do-not-expose"
    assert redact_text(task.error_message) == REDACTED
    assert task.error_traceback == raw_traceback
    assert normalize_terminal_text(task.error_traceback) == (
        'ray::task()\nFile "/app/task.py", line 1\nRuntimeError: failed'
    )
    assert task.cancellation_error == raw_cancellation
    assert normalize_terminal_text(task.cancellation_error) == "stop not confirmed\nretry blocked"
    attempt = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert attempt.error_message == task.error_message
    assert attempt.error_traceback == task.error_traceback


@pytest.mark.django_db
def test_cancellation_preserves_raw_diagnostic_for_bounded_readers() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-terminal-cancellation-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.CANCELLING,
    )

    raw_cancellation = "\x1b[33mRay stop uncertain\x1b[39m\rmanual review"
    accepted = cancel_task(
        task,
        cancellation_status="INDETERMINATE",
        cancellation_error=raw_cancellation,
    )

    assert accepted
    task.refresh_from_db()
    assert task.state == TaskState.CANCELLED
    assert task.cancellation_error == raw_cancellation
    assert normalize_terminal_text(task.cancellation_error) == "Ray stop uncertain\nmanual review"


@pytest.mark.django_db
def test_retry_task_promotes_legacy_submission_address_to_target() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-legacy-routing-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        ray_address="ray://legacy:10001",
    )

    retried = retry_task(task)

    assert retried is not None
    task.refresh_from_db()
    assert task.ray_target_address == "ray://legacy:10001"
    assert task.ray_address is None


@pytest.mark.django_db
def test_retry_task_keeps_ambiguous_legacy_auto_on_global_fallback() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-legacy-auto-routing-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        ray_address="auto",
    )

    retried = retry_task(task)

    assert retried is not None
    task.refresh_from_db()
    assert task.ray_target_address is None
    assert task.ray_address is None


@pytest.mark.django_db
def test_retry_task_does_not_promote_ray_core_handle_to_job_target() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-ray-core-routing-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        ray_job_id="ray_core:17",
        ray_address="ray://core-cluster:10001",
    )

    retried = retry_task(task)

    assert retried is not None
    task.refresh_from_db()
    assert task.ray_target_address is None
    assert task.ray_job_id is None
    assert task.ray_address is None


@pytest.mark.django_db
def test_retry_task_rejects_a_stale_execution_generation() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-stale-generation-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=3,
        execution_generation=7,
        error_message="newer attempt failed",
    )

    assert (
        retry_task(
            task.pk,
            expected_attempt_number=2,
            expected_execution_generation=7,
        )
        is None
    )
    assert (
        retry_task(
            task.pk,
            expected_attempt_number=3,
            expected_execution_generation=6,
        )
        is None
    )

    task.refresh_from_db()
    assert task.state == TaskState.FAILED
    assert task.attempt_number == 3
    assert task.execution_generation == 7
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_request_task_retry_reports_acceptance_and_duplicate_current_state() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-request-accepted-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=4,
        error_message="first attempt failed",
    )

    accepted = request_task_retry(
        task.pk,
        expected_attempt_number=2,
        expected_execution_generation=4,
    )

    assert accepted.status is TaskRetryRequestStatus.ACCEPTED
    assert accepted.accepted is True
    assert accepted.execution_id == task.pk
    assert accepted.state == TaskState.QUEUED
    assert accepted.attempt_number == 3
    assert accepted.execution_generation == 5
    assert TaskAttempt.objects.get(execution=task, attempt_number=2).state == TaskState.FAILED

    duplicate = request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=5,
    )

    assert duplicate.status is TaskRetryRequestStatus.NOT_RETRYABLE
    assert duplicate.accepted is False
    assert duplicate.state == TaskState.QUEUED
    assert duplicate.attempt_number == 3
    assert duplicate.execution_generation == 5
    assert TaskAttempt.objects.filter(execution=task).count() == 1


@pytest.mark.django_db
def test_request_task_retry_preserves_a_succeeded_execution() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-request-succeeded-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.SUCCEEDED,
        attempt_number=3,
        execution_generation=3,
        workflow_run_id="00000000-0000-0000-0000-000000000321",
        result_data='{"sensitive_result":"must-remain-current"}',
        result_reference="digest:successful-result",
        finished_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

    outcome = request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=3,
    )

    assert outcome.status is TaskRetryRequestStatus.NOT_RETRYABLE
    assert outcome.accepted is False
    assert outcome.execution_id == task.pk
    assert outcome.state == TaskState.SUCCEEDED
    assert outcome.attempt_number == 3
    assert outcome.execution_generation == 3
    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_request_task_retry_reports_stale_and_missing_fences() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-request-stale-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=3,
        execution_generation=7,
        workflow_run_id="00000000-0000-0000-0000-000000000322",
        workflow_plan_fingerprint="sha256:" + "a" * 64,
        error_message="current failure",
    )

    stale_attempt = request_task_retry(
        task.pk,
        expected_attempt_number=2,
        expected_execution_generation=7,
    )
    stale_generation = request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=6,
    )
    stale_workflow = request_task_retry(
        task.pk,
        expected_attempt_number=3,
        expected_execution_generation=7,
        expected_workflow_identity=(
            "00000000-0000-0000-0000-000000000323",
            "sha256:" + "b" * 64,
        ),
    )
    missing = request_task_retry(task.pk + 100_000)

    assert stale_attempt.status is TaskRetryRequestStatus.STALE_ATTEMPT
    assert stale_generation.status is TaskRetryRequestStatus.STALE_GENERATION
    assert stale_workflow.status is TaskRetryRequestStatus.STALE_WORKFLOW_IDENTITY
    for outcome in (stale_attempt, stale_generation, stale_workflow):
        assert outcome.state == TaskState.FAILED
        assert outcome.attempt_number == 3
        assert outcome.execution_generation == 7
        assert outcome.accepted is False
    assert missing.status is TaskRetryRequestStatus.NOT_FOUND
    assert missing.state is None
    assert missing.attempt_number is None
    assert missing.execution_generation is None
    assert missing.accepted is False
    task.refresh_from_db()
    assert task.state == TaskState.FAILED
    assert task.attempt_number == 3
    assert task.execution_generation == 7
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_retry_task_integrity_preflight_preserves_the_complete_execution(monkeypatch) -> None:
    snapshot = normalize_runtime_env(
        {"env_vars": {"VALUE": "arbitrary-customer-marker-7cf3"}},
        profile="thin",
    )
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-runtime-env-integrity-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=4,
        run_after=datetime(2026, 8, 1, tzinfo=UTC),
        result_data='{"partial":true}',
        result_reference="digest:retained-result",
        progress_data='{"revision":7}',
        workflow_progress_summary_json='{"schema_version":3}',
        workflow_run_id="00000000-0000-0000-0000-000000000232",
        workflow_plan_selection='{"selected_strategy":"dynamic_tasks"}',
        completion_data='{"completion":"retained"}',
        error_message="original failure",
        error_traceback="OriginalError: retained",
        started_at=datetime(2026, 7, 29, tzinfo=UTC),
        finished_at=datetime(2026, 7, 30, tzinfo=UTC),
        last_heartbeat_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        claimed_by_worker="retained-worker",
        ray_target_address="ray://target:10001",
        ray_job_id="raysubmit_retained",
        ray_address="ray://submitted:10001",
        cancellation_status="INDETERMINATE",
        cancellation_error="retained cancellation diagnostic",
        runtime_env_profile=snapshot.profile,
        runtime_env_json=snapshot.serialized,
        runtime_env_hash="0" * 64,
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    monkeypatch.setattr(
        "django_ray.lifecycle._record_attempt",
        lambda _execution: pytest.fail("attempt archival ran before RuntimeEnv preflight"),
    )

    with pytest.raises(RuntimeEnvSnapshotError, match="hash does not match") as exc_info:
        retry_task(task.pk)

    after = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    assert after == before
    assert not TaskAttempt.objects.filter(execution=task).exists()
    assert "arbitrary-customer-marker-7cf3" not in str(exc_info.value)


@pytest.mark.django_db
def test_retry_task_missing_encryption_key_preserves_the_complete_execution(
    settings,
) -> None:
    key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
    write_config = {
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"retained-key": key},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "retained-key",
    }
    snapshot = normalize_runtime_env(
        {"env_vars": {"VALUE": "arbitrary-encrypted-retry-marker-8b2a"}},
        profile="thin",
    )
    task_id = "lifecycle-retry-encrypted-key-001"
    stored = runtime_env_for_storage(
        snapshot,
        task_id=task_id,
        config=write_config,
    )
    task = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=4,
        error_message="original failure",
        error_traceback="OriginalError: retained",
        runtime_env_profile=stored.profile,
        runtime_env_json=stored.serialized,
        runtime_env_hash=stored.digest,
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RUNTIME_ENV_STORAGE_MODE": "plaintext",
    }

    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(
            RuntimeEnvSnapshotError, match="decryption key is unavailable"
        ) as exc_info:
            retry_task(task.pk)

    after = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    assert after == before
    assert not TaskAttempt.objects.filter(execution=task).exists()
    assert stored.serialized not in str(exc_info.value)
    assert "arbitrary-encrypted-retry-marker-8b2a" not in str(exc_info.value)
    assert [fields for fields, _sql in _execution_select_projections(queries)] == [
        _RETRY_LOCK_PROJECTION,
        _RETRY_ACCEPTED_READ_PROJECTION,
    ]


@pytest.mark.django_db
def test_retry_task_rejects_missing_hash_on_encrypted_no_profile_snapshot(
    settings,
) -> None:
    key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
    config = {
        "RAY_ADDRESS": "auto",
        "RAY_RUNTIME_ENV": {"env_vars": {"MODE": "current-default"}},
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"retained-key": key},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "retained-key",
    }
    marker = "arbitrary-encrypted-retry-missing-hash-3c91"
    task_id = "lifecycle-retry-encrypted-missing-hash-001"
    stored = runtime_env_for_storage(
        normalize_runtime_env({"env_vars": {"VALUE": marker}}),
        task_id=task_id,
        config=config,
    )
    task = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=4,
        error_message="original failure",
        runtime_env_profile=stored.profile,
        runtime_env_json=stored.serialized,
        runtime_env_hash="",
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    settings.DJANGO_RAY = config

    with pytest.raises(RuntimeEnvSnapshotError, match="incomplete identity") as exc_info:
        retry_task(task.pk)

    after = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    assert after == before
    assert not TaskAttempt.objects.filter(execution=task).exists()
    assert stored.serialized not in str(exc_info.value)
    assert marker not in str(exc_info.value)


@pytest.mark.django_db
def test_automatic_retry_integrity_preflight_rolls_back_before_attempt_history() -> None:
    snapshot = normalize_runtime_env(
        {"env_vars": {"VALUE": "arbitrary-customer-marker-7cf3"}},
    )
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-auto-retry-runtime-env-integrity-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=3,
        error_message="previous diagnostic",
        claimed_by_worker="retained-worker",
        runtime_env_json=snapshot.serialized,
        runtime_env_hash="0" * 64,
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

    with pytest.raises(RuntimeEnvSnapshotError, match="hash does not match"):
        record_failure(
            task,
            error_message="current callable failure",
            retry=True,
        )

    after = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    assert after == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_automatic_retry_missing_encryption_key_rolls_back_before_attempt_history(
    settings,
) -> None:
    key = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode("ascii")
    task_id = "lifecycle-auto-retry-encrypted-key-001"
    snapshot = normalize_runtime_env(
        {"env_vars": {"VALUE": "arbitrary-encrypted-auto-retry-marker-5d7c"}},
    )
    stored = runtime_env_for_storage(
        snapshot,
        task_id=task_id,
        config={
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"retained-key": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "retained-key",
        },
    )
    task = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=3,
        error_message="previous diagnostic",
        claimed_by_worker="retained-worker",
        runtime_env_profile=stored.profile,
        runtime_env_json=stored.serialized,
        runtime_env_hash=stored.digest,
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RUNTIME_ENV_STORAGE_MODE": "plaintext",
    }

    with pytest.raises(RuntimeEnvSnapshotError, match="decryption key is unavailable"):
        record_failure(
            task,
            error_message="current callable failure",
            retry=True,
        )

    after = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    assert after == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_automatic_retry_rejects_missing_hash_on_encrypted_no_profile_snapshot(
    settings,
) -> None:
    key = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode("ascii")
    config = {
        "RAY_ADDRESS": "auto",
        "RAY_RUNTIME_ENV": {"env_vars": {"MODE": "current-default"}},
        "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"retained-key": key},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "retained-key",
    }
    marker = "arbitrary-encrypted-auto-retry-missing-hash-4da2"
    task_id = "lifecycle-auto-retry-encrypted-missing-hash-001"
    stored = runtime_env_for_storage(
        normalize_runtime_env({"env_vars": {"VALUE": marker}}),
        task_id=task_id,
        config=config,
    )
    task = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=3,
        error_message="previous diagnostic",
        claimed_by_worker="retained-worker",
        runtime_env_profile=stored.profile,
        runtime_env_json=stored.serialized,
        runtime_env_hash="",
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    settings.DJANGO_RAY = config

    with pytest.raises(RuntimeEnvSnapshotError, match="incomplete identity") as exc_info:
        record_failure(
            task,
            error_message="current callable failure",
            retry=True,
        )

    after = RayTaskExecution.objects.filter(pk=task.pk).values().get()
    assert after == before
    assert not TaskAttempt.objects.filter(execution=task).exists()
    assert stored.serialized not in str(exc_info.value)
    assert marker not in str(exc_info.value)


@pytest.mark.django_db
def test_terminal_failure_can_record_a_corrupt_snapshot_without_retrying() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-terminal-runtime-env-integrity-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=3,
        runtime_env_json='{"env_vars":{"VALUE":"arbitrary-customer-marker-7cf3"}}',
        runtime_env_hash="0" * 64,
    )

    assert record_failure(
        task,
        error_message="Persisted RuntimeEnv snapshot failed validation",
        retry=False,
    )

    task.refresh_from_db()
    assert task.state == TaskState.FAILED
    assert task.attempt_number == 1
    assert TaskAttempt.objects.filter(execution=task, attempt_number=1).exists()


@pytest.mark.django_db
def test_cancellation_request_cancels_queued_task_and_records_attempt() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-cancel-queued-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.QUEUED,
        attempt_number=2,
        execution_generation=4,
    )

    result = request_task_cancellation(
        task.pk,
        expected_execution_generation=4,
    )

    assert result.status is TaskCancellationRequestStatus.ACCEPTED
    assert result.accepted is True
    assert result.state == TaskState.CANCELLED
    assert result.attempt_number == 2
    assert result.execution_generation == 4
    task.refresh_from_db()
    assert task.state == TaskState.CANCELLED
    assert task.finished_at is not None
    attempt = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert attempt.state == TaskState.CANCELLED
    assert attempt.finished_at == task.finished_at


@pytest.mark.django_db
def test_cancellation_request_marks_running_task_for_worker() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-cancel-running-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        execution_generation=5,
    )

    result = request_task_cancellation(
        task.pk,
        expected_execution_generation=5,
    )

    assert result.status is TaskCancellationRequestStatus.ACCEPTED
    assert result.state == TaskState.CANCELLING
    assert result.attempt_number == 1
    task.refresh_from_db()
    assert task.state == TaskState.CANCELLING
    assert task.finished_at is None
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_cancellation_request_preserves_pending_completion_publication() -> None:
    from django_ray.runtime.entrypoint import _persist_task_completion

    task = RayTaskExecution.objects.create(
        task_id="lifecycle-cancel-completion-pending-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    completion_data = '{"success": true, "result": 3}'
    _persist_task_completion(
        task.pk,
        task.attempt_number,
        task.execution_generation,
        completion_data,
    )

    result = request_task_cancellation(
        task.pk,
        expected_attempt_number=2,
        expected_execution_generation=5,
    )

    assert result.status is TaskCancellationRequestStatus.COMPLETION_PENDING
    assert result.accepted is False
    assert result.state == TaskState.RUNNING
    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.completion_data == completion_data
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (TaskState.CANCELLING, TaskCancellationRequestStatus.ALREADY_REQUESTED),
        (TaskState.SUCCEEDED, TaskCancellationRequestStatus.ALREADY_TERMINAL),
        (TaskState.FAILED, TaskCancellationRequestStatus.ALREADY_TERMINAL),
        (TaskState.CANCELLED, TaskCancellationRequestStatus.ALREADY_TERMINAL),
        (TaskState.LOST, TaskCancellationRequestStatus.ALREADY_TERMINAL),
        (TaskState.EXPIRED, TaskCancellationRequestStatus.ALREADY_TERMINAL),
    ],
)
def test_cancellation_request_returns_stable_noop_status(
    state: str,
    expected_status: TaskCancellationRequestStatus,
) -> None:
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-cancel-noop-{state.lower()}",
        callable_path="testproject.tasks.add_numbers",
        state=state,
        execution_generation=3,
    )

    result = request_task_cancellation(task.pk)

    assert result.status is expected_status
    assert result.accepted is False
    assert result.state == state
    task.refresh_from_db()
    assert task.state == state


@pytest.mark.django_db
def test_cancellation_request_distinguishes_missing_invalid_and_stale_rows() -> None:
    missing = request_task_cancellation(999_999)
    assert missing.status is TaskCancellationRequestStatus.NOT_FOUND
    assert missing.state is None
    assert missing.attempt_number is None
    assert missing.execution_generation is None

    invalid = RayTaskExecution.objects.create(
        task_id="lifecycle-cancel-invalid-001",
        callable_path="testproject.tasks.add_numbers",
        execution_generation=9,
    )
    RayTaskExecution.objects.filter(pk=invalid.pk).update(state="CORRUPT")
    invalid_result = request_task_cancellation(invalid.pk)
    assert invalid_result.status is TaskCancellationRequestStatus.INVALID_STATE
    assert invalid_result.state == "CORRUPT"

    stale = RayTaskExecution.objects.create(
        task_id="lifecycle-cancel-stale-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        execution_generation=11,
    )
    stale_result = request_task_cancellation(
        stale.pk,
        expected_attempt_number=stale.attempt_number,
        expected_execution_generation=10,
    )
    assert stale_result.status is TaskCancellationRequestStatus.STALE_GENERATION
    assert stale_result.state == TaskState.RUNNING
    assert stale_result.attempt_number == stale.attempt_number
    assert stale_result.execution_generation == 11
    stale.refresh_from_db()
    assert stale.state == TaskState.RUNNING


@pytest.mark.django_db
def test_cancellation_attempt_fence_rejects_automatic_retry_replacement() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-cancel-auto-retry-race-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=7,
        queue_timeout_seconds=60,
    )
    stale_attempt = task.attempt_number
    stale_generation = task.execution_generation
    next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
    assert record_failure(
        task,
        error_message="automatic retry",
        retry=True,
        next_attempt_at=next_attempt_at,
    )

    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 3
    assert task.execution_generation == stale_generation
    assert task.run_after == next_attempt_at
    assert task.queue_deadline_at == next_attempt_at + timedelta(seconds=60)

    result = request_task_cancellation(
        task.pk,
        expected_attempt_number=stale_attempt,
        expected_execution_generation=stale_generation,
    )

    assert result.status is TaskCancellationRequestStatus.STALE_ATTEMPT
    assert result.state == TaskState.QUEUED
    assert result.attempt_number == 3
    assert result.execution_generation == stale_generation
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 3


@pytest.mark.django_db
def test_record_failure_rejects_replaced_execution() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-race-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        ray_job_id="new-job",
        execution_generation=2,
    )

    assert (
        record_failure(
            task,
            error_message="stale",
            retry=False,
            expected_ray_job_id="old-job",
            expected_execution_generation=1,
        )
        is False
    )
    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_record_failure_rejects_replaced_worker_owner() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-owner-race-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        claimed_by_worker="replacement-worker",
        attempt_number=2,
        execution_generation=7,
    )

    assert (
        record_failure(
            task,
            error_message="stale owner failure",
            retry=False,
            expected_claimed_by_worker="expired-worker",
            expected_attempt_number=2,
            expected_execution_generation=7,
        )
        is False
    )
    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.claimed_by_worker == "replacement-worker"
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("transition", ["failure", "success", "cancel"])
def test_terminal_transitions_reject_replaced_completion_envelope(transition: str) -> None:
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-completion-fence-{transition}-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        ray_job_id="raysubmit_completion_fence",
        attempt_number=2,
        execution_generation=7,
    )
    RayTaskExecution.objects.filter(pk=task.pk).update(
        completion_data='{"success": true, "result": 3}'
    )

    common = {
        "expected_ray_job_id": "raysubmit_completion_fence",
        "expected_attempt_number": 2,
        "expected_execution_generation": 7,
        "expected_completion_data": None,
        "require_completion_data_match": True,
    }
    if transition == "failure":
        persisted = record_failure(
            task,
            error_message="stale failure",
            retry=False,
            **common,
        )
    elif transition == "success":
        persisted = succeed_task(
            task,
            result_data="3",
            result_reference=None,
            **common,
        )
    else:
        persisted = cancel_task(
            task,
            allowed_states=(TaskState.RUNNING,),
            **common,
        )

    assert persisted is False
    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.completion_data == '{"success": true, "result": 3}'
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("transition", ["failure", "success", "lost", "cancel"])
def test_terminal_transitions_reject_replaced_attempt(transition: str) -> None:
    initial_state = TaskState.CANCELLING if transition == "cancel" else TaskState.RUNNING
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-attempt-fence-{transition}-001",
        callable_path="testproject.tasks.add_numbers",
        state=initial_state,
        attempt_number=3,
        execution_generation=7,
    )

    if transition == "failure":
        persisted = record_failure(
            task,
            error_message="stale failure",
            retry=False,
            expected_attempt_number=2,
            expected_execution_generation=7,
        )
    elif transition == "success":
        persisted = succeed_task(
            task,
            result_data='"stale"',
            result_reference=None,
            expected_attempt_number=2,
            expected_execution_generation=7,
        )
    elif transition == "lost":
        persisted = record_lost(
            task,
            error_message="stale owner",
            expected_attempt_number=2,
            expected_execution_generation=7,
        )
    else:
        persisted = cancel_task(
            task,
            expected_attempt_number=2,
            expected_execution_generation=7,
        )

    assert persisted is False
    task.refresh_from_db()
    assert task.state == initial_state
    assert task.result_data is None
    assert task.error_message is None
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("claimed_by_worker", "replacement-worker"),
        ("ray_job_id", "raysubmit_replacement"),
        ("last_heartbeat_at", datetime.now(UTC)),
    ],
)
def test_record_lost_rejects_refreshed_activity_snapshot(
    field: str,
    replacement: object,
) -> None:
    observed_heartbeat = datetime.now(UTC) - timedelta(minutes=10)
    task = RayTaskExecution.objects.create(
        task_id=f"lifecycle-lost-activity-fence-{field}-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        claimed_by_worker="stale-worker",
        ray_job_id="raysubmit_observed",
        last_heartbeat_at=observed_heartbeat,
        attempt_number=2,
        execution_generation=7,
    )
    RayTaskExecution.objects.filter(pk=task.pk).update(**{field: replacement})

    assert (
        record_lost(
            task,
            error_message="stale owner",
            expected_attempt_number=2,
            expected_execution_generation=7,
        )
        is False
    )

    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert getattr(task, field) == replacement
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_record_lost_rejects_durable_completion_envelope() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-lost-completion-fence-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        claimed_by_worker="stale-worker",
        ray_job_id="raysubmit_completed",
        last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        attempt_number=2,
        execution_generation=7,
    )
    RayTaskExecution.objects.filter(pk=task.pk).update(
        completion_data='{"success": true, "result": 3}'
    )

    assert (
        record_lost(
            task,
            error_message="stale owner",
            expected_attempt_number=2,
            expected_execution_generation=7,
        )
        is False
    )

    task.refresh_from_db()
    assert task.state == TaskState.RUNNING
    assert task.completion_data == '{"success": true, "result": 3}'
    assert not TaskAttempt.objects.filter(execution=task).exists()


@pytest.mark.django_db
def test_record_failure_clears_attempt_selection_when_retrying() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-selection-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        workflow_plan_selection='{"selected_strategy":"dynamic_tasks"}',
    )

    assert record_failure(task, error_message="retry", retry=True)

    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.workflow_plan_selection is None


@pytest.mark.django_db
def test_succeed_task_records_success_attempt_and_clears_errors() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-success-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.RUNNING,
        attempt_number=2,
        error_message="previous failure",
    )

    assert succeed_task(task, result_data="3", result_reference=None)

    task.refresh_from_db()
    assert task.state == TaskState.SUCCEEDED
    assert task.error_message is None
    history = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert history.state == TaskState.SUCCEEDED
    assert history.result_data == "3"
