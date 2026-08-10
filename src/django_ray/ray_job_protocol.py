"""Bounded Ray Job request bindings shared by submitters and executors.

Ray exposes submitted Job metadata to the entrypoint process through
``RAY_JOB_CONFIG_JSON_ENV_VAR``.  A strict request binds the canonical request bytes to
that independent control-plane metadata before Django, task input, or user code
is imported.  Errors from this module are fixed classifications and never
retain attacker-controlled metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Never

from django_ray.execution_codec import (
    EXECUTION_REQUEST_MAX_BYTES,
    ExecutionIdentity,
    ExecutionRequest,
)

RAY_JOB_CONFIG_JSON_ENV_VAR = "RAY_JOB_CONFIG_JSON_ENV_VAR"
RAY_JOB_REQUEST_REJECTED_EXIT_CODE = 78

LEGACY_RAY_JOB_SUBMISSION_ID_PREFIX = "raysubmit_django_ray_v1_"
STRICT_RAY_JOB_SUBMISSION_ID_FAMILY_PREFIX = "raysubmit_django_ray_rq"
STRICT_RAY_JOB_SUBMISSION_ID_PREFIX = "raysubmit_django_ray_rq1_"

RAY_JOB_REQUEST_METADATA_MARKER_KEY = "django_ray_request_binding"
RAY_JOB_REQUEST_METADATA_MARKER_VALUE = "django-ray.ray-job-request-binding/v1"

RAY_JOB_CONFIG_JSON_MAX_BYTES = 4 * 1024 * 1024
RAY_JOB_REQUEST_METADATA_MAX_BYTES = 16 * 1024

_TASK_ID_MAX_CHARS = 255
_MAX_COUNTER = (1 << 63) - 1
_MAX_COUNTER_DECIMAL_DIGITS = 19
_UTF8_CHUNK_CHARS = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STRICT_SUBMISSION_ID = re.compile(
    rf"{re.escape(STRICT_RAY_JOB_SUBMISSION_ID_PREFIX)}[0-9a-f]{{64}}"
)

_TASK_EXECUTION_PK_KEY = "django_ray_task_execution_pk"
_PUBLIC_TASK_ID_KEY = "django_ray_public_task_id"
_ATTEMPT_NUMBER_KEY = "django_ray_attempt_number"
_EXECUTION_GENERATION_KEY = "django_ray_execution_generation"
_EXECUTION_PROTOCOL_VERSION_KEY = "django_ray_execution_protocol_version"
_REQUEST_SHA256_KEY = "django_ray_request_sha256"
_SUBMISSION_TRANSPORT_KEY = "django_ray_submission_transport"

_STRICT_METADATA_KEYS = (
    RAY_JOB_REQUEST_METADATA_MARKER_KEY,
    _TASK_EXECUTION_PK_KEY,
    _PUBLIC_TASK_ID_KEY,
    _ATTEMPT_NUMBER_KEY,
    _EXECUTION_GENERATION_KEY,
    _EXECUTION_PROTOCOL_VERSION_KEY,
    _REQUEST_SHA256_KEY,
    _SUBMISSION_TRANSPORT_KEY,
)
_STRICT_MARKER_KEYS = (
    RAY_JOB_REQUEST_METADATA_MARKER_KEY,
    _TASK_EXECUTION_PK_KEY,
    _PUBLIC_TASK_ID_KEY,
    _EXECUTION_PROTOCOL_VERSION_KEY,
    _REQUEST_SHA256_KEY,
    _SUBMISSION_TRANSPORT_KEY,
)


class RayJobRequestBindingRejection(StrEnum):
    """Stable, secret-safe failures at the Ray Job control-plane boundary."""

    MISSING = "missing"
    INVALID = "invalid"
    RESOURCE_LIMIT = "resource_limit"
    IDENTITY_MISMATCH = "identity_mismatch"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    TRANSPORT_MISMATCH = "transport_mismatch"


class RayJobRequestBindingError(ValueError):
    """Reject a Ray Job request binding without retaining untrusted fields."""

    def __init__(self, classification: RayJobRequestBindingRejection) -> None:
        self.classification = classification
        super().__init__(f"Ray Job request binding rejected: {classification.value}")


@dataclass(frozen=True, slots=True)
class RayJobRequestExpectation:
    """Validated control-plane values that one request must match exactly."""

    identity: ExecutionIdentity
    execution_protocol_version: int
    request_sha256: str
    submission_transport: str


class _DuplicateKeyError(ValueError):
    pass


def _reject(classification: RayJobRequestBindingRejection) -> Never:
    raise RayJobRequestBindingError(classification) from None


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value


def _valid_counter(value: object) -> bool:
    return type(value) is int and 0 < value <= _MAX_COUNTER


def _valid_generation(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_COUNTER


def _valid_identity(identity: ExecutionIdentity) -> bool:
    return (
        _valid_counter(identity.task_execution_pk)
        and type(identity.task_id) is str
        and 0 < len(identity.task_id) <= _TASK_ID_MAX_CHARS
        and _valid_counter(identity.attempt_number)
        and _valid_generation(identity.execution_generation)
    )


def _bounded_utf8_chunks(value: object, *, max_bytes: int) -> Iterator[bytes]:
    """Yield bounded UTF-8 chunks without duplicating one whole input string."""
    if type(value) is not str:
        _reject(RayJobRequestBindingRejection.INVALID)
    if len(value) > max_bytes:
        _reject(RayJobRequestBindingRejection.RESOURCE_LIMIT)

    encoded_size = 0
    for offset in range(0, len(value), _UTF8_CHUNK_CHARS):
        try:
            chunk = value[offset : offset + _UTF8_CHUNK_CHARS].encode("utf-8")
        except UnicodeError:
            _reject(RayJobRequestBindingRejection.INVALID)
        encoded_size += len(chunk)
        if encoded_size > max_bytes:
            _reject(RayJobRequestBindingRejection.RESOURCE_LIMIT)
        yield chunk


def _bounded_utf8_size(value: object, *, max_bytes: int) -> int:
    """Return UTF-8 size while retaining at most one bounded encoded chunk."""
    return sum(len(chunk) for chunk in _bounded_utf8_chunks(value, max_bytes=max_bytes))


def _bounded_metadata_values_size(values: Iterable[object]) -> int:
    """Bound all selected metadata values before creating canonical JSON."""
    encoded_size = 0
    for value in values:
        remaining = RAY_JOB_REQUEST_METADATA_MAX_BYTES - encoded_size
        encoded_size += _bounded_utf8_size(value, max_bytes=remaining)
    return encoded_size


def _bounded_json_int(value: object) -> int:
    """Parse one JSON integer token without consulting Python's digit limit."""
    if type(value) is not str:
        _reject(RayJobRequestBindingRejection.INVALID)
    digits = value[1:] if value.startswith("-") else value
    if (
        not digits
        or len(digits) > _MAX_COUNTER_DECIMAL_DIGITS
        or not digits.isascii()
        or not digits.isdecimal()
    ):
        _reject(RayJobRequestBindingRejection.INVALID)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        _reject(RayJobRequestBindingRejection.INVALID)
    return parsed


