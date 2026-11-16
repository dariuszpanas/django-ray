"""Unit tests for bounded list-preserving map result transport (#91)."""

from __future__ import annotations

import gc
import math
import random
import weakref
from dataclasses import replace
from typing import Any, cast

import pytest

from django_ray.workflow.map_transport import (
    STORE_RELEASE_HISTORY_MAX_ITEMS,
    BoundedMapListAggregator,
    BoundedMapListTransportConfig,
    InMemoryChunkObjectStore,
    MapResultTransportAbortedError,
    MapResultTransportConfigurationError,
    MapResultTransportError,
    MapResultTransportLimitError,
)


def test_map_transport_config_validation() -> None:
    with pytest.raises(MapResultTransportConfigurationError, match="branching_factor must not"):
        BoundedMapListTransportConfig(
            branching_factor=16,
            max_coordinator_live_refs=8,
        ).validated()

    with pytest.raises(MapResultTransportConfigurationError, match="max_total_bytes must be"):
        BoundedMapListTransportConfig(
            max_chunk_bytes=1024,
            max_total_bytes=512,
        ).validated()


def test_out_of_order_completions_preserve_index_order_and_bound_live_refs() -> None:
    store = InMemoryChunkObjectStore()
    released_leaves: list[int] = []
    config = BoundedMapListTransportConfig(
        chunk_size=4,
        branching_factor=3,
        max_coordinator_live_refs=4,
        max_out_of_order_completions=16,
    )
    aggregator = BoundedMapListAggregator(
        total_items=40,
        config=config,
        store=store,
        on_release_leaf_ref=released_leaves.append,
    )

    # Feed each 4-item window in reverse order (3, 2, 1, 0, 7, 6, 5, 4, ...)
    for base in range(0, 40, 4):
        for offset in (3, 2, 1, 0):
            idx = base + offset
            aggregator.record_leaf_completion(idx, f"item-{idx:02d}")

    diag = aggregator.diagnostics()
    assert diag.peak_live_refs <= config.max_coordinator_live_refs
    assert diag.tree_merges_performed >= 2
    assert released_leaves == list(range(40))

    handoff = aggregator.finalize_downstream_handoff()
    assert handoff.total_items == 40
    assert handoff.root_handle is not None
    assert handoff.peak_coordinator_live_refs <= config.max_coordinator_live_refs
    # Only the single root handle remains live in the store for downstream handoff.
    assert store.live_object_count == 1
    assert store.get_items(handoff.root_handle) == [f"item-{i:02d}" for i in range(40)]


def test_terminal_list_materialization_releases_all_chunk_handles() -> None:
    store = InMemoryChunkObjectStore()
    aggregator = BoundedMapListAggregator(
        total_items=15,
        config=BoundedMapListTransportConfig(
            chunk_size=4,
            branching_factor=2,
            max_coordinator_live_refs=3,
        ),
        store=store,
    )
    for i in range(15):
        aggregator.record_leaf_completion(i, i * 10)

    items = aggregator.materialize_terminal_list()
    assert items == [i * 10 for i in range(15)]
    assert store.live_object_count == 0


def test_fail_fast_abort_and_limit_refusals_release_all_handles() -> None:
    store = InMemoryChunkObjectStore()
    released_leaves: list[int] = []
    config = BoundedMapListTransportConfig(
        chunk_size=2,
        branching_factor=2,
        max_coordinator_live_refs=4,
        max_out_of_order_completions=3,
        max_chunk_bytes=256,
        max_total_bytes=512,
    )
    aggregator = BoundedMapListAggregator(
        total_items=10,
        config=config,
        store=store,
        on_release_leaf_ref=released_leaves.append,
    )
    aggregator.record_leaf_completion(0, "a", byte_size=50)
    aggregator.record_leaf_completion(1, "b", byte_size=50)
    assert store.live_object_count == 1

    # Out-of-order completions exceeding buffer ceiling abort and clean up all handles.
    aggregator.record_leaf_completion(3, "d", byte_size=50)
    aggregator.record_leaf_completion(4, "e", byte_size=50)
    aggregator.record_leaf_completion(5, "f", byte_size=50)
    with pytest.raises(MapResultTransportLimitError, match="Out-of-order leaf completions"):
        aggregator.record_leaf_completion(6, "g", byte_size=50)

    assert store.live_object_count == 0
    assert aggregator.diagnostics().aborted is True
    assert sorted(released_leaves) == [0, 1, 3, 4, 5]

    with pytest.raises(MapResultTransportAbortedError):
        aggregator.record_leaf_completion(2, "c", byte_size=50)


