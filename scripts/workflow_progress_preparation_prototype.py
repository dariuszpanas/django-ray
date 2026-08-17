"""Non-production SQLite prototype for bounded workflow-progress preparation.

The runtime does not import this module.  It exists to exercise the preparation
contract selected by issue #140 while schema-v3 producer activation remains off.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from secrets import token_hex
from typing import Any, Never
from uuid import UUID, uuid4

import django_ray.workflow.progress.storage as storage
from django_ray.runtime.context import WorkflowRunIdentity

SQLITE_PAGE_BYTES = 4 * 1024
SQLITE_CACHE_BYTES = 8 * 1024 * 1024
SQLITE_MMAP_BYTES = 0
SQLITE_SPILL_MAX_BYTES = 1024 * 1024 * 1024
SQLITE_CONTROL_RESERVE_BYTES = 4 * 1024 * 1024
SQLITE_NODE_MAX_ITEMS = 1_000_000
SQLITE_EDGE_MAX_ITEMS = 4_000_000
SQLITE_DETAIL_MAX_ITEMS = 1_000_000
SQLITE_BATCH_MAX_ITEMS = 256
SQLITE_BATCH_MAX_DECODED_BYTES = 4 * 1024 * 1024

_MINIMUM_SPILL_BYTES = 64 * 1024
_WORKSPACE_PREFIX = "django-ray-preparation-"
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
_DETAIL_SELECTION_SQL = "SELECT * FROM detail WHERE payload IS NOT NULL ORDER BY node_id"


def _poison_on_failure(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(self: SQLitePreparationWorkspace, *args: Any, **kwargs: Any) -> Any:
        try:
            return function(self, *args, **kwargs)
        except BaseException:
            self._close_active_input_iterator()
            self._phase = "poisoned"
            raise

    return wrapped


class PrototypeConfigurationError(ValueError):
    """Raised when the experimental workspace budget is invalid."""


class PrototypeSpillExhaustedError(RuntimeError):
    """Raised when an explicit item or file-byte budget is exhausted."""


class PrototypeCleanupRefusedError(RuntimeError):
    """Raised when workspace ownership cannot be proven before deletion."""


class PrototypeWorkspaceIntegrityError(RuntimeError):
    """Raised when unexpected files escape the frozen SQLite profile."""


class _BudgetConnection(sqlite3.Connection):
    """Translate SQLite's max-page failure into the prototype contract."""

    @staticmethod
    def _translate(error: sqlite3.OperationalError) -> Never:
        if "full" in str(error).lower():
            raise PrototypeSpillExhaustedError(
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
    max_detail_items: int = SQLITE_DETAIL_MAX_ITEMS
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
            "max_detail_items": self.max_detail_items,
            "batch_max_items": self.batch_max_items,
            "batch_max_decoded_bytes": self.batch_max_decoded_bytes,
        }
        for name, value in integer_fields.items():
            if type(value) is not int:
                raise PrototypeConfigurationError(f"{name} must be an integer")
        if self.page_bytes != SQLITE_PAGE_BYTES:
            raise PrototypeConfigurationError("page_bytes must remain 4096 for preparation-v1")
        if (
            self.cache_bytes < self.page_bytes
            or self.cache_bytes > SQLITE_CACHE_BYTES
            or self.cache_bytes % 1024
        ):
            raise PrototypeConfigurationError(
                "cache_bytes must be a whole number of KiB from 4 KiB through 8 MiB"
            )
        if self.mmap_bytes != 0:
            raise PrototypeConfigurationError("mmap_bytes must be zero for preparation-v1")
        if self.max_spill_bytes > SQLITE_SPILL_MAX_BYTES:
            raise PrototypeConfigurationError("max_spill_bytes cannot exceed 1 GiB")
        if self.max_spill_bytes % self.page_bytes:
            raise PrototypeConfigurationError("max_spill_bytes must be page aligned")
        if (
            self.control_reserve_bytes < self.page_bytes
            or self.control_reserve_bytes > SQLITE_CONTROL_RESERVE_BYTES
            or self.control_reserve_bytes % self.page_bytes
        ):
            raise PrototypeConfigurationError(
                "control_reserve_bytes must be page aligned from 4 KiB through 4 MiB"
            )
        if self.max_spill_bytes - self.control_reserve_bytes < _MINIMUM_SPILL_BYTES:
            raise PrototypeConfigurationError(
                "max_spill_bytes must leave at least 64 KiB after the control reserve"
            )
        item_limits = {
            "max_node_items": SQLITE_NODE_MAX_ITEMS,
            "max_edge_items": SQLITE_EDGE_MAX_ITEMS,
            "max_detail_items": SQLITE_DETAIL_MAX_ITEMS,
        }
        for name, maximum in item_limits.items():
            if not 1 <= getattr(self, name) <= maximum:
                raise PrototypeConfigurationError(
                    f"{name} must be positive and cannot exceed {maximum}"
                )
        if not 1 <= self.batch_max_items <= SQLITE_BATCH_MAX_ITEMS:
            raise PrototypeConfigurationError("batch_max_items must be from 1 through 256")
        if not self.page_bytes <= self.batch_max_decoded_bytes <= SQLITE_BATCH_MAX_DECODED_BYTES:
            raise PrototypeConfigurationError(
                "batch_max_decoded_bytes must be from 4 KiB through 4 MiB"
            )
        return self


