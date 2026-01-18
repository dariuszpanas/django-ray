"""Tasks designed for distributed cluster execution.

These tasks demonstrate patterns that work well in a distributed Ray cluster:
- Data processing with chunking
- Fan-out/fan-in patterns
- Long-running batch jobs

Example:
    from testproject.apps.cluster_tasks.tasks import process_chunk

    # Process data in chunks across cluster
    for chunk in chunks:
        process_chunk.enqueue(data=chunk)
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from django.tasks import task


@task(queue_name="default")
def process_chunk(data: list[Any], chunk_id: int = 0) -> dict[str, Any]:
    """Process a chunk of data.

    Designed for distributed data processing where large datasets
    are split into chunks and processed in parallel.

    Args:
        data: List of items to process
        chunk_id: Identifier for this chunk

    Returns:
        Processing results for this chunk
    """
    start = time.time()

    # Simulate processing
    processed = []
    for item in data:
        # Hash each item
        item_hash = hashlib.md5(str(item).encode()).hexdigest()[:8]
        processed.append({"original": item, "hash": item_hash})

    elapsed = time.time() - start

    return {
        "chunk_id": chunk_id,
        "input_count": len(data),
        "output_count": len(processed),
        "elapsed_seconds": round(elapsed, 4),
        "sample": processed[:3] if processed else [],
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


@task(queue_name="default")
def distributed_search(
    pattern: str,
    data_sources: list[str],
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Search for a pattern across multiple data sources.

    Simulates a distributed search operation.

    Args:
        pattern: Search pattern
        data_sources: List of data source identifiers
        case_sensitive: Whether search is case-sensitive

    Returns:
        Search results
    """
    results = []

    for source in data_sources:
        # Simulate searching this source
        time.sleep(0.01)  # Simulate I/O

        # Mock result
        if pattern.lower() in source.lower():
            results.append({
                "source": source,
                "matches": 1,
                "positions": [source.lower().find(pattern.lower())],
            })

    return {
        "pattern": pattern,
        "sources_searched": len(data_sources),
        "matches_found": len(results),
        "results": results,
    }


@task(queue_name="default")
def batch_http_requests(
    urls: list[str],
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Simulate batch HTTP requests.

    In a real cluster, this could make actual HTTP requests in parallel.
    Here we simulate the pattern.

    Args:
        urls: List of URLs to fetch
        timeout_seconds: Request timeout

    Returns:
        Batch request results
    """
    results = []

    for url in urls:
        start = time.time()

        # Simulate HTTP request
        time.sleep(0.05)  # 50ms per "request"

        elapsed = time.time() - start

        results.append({
            "url": url,
            "status": 200,  # Mock success
            "elapsed_ms": round(elapsed * 1000, 2),
            "content_length": len(url) * 100,  # Mock content
        })

    return {
        "total_requests": len(urls),
        "successful": len(results),
        "failed": 0,
        "total_bytes": sum(r["content_length"] for r in results),
        "avg_latency_ms": round(
            sum(r["elapsed_ms"] for r in results) / len(results), 2
        ) if results else 0,
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
                result = {k: v.upper() if isinstance(v, str) else v
                         for k, v in result.items()}
            elif transform == "hash_id":
                if "id" in result:
                    result["id_hash"] = hashlib.md5(
                        str(result["id"]).encode()
                    ).hexdigest()[:8]
            elif transform == "timestamp":
                result["processed_at"] = time.time()
            elif transform == "validate":
                result["is_valid"] = all(
                    v is not None for v in result.values()
                )

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

        checkpoints.append({
            "elapsed": round(elapsed, 2),
            "progress": min(100, round(elapsed / duration_seconds * 100, 1)),
        })

    return {
        "duration_seconds": duration_seconds,
        "actual_duration": round(time.time() - start, 2),
        "checkpoints": checkpoints,
        "status": "completed",
    }

