"""Private nested adapters deriving only from an independently bound parent."""

from django_ray.execution_codec import decode_nested_execution_request
from django_ray.target.cohort_contract import (
    CohortExecutionContract,
    cohort_leaf_contract_digest,
    decode_cohort_leaf_contract,
    derive_cohort_leaf_contract,
    encode_cohort_leaf_contract,
)
from django_ray.target.cohort_runtime import (
    CohortRuntimeGuardError,
    CohortRuntimeGuardReason,
    verify_cohort_runtime,
)
from django_ray.target.cohort_transport import (
    decode_cohort_nested_execution_request,
    decode_cohort_outer_contract,
)


def cohort_leaf_controls(context):
    if context.execution_protocol_version != 3:
        return None, None, None
    if context.cohort_contract_json is not None:
        outer = decode_cohort_outer_contract(
            context.cohort_contract_json, expected_contract_digest=context.cohort_contract_digest
        )
        if type(outer) is not CohortExecutionContract:
            raise CohortRuntimeGuardError(CohortRuntimeGuardReason.INVALID_CONTRACT)
        leaf = derive_cohort_leaf_contract(outer)
    else:
        leaf = decode_cohort_leaf_contract(
            context.cohort_leaf_contract_json,
            expected_contract_digest=context.cohort_leaf_digest,
            expected_outer_contract_digest=context.cohort_contract_digest,
        )
    return (
        encode_cohort_leaf_contract(leaf),
        cohort_leaf_contract_digest(leaf),
        leaf.outer_contract_digest,
    )


def decode_runtime_nested_request(
    serialized,
    *,
    expected_execution_protocol_version,
    expected_cohort_leaf_digest=None,
    expected_outer_contract_digest=None,
    **kwargs,
):
    if expected_execution_protocol_version != 3:
        if expected_cohort_leaf_digest is not None or expected_outer_contract_digest is not None:
            raise CohortRuntimeGuardError(CohortRuntimeGuardReason.INVALID_CONTRACT)
        return decode_nested_execution_request(
            serialized,
            expected_execution_protocol_version=expected_execution_protocol_version,
            **kwargs,
        )
    request, leaf = decode_cohort_nested_execution_request(
        serialized,
        expected_cohort_leaf_digest=expected_cohort_leaf_digest,
        expected_outer_contract_digest=expected_outer_contract_digest,
        expected_output_preview_callable_path=kwargs.pop(
            "expected_output_preview_callable_path", None
        ),
        **kwargs,
    )
    verify_cohort_runtime(leaf)
    return request


def nested_cohort_context(request):
    if request.execution_protocol_version != 3:
        return {}
    leaf = decode_cohort_leaf_contract(
        request.cohort_leaf_contract_json, expected_identity=request.outer_identity
    )
    return {
        "cohort_leaf_contract_json": request.cohort_leaf_contract_json,
        "cohort_leaf_digest": cohort_leaf_contract_digest(leaf),
        "cohort_contract_digest": leaf.outer_contract_digest,
    }
