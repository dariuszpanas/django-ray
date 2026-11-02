"""Node-local workflow preparation spill capacity reservation and janitor (#158).

Bounds aggregate concurrent spill workspace reservations, workspace counts,
per-owner counts, and filesystem free-space headroom across processes on one
node, and provides a conservative janitor for proven-stale package-owned
workspaces and quarantines.
"""

from __future__ import annotations

import atexit
import errno
import json
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from secrets import token_hex
from threading import Lock
from typing import Any
from uuid import UUID

from django_ray.workflow.progress.preparation import (
    _DATABASE_NAME,
    _LEASE_NAME,
    _QUARANTINE_PREFIX,
    _WORKSPACE_PREFIX,
    SQLITE_SPILL_MAX_BYTES,
    _PathIdentity,
)

DEFAULT_MAX_AGGREGATE_SPILL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
DEFAULT_MAX_SPILL_WORKSPACES = 8
DEFAULT_MAX_WORKSPACES_PER_OWNER = 2
DEFAULT_MIN_FREE_HEADROOM_BYTES = 512 * 1024 * 1024  # 512 MiB
DEFAULT_STALE_LEASE_GRACE_SECONDS = 30.0
_RESERVATION_SUBDIR = "reservations"
_LOCK_DIR_NAME = ".django-ray-spill-capacity.lock"
_RESERVATION_PREFIX = "reservation-"
_RESERVATION_SUFFIX = ".json"


class WorkflowProgressSpillCapacityError(RuntimeError):
    """Base error for node-local spill capacity ownership and admission."""


class WorkflowProgressSpillCapacityConfigurationError(
    WorkflowProgressSpillCapacityError, ValueError
):
    """Raised when spill capacity configuration or root ownership is invalid."""


class WorkflowProgressSpillCapacityExhaustedError(WorkflowProgressSpillCapacityError):
    """Raised when aggregate byte, workspace-count, per-owner, or headroom limits are exceeded."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class WorkflowProgressSpillCapacityUnavailableError(WorkflowProgressSpillCapacityError):
    """Raised when aggregate capacity ownership is unavailable or inconsistent."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SpillOwnerDisposition(StrEnum):
    """Conservative classification of a discovered spill workspace or reservation."""

    LIVE = "live"
    INDETERMINATE = "indeterminate"
    STALE = "stale"


class SpillCleanupOutcome(StrEnum):
    """Bounded cleanup outcome for one workspace or quarantine candidate."""

    RECLAIMED = "reclaimed"
    SKIPPED_LIVE = "skipped_live"
    SKIPPED_INDETERMINATE = "skipped_indeterminate"
    REFUSED = "refused"
    OPERATIONAL_FAILURE = "operational_failure"


@dataclass(frozen=True)
class SpillCapacityConfig:
    """Explicit aggregate node-local spill capacity configuration (#158)."""

    root_directory: Path
    max_aggregate_bytes: int = DEFAULT_MAX_AGGREGATE_SPILL_BYTES
    max_workspaces: int = DEFAULT_MAX_SPILL_WORKSPACES
    max_workspaces_per_owner: int = DEFAULT_MAX_WORKSPACES_PER_OWNER
    min_free_headroom_bytes: int = DEFAULT_MIN_FREE_HEADROOM_BYTES
    stale_lease_grace_seconds: float = DEFAULT_STALE_LEASE_GRACE_SECONDS
    lock_timeout_seconds: float = 2.0

    def validated(self) -> SpillCapacityConfig:
        if not isinstance(self.root_directory, Path):
            raise WorkflowProgressSpillCapacityConfigurationError(
                "root_directory must be a pathlib.Path"
            )
        int_fields = {
            "max_aggregate_bytes": self.max_aggregate_bytes,
            "max_workspaces": self.max_workspaces,
            "max_workspaces_per_owner": self.max_workspaces_per_owner,
            "min_free_headroom_bytes": self.min_free_headroom_bytes,
        }
        for name, value in int_fields.items():
            if type(value) is not int:
                raise WorkflowProgressSpillCapacityConfigurationError(f"{name} must be an integer")
        if self.max_aggregate_bytes < 64 * 1024:
            raise WorkflowProgressSpillCapacityConfigurationError(
                "max_aggregate_bytes must be at least 64 KiB"
            )
        if self.max_workspaces < 1 or self.max_workspaces > 1024:
            raise WorkflowProgressSpillCapacityConfigurationError(
                "max_workspaces must be between 1 and 1024"
            )
        if self.max_workspaces_per_owner < 1 or self.max_workspaces_per_owner > self.max_workspaces:
            raise WorkflowProgressSpillCapacityConfigurationError(
                "max_workspaces_per_owner must be between 1 and max_workspaces"
            )
        if self.min_free_headroom_bytes < 0:
            raise WorkflowProgressSpillCapacityConfigurationError(
                "min_free_headroom_bytes cannot be negative"
            )
        if (
            not isinstance(self.stale_lease_grace_seconds, int | float)
            or isinstance(self.stale_lease_grace_seconds, bool)
            or self.stale_lease_grace_seconds < 0
        ):
            raise WorkflowProgressSpillCapacityConfigurationError(
                "stale_lease_grace_seconds must be a non-negative number"
            )
        if (
            not isinstance(self.lock_timeout_seconds, int | float)
            or isinstance(self.lock_timeout_seconds, bool)
            or self.lock_timeout_seconds <= 0
        ):
            raise WorkflowProgressSpillCapacityConfigurationError(
                "lock_timeout_seconds must be a positive number"
            )
        return self


