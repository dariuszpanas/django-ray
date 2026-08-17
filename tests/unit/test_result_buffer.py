"""Tests for the bounded Ray workflow map result-buffer protocol."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from django_ray.runtime.result_buffer import (
    RESULT_BUFFER_ACTOR_MAX_CONCURRENCY,
    RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS,
    RESULT_BUFFER_CODEC,
    RESULT_BUFFER_CODEC_VERSION,
    RESULT_BUFFER_PROTOCOL,
    RESULT_BUFFER_PROTOCOL_VERSION,
    ResultBufferOverflowError,
    ResultBufferProtocolError,
    WorkflowMapResultBuffer,
    normalize_result_buffer_actor_options,
    result_buffer_plan_contract,
    validate_result_buffer_ack,
)
from django_ray.workflow.plans import WorkflowPlanValidationError
from django_ray.workflows import _Executor, _RayExecutor, chain, map_step, step

SIDE_EFFECTS: list[int] = []


def increment(value: int) -> int:
    return value + 1


def identity(value: Any) -> Any:
    return value


def record_side_effect(value: int) -> int:
    SIDE_EFFECTS.append(value)
    return value


def sum_values(values: list[int]) -> int:
    return sum(values)


class _CoordinatorDecodeGuard:
    def __init__(self, value: int, forbidden_pid: int) -> None:
        self.value = value
        self.forbidden_pid = forbidden_pid

    def __reduce__(self):
        return (_restore_coordinator_decode_guard, (self.value, self.forbidden_pid))


def _restore_coordinator_decode_guard(
    value: int,
    forbidden_pid: int,
) -> _CoordinatorDecodeGuard:
    if os.getpid() == forbidden_pid:
        raise RuntimeError("mapped payload was decoded in the outer coordinator")
    return _CoordinatorDecodeGuard(value, forbidden_pid)


def build_k8s_resource(
    index: int,
    coordinator_pid: int,
    payload_bytes: int,
) -> dict[str, Any]:
    return {
        "namespace": f"application-{index % 2}",
        "kind": "Deployment",
        "name": f"workload-{index}",
        "resource_version": str(10_000 + index),
        "labels": {"app.kubernetes.io/managed-by": "django-ray"},
        "manifest": {"spec": {"replicas": index + 1, "template": "x" * payload_bytes}},
        "guard": _CoordinatorDecodeGuard(index, coordinator_pid),
    }


def reduce_k8s_resources(values: list[dict[str, Any]]) -> dict[str, Any]:
    assert [value["guard"].value for value in values] == list(range(len(values)))
    return {
        "count": len(values),
        "names": [value["name"] for value in values],
        "manifest_bytes": sum(len(value["manifest"]["spec"]["template"]) for value in values),
    }


class _RealEventTracker:
    def __init__(self) -> None:
        self.values: list[int] = []

    def record(self, value: int) -> None:
        self.values.append(value)

    def snapshot(self) -> list[int]:
        return list(self.values)


def build_oversized_resource(index: int, tracker: Any) -> dict[str, Any]:
    import ray

    ray.get(tracker.record.remote(index))
    return {"index": index, "manifest": "x" * 4096}


class _ResultBufferOwner:
    def spawn(self) -> Any:
        import ray

        from django_ray.runtime.result_buffer import (
            WorkflowMapResultBuffer,
            normalize_result_buffer_actor_options,
            result_buffer_ray_actor_options,
        )

        options = normalize_result_buffer_actor_options(
            {"num_cpus": 0.1, "memory": 1024 * 1024},
            max_serialized_bytes=1024 * 1024,
        )
        child = (
            ray.remote(WorkflowMapResultBuffer)
            .options(**result_buffer_ray_actor_options(options))
            .remote(1, 1024 * 1024)
        )
        ray.get(child.ready.remote())
        return child


def _actor_options(**overrides: Any) -> dict[str, Any]:
    return {
        "num_cpus": 0.25,
        "memory": 4096,
        **overrides,
    }


def test_actor_options_are_canonical_resource_accounted_and_non_detached() -> None:
    options = normalize_result_buffer_actor_options(
        _actor_options(
            resources={"zeta": 2.0, "alpha": 1},
            scheduling_strategy="SPREAD",
        ),
        max_serialized_bytes=2048,
    )

    assert options["max_concurrency"] == RESULT_BUFFER_ACTOR_MAX_CONCURRENCY == 1
    assert options["max_pending_calls"] == RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS == 2
    assert options == {
        "num_cpus": 0.25,
        "memory": 4096,
        "resources": {"alpha": 1, "zeta": 2},
        "scheduling_strategy": "SPREAD",
        "lifetime": "non_detached",
        "max_restarts": 0,
        "max_task_retries": 0,
        "max_concurrency": RESULT_BUFFER_ACTOR_MAX_CONCURRENCY,
        "max_pending_calls": RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS,
    }


def test_builder_requires_all_three_explicit_positive_bounds() -> None:
    with pytest.raises(ValueError, match="positive max_concurrency and max_items"):
        map_step(increment).with_result_buffer(
            max_serialized_bytes=2048,
            actor_options=_actor_options(),
        )
    with pytest.raises(ValueError, match="positive max_concurrency and max_items"):
        map_step(increment).with_limits(max_concurrency=2).with_result_buffer(
            max_serialized_bytes=2048,
            actor_options=_actor_options(),
        )
    with pytest.raises(ValueError, match="positive max_concurrency and max_items"):
        map_step(increment).with_limits(max_items=10).with_result_buffer(
            max_serialized_bytes=2048,
            actor_options=_actor_options(),
        )
    with pytest.raises(ValueError, match="max_serialized_bytes must be at least 1"):
        map_step(increment).with_limits(
            max_concurrency=2,
            max_items=10,
        ).with_result_buffer(
            max_serialized_bytes=0,
            actor_options=_actor_options(),
        )


def test_opt_in_keeps_local_execution_actor_free_and_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "django_ray.workflows._get_cached_result_buffer_actor",
        lambda: pytest.fail("local result-buffer execution created a Ray actor"),
    )
    signature = (
        map_step(identity)
        .with_limits(
            max_concurrency=2,
            max_items=10,
        )
        .with_result_buffer(
            # Local mode validates and fingerprints this bound but deliberately
            # does not cloudpickle or measure results without a Ray actor.
            max_serialized_bytes=1,
            actor_options={"num_cpus": 0.25, "memory": 1},
        )
    )

    oversized_values = ["x" * 4096, "y" * 4096]
    assert signature.run(oversized_values, use_ray=False) == oversized_values


def test_nested_result_buffer_is_rejected_before_local_leaf_effects() -> None:
    SIDE_EFFECTS.clear()
    buffered_inner = (
        map_step(record_side_effect)
        .with_limits(
            max_concurrency=1,
            max_items=2,
        )
        .with_result_buffer(
            max_serialized_bytes=2048,
            actor_options=_actor_options(),
        )
    )
    outer = map_step(buffered_inner).with_limits(max_concurrency=1, max_items=2)

    with pytest.raises(WorkflowPlanValidationError, match="cannot be nested"):
        outer.run([[1]], use_ray=False)

    assert SIDE_EFFECTS == []


@pytest.mark.parametrize(
    ("actor_options", "message"),
    [
        ({"memory": 4096}, "num_cpus"),
        ({"num_cpus": 0.25}, "memory"),
        (_actor_options(num_cpus=0), "greater than zero"),
        (_actor_options(num_cpus=True), "must be a number"),
        (_actor_options(memory=1024.5), "integer byte count"),
        (_actor_options(memory=1024), "at least max_serialized_bytes"),
        (_actor_options(num_gpus=1), "unsupported fields: num_gpus"),
        (_actor_options(lifetime="detached"), "unsupported fields: lifetime"),
        (_actor_options(max_restarts=1), "unsupported fields: max_restarts"),
        (_actor_options(scheduling_strategy="NODE_AFFINITY"), "DEFAULT.*SPREAD"),
        (_actor_options(scheduling_strategy=[]), "must be a string"),
        (_actor_options(resources={"database": 0}), "greater than zero"),
    ],
)
def test_actor_options_reject_unbounded_or_unsupported_values(
    actor_options: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        normalize_result_buffer_actor_options(
            actor_options,
            max_serialized_bytes=2048,
        )


@pytest.mark.parametrize(
    ("actor_options", "message"),
    [
        (_actor_options(resources={1: 1}), "must be a string"),
        (_actor_options(resources={"": 1}), "must contain 1 to 256 characters"),
        (_actor_options(num_cpus=float("inf")), "must be finite"),
        (_actor_options(resources=[]), "resources must be a mapping"),
        (
            _actor_options(resources={f"resource-{index}": 1 for index in range(33)}),
            "resources must contain at most 32 entries",
        ),
        (
            _actor_options(**{f"option_{index}": 1 for index in range(15)}),
            "actor_options must contain at most 16 entries",
        ),
    ],
)
def test_actor_options_reject_malformed_bounded_structures(
    actor_options: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        normalize_result_buffer_actor_options(
            actor_options,
            max_serialized_bytes=2048,
        )


def test_actor_options_reject_invalid_container_and_serialized_bound() -> None:
    with pytest.raises(ValueError, match="max_serialized_bytes must be a positive integer"):
        normalize_result_buffer_actor_options(
            _actor_options(),
            max_serialized_bytes=0,
        )
    with pytest.raises(TypeError, match="actor_options must be a mapping"):
        normalize_result_buffer_actor_options(
            [],  # type: ignore[arg-type]
            max_serialized_bytes=2048,
        )


def test_plan_contract_versions_codec_bounds_and_fixed_actor_semantics() -> None:
    options = normalize_result_buffer_actor_options(
        _actor_options(),
        max_serialized_bytes=2048,
    )

    contract = result_buffer_plan_contract(
        max_items=10,
        max_concurrency=3,
        max_serialized_bytes=2048,
        actor_options=options,
    )

    assert contract["protocol"] == RESULT_BUFFER_PROTOCOL
    assert contract["protocol_version"] == RESULT_BUFFER_PROTOCOL_VERSION == 1
    assert contract["codec"] == {
        "name": RESULT_BUFFER_CODEC,
        "version": RESULT_BUFFER_CODEC_VERSION,
        "pickle_protocol": 5,
        "measurement": "retained_serialized_bytes",
    }
    assert contract["bounds"] == {
        "maximum_items": 10,
        "maximum_in_flight_leaves": 3,
        "maximum_serialized_bytes": 2048,
        "maximum_pending_actor_calls": 2,
    }
    assert (
        contract["bounds"]["maximum_pending_actor_calls"]
        == RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS
        == 2
    )
    assert contract["lifetime"]["kind"] == "non_detached"
    assert contract["lifetime"]["node_loss_recovery"] is False
    assert contract["restart"] == {"max_restarts": 0, "max_task_retries": 0}
    assert contract["result"]["finalize_returns"] == 2
    assert contract["result"]["payload_ref_resolved_by_coordinator"] is False


def test_actor_counts_retained_cloudpickle_bytes_and_preserves_order() -> None:
    actor = WorkflowMapResultBuffer(max_items=3, max_serialized_bytes=4096)

    assert validate_result_buffer_ack(actor.ready(), state="ready")["state"] == "ready"
    second = actor.append(1, {"value": "second"})
    first = actor.append(0, {"value": "first"})
    payload, ack = actor.finalize(2)

    assert second["retained_bytes"] > 0
    assert first["retained_bytes"] > second["retained_bytes"]
    assert payload == [{"value": "first"}, {"value": "second"}]
    assert (
        validate_result_buffer_ack(
            ack,
            state="finalized",
            expected_items=2,
        )["retained_bytes"]
        == first["retained_bytes"]
    )
    assert actor.retained_bytes == 0


def test_actor_checks_byte_overflow_before_retaining_the_item() -> None:
    actor = WorkflowMapResultBuffer(max_items=2, max_serialized_bytes=512)
    first_ack = actor.append(0, "small")

    with pytest.raises(ResultBufferOverflowError, match="max_serialized_bytes=512"):
        actor.append(1, "x" * 4096)

    payload, final_ack = actor.finalize(1)
    assert payload == ["small"]
    assert final_ack["retained_bytes"] == first_ack["retained_bytes"]


def test_actor_rejects_duplicate_missing_and_post_finalize_appends() -> None:
    actor = WorkflowMapResultBuffer(max_items=2, max_serialized_bytes=4096)
    actor.append(1, "second")

    with pytest.raises(ResultBufferProtocolError, match="appended twice"):
        actor.append(1, "duplicate")
    with pytest.raises(ResultBufferProtocolError, match="missing or unexpected"):
        actor.finalize(2)

    actor.append(0, "first")
    payload, _ = actor.finalize(2)
    assert payload == ["first", "second"]
    with pytest.raises(ResultBufferProtocolError, match="after result-buffer finalization"):
        actor.append(0, "late")


@pytest.mark.parametrize(
    ("max_items", "max_serialized_bytes", "message"),
    [
        (0, 4096, "max_items must be a positive integer"),
        (1, 0, "max_serialized_bytes must be a positive integer"),
    ],
)
def test_actor_constructor_rejects_invalid_bounds(
    max_items: int,
    max_serialized_bytes: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        WorkflowMapResultBuffer(max_items, max_serialized_bytes)


def test_actor_rejects_invalid_indices_and_final_count_then_discards_state() -> None:
    actor = WorkflowMapResultBuffer(max_items=1, max_serialized_bytes=4096)

    with pytest.raises(ResultBufferProtocolError, match="non-negative integer"):
        actor.append(-1, "invalid")
    with pytest.raises(ResultBufferOverflowError, match="exceeds max_items=1"):
        actor.append(1, "overflow")
    with pytest.raises(ResultBufferProtocolError, match="Invalid result-buffer final item count"):
        actor.finalize(2)

    retained = actor.append(0, "retained")
    discarded = actor.discard()

    assert discarded == {
        "protocol": RESULT_BUFFER_PROTOCOL,
        "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION,
        "codec": RESULT_BUFFER_CODEC,
        "codec_version": RESULT_BUFFER_CODEC_VERSION,
        "state": "discarded",
        "item_count": 1,
        "retained_bytes": retained["retained_bytes"],
    }
    assert actor.retained_bytes == 0
    with pytest.raises(ResultBufferProtocolError, match="already finalized"):
        actor.finalize(1)


def test_ack_validation_rejects_payload_or_wrong_protocol() -> None:
    ready = WorkflowMapResultBuffer(1, 4096).ready()

    with pytest.raises(ResultBufferProtocolError, match="unexpected payload"):
        validate_result_buffer_ack({**ready, "payload": ["must not cross"]}, state="ready")
    with pytest.raises(ResultBufferProtocolError, match="protocol mismatch"):
        validate_result_buffer_ack(
            {**ready, "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION + 1},
            state="ready",
        )


def test_ack_validation_rejects_non_mapping_counts_and_indices() -> None:
    actor = WorkflowMapResultBuffer(1, 4096)
    retained = actor.append(0, "retained")

    with pytest.raises(ResultBufferProtocolError, match="must be a mapping"):
        validate_result_buffer_ack(None, state="ready")
    with pytest.raises(ResultBufferProtocolError, match="must be a non-negative integer"):
        validate_result_buffer_ack(
            {**retained, "retained_bytes": -1},
            state="retained",
        )
    with pytest.raises(ResultBufferProtocolError, match="index mismatch"):
        validate_result_buffer_ack(
            retained,
            state="retained",
            expected_index=1,
        )

    _, finalized = actor.finalize(1)
    with pytest.raises(ResultBufferProtocolError, match="item count mismatch"):
        validate_result_buffer_ack(
            finalized,
            state="finalized",
            expected_items=0,
        )


@dataclass(eq=False)
class _Ref:
    value: Any
    kind: str


class _RemoteMethod:
    def __init__(self, callback: Any) -> None:
        self.callback = callback

    def remote(self, *args: Any) -> Any:
        return self.callback(*args)


class _FinalizeMethod:
    def __init__(self, actor: _FakeActor) -> None:
        self.actor = actor

    def options(self, **options: Any) -> _FinalizeMethod:
        self.actor.finalize_options.append(options)
        return self

    def remote(self, expected_items: int) -> tuple[_Ref, _Ref]:
        self.actor.finalized_counts.append(expected_items)
        return (
            _Ref(["large", "payload"], "payload"),
            _Ref(
                {
                    "protocol": RESULT_BUFFER_PROTOCOL,
                    "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION,
                    "codec": RESULT_BUFFER_CODEC,
                    "codec_version": RESULT_BUFFER_CODEC_VERSION,
                    "state": "finalized",
                    "item_count": expected_items,
                    "retained_bytes": 123,
                },
                "final_ack",
            ),
        )


class _FakeActor:
    def __init__(self) -> None:
        self.appended: list[tuple[int, _Ref]] = []
        self.finalize_options: list[dict[str, Any]] = []
        self.finalized_counts: list[int] = []
        self.ready = _RemoteMethod(
            lambda: _Ref(
                {
                    "protocol": RESULT_BUFFER_PROTOCOL,
                    "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION,
                    "codec": RESULT_BUFFER_CODEC,
                    "codec_version": RESULT_BUFFER_CODEC_VERSION,
                    "state": "ready",
                },
                "ready_ack",
            )
        )
        self.append = _RemoteMethod(self._append)
        self.finalize = _FinalizeMethod(self)
        self.discard = _RemoteMethod(
            lambda: _Ref(
                {
                    "protocol": RESULT_BUFFER_PROTOCOL,
                    "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION,
                    "codec": RESULT_BUFFER_CODEC,
                    "codec_version": RESULT_BUFFER_CODEC_VERSION,
                    "state": "discarded",
                    "item_count": 0,
                    "retained_bytes": 0,
                },
                "discard_ack",
            )
        )

    def _append(self, index: int, value: _Ref) -> _Ref:
        self.appended.append((index, value))
        return _Ref(
            {
                "protocol": RESULT_BUFFER_PROTOCOL,
                "protocol_version": RESULT_BUFFER_PROTOCOL_VERSION,
                "codec": RESULT_BUFFER_CODEC,
                "codec_version": RESULT_BUFFER_CODEC_VERSION,
                "state": "retained",
                "index": index,
                "item_count": index + 1,
                "retained_bytes": 10,
            },
            "append_ack",
        )


class _FakeActorClass:
    def __init__(self, actor: _FakeActor) -> None:
        self.actor = actor
        self.options_seen: list[dict[str, Any]] = []
        self.constructor_args: list[tuple[Any, ...]] = []

    def options(self, **options: Any) -> _FakeActorClass:
        self.options_seen.append(options)
        return self

    def remote(self, *args: Any) -> _FakeActor:
        self.constructor_args.append(args)
        return self.actor


class _FakeRay:
    def __init__(self, *, kill_error: BaseException | None = None) -> None:
        self.get_kinds: list[str] = []
        self.wait_calls: list[dict[str, Any]] = []
        self.cancelled: list[_Ref] = []
        self.killed: list[tuple[Any, bool]] = []
        self.kill_error = kill_error

    def get(self, value: _Ref) -> Any:
        self.get_kinds.append(value.kind)
        if value.kind in {"leaf_payload", "payload"}:
            raise AssertionError("coordinator decoded a mapped payload")
        return value.value

    def wait(
        self,
        values: list[_Ref],
        *,
        num_returns: int = 1,
        timeout: float | None = None,
        fetch_local: bool = True,
    ) -> tuple[list[_Ref], list[_Ref]]:
        self.wait_calls.append(
            {
                "values": list(values),
                "num_returns": num_returns,
                "timeout": timeout,
                "fetch_local": fetch_local,
            }
        )
        return values[:num_returns], values[num_returns:]

    def cancel(self, value: _Ref, *, force: bool, recursive: bool) -> None:
        assert force is False
        assert recursive is True
        self.cancelled.append(value)

    def kill(self, actor: Any, *, no_restart: bool) -> None:
        self.killed.append((actor, no_restart))
        if self.kill_error is not None:
            raise self.kill_error


def _ray_executor(fake_ray: _FakeRay) -> _RayExecutor:
    executor = object.__new__(_RayExecutor)
    executor.ray = fake_ray
    executor.progress_actor = None
    executor.workflow_run_identity = None
    return executor


def test_ray_protocol_never_decodes_leaf_or_final_payload_in_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _FakeActor()
    actor_cls = _FakeActorClass(actor)
    fake_ray = _FakeRay(kill_error=RuntimeError("best-effort kill failed"))
    executor = _ray_executor(fake_ray)
    monkeypatch.setattr(
        "django_ray.workflows._get_cached_result_buffer_actor",
        lambda: actor_cls,
    )
    options = normalize_result_buffer_actor_options(
        _actor_options(resources={"buffer": 1}),
        max_serialized_bytes=2048,
    )

    session = executor.start_result_buffer(
        max_items=2,
        max_serialized_bytes=2048,
        actor_options=options,
    )
    leaf = _Ref({"large": "mapped payload"}, "leaf_payload")
    assert executor.wait_result_buffer_leaf([leaf]) == 0
    executor.append_result_buffer(session, index=0, value=leaf)
    payload_ref = executor.finalize_result_buffer(session, expected_items=1)

    assert payload_ref.kind == "payload"
    assert actor.appended == [(0, leaf)]
    assert actor.finalize_options == [{"num_returns": 2}]
    assert fake_ray.get_kinds == ["ready_ack", "append_ack", "final_ack"]
    assert all(
        call["fetch_local"] is False
        for call in fake_ray.wait_calls
        if any(ref.kind in {"leaf_payload", "payload"} for ref in call["values"])
    )
    assert fake_ray.killed == [(actor, True)]
    assert session.closed is True
    assert actor_cls.options_seen == [
        {
            "num_cpus": 0.25,
            "memory": 4096,
            "resources": {"buffer": 1},
            "scheduling_strategy": "DEFAULT",
            "lifetime": None,
            "max_restarts": 0,
            "max_task_retries": 0,
            "max_concurrency": 1,
            "max_pending_calls": 2,
        }
    ]
    assert actor_cls.constructor_args == [(2, 2048)]


def test_ack_resolution_and_payload_polling_flush_normal_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _FakeActor()
    actor_cls = _FakeActorClass(actor)
    fake_ray = _FakeRay()
    executor = _ray_executor(fake_ray)
    executor.progress_actor = SimpleNamespace()
    flushes: list[bool] = []
    executor._flush_progress = lambda **kwargs: flushes.append(True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "django_ray.workflows._get_cached_result_buffer_actor",
        lambda: actor_cls,
    )
    options = normalize_result_buffer_actor_options(
        _actor_options(),
        max_serialized_bytes=2048,
    )

    session = executor.start_result_buffer(
        max_items=1,
        max_serialized_bytes=2048,
        actor_options=options,
    )
    leaf = _Ref("mapped", "leaf_payload")
    executor.wait_result_buffer_leaf([leaf])
    executor.append_result_buffer(session, index=0, value=leaf)
    executor.finalize_result_buffer(session, expected_items=1)

    # ready resolve, leaf readiness, append resolve, final readiness, final ack resolve
    assert len(flushes) == 5


@dataclass(eq=False)
class _BufferedValue:
    value: Any
    kind: str


@dataclass
class _BufferedExecutor(_Executor):
    fail_append_index: int | None = None
    events: list[str] = field(default_factory=list)
    retained: dict[int, Any] = field(default_factory=dict)
    downstream_inputs: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    cancelled: list[_BufferedValue] = field(default_factory=list)
    discarded: bool = False

    def submit_step(
        self,
        signature,
        input_args,
        input_kwargs,
        node_id,
        dependencies,
    ):
        del dependencies
        self.events.append(f"leaf:{node_id}")
        self.downstream_inputs[node_id] = input_args
        callable_obj = __import__(
            signature.callable_path.rsplit(".", 1)[0],
            fromlist=[signature.callable_path.rsplit(".", 1)[1]],
        )
        function = getattr(callable_obj, signature.callable_path.rsplit(".", 1)[1])
        worker_args = tuple(
            item.value if isinstance(item, _BufferedValue) else item for item in input_args
        )
        value = function(
            *worker_args,
            *signature.bound_args,
            **{**input_kwargs, **signature.bound_kwargs},
        )
        return _BufferedValue(value, "leaf_payload")

    def collect(self, values):
        raise AssertionError("result-buffer map must not use a coordinator collector")

    def resolve(self, value):
        if isinstance(value, _BufferedValue):
            return value.value
        return value

    def start_result_buffer(self, **kwargs):
        self.events.append("buffer:ready")
        assert kwargs["max_items"] > 0
        assert kwargs["max_serialized_bytes"] > 0
        return object()

    def wait_result_buffer_leaf(self, values):
        # Exercise actor-side ordered assembly when leaves complete out of order.
        return len(values) - 1

    def append_result_buffer(self, buffer, *, index, value):
        del buffer
        self.events.append(f"buffer:append:{index}")
        if index == self.fail_append_index:
            raise ResultBufferOverflowError("original append overflow")
        self.retained[index] = value.value

    def finalize_result_buffer(self, buffer, *, expected_items):
        del buffer
        self.events.append("buffer:finalize")
        return _BufferedValue(
            [self.retained[index] for index in range(expected_items)],
            "buffer_payload",
        )

    def cancel_and_drain(self, values, *, timeout_seconds):
        assert timeout_seconds >= 0
        self.cancelled.extend(values)

    def discard_result_buffer(self, buffer, *, timeout_seconds):
        del buffer
        assert timeout_seconds >= 0
        self.discarded = True


def test_buffered_map_reserves_actor_before_leaves_and_passes_one_downstream_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _BufferedExecutor()
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    workflow = chain(
        map_step(increment)
        .with_limits(
            max_concurrency=2,
            max_items=10,
        )
        .with_result_buffer(
            max_serialized_bytes=2048,
            actor_options=_actor_options(),
        ),
        step(sum_values),
    )

    assert workflow.run([3, 1, 2]) == 9
    assert executor.events[0] == "buffer:ready"
    assert executor.events[-2:] == ["buffer:finalize", "leaf:0.1"]
    assert len(executor.downstream_inputs["0.1"]) == 1
    downstream_ref = executor.downstream_inputs["0.1"][0]
    assert isinstance(downstream_ref, _BufferedValue)
    assert downstream_ref.kind == "buffer_payload"
    assert executor.retained == {0: 4, 1: 2, 2: 3}


def test_buffered_map_failure_preserves_error_and_cleans_dependencies_and_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _BufferedExecutor(fail_append_index=1)
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    signature = (
        map_step(increment)
        .with_limits(
            max_concurrency=2,
            max_items=10,
            cancel_timeout_seconds=0.25,
        )
        .with_result_buffer(
            max_serialized_bytes=2048,
            actor_options=_actor_options(),
        )
    )

    with pytest.raises(ResultBufferOverflowError, match="original append overflow"):
        signature.run([1, 2, 3])

    assert executor.discarded is True
    assert len(executor.cancelled) == 2
    assert all(value.kind == "leaf_payload" for value in executor.cancelled)


def test_buffered_map_cancellation_cleans_dependencies_and_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _BufferedExecutor()

    def cancel_append(buffer, *, index, value):
        del buffer, index, value
        raise KeyboardInterrupt("workflow cancelled")

    executor.append_result_buffer = cancel_append  # type: ignore[method-assign]
    monkeypatch.setattr("django_ray.workflows._get_executor", lambda use_ray: executor)
    signature = (
        map_step(increment)
        .with_limits(max_concurrency=2, max_items=10, cancel_timeout_seconds=0.25)
        .with_result_buffer(
            max_serialized_bytes=2048,
            actor_options=_actor_options(),
        )
    )

    with pytest.raises(KeyboardInterrupt, match="workflow cancelled"):
        signature.run([1, 2, 3])

    assert executor.discarded is True
    assert len(executor.cancelled) == 2


@pytest.mark.real_ray
def test_real_ray_production_payload_stays_out_of_coordinator_until_reducer() -> None:
    import ray

    ray.init(
        ignore_reinit_error=True,
        num_cpus=4,
    )
    try:
        payload_bytes = 64 * 1024
        workflow = chain(
            step(identity),
            map_step(
                build_k8s_resource,
                os.getpid(),
                payload_bytes,
            )
            .with_limits(
                max_concurrency=2,
                max_items=8,
            )
            .with_result_buffer(
                max_serialized_bytes=2 * 1024 * 1024,
                actor_options={
                    "num_cpus": 0.25,
                    "memory": 4 * 1024 * 1024,
                    "scheduling_strategy": "SPREAD",
                },
            ),
            step(reduce_k8s_resources),
        )

        assert workflow.run([0, 1, 2, 3], use_ray=True) == {
            "count": 4,
            "names": ["workload-0", "workload-1", "workload-2", "workload-3"],
            "manifest_bytes": 4 * payload_bytes,
        }
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_overflow_stops_admission_and_preserves_actor_error() -> None:
    import ray
    from ray.exceptions import RayTaskError

    ray.init(ignore_reinit_error=True, num_cpus=3)
    try:
        tracker = ray.remote(num_cpus=0)(_RealEventTracker).remote()
        workflow = chain(
            step(identity),
            map_step(build_oversized_resource, tracker)
            .with_limits(max_concurrency=1, max_items=10)
            .with_result_buffer(
                max_serialized_bytes=256,
                actor_options={"num_cpus": 0.25, "memory": 1024 * 1024},
            ),
        )

        with pytest.raises(RayTaskError, match="max_serialized_bytes=256"):
            workflow.run(range(10), use_ray=True)

        assert ray.get(tracker.snapshot.remote()) == [0]
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_actor_resources_direct_returns_and_success_cleanup() -> None:
    import ray
    from ray.exceptions import RayActorError
    from ray.util.state import get_actor

    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        address="local",
        num_cpus=2,
        resources={"result_buffer": 1},
    )
    try:
        options = normalize_result_buffer_actor_options(
            {
                "num_cpus": 0.25,
                "memory": 2 * 1024 * 1024,
                "resources": {"result_buffer": 0.5},
                "scheduling_strategy": "SPREAD",
            },
            max_serialized_bytes=1024 * 1024,
        )
        assert options["max_concurrency"] == RESULT_BUFFER_ACTOR_MAX_CONCURRENCY == 1
        assert options["max_pending_calls"] == RESULT_BUFFER_ACTOR_MAX_PENDING_CALLS == 2
        executor = _RayExecutor()
        session = executor.start_result_buffer(
            max_items=2,
            max_serialized_bytes=1024 * 1024,
            actor_options=options,
        )
        actor_state = get_actor(session.actor._actor_id.hex())
        assert actor_state is not None
        assert actor_state.required_resources == {
            "CPU": 0.25,
            "memory": float(2 * 1024 * 1024),
            "result_buffer": 0.5,
        }
        assert actor_state.is_detached is False
        assert actor_state.num_restarts == 0

        executor.append_result_buffer(
            session,
            index=0,
            value=ray.put({"namespace": "application-0"}),
        )
        executor.append_result_buffer(
            session,
            index=1,
            value=ray.put({"namespace": "application-1"}),
        )
        payload_ref = executor.finalize_result_buffer(session, expected_items=2)

        assert isinstance(payload_ref, ray.ObjectRef)
        payload = ray.get(payload_ref)
        assert payload == [
            {"namespace": "application-0"},
            {"namespace": "application-1"},
        ]
        assert not isinstance(payload, ray.ObjectRef)
        deadline = time.monotonic() + 10
        while True:
            try:
                ray.get(session.actor.ready.remote(), timeout=0.5)
            except RayActorError:
                break
            if time.monotonic() >= deadline:
                pytest.fail("result buffer survived successful cleanup")
            time.sleep(0.05)
    finally:
        ray.shutdown()


@pytest.mark.real_ray
def test_real_ray_non_detached_buffer_dies_with_owner() -> None:
    import ray
    from ray.exceptions import RayActorError, RayTaskError

    ray.init(ignore_reinit_error=True, num_cpus=2)
    try:
        owner = ray.remote(num_cpus=0.1)(_ResultBufferOwner).remote()
        child = ray.get(owner.spawn.remote())
        assert validate_result_buffer_ack(ray.get(child.ready.remote()), state="ready")

        ray.kill(owner, no_restart=True)
        deadline = time.monotonic() + 10
        while True:
            try:
                ray.get(child.ready.remote(), timeout=0.5)
            except (RayActorError, RayTaskError):
                break
            if time.monotonic() >= deadline:
                pytest.fail("non-detached result buffer survived owner death")
            time.sleep(0.05)
    finally:
        ray.shutdown()