def _parse_counter(value: object, *, allow_zero: bool = False) -> int:
    parsed = _bounded_json_int(value)
    if not (_valid_generation(parsed) if allow_zero else _valid_counter(parsed)):
        _reject(RayJobRequestBindingRejection.INVALID)
    return parsed


def request_sha256(serialized_request: str) -> str:
    """Return the digest of the exact canonical UTF-8 request bytes."""
    digest = hashlib.sha256()
    for chunk in _bounded_utf8_chunks(
        serialized_request,
        max_bytes=EXECUTION_REQUEST_MAX_BYTES,
    ):
        digest.update(chunk)
    return digest.hexdigest()


def is_strict_ray_job_submission_id(value: object) -> bool:
    """Return whether an ID selects the strict path, even if its suffix is bad.

    Treating the prefix itself as the marker prevents a corrupted strict ID
    from being reinterpreted as a released legacy submission.
    """
    return type(value) is str and value.startswith(STRICT_RAY_JOB_SUBMISSION_ID_FAMILY_PREFIX)


def is_valid_strict_ray_job_submission_id(value: object) -> bool:
    """Return whether an ID is one canonical strict Ray Job submission ID."""
    return type(value) is str and _STRICT_SUBMISSION_ID.fullmatch(value) is not None


def build_ray_job_request_metadata(
    request: ExecutionRequest,
    serialized_request: str,
) -> dict[str, str]:
    """Build bounded metadata that independently binds one canonical request."""
    if not _valid_identity(request.identity) or not _valid_counter(
        request.execution_protocol_version
    ):
        _reject(RayJobRequestBindingRejection.INVALID)
    if request.compiled_graph_submission_transport != "ray-job":
        _reject(RayJobRequestBindingRejection.TRANSPORT_MISMATCH)

    metadata = {
        RAY_JOB_REQUEST_METADATA_MARKER_KEY: RAY_JOB_REQUEST_METADATA_MARKER_VALUE,
        _TASK_EXECUTION_PK_KEY: str(request.identity.task_execution_pk),
        _PUBLIC_TASK_ID_KEY: request.identity.task_id,
        _ATTEMPT_NUMBER_KEY: str(request.identity.attempt_number),
        _EXECUTION_GENERATION_KEY: str(request.identity.execution_generation),
        _EXECUTION_PROTOCOL_VERSION_KEY: str(request.execution_protocol_version),
        _REQUEST_SHA256_KEY: request_sha256(serialized_request),
        _SUBMISSION_TRANSPORT_KEY: "ray-job",
    }
    _bounded_metadata_values_size(metadata.values())
    try:
        serialized_metadata = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError):
        _reject(RayJobRequestBindingRejection.INVALID)
    _bounded_utf8_size(
        serialized_metadata,
        max_bytes=RAY_JOB_REQUEST_METADATA_MAX_BYTES,
    )
    return metadata


def ray_job_metadata_has_strict_marker(metadata: object) -> bool:
    """Return whether user metadata contains any strict reserved field."""
    return type(metadata) is dict and any(key in metadata for key in _STRICT_MARKER_KEYS)


