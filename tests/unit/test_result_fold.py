"""Tests for bounded actor-backed ordered workflow result folds."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from django_ray.execution_codec import (
    ExecutionIdentity,
    NestedCallableBindingKind,
    NestedExecutionBoundaryKind,
    NestedExecutionRequest,
    NestedExecutionRequestRejected,
    NestedExecutionRequestRejection,
    NestedWorkflowBoundaryIdentity,
    encode_nested_execution_request,
)
from django_ray.runtime.result_fold import (
    RESULT_FOLD_ACTOR_MAX_PENDING_CALLS,
    RESULT_FOLD_CODEC,
    RESULT_FOLD_CODEC_VERSION,
    RESULT_FOLD_PROTOCOL,
    RESULT_FOLD_PROTOCOL_VERSION,
    ResultFoldOverflowError,
    ResultFoldProtocolError,
    WorkflowMapResultFold,
    normalize_result_fold_actor_options,
    result_fold_plan_contract,
    result_fold_ray_actor_options,
    validate_result_fold_ack,
)
from django_ray.workflow_plans import (
    MAX_PLAN_BYTES,
    PLAN_FORMAT_VERSION,
    WorkflowPlanMismatchError,
    WorkflowPlanValidationError,
    materialize_workflow_plan,
)
from django_ray.workflows import (
    WorkflowDefinitionError,
    _Executor,
    _LocalExecutor,
    _RayExecutor,
    chain,
    group,
    map_step,
    step,
)

SIDE_EFFECTS: list[int] = []


def identity(value: Any) -> Any:
    return value


def increment(value: int) -> int:
    return value + 1


def double(value: int) -> int:
    return value * 2


def append_text(accumulator: str, item: Any, separator: str = ">") -> str:
    return f"{accumulator}{separator}{item}"


def sum_items(accumulator: int, item: int) -> int:
    return accumulator + item


def strict_context_reducer(accumulator: int, item: int) -> dict[str, Any]:
    from django_ray.runtime.context import get_current_task_context

    context = get_current_task_context()
    assert context is not None
    return {
        "value": accumulator + item,
        "task_pk": context.task_pk,
        "task_id": context.task_id,
        "attempt_number": context.attempt_number,
        "execution_generation": context.execution_generation,
        "execution_protocol_version": context.execution_protocol_version,
        "strict_execution_request": context.strict_execution_request,
        "compiled_graph_submission_transport": (context.compiled_graph_submission_transport),
        "runtime_env_hash": context.runtime_env_hash,
        "runtime_env_plan_identity": context.runtime_env_plan_identity,
    }


def counted_strict_reducer(accumulator: int, item: int, counter: Any) -> int:
    """Record reducer invocation through a real Ray actor."""
    import ray

    ray.get(counter.increment.remote())
    return accumulator + item


def sum_from_none(accumulator: int | None, item: int) -> int:
    return (0 if accumulator is None else accumulator) + item


def sum_group(accumulator: int, item: list[int]) -> int:
    return accumulator + sum(item)


def mutate_list(accumulator: list[int], item: int) -> list[int]:
    accumulator.append(item)
    return accumulator


def grow_accumulator(accumulator: str, item: str) -> str:
    return accumulator + item


def fail_on_two(accumulator: list[int], item: int) -> list[int]:
    if item == 2:
        raise RuntimeError("reducer failed on two")
    return [*accumulator, item]


def record_side_effect(value: int) -> int:
    SIDE_EFFECTS.append(value)
    return value


async def async_reducer(accumulator: int, item: int) -> int:
    return accumulator + item


def generator_reducer(accumulator: int, item: int):
    yield accumulator + item


def wrapped_generator_reducer(accumulator: int, item: int) -> Any:
    return (value for value in (accumulator + item,))


def reducer_with_bound(
    accumulator: int,
    item: int,
    multiplier: int,
    *,
    offset: int = 0,
) -> int:
    return accumulator + item * multiplier + offset


def alternate_reducer(accumulator: int, item: int) -> int:
    return accumulator - item


class _UnserializableInitial:
    def __reduce__(self) -> Any:
        raise TypeError("initial cannot be serialized")


def _actor_options(**overrides: Any) -> dict[str, Any]:
    return {
        "num_cpus": 0.25,
        "memory": 64 * 1024,
        **overrides,
    }


def _strict_fold_request(
    *,
    callable_path: str,
    workflow_run_id: str = "00000000-0000-4000-8000-000000000611",
    node_id: str = "0.reducer",
) -> tuple[str, dict[str, object], dict[str, object]]:
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.workflow_plans import runtime_env_plan_identity

    outer_identity = ExecutionIdentity(
        task_execution_pk=611,
        task_id="00000000-0000-4000-8000-000000000611",
        attempt_number=3,
        execution_generation=7,
    )
    runtime_identity = runtime_env_plan_identity(
        normalize_runtime_env({"env_vars": {"FOLD_TEST": "1"}})
    ).as_transport_dict()
    serialized = encode_nested_execution_request(
        NestedExecutionRequest(
            outer_identity=outer_identity,
            execution_protocol_version=1,
            boundary_kind=NestedExecutionBoundaryKind.RESULT_FOLD,
            boundary_identity=NestedWorkflowBoundaryIdentity(
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            ),
            callable_binding_kind=NestedCallableBindingKind.PATH,
            callable_binding=callable_path,
            runtime_env_plan_identity=runtime_identity,
            runtime_env_plan_digest=str(runtime_identity["digest"]),
            runtime_env_transport_digest=str(runtime_identity["transport_digest"]),
        )
    )
    kwargs: dict[str, object] = {
        "nested_execution_request": serialized,
        "expected_outer_task_execution_pk": outer_identity.task_execution_pk,
        "expected_outer_task_id": outer_identity.task_id,
        "expected_outer_attempt_number": outer_identity.attempt_number,
        "expected_outer_execution_generation": outer_identity.execution_generation,
        "expected_execution_protocol_version": 1,
        "expected_workflow_run_id": workflow_run_id,
        "expected_node_id": node_id,
        "expected_runtime_env_plan_digest": runtime_identity["digest"],
        "expected_runtime_env_transport_digest": runtime_identity["transport_digest"],
    }
    return serialized, runtime_identity, kwargs


def _fold(
    *,
    reducer: Any = sum_items,
    initial: Any = 0,
    max_items: int = 20,
    max_concurrency: int = 4,
    max_serialized_bytes: int = 32 * 1024,
    actor_options: dict[str, Any] | None = None,
):
    return (
        map_step(identity)
        .with_limits(max_items=max_items, max_concurrency=max_concurrency)
        .reduce(
            reducer if hasattr(reducer, "callable_path") else step(reducer),
            initial=initial,
            max_serialized_bytes=max_serialized_bytes,
            actor_options=actor_options or _actor_options(),
        )
    )


def test_builder_requires_explicit_bounds_step_and_supported_sync_reducer() -> None:
    reduce_call: Any = (
        map_step(identity)
        .with_limits(
            max_items=2,
            max_concurrency=1,
        )
        .reduce
    )
    with pytest.raises(TypeError, match="initial"):
        reduce_call(
            step(sum_items),
            max_serialized_bytes=1024,
            actor_options=_actor_options(),
        )
    with pytest.raises(ValueError, match="positive max_concurrency and max_items"):
        map_step(identity).reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=1024,
            actor_options=_actor_options(),
        )
    with pytest.raises(ValueError, match="positive max_concurrency and max_items"):
        map_step(identity).with_limits(max_items=2).reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=1024,
            actor_options=_actor_options(),
        )
    with pytest.raises(TypeError, match="one Step reducer"):
        map_step(identity).with_limits(max_items=2, max_concurrency=1).reduce(
            sum_items,  # type: ignore[arg-type]
            initial=0,
            max_serialized_bytes=1024,
            actor_options=_actor_options(),
        )
    with pytest.raises(WorkflowDefinitionError, match="Ray task options"):
        _fold(reducer=step(sum_items).with_options(num_cpus=1))
    with pytest.raises(WorkflowDefinitionError, match="synchronous non-generator"):
        _fold(reducer=async_reducer)
    with pytest.raises(WorkflowDefinitionError, match="synchronous non-generator"):
        _fold(reducer=generator_reducer)


def test_builder_requires_positive_serialized_bound_and_resource_accounting() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _fold(max_serialized_bytes=0)
    with pytest.raises(TypeError, match="must be an integer"):
        _fold(max_serialized_bytes=True)
    with pytest.raises(ValueError, match="explicitly set num_cpus"):
        _fold(actor_options={"memory": 64 * 1024})
    with pytest.raises(ValueError, match="at least max_serialized_bytes"):
        _fold(max_serialized_bytes=32 * 1024, actor_options=_actor_options(memory=1024))


def test_worker_only_reducer_can_be_bound_by_its_runtime_environment() -> None:
    reducer = step(
        "worker_only_package.reducers.merge",
        runtime_env={"working_dir": "https://example.invalid/fold-code.zip"},
    )
    signature = _fold(reducer=reducer)

    materialized = materialize_workflow_plan(signature, invocation_args=([1],))

    assert materialized.binding_for_node("0.reducer") is not None
    assert "worker_only_package.reducers.merge" in materialized.plan.canonical_json


def test_fold_and_list_buffer_modes_are_mutually_exclusive() -> None:
    buffered = (
        map_step(identity)
        .with_limits(max_items=2, max_concurrency=1)
        .with_result_buffer(
            max_serialized_bytes=1024,
            actor_options={"num_cpus": 0.25, "memory": 1024},
        )
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        buffered.reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=1024,
            actor_options={"num_cpus": 0.25, "memory": 1024},
        )
    folded = _fold(max_serialized_bytes=1024, actor_options=_actor_options(memory=1024))
    with pytest.raises(ValueError, match="mutually exclusive"):
        folded.with_result_buffer(
            max_serialized_bytes=1024,
            actor_options={"num_cpus": 0.25, "memory": 1024},
        )


def test_local_fold_handles_empty_single_generator_and_non_commutative_order() -> None:
    signature = _fold(reducer=append_text, initial="start")

    assert signature.run([], use_ray=False) == "start"
    assert signature.run([1], use_ray=False) == "start>1"
    assert signature.run((value for value in [3, 1, 2]), use_ray=False) == "start>3>1>2"
    assert _fold(reducer=sum_from_none, initial=None).run([1, 2], use_ray=False) == 3


def test_local_fold_supports_nested_static_chain_and_group_mappers() -> None:
    chained = (
        map_step(chain(step(increment), step(double)))
        .with_limits(max_items=10, max_concurrency=2)
        .reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=4096,
            actor_options=_actor_options(),
        )
    )
    grouped = (
        map_step(group(step(increment), step(double)))
        .with_limits(max_items=10, max_concurrency=2)
        .reduce(
            step(sum_group),
            initial=0,
            max_serialized_bytes=4096,
            actor_options=_actor_options(),
        )
    )

    assert chained.run([1, 2, 3], use_ray=False) == 18
    assert grouped.run([1, 2, 3], use_ray=False) == 21


def test_local_fold_clones_mutable_initial_each_run_without_enforcing_ray_byte_limit() -> None:
    initial: list[int] = []
    signature = _fold(
        reducer=mutate_list,
        initial=initial,
        max_serialized_bytes=1,
        actor_options={"num_cpus": 0.25, "memory": 1},
    )

    assert signature.run([1, 2], use_ray=False) == [1, 2]
    assert signature.run([1, 2], use_ray=False) == [1, 2]
    assert initial == []


def test_local_initial_serialization_fails_before_leaf_effects() -> None:
    SIDE_EFFECTS.clear()
    signature = (
        map_step(record_side_effect)
        .with_limits(max_items=4, max_concurrency=2)
        .reduce(
            step(sum_items),
            initial=_UnserializableInitial(),
            max_serialized_bytes=4096,
            actor_options=_actor_options(),
        )
    )

    with pytest.raises(TypeError, match="initial cannot be serialized"):
        signature.run([1, 2], use_ray=False)
    assert SIDE_EFFECTS == []


def test_local_missing_runtime_only_reducer_fails_before_leaf_effects() -> None:
    SIDE_EFFECTS.clear()
    signature = (
        map_step(record_side_effect)
        .with_limits(max_items=4, max_concurrency=2)
        .reduce(
            step(
                "worker_only_package.reducers.merge",
                runtime_env={"working_dir": "https://example.invalid/fold-code.zip"},
            ),
            initial=0,
            max_serialized_bytes=4096,
            actor_options=_actor_options(),
        )
    )

    with pytest.raises(ImportError, match="worker_only_package"):
        signature.run([1, 2], use_ray=False)
    assert SIDE_EFFECTS == []


def test_decorated_sync_reducer_cannot_return_a_generator() -> None:
    signature = _fold(reducer=wrapped_generator_reducer)

    with pytest.raises(ResultFoldProtocolError, match="concrete synchronous value"):
        signature.run([1], use_ray=False)


def test_local_start_validates_bootstrap_and_async_shape_before_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstraps: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstraps.append(True),
    )
    executor = _LocalExecutor()
    options = normalize_result_fold_actor_options(
        _actor_options(),
        max_serialized_bytes=4096,
    )
    assert (
        executor.start_result_fold(
            max_items=2,
            max_concurrency=1,
            max_serialized_bytes=4096,
            actor_options=options,
            reducer=step(sum_items, django=True),
            reducer_node_id="0.reducer",
            initial=0,
        )
        is None
    )
    assert bootstraps == [True]
    assert executor.reduce_local(step(sum_items, django=True), 1, 2) == 3
    assert bootstraps == [True, True]
    assert executor.wait_result_fold_leaf(["ready"]) == 0

    with pytest.raises(WorkflowDefinitionError, match="synchronous non-generator"):
        executor.start_result_fold(
            max_items=2,
            max_concurrency=1,
            max_serialized_bytes=4096,
            actor_options=options,
            reducer=step(async_reducer),
            reducer_node_id="0.reducer",
            initial=0,
        )


def test_nested_dynamic_fold_shapes_are_rejected_before_leaf_effects() -> None:
    SIDE_EFFECTS.clear()
    inner_fold = (
        map_step(record_side_effect)
        .with_limits(max_items=2, max_concurrency=1)
        .reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=4096,
            actor_options=_actor_options(),
        )
    )
    with pytest.raises(WorkflowPlanValidationError, match="cannot be nested"):
        map_step(inner_fold).with_limits(max_items=2, max_concurrency=1).run(
            [[1]],
            use_ray=False,
        )
    fold_with_nested_mapper = (
        map_step(chain(step(identity), map_step(record_side_effect)))
        .with_limits(max_items=2, max_concurrency=1)
        .reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=4096,
            actor_options=_actor_options(),
        )
    )
    with pytest.raises(WorkflowPlanValidationError, match="mapper cannot contain"):
        fold_with_nested_mapper.run([[1]], use_ray=False)
    assert SIDE_EFFECTS == []


def test_actor_folds_out_of_order_values_strictly_and_releases_credits() -> None:
    actor = WorkflowMapResultFold(
        3,
        3,
        4096,
        f"{__name__}.append_text",
        False,
        (),
        {"separator": "|"},
        "start",
    )

    ready = validate_result_fold_ack(actor.ready(), state="ready")
    third = validate_result_fold_ack(actor.append(2, "third"), state="folded", expected_index=2)
    second = validate_result_fold_ack(actor.append(1, "second"), state="folded", expected_index=1)
    first = validate_result_fold_ack(actor.append(0, "first"), state="folded", expected_index=0)
    result, final = actor.finalize(3)

    assert ready["folded_items"] == 0
    assert third["released_credits"] == second["released_credits"] == 0
    assert first["released_credits"] == 3
    assert first["folded_items"] == 3
    assert first["out_of_order_items"] == 0
    assert result == "start|first|second|third"
    assert validate_result_fold_ack(final, state="finalized", expected_items=3)


def test_ack_validation_rejects_malformed_or_inconsistent_counts() -> None:
    actor = WorkflowMapResultFold(
        2,
        2,
        4096,
        f"{__name__}.sum_items",
        False,
        (),
        {},
        0,
    )
    ready = actor.ready()
    finalized = actor.finalize(0)[1]

    invalid_cases = [
        ([], "ready", {}, "must be a mapping"),
        ({**ready, "payload": "forbidden"}, "ready", {}, "unexpected payload"),
        ({**ready, "protocol_version": 99}, "ready", {}, "protocol mismatch"),
        ({**ready, "retained_bytes": -1}, "ready", {}, "non-negative integer"),
        (
            {
                **ready,
                "state": "folded",
                "index": 1,
                "released_credits": 0,
            },
            "folded",
            {"expected_index": 0},
            "index mismatch",
        ),
        (finalized, "finalized", {"expected_items": 1}, "item count mismatch"),
        (
            {**finalized, "out_of_order_items": 1},
            "finalized",
            {"expected_items": 0},
            "retained out-of-order",
        ),
    ]
    for value, state, kwargs, message in invalid_cases:
        with pytest.raises(ResultFoldProtocolError, match=message):
            validate_result_fold_ack(value, state=state, **kwargs)


@pytest.mark.parametrize(
    ("overrides", "error_type", "message"),
    [
        ({"max_items": 0}, ValueError, "max_items"),
        ({"max_concurrency": 0}, ValueError, "max_concurrency"),
        ({"max_serialized_bytes": 0}, ValueError, "max_serialized_bytes"),
        ({"reducer_callable_path": ""}, TypeError, "reducer_callable_path"),
        ({"reducer_bootstrap_django": 1}, TypeError, "reducer_bootstrap_django"),
        ({"reducer_bound_args": []}, TypeError, "reducer_bound_args"),
        ({"reducer_bound_kwargs": []}, TypeError, "reducer_bound_kwargs"),
    ],
)
def test_actor_constructor_rejects_invalid_protocol_inputs(
    overrides: dict[str, Any],
    error_type: type[Exception],
    message: str,
) -> None:
    kwargs: dict[str, Any] = {
        "max_items": 2,
        "max_concurrency": 2,
        "max_serialized_bytes": 4096,
        "reducer_callable_path": f"{__name__}.sum_items",
        "reducer_bootstrap_django": False,
        "reducer_bound_args": (),
        "reducer_bound_kwargs": {},
        "initial": 0,
    }
    kwargs.update(overrides)

    with pytest.raises(error_type, match=message):
        WorkflowMapResultFold(**kwargs)


def test_actor_bootstraps_and_defensively_rejects_async_reducer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstraps: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstraps.append(True),
    )
    actor = WorkflowMapResultFold(
        1,
        1,
        4096,
        f"{__name__}.sum_items",
        True,
        (),
        {},
        0,
    )
    assert actor.ready()["folded_items"] == 0
    assert bootstraps == [True]

    with pytest.raises(ResultFoldProtocolError, match="synchronous and non-generator"):
        WorkflowMapResultFold(
            1,
            1,
            4096,
            f"{__name__}.async_reducer",
            False,
            (),
            {},
            0,
        )


def test_strict_fold_installs_context_for_later_reducer_and_finalize_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ray.cloudpickle as cloudpickle

    from django_ray.runtime.context import get_current_task_context

    callable_path = f"{__name__}.strict_context_reducer"
    _, runtime_identity, request_kwargs = _strict_fold_request(callable_path=callable_path)
    actor = WorkflowMapResultFold(
        1,
        1,
        4096,
        callable_path,
        False,
        (),
        {},
        0,
        **request_kwargs,
    )
    original_loads = cloudpickle.loads
    observed_unpickle_contexts: list[bool] = []

    def loads_in_strict_context(payload: bytes) -> Any:
        context = get_current_task_context()
        observed_unpickle_contexts.append(
            context is not None and context.strict_execution_request is True
        )
        return original_loads(payload)

    monkeypatch.setattr(cloudpickle, "loads", loads_in_strict_context)

    assert validate_result_fold_ack(actor.ready(), state="ready")["folded_items"] == 0
    assert actor.append(0, 2)["folded_items"] == 1
    result, final = actor.finalize(1)

    assert validate_result_fold_ack(final, state="finalized", expected_items=1)
    assert result == {
        "value": 2,
        "task_pk": 611,
        "task_id": "00000000-0000-4000-8000-000000000611",
        "attempt_number": 3,
        "execution_generation": 7,
        "execution_protocol_version": 1,
        "strict_execution_request": True,
        "compiled_graph_submission_transport": "direct-ray-core",
        "runtime_env_hash": "",
        "runtime_env_plan_identity": runtime_identity,
    }
    assert observed_unpickle_contexts
    assert all(observed_unpickle_contexts)


def test_strict_fold_reports_fixed_ready_rejection_before_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callable_path = f"{__name__}.sum_items"
    serialized, _, request_kwargs = _strict_fold_request(callable_path=callable_path)
    request_kwargs["nested_execution_request"] = serialized.replace(
        '"node_id":"0.reducer"',
        '"node_id":"secret-mixed-node"',
    )
    bootstrapped: list[bool] = []
    monkeypatch.setattr(
        "django_ray.runtime.entrypoint.bootstrap_django",
        lambda: bootstrapped.append(True),
    )
    monkeypatch.setattr(
        "django_ray.runtime.result_fold.import_callable",
        lambda _path: pytest.fail("reducer imported before strict ready rejection"),
    )

    actor = WorkflowMapResultFold(
        1,
        1,
        4096,
        callable_path,
        True,
        (),
        {},
        _UnserializableInitial(),
        **request_kwargs,
    )

    with pytest.raises(NestedExecutionRequestRejected) as captured:
        actor.ready()
    assert captured.value.classification is NestedExecutionRequestRejection.BOUNDARY_MISMATCH
    assert "secret-mixed-node" not in str(captured.value)
    assert captured.value.retryable is False
    assert bootstrapped == []


def test_actor_rejects_invalid_append_and_finalize_transitions() -> None:
    actor = WorkflowMapResultFold(
        3,
        2,
        4096,
        f"{__name__}.sum_items",
        False,
        (),
        {},
        0,
    )
    with pytest.raises(ResultFoldProtocolError, match="non-negative integer"):
        actor.append(-1, 1)
    with pytest.raises(ResultFoldOverflowError, match="max_items=3"):
        actor.append(3, 1)
    actor.append(1, 2)
    with pytest.raises(ResultFoldProtocolError, match="appended twice"):
        actor.append(1, 2)
    with pytest.raises(ResultFoldProtocolError, match="admission window"):
        actor.append(2, 3)
    with pytest.raises(ResultFoldProtocolError, match="Invalid result-fold"):
        actor.finalize(-1)
    with pytest.raises(ResultFoldProtocolError, match="missing or unexpected"):
        actor.finalize(2)

    actor.append(0, 1)
    actor.finalize(2)
    with pytest.raises(ResultFoldProtocolError, match="after result-fold finalization"):
        actor.append(2, 3)
    with pytest.raises(ResultFoldProtocolError, match="already finalized"):
        actor.finalize(2)


def large_first_reducer(accumulator: str, item: str) -> str:
    del accumulator, item
    return "z" * 200


def test_contiguous_fold_combined_overflow_is_atomic_after_consuming_next_item() -> None:
    actor = WorkflowMapResultFold(
        3,
        3,
        300,
        f"{__name__}.large_first_reducer",
        False,
        (),
        {},
        "",
    )
    actor.append(2, "x" * 100)
    actor.append(1, "small")
    retained_before = actor.retained_bytes

    with pytest.raises(ResultFoldOverflowError, match="combined retained"):
        actor.append(0, "first")

    assert actor.folded_items == 0
    assert actor.retained_bytes == retained_before
    assert actor.ready()["out_of_order_items"] == 2


def test_actor_overflow_checks_initial_item_accumulator_and_combined_state() -> None:
    with pytest.raises(ResultFoldOverflowError, match="initial accumulator"):
        WorkflowMapResultFold(
            2,
            2,
            32,
            f"{__name__}.sum_items",
            False,
            (),
            {},
            "x" * 4096,
        )

    actor = WorkflowMapResultFold(
        2,
        2,
        128,
        f"{__name__}.grow_accumulator",
        False,
        (),
        {},
        "",
    )
    with pytest.raises(ResultFoldOverflowError, match="item serialization"):
        actor.append(0, "x" * 4096)
    assert actor.folded_items == 0

    actor = WorkflowMapResultFold(
        2,
        2,
        256,
        f"{__name__}.grow_accumulator",
        False,
        (),
        {},
        "x" * 120,
    )
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        actor.append(0, "y" * 200)
    assert actor.folded_items == 0

    import ray.cloudpickle as cloudpickle

    initial = "a" * 100
    later = "b" * 100
    combined_limit = (
        len(cloudpickle.dumps(initial, protocol=5)) + len(cloudpickle.dumps(later, protocol=5)) - 1
    )
    actor = WorkflowMapResultFold(
        2,
        2,
        combined_limit,
        f"{__name__}.grow_accumulator",
        False,
        (),
        {},
        initial,
    )
    with pytest.raises(ResultFoldOverflowError, match="combined retained"):
        actor.append(1, later)
    assert actor.folded_items == 0
    assert actor.ready()["out_of_order_items"] == 0


def test_contiguous_reducer_failure_is_transactional_and_preserves_original_error() -> None:
    actor = WorkflowMapResultFold(
        3,
        3,
        4096,
        f"{__name__}.fail_on_two",
        False,
        (),
        {},
        [],
    )
    actor.append(2, 3)
    actor.append(1, 2)
    retained_before = actor.retained_bytes

    with pytest.raises(RuntimeError, match="reducer failed on two"):
        actor.append(0, 1)

    assert actor.folded_items == 0
    assert actor.retained_bytes == retained_before
    discarded = actor.discard()
    assert discarded["out_of_order_items"] == 2
    assert discarded["folded_items"] == 0


def test_plan_contract_records_complete_fold_semantics_without_initial_value() -> None:
    options = normalize_result_fold_actor_options(
        _actor_options(resources={"fold": 1}, scheduling_strategy="SPREAD"),
        max_serialized_bytes=4096,
    )
    contract = result_fold_plan_contract(
        max_items=10,
        max_concurrency=3,
        max_serialized_bytes=4096,
        actor_options=options,
        reducer={"callable": {"ref": "callable:1"}},
    )

    assert contract["protocol"] == RESULT_FOLD_PROTOCOL
    assert contract["protocol_version"] == RESULT_FOLD_PROTOCOL_VERSION
    assert contract["codec"] == {
        "name": RESULT_FOLD_CODEC,
        "version": RESULT_FOLD_CODEC_VERSION,
        "pickle_protocol": 5,
        "measurement": "retained_serialized_bytes",
    }
    assert contract["ordering"]["kind"] == "strict_input_order_left_fold"
    assert contract["bounds"]["maximum_out_of_order_items"] == 2
    assert contract["bounds"]["maximum_retained_state_objects"] == 3
    assert options["max_concurrency"] == 1
    assert options["max_pending_calls"] == RESULT_FOLD_ACTOR_MAX_PENDING_CALLS == 2
    assert contract["bounds"]["maximum_pending_actor_calls"] == 2
    assert contract["admission"]["credit_source"] == "incorporated_items"
    assert contract["initial"] == {
        "binding": "invocation_data",
        "required": True,
        "persisted_value": False,
        "validated_before_leaf_admission": True,
    }
    assert contract["result"]["finalize_returns"] == 2
    assert contract["result"]["payload_ref_resolved_by_coordinator"] is False
    assert result_fold_ray_actor_options(options)["lifetime"] is None


def test_materialized_plan_uses_v1_actor_slots_and_excludes_initial_values() -> None:
    first = materialize_workflow_plan(
        _fold(initial={"secret": "super-secret-alpha-7788"}),
        invocation_args=([1, 2],),
    )
    second = materialize_workflow_plan(
        _fold(initial={"secret": "super-secret-beta-9911"}),
        invocation_args=([1, 2],),
    )
    manifest = first.plan.manifest
    actor = manifest["physical_topology"]["actors"][0]

    assert PLAN_FORMAT_VERSION == 1
    assert first.plan.fingerprint == second.plan.fingerprint
    assert "super-secret-alpha-7788" not in first.plan.canonical_json
    assert "super-secret-beta-9911" not in second.plan.canonical_json
    assert actor["kind"] == "ordered_map_result_fold"
    assert actor["id"] == "0.result_fold"
    assert actor["contract"]["reducer"]["binding_schema"] == {
        "bound_positional_count": 0,
        "bound_keyword_names": (),
        "keyword_precedence": "bound_over_invocation",
    }
    assert manifest["nodes"][0]["actor_layout"] == "0.result_fold"
    assert manifest["nodes"][-1]["operation"] == "ordered_actor_fold_finalize"
    assert manifest["capabilities"]["admission"]["maximum_buffered_results"] == 4
    assert first.binding_for_node("0.reducer") is not None


def test_fold_plan_fingerprint_changes_for_effective_semantics() -> None:
    def fingerprint(**overrides: Any) -> str:
        reducer = overrides.pop("reducer", step(sum_items))
        initial = overrides.pop("initial", 0)
        max_items = overrides.pop("max_items", 20)
        max_concurrency = overrides.pop("max_concurrency", 4)
        max_serialized_bytes = overrides.pop("max_serialized_bytes", 4096)
        actor_options = {
            "num_cpus": overrides.pop("num_cpus", 0.25),
            "memory": overrides.pop("memory", 8192),
            "resources": overrides.pop("resources", {}),
            "scheduling_strategy": overrides.pop("scheduling_strategy", "DEFAULT"),
        }
        assert not overrides
        signature = (
            map_step(identity)
            .with_limits(max_items=max_items, max_concurrency=max_concurrency)
            .reduce(
                reducer,
                initial=initial,
                max_serialized_bytes=max_serialized_bytes,
                actor_options=actor_options,
            )
        )
        return materialize_workflow_plan(
            signature,
            invocation_args=([1],),
        ).plan.fingerprint

    base = fingerprint()
    changes = [
        fingerprint(reducer=step(alternate_reducer)),
        fingerprint(reducer=step(sum_items, django=True)),
        fingerprint(reducer=step(reducer_with_bound, 2, offset=1)),
        fingerprint(reducer=step(sum_items, runtime_env={"env_vars": {"MODE": "fold"}})),
        fingerprint(max_items=21),
        fingerprint(max_concurrency=5),
        fingerprint(max_serialized_bytes=5000),
        fingerprint(num_cpus=0.5),
        fingerprint(memory=16384),
        fingerprint(resources={"fold": 1}),
        fingerprint(scheduling_strategy="SPREAD"),
    ]

    assert all(value != base for value in changes)
    assert fingerprint(initial=999) == base


@pytest.mark.django_db
def test_retry_rejects_fold_resource_drift_before_leaf_effects() -> None:
    from django_ray.lifecycle import record_failure
    from django_ray.models import RayTaskExecution, TaskState
    from django_ray.runtime.context import DurableTaskContext
    from django_ray.workflow_progress import allocate_workflow_run

    execution = RayTaskExecution.objects.create(
        task_id="workflow-result-fold-plan-retry",
        callable_path=f"{__name__}.record_side_effect",
        state=TaskState.RUNNING,
        execution_generation=3,
    )

    def folded(memory: int):
        return (
            map_step(record_side_effect)
            .with_limits(max_items=4, max_concurrency=2)
            .reduce(
                step(sum_items),
                initial=0,
                max_serialized_bytes=4096,
                actor_options={"num_cpus": 0.25, "memory": memory},
            )
        )

    first_plan = materialize_workflow_plan(
        folded(8192),
        invocation_args=([1],),
    ).plan
    first_selection = first_plan.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
    )
    first_identity = allocate_workflow_run(
        DurableTaskContext(
            execution.pk,
            execution.attempt_number,
            execution.execution_generation,
        ),
        plan=first_plan,
        selection=first_selection,
    )
    assert first_identity is not None
    assert record_failure(execution, error_message="retry", retry=True)
    RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.RUNNING)
    execution.refresh_from_db()

    replacement = materialize_workflow_plan(
        folded(16384),
        invocation_args=([1],),
    ).plan
    replacement_selection = replacement.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
    )
    SIDE_EFFECTS.clear()

    with pytest.raises(WorkflowPlanMismatchError, match="different effective plan"):
        allocate_workflow_run(
            DurableTaskContext(
                execution.pk,
                execution.attempt_number,
                execution.execution_generation,
            ),
            plan=replacement,
            selection=replacement_selection,
        )
    assert SIDE_EFFECTS == []


def test_many_fold_actors_keep_overflow_manifest_bounded_and_identity_bearing() -> None:
    signatures = tuple(
        _fold(
            reducer=step(sum_items, index),
            max_items=2,
            max_concurrency=2,
            actor_options=_actor_options(resources={f"fold-{index:03d}-{'x' * 180}": 1}),
        )
        for index in range(55)
    )
    plan = materialize_workflow_plan(
        group(*signatures),
        invocation_args=([1],),
    ).plan
    summary = plan.manifest["physical_topology"]["overflow_summary"]

    assert len(plan.canonical_json.encode()) <= MAX_PLAN_BYTES
    assert plan.manifest["snapshot"]["state"] == "overflow"
    assert plan.manifest["snapshot"]["source_digest"].startswith("sha256:")
    assert summary["result_fold_actor_count"] == 55
    assert plan.manifest["physical_topology"]["actors"] == ()


@dataclass(eq=False)
class _FoldRef:
    value: Any
    kind: str


@dataclass
class _DeterministicFoldExecutor(_Executor):
    fail_on_item: int | None = None
    reducer: Any = None
    accumulator: Any = None
    out_of_order: dict[int, Any] = field(default_factory=dict)
    incorporated: int = 0
    submitted: int = 0
    maximum_window_refs: int = 0
    maximum_outstanding: int = 0
    maximum_out_of_order: int = 0
    maximum_retained_bytes: int = 0
    max_serialized_bytes: int = 0
    map_progress_nodes: int = 0
    physical_progress_nodes: int = 0
    suppression_depth: int = 0
    discarded: bool = False
    cancelled_payloads: list[_FoldRef] = field(default_factory=list)

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        del dependencies
        if self.suppression_depth:
            self.submitted += 1
            self.maximum_outstanding = max(
                self.maximum_outstanding,
                self.submitted - self.incorporated,
            )
        else:
            self.physical_progress_nodes += 1
        callable_obj = __import__(
            signature.callable_path.rsplit(".", 1)[0],
            fromlist=[signature.callable_path.rsplit(".", 1)[1]],
        )
        function = getattr(callable_obj, signature.callable_path.rsplit(".", 1)[1])
        values = tuple(item.value if isinstance(item, _FoldRef) else item for item in input_args)
        result = function(
            *values,
            *signature.bound_args,
            **{**input_kwargs, **signature.bound_kwargs},
        )
        return _FoldRef(result, f"payload:{node_id}")

    def collect(self, values):
        raise AssertionError("result fold must not collect mapped payloads in the coordinator")

    def resolve(self, value):
        if isinstance(value, _FoldRef):
            if value.kind.startswith("payload:0.m"):
                raise AssertionError("coordinator decoded a mapped payload")
            return value.value
        return value

    def start_result_fold(self, **kwargs):
        from django_ray.runtime.result_fold import clone_result_fold_initial

        self.reducer = kwargs["reducer"]
        self.accumulator = clone_result_fold_initial(kwargs["initial"])
        self.max_serialized_bytes = kwargs["max_serialized_bytes"]
        self._track_retained_bytes()
        return self

    def wait_result_fold_leaf(self, values):
        self.maximum_window_refs = max(self.maximum_window_refs, len(values))
        return len(values) - 1

    def append_result_fold(self, fold, *, index, value):
        assert fold is self
        assert isinstance(value, _FoldRef)
        self.out_of_order[index] = value.value
        while self.incorporated in self.out_of_order:
            item = self.out_of_order.pop(self.incorporated)
            if item == self.fail_on_item:
                raise RuntimeError(f"reducer failed for item {item}")
            self.accumulator = self.reduce_local(self.reducer, self.accumulator, item)
            self.incorporated += 1
        self.maximum_out_of_order = max(self.maximum_out_of_order, len(self.out_of_order))
        self._track_retained_bytes()
        return self.incorporated

    def _track_retained_bytes(self) -> None:
        import ray.cloudpickle as cloudpickle

        retained = len(cloudpickle.dumps(self.accumulator, protocol=5))
        retained += sum(
            len(cloudpickle.dumps(value, protocol=5)) for value in self.out_of_order.values()
        )
        self.maximum_retained_bytes = max(self.maximum_retained_bytes, retained)
        assert retained <= self.max_serialized_bytes

    def finalize_result_fold(self, fold, *, expected_items):
        assert fold is self
        assert self.incorporated == expected_items
        assert self.out_of_order == {}
        return _FoldRef(self.accumulator, "fold_payload")

    def discard_result_fold(self, fold, *, timeout_seconds):
        assert fold is self
        assert timeout_seconds >= 0
        self.discarded = True
        self.out_of_order.clear()

    def cancel_and_drain_fold_payloads(self, values, *, timeout_seconds):
        assert timeout_seconds >= 0
        self.cancelled_payloads.extend(values)

    @contextmanager
    def suppress_progress(self):
        self.suppression_depth += 1
        try:
            yield
        finally:
            self.suppression_depth -= 1

    def map_started(self, *args, **kwargs):
        del args, kwargs
        self.map_progress_nodes += 1


@pytest.mark.parametrize("item_count", [1_000, 10_000, 50_000])
def test_large_folds_keep_admission_references_bytes_and_plan_metadata_bounded(
    monkeypatch: pytest.MonkeyPatch,
    item_count: int,
) -> None:
    executor = _DeterministicFoldExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    max_concurrency = 8
    max_serialized_bytes = 64 * 1024
    signature = (
        map_step(identity)
        .with_limits(max_items=item_count, max_concurrency=max_concurrency)
        .reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=max_serialized_bytes,
            actor_options=_actor_options(),
        )
    )

    result = signature.run(1 for _ in range(item_count))
    plan = materialize_workflow_plan(signature, invocation_args=(range(item_count),)).plan

    assert result == item_count
    assert executor.maximum_window_refs <= max_concurrency
    assert executor.maximum_outstanding <= max_concurrency
    assert executor.maximum_out_of_order == max_concurrency - 1
    assert executor.maximum_retained_bytes <= max_serialized_bytes
    assert executor.map_progress_nodes == 1
    assert executor.physical_progress_nodes == 0
    assert len(plan.manifest["nodes"]) == 3
    assert ".m0" not in plan.canonical_json
    assert "persisted_items" not in plan.canonical_json


def test_fold_failure_stops_admission_uses_payload_safe_cleanup_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _DeterministicFoldExecutor(fail_on_item=2)
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    signature = (
        map_step(identity)
        .with_limits(
            max_items=10,
            max_concurrency=2,
            cancel_timeout_seconds=0.25,
        )
        .reduce(
            step(sum_items),
            initial=0,
            max_serialized_bytes=4096,
            actor_options=_actor_options(),
        )
    )

    with pytest.raises(RuntimeError, match="reducer failed for item 2"):
        signature.run([1, 2, 3, 4])

    assert executor.submitted == 2
    assert executor.discarded is True
    assert executor.cancelled_payloads
    assert all(value.kind.startswith("payload:0.m") for value in executor.cancelled_payloads)


class _PayloadSafeRay:
    def __init__(self) -> None:
        self.cancelled: list[Any] = []
        self.waits: list[dict[str, Any]] = []
        self.get_calls = 0

    def cancel(self, value, *, force, recursive):
        assert force is False
        assert recursive is True
        self.cancelled.append(value)

    def wait(self, values, *, num_returns, timeout, fetch_local=True):
        self.waits.append(
            {
                "values": list(values),
                "num_returns": num_returns,
                "timeout": timeout,
                "fetch_local": fetch_local,
            }
        )
        return list(values), []

    def get(self, value):
        del value
        self.get_calls += 1
        raise AssertionError("fold cleanup decoded a mapped payload")


def test_ray_fold_cleanup_never_fetches_or_decodes_mapped_payloads() -> None:
    fake_ray = _PayloadSafeRay()
    executor = object.__new__(_RayExecutor)
    executor.ray = fake_ray
    payloads = [_FoldRef("large", "leaf"), _FoldRef("nested", "nested")]

    executor.cancel_and_drain_fold_payloads(payloads, timeout_seconds=0.5)

    assert fake_ray.cancelled == payloads
    assert fake_ray.get_calls == 0
    assert fake_ray.waits == [
        {
            "values": payloads,
            "num_returns": 2,
            "timeout": 0.5,
            "fetch_local": False,
        }
    ]


def test_ray_fold_rejects_invalid_ready_state_and_cleans_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RemoteCall:
        def __init__(self, result: Any) -> None:
            self.result = result

        def remote(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return self.result

    class _FoldActor:
        def __init__(self) -> None:
            self.ready = _RemoteCall("ready-ref")
            self.discard = _RemoteCall("discard-ref")

    class _FoldActorClass:
        def __init__(self, actor: _FoldActor) -> None:
            self.actor = actor

        def options(self, **options: Any) -> _FoldActorClass:
            assert options["num_cpus"] == 0.25
            return self

        def remote(self, *args: Any) -> _FoldActor:
            assert args[0:3] == (4, 2, 4096)
            return self.actor

    class _CleanupRay:
        def __init__(self) -> None:
            self.cancelled: list[Any] = []
            self.killed: list[Any] = []

        def cancel(self, value: Any, *, force: bool, recursive: bool) -> None:
            assert force is False
            assert recursive is True
            self.cancelled.append(value)

        def wait(
            self,
            values: list[Any],
            *,
            num_returns: int,
            timeout: float,
            fetch_local: bool,
        ) -> tuple[list[Any], list[Any]]:
            assert num_returns == 1
            assert timeout == 0
            assert fetch_local is False
            return values, []

        def get(self, value: Any) -> dict[str, Any]:
            assert value == "discard-ref"
            return {"state": "discarded"}

        def kill(self, actor: Any, *, no_restart: bool) -> None:
            assert no_restart is True
            self.killed.append(actor)

    actor = _FoldActor()
    actor_class = _FoldActorClass(actor)
    fake_ray = _CleanupRay()
    executor = object.__new__(_RayExecutor)
    executor.ray = fake_ray
    invalid_ready = {
        "protocol": RESULT_FOLD_PROTOCOL,
        "protocol_version": RESULT_FOLD_PROTOCOL_VERSION,
        "codec": RESULT_FOLD_CODEC,
        "codec_version": RESULT_FOLD_CODEC_VERSION,
        "state": "ready",
        "folded_items": 1,
        "out_of_order_items": 0,
        "retained_bytes": 1,
    }
    monkeypatch.setattr(
        "django_ray.workflows._get_cached_result_fold_actor",
        lambda: actor_class,
    )
    monkeypatch.setattr(_RayExecutor, "resolve", lambda self, value: invalid_ready)
    monkeypatch.setattr(
        _RayExecutor,
        "_result_fold_runtime_env",
        lambda self, reducer, reducer_node_id: None,
    )
    options = normalize_result_fold_actor_options(
        _actor_options(),
        max_serialized_bytes=4096,
    )

    with pytest.raises(ResultFoldProtocolError, match="invalid initial state"):
        executor.start_result_fold(
            max_items=4,
            max_concurrency=2,
            max_serialized_bytes=4096,
            actor_options=options,
            reducer=step(sum_items),
            reducer_node_id="0.reducer",
            initial=0,
        )

    assert fake_ray.cancelled == ["ready-ref"]
    assert fake_ray.killed == [actor]


def test_ray_executor_resolves_unbound_reducer_runtime_env_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django_ray.runtime.runtime_env import normalize_runtime_env

    executor = object.__new__(_RayExecutor)
    executor.materialized_plan = None

    assert executor._result_fold_runtime_env(step(sum_items), "0.reducer") is None
    inline = executor._result_fold_runtime_env(
        step(sum_items, runtime_env={"env_vars": {"FOLD_MODE": "inline"}}),
        "0.reducer",
    )
    assert inline is not None
    assert inline["env_vars"]["FOLD_MODE"] == "inline"

    monkeypatch.setattr(
        "django_ray.runtime.runtime_env.resolve_runtime_env_profile",
        lambda profile: normalize_runtime_env(
            {"env_vars": {"FOLD_PROFILE": profile}},
            profile=profile,
        ),
    )
    named = executor._result_fold_runtime_env(
        step(sum_items, runtime_env="ordered-fold"),
        "0.reducer",
    )
    assert named is not None
    assert named["env_vars"]["FOLD_PROFILE"] == "ordered-fold"


class _CoordinatorDecodeGuard:
    def __init__(self, value: int, forbidden_pid: int) -> None:
        self.value = value
        self.forbidden_pid = forbidden_pid

    def __reduce__(self):
        return (_restore_decode_guard, (self.value, self.forbidden_pid))


def _restore_decode_guard(value: int, forbidden_pid: int) -> _CoordinatorDecodeGuard:
    if os.getpid() == forbidden_pid:
        raise RuntimeError("mapped payload was decoded in the workflow coordinator")
    return _CoordinatorDecodeGuard(value, forbidden_pid)


def build_sync_resource(
    index: int,
    coordinator_pid: int,
    payload_bytes: int,
) -> dict[str, Any]:
    if index == 0:
        time.sleep(0.2)
    return {
        "namespace": f"application-{index % 2}",
        "name": f"workload-{index}",
        "manifest": "x" * payload_bytes,
        "guard": _CoordinatorDecodeGuard(index, coordinator_pid),
    }


def merge_sync_summary(
    accumulator: dict[str, Any],
    item: dict[str, Any],
    expected_env: str,
) -> dict[str, Any]:
    assert os.environ.get("DJANGO_RAY_FOLD_ENV") == expected_env
    expected_index = accumulator["count"]
    assert item["guard"].value == expected_index
    return {
        "count": expected_index + 1,
        "names": [*accumulator["names"], item["name"]],
        "manifest_bytes": accumulator["manifest_bytes"] + len(item["manifest"]),
    }


def summary_count(summary: dict[str, Any]) -> int:
    return int(summary["count"])


class _RealEventTracker:
    def __init__(self) -> None:
        self.values: list[int] = []

    def record(self, value: int) -> None:
        self.values.append(value)

    def snapshot(self) -> list[int]:
        return list(self.values)


class _RealInvocationCounter:
    def __init__(self) -> None:
        self.count = 0

    def increment(self) -> None:
        self.count += 1

    def value(self) -> int:
        return self.count


def build_oversized_fold_item(index: int, tracker: Any) -> str:
    import ray

    ray.get(tracker.record.remote(index))
    return "x" * 4096


def count_payload(accumulator: int, item: str) -> int:
    return accumulator + len(item)


def env_sum(accumulator: int, item: int) -> int:
    assert os.environ.get("DJANGO_RAY_FOLD_DIRECT_ENV") == "ready"
    return accumulator + item


class _ResultFoldOwner:
    def spawn(self) -> Any:
        import ray

        from django_ray.runtime.result_fold import (
            WorkflowMapResultFold,
            normalize_result_fold_actor_options,
            result_fold_ray_actor_options,
        )

        options = normalize_result_fold_actor_options(
            {"num_cpus": 0.1, "memory": 1024 * 1024},
            max_serialized_bytes=1024 * 1024,
        )
        child = (
            ray.remote(WorkflowMapResultFold)
            .options(**result_fold_ray_actor_options(options))
            .remote(
                2,
                2,
                1024 * 1024,
                f"{__name__}.sum_items",
                False,
                (),
                {},
                0,
            )
        )
        ray.get(child.ready.remote())
        return child


@pytest.mark.real_ray
def test_real_ray_strict_fold_ready_rejection_is_typed_and_has_no_effects(
    ray_cluster: Any,
) -> None:
    from ray.exceptions import RayTaskError

    callable_path = f"{__name__}.counted_strict_reducer"
    serialized, _runtime_identity, kwargs = _strict_fold_request(
        callable_path=callable_path,
        workflow_run_id="00000000-0000-4000-8000-000000000612",
        node_id="0.reducer-real-ray",
    )
    protocol_payload = json.loads(serialized)
    protocol_payload["execution_protocol_version"] += 1
    tampered_protocol = json.dumps(
        protocol_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    counter = ray_cluster.remote(num_cpus=0)(_RealInvocationCounter).remote()
    cases = (
        (
            tampered_protocol,
            callable_path,
            NestedExecutionRequestRejection.PROTOCOL_MISMATCH,
        ),
        (
            serialized,
            f"{__name__}.alternate_reducer",
            NestedExecutionRequestRejection.CALLABLE_MISMATCH,
        ),
    )

    for submitted_request, submitted_callable, classification in cases:
        actor = (
            ray_cluster.remote(WorkflowMapResultFold)
            .options(num_cpus=0.25, max_restarts=0, max_task_retries=0)
            .remote(
                1,
                1,
                1024 * 1024,
                submitted_callable,
                False,
                (counter,),
                {},
                0,
                **{**kwargs, "nested_execution_request": submitted_request},
            )
        )
        try:
            with pytest.raises(RayTaskError) as caught:
                ray_cluster.get(actor.ready.remote())

            cause = caught.value.cause
            assert isinstance(cause, NestedExecutionRequestRejected)
            assert cause.classification is classification
            assert str(cause) == (f"nested execution request rejected: {classification.value}")
            assert cause.retryable is False
        finally:
            ray_cluster.kill(actor, no_restart=True)

    assert ray_cluster.get(counter.value.remote()) == 0


@pytest.mark.real_ray
def test_real_ray_production_summary_stays_out_of_coordinator_until_final_result() -> None:
    import ray

    ray.init(ignore_reinit_error=True, num_cpus=4)
    try:
        payload_bytes = 64 * 1024
        reducer = step(
            merge_sync_summary,
            "fold-runtime",
            runtime_env={"env_vars": {"DJANGO_RAY_FOLD_ENV": "fold-runtime"}},
        )
        workflow = chain(
            step(identity),
            map_step(build_sync_resource, os.getpid(), payload_bytes)
            .with_limits(max_items=8, max_concurrency=3)
            .reduce(
                reducer,
                initial={"count": 0, "names": [], "manifest_bytes": 0},
                max_serialized_bytes=2 * 1024 * 1024,
                actor_options={
                    "num_cpus": 0.25,
                    "memory": 4 * 1024 * 1024,
                    "scheduling_strategy": "SPREAD",
                },
            ),
            step(summary_count),
        )

        assert workflow.run([0, 1, 2, 3], use_ray=True) == 4
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_item_overflow_stops_admission_and_preserves_actor_error() -> None:
    import ray
    from ray.exceptions import RayTaskError

    ray.init(ignore_reinit_error=True, num_cpus=3)
    try:
        tracker = ray.remote(num_cpus=0)(_RealEventTracker).remote()
        workflow = chain(
            step(identity),
            map_step(build_oversized_fold_item, tracker)
            .with_limits(max_items=10, max_concurrency=1)
            .reduce(
                step(count_payload),
                initial=0,
                max_serialized_bytes=256,
                actor_options={"num_cpus": 0.25, "memory": 1024 * 1024},
            ),
        )

        with pytest.raises(RayTaskError, match="item serialization.*max_serialized_bytes=256"):
            workflow.run(range(10), use_ray=True)
        assert ray.get(tracker.snapshot.remote()) == [0]
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup() -> None:
    import ray
    from ray.exceptions import RayActorError
    from ray.util.state import get_actor

    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        address="local",
        num_cpus=2,
        resources={"result_fold": 1},
    )
    try:
        reducer = step(
            env_sum,
            runtime_env={"env_vars": {"DJANGO_RAY_FOLD_DIRECT_ENV": "ready"}},
        )
        signature = (
            map_step(identity)
            .with_limits(max_items=2, max_concurrency=2)
            .reduce(
                reducer,
                initial=0,
                max_serialized_bytes=1024 * 1024,
                actor_options={
                    "num_cpus": 0.25,
                    "memory": 2 * 1024 * 1024,
                    "resources": {"result_fold": 0.5},
                    "scheduling_strategy": "SPREAD",
                },
            )
        )
        materialized = materialize_workflow_plan(signature, invocation_args=([1, 2],))
        executor = _RayExecutor(materialized)
        assert signature.result_fold is not None
        session = executor.start_result_fold(
            max_items=2,
            max_concurrency=2,
            max_serialized_bytes=1024 * 1024,
            actor_options=dict(signature.result_fold.actor_options),
            reducer=reducer,
            reducer_node_id="0.reducer",
            initial=0,
        )
        actor_state = get_actor(session.actor._actor_id.hex())
        assert actor_state is not None
        assert actor_state.required_resources == {
            "CPU": 0.25,
            "memory": float(2 * 1024 * 1024),
            "result_fold": 0.5,
        }
        assert actor_state.is_detached is False
        assert actor_state.num_restarts == 0

        assert (
            executor.append_result_fold(
                session,
                index=1,
                value=ray.put(2),
            )
            == 0
        )
        assert (
            executor.append_result_fold(
                session,
                index=0,
                value=ray.put(1),
            )
            == 2
        )
        payload_ref = executor.finalize_result_fold(session, expected_items=2)

        assert isinstance(payload_ref, ray.ObjectRef)
        assert ray.get(payload_ref) == 3
        deadline = time.monotonic() + 10
        while True:
            try:
                ray.get(session.actor.ready.remote(), timeout=0.5)
            except RayActorError:
                break
            if time.monotonic() >= deadline:
                pytest.fail("result-fold actor survived successful cleanup")
            time.sleep(0.05)
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_non_detached_fold_dies_with_owner() -> None:
    import ray
    from ray.exceptions import RayActorError, RayTaskError

    ray.init(ignore_reinit_error=True, num_cpus=2)
    try:
        owner = ray.remote(num_cpus=0.1)(_ResultFoldOwner).remote()
        child = ray.get(owner.spawn.remote())
        assert validate_result_fold_ack(ray.get(child.ready.remote()), state="ready")

        ray.kill(owner, no_restart=True)
        deadline = time.monotonic() + 10
        while True:
            try:
                ray.get(child.ready.remote(), timeout=0.5)
            except (RayActorError, RayTaskError):
                break
            if time.monotonic() >= deadline:
                pytest.fail("non-detached result fold survived owner death")
            time.sleep(0.05)
    finally:
        ray.shutdown()
