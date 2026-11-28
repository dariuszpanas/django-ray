"""Unit tests for entrypoint completion persistence, offload, async, and diagnostics."""

from __future__ import annotations

import asyncio
import json
import pickle
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.db import OperationalError

import django_ray.runtime.entrypoint as entrypoint
import django_ray.runtime.remote as remote
from django_ray.execution_codec import (
    EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES,
    ExecutionCompletionSource,
    ExecutionIdentity,
    ExecutionRequest,
    _preparse_json_scan,
    decode_execution_completion,
    encode_execution_request,
)
from django_ray.models import RayTaskExecution, TaskState
from django_ray.result_storage import ResultStorageError


def _make_identity(pk: int = 101) -> ExecutionIdentity:
    return ExecutionIdentity(
        task_execution_pk=pk,
        task_id=f"00000000-0000-4000-8000-{pk:012d}",
        attempt_number=1,
        execution_generation=2,
    )


class TestCompletionPersistenceRetryAndExit:
    """F-001 & F-096: DB connection hygiene, transient DB retry, and non-zero exit."""

    @pytest.mark.django_db
    def test_persist_task_completion_retries_transient_db_error_and_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="persist-retry-success-001",
            callable_path="tests.ok_task",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )
        close_calls = 0
        original_filter = RayTaskExecution.objects.filter
        attempts = 0

        def tracking_close() -> None:
            nonlocal close_calls
            close_calls += 1

        def flaky_filter(**kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OperationalError("connection reset by peer")
            return original_filter(**kwargs)

        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", tracking_close)
        monkeypatch.setattr(entrypoint, "_COMPLETION_PERSIST_BACKOFF_SECONDS", (0.0, 0.0, 0.0, 0.0))
        monkeypatch.setattr(RayTaskExecution.objects, "filter", flaky_filter)

        ok = entrypoint._persist_task_completion(task.pk, 1, 1, '{"success":true}')

        assert ok is True
        assert attempts == 3
        assert close_calls >= 5
        task.refresh_from_db()
        assert task.completion_data == '{"success":true}'

    @pytest.mark.django_db
    def test_main_exits_nonzero_when_completion_persistence_exhausts_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(entrypoint, "_COMPLETION_PERSIST_BACKOFF_SECONDS", (0.0, 0.0, 0.0, 0.0))
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_kw: ([], {}))
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: lambda: {"ok": True},
        )
        monkeypatch.setattr(
            RayTaskExecution.objects,
            "filter",
            lambda **_kw: (_ for _ in ()).throw(OperationalError("primary database down")),
        )

        result = entrypoint.execute_task(
            "tests.ok_task",
            "[]",
            "{}",
            task_execution_pk=55,
            task_id="00000000-0000-4000-8000-000000000055",
            attempt_number=1,
            execution_generation=1,
            ray_job_driver=True,
        )

        assert isinstance(result, entrypoint._CompletionPersistenceFailedResult)
        monkeypatch.setattr(entrypoint, "execute_task_from_reference", lambda _ref: result)
        exit_code = entrypoint.main(["--request-ref-b64", "dummy-locator"])
        assert exit_code == entrypoint.RAY_JOB_COMPLETION_PERSISTENCE_FAILED_EXIT_CODE


