"""Unit tests for runtime entrypoint payload handling."""

from __future__ import annotations

import asyncio
import base64
import json
import runpy
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

import django_ray.runtime.entrypoint as entrypoint
from django_ray.execution_codec import (
    ExecutionCompletionSource,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionRequestRejection,
    decode_execution_completion,
    encode_execution_request,
)
from django_ray.models import RayTaskExecution, TaskState
from django_ray.ray_job_protocol import (
    RAY_JOB_CONFIG_JSON_ENV_VAR,
    RAY_JOB_REQUEST_REJECTED_EXIT_CODE,
    build_ray_job_request_metadata,
)
from django_ray.workflow_plans import WorkflowPlanMismatchError


def _payload_b64(serialized: str) -> str:
    return base64.urlsafe_b64encode(serialized.encode("utf-8")).decode("ascii")


def _strict_request(
    *,
    identity: ExecutionIdentity | None = None,
    transport_version: int = 1,
    compiled_graph_submission_transport: str = "ray-job",
) -> tuple[ExecutionRequest, str]:
    identity = identity or ExecutionIdentity(
        task_execution_pk=361,
        task_id="00000000-0000-4000-8000-000000000361",
        attempt_number=2,
        execution_generation=7,
    )
    input_reference = None
    serialized_args = '["value"]'
    serialized_kwargs = '{"flag":true}'
    if transport_version == 2:
        input_reference = "s3://task-inputs/django-ray/strict-request.json"
        serialized_args = "null"
        serialized_kwargs = "null"
    request = ExecutionRequest(
        identity=identity,
        execution_protocol_version=1,
        callable_path="tests.strict_task",
        transport_version=transport_version,
        serialized_args=serialized_args,
        serialized_kwargs=serialized_kwargs,
        input_reference=input_reference,
        runtime_env_profile="strict",
        runtime_env_hash="a" * 64,
        runtime_env_plan_identity={"manifest": "strict"},
        compiled_graph_submission_transport=compiled_graph_submission_transport,
    )
    return request, encode_execution_request(request)


def _strict_config(request: ExecutionRequest, serialized: str) -> str:
    return json.dumps(
        {
            "runtime_env": {},
            "metadata": build_ray_job_request_metadata(request, serialized),
        }
    )


def _booby_trap_application_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("strict rejection crossed the application boundary")

    monkeypatch.setattr(entrypoint, "execute_task", forbidden)
    monkeypatch.setattr(entrypoint, "bootstrap_django", forbidden)
    monkeypatch.setattr(entrypoint, "load_task_input", forbidden)
    monkeypatch.setattr(entrypoint, "_invoke_task_callable", forbidden)
    monkeypatch.setattr(entrypoint, "_persist_task_completion", forbidden)
    monkeypatch.setattr("django_ray.runtime.import_utils.import_callable", forbidden)
    monkeypatch.setattr("django_ray.runtime.context.durable_task_execution", forbidden)


