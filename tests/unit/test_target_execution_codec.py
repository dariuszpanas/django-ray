from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo

import pytest

import django_ray.target.execution_codec as codec
from django_ray.execution_codec import (
    EXECUTION_COMPLETION_MAX_BYTES,
    EXECUTION_REQUEST_MAX_BYTES,
    ExecutionIdentity,
    ExecutionRequest,
    encode_execution_request,
)
from django_ray.execution_protocol import (
    EXECUTION_PROTOCOL_VERSION,
    MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION,
    TARGET_EXECUTION_PROTOCOL_VERSION,
)
from django_ray.target.attestation import (
    RAY_CLUSTER_ATTESTATION_MAX_BYTES,
    RAY_TARGET_EXPECTATION_MAX_BYTES,
    RayNodeStateVersion,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    build_ray_cluster_attestation,
    build_ray_node_observation,
    build_ray_observation_boundary,
    encode_ray_cluster_attestation,
    encode_ray_target_expectation,
    ray_cluster_attestation_digest,
    ray_membership_digest,
    ray_target_expectation_digest,
)
from django_ray.target.execution_codec import (
    TARGET_EXECUTION_METADATA_MAX_BYTES,
    TargetApplicationCompletion,
    TargetExecutionCompatibilityReason,
    TargetExecutionCompatibilityRejection,
    TargetExecutionCompletion,
    TargetExecutionRequest,
    TargetExecutionRequestDecodeError,
    TargetExecutionRequestEncodeError,
    TargetExecutionRequestRejection,
    TargetExecutionResultDecodeError,
    TargetExecutionResultEncodeError,
    TargetExecutionResultRejection,
    build_target_execution_observed_evidence,
    decode_target_application_completion,
    decode_target_execution_request,
    decode_target_execution_result,
    encode_target_execution_request,
    encode_target_execution_result,
    target_execution_observed_proof_digest,
)