def test_total_byte_ceiling_aborts_and_releases_chunks() -> None:
    store = InMemoryChunkObjectStore()
    aggregator = BoundedMapListAggregator(
        total_items=4,
        config=BoundedMapListTransportConfig(
            chunk_size=2,
            branching_factor=2,
            max_coordinator_live_refs=4,
            max_chunk_bytes=100,
            max_total_bytes=150,
        ),
        store=store,
    )
    aggregator.record_leaf_completion(0, "x", byte_size=60)
    aggregator.record_leaf_completion(1, "y", byte_size=60)
    with pytest.raises(MapResultTransportLimitError, match="max_total_bytes"):
        aggregator.record_leaf_completion(2, "z", byte_size=60)
    assert store.live_object_count == 0


def test_map_transport_config_and_error_branches() -> None:
    from django_ray.workflow.map_transport import (
        InMemoryChunkObjectStore,
        MapResultTransportConfigurationError,
        MapResultTransportError,
    )

    with pytest.raises(MapResultTransportConfigurationError, match="chunk_size"):
        BoundedMapListTransportConfig(chunk_size=0).validated()
    with pytest.raises(MapResultTransportConfigurationError, match="branching_factor"):
        BoundedMapListTransportConfig(branching_factor=1).validated()
    with pytest.raises(MapResultTransportConfigurationError, match="max_coordinator_live_refs"):
        BoundedMapListTransportConfig(max_coordinator_live_refs=1).validated()
    with pytest.raises(MapResultTransportConfigurationError, match="max_out_of_order_completions"):
        BoundedMapListTransportConfig(max_out_of_order_completions=0).validated()
    with pytest.raises(MapResultTransportConfigurationError, match="max_chunk_bytes"):
        BoundedMapListTransportConfig(max_chunk_bytes=16).validated()
    with pytest.raises(MapResultTransportConfigurationError, match="total_items"):
        BoundedMapListAggregator(-1)

    store = InMemoryChunkObjectStore()
    with pytest.raises(ValueError, match="at least one child"):
        store.merge_chunks([])

    agg = BoundedMapListAggregator(
        2,
        BoundedMapListTransportConfig(
            chunk_size=2,
            branching_factor=2,
            max_coordinator_live_refs=2,
            max_chunk_bytes=128,
            max_total_bytes=256,
        ),
    )
    with pytest.raises(MapResultTransportError, match="out of range"):
        agg.record_leaf_completion(5, "x")
    with pytest.raises(MapResultTransportError, match="byte_size must be non-negative"):
        agg.record_leaf_completion(0, "x", byte_size=-1)
    agg.record_leaf_completion(0, "a", byte_size=10)
    with pytest.raises(MapResultTransportError, match="Duplicate completion"):
        agg.record_leaf_completion(0, "a", byte_size=10)
    with pytest.raises(MapResultTransportError, match="Cannot finalize"):
        agg.finalize_downstream_handoff()
    agg.record_leaf_completion(1, "b", byte_size=10)
    assert agg.materialize_terminal_list() == ["a", "b"]
    with pytest.raises(MapResultTransportError, match="already finalized"):
        agg.record_leaf_completion(0, "a")


