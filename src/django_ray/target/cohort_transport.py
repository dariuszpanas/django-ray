"""Private protocol-3 request adapters with independent cohort digest bindings.

The ordinary producer and worker still advertise protocol 1. These adapters
reuse its bounded application and nested codecs while requiring a canonical
cohort claim for protocol 3; they cannot execute an application or grant a
claim. A remote adapter must obtain expected identity/digests independently of
the serialized request, and run the point guard before any application entry.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import cast

from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    NestedCallableBindingKind,
    NestedDistributedBoundaryIdentity,
    NestedExecutionBoundaryKind,
    NestedExecutionRequest,
    NestedWorkflowBoundaryIdentity,
    _bounded_json_dumps,
    _bounded_json_loads,
    _encode_execution_completion_for_protocols,
    _encode_execution_request_for_protocols,
    _encode_nested_request_for_protocols,
    decode_execution_completion,
    decode_execution_request,
    decode_nested_execution_request,
    is_valid_execution_identity,
)
from django_ray.execution_protocol import COHORT_EXECUTION_PROTOCOL_VERSION, ExecutionProtocolRange
from django_ray.target.cohort_contract import (
    COHORT_CONTRACT_MAX_BYTES,
    CohortContractError,
    CohortContractRejection,
    CohortExecutionContract,
    CohortLeafContract,
    _load,
    cohort_execution_contract_digest,
    decode_cohort_execution_contract,
    decode_cohort_leaf_contract,
    encode_cohort_execution_contract,
)
from django_ray.target.cohort_sync import (
    CohortSyncContract,
    cohort_sync_contract_digest,
    decode_cohort_sync_contract,
    encode_cohort_sync_contract,
)

_PROTOCOLS = ExecutionProtocolRange(
    COHORT_EXECUTION_PROTOCOL_VERSION, COHORT_EXECUTION_PROTOCOL_VERSION
)
_REQUEST_DOMAIN = b"django-ray/cohort-execution-request/v3\x00"


def decode_cohort_outer_contract(
    serialized, *, expected_identity=None, expected_contract_digest=None
):
    """Decode the closed Ray/Sync union, never infer a runner from application data."""
    value = _load(serialized, leaf=False, max_bytes=COHORT_CONTRACT_MAX_BYTES)
    decoder = (
        decode_cohort_sync_contract
        if value.get("schema") == "django-ray.cohort-sync-contract"
        else decode_cohort_execution_contract
    )
    return decoder(
        serialized,
        expected_identity=expected_identity,
        expected_contract_digest=expected_contract_digest,
    )


@dataclass(frozen=True, slots=True)
class PreparedCohortExecution:
    """Canonical data retained by its owner; constructing it grants no authority."""

    identity: ExecutionIdentity
    request_json: str = field(repr=False)
    request_digest: str
    contract_digest: str


def prepare_cohort_execution(task, *, contract, transport=None) -> PreparedCohortExecution:
    """Prepare opaque inputs from the current task snapshot outside claim locks."""
    from django_ray.conf.settings import get_settings
    from django_ray.runtime.runtime_env import runtime_env_for_execution

    identity = ExecutionIdentity(
        int(task.pk), str(task.task_id), int(task.attempt_number), int(task.execution_generation)
    )
    if task.execution_protocol_version != 3 or contract.identity != identity:
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH)
    return _prepare_cohort_execution_from_environment(
        task,
        contract=contract,
        transport=transport,
        environment=runtime_env_for_execution(task),
        trust_identity=get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
    )


def _prepare_cohort_execution_from_environment(
    task, *, contract, transport, environment, trust_identity
) -> PreparedCohortExecution:
    """Prepare from the owner's captured environment without reading ambient settings."""
    from django_ray.workflow.plans import runtime_env_plan_identity

    identity = ExecutionIdentity(
        int(task.pk), str(task.task_id), int(task.attempt_number), int(task.execution_generation)
    )
    if task.execution_protocol_version != 3 or contract.identity != identity:
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH)
    if type(contract) is CohortSyncContract:
        contract_json = encode_cohort_sync_contract(contract)
        contract_digest = cohort_sync_contract_digest(contract)
        if transport is not None:
            raise CohortContractError(CohortContractRejection.BINDING_MISMATCH)
    else:
        contract_json = encode_cohort_execution_contract(contract)
        contract_digest = cohort_execution_contract_digest(contract)
    plan = runtime_env_plan_identity(environment, trust_identity=trust_identity)
    request = ExecutionRequest(
        identity,
        3,
        task.callable_path,
        2 if task.input_reference else 1,
        task.args_json,
        task.kwargs_json,
        task.input_reference,
        environment.profile,
        environment.digest,
        plan.as_transport_dict(),
        transport,
        contract_json,
    )
    encoded = encode_cohort_execution_request(request)
    return PreparedCohortExecution(identity, encoded, _request_digest(encoded), contract_digest)