class TestEntrypointPayload:
    """Tests for payload-based task execution path."""

    def test_execute_task_from_payload_decodes_and_dispatches(self, monkeypatch) -> None:
        """Payload decoding should forward values to execute_task unchanged."""
        payload = {
            "callable_path": "myapp.tasks.run",
            "serialized_args": '["arg"]',
            "serialized_kwargs": '{"key":"value"}',
            "task_id": "00000000-0000-4000-8000-000000000123",
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
            "runtime_env_plan_identity": None,
            "input_reference": None,
        }

    @pytest.mark.parametrize("transport_version", [1, 2])
    def test_strict_payload_validates_before_dispatch_and_enriches_completion(
        self,
        monkeypatch,
        transport_version: int,
    ) -> None:
        request, serialized = _strict_request(transport_version=transport_version)
        monkeypatch.setenv(
            RAY_JOB_CONFIG_JSON_ENV_VAR,
            _strict_config(request, serialized),
        )
        captured: dict[str, object] = {}

        def fake_execute_task(
            callable_path: str,
            serialized_args: str,
            serialized_kwargs: str,
            **kwargs,
        ) -> str:
            captured.update(
                callable_path=callable_path,
                serialized_args=serialized_args,
                serialized_kwargs=serialized_kwargs,
                **kwargs,
            )
            return entrypoint._serialize_completion(
                success=True,
                result={"accepted": True},
                result_reference=None,
                error=None,
                error_traceback=None,
                exception_type=None,
                retryable=None,
                completion_identity=kwargs["_completion_identity"],
                execution_protocol_version=kwargs["_execution_protocol_version"],
            )

        monkeypatch.setattr(entrypoint, "execute_task", fake_execute_task)

        encoded = entrypoint.execute_task_from_payload(_payload_b64(serialized))
        decoded = decode_execution_completion(
            encoded,
            expected_identity=request.identity,
            expected_execution_protocol_version=request.execution_protocol_version,
        )

        assert decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
        assert decoded.completion.result == {"accepted": True}
        assert captured == {
            "callable_path": request.callable_path,
            "serialized_args": request.serialized_args,
            "serialized_kwargs": request.serialized_kwargs,
            "task_execution_pk": request.identity.task_execution_pk,
            "task_id": request.identity.task_id,
            "attempt_number": request.identity.attempt_number,
            "execution_generation": request.identity.execution_generation,
            "runtime_env_profile": request.runtime_env_profile,
            "runtime_env_hash": request.runtime_env_hash,
            "runtime_env_plan_identity": request.runtime_env_plan_identity,
            "input_reference": request.input_reference,
            "ray_job_driver": True,
            "_completion_identity": request.identity,
            "_execution_protocol_version": request.execution_protocol_version,
        }

    def test_legacy_payload_accepts_released_ray_job_metadata(self, monkeypatch) -> None:
        payload = {
            "callable_path": "myapp.tasks.legacy",
            "serialized_args": "[]",
            "serialized_kwargs": "{}",
            "task_execution_pk": 44,
            "task_id": "legacy-task",
            "attempt_number": 3,
            "execution_generation": 9,
        }
        legacy_metadata = {
            "django_ray_task_id": "44",
            "django_ray_attempt_number": "3",
            "django_ray_execution_generation": "9",
            "callable_path": payload["callable_path"],
            "runtime_env_profile": "",
            "runtime_env_hash": "b" * 64,
        }
        monkeypatch.setenv(
            RAY_JOB_CONFIG_JSON_ENV_VAR,
            json.dumps({"runtime_env": {}, "metadata": legacy_metadata}),
        )
        monkeypatch.setattr(entrypoint, "execute_task", lambda **_kwargs: '{"success":true}')

        result = entrypoint.execute_task_from_payload(
            _payload_b64(json.dumps(payload, separators=(",", ":")))
        )

        assert result == '{"success":true}'

    @pytest.mark.parametrize(
        ("mismatch", "classification"),
        [
            ("identity", ExecutionRequestRejection.IDENTITY_MISMATCH),
            ("protocol", ExecutionRequestRejection.PROTOCOL_MISMATCH),
            ("digest", ExecutionRequestRejection.INVALID_VERSIONED),
            ("transport", ExecutionRequestRejection.UNSUPPORTED_TRANSPORT),
        ],
    )
    def test_strict_request_mismatch_rejects_before_every_application_seam(
        self,
        monkeypatch,
        mismatch: str,
        classification: ExecutionRequestRejection,
    ) -> None:
        request, serialized = _strict_request()
        submitted = serialized
        if mismatch == "identity":
            different_identity = replace(request.identity, task_execution_pk=999)
            submitted = encode_execution_request(replace(request, identity=different_identity))
        elif mismatch == "protocol":
            payload = json.loads(serialized)
            payload["execution_protocol_version"] = 2
            submitted = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        elif mismatch == "digest":
            submitted = encode_execution_request(
                replace(request, callable_path="tests.strict_task_changed")
            )
        else:
            submitted = encode_execution_request(
                replace(request, compiled_graph_submission_transport="direct-ray-core")
            )
        monkeypatch.setenv(
            RAY_JOB_CONFIG_JSON_ENV_VAR,
            _strict_config(request, serialized),
        )
        _booby_trap_application_seams(monkeypatch)

        encoded = entrypoint.execute_task_from_payload(_payload_b64(submitted))
        decoded = decode_execution_completion(
            encoded,
            expected_identity=request.identity,
            expected_execution_protocol_version=request.execution_protocol_version,
        )

        assert isinstance(encoded, entrypoint._StrictRequestRejectionResult)
        assert decoded.completion.success is False
        assert decoded.completion.retryable is False
        assert decoded.completion.error == (f"execution request rejected: {classification.value}")
        assert decoded.completion.exception_type == "RayExecutionRequestIncompatible"
        assert decoded.completion.traceback is None

    @pytest.mark.parametrize(
        "failure",
        ["invalid_alphabet", "invalid_padding", "resource_limit"],
    )
    def test_strict_payload_decode_failure_is_fixed_and_secret_free(
        self,
        monkeypatch,
        failure: str,
    ) -> None:
        request, serialized = _strict_request()
        monkeypatch.setenv(
            RAY_JOB_CONFIG_JSON_ENV_VAR,
            _strict_config(request, serialized),
        )
        _booby_trap_application_seams(monkeypatch)
        secret = "RAY_JOB_PAYLOAD_SECRET"
        if failure == "resource_limit":
            monkeypatch.setattr(entrypoint, "_MAX_RAY_JOB_PAYLOAD_B64_BYTES", 8)
            submitted = _payload_b64(json.dumps({"secret": secret}))
            expected = ExecutionRequestRejection.RESOURCE_LIMIT
        elif failure == "invalid_padding":
            submitted = "e30"
            expected = ExecutionRequestRejection.INVALID_VERSIONED
        else:
            submitted = f"%%%{secret}%%%"
            expected = ExecutionRequestRejection.INVALID_VERSIONED

        encoded = entrypoint.execute_task_from_payload(submitted)
        decoded = decode_execution_completion(
            encoded,
            expected_identity=request.identity,
            expected_execution_protocol_version=request.execution_protocol_version,
        )

        assert decoded.completion.error == f"execution request rejected: {expected.value}"
        assert decoded.completion.retryable is False
        assert secret not in encoded

    def test_oversized_payload_rejects_before_base64_json_or_application(
        self,
        monkeypatch,
    ) -> None:
        import django_ray.execution_codec as execution_codec

        def forbidden(*_args, **_kwargs):
            raise AssertionError("oversized payload crossed its resource boundary")

        monkeypatch.delenv(RAY_JOB_CONFIG_JSON_ENV_VAR, raising=False)
        monkeypatch.setattr(entrypoint, "_MAX_RAY_JOB_PAYLOAD_B64_BYTES", 8)
        monkeypatch.setattr(entrypoint.base64, "b64decode", forbidden)
        monkeypatch.setattr(execution_codec, "decode_execution_request", forbidden)
        monkeypatch.setattr(entrypoint, "_execute_legacy_payload", forbidden)
        _booby_trap_application_seams(monkeypatch)

        encoded = entrypoint.execute_task_from_payload("A" * 9)

        assert isinstance(encoded, entrypoint._StrictRequestRejectionResult)
        assert "execution request rejected: resource_limit" in encoded

    def test_strict_payload_marker_without_metadata_never_falls_back(
        self,
        monkeypatch,
    ) -> None:
        _request, serialized = _strict_request()
        monkeypatch.delenv(RAY_JOB_CONFIG_JSON_ENV_VAR, raising=False)
        _booby_trap_application_seams(monkeypatch)

        encoded = entrypoint.execute_task_from_payload(_payload_b64(serialized))
        result = json.loads(encoded)

        assert isinstance(encoded, entrypoint._StrictRequestRejectionResult)
        assert result["success"] is False
        assert result["error"] == "execution request rejected: invalid_versioned"
        assert result["retryable"] is False
        assert "completion_schema" not in result

    def test_oversized_ray_job_config_rejects_before_application_setup(
        self,
        monkeypatch,
    ) -> None:
        import django_ray.ray_job_protocol as ray_job_protocol

        parse_json = json.loads

        def forbidden(*_args, **_kwargs):
            raise AssertionError("oversized config crossed its resource boundary")

        secret = "RAY_JOB_CONFIG_SECRET"
        monkeypatch.setattr(ray_job_protocol, "RAY_JOB_CONFIG_JSON_MAX_BYTES", 32)
        monkeypatch.setenv(
            RAY_JOB_CONFIG_JSON_ENV_VAR,
            json.dumps(
                {
                    "metadata": {
                        "django_ray_request_binding": ("django-ray.ray-job-request-binding/v1"),
                        "secret": secret,
                    }
                }
            ),
        )
        monkeypatch.setattr(ray_job_protocol.json, "loads", forbidden)
        monkeypatch.setattr(entrypoint, "_decode_payload_b64", forbidden)
        _booby_trap_application_seams(monkeypatch)

        encoded = entrypoint.execute_task_from_payload(_payload_b64("{}"))
        result = parse_json(encoded)

        assert isinstance(encoded, entrypoint._StrictRequestRejectionResult)
        assert result["error"] == "execution request rejected: resource_limit"
        assert result["retryable"] is False
        assert secret not in encoded

    def test_strict_metadata_marker_never_falls_back_to_legacy_payload(
        self,
        monkeypatch,
    ) -> None:
        request, serialized = _strict_request()
        monkeypatch.setenv(
            RAY_JOB_CONFIG_JSON_ENV_VAR,
            _strict_config(request, serialized),
        )
        _booby_trap_application_seams(monkeypatch)
        legacy_payload = json.dumps(
            {
                "callable_path": "tests.must_not_run",
                "serialized_args": "[]",
                "serialized_kwargs": "{}",
            },
            separators=(",", ":"),
        )

        encoded = entrypoint.execute_task_from_payload(_payload_b64(legacy_payload))
        decoded = decode_execution_completion(
            encoded,
            expected_identity=request.identity,
            expected_execution_protocol_version=request.execution_protocol_version,
        )

        assert decoded.completion.error == "execution request rejected: legacy_request"
        assert decoded.completion.retryable is False

    def test_execute_task_from_payload_invalid_payload_returns_error_result(self) -> None:
        """Invalid payload should produce a structured failure JSON result."""
        result_json = entrypoint.execute_task_from_payload("%%%not-base64%%%")
        result = json.loads(result_json)

        assert result["success"] is False
        assert result["error"] is not None
        assert isinstance(result["exception_type"], str)

    @pytest.mark.parametrize(
        "payload",
        [
            {"transport_version": 99, "callable_path": "tests.fake"},
            {"transport_version": 2, "callable_path": "tests.fake"},
        ],
    )
    def test_execute_task_from_payload_rejects_invalid_reference_transport(
        self,
        payload: dict[str, object],
    ) -> None:
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

        result = json.loads(entrypoint.execute_task_from_payload(payload_b64))

        assert result["success"] is False
        assert result["retryable"] is False
        assert "transport" in result["error"]

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

    def test_main_uses_dedicated_exit_for_fixed_strict_rejection(
        self,
        monkeypatch,
        capsys,
    ) -> None:
        request, serialized = _strict_request()
        metadata = json.loads(_strict_config(request, serialized))
        metadata["metadata"]["django_ray_request_sha256"] = "0" * 64
        monkeypatch.setenv(RAY_JOB_CONFIG_JSON_ENV_VAR, json.dumps(metadata))
        _booby_trap_application_seams(monkeypatch)

        exit_code = entrypoint.main(["--payload-b64", _payload_b64(serialized)])
        captured = capsys.readouterr()

        assert exit_code == RAY_JOB_REQUEST_REJECTED_EXIT_CODE
        assert captured.out == ""
        assert captured.err.strip() == (
            "django-ray task failed: execution request rejected: invalid_versioned"
        )

    def test_main_keeps_accepted_strict_application_failure_at_zero(
        self,
        monkeypatch,
        capsys,
    ) -> None:
        request, serialized = _strict_request()
        monkeypatch.setenv(
            RAY_JOB_CONFIG_JSON_ENV_VAR,
            _strict_config(request, serialized),
        )
        monkeypatch.setattr(
            entrypoint,
            "execute_task",
            lambda **_kwargs: json.dumps(
                {
                    "success": False,
                    "error": "application failure",
                    "retryable": True,
                }
            ),
        )

        exit_code = entrypoint.main(["--payload-b64", _payload_b64(serialized)])

        assert exit_code == 0
        assert capsys.readouterr().err.strip() == ("django-ray task failed: application failure")

    @pytest.mark.parametrize(
        "result_json",
        [
            "not json",
            "[]",
        ],
    )
    def test_main_reports_invalid_completion_envelopes(
        self, monkeypatch, capsys, result_json: str
    ) -> None:
        monkeypatch.setattr(entrypoint, "execute_task_from_payload", lambda _: result_json)

        assert entrypoint.main(["--payload-b64", "abc"]) == 0
        assert "invalid completion envelope" in capsys.readouterr().err

    def test_main_redacts_failed_completion_errors(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            entrypoint,
            "execute_task_from_payload",
            lambda _: '{"success": false, "error": "password=secret"}',
        )

        assert entrypoint.main(["--payload-b64", "abc"]) == 0
        assert capsys.readouterr().err.strip() == "django-ray task failed: [REDACTED]"

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
                "task_id": context.task_id,
                "attempt_number": context.attempt_number,
                "execution_generation": context.execution_generation,
                "runtime_env_profile": context.runtime_env_profile,
                "runtime_env_hash": context.runtime_env_hash,
                "ray_job_driver": context.ray_job_driver,
                "compiled_graph_submission_transport": (
                    context.compiled_graph_submission_transport
                ),
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
            task_id="00000000-0000-4000-8000-000000000042",
            attempt_number=3,
            execution_generation=7,
            runtime_env_profile="thin",
            runtime_env_hash="abc123",
        )

        result = json.loads(result_json)
        assert result["success"] is True, result
        assert result["result"] == {
            "task_pk": 42,
            "task_id": "00000000-0000-4000-8000-000000000042",
            "attempt_number": 3,
            "execution_generation": 7,
            "runtime_env_profile": "thin",
            "runtime_env_hash": "abc123",
            "ray_job_driver": True,
            "compiled_graph_submission_transport": "ray-job",
        }

    def test_execute_task_emits_enriched_completion_for_bound_strict_request(
        self,
        monkeypatch,
    ) -> None:
        identity = ExecutionIdentity(
            task_execution_pk=81,
            task_id="00000000-0000-4000-8000-000000000081",
            attempt_number=2,
            execution_generation=4,
        )
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_values: ([3], {}))
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: lambda value: {"value": value},
        )

        encoded = entrypoint.execute_task(
            "tests.strict_result",
            "[3]",
            "{}",
            _completion_identity=identity,
            _execution_protocol_version=1,
        )
        decoded = decode_execution_completion(
            encoded,
            expected_identity=identity,
            expected_execution_protocol_version=1,
        )

        assert decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
        assert decoded.completion.success is True
        assert decoded.completion.result == {"value": 3}
        assert decoded.completion.executor_django_ray_version

    def test_execute_task_without_strict_binding_retains_legacy_completion(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_values: ([], {}))
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: lambda: "legacy",
        )

        result = json.loads(entrypoint.execute_task("tests.legacy_result", "[]", "{}"))

        assert result == {
            "success": True,
            "result": "legacy",
            "result_reference": None,
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        }
        assert "completion_schema" not in result

    def test_execute_task_awaits_coroutine_and_closes_event_loop(self, monkeypatch) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        observed_loops: list[asyncio.AbstractEventLoop] = []

        async def add_numbers(left: int, right: int) -> int:
            observed_loops.append(asyncio.get_running_loop())
            await asyncio.sleep(0)
            return left + right

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: add_numbers,
        )

        result = json.loads(entrypoint.execute_task("tests.async_add", "[2, 3]", "{}"))

        assert result["success"] is True
        assert result["result"] == 5
        assert len(observed_loops) == 1
        assert observed_loops[0].is_closed()

    def test_malformed_input_reference_does_not_enter_durable_traceback(
        self,
        monkeypatch,
    ) -> None:
        sensitive = "VERY_PRIVATE_STORAGE_TOKEN"
        digest = "a" * 64
        reference = f"s3://task-inputs/django-ray/inputs/aa/aa/{digest}.json?bytes=1&{sensitive}"
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(
            "django_ray.input_storage.get_settings",
            lambda: {
                "INPUT_STORAGE_BACKEND": "s3",
                "INPUT_STORAGE_S3_BUCKET": "task-inputs",
                "INPUT_STORAGE_S3_PREFIX": "django-ray/inputs",
            },
        )

        result = json.loads(
            entrypoint.execute_task(
                "tests.never_imported",
                "null",
                "null",
                input_reference=reference,
            )
        )

        assert result["success"] is False
        assert result["error"] == "Task input reference is invalid"
        assert sensitive not in result["traceback"]
        assert reference not in result["traceback"]

    def test_execute_task_preserves_coroutine_exception_type(self, monkeypatch) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)

        async def fail() -> None:
            await asyncio.sleep(0)
            raise ValueError("async failure")

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: fail,
        )

        result = json.loads(entrypoint.execute_task("tests.async_fail", "[]", "{}"))

        assert result["success"] is False
        assert result["error"] == "async failure"
        assert result["exception_type"] == "builtins.ValueError"
        assert result["retryable"] is True

    def test_execute_task_preserves_failure_evidence_inside_json_framing(
        self,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)

        def fail() -> None:
            raise ValueError("\x1b[31mformatted failure\x1b[39m\rnext line")

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: fail,
        )

        encoded = entrypoint.execute_task("tests.formatted_fail", "[]", "{}")
        result = json.loads(encoded)

        assert result["error"] == "\x1b[31mformatted failure\x1b[39m\rnext line"
        assert "ValueError: \x1b[31mformatted failure\x1b[39m\rnext line" in result["traceback"]
        assert "\x1b" not in encoded
        assert "\\u001b" in encoded

    def test_execute_task_marks_pinned_plan_mismatch_non_retryable(self, monkeypatch) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)

        def mismatch() -> None:
            raise WorkflowPlanMismatchError("pinned plan changed")

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: mismatch,
        )

        result = json.loads(entrypoint.execute_task("tests.plan_mismatch", "[]", "{}"))

        assert result["success"] is False
        assert result["retryable"] is False
        assert result["exception_type"].endswith("WorkflowPlanMismatchError")

    @pytest.mark.parametrize("exception_type", [asyncio.CancelledError, KeyboardInterrupt])
    def test_execute_task_does_not_serialize_async_cancellation_base_exceptions(
        self,
        monkeypatch,
        exception_type: type[BaseException],
    ) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)

        async def cancel() -> None:
            raise exception_type()

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: cancel,
        )

        with pytest.raises(exception_type):
            entrypoint.execute_task("tests.async_cancel", "[]", "{}")

    def test_execute_task_cancels_child_tasks_before_closing_loop(self, monkeypatch) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        child_started = False
        child_cancelled = False

        async def child() -> None:
            nonlocal child_started, child_cancelled
            child_started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                child_cancelled = True
                raise

        async def spawn_child() -> str:
            asyncio.create_task(child())
            await asyncio.sleep(0)
            return "complete"

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: spawn_child,
        )

        result = json.loads(entrypoint.execute_task("tests.async_child", "[]", "{}"))

        assert result["success"] is True
        assert result["result"] == "complete"
        assert child_started is True
        assert child_cancelled is True

    def test_execute_task_exposes_and_resets_context_for_coroutine(self, monkeypatch) -> None:
        from django_ray.runtime.context import get_current_task_context

        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)

        async def read_context() -> dict[str, object]:
            await asyncio.sleep(0)
            context = get_current_task_context()
            assert context is not None
            return {
                "task_pk": context.task_pk,
                "ray_job_driver": context.ray_job_driver,
            }

        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: read_context,
        )

        result = json.loads(
            entrypoint.execute_task(
                "tests.async_context",
                "[]",
                "{}",
                task_execution_pk=73,
                ray_job_driver=False,
            )
        )

        assert result["success"] is True
        assert result["result"] == {"task_pk": 73, "ray_job_driver": False}
        assert get_current_task_context() is None

    def test_invoke_coroutine_rejects_an_existing_event_loop_without_calling_it(self) -> None:
        called = False

        async def coroutine_task() -> None:
            nonlocal called
            called = True

        async def invoke() -> None:
            with pytest.raises(RuntimeError, match="already has a running event loop"):
                entrypoint._invoke_task_callable(coroutine_task, [], {})

        asyncio.run(invoke())
        assert called is False

    def test_invoke_sync_task_does_not_create_an_event_loop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            entrypoint.asyncio,
            "run",
            lambda _awaitable: (_ for _ in ()).throw(AssertionError("unexpected event loop")),
        )

        assert entrypoint._invoke_task_callable(lambda value: value + 1, [4], {}) == 5

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

    @pytest.mark.django_db
    def test_sync_execution_keeps_result_for_worker_storage(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="entrypoint-sync-oversized-001",
            callable_path="testproject.tasks.echo_task",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )
        expected = {"message": "x" * 256}
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.import_callable",
            lambda _path: lambda: expected,
        )
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 1, "RESULT_STORAGE_BACKEND": "digest"},
        )

        result = json.loads(
            entrypoint.execute_task(
                task.callable_path,
                "[]",
                "{}",
                task_execution_pk=task.pk,
                attempt_number=1,
                execution_generation=1,
                ray_job_driver=False,
            )
        )

        task.refresh_from_db()
        assert result["result"] == expected
        assert result["result_reference"] is None
        assert task.completion_data is None

    @pytest.mark.django_db
    def test_completion_persistence_logs_database_errors(self, monkeypatch, caplog) -> None:
        secret = "completion-database-password"
        monkeypatch.setattr(
            RayTaskExecution.objects,
            "filter",
            lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError(f"database unavailable password={secret}")
            ),
        )

        entrypoint._persist_task_completion(1, 1, 1, '{"success": true}')

        assert "Failed to persist completion envelope for task 1" in caplog.text
        assert secret not in caplog.text
        assert "Traceback (most recent call last)" not in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_prepare_completion_falls_back_to_digest_storage(self, monkeypatch, caplog) -> None:
        from django_ray.result_storage import ResultStorageError

        secret = "result-storage-api-key"

        class FailingStorage:
            def store(self, *, serialized_result: str) -> str:
                raise ResultStorageError(f"object storage unavailable api_key={secret}")

        monkeypatch.setattr(
            entrypoint,
            "get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 1, "RESULT_STORAGE_BACKEND": "digest"},
        )
        monkeypatch.setattr(
            "django_ray.result_storage.get_result_storage_backend",
            lambda _settings: FailingStorage(),
        )

        result, reference = entrypoint._prepare_completion_result(
            {"large": "value"},
            task_execution_pk=1,
            attempt_number=1,
            execution_generation=1,
        )

        assert result is None
        assert reference is not None and reference.startswith("oversize://sha256/")
        assert "using digest-only reference" in caplog.text
        assert secret not in caplog.text
        assert "ResultStorageError" in caplog.text

    def test_module_main_guard_invokes_cli_entrypoint(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "argv", ["entrypoint", "--payload-b64", "abc"])
        monkeypatch.delitem(sys.modules, "django_ray.runtime.entrypoint")

        with pytest.raises(SystemExit, match="0"):
            runpy.run_module("django_ray.runtime.entrypoint", run_name="__main__")
