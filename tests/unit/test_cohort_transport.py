from __future__ import annotations

import builtins
import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from django_ray.execution_codec import (
    _EXPECTED_OUTPUT_PREVIEW_CALLABLE_PATH_UNSET,
    ExecutionRequestDecodeError,
    ExecutionRequestEncodeError,
    NestedExecutionRequestEncodeError,
    NestedExecutionRequestRejected,
    decode_execution_request,
    decode_nested_execution_request,
    encode_execution_request,
    encode_nested_execution_request,
)
from django_ray.execution_protocol import (
    COHORT_EXECUTION_PROTOCOL_VERSION,
    EXECUTION_PROTOCOL_VERSION,
    SUPPORTED_EXECUTION_PROTOCOL_RANGE,
)
from django_ray.target.cohort_contract import (
    CohortContractError,
    cohort_execution_contract_digest,
    cohort_leaf_contract_digest,
    derive_cohort_leaf_contract,
    encode_cohort_execution_contract,
    encode_cohort_leaf_contract,
)
from django_ray.target.cohort_transport import (
    cohort_execution_request_digest,
    decode_cohort_execution_request,
    decode_cohort_nested_execution_request,
    encode_cohort_execution_request,
    encode_cohort_nested_execution_request,
)
from tests.unit.test_cohort_contract import contract
from tests.unit.test_execution_codec import _inline_request, _workflow_nested_request


def outer_request():
    claim = contract()
    return (
        replace(
            _inline_request(claim.identity),
            execution_protocol_version=COHORT_EXECUTION_PROTOCOL_VERSION,
            cohort_contract_json=encode_cohort_execution_contract(claim),
        ),
        claim,
    )


def nested_request():
    claim = derive_cohort_leaf_contract(contract())
    return (
        replace(
            _workflow_nested_request(claim.identity),
            execution_protocol_version=COHORT_EXECUTION_PROTOCOL_VERSION,
            cohort_leaf_contract_json=encode_cohort_leaf_contract(claim),
        ),
        claim,
    )


def nested_bindings(request, claim):
    return {
        "expected_outer_identity": request.outer_identity,
        "expected_cohort_leaf_digest": cohort_leaf_contract_digest(claim),
        "expected_outer_contract_digest": claim.outer_contract_digest,
        "expected_boundary_kind": request.boundary_kind,
        "expected_boundary_identity": request.boundary_identity,
        "expected_callable_binding_kind": request.callable_binding_kind,
        "expected_callable_binding": request.callable_binding,
        "expected_output_preview_callable_path": request.output_preview_callable_path,
        "expected_runtime_env_plan_digest": request.runtime_env_plan_digest,
        "expected_runtime_env_transport_digest": request.runtime_env_transport_digest,
    }


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_outer_round_trip_preserves_opaque_inputs_and_claim_before_any_application_import(
    monkeypatch,
):
    request, claim = outer_request()
    original = builtins.__import__

    def no_application(name, *args, **kwargs):
        if name == "django" or name.startswith(("django.", "ray", "testproject.")):
            raise AssertionError("transport cannot enter application or Ray")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_application)
    wire = encode_cohort_execution_request(request)
    decoded, decoded_claim = decode_cohort_execution_request(
        wire,
        expected_identity=claim.identity,
        expected_cohort_contract_digest=cohort_execution_contract_digest(claim),
        expected_request_digest=cohort_execution_request_digest(request),
    )
    assert decoded == request
    assert decoded_claim == claim
    assert decoded.serialized_args == "[20,22]"


def test_nested_round_trip_fences_operation_and_compact_parent_claim():
    request, claim = nested_request()
    assert decode_cohort_nested_execution_request(
        encode_cohort_nested_execution_request(request), **nested_bindings(request, claim)
    ) == (request, claim)


