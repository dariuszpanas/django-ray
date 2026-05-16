"""Unit tests for cancellation helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runner.cancellation import finalize_cancellation, request_cancellation


@pytest.mark.django_db
class TestCancellationHelpers:
    """Tests for request/finalize cancellation behavior."""

    def test_request_cancellation_rejects_terminal_state(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="[]",
            kwargs_json="{}",
        )

        ok = request_cancellation(task, runner=SimpleNamespace(cancel=lambda handle: True))

        task.refresh_from_db()
        assert ok is False
        assert task.state == TaskState.SUCCEEDED

    def test_request_cancellation_marks_state_and_calls_runner(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-002",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_cancel_001",
            ray_address="ray://cluster:10001",
            started_at=datetime.now(UTC),
            args_json="[]",
            kwargs_json="{}",
        )

        seen: list[object] = []

        class Runner:
            def cancel(self, handle) -> bool:
                seen.append(handle)
                return True

        ok = request_cancellation(task, runner=Runner())

        task.refresh_from_db()
        assert ok is True
        assert task.state == TaskState.CANCELLING
        assert len(seen) == 1
        assert seen[0].ray_job_id == "raysubmit_cancel_001"
        assert seen[0].ray_address == "ray://cluster:10001"

    def test_request_cancellation_ignores_runner_errors(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-003",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_cancel_002",
            args_json="[]",
            kwargs_json="{}",
        )

        class Runner:
            def cancel(self, handle) -> bool:  # noqa: ARG002
                raise RuntimeError("ray unavailable")

        ok = request_cancellation(task, runner=Runner())

        task.refresh_from_db()
        assert ok is True
        assert task.state == TaskState.CANCELLING

    def test_finalize_cancellation_sets_terminal_state(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-004",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            args_json="[]",
            kwargs_json="{}",
        )

        finalize_cancellation(task)
        task.refresh_from_db()

        assert task.state == TaskState.CANCELLED
        assert task.finished_at is not None
