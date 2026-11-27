"""Unit tests for Ray-native workflow signatures, limits, and local execution."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    durable_task_execution,
    get_current_task_execution_pk,
    workflow_step_execution,
)
from django_ray.runtime.remote import WorkflowProgressActor
from django_ray.workflow.progress.protocol import (
    WorkflowProgressEventKind,
    prepare_workflow_progress_event,
)
from django_ray.workflows import (
    WorkflowDefinitionError,
    _callable_path,
    _get_executor,
    _json_safe,
    _LocalExecutor,
    _RayExecutor,
    chain,
    group,
    map_step,
    report_progress,
    step,
)
from tests.unit._workflows_helpers import (
    _CleanupTrackingExecutor,
    _decoded_ingests,
    _FakeRay,
    _GraphExecutor,
    _ingest_event,
    _IngestOnlyProgressActor,
    _InstrumentedWindowExecutor,
    _workflow_identity,
    _workflow_plan_summary,
)


def make_range(limit: int) -> list[int]:
    return list(range(limit))


def multiply(value: int, factor: int = 1) -> int:
    return value * factor


def increment(value: int) -> int:
    return value + 1


OUTPUT_PREVIEW_CALLS: list[int] = []


def preview_increment(value: int) -> dict[str, int]:
    OUTPUT_PREVIEW_CALLS.append(value)
    return {"value": value}


def report_and_increment(value: int) -> tuple[bool, int]:
    return report_progress(1, 2), value + 1


def report_then_make_range(limit: int) -> list[int]:
    reported = report_progress(
        1,
        2,
        message="Preparing bounded fan-out",
        metrics={"items": limit},
    )
    if not reported:
        raise RuntimeError("workflow progress actor was not available")
    return list(range(limit))


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


def fail_with_nested_request_rejection(value: int) -> int:
    from ray.exceptions import RayTaskError

    from django_ray.execution_codec import (
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
    )

    cause = NestedExecutionRequestRejected(NestedExecutionRequestRejection.RUNTIME_ENV_MISMATCH)
    raise RayTaskError(
        "nested_map_leaf",
        "secret-bearing mapped traceback",
        cause,
        proctitle="ray::nested_map_leaf",
        pid=123,
        ip="127.0.0.1",
    )


def fail_workflow_step(value: int) -> int:
    del value
    raise RuntimeError("intentional workflow failure")


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


def run_nested_workflow(limit: int) -> int:
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=4),
        step(sum_values),
    )
    return workflow.run(limit, use_ray=True)


def test_local_chain_and_dynamic_map() -> None:
    workflow = chain(
        step(make_range),
        map_step(multiply, factor=3),
        step(sum_values),
    )

    assert workflow.run(4, use_ray=False) == 18


def test_local_execution_does_not_evaluate_output_preview_projector() -> None:
    OUTPUT_PREVIEW_CALLS.clear()

    result = step(increment).with_output_preview(preview_increment).run(4, use_ray=False)

    assert result == 5
    assert OUTPUT_PREVIEW_CALLS == []


def test_map_step_preserves_leaf_kwargs_named_like_limits() -> None:
    mapped = map_step(echo_limits, max_concurrency=7, max_items=9).with_limits(
        max_concurrency=2,
        max_items=10,
    )

    assert mapped.run([1, 2], use_ray=False) == [(1, 7, 9), (2, 7, 9)]
    assert mapped.max_concurrency == 2
    assert mapped.max_items == 10


@pytest.mark.parametrize("size", [100, 1_000, 5_000])
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


def test_bounded_map_sanitizes_nested_rejection_before_failure_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.execution_codec import (
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
    )

    executor = _InstrumentedWindowExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)

    with pytest.raises(NestedExecutionRequestRejected) as caught:
        map_step(fail_with_nested_request_rejection).with_limits(
            max_concurrency=1,
            max_items=1,
        ).run([1])

    assert caught.value.classification is (NestedExecutionRequestRejection.RUNTIME_ENV_MISMATCH)
    assert executor.finished_errors == ["nested execution request rejected: runtime_env_mismatch"]
    reported_error = executor.finished_errors[0]
    assert reported_error is not None
    assert "secret-bearing" not in reported_error


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


def test_step_clones_preserve_or_explicitly_disable_output_preview() -> None:
    original = step(increment).with_output_preview(preview_increment)

    assert original.output_preview_path == "tests.unit.test_workflows.preview_increment"
    assert original.with_options(num_cpus=1).output_preview_path == original.output_preview_path
    assert (
        original.with_runtime_env({"env_vars": {"MODE": "preview"}}).output_preview_path
        == original.output_preview_path
    )
    assert original.with_output_preview(None).output_preview_path is None


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


def test_durable_task_context_is_scoped() -> None:
    assert get_current_task_execution_pk() is None
    with durable_task_execution(42):
        assert get_current_task_execution_pk() == 42
    assert get_current_task_execution_pk() is None


def test_progress_actor_builds_node_snapshot() -> None:
    identity = _workflow_identity(
        run_id="00000000-0000-0000-0000-000000000010",
    )
    plan_summary = _workflow_plan_summary()
    progress = WorkflowProgressActor(
        prepare_workflow_progress_event(
            identity.as_dict(),
            WorkflowProgressEventKind.INITIALIZED,
            {"plan": plan_summary},
        )
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.NODE_REGISTERED,
        {
            "node_id": "0.0",
            "label": "prepare",
            "callable_path": "tests.unit.test_workflows.increment",
            "runtime_env": {"mode": "inherit", "hash": "abc"},
            "ray_options": {"num_cpus": 1},
        },
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.STARTED,
        {
            "node_id": "0.0",
            "label": "prepare",
            "execution": {
                "ray_task_id": "ray-task-1",
                "ray_job_id": None,
                "ray_node_id": None,
                "ray_worker_id": None,
                "assigned_resources": {},
            },
        },
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.COMPLETED,
        {"node_id": "0.0", "label": "prepare"},
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.NODE_REGISTERED,
        {
            "node_id": "0.1.m0",
            "label": "leaf",
            "callable_path": "tests.unit.test_workflows.multiply",
            "runtime_env": {"mode": "inherit"},
            "ray_options": {},
        },
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        {"edges": [{"source": "0.0", "target": "0.1.m0"}]},
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.STARTED,
        {
            "node_id": "0.1.m0",
            "label": "leaf",
            "execution": {
                "ray_task_id": None,
                "ray_job_id": None,
                "ray_node_id": None,
                "ray_worker_id": None,
                "assigned_resources": {},
            },
        },
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.SUBMITTED,
        {
            "node_id": "0.1.m0",
            "label": "leaf",
            "ray_task_id": "ray-task-2",
        },
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        {
            "node_id": "0.1.m0",
            "current": 2,
            "total": 4,
            "message": "half way",
            "metrics": {"rows": 10},
        },
    )

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
    assert snapshot["plan"] == plan_summary
    assert "selection" not in snapshot["plan"]
    assert snapshot["total_nodes"] == 2
    assert snapshot["completed_nodes"] == 1
    assert snapshot["running_nodes"] == 1
    assert snapshot["progress_percent"] == 50.0
    assert snapshot["graph"]["edges"] == [{"source": "0.0", "target": "0.1.m0"}]
    assert snapshot["graph"]["nodes"][0]["execution"]["ray_task_id"] == "ray-task-1"
    assert snapshot["graph"]["nodes"][1]["label"] == "leaf"
    assert snapshot["graph"]["nodes"][1]["progress"]["percent"] == 50.0
    assert snapshot["ingress"]["accepted"] == 9
    assert snapshot["ingress"]["rejected"] == 0
    assert snapshot["ingress"]["retained_nodes"] == 2
    assert snapshot["ingress"]["retained_edges"] == 1
    assert snapshot["revision"] == unchanged["revision"]
    assert snapshot["updated_at"] == unchanged["updated_at"]


def test_progress_actor_aggregates_bounded_map_items() -> None:
    identity = _workflow_identity()
    progress = WorkflowProgressActor(
        prepare_workflow_progress_event(
            identity.as_dict(),
            WorkflowProgressEventKind.INITIALIZED,
            {"plan": _workflow_plan_summary()},
        )
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.MAP_REGISTERED,
        {
            "node_id": "0.1",
            "label": "map:increment",
            "max_concurrency": 4,
            "max_items": 50_000,
        },
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.EDGES_REGISTERED,
        {"edges": [{"source": "0.0", "target": "0.1"}]},
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.MAP_PROGRESS,
        {
            "node_id": "0.1",
            "label": "map:increment",
            "submitted": 50_000,
            "completed": 49_999,
            "input_exhausted": True,
        },
    )
    _ingest_event(
        progress,
        identity,
        WorkflowProgressEventKind.COMPLETED,
        {"node_id": "0.1", "label": "map:increment"},
    )

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
    identity = _workflow_identity()
    actor = _IngestOnlyProgressActor()

    assert report_progress(1, 2) is False
    with workflow_step_execution(actor, "0.1", identity.as_dict()):
        assert report_progress(1, 2, message="half", metrics={"rows": 5}) is True

    events = _decoded_ingests(actor, identity)
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        WorkflowProgressEventKind.PRODUCER_REPORT,
    ]
    assert events[0].payload == {
        "current": 1.0,
        "message": "half",
        "metrics": {"rows": 5},
        "node_id": "0.1",
        "total": 2.0,
    }
    assert events[1].payload == {
        "schema_version": 1,
        "saturated": False,
        "offered": 1,
        "submitted": 1,
        "superseded": 0,
        "locally_dropped": 0,
        "acknowledged": 1,
        "actor_rejected": 0,
        "ack_failed": 0,
        "pending_acknowledgements": 0,
        "terminal_handoff": "not_needed",
    }


def test_report_progress_validates_values_and_metrics() -> None:
    from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_METRICS_MAX_ITEMS

    identity = _workflow_identity()
    actor = _IngestOnlyProgressActor()
    with workflow_step_execution(actor, "0.1", identity.as_dict()):
        with pytest.raises(ValueError, match="total must be greater than zero"):
            report_progress(0, 0)
        with pytest.raises(ValueError, match="current must be between zero and total"):
            report_progress(-1, 1)
        with pytest.raises(ValueError, match="must be a scalar"):
            report_progress(1, 2, metrics={"bad": object()})
        with pytest.raises(ValueError):
            report_progress(float("nan"), 2)
        with pytest.raises(ValueError):
            report_progress(
                1,
                2,
                metrics={
                    f"metric-{index}": index
                    for index in range(WORKFLOW_PROGRESS_METRICS_MAX_ITEMS + 1)
                },
            )

    assert actor.ingest.calls == []


def test_report_progress_actor_requires_complete_run_identity() -> None:
    actor = _IngestOnlyProgressActor()

    with workflow_step_execution(actor, "0.1"):
        with pytest.raises(AssertionError, match="complete run identity"):
            report_progress(1, 2)

    assert actor.ingest.calls == []


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


def test_non_strict_durable_executor_refuses_before_creating_remotes(monkeypatch):
    from django_ray.execution_codec import NestedExecutionRequestRejected

    monkeypatch.setattr(
        "django_ray.workflows._get_cached_workflow_remotes",
        lambda: pytest.fail("unbound durable executor created remotes"),
    )
    with durable_task_execution(9), pytest.raises(NestedExecutionRequestRejected):
        _RayExecutor()
