"""Current native boundary fixtures on an already-owned Ray runtime.

These contracts exercise point guards with actual all-node observations. Test
binding/evidence identifiers do not register a target or grant manager eligibility.
"""

from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

from django_ray import __version__
from django_ray.runtime.context import DurableTaskContext, durable_task_execution
from django_ray.runtime.runtime_env import normalize_runtime_env
from django_ray.target.attestation import RayRunnerFamily, ray_target_expectation_digest
from django_ray.target.cohort_contract import (
    CohortExecutionContract,
    cohort_execution_contract_digest,
    encode_cohort_execution_contract,
)
from django_ray.target.cohort_probe import observe_current_cohort_target
from django_ray.target.cohort_runtime import _local_runtime
from django_ray.target.cohort_transport import _prepare_cohort_execution_from_environment
from django_ray.workflow.plans import runtime_env_plan_identity


def observed_contract(ray, identity, *, family=RayRunnerFamily.RAY_CORE):
    assert ray.is_initialized() is True, "native fixture must own its runtime"
    package, runtime = _local_runtime(ray)
    assert package == __version__
    attestation = observe_current_cohort_target(
        target_key=None,
        runner_family=family,
        expected_django_ray_version=package,
        expected_runtime=runtime,
        ttl_seconds=120,
        timeout_seconds=20,
        max_nodes=4,
    )
    return CohortExecutionContract(
        identity=identity,
        expected_django_ray_version=package,
        target_binding_id=identity.task_execution_pk,
        cohort_evidence_id=identity.task_execution_pk,
        cohort_evidence_digest=attestation.attestation_digest,
        claimed_at=datetime.now(UTC),
        target_expectation=attestation.expectation,
        target_expectation_digest=ray_target_expectation_digest(attestation.expectation),
        claim_attestation=attestation,
        claim_attestation_digest=attestation.attestation_digest,
    )


def task_context(contract, *, runtime_identity=None):
    identity = contract.identity
    return DurableTaskContext(
        task_pk=identity.task_execution_pk,
        task_id=identity.task_id,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        execution_protocol_version=3,
        strict_execution_request=True,
        runtime_env_plan_identity=(
            runtime_identity
            if runtime_identity is not None
            else runtime_env_plan_identity(normalize_runtime_env({})).as_transport_dict()
        ),
        compiled_graph_submission_transport="direct-ray-core",
        cohort_contract_json=encode_cohort_execution_contract(contract),
        cohort_contract_digest=cohort_execution_contract_digest(contract),
    )


def execution_context(contract, *, runtime_identity=None):
    return durable_task_execution(
        **asdict(task_context(contract, runtime_identity=runtime_identity))
    )


def prepared_execution(contract, callable_path, args_json, *, transport="direct-ray-core"):
    identity = contract.identity
    task = SimpleNamespace(
        pk=identity.task_execution_pk,
        task_id=identity.task_id,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        execution_protocol_version=3,
        callable_path=callable_path,
        args_json=args_json,
        kwargs_json="{}",
        input_reference=None,
    )
    return _prepare_cohort_execution_from_environment(
        task,
        contract=contract,
        transport=transport,
        environment=normalize_runtime_env({}),
        trust_identity={},
    )


def remote_expectations(prepared):
    identity = prepared.identity
    return {
        "expected_task_execution_pk": identity.task_execution_pk,
        "expected_task_id": identity.task_id,
        "expected_attempt_number": identity.attempt_number,
        "expected_execution_generation": identity.execution_generation,
        "expected_execution_protocol_version": 3,
        "expected_request_digest": prepared.request_digest,
        "expected_cohort_contract_digest": prepared.contract_digest,
    }


def decode_completion(encoded, prepared):
    from django_ray.execution_codec import ExecutionCompletionSource, decode_execution_completion
    from django_ray.target.cohort_transport import decode_cohort_execution_result

    result = decode_cohort_execution_result(
        encoded,
        expected_identity=prepared.identity,
        expected_request_digest=prepared.request_digest,
        expected_cohort_contract_digest=prepared.contract_digest,
    )
    assert result.refusal is None and result.completion_json is not None
    decoded = decode_execution_completion(
        result.completion_json,
        expected_identity=prepared.identity,
        expected_execution_protocol_version=3,
    )
    assert decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
    return decoded.completion


async def async_context_probe(value):
    """Read the bound context across await without inventing a database row."""
    import asyncio

    from django_ray.runtime.context import get_current_task_context

    before = get_current_task_context()
    assert before is not None and before.strict_execution_request is True
    await asyncio.sleep(0)
    after = get_current_task_context()
    assert after is before
    return {
        "value": value,
        "execution_id_before": before.task_pk,
        "execution_id_after": after.task_pk,
        "ray_job_driver_before": before.ray_job_driver,
        "ray_job_driver_after": after.ray_job_driver,
        "task_id": after.task_id,
        "active_task_count": len(asyncio.all_tasks()),
        "loop_running": asyncio.get_running_loop().is_running(),
    }


async def async_cli_marker(path):
    """Independent awaited application effect for the actual fixed Jobs CLI test."""
    import asyncio
    import json
    from pathlib import Path

    import ray

    from django_ray.runtime.context import get_current_task_context

    await asyncio.sleep(0)
    context = get_current_task_context()
    assert context is not None and context.strict_execution_request is True
    Path(path).write_text(
        json.dumps(
            {
                "value": 42,
                "protocol": context.execution_protocol_version,
                "task_id": context.task_id,
                "native_job_id": ray.get_runtime_context().get_job_id(),
                "awaited": asyncio.get_running_loop().is_running(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 42
