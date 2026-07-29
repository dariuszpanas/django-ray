"""Unit tests for Ray-native workflow signatures."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    WorkflowRunIdentity,
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
    _json_safe,
    _LocalExecutor,
    _RayExecutor,
    _workflow_progress_policy,
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


def report_and_increment(value: int) -> tuple[bool, int]:
    return report_progress(1, 2), value + 1


def echo_workflow_progress_policy(*, workflow_progress_policy: str) -> str:
    return workflow_progress_policy


def sum_values(values: list[int]) -> int:
    return sum(values)


def identity(value: Any) -> Any:
    return value


def return_bound_payload(*, payload: Any) -> Any:
    return payload


def fail_on_two(value: int) -> int:
    if value == 2:
        raise RuntimeError("original map failure")
    return value


def cleanup_failure(value: Any) -> Any:
    raise RuntimeError("original cleanup failure")


def cleanup_submission_failure(value: Any) -> Any:
    raise RuntimeError("original submission failure")


def echo_limits(value: int, *, max_concurrency: int, max_items: int) -> tuple[int, int, int]:
    return value, max_concurrency, max_items


def track_barrier_value(value: int, tracker: Any) -> int:
    import ray

    ray.get(tracker.enter.remote())
    try:
        deadline = time.monotonic() + 15.0
        while not ray.get(tracker.reached.remote(2)):
            if time.monotonic() >= deadline:
                raise TimeoutError("two mapped leaves did not reach the admission barrier")
            time.sleep(0.01)
    finally:
        ray.get(tracker.exit.remote())
    return value


class _ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def exit(self) -> None:
        self.active -= 1

    def reached(self, minimum: int) -> bool:
        return self.peak >= minimum

    def snapshot(self) -> tuple[int, int]:
        return self.active, self.peak


def fail_group_branch(value: int, tracker: Any) -> int:
    import ray

    ray.get(tracker.record.remote("failing_started"))
    deadline = time.monotonic() + 15.0
    while not ray.get(tracker.contains.remote("release_failure")):
        if time.monotonic() >= deadline:
            raise TimeoutError("group failure release was not recorded")
        time.sleep(0.01)
    raise RuntimeError("original mapped group failure")


def finish_or_cancel_group_branch(value: int, tracker: Any) -> int:
    import ray

    ray.get(tracker.record.remote("sibling_started"))
    try:
        deadline = time.monotonic() + 15.0
        while not ray.get(tracker.contains.remote("release_failure")):
            if time.monotonic() >= deadline:
                raise TimeoutError("group failure release was not recorded")
            time.sleep(0.01)
        time.sleep(0.75)
    except BaseException:
        ray.get(tracker.record.remote("sibling_cancelled"))
        raise
    ray.get(tracker.record.remote("sibling_completed"))
    return value


def record_chain_upstream(value: int, tracker: Any) -> int:
    import ray

    time.sleep(0.05)
    ray.get(tracker.record.remote("upstream_completed"))
    return value


def fail_chain_terminal(value: int, tracker: Any) -> int:
    import ray

    ray.get(tracker.record.remote("terminal_started"))
    raise RuntimeError("original mapped chain failure")


class _LifecycleTracker:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        self.events.append(event)

    def contains(self, event: str) -> bool:
        return event in self.events

    def snapshot(self) -> list[str]:
        return list(self.events)


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
        self.remote_invocations: list[tuple[str, int]] = []
        self.wait_sizes: list[int] = []
        self.cancelled: list[_Ref] = []
        self.put_calls = 0

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
                    fake.remote_invocations.append((fn.__name__, len(args) + len(kw)))
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

    def put(self, value: Any) -> _Ref:
        self.put_calls += 1
        return _Ref(value)

    def wait(
        self,
        refs: list[_Ref],
        *,
        num_returns: int = 1,
        timeout: float | None = None,
    ) -> tuple[list[_Ref], list[_Ref]]:
        del timeout
        self.wait_sizes.append(len(refs))
        return refs[:num_returns], refs[num_returns:]

    def cancel(
        self,
        ref: _Ref,
        *,
        force: bool,
        recursive: bool,
    ) -> None:
        assert force is False
        assert recursive is True
        self.cancelled.append(ref)


class _PendingValue:
    def __init__(self, value: Any = None, error: BaseException | None = None) -> None:
        self.value = value
        self.error = error


@dataclass
class _InstrumentedWindowExecutor(_Executor):
    """Deterministic future executor used to prove the map admission invariant."""

    live: set[_PendingValue] = field(default_factory=set)
    peak_live: int = 0
    submissions: int = 0
    cancelled: list[_PendingValue] = field(default_factory=list)
    wait_sizes: list[int] = field(default_factory=list)

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        del node_id, dependencies
        callable_obj = __import__(
            signature.callable_path.rsplit(".", 1)[0],
            fromlist=[signature.callable_path.rsplit(".", 1)[1]],
        )
        function = getattr(callable_obj, signature.callable_path.rsplit(".", 1)[1])
        kwargs = {**input_kwargs, **signature.bound_kwargs}
        try:
            value = function(*input_args, *signature.bound_args, **kwargs)
            ref = _PendingValue(value=value)
        except BaseException as error:
            ref = _PendingValue(error=error)
        self.live.add(ref)
        self.submissions += 1
        self.peak_live = max(self.peak_live, len(self.live))
        return ref

    def collect(self, values):
        raise AssertionError("bounded map must not use an all-results collector")

    def resolve(self, value):
        if not isinstance(value, _PendingValue):
            return value
        self.live.discard(value)
        if value.error is not None:
            raise value.error
        return value.value

    def wait_one(self, values):
        self.wait_sizes.append(len(values))
        # Completing the newest item first exercises ordered reassembly under skew.
        return len(values) - 1

    def cancel_and_drain(self, values, *, timeout_seconds):
        assert timeout_seconds >= 0
        self.cancelled.extend(values)
        self.live.difference_update(values)


class _CleanupRef:
    def __init__(self, label: str, error: BaseException | None = None) -> None:
        self.label = label
        self.error = error


@dataclass
class _CleanupTrackingExecutor(_Executor):
    """Model dependency refs separately from a logical item's terminal ref."""

    cleaned: list[_CleanupRef] = field(default_factory=list)

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        del input_args, input_kwargs, dependencies
        if signature.callable_path.endswith(".cleanup_submission_failure"):
            raise RuntimeError("original submission failure")
        error = (
            RuntimeError("original cleanup failure")
            if signature.callable_path.endswith(".cleanup_failure")
            else None
        )
        return _CleanupRef(node_id, error)

    def collect(self, values):
        error = next((value.error for value in values if value.error is not None), None)
        return _CleanupRef("collector", error)

    def resolve(self, value):
        if not isinstance(value, _CleanupRef):
            return value
        if value.error is not None:
            raise value.error
        return value

    def wait_one(self, values):
        return 0

    def cancel_and_drain(self, values, *, timeout_seconds):
        assert timeout_seconds == 0.25
        self.cleaned.extend(values)


