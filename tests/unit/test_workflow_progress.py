"""Tests for workflow-run ownership and stale progress writers."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from django.db import IntegrityError

import django_ray.workflow.progress.runs as workflow_progress_module
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
from django_ray.workflow.progress.runs import (
    WORKFLOW_RUN_NAMESPACE_MAX,
    WORKFLOW_RUN_SEQUENCE_MAX,
    WorkflowRunAllocationError,
    allocate_workflow_run,
    claim_workflow_run,
    persist_workflow_progress,
    pin_workflow_plan,
    reclaim_workflow_run,
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


def _allocate(execution: RayTaskExecution) -> WorkflowRunIdentity:
    identity = allocate_workflow_run(
        DurableTaskContext(
            task_pk=execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
        )
    )
    assert identity is not None
    return identity


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
    context = DurableTaskContext(
        task_pk=1,
        attempt_number=1,
        execution_generation=1,
    )

    with pytest.raises(ValueError, match="must be supplied together"):
        allocate_workflow_run(context, plan=object())  # type: ignore[arg-type]


def test_workflow_run_id_encoding_is_injective_at_supported_boundaries() -> None:
    identities = {
        workflow_progress_module._workflow_run_id(namespace, sequence)
        for namespace, sequence in (
            (1, 1),
            (1, 2),
            (2, 1),
            (WORKFLOW_RUN_NAMESPACE_MAX, WORKFLOW_RUN_SEQUENCE_MAX),
        )
    }

    assert len(identities) == 4
    assert all(UUID(run_id).version == 8 for run_id in identities)


@pytest.mark.django_db
def test_fresh_allocation_reuses_namespace_and_advances_sequence_in_one_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = 0x1317A23B01FD4215
    candidate_calls: list[int] = []

    def candidate(bits: int) -> int:
        candidate_calls.append(bits)
        return namespace

    monkeypatch.setattr(workflow_progress_module, "randbits", candidate)
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-forced-same-scope-collision",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
    )

    first = _allocate(execution)
    assert persist_workflow_progress(first, _snapshot(first)) is True
    second = _allocate(execution)

    execution.refresh_from_db()
    assert first.run_id != second.run_id
    assert UUID(first.run_id).version == UUID(second.run_id).version == 8
    assert candidate_calls == [63]
    assert execution.workflow_run_namespace == namespace
    assert execution.workflow_run_sequence == 2
    assert str(execution.workflow_run_id) == second.run_id
    assert persist_workflow_progress(first, _snapshot(first, revision=2)) is False


@pytest.mark.django_db
def test_fresh_allocation_skips_a_migrated_current_candidate_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = 0x2317A23B01FD4215
    legacy_run_id = workflow_progress_module._workflow_run_id(namespace, 1)
    monkeypatch.setattr(workflow_progress_module, "randbits", lambda _bits: namespace)
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-forced-legacy-collision",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=3,
        workflow_run_id=legacy_run_id,
    )

    fresh_identity = _allocate(execution)

    execution.refresh_from_db()
    assert fresh_identity.run_id != legacy_run_id
    assert execution.workflow_run_namespace == namespace
    assert execution.workflow_run_sequence == 2
    assert str(execution.workflow_run_id) == fresh_identity.run_id


@pytest.mark.django_db
def test_one_namespace_stays_distinct_across_attempts_and_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = 0x3317A23B01FD4215
    monkeypatch.setattr(workflow_progress_module, "randbits", lambda _bits: namespace)
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-forced-cross-fence-collision",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=4,
    )
    first = _allocate(execution)
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        attempt_number=2,
        execution_generation=5,
        workflow_run_id=None,
    )
    execution.refresh_from_db()
    second = _allocate(execution)
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        execution_generation=6,
        workflow_run_id=None,
    )
    execution.refresh_from_db()
    third = _allocate(execution)

    execution.refresh_from_db()
    assert len({first.run_id, second.run_id, third.run_id}) == 3
    assert (first.attempt_number, first.execution_generation) == (1, 4)
    assert (second.attempt_number, second.execution_generation) == (2, 5)
    assert (third.attempt_number, third.execution_generation) == (2, 6)
    assert execution.workflow_run_namespace == namespace
    assert execution.workflow_run_sequence == 3


@pytest.mark.django_db
def test_namespace_collision_retries_under_database_unique_constraint(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collided_namespace = 0x4317A23B01FD4215
    replacement_namespace = 0x5317A23B01FD4215
    first_execution = RayTaskExecution.objects.create(
        task_id="workflow-run-cross-task-collision-a",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    second_execution = RayTaskExecution.objects.create(
        task_id="workflow-run-cross-task-collision-b",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )

    monkeypatch.setattr(
        workflow_progress_module,
        "randbits",
        lambda _bits: collided_namespace,
    )
    first = _allocate(first_execution)
    candidates = iter((collided_namespace, replacement_namespace))
    monkeypatch.setattr(
        workflow_progress_module,
        "randbits",
        lambda _bits: next(candidates),
    )
    with caplog.at_level(logging.WARNING, logger="django_ray.workflow.progress.runs"):
        second = _allocate(second_execution)

    assert first.run_id != second.run_id
    assert first.task_execution_pk != second.task_execution_pk
    first_execution.refresh_from_db()
    second_execution.refresh_from_db()
    assert first_execution.workflow_run_namespace == collided_namespace
    assert second_execution.workflow_run_namespace == replacement_namespace
    assert persist_workflow_progress(first, _snapshot(first)) is True
    assert persist_workflow_progress(second, _snapshot(second)) is True
    first_execution.refresh_from_db()
    second_execution.refresh_from_db()
    assert json.loads(first_execution.progress_data or "{}") == _snapshot(first)
    assert json.loads(second_execution.progress_data or "{}") == _snapshot(second)
    assert "retrying allocation" in caplog.text
    assert str(collided_namespace) not in caplog.text
    assert first_execution.task_id not in caplog.text
    assert second_execution.task_id not in caplog.text


@pytest.mark.django_db
def test_namespace_allocation_exhaustion_is_bounded_and_fail_closed(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collided_namespace = 0x6317A23B01FD4215
    RayTaskExecution.objects.create(
        task_id="workflow-run-namespace-collision-owner",
        callable_path="tests.unit.test_workflows.increment",
        workflow_run_namespace=collided_namespace,
    )
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-namespace-exhaustion",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    candidate_calls = 0

    def repeat_candidate(_bits: int) -> int:
        nonlocal candidate_calls
        candidate_calls += 1
        return collided_namespace

    monkeypatch.setattr(workflow_progress_module, "randbits", repeat_candidate)

    with (
        caplog.at_level(logging.WARNING, logger="django_ray.workflow.progress.runs"),
        pytest.raises(WorkflowRunAllocationError, match="after 3 attempts") as error,
    ):
        _allocate(execution)

    assert candidate_calls == 3
    assert len(str(error.value).encode("utf-8")) < 128
    assert execution.task_id not in str(error.value)
    execution.refresh_from_db()
    assert execution.workflow_run_namespace is None
    assert execution.workflow_run_id is None
    assert execution.workflow_run_sequence == 0
    assert str(collided_namespace) not in caplog.text
    assert execution.task_id not in caplog.text


@pytest.mark.django_db
def test_namespace_allocator_does_not_retry_unrelated_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-namespace-unrelated-integrity-error",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    candidate_calls = 0

    def invalid_candidate(_bits: int) -> int:
        nonlocal candidate_calls
        candidate_calls += 1
        return -1

    monkeypatch.setattr(workflow_progress_module, "randbits", invalid_candidate)

    with pytest.raises(IntegrityError):
        _allocate(execution)

    assert candidate_calls == 1
    execution.refresh_from_db()
    assert execution.workflow_run_namespace is None
    assert execution.workflow_run_sequence == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("updates", "expected_attempt", "expected_generation"),
    [
        ({"attempt_number": 3}, 3, 5),
        ({"execution_generation": 6}, 2, 6),
    ],
)
def test_exact_reclaim_scopes_a_reused_legacy_uuid_by_outer_fences(
    updates: dict[str, int],
    expected_attempt: int,
    expected_generation: int,
) -> None:
    run_id = "00000000-0000-4000-8000-000000000012"
    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-run-legacy-outer-fence-{expected_attempt}-{expected_generation}",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=5,
        workflow_run_id=run_id,
    )
    stale = _identity(execution, run_id)
    RayTaskExecution.objects.filter(pk=execution.pk).update(**updates)
    execution.refresh_from_db()
    current = _identity(execution, run_id)

    assert reclaim_workflow_run(stale) is False
    assert current.attempt_number == expected_attempt
    assert current.execution_generation == expected_generation
    assert reclaim_workflow_run(current) is True


@pytest.mark.django_db
def test_ambiguous_compatibility_claim_cannot_allocate_a_fresh_identity() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-compatibility-claim",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    identity = _identity(execution, "00000000-0000-4000-8000-000000000010")

    assert claim_workflow_run(identity) is False
    execution.refresh_from_db()
    assert execution.workflow_run_id is None
    assert execution.workflow_run_sequence == 0


@pytest.mark.django_db
def test_fresh_allocation_exhaustion_is_bounded_and_fail_closed() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-sequence-exhaustion",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
        workflow_run_namespace=0x7317A23B01FD4215,
        workflow_run_sequence=WORKFLOW_RUN_SEQUENCE_MAX,
    )

    with pytest.raises(
        WorkflowRunAllocationError,
        match="workflow run allocation sequence is exhausted",
    ) as error:
        _allocate(execution)

    assert len(str(error.value).encode("utf-8")) < 128
    assert execution.task_id not in str(error.value)
    execution.refresh_from_db()
    assert execution.workflow_run_id is None
    assert execution.workflow_run_namespace == 0x7317A23B01FD4215
    assert execution.workflow_run_sequence == WORKFLOW_RUN_SEQUENCE_MAX


@pytest.mark.django_db
def test_fresh_allocation_rejects_a_nonadvancing_identity_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_run_id = "00000000-0000-4000-8000-000000000011"
    execution = RayTaskExecution.objects.create(
        task_id="workflow-run-nonadvancing-generator",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        execution_generation=1,
        workflow_run_id=current_run_id,
    )
    monkeypatch.setattr(
        workflow_progress_module,
        "_workflow_run_id",
        lambda _namespace, _sequence: current_run_id,
    )
    monkeypatch.setattr(workflow_progress_module, "randbits", lambda _bits: 17)

    with pytest.raises(
        WorkflowRunAllocationError,
        match="workflow run allocation could not advance ownership",
    ) as error:
        _allocate(execution)

    assert len(str(error.value).encode("utf-8")) < 128
    assert execution.task_id not in str(error.value)
    execution.refresh_from_db()
    assert str(execution.workflow_run_id) == current_run_id
    assert execution.workflow_run_namespace is None
    assert execution.workflow_run_sequence == 0


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
    old = _allocate(execution)
    RayTaskExecution.objects.filter(pk=execution.pk).update(last_heartbeat_at=None)
    assert persist_workflow_progress(old, _snapshot(old)) is True
    execution.refresh_from_db()
    assert execution.last_heartbeat_at is not None
    replacement = _allocate(execution)
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
    _allocate(execution)
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
    identity = _allocate(execution)
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
    old = _allocate(execution)
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

    replacement = _allocate(execution)

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
    identity = _allocate(execution)
    run_storage = _run_storage(execution, identity.run_id)
    execution.refresh_from_db(fields=["workflow_run_namespace", "workflow_run_sequence"])
    allocated_namespace = execution.workflow_run_namespace
    allocated_sequence = execution.workflow_run_sequence
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        progress_data='{"revision":1}',
        workflow_progress_summary_json='{"summary_revision":1}',
    )

    assert reclaim_workflow_run(identity) is True

    execution.refresh_from_db()
    assert str(execution.workflow_run_id) == identity.run_id
    assert execution.progress_data is None
    assert execution.workflow_progress_summary_json is None
    assert execution.workflow_run_namespace == allocated_namespace
    assert execution.workflow_run_sequence == allocated_sequence
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
    old = _allocate(execution)
    old_storage = _run_storage(execution, old.run_id)

    def fail_save(*_args, **_kwargs) -> None:
        raise RuntimeError("roll back replacement claim")

    monkeypatch.setattr(RayTaskExecution, "save", fail_save)

    with pytest.raises(RuntimeError, match="roll back replacement claim"):
        allocate_workflow_run(
            DurableTaskContext(
                task_pk=execution.pk,
                attempt_number=execution.attempt_number,
                execution_generation=execution.execution_generation,
            )
        )

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
    stale = _allocate(execution)
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
    identity = _allocate(execution)

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
    identity = _allocate(execution)

    with pytest.raises(ValueError, match="unsupported schema"):
        persist_workflow_progress(identity, {**_snapshot(identity), "schema_version": 1})

    other = _identity(execution, "00000000-0000-0000-0000-000000000007")
    with pytest.raises(ValueError, match="identity does not match"):
        persist_workflow_progress(identity, _snapshot(other))