@dataclass(frozen=True)
class SpillCapacityReservation:
    """Immutable lease binding one admitted spill reservation to its creator."""

    reservation_id: str
    workspace_id: str
    workspace_token: str
    creator_pid: int
    creator_uid: int | None
    process_start_token: str
    created_at_ns: int
    requested_bytes: int
    profile: str
    root_directory: Path
    root_identity: _PathIdentity
    reservation_file: Path


@dataclass(frozen=True)
class SpillCapacityDiagnostics:
    """Bounded operational diagnostics and metrics snapshot (#158)."""

    admitted_workspaces: int
    refused_workspaces: int
    reserved_bytes: int
    used_bytes: int
    available_bytes: int
    active_count: int
    indeterminate_count: int
    stale_count: int
    reclaimed_count: int
    cleanup_refused_count: int
    cleanup_failed_count: int
    oldest_age_seconds: float
    last_refusal_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted_workspaces": self.admitted_workspaces,
            "refused_workspaces": self.refused_workspaces,
            "reserved_bytes": self.reserved_bytes,
            "used_bytes": self.used_bytes,
            "available_bytes": self.available_bytes,
            "active_count": self.active_count,
            "indeterminate_count": self.indeterminate_count,
            "stale_count": self.stale_count,
            "reclaimed_count": self.reclaimed_count,
            "cleanup_refused_count": self.cleanup_refused_count,
            "cleanup_failed_count": self.cleanup_failed_count,
            "oldest_age_seconds": round(self.oldest_age_seconds, 6),
            "last_refusal_reason": self.last_refusal_reason,
        }


@dataclass(frozen=True)
class SpillJanitorCandidateOutcome:
    """Bounded outcome for one candidate inspected by the spill janitor."""

    name: str
    disposition: SpillOwnerDisposition
    outcome: SpillCleanupOutcome
    reason: str | None = None
    reclaimed_bytes: int = 0


@dataclass(frozen=True)
class SpillJanitorReport:
    """Summary returned by a conservative spill janitor pass."""

    active_count: int
    indeterminate_count: int
    stale_count: int
    reclaimed_count: int
    cleanup_refused_count: int
    cleanup_failed_count: int
    reclaimed_bytes: int
    outcomes: tuple[SpillJanitorCandidateOutcome, ...]
    diagnostics: SpillCapacityDiagnostics


def _default_process_start_token(pid: int) -> str:
    """Return a platform-safe process start token to guard against PID reuse."""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        if proc_stat.is_file():
            raw = proc_stat.read_text(encoding="utf-8", errors="replace")
            rparen = raw.rfind(")")
            if rparen != -1:
                fields = raw[rparen + 2 :].split()
                if len(fields) > 19:
                    return f"proc-starttime:{fields[19]}"
    except OSError:
        pass
    return f"pid:{pid}"


def _default_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        return True
    return True


def _canonical_uuid(value: str) -> str | None:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError):
        return None
    return value if str(parsed) == value else None


_LIVE_RESERVATIONS: dict[str, tuple[SpillCapacityController, SpillCapacityReservation]] = {}
_LIVE_RESERVATIONS_LOCK = Lock()


