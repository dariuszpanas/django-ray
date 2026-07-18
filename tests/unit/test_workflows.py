"""Unit tests for Ray-native workflow signatures."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime.context import (
    durable_task_execution,
    get_current_task_execution_pk,
    workflow_step_execution,
)
from django_ray.runtime.remote import WorkflowProgressActor
from django_ray.workflows import (
    WorkflowDefinitionError,
    _callable_path,
    _Executor,
    _get_executor,
    _LocalExecutor,
    _RayExecutor,
    chain,
    group,
    map_step,
    report_progress,
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


@dataclass
class _GraphExecutor(_Executor):
    nodes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        self.nodes[node_id] = dependencies
        callable_obj = __import__(
            signature.callable_path.rsplit(".", 1)[0],
            fromlist=[signature.callable_path.rsplit(".", 1)[1]],
        )
        function = getattr(callable_obj, signature.callable_path.rsplit(".", 1)[1])
        kwargs = {**input_kwargs, **signature.bound_kwargs}
        return function(
            *input_args,
            *signature.bound_args,
            **kwargs,
        )

    def collect(self, values):
        return values

    def resolve(self, value):
        return value


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
        self.init_calls: list[dict[str, Any]] = []
        self.remote_calls: list[dict[str, Any]] = []

    def is_initialized(self) -> bool:
        return self.initialized

    def init(self, **kwargs: Any) -> None:
        self.init_calls.append(kwargs)
        self.initialized = True

    def remote(self, *args, **kwargs: Any):
        self.remote_calls.append(kwargs)

        def _decorator(fn):
            fake = self

            class _RemoteCallable:
                @staticmethod
                def remote(*args: Any, **kw: Any) -> _Ref:
                    resolved_args = tuple(
                        arg.value if isinstance(arg, _Ref) else arg for arg in args
                    )
                    resolved_kwargs = {
                        key: value.value if isinstance(value, _Ref) else value
                        for key, value in kw.items()
                    }
                    fake.submissions += 1
                    return _Ref(fn(*resolved_args, **resolved_kwargs))

                def options(self, **kw: Any):
                    fake.options_seen.append(kw)
                    return self

            return _RemoteCallable()

        if args and callable(args[0]):
            return _decorator(args[0])
        return _decorator

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


def test_workflow_submission_captures_group_dependency_edges(monkeypatch) -> None:
    executor = _GraphExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    workflow = chain(
        step(increment),
        group(
            step(multiply, factor=2),
            step(multiply, factor=3),
        ),
        step(sum_values),
    )

    assert workflow.run(4) == 25
    assert executor.nodes == {
        "0.0": (),
        "0.1.g0": ("0.0",),
        "0.1.g1": ("0.0",),
        "0.2": ("0.1.g0", "0.1.g1"),
    }


def test_workflow_submission_captures_dynamic_map_edges(monkeypatch) -> None:
    executor = _GraphExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=2),
        step(sum_values),
    )

    assert workflow.run(3) == 6
    assert executor.nodes["0.1.m0"] == ("0.0",)
    assert executor.nodes["0.1.m1"] == ("0.0",)
    assert executor.nodes["0.1.m2"] == ("0.0",)
    assert executor.nodes["0.2"] == ("0.1.m0", "0.1.m1", "0.1.m2")


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


def test_step_defensively_copies_inline_runtime_env() -> None:
    runtime_env = {"env_vars": {"MODE": "inline"}}
    signature = step(increment, runtime_env=runtime_env)

    runtime_env["env_vars"]["MODE"] = "mutated"

    assert signature.runtime_env == {"env_vars": {"MODE": "inline"}}


def test_with_runtime_env_defensively_copies_inline_runtime_env() -> None:
    runtime_env = {"env_vars": {"MODE": "inline"}}
    signature = step(increment).with_runtime_env(runtime_env)

    runtime_env["env_vars"]["MODE"] = "mutated"

    assert signature.runtime_env == {"env_vars": {"MODE": "inline"}}


def test_with_options_copies_signature_metadata() -> None:
    original = step(
        increment,
        ray_options={"num_cpus": 1},
        runtime_env={"env_vars": {"MODE": "inline"}},
    )

    updated = original.with_options(num_gpus=1)
    assert isinstance(updated.runtime_env, dict)
    updated.runtime_env["env_vars"]["MODE"] = "changed"

    assert updated.ray_options == {"num_cpus": 1, "num_gpus": 1}
    assert original.runtime_env == {"env_vars": {"MODE": "inline"}}


def test_callable_path_supports_wrappers_and_rejects_invalid_shapes() -> None:
    assert (
        _callable_path("tests.unit.test_workflows.increment")
        == "tests.unit.test_workflows.increment"
    )
    wrapper = SimpleNamespace(module_path="tests.unit.test_workflows.increment")

    assert _callable_path(wrapper) == "tests.unit.test_workflows.increment"
    with pytest.raises(WorkflowDefinitionError, match="dotted import path"):
        step("increment")
    with pytest.raises(WorkflowDefinitionError, match="not methods"):
        step(_GraphExecutor().collect)


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


def test_map_requires_one_positional_input() -> None:
    with pytest.raises(WorkflowDefinitionError, match="exactly one iterable"):
        map_step(increment).run([1, 2], extra=True, use_ray=False)


def test_step_rejects_duplicate_runtime_env_options() -> None:
    with pytest.raises(WorkflowDefinitionError, match="not in both"):
        step(
            increment,
            runtime_env="thin",
            ray_options={"runtime_env": {"env_vars": {"MODE": "inline"}}},
        )


def test_map_rejects_options_on_existing_signature() -> None:
    with pytest.raises(WorkflowDefinitionError, match="cannot be added"):
        map_step(step(increment), django=True)


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


def test_ray_job_workflow_lazily_initializes_ray(monkeypatch) -> None:
    fake_ray = _FakeRay(initialized=False)
    executor = object()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    monkeypatch.setattr("django_ray.workflows._RayExecutor", lambda: executor)

    with durable_task_execution(42, ray_job_driver=True):
        assert _get_executor(True) is executor

    assert fake_ray.init_calls == [{"address": "auto", "ignore_reinit_error": True}]


def test_ray_executor_progress_flush_handles_unavailable_actor() -> None:
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = None
    executor.task_execution_pk = 1

    assert executor._flush_progress() is None

    snapshot_ref = object()
    executor.progress_actor = SimpleNamespace(snapshot=SimpleNamespace(remote=lambda: snapshot_ref))
    executor.ray = SimpleNamespace(wait=lambda refs, timeout: ([], refs))
    assert executor._flush_progress() is None

    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: (_ for _ in ()).throw(RuntimeError("actor died")),
    )
    assert executor._flush_progress() is None
    assert executor.progress_actor is None


def test_finish_progress_returns_when_snapshot_is_unavailable(monkeypatch) -> None:
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: None)

    executor.finish_progress()


def test_finish_progress_waits_for_terminal_snapshot(monkeypatch) -> None:
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = object()
    snapshots = iter(
        [
            {"completed_nodes": 0, "failed_nodes": 0, "total_nodes": 1},
            {"completed_nodes": 1, "failed_nodes": 0, "total_nodes": 1},
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(executor, "_flush_progress", lambda **kwargs: next(snapshots))
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress()

    assert sleeps == [0.05]


def test_ray_executor_submit_ignores_missing_ray_task_id() -> None:
    class _BadRef:
        def task_id(self):
            raise RuntimeError("task id unavailable")

    class _RemoteStep:
        def options(self, **kwargs):
            return self

        def remote(self, *args, **kwargs):
            return _BadRef()

    class _RemoteMethod:
        def remote(self, *args):
            del args

    executor = object.__new__(_RayExecutor)
    executor.task_context = None
    executor.task_execution_pk = None
    executor.progress_actor = SimpleNamespace(
        register=_RemoteMethod(),
        submitted=_RemoteMethod(),
    )
    executor.remote_step = _RemoteStep()

    executor.submit_step(step(increment), (), {}, "0.0", ())


@pytest.mark.django_db
def test_ray_executor_flushes_failed_progress_snapshot() -> None:
    from django_ray.models import RayTaskExecution

    execution = RayTaskExecution.objects.create(
        task_id="workflow-flush",
        callable_path="tests.unit.test_workflows.increment",
    )
    snapshot_ref = object()
    snapshot = {
        "revision": 2,
        "state": "RUNNING",
        "completed_nodes": 0,
        "failed_nodes": 1,
        "total_nodes": 1,
    }
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=SimpleNamespace(remote=lambda: snapshot_ref))
    executor.task_execution_pk = execution.pk
    executor.last_progress_revision = -1
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: snapshot,
    )

    assert executor._flush_progress(failed=True)["state"] == "FAILED"

    execution.refresh_from_db()
    assert json.loads(execution.progress_data)["state"] == "FAILED"


@pytest.mark.real_ray
def test_workflow_executes_on_real_ray() -> None:
    import ray

    ray.init(ignore_reinit_error=True)
    try:
        outer_task = ray.remote(run_nested_workflow)
        assert ray.get(outer_task.remote(5)) == 40
    finally:
        ray.shutdown()


@pytest.mark.real_ray
@pytest.mark.django_db
def test_real_ray_workflow_persists_graph_and_execution_metadata() -> None:
    import ray

    from django_ray.models import RayTaskExecution

    execution = RayTaskExecution.objects.create(
        task_id="real-ray-workflow-graph",
        callable_path="tests.unit.test_workflows.run_nested_workflow",
    )
    workflow = chain(
        step(increment),
        step(multiply, factor=2),
    )

    ray.init(ignore_reinit_error=True)
    try:
        with durable_task_execution(
            execution.pk,
            runtime_env_profile="test",
            runtime_env_hash="abc123",
        ):
            assert workflow.run(2, use_ray=True) == 6
    finally:
        ray.shutdown()

    execution.refresh_from_db()
    progress = json.loads(execution.progress_data)
    nodes = progress["graph"]["nodes"]

    assert progress["state"] == "SUCCEEDED"
    assert progress["graph"]["edges"] == [{"source": "0.0", "target": "0.1"}]
    assert nodes[0]["runtime_env"] == {
        "mode": "inherit",
        "profile": "test",
        "hash": "abc123",
    }
    assert nodes[0]["execution"]["ray_task_id"]
    assert nodes[0]["execution"]["ray_node_id"]


def test_durable_task_context_is_scoped() -> None:
    assert get_current_task_execution_pk() is None
    with durable_task_execution(42):
        assert get_current_task_execution_pk() == 42
    assert get_current_task_execution_pk() is None


def test_progress_actor_builds_node_snapshot() -> None:
    progress = WorkflowProgressActor()
    progress.register(
        "0.0",
        "prepare",
        "tests.unit.test_workflows.increment",
        [],
        {"mode": "inherit", "hash": "abc"},
        {"num_cpus": 1},
    )
    progress.started("0.0", "prepare", {"ray_task_id": "ray-task-1"})
    progress.completed("0.0", "prepare")
    progress.register(
        "0.1.m0",
        "leaf",
        "tests.unit.test_workflows.multiply",
        ["0.0"],
    )
    progress.started("0.1.m0", "leaf")
    progress.submitted("0.1.m0", "leaf", "ray-task-2")
    progress.progress("0.1.m0", 2, 4, "half way", {"rows": 10})

    snapshot = progress.snapshot()
    unchanged = progress.snapshot()

    assert snapshot["schema_version"] == 1
    assert snapshot["state"] == "RUNNING"
    assert snapshot["total_nodes"] == 2
    assert snapshot["completed_nodes"] == 1
    assert snapshot["running_nodes"] == 1
    assert snapshot["progress_percent"] == 50.0
    assert snapshot["graph"]["edges"] == [{"source": "0.0", "target": "0.1.m0"}]
    assert snapshot["graph"]["nodes"][0]["execution"]["ray_task_id"] == "ray-task-1"
    assert snapshot["graph"]["nodes"][1]["label"] == "leaf"
    assert snapshot["graph"]["nodes"][1]["progress"]["percent"] == 50.0
    assert snapshot["revision"] == unchanged["revision"]
    assert snapshot["updated_at"] == unchanged["updated_at"]


def test_report_progress_uses_current_workflow_context() -> None:
    calls: list[tuple] = []

    class _RemoteMethod:
        def remote(self, *args):
            calls.append(args)

    actor = type("_Actor", (), {"progress": _RemoteMethod()})()

    assert report_progress(1, 2) is False
    with workflow_step_execution(actor, "0.1"):
        assert report_progress(1, 2, message="half", metrics={"rows": 5}) is True

    assert calls == [("0.1", 1.0, 2.0, "half", {"rows": 5})]


def test_report_progress_validates_values_and_metrics() -> None:
    class _RemoteMethod:
        def remote(self, *args):
            del args

    actor = type("_Actor", (), {"progress": _RemoteMethod()})()

    with workflow_step_execution(actor, "0.1"):
        with pytest.raises(ValueError, match="total must be greater than zero"):
            report_progress(0, 0)
        with pytest.raises(ValueError, match="current must be between zero and total"):
            report_progress(-1, 1)
        with pytest.raises(ValueError, match="progress metrics must be JSON-serializable"):
            report_progress(1, 2, metrics={"bad": object()})


def test_map_accepts_existing_signature() -> None:
    signature = step(increment)

    mapped = map_step(signature)

    assert mapped.signature is signature


def test_get_executor_uses_local_executor_when_ray_is_unavailable(monkeypatch) -> None:
    import builtins

    original_import = builtins.__import__

    def fail_ray_import(name, *args, **kwargs):
        if name == "ray":
            raise ImportError("ray unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_ray_import)

    assert isinstance(_get_executor(None), _LocalExecutor)
