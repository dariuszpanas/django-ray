"""Bounded, chunked, and hierarchical list-preserving map result transport (#91).

When a workflow ``Map`` step does not use ``.reduce(...)`` or ``.fold(...)``,
it produces an ordered ``list`` output matching the input item indices
``0..N-1``. Holding ``N`` leaf ``ObjectRef`` handles or materializing all ``N``
leaf outputs inside the workflow coordinator creates ``O(N)`` coordinator
memory growth and Ray Client gRPC pressure.

This module bounds list-preserving map aggregation via:
- Contiguous index-ordered chunking of leaf completions with a bounded
  out-of-order completion buffer.
- Hierarchical k-ary tree merging of chunk handles whenever live coordinator
  handles approach ``max_coordinator_live_refs``.
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
from typing import Any

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
    """Opaque handle representing a remotely stored chunk or merged subtree."""

    handle_id: str
    start_index: int
    end_index: int
    item_count: int
    byte_size: int
    tree_level: int


@dataclass(frozen=True)
class DownstreamMapListHandoff:
    """Single-reference descriptor passed to downstream workflow steps."""

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


def _estimate_item_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return len(repr(value).encode("utf-8", errors="replace"))


class InMemoryChunkObjectStore:
    """Reference object store backing ``MapChunkHandle`` instances."""

    def __init__(self) -> None:
        self._counter = 0
        self._objects: dict[str, tuple[Any, ...]] = {}
        self.released_handle_ids: list[str] = []

    def put_chunk(
        self,
        items: list[Any],
        *,
        start_index: int,
        end_index: int,
        byte_size: int,
    ) -> MapChunkHandle:
        self._counter += 1
        handle_id = f"map-chunk-{self._counter:06d}"
        self._objects[handle_id] = tuple(items)
        return MapChunkHandle(
            handle_id=handle_id,
            start_index=start_index,
            end_index=end_index,
            item_count=len(items),
            byte_size=byte_size,
            tree_level=0,
        )

    def merge_chunks(self, children: list[MapChunkHandle]) -> MapChunkHandle:
        if not children:
            raise ValueError("merge_chunks requires at least one child handle")
        self._counter += 1
        handle_id = f"map-tree-{self._counter:06d}"
        merged_items: list[Any] = []
        total_bytes = 0
        max_level = 0
        for child in children:
            payload = self._objects.get(child.handle_id)
            if payload is None:
                raise MapResultTransportError(f"Chunk handle {child.handle_id!r} was already freed")
            merged_items.extend(payload)
            total_bytes += child.byte_size
            max_level = max(max_level, child.tree_level)
        self._objects[handle_id] = tuple(merged_items)
        return MapChunkHandle(
            handle_id=handle_id,
            start_index=children[0].start_index,
            end_index=children[-1].end_index,
            item_count=len(merged_items),
            byte_size=total_bytes,
            tree_level=max_level + 1,
        )

    def get_items(self, handle: MapChunkHandle) -> list[Any]:
        payload = self._objects.get(handle.handle_id)
        if payload is None:
            raise MapResultTransportError(f"Chunk handle {handle.handle_id!r} is unavailable")
        return list(payload)

    def release(self, handle: MapChunkHandle) -> None:
        if handle.handle_id in self._objects:
            del self._objects[handle.handle_id]
        self.released_handle_ids.append(handle.handle_id)

    @property
    def live_object_count(self) -> int:
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
        self.store = store or InMemoryChunkObjectStore()
        self._on_release_leaf_ref = on_release_leaf_ref

        self._next_expected_index = 0
        self._out_of_order: dict[int, tuple[Any, int]] = {}
        self._seen_indices: set[int] = set()

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
        if index in self._seen_indices:
            raise MapResultTransportError(f"Duplicate completion for leaf index {index}")

        resolved_bytes = _estimate_item_bytes(value) if byte_size is None else int(byte_size)
        if resolved_bytes < 0:
            raise MapResultTransportError("byte_size must be non-negative")
        if resolved_bytes > self.config.max_chunk_bytes:
            self.abort(f"leaf {index} byte_size {resolved_bytes} exceeds max_chunk_bytes")
            raise MapResultTransportLimitError(
                f"Leaf {index} size ({resolved_bytes} B) exceeds max_chunk_bytes "
                f"({self.config.max_chunk_bytes} B)"
            )
        if self._total_bytes + resolved_bytes > self.config.max_total_bytes:
            self.abort("total map list byte ceiling exceeded")
            raise MapResultTransportLimitError(
                f"Map list output exceeds max_total_bytes ({self.config.max_total_bytes} B)"
            )

        self._seen_indices.add(index)
        self._completed_items += 1
        self._total_bytes += resolved_bytes

        if index != self._next_expected_index:
            if len(self._out_of_order) >= self.config.max_out_of_order_completions:
                self.abort("out-of-order completion buffer ceiling exceeded")
                raise MapResultTransportLimitError(
                    "Out-of-order leaf completions exceeded "
                    f"{self.config.max_out_of_order_completions}"
                )
            self._out_of_order[index] = (value, resolved_bytes)
            self._peak_out_of_order = max(self._peak_out_of_order, len(self._out_of_order))
            return

        self._append_contiguous_leaf(index, value, resolved_bytes)
        while self._next_expected_index in self._out_of_order:
            next_idx = self._next_expected_index
            buffered_val, buffered_bytes = self._out_of_order.pop(next_idx)
            self._append_contiguous_leaf(next_idx, buffered_val, buffered_bytes)

    def _append_contiguous_leaf(self, index: int, value: Any, byte_size: int) -> None:
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
        if self._on_release_leaf_ref is not None:
            self._on_release_leaf_ref(index)

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
        # Merge the oldest contiguous batch of handles at the lowest level.
        batch = self._live_handles[:batch_len]
        merged = self.store.merge_chunks(batch)
        for child in batch:
            self.store.release(child)
            self._released_handles_count += 1
        self._live_handles = [merged, *self._live_handles[batch_len:]]
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
            self._flush_current_chunk(end_index=self.total_items - 1)

    def finalize_downstream_handoff(self) -> DownstreamMapListHandoff:
        """Collapse all chunks into a single root handle for downstream steps."""
        self._seal_all_chunks()
        while len(self._live_handles) > 1:
            batch_len = min(len(self._live_handles), self.config.branching_factor)
            batch = self._live_handles[:batch_len]
            merged = self.store.merge_chunks(batch)
            for child in batch:
                self.store.release(child)
                self._released_handles_count += 1
            self._live_handles = [merged, *self._live_handles[batch_len:]]
            self._tree_merges += 1

        self._finalized = True
        root = self._live_handles[0] if self._live_handles else None
        return DownstreamMapListHandoff(
            root_handle=root,
            total_items=self.total_items,
            total_bytes=self._total_bytes,
            tree_depth=root.tree_level if root is not None else 0,
            chunk_count=self._chunks_emitted,
            peak_coordinator_live_refs=self._peak_live_refs,
        )

    def materialize_terminal_list(self) -> list[Any]:
        """Stream chunks in index order for a top-level terminal workflow return."""
        self._seal_all_chunks()
        result: list[Any] = []
        for handle in list(self._live_handles):
            result.extend(self.store.get_items(handle))
            self.store.release(handle)
            self._released_handles_count += 1
        self._live_handles.clear()
        self._finalized = True
        return result

    def abort(self, reason: str = "cancelled") -> None:
        """Fail-fast cleanup of all buffered items and remote chunk handles."""
        if self._aborted:
            return
        self._aborted = True
        self._abort_reason = reason
        self._current_chunk_items.clear()
        self._current_chunk_bytes = 0
        if self._on_release_leaf_ref is not None:
            for idx in sorted(self._out_of_order):
                self._on_release_leaf_ref(idx)
        self._out_of_order.clear()
        for handle in list(self._live_handles):
            self.store.release(handle)
            self._released_handles_count += 1
        self._live_handles.clear()

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
        )


__all__ = [
    "DEFAULT_MAP_CHUNK_SIZE",
    "DEFAULT_MAX_CHUNK_BYTES",
    "DEFAULT_MAX_COORDINATOR_LIVE_REFS",
    "DEFAULT_MAX_OUT_OF_ORDER_COMPLETIONS",
    "DEFAULT_MAX_TOTAL_LIST_BYTES",
    "DEFAULT_TREE_BRANCHING_FACTOR",
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
