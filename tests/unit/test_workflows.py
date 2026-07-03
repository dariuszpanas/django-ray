"""Unit tests for Ray-native workflow signatures."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from django_ray.runtime.context import (
    durable_task_execution,
    get_current_task_execution_pk,
)
from django_ray.runtime.remote import WorkflowProgressActor
from django_ray.workflows import (
    WorkflowDefinitionError,
    chain,
    group,
    map_step,
    step,
)


def make_range(limit: int) -> list[int]:
    return list(range(limit))


def multiply(value: int, factor: int = 1) -> int:
    return value * factor


def increment(value: int) -> int:
    return value + 1


def sum_values(values: list[int]) -> int:
    return sum(values)


def run_nested_workflow(limit: int) -> int:
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=4),
        step(sum_values),
    )
    return workflow.run(limit, use_ray=True)


class _Ref:
    def __init__(self, value: Any) -> None:
        self.value = value


class _RemoteFunction:
    def __init__(self, ray: _FakeRay, function: Any) -> None:
        self.ray = ray
        self.function = function
        self.options_seen: dict[str, Any] = {}

    def options(self, **options: Any) -> _RemoteFunction:
        self.options_seen = options
        self.ray.options_seen.append(options)
        return self

    def remote(self, *args: Any, **kwargs: Any) -> _Ref:
        resolved_args = tuple(arg.value if isinstance(arg, _Ref) else arg for arg in args)
        resolved_kwargs = {
            key: value.value if isinstance(value, _Ref) else value for key, value in kwargs.items()
        }
        self.ray.submissions += 1
        return _Ref(self.function(*resolved_args, **resolved_kwargs))


class _FakeRay:
    def __init__(self, *, initialized: bool = True) -> None:
        self.initialized = initialized
        self.submissions = 0
        self.get_calls = 0
        self.options_seen: list[dict[str, Any]] = []

    def is_initialized(self) -> bool:
        return self.initialized

    def remote(self, function: Any) -> _RemoteFunction:
        return _RemoteFunction(self, function)

    def get(self, ref: _Ref) -> Any:
        self.get_calls += 1
        return ref.value


def test_local_chain_and_dynamic_map() -> None:
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=3),
        step(sum_values),
    )

    assert workflow.run(4, use_ray=False) == 18


def test_local_group_fans_out_same_input() -> None:
    workflow = chain(
        step(increment),
        group(
            step(multiply, factor=2),
            step(multiply, factor=3),
        ),
    )

    assert workflow.run(4, use_ray=False) == [10, 15]


def test_ray_chain_uses_native_submissions_and_resource_options(monkeypatch) -> None:
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=2, ray_options={"num_cpus": 0.25}),
        step(sum_values),
    )

    assert workflow.run(5) == 20
    # make_range + five multiply tasks + one collector + sum
    assert fake_ray.submissions == 8
    assert any(options.get("num_cpus") == 0.25 for options in fake_ray.options_seen)


def test_workflow_step_resolves_named_runtime_env(monkeypatch, settings) -> None:
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    settings.DJANGO_RAY = {
        "RAY_ADDRESS": "auto",
        "RUNTIME_ENV_PROFILES": {
            "thin": {"env_vars": {"DJANGO_RAY_RUNTIME_ENV": "thin"}},
        },
    }

    assert step(increment, runtime_env="thin").run(1) == 2
    assert any(
        options.get("runtime_env") == {"env_vars": {"DJANGO_RAY_RUNTIME_ENV": "thin"}}
        for options in fake_ray.options_seen
    )


def test_workflow_step_accepts_legacy_runtime_env_ray_option(monkeypatch) -> None:
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)

    signature = step(
        increment,
        ray_options={"runtime_env": {"env_vars": {"MODE": "inline"}}},
    )

    assert signature.run(1) == 2
    assert any(
        options.get("runtime_env") == {"env_vars": {"MODE": "inline"}}
        for options in fake_ray.options_seen
    )


def test_step_can_request_django_bootstrap(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: calls.append("bootstrap"),
    )

    assert step(increment, django=True).run(1, use_ray=False) == 2
    assert calls == ["bootstrap"]


def test_map_rejects_non_iterable_input() -> None:
    with pytest.raises(WorkflowDefinitionError, match="non-string iterable"):
        map_step(increment).run(1, use_ray=False)


def test_empty_compositions_are_rejected() -> None:
    with pytest.raises(WorkflowDefinitionError, match="chain requires"):
        chain()
    with pytest.raises(WorkflowDefinitionError, match="group requires"):
        group()


def test_local_function_is_rejected() -> None:
    def nested(value: int) -> int:
        return value

    with pytest.raises(WorkflowDefinitionError, match="module-level"):
        step(nested)


def test_forced_ray_mode_requires_initialized_ray(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "ray", _FakeRay(initialized=False))

    with pytest.raises(RuntimeError, match="initialized"):
        step(increment).run(1, use_ray=True)


def test_workflow_executes_on_real_ray() -> None:
    import ray

    ray.init(ignore_reinit_error=True)
    try:
        outer_task = ray.remote(run_nested_workflow)
        assert ray.get(outer_task.remote(5)) == 40
    finally:
        ray.shutdown()


def test_durable_task_context_is_scoped() -> None:
    assert get_current_task_execution_pk() is None
    with durable_task_execution(42):
        assert get_current_task_execution_pk() == 42
    assert get_current_task_execution_pk() is None


def test_progress_actor_builds_node_snapshot() -> None:
    progress = WorkflowProgressActor()
    progress.register("0.0", "prepare")
    progress.started("0.0", "prepare")
    progress.completed("0.0", "prepare")
    progress.register("0.1.m0", "leaf")
    progress.started("0.1.m0", "leaf")

    snapshot = progress.snapshot()

    assert snapshot["state"] == "RUNNING"
    assert snapshot["total_nodes"] == 2
    assert snapshot["completed_nodes"] == 1
    assert snapshot["running_nodes"] == 1
    assert snapshot["progress_percent"] == 50.0
    assert [event["state"] for event in snapshot["recent_events"]] == [
        "RUNNING",
        "SUCCEEDED",
        "RUNNING",
    ]