@dataclass(frozen=True)
class PrototypePreparedTopology:
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
        self._owned_directory: Path | None = None
        self.database_path: Path | None = None
        self.lease_path: Path | None = None
        self.connection: sqlite3.Connection | None = None
        self.cleanup_outcome = "not_started"
        self.spill_peak_bytes = 0
        self.spill_items = 0
        self.observed_node_count = 0
        self.observed_edge_count = 0
        self.observed_detail_count = 0
        self._active_input_iterator: Iterator[Mapping[str, Any]] | None = None
        self._phase = "new"

    def __enter__(self) -> SQLitePreparationWorkspace:
        if self._phase != "new":
            raise RuntimeError("SQLite preparation workspace may be entered exactly once")
        self._phase = "acquiring"
        directory_created = False
        lease_initialized = False
        try:
            parent_candidate = (
                self.parent_directory
                if self.parent_directory is not None
                else Path(tempfile.gettempdir())
            )
            parent_candidate.mkdir(parents=True, exist_ok=True)
            parent = parent_candidate.resolve(strict=True)
            if not parent.is_dir():
                raise PrototypeCleanupRefusedError(
                    "SQLite preparation workspace parent is not a directory"
                )
            workspace_id = uuid4()
            directory = parent / f"{_WORKSPACE_PREFIX}{workspace_id}"
            self._owned_parent = parent
            self._owned_directory = directory
            self.database_path = directory / _DATABASE_NAME
            self.lease_path = directory / _LEASE_NAME
            directory.mkdir(mode=0o700, exist_ok=False)
            directory_created = True
            self._validated_owned_path()
            with self.lease_path.open("x", encoding="utf-8", errors="strict") as lease:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "token": self.token,
                        "workspace_id": str(workspace_id),
                    },
                    lease,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            lease_initialized = True
            connection = sqlite3.connect(self.database_path, factory=_BudgetConnection)
            connection.row_factory = sqlite3.Row
            self.connection = connection
            self._configure_connection()
            self._assert_pragmas()
            self._create_schema()
            self._assert_query_plans()
            self._flush_batch()
        except BaseException:
            self._phase = "poisoned"
            if directory_created:
                if lease_initialized:
                    self._close_and_remove(acquisition_failure=True)
                else:
                    self._remove_empty_acquisition_directory()
            else:
                self.cleanup_outcome = "not_created"
            raise
        self.cleanup_outcome = "pending"
        self._phase = "topology"
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
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
        return self._owned_directory is not None and os.path.lexists(self._owned_directory)

    def sqlite_pragmas(self) -> dict[str, int | str]:
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
        """Expose prototype query plans so tests can reject temp B-tree sorting."""
        connection = self._connection()
        statements = (
            (_NODE_SELECTION_SQL, (257,)),
            (_EDGE_SELECTION_SQL, (257,)),
            (_DETAIL_SELECTION_SQL, ()),
        )
        plans: list[str] = []
        for statement, parameters in statements:
            plans.extend(
                str(row[3])
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN {statement}", parameters
                ).fetchall()
            )
        return tuple(plans)

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
            raise PrototypeConfigurationError(
                "SQLite did not apply the exact preparation-v1 PRAGMA profile"
            )

    def _assert_query_plans(self) -> None:
        plans = self.retained_query_plans()
        prohibited = ("TEMP B-TREE", "MATERIALIZE")
        if any(marker in plan.upper() for marker in prohibited for plan in plans):
            raise PrototypeConfigurationError(
                "preparation-v1 query plan requires unbudgeted temporary storage"
            )

    @_poison_on_failure
    def prepare_topology(
        self,
        identity: WorkflowRunIdentity,
        topology_version: int,
        nodes: Iterable[Mapping[str, Any]],
        edges: Iterable[Mapping[str, Any]],
    ) -> PrototypePreparedTopology:
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
        self._phase = "detail"
        return prepared

    @_poison_on_failure
    def prepare_detail(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        topology: PrototypePreparedTopology,
        reporting_policy: str = "full",
    ) -> storage.PreparedWorkflowProgressDetail:
        if self._phase != "detail" or topology._workspace_token != self.token:
            raise RuntimeError("detail preparation requires this live topology workspace")
        self._check_cancellation()
        if reporting_policy not in {"full", "sampled", "terminal_only", "disabled"}:
            raise storage.WorkflowProgressStorageError("workflow reporting policy is unsupported")
        connection = self._connection()
        reasons = set(topology.truncation_reasons)
        node_kinds = dict(topology.node_kinds)
        detail_batch_items = 0
        detail_batch_bytes = 0
        for value in self._cancelable_values(records):
            self.observed_detail_count += 1
            self._check_item_limit(
                "detail",
                self.observed_detail_count,
                self.config.max_detail_items,
            )
            detail_keys = frozenset(value)
            expected_detail_keys = (
                storage._DETAIL_KEYS_V1
                if detail_keys == storage._DETAIL_KEYS_V1
                else storage._DETAIL_KEYS_V2
            )
            detail_value = storage._exact_mapping(
                value,
                expected_detail_keys,
                "node detail",
            )
            node_id = storage._bounded_identity_text(
                detail_value["node_id"],
                "node detail node_id",
                max_bytes=storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES,
            )
            if node_id is None:
                raise AssertionError("non-null node detail identity normalized to None")
            node_key = node_id.encode("utf-8")
            detail_batch_items, detail_batch_bytes = self._prepare_batch_for_potential_item(
                len(node_key) + storage.WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES,
                items=detail_batch_items,
                decoded_bytes=detail_batch_bytes,
            )
            self._insert_detail_identity(node_key)
            try:
                record = storage.prepare_workflow_progress_node_detail(
                    value,
                    identity=topology.identity,
                )
            except storage.WorkflowProgressStorageLimitError:
                reasons.add(storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
                item_bytes = len(node_key) + storage.WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES
            else:
                if record.node_id != node_id:
                    raise AssertionError("normalized node detail identity changed")
                if record.node_id in topology.node_ids:
                    decoded = json.loads(record.payload)
                    has_fanout = decoded["fanout"] is not None
                    if (node_kinds[record.node_id] == "map") != has_fanout:
                        raise storage.WorkflowProgressStorageError(
                            "node detail fanout does not match the retained topology kind"
                        )
                    if record.truncated:
                        reasons.add(
                            storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value
                        )
                    connection.execute(
                        "UPDATE detail SET state = ?, invocation_id = ?, payload = ?, "
                        "digest = ?, encoded_bytes = ?, decoded_bytes = ?, event_count = ?, "
                        "truncated = ? WHERE node_id = ?",
                        (
                            record.state,
                            record.invocation_id,
                            record.payload,
                            record.digest,
                            record.encoded_bytes,
                            record.decoded_bytes,
                            record.event_count,
                            int(record.truncated),
                            node_key,
                        ),
                    )
                item_bytes = len(node_key) + record.decoded_bytes
            detail_batch_items, detail_batch_bytes = self._record_batch_item(
                item_bytes,
                items=detail_batch_items,
                decoded_bytes=detail_batch_bytes,
            )
        if detail_batch_items:
            self._flush_batch()
        self._check_cancellation()
        if reporting_policy == "full":
            detail_count = int(connection.execute("SELECT COUNT(*) FROM detail").fetchone()[0])
            node_count = int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            if detail_count != node_count:
                raise storage.WorkflowProgressStorageError(
                    "full detail must contain one record per observed topology node"
                )
        else:
            reasons.add(storage.WorkflowProgressTruncationReason.REPORTING_POLICY.value)

        retained = self._select_retained_detail(topology, reasons)
        self._check_cancellation()
        self._phase = "prepared"
        return retained

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
            CREATE TABLE detail (
                node_id BLOB PRIMARY KEY,
                state TEXT,
                invocation_id TEXT,
                payload BLOB,
                digest TEXT,
                encoded_bytes INTEGER,
                decoded_bytes INTEGER,
                event_count INTEGER,
                truncated INTEGER,
                FOREIGN KEY (node_id) REFERENCES nodes(node_id)
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

    def _assemble_topology(
        self,
        *,
        identity: WorkflowRunIdentity,
        topology_version: int,
        normalized_nodes: list[dict[str, Any]],
        normalized_edges: list[dict[str, str]],
        reasons: set[str],
    ) -> PrototypePreparedTopology:
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
        return PrototypePreparedTopology(
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

    def _select_retained_detail(
        self,
        topology: PrototypePreparedTopology,
        reasons: set[str],
    ) -> storage.PreparedWorkflowProgressDetail:
        cursor = self._connection().execute(_DETAIL_SELECTION_SQL)
        retained: list[storage.PreparedWorkflowProgressNodeDetail] = []
        encoded_bytes = 0
        decoded_bytes = 0
        stop = False
        while not stop:
            self._check_cancellation()
            rows = cursor.fetchmany(self.config.batch_max_items)
            if not rows:
                break
            for row in rows:
                node_id = bytes(row["node_id"]).decode("utf-8")
                record = storage.PreparedWorkflowProgressNodeDetail(
                    node_id=node_id,
                    node_key=hashlib.sha256(bytes(row["node_id"])).hexdigest(),
                    state=str(row["state"]),
                    invocation_id=(
                        None if row["invocation_id"] is None else str(row["invocation_id"])
                    ),
                    payload=bytes(row["payload"]),
                    digest=str(row["digest"]),
                    encoded_bytes=int(row["encoded_bytes"]),
                    decoded_bytes=int(row["decoded_bytes"]),
                    event_count=int(row["event_count"]),
                    truncated=bool(row["truncated"]),
                )
                if len(retained) >= storage.WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS:
                    reasons.add(storage.WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value)
                    stop = True
                    break
                if (
                    encoded_bytes + record.encoded_bytes
                    > storage.WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES
                    or topology.encoded_bytes + encoded_bytes + record.encoded_bytes
                    > storage.WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES
                ):
                    reasons.add(storage.WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value)
                    stop = True
                    break
                if (
                    decoded_bytes + record.decoded_bytes
                    > storage.WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES
                    or topology.decoded_bytes + decoded_bytes + record.decoded_bytes
                    > storage.WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES
                ):
                    reasons.add(storage.WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value)
                    stop = True
                    break
                retained.append(record)
                encoded_bytes += record.encoded_bytes
                decoded_bytes += record.decoded_bytes

        decoded_records: dict[str, dict[str, Any]] = {}
        event_entries: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
        for record in retained:
            decoded = json.loads(record.payload)
            decoded_records[record.node_id] = decoded
            for occurrence, event in enumerate(decoded["recent_events"]):
                event_entries.append(
                    (
                        storage._event_sort_key(
                            event,
                            node_id=record.node_id,
                            occurrence=occurrence,
                        ),
                        record.node_id,
                        event,
                    )
                )
        selected_events = sorted(event_entries, key=lambda item: item[0])[
            -storage.WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS :
        ]
        events_by_node: dict[str, list[dict[str, Any]]] = {}
        for _, node_id, event in selected_events:
            events_by_node.setdefault(node_id, []).append(event)

        event_bounded: list[storage.PreparedWorkflowProgressNodeDetail] = []
        for record in retained:
            retained_events = sorted(
                events_by_node.get(record.node_id, []),
                key=storage._event_sort_key,
            )
            if len(retained_events) != record.event_count:
                decoded = decoded_records[record.node_id]
                decoded["recent_events"] = retained_events
                record = storage._prepared_node_detail(
                    decoded,
                    invocation_id=record.invocation_id,
                    truncated=True,
                )
                reasons.add(storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value)
            event_bounded.append(record)
        encoded_bytes = sum(record.encoded_bytes for record in event_bounded)
        decoded_bytes = sum(record.decoded_bytes for record in event_bounded)
        return storage.PreparedWorkflowProgressDetail(
            records=tuple(event_bounded),
            observed_count=topology.observed_node_count,
            encoded_bytes=encoded_bytes,
            decoded_bytes=decoded_bytes,
            truncation_reasons=tuple(sorted(reasons)),
        )

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

    def _insert_detail_identity(self, node_key: bytes) -> None:
        try:
            self._connection().execute(
                "INSERT INTO detail(node_id) VALUES (?)",
                (node_key,),
            )
        except sqlite3.IntegrityError as error:
            message = str(error).lower()
            reason = (
                "node detail contains a duplicate node_id"
                if "unique" in message
                else "node detail references an unknown topology node_id"
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
            raise PrototypeSpillExhaustedError(
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
        directory = self._owned_directory
        if directory is None or not os.path.lexists(directory):
            raise PrototypeWorkspaceIntegrityError("SQLite preparation workspace is missing")
        try:
            directory = self._validated_owned_path()
        except (OSError, PrototypeCleanupRefusedError) as error:
            raise PrototypeWorkspaceIntegrityError(
                "SQLite preparation workspace path is invalid"
            ) from error
        expected = {_DATABASE_NAME, _LEASE_NAME}
        total_bytes = 0
        entries = list(directory.iterdir())
        if {path.name for path in entries} != expected:
            raise PrototypeWorkspaceIntegrityError(
                "unexpected or missing SQLite preparation workspace entry"
            )
        for path in entries:
            if path.name not in expected or path.is_symlink() or not path.is_file():
                raise PrototypeWorkspaceIntegrityError(
                    f"unexpected SQLite preparation workspace entry: {path.name}"
                )
            total_bytes += path.stat().st_size
        if self.lease_path is None or not self.lease_path.exists():
            raise PrototypeWorkspaceIntegrityError("preparation owner lease is missing")
        if self.lease_path.stat().st_size > self.config.control_reserve_bytes:
            raise PrototypeSpillExhaustedError(
                "SQLite preparation control reserve byte budget exhausted"
            )
        self.spill_peak_bytes = max(self.spill_peak_bytes, total_bytes)
        if total_bytes > self.config.max_spill_bytes:
            raise PrototypeSpillExhaustedError("SQLite preparation spill byte budget exhausted")

    @staticmethod
    def _check_item_limit(kind: str, count: int, maximum: int) -> None:
        if count > maximum:
            raise PrototypeSpillExhaustedError(
                f"SQLite preparation {kind} item budget exhausted at {maximum}"
            )

    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("SQLite preparation workspace is not open")
        return self.connection

    def _close_and_remove(self, *, acquisition_failure: bool = False) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
        directory = self._owned_directory
        if directory is None:
            self.cleanup_outcome = "not_created"
            return
        if not os.path.lexists(directory):
            self.cleanup_outcome = "refused"
            raise PrototypeCleanupRefusedError("workspace path disappeared before owned cleanup")
        try:
            self._validate_owned_directory(acquisition_failure=acquisition_failure)
            for name in (_DATABASE_NAME, _LEASE_NAME):
                path = directory / name
                if path.exists():
                    path.unlink()
            directory.rmdir()
        except (OSError, PrototypeCleanupRefusedError):
            self.cleanup_outcome = "refused"
            raise
        if os.path.lexists(directory):
            self.cleanup_outcome = "refused"
            raise PrototypeCleanupRefusedError("workspace path reappeared during cleanup")
        self.cleanup_outcome = "removed"

    def _remove_empty_acquisition_directory(self) -> None:
        """Remove only the exact empty directory created before a lease was proven."""
        try:
            directory = self._validated_owned_path()
            directory.rmdir()
        except (OSError, PrototypeCleanupRefusedError) as error:
            self.cleanup_outcome = "refused"
            raise PrototypeCleanupRefusedError(
                "unleased acquisition workspace is not safely removable"
            ) from error
        if os.path.lexists(directory):
            self.cleanup_outcome = "refused"
            raise PrototypeCleanupRefusedError(
                "unleased acquisition workspace reappeared during cleanup"
            )
        self.cleanup_outcome = "removed"

    def _validated_owned_path(self) -> Path:
        directory = self._owned_directory
        parent = self._owned_parent
        if directory is None or parent is None:
            raise PrototypeCleanupRefusedError("workspace ownership path is incomplete")
        if not os.path.lexists(directory):
            raise PrototypeCleanupRefusedError("workspace ownership path is missing")
        if directory.is_symlink() or not directory.is_dir():
            raise PrototypeCleanupRefusedError("workspace path was replaced or redirected")
        if directory.resolve(strict=True) != directory:
            raise PrototypeCleanupRefusedError("workspace path was replaced or redirected")
        if directory.parent != parent or parent.resolve(strict=True) != parent:
            raise PrototypeCleanupRefusedError("workspace escaped its resolved owner parent")
        if not directory.name.startswith(_WORKSPACE_PREFIX):
            raise PrototypeCleanupRefusedError("workspace name does not use the owned prefix")
        workspace_id = directory.name.removeprefix(_WORKSPACE_PREFIX)
        try:
            if str(UUID(workspace_id)) != workspace_id:
                raise ValueError
        except ValueError as error:
            raise PrototypeCleanupRefusedError("workspace name has no canonical UUID") from error
        return directory

    def _validate_owned_directory(self, *, acquisition_failure: bool) -> None:
        directory = self._validated_owned_path()
        workspace_id = directory.name.removeprefix(_WORKSPACE_PREFIX)
        entries = list(directory.iterdir())
        expected = {_DATABASE_NAME, _LEASE_NAME}
        invalid_entry = any(
            entry.name not in expected or entry.is_symlink() or not entry.is_file()
            for entry in entries
        )
        if invalid_entry or (
            not acquisition_failure and {entry.name for entry in entries} != expected
        ):
            raise PrototypeCleanupRefusedError("workspace contains an unowned entry")
        lease = directory / _LEASE_NAME
        try:
            if lease.stat().st_size > self.config.control_reserve_bytes:
                raise PrototypeCleanupRefusedError(
                    "workspace owner lease exceeds the control reserve"
                )
            value = json.loads(lease.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise PrototypeCleanupRefusedError("workspace owner lease is unreadable") from None
        if not isinstance(value, dict) or value.get("token") != self.token:
            raise PrototypeCleanupRefusedError("workspace owner lease token does not match")
        if value.get("workspace_id") != workspace_id:
            raise PrototypeCleanupRefusedError("workspace owner lease UUID does not match")


__all__ = [
    "PrototypeConfigurationError",
    "PrototypeCleanupRefusedError",
    "PrototypePreparedTopology",
    "PrototypeSpillExhaustedError",
    "PrototypeWorkspaceIntegrityError",
    "SQLITE_BATCH_MAX_DECODED_BYTES",
    "SQLITE_BATCH_MAX_ITEMS",
    "SQLITE_CACHE_BYTES",
    "SQLITE_CONTROL_RESERVE_BYTES",
    "SQLITE_DETAIL_MAX_ITEMS",
    "SQLITE_EDGE_MAX_ITEMS",
    "SQLITE_MMAP_BYTES",
    "SQLITE_NODE_MAX_ITEMS",
    "SQLITE_PAGE_BYTES",
    "SQLITE_SPILL_MAX_BYTES",
    "SQLitePreparationConfig",
    "SQLitePreparationWorkspace",
    "canonical_topology_evidence",
]