def parse_ray_job_request_metadata(
    metadata: object,
    *,
    required: bool = False,
) -> RayJobRequestExpectation | None:
    """Parse bounded Ray Job user metadata into a trusted expectation."""
    if type(metadata) is not dict:
        if required:
            _reject(RayJobRequestBindingRejection.MISSING)
        return None
    if not ray_job_metadata_has_strict_marker(metadata):
        if required:
            _reject(RayJobRequestBindingRejection.MISSING)
        return None

    selected: dict[str, object] = {key: metadata.get(key) for key in _STRICT_METADATA_KEYS}
    _bounded_metadata_values_size(selected.values())
    try:
        serialized_metadata = json.dumps(
            selected,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError):
        _reject(RayJobRequestBindingRejection.INVALID)
    _bounded_utf8_size(
        serialized_metadata,
        max_bytes=RAY_JOB_REQUEST_METADATA_MAX_BYTES,
    )

    if selected[RAY_JOB_REQUEST_METADATA_MARKER_KEY] != RAY_JOB_REQUEST_METADATA_MARKER_VALUE:
        _reject(RayJobRequestBindingRejection.INVALID)
    task_id = selected[_PUBLIC_TASK_ID_KEY]
    digest = selected[_REQUEST_SHA256_KEY]
    submission_transport = selected[_SUBMISSION_TRANSPORT_KEY]
    if (
        type(task_id) is not str
        or not 0 < len(task_id) <= _TASK_ID_MAX_CHARS
        or type(digest) is not str
        or _SHA256.fullmatch(digest) is None
        or submission_transport != "ray-job"
    ):
        _reject(RayJobRequestBindingRejection.INVALID)

    identity = ExecutionIdentity(
        task_execution_pk=_parse_counter(selected[_TASK_EXECUTION_PK_KEY]),
        task_id=task_id,
        attempt_number=_parse_counter(selected[_ATTEMPT_NUMBER_KEY]),
        execution_generation=_parse_counter(
            selected[_EXECUTION_GENERATION_KEY],
            allow_zero=True,
        ),
    )
    execution_protocol_version = _parse_counter(selected[_EXECUTION_PROTOCOL_VERSION_KEY])
    return RayJobRequestExpectation(
        identity=identity,
        execution_protocol_version=execution_protocol_version,
        request_sha256=digest,
        submission_transport=submission_transport,
    )


def load_ray_job_request_expectation(
    config_json: str | None,
) -> RayJobRequestExpectation | None:
    """Load strict metadata from bounded ``RAY_JOB_CONFIG_JSON_ENV_VAR`` input."""
    if config_json is None:
        return None
    if type(config_json) is not str:
        _reject(RayJobRequestBindingRejection.INVALID)
    _bounded_utf8_size(config_json, max_bytes=RAY_JOB_CONFIG_JSON_MAX_BYTES)
    try:
        config = json.loads(
            config_json,
            object_pairs_hook=_duplicate_safe_object,
            parse_int=_bounded_json_int,
        )
    except (ValueError, OverflowError, RecursionError):
        _reject(RayJobRequestBindingRejection.INVALID)
    if type(config) is not dict:
        _reject(RayJobRequestBindingRejection.INVALID)
    return parse_ray_job_request_metadata(config.get("metadata"))


def validate_ray_job_request_expectation(
    expectation: RayJobRequestExpectation,
    *,
    expected_identity: ExecutionIdentity,
    expected_execution_protocol_version: int,
    serialized_request: str | None = None,
    expected_submission_transport: str = "ray-job",
) -> None:
    """Compare one parsed expectation with independently known request values."""
    if expectation.identity != expected_identity:
        _reject(RayJobRequestBindingRejection.IDENTITY_MISMATCH)
    if expectation.execution_protocol_version != expected_execution_protocol_version:
        _reject(RayJobRequestBindingRejection.PROTOCOL_MISMATCH)
    if expectation.submission_transport != expected_submission_transport:
        _reject(RayJobRequestBindingRejection.TRANSPORT_MISMATCH)
    if (
        serialized_request is not None
        and request_sha256(serialized_request) != expectation.request_sha256
    ):
        _reject(RayJobRequestBindingRejection.DIGEST_MISMATCH)


def fixed_safe_ray_job_metadata(metadata: object) -> dict[str, str] | None:
    """Copy only bounded protocol fields from Ray's untrusted JobInfo metadata.

    A present field with an invalid value becomes an empty fixed sentinel.  That
    preserves strict-marker and partial-binding evidence for the worker while
    avoiding retention of arbitrary metadata, secrets, or oversized values.
    """
    if type(metadata) is not dict:
        return None
    selected: dict[str, str] = {}
    for key in _STRICT_METADATA_KEYS:
        if key not in metadata:
            continue
        value = metadata[key]
        try:
            valid_value = (
                type(value) is str
                and len(value) <= 1024
                and _bounded_utf8_size(value, max_bytes=1024) <= 1024
            )
        except RayJobRequestBindingError:
            valid_value = False
        if not valid_value:
            selected[key] = ""
        else:
            selected[key] = value
    return selected or None
