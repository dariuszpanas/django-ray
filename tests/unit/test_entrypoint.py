"""Unit tests for runtime entrypoint payload handling."""

from __future__ import annotations

import base64
import json

import django_ray.runtime.entrypoint as entrypoint


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

        captured: dict[str, str] = {}

        def fake_execute_task(
            callable_path: str, serialized_args: str, serialized_kwargs: str
        ) -> str:
            captured["callable_path"] = callable_path
            captured["serialized_args"] = serialized_args
            captured["serialized_kwargs"] = serialized_kwargs
            return '{"success": true}'

        monkeypatch.setattr(entrypoint, "execute_task", fake_execute_task)

        result = entrypoint.execute_task_from_payload(payload_b64)

        assert result == '{"success": true}'
        assert captured == payload

    def test_execute_task_from_payload_invalid_payload_returns_error_result(self) -> None:
        """Invalid payload should produce a structured failure JSON result."""
        result_json = entrypoint.execute_task_from_payload("%%%not-base64%%%")
        result = json.loads(result_json)

        assert result["success"] is False
        assert result["error"] is not None
        assert isinstance(result["exception_type"], str)

    def test_main_prints_payload_execution_result(self, monkeypatch, capsys) -> None:
        """CLI main should print execution JSON and return zero."""
        monkeypatch.setattr(entrypoint, "execute_task_from_payload", lambda _: '{"ok":1}')

        exit_code = entrypoint.main(["--payload-b64", "abc"])
        output = capsys.readouterr().out.strip()

        assert exit_code == 0
        assert output == '{"ok":1}'
