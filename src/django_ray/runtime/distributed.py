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

import math
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import uuid4

if TYPE_CHECKING:
    from django_ray.execution_codec import (
        ExecutionIdentity,
        NestedExecutionBoundaryKind,
    )

T = TypeVar("T")
R = TypeVar("R")

# Track if Django has been bootstrapped in this process
_django_bootstrapped = False

# Keep one remote function definition per helper process.  Re-decorating a nested
# function for every invocation causes Ray's GCS to retain a new definition each
# time, which is particularly expensive for tasks that fan out repeatedly.
_parallel_map_remote_cached: Any = None
_parallel_starmap_remote_cached: Any = None
_scatter_gather_remote_cached: Any = None


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
    from django_ray.execution_codec import nested_runtime_env_digests

    outer_identity, execution_protocol_version = nested_execution_identity(current)
    runtime_env_plan_identity = cast(dict[str, Any], current.runtime_env_plan_identity)
    plan_digest, transport_digest = nested_runtime_env_digests(runtime_env_plan_identity)
    return _NestedDistributedOperation(
        outer_identity=outer_identity,
        execution_protocol_version=execution_protocol_version,
        boundary_kind=boundary_kind,
        operation_id=uuid4().hex,
        runtime_env_plan_identity=runtime_env_plan_identity,
        runtime_env_plan_digest=plan_digest,
        runtime_env_transport_digest=transport_digest,
    )


