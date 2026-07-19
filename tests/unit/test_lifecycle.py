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
        error_message="boom",
        error_traceback="RuntimeError: boom",
    )

    retried = retry_task(task)

    assert retried is not None
    task.refresh_from_db()
    assert task.state == TaskState.QUEUED
    assert task.attempt_number == 3
    assert task.execution_generation == 5
    assert task.input_reference == "s3://inputs/django-ray/inputs/immutable.json?bytes=42"
    assert task.error_message is None
    history = TaskAttempt.objects.get(execution=task, attempt_number=2)
    assert history.state == TaskState.FAILED
    assert history.error_message == "boom"


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
