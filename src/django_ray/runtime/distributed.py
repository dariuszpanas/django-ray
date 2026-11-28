"""Utilities for distributed computing within Django-Ray tasks.

This module provides helpers for tasks that need to leverage Ray's
distributed computing capabilities (parallel execution across the cluster).

The key insight is that when a task runs on a Ray worker, it CAN use Ray
APIs to spawn additional parallel work - but it needs to be done carefully.

Example:
    from django_ray.runtime.distributed import parallel_map, is_ray_available

    @task(queue_name="default")
    def distributed_search(pattern: str, data_sources: list[str]) -> dict:
        if is_ray_available():
            # Run search across cluster in parallel
            results = parallel_map(search_single_source, data_sources, pattern=pattern)
        else:
            # Fallback to sequential execution
            results = [search_single_source(source, pattern=pattern) for source in data_sources]
        return aggregate_results(results)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import threading
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import uuid4

from django_ray.runtime.ray_cancel import cancel_unready_ray_refs

if TYPE_CHECKING:
    from django_ray.execution_codec import (
        ExecutionIdentity,
        NestedExecutionBoundaryKind,
        _PreparedNestedDistributedRequest,
    )

T = TypeVar("T")
R = TypeVar("R")

logger = logging.getLogger(__name__)

# Track if Django has been bootstrapped in this process (guarded by a lock so
# multi-threaded Ray workers never race django.setup()).
_django_bootstrapped = False
_django_bootstrap_lock = threading.Lock()

# Keep one remote function definition per (Ray runtime session, helper kind).
# Keying by session identity ensures wrappers are automatically recreated after
# ray.shutdown() / reconnects instead of keeping stale ClientRemoteFunc handles.
_cached_remotes: dict[tuple[Any, str], Any] = {}
_cached_remotes_lock = threading.Lock()
_parallel_map_remote_cached: Any = None
_parallel_starmap_remote_cached: Any = None
_scatter_gather_remote_cached: Any = None


def reset_cached_remotes() -> None:
    """Clear cached Ray remote wrappers (e.g., on shutdown/reconnect or tests)."""
    global _parallel_map_remote_cached
    global _parallel_starmap_remote_cached
    global _scatter_gather_remote_cached
    with _cached_remotes_lock:
        _cached_remotes.clear()
        _parallel_map_remote_cached = None
        _parallel_starmap_remote_cached = None
        _scatter_gather_remote_cached = None


def _close_old_django_connections() -> None:
    """Close stale/expired Django DB connections in reused Ray worker processes."""
    try:
        from django.apps import apps

        if not apps.ready:
            return
        from django.db import close_old_connections

        close_old_connections()
    except Exception:
        pass


def _invoke_callable(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke a sync or async callable cleanly at the synchronous worker boundary."""
    if inspect.iscoroutinefunction(fn):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "django-ray cannot execute a coroutine callable from a thread "
                "that already has a running event loop"
            )
        return asyncio.run(fn(*args, **kwargs))

    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            close = getattr(result, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            raise RuntimeError(
                "django-ray cannot await a coroutine result from a thread "
                "that already has a running event loop"
            )
        return asyncio.run(result)
    return result


@dataclass(frozen=True, slots=True)
class _RemoteCalls:
    """Prepare exactly the indexed call requested by the submission window."""

    count: int
    prepare: Callable[[int], tuple[Any, ...]]

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int) -> tuple[Any, ...]:
        if not 0 <= index < self.count:
            raise IndexError(index)
        return self.prepare(index)

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        for index in range(self.count):
            yield self[index]


@dataclass(slots=True)
class _NestedCallableBindingCache:
    """Retain at most one callable, with no process-global payload cache."""

    serialized: bytes | None = None
    binding: str | None = None

    def get(self, serialized: bytes) -> str:
        if serialized is self.serialized and self.binding is not None:
            return self.binding
        from django_ray.execution_codec import nested_callable_digest

        binding = nested_callable_digest(serialized)
        self.serialized = serialized
        self.binding = binding
        return binding


