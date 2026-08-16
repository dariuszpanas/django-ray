"""Lifecycle coverage for normalized workflow-progress detail retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from django_ray.lifecycle import (
    cancel_task,
    record_failure,
    record_lost,
    retry_task,
    succeed_task,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    WorkflowProgressRunStorage,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.summary import (
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)

RUN_ID = "00000000-0000-0000-0000-000000000126"


def _execution(*, state: str = TaskState.RUNNING) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id="workflow-terminal-detail-expiry",
        callable_path="tests.unit.test_workflows.increment",
        state=state,
        attempt_number=2,
        execution_generation=4,
        workflow_run_id=RUN_ID,
    )


def _identity(execution: RayTaskExecution) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=RUN_ID,
    )


def _running_summary(
    identity: WorkflowRunIdentity,
    *,
    detail_days: int = 7,
    published_detail: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "storage_protocol_version": 1,
        "run_identity": identity.as_dict(),
        "reporting_policy": "full",
        "selected_strategy": None,
        "plan_fingerprint": None,
        "limits_profile": "v1",
        "summary_revision": 1,
        "topology_version": 1 if published_detail else None,
        "detail_revision": 1 if published_detail else None,
        "state": "RUNNING",
        "node_counts": {
            "declared": 1,
            "discovered": 1,
            "retained_topology": 1 if published_detail else 0,
            "retained_detail": 1 if published_detail else 0,
            "pending": 1,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
        },
        "edge_counts": {
            "declared": 0,
            "discovered": 0,
            "retained_topology": 0,
        },
        "progress_percent": 0.0,
        "timestamps": {
            "started_at": "2026-07-20T12:00:00Z",
            "updated_at": "2026-07-20T12:00:01Z",
            "finished_at": None,
        },
        "detail": {
            "availability": "AVAILABLE" if published_detail else "NOT_REPORTED",
            "complete": published_detail,
            "truncation_reasons": [],
        },
        "storage": {
            "kind": "database",
            "manifest_id": "manifest_126" if published_detail else None,
        },
        "retention": {
            "detail_days": detail_days,
            "detail_expires_at": None,
        },
        "terminal": {"outcome": None, "finished_at": None},
    }


def _attach_summary(
    execution: RayTaskExecution,
    *,
    detail_days: int = 7,
    published_detail: bool = True,
) -> WorkflowRunIdentity:
    identity = _identity(execution)
    execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
        _running_summary(
            identity,
            detail_days=detail_days,
            published_detail=published_detail,
        ),
        expected_identity=identity,
    )
    execution.save(update_fields=["workflow_progress_summary_json"])
    return identity


def _run_storage(
    execution: RayTaskExecution,
    identity: WorkflowRunIdentity,
    *,
    retention_days: int = 7,
    expires_at: datetime | None = None,
    detail_revision: int = 1,
) -> WorkflowProgressRunStorage:
    return WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        run_id=identity.run_id,
        detail_revision=detail_revision,
        detail_retention_days=retention_days,
        detail_expires_at=expires_at,
    )


def _archived_summary(execution: RayTaskExecution) -> dict[str, Any]:
    attempt = TaskAttempt.objects.get(
        execution=execution,
        attempt_number=2,
    )
    assert attempt.workflow_progress_summary_json is not None
    return deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _canonical_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _assert_exact_expiry(
    execution: RayTaskExecution,
    run_storage: WorkflowProgressRunStorage,
    *,
    detail_days: int,
    terminal_state: str,
) -> dict[str, Any]:
    archived = _archived_summary(execution)
    finished_at = archived["terminal"]["finished_at"]
    assert isinstance(finished_at, str)
    expected = _timestamp(finished_at) + timedelta(days=detail_days)
    assert archived["state"] == terminal_state
    assert archived["terminal"]["outcome"] == terminal_state
    assert archived["retention"]["detail_expires_at"] == _canonical_utc(expected)

    run_storage.refresh_from_db()
    assert run_storage.detail_expires_at == expected
    return archived


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("transition", "terminal_state"),
    [
        ("success", TaskState.SUCCEEDED),
        ("permanent_failure", TaskState.FAILED),
        ("lost", TaskState.LOST),
        ("cancellation", TaskState.CANCELLED),
    ],
)
def test_outer_terminal_transition_stamps_detail_when_producer_only_reported_running(
    transition: str,
    terminal_state: str,
) -> None:
    execution = _execution()
    identity = _attach_summary(execution)
    run_storage = _run_storage(execution, identity)

    if transition == "success":
        assert succeed_task(execution, result_data="{}", result_reference=None)
    elif transition == "permanent_failure":
        assert record_failure(execution, error_message="failed", retry=False)
    elif transition == "lost":
        assert record_lost(execution, error_message="owner lost")
    else:
        RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.CANCELLING)
        execution.refresh_from_db()
        assert cancel_task(execution)

    _assert_exact_expiry(
        execution,
        run_storage,
        detail_days=7,
        terminal_state=terminal_state,
    )


@pytest.mark.django_db
def test_zero_day_retention_uses_the_exact_canonical_terminal_timestamp() -> None:
    execution = _execution()
    identity = _attach_summary(execution, detail_days=0)
    run_storage = _run_storage(execution, identity, retention_days=0)

    assert succeed_task(execution, result_data=None, result_reference=None)

    archived = _assert_exact_expiry(
        execution,
        run_storage,
        detail_days=0,
        terminal_state=TaskState.SUCCEEDED,
    )
    assert archived["retention"]["detail_expires_at"] == archived["terminal"]["finished_at"]


@pytest.mark.django_db
def test_automatic_retry_stamps_and_retains_the_completed_attempt_run() -> None:
    execution = _execution()
    identity = _attach_summary(execution, detail_days=3)
    run_storage = _run_storage(execution, identity, retention_days=3)

    assert record_failure(execution, error_message="retry", retry=True)

    execution.refresh_from_db()
    assert execution.state == TaskState.QUEUED
    assert execution.attempt_number == 3
    assert execution.workflow_run_id is None
    assert WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    _assert_exact_expiry(
        execution,
        run_storage,
        detail_days=3,
        terminal_state=TaskState.FAILED,
    )


@pytest.mark.django_db
def test_manual_retry_stamps_and_retains_the_completed_attempt_run() -> None:
    execution = _execution()
    identity = _attach_summary(execution, detail_days=5)
    run_storage = _run_storage(execution, identity, retention_days=5)
    finished_at = datetime(2026, 7, 20, 12, 0, 2, tzinfo=UTC)
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        state=TaskState.FAILED,
        finished_at=finished_at,
    )

    assert retry_task(execution.pk) is not None

    execution.refresh_from_db()
    assert execution.state == TaskState.QUEUED
    assert execution.attempt_number == 3
    assert execution.execution_generation == 5
    assert execution.workflow_run_id is None
    assert WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    _assert_exact_expiry(
        execution,
        run_storage,
        detail_days=5,
        terminal_state=TaskState.FAILED,
    )


@pytest.mark.django_db
def test_authoritative_terminal_summary_extends_an_earlier_producer_deadline() -> None:
    execution = _execution()
    identity = _identity(execution)
    producer_finished_at = "2026-07-20T12:00:02Z"
    producer_summary = _running_summary(identity)
    producer_summary["summary_revision"] = 2
    producer_summary["state"] = TaskState.FAILED
    producer_summary["node_counts"].update(pending=0, failed=1)
    producer_summary["timestamps"].update(
        updated_at=producer_finished_at,
        finished_at=producer_finished_at,
    )
    producer_summary["retention"]["detail_expires_at"] = "2026-07-27T12:00:02Z"
    producer_summary["terminal"] = {
        "outcome": TaskState.FAILED,
        "finished_at": producer_finished_at,
    }
    execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
        producer_summary,
        expected_identity=identity,
    )
    execution.save(update_fields=["workflow_progress_summary_json"])
    earlier_expiry = _timestamp(producer_summary["retention"]["detail_expires_at"])
    run_storage = _run_storage(execution, identity, expires_at=earlier_expiry)

    assert record_failure(execution, error_message="failed", retry=False)

    archived = _archived_summary(execution)
    assert archived["summary_revision"] == 3
    authoritative_expiry = _timestamp(archived["retention"]["detail_expires_at"])
    assert authoritative_expiry > earlier_expiry
    run_storage.refresh_from_db()
    assert run_storage.detail_expires_at == authoritative_expiry


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("summary_case", "retention_days"),
    [("missing", 4), ("corrupt", 0)],
)
def test_terminal_lifecycle_falls_back_to_persisted_policy_without_canonical_summary(
    summary_case: str,
    retention_days: int,
) -> None:
    execution = _execution()
    identity = _identity(execution)
    if summary_case == "corrupt":
        execution.workflow_progress_summary_json = "{not-json"
        execution.save(update_fields=["workflow_progress_summary_json"])
    run_storage = _run_storage(
        execution,
        identity,
        retention_days=retention_days,
    )

    assert succeed_task(execution, result_data=None, result_reference=None)

    attempt = TaskAttempt.objects.get(execution=execution, attempt_number=2)
    assert attempt.workflow_progress_summary_json is None
    assert attempt.finished_at is not None
    expected = attempt.finished_at + timedelta(days=retention_days)
    run_storage.refresh_from_db()
    assert run_storage.detail_retention_days == retention_days
    assert run_storage.detail_expires_at == expected


@pytest.mark.django_db
def test_canonical_terminal_summary_preserves_exact_detail_revision_fence() -> None:
    execution = _execution()
    identity = _attach_summary(execution)
    run_storage = _run_storage(execution, identity, detail_revision=2)

    assert succeed_task(execution, result_data=None, result_reference=None)

    archived = _archived_summary(execution)
    assert archived["detail_revision"] == 1
    run_storage.refresh_from_db()
    assert run_storage.detail_expires_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["summary_only", "invalid", "missing_run"])
def test_non_stampable_progress_never_blocks_the_outer_terminal_transition(case: str) -> None:
    execution = _execution()
    if case == "summary_only":
        _attach_summary(execution, published_detail=False)
    elif case == "invalid":
        execution.workflow_progress_summary_json = "{not-json"
        execution.save(update_fields=["workflow_progress_summary_json"])
    else:
        _attach_summary(execution)

    assert succeed_task(execution, result_data="{}", result_reference=None)

    execution.refresh_from_db()
    assert execution.state == TaskState.SUCCEEDED
    attempt = TaskAttempt.objects.get(execution=execution, attempt_number=2)
    if case == "invalid":
        assert attempt.workflow_progress_summary_json is None
    else:
        assert attempt.workflow_progress_summary_json is not None
        archived = deserialize_workflow_progress_summary(attempt.workflow_progress_summary_json)
        if case == "summary_only":
            assert archived["detail_revision"] is None
            assert archived["retention"]["detail_expires_at"] is None
        else:
            assert archived["detail_revision"] == 1
            assert archived["retention"]["detail_expires_at"] is not None
    assert not WorkflowProgressRunStorage.objects.filter(execution=execution).exists()
