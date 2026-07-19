"""Durable storage helpers for oversized task inputs.

The database keeps the historical ``args_json``/``kwargs_json`` pair for
ordinary inputs.  When the combined, versioned envelope exceeds the configured
threshold, this module stores the envelope through a retrievable payload
backend and leaves JSON ``null`` placeholders for legacy workers.  Such a
worker fails while unpacking the input rather than calling application code
with fabricated empty arguments.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from django.utils import timezone

from django_ray.conf.settings import get_settings
from django_ray.result_storage import (
    FilesystemResultStorage,
    GCSResultStorage,
    PayloadStorageBackend,
    ResultStorageError,
    S3ResultStorage,
)

if TYPE_CHECKING:
    from django_ray.models import TaskInputPayload


INPUT_ENVELOPE_SCHEMA = "django-ray.task-input"
INPUT_ENVELOPE_VERSION = 1
EXTERNAL_INPUT_PLACEHOLDER = "null"
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class InputPayloadError(RuntimeError):
    """Base error for durable task-input preparation and retrieval."""


class InputPayloadValidationError(InputPayloadError):
    """Raised when task input or a stored reference violates the protocol."""


class InputPayloadStorageError(InputPayloadError):
    """Raised when a configured durable storage operation fails."""


@dataclass(frozen=True)
class PreparedTaskInput:
    """Database fields and immutable payload metadata prepared at enqueue."""

    args_json: str
    kwargs_json: str
    input_reference: str | None = None
    serialized_payload: str | None = None
    digest: str | None = None
    size_bytes: int | None = None
    envelope_version: int | None = None
    backend: str | None = None

    @property
    def is_external(self) -> bool:
        """Return whether this input uses a durable storage reference."""
        return self.input_reference is not None


@dataclass(frozen=True)
class InputReferenceMetadata:
    """Validated metadata encoded by a durable input reference."""

    backend: str
    digest: str
    size_bytes: int


def _serialize_json(value: Any, *, description: str) -> str:
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as error:
        raise InputPayloadValidationError(
            f"Task {description} must be JSON-serializable"
        ) from error


def _serialize_envelope(args: list[Any], kwargs: dict[str, Any]) -> str:
    envelope = {
        "args": args,
        "kwargs": kwargs,
        "schema": INPUT_ENVELOPE_SCHEMA,
        "version": INPUT_ENVELOPE_VERSION,
    }
    try:
        return json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise InputPayloadValidationError("Task inputs must be JSON-serializable") from error


def _deserialize_inline(args_json: str, kwargs_json: str) -> tuple[list[Any], dict[str, Any]]:
    try:
        args = json.loads(args_json)
        kwargs = json.loads(kwargs_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise InputPayloadValidationError("Inline task input contains invalid JSON") from error
    if not isinstance(args, list):
        raise InputPayloadValidationError("Inline task args must decode to a list")
    if not isinstance(kwargs, dict):
        raise InputPayloadValidationError("Inline task kwargs must decode to an object")
    return args, kwargs


def _deserialize_envelope(serialized_payload: str) -> tuple[list[Any], dict[str, Any]]:
    try:
        envelope = json.loads(serialized_payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise InputPayloadValidationError("Stored task input contains invalid JSON") from error

    if not isinstance(envelope, dict):
        raise InputPayloadValidationError("Stored task input envelope must be an object")
    expected_keys = {"args", "kwargs", "schema", "version"}
    if set(envelope) != expected_keys:
        raise InputPayloadValidationError("Stored task input envelope has invalid fields")
    if envelope["schema"] != INPUT_ENVELOPE_SCHEMA:
        raise InputPayloadValidationError("Stored task input envelope has an unknown schema")
    if envelope["version"] != INPUT_ENVELOPE_VERSION:
        raise InputPayloadValidationError("Stored task input envelope version is unsupported")

    args = envelope["args"]
    kwargs = envelope["kwargs"]
    if not isinstance(args, list):
        raise InputPayloadValidationError("Stored task input args must be a list")
    if not isinstance(kwargs, dict):
        raise InputPayloadValidationError("Stored task input kwargs must be an object")
    if _serialize_envelope(args, kwargs) != serialized_payload:
        raise InputPayloadValidationError("Stored task input envelope is not canonical")
    return args, kwargs


def _optional_threshold(config: dict[str, Any]) -> int | None:
    threshold = config.get("MAX_INLINE_INPUT_SIZE_BYTES")
    if threshold is None:
        return None
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise InputPayloadValidationError(
            "MAX_INLINE_INPUT_SIZE_BYTES must be a non-negative integer or None"
        )
    return threshold


def _configured_backend_name(config: dict[str, Any]) -> str:
    value = config.get("INPUT_STORAGE_BACKEND")
    if not isinstance(value, str) or value not in {"filesystem", "s3", "gcs"}:
        raise InputPayloadValidationError(
            "INPUT_STORAGE_BACKEND must select filesystem, s3, or gcs"
        )
    return value


def _required_string(config: dict[str, Any], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value:
        raise InputPayloadValidationError(f"{name} must be configured for durable task inputs")
    return value


def _storage_backend(config: dict[str, Any]) -> tuple[str, PayloadStorageBackend]:
    backend_name = _configured_backend_name(config)
    try:
        if backend_name == "filesystem":
            return backend_name, FilesystemResultStorage(
                _required_string(config, "INPUT_STORAGE_FILESYSTEM_PATH")
            )
        if backend_name == "s3":
            endpoint = config.get("INPUT_STORAGE_S3_ENDPOINT_URL")
            region = config.get("INPUT_STORAGE_S3_REGION")
            return backend_name, S3ResultStorage(
                bucket=_required_string(config, "INPUT_STORAGE_S3_BUCKET"),
                prefix=str(config.get("INPUT_STORAGE_S3_PREFIX", "django-ray/inputs")),
                endpoint_url=str(endpoint) if endpoint else None,
                region_name=str(region) if region else None,
            )
        return backend_name, GCSResultStorage(
            bucket=_required_string(config, "INPUT_STORAGE_GCS_BUCKET"),
            prefix=str(config.get("INPUT_STORAGE_GCS_PREFIX", "django-ray/inputs")),
        )
    except ResultStorageError as error:
        raise InputPayloadStorageError("Failed to initialize durable task input storage") from error


def _query_values(reference: str) -> tuple[dict[str, list[str]], Any]:
    if not isinstance(reference, str) or not reference or len(reference) > 500:
        raise InputPayloadValidationError("Task input reference is invalid")
    try:
        parsed = urlparse(reference)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise InputPayloadValidationError("Task input reference is malformed") from error
    if parsed.fragment:
        raise InputPayloadValidationError("Task input reference must not contain a fragment")
    return query, parsed


def _single_query_value(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name)
    if values is None or len(values) != 1 or not values[0]:
        raise InputPayloadValidationError(
            f"Task input reference must contain exactly one {name} value"
        )
    return values[0]


def _parse_size(query: dict[str, list[str]]) -> int:
    raw_size = _single_query_value(query, "bytes")
    try:
        size_bytes = int(raw_size)
    except ValueError as error:
        raise InputPayloadValidationError("Task input reference byte count is invalid") from error
    if size_bytes < 0 or str(size_bytes) != raw_size:
        raise InputPayloadValidationError("Task input reference byte count is invalid")
    return size_bytes


def _expected_object_key(prefix: str, digest: str) -> str:
    clean_prefix = prefix.strip("/")
    suffix = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
    return f"{clean_prefix}/{suffix}" if clean_prefix else suffix


def _validated_reference(reference: str, config: dict[str, Any]) -> InputReferenceMetadata:
    query, parsed = _query_values(reference)
    backend = _configured_backend_name(config)
    size_bytes = _parse_size(query)

    if backend == "filesystem":
        if set(query) != {"bytes", "rel"}:
            raise InputPayloadValidationError("Filesystem task input reference query is invalid")
        if parsed.scheme != "resultfs" or parsed.netloc != "sha256":
            raise InputPayloadValidationError(
                "Task input reference does not match the configured filesystem backend"
            )
        digest = parsed.path.removeprefix("/")
        if _DIGEST_PATTERN.fullmatch(digest) is None:
            raise InputPayloadValidationError("Task input reference digest is invalid")
        relative_path = PurePosixPath(_single_query_value(query, "rel"))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise InputPayloadValidationError("Filesystem task input reference path is unsafe")
        if relative_path.as_posix() != _expected_object_key("", digest):
            raise InputPayloadValidationError(
                "Filesystem task input reference path does not match its digest"
            )
        _required_string(config, "INPUT_STORAGE_FILESYSTEM_PATH")
        return InputReferenceMetadata(backend, digest, size_bytes)

    if set(query) != {"bytes"}:
        raise InputPayloadValidationError("Object-storage task input reference query is invalid")
    key = parsed.path.removeprefix("/")
    digest = PurePosixPath(key).stem
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        raise InputPayloadValidationError("Task input reference digest is invalid")

    if backend == "s3":
        bucket = _required_string(config, "INPUT_STORAGE_S3_BUCKET")
        prefix = str(config.get("INPUT_STORAGE_S3_PREFIX", "django-ray/inputs"))
        expected_scheme = "s3"
    else:
        bucket = _required_string(config, "INPUT_STORAGE_GCS_BUCKET")
        prefix = str(config.get("INPUT_STORAGE_GCS_PREFIX", "django-ray/inputs"))
        expected_scheme = "gs"
    if parsed.scheme != expected_scheme or parsed.netloc != bucket:
        raise InputPayloadValidationError(
            "Task input reference does not match the configured object-storage backend"
        )
    if key != _expected_object_key(prefix, digest):
        raise InputPayloadValidationError(
            "Task input reference key is outside the configured input prefix"
        )
    return InputReferenceMetadata(backend, digest, size_bytes)


def prepare_task_input(
    args: tuple[Any, ...] | list[Any],
    kwargs: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> PreparedTaskInput:
    """Serialize task input and externalize an oversized combined envelope."""
    if not isinstance(args, tuple | list):
        raise InputPayloadValidationError("Task args must be a tuple or list")
    if not isinstance(kwargs, dict):
        raise InputPayloadValidationError("Task kwargs must be an object")

    args_list = list(args)
    args_json = _serialize_json(args_list, description="args")
    kwargs_json = _serialize_json(kwargs, description="kwargs")
    resolved_config = config if config is not None else get_settings()
    threshold = _optional_threshold(resolved_config)
    if threshold is None:
        return PreparedTaskInput(args_json=args_json, kwargs_json=kwargs_json)

    serialized_payload = _serialize_envelope(args_list, kwargs)
    payload_bytes = serialized_payload.encode("utf-8")
    if len(payload_bytes) <= threshold:
        return PreparedTaskInput(args_json=args_json, kwargs_json=kwargs_json)

    backend_name, storage = _storage_backend(resolved_config)
    try:
        reference = storage.store_payload(serialized_payload=serialized_payload)
    except (ResultStorageError, OSError, UnicodeError) as error:
        raise InputPayloadStorageError("Failed to persist durable task input") from error

    metadata = _validated_reference(reference, resolved_config)
    digest = hashlib.sha256(payload_bytes).hexdigest()
    if metadata.digest != digest or metadata.size_bytes != len(payload_bytes):
        raise InputPayloadValidationError(
            "Storage backend returned inconsistent task input metadata"
        )
    return PreparedTaskInput(
        args_json=EXTERNAL_INPUT_PLACEHOLDER,
        kwargs_json=EXTERNAL_INPUT_PLACEHOLDER,
        input_reference=reference,
        serialized_payload=serialized_payload,
        digest=digest,
        size_bytes=len(payload_bytes),
        envelope_version=INPUT_ENVELOPE_VERSION,
        backend=backend_name,
    )


def register_task_input(
    prepared: PreparedTaskInput,
    config: dict[str, Any] | None = None,
) -> TaskInputPayload | None:
    """Register or reactivate an external input while the caller holds a transaction."""
    if not prepared.is_external:
        return None
    if (
        prepared.input_reference is None
        or prepared.serialized_payload is None
        or prepared.digest is None
        or prepared.size_bytes is None
        or prepared.envelope_version is None
        or prepared.backend is None
    ):
        raise InputPayloadValidationError("Prepared external task input is incomplete")

    from django_ray.models import InputPayloadState, TaskInputPayload

    resolved_config = config if config is not None else get_settings()
    metadata = _validated_reference(prepared.input_reference, resolved_config)
    expected_metadata = (
        prepared.backend,
        prepared.digest,
        prepared.size_bytes,
    )
    actual_metadata = (metadata.backend, metadata.digest, metadata.size_bytes)
    if actual_metadata != expected_metadata:
        raise InputPayloadValidationError("Prepared task input metadata is inconsistent")

    now = timezone.now()
    payload, created = TaskInputPayload.objects.select_for_update().get_or_create(
        reference=prepared.input_reference,
        defaults={
            "backend": prepared.backend,
            "digest": prepared.digest,
            "size_bytes": prepared.size_bytes,
            "envelope_version": prepared.envelope_version,
            "state": InputPayloadState.ACTIVE,
            "last_used_at": now,
        },
    )
    if not created:
        persisted_metadata = (
            payload.backend,
            payload.digest,
            payload.size_bytes,
            payload.envelope_version,
        )
        supplied_metadata = (
            prepared.backend,
            prepared.digest,
            prepared.size_bytes,
            prepared.envelope_version,
        )
        if persisted_metadata != supplied_metadata:
            raise InputPayloadValidationError(
                "Existing task input registry metadata is inconsistent"
            )

        update_fields = ["last_used_at"]
        payload.last_used_at = now
        if payload.state == InputPayloadState.PURGED:
            _, storage = _storage_backend(resolved_config)
            try:
                restored_reference = storage.store_payload(
                    serialized_payload=prepared.serialized_payload
                )
            except (ResultStorageError, OSError, UnicodeError) as error:
                raise InputPayloadStorageError("Failed to reactivate durable task input") from error
            if restored_reference != prepared.input_reference:
                raise InputPayloadValidationError(
                    "Storage backend changed an immutable task input reference"
                )
            payload.state = InputPayloadState.ACTIVE
            payload.purged_at = None
            payload.cleanup_error = ""
            update_fields.extend(["state", "purged_at", "cleanup_error"])
        payload.save(update_fields=update_fields)
    return payload


def load_task_input(
    *,
    args_json: str,
    kwargs_json: str,
    input_reference: str | None,
    config: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Load and validate either legacy inline input or a durable envelope."""
    if input_reference is None:
        return _deserialize_inline(args_json, kwargs_json)
    if args_json != EXTERNAL_INPUT_PLACEHOLDER or kwargs_json != EXTERNAL_INPUT_PLACEHOLDER:
        raise InputPayloadValidationError(
            "External task input must use the legacy-worker safety placeholders"
        )

    resolved_config = config if config is not None else get_settings()
    metadata = _validated_reference(input_reference, resolved_config)
    _, storage = _storage_backend(resolved_config)
    try:
        serialized_payload = storage.load(reference=input_reference)
    except (ResultStorageError, OSError, UnicodeError) as error:
        raise InputPayloadStorageError("Failed to load durable task input") from error
    if serialized_payload is None:
        raise InputPayloadStorageError("Durable task input payload is unavailable")

    payload_bytes = serialized_payload.encode("utf-8")
    if len(payload_bytes) != metadata.size_bytes:
        raise InputPayloadValidationError("Durable task input byte count does not match")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    if digest != metadata.digest:
        raise InputPayloadValidationError("Durable task input digest does not match")
    return _deserialize_envelope(serialized_payload)


def delete_input_reference(
    reference: str,
    config: dict[str, Any] | None = None,
) -> None:
    """Validate and delete a durable input object for a purge transaction."""
    resolved_config = config if config is not None else get_settings()
    _validated_reference(reference, resolved_config)
    _, storage = _storage_backend(resolved_config)
    try:
        storage.delete(reference=reference)
    except (ResultStorageError, OSError) as error:
        raise InputPayloadStorageError("Failed to delete durable task input") from error


__all__ = [
    "EXTERNAL_INPUT_PLACEHOLDER",
    "INPUT_ENVELOPE_SCHEMA",
    "INPUT_ENVELOPE_VERSION",
    "InputPayloadError",
    "InputPayloadStorageError",
    "InputPayloadValidationError",
    "InputReferenceMetadata",
    "PreparedTaskInput",
    "delete_input_reference",
    "load_task_input",
    "prepare_task_input",
    "register_task_input",
]
