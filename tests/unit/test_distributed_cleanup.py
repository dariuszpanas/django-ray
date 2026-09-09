"""Failure ownership checks without starting Ray or executing application work."""

from __future__ import annotations

from typing import Any

import pytest

from django_ray.runtime.distributed import _collect_remote_results


class _CollectorRay:
    def __init__(self) -> None:
        self.submitted: list[int] = []
        self.cancelled: list[tuple[int, bool, bool]] = []
        self.consumed: list[int] = []
        self.failure: BaseException = ValueError("original application failure")
        self.fail_submit: int | None = None
        self.fail_wait = False
        self.fail_get = False
        self.fail_cancel: BaseException | None = None

    def remote(self, value: int) -> int:
        if value == self.fail_submit:
            raise self.failure
        self.submitted.append(value)
        return value

    def wait(self, refs: list[int], *, num_returns: int) -> tuple[list[int], list[int]]:
        assert num_returns == 1
        if self.fail_wait:
            raise self.failure
        # Deliberately finish out of order.
        return refs[-1:], refs[:-1]

    def get(self, refs: Any) -> Any:
        if self.fail_get:
            raise self.failure
        if isinstance(refs, list):
            self.consumed.extend(refs)
            return [ref * 10 for ref in refs]
        self.consumed.append(refs)
        return refs * 10

    def cancel(self, ref: int, *, force: bool, recursive: bool) -> None:
        self.cancelled.append((ref, force, recursive))
        if self.fail_cancel is not None and len(self.cancelled) == 1:
            raise self.fail_cancel


@pytest.mark.parametrize("window", [None, 2, 8])
@pytest.mark.parametrize("failure_type", [ValueError, KeyboardInterrupt, SystemExit, GeneratorExit])
def test_collection_failure_cancels_owned_refs_and_preserves_exception(
    window: int | None, failure_type: type[BaseException]
) -> None:
    ray = _CollectorRay()
    ray.failure = failure_type("original failure")
    ray.fail_get = True
    with pytest.raises(failure_type) as caught:
        _collect_remote_results(ray, ray, [(i,) for i in range(4)], window)
    assert caught.value is ray.failure
    expected = [0, 1] if window == 2 else [0, 1, 2, 3]
    assert ray.submitted == expected
    assert ray.cancelled == [(ref, False, True) for ref in expected]


@pytest.mark.parametrize("window", [None, 2, 8])
def test_partial_submission_cleans_only_refs_actually_returned(window: int | None) -> None:
    ray = _CollectorRay()
    ray.fail_submit = 1
    with pytest.raises(ValueError) as caught:
        _collect_remote_results(ray, ray, [(i,) for i in range(4)], window)
    assert caught.value is ray.failure
    assert ray.submitted == [0]
    assert ray.cancelled == [(0, False, True)]


def test_wait_failure_cleans_entire_active_window() -> None:
    ray = _CollectorRay()
    ray.fail_wait = True
    with pytest.raises(ValueError) as caught:
        _collect_remote_results(ray, ray, [(i,) for i in range(4)], 2)
    assert caught.value is ray.failure
    assert ray.cancelled == [(0, False, True), (1, False, True)]


def test_replenishment_failure_does_not_cancel_consumed_or_unsubmitted_work() -> None:
    ray = _CollectorRay()
    ray.fail_submit = 2
    with pytest.raises(ValueError) as caught:
        _collect_remote_results(ray, ray, [(i,) for i in range(4)], 2)
    assert caught.value is ray.failure
    assert ray.consumed == [1]
    assert ray.submitted == [0, 1]
    assert ray.cancelled == [(0, False, True)]


@pytest.mark.parametrize("cleanup_failure", [RuntimeError("offline"), KeyboardInterrupt()])
def test_failed_cancellation_keeps_cleaning_without_replacing_original_exception(
    cleanup_failure: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ray = _CollectorRay()
    ray.fail_get = True
    ray.fail_cancel = cleanup_failure
    with pytest.raises(ValueError) as caught:
        _collect_remote_results(ray, ray, [(i,) for i in range(4)], 2)
    assert caught.value is ray.failure
    assert ray.cancelled == [(0, False, True), (1, False, True)]
    assert caplog.messages == ["Could not request cancellation for 1 distributed children"]


@pytest.mark.parametrize("window", [None, 1, 2, 8])
def test_success_preserves_order_without_cancellation(window: int | None) -> None:
    ray = _CollectorRay()
    assert _collect_remote_results(ray, ray, [(i,) for i in range(4)], window) == [0, 10, 20, 30]
    assert ray.cancelled == []


def test_first_submission_failure_has_nothing_to_cancel() -> None:
    ray = _CollectorRay()
    ray.fail_submit = 0
    with pytest.raises(ValueError) as caught:
        _collect_remote_results(ray, ray, [(0,)], None)
    assert caught.value is ray.failure
    assert ray.cancelled == []