def test_local_chain_and_dynamic_map() -> None:
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=3),
        step(sum_values),
    )

    assert workflow.run(4, use_ray=False) == 18


def test_map_step_preserves_leaf_kwargs_named_like_limits() -> None:
    mapped = map_step(echo_limits, max_concurrency=7, max_items=9).with_limits(
        max_concurrency=2,
        max_items=10,
    )

    assert mapped.run([1, 2], use_ray=False) == [(1, 7, 9), (2, 7, 9)]
    assert mapped.max_concurrency == 2
    assert mapped.max_items == 10


@pytest.mark.parametrize("size", [1_000, 10_000, 50_000])
def test_bounded_map_keeps_peak_pending_refs_within_window(monkeypatch, size) -> None:
    executor = _InstrumentedWindowExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    yielded = 0

    def items():
        nonlocal yielded
        for value in range(size):
            yielded += 1
            yield value

    result = (
        map_step(identity)
        .with_limits(
            max_concurrency=17,
            max_items=size,
        )
        .run(items())
    )

    assert len(result) == size
    assert result[:2] == [0, 1]
    assert result[-2:] == [size - 2, size - 1]
    assert yielded == size
    assert executor.submissions == size
    assert executor.peak_live == 17
    assert max(executor.wait_sizes) == 17
    assert executor.live == set()