def test_ordinary_codecs_and_worker_range_do_not_activate_cohort_execution():
    assert EXECUTION_PROTOCOL_VERSION == 1
    assert not SUPPORTED_EXECUTION_PROTOCOL_RANGE.supports(COHORT_EXECUTION_PROTOCOL_VERSION)
    request, _claim = outer_request()
    nested, _leaf = nested_request()
    with pytest.raises(ExecutionRequestEncodeError):
        encode_execution_request(request)
    with pytest.raises(ExecutionRequestDecodeError):
        decode_execution_request(encode_cohort_execution_request(request))
    with pytest.raises(NestedExecutionRequestEncodeError):
        encode_nested_execution_request(nested)
    with pytest.raises(NestedExecutionRequestRejected):
        decode_nested_execution_request(encode_cohort_nested_execution_request(nested))


@pytest.mark.parametrize("value", [None, "", "{}", "not-json"])
def test_cohort_wire_requires_canonical_outer_and_leaf_contracts(value):
    request, _claim = outer_request()
    nested, _leaf = nested_request()
    with pytest.raises(ExecutionRequestEncodeError):
        encode_cohort_execution_request(replace(request, cohort_contract_json=value))
    with pytest.raises(NestedExecutionRequestEncodeError):
        encode_cohort_nested_execution_request(replace(nested, cohort_leaf_contract_json=value))


def test_contracts_cannot_be_inserted_into_released_protocol_requests():
    request, _claim = outer_request()
    nested, _leaf = nested_request()
    with pytest.raises(ExecutionRequestEncodeError):
        encode_execution_request(replace(request, execution_protocol_version=1))
    with pytest.raises(NestedExecutionRequestEncodeError):
        encode_nested_execution_request(replace(nested, execution_protocol_version=1))


def test_outer_contract_cannot_disagree_with_application_request_identity():
    request, claim = outer_request()
    other = replace(claim, identity=replace(claim.identity, execution_generation=99))
    with pytest.raises(ExecutionRequestEncodeError):
        encode_cohort_execution_request(
            replace(request, cohort_contract_json=encode_cohort_execution_contract(other))
        )


def test_leaf_contract_cannot_disagree_with_nested_request_identity():
    request, claim = nested_request()
    other = replace(claim, identity=replace(claim.identity, execution_generation=99))
    with pytest.raises(NestedExecutionRequestEncodeError):
        encode_cohort_nested_execution_request(
            replace(request, cohort_leaf_contract_json=encode_cohort_leaf_contract(other))
        )


@pytest.mark.parametrize("replacement", [None, "", "sha256:" + "b" * 64])
def test_outer_independent_digest_cannot_be_missing_or_replaced(replacement):
    request, claim = outer_request()
    with pytest.raises(CohortContractError):
        decode_cohort_execution_request(
            encode_cohort_execution_request(request),
            expected_identity=claim.identity,
            expected_cohort_contract_digest=replacement,
            expected_request_digest=cohort_execution_request_digest(request),
        )


@pytest.mark.parametrize(
    "field",
    [
        "expected_outer_identity",
        "expected_cohort_leaf_digest",
        "expected_outer_contract_digest",
        "expected_boundary_kind",
        "expected_boundary_identity",
        "expected_callable_binding_kind",
        "expected_callable_binding",
        "expected_runtime_env_plan_digest",
        "expected_runtime_env_transport_digest",
    ],
)
def test_nested_independent_controls_cannot_be_disabled(field):
    request, claim = nested_request()
    controls = nested_bindings(request, claim)
    controls[field] = None
    with pytest.raises(CohortContractError):
        decode_cohort_nested_execution_request(
            encode_cohort_nested_execution_request(request), **controls
        )


@pytest.mark.parametrize("preview", [_EXPECTED_OUTPUT_PREVIEW_CALLABLE_PATH_UNSET, False, object()])
def test_nested_preview_control_cannot_use_optional_decoder_bypass(preview):
    request, claim = nested_request()
    controls = nested_bindings(request, claim)
    controls["expected_output_preview_callable_path"] = preview
    with pytest.raises(CohortContractError):
        decode_cohort_nested_execution_request(
            encode_cohort_nested_execution_request(request), **controls
        )


def test_mutating_leaf_package_with_same_echoed_parent_digest_is_rejected():
    request, claim = nested_request()
    changed_claim = replace(claim, expected_django_ray_version="0.4.0")
    changed_request = replace(
        request, cohort_leaf_contract_json=encode_cohort_leaf_contract(changed_claim)
    )
    with pytest.raises(CohortContractError):
        decode_cohort_nested_execution_request(
            encode_cohort_nested_execution_request(changed_request),
            **nested_bindings(request, claim),
        )