class TestResultOffloadAndStrictEncoding:
    """F-004 & F-012: Fail-loud result storage, Ray Core offload, and strict Sync."""

    def test_external_storage_retries_transient_error_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        class FlakyBackend:
            def store(self, *, serialized_result: str) -> str:
                nonlocal calls
                calls += 1
                if calls < 2:
                    raise ResultStorageError("temporary 503")
                return "s3://bucket/prefix/abc.json"

        monkeypatch.setattr(
            entrypoint,
            "get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 16, "RESULT_STORAGE_BACKEND": "s3"},
        )
        monkeypatch.setattr(entrypoint, "_RESULT_STORAGE_STORE_BACKOFF_SECONDS", (0.0, 0.0))
        monkeypatch.setattr(
            "django_ray.result_storage.get_result_storage_backend",
            lambda _settings: FlakyBackend(),
        )

        value, ref = entrypoint._prepare_completion_result(
            {"payload": "x" * 128},
            task_execution_pk=None,
            attempt_number=None,
            execution_generation=None,
            allow_result_offload=True,
        )

        assert calls == 2
        assert value is None
        assert ref == "s3://bucket/prefix/abc.json"

    def test_external_storage_exhaustion_fails_non_retryable_without_digest_downgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity = _make_identity(202)
        calls = 0

        class BrokenBackend:
            def store(self, *, serialized_result: str) -> str:
                nonlocal calls
                calls += 1
                raise ResultStorageError("read-only filesystem")

        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_kw: ([], {}))
        monkeypatch.setattr(
            entrypoint,
            "get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 16, "RESULT_STORAGE_BACKEND": "filesystem"},
        )
        monkeypatch.setattr(entrypoint, "_RESULT_STORAGE_STORE_BACKOFF_SECONDS", (0.0, 0.0))
        monkeypatch.setattr(
            "django_ray.result_storage.get_result_storage_backend",
            lambda _settings: BrokenBackend(),
        )
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: lambda: {"blob": "y" * 256},
        )
        monkeypatch.setattr(entrypoint, "_persist_task_completion", lambda *a: True)

        wire = entrypoint.execute_task(
            "tests.big_result",
            "[]",
            "{}",
            _completion_identity=identity,
            _execution_protocol_version=1,
            allow_result_offload=True,
        )
        decoded = decode_execution_completion(
            wire, expected_identity=identity, expected_execution_protocol_version=1
        )

        assert calls == entrypoint._RESULT_STORAGE_STORE_MAX_ATTEMPTS
        assert decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
        assert decoded.completion.success is False
        assert decoded.completion.retryable is False
        assert decoded.completion.exception_type == "ResultStoragePersistenceError"
        assert decoded.completion.result_reference is None

    def test_ray_core_remote_offloads_oversized_result_without_db_completion_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity = _make_identity(303)
        request = ExecutionRequest(
            identity=identity,
            execution_protocol_version=1,
            callable_path="tests.huge_task",
            transport_version=1,
            serialized_args="[]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=None,
            runtime_env_hash="a" * 64,
            runtime_env_plan_identity={"digest": "sha256:" + ("b" * 64)},
            compiled_graph_submission_transport="direct-ray-core",
        )
        encoded_request = encode_execution_request(request)
        stored_refs: list[str] = []
        db_writes: list[Any] = []

        class CapturingBackend:
            def store(self, *, serialized_result: str) -> str:
                ref = f"resultfs://sha256/{'c' * 64}?rel=cc/cc/{'c' * 64}.json&bytes={len(serialized_result)}"
                stored_refs.append(ref)
                return ref

        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_kw: ([], {}))
        monkeypatch.setattr(
            entrypoint,
            "get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 32, "RESULT_STORAGE_BACKEND": "filesystem"},
        )
        monkeypatch.setattr(
            "django_ray.result_storage.get_result_storage_backend",
            lambda _settings: CapturingBackend(),
        )
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: lambda: {"data": "z" * 512},
        )
        monkeypatch.setattr(
            entrypoint,
            "_persist_task_completion",
            lambda pk, *rest: db_writes.append(pk) or True,
        )

        wire = remote.execute_django_task_remote(
            encoded_request,
            expected_task_execution_pk=identity.task_execution_pk,
            expected_task_id=identity.task_id,
            expected_attempt_number=identity.attempt_number,
            expected_execution_generation=identity.execution_generation,
            expected_execution_protocol_version=1,
        )
        decoded = decode_execution_completion(
            wire, expected_identity=identity, expected_execution_protocol_version=1
        )

        # Ray Core remote return value must be a plain built-in str so generic Ray
        # clusters without django_ray installed can unpickle/deserialize it safely.
        assert type(wire) is str
        assert stored_refs
        assert decoded.completion.success is True
        assert decoded.completion.result is None
        assert decoded.completion.result_reference == stored_refs[0]
        # Ray Core MUST NOT write completion_data to PostgreSQL!
        assert db_writes == [None]

        # _EncodedCompletionResult itself must pickle/unpickle cleanly as built-in str.
        tagged = entrypoint._EncodedCompletionResult(
            wire,
            completion_success=True,
            completion_error=None,
        )
        assert tagged.completion_success is True
        unpickled = pickle.loads(pickle.dumps(tagged))
        assert type(unpickled) is str
        assert unpickled == wire

        # F-071: verify remote.py logs SUCCESS without invoking json.loads on the returned envelope
        monkeypatch.setattr(entrypoint, "execute_task", lambda *_a, **_kw: tagged)
        monkeypatch.setattr(
            remote,
            "json",
            type(
                "_ForbiddenJson",
                (),
                {
                    "loads": staticmethod(
                        lambda *_a, **_kw: (_ for _ in ()).throw(
                            AssertionError("redundant json.loads")
                        )
                    )
                },
            )(),
        )
        returned = remote.execute_django_task_remote(
            encoded_request,
            expected_task_execution_pk=identity.task_execution_pk,
            expected_task_id=identity.task_id,
            expected_attempt_number=identity.attempt_number,
            expected_execution_generation=identity.execution_generation,
            expected_execution_protocol_version=1,
        )
        assert type(returned) is str
        assert returned == wire

    def test_sync_mode_enforces_strict_allow_nan_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_kw: ([], {}))
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: lambda: {"bad": float("nan")},
        )

        wire = entrypoint.execute_task(
            "tests.nan_task",
            "[]",
            "{}",
            task_execution_pk=404,
            attempt_number=1,
            execution_generation=1,
            ray_job_driver=False,
        )
        parsed = json.loads(wire)
        assert parsed["success"] is False
        assert parsed["retryable"] is False
        assert parsed["exception_type"] == "RayExecutionCompletionEncodingError"
        assert "non-finite float" in parsed["error"]