class SpillCapacityController:
    """Node-local owner for aggregate spill capacity reservation and reclamation (#158)."""

    def __init__(
        self,
        config: SpillCapacityConfig,
        *,
        disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
        pid_alive_fn: Callable[[int], bool] = _default_pid_alive,
        process_start_token_fn: Callable[[int], str] = _default_process_start_token,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.config = config.validated()
        self._disk_usage_fn = disk_usage_fn
        self._pid_alive_fn = pid_alive_fn
        self._process_start_token_fn = process_start_token_fn
        self._clock_ns = clock_ns
        self._lock = Lock()
        self._admitted_workspaces = 0
        self._refused_workspaces = 0
        self._reclaimed_count = 0
        self._cleanup_refused_count = 0
        self._cleanup_failed_count = 0
        self._last_refusal_reason: str | None = None
        self._root_identity: _PathIdentity | None = None
        self._resolved_root: Path | None = None

    def verify_ready(self) -> _PathIdentity:
        """Validate spill root existence, permissions, ownership, and directory structure."""
        root, root_identity = self._validate_root_directory(initialize=True)
        reservations_dir = root / _RESERVATION_SUBDIR
        self._validate_reservations_dir(reservations_dir, expected_dev=root_identity.device)
        return root_identity

    def _validate_root_directory(
        self,
        *,
        initialize: bool = False,
    ) -> tuple[Path, _PathIdentity]:
        candidate = self.config.root_directory
        try:
            if initialize and not os.path.lexists(candidate):
                os.mkdir(candidate, mode=0o700)
            root_stat = os.stat(candidate, follow_symlinks=False)
        except OSError as error:
            self._record_refusal("spill_root_inaccessible")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity root directory is inaccessible",
                reason="spill_root_inaccessible",
            ) from error
        if not stat.S_ISDIR(root_stat.st_mode):
            self._record_refusal("spill_root_not_directory")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity root path is not a regular directory",
                reason="spill_root_not_directory",
            )
        try:
            resolved = candidate.resolve(strict=True)
            resolved_stat = os.stat(resolved, follow_symlinks=False)
        except OSError as error:
            self._record_refusal("spill_root_resolve_failed")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity root canonical resolution failed",
                reason="spill_root_resolve_failed",
            ) from error
        identity = _PathIdentity.from_stat(root_stat)
        if _PathIdentity.from_stat(resolved_stat) != identity:
            self._record_refusal("spill_root_redirected")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity root path was redirected via symlink",
                reason="spill_root_redirected",
            )
        if os.name == "posix":
            mode = stat.S_IMODE(root_stat.st_mode)
            if int(root_stat.st_uid) != os.geteuid() or (mode & 0o077) != 0:
                self._record_refusal("spill_root_unsafe_permissions")
                raise WorkflowProgressSpillCapacityUnavailableError(
                    "Spill capacity root permissions or owner are unsafe",
                    reason="spill_root_unsafe_permissions",
                )
        if self._root_identity is None:
            self._root_identity = identity
            self._resolved_root = resolved
        elif identity != self._root_identity or resolved != self._resolved_root:
            self._record_refusal("spill_root_identity_drift")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity root filesystem or inode identity drifted",
                reason="spill_root_identity_drift",
            )
        return resolved, identity

    def _validate_reservations_dir(self, reservations_dir: Path, *, expected_dev: int) -> None:
        try:
            if not os.path.lexists(reservations_dir):
                os.mkdir(reservations_dir, mode=0o700)
            dir_stat = os.stat(reservations_dir, follow_symlinks=False)
        except OSError as error:
            self._record_refusal("reservations_dir_inaccessible")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity reservations directory is inaccessible",
                reason="reservations_dir_inaccessible",
            ) from error
        if not stat.S_ISDIR(dir_stat.st_mode) or int(dir_stat.st_dev) != expected_dev:
            self._record_refusal("reservations_dir_invalid")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity reservations directory is invalid or on a different device",
                reason="reservations_dir_invalid",
            )
        if os.name == "posix" and (
            int(dir_stat.st_uid) != os.geteuid() or (stat.S_IMODE(dir_stat.st_mode) & 0o077) != 0
        ):
            self._record_refusal("reservations_dir_unsafe_permissions")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill capacity reservations directory permissions are unsafe",
                reason="reservations_dir_unsafe_permissions",
            )

    def _record_refusal(self, reason: str) -> None:
        with self._lock:
            self._refused_workspaces += 1
            self._last_refusal_reason = reason

    def _acquire_cross_process_lock(self, root: Path) -> Path:
        lock_dir = root / _LOCK_DIR_NAME
        deadline = time.monotonic() + float(self.config.lock_timeout_seconds)
        while True:
            try:
                os.mkdir(lock_dir, mode=0o700)
                return lock_dir
            except FileExistsError:
                if time.monotonic() >= deadline:
                    self._record_refusal("lock_contention")
                    raise WorkflowProgressSpillCapacityUnavailableError(
                        "Timed out acquiring cross-process spill capacity lock",
                        reason="lock_contention",
                    ) from None
                time.sleep(0.005)
            except OSError as error:
                self._record_refusal("lock_acquisition_failed")
                raise WorkflowProgressSpillCapacityUnavailableError(
                    "Failed to acquire cross-process spill capacity lock",
                    reason="lock_acquisition_failed",
                ) from error

    @staticmethod
    def _release_cross_process_lock(lock_dir: Path) -> None:
        try:
            if os.path.lexists(lock_dir):
                os.rmdir(lock_dir)
        except OSError:
            pass

    def _classify_reservation_payload(
        self,
        payload: dict[str, Any],
        *,
        now_ns: int,
    ) -> SpillOwnerDisposition:
        pid = payload.get("creator_pid")
        start_token = payload.get("process_start_token")
        created_at_ns = payload.get("created_at_ns")
        if (
            type(pid) is not int
            or pid <= 0
            or type(start_token) is not str
            or not start_token
            or type(created_at_ns) is not int
        ):
            return SpillOwnerDisposition.INDETERMINATE
        if self._pid_alive_fn(pid):
            observed_token = self._process_start_token_fn(pid)
            if observed_token == start_token:
                return SpillOwnerDisposition.LIVE
            # PID was reused by a different process!
            return SpillOwnerDisposition.STALE
        age_seconds = max(0.0, (now_ns - created_at_ns) / 1_000_000_000.0)
        if age_seconds < float(self.config.stale_lease_grace_seconds):
            return SpillOwnerDisposition.INDETERMINATE
        return SpillOwnerDisposition.STALE

    def _inspect_reservations_locked(
        self,
        root: Path,
        root_identity: _PathIdentity,
    ) -> tuple[list[dict[str, Any]], int, int, float]:
        """Return active/indeterminate reservations, counts, and oldest age."""
        reservations_dir = root / _RESERVATION_SUBDIR
        self._validate_reservations_dir(reservations_dir, expected_dev=root_identity.device)
        now_ns = int(self._clock_ns())
        active_records: list[dict[str, Any]] = []
        indeterminate_count = 0
        stale_count = 0
        oldest_age_seconds = 0.0
        try:
            entries = sorted(reservations_dir.iterdir(), key=lambda p: p.name)
        except OSError as error:
            self._record_refusal("reservations_scan_failed")
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Failed to scan spill capacity reservations",
                reason="reservations_scan_failed",
            ) from error

        for entry in entries:
            try:
                entry_stat = os.stat(entry, follow_symlinks=False)
            except OSError:
                indeterminate_count += 1
                continue
            if (
                not stat.S_ISREG(entry_stat.st_mode)
                or entry_stat.st_nlink > 1
                or int(entry_stat.st_dev) != root_identity.device
                or not entry.name.startswith(_RESERVATION_PREFIX)
                or not entry.name.endswith(_RESERVATION_SUFFIX)
            ):
                indeterminate_count += 1
                continue
            try:
                payload = json.loads(entry.read_text(encoding="utf-8", errors="strict"))
            except (OSError, UnicodeError, ValueError):
                indeterminate_count += 1
                continue
            if not isinstance(payload, dict):
                indeterminate_count += 1
                continue
            requested_bytes = payload.get("requested_bytes")
            created_at_ns = payload.get("created_at_ns")
            if type(requested_bytes) is not int or requested_bytes <= 0:
                indeterminate_count += 1
                continue
            if type(created_at_ns) is int and now_ns >= created_at_ns:
                oldest_age_seconds = max(
                    oldest_age_seconds,
                    (now_ns - created_at_ns) / 1_000_000_000.0,
                )
            disposition = self._classify_reservation_payload(payload, now_ns=now_ns)
            if disposition is SpillOwnerDisposition.LIVE:
                active_records.append(payload)
            elif disposition is SpillOwnerDisposition.INDETERMINATE:
                indeterminate_count += 1
                active_records.append(payload)
            else:
                stale_count += 1
        return active_records, indeterminate_count, stale_count, oldest_age_seconds

    def reserve(
        self,
        *,
        requested_bytes: int = SQLITE_SPILL_MAX_BYTES,
        workspace_id: str,
        workspace_token: str,
        profile: str = "preparation-v1",
    ) -> SpillCapacityReservation:
        """Reserve worst-case spill bytes before consuming any workflow iterable (#158)."""
        if type(requested_bytes) is not int or requested_bytes <= 0:
            raise WorkflowProgressSpillCapacityConfigurationError(
                "requested_bytes must be a positive integer"
            )
        if _canonical_uuid(workspace_id) is None:
            raise WorkflowProgressSpillCapacityConfigurationError(
                "workspace_id must be a canonical UUID string"
            )
        if not isinstance(workspace_token, str) or not workspace_token:
            raise WorkflowProgressSpillCapacityConfigurationError(
                "workspace_token must be a non-empty string"
            )

        root, root_identity = self._validate_root_directory(initialize=True)
        lock_dir = self._acquire_cross_process_lock(root)
        try:
            active_records, _indeterminate, _stale, _oldest = self._inspect_reservations_locked(
                root, root_identity
            )
            current_pid = os.getpid()
            reserved_bytes = sum(int(item["requested_bytes"]) for item in active_records)
            active_count = len(active_records)
            owner_count = sum(
                1 for item in active_records if item.get("creator_pid") == current_pid
            )

            if active_count + 1 > self.config.max_workspaces:
                self._record_refusal("workspace_count_exhausted")
                raise WorkflowProgressSpillCapacityExhaustedError(
                    f"Aggregate spill workspace count limit ({self.config.max_workspaces}) exhausted",
                    reason="workspace_count_exhausted",
                )
            if owner_count + 1 > self.config.max_workspaces_per_owner:
                self._record_refusal("per_owner_workspace_count_exhausted")
                raise WorkflowProgressSpillCapacityExhaustedError(
                    f"Per-owner spill workspace count limit ({self.config.max_workspaces_per_owner}) exhausted",
                    reason="per_owner_workspace_count_exhausted",
                )
            if reserved_bytes + requested_bytes > self.config.max_aggregate_bytes:
                self._record_refusal("aggregate_bytes_exhausted")
                raise WorkflowProgressSpillCapacityExhaustedError(
                    f"Aggregate spill byte budget ({self.config.max_aggregate_bytes}) exhausted",
                    reason="aggregate_bytes_exhausted",
                )

            try:
                usage = self._disk_usage_fn(root)
                free_bytes = int(usage.free)
            except OSError as error:
                self._record_refusal("disk_usage_unavailable")
                raise WorkflowProgressSpillCapacityUnavailableError(
                    "Unable to inspect spill filesystem free space",
                    reason="disk_usage_unavailable",
                ) from error

            unreserved_free = max(0, free_bytes - reserved_bytes)
            if unreserved_free - requested_bytes < self.config.min_free_headroom_bytes:
                self._record_refusal("free_space_headroom_exhausted")
                raise WorkflowProgressSpillCapacityExhaustedError(
                    "Spill filesystem free-space headroom is insufficient for reservation",
                    reason="free_space_headroom_exhausted",
                )

            reservation_id = token_hex(16)
            created_at_ns = int(self._clock_ns())
            creator_uid = os.geteuid() if os.name == "posix" else None
            process_start_token = self._process_start_token_fn(current_pid)
            reservation_file = (
                root
                / _RESERVATION_SUBDIR
                / f"{_RESERVATION_PREFIX}{workspace_id}{_RESERVATION_SUFFIX}"
            )
            payload = {
                "reservation_id": reservation_id,
                "workspace_id": workspace_id,
                "workspace_token": workspace_token,
                "creator_pid": current_pid,
                "creator_uid": creator_uid,
                "process_start_token": process_start_token,
                "created_at_ns": created_at_ns,
                "requested_bytes": requested_bytes,
                "profile": profile,
                "root_device": root_identity.device,
                "root_inode": root_identity.inode,
            }
            try:
                with reservation_file.open("x", encoding="utf-8", errors="strict") as handle:
                    if os.name == "posix":
                        os.fchmod(handle.fileno(), 0o600)
                    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            except FileExistsError:
                self._record_refusal("duplicate_workspace_reservation")
                raise WorkflowProgressSpillCapacityExhaustedError(
                    "Spill workspace reservation already exists for workspace_id",
                    reason="duplicate_workspace_reservation",
                ) from None
            except OSError as error:
                self._record_refusal("reservation_write_failed")
                raise WorkflowProgressSpillCapacityUnavailableError(
                    "Failed to persist spill capacity reservation",
                    reason="reservation_write_failed",
                ) from error

            reservation = SpillCapacityReservation(
                reservation_id=reservation_id,
                workspace_id=workspace_id,
                workspace_token=workspace_token,
                creator_pid=current_pid,
                creator_uid=creator_uid,
                process_start_token=process_start_token,
                created_at_ns=created_at_ns,
                requested_bytes=requested_bytes,
                profile=profile,
                root_directory=root,
                root_identity=root_identity,
                reservation_file=reservation_file,
            )
            with self._lock:
                self._admitted_workspaces += 1
            _register_live_reservation(self, reservation)
            return reservation
        finally:
            self._release_cross_process_lock(lock_dir)

    def release(self, reservation: SpillCapacityReservation) -> bool:
        """Idempotently release an owned spill reservation (#158)."""
        _discard_live_reservation(reservation.reservation_id)
        root, root_identity = self._validate_root_directory(initialize=False)
        if root_identity != reservation.root_identity:
            with self._lock:
                self._cleanup_refused_count += 1
            raise WorkflowProgressSpillCapacityUnavailableError(
                "Spill root identity changed before reservation release",
                reason="spill_root_identity_drift",
            )
        lock_dir = self._acquire_cross_process_lock(root)
        try:
            path = reservation.reservation_file
            if not os.path.lexists(path):
                return False
            try:
                path_stat = os.stat(path, follow_symlinks=False)
                if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink > 1:
                    with self._lock:
                        self._cleanup_refused_count += 1
                    raise WorkflowProgressSpillCapacityUnavailableError(
                        "Reservation file was replaced by a non-regular file or hardlink",
                        reason="reservation_file_tampered",
                    )
                payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
            except WorkflowProgressSpillCapacityError:
                raise
            except (OSError, UnicodeError, ValueError) as error:
                with self._lock:
                    self._cleanup_failed_count += 1
                raise WorkflowProgressSpillCapacityUnavailableError(
                    "Failed to inspect reservation file during release",
                    reason="reservation_release_inspection_failed",
                ) from error
            if (
                not isinstance(payload, dict)
                or payload.get("reservation_id") != reservation.reservation_id
                or payload.get("workspace_token") != reservation.workspace_token
                or payload.get("workspace_id") != reservation.workspace_id
            ):
                with self._lock:
                    self._cleanup_refused_count += 1
                raise WorkflowProgressSpillCapacityUnavailableError(
                    "Reservation identity mismatch during release",
                    reason="reservation_identity_mismatch",
                )
            try:
                path.unlink()
            except OSError as error:
                with self._lock:
                    self._cleanup_failed_count += 1
                raise WorkflowProgressSpillCapacityUnavailableError(
                    "Failed to unlink reservation file during release",
                    reason="reservation_unlink_failed",
                ) from error
            return True
        finally:
            self._release_cross_process_lock(lock_dir)

    def _measure_used_spill_bytes(self, root: Path, root_identity: _PathIdentity) -> int:
        used = 0
        try:
            entries = list(root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if not (
                entry.name.startswith(_WORKSPACE_PREFIX)
                or entry.name.startswith(_QUARANTINE_PREFIX)
            ):
                continue
            try:
                entry_stat = os.stat(entry, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(entry_stat.st_mode)
                    or int(entry_stat.st_dev) != root_identity.device
                ):
                    continue
                for child in entry.iterdir():
                    child_stat = os.stat(child, follow_symlinks=False)
                    if stat.S_ISREG(child_stat.st_mode):
                        used += int(child_stat.st_size)
            except OSError:
                continue
        return used

    def diagnostics(self) -> SpillCapacityDiagnostics:
        """Return bounded operational metrics and diagnostics for spill capacity (#158)."""
        root, root_identity = self._validate_root_directory(initialize=True)
        lock_dir = self._acquire_cross_process_lock(root)
        try:
            active_records, indeterminate_count, stale_count, oldest_age = (
                self._inspect_reservations_locked(root, root_identity)
            )
            reserved_bytes = sum(int(item["requested_bytes"]) for item in active_records)
            used_bytes = self._measure_used_spill_bytes(root, root_identity)
            try:
                free_bytes = int(self._disk_usage_fn(root).free)
            except OSError:
                free_bytes = 0
            budget_remaining = max(0, self.config.max_aggregate_bytes - reserved_bytes)
            disk_remaining = max(
                0, free_bytes - reserved_bytes - self.config.min_free_headroom_bytes
            )
            available_bytes = min(budget_remaining, disk_remaining)
            live_only_count = max(0, len(active_records) - indeterminate_count)
            with self._lock:
                return SpillCapacityDiagnostics(
                    admitted_workspaces=self._admitted_workspaces,
                    refused_workspaces=self._refused_workspaces,
                    reserved_bytes=reserved_bytes,
                    used_bytes=used_bytes,
                    available_bytes=available_bytes,
                    active_count=live_only_count,
                    indeterminate_count=indeterminate_count,
                    stale_count=stale_count,
                    reclaimed_count=self._reclaimed_count,
                    cleanup_refused_count=self._cleanup_refused_count,
                    cleanup_failed_count=self._cleanup_failed_count,
                    oldest_age_seconds=oldest_age,
                    last_refusal_reason=self._last_refusal_reason,
                )
        finally:
            self._release_cross_process_lock(lock_dir)

    def reclaim_stale_workspaces(self) -> SpillJanitorReport:
        """Conservatively reclaim only proven-stale package-owned spill workspaces (#158)."""
        root, root_identity = self._validate_root_directory(initialize=True)
        lock_dir = self._acquire_cross_process_lock(root)
        outcomes: list[SpillJanitorCandidateOutcome] = []
        reclaimed_bytes_total = 0
        try:
            reservations_dir = root / _RESERVATION_SUBDIR
            self._validate_reservations_dir(reservations_dir, expected_dev=root_identity.device)
            now_ns = int(self._clock_ns())
            reservations_by_ws: dict[str, tuple[Path, dict[str, Any]]] = {}
            for res_entry in sorted(reservations_dir.iterdir(), key=lambda p: p.name):
                if not (
                    res_entry.name.startswith(_RESERVATION_PREFIX)
                    and res_entry.name.endswith(_RESERVATION_SUFFIX)
                ):
                    continue
                ws_id = res_entry.name.removeprefix(_RESERVATION_PREFIX).removesuffix(
                    _RESERVATION_SUFFIX
                )
                if _canonical_uuid(ws_id) is None:
                    continue
                try:
                    st = os.stat(res_entry, follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                        continue
                    data = json.loads(res_entry.read_text(encoding="utf-8", errors="strict"))
                    if isinstance(data, dict) and data.get("workspace_id") == ws_id:
                        reservations_by_ws[ws_id] = (res_entry, data)
                except (OSError, UnicodeError, ValueError):
                    continue

            seen_workspace_ids: set[str] = set()
            for entry in sorted(root.iterdir(), key=lambda p: p.name):
                if entry.name in {_RESERVATION_SUBDIR, _LOCK_DIR_NAME}:
                    continue
                workspace_id: str | None = None
                if entry.name.startswith(_QUARANTINE_PREFIX):
                    suffix = entry.name.removeprefix(_QUARANTINE_PREFIX)
                    parts = suffix.split("-", 5)
                    if len(parts) == 6:
                        candidate_uuid = "-".join(parts[:5])
                        workspace_id = _canonical_uuid(candidate_uuid)
                elif entry.name.startswith(_WORKSPACE_PREFIX):
                    suffix = entry.name.removeprefix(_WORKSPACE_PREFIX)
                    workspace_id = _canonical_uuid(suffix)

                if workspace_id is None:
                    # Foreign entry: never touch or delete (#158).
                    continue

                seen_workspace_ids.add(workspace_id)
                outcome = self._reclaim_candidate_directory_locked(
                    entry,
                    workspace_id=workspace_id,
                    root=root,
                    root_identity=root_identity,
                    reservation_info=reservations_by_ws.get(workspace_id),
                    now_ns=now_ns,
                )
                outcomes.append(outcome)
                reclaimed_bytes_total += outcome.reclaimed_bytes

            # Also reclaim orphan stale reservation files whose workspace directory is already gone.
            for ws_id, (res_file, res_data) in sorted(reservations_by_ws.items()):
                if ws_id in seen_workspace_ids:
                    continue
                disposition = self._classify_reservation_payload(res_data, now_ns=now_ns)
                if disposition is SpillOwnerDisposition.STALE:
                    try:
                        res_file.unlink()
                        with self._lock:
                            self._reclaimed_count += 1
                        outcomes.append(
                            SpillJanitorCandidateOutcome(
                                name=res_file.name,
                                disposition=SpillOwnerDisposition.STALE,
                                outcome=SpillCleanupOutcome.RECLAIMED,
                            )
                        )
                    except OSError:
                        with self._lock:
                            self._cleanup_failed_count += 1
                        outcomes.append(
                            SpillJanitorCandidateOutcome(
                                name=res_file.name,
                                disposition=SpillOwnerDisposition.STALE,
                                outcome=SpillCleanupOutcome.OPERATIONAL_FAILURE,
                                reason="stale_reservation_unlink_failed",
                            )
                        )
        finally:
            self._release_cross_process_lock(lock_dir)

        diag = self.diagnostics()
        return SpillJanitorReport(
            active_count=diag.active_count,
            indeterminate_count=diag.indeterminate_count,
            stale_count=diag.stale_count,
            reclaimed_count=diag.reclaimed_count,
            cleanup_refused_count=diag.cleanup_refused_count,
            cleanup_failed_count=diag.cleanup_failed_count,
            reclaimed_bytes=reclaimed_bytes_total,
            outcomes=tuple(outcomes),
            diagnostics=diag,
        )

    def _reclaim_candidate_directory_locked(
        self,
        directory: Path,
        *,
        workspace_id: str,
        root: Path,
        root_identity: _PathIdentity,
        reservation_info: tuple[Path, dict[str, Any]] | None,
        now_ns: int,
    ) -> SpillJanitorCandidateOutcome:
        try:
            dir_stat = os.stat(directory, follow_symlinks=False)
        except OSError:
            with self._lock:
                self._cleanup_failed_count += 1
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.OPERATIONAL_FAILURE,
                reason="candidate_stat_failed",
            )
        if (
            not stat.S_ISDIR(dir_stat.st_mode)
            or int(dir_stat.st_dev) != root_identity.device
            or directory.parent != root
        ):
            with self._lock:
                self._cleanup_refused_count += 1
                self._last_refusal_reason = "candidate_not_owned_directory"
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.REFUSED,
                reason="candidate_not_owned_directory",
            )
        if os.name == "posix" and (
            int(dir_stat.st_uid) != os.geteuid() or stat.S_IMODE(dir_stat.st_mode) != 0o700
        ):
            with self._lock:
                self._cleanup_refused_count += 1
                self._last_refusal_reason = "candidate_unsafe_permissions"
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.REFUSED,
                reason="candidate_unsafe_permissions",
            )

        try:
            entries = list(directory.iterdir())
        except OSError:
            with self._lock:
                self._cleanup_failed_count += 1
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.OPERATIONAL_FAILURE,
                reason="candidate_iterdir_failed",
            )

        expected_names = {_DATABASE_NAME, _LEASE_NAME}
        reclaimed_bytes = 0
        for child in entries:
            try:
                child_stat = os.stat(child, follow_symlinks=False)
            except OSError:
                with self._lock:
                    self._cleanup_failed_count += 1
                return SpillJanitorCandidateOutcome(
                    name=directory.name,
                    disposition=SpillOwnerDisposition.INDETERMINATE,
                    outcome=SpillCleanupOutcome.OPERATIONAL_FAILURE,
                    reason="child_stat_failed",
                )
            if (
                child.name not in expected_names
                or not stat.S_ISREG(child_stat.st_mode)
                or child_stat.st_nlink > 1
                or int(child_stat.st_dev) != root_identity.device
            ):
                with self._lock:
                    self._cleanup_refused_count += 1
                    self._last_refusal_reason = "unexpected_or_linked_entry"
                return SpillJanitorCandidateOutcome(
                    name=directory.name,
                    disposition=SpillOwnerDisposition.INDETERMINATE,
                    outcome=SpillCleanupOutcome.REFUSED,
                    reason="unexpected_or_linked_entry",
                )
            reclaimed_bytes += int(child_stat.st_size)

        lease_path = directory / _LEASE_NAME
        if not os.path.lexists(lease_path):
            with self._lock:
                self._cleanup_refused_count += 1
                self._last_refusal_reason = "missing_owner_lease"
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.REFUSED,
                reason="missing_owner_lease",
            )

        try:
            lease_data = json.loads(lease_path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, ValueError):
            with self._lock:
                self._cleanup_refused_count += 1
                self._last_refusal_reason = "corrupt_owner_lease"
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.REFUSED,
                reason="corrupt_owner_lease",
            )

        if (
            not isinstance(lease_data, dict)
            or lease_data.get("workspace_id") != workspace_id
            or type(lease_data.get("pid")) is not int
            or type(lease_data.get("token")) is not str
            or not lease_data.get("token")
        ):
            with self._lock:
                self._cleanup_refused_count += 1
                self._last_refusal_reason = "invalid_owner_lease_fields"
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.REFUSED,
                reason="invalid_owner_lease_fields",
            )

        lease_pid = int(lease_data["pid"])
        lease_token = str(lease_data["token"])

        if reservation_info is not None:
            res_file, res_data = reservation_info
            if (
                res_data.get("workspace_token") != lease_token
                or res_data.get("creator_pid") != lease_pid
            ):
                with self._lock:
                    self._cleanup_refused_count += 1
                    self._last_refusal_reason = "reservation_lease_mismatch"
                return SpillJanitorCandidateOutcome(
                    name=directory.name,
                    disposition=SpillOwnerDisposition.INDETERMINATE,
                    outcome=SpillCleanupOutcome.REFUSED,
                    reason="reservation_lease_mismatch",
                )
            disposition = self._classify_reservation_payload(res_data, now_ns=now_ns)
        else:
            if self._pid_alive_fn(lease_pid):
                disposition = SpillOwnerDisposition.LIVE
            else:
                age_ns = max(0, now_ns - int(dir_stat.st_mtime_ns))
                if (age_ns / 1_000_000_000.0) < float(self.config.stale_lease_grace_seconds):
                    disposition = SpillOwnerDisposition.INDETERMINATE
                else:
                    disposition = SpillOwnerDisposition.STALE

        if disposition is SpillOwnerDisposition.LIVE:
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.LIVE,
                outcome=SpillCleanupOutcome.SKIPPED_LIVE,
            )
        if disposition is SpillOwnerDisposition.INDETERMINATE:
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.INDETERMINATE,
                outcome=SpillCleanupOutcome.SKIPPED_INDETERMINATE,
                reason="within_grace_or_indeterminate",
            )

        # Proven STALE: verify directory identity has not changed and remove owned files.
        try:
            recheck_stat = os.stat(directory, follow_symlinks=False)
            if _PathIdentity.from_stat(recheck_stat) != _PathIdentity.from_stat(dir_stat):
                with self._lock:
                    self._cleanup_refused_count += 1
                    self._last_refusal_reason = "candidate_identity_changed"
                return SpillJanitorCandidateOutcome(
                    name=directory.name,
                    disposition=SpillOwnerDisposition.STALE,
                    outcome=SpillCleanupOutcome.REFUSED,
                    reason="candidate_identity_changed",
                )
            for name in (_DATABASE_NAME, _LEASE_NAME):
                target = directory / name
                if os.path.lexists(target):
                    target.unlink()
            directory.rmdir()
            if reservation_info is not None and os.path.lexists(reservation_info[0]):
                reservation_info[0].unlink()
        except OSError:
            with self._lock:
                self._cleanup_failed_count += 1
            return SpillJanitorCandidateOutcome(
                name=directory.name,
                disposition=SpillOwnerDisposition.STALE,
                outcome=SpillCleanupOutcome.OPERATIONAL_FAILURE,
                reason="stale_removal_failed",
            )

        with self._lock:
            self._reclaimed_count += 1
        return SpillJanitorCandidateOutcome(
            name=directory.name,
            disposition=SpillOwnerDisposition.STALE,
            outcome=SpillCleanupOutcome.RECLAIMED,
            reclaimed_bytes=reclaimed_bytes,
        )