def validate_prepared_cohort_execution(prepared, *, task=None):
    if type(prepared) is not PreparedCohortExecution:
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH)
    request, contract = decode_cohort_execution_request(
        prepared.request_json,
        expected_identity=prepared.identity,
        expected_request_digest=prepared.request_digest,
        expected_cohort_contract_digest=prepared.contract_digest,
    )
    if task is not None and (
        task.execution_protocol_version != 3
        or request.identity
        != ExecutionIdentity(
            int(task.pk),
            str(task.task_id),
            int(task.attempt_number),
            int(task.execution_generation),
        )
        or request.callable_path != task.callable_path
        or request.serialized_args != task.args_json
        or request.serialized_kwargs != task.kwargs_json
        or request.input_reference != task.input_reference
    ):
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH)
    return request, contract


_REFUSALS = frozenset(
    {
        "package_mismatch",
        "runtime_mismatch",
        "session_mismatch",
        "membership_mismatch",
        "observation_unavailable",
        "nested_refusal",
    }
)


@dataclass(frozen=True, slots=True)
class CohortExecutionResult:
    identity: ExecutionIdentity
    request_digest: str
    contract_digest: str
    completion_json: str | None = field(default=None, repr=False)
    refusal: str | None = None
    boundary: str = "outer"
    application_invoked: bool | None = None


def encode_cohort_execution_result(result: CohortExecutionResult) -> str:
    """Bound exactly one outcome; raw transport failures are not encoded refusals."""
    if type(result) is not CohortExecutionResult or not is_valid_execution_identity(
        result.identity
    ):
        raise CohortContractError(CohortContractRejection.INVALID)
    _require_expected_digest(result.request_digest)
    _require_expected_digest(result.contract_digest)
    if result.boundary not in {"outer", "nested"} or (
        result.application_invoked is not None and type(result.application_invoked) is not bool
    ):
        raise CohortContractError(CohortContractRejection.INVALID)
    if result.completion_json is not None:
        if (
            result.refusal is not None
            or result.boundary != "outer"
            or result.application_invoked is False
        ):
            raise CohortContractError(CohortContractRejection.INVALID)
        decoded = decode_execution_completion(
            result.completion_json,
            expected_identity=result.identity,
            expected_execution_protocol_version=3,
            supported_protocols=_PROTOCOLS,
        )
        if (
            _encode_execution_completion_for_protocols(decoded.completion, _PROTOCOLS)
            != result.completion_json
        ):
            raise CohortContractError(CohortContractRejection.NONCANONICAL)
    elif result.refusal not in _REFUSALS or (
        result.application_invoked is False
        and (
            result.boundary != "outer"
            or result.refusal
            not in {
                "package_mismatch",
                "runtime_mismatch",
                "session_mismatch",
                "membership_mismatch",
            }
        )
    ):
        raise CohortContractError(CohortContractRejection.INVALID)
    value = asdict(result)
    value.update(
        schema="django-ray.cohort-execution-result", schema_version=1, execution_protocol_version=3
    )
    return _bounded_json_dumps(value, sort_keys=True)


def decode_cohort_execution_result(
    serialized, *, expected_identity, expected_request_digest, expected_cohort_contract_digest
) -> CohortExecutionResult:
    _require_expected_digest(expected_request_digest)
    _require_expected_digest(expected_cohort_contract_digest)
    try:
        value, _ = _bounded_json_loads(serialized, expected_execution_protocol_version=3)
        result = CohortExecutionResult(
            ExecutionIdentity(**value["identity"]),
            value["request_digest"],
            value["contract_digest"],
            value["completion_json"],
            value["refusal"],
            value["boundary"],
            value["application_invoked"],
        )
        if encode_cohort_execution_result(result) != serialized:
            raise ValueError
        if (
            result.identity != expected_identity
            or result.request_digest != expected_request_digest
            or result.contract_digest != expected_cohort_contract_digest
        ):
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CohortContractError(CohortContractRejection.BINDING_MISMATCH) from None


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
) -> tuple[ExecutionRequest, CohortExecutionContract | CohortSyncContract]:
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
    contract = decode_cohort_outer_contract(
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
