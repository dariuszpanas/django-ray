"""Tests for race-safe lifecycle transitions and attempt history."""

from __future__ import annotations

import pytest

from django_ray.lifecycle import record_failure, retry_task, succeed_task
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
