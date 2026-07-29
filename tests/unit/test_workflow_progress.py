"""Tests for workflow-run ownership and stale progress writers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from django_ray.lifecycle import record_failure, retry_task
from django_ray.models import (
    RayTaskExecution,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressNodeState,
    WorkflowProgressRunStorage,
)
from django_ray.runner.reconciliation import mark_task_lost
from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    DurableTaskContext,
    WorkflowInvocationIdentity,
    WorkflowRunIdentity,
)
from django_ray.workflow_progress import (
    claim_workflow_run,
    persist_workflow_progress,
    pin_workflow_plan,
    refresh_workflow_run_activity,
)


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


def _run_storage(
    execution: RayTaskExecution,
    run_id: str,
    *,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> WorkflowProgressRunStorage:
    return WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=(execution.attempt_number if attempt_number is None else attempt_number),
        execution_generation=(
            execution.execution_generation if execution_generation is None else execution_generation
        ),
        run_id=run_id,
    )


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


def test_workflow_invocation_identity_extends_without_changing_run_serialization() -> None:
    run_identity = WorkflowRunIdentity(
        task_execution_pk=7,
        attempt_number=2,
        execution_generation=3,
        run_id="00000000-0000-0000-0000-000000000101",
    )
    original_run_snapshot = run_identity.as_dict()
    invocation = WorkflowInvocationIdentity(
        run_identity=run_identity,
        invocation_id="00000000-0000-0000-0000-000000000102",
    )

    assert (
        run_identity.as_dict()
        == original_run_snapshot
        == {
            "schema_version": 1,
            "run_id": "00000000-0000-0000-0000-000000000101",
            "task_execution_pk": 7,
            "attempt_number": 2,
            "execution_generation": 3,
        }
    )
    assert invocation.as_dict() == {
        "schema_version": 1,
        "task_execution_pk": 7,
        "attempt_number": 2,
        "execution_generation": 3,
        "run_id": "00000000-0000-0000-0000-000000000101",
        "invocation_id": "00000000-0000-0000-0000-000000000102",
    }


def test_workflow_invocation_identity_factory_preserves_parent_fence() -> None:
    run_identity = WorkflowRunIdentity(
        task_execution_pk=8,
        attempt_number=4,
        execution_generation=9,
        run_id="00000000-0000-0000-0000-000000000103",
    )

    invocation = WorkflowInvocationIdentity.create(run_identity)

    assert invocation.run_identity is run_identity
    assert invocation.task_execution_pk == 8
    assert invocation.attempt_number == 4
    assert invocation.execution_generation == 9
    assert invocation.run_id == run_identity.run_id
    assert invocation.invocation_id


def test_claim_workflow_run_requires_plan_and_selection_together() -> None:
    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=1,
        run_id="00000000-0000-0000-0000-000000000010",
    )

    with pytest.raises(ValueError, match="must be supplied together"):
        claim_workflow_run(identity, plan=object())  # type: ignore[arg-type]


def test_pin_workflow_plan_requires_complete_durable_fence() -> None:
    assert (
        pin_workflow_plan(
            DurableTaskContext(task_pk=1),
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
        is False
    )


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
    RayTaskExecution.objects.filter(pk=execution.pk).update(last_heartbeat_at=None)
    assert persist_workflow_progress(old, _snapshot(old)) is True
    execution.refresh_from_db()
    assert execution.last_heartbeat_at is not None
    assert claim_workflow_run(replacement) is True
    assert persist_workflow_progress(old, _snapshot(old, revision=2)) is False

    execution.refresh_from_db()
    assert str(execution.workflow_run_id) == replacement.run_id
    assert execution.progress_data is None


@pytest.mark.django_db
def test_workflow_run_claim_refreshes_activity_and_fences_stale_lost() -> None:
    observed_heartbeat = datetime.now(UTC) - timedelta(minutes=10)
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-claim-activity",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
        last_heartbeat_at=observed_heartbeat,
    )
    stale_lost_snapshot = RayTaskExecution.objects.get(pk=execution.pk)
    identity = _identity(execution, "00000000-0000-0000-0000-000000000008")

    assert claim_workflow_run(identity) is True
    assert mark_task_lost(stale_lost_snapshot) is False

    execution.refresh_from_db()
    assert execution.state == TaskState.RUNNING
    assert execution.last_heartbeat_at is not None
    assert execution.last_heartbeat_at > observed_heartbeat


@pytest.mark.django_db
def test_current_run_activity_refresh_fences_stale_lost_before_leaf_submission() -> None:
    observed_heartbeat = datetime.now(UTC) - timedelta(minutes=10)
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-post-package-activity",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
        last_heartbeat_at=observed_heartbeat,
    )
    identity = _identity(execution, "00000000-0000-0000-0000-000000000009")
    assert claim_workflow_run(identity) is True
    RayTaskExecution.objects.filter(pk=execution.pk).update(last_heartbeat_at=observed_heartbeat)
    stale_lost_snapshot = RayTaskExecution.objects.get(pk=execution.pk)

    assert refresh_workflow_run_activity(identity) is True
    assert mark_task_lost(stale_lost_snapshot) is False

    execution.refresh_from_db()
    assert execution.state == TaskState.RUNNING
    assert execution.last_heartbeat_at is not None
    assert execution.last_heartbeat_at > observed_heartbeat


@pytest.mark.django_db
def test_replacement_claim_deletes_only_exact_same_attempt_run_storage() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-storage-replacement",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    old = _identity(execution, "00000000-0000-0000-0000-000000000101")
    replacement = _identity(execution, "00000000-0000-0000-0000-000000000102")
    assert claim_workflow_run(old) is True
    exact_old_storage = _run_storage(execution, old.run_id)
    prior_attempt_storage = _run_storage(
        execution,
        old.run_id,
        attempt_number=1,
        execution_generation=4,
    )
    unrelated_storage = _run_storage(
        execution,
        "00000000-0000-0000-0000-000000000103",
    )
    detail_payload = b'{"node_id":"node-a","state":"RUNNING"}'
    old_detail = WorkflowProgressNodeDetail.objects.create(
        run_storage=exact_old_storage,
        node_key=sha256(b"node-a").hexdigest(),
        node_id="node-a",
        state=WorkflowProgressNodeState.RUNNING,
        event_count=0,
        payload=detail_payload,
        digest=sha256(detail_payload).hexdigest(),
        encoded_bytes=len(detail_payload),
        decoded_bytes=len(detail_payload),
        last_topology_version=1,
        last_detail_revision=1,
    )

    assert claim_workflow_run(replacement) is True

    execution.refresh_from_db()
    assert str(execution.workflow_run_id) == replacement.run_id
    assert not WorkflowProgressRunStorage.objects.filter(pk=exact_old_storage.pk).exists()
    assert not WorkflowProgressNodeDetail.objects.filter(pk=old_detail.pk).exists()
    assert WorkflowProgressRunStorage.objects.filter(pk=prior_attempt_storage.pk).exists()
    assert WorkflowProgressRunStorage.objects.filter(pk=unrelated_storage.pk).exists()


@pytest.mark.django_db
def test_reclaiming_same_run_keeps_storage_and_clears_existing_snapshots() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-storage-idempotent-claim",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    identity = _identity(execution, "00000000-0000-0000-0000-000000000104")
    assert claim_workflow_run(identity) is True
    run_storage = _run_storage(execution, identity.run_id)
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        progress_data='{"revision":1}',
        workflow_progress_summary_json='{"summary_revision":1}',
    )

    assert claim_workflow_run(identity) is True

    execution.refresh_from_db()
    assert str(execution.workflow_run_id) == identity.run_id
    assert execution.progress_data is None
    assert execution.workflow_progress_summary_json is None
    assert WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()


@pytest.mark.django_db
def test_replacement_claim_rolls_back_storage_deletion_when_task_update_fails(
    monkeypatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-storage-rollback",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )
    old = _identity(execution, "00000000-0000-0000-0000-000000000105")
    replacement = _identity(execution, "00000000-0000-0000-0000-000000000106")
    assert claim_workflow_run(old) is True
    old_storage = _run_storage(execution, old.run_id)

    def fail_save(*_args, **_kwargs) -> None:
        raise RuntimeError("roll back replacement claim")

    monkeypatch.setattr(RayTaskExecution, "save", fail_save)

    with pytest.raises(RuntimeError, match="roll back replacement claim"):
        claim_workflow_run(replacement)

    execution.refresh_from_db()
    assert str(execution.workflow_run_id) == old.run_id
    assert WorkflowProgressRunStorage.objects.filter(pk=old_storage.pk).exists()


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
