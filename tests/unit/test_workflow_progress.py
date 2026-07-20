"""Tests for workflow-run ownership and stale progress writers."""

from __future__ import annotations

import json

import pytest

from django_ray.lifecycle import record_failure, retry_task
from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    DurableTaskContext,
    WorkflowRunIdentity,
)
from django_ray.workflow_progress import claim_workflow_run, persist_workflow_progress


def _identity(
    execution: RayTaskExecution,
    run_id: str,
    *,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=(execution.attempt_number if attempt_number is None else attempt_number),
        execution_generation=(
            execution.execution_generation if execution_generation is None else execution_generation
        ),
        run_id=run_id,
    )


def _snapshot(identity: WorkflowRunIdentity, revision: int = 1) -> dict[str, object]:
    return {
        "schema_version": WORKFLOW_PROGRESS_SCHEMA_VERSION,
        "workflow_id": f"django-ray:{identity.task_execution_pk}",
        "run_identity": identity.as_dict(),
        "revision": revision,
        "state": "RUNNING",
        "total_nodes": 1,
        "completed_nodes": 0,
        "failed_nodes": 0,
    }


def test_workflow_run_identity_requires_complete_durable_fence() -> None:
    assert WorkflowRunIdentity.create(DurableTaskContext(task_pk=1)) is None

    identity = WorkflowRunIdentity.create(
        DurableTaskContext(
            task_pk=1,
            attempt_number=2,
            execution_generation=3,
        )
    )

    assert identity is not None
    assert identity.task_execution_pk == 1
    assert identity.attempt_number == 2
    assert identity.execution_generation == 3
    assert identity.as_dict()["run_id"] == identity.run_id


@pytest.mark.django_db
def test_new_invocation_fences_an_older_writer_in_the_same_attempt() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-replacement",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    old = _identity(execution, "00000000-0000-0000-0000-000000000001")
    replacement = _identity(execution, "00000000-0000-0000-0000-000000000002")

    assert claim_workflow_run(old) is True
    assert persist_workflow_progress(old, _snapshot(old)) is True
    assert claim_workflow_run(replacement) is True
    assert persist_workflow_progress(old, _snapshot(old, revision=2)) is False

    execution.refresh_from_db()
    assert str(execution.workflow_run_id) == replacement.run_id
    assert execution.progress_data is None


@pytest.mark.django_db
def test_automatic_retry_clears_identity_and_rejects_late_writer() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-auto-retry",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=3,
    )
    stale = _identity(execution, "00000000-0000-0000-0000-000000000003")
    assert claim_workflow_run(stale) is True
    assert persist_workflow_progress(stale, _snapshot(stale)) is True

    assert record_failure(execution, error_message="retry", retry=True) is True
    assert persist_workflow_progress(stale, _snapshot(stale, revision=2)) is False

    execution.refresh_from_db()
    assert execution.state == TaskState.QUEUED
    assert execution.attempt_number == 2
    assert execution.workflow_run_id is None
    assert execution.progress_data is None


@pytest.mark.django_db
def test_manual_retry_clears_identity_and_rejects_terminal_writer() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-manual-retry",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=7,
        workflow_run_id="00000000-0000-0000-0000-000000000004",
        progress_data=json.dumps({"revision": 9}),
    )
    stale = _identity(execution, "00000000-0000-0000-0000-000000000004")

    assert retry_task(execution) is not None
    assert persist_workflow_progress(stale, _snapshot(stale, revision=10)) is False

    execution.refresh_from_db()
    assert execution.state == TaskState.QUEUED
    assert execution.attempt_number == 3
    assert execution.execution_generation == 8
    assert execution.workflow_run_id is None
    assert execution.progress_data is None


@pytest.mark.django_db
def test_progress_write_requires_running_lifecycle_and_exact_identity() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-terminal",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
    )
    identity = _identity(execution, "00000000-0000-0000-0000-000000000005")
    assert claim_workflow_run(identity) is True

    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.CANCELLING)

    assert persist_workflow_progress(identity, _snapshot(identity)) is False


@pytest.mark.django_db
def test_progress_persistence_rejects_mismatched_snapshot_protocol() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-invalid-snapshot",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    identity = _identity(execution, "00000000-0000-0000-0000-000000000006")
    assert claim_workflow_run(identity) is True

    with pytest.raises(ValueError, match="unsupported schema"):
        persist_workflow_progress(identity, {**_snapshot(identity), "schema_version": 1})

    other = _identity(execution, "00000000-0000-0000-0000-000000000007")
    with pytest.raises(ValueError, match="identity does not match"):
        persist_workflow_progress(identity, _snapshot(other))
