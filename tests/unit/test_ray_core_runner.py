"""Additional unit tests for RayCoreRunner runtime branches."""

from __future__ import annotations

import base64
import json
import sys
import threading
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_core import (
    RayCoreHandle,
    RayCoreRunner,
    _compiled_graph_submission_transport,
)
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    normalize_runtime_env,
    runtime_env_for_storage,
)


class _FakeObjectRef:
    def __init__(self, hex_value: str) -> None:
        self._hex_value = hex_value

    def hex(self) -> str:
        return self._hex_value


class _FakeExceptions:
    RayTaskError = RuntimeError


class _FakeJobID:
    def hex(self) -> str:
        return "02000000"

    def __str__(self) -> str:
        return "JobID(02000000)"


class _FakeRay:
    def __init__(self, *, initialized: bool = True) -> None:
        self.initialized = initialized
        self.init_calls: list[dict[str, Any]] = []
        self.remote_calls: list[dict[str, Any]] = []
        self.remote_invocations: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.runtime_job_id = "02000000"
        self.runtime_context_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.remote_error: Exception | None = None
        self.default_hex = "abcdef0123456789" * 4
        self.client_connected = False
        self.util = SimpleNamespace(
            client=SimpleNamespace(ray=SimpleNamespace(is_connected=lambda: self.client_connected))
        )

        self.values: dict[_FakeObjectRef, Any] = {}
        self.ready_refs: set[_FakeObjectRef] = set()
        self.cancelled: list[tuple[_FakeObjectRef, bool]] = []
        self.exceptions = _FakeExceptions()

    def init(self, **kwargs: Any) -> None:
        self.init_calls.append(kwargs)
        self.initialized = True

    def is_initialized(self) -> bool:
        return self.initialized

    def remote(self, *args, **kwargs: Any):
        self.remote_calls.append(kwargs)

        def _decorator(fn):
            fake = self

            class _RemoteCallable:
                @staticmethod
                def remote(*args: Any, **kw: Any) -> _FakeObjectRef:
                    fake.remote_invocations.append((args, kw))
                    if fake.remote_error is not None:
                        raise fake.remote_error
                    value = fn(*args, **kw)
                    ref = _FakeObjectRef(fake.default_hex)
                    fake.values[ref] = value
                    return ref

                def options(self, **kw: Any):
                    fake.remote_calls.append(kw)
                    return self

            return _RemoteCallable()

        if args and callable(args[0]):
            return _decorator(args[0])
        return _decorator

    def get_runtime_context(self) -> Any:
        if self.runtime_context_error is not None:
            raise self.runtime_context_error
        return SimpleNamespace(get_job_id=lambda: self.runtime_job_id)

    def wait(self, refs: list[_FakeObjectRef], timeout: int = 0, num_returns: int | None = None):
        ready = [ref for ref in refs if ref in self.ready_refs]
        if num_returns is not None:
            ready = ready[:num_returns]
        not_ready = [ref for ref in refs if ref not in ready]
        return ready, not_ready

    def get(self, ref: _FakeObjectRef) -> Any:
        value = self.values[ref]
        if isinstance(value, Exception):
            raise value
        return value

    def cancel(self, ref: _FakeObjectRef, force: bool = False) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append((ref, force))


def _install_fake_ray(monkeypatch) -> _FakeRay:
    fake = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake)
    monkeypatch.setitem(sys.modules, "ray.exceptions", fake.exceptions)
    return fake


def _make_handle(job_id: str) -> SubmissionHandle:
    return SubmissionHandle(
        ray_job_id=job_id,
        ray_address="auto",
        submitted_at=datetime.now(UTC),
    )


def _task_execution(
    pk: int,
    *,
    attempt_number: int = 1,
    execution_generation: int = 0,
    **attributes: Any,
) -> SimpleNamespace:
    task_attributes = {
        "task_id": f"task-{pk}",
        "execution_protocol_version": 1,
        "args_json": "[]",
        "kwargs_json": "{}",
        "input_reference": None,
        "runtime_env_profile": None,
        "runtime_env_json": "{}",
        "runtime_env_hash": "",
        **attributes,
    }
    return SimpleNamespace(
        pk=pk,
        attempt_number=attempt_number,
        execution_generation=execution_generation,
        **task_attributes,
    )