def test_nested_operation_cannot_be_substituted_with_same_valid_cohort_claim():
    request, claim = nested_request()
    changed = replace(
        request, boundary_identity=replace(request.boundary_identity, node_id="other")
    )
    with pytest.raises(NestedExecutionRequestRejected):
        decode_cohort_nested_execution_request(
            encode_cohort_nested_execution_request(changed), **nested_bindings(request, claim)
        )


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("change", ["extra", "omit", "duplicate", "noncanonical"])
def test_cohort_request_envelopes_remain_strict(nested, change):
    request, claim = nested_request() if nested else outer_request()
    wire = (
        encode_cohort_nested_execution_request(request)
        if nested
        else encode_cohort_execution_request(request)
    )
    value = json.loads(wire)
    field = "cohort_leaf_contract_json" if nested else "cohort_contract_json"
    if change == "extra":
        value["unrecognized"] = True
        wire = canonical(value)
    elif change == "omit":
        del value[field]
        wire = canonical(value)
    elif change == "duplicate":
        wire = '{"execution_protocol_version":3,' + wire[1:]
    else:
        wire = json.dumps(value, indent=2)
    if nested:
        with pytest.raises(NestedExecutionRequestRejected):
            decode_cohort_nested_execution_request(wire, **nested_bindings(request, claim))
    else:
        with pytest.raises(ExecutionRequestDecodeError):
            decode_cohort_execution_request(
                wire,
                expected_identity=claim.identity,
                expected_cohort_contract_digest=cohort_execution_contract_digest(claim),
                expected_request_digest=cohort_execution_request_digest(request),
            )


@pytest.mark.parametrize("field", ["callable_path", "serialized_args", "serialized_kwargs"])
def test_whole_request_digest_binds_application_body_beyond_cohort_claim(field):
    request, claim = outer_request()
    changed = replace(
        request, **{field: "testproject.tasks.other" if field == "callable_path" else "null"}
    )
    with pytest.raises(CohortContractError):
        decode_cohort_execution_request(
            encode_cohort_execution_request(changed),
            expected_identity=claim.identity,
            expected_cohort_contract_digest=cohort_execution_contract_digest(claim),
            expected_request_digest=cohort_execution_request_digest(request),
        )


def test_nested_transport_is_django_free_in_a_fresh_remote_process():
    request, claim = nested_request()
    controls = nested_bindings(request, claim)
    controls["expected_outer_identity"] = asdict(request.outer_identity)
    controls["expected_boundary_identity"] = asdict(request.boundary_identity)
    payload = json.dumps(
        {
            "wire": encode_cohort_nested_execution_request(request),
            "controls": controls,
        }
    )
    root = Path(__file__).resolve().parents[2]
    script = """
import builtins, json, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {'django', 'ray', 'testproject'}:
        raise AssertionError('forbidden framework or application import')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
from django_ray.execution_codec import (
    ExecutionIdentity, NestedWorkflowBoundaryIdentity,
    NestedExecutionBoundaryKind, NestedCallableBindingKind,
)
from django_ray.target.cohort_transport import decode_cohort_nested_execution_request
payload = json.load(sys.stdin)
controls = payload['controls']
controls['expected_outer_identity'] = ExecutionIdentity(**controls['expected_outer_identity'])
controls['expected_boundary_identity'] = NestedWorkflowBoundaryIdentity(**controls['expected_boundary_identity'])
controls['expected_boundary_kind'] = NestedExecutionBoundaryKind(controls['expected_boundary_kind'])
controls['expected_callable_binding_kind'] = NestedCallableBindingKind(controls['expected_callable_binding_kind'])
request, claim = decode_cohort_nested_execution_request(payload['wire'], **controls)
assert request.outer_identity == claim.identity == controls['expected_outer_identity']
assert 'django' not in sys.modules and 'ray' not in sys.modules
assert 'django_ray.workflow.plans' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root / "src")],
        cwd=root,
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
