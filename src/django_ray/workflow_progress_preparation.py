"""Package-owned SQLite engine for bounded workflow-progress preparation.

Issue #141 activates only the topology phase through the legacy-compatible public
preparer. Composite topology/detail detachment and schema-v3 producer activation
remain disabled until issues #142 and #79 complete their independent boundaries.
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from secrets import token_hex
from threading import Lock, RLock, get_ident
from typing import Any, Never
from uuid import UUID, uuid4

import django_ray.workflow_progress_storage as storage
from django_ray.runtime.context import WorkflowRunIdentity

SQLITE_PAGE_BYTES = 4 * 1024
SQLITE_CACHE_BYTES = 8 * 1024 * 1024
SQLITE_MMAP_BYTES = 0
SQLITE_SPILL_MAX_BYTES = 1024 * 1024 * 1024
SQLITE_CONTROL_RESERVE_BYTES = 4 * 1024 * 1024
SQLITE_NODE_MAX_ITEMS = 1_000_000
SQLITE_EDGE_MAX_ITEMS = 4_000_000
SQLITE_BATCH_MAX_ITEMS = 256
SQLITE_BATCH_MAX_DECODED_BYTES = 4 * 1024 * 1024

_MINIMUM_SPILL_BYTES = 64 * 1024
_WORKSPACE_PREFIX = "django-ray-preparation-"
_QUARANTINE_PREFIX = "django-ray-preparation-cleanup-"
_DATABASE_NAME = "workspace.sqlite3"
_LEASE_NAME = "owner.lease"
_NODE_SELECTION_SQL = (
    "SELECT node_id, payload FROM nodes WHERE payload IS NOT NULL ORDER BY node_id LIMIT ?"
)
_EDGE_SELECTION_SQL = (
    "SELECT e.source, e.target FROM edges AS e "
    "JOIN nodes AS source_node ON source_node.node_id = e.source "
    "JOIN nodes AS target_node ON target_node.node_id = e.target "
    "WHERE source_node.retained = 1 AND target_node.retained = 1 "
    "ORDER BY e.source, e.target LIMIT ?"
)
_OBSERVED_NODE_SELECTION_SQL = "SELECT node_id FROM nodes ORDER BY node_id"
_LIVE_WORKSPACES: set[SQLitePreparationWorkspace] = set()
_LIVE_WORKSPACES_LOCK = Lock()


def _poison_on_failure(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(self: SQLitePreparationWorkspace, *args: Any, **kwargs: Any) -> Any:
        self._assert_creator_process()
        with self._operation_lock:
            self._assert_owner_thread()
            try:
                return function(self, *args, **kwargs)
            except BaseException:
                self._close_active_input_iterator()
                self._phase = "poisoned"
                raise

    return wrapped


class WorkflowProgressPreparationConfigurationError(ValueError):
    """Raised when the preparation workspace budget is invalid."""


class WorkflowProgressPreparationSpillExhaustedError(RuntimeError):
    """Raised when an explicit item or file-byte budget is exhausted."""


class WorkflowProgressPreparationCleanupRefusedError(RuntimeError):
    """Raised when workspace ownership cannot be proven before deletion."""


class WorkflowProgressPreparationWorkspaceAcquisitionError(RuntimeError):
    """Raised when a private workspace cannot be acquired safely."""


class WorkflowProgressPreparationCleanupOperationalError(RuntimeError):
    """Raised when proven-owned workspace cleanup fails operationally."""


class WorkflowProgressPreparationWorkspaceIntegrityError(RuntimeError):
    """Raised when unexpected files escape the frozen SQLite profile."""


@dataclass(frozen=True)
class _PathIdentity:
    """Stable path identity used to reject parent or workspace replacement."""

    device: int
    inode: int
    windows_creation_ns: int | None

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _PathIdentity:
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            windows_creation_ns=int(value.st_ctime_ns) if os.name == "nt" else None,
        )


class _BudgetConnection(sqlite3.Connection):
    """Translate SQLite's max-page failure into the preparation contract."""

    @staticmethod
    def _translate(error: sqlite3.OperationalError) -> Never:
        if "full" in str(error).lower():
            raise WorkflowProgressPreparationSpillExhaustedError(
                "SQLite preparation spill byte budget exhausted"
            ) from error
        raise error

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as error:
            self._translate(error)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as error:
            self._translate(error)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.OperationalError as error:
            self._translate(error)

    def commit(self) -> None:
        try:
            super().commit()
        except sqlite3.OperationalError as error:
            self._translate(error)


@dataclass(frozen=True)
class SQLitePreparationConfig:
    """Explicit resident, transfer-batch, and spill budgets for one workspace."""

    page_bytes: int = SQLITE_PAGE_BYTES
    cache_bytes: int = SQLITE_CACHE_BYTES
    mmap_bytes: int = SQLITE_MMAP_BYTES
    max_spill_bytes: int = SQLITE_SPILL_MAX_BYTES
    control_reserve_bytes: int = SQLITE_CONTROL_RESERVE_BYTES
    max_node_items: int = SQLITE_NODE_MAX_ITEMS
    max_edge_items: int = SQLITE_EDGE_MAX_ITEMS
    batch_max_items: int = SQLITE_BATCH_MAX_ITEMS
    batch_max_decoded_bytes: int = SQLITE_BATCH_MAX_DECODED_BYTES

    def validated(self) -> SQLitePreparationConfig:
        integer_fields = {
            "page_bytes": self.page_bytes,
            "cache_bytes": self.cache_bytes,
            "mmap_bytes": self.mmap_bytes,
            "max_spill_bytes": self.max_spill_bytes,
            "control_reserve_bytes": self.control_reserve_bytes,
            "max_node_items": self.max_node_items,
            "max_edge_items": self.max_edge_items,
            "batch_max_items": self.batch_max_items,
            "batch_max_decoded_bytes": self.batch_max_decoded_bytes,
        }
        for name, value in integer_fields.items():
            if type(value) is not int:
                raise WorkflowProgressPreparationConfigurationError(f"{name} must be an integer")
        if self.page_bytes != SQLITE_PAGE_BYTES:
            raise WorkflowProgressPreparationConfigurationError(
                "page_bytes must remain 4096 for preparation-v1"
            )
        if (
            self.cache_bytes < self.page_bytes
            or self.cache_bytes > SQLITE_CACHE_BYTES
            or self.cache_bytes % 1024
        ):
            raise WorkflowProgressPreparationConfigurationError(
                "cache_bytes must be a whole number of KiB from 4 KiB through 8 MiB"
            )
        if self.mmap_bytes != 0:
            raise WorkflowProgressPreparationConfigurationError(
                "mmap_bytes must be zero for preparation-v1"
            )
        if self.max_spill_bytes > SQLITE_SPILL_MAX_BYTES:
            raise WorkflowProgressPreparationConfigurationError(
                "max_spill_bytes cannot exceed 1 GiB"
            )
        if self.max_spill_bytes % self.page_bytes:
            raise WorkflowProgressPreparationConfigurationError(
                "max_spill_bytes must be page aligned"
            )
        if (
            self.control_reserve_bytes < self.page_bytes
            or self.control_reserve_bytes > SQLITE_CONTROL_RESERVE_BYTES
            or self.control_reserve_bytes % self.page_bytes
        ):
            raise WorkflowProgressPreparationConfigurationError(
                "control_reserve_bytes must be page aligned from 4 KiB through 4 MiB"
            )
        if self.max_spill_bytes - self.control_reserve_bytes < _MINIMUM_SPILL_BYTES:
            raise WorkflowProgressPreparationConfigurationError(
                "max_spill_bytes must leave at least 64 KiB after the control reserve"
            )
        item_limits = {
            "max_node_items": SQLITE_NODE_MAX_ITEMS,
            "max_edge_items": SQLITE_EDGE_MAX_ITEMS,
        }
        for name, maximum in item_limits.items():
            if not 1 <= getattr(self, name) <= maximum:
                raise WorkflowProgressPreparationConfigurationError(
                    f"{name} must be positive and cannot exceed {maximum}"
                )
        if not 1 <= self.batch_max_items <= SQLITE_BATCH_MAX_ITEMS:
            raise WorkflowProgressPreparationConfigurationError(
                "batch_max_items must be from 1 through 256"
            )
        if not self.page_bytes <= self.batch_max_decoded_bytes <= SQLITE_BATCH_MAX_DECODED_BYTES:
            raise WorkflowProgressPreparationConfigurationError(
                "batch_max_decoded_bytes must be from 4 KiB through 4 MiB"
            )
        return self


