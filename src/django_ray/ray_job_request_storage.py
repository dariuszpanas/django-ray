"""Content-addressed storage for bounded Ray Job execution requests.

The locator codec in this module is deliberately independent from Django.
Ray Job drivers can therefore authorize and load one canonical request before
settings setup, model import, task-input hydration, or application import.
Only the manager-side registry attachment helper imports Django and the ORM.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never
from urllib.parse import urlsplit

from django_ray.execution_codec import (
    EXECUTION_REQUEST_MAX_BYTES,
    EXECUTION_REQUEST_SCHEMA_VERSION,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionRequestDecodeError,
    ExecutionRequestRejection,
    decode_execution_request,
)
from django_ray.result_storage import (
    FilesystemResultStorage,
    GCSResultStorage,
    PayloadStorageBackend,
    ResultStorageError,
    ResultStorageIntegrityError,
    S3ResultStorage,
    _build_object_key,
    _canonical_authority,
    _canonical_object_prefix,
    _object_reference,
    _parse_result_reference,
    _validate_object_reference,
)

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution
    from django_ray.runner.base import SubmissionHandle


RAY_JOB_REQUEST_LOCATOR_SCHEMA = "django-ray.ray-job-request-locator"
RAY_JOB_REQUEST_LOCATOR_VERSION = 1
RAY_JOB_REQUEST_LOCATOR_MAX_BYTES = 4096
RAY_JOB_REQUEST_LOCATOR_MAX_CHARS = (RAY_JOB_REQUEST_LOCATOR_MAX_BYTES * 4 + 2) // 3
RAY_JOB_REQUEST_REFERENCE_MAX_BYTES = 500
RAY_JOB_REQUEST_REFERENCE_MAX_CHARS = RAY_JOB_REQUEST_REFERENCE_MAX_BYTES

_DEFAULT_INPUT_S3_PREFIX = "django-ray/inputs"
_DEFAULT_INPUT_GCS_PREFIX = "django-ray/inputs"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REGION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_MAX_COUNTER = (1 << 63) - 1
_LOCATION_VALUE_MAX_BYTES = 2048
_UTF8_CHUNK_CHARS = 512

_COMMON_LOCATOR_KEYS = frozenset(
    {
        "backend",
        "digest",
        "reference",
        "request_size_bytes",
        "schema",
        "version",
    }
)
_FILESYSTEM_LOCATOR_KEYS = _COMMON_LOCATOR_KEYS | {"filesystem_path"}
_S3_LOCATOR_KEYS = _COMMON_LOCATOR_KEYS | {
    "s3_bucket",
    "s3_endpoint_url",
    "s3_prefix",
    "s3_region",
}
_GCS_LOCATOR_KEYS = _COMMON_LOCATOR_KEYS | {"gcs_bucket", "gcs_prefix"}


class RayJobRequestStorageRejection(StrEnum):
    """Fixed, redaction-safe request storage failure classifications."""

    INVALID_LOCATOR = "invalid_locator"
    RESOURCE_LIMIT = "resource_limit"
    CONFIGURATION = "configuration"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INTEGRITY_MISMATCH = "integrity_mismatch"
    INVALID_REQUEST = "invalid_request"
    REGISTRY_MISMATCH = "registry_mismatch"
    BINDING_MISMATCH = "binding_mismatch"


class RayJobRequestStorageError(RuntimeError):
    """Reject request storage without retaining configuration or payload data."""

    def __init__(self, classification: RayJobRequestStorageRejection) -> None:
        self.classification = classification
        super().__init__(f"Ray Job request storage rejected: {classification.value}")


class RayJobRequestLoadError(RayJobRequestStorageError):
    """Reject one pre-Django request locator or payload load."""


@dataclass(frozen=True, slots=True)
class RayJobRequestLocator:
    """Validated non-secret location and content identity for one request."""

    backend: str
    reference: str
    digest: str
    size_bytes: int
    filesystem_path: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    gcs_bucket: str | None = None
    gcs_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedRayJobRequest:
    """Manager-side stored request plus its bounded driver locator."""

    serialized_request: str
    request: ExecutionRequest
    reference: str
    locator: RayJobRequestLocator
    locator_json: str
    encoded_locator: str
    backend: str
    digest: str
    size_bytes: int
    envelope_version: int = EXECUTION_REQUEST_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class LoadedRayJobRequest:
    """Strict request recovered and validated before Django bootstrap."""

    serialized_request: str
    request: ExecutionRequest
    locator: RayJobRequestLocator
    reference: str
    digest: str
    size_bytes: int


class _DuplicateLocatorKeyError(ValueError):
    pass


def _reject(
    classification: RayJobRequestStorageRejection,
    *,
    load: bool = False,
) -> Never:
    error_type = RayJobRequestLoadError if load else RayJobRequestStorageError
    raise error_type(classification) from None


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateLocatorKeyError
        value[key] = item
    return value


def _bounded_json_int(value: str) -> int:
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise ValueError
    return int(value)


def _reject_constant(_value: str) -> Never:
    raise ValueError


def _required_location(value: object) -> str:
    if not _bounded_printable_text(value, allow_empty=False):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    assert isinstance(value, str)
    return value


def _bounded_printable_text(value: object, *, allow_empty: bool) -> bool:
    if (
        type(value) is not str
        or (not allow_empty and not value)
        or len(value) > _LOCATION_VALUE_MAX_BYTES
    ):
        return False
    encoded_size = 0
    for offset in range(0, len(value), _UTF8_CHUNK_CHARS):
        chunk = value[offset : offset + _UTF8_CHUNK_CHARS]
        if any(not character.isprintable() for character in chunk):
            return False
        try:
            encoded_size += len(chunk.encode("utf-8"))
        except UnicodeError:
            return False
        if encoded_size > _LOCATION_VALUE_MAX_BYTES:
            return False
    return True


def _optional_region(value: object) -> str | None:
    if value is None:
        return None
    if (
        not _bounded_printable_text(value, allow_empty=False)
        or not isinstance(value, str)
        or len(value) > 128
        or _REGION.fullmatch(value) is None
    ):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    return value


def _optional_endpoint(value: object) -> str | None:
    if value is None:
        return None
    if not _bounded_printable_text(value, allow_empty=False):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    assert isinstance(value, str)
    parsed: Any | None = None
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except (TypeError, ValueError):
        parsed = None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    return value


def _configured_prefix(config: Mapping[str, Any], name: str, default: str) -> str:
    value = config.get(name)
    if value is None:
        return default
    if not _bounded_printable_text(value, allow_empty=True):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    assert isinstance(value, str)
    return value


def _storage_location(
    config: Mapping[str, Any],
) -> tuple[str, dict[str, str | None]]:
    if not isinstance(config, Mapping):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    backend_name = config.get("INPUT_STORAGE_BACKEND")
    try:
        if backend_name == "filesystem":
            storage = FilesystemResultStorage(
                _required_location(config.get("INPUT_STORAGE_FILESYSTEM_PATH"))
            )
            return backend_name, {"filesystem_path": str(storage.root_path)}
        if backend_name == "s3":
            bucket = _canonical_authority(_required_location(config.get("INPUT_STORAGE_S3_BUCKET")))
            prefix = _canonical_object_prefix(
                _configured_prefix(
                    config,
                    "INPUT_STORAGE_S3_PREFIX",
                    _DEFAULT_INPUT_S3_PREFIX,
                )
            )
            return (
                backend_name,
                {
                    "s3_bucket": bucket,
                    "s3_prefix": prefix,
                    "s3_region": _optional_region(config.get("INPUT_STORAGE_S3_REGION")),
                    "s3_endpoint_url": _optional_endpoint(
                        config.get("INPUT_STORAGE_S3_ENDPOINT_URL")
                    ),
                },
            )
        if backend_name == "gcs":
            bucket = _canonical_authority(
                _required_location(config.get("INPUT_STORAGE_GCS_BUCKET"))
            )
            prefix = _canonical_object_prefix(
                _configured_prefix(
                    config,
                    "INPUT_STORAGE_GCS_PREFIX",
                    _DEFAULT_INPUT_GCS_PREFIX,
                )
            )
            return backend_name, {"gcs_bucket": bucket, "gcs_prefix": prefix}
    except RayJobRequestStorageError:
        raise
    except ResultStorageError:
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    _reject(RayJobRequestStorageRejection.CONFIGURATION)


def _configured_storage(
    config: Mapping[str, Any],
) -> tuple[str, PayloadStorageBackend, dict[str, str | None]]:
    backend_name, location = _storage_location(config)
    try:
        if backend_name == "filesystem":
            return backend_name, FilesystemResultStorage(str(location["filesystem_path"])), location
        if backend_name == "s3":
            storage = S3ResultStorage(
                bucket=str(location["s3_bucket"]),
                prefix=str(location["s3_prefix"]),
                endpoint_url=location["s3_endpoint_url"],
                region_name=location["s3_region"],
            )
            return backend_name, storage, location
        storage = GCSResultStorage(
            bucket=str(location["gcs_bucket"]),
            prefix=str(location["gcs_prefix"]),
        )
        return backend_name, storage, location
    except RayJobRequestStorageError:
        raise
    except ResultStorageError:
        _reject(RayJobRequestStorageRejection.CONFIGURATION)


def _validated_reference(
    reference: object,
    *,
    backend: str,
    location: Mapping[str, str | None],
    load: bool = False,
) -> tuple[str, int]:
    if type(reference) is not str or len(reference) > RAY_JOB_REQUEST_REFERENCE_MAX_CHARS:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=load)
    try:
        metadata = _parse_result_reference(reference)
        if backend == "filesystem":
            if metadata.scheme != "resultfs" or not location.get("filesystem_path"):
                raise ResultStorageError
        elif backend == "s3":
            _validate_object_reference(
                metadata,
                scheme="s3",
                bucket=str(location.get("s3_bucket") or ""),
                prefix=str(location.get("s3_prefix") or ""),
            )
        elif backend == "gcs":
            _validate_object_reference(
                metadata,
                scheme="gs",
                bucket=str(location.get("gcs_bucket") or ""),
                prefix=str(location.get("gcs_prefix") or ""),
            )
        else:
            raise ResultStorageError
    except ResultStorageError:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=load)
    return metadata.digest, metadata.size_bytes


def _locator_payload(locator: RayJobRequestLocator) -> dict[str, Any]:
    if not isinstance(locator, RayJobRequestLocator):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    if (
        type(locator.backend) is not str
        or len(locator.backend) > len("filesystem")
        or locator.backend not in {"filesystem", "s3", "gcs"}
        or type(locator.reference) is not str
        or not locator.reference
        or len(locator.reference) > RAY_JOB_REQUEST_REFERENCE_MAX_CHARS
        or not locator.reference.isascii()
        or type(locator.digest) is not str
        or len(locator.digest) != 64
        or _DIGEST.fullmatch(locator.digest) is None
        or type(locator.size_bytes) is not int
        or not 0 < locator.size_bytes <= EXECUTION_REQUEST_MAX_BYTES
    ):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    for value in (
        locator.reference,
        locator.filesystem_path,
        locator.s3_bucket,
        locator.s3_prefix,
        locator.s3_region,
        locator.s3_endpoint_url,
        locator.gcs_bucket,
        locator.gcs_prefix,
    ):
        if value is not None and not _bounded_printable_text(value, allow_empty=True):
            _reject(RayJobRequestStorageRejection.CONFIGURATION)
    if locator.backend == "filesystem" and (
        locator.filesystem_path is None
        or any(
            value is not None
            for value in (
                locator.s3_bucket,
                locator.s3_prefix,
                locator.s3_region,
                locator.s3_endpoint_url,
                locator.gcs_bucket,
                locator.gcs_prefix,
            )
        )
    ):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    if locator.backend == "s3" and (
        locator.s3_bucket is None
        or locator.s3_prefix is None
        or any(
            value is not None
            for value in (
                locator.filesystem_path,
                locator.gcs_bucket,
                locator.gcs_prefix,
            )
        )
    ):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    if locator.backend == "gcs" and (
        locator.gcs_bucket is None
        or locator.gcs_prefix is None
        or any(
            value is not None
            for value in (
                locator.filesystem_path,
                locator.s3_bucket,
                locator.s3_prefix,
                locator.s3_region,
                locator.s3_endpoint_url,
            )
        )
    ):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    common: dict[str, Any] = {
        "backend": locator.backend,
        "digest": locator.digest,
        "reference": locator.reference,
        "request_size_bytes": locator.size_bytes,
        "schema": RAY_JOB_REQUEST_LOCATOR_SCHEMA,
        "version": RAY_JOB_REQUEST_LOCATOR_VERSION,
    }
    if locator.backend == "filesystem":
        return {**common, "filesystem_path": locator.filesystem_path}
    if locator.backend == "s3":
        return {
            **common,
            "s3_bucket": locator.s3_bucket,
            "s3_endpoint_url": locator.s3_endpoint_url,
            "s3_prefix": locator.s3_prefix,
            "s3_region": locator.s3_region,
        }
    if locator.backend == "gcs":
        return {
            **common,
            "gcs_bucket": locator.gcs_bucket,
            "gcs_prefix": locator.gcs_prefix,
        }
    _reject(RayJobRequestStorageRejection.CONFIGURATION)


def serialize_ray_job_request_locator(locator: RayJobRequestLocator) -> str:
    """Serialize one validated locator as bounded canonical JSON."""
    try:
        serialized = json.dumps(
            _locator_payload(locator),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    if len(encoded) > RAY_JOB_REQUEST_LOCATOR_MAX_BYTES:
        _reject(RayJobRequestStorageRejection.RESOURCE_LIMIT)
    return serialized


def encode_ray_job_request_locator(locator: RayJobRequestLocator) -> str:
    """Encode canonical locator JSON as strict unpadded base64url."""
    serialized = serialize_ray_job_request_locator(locator)
    encoded = base64.urlsafe_b64encode(serialized.encode("utf-8")).rstrip(b"=")
    if len(encoded) > RAY_JOB_REQUEST_LOCATOR_MAX_CHARS:
        _reject(RayJobRequestStorageRejection.RESOURCE_LIMIT)
    return encoded.decode("ascii")


def _decoded_location(value: dict[str, Any], *, backend: str) -> dict[str, str | None]:
    if backend == "filesystem":
        if set(value) != _FILESYSTEM_LOCATOR_KEYS:
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        path = value["filesystem_path"]
        if not _bounded_printable_text(path, allow_empty=False):
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        assert isinstance(path, str)
        resolved_path: Path | None = None
        try:
            resolved_path = Path(path).resolve(strict=False)
        except (OSError, OverflowError, RuntimeError, ValueError):
            pass
        if resolved_path is None or str(resolved_path) != path:
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        return {"filesystem_path": path}
    if backend == "s3":
        if set(value) != _S3_LOCATOR_KEYS:
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        bucket = value["s3_bucket"]
        prefix = value["s3_prefix"]
        endpoint = value["s3_endpoint_url"]
        region = value["s3_region"]
        try:
            validated_endpoint = _optional_endpoint(endpoint)
            validated_region = _optional_region(region)
        except RayJobRequestStorageError:
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        if not _bounded_printable_text(bucket, allow_empty=False) or not _bounded_printable_text(
            prefix, allow_empty=True
        ):
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        assert isinstance(bucket, str)
        assert isinstance(prefix, str)
        return {
            "s3_bucket": bucket,
            "s3_prefix": prefix,
            "s3_endpoint_url": validated_endpoint,
            "s3_region": validated_region,
        }
    if backend == "gcs":
        if set(value) != _GCS_LOCATOR_KEYS:
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        bucket = value["gcs_bucket"]
        prefix = value["gcs_prefix"]
        if not _bounded_printable_text(bucket, allow_empty=False) or not _bounded_printable_text(
            prefix, allow_empty=True
        ):
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
        assert isinstance(bucket, str)
        assert isinstance(prefix, str)
        return {"gcs_bucket": bucket, "gcs_prefix": prefix}
    _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)


def _locator_from_payload(value: object, serialized: str) -> RayJobRequestLocator:
    if type(value) is not dict:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    if (
        type(value.get("schema")) is not str
        or value.get("schema") != RAY_JOB_REQUEST_LOCATOR_SCHEMA
        or type(value.get("version")) is not int
        or value.get("version") != RAY_JOB_REQUEST_LOCATOR_VERSION
    ):
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    backend = value.get("backend")
    if type(backend) is not str:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    location = _decoded_location(value, backend=backend)
    digest = value.get("digest")
    size_bytes = value.get("request_size_bytes")
    if (
        type(digest) is not str
        or _DIGEST.fullmatch(digest) is None
        or type(size_bytes) is not int
        or not 0 < size_bytes <= EXECUTION_REQUEST_MAX_BYTES
    ):
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    reference_digest, reference_size = _validated_reference(
        value.get("reference"),
        backend=backend,
        location=location,
        load=True,
    )
    if digest != reference_digest or size_bytes != reference_size:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH, load=True)
    locator = RayJobRequestLocator(
        backend=backend,
        reference=value["reference"],
        digest=digest,
        size_bytes=size_bytes,
        filesystem_path=location.get("filesystem_path"),
        s3_bucket=location.get("s3_bucket"),
        s3_prefix=location.get("s3_prefix"),
        s3_region=location.get("s3_region"),
        s3_endpoint_url=location.get("s3_endpoint_url"),
        gcs_bucket=location.get("gcs_bucket"),
        gcs_prefix=location.get("gcs_prefix"),
    )
    try:
        canonical = serialize_ray_job_request_locator(locator)
    except RayJobRequestStorageError:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    if serialized != canonical:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    return locator


def decode_ray_job_request_locator(encoded_locator: object) -> RayJobRequestLocator:
    """Decode and validate one bounded locator without storage or Django I/O."""
    if type(encoded_locator) is not str:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    if len(encoded_locator) > RAY_JOB_REQUEST_LOCATOR_MAX_CHARS:
        _reject(RayJobRequestStorageRejection.RESOURCE_LIMIT, load=True)
    if not encoded_locator or _BASE64URL.fullmatch(encoded_locator) is None:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    try:
        encoded_bytes = encoded_locator.encode("ascii")
        padding = b"=" * ((4 - len(encoded_bytes) % 4) % 4)
        decoded = base64.b64decode(
            encoded_bytes + padding,
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeError, binascii.Error, ValueError):
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    if len(decoded) > RAY_JOB_REQUEST_LOCATOR_MAX_BYTES:
        _reject(RayJobRequestStorageRejection.RESOURCE_LIMIT, load=True)
    if base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded_bytes:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    try:
        serialized = decoded.decode("utf-8")
        value = json.loads(
            serialized,
            object_pairs_hook=_duplicate_safe_object,
            parse_int=_bounded_json_int,
            parse_constant=_reject_constant,
        )
    except UnicodeError:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    except (_DuplicateLocatorKeyError, TypeError, ValueError, RecursionError):
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    return _locator_from_payload(value, serialized)


def _build_locator(
    *,
    reference: str,
    backend: str,
    location: Mapping[str, str | None],
) -> RayJobRequestLocator:
    digest, size_bytes = _validated_reference(
        reference,
        backend=backend,
        location=location,
    )
    return RayJobRequestLocator(
        backend=backend,
        reference=reference,
        digest=digest,
        size_bytes=size_bytes,
        filesystem_path=location.get("filesystem_path"),
        s3_bucket=location.get("s3_bucket"),
        s3_prefix=location.get("s3_prefix"),
        s3_region=location.get("s3_region"),
        s3_endpoint_url=location.get("s3_endpoint_url"),
        gcs_bucket=location.get("gcs_bucket"),
        gcs_prefix=location.get("gcs_prefix"),
    )


def ray_job_request_reference_content_identity(reference: object) -> tuple[str, int]:
    """Return canonical retrievable reference content identity without I/O."""
    if (
        type(reference) is not str
        or not reference
        or len(reference) > RAY_JOB_REQUEST_REFERENCE_MAX_CHARS
        or not reference.isascii()
    ):
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR)
    try:
        metadata = _parse_result_reference(reference)
    except ResultStorageError:
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR)
    if (
        metadata.scheme not in {"resultfs", "s3", "gs"}
        or not 0 < metadata.size_bytes <= EXECUTION_REQUEST_MAX_BYTES
    ):
        _reject(RayJobRequestStorageRejection.INVALID_LOCATOR)
    return metadata.digest, metadata.size_bytes


def validate_ray_job_request_storage_config(config: Mapping[str, Any]) -> None:
    """Validate a bounded retrievable request location without object-store I/O."""
    backend, location = _storage_location(config)
    digest = "0" * 64
    size_bytes = EXECUTION_REQUEST_MAX_BYTES
    try:
        if backend == "filesystem":
            relative_path = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
            reference = f"resultfs://sha256/{digest}?rel={relative_path}&bytes={size_bytes}"
        elif backend == "s3":
            reference = _object_reference(
                scheme="s3",
                bucket=str(location["s3_bucket"]),
                key=_build_object_key(str(location["s3_prefix"]), digest),
                size_bytes=size_bytes,
            )
        else:
            reference = _object_reference(
                scheme="gs",
                bucket=str(location["gcs_bucket"]),
                key=_build_object_key(str(location["gcs_prefix"]), digest),
                size_bytes=size_bytes,
            )
        locator = _build_locator(
            reference=reference,
            backend=backend,
            location=location,
        )
        serialize_ray_job_request_locator(locator)
        encode_ray_job_request_locator(locator)
    except RayJobRequestStorageError:
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    except (ResultStorageError, TypeError, ValueError, UnicodeError):
        _reject(RayJobRequestStorageRejection.CONFIGURATION)


def prepare_ray_job_request(
    serialized_request: str,
    config: Mapping[str, Any],
) -> PreparedRayJobRequest:
    """Validate and immediately persist one canonical execution request."""
    try:
        request = decode_execution_request(serialized_request)
        payload = serialized_request.encode("utf-8")
    except ExecutionRequestDecodeError as error:
        classification = (
            RayJobRequestStorageRejection.RESOURCE_LIMIT
            if error.classification is ExecutionRequestRejection.RESOURCE_LIMIT
            else RayJobRequestStorageRejection.INVALID_REQUEST
        )
        _reject(classification)
    except (AttributeError, UnicodeError):
        _reject(RayJobRequestStorageRejection.INVALID_REQUEST)
    digest = hashlib.sha256(payload).hexdigest()
    size_bytes = len(payload)
    backend, storage, location = _configured_storage(config)
    try:
        reference = storage.store_payload(serialized_payload=serialized_request)
    except ResultStorageIntegrityError:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)
    except (ResultStorageError, OSError, UnicodeError):
        _reject(RayJobRequestStorageRejection.STORAGE_UNAVAILABLE)
    try:
        locator = _build_locator(
            reference=reference,
            backend=backend,
            location=location,
        )
    except RayJobRequestStorageError:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)
    if locator.digest != digest or locator.size_bytes != size_bytes:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)
    try:
        locator_json = serialize_ray_job_request_locator(locator)
        encoded_locator = encode_ray_job_request_locator(locator)
    except RayJobRequestStorageError:
        _reject(RayJobRequestStorageRejection.CONFIGURATION)
    return PreparedRayJobRequest(
        serialized_request=serialized_request,
        request=request,
        reference=reference,
        locator=locator,
        locator_json=locator_json,
        encoded_locator=encoded_locator,
        backend=backend,
        digest=digest,
        size_bytes=size_bytes,
    )


def _storage_for_locator(locator: RayJobRequestLocator) -> PayloadStorageBackend:
    try:
        if locator.backend == "filesystem" and locator.filesystem_path is not None:
            return FilesystemResultStorage(locator.filesystem_path)
        if (
            locator.backend == "s3"
            and locator.s3_bucket is not None
            and locator.s3_prefix is not None
        ):
            return S3ResultStorage(
                bucket=locator.s3_bucket,
                prefix=locator.s3_prefix,
                endpoint_url=locator.s3_endpoint_url,
                region_name=locator.s3_region,
            )
        if (
            locator.backend == "gcs"
            and locator.gcs_bucket is not None
            and locator.gcs_prefix is not None
        ):
            return GCSResultStorage(
                bucket=locator.gcs_bucket,
                prefix=locator.gcs_prefix,
            )
    except ResultStorageError:
        _reject(RayJobRequestStorageRejection.CONFIGURATION, load=True)
    _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)


def load_ray_job_request(
    locator: object | RayJobRequestLocator,
    *,
    expected_identity: ExecutionIdentity | None = None,
    expected_execution_protocol_version: int | None = None,
) -> LoadedRayJobRequest:
    """Load and strictly validate one request without consulting Django."""
    if isinstance(locator, RayJobRequestLocator):
        try:
            encoded_locator = encode_ray_job_request_locator(locator)
        except RayJobRequestStorageError as error:
            classification = (
                RayJobRequestStorageRejection.RESOURCE_LIMIT
                if error.classification is RayJobRequestStorageRejection.RESOURCE_LIMIT
                else RayJobRequestStorageRejection.INVALID_LOCATOR
            )
            _reject(classification, load=True)
        decoded = decode_ray_job_request_locator(encoded_locator)
        if decoded != locator:
            _reject(RayJobRequestStorageRejection.INVALID_LOCATOR, load=True)
    else:
        decoded = decode_ray_job_request_locator(locator)
    storage = _storage_for_locator(decoded)
    try:
        serialized_request = storage.load(reference=decoded.reference)
    except ResultStorageIntegrityError:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH, load=True)
    except (ResultStorageError, OSError, UnicodeError):
        _reject(RayJobRequestStorageRejection.STORAGE_UNAVAILABLE, load=True)
    if serialized_request is None:
        _reject(RayJobRequestStorageRejection.STORAGE_UNAVAILABLE, load=True)
    try:
        payload = serialized_request.encode("utf-8")
    except (AttributeError, UnicodeError):
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH, load=True)
    if len(payload) != decoded.size_bytes or hashlib.sha256(payload).hexdigest() != decoded.digest:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH, load=True)
    try:
        request = decode_execution_request(
            serialized_request,
            expected_identity=expected_identity,
            expected_execution_protocol_version=expected_execution_protocol_version,
        )
    except ExecutionRequestDecodeError as error:
        classification = (
            RayJobRequestStorageRejection.RESOURCE_LIMIT
            if error.classification is ExecutionRequestRejection.RESOURCE_LIMIT
            else RayJobRequestStorageRejection.INVALID_REQUEST
        )
        _reject(classification, load=True)
    return LoadedRayJobRequest(
        serialized_request=serialized_request,
        request=request,
        locator=decoded,
        reference=decoded.reference,
        digest=decoded.digest,
        size_bytes=decoded.size_bytes,
    )


def _restore_purged_request(prepared: PreparedRayJobRequest) -> None:
    storage = _storage_for_locator(prepared.locator)
    try:
        restored_reference = storage.store_payload(serialized_payload=prepared.serialized_request)
    except ResultStorageIntegrityError:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)
    except (ResultStorageError, OSError, UnicodeError):
        _reject(RayJobRequestStorageRejection.STORAGE_UNAVAILABLE)
    if restored_reference != prepared.reference:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)


def _validate_prepared_request(
    prepared: object,
) -> PreparedRayJobRequest:
    if not isinstance(prepared, PreparedRayJobRequest):
        _reject(RayJobRequestStorageRejection.INVALID_REQUEST)
    if (
        type(prepared.locator_json) is not str
        or len(prepared.locator_json) > RAY_JOB_REQUEST_LOCATOR_MAX_BYTES
        or type(prepared.encoded_locator) is not str
        or len(prepared.encoded_locator) > RAY_JOB_REQUEST_LOCATOR_MAX_CHARS
    ):
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)
    try:
        request = decode_execution_request(prepared.serialized_request)
        payload = prepared.serialized_request.encode("utf-8")
    except ExecutionRequestDecodeError as error:
        classification = (
            RayJobRequestStorageRejection.RESOURCE_LIMIT
            if error.classification is ExecutionRequestRejection.RESOURCE_LIMIT
            else RayJobRequestStorageRejection.INVALID_REQUEST
        )
        _reject(classification)
    except (AttributeError, UnicodeError):
        _reject(RayJobRequestStorageRejection.INVALID_REQUEST)
    try:
        locator_json = serialize_ray_job_request_locator(prepared.locator)
        encoded_locator = encode_ray_job_request_locator(prepared.locator)
        decoded_locator = decode_ray_job_request_locator(encoded_locator)
    except RayJobRequestStorageError:
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)
    if (
        request != prepared.request
        or prepared.envelope_version != EXECUTION_REQUEST_SCHEMA_VERSION
        or prepared.reference != decoded_locator.reference
        or prepared.backend != decoded_locator.backend
        or prepared.digest != decoded_locator.digest
        or prepared.size_bytes != decoded_locator.size_bytes
        or prepared.digest != hashlib.sha256(payload).hexdigest()
        or prepared.size_bytes != len(payload)
        or prepared.locator != decoded_locator
        or prepared.locator_json != locator_json
        or prepared.encoded_locator != encoded_locator
    ):
        _reject(RayJobRequestStorageRejection.INTEGRITY_MISMATCH)
    return prepared


def register_and_attach_ray_job_request(
    prepared: PreparedRayJobRequest,
    *,
    task_execution: RayTaskExecution,
    submission_handle: SubmissionHandle,
    using: str | None = None,
) -> str:
    """Register then attach one request under the purge-safe global lock order."""
    prepared = _validate_prepared_request(prepared)

    from django.db import transaction
    from django.utils import timezone

    from django_ray.models import (
        InputPayloadKind,
        InputPayloadState,
        RayTaskExecution,
        TaskInputPayload,
        TaskState,
    )
    from django_ray.ray_job_protocol import (
        STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
        coordination_sha256,
        is_valid_rq2_ray_job_submission_id,
    )

    row_database = getattr(getattr(task_execution, "_state", None), "db", None)
    database = using or row_database or "default"
    if using is not None and row_database is not None and using != row_database:
        _reject(RayJobRequestStorageRejection.BINDING_MISMATCH)

    identity = prepared.request.identity
    expected_worker = getattr(task_execution, "claimed_by_worker", None)
    expected_job_id = getattr(submission_handle, "ray_job_id", None)
    expected_address = getattr(submission_handle, "ray_address", None)
    bound_job_id = (
        f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{coordination_sha256(identity)}"
    )
    if (
        getattr(task_execution, "pk", None) != identity.task_execution_pk
        or getattr(task_execution, "task_id", None) != identity.task_id
        or getattr(task_execution, "attempt_number", None) != identity.attempt_number
        or getattr(task_execution, "execution_generation", None) != identity.execution_generation
        or getattr(task_execution, "execution_protocol_version", None)
        != prepared.request.execution_protocol_version
        or type(expected_worker) is not str
        or not expected_worker
        or type(expected_job_id) is not str
        or not is_valid_rq2_ray_job_submission_id(expected_job_id)
        or expected_job_id != bound_job_id
        or type(expected_address) is not str
        or not expected_address
    ):
        _reject(RayJobRequestStorageRejection.BINDING_MISMATCH)

    now = timezone.now()
    with transaction.atomic(using=database):
        payload, created = (
            TaskInputPayload.objects.using(database)
            .select_for_update()
            .get_or_create(
                reference=prepared.reference,
                defaults={
                    "payload_kind": InputPayloadKind.RAY_JOB_REQUEST,
                    "backend": prepared.backend,
                    "digest": prepared.digest,
                    "size_bytes": prepared.size_bytes,
                    "envelope_version": prepared.envelope_version,
                    "state": InputPayloadState.ACTIVE,
                    "last_used_at": now,
                },
            )
        )
        if not created:
            persisted_metadata = (
                payload.payload_kind,
                payload.backend,
                payload.digest,
                payload.size_bytes,
                payload.envelope_version,
            )
            expected_metadata = (
                InputPayloadKind.RAY_JOB_REQUEST,
                prepared.backend,
                prepared.digest,
                prepared.size_bytes,
                prepared.envelope_version,
            )
            if persisted_metadata != expected_metadata:
                _reject(RayJobRequestStorageRejection.REGISTRY_MISMATCH)
            update_fields = ["last_used_at"]
            payload.last_used_at = now
            if payload.state == InputPayloadState.PURGED:
                _restore_purged_request(prepared)
                payload.state = InputPayloadState.ACTIVE
                payload.purged_at = None
                payload.cleanup_error = ""
                update_fields.extend(["state", "purged_at", "cleanup_error"])
            payload.save(update_fields=update_fields, using=database)

        current = (
            RayTaskExecution.objects.using(database)
            .select_for_update()
            .only(
                "pk",
                "state",
                "task_id",
                "attempt_number",
                "execution_generation",
                "execution_protocol_version",
                "claimed_by_worker",
                "ray_job_id",
                "ray_address",
                "callable_path",
                "args_json",
                "kwargs_json",
                "input_reference",
                "runtime_env_profile",
                "runtime_env_hash",
                "ray_job_request_reference",
            )
            .filter(pk=identity.task_execution_pk)
            .first()
        )
        if current is None:
            _reject(RayJobRequestStorageRejection.BINDING_MISMATCH)
        exact_binding = (
            current.state == TaskState.RUNNING
            and current.task_id == identity.task_id
            and current.attempt_number == identity.attempt_number
            and current.execution_generation == identity.execution_generation
            and current.execution_protocol_version == prepared.request.execution_protocol_version
            and current.claimed_by_worker == expected_worker
            and current.ray_job_id == expected_job_id
            and current.ray_address == expected_address
            and current.callable_path == prepared.request.callable_path
            and current.args_json == prepared.request.serialized_args
            and current.kwargs_json == prepared.request.serialized_kwargs
            and current.input_reference == prepared.request.input_reference
            and current.runtime_env_profile == prepared.request.runtime_env_profile
            and current.runtime_env_hash == prepared.request.runtime_env_hash
            and current.ray_job_request_reference in {None, prepared.reference}
        )
        if not exact_binding:
            _reject(RayJobRequestStorageRejection.BINDING_MISMATCH)
        if current.ray_job_request_reference is None:
            current.ray_job_request_reference = prepared.reference
            current.save(update_fields=["ray_job_request_reference"], using=database)

    task_execution.ray_job_request_reference = prepared.reference
    return prepared.reference


def release_ray_job_request_reservation(
    task_execution: RayTaskExecution,
    submission_handle: SubmissionHandle,
    *,
    expected_reference: str | None,
    using: str | None = None,
) -> bool:
    """Clear one definitely unsubmitted rq2 tuple under the registry lock order."""
    from django.db import transaction

    from django_ray.models import (
        InputPayloadKind,
        RayTaskExecution,
        TaskInputPayload,
        TaskState,
    )
    from django_ray.ray_job_protocol import is_valid_rq2_ray_job_submission_id

    row_database = getattr(getattr(task_execution, "_state", None), "db", None)
    database = using or row_database or "default"
    expected_pk = getattr(task_execution, "pk", None)
    expected_task_id = getattr(task_execution, "task_id", None)
    expected_attempt = getattr(task_execution, "attempt_number", None)
    expected_generation = getattr(task_execution, "execution_generation", None)
    expected_protocol = getattr(task_execution, "execution_protocol_version", None)
    expected_worker = getattr(task_execution, "claimed_by_worker", None)
    expected_job_id = getattr(submission_handle, "ray_job_id", None)
    expected_address = getattr(submission_handle, "ray_address", None)
    if (
        (using is not None and row_database is not None and using != row_database)
        or type(expected_pk) is not int
        or not 0 < expected_pk <= _MAX_COUNTER
        or type(expected_task_id) is not str
        or not expected_task_id
        or len(expected_task_id) > 255
        or type(expected_attempt) is not int
        or not 0 < expected_attempt <= _MAX_COUNTER
        or type(expected_generation) is not int
        or not 0 <= expected_generation <= _MAX_COUNTER
        or type(expected_protocol) is not int
        or not 0 < expected_protocol <= _MAX_COUNTER
        or type(expected_worker) is not str
        or not expected_worker
        or type(expected_job_id) is not str
        or not is_valid_rq2_ray_job_submission_id(expected_job_id)
        or type(expected_address) is not str
        or not expected_address
        or getattr(task_execution, "ray_job_id", None) != expected_job_id
        or getattr(task_execution, "ray_address", None) != expected_address
        or getattr(task_execution, "ray_job_request_reference", None) != expected_reference
    ):
        return False
    if expected_reference is not None:
        try:
            metadata = _parse_result_reference(expected_reference)
        except ResultStorageError:
            return False
        if metadata.scheme not in {"resultfs", "s3", "gs"}:
            return False

    with transaction.atomic(using=database):
        if expected_reference is not None:
            payload = (
                TaskInputPayload.objects.using(database)
                .select_for_update()
                .only("reference", "payload_kind")
                .filter(reference=expected_reference)
                .first()
            )
            if payload is None or payload.payload_kind != InputPayloadKind.RAY_JOB_REQUEST:
                return False

        current = (
            RayTaskExecution.objects.using(database)
            .select_for_update()
            .only(
                "pk",
                "state",
                "task_id",
                "attempt_number",
                "execution_generation",
                "execution_protocol_version",
                "claimed_by_worker",
                "ray_job_id",
                "ray_address",
                "ray_job_request_reference",
            )
            .filter(pk=expected_pk)
            .first()
        )
        if current is None or not (
            current.state == TaskState.RUNNING
            and current.task_id == expected_task_id
            and current.attempt_number == expected_attempt
            and current.execution_generation == expected_generation
            and current.execution_protocol_version == expected_protocol
            and current.claimed_by_worker == expected_worker
            and current.ray_job_id == expected_job_id
            and current.ray_address == expected_address
            and current.ray_job_request_reference == expected_reference
        ):
            return False
        current.ray_job_id = None
        current.ray_address = None
        current.ray_job_request_reference = None
        current.save(
            update_fields=[
                "ray_job_id",
                "ray_address",
                "ray_job_request_reference",
            ],
            using=database,
        )

    task_execution.ray_job_id = None
    task_execution.ray_address = None
    task_execution.ray_job_request_reference = None
    return True


__all__ = [
    "LoadedRayJobRequest",
    "PreparedRayJobRequest",
    "RAY_JOB_REQUEST_LOCATOR_MAX_BYTES",
    "RAY_JOB_REQUEST_LOCATOR_MAX_CHARS",
    "RAY_JOB_REQUEST_LOCATOR_SCHEMA",
    "RAY_JOB_REQUEST_LOCATOR_VERSION",
    "RAY_JOB_REQUEST_REFERENCE_MAX_BYTES",
    "RayJobRequestLoadError",
    "RayJobRequestLocator",
    "RayJobRequestStorageError",
    "RayJobRequestStorageRejection",
    "decode_ray_job_request_locator",
    "encode_ray_job_request_locator",
    "load_ray_job_request",
    "prepare_ray_job_request",
    "ray_job_request_reference_content_identity",
    "register_and_attach_ray_job_request",
    "release_ray_job_request_reservation",
    "serialize_ray_job_request_locator",
    "validate_ray_job_request_storage_config",
]
