"""Unified internal blob-storage engine for results, inputs, and request payloads.

Consolidates filesystem security hardening (0o600/0o640 modes, O_NOFOLLOW,
O_NONBLOCK, fstat S_ISREG), atomic hard-link/filelock installs, stale-lock-safe
cross-process directory locks, thread-safe S3/GCS client pooling with bounded
timeouts, metadata-only 404-idempotent deletion, and canonical namespace
containment checks behind django-ray's existing 0.8.x storage settings.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib
import json
import os
import secrets
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

_DEFAULT_FILESYSTEM_FILE_MODE = 0o600
_ALLOWED_FILESYSTEM_FILE_MODES = frozenset({0o600, 0o640})
_FILESYSTEM_DIR_MODE_BY_FILE_MODE: dict[int, int] = {
    0o600: 0o700,
    0o640: 0o750,
}
_FILESYSTEM_TEMP_ATTEMPTS = 3
_FILESYSTEM_WRITE_CHUNK_BYTES = 64 * 1024
_FILESYSTEM_INSTALL_LOCK_TIMEOUT_SECONDS = 5.0
_FILESYSTEM_LEGACY_LOCK_STALE_SECONDS = 30.0

_DEFAULT_S3_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_S3_READ_TIMEOUT_SECONDS = 30.0
_DEFAULT_S3_MAX_ATTEMPTS = 3

_LOCK_OWNER_FILENAME = "owner.json"

_ORIGINAL_IMPORT_MODULE = importlib.import_module
_ORIGINAL_PATH_OPEN = Path.open
_ORIGINAL_PATH_STAT = Path.stat

_S3_CLIENT_POOL: dict[tuple[str | None, str | None, float, float], Any] = {}
_S3_CLIENT_POOL_LOCK = threading.RLock()
_GCS_CLIENT_POOL: dict[str, Any] = {}
_GCS_CLIENT_POOL_LOCK = threading.RLock()


class ResultStorageError(RuntimeError):
    """Raised when result or blob storage operations fail."""


class ResultStorageIntegrityError(ResultStorageError):
    """Raised when stored bytes do not match their content-addressed reference."""


@dataclass(frozen=True, slots=True)
class BlobDeleteOutcome:
    """Normalized result of a metadata-verified blob deletion."""

    deleted: bool
    already_missing: bool
    reference: str
    backend: str


def clear_blob_client_caches() -> None:
    """Clear pooled S3 and GCS SDK clients (intended for tests and reconnects)."""
    with _S3_CLIENT_POOL_LOCK:
        _S3_CLIENT_POOL.clear()
    with _GCS_CLIENT_POOL_LOCK:
        _GCS_CLIENT_POOL.clear()


def provider_error_markers(error: Exception) -> set[str | int]:
    """Extract only bounded status markers from an SDK exception."""
    markers: set[str | int] = set()

    def add_marker(value: object) -> None:
        normalized: str | int | None = None
        try:
            if type(value) is int:
                normalized = int(value)
            elif isinstance(value, int) and not isinstance(value, bool):
                normalized = int(value)
            elif isinstance(value, str) and len(value) <= 128:
                normalized = str(value)
        except Exception:
            pass
        if normalized is not None:
            markers.add(normalized)

    response: object | None = None
    try:
        response = getattr(error, "response", None)
    except Exception:
        pass
    if isinstance(response, dict):
        error_data: object | None = None
        response_metadata: object | None = None
        try:
            error_data = response.get("Error")
            response_metadata = response.get("ResponseMetadata")
        except Exception:
            pass
        if isinstance(error_data, dict):
            try:
                add_marker(error_data.get("Code"))
            except Exception:
                pass
        if isinstance(response_metadata, dict):
            try:
                add_marker(response_metadata.get("HTTPStatusCode"))
            except Exception:
                pass
    for name in ("code", "status_code"):
        value: object | None = None
        try:
            value = getattr(error, name, None)
        except Exception:
            pass
        add_marker(value)
    return markers


def provider_error_matches(error: Exception, *expected: str | int) -> bool:
    """Return whether an SDK exception carries any expected status or error code."""
    markers = provider_error_markers(error)
    normalized = {str(marker).casefold() for marker in markers}
    return any(str(marker).casefold() in normalized for marker in expected)


def is_provider_not_found(error: Exception) -> bool:
    """Return whether a storage exception indicates the object does not exist."""
    if isinstance(error, FileNotFoundError):
        return True
    if type(error).__name__ in {"NotFound", "NoSuchKey", "NoSuchObject"}:
        return True
    return provider_error_matches(error, 404, "404", "NoSuchKey", "NotFound", "NoSuchObject")


def is_s3_conditional_delete_unsupported(error: Exception) -> bool:
    """Return whether an S3 client or S3-compatible store rejects ``IfMatch`` deletes."""
    if type(error).__name__ == "ParamValidationError":
        return True
    return provider_error_matches(
        error,
        501,
        "501",
        "NotImplemented",
        "NotSupported",
        "InvalidRequest",
        "InvalidArgument",
    )


def configured_filesystem_file_mode(file_mode: int | None = None) -> int:
    """Resolve and validate the shared-filesystem payload file mode."""
    candidate: object = file_mode
    if candidate is None:
        django_conf = sys.modules.get("django.conf")
        django_settings = (
            getattr(django_conf, "settings", None) if django_conf is not None else None
        )
        if django_settings is not None and getattr(django_settings, "configured", False):
            config = getattr(django_settings, "DJANGO_RAY", None)
            if isinstance(config, dict):
                candidate = config.get("RESULT_STORAGE_FILESYSTEM_FILE_MODE")
    if candidate is None:
        return _DEFAULT_FILESYSTEM_FILE_MODE
    if isinstance(candidate, str):
        try:
            candidate = int(candidate.strip(), 8)
        except ValueError as error:
            raise ResultStorageError(
                "RESULT_STORAGE_FILESYSTEM_FILE_MODE must be 0o600 or 0o640"
            ) from error
    if type(candidate) is not int or candidate not in _ALLOWED_FILESYSTEM_FILE_MODES:
        raise ResultStorageError("RESULT_STORAGE_FILESYSTEM_FILE_MODE must be 0o600 or 0o640")
    return candidate


def write_temporary_payload_stream_hashed(
    directory: Path,
    payload: bytes,
    *,
    expected_digest: str | None = None,
    file_mode: int | None = None,
    os_module: Any = os,
) -> tuple[Path, str]:
    """Write a temporary file with restrictive mode and streaming SHA-256 verification."""
    mode = configured_filesystem_file_mode(file_mode)
    for _attempt in range(_FILESYSTEM_TEMP_ATTEMPTS):
        temporary_path = directory / f".django-ray-result-{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os_module.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
            )
        except FileExistsError:
            pass
        if descriptor is None:
            continue

        write_failed = False
        stream_hash = hashlib.sha256()
        written_bytes = 0
        try:
            if hasattr(os_module, "fchmod"):
                os_module.fchmod(descriptor, mode)
            with os_module.fdopen(descriptor, "wb") as handle:
                view = memoryview(payload)
                for offset in range(0, len(view), _FILESYSTEM_WRITE_CHUNK_BYTES):
                    chunk = view[offset : offset + _FILESYSTEM_WRITE_CHUNK_BYTES]
                    handle.write(chunk)
                    stream_hash.update(chunk)
                    written_bytes += len(chunk)
                handle.flush()
                os_module.fsync(handle.fileno())
        except OSError:
            write_failed = True
        digest = stream_hash.hexdigest()
        if (
            write_failed
            or written_bytes != len(payload)
            or (expected_digest is not None and digest != expected_digest)
        ):
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError("temporary result payload write failed")
        return temporary_path, digest
    raise ResultStorageError("Failed to persist result payload to filesystem storage")


def hard_link_is_unsupported(error: OSError) -> bool:
    """Return whether ``os.link`` failed because the filesystem lacks hard links."""
    unsupported_errnos = {
        errno.EACCES,
        errno.EPERM,
        errno.EXDEV,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
    return error.errno in unsupported_errnos or getattr(error, "winerror", None) in {1, 50}


def _reap_legacy_install_lock_dir(lock_dir: Path) -> None:
    """Remove an obsolete stale ``.install-lock`` directory from pre-0.8.1 crashes."""
    try:
        lock_stat = os.stat(lock_dir, follow_symlinks=False)
    except OSError:
        return
    if not stat.S_ISDIR(lock_stat.st_mode):
        return
    age_seconds = max(0.0, (time.time_ns() - lock_stat.st_mtime_ns) / 1_000_000_000.0)
    if age_seconds >= _FILESYSTEM_LEGACY_LOCK_STALE_SECONDS:
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def install_temporary_payload_atomic(
    temporary_path: Path,
    full_path: Path,
    *,
    os_module: Any = os,
    lock_timeout_seconds: float = _FILESYSTEM_INSTALL_LOCK_TIMEOUT_SECONDS,
) -> bool:
    """Atomically install ``temporary_path`` at ``full_path`` without overwriting.

    Returns ``True`` when this writer installed the file and ``False`` when an
    object already existed at ``full_path`` (requiring integrity verification).

    Uses lock-free atomic ``os.link`` on POSIX/NTFS filesystems so ``SIGKILL``
    or OOM cannot leave stale directory locks behind. When the filesystem does
    not support hard links, falls back to an OS-kernel-released ``FileLock``
    (``fcntl.flock`` / ``msvcrt.locking``) around a guarded ``os.replace``.
    """
    if full_path.exists():
        return False

    link_error: OSError | None = None
    try:
        os_module.link(temporary_path, full_path)
        return True
    except FileExistsError:
        return False
    except OSError as error:
        link_error = error

    if not hard_link_is_unsupported(link_error):
        raise OSError("filesystem result installation failed") from link_error

    legacy_lock_dir = full_path.with_name(f".{full_path.name}.install-lock")
    _reap_legacy_install_lock_dir(legacy_lock_dir)

    # Use a persistent per-directory lock file rather than unlinking an active
    # flock target in ``finally:``. Unlinking a POSIX lock file while waiters
    # hold open fds allows concurrent writers to lock distinct inodes and
    # breaks mutual exclusion on FUSE/NFS (and on FileLockTimeout would remove
    # the active holder's lock file).
    lock_file = full_path.parent / ".django-ray-install.lock"
    lock = FileLock(str(lock_file), timeout=lock_timeout_seconds)
    try:
        with lock:
            if full_path.exists():
                return False
            os_module.replace(temporary_path, full_path)
            return True
    except FileLockTimeout as error:
        raise OSError("filesystem result installation lock is unavailable") from error


def _read_lock_owner_metadata(owner_file: Path) -> dict[str, Any] | None:
    try:
        owner_stat = os.stat(owner_file, follow_symlinks=False)
        if not stat.S_ISREG(owner_stat.st_mode) or owner_stat.st_nlink > 1:
            return None
        parsed = json.loads(owner_file.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _stale_lock_coordinator_path(lock_dir: Path) -> Path:
    """Return the persistent kernel-released FileLock path serializing lock transitions."""
    return lock_dir.with_name(f"{lock_dir.name}.coord")


def _try_reclaim_stale_lock_dir_locked(
    lock_dir: Path,
    *,
    now_ns: int,
    stale_grace_seconds: float,
    pid_alive_fn: Callable[[int], bool],
    process_start_token_fn: Callable[[int], str],
) -> bool:
    """Reclaim ``lock_dir`` while the caller holds the kernel coordinator lock."""
    try:
        dir_stat = os.stat(lock_dir, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISDIR(dir_stat.st_mode):
        return False

    owner_file = lock_dir / _LOCK_OWNER_FILENAME
    owner = _read_lock_owner_metadata(owner_file)
    is_stale = False
    if owner is not None:
        pid = owner.get("creator_pid")
        start_token = owner.get("process_start_token")
        if type(pid) is int and pid > 0 and isinstance(start_token, str) and start_token:
            if not pid_alive_fn(pid):
                is_stale = True
            else:
                observed_token = process_start_token_fn(pid)
                if observed_token != start_token:
                    is_stale = True
    if not is_stale:
        age_seconds = max(0.0, (now_ns - dir_stat.st_mtime_ns) / 1_000_000_000.0)
        if owner is None and age_seconds >= stale_grace_seconds:
            is_stale = True
    if not is_stale:
        return False

    stale_reclaim_path = lock_dir.with_name(f"{lock_dir.name}.stale-{secrets.token_hex(8)}")
    try:
        os.rename(lock_dir, stale_reclaim_path)
    except OSError:
        return False
    try:
        shutil.rmtree(stale_reclaim_path, ignore_errors=True)
    except OSError:
        pass
    return True


def _try_reclaim_stale_lock_dir(
    lock_dir: Path,
    *,
    now_ns: int,
    stale_grace_seconds: float,
    pid_alive_fn: Callable[[int], bool],
    process_start_token_fn: Callable[[int], str],
    coordinator_timeout_seconds: float = 1.0,
) -> bool:
    """Reclaim ``lock_dir`` only when proven dead or past orphan grace under a kernel lock."""
    coord_lock = FileLock(
        str(_stale_lock_coordinator_path(lock_dir)),
        timeout=max(0.0, float(coordinator_timeout_seconds)),
        mode=0o600,
    )
    try:
        with coord_lock:
            return _try_reclaim_stale_lock_dir_locked(
                lock_dir,
                now_ns=now_ns,
                stale_grace_seconds=stale_grace_seconds,
                pid_alive_fn=pid_alive_fn,
                process_start_token_fn=process_start_token_fn,
            )
    except (FileLockTimeout, OSError):
        return False


def acquire_stale_safe_lock_dir(
    lock_dir: Path,
    *,
    timeout_seconds: float,
    stale_grace_seconds: float,
    pid_alive_fn: Callable[[int], bool],
    process_start_token_fn: Callable[[int], str],
    clock_ns: Callable[[], int] = time.time_ns,
    mkdir_fn: Callable[..., Any] = os.mkdir,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Path:
    """Acquire a cross-process directory lock with kernel-serialized stale recovery."""
    now = monotonic_fn()
    deadline = now + float(timeout_seconds)
    current_pid = os.getpid()
    use_kernel_coord = mkdir_fn is os.mkdir
    coord_path = _stale_lock_coordinator_path(lock_dir)
    reclaim_retries = 0
    while True:
        remaining = max(0.0, deadline - now)
        coord_ctx: Any = (
            FileLock(str(coord_path), timeout=remaining, mode=0o600)
            if use_kernel_coord
            else contextlib.nullcontext()
        )
        reclaimed = False
        try:
            with coord_ctx:
                acquired = False
                try:
                    mkdir_fn(lock_dir, mode=0o700)
                    acquired = True
                except FileExistsError:
                    reclaimed = _try_reclaim_stale_lock_dir_locked(
                        lock_dir,
                        now_ns=int(clock_ns()),
                        stale_grace_seconds=stale_grace_seconds,
                        pid_alive_fn=pid_alive_fn,
                        process_start_token_fn=process_start_token_fn,
                    )
                    if reclaimed:
                        try:
                            mkdir_fn(lock_dir, mode=0o700)
                            acquired = True
                        except FileExistsError:
                            acquired = False
                if acquired:
                    owner_payload = {
                        "creator_pid": current_pid,
                        "process_start_token": process_start_token_fn(current_pid),
                        "created_at_ns": int(clock_ns()),
                    }
                    owner_file = lock_dir / _LOCK_OWNER_FILENAME
                    try:
                        fd = os.open(owner_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            json.dump(owner_payload, handle, sort_keys=True, separators=(",", ":"))
                    except OSError:
                        pass
                    return lock_dir
        except FileLockTimeout:
            raise TimeoutError("Timed out acquiring cross-process lock directory") from None

        if reclaimed and reclaim_retries < 3:
            reclaim_retries += 1
            continue
        reclaim_retries = 0
        now = monotonic_fn()
        if now >= deadline:
            raise TimeoutError("Timed out acquiring cross-process lock directory") from None
        sleep_fn(0.005)


def release_stale_safe_lock_dir(lock_dir: Path) -> None:
    """Release a cross-process lock directory and its owner metadata atomically."""

    def _remove_dir() -> None:
        owner_file = lock_dir / _LOCK_OWNER_FILENAME
        try:
            owner_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if os.path.lexists(lock_dir):
                os.rmdir(lock_dir)
        except OSError:
            pass

    coord_lock = FileLock(str(_stale_lock_coordinator_path(lock_dir)), timeout=1.0, mode=0o600)
    try:
        with coord_lock:
            _remove_dir()
    except (FileLockTimeout, OSError):
        _remove_dir()


def get_or_create_s3_client(
    *,
    endpoint_url: str | None = None,
    region_name: str | None = None,
    connect_timeout: float = _DEFAULT_S3_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = _DEFAULT_S3_READ_TIMEOUT_SECONDS,
    import_module_fn: Callable[[str], Any] = importlib.import_module,
) -> Any:
    """Return a pooled boto3 S3 client with bounded timeouts when using stdlib import."""
    use_pool = import_module_fn is _ORIGINAL_IMPORT_MODULE
    pool_key = (endpoint_url, region_name, float(connect_timeout), float(read_timeout))
    if use_pool:
        with _S3_CLIENT_POOL_LOCK:
            cached = _S3_CLIENT_POOL.get(pool_key)
            if cached is not None:
                return cached

    boto3_module: Any | None = None
    import_failed = False
    try:
        boto3_module = import_module_fn("boto3")
    except ImportError:
        pass
    except Exception:
        import_failed = True
    if import_failed:
        raise ResultStorageError("Failed to initialize S3 result storage") from None
    if boto3_module is None:
        raise ResultStorageError(
            "S3 result storage requires boto3. Install with: pip install boto3"
        ) from None

    boto_config: Any | None = None
    try:
        botocore_config_module = import_module_fn("botocore.config")
        config_cls = getattr(botocore_config_module, "Config", None)
        if callable(config_cls):
            boto_config = config_cls(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retries={"max_attempts": _DEFAULT_S3_MAX_ATTEMPTS, "mode": "standard"},
            )
    except Exception:
        boto_config = None

    initialized_client: Any | None = None
    try:
        if boto_config is not None:
            try:
                initialized_client = boto3_module.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    region_name=region_name,
                    config=boto_config,
                )
            except TypeError:
                initialized_client = boto3_module.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    region_name=region_name,
                )
        else:
            initialized_client = boto3_module.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region_name,
            )
    except Exception:
        pass
    if initialized_client is None:
        raise ResultStorageError("Failed to initialize S3 result storage") from None

    if use_pool:
        with _S3_CLIENT_POOL_LOCK:
            return _S3_CLIENT_POOL.setdefault(pool_key, initialized_client)
    return initialized_client


def get_or_create_gcs_client(
    *,
    import_module_fn: Callable[[str], Any] = importlib.import_module,
) -> Any:
    """Return a pooled GCS storage.Client when using stdlib import."""
    use_pool = import_module_fn is _ORIGINAL_IMPORT_MODULE
    pool_key = "default"
    if use_pool:
        with _GCS_CLIENT_POOL_LOCK:
            cached = _GCS_CLIENT_POOL.get(pool_key)
            if cached is not None:
                return cached

    storage_module: Any | None = None
    import_failed = False
    try:
        storage_module = import_module_fn("google.cloud.storage")
    except ImportError:
        pass
    except Exception:
        import_failed = True
    if import_failed:
        raise ResultStorageError("Failed to initialize GCS result storage") from None
    if storage_module is None:
        raise ResultStorageError(
            "GCS result storage requires google-cloud-storage. "
            "Install with: pip install google-cloud-storage"
        ) from None

    initialized_client: Any | None = None
    try:
        initialized_client = storage_module.Client()
    except Exception:
        pass
    if initialized_client is None:
        raise ResultStorageError("Failed to initialize GCS result storage") from None

    if use_pool:
        with _GCS_CLIENT_POOL_LOCK:
            return _GCS_CLIENT_POOL.setdefault(pool_key, initialized_client)
    return initialized_client


def filesystem_namespaces_overlap(
    path_a: str,
    path_b: str,
    *,
    normpath_fn: Callable[[str], str] = os.path.normpath,
) -> bool:
    """Return whether two filesystem roots are identical or nested after realpath."""
    resolved_a = Path(os.path.normcase(os.path.abspath(os.path.realpath(normpath_fn(path_a)))))
    resolved_b = Path(os.path.normcase(os.path.abspath(os.path.realpath(normpath_fn(path_b)))))
    return (
        resolved_a == resolved_b
        or resolved_a.is_relative_to(resolved_b)
        or resolved_b.is_relative_to(resolved_a)
    )


def object_prefixes_overlap(prefix_a: str, prefix_b: str) -> bool:
    """Return whether two canonical object-key prefixes collide or nest."""
    clean_a = prefix_a.strip("/")
    clean_b = prefix_b.strip("/")
    if not clean_a or not clean_b:
        return True
    if clean_a == clean_b:
        return True
    return clean_a.startswith(f"{clean_b}/") or clean_b.startswith(f"{clean_a}/")


_blob_backend_resolver: Callable[[str, dict[str, Any] | None], Any] | None = None

_BUILTIN_BACKEND_NAMES = {
    "FilesystemResultStorage": "filesystem",
    "S3ResultStorage": "s3",
    "GCSResultStorage": "gcs",
}


def register_blob_backend_resolver(
    resolver: Callable[[str, dict[str, Any] | None], Any],
) -> None:
    """Register the reference-to-backend resolver owned by ``result_storage``."""
    global _blob_backend_resolver
    _blob_backend_resolver = resolver


def delete_blob_verified(
    backend_cfg: Mapping[str, Any] | Any | None,
    reference: str,
    *,
    ignore_missing: bool = True,
) -> BlobDeleteOutcome:
    """Delete one validated blob reference with metadata checks and 404 idempotency.

    Accepts either an already-initialized backend instance (supporting
    ``delete_verified`` or ``delete``) or a configuration mapping (or ``None``
    to resolve active Django settings via the registered backend resolver).
    """
    if backend_cfg is None or isinstance(backend_cfg, Mapping):
        if _blob_backend_resolver is None:
            raise ResultStorageError(
                "Blob backend resolver is not registered; import django_ray.result_storage first"
            )
        config = dict(backend_cfg) if isinstance(backend_cfg, Mapping) else None
        backend: Any = _blob_backend_resolver(reference, config)
    else:
        backend = backend_cfg

    delete_verified = getattr(backend, "delete_verified", None)
    if callable(delete_verified):
        return delete_verified(reference=reference, ignore_missing=ignore_missing)

    backend_type_name = type(backend).__name__
    if backend_type_name == "DigestResultStorage":
        backend.delete(reference=reference)
        return BlobDeleteOutcome(
            deleted=False,
            already_missing=False,
            reference=reference,
            backend="digest",
        )
    backend_name = _BUILTIN_BACKEND_NAMES.get(backend_type_name, "custom")
    backend.delete(reference=reference)
    return BlobDeleteOutcome(
        deleted=True,
        already_missing=False,
        reference=reference,
        backend=backend_name,
    )


__all__ = [
    "BlobDeleteOutcome",
    "ResultStorageError",
    "ResultStorageIntegrityError",
    "acquire_stale_safe_lock_dir",
    "clear_blob_client_caches",
    "configured_filesystem_file_mode",
    "delete_blob_verified",
    "filesystem_namespaces_overlap",
    "get_or_create_gcs_client",
    "get_or_create_s3_client",
    "hard_link_is_unsupported",
    "install_temporary_payload_atomic",
    "is_provider_not_found",
    "is_s3_conditional_delete_unsupported",
    "object_prefixes_overlap",
    "provider_error_markers",
    "provider_error_matches",
    "register_blob_backend_resolver",
    "release_stale_safe_lock_dir",
    "write_temporary_payload_stream_hashed",
]
