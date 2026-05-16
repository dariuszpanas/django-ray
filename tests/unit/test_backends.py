"""Unit tests for Django task backend result handling."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from django.tasks.exceptions import TaskResultDoesNotExist

from django_ray.backends import RayTaskBackend
from django_ray.models import RayTaskExecution, TaskState
from django_ray.result_storage import ResultStorageError


def _make_backend() -> RayTaskBackend:
    return RayTaskBackend(
        "default",
        {
            "QUEUES": ["default"],
            "OPTIONS": {"RAY_ADDRESS": "auto"},
        },
    )


@pytest.mark.django_db
class TestRayTaskBackend:
    """Backend result retrieval coverage."""

    def test_enqueue_creates_execution_with_serialized_payload(self) -> None:
        from testproject.tasks import add_numbers

        task = add_numbers.using(queue_name="default")

        result = _make_backend().enqueue(task, args=(2, 3), kwargs={})
        execution = RayTaskExecution.objects.get(task_id=result.id)

        assert execution.callable_path == "testproject.tasks.add_numbers"
        assert execution.state == TaskState.QUEUED
        assert json.loads(execution.args_json) == [2, 3]
        assert json.loads(execution.kwargs_json) == {}

    def test_get_result_parses_inline_success_error_and_worker_metadata(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="backend-inline-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.FAILED,
            args_json="not-json",
            kwargs_json="not-json",
            error_message="boom",
            error_traceback="Traceback...\nValueError: boom",
            claimed_by_worker="worker-a",
            started_at=datetime.now(UTC) - timedelta(seconds=2),
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.args == []
        assert result.kwargs == {}
        assert result.worker_ids == ["worker-a"]
        assert result.errors[0].exception_class_path == "builtins.ValueError"

    def test_get_result_loads_return_value_from_result_reference(self, monkeypatch) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="backend-result-ref-001",
            callable_path="testproject.tasks.echo_task",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json='["hello"]',
            kwargs_json="{}",
            result_reference="resultfs://sha256/abc?rel=a/b.json&bytes=21",
        )

        monkeypatch.setattr(
            "django_ray.result_storage.load_result_reference",
            lambda reference: json.dumps({"reference": reference, "value": 42}),
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.return_value == {
            "reference": "resultfs://sha256/abc?rel=a/b.json&bytes=21",
            "value": 42,
        }

    def test_get_result_keeps_success_result_when_reference_load_fails(self, monkeypatch) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="backend-result-ref-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_reference="resultfs://sha256/missing?rel=a/b.json&bytes=21",
        )

        monkeypatch.setattr(
            "django_ray.result_storage.load_result_reference",
            lambda reference: (_ for _ in ()).throw(ResultStorageError(f"missing: {reference}")),
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.status.name == "SUCCESSFUL"
        assert result.return_value is None

    def test_get_result_raises_for_missing_execution(self) -> None:
        with pytest.raises(TaskResultDoesNotExist):
            _make_backend().get_result("missing-task-id")

    def test_check_reports_missing_ray_dependency(self, monkeypatch) -> None:
        backend = _make_backend()
        original_import = __import__

        def fake_import(name, global_ns=None, local_ns=None, fromlist=(), level=0):  # noqa: ANN001
            if name == "ray":
                raise ImportError("ray missing")
            return original_import(name, global_ns, local_ns, fromlist, level)

        monkeypatch.setattr("builtins.__import__", fake_import)
        monkeypatch.delitem(sys.modules, "ray", raising=False)

        errors = backend.check()

        assert len(errors) == 1
        assert errors[0].id == "django_ray.E001"
