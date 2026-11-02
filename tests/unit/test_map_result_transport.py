"""Unit tests for bounded list-preserving map result transport (#91)."""

from __future__ import annotations

import pytest

from django_ray.workflow.map_transport import (
    BoundedMapListAggregator,
    BoundedMapListTransportConfig,
    InMemoryChunkObjectStore,
    MapResultTransportAbortedError,
    MapResultTransportConfigurationError,
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
