"""Read historical results without importing application code selected by a row."""

from __future__ import annotations

import copy
import gc
import json
import logging
import os
import pickle
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import FunctionType
from weakref import WeakValueDictionary, ref

import pytest
from asgiref.sync import async_to_sync
from django.tasks import TaskResultStatus, task_backends
from django.tasks.base import Task
from django.tasks.exceptions import InvalidTask, TaskResultMismatch
from django.utils.inspect import _get_func_parameters

from django_ray import _result_tasks
from django_ray.backends import RayTaskBackend
from django_ray.models import RayTaskExecution, TaskState
from django_ray.result_storage import FilesystemResultStorage


@pytest.mark.parametrize("path_kind", ["unimported", "module_attribute", "removed_task"])
def test_result_reads_are_inert_in_a_fresh_process(tmp_path: Path, path_kind: str) -> None:
    from qualification.results.contract import validate_probe

    root = Path(__file__).resolve().parents[2]
    module = Path(_result_tasks.__file__).with_name("__init__.py").resolve()
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(module.parent.parent), str(root), environment.get("PYTHONPATH", ""))
    )
    process = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "qualification.results.probe",
            str(tmp_path),
            path_kind,
            str(module),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    validate_probe(json.loads(process.stdout), kind=path_kind, expected_module=str(module))


def _application_function(value=0):
    raise AssertionError("a result projection executed application code")


def _function(name="retained_task"):
    function = FunctionType(_application_function.__code__, globals(), name)
    function.__module__ = "trusted_application"
    function.__qualname__ = name
    return function


def _declare(function, *, queue_name="default"):
    # Django 6.0 requires these explicitly; later versions add defaults.
    return Task(func=function, priority=0, backend="default", queue_name=queue_name, run_after=None)


@pytest.fixture
def registry(monkeypatch):
    functions = WeakValueDictionary()
    monkeypatch.setattr(_result_tasks, "_trusted_functions", functions)
    return functions


def _projection(path="removed.tasks.work", alias="default"):
    return _result_tasks._project_result_task(
        alias=alias,
        callable_path=path,
        priority=7,
        queue_name="retired-queue",
        run_after=datetime(2025, 1, 1, tzinfo=UTC),
    )


def test_trusted_identity_and_historical_metadata_are_preserved(registry):
    declared = _declare(_function())
    projected = _projection(declared.module_path)

    assert isinstance(projected, Task)
    assert projected.func is declared.func
    assert projected.module_path == declared.module_path
    assert projected.name == "retained_task"
    assert projected.queue_name == "retired-queue"
    assert projected.priority == 7
    assert projected.run_after == datetime(2025, 1, 1, tzinfo=UTC)
    assert len(registry) == 1


def test_registration_is_alias_scoped_and_only_follows_successful_validation(registry):
    function = _function()
    with pytest.raises(InvalidTask, match="Queue"):
        _declare(function, queue_name="removed-queue")
    assert not registry

    declared = _declare(function)
    assert _projection(declared.module_path).func is function
    assert _projection(declared.module_path, alias="other").func is not function
    with pytest.raises(TypeError, match="read-only"):
        task_backends["default"].validate_task(_projection("unknown.path"))
    assert len(registry) == 1


def test_latest_validated_function_owns_identity_without_reviving_older_code(registry):
    original = _declare(_function())
    replacement = _declare(_function())
    path = original.module_path
    assert _projection(path).func is replacement.func
    assert _projection(path).func is not original.func
    del replacement
    # Django's bounded signature cache independently retains validated functions.
    # Release that framework cache to isolate our registry's ownership.
    _get_func_parameters.cache_clear()
    gc.collect()
    # An older live Task does not silently become current after the replacement dies.
    assert not registry
    assert _projection(path).func is not original.func


def test_registry_does_not_retain_dead_functions_or_cache_unknown_read_paths(registry):
    declared = _declare(_function())
    function_ref = ref(declared.func)
    assert len(registry) == 1
    del declared
    _get_func_parameters.cache_clear()
    gc.collect()
    assert function_ref() is None
    assert not registry
    for index in range(100):
        projected = _projection(f"historical.module.task_{index}")
        assert projected.module_path == f"historical.module.task_{index}"
    assert not registry


def test_unknown_identity_is_stable_per_alias_and_path(registry):
    first = _projection()
    assert first.func == _projection().func
    assert first.func != _projection("another.path").func
    assert first.func != _projection(alias="other").func
    assert first.name == "work"
    with pytest.raises(TypeError, match="read-only"):
        first.func()
    assert not registry


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("method", ["call", "acall", "enqueue", "aenqueue", "using"])
def test_read_projection_refuses_execution_and_using(registry, known, method):
    declared = _declare(_function()) if known else None
    projected = _projection(declared.module_path if declared else "unknown.task")
    operation = getattr(projected, method)
    if method.startswith("a"):
        operation = async_to_sync(operation)
    with pytest.raises(TypeError, match="explicit application Task"):
        operation()


