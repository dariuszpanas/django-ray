"""Unit tests for Ray Core cancellation recovery, peek/ack, address, and RuntimeEnv."""

from __future__ import annotations

import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
from django_ray.runtime.runtime_env import (
    normalize_runtime_env,
    prepare_runtime_env_for_ray_core,
    snapshot_local_runtime_env,
)
from django_ray.workflow.plans import _local_path_identity, runtime_env_plan_identity


class _FakeRef:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"_FakeRef({self.name!r})"


class _FakeRay(ModuleType):
    def __init__(self) -> None:
        super().__init__("ray")
        self.initialized = True
        self.values: dict[_FakeRef, Any] = {}
        self.ready: set[_FakeRef] = set()
        self.cancelled: list[tuple[_FakeRef, bool]] = []
        self.get_calls: list[_FakeRef] = []
        self.gcs_address = "10.0.0.1:6379"

    def is_initialized(self) -> bool:
        return self.initialized

    def init(self, **_kwargs: Any) -> None:
        self.initialized = True

    def get_runtime_context(self) -> Any:
        return SimpleNamespace(gcs_address=self.gcs_address, get_job_id=lambda: "02000000")

    def wait(
        self,
        refs: list[_FakeRef],
        *,
        num_returns: int = 1,
        timeout: float | None = None,
        fetch_local: bool = True,
    ) -> tuple[list[_FakeRef], list[_FakeRef]]:
        del timeout, fetch_local
        ready = [ref for ref in refs if ref in self.ready][:num_returns]
        pending = [ref for ref in refs if ref not in ready]
        return ready, pending

    def get(self, ref: _FakeRef) -> Any:
        self.get_calls.append(ref)
        val = self.values[ref]
        if isinstance(val, Exception):
            raise val
        return val

    def cancel(self, ref: _FakeRef, *, force: bool = False) -> None:
        self.cancelled.append((ref, force))


def _make_handle(pk: int, ref: _FakeRef, attempt: int = 1, gen: int = 1) -> RayCoreHandle:
    return RayCoreHandle(
        task_pk=pk,
        object_ref=ref,
        submitted_at=datetime.now(UTC),
        task_name=f"task-{pk}",
        attempt_number=attempt,
        execution_generation=gen,
    )


class TestRayCoreCancellationAndCompletionLifecycle:
    """F-013, F-014, F-022, & F-068."""

    def test_single_hung_cancel_does_not_brick_other_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeRay()
        monkeypatch.setitem(sys.modules, "ray", fake)
        runner = RayCoreRunner()

        hung_ref = _FakeRef("hung")
        healthy_ref = _FakeRef("healthy")
        hung_handle = _make_handle(1, hung_ref)
        healthy_handle = _make_handle(2, healthy_ref)
        runner._pending_tasks[1] = hung_handle
        runner._pending_tasks[2] = healthy_handle

        started = threading.Event()
        release = threading.Event()

        def cancel_fn(ref: _FakeRef, force: bool = False) -> None:
            fake.cancelled.append((ref, force))
            if ref is hung_ref:
                started.set()
                release.wait()

        fake.cancel = cancel_fn  # type: ignore[method-assign]
        try:
            first = runner.cancel_pending_with_status(hung_handle, timeout_seconds=0.01)
            assert first.status == CancellationOutcomeStatus.INDETERMINATE
            assert started.wait(timeout=1.0)
            assert runner.hung_cancel_count == 1

            # Second task on the SAME runner must still cancel cleanly!
            second = runner.cancel_pending_with_status(healthy_handle, timeout_seconds=0.5)
            assert second.status == CancellationOutcomeStatus.REQUESTED
            assert 2 not in runner._pending_tasks
        finally:
            release.set()

    def test_peek_completed_retains_handle_until_acknowledge_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeRay()
        monkeypatch.setitem(sys.modules, "ray", fake)
        runner = RayCoreRunner()

        ref = _FakeRef("done-1")
        handle = _make_handle(11, ref)
        runner._pending_tasks[11] = handle
        fake.ready.add(ref)
        fake.values[ref] = '{"success":true,"result":42}'

        first_peek = runner.peek_completed()
        assert len(first_peek) == 1
        assert first_peek[0].handle is handle
        assert runner.pending_count == 1

        # Re-peeking after a transient DB error serves cached completion without re-calling ray.get
        second_peek = runner.peek_completed()
        assert len(second_peek) == 1
        assert second_peek[0].result_json == '{"success":true,"result":42}'
        assert len(fake.get_calls) == 1

        # Explicit ack retires handle cleanly
        assert runner.acknowledge_completed(handle) is True
        assert runner.pending_count == 0
        assert runner.peek_completed() == []

    def test_drain_ready_for_shutdown_separates_ready_from_inflight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeRay()
        monkeypatch.setitem(sys.modules, "ray", fake)
        runner = RayCoreRunner()

        ready_ref = _FakeRef("ready")
        inflight_ref = _FakeRef("inflight")
        h_ready = _make_handle(21, ready_ref)
        h_inflight = _make_handle(22, inflight_ref)
        runner._pending_tasks[21] = h_ready
        runner._pending_tasks[22] = h_inflight
        fake.ready.add(ready_ref)
        fake.values[ready_ref] = '{"success":true,"result":"ok"}'

        ready_completions, remaining = runner.drain_ready_for_shutdown()

        assert len(ready_completions) == 1
        assert ready_completions[0].task_pk == 21
        assert ready_completions[0].handle is h_ready
        assert remaining == (h_inflight,)
        # Ready handle stays pending until caller commits DB and acknowledges it
        assert 21 in runner._pending_tasks

    def test_explicit_address_preserved_on_effective_ray_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeRay()
        monkeypatch.setitem(sys.modules, "ray", fake)
        monkeypatch.setenv("RAY_ADDRESS", "ray://ambient:10001")

        runner = RayCoreRunner(address="ray://explicit-cluster:10001")
        assert runner._effective_ray_address() == "ray://explicit-cluster:10001"

        default_runner = RayCoreRunner()
        assert default_runner._effective_ray_address() == "ray://ambient:10001"


