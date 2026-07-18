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
        assert json.loads(execution.runtime_env_json) == {}
        assert len(execution.runtime_env_hash) == 64

    def test_enqueue_persists_address_for_each_backend_alias(self) -> None:
        """Backend aliases retain their own Ray cluster for worker submission."""
        from testproject.tasks import add_numbers

        task = add_numbers.using(queue_name="default")
        backend_a = RayTaskBackend(
            "cluster_a",
            {"QUEUES": ["default"], "OPTIONS": {"RAY_ADDRESS": "ray://a:10001"}},
        )
        backend_b = RayTaskBackend(
            "cluster_b",
            {"QUEUES": ["default"], "OPTIONS": {"RAY_ADDRESS": "ray://b:10001"}},
        )

        result_a = backend_a.enqueue(task, args=(1, 2), kwargs={})
        result_b = backend_b.enqueue(task, args=(3, 4), kwargs={})

        assert RayTaskExecution.objects.get(task_id=result_a.id).ray_address == "ray://a:10001"
        assert RayTaskExecution.objects.get(task_id=result_b.id).ray_address == "ray://b:10001"

    def test_enqueue_snapshots_named_runtime_env_profile(self, settings) -> None:
        from testproject.tasks import add_numbers

        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_PROFILES": {
                "numpy": {
                    "pip": ["numpy==2.3.5"],
                    "env_vars": {"DJANGO_RAY_RUNTIME_ENV": "numpy"},
                }
            },
        }
        backend = RayTaskBackend(
            "numpy",
            {
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": "auto",
                    "RUNTIME_ENV_PROFILE": "numpy",
                },
            },
        )

        result = backend.enqueue(
            add_numbers.using(queue_name="default"),
            args=(2, 3),
            kwargs={},
        )
        execution = RayTaskExecution.objects.get(task_id=result.id)

        assert execution.runtime_env_profile == "numpy"
        assert json.loads(execution.runtime_env_json)["pip"] == ["numpy==2.3.5"]
        assert len(execution.runtime_env_hash) == 64

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

    def test_get_result_does_not_expose_stale_errors_for_success(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="backend-success-stale-error-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_data="3",
            error_message="transient failure from an earlier attempt",
            error_traceback="Traceback...\nRuntimeError: transient failure",
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.return_value == 3
        assert result.errors == []

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

    def test_get_result_warns_when_stored_reference_payload_is_invalid(
        self, monkeypatch, caplog
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="backend-result-ref-invalid-json",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_reference="resultfs://sha256/invalid?rel=a/b.json&bytes=8",
        )

        monkeypatch.setattr(
            "django_ray.result_storage.load_result_reference",
            lambda reference: "not-json",
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.return_value is None
        assert any(
            "Failed to decode stored task result payload" in record.getMessage()
            for record in caplog.records
        )

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

    def test_check_allows_uninitialized_ray(self, monkeypatch) -> None:
        backend = _make_backend()
        monkeypatch.setitem(sys.modules, "ray", type("Ray", (), {"is_initialized": lambda: False}))

        assert backend.check() == []
