"""Bounded execution request and completion codecs for worker trust boundaries.

Both versioned schemas remain flat.  Completion fields stay readable by a
protocol-v1 manager, while request fields stay readable by the released Ray Job
entrypoint during rolling deployment.  New header markers always select strict
versioned decoding and can never fall back to a legacy adapter.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from io import StringIO
from typing import Any

from django_ray.execution_protocol import (
    LEGACY_EXECUTION_PROTOCOL_VERSION,
    SUPPORTED_EXECUTION_PROTOCOL_RANGE,
    ExecutionProtocolRange,
)

EXECUTION_COMPLETION_SCHEMA = "django-ray.execution-completion"
EXECUTION_COMPLETION_SCHEMA_VERSION = 1
EXECUTION_COMPLETION_MAX_BYTES = 128 * 1024 * 1024
EXECUTION_COMPLETION_MAX_DEPTH = 64
EXECUTION_COMPLETION_MAX_NODES = 1_000_000
EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES = 64 * 1024

EXECUTION_REQUEST_SCHEMA = "django-ray.execution-request"
EXECUTION_REQUEST_SCHEMA_VERSION = 1
EXECUTION_REQUEST_MAX_BYTES = EXECUTION_COMPLETION_MAX_BYTES
EXECUTION_REQUEST_MAX_DEPTH = EXECUTION_COMPLETION_MAX_DEPTH
EXECUTION_REQUEST_MAX_NODES = EXECUTION_COMPLETION_MAX_NODES
EXECUTION_REQUEST_RUNTIME_ENV_IDENTITY_MAX_BYTES = 16 * 1024

_TASK_ID_MAX_CHARS = 255
_EXECUTOR_VERSION_MAX_CHARS = 128
_RESULT_REFERENCE_MAX_CHARS = 500
_EXCEPTION_TYPE_MAX_BYTES = 512
_CALLABLE_PATH_MAX_CHARS = 500
_RUNTIME_ENV_PROFILE_MAX_CHARS = 100
_INPUT_REFERENCE_MAX_CHARS = 500
_MAX_COUNTER = (1 << 63) - 1
_UTF8_CHUNK_CHARS = 64 * 1024
_RUNTIME_ENV_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_RUNTIME_ENV_HASH = re.compile(r"[0-9a-f]{64}")
_COMPILED_GRAPH_SUBMISSION_TRANSPORTS = frozenset({"direct-ray-core", "ray-client", "ray-job"})

_RESERVED_HEADER_KEYS = frozenset(
    {
        "completion_schema",
        "completion_schema_version",
        "execution_protocol_version",
        "task_execution_pk",
        "task_id",
        "attempt_number",
        "execution_generation",
        "executor_django_ray_version",
    }
)
_OUTCOME_KEYS = frozenset(
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
_ENRICHED_KEYS = _RESERVED_HEADER_KEYS | _OUTCOME_KEYS

_REQUEST_RESERVED_MARKER_KEYS = frozenset(
    {
        "request_schema",
        "request_schema_version",
        "execution_protocol_version",
    }
)
_REQUEST_KEYS = frozenset(
    {
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
)


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Durable identity that one completion must echo exactly."""

    task_execution_pk: int
    task_id: str
    attempt_number: int
    execution_generation: int


@dataclass(frozen=True, slots=True)
class ExecutionCompletion:
    """Validated and normalized completion consumed by a manager."""

    identity: ExecutionIdentity
    execution_protocol_version: int
    executor_django_ray_version: str | None
    success: bool
    result: Any
    result_reference: str | None
    error: str | None
    traceback: str | None
    exception_type: str | None
    retryable: bool | None


class ExecutionCompletionSource(StrEnum):
    """Accepted wire representation."""

    ACCEPTED_LEGACY_V1 = "accepted_legacy_v1"
    ACCEPTED_VERSIONED_V1 = "accepted_versioned_v1"


@dataclass(frozen=True, slots=True)
class DecodedExecutionCompletion:
    """One accepted completion and the compatibility path that produced it."""

    source: ExecutionCompletionSource
    completion: ExecutionCompletion


class ExecutionCompletionRejection(StrEnum):
    """Stable, secret-safe rejection classifications for worker policy."""

    MALFORMED_LEGACY = "malformed_legacy"
    INVALID_VERSIONED = "invalid_versioned"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    RESOURCE_LIMIT = "resource_limit"