_NODE_ID = "01" * 28
_SESSION = "session_target_transport"
_OBSERVED_AT = datetime(2030, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
_CLAIMED_AT = _OBSERVED_AT + timedelta(microseconds=1)
_DIGEST = "sha256:" + "a" * 64


def _runtime(**changes: object) -> RayRuntimeVersion:
    values: dict[str, object] = {
        "ray_major": 2,
        "ray_minor": 56,
        "ray_patch": 0,
        "python_implementation": "cpython",
        "python_major": 3,
        "python_minor": 12,
        "python_patch": 10,
    }
    values.update(changes)
    return RayRuntimeVersion(**values)  # type: ignore[arg-type]


def _target_request(**changes: object) -> TargetExecutionRequest:
    runtime = _runtime()
    expectation = RayTargetExpectation(
        target_key="green",
        runner_family=RayRunnerFamily.RAY_CORE,
        cluster_session=_SESSION,
        policy_revision=4,
        runtime=runtime,
    )
    boundary = build_ray_observation_boundary(
        resource_state_version_before=10,
        resource_state_version_after=11,
        node_state_versions_before=(RayNodeStateVersion(node_id=_NODE_ID, node_state_version=20),),
        node_state_versions_after=(RayNodeStateVersion(node_id=_NODE_ID, node_state_version=21),),
    )
    attestation = build_ray_cluster_attestation(
        expectation=expectation,
        boundary=boundary,
        nodes=(
            build_ray_node_observation(
                node_id=_NODE_ID,
                cluster_session=_SESSION,
                runtime=runtime,
            ),
        ),
        observed_at=_OBSERVED_AT,
        expires_at=_OBSERVED_AT + timedelta(minutes=5),
    )
    values: dict[str, object] = {
        "identity": ExecutionIdentity(
            task_execution_pk=17,
            task_id="durable-task-id",
            attempt_number=2,
            execution_generation=7,
        ),
        "execution_protocol_version": TARGET_EXECUTION_PROTOCOL_VERSION,
        "target_execution_evidence_id": 23,
        "target_execution_evidence_digest": _DIGEST,
        "target_execution_claimed_at": _CLAIMED_AT,
        "target_expectation": expectation,
        "target_expectation_digest": ray_target_expectation_digest(expectation),
        "claim_attestation": attestation,
        "claim_attestation_digest": ray_cluster_attestation_digest(attestation),
        "callable_path": "tests.tasks.add",
        "transport_version": 1,
        "serialized_args": "[1,2]",
        "serialized_kwargs": "{}",
        "input_reference": None,
        "runtime_env_profile": None,
        "runtime_env_hash": "0" * 64,
        "runtime_env_plan_identity": {"digest": "sha256:" + "b" * 64},
        "compiled_graph_submission_transport": "direct-ray-core",
    }
    values.update(changes)
    return TargetExecutionRequest(**values)  # type: ignore[arg-type]


def _expected_controls(request: TargetExecutionRequest) -> dict[str, object]:
    return {
        "expected_identity": request.identity,
        "expected_target_execution_evidence_id": request.target_execution_evidence_id,
        "expected_target_execution_evidence_digest": (request.target_execution_evidence_digest),
        "expected_target_execution_claimed_at": request.target_execution_claimed_at,
        "expected_target_expectation_digest": request.target_expectation_digest,
        "expected_claim_attestation_digest": request.claim_attestation_digest,
    }


def _observed_target(request: TargetExecutionRequest):
    return build_target_execution_observed_evidence(
        identity=request.identity,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        observed_node_id=_NODE_ID,
        observed_cluster_session=_SESSION,
        observed_runtime=request.target_expectation.runtime,
        observed_membership_digest=ray_membership_digest(request.claim_attestation.boundary),
        observed_at=_OBSERVED_AT + timedelta(seconds=1),
    )


def _application_completion(*, result: object = 3) -> TargetApplicationCompletion:
    return TargetApplicationCompletion(
        success=True,
        result=result,
        result_reference=None,
        error=None,
        traceback=None,
        exception_type=None,
        retryable=None,
    )


def _target_completion(
    request: TargetExecutionRequest | None = None,
) -> TargetExecutionCompletion:
    request = _target_request() if request is None else request
    return TargetExecutionCompletion(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        application_completion=_application_completion(),
    )


def test_protocol_2_is_named_but_production_support_remains_1_to_1() -> None:
    assert TARGET_EXECUTION_PROTOCOL_VERSION == 2
    assert EXECUTION_PROTOCOL_VERSION == 1
    assert MIN_SUPPORTED_EXECUTION_PROTOCOL_VERSION == 1
    assert MAX_SUPPORTED_EXECUTION_PROTOCOL_VERSION == 1


def test_protocol_1_request_bytes_remain_unchanged() -> None:
    serialized = encode_execution_request(
        ExecutionRequest(
            identity=ExecutionIdentity(
                task_execution_pk=1,
                task_id="task",
                attempt_number=1,
                execution_generation=0,
            ),
            execution_protocol_version=1,
            callable_path="tests.tasks.add",
            transport_version=1,
            serialized_args="[]",
            serialized_kwargs="{}",
            input_reference=None,
            runtime_env_profile=None,
            runtime_env_hash="0" * 64,
            runtime_env_plan_identity={},
            compiled_graph_submission_transport="direct-ray-core",
        )
    )

    assert serialized == (
        '{"attempt_number":1,"callable_path":"tests.tasks.add",'
        '"compiled_graph_submission_transport":"direct-ray-core",'
        '"execution_generation":0,"execution_protocol_version":1,'
        '"input_reference":null,"request_schema":"django-ray.execution-request",'
        '"request_schema_version":1,'
        '"runtime_env_hash":"' + "0" * 64 + '",'
        '"runtime_env_plan_identity":{},"runtime_env_profile":null,'
        '"serialized_args":"[]","serialized_kwargs":"{}",'
        '"task_execution_pk":1,"task_id":"task","transport_version":1}'
    )


def test_target_request_round_trips_exact_canonical_evidence() -> None:
    request = _target_request()
    serialized = encode_target_execution_request(request)

    assert serialized == json.dumps(
        json.loads(serialized),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert decode_target_execution_request(serialized, **_expected_controls(request)) == request
    wire = json.loads(serialized)
    assert wire["request_schema"] == "django-ray.target-execution-request"
    assert wire["request_schema_version"] == 2
    assert wire["execution_protocol_version"] == 2
    assert wire["target_execution_evidence_id"] == 23
    assert wire["target_expectation"] == json.loads(
        encode_ray_target_expectation(request.target_expectation)
    )
    assert wire["claim_attestation"] == json.loads(
        encode_ray_cluster_attestation(request.claim_attestation)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_execution_evidence_id", 24),
        ("target_execution_evidence_digest", "sha256:" + "c" * 64),
        (
            "target_execution_claimed_at",
            (_CLAIMED_AT + timedelta(microseconds=1))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        ),
        ("target_expectation_digest", "sha256:" + "d" * 64),
        ("claim_attestation_digest", "sha256:" + "e" * 64),
    ],
)
def test_target_request_rejects_any_duplicated_evidence_mismatch(field: str, value: object) -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_request(request))
    wire[field] = value
    serialized = json.dumps(wire, sort_keys=True, separators=(",", ":"))

    with pytest.raises(TargetExecutionRequestDecodeError) as error:
        decode_target_execution_request(serialized, **_expected_controls(request))

    assert error.value.classification is TargetExecutionRequestRejection.EVIDENCE_MISMATCH


