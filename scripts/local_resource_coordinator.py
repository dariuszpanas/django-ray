"""Daemonless host-wide coordination for django-ray local validation resources.

The first compatibility boundary in this module is the legacy real-Ray pytest
lock.  Its path, locked byte, metadata offset, open hardening, and nonblocking
API are intentionally stable so an older same-user worktree and a
coordinator-aware worktree remain mutually exclusive.  On POSIX the
coordinator's authority lives inside its private per-user state directory; a
proved foreign-user fixed legacy path is outside that cooperative boundary.
Exact contained-process custody is implemented only on Windows, Linux, and
Darwin; other POSIX systems fail before launch rather than deriving authority
from wall-clock process timestamps.

Phase 1 FIFO ordering covers coordinator-aware clients.  An unaware legacy
client observes only byte 0, so it cannot honor queued tickets or an orphan
record after the owning OS lock is released.  The private state directory and
POSIX process-group cleanup also use a cooperative same-user trust boundary;
self-daemonizing POSIX workloads can escape a process group and are unsupported.
Read-only status never grants termination authority.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, MappingProxyType
from typing import BinaryIO, Final, Literal, cast


def _windows_current_user_sid() -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    if not open_process_token(get_current_process(), token_query, ctypes.byref(token)):
        return None
    try:
        required = wintypes.DWORD()
        get_token_information(token, token_user, None, 0, ctypes.byref(required))
        if required.value == 0:
            return None
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token,
            token_user,
            buffer,
            required,
            ctypes.byref(required),
        ):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(_SidAndAttributes)).contents.Sid
        rendered = wintypes.LPWSTR()
        if not convert_sid(sid, ctypes.byref(rendered)):
            return None
        try:
            return rendered.value
        finally:
            local_free(ctypes.cast(rendered, wintypes.HLOCAL))
    finally:
        close_handle(token)


LOCK_BYTE_OFFSET = 0
OWNER_METADATA_OFFSET = 1
MAX_OWNER_METADATA_BYTES = 2048
_MAX_METADATA_TEXT_JSON_BYTES = 512
_MAX_STATUS_TEXT_JSON_BYTES = 512
_MAX_STATE_FILE_BYTES = 64 * 1024
_MAX_QUEUE_ITEMS = 128
_MAX_STATUS_QUEUE_ITEMS = 20
_QUEUE_POLL_SECONDS = 0.1
_MAX_PROCESS_ANCESTRY = 256
_MAX_POSIX_GROUP_MEMBERS = 65_536
_MAX_SELECTED_COUNT = 0x7FFFFFFF
_MAX_TICKET = (2**63) - 2
_CAPABILITY_TOKEN_BYTES = 32
_RUN_ID_BYTES = 16
_PROCESS_TREE_SETTLE_SECONDS = 2.0
_PROCESS_TREE_NATURAL_EXIT_SECONDS = 30.0
_PROCESS_TREE_SHUTDOWN_SECONDS = 10.0
_RUNNER_INTERNAL_FAILURE = 125
_GIT_DIRTY_OUTPUT_CAP_BYTES = 1
_MAX_AUTHORITY_PATH_CHARACTERS = 32_767
_MAX_AUTHORITY_PATH_BYTES = 32_768
_MAX_PID = 0xFFFFFFFF if os.name == "nt" else 0x7FFFFFFF
_TEXT_METADATA_FIELDS = ("hostname", "acquired_at", "rootpath")

GIT_ENVIRONMENT_KEYS: Final = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)

# Older worktrees know only this fixed path. It remains a compatibility lock,
# but it is not the coordinator's per-user authority on POSIX: a 0600 inode in
# a shared temporary directory may belong to an unrelated OS user.
DEFAULT_REAL_RAY_LOCK_PATH = Path(tempfile.gettempdir()) / ("django-ray-pytest-real-ray-owner.lock")
_WINDOWS_CURRENT_USER_SID: Final = _windows_current_user_sid()


def _default_local_resource_state_parent(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Select an OS-provisioned user path; validation remains fail closed."""

    selected_environment = os.environ if environment is None else environment
    if os.name == "posix":
        if home is not None:
            return home
        try:
            import pwd

            return Path(pwd.getpwuid(os.geteuid()).pw_dir)
        except (KeyError, OSError):
            # No stable account path means no safe Phase 1 authority domain.
            # The relative sentinel is rejected by _validate_state_parent.
            return Path()
    return Path(selected_environment.get("LOCALAPPDATA", tempfile.gettempdir()))


_STATE_DIR_PARENT = _default_local_resource_state_parent()
DEFAULT_LOCAL_RESOURCE_STATE_DIR = _STATE_DIR_PARENT / (
    "django-ray-local-resources"
    if os.name == "posix" or _WINDOWS_CURRENT_USER_SID is None
    else f"django-ray-local-resources-{_WINDOWS_CURRENT_USER_SID}"
)
ACTIVE_STATE_FILE = "active.json"
LAST_COMPLETED_STATE_FILE = "last-completed.json"
QUEUE_STATE_FILE = "queue.json"
QUEUE_LOCK_FILE = "queue.lock"
AUTHORITY_LOCK_FILE = "authority.lock"

LOCAL_RESOURCE_SCHEMA_VERSION: Final = 1
HOST_HEAVY_RESOURCE: Final = "host-heavy"
LOCAL_RESOURCE_PROFILES: Final = MappingProxyType(
    {
        "ci-final": (HOST_HEAVY_RESOURCE,),
        "kuberay-final": (HOST_HEAVY_RESOURCE,),
        "real-ray": (HOST_HEAVY_RESOURCE,),
    }
)
LocalResourceProfile = Literal["ci-final", "kuberay-final", "real-ray"]

# These values are authorization-bearing only after the coordinator validates
# them against its private registry.  Export the complete inventory so child
# launchers can remove local custody from Docker/Kubernetes environments.
LOCAL_RESOURCE_RUN_ID_ENV: Final = "DJANGO_RAY_LOCAL_RUN_ID"
LOCAL_RESOURCE_CAPABILITY_ENV: Final = "DJANGO_RAY_LOCAL_LEASE_TOKEN"
LOCAL_RESOURCE_PROFILE_ENV: Final = "DJANGO_RAY_LOCAL_PROFILE"
LOCAL_RESOURCE_STATE_DIR_ENV: Final = "DJANGO_RAY_LOCAL_STATE_DIR"
LOCAL_RESOURCE_INHERITANCE_ENV_KEYS: Final = (
    LOCAL_RESOURCE_RUN_ID_ENV,
    LOCAL_RESOURCE_CAPABILITY_ENV,
    LOCAL_RESOURCE_PROFILE_ENV,
    LOCAL_RESOURCE_STATE_DIR_ENV,
)

LOCAL_RESOURCE_OWNER_ENV: Final = "DJANGO_RAY_LOCAL_OWNER"
LOCAL_RESOURCE_SESSION_ENV: Final = "DJANGO_RAY_LOCAL_SESSION"
LOCAL_RESOURCE_AGENT_ENV: Final = "DJANGO_RAY_LOCAL_AGENT"
LOCAL_RESOURCE_MODEL_ENV: Final = "DJANGO_RAY_LOCAL_MODEL"
LOCAL_RESOURCE_INTENT_ENV: Final = "DJANGO_RAY_LOCAL_INTENT"
LOCAL_RESOURCE_HANDOFF_ENV: Final = "DJANGO_RAY_LOCAL_HANDOFF"
LOCAL_RESOURCE_ENV_KEYS: Final = LOCAL_RESOURCE_INHERITANCE_ENV_KEYS + (
    LOCAL_RESOURCE_OWNER_ENV,
    LOCAL_RESOURCE_SESSION_ENV,
    LOCAL_RESOURCE_AGENT_ENV,
    LOCAL_RESOURCE_MODEL_ENV,
    LOCAL_RESOURCE_INTENT_ENV,
    LOCAL_RESOURCE_HANDOFF_ENV,
)


class LocalResourceCoordinationError(RuntimeError):
    """Base class for bounded, user-safe local coordination failures."""


class LocalResourceStatePathError(LocalResourceCoordinationError):
    """Raised when the private coordinator state directory is unsafe."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "local resource coordination requires a private, stable state directory; "
            f"refusing unsafe state path {path}"
        )


class LocalResourceBusyError(LocalResourceCoordinationError):
    """Raised when a bounded wait cannot obtain the requested local resources."""

    def __init__(self, message: str = "local resources remain busy after the bounded wait") -> None:
        super().__init__(_bounded_status_text(message) or "local resources remain busy")


class LocalResourceInheritanceError(LocalResourceCoordinationError):
    """Raised when an inherited capability is incomplete, stale, or unauthorized."""


class LocalResourceStateError(LocalResourceCoordinationError):
    """Raised when bounded coordinator registry state cannot be trusted."""


class RealRayOwnershipPathError(LocalResourceCoordinationError):
    """Raised when the shared ownership path cannot be opened without redirection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "real_ray ownership requires a stable, regular lock file; "
            f"refusing unsafe lock path {path}"
        )


def _json_encoded_size(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=True).encode("ascii"))


def _bounded_text(value: object) -> str | None:
    """Retain the exact legacy metadata truncation contract."""

    if not isinstance(value, str):
        return None
    if _json_encoded_size(value) <= _MAX_METADATA_TEXT_JSON_BYTES:
        return value

    suffix = "..."
    lower = 0
    upper = len(value)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if _json_encoded_size(f"{value[:midpoint]}{suffix}") <= (_MAX_METADATA_TEXT_JSON_BYTES):
            lower = midpoint
        else:
            upper = midpoint - 1
    return f"{value[:lower]}{suffix}"


def _bounded_status_text(value: object) -> str | None:
    """Return bounded printable ASCII diagnostics without interpreting contents."""

    if not isinstance(value, str):
        return None
    printable = "".join(character if 0x20 <= ord(character) <= 0x7E else "?" for character in value)
    if _json_encoded_size(printable) <= _MAX_STATUS_TEXT_JSON_BYTES:
        return printable

    suffix = "..."
    lower = 0
    upper = len(printable)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if _json_encoded_size(f"{printable[:midpoint]}{suffix}") <= (_MAX_STATUS_TEXT_JSON_BYTES):
            lower = midpoint
        else:
            upper = midpoint - 1
    return f"{printable[:lower]}{suffix}"


def _validated_process_birth(value: object) -> str:
    """Return an exact authority identity; never sanitize or truncate it."""

    bounded = _bounded_status_text(value)
    if not isinstance(value, str) or not value or bounded != value:
        raise LocalResourceStateError("local resource process birth identity is invalid")
    return value


def _safe_pid(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _MAX_PID:
        return value
    return None


@dataclass(frozen=True, slots=True)
class LocalResourceOwner:
    """Bounded, allowlisted diagnostic identity; never takeover authority."""

    owner: str | None = None
    session: str | None = None
    agent: str | None = None
    model: str | None = None
    host_id: str | None = None
    pid: int | None = None
    process_birth: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("owner", "session", "agent", "model", "host_id", "process_birth"):
            object.__setattr__(self, field_name, _bounded_status_text(getattr(self, field_name)))
        object.__setattr__(self, "pid", _safe_pid(self.pid))

    def as_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "session": self.session,
            "agent": self.agent,
            "model": self.model,
            "host_id": self.host_id,
            "pid": self.pid,
            "process_birth": self.process_birth,
        }


@dataclass(frozen=True, slots=True)
class LocalResourceSource:
    """Bounded source-tree diagnostics for one local validation request."""

    worktree: str | None = None
    branch: str | None = None
    commit: str | None = None
    source_tree: str | None = None
    dirty: bool | None = None

    def __post_init__(self) -> None:
        for field_name in ("worktree", "branch", "commit", "source_tree"):
            object.__setattr__(self, field_name, _bounded_status_text(getattr(self, field_name)))
        if not isinstance(self.dirty, bool):
            object.__setattr__(self, "dirty", None)

    def as_dict(self) -> dict[str, object]:
        return {
            "worktree": self.worktree,
            "branch": self.branch,
            "commit": self.commit,
            "source_tree": self.source_tree,
            "dirty": self.dirty,
        }


def _stable_host_id() -> str:
    user_scope = str(os.geteuid()) if os.name == "posix" else os.environ.get("USERNAME", "user")
    material = f"{socket.gethostname()}\0{user_scope}".encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(material).hexdigest()[:16]


