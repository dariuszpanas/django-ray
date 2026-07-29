"""Tests for race-safe lifecycle transitions and attempt history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    cancel_task,
    record_failure,
    record_lost,
    request_task_cancellation,
    retry_task,
    succeed_task,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState


@pytest.mark.django_db
def test_retry_task_uses_one_based_counter_and_preserves_attempt() -> None:
    task = RayTaskExecution.objects.create(
        task_id="lifecycle-retry-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
        execution_generation=4,
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
    assert task.input_reference == "s3://inputs/django-ray/inputs/immutable.json?bytes=42"
    assert task.workflow_plan_selection is None
    assert task.error_message is None
    assert task.ray_target_address == "ray://target:10001"
    assert task.ray_address is None
    history = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert history.state == TaskState.FAILED
    assert history.error_message == "boom"


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
    )
    stale_attempt = task.attempt_number
    stale_generation = task.execution_generation
    assert record_failure(
        task,
        error_message="automatic retry",
        retry=True,
    )

    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 3
    assert task.execution_generation == stale_generation

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