class TestRayCoreRunnerRuntime:
    """Coverage for RayCoreRunner execution branches."""

    def test_ensure_ray_initialized_uses_env_address(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.initialized = False
        monkeypatch.setenv("RAY_ADDRESS", "ray://unit:10001")

        RayCoreRunner()

        assert fake.init_calls == [{"address": "ray://unit:10001", "ignore_reinit_error": True}]

    def test_submit_builds_composite_handle_and_tracks_pending(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json, **_kwargs: json.dumps(
                {
                    "success": True,
                    "result": {
                        "callable_path": callable_path,
                        "args": json.loads(args_json),
                        "kwargs": json.loads(kwargs_json),
                    },
                }
            ),
        )
        monkeypatch.setenv("RAY_ADDRESS", "ray://unit-submit:10001")

        runner = RayCoreRunner()
        handle = runner.submit(
            task_execution=_task_execution(11),
            callable_path="testproject.tasks.add_numbers",
            args=(3, 4),
            kwargs={"x": 1},
        )

        assert handle.ray_job_id.startswith("02000000:")
        assert handle.ray_address == "ray://unit-submit:10001"
        assert runner.pending_count == 1
        pending = runner._pending_tasks[11]
        assert pending.attempt_number == 1
        assert pending.execution_generation == 0
        assert pending.ray_job_id == "02000000"
        assert pending.ray_task_id == fake.default_hex[:48]
        from django_ray.execution_codec import decode_execution_request

        submitted_request = decode_execution_request(fake.remote_invocations[-1][0][0])
        assert json.loads(submitted_request.serialized_args) == [3, 4]
        assert json.loads(submitted_request.serialized_kwargs) == {"x": 1}

    def test_submit_uses_durable_json_as_the_opaque_request_source(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda *_args, **_kwargs: json.dumps({"success": True, "result": None}),
        )
        execution = _task_execution(
            111,
            task_id="durable-request-task",
            attempt_number=4,
            execution_generation=7,
            callable_path="testproject.tasks.echo_task",
            args_json='["durable-argument"]',
            kwargs_json='{"source":"durable"}',
        )

        RayCoreRunner().submit_durable(task_execution=execution)

        from django_ray.execution_codec import (
            ExecutionIdentity,
            decode_execution_request,
        )

        submitted_args, submitted_kwargs = fake.remote_invocations[-1]
        assert len(submitted_args) == 1
        assert (
            decode_execution_request(
                submitted_args[0],
                expected_identity=ExecutionIdentity(
                    task_execution_pk=111,
                    task_id="durable-request-task",
                    attempt_number=4,
                    execution_generation=7,
                ),
                expected_execution_protocol_version=1,
            ).serialized_args
            == '["durable-argument"]'
        )
        assert json.loads(submitted_args[0])["serialized_kwargs"] == '{"source":"durable"}'
        assert submitted_kwargs == {
            "expected_task_execution_pk": 111,
            "expected_task_id": "durable-request-task",
            "expected_attempt_number": 4,
            "expected_execution_generation": 7,
            "expected_execution_protocol_version": 1,
        }

    def test_submit_decrypts_stored_runtime_env_before_remote_submission(
        self,
        monkeypatch,
        settings,
    ) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda *_args, **_kwargs: json.dumps({"success": True, "result": None}),
        )
        key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        encryption_config = {
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"runner-key": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "runner-key",
        }
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", **encryption_config}
        runtime_env = normalize_runtime_env(
            {"env_vars": {"EXECUTION_MODE": "encrypted-ray-core"}},
            profile="encrypted-core",
        )
        task_id = "encrypted-ray-core-task"
        stored = runtime_env_for_storage(
            runtime_env,
            task_id=task_id,
            config=encryption_config,
        )

        RayCoreRunner().submit(
            task_execution=_task_execution(
                12,
                task_id=task_id,
                runtime_env_profile=stored.profile,
                runtime_env_json=stored.serialized,
                runtime_env_hash=stored.digest,
            ),
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert stored.serialized != runtime_env.serialized
        assert fake.remote_calls[-1]["runtime_env"] == runtime_env.spec
        submitted_args, submitted_kwargs = fake.remote_invocations[-1]
        from django_ray.execution_codec import decode_execution_request

        request = decode_execution_request(submitted_args[0])
        assert request.runtime_env_profile == runtime_env.profile
        assert request.runtime_env_hash == runtime_env.digest
        assert request.runtime_env_plan_identity["profile"] == runtime_env.profile
        assert submitted_kwargs == {
            "expected_task_execution_pk": 12,
            "expected_task_id": task_id,
            "expected_attempt_number": 1,
            "expected_execution_generation": 0,
            "expected_execution_protocol_version": 1,
        }

    def test_submit_rejects_duplicate_pk_before_remote_submission(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        execution = _task_execution(11)
        runner.submit(
            task_execution=execution,
            callable_path="testproject.tasks.add_numbers",
            args=(3, 4),
            kwargs={},
        )
        pending = runner.pending_task_handles
        remote_call_count = len(fake.remote_calls)

        with pytest.raises(RuntimeError, match="already has a pending Ray Core submission"):
            runner.submit(
                task_execution=execution,
                callable_path="testproject.tasks.add_numbers",
                args=(5, 6),
                kwargs={},
            )

        assert runner.pending_task_handles == pending
        assert len(fake.remote_calls) == remote_call_count

    def test_submit_rejects_immutable_runtime_env_snapshot_mismatch(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        digests = iter(("planned", "snapshotted"))
        monkeypatch.setattr(
            "django_ray.workflow.plans.runtime_env_plan_identity",
            lambda *_args, **_kwargs: SimpleNamespace(manifest={"digest": next(digests)}),
        )
        from django_ray.workflow.plans import WorkflowPlanMismatchError

        with pytest.raises(
            WorkflowPlanMismatchError,
            match="immutable snapshot differs",
        ):
            RayCoreRunner().submit(
                task_execution=_task_execution(12),
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert fake.remote_calls == []

    def test_submit_rejects_runtime_env_changed_during_packaging(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        digests = iter(("planned", "planned", "changed"))
        monkeypatch.setattr(
            "django_ray.workflow.plans.runtime_env_plan_identity",
            lambda *_args, **_kwargs: SimpleNamespace(manifest={"digest": next(digests)}),
        )
        from django_ray.workflow.plans import WorkflowPlanMismatchError

        with pytest.raises(
            WorkflowPlanMismatchError,
            match="local content changed",
        ):
            RayCoreRunner().submit(
                task_execution=_task_execution(13),
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert fake.remote_calls == []

    def test_submit_transports_external_input_by_reference(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        captured: dict[str, object] = {}

        def fake_execute(
            callable_path: str,
            args_json: str,
            kwargs_json: str,
            *,
            input_reference: str | None = None,
            **_kwargs: object,
        ) -> str:
            captured.update(
                callable_path=callable_path,
                args_json=args_json,
                kwargs_json=kwargs_json,
                input_reference=input_reference,
            )
            return json.dumps({"success": True, "result": None})

        monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", fake_execute)
        reference = "resultfs://sha256/" + "a" * 64 + "?rel=aa/aa/" + "a" * 64 + ".json&bytes=4"
        task_execution = _task_execution(
            14,
            input_reference=reference,
            args_json="null",
            kwargs_json="null",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
        )

        RayCoreRunner().submit(
            task_execution=task_execution,
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert captured == {
            "callable_path": "testproject.tasks.echo_task",
            "args_json": "null",
            "kwargs_json": "null",
            "input_reference": reference,
        }

    def test_submit_propagates_progress_fence_identity(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        captured: dict[str, object] = {}

        def fake_execute(*_args, **_kwargs) -> str:
            from django_ray.runtime.context import get_current_task_context

            context = get_current_task_context()
            assert context is not None
            captured.update(
                task_pk=context.task_pk,
                attempt_number=context.attempt_number,
                execution_generation=context.execution_generation,
                compiled_graph_submission_transport=(context.compiled_graph_submission_transport),
            )
            return json.dumps({"success": True, "result": None})

        monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", fake_execute)

        RayCoreRunner().submit(
            task_execution=_task_execution(
                15,
                attempt_number=3,
                execution_generation=9,
            ),
            callable_path="testproject.tasks.echo_task",
            args=(),
            kwargs={},
        )

        assert captured == {
            "task_pk": 15,
            "attempt_number": 3,
            "execution_generation": 9,
            "compiled_graph_submission_transport": "direct-ray-core",
        }

    def test_submission_transport_uses_the_live_client_connection(self) -> None:
        fake = _FakeRay()

        assert _compiled_graph_submission_transport(fake) == "direct-ray-core"

        fake.client_connected = True

        assert _compiled_graph_submission_transport(fake) == "ray-client"

    def test_submission_transport_fails_closed_when_client_state_is_indeterminate(
        self,
    ) -> None:
        fake = _FakeRay()
        initialized_checks: list[bool] = []
        fake.util.client.ray.is_connected = lambda: None
        fake.is_initialized = lambda: initialized_checks.append(True) or True

        assert _compiled_graph_submission_transport(fake) is None
        assert initialized_checks == []

        def fail_client_check() -> bool:
            raise RuntimeError("client state unavailable")

        fake.util.client.ray.is_connected = fail_client_check

        assert _compiled_graph_submission_transport(fake) is None
        assert initialized_checks == []

    def test_submission_transport_fails_closed_when_core_state_is_unavailable(self) -> None:
        fake = _FakeRay(initialized=False)

        assert _compiled_graph_submission_transport(fake) is None

        def fail_initialized_check() -> bool:
            raise RuntimeError("Ray Core state unavailable")

        fake.is_initialized = fail_initialized_check

        assert _compiled_graph_submission_transport(fake) is None

    def test_submit_registers_remote_module_for_ray_cloudpickle(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        registered: list[object] = []
        fake.cloudpickle = SimpleNamespace(register_pickle_by_value=registered.append)
        import django_ray.runtime.remote as remote_module

        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json, **_kwargs: json.dumps(
                {"success": True, "result": callable_path}
            ),
        )

        runner = RayCoreRunner()
        runner.submit(
            task_execution=_task_execution(23),
            callable_path="testproject.tasks.echo_task",
            args=("hello",),
            kwargs={},
        )

        assert registered == [remote_module]

    def test_submit_discards_failed_ray_client_definition_before_retry(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.remote_error = ModuleNotFoundError("django_ray")
        import django_ray.runner.ray_core as ray_core_module

        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json, **_kwargs: json.dumps(
                {"success": True, "result": callable_path}
            ),
        )

        runner = RayCoreRunner()
        with pytest.raises(ModuleNotFoundError, match="django_ray"):
            runner.submit(
                task_execution=_task_execution(24),
                callable_path="testproject.tasks.echo_task",
                args=("hello",),
                kwargs={},
            )

        assert ray_core_module._execute_django_task_remote_cached is None

        fake.remote_error = None
        handle = runner.submit(
            task_execution=_task_execution(24),
            callable_path="testproject.tasks.echo_task",
            args=("hello",),
            kwargs={},
        )

        assert handle.ray_job_id.startswith("02000000:")
        assert runner.pending_count == 1
        assert sum(call == {} for call in fake.remote_calls) == 2

    def test_submit_applies_persisted_runtime_env(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json, **_kwargs: json.dumps(
                {"success": True, "result": callable_path}
            ),
        )

        runner = RayCoreRunner()
        runtime_env = normalize_runtime_env(
            {"env_vars": {"MODE": "thin"}},
            profile="thin",
        )
        runner.submit(
            task_execution=_task_execution(
                12,
                runtime_env_profile=runtime_env.profile,
                runtime_env_json=runtime_env.serialized,
                runtime_env_hash=runtime_env.digest,
            ),
            callable_path="testproject.tasks.echo_task",
            args=("hello",),
            kwargs={},
        )

        assert fake.remote_calls[-1]["runtime_env"] == {"env_vars": {"MODE": "thin"}}

    def test_submit_rejects_corrupt_runtime_env_before_remote_call(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        runtime_env = normalize_runtime_env(
            {"env_vars": {"VALUE": "arbitrary-customer-marker-7cf3"}},
            profile="thin",
        )

        with pytest.raises(RuntimeEnvSnapshotError, match="hash does not match") as exc_info:
            runner.submit(
                task_execution=_task_execution(
                    13,
                    runtime_env_profile=runtime_env.profile,
                    runtime_env_json=runtime_env.serialized,
                    runtime_env_hash="0" * 64,
                ),
                callable_path="testproject.tasks.echo_task",
                args=(),
                kwargs={},
            )

        assert runner.pending_count == 0
        assert fake.remote_calls == []
        assert "arbitrary-customer-marker-7cf3" not in str(exc_info.value)

    def test_submit_normalizes_ray_job_id_to_hex(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.runtime_job_id = _FakeJobID()
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json, **_kwargs: json.dumps(
                {"success": True, "result": callable_path}
            ),
        )

        runner = RayCoreRunner()
        handle = runner.submit(
            task_execution=_task_execution(13),
            callable_path="testproject.tasks.echo_task",
            args=("hello",),
            kwargs={},
        )

        assert handle.ray_job_id.startswith("02000000:")
        assert runner._pending_tasks[13].ray_job_id == "02000000"

    def test_submit_falls_back_to_legacy_id_when_runtime_context_fails(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.runtime_context_error = RuntimeError("no runtime context")
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json, **_kwargs: json.dumps(
                {"success": True, "result": {"callable_path": callable_path}}
            ),
        )

        runner = RayCoreRunner()
        handle = runner.submit(
            task_execution=_task_execution(22),
            callable_path="testproject.tasks.echo_task",
            args=("a",),
            kwargs={},
        )

        assert handle.ray_job_id == "ray_core:22"
        assert runner.get_status(handle).status == JobStatus.RUNNING
        assert runner.cancel(handle) is True
        status_after_cancel = runner.get_status(handle)
        assert status_after_cancel.status == JobStatus.UNKNOWN
        assert status_after_cancel.message == "Submission handle is no longer tracked"

    def test_returned_legacy_handle_does_not_poll_or_cancel_replacement(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.runtime_context_error = RuntimeError("no runtime context")
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json, **_kwargs: json.dumps(
                {"success": True, "result": callable_path}
            ),
        )

        runner = RayCoreRunner()
        stale_submission = runner.submit(
            task_execution=_task_execution(23),
            callable_path="testproject.tasks.echo_task",
            args=("a",),
            kwargs={},
        )
        replacement = RayCoreHandle(
            task_pk=23,
            object_ref=_FakeObjectRef("replacement"),
            submitted_at=datetime.now(UTC),
            task_name="replacement",
            attempt_number=2,
            execution_generation=1,
        )
        runner._pending_tasks[23] = replacement

        info = runner.get_status(stale_submission)

        assert info.status == JobStatus.UNKNOWN
        assert runner.cancel(stale_submission) is False
        assert fake.cancelled == []
        assert runner._pending_tasks[23] is replacement

    def test_build_composite_id_returns_none_without_ids(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        core_handle = RayCoreHandle(
            task_pk=1,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="task",
            attempt_number=1,
            execution_generation=0,
        )
        assert runner._build_composite_id(core_handle) is None

    def test_ray_core_handle_preserves_legacy_positional_id_slots(self) -> None:
        submitted_at = datetime.now(UTC)
        object_ref = object()

        handle = RayCoreHandle(
            1,
            object_ref,
            submitted_at,
            "task",
            "02000000",
            "abcdef",
            attempt_number=2,
            execution_generation=3,
        )

        assert handle.ray_job_id == "02000000"
        assert handle.ray_task_id == "abcdef"
        assert handle.attempt_number == 2
        assert handle.execution_generation == 3

    def test_resolve_task_pk_returns_none_for_invalid_legacy_id(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        assert runner._resolve_task_pk("ray_core:not-an-int") is None

    @pytest.mark.parametrize("handle_id", ["ray_core:1", "raysubmit_job"])
    def test_legacy_handle_ids_are_not_canonical(self, handle_id: str) -> None:
        assert RayCoreRunner._is_canonical_handle_id(handle_id) is False

    def test_reconstructed_legacy_status_is_unknown_when_pending_missing(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()

        info = runner.get_status(_make_handle("ray_core:999"))

        assert info.status == JobStatus.UNKNOWN
        assert info.message == "Legacy handle lacks exact submission identity"

    def test_canonical_status_is_unknown_when_pending_missing(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()

        info = runner.get_status(_make_handle("02000000:abcdef"))

        assert info.status == JobStatus.UNKNOWN
        assert info.message == "Submission handle is not tracked by this runner"

    def test_get_status_returns_failed_for_unsuccessful_payload(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        ref = _FakeObjectRef("deadbeef")
        fake.values[ref] = '{"success": false, "error": "boom"}'
        fake.ready_refs.add(ref)
        runner._pending_tasks[3] = RayCoreHandle(
            task_pk=3,
            object_ref=ref,
            submitted_at=datetime.now(UTC),
            task_name="task",
            attempt_number=1,
            execution_generation=0,
            ray_job_id="02000000",
            ray_task_id="deadbeef",
        )

        info = runner.get_status(_make_handle("02000000:deadbeef"))

        assert info.status == JobStatus.FAILED
        assert info.message == "boom"
        assert 3 not in runner._pending_tasks

    def test_get_status_returns_failed_when_ray_get_raises(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        ref = _FakeObjectRef("feedface")
        fake.values[ref] = RuntimeError("ray get failed")
        fake.ready_refs.add(ref)
        runner._pending_tasks[4] = RayCoreHandle(
            task_pk=4,
            object_ref=ref,
            submitted_at=datetime.now(UTC),
            task_name="task",
            attempt_number=1,
            execution_generation=0,
            ray_job_id="02000000",
            ray_task_id="feedface",
        )

        info = runner.get_status(_make_handle("02000000:feedface"))

        assert info.status == JobStatus.FAILED
        assert "ray get failed" in (info.message or "")
        assert 4 not in runner._pending_tasks

    def test_error_envelopes_survive_broken_exception_messages(self, monkeypatch) -> None:
        calls = 0

        class BrokenRayError(RuntimeError):
            def __str__(self) -> str:
                nonlocal calls
                calls += 1
                raise RuntimeError("secondary password=do-not-expose")

        fake = _install_fake_ray(monkeypatch)
        status_runner = RayCoreRunner()
        status_ref = _FakeObjectRef("broken-status")
        fake.values[status_ref] = BrokenRayError()
        fake.ready_refs.add(status_ref)
        status_runner._pending_tasks[5] = RayCoreHandle(
            task_pk=5,
            object_ref=status_ref,
            submitted_at=datetime.now(UTC),
            task_name="status",
            attempt_number=1,
            execution_generation=0,
            ray_job_id="02000000",
            ray_task_id="broken-status",
        )

        info = status_runner.get_status(_make_handle("02000000:broken-status"))

        poll_runner = RayCoreRunner()
        poll_ref = _FakeObjectRef("broken-poll")
        fake.values[poll_ref] = BrokenRayError()
        fake.ready_refs.add(poll_ref)
        poll_runner._pending_tasks[6] = RayCoreHandle(
            task_pk=6,
            object_ref=poll_ref,
            submitted_at=datetime.now(UTC),
            task_name="poll",
            attempt_number=2,
            execution_generation=3,
        )

        completion = poll_runner.poll_completed()[0]

        assert info.status == JobStatus.FAILED
        assert info.message == "exception message unavailable"
        assert json.loads(completion.result_json)["error"] == "exception message unavailable"
        assert "secondary password" not in completion.result_json
        assert calls == 2

    def test_cancel_returns_false_when_task_not_pending(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()

        assert runner.cancel(_make_handle("ray_core:777")) is False

    def test_cancel_returns_false_when_resolved_task_is_no_longer_pending(
        self,
        monkeypatch,
    ) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        monkeypatch.setattr(runner, "_resolve_task_pk", lambda _handle_id: 777)

        assert runner.cancel(_make_handle("02000000:disappeared")) is False
        assert fake.cancelled == []

    def test_cancel_handles_ray_exception_and_clears_pending(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.cancel_error = RuntimeError("already done")
        runner = RayCoreRunner()
        ref = _FakeObjectRef("cafebabe")
        runner._pending_tasks[5] = RayCoreHandle(
            task_pk=5,
            object_ref=ref,
            submitted_at=datetime.now(UTC),
            task_name="task",
            attempt_number=1,
            execution_generation=0,
            ray_job_id="02000000",
            ray_task_id="cafebabe",
        )

        ok = runner.cancel(_make_handle("02000000:cafebabe"))

        assert ok is False
        assert 5 not in runner._pending_tasks

    def test_cancel_pending_thread_start_failure_retires_exact_handle_and_releases_slot(
        self,
        monkeypatch,
    ) -> None:
        fake = _install_fake_ray(monkeypatch)
        failed_runner = RayCoreRunner()
        failed = RayCoreHandle(
            task_pk=51,
            object_ref=_FakeObjectRef("thread-start-failure"),
            submitted_at=datetime.now(UTC),
            task_name="failed-start",
            attempt_number=2,
            execution_generation=4,
        )
        failed_runner._pending_tasks[failed.task_pk] = failed

        class StartFailingThread:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread unavailable")

        with monkeypatch.context() as start_patch:
            start_patch.setattr("django_ray.runner.ray_core.Thread", StartFailingThread)
            failed_outcome = failed_runner.cancel_pending_with_status(failed)

        assert failed_outcome.status == CancellationOutcomeStatus.FAILED
        assert failed_outcome.message == "Ray Core cancellation worker could not start"
        assert failed.task_pk not in failed_runner._pending_tasks
        assert fake.cancelled == []

        subsequent_runner = RayCoreRunner()
        subsequent = RayCoreHandle(
            task_pk=52,
            object_ref=_FakeObjectRef("after-thread-start-failure"),
            submitted_at=datetime.now(UTC),
            task_name="subsequent",
            attempt_number=1,
            execution_generation=1,
        )
        subsequent_runner._pending_tasks[subsequent.task_pk] = subsequent

        subsequent_outcome = subsequent_runner.cancel_pending_with_status(
            subsequent,
            timeout_seconds=0.1,
        )

        assert subsequent_outcome.status == CancellationOutcomeStatus.REQUESTED
        assert fake.cancelled == [(subsequent.object_ref, False)]
        assert subsequent.task_pk not in subsequent_runner._pending_tasks

    def test_cancel_pending_rejects_replaced_handle(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        stale = RayCoreHandle(
            task_pk=5,
            object_ref=_FakeObjectRef("stale"),
            submitted_at=datetime.now(UTC),
            task_name="task",
            attempt_number=1,
            execution_generation=7,
        )
        replacement = RayCoreHandle(
            task_pk=5,
            object_ref=_FakeObjectRef("replacement"),
            submitted_at=datetime.now(UTC),
            task_name="task",
            attempt_number=2,
            execution_generation=7,
        )
        runner._pending_tasks[5] = replacement

        assert runner.cancel_pending(stale) is False
        assert fake.cancelled == []
        assert runner._pending_tasks[5] is replacement
        assert (
            runner.get_pending_handle(
                5,
                attempt_number=1,
                execution_generation=7,
            )
            is None
        )
        assert (
            runner.get_pending_handle(
                5,
                attempt_number=2,
                execution_generation=7,
            )
            is replacement
        )

    def test_cancel_pending_preserves_replacement_installed_during_ray_cancel(
        self, monkeypatch
    ) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        stale = RayCoreHandle(
            task_pk=6,
            object_ref=_FakeObjectRef("stale-in-flight"),
            submitted_at=datetime.now(UTC),
            task_name="stale",
            attempt_number=1,
            execution_generation=7,
        )
        replacement = RayCoreHandle(
            task_pk=6,
            object_ref=_FakeObjectRef("replacement-in-flight"),
            submitted_at=datetime.now(UTC),
            task_name="replacement",
            attempt_number=2,
            execution_generation=8,
        )
        runner._pending_tasks[6] = stale

        def replace_during_cancel(ref: _FakeObjectRef, force: bool = False) -> None:
            fake.cancelled.append((ref, force))
            runner._pending_tasks[6] = replacement

        fake.cancel = replace_during_cancel  # type: ignore[method-assign]

        assert runner.cancel_pending(stale) is True
        assert fake.cancelled == [(stale.object_ref, False)]
        assert runner._pending_tasks[6] is replacement

    def test_cancel_pending_timeout_is_indeterminate_and_late_completion_is_exact(
        self,
        monkeypatch,
    ) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        stale = RayCoreHandle(
            task_pk=7,
            object_ref=_FakeObjectRef("stale-hanging-cancel"),
            submitted_at=datetime.now(UTC),
            task_name="stale",
            attempt_number=2,
            execution_generation=8,
        )
        replacement = RayCoreHandle(
            task_pk=7,
            object_ref=_FakeObjectRef("replacement-after-timeout"),
            submitted_at=datetime.now(UTC),
            task_name="replacement",
            attempt_number=3,
            execution_generation=9,
        )
        cancel_started = Event()
        release_cancel = Event()

        def hanging_cancel(ref: _FakeObjectRef, force: bool = False) -> None:
            fake.cancelled.append((ref, force))
            cancel_started.set()
            release_cancel.wait()

        fake.cancel = hanging_cancel  # type: ignore[method-assign]
        runner._pending_tasks[stale.task_pk] = stale
        request_thread: threading.Thread | None = None
        try:
            outcome = runner.cancel_pending_with_status(stale, timeout_seconds=0.01)

            assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
            assert outcome.message == (
                "Ray Core cancellation timed out; the exact stop may complete later"
            )
            assert cancel_started.wait(timeout=1)
            assert stale.task_pk not in runner._pending_tasks
            request_thread = next(
                thread
                for thread in threading.enumerate()
                if thread.name == "django-ray-core-cancel-7-2-8"
            )
            assert request_thread.is_alive()
            runner._pending_tasks[replacement.task_pk] = replacement
        finally:
            release_cancel.set()
            if request_thread is not None:
                request_thread.join(timeout=1)

        assert request_thread is not None
        assert request_thread.is_alive() is False
        assert fake.cancelled == [(stale.object_ref, False)]
        assert runner._pending_tasks[replacement.task_pk] is replacement

    def test_process_wide_cancel_slot_rejects_a_second_hung_request(
        self,
        monkeypatch,
    ) -> None:
        fake = _install_fake_ray(monkeypatch)
        first_runner = RayCoreRunner()
        second_runner = RayCoreRunner()
        third_runner = RayCoreRunner()
        first = RayCoreHandle(
            task_pk=8,
            object_ref=_FakeObjectRef("first-hung-cancel"),
            submitted_at=datetime.now(UTC),
            task_name="first",
            attempt_number=1,
            execution_generation=1,
        )
        second = RayCoreHandle(
            task_pk=9,
            object_ref=_FakeObjectRef("second-saturated-cancel"),
            submitted_at=datetime.now(UTC),
            task_name="second",
            attempt_number=1,
            execution_generation=1,
        )
        third = RayCoreHandle(
            task_pk=10,
            object_ref=_FakeObjectRef("third-after-release"),
            submitted_at=datetime.now(UTC),
            task_name="third",
            attempt_number=1,
            execution_generation=1,
        )
        first_started = Event()
        release_first = Event()

        def cancel_one_at_a_time(ref: _FakeObjectRef, force: bool = False) -> None:
            fake.cancelled.append((ref, force))
            if ref is first.object_ref:
                first_started.set()
                release_first.wait()

        fake.cancel = cancel_one_at_a_time  # type: ignore[method-assign]
        first_runner._pending_tasks[first.task_pk] = first
        second_runner._pending_tasks[second.task_pk] = second
        third_runner._pending_tasks[third.task_pk] = third
        first_thread: threading.Thread | None = None
        try:
            first_outcome = first_runner.cancel_pending_with_status(first, timeout_seconds=0.01)
            assert first_outcome.status == CancellationOutcomeStatus.INDETERMINATE
            assert first_started.wait(timeout=1)
            first_thread = next(
                thread
                for thread in threading.enumerate()
                if thread.name == "django-ray-core-cancel-8-1-1"
            )
            assert first_thread.is_alive()

            second_outcome = second_runner.cancel_pending_with_status(
                second,
                timeout_seconds=0.01,
            )

            assert second_outcome.status == CancellationOutcomeStatus.INDETERMINATE
            assert second_outcome.message == (
                "Ray Core cancellation capacity is occupied; the exact stop was not attempted"
            )
            assert second.task_pk not in second_runner._pending_tasks
            assert fake.cancelled == [(first.object_ref, False)]
        finally:
            release_first.set()
            if first_thread is not None:
                first_thread.join(timeout=1)

        assert first_thread is not None
        assert first_thread.is_alive() is False

        third_outcome = third_runner.cancel_pending_with_status(third, timeout_seconds=0.1)

        assert third_outcome.status == CancellationOutcomeStatus.REQUESTED
        assert fake.cancelled == [
            (first.object_ref, False),
            (third.object_ref, False),
        ]
        assert third.task_pk not in third_runner._pending_tasks

    def test_get_logs_returns_none(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        assert runner.get_logs(_make_handle("ray_core:1")) is None

    def test_poll_completed_returns_empty_when_no_pending(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        assert runner.poll_completed() == []

    def test_poll_completed_handles_success_and_error_results(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        ref_ok = _FakeObjectRef("aaa")
        ref_err = _FakeObjectRef("bbb")
        fake.values[ref_ok] = '{"success": true, "result": 1}'
        fake.values[ref_err] = RuntimeError("task crashed with password=secret")
        fake.ready_refs.update({ref_ok, ref_err})
        runner._pending_tasks[10] = RayCoreHandle(
            task_pk=10,
            object_ref=ref_ok,
            submitted_at=datetime.now(UTC),
            task_name="ok",
            attempt_number=2,
            execution_generation=3,
        )
        runner._pending_tasks[20] = RayCoreHandle(
            task_pk=20,
            object_ref=ref_err,
            submitted_at=datetime.now(UTC),
            task_name="err",
            attempt_number=4,
            execution_generation=5,
            strict_request=True,
        )

        completed = runner.poll_completed()
        completed_by_pk = {completion.task_pk: completion for completion in completed}

        assert completed_by_pk[10].attempt_number == 2
        assert completed_by_pk[10].execution_generation == 3
        assert completed_by_pk[10].result_json == '{"success": true, "result": 1}'
        assert completed_by_pk[20].attempt_number == 4
        assert completed_by_pk[20].execution_generation == 5
        assert json.loads(completed_by_pk[20].result_json) == {
            "success": False,
            "result": None,
            "result_reference": None,
            "error": "Ray Core execution transport failed",
            "traceback": None,
            "exception_type": "RayCoreExecutionTransportError",
            "retryable": False,
        }
        assert "secret" not in completed_by_pk[20].result_json
        assert runner.pending_count == 0

    def test_poll_completed_only_crosses_selected_exact_handles(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        selected_ref = _FakeObjectRef("selected")
        retained_ref = _FakeObjectRef("retained")
        fake.values[selected_ref] = '{"success": true, "result": 1}'
        fake.values[retained_ref] = '{"success": true, "result": 2}'
        fake.ready_refs.update({selected_ref, retained_ref})
        selected = RayCoreHandle(
            task_pk=10,
            object_ref=selected_ref,
            submitted_at=datetime.now(UTC),
            task_name="selected",
            attempt_number=1,
            execution_generation=2,
        )
        retained = RayCoreHandle(
            task_pk=20,
            object_ref=retained_ref,
            submitted_at=datetime.now(UTC),
            task_name="retained",
            attempt_number=3,
            execution_generation=4,
        )
        runner._pending_tasks = {10: selected, 20: retained}

        completed = runner.poll_completed((selected,))

        assert [completion.task_pk for completion in completed] == [10]
        assert runner.pending_task_handles == (retained,)

    def test_selected_stale_handle_cannot_poll_or_retire_replacement(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        stale = RayCoreHandle(
            task_pk=10,
            object_ref=_FakeObjectRef("stale"),
            submitted_at=datetime.now(UTC),
            task_name="stale",
            attempt_number=1,
            execution_generation=2,
        )
        replacement = RayCoreHandle(
            task_pk=10,
            object_ref=_FakeObjectRef("replacement"),
            submitted_at=datetime.now(UTC),
            task_name="replacement",
            attempt_number=2,
            execution_generation=3,
        )
        runner._pending_tasks = {10: replacement}
        monkeypatch.setattr(
            fake,
            "wait",
            lambda *_args, **_kwargs: pytest.fail(
                "a stale exact handle must not cross the Ray boundary"
            ),
        )

        assert runner.poll_completed((stale,)) == []
        assert runner.retire_pending_handle(stale) is False
        assert runner.pending_task_handles == (replacement,)
        assert runner.retire_pending_handle(replacement) is True
        assert runner.pending_task_handles == ()

    def test_pending_tracking_api_returns_snapshot_and_clears(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        runner._pending_tasks[10] = RayCoreHandle(
            task_pk=10,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="task",
            attempt_number=2,
            execution_generation=7,
        )

        pending_ids = runner.pending_task_ids
        pending_handles = runner.pending_task_handles
        runner.clear_pending_tasks()

        assert pending_ids == (10,)
        assert pending_handles[0].attempt_number == 2
        assert pending_handles[0].execution_generation == 7
        assert runner.pending_task_ids == ()
        assert runner.pending_task_handles == ()
        assert runner.pending_count == 0
