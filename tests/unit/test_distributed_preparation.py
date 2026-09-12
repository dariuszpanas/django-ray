"""Observe sender preparation without running Ray or application callables."""

from __future__ import annotations

import pickle
import sys
from contextlib import nullcontext
from typing import Any

import pytest

from django_ray import execution_codec as codec
from django_ray.runtime import distributed
from tests.unit.test_distributed_mocked import _strict_execution


def _square(value: int) -> int:
    return value * value


def _add(left: int, right: int) -> int:
    return left + right


class _SenderRay:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.consumed = 0
        self.cancelled: list[int] = []

    def options(self, **kwargs: Any):
        return self

    def remote(self, *args: Any) -> int:
        self.calls.append(args)
        return len(self.calls) - 1

    def get(self, refs: Any) -> Any:
        if isinstance(refs, list):
            self.consumed += len(refs)
            return refs
        self.consumed += 1
        return refs

    def wait(self, refs: list[int], *, num_returns: int):
        assert num_returns == 1
        return refs[-1:], refs[:-1]

    def cancel(self, ref: int, *, force: bool, recursive: bool) -> None:
        assert force is False and recursive is True
        self.cancelled.append(ref)


@pytest.fixture
def sender(monkeypatch: pytest.MonkeyPatch) -> _SenderRay:
    ray = _SenderRay()
    monkeypatch.setitem(sys.modules, "ray", ray)
    monkeypatch.setattr(distributed, "is_ray_available", lambda: True)
    monkeypatch.setattr(distributed, "_get_cached_remote", lambda _: ray)
    return ray


@pytest.mark.parametrize("helper", ["map", "starmap", "scatter"])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("window", [1, 3])
def test_preparation_tracks_the_window_with_exact_order_and_bindings(
    sender: _SenderRay, monkeypatch: pytest.MonkeyPatch, helper: str, strict: bool, window: int
) -> None:
    count = 17
    requests: list[int] = []
    hashed: list[bytes] = []
    encodings: list[object] = []
    pickled: list[object] = []
    original_request = distributed._nested_distributed_request
    original_digest = codec.nested_callable_digest
    original_encode = codec._encode_nested_request_for_protocols
    original_pickle = pickle.dumps

    def request(operation, serialized: bytes, index: int):
        requests.append(index)
        assert len(requests) - sender.consumed <= window
        assert len(requests) == len(sender.calls) + 1
        return original_request(operation, serialized, index)

    def digest(serialized: bytes) -> str:
        hashed.append(serialized)
        return original_digest(serialized)

    def encode(value, protocols) -> str:
        encodings.append(value)
        return original_encode(value, protocols)

    def serialize(value, *args, **kwargs) -> bytes:
        pickled.append(value)
        if helper == "scatter":
            assert len(pickled) - sender.consumed <= window
        return original_pickle(value, *args, **kwargs)

    monkeypatch.setattr(distributed, "_nested_distributed_request", request)
    monkeypatch.setattr(codec, "nested_callable_digest", digest)
    monkeypatch.setattr(codec, "_encode_nested_request_for_protocols", encode)
    monkeypatch.setattr(pickle, "dumps", serialize)
    with _strict_execution() if strict else nullcontext():
        if helper == "map":
            result = distributed.parallel_map(_square, list(range(count)), max_concurrency=window)
        elif helper == "starmap":
            result = distributed.parallel_starmap(
                _add, [(i, 1) for i in range(count)], max_concurrency=window
            )
        else:
            tasks = [(_square, (i,), {}) if i % 2 else (_add, (i, 1), {}) for i in range(count)]
            result = distributed.scatter_gather(tasks, max_concurrency=window)

    assert result == list(range(count))
    assert sender.cancelled == []
    assert len(encodings) == int(strict)
    assert len(pickled) == (count if helper == "scatter" else 1)
    assert len(hashed) == ((count if helper == "scatter" else 1) if strict else 0)
    assert requests == (list(range(count)) if strict else [])
    if strict:
        decoded = [codec.decode_nested_execution_request(call[-10]) for call in sender.calls]
        assert [request.boundary_identity.item_index for request in decoded] == list(range(count))
        assert len({request.boundary_identity.operation_id for request in decoded}) == 1
        assert all(request.outer_identity.task_id == "task-41" for request in decoded)
        for request, call in zip(decoded, sender.calls, strict=True):
            assert request.callable_binding == original_digest(call[0])
    else:
        assert all(len(call) == (2 if helper == "starmap" else 3) for call in sender.calls)