class TestDiagnosticsTruncationAndAsyncHandling:
    """F-026, F-071, & F-099: Bounded diagnostics, type hints, CancelledError, awaitables."""

    def test_oversized_traceback_and_nul_preserve_diagnostics_and_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity = _make_identity(505)

        def failing_with_huge_error() -> None:
            raise ValueError("head-msg\x00" + ("A" * 80_000) + "tail-sentinel")

        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_kw: ([], {}))
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: failing_with_huge_error,
        )
        monkeypatch.setattr(entrypoint, "_persist_task_completion", lambda *a: True)

        wire = entrypoint.execute_task(
            "tests.huge_fail",
            "[]",
            "{}",
            _completion_identity=identity,
            _execution_protocol_version=1,
        )
        decoded = decode_execution_completion(
            wire, expected_identity=identity, expected_execution_protocol_version=1
        )

        assert decoded.completion.success is False
        assert decoded.completion.retryable is True
        assert decoded.completion.exception_type == "builtins.ValueError"
        assert decoded.completion.error is not None
        assert len(decoded.completion.error.encode("utf-8")) <= (
            EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES
        )
        assert "\\x00" in decoded.completion.error
        assert "tail-sentinel" in decoded.completion.error
        assert decoded.completion.traceback is not None
        assert len(decoded.completion.traceback.encode("utf-8")) <= (
            EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES
        )

    @pytest.mark.parametrize(
        ("bad_value", "expected_hint"),
        [
            (Decimal("12.34"), "decimal.Decimal"),
            (UUID("00000000-0000-4000-8000-000000000001"), "uuid.UUID"),
            (datetime(2026, 1, 1, tzinfo=UTC), "datetime.datetime"),
        ],
    )
    def test_non_json_return_includes_offending_type_without_leaking_value(
        self, monkeypatch: pytest.MonkeyPatch, bad_value: Any, expected_hint: str
    ) -> None:
        identity = _make_identity(606)
        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_kw: ([], {}))
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: lambda: {"secret_field": bad_value},
        )
        monkeypatch.setattr(entrypoint, "_persist_task_completion", lambda *a: True)

        wire = entrypoint.execute_task(
            "tests.non_json",
            "[]",
            "{}",
            _completion_identity=identity,
            _execution_protocol_version=1,
        )
        decoded = decode_execution_completion(
            wire, expected_identity=identity, expected_execution_protocol_version=1
        )

        assert decoded.completion.success is False
        assert decoded.completion.retryable is False
        assert decoded.completion.exception_type == "RayExecutionCompletionEncodingError"
        assert expected_hint in (decoded.completion.error or "")
        assert str(bad_value) not in (decoded.completion.error or "")

    def test_asyncio_cancelled_error_is_captured_as_structured_completion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity = _make_identity(707)

        async def cancelled_coro() -> None:
            raise asyncio.CancelledError("cancelled inside async task")

        monkeypatch.setattr(entrypoint, "bootstrap_django", lambda: None)
        monkeypatch.setattr(entrypoint, "_close_stale_db_connections", lambda: None)
        monkeypatch.setattr(entrypoint, "load_task_input", lambda **_kw: ([], {}))
        monkeypatch.setattr(
            "django_ray.runtime.import_utils.resolve_task_callable",
            lambda _path: cancelled_coro,
        )
        monkeypatch.setattr(entrypoint, "_persist_task_completion", lambda *a: True)

        wire = entrypoint.execute_task(
            "tests.cancelled_async",
            "[]",
            "{}",
            _completion_identity=identity,
            _execution_protocol_version=1,
        )
        decoded = decode_execution_completion(
            wire, expected_identity=identity, expected_execution_protocol_version=1
        )

        assert decoded.completion.success is False
        assert decoded.completion.retryable is False
        assert decoded.completion.exception_type is not None
        assert decoded.completion.exception_type.endswith("CancelledError")

    def test_sync_wrapper_returning_awaitable_is_awaited(self) -> None:
        def sync_wrapper(value: int) -> Any:
            async def _inner() -> int:
                return value * 3

            return _inner()

        assert entrypoint._invoke_task_callable(sync_wrapper, [7], {}) == 21

    def test_preparse_json_scan_handles_large_strings_and_unterminated_linear_time(self) -> None:
        large_str = '{"completion_schema":"x","result":"' + ("a" * 200_000) + '\\"end"}'
        assert _preparse_json_scan(large_str) is True
        unterminated = '{"result":"' + ("b" * 200_000)
        assert _preparse_json_scan(unterminated) is False
