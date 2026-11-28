"""Helpers for cancelling only unready Ray ObjectRefs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def _deduplicate_refs(refs: Sequence[Any]) -> list[Any]:
    unique_refs: list[Any] = []
    seen: set[Any] = set()
    unhashable: list[Any] = []
    for ref in refs:
        try:
            if ref in seen:
                continue
            seen.add(ref)
        except TypeError:
            if any(existing == ref for existing in unhashable):
                continue
            unhashable.append(ref)
        unique_refs.append(ref)
    return unique_refs


def select_unready_ray_refs(ray_module: Any, refs: Sequence[Any]) -> list[Any]:
    """Return unique refs that are not yet ready in Ray.

    Ray 2.58-2.60 can abort the driver with ``task_manager.cc:1329``
    (``Check failed: it != submissible_tasks_.end() Tried to complete task
    that was not pending``) if ``ray.cancel()`` is called on an ``ObjectRef``
    whose return value or error was just published to the in-memory store by
    ``HandleTaskReturn()`` before ``CompletePendingTask()`` removes the task
    from ``submissible_tasks_``.

    Probing readiness with a non-blocking, zero-fetch ``ray.wait`` excludes
    already-completed and already-failed refs before cancellation without
    pulling object payloads into the caller process.
    """
    unique_refs = _deduplicate_refs(refs)
    if not unique_refs:
        return []
    wait: Any = getattr(ray_module, "wait", None)
    if not callable(wait):
        return unique_refs
    try:
        _, unready = wait(
            unique_refs,
            num_returns=len(unique_refs),
            timeout=0,
            fetch_local=False,
        )
    except TypeError:
        try:
            _, unready = wait(
                unique_refs,
                num_returns=len(unique_refs),
                timeout=0,
            )
        except BaseException:
            return unique_refs
    except BaseException:
        return unique_refs
    return list(unready)


def is_ray_ref_ready(ray_module: Any, ref: Any) -> bool:
    """Return True when a non-blocking readiness check reports ``ref`` ready."""
    return not select_unready_ray_refs(ray_module, (ref,))


def cancel_unready_ray_refs(
    ray_module: Any,
    refs: Sequence[Any],
    *,
    force: bool = False,
    recursive: bool = True,
) -> int:
    """Request best-effort cancellation for still-unready refs only.

    Returns the number of cancellation calls that raised an exception so
    callers can emit bounded diagnostics without masking the primary error.
    """
    unready = select_unready_ray_refs(ray_module, refs)
    failed_cancellations = 0
    for ref in unready:
        try:
            ray_module.cancel(ref, force=force, recursive=recursive)
        except BaseException:
            failed_cancellations += 1
    return failed_cancellations