@dataclass(frozen=True, slots=True)
class _NestedDistributedOperation:
    """Strict outer context shared by every item in one helper invocation."""

    outer_identity: ExecutionIdentity
    execution_protocol_version: int
    boundary_kind: NestedExecutionBoundaryKind
    operation_id: str
    runtime_env_plan_identity: dict[str, Any]
    runtime_env_plan_digest: str
    runtime_env_transport_digest: str
    encoder: _PreparedNestedDistributedRequest
    callable_cache: _NestedCallableBindingCache = field(default_factory=_NestedCallableBindingCache)


def _validate_resources(num_cpus: float, num_gpus: float) -> None:
    """Validate Ray resource requests before trying to submit work."""
    for name, value in (("num_cpus", num_cpus), ("num_gpus", num_gpus)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a finite non-negative number")
        if not math.isfinite(float(value)) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number")


def _validate_max_concurrency(max_concurrency: int | None) -> None:
    """Validate an optional bounded submission window."""
    if max_concurrency is None:
        return
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
        raise TypeError("max_concurrency must be an integer or None")
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")


def _materialize_items(items: object, name: str) -> list[Any]:
    """Accept list/tuple inputs while rejecting scalar and string values."""
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise TypeError(f"{name} must be a list or tuple")
    return list(items)


def _validate_callable(func: object, name: str = "func") -> None:
    if not callable(func):
        raise TypeError(f"{name} must be callable")


def _strict_nested_operation(
    boundary_kind: NestedExecutionBoundaryKind,
) -> _NestedDistributedOperation | None:
    """Capture one strict outer context, or retain the released legacy path."""
    from django_ray.runtime.context import (
        get_current_task_context,
        nested_execution_identity,
        require_strict_task_execution_context,
    )

    current = get_current_task_context()
    if current is None or getattr(current, "strict_execution_request", False) is False:
        return None

    current = require_strict_task_execution_context(current)
    from django_ray.execution_codec import (
        NestedCallableBindingKind,
        NestedDistributedBoundaryIdentity,
        NestedExecutionRequest,
        _prepare_nested_distributed_request,
        nested_runtime_env_digests,
    )

    outer_identity, execution_protocol_version = nested_execution_identity(current)
    runtime_env_plan_identity = cast(dict[str, Any], current.runtime_env_plan_identity)
    plan_digest, transport_digest = nested_runtime_env_digests(runtime_env_plan_identity)
    operation_id = uuid4().hex
    encoder = _prepare_nested_distributed_request(
        NestedExecutionRequest(
            outer_identity=outer_identity,
            execution_protocol_version=execution_protocol_version,
            boundary_kind=boundary_kind,
            boundary_identity=NestedDistributedBoundaryIdentity(operation_id, 0),
            callable_binding_kind=NestedCallableBindingKind.DIGEST,
            callable_binding="sha256:" + "0" * 64,
            runtime_env_plan_identity=runtime_env_plan_identity,
            runtime_env_plan_digest=plan_digest,
            runtime_env_transport_digest=transport_digest,
        )
    )
    return _NestedDistributedOperation(
        outer_identity=outer_identity,
        execution_protocol_version=execution_protocol_version,
        boundary_kind=boundary_kind,
        operation_id=operation_id,
        runtime_env_plan_identity=runtime_env_plan_identity,
        runtime_env_plan_digest=plan_digest,
        runtime_env_transport_digest=transport_digest,
        encoder=encoder,
    )


def _nested_distributed_request(
    operation: _NestedDistributedOperation,
    pickled_func: bytes,
    item_index: int,
) -> tuple[str, int, str, int, int, int, str, int, str, str]:
    """Bind one still-opaque callable to an exact distributed leaf identity."""
    serialized = operation.encoder.encode(
        item_index=item_index,
        callable_binding=operation.callable_cache.get(pickled_func),
    )
    identity = operation.outer_identity
    return (
        serialized,
        identity.task_execution_pk,
        identity.task_id,
        identity.attempt_number,
        identity.execution_generation,
        operation.execution_protocol_version,
        operation.operation_id,
        item_index,
        operation.runtime_env_plan_digest,
        operation.runtime_env_transport_digest,
    )


def _is_strict_nested_request_enforced() -> bool:
    """Return True when remote workers must reject unauthenticated legacy leaf calls.

    Remote Ray workers execute in fresh processes where ``get_current_task_context()``
    is initially ``None`` until ``durable_task_execution(...)`` is entered. Enforce
    strict nested execution requests whenever the worker environment, Django
    settings, or active Ray Job metadata mandates the strict request boundary.
    """
    require_env = os.environ.get("DJANGO_RAY_REQUIRE_STRICT_NESTED_REQUEST")
    if isinstance(require_env, str) and require_env.strip().lower() in {"1", "true", "yes", "on"}:
        return True

    allow_legacy_env = os.environ.get("DJANGO_RAY_ALLOW_LEGACY_NESTED_CALLABLES")
    if isinstance(allow_legacy_env, str) and allow_legacy_env.strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return True

    try:
        from collections.abc import Mapping

        from django.conf import settings as django_settings

        if django_settings.configured:
            config = getattr(django_settings, "DJANGO_RAY", None)
            if isinstance(config, Mapping) and config.get("REQUIRE_STRICT_NESTED_REQUEST") is True:
                return True
    except Exception:
        pass

    raw_job_config = os.environ.get("RAY_JOB_CONFIG_JSON_ENV_VAR")
    if isinstance(raw_job_config, str) and raw_job_config.strip():
        try:
            from django_ray.ray_job_protocol import load_ray_job_request_expectation

            if load_ray_job_request_expectation(raw_job_config) is not None:
                return True
        except Exception:
            # Malformed strict job config must fail closed rather than fall back.
            return True

    return False


def _assert_active_leaf_context(*, strict_expected: bool) -> None:
    """Verify the leaf execution context invariant immediately before unpickling."""
    from django_ray.execution_codec import (
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
    )
    from django_ray.runtime.context import get_current_task_context

    current = get_current_task_context()
    if strict_expected:
        if current is None or getattr(current, "strict_execution_request", False) is not True:
            raise NestedExecutionRequestRejected(
                NestedExecutionRequestRejection.MISSING_CONTEXT
            ) from None
    elif current is not None or _is_strict_nested_request_enforced():
        raise NestedExecutionRequestRejected(
            NestedExecutionRequestRejection.INVALID_VERSIONED
        ) from None


@contextmanager
def _nested_distributed_leaf_execution(
    pickled_func: bytes,
    nested_request: str | None,
    expected_task_execution_pk: int | None,
    expected_task_id: str | None,
    expected_attempt_number: int | None,
    expected_execution_generation: int | None,
    expected_execution_protocol_version: int | None,
    expected_operation_id: str | None,
    expected_item_index: int | None,
    expected_runtime_env_plan_digest: str | None,
    expected_runtime_env_transport_digest: str | None,
    *,
    boundary_kind: NestedExecutionBoundaryKind,
) -> Generator[None, None, None]:
    """Validate strict controls before loading the still-opaque callable bytes.

    Ray has already deserialized this handler's ordinary bootstrap and item
    arguments. This boundary deliberately protects the explicit callable pickle
    from ``pickle.loads`` and prevents application setup or invocation on reject.
    """
    expected_controls = (
        expected_task_execution_pk,
        expected_task_id,
        expected_attempt_number,
        expected_execution_generation,
        expected_execution_protocol_version,
        expected_operation_id,
        expected_item_index,
        expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest,
    )
    if nested_request is None and all(control is None for control in expected_controls):
        _assert_active_leaf_context(strict_expected=False)
        yield
        return

    from django_ray.execution_codec import (
        ExecutionIdentity,
        NestedCallableBindingKind,
        NestedDistributedBoundaryIdentity,
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
        decode_nested_execution_request,
        nested_callable_digest,
    )

    if type(nested_request) is not str or any(control is None for control in expected_controls):
        raise NestedExecutionRequestRejected(
            NestedExecutionRequestRejection.INVALID_VERSIONED
        ) from None

    expected_outer_identity = ExecutionIdentity(
        task_execution_pk=cast(int, expected_task_execution_pk),
        task_id=cast(str, expected_task_id),
        attempt_number=cast(int, expected_attempt_number),
        execution_generation=cast(int, expected_execution_generation),
    )
    expected_boundary_identity = NestedDistributedBoundaryIdentity(
        operation_id=cast(str, expected_operation_id),
        item_index=cast(int, expected_item_index),
    )
    # Compute the SHA-256 callable digest once and pass it straight into
    # decode_nested_execution_request(), avoiding a redundant second hash pass.
    callable_binding = nested_callable_digest(pickled_func)
    decoded = decode_nested_execution_request(
        nested_request,
        expected_outer_identity=expected_outer_identity,
        expected_execution_protocol_version=expected_execution_protocol_version,
        expected_boundary_kind=boundary_kind,
        expected_boundary_identity=expected_boundary_identity,
        expected_callable_binding_kind=NestedCallableBindingKind.DIGEST,
        expected_callable_binding=callable_binding,
        expected_runtime_env_plan_digest=expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest=expected_runtime_env_transport_digest,
    )
    if (
        decoded.callable_binding_kind is not NestedCallableBindingKind.DIGEST
        or decoded.callable_binding != callable_binding
    ):
        raise NestedExecutionRequestRejected(
            NestedExecutionRequestRejection.CALLABLE_MISMATCH
        ) from None

    from django_ray.runtime.compiled_graph import CompiledGraphSubmissionTransport
    from django_ray.runtime.context import durable_task_execution

    identity = decoded.outer_identity
    runtime_env_profile = decoded.runtime_env_plan_identity.get("profile")
    with durable_task_execution(
        identity.task_execution_pk,
        task_id=identity.task_id,
        execution_protocol_version=decoded.execution_protocol_version,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        runtime_env_profile=runtime_env_profile,
        runtime_env_plan_identity=decoded.runtime_env_plan_identity,
        compiled_graph_submission_transport=(
            CompiledGraphSubmissionTransport.DIRECT_RAY_CORE.value
        ),
        strict_execution_request=True,
    ):
        _assert_active_leaf_context(strict_expected=True)
        yield


def _parallel_map_remote(
    pickled_func: bytes,
    item: Any,
    kwargs: dict[str, Any],
    nested_request: str | None = None,
    expected_task_execution_pk: int | None = None,
    expected_task_id: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_execution_protocol_version: int | None = None,
    expected_operation_id: str | None = None,
    expected_item_index: int | None = None,
    expected_runtime_env_plan_digest: str | None = None,
    expected_runtime_env_transport_digest: str | None = None,
) -> Any:
    """Execute one ``parallel_map`` item on a Ray worker."""
    from django_ray.execution_codec import NestedExecutionBoundaryKind

    with _nested_distributed_leaf_execution(
        pickled_func,
        nested_request,
        expected_task_execution_pk,
        expected_task_id,
        expected_attempt_number,
        expected_execution_generation,
        expected_execution_protocol_version,
        expected_operation_id,
        expected_item_index,
        expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest,
        boundary_kind=NestedExecutionBoundaryKind.DISTRIBUTED_MAP,
    ):
        import pickle

        _assert_active_leaf_context(strict_expected=nested_request is not None)
        _bootstrap_django_if_needed()
        _close_old_django_connections()
        try:
            fn = pickle.loads(pickled_func)
            return _invoke_callable(fn, item, **kwargs)
        finally:
            _close_old_django_connections()


def _parallel_starmap_remote(
    pickled_func: bytes,
    args: tuple[Any, ...],
    nested_request: str | None = None,
    expected_task_execution_pk: int | None = None,
    expected_task_id: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_execution_protocol_version: int | None = None,
    expected_operation_id: str | None = None,
    expected_item_index: int | None = None,
    expected_runtime_env_plan_digest: str | None = None,
    expected_runtime_env_transport_digest: str | None = None,
) -> Any:
    """Execute one ``parallel_starmap`` item on a Ray worker."""
    from django_ray.execution_codec import NestedExecutionBoundaryKind

    with _nested_distributed_leaf_execution(
        pickled_func,
        nested_request,
        expected_task_execution_pk,
        expected_task_id,
        expected_attempt_number,
        expected_execution_generation,
        expected_execution_protocol_version,
        expected_operation_id,
        expected_item_index,
        expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest,
        boundary_kind=NestedExecutionBoundaryKind.DISTRIBUTED_STARMAP,
    ):
        import pickle

        _assert_active_leaf_context(strict_expected=nested_request is not None)
        _bootstrap_django_if_needed()
        _close_old_django_connections()
        try:
            fn = pickle.loads(pickled_func)
            return _invoke_callable(fn, *args)
        finally:
            _close_old_django_connections()


def _scatter_gather_remote(
    pickled_func: bytes,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    nested_request: str | None = None,
    expected_task_execution_pk: int | None = None,
    expected_task_id: str | None = None,
    expected_attempt_number: int | None = None,
    expected_execution_generation: int | None = None,
    expected_execution_protocol_version: int | None = None,
    expected_operation_id: str | None = None,
    expected_item_index: int | None = None,
    expected_runtime_env_plan_digest: str | None = None,
    expected_runtime_env_transport_digest: str | None = None,
) -> Any:
    """Execute one ``scatter_gather`` item on a Ray worker."""
    from django_ray.execution_codec import NestedExecutionBoundaryKind

    with _nested_distributed_leaf_execution(
        pickled_func,
        nested_request,
        expected_task_execution_pk,
        expected_task_id,
        expected_attempt_number,
        expected_execution_generation,
        expected_execution_protocol_version,
        expected_operation_id,
        expected_item_index,
        expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest,
        boundary_kind=NestedExecutionBoundaryKind.DISTRIBUTED_SCATTER,
    ):
        import pickle

        _assert_active_leaf_context(strict_expected=nested_request is not None)
        _bootstrap_django_if_needed()
        _close_old_django_connections()
        try:
            fn = pickle.loads(pickled_func)
            return _invoke_callable(fn, *args, **kwargs)
        finally:
            _close_old_django_connections()


def _ray_runtime_session_key(ray_module: Any) -> tuple[Any, ...]:
    """Build a reconnect-safe cache key for the active Ray runtime/client session."""
    session_token: Any = None
    get_ctx = getattr(ray_module, "get_runtime_context", None)
    if callable(get_ctx):
        try:
            ctx = get_ctx()
            job_id = getattr(ctx, "get_job_id", None)
            if callable(job_id):
                session_token = ("job", job_id())
        except Exception:
            session_token = None
    if session_token is None:
        private = getattr(ray_module, "_private", None)
        worker_mod = getattr(private, "worker", None) if private is not None else None
        global_worker = (
            getattr(worker_mod, "global_worker", None) if worker_mod is not None else None
        )
        node = getattr(global_worker, "node", None) if global_worker is not None else None
        session_name = getattr(node, "session_name", None) if node is not None else None
        if session_name is not None:
            session_token = ("node", session_name)
    client_ctx = getattr(ray_module, "_client_context", None)
    client_id = id(client_ctx) if client_ctx is not None else None
    return (id(ray_module), session_token, client_id)


def _get_cached_remote(kind: str) -> Any:
    """Return a reconnect-safe cached Ray wrapper for one of the distributed helpers."""
    global _parallel_map_remote_cached
    global _parallel_starmap_remote_cached
    global _scatter_gather_remote_cached

    import ray

    target_by_kind = {
        "map": _parallel_map_remote,
        "starmap": _parallel_starmap_remote,
        "scatter_gather": _scatter_gather_remote,
    }
    target = target_by_kind.get(kind)
    if target is None:
        raise ValueError(f"unknown distributed helper: {kind}")

    session_key = _ray_runtime_session_key(ray)
    cache_key = (session_key, kind)
    with _cached_remotes_lock:
        cached = _cached_remotes.get(cache_key)
        if cached is not None:
            return cached
        # Evict entries from prior disconnected Ray sessions so stale handles
        # do not accumulate across reconnects.
        stale_keys = [key for key in _cached_remotes if key[0] != session_key]
        for stale_key in stale_keys:
            _cached_remotes.pop(stale_key, None)
        wrapped = ray.remote(target)
        _cached_remotes[cache_key] = wrapped
        if kind == "map":
            _parallel_map_remote_cached = wrapped
        elif kind == "starmap":
            _parallel_starmap_remote_cached = wrapped
        else:
            _scatter_gather_remote_cached = wrapped
        return wrapped


def _collect_remote_results(
    ray: Any,
    remote: Any,
    calls: list[tuple[Any, ...]] | _RemoteCalls,
    max_concurrency: int | None,
) -> list[Any]:
    """Preserve order, fail fast in bounded & unbounded modes, and cancel on error.

    Uses O(1) swap-pop tracking for pending refs plus opportunistic batch
    draining of already-ready refs, avoiding O(N * W) linear scans. Already-ready
    refs are excluded before cancellation so a just-completed or just-failed
    task does not trigger Ray's concurrent completion/cancellation crash.
    """
    total = len(calls)
    if total == 0:
        return []

    window = total if max_concurrency is None else min(max_concurrency, total)
    results: list[Any] = [None] * total
    pending_refs: list[Any] = []
    ref_to_pos: dict[Any, int] = {}
    ref_to_index: dict[Any, int] = {}

    def _add_pending(item_index: int, ref: Any) -> None:
        ref_to_pos[ref] = len(pending_refs)
        ref_to_index[ref] = item_index
        pending_refs.append(ref)

    def _pop_pending(ref: Any) -> int:
        pos = ref_to_pos.pop(ref)
        item_index = ref_to_index.pop(ref)
        last = pending_refs.pop()
        if pos < len(pending_refs):
            pending_refs[pos] = last
            ref_to_pos[last] = pos
        return item_index

    next_index = 0
    try:
        for index in range(window):
            _add_pending(index, remote.remote(*calls[index]))
            next_index = index + 1

        while pending_refs:
            ready, _ = ray.wait(pending_refs, num_returns=1)
            batch = list(ready)
            # Opportunistically drain already-ready refs in O(1) extra RPC when
            # many tasks finish concurrently in large fan-outs.
            if len(pending_refs) - len(batch) >= 64:
                remaining_for_drain = [ref for ref in pending_refs if ref not in set(batch)]
                try:
                    extra_ready, _ = ray.wait(
                        remaining_for_drain,
                        num_returns=len(remaining_for_drain),
                        timeout=0,
                        fetch_local=False,
                    )
                    batch.extend(extra_ready)
                except TypeError:
                    pass

            for ready_ref in batch:
                if ready_ref not in ref_to_index:
                    continue
                ready_index = _pop_pending(ready_ref)
                # ray.wait() already proved ready_ref is terminal (value or error),
                # so it is excluded from cleanup cancellation before ray.get().
                results[ready_index] = ray.get(ready_ref)
                if next_index < total:
                    _add_pending(next_index, remote.remote(*calls[next_index]))
                    next_index += 1

        return results
    except BaseException:
        failed_cancellations = cancel_unready_ray_refs(
            ray,
            list(pending_refs),
            force=False,
            recursive=True,
        )
        if failed_cancellations:
            with suppress(BaseException):
                logger.warning(
                    "Could not request cancellation for %d distributed children",
                    failed_cancellations,
                )
        raise


def _bootstrap_django_if_needed() -> None:
    """Bootstrap Django in a Ray worker process safely under a lock.

    Only latches ``_django_bootstrapped = True`` once Django apps are actually
    ready, so a missing ``DJANGO_SETTINGS_MODULE`` never permanently disables
    later bootstrap attempts.
    """
    global _django_bootstrapped

    if _django_bootstrapped:
        return

    with _django_bootstrap_lock:
        if _django_bootstrapped:
            return

        import django
        from django.apps import apps

        if apps.ready:
            _django_bootstrapped = True
            return

        settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
        if settings_module:
            django.setup()
            _django_bootstrapped = True


def _maybe_put_shared(ray_module: Any, value: Any, *, item_count: int) -> Any:
    """Store shared payloads in Ray object store once for multi-item fan-outs."""
    if item_count <= 1:
        return value
    put_fn = getattr(ray_module, "put", None)
    if callable(put_fn):
        return put_fn(value)
    return value


def is_ray_available() -> bool:
    """Check if Ray is available and initialized, auto-connecting in Ray Job drivers.

    Returns:
        True if Ray can be used for distributed computing.
    """
    try:
        import ray
    except ImportError:
        return False

    if ray.is_initialized():
        return True

    from django_ray.runtime.context import get_current_task_context

    task_context = get_current_task_context()
    if task_context is not None and getattr(task_context, "ray_job_driver", False):
        try:
            ray.init(address="auto", ignore_reinit_error=True)
        except Exception:
            return False
        return bool(ray.is_initialized())
    return False


def get_ray_resources() -> dict[str, Any]:
    """Get available Ray cluster resources.

    Returns:
        Dictionary of available resources, or empty dict if Ray not available.
    """
    if not is_ray_available():
        return {}

    import ray

    return dict(ray.cluster_resources())


def parallel_map[T, R](
    func: Callable[..., R],
    items: list[T],
    *,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    max_concurrency: int | None = None,
    **kwargs: Any,
) -> list[R]:
    """Execute a function over items in parallel using Ray.

    This is the recommended way to do parallel processing within a Django-Ray task.
    If Ray is not available, falls back to sequential execution.

    Args:
        func: Function to apply to each item. Must be picklable.
        items: List of items to process.
        num_cpus: CPUs per task (default: 1.0).
        num_gpus: GPUs per task (default: 0.0).
        max_concurrency: Maximum concurrent tasks (default: all at once).
        **kwargs: Additional keyword arguments passed to func.

    Returns:
        List of results in the same order as items.

    Example:
        def process_item(item, multiplier=1):
            return item * multiplier

        results = parallel_map(process_item, [1, 2, 3, 4, 5], multiplier=10)
        # Returns [10, 20, 30, 40, 50]
    """
    _validate_callable(func)
    _validate_resources(num_cpus, num_gpus)
    _validate_max_concurrency(max_concurrency)
    materialized_items = _materialize_items(items, "items")
    if not materialized_items:
        return []

    if not is_ray_available():
        # Fallback to sequential execution
        return [_invoke_callable(func, item, **kwargs) for item in materialized_items]

    from django_ray.execution_codec import NestedExecutionBoundaryKind

    operation = _strict_nested_operation(NestedExecutionBoundaryKind.DISTRIBUTED_MAP)

    # Pickle the function once and place shared payloads in Ray's object store
    # once for multi-item fan-outs.
    import pickle

    import ray

    pickled_func = pickle.dumps(func)
    shared_func = _maybe_put_shared(ray, pickled_func, item_count=len(materialized_items))
    shared_kwargs = (
        _maybe_put_shared(ray, kwargs, item_count=len(materialized_items)) if kwargs else kwargs
    )
    remote = _get_cached_remote("map").options(num_cpus=num_cpus, num_gpus=num_gpus)

    def prepare(index: int) -> tuple[Any, ...]:
        call = (shared_func, materialized_items[index], shared_kwargs)
        if operation is None:
            return call
        return (*call, *_nested_distributed_request(operation, pickled_func, index))

    calls = _RemoteCalls(len(materialized_items), prepare)
    return _collect_remote_results(ray, remote, calls, max_concurrency)


def parallel_starmap[R](
    func: Callable[..., R],
    items: list[tuple[Any, ...]],
    *,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    max_concurrency: int | None = None,
) -> list[R]:
    """Execute a function over items in parallel, unpacking arguments.

    Like parallel_map but each item is a tuple of arguments.

    Args:
        func: Function to apply. Must be picklable.
        items: List of argument tuples.
        num_cpus: CPUs per task.
        num_gpus: GPUs per task.
        max_concurrency: Maximum concurrent tasks.

    Returns:
        List of results in the same order as items.

    Example:
        def add(a, b):
            return a + b

        results = parallel_starmap(add, [(1, 2), (3, 4), (5, 6)])
        # Returns [3, 7, 11]
    """
    _validate_callable(func)
    _validate_resources(num_cpus, num_gpus)
    _validate_max_concurrency(max_concurrency)
    materialized_items = _materialize_items(items, "items")
    for index, args in enumerate(materialized_items):
        if not isinstance(args, tuple):
            raise TypeError(f"items[{index}] must be a tuple of positional arguments")
    if not materialized_items:
        return []

    if not is_ray_available():
        return [_invoke_callable(func, *args) for args in materialized_items]

    from django_ray.execution_codec import NestedExecutionBoundaryKind

    operation = _strict_nested_operation(NestedExecutionBoundaryKind.DISTRIBUTED_STARMAP)

    import pickle

    import ray

    # Pickle the function once and store in Ray object store for multi-item fan-outs
    pickled_func = pickle.dumps(func)
    shared_func = _maybe_put_shared(ray, pickled_func, item_count=len(materialized_items))
    remote = _get_cached_remote("starmap").options(num_cpus=num_cpus, num_gpus=num_gpus)

    def prepare(index: int) -> tuple[Any, ...]:
        call = (shared_func, materialized_items[index])
        if operation is None:
            return call
        return (*call, *_nested_distributed_request(operation, pickled_func, index))

    calls = _RemoteCalls(len(materialized_items), prepare)
    return _collect_remote_results(ray, remote, calls, max_concurrency)


def scatter_gather[R](
    tasks: list[tuple[Callable[..., R], tuple[Any, ...], dict[str, Any]]],
    *,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    max_concurrency: int | None = None,
) -> list[R]:
    """Execute multiple different functions in parallel (scatter-gather pattern).

    Useful when you have heterogeneous work to parallelize.

    Args:
        tasks: List of (function, args, kwargs) tuples.
        num_cpus: CPUs per task.
        num_gpus: GPUs per task.
        max_concurrency: Maximum concurrent tasks (default: all at once).

    Returns:
        List of results in the same order as tasks.

    Example:
        def fetch_users(): ...
        def fetch_orders(): ...
        def fetch_products(): ...

        users, orders, products = scatter_gather([
            (fetch_users, (), {}),
            (fetch_orders, (), {}),
            (fetch_products, (), {}),
        ])
    """
    _validate_resources(num_cpus, num_gpus)
    _validate_max_concurrency(max_concurrency)
    materialized_tasks = _materialize_items(tasks, "tasks")
    for index, task in enumerate(materialized_tasks):
        if not isinstance(task, tuple) or len(task) != 3:
            raise TypeError(f"tasks[{index}] must be a (callable, tuple, dict) tuple")
        func, args, kwargs = task
        _validate_callable(func, f"tasks[{index}][0]")
        if not isinstance(args, tuple):
            raise TypeError(f"tasks[{index}][1] must be a tuple of positional arguments")
        if not isinstance(kwargs, dict):
            raise TypeError(f"tasks[{index}][2] must be a dictionary of keyword arguments")
    if not materialized_tasks:
        return []

    if not is_ray_available():
        return [
            _invoke_callable(func, *args, **kwargs) for func, args, kwargs in materialized_tasks
        ]

    from django_ray.execution_codec import NestedExecutionBoundaryKind

    operation = _strict_nested_operation(NestedExecutionBoundaryKind.DISTRIBUTED_SCATTER)

    import pickle

    import ray

    remote = _get_cached_remote("scatter_gather").options(num_cpus=num_cpus, num_gpus=num_gpus)

    def prepare(index: int) -> tuple[Any, ...]:
        func, args, kwargs = materialized_tasks[index]
        pickled_func = pickle.dumps(func)
        if operation is None:
            return pickled_func, args, kwargs
        return (
            pickled_func,
            args,
            kwargs,
            *_nested_distributed_request(operation, pickled_func, index),
        )

    calls = _RemoteCalls(len(materialized_tasks), prepare)
    return _collect_remote_results(ray, remote, calls, max_concurrency)


def get_num_workers() -> int:
    """Get the number of available Ray worker nodes.

    Returns:
        Number of worker nodes, or 1 if Ray not available.
    """
    if not is_ray_available():
        return 1

    import ray

    resources = ray.cluster_resources()
    # Count nodes by looking for node:* resources
    nodes = sum(
        1 for k in resources if k.startswith("node:") and not k.endswith("__internal_head__")
    )
    return max(1, nodes)


def get_total_cpus() -> float:
    """Get total CPUs available in the Ray cluster.

    Returns:
        Total CPUs, or os.cpu_count() if Ray not available.
    """
    if not is_ray_available():
        return float(os.cpu_count() or 1)

    import ray

    resources = ray.cluster_resources()
    return resources.get("CPU", 1.0)