def test_target_result_variants_are_disjoint_and_prove_invocation() -> None:
    request = _target_request()
    observed = _observed_target(request)
    completion = TargetExecutionCompletion(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=observed,
        application_completion=_application_completion(),
    )
    rejection = TargetExecutionCompatibilityRejection(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=observed,
        compatibility_reason=TargetExecutionCompatibilityReason.PYTHON_VERSION_MISMATCH,
    )

    completion_wire = json.loads(encode_target_execution_result(completion))
    rejection_wire = json.loads(encode_target_execution_result(rejection))

    assert completion_wire["result_kind"] == "completion"
    assert completion_wire["application_invoked"] is True
    assert "application_completion" in completion_wire
    assert "compatibility_reason" not in completion_wire
    assert rejection_wire["result_kind"] == "compatibility_rejection"
    assert rejection_wire["application_invoked"] is False
    assert "compatibility_reason" in rejection_wire
    assert "application_completion" not in rejection_wire
    assert (
        decode_target_execution_result(
            json.dumps(completion_wire, sort_keys=True, separators=(",", ":")),
            **_expected_controls(request),
        )
        == completion
    )
    assert (
        decode_target_execution_result(
            json.dumps(rejection_wire, sort_keys=True, separators=(",", ":")),
            **_expected_controls(request),
        )
        == rejection
    )


def test_result_wrong_identity_is_uncertain_codec_rejection() -> None:
    request = _target_request()
    completion = TargetExecutionCompletion(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        application_completion=_application_completion(),
    )
    wire = json.loads(encode_target_execution_result(completion))
    wire["execution_generation"] += 1
    serialized = json.dumps(wire, sort_keys=True, separators=(",", ":"))

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result(serialized, **_expected_controls(request))

    assert error.value.classification is TargetExecutionResultRejection.IDENTITY_MISMATCH


def test_raw_application_outcome_is_normalized_only_inside_trusted_wrapper() -> None:
    raw = json.dumps(
        {
            "success": True,
            "result": {"answer": 42},
            "result_reference": None,
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        },
        indent=2,
    )

    assert decode_target_application_completion(raw) == _application_completion(
        result={"answer": 42}
    )


class _HostileTimezone(tzinfo):
    def utcoffset(self, _value):
        raise RuntimeError("poison target timezone")

    def dst(self, _value):
        return None


@pytest.mark.parametrize(
    "timestamp_field",
    ["target_execution_claimed_at", "observed_at"],
)
def test_observed_proof_boundaries_redact_hostile_timezone_callbacks(
    timestamp_field: str,
) -> None:
    request = _target_request()
    hostile = datetime(2030, 1, 1, tzinfo=_HostileTimezone())
    arguments = {
        "identity": request.identity,
        "target_execution_evidence_id": request.target_execution_evidence_id,
        "target_execution_evidence_digest": request.target_execution_evidence_digest,
        "target_execution_claimed_at": request.target_execution_claimed_at,
        "target_expectation_digest": request.target_expectation_digest,
        "claim_attestation_digest": request.claim_attestation_digest,
        "observed_node_id": _NODE_ID,
        "observed_cluster_session": _SESSION,
        "observed_runtime": request.target_expectation.runtime,
        "observed_membership_digest": request.claim_attestation.membership_digest,
        "observed_at": hostile,
    }
    arguments["observed_at"] = _OBSERVED_AT + timedelta(seconds=1)
    arguments[timestamp_field] = hostile

    for boundary in (
        target_execution_observed_proof_digest,
        build_target_execution_observed_evidence,
    ):
        with pytest.raises(TargetExecutionResultEncodeError) as error:
            boundary(**arguments)
        assert str(error.value) == "target execution result is invalid"
        assert "poison" not in str(error.value)