def test_bounded_map_handles_empty_and_nested_inputs() -> None:
    bounded_leaf = map_step(increment).with_limits(max_concurrency=2, max_items=10)
    nested_map = map_step(
        map_step(increment).with_limits(max_concurrency=1, max_items=10)
    ).with_limits(max_concurrency=2, max_items=10)
    nested_group = map_step(
        group(
            step(multiply, factor=2),
            step(multiply, factor=3),
        )
    ).with_limits(max_concurrency=2, max_items=10)

    assert bounded_leaf.run([], use_ray=False) == []
    assert map_step(increment).with_limits(max_items=3).run([1, 2, 3], use_ray=False) == [2, 3, 4]
    assert nested_map.run([[1, 2], [3]], use_ray=False) == [[2, 3], [4]]
    assert nested_group.run([1, 2], use_ray=False) == [[2, 3], [4, 6]]


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    [
        ({"max_concurrency": True}, TypeError, "max_concurrency"),
        ({"max_concurrency": 1.5}, TypeError, "max_concurrency"),
        ({"max_concurrency": 0}, ValueError, "max_concurrency"),
        ({"max_concurrency": 1, "max_items": True}, TypeError, "max_items"),
        ({"max_concurrency": 1, "max_items": 0}, ValueError, "max_items"),
        (
            {"max_concurrency": 1, "cancel_timeout_seconds": float("inf")},
            ValueError,
            "cancel_timeout_seconds",
        ),
        (
            {"max_concurrency": 1, "cancel_timeout_seconds": True},
            TypeError,
            "cancel_timeout_seconds",
        ),
        (
            {"max_concurrency": 1, "cancel_timeout_seconds": -1},
            ValueError,
            "cancel_timeout_seconds",
        ),
    ],
)
def test_map_limits_are_validated(kwargs, error_type, message) -> None:
    with pytest.raises(error_type, match=message):
        map_step(identity).with_limits(**kwargs)

    with pytest.raises(ValueError, match="requires max_concurrency or max_items"):
        map_step(identity).with_limits()


def test_bounded_map_rejects_sized_input_before_submission(monkeypatch) -> None:
    executor = _InstrumentedWindowExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)

    with pytest.raises(WorkflowDefinitionError, match="exceeds max_items=2"):
        map_step(identity).with_limits(max_concurrency=2, max_items=2).run([1, 2, 3])

    assert executor.submissions == 0


def test_bounded_map_stops_generator_admission_and_preserves_failure(monkeypatch) -> None:
    executor = _InstrumentedWindowExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    yielded: list[int] = []
    closed = False

    def items():
        nonlocal closed
        try:
            for value in range(100):
                yielded.append(value)
                yield value
        finally:
            closed = True

    with pytest.raises(RuntimeError, match="original map failure"):
        map_step(fail_on_two).with_limits(max_concurrency=2).run(items())

    assert yielded == [0, 1, 2]
    assert executor.submissions == 3
    assert executor.peak_live == 2
    # The failed ref and the still-pending ref are both explicitly cleaned up.
    assert len(executor.cancelled) == 2
    assert executor.live == set()
    assert closed is True


@pytest.mark.parametrize(
    ("signature", "expected_cleanup_labels"),
    [
        (
            group(step(cleanup_failure), step(identity)),
            ["collector", "0.m0.g1", "0.m0.g0"],
        ),
        (
            chain(step(identity), step(cleanup_failure)),
            ["0.m0.1", "0.m0.0"],
        ),
    ],
    ids=["group", "chain"],
)
def test_bounded_map_cleans_every_nested_physical_ref_on_failure(
    monkeypatch,
    signature,
    expected_cleanup_labels,
) -> None:
    executor = _CleanupTrackingExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)

    with pytest.raises(RuntimeError, match="original cleanup failure"):
        map_step(signature).with_limits(
            max_concurrency=1,
            max_items=1,
            cancel_timeout_seconds=0.25,
        ).run([1])

    assert [ref.label for ref in executor.cleaned] == expected_cleanup_labels


def test_bounded_map_cleans_partial_group_submission(monkeypatch) -> None:
    executor = _CleanupTrackingExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    signature = group(step(identity), step(cleanup_submission_failure))

    with pytest.raises(RuntimeError, match="original submission failure"):
        map_step(signature).with_limits(
            max_concurrency=1,
            max_items=1,
            cancel_timeout_seconds=0.25,
        ).run([1])

    assert [ref.label for ref in executor.cleaned] == ["0.m0.g0"]


def test_legacy_eager_map_submission_retains_only_its_collector() -> None:
    executor = _CleanupTrackingExecutor()

    submission = map_step(identity)._submit(
        executor,
        (range(1_000),),
        {},
        "0",
        (),
    )

    assert submission.value.label == "collector"
    assert set(vars(submission)) == {"value", "terminal_node_ids"}
    assert getattr(executor, "_cleanup_capture_stack", []) == []


