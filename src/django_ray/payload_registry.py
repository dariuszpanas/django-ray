"""Bounded physical-identity refusal, inventory, and orphan-reap helpers for input/request registries."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

from django_ray._storage_types import (
    RAY_JOB_REQUEST_REFERENCE_PREFIX,
    RayJobRequestStorageError,
    _native_request_reference,
    _PayloadRegistryReferences,
    _wrap_request_reference,
)
from django_ray._storage_types import (
    _has_other_payload_registry_reference as _has_other_payload_registry_reference,
)
from django_ray._storage_types import (
    _payload_registry_references as _storage_payload_registry_references,
)
from django_ray.conf.settings import get_settings
from django_ray.execution_codec import (
    EXECUTION_REQUEST_SCHEMA_VERSION,
    ExecutionRequestDecodeError,
    decode_execution_request,
)
from django_ray.result_storage import (
    _MAX_REFERENCE_BYTE_COUNT,
    FilesystemResultStorage,
    GCSResultStorage,
    ResultStorageError,
    S3ResultStorage,
    _object_key_digest,
    _object_reference,
)

DEFAULT_UNREGISTERED_GRACE_SECONDS = 3600
MIN_UNREGISTERED_GRACE_SECONDS = 60
MAX_UNREGISTERED_GRACE_SECONDS = 3650 * 86400
DEFAULT_INVENTORY_MAX_OBJECTS = 100
MAX_INVENTORY_MAX_OBJECTS = 1000
DEFAULT_INVENTORY_MAX_PAGES = 10
MAX_INVENTORY_MAX_PAGES = 100
INVENTORY_DETAIL_MAX_BYTES = 4096


@dataclass(frozen=True)
class _RawDiscoveredObject:
    fingerprint_sha256: str
    malformed: bool
    digest: str | None = None
    size_bytes: int | None = None
    native_reference: str | None = None
    modified_at: datetime | None = None
    storage_key: str | None = None


@dataclass(frozen=True)
class PayloadBackendInventoryReport:
    """Bounded, redaction-safe inventory and orphan-reap summary."""

    backend: str
    cutoff: str
    grace_seconds: int
    delete_unregistered: bool
    has_more: bool
    counts: dict[str, int]
    details: list[dict[str, str]]
    details_omitted: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "cutoff": self.cutoff,
            "grace_seconds": self.grace_seconds,
            "delete_unregistered": self.delete_unregistered,
            "has_more": self.has_more,
            "counts": dict(self.counts),
            "details": [dict(item) for item in self.details],
            "details_omitted": self.details_omitted,
        }


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


def _normalize_timestamp(value: Any) -> datetime | None:
    if type(value) is not datetime:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _payload_registry_references(
    reference: str, config: dict[str, Any] | None = None
) -> _PayloadRegistryReferences:
    return _storage_payload_registry_references(reference, config, _get_settings=get_settings)


def _is_temporary_filesystem_entry(name: str, *, is_dir: bool) -> bool:
    if is_dir:
        return name.startswith(".") and name.endswith(".install-lock")
    return (
        (name.startswith(".django-ray-result-") and name.endswith(".tmp"))
        or (name.startswith(".") and name.endswith(".install.lock"))
        or name == ".django-ray-install.lock"
    )


def _discover_filesystem_objects(
    storage: FilesystemResultStorage,
    *,
    max_objects: int,
) -> tuple[list[_RawDiscoveredObject], bool]:
    root = storage.root_path
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return [], False
    except OSError as error:
        raise ResultStorageError("Failed to inspect filesystem payload inventory") from error
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ResultStorageError("Filesystem payload inventory root is not a directory")

    items: list[_RawDiscoveredObject] = []
    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current_root)
        rel_dir = current_path.relative_to(root)
        rel_dir_parts = rel_dir.parts

        retained_dirs: list[str] = []
        for dirname in dirnames:
            if _is_temporary_filesystem_entry(dirname, is_dir=True):
                continue
            candidate_dir = current_path / dirname
            rel_posix = (rel_dir / dirname).as_posix()
            try:
                dir_stat = candidate_dir.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ResultStorageError(
                    "Failed to inspect filesystem payload inventory"
                ) from error
            valid_dir = (
                stat.S_ISDIR(dir_stat.st_mode)
                and len(rel_dir_parts) < 2
                and len(dirname) == 2
                and all(ch in "0123456789abcdef" for ch in dirname)
            )
            if not valid_dir:
                items.append(
                    _RawDiscoveredObject(
                        fingerprint_sha256=_fingerprint(rel_posix),
                        malformed=True,
                    )
                )
                if len(items) > max_objects:
                    return items[:max_objects], True
                continue
            retained_dirs.append(dirname)
        dirnames[:] = retained_dirs

        for filename in filenames:
            if _is_temporary_filesystem_entry(filename, is_dir=False):
                continue
            file_path = current_path / filename
            rel_posix = (rel_dir / filename).as_posix()
            try:
                file_stat = file_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ResultStorageError(
                    "Failed to inspect filesystem payload inventory"
                ) from error
            digest = _object_key_digest(rel_posix) if len(rel_dir_parts) == 2 else None
            size_bytes = file_stat.st_size
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or digest is None
                or type(size_bytes) is not int
                or not 0 <= size_bytes <= _MAX_REFERENCE_BYTE_COUNT
            ):
                items.append(
                    _RawDiscoveredObject(
                        fingerprint_sha256=_fingerprint(rel_posix),
                        malformed=True,
                    )
                )
            else:
                native_reference = f"resultfs://sha256/{digest}?rel={rel_posix}&bytes={size_bytes}"
                modified_at = datetime.fromtimestamp(
                    file_stat.st_mtime_ns / 1_000_000_000,
                    tz=UTC,
                )
                items.append(
                    _RawDiscoveredObject(
                        fingerprint_sha256=_fingerprint(native_reference),
                        malformed=False,
                        digest=digest,
                        size_bytes=size_bytes,
                        native_reference=native_reference,
                        modified_at=modified_at,
                        storage_key=rel_posix,
                    )
                )
            if len(items) > max_objects:
                return items[:max_objects], True

    return items, False


def _validate_prefixed_object_key(key: str, prefix: str) -> str | None:
    expected_prefix = f"{prefix}/" if prefix else ""
    if not key.startswith(expected_prefix):
        raise ResultStorageError("Object inventory returned a key outside the authorized prefix")
    suffix = key.removeprefix(expected_prefix)
    if suffix.count("/") != 2:
        return None
    return _object_key_digest(key)


def _discover_s3_objects(
    storage: S3ResultStorage,
    *,
    max_objects: int,
    max_pages: int,
) -> tuple[list[_RawDiscoveredObject], bool]:
    prefix_filter = f"{storage.prefix}/" if storage.prefix else ""
    items: list[_RawDiscoveredObject] = []
    continuation_token: str | None = None
    pages_read = 0

    while pages_read < max_pages:
        pages_read += 1
        remaining = max_objects + 1 - len(items)
        request_kwargs: dict[str, Any] = {
            "Bucket": storage.bucket,
            "Prefix": prefix_filter,
            "MaxKeys": min(max(remaining, 1), 1000),
        }
        if continuation_token is not None:
            request_kwargs["ContinuationToken"] = continuation_token
        try:
            response = storage.client.list_objects_v2(**request_kwargs)
        except Exception as error:
            raise ResultStorageError("Failed to list S3 payload inventory") from error
        if not isinstance(response, dict):
            raise ResultStorageError("S3 payload inventory returned an invalid response")
        contents = response.get("Contents", [])
        if contents is None:
            contents = []
        if not isinstance(contents, list):
            raise ResultStorageError("S3 payload inventory Contents must be a list")

        for entry in contents:
            if not isinstance(entry, dict):
                raise ResultStorageError("S3 payload inventory entry must be an object")
            key = entry.get("Key")
            if not isinstance(key, str) or not key:
                raise ResultStorageError("S3 payload inventory entry is missing a valid Key")
            digest = _validate_prefixed_object_key(key, storage.prefix)
            size_bytes = entry.get("Size")
            modified_at = _normalize_timestamp(entry.get("LastModified"))
            if (
                digest is None
                or type(size_bytes) is not int
                or not 0 <= size_bytes <= _MAX_REFERENCE_BYTE_COUNT
                or modified_at is None
            ):
                items.append(
                    _RawDiscoveredObject(
                        fingerprint_sha256=_fingerprint(key),
                        malformed=True,
                    )
                )
            else:
                try:
                    native_reference = _object_reference(
                        scheme="s3",
                        bucket=storage.bucket,
                        key=key,
                        size_bytes=size_bytes,
                    )
                except ResultStorageError:
                    items.append(
                        _RawDiscoveredObject(
                            fingerprint_sha256=_fingerprint(key),
                            malformed=True,
                        )
                    )
                else:
                    items.append(
                        _RawDiscoveredObject(
                            fingerprint_sha256=_fingerprint(native_reference),
                            malformed=False,
                            digest=digest,
                            size_bytes=size_bytes,
                            native_reference=native_reference,
                            modified_at=modified_at,
                            storage_key=key,
                        )
                    )
            if len(items) > max_objects:
                return items[:max_objects], True

        is_truncated = response.get("IsTruncated", False)
        if type(is_truncated) is not bool:
            raise ResultStorageError("S3 payload inventory IsTruncated flag is invalid")
        if not is_truncated:
            return items, False
        next_token = response.get("NextContinuationToken")
        if not isinstance(next_token, str) or not next_token:
            raise ResultStorageError("S3 payload inventory omitted NextContinuationToken")
        continuation_token = next_token

    return items[:max_objects], True


def _discover_gcs_objects(
    storage: GCSResultStorage,
    *,
    max_objects: int,
    max_pages: int,
) -> tuple[list[_RawDiscoveredObject], bool]:
    prefix_filter = f"{storage.prefix}/" if storage.prefix else ""
    try:
        iterator = storage.client.list_blobs(
            storage.bucket,
            prefix=prefix_filter,
            max_results=max_objects + 1,
            page_size=min(max_objects + 1, 1000),
        )
    except TypeError:
        try:
            iterator = storage.client.list_blobs(storage.bucket, prefix=prefix_filter)
        except Exception as error:
            raise ResultStorageError("Failed to list GCS payload inventory") from error
    except Exception as error:
        raise ResultStorageError("Failed to list GCS payload inventory") from error

    items: list[_RawDiscoveredObject] = []
    pages = getattr(iterator, "pages", None)
    if pages is not None:
        pages_read = 0
        pages_iter = iter(pages)
        has_token_attr = hasattr(iterator, "next_page_token")
        try:
            while pages_read < max_pages:
                try:
                    page = next(pages_iter)
                except StopIteration:
                    return items[:max_objects], False
                pages_read += 1
                for blob in page:
                    items.append(_classify_gcs_blob(storage, blob))
                    if len(items) > max_objects:
                        return items[:max_objects], True
        except ResultStorageError:
            raise
        except Exception as error:
            raise ResultStorageError("Failed to iterate GCS payload inventory pages") from error
        if has_token_attr:
            next_token = getattr(iterator, "next_page_token", None)
            return items[:max_objects], bool(next_token)
        # Synthetic page iterables without next_page_token: peek conservatively
        # only when the iterator does not expose page-token state.
        sentinel = object()
        try:
            has_more_pages = next(pages_iter, sentinel) is not sentinel
        except Exception as error:
            raise ResultStorageError("Failed to iterate GCS payload inventory pages") from error
        return items[:max_objects], has_more_pages

    try:
        for blob in iterator:
            items.append(_classify_gcs_blob(storage, blob))
            if len(items) > max_objects:
                return items[:max_objects], True
    except ResultStorageError:
        raise
    except Exception as error:
        raise ResultStorageError("Failed to iterate GCS payload inventory") from error
    return items, False


def _classify_gcs_blob(storage: GCSResultStorage, blob: Any) -> _RawDiscoveredObject:
    key = getattr(blob, "name", None)
    if not isinstance(key, str) or not key:
        raise ResultStorageError("GCS payload inventory blob is missing a valid name")
    digest = _validate_prefixed_object_key(key, storage.prefix)
    size_bytes = getattr(blob, "size", None)
    raw_updated = getattr(blob, "updated", None)
    if raw_updated is None:
        raw_updated = getattr(blob, "time_created", None)
    modified_at = _normalize_timestamp(raw_updated)
    if (
        digest is None
        or type(size_bytes) is not int
        or not 0 <= size_bytes <= _MAX_REFERENCE_BYTE_COUNT
        or modified_at is None
    ):
        return _RawDiscoveredObject(
            fingerprint_sha256=_fingerprint(key),
            malformed=True,
        )
    try:
        native_reference = _object_reference(
            scheme="gs",
            bucket=storage.bucket,
            key=key,
            size_bytes=size_bytes,
        )
    except ResultStorageError:
        return _RawDiscoveredObject(
            fingerprint_sha256=_fingerprint(key),
            malformed=True,
        )
    return _RawDiscoveredObject(
        fingerprint_sha256=_fingerprint(native_reference),
        malformed=False,
        digest=digest,
        size_bytes=size_bytes,
        native_reference=native_reference,
        modified_at=modified_at,
        storage_key=key,
    )


def _current_object_modified_at(
    storage: Any,
    item: _RawDiscoveredObject,
) -> datetime | None:
    if item.storage_key is None:
        return None
    if isinstance(storage, FilesystemResultStorage):
        path = storage.root_path.joinpath(*item.storage_key.split("/"))
        try:
            st = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ResultStorageError("Failed to revalidate filesystem payload age") from error
        if not stat.S_ISREG(st.st_mode):
            return None
        return datetime.fromtimestamp(st.st_mtime_ns / 1_000_000_000, tz=UTC)
    if isinstance(storage, S3ResultStorage):
        head_fn = getattr(storage.client, "head_object", None)
        if callable(head_fn):
            try:
                response = head_fn(Bucket=storage.bucket, Key=item.storage_key)
            except Exception as error:
                raise ResultStorageError("Failed to revalidate S3 payload age") from error
            if isinstance(response, dict) and "LastModified" in response:
                return _normalize_timestamp(response.get("LastModified"))
        return item.modified_at
    if isinstance(storage, GCSResultStorage):
        try:
            blob = storage.client.bucket(storage.bucket).blob(item.storage_key)
            blob.reload()
        except Exception as error:
            raise ResultStorageError("Failed to revalidate GCS payload age") from error
        raw_updated = getattr(blob, "updated", None)
        if raw_updated is None:
            raw_updated = getattr(blob, "time_created", None)
        return _normalize_timestamp(raw_updated)
    return item.modified_at


def _detect_payload_envelope_kind(
    serialized_payload: str,
    *,
    references: _PayloadRegistryReferences,
) -> tuple[str, int, str] | None:
    from django_ray.input_storage import (
        INPUT_ENVELOPE_VERSION,
        InputPayloadValidationError,
        _deserialize_envelope,
    )
    from django_ray.models import InputPayloadKind

    try:
        _deserialize_envelope(serialized_payload)
        return (
            InputPayloadKind.TASK_INPUT,
            INPUT_ENVELOPE_VERSION,
            references.canonical,
        )
    except InputPayloadValidationError:
        pass
    try:
        decode_execution_request(serialized_payload)
        wrapped_reference = _wrap_request_reference(references.canonical)
        return (
            InputPayloadKind.RAY_JOB_REQUEST,
            EXECUTION_REQUEST_SCHEMA_VERSION,
            wrapped_reference,
        )
    except (ExecutionRequestDecodeError, RayJobRequestStorageError):
        return None


def _reap_unregistered_object(
    storage: Any,
    *,
    backend_name: str,
    item: _RawDiscoveredObject,
    references: _PayloadRegistryReferences,
    cutoff: datetime,
    config: dict[str, Any] | None = None,
    use_default_delete: bool = True,
    using: str | None = None,
) -> str:
    """Revalidate ownership, age, and envelope, then tombstone + delete."""
    from django_ray.input_storage import delete_input_reference
    from django_ray.models import InputPayloadState, RayTaskExecution, TaskInputPayload

    assert item.native_reference is not None
    assert item.digest is not None
    assert item.size_bytes is not None

    try:
        revalidated_modified_at = _current_object_modified_at(storage, item)
        if revalidated_modified_at is None:
            return "skipped"
        if revalidated_modified_at > cutoff:
            return "unregistered_recent"
        serialized_payload = storage.load(reference=item.native_reference)
    except Exception:
        return "failed"
    if serialized_payload is None:
        return "skipped"

    detected = _detect_payload_envelope_kind(serialized_payload, references=references)
    if detected is None:
        return "ambiguous"
    payload_kind, envelope_version, canonical_reference = detected

    try:
        with transaction.atomic(using=using):
            existing_rows = list(
                TaskInputPayload.objects.using(using)
                .select_for_update()
                .filter(reference__in=references.aliases)
                .order_by("reference")
                .values_list("reference", flat=True)[:1]
            )
            if existing_rows:
                return "skipped"
            attached_executions = list(
                RayTaskExecution.objects.using(using)
                .select_for_update()
                .filter(
                    Q(input_reference__in=references.aliases)
                    | Q(ray_job_request_reference__in=references.aliases)
                )
                .order_by("pk")
                .values_list("pk", flat=True)[:1]
            )
            if attached_executions:
                return "ambiguous"

            now = timezone.now()
            try:
                with transaction.atomic(using=using):
                    TaskInputPayload.objects.using(using).create(
                        reference=canonical_reference,
                        payload_kind=payload_kind,
                        backend=backend_name,
                        digest=item.digest,
                        size_bytes=item.size_bytes,
                        envelope_version=envelope_version,
                        state=InputPayloadState.PURGED,
                        last_used_at=revalidated_modified_at,
                        purged_at=now,
                        cleanup_error="",
                    )
            except IntegrityError:
                return "skipped"

            final_modified_at = _current_object_modified_at(storage, item)
            if final_modified_at is not None and final_modified_at > cutoff:
                raise ResultStorageError("Payload object changed during unregistered reap")
            if use_default_delete:
                delete_input_reference(item.native_reference, config=config)
            else:
                storage.delete(reference=item.native_reference)
            return "purged"
    except Exception:
        return "failed"


def discover_and_reap_unregistered_payloads(
    *,
    config: dict[str, Any] | None = None,
    grace_seconds: int = DEFAULT_UNREGISTERED_GRACE_SECONDS,
    max_objects: int = DEFAULT_INVENTORY_MAX_OBJECTS,
    max_pages: int = DEFAULT_INVENTORY_MAX_PAGES,
    max_details: int = 20,
    delete_unregistered: bool = False,
    now: datetime | None = None,
    using: str | None = None,
    storage_backend: tuple[str, Any] | None = None,
) -> PayloadBackendInventoryReport:
    """Inventory django-ray input objects and optionally reap verified old orphans."""
    if (
        type(grace_seconds) is not int
        or not MIN_UNREGISTERED_GRACE_SECONDS <= grace_seconds <= MAX_UNREGISTERED_GRACE_SECONDS
    ):
        raise ValueError(
            f"grace_seconds must be between {MIN_UNREGISTERED_GRACE_SECONDS} "
            f"and {MAX_UNREGISTERED_GRACE_SECONDS}"
        )
    if type(max_objects) is not int or not 1 <= max_objects <= MAX_INVENTORY_MAX_OBJECTS:
        raise ValueError(f"max_objects must be between 1 and {MAX_INVENTORY_MAX_OBJECTS}")
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_INVENTORY_MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_INVENTORY_MAX_PAGES}")
    if type(max_details) is not int or not 0 <= max_details <= 100:
        raise ValueError("max_details must be between 0 and 100")

    from django_ray.input_storage import _storage_backend
    from django_ray.models import InputPayloadState, RayTaskExecution, TaskInputPayload

    resolved_config = config if config is not None else get_settings()
    if storage_backend is not None:
        backend_name, storage = storage_backend
    else:
        backend_name, storage = _storage_backend(resolved_config)

    evaluation_now = _normalize_timestamp(now) or timezone.now()
    cutoff = evaluation_now - timedelta(seconds=grace_seconds)

    if isinstance(storage, FilesystemResultStorage):
        raw_items, has_more = _discover_filesystem_objects(storage, max_objects=max_objects)
    elif isinstance(storage, S3ResultStorage):
        raw_items, has_more = _discover_s3_objects(
            storage,
            max_objects=max_objects,
            max_pages=max_pages,
        )
    elif isinstance(storage, GCSResultStorage):
        raw_items, has_more = _discover_gcs_objects(
            storage,
            max_objects=max_objects,
            max_pages=max_pages,
        )
    else:
        raise ResultStorageError(f"Unsupported payload inventory backend: {backend_name!r}")

    counts = dict.fromkeys(
        (
            "scanned",
            "unregistered_eligible",
            "unregistered_recent",
            "registered_active",
            "registered_missing",
            "tombstone",
            "ambiguous",
            "malformed_key",
            "purged",
            "failed",
        ),
        0,
    )
    details: list[dict[str, str]] = []
    detail_bytes = 0
    discovered_native_references: set[str] = set()

    def record_detail(fingerprint: str, classification: str) -> None:
        nonlocal detail_bytes
        if len(details) >= max_details:
            return
        entry = {
            "object_sha256": fingerprint,
            "classification": classification,
        }
        encoded_len = len(json.dumps(entry, separators=(",", ":")).encode("utf-8")) + 2
        if detail_bytes + encoded_len <= INVENTORY_DETAIL_MAX_BYTES:
            details.append(entry)
            detail_bytes += encoded_len

    _inventory_batch_size = 250
    resolved_items: list[tuple[_RawDiscoveredObject, _PayloadRegistryReferences | None]] = []
    all_aliases: list[str] = []
    seen_aliases: set[str] = set()

    for item in raw_items:
        if item.malformed or item.native_reference is None:
            resolved_items.append((item, None))
            continue
        try:
            references = _payload_registry_references(item.native_reference, resolved_config)
        except ResultStorageError:
            resolved_items.append((item, None))
            continue
        resolved_items.append((item, references))
        for alias in references.aliases:
            if alias not in seen_aliases:
                seen_aliases.add(alias)
                all_aliases.append(alias)

    use_batched_lookups = isinstance(TaskInputPayload.objects, models.Manager) and isinstance(
        RayTaskExecution.objects, models.Manager
    )
    payload_rows_by_reference: dict[str, tuple[str, str, str, int]] = {}
    attached_references: set[str] = set()
    if use_batched_lookups:
        for start in range(0, len(all_aliases), _inventory_batch_size):
            chunk = all_aliases[start : start + _inventory_batch_size]
            for row in (
                TaskInputPayload.objects.using(using)
                .filter(reference__in=chunk)
                .values_list("reference", "state", "digest", "size_bytes")
            ):
                payload_rows_by_reference[str(row[0])] = (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    int(row[3]),
                )

        unmatched_aliases: list[str] = []
        seen_unmatched: set[str] = set()
        for _item, references in resolved_items:
            if references is None:
                continue
            if any(alias in payload_rows_by_reference for alias in references.aliases):
                continue
            for alias in references.aliases:
                if alias not in seen_unmatched:
                    seen_unmatched.add(alias)
                    unmatched_aliases.append(alias)

        for start in range(0, len(unmatched_aliases), _inventory_batch_size):
            chunk = unmatched_aliases[start : start + _inventory_batch_size]
            for input_ref, request_ref in (
                RayTaskExecution.objects.using(using)
                .filter(Q(input_reference__in=chunk) | Q(ray_job_request_reference__in=chunk))
                .values_list("input_reference", "ray_job_request_reference")
            ):
                if isinstance(input_ref, str) and input_ref:
                    attached_references.add(input_ref)
                if isinstance(request_ref, str) and request_ref:
                    attached_references.add(request_ref)
    else:
        for _item, references in resolved_items:
            if references is None:
                continue
            matched = False
            for row in (
                TaskInputPayload.objects.using(using)
                .filter(reference__in=references.aliases)
                .values_list("reference", "state", "digest", "size_bytes")
            ):
                payload_rows_by_reference[str(row[0])] = (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    int(row[3]),
                )
                matched = True
            if not matched:
                if (
                    RayTaskExecution.objects.using(using)
                    .filter(
                        Q(input_reference__in=references.aliases)
                        | Q(ray_job_request_reference__in=references.aliases)
                    )
                    .exists()
                ):
                    attached_references.update(references.aliases)

    for item, references in resolved_items:
        counts["scanned"] += 1
        if references is None or item.malformed or item.native_reference is None:
            counts["malformed_key"] += 1
            record_detail(item.fingerprint_sha256, "malformed_key")
            continue

        discovered_native_references.add(references.canonical)
        rows = [
            payload_rows_by_reference[alias]
            for alias in sorted(references.aliases)
            if alias in payload_rows_by_reference
        ]
        if len(rows) > 1:
            counts["ambiguous"] += 1
            record_detail(item.fingerprint_sha256, "ambiguous")
            continue
        if len(rows) == 1:
            _row_ref, row_state, row_digest, row_size = rows[0]
            if row_digest != item.digest or row_size != item.size_bytes:
                counts["ambiguous"] += 1
                record_detail(item.fingerprint_sha256, "ambiguous")
            elif row_state == InputPayloadState.ACTIVE:
                counts["registered_active"] += 1
                record_detail(item.fingerprint_sha256, "registered_active")
            elif row_state == InputPayloadState.PURGED:
                counts["tombstone"] += 1
                record_detail(item.fingerprint_sha256, "tombstone")
            else:
                counts["ambiguous"] += 1
                record_detail(item.fingerprint_sha256, "ambiguous")
            continue

        if any(alias in attached_references for alias in references.aliases):
            counts["ambiguous"] += 1
            record_detail(item.fingerprint_sha256, "ambiguous")
            continue

        if item.modified_at is None or item.modified_at > cutoff:
            counts["unregistered_recent"] += 1
            record_detail(item.fingerprint_sha256, "unregistered_recent")
            continue

        counts["unregistered_eligible"] += 1
        if not delete_unregistered:
            record_detail(item.fingerprint_sha256, "unregistered_eligible")
            continue

        reap_outcome = _reap_unregistered_object(
            storage,
            backend_name=backend_name,
            item=item,
            references=references,
            cutoff=cutoff,
            config=resolved_config,
            use_default_delete=storage_backend is None,
            using=using,
        )
        if reap_outcome == "purged":
            counts["purged"] += 1
        elif reap_outcome == "failed":
            counts["failed"] += 1
        elif reap_outcome == "ambiguous":
            counts["ambiguous"] += 1
        elif reap_outcome == "unregistered_recent":
            counts["unregistered_recent"] += 1
        record_detail(item.fingerprint_sha256, reap_outcome)

    if not has_more:
        active_refs = list(
            TaskInputPayload.objects.using(using)
            .filter(state=InputPayloadState.ACTIVE, backend=backend_name)
            .order_by("reference")
            .values_list("reference", flat=True)[: max_objects + 1]
        )
        # The reverse registry scan has its own bound, even after the backend
        # listing completed. Uninspected registry rows cannot imply success.
        has_more = len(active_refs) > max_objects
        for active_ref in active_refs[:max_objects]:
            try:
                active_aliases = _payload_registry_references(active_ref, resolved_config)
                native_canonical = (
                    _native_request_reference(active_aliases.canonical)
                    if active_aliases.canonical.startswith(RAY_JOB_REQUEST_REFERENCE_PREFIX)
                    else active_aliases.canonical
                )
            except (ResultStorageError, RayJobRequestStorageError):
                counts["ambiguous"] += 1
                continue
            if native_canonical not in discovered_native_references:
                counts["registered_missing"] += 1
                record_detail(_fingerprint(active_ref), "registered_missing")

    total_events = counts["scanned"] + counts["registered_missing"]
    return PayloadBackendInventoryReport(
        backend=backend_name,
        cutoff=cutoff.isoformat(),
        grace_seconds=grace_seconds,
        delete_unregistered=delete_unregistered,
        has_more=has_more,
        counts=counts,
        details=details,
        details_omitted=max(0, total_events - len(details)),
    )