@pytest.mark.parametrize(
    ("total_items", "chunk_size", "branching_factor", "live_limit"),
    [(count, 1, 2, 2) for count in (100, 200, 400, 800)] + [(1025, 64, 16, 32), (257, 4, 3, 4)],
)
def test_balanced_compaction_bounds_actual_work_and_payload_traversal(
    total_items: int, chunk_size: int, branching_factor: int, live_limit: int
) -> None:
    store = InMemoryChunkObjectStore()
    aggregator = BoundedMapListAggregator(
        total_items,
        BoundedMapListTransportConfig(
            chunk_size=chunk_size,
            branching_factor=branching_factor,
            max_coordinator_live_refs=live_limit,
        ),
        store=store,
    )
    for index in range(total_items):
        aggregator.record_leaf_completion(index, index, byte_size=1)
        assert store.live_object_count <= live_limit
        assert aggregator.diagnostics().current_live_refs <= live_limit

    handoff = aggregator.finalize_downstream_handoff()
    root = handoff.root_handle
    assert root is not None
    chunks = math.ceil(total_items / chunk_size)
    levels = math.ceil(math.log2(chunks))
    assert handoff.tree_depth <= 2 * levels
    assert store.join_node_visits <= 2 * chunks * max(levels, 1)
    assert store.branch_nodes_created <= 4 * chunks * max(levels, 1)
    assert store.peak_live_object_count <= live_limit
    assert store.payload_items_stored == total_items
    assert store.payload_items_read == 0
    # Inspect structure without flattening: original leaves remain payload
    # owners and every branch satisfies the AVL height invariant.
    pending = [store._objects[root.handle_id]]
    leaf_count = 0
    branch_count = 0
    while pending:
        node = pending.pop()
        if hasattr(node, "items"):
            leaf_count += 1
        else:
            branch_count += 1
            assert abs(node.left.height - node.right.height) <= 1
            assert node.height == max(node.left.height, node.right.height) + 1
            pending.extend((node.right, node.left))
    assert leaf_count == chunks
    assert branch_count == chunks - 1
    assert aggregator.diagnostics().current_live_refs == 0
    assert not aggregator._out_of_order
    assert not aggregator._failed_leaf_releases
    assert not hasattr(aggregator, "_seen_indices")
    assert len(store.released_handle_ids) <= STORE_RELEASE_HISTORY_MAX_ITEMS
    assert store.get_items(root) == list(range(total_items))
    assert store.payload_items_read == total_items
    store.release(root)
    assert store.live_object_count == 0


def test_store_ownership_survives_nonconsuming_merge_and_downstream_handoff() -> None:
    store = InMemoryChunkObjectStore()
    left = store.put_chunk(["left"], start_index=0, end_index=0, byte_size=1)
    right = store.put_chunk(["right"], start_index=1, end_index=1, byte_size=1)
    merged = store.merge_chunks([left, right])
    assert store.get_items(left) == ["left"]
    assert store.get_items(right) == ["right"]
    store.release(left)
    store.release(right)
    assert store.get_items(merged) == ["left", "right"]
    store.release(merged)

    aggregator = BoundedMapListAggregator(2, store=store)
    aggregator.record_leaf_completion(0, "a")
    aggregator.record_leaf_completion(1, "b")
    root = aggregator.finalize_downstream_handoff().root_handle
    assert root is not None
    aggregator.abort()
    assert store.get_items(root) == ["a", "b"]
    store.release(root)
    assert store.live_object_count == 0


def test_consuming_compaction_reclaims_payload_only_after_final_owner_release() -> None:
    class Payload:
        pass

    store = InMemoryChunkObjectStore()
    payload = Payload()
    observed = weakref.ref(payload)
    left = store.put_chunk([payload], start_index=0, end_index=0, byte_size=1)
    right = store.put_chunk(["other"], start_index=1, end_index=1, byte_size=1)
    del payload
    root = store.compact_chunks([left, right])
    assert observed() is not None
    with pytest.raises(MapResultTransportError, match="unavailable"):
        store.get_items(left)
    assert store.live_object_count == 1
    released = store.released_handle_count
    store.release(left)
    assert store.released_handle_count == released
    store.release(root)
    gc.collect()
    assert observed() is None