def test_bounded_generator_enforces_expansion_limit(monkeypatch) -> None:
    executor = _InstrumentedWindowExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    yielded: list[int] = []

    def items():
        for value in range(10):
            yielded.append(value)
            yield value

    with pytest.raises(WorkflowDefinitionError, match="exceeds max_items=3"):
        map_step(identity).with_limits(max_concurrency=2, max_items=3).run(items())

    assert yielded == [0, 1, 2, 3]
    assert executor.submissions == 3
    assert executor.peak_live == 2
    assert executor.live == set()


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


def test_bound_keyword_values_keep_nested_application_types(monkeypatch) -> None:
    payload = {"items": [1, 2], "coordinates": (3, 4)}
    signature = step(return_bound_payload, payload=payload)

    local_result = signature.run(use_ray=False)
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    ray_result = signature.run(use_ray=True)

    for result in (local_result, ray_result):
        assert isinstance(result, dict)
        assert isinstance(result["items"], list)
        assert isinstance(result["coordinates"], tuple)
        assert result == payload


def test_progress_metadata_thaws_frozen_option_mappings() -> None:
    signature = step(
        increment,
        ray_options={"resources": {"database": 1}, "num_cpus": 0.5},
    )

    assert _json_safe(signature.ray_options) == {
        "resources": {"database": 1},
        "num_cpus": 0.5,
    }


def test_ray_bounded_map_uses_sliding_wait_without_collector(monkeypatch) -> None:
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=2).with_limits(
            max_concurrency=3,
            max_items=10,
        ),
        step(sum_values),
    )

    assert workflow.run(8) == 56
    assert max(fake_ray.wait_sizes) == 3
    assert fake_ray.put_calls == 1
    # make_range + eight map leaves + sum_values; no all-results collector.
    assert fake_ray.submissions == 10


def test_ray_bounded_map_keeps_nested_group_collectors_fixed_width(monkeypatch) -> None:
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    workflow = chain(
        step(make_range),
        map_step(
            group(
                step(multiply, factor=2),
                step(multiply, factor=3),
            )
        ).with_limits(max_concurrency=4, max_items=1_000),
    )

    result = workflow.run(100)
    collector_widths = [
        argument_count
        for name, argument_count in fake_ray.remote_invocations
        if name == "collect_workflow_results_remote"
    ]

    assert result[0] == [0, 0]
    assert result[-1] == [198, 297]
    assert max(fake_ray.wait_sizes) == 4
    assert collector_widths == [2] * 100


def test_ray_bounded_map_retains_only_terminal_refs_for_nested_chains(monkeypatch) -> None:
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    workflow = chain(
        step(make_range),
        map_step(
            chain(
                step(increment),
                step(multiply, factor=2),
            )
        ).with_limits(max_concurrency=3, max_items=100),
    )

    assert workflow.run(20) == [value * 2 for value in range(1, 21)]
    assert max(fake_ray.wait_sizes) == 3
    assert all(name != "collect_workflow_results_remote" for name, _ in fake_ray.remote_invocations)


def test_ray_bounded_map_cancels_pending_refs_without_hiding_failure(monkeypatch) -> None:
    fake_ray = _FakeRay()
    monkeypatch.setitem(sys.modules, "ray", fake_ray)
    workflow = chain(
        step(make_range),
        map_step(fail_on_two).with_limits(max_concurrency=2, max_items=10),
    )

    with pytest.raises(RuntimeError, match="original map failure"):
        workflow.run(10)

    assert fake_ray.submissions == 4
    assert len(fake_ray.cancelled) == 1


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
    assert isinstance(updated.runtime_env, Mapping)
    with pytest.raises(TypeError):
        updated.runtime_env["env_vars"]["MODE"] = "changed"
    with pytest.raises(TypeError):
        updated.ray_options["num_cpus"] = 2

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


def test_invalid_workflow_progress_policy_fails_before_plan_materialization(
    monkeypatch,
) -> None:
    materialized = False

    def record_materialization(*args, **kwargs):
        del args, kwargs
        nonlocal materialized
        materialized = True
        raise AssertionError("policy validation must run first")

    monkeypatch.setattr(
        "django_ray.workflow_plans.materialize_workflow_plan",
        record_materialization,
    )

    with pytest.raises(WorkflowDefinitionError, match="full, disabled|disabled, full"):
        step(increment).with_progress_reporting("sampled")

    assert materialized is False


