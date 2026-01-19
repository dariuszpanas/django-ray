"""Tasks designed for distributed cluster execution.

These tasks demonstrate patterns that work well in a distributed Ray cluster:
- Data processing with chunking
- Fan-out/fan-in patterns
- Long-running batch jobs

IMPORTANT: These tasks use Ray's distributed computing capabilities.
When running on a Ray cluster, the work is actually parallelized across
all available workers. When Ray is not available, they fall back to
sequential execution.

Example:
    from testproject.apps.cluster_tasks.tasks import distributed_search

    # This will actually search in parallel across the Ray cluster!
    result = distributed_search.enqueue(
        pattern="test",
        data_sources=["source1", "source2", "source3", ...]
    )
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from django.tasks import task

# Import distributed computing utilities
from django_ray.runtime.distributed import (
    get_num_workers,
    get_total_cpus,
    is_ray_available,
    parallel_map,
    parallel_starmap,
    scatter_gather,
)


def _process_single_item(item: Any) -> dict[str, Any]:
    """Process a single item (runs on Ray worker)."""
    item_hash = hashlib.md5(str(item).encode()).hexdigest()[:8]
    return {"original": item, "hash": item_hash}


@task(queue_name="default")
def process_chunk(data: list[Any], chunk_id: int = 0) -> dict[str, Any]:
    """Process a chunk of data using distributed computing.

    When Ray is available, each item is processed in parallel across
    the entire cluster. Otherwise falls back to sequential processing.

    Args:
        data: List of items to process
        chunk_id: Identifier for this chunk

    Returns:
        Processing results for this chunk
    """
    start = time.time()

    # This actually distributes work across the Ray cluster!
    processed = parallel_map(_process_single_item, data)

    elapsed = time.time() - start

    return {
        "chunk_id": chunk_id,
        "input_count": len(data),
        "output_count": len(processed),
        "elapsed_seconds": round(elapsed, 4),
        "sample": processed[:3] if processed else [],
        "distributed": is_ray_available(),
        "cluster_cpus": get_total_cpus(),
        "cluster_workers": get_num_workers(),
    }


@task(queue_name="default")
def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate results from multiple chunks.

    Fan-in task that combines results from parallel chunk processing.

    Args:
        results: List of chunk processing results

    Returns:
        Aggregated statistics
    """
    total_items = sum(r.get("input_count", 0) for r in results)
    total_time = sum(r.get("elapsed_seconds", 0) for r in results)
    chunk_ids = [r.get("chunk_id") for r in results]

    return {
        "total_chunks": len(results),
        "total_items": total_items,
        "total_processing_time": round(total_time, 4),
        "avg_time_per_chunk": round(total_time / len(results), 4) if results else 0,
        "chunk_ids": sorted(chunk_ids),
    }


def _search_single_source(source: str, pattern: str, case_sensitive: bool = False) -> dict[str, Any] | None:
    """Search a single data source (runs on Ray worker)."""
    time.sleep(0.1)  # Simulate 100ms I/O per source

    search_pattern = pattern if case_sensitive else pattern.lower()
    search_source = source if case_sensitive else source.lower()

    if search_pattern in search_source:
        return {
            "source": source,
            "matches": 1,
            "positions": [search_source.find(search_pattern)],
        }
    return None


