"""Trust-boundary tests for execution request and completion codecs."""

from __future__ import annotations

import json
import math
import tracemalloc
from dataclasses import replace

import pytest

import django_ray.execution_codec as codec_module
from django_ray.execution_codec import (
    EXECUTION_COMPLETION_SCHEMA,
    EXECUTION_COMPLETION_SCHEMA_VERSION,
    EXECUTION_REQUEST_SCHEMA,
    EXECUTION_REQUEST_SCHEMA_VERSION,
    DecodedExecutionCompletion,
    ExecutionCompletion,
    ExecutionCompletionDecodeError,
    ExecutionCompletionRejection,
    ExecutionCompletionSource,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionRequestDecodeError,
    ExecutionRequestEncodeError,
    ExecutionRequestRejection,
    decode_execution_completion,
    decode_execution_request,
    decode_legacy_v1_completion,
    encode_execution_completion,
    encode_execution_request,
    encode_execution_request_rejection,
)
from django_ray.execution_protocol import ExecutionProtocolRange

_PROTOCOL_V1 = ExecutionProtocolRange(1, 1)


@pytest.fixture
def identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        task_execution_pk=41,
        task_id="task-41",
        attempt_number=2,
        execution_generation=3,
    )


def _success(identity: ExecutionIdentity) -> ExecutionCompletion:
    return ExecutionCompletion(
        identity=identity,
        execution_protocol_version=1,
        executor_django_ray_version="0.5.0",
        success=True,
        result={"answer": [42]},
        result_reference=None,
        error=None,
        traceback=None,
        exception_type=None,
        retryable=None,
    )


def _failure(identity: ExecutionIdentity) -> ExecutionCompletion:
    return ExecutionCompletion(
        identity=identity,
        execution_protocol_version=1,
        executor_django_ray_version="0.5.0",
        success=False,
        result=None,
        result_reference=None,
        error="boom",
        traceback="trace",
        exception_type="ValueError",
        retryable=False,
    )


def _inline_request(identity: ExecutionIdentity) -> ExecutionRequest:
    return ExecutionRequest(
        identity=identity,
        execution_protocol_version=1,
        callable_path="testproject.tasks.add_numbers",
        transport_version=1,
        serialized_args="[20,22]",
        serialized_kwargs='{"scale":1}',
        input_reference=None,
        runtime_env_profile="default",
        runtime_env_hash="a" * 64,
        runtime_env_plan_identity={
            "plan_format": "django-ray.runtime-env-plan",
            "plan_format_version": 1,
        },
        compiled_graph_submission_transport="direct-ray-core",
    )


def _referenced_request(identity: ExecutionIdentity) -> ExecutionRequest:
    return replace(
        _inline_request(identity),
        transport_version=2,
        serialized_args="null",
        serialized_kwargs="null",
        input_reference="resultfs://inputs/sha256/abc/42",
        compiled_graph_submission_transport="ray-job",
    )


def _decode(
    serialized: object,
    identity: ExecutionIdentity,
    *,
    expected_protocol: int = 1,
    supported_protocols: ExecutionProtocolRange = _PROTOCOL_V1,
) -> DecodedExecutionCompletion:
    return decode_execution_completion(
        serialized,
        expected_identity=identity,
        expected_execution_protocol_version=expected_protocol,
        supported_protocols=supported_protocols,
    )


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _assert_rejection(
    serialized: object,
    identity: ExecutionIdentity,
    classification: ExecutionCompletionRejection,
    *,
    attempted_versioned: bool,
    expected_protocol: int = 1,
    supported_protocols: ExecutionProtocolRange = _PROTOCOL_V1,
) -> ExecutionCompletionDecodeError:
    with pytest.raises(ExecutionCompletionDecodeError) as caught:
        _decode(
            serialized,
            identity,
            expected_protocol=expected_protocol,
            supported_protocols=supported_protocols,
        )
    error = caught.value
    assert error.classification is classification
    assert error.attempted_versioned is attempted_versioned
    assert error.requires_nonretryable_disposition is (
        classification is not ExecutionCompletionRejection.MALFORMED_LEGACY
    )
    assert str(error) == f"execution completion rejected: {classification.value}"
    return error


def _decode_request(
    serialized: object,
    *,
    supported_protocols: ExecutionProtocolRange = _PROTOCOL_V1,
    expected_identity: ExecutionIdentity | None = None,
    expected_protocol: int | None = None,
) -> ExecutionRequest:
    return decode_execution_request(
        serialized,
        supported_protocols=supported_protocols,
        expected_identity=expected_identity,
        expected_execution_protocol_version=expected_protocol,
    )


def _assert_request_rejection(
    serialized: object,
    classification: ExecutionRequestRejection,
    *,
    attempted_versioned: bool,
    supported_protocols: ExecutionProtocolRange = _PROTOCOL_V1,
    expected_identity: ExecutionIdentity | None = None,
    expected_protocol: int | None = None,
) -> ExecutionRequestDecodeError:
    with pytest.raises(ExecutionRequestDecodeError) as caught:
        _decode_request(
            serialized,
            supported_protocols=supported_protocols,
            expected_identity=expected_identity,
            expected_protocol=expected_protocol,
        )
    error = caught.value
    assert error.classification is classification
    assert error.attempted_versioned is attempted_versioned
    assert error.allows_legacy_fallback is (
        classification is ExecutionRequestRejection.LEGACY_REQUEST and not attempted_versioned
    )
    assert str(error) == f"execution request rejected: {classification.value}"
    return error


def test_execution_request_round_trips_as_exact_canonical_flat_schema(
    identity: ExecutionIdentity,
) -> None:
    request = _inline_request(identity)

    serialized = encode_execution_request(request)
    value = json.loads(serialized)
    decoded = _decode_request(
        serialized,
        expected_identity=identity,
        expected_protocol=1,
    )

    assert serialized == _canonical(value)
    assert set(value) == {
        "request_schema",
        "request_schema_version",
        "execution_protocol_version",
        "task_execution_pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "callable_path",
        "transport_version",
        "serialized_args",
        "serialized_kwargs",
        "input_reference",
        "runtime_env_profile",
        "runtime_env_hash",
        "runtime_env_plan_identity",
        "compiled_graph_submission_transport",
    }
    assert value["request_schema"] == EXECUTION_REQUEST_SCHEMA
    assert value["request_schema_version"] == EXECUTION_REQUEST_SCHEMA_VERSION
    assert decoded == request