def test_target_envelopes_reserve_metadata_beyond_legacy_maxima(monkeypatch) -> None:
    assert codec.TARGET_EXECUTION_REQUEST_MAX_BYTES == (
        EXECUTION_REQUEST_MAX_BYTES
        + RAY_CLUSTER_ATTESTATION_MAX_BYTES
        + RAY_TARGET_EXPECTATION_MAX_BYTES
        + TARGET_EXECUTION_METADATA_MAX_BYTES
    )
    assert codec.TARGET_EXECUTION_RESULT_MAX_BYTES == (
        EXECUTION_COMPLETION_MAX_BYTES + TARGET_EXECUTION_METADATA_MAX_BYTES
    )
    request = _target_request(serialized_args='["' + "x" * 2048 + '"]')
    serialized_request = encode_target_execution_request(request)
    embedded_attestation_size = len(
        encode_ray_cluster_attestation(request.claim_attestation).encode("utf-8")
    )
    embedded_expectation_size = len(
        encode_ray_target_expectation(request.target_expectation).encode("utf-8")
    )
    simulated_v1_max = len(serialized_request.encode("utf-8")) - (
        embedded_attestation_size + embedded_expectation_size
    )
    monkeypatch.setattr(codec, "EXECUTION_REQUEST_MAX_BYTES", simulated_v1_max)
    monkeypatch.setattr(
        codec,
        "TARGET_EXECUTION_REQUEST_MAX_BYTES",
        simulated_v1_max
        + RAY_CLUSTER_ATTESTATION_MAX_BYTES
        + RAY_TARGET_EXPECTATION_MAX_BYTES
        + TARGET_EXECUTION_METADATA_MAX_BYTES,
    )
    assert (
        decode_target_execution_request(
            encode_target_execution_request(request), **_expected_controls(request)
        )
        == request
    )

    raw_application = json.dumps(
        {
            "success": True,
            "result": "x" * 2048,
            "result_reference": None,
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    simulated_completion_max = len(raw_application.encode("utf-8"))
    monkeypatch.setattr(codec, "EXECUTION_COMPLETION_MAX_BYTES", simulated_completion_max)
    monkeypatch.setattr(
        codec,
        "TARGET_EXECUTION_RESULT_MAX_BYTES",
        simulated_completion_max + TARGET_EXECUTION_METADATA_MAX_BYTES,
    )
    application = decode_target_application_completion(raw_application)
    completion = TargetExecutionCompletion(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        application_completion=application,
    )
    serialized_result = encode_target_execution_result(completion)
    assert (
        decode_target_execution_result(serialized_result, **_expected_controls(request))
        == completion
    )
    assert len(serialized_result.encode("utf-8")) > simulated_completion_max


def _serialized_application_completion(**changes: object) -> str:
    value: dict[str, object] = {
        "success": True,
        "result": 3,
        "result_reference": None,
        "error": None,
        "traceback": None,
        "exception_type": None,
        "retryable": None,
    }
    value.update(changes)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    "serialized",
    [
        None,
        "[]",
        "{not-json",
        '{"success":true,"success":false}',
        _serialized_application_completion(success=1),
        _serialized_application_completion(error="not allowed"),
        _serialized_application_completion(success=False, result=3, error="failed"),
        _serialized_application_completion(success=False, result=None, error=None),
        _serialized_application_completion(retryable="yes"),
        _serialized_application_completion(result=9223372036854775808),
        _serialized_application_completion(result=1e300).replace("1e+300", "1e999"),
        _serialized_application_completion(result=float("nan")),
        "\ud800",
    ],
)
def test_application_completion_rejects_hostile_or_inconsistent_json(
    serialized: object,
) -> None:
    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_application_completion(serialized)

    assert error.value.classification in {
        TargetExecutionResultRejection.INVALID,
        TargetExecutionResultRejection.RESOURCE_LIMIT,
    }


def test_application_completion_reports_resource_limit(monkeypatch) -> None:
    serialized = _serialized_application_completion(result="large")
    monkeypatch.setattr(codec, "EXECUTION_COMPLETION_MAX_BYTES", len(serialized) - 1)

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_application_completion(serialized)

    assert error.value.classification is TargetExecutionResultRejection.RESOURCE_LIMIT


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_protocol_version": 1},
        {
            "identity": ExecutionIdentity(
                task_execution_pk=0,
                task_id="task",
                attempt_number=1,
                execution_generation=0,
            )
        },
        {
            "identity": ExecutionIdentity(
                task_execution_pk=1,
                task_id="task",
                attempt_number=1 << 31,
                execution_generation=1,
            )
        },
        {
            "identity": ExecutionIdentity(
                task_execution_pk=1,
                task_id="task",
                attempt_number=1,
                execution_generation=0,
            )
        },
        {"target_execution_evidence_id": 0},
        {"target_execution_evidence_digest": "invalid"},
        {"target_execution_claimed_at": datetime(2030, 1, 1, tzinfo=_HostileTimezone())},
        {"target_expectation_digest": _DIGEST},
        {"callable_path": "undotted"},
        {"transport_version": 3},
        {"input_reference": "unexpected"},
        {"transport_version": 2, "input_reference": None},
        {
            "transport_version": 2,
            "input_reference": "sha256:" + "1" * 64,
            "serialized_args": "[]",
            "serialized_kwargs": "null",
        },
        {"runtime_env_profile": "bad profile"},
        {"runtime_env_hash": "bad"},
        {"runtime_env_plan_identity": []},
        {"runtime_env_plan_identity": {"bad": "nul\x00value"}},
        {"runtime_env_plan_identity": {1: "not-a-string-key"}},
        {"runtime_env_plan_identity": {"bad": object()}},
        {"runtime_env_plan_identity": {"bad": 9223372036854775808}},
        {"runtime_env_plan_identity": {"bad": float("nan")}},
        {"compiled_graph_submission_transport": "unknown"},
    ],
)
def test_request_encoder_rejects_invalid_identity_evidence_and_body(
    changes: dict[str, object],
) -> None:
    with pytest.raises(TargetExecutionRequestEncodeError) as error:
        encode_target_execution_request(replace(_target_request(), **changes))

    assert error.value.classification in {
        TargetExecutionRequestRejection.INVALID,
        TargetExecutionRequestRejection.RESOURCE_LIMIT,
    }