@pytest.mark.parametrize("known", [False, True])
def test_direct_backend_and_base_enqueue_cannot_execute_read_projection(registry, known):
    declared = _declare(_function()) if known else None
    projected = _projection(declared.module_path if declared else "unknown.task")
    backend = task_backends["default"]
    for candidate in (projected, replace(projected, queue_name="default")):
        with pytest.raises(TypeError, match="read-only"):
            backend.enqueue(candidate, (), {})
        with pytest.raises(TypeError, match="read-only"):
            async_to_sync(backend.aenqueue)(candidate, (), {})
        with pytest.raises(TypeError, match="read-only"):
            Task.enqueue(candidate)
        with pytest.raises(TypeError, match="read-only"):
            async_to_sync(Task.aenqueue)(candidate)


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_result_projection_pickle_never_creates_a_stored_path_import(registry, known, protocol):
    declared = _declare(_function()) if known else None
    projected = _projection(declared.module_path if declared else "unknown.task")
    with pytest.raises(TypeError, match="read-only"):
        pickle.dumps(projected, protocol=protocol)


def test_copy_and_explicit_reconstruction_keep_projection_inert(registry):
    projected = _projection()
    for operation in (copy.copy, copy.deepcopy):
        with pytest.raises(TypeError, match="read-only"):
            operation(projected)
    with pytest.raises(TypeError, match="read-only"):
        type(projected)._reconstruct({"func": projected.module_path})


@pytest.mark.django_db
def test_unknown_projection_keeps_matching_checks_on_subsequent_reads(registry):
    backend = task_backends["default"]
    for result_id, path in (("one", "old.one"), ("two", "old.two")):
        RayTaskExecution.objects.create(task_id=result_id, callable_path=path)
    projected = backend.get_result("one").task
    assert projected.get_result("one").id == "one"
    with pytest.raises(TaskResultMismatch):
        projected.get_result("two")


@pytest.mark.django_db
def test_fetched_result_cannot_restore_a_task_from_its_persisted_path(registry):
    RayTaskExecution.objects.create(task_id="snapshot", callable_path="removed.task")
    result = task_backends["default"].get_result("snapshot")
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError, match="read-only"):
            pickle.dumps(result, protocol=protocol)
    with pytest.raises(TypeError, match="read-only"):
        copy.deepcopy(result)
    # A shallow snapshot copy keeps the same inert Task, without reconstructing it.
    assert copy.copy(result).task is result.task


@pytest.mark.django_db
def test_unregistered_result_uses_guarded_external_input_and_result_loaders(
    registry, settings, tmp_path
):
    settings.DJANGO_RAY = {
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    storage = FilesystemResultStorage(tmp_path)
    input_payload = json.dumps(
        {
            "schema": "django-ray.task-input",
            "version": 1,
            "args": [42],
            "kwargs": {"reason": "recovery"},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    input_reference = storage.store(serialized_result=input_payload)
    result_reference = storage.store(serialized_result='"execution-failed"')
    RayTaskExecution.objects.create(
        task_id="external-historical",
        callable_path="removed.application.task",
        state=TaskState.SUCCEEDED,
        args_json="null",
        kwargs_json="null",
        input_reference=input_reference,
        result_reference=result_reference,
    )
    result = task_backends["default"].get_result("external-historical")
    assert result.args == [42]
    assert result.kwargs == {"reason": "recovery"}
    assert result.return_value == "execution-failed"
    assert not registry


@pytest.mark.django_db
def test_unregistered_result_contains_malformed_references_and_redacts_diagnostics(
    registry, caplog
):
    reference = "s3://user:private-credential@bucket/object?bytes=1"
    RayTaskExecution.objects.create(
        task_id="invalid-historical",
        callable_path="removed.application.task",
        state=TaskState.SUCCEEDED,
        args_json="null",
        kwargs_json="null",
        input_reference=reference,
        result_reference=reference,
    )
    with caplog.at_level(logging.WARNING):
        result = task_backends["default"].get_result("invalid-historical")
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.args == []
    assert result.kwargs == {}
    assert result.return_value is None
    assert "Failed to load durable task input" in caplog.text
    assert "Failed to load external task result" in caplog.text
    for record in caplog.records:
        assert "private-credential" not in str(vars(record))
        assert reference not in str(vars(record))
    assert not registry


@pytest.mark.django_db
def test_explicit_application_task_still_enqueues_after_read(registry):
    declared = _declare(_function())
    backend = RayTaskBackend("default", {"QUEUES": ["default"]})
    original = backend.enqueue(declared, (42,), {})
    historical = backend.get_result(original.id)
    assert historical.task.func is declared.func
    assert original.task is declared
    repeated = declared.enqueue(*historical.args, **historical.kwargs)
    assert repeated.id != original.id
    assert RayTaskExecution.objects.count() == 2
