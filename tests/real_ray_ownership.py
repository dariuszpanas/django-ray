"""Host-wide advisory ownership for pytest sessions that run local Ray."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

LOCK_BYTE_OFFSET = 0
OWNER_METADATA_OFFSET = 1
MAX_OWNER_METADATA_BYTES = 2048
_MAX_METADATA_TEXT_JSON_BYTES = 512
_TEXT_METADATA_FIELDS = ("hostname", "acquired_at", "rootpath")

DEFAULT_REAL_RAY_LOCK_PATH = Path(tempfile.gettempdir()) / "django-ray-pytest-real-ray-owner.lock"


class RealRayOwnershipPathError(RuntimeError):
    """Raised when the shared ownership path cannot be opened without redirection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "real_ray ownership requires a stable, regular lock file; "
            f"refusing unsafe lock path {path}"
        )


def _bounded_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    def encoded_size(candidate: str) -> int:
        return len(json.dumps(candidate, ensure_ascii=True).encode("ascii"))

    if encoded_size(value) <= _MAX_METADATA_TEXT_JSON_BYTES:
        return value

    suffix = "..."
    lower = 0
    upper = len(value)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if encoded_size(f"{value[:midpoint]}{suffix}") <= _MAX_METADATA_TEXT_JSON_BYTES:
            lower = midpoint
        else:
            upper = midpoint - 1
    return f"{value[:lower]}{suffix}"


def _safe_owner_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    pid = metadata.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        safe["pid"] = pid
    selected_count = metadata.get("selected_count")
    if isinstance(selected_count, int) and not isinstance(selected_count, bool):
        safe["selected_count"] = selected_count
    for field_name in _TEXT_METADATA_FIELDS:
        value = _bounded_text(metadata.get(field_name))
        if value is not None:
            safe[field_name] = value
    return safe


def build_owner_metadata(*, rootpath: Path, selected_count: int) -> dict[str, object]:
    """Build bounded, allowlisted diagnostics for the current pytest owner."""
    return _safe_owner_metadata(
        {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "rootpath": str(rootpath),
            "selected_count": selected_count,
        }
    )


