"""Canonical-equivalence and bounds for operation-scoped request preparation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from django_ray import execution_codec as codec
from django_ray.target.cohort_contract import (
    derive_cohort_leaf_contract,
    encode_cohort_leaf_contract,
)
from tests.unit.test_cohort_contract import contract
from tests.unit.test_execution_codec import _workflow_nested_request


def _current_workflow_request(identity):
    parent = replace(contract(), identity=identity)
    return replace(
        _workflow_nested_request(identity),
        execution_protocol_version=3,
        cohort_leaf_contract_json=encode_cohort_leaf_contract(derive_cohort_leaf_contract(parent)),
    )


def _request(kind=codec.NestedExecutionBoundaryKind.DISTRIBUTED_MAP):
    identity = codec.ExecutionIdentity(41, 'task-雪-"item_index":0', 2, 7)
    return replace(
        _current_workflow_request(identity),
        boundary_kind=kind,
        boundary_identity=codec.NestedDistributedBoundaryIdentity("operation-41", 0),
        callable_binding_kind=codec.NestedCallableBindingKind.DIGEST,
        callable_binding="sha256:" + "a" * 64,
    )


@pytest.mark.parametrize(
    "kind",
    [
        codec.NestedExecutionBoundaryKind.DISTRIBUTED_MAP,
        codec.NestedExecutionBoundaryKind.DISTRIBUTED_STARMAP,
        codec.NestedExecutionBoundaryKind.DISTRIBUTED_SCATTER,
    ],
)
@pytest.mark.parametrize("index", [0, 9, 10, (1 << 63) - 1])
@pytest.mark.parametrize("digest", ["sha256:" + "a" * 64, "sha256:" + "f" * 64])
def test_prepared_requests_are_byte_identical_to_the_full_encoder(
    kind, index: int, digest: str
) -> None:
    prototype = _request(kind)
    prepared = codec._prepare_nested_distributed_request(prototype)
    expected = replace(
        prototype,
        boundary_identity=codec.NestedDistributedBoundaryIdentity("operation-41", index),
        callable_binding=digest,
    )
    actual = prepared.encode(item_index=index, callable_binding=digest)
    assert actual == codec.encode_nested_execution_request(expected)
    assert codec.decode_nested_execution_request(actual) == expected


@pytest.mark.parametrize("index", [-1, 1 << 63, True, 1.0, "1", None])
def test_prepared_counter_rejects_out_of_range_and_noninteger_values(index) -> None:
    request = _request()
    prepared = codec._prepare_nested_distributed_request(request)
    with pytest.raises(
        codec.NestedExecutionRequestEncodeError, match="nested execution request is invalid"
    ):
        prepared.encode(item_index=index, callable_binding=request.callable_binding)


@pytest.mark.parametrize("digest", [None, 42, "a" * 64, "sha256:" + "A" * 64, 'sha256:"injected"'])
def test_prepared_binding_rejects_noncanonical_or_injectable_values(digest) -> None:
    prepared = codec._prepare_nested_distributed_request(_request())
    with pytest.raises(
        codec.NestedExecutionRequestEncodeError, match="nested execution request is invalid"
    ):
        prepared.encode(item_index=0, callable_binding=digest)


def test_prepared_request_detaches_mutable_runtime_env_context() -> None:
    request = _request()
    canonical = codec.encode_nested_execution_request(request)
    prepared = codec._prepare_nested_distributed_request(request)
    request.runtime_env_plan_identity.clear()
    actual = prepared.encode(item_index=0, callable_binding=request.callable_binding)
    assert actual == canonical
    assert codec.decode_nested_execution_request(actual).runtime_env_plan_identity


def test_final_utf8_byte_limit_includes_growing_item_index(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    prepared = codec._prepare_nested_distributed_request(request)
    canonical = codec.encode_nested_execution_request(request)
    # The task ID is multibyte and contains a JSON-looking string. Neither
    # character counts nor replacing a matching substring would be sufficient.
    monkeypatch.setattr(codec, "NESTED_EXECUTION_REQUEST_MAX_BYTES", len(canonical.encode("utf-8")))
    assert prepared.encode(item_index=0, callable_binding=request.callable_binding) == canonical
    with pytest.raises(codec.NestedExecutionRequestEncodeError):
        prepared.encode(item_index=10, callable_binding=request.callable_binding)


def test_preparation_retains_full_prototype_validation() -> None:
    request = _request()
    request.runtime_env_plan_identity.clear()
    with pytest.raises(codec.NestedExecutionRequestEncodeError):
        codec._prepare_nested_distributed_request(request)


@pytest.mark.parametrize("workflow", [False, True])
def test_prepared_encoder_rejects_other_callable_boundaries(workflow: bool) -> None:
    request = _request()
    if workflow:
        request = _current_workflow_request(request.outer_identity)
    else:
        request = replace(
            request,
            callable_binding_kind=codec.NestedCallableBindingKind.PATH,
            callable_binding="testproject.tasks.add_numbers",
        )
    with pytest.raises(codec.NestedExecutionRequestEncodeError):
        codec._prepare_nested_distributed_request(request)


def test_prepared_encoding_does_not_revisit_invariant_json(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    prepared = codec._prepare_nested_distributed_request(request)

    def unexpected(*args, **kwargs):
        raise AssertionError("invariant JSON was revisited")

    monkeypatch.setattr(codec, "encode_nested_execution_request", unexpected)
    monkeypatch.setattr(codec, "decode_nested_execution_request", unexpected)
    monkeypatch.setattr(codec.json, "dumps", unexpected)
    for index in range(10):
        assert prepared.encode(item_index=index, callable_binding=request.callable_binding)
