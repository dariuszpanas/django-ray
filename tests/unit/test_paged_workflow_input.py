"""Unit tests for bounded paged workflow map input streaming (#94)."""

from __future__ import annotations

import pytest

from django_ray.workflow.paged_input import (
    BoundedPagedMapInputStream,
    PageBatch,
    PagedCursorExpiredError,
    PagedMapInputConfig,
    PagedMapInputConfigurationError,
    PagedMapInputLimitError,
    PagedMapStreamAbortedError,
    PagedSnapshotDriftError,
    PagedSourceThrottledError,
)


class FakeK8sPagedSource:
    def __init__(
        self,
        pages: list[PageBatch],
        *,
        fail_on_call: dict[int, Exception] | None = None,
    ) -> None:
        self.pages = list(pages)
        self.fail_on_call = dict(fail_on_call or {})
        self.calls: list[tuple[str | None, int, str | None]] = []
        self.closed = False
        self._page_idx = 0

    def fetch_page(
        self,
        *,
        cursor: str | None,
        page_size: int,
        snapshot_token: str | None,
    ) -> PageBatch:
        call_num = len(self.calls) + 1
        self.calls.append((cursor, page_size, snapshot_token))
        if call_num in self.fail_on_call:
            raise self.fail_on_call.pop(call_num)
        if cursor is None:
            self._page_idx = 0
        batch = self.pages[self._page_idx]
        self._page_idx += 1
        return batch

    def close(self) -> None:
        self.closed = True


def test_paged_input_config_validation() -> None:
    with pytest.raises(PagedMapInputConfigurationError, match="max_buffered_items must be >="):
        PagedMapInputConfig(page_size=50, max_buffered_items=10).validated()


def test_paged_stream_backpressure_and_deterministic_indexing() -> None:
    pages = [
        PageBatch(items=("pod-0", "pod-1"), next_cursor="c1", snapshot_token="rv-100"),
        PageBatch(items=("pod-2", "pod-3"), next_cursor="c2", snapshot_token="rv-100"),
        PageBatch(items=("pod-4",), next_cursor=None, snapshot_token="rv-100"),
    ]
    source = FakeK8sPagedSource(pages)
    config = PagedMapInputConfig(
        page_size=2,
        max_buffered_pages=1,
        max_buffered_items=2,
        max_buffered_bytes=4096,
    )
    stream = BoundedPagedMapInputStream(source, config)

    # Pull first two items (page 1).
    item0 = stream.next_item()
    item1 = stream.next_item()
    assert item0 is not None and (item0.index, item0.value) == (0, "pod-0")
    assert item1 is not None and (item1.index, item1.value) == (1, "pod-1")

    # Because both items from page 1 are still unacknowledged (in-flight == 2 == max_buffered_items),
    # pulling page 2 is backpressured until an item is acknowledged.
    assert not stream.can_prefetch_next_page()
    with pytest.raises(PagedMapInputLimitError, match="backpressured by unacknowledged"):
        stream.next_item()

    stream.acknowledge_item(0)
    stream.acknowledge_item(1)

    item2 = stream.next_item()
    item3 = stream.next_item()
    assert item2 is not None and (item2.index, item2.value) == (2, "pod-2")
    assert item3 is not None and (item3.index, item3.value) == (3, "pod-3")
    stream.acknowledge_item(2)
    stream.acknowledge_item(3)

    item4 = stream.next_item()
    assert item4 is not None and (item4.index, item4.value) == (4, "pod-4")
    stream.acknowledge_item(4)
    assert stream.next_item() is None

    diag = stream.diagnostics()
    assert diag.pages_fetched == 3
    assert diag.items_yielded == 5
    assert diag.items_acknowledged == 5
    assert diag.peak_buffered_pages <= 1
    assert diag.peak_buffered_items <= 2
    assert diag.source_closed is True
    assert source.closed is True


def test_snapshot_drift_and_expired_cursor_handling() -> None:
    # Snapshot drift across pages raises PagedSnapshotDriftError and closes source.
    drift_source = FakeK8sPagedSource(
        [
            PageBatch(items=("a",), next_cursor="c1", snapshot_token="rv-1"),
            PageBatch(items=("b",), next_cursor=None, snapshot_token="rv-2"),
        ]
    )
    drift_stream = BoundedPagedMapInputStream(
        drift_source,
        PagedMapInputConfig(page_size=1, max_buffered_items=4),
    )
    first = drift_stream.next_item()
    assert first is not None
    drift_stream.acknowledge_item(first.index)
    with pytest.raises(PagedSnapshotDriftError, match="drifted from 'rv-1' to 'rv-2'"):
        drift_stream.next_item()
    assert drift_source.closed is True

    # Expired cursor after items were already yielded fails closed even if relist is enabled.
    expired_source = FakeK8sPagedSource(
        [
            PageBatch(items=("a",), next_cursor="c1", snapshot_token="rv-1"),
            PageBatch(items=("b",), next_cursor=None, snapshot_token="rv-1"),
        ],
        fail_on_call={2: PagedCursorExpiredError("410 Gone", fresh_snapshot_token="rv-2")},
    )
    expired_stream = BoundedPagedMapInputStream(
        expired_source,
        PagedMapInputConfig(
            page_size=1,
            max_buffered_items=4,
            allow_relist_on_expired_cursor=True,
        ),
    )
    first_item = expired_stream.next_item()
    assert first_item is not None
    expired_stream.acknowledge_item(first_item.index)
    with pytest.raises(PagedCursorExpiredError, match="410 Gone"):
        expired_stream.next_item()
    assert expired_source.closed is True


