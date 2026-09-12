"""Explicit historical runtime epoch for retained protocol-1 adapter tests."""

import pytest

from django_ray import execution_codec, execution_protocol

LEGACY_PROTOCOLS = execution_protocol.ExecutionProtocolRange(1, 1)


def encode_legacy_execution_request(request: execution_codec.ExecutionRequest) -> str:
    """Build historical wire fixtures without changing the active producer."""
    return execution_codec._encode_execution_request_for_protocols(request, LEGACY_PROTOCOLS)


def encode_legacy_nested_execution_request(request: execution_codec.NestedExecutionRequest) -> str:
    """Build a retained nested envelope independently of the active epoch."""
    return execution_codec._encode_nested_request_for_protocols(request, LEGACY_PROTOCOLS)


def cohort_sender_context():
    """Canonical synthetic parent for current sender tests; no runtime proof."""
    from dataclasses import asdict

    from django_ray.runtime.context import durable_task_execution

    return durable_task_execution(**asdict(cohort_sender_task_context()))


def cohort_sender_task_context(
    identity=None,
    *,
    runtime_env_identity=None,
):
    """Return canonical parent controls for one explicitly synthetic sender."""
    from dataclasses import replace

    from django_ray.runtime.context import DurableTaskContext
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.target.cohort_contract import (
        cohort_execution_contract_digest,
        encode_cohort_execution_contract,
    )
    from django_ray.workflow.plans import runtime_env_plan_identity
    from tests.unit.test_cohort_contract import contract

    if identity is None:
        identity = execution_codec.ExecutionIdentity(41, "task-41", 3, 7)
    parent = replace(contract(), identity=identity)
    return DurableTaskContext(
        task_pk=identity.task_execution_pk,
        task_id=identity.task_id,
        execution_protocol_version=3,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        runtime_env_plan_identity=(
            runtime_env_plan_identity(normalize_runtime_env({})).as_transport_dict()
            if runtime_env_identity is None
            else runtime_env_identity
        ),
        strict_execution_request=True,
        cohort_contract_json=encode_cohort_execution_contract(parent),
        cohort_contract_digest=cohort_execution_contract_digest(parent),
    )


def install_legacy_execution_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the released epoch only for one explicitly opted-in test.

    Decoder defaults were bound when their functions were imported, so copying
    and restoring the keyword defaults is necessary alongside the encoder's
    module constant. Active-cohort tests never opt into this historical epoch.
    """
    monkeypatch.setattr(execution_protocol, "EXECUTION_PROTOCOL_VERSION", 1)
    monkeypatch.setattr(execution_protocol, "SUPPORTED_EXECUTION_PROTOCOL_RANGE", LEGACY_PROTOCOLS)
    monkeypatch.setattr(execution_codec, "SUPPORTED_EXECUTION_PROTOCOL_RANGE", LEGACY_PROTOCOLS)
    for decoder in (
        execution_codec._normalize_legacy_v1_completion,
        execution_codec.decode_legacy_v1_completion,
        execution_codec.decode_execution_completion,
        execution_codec.decode_execution_request,
        execution_codec.decode_nested_execution_request,
        execution_codec._prepare_nested_distributed_request,
    ):
        defaults = decoder.__kwdefaults__
        assert defaults is not None and "supported_protocols" in defaults
        monkeypatch.setattr(
            decoder, "__kwdefaults__", {**defaults, "supported_protocols": LEGACY_PROTOCOLS}
        )
