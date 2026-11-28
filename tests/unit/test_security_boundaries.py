"""Unit tests for callable resolution and shared-filesystem storage boundaries."""

from __future__ import annotations

import errno
import functools
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.tasks import task
from django.tasks.base import Task

from django_ray.result_storage import (
    FilesystemResultStorage,
    ResultStorageError,
    ResultStorageIntegrityError,
    _write_temporary_payload,
)
from django_ray.runtime import import_utils
from django_ray.runtime.import_utils import (
    get_driver_allowed_callable_modules,
    resolve_task_callable,
    resolve_workflow_callable,
)


@task
def _sample_task(value: int) -> int:
    return value + 1


def _plain_workflow_step(value: int) -> int:
    return value * 2


def _add(left: int, right: int) -> int:
    return left + right


_partial_callable = functools.partial(_add, 10)
_non_callable_value = "not-callable"


class TestResolveTaskCallable:
    """Boundary checks for top-level task resolution."""

    def test_resolves_registered_django_task(self, settings) -> None:
        settings.DJANGO_RAY = {}
        # User app in INSTALLED_APPS (testproject) is permitted by default.
        default_resolved = resolve_task_callable("testproject.tasks.echo_task")
        assert isinstance(default_resolved, Task)

        resolved = resolve_task_callable(
            f"{__name__}._sample_task",
            allowed_modules=(__name__,),
        )
        assert isinstance(resolved, Task)
        assert resolved is _sample_task

    def test_rejects_unlisted_module_before_import_by_default(self, monkeypatch, settings) -> None:
        settings.DJANGO_RAY = {}
        imported: list[str] = []

        def fail_import(name: str):
            imported.append(name)
            raise AssertionError(f"import_module must not run for blocked module {name}")

        monkeypatch.setattr(import_utils, "import_module", fail_import)

        for blocked in (
            "json.dumps",
            "django.utils.html.escape",
            "django_ray.runtime.import_utils.import_callable",
        ):
            with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
                resolve_task_callable(blocked)
        assert imported == []

    def test_rejects_plain_function_and_partial(self) -> None:
        with pytest.raises(TypeError, match="not a Django Task"):
            resolve_task_callable(
                f"{__name__}._plain_workflow_step",
                allowed_modules=(__name__,),
            )

        with pytest.raises(TypeError, match="not a Django Task"):
            resolve_task_callable(
                f"{__name__}._partial_callable",
                allowed_modules=(__name__,),
            )

        with pytest.raises(TypeError, match="not a Django Task"):
            resolve_task_callable("json.dumps", allowed_modules=("json",))

    def test_enforces_allowed_modules_before_importing(self, monkeypatch) -> None:
        imported: list[str] = []

        def fail_import(name: str):
            imported.append(name)
            raise AssertionError(f"import_module must not run for blocked module {name}")

        monkeypatch.setattr(import_utils, "import_module", fail_import)

        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_task_callable(
                "untrusted.pkg.fn",
                allowed_modules=("testproject.tasks",),
            )
        assert imported == []

    def test_honors_django_ray_allowed_callable_modules_setting(self, settings) -> None:
        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": ["testproject"]}
        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_task_callable(f"{__name__}._sample_task")

        settings.DJANGO_RAY = {"ALLOWED_CALLABLE_MODULES": [__name__]}
        assert resolve_task_callable(f"{__name__}._sample_task") is _sample_task

    @pytest.mark.parametrize("bad_path", ["", "nodot", "a..b", "1invalid.mod.fn", "a.b.1bad"])
    def test_rejects_malformed_dotted_paths(self, bad_path: str) -> None:
        with pytest.raises(ImportError, match="Invalid callable path"):
            resolve_task_callable(bad_path)

    def test_driver_allowlist_and_unconfigured_settings_fail_closed(
        self,
        monkeypatch,
        settings,
    ) -> None:
        settings.DJANGO_RAY = {}
        assert "testproject" in get_driver_allowed_callable_modules()
        assert "django" not in get_driver_allowed_callable_modules()

        monkeypatch.setattr("django.conf.settings", SimpleNamespace(configured=False))
        assert get_driver_allowed_callable_modules() == ()
        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_task_callable(f"{__name__}._sample_task")
        assert (
            resolve_task_callable(f"{__name__}._sample_task", allowed_modules=(__name__,))
            is _sample_task
        )