class TestRuntimeEnvParityRedactionAndCaching:
    """F-027, F-071, F-092, & F-093."""

    def test_normalize_runtime_env_rejects_nan(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="JSON-serializable"):
            normalize_runtime_env({"env_vars": {}, "extra": float("nan")})

    def test_prepare_runtime_env_for_ray_core_honors_ignore_gitignore_and_redacts_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        work_dir = tmp_path / "app"
        work_dir.mkdir()
        (work_dir / "main.py").write_text("x = 1\n", encoding="utf-8")

        observed_flags: list[bool] = []

        def fake_upload_working_dir(spec: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            observed_flags.append(bool(kwargs["include_gitignore"]))
            return spec

        def fake_upload_py_modules(spec: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            observed_flags.append(bool(kwargs["include_gitignore"]))
            return spec

        monkeypatch.setenv("RAY_RUNTIME_ENV_IGNORE_GITIGNORE", "1")
        fake_wd_mod = ModuleType("ray._private.runtime_env.working_dir")
        fake_wd_mod.upload_working_dir_if_needed = fake_upload_working_dir  # type: ignore[attr-defined]
        fake_pm_mod = ModuleType("ray._private.runtime_env.py_modules")
        fake_pm_mod.upload_py_modules_if_needed = fake_upload_py_modules  # type: ignore[attr-defined]
        fake_rc_mod = ModuleType("ray._private.ray_constants")
        fake_rc_mod.RAY_RUNTIME_ENV_IGNORE_GITIGNORE = "RAY_RUNTIME_ENV_IGNORE_GITIGNORE"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ray._private.runtime_env.working_dir", fake_wd_mod)
        monkeypatch.setitem(sys.modules, "ray._private.runtime_env.py_modules", fake_pm_mod)
        monkeypatch.setitem(sys.modules, "ray._private.ray_constants", fake_rc_mod)

        env = normalize_runtime_env({"working_dir": str(work_dir)})
        prepare_runtime_env_for_ray_core(env)
        assert observed_flags == [False, False]

        # Secret embedded in upload failure must be redacted (F-092)
        def failing_upload(_spec: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("upload failed token=super-secret-token-value")

        fake_wd_mod.upload_working_dir_if_needed = failing_upload  # type: ignore[attr-defined]
        with pytest.raises(ImproperlyConfigured) as exc_info:
            prepare_runtime_env_for_ray_core(env)
        assert "super-secret-token-value" not in str(exc_info.value)

    def test_snapshot_local_runtime_env_matches_plan_identity_for_relative_dot_py_module(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pytest.importorskip("ray._private.runtime_env.packaging")
        pkg_dir = tmp_path / "my_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

        monkeypatch.chdir(pkg_dir)
        env = normalize_runtime_env({"py_modules": ["."]})
        plan_id = runtime_env_plan_identity(env)
        with snapshot_local_runtime_env(env) as snapped:
            snap_id = runtime_env_plan_identity(snapped)

        assert snap_id.manifest["digest"] == plan_id.manifest["digest"]

    def test_local_path_identity_file_hash_cache_invalidates_on_edit(self, tmp_path: Path) -> None:
        sample = tmp_path / "mod.py"
        sample.write_text("x = 1\n", encoding="utf-8")
        first = _local_path_identity(sample, excludes=[])
        second = _local_path_identity(sample, excludes=[])
        assert first == second

        sample.write_text("x = 123456\n", encoding="utf-8")
        os.utime(sample, ns=(sample.stat().st_atime_ns, sample.stat().st_mtime_ns + 10_000))
        third = _local_path_identity(sample, excludes=[])
        assert third["sha256"] != first["sha256"]