def _nested_distributed_request(
    operation: _NestedDistributedOperation,
    pickled_func: bytes,
    item_index: int,
) -> tuple[str, int, str, int, int, int, str, int, str, str]:
    """Bind one still-opaque callable to an exact distributed leaf identity."""
    from django_ray.execution_codec import (
        NestedCallableBindingKind,
        NestedDistributedBoundaryIdentity,
        NestedExecutionRequest,
        encode_nested_execution_request,
        nested_callable_digest,
    )

    boundary_identity = NestedDistributedBoundaryIdentity(
        operation_id=operation.operation_id,
        item_index=item_index,
    )
    request = NestedExecutionRequest(
        outer_identity=operation.outer_identity,
        execution_protocol_version=operation.execution_protocol_version,
        boundary_kind=operation.boundary_kind,
        boundary_identity=boundary_identity,
        callable_binding_kind=NestedCallableBindingKind.DIGEST,
        callable_binding=nested_callable_digest(pickled_func),
        runtime_env_plan_identity=operation.runtime_env_plan_identity,
        runtime_env_plan_digest=operation.runtime_env_plan_digest,
        runtime_env_transport_digest=operation.runtime_env_transport_digest,
    )
    identity = operation.outer_identity
    return (
        encode_nested_execution_request(request),
        identity.task_execution_pk,
        identity.task_id,
        identity.attempt_number,
        identity.execution_generation,
        operation.execution_protocol_version,
        boundary_identity.operation_id,
        boundary_identity.item_index,
        operation.runtime_env_plan_digest,
        operation.runtime_env_transport_digest,
    )


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
) -> Iterator[None]:
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
        yield
        return

    from django_ray.execution_codec import (
        ExecutionIdentity,
        NestedCallableBindingKind,
        NestedDistributedBoundaryIdentity,
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
        assert_nested_callable_binding,
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
    assert_nested_callable_binding(decoded, serialized_callable=pickled_func)

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

        _bootstrap_django_if_needed()
        fn = pickle.loads(pickled_func)
        return fn(item, **kwargs)


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

        _bootstrap_django_if_needed()
        fn = pickle.loads(pickled_func)
        return fn(*args)


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

        _bootstrap_django_if_needed()
        fn = pickle.loads(pickled_func)
        return fn(*args, **kwargs)


def _get_cached_remote(kind: str) -> Any:
    """Return a cached Ray wrapper for one of the distributed helpers."""
    global _parallel_map_remote_cached
    global _parallel_starmap_remote_cached
    global _scatter_gather_remote_cached

    import ray

    if kind == "map":
        if _parallel_map_remote_cached is None:
            _parallel_map_remote_cached = ray.remote(_parallel_map_remote)
        return _parallel_map_remote_cached
    if kind == "starmap":
        if _parallel_starmap_remote_cached is None:
            _parallel_starmap_remote_cached = ray.remote(_parallel_starmap_remote)
        return _parallel_starmap_remote_cached
    if kind == "scatter_gather":
        if _scatter_gather_remote_cached is None:
            _scatter_gather_remote_cached = ray.remote(_scatter_gather_remote)
        return _scatter_gather_remote_cached
    raise ValueError(f"unknown distributed helper: {kind}")


def _collect_remote_results(
    ray: Any,
    remote: Any,
    calls: list[tuple[Any, ...]],
    max_concurrency: int | None,
) -> list[Any]:
    """Submit calls with an optional sliding window and preserve input order."""
    if max_concurrency is None or max_concurrency >= len(calls):
        return list(ray.get([remote.remote(*call) for call in calls]))

    results: list[Any] = [None] * len(calls)
    pending: list[tuple[int, Any]] = []
    next_index = 0
    for index in range(min(max_concurrency, len(calls))):
        pending.append((index, remote.remote(*calls[index])))
        next_index = index + 1

    while pending:
        ready, _ = ray.wait([ref for _, ref in pending], num_returns=1)
        ready_ref = ready[0]
        ready_index = next(index for index, ref in pending if ref == ready_ref)
        pending = [(index, ref) for index, ref in pending if index != ready_index]
        results[ready_index] = ray.get(ready_ref)
        if next_index < len(calls):
            pending.append((next_index, remote.remote(*calls[next_index])))
            next_index += 1

    return results


def _bootstrap_django_if_needed() -> None:
    """Bootstrap Django in a Ray worker process if not already done.

    This is called automatically by parallel_map/parallel_starmap/scatter_gather
    when running on Ray workers.
    """
    global _django_bootstrapped

    if _django_bootstrapped:
        return

    import django
    from django.apps import apps

    if not apps.ready:
        settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
        if settings_module:
            django.setup()

    _django_bootstrapped = True


def is_ray_available() -> bool:
    """Check if Ray is available and initialized.

    Returns:
        True if Ray can be used for distributed computing.
    """
    try:
        import ray

        return ray.is_initialized()
    except ImportError:
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
        return [func(item, **kwargs) for item in materialized_items]

    from django_ray.execution_codec import NestedExecutionBoundaryKind

    operation = _strict_nested_operation(NestedExecutionBoundaryKind.DISTRIBUTED_MAP)

    # Pickle the function once to send to workers
    import pickle

    import ray

    pickled_func = pickle.dumps(func)
    remote = _get_cached_remote("map").options(num_cpus=num_cpus, num_gpus=num_gpus)
    if operation is None:
        calls = [(pickled_func, item, kwargs) for item in materialized_items]
    else:
        calls = [
            (
                pickled_func,
                item,
                kwargs,
                *_nested_distributed_request(operation, pickled_func, index),
            )
            for index, item in enumerate(materialized_items)
        ]
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
        return [func(*args) for args in materialized_items]

    from django_ray.execution_codec import NestedExecutionBoundaryKind

    operation = _strict_nested_operation(NestedExecutionBoundaryKind.DISTRIBUTED_STARMAP)

    import pickle

    import ray

    # Pickle the function once
    pickled_func = pickle.dumps(func)
    remote = _get_cached_remote("starmap").options(num_cpus=num_cpus, num_gpus=num_gpus)
    if operation is None:
        calls = [(pickled_func, args) for args in materialized_items]
    else:
        calls = [
            (
                pickled_func,
                args,
                *_nested_distributed_request(operation, pickled_func, index),
            )
            for index, args in enumerate(materialized_items)
        ]
    return _collect_remote_results(ray, remote, calls, max_concurrency)


def scatter_gather[R](
    tasks: list[tuple[Callable[..., R], tuple[Any, ...], dict[str, Any]]],
    *,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
) -> list[R]:
    """Execute multiple different functions in parallel (scatter-gather pattern).

    Useful when you have heterogeneous work to parallelize.

    Args:
        tasks: List of (function, args, kwargs) tuples.
        num_cpus: CPUs per task.
        num_gpus: GPUs per task.

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
        return [func(*args, **kwargs) for func, args, kwargs in materialized_tasks]

    from django_ray.execution_codec import NestedExecutionBoundaryKind

    operation = _strict_nested_operation(NestedExecutionBoundaryKind.DISTRIBUTED_SCATTER)

    import pickle

    import ray

    remote = _get_cached_remote("scatter_gather").options(num_cpus=num_cpus, num_gpus=num_gpus)
    calls: list[tuple[Any, ...]] = []
    for index, (func, args, kwargs) in enumerate(materialized_tasks):
        pickled_func = pickle.dumps(func)
        if operation is None:
            calls.append((pickled_func, args, kwargs))
        else:
            calls.append(
                (
                    pickled_func,
                    args,
                    kwargs,
                    *_nested_distributed_request(operation, pickled_func, index),
                )
            )
    return _collect_remote_results(ray, remote, calls, None)


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