def test_progress_policy_named_application_kwarg_is_not_reserved() -> None:
    assert (
        step(echo_workflow_progress_policy).run(
            use_ray=False,
            workflow_progress_policy="business-value",
        )
        == "business-value"
    )


def test_workflow_progress_policy_uses_configured_default(settings) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_REPORTING_POLICY": "disabled",
    }

    assert _workflow_progress_policy(None) == "disabled"
    assert _workflow_progress_policy("full") == "full"


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
    executor.workflow_run_identity = None

    assert executor._flush_progress() is None

    snapshot_ref = object()
    executor.progress_actor = SimpleNamespace(snapshot=SimpleNamespace(remote=lambda: snapshot_ref))
    executor.workflow_run_identity = object()
    executor.ray = SimpleNamespace(wait=lambda refs, timeout: ([], refs))
    assert executor._flush_progress(bypass_interval=True) is None

    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: (_ for _ in ()).throw(RuntimeError("actor died")),
    )
    assert executor._flush_progress(bypass_interval=True) is None
    assert executor.progress_actor is None


def test_ray_executor_throttles_high_cardinality_progress_snapshots(
    monkeypatch,
    settings,
) -> None:
    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=1,
        run_id="progress-throttle-run",
    )
    persisted: list[dict[str, Any]] = []

    def persist(reported_identity, snapshot):
        assert reported_identity == identity
        persisted.append(snapshot)
        return True

    monkeypatch.setattr(
        "django_ray.workflow_progress.persist_workflow_progress",
        persist,
    )
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "WORKFLOW_PROGRESS_FLUSH_SECONDS": 1,
    }
    now = [100.0]
    monkeypatch.setattr("django_ray.workflows.time.monotonic", lambda: now[0])
    snapshot_ref = object()
    work_ref = object()
    snapshot_calls = 0

    class _SnapshotMethod:
        def remote(self):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot_ref

    class _ProgressRay:
        def __init__(self) -> None:
            self.work_wait_calls = 0

        def wait(self, refs, **kwargs):
            del kwargs
            if refs == [work_ref]:
                self.work_wait_calls += 1
            return refs, []

        def get(self, ref):
            if ref is snapshot_ref:
                return {
                    "schema_version": 2,
                    "run_identity": identity.as_dict(),
                    "revision": 1,
                    "state": "RUNNING",
                    "completed_nodes": 0,
                    "failed_nodes": 0,
                    "total_nodes": 1,
                }
            assert ref is work_ref
            return "ready"

    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=_SnapshotMethod())
    executor.workflow_run_identity = identity
    executor.last_progress_revision = 0
    executor.last_progress_flush_at = now[0]
    executor.ray = _ProgressRay()

    for index in range(50_000):
        if index == 25_000:
            now[0] += 1.0
        assert executor.wait_one([work_ref]) == 0
        assert executor.resolve_ready(work_ref) == "ready"

    assert snapshot_calls == 1
    assert executor.ray.work_wait_calls == 50_000

    executor._flush_progress(bypass_interval=True)

    assert snapshot_calls == 2
    assert len(persisted) == 1


def test_finish_progress_polls_without_rewriting_unchanged_snapshot(monkeypatch) -> None:
    identity = object()
    persisted: list[dict[str, Any]] = []
    snapshot_ref = object()
    snapshot_calls = 0

    def persist(reported_identity, snapshot):
        assert reported_identity is identity
        persisted.append(snapshot)
        return True

    monkeypatch.setattr(
        "django_ray.workflow_progress.persist_workflow_progress",
        persist,
    )

    class _SnapshotMethod:
        def remote(self):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot_ref

    snapshot = {
        "revision": 3,
        "completed_nodes": 0,
        "failed_nodes": 0,
        "total_nodes": 1,
    }
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=_SnapshotMethod())
    executor.workflow_run_identity = identity
    executor.last_progress_revision = -1
    executor.last_progress_flush_at = 0.0
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: dict(snapshot),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("django_ray.workflows.time.sleep", sleeps.append)

    executor.finish_progress()

    assert snapshot_calls == 10
    assert len(persisted) == 1
    assert sleeps == [0.05] * 10


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
    executor.workflow_run_identity = None
    executor.progress_actor = SimpleNamespace(
        register=_RemoteMethod(),
        submitted=_RemoteMethod(),
    )
    executor.remote_step = _RemoteStep()

    executor.submit_step(step(increment), (), {}, "0.0", ())


