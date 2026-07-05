"""Tests for module-level Ray task and workflow executors."""

from __future__ import annotations

import json
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


def failing_workflow_target(value: int) -> int:
    raise RuntimeError(f"failed:{value}")


def test_execute_django_task_remote_logs_failure(monkeypatch, capsys) -> None:
    payload = json.dumps({"success": False, "error": "boom"})
    monkeypatch.setattr("django_ray.runtime.entrypoint.execute_task", lambda *args: payload)

    result = execute_django_task_remote("tests.fake", "[]", "{}", 12)

    captured = capsys.readouterr()
    assert result == payload
    assert "[Task 12] Starting: tests.fake" in captured.out
    assert "[Task 12] FAILED: boom" in captured.err


def test_execute_workflow_step_bootstraps_and_reports_completion(monkeypatch) -> None:
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstrapped.append(True),
    )
    monkeypatch.setattr(remote_module, "_ray_execution_metadata", lambda: {"ray_task_id": "1"})
    actor = _ProgressActor()

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
    )

    assert result == 5
    assert bootstrapped == [True]
    assert actor.started.calls == [("0.0", "workflow_target", {"ray_task_id": "1"})]
    assert actor.completed.calls == [("0.0", "workflow_target")]
    assert actor.failed.calls == []


def test_execute_workflow_step_reports_failure() -> None:
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
        SimpleNamespace(get_runtime_context=lambda: context),
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
            get_runtime_context=lambda: (_ for _ in ()).throw(RuntimeError("outside Ray"))
        ),
    )
    assert remote_module._ray_execution_metadata() == {}


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