def test_request_encoder_rejects_non_core_expectation() -> None:
    request = _target_request()
    expectation = replace(request.target_expectation, runner_family=RayRunnerFamily.RAY_JOB)

    with pytest.raises(TargetExecutionRequestEncodeError) as error:
        encode_target_execution_request(replace(request, target_expectation=expectation))

    assert error.value.classification is TargetExecutionRequestRejection.INVALID


def test_request_encoder_rejects_recursive_and_overdeep_plan_identity() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(codec.TARGET_EXECUTION_MAX_DEPTH + 1):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child

    for plan in (recursive, nested):
        with pytest.raises(TargetExecutionRequestEncodeError):
            encode_target_execution_request(
                replace(_target_request(), runtime_env_plan_identity=plan)
            )


def test_request_encoder_maps_nested_decoder_and_size_failures(monkeypatch) -> None:
    request = _target_request()

    def reject(*_args, **_kwargs):
        raise TargetExecutionRequestDecodeError(TargetExecutionRequestRejection.RESOURCE_LIMIT)

    monkeypatch.setattr(codec, "decode_target_execution_request", reject)
    with pytest.raises(TargetExecutionRequestEncodeError) as error:
        encode_target_execution_request(request)
    assert error.value.classification is TargetExecutionRequestRejection.RESOURCE_LIMIT

    monkeypatch.undo()
    monkeypatch.setattr(codec, "TARGET_EXECUTION_REQUEST_MAX_BYTES", 1)
    with pytest.raises(TargetExecutionRequestEncodeError) as error:
        encode_target_execution_request(request)
    assert error.value.classification is TargetExecutionRequestRejection.RESOURCE_LIMIT


@pytest.mark.parametrize(
    ("field", "value", "classification"),
    [
        ("request_schema", "unknown", TargetExecutionRequestRejection.UNSUPPORTED_SCHEMA),
        ("request_schema_version", 3, TargetExecutionRequestRejection.UNSUPPORTED_SCHEMA),
        ("execution_protocol_version", 1, TargetExecutionRequestRejection.UNSUPPORTED_PROTOCOL),
        ("task_id", "other-task", TargetExecutionRequestRejection.IDENTITY_MISMATCH),
        ("attempt_number", 1 << 31, TargetExecutionRequestRejection.INVALID),
        ("execution_generation", 0, TargetExecutionRequestRejection.INVALID),
        ("target_execution_evidence_id", 0, TargetExecutionRequestRejection.INVALID),
        ("target_execution_evidence_digest", "invalid", TargetExecutionRequestRejection.INVALID),
        ("target_execution_claimed_at", "invalid", TargetExecutionRequestRejection.INVALID),
        ("callable_path", "undotted", TargetExecutionRequestRejection.INVALID),
        ("transport_version", 3, TargetExecutionRequestRejection.INVALID),
        ("runtime_env_plan_identity", [], TargetExecutionRequestRejection.INVALID),
        ("compiled_graph_submission_transport", "unknown", TargetExecutionRequestRejection.INVALID),
    ],
)
def test_request_decoder_has_fixed_rejections_for_hostile_fields(
    field: str,
    value: object,
    classification: TargetExecutionRequestRejection,
) -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_request(request))
    wire[field] = value
    serialized = json.dumps(wire, sort_keys=True, separators=(",", ":"))

    with pytest.raises(TargetExecutionRequestDecodeError) as error:
        decode_target_execution_request(serialized, **_expected_controls(request))

    assert error.value.classification is classification


@pytest.mark.parametrize(
    ("control", "value"),
    [
        ("expected_identity", ExecutionIdentity(0, "task", 1, 0)),
        ("expected_target_execution_evidence_id", 0),
        ("expected_target_execution_evidence_digest", "invalid"),
        ("expected_target_execution_claimed_at", "invalid"),
        ("expected_target_expectation_digest", "invalid"),
        ("expected_claim_attestation_digest", "invalid"),
    ],
)
def test_request_decoder_rejects_invalid_expected_controls(
    control: str,
    value: object,
) -> None:
    request = _target_request()
    controls = _expected_controls(request)
    controls[control] = value

    with pytest.raises(TargetExecutionRequestDecodeError) as error:
        decode_target_execution_request(
            encode_target_execution_request(request),
            **controls,
        )

    assert error.value.classification in {
        TargetExecutionRequestRejection.IDENTITY_MISMATCH,
        TargetExecutionRequestRejection.EVIDENCE_MISMATCH,
    }