def _register_live_reservation(
    controller: SpillCapacityController,
    reservation: SpillCapacityReservation,
) -> None:
    with _LIVE_RESERVATIONS_LOCK:
        _LIVE_RESERVATIONS[reservation.reservation_id] = (controller, reservation)


def _discard_live_reservation(reservation_id: str) -> None:
    with _LIVE_RESERVATIONS_LOCK:
        _LIVE_RESERVATIONS.pop(reservation_id, None)


def _cleanup_live_reservations_at_exit() -> None:
    with _LIVE_RESERVATIONS_LOCK:
        items = list(_LIVE_RESERVATIONS.values())
    for controller, reservation in items:
        try:
            controller.release(reservation)
        except BaseException:
            continue


atexit.register(_cleanup_live_reservations_at_exit)


__all__ = [
    "DEFAULT_MAX_AGGREGATE_SPILL_BYTES",
    "DEFAULT_MAX_SPILL_WORKSPACES",
    "DEFAULT_MAX_WORKSPACES_PER_OWNER",
    "DEFAULT_MIN_FREE_HEADROOM_BYTES",
    "DEFAULT_STALE_LEASE_GRACE_SECONDS",
    "SpillCapacityConfig",
    "SpillCapacityController",
    "SpillCapacityDiagnostics",
    "SpillCapacityReservation",
    "SpillCleanupOutcome",
    "SpillJanitorCandidateOutcome",
    "SpillJanitorReport",
    "SpillOwnerDisposition",
    "WorkflowProgressSpillCapacityConfigurationError",
    "WorkflowProgressSpillCapacityError",
    "WorkflowProgressSpillCapacityExhaustedError",
    "WorkflowProgressSpillCapacityUnavailableError",
]