@pytest.mark.parametrize("strict", [False, True])
def test_later_scatter_pickle_failure_cleans_owned_sibling_without_replay(
    sender: _SenderRay, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    original_pickle = pickle.dumps
    failure = pickle.PicklingError("late callable cannot be serialized")
    prepared: list[object] = []

    def serialize(value) -> bytes:
        prepared.append(value)
        if value is _add:
            raise failure
        return original_pickle(value)

    monkeypatch.setattr(pickle, "dumps", serialize)
    with (
        _strict_execution() if strict else nullcontext(),
        pytest.raises(pickle.PicklingError) as caught,
    ):
        distributed.scatter_gather(
            [(_square, (1,), {}), (_square, (2,), {}), (_add, (3, 4), {}), (_square, (5,), {})],
            max_concurrency=2,
        )
    assert caught.value is failure
    assert prepared == [_square, _square, _add]
    assert len(sender.calls) == 2
    assert sender.consumed == 1
    assert sender.cancelled == [0]


@pytest.mark.parametrize("helper", ["starmap", "scatter"])
def test_shape_validation_stays_eager_before_any_submission(
    sender: _SenderRay, helper: str
) -> None:
    with pytest.raises(TypeError):
        if helper == "starmap":
            distributed.parallel_starmap(_add, [(1, 2), [3, 4]], max_concurrency=1)  # type: ignore[list-item]
        else:
            distributed.scatter_gather([(_square, (1,), {}), (_add, [3, 4], {})], max_concurrency=1)  # type: ignore[list-item]
    assert sender.calls == []


@pytest.mark.parametrize("window", [0, -1, True, 1.5, "2"])
def test_scatter_rejects_invalid_window_before_submission(sender: _SenderRay, window: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="max_concurrency"):
        distributed.scatter_gather([(_square, (1,), {})], max_concurrency=window)
    assert sender.calls == []


def test_scatter_window_is_optional_and_keyword_only() -> None:
    import inspect

    parameter = inspect.signature(distributed.scatter_gather).parameters["max_concurrency"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_scatter_window_preserves_sequential_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(distributed, "is_ray_available", lambda: False)
    assert distributed.scatter_gather(
        [(_square, (3,), {}), (_add, (4, 5), {})], max_concurrency=1
    ) == [9, 9]


def test_later_map_request_failure_uses_owned_cleanup(
    sender: _SenderRay, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = distributed._nested_distributed_request
    failure = codec.NestedExecutionRequestEncodeError()

    def request(operation, serialized: bytes, index: int):
        if index == 2:
            raise failure
        return original(operation, serialized, index)

    monkeypatch.setattr(distributed, "_nested_distributed_request", request)
    with _strict_execution(), pytest.raises(codec.NestedExecutionRequestEncodeError) as caught:
        distributed.parallel_map(_square, list(range(5)), max_concurrency=2)
    assert caught.value is failure
    assert len(sender.calls) == 2
    assert sender.consumed == 1
    assert sender.cancelled == [0]


def test_remote_calls_do_not_prepare_out_of_range_indexes() -> None:
    prepared: list[int] = []
    calls = distributed._RemoteCalls(2, lambda index: (prepared.append(index),))
    for index in (-1, 2):
        with pytest.raises(IndexError):
            calls[index]
    assert prepared == []