def test_consuming_compaction_failure_keeps_children_and_other_store_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryChunkObjectStore()
    left = store.put_chunk(["left"], start_index=0, end_index=0, byte_size=1)
    right = store.put_chunk(["right"], start_index=1, end_index=1, byte_size=1)

    def reject_publication(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("publication refused")

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "_publish_root", reject_publication)
        with pytest.raises(RuntimeError, match="publication refused"):
            store.compact_chunks([left, right])
    assert store.get_items(left) == ["left"]
    assert store.get_items(right) == ["right"]
    assert store.live_object_count == 2
    assert store.released_handle_count == 0

    for operation in (store.merge_chunks, store.compact_chunks):
        with pytest.raises(MapResultTransportError, match="duplicate"):
            operation([left, left])
        with pytest.raises(MapResultTransportError, match="contiguous"):
            operation([right, left])
        with pytest.raises(MapResultTransportError, match="metadata"):
            operation([replace(left, item_count=2), right])
    other = InMemoryChunkObjectStore()
    other_left = other.put_chunk(["foreign"], start_index=0, end_index=0, byte_size=1)
    with pytest.raises(MapResultTransportError, match="unavailable"):
        store.compact_chunks([other_left, right])
    other.release(left)
    assert other.get_items(other_left) == ["foreign"]
    assert store.live_object_count == 2


def test_duplicate_indices_need_only_the_completed_prefix_and_bounded_buffer() -> None:
    aggregator = BoundedMapListAggregator(5)
    aggregator.record_leaf_completion(3, "three")
    with pytest.raises(MapResultTransportError, match="Duplicate"):
        aggregator.record_leaf_completion(3, "duplicate")
    for index in (0, 1, 2):
        aggregator.record_leaf_completion(index, str(index))
    with pytest.raises(MapResultTransportError, match="Duplicate"):
        aggregator.record_leaf_completion(3, "already drained")
    aggregator.record_leaf_completion(4, "four")
    assert aggregator.materialize_terminal_list() == ["0", "1", "2", "three", "four"]


def test_release_history_is_a_bounded_tail_with_an_exact_scalar_total() -> None:
    store = InMemoryChunkObjectStore()
    for index in range(STORE_RELEASE_HISTORY_MAX_ITEMS * 3):
        handle = store.put_chunk([index], start_index=index, end_index=index, byte_size=1)
        store.release(handle)
    assert len(store.released_handle_ids) == STORE_RELEASE_HISTORY_MAX_ITEMS
    assert store.released_handle_ids[-1] == handle.handle_id
    assert store.released_handle_count == STORE_RELEASE_HISTORY_MAX_ITEMS * 3
    assert store.live_object_count == 0