@dataclass(frozen=True)
class PreparedWorkflowProgressTopologyCandidate:
    """Bounded canonical topology output without the old all-observed identity set."""

    identity: WorkflowRunIdentity
    topology_version: int
    manifest_payload: bytes
    manifest_digest: str
    pages: tuple[storage.PreparedWorkflowProgressTopologyPage, ...]
    node_ids: frozenset[str]
    node_kinds: tuple[tuple[str, str], ...]
    edges: tuple[tuple[str, str], ...]
    observed_node_count: int
    observed_edge_count: int
    retained_node_count: int
    retained_edge_count: int
    encoded_bytes: int
    decoded_bytes: int
    truncation_reasons: tuple[str, ...]
    map_node_ids: frozenset[str]
    _workspace_token: str
    _workspace_pid: int


def canonical_topology_evidence(topology: Any) -> dict[str, Any]:
    """Return only durable/canonical fields shared with the production preparer."""
    return {
        "identity": topology.identity,
        "topology_version": topology.topology_version,
        "manifest_payload": topology.manifest_payload,
        "manifest_digest": topology.manifest_digest,
        "pages": tuple(
            (
                page.collection,
                page.page_index,
                page.payload,
                page.digest,
                page.item_count,
                page.encoded_bytes,
                page.decoded_bytes,
            )
            for page in topology.pages
        ),
        "node_ids": topology.node_ids,
        "node_kinds": topology.node_kinds,
        "edges": topology.edges,
        "observed_node_count": topology.observed_node_count,
        "observed_edge_count": topology.observed_edge_count,
        "retained_node_count": topology.retained_node_count,
        "retained_edge_count": topology.retained_edge_count,
        "encoded_bytes": topology.encoded_bytes,
        "decoded_bytes": topology.decoded_bytes,
        "truncation_reasons": topology.truncation_reasons,
        "map_node_ids": topology.map_node_ids,
    }


@dataclass(frozen=True)
class _PreparedTopologyCandidateSignature:
    """Immutable snapshot of the exact candidate evidence sealed by a workspace."""

    scalar_signature: tuple[tuple[type[Any], Any], ...]
    evidence_references: tuple[object, ...]
    page_signatures: tuple[tuple[tuple[type[Any], Any], ...], ...]


def _type_exact_candidate_value(value: Any) -> tuple[type[Any], Any]:
    """Bind one immutable value without Python's cross-type equality coercions."""
    return type(value), value


def _candidate_signature(
    topology: PreparedWorkflowProgressTopologyCandidate,
) -> _PreparedTopologyCandidateSignature:
    """Snapshot the candidate without retaining mutable evidence by equality."""
    scalar_signature = (
        _type_exact_candidate_value(topology.identity.task_execution_pk),
        _type_exact_candidate_value(topology.identity.attempt_number),
        _type_exact_candidate_value(topology.identity.execution_generation),
        _type_exact_candidate_value(topology.identity.run_id),
        _type_exact_candidate_value(topology.topology_version),
        _type_exact_candidate_value(topology.manifest_digest),
        _type_exact_candidate_value(topology.observed_node_count),
        _type_exact_candidate_value(topology.observed_edge_count),
        _type_exact_candidate_value(topology.retained_node_count),
        _type_exact_candidate_value(topology.retained_edge_count),
        _type_exact_candidate_value(topology.encoded_bytes),
        _type_exact_candidate_value(topology.decoded_bytes),
        _type_exact_candidate_value(topology._workspace_pid),
    )
    evidence_references: list[object] = [
        topology.identity,
        topology.manifest_payload,
        topology.pages,
        topology.node_ids,
        topology.node_kinds,
        topology.edges,
        topology.truncation_reasons,
        topology.map_node_ids,
        topology._workspace_token,
    ]
    page_signatures = []
    for page in topology.pages:
        evidence_references.extend((page, page.payload))
        page_signatures.append(
            (
                _type_exact_candidate_value(page.collection),
                _type_exact_candidate_value(page.page_index),
                _type_exact_candidate_value(page.digest),
                _type_exact_candidate_value(page.item_count),
                _type_exact_candidate_value(page.encoded_bytes),
                _type_exact_candidate_value(page.decoded_bytes),
            )
        )
    return _PreparedTopologyCandidateSignature(
        scalar_signature=scalar_signature,
        evidence_references=tuple(evidence_references),
        page_signatures=tuple(page_signatures),
    )


def _candidate_signature_matches(
    topology: PreparedWorkflowProgressTopologyCandidate,
    expected: _PreparedTopologyCandidateSignature | None,
) -> bool:
    """Reject replacement, in-place mutation, and cross-type-equal evidence."""
    if expected is None:
        return False
    try:
        current = _candidate_signature(topology)
    except (AttributeError, TypeError):
        return False
    return bool(
        current.scalar_signature == expected.scalar_signature
        and current.page_signatures == expected.page_signatures
        and len(current.evidence_references) == len(expected.evidence_references)
        and all(
            observed is sealed
            for observed, sealed in zip(
                current.evidence_references,
                expected.evidence_references,
                strict=True,
            )
        )
    )


