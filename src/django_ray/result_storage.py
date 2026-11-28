"""Result storage backends for oversized task payloads."""

from __future__ import annotations

import errno
import hashlib
import importlib
import os
import re
import stat
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit

from django_ray._blob_storage import (
    _ALLOWED_FILESYSTEM_FILE_MODES,
    _DEFAULT_FILESYSTEM_FILE_MODE,
    _FILESYSTEM_DIR_MODE_BY_FILE_MODE,
    BlobDeleteOutcome,
    ResultStorageError,
    ResultStorageIntegrityError,
    clear_blob_client_caches,
    configured_filesystem_file_mode,
    delete_blob_verified,
    filesystem_namespaces_overlap,
    get_or_create_gcs_client,
    get_or_create_s3_client,
    hard_link_is_unsupported,
    install_temporary_payload_atomic,
    is_provider_not_found,
    is_s3_conditional_delete_unsupported,
    object_prefixes_overlap,
    provider_error_markers,
    provider_error_matches,
    register_blob_backend_resolver,
    write_temporary_payload_stream_hashed,
)

_REFERENCE_MAX_LENGTH = 500
_MAX_REFERENCE_BYTE_COUNT = 9_223_372_036_854_775_807
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_BYTE_COUNT_PATTERN = re.compile(r"0|[1-9][0-9]*")
_AUTHORITY_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?")
_FILESYSTEM_TEMP_ATTEMPTS = 3
_FILESYSTEM_LOCK_ATTEMPTS = 100
_FILESYSTEM_LOCK_WAIT_SECONDS = 0.01
_OBJECT_WRITE_ATTEMPTS = 3
_DEFAULT_RESULT_S3_PREFIX = "django-ray/results"
_DEFAULT_RESULT_GCS_PREFIX = "django-ray/results"
_DEFAULT_INPUT_S3_PREFIX = "django-ray/inputs"
_DEFAULT_INPUT_GCS_PREFIX = "django-ray/inputs"
_DIGEST_RESULT_PREFIX = "oversize://"


@dataclass
class _RefCountedResultLock:
    """In-process re-entrant lock and active-waiter count for one result reference."""

    lock: Any
    waiters: int = 0


_RESULT_REFERENCE_LOCKS_GUARD = threading.Lock()
_RESULT_REFERENCE_LOCKS: dict[tuple[int, int], _RefCountedResultLock] = {}


def is_external_result_reference(reference: str | None) -> bool:
    """Return whether ``reference`` points to an external result blob."""
    return bool(
        isinstance(reference, str) and reference and not reference.startswith(_DIGEST_RESULT_PREFIX)
    )


def _result_reference_fence_key(reference: str) -> tuple[int, int]:
    """Derive a stable 64-bit advisory lock key pair for one result reference."""
    normalized = reference
    try:
        normalized = canonicalize_result_reference(reference)
    except Exception:
        normalized = reference
    digest = hashlib.sha256(f"django-ray:result-ref-fence:v1:{normalized}".encode()).digest()
    key1 = int.from_bytes(digest[:4], byteorder="big", signed=True)
    key2 = int.from_bytes(digest[4:8], byteorder="big", signed=True)
    return key1, key2


@contextmanager
def _in_process_result_reference_lock(reference: str) -> Generator[tuple[int, int], None, None]:
    """Serialize in-process store/publish/delete operations on the same result blob."""
    key = _result_reference_fence_key(reference)
    with _RESULT_REFERENCE_LOCKS_GUARD:
        entry = _RESULT_REFERENCE_LOCKS.get(key)
        if entry is None:
            entry = _RefCountedResultLock(lock=threading.RLock())
            _RESULT_REFERENCE_LOCKS[key] = entry
        entry.waiters += 1
        lock = entry.lock
    lock.acquire()
    try:
        yield key
    finally:
        lock.release()
        with _RESULT_REFERENCE_LOCKS_GUARD:
            current = _RESULT_REFERENCE_LOCKS.get(key)
            if current is entry:
                current.waiters -= 1
                if current.waiters <= 0:
                    _RESULT_REFERENCE_LOCKS.pop(key, None)


def get_settings() -> dict[str, Any]:
    """Load Django-backed settings only when a caller omits explicit config."""
    from django_ray.conf.settings import get_settings as load_settings

    return load_settings()


@dataclass(frozen=True)
class _ResultReference:
    """Validated metadata carried by a result reference."""

    scheme: str
    digest: str
    size_bytes: int
    authority: str
    object_key_candidates: tuple[str, ...] = ()
    relative_path: str | None = None


def _invalid_reference() -> ResultStorageError:
    return ResultStorageError("Result reference is invalid")


def _parse_byte_count(raw_value: str) -> int:
    if _BYTE_COUNT_PATTERN.fullmatch(raw_value) is None:
        raise _invalid_reference()
    size_bytes = int(raw_value)
    if size_bytes > _MAX_REFERENCE_BYTE_COUNT:
        raise _invalid_reference()
    return size_bytes


def _object_key_is_safe(object_key: str) -> bool:
    parts = object_key.split("/")
    return bool(parts) and not any(
        not part
        or part in {".", ".."}
        or "\\" in part
        or any(not character.isprintable() for character in part)
        for part in parts
    )


def _canonical_object_prefix(prefix: str) -> str:
    if (
        not isinstance(prefix, str)
        or prefix != prefix.strip("/")
        or (prefix and not _object_key_is_safe(prefix))
    ):
        raise ResultStorageError("Result storage configuration is invalid")
    return prefix


def _canonical_authority(authority: str) -> str:
    if not isinstance(authority, str) or _AUTHORITY_PATTERN.fullmatch(authority) is None:
        raise ResultStorageError("Result storage configuration is invalid")
    return authority


def _build_object_key(prefix: str, digest: str) -> str:
    clean_prefix = _canonical_object_prefix(prefix)
    suffix = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
    if not clean_prefix:
        return suffix
    return f"{clean_prefix}/{suffix}"


def _configured_prefix(config: dict[str, Any], name: str, default: str) -> str:
    value = config.get(name)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ResultStorageError("Result storage configuration is invalid")
    return value


def _provider_error_markers(error: Exception) -> set[str | int]:
    """Extract only bounded status markers from an SDK exception."""
    return provider_error_markers(error)


def _provider_error_matches(error: Exception, *expected: str | int) -> bool:
    return provider_error_matches(error, *expected)


def _validated_s3_etag(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(not character.isprintable() for character in value)
    ):
        return None
    return value


def _decoded_canonical_object_key(encoded_key: str) -> str | None:
    object_key: str | None = None
    canonical_encoding: str | None = None
    try:
        object_key = unquote(encoded_key, encoding="utf-8", errors="strict")
        canonical_encoding = quote(object_key, safe="/-._~")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    if (
        object_key is None
        or canonical_encoding != encoded_key
        or not _object_key_is_safe(object_key)
    ):
        return None
    return object_key


