"""Unit tests for task reconciliation guard conditions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from django_ray.models import CancellationStatus, RayTaskExecution, TaskState
from django_ray.runner import reconciliation


def test_is_task_stuck_rejects_non_running_and_missing_activity() -> None:
    assert reconciliation.is_task_stuck(SimpleNamespace(state="SUCCEEDED")) is False
    assert (
        reconciliation.is_task_stuck(
            SimpleNamespace(state="RUNNING", last_heartbeat_at=None, started_at=None)
        )
        is False
    )


def test_is_task_timed_out_rejects_non_running_and_incomplete_tasks() -> None:
    assert reconciliation.is_task_timed_out(SimpleNamespace(state="FAILED")) is False
    assert (
        reconciliation.is_task_timed_out(
            SimpleNamespace(state="RUNNING", timeout_seconds=None, started_at=None)
        )
        is False
    )


@pytest.mark.django_db
def test_mark_task_timed_out_records_indeterminate_cancellation() -> None:
    task = RayTaskExecution.objects.create(
        task_id="timeout-indeterminate-001",
        callable_path="testproject.tasks.slow_task",
        state=TaskState.RUNNING,
        timeout_seconds=5,
        ray_job_id="raysubmit_timeout_indeterminate_001",
        execution_generation=3,
        args_json="[]",
        kwargs_json="{}",
    )

    marked = reconciliation.mark_task_timed_out(
        task,
        cancellation_status=CancellationStatus.INDETERMINATE,
        cancellation_error="Ray API did not confirm the stop request",
        expected_ray_job_id=task.ray_job_id,
        expected_execution_generation=3,
    )

    task.refresh_from_db()
    assert marked is True
    assert task.state == TaskState.FAILED
    assert task.cancellation_status == CancellationStatus.INDETERMINATE
    assert task.cancellation_error == "Ray API did not confirm the stop request"
    assert "indeterminate" in (task.error_message or "")
    assert (
        reconciliation.is_task_timed_out(
            SimpleNamespace(state="RUNNING", timeout_seconds=10, started_at=None)
        )
        is False
    )
