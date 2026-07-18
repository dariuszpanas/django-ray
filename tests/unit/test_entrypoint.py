"""Unit tests for runtime entrypoint payload handling."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

import django_ray.runtime.entrypoint as entrypoint
from django_ray.models import RayTaskExecution, TaskState


class TestEntrypointPayload:
    """Tests for payload-based task execution path."""

    def test_execute_task_from_payload_decodes_and_dispatches(self, monkeypatch) -> None:
        """Payload decoding should forward values to execute_task unchanged."""
        payload = {
            "callable_path": "myapp.tasks.run",
            "serialized_args": '["arg"]',
            "serialized_kwargs": '{"key":"value"}',
        }
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

        captured: dict[str, object] = {}

        def fake_execute_task(
            callable_path: str,
            serialized_args: str,
            serialized_kwargs: str,
            **kwargs,
        ) -> str:
            captured["callable_path"] = callable_path
            captured["serialized_args"] = serialized_args
            captured["serialized_kwargs"] = serialized_kwargs
            captured.update(kwargs)
            return '{"success": true}'

        monkeypatch.setattr(entrypoint, "execute_task", fake_execute_task)

        result = entrypoint.execute_task_from_payload(payload_b64)

        assert result == '{"success": true}'
        assert captured == {
            **payload,
            "task_execution_pk": None,
            "attempt_number": None,
            "execution_generation": None,
            "runtime_env_profile": None,
            "runtime_env_hash": "",
        }

    def test_execute_task_from_payload_invalid_payload_returns_error_result(self) -> None:
        """Invalid payload should produce a structured failure JSON result."""
        result_json = entrypoint.execute_task_from_payload("%%%not-base64%%%")
        result = json.loads(result_json)

        assert result["success"] is False
        assert result["error"] is not None
        assert isinstance(result["exception_type"], str)

    def test_main_does_not_print_payload_execution_result(self, monkeypatch, capsys) -> None:
        """CLI main should keep the completion envelope out of Ray logs."""
        monkeypatch.setattr(
            entrypoint, "execute_task_from_payload", lambda _: '{"success":true,"result":"secret"}'
        )

        exit_code = entrypoint.main(["--payload-b64", "abc"])
        output = capsys.readouterr().out.strip()

        assert exit_code == 0
        assert output == "django-ray task completed successfully"
        assert "secret" not in output

    def test_bootstrap_requires_settings_module(self, monkeypatch) -> None:
        monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with pytest.raises(RuntimeError, match="DJANGO_SETTINGS_MODULE"):
            entrypoint.bootstrap_django()

    def test_bootstrap_initializes_django_when_apps_are_not_ready(self, monkeypatch) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
        monkeypatch.setattr(entrypoint, "apps", SimpleNamespace(ready=False))
        setup_calls: list[bool] = []
        monkeypatch.setattr(entrypoint.django, "setup", lambda: setup_calls.append(True))

        entrypoint.bootstrap_django()

        assert setup_calls == [True]

    def test_execute_task_exposes_durable_task_context(self, monkeypatch) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)

        def read_context() -> dict[str, object]:
            from django_ray.runtime.context import get_current_task_context

            context = get_current_task_context()
            assert context is not None
            return {
                "task_pk": context.task_pk,
                "runtime_env_profile": context.runtime_env_profile,
                "runtime_env_hash": context.runtime_env_hash,
                "ray_job_driver": context.ray_job_driver,
            }

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda path: read_context,
        )
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda value: {} if value == "{}" else [],
        )

        result_json = entrypoint.execute_task(
            "testproject.tasks.echo_task",
            "[]",
            "{}",
            task_execution_pk=42,
            runtime_env_profile="thin",
            runtime_env_hash="abc123",
        )

        result = json.loads(result_json)
        assert result["success"] is True, result
        assert result["result"] == {
            "task_pk": 42,
            "runtime_env_profile": "thin",
            "runtime_env_hash": "abc123",
            "ray_job_driver": True,
        }

    @pytest.mark.django_db
    def test_execute_task_persists_completion_envelope_for_current_attempt(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="entrypoint-completion-001",
            callable_path="testproject.tasks.echo_task",
            state=TaskState.RUNNING,
            attempt_number=3,
            execution_generation=7,
        )
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: lambda: {"value": 7},
        )
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda value: {} if value == "{}" else [],
        )

        result = json.loads(
            entrypoint.execute_task(
                task.callable_path,
                "[]",
                "{}",
                task_execution_pk=task.pk,
                attempt_number=3,
                execution_generation=7,
            )
        )

        task.refresh_from_db()
        assert result["success"] is True
        assert json.loads(task.completion_data or "{}") == result

    @pytest.mark.django_db
    def test_execute_task_does_not_overwrite_newer_generation_completion(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="entrypoint-completion-stale-001",
            callable_path="testproject.tasks.echo_task",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=2,
            completion_data='{"success": true, "result": "newer"}',
        )
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: lambda: "stale",
        )
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda value: {} if value == "{}" else [],
        )

        entrypoint.execute_task(
            task.callable_path,
            "[]",
            "{}",
            task_execution_pk=task.pk,
            attempt_number=2,
            execution_generation=1,
        )

        task.refresh_from_db()
        assert json.loads(task.completion_data or "{}")["result"] == "newer"

    @pytest.mark.django_db
    def test_execute_task_does_not_persist_without_attempt_number(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="entrypoint-completion-missing-attempt-001",
            callable_path="testproject.tasks.echo_task",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: lambda: "legacy",
        )
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda value: {} if value == "{}" else [],
        )

        entrypoint.execute_task(
            task.callable_path,
            "[]",
            "{}",
            task_execution_pk=task.pk,
        )

        task.refresh_from_db()
        assert task.completion_data is None

    @pytest.mark.django_db
    def test_execute_task_uses_result_reference_for_oversized_completion(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="entrypoint-completion-oversized-001",
            callable_path="testproject.tasks.echo_task",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: lambda: {"message": "x" * 256},
        )
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda value: {} if value == "{}" else [],
        )
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 64, "RESULT_STORAGE_BACKEND": "digest"},
        )

        result = json.loads(
            entrypoint.execute_task(
                task.callable_path,
                "[]",
                "{}",
                task_execution_pk=task.pk,
                attempt_number=1,
                execution_generation=1,
            )
        )

        task.refresh_from_db()
        assert result["result"] is None
        assert result["result_reference"].startswith("oversize://sha256/")
        assert len(task.completion_data or "") < 256