@pytest.mark.parametrize("field", ["target_expectation", "claim_attestation"])
def test_request_decoder_rejects_malformed_embedded_claim(field: str) -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_request(request))
    wire[field] = {}

    with pytest.raises(TargetExecutionRequestDecodeError) as error:
        decode_target_execution_request(
            json.dumps(wire, sort_keys=True, separators=(",", ":")),
            **_expected_controls(request),
        )

    assert error.value.classification is TargetExecutionRequestRejection.INVALID


def test_request_decoder_rejects_attestation_for_another_expectation() -> None:
    request = _target_request()
    attested_expectation = replace(request.target_expectation, target_key="blue")
    attestation = build_ray_cluster_attestation(
        expectation=attested_expectation,
        boundary=request.claim_attestation.boundary,
        nodes=request.claim_attestation.nodes,
        observed_at=request.claim_attestation.observed_at,
        expires_at=request.claim_attestation.expires_at,
    )
    attestation_digest = ray_cluster_attestation_digest(attestation)
    wire = json.loads(encode_target_execution_request(request))
    wire["claim_attestation"] = json.loads(encode_ray_cluster_attestation(attestation))
    wire["claim_attestation_digest"] = attestation_digest
    controls = _expected_controls(request)
    controls["expected_claim_attestation_digest"] = attestation_digest

    with pytest.raises(TargetExecutionRequestDecodeError) as error:
        decode_target_execution_request(
            json.dumps(wire, sort_keys=True, separators=(",", ":")),
            **controls,
        )

    assert error.value.classification is TargetExecutionRequestRejection.EVIDENCE_MISMATCH


def test_request_decoder_rejects_noncanonical_and_resource_limited_input(
    monkeypatch,
) -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_request(request))

    with pytest.raises(TargetExecutionRequestDecodeError) as error:
        decode_target_execution_request(
            json.dumps(wire, indent=2, sort_keys=True),
            **_expected_controls(request),
        )
    assert error.value.classification is TargetExecutionRequestRejection.INVALID

    monkeypatch.setattr(codec, "TARGET_EXECUTION_REQUEST_MAX_BYTES", 1)
    with pytest.raises(TargetExecutionRequestDecodeError) as error:
        decode_target_execution_request(
            encode_target_execution_request.__name__,
            **_expected_controls(request),
        )
    assert error.value.classification is TargetExecutionRequestRejection.RESOURCE_LIMIT


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_protocol_version": 1},
        {
            "identity": ExecutionIdentity(
                task_execution_pk=0,
                task_id="task",
                attempt_number=1,
                execution_generation=0,
            )
        },
        {
            "identity": ExecutionIdentity(
                task_execution_pk=1,
                task_id="task",
                attempt_number=1 << 31,
                execution_generation=1,
            )
        },
        {
            "identity": ExecutionIdentity(
                task_execution_pk=1,
                task_id="task",
                attempt_number=1,
                execution_generation=0,
            )
        },
        {"target_execution_evidence_id": 0},
        {"target_execution_evidence_digest": "invalid"},
        {"target_execution_claimed_at": datetime(2030, 1, 1, tzinfo=_HostileTimezone())},
        {"executor_django_ray_version": "x" * 129},
        {"observed_target": object()},
        {
            "observed_target": replace(
                _observed_target(_target_request()),
                observed_proof_digest="sha256:" + "f" * 64,
            )
        },
        {"application_completion": object()},
        {
            "application_completion": TargetApplicationCompletion(
                success=False,
                result=3,
                result_reference=None,
                error=None,
                traceback=None,
                exception_type=None,
                retryable=None,
            )
        },
    ],
)
def test_result_encoder_rejects_invalid_envelope(changes: dict[str, object]) -> None:
    with pytest.raises(TargetExecutionResultEncodeError) as error:
        encode_target_execution_result(replace(_target_completion(), **changes))

    assert error.value.classification in {
        TargetExecutionResultRejection.INVALID,
        TargetExecutionResultRejection.RESOURCE_LIMIT,
    }


def test_result_encoder_rejects_recursive_application_result() -> None:
    recursive: list[object] = []
    recursive.append(recursive)
    completion = replace(
        _target_completion(),
        application_completion=_application_completion(result=recursive),
    )

    with pytest.raises(TargetExecutionResultEncodeError) as error:
        encode_target_execution_result(completion)

    assert error.value.classification is TargetExecutionResultRejection.INVALID


def test_result_encoder_rejects_invalid_compatibility_enum() -> None:
    request = _target_request()
    rejection = TargetExecutionCompatibilityRejection(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        compatibility_reason="unknown",  # type: ignore[arg-type]
    )

    with pytest.raises(TargetExecutionResultEncodeError):
        encode_target_execution_result(rejection)