def _windows_process_birth(pid: int) -> str | None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not get_process_times(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        value = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return f"windows-filetime:{value}"
    finally:
        close_handle(handle)


@dataclass(frozen=True, slots=True)
class _PosixProcessSnapshot:
    parent_pid: int
    process_group: int
    session: int | None
    start_identity: str


@dataclass(frozen=True, slots=True)
class _LinuxProcStat:
    parent_pid: int
    process_group: int
    session: int
    start_ticks: str
    state: str


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_DARWIN_PROC_PIDTBSDINFO = 3
_DARWIN_PROC_BSD_INFO_SIZE = 136


@dataclass(frozen=True, slots=True)
class _DarwinProcIdentity:
    parent_pid: int
    process_group: int
    start_seconds: int
    start_microseconds: int


def _linux_boot_identity() -> str | None:
    try:
        payload = (Path("/proc") / "sys" / "kernel" / "random" / "boot_id").read_text(
            encoding="ascii"
        )
        parsed = uuid.UUID(payload.strip())
    except (OSError, UnicodeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if payload.strip().lower() == canonical else None


def _linux_proc_stat(pid: int) -> _LinuxProcStat | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        payload = stat_path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    closing = payload.rfind(")")
    if closing < 0:
        return None
    fields = payload[closing + 2 :].split()
    # The tail begins at field 3 (state).  Parent, process group, session,
    # and starttime (field 22) are therefore indexes 1, 2, 3, and 19.
    if (
        len(fields) <= 19
        or len(fields[0]) != 1
        or not fields[0].isascii()
        or not fields[0].isalpha()
        or any(not fields[index].isdigit() for index in (1, 2, 3, 19))
    ):
        return None
    parent_pid = int(fields[1])
    process_group = int(fields[2])
    session = int(fields[3])
    if (
        not 0 <= parent_pid <= _MAX_PID
        or not 0 <= process_group <= _MAX_PID
        or not 0 <= session <= _MAX_PID
    ):
        return None
    return _LinuxProcStat(
        parent_pid=parent_pid,
        process_group=process_group,
        session=session,
        start_ticks=fields[19],
        state=fields[0],
    )


def _linux_process_snapshot(pid: int) -> _PosixProcessSnapshot | None:
    stat = _linux_proc_stat(pid)
    if stat is None or stat.process_group <= 0 or stat.session <= 0:
        return None
    boot_identity = _linux_boot_identity()
    if boot_identity is None:
        return None
    return _PosixProcessSnapshot(
        parent_pid=stat.parent_pid,
        process_group=stat.process_group,
        session=stat.session,
        start_identity=f"linux-boot:{boot_identity}:proc-start-ticks:{stat.start_ticks}",
    )


def _load_darwin_libproc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None


def _load_darwin_libc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None


def _darwin_boot_identity() -> str | None:
    libc = _load_darwin_libc()
    if libc is None:
        return None
    try:
        sysctlbyname = libc.sysctlbyname
    except AttributeError:
        return None
    sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    sysctlbyname.restype = ctypes.c_int
    name = b"kern.bootsessionuuid"
    size = ctypes.c_size_t()
    try:
        result = sysctlbyname(name, None, ctypes.byref(size), None, 0)
    except (OSError, ctypes.ArgumentError):
        return None
    if result != 0 or not 1 < size.value <= 128:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    try:
        result = sysctlbyname(name, ctypes.byref(buffer), ctypes.byref(size), None, 0)
    except (OSError, ctypes.ArgumentError):
        return None
    if result != 0 or not 1 < size.value <= len(buffer):
        return None
    payload = bytes(buffer.raw[: size.value])
    if not payload.endswith(b"\0"):
        return None
    try:
        text = payload[:-1].decode("ascii")
        parsed = uuid.UUID(text)
    except (UnicodeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if text.lower() == canonical else None


def _darwin_proc_identity(pid: int) -> _DarwinProcIdentity | None:
    libproc = _load_darwin_libproc()
    if libproc is None:
        return None
    try:
        proc_pidinfo = libproc.proc_pidinfo
    except AttributeError:
        return None
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _DarwinProcBsdInfo()
    size = ctypes.sizeof(info)
    if size != _DARWIN_PROC_BSD_INFO_SIZE:
        return None
    try:
        returned = proc_pidinfo(
            pid,
            _DARWIN_PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            size,
        )
    except (OSError, ctypes.ArgumentError):
        return None
    parent_pid = int(info.pbi_ppid)
    process_group = int(info.pbi_pgid)
    seconds = int(info.pbi_start_tvsec)
    microseconds = int(info.pbi_start_tvusec)
    if (
        returned != size
        or int(info.pbi_pid) != pid
        or not 0 <= parent_pid <= _MAX_PID
        or not 0 < process_group <= _MAX_PID
        or seconds <= 0
        or not 0 <= microseconds < 1_000_000
    ):
        return None
    return _DarwinProcIdentity(
        parent_pid=parent_pid,
        process_group=process_group,
        start_seconds=seconds,
        start_microseconds=microseconds,
    )


def _darwin_process_snapshot(pid: int) -> _PosixProcessSnapshot | None:
    before = _darwin_proc_identity(pid)
    boot_identity = _darwin_boot_identity()
    try:
        process_group = os.getpgid(pid)
        session = os.getsid(pid)
    except OSError:
        return None
    after = _darwin_proc_identity(pid)
    if (
        before is None
        or after is None
        or before != after
        or before.process_group != process_group
        or not 0 < session <= _MAX_PID
        or boot_identity is None
    ):
        return None
    return _PosixProcessSnapshot(
        parent_pid=before.parent_pid,
        process_group=before.process_group,
        session=session,
        start_identity=(
            f"darwin-boot:{boot_identity}:process-start:"
            f"{before.start_seconds}:{before.start_microseconds}"
        ),
    )


def _posix_process_snapshot(pid: int) -> _PosixProcessSnapshot | None:
    if sys.platform == "darwin":
        return _darwin_process_snapshot(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_snapshot(pid)
    return None


def _posix_process_birth(pid: int) -> str | None:
    snapshot = _posix_process_snapshot(pid)
    return snapshot.start_identity if snapshot is not None else None


def _process_birth(pid: int) -> str | None:
    if pid <= 0:
        return None
    return _windows_process_birth(pid) if os.name == "nt" else _posix_process_birth(pid)


def _pid_presence(pid: int) -> Literal["present", "absent", "unknown"]:
    if not 0 < pid <= _MAX_PID:
        return "absent"
    if pid == os.getpid():
        return "present"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        still_active = 259
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == 87:  # ERROR_INVALID_PARAMETER: no such process.
                return "absent"
            return "unknown"
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return "unknown"
            return "present" if exit_code.value == still_active else "absent"
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "present"
    except OSError:
        return "unknown"
    return "present"


def _process_liveness(pid: object, expected_birth: object) -> Literal["live", "dead", "unknown"]:
    safe_pid = _safe_pid(pid)
    if safe_pid is None:
        return "unknown"
    presence = _pid_presence(safe_pid)
    if presence == "absent":
        return "dead"
    if presence == "unknown":
        return "unknown"
    if not isinstance(expected_birth, str) or not expected_birth:
        return "unknown"
    observed_birth = _process_birth(safe_pid)
    if observed_birth is None:
        return "unknown"
    return "live" if hmac.compare_digest(observed_birth, expected_birth) else "dead"


def _posix_parent_pid(pid: int) -> int | None:
    snapshot = _posix_process_snapshot(pid)
    return snapshot.parent_pid if snapshot is not None else None


def _windows_parent_snapshot() -> dict[int, int] | None:
    if os.name != "nt":  # pragma: no cover - guarded by caller
        return None
    import ctypes
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not process_first(snapshot, ctypes.byref(entry)):
            return None
        parents: dict[int, int] = {}
        while True:
            if len(parents) >= 65_536:
                return None
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            entry.dwSize = ctypes.sizeof(entry)
            if not process_next(snapshot, ctypes.byref(entry)):
                break
        return parents
    finally:
        close_handle(snapshot)


def _process_is_descendant(
    pid: object,
    ancestor_pid: object,
    ancestor_birth: object,
) -> Literal["yes", "no", "unknown"]:
    """Prove ancestry while binding the authority-bearing ancestor to its birth."""

    current = _safe_pid(pid)
    ancestor = _safe_pid(ancestor_pid)
    if current is None or ancestor is None or not isinstance(ancestor_birth, str):
        return "unknown"
    if _process_liveness(ancestor, ancestor_birth) != "live":
        return "unknown"
    if current == ancestor:
        return "yes"

    parents = _windows_parent_snapshot() if os.name == "nt" else None
    if os.name == "nt" and parents is None:
        return "unknown"
    visited: set[int] = set()
    for _ in range(_MAX_PROCESS_ANCESTRY):
        if current in visited:
            return "unknown"
        visited.add(current)
        parent = parents.get(current) if parents is not None else _posix_parent_pid(current)
        if parent is None:
            return "unknown"
        if parent == ancestor:
            return "yes" if _process_liveness(ancestor, ancestor_birth) == "live" else "unknown"
        if parent <= 1:
            return "no"
        current = parent
    return "unknown"


def _posix_process_group_presence(pgid: object) -> Literal["present", "absent", "unknown"]:
    safe_pgid = _safe_pid(pgid)
    if safe_pgid is None or os.name != "posix":
        return "unknown"
    try:
        os.killpg(safe_pgid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "present"
    except OSError:
        return "unknown"
    return "present"


def _linux_posix_process_group_members(
    pgid: int,
    *,
    retain_zombie_leader: bool = True,
) -> frozenset[int] | None:
    members: set[int] = set()
    examined = 0
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                examined += 1
                if examined > _MAX_POSIX_GROUP_MEMBERS:
                    return None
                pid = int(entry.name)
                if not 0 < pid <= _MAX_PID:
                    return None
                stat = _linux_proc_stat(pid)
                if stat is None:
                    if _pid_presence(pid) == "absent":
                        continue
                    return None
                if stat.process_group != pgid:
                    continue
                if stat.session != pgid:
                    return None
                # Keep the intentionally unreaped session leader as exact
                # custody, but a terminated descendant can remain adopted by
                # a PID 1 that does not promptly reap it.  Such a zombie is no
                # longer executable and therefore is not a tree survivor.
                if stat.state == "Z" and (pid != pgid or not retain_zombie_leader):
                    continue
                members.add(pid)
    except OSError:
        return None
    return frozenset(members)


def _darwin_posix_process_group_members(
    pgid: int,
    *,
    retain_zombie_leader: bool = True,
) -> frozenset[int] | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        pids = psutil.pids()
    except psutil.Error:
        return None
    if len(pids) > _MAX_POSIX_GROUP_MEMBERS:
        return None
    members: set[int] = set()
    for pid in pids:
        if pid == 0:
            continue
        if not 0 < pid <= _MAX_PID:
            return None
        try:
            process_group = os.getpgid(pid)
        except ProcessLookupError:
            continue
        except OSError:
            return None
        if process_group != pgid:
            continue
        before = _darwin_proc_identity(pid)
        if before is None:
            if _pid_presence(pid) == "absent":
                continue
            return None
        try:
            session = os.getsid(pid)
            process_status = (
                None if pid == pgid and retain_zombie_leader else psutil.Process(pid).status()
            )
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (OSError, psutil.Error):
            return None
        after = _darwin_proc_identity(pid)
        if after is None:
            if _pid_presence(pid) == "absent":
                continue
            return None
        if before != after or before.process_group != process_group or session != pgid:
            return None
        if process_status == psutil.STATUS_ZOMBIE:
            continue
        members.add(pid)
    return frozenset(members)


def _posix_process_group_members(
    pgid: int,
    *,
    retain_zombie_leader: bool = True,
) -> frozenset[int] | None:
    if sys.platform.startswith("linux"):
        return _linux_posix_process_group_members(
            pgid,
            retain_zombie_leader=retain_zombie_leader,
        )
    if sys.platform == "darwin":
        return _darwin_posix_process_group_members(
            pgid,
            retain_zombie_leader=retain_zombie_leader,
        )
    return None


def _orphaned_posix_process_group_presence(
    pgid: int,
) -> Literal["present", "absent", "unknown"]:
    """Classify executable members after the authoritative owner is gone."""

    presence = _posix_process_group_presence(pgid)
    if presence != "present":
        # This helper is used only after the exact recorded leader identity
        # was observed live.  A missing or unreadable numeric group therefore
        # contradicts the durable custody record; it is not proof that the
        # live child settled and must remain fail closed.
        return "unknown"
    for _attempt in range(2):
        members = _posix_process_group_members(
            pgid,
            retain_zombie_leader=False,
        )
        if members is None:
            return "unknown"
        if members:
            return "present"
    return "absent"


def _posix_process_group_survivor_presence(
    pgid: int,
    *,
    leader_pid: int,
    leader_reaped: bool = False,
) -> Literal["present", "absent", "unknown"]:
    presence = _posix_process_group_presence(pgid)
    if presence != "present":
        return presence
    # Before reaping, the exact leader makes the group continuous and
    # authority-bearing.  After reaping, a raw group-presence probe can remain
    # positive for adopted zombies, so repeat the same read-only executable-
    # membership proof.  Any member, including a reused numeric leader, keeps
    # the result present.  Two bounded snapshots avoid treating one raced
    # process scan as absence.
    for _attempt in range(2):
        members = _posix_process_group_members(pgid)
        if members is None:
            return "unknown"
        if leader_reaped:
            if members:
                return "present"
        elif leader_pid not in members:
            return "unknown"
        elif members - {leader_pid}:
            return "present"
    return "absent"


def _signal_exact_posix_process_group(
    pid: object,
    expected_birth: object,
    signal_number: int,
) -> None:
    """Signal a group only while its exact recorded session leader is live."""

    leader = _safe_pid(pid)
    if leader is None or not isinstance(expected_birth, str) or not expected_birth:
        raise LocalResourceStateError("contained POSIX process group has no exact leader identity")
    if _process_liveness(leader, expected_birth) != "live":
        raise LocalResourceStateError(
            "contained POSIX process group leader identity is no longer exact"
        )
    try:
        process_group = os.getpgid(leader)
        session = os.getsid(leader)
    except OSError as error:
        raise LocalResourceStateError(
            "contained POSIX process group leader custody cannot be proved"
        ) from error
    if process_group != leader or session != leader:
        raise LocalResourceStateError(
            "contained POSIX process group leader custody no longer matches"
        )
    # Revalidate after the group/session lookup.  A departed or reused leader
    # makes the numeric PGID diagnostic-only; leaving survivors orphaned and
    # blocking is safer than signalling a group whose authority is ambiguous.
    if _process_liveness(leader, expected_birth) != "live":
        raise LocalResourceStateError(
            "contained POSIX process group leader identity changed before signalling"
        )
    try:
        os.killpg(leader, signal_number)
    except ProcessLookupError:
        return
    except OSError as error:
        raise LocalResourceStateError(
            "contained POSIX process group could not be signalled safely"
        ) from error


def _child_record_liveness(
    value: Mapping[str, object],
    *,
    owner_lock_held: bool = True,
) -> Literal["live", "dead", "unknown"]:
    direct = _process_liveness(value.get("pid"), value.get("process_birth"))
    tree_kind = value.get("tree_kind")
    if tree_kind == "windows-job":
        return direct
    if tree_kind != "posix-process-group":
        return "unknown"
    if direct == "live":
        if owner_lock_held:
            return "live"
        leader = _safe_pid(value.get("pid"))
        if leader is None:
            return "unknown"
        orphaned_presence = _orphaned_posix_process_group_presence(leader)
        if orphaned_presence == "present":
            return "live"
        return "dead" if orphaned_presence == "absent" else "unknown"
    if direct != "dead":
        return "unknown"
    leader = _safe_pid(value.get("pid"))
    if leader is None:
        return "unknown"
    survivors = _posix_process_group_survivor_presence(
        leader,
        leader_pid=leader,
        leader_reaped=True,
    )
    if survivors == "present":
        return "live"
    return "dead" if survivors == "absent" else "unknown"


def _git_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    inheritance = frozenset(LOCAL_RESOURCE_INHERITANCE_ENV_KEYS)
    for key, value in os.environ.items():
        normalized = key.upper()
        if (
            normalized in inheritance
            or normalized in GIT_ENVIRONMENT_KEYS
            or normalized.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        ):
            continue
        environment[key] = value
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _git_value(rootpath: Path, *args: str) -> str | None:
    with tempfile.TemporaryFile() as output:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=rootpath,
                check=False,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=10,
                env=_git_environment(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        output.seek(0)
        payload = output.read(_MAX_STATUS_TEXT_JSON_BYTES + 1)
    if len(payload) > _MAX_STATUS_TEXT_JSON_BYTES:
        return None
    return _bounded_status_text(payload.decode("utf-8", errors="replace").strip())


def _git_dirty(rootpath: Path) -> bool | None:
    """Detect the first porcelain byte without spooling an unbounded path list."""

    try:
        process = subprocess.Popen(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=rootpath,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(),
            close_fds=True,
        )
    except OSError:
        return None
    output = process.stdout
    if output is None:  # pragma: no cover - Popen was constructed with a pipe
        try:
            process.kill()
        except OSError:
            pass
        return None

    observed: list[bytes | BaseException] = []

    def read_first_byte() -> None:
        try:
            observed.append(output.read(_GIT_DIRTY_OUTPUT_CAP_BYTES))
        except BaseException as error:  # pragma: no cover - defensive pipe failure
            observed.append(error)

    reader = threading.Thread(target=read_first_byte, daemon=True)
    reader.start()
    reader.join(timeout=10)
    timed_out = reader.is_alive()
    first = observed[0] if observed else b""
    if timed_out or isinstance(first, bytes) and first:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            output.close()
            return None
    output.close()
    reader.join(timeout=1)
    if timed_out or reader.is_alive() or not observed or isinstance(observed[0], BaseException):
        return None
    first = observed[0]
    if first:
        return True
    return False if process.returncode == 0 else None


def _source_identity(rootpath: Path) -> LocalResourceSource:
    resolved = rootpath.resolve()
    branch = _git_value(resolved, "branch", "--show-current")
    commit = _git_value(resolved, "rev-parse", "--verify", "HEAD^{commit}")
    source_tree = (
        _git_value(resolved, "rev-parse", "--verify", f"{commit}^{{tree}}") if commit else None
    )
    return LocalResourceSource(
        worktree=str(resolved),
        branch=branch or None,
        commit=commit,
        source_tree=source_tree,
        dirty=_git_dirty(resolved),
    )


def _first_environment_text(*names: str) -> str | None:
    for name in names:
        value = _bounded_status_text(os.environ.get(name))
        if value:
            return value
    return None


def _current_owner() -> LocalResourceOwner:
    """Build bounded diagnostic identity independently of lease authority."""

    pid = os.getpid()
    process_birth = _process_birth(pid)
    if process_birth is None:
        raise LocalResourceStateError("current process birth cannot be established safely")
    return LocalResourceOwner(
        owner=_first_environment_text(
            LOCAL_RESOURCE_OWNER_ENV,
            "USER",
            "USERNAME",
        ),
        session=_first_environment_text(
            LOCAL_RESOURCE_SESSION_ENV,
            "CODEX_THREAD_ID",
            "CODEX_SESSION_ID",
            "GITHUB_RUN_ID",
        ),
        agent=_first_environment_text(
            LOCAL_RESOURCE_AGENT_ENV,
            "CODEX_AGENT_ID",
            "CODEX_TASK_PATH",
        ),
        model=_first_environment_text(
            LOCAL_RESOURCE_MODEL_ENV,
            "CODEX_MODEL",
        ),
        host_id=_stable_host_id(),
        pid=pid,
        process_birth=process_birth,
    )


def _request_diagnostics(profile: str) -> tuple[str, str | None]:
    intent = _first_environment_text(LOCAL_RESOURCE_INTENT_ENV)
    handoff = _first_environment_text(LOCAL_RESOURCE_HANDOFF_ENV)
    return intent or f"{profile} local validation", handoff


def _resolved_rootpath(rootpath: Path | str) -> Path:
    try:
        resolved = Path(rootpath).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LocalResourceStateError("local resource root path is unavailable") from error
    if not resolved.is_dir():
        raise LocalResourceStateError("local resource root path is not a directory")
    return resolved


def _validated_authority_path_text(value: object) -> str:
    """Preserve Unicode path authority while rejecting ambiguous env payloads."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_AUTHORITY_PATH_CHARACTERS
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise LocalResourceInheritanceError("inherited local resource state path is invalid")
    try:
        encoded = os.fsencode(value)
    except UnicodeEncodeError as error:
        raise LocalResourceInheritanceError(
            "inherited local resource state path is invalid"
        ) from error
    if not encoded or len(encoded) > _MAX_AUTHORITY_PATH_BYTES:
        raise LocalResourceInheritanceError("inherited local resource state path is invalid")
    return value


def _rootpath_digest(rootpath: Path) -> str:
    material = str(rootpath).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(material).hexdigest()


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


def _open_windows_lock_descriptor(path: Path, *, create: bool = True) -> int:
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
    open_existing = 3
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
        open_always if create else open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        if not create and error_code in {2, 3}:
            raise FileNotFoundError(error_code, "Windows lock path does not exist")
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


def _open_posix_lock_descriptor(path: Path, *, create: bool = True) -> int:
    """Open the final POSIX path without following a symbolic link."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:  # pragma: no cover - supported POSIX platforms provide O_NOFOLLOW
        raise RealRayOwnershipPathError(path)
    flags = os.O_RDWR | no_follow
    if create:
        flags |= os.O_CREAT
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


def _local_resource_authority_lock_path(*, state_dir: Path, legacy_lock_path: Path) -> Path:
    """Return the stable per-user authority while preserving Windows's user temp path."""

    if os.name == "posix":
        return state_dir / AUTHORITY_LOCK_FILE
    return legacy_lock_path


def _legacy_compatibility_is_current_user(path: Path) -> bool:
    """Treat a proved foreign POSIX inode as outside the cooperative boundary."""

    _validate_lock_parent(path)
    if os.name != "posix":
        return True
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError as error:
        raise RealRayOwnershipPathError(path) from error
    return path_stat.st_uid == os.geteuid()


def _open_lock_descriptor(path: Path, *, create: bool = True) -> int:
    try:
        descriptor = (
            _open_windows_lock_descriptor(path, create=create)
            if os.name == "nt"
            else _open_posix_lock_descriptor(path, create=create)
        )
    except (FileNotFoundError, RealRayOwnershipPathError):
        raise
    except OSError as error:
        raise RealRayOwnershipPathError(path) from error

    try:
        _validate_lock_descriptor(path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class RealRayOwnershipUnavailableError(LocalResourceCoordinationError):
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
    """Own one authority lock and an optional same-user legacy lock."""

    def __init__(
        self,
        path: Path = DEFAULT_REAL_RAY_LOCK_PATH,
        *,
        compatibility_path: Path | None = None,
    ) -> None:
        self.path = path
        self._compatibility_path = compatibility_path if compatibility_path != path else None
        self._handle: BinaryIO | None = None
        self._compatibility_handle: BinaryIO | None = None
        self._handle_unlock_started = False
        self._compatibility_unlock_started = False

    @property
    def acquired(self) -> bool:
        return self._handle is not None or self._compatibility_handle is not None

    @staticmethod
    def _acquire_path(path: Path, owner: Mapping[str, object]) -> BinaryIO:
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_lock_parent(path)
        descriptor = _open_lock_descriptor(path)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            try:
                _try_advisory_lock(handle)
            except OSError as error:
                if not _lock_contention(error):
                    raise LocalResourceStateError(
                        "real_ray ownership lock could not be acquired safely"
                    ) from error
                current_owner = _read_owner_metadata(handle)
                raise RealRayOwnershipUnavailableError(path, current_owner) from error

            _validate_lock_parent(path)
            _validate_lock_descriptor(path, descriptor)
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
        return handle

    def acquire(self, owner: Mapping[str, object]) -> None:
        if self.acquired:
            raise RuntimeError("real_ray ownership lock is already acquired")
        authority_handle = self._acquire_path(self.path, owner)
        compatibility_handle: BinaryIO | None = None
        try:
            compatibility_path = self._compatibility_path
            if compatibility_path is not None and _legacy_compatibility_is_current_user(
                compatibility_path
            ):
                try:
                    compatibility_handle = self._acquire_path(compatibility_path, owner)
                except RealRayOwnershipPathError:
                    # A foreign user may win the absent-path race in a sticky
                    # shared temp directory. Reclassify only when ownership is
                    # now proved foreign; same-user path failures remain fatal.
                    if _legacy_compatibility_is_current_user(compatibility_path):
                        raise
        except BaseException:
            try:
                _release_advisory_lock(authority_handle)
            finally:
                authority_handle.close()
            raise
        self._handle = authority_handle
        self._compatibility_handle = compatibility_handle
        self._handle_unlock_started = False
        self._compatibility_unlock_started = False

    def _settle_handle(
        self,
        *,
        handle_attribute: str,
        unlock_attribute: str,
    ) -> BaseException | None:
        handle = cast(BinaryIO | None, getattr(self, handle_attribute))
        if handle is None:
            return None
        if handle.closed:
            setattr(self, handle_attribute, None)
            setattr(self, unlock_attribute, False)
            return None

        release_error: BaseException | None = None
        if not cast(bool, getattr(self, unlock_attribute)):
            # Consume the unlock attempt before its native side effect. If an
            # asynchronous exception arrives after the unlock commits, a retry
            # closes the same retained descriptor without repeating unlock on
            # an already-unlocked byte.
            setattr(self, unlock_attribute, True)
            try:
                _release_advisory_lock(handle)
            except BaseException as error:
                release_error = error
        try:
            handle.close()
        except BaseException as error:
            if release_error is None:
                release_error = error

        if handle.closed:
            setattr(self, handle_attribute, None)
            setattr(self, unlock_attribute, False)
        elif release_error is None:
            release_error = LocalResourceStateError(
                "real_ray ownership lock close did not settle its descriptor"
            )
        return release_error

    def release(self) -> None:
        if not self.acquired:
            return
        release_error: BaseException | None = None
        try:
            # Defer a real Ctrl-C across the whole composite transition so the
            # compatibility cleanup cannot prevent authoritative settlement.
            # Replaying into an outer lifecycle guard is safe: that guard will
            # latch cancellation while its own cleanup remains in progress.
            with _deferred_sigint_state_descriptor_transition():
                for handle_attribute, unlock_attribute in (
                    ("_compatibility_handle", "_compatibility_unlock_started"),
                    ("_handle", "_handle_unlock_started"),
                ):
                    try:
                        error = self._settle_handle(
                            handle_attribute=handle_attribute,
                            unlock_attribute=unlock_attribute,
                        )
                    except BaseException as caught:
                        error = caught
                    if release_error is None and error is not None:
                        release_error = error
        except BaseException as caught:
            if release_error is None:
                release_error = caught
        if release_error is not None:
            raise release_error


def _windows_sid_pointer(sid: str):
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert_sid = advapi32.ConvertStringSidToSidW
    convert_sid.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
    convert_sid.restype = wintypes.BOOL
    pointer = wintypes.LPVOID()
    if not convert_sid(sid, ctypes.byref(pointer)):
        return None
    return pointer


def _windows_acl_is_safe(path: Path, *, expected_sid: str, require_private: bool) -> bool:
    """Validate a directory DACL without granting authority from display names."""

    if os.name != "nt":  # pragma: no cover - guarded by Windows callers
        return False
    import ctypes
    from ctypes import wintypes

    class _Acl(ctypes.Structure):
        _fields_ = [
            ("AclRevision", wintypes.BYTE),
            ("Sbz1", wintypes.BYTE),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_named_security = advapi32.GetNamedSecurityInfoW
    get_named_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    get_named_security.restype = wintypes.DWORD
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_control.restype = wintypes.BOOL
    get_ace = advapi32.GetAce
    get_ace.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    get_ace.restype = wintypes.BOOL
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    equal_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    se_file_object = 1
    se_dacl_protected = 0x1000
    access_allowed_ace_type = 0
    inherited_ace = 0x10
    file_all_access = 0x001F01FF
    foreign_write_rights = (
        0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
        | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
        | 0x00000010  # FILE_WRITE_EA
        | 0x00000040  # FILE_DELETE_CHILD
        | 0x00000100  # FILE_WRITE_ATTRIBUTES
        | 0x00010000  # DELETE
        | 0x00040000  # WRITE_DAC
        | 0x00080000  # WRITE_OWNER
        | 0x10000000  # GENERIC_ALL
        | 0x40000000  # GENERIC_WRITE
    )

    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    result = get_named_security(
        str(path),
        se_file_object,
        owner_security_information | dacl_security_information,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not descriptor or not owner or not dacl:
        return False

    sid_pointers = [
        pointer
        for sid in (expected_sid, "S-1-5-18", "S-1-5-32-544")
        if (pointer := _windows_sid_pointer(sid)) is not None
    ]
    if len(sid_pointers) != 3:
        for pointer in sid_pointers:
            local_free(ctypes.cast(pointer, wintypes.HLOCAL))
        local_free(ctypes.cast(descriptor, wintypes.HLOCAL))
        return False
    current_sid, system_sid, administrators_sid = sid_pointers
    try:
        trusted_sids = (current_sid, system_sid, administrators_sid)
        owner_matches = any(equal_sid(owner, trusted) for trusted in trusted_sids)
        if require_private:
            owner_matches = bool(equal_sid(owner, current_sid))
        if not owner_matches:
            return False

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            return False
        if require_private and not control.value & se_dacl_protected:
            return False

        acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
        current_full_access = False
        accepted_aces = 0
        for index in range(acl.AceCount):
            ace_pointer = wintypes.LPVOID()
            if not get_ace(dacl, index, ctypes.byref(ace_pointer)) or not ace_pointer:
                return False
            header = ctypes.cast(ace_pointer, ctypes.POINTER(_AceHeader)).contents
            if header.AceType != access_allowed_ace_type or header.AceSize < 12:
                return False
            address = cast(int, ace_pointer.value)
            mask = ctypes.c_uint32.from_address(address + 4).value
            sid_pointer = wintypes.LPVOID(address + 8)
            is_current = bool(equal_sid(sid_pointer, current_sid))
            is_trusted = any(equal_sid(sid_pointer, trusted) for trusted in trusted_sids)
            if is_current and mask & file_all_access == file_all_access:
                current_full_access = True
            if require_private:
                if not is_current or header.AceFlags & inherited_ace:
                    return False
            elif not is_trusted and mask & foreign_write_rights:
                return False
            accepted_aces += 1
        return current_full_access and (not require_private or accepted_aces == 1)
    finally:
        for pointer in sid_pointers:
            local_free(ctypes.cast(pointer, wintypes.HLOCAL))
        local_free(ctypes.cast(descriptor, wintypes.HLOCAL))


def _windows_create_private_directory(path: Path, *, sid: str) -> None:
    if os.name != "nt":  # pragma: no cover - guarded by Windows callers
        raise LocalResourceStatePathError(path)
    import ctypes
    from ctypes import wintypes

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_descriptor.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert_descriptor.restype = wintypes.BOOL
    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
    create_directory.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    sddl = f"O:{sid}G:{sid}D:P(A;OICI;FA;;;{sid})"
    if not convert_descriptor(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
        raise LocalResourceStatePathError(path)
    try:
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            descriptor,
            False,
        )
        if not create_directory(str(path), ctypes.byref(attributes)):
            error_code = ctypes.get_last_error()
            if error_code != 183:  # ERROR_ALREADY_EXISTS
                raise LocalResourceStatePathError(path)
    finally:
        local_free(ctypes.cast(descriptor, wintypes.HLOCAL))


def _validate_private_state_dir(path: Path, *, allow_absent: bool) -> bool:
    """Validate a private per-user directory without creating or repairing it."""

    _validate_state_parent(path)
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        if allow_absent:
            return False
        raise LocalResourceStatePathError(path) from None
    except OSError as error:
        raise LocalResourceStatePathError(path) from error

    if not stat.S_ISDIR(path_stat.st_mode):
        raise LocalResourceStatePathError(path)
    if os.name == "nt":
        if (
            _WINDOWS_CURRENT_USER_SID is None
            or getattr(path_stat, "st_file_attributes", 0) & 0x00000400
            or not _windows_acl_is_safe(
                path,
                expected_sid=_WINDOWS_CURRENT_USER_SID,
                require_private=True,
            )
        ):
            raise LocalResourceStatePathError(path)
        try:
            rechecked = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise LocalResourceStatePathError(path) from error
        if not os.path.samestat(path_stat, rechecked):
            raise LocalResourceStatePathError(path)
        return True

    if path_stat.st_uid != os.geteuid() or stat.S_IMODE(path_stat.st_mode) & 0o077:
        raise LocalResourceStatePathError(path)
    return True


def _validate_state_parent(path: Path) -> None:
    """Require a stable parent that prevents unsafe state-directory replacement."""

    if not path.is_absolute():
        raise LocalResourceStatePathError(path)
    try:
        parent_stat = os.stat(path.parent, follow_symlinks=False)
    except OSError as error:
        raise LocalResourceStatePathError(path) from error
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise LocalResourceStatePathError(path)
    if os.name == "nt":
        if (
            _WINDOWS_CURRENT_USER_SID is None
            or getattr(parent_stat, "st_file_attributes", 0) & 0x00000400
            or not _windows_acl_is_safe(
                path.parent,
                expected_sid=_WINDOWS_CURRENT_USER_SID,
                require_private=False,
            )
        ):
            raise LocalResourceStatePathError(path)
        try:
            rechecked = os.stat(path.parent, follow_symlinks=False)
        except OSError as error:
            raise LocalResourceStatePathError(path) from error
        if not os.path.samestat(parent_stat, rechecked):
            raise LocalResourceStatePathError(path)
        return

    permissions = stat.S_IMODE(parent_stat.st_mode)
    if parent_stat.st_uid != os.geteuid() or permissions & 0o022:
        raise LocalResourceStatePathError(path)
    controlled_path = path.parent
    controlled_stat = parent_stat
    while True:
        controlling_path = controlled_path.parent
        try:
            controlling_stat = os.stat(controlling_path, follow_symlinks=False)
            rechecked_controlled = os.stat(controlled_path, follow_symlinks=False)
        except OSError as error:
            raise LocalResourceStatePathError(path) from error
        controlling_permissions = stat.S_IMODE(controlling_stat.st_mode)
        controlling_owner = controlling_stat.st_uid in {0, os.geteuid()}
        controlling_foreign_writable = bool(controlling_permissions & 0o022)
        controlling_sticky = bool(controlling_stat.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(controlling_stat.st_mode)
            or not controlling_owner
            or (controlling_foreign_writable and not controlling_sticky)
            or not os.path.samestat(controlled_stat, rechecked_controlled)
        ):
            raise LocalResourceStatePathError(path)
        if controlling_path == controlled_path:
            break
        controlled_path = controlling_path
        controlled_stat = controlling_stat


def _ensure_private_state_dir(path: Path) -> None:
    """Create only the final private directory and then validate its exact identity."""

    _validate_state_parent(path)
    if os.name == "nt":
        if _WINDOWS_CURRENT_USER_SID is None:
            raise LocalResourceStatePathError(path)
        _windows_create_private_directory(path, sid=_WINDOWS_CURRENT_USER_SID)
    else:
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError as error:
            raise LocalResourceStatePathError(path) from error
    _validate_private_state_dir(path, allow_absent=False)


def _state_file_path(state_dir: Path, filename: str) -> Path:
    if filename not in {
        ACTIVE_STATE_FILE,
        LAST_COMPLETED_STATE_FILE,
        QUEUE_STATE_FILE,
        QUEUE_LOCK_FILE,
    }:
        raise ValueError("unrecognized local resource state filename")
    return state_dir / filename


def _open_existing_state_descriptor(path: Path) -> int:
    try:
        return _open_lock_descriptor(path, create=False)
    except FileNotFoundError:
        raise
    except RealRayOwnershipPathError as error:
        raise LocalResourceStatePathError(path) from error


@contextmanager
def _deferred_sigint_state_descriptor_transition():
    """Defer main-thread SIGINT until one raw descriptor has stable custody."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous_handler = signal.getsignal(signal.SIGINT)
    observed = False
    observed_frame: FrameType | None = None

    def defer_sigint(_signum: int, frame: FrameType | None) -> None:
        nonlocal observed, observed_frame
        observed = True
        observed_frame = frame

    signal.signal(signal.SIGINT, defer_sigint)
    restore_error: BaseException | None = None
    try:
        yield
    finally:
        restored = False
        while not restored:
            try:
                signal.signal(signal.SIGINT, previous_handler)  # type: ignore[arg-type]
                restored = True
            except BaseException as caught:
                # Restoring one disposition is idempotent if SIGINT lands
                # after the native transition but before Python records it.
                if restore_error is None:
                    restore_error = caught
        if restore_error is not None:
            raise restore_error
        if observed and previous_handler is not signal.SIG_IGN:
            if callable(previous_handler):
                handler = cast(
                    Callable[[int, FrameType | None], object],
                    previous_handler,
                )
                handler(signal.SIGINT, observed_frame)
            else:
                raise KeyboardInterrupt


@dataclass(slots=True)
class _OwnedStateDescriptor:
    """Own one raw state fd across interruptible open, transfer, and close."""

    value: int | None = None

    def acquire(self, opener: Callable[[], int]) -> None:
        if self.value is not None:  # pragma: no cover - local ownership invariant
            raise RuntimeError("state descriptor is already owned")
        with _deferred_sigint_state_descriptor_transition():
            self.value = opener()

    def fileno(self) -> int:
        if self.value is None:  # pragma: no cover - local ownership invariant
            raise RuntimeError("state descriptor is not owned")
        return self.value

    def read_bounded(self, size: int) -> bytes:
        descriptor = self.fileno()
        with _deferred_sigint_state_descriptor_transition():
            handle = os.fdopen(descriptor, "rb", buffering=0)
            self.value = None
            try:
                return handle.read(size)
            finally:
                # Keep SIGINT deferred through the bounded read and file-object
                # close.  Replaying before this close would retain the handle
                # in the KeyboardInterrupt traceback and block a Windows
                # replace during lifecycle cleanup.
                handle.close()

    def close(self) -> None:
        if self.value is None:
            return
        with _deferred_sigint_state_descriptor_transition():
            descriptor = self.value
            # Consume before the non-idempotent close.  The local SIGINT
            # deferrer prevents a pre-close interruption from stranding the
            # fd, while a post-close interruption cannot retry a recycled fd.
            self.value = None
            os.close(descriptor)


def _read_state_json(path: Path) -> dict[str, object] | None:
    """Read one bounded private JSON object without creating or repairing it."""

    descriptor = _OwnedStateDescriptor()
    try:
        try:
            descriptor.acquire(lambda: _open_existing_state_descriptor(path))
        except FileNotFoundError:
            # Absence is a read-only result only when open never committed an
            # fd.  A later exception after ownership publication must unwind
            # through this function's descriptor cleanup instead.
            if descriptor.value is None:
                return None
            raise
        size = os.fstat(descriptor.fileno()).st_size
        if size <= 0 or size > _MAX_STATE_FILE_BYTES:
            raise LocalResourceStateError(f"local resource state file {path.name} has invalid size")
        payload = descriptor.read_bounded(_MAX_STATE_FILE_BYTES + 1)
    finally:
        descriptor.close()

    def reject_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def bounded_int(value: str) -> int:
        if len(value.lstrip("-")) > 19:
            raise ValueError("oversized JSON integer")
        parsed = int(value)
        if not -(2**63) <= parsed < 2**63:
            raise ValueError("oversized JSON integer")
        return parsed

    def reject_number(_value: str) -> float:
        raise ValueError("non-integer JSON number")

    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=reject_pairs,
            parse_constant=reject_number,
            parse_float=reject_number,
            parse_int=bounded_int,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise LocalResourceStateError(
            f"local resource state file {path.name} is not canonical JSON"
        ) from error
    if not isinstance(decoded, dict):
        raise LocalResourceStateError(f"local resource state file {path.name} is not an object")
    try:
        canonical = json.dumps(
            decoded,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (ValueError, RecursionError) as error:
        raise LocalResourceStateError(
            f"local resource state file {path.name} is not canonical JSON"
        ) from error
    if canonical != payload:
        raise LocalResourceStateError(
            f"local resource state file {path.name} is not canonical JSON"
        )
    return cast(dict[str, object], decoded)


def _write_state_json(path: Path, value: Mapping[str, object]) -> None:
    """Atomically replace one private bounded state object."""

    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not payload or len(payload) > _MAX_STATE_FILE_BYTES:
        raise LocalResourceStateError(f"local resource state file {path.name} exceeds its bound")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = _OwnedStateDescriptor()
    validated = _OwnedStateDescriptor()
    try:
        descriptor.acquire(lambda: os.open(temporary, flags, 0o600))
        written = 0
        while written < len(payload):
            written += os.write(descriptor.fileno(), payload[written:])
        os.fsync(descriptor.fileno())
        descriptor.close()
        os.replace(temporary, path)
        validated.acquire(lambda: _open_existing_state_descriptor(path))
        validated.close()
    except (OSError, LocalResourceStatePathError) as error:
        raise LocalResourceStateError(
            f"local resource state file {path.name} could not be written safely"
        ) from error
    finally:
        validated.close()
        descriptor.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _open_windows_state_delete_handle(path: Path) -> int:
    """Open one exact non-reparse Windows file identity for handle-based deletion."""

    import ctypes
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
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    share_read = 0x00000001
    share_write = 0x00000002
    share_delete = 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        delete_access | file_read_attributes,
        share_read | share_write | share_delete,
        None,
        open_existing,
        file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle:
        error_code = ctypes.get_last_error()
        if error_code in {2, 3}:
            raise FileNotFoundError(error_code, "Windows state file does not exist")
        raise LocalResourceStateError(
            f"local resource state file {path.name} could not be opened for deletion"
        )
    information = _ByHandleFileInformation()
    unsafe = file_attribute_directory | file_attribute_reparse_point
    if (
        not get_information(handle, ctypes.byref(information))
        or information.dwFileAttributes & unsafe
        or information.nNumberOfLinks != 1
    ):
        close_handle(handle)
        raise LocalResourceStateError(
            f"local resource state file {path.name} has an unsafe deletion identity"
        )
    return cast(int, handle)


def _delete_windows_state_handle(path: Path, handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class _FileDispositionInformation(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    disposition = _FileDispositionInformation(True)
    try:
        if not set_information(
            handle,
            4,  # FileDispositionInfo
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise LocalResourceStateError(
                f"local resource state file {path.name} could not be removed safely"
            )
    finally:
        close_handle(handle)


def _remove_state_file(path: Path) -> None:
    """Delete only the validated identity; the private same-user directory is trusted."""

    if os.name == "nt":
        try:
            handle = _open_windows_state_delete_handle(path)
        except FileNotFoundError:
            return
        _delete_windows_state_handle(path, handle)
        return
    try:
        descriptor = _open_existing_state_descriptor(path)
    except FileNotFoundError:
        return
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(descriptor_stat, path_stat):
            raise LocalResourceStateError(
                f"local resource state file {path.name} changed before deletion"
            )
        path.unlink()
    except FileNotFoundError:
        return
    except LocalResourceStateError:
        raise
    except OSError as error:
        raise LocalResourceStateError(
            f"local resource state file {path.name} could not be removed safely"
        ) from error
    finally:
        os.close(descriptor)


@contextmanager
def _locked_state_mutex(
    state_dir: Path,
    *,
    create: bool,
    timeout_seconds: float,
):
    path = _state_file_path(state_dir, QUEUE_LOCK_FILE)
    try:
        descriptor = _open_lock_descriptor(path, create=create)
    except FileNotFoundError:
        yield False
        return
    except RealRayOwnershipPathError as error:
        raise LocalResourceStatePathError(path) from error
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                _try_advisory_lock(handle)
                break
            except OSError as error:
                if not _lock_contention(error):
                    raise LocalResourceStateError(
                        "local resource state mutex is unavailable"
                    ) from error
                if time.monotonic() >= deadline:
                    raise LocalResourceStateError("local resource state mutex timed out") from error
                time.sleep(0.02)
        try:
            yield True
        finally:
            _release_advisory_lock(handle)
    finally:
        handle.close()


@contextmanager
def _state_mutex(state_dir: Path, *, timeout_seconds: float = 5.0):
    _ensure_private_state_dir(state_dir)
    with _locked_state_mutex(
        state_dir,
        create=True,
        timeout_seconds=timeout_seconds,
    ) as locked:
        if not locked:  # pragma: no cover - create=True cannot report absence
            raise LocalResourceStateError("local resource state mutex was not created")
        yield


@contextmanager
def _existing_state_mutex(state_dir: Path, *, timeout_seconds: float = 1.0):
    with _locked_state_mutex(
        state_dir,
        create=False,
        timeout_seconds=timeout_seconds,
    ) as locked:
        yield locked


def _new_queue_state() -> dict[str, object]:
    return {"schema_version": LOCAL_RESOURCE_SCHEMA_VERSION, "next_ticket": 1, "items": []}


def _read_queue_state(state_dir: Path) -> dict[str, object]:
    payload = _read_state_json(_state_file_path(state_dir, QUEUE_STATE_FILE))
    if payload is None:
        return _new_queue_state()
    if payload.get("schema_version") != LOCAL_RESOURCE_SCHEMA_VERSION:
        raise LocalResourceStateError("local resource queue has an unsupported schema")
    next_ticket = payload.get("next_ticket")
    items = payload.get("items")
    if (
        not isinstance(next_ticket, int)
        or isinstance(next_ticket, bool)
        or next_ticket < 1
        or next_ticket > _MAX_TICKET + 1
        or not isinstance(items, list)
        or len(items) > _MAX_QUEUE_ITEMS
        or not all(isinstance(item, dict) for item in items)
    ):
        raise LocalResourceStateError("local resource queue is corrupt")
    return payload


def _write_queue_state(state_dir: Path, payload: Mapping[str, object]) -> None:
    _write_state_json(_state_file_path(state_dir, QUEUE_STATE_FILE), payload)


def _validated_queue_items(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("items")
    next_ticket = payload.get("next_ticket")
    if not isinstance(raw_items, list) or not isinstance(next_ticket, int):
        raise LocalResourceStateError("local resource queue is corrupt")
    items: list[dict[str, object]] = []
    request_ids: set[str] = set()
    tickets: set[int] = set()
    previous_ticket = 0
    for raw_item in raw_items:
        public = _public_queue_item(raw_item)
        request_id = cast(str, public["request_id"])
        ticket = cast(int, public["ticket"])
        if (
            request_id in request_ids
            or ticket in tickets
            or ticket <= previous_ticket
            or ticket >= next_ticket
        ):
            raise LocalResourceStateError("local resource queue ordering is corrupt")
        request_ids.add(request_id)
        tickets.add(ticket)
        previous_ticket = ticket
        items.append(cast(dict[str, object], raw_item))
    return items


def _authority_owner_dict(owner: LocalResourceOwner) -> dict[str, object]:
    pid = _safe_pid(owner.pid)
    if pid is None:
        raise LocalResourceStateError("local resource owner PID is invalid")
    process_birth = _validated_process_birth(owner.process_birth)
    value = owner.as_dict()
    value["pid"] = pid
    value["process_birth"] = process_birth
    return value


def _owner_from_mapping(value: object) -> LocalResourceOwner:
    if not isinstance(value, Mapping):
        raise LocalResourceStateError("local resource owner metadata is corrupt")
    pid = _safe_pid(value.get("pid"))
    if pid is None:
        raise LocalResourceStateError("local resource owner PID is invalid")
    process_birth = _validated_process_birth(value.get("process_birth"))
    return LocalResourceOwner(
        owner=cast(str | None, value.get("owner")),
        session=cast(str | None, value.get("session")),
        agent=cast(str | None, value.get("agent")),
        model=cast(str | None, value.get("model")),
        host_id=cast(str | None, value.get("host_id")),
        pid=pid,
        process_birth=process_birth,
    )


def _source_from_mapping(value: object) -> LocalResourceSource:
    if not isinstance(value, Mapping):
        raise LocalResourceStateError("local resource source metadata is corrupt")
    return LocalResourceSource(
        worktree=cast(str | None, value.get("worktree")),
        branch=cast(str | None, value.get("branch")),
        commit=cast(str | None, value.get("commit")),
        source_tree=cast(str | None, value.get("source_tree")),
        dirty=cast(bool | None, value.get("dirty")),
    )


def _validate_profile(value: object) -> str:
    if not isinstance(value, str) or value not in LOCAL_RESOURCE_PROFILES:
        raise LocalResourceStateError("local resource profile is unknown")
    return value


def _validated_resources(profile: str, value: object) -> list[str]:
    expected = list(LOCAL_RESOURCE_PROFILES[profile])
    if value != expected:
        raise LocalResourceStateError("local resource profile resources are inconsistent")
    return expected


def _validated_identifier(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LocalResourceStateError(f"local resource {field_name} is invalid")
    return value


def _validated_phase(value: object) -> str:
    phase = _bounded_status_text(value)
    if phase is None or not phase or phase != value:
        raise LocalResourceStateError("local resource phase is invalid")
    return phase


def _public_queue_item(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise LocalResourceStateError("local resource queue item is corrupt")
    request_id = _validated_identifier(value.get("request_id"), field_name="request id")
    ticket = value.get("ticket")
    if not isinstance(ticket, int) or isinstance(ticket, bool) or ticket < 1:
        raise LocalResourceStateError("local resource queue ticket is invalid")
    profile = _validate_profile(value.get("profile"))
    resources = _validated_resources(profile, value.get("resources"))
    owner = _owner_from_mapping(value.get("owner"))
    source = _source_from_mapping(value.get("source"))
    return {
        "request_id": request_id,
        "ticket": ticket,
        "profile": profile,
        "resources": resources,
        "phase": _validated_phase(value.get("phase")),
        "owner": owner.as_dict(),
        "source": source.as_dict(),
        "intent": _bounded_status_text(value.get("intent")),
        "handoff": _bounded_status_text(value.get("handoff")),
        "requested_at": _bounded_status_text(value.get("requested_at")),
        "liveness": _process_liveness(owner.pid, owner.process_birth),
    }


def _public_active_record(
    value: object,
    *,
    owner_lock_held: bool = True,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise LocalResourceStateError("local resource active record is corrupt")
    if value.get("schema_version") != LOCAL_RESOURCE_SCHEMA_VERSION:
        raise LocalResourceStateError("local resource active record has an unsupported schema")
    run_id = _validated_identifier(value.get("run_id"), field_name="run id")
    profile = _validate_profile(value.get("profile"))
    resources = _validated_resources(profile, value.get("resources"))
    owner = _owner_from_mapping(value.get("owner"))
    source = _source_from_mapping(value.get("source"))
    child_value = value.get("child")
    child: dict[str, object] | None = None
    if child_value is not None:
        if not isinstance(child_value, Mapping):
            raise LocalResourceStateError("local resource child metadata is corrupt")
        child_pid = _safe_pid(child_value.get("pid"))
        child_birth = _validated_process_birth(child_value.get("process_birth"))
        tree_kind = child_value.get("tree_kind")
        expected_tree_kind = "windows-job" if os.name == "nt" else "posix-process-group"
        if tree_kind != expected_tree_kind:
            raise LocalResourceStateError("local resource child tree custody is invalid")
        if child_pid is None:
            raise LocalResourceStateError("local resource child identity is incomplete")
        child = {
            "pid": child_pid,
            "process_birth": child_birth,
            "tree_kind": tree_kind,
            "liveness": _child_record_liveness(
                child_value,
                owner_lock_held=owner_lock_held,
            ),
        }
    return {
        "run_id": run_id,
        "profile": profile,
        "resources": resources,
        "phase": _validated_phase(value.get("phase")),
        "queue_position": 0,
        "owner": owner.as_dict(),
        "source": source.as_dict(),
        "intent": _bounded_status_text(value.get("intent")),
        "handoff": _bounded_status_text(value.get("handoff")),
        "acquired_at": _bounded_status_text(value.get("acquired_at")),
        "heartbeat_at": _bounded_status_text(value.get("heartbeat_at")),
        "expiry_at": _bounded_status_text(value.get("expiry_at")),
        "selected_count": _validated_selected_count(value.get("selected_count")),
        "child": child,
        "outcome": _bounded_status_text(value.get("outcome")),
        "postcondition": _bounded_status_text(value.get("postcondition")),
        "liveness": "os-lock-held",
        "legacy": False,
    }


def _validate_capability_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LocalResourceStateError("local resource capability digest is invalid")
    return value


def _read_active_record(state_dir: Path) -> dict[str, object] | None:
    value = _read_state_json(_state_file_path(state_dir, ACTIVE_STATE_FILE))
    if value is None:
        return None
    _public_active_record(value)
    _validate_capability_digest(value.get("capability_sha256"))
    _validate_capability_digest(value.get("rootpath_sha256"))
    ticket = value.get("ticket")
    if not isinstance(ticket, int) or isinstance(ticket, bool) or not 1 <= ticket <= _MAX_TICKET:
        raise LocalResourceStateError("local resource active ticket is invalid")
    return value


def _public_last_completed(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise LocalResourceStateError("local resource completion record is corrupt")
    if value.get("schema_version") != LOCAL_RESOURCE_SCHEMA_VERSION:
        raise LocalResourceStateError("local resource completion record has an unsupported schema")
    profile = _validate_profile(value.get("profile"))
    return {
        "run_id": _validated_identifier(value.get("run_id"), field_name="run id"),
        "profile": profile,
        "resources": _validated_resources(profile, value.get("resources")),
        "owner": _owner_from_mapping(value.get("owner")).as_dict(),
        "source": _source_from_mapping(value.get("source")).as_dict(),
        "intent": _bounded_status_text(value.get("intent")),
        "handoff": _bounded_status_text(value.get("handoff")),
        "acquired_at": _bounded_status_text(value.get("acquired_at")),
        "completed_at": _bounded_status_text(value.get("completed_at")),
        "outcome": _bounded_status_text(value.get("outcome")),
        "postcondition": _bounded_status_text(value.get("postcondition")),
    }


def _capability_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _validated_selected_count(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_SELECTED_COUNT
    ):
        raise LocalResourceStateError("local resource selected count is invalid")
    return value


def _transition_active_to_last_completed(
    state_dir: Path,
    active: Mapping[str, object],
) -> None:
    """Atomically retire active ownership while publishing its completion."""

    _public_last_completed(active)
    active_path = _state_file_path(state_dir, ACTIVE_STATE_FILE)
    completed_path = _state_file_path(state_dir, LAST_COMPLETED_STATE_FILE)
    try:
        os.replace(active_path, completed_path)
    except OSError as error:
        raise LocalResourceStateError(
            "local resource completion transition could not retire active ownership"
        ) from error


def _validated_wait_seconds(value: object, *, field_name: str, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalResourceStateError(f"local resource {field_name} is invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or (positive and parsed == 0):
        raise LocalResourceStateError(f"local resource {field_name} is invalid")
    return parsed


def _active_liveness(record: Mapping[str, object]) -> tuple[str, str]:
    active = _public_active_record(record, owner_lock_held=False)
    owner = cast(Mapping[str, object], active["owner"])
    root_liveness = _process_liveness(owner.get("pid"), owner.get("process_birth"))
    child = active.get("child")
    child_liveness = cast(str, child.get("liveness")) if isinstance(child, Mapping) else "dead"
    return root_liveness, child_liveness


def _validate_registry_authority(
    active_record: Mapping[str, object] | None,
    *,
    legacy_state: str,
    legacy_metadata: Mapping[str, object],
) -> Literal["clear", "busy", "stale"]:
    """Classify a coherent snapshot without granting takeover authority."""

    if legacy_state == "held":
        if active_record is None:
            return "busy"
        active = _public_active_record(active_record)
        owner = cast(Mapping[str, object], active["owner"])
        legacy_pid = _safe_pid(legacy_metadata.get("pid"))
        owner_pid = _safe_pid(owner.get("pid"))
        owner_birth = owner.get("process_birth")
        if (
            legacy_pid is None
            or owner_pid is None
            or legacy_pid != owner_pid
            or _process_liveness(owner_pid, owner_birth) != "live"
        ):
            raise LocalResourceStateError(
                "held local resource lock does not match the active registry owner"
            )
        return "busy"

    if legacy_state != "free":
        raise LocalResourceStateError("legacy ownership lock state is unavailable")
    if active_record is None:
        return "clear"
    root_liveness, child_liveness = _active_liveness(active_record)
    if root_liveness == "live":
        raise LocalResourceStateError(
            "active local resource owner is live without its authoritative OS lock"
        )
    if root_liveness == "unknown":
        raise LocalResourceStateError("active local resource owner liveness is unknown")
    if child_liveness != "dead":
        raise LocalResourceStateError("orphaned local resource child liveness is not absent")
    return "stale"


def _prune_queue_items(items: Sequence[dict[str, object]]) -> tuple[list[dict[str, object]], bool]:
    retained: list[dict[str, object]] = []
    changed = False
    for item in items:
        public = _public_queue_item(item)
        liveness = public["liveness"]
        if liveness == "dead":
            changed = True
            continue
        if liveness != "live":
            raise LocalResourceStateError(
                "queued local resource requester liveness cannot be proved"
            )
        retained.append(item)
    return retained, changed


def _new_queue_item(
    *,
    request_id: str,
    ticket: int,
    profile: str,
    phase: str,
    owner: LocalResourceOwner,
    source: LocalResourceSource,
    intent: str,
    handoff: str | None,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "ticket": ticket,
        "profile": profile,
        "resources": list(LOCAL_RESOURCE_PROFILES[profile]),
        "phase": phase,
        "owner": _authority_owner_dict(owner),
        "source": source.as_dict(),
        "intent": intent,
        "handoff": handoff,
        "requested_at": _observed_at(),
    }


def _register_queue_request(
    *,
    state_dir: Path,
    lock_path: Path,
    request_id: str,
    profile: str,
    phase: str,
    owner: LocalResourceOwner,
    source: LocalResourceSource,
    intent: str,
    handoff: str | None,
) -> int:
    with _state_mutex(state_dir):
        active_record = _read_active_record(state_dir)
        queue_state = _read_queue_state(state_dir)
        items, changed = _prune_queue_items(_validated_queue_items(queue_state))
        authority_state, authority_metadata = _probe_legacy_lock(
            _local_resource_authority_lock_path(
                state_dir=state_dir,
                legacy_lock_path=lock_path,
            )
        )
        _validate_registry_authority(
            active_record,
            legacy_state=authority_state,
            legacy_metadata=authority_metadata,
        )
        _probe_legacy_compatibility_lock(lock_path)
        for item in items:
            if item.get("request_id") == request_id:
                return cast(int, item["ticket"])
        if len(items) >= _MAX_QUEUE_ITEMS:
            raise LocalResourceBusyError("local resource wait queue is full")
        next_ticket = cast(int, queue_state["next_ticket"])
        if next_ticket > _MAX_TICKET:
            raise LocalResourceStateError("local resource queue ticket space is exhausted")
        items.append(
            _new_queue_item(
                request_id=request_id,
                ticket=next_ticket,
                profile=profile,
                phase=phase,
                owner=owner,
                source=source,
                intent=intent,
                handoff=handoff,
            )
        )
        queue_state["next_ticket"] = next_ticket + 1
        queue_state["items"] = items
        _write_queue_state(state_dir, queue_state)
        return next_ticket


def _remove_queue_request(*, state_dir: Path, request_id: str) -> None:
    with _state_mutex(state_dir):
        queue_state = _read_queue_state(state_dir)
        items = _validated_queue_items(queue_state)
        retained = [item for item in items if item.get("request_id") != request_id]
        if len(retained) != len(items):
            queue_state["items"] = retained
            _write_queue_state(state_dir, queue_state)


def _retire_stale_active_record(
    *,
    state_dir: Path,
    active_record: Mapping[str, object] | None,
) -> None:
    if active_record is None:
        return
    root_liveness, child_liveness = _active_liveness(active_record)
    if root_liveness != "dead" or child_liveness != "dead":
        raise LocalResourceStateError(
            "local resource active record is not proved stale while the OS lock is owned"
        )
    _remove_state_file(_state_file_path(state_dir, ACTIVE_STATE_FILE))


def _active_record_for_request(
    *,
    run_id: str,
    ticket: int,
    capability_token: str,
    profile: str,
    phase: str,
    owner: LocalResourceOwner,
    source: LocalResourceSource,
    rootpath: Path,
    intent: str,
    handoff: str | None,
    selected_count: int | None,
) -> dict[str, object]:
    acquired_at = _observed_at()
    return {
        "schema_version": LOCAL_RESOURCE_SCHEMA_VERSION,
        "run_id": run_id,
        "ticket": ticket,
        "profile": profile,
        "resources": list(LOCAL_RESOURCE_PROFILES[profile]),
        "phase": phase,
        "owner": _authority_owner_dict(owner),
        "source": source.as_dict(),
        "rootpath_sha256": _rootpath_digest(rootpath),
        "intent": intent,
        "handoff": handoff,
        "acquired_at": acquired_at,
        "heartbeat_at": acquired_at,
        "expiry_at": None,
        "selected_count": selected_count,
        "child": None,
        "outcome": None,
        "postcondition": None,
        "capability_sha256": _capability_digest(capability_token),
    }


class LocalResourceLease:
    """One exact coordinator capability; only the root object owns mutations."""

    def __init__(
        self,
        *,
        run_id: str,
        profile: str,
        authority_profile: str,
        state_dir: Path,
        capability_token: str,
        inherited: bool,
        ownership_lock: RealRayOwnershipLock | None = None,
    ) -> None:
        self.run_id = run_id
        self.profile = profile
        self.inherited = inherited
        self._authority_profile = authority_profile
        self._state_dir = state_dir
        self._capability_token = capability_token
        self._ownership_lock = ownership_lock
        self._released = False
        self._child_recorded = False
        self._windows_child_job_handle: int | None = None
        self._pending_release: tuple[str, str | None] | None = None

    @property
    def resources(self) -> tuple[str, ...]:
        return LOCAL_RESOURCE_PROFILES[self.profile]

    @property
    def termination_authority(self) -> Literal["owned-child-tree", "none"]:
        if not self.inherited and not self._released and self._ownership_lock is not None:
            return "owned-child-tree" if self._child_recorded else "none"
        return "none"

    def inheritance_environment(self) -> dict[str, str]:
        if self._released:
            raise LocalResourceStateError("released local resource lease cannot be inherited")
        return {
            LOCAL_RESOURCE_RUN_ID_ENV: self.run_id,
            LOCAL_RESOURCE_CAPABILITY_ENV: self._capability_token,
            LOCAL_RESOURCE_PROFILE_ENV: self._authority_profile,
            LOCAL_RESOURCE_STATE_DIR_ENV: str(self._state_dir),
        }

    def _verified_active_record(self) -> dict[str, object]:
        active = _read_active_record(self._state_dir)
        if active is None:
            raise LocalResourceStateError("owned local resource active record is missing")
        if active.get("run_id") != self.run_id or not hmac.compare_digest(
            cast(str, active.get("capability_sha256")),
            _capability_digest(self._capability_token),
        ):
            raise LocalResourceStateError("owned local resource capability no longer matches")
        owner = _owner_from_mapping(active.get("owner"))
        if owner.pid != os.getpid() or _process_liveness(owner.pid, owner.process_birth) != "live":
            raise LocalResourceStateError("owned local resource process identity no longer matches")
        return active

    def update_phase(self, phase: str) -> None:
        if self.inherited:
            return
        if self._released:
            raise LocalResourceStateError("released local resource lease cannot be updated")
        validated_phase = _validated_phase(phase)
        with _state_mutex(self._state_dir):
            active = self._verified_active_record()
            active["phase"] = validated_phase
            active["heartbeat_at"] = _observed_at()
            _write_state_json(_state_file_path(self._state_dir, ACTIVE_STATE_FILE), active)

    def record_child(
        self,
        pid: int,
        process_birth: str,
        *,
        tree_kind: Literal["posix-process-group", "windows-job"],
        windows_job_handle: int | None = None,
    ) -> None:
        if self.inherited:
            raise LocalResourceInheritanceError(
                "inherited local resource lease has no child termination authority"
            )
        if self._released:
            raise LocalResourceStateError("released local resource lease cannot record a child")
        child_pid = _safe_pid(pid)
        child_birth = _validated_process_birth(process_birth)
        expected_tree_kind = "windows-job" if os.name == "nt" else "posix-process-group"
        if tree_kind != expected_tree_kind:
            raise LocalResourceStateError("local resource child tree custody is invalid")
        if child_pid is None:
            raise LocalResourceStateError("local resource child identity is invalid")
        expected = {
            "pid": child_pid,
            "process_birth": child_birth,
            "tree_kind": tree_kind,
        }
        retained_job_handle: int | None = None
        if tree_kind == "windows-job":
            if (
                not isinstance(windows_job_handle, int)
                or isinstance(windows_job_handle, bool)
                or windows_job_handle <= 0
            ):
                raise LocalResourceStateError(
                    "local resource Windows child Job custody is not proved"
                )
            if self._windows_child_job_handle is not None:
                raise LocalResourceStateError(
                    "local resource lease already retains Windows Job custody"
                )
            retained_job_handle = _duplicate_windows_local_job(windows_job_handle)
            if not _windows_pid_is_in_job(child_pid, retained_job_handle):
                _close_windows_local_job(retained_job_handle)
                raise LocalResourceStateError(
                    "local resource Windows child Job custody is not proved"
                )
        else:
            if windows_job_handle is not None:
                raise LocalResourceStateError("POSIX child custody cannot use a Windows Job handle")
            try:
                process_group = os.getpgid(child_pid)
                session = os.getsid(child_pid)
            except OSError as error:
                raise LocalResourceStateError(
                    "local resource POSIX child session custody is not proved"
                ) from error
            if process_group != child_pid or session != child_pid:
                raise LocalResourceStateError(
                    "local resource POSIX child is not its owned session and group leader"
                )
        # Publish in-memory custody before the uncertain durable transition.
        # If any exception lands after the write, outer lifecycle cleanup can
        # still find the exact process tree and retained Job duplicate instead
        # of crossing a post-write flag/store gap.
        self._windows_child_job_handle = retained_job_handle
        self._child_recorded = True
        try:
            with _state_mutex(self._state_dir):
                active = self._verified_active_record()
                existing = active.get("child")
                if existing is not None and existing != expected:
                    raise LocalResourceStateError(
                        "local resource lease already records another child"
                    )
                owner = _owner_from_mapping(active.get("owner"))
                if (
                    _process_liveness(child_pid, child_birth) != "live"
                    or _process_is_descendant(child_pid, owner.pid, owner.process_birth) != "yes"
                ):
                    raise LocalResourceStateError(
                        "local resource child is not a proved live descendant of its owner"
                    )
                active["child"] = expected
                active["heartbeat_at"] = _observed_at()
                _write_state_json(_state_file_path(self._state_dir, ACTIVE_STATE_FILE), active)
        except BaseException as record_error:
            recorded_exactly = False
            try:
                with _state_mutex(self._state_dir):
                    observed = self._verified_active_record()
                    recorded_exactly = observed.get("child") == expected
            except BaseException as reconciliation_error:
                # Unknown durable state must retain custody. Outer cleanup can
                # retry exact reconciliation; discarding the Job duplicate
                # here could make a committed child unowned.
                raise reconciliation_error from record_error
            if recorded_exactly:
                # Preserve every abnormal exception after an exact commit.
                # Custody is already retained, so outer cleanup can settle the
                # child before propagating the original failure.
                raise
            self._windows_child_job_handle = None
            self._child_recorded = False
            if retained_job_handle is not None:
                # Consume only after exact state proves this child was not
                # durably published. An unknown transition retains the handle
                # for fail-closed outer cleanup.
                _close_windows_local_job(retained_job_handle)
            raise

    def clear_child(self) -> None:
        if self.inherited:
            raise LocalResourceInheritanceError(
                "inherited local resource lease cannot clear root child custody"
            )
        if self._released:
            return
        with _state_mutex(self._state_dir):
            active = self._verified_active_record()
            child = active.get("child")
            retained_job_handle = self._windows_child_job_handle
            if os.name == "nt":
                if child is not None and retained_job_handle is None:
                    raise LocalResourceStateError(
                        "local resource Windows Job authority is unavailable; refusing to clear custody"
                    )
                if (
                    retained_job_handle is not None
                    and _windows_job_active_processes(retained_job_handle) != 0
                ):
                    raise LocalResourceStateError(
                        "local resource Windows Job still has active members; refusing to clear custody"
                    )
            elif child is not None:
                if not isinstance(child, Mapping):
                    raise LocalResourceStateError("local resource child metadata is corrupt")
                if _child_record_liveness(child) != "dead":
                    raise LocalResourceStateError(
                        "local resource child absence is not proved; refusing to clear custody"
                    )
            if child is not None:
                active["child"] = None
                active["heartbeat_at"] = _observed_at()
                _write_state_json(_state_file_path(self._state_dir, ACTIVE_STATE_FILE), active)
            if retained_job_handle is not None:
                self._windows_child_job_handle = None
                self._child_recorded = False
                _close_windows_local_job(retained_job_handle)
            self._child_recorded = False

    def _verify_recorded_child(
        self,
        *,
        pid: int,
        process_birth: str,
        tree_kind: str,
    ) -> None:
        if self.inherited or self._released:
            raise LocalResourceStateError("local resource child custody is not held")
        with _state_mutex(self._state_dir):
            active = self._verified_active_record()
            if active.get("child") != {
                "pid": pid,
                "process_birth": process_birth,
                "tree_kind": tree_kind,
            }:
                raise LocalResourceStateError(
                    "local resource process tree does not match durable child custody"
                )

    def release(
        self,
        *,
        outcome: str = "completed",
        postcondition: str | None = None,
        _completion_resolver: Callable[[bool], tuple[str, str | None]] | None = None,
    ) -> None:
        if self._released or self.inherited:
            self._released = self._released or self.inherited
            return
        safe_outcome = _bounded_status_text(outcome)
        safe_postcondition = _bounded_status_text(postcondition)
        if not safe_outcome or safe_outcome != outcome:
            raise LocalResourceStateError("local resource completion outcome is invalid")
        if postcondition is not None and safe_postcondition != postcondition:
            raise LocalResourceStateError("local resource completion postcondition is invalid")
        ownership = self._ownership_lock
        if ownership is None or not ownership.acquired:
            raise LocalResourceStateError("local resource OS lock is not owned")
        if self._child_recorded or self._windows_child_job_handle is not None:
            raise LocalResourceStateError(
                "local resource child custody must be cleared before releasing ownership"
            )
        if self._pending_release is not None and _completion_resolver is None:
            safe_outcome, safe_postcondition = self._pending_release

        def resolved_completion(*, final: bool) -> tuple[str, str | None]:
            candidate_outcome = safe_outcome
            candidate_postcondition = safe_postcondition
            if _completion_resolver is not None:
                candidate_outcome, candidate_postcondition = _completion_resolver(final)
            bounded_outcome = _bounded_status_text(candidate_outcome)
            bounded_postcondition = _bounded_status_text(candidate_postcondition)
            if not bounded_outcome or bounded_outcome != candidate_outcome:
                raise LocalResourceStateError("local resource completion outcome is invalid")
            if (
                candidate_postcondition is not None
                and bounded_postcondition != candidate_postcondition
            ):
                raise LocalResourceStateError("local resource completion postcondition is invalid")
            return bounded_outcome, bounded_postcondition

        with _state_mutex(self._state_dir):
            provisional_outcome, provisional_postcondition = resolved_completion(final=False)
            self._pending_release = (provisional_outcome, provisional_postcondition)
            active = _read_active_record(self._state_dir)
            if active is None:
                completed = _read_state_json(
                    _state_file_path(self._state_dir, LAST_COMPLETED_STATE_FILE)
                )
                if completed is None:
                    raise LocalResourceStateError("owned local resource active record is missing")
                public_completed = _public_last_completed(completed)
                capability = _validate_capability_digest(completed.get("capability_sha256"))
                if public_completed.get("run_id") != self.run_id or not hmac.compare_digest(
                    capability,
                    _capability_digest(self._capability_token),
                ):
                    raise LocalResourceStateError(
                        "owned local resource completion transition no longer matches"
                    )
                if _completion_resolver is None and (
                    public_completed.get("outcome") != provisional_outcome
                    or public_completed.get("postcondition") != provisional_postcondition
                ):
                    raise LocalResourceStateError(
                        "owned local resource completion transition no longer matches"
                    )
            else:
                active = self._verified_active_record()
                child = active.get("child")
                if child is not None:
                    raise LocalResourceStateError(
                        "local resource child custody must be cleared before releasing ownership"
                    )
                active["completed_at"] = _observed_at()
                active["outcome"] = provisional_outcome
                active["postcondition"] = provisional_postcondition
                active_path = _state_file_path(self._state_dir, ACTIVE_STATE_FILE)
                _write_state_json(active_path, active)
                _transition_active_to_last_completed(self._state_dir, active)
                completed = active

            def persist_completion(
                completion_outcome: str,
                completion_postcondition: str | None,
            ) -> None:
                completed["outcome"] = completion_outcome
                completed["postcondition"] = completion_postcondition
                _public_last_completed(completed)
                completed_path = _state_file_path(
                    self._state_dir,
                    LAST_COMPLETED_STATE_FILE,
                )
                finalization_error: LocalResourceStateError | None = None
                for _attempt in range(2):
                    try:
                        _write_state_json(completed_path, completed)
                    except LocalResourceStateError as caught:
                        # Rewriting the same private atomic object is
                        # idempotent. One retry covers a transition that
                        # committed before its wrapper raised.
                        finalization_error = caught
                    else:
                        finalization_error = None
                        break
                if finalization_error is not None:
                    raise finalization_error

            release_error: BaseException | None = None
            try:
                # Keep the registry mutex across the OS-lane unlock.  Aware
                # contenders cannot advance until the late-bound completion is
                # frozen and, when needed, rewritten below.
                ownership.release()
            except BaseException as caught:
                release_error = caught
            final_outcome, final_postcondition = resolved_completion(final=True)
            if release_error is not None:
                if isinstance(release_error, (KeyboardInterrupt, SystemExit)):
                    final_outcome = "interrupted"
                    final_postcondition = (
                        "OS lane release interrupted before settlement"
                        if ownership.acquired
                        else "OS lane released after an ownership-release interruption"
                    )
                elif final_outcome != "interrupted":
                    final_outcome = "failed"
                    final_postcondition = (
                        "OS lane release incomplete after an ownership-release error"
                        if ownership.acquired
                        else "OS lane released with an ownership-release error"
                    )
            if (
                final_outcome != provisional_outcome
                or final_postcondition != provisional_postcondition
            ):
                persist_completion(final_outcome, final_postcondition)
            self._pending_release = (final_outcome, final_postcondition)
            if not ownership.acquired:
                self._released = True
                self._child_recorded = False
                self._ownership_lock = None
            if release_error is not None:
                raise release_error

    def __enter__(self) -> LocalResourceLease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.inherited:
            self.release()
            return
        if exc_type is None:
            self.release(outcome="passed", postcondition="owned child absent")
        else:
            name = getattr(exc_type, "__name__", "error")
            self.release(outcome="failed", postcondition=f"raised {name}")


def _promote_queue_head(
    *,
    state_dir: Path,
    lock_path: Path,
    request_id: str,
    run_id: str,
    capability_token: str,
    profile: str,
    phase: str,
    owner: LocalResourceOwner,
    rootpath: Path,
    intent: str,
    handoff: str | None,
    selected_count: int | None,
    retain_acquired: Callable[[LocalResourceLease], None],
) -> tuple[LocalResourceLease | None, int]:
    ownership: RealRayOwnershipLock | None = None
    promotion_head: dict[str, object] | None = None
    active_record: dict[str, object] | None = None
    lease: LocalResourceLease | None = None
    active_write_started = False
    try:
        with _state_mutex(state_dir):
            queue_state = _read_queue_state(state_dir)
            items, changed = _prune_queue_items(_validated_queue_items(queue_state))
            own_index = next(
                (index for index, item in enumerate(items) if item.get("request_id") == request_id),
                None,
            )
            if own_index is None:
                raise LocalResourceStateError("local resource queue request disappeared")
            if changed:
                queue_state["items"] = items
                _write_queue_state(state_dir, queue_state)
            if own_index != 0:
                return None, own_index + 1

            ownership = RealRayOwnershipLock(
                _local_resource_authority_lock_path(
                    state_dir=state_dir,
                    legacy_lock_path=lock_path,
                ),
                compatibility_path=lock_path,
            )
            ownership.acquire(
                build_owner_metadata(
                    rootpath=rootpath,
                    selected_count=selected_count or 0,
                )
            )
            active_record = _read_active_record(state_dir)
            _retire_stale_active_record(
                state_dir=state_dir,
                active_record=active_record,
            )
            promotion_head = items[0]

        # Git identity discovery can take tens of seconds on a stressed
        # checkout. Keep it outside the short registry mutex while the
        # authoritative legacy byte lock prevents another requester from being
        # granted the host lane. The queue head is then re-read and compared
        # exactly before publication so a concurrent registry/source mutation
        # fails closed instead of granting a different request with this source
        # snapshot.
        if ownership is None or promotion_head is None:  # pragma: no cover - guarded above
            raise LocalResourceStateError("local resource promotion authority is unavailable")
        grant_source = _source_identity(rootpath)
        with _state_mutex(state_dir):
            queue_state = _read_queue_state(state_dir)
            items, changed = _prune_queue_items(_validated_queue_items(queue_state))
            if not items or items[0] != promotion_head:
                raise LocalResourceStateError(
                    "local resource queue head changed during grant source capture"
                )
            if items[0].get("request_id") != request_id:
                raise LocalResourceStateError(
                    "local resource queue ownership changed during grant source capture"
                )
            if _read_active_record(state_dir) is not None:
                raise LocalResourceStateError(
                    "local resource active state changed during grant source capture"
                )
            remaining = items[1:]
            queue_state["items"] = remaining
            _write_queue_state(state_dir, queue_state)
            active_record = _active_record_for_request(
                run_id=run_id,
                ticket=cast(int, items[0]["ticket"]),
                capability_token=capability_token,
                profile=profile,
                phase=phase,
                owner=owner,
                source=grant_source,
                rootpath=rootpath,
                intent=intent,
                handoff=handoff,
                selected_count=selected_count,
            )
            # Construct exact lease custody before the atomic active write.
            # Every exception from the uncertain publication boundary is then
            # caught while either raw ownership or this lease remains local.
            lease = LocalResourceLease(
                run_id=run_id,
                profile=profile,
                authority_profile=profile,
                state_dir=state_dir,
                capability_token=capability_token,
                inherited=False,
                ownership_lock=ownership,
            )
            active_write_started = True
            _write_state_json(
                _state_file_path(state_dir, ACTIVE_STATE_FILE),
                active_record,
            )
        # Retain inside promotion before the lease crosses its return/store
        # boundary. Any exception through the return remains inside this
        # cleanup-owned try; once the function returns, the caller already
        # owns the exact object through this callback.
        retain_acquired(lease)
        return lease, 0
    except BaseException as publication_error:
        if isinstance(publication_error, RealRayOwnershipUnavailableError) and (
            ownership is None or not ownership.acquired
        ):
            return None, 1
        published = False
        reconciliation_error: BaseException | None = None
        if active_write_started and active_record is not None:
            try:
                with _state_mutex(state_dir):
                    observed = _read_active_record(state_dir)
                    published = bool(
                        observed is not None
                        and observed.get("run_id") == run_id
                        and hmac.compare_digest(
                            cast(str, observed.get("capability_sha256")),
                            _capability_digest(capability_token),
                        )
                        and observed == active_record
                    )
            except BaseException as caught:
                reconciliation_error = caught
        if published:
            if lease is None:  # pragma: no cover - publication invariant
                raise LocalResourceStateError(
                    "local resource published without retained lease custody"
                ) from publication_error
            try:
                lease.release(
                    outcome=(
                        "interrupted"
                        if isinstance(publication_error, KeyboardInterrupt)
                        else "failed"
                    ),
                    postcondition="acquisition aborted after active publication",
                )
            except BaseException as cleanup_error:
                raise cleanup_error from publication_error
            raise
        if ownership is not None and ownership.acquired:
            try:
                ownership.release()
            except BaseException as cleanup_error:
                raise cleanup_error from reconciliation_error or publication_error
        if reconciliation_error is not None:
            raise reconciliation_error from publication_error
        raise


def _inheritance_environment_mode() -> Literal["root", "inherited"]:
    present = [key for key in LOCAL_RESOURCE_INHERITANCE_ENV_KEYS if key in os.environ]
    if not present:
        return "root"
    if len(present) != len(LOCAL_RESOURCE_INHERITANCE_ENV_KEYS):
        raise LocalResourceInheritanceError(
            "local resource inheritance requires all four capability variables; partial state is refused"
        )
    return "inherited"


def _inherited_state_dir() -> Path:
    raw = _validated_authority_path_text(os.environ.get(LOCAL_RESOURCE_STATE_DIR_ENV, ""))
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise LocalResourceInheritanceError("inherited local resource state path is not absolute")
    try:
        resolved = candidate.resolve(strict=True)
        _validated_authority_path_text(str(resolved))
        _validate_private_state_dir(resolved, allow_absent=False)
    except (OSError, RuntimeError, LocalResourceCoordinationError) as error:
        raise LocalResourceInheritanceError(
            "inherited local resource state path is unavailable or unsafe"
        ) from error
    return resolved


def require_inherited_local_resources(
    *,
    profile: str,
    rootpath: Path | str,
) -> LocalResourceLease:
    """Validate, without mutation, one complete descendant capability."""

    if _inheritance_environment_mode() != "inherited":
        raise LocalResourceInheritanceError("local resource inheritance capability is absent")
    requested_profile = _validate_profile(profile)
    resolved_root = _resolved_rootpath(rootpath)
    run_id = os.environ.get(LOCAL_RESOURCE_RUN_ID_ENV, "")
    token = os.environ.get(LOCAL_RESOURCE_CAPABILITY_ENV, "")
    authority_profile = os.environ.get(LOCAL_RESOURCE_PROFILE_ENV, "")
    try:
        _validated_identifier(run_id, field_name="run id")
        _validate_profile(authority_profile)
    except LocalResourceStateError as error:
        raise LocalResourceInheritanceError(
            "inherited local resource identity is invalid"
        ) from error
    if len(token) != _CAPABILITY_TOKEN_BYTES * 2 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise LocalResourceInheritanceError("inherited local resource capability is invalid")
    state_dir = _inherited_state_dir()
    try:
        with _existing_state_mutex(state_dir) as mutex_exists:
            if not mutex_exists:
                raise LocalResourceInheritanceError(
                    "inherited local resource registry mutex is absent"
                )
            active = _read_active_record(state_dir)
            authority_state, authority_metadata = _probe_legacy_lock(
                _local_resource_authority_lock_path(
                    state_dir=state_dir,
                    legacy_lock_path=DEFAULT_REAL_RAY_LOCK_PATH,
                )
            )
            if active is None or authority_state != "held":
                raise LocalResourceInheritanceError(
                    "inherited local resource authority is not actively held"
                )
            public = _public_active_record(active)
            owner = cast(Mapping[str, object], public["owner"])
            child = public.get("child")
            if (
                active.get("run_id") != run_id
                or active.get("profile") != authority_profile
                or not hmac.compare_digest(
                    cast(str, active.get("capability_sha256")),
                    _capability_digest(token),
                )
                or _safe_pid(authority_metadata.get("pid")) != _safe_pid(owner.get("pid"))
                or _process_liveness(owner.get("pid"), owner.get("process_birth")) != "live"
                or not hmac.compare_digest(
                    cast(str, active.get("rootpath_sha256")),
                    _rootpath_digest(resolved_root),
                )
                or not set(LOCAL_RESOURCE_PROFILES[requested_profile]).issubset(
                    set(cast(Sequence[str], public["resources"]))
                )
            ):
                raise LocalResourceInheritanceError(
                    "inherited local resource capability does not match the active owner"
                )
            current_pid = os.getpid()
            root_pid = owner.get("pid")
            if current_pid != root_pid:
                if (
                    not isinstance(child, Mapping)
                    or _process_is_descendant(
                        current_pid,
                        child.get("pid"),
                        child.get("process_birth"),
                    )
                    != "yes"
                ):
                    raise LocalResourceInheritanceError(
                        "inherited local resource caller is not a proved launched descendant"
                    )
    except LocalResourceInheritanceError:
        raise
    except LocalResourceCoordinationError as error:
        raise LocalResourceInheritanceError(
            "inherited local resource registry cannot be trusted"
        ) from error
    return LocalResourceLease(
        run_id=run_id,
        profile=requested_profile,
        authority_profile=authority_profile,
        state_dir=state_dir,
        capability_token=token,
        inherited=True,
    )


def acquire_local_resources(
    *,
    profile: str,
    phase: str,
    rootpath: Path | str,
    selected_count: int | None = None,
    timeout_seconds: float = 14_400,
    progress_interval_seconds: float = 30,
    progress: Callable[[str], None] | None = None,
    on_acquired: Callable[[LocalResourceLease], None] | None = None,
) -> LocalResourceLease:
    """Acquire or safely borrow one fixed Phase-1 host resource profile."""

    return _acquire_local_resources(
        profile=profile,
        phase=phase,
        rootpath=rootpath,
        selected_count=selected_count,
        timeout_seconds=timeout_seconds,
        progress_interval_seconds=progress_interval_seconds,
        progress=progress,
        retained=on_acquired,
    )


def _acquire_local_resources(
    *,
    profile: str,
    phase: str,
    rootpath: Path | str,
    selected_count: int | None = None,
    timeout_seconds: float = 14_400,
    progress_interval_seconds: float = 30,
    progress: Callable[[str], None] | None = None,
    retained: Callable[[LocalResourceLease], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> LocalResourceLease:
    """Internal acquisition with a gap-free retained-lease handoff."""

    def check_cancellation() -> None:
        if cancel_requested is not None and cancel_requested():
            raise KeyboardInterrupt

    mode = _inheritance_environment_mode()
    requested_profile = _validate_profile(profile)
    validated_phase = _validated_phase(phase)
    resolved_root = _resolved_rootpath(rootpath)
    validated_count = _validated_selected_count(selected_count)
    timeout = _validated_wait_seconds(timeout_seconds, field_name="timeout", positive=False)
    progress_interval = _validated_wait_seconds(
        progress_interval_seconds,
        field_name="progress interval",
        positive=True,
    )
    if progress is not None and not callable(progress):
        raise LocalResourceStateError("local resource progress callback is invalid")
    if retained is not None and not callable(retained):
        raise LocalResourceStateError("local resource acquisition callback is invalid")
    if mode == "inherited":
        inherited = require_inherited_local_resources(
            profile=requested_profile,
            rootpath=resolved_root,
        )
        if retained is not None:
            retained(inherited)
        return inherited

    # All read-only preflight and identity discovery precedes this method's first
    # registry mutation in _register_queue_request.
    owner = _current_owner()
    source = _source_identity(resolved_root)
    intent, handoff = _request_diagnostics(requested_profile)
    check_cancellation()
    request_id = secrets.token_hex(_RUN_ID_BYTES)
    run_id = secrets.token_hex(_RUN_ID_BYTES)
    capability_token = secrets.token_hex(_CAPABILITY_TOKEN_BYTES)
    state_dir = DEFAULT_LOCAL_RESOURCE_STATE_DIR
    lock_path = DEFAULT_REAL_RAY_LOCK_PATH
    acquired: LocalResourceLease | None = None
    promoted_leases: list[LocalResourceLease] = []
    handoff_completed = False
    acquisition_error: BaseException | None = None
    try:
        # Registration itself is an uncertain atomic boundary: the queue write
        # may commit before its wrapper reports failure.  The exact request ID
        # is therefore cleanup-owned before the first mutation is attempted.
        _register_queue_request(
            state_dir=state_dir,
            lock_path=lock_path,
            request_id=request_id,
            profile=requested_profile,
            phase=validated_phase,
            owner=owner,
            source=source,
            intent=intent,
            handoff=handoff,
        )
        deadline = time.monotonic() + timeout
        next_progress = time.monotonic()
        while True:
            check_cancellation()
            acquired, position = _promote_queue_head(
                state_dir=state_dir,
                lock_path=lock_path,
                request_id=request_id,
                run_id=run_id,
                capability_token=capability_token,
                profile=requested_profile,
                phase=validated_phase,
                owner=owner,
                rootpath=resolved_root,
                intent=intent,
                handoff=handoff,
                selected_count=validated_count,
                retain_acquired=promoted_leases.append,
            )
            if acquired is not None:
                if not promoted_leases or promoted_leases[-1] is not acquired:
                    raise LocalResourceStateError(
                        "local resource promotion retention is unavailable"
                    )
                if retained is not None:
                    retained(acquired)
                handoff_completed = True
                return acquired
            now = time.monotonic()
            if progress is not None and now >= next_progress:
                progress(f"local resources queued at FIFO position {position}")
                next_progress = now + progress_interval
            if now >= deadline:
                raise LocalResourceBusyError()
            time.sleep(min(_QUEUE_POLL_SECONDS, max(0.0, deadline - now)))
    except BaseException as caught:
        acquisition_error = caught
        raise
    finally:
        if promoted_leases and not handoff_completed:
            retained_lease = promoted_leases[-1]
            try:
                if not retained_lease._released:
                    retained_lease.release(
                        outcome=(
                            "interrupted"
                            if isinstance(acquisition_error, KeyboardInterrupt)
                            else "failed"
                        ),
                        postcondition="acquisition handoff aborted before child launch",
                    )
            finally:
                _remove_queue_request(state_dir=state_dir, request_id=request_id)
        elif acquired is None:
            _remove_queue_request(state_dir=state_dir, request_id=request_id)


@dataclass(slots=True)
class _OwnedLocalCommand:
    process: subprocess.Popen[bytes]
    process_birth: str
    tree_kind: Literal["posix-process-group", "windows-job"]
    windows_job_handle: int | None = None
    posix_exit_observed: bool = False


def _linux_wait_for_process_exit_without_reaping(pid: int) -> None:
    waitid = getattr(os, "waitid", None)
    pid_type = getattr(os, "P_PID", None)
    exited = getattr(os, "WEXITED", None)
    no_wait = getattr(os, "WNOWAIT", None)
    if (
        waitid is None
        or not isinstance(pid_type, int)
        or not isinstance(exited, int)
        or not isinstance(no_wait, int)
    ):
        raise LocalResourceStateError("POSIX child exit observation is unavailable")
    while True:
        try:
            result = waitid(pid_type, pid, exited | no_wait)
        except InterruptedError:
            continue
        except (ChildProcessError, OSError) as error:
            raise LocalResourceStateError(
                "POSIX child exit could not be observed without reaping"
            ) from error
        if result is None or result.si_pid != pid:
            raise LocalResourceStateError(
                "POSIX child exit observation did not preserve exact custody"
            )
        return


def _darwin_wait_for_process_exit_without_reaping(pid: int, process_birth: str) -> None:
    try:
        import psutil
    except ImportError as error:
        raise LocalResourceStateError("POSIX child exit observation is unavailable") from error
    while True:
        if _process_liveness(pid, process_birth) != "live":
            raise LocalResourceStateError(
                "POSIX child identity changed before its exit could be observed"
            )
        try:
            status = psutil.Process(pid).status()
        except psutil.Error as error:
            raise LocalResourceStateError(
                "POSIX child exit could not be observed without reaping"
            ) from error
        if status == psutil.STATUS_ZOMBIE:
            if _process_liveness(pid, process_birth) != "live":
                raise LocalResourceStateError(
                    "POSIX child identity changed during exit observation"
                )
            return
        time.sleep(0.02)


def _wait_for_owned_launcher_exit(owned: _OwnedLocalCommand) -> None:
    if owned.tree_kind == "windows-job":
        owned.process.wait()
        return
    if sys.platform.startswith("linux"):
        _linux_wait_for_process_exit_without_reaping(owned.process.pid)
    elif sys.platform == "darwin":
        _darwin_wait_for_process_exit_without_reaping(
            owned.process.pid,
            owned.process_birth,
        )
    else:  # pragma: no cover - rejected before the contained process is created
        raise LocalResourceStateError(
            "exact local resource process custody supports only Windows, Linux, and macOS"
        )
    owned.posix_exit_observed = True


class _RecordedLaunchError(LocalResourceStateError):
    def __init__(self, owned: _OwnedLocalCommand) -> None:
        self.owned = owned
        super().__init__("contained command failed after durable child recording")


def _windows_process_handle(process: subprocess.Popen[bytes]) -> int:
    try:
        return int(vars(process)["_handle"])
    except (KeyError, TypeError, ValueError) as error:  # pragma: no cover - Windows invariant
        raise LocalResourceStateError(
            "contained Windows process omitted its native handle"
        ) from error


def _windows_pid_is_in_job(pid: int, job_handle: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    is_process_in_job = kernel32.IsProcessInJob
    is_process_in_job.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    is_process_in_job.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    process = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    try:
        result = wintypes.BOOL()
        return bool(
            is_process_in_job(
                process,
                wintypes.HANDLE(job_handle),
                ctypes.byref(result),
            )
            and result.value
        )
    finally:
        close_handle(process)


def _duplicate_windows_local_job(handle: int) -> int:
    """Retain one non-inheritable root-lease authority handle."""

    if os.name != "nt":  # pragma: no cover - guarded by Windows callers
        raise LocalResourceStateError("Windows Job duplication is unavailable")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    duplicate_handle = kernel32.DuplicateHandle
    duplicate_handle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    duplicate_handle.restype = wintypes.BOOL
    current_process = get_current_process()
    duplicated = wintypes.HANDLE()
    if not duplicate_handle(
        current_process,
        wintypes.HANDLE(handle),
        current_process,
        ctypes.byref(duplicated),
        0,
        False,
        0x00000002,  # DUPLICATE_SAME_ACCESS
    ):
        raise LocalResourceStateError(
            f"contained Windows Job duplication failed with error {ctypes.get_last_error()}"
        )
    return int(duplicated.value)


def _create_windows_local_job(process: subprocess.Popen[bytes]) -> int:
    """Assign a suspended child to a private kill-on-close Job."""

    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", _BasicLimitInformation),
            ("io_info", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_job(None, None)
    if not handle:
        raise LocalResourceStateError(
            f"contained Windows Job creation failed with error {ctypes.get_last_error()}"
        )
    information = _ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00002000
    if not set_information(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
        error_code = ctypes.get_last_error()
        close_handle(handle)
        raise LocalResourceStateError(
            f"contained Windows Job configuration failed with error {error_code}"
        )
    if not assign_process(
        handle,
        wintypes.HANDLE(_windows_process_handle(process)),
    ):
        error_code = ctypes.get_last_error()
        close_handle(handle)
        raise LocalResourceStateError(
            f"suspended Windows child could not enter its Job with error {error_code}"
        )
    return int(handle)


def _resume_windows_local_process(process: subprocess.Popen[bytes]) -> None:
    import ctypes
    from ctypes import wintypes

    resume_process = ctypes.WinDLL("ntdll").NtResumeProcess
    resume_process.argtypes = [wintypes.HANDLE]
    resume_process.restype = ctypes.c_long
    status = int(resume_process(wintypes.HANDLE(_windows_process_handle(process))))
    if status != 0:
        raise LocalResourceStateError(
            f"contained Windows child could not resume with status {status}"
        )


def _windows_job_active_processes(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("total_user_time", ctypes.c_longlong),
            ("total_kernel_time", ctypes.c_longlong),
            ("this_period_total_user_time", ctypes.c_longlong),
            ("this_period_total_kernel_time", ctypes.c_longlong),
            ("total_page_fault_count", wintypes.DWORD),
            ("total_processes", wintypes.DWORD),
            ("active_processes", wintypes.DWORD),
            ("total_terminated_processes", wintypes.DWORD),
        ]

    query = ctypes.WinDLL("kernel32", use_last_error=True).QueryInformationJobObject
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    query.restype = wintypes.BOOL
    information = _BasicAccountingInformation()
    if not query(
        wintypes.HANDLE(handle),
        1,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    ):
        raise LocalResourceStateError(
            f"contained Windows Job query failed with error {ctypes.get_last_error()}"
        )
    return int(information.active_processes)


def _terminate_windows_local_job(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    terminate = ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject
    terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate.restype = wintypes.BOOL
    if not terminate(wintypes.HANDLE(handle), _RUNNER_INTERNAL_FAILURE):
        raise LocalResourceStateError(
            f"contained Windows Job termination failed with error {ctypes.get_last_error()}"
        )


def _close_windows_local_job(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise LocalResourceStateError(
            f"contained Windows Job close failed with error {ctypes.get_last_error()}"
        )


def _wait_for_process_birth(process: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + 2.0
    while True:
        birth = _process_birth(process.pid)
        if birth is not None:
            return birth
        if process.poll() is not None or time.monotonic() >= deadline:
            raise LocalResourceStateError("contained child process birth could not be established")
        time.sleep(0.01)


def _owned_tree_presence(
    owned: _OwnedLocalCommand,
) -> Literal["present", "absent", "unknown"]:
    if owned.tree_kind == "posix-process-group":
        if owned.posix_exit_observed:
            if owned.process.returncode is not None:
                return _posix_process_group_survivor_presence(
                    owned.process.pid,
                    leader_pid=owned.process.pid,
                    leader_reaped=True,
                )
            if _process_liveness(owned.process.pid, owned.process_birth) != "live":
                return "unknown"
            return _posix_process_group_survivor_presence(
                owned.process.pid,
                leader_pid=owned.process.pid,
            )
        return _posix_process_group_presence(owned.process.pid)
    handle = owned.windows_job_handle
    if handle is None:
        return (
            "absent"
            if _process_liveness(owned.process.pid, owned.process_birth) == "dead"
            else "unknown"
        )
    try:
        return "present" if _windows_job_active_processes(handle) else "absent"
    except LocalResourceStateError:
        return "unknown"


def _wait_for_owned_tree_absence(
    owned: _OwnedLocalCommand,
    *,
    timeout_seconds: float,
) -> Literal["absent", "present", "unknown"]:
    deadline = time.monotonic() + timeout_seconds
    last: Literal["present", "absent", "unknown"] = "unknown"
    while True:
        last = _owned_tree_presence(owned)
        if last == "absent":
            return "absent"
        if time.monotonic() >= deadline:
            return last
        time.sleep(0.02)


def _close_owned_tree_boundary(owned: _OwnedLocalCommand) -> None:
    handle = owned.windows_job_handle
    if handle is None:
        return
    owned.windows_job_handle = None
    _close_windows_local_job(handle)


def _terminate_owned_tree(
    lease: LocalResourceLease,
    owned: _OwnedLocalCommand,
) -> None:
    """Stop only the exact tree durably bound to this root lease."""

    lease._verify_recorded_child(
        pid=owned.process.pid,
        process_birth=owned.process_birth,
        tree_kind=owned.tree_kind,
    )
    if owned.tree_kind == "windows-job":
        handle = owned.windows_job_handle
        if handle is None:
            raise LocalResourceStateError("contained Windows Job custody is absent")
        _terminate_windows_local_job(handle)
        try:
            owned.process.wait(timeout=_PROCESS_TREE_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise LocalResourceStateError(
                "contained child did not terminate within the shutdown bound"
            ) from error
    else:
        if getattr(owned.process, "returncode", None) is not None:
            raise LocalResourceStateError("contained POSIX process group leader was already reaped")
        _signal_exact_posix_process_group(
            owned.process.pid,
            owned.process_birth,
            signal.SIGKILL,
        )
        if not owned.posix_exit_observed:
            _wait_for_owned_launcher_exit(owned)
        if (
            _wait_for_owned_tree_absence(
                owned,
                timeout_seconds=_PROCESS_TREE_SHUTDOWN_SECONDS,
            )
            != "absent"
        ):
            raise LocalResourceStateError(
                "contained POSIX descendants did not terminate within the shutdown bound"
            )
        owned.process.wait(timeout=_PROCESS_TREE_SHUTDOWN_SECONDS)
    if (
        _wait_for_owned_tree_absence(
            owned,
            timeout_seconds=_PROCESS_TREE_SHUTDOWN_SECONDS,
        )
        != "absent"
    ):
        raise LocalResourceStateError(
            "contained process tree absence could not be proved after termination"
        )


@dataclass(slots=True)
class _UnrecordedCleanupState:
    process: subprocess.Popen[bytes]
    settle_deadline: float | None = None


def _remember_process_returncode(process: subprocess.Popen[bytes], returncode: int) -> None:
    """Make fake and interrupted Popen waits share the normal cached invariant."""

    if getattr(process, "returncode", None) is None:
        process.returncode = returncode


def _close_unrecorded_launch_stream(process: subprocess.Popen[bytes]) -> None:
    stream = process.stdin
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _settle_unrecorded_process(state: _UnrecordedCleanupState) -> None:
    process = state.process
    if getattr(process, "returncode", None) is not None:
        return
    if state.settle_deadline is None:
        state.settle_deadline = time.monotonic() + _PROCESS_TREE_SETTLE_SECONDS
    while getattr(process, "returncode", None) is None:
        try:
            returncode = process.wait(timeout=max(0.0, state.settle_deadline - time.monotonic()))
        except InterruptedError:
            continue
        except (OSError, subprocess.SubprocessError):
            return
        _remember_process_returncode(process, returncode)
        return


def _reap_unrecorded_process(process: subprocess.Popen[bytes]) -> None:
    while getattr(process, "returncode", None) is None:
        try:
            returncode = process.wait()
        except InterruptedError:
            continue
        except OSError:
            if getattr(process, "returncode", None) is not None:
                return
            time.sleep(_QUEUE_POLL_SECONDS)
            continue
        _remember_process_returncode(process, returncode)


def _complete_unrecorded_posix_cleanup(state: _UnrecordedCleanupState) -> None:
    """Idempotently prove the exact pre-GO child has been reaped."""

    process = state.process
    _close_unrecorded_launch_stream(process)
    _settle_unrecorded_process(state)
    if getattr(process, "returncode", None) is not None:
        return
    # GO was never sent, so this exact Popen child is the only possible member.
    # A direct kill also terminates a stopped shim.  Repeating the kill after an
    # asynchronous interruption is safe because an unreaped child keeps its PID.
    try:
        process.kill()
    except OSError:
        pass
    # If signaling is denied, the blocking wait deliberately retains the lane
    # rather than publishing an unproved absence.
    _reap_unrecorded_process(process)


def _complete_unrecorded_windows_cleanup(
    state: _UnrecordedCleanupState,
    *,
    job_handle: int | None,
) -> None:
    """Idempotently prove the pre-GO process and Job have no live members."""

    process = state.process
    _close_unrecorded_launch_stream(process)
    _settle_unrecorded_process(state)
    if job_handle is None:
        if getattr(process, "returncode", None) is None:
            try:
                process.kill()
            except OSError:
                pass
            _reap_unrecorded_process(process)
        return

    while True:
        try:
            active_processes = _windows_job_active_processes(job_handle)
        except LocalResourceStateError:
            active_processes = -1
        if active_processes == 0:
            break
        try:
            _terminate_windows_local_job(job_handle)
        except LocalResourceStateError:
            pass
        time.sleep(_QUEUE_POLL_SECONDS)
    _reap_unrecorded_process(process)
    while True:
        try:
            if _windows_job_active_processes(job_handle) == 0:
                return
        except LocalResourceStateError:
            pass
        try:
            _terminate_windows_local_job(job_handle)
        except LocalResourceStateError:
            pass
        time.sleep(_QUEUE_POLL_SECONDS)


def _complete_cleanup_deferring_interrupts(
    cleanup: Callable[[], None],
) -> BaseException | None:
    """Retry an idempotent whole cleanup after any asynchronous interruption."""

    deferred_error: BaseException | None = None
    while True:
        try:
            cleanup()
            break
        except BaseException as error:
            if deferred_error is None:
                deferred_error = error
    return deferred_error


def _abort_unrecorded_posix_launch(process: subprocess.Popen[bytes]) -> None:
    """Reap the exact pre-GO child before propagating an interruption."""

    state = _UnrecordedCleanupState(process=process)
    deferred_error = _complete_cleanup_deferring_interrupts(
        lambda: _complete_unrecorded_posix_cleanup(state)
    )
    if deferred_error is not None:
        raise deferred_error


def _abort_unrecorded_windows_launch(
    process: subprocess.Popen[bytes],
    *,
    job_handle: int | None,
) -> None:
    """Release exact pre-GO Job custody before propagating an interruption."""

    state = _UnrecordedCleanupState(process=process)
    deferred_error = _complete_cleanup_deferring_interrupts(
        lambda: _complete_unrecorded_windows_cleanup(state, job_handle=job_handle)
    )
    # Job absence has already been proved.  CloseHandle is non-idempotent: call
    # it exactly once outside the retry boundary.  An interruption after its
    # side effect can now propagate without making lane release untruthful.
    if job_handle is not None:
        _close_windows_local_job(job_handle)
    if deferred_error is not None:
        raise deferred_error


def _abort_unrecorded_launch(
    process: subprocess.Popen[bytes],
    *,
    tree_kind: Literal["posix-process-group", "windows-job"],
    windows_job_handle: int | None,
) -> None:
    if tree_kind == "posix-process-group":
        _abort_unrecorded_posix_launch(process)
        return
    _abort_unrecorded_windows_launch(process, job_handle=windows_job_handle)


def _release_launch_barrier(stream: BinaryIO) -> None:
    stream.write(b"G")
    stream.flush()
    stream.close()


def _resolved_contained_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> list[str]:
    """Pin a bare executable to the child environment's PATH selection."""

    executable = command[0]
    if os.path.dirname(executable):
        return list(command)
    search_path = environment.get("PATH")
    resolved = shutil.which(executable, path=search_path) if search_path is not None else None
    if resolved is None:
        raise LocalResourceStateError(
            "contained command executable was not found on the captured PATH"
        )
    return [os.path.abspath(resolved), *command[1:]]


@dataclass(slots=True)
class _MainThreadSigintGuard:
    previous_handler: object = signal.SIG_DFL
    mode: Literal["launch", "normal", "suppress", "terminal"] = "launch"
    deferred_sigint: bool = False
    sigint_observed: bool = False
    active: bool = False

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        if not self.active:
            self.previous_handler = signal.getsignal(signal.SIGINT)
            # Mark the restore obligation before installing.  Reapplying either
            # signal disposition is idempotent if an asynchronous exception lands
            # immediately before or after the native handler transition.
            self.active = True
        signal.signal(signal.SIGINT, self._capture)

    def _capture(self, _signum: int, _frame: FrameType | None) -> None:
        if self.previous_handler is signal.SIG_IGN:
            return
        if self.mode == "terminal":
            # The release outcome and replay decision are already sealed.  This
            # is the explicit transaction linearization point; later SIGINTs
            # belong to the restored caller rather than the completed run.
            return
        self.sigint_observed = True
        if self.mode != "normal":
            self.mode = "suppress"
            self.deferred_sigint = True
            return
        # The coordinator defines SIGINT as cancellation while it owns the
        # contained lifecycle.  Arbitrary prior handlers are restored later,
        # but are never invoked inside this custody boundary because a handler
        # that returns or raises another exception could make durable outcome
        # and API behavior disagree.
        self.mode = "suppress"
        raise KeyboardInterrupt

    def enter_normal_mode_after_launch(self) -> None:
        # Publish normal mode first.  A SIGINT between this store and the
        # deferred check raises inside the caller's cleanup-owning try instead
        # of being overwritten by a later normal-mode store.
        self.mode = "normal"
        if not self.deferred_sigint:
            return
        self.deferred_sigint = False
        self.mode = "suppress"
        raise KeyboardInterrupt

    def raise_deferred_before_launch(self) -> None:
        """Cancel an acquired lease before creating its contained child."""

        if not self.sigint_observed:
            return
        self.mode = "suppress"
        self.deferred_sigint = False
        raise KeyboardInterrupt

    def enter_suppress_mode(self) -> None:
        self.mode = "suppress"

    def freeze_after_release(self) -> bool:
        """Seal the outcome and ignore later SIGINT until immediate restore."""

        self.mode = "terminal"
        return self.sigint_observed

    def restore_and_replay(self) -> None:
        deferred_error: BaseException | None = None
        while self.active:
            try:
                previous_handler = self.previous_handler
                signal.signal(signal.SIGINT, previous_handler)  # type: ignore[arg-type]
                self.active = False
            except BaseException as caught:
                # Reapplying a signal disposition is idempotent even when an
                # asynchronous exception lands after the native side effect.
                if deferred_error is None:
                    deferred_error = caught
        replay_sigint = self.deferred_sigint
        self.deferred_sigint = False
        if deferred_error is not None:
            raise deferred_error
        if replay_sigint:
            raise KeyboardInterrupt


@dataclass(slots=True)
class _DeferredPopenState:
    ready: threading.Event
    permission: threading.Event
    cancelled: threading.Event
    complete: threading.Event
    process: subprocess.Popen[bytes] | None = None
    error: BaseException | None = None
    deferred_error: BaseException | None = None


def _run_deferred_popen_worker(
    state: _DeferredPopenState,
    create_process: Callable[[], subprocess.Popen[bytes]],
) -> None:
    """Create Popen away from main-thread asynchronous exception delivery."""

    state.ready.set()
    state.permission.wait()
    if state.cancelled.is_set():
        state.complete.set()
        return
    try:
        process = create_process()
    except BaseException as error:
        state.error = error
    else:
        # Python delivers process signals on the main thread.  The worker owns
        # this Popen-return/STORE_ATTR handoff, so Ctrl-C cannot discard the only
        # reference after the OS child exists.
        state.process = process
    finally:
        state.complete.set()


def _cancel_uncommitted_popen_worker(state: _DeferredPopenState) -> None:
    # Set cancellation before permission: interruption at either idempotent
    # Event transition cannot let the worker enter Popen.
    while True:
        try:
            if state.permission.is_set():
                # Permission without cancellation commits process creation.  Do
                # not propagate an unrelated boundary error until the worker has
                # retained either the Popen reference or its creation failure.
                if not state.cancelled.is_set() and not state.complete.is_set():
                    _wait_for_deferred_popen_completion(state)
                return
            state.cancelled.set()
            state.permission.set()
            return
        except BaseException as error:
            if state.deferred_error is None:
                state.deferred_error = error


def _wait_for_deferred_popen_completion(state: _DeferredPopenState) -> None:
    """Separate completion wait so tests can inject a main-thread transition."""

    state.complete.wait()


def _commit_deferred_popen_launch(state: _DeferredPopenState) -> None:
    """Defer main-thread interruption until the Popen outcome is retained."""

    while True:
        try:
            state.permission.set()
            if not state.complete.is_set():
                _wait_for_deferred_popen_completion(state)
            return
        except BaseException as error:
            if state.deferred_error is None:
                state.deferred_error = error


def _start_deferred_popen_worker(
    state: _DeferredPopenState,
    create_process: Callable[[], subprocess.Popen[bytes]],
) -> None:
    worker = threading.Thread(
        target=_run_deferred_popen_worker,
        args=(state, create_process),
        name="django-ray-local-resource-launch",
        daemon=True,
    )
    worker.start()
    state.ready.wait()


def _launch_finally_needs_cleanup(
    *,
    launch_succeeded: bool,
    launch_error: BaseException | None,
) -> bool:
    """Expose the successful-finally transition for deterministic regression tests."""

    return not launch_succeeded or launch_error is not None


def _launch_owned_command(
    *,
    lease: LocalResourceLease,
    command: Sequence[str],
    rootpath: Path,
    sigint_guard: _MainThreadSigintGuard,
) -> _OwnedLocalCommand:
    """Create custody, durably record identity, then release the GO barrier."""

    if not command or any(
        not isinstance(argument, str) or "\0" in argument for argument in command
    ):
        raise LocalResourceStateError("local resource run command is empty or invalid")
    environment = os.environ.copy()
    for key in LOCAL_RESOURCE_INHERITANCE_ENV_KEYS:
        environment.pop(key, None)
    environment.update(lease.inheritance_environment())
    contained_command = _resolved_contained_command(command, environment=environment)
    shim_command = [
        sys.executable,
        "-I",
        "-S",
        str(Path(__file__).resolve()),
        "_launch-barrier",
        "--",
        *contained_command,
    ]
    tree_kind: Literal["posix-process-group", "windows-job"]
    create_process: Callable[[], subprocess.Popen[bytes]]
    if os.name == "nt":
        tree_kind = "windows-job"

        def create_process() -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                shim_command,
                cwd=rootpath,
                env=environment,
                stdin=subprocess.PIPE,
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | 0x00000004  # CREATE_SUSPENDED
                ),
            )

    elif os.name == "posix" and (sys.platform.startswith("linux") or sys.platform == "darwin"):
        tree_kind = "posix-process-group"

        def create_process() -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                shim_command,
                cwd=rootpath,
                env=environment,
                stdin=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
    else:
        raise LocalResourceStateError(
            "exact local resource process custody supports only Windows, Linux, and macOS"
        )
    launch_state = _DeferredPopenState(
        ready=threading.Event(),
        permission=threading.Event(),
        cancelled=threading.Event(),
        complete=threading.Event(),
    )
    job_handle: int | None = None
    owned: _OwnedLocalCommand | None = None
    launch_succeeded = False
    launch_error: BaseException | None = None
    try:
        _start_deferred_popen_worker(launch_state, create_process)
        _commit_deferred_popen_launch(launch_state)
        process_error = launch_state.error
        process = launch_state.process
        if launch_state.deferred_error is not None:
            raise launch_state.deferred_error
        if process_error is not None:
            raise process_error
        if process is None:  # pragma: no cover - worker completion invariant
            raise LocalResourceStateError("contained command process outcome is absent")
        if tree_kind == "windows-job":
            job_handle = _create_windows_local_job(process)
        birth = _wait_for_process_birth(process)
        lease.record_child(
            process.pid,
            birth,
            tree_kind=tree_kind,
            windows_job_handle=job_handle,
        )
        owned = _OwnedLocalCommand(
            process=process,
            process_birth=birth,
            tree_kind=tree_kind,
            windows_job_handle=job_handle,
        )
        if tree_kind == "windows-job":
            _resume_windows_local_process(process)
        stream = process.stdin
        if stream is None:  # pragma: no cover - constructed with a pipe
            raise LocalResourceStateError("local resource launch barrier pipe is absent")
        _release_launch_barrier(stream)
        launch_succeeded = True
    except BaseException as caught:
        launch_error = caught
    finally:
        cleanup_required = _launch_finally_needs_cleanup(
            launch_succeeded=launch_succeeded,
            launch_error=launch_error,
        )
        if cleanup_required:
            transition_error: BaseException | None = None
            try:
                sigint_guard.enter_suppress_mode()
            except BaseException as caught:
                transition_error = caught
            propagated_error = transition_error or launch_error
            if propagated_error is None:  # pragma: no cover - invariant
                propagated_error = LocalResourceStateError(
                    "contained command launch failed without an error"
                )
            _cancel_uncommitted_popen_worker(launch_state)
            if owned is not None:
                raise _RecordedLaunchError(owned) from propagated_error
            process = launch_state.process
            if process is None:
                if isinstance(
                    propagated_error,
                    (OSError, subprocess.SubprocessError),
                ):
                    raise LocalResourceStateError(
                        "contained command process could not be created safely"
                    ) from propagated_error
                raise propagated_error
            cleanup_error: BaseException | None = None
            try:
                _abort_unrecorded_launch(
                    process,
                    tree_kind=tree_kind,
                    windows_job_handle=job_handle,
                )
            except BaseException as cleanup_caught:
                # The abort boundary raises deferred errors only after exact
                # absence and the one-shot Job close attempt.
                cleanup_error = cleanup_caught
            if cleanup_error is not None:
                raise cleanup_error from propagated_error
            if isinstance(
                propagated_error,
                (OSError, subprocess.SubprocessError),
            ):
                raise LocalResourceStateError(
                    "contained command setup failed before durable custody"
                ) from propagated_error
            raise propagated_error
    if owned is None:  # pragma: no cover - successful launch invariant
        raise LocalResourceStateError("contained command custody is absent after launch")
    return owned


def _finish_owned_command(
    *,
    lease: LocalResourceLease,
    owned: _OwnedLocalCommand,
) -> tuple[int, bool]:
    """Prove tree absence, terminating post-launcher survivors if necessary."""

    presence = _wait_for_owned_tree_absence(
        owned,
        timeout_seconds=_PROCESS_TREE_NATURAL_EXIT_SECONDS,
    )
    survivors = presence != "absent"
    if survivors:
        _terminate_owned_tree(lease, owned)
    else:
        owned.process.wait()
        if (
            _wait_for_owned_tree_absence(
                owned,
                timeout_seconds=_PROCESS_TREE_SHUTDOWN_SECONDS,
            )
            != "absent"
        ):
            raise LocalResourceStateError(
                "contained process tree absence changed while reaping its leader"
            )
    return_code = owned.process.returncode
    if return_code is None:
        raise LocalResourceStateError("contained launcher exit status is unavailable")
    lease.clear_child()
    _close_owned_tree_boundary(owned)
    return return_code, survivors


def _cleanup_owned_command_after_error(
    *,
    lease: LocalResourceLease,
    owned: _OwnedLocalCommand,
) -> None:
    """Retry one bounded cleanup failure without discarding tree custody."""

    last_error: BaseException | None = None
    for _attempt in range(2):
        try:
            stream = owned.process.stdin
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
            presence = _wait_for_owned_tree_absence(owned, timeout_seconds=0)
            if presence != "absent":
                _terminate_owned_tree(lease, owned)
            elif (
                owned.tree_kind == "posix-process-group"
                and owned.posix_exit_observed
                and owned.process.returncode is None
            ):
                owned.process.wait(timeout=_PROCESS_TREE_SHUTDOWN_SECONDS)
                if (
                    _wait_for_owned_tree_absence(
                        owned,
                        timeout_seconds=_PROCESS_TREE_SHUTDOWN_SECONDS,
                    )
                    != "absent"
                ):
                    raise LocalResourceStateError(
                        "contained process tree absence changed while reaping its leader"
                    )
            lease.clear_child()
            _close_owned_tree_boundary(owned)
            return
        except BaseException as error:
            last_error = error
    raise LocalResourceStateError(
        "contained command cleanup could not prove its release postcondition"
    ) from last_error


def _normalized_contained_command_error(error: BaseException) -> BaseException:
    if isinstance(error, (OSError, subprocess.SubprocessError)):
        return LocalResourceStateError("contained command failed within its owned boundary")
    return error


def run_local_resource_command(
    *,
    profile: str,
    phase: str,
    rootpath: Path | str,
    timeout_seconds: float,
    command: Sequence[str],
    progress: Callable[[str], None] | None = None,
) -> int:
    """Acquire, launch behind a containment barrier, settle, and release."""

    if not command or any(
        not isinstance(argument, str) or "\0" in argument for argument in command
    ):
        raise LocalResourceStateError("local resource run command is empty or invalid")
    if os.name != "nt" and not (
        os.name == "posix" and (sys.platform.startswith("linux") or sys.platform == "darwin")
    ):
        raise LocalResourceStateError(
            "exact local resource process custody supports only Windows, Linux, and macOS"
        )
    resolved_root = _resolved_rootpath(rootpath)
    sigint_guard = _MainThreadSigintGuard()
    acquisition_error = _complete_cleanup_deferring_interrupts(sigint_guard.install)
    retained_leases: list[LocalResourceLease] = []
    if acquisition_error is None:
        try:
            _acquire_local_resources(
                profile=profile,
                phase=phase,
                rootpath=resolved_root,
                timeout_seconds=timeout_seconds,
                progress=progress,
                retained=retained_leases.append,
                cancel_requested=lambda: sigint_guard.sigint_observed,
            )
        except BaseException as caught:
            acquisition_error = caught
    if not retained_leases:
        sigint_guard.enter_suppress_mode()
        replayed_sigint: BaseException | None = None
        try:
            sigint_guard.restore_and_replay()
        except BaseException as caught:
            replayed_sigint = caught
        if isinstance(
            acquisition_error,
            (
                LocalResourceStateError,
                LocalResourceStatePathError,
                RealRayOwnershipPathError,
            ),
        ):
            # A deferred Ctrl-C cannot prove that the registered request was
            # removed.  Preserve the coordination failure so callers cannot
            # mistake a potentially live FIFO wedge for clean cancellation.
            raise acquisition_error
        if replayed_sigint is not None:
            raise replayed_sigint from acquisition_error
        if acquisition_error is not None:
            raise acquisition_error
        raise LocalResourceStateError("local resource acquisition outcome is unavailable")
    lease = retained_leases[0]
    operation_error = acquisition_error
    owned: _OwnedLocalCommand | None = None
    result_code: int | None = None
    success_outcome = "failed"
    success_postcondition = "owned process tree absent"
    cleanup_error: BaseException | None = None
    try:
        if lease.inherited:
            raise LocalResourceInheritanceError(
                "local resource run must be the root owner, not an inherited borrower"
            )
        sigint_guard.raise_deferred_before_launch()
        if operation_error is None:
            owned = _launch_owned_command(
                lease=lease,
                command=command,
                rootpath=resolved_root,
                sigint_guard=sigint_guard,
            )
            # The caller now holds the recorded object before any launch-phase
            # SIGINT is dispatched and allowed to initiate an unwind.
            sigint_guard.enter_normal_mode_after_launch()
            _wait_for_owned_launcher_exit(owned)
            return_code, survivors = _finish_owned_command(lease=lease, owned=owned)
            owned = None
            success_outcome = "passed" if return_code == 0 and not survivors else "failed"
            success_postcondition = (
                "owned process tree absent"
                if not survivors
                else "post-launcher descendants terminated; owned process tree absent"
            )
            result_code = (
                _RUNNER_INTERNAL_FAILURE
                if survivors
                else return_code
                if return_code >= 0
                else 128 + abs(return_code)
            )
        # Leave the protected command path only after SIGINT has become a
        # non-raising deferred request.  A first SIGINT before this call still
        # raises inside this try and is captured by the except below.
        sigint_guard.enter_suppress_mode()
    except BaseException as caught:
        operation_error = caught
        if isinstance(caught, _RecordedLaunchError):
            owned = caught.owned
            cause = caught.__cause__
            if isinstance(cause, BaseException):
                operation_error = cause
        # If SIGINT arrives anywhere in this handler before this store, its
        # capture latches the cancellation and switches to suppress mode; the
        # finally below therefore remains cleanup-owning without consulting an
        # ambient interpreter exception.
        sigint_guard.enter_suppress_mode()
    finally:
        effective_error = operation_error
        if owned is not None:
            try:
                _cleanup_owned_command_after_error(lease=lease, owned=owned)
                owned = None
            except BaseException as caught:
                cleanup_error = caught
        release_error = effective_error is not None
        if owned is None and cleanup_error is None:
            try:
                if lease._child_recorded or lease._windows_child_job_handle is not None:
                    lease.clear_child()

                def resolve_completion(final: bool) -> tuple[str, str | None]:
                    interrupted = (
                        isinstance(effective_error, KeyboardInterrupt)
                        or sigint_guard.sigint_observed
                    )
                    if final:
                        interrupted = sigint_guard.freeze_after_release() or interrupted
                    if interrupted:
                        return (
                            "interrupted",
                            "owned process tree absent before error propagation",
                        )
                    if release_error:
                        return (
                            "failed",
                            "owned process tree absent before error propagation",
                        )
                    if not final and success_outcome == "passed":
                        # The temporary conservative outcome is hidden behind
                        # the state mutex until OS unlock and signal freeze are
                        # both complete.
                        return (
                            "failed",
                            "owned process tree absent before completion commit",
                        )
                    return success_outcome, success_postcondition

                lease.release(
                    outcome="failed",
                    postcondition="owned process tree absent before completion commit",
                    _completion_resolver=resolve_completion,
                )
            except BaseException as caught:
                cleanup_error = caught
        replayed_sigint: BaseException | None = None
        try:
            # Exact absence, durable child clearing, both one-shot handle
            # closes, and lane release precede restoration and replay.
            sigint_guard.restore_and_replay()
        except BaseException as caught:
            replayed_sigint = caught
        completion_is_interrupted = (
            lease._released
            and lease._pending_release is not None
            and lease._pending_release[0] == "interrupted"
        )
        if replayed_sigint is not None and completion_is_interrupted:
            raise replayed_sigint from cleanup_error or effective_error
        if cleanup_error is not None:
            raise LocalResourceStateError(
                "contained command cleanup could not prove its release postcondition"
            ) from cleanup_error
        if replayed_sigint is not None:
            raise replayed_sigint from effective_error
    if operation_error is not None:
        normalized = _normalized_contained_command_error(operation_error)
        if normalized is operation_error:
            raise operation_error
        raise normalized from operation_error
    if result_code is None:  # pragma: no cover - successful run invariant
        raise LocalResourceStateError("contained command result is unavailable")
    return result_code


def _launch_barrier_main(command: Sequence[str]) -> int:
    if not command:
        return _RUNNER_INTERNAL_FAILURE
    try:
        go = sys.stdin.buffer.read(1)
    except OSError:
        return _RUNNER_INTERNAL_FAILURE
    if go != b"G":
        return _RUNNER_INTERNAL_FAILURE
    try:
        process = subprocess.Popen(command)
        return_code = process.wait()
    except OSError:
        return 126
    return return_code if return_code >= 0 else 128 + abs(return_code)


def _observed_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _empty_status(*, state: str, safe_action: str, local_liveness: str) -> dict[str, object]:
    return {
        "schema_version": LOCAL_RESOURCE_SCHEMA_VERSION,
        "observed_at": _observed_at(),
        "state": state,
        "safe_action": safe_action,
        "termination_authority": "none",
        "resources": [HOST_HEAVY_RESOURCE],
        "active": None,
        "queue": [],
        "queue_total": 0,
        "orphaned": [],
        "last_completed": None,
        "local_liveness": local_liveness,
        "state_registry": "unknown",
        "kubernetes_mirror": {"state": "not-configured"},
        "deployed_stack": {
            "state": "not-observed",
            "provenance": "not-configured",
        },
        "diagnostics": [],
    }


def _legacy_active_status(metadata: Mapping[str, object]) -> dict[str, object]:
    hostname = metadata.get("hostname")
    host_id = None
    if isinstance(hostname, str):
        host_id = "sha256:" + hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:16]
    owner = LocalResourceOwner(
        host_id=host_id,
        pid=cast(int | None, metadata.get("pid")),
    )
    source = LocalResourceSource(worktree=cast(str | None, metadata.get("rootpath")))
    selected_count = metadata.get("selected_count")
    return {
        "run_id": None,
        "profile": "real-ray",
        "resources": [HOST_HEAVY_RESOURCE],
        "phase": "legacy-pytest",
        "queue_position": 0,
        "owner": owner.as_dict(),
        "source": source.as_dict(),
        "intent": "legacy real_ray pytest ownership",
        "handoff": None,
        "acquired_at": _bounded_status_text(metadata.get("acquired_at")),
        "heartbeat_at": None,
        "expiry_at": None,
        "selected_count": (
            selected_count
            if isinstance(selected_count, int) and not isinstance(selected_count, bool)
            else None
        ),
        "child": None,
        "outcome": None,
        "postcondition": None,
        "liveness": "os-lock-held",
        "legacy": True,
    }


def _lock_contention(error: OSError) -> bool:
    return error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def _unknown_status(
    *, code: str, message: object, local_liveness: str, state_registry: str
) -> dict[str, object]:
    status = _empty_status(
        state="unknown",
        safe_action="investigate",
        local_liveness=local_liveness,
    )
    status["state_registry"] = state_registry
    status["diagnostics"] = [
        {
            "code": code,
            "message": _bounded_status_text(str(message)) or "local resource status is unavailable",
        }
    ]
    return status


def _probe_legacy_lock(lock_path: Path) -> tuple[str, dict[str, object]]:
    try:
        _validate_lock_parent(lock_path)
        descriptor = _open_lock_descriptor(lock_path, create=False)
    except FileNotFoundError:
        return "free", {}
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    try:
        try:
            _try_advisory_lock(handle)
        except OSError as error:
            if not _lock_contention(error):
                raise LocalResourceStateError(
                    "legacy ownership lock could not be inspected safely"
                ) from error
            return "held", _read_owner_metadata(handle)
        _release_advisory_lock(handle)
        return "free", {}
    finally:
        handle.close()


def _probe_legacy_compatibility_lock(lock_path: Path) -> tuple[str, dict[str, object]]:
    """Inspect only a same-user legacy bridge; a foreign POSIX inode is irrelevant."""

    if not _legacy_compatibility_is_current_user(lock_path):
        return "foreign-user", {}
    try:
        return _probe_legacy_lock(lock_path)
    except RealRayOwnershipPathError:
        # Close the absent-path race in the same way as composite acquisition.
        if _legacy_compatibility_is_current_user(lock_path):
            raise
        return "foreign-user", {}


def read_local_resource_status(
    *,
    lock_path: Path = DEFAULT_REAL_RAY_LOCK_PATH,
    state_dir: Path = DEFAULT_LOCAL_RESOURCE_STATE_DIR,
) -> dict[str, object]:
    """Read bounded host ownership status without creating or writing local state."""

    try:
        state_dir_exists = _validate_private_state_dir(state_dir, allow_absent=True)
    except LocalResourceStatePathError as error:
        return _unknown_status(
            code="state-path-unsafe",
            message=error,
            local_liveness="state-path-unsafe",
            state_registry="unsafe",
        )

    active_record: dict[str, object] | None = None
    queue: list[dict[str, object]] = []
    last_completed: dict[str, object] | None = None
    authority_state = "free"
    authority_metadata: dict[str, object] = {}
    legacy_state = "free"
    legacy_metadata: dict[str, object] = {}
    authority_path = _local_resource_authority_lock_path(
        state_dir=state_dir,
        legacy_lock_path=lock_path,
    )

    def read_registry_records() -> None:
        nonlocal active_record, queue, last_completed
        active_record = _read_active_record(state_dir)
        queue_state = _read_queue_state(state_dir)
        queue_items = _validated_queue_items(queue_state)
        queue = []
        for position, item in enumerate(queue_items, start=1):
            public_item = _public_queue_item(item)
            public_item["queue_position"] = position
            queue.append(public_item)
        completed_record = _read_state_json(_state_file_path(state_dir, LAST_COMPLETED_STATE_FILE))
        if completed_record is not None:
            last_completed = _public_last_completed(completed_record)

    try:
        if state_dir_exists:
            with _existing_state_mutex(state_dir) as mutex_exists:
                if not mutex_exists:
                    unexpected = any(
                        _read_state_json(_state_file_path(state_dir, filename)) is not None
                        for filename in (
                            ACTIVE_STATE_FILE,
                            QUEUE_STATE_FILE,
                            LAST_COMPLETED_STATE_FILE,
                        )
                    )
                    if unexpected:
                        raise LocalResourceStateError(
                            "local resource registry exists without its control mutex"
                        )
                else:
                    read_registry_records()
                authority_state, authority_metadata = _probe_legacy_lock(authority_path)
        elif authority_path == lock_path:
            authority_state, authority_metadata = _probe_legacy_lock(authority_path)
        if authority_path == lock_path:
            legacy_state, legacy_metadata = authority_state, authority_metadata
        else:
            legacy_state, legacy_metadata = _probe_legacy_compatibility_lock(lock_path)
    except RealRayOwnershipPathError as error:
        return _unknown_status(
            code="legacy-lock-path-unsafe",
            message=error,
            local_liveness="legacy-lock-path-unsafe",
            state_registry="present" if state_dir_exists else "absent",
        )
    except LocalResourceCoordinationError as error:
        message = str(error)
        legacy_failure = message.startswith("legacy ownership lock")
        return _unknown_status(
            code="legacy-lock-unavailable" if legacy_failure else "state-registry-corrupt",
            message=error,
            local_liveness=(
                "legacy-lock-unavailable" if legacy_failure else "state-registry-unavailable"
            ),
            state_registry="corrupt" if state_dir_exists else "absent",
        )

    state_registry = "present" if state_dir_exists else "absent"
    status = _empty_status(state="idle", safe_action="proceed", local_liveness="idle")
    status["state_registry"] = state_registry
    status["queue_total"] = len(queue)
    status["queue"] = queue[:_MAX_STATUS_QUEUE_ITEMS]
    status["last_completed"] = last_completed
    if legacy_state == "foreign-user":
        status["diagnostics"] = [
            {
                "code": "foreign-legacy-lock-ignored",
                "message": (
                    "the fixed legacy lock belongs to another OS user and is outside this "
                    "coordination boundary"
                ),
            }
        ]

    if active_record is None and authority_state == "held":
        status["state"] = "legacy-held"
        status["safe_action"] = "wait"
        status["local_liveness"] = "os-lock-held"
        status["active"] = _legacy_active_status(authority_metadata)
        return status

    if active_record is not None and authority_state == "held":
        active = _public_active_record(active_record)
        owner = cast(Mapping[str, object], active["owner"])
        authority_pid = _safe_pid(authority_metadata.get("pid"))
        owner_pid = _safe_pid(owner.get("pid"))
        owner_birth = owner.get("process_birth")
        owner_liveness = _process_liveness(owner_pid, owner_birth)
        compatibility_pid = (
            _safe_pid(legacy_metadata.get("pid")) if legacy_state == "held" else owner_pid
        )
        if (
            authority_pid is None
            or owner_pid is None
            or authority_pid != owner_pid
            or compatibility_pid != owner_pid
            or not isinstance(owner_birth, str)
            or owner_liveness != "live"
        ):
            return _unknown_status(
                code="active-lock-identity-conflict",
                message=("active registry PID and process birth do not prove the held lock owner"),
                local_liveness="identity-conflict",
                state_registry=state_registry,
            )
        status["state"] = "active"
        status["safe_action"] = "wait"
        status["local_liveness"] = "os-lock-held"
        status["active"] = active
        return status

    if active_record is None and authority_state != "held" and legacy_state == "held":
        status["state"] = "legacy-held"
        status["safe_action"] = "wait"
        status["local_liveness"] = "os-lock-held"
        status["active"] = _legacy_active_status(legacy_metadata)
        return status

    if active_record is not None:
        active = _public_active_record(active_record, owner_lock_held=False)
        owner = cast(Mapping[str, object], active["owner"])
        root_liveness = _process_liveness(owner.get("pid"), owner.get("process_birth"))
        child = active.get("child")
        child_liveness = child.get("liveness") if isinstance(child, Mapping) else "dead"
        if root_liveness == "live":
            return _unknown_status(
                code="active-owner-without-lock",
                message="the recorded owner is live but the authoritative OS lock is free",
                local_liveness="identity-conflict",
                state_registry=state_registry,
            )
        if root_liveness == "unknown":
            return _unknown_status(
                code="active-owner-liveness-unknown",
                message="the recorded owner cannot be proved absent while the OS lock is free",
                local_liveness="owner-liveness-unknown",
                state_registry=state_registry,
            )
        if child_liveness == "unknown":
            return _unknown_status(
                code="active-child-liveness-unknown",
                message=(
                    "the recorded child or process tree cannot be proved absent while the OS lock is free"
                ),
                local_liveness="child-liveness-unknown",
                state_registry=state_registry,
            )
        if child_liveness == "live":
            active["liveness"] = "orphaned-child-live"
            status["state"] = "orphaned"
            status["safe_action"] = "investigate"
            status["local_liveness"] = "orphaned-child-live"
            status["orphaned"] = [active]
            return status
        if legacy_state == "held":
            status["state"] = "legacy-held"
            status["safe_action"] = "wait"
            status["local_liveness"] = "os-lock-held"
            status["active"] = _legacy_active_status(legacy_metadata)
            return status
        status["diagnostics"] = [
            {
                "code": "stale-active-record",
                "message": "the OS lock is free and no recorded process identity remains live",
            }
        ]

    queue_liveness = {item.get("liveness") for item in queue}
    if "unknown" in queue_liveness:
        status["state"] = "unknown"
        status["safe_action"] = "investigate"
        status["local_liveness"] = "queue-liveness-unknown"
        status["diagnostics"] = [
            {
                "code": "queue-requester-liveness-unknown",
                "message": "a queued requester cannot be proved live or absent",
            }
        ]
    elif "live" in queue_liveness:
        status["state"] = "waiting"
        status["safe_action"] = "wait"
        status["local_liveness"] = "queue-present"
    elif queue:
        diagnostics = cast(list[object], status["diagnostics"])
        diagnostics.append(
            {
                "code": "stale-queue-entries",
                "message": "all queued requester identities are proved absent and may be pruned by acquisition",
            }
        )
    return status


def _render_status_scalar(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    bounded = _bounded_status_text(value)
    return bounded if bounded else "unavailable"


def _append_identity_context(
    lines: list[str],
    *,
    prefix: str,
    record: Mapping[str, object],
) -> None:
    owner = record.get("owner")
    owner = owner if isinstance(owner, Mapping) else {}
    source = record.get("source")
    source = source if isinstance(source, Mapping) else {}
    lines.extend(
        [
            f"{prefix} owner: {_render_status_scalar(owner.get('owner'))}",
            f"{prefix} session: {_render_status_scalar(owner.get('session'))}",
            f"{prefix} agent: {_render_status_scalar(owner.get('agent'))}",
            f"{prefix} model: {_render_status_scalar(owner.get('model'))}",
            f"{prefix} host: {_render_status_scalar(owner.get('host_id'))}",
            f"{prefix} PID: {_render_status_scalar(owner.get('pid'))}",
            f"{prefix} process birth: {_render_status_scalar(owner.get('process_birth'))}",
            f"{prefix} intent: {_render_status_scalar(record.get('intent'))}",
            f"{prefix} handoff: {_render_status_scalar(record.get('handoff'))}",
            f"{prefix} worktree: {_render_status_scalar(source.get('worktree'))}",
            f"{prefix} branch: {_render_status_scalar(source.get('branch'))}",
            f"{prefix} commit: {_render_status_scalar(source.get('commit'))}",
            f"{prefix} source tree: {_render_status_scalar(source.get('source_tree'))}",
            f"{prefix} source dirty: {_render_status_scalar(source.get('dirty'))}",
        ]
    )


def render_local_resource_status(
    status: Mapping[str, object], *, output_format: Literal["text", "json"] = "text"
) -> str:
    """Render one stable bounded status snapshot without performing another read."""

    if output_format == "json":
        return json.dumps(status, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    if output_format != "text":
        raise ValueError("local resource status format must be 'text' or 'json'")

    lines = [
        f"Local resources: {status.get('state', 'unavailable')}",
        f"Safe action: {status.get('safe_action', 'investigate')}",
        f"Termination authority: {status.get('termination_authority', 'none')}",
    ]
    active = status.get("active")
    if isinstance(active, Mapping):
        child = active.get("child")
        child_summary = "none"
        if isinstance(child, Mapping):
            child_summary = ", ".join(
                (
                    f"pid={_render_status_scalar(child.get('pid'))}",
                    f"birth={_render_status_scalar(child.get('process_birth'))}",
                    f"tree={_render_status_scalar(child.get('tree_kind'))}",
                    f"liveness={_render_status_scalar(child.get('liveness'))}",
                )
            )
        lines.extend(
            [
                f"Profile: {_render_status_scalar(active.get('profile'))}",
                "Resources: "
                + ", ".join(
                    _render_status_scalar(resource)
                    for resource in active.get("resources", [])
                    if isinstance(resource, str)
                ),
                f"Phase: {_render_status_scalar(active.get('phase'))}",
                f"Queue position: {_render_status_scalar(active.get('queue_position'))} (active)",
                f"Child: {child_summary}",
            ]
        )
        _append_identity_context(lines, prefix="Active", record=active)
    orphaned = status.get("orphaned")
    if isinstance(orphaned, Sequence) and not isinstance(orphaned, (str, bytes)):
        rendered_orphans = min(len(orphaned), _MAX_STATUS_QUEUE_ITEMS)
        for index in range(rendered_orphans):
            orphan = orphaned[index]
            if not isinstance(orphan, Mapping):
                continue
            position = index + 1
            resources = orphan.get("resources")
            resource_text = (
                ", ".join(
                    _render_status_scalar(resource)
                    for resource in resources
                    if isinstance(resource, str)
                )
                if isinstance(resources, Sequence) and not isinstance(resources, (str, bytes))
                else "unavailable"
            )
            child = orphan.get("child")
            child_summary = "none"
            if isinstance(child, Mapping):
                child_summary = ", ".join(
                    (
                        f"pid={_render_status_scalar(child.get('pid'))}",
                        f"birth={_render_status_scalar(child.get('process_birth'))}",
                        f"tree={_render_status_scalar(child.get('tree_kind'))}",
                        f"liveness={_render_status_scalar(child.get('liveness'))}",
                    )
                )
            lines.extend(
                [
                    f"Orphan {position}: profile={_render_status_scalar(orphan.get('profile'))}, "
                    f"resources={resource_text}, "
                    f"phase={_render_status_scalar(orphan.get('phase'))}, "
                    f"liveness={_render_status_scalar(orphan.get('liveness'))}",
                    f"Orphan {position} child: {child_summary}",
                ]
            )
            _append_identity_context(lines, prefix=f"Orphan {position}", record=orphan)
        if len(orphaned) > rendered_orphans:
            lines.append(f"Orphan entries omitted: {len(orphaned) - rendered_orphans}")
    queue = status.get("queue")
    if isinstance(queue, Sequence) and not isinstance(queue, (str, bytes)):
        queue_total = status.get("queue_total")
        if queue or isinstance(queue_total, int) and queue_total:
            lines.append(f"Queue total: {_render_status_scalar(queue_total)}")
        for position, item in enumerate(queue, start=1):
            if not isinstance(item, Mapping):
                continue
            queue_position = item.get("queue_position", position)
            lines.append(
                "Queue position "
                f"{_render_status_scalar(queue_position)}: "
                f"ticket={_render_status_scalar(item.get('ticket'))}, "
                f"profile={_render_status_scalar(item.get('profile'))}, "
                f"phase={_render_status_scalar(item.get('phase'))}, "
                f"liveness={_render_status_scalar(item.get('liveness'))}"
            )
            _append_identity_context(
                lines,
                prefix=f"Queue {_render_status_scalar(queue_position)}",
                record=item,
            )
        if isinstance(queue_total, int) and queue_total > len(queue):
            lines.append(f"Queue entries omitted: {queue_total - len(queue)}")
    last_completed = status.get("last_completed")
    if isinstance(last_completed, Mapping):
        lines.extend(
            [
                "Last completion: "
                f"profile={_render_status_scalar(last_completed.get('profile'))}, "
                f"outcome={_render_status_scalar(last_completed.get('outcome'))}, "
                f"completed_at={_render_status_scalar(last_completed.get('completed_at'))}",
                f"Last postcondition: {_render_status_scalar(last_completed.get('postcondition'))}",
            ]
        )
        _append_identity_context(lines, prefix="Last", record=last_completed)
    kubernetes = status.get("kubernetes_mirror")
    kubernetes_state = (
        kubernetes.get("state", "unavailable") if isinstance(kubernetes, Mapping) else "unavailable"
    )
    lines.append(f"Kubernetes mirror: {kubernetes_state}")
    diagnostics = status.get("diagnostics")
    if isinstance(diagnostics, Sequence):
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, Mapping):
                continue
            code = diagnostic.get("code")
            message = diagnostic.get("message")
            if isinstance(code, str) and isinstance(message, str):
                lines.append(f"Diagnostic [{code}]: {message}")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="read local resource ownership status")
    status_parser.add_argument("--format", choices=("text", "json"), default="text")
    run_parser = subparsers.add_parser(
        "run",
        help="acquire local resources and run one contained noninteractive command",
    )
    run_parser.add_argument("--profile", choices=tuple(LOCAL_RESOURCE_PROFILES), required=True)
    run_parser.add_argument("--phase", required=True)
    run_parser.add_argument("--root", type=Path, required=True)
    run_parser.add_argument("--timeout", type=float, default=14_400)
    run_parser.add_argument("run_command", nargs=argparse.REMAINDER)
    inherited_parser = subparsers.add_parser(
        "require-inherited",
        help="fail closed unless this process has a valid inherited lease",
    )
    inherited_parser.add_argument(
        "--profile",
        choices=tuple(LOCAL_RESOURCE_PROFILES),
        required=True,
    )
    inherited_parser.add_argument("--root", type=Path, required=True)
    barrier_parser = subparsers.add_parser("_launch-barrier", help=argparse.SUPPRESS)
    barrier_parser.add_argument("barrier_command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        status = read_local_resource_status()
        print(
            render_local_resource_status(
                status,
                output_format=args.format,
            ),
            end="",
        )
        return 3 if status.get("state") == "unknown" else 0
    if args.command == "_launch-barrier":
        barrier_command = list(args.barrier_command)
        if barrier_command[:1] == ["--"]:
            barrier_command = barrier_command[1:]
        return _launch_barrier_main(barrier_command)
    if args.command == "require-inherited":
        try:
            lease = require_inherited_local_resources(
                profile=args.profile,
                rootpath=args.root,
            )
        except LocalResourceCoordinationError as error:
            message = _bounded_status_text(str(error)) or "local resource inheritance failed"
            print(f"FAILED [local-resources]: {message}", file=sys.stderr)
            return 4
        print(f"Local resource inheritance verified for profile {lease.profile}.")
        return 0
    if args.command == "run":
        run_command = list(args.run_command)
        if run_command[:1] == ["--"]:
            run_command = run_command[1:]
        if not run_command:
            print(
                "FAILED [local-resources]: run requires a command after --",
                file=sys.stderr,
            )
            return 2
        try:
            return run_local_resource_command(
                profile=args.profile,
                phase=args.phase,
                rootpath=args.root,
                timeout_seconds=args.timeout,
                command=run_command,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        except KeyboardInterrupt:
            return 130
        except LocalResourceCoordinationError as error:
            message = _bounded_status_text(str(error)) or "local resource run failed"
            print(f"FAILED [local-resources]: {message}", file=sys.stderr)
            return 4
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover - exercised through CLI subprocess tests
    raise SystemExit(main())
