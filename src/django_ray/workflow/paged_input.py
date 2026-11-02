"""Bounded, backpressure-aware paged workflow map input stream (#94).

Instead of materializing an entire paginated external collection (such as a
Kubernetes ``list`` with ``continue`` tokens and ``resourceVersion``, a REST
API cursor, or a keyset-paginated query) in an upstream step and passing a
monolithic list to ``Map``, this module provides a pull-driven, bounded paged
input stream with:

- Producer-ahead page, item, and byte backpressure caps.
- Deterministic 0-based item indexing across page boundaries.
- Snapshot consistency verification (e.g. Kubernetes ``resourceVersion``) and
  fail-closed handling of expired continuation cursors (HTTP ``410 Gone``).
- Bounded ``Retry-After`` throttle backoff and guaranteed source cleanup on
  downstream leaf failure or task cancellation.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 10_000
DEFAULT_MAX_BUFFERED_PAGES = 2
MAX_BUFFERED_PAGES_CEILING = 32
DEFAULT_MAX_BUFFERED_ITEMS = 500
MAX_BUFFERED_ITEMS_CEILING = 50_000
DEFAULT_MAX_BUFFERED_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_TOTAL_PAGED_ITEMS = 100_000
DEFAULT_MAX_TOTAL_PAGES = 2_000
DEFAULT_MAX_THROTTLE_RETRIES = 3
MAX_THROTTLE_BACKOFF_SECONDS = 60.0


class PagedMapInputError(RuntimeError):
    """Base error for paged workflow map input streaming."""


class PagedMapInputConfigurationError(PagedMapInputError, ValueError):
    """Raised when ``PagedMapInputConfig`` is invalid."""


class PagedMapInputLimitError(PagedMapInputError):
    """Raised when paged input exceeds buffered or total page/item/byte ceilings."""


class PagedSnapshotDriftError(PagedMapInputError):
    """Raised when a paginated source changes snapshot/resourceVersion mid-stream."""


class PagedCursorExpiredError(PagedMapInputError):
    """Raised by a paged source when its continuation cursor expires (e.g. 410 Gone)."""

    def __init__(
        self,
        message: str = "Continuation cursor expired",
        *,
        fresh_snapshot_token: str | None = None,
    ) -> None:
        super().__init__(message)
        self.fresh_snapshot_token = fresh_snapshot_token


class PagedSourceThrottledError(PagedMapInputError):
    """Raised by a paged source when rate-limited (e.g. 429 Too Many Requests)."""

    def __init__(
        self,
        message: str = "Paged source throttled",
        *,
        retry_after_seconds: float = 1.0,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(
            0.0, min(float(retry_after_seconds), MAX_THROTTLE_BACKOFF_SECONDS)
        )


class PagedMapStreamAbortedError(PagedMapInputError):
    """Raised when a paged input stream was aborted due to downstream failure or cancellation."""


@dataclass(frozen=True)
class PageBatch:
    """One page of items returned by a ``PagedSourceProtocol`` implementation."""

    items: tuple[Any, ...]
    next_cursor: str | None = None
    snapshot_token: str | None = None
    byte_size: int | None = None


class PagedSourceProtocol(Protocol):
    """Protocol for remotely-backed paginated input sources."""

    def fetch_page(
        self,
        *,
        cursor: str | None,
        page_size: int,
        snapshot_token: str | None,
    ) -> PageBatch: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class PagedMapInputConfig:
    """Configuration and backpressure bounds for paged workflow map inputs."""

    page_size: int = DEFAULT_PAGE_SIZE
    max_buffered_pages: int = DEFAULT_MAX_BUFFERED_PAGES
    max_buffered_items: int = DEFAULT_MAX_BUFFERED_ITEMS
    max_buffered_bytes: int = DEFAULT_MAX_BUFFERED_BYTES
    max_total_items: int = DEFAULT_MAX_TOTAL_PAGED_ITEMS
    max_total_pages: int = DEFAULT_MAX_TOTAL_PAGES
    require_snapshot_consistency: bool = True
    allow_relist_on_expired_cursor: bool = False
    max_throttle_retries: int = DEFAULT_MAX_THROTTLE_RETRIES

    def validated(self) -> PagedMapInputConfig:
        if type(self.page_size) is not int or not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise PagedMapInputConfigurationError(
                f"page_size must be an int between 1 and {MAX_PAGE_SIZE}"
            )
        if (
            type(self.max_buffered_pages) is not int
            or not 1 <= self.max_buffered_pages <= MAX_BUFFERED_PAGES_CEILING
        ):
            raise PagedMapInputConfigurationError(
                f"max_buffered_pages must be an int between 1 and {MAX_BUFFERED_PAGES_CEILING}"
            )
        if (
            type(self.max_buffered_items) is not int
            or not 1 <= self.max_buffered_items <= MAX_BUFFERED_ITEMS_CEILING
        ):
            raise PagedMapInputConfigurationError(
                f"max_buffered_items must be an int between 1 and {MAX_BUFFERED_ITEMS_CEILING}"
            )
        if self.max_buffered_items < self.page_size:
            raise PagedMapInputConfigurationError(
                "max_buffered_items must be >= page_size so at least one full page fits"
            )
        if type(self.max_buffered_bytes) is not int or self.max_buffered_bytes < 64:
            raise PagedMapInputConfigurationError("max_buffered_bytes must be at least 64 bytes")
        if type(self.max_total_items) is not int or self.max_total_items < 1:
            raise PagedMapInputConfigurationError("max_total_items must be >= 1")
        if type(self.max_total_pages) is not int or self.max_total_pages < 1:
            raise PagedMapInputConfigurationError("max_total_pages must be >= 1")
        if type(self.require_snapshot_consistency) is not bool:
            raise PagedMapInputConfigurationError("require_snapshot_consistency must be a bool")
        if type(self.allow_relist_on_expired_cursor) is not bool:
            raise PagedMapInputConfigurationError("allow_relist_on_expired_cursor must be a bool")
        if type(self.max_throttle_retries) is not int or not 0 <= self.max_throttle_retries <= 20:
            raise PagedMapInputConfigurationError("max_throttle_retries must be between 0 and 20")
        return self


@dataclass(frozen=True)
class PagedMapItem:
    """Deterministic indexed item emitted by ``BoundedPagedMapInputStream``."""

    index: int
    page_number: int
    value: Any
    byte_size: int
    snapshot_token: str | None


@dataclass(frozen=True)
class PagedMapStreamDiagnostics:
    """Bounded operational diagnostics for paged workflow map input streaming."""

    pages_fetched: int
    items_yielded: int
    items_acknowledged: int
    buffered_pages: int
    buffered_items: int
    buffered_bytes: int
    peak_buffered_pages: int
    peak_buffered_items: int
    peak_buffered_bytes: int
    throttle_retries: int
    relist_count: int
    snapshot_token: str | None
    source_closed: bool
    aborted: bool
    abort_reason: str | None


def _estimate_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return len(repr(value).encode("utf-8", errors="replace"))


class BoundedPagedMapInputStream:
    """Backpressure-bounded pull stream over a ``PagedSourceProtocol``."""

    def __init__(
        self,
        source: PagedSourceProtocol,
        config: PagedMapInputConfig | None = None,
        *,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.source = source
        self.config = (config or PagedMapInputConfig()).validated()
        self._sleep_fn = sleep_fn or (lambda _s: None)

        self._cursor: str | None = None
        self._snapshot_token: str | None = None
        self._exhausted = False
        self._closed = False
        self._aborted = False
        self._abort_reason: str | None = None

        self._page_queue: deque[deque[PagedMapItem]] = deque()
        self._in_flight_bytes_by_index: dict[int, int] = {}
        self._buffered_items = 0
        self._buffered_bytes = 0

        self._pages_fetched = 0
        self._next_item_index = 0
        self._items_yielded = 0
        self._items_acknowledged = 0
        self._peak_buffered_pages = 0
        self._peak_buffered_items = 0
        self._peak_buffered_bytes = 0
        self._throttle_retries = 0
        self._relist_count = 0
        self._seen_cursors: set[str] = set()

    @property
    def in_flight_items(self) -> int:
        return len(self._in_flight_bytes_by_index)

    @property
    def total_outstanding_items(self) -> int:
        return self._buffered_items + len(self._in_flight_bytes_by_index)

    @property
    def total_outstanding_bytes(self) -> int:
        return self._buffered_bytes + sum(self._in_flight_bytes_by_index.values())

    def can_prefetch_next_page(self) -> bool:
        """Return whether producer-ahead backpressure allows fetching another page."""
        if self._closed or self._aborted or self._exhausted:
            return False
        if len(self._page_queue) >= self.config.max_buffered_pages:
            return False
        if self.total_outstanding_items + self.config.page_size > self.config.max_buffered_items:
            # Allow fetching if there are zero buffered items AND zero in-flight items.
            if self.total_outstanding_items > 0:
                return False
        if self.total_outstanding_bytes >= self.config.max_buffered_bytes:
            return False
        return True

    def prefetch_page(self) -> bool:
        """Fetch one page from the source if backpressure permits."""
        self._ensure_usable()
        if not self.can_prefetch_next_page():
            return False
        if self._pages_fetched >= self.config.max_total_pages:
            self._fail_and_close(
                PagedMapInputLimitError(
                    f"Paged source exceeded max_total_pages ({self.config.max_total_pages})"
                )
            )

        batch = self._fetch_page_with_retry_and_relist()
        self._pages_fetched += 1
        page_number = self._pages_fetched

        if batch.snapshot_token is not None:
            if self._snapshot_token is None:
                self._snapshot_token = batch.snapshot_token
            elif (
                self.config.require_snapshot_consistency
                and batch.snapshot_token != self._snapshot_token
            ):
                self._fail_and_close(
                    PagedSnapshotDriftError(
                        f"Paged source snapshot drifted from {self._snapshot_token!r} "
                        f"to {batch.snapshot_token!r}"
                    )
                )

        page_items: deque[PagedMapItem] = deque()
        page_bytes = 0
        for raw in batch.items:
            if self._next_item_index >= self.config.max_total_items:
                self._fail_and_close(
                    PagedMapInputLimitError(
                        f"Paged source exceeded max_total_items ({self.config.max_total_items})"
                    )
                )
            item_bytes = _estimate_bytes(raw)
            if (
                self.total_outstanding_bytes + page_bytes + item_bytes
                > self.config.max_buffered_bytes
            ):
                self._fail_and_close(
                    PagedMapInputLimitError(
                        f"Paged source buffered bytes exceeded max_buffered_bytes "
                        f"({self.config.max_buffered_bytes} B)"
                    )
                )
            item = PagedMapItem(
                index=self._next_item_index,
                page_number=page_number,
                value=raw,
                byte_size=item_bytes,
                snapshot_token=self._snapshot_token,
            )
            self._next_item_index += 1
            page_items.append(item)
            page_bytes += item_bytes

        if page_items:
            self._page_queue.append(page_items)
            self._buffered_items += len(page_items)
            self._buffered_bytes += page_bytes
            self._peak_buffered_pages = max(self._peak_buffered_pages, len(self._page_queue))
            self._peak_buffered_items = max(self._peak_buffered_items, self.total_outstanding_items)
            self._peak_buffered_bytes = max(self._peak_buffered_bytes, self.total_outstanding_bytes)

        if batch.next_cursor is None or batch.next_cursor == "":
            self._cursor = None
            self._exhausted = True
            self.close()
        else:
            if batch.next_cursor in self._seen_cursors:
                self._fail_and_close(
                    PagedMapInputError(
                        f"Paged source returned duplicate continuation cursor {batch.next_cursor!r}"
                    )
                )
            self._seen_cursors.add(batch.next_cursor)
            self._cursor = batch.next_cursor
        return True

    def _fetch_page_with_retry_and_relist(self) -> PageBatch:
        attempts = 0
        while True:
            try:
                return self.source.fetch_page(
                    cursor=self._cursor,
                    page_size=self.config.page_size,
                    snapshot_token=self._snapshot_token,
                )
            except PagedSourceThrottledError as exc:
                if attempts >= self.config.max_throttle_retries:
                    self._fail_and_close(exc)
                attempts += 1
                self._throttle_retries += 1
                self._sleep_fn(exc.retry_after_seconds)
            except PagedCursorExpiredError as exc:
                if (
                    self.config.allow_relist_on_expired_cursor
                    and self._items_yielded == 0
                    and self._relist_count == 0
                ):
                    self._relist_count += 1
                    self._cursor = None
                    self._snapshot_token = exc.fresh_snapshot_token
                    self._seen_cursors.clear()
                    self._page_queue.clear()
                    self._buffered_items = 0
                    self._buffered_bytes = 0
                    self._next_item_index = 0
                    continue
                self._fail_and_close(exc)
            except Exception as exc:
                self._fail_and_close(exc)

    def next_item(self) -> PagedMapItem | None:
        """Pull the next indexed item, fetching a page if needed and permitted."""
        self._ensure_usable()
        while not self._page_queue and not self._exhausted:
            if not self.can_prefetch_next_page():
                raise PagedMapInputLimitError(
                    "Paged input stream is backpressured by unacknowledged in-flight items; "
                    "call acknowledge_item() before pulling more items"
                )
            self.prefetch_page()

        if not self._page_queue:
            return None

        front_page = self._page_queue[0]
        item = front_page.popleft()
        if not front_page:
            self._page_queue.popleft()
        self._buffered_items -= 1
        self._buffered_bytes -= item.byte_size
        self._in_flight_bytes_by_index[item.index] = item.byte_size
        self._items_yielded += 1
        return item

    def acknowledge_item(self, index: int) -> None:
        """Release backpressure budget for a completed leaf item."""
        if index not in self._in_flight_bytes_by_index:
            raise PagedMapInputError(f"Unknown or already acknowledged item index: {index}")
        del self._in_flight_bytes_by_index[index]
        self._items_acknowledged += 1

    def abort_on_downstream_failure(self, reason: str = "downstream_leaf_failed") -> None:
        """Immediately stop pulling pages, clear buffers, and close the source."""
        if not self._aborted:
            self._aborted = True
            self._abort_reason = reason
        self._page_queue.clear()
        self._in_flight_bytes_by_index.clear()
        self._buffered_items = 0
        self._buffered_bytes = 0
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.source.close()
            except Exception:
                pass

    def _ensure_usable(self) -> None:
        if self._aborted:
            raise PagedMapStreamAbortedError(
                f"Paged map input stream was aborted: {self._abort_reason or 'cancelled'}"
            )

    def _fail_and_close(self, exc: BaseException) -> Any:
        self._aborted = True
        self._abort_reason = type(exc).__name__
        self._page_queue.clear()
        self._in_flight_bytes_by_index.clear()
        self._buffered_items = 0
        self._buffered_bytes = 0
        self.close()
        raise exc

    def diagnostics(self) -> PagedMapStreamDiagnostics:
        return PagedMapStreamDiagnostics(
            pages_fetched=self._pages_fetched,
            items_yielded=self._items_yielded,
            items_acknowledged=self._items_acknowledged,
            buffered_pages=len(self._page_queue),
            buffered_items=self._buffered_items,
            buffered_bytes=self._buffered_bytes,
            peak_buffered_pages=self._peak_buffered_pages,
            peak_buffered_items=self._peak_buffered_items,
            peak_buffered_bytes=self._peak_buffered_bytes,
            throttle_retries=self._throttle_retries,
            relist_count=self._relist_count,
            snapshot_token=self._snapshot_token,
            source_closed=self._closed,
            aborted=self._aborted,
            abort_reason=self._abort_reason,
        )


__all__ = [
    "DEFAULT_MAX_BUFFERED_BYTES",
    "DEFAULT_MAX_BUFFERED_ITEMS",
    "DEFAULT_MAX_BUFFERED_PAGES",
    "DEFAULT_MAX_THROTTLE_RETRIES",
    "DEFAULT_MAX_TOTAL_PAGED_ITEMS",
    "DEFAULT_MAX_TOTAL_PAGES",
    "DEFAULT_PAGE_SIZE",
    "BoundedPagedMapInputStream",
    "PageBatch",
    "PagedCursorExpiredError",
    "PagedMapInputConfig",
    "PagedMapInputConfigurationError",
    "PagedMapInputError",
    "PagedMapInputLimitError",
    "PagedMapItem",
    "PagedMapStreamAbortedError",
    "PagedMapStreamDiagnostics",
    "PagedSnapshotDriftError",
    "PagedSourceProtocol",
    "PagedSourceThrottledError",
]