def test_result_encoder_maps_nested_decoder_and_size_failures(monkeypatch) -> None:
    completion = _target_completion()

    def reject(*_args, **_kwargs):
        raise TargetExecutionResultDecodeError(TargetExecutionResultRejection.RESOURCE_LIMIT)

    monkeypatch.setattr(codec, "decode_target_execution_result", reject)
    with pytest.raises(TargetExecutionResultEncodeError) as error:
        encode_target_execution_result(completion)
    assert error.value.classification is TargetExecutionResultRejection.RESOURCE_LIMIT

    monkeypatch.undo()
    monkeypatch.setattr(codec, "TARGET_EXECUTION_RESULT_MAX_BYTES", 1)
    with pytest.raises(TargetExecutionResultEncodeError) as error:
        encode_target_execution_result(completion)
    assert error.value.classification is TargetExecutionResultRejection.RESOURCE_LIMIT


@pytest.mark.parametrize(
    ("field", "value", "classification"),
    [
        ("result_kind", "unknown", TargetExecutionResultRejection.INVALID),
        ("result_schema", "unknown", TargetExecutionResultRejection.UNSUPPORTED_SCHEMA),
        ("result_schema_version", 3, TargetExecutionResultRejection.UNSUPPORTED_SCHEMA),
        ("execution_protocol_version", 1, TargetExecutionResultRejection.UNSUPPORTED_PROTOCOL),
        ("attempt_number", 1 << 31, TargetExecutionResultRejection.INVALID),
        ("execution_generation", 0, TargetExecutionResultRejection.INVALID),
        ("target_execution_evidence_id", 0, TargetExecutionResultRejection.INVALID),
        ("target_execution_evidence_digest", "invalid", TargetExecutionResultRejection.INVALID),
        ("target_execution_claimed_at", "invalid", TargetExecutionResultRejection.INVALID),
        ("executor_django_ray_version", "x" * 129, TargetExecutionResultRejection.RESOURCE_LIMIT),
        ("application_invoked", False, TargetExecutionResultRejection.INVALID),
    ],
)
def test_result_decoder_has_fixed_rejections_for_hostile_fields(
    field: str,
    value: object,
    classification: TargetExecutionResultRejection,
) -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_result(_target_completion(request)))
    wire[field] = value

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result(
            json.dumps(wire, sort_keys=True, separators=(",", ":")),
            **_expected_controls(request),
        )

    assert error.value.classification is classification


@pytest.mark.parametrize(
    ("field", "value", "classification"),
    [
        ("observed_node_id", "bad", TargetExecutionResultRejection.INVALID),
        ("observed_cluster_session", "bad", TargetExecutionResultRejection.INVALID),
        ("observed_runtime", {}, TargetExecutionResultRejection.INVALID),
        ("observed_membership_digest", "bad", TargetExecutionResultRejection.INVALID),
        ("observed_at", "not-a-timestamp", TargetExecutionResultRejection.INVALID),
        (
            "observed_proof_digest",
            "sha256:" + "f" * 64,
            TargetExecutionResultRejection.PROOF_MISMATCH,
        ),
    ],
)
def test_result_decoder_rejects_malformed_or_unbound_observed_proof(
    field: str,
    value: object,
    classification: TargetExecutionResultRejection,
) -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_result(_target_completion(request)))
    wire["observed_target"][field] = value

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result(
            json.dumps(wire, sort_keys=True, separators=(",", ":")),
            **_expected_controls(request),
        )

    assert error.value.classification is classification


def test_result_claim_time_is_bound_into_observed_proof_preimage() -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_result(_target_completion(request)))
    changed_claimed_at = request.target_execution_claimed_at + timedelta(microseconds=1)
    wire["target_execution_claimed_at"] = changed_claimed_at.isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    controls = _expected_controls(request)
    controls["expected_target_execution_claimed_at"] = changed_claimed_at

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result(
            json.dumps(wire, sort_keys=True, separators=(",", ":")),
            **controls,
        )

    assert error.value.classification is TargetExecutionResultRejection.PROOF_MISMATCH


@pytest.mark.parametrize(
    ("control", "value"),
    [
        ("expected_identity", ExecutionIdentity(0, "task", 1, 0)),
        ("expected_target_execution_evidence_id", 0),
        ("expected_target_execution_evidence_digest", "invalid"),
        ("expected_target_execution_claimed_at", "invalid"),
        ("expected_target_expectation_digest", "invalid"),
        ("expected_claim_attestation_digest", "invalid"),
    ],
)
def test_result_decoder_rejects_invalid_expected_controls(
    control: str,
    value: object,
) -> None:
    request = _target_request()
    controls = _expected_controls(request)
    controls[control] = value

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result(
            encode_target_execution_result(_target_completion(request)),
            **controls,
        )

    assert error.value.classification in {
        TargetExecutionResultRejection.IDENTITY_MISMATCH,
        TargetExecutionResultRejection.EVIDENCE_MISMATCH,
    }