def test_referenced_request_round_trips_without_hydrating_opaque_input(
    identity: ExecutionIdentity,
) -> None:
    request = _referenced_request(identity)

    serialized = encode_execution_request(request)
    decoded = _decode_request(serialized)
    value = json.loads(serialized)

    assert decoded == request
    assert decoded.input_reference == "resultfs://inputs/sha256/abc/42"
    assert decoded.serialized_args == decoded.serialized_kwargs == "null"
    assert value["transport_version"] == 2
    assert value["callable_path"] == request.callable_path
    assert value["task_execution_pk"] == identity.task_execution_pk


def test_request_encoder_normalizes_numeric_mapping_keys(
    identity: ExecutionIdentity,
) -> None:
    request = replace(
        _inline_request(identity),
        runtime_env_plan_identity={10: "ten", 2: "two"},
    )

    decoded = _decode_request(encode_execution_request(request))

    assert decoded.runtime_env_plan_identity == {"10": "ten", "2": "two"}


def test_request_encoder_rejects_post_stringification_key_collision(
    identity: ExecutionIdentity,
) -> None:
    request = replace(
        _inline_request(identity),
        runtime_env_plan_identity={1: "numeric", "1": "text"},
    )

    with pytest.raises(ExecutionRequestEncodeError) as caught:
        encode_execution_request(request)

    assert str(caught.value) == "execution request is invalid"


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_protocol_version": 2},
        {"execution_protocol_version": True},
        {"callable_path": "not_dotted"},
        {"callable_path": "tests.task\x00forged"},
        {"callable_path": "x." + "y" * 499},
        {"transport_version": True},
        {"transport_version": 3},
        {"serialized_args": 1},
        {"serialized_args": "[1]\x00forged"},
        {"serialized_kwargs": ""},
        {"input_reference": "unexpected"},
        {"runtime_env_profile": "invalid profile"},
        {"runtime_env_profile": "profile\x00forged"},
        {"runtime_env_hash": "A" * 64},
        {"runtime_env_hash": "a" * 63},
        {"runtime_env_plan_identity": []},
        {"compiled_graph_submission_transport": "other"},
    ],
)
def test_request_encoder_has_fixed_failure_for_invalid_fields(
    identity: ExecutionIdentity,
    changes: dict[str, object],
) -> None:
    request = replace(_inline_request(identity), **changes)

    with pytest.raises(ExecutionRequestEncodeError) as caught:
        encode_execution_request(request)

    assert str(caught.value) == "execution request is invalid"
    assert "forged" not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"input_reference": None},
        {"input_reference": "x" * 501},
        {"serialized_args": "[]"},
        {"serialized_kwargs": "{}"},
    ],
)
def test_referenced_request_requires_reference_and_safety_placeholders(
    identity: ExecutionIdentity,
    changes: dict[str, object],
) -> None:
    request = replace(_referenced_request(identity), **changes)

    with pytest.raises(ExecutionRequestEncodeError, match="execution request is invalid"):
        encode_execution_request(request)


def test_request_encoder_rejects_nonfinite_and_invalid_unicode_metadata(
    identity: ExecutionIdentity,
) -> None:
    for value in (float("nan"), float("inf"), "\ud800"):
        request = replace(
            _inline_request(identity),
            runtime_env_plan_identity={"value": value},
        )
        with pytest.raises(ExecutionRequestEncodeError, match="execution request is invalid"):
            encode_execution_request(request)


def test_inline_serialized_payload_remains_an_opaque_subordinate_format(
    identity: ExecutionIdentity,
) -> None:
    request = replace(
        _inline_request(identity),
        serialized_args="[NaN]",
        serialized_kwargs='{"duplicate":1,"duplicate":2}',
    )

    decoded = _decode_request(encode_execution_request(request))

    assert decoded.serialized_args == "[NaN]"
    assert decoded.serialized_kwargs == '{"duplicate":1,"duplicate":2}'


