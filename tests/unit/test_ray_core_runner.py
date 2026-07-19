"""Additional unit tests for RayCoreRunner runtime branches."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from django_ray.runner.base import JobStatus, SubmissionHandle
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner


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
        self.default_hex = "abcdef0123456789" * 4

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
            task_execution=SimpleNamespace(pk=11),
            callable_path="testproject.tasks.add_numbers",
            args=(3, 4),
            kwargs={"x": 1},
        )

        assert handle.ray_job_id.startswith("02000000:")
        assert handle.ray_address == "ray://unit-submit:10001"
        assert runner.pending_count == 1
        pending = runner._pending_tasks[11]
        assert pending.ray_job_id == "02000000"
        assert pending.ray_task_id == fake.default_hex[:48]

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
        task_execution = SimpleNamespace(
            pk=14,
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
            task_execution=SimpleNamespace(pk=23),
            callable_path="testproject.tasks.echo_task",
            args=("hello",),
            kwargs={},
        )

        assert registered == [remote_module]

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
            task_execution=SimpleNamespace(
                pk=12,
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
            task_execution=SimpleNamespace(pk=13),
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
            task_execution=SimpleNamespace(pk=22),
            callable_path="testproject.tasks.echo_task",
            args=("a",),
            kwargs={},
        )

        assert handle.ray_job_id == "ray_core:22"

    def test_build_composite_id_returns_none_without_ids(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        core_handle = RayCoreHandle(
            task_pk=1,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="task",
        )
        assert runner._build_composite_id(core_handle) is None

    def test_resolve_task_pk_returns_none_for_invalid_legacy_id(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        assert runner._resolve_task_pk("ray_core:not-an-int") is None

    def test_get_status_returns_succeeded_when_pending_missing(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()

        info = runner.get_status(_make_handle("ray_core:999"))

        assert info.status == JobStatus.SUCCEEDED

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
        )

        info = runner.get_status(_make_handle("ray_core:3"))

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
        )

        info = runner.get_status(_make_handle("ray_core:4"))

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
        )

        ok = runner.cancel(_make_handle("ray_core:5"))

        assert ok is False
        assert 5 not in runner._pending_tasks

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
        )
        runner._pending_tasks[20] = RayCoreHandle(
            task_pk=20,
            object_ref=ref_err,
            submitted_at=datetime.now(UTC),
            task_name="err",
        )

        completed = runner.poll_completed()
        completed_map = dict(completed)

        assert 10 in completed_map
        assert completed_map[10] == '{"success": true, "result": 1}'
        assert 20 in completed_map
        assert "task crashed" in completed_map[20]
        assert runner.pending_count == 0

    def test_pending_tracking_api_returns_snapshot_and_clears(self, monkeypatch) -> None:
        _install_fake_ray(monkeypatch)
        runner = RayCoreRunner()
        runner._pending_tasks[10] = RayCoreHandle(
            task_pk=10,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="task",
        )

        pending_ids = runner.pending_task_ids
        runner.clear_pending_tasks()

        assert pending_ids == (10,)
        assert runner.pending_task_ids == ()
        assert runner.pending_count == 0