class SQLitePreparationWorkspace:
    """Private, single-owner, disposable preparation workspace.

    ``journal_mode=OFF`` and ``synchronous=OFF`` are deliberate: this file is
    never durable state, and any error discards the whole workspace.  The main
    database file is therefore the only expected spill file and
    ``max_page_count`` enforces its physical byte ceiling.
    """

    def __init__(
        self,
        config: SQLitePreparationConfig | None = None,
        *,
        parent_directory: Path | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> None:
        self.config = (config or SQLitePreparationConfig()).validated()
        self.parent_directory = parent_directory
        self.cancellation_check = cancellation_check or (lambda: None)
        self.token = token_hex(16)
        self._owned_parent: Path | None = None
        self._owned_parent_identity: _PathIdentity | None = None
        self._owned_directory: Path | None = None
        self._owned_directory_identity: _PathIdentity | None = None
        self._cleanup_quarantine: Path | None = None
        self.database_path: Path | None = None
        self.lease_path: Path | None = None
        self._lease_identity: _PathIdentity | None = None
        self.connection: sqlite3.Connection | None = None
        self.cleanup_outcome = "not_started"
        self.spill_peak_bytes = 0
        self.spill_items = 0
        self.observed_node_count = 0
        self.observed_edge_count = 0
        self._active_input_iterator: Iterator[Mapping[str, Any]] | None = None
        self._prepared_topology: PreparedWorkflowProgressTopologyCandidate | None = None
        self._prepared_topology_signature: _PreparedTopologyCandidateSignature | None = None
        self._legacy_observed_node_ids: frozenset[str] | None = None
        self._legacy_detachment_ready = False
        self._creator_pid: int | None = None
        self._creator_uid: int | None = None
        self._owner_thread_id: int | None = None
        self._operation_lock = RLock()
        self._phase = "new"

    def __enter__(self) -> SQLitePreparationWorkspace:
        self._assert_creator_process()
        with self._operation_lock:
            if self._phase != "new":
                raise RuntimeError("SQLite preparation workspace may be entered exactly once")
            self._creator_pid = os.getpid()
            self._creator_uid = os.geteuid() if os.name == "posix" else None
            self._owner_thread_id = get_ident()
            self._phase = "acquiring"
            directory_created = False
            lease_initialized = False
            acquisition_error: BaseException | None = None
            try:
                parent = self._acquire_safe_parent()
                workspace_id = uuid4()
                directory = parent / f"{_WORKSPACE_PREFIX}{workspace_id}"
                self._owned_directory = directory
                self.database_path = directory / _DATABASE_NAME
                self.lease_path = directory / _LEASE_NAME
                self._create_private_workspace_directory(directory)
                directory_created = True
                self._revalidate_acquisition_parent()
                self._owned_directory_identity = self._validate_new_workspace_directory()
                self._initialize_owner_lease(workspace_id)
                lease_initialized = True
                connection = sqlite3.connect(
                    self.database_path,
                    factory=_BudgetConnection,
                    check_same_thread=False,
                )
                self.connection = connection
                connection.row_factory = sqlite3.Row
                self._configure_connection()
                self._assert_pragmas()
                self._create_schema()
                self._assert_query_plans()
                self._flush_batch()
                self.cleanup_outcome = "pending"
                self._phase = "topology"
                _register_live_workspace(self)
            except BaseException as error:
                acquisition_error = error

            if acquisition_error is not None:
                self._phase = "poisoned"
                cleanup_already_failed = self.cleanup_outcome in {
                    "busy",
                    "operational_failure",
                    "refused",
                }
                if directory_created and not cleanup_already_failed:
                    if lease_initialized:
                        self._close_and_remove(acquisition_failure=True)
                    else:
                        self._remove_empty_acquisition_directory()
                elif not directory_created:
                    self.cleanup_outcome = "not_created"
                raise acquisition_error.with_traceback(acquisition_error.__traceback__)
            return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._assert_creator_process()
        with self._operation_lock:
            self._assert_owner_thread()
            poisoned = self._phase == "poisoned" or _type is not None
            try:
                self._close_and_remove()
            except BaseException:
                self._phase = "poisoned"
                raise
            self._phase = "poisoned" if poisoned else "closed"

    @property
    def directory(self) -> Path | None:
        return self._owned_directory

    @property
    def path_exists(self) -> bool:
        return bool(
            (self._owned_directory is not None and os.path.lexists(self._owned_directory))
            or (self._cleanup_quarantine is not None and os.path.lexists(self._cleanup_quarantine))
        )

    def _assert_creator_process(self) -> None:
        if self._creator_pid is not None and self._creator_pid != os.getpid():
            raise RuntimeError("SQLite preparation workspace belongs to a different process")

    def _assert_owner_thread(self) -> None:
        if self._owner_thread_id is not None and self._owner_thread_id != get_ident():
            raise RuntimeError("SQLite preparation operations require the owner thread")

    def _acquire_safe_parent(self) -> Path:
        failed = False
        parent: Path | None = None
        parent_stat: os.stat_result | None = None
        try:
            candidate = (
                self.parent_directory
                if self.parent_directory is not None
                else Path(tempfile.gettempdir())
            )
            parent = candidate.resolve(strict=True)
            parent_stat = os.stat(parent, follow_symlinks=False)
        except (OSError, RuntimeError):
            failed = True
        if failed or parent is None or parent_stat is None:
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation workspace parent inspection failed"
            )
        self._validate_acquisition_parent_stat(parent_stat)
        self._owned_parent = parent
        self._owned_parent_identity = _PathIdentity.from_stat(parent_stat)
        return parent

    @staticmethod
    def _validate_acquisition_parent_stat(parent_stat: os.stat_result) -> None:
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation workspace parent is not a directory"
            )
        if os.name != "posix":
            return
        mode = stat.S_IMODE(parent_stat.st_mode)
        owner = int(parent_stat.st_uid)
        current_owner = os.geteuid()
        sticky_shared = bool(mode & stat.S_ISVTX) and bool(mode & 0o022)
        if owner != current_owner and not (owner == 0 and sticky_shared):
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation workspace parent owner is unsafe"
            )
        if mode & 0o022 and not sticky_shared:
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation workspace parent permissions are unsafe"
            )

    def _revalidate_acquisition_parent(self) -> None:
        parent = self._owned_parent
        expected = self._owned_parent_identity
        failed = False
        current: os.stat_result | None = None
        try:
            if parent is not None:
                current = os.stat(parent, follow_symlinks=False)
        except OSError:
            failed = True
        if failed or parent is None or expected is None or current is None:
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation workspace parent revalidation failed"
            )
        self._validate_acquisition_parent_stat(current)
        if _PathIdentity.from_stat(current) != expected:
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation workspace parent identity changed during acquisition"
            )

    @staticmethod
    def _create_private_workspace_directory(directory: Path) -> None:
        failed = False
        try:
            os.mkdir(directory, mode=0o700)
        except OSError:
            failed = True
        if failed:
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation private directory creation failed"
            )

    def _validate_new_workspace_directory(self) -> _PathIdentity:
        directory = self._owned_directory
        failed = False
        directory_stat: os.stat_result | None = None
        try:
            if directory is not None:
                directory_stat = os.stat(directory, follow_symlinks=False)
        except OSError:
            failed = True
        if failed or directory is None or directory_stat is None:
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation private directory inspection failed"
            )
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation private path is not a directory"
            )
        if os.name == "posix" and (
            int(directory_stat.st_uid) != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation private directory ownership is unsafe"
            )
        return _PathIdentity.from_stat(directory_stat)

    def _initialize_owner_lease(self, workspace_id: UUID) -> None:
        lease_path = self.lease_path
        failed = False
        invalid = False
        if lease_path is None:
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation owner lease path is unavailable"
            )
        try:
            with lease_path.open("x", encoding="utf-8", errors="strict") as lease:
                lease_stat = os.fstat(lease.fileno())
                if not stat.S_ISREG(lease_stat.st_mode):
                    invalid = True
                self._lease_identity = _PathIdentity.from_stat(lease_stat)
                if os.name == "posix":
                    os.fchmod(lease.fileno(), 0o600)
                json.dump(
                    {
                        "pid": self._creator_pid,
                        "token": self.token,
                        "workspace_id": str(workspace_id),
                    },
                    lease,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            current = os.stat(lease_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or self._lease_identity is None
                or _PathIdentity.from_stat(current) != self._lease_identity
                or (os.name == "posix" and stat.S_IMODE(current.st_mode) != 0o600)
            ):
                invalid = True
        except BaseException:
            failed = True
        if failed or invalid:
            if self._lease_identity is not None:
                self._remove_partial_owner_lease()
            raise WorkflowProgressPreparationWorkspaceAcquisitionError(
                "SQLite preparation owner lease initialization failed"
            )

    def _remove_partial_owner_lease(self) -> None:
        """Remove only the exact lease file created by this acquisition."""
        lease_path = self.lease_path
        expected = self._lease_identity
        if lease_path is None or expected is None:
            return
        directory = self._validated_owned_path()
        if lease_path.parent != directory or not os.path.lexists(lease_path):
            self.cleanup_outcome = "refused"
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation partial owner lease is missing or redirected"
            )
        inspection_failed = False
        lease_stat: os.stat_result | None = None
        try:
            lease_stat = os.stat(lease_path, follow_symlinks=False)
        except BaseException:
            inspection_failed = True
        if inspection_failed or lease_stat is None:
            self.cleanup_outcome = "operational_failure"
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation partial owner lease inspection failed"
            )
        if not stat.S_ISREG(lease_stat.st_mode) or _PathIdentity.from_stat(lease_stat) != expected:
            self.cleanup_outcome = "refused"
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation partial owner lease identity changed"
            )
        removal_failed = False
        try:
            lease_path.unlink()
        except BaseException:
            removal_failed = True
        if removal_failed:
            self.cleanup_outcome = "operational_failure"
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation partial owner lease removal failed"
            )
        if os.path.lexists(lease_path):
            self.cleanup_outcome = "refused"
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation partial owner lease reappeared during cleanup"
            )
        self._lease_identity = None

    def sqlite_pragmas(self) -> dict[str, int | str]:
        self._assert_creator_process()
        with self._operation_lock:
            self._assert_owner_thread()
            connection = self._connection()
            return {
                "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
                "cache_size": int(connection.execute("PRAGMA cache_size").fetchone()[0]),
                "mmap_size": int(connection.execute("PRAGMA mmap_size").fetchone()[0]),
                "temp_store": int(connection.execute("PRAGMA temp_store").fetchone()[0]),
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
                "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                "locking_mode": str(connection.execute("PRAGMA locking_mode").fetchone()[0]),
                "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "trusted_schema": int(connection.execute("PRAGMA trusted_schema").fetchone()[0]),
                "max_page_count": int(connection.execute("PRAGMA max_page_count").fetchone()[0]),
            }

    def retained_query_plans(self) -> tuple[str, ...]:
        """Expose statement-ordered preparation query plans for retained evidence."""
        self._assert_creator_process()
        with self._operation_lock:
            self._assert_owner_thread()
            self._connection()
            return tuple(
                detail
                for statement_plan in self._retained_query_plans_by_statement()
                for detail in statement_plan
            )

    def _retained_query_plans_by_statement(self) -> tuple[tuple[str, ...], ...]:
        connection = self._connection()
        statements = (
            (_NODE_SELECTION_SQL, (257,)),
            (_EDGE_SELECTION_SQL, (257,)),
            (_OBSERVED_NODE_SELECTION_SQL, ()),
        )
        return tuple(
            tuple(
                str(row[3])
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                ).fetchall()
            )
            for statement, parameters in statements
        )

    def _assert_pragmas(self) -> None:
        config = self.config
        expected: dict[str, int | str] = {
            "page_size": config.page_bytes,
            "cache_size": -config.cache_bytes // 1024,
            "mmap_size": 0,
            "temp_store": 2,
            "journal_mode": "off",
            "synchronous": 0,
            "locking_mode": "exclusive",
            "foreign_keys": 1,
            "trusted_schema": 0,
            "max_page_count": (config.max_spill_bytes - config.control_reserve_bytes)
            // config.page_bytes,
        }
        if self.sqlite_pragmas() != expected:
            raise WorkflowProgressPreparationConfigurationError(
                "SQLite did not apply the exact preparation-v1 PRAGMA profile"
            )

    def _assert_query_plans(self) -> None:
        statement_plans = self._retained_query_plans_by_statement()
        normalized = tuple(
            tuple(" ".join(detail.upper().split()) for detail in statement_plan)
            for statement_plan in statement_plans
        )
        plans = tuple(detail for statement_plan in normalized for detail in statement_plan)
        prohibited = ("TEMP B-TREE", "MATERIALIZE", "AUTOMATIC")
        if any(marker in plan for marker in prohibited for plan in plans):
            raise WorkflowProgressPreparationConfigurationError(
                "preparation-v1 query plan requires unbudgeted temporary storage"
            )
        expected = (
            ("SCAN NODES",),
            (
                "SCAN E",
                "SEARCH SOURCE_NODE USING PRIMARY KEY (NODE_ID=?)",
                "SEARCH TARGET_NODE USING PRIMARY KEY (NODE_ID=?)",
            ),
            ("SCAN NODES",),
        )
        if normalized != expected:
            raise WorkflowProgressPreparationConfigurationError(
                "preparation-v1 query plan drifted from its primary-key ordering contract"
            )

    @_poison_on_failure
    def prepare_topology(
        self,
        identity: WorkflowRunIdentity,
        topology_version: int,
        nodes: Iterable[Mapping[str, Any]],
        edges: Iterable[Mapping[str, Any]],
    ) -> PreparedWorkflowProgressTopologyCandidate:
        """Stream one-shot topology inputs through exact SQLite identity state."""
        if self._phase != "topology":
            raise RuntimeError("topology preparation may run exactly once per workspace")
        self._check_cancellation()
        storage._validate_run_identity(identity)
        if (
            type(topology_version) is not int
            or topology_version <= 0
            or topology_version > storage.WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
        ):
            raise storage.WorkflowProgressStorageError(
                "topology_version must be a positive integer within the durable range"
            )

        reasons: set[str] = set()
        connection = self._connection()
        node_batch_items = 0
        node_batch_bytes = 0
        for value in self._cancelable_values(nodes):
            self.observed_node_count += 1
            self._check_item_limit(
                "node",
                self.observed_node_count,
                self.config.max_node_items,
            )
            node = storage._exact_mapping(value, storage._TOPOLOGY_NODE_KEYS, "topology node")
            try:
                node_id = storage._bounded_identity_text(
                    node["node_id"],
                    "topology node_id",
                    max_bytes=storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
                )
            except storage.WorkflowProgressStorageLimitError:
                reasons.add(storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
                continue
            if node_id is None:
                raise AssertionError("non-null topology node identity normalized to None")
            node_key = node_id.encode("utf-8")
            node_batch_items, node_batch_bytes = self._prepare_batch_for_potential_item(
                len(node_key) + storage.WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES,
                items=node_batch_items,
                decoded_bytes=node_batch_bytes,
            )
            self._insert_node_identity(node_key)
            payload: bytes | None = None
            kind: str | None = None
            try:
                normalized, truncated = storage._normalize_topology_node(value)
            except storage.WorkflowProgressStorageLimitError:
                reasons.add(storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
                item_bytes = len(node_key) + storage.WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES
            else:
                if truncated:
                    reasons.add(storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
                payload = storage._canonical_json_bytes(normalized)
                kind = str(normalized["kind"])
                item_bytes = len(node_key) + len(payload)
            if payload is not None:
                connection.execute(
                    "UPDATE nodes SET payload = ?, kind = ? WHERE node_id = ?",
                    (payload, kind, node_key),
                )
            node_batch_items, node_batch_bytes = self._record_batch_item(
                item_bytes,
                items=node_batch_items,
                decoded_bytes=node_batch_bytes,
            )
        if node_batch_items:
            self._flush_batch()
        self._check_cancellation()
        node_rows = self._select_node_rows()
        if len(node_rows) > storage.WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS:
            reasons.add(storage.WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value)
            node_rows = node_rows[: storage.WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS]
        normalized_nodes = [json.loads(bytes(row["payload"])) for row in node_rows]
        retained_node_ids = {str(item["node_id"]) for item in normalized_nodes}
        self._set_retained_nodes(retained_node_ids)

        edge_batch_items = 0
        edge_batch_bytes = 0
        for value in self._cancelable_values(edges):
            self.observed_edge_count += 1
            self._check_item_limit(
                "edge",
                self.observed_edge_count,
                self.config.max_edge_items,
            )
            try:
                normalized = storage._normalize_topology_edge(value)
            except storage.WorkflowProgressStorageLimitError:
                reasons.add(storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
                continue
            source_key = normalized["source"].encode("utf-8")
            target_key = normalized["target"].encode("utf-8")
            item_bytes = len(source_key) + len(target_key)
            edge_batch_items, edge_batch_bytes = self._reserve_batch_item(
                item_bytes,
                items=edge_batch_items,
                decoded_bytes=edge_batch_bytes,
            )
            self._insert_edge(source_key, target_key)
        if edge_batch_items:
            self._flush_batch()
        self._check_cancellation()
        edge_rows = self._select_edge_rows()
        if len(edge_rows) > storage.WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS:
            reasons.add(storage.WorkflowProgressTruncationReason.EDGE_COUNT_LIMIT.value)
            edge_rows = edge_rows[: storage.WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS]
        normalized_edges = [
            {
                "source": bytes(row["source"]).decode("utf-8"),
                "target": bytes(row["target"]).decode("utf-8"),
            }
            for row in edge_rows
        ]

        self._check_cancellation()
        prepared = self._assemble_topology(
            identity=identity,
            topology_version=topology_version,
            normalized_nodes=normalized_nodes,
            normalized_edges=normalized_edges,
            reasons=reasons,
        )
        self._check_cancellation()
        self._prepared_topology = prepared
        self._prepared_topology_signature = _candidate_signature(prepared)
        self._phase = "prepared"
        return prepared

    @_poison_on_failure
    def prepare_legacy_detachment(
        self,
        topology: PreparedWorkflowProgressTopologyCandidate,
    ) -> None:
        """Capture the one O(observed) compatibility value before cleanup.

        Issue #142 removes this residue by keeping topology and detail in one
        workspace lifetime. Until then, the public preparer must preserve the
        historical ``observed_node_ids`` field exactly.
        """
        if (
            self._phase != "prepared"
            or topology is not self._prepared_topology
            or not _candidate_signature_matches(topology, self._prepared_topology_signature)
        ):
            raise RuntimeError("legacy detachment requires this workspace's sealed topology")
        self._check_cancellation()
        self._legacy_observed_node_ids = frozenset(self._iter_observed_node_ids())
        self._check_cancellation()
        self._measure_spill()
        self._legacy_detachment_ready = True
        self._phase = "legacy_ready"

    def detach_legacy_topology(
        self,
        topology: PreparedWorkflowProgressTopologyCandidate,
    ) -> storage.PreparedWorkflowProgressTopology:
        """Issue a legacy-compatible capability only after successful cleanup."""
        self._assert_creator_process()
        self._assert_owner_thread()
        if (
            self._phase != "closed"
            or self.cleanup_outcome != "removed"
            or self.path_exists
            or not self._legacy_detachment_ready
            or topology is not self._prepared_topology
            or not _candidate_signature_matches(topology, self._prepared_topology_signature)
            or self._legacy_observed_node_ids is None
            or topology._workspace_pid != self._creator_pid
        ):
            raise RuntimeError(
                "legacy topology cannot detach before owned workspace cleanup succeeds"
            )
        prepared = storage.PreparedWorkflowProgressTopology(
            identity=topology.identity,
            topology_version=topology.topology_version,
            manifest_payload=topology.manifest_payload,
            manifest_digest=topology.manifest_digest,
            pages=topology.pages,
            node_ids=topology.node_ids,
            observed_node_ids=self._legacy_observed_node_ids,
            node_kinds=topology.node_kinds,
            edges=topology.edges,
            observed_node_count=topology.observed_node_count,
            observed_edge_count=topology.observed_edge_count,
            retained_node_count=topology.retained_node_count,
            retained_edge_count=topology.retained_edge_count,
            encoded_bytes=topology.encoded_bytes,
            decoded_bytes=topology.decoded_bytes,
            truncation_reasons=topology.truncation_reasons,
            map_node_ids=topology.map_node_ids,
        )
        storage._register_prepared_topology_capability(
            prepared,
            trust_observed_node_ids=True,
        )
        self._prepared_topology = None
        self._prepared_topology_signature = None
        self._legacy_observed_node_ids = None
        self._legacy_detachment_ready = False
        self._phase = "detached"
        return prepared

    def _configure_connection(self) -> None:
        connection = self._connection()
        config = self.config
        connection.execute(f"PRAGMA page_size = {config.page_bytes}")
        connection.execute(f"PRAGMA cache_size = {-config.cache_bytes // 1024}")
        connection.execute(f"PRAGMA mmap_size = {config.mmap_bytes}")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        database_budget = config.max_spill_bytes - config.control_reserve_bytes
        connection.execute(f"PRAGMA max_page_count = {database_budget // config.page_bytes}")

    def _create_schema(self) -> None:
        self._connection().executescript(
            """
            CREATE TABLE nodes (
                node_id BLOB PRIMARY KEY,
                payload BLOB,
                kind TEXT,
                retained INTEGER NOT NULL DEFAULT 0 CHECK (retained IN (0, 1))
            ) WITHOUT ROWID;
            CREATE TABLE edges (
                source BLOB NOT NULL,
                target BLOB NOT NULL,
                PRIMARY KEY (source, target),
                FOREIGN KEY (source) REFERENCES nodes(node_id),
                FOREIGN KEY (target) REFERENCES nodes(node_id)
            ) WITHOUT ROWID;
            """
        )

    def _select_node_rows(self) -> list[sqlite3.Row]:
        return (
            self._connection()
            .execute(
                _NODE_SELECTION_SQL,
                (storage.WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS + 1,),
            )
            .fetchall()
        )

    def _select_edge_rows(self) -> list[sqlite3.Row]:
        return (
            self._connection()
            .execute(
                _EDGE_SELECTION_SQL,
                (storage.WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS + 1,),
            )
            .fetchall()
        )

    def _iter_observed_node_ids(self) -> Iterator[str]:
        """Scan compatibility identities with one bounded cancellation batch."""
        cursor = self._connection().execute(_OBSERVED_NODE_SELECTION_SQL)
        try:
            while True:
                self._check_cancellation()
                rows = cursor.fetchmany(self.config.batch_max_items)
                if not rows:
                    return
                for row in rows:
                    yield bytes(row["node_id"]).decode("utf-8")
        finally:
            cursor.close()

    def _assemble_topology(
        self,
        *,
        identity: WorkflowRunIdentity,
        topology_version: int,
        normalized_nodes: list[dict[str, Any]],
        normalized_edges: list[dict[str, str]],
        reasons: set[str],
    ) -> PreparedWorkflowProgressTopologyCandidate:
        node_pages, _ = self._build_pages(
            storage.WorkflowProgressTopologyCollection.NODE,
            normalized_nodes,
        )
        edge_pages, _ = self._build_pages(
            storage.WorkflowProgressTopologyCollection.EDGE,
            normalized_edges,
        )
        retained_pages = [*node_pages, *edge_pages]
        retained_nodes = len(normalized_nodes)
        retained_edges = len(normalized_edges)
        while True:
            manifest_payload = storage._topology_manifest_payload(
                identity=identity,
                topology_version=topology_version,
                pages=retained_pages,
                node_count=retained_nodes,
                edge_count=retained_edges,
                truncation_reasons=sorted(reasons),
            )
            encoded_bytes = len(manifest_payload) + sum(
                page.encoded_bytes for page in retained_pages
            )
            decoded_bytes = len(manifest_payload) + sum(
                page.decoded_bytes for page in retained_pages
            )
            manifest_fits = (
                len(manifest_payload)
                <= storage.WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES
            )
            encoded_fits = encoded_bytes <= storage.WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES
            decoded_fits = decoded_bytes <= storage.WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES
            if manifest_fits and encoded_fits and decoded_fits:
                break
            if not encoded_fits or not manifest_fits:
                reasons.add(storage.WorkflowProgressTruncationReason.TOPOLOGY_ENCODED_BYTES.value)
            if not decoded_fits:
                reasons.add(storage.WorkflowProgressTruncationReason.TOPOLOGY_DECODED_BYTES.value)
            if not retained_pages:
                raise storage.WorkflowProgressStorageLimitError(
                    "empty topology manifest exceeds the protocol byte limit"
                )
            removed_page = retained_pages.pop()
            if removed_page.collection is storage.WorkflowProgressTopologyCollection.NODE:
                retained_nodes -= removed_page.item_count
            else:
                retained_edges -= removed_page.item_count

        retained_node_records = normalized_nodes[:retained_nodes]
        node_ids = frozenset(str(item["node_id"]) for item in retained_node_records)
        return PreparedWorkflowProgressTopologyCandidate(
            identity=identity,
            topology_version=topology_version,
            manifest_payload=manifest_payload,
            manifest_digest=storage._digest(storage._MANIFEST_DOMAIN, manifest_payload),
            pages=tuple(retained_pages),
            node_ids=node_ids,
            node_kinds=tuple(
                (str(item["node_id"]), str(item["kind"])) for item in retained_node_records
            ),
            edges=tuple(
                (str(item["source"]), str(item["target"]))
                for item in normalized_edges[:retained_edges]
            ),
            observed_node_count=self.observed_node_count,
            observed_edge_count=self.observed_edge_count,
            retained_node_count=retained_nodes,
            retained_edge_count=retained_edges,
            encoded_bytes=encoded_bytes,
            decoded_bytes=decoded_bytes,
            truncation_reasons=tuple(sorted(reasons)),
            map_node_ids=frozenset(
                str(item["node_id"]) for item in retained_node_records if item["kind"] == "map"
            ),
            _workspace_token=self.token,
            _workspace_pid=os.getpid(),
        )

    def _build_pages(
        self,
        collection: storage.WorkflowProgressTopologyCollection,
        records: list[dict[str, Any]],
    ) -> tuple[list[storage.PreparedWorkflowProgressTopologyPage], int]:
        """Build canonical pages with one cancellation boundary per output page."""
        pages: list[storage.PreparedWorkflowProgressTopologyPage] = []
        consumed = 0
        while consumed < len(records):
            self._check_cancellation()
            page_records: list[dict[str, Any]] = []
            while consumed + len(page_records) < len(records):
                candidate = records[consumed + len(page_records)]
                trial = {
                    "collection": collection.value,
                    "records": [*page_records, candidate],
                    "schema_version": storage.WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
                }
                encoded = storage._canonical_json_bytes(trial)
                if page_records and (
                    len(page_records) >= storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS
                    or len(encoded) > storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
                    or len(encoded) > storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
                ):
                    break
                if (
                    len(encoded) > storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES
                    or len(encoded) > storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES
                ):
                    raise storage.WorkflowProgressStorageLimitError(
                        "one topology record cannot fit in a storage page"
                    )
                page_records.append(candidate)
                if len(page_records) == storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS:
                    break
            payload = storage._canonical_json_bytes(
                {
                    "collection": collection.value,
                    "records": page_records,
                    "schema_version": storage.WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
                }
            )
            pages.append(
                storage.PreparedWorkflowProgressTopologyPage(
                    collection=collection,
                    page_index=len(pages),
                    payload=payload,
                    digest=storage._digest(storage._PAGE_DOMAIN, payload),
                    item_count=len(page_records),
                    encoded_bytes=len(payload),
                    decoded_bytes=len(payload),
                )
            )
            consumed += len(page_records)
        return pages, consumed

    def _insert_node_identity(self, node_key: bytes) -> None:
        try:
            self._connection().execute(
                "INSERT INTO nodes(node_id, retained) VALUES (?, 0)",
                (node_key,),
            )
        except sqlite3.IntegrityError as error:
            raise storage.WorkflowProgressStorageError(
                "topology contains a duplicate node_id"
            ) from error
        self.spill_items += 1

    def _insert_edge(self, source: bytes, target: bytes) -> None:
        try:
            self._connection().execute(
                "INSERT INTO edges(source, target) VALUES (?, ?)",
                (source, target),
            )
        except sqlite3.IntegrityError as error:
            message = str(error).lower()
            reason = (
                "topology contains a duplicate edge"
                if "unique" in message
                else "topology edge references an unknown node_id"
            )
            raise storage.WorkflowProgressStorageError(reason) from error
        self.spill_items += 1

    def _set_retained_nodes(self, node_ids: set[str]) -> None:
        connection = self._connection()
        values = sorted(node_ids)
        for offset in range(0, len(values), self.config.batch_max_items):
            connection.executemany(
                "UPDATE nodes SET retained = 1 WHERE node_id = ?",
                (
                    (node_id.encode("utf-8"),)
                    for node_id in values[offset : offset + self.config.batch_max_items]
                ),
            )
            self._flush_batch()

    def _require_item_fits_batch(self, decoded_bytes: int) -> None:
        if decoded_bytes > self.config.batch_max_decoded_bytes:
            raise WorkflowProgressPreparationSpillExhaustedError(
                "one decoded preparation item exceeds the 4 MiB batch budget"
            )

    def _reserve_batch_item(
        self,
        item_bytes: int,
        *,
        items: int,
        decoded_bytes: int,
    ) -> tuple[int, int]:
        items, decoded_bytes = self._prepare_batch_for_potential_item(
            item_bytes,
            items=items,
            decoded_bytes=decoded_bytes,
        )
        return self._record_batch_item(
            item_bytes,
            items=items,
            decoded_bytes=decoded_bytes,
        )

    def _prepare_batch_for_potential_item(
        self,
        potential_bytes: int,
        *,
        items: int,
        decoded_bytes: int,
    ) -> tuple[int, int]:
        if items and (
            items >= self.config.batch_max_items
            or decoded_bytes + potential_bytes > self.config.batch_max_decoded_bytes
        ):
            self._flush_batch()
            return 0, 0
        return items, decoded_bytes

    def _record_batch_item(
        self,
        item_bytes: int,
        *,
        items: int,
        decoded_bytes: int,
    ) -> tuple[int, int]:
        self._require_item_fits_batch(item_bytes)
        if (
            items >= self.config.batch_max_items
            or decoded_bytes + item_bytes > self.config.batch_max_decoded_bytes
        ):
            raise AssertionError("preparation item escaped its preflighted transaction batch")
        return items + 1, decoded_bytes + item_bytes

    def _flush_batch(self) -> None:
        self._check_cancellation()
        self._connection().commit()
        self._measure_spill()
        self._check_cancellation()

    def _check_cancellation(self) -> None:
        self.cancellation_check()

    def _cancelable_values(
        self,
        values: Iterable[Mapping[str, Any]],
    ) -> Iterator[Mapping[str, Any]]:
        iterator = iter(values)
        if self._active_input_iterator is not None:
            raise RuntimeError("SQLite preparation input iterators cannot be nested")
        self._active_input_iterator = iterator
        completed = False
        try:
            while True:
                self._check_cancellation()
                try:
                    yield next(iterator)
                except StopIteration:
                    completed = True
                    return
        finally:
            if self._active_input_iterator is iterator:
                self._active_input_iterator = None
                if not completed:
                    self._close_iterator(iterator)

    def _close_active_input_iterator(self) -> None:
        iterator = self._active_input_iterator
        if iterator is None:
            return
        self._active_input_iterator = None
        self._close_iterator(iterator)

    @staticmethod
    def _close_iterator(iterator: Iterator[Mapping[str, Any]]) -> None:
        close = getattr(iterator, "close", None)
        if not callable(close):
            return
        try:
            close()
        except BaseException:
            pass

    def _measure_spill(self) -> None:
        self._assert_creator_process()
        with self._operation_lock:
            self._assert_owner_thread()
            directory = self._owned_directory
            if directory is None or not os.path.lexists(directory):
                raise WorkflowProgressPreparationWorkspaceIntegrityError(
                    "SQLite preparation workspace is missing"
                )
            invalid_path = False
            try:
                directory = self._validated_owned_path()
            except (
                WorkflowProgressPreparationCleanupOperationalError,
                WorkflowProgressPreparationCleanupRefusedError,
            ):
                invalid_path = True
            if invalid_path:
                raise WorkflowProgressPreparationWorkspaceIntegrityError(
                    "SQLite preparation workspace path is invalid"
                )

            expected = {_DATABASE_NAME, _LEASE_NAME}
            total_bytes = 0
            filesystem_failed = False
            entries: list[Path] = []
            lease_size: int | None = None
            try:
                entries = list(directory.iterdir())
                for path in entries:
                    path_stat = os.stat(path, follow_symlinks=False)
                    if path.name not in expected or not stat.S_ISREG(path_stat.st_mode):
                        raise WorkflowProgressPreparationWorkspaceIntegrityError(
                            "unexpected or unsafe SQLite preparation workspace entry"
                        )
                    total_bytes += int(path_stat.st_size)
                if self.lease_path is not None:
                    lease_size = int(os.stat(self.lease_path, follow_symlinks=False).st_size)
            except OSError:
                filesystem_failed = True
            if filesystem_failed:
                raise WorkflowProgressPreparationWorkspaceIntegrityError(
                    "SQLite preparation workspace accounting failed"
                )
            if {path.name for path in entries} != expected:
                raise WorkflowProgressPreparationWorkspaceIntegrityError(
                    "unexpected or missing SQLite preparation workspace entry"
                )
            if self.lease_path is None or lease_size is None:
                raise WorkflowProgressPreparationWorkspaceIntegrityError(
                    "preparation owner lease is missing"
                )
            if lease_size > self.config.control_reserve_bytes:
                raise WorkflowProgressPreparationSpillExhaustedError(
                    "SQLite preparation control reserve byte budget exhausted"
                )
            self.spill_peak_bytes = max(self.spill_peak_bytes, total_bytes)
            if total_bytes > self.config.max_spill_bytes:
                raise WorkflowProgressPreparationSpillExhaustedError(
                    "SQLite preparation spill byte budget exhausted"
                )

    @staticmethod
    def _check_item_limit(kind: str, count: int, maximum: int) -> None:
        if count > maximum:
            raise WorkflowProgressPreparationSpillExhaustedError(
                f"SQLite preparation {kind} item budget exhausted at {maximum}"
            )

    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("SQLite preparation workspace is not open")
        self._assert_creator_process()
        self._assert_owner_thread()
        return self.connection

    def _close_and_remove(
        self,
        *,
        acquisition_failure: bool = False,
        wait_for_operations: bool = True,
    ) -> None:
        self._assert_creator_process()
        acquired = self._operation_lock.acquire(blocking=wait_for_operations)
        if not acquired:
            self.cleanup_outcome = "busy"
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation cleanup found an active owner operation"
            )
        try:
            connection = self.connection
            if connection is not None:
                close_failed = False
                try:
                    connection.close()
                except Exception:
                    close_failed = True
                if close_failed:
                    self.cleanup_outcome = "operational_failure"
                    raise WorkflowProgressPreparationCleanupOperationalError(
                        "SQLite preparation connection close failed"
                    )
                self.connection = None

            directory = self._owned_directory
            if directory is None:
                self.cleanup_outcome = "not_created"
                _discard_live_workspace(self)
                return
            if self._cleanup_quarantine is None:
                if not os.path.lexists(directory):
                    self.cleanup_outcome = "refused"
                    raise WorkflowProgressPreparationCleanupRefusedError(
                        "SQLite preparation workspace disappeared before owned cleanup"
                    )
                self._run_cleanup_validation(
                    lambda: self._validate_owned_directory(acquisition_failure=acquisition_failure)
                )
                quarantine = self._run_cleanup_validation(self._quarantine_owned_directory)
                self._run_cleanup_validation(
                    lambda: self._validate_quarantined_directory(
                        quarantine,
                        acquisition_failure=acquisition_failure,
                        require_lease=True,
                    )
                )
            else:
                quarantine = self._run_cleanup_validation(self._validated_cleanup_quarantine)
                self._run_cleanup_validation(
                    lambda: self._validate_quarantined_directory(
                        quarantine,
                        acquisition_failure=True,
                        require_lease=False,
                    )
                )
            self._remove_quarantined_directory(quarantine, leased=True)
        finally:
            self._operation_lock.release()

    def _remove_empty_acquisition_directory(self) -> None:
        """Remove only the exact empty directory created before a lease was proven."""
        if self._cleanup_quarantine is None:
            directory = self._run_cleanup_validation(self._validated_owned_path)
        else:
            directory = self._run_cleanup_validation(self._validated_cleanup_quarantine)
        inspection_failed = False
        entries: list[Path] = []
        try:
            entries = list(directory.iterdir())
        except OSError:
            inspection_failed = True
        if inspection_failed:
            self.cleanup_outcome = "operational_failure"
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation unleased-directory inspection failed"
            )
        if entries:
            self.cleanup_outcome = "refused"
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation unleased directory is not empty"
            )
        quarantine = (
            self._run_cleanup_validation(self._quarantine_owned_directory)
            if self._cleanup_quarantine is None
            else directory
        )
        quarantine = self._run_cleanup_validation(self._validated_cleanup_quarantine)
        inspection_failed = False
        try:
            entries = list(quarantine.iterdir())
        except OSError:
            inspection_failed = True
        if inspection_failed:
            self.cleanup_outcome = "operational_failure"
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation unleased-directory inspection failed"
            )
        if entries:
            self.cleanup_outcome = "refused"
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation quarantined unleased directory is not empty"
            )
        self._remove_quarantined_directory(quarantine, leased=False)

    def _run_cleanup_validation(self, function: Callable[[], Any]) -> Any:
        try:
            return function()
        except WorkflowProgressPreparationCleanupRefusedError:
            self.cleanup_outcome = "refused"
            raise
        except WorkflowProgressPreparationCleanupOperationalError:
            self.cleanup_outcome = "operational_failure"
            raise

    def _quarantine_owned_directory(self) -> Path:
        """Atomically move the exact workspace before deleting any owned file."""
        if self._cleanup_quarantine is not None:
            return self._validated_cleanup_quarantine()
        directory = self._validated_owned_path()
        parent = self._owned_parent
        if parent is None:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup parent is unavailable"
            )
        quarantine = parent / f"{_QUARANTINE_PREFIX}{uuid4()}"
        if os.path.lexists(quarantine):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine already exists"
            )
        rename_failed = False
        try:
            directory.rename(quarantine)
        except OSError:
            rename_failed = True
        if rename_failed:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation workspace quarantine rename failed"
            )
        self._cleanup_quarantine = quarantine
        return self._validated_cleanup_quarantine()

    def _validated_cleanup_quarantine(self) -> Path:
        quarantine = self._cleanup_quarantine
        directory = self._owned_directory
        parent = self._owned_parent
        expected_directory = self._owned_directory_identity
        expected_parent = self._owned_parent_identity
        if (
            quarantine is None
            or directory is None
            or parent is None
            or expected_directory is None
            or expected_parent is None
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine identity is incomplete"
            )
        if os.path.lexists(directory):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace source reappeared during cleanup"
            )
        if not os.path.lexists(quarantine):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine is missing"
            )
        inspection_failed = False
        quarantine_stat: os.stat_result | None = None
        parent_stat: os.stat_result | None = None
        try:
            quarantine_stat = os.stat(quarantine, follow_symlinks=False)
            parent_stat = os.stat(parent, follow_symlinks=False)
        except OSError:
            inspection_failed = True
        if inspection_failed or quarantine_stat is None or parent_stat is None:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation cleanup quarantine inspection failed"
            )
        if (
            not stat.S_ISDIR(quarantine_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
            or quarantine.parent != parent
            or not quarantine.name.startswith(_QUARANTINE_PREFIX)
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine was replaced or redirected"
            )
        quarantine_id = quarantine.name.removeprefix(_QUARANTINE_PREFIX)
        try:
            valid_quarantine_id = str(UUID(quarantine_id)) == quarantine_id
        except ValueError:
            valid_quarantine_id = False
        if not valid_quarantine_id:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine has no canonical UUID"
            )
        if (
            _PathIdentity.from_stat(quarantine_stat) != expected_directory
            or _PathIdentity.from_stat(parent_stat) != expected_parent
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine identity changed"
            )
        unsafe_parent = False
        try:
            self._validate_acquisition_parent_stat(parent_stat)
        except WorkflowProgressPreparationWorkspaceAcquisitionError:
            unsafe_parent = True
        if unsafe_parent:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup parent became unsafe"
            )
        if os.name == "posix" and (
            int(quarantine_stat.st_uid) != self._creator_pid_owner_uid()
            or stat.S_IMODE(quarantine_stat.st_mode) != 0o700
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine permissions became unsafe"
            )
        if os.path.lexists(directory):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace source reappeared during cleanup"
            )
        return quarantine

    def _validate_quarantined_directory(
        self,
        quarantine: Path,
        *,
        acquisition_failure: bool,
        require_lease: bool,
    ) -> None:
        if self._validated_cleanup_quarantine() != quarantine:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation cleanup quarantine path changed"
            )
        directory = self._owned_directory
        if directory is None:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace identity is unavailable"
            )
        workspace_id = directory.name.removeprefix(_WORKSPACE_PREFIX)
        self._validate_directory_inventory(
            quarantine,
            workspace_id=workspace_id,
            acquisition_failure=acquisition_failure,
            require_lease=require_lease,
        )
        self._validated_cleanup_quarantine()

    def _remove_quarantined_directory(self, quarantine: Path, *, leased: bool) -> None:
        quarantine = self._run_cleanup_validation(self._validated_cleanup_quarantine)
        if leased:
            self._run_cleanup_validation(
                lambda: self._validate_quarantined_directory(
                    quarantine,
                    acquisition_failure=True,
                    require_lease=False,
                )
            )
        removal_failed = False
        try:
            for name in (_DATABASE_NAME, _LEASE_NAME) if leased else ():
                self._run_cleanup_validation(self._validated_cleanup_quarantine)
                path = quarantine / name
                if os.path.lexists(path):
                    path_stat = os.stat(path, follow_symlinks=False)
                    if not stat.S_ISREG(path_stat.st_mode):
                        raise WorkflowProgressPreparationCleanupRefusedError(
                            "SQLite preparation cleanup entry identity changed"
                        )
                    if (
                        name == _LEASE_NAME
                        and self._lease_identity is not None
                        and _PathIdentity.from_stat(path_stat) != self._lease_identity
                    ):
                        raise WorkflowProgressPreparationCleanupRefusedError(
                            "SQLite preparation owner lease identity changed"
                        )
                    path.unlink()
            self._run_cleanup_validation(self._validated_cleanup_quarantine)
            if list(quarantine.iterdir()):
                raise WorkflowProgressPreparationCleanupRefusedError(
                    "SQLite preparation cleanup quarantine contains an unowned entry"
                )
            quarantine.rmdir()
        except WorkflowProgressPreparationCleanupRefusedError:
            self.cleanup_outcome = "refused"
            raise
        except WorkflowProgressPreparationCleanupOperationalError:
            self.cleanup_outcome = "operational_failure"
            raise
        except OSError:
            removal_failed = True
        if removal_failed:
            self.cleanup_outcome = "operational_failure"
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation owned-file removal failed"
                if leased
                else "SQLite preparation unleased-directory removal failed"
            )
        directory = self._owned_directory
        if os.path.lexists(quarantine) or (directory is not None and os.path.lexists(directory)):
            self.cleanup_outcome = "refused"
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace reappeared during cleanup"
            )
        self._cleanup_quarantine = None
        self._lease_identity = None
        self.cleanup_outcome = "removed"
        _discard_live_workspace(self)

    def _validated_owned_path(self) -> Path:
        directory = self._owned_directory
        parent = self._owned_parent
        expected_directory = self._owned_directory_identity
        expected_parent = self._owned_parent_identity
        if (
            directory is None
            or parent is None
            or expected_directory is None
            or expected_parent is None
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace ownership identity is incomplete"
            )
        if not os.path.lexists(directory):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace ownership path is missing"
            )
        inspection_failed = False
        directory_stat: os.stat_result | None = None
        parent_stat: os.stat_result | None = None
        try:
            directory_stat = os.stat(directory, follow_symlinks=False)
            parent_stat = os.stat(parent, follow_symlinks=False)
        except OSError:
            inspection_failed = True
        if inspection_failed:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation workspace ownership inspection failed"
            )
        if (
            directory_stat is None
            or parent_stat is None
            or not stat.S_ISDIR(directory_stat.st_mode)
            or not stat.S_ISDIR(parent_stat.st_mode)
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace path was replaced or redirected"
            )
        resolved_directory: Path | None = None
        resolved_parent: Path | None = None
        try:
            resolved_directory = directory.resolve(strict=True)
            resolved_parent = parent.resolve(strict=True)
        except (OSError, RuntimeError):
            inspection_failed = True
        if inspection_failed:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation workspace canonical-path inspection failed"
            )
        if (
            resolved_directory != directory
            or resolved_parent != parent
            or directory.parent != parent
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace path was replaced or redirected"
            )
        if (
            _PathIdentity.from_stat(directory_stat) != expected_directory
            or _PathIdentity.from_stat(parent_stat) != expected_parent
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace ownership identity changed"
            )
        unsafe_parent = False
        try:
            self._validate_acquisition_parent_stat(parent_stat)
        except WorkflowProgressPreparationWorkspaceAcquisitionError:
            unsafe_parent = True
        if unsafe_parent:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace parent became unsafe"
            )
        if os.name == "posix" and (
            int(directory_stat.st_uid) != self._creator_pid_owner_uid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace permissions became unsafe"
            )
        if not directory.name.startswith(_WORKSPACE_PREFIX):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace name has no owned prefix"
            )
        workspace_id = directory.name.removeprefix(_WORKSPACE_PREFIX)
        valid_workspace_id = True
        try:
            if str(UUID(workspace_id)) != workspace_id:
                valid_workspace_id = False
        except ValueError:
            valid_workspace_id = False
        if not valid_workspace_id:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace name has no canonical UUID"
            )
        return directory

    def _creator_pid_owner_uid(self) -> int:
        if self._creator_uid is None:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace owner identity is incomplete"
            )
        return self._creator_uid

    def _validate_owned_directory(self, *, acquisition_failure: bool) -> None:
        directory = self._validated_owned_path()
        workspace_id = directory.name.removeprefix(_WORKSPACE_PREFIX)
        self._validate_directory_inventory(
            directory,
            workspace_id=workspace_id,
            acquisition_failure=acquisition_failure,
            require_lease=True,
        )

    def _validate_directory_inventory(
        self,
        directory: Path,
        *,
        workspace_id: str,
        acquisition_failure: bool,
        require_lease: bool,
    ) -> None:
        inspection_failed = False
        entries: list[Path] = []
        try:
            entries = list(directory.iterdir())
        except OSError:
            inspection_failed = True
        if inspection_failed:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation cleanup inventory failed"
            )
        expected = {_DATABASE_NAME, _LEASE_NAME}
        invalid_entry = False
        try:
            invalid_entry = any(
                entry.name not in expected
                or not stat.S_ISREG(os.stat(entry, follow_symlinks=False).st_mode)
                for entry in entries
            )
        except OSError:
            inspection_failed = True
        if inspection_failed:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation cleanup entry inspection failed"
            )
        if invalid_entry or (
            not acquisition_failure and {entry.name for entry in entries} != expected
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation workspace contains an unowned entry"
            )
        lease = directory / _LEASE_NAME
        if not os.path.lexists(lease):
            if not require_lease:
                return
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation owner lease read failed"
            )
        lease_read_failed = False
        lease_invalid = False
        lease_stat: os.stat_result | None = None
        value: object = None
        try:
            lease_stat = os.stat(lease, follow_symlinks=False)
        except OSError:
            lease_read_failed = True
        if lease_read_failed or lease_stat is None:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation owner lease read failed"
            )
        if lease_stat.st_size > self.config.control_reserve_bytes:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation owner lease exceeds the control reserve"
            )
        try:
            value = json.loads(lease.read_text(encoding="utf-8", errors="strict"))
        except OSError:
            lease_read_failed = True
        except (UnicodeError, ValueError, RecursionError):
            lease_invalid = True
        if lease_read_failed:
            raise WorkflowProgressPreparationCleanupOperationalError(
                "SQLite preparation owner lease read failed"
            )
        if lease_invalid:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation owner lease is invalid"
            )
        if (
            self._lease_identity is None
            or _PathIdentity.from_stat(lease_stat) != self._lease_identity
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation owner lease identity changed"
            )
        if os.name == "posix" and (
            int(lease_stat.st_uid) != self._creator_pid_owner_uid()
            or stat.S_IMODE(lease_stat.st_mode) != 0o600
        ):
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation owner lease permissions are unsafe"
            )
        if not isinstance(value, dict) or value.get("token") != self.token:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation owner lease token does not match"
            )
        if value.get("workspace_id") != workspace_id:
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation owner lease UUID does not match"
            )
        if value.get("pid") != self._creator_pid or self._creator_pid != os.getpid():
            raise WorkflowProgressPreparationCleanupRefusedError(
                "SQLite preparation owner lease PID does not match"
            )


