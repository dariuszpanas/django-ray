"""Additional unit tests for RayCoreRunner runtime branches."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.ray_core import (
    RayCoreHandle,
    RayCoreRunner,
    _compiled_graph_submission_transport,
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
    return SimpleNamespace(
        pk=pk,
        attempt_number=attempt_number,
        execution_generation=execution_generation,
        **attributes,
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
            lambda callable_path, args_json, kwargs_json: json.dumps(
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

    def test_submit_transports_external_input_by_reference(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        captured: dict[str, object] = {}

        def fake_execute(
            callable_path: str,
            args_json: str,
            kwargs_json: str,
            *,
            input_reference: str | None = None,
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

    def test_submit_registers_remote_module_for_ray_cloudpickle(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        registered: list[object] = []
        fake.cloudpickle = SimpleNamespace(register_pickle_by_value=registered.append)
        import django_ray.runtime.remote as remote_module

        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json: json.dumps(
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
            lambda callable_path, args_json, kwargs_json: json.dumps(
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
            lambda callable_path, args_json, kwargs_json: json.dumps(
                {"success": True, "result": callable_path}
            ),
        )

        runner = RayCoreRunner()
        runner.submit(
            task_execution=_task_execution(
                12,
                runtime_env_profile="thin",
                runtime_env_json='{"env_vars":{"MODE":"thin"}}',
                runtime_env_hash="",
            ),
            callable_path="testproject.tasks.echo_task",
            args=("hello",),
            kwargs={},
        )

        assert fake.remote_calls[-1]["runtime_env"] == {"env_vars": {"MODE": "thin"}}

    def test_submit_normalizes_ray_job_id_to_hex(self, monkeypatch) -> None:
        fake = _install_fake_ray(monkeypatch)
        fake.runtime_job_id = _FakeJobID()
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda callable_path, args_json, kwargs_json: json.dumps(
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
            lambda callable_path, args_json, kwargs_json: json.dumps(
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
            lambda callable_path, args_json, kwargs_json: json.dumps(
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

    def test_cancel_returns_false_when_task_not_pending(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()

        assert runner.cancel(_make_handle("ray_core:777")) is False

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
        fake.values[ref_err] = RuntimeError("task crashed")
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
        )

        completed = runner.poll_completed()
        completed_by_pk = {completion.task_pk: completion for completion in completed}

        assert completed_by_pk[10].attempt_number == 2
        assert completed_by_pk[10].execution_generation == 3
        assert completed_by_pk[10].result_json == '{"success": true, "result": 1}'
        assert completed_by_pk[20].attempt_number == 4
        assert completed_by_pk[20].execution_generation == 5
        assert "task crashed" in completed_by_pk[20].result_json
        assert runner.pending_count == 0

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