def test_request_requires_exact_keys(identity: ExecutionIdentity) -> None:
    value = json.loads(encode_execution_request(_inline_request(identity)))
    value["extra"] = "ignored only by the legacy adapter"
    _assert_request_rejection(
        _canonical(value),
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )

    del value["extra"]
    del value["runtime_env_hash"]
    _assert_request_rejection(
        _canonical(value),
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


@pytest.mark.parametrize(
    ("change", "classification"),
    [
        ({"request_schema": "other"}, ExecutionRequestRejection.UNSUPPORTED_SCHEMA),
        ({"request_schema_version": 2}, ExecutionRequestRejection.UNSUPPORTED_SCHEMA),
        ({"request_schema_version": True}, ExecutionRequestRejection.UNSUPPORTED_SCHEMA),
        ({"transport_version": 3}, ExecutionRequestRejection.UNSUPPORTED_TRANSPORT),
        ({"transport_version": True}, ExecutionRequestRejection.UNSUPPORTED_TRANSPORT),
        ({"callable_path": 1}, ExecutionRequestRejection.INVALID_VERSIONED),
        ({"runtime_env_plan_identity": []}, ExecutionRequestRejection.INVALID_VERSIONED),
    ],
)
def test_request_schema_and_body_types_fail_closed(
    identity: ExecutionIdentity,
    change: dict[str, object],
    classification: ExecutionRequestRejection,
) -> None:
    value = json.loads(encode_execution_request(_inline_request(identity)))
    value.update(change)

    _assert_request_rejection(
        _canonical(value),
        classification,
        attempted_versioned=True,
    )


def test_invalid_identity_precedes_unsupported_protocol(
    identity: ExecutionIdentity,
) -> None:
    value = json.loads(encode_execution_request(_inline_request(identity)))
    value["task_execution_pk"] = 0
    value["execution_protocol_version"] = 2

    error = _assert_request_rejection(
        _canonical(value),
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )

    assert error.validated_identity is None
    assert error.requested_execution_protocol_version is None


def test_unsupported_protocol_exposes_only_bounded_identity_and_epoch(
    identity: ExecutionIdentity,
) -> None:
    value = json.loads(encode_execution_request(_inline_request(identity)))
    value["execution_protocol_version"] = 2
    value["serialized_args"] = '"password=must-not-be-retained"'

    error = _assert_request_rejection(
        _canonical(value),
        ExecutionRequestRejection.UNSUPPORTED_PROTOCOL,
        attempted_versioned=True,
    )

    assert error.validated_identity == identity
    assert error.requested_execution_protocol_version == 2
    assert not hasattr(error, "request")
    assert not hasattr(error, "serialized_args")
    assert "must-not-be-retained" not in str(error)


def test_invalid_protocol_type_exposes_no_identity_for_persistence(
    identity: ExecutionIdentity,
) -> None:
    value = json.loads(encode_execution_request(_inline_request(identity)))
    value["execution_protocol_version"] = True

    error = _assert_request_rejection(
        _canonical(value),
        ExecutionRequestRejection.UNSUPPORTED_PROTOCOL,
        attempted_versioned=True,
    )

    assert error.validated_identity is None
    assert error.requested_execution_protocol_version is None


@pytest.mark.parametrize(
    ("field", "replacement", "classification"),
    [
        ("task_execution_pk", 42, ExecutionRequestRejection.IDENTITY_MISMATCH),
        ("task_id", "other", ExecutionRequestRejection.IDENTITY_MISMATCH),
        ("attempt_number", 3, ExecutionRequestRejection.IDENTITY_MISMATCH),
        ("execution_generation", 4, ExecutionRequestRejection.IDENTITY_MISMATCH),
        ("execution_protocol_version", 2, ExecutionRequestRejection.PROTOCOL_MISMATCH),
    ],
)
def test_external_expected_header_proof_rejects_mismatch(
    identity: ExecutionIdentity,
    field: str,
    replacement: object,
    classification: ExecutionRequestRejection,
) -> None:
    value = json.loads(encode_execution_request(_inline_request(identity)))
    value[field] = replacement

    _assert_request_rejection(
        _canonical(value),
        classification,
        attempted_versioned=True,
        supported_protocols=ExecutionProtocolRange(1, 2),
        expected_identity=identity,
        expected_protocol=1,
    )


def test_noncanonical_execution_request_is_rejected(identity: ExecutionIdentity) -> None:
    value = json.loads(encode_execution_request(_inline_request(identity)))

    _assert_request_rejection(
        json.dumps(value),
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


def test_request_duplicate_keys_and_nonfinite_metadata_are_rejected(
    identity: ExecutionIdentity,
) -> None:
    serialized = encode_execution_request(_inline_request(identity))
    duplicate = serialized.replace(
        '"request_schema":',
        '"request_schema":"duplicate","request_schema":',
        1,
    )
    _assert_request_rejection(
        duplicate,
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )

    value = json.loads(serialized)
    value["runtime_env_plan_identity"] = {"value": float("nan")}
    _assert_request_rejection(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


@pytest.mark.parametrize(
    "field",
    [
        "task_id",
        "callable_path",
        "serialized_args",
        "serialized_kwargs",
        "input_reference",
        "runtime_env_profile",
        "runtime_env_hash",
        "compiled_graph_submission_transport",
    ],
)
def test_request_decoder_rejects_nul_in_executor_facing_scalars(
    identity: ExecutionIdentity,
    field: str,
) -> None:
    request = (
        _referenced_request(identity) if field == "input_reference" else _inline_request(identity)
    )
    value = json.loads(encode_execution_request(request))
    value[field] = "value\x00forged"

    _assert_request_rejection(
        _canonical(value),
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


@pytest.mark.parametrize(
    "reserved_marker",
    ["request_schema", "request_schema_version", "execution_protocol_version"],
)
def test_any_request_marker_prevents_legacy_fallback(reserved_marker: str) -> None:
    error = _assert_request_rejection(
        json.dumps({reserved_marker: None}),
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )

    assert error.allows_legacy_fallback is False


@pytest.mark.parametrize(
    "legacy_payload",
    [
        {
            "callable_path": "testproject.tasks.add_numbers",
            "serialized_args": "[1,2]",
            "serialized_kwargs": "{}",
            "task_execution_pk": 1,
            "task_id": "legacy-inline",
            "attempt_number": 1,
            "execution_generation": 0,
        },
        {
            "callable_path": "testproject.tasks.add_numbers",
            "transport_version": 2,
            "input_reference": "resultfs://legacy/reference",
            "task_execution_pk": 1,
            "task_id": "legacy-reference",
            "attempt_number": 1,
            "execution_generation": 0,
        },
    ],
)
def test_released_job_payloads_are_left_for_explicit_legacy_adapter(
    legacy_payload: dict[str, object],
) -> None:
    error = _assert_request_rejection(
        json.dumps(legacy_payload),
        ExecutionRequestRejection.LEGACY_REQUEST,
        attempted_versioned=False,
    )

    assert error.allows_legacy_fallback is True


def test_malformed_request_marker_still_suppresses_legacy_fallback() -> None:
    _assert_request_rejection(
        '{"request_schema":"django-ray.execution-request","secret":',
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


def test_request_raw_surrogate_classification_respects_marker() -> None:
    _assert_request_rejection(
        '{"request_schema":"django-ray.execution-request","value":"' + "\ud800" + '"}',
        ExecutionRequestRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )
    _assert_request_rejection(
        '{"transport_version":2,"value":"' + "\ud800" + '"}',
        ExecutionRequestRejection.LEGACY_REQUEST,
        attempted_versioned=False,
    )


def test_request_encoded_byte_limit_is_checked_before_decoding(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = encode_execution_request(_inline_request(identity))
    byte_size = len(serialized.encode("utf-8"))
    monkeypatch.setattr(codec_module, "EXECUTION_REQUEST_MAX_BYTES", byte_size)
    assert _decode_request(serialized).identity == identity

    monkeypatch.setattr(codec_module, "EXECUTION_REQUEST_MAX_BYTES", byte_size - 1)
    error = _assert_request_rejection(
        serialized,
        ExecutionRequestRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )
    assert error.allows_legacy_fallback is False


def test_oversized_request_never_reaches_reserved_marker_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec_module, "EXECUTION_REQUEST_MAX_BYTES", 32)
    serialized = '{"request_schema":' + "x" * 32

    def fail_scan(*_args, **_kwargs):
        pytest.fail("oversized request reached the reserved-key scanner")

    monkeypatch.setattr(codec_module, "_preparse_json_scan", fail_scan)
    _assert_request_rejection(
        serialized,
        ExecutionRequestRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )


def test_request_depth_and_node_budgets_precede_json_allocation(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = encode_execution_request(_inline_request(identity))
    monkeypatch.setattr(codec_module, "EXECUTION_REQUEST_MAX_DEPTH", 1)
    _assert_request_rejection(
        serialized,
        ExecutionRequestRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )

    monkeypatch.setattr(codec_module, "EXECUTION_REQUEST_MAX_DEPTH", 64)
    monkeypatch.setattr(codec_module, "EXECUTION_REQUEST_MAX_NODES", 10)
    original_loads = codec_module.json.loads

    def fail_parse(value, *args, **kwargs):
        if len(value) > len('"django-ray.execution-request"'):
            pytest.fail("wide request reached full json.loads")
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(codec_module.json, "loads", fail_parse)
    _assert_request_rejection(
        '{"request_schema":"django-ray.execution-request","wide":[0,0,0,0,0,0,0,0]}',
        ExecutionRequestRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )


def test_request_runtime_identity_has_an_independent_byte_budget(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = replace(
        _inline_request(identity),
        runtime_env_plan_identity={"value": "abcdefghij"},
    )
    serialized_identity = _canonical(request.runtime_env_plan_identity)
    identity_size = len(serialized_identity.encode("utf-8"))
    monkeypatch.setattr(
        codec_module,
        "EXECUTION_REQUEST_RUNTIME_ENV_IDENTITY_MAX_BYTES",
        identity_size,
    )
    serialized = encode_execution_request(request)
    assert (
        _decode_request(serialized).runtime_env_plan_identity == request.runtime_env_plan_identity
    )

    monkeypatch.setattr(
        codec_module,
        "EXECUTION_REQUEST_RUNTIME_ENV_IDENTITY_MAX_BYTES",
        identity_size - 1,
    )
    with pytest.raises(ExecutionRequestEncodeError, match="execution request is invalid"):
        encode_execution_request(request)
    _assert_request_rejection(
        serialized,
        ExecutionRequestRejection.RESOURCE_LIMIT,
        attempted_versioned=True,
    )


def test_request_encoder_stops_aggregate_json_before_detached_parse(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = replace(_inline_request(identity), serialized_args='["' + "x" * 200 + '"]')
    serialized = encode_execution_request(request)
    monkeypatch.setattr(
        codec_module,
        "EXECUTION_REQUEST_MAX_BYTES",
        len(serialized.encode("utf-8")) - 1,
    )

    def fail_parse(*_args, **_kwargs):
        pytest.fail("aggregate-over-cap request reached detached parsing")

    monkeypatch.setattr(codec_module, "_bounded_request_json_loads", fail_parse)
    with pytest.raises(ExecutionRequestEncodeError, match="execution request is invalid"):
        encode_execution_request(request)


@pytest.mark.parametrize(
    ("protocol", "classification"),
    [
        (1, ExecutionRequestRejection.IDENTITY_MISMATCH),
        (2, ExecutionRequestRejection.UNSUPPORTED_PROTOCOL),
    ],
)
def test_request_rejection_encodes_a_canonical_enriched_failure(
    identity: ExecutionIdentity,
    protocol: int,
    classification: ExecutionRequestRejection,
) -> None:
    serialized = encode_execution_request_rejection(
        expected_identity=identity,
        expected_execution_protocol_version=protocol,
        executor_django_ray_version="0.5.0",
        classification=classification,
    )
    value = json.loads(serialized)
    decoded = _decode(
        serialized,
        identity,
        expected_protocol=protocol,
        supported_protocols=ExecutionProtocolRange(1, protocol),
    )

    assert serialized == _canonical(value)
    assert set(value) == {
        "completion_schema",
        "completion_schema_version",
        "execution_protocol_version",
        "task_execution_pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "executor_django_ray_version",
        "success",
        "result",
        "result_reference",
        "error",
        "traceback",
        "exception_type",
        "retryable",
    }
    assert decoded.completion == ExecutionCompletion(
        identity=identity,
        execution_protocol_version=protocol,
        executor_django_ray_version="0.5.0",
        success=False,
        result=None,
        result_reference=None,
        error=f"execution request rejected: {classification.value}",
        traceback=None,
        exception_type="RayExecutionRequestIncompatible",
        retryable=False,
    )

    if protocol == 2:
        with pytest.raises(ValueError, match="execution completion is invalid"):
            encode_execution_completion(decoded.completion)


@pytest.mark.parametrize("classification", list(ExecutionRequestRejection))
def test_request_rejection_uses_only_fixed_classification_text(
    identity: ExecutionIdentity,
    classification: ExecutionRequestRejection,
) -> None:
    secret = "password=must-not-be-reflected"

    value = json.loads(
        encode_execution_request_rejection(
            expected_identity=identity,
            expected_execution_protocol_version=1,
            executor_django_ray_version="0.5.0",
            classification=classification,
        )
    )

    assert value["error"] == f"execution request rejected: {classification.value}"
    assert value["success"] is False
    assert value["result"] is None
    assert value["result_reference"] is None
    assert value["traceback"] is None
    assert value["exception_type"] == "RayExecutionRequestIncompatible"
    assert value["retryable"] is False
    assert secret not in json.dumps(value)


@pytest.mark.parametrize(
    "identity",
    [
        ExecutionIdentity(True, "task", 1, 0),
        ExecutionIdentity(0, "task", 1, 0),
        ExecutionIdentity(1, "", 1, 0),
        ExecutionIdentity(1, "task\x00forged", 1, 0),
        ExecutionIdentity(1, "task", True, 0),
        ExecutionIdentity(1, "task", 0, 0),
        ExecutionIdentity(1, "task", 1, True),
        ExecutionIdentity(1, "task", 1, -1),
    ],
)
def test_request_rejection_rejects_invalid_expected_identity(
    identity: ExecutionIdentity,
) -> None:
    with pytest.raises(ExecutionRequestEncodeError) as caught:
        encode_execution_request_rejection(
            expected_identity=identity,
            expected_execution_protocol_version=1,
            executor_django_ray_version="0.5.0",
            classification=ExecutionRequestRejection.INVALID_VERSIONED,
        )

    assert str(caught.value) == "execution request is invalid"
    assert "forged" not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_execution_protocol_version", True),
        ("expected_execution_protocol_version", 0),
        ("expected_execution_protocol_version", -1),
        ("expected_execution_protocol_version", 1 << 63),
        ("expected_execution_protocol_version", "2"),
        ("executor_django_ray_version", ""),
        ("executor_django_ray_version", "x" * 129),
        ("executor_django_ray_version", "0.5.0\x00password=secret"),
        ("executor_django_ray_version", "\ud800"),
        ("classification", "password=secret"),
    ],
)
def test_request_rejection_invalid_inputs_have_a_fixed_safe_error(
    identity: ExecutionIdentity,
    field: str,
    value: object,
) -> None:
    kwargs = {
        "expected_identity": identity,
        "expected_execution_protocol_version": 1,
        "executor_django_ray_version": "0.5.0",
        "classification": ExecutionRequestRejection.INVALID_VERSIONED,
    }
    kwargs[field] = value

    with pytest.raises(ExecutionRequestEncodeError) as caught:
        encode_execution_request_rejection(**kwargs)

    assert str(caught.value) == "execution request is invalid"
    assert "password" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_request_rejection_executor_version_and_resource_bounds_are_exact(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = encode_execution_request_rejection(
        expected_identity=identity,
        expected_execution_protocol_version=1,
        executor_django_ray_version="v" * 128,
        classification=ExecutionRequestRejection.RESOURCE_LIMIT,
    )
    decoded = _decode(serialized, identity)
    assert decoded.completion.executor_django_ray_version == "v" * 128

    monkeypatch.setattr(
        codec_module,
        "EXECUTION_COMPLETION_MAX_BYTES",
        len(serialized.encode("utf-8")) - 1,
    )
    with pytest.raises(ExecutionRequestEncodeError, match="execution request is invalid"):
        encode_execution_request_rejection(
            expected_identity=identity,
            expected_execution_protocol_version=1,
            executor_django_ray_version="v" * 128,
            classification=ExecutionRequestRejection.RESOURCE_LIMIT,
        )


def test_enriched_success_round_trips_as_exact_canonical_flat_schema(
    identity: ExecutionIdentity,
) -> None:
    completion = _success(identity)

    serialized = encode_execution_completion(completion)
    value = json.loads(serialized)
    decoded = _decode(serialized, identity)

    assert serialized == _canonical(value)
    assert set(value) == {
        "completion_schema",
        "completion_schema_version",
        "execution_protocol_version",
        "task_execution_pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "executor_django_ray_version",
        "success",
        "result",
        "result_reference",
        "error",
        "traceback",
        "exception_type",
        "retryable",
    }
    assert value["completion_schema"] == EXECUTION_COMPLETION_SCHEMA
    assert value["completion_schema_version"] == EXECUTION_COMPLETION_SCHEMA_VERSION
    assert decoded == DecodedExecutionCompletion(
        source=ExecutionCompletionSource.ACCEPTED_VERSIONED_V1,
        completion=completion,
    )


def test_enriched_failure_round_trips_with_executor_provenance(
    identity: ExecutionIdentity,
) -> None:
    completion = _failure(identity)

    decoded = _decode(encode_execution_completion(completion), identity)

    assert decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
    assert decoded.completion == completion
    assert decoded.completion.executor_django_ray_version == "0.5.0"


def test_encoder_normalizes_numeric_mapping_keys_before_canonical_sorting(
    identity: ExecutionIdentity,
) -> None:
    completion = replace(_success(identity), result={10: "ten", 2: "two"})

    serialized = encode_execution_completion(completion)
    decoded = _decode(serialized, identity)

    assert decoded.completion.result == {"10": "ten", "2": "two"}
    assert serialized.index('"10"') < serialized.index('"2"')


def test_encoder_rejects_keys_that_collide_after_json_stringification(
    identity: ExecutionIdentity,
) -> None:
    completion = replace(_success(identity), result={1: "numeric", "1": "text"})

    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(completion)


@pytest.mark.parametrize(
    "changes",
    [
        {"success": True, "error": "boom"},
        {"success": False, "result": 1},
        {"success": False, "error": None},
        {"success": False, "retryable": "yes"},
        {"result_reference": 1},
        {"execution_protocol_version": True},
        {"executor_django_ray_version": ""},
        {"executor_django_ray_version": "0.5.0\x00forged"},
        {"result_reference": "resultfs://valid\x00forged"},
    ],
)
def test_encoder_rejects_invalid_enriched_values(
    identity: ExecutionIdentity,
    changes: dict[str, object],
) -> None:
    completion = replace(_success(identity), **changes)

    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(completion)


@pytest.mark.parametrize(
    "identity",
    [
        ExecutionIdentity(True, "task", 1, 0),
        ExecutionIdentity(0, "task", 1, 0),
        ExecutionIdentity(1, "", 1, 0),
        ExecutionIdentity(1, "task\x00forged", 1, 0),
        ExecutionIdentity(1, "task", True, 0),
        ExecutionIdentity(1, "task", 0, 0),
        ExecutionIdentity(1, "task", 1, True),
        ExecutionIdentity(1, "task", 1, -1),
    ],
)
def test_encoder_rejects_invalid_identity(identity: ExecutionIdentity) -> None:
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(_success(identity))


@pytest.mark.parametrize(
    ("change", "classification"),
    [
        ({"completion_schema": "other"}, ExecutionCompletionRejection.UNSUPPORTED_SCHEMA),
        ({"completion_schema_version": 2}, ExecutionCompletionRejection.UNSUPPORTED_SCHEMA),
        ({"completion_schema_version": True}, ExecutionCompletionRejection.UNSUPPORTED_SCHEMA),
        ({"execution_protocol_version": 2}, ExecutionCompletionRejection.UNSUPPORTED_PROTOCOL),
        ({"execution_protocol_version": True}, ExecutionCompletionRejection.UNSUPPORTED_PROTOCOL),
        ({"success": 1}, ExecutionCompletionRejection.INVALID_VERSIONED),
        ({"result_reference": 1}, ExecutionCompletionRejection.INVALID_VERSIONED),
    ],
)
def test_enriched_schema_protocol_and_body_types_fail_closed(
    identity: ExecutionIdentity,
    change: dict[str, object],
    classification: ExecutionCompletionRejection,
) -> None:
    value = json.loads(encode_execution_completion(_success(identity)))
    value.update(change)

    _assert_rejection(
        _canonical(value),
        identity,
        classification,
        attempted_versioned=True,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_execution_pk", True),
        ("task_id", "x" * 256),
        ("attempt_number", 0),
        ("execution_generation", -1),
        ("executor_django_ray_version", ""),
    ],
)
def test_enriched_identity_and_executor_shape_are_strict(
    identity: ExecutionIdentity,
    field: str,
    replacement: object,
) -> None:
    value = json.loads(encode_execution_completion(_success(identity)))
    value[field] = replacement

    _assert_rejection(
        _canonical(value),
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"retryable": "yes"},
        {"result": 1},
        {"result_reference": "resultfs://unexpected"},
    ],
)
def test_enriched_failure_union_rejects_invalid_values(
    identity: ExecutionIdentity,
    change: dict[str, object],
) -> None:
    value = json.loads(encode_execution_completion(_failure(identity)))
    value.update(change)

    _assert_rejection(
        _canonical(value),
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


@pytest.mark.parametrize("field", ["error", "traceback", "exception_type"])
def test_encoder_rejects_nul_in_failure_diagnostics(
    identity: ExecutionIdentity,
    field: str,
) -> None:
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(replace(_failure(identity), **{field: "value\x00forged"}))


@pytest.mark.parametrize(
    ("field", "failure"),
    [
        ("task_id", False),
        ("executor_django_ray_version", False),
        ("result_reference", False),
        ("error", True),
        ("traceback", True),
        ("exception_type", True),
    ],
)
def test_decoder_rejects_nul_in_enriched_raw_persisted_scalars(
    identity: ExecutionIdentity,
    field: str,
    failure: bool,
) -> None:
    completion = _failure(identity) if failure else _success(identity)
    value = json.loads(encode_execution_completion(completion))
    value[field] = "value\x00forged"

    _assert_rejection(
        _canonical(value),
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


def test_enriched_requires_exact_keys(identity: ExecutionIdentity) -> None:
    value = json.loads(encode_execution_completion(_success(identity)))
    value["extra"] = "ignored only by legacy"
    _assert_rejection(
        _canonical(value),
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )

    del value["extra"]
    del value["retryable"]
    _assert_rejection(
        _canonical(value),
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_execution_pk", 42),
        ("task_id", "different"),
        ("attempt_number", 3),
        ("execution_generation", 4),
    ],
)
def test_enriched_identity_mismatch_is_distinct_and_untrusted(
    identity: ExecutionIdentity,
    field: str,
    replacement: object,
) -> None:
    value = json.loads(encode_execution_completion(_success(identity)))
    value[field] = replacement

    error = _assert_rejection(
        _canonical(value),
        identity,
        ExecutionCompletionRejection.IDENTITY_MISMATCH,
        attempted_versioned=True,
    )
    assert error.identity_verified is False


def test_supported_but_different_protocol_is_a_mismatch(identity: ExecutionIdentity) -> None:
    value = json.loads(encode_execution_completion(_success(identity)))
    value["execution_protocol_version"] = 2

    _assert_rejection(
        _canonical(value),
        identity,
        ExecutionCompletionRejection.PROTOCOL_MISMATCH,
        attempted_versioned=True,
        supported_protocols=ExecutionProtocolRange(1, 2),
    )


def test_noncanonical_enriched_json_is_rejected_after_identity_verification(
    identity: ExecutionIdentity,
) -> None:
    value = json.loads(encode_execution_completion(_success(identity)))
    noncanonical = json.dumps(value)

    error = _assert_rejection(
        noncanonical,
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )
    assert error.identity_verified is True


@pytest.mark.parametrize(
    "value",
    [
        {"success": True, "result": {"answer": 42}},
        {
            "success": True,
            "result": None,
            "result_reference": "resultfs://sha256/abc",
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        },
        {
            "success": False,
            "result": None,
            "error": "boom",
            "traceback": "trace",
            "exception_type": "ValueError",
            "retryable": True,
        },
        {
            "success": False,
            "result": "ignored legacy failure value",
            "result_reference": 123,
            "error": "boom",
        },
        {"success": True, "result": 42, "future_additive_key": {"ok": True}},
        {"success": True, "result": 1.25},
    ],
)
def test_legacy_v1_adapter_preserves_deployed_shapes_and_additive_keys(
    identity: ExecutionIdentity,
    value: dict[str, object],
) -> None:
    decoded = _decode(json.dumps(value), identity)

    assert decoded.source is ExecutionCompletionSource.ACCEPTED_LEGACY_V1
    assert decoded.completion.identity == identity
    assert decoded.completion.execution_protocol_version == 1
    assert decoded.completion.executor_django_ray_version is None
    if decoded.completion.success:
        assert decoded.completion.result == value["result"]
    else:
        assert decoded.completion.result is None
        assert decoded.completion.result_reference is None


def test_explicit_legacy_adapter_is_bounded_and_rejects_reserved_keys(
    identity: ExecutionIdentity,
) -> None:
    accepted = decode_legacy_v1_completion(
        '{"success":true,"result":42}',
        expected_identity=identity,
        expected_execution_protocol_version=1,
    )
    assert accepted.source is ExecutionCompletionSource.ACCEPTED_LEGACY_V1

    with pytest.raises(ExecutionCompletionDecodeError) as caught:
        decode_legacy_v1_completion(
            '{"success":true,"result":42,"task_id":"task-41"}',
            expected_identity=identity,
            expected_execution_protocol_version=1,
        )
    assert caught.value.classification is ExecutionCompletionRejection.INVALID_VERSIONED
    assert caught.value.attempted_versioned is True


@pytest.mark.parametrize(
    "reserved_key",
    [
        "completion_schema",
        "completion_schema_version",
        "execution_protocol_version",
        "task_execution_pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "executor_django_ray_version",
    ],
)
def test_any_reserved_header_key_prevents_legacy_downgrade(
    identity: ExecutionIdentity,
    reserved_key: str,
) -> None:
    value = {"success": True, "result": 42, reserved_key: None}

    _assert_rejection(
        json.dumps(value),
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


@pytest.mark.parametrize(
    ("serialized", "attempted_versioned", "classification"),
    [
        (
            '{"success":true,"result":1,"result":2}',
            False,
            ExecutionCompletionRejection.MALFORMED_LEGACY,
        ),
        (
            '{"success":true,"result":{"key":1,"key":2}}',
            False,
            ExecutionCompletionRejection.MALFORMED_LEGACY,
        ),
        (
            '{"completion_schema":"django-ray.execution-completion",'
            '"success":true,"result":1,"result":2}',
            True,
            ExecutionCompletionRejection.INVALID_VERSIONED,
        ),
    ],
)
def test_duplicate_json_keys_are_rejected(
    identity: ExecutionIdentity,
    serialized: str,
    attempted_versioned: bool,
    classification: ExecutionCompletionRejection,
) -> None:
    _assert_rejection(
        serialized,
        identity,
        classification,
        attempted_versioned=attempted_versioned,
    )


def test_legacy_v1_accepts_released_escaped_unpaired_surrogate_result(
    identity: ExecutionIdentity,
) -> None:
    result = "\ud800"
    serialized = json.dumps(
        {
            "success": True,
            "result": result,
            "result_reference": None,
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        }
    )

    decoded = _decode(serialized, identity)

    assert decoded.source is ExecutionCompletionSource.ACCEPTED_LEGACY_V1
    assert decoded.completion.result == result


def test_enriched_rejects_unpaired_surrogate_result(identity: ExecutionIdentity) -> None:
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(replace(_success(identity), result="\ud800"))

    value = json.loads(encode_execution_completion(_success(identity)))
    value["result"] = "\ud800"
    _assert_rejection(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


def test_raw_surrogate_with_reserved_marker_requires_nonretryable_disposition(
    identity: ExecutionIdentity,
) -> None:
    serialized = (
        '{"completion_schema":"django-ray.execution-completion","result":"' + "\ud800" + '"}'
    )

    error = _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )
    assert error.requires_nonretryable_disposition is True


def test_raw_surrogate_without_reserved_marker_is_malformed_legacy(
    identity: ExecutionIdentity,
) -> None:
    serialized = '{"success":true,"result":"' + "\ud800" + '"}'

    error = _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.MALFORMED_LEGACY,
        attempted_versioned=False,
    )
    assert error.requires_nonretryable_disposition is False


@pytest.mark.parametrize(
    ("result", "expected_sign"),
    [(float("nan"), 0), (float("inf"), 1), (float("-inf"), -1)],
)
def test_legacy_v1_accepts_released_nonfinite_success_shape(
    identity: ExecutionIdentity,
    result: float,
    expected_sign: int,
) -> None:
    serialized = json.dumps(
        {
            "success": True,
            "result": result,
            "result_reference": None,
            "error": None,
            "traceback": None,
            "exception_type": None,
            "retryable": None,
        }
    )

    decoded = _decode(serialized, identity)
    decoded_result = decoded.completion.result

    assert decoded.source is ExecutionCompletionSource.ACCEPTED_LEGACY_V1
    assert isinstance(decoded_result, float)
    if expected_sign == 0:
        assert math.isnan(decoded_result)
    else:
        assert math.isinf(decoded_result)
        assert math.copysign(1, decoded_result) == expected_sign


@pytest.mark.parametrize("result", [float("nan"), float("inf"), float("-inf")])
def test_enriched_rejects_nonfinite_results(
    identity: ExecutionIdentity,
    result: float,
) -> None:
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(replace(_success(identity), result=result))

    value = json.loads(encode_execution_completion(_success(identity)))
    value["result"] = result
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


def test_enriched_float_parser_rejects_overflow_and_accepts_finite_values(
    identity: ExecutionIdentity,
) -> None:
    finite = replace(_success(identity), result=1.25)
    assert _decode(encode_execution_completion(finite), identity).completion.result == 1.25

    serialized = encode_execution_completion(_success(identity)).replace(
        '"result":{"answer":[42]}',
        '"result":1e999',
    )
    _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


def test_encoded_byte_limit_is_checked_before_decoding(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = '{"success":true,"result":"é"}'
    size = len(serialized.encode("utf-8"))
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_BYTES", size)
    assert _decode(serialized, identity).completion.result == "é"

    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_BYTES", size - 1)
    _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )


def test_oversize_is_nonretryable_before_reserved_marker_scan(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_BYTES", 32)
    serialized = '{"completion_schema":' + "x" * 32

    def fail_scan(_serialized: str) -> bool:
        pytest.fail("oversized input reached the reserved-key scanner")

    monkeypatch.setattr(codec_module, "_preparse_json_scan", fail_scan)
    error = _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )
    assert error.requires_nonretryable_disposition is True


def test_json_depth_limit_has_an_exact_boundary(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_DEPTH", 2)
    assert _decode('{"success":true,"result":[]}', identity).completion.result == []

    _assert_rejection(
        '{"success":true,"result":[[]]}',
        identity,
        ExecutionCompletionRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )


def test_diagnostics_are_utf8_bounded(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES", 4)
    serialized = encode_execution_completion(
        replace(
            _failure(identity),
            error="éé",
            traceback=None,
            exception_type=None,
        )
    )
    accepted = _decode(serialized, identity)
    assert accepted.completion.error == "éé"

    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES", 3)
    _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )


def test_legacy_v1_accepts_released_long_nonretryable_failure_shape(
    identity: ExecutionIdentity,
) -> None:
    long_text = "x" * (64 * 1024 + 1)
    serialized = json.dumps(
        {
            "success": False,
            "result": None,
            "error": long_text,
            "traceback": long_text,
            "exception_type": long_text,
            "retryable": False,
        }
    )

    decoded = _decode(serialized, identity)

    assert decoded.source is ExecutionCompletionSource.ACCEPTED_LEGACY_V1
    assert decoded.completion.error == long_text
    assert decoded.completion.traceback == long_text
    assert decoded.completion.exception_type == long_text
    assert decoded.completion.retryable is False


def test_legacy_v1_retains_released_nul_behavior(identity: ExecutionIdentity) -> None:
    failure = _decode(
        json.dumps(
            {
                "success": False,
                "result": None,
                "error": "error\x00detail\ud800",
                "traceback": "trace\x00detail",
                "exception_type": "Type\x00detail",
                "retryable": False,
            }
        ),
        identity,
    )
    success = _decode(
        json.dumps(
            {
                "success": True,
                "result": None,
                "result_reference": "legacy\x00reference",
            }
        ),
        identity,
    )

    assert failure.completion.error == "error\x00detail\ud800"
    assert failure.completion.traceback == "trace\x00detail"
    assert failure.completion.exception_type == "Type\x00detail"
    assert success.completion.result_reference == "legacy\x00reference"


def test_legacy_success_validates_present_optional_field_types(
    identity: ExecutionIdentity,
) -> None:
    _assert_rejection(
        json.dumps({"success": True, "result": 42, "retryable": "yes"}),
        identity,
        ExecutionCompletionRejection.MALFORMED_LEGACY,
        attempted_versioned=False,
    )


def test_legacy_result_reference_has_a_storage_field_bound(
    identity: ExecutionIdentity,
) -> None:
    _assert_rejection(
        json.dumps({"success": True, "result": None, "result_reference": "x" * 501}),
        identity,
        ExecutionCompletionRejection.MALFORMED_LEGACY,
        attempted_versioned=False,
    )


def test_wide_json_is_rejected_by_preparse_node_budget(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_NODES", 10)

    def fail_parse(*_args, **_kwargs):
        pytest.fail("node-budget violation reached json.loads")

    monkeypatch.setattr(codec_module.json, "loads", fail_parse)
    _assert_rejection(
        "[0,0,0,0,0,0,0,0,0,0]",
        identity,
        ExecutionCompletionRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )


def test_json_node_limit_has_an_exact_boundary(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = '{"success":true,"result":[null,null]}'
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_NODES", 7)
    assert _decode(serialized, identity).completion.result == [None, None]

    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_NODES", 6)
    _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.RESOURCE_LIMIT,
        attempted_versioned=False,
    )


def test_postparse_tree_validation_uses_depth_bounded_auxiliary_memory() -> None:
    wide = [None] * 200_000

    tracemalloc.start()
    try:
        codec_module._validate_json_tree(wide, allow_nonfinite=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 2 * 1024 * 1024


def test_encoder_enforces_depth_bytes_and_json_types(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = _success(identity)
    serialized = encode_execution_completion(completion)

    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_DEPTH", 1)
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(completion)

    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_DEPTH", 64)
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_BYTES", len(serialized) - 1)
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(completion)

    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_BYTES", 128 * 1024 * 1024)
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(replace(completion, result=object()))


def test_encoder_stops_aggregate_json_before_detached_parse(
    identity: ExecutionIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = replace(_success(identity), result=["abcdefghij"] * 20)
    serialized = encode_execution_completion(completion)
    byte_limit = len(serialized.encode("utf-8")) - 1
    assert all(len(item.encode("utf-8")) < byte_limit for item in completion.result)
    monkeypatch.setattr(codec_module, "EXECUTION_COMPLETION_MAX_BYTES", byte_limit)

    def fail_parse(*_args, **_kwargs):
        pytest.fail("aggregate-over-cap JSON reached detached parsing")

    monkeypatch.setattr(codec_module, "_bounded_json_loads", fail_parse)
    with pytest.raises(ValueError, match="execution completion is invalid"):
        encode_execution_completion(completion)


@pytest.mark.parametrize("serialized", [None, b"{}", [], {}, "[1]", "not-json", "\ud800"])
def test_invalid_framing_has_a_fixed_secret_safe_legacy_classification(
    identity: ExecutionIdentity,
    serialized: object,
) -> None:
    error = _assert_rejection(
        serialized,
        identity,
        ExecutionCompletionRejection.MALFORMED_LEGACY,
        attempted_versioned=False,
    )
    assert "not-json" not in str(error)


def test_malformed_unversioned_data_cannot_be_claimed_by_protocol_v2(
    identity: ExecutionIdentity,
) -> None:
    _assert_rejection(
        "not-json",
        identity,
        ExecutionCompletionRejection.PROTOCOL_MISMATCH,
        attempted_versioned=False,
        expected_protocol=2,
        supported_protocols=ExecutionProtocolRange(1, 2),
    )


def test_valid_legacy_data_cannot_be_claimed_by_protocol_v2(
    identity: ExecutionIdentity,
) -> None:
    error = _assert_rejection(
        '{"success":true,"result":42}',
        identity,
        ExecutionCompletionRejection.PROTOCOL_MISMATCH,
        attempted_versioned=False,
        expected_protocol=2,
        supported_protocols=ExecutionProtocolRange(1, 2),
    )
    assert error.requires_nonretryable_disposition is True


def test_legacy_v1_is_rejected_when_worker_range_does_not_support_it(
    identity: ExecutionIdentity,
) -> None:
    _assert_rejection(
        '{"success":true,"result":42}',
        identity,
        ExecutionCompletionRejection.UNSUPPORTED_PROTOCOL,
        attempted_versioned=False,
        supported_protocols=ExecutionProtocolRange(2, 2),
    )


def test_reserved_key_in_malformed_json_still_suppresses_legacy_grace(
    identity: ExecutionIdentity,
) -> None:
    _assert_rejection(
        '{"completion_schema":"django-ray.execution-completion","secret":',
        identity,
        ExecutionCompletionRejection.INVALID_VERSIONED,
        attempted_versioned=True,
    )