def test_result_decoder_rejects_wrong_shape_and_noncanonical_wire() -> None:
    request = _target_request()
    serialized = encode_target_execution_result(_target_completion(request))
    wire = json.loads(serialized)

    for hostile in (None, [], {**wire, "unexpected": True}):
        with pytest.raises(TargetExecutionResultDecodeError) as error:
            decode_target_execution_result(
                json.dumps(hostile, sort_keys=True, separators=(",", ":")),
                **_expected_controls(request),
            )
        assert error.value.classification is TargetExecutionResultRejection.INVALID

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result(
            json.dumps(wire, indent=2, sort_keys=True),
            **_expected_controls(request),
        )
    assert error.value.classification is TargetExecutionResultRejection.INVALID


def test_result_decoder_rejects_invalid_rejection_semantics() -> None:
    request = _target_request()
    rejection = TargetExecutionCompatibilityRejection(
        identity=request.identity,
        execution_protocol_version=2,
        target_execution_evidence_id=request.target_execution_evidence_id,
        target_execution_evidence_digest=request.target_execution_evidence_digest,
        target_execution_claimed_at=request.target_execution_claimed_at,
        target_expectation_digest=request.target_expectation_digest,
        claim_attestation_digest=request.claim_attestation_digest,
        executor_django_ray_version="0.5.0",
        observed_target=_observed_target(request),
        compatibility_reason=TargetExecutionCompatibilityReason.EXPIRED,
    )
    wire = json.loads(encode_target_execution_result(rejection))

    for field, value in (
        ("application_invoked", True),
        ("compatibility_reason", "unknown"),
    ):
        mutated = {**wire, field: value}
        with pytest.raises(TargetExecutionResultDecodeError) as error:
            decode_target_execution_result(
                json.dumps(mutated, sort_keys=True, separators=(",", ":")),
                **_expected_controls(request),
            )
        assert error.value.classification is TargetExecutionResultRejection.INVALID


def test_result_decoder_reports_resource_limit(monkeypatch) -> None:
    request = _target_request()
    monkeypatch.setattr(codec, "TARGET_EXECUTION_RESULT_MAX_BYTES", 1)

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result("{}", **_expected_controls(request))

    assert error.value.classification is TargetExecutionResultRejection.RESOURCE_LIMIT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ray_major", 0),
        ("python_major", 0),
        ("python_implementation", "CPython\n"),
        ("python_implementation", "cpython!"),
    ],
)
def test_observed_runtime_encoder_rejects_noncanonical_attestation_values(
    field: str,
    value: object,
) -> None:
    request = _target_request()
    runtime = replace(request.target_expectation.runtime, **{field: value})
    observed = replace(_observed_target(request), observed_runtime=runtime)

    with pytest.raises(TargetExecutionResultEncodeError) as error:
        encode_target_execution_result(
            replace(_target_completion(request), observed_target=observed)
        )

    assert error.value.classification is TargetExecutionResultRejection.INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ray_major", 0),
        ("python_major", 0),
        ("python_implementation", "CPython\n"),
        ("python_implementation", "cpython!"),
    ],
)
def test_observed_runtime_decoder_rejects_noncanonical_attestation_values(
    field: str,
    value: object,
) -> None:
    request = _target_request()
    wire = json.loads(encode_target_execution_result(_target_completion(request)))
    wire["observed_target"]["observed_runtime"][field] = value

    with pytest.raises(TargetExecutionResultDecodeError) as error:
        decode_target_execution_result(
            json.dumps(wire, sort_keys=True, separators=(",", ":")),
            **_expected_controls(request),
        )

    assert error.value.classification is TargetExecutionResultRejection.INVALID


@pytest.mark.parametrize(
    "identity",
    [
        ExecutionIdentity(
            task_execution_pk=17,
            task_id="task",
            attempt_number=1 << 31,
            execution_generation=1,
        ),
        ExecutionIdentity(
            task_execution_pk=17,
            task_id="task",
            attempt_number=1,
            execution_generation=0,
        ),
    ],
)
def test_observed_proof_rejects_noncanonical_protocol_2_identity(
    identity: ExecutionIdentity,
) -> None:
    request = _target_request()

    with pytest.raises(TargetExecutionResultEncodeError) as error:
        build_target_execution_observed_evidence(
            identity=identity,
            target_execution_evidence_id=request.target_execution_evidence_id,
            target_execution_evidence_digest=request.target_execution_evidence_digest,
            target_execution_claimed_at=request.target_execution_claimed_at,
            target_expectation_digest=request.target_expectation_digest,
            claim_attestation_digest=request.claim_attestation_digest,
            observed_node_id=_NODE_ID,
            observed_cluster_session=_SESSION,
            observed_runtime=request.target_expectation.runtime,
            observed_membership_digest=request.claim_attestation.membership_digest,
            observed_at=_OBSERVED_AT + timedelta(seconds=1),
        )

    assert error.value.classification is TargetExecutionResultRejection.INVALID