def _object_key_digest(object_key: str) -> str | None:
    parts = object_key.split("/")
    if len(parts) < 3:
        return None
    filename = parts[-1]
    if not filename.endswith(".json"):
        return None
    digest = filename.removesuffix(".json")
    if _DIGEST_PATTERN.fullmatch(digest) is None or parts[-3:] != [
        digest[:2],
        digest[2:4],
        f"{digest}.json",
    ]:
        return None
    return digest


def _parse_result_reference(
    reference: str,
    *,
    allow_encoding_legacy: bool = False,
) -> _ResultReference:
    if not isinstance(reference, str) or not reference or len(reference) > _REFERENCE_MAX_LENGTH:
        raise _invalid_reference()

    parsed: Any | None = None
    try:
        parsed = urlsplit(reference)
    except (TypeError, ValueError):
        pass
    if parsed is None or parsed.fragment:
        raise _invalid_reference()

    if parsed.scheme == "oversize":
        digest = parsed.path.removeprefix("/")
        if (
            parsed.netloc != "sha256"
            or _DIGEST_PATTERN.fullmatch(digest) is None
            or not parsed.query.startswith("bytes=")
        ):
            raise _invalid_reference()
        raw_size = parsed.query.removeprefix("bytes=")
        size_bytes = _parse_byte_count(raw_size)
        canonical = f"oversize://sha256/{digest}?bytes={raw_size}"
        if reference != canonical:
            raise _invalid_reference()
        return _ResultReference(
            scheme="oversize",
            digest=digest,
            size_bytes=size_bytes,
            authority="sha256",
        )

    if parsed.scheme == "resultfs":
        digest = parsed.path.removeprefix("/")
        if parsed.netloc != "sha256" or _DIGEST_PATTERN.fullmatch(digest) is None:
            raise _invalid_reference()
        relative_path = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
        query_prefix = f"rel={relative_path}&bytes="
        if not parsed.query.startswith(query_prefix):
            raise _invalid_reference()
        raw_size = parsed.query.removeprefix(query_prefix)
        size_bytes = _parse_byte_count(raw_size)
        canonical = f"resultfs://sha256/{digest}?rel={relative_path}&bytes={raw_size}"
        if reference != canonical:
            raise _invalid_reference()
        return _ResultReference(
            scheme="resultfs",
            digest=digest,
            size_bytes=size_bytes,
            authority="sha256",
            relative_path=relative_path,
        )

    if (
        parsed.scheme not in {"s3", "gs"}
        or _AUTHORITY_PATTERN.fullmatch(parsed.netloc) is None
        or not parsed.path.startswith("/")
        or not parsed.query.startswith("bytes=")
    ):
        raise _invalid_reference()

    raw_size = parsed.query.removeprefix("bytes=")
    size_bytes = _parse_byte_count(raw_size)
    encoded_or_legacy_key = parsed.path.removeprefix("/")
    candidates: list[str] = []
    canonical_key = _decoded_canonical_object_key(encoded_or_legacy_key)
    if canonical_key is not None:
        candidates.append(canonical_key)
    # django-ray 0.2 and 0.3 interpolated configured keys into references
    # without percent-encoding. Keep that encoding-only compatibility path;
    # the configured bucket and prefix must still select the exact candidate.
    if _object_key_is_safe(encoded_or_legacy_key) and encoded_or_legacy_key not in candidates:
        candidates.append(encoded_or_legacy_key)
    digests = {
        digest for candidate in candidates if (digest := _object_key_digest(candidate)) is not None
    }
    if len(digests) != 1:
        raise _invalid_reference()
    digest = digests.pop()
    candidates = [candidate for candidate in candidates if _object_key_digest(candidate) == digest]
    if not candidates:
        raise _invalid_reference()
    canonical_reference = (
        f"{parsed.scheme}://{parsed.netloc}/{quote(candidates[0], safe='/-._~')}?bytes={raw_size}"
        if canonical_key is not None and candidates[0] == canonical_key
        else None
    )
    legacy_reference = f"{parsed.scheme}://{parsed.netloc}/{encoded_or_legacy_key}?bytes={raw_size}"
    if reference != canonical_reference and (
        not allow_encoding_legacy or reference != legacy_reference
    ):
        raise _invalid_reference()
    return _ResultReference(
        scheme=parsed.scheme,
        digest=digest,
        size_bytes=size_bytes,
        authority=parsed.netloc,
        object_key_candidates=tuple(candidates),
    )


def is_valid_result_reference(reference: str) -> bool:
    """Validate a canonical result reference without backend I/O."""
    try:
        _parse_result_reference(reference)
    except ResultStorageError:
        return False
    return True


class PayloadStorageBackend(Protocol):
    """Internal storage contract for durable serialized JSON payloads."""

    def store_payload(self, *, serialized_payload: str) -> str:
        """Persist a serialized payload and return a reference string."""

    def load(self, *, reference: str) -> str | None:
        """Load a serialized payload from a reference if supported."""

    def delete(self, *, reference: str) -> None:
        """Delete a serialized payload when the backend supports cleanup."""


class ResultStorageBackend(PayloadStorageBackend, Protocol):
    """Backward-compatible result storage contract."""

    def store(self, *, serialized_result: str) -> str:
        """Persist serialized result and return a reference string."""

    def load(self, *, reference: str) -> str | None:
        """Load serialized result from a reference if supported."""


def _payload_bytes(serialized_payload: str) -> bytes:
    payload: bytes | None = None
    try:
        payload = serialized_payload.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        pass
    if payload is None:
        raise ResultStorageError("Result payload could not be encoded")
    return payload


def _build_digest_and_bytes(serialized_result: str) -> tuple[bytes, str, int]:
    payload = _payload_bytes(serialized_result)
    return payload, hashlib.sha256(payload).hexdigest(), len(payload)


def _verified_payload(payload: object, metadata: _ResultReference) -> str:
    if (
        not isinstance(payload, bytes)
        or len(payload) != metadata.size_bytes
        or hashlib.sha256(payload).hexdigest() != metadata.digest
    ):
        raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
    decoded: str | None = None
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if decoded is None:
        raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
    return decoded


_ORIGINAL_PATH_OPEN = Path.open
_ORIGINAL_PATH_STAT = Path.stat


def _configured_filesystem_file_mode(file_mode: int | None = None) -> int:
    """Resolve and validate the shared-filesystem payload file mode."""
    return configured_filesystem_file_mode(file_mode)


def _write_temporary_payload(
    directory: Path,
    payload: bytes,
    *,
    file_mode: int | None = None,
) -> Path:
    """Write a same-directory temporary file without adopting a name collision."""
    temporary_path, _digest = write_temporary_payload_stream_hashed(
        directory,
        payload,
        file_mode=file_mode,
        os_module=os,
    )
    return temporary_path