def _register_live_workspace(workspace: SQLitePreparationWorkspace) -> None:
    with _LIVE_WORKSPACES_LOCK:
        _LIVE_WORKSPACES.add(workspace)


def _discard_live_workspace(workspace: SQLitePreparationWorkspace) -> None:
    with _LIVE_WORKSPACES_LOCK:
        _LIVE_WORKSPACES.discard(workspace)


def _reset_live_workspaces_after_fork() -> None:
    """Disable inherited workspace cleanup authority and replace lock state."""
    global _LIVE_WORKSPACES
    global _LIVE_WORKSPACES_LOCK
    inherited = tuple(_LIVE_WORKSPACES)
    for workspace in inherited:
        workspace._phase = "poisoned"
    _LIVE_WORKSPACES = set()
    _LIVE_WORKSPACES_LOCK = Lock()


def _cleanup_live_workspaces_at_exit() -> None:
    """Best-effort cleanup for graceful interpreter termination."""
    with _LIVE_WORKSPACES_LOCK:
        workspaces = tuple(_LIVE_WORKSPACES)
    for workspace in workspaces:
        try:
            workspace._close_and_remove(wait_for_operations=False)
        except BaseException:
            continue


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_live_workspaces_after_fork)


atexit.register(_cleanup_live_workspaces_at_exit)


