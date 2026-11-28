"""Leaf storage protocol constants, error types, and registry-alias helpers.

Shared across ``input_storage``, ``payload_registry``, ``ray_job_protocol``,
and ``ray_job_request_storage`` so those modules form an acyclic dependency
graph while remaining safe to import prior to Django setup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Never, cast
from urllib.parse import quote

from django_ray.execution_codec import EXECUTION_REQUEST_MAX_BYTES
from django_ray.result_storage import (
    ResultStorageError,
    _configured_prefix,
    _parse_result_reference,
    _validate_object_reference,
)

RAY_JOB_REQUEST_LOCATOR_SCHEMA = "django-ray.ray-job-request-locator"
RAY_JOB_REQUEST_LOCATOR_VERSION = 1
RAY_JOB_REQUEST_LOCATOR_MAX_BYTES = 4096
RAY_JOB_REQUEST_LOCATOR_MAX_CHARS = (RAY_JOB_REQUEST_LOCATOR_MAX_BYTES * 4 + 2) // 3
RAY_JOB_REQUEST_REFERENCE_MAX_BYTES = 500
RAY_JOB_REQUEST_REFERENCE_MAX_CHARS = RAY_JOB_REQUEST_REFERENCE_MAX_BYTES
RAY_JOB_REQUEST_REFERENCE_PREFIX = "rq2ref:v1:"


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


def _reject_request_storage(
    classification: RayJobRequestStorageRejection,
    *,
    load: bool = False,
) -> Never:
    error_type = RayJobRequestLoadError if load else RayJobRequestStorageError
    raise error_type(classification) from None


def _native_request_reference(reference: object, *, load: bool = False) -> str:
    """Validate one wrapped or historical native rq2 reference without I/O.

    The discriminator belongs only to request storage. Generic input/result
    parsers must continue rejecting it before selecting a storage backend.
    """
    if (
        type(reference) is not str
        or not reference
        or len(reference) > RAY_JOB_REQUEST_REFERENCE_MAX_CHARS
        or not reference.isascii()
    ):
        _reject_request_storage(RayJobRequestStorageRejection.INVALID_LOCATOR, load=load)
    native = reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    try:
        metadata = _parse_result_reference(native)
    except ResultStorageError:
        _reject_request_storage(RayJobRequestStorageRejection.INVALID_LOCATOR, load=load)
    if (
        metadata.scheme not in {"resultfs", "s3", "gs"}
        or not 0 < metadata.size_bytes <= EXECUTION_REQUEST_MAX_BYTES
    ):
        _reject_request_storage(RayJobRequestStorageRejection.INVALID_LOCATOR, load=load)
    return native


def _wrap_request_reference(reference: object) -> str:
    """Create the sole current discriminator without allocating oversized text."""
    if (
        type(reference) is not str
        or len(reference)
        > RAY_JOB_REQUEST_REFERENCE_MAX_CHARS - len(RAY_JOB_REQUEST_REFERENCE_PREFIX)
        or reference.startswith(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    ):
        _reject_request_storage(RayJobRequestStorageRejection.INVALID_LOCATOR)
    return RAY_JOB_REQUEST_REFERENCE_PREFIX + _native_request_reference(reference)


def _request_reference_alias(reference: object) -> str | None:
    """Return the other possible registry spelling, solely for refusal checks."""
    native = _native_request_reference(reference)
    if native != reference:
        return native
    if len(native) + len(RAY_JOB_REQUEST_REFERENCE_PREFIX) <= RAY_JOB_REQUEST_REFERENCE_MAX_CHARS:
        return RAY_JOB_REQUEST_REFERENCE_PREFIX + native
    return None


@dataclass(frozen=True)
class _PayloadRegistryReferences:
    canonical: str
    aliases: tuple[str, ...]


def _payload_registry_references(
    reference: str,
    config: dict[str, Any] | None = None,
    *,
    _get_settings: Callable[[], dict[str, Any]] | None = None,
) -> _PayloadRegistryReferences:
    """Enumerate at most four spellings in the exact authorized namespace.

    A key has only its raw historical spelling and its exact quoted spelling.
    Each may also have a request discriminator if the request grammar permits
    it. This does not authorize deletion or broaden any payload reader.
    """
    wrapped = reference.startswith(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    try:
        native = _native_request_reference(reference) if wrapped else reference
    except RayJobRequestStorageError:
        raise ResultStorageError("Payload registry reference is invalid") from None
    metadata = _parse_result_reference(native, allow_encoding_legacy=True)
    if metadata.scheme == "resultfs":
        canonical = native
        candidates = {native}
    elif metadata.scheme in {"s3", "gs"}:
        if config is not None:
            resolved_config = config
        elif _get_settings is not None:
            resolved_config = _get_settings()
        else:
            from django_ray.conf.settings import get_settings

            resolved_config = get_settings()
        backend = "S3" if metadata.scheme == "s3" else "GCS"
        bucket = resolved_config.get(f"INPUT_STORAGE_{backend}_BUCKET")
        if not isinstance(bucket, str):
            raise ResultStorageError("Payload registry namespace is unavailable")
        prefix = _configured_prefix(
            resolved_config, f"INPUT_STORAGE_{backend}_PREFIX", "django-ray/inputs"
        )
        bucket, key = _validate_object_reference(
            metadata, scheme=metadata.scheme, bucket=bucket, prefix=prefix
        )
        canonical = (
            f"{metadata.scheme}://{bucket}/{quote(key, safe='/-._~')}?bytes={metadata.size_bytes}"
        )
        raw = f"{metadata.scheme}://{bucket}/{key}?bytes={metadata.size_bytes}"
        candidates = {canonical, raw}
    else:
        raise ResultStorageError("Payload registry reference is invalid")

    aliases: set[str] = set()
    for candidate in candidates:
        try:
            _parse_result_reference(candidate, allow_encoding_legacy=True)
        except ResultStorageError:
            # A raw '?'/'#' spelling or an oversized canonical encoding is
            # not a supported registry identity, even for a valid object key.
            continue
        aliases.add(candidate)
        try:
            aliases.add(_wrap_request_reference(candidate))
        except RayJobRequestStorageError:
            pass
    if wrapped:
        canonical = RAY_JOB_REQUEST_REFERENCE_PREFIX + canonical
    return _PayloadRegistryReferences(canonical, tuple(sorted(aliases)))


def _has_other_payload_registry_reference(
    reference: str,
    references: _PayloadRegistryReferences,
    *,
    using: str | None = None,
    lock: bool = False,
) -> bool:
    """Refuse one existing alias without taking a second registry row lock."""
    from django_ray.models import TaskInputPayload

    aliases = tuple(alias for alias in references.aliases if alias != reference)
    if not aliases:
        return False
    payload_model: Any = cast(Any, TaskInputPayload)
    query = payload_model.objects.using(using).filter(reference__in=aliases)
    if not lock:
        return query.exists()
    # Writers call this before locking their own row. One existing alias is
    # enough to refuse, including a tombstone; never acquire another row lock.
    return (
        query.select_for_update().order_by("reference").values_list("reference", flat=True).first()
        is not None
    )


__all__ = [
    "RAY_JOB_REQUEST_LOCATOR_MAX_BYTES",
    "RAY_JOB_REQUEST_LOCATOR_MAX_CHARS",
    "RAY_JOB_REQUEST_LOCATOR_SCHEMA",
    "RAY_JOB_REQUEST_LOCATOR_VERSION",
    "RAY_JOB_REQUEST_REFERENCE_MAX_BYTES",
    "RAY_JOB_REQUEST_REFERENCE_MAX_CHARS",
    "RAY_JOB_REQUEST_REFERENCE_PREFIX",
    "RayJobRequestLoadError",
    "RayJobRequestStorageError",
    "RayJobRequestStorageRejection",
    "_PayloadRegistryReferences",
    "_has_other_payload_registry_reference",
    "_native_request_reference",
    "_payload_registry_references",
    "_reject_request_storage",
    "_request_reference_alias",
    "_wrap_request_reference",
]
