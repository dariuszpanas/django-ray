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

import os
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")
R = TypeVar("R")

# Track if Django has been bootstrapped in this process
_django_bootstrapped = False


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
    if not items:
        return []

    if not is_ray_available():
        # Fallback to sequential execution
        return [func(item, **kwargs) for item in items]

    import ray

    # Create a Ray remote function that bootstraps Django
    @ray.remote(num_cpus=num_cpus, num_gpus=num_gpus)
    def _remote_func(pickled_func: bytes, item: T, **kw: Any) -> R:
        import pickle

        # Bootstrap Django before running the function
        _bootstrap_django_if_needed()
        fn = pickle.loads(pickled_func)
        return fn(item, **kw)

    # Pickle the function once to send to workers
    import pickle

    pickled_func = pickle.dumps(func)

    # Submit all tasks
    if max_concurrency and max_concurrency < len(items):
        # Process in batches to limit concurrency
        results = []
        for i in range(0, len(items), max_concurrency):
            batch = items[i : i + max_concurrency]
            refs = [_remote_func.remote(pickled_func, item, **kwargs) for item in batch]
            results.extend(ray.get(refs))
        return results
    else:
        # Submit all at once
        refs = [_remote_func.remote(pickled_func, item, **kwargs) for item in items]
        return ray.get(refs)


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
    if not items:
        return []

    if not is_ray_available():
        return [func(*args) for args in items]

    import pickle

    import ray

    @ray.remote(num_cpus=num_cpus, num_gpus=num_gpus)
    def _remote_func(pickled_func: bytes, *args: Any) -> R:
        # Bootstrap Django before running the function
        _bootstrap_django_if_needed()
        fn = pickle.loads(pickled_func)
        return fn(*args)

    # Pickle the function once
    pickled_func = pickle.dumps(func)

    if max_concurrency and max_concurrency < len(items):
        results = []
        for i in range(0, len(items), max_concurrency):
            batch = items[i : i + max_concurrency]
            refs = [_remote_func.remote(pickled_func, *args) for args in batch]
            results.extend(ray.get(refs))
        return results
    else:
        refs = [_remote_func.remote(pickled_func, *args) for args in items]
        return ray.get(refs)


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
    if not tasks:
        return []

    if not is_ray_available():
        return [func(*args, **kwargs) for func, args, kwargs in tasks]

    import pickle

    import ray

    @ray.remote(num_cpus=num_cpus, num_gpus=num_gpus)
    def _run_task(pickled_func: bytes, args: tuple, kwargs: dict) -> R:
        # Bootstrap Django before running the function
        _bootstrap_django_if_needed()
        func = pickle.loads(pickled_func)
        return func(*args, **kwargs)

    # Pickle each function and submit
    refs = [_run_task.remote(pickle.dumps(func), args, kwargs) for func, args, kwargs in tasks]
    return ray.get(refs)


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