def _read_filesystem_payload(
    path: Path,
    metadata: _ResultReference,
    *,
    unavailable_message: str,
) -> str:
    if Path.open is not _ORIGINAL_PATH_OPEN or Path.stat is not _ORIGINAL_PATH_STAT:
        # Preserve test seams that explicitly patch pathlib.Path.stat / Path.open.
        size: int | None = None
        try:
            size = path.stat().st_size
        except (OSError, OverflowError, RuntimeError, ValueError):
            pass
        if size is None:
            raise ResultStorageError(unavailable_message)
        if type(size) is not int or size != metadata.size_bytes:
            raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
        legacy_payload: bytes | None = None
        try:
            with path.open("rb") as legacy_handle:
                legacy_payload = legacy_handle.read(metadata.size_bytes + 1)
        except (OSError, OverflowError, RuntimeError, ValueError):
            pass
        if legacy_payload is None:
            raise ResultStorageError(unavailable_message)
        return _verified_payload(legacy_payload, metadata)

    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        try:
            if path.is_symlink():
                raise ResultStorageIntegrityError(
                    "Stored result payload failed integrity verification"
                )
        except ResultStorageIntegrityError:
            raise
        except (OSError, OverflowError, RuntimeError, ValueError):
            pass

    payload: bytes | None = None
    integrity_failed = False
    try:
        with open(
            path,
            "rb",
            buffering=0,
            opener=lambda name, mode: os.open(name, mode | flags),
        ) as handle:
            file_stat = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or type(file_stat.st_size) is not int
                or file_stat.st_size != metadata.size_bytes
            ):
                integrity_failed = True
            else:
                chunks = bytearray()
                limit = metadata.size_bytes + 1
                while len(chunks) < limit:
                    chunk = handle.read(min(64 * 1024, limit - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                payload = bytes(chunks)
    except OSError as error:
        if error.errno == errno.ELOOP:
            integrity_failed = True
    except (OverflowError, RuntimeError, ValueError):
        pass
    if integrity_failed:
        raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
    if payload is None:
        raise ResultStorageError(unavailable_message)
    return _verified_payload(payload, metadata)


def _filesystem_object_identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _verify_filesystem_delete(path: Path, metadata: _ResultReference) -> bool:
    """Verify bounded chunks before unlink; this is not a conditional unlink."""
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    handle = None
    try:
        handle = open(
            path, "rb", buffering=0, opener=lambda name, mode: os.open(name, mode | flags)
        )
    except FileNotFoundError:
        return False
    except (OSError, OverflowError, RuntimeError, ValueError):
        pass
    if handle is None:
        raise ResultStorageError("Failed to verify filesystem payload before deletion")

    failed = False
    integrity_failed = False
    try:
        with handle:
            initial = os.fstat(handle.fileno())
            if not stat.S_ISREG(initial.st_mode) or initial.st_size != metadata.size_bytes:
                integrity_failed = True
            else:
                digest = hashlib.sha256()
                remaining = metadata.size_bytes
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        integrity_failed = True
                        break
                    digest.update(chunk)
                    remaining -= len(chunk)
                if handle.read(1) or digest.hexdigest() != metadata.digest:
                    integrity_failed = True
                final = os.fstat(handle.fileno())
                if (
                    _filesystem_object_identity(final) != _filesystem_object_identity(initial)
                    or final.st_ctime_ns != initial.st_ctime_ns
                ):
                    integrity_failed = True
        # Refuse an already observed replacement, but do not claim an atomic
        # compare-and-unlink against arbitrary external filesystem mutations.
        if not integrity_failed and _filesystem_object_identity(
            path.stat(follow_symlinks=False)
        ) != (_filesystem_object_identity(initial)):
            integrity_failed = True
    except (OSError, OverflowError, RuntimeError, ValueError):
        failed = True
    if failed:
        raise ResultStorageError("Failed to verify filesystem payload before deletion")
    if integrity_failed:
        raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
    return True


def _hard_link_is_unsupported(error: OSError) -> bool:
    return hard_link_is_unsupported(error)


def _install_temporary_payload(temporary_path: Path, full_path: Path) -> bool:
    """Install without replacing an object created by another django-ray writer."""
    return install_temporary_payload_atomic(temporary_path, full_path, os_module=os)


class DigestResultStorage:
    """Digest-only reference backend (no external persistence)."""

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        _payload, digest, payload_size = _build_digest_and_bytes(serialized_payload)
        return f"oversize://sha256/{digest}?bytes={payload_size}"

    def load(self, *, reference: str) -> str | None:
        metadata = _parse_result_reference(reference)
        if metadata.scheme != "oversize":
            raise _invalid_reference()
        return None

    def delete_verified(
        self,
        *,
        reference: str,
        ignore_missing: bool = True,  # noqa: ARG002
    ) -> BlobDeleteOutcome:
        metadata = _parse_result_reference(reference)
        if metadata.scheme != "oversize":
            raise _invalid_reference()
        return BlobDeleteOutcome(
            deleted=False,
            already_missing=False,
            reference=reference,
            backend="digest",
        )

    def delete(self, *, reference: str, ignore_missing: bool = True) -> None:
        self.delete_verified(reference=reference, ignore_missing=ignore_missing)


class FilesystemResultStorage:
    """Filesystem-backed result storage for oversized payloads."""

    def __init__(self, root_path: str | Path, *, file_mode: int | None = None) -> None:
        resolved_root: Path | None = None
        try:
            resolved_root = Path(root_path).resolve(strict=False)
        except (OSError, OverflowError, RuntimeError, ValueError):
            pass
        if resolved_root is None:
            raise ResultStorageError("Filesystem result storage path is unavailable")
        # Freeze relative paths and existing symlink aliases at construction.
        # Every reference path is resolved again below and checked against this
        # stable root before filesystem I/O.
        self.root_path = resolved_root
        self.file_mode = _configured_filesystem_file_mode(file_mode)
        self.dir_mode = _FILESYSTEM_DIR_MODE_BY_FILE_MODE[self.file_mode]

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        payload, digest, payload_size = _build_digest_and_bytes(serialized_payload)
        relative_path = f"{digest[:2]}/{digest[2:4]}/{digest}.json"
        reference = f"resultfs://sha256/{digest}?rel={relative_path}&bytes={payload_size}"
        metadata = _parse_result_reference(reference)
        full_path = self._path_for_reference(metadata)

        setup_failed = False
        try:
            full_path.parent.mkdir(mode=self.dir_mode, parents=True, exist_ok=True)
        except (OSError, OverflowError, RuntimeError, ValueError):
            setup_failed = True
        if setup_failed:
            raise ResultStorageError("Failed to persist result payload to filesystem storage")

        with _in_process_result_reference_lock(reference):
            exists = False
            stat_failed = False
            try:
                full_path.stat()
                exists = True
            except FileNotFoundError:
                pass
            except (OSError, OverflowError, RuntimeError, ValueError):
                stat_failed = True
            if stat_failed:
                raise ResultStorageError("Failed to persist result payload to filesystem storage")
            if exists:
                reused = False
                try:
                    _read_filesystem_payload(
                        full_path,
                        metadata,
                        unavailable_message="Failed to persist result payload to filesystem storage",
                    )
                    # Advance mtime on content-addressed dedup reuse so concurrent
                    # retention purges observe a live modification timestamp.
                    os.utime(full_path, None)
                    reused = True
                except FileNotFoundError:
                    reused = False
                except ResultStorageError:
                    if full_path.exists():
                        raise
                    reused = False
                except (OSError, OverflowError, RuntimeError, ValueError):
                    raise ResultStorageError(
                        "Failed to persist result payload to filesystem storage"
                    ) from None
                if reused:
                    return reference

            for _attempt in range(2):
                temporary_path: Path | None = None
                install_failed = False
                installed_by_us = False
                try:
                    temporary_path, _written_digest = write_temporary_payload_stream_hashed(
                        full_path.parent,
                        payload,
                        expected_digest=digest,
                        file_mode=self.file_mode,
                        os_module=os,
                    )
                    installed_by_us = _install_temporary_payload(temporary_path, full_path)
                except (OSError, OverflowError, RuntimeError, ValueError):
                    install_failed = True
                finally:
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink(missing_ok=True)
                        except (OSError, OverflowError, RuntimeError, ValueError):
                            pass
                if install_failed:
                    raise ResultStorageError(
                        "Failed to persist result payload to filesystem storage"
                    )
                # Stream-hashed writes are verified during fsync before atomic install.
                # Only re-read and refresh mtime when a concurrent writer won installation first.
                if installed_by_us:
                    return reference
                try:
                    _read_filesystem_payload(
                        full_path,
                        metadata,
                        unavailable_message="Failed to persist result payload to filesystem storage",
                    )
                    os.utime(full_path, None)
                    return reference
                except FileNotFoundError:
                    continue
                except ResultStorageError:
                    if full_path.exists():
                        raise
                    continue
                except (OSError, OverflowError, RuntimeError, ValueError):
                    raise ResultStorageError(
                        "Failed to persist result payload to filesystem storage"
                    ) from None
            raise ResultStorageError("Failed to persist result payload to filesystem storage")

    def load(self, *, reference: str) -> str | None:
        metadata = self._filesystem_reference(reference)
        full_path = self._path_for_reference(metadata)
        return _read_filesystem_payload(
            full_path,
            metadata,
            unavailable_message="Result payload is unavailable from filesystem storage",
        )

    def delete_verified(
        self,
        *,
        reference: str,
        ignore_missing: bool = True,
    ) -> BlobDeleteOutcome:
        metadata = self._filesystem_reference(reference)
        full_path = self._path_for_reference(metadata)
        with _in_process_result_reference_lock(reference):
            if not _verify_filesystem_delete(full_path, metadata):
                if not ignore_missing:
                    raise ResultStorageError(
                        "Result payload is unavailable from filesystem storage"
                    )
                return BlobDeleteOutcome(
                    deleted=False,
                    already_missing=True,
                    reference=reference,
                    backend="filesystem",
                )
            delete_failed = False
            already_missing = False
            try:
                full_path.unlink(missing_ok=False)
            except FileNotFoundError:
                if not ignore_missing:
                    raise ResultStorageError(
                        "Result payload is unavailable from filesystem storage"
                    ) from None
                already_missing = True
            except (OSError, OverflowError, RuntimeError, ValueError):
                delete_failed = True
            if delete_failed:
                raise ResultStorageError("Failed to delete result payload from filesystem storage")
            return BlobDeleteOutcome(
                deleted=not already_missing,
                already_missing=already_missing,
                reference=reference,
                backend="filesystem",
            )

    def delete(self, *, reference: str, ignore_missing: bool = True) -> None:
        self.delete_verified(reference=reference, ignore_missing=ignore_missing)

    @staticmethod
    def _filesystem_reference(reference: str) -> _ResultReference:
        metadata = _parse_result_reference(reference)
        if metadata.scheme != "resultfs":
            raise _invalid_reference()
        return metadata

    def _path_for_reference(self, metadata: _ResultReference) -> Path:
        if metadata.relative_path is None:
            raise _invalid_reference()
        resolved_path: Path | None = None
        full_path = self.root_path.joinpath(*metadata.relative_path.split("/"))
        try:
            resolved_path = full_path.resolve(strict=False)
        except (OSError, OverflowError, RuntimeError, ValueError):
            pass
        if resolved_path is None:
            raise ResultStorageError("Filesystem result storage path is unavailable")
        if not resolved_path.is_relative_to(self.root_path):
            raise _invalid_reference()
        return resolved_path


def _validate_object_reference(
    metadata: _ResultReference,
    *,
    scheme: str,
    bucket: str,
    prefix: str,
) -> tuple[str, str]:
    canonical_bucket = _canonical_authority(bucket)
    expected_key = _build_object_key(prefix, metadata.digest)
    if (
        metadata.scheme != scheme
        or metadata.authority != canonical_bucket
        or expected_key not in metadata.object_key_candidates
    ):
        raise _invalid_reference()
    return canonical_bucket, expected_key


def _object_reference(*, scheme: str, bucket: str, key: str, size_bytes: int) -> str:
    encoded_key = quote(key, safe="/-._~")
    reference = f"{scheme}://{bucket}/{encoded_key}?bytes={size_bytes}"
    _parse_result_reference(reference)
    return reference


def canonicalize_result_reference(
    reference: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Return the canonical encoding of a validated historical result reference."""
    if config is None:
        config = get_settings()
    metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
    if metadata.scheme == "oversize":
        return reference
    if metadata.scheme == "resultfs":
        root_path = config.get("RESULT_STORAGE_FILESYSTEM_PATH")
        if (
            not isinstance(root_path, str)
            or not root_path
            or any(not character.isprintable() for character in root_path)
        ):
            raise ResultStorageError(
                "RESULT_STORAGE_FILESYSTEM_PATH is required to canonicalize filesystem "
                "result references"
            )
        return reference
    if metadata.scheme == "s3":
        bucket = config.get("RESULT_STORAGE_S3_BUCKET")
        if not isinstance(bucket, str) or not bucket:
            raise ResultStorageError(
                "RESULT_STORAGE_S3_BUCKET is required to canonicalize S3 result references"
            )
        prefix = _configured_prefix(
            config,
            "RESULT_STORAGE_S3_PREFIX",
            _DEFAULT_RESULT_S3_PREFIX,
        )
        canonical_bucket, key = _validate_object_reference(
            metadata,
            scheme="s3",
            bucket=bucket,
            prefix=prefix,
        )
        return _object_reference(
            scheme="s3",
            bucket=canonical_bucket,
            key=key,
            size_bytes=metadata.size_bytes,
        )
    bucket = config.get("RESULT_STORAGE_GCS_BUCKET")
    if not isinstance(bucket, str) or not bucket:
        raise ResultStorageError(
            "RESULT_STORAGE_GCS_BUCKET is required to canonicalize GCS result references"
        )
    prefix = _configured_prefix(
        config,
        "RESULT_STORAGE_GCS_PREFIX",
        _DEFAULT_RESULT_GCS_PREFIX,
    )
    canonical_bucket, key = _validate_object_reference(
        metadata,
        scheme="gs",
        bucket=bucket,
        prefix=prefix,
    )
    return _object_reference(
        scheme="gs",
        bucket=canonical_bucket,
        key=key,
        size_bytes=metadata.size_bytes,
    )


class S3ResultStorage:
    """S3-backed result storage for oversized payloads."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = _DEFAULT_RESULT_S3_PREFIX,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client: Any | None = None,
        conditional_delete: bool | None = None,
    ) -> None:
        self.bucket = _canonical_authority(bucket)
        self.prefix = _canonical_object_prefix(prefix)
        self.endpoint_url = endpoint_url
        self.region_name = region_name
        self.conditional_delete = conditional_delete
        if client is not None:
            self.client = client
            return
        self.client = get_or_create_s3_client(
            endpoint_url=endpoint_url,
            region_name=region_name,
            import_module_fn=importlib.import_module,
        )

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        payload, digest, payload_size = _build_digest_and_bytes(serialized_payload)
        key = _build_object_key(self.prefix, digest)
        reference = _object_reference(
            scheme="s3",
            bucket=self.bucket,
            key=key,
            size_bytes=payload_size,
        )
        for _attempt in range(_OBJECT_WRITE_ATTEMPTS):
            outcome = "stored"
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=payload,
                    ContentType="application/json",
                    IfNoneMatch="*",
                )
            except Exception as error:
                if _provider_error_matches(error, 412, "PreconditionFailed"):
                    outcome = "exists"
                elif _provider_error_matches(error, 409, "ConditionalRequestConflict"):
                    outcome = "retry"
                else:
                    outcome = "failed"
            if outcome == "stored":
                return reference
            if outcome == "exists":
                self.load(reference=reference)
                return reference
            if outcome == "failed":
                break
        raise ResultStorageError("Failed to persist result payload to S3 storage") from None

    def load(self, *, reference: str) -> str | None:
        metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
        bucket, key = _validate_object_reference(
            metadata,
            scheme="s3",
            bucket=self.bucket,
            prefix=self.prefix,
        )
        payload, _etag = self._load_verified_payload(
            metadata=metadata,
            bucket=bucket,
            key=key,
        )
        return payload

    def _load_verified_payload(
        self,
        *,
        metadata: _ResultReference,
        bucket: str,
        key: str,
        allow_missing: bool = False,
    ) -> tuple[str | None, str | None]:
        response: Any | None = None
        missing = False
        failed = False
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
        except Exception as error:
            if allow_missing and is_provider_not_found(error):
                missing = True
            else:
                failed = True
        if missing:
            return None, None
        if failed or not isinstance(response, dict):
            raise ResultStorageError("Result payload is unavailable from S3 storage") from None
        content_length: object | None = None
        body: object | None = None
        response_etag: object | None = None
        response_failed = False
        try:
            content_length = response.get("ContentLength")
            body = response.get("Body")
            response_etag = response.get("ETag")
        except Exception:
            response_failed = True
        if response_failed:
            _close_response_body(body)
            raise ResultStorageError("Result payload is unavailable from S3 storage") from None
        if type(content_length) is not int or content_length != metadata.size_bytes or body is None:
            _close_response_body(body)
            raise ResultStorageIntegrityError("Stored result payload failed integrity verification")

        payload: object | None = None
        read_failed = False
        try:
            payload = body.read(metadata.size_bytes + 1)
        except Exception:
            read_failed = True
        finally:
            _close_response_body(body)
        if read_failed:
            raise ResultStorageError("Result payload is unavailable from S3 storage") from None
        return _verified_payload(payload, metadata), _validated_s3_etag(response_etag)

    def _head_verified_metadata(
        self,
        *,
        metadata: _ResultReference,
        bucket: str,
        key: str,
    ) -> tuple[bool, str | None]:
        """Verify S3 object size and ETag via HEAD without downloading payload bytes."""
        response: Any | None = None
        missing = False
        failed = False
        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
        except Exception as error:
            if is_provider_not_found(error):
                missing = True
            else:
                failed = True
        if missing:
            return False, None
        if failed or not isinstance(response, dict):
            raise ResultStorageError("Result payload is unavailable from S3 storage") from None
        content_length: object | None = None
        response_etag: object | None = None
        response_failed = False
        try:
            content_length = response.get("ContentLength")
            response_etag = response.get("ETag")
        except Exception:
            response_failed = True
        if response_failed:
            raise ResultStorageError("Result payload is unavailable from S3 storage") from None
        if type(content_length) is not int or content_length != metadata.size_bytes:
            raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
        return True, _validated_s3_etag(response_etag)

    def delete_verified(
        self,
        *,
        reference: str,
        ignore_missing: bool = True,
    ) -> BlobDeleteOutcome:
        metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
        bucket, key = _validate_object_reference(
            metadata,
            scheme="s3",
            bucket=self.bucket,
            prefix=self.prefix,
        )
        head_object = getattr(self.client, "head_object", None)
        etag: str | None = None
        if callable(head_object):
            exists, etag = self._head_verified_metadata(
                metadata=metadata,
                bucket=bucket,
                key=key,
            )
            if not exists:
                if not ignore_missing:
                    raise ResultStorageError("Result payload is unavailable from S3 storage")
                return BlobDeleteOutcome(
                    deleted=False,
                    already_missing=True,
                    reference=reference,
                    backend="s3",
                )
        else:
            loaded_payload, etag = self._load_verified_payload(
                metadata=metadata,
                bucket=bucket,
                key=key,
                allow_missing=ignore_missing,
            )
            if loaded_payload is None:
                return BlobDeleteOutcome(
                    deleted=False,
                    already_missing=True,
                    reference=reference,
                    backend="s3",
                )

        if self.conditional_delete is False:
            missing = False
            failed = False
            try:
                self.client.delete_object(Bucket=bucket, Key=key)
            except Exception as error:
                if ignore_missing and is_provider_not_found(error):
                    missing = True
                else:
                    failed = True
            if missing:
                return BlobDeleteOutcome(
                    deleted=False,
                    already_missing=True,
                    reference=reference,
                    backend="s3",
                )
            if failed:
                raise ResultStorageError("Failed to delete result payload from S3 storage")
            return BlobDeleteOutcome(
                deleted=True,
                already_missing=False,
                reference=reference,
                backend="s3",
            )

        if etag is None:
            raise ResultStorageError("Failed to delete result payload from S3 storage")

        missing = False
        need_unconditional_fallback = False
        failed = False
        try:
            self.client.delete_object(Bucket=bucket, Key=key, IfMatch=etag)
        except Exception as error:
            if ignore_missing and is_provider_not_found(error):
                missing = True
            elif is_s3_conditional_delete_unsupported(error) and (
                self.endpoint_url is not None or self.conditional_delete is None
            ):
                need_unconditional_fallback = True
            else:
                failed = True
        if missing:
            return BlobDeleteOutcome(
                deleted=False,
                already_missing=True,
                reference=reference,
                backend="s3",
            )
        if need_unconditional_fallback:
            try:
                self.client.delete_object(Bucket=bucket, Key=key)
            except Exception as fallback_error:
                if ignore_missing and is_provider_not_found(fallback_error):
                    missing = True
                else:
                    failed = True
            if missing:
                return BlobDeleteOutcome(
                    deleted=False,
                    already_missing=True,
                    reference=reference,
                    backend="s3",
                )
        if failed:
            raise ResultStorageError("Failed to delete result payload from S3 storage")
        return BlobDeleteOutcome(
            deleted=True,
            already_missing=False,
            reference=reference,
            backend="s3",
        )

    def delete(self, *, reference: str, ignore_missing: bool = True) -> None:
        self.delete_verified(reference=reference, ignore_missing=ignore_missing)


def _close_response_body(body: object) -> None:
    close: Any | None = None
    try:
        close = getattr(body, "close", None)
    except Exception:
        pass
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


class GCSResultStorage:
    """GCS-backed result storage for oversized payloads."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = _DEFAULT_RESULT_GCS_PREFIX,
        client: Any | None = None,
    ) -> None:
        self.bucket = _canonical_authority(bucket)
        self.prefix = _canonical_object_prefix(prefix)
        if client is not None:
            self.client = client
            return
        self.client = get_or_create_gcs_client(import_module_fn=importlib.import_module)

    def store(self, *, serialized_result: str) -> str:
        return self.store_payload(serialized_payload=serialized_result)

    def store_payload(self, *, serialized_payload: str) -> str:
        payload, digest, payload_size = _build_digest_and_bytes(serialized_payload)
        key = _build_object_key(self.prefix, digest)
        reference = _object_reference(
            scheme="gs",
            bucket=self.bucket,
            key=key,
            size_bytes=payload_size,
        )
        outcome = "stored"
        try:
            blob = self.client.bucket(self.bucket).blob(key)
            blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
        except Exception as error:
            if _provider_error_matches(error, 412, "PreconditionFailed"):
                outcome = "exists"
            else:
                outcome = "failed"
        if outcome == "exists":
            self.load(reference=reference)
            return reference
        if outcome == "failed":
            raise ResultStorageError("Failed to persist result payload to GCS storage") from None
        return reference

    def load(self, *, reference: str) -> str | None:
        metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
        bucket, key = _validate_object_reference(
            metadata,
            scheme="gs",
            bucket=self.bucket,
            prefix=self.prefix,
        )
        payload, _blob, _generation = self._load_verified_payload(
            metadata=metadata,
            bucket=bucket,
            key=key,
        )
        return payload

    def _load_verified_payload(
        self,
        *,
        metadata: _ResultReference,
        bucket: str,
        key: str,
    ) -> tuple[str, Any, int]:
        blob: Any | None = None
        failed = False
        try:
            blob = self.client.bucket(bucket).blob(key)
            blob.reload()
        except Exception:
            failed = True
        if failed or blob is None:
            raise ResultStorageError("Result payload is unavailable from GCS storage") from None
        blob_size: object | None = None
        try:
            blob_size = blob.size
        except Exception:
            failed = True
        if failed:
            raise ResultStorageError("Result payload is unavailable from GCS storage") from None
        if type(blob_size) is not int or blob_size != metadata.size_bytes:
            raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
        generation: object | None = None
        try:
            generation = blob.generation
        except Exception:
            failed = True
        if failed:
            raise ResultStorageError("Result payload is unavailable from GCS storage") from None
        if type(generation) is not int or generation <= 0:
            raise ResultStorageIntegrityError("Stored result payload failed integrity verification")

        payload: object | None = None
        try:
            payload = blob.download_as_bytes(
                start=0,
                end=metadata.size_bytes,
                if_generation_match=generation,
            )
        except Exception:
            failed = True
        if failed:
            raise ResultStorageError("Result payload is unavailable from GCS storage") from None
        return _verified_payload(payload, metadata), blob, generation

    def _reload_verified_blob_metadata(
        self,
        *,
        metadata: _ResultReference,
        bucket: str,
        key: str,
    ) -> tuple[Any | None, int | None]:
        """Fetch GCS blob metadata via reload() without downloading payload bytes."""
        blob: Any | None = None
        missing = False
        failed = False
        try:
            blob = self.client.bucket(bucket).blob(key)
            blob.reload()
        except Exception as error:
            if is_provider_not_found(error):
                missing = True
            else:
                failed = True
        if missing:
            return None, None
        if failed or blob is None:
            raise ResultStorageError("Result payload is unavailable from GCS storage") from None
        blob_size: object | None = None
        size_failed = False
        try:
            blob_size = blob.size
        except Exception:
            size_failed = True
        if size_failed:
            raise ResultStorageError("Result payload is unavailable from GCS storage") from None
        if type(blob_size) is not int or blob_size != metadata.size_bytes:
            raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
        generation: object | None = None
        generation_failed = False
        try:
            generation = blob.generation
        except Exception:
            generation_failed = True
        if generation_failed:
            raise ResultStorageError("Result payload is unavailable from GCS storage") from None
        if type(generation) is not int or generation <= 0:
            raise ResultStorageIntegrityError("Stored result payload failed integrity verification")
        return blob, generation

    def delete_verified(
        self,
        *,
        reference: str,
        ignore_missing: bool = True,
    ) -> BlobDeleteOutcome:
        metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
        bucket, key = _validate_object_reference(
            metadata,
            scheme="gs",
            bucket=self.bucket,
            prefix=self.prefix,
        )
        blob, generation = self._reload_verified_blob_metadata(
            metadata=metadata,
            bucket=bucket,
            key=key,
        )
        if blob is None or generation is None:
            if not ignore_missing:
                raise ResultStorageError("Result payload is unavailable from GCS storage")
            return BlobDeleteOutcome(
                deleted=False,
                already_missing=True,
                reference=reference,
                backend="gcs",
            )
        missing = False
        failed = False
        try:
            blob.delete(if_generation_match=generation)
        except Exception as error:
            if ignore_missing and is_provider_not_found(error):
                missing = True
            else:
                failed = True
        if missing:
            return BlobDeleteOutcome(
                deleted=False,
                already_missing=True,
                reference=reference,
                backend="gcs",
            )
        if failed:
            raise ResultStorageError("Failed to delete result payload from GCS storage")
        return BlobDeleteOutcome(
            deleted=True,
            already_missing=False,
            reference=reference,
            backend="gcs",
        )

    def delete(self, *, reference: str, ignore_missing: bool = True) -> None:
        self.delete_verified(reference=reference, ignore_missing=ignore_missing)


def _configuration_error(name: str, detail: str) -> ResultStorageError:
    return ResultStorageError(f"{name} {detail}")


def _validate_filesystem_setting(config: dict[str, Any], name: str, *, required: bool) -> None:
    value = config.get(name)
    if value is None and not required:
        return
    if (
        not isinstance(value, str)
        or not value
        or any(not character.isprintable() for character in value)
    ):
        raise _configuration_error(name, "must be a non-empty printable path")


def _validated_config_prefix(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip("/")
        or (value and not _object_key_is_safe(value))
    ):
        raise _configuration_error(name, "must be a canonical object-key prefix")
    return value


def _validate_object_settings(
    config: dict[str, Any],
    *,
    scheme: str,
    bucket_name: str,
    prefix_name: str,
    default_prefix: str,
    required: bool,
    retained: bool,
) -> None:
    if not required and not retained:
        return
    bucket = config.get(bucket_name)
    if not isinstance(bucket, str) or _AUTHORITY_PATTERN.fullmatch(bucket) is None:
        raise _configuration_error(bucket_name, "must be a canonical storage authority")
    raw_prefix = config.get(prefix_name)
    prefix = (
        default_prefix if raw_prefix is None else _validated_config_prefix(raw_prefix, prefix_name)
    )
    key = _build_object_key(prefix, "0" * 64)
    reference = f"{scheme}://{bucket}/{quote(key, safe='/-._~')}?bytes={_MAX_REFERENCE_BYTE_COUNT}"
    if len(reference) > _REFERENCE_MAX_LENGTH:
        raise _configuration_error(
            prefix_name,
            f"cannot produce references within {_REFERENCE_MAX_LENGTH} characters",
        )


def _normalized_filesystem_namespace(value: str, name: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.path.realpath(os.path.normpath(value))))
    except (OSError, OverflowError, RuntimeError, ValueError):
        raise _configuration_error(name, "must identify a usable filesystem namespace") from None


def _normalized_s3_endpoint_namespace(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/")


def validate_storage_configuration(config: dict[str, Any]) -> None:
    """Validate active and retained result/input storage namespaces without I/O."""
    result_backend = config.get("RESULT_STORAGE_BACKEND", "digest")
    _validate_filesystem_setting(
        config,
        "RESULT_STORAGE_FILESYSTEM_PATH",
        required=result_backend == "filesystem",
    )

    result_s3_prefix = config.get("RESULT_STORAGE_S3_PREFIX")
    result_s3_retained = config.get("RESULT_STORAGE_S3_BUCKET") is not None or (
        result_s3_prefix not in {None, _DEFAULT_RESULT_S3_PREFIX}
    )
    result_s3_configured = result_backend == "s3" or result_s3_retained
    _validate_object_settings(
        config,
        scheme="s3",
        bucket_name="RESULT_STORAGE_S3_BUCKET",
        prefix_name="RESULT_STORAGE_S3_PREFIX",
        default_prefix=_DEFAULT_RESULT_S3_PREFIX,
        required=result_backend == "s3",
        retained=result_s3_retained,
    )

    result_gcs_prefix = config.get("RESULT_STORAGE_GCS_PREFIX")
    result_gcs_retained = config.get("RESULT_STORAGE_GCS_BUCKET") is not None or (
        result_gcs_prefix not in {None, _DEFAULT_RESULT_GCS_PREFIX}
    )
    _validate_object_settings(
        config,
        scheme="gs",
        bucket_name="RESULT_STORAGE_GCS_BUCKET",
        prefix_name="RESULT_STORAGE_GCS_PREFIX",
        default_prefix=_DEFAULT_RESULT_GCS_PREFIX,
        required=result_backend == "gcs",
        retained=result_gcs_retained,
    )

    input_backend = config.get("INPUT_STORAGE_BACKEND")
    _validate_filesystem_setting(
        config,
        "INPUT_STORAGE_FILESYSTEM_PATH",
        required=input_backend == "filesystem",
    )

    input_s3_prefix = config.get("INPUT_STORAGE_S3_PREFIX")
    input_s3_retained = config.get("INPUT_STORAGE_S3_BUCKET") is not None or (
        input_s3_prefix not in {None, _DEFAULT_INPUT_S3_PREFIX}
    )
    input_s3_configured = input_backend == "s3" or input_s3_retained
    _validate_object_settings(
        config,
        scheme="s3",
        bucket_name="INPUT_STORAGE_S3_BUCKET",
        prefix_name="INPUT_STORAGE_S3_PREFIX",
        default_prefix=_DEFAULT_INPUT_S3_PREFIX,
        required=input_backend == "s3",
        retained=input_s3_retained,
    )

    input_gcs_prefix = config.get("INPUT_STORAGE_GCS_PREFIX")
    input_gcs_retained = config.get("INPUT_STORAGE_GCS_BUCKET") is not None or (
        input_gcs_prefix not in {None, _DEFAULT_INPUT_GCS_PREFIX}
    )
    _validate_object_settings(
        config,
        scheme="gs",
        bucket_name="INPUT_STORAGE_GCS_BUCKET",
        prefix_name="INPUT_STORAGE_GCS_PREFIX",
        default_prefix=_DEFAULT_INPUT_GCS_PREFIX,
        required=input_backend == "gcs",
        retained=input_gcs_retained,
    )

    result_filesystem_path = config.get("RESULT_STORAGE_FILESYSTEM_PATH")
    input_filesystem_path = config.get("INPUT_STORAGE_FILESYSTEM_PATH")
    if isinstance(result_filesystem_path, str) and isinstance(input_filesystem_path, str):
        result_filesystem_namespace = _normalized_filesystem_namespace(
            result_filesystem_path,
            "RESULT_STORAGE_FILESYSTEM_PATH",
        )
        input_filesystem_namespace = _normalized_filesystem_namespace(
            input_filesystem_path,
            "INPUT_STORAGE_FILESYSTEM_PATH",
        )
        if result_filesystem_namespace == input_filesystem_namespace or (
            filesystem_namespaces_overlap(
                result_filesystem_namespace,
                input_filesystem_namespace,
                normpath_fn=os.path.realpath,
            )
        ):
            raise _configuration_error(
                "INPUT_STORAGE_FILESYSTEM_PATH",
                "must not reuse the result storage namespace",
            )

    if result_s3_configured and input_s3_configured:
        result_s3_endpoint = _normalized_s3_endpoint_namespace(
            config.get("RESULT_STORAGE_S3_ENDPOINT_URL")
        )
        input_s3_endpoint = _normalized_s3_endpoint_namespace(
            config.get("INPUT_STORAGE_S3_ENDPOINT_URL")
        )
        result_s3_bucket = config.get("RESULT_STORAGE_S3_BUCKET")
        input_s3_bucket = config.get("INPUT_STORAGE_S3_BUCKET")
        result_s3_effective_prefix = (
            _DEFAULT_RESULT_S3_PREFIX if result_s3_prefix is None else str(result_s3_prefix)
        )
        input_s3_effective_prefix = (
            _DEFAULT_INPUT_S3_PREFIX if input_s3_prefix is None else str(input_s3_prefix)
        )
        if (
            result_s3_endpoint == input_s3_endpoint
            and result_s3_bucket == input_s3_bucket
            and object_prefixes_overlap(result_s3_effective_prefix, input_s3_effective_prefix)
        ):
            raise _configuration_error(
                "INPUT_STORAGE_S3_PREFIX",
                "must not reuse the result storage namespace",
            )

    result_gcs_configured = result_backend == "gcs" or result_gcs_retained
    input_gcs_configured = input_backend == "gcs" or input_gcs_retained
    if result_gcs_configured and input_gcs_configured:
        result_gcs_bucket = config.get("RESULT_STORAGE_GCS_BUCKET")
        input_gcs_bucket = config.get("INPUT_STORAGE_GCS_BUCKET")
        result_gcs_effective_prefix = (
            _DEFAULT_RESULT_GCS_PREFIX if result_gcs_prefix is None else str(result_gcs_prefix)
        )
        input_gcs_effective_prefix = (
            _DEFAULT_INPUT_GCS_PREFIX if input_gcs_prefix is None else str(input_gcs_prefix)
        )
        if result_gcs_bucket == input_gcs_bucket and object_prefixes_overlap(
            result_gcs_effective_prefix, input_gcs_effective_prefix
        ):
            raise _configuration_error(
                "INPUT_STORAGE_GCS_PREFIX",
                "must not reuse the result storage namespace",
            )


def get_result_storage_backend_for_reference(
    reference: str,
    config: dict[str, Any] | None = None,
) -> ResultStorageBackend:
    """Resolve a result storage backend from a validated stored reference."""
    if config is None:
        config = get_settings()

    canonical_reference = is_valid_result_reference(reference)
    metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
    if metadata.scheme == "oversize":
        return DigestResultStorage()
    if metadata.scheme == "resultfs":
        root_path = config.get("RESULT_STORAGE_FILESYSTEM_PATH")
        if not root_path:
            raise ResultStorageError(
                "RESULT_STORAGE_FILESYSTEM_PATH is required to load filesystem result references"
            )
        return FilesystemResultStorage(root_path)
    if metadata.scheme == "s3":
        bucket = config.get("RESULT_STORAGE_S3_BUCKET")
        if not bucket:
            if not canonical_reference:
                raise _invalid_reference()
            raise ResultStorageError(
                "RESULT_STORAGE_S3_BUCKET is required to load S3 result references"
            )
        prefix = _configured_prefix(
            config,
            "RESULT_STORAGE_S3_PREFIX",
            _DEFAULT_RESULT_S3_PREFIX,
        )
        _validate_object_reference(
            metadata,
            scheme="s3",
            bucket=str(bucket),
            prefix=str(prefix),
        )
        return S3ResultStorage(
            bucket=str(bucket),
            prefix=str(prefix),
            endpoint_url=(
                str(config["RESULT_STORAGE_S3_ENDPOINT_URL"])
                if config.get("RESULT_STORAGE_S3_ENDPOINT_URL")
                else None
            ),
            region_name=(
                str(config["RESULT_STORAGE_S3_REGION"])
                if config.get("RESULT_STORAGE_S3_REGION")
                else None
            ),
        )
    if metadata.scheme == "gs":
        bucket = config.get("RESULT_STORAGE_GCS_BUCKET")
        if not bucket:
            if not canonical_reference:
                raise _invalid_reference()
            raise ResultStorageError(
                "RESULT_STORAGE_GCS_BUCKET is required to load GCS result references"
            )
        prefix = _configured_prefix(
            config,
            "RESULT_STORAGE_GCS_PREFIX",
            _DEFAULT_RESULT_GCS_PREFIX,
        )
        _validate_object_reference(
            metadata,
            scheme="gs",
            bucket=str(bucket),
            prefix=str(prefix),
        )
        return GCSResultStorage(bucket=str(bucket), prefix=str(prefix))

    raise _invalid_reference()


def load_result_reference(
    reference: str,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Load serialized result data from a validated stored reference."""
    backend = get_result_storage_backend_for_reference(reference, config)
    return backend.load(reference=reference)


def delete_result_reference(
    reference: str,
    config: dict[str, Any] | None = None,
    *,
    ignore_missing: bool = True,
) -> bool:
    """Delete one validated external result payload with 404 idempotency.

    Returns ``True`` if an external object was deleted, or ``False`` if the
    reference is digest-only or already missing (when ``ignore_missing=True``).
    """
    outcome = delete_blob_verified(
        config,
        reference,
        ignore_missing=ignore_missing,
    )
    return outcome.deleted


def get_result_storage_backend(config: dict[str, Any] | None = None) -> ResultStorageBackend:
    """Resolve the configured result storage backend."""
    if config is None:
        config = get_settings()

    backend = str(config.get("RESULT_STORAGE_BACKEND", "digest")).lower()
    if backend == "digest":
        return DigestResultStorage()
    if backend == "filesystem":
        root_path = config.get("RESULT_STORAGE_FILESYSTEM_PATH")
        if not root_path:
            raise ResultStorageError(
                "RESULT_STORAGE_FILESYSTEM_PATH is required for RESULT_STORAGE_BACKEND='filesystem'"
            )
        return FilesystemResultStorage(root_path)
    if backend == "s3":
        bucket = config.get("RESULT_STORAGE_S3_BUCKET")
        if not bucket:
            raise ResultStorageError(
                "RESULT_STORAGE_S3_BUCKET is required for RESULT_STORAGE_BACKEND='s3'"
            )
        return S3ResultStorage(
            bucket=str(bucket),
            prefix=_configured_prefix(
                config,
                "RESULT_STORAGE_S3_PREFIX",
                _DEFAULT_RESULT_S3_PREFIX,
            ),
            endpoint_url=(
                str(config["RESULT_STORAGE_S3_ENDPOINT_URL"])
                if config.get("RESULT_STORAGE_S3_ENDPOINT_URL")
                else None
            ),
            region_name=(
                str(config["RESULT_STORAGE_S3_REGION"])
                if config.get("RESULT_STORAGE_S3_REGION")
                else None
            ),
        )
    if backend == "gcs":
        bucket = config.get("RESULT_STORAGE_GCS_BUCKET")
        if not bucket:
            raise ResultStorageError(
                "RESULT_STORAGE_GCS_BUCKET is required for RESULT_STORAGE_BACKEND='gcs'"
            )
        return GCSResultStorage(
            bucket=str(bucket),
            prefix=_configured_prefix(
                config,
                "RESULT_STORAGE_GCS_PREFIX",
                _DEFAULT_RESULT_GCS_PREFIX,
            ),
        )

    raise ResultStorageError(f"Unsupported RESULT_STORAGE_BACKEND: {backend}")


def _resolve_blob_backend_for_reference(
    reference: str,
    config: dict[str, Any] | None = None,
) -> ResultStorageBackend:
    return get_result_storage_backend_for_reference(reference, config)


register_blob_backend_resolver(_resolve_blob_backend_for_reference)


__all__ = [
    "BlobDeleteOutcome",
    "DigestResultStorage",
    "FilesystemResultStorage",
    "GCSResultStorage",
    "PayloadStorageBackend",
    "ResultStorageBackend",
    "ResultStorageError",
    "ResultStorageIntegrityError",
    "S3ResultStorage",
    "_ALLOWED_FILESYSTEM_FILE_MODES",
    "_DEFAULT_FILESYSTEM_FILE_MODE",
    "_FILESYSTEM_DIR_MODE_BY_FILE_MODE",
    "canonicalize_result_reference",
    "clear_blob_client_caches",
    "delete_blob_verified",
    "delete_result_reference",
    "get_result_storage_backend",
    "get_result_storage_backend_for_reference",
    "is_external_result_reference",
    "is_valid_result_reference",
    "load_result_reference",
    "validate_storage_configuration",
]
