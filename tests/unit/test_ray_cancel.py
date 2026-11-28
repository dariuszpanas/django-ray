"""Unit tests for unready-only Ray ObjectRef cancellation helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from django_ray.runtime.ray_cancel import (
    cancel_unready_ray_refs,
    is_ray_ref_ready,
    select_unready_ray_refs,
)


def test_select_unready_ray_refs_handles_empty_input_without_calling_wait() -> None:
    calls: list[Any] = []
    ray = SimpleNamespace(wait=lambda *args, **kwargs: calls.append((args, kwargs)))

    assert select_unready_ray_refs(ray, []) == []
    assert calls == []


def test_select_unready_ray_refs_deduplicates_and_uses_nonblocking_zero_fetch_wait() -> None:
    observed: list[dict[str, Any]] = []

    def wait(
        refs: list[Any],
        *,
        num_returns: int,
        timeout: float,
        fetch_local: bool,
    ) -> tuple[list[Any], list[Any]]:
        observed.append(
            {
                "refs": list(refs),
                "num_returns": num_returns,
                "timeout": timeout,
                "fetch_local": fetch_local,
            }
        )
        ready = [ref for ref in refs if ref == "done"]
        unready = [ref for ref in refs if ref != "done"]
        return ready, unready

    ray = SimpleNamespace(wait=wait)
    result = select_unready_ray_refs(ray, ["done", "pending-a", "done", "pending-b", "pending-a"])

    assert result == ["pending-a", "pending-b"]
    assert observed == [
        {
            "refs": ["done", "pending-a", "pending-b"],
            "num_returns": 3,
            "timeout": 0,
            "fetch_local": False,
        }
    ]


def test_select_unready_ray_refs_deduplicates_unhashable_refs() -> None:
    ref_a = {"id": "a"}
    ref_b = {"id": "b"}

    def wait(
        refs: list[Any],
        *,
        num_returns: int,
        timeout: float,
        fetch_local: bool,
    ) -> tuple[list[Any], list[Any]]:
        assert num_returns == 2
        assert timeout == 0
        assert fetch_local is False
        return [ref_a], [ref_b]

    ray = SimpleNamespace(wait=wait)
    assert select_unready_ray_refs(ray, [ref_a, ref_b, {"id": "a"}]) == [ref_b]


def test_select_unready_ray_refs_falls_back_when_wait_omits_fetch_local() -> None:
    def wait(refs: list[Any], *, num_returns: int, timeout: float) -> tuple[list[Any], list[Any]]:
        assert num_returns == len(refs)
        assert timeout == 0
        return refs[:1], refs[1:]

    ray = SimpleNamespace(wait=wait)
    assert select_unready_ray_refs(ray, ["ready", "running"]) == ["running"]
    assert is_ray_ref_ready(ray, "ready") is True


def test_select_unready_ray_refs_falls_back_to_all_unique_refs_when_wait_unavailable_or_fails() -> (
    None
):
    no_wait_ray = SimpleNamespace()
    assert select_unready_ray_refs(no_wait_ray, ["a", "a", "b"]) == ["a", "b"]

    minimal_wait_ray = SimpleNamespace(wait=lambda refs, *, num_returns: (refs[:1], refs[1:]))
    assert select_unready_ray_refs(minimal_wait_ray, ["a", "b"]) == ["a", "b"]

    broken_wait_ray = SimpleNamespace(
        wait=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ray disconnected"))
    )
    assert select_unready_ray_refs(broken_wait_ray, ["a", "b"]) == ["a", "b"]
    assert is_ray_ref_ready(broken_wait_ray, "a") is False


def test_cancel_unready_ray_refs_skips_ready_and_counts_cancel_errors() -> None:
    cancelled: list[tuple[Any, bool, bool]] = []

    def wait(
        refs: list[Any],
        *,
        num_returns: int,
        timeout: float,
        fetch_local: bool,
    ) -> tuple[list[Any], list[Any]]:
        assert num_returns == len(refs)
        assert timeout == 0
        assert fetch_local is False
        return [ref for ref in refs if ref == "done"], [ref for ref in refs if ref != "done"]

    def cancel(ref: Any, *, force: bool, recursive: bool) -> None:
        cancelled.append((ref, force, recursive))
        if ref == "flaky":
            raise RuntimeError("cancel rpc failed")

    ray = SimpleNamespace(wait=wait, cancel=cancel)
    failures = cancel_unready_ray_refs(
        ray,
        ["done", "flaky", "running", "flaky"],
        force=False,
        recursive=True,
    )

    assert failures == 1
    assert cancelled == [("flaky", False, True), ("running", False, True)]
