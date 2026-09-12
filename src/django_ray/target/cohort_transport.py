"""Private protocol-3 request adapters with independent cohort digest bindings.

The ordinary producer and worker still advertise protocol 1. These adapters
reuse its bounded application and nested codecs while requiring a canonical
cohort claim for protocol 3; they cannot execute an application or grant a
claim. A remote adapter must obtain expected identity/digests independently of
the serialized request, and run the point guard before any application entry.
"""

from __future__ import annotations

import hashlib
from typing import cast

from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    NestedCallableBindingKind,
    NestedDistributedBoundaryIdentity,
    NestedExecutionBoundaryKind,
    NestedExecutionRequest,
    NestedWorkflowBoundaryIdentity,
    _encode_execution_request_for_protocols,
    _encode_nested_request_for_protocols,
    decode_execution_request,
    decode_nested_execution_request,
    is_valid_execution_identity,
)
from django_ray.execution_protocol import COHORT_EXECUTION_PROTOCOL_VERSION, ExecutionProtocolRange
from django_ray.target.cohort_contract import (
    CohortContractError,
    CohortContractRejection,
    CohortExecutionContract,
    CohortLeafContract,
    decode_cohort_execution_contract,
    decode_cohort_leaf_contract,
)

_PROTOCOLS = ExecutionProtocolRange(
    COHORT_EXECUTION_PROTOCOL_VERSION, COHORT_EXECUTION_PROTOCOL_VERSION
)
_REQUEST_DOMAIN = b"django-ray/cohort-execution-request/v3\x00"


def _require_expected_digest(value: object) -> None:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH) from None


def encode_cohort_execution_request(request: ExecutionRequest) -> str:
    """Encode a bounded application request with a mandatory protocol-3 claim."""
    return _encode_execution_request_for_protocols(request, _PROTOCOLS)


def _request_digest(serialized: str) -> str:
    digest = hashlib.sha256(_REQUEST_DOMAIN)
    digest.update(serialized.encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def cohort_execution_request_digest(request: ExecutionRequest) -> str:
    """Bind the complete request, including the application callable and inputs."""
    return _request_digest(encode_cohort_execution_request(request))


def decode_cohort_execution_request(
    serialized: object,
    *,
    expected_identity: ExecutionIdentity,
    expected_cohort_contract_digest: str,
    expected_request_digest: str,
) -> tuple[ExecutionRequest, CohortExecutionContract]:
    """Validate the request against the independent exact claim identity/digest."""
    _require_expected_digest(expected_cohort_contract_digest)
    _require_expected_digest(expected_request_digest)
    if not is_valid_execution_identity(expected_identity):
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH) from None
    request = decode_execution_request(
        serialized,
        supported_protocols=_PROTOCOLS,
        expected_identity=expected_identity,
        expected_execution_protocol_version=COHORT_EXECUTION_PROTOCOL_VERSION,
    )
    if _request_digest(cast(str, serialized)) != expected_request_digest:
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH) from None
    contract = decode_cohort_execution_contract(
        request.cohort_contract_json,
        expected_identity=expected_identity,
        expected_contract_digest=expected_cohort_contract_digest,
    )
    return request, contract


def encode_cohort_nested_execution_request(request: NestedExecutionRequest) -> str:
    """Encode a nested request with a mandatory compact inherited cohort claim."""
    return _encode_nested_request_for_protocols(request, _PROTOCOLS)


def decode_cohort_nested_execution_request(
    serialized: object,
    *,
    expected_outer_identity: ExecutionIdentity,
    expected_cohort_leaf_digest: str,
    expected_outer_contract_digest: str,
    expected_boundary_kind: NestedExecutionBoundaryKind,
    expected_boundary_identity: NestedWorkflowBoundaryIdentity | NestedDistributedBoundaryIdentity,
    expected_callable_binding_kind: NestedCallableBindingKind,
    expected_callable_binding: str,
    expected_output_preview_callable_path: str | None,
    expected_runtime_env_plan_digest: str,
    expected_runtime_env_transport_digest: str,
) -> tuple[NestedExecutionRequest, CohortLeafContract]:
    """Fence both the operation and its independently derived parent claim."""
    _require_expected_digest(expected_cohort_leaf_digest)
    _require_expected_digest(expected_outer_contract_digest)
    _require_expected_digest(expected_runtime_env_plan_digest)
    _require_expected_digest(expected_runtime_env_transport_digest)
    if (
        not is_valid_execution_identity(expected_outer_identity)
        or type(expected_boundary_kind) is not NestedExecutionBoundaryKind
        or type(expected_boundary_identity)
        not in {NestedWorkflowBoundaryIdentity, NestedDistributedBoundaryIdentity}
        or type(expected_callable_binding_kind) is not NestedCallableBindingKind
        or type(expected_callable_binding) is not str
        or not expected_callable_binding
        or (
            expected_output_preview_callable_path is not None
            and type(expected_output_preview_callable_path) is not str
        )
    ):
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH) from None
    request = decode_nested_execution_request(
        serialized,
        supported_protocols=_PROTOCOLS,
        expected_outer_identity=expected_outer_identity,
        expected_execution_protocol_version=COHORT_EXECUTION_PROTOCOL_VERSION,
        expected_boundary_kind=expected_boundary_kind,
        expected_boundary_identity=expected_boundary_identity,
        expected_callable_binding_kind=expected_callable_binding_kind,
        expected_callable_binding=expected_callable_binding,
        expected_output_preview_callable_path=expected_output_preview_callable_path,
        expected_runtime_env_plan_digest=expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest=expected_runtime_env_transport_digest,
    )
    contract = decode_cohort_leaf_contract(
        request.cohort_leaf_contract_json,
        expected_identity=expected_outer_identity,
        expected_contract_digest=expected_cohort_leaf_digest,
        expected_outer_contract_digest=expected_outer_contract_digest,
    )
    return request, contract
