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

from django_ray import __version__ as django_ray_version
from django_ray.backends import RayTaskBackend, TaskResultIdAllocationError
from django_ray.execution_protocol import (
    EXECUTION_METADATA_SCHEMA_VERSION,
    EXECUTION_PROTOCOL_VERSION,
)
from django_ray.input_storage import prepare_task_input
from django_ray.models import RayTaskCohortIntent, RayTaskExecution, TaskState
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
        assert execution.metadata_schema_version == EXECUTION_METADATA_SCHEMA_VERSION
        assert execution.execution_protocol_version == EXECUTION_PROTOCOL_VERSION
        assert execution.created_with_django_ray_version == django_ray_version
        assert execution.managed_with_django_ray_version is None
        assert execution.executor_django_ray_version is None
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
        observed_configs = []

        def record_storage(runtime_env, *, task_id, config):
            observed.append(runtime_env)
            observed_task_ids.append(task_id)
            observed_configs.append(config)
            return runtime_env_for_storage(runtime_env, task_id=task_id, config=config)

        monkeypatch.setattr("django_ray.backends.runtime_env_for_storage", record_storage)

        result = _make_backend().enqueue(
            add_numbers.using(queue_name="default"),
            args=(2, 3),
            kwargs={},
        )

        execution = RayTaskExecution.objects.get(task_id=result.id)
        assert len(observed) == 1
        assert observed_task_ids == [result.id]
        assert len(observed_configs) == 1 and observed_configs[0] is not None
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
        observed_configs = []

        def record_storage(runtime_env, *, task_id, config):
            observed_task_ids.append(task_id)
            observed_configs.append(config)
            return runtime_env_for_storage(runtime_env, task_id=task_id, config=config)

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
        assert len(observed_configs) == 2 and observed_configs[0] is observed_configs[1]
        assert observed_configs[0]["RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY"] == "backend-key"
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
            lambda _prepared, **_kwargs: (_ for _ in ()).throw(
                IntegrityError("input registry failed")
            ),
        )

        with pytest.raises(IntegrityError, match="input registry failed"):
            _make_backend().enqueue(add_numbers, args=(2, 3), kwargs={})

        assert candidate_calls == 1
        assert RayTaskExecution.objects.filter(task_id=candidate_id).count() == 1

    def test_runtime_env_storage_failure_creates_no_execution(self, monkeypatch) -> None:
        from testproject.tasks import add_numbers

        def reject_storage(_runtime_env, *, task_id, config):
            assert task_id and config is not None
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

    def test_backend_defaults_ray_job_queue_affinity_off(self) -> None:
        assert _make_backend().ray_job_only is False

    def test_backend_accepts_explicit_ray_job_queue_affinity(self) -> None:
        backend = RayTaskBackend(
            "jobs",
            {
                "QUEUES": ["batch"],
                "OPTIONS": {"RAY_ADDRESS": "auto", "RAY_JOB_ONLY": True},
            },
        )

        assert backend.ray_job_only is True

    @pytest.mark.parametrize("ray_job_only", [None, 0, 1, "true", [], {}])
    def test_backend_rejects_non_boolean_ray_job_queue_affinity(self, ray_job_only: object) -> None:
        with pytest.raises(ImproperlyConfigured, match="RAY_JOB_ONLY"):
            RayTaskBackend(
                "jobs",
                {
                    "QUEUES": ["batch"],
                    "OPTIONS": {
                        "RAY_ADDRESS": "auto",
                        "RAY_JOB_ONLY": ray_job_only,
                    },
                },
            )

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
            error_message="\x1b[31mboom\x1b[39m",
            error_traceback="\x1b[36mTraceback...\x1b[39m\r\nValueError: boom",
            claimed_by_worker="worker-a",
            started_at=datetime.now(UTC) - timedelta(seconds=2),
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.args == []
        assert result.kwargs == {}
        assert result.worker_ids == ["worker-a"]
        assert result.errors[0].exception_class_path == "builtins.ValueError"
        assert result.errors[0].traceback == "Traceback...\nValueError: boom"

    def test_get_result_redacts_legacy_oversized_diagnostics_before_projecting(
        self,
        monkeypatch,
    ) -> None:
        import django_ray.redaction as redaction

        monkeypatch.setattr(redaction, "_REDACTION_TEXT_MAX_CHARS", 128)
        execution = RayTaskExecution.objects.create(
            task_id="backend-oversized-diagnostic-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.FAILED,
            error_message="\x1b[31m" + ("message" * 40) + "\x1b[0m",
            error_traceback=(
                "\x1b[36mTraceback\x1b[0m\n" + ("frame-data" * 40) + "\nValueError: retained tail"
            ),
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.errors[0].exception_class_path == "builtins.Exception"
        assert result.errors[0].traceback == "[REDACTED]"

    def test_get_result_redacts_sensitive_failure_diagnostics(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="backend-sensitive-diagnostic-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.FAILED,
            error_message="password=hunter2",
            error_traceback="Traceback...\nValueError: password=hunter2",
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.errors[0].exception_class_path == "builtins.Exception"
        assert result.errors[0].traceback == "[REDACTED]"

    @pytest.mark.parametrize(
        "tail",
        (
            "field: malformed diagnostic",
            "os.path: attacker-controlled dotted path",
            "NotAnException: ordinary text",
        ),
    )
    def test_get_result_never_imports_an_exception_path_inferred_from_diagnostics(
        self,
        tail: str,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id=f"backend-untrusted-exception-path-{abs(hash(tail))}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.FAILED,
            error_message="task failed",
            error_traceback=f"Traceback...\n{tail}",
        )

        result = _make_backend().get_result(execution.task_id)

        assert result.errors[0].exception_class_path == "builtins.Exception"
        assert result.errors[0].exception_class is Exception

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


@pytest.mark.django_db(transaction=True)
class TestCohortEnqueue:
    @pytest.fixture(autouse=True)
    def cohort_protocol(self, _restore_execution_protocol_rollout_seed):
        from django_ray.models import TaskExecutionProtocolPolicy

        policy = TaskExecutionProtocolPolicy.objects.get(singleton_key=1)
        assert policy.active_write_protocol_version == EXECUTION_PROTOCOL_VERSION == 3
        assert not policy.legacy_worker_admission_enabled

    @staticmethod
    def enqueue(backend):
        from testproject.tasks import add_numbers

        result = backend.enqueue(add_numbers.using(queue_name="default"), args=(1, 2), kwargs={})
        return RayTaskExecution.objects.get(task_id=result.id)

    @staticmethod
    def pause(*, scopes=(), pause_enqueues=False, pause_claims=False):
        from django_ray.maintenance import read_maintenance_policy, replace_maintenance_policy

        return replace_maintenance_policy(
            scopes,
            pause_enqueues=pause_enqueues,
            pause_claims=pause_claims,
            expected_revision=read_maintenance_policy().revision,
            actor="test-operator",
            reason="enqueue-regression",
            authorized=True,
        )

    @pytest.mark.parametrize("scope", ["global", "queue", "protocol"])
    def test_paused_enqueue_refuses_before_input_even_in_application_transaction(
        self, monkeypatch, scope
    ):
        from django_ray.maintenance import MaintenanceAdmissionError, MaintenanceScope

        scopes = (
            ()
            if scope == "global"
            else (
                MaintenanceScope(
                    scope,
                    queue_name="default" if scope == "queue" else None,
                    protocol_version=3 if scope == "protocol" else None,
                    pause_enqueues=True,
                ),
            )
        )
        self.pause(scopes=scopes, pause_enqueues=scope == "global")
        monkeypatch.setattr(
            "django_ray.backends.prepare_task_input",
            lambda *a, **k: pytest.fail("paused admission cannot prepare external inputs"),
        )
        with transaction.atomic(), pytest.raises(MaintenanceAdmissionError, match="paused"):
            self.enqueue(_make_backend())
        assert not RayTaskExecution.objects.exists() and not RayTaskCohortIntent.objects.exists()

    def test_pause_after_preparation_is_rechecked_before_any_registry_or_execution_lock(
        self, settings, tmp_path, monkeypatch
    ):
        from django_ray.maintenance import MaintenanceAdmissionError
        from django_ray.models import TaskInputPayload

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "MAX_INLINE_INPUT_SIZE_BYTES": 0,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        original = prepare_task_input

        def prepare(*args, **kwargs):
            prepared = original(*args, **kwargs)
            self.pause(pause_enqueues=True)
            return prepared

        monkeypatch.setattr("django_ray.backends.prepare_task_input", prepare)
        monkeypatch.setattr(
            "django_ray.backends.register_task_input",
            lambda *a, **k: pytest.fail("pause barrier must precede registry locks"),
        )
        with pytest.raises(MaintenanceAdmissionError, match="paused"):
            self.enqueue(_make_backend())
        assert not RayTaskExecution.objects.exists() and not RayTaskCohortIntent.objects.exists()
        assert not TaskInputPayload.objects.exists()
        assert len(list(tmp_path.rglob("*.json"))) == 1

    def test_claim_pause_does_not_imply_enqueue_pause(self):
        self.pause(pause_claims=True)
        execution = self.enqueue(_make_backend())
        assert execution.cohort_intent.execution_id == execution.pk

    @pytest.mark.parametrize("jobs_only", [False, True])
    def test_intent_matches_manager_declaration_and_plaintext_snapshot(self, settings, jobs_only):
        from django_ray.conf.settings import get_settings
        from django_ray.runner.cohort_configuration import prepare_cohort_worker_configuration
        from django_ray.target.attestation import RayRunnerFamily

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "WORKFLOW_PLAN_TRUST_IDENTITY": {"trust_domain": "cafe\u0301"},
        }
        params = {
            "QUEUES": ["default"],
            "OPTIONS": {
                "RAY_ADDRESS": "https://EXACT:8265/",
                "RAY_JOB_ONLY": jobs_only,
                "RAY_RUNTIME_ENV": {
                    "pip": ["dynamic-package"],
                    "application_plugin": {"dynamic": True},
                },
            },
        }
        plan = prepare_cohort_worker_configuration(
            tasks={"current": params},
            validated_aliases=["current"],
            selected_queues=["default"],
            manager_settings=get_settings(),
            django_settings_module="testproject.settings",
            runner_family=RayRunnerFamily.RAY_JOB,
            execution_mode="ray",
        )
        execution = self.enqueue(RayTaskBackend("current", params))
        intent = execution.cohort_intent
        assert execution.execution_protocol_version == 3 and intent.schema_version == 2
        assert intent.package_version == django_ray_version
        assert intent.configuration_digest == plan.aliases[0].declaration_digest
        assert intent.selection_policy == ("jobs_only" if jobs_only else "worker_selected")
        assert intent.runtime_env_identity_digest == f"sha256:{execution.runtime_env_hash}"
        assert (
            execution.ray_target_address == "https://EXACT:8265/" and execution.ray_address is None
        )

    def test_global_fallback_and_trust_are_captured_once_before_input(self, settings, monkeypatch):
        from django_ray.conf.settings import get_settings
        from django_ray.target.cohort_intent import (
            cohort_declaration_digest,
            prepare_cohort_declaration,
        )

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RAY_ADDRESS": "old:6379",
            "WORKFLOW_PLAN_TRUST_IDENTITY": {"trust_domain": "original"},
        }
        backend = RayTaskBackend("current", {"QUEUES": ["default"]})
        settings.DJANGO_RAY["RAY_ADDRESS"] = "http://current:8265"
        expected = cohort_declaration_digest(
            prepare_cohort_declaration("current", options={}, current_settings=get_settings())
        )
        original = prepare_task_input

        def mutate(*args, **kwargs):
            settings.DJANGO_RAY["RAY_ADDRESS"] = "http://later:8265"
            settings.DJANGO_RAY["WORKFLOW_PLAN_TRUST_IDENTITY"]["trust_domain"] = "later"
            return original(*args, **kwargs)

        monkeypatch.setattr("django_ray.backends.prepare_task_input", mutate)
        execution = self.enqueue(backend)
        assert execution.ray_target_address == "http://current:8265"
        assert execution.cohort_intent.configuration_digest == expected
        assert backend.ray_target_address == "old:6379"  # Retained legacy attribute.

    @pytest.mark.parametrize("invalid", ["address", "alias", "trust"])
    def test_invalid_finite_intent_precedes_input_side_effects(
        self, settings, monkeypatch, invalid
    ):
        from django_ray.target.cohort_intent import CohortIntentError

        options = {"RAY_ADDRESS": "http://user:secret@host:8265"} if invalid == "address" else {}
        alias = "invalid alias" if invalid == "alias" else "default"
        if invalid == "trust":
            settings.DJANGO_RAY = {
                **settings.DJANGO_RAY,
                "WORKFLOW_PLAN_TRUST_IDENTITY": {"unexpected": "secret"},
            }
        backend = RayTaskBackend(alias, {"QUEUES": ["default"], "OPTIONS": options})
        monkeypatch.setattr(
            "django_ray.backends.prepare_task_input",
            lambda *a, **k: pytest.fail("intent must precede input publication"),
        )
        with pytest.raises(CohortIntentError) as caught:
            self.enqueue(backend)
        assert "secret" not in str(caught.value)
        assert not RayTaskExecution.objects.exists() and not RayTaskCohortIntent.objects.exists()

    def test_large_mapping_and_unreadable_code_path_do_not_trigger_planning(
        self, monkeypatch, tmp_path
    ):
        from pathlib import Path

        location = tmp_path / "unreadable-code"
        location.mkdir()
        original_stat = Path.stat

        def no_read(path, *args, **kwargs):
            if path == location:
                raise PermissionError("producer cannot inspect this tree")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", no_read)
        monkeypatch.setattr(
            "django_ray.workflow.plans.runtime_env_plan_identity",
            lambda *a, **k: pytest.fail("enqueue must not plan local content"),
        )
        spec = {
            "working_dir": str(location),
            "env_vars": {f"KEY_{index}": "value" for index in range(300)},
            "worker_process_setup_hook": "application.worker_hook",
            "application_plugin": {"new_field": True},
        }
        execution = self.enqueue(
            RayTaskBackend("default", {"QUEUES": ["default"], "OPTIONS": {"RAY_RUNTIME_ENV": spec}})
        )
        assert json.loads(execution.runtime_env_json) == spec
        assert (
            execution.cohort_intent.runtime_env_identity_digest
            == f"sha256:{execution.runtime_env_hash}"
        )

    def test_different_task_environments_do_not_split_admission(self):
        rows = [
            self.enqueue(
                RayTaskBackend(
                    "default",
                    {"QUEUES": ["default"], "OPTIONS": {"RAY_RUNTIME_ENV": {"pip": [package]}}},
                )
            )
            for package in ("first", "second")
        ]
        assert (
            rows[0].cohort_intent.configuration_digest == rows[1].cohort_intent.configuration_digest
        )
        assert (
            rows[0].cohort_intent.runtime_env_identity_digest
            != rows[1].cohort_intent.runtime_env_identity_digest
        )

    def test_intent_failure_rolls_back_execution_and_input_registry(
        self, settings, tmp_path, monkeypatch
    ):
        from django_ray.models import TaskInputPayload
        from django_ray.target.cohort_intent_storage import (
            CohortIntentStorageError,
            CohortIntentStorageRejection,
        )

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "MAX_INLINE_INPUT_SIZE_BYTES": 0,
            "INPUT_STORAGE_BACKEND": "filesystem",
            "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        calls = []

        def refuse(*args, **kwargs):
            calls.append(args[0])
            raise CohortIntentStorageError(CohortIntentStorageRejection.PERSISTENCE_REFUSED)

        monkeypatch.setattr("django_ray.backends.persist_cohort_intent", refuse)
        with pytest.raises(CohortIntentStorageError):
            self.enqueue(_make_backend())
        assert len(calls) == 1
        assert not RayTaskExecution.objects.exists() and not RayTaskCohortIntent.objects.exists()
        assert not TaskInputPayload.objects.exists()
        assert len(list(tmp_path.rglob("*.json"))) == 1  # External object is not transactional.

    def test_uuid_collision_rebinds_encryption_without_duplicate_intent_or_input(
        self, settings, monkeypatch
    ):
        collision, replacement = UUID(int=1), UUID(int=2)
        RayTaskExecution.objects.create(
            task_id=str(collision), callable_path="application.historical"
        )
        candidates = iter((collision, replacement))
        monkeypatch.setattr("django_ray.backends.uuid.uuid4", lambda: next(candidates))
        key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"current": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "current",
        }
        spec = {"env_vars": {"API_TOKEN": "cohort-private-token"}}
        prepared = []
        original = prepare_task_input

        def prepare(*args, **kwargs):
            prepared.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr("django_ray.backends.prepare_task_input", prepare)
        execution = self.enqueue(
            RayTaskBackend("default", {"QUEUES": ["default"], "OPTIONS": {"RAY_RUNTIME_ENV": spec}})
        )
        assert execution.task_id == str(replacement) and prepared == [True]
        assert RayTaskCohortIntent.objects.count() == 1
        assert (
            execution.cohort_intent.runtime_env_identity_digest
            == f"sha256:{execution.runtime_env_hash}"
        )
        assert runtime_env_for_execution(execution).spec == spec
        assert "cohort-private-token" not in execution.runtime_env_json
