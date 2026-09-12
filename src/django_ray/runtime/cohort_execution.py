"""Explicit protocol-3 execution boundary; application imports follow the guard.

Returned data is authoritative only through the independently owned transport.
A Jobs pre-Django refusal is not published through logs or status as evidence.
"""

from __future__ import annotations

from django_ray.execution_codec import ExecutionIdentity
from django_ray.target.cohort_runtime import CohortRuntimeGuardError, verify_cohort_runtime
from django_ray.target.cohort_sync import CohortSyncContract, verify_cohort_sync_runtime
from django_ray.target.cohort_transport import (
    CohortExecutionResult,
    decode_cohort_execution_request,
    encode_cohort_execution_result,
)


def _refusal(error: CohortRuntimeGuardError) -> str:
    return {
        "package_version_mismatch": "package_mismatch",
        "ray_version_mismatch": "runtime_mismatch",
        "python_version_mismatch": "runtime_mismatch",
        "python_implementation_mismatch": "runtime_mismatch",
        "cluster_session_mismatch": "session_mismatch",
        "membership_changed": "membership_mismatch",
    }.get(error.reason.value, "observation_unavailable")


def find_cohort_guard_error(error: BaseException) -> CohortRuntimeGuardError | None:
    """Inspect a bounded exception chain, including Ray's retained application cause."""
    seen = set()
    pending = [error]
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, CohortRuntimeGuardError):
            return current
        for candidate in (current.__cause__, current.__context__, getattr(current, "cause", None)):
            if isinstance(candidate, BaseException):
                pending.append(candidate)
    return None


def execute_cohort_request(
    request_json: str,
    *,
    expected_identity: ExecutionIdentity,
    expected_request_digest: str,
    expected_cohort_contract_digest: str,
    ray_job_driver: bool = False,
) -> str:
    request, contract = decode_cohort_execution_request(
        request_json,
        expected_identity=expected_identity,
        expected_request_digest=expected_request_digest,
        expected_cohort_contract_digest=expected_cohort_contract_digest,
    )
    refusal = None
    if type(contract) is CohortSyncContract:
        if ray_job_driver:
            raise ValueError("Invalid Sync transport")
        refusal = verify_cohort_sync_runtime(contract)
    else:
        try:
            verify_cohort_runtime(contract)
        except CohortRuntimeGuardError as error:
            refusal = _refusal(error)
    if refusal is not None:
        return encode_cohort_execution_result(
            CohortExecutionResult(
                expected_identity,
                expected_request_digest,
                expected_cohort_contract_digest,
                refusal=refusal,
                application_invoked=False if refusal != "observation_unavailable" else None,
            )
        )

    from django_ray.runtime.entrypoint import _persist_task_completion, execute_task

    try:
        completion = execute_task(
            request.callable_path,
            request.serialized_args,
            request.serialized_kwargs,
            task_execution_pk=request.identity.task_execution_pk,
            task_id=request.identity.task_id,
            attempt_number=request.identity.attempt_number,
            execution_generation=request.identity.execution_generation,
            runtime_env_profile=request.runtime_env_profile,
            runtime_env_hash=request.runtime_env_hash,
            runtime_env_plan_identity=request.runtime_env_plan_identity,
            input_reference=request.input_reference,
            ray_job_driver=ray_job_driver,
            _completion_identity=request.identity,
            _execution_protocol_version=3,
            _strict_execution_request=True,
            _cohort_contract_json=request.cohort_contract_json,
            _cohort_contract_digest=expected_cohort_contract_digest,
            _persist_completion=False,
        )
        result = CohortExecutionResult(
            expected_identity,
            expected_request_digest,
            expected_cohort_contract_digest,
            completion_json=completion,
            application_invoked=None,
        )
    except Exception as error:
        if find_cohort_guard_error(error) is None:
            raise
        result = CohortExecutionResult(
            expected_identity,
            expected_request_digest,
            expected_cohort_contract_digest,
            refusal="nested_refusal",
            boundary="nested",
            application_invoked=None,
        )
    encoded = encode_cohort_execution_result(result)
    if ray_job_driver:
        _persist_task_completion(
            request.identity.task_execution_pk,
            request.identity.attempt_number,
            request.identity.execution_generation,
            encoded,
        )
    return encoded
