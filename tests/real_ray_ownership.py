"""Host-wide advisory ownership for pytest sessions that run local Ray."""

from __future__ import annotations

import json
import os
import socket
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
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            try:
                _try_advisory_lock(handle)
            except OSError as error:
                current_owner = _read_owner_metadata(handle)
                raise RealRayOwnershipUnavailableError(self.path, current_owner) from error

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