def _encode_owner_metadata(metadata: Mapping[str, object]) -> bytes:
    payload = json.dumps(
        _safe_owner_metadata(metadata),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > MAX_OWNER_METADATA_BYTES:  # pragma: no cover - field caps enforce this
        raise ValueError("real_ray owner metadata exceeds its bounded payload")
    return payload


def _read_owner_metadata(handle: BinaryIO) -> dict[str, object]:
    try:
        handle.seek(OWNER_METADATA_OFFSET)
        payload = handle.read(MAX_OWNER_METADATA_BYTES + 1)
    except OSError:
        return {}
    if not payload or len(payload) > MAX_OWNER_METADATA_BYTES:
        return {}
    try:
        decoded = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return _safe_owner_metadata(decoded)


def _try_advisory_lock(handle: BinaryIO) -> None:
    handle.seek(LOCK_BYTE_OFFSET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_advisory_lock(handle: BinaryIO) -> None:
    handle.seek(LOCK_BYTE_OFFSET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_windows_lock_descriptor(path: Path) -> int:
    """Open the final Windows path without traversing a reparse point."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_file_information = kernel32.GetFileInformationByHandle
    get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_file_information.restype = wintypes.BOOL
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [wintypes.HANDLE]
    get_file_type.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_always = 4
    file_attribute_normal = 0x00000080
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_flag_open_reparse_point = 0x00200000
    file_type_disk = 0x0001
    invalid_handle_value = ctypes.c_void_p(-1).value

    handle = create_file(
        str(path),
        generic_read | generic_write,
        file_share_read | file_share_write,
        None,
        open_always,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "Windows lock path open failed")

    descriptor: int | None = None
    try:
        information = _ByHandleFileInformation()
        if not get_file_information(handle, ctypes.byref(information)):
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "Windows lock path inspection failed")
        unsafe_attributes = file_attribute_directory | file_attribute_reparse_point
        if (
            information.dwFileAttributes & unsafe_attributes
            or get_file_type(handle) != file_type_disk
            or information.nNumberOfLinks != 1
        ):
            raise RealRayOwnershipPathError(path)

        descriptor_flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        descriptor = msvcrt.open_osfhandle(handle, descriptor_flags)
    finally:
        if descriptor is None:
            close_handle(handle)
    if descriptor is None:  # pragma: no cover - open_osfhandle either returns or raises
        raise RealRayOwnershipPathError(path)
    return descriptor


def _open_posix_lock_descriptor(path: Path) -> int:
    """Open the final POSIX path without following a symbolic link."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:  # pragma: no cover - supported POSIX platforms provide O_NOFOLLOW
        raise RealRayOwnershipPathError(path)
    flags = os.O_CREAT | os.O_RDWR | no_follow
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, 0o600)


def _validate_lock_descriptor(path: Path, descriptor: int) -> None:
    """Require one regular, stable path identity before any metadata write."""
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise RealRayOwnershipPathError(path) from error
    if (
        not stat.S_ISREG(descriptor_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or descriptor_stat.st_nlink != 1
        or path_stat.st_nlink != 1
        or not os.path.samestat(descriptor_stat, path_stat)
        or (os.name == "posix" and descriptor_stat.st_uid != os.geteuid())
    ):
        raise RealRayOwnershipPathError(path)


def _validate_lock_parent(path: Path) -> None:
    """Require a real parent that prevents foreign path replacement."""
    try:
        parent_stat = os.stat(path.parent, follow_symlinks=False)
    except OSError as error:
        raise RealRayOwnershipPathError(path) from error
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise RealRayOwnershipPathError(path)
    if os.name == "nt":
        if getattr(parent_stat, "st_file_attributes", 0) & 0x00000400:
            raise RealRayOwnershipPathError(path)
        return

    permissions = stat.S_IMODE(parent_stat.st_mode)
    trusted_owner = parent_stat.st_uid in {0, os.geteuid()}
    foreign_writable = bool(permissions & 0o022)
    sticky = bool(parent_stat.st_mode & stat.S_ISVTX)
    if not trusted_owner or (foreign_writable and not sticky):
        raise RealRayOwnershipPathError(path)


def _open_lock_descriptor(path: Path) -> int:
    try:
        descriptor = (
            _open_windows_lock_descriptor(path)
            if os.name == "nt"
            else _open_posix_lock_descriptor(path)
        )
    except RealRayOwnershipPathError:
        raise
    except OSError as error:
        raise RealRayOwnershipPathError(path) from error

    try:
        _validate_lock_descriptor(path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class RealRayOwnershipUnavailableError(RuntimeError):
    """Raised when another process owns the local-Ray pytest boundary."""

    def __init__(self, path: Path, owner: Mapping[str, object]) -> None:
        self.path = path
        self.owner = _safe_owner_metadata(owner)
        owner_summary = (
            json.dumps(self.owner, ensure_ascii=True, sort_keys=True)
            if self.owner
            else "unavailable"
        )
        super().__init__(
            "selected real_ray tests require exclusive host ownership; "
            f"lock {path} is held by another process; owner metadata: {owner_summary}"
        )


class RealRayOwnershipLock:
    """Own a stable lock file through an OS-released advisory lock."""

    def __init__(self, path: Path = DEFAULT_REAL_RAY_LOCK_PATH) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self, owner: Mapping[str, object]) -> None:
        if self._handle is not None:
            raise RuntimeError("real_ray ownership lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _validate_lock_parent(self.path)
        descriptor = _open_lock_descriptor(self.path)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            try:
                _try_advisory_lock(handle)
            except OSError as error:
                current_owner = _read_owner_metadata(handle)
                raise RealRayOwnershipUnavailableError(self.path, current_owner) from error

            _validate_lock_parent(self.path)
            _validate_lock_descriptor(self.path, descriptor)
            if os.fstat(descriptor).st_size < OWNER_METADATA_OFFSET:
                handle.seek(LOCK_BYTE_OFFSET)
                handle.write(b"\0")
            payload = _encode_owner_metadata(owner)
            handle.seek(OWNER_METADATA_OFFSET)
            handle.write(payload)
            handle.truncate()
            os.fsync(descriptor)
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _release_advisory_lock(handle)
        finally:
            handle.close()