@task(queue_name="default")
def distributed_search(
    pattern: str,
    data_sources: list[str],
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Search for a pattern across multiple data sources IN PARALLEL.

    This is a TRUE distributed search - when running on a Ray cluster,
    each data source is searched on a different worker simultaneously.

    Args:
        pattern: Search pattern
        data_sources: List of data source identifiers
        case_sensitive: Whether search is case-sensitive

    Returns:
        Search results with cluster info
    """
    start = time.time()

    # Create argument tuples for parallel execution
    search_args = [(source, pattern, case_sensitive) for source in data_sources]

    # This distributes the search across the entire Ray cluster!
    all_results = parallel_starmap(_search_single_source, search_args)

    # Filter out None results
    results = [r for r in all_results if r is not None]

    elapsed = time.time() - start

    # Calculate what sequential would have taken
    sequential_estimate = len(data_sources) * 0.1  # 100ms per source

    return {
        "pattern": pattern,
        "sources_searched": len(data_sources),
        "matches_found": len(results),
        "results": results,
        "elapsed_seconds": round(elapsed, 4),
        "sequential_estimate_seconds": round(sequential_estimate, 4),
        "speedup": round(sequential_estimate / elapsed, 2) if elapsed > 0 else 0,
        "distributed": is_ray_available(),
        "cluster_cpus": get_total_cpus(),
        "cluster_workers": get_num_workers(),
    }


def _cpu_intensive_work(item_id: int, duration_seconds: float) -> dict[str, Any]:
    """CPU-intensive work that runs on Ray worker."""
    import hashlib

    start = time.time()
    iterations = 0
    data = f"item_{item_id}_data".encode() * 100

    # Burn CPU for the specified duration
    while (time.time() - start) < duration_seconds:
        hashlib.sha256(data).hexdigest()
        iterations += 1

    actual_duration = time.time() - start
    return {
        "item_id": item_id,
        "iterations": iterations,
        "duration_seconds": round(actual_duration, 4),
    }


@task(queue_name="default")
def distributed_cpu_benchmark(
    num_items: int = 10,
    seconds_per_item: float = 2.0,
) -> dict[str, Any]:
    """Benchmark distributed CPU work across the cluster.

    This task spawns num_items Ray tasks, each doing CPU work for
    seconds_per_item seconds. With a cluster, these run in parallel.

    Args:
        num_items: Number of parallel CPU tasks to spawn
        seconds_per_item: How long each task should burn CPU

    Returns:
        Benchmark results showing parallelization benefit
    """
    start = time.time()

    # Create work items
    work_items = [(i, seconds_per_item) for i in range(num_items)]

    # Execute in parallel across cluster
    results = parallel_starmap(_cpu_intensive_work, work_items)

    elapsed = time.time() - start
    sequential_estimate = num_items * seconds_per_item

    # Calculate effective parallelism (how many tasks ran truly in parallel)
    # This is more accurate than using Ray's reported CPUs
    effective_parallelism = sequential_estimate / elapsed if elapsed > 0 else 1
    batches_needed = num_items / effective_parallelism if effective_parallelism > 0 else num_items

    return {
        "num_items": num_items,
        "seconds_per_item": seconds_per_item,
        "total_work_seconds": round(num_items * seconds_per_item, 2),
        "actual_elapsed_seconds": round(elapsed, 4),
        "sequential_estimate_seconds": round(sequential_estimate, 2),
        "speedup": round(sequential_estimate / elapsed, 2) if elapsed > 0 else 0,
        "effective_parallelism": round(effective_parallelism, 1),
        "batches_executed": round(batches_needed, 2),
        "distributed": is_ray_available(),
        "ray_reported_cpus": get_total_cpus(),
        "cluster_workers": get_num_workers(),
        "item_results": results,
    }


def _fetch_single_url(url: str, timeout_seconds: int = 30) -> dict[str, Any]:
    """Fetch a single URL (runs on Ray worker)."""
    start = time.time()
    time.sleep(0.05)  # 50ms simulated latency
    elapsed = time.time() - start

    return {
        "url": url,
        "status": 200,
        "elapsed_ms": round(elapsed * 1000, 2),
        "content_length": len(url) * 100,
    }


@task(queue_name="default")
def batch_http_requests(
    urls: list[str],
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Fetch multiple URLs in parallel across the cluster.

    In production, this would make actual HTTP requests.
    Each URL is fetched on a potentially different Ray worker.

    Args:
        urls: List of URLs to fetch
        timeout_seconds: Request timeout

    Returns:
        Batch request results
    """
    start = time.time()

    # Fetch all URLs in parallel across the cluster
    results = parallel_map(_fetch_single_url, urls, timeout_seconds=timeout_seconds)

    elapsed = time.time() - start

    return {
        "total_requests": len(urls),
        "successful": len(results),
        "failed": 0,
        "total_bytes": sum(r["content_length"] for r in results),
        "avg_latency_ms": round(sum(r["elapsed_ms"] for r in results) / len(results), 2)
        if results
        else 0,
        "total_elapsed_seconds": round(elapsed, 4),
        "distributed": is_ray_available(),
        "cluster_workers": get_num_workers(),
        # Show the parallelization benefit
        "sequential_time_estimate": round(len(urls) * 0.05, 2),
        "speedup": round((len(urls) * 0.05) / elapsed, 2) if elapsed > 0 else 0,
    }


@task(queue_name="default")
def etl_transform(
    records: list[dict[str, Any]],
    transformations: list[str],
) -> dict[str, Any]:
    """Apply transformations to records (ETL pattern).

    Demonstrates data transformation in a distributed context.

    Args:
        records: Input records
        transformations: List of transformation names to apply

    Returns:
        Transformed records and statistics
    """
    transformed = []

    for record in records:
        result = record.copy()

        for transform in transformations:
            if transform == "uppercase":
                result = {
                    k: v.upper() if isinstance(v, str) else v for k, v in result.items()
                }
            elif transform == "hash_id":
                if "id" in result:
                    result["id_hash"] = hashlib.md5(
                        str(result["id"]).encode()
                    ).hexdigest()[:8]
            elif transform == "timestamp":
                result["processed_at"] = time.time()
            elif transform == "validate":
                result["is_valid"] = all(v is not None for v in result.values())

        transformed.append(result)

    return {
        "input_count": len(records),
        "output_count": len(transformed),
        "transformations_applied": transformations,
        "sample_output": transformed[:2] if transformed else [],
    }


@task(queue_name="default")
def long_running_job(
    duration_seconds: int = 60,
    checkpoint_interval: int = 10,
) -> dict[str, Any]:
    """Simulate a long-running batch job with checkpoints.

    Demonstrates pattern for jobs that run for extended periods.

    Args:
        duration_seconds: Total job duration
        checkpoint_interval: Seconds between checkpoints

    Returns:
        Job completion status
    """
    checkpoints = []
    start = time.time()

    elapsed = 0
    while elapsed < duration_seconds:
        time.sleep(min(checkpoint_interval, duration_seconds - elapsed))
        elapsed = time.time() - start

        checkpoints.append(
            {
                "elapsed": round(elapsed, 2),
                "progress": min(100, round(elapsed / duration_seconds * 100, 1)),
            }
        )

    return {
        "duration_seconds": duration_seconds,
        "actual_duration": round(time.time() - start, 2),
        "checkpoints": checkpoints,
        "status": "completed",
    }
