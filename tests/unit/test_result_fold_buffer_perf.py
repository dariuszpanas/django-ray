"""Performance and memory regression tests for WorkflowMapResultFold and WorkflowMapResultBuffer."""

from __future__ import annotations

from typing import Any

import pytest

from django_ray.runtime.result_buffer import (
    ResultBufferOverflowError,
    ResultBufferProtocolError,
    WorkflowMapResultBuffer,
    validate_result_buffer_ack,
)
from django_ray.runtime.result_fold import (
    ResultFoldOverflowError,
    ResultFoldProtocolError,
    WorkflowMapResultFold,
    validate_result_fold_ack,
)


def sum_items(acc: int, item: int) -> int:
    return acc + item


def append_in_place(acc: list[int], item: int) -> list[int]:
    acc.append(item)
    return acc


def append_and_fail_on_value(acc: list[int], item: int, *, fail_value: int = 2) -> list[int]:
    acc.append(item)
    if item == fail_value:
        raise RuntimeError(f"boom at {item}")
    return acc


def concat_strings(acc: str, item: str) -> str:
    return acc + item


def test_result_fold_avoids_repeated_unpickle_and_repickle_on_primitive_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ray.cloudpickle as cloudpickle

    fold = WorkflowMapResultFold(
        500,
        4,
        16 * 1024,
        f"{__name__}.sum_items",
        False,
        (),
        {},
        0,
    )
    real_dumps = cloudpickle.dumps
    real_loads = cloudpickle.loads
    dumps_calls = 0
    loads_calls = 0

    def counting_dumps(*args: Any, **kwargs: Any) -> bytes:
        nonlocal dumps_calls
        dumps_calls += 1
        return real_dumps(*args, **kwargs)

    def counting_loads(*args: Any, **kwargs: Any) -> Any:
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(cloudpickle, "dumps", counting_dumps)
    monkeypatch.setattr(cloudpickle, "loads", counting_loads)

    for index in range(500):
        ack = fold.append(index, index + 1)
        assert ack["released_credits"] == 1
    result, final_ack = fold.finalize(500)

    assert result == sum(range(1, 501))
    assert validate_result_fold_ack(final_ack, state="finalized", expected_items=500)
    assert loads_calls == 0
    # Only the single final exact measurement on finalize() (never 500x per item).
    assert dumps_calls <= 2


def test_result_fold_growing_list_uses_amortized_dumps_not_quadratic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ray.cloudpickle as cloudpickle

    fold = WorkflowMapResultFold(
        300,
        4,
        128 * 1024,
        f"{__name__}.append_in_place",
        False,
        (),
        {},
        [],
    )
    real_dumps = cloudpickle.dumps
    real_loads = cloudpickle.loads
    dumps_calls = 0
    loads_calls = 0

    def counting_dumps(*args: Any, **kwargs: Any) -> bytes:
        nonlocal dumps_calls
        dumps_calls += 1
        return real_dumps(*args, **kwargs)

    def counting_loads(*args: Any, **kwargs: Any) -> Any:
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(cloudpickle, "dumps", counting_dumps)
    monkeypatch.setattr(cloudpickle, "loads", counting_loads)

    for index in range(300):
        fold.append(index, index)
    result, final_ack = fold.finalize(300)

    assert result == list(range(300))
    assert final_ack["folded_items"] == 300
    assert loads_calls == 0
    # Amortized recalibration runs logarithmically instead of 300x O(N^2) re-pickles.
    assert dumps_calls < 30


def test_result_fold_exact_overflow_boundary_and_mutable_rollback() -> None:
    import ray.cloudpickle as cloudpickle

    # Verify conservative upper bound never causes false-positive overflow when
    # exact pickle size fits right at max_serialized_bytes.
    target = "a" * 100
    exact_size = len(cloudpickle.dumps(target, protocol=5))
    fold = WorkflowMapResultFold(
        2,
        2,
        exact_size,
        f"{__name__}.concat_strings",
        False,
        (),
        {},
        "",
    )
    fold.append(0, "a" * 50)
    fold.append(1, "a" * 50)
    value, ack = fold.finalize(2)
    assert value == target
    assert ack["retained_bytes"] == exact_size

    # Exceeding exact size by 1 byte still raises ResultFoldOverflowError.
    tight_fold = WorkflowMapResultFold(
        2,
        2,
        exact_size - 1,
        f"{__name__}.concat_strings",
        False,
        (),
        {},
        "",
    )
    tight_fold.append(0, "a" * 50)
    with pytest.raises(ResultFoldOverflowError, match="accumulator serialization"):
        tight_fold.append(1, "a" * 50)

    # Mutable in-place list rollback restores original list on reducer error.
    mutable_fold = WorkflowMapResultFold(
        4,
        4,
        4096,
        f"{__name__}.append_and_fail_on_value",
        False,
        (),
        {"fail_value": 2},
        [],
    )
    mutable_fold.append(0, 10)
    mutable_fold.append(2, 2)
    with pytest.raises(RuntimeError, match="boom at 2"):
        mutable_fold.append(1, 20)
    assert mutable_fold.folded_items == 1
    assert mutable_fold._accumulator == [10]