class ExecutionCompletionDecodeError(ValueError):
    """Reject one completion without retaining attacker-controlled text."""

    def __init__(
        self,
        classification: ExecutionCompletionRejection,
        *,
        attempted_versioned: bool,
        identity_verified: bool = False,
    ) -> None:
        self.classification = classification
        self.attempted_versioned = attempted_versioned
        self.identity_verified = identity_verified
        super().__init__(f"execution completion rejected: {classification.value}")

    @property
    def requires_nonretryable_disposition(self) -> bool:
        """Return whether manager policy must suppress automatic retry."""
        return self.classification is not ExecutionCompletionRejection.MALFORMED_LEGACY


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Validated execution inputs transported to one remote executor."""

    identity: ExecutionIdentity
    execution_protocol_version: int
    callable_path: str
    transport_version: int
    serialized_args: str
    serialized_kwargs: str
    input_reference: str | None
    runtime_env_profile: str | None
    runtime_env_hash: str
    runtime_env_plan_identity: dict[str, Any]
    compiled_graph_submission_transport: str | None


class ExecutionRequestRejection(StrEnum):
    """Stable, secret-safe rejection classifications for request policy."""

    LEGACY_REQUEST = "legacy_request"
    INVALID_VERSIONED = "invalid_versioned"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_TRANSPORT = "unsupported_transport"
    RESOURCE_LIMIT = "resource_limit"


class ExecutionRequestDecodeError(ValueError):
    """Reject one request without retaining executor-facing input data."""

    def __init__(
        self,
        classification: ExecutionRequestRejection,
        *,
        attempted_versioned: bool,
        validated_identity: ExecutionIdentity | None = None,
        requested_execution_protocol_version: int | None = None,
    ) -> None:
        if (
            classification is not ExecutionRequestRejection.UNSUPPORTED_PROTOCOL
            or validated_identity is None
            or not _valid_identity_shape(validated_identity)
            or type(requested_execution_protocol_version) is not int
            or not 0 < requested_execution_protocol_version <= _MAX_COUNTER
        ):
            validated_identity = None
            requested_execution_protocol_version = None
        self.classification = classification
        self.attempted_versioned = attempted_versioned
        self.validated_identity = validated_identity
        self.requested_execution_protocol_version = requested_execution_protocol_version
        super().__init__(f"execution request rejected: {classification.value}")

    @property
    def allows_legacy_fallback(self) -> bool:
        """Return whether an outer adapter may try the released request path."""
        return (
            self.classification is ExecutionRequestRejection.LEGACY_REQUEST
            and not self.attempted_versioned
        )


class ExecutionRequestEncodeError(ValueError):
    """Report a deterministic request-construction failure without field data."""

    def __init__(self) -> None:
        super().__init__("execution request is invalid")


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


class _InvalidJsonTreeError(ValueError):
    pass


class _ResourceLimitError(ValueError):
    pass


class _UnsupportedRequestTransportError(ValueError):
    pass


def _reject(
    classification: ExecutionCompletionRejection,
    *,
    attempted_versioned: bool,
    identity_verified: bool = False,
) -> None:
    raise ExecutionCompletionDecodeError(
        classification,
        attempted_versioned=attempted_versioned,
        identity_verified=identity_verified,
    ) from None


def _framing_rejection(
    *,
    attempted_versioned: bool,
    expected_execution_protocol_version: int,
) -> ExecutionCompletionRejection:
    if attempted_versioned:
        return ExecutionCompletionRejection.INVALID_VERSIONED
    if expected_execution_protocol_version != LEGACY_EXECUTION_PROTOCOL_VERSION:
        return ExecutionCompletionRejection.PROTOCOL_MISMATCH
    return ExecutionCompletionRejection.MALFORMED_LEGACY


def _bounded_utf8_size(value: str, *, max_bytes: int) -> int:
    """Count UTF-8 bytes with bounded temporary allocations."""
    total = 0
    for offset in range(0, len(value), _UTF8_CHUNK_CHARS):
        total += len(value[offset : offset + _UTF8_CHUNK_CHARS].encode("utf-8"))
        if total > max_bytes:
            raise _ResourceLimitError
    return total


def _preparse_json_scan(
    serialized: str,
    *,
    reserved_marker_keys: frozenset[str] = _RESERVED_HEADER_KEYS,
    max_depth: int | None = None,
    max_nodes: int | None = None,
) -> bool:
    """Bound structure before parsing and identify reserved top-level keys."""
    if max_depth is None:
        max_depth = EXECUTION_COMPLETION_MAX_DEPTH
    if max_nodes is None:
        max_nodes = EXECUTION_COMPLETION_MAX_NODES
    depth = 0
    nodes = 0
    index = 0
    length = len(serialized)
    attempted_versioned = False
    primitive = False
    maximum_reserved_token_chars = 2 + 6 * max(map(len, reserved_marker_keys))

    def count_node() -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise _ResourceLimitError

    while index < length:
        character = serialized[index]
        if character == '"':
            count_node()
            primitive = False
            start = index
            index += 1
            escaped = False
            while index < length:
                character = serialized[index]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    break
                index += 1
            if index >= length:
                return attempted_versioned
            if depth == 1:
                after = index + 1
                while after < length and serialized[after].isspace():
                    after += 1
                token_chars = index + 1 - start
                if (
                    after < length
                    and serialized[after] == ":"
                    and token_chars <= maximum_reserved_token_chars
                ):
                    try:
                        key = json.loads(serialized[start : index + 1])
                    except (TypeError, ValueError):
                        key = None
                    if key in reserved_marker_keys:
                        attempted_versioned = True
        elif character in "{[":
            count_node()
            primitive = False
            depth += 1
            if depth > max_depth:
                raise _ResourceLimitError
        elif character in "}]":
            primitive = False
            depth = max(depth - 1, 0)
        elif character in ",:" or character.isspace():
            primitive = False
        elif not primitive:
            count_node()
            primitive = True
        index += 1
    return attempted_versioned


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _NonFiniteNumberError
    return parsed


def _reject_constant(_value: str) -> None:
    raise _NonFiniteNumberError


def _bounded_json_loads(
    serialized: object,
    *,
    expected_execution_protocol_version: int,
) -> tuple[Any, bool]:
    if not isinstance(serialized, str):
        _reject(
            _framing_rejection(
                attempted_versioned=False,
                expected_execution_protocol_version=expected_execution_protocol_version,
            ),
            attempted_versioned=False,
        )
    if len(serialized) > EXECUTION_COMPLETION_MAX_BYTES:
        _reject(
            ExecutionCompletionRejection.RESOURCE_LIMIT,
            attempted_versioned=False,
        )
    try:
        attempted_versioned = _preparse_json_scan(
            serialized,
            reserved_marker_keys=_RESERVED_HEADER_KEYS,
            max_depth=EXECUTION_COMPLETION_MAX_DEPTH,
            max_nodes=EXECUTION_COMPLETION_MAX_NODES,
        )
    except _ResourceLimitError:
        _reject(
            ExecutionCompletionRejection.RESOURCE_LIMIT,
            attempted_versioned=False,
        )
    try:
        _bounded_utf8_size(serialized, max_bytes=EXECUTION_COMPLETION_MAX_BYTES)
    except _ResourceLimitError:
        _reject(
            ExecutionCompletionRejection.RESOURCE_LIMIT,
            attempted_versioned=attempted_versioned,
        )
    except UnicodeEncodeError:
        _reject(
            _framing_rejection(
                attempted_versioned=attempted_versioned,
                expected_execution_protocol_version=expected_execution_protocol_version,
            ),
            attempted_versioned=attempted_versioned,
        )
    try:
        if attempted_versioned:
            value = json.loads(
                serialized,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
        else:
            value = json.loads(serialized, object_pairs_hook=_unique_object)
    except (_DuplicateKeyError, _NonFiniteNumberError, ValueError, RecursionError):
        _reject(
            _framing_rejection(
                attempted_versioned=attempted_versioned,
                expected_execution_protocol_version=expected_execution_protocol_version,
            ),
            attempted_versioned=attempted_versioned,
        )
    try:
        _validate_json_tree(
            value,
            allow_nonfinite=not attempted_versioned,
            allow_invalid_unicode=not attempted_versioned,
        )
    except _ResourceLimitError:
        _reject(
            ExecutionCompletionRejection.RESOURCE_LIMIT,
            attempted_versioned=attempted_versioned,
        )
    except _InvalidJsonTreeError:
        _reject(
            _framing_rejection(
                attempted_versioned=attempted_versioned,
                expected_execution_protocol_version=expected_execution_protocol_version,
            ),
            attempted_versioned=attempted_versioned,
        )
    return value, attempted_versioned


def _dict_children(value: dict[Any, Any]) -> Iterator[Any]:
    for key, item in value.items():
        yield key
        yield item


def _validate_json_tree(
    value: Any,
    *,
    allow_nonfinite: bool,
    allow_invalid_unicode: bool = False,
    allow_nul: bool = True,
    max_depth: int | None = None,
    max_nodes: int | None = None,
    max_string_bytes: int | None = None,
) -> None:
    """Validate a JSON-like tree with O(depth) auxiliary storage."""
    if max_depth is None:
        max_depth = EXECUTION_COMPLETION_MAX_DEPTH
    if max_nodes is None:
        max_nodes = EXECUTION_COMPLETION_MAX_NODES
    if max_string_bytes is None:
        max_string_bytes = EXECUTION_COMPLETION_MAX_BYTES
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
            raise _ResourceLimitError
        if isinstance(item, str):
            if not allow_nul and "\x00" in item:
                raise _InvalidJsonTreeError
            if not allow_invalid_unicode:
                try:
                    _bounded_utf8_size(item, max_bytes=max_string_bytes)
                except UnicodeEncodeError as error:
                    raise _InvalidJsonTreeError from error
            continue
        if isinstance(item, float) and not allow_nonfinite and not math.isfinite(item):
            raise _InvalidJsonTreeError
        if isinstance(item, dict):
            nested = _dict_children(item)
        elif isinstance(item, (list, tuple)):
            nested = iter(item)
        else:
            continue

        depth = parent_depth + 1
        if depth > max_depth:
            raise _ResourceLimitError
        identity = id(item)
        if identity in ancestors:
            raise _InvalidJsonTreeError
        ancestors.add(identity)
        stack.append((nested, depth, identity))


def _bounded_text(
    value: Any,
    *,
    max_bytes: int | None,
    nullable: bool,
    allow_nul: bool,
) -> str | None:
    if nullable and value is None:
        return None
    if type(value) is not str or (not allow_nul and "\x00" in value):
        raise ValueError
    if max_bytes is None:
        return value
    try:
        _bounded_utf8_size(value, max_bytes=max_bytes)
    except (UnicodeEncodeError, _ResourceLimitError) as error:
        raise ValueError from error
    return value


def _valid_identity_shape(identity: ExecutionIdentity) -> bool:
    return (
        type(identity.task_execution_pk) is int
        and 0 < identity.task_execution_pk <= _MAX_COUNTER
        and type(identity.task_id) is str
        and 0 < len(identity.task_id) <= _TASK_ID_MAX_CHARS
        and "\x00" not in identity.task_id
        and type(identity.attempt_number) is int
        and 0 < identity.attempt_number <= _MAX_COUNTER
        and type(identity.execution_generation) is int
        and 0 <= identity.execution_generation <= _MAX_COUNTER
    )


def _normalize_body(value: dict[str, Any], *, legacy: bool) -> dict[str, Any]:
    success = value.get("success")
    if type(success) is not bool or "result" not in value:
        raise ValueError

    if legacy and not success:
        result = None
        result_reference = None
    else:
        result = value["result"]
        result_reference = value.get("result_reference")
        if result_reference is not None and type(result_reference) is not str:
            raise ValueError
        if result_reference is not None and len(result_reference) > _RESULT_REFERENCE_MAX_CHARS:
            raise ValueError
        if not legacy and result_reference is not None and "\x00" in result_reference:
            raise ValueError

    if success:
        if legacy:
            _bounded_text(
                value.get("error"),
                max_bytes=None,
                nullable=True,
                allow_nul=True,
            )
            _bounded_text(
                value.get("traceback"),
                max_bytes=None,
                nullable=True,
                allow_nul=True,
            )
            _bounded_text(
                value.get("exception_type"),
                max_bytes=None,
                nullable=True,
                allow_nul=True,
            )
            legacy_retryable = value.get("retryable")
            if legacy_retryable is not None and type(legacy_retryable) is not bool:
                raise ValueError
        elif any(
            value[field] is not None
            for field in ("error", "traceback", "exception_type", "retryable")
        ):
            raise ValueError
        error = None
        traceback = None
        exception_type = None
        retryable = None
    else:
        error = _bounded_text(
            value.get("error"),
            max_bytes=(None if legacy else EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES),
            nullable=False,
            allow_nul=legacy,
        )
        traceback = _bounded_text(
            value.get("traceback"),
            max_bytes=(None if legacy else EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES),
            nullable=True,
            allow_nul=legacy,
        )
        exception_type = _bounded_text(
            value.get("exception_type"),
            max_bytes=(None if legacy else _EXCEPTION_TYPE_MAX_BYTES),
            nullable=True,
            allow_nul=legacy,
        )
        retryable = value.get("retryable")
        if retryable is not None and type(retryable) is not bool:
            raise ValueError
        if not legacy and (result is not None or result_reference is not None):
            raise ValueError

    return {
        "success": success,
        "result": result,
        "result_reference": result_reference,
        "error": error,
        "traceback": traceback,
        "exception_type": exception_type,
        "retryable": retryable,
    }


def _canonical_json(value: dict[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError from error


def _bounded_json_dumps(
    value: Any,
    *,
    sort_keys: bool,
    max_bytes: int | None = None,
) -> str:
    """Serialize JSON incrementally and stop before aggregate allocation escapes its cap."""
    if max_bytes is None:
        max_bytes = EXECUTION_COMPLETION_MAX_BYTES
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
        allow_nan=False,
    )
    buffer = StringIO()
    encoded_bytes = 0
    for chunk in encoder.iterencode(value):
        remaining = max_bytes - encoded_bytes
        encoded_bytes += _bounded_utf8_size(chunk, max_bytes=remaining)
        buffer.write(chunk)
    return buffer.getvalue()


def _detached_json_tree_for_encoding(
    value: dict[str, Any],
    *,
    execution_protocol_version: int,
) -> dict[str, Any]:
    """Normalize JSON-compatible Python values before canonical ordering."""
    _validate_json_tree(value, allow_nonfinite=False)
    serialized = _bounded_json_dumps(value, sort_keys=False)
    normalized, attempted_versioned = _bounded_json_loads(
        serialized,
        expected_execution_protocol_version=execution_protocol_version,
    )
    if not attempted_versioned or not isinstance(normalized, dict):
        raise ValueError
    return normalized


def encode_execution_completion(completion: ExecutionCompletion) -> str:
    """Encode one exact canonical enriched-v1 completion."""
    identity = completion.identity
    if not _valid_identity_shape(identity):
        raise ValueError("execution completion is invalid")
    if (
        type(completion.execution_protocol_version) is not int
        or not SUPPORTED_EXECUTION_PROTOCOL_RANGE.supports(completion.execution_protocol_version)
        or type(completion.executor_django_ray_version) is not str
        or not completion.executor_django_ray_version
        or len(completion.executor_django_ray_version) > _EXECUTOR_VERSION_MAX_CHARS
        or "\x00" in completion.executor_django_ray_version
    ):
        raise ValueError("execution completion is invalid")
    value = {
        "completion_schema": EXECUTION_COMPLETION_SCHEMA,
        "completion_schema_version": EXECUTION_COMPLETION_SCHEMA_VERSION,
        "execution_protocol_version": completion.execution_protocol_version,
        "task_execution_pk": identity.task_execution_pk,
        "task_id": identity.task_id,
        "attempt_number": identity.attempt_number,
        "execution_generation": identity.execution_generation,
        "executor_django_ray_version": completion.executor_django_ray_version,
        "success": completion.success,
        "result": completion.result,
        "result_reference": completion.result_reference,
        "error": completion.error,
        "traceback": completion.traceback,
        "exception_type": completion.exception_type,
        "retryable": completion.retryable,
    }
    try:
        value.update(_normalize_body(value, legacy=False))
        normalized = _detached_json_tree_for_encoding(
            value,
            execution_protocol_version=completion.execution_protocol_version,
        )
        normalized.update(_normalize_body(normalized, legacy=False))
        serialized = _bounded_json_dumps(normalized, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError("execution completion is invalid") from error
    return serialized


def encode_execution_request_rejection(
    *,
    expected_identity: ExecutionIdentity,
    expected_execution_protocol_version: int,
    executor_django_ray_version: str,
    classification: ExecutionRequestRejection,
) -> str:
    """Encode a fixed failure for a request rejected before application setup."""
    if (
        not isinstance(expected_identity, ExecutionIdentity)
        or not _valid_identity_shape(expected_identity)
        or type(expected_execution_protocol_version) is not int
        or not 0 < expected_execution_protocol_version <= _MAX_COUNTER
        or type(executor_django_ray_version) is not str
        or not executor_django_ray_version
        or len(executor_django_ray_version) > _EXECUTOR_VERSION_MAX_CHARS
        or "\x00" in executor_django_ray_version
        or type(classification) is not ExecutionRequestRejection
    ):
        raise ExecutionRequestEncodeError from None

    value = {
        "completion_schema": EXECUTION_COMPLETION_SCHEMA,
        "completion_schema_version": EXECUTION_COMPLETION_SCHEMA_VERSION,
        "execution_protocol_version": expected_execution_protocol_version,
        "task_execution_pk": expected_identity.task_execution_pk,
        "task_id": expected_identity.task_id,
        "attempt_number": expected_identity.attempt_number,
        "execution_generation": expected_identity.execution_generation,
        "executor_django_ray_version": executor_django_ray_version,
        "success": False,
        "result": None,
        "result_reference": None,
        "error": f"execution request rejected: {classification.value}",
        "traceback": None,
        "exception_type": "RayExecutionRequestIncompatible",
        "retryable": False,
    }
    try:
        value.update(_normalize_body(value, legacy=False))
        normalized = _detached_json_tree_for_encoding(
            value,
            execution_protocol_version=expected_execution_protocol_version,
        )
        normalized.update(_normalize_body(normalized, legacy=False))
        serialized = _bounded_json_dumps(normalized, sort_keys=True)
    except (
        ExecutionCompletionDecodeError,
        _InvalidJsonTreeError,
        _ResourceLimitError,
        TypeError,
        ValueError,
        RecursionError,
        UnicodeError,
    ):
        raise ExecutionRequestEncodeError from None
    return serialized


def _decode_enriched_v1(
    value: Any,
    serialized: str,
    *,
    expected_identity: ExecutionIdentity,
    expected_execution_protocol_version: int,
    supported_protocols: ExecutionProtocolRange,
) -> DecodedExecutionCompletion:
    if not isinstance(value, dict) or set(value) != _ENRICHED_KEYS:
        _reject(
            ExecutionCompletionRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    if (
        value["completion_schema"] != EXECUTION_COMPLETION_SCHEMA
        or type(value["completion_schema"]) is not str
        or type(value["completion_schema_version"]) is not int
        or value["completion_schema_version"] != EXECUTION_COMPLETION_SCHEMA_VERSION
    ):
        _reject(
            ExecutionCompletionRejection.UNSUPPORTED_SCHEMA,
            attempted_versioned=True,
        )
    protocol = value["execution_protocol_version"]
    if type(protocol) is not int or protocol < 1 or not supported_protocols.supports(protocol):
        _reject(
            ExecutionCompletionRejection.UNSUPPORTED_PROTOCOL,
            attempted_versioned=True,
        )
    if protocol != expected_execution_protocol_version:
        _reject(
            ExecutionCompletionRejection.PROTOCOL_MISMATCH,
            attempted_versioned=True,
        )
    identity = ExecutionIdentity(
        task_execution_pk=value["task_execution_pk"],
        task_id=value["task_id"],
        attempt_number=value["attempt_number"],
        execution_generation=value["execution_generation"],
    )
    if not _valid_identity_shape(identity):
        _reject(
            ExecutionCompletionRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    if identity != expected_identity:
        _reject(
            ExecutionCompletionRejection.IDENTITY_MISMATCH,
            attempted_versioned=True,
        )
    executor_version = value["executor_django_ray_version"]
    if (
        type(executor_version) is not str
        or not executor_version
        or len(executor_version) > _EXECUTOR_VERSION_MAX_CHARS
        or "\x00" in executor_version
    ):
        _reject(
            ExecutionCompletionRejection.INVALID_VERSIONED,
            attempted_versioned=True,
            identity_verified=True,
        )
    try:
        body = _normalize_body(value, legacy=False)
        canonical = _canonical_json(value)
    except ValueError:
        _reject(
            ExecutionCompletionRejection.INVALID_VERSIONED,
            attempted_versioned=True,
            identity_verified=True,
        )
    if serialized != canonical:
        _reject(
            ExecutionCompletionRejection.INVALID_VERSIONED,
            attempted_versioned=True,
            identity_verified=True,
        )
    return DecodedExecutionCompletion(
        source=ExecutionCompletionSource.ACCEPTED_VERSIONED_V1,
        completion=ExecutionCompletion(
            identity=identity,
            execution_protocol_version=protocol,
            executor_django_ray_version=executor_version,
            **body,
        ),
    )


def _normalize_legacy_v1_completion(
    value: Any,
    *,
    expected_identity: ExecutionIdentity,
    expected_execution_protocol_version: int,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
) -> DecodedExecutionCompletion:
    if expected_execution_protocol_version != LEGACY_EXECUTION_PROTOCOL_VERSION:
        _reject(
            ExecutionCompletionRejection.PROTOCOL_MISMATCH,
            attempted_versioned=False,
        )
    if not supported_protocols.supports(LEGACY_EXECUTION_PROTOCOL_VERSION):
        _reject(
            ExecutionCompletionRejection.UNSUPPORTED_PROTOCOL,
            attempted_versioned=False,
        )
    if (
        not _valid_identity_shape(expected_identity)
        or not isinstance(value, dict)
        or _RESERVED_HEADER_KEYS.intersection(value)
    ):
        _reject(
            ExecutionCompletionRejection.MALFORMED_LEGACY,
            attempted_versioned=False,
        )
    try:
        body = _normalize_body(value, legacy=True)
    except ValueError:
        _reject(
            ExecutionCompletionRejection.MALFORMED_LEGACY,
            attempted_versioned=False,
        )
    return DecodedExecutionCompletion(
        source=ExecutionCompletionSource.ACCEPTED_LEGACY_V1,
        completion=ExecutionCompletion(
            identity=expected_identity,
            execution_protocol_version=LEGACY_EXECUTION_PROTOCOL_VERSION,
            executor_django_ray_version=None,
            **body,
        ),
    )


def decode_legacy_v1_completion(
    serialized: object,
    *,
    expected_identity: ExecutionIdentity,
    expected_execution_protocol_version: int,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
) -> DecodedExecutionCompletion:
    """Bound and normalize one explicitly legacy protocol-v1 completion."""
    value, attempted_versioned = _bounded_json_loads(
        serialized,
        expected_execution_protocol_version=expected_execution_protocol_version,
    )
    if attempted_versioned:
        _reject(
            ExecutionCompletionRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    return _normalize_legacy_v1_completion(
        value,
        expected_identity=expected_identity,
        expected_execution_protocol_version=expected_execution_protocol_version,
        supported_protocols=supported_protocols,
    )


def decode_execution_completion(
    serialized: object,
    *,
    expected_identity: ExecutionIdentity,
    expected_execution_protocol_version: int,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
) -> DecodedExecutionCompletion:
    """Bound, decode, fence, and normalize one execution completion."""
    value, attempted_versioned = _bounded_json_loads(
        serialized,
        expected_execution_protocol_version=expected_execution_protocol_version,
    )
    if attempted_versioned:
        return _decode_enriched_v1(
            value,
            serialized,
            expected_identity=expected_identity,
            expected_execution_protocol_version=expected_execution_protocol_version,
            supported_protocols=supported_protocols,
        )
    return _normalize_legacy_v1_completion(
        value,
        expected_identity=expected_identity,
        expected_execution_protocol_version=expected_execution_protocol_version,
        supported_protocols=supported_protocols,
    )


def _reject_request(
    classification: ExecutionRequestRejection,
    *,
    attempted_versioned: bool,
    validated_identity: ExecutionIdentity | None = None,
    requested_execution_protocol_version: int | None = None,
) -> None:
    raise ExecutionRequestDecodeError(
        classification,
        attempted_versioned=attempted_versioned,
        validated_identity=validated_identity,
        requested_execution_protocol_version=requested_execution_protocol_version,
    ) from None


def _bounded_request_json_loads(serialized: object) -> tuple[Any, bool]:
    """Parse only strict versioned requests after bounded marker detection."""
    if not isinstance(serialized, str):
        _reject_request(
            ExecutionRequestRejection.LEGACY_REQUEST,
            attempted_versioned=False,
        )
    if len(serialized) > EXECUTION_REQUEST_MAX_BYTES:
        _reject_request(
            ExecutionRequestRejection.RESOURCE_LIMIT,
            attempted_versioned=False,
        )
    try:
        attempted_versioned = _preparse_json_scan(
            serialized,
            reserved_marker_keys=_REQUEST_RESERVED_MARKER_KEYS,
            max_depth=EXECUTION_REQUEST_MAX_DEPTH,
            max_nodes=EXECUTION_REQUEST_MAX_NODES,
        )
    except _ResourceLimitError:
        _reject_request(
            ExecutionRequestRejection.RESOURCE_LIMIT,
            attempted_versioned=False,
        )
    try:
        _bounded_utf8_size(serialized, max_bytes=EXECUTION_REQUEST_MAX_BYTES)
    except _ResourceLimitError:
        _reject_request(
            ExecutionRequestRejection.RESOURCE_LIMIT,
            attempted_versioned=attempted_versioned,
        )
    except UnicodeEncodeError:
        _reject_request(
            (
                ExecutionRequestRejection.INVALID_VERSIONED
                if attempted_versioned
                else ExecutionRequestRejection.LEGACY_REQUEST
            ),
            attempted_versioned=attempted_versioned,
        )
    if not attempted_versioned:
        _reject_request(
            ExecutionRequestRejection.LEGACY_REQUEST,
            attempted_versioned=False,
        )
    try:
        value = json.loads(
            serialized,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (_DuplicateKeyError, _NonFiniteNumberError, ValueError, RecursionError):
        _reject_request(
            ExecutionRequestRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    try:
        _validate_json_tree(
            value,
            allow_nonfinite=False,
            allow_nul=False,
            max_depth=EXECUTION_REQUEST_MAX_DEPTH,
            max_nodes=EXECUTION_REQUEST_MAX_NODES,
            max_string_bytes=EXECUTION_REQUEST_MAX_BYTES,
        )
    except _ResourceLimitError:
        _reject_request(
            ExecutionRequestRejection.RESOURCE_LIMIT,
            attempted_versioned=True,
        )
    except _InvalidJsonTreeError:
        _reject_request(
            ExecutionRequestRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    return value, attempted_versioned


def _request_text(
    value: Any,
    *,
    max_chars: int,
    nullable: bool,
    nonempty: bool,
) -> str | None:
    if nullable and value is None:
        return None
    if (
        type(value) is not str
        or (nonempty and not value)
        or len(value) > max_chars
        or "\x00" in value
    ):
        raise ValueError
    return value


def _normalize_request_body(value: dict[str, Any]) -> dict[str, Any]:
    callable_path = _request_text(
        value["callable_path"],
        max_chars=_CALLABLE_PATH_MAX_CHARS,
        nullable=False,
        nonempty=True,
    )
    if callable_path is None or "." not in callable_path:
        raise ValueError

    transport_version = value["transport_version"]
    if type(transport_version) is not int or transport_version not in (1, 2):
        raise _UnsupportedRequestTransportError

    serialized_args = value["serialized_args"]
    serialized_kwargs = value["serialized_kwargs"]
    if (
        type(serialized_args) is not str
        or not serialized_args
        or "\x00" in serialized_args
        or type(serialized_kwargs) is not str
        or not serialized_kwargs
        or "\x00" in serialized_kwargs
    ):
        raise ValueError

    input_reference = _request_text(
        value["input_reference"],
        max_chars=_INPUT_REFERENCE_MAX_CHARS,
        nullable=True,
        nonempty=True,
    )
    if transport_version == 1:
        if input_reference is not None:
            raise ValueError
    elif input_reference is None or serialized_args != "null" or serialized_kwargs != "null":
        raise ValueError

    runtime_env_profile = _request_text(
        value["runtime_env_profile"],
        max_chars=_RUNTIME_ENV_PROFILE_MAX_CHARS,
        nullable=True,
        nonempty=True,
    )
    if (
        runtime_env_profile is not None
        and _RUNTIME_ENV_PROFILE.fullmatch(runtime_env_profile) is None
    ):
        raise ValueError

    runtime_env_hash = value["runtime_env_hash"]
    if type(runtime_env_hash) is not str or _RUNTIME_ENV_HASH.fullmatch(runtime_env_hash) is None:
        raise ValueError

    runtime_env_plan_identity = value["runtime_env_plan_identity"]
    if type(runtime_env_plan_identity) is not dict:
        raise ValueError
    _validate_json_tree(
        runtime_env_plan_identity,
        allow_nonfinite=False,
        allow_nul=False,
        max_depth=EXECUTION_REQUEST_MAX_DEPTH,
        max_nodes=EXECUTION_REQUEST_MAX_NODES,
        max_string_bytes=EXECUTION_REQUEST_RUNTIME_ENV_IDENTITY_MAX_BYTES,
    )
    _bounded_json_dumps(
        runtime_env_plan_identity,
        sort_keys=False,
        max_bytes=EXECUTION_REQUEST_RUNTIME_ENV_IDENTITY_MAX_BYTES,
    )

    compiled_graph_submission_transport = value["compiled_graph_submission_transport"]
    if compiled_graph_submission_transport is not None and (
        type(compiled_graph_submission_transport) is not str
        or compiled_graph_submission_transport not in _COMPILED_GRAPH_SUBMISSION_TRANSPORTS
    ):
        raise ValueError

    return {
        "callable_path": callable_path,
        "transport_version": transport_version,
        "serialized_args": serialized_args,
        "serialized_kwargs": serialized_kwargs,
        "input_reference": input_reference,
        "runtime_env_profile": runtime_env_profile,
        "runtime_env_hash": runtime_env_hash,
        "runtime_env_plan_identity": runtime_env_plan_identity,
        "compiled_graph_submission_transport": compiled_graph_submission_transport,
    }


def _decode_versioned_execution_request(
    value: Any,
    serialized: str,
    *,
    supported_protocols: ExecutionProtocolRange,
    expected_identity: ExecutionIdentity | None,
    expected_execution_protocol_version: int | None,
) -> ExecutionRequest:
    if not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        _reject_request(
            ExecutionRequestRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    if (
        type(value["request_schema"]) is not str
        or value["request_schema"] != EXECUTION_REQUEST_SCHEMA
        or type(value["request_schema_version"]) is not int
        or value["request_schema_version"] != EXECUTION_REQUEST_SCHEMA_VERSION
    ):
        _reject_request(
            ExecutionRequestRejection.UNSUPPORTED_SCHEMA,
            attempted_versioned=True,
        )

    identity = ExecutionIdentity(
        task_execution_pk=value["task_execution_pk"],
        task_id=value["task_id"],
        attempt_number=value["attempt_number"],
        execution_generation=value["execution_generation"],
    )
    if not _valid_identity_shape(identity):
        _reject_request(
            ExecutionRequestRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    if expected_identity is not None and (
        not _valid_identity_shape(expected_identity) or identity != expected_identity
    ):
        _reject_request(
            ExecutionRequestRejection.IDENTITY_MISMATCH,
            attempted_versioned=True,
        )

    protocol = value["execution_protocol_version"]
    if type(protocol) is not int or not 0 < protocol <= _MAX_COUNTER:
        _reject_request(
            ExecutionRequestRejection.UNSUPPORTED_PROTOCOL,
            attempted_versioned=True,
        )
    if expected_execution_protocol_version is not None and (
        type(expected_execution_protocol_version) is not int
        or not 0 < expected_execution_protocol_version <= _MAX_COUNTER
        or protocol != expected_execution_protocol_version
    ):
        _reject_request(
            ExecutionRequestRejection.PROTOCOL_MISMATCH,
            attempted_versioned=True,
        )
    if not supported_protocols.supports(protocol):
        _reject_request(
            ExecutionRequestRejection.UNSUPPORTED_PROTOCOL,
            attempted_versioned=True,
            validated_identity=identity,
            requested_execution_protocol_version=protocol,
        )

    try:
        body = _normalize_request_body(value)
        canonical = _bounded_json_dumps(
            value,
            sort_keys=True,
            max_bytes=EXECUTION_REQUEST_MAX_BYTES,
        )
    except _UnsupportedRequestTransportError:
        _reject_request(
            ExecutionRequestRejection.UNSUPPORTED_TRANSPORT,
            attempted_versioned=True,
        )
    except _ResourceLimitError:
        _reject_request(
            ExecutionRequestRejection.RESOURCE_LIMIT,
            attempted_versioned=True,
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _reject_request(
            ExecutionRequestRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    if serialized != canonical:
        _reject_request(
            ExecutionRequestRejection.INVALID_VERSIONED,
            attempted_versioned=True,
        )
    return ExecutionRequest(
        identity=identity,
        execution_protocol_version=protocol,
        **body,
    )


def decode_execution_request(
    serialized: object,
    *,
    supported_protocols: ExecutionProtocolRange = SUPPORTED_EXECUTION_PROTOCOL_RANGE,
    expected_identity: ExecutionIdentity | None = None,
    expected_execution_protocol_version: int | None = None,
) -> ExecutionRequest:
    """Bound and decode one exact versioned request before application setup."""
    value, attempted_versioned = _bounded_request_json_loads(serialized)
    if not attempted_versioned:
        _reject_request(
            ExecutionRequestRejection.LEGACY_REQUEST,
            attempted_versioned=False,
        )
    return _decode_versioned_execution_request(
        value,
        serialized,
        supported_protocols=supported_protocols,
        expected_identity=expected_identity,
        expected_execution_protocol_version=expected_execution_protocol_version,
    )


def encode_execution_request(request: ExecutionRequest) -> str:
    """Encode one exact canonical versioned-v1 execution request."""
    try:
        if not isinstance(request, ExecutionRequest):
            raise ValueError
        identity = request.identity
        if (
            not _valid_identity_shape(identity)
            or type(request.execution_protocol_version) is not int
            or not SUPPORTED_EXECUTION_PROTOCOL_RANGE.supports(request.execution_protocol_version)
        ):
            raise ValueError
        value = {
            "request_schema": EXECUTION_REQUEST_SCHEMA,
            "request_schema_version": EXECUTION_REQUEST_SCHEMA_VERSION,
            "execution_protocol_version": request.execution_protocol_version,
            "task_execution_pk": identity.task_execution_pk,
            "task_id": identity.task_id,
            "attempt_number": identity.attempt_number,
            "execution_generation": identity.execution_generation,
            "callable_path": request.callable_path,
            "transport_version": request.transport_version,
            "serialized_args": request.serialized_args,
            "serialized_kwargs": request.serialized_kwargs,
            "input_reference": request.input_reference,
            "runtime_env_profile": request.runtime_env_profile,
            "runtime_env_hash": request.runtime_env_hash,
            "runtime_env_plan_identity": request.runtime_env_plan_identity,
            "compiled_graph_submission_transport": (request.compiled_graph_submission_transport),
        }
        _normalize_request_body(value)
        _validate_json_tree(
            value,
            allow_nonfinite=False,
            allow_nul=False,
            max_depth=EXECUTION_REQUEST_MAX_DEPTH,
            max_nodes=EXECUTION_REQUEST_MAX_NODES,
            max_string_bytes=EXECUTION_REQUEST_MAX_BYTES,
        )
        detached_json = _bounded_json_dumps(
            value,
            sort_keys=False,
            max_bytes=EXECUTION_REQUEST_MAX_BYTES,
        )
        normalized, attempted_versioned = _bounded_request_json_loads(detached_json)
        if not attempted_versioned or not isinstance(normalized, dict):
            raise ValueError
        canonical = _bounded_json_dumps(
            normalized,
            sort_keys=True,
            max_bytes=EXECUTION_REQUEST_MAX_BYTES,
        )
        decode_execution_request(canonical)
    except (
        ExecutionRequestDecodeError,
        _InvalidJsonTreeError,
        _ResourceLimitError,
        _UnsupportedRequestTransportError,
        TypeError,
        ValueError,
        RecursionError,
        UnicodeError,
    ):
        raise ExecutionRequestEncodeError from None
    return canonical


__all__ = [
    "EXECUTION_COMPLETION_DIAGNOSTIC_MAX_BYTES",
    "EXECUTION_COMPLETION_MAX_BYTES",
    "EXECUTION_COMPLETION_MAX_DEPTH",
    "EXECUTION_COMPLETION_MAX_NODES",
    "EXECUTION_COMPLETION_SCHEMA",
    "EXECUTION_COMPLETION_SCHEMA_VERSION",
    "EXECUTION_REQUEST_MAX_BYTES",
    "EXECUTION_REQUEST_MAX_DEPTH",
    "EXECUTION_REQUEST_MAX_NODES",
    "EXECUTION_REQUEST_RUNTIME_ENV_IDENTITY_MAX_BYTES",
    "EXECUTION_REQUEST_SCHEMA",
    "EXECUTION_REQUEST_SCHEMA_VERSION",
    "DecodedExecutionCompletion",
    "ExecutionCompletion",
    "ExecutionCompletionDecodeError",
    "ExecutionCompletionRejection",
    "ExecutionCompletionSource",
    "ExecutionIdentity",
    "ExecutionRequest",
    "ExecutionRequestDecodeError",
    "ExecutionRequestEncodeError",
    "ExecutionRequestRejection",
    "decode_execution_completion",
    "decode_execution_request",
    "decode_legacy_v1_completion",
    "encode_execution_completion",
    "encode_execution_request",
    "encode_execution_request_rejection",
]
