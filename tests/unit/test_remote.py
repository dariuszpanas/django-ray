"""Tests for module-level Ray task and workflow executors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import django_ray.runtime.remote as remote_module
from django_ray.runtime.remote import (
    WorkflowProgressActor,
    execute_django_task_remote,
    execute_workflow_step_remote,
)


class _RemoteMethod:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def remote(self, *args) -> None:
        self.calls.append(args)


class _ProgressActor:
    def __init__(self) -> None:
        self.started = _RemoteMethod()
        self.completed = _RemoteMethod()
        self.failed = _RemoteMethod()


def workflow_target(value: int, increment: int = 0) -> int:
    return value + increment


def test_task_bootstrap_unpickles_before_django_ray_is_importable() -> None:
    import ray.cloudpickle as cloudpickle

    cloudpickle.register_pickle_by_value(remote_module)
    try:
        payload = cloudpickle.dumps(execute_django_task_remote)
    finally:
        cloudpickle.unregister_pickle_by_value(remote_module)

    unpickle_without_django_ray = """
import importlib.abc
import sys

import ray.cloudpickle as cloudpickle


class BlockDjangoRay(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django_ray" or fullname.startswith("django_ray."):
            raise ModuleNotFoundError(fullname, name=fullname)
        return None


sys.meta_path.insert(0, BlockDjangoRay())
cloudpickle.loads(sys.stdin.buffer.read())
"""
    result = subprocess.run(
        [sys.executable, "-c", unpickle_without_django_ray],
        input=payload,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def failing_workflow_target(value: int) -> int:
    raise RuntimeError(f"failed:{value}")


def workflow_context_target() -> dict[str, object] | None:
    from django_ray.runtime.context import get_current_workflow_run_identity

    return get_current_workflow_run_identity()


def test_execute_django_task_remote_logs_failure(monkeypatch, capsys) -> None:
    payload = json.dumps({"success": False, "error": "boom"})
    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", lambda *args: payload)

    result = execute_django_task_remote("tests.fake", "[]", "{}", 12)

    captured = capsys.readouterr()
    assert result == payload
    assert "[Task 12] Starting: tests.fake" in captured.out
    assert "[Task 12] FAILED: boom" in captured.err


def test_execute_django_task_remote_logs_bounded_success_metadata(monkeypatch, capsys) -> None:
    payload = json.dumps({"success": True, "result": {"password": "secret-value"}})
    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", lambda *args: payload)

    execute_django_task_remote("tests.fake", "[]", "{}", 13)

    captured = capsys.readouterr()
    assert "SUCCESS" in captured.out
    assert "secret-value" not in captured.out
    assert "result_type" in captured.out


def test_execute_django_task_remote_forwards_input_reference(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(*args, **kwargs) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return json.dumps({"success": True, "result": None})

    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", fake_execute)

    execute_django_task_remote(
        "tests.fake",
        "null",
        "null",
        14,
        input_reference="resultfs://input",
    )

    assert captured["kwargs"] == {"input_reference": "resultfs://input"}


def test_execute_django_task_remote_propagates_attempt_and_generation(monkeypatch) -> None:
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

    execute_django_task_remote(
        "tests.fake",
        "[]",
        "{}",
        15,
        attempt_number=3,
        execution_generation=9,
        compiled_graph_submission_transport="ray-client",
    )

    assert captured == {
        "task_pk": 15,
        "attempt_number": 3,
        "execution_generation": 9,
        "compiled_graph_submission_transport": "ray-client",
    }


def test_execute_workflow_step_bootstraps_and_reports_completion(monkeypatch) -> None:
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstrapped.append(True),
    )
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", lambda: {"ray_task_id": "1"})
    actor = _ProgressActor()
    log_context: dict[str, object] = {}
    monkeypatch.setattr(
        "django_ray.logging.get_logger",
        lambda _name, **kwargs: (
            log_context.update(kwargs)
            or SimpleNamespace(info=lambda *_args: None, exception=lambda *_args: None)
        ),
    )
    run_identity = {
        "schema_version": 1,
        "run_id": "00000000-0000-0000-0000-000000000011",
        "task_execution_pk": 9,
        "attempt_number": 2,
        "execution_generation": 6,
    }

    result = execute_workflow_step_remote(
        "tests.unit.test_remote.workflow_target",
        True,
        (),
        {"increment": 2},
        {},
        9,
        actor,
        "0.0",
        3,
        workflow_run_identity=run_identity,
    )

    assert result == 5
    assert bootstrapped == [True]
    assert actor.started.calls == [("0.0", "workflow_target", {"ray_task_id": "1"})]
    assert actor.completed.calls == [("0.0", "workflow_target")]
    assert actor.failed.calls == []
    assert log_context["workflow_run_id"] == run_identity["run_id"]
    assert log_context["workflow_attempt_number"] == 2
    assert log_context["workflow_execution_generation"] == 6


def test_execute_workflow_step_exposes_run_identity_to_leaf(monkeypatch) -> None:
    from django_ray.runtime.context import get_current_workflow_run_identity

    monkeypatch.setattr(remote_module, "_ray_execution_metadata", dict)
    run_identity = {
        "schema_version": 1,
        "run_id": "00000000-0000-0000-0000-000000000012",
        "task_execution_pk": 10,
        "attempt_number": 4,
        "execution_generation": 8,
    }

    result = execute_workflow_step_remote(
        "tests.unit.test_remote.workflow_context_target",
        False,
        (),
        {},
        {},
        10,
        None,
        "0.0",
        workflow_run_identity=run_identity,
    )

    assert result == run_identity
    assert get_current_workflow_run_identity() is None


def test_execute_workflow_step_reports_failure() -> None:
    import ray

    assert not ray.is_initialized()
    actor = _ProgressActor()

    with pytest.raises(RuntimeError, match="failed:3"):
        execute_workflow_step_remote(
            "tests.unit.test_remote.failing_workflow_target",
            False,
            (),
            {},
            {},
            None,
            actor,
            "0.1",
            3,
        )

    assert actor.failed.calls == [("0.1", "failing_workflow_target", "failed:3")]
    assert actor.completed.calls == []
    assert not ray.is_initialized()


def test_ray_execution_metadata_handles_context_and_runtime_errors(monkeypatch) -> None:
    context = SimpleNamespace(
        get_task_id=lambda: "task",
        get_job_id=lambda: "job",
        get_node_id=lambda: "node",
        get_worker_id=lambda: "worker",
        get_assigned_resources=lambda: {"CPU": 1},
    )
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: True,
            get_runtime_context=lambda: context,
        ),
    )

    assert remote_module._ray_execution_metadata() == {
        "ray_task_id": "task",
        "ray_job_id": "job",
        "ray_node_id": "node",
        "ray_worker_id": "worker",
        "assigned_resources": {"CPU": 1},
    }

    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: True,
            get_runtime_context=lambda: (_ for _ in ()).throw(RuntimeError("outside Ray")),
        ),
    )
    assert remote_module._ray_execution_metadata() == {}


def test_ray_execution_metadata_skips_context_when_ray_is_uninitialized(
    monkeypatch,
) -> None:
    context_lookups: list[bool] = []
    monkeypatch.setitem(
        sys.modules,
        "ray",
        SimpleNamespace(
            is_initialized=lambda: False,
            get_runtime_context=lambda: context_lookups.append(True),
        ),
    )

    assert remote_module._ray_execution_metadata() == {}
    assert context_lookups == []


def test_ray_execution_metadata_does_not_import_ray() -> None:
    probe = """
import json
import sys

ray_loaded_before = "ray" in sys.modules
from django_ray.runtime.remote import _ray_execution_metadata

payload = {
    "ray_loaded_before": ray_loaded_before,
    "ray_loaded_after_module_import": "ray" in sys.modules,
    "metadata": _ray_execution_metadata(),
    "ray_loaded_after_metadata": "ray" in sys.modules,
}
print("DJANGO_RAY_COLD_METADATA_PROBE=" + json.dumps(payload, sort_keys=True))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    marker = "DJANGO_RAY_COLD_METADATA_PROBE="
    payload_line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
    assert json.loads(payload_line.removeprefix(marker)) == {
        "metadata": {},
        "ray_loaded_after_metadata": False,
        "ray_loaded_after_module_import": False,
        "ray_loaded_before": False,
    }


def test_ray_execution_metadata_does_not_initialize_local_ray() -> None:
    probe = """
import json

import ray

from django_ray.runtime.remote import _ray_execution_metadata

payload = {
    "initialized_before": ray.is_initialized(),
    "metadata": _ray_execution_metadata(),
    "initialized_after": ray.is_initialized(),
}
print("DJANGO_RAY_METADATA_PROBE=" + json.dumps(payload, sort_keys=True))
"""
    environment = os.environ.copy()
    environment.pop("RAY_ADDRESS", None)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    marker = "DJANGO_RAY_METADATA_PROBE="
    payload_line = next(line for line in result.stdout.splitlines() if line.startswith(marker))
    assert json.loads(payload_line.removeprefix(marker)) == {
        "initialized_after": False,
        "initialized_before": False,
        "metadata": {},
    }
    combined_output = f"{result.stdout}\n{result.stderr}".lower()
    assert "local ray instance" not in combined_output


def test_progress_actor_updates_registration_and_terminal_states() -> None:
    actor = WorkflowProgressActor(task_execution_pk=7)
    actor.register(
        "0.0",
        "old",
        "old.path",
        ["old-dependency"],
        {"mode": "inherit"},
        {"num_cpus": 1},
    )
    revision = actor.revision

    actor.register(
        "0.0",
        "new",
        "new.path",
        ["new-dependency"],
        {"mode": "override"},
        {"num_cpus": 2},
    )

    node = actor.nodes["0.0"]
    assert actor.revision == revision + 1
    assert node["label"] == "new"
    assert node["callable_path"] == "new.path"
    assert node["dependencies"] == ["new-dependency"]
    assert node["runtime_env"] == {"mode": "override"}
    assert node["ray_options"] == {"num_cpus": 2}

    actor.progress("0.0", 1, 2, "half", {})
    actor.completed("0.0", "new")
    assert node["progress"]["current"] == 2
    assert node["progress"]["percent"] == 100.0
    assert actor.snapshot()["state"] == "SUCCEEDED"

    actor.failed("0.1", "failed-node", "boom")
    snapshot = actor.snapshot()
    assert snapshot["state"] == "FAILED"
    assert actor.nodes["0.1"]["error"] == "boom"


def test_progress_actor_registers_unknown_progress_node() -> None:
    actor = WorkflowProgressActor()

    actor.progress("dynamic", 1, 4, None, {"rows": 1})

    assert actor.nodes["dynamic"]["label"] == "dynamic"
    assert actor.nodes["dynamic"]["progress"]["percent"] == 25.0


def test_progress_actor_drains_updates_after_disable() -> None:
    actor = WorkflowProgressActor()
    actor.register("0.0", "before")
    actor.register_map("0.1", "map:before", ["0.0"], 2, 10)
    revision = actor.revision

    actor.disable()
    actor.started("0.0", "after")
    actor.progress("0.0", 1, 2, "late", {})
    actor.completed("0.0", "after")
    actor.failed("0.2", "late", "boom")
    actor.register_map("0.3", "map:late", ["0.0"], 4, 20)
    actor.map_progress("0.1", "map:after", 2, 1, True)

    assert actor.revision == revision
    assert actor.nodes["0.0"]["state"] == "PENDING"
    assert actor.nodes["0.1"]["fanout"]["submitted_items"] == 0
    assert "0.2" not in actor.nodes
    assert "0.3" not in actor.nodes