@pytest.mark.parametrize("operation", ["put_chunk", "compact_chunks", "get_items"])
def test_store_failures_abort_and_reclaim_owned_roots(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store = InMemoryChunkObjectStore()
    released: list[int] = []
    aggregator = BoundedMapListAggregator(
        4,
        BoundedMapListTransportConfig(
            chunk_size=1, branching_factor=2, max_coordinator_live_refs=2
        ),
        store=store,
        on_release_leaf_ref=released.append,
    )
    aggregator.record_leaf_completion(0, "zero")
    failure = RuntimeError("store unavailable")

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise failure

    if operation == "get_items":
        for index in (1, 2, 3):
            aggregator.record_leaf_completion(index, str(index))
    monkeypatch.setattr(store, operation, fail)
    with pytest.raises(RuntimeError) as caught:
        if operation == "get_items":
            aggregator.materialize_terminal_list()
        else:
            aggregator.record_leaf_completion(1, "one")
    assert caught.value is failure
    assert aggregator.diagnostics().aborted
    assert store.live_object_count == 0
    assert aggregator.diagnostics().current_live_refs == 0


def test_callback_and_release_failures_continue_cleanup_and_remain_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryChunkObjectStore()
    calls: list[int] = []
    callback_fails = True

    def release_leaf(index: int) -> None:
        calls.append(index)
        if callback_fails and index == 3:
            raise RuntimeError("leaf release refused")

    aggregator = BoundedMapListAggregator(
        6,
        BoundedMapListTransportConfig(chunk_size=1),
        store=store,
        on_release_leaf_ref=release_leaf,
    )
    aggregator.record_leaf_completion(0, "stored")
    aggregator.record_leaf_completion(3, "buffered")
    aggregator.record_leaf_completion(4, "also buffered")
    original_release = store.release

    def refuse_release(_handle: Any) -> None:
        raise RuntimeError("root release refused")

    monkeypatch.setattr(store, "release", refuse_release)
    with pytest.raises(RuntimeError, match="leaf release refused"):
        aggregator.abort()
    assert calls == [0, 3, 4]
    assert store.live_object_count == 1
    assert aggregator.diagnostics().current_live_refs == 1
    assert aggregator.diagnostics().released_handles_count == 0
    assert aggregator.diagnostics().pending_leaf_releases == 1
    callback_fails = False
    monkeypatch.setattr(store, "release", original_release)
    aggregator.abort()
    assert calls == [0, 3, 4, 3]
    assert store.live_object_count == 0
    assert aggregator.diagnostics().pending_leaf_releases == 0
    assert aggregator.diagnostics().released_handles_count == 1


def test_incoming_leaf_is_released_if_a_prior_byte_full_chunk_cannot_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[int] = []
    store = InMemoryChunkObjectStore()
    aggregator = BoundedMapListAggregator(
        2,
        BoundedMapListTransportConfig(chunk_size=2, max_chunk_bytes=64),
        store=store,
        on_release_leaf_ref=released.append,
    )
    aggregator.record_leaf_completion(0, "first", byte_size=64)

    def fail_put(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cannot publish previous chunk")

    monkeypatch.setattr(store, "put_chunk", fail_put)
    with pytest.raises(RuntimeError, match="cannot publish previous chunk"):
        aggregator.record_leaf_completion(1, "incoming", byte_size=1)
    assert released == [0, 1]
    assert store.live_object_count == 0


def test_old_store_without_consuming_operation_is_refused() -> None:
    with pytest.raises(MapResultTransportConfigurationError, match="consuming compact_chunks"):
        BoundedMapListAggregator(1, store=cast(Any, object()))


def test_empty_handoff_and_terminal_list_have_no_store_roots() -> None:
    aggregator = BoundedMapListAggregator(0)
    assert aggregator.finalize_downstream_handoff().root_handle is None
    assert aggregator.store.live_object_count == 0
    assert BoundedMapListAggregator(0).materialize_terminal_list() == []


@pytest.mark.parametrize("seed", range(8))
def test_store_balances_arbitrary_ordered_concatenation_shapes(seed: int) -> None:
    store = InMemoryChunkObjectStore()
    roots = [
        store.put_chunk([index], start_index=index, end_index=index, byte_size=1)
        for index in range(127)
    ]
    generator = random.Random(seed)
    while len(roots) > 1:
        position = generator.randrange(len(roots) - 1)
        parent = store.compact_chunks(roots[position : position + 2])
        roots[position : position + 2] = [parent]
    assert roots[0].tree_level <= 2 * math.ceil(math.log2(127))
    assert store.payload_items_read == 0
    assert store.get_items(roots[0]) == list(range(127))
    store.release(roots[0])
    assert store.live_object_count == 0


def test_shared_root_aliases_cannot_be_merged_as_disjoint_ranges() -> None:
    store = InMemoryChunkObjectStore()
    left = store.put_chunk(["left"], start_index=0, end_index=0, byte_size=1)
    right = store.put_chunk(["right"], start_index=1, end_index=1, byte_size=1)
    alias = store.merge_chunks([left])
    parent = store.merge_chunks([left, right])
    for operation in (store.merge_chunks, store.compact_chunks):
        for children in ([left, alias], [parent, right]):
            with pytest.raises(MapResultTransportError, match="contiguous"):
                operation(children)
    replacement = store.compact_chunks([left, right])
    store.release(replacement)
    assert store.get_items(alias) == ["left"]
    assert store.get_items(parent) == ["left", "right"]
    store.release(alias)
    store.release(parent)
    assert store.live_object_count == 0


def test_compaction_allocation_failure_does_not_transfer_outward_ownership() -> None:
    class RefuseCopy(dict):
        def copy(self):
            raise MemoryError("root map allocation refused")

    store = InMemoryChunkObjectStore()
    left = store.put_chunk(["a"], start_index=0, end_index=0, byte_size=1)
    right = store.put_chunk(["b"], start_index=1, end_index=1, byte_size=1)
    store._objects = RefuseCopy(store._objects)
    with pytest.raises(MemoryError, match="root map allocation refused"):
        store.compact_chunks([left, right])
    assert store.get_items(left) == ["a"]
    assert store.get_items(right) == ["b"]
    assert store.released_handle_count == 0
    assert store.live_object_count == 2


@pytest.mark.parametrize(
    "terminal_method", ["materialize_terminal_list", "finalize_downstream_handoff"]
)
@pytest.mark.parametrize("operation", ["put_chunk", "compact_chunks"])
def test_finalization_store_failures_preserve_the_error_and_clean_up(
    monkeypatch: pytest.MonkeyPatch, terminal_method: str, operation: str
) -> None:
    store = InMemoryChunkObjectStore()
    live_limit = 2 if terminal_method == "materialize_terminal_list" else 32
    aggregator = BoundedMapListAggregator(
        3,
        BoundedMapListTransportConfig(
            chunk_size=2, branching_factor=2, max_coordinator_live_refs=live_limit
        ),
        store=store,
    )
    for index in range(3):
        aggregator.record_leaf_completion(index, index)
    failure = RuntimeError("finalization failed")

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(store, operation, fail)
    with pytest.raises(RuntimeError) as caught:
        getattr(aggregator, terminal_method)()
    assert caught.value is failure
    assert store.live_object_count == 0
    assert aggregator.diagnostics().aborted


def test_primary_failure_survives_cleanup_failure_until_explicit_abort_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryChunkObjectStore()
    aggregator = BoundedMapListAggregator(
        2,
        BoundedMapListTransportConfig(chunk_size=1),
        store=store,
    )
    aggregator.record_leaf_completion(0, "retained")
    original_release = store.release

    def refuse_release(_handle: Any) -> None:
        raise RuntimeError("release unavailable")

    monkeypatch.setattr(store, "release", refuse_release)
    with pytest.raises(MapResultTransportLimitError, match="exceeds max_chunk_bytes") as caught:
        aggregator.record_leaf_completion(1, "oversized", byte_size=10**9)
    assert "cleanup also failed" in caught.value.__notes__[0]
    assert aggregator.diagnostics().current_live_refs == 1
    assert aggregator.diagnostics().released_handles_count == 0
    monkeypatch.setattr(store, "release", original_release)
    aggregator.abort()
    assert store.live_object_count == 0


def test_callback_failure_during_normal_admission_is_reported_without_retrying_it() -> None:
    callback_calls: list[int] = []

    def fail_release(index: int) -> None:
        callback_calls.append(index)
        raise RuntimeError("release callback failed")

    aggregator = BoundedMapListAggregator(1, on_release_leaf_ref=fail_release)
    with pytest.raises(RuntimeError, match="release callback failed"):
        aggregator.record_leaf_completion(0, "value")
    assert callback_calls == [0]
    assert aggregator.diagnostics().pending_leaf_releases == 1
    assert aggregator.diagnostics().aborted
