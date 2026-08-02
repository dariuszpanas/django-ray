"""Unit tests for Django task backend result handling."""

from __future__ import annotations

import base64
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist

from django_ray.backends import RayTaskBackend, TaskResultIdAllocationError
from django_ray.input_storage import prepare_task_input
from django_ray.models import RayTaskExecution, TaskState
from django_ray.result_storage import FilesystemResultStorage, ResultStorageError
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    runtime_env_for_execution,
    runtime_env_for_storage,
)


async def _async_backend_task(value: int) -> int:
    return value + 1


def _make_backend(
    *,
    timeout_seconds: int | None = None,
    queue_timeout_seconds: int | None = 86400,
) -> RayTaskBackend:
    options = {
        "RAY_ADDRESS": "auto",
        "TIMEOUT_SECONDS": timeout_seconds,
        "QUEUE_TIMEOUT_SECONDS": queue_timeout_seconds,
    }
    return RayTaskBackend(
        "default",
        {
            "QUEUES": ["default"],
            "OPTIONS": options,
        },
    )


@pytest.mark.django_db
class TestRayTaskBackend:
    """Backend result retrieval coverage."""

    def test_backend_advertises_priority_support(self) -> None:
        assert _make_backend().supports_priority is True

    @pytest.mark.parametrize(
        ("global_options", "expected"),
        [
            ({"RAY_ADDRESS": "auto"}, 86400),
            ({"RAY_ADDRESS": "auto", "QUEUE_TIMEOUT_SECONDS": 75}, 75),
        ],
    )
    def test_backend_uses_global_queue_timeout_fallback(
        self,
        settings,
        global_options,
        expected,
    ) -> None:
        settings.DJANGO_RAY = global_options

        backend = RayTaskBackend(
            "default",
            {"QUEUES": ["default"], "OPTIONS": {"RAY_ADDRESS": "auto"}},
        )

        assert backend.queue_timeout_seconds == expected

    def test_backend_snapshots_default_and_unlimited_queue_policy(self) -> None:
        from django.tasks.base import Task

        run_after = datetime.now(UTC) + timedelta(hours=2)
        task = Task(
            priority=0,
            func=_async_backend_task,
            backend="default",
            queue_name="default",
            run_after=run_after,
        )
        bounded = _make_backend(queue_timeout_seconds=60).enqueue(task, args=(1,), kwargs={})
        unlimited = _make_backend(queue_timeout_seconds=None).enqueue(task, args=(2,), kwargs={})

        bounded_row = RayTaskExecution.objects.get(task_id=bounded.id)
        unlimited_row = RayTaskExecution.objects.get(task_id=unlimited.id)
        assert bounded_row.queue_timeout_seconds == 60
        assert bounded_row.queue_deadline_at == run_after + timedelta(seconds=60)
        assert unlimited_row.queue_timeout_seconds is None
        assert unlimited_row.queue_deadline_at is None

    def test_expired_execution_maps_to_failed_task_result_with_stable_error(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="backend-expired-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.EXPIRED,
            args_json="[1, 2]",
            kwargs_json="{}",
            error_message="Task expired before execution after exceeding its queued-wait deadline",
        )

        result = _make_backend().get_result(execution.task_id)

        from django.tasks import TaskResultStatus

        assert result.status is TaskResultStatus.FAILED
        assert result.errors[0].traceback == execution.error_message

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "60", 2_147_483_648])
    def test_backend_rejects_invalid_queue_timeout(self, value) -> None:
        with pytest.raises(ImproperlyConfigured, match="QUEUE_TIMEOUT_SECONDS"):
            RayTaskBackend(
                "default",
                {
                    "QUEUES": ["default"],
                    "OPTIONS": {"RAY_ADDRESS": "auto", "QUEUE_TIMEOUT_SECONDS": value},
                },
            )

    def test_backend_keeps_legacy_address_attribute(self) -> None:
        assert RayTaskBackend("default", {"QUEUES": ["default"]}).ray_address == "auto"

    def test_backend_advertises_and_accepts_coroutine_tasks(self) -> None:
        from django.tasks.base import Task

        backend = _make_backend()
        task = Task(
            priority=0,
            func=_async_backend_task,
            backend="default",
            queue_name="default",
            run_after=None,
        )

        result = backend.enqueue(task, args=(4,), kwargs={})

        assert backend.supports_async_task is True
        execution = RayTaskExecution.objects.get(task_id=result.id)
        assert execution.callable_path == "tests.unit.test_backends._async_backend_task"

    @pytest.mark.parametrize("priority", [-100, 0, 100])
    def test_priority_boundaries_persist_and_round_trip(self, priority: int) -> None:
        from testproject.tasks import add_numbers

        backend = _make_backend()
        task = add_numbers.using(priority=priority)

        result = backend.enqueue(task, args=(2, 3), kwargs={})
        execution = RayTaskExecution.objects.get(task_id=result.id)

        assert execution.priority == priority
        assert backend.get_result(result.id).task.priority == priority

    @pytest.mark.parametrize("priority", [-101, 101, -1.5, 1.5])
    def test_django_rejects_invalid_priority(self, priority: float) -> None:
        from testproject.tasks import add_numbers

        with pytest.raises(InvalidTask, match="whole number between -100 and 100"):
            add_numbers.using(priority=priority)

    @pytest.mark.parametrize("priority", [-101, 101])
    def test_execution_constraint_rejects_out_of_range_priority(self, priority: int) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            RayTaskExecution.objects.create(
                task_id=f"invalid-priority-{priority}",
                callable_path="testproject.tasks.add_numbers",
                priority=priority,
            )

    def test_enqueue_creates_execution_with_serialized_payload(self) -> None:
        from testproject.tasks import add_numbers

        task = add_numbers.using(queue_name="default")

        result = _make_backend().enqueue(task, args=(2, 3), kwargs={})
        execution = RayTaskExecution.objects.get(task_id=result.id)

        assert execution.callable_path == "testproject.tasks.add_numbers"
        assert execution.priority == 0
        assert execution.state == TaskState.QUEUED
        assert json.loads(execution.args_json) == [2, 3]
        assert json.loads(execution.kwargs_json) == {}
        assert json.loads(execution.runtime_env_json) == {}
        assert len(execution.runtime_env_hash) == 64
        assert execution.timeout_seconds is None
        assert execution.ray_target_address == "auto"
        assert execution.ray_address is None

    def test_enqueue_uses_the_runtime_env_storage_seam(self, monkeypatch) -> None:
        from testproject.tasks import add_numbers

        observed = []

        observed_task_ids = []

        def record_storage(runtime_env, *, task_id):
            observed.append(runtime_env)
            observed_task_ids.append(task_id)
            return runtime_env_for_storage(runtime_env, task_id=task_id)

        monkeypatch.setattr("django_ray.backends.runtime_env_for_storage", record_storage)

        result = _make_backend().enqueue(
            add_numbers.using(queue_name="default"),
            args=(2, 3),
            kwargs={},
        )

        execution = RayTaskExecution.objects.get(task_id=result.id)
        assert len(observed) == 1
        assert observed_task_ids == [result.id]
        assert execution.runtime_env_json == observed[0].serialized
        assert execution.runtime_env_hash == observed[0].digest

    def test_enqueue_recovers_from_a_task_id_collision_and_rebinds_encryption(
        self,
        caplog,
        monkeypatch,
        settings,
    ) -> None:
        from testproject.tasks import add_numbers

        collided_id = "00000000-0000-4000-8000-000000000001"
        replacement_id = "00000000-0000-4000-8000-000000000002"
        RayTaskExecution.objects.create(
            task_id=collided_id,
            callable_path="testproject.tasks.add_numbers",
        )
        candidates = iter((UUID(collided_id), UUID(replacement_id)))
        monkeypatch.setattr("django_ray.backends.uuid.uuid4", lambda: next(candidates))

        marker = "collision-rebound-runtime-env-secret-99f1"
        key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"backend-key": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "backend-key",
        }
        run_after = datetime.now(UTC) + timedelta(hours=2)
        backend = RayTaskBackend(
            "encrypted",
            {
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": "auto",
                    "RAY_RUNTIME_ENV": {"env_vars": {"API_TOKEN": marker}},
                    "QUEUE_TIMEOUT_SECONDS": 90,
                },
            },
        )

        observed_task_ids: list[str] = []

        def record_storage(runtime_env, *, task_id):
            observed_task_ids.append(task_id)
            return runtime_env_for_storage(runtime_env, task_id=task_id)

        prepared_inputs = 0
        original_prepare = prepare_task_input

        def record_prepare(*args, **kwargs):
            nonlocal prepared_inputs
            prepared_inputs += 1
            return original_prepare(*args, **kwargs)

        monkeypatch.setattr("django_ray.backends.runtime_env_for_storage", record_storage)
        monkeypatch.setattr("django_ray.backends.prepare_task_input", record_prepare)

        with caplog.at_level(logging.WARNING, logger="django_ray.backend"):
            result = backend.enqueue(
                add_numbers.using(run_after=run_after),
                args=(2, 3),
                kwargs={},
            )

        execution = RayTaskExecution.objects.get(task_id=replacement_id)
        assert result.id == replacement_id
        assert RayTaskExecution.objects.filter(task_id=collided_id).count() == 1
        assert observed_task_ids == [collided_id, replacement_id]
        assert prepared_inputs == 1
        assert execution.queue_timeout_seconds == 90
        assert execution.queue_deadline_at == run_after + timedelta(seconds=90)
        assert runtime_env_for_execution(execution).spec == {"env_vars": {"API_TOKEN": marker}}
        assert "retrying allocation" in caplog.text
        assert collided_id not in caplog.text
        assert marker not in caplog.text

    def test_enqueue_fails_closed_after_bounded_task_id_collisions(
        self,
        caplog,
        monkeypatch,
    ) -> None:
        from testproject.tasks import add_numbers

        collided_id = "00000000-0000-4000-8000-000000000003"
        RayTaskExecution.objects.create(
            task_id=collided_id,
            callable_path="testproject.tasks.add_numbers",
        )
        candidate = UUID(collided_id)
        candidate_calls = 0

        def repeat_candidate():
            nonlocal candidate_calls
            candidate_calls += 1
            return candidate

        monkeypatch.setattr("django_ray.backends.uuid.uuid4", repeat_candidate)

        with (
            caplog.at_level(logging.WARNING, logger="django_ray.backend"),
            pytest.raises(TaskResultIdAllocationError, match="after 3 attempts"),
        ):
            _make_backend().enqueue(add_numbers, args=(2, 3), kwargs={})

        assert candidate_calls == 3
        assert RayTaskExecution.objects.filter(task_id=collided_id).count() == 1
        assert RayTaskExecution.objects.count() == 1
        assert collided_id not in caplog.text

    def test_enqueue_does_not_retry_an_unrelated_integrity_error(self, monkeypatch) -> None:
        candidate_id = "00000000-0000-4000-8000-000000000004"
        candidate_calls = 0

        def candidate():
            nonlocal candidate_calls
            candidate_calls += 1
            return UUID(candidate_id)

        monkeypatch.setattr("django_ray.backends.uuid.uuid4", candidate)
        invalid_task = SimpleNamespace(
            module_path="testproject.tasks.add_numbers",
            queue_name="default",
            priority=101,
            run_after=None,
        )

        with pytest.raises(IntegrityError):
            _make_backend().enqueue(invalid_task, args=(2, 3), kwargs={})

        assert candidate_calls == 1
        assert not RayTaskExecution.objects.exists()

    def test_enqueue_does_not_mask_an_unrelated_error_when_task_id_also_exists(
        self,
        monkeypatch,
    ) -> None:
        candidate_id = "00000000-0000-4000-8000-000000000005"
        RayTaskExecution.objects.create(
            task_id=candidate_id,
            callable_path="testproject.tasks.add_numbers",
        )
        candidate_calls = 0

        def candidate():
            nonlocal candidate_calls
            candidate_calls += 1
            return UUID(candidate_id)

        monkeypatch.setattr("django_ray.backends.uuid.uuid4", candidate)
        invalid_task = SimpleNamespace(
            module_path="testproject.tasks.add_numbers",
            queue_name="default",
            priority=101,
            run_after=None,
        )

        with pytest.raises(IntegrityError, match="ray_task_priority_valid_range"):
            _make_backend().enqueue(invalid_task, args=(2, 3), kwargs={})

        assert candidate_calls == 1
        assert RayTaskExecution.objects.filter(task_id=candidate_id).count() == 1

    def test_enqueue_does_not_classify_input_registration_failure_as_a_collision(
        self,
        monkeypatch,
    ) -> None:
        from testproject.tasks import add_numbers

        candidate_id = "00000000-0000-4000-8000-000000000006"
        RayTaskExecution.objects.create(
            task_id=candidate_id,
            callable_path="testproject.tasks.add_numbers",
        )
        candidate_calls = 0

        def candidate():
            nonlocal candidate_calls
            candidate_calls += 1
            return UUID(candidate_id)

        monkeypatch.setattr("django_ray.backends.uuid.uuid4", candidate)
        monkeypatch.setattr(
            "django_ray.backends.register_task_input",
            lambda _prepared: (_ for _ in ()).throw(IntegrityError("input registry failed")),
        )

        with pytest.raises(IntegrityError, match="input registry failed"):
            _make_backend().enqueue(add_numbers, args=(2, 3), kwargs={})

        assert candidate_calls == 1
        assert RayTaskExecution.objects.filter(task_id=candidate_id).count() == 1

    def test_runtime_env_storage_failure_creates_no_execution(self, monkeypatch) -> None:
        from testproject.tasks import add_numbers

        def reject_storage(_runtime_env, *, task_id):
            assert task_id
            raise RuntimeEnvSnapshotError(
                "django-ray: Resolved RuntimeEnv storage snapshot is invalid"
            )

        monkeypatch.setattr("django_ray.backends.runtime_env_for_storage", reject_storage)
        monkeypatch.setattr(
            "django_ray.backends.prepare_task_input",
            lambda *_args, **_kwargs: pytest.fail(
                "task input was prepared before RuntimeEnv storage validation"
            ),
        )

        with pytest.raises(RuntimeEnvSnapshotError, match="snapshot is invalid"):
            _make_backend().enqueue(
                add_numbers.using(queue_name="default"),
                args=(2, 3),
                kwargs={},
            )

        assert not RayTaskExecution.objects.exists()

    def test_encryption_configuration_failure_precedes_input_storage(
        self,
        monkeypatch,
        settings,
    ) -> None:
        from testproject.tasks import add_numbers

        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
        }
        monkeypatch.setattr(
            "django_ray.backends.prepare_task_input",
            lambda *_args, **_kwargs: pytest.fail(
                "task input was prepared before RuntimeEnv encryption configuration"
            ),
        )

        with pytest.raises(
            ImproperlyConfigured,
            match="RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY",
        ):
            _make_backend().enqueue(
                add_numbers.using(queue_name="default"),
                args=(2, 3),
                kwargs={},
            )

        assert not RayTaskExecution.objects.exists()

    def test_enqueue_encrypts_runtime_env_before_persisting(
        self,
        settings,
    ) -> None:
        from testproject.tasks import add_numbers

        marker = "arbitrary-enqueue-runtime-env-secret-61b8"
        key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"backend-key": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "backend-key",
        }
        backend = RayTaskBackend(
            "encrypted",
            {
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": "auto",
                    "RAY_RUNTIME_ENV": {"env_vars": {"API_TOKEN": marker}},
                },
            },
        )

        result = backend.enqueue(
            add_numbers.using(queue_name="default"),
            args=(2, 3),
            kwargs={},
        )

        execution = RayTaskExecution.objects.get(task_id=result.id)
        assert marker not in execution.runtime_env_json
        assert json.loads(execution.runtime_env_json)["format"] == (
            "django-ray.runtime-env.encrypted"
        )
        assert runtime_env_for_execution(execution).spec == {"env_vars": {"API_TOKEN": marker}}

    def test_enqueue_logs_only_runtime_env_identity(self, caplog) -> None:
        from testproject.tasks import add_numbers

        marker = "arbitrary-customer-marker-7cf3"
        backend = RayTaskBackend(
            "inline",
            {
                "QUEUES": ["default"],
                "OPTIONS": {
                    "RAY_ADDRESS": "auto",
                    "RAY_RUNTIME_ENV": {"env_vars": {"VALUE": marker}},
                },
            },
        )

        with caplog.at_level(logging.INFO, logger="django_ray.backend"):
            backend.enqueue(
                add_numbers.using(queue_name="default"),
                args=(2, 3),
                kwargs={},
            )

        assert caplog.records
        assert marker not in caplog.text
        assert all(marker not in repr(record.__dict__) for record in caplog.records)

    def test_enqueue_persists_backend_timeout(self) -> None:
        from testproject.tasks import add_numbers

        task = add_numbers.using(queue_name="default")

        result = _make_backend(timeout_seconds=45).enqueue(task, args=(2, 3), kwargs={})
        execution = RayTaskExecution.objects.get(task_id=result.id)

        assert execution.timeout_seconds == 45

    @pytest.mark.parametrize("timeout_seconds", [0, -1, True, False, 1.5, "30"])
    def test_backend_rejects_invalid_timeout(self, timeout_seconds: object) -> None:
        with pytest.raises(ImproperlyConfigured, match="TIMEOUT_SECONDS"):
            RayTaskBackend(
                "default",
                {
                    "QUEUES": ["default"],
                    "OPTIONS": {
                        "RAY_ADDRESS": "auto",
                        "TIMEOUT_SECONDS": timeout_seconds,
                    },
                },
            )

    @pytest.mark.parametrize("ray_address", [None, "", "   ", 123, True])
    def test_backend_rejects_invalid_ray_target(self, ray_address: object) -> None:
        with pytest.raises(ImproperlyConfigured, match="RAY_ADDRESS"):
            RayTaskBackend(
                "default",
                {
                    "QUEUES": ["default"],
                    "OPTIONS": {"RAY_ADDRESS": ray_address},
                },
            )

    def test_enqueue_persists_target_for_each_backend_alias(self) -> None:
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

        execution_a = RayTaskExecution.objects.get(task_id=result_a.id)
        execution_b = RayTaskExecution.objects.get(task_id=result_b.id)
        assert execution_a.ray_target_address == "ray://a:10001"
        assert execution_b.ray_target_address == "ray://b:10001"
        assert execution_a.ray_address is None
        assert execution_b.ray_address is None

    def test_enqueue_without_backend_target_snapshots_global_fallback(
        self,
        settings,
    ) -> None:
        from testproject.tasks import add_numbers

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RAY_ADDRESS": "ray://global:10001",
        }
        backend = RayTaskBackend("default", {"QUEUES": ["default"]})

        result = backend.enqueue(
            add_numbers.using(queue_name="default"),
            args=(1, 2),
            kwargs={},
        )

        execution = RayTaskExecution.objects.get(task_id=result.id)
        assert execution.ray_target_address == "ray://global:10001"
        assert execution.ray_address is None

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

    def test_get_result_rejects_tampered_external_payload_without_logging_reference(
        self, monkeypatch, caplog, tmp_path
    ) -> None:
        payload = json.dumps({"value": 42})
        storage = FilesystemResultStorage(tmp_path)
        reference = storage.store(serialized_result=payload)
        payload_path = next(tmp_path.rglob("*.json"))
        payload_path.write_text(json.dumps({"value": 99}), encoding="utf-8")
        execution = RayTaskExecution.objects.create(
            task_id="backend-result-ref-tampered",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_reference=reference,
        )
        monkeypatch.setattr(
            "django_ray.result_storage.get_settings",
            lambda: {"RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path)},
        )

        with caplog.at_level(logging.WARNING):
            result = _make_backend().get_result(execution.task_id)

        assert result.status.name == "SUCCESSFUL"
        assert result.return_value is None
        assert "Failed to load external task result" in caplog.text
        assert reference not in caplog.text

    def test_get_result_does_not_log_malicious_reference(self, caplog) -> None:
        reference = "s3://user:private-credential@bucket/object?bytes=1"
        execution = RayTaskExecution.objects.create(
            task_id="backend-result-ref-malicious",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.SUCCEEDED,
            args_json="[1, 2]",
            kwargs_json="{}",
            result_reference=reference,
        )

        with caplog.at_level(logging.WARNING):
            result = _make_backend().get_result(execution.task_id)

        assert result.return_value is None
        assert "Failed to load external task result" in caplog.text
        assert "private-credential" not in caplog.text
        assert reference not in caplog.text

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
