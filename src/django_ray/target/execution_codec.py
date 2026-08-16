"""Canonical protocol-2 transport for target-bound Ray execution.

Protocol 2 is intentionally dormant: this module can construct and validate
its wire contract without expanding the package's advertised 1..1 execution
protocol range.  The request binds one exact claim-generation evidence row to
its immutable target expectation and full cluster attestation.  A result is
either a verifier-passed application completion or a proven pre-invocation
compatibility rejection; transport uncertainty has no wire representation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from io import StringIO
from typing import Any, Never

from django_ray.execution_codec import (
    EXECUTION_COMPLETION_MAX_BYTES,
    EXECUTION_REQUEST_MAX_BYTES,
    ExecutionIdentity,
)
from django_ray.execution_protocol import TARGET_EXECUTION_PROTOCOL_VERSION
from django_ray.target.attestation import (
    RAY_CLUSTER_ATTESTATION_MAX_BYTES,
    RAY_TARGET_EXPECTATION_MAX_BYTES,
    RayClusterAttestation,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetAttestationEncodeError,
    RayTargetAttestationError,
    RayTargetExpectation,
    decode_ray_cluster_attestation,
    decode_ray_target_expectation,
    encode_ray_cluster_attestation,
    encode_ray_target_expectation,
    ray_cluster_attestation_digest,
    ray_target_expectation_digest,
)

TARGET_EXECUTION_REQUEST_SCHEMA = "django-ray.target-execution-request"
TARGET_EXECUTION_REQUEST_SCHEMA_VERSION = 2
TARGET_EXECUTION_RESULT_SCHEMA = "django-ray.target-execution-result"
TARGET_EXECUTION_RESULT_SCHEMA_VERSION = 2

TARGET_EXECUTION_METADATA_MAX_BYTES = 64 * 1024
TARGET_EXECUTION_REQUEST_MAX_BYTES = (
    EXECUTION_REQUEST_MAX_BYTES
    + RAY_CLUSTER_ATTESTATION_MAX_BYTES
    + RAY_TARGET_EXPECTATION_MAX_BYTES
    + TARGET_EXECUTION_METADATA_MAX_BYTES
)
TARGET_EXECUTION_RESULT_MAX_BYTES = (
    EXECUTION_COMPLETION_MAX_BYTES + TARGET_EXECUTION_METADATA_MAX_BYTES
)
TARGET_EXECUTION_MAX_DEPTH = 64
TARGET_EXECUTION_MAX_NODES = 1_000_000
TARGET_EXECUTION_DIAGNOSTIC_MAX_BYTES = 64 * 1024
TARGET_EXECUTION_RUNTIME_ENV_IDENTITY_MAX_BYTES = 16 * 1024

_MAX_COUNTER = (1 << 63) - 1
_MAX_POSITIVE_INTEGER = (1 << 31) - 1
_TASK_ID_MAX_CHARS = 255
_CALLABLE_PATH_MAX_CHARS = 500
_INPUT_REFERENCE_MAX_CHARS = 500
_RUNTIME_ENV_PROFILE_MAX_CHARS = 100
_EXECUTOR_VERSION_MAX_CHARS = 128
_RESULT_REFERENCE_MAX_CHARS = 500
_EXCEPTION_TYPE_MAX_BYTES = 512
_UTF8_CHUNK_CHARS = 64 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_RUNTIME_ENV_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_RUNTIME_ENV_HASH = re.compile(r"[0-9a-f]{64}")
_NODE_ID = re.compile(r"[0-9a-f]{56}")
_CLUSTER_SESSION = re.compile(r"session_[A-Za-z0-9][A-Za-z0-9_.-]{0,247}")
_PYTHON_IMPLEMENTATION = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_COMPILED_GRAPH_SUBMISSION_TRANSPORTS = frozenset({"direct-ray-core", "ray-client", "ray-job"})
_OBSERVED_PROOF_DOMAIN = b"django-ray/target-execution-observed-proof/v2\x00"

_REQUEST_KEYS = frozenset(
    {
        "request_schema",
        "request_schema_version",
        "execution_protocol_version",
        "task_execution_pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "target_execution_evidence_id",
        "target_execution_evidence_digest",
        "target_execution_claimed_at",
        "target_expectation",
        "target_expectation_digest",
        "claim_attestation",
        "claim_attestation_digest",
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
)
_RESULT_COMMON_KEYS = frozenset(
    {
        "result_schema",
        "result_schema_version",
        "result_kind",
        "execution_protocol_version",
        "task_execution_pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "target_execution_evidence_id",
        "target_execution_evidence_digest",
        "target_execution_claimed_at",
        "target_expectation_digest",
        "claim_attestation_digest",
        "executor_django_ray_version",
        "application_invoked",
        "observed_target",
    }
)
_OBSERVED_TARGET_KEYS = frozenset(
    {
        "observed_node_id",
        "observed_cluster_session",
        "observed_runtime",
        "observed_membership_digest",
        "observed_at",
        "observed_proof_digest",
    }
)
_RUNTIME_KEYS = frozenset(
    {
        "ray_major",
        "ray_minor",
        "ray_patch",
        "python_implementation",
        "python_major",
        "python_minor",
        "python_patch",
    }
)
_APPLICATION_COMPLETION_KEYS = frozenset(
    {
        "success",
        "result",
        "result_reference",
        "error",
        "traceback",
        "exception_type",
        "retryable",
    }
)


@dataclass(frozen=True, slots=True)
class TargetExecutionRequest:
    """One exact protocol-2 request, including immutable claim evidence."""

    identity: ExecutionIdentity
    execution_protocol_version: int
    target_execution_evidence_id: int
    target_execution_evidence_digest: str
    target_execution_claimed_at: datetime
    target_expectation: RayTargetExpectation
    target_expectation_digest: str
    claim_attestation: RayClusterAttestation
    claim_attestation_digest: str
    callable_path: str
    transport_version: int
    serialized_args: str
    serialized_kwargs: str
    input_reference: str | None
    runtime_env_profile: str | None
    runtime_env_hash: str
    runtime_env_plan_identity: dict[str, Any]
    compiled_graph_submission_transport: str | None


@dataclass(frozen=True, slots=True)
class TargetApplicationCompletion:
    """Normalized application outcome nested inside a verified result."""

    success: bool
    result: Any
    result_reference: str | None
    error: str | None
    traceback: str | None
    exception_type: str | None
    retryable: bool | None


@dataclass(frozen=True, slots=True)
class TargetExecutionObservedEvidence:
    """Bounded actual target proof collected by the remote executor."""

    observed_node_id: str
    observed_cluster_session: str
    observed_runtime: RayRuntimeVersion
    observed_membership_digest: str
    observed_at: datetime
    observed_proof_digest: str


class TargetExecutionResultKind(StrEnum):
    """Disjoint trusted protocol-2 result variants."""

    COMPLETION = "completion"
    COMPATIBILITY_REJECTION = "compatibility_rejection"


class TargetExecutionCompatibilityReason(StrEnum):
    """Fixed mismatches that may be asserted only with a complete proof."""

    EXPIRED = "expired"
    CLUSTER_SESSION_MISMATCH = "cluster_session_mismatch"
    RAY_VERSION_MISMATCH = "ray_version_mismatch"
    PYTHON_IMPLEMENTATION_MISMATCH = "python_implementation_mismatch"
    PYTHON_VERSION_MISMATCH = "python_version_mismatch"
    CURRENT_NODE_NOT_ATTESTED = "current_node_not_attested"
    MEMBERSHIP_MISMATCH = "membership_mismatch"


@dataclass(frozen=True, slots=True)
class TargetExecutionCompletion:
    """Verifier-passed result proving that application invocation occurred."""

    identity: ExecutionIdentity
    execution_protocol_version: int
    target_execution_evidence_id: int
    target_execution_evidence_digest: str
    target_execution_claimed_at: datetime
    target_expectation_digest: str
    claim_attestation_digest: str
    executor_django_ray_version: str
    observed_target: TargetExecutionObservedEvidence
    application_completion: TargetApplicationCompletion


@dataclass(frozen=True, slots=True)
class TargetExecutionCompatibilityRejection:
    """Proven target mismatch returned before any application seam was imported."""

    identity: ExecutionIdentity
    execution_protocol_version: int
    target_execution_evidence_id: int
    target_execution_evidence_digest: str
    target_execution_claimed_at: datetime
    target_expectation_digest: str
    claim_attestation_digest: str
    executor_django_ray_version: str
    observed_target: TargetExecutionObservedEvidence
    compatibility_reason: TargetExecutionCompatibilityReason


TargetExecutionResult = TargetExecutionCompletion | TargetExecutionCompatibilityRejection


class TargetExecutionRequestRejection(StrEnum):
    """Secret-safe protocol-2 request rejection classifications."""

    INVALID = "invalid"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    IDENTITY_MISMATCH = "identity_mismatch"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    RESOURCE_LIMIT = "resource_limit"


class TargetExecutionResultRejection(StrEnum):
    """Secret-safe result failures, all of which imply runner uncertainty."""

    INVALID = "invalid"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    IDENTITY_MISMATCH = "identity_mismatch"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    PROOF_MISMATCH = "proof_mismatch"
    RESOURCE_LIMIT = "resource_limit"


class TargetExecutionRequestDecodeError(ValueError):
    """Reject an untrusted protocol-2 request without retaining its contents."""

    def __init__(self, classification: TargetExecutionRequestRejection) -> None:
        self.classification = classification
        super().__init__(f"target execution request rejected: {classification.value}")


class TargetExecutionRequestEncodeError(ValueError):
    """Report a fixed request construction failure."""

    def __init__(self, classification: TargetExecutionRequestRejection) -> None:
        self.classification = classification
        super().__init__("target execution request is invalid")


class TargetExecutionResultDecodeError(ValueError):
    """Reject an untrusted result; callers must retain uncertain semantics."""

    def __init__(self, classification: TargetExecutionResultRejection) -> None:
        self.classification = classification
        super().__init__(f"target execution result rejected: {classification.value}")


class TargetExecutionResultEncodeError(ValueError):
    """Report a fixed result construction failure."""

    def __init__(self, classification: TargetExecutionResultRejection) -> None:
        self.classification = classification
        super().__init__("target execution result is invalid")


class _Invalid(ValueError):  # noqa: N818 - private control-flow sentinel
    pass


class _ResourceLimit(ValueError):  # noqa: N818 - private control-flow sentinel
    pass


class _DuplicateKey(ValueError):  # noqa: N818 - private parser sentinel
    pass


def _reject_request(classification: TargetExecutionRequestRejection) -> Never:
    raise TargetExecutionRequestDecodeError(classification) from None


def _reject_result(classification: TargetExecutionResultRejection) -> Never:
    raise TargetExecutionResultDecodeError(classification) from None


def _bounded_utf8_size(value: str, *, max_bytes: int) -> int:
    total = 0
    for start in range(0, len(value), _UTF8_CHUNK_CHARS):
        total += len(value[start : start + _UTF8_CHUNK_CHARS].encode("utf-8"))
        if total > max_bytes:
            raise _ResourceLimit
    return total


def _dict_children(value: dict[Any, Any]) -> Iterator[Any]:
    for key, item in value.items():
        yield key
        yield item


def _validate_json_tree(
    value: object,
    *,
    max_bytes: int,
    max_depth: int = TARGET_EXECUTION_MAX_DEPTH,
    max_nodes: int = TARGET_EXECUTION_MAX_NODES,
    allow_nul: bool = True,
) -> None:
    nodes = 0
    ancestors: set[int] = set()
    stack: list[tuple[Iterator[Any], int, int | None]] = [(iter((value,)), 0, None)]
    while stack:
        children, parent_depth, container_id = stack[-1]
        try:
            item = next(children)
        except StopIteration:
            stack.pop()
            if container_id is not None:
                ancestors.remove(container_id)
            continue
        nodes += 1
        if nodes > max_nodes:
            raise _ResourceLimit
        if isinstance(item, str):
            if not allow_nul and "\x00" in item:
                raise _Invalid
            try:
                _bounded_utf8_size(item, max_bytes=max_bytes)
            except UnicodeEncodeError as error:
                raise _Invalid from error
            continue
        if item is None or type(item) in {bool, int}:
            if type(item) is int and abs(item) > _MAX_COUNTER:
                raise _ResourceLimit
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise _Invalid
            continue
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise _Invalid
            nested = _dict_children(item)
        elif type(item) in {list, tuple}:
            nested = iter(item)
        else:
            raise _Invalid
        depth = parent_depth + 1
        if depth > max_depth:
            raise _ResourceLimit
        identity = id(item)
        if identity in ancestors:
            raise _Invalid
        ancestors.add(identity)
        stack.append((nested, depth, identity))


def _canonical_json(value: object, *, max_bytes: int) -> str:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    output = StringIO()
    encoded_bytes = 0
    try:
        for chunk in encoder.iterencode(value):
            encoded_bytes += _bounded_utf8_size(chunk, max_bytes=max_bytes - encoded_bytes)
            output.write(chunk)
    except (TypeError, ValueError, UnicodeError) as error:
        raise _Invalid from error
    return output.getvalue()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _bounded_int(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        raise _ResourceLimit
    parsed = int(value)
    if abs(parsed) > _MAX_COUNTER:
        raise _ResourceLimit
    return parsed


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _Invalid
    return parsed


def _reject_constant(_value: str) -> Never:
    raise _Invalid


def _loads(serialized: object, *, max_bytes: int) -> object:
    if type(serialized) is not str:
        raise _Invalid
    try:
        _bounded_utf8_size(serialized, max_bytes=max_bytes)
    except UnicodeEncodeError as error:
        raise _Invalid from error
    try:
        value = json.loads(
            serialized,
            object_pairs_hook=_unique_object,
            parse_int=_bounded_int,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except (_DuplicateKey, _Invalid, _ResourceLimit):
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise _Invalid from error
    _validate_json_tree(value, max_bytes=max_bytes)
    return value


def _positive_counter(value: object) -> int:
    if type(value) is not int or not 0 < value <= _MAX_COUNTER:
        raise _Invalid
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _Invalid
    return value


def _bounded_text(
    value: object,
    *,
    max_chars: int | None = None,
    max_bytes: int | None = None,
    nullable: bool = False,
    nonempty: bool = True,
) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or (nonempty and not value) or "\x00" in value:
        raise _Invalid
    if max_chars is not None and len(value) > max_chars:
        raise _ResourceLimit
    if max_bytes is not None:
        _bounded_utf8_size(value, max_bytes=max_bytes)
    return value


def _timestamp(value: datetime) -> str:
    try:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise _Invalid
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except _Invalid:
        raise
    except Exception:
        raise _Invalid from None


def _decode_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise _Invalid
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise _Invalid from error
    if _timestamp(parsed) != value:
        raise _Invalid
    return parsed.astimezone(UTC)


def _runtime_wire(runtime: RayRuntimeVersion) -> dict[str, object]:
    if type(runtime) is not RayRuntimeVersion:
        raise _Invalid
    value = {
        "ray_major": runtime.ray_major,
        "ray_minor": runtime.ray_minor,
        "ray_patch": runtime.ray_patch,
        "python_implementation": runtime.python_implementation,
        "python_major": runtime.python_major,
        "python_minor": runtime.python_minor,
        "python_patch": runtime.python_patch,
    }
    decoded = _decode_runtime(value)
    if decoded != runtime:
        raise _Invalid
    return value


def _decode_runtime(value: object) -> RayRuntimeVersion:
    if type(value) is not dict or frozenset(value) != _RUNTIME_KEYS:
        raise _Invalid
    for field in ("ray_major", "python_major"):
        component = value[field]
        if type(component) is not int or not 0 < component <= _MAX_COUNTER:
            raise _Invalid
    for field in ("ray_minor", "ray_patch", "python_minor", "python_patch"):
        component = value[field]
        if type(component) is not int or not 0 <= component <= _MAX_COUNTER:
            raise _Invalid
    implementation = _bounded_text(value["python_implementation"], max_chars=64)
    if implementation is None or _PYTHON_IMPLEMENTATION.fullmatch(implementation) is None:
        raise _Invalid
    runtime = RayRuntimeVersion(
        ray_major=value["ray_major"],
        ray_minor=value["ray_minor"],
        ray_patch=value["ray_patch"],
        python_implementation=implementation,
        python_major=value["python_major"],
        python_minor=value["python_minor"],
        python_patch=value["python_patch"],
    )
    return runtime


def _is_valid_target_execution_identity(identity: object) -> bool:
    return (
        type(identity) is ExecutionIdentity
        and type(identity.task_execution_pk) is int
        and 0 < identity.task_execution_pk <= _MAX_COUNTER
        and type(identity.task_id) is str
        and 0 < len(identity.task_id) <= _TASK_ID_MAX_CHARS
        and "\x00" not in identity.task_id
        and type(identity.attempt_number) is int
        and 0 < identity.attempt_number <= _MAX_POSITIVE_INTEGER
        and type(identity.execution_generation) is int
        and 0 < identity.execution_generation <= _MAX_COUNTER
    )


def _identity_from_wire(value: dict[str, Any]) -> ExecutionIdentity:
    identity = ExecutionIdentity(
        task_execution_pk=value["task_execution_pk"],
        task_id=value["task_id"],
        attempt_number=value["attempt_number"],
        execution_generation=value["execution_generation"],
    )
    if not _is_valid_target_execution_identity(identity):
        raise _Invalid
    return identity


def _identity_wire(identity: ExecutionIdentity) -> dict[str, object]:
    if not _is_valid_target_execution_identity(identity):
        raise _Invalid
    return {
        "task_execution_pk": identity.task_execution_pk,
        "task_id": identity.task_id,
        "attempt_number": identity.attempt_number,
        "execution_generation": identity.execution_generation,
    }


def _embedded_canonical(serialized: str) -> dict[str, Any]:
    value = json.loads(serialized)
    if type(value) is not dict:
        raise _Invalid
    return value


def _decode_embedded_expectation(value: object) -> RayTargetExpectation:
    try:
        serialized = _canonical_json(value, max_bytes=RAY_TARGET_EXPECTATION_MAX_BYTES)
        return decode_ray_target_expectation(serialized)
    except (RayTargetAttestationError, _Invalid, _ResourceLimit):
        raise _Invalid from None


def _decode_embedded_attestation(value: object) -> RayClusterAttestation:
    try:
        serialized = _canonical_json(value, max_bytes=RAY_CLUSTER_ATTESTATION_MAX_BYTES)
        return decode_ray_cluster_attestation(serialized)
    except (RayTargetAttestationError, _Invalid, _ResourceLimit):
        raise _Invalid from None


def _normalize_application_completion(value: object) -> TargetApplicationCompletion:
    if type(value) is not dict or frozenset(value) != _APPLICATION_COMPLETION_KEYS:
        raise _Invalid
    success = value["success"]
    if type(success) is not bool:
        raise _Invalid
    result_reference = _bounded_text(
        value["result_reference"],
        max_chars=_RESULT_REFERENCE_MAX_CHARS,
        nullable=True,
    )
    error = _bounded_text(
        value["error"],
        max_bytes=TARGET_EXECUTION_DIAGNOSTIC_MAX_BYTES,
        nullable=True,
    )
    traceback = _bounded_text(
        value["traceback"],
        max_bytes=TARGET_EXECUTION_DIAGNOSTIC_MAX_BYTES,
        nullable=True,
    )
    exception_type = _bounded_text(
        value["exception_type"],
        max_bytes=_EXCEPTION_TYPE_MAX_BYTES,
        nullable=True,
    )
    retryable = value["retryable"]
    if retryable is not None and type(retryable) is not bool:
        raise _Invalid
    if success:
        if any(item is not None for item in (error, traceback, exception_type, retryable)):
            raise _Invalid
    elif value["result"] is not None or result_reference is not None or error is None:
        raise _Invalid
    return TargetApplicationCompletion(
        success=success,
        result=value["result"],
        result_reference=result_reference,
        error=error,
        traceback=traceback,
        exception_type=exception_type,
        retryable=retryable,
    )


def decode_target_application_completion(serialized: object) -> TargetApplicationCompletion:
    """Decode the application body returned after protocol-2 verification."""
    try:
        value = _loads(serialized, max_bytes=EXECUTION_COMPLETION_MAX_BYTES)
        return _normalize_application_completion(value)
    except _ResourceLimit:
        raise TargetExecutionResultDecodeError(
            TargetExecutionResultRejection.RESOURCE_LIMIT
        ) from None
    except (_Invalid, _DuplicateKey, TypeError, ValueError, UnicodeError):
        raise TargetExecutionResultDecodeError(TargetExecutionResultRejection.INVALID) from None


def _application_completion_wire(value: TargetApplicationCompletion) -> dict[str, Any]:
    if type(value) is not TargetApplicationCompletion:
        raise _Invalid
    wire = {
        "success": value.success,
        "result": value.result,
        "result_reference": value.result_reference,
        "error": value.error,
        "traceback": value.traceback,
        "exception_type": value.exception_type,
        "retryable": value.retryable,
    }
    return {
        "success": _normalize_application_completion(wire).success,
        "result": wire["result"],
        "result_reference": wire["result_reference"],
        "error": wire["error"],
        "traceback": wire["traceback"],
        "exception_type": wire["exception_type"],
        "retryable": wire["retryable"],
    }


def _normalize_request_body(value: dict[str, Any]) -> dict[str, Any]:
    callable_path = _bounded_text(value["callable_path"], max_chars=_CALLABLE_PATH_MAX_CHARS)
    if callable_path is None or "." not in callable_path:
        raise _Invalid
    transport_version = value["transport_version"]
    if type(transport_version) is not int or transport_version not in (1, 2):
        raise _Invalid
    serialized_args = _bounded_text(value["serialized_args"])
    serialized_kwargs = _bounded_text(value["serialized_kwargs"])
    assert serialized_args is not None and serialized_kwargs is not None
    input_reference = _bounded_text(
        value["input_reference"], max_chars=_INPUT_REFERENCE_MAX_CHARS, nullable=True
    )
    if transport_version == 1:
        if input_reference is not None:
            raise _Invalid
    elif input_reference is None or serialized_args != "null" or serialized_kwargs != "null":
        raise _Invalid
    runtime_env_profile = _bounded_text(
        value["runtime_env_profile"],
        max_chars=_RUNTIME_ENV_PROFILE_MAX_CHARS,
        nullable=True,
    )
    if (
        runtime_env_profile is not None
        and _RUNTIME_ENV_PROFILE.fullmatch(runtime_env_profile) is None
    ):
        raise _Invalid
    runtime_env_hash = value["runtime_env_hash"]
    if type(runtime_env_hash) is not str or _RUNTIME_ENV_HASH.fullmatch(runtime_env_hash) is None:
        raise _Invalid
    runtime_env_plan_identity = value["runtime_env_plan_identity"]
    if type(runtime_env_plan_identity) is not dict:
        raise _Invalid
    _validate_json_tree(
        runtime_env_plan_identity,
        max_bytes=TARGET_EXECUTION_RUNTIME_ENV_IDENTITY_MAX_BYTES,
        allow_nul=False,
    )
    _canonical_json(
        runtime_env_plan_identity,
        max_bytes=TARGET_EXECUTION_RUNTIME_ENV_IDENTITY_MAX_BYTES,
    )
    submission_transport = value["compiled_graph_submission_transport"]
    if submission_transport is not None and (
        type(submission_transport) is not str
        or submission_transport not in _COMPILED_GRAPH_SUBMISSION_TRANSPORTS
    ):
        raise _Invalid
    return {
        "callable_path": callable_path,
        "transport_version": transport_version,
        "serialized_args": serialized_args,
        "serialized_kwargs": serialized_kwargs,
        "input_reference": input_reference,
        "runtime_env_profile": runtime_env_profile,
        "runtime_env_hash": runtime_env_hash,
        "runtime_env_plan_identity": runtime_env_plan_identity,
        "compiled_graph_submission_transport": submission_transport,
    }


def _request_wire(request: TargetExecutionRequest) -> dict[str, Any]:
    if type(request) is not TargetExecutionRequest:
        raise _Invalid
    if request.execution_protocol_version != TARGET_EXECUTION_PROTOCOL_VERSION:
        raise _Invalid
    identity = _identity_wire(request.identity)
    evidence_id = _positive_counter(request.target_execution_evidence_id)
    evidence_digest = _digest(request.target_execution_evidence_digest)
    claimed_at = _decode_timestamp(_timestamp(request.target_execution_claimed_at))
    expectation_digest = _digest(request.target_expectation_digest)
    attestation_digest = _digest(request.claim_attestation_digest)
    if request.target_expectation.runner_family is not RayRunnerFamily.RAY_CORE:
        raise _Invalid
    try:
        expectation_wire = _embedded_canonical(
            encode_ray_target_expectation(request.target_expectation)
        )
        attestation_wire = _embedded_canonical(
            encode_ray_cluster_attestation(request.claim_attestation)
        )
    except (RayTargetAttestationEncodeError, TypeError, ValueError):
        raise _Invalid from None
    if (
        expectation_digest != ray_target_expectation_digest(request.target_expectation)
        or attestation_digest != ray_cluster_attestation_digest(request.claim_attestation)
        or request.claim_attestation.expectation != request.target_expectation
        or request.claim_attestation.expectation_digest != expectation_digest
        or request.claim_attestation.attestation_digest != attestation_digest
        or claimed_at < request.claim_attestation.observed_at
        or claimed_at >= request.claim_attestation.expires_at
    ):
        raise _Invalid
    body = _normalize_request_body(
        {
            "callable_path": request.callable_path,
            "transport_version": request.transport_version,
            "serialized_args": request.serialized_args,
            "serialized_kwargs": request.serialized_kwargs,
            "input_reference": request.input_reference,
            "runtime_env_profile": request.runtime_env_profile,
            "runtime_env_hash": request.runtime_env_hash,
            "runtime_env_plan_identity": request.runtime_env_plan_identity,
            "compiled_graph_submission_transport": request.compiled_graph_submission_transport,
        }
    )
    return {
        "request_schema": TARGET_EXECUTION_REQUEST_SCHEMA,
        "request_schema_version": TARGET_EXECUTION_REQUEST_SCHEMA_VERSION,
        "execution_protocol_version": TARGET_EXECUTION_PROTOCOL_VERSION,
        **identity,
        "target_execution_evidence_id": evidence_id,
        "target_execution_evidence_digest": evidence_digest,
        "target_execution_claimed_at": _timestamp(claimed_at),
        "target_expectation": expectation_wire,
        "target_expectation_digest": expectation_digest,
        "claim_attestation": attestation_wire,
        "claim_attestation_digest": attestation_digest,
        **body,
    }


def encode_target_execution_request(request: TargetExecutionRequest) -> str:
    """Encode one exact canonical target-bound protocol-2 request."""
    try:
        value = _request_wire(request)
        _validate_json_tree(value, max_bytes=TARGET_EXECUTION_REQUEST_MAX_BYTES)
        canonical = _canonical_json(value, max_bytes=TARGET_EXECUTION_REQUEST_MAX_BYTES)
        decode_target_execution_request(
            canonical,
            expected_identity=request.identity,
            expected_target_execution_evidence_id=request.target_execution_evidence_id,
            expected_target_execution_evidence_digest=request.target_execution_evidence_digest,
            expected_target_execution_claimed_at=request.target_execution_claimed_at,
            expected_target_expectation_digest=request.target_expectation_digest,
            expected_claim_attestation_digest=request.claim_attestation_digest,
        )
        return canonical
    except TargetExecutionRequestDecodeError as error:
        classification = (
            TargetExecutionRequestRejection.RESOURCE_LIMIT
            if error.classification is TargetExecutionRequestRejection.RESOURCE_LIMIT
            else TargetExecutionRequestRejection.INVALID
        )
        raise TargetExecutionRequestEncodeError(classification) from None
    except _ResourceLimit:
        raise TargetExecutionRequestEncodeError(
            TargetExecutionRequestRejection.RESOURCE_LIMIT
        ) from None
    except Exception:
        raise TargetExecutionRequestEncodeError(TargetExecutionRequestRejection.INVALID) from None


def decode_target_execution_request(
    serialized: object,
    *,
    expected_identity: ExecutionIdentity,
    expected_target_execution_evidence_id: int,
    expected_target_execution_evidence_digest: str,
    expected_target_execution_claimed_at: datetime,
    expected_target_expectation_digest: str,
    expected_claim_attestation_digest: str,
) -> TargetExecutionRequest:
    """Decode and bind one canonical protocol-2 request before application setup."""
    try:
        value = _loads(serialized, max_bytes=TARGET_EXECUTION_REQUEST_MAX_BYTES)
        if type(value) is not dict or frozenset(value) != _REQUEST_KEYS:
            _reject_request(TargetExecutionRequestRejection.INVALID)
        if (
            value["request_schema"] != TARGET_EXECUTION_REQUEST_SCHEMA
            or value["request_schema_version"] != TARGET_EXECUTION_REQUEST_SCHEMA_VERSION
        ):
            _reject_request(TargetExecutionRequestRejection.UNSUPPORTED_SCHEMA)
        if value["execution_protocol_version"] != TARGET_EXECUTION_PROTOCOL_VERSION:
            _reject_request(TargetExecutionRequestRejection.UNSUPPORTED_PROTOCOL)
        identity = _identity_from_wire(value)
        if (
            not _is_valid_target_execution_identity(expected_identity)
            or identity != expected_identity
        ):
            _reject_request(TargetExecutionRequestRejection.IDENTITY_MISMATCH)
        evidence_id = _positive_counter(value["target_execution_evidence_id"])
        evidence_digest = _digest(value["target_execution_evidence_digest"])
        claimed_at = _decode_timestamp(value["target_execution_claimed_at"])
        expectation_digest = _digest(value["target_expectation_digest"])
        attestation_digest = _digest(value["claim_attestation_digest"])
        try:
            expected_evidence_id = _positive_counter(expected_target_execution_evidence_id)
            expected_evidence_digest = _digest(expected_target_execution_evidence_digest)
            expected_claimed_at = _decode_timestamp(
                _timestamp(expected_target_execution_claimed_at)
            )
            expected_expectation_digest = _digest(expected_target_expectation_digest)
            expected_attestation_digest = _digest(expected_claim_attestation_digest)
        except (_Invalid, _ResourceLimit):
            _reject_request(TargetExecutionRequestRejection.EVIDENCE_MISMATCH)
        if (
            evidence_id != expected_evidence_id
            or evidence_digest != expected_evidence_digest
            or claimed_at != expected_claimed_at
            or expectation_digest != expected_expectation_digest
            or attestation_digest != expected_attestation_digest
        ):
            _reject_request(TargetExecutionRequestRejection.EVIDENCE_MISMATCH)
        expectation = _decode_embedded_expectation(value["target_expectation"])
        attestation = _decode_embedded_attestation(value["claim_attestation"])
        if (
            expectation.runner_family is not RayRunnerFamily.RAY_CORE
            or expectation_digest != ray_target_expectation_digest(expectation)
            or attestation_digest != ray_cluster_attestation_digest(attestation)
            or attestation.expectation != expectation
            or attestation.expectation_digest != expectation_digest
            or attestation.attestation_digest != attestation_digest
            or claimed_at < attestation.observed_at
            or claimed_at >= attestation.expires_at
        ):
            _reject_request(TargetExecutionRequestRejection.EVIDENCE_MISMATCH)
        body = _normalize_request_body(value)
        canonical = _canonical_json(value, max_bytes=TARGET_EXECUTION_REQUEST_MAX_BYTES)
        if serialized != canonical:
            _reject_request(TargetExecutionRequestRejection.INVALID)
        return TargetExecutionRequest(
            identity=identity,
            execution_protocol_version=TARGET_EXECUTION_PROTOCOL_VERSION,
            target_execution_evidence_id=evidence_id,
            target_execution_evidence_digest=evidence_digest,
            target_execution_claimed_at=claimed_at,
            target_expectation=expectation,
            target_expectation_digest=expectation_digest,
            claim_attestation=attestation,
            claim_attestation_digest=attestation_digest,
            **body,
        )
    except TargetExecutionRequestDecodeError:
        raise
    except _ResourceLimit:
        _reject_request(TargetExecutionRequestRejection.RESOURCE_LIMIT)
    except (
        RayTargetAttestationError,
        _DuplicateKey,
        _Invalid,
        TypeError,
        ValueError,
        UnicodeError,
    ):
        _reject_request(TargetExecutionRequestRejection.INVALID)


def _observed_proof_body(
    *,
    identity: ExecutionIdentity,
    target_execution_evidence_id: int,
    target_execution_evidence_digest: str,
    target_execution_claimed_at: datetime,
    target_expectation_digest: str,
    claim_attestation_digest: str,
    observed_node_id: str,
    observed_cluster_session: str,
    observed_runtime: RayRuntimeVersion,
    observed_membership_digest: str,
    observed_at: datetime,
) -> dict[str, object]:
    identity_wire = _identity_wire(identity)
    if type(observed_node_id) is not str or _NODE_ID.fullmatch(observed_node_id) is None:
        raise _Invalid
    if (
        type(observed_cluster_session) is not str
        or _CLUSTER_SESSION.fullmatch(observed_cluster_session) is None
    ):
        raise _Invalid
    return {
        **identity_wire,
        "target_execution_evidence_id": _positive_counter(target_execution_evidence_id),
        "target_execution_evidence_digest": _digest(target_execution_evidence_digest),
        "target_execution_claimed_at": _timestamp(target_execution_claimed_at),
        "target_expectation_digest": _digest(target_expectation_digest),
        "claim_attestation_digest": _digest(claim_attestation_digest),
        "observed_node_id": observed_node_id,
        "observed_cluster_session": observed_cluster_session,
        "observed_runtime": _runtime_wire(observed_runtime),
        "observed_membership_digest": _digest(observed_membership_digest),
        "observed_at": _timestamp(observed_at),
    }


def target_execution_observed_proof_digest(
    *,
    identity: ExecutionIdentity,
    target_execution_evidence_id: int,
    target_execution_evidence_digest: str,
    target_execution_claimed_at: datetime,
    target_expectation_digest: str,
    claim_attestation_digest: str,
    observed_node_id: str,
    observed_cluster_session: str,
    observed_runtime: RayRuntimeVersion,
    observed_membership_digest: str,
    observed_at: datetime,
) -> str:
    """Return the domain-separated digest of one exact observed proof."""
    try:
        body = _observed_proof_body(
            identity=identity,
            target_execution_evidence_id=target_execution_evidence_id,
            target_execution_evidence_digest=target_execution_evidence_digest,
            target_execution_claimed_at=target_execution_claimed_at,
            target_expectation_digest=target_expectation_digest,
            claim_attestation_digest=claim_attestation_digest,
            observed_node_id=observed_node_id,
            observed_cluster_session=observed_cluster_session,
            observed_runtime=observed_runtime,
            observed_membership_digest=observed_membership_digest,
            observed_at=observed_at,
        )
        payload = _canonical_json(body, max_bytes=RAY_TARGET_EXPECTATION_MAX_BYTES).encode("utf-8")
        return f"sha256:{hashlib.sha256(_OBSERVED_PROOF_DOMAIN + payload).hexdigest()}"
    except Exception:
        raise TargetExecutionResultEncodeError(TargetExecutionResultRejection.INVALID) from None


def build_target_execution_observed_evidence(
    *,
    identity: ExecutionIdentity,
    target_execution_evidence_id: int,
    target_execution_evidence_digest: str,
    target_execution_claimed_at: datetime,
    target_expectation_digest: str,
    claim_attestation_digest: str,
    observed_node_id: str,
    observed_cluster_session: str,
    observed_runtime: RayRuntimeVersion,
    observed_membership_digest: str,
    observed_at: datetime,
) -> TargetExecutionObservedEvidence:
    """Build one complete canonical proof bound to a claim generation."""
    try:
        digest = target_execution_observed_proof_digest(
            identity=identity,
            target_execution_evidence_id=target_execution_evidence_id,
            target_execution_evidence_digest=target_execution_evidence_digest,
            target_execution_claimed_at=target_execution_claimed_at,
            target_expectation_digest=target_expectation_digest,
            claim_attestation_digest=claim_attestation_digest,
            observed_node_id=observed_node_id,
            observed_cluster_session=observed_cluster_session,
            observed_runtime=observed_runtime,
            observed_membership_digest=observed_membership_digest,
            observed_at=observed_at,
        )
        return TargetExecutionObservedEvidence(
            observed_node_id=observed_node_id,
            observed_cluster_session=observed_cluster_session,
            observed_runtime=observed_runtime,
            observed_membership_digest=observed_membership_digest,
            observed_at=observed_at,
            observed_proof_digest=digest,
        )
    except Exception:
        raise TargetExecutionResultEncodeError(TargetExecutionResultRejection.INVALID) from None


def _observed_target_wire(
    value: TargetExecutionObservedEvidence,
    *,
    identity: ExecutionIdentity,
    target_execution_evidence_id: int,
    target_execution_evidence_digest: str,
    target_execution_claimed_at: datetime,
    target_expectation_digest: str,
    claim_attestation_digest: str,
) -> dict[str, object]:
    if type(value) is not TargetExecutionObservedEvidence:
        raise _Invalid
    expected_digest = target_execution_observed_proof_digest(
        identity=identity,
        target_execution_evidence_id=target_execution_evidence_id,
        target_execution_evidence_digest=target_execution_evidence_digest,
        target_execution_claimed_at=target_execution_claimed_at,
        target_expectation_digest=target_expectation_digest,
        claim_attestation_digest=claim_attestation_digest,
        observed_node_id=value.observed_node_id,
        observed_cluster_session=value.observed_cluster_session,
        observed_runtime=value.observed_runtime,
        observed_membership_digest=value.observed_membership_digest,
        observed_at=value.observed_at,
    )
    if value.observed_proof_digest != expected_digest:
        raise _Invalid
    return {
        "observed_node_id": value.observed_node_id,
        "observed_cluster_session": value.observed_cluster_session,
        "observed_runtime": _runtime_wire(value.observed_runtime),
        "observed_membership_digest": value.observed_membership_digest,
        "observed_at": _timestamp(value.observed_at),
        "observed_proof_digest": value.observed_proof_digest,
    }


def _result_common_wire(result: TargetExecutionResult) -> dict[str, Any]:
    if result.execution_protocol_version != TARGET_EXECUTION_PROTOCOL_VERSION:
        raise _Invalid
    identity = _identity_wire(result.identity)
    executor_version = _bounded_text(
        result.executor_django_ray_version,
        max_chars=_EXECUTOR_VERSION_MAX_CHARS,
    )
    assert executor_version is not None
    evidence_id = _positive_counter(result.target_execution_evidence_id)
    evidence_digest = _digest(result.target_execution_evidence_digest)
    claimed_at = _decode_timestamp(_timestamp(result.target_execution_claimed_at))
    expectation_digest = _digest(result.target_expectation_digest)
    attestation_digest = _digest(result.claim_attestation_digest)
    observed = _observed_target_wire(
        result.observed_target,
        identity=result.identity,
        target_execution_evidence_id=evidence_id,
        target_execution_evidence_digest=evidence_digest,
        target_execution_claimed_at=claimed_at,
        target_expectation_digest=expectation_digest,
        claim_attestation_digest=attestation_digest,
    )
    return {
        "result_schema": TARGET_EXECUTION_RESULT_SCHEMA,
        "result_schema_version": TARGET_EXECUTION_RESULT_SCHEMA_VERSION,
        "execution_protocol_version": TARGET_EXECUTION_PROTOCOL_VERSION,
        **identity,
        "target_execution_evidence_id": evidence_id,
        "target_execution_evidence_digest": evidence_digest,
        "target_execution_claimed_at": _timestamp(claimed_at),
        "target_expectation_digest": expectation_digest,
        "claim_attestation_digest": attestation_digest,
        "executor_django_ray_version": executor_version,
        "observed_target": observed,
    }


def encode_target_execution_result(result: TargetExecutionResult) -> str:
    """Encode one verified completion or proven compatibility rejection."""
    try:
        common = _result_common_wire(result)
        if type(result) is TargetExecutionCompletion:
            value = {
                **common,
                "result_kind": TargetExecutionResultKind.COMPLETION.value,
                "application_invoked": True,
                "application_completion": _application_completion_wire(
                    result.application_completion
                ),
            }
        elif type(result) is TargetExecutionCompatibilityRejection:
            if type(result.compatibility_reason) is not TargetExecutionCompatibilityReason:
                raise _Invalid
            value = {
                **common,
                "result_kind": TargetExecutionResultKind.COMPATIBILITY_REJECTION.value,
                "application_invoked": False,
                "compatibility_reason": result.compatibility_reason.value,
            }
        else:
            raise _Invalid
        _validate_json_tree(value, max_bytes=TARGET_EXECUTION_RESULT_MAX_BYTES)
        canonical = _canonical_json(value, max_bytes=TARGET_EXECUTION_RESULT_MAX_BYTES)
        decode_target_execution_result(
            canonical,
            expected_identity=result.identity,
            expected_target_execution_evidence_id=result.target_execution_evidence_id,
            expected_target_execution_evidence_digest=result.target_execution_evidence_digest,
            expected_target_execution_claimed_at=result.target_execution_claimed_at,
            expected_target_expectation_digest=result.target_expectation_digest,
            expected_claim_attestation_digest=result.claim_attestation_digest,
        )
        return canonical
    except TargetExecutionResultDecodeError as error:
        classification = (
            TargetExecutionResultRejection.RESOURCE_LIMIT
            if error.classification is TargetExecutionResultRejection.RESOURCE_LIMIT
            else TargetExecutionResultRejection.INVALID
        )
        raise TargetExecutionResultEncodeError(classification) from None
    except _ResourceLimit:
        raise TargetExecutionResultEncodeError(
            TargetExecutionResultRejection.RESOURCE_LIMIT
        ) from None
    except Exception:
        raise TargetExecutionResultEncodeError(TargetExecutionResultRejection.INVALID) from None


def _decode_observed_target(
    value: object,
    *,
    identity: ExecutionIdentity,
    target_execution_evidence_id: int,
    target_execution_evidence_digest: str,
    target_execution_claimed_at: datetime,
    target_expectation_digest: str,
    claim_attestation_digest: str,
) -> TargetExecutionObservedEvidence:
    if type(value) is not dict or frozenset(value) != _OBSERVED_TARGET_KEYS:
        raise _Invalid
    node_id = value["observed_node_id"]
    cluster_session = value["observed_cluster_session"]
    if type(node_id) is not str or _NODE_ID.fullmatch(node_id) is None:
        raise _Invalid
    if type(cluster_session) is not str or _CLUSTER_SESSION.fullmatch(cluster_session) is None:
        raise _Invalid
    runtime = _decode_runtime(value["observed_runtime"])
    assert isinstance(runtime, RayRuntimeVersion)
    membership_digest = _digest(value["observed_membership_digest"])
    observed_at = _decode_timestamp(value["observed_at"])
    proof_digest = _digest(value["observed_proof_digest"])
    expected_proof_digest = target_execution_observed_proof_digest(
        identity=identity,
        target_execution_evidence_id=target_execution_evidence_id,
        target_execution_evidence_digest=target_execution_evidence_digest,
        target_execution_claimed_at=target_execution_claimed_at,
        target_expectation_digest=target_expectation_digest,
        claim_attestation_digest=claim_attestation_digest,
        observed_node_id=node_id,
        observed_cluster_session=cluster_session,
        observed_runtime=runtime,
        observed_membership_digest=membership_digest,
        observed_at=observed_at,
    )
    if proof_digest != expected_proof_digest:
        _reject_result(TargetExecutionResultRejection.PROOF_MISMATCH)
    return TargetExecutionObservedEvidence(
        observed_node_id=node_id,
        observed_cluster_session=cluster_session,
        observed_runtime=runtime,
        observed_membership_digest=membership_digest,
        observed_at=observed_at,
        observed_proof_digest=proof_digest,
    )


def decode_target_execution_result(
    serialized: object,
    *,
    expected_identity: ExecutionIdentity,
    expected_target_execution_evidence_id: int,
    expected_target_execution_evidence_digest: str,
    expected_target_execution_claimed_at: datetime,
    expected_target_expectation_digest: str,
    expected_claim_attestation_digest: str,
) -> TargetExecutionResult:
    """Decode one exact result; every rejection preserves runner uncertainty."""
    try:
        value = _loads(serialized, max_bytes=TARGET_EXECUTION_RESULT_MAX_BYTES)
        if type(value) is not dict:
            _reject_result(TargetExecutionResultRejection.INVALID)
        kind = value.get("result_kind")
        if kind == TargetExecutionResultKind.COMPLETION.value:
            expected_keys = _RESULT_COMMON_KEYS | {"application_completion"}
        elif kind == TargetExecutionResultKind.COMPATIBILITY_REJECTION.value:
            expected_keys = _RESULT_COMMON_KEYS | {"compatibility_reason"}
        else:
            _reject_result(TargetExecutionResultRejection.INVALID)
        if frozenset(value) != expected_keys:
            _reject_result(TargetExecutionResultRejection.INVALID)
        if (
            value["result_schema"] != TARGET_EXECUTION_RESULT_SCHEMA
            or value["result_schema_version"] != TARGET_EXECUTION_RESULT_SCHEMA_VERSION
        ):
            _reject_result(TargetExecutionResultRejection.UNSUPPORTED_SCHEMA)
        if value["execution_protocol_version"] != TARGET_EXECUTION_PROTOCOL_VERSION:
            _reject_result(TargetExecutionResultRejection.UNSUPPORTED_PROTOCOL)
        identity = _identity_from_wire(value)
        if (
            not _is_valid_target_execution_identity(expected_identity)
            or identity != expected_identity
        ):
            _reject_result(TargetExecutionResultRejection.IDENTITY_MISMATCH)
        evidence_id = _positive_counter(value["target_execution_evidence_id"])
        evidence_digest = _digest(value["target_execution_evidence_digest"])
        claimed_at = _decode_timestamp(value["target_execution_claimed_at"])
        expectation_digest = _digest(value["target_expectation_digest"])
        attestation_digest = _digest(value["claim_attestation_digest"])
        try:
            expected_evidence_id = _positive_counter(expected_target_execution_evidence_id)
            expected_evidence_digest = _digest(expected_target_execution_evidence_digest)
            expected_claimed_at = _decode_timestamp(
                _timestamp(expected_target_execution_claimed_at)
            )
            expected_expectation_digest = _digest(expected_target_expectation_digest)
            expected_attestation_digest = _digest(expected_claim_attestation_digest)
        except (_Invalid, _ResourceLimit):
            _reject_result(TargetExecutionResultRejection.EVIDENCE_MISMATCH)
        if (
            evidence_id != expected_evidence_id
            or evidence_digest != expected_evidence_digest
            or claimed_at != expected_claimed_at
            or expectation_digest != expected_expectation_digest
            or attestation_digest != expected_attestation_digest
        ):
            _reject_result(TargetExecutionResultRejection.EVIDENCE_MISMATCH)
        executor_version = _bounded_text(
            value["executor_django_ray_version"], max_chars=_EXECUTOR_VERSION_MAX_CHARS
        )
        assert executor_version is not None
        observed = _decode_observed_target(
            value["observed_target"],
            identity=identity,
            target_execution_evidence_id=evidence_id,
            target_execution_evidence_digest=evidence_digest,
            target_execution_claimed_at=claimed_at,
            target_expectation_digest=expectation_digest,
            claim_attestation_digest=attestation_digest,
        )
        if kind == TargetExecutionResultKind.COMPLETION.value:
            if value["application_invoked"] is not True:
                _reject_result(TargetExecutionResultRejection.INVALID)
            result: TargetExecutionResult = TargetExecutionCompletion(
                identity=identity,
                execution_protocol_version=TARGET_EXECUTION_PROTOCOL_VERSION,
                target_execution_evidence_id=evidence_id,
                target_execution_evidence_digest=evidence_digest,
                target_execution_claimed_at=claimed_at,
                target_expectation_digest=expectation_digest,
                claim_attestation_digest=attestation_digest,
                executor_django_ray_version=executor_version,
                observed_target=observed,
                application_completion=_normalize_application_completion(
                    value["application_completion"]
                ),
            )
        else:
            if value["application_invoked"] is not False:
                _reject_result(TargetExecutionResultRejection.INVALID)
            try:
                reason = TargetExecutionCompatibilityReason(value["compatibility_reason"])
            except (TypeError, ValueError):
                _reject_result(TargetExecutionResultRejection.INVALID)
            result = TargetExecutionCompatibilityRejection(
                identity=identity,
                execution_protocol_version=TARGET_EXECUTION_PROTOCOL_VERSION,
                target_execution_evidence_id=evidence_id,
                target_execution_evidence_digest=evidence_digest,
                target_execution_claimed_at=claimed_at,
                target_expectation_digest=expectation_digest,
                claim_attestation_digest=attestation_digest,
                executor_django_ray_version=executor_version,
                observed_target=observed,
                compatibility_reason=reason,
            )
        canonical = _canonical_json(value, max_bytes=TARGET_EXECUTION_RESULT_MAX_BYTES)
        if serialized != canonical:
            _reject_result(TargetExecutionResultRejection.INVALID)
        return result
    except TargetExecutionResultDecodeError:
        raise
    except _ResourceLimit:
        _reject_result(TargetExecutionResultRejection.RESOURCE_LIMIT)
    except (_DuplicateKey, _Invalid, TypeError, ValueError, UnicodeError):
        _reject_result(TargetExecutionResultRejection.INVALID)


__all__ = [
    "TARGET_EXECUTION_DIAGNOSTIC_MAX_BYTES",
    "TARGET_EXECUTION_MAX_DEPTH",
    "TARGET_EXECUTION_MAX_NODES",
    "TARGET_EXECUTION_METADATA_MAX_BYTES",
    "TARGET_EXECUTION_REQUEST_MAX_BYTES",
    "TARGET_EXECUTION_REQUEST_SCHEMA",
    "TARGET_EXECUTION_REQUEST_SCHEMA_VERSION",
    "TARGET_EXECUTION_RESULT_MAX_BYTES",
    "TARGET_EXECUTION_RESULT_SCHEMA",
    "TARGET_EXECUTION_RESULT_SCHEMA_VERSION",
    "TargetApplicationCompletion",
    "TargetExecutionCompatibilityReason",
    "TargetExecutionCompatibilityRejection",
    "TargetExecutionCompletion",
    "TargetExecutionObservedEvidence",
    "TargetExecutionRequest",
    "TargetExecutionRequestDecodeError",
    "TargetExecutionRequestEncodeError",
    "TargetExecutionRequestRejection",
    "TargetExecutionResult",
    "TargetExecutionResultDecodeError",
    "TargetExecutionResultEncodeError",
    "TargetExecutionResultKind",
    "TargetExecutionResultRejection",
    "build_target_execution_observed_evidence",
    "decode_target_application_completion",
    "decode_target_execution_request",
    "decode_target_execution_result",
    "encode_target_execution_request",
    "encode_target_execution_result",
    "target_execution_observed_proof_digest",
]