def prepare_workflow_progress_topology(
    identity: WorkflowRunIdentity,
    topology_version: int,
    nodes: Iterable[Mapping[str, Any]],
    edges: Iterable[Mapping[str, Any]],
) -> storage.PreparedWorkflowProgressTopology:
    """Prepare legacy-compatible topology through bounded external state.

    The SQLite workspace owns all complete duplicate/reference state. The only
    O(observed) Python value produced is the legacy ``observed_node_ids`` field,
    captured immediately before cleanup for compatibility until issue #142.
    """
    workspace = SQLitePreparationWorkspace()
    with workspace:
        candidate = workspace.prepare_topology(
            identity,
            topology_version,
            nodes,
            edges,
        )
        workspace.prepare_legacy_detachment(candidate)
    return workspace.detach_legacy_topology(candidate)


__all__ = [
    "WorkflowProgressPreparationConfigurationError",
    "WorkflowProgressPreparationCleanupOperationalError",
    "WorkflowProgressPreparationCleanupRefusedError",
    "WorkflowProgressPreparationWorkspaceAcquisitionError",
    "PreparedWorkflowProgressTopologyCandidate",
    "WorkflowProgressPreparationSpillExhaustedError",
    "WorkflowProgressPreparationWorkspaceIntegrityError",
    "SQLITE_BATCH_MAX_DECODED_BYTES",
    "SQLITE_BATCH_MAX_ITEMS",
    "SQLITE_CACHE_BYTES",
    "SQLITE_CONTROL_RESERVE_BYTES",
    "SQLITE_EDGE_MAX_ITEMS",
    "SQLITE_MMAP_BYTES",
    "SQLITE_NODE_MAX_ITEMS",
    "SQLITE_PAGE_BYTES",
    "SQLITE_SPILL_MAX_BYTES",
    "SQLitePreparationConfig",
    "SQLitePreparationWorkspace",
    "canonical_topology_evidence",
    "prepare_workflow_progress_topology",
]
