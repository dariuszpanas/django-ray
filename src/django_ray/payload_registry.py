"""Bounded physical-identity refusal checks for input/request registries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from django_ray.conf.settings import get_settings
from django_ray.ray_job_request_storage import (
    RAY_JOB_REQUEST_REFERENCE_PREFIX,
    RayJobRequestStorageError,
    _native_request_reference,
    _wrap_request_reference,
)
from django_ray.result_storage import (
    ResultStorageError,
    _configured_prefix,
    _parse_result_reference,
    _validate_object_reference,
)


@dataclass(frozen=True)
class _PayloadRegistryReferences:
    canonical: str
    aliases: tuple[str, ...]


def _payload_registry_references(
    reference: str, config: dict[str, Any] | None = None
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
        resolved_config = config if config is not None else get_settings()
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
    query = TaskInputPayload.objects.using(using).filter(reference__in=aliases)
    if not lock:
        return query.exists()
    # Writers call this before locking their own row. One existing alias is
    # enough to refuse, including a tombstone; never acquire another row lock.
    return (
        query.select_for_update().order_by("reference").values_list("reference", flat=True).first()
        is not None
    )