def test_result_fold_append_batch_folds_mixed_order_and_validates_shape() -> None:
    fold = WorkflowMapResultFold(
        5,
        4,
        4096,
        f"{__name__}.sum_items",
        False,
        (),
        {},
        0,
    )
    acks = fold.append_batch([(2, 30), (1, 20), (0, 10), (3, 40), (4, 50)])
    assert len(acks) == 5
    for ack in acks:
        validate_result_fold_ack(ack, state="folded")
    assert acks[2]["released_credits"] == 3
    value, final_ack = fold.finalize(5)
    assert value == 150
    assert final_ack["folded_items"] == 5

    with pytest.raises(ResultFoldProtocolError, match="after result-fold finalization"):
        fold.append_batch([(0, 1)])

    fresh = WorkflowMapResultFold(2, 2, 4096, f"{__name__}.sum_items", False, (), {}, 0)
    with pytest.raises(ResultFoldProtocolError, match="sequence of pairs"):
        fresh.append_batch("bad")  # type: ignore[arg-type]


def test_result_buffer_finalize_releases_serialized_bytes_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ray.cloudpickle as cloudpickle

    buf = WorkflowMapResultBuffer(max_items=20, max_serialized_bytes=64 * 1024)
    for index in range(20):
        buf.append(index, {"index": index, "blob": "x" * 256})
    peak_bytes = buf.retained_bytes
    assert peak_bytes > 4096

    real_loads = cloudpickle.loads
    observed_remaining_items: list[int] = []
    observed_remaining_bytes: list[int] = []

    def observing_loads(payload: bytes) -> Any:
        observed_remaining_items.append(len(buf._serialized))
        observed_remaining_bytes.append(buf.retained_bytes)
        return real_loads(payload)

    monkeypatch.setattr(cloudpickle, "loads", observing_loads)

    values, ack = buf.finalize(20)
    assert len(values) == 20
    assert values[0]["index"] == 0
    assert values[-1]["index"] == 19
    # Serialized entries were popped before deserializing each item -> monotonic drop to 0.
    assert observed_remaining_items == list(range(19, -1, -1))
    assert observed_remaining_bytes[0] < peak_bytes
    assert observed_remaining_bytes[-1] == 0
    assert all(
        observed_remaining_bytes[i] > observed_remaining_bytes[i + 1]
        for i in range(len(observed_remaining_bytes) - 1)
    )
    assert buf.retained_bytes == 0
    assert validate_result_buffer_ack(ack, state="finalized", expected_items=20)
    assert ack["retained_bytes"] == peak_bytes


def test_result_buffer_append_batch_is_atomic_on_overflow_and_duplicate() -> None:
    buf = WorkflowMapResultBuffer(max_items=5, max_serialized_bytes=256)
    acks = buf.append_batch([(0, "a"), (1, "b")])
    assert len(acks) == 2
    for index, ack in enumerate(acks):
        validate_result_buffer_ack(ack, state="retained", expected_index=index)
    retained_before = buf.retained_bytes

    # Overflow on second item in batch rolls back entire batch.
    with pytest.raises(ResultBufferOverflowError, match="max_serialized_bytes=256"):
        buf.append_batch([(2, "c"), (3, "x" * 1024)])
    assert len(buf._serialized) == 2
    assert buf.retained_bytes == retained_before

    # Duplicate index inside batch rejects cleanly without mutating state.
    with pytest.raises(ResultBufferProtocolError, match="appended twice"):
        buf.append_batch([(2, "c"), (2, "d")])
    assert len(buf._serialized) == 2

    values, ack = buf.finalize(2)
    assert values == ["a", "b"]
    assert ack["item_count"] == 2