def test_ray_map_cleanup_has_a_hard_deadline() -> None:
    class _SlowRay:
        def __init__(self) -> None:
            self.cancelled: list[object] = []
            self.wait_timeouts: list[float] = []
            self.get_calls = 0

        def cancel(self, ref, *, force, recursive):
            assert force is False
            assert recursive is True
            self.cancelled.append(ref)

        def wait(self, refs, *, num_returns, timeout):
            assert num_returns == len(refs)
            self.wait_timeouts.append(timeout)
            time.sleep(timeout)
            return [], refs

        def get(self, ref):
            del ref
            self.get_calls += 1

    ray = _SlowRay()
    executor = object.__new__(_RayExecutor)
    executor.ray = ray
    refs = [object(), object()]
    started_at = time.monotonic()

    executor.cancel_and_drain(refs, timeout_seconds=0.02)

    elapsed = time.monotonic() - started_at
    assert ray.cancelled == refs
    assert ray.wait_timeouts == [0.02]
    assert ray.get_calls == 0
    assert elapsed < 0.1


@pytest.mark.django_db
def test_ray_executor_flushes_failed_progress_snapshot() -> None:
    from django_ray.models import RayTaskExecution
    from django_ray.workflow_progress import claim_workflow_run

    execution = RayTaskExecution.objects.create(
        task_id="workflow-flush",
        callable_path="tests.unit.test_workflows.increment",
        state="RUNNING",
        attempt_number=2,
        execution_generation=4,
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=2,
        execution_generation=4,
        run_id="00000000-0000-0000-0000-000000000008",
    )
    assert claim_workflow_run(identity) is True
    snapshot_ref = object()
    snapshot = {
        "schema_version": WORKFLOW_PROGRESS_SCHEMA_VERSION,
        "run_identity": identity.as_dict(),
        "revision": 2,
        "state": "RUNNING",
        "completed_nodes": 0,
        "failed_nodes": 1,
        "total_nodes": 1,
    }
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = SimpleNamespace(snapshot=SimpleNamespace(remote=lambda: snapshot_ref))
    executor.task_execution_pk = execution.pk
    executor.workflow_run_identity = identity
    executor.last_progress_revision = 2
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: snapshot,
    )

    flushed = executor._flush_progress(failed=True)
    assert flushed is not None
    assert flushed["state"] == "FAILED"

    execution.refresh_from_db()
    assert json.loads(execution.progress_data)["state"] == "FAILED"


@pytest.mark.django_db
def test_ray_executor_disables_reporter_after_stale_write() -> None:
    from django_ray.models import RayTaskExecution, TaskState
    from django_ray.workflow_progress import claim_workflow_run

    execution = RayTaskExecution.objects.create(
        task_id="workflow-stale-flush",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=2,
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=1,
        execution_generation=2,
        run_id="00000000-0000-0000-0000-000000000009",
    )
    assert claim_workflow_run(identity) is True
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.CANCELLED)

    snapshot = {
        "schema_version": WORKFLOW_PROGRESS_SCHEMA_VERSION,
        "run_identity": identity.as_dict(),
        "revision": 1,
        "state": "RUNNING",
        "completed_nodes": 0,
        "failed_nodes": 0,
        "total_nodes": 1,
    }
    disabled: list[bool] = []
    snapshot_ref = object()
    actor = SimpleNamespace(
        snapshot=SimpleNamespace(remote=lambda: snapshot_ref),
        disable=SimpleNamespace(remote=lambda: disabled.append(True)),
    )
    executor = object.__new__(_RayExecutor)
    executor.progress_actor = actor
    executor.task_execution_pk = execution.pk
    executor.workflow_run_identity = identity
    executor.last_progress_revision = -1
    executor.ray = SimpleNamespace(
        wait=lambda refs, timeout: (refs, []),
        get=lambda ref: snapshot,
    )

    assert executor._flush_progress() is None
    assert executor.progress_actor is None
    assert disabled == [True]


@pytest.mark.real_ray
def test_workflow_executes_on_real_ray() -> None:
    import ray

    ray.init(ignore_reinit_error=True)
    try:
        outer_task = ray.remote(run_nested_workflow)
        assert ray.get(outer_task.remote(5)) == 40
    finally:
        ray.shutdown()