def test_throttling_retry_and_downstream_abort_cleanup() -> None:
    sleeps: list[float] = []
    source = FakeK8sPagedSource(
        [
            PageBatch(items=("x", "y"), next_cursor="c1", snapshot_token="rv-10"),
            PageBatch(items=("z",), next_cursor=None, snapshot_token="rv-10"),
        ],
        fail_on_call={
            1: PagedSourceThrottledError("429", retry_after_seconds=0.25),
            2: PagedSourceThrottledError("429", retry_after_seconds=0.5),
        },
    )
    stream = BoundedPagedMapInputStream(
        source,
        PagedMapInputConfig(page_size=2, max_buffered_items=4, max_throttle_retries=2),
        sleep_fn=sleeps.append,
    )
    first = stream.next_item()
    assert first is not None and first.value == "x"
    assert sleeps == [0.25, 0.5]
    assert stream.diagnostics().throttle_retries == 2

    # Downstream failure aborts stream and closes source without pulling page 2.
    stream.abort_on_downstream_failure("leaf_0_failed")
    assert source.closed is True
    assert len(source.calls) == 3  # 2 throttled + 1 succeeded for page 1; page 2 never fetched
    with pytest.raises(PagedMapStreamAbortedError, match="leaf_0_failed"):
        stream.next_item()


def test_paged_input_config_and_stream_edge_cases() -> None:
    from django_ray.workflow.paged_input import (
        PagedMapInputConfigurationError,
        PagedMapInputError,
    )

    with pytest.raises(PagedMapInputConfigurationError, match="page_size"):
        PagedMapInputConfig(page_size=0).validated()
    with pytest.raises(PagedMapInputConfigurationError, match="max_buffered_pages"):
        PagedMapInputConfig(max_buffered_pages=0).validated()
    with pytest.raises(PagedMapInputConfigurationError, match="max_buffered_items"):
        PagedMapInputConfig(max_buffered_items=0).validated()
    with pytest.raises(PagedMapInputConfigurationError, match="max_buffered_bytes"):
        PagedMapInputConfig(max_buffered_bytes=10).validated()
    with pytest.raises(PagedMapInputConfigurationError, match="max_total_items"):
        PagedMapInputConfig(max_total_items=0).validated()
    with pytest.raises(PagedMapInputConfigurationError, match="max_total_pages"):
        PagedMapInputConfig(max_total_pages=0).validated()
    with pytest.raises(PagedMapInputConfigurationError, match="require_snapshot_consistency"):
        PagedMapInputConfig(require_snapshot_consistency="yes").validated()  # type: ignore[arg-type]
    with pytest.raises(PagedMapInputConfigurationError, match="allow_relist_on_expired_cursor"):
        PagedMapInputConfig(allow_relist_on_expired_cursor="yes").validated()  # type: ignore[arg-type]
    with pytest.raises(PagedMapInputConfigurationError, match="max_throttle_retries"):
        PagedMapInputConfig(max_throttle_retries=99).validated()

    # Relist on expired cursor before any items yielded
    relist_source = FakeK8sPagedSource(
        [PageBatch(items=("ok-after-relist",), next_cursor=None, snapshot_token="rv-2")],
        fail_on_call={1: PagedCursorExpiredError("410", fresh_snapshot_token="rv-2")},
    )
    relist_stream = BoundedPagedMapInputStream(
        relist_source,
        PagedMapInputConfig(page_size=1, max_buffered_items=2, allow_relist_on_expired_cursor=True),
    )
    it = relist_stream.next_item()
    assert it is not None and it.value == "ok-after-relist"
    assert relist_stream.in_flight_items == 1
    assert relist_stream.diagnostics().relist_count == 1
    with pytest.raises(PagedMapInputError, match="Unknown or already acknowledged"):
        relist_stream.acknowledge_item(999)

    # Duplicate continuation cursor and max_total_items / max_buffered_bytes limits
    dup_source = FakeK8sPagedSource(
        [
            PageBatch(items=("a",), next_cursor="dup", snapshot_token="rv-1"),
            PageBatch(items=("b",), next_cursor="dup", snapshot_token="rv-1"),
        ]
    )
    dup_stream = BoundedPagedMapInputStream(
        dup_source,
        PagedMapInputConfig(page_size=1, max_buffered_items=4),
    )
    first = dup_stream.next_item()
    assert first is not None
    dup_stream.acknowledge_item(first.index)
    with pytest.raises(PagedMapInputError, match="duplicate continuation cursor"):
        dup_stream.next_item()

    limit_source = FakeK8sPagedSource(
        [PageBatch(items=("1", "2"), next_cursor=None, snapshot_token="rv-1")]
    )
    limit_stream = BoundedPagedMapInputStream(
        limit_source,
        PagedMapInputConfig(page_size=2, max_buffered_items=2, max_total_items=1),
    )
    with pytest.raises(PagedMapInputLimitError, match="max_total_items"):
        limit_stream.next_item()
