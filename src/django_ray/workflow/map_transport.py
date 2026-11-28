"""Bounded, chunked, balanced list-preserving map result transport (#91, #829).

.. warning::
   **Experimental reference module**: this Ray-free state machine is not yet
   wired into the active ``django-ray`` worker runtime.

When a workflow ``Map`` step does not use ``.reduce(...)`` or ``.fold(...)``,
it produces an ordered ``list`` output matching the input item indices
``0..N-1``. Holding ``N`` leaf ``ObjectRef`` handles or materializing all ``N``
leaf outputs inside the workflow coordinator creates ``O(N)`` coordinator
memory growth and Ray Client gRPC pressure.

This module bounds list-preserving map aggregation via:
- Contiguous index-ordered chunking of leaf completions with a bounded
  out-of-order completion buffer.
- Balanced binary reference trees, compacting batches of up to
  ``branching_factor`` handles within ``max_coordinator_live_refs``.
- Single-reference downstream step handoff (``DownstreamMapListHandoff``) so
  intermediate workflow steps receive one root handle without coordinator
  list materialization.
- Deterministic fail-fast cleanup of pending leaf, chunk, and tree handles on
  item failure or task cancellation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Never
from uuid import uuid4

DEFAULT_MAP_CHUNK_SIZE = 64
MAX_MAP_CHUNK_SIZE = 4096
DEFAULT_TREE_BRANCHING_FACTOR = 16
MAX_TREE_BRANCHING_FACTOR = 256
DEFAULT_MAX_COORDINATOR_LIVE_REFS = 32
MAX_COORDINATOR_LIVE_REFS_CEILING = 512
DEFAULT_MAX_OUT_OF_ORDER_COMPLETIONS = 128
MAX_OUT_OF_ORDER_COMPLETIONS_CEILING = 4096
DEFAULT_MAX_CHUNK_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_LIST_BYTES = 64 * 1024 * 1024
STORE_RELEASE_HISTORY_MAX_ITEMS = 128


class MapResultTransportError(RuntimeError):
    """Base error for list-preserving map result transport."""


class MapResultTransportConfigurationError(MapResultTransportError, ValueError):
    """Raised when ``BoundedMapListTransportConfig`` is invalid."""


class MapResultTransportLimitError(MapResultTransportError):
    """Raised when map output exceeds chunk, buffer, or total byte ceilings."""


class MapResultTransportAbortedError(MapResultTransportError):
    """Raised when map aggregation is aborted due to item failure or cancellation."""


@dataclass(frozen=True)
class BoundedMapListTransportConfig:
    """Bounds for list-preserving workflow map result transport."""

    chunk_size: int = DEFAULT_MAP_CHUNK_SIZE
    branching_factor: int = DEFAULT_TREE_BRANCHING_FACTOR
    max_coordinator_live_refs: int = DEFAULT_MAX_COORDINATOR_LIVE_REFS
    max_out_of_order_completions: int = DEFAULT_MAX_OUT_OF_ORDER_COMPLETIONS
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_LIST_BYTES

    def validated(self) -> BoundedMapListTransportConfig:
        if type(self.chunk_size) is not int or not 1 <= self.chunk_size <= MAX_MAP_CHUNK_SIZE:
            raise MapResultTransportConfigurationError(
                f"chunk_size must be an int between 1 and {MAX_MAP_CHUNK_SIZE}"
            )
        if (
            type(self.branching_factor) is not int
            or not 2 <= self.branching_factor <= MAX_TREE_BRANCHING_FACTOR
        ):
            raise MapResultTransportConfigurationError(
                f"branching_factor must be an int between 2 and {MAX_TREE_BRANCHING_FACTOR}"
            )
        if (
            type(self.max_coordinator_live_refs) is not int
            or not 2 <= self.max_coordinator_live_refs <= MAX_COORDINATOR_LIVE_REFS_CEILING
        ):
            raise MapResultTransportConfigurationError(
                "max_coordinator_live_refs must be an int between 2 and "
                f"{MAX_COORDINATOR_LIVE_REFS_CEILING}"
            )
        if self.branching_factor > self.max_coordinator_live_refs:
            raise MapResultTransportConfigurationError(
                "branching_factor must not exceed max_coordinator_live_refs"
            )
        if (
            type(self.max_out_of_order_completions) is not int
            or not 1 <= self.max_out_of_order_completions <= MAX_OUT_OF_ORDER_COMPLETIONS_CEILING
        ):
            raise MapResultTransportConfigurationError(
                "max_out_of_order_completions must be an int between 1 and "
                f"{MAX_OUT_OF_ORDER_COMPLETIONS_CEILING}"
            )
        if type(self.max_chunk_bytes) is not int or self.max_chunk_bytes < 64:
            raise MapResultTransportConfigurationError("max_chunk_bytes must be at least 64 bytes")
        if type(self.max_total_bytes) is not int or self.max_total_bytes < self.max_chunk_bytes:
            raise MapResultTransportConfigurationError(
                "max_total_bytes must be an int >= max_chunk_bytes"
            )
        return self


@dataclass(frozen=True)
class MapChunkHandle:
    """Opaque root handle; ``tree_level`` is its height, with leaf chunks at zero."""

    handle_id: str
    start_index: int
    end_index: int
    item_count: int
    byte_size: int
    tree_level: int


@dataclass(frozen=True)
class DownstreamMapListHandoff:
    """Transferred root ownership and its actual height in ``tree_depth``."""

    root_handle: MapChunkHandle | None
    total_items: int
    total_bytes: int
    tree_depth: int
    chunk_count: int
    peak_coordinator_live_refs: int


@dataclass(frozen=True)
class MapTransportDiagnostics:
    """Bounded operational diagnostics for list-preserving map transport."""

    total_items_expected: int
    completed_items: int
    chunks_emitted: int
    tree_merges_performed: int
    current_live_refs: int
    peak_live_refs: int
    out_of_order_buffered: int
    peak_out_of_order_buffered: int
    total_bytes_accumulated: int
    released_handles_count: int
    aborted: bool
    abort_reason: str | None
    pending_leaf_releases: int = 0


def _estimate_item_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return len(repr(value).encode("utf-8", errors="replace"))


@dataclass(frozen=True, slots=True)
class _ChunkLeaf:
    items: tuple[Any, ...]
    start_index: int
    end_index: int
    byte_size: int

    @property
    def height(self) -> int:
        return 0

    @property
    def item_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class _ChunkBranch:
    left: _ChunkLeaf | _ChunkBranch
    right: _ChunkLeaf | _ChunkBranch
    height: int
    item_count: int
    start_index: int
    end_index: int
    byte_size: int


_ChunkNode = _ChunkLeaf | _ChunkBranch


class InMemoryChunkObjectStore:
    """Balanced reference trees with separately owned outward root handles.

    ``merge_chunks`` retains its children. ``compact_chunks`` atomically
    replaces them with the parent: failure leaves every child valid. Both
    retain leaf payloads by reference, without flattening during compaction.
    The in-process store retains the output payload, not a hard RSS bound.
    Root-map publication copies outward root metadata: the aggregator bounds
    that work by its handle cap; unbounded direct non-consuming store use does
    not have the same bound. Release history is a 128-ID tail plus a scalar
    total. Work counters record actual node visits, construction and explicit
    payload traversal, including work before a failed publication.
    """

    def __init__(self) -> None:
        self._counter = 0
        self._namespace = uuid4().hex
        self._objects: dict[str, _ChunkNode] = {}
        self._lock = RLock()
        self.released_handle_ids: list[str] = []
        self.released_handle_count = 0
        self.peak_live_object_count = 0
        self.join_node_visits = 0
        self.branch_nodes_created = 0
        self.payload_items_stored = 0
        self.payload_items_read = 0

    def _branch(self, left: _ChunkNode, right: _ChunkNode) -> _ChunkBranch:
        self.branch_nodes_created += 1
        return _ChunkBranch(
            left,
            right,
            max(left.height, right.height) + 1,
            left.item_count + right.item_count,
            left.start_index,
            right.end_index,
            left.byte_size + right.byte_size,
        )

    def _balance(self, left: _ChunkNode, right: _ChunkNode) -> _ChunkBranch:
        if left.height > right.height + 1:
            assert isinstance(left, _ChunkBranch)
            if left.left.height >= left.right.height:
                return self._branch(left.left, self._branch(left.right, right))
            middle = left.right
            assert isinstance(middle, _ChunkBranch)
            return self._branch(
                self._branch(left.left, middle.left), self._branch(middle.right, right)
            )
        if right.height > left.height + 1:
            assert isinstance(right, _ChunkBranch)
            if right.right.height >= right.left.height:
                return self._branch(self._branch(left, right.left), right.right)
            middle = right.left
            assert isinstance(middle, _ChunkBranch)
            return self._branch(
                self._branch(left, middle.left), self._branch(middle.right, right.right)
            )
        return self._branch(left, right)

    def _join(self, left: _ChunkNode, right: _ChunkNode) -> _ChunkNode:
        self.join_node_visits += 1
        if left.height > right.height + 1:
            assert isinstance(left, _ChunkBranch)
            return self._balance(left.left, self._join(left.right, right))
        if right.height > left.height + 1:
            assert isinstance(right, _ChunkBranch)
            return self._balance(self._join(left, right.left), right.right)
        return self._branch(left, right)

    def _root(self, handle: MapChunkHandle) -> _ChunkNode:
        node = self._objects.get(handle.handle_id)
        if node is None:
            raise MapResultTransportError(f"Chunk handle {handle.handle_id!r} is unavailable")
        if (
            handle.start_index,
            handle.end_index,
            handle.item_count,
            handle.byte_size,
            handle.tree_level,
        ) != (node.start_index, node.end_index, node.item_count, node.byte_size, node.height):
            raise MapResultTransportError("Chunk handle metadata does not match its stored root")
        return node

    def _publish_root(
        self,
        node: _ChunkNode,
        *,
        consumed: tuple[str, ...] = (),
        prefix: str,
    ) -> MapChunkHandle:
        """Prepare every allocation before swapping the outward ownership map."""
        next_counter = self._counter + 1
        handle_id = f"{prefix}-{self._namespace}-{next_counter:06d}"
        handle = MapChunkHandle(
            handle_id,
            node.start_index,
            node.end_index,
            node.item_count,
            node.byte_size,
            node.height,
        )
        next_objects = self._objects.copy()
        for child_id in consumed:
            del next_objects[child_id]
        next_objects[handle_id] = node
        history = [*self.released_handle_ids, *consumed][-STORE_RELEASE_HISTORY_MAX_ITEMS:]
        released_count = self.released_handle_count + len(consumed)
        peak_count = max(self.peak_live_object_count, len(next_objects))
        # No provider or observer code runs after this exception-atomic root swap.
        self._objects = next_objects
        self._counter = next_counter
        self.released_handle_ids = history
        self.released_handle_count = released_count
        self.peak_live_object_count = peak_count
        return handle

    def put_chunk(
        self,
        items: list[Any],
        *,
        start_index: int,
        end_index: int,
        byte_size: int,
    ) -> MapChunkHandle:
        with self._lock:
            if (
                not items
                or type(start_index) is not int
                or type(end_index) is not int
                or type(byte_size) is not int
                or start_index < 0
                or end_index - start_index + 1 != len(items)
                or byte_size < 0
            ):
                raise MapResultTransportError("Chunk range and payload metadata must agree")
            leaf = _ChunkLeaf(tuple(items), start_index, end_index, byte_size)
            handle = self._publish_root(leaf, prefix="map-chunk")
            self.payload_items_stored += len(items)
            return handle

    def merge_chunks(self, children: list[MapChunkHandle]) -> MapChunkHandle:
        return self._merge(children, consume=False)

    def compact_chunks(self, children: list[MapChunkHandle]) -> MapChunkHandle:
        """Consume child roots only when a balanced parent is ready to return.

        Store adapters must implement this operation explicitly. Subclasses
        replacing payload storage must also replace this consuming operation;
        overriding non-consuming ``merge_chunks`` alone is insufficient.
        """
        return self._merge(children, consume=True)

    def _merge(self, children: list[MapChunkHandle], *, consume: bool) -> MapChunkHandle:
        with self._lock:
            if not children:
                raise ValueError("merge_chunks requires at least one child handle")
            identifiers = tuple(child.handle_id for child in children)
            if len(set(identifiers)) != len(identifiers):
                raise MapResultTransportError("Cannot merge duplicate child handles")
            roots = [self._root(child) for child in children]
            for left, right in zip(roots, roots[1:], strict=False):
                if left.end_index + 1 != right.start_index:
                    raise MapResultTransportError("Merged chunks must be contiguous and ordered")
            node = roots[0]
            for right in roots[1:]:
                node = self._join(node, right)
            return self._publish_root(
                node,
                consumed=identifiers if consume else (),
                prefix="map-tree",
            )

    def get_items(self, handle: MapChunkHandle) -> list[Any]:
        with self._lock:
            pending = [self._root(handle)]
            result: list[Any] = []
            while pending:
                node = pending.pop()
                if isinstance(node, _ChunkLeaf):
                    result.extend(node.items)
                    self.payload_items_read += len(node.items)
                else:
                    pending.extend((node.right, node.left))
            return result

    def release(self, handle: MapChunkHandle) -> None:
        with self._lock:
            if handle.handle_id not in self._objects:
                return
            self._root(handle)
            history = [*self.released_handle_ids, handle.handle_id][
                -STORE_RELEASE_HISTORY_MAX_ITEMS:
            ]
            released_count = self.released_handle_count + 1
            del self._objects[handle.handle_id]
            self.released_handle_ids = history
            self.released_handle_count = released_count

    @property
    def live_object_count(self) -> int:
        with self._lock:
            return len(self._objects)


class BoundedMapListAggregator:
    """Coordinator-bounded ordered aggregator for list-preserving workflow maps."""

    def __init__(
        self,
        total_items: int,
        config: BoundedMapListTransportConfig | None = None,
        *,
        store: InMemoryChunkObjectStore | None = None,
        on_release_leaf_ref: Callable[[int], None] | None = None,
    ) -> None:
        if type(total_items) is not int or total_items < 0:
            raise MapResultTransportConfigurationError("total_items must be a non-negative int")
        self.total_items = total_items
        self.config = (config or BoundedMapListTransportConfig()).validated()
        self.store = InMemoryChunkObjectStore() if store is None else store
        if not callable(getattr(self.store, "compact_chunks", None)):
            raise MapResultTransportConfigurationError(
                "The map object store must implement consuming compact_chunks"
            )
        self._on_release_leaf_ref = on_release_leaf_ref

        self._next_expected_index = 0
        self._out_of_order: dict[int, tuple[Any, int]] = {}
        self._failed_leaf_releases: set[int] = set()
        self._pending_contiguous_leaf: int | None = None

        self._current_chunk_items: list[Any] = []
        self._current_chunk_start_index: int = 0
        self._current_chunk_bytes: int = 0

        self._live_handles: list[MapChunkHandle] = []
        self._completed_items = 0
        self._chunks_emitted = 0
        self._tree_merges = 0
        self._peak_live_refs = 0
        self._peak_out_of_order = 0
        self._total_bytes = 0
        self._released_handles_count = 0
        self._aborted = False
        self._abort_reason: str | None = None
        self._finalized = False

    def _ensure_active(self) -> None:
        if self._aborted:
            raise MapResultTransportAbortedError(
                f"Map result aggregation was aborted: {self._abort_reason or 'cancelled'}"
            )
        if self._finalized:
            raise MapResultTransportError("Map result aggregation is already finalized")

    def record_leaf_completion(
        self,
        index: int,
        value: Any,
        *,
        byte_size: int | None = None,
    ) -> None:
        """Record completion of leaf ``index`` while maintaining bounded state."""
        self._ensure_active()
        if type(index) is not int or not 0 <= index < self.total_items:
            raise MapResultTransportError(
                f"Leaf index {index!r} is out of range for total_items={self.total_items}"
            )
        if index < self._next_expected_index or index in self._out_of_order:
            raise MapResultTransportError(f"Duplicate completion for leaf index {index}")

        if byte_size is None:
            resolved_bytes = _estimate_item_bytes(value)
        else:
            if type(byte_size) is not int or byte_size < 0:
                raise MapResultTransportError("byte_size must be non-negative (expected int)")
            resolved_bytes = byte_size
        if resolved_bytes > self.config.max_chunk_bytes:
            self._fail(
                MapResultTransportLimitError(
                    f"Leaf {index} size ({resolved_bytes} B) exceeds max_chunk_bytes "
                    f"({self.config.max_chunk_bytes} B)"
                )
            )
        if self._total_bytes + resolved_bytes > self.config.max_total_bytes:
            self._fail(
                MapResultTransportLimitError(
                    f"Map list output exceeds max_total_bytes ({self.config.max_total_bytes} B)"
                )
            )

        if index != self._next_expected_index:
            if len(self._out_of_order) >= self.config.max_out_of_order_completions:
                self._fail(
                    MapResultTransportLimitError(
                        "Out-of-order leaf completions exceeded "
                        f"{self.config.max_out_of_order_completions}"
                    )
                )
            self._completed_items += 1
            self._total_bytes += resolved_bytes
            self._out_of_order[index] = (value, resolved_bytes)
            self._peak_out_of_order = max(self._peak_out_of_order, len(self._out_of_order))
            return

        self._completed_items += 1
        self._total_bytes += resolved_bytes
        try:
            self._append_contiguous_leaf(index, value, resolved_bytes)
            while self._next_expected_index in self._out_of_order:
                next_idx = self._next_expected_index
                buffered_val, buffered_bytes = self._out_of_order.pop(next_idx)
                self._append_contiguous_leaf(next_idx, buffered_val, buffered_bytes)
        except BaseException as error:
            self._fail(error)

    def _release_leaf(self, index: int) -> None:
        if self._pending_contiguous_leaf == index:
            self._pending_contiguous_leaf = None
        if self._on_release_leaf_ref is None:
            return
        try:
            self._on_release_leaf_ref(index)
        except BaseException:
            self._failed_leaf_releases.add(index)
            raise
        self._failed_leaf_releases.discard(index)

    def _append_contiguous_leaf(self, index: int, value: Any, byte_size: int) -> None:
        self._pending_contiguous_leaf = index
        if not self._current_chunk_items:
            self._current_chunk_start_index = index

        if (
            self._current_chunk_items
            and self._current_chunk_bytes + byte_size > self.config.max_chunk_bytes
        ):
            self._flush_current_chunk(end_index=index - 1)
            self._current_chunk_start_index = index

        self._current_chunk_items.append(value)
        self._current_chunk_bytes += byte_size
        self._next_expected_index = index + 1
        self._release_leaf(index)

        if len(self._current_chunk_items) >= self.config.chunk_size:
            self._flush_current_chunk(end_index=index)

    def _flush_current_chunk(self, *, end_index: int) -> None:
        if not self._current_chunk_items:
            return
        handle = self.store.put_chunk(
            self._current_chunk_items,
            start_index=self._current_chunk_start_index,
            end_index=end_index,
            byte_size=self._current_chunk_bytes,
        )
        self._current_chunk_items = []
        self._current_chunk_bytes = 0
        self._chunks_emitted += 1
        self._live_handles.append(handle)
        self._peak_live_refs = max(self._peak_live_refs, len(self._live_handles))

        while len(self._live_handles) >= self.config.max_coordinator_live_refs:
            self._compact_live_handles()

    def _compact_live_handles(self) -> None:
        batch_len = min(len(self._live_handles), self.config.branching_factor)
        if batch_len < 2:
            return
        # The store balances references while atomically consuming the inputs.
        batch = self._live_handles[:batch_len]
        next_handles = [batch[0], *self._live_handles[batch_len:]]
        merged = self.store.compact_chunks(batch)
        next_handles[0] = merged
        self._live_handles = next_handles
        self._released_handles_count += batch_len
        self._tree_merges += 1
        self._peak_live_refs = max(self._peak_live_refs, len(self._live_handles))

    def _seal_all_chunks(self) -> None:
        self._ensure_active()
        if self._completed_items != self.total_items or self._out_of_order:
            raise MapResultTransportError(
                f"Cannot finalize map result: {self._completed_items}/{self.total_items} "
                "items completed"
            )
        if self._current_chunk_items:
            try:
                self._flush_current_chunk(end_index=self.total_items - 1)
            except BaseException as error:
                self._fail(error)

    def finalize_downstream_handoff(self) -> DownstreamMapListHandoff:
        """Collapse all chunks into a single root handle for downstream steps."""
        self._seal_all_chunks()
        try:
            while len(self._live_handles) > 1:
                self._compact_live_handles()
            root = self._live_handles[0] if self._live_handles else None
            handoff = DownstreamMapListHandoff(
                root_handle=root,
                total_items=self.total_items,
                total_bytes=self._total_bytes,
                tree_depth=root.tree_level if root is not None else 0,
                chunk_count=self._chunks_emitted,
                peak_coordinator_live_refs=self._peak_live_refs,
            )
        except BaseException as error:
            self._fail(error)
        self._live_handles.clear()
        self._finalized = True
        return handoff

    def materialize_terminal_list(self) -> list[Any]:
        """Stream chunks in index order for a top-level terminal workflow return."""
        self._seal_all_chunks()
        result: list[Any] = []
        try:
            for handle in list(self._live_handles):
                result.extend(self.store.get_items(handle))
                self.store.release(handle)
                self._live_handles.remove(handle)
                self._released_handles_count += 1
        except BaseException as error:
            self._fail(error)
        self._finalized = True
        return result

    def abort(self, reason: str = "cancelled") -> None:
        """Attempt owned cleanup; failed releases remain available for retry."""
        if self._finalized:
            return
        self._cleanup(reason, retry_failed_leaves=True)

    def _fail(self, error: BaseException) -> Never:
        try:
            self._cleanup(type(error).__name__, retry_failed_leaves=False)
        except BaseException as cleanup_error:
            error.add_note(
                f"Map cleanup also failed ({type(cleanup_error).__name__}); retry abort()"
            )
        raise error

    def _cleanup(self, reason: str, *, retry_failed_leaves: bool) -> None:
        self._aborted = True
        if self._abort_reason is None:
            self._abort_reason = reason
        self._current_chunk_items.clear()
        self._current_chunk_bytes = 0
        indices = set(self._out_of_order)
        if self._pending_contiguous_leaf is not None:
            indices.add(self._pending_contiguous_leaf)
            self._pending_contiguous_leaf = None
        if retry_failed_leaves:
            indices.update(self._failed_leaf_releases)
        self._out_of_order.clear()
        first_error: BaseException | None = None
        for idx in sorted(indices):
            try:
                self._release_leaf(idx)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        for handle in list(self._live_handles):
            try:
                self.store.release(handle)
            except BaseException as error:
                if first_error is None:
                    first_error = error
            else:
                self._live_handles.remove(handle)
                self._released_handles_count += 1
        if first_error is not None:
            raise first_error

    def diagnostics(self) -> MapTransportDiagnostics:
        return MapTransportDiagnostics(
            total_items_expected=self.total_items,
            completed_items=self._completed_items,
            chunks_emitted=self._chunks_emitted,
            tree_merges_performed=self._tree_merges,
            current_live_refs=len(self._live_handles),
            peak_live_refs=self._peak_live_refs,
            out_of_order_buffered=len(self._out_of_order),
            peak_out_of_order_buffered=self._peak_out_of_order,
            total_bytes_accumulated=self._total_bytes,
            released_handles_count=self._released_handles_count,
            aborted=self._aborted,
            abort_reason=self._abort_reason,
            pending_leaf_releases=len(self._failed_leaf_releases),
        )


__all__ = [
    "DEFAULT_MAP_CHUNK_SIZE",
    "DEFAULT_MAX_CHUNK_BYTES",
    "DEFAULT_MAX_COORDINATOR_LIVE_REFS",
    "DEFAULT_MAX_OUT_OF_ORDER_COMPLETIONS",
    "DEFAULT_MAX_TOTAL_LIST_BYTES",
    "DEFAULT_TREE_BRANCHING_FACTOR",
    "STORE_RELEASE_HISTORY_MAX_ITEMS",
    "BoundedMapListAggregator",
    "BoundedMapListTransportConfig",
    "DownstreamMapListHandoff",
    "InMemoryChunkObjectStore",
    "MapChunkHandle",
    "MapResultTransportAbortedError",
    "MapResultTransportConfigurationError",
    "MapResultTransportError",
    "MapResultTransportLimitError",
    "MapTransportDiagnostics",
]