class TestResolveWorkflowCallable:
    """Pre-import allowlist enforcement for workflow step/fold callables."""

    def test_blocks_unlisted_module_before_import(self, monkeypatch, settings) -> None:
        settings.DJANGO_RAY = {}
        imported: list[str] = []

        def fail_import(name: str):
            imported.append(name)
            raise AssertionError(f"import_module must not run for blocked module {name}")

        monkeypatch.setattr(import_utils, "import_module", fail_import)

        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_workflow_callable("json.dumps")
        assert imported == []

    def test_defaults_to_non_framework_installed_apps(self, settings) -> None:
        settings.DJANGO_RAY = {}
        # User app in INSTALLED_APPS (testproject) is permitted by default;
        # django and django_ray framework modules are excluded by default.
        resolved = resolve_workflow_callable("testproject.tasks.echo_task")
        assert callable(resolved)
        assert not isinstance(resolved, Task)

        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_workflow_callable("django_ray.runtime.import_utils.import_callable")

        with pytest.raises(ImportError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_workflow_callable("django.utils.html.escape")

    def test_preserves_functools_partial_and_unwraps_django_task(self) -> None:
        resolved_partial = resolve_workflow_callable(
            f"{__name__}._partial_callable",
            allowed_modules=(__name__,),
        )
        assert isinstance(resolved_partial, functools.partial)
        assert resolved_partial(5) == 15

        resolved_task_fn = resolve_workflow_callable(
            f"{__name__}._sample_task",
            allowed_modules=(__name__,),
        )
        assert resolved_task_fn is _sample_task.func
        assert resolved_task_fn(4) == 5

    def test_rejects_non_callable_and_invalid_allowlist_entries(self) -> None:
        with pytest.raises(TypeError, match="is not callable"):
            resolve_workflow_callable(
                f"{__name__}._non_callable_value",
                allowed_modules=(__name__,),
            )

        with pytest.raises(ValueError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_workflow_callable(
                f"{__name__}._plain_workflow_step",
                allowed_modules=("*",),
            )

        with pytest.raises(ValueError, match="ALLOWED_CALLABLE_MODULES"):
            resolve_workflow_callable(
                f"{__name__}._plain_workflow_step",
                allowed_modules="testproject",  # type: ignore[arg-type]
            )


class TestSharedFilesystemStorageBoundaries:
    """Permission mode and safe-open invariants for shared filesystem storage."""

    def test_temp_file_creation_uses_restrictive_mode_by_default(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        recorded_modes: list[int] = []
        real_open = os.open

        def spy_open(path, flags, mode=0o777, *args, **kwargs):
            recorded_modes.append(mode)
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy_open)
        temp_path = _write_temporary_payload(tmp_path, b'{"ok":true}')
        try:
            assert recorded_modes == [0o600]
            if os.name == "posix":
                assert stat.S_IMODE(temp_path.stat().st_mode) == 0o600
        finally:
            temp_path.unlink(missing_ok=True)

    def test_allows_0o640_opt_in_and_rejects_permissive_modes(
        self,
        monkeypatch,
        tmp_path: Path,
        settings,
    ) -> None:
        recorded_modes: list[int] = []
        real_open = os.open

        def spy_open(path, flags, mode=0o777, *args, **kwargs):
            recorded_modes.append(mode)
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy_open)
        settings.DJANGO_RAY = {"RESULT_STORAGE_FILESYSTEM_FILE_MODE": 0o640}
        storage = FilesystemResultStorage(tmp_path)
        reference = storage.store(serialized_result='{"group":"shared"}')
        assert storage.load(reference=reference) == '{"group":"shared"}'
        assert 0o640 in recorded_modes

        with pytest.raises(ResultStorageError, match="0o600 or 0o640"):
            FilesystemResultStorage(tmp_path, file_mode=0o666)

        settings.DJANGO_RAY = {"RESULT_STORAGE_FILESYSTEM_FILE_MODE": "0644"}
        with pytest.raises(ResultStorageError, match="0o600 or 0o640"):
            FilesystemResultStorage(tmp_path)

    def test_read_passes_nofollow_and_nonblock_flags_and_rejects_non_regular_files(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        storage = FilesystemResultStorage(tmp_path)
        reference = storage.store(serialized_result='{"value":1}')

        expected_flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        seen_flags: list[int] = []
        real_open = os.open

        def spy_open(path, flags, *args, **kwargs):
            seen_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy_open)
        assert storage.load(reference=reference) == '{"value":1}'
        assert seen_flags
        assert (seen_flags[-1] & expected_flags) == expected_flags

        # Simulate a non-regular file descriptor (e.g. FIFO) observed via fstat.
        fake_fifo_stat = SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_size=len('{"value":1}'))
        monkeypatch.setattr(os, "fstat", lambda _fd: fake_fifo_stat)
        with pytest.raises(ResultStorageIntegrityError, match="integrity verification"):
            storage.load(reference=reference)

    def test_read_rejects_eloop_and_symlinks(self, monkeypatch, tmp_path: Path) -> None:
        storage = FilesystemResultStorage(tmp_path)
        reference = storage.store(serialized_result='{"value":2}')

        def raise_eloop(*_args, **_kwargs):
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")

        monkeypatch.setattr(os, "open", raise_eloop)
        with pytest.raises(ResultStorageIntegrityError, match="integrity verification"):
            storage.load(reference=reference)


class TestPublicRuntimeHardening:
    """Regression tests for functools.partial preservation and nested leaf checks."""

    def test_import_callable_does_not_unwrap_functools_partial(self) -> None:
        resolved = import_utils.import_callable(f"{__name__}._partial_callable")
        assert isinstance(resolved, functools.partial)
        assert resolved(7) == 17

        resolved_task = import_utils.import_callable(f"{__name__}._sample_task")
        assert resolved_task is _sample_task.func

    def test_distributed_leaf_forbids_none_nested_request_under_durable_context(
        self, monkeypatch
    ) -> None:
        import pickle

        from django_ray.execution_codec import (
            NestedExecutionBoundaryKind,
            NestedExecutionRequestRejected,
            NestedExecutionRequestRejection,
        )
        from django_ray.runtime import distributed
        from django_ray.runtime.context import durable_task_execution

        pickled = pickle.dumps(_plain_workflow_step)
        monkeypatch.setattr(
            pickle,
            "loads",
            lambda *_args, **_kwargs: pytest.fail("pickle.loads must not execute"),
        )

        with (
            durable_task_execution(42, task_id="durable-nested-42"),
            pytest.raises(NestedExecutionRequestRejected) as caught,
        ):
            distributed._parallel_map_remote(pickled, 3, {})
        assert caught.value.classification is NestedExecutionRequestRejection.INVALID_VERSIONED

        with (
            durable_task_execution(42, task_id="durable-nested-42"),
            pytest.raises(NestedExecutionRequestRejected),
        ):
            with distributed._nested_distributed_leaf_execution(
                pickled,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                boundary_kind=NestedExecutionBoundaryKind.DISTRIBUTED_MAP,
            ):
                pass