@pytest.mark.django_db
@pytest.mark.real_ray
def test_real_ray_workflow_can_disable_node_reporting() -> None:
    import ray

    from django_ray.models import RayTaskExecution, TaskState

    execution = RayTaskExecution.objects.create(
        task_id="real-ray-disabled-progress",
        callable_path=f"{__name__}.report_and_increment",
        state=TaskState.RUNNING,
        execution_generation=1,
    )
    ray.init(ignore_reinit_error=True)
    try:
        with durable_task_execution(
            execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
        ):
            result = (
                step(report_and_increment).with_progress_reporting("disabled").run(5, use_ray=True)
            )
    finally:
        ray.shutdown()

    execution.refresh_from_db()
    selection = json.loads(execution.workflow_plan_selection)
    assert result == (False, 6)
    assert selection["reporting_policy"] == "disabled"
    assert execution.progress_data is None


@pytest.mark.real_ray
def test_real_ray_bounded_map_preserves_order_and_limits_concurrency() -> None:
    import ray

    ray.init(ignore_reinit_error=True, num_cpus=4)
    try:
        tracker = ray.remote(num_cpus=0)(_ConcurrencyTracker).remote()
        workflow = chain(
            step(identity),
            map_step(
                track_barrier_value,
                tracker,
                ray_options={"num_cpus": 0},
            ).with_limits(max_concurrency=2, max_items=10),
        )
        items = [0, 1, 2, 3]

        assert workflow.run(items, use_ray=True) == [0, 1, 2, 3]
        assert ray.get(tracker.snapshot.remote()) == (0, 2)
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_mapped_group_cleans_sibling_ref_before_failure_returns() -> None:
    import ray
    from ray.exceptions import RayTaskError

    ray.init(ignore_reinit_error=True, num_cpus=4)
    try:
        tracker = ray.remote(num_cpus=0)(_LifecycleTracker).remote()
        cancel_timeout_seconds = 1.5
        workflow = chain(
            step(identity),
            map_step(
                group(
                    step(
                        fail_group_branch,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                    step(
                        finish_or_cancel_group_branch,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                )
            ).with_limits(
                max_concurrency=1,
                max_items=1,
                cancel_timeout_seconds=cancel_timeout_seconds,
            ),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            workflow_result = pool.submit(workflow.run, [1], use_ray=True)
            required_events = {"failing_started", "sibling_started"}
            deadline = time.monotonic() + 15.0
            while True:
                events = ray.get(tracker.snapshot.remote())
                if required_events <= set(events):
                    break
                if workflow_result.done():
                    unexpected_result = workflow_result.result()
                    pytest.fail(f"mapped group unexpectedly returned {unexpected_result!r}")
                if time.monotonic() >= deadline:
                    pytest.fail("mapped group branches did not reach the start barrier")
                time.sleep(0.01)

            started_at = time.monotonic()
            ray.get(tracker.record.remote("release_failure"))
            with pytest.raises(RayTaskError, match="original mapped group failure"):
                workflow_result.result(timeout=5.0)
            elapsed = time.monotonic() - started_at

        events = ray.get(tracker.snapshot.remote())
        assert "failing_started" in events
        assert "sibling_started" in events
        assert {"sibling_cancelled", "sibling_completed"} & set(events)
        assert elapsed < cancel_timeout_seconds + 1.0
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_mapped_chain_drains_upstream_ref_and_preserves_failure() -> None:
    import ray
    from ray.exceptions import RayTaskError

    ray.init(ignore_reinit_error=True, num_cpus=4)
    try:
        tracker = ray.remote(num_cpus=0)(_LifecycleTracker).remote()
        workflow = chain(
            step(identity),
            map_step(
                chain(
                    step(
                        record_chain_upstream,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                    step(
                        fail_chain_terminal,
                        tracker,
                        ray_options={"num_cpus": 0},
                    ),
                )
            ).with_limits(
                max_concurrency=1,
                max_items=1,
                cancel_timeout_seconds=1.0,
            ),
        )

        with pytest.raises(RayTaskError, match="original mapped chain failure"):
            workflow.run([1], use_ray=True)

        events = ray.get(tracker.snapshot.remote())
        assert events == ["upstream_completed", "terminal_started"]
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
        state="RUNNING",
        execution_generation=1,
    )
    workflow = chain(
        step(increment),
        step(multiply, factor=2),
    )

    ray.init(ignore_reinit_error=True)
    try:
        with durable_task_execution(
            execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
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
    assert progress["schema_version"] == WORKFLOW_PROGRESS_SCHEMA_VERSION
    assert execution.workflow_progress_summary_json is None
    assert progress["run_identity"]["attempt_number"] == 1
    assert progress["run_identity"]["execution_generation"] == 1
    assert progress["graph"]["edges"] == [{"source": "0.0", "target": "0.1"}]
    assert nodes[0]["runtime_env"]["mode"] == "inherit"
    assert nodes[0]["runtime_env"]["profile"] == "test"
    assert nodes[0]["runtime_env"]["hash"].startswith("sha256:")
    assert nodes[0]["execution"]["ray_task_id"]
    assert nodes[0]["execution"]["ray_node_id"]


@pytest.mark.real_ray
@pytest.mark.django_db
def test_real_ray_bounded_map_persists_one_aggregate_node() -> None:
    import ray

    from django_ray.models import RayTaskExecution

    execution = RayTaskExecution.objects.create(
        task_id="real-ray-bounded-map-graph",
        callable_path="tests.unit.test_workflows.run_nested_workflow",
        state="RUNNING",
        execution_generation=1,
    )
    workflow = chain(
        step(make_range),
        map_step(increment).with_limits(max_concurrency=2, max_items=10),
    )

    ray.init(ignore_reinit_error=True)
    try:
        with durable_task_execution(
            execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
        ):
            assert workflow.run(6, use_ray=True) == [1, 2, 3, 4, 5, 6]
    finally:
        ray.shutdown()

    execution.refresh_from_db()
    progress = json.loads(execution.progress_data)
    nodes = progress["graph"]["nodes"]
    map_node = next(node for node in nodes if node["kind"] == "map")

    assert progress["state"] == "SUCCEEDED"
    assert progress["schema_version"] == WORKFLOW_PROGRESS_SCHEMA_VERSION
    assert progress["run_identity"]["attempt_number"] == 1
    assert progress["run_identity"]["execution_generation"] == 1
    assert progress["total_nodes"] == 2
    assert map_node["node_id"] == "0.1"
    assert map_node["fanout"]["max_concurrency"] == 2
    assert map_node["fanout"]["submitted_items"] == 6
    assert map_node["fanout"]["completed_items"] == 6
    assert map_node["fanout"]["in_flight_items"] == 0


def test_durable_task_context_is_scoped() -> None:
    assert get_current_task_execution_pk() is None
    with durable_task_execution(42):
        assert get_current_task_execution_pk() == 42
    assert get_current_task_execution_pk() is None


def test_progress_actor_builds_node_snapshot() -> None:
    progress = WorkflowProgressActor(
        task_execution_pk=42,
        attempt_number=2,
        execution_generation=5,
        workflow_run_id="00000000-0000-0000-0000-000000000010",
        plan_summary={
            "fingerprint": "sha256:plan",
            "definition_name": "workflow:prepare",
        },
    )
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

    assert snapshot["schema_version"] == WORKFLOW_PROGRESS_SCHEMA_VERSION
    assert snapshot["run_identity"] == {
        "schema_version": 1,
        "run_id": "00000000-0000-0000-0000-000000000010",
        "task_execution_pk": 42,
        "attempt_number": 2,
        "execution_generation": 5,
    }
    assert snapshot["state"] == "RUNNING"
    assert snapshot["plan"] == {
        "fingerprint": "sha256:plan",
        "definition_name": "workflow:prepare",
    }
    assert "selection" not in snapshot["plan"]
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


def test_progress_actor_aggregates_bounded_map_items() -> None:
    progress = WorkflowProgressActor()
    progress.register_map("0.1", "map:increment", ["0.0"], 4, 50_000)
    progress.map_progress("0.1", "map:increment", 50_000, 49_999, True)
    progress.completed("0.1", "map:increment")

    snapshot = progress.snapshot()
    node = snapshot["graph"]["nodes"][0]

    assert snapshot["total_nodes"] == 1
    assert snapshot["graph"]["edges"] == [{"source": "0.0", "target": "0.1"}]
    assert node["kind"] == "map"
    assert node["state"] == "SUCCEEDED"
    assert node["fanout"] == {
        "max_concurrency": 4,
        "max_items": 50_000,
        "submitted_items": 50_000,
        "completed_items": 50_000,
        "in_flight_items": 0,
        "input_exhausted": True,
    }
    assert node["progress"]["percent"] == 100.0


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
