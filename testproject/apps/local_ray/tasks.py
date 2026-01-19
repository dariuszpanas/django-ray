"""Tasks optimized for local Ray execution.

These tasks demonstrate CPU-intensive operations that benefit from
Ray's parallel execution, even on a single machine.

Example:
    from testproject.apps.local_ray.tasks import parallel_sum
    result = parallel_sum.enqueue(numbers=list(range(1000000)))
"""

from __future__ import annotations

import time
from typing import Any

from django.tasks import task


@task(queue_name="default")
def parallel_sum(numbers: list[int]) -> int:
    """Sum a large list of numbers.

    In Ray, this could be distributed across workers.

    Args:
        numbers: List of integers to sum

    Returns:
        Sum of all numbers
    """
    return sum(numbers)


@task(queue_name="default")
def fibonacci(n: int) -> dict[str, Any]:
    """Calculate the nth Fibonacci number.

    CPU-intensive calculation. Returns metadata for large results
    to avoid integer string conversion limits (Python limit: 4300 digits).

    Args:
        n: Which Fibonacci number to calculate

    Returns:
        Dict with result info (full value for small n, metadata for large n)
    """
    import math
    import sys

    if n <= 1:
        return {"n": n, "result": n, "digits": 1}

    # Iterative for efficiency
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b

    # Estimate digits using logarithm (avoids string conversion)
    # For Fibonacci, F(n) ≈ φ^n / √5 where φ = (1+√5)/2
    # So digits ≈ n * log10(φ) - 0.5 * log10(5)
    phi = (1 + math.sqrt(5)) / 2
    estimated_digits = int(n * math.log10(phi) - 0.5 * math.log10(5)) + 1

    # Get the current limit
    current_limit = sys.get_int_max_str_digits()

    if estimated_digits > current_limit - 100:  # Leave some margin
        # Return metadata only - can't convert to string safely
        return {
            "n": n,
            "estimated_digits": estimated_digits,
            "too_large_for_string_conversion": True,
            "python_str_digit_limit": current_limit,
            "tip": "Use smaller n, or increase limit with sys.set_int_max_str_digits()",
        }

    # Safe to convert - get actual digits
    result_str = str(b)
    actual_digits = len(result_str)

    # For readability, truncate very long results
    if actual_digits > 1000:
        return {
            "n": n,
            "digits": actual_digits,
            "first_50_digits": result_str[:50],
            "last_50_digits": result_str[-50:],
        }

    return {"n": n, "result": b, "digits": actual_digits}


@task(queue_name="default")
def prime_check(n: int) -> dict[str, Any]:
    """Check if a number is prime and find its factors.

    Args:
        n: Number to check

    Returns:
        Dict with primality and factors
    """
    if n < 2:
        return {"n": n, "is_prime": False, "factors": []}

    factors = []
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            factors.extend([i, n // i])

    factors = sorted(set(factors))

    return {
        "n": n,
        "is_prime": len(factors) == 0,
        "factors": factors if factors else [1, n],
    }


@task(queue_name="default")
def matrix_multiply(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Multiply two matrices.

    CPU-intensive operation suitable for Ray parallelization.

    Args:
        a: First matrix (list of rows)
        b: Second matrix (list of rows)

    Returns:
        Result matrix
    """
    if not a or not b or not a[0]:
        return []

    rows_a, cols_a = len(a), len(a[0])
    rows_b, cols_b = len(b), len(b[0])

    if cols_a != rows_b:
        raise ValueError(f"Cannot multiply {rows_a}x{cols_a} with {rows_b}x{cols_b}")

    result = [[0.0] * cols_b for _ in range(rows_a)]

    for i in range(rows_a):
        for j in range(cols_b):
            for k in range(cols_a):
                result[i][j] += a[i][k] * b[k][j]

    return result


@task(queue_name="default")
def simulate_workload(iterations: int = 1000000, sleep_ms: int = 0) -> dict[str, Any]:
    """Simulate a CPU-intensive workload.

    Useful for testing worker scaling and load distribution.

    Args:
        iterations: Number of iterations to perform
        sleep_ms: Optional sleep time in milliseconds

    Returns:
        Execution statistics
    """
    import time

    start = time.time()

    # CPU work
    total = 0
    for i in range(iterations):
        total += i * i

    # Optional sleep to simulate I/O
    if sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)

    elapsed = time.time() - start

    return {
        "iterations": iterations,
        "result": total,
        "elapsed_seconds": round(elapsed, 4),
        "iterations_per_second": round(iterations / elapsed, 2) if elapsed > 0 else 0,
    }


@task(queue_name="high-priority")
def urgent_task(message: str) -> str:
    """A high-priority task that should be processed quickly.

    Uses the 'high-priority' queue for faster processing.

    Args:
        message: Message to process

    Returns:
        Processed message
    """
    return f"URGENT: {message.upper()}"


@task(queue_name="low-priority")
def background_task(message: str) -> str:
    """A low-priority background task.

    Uses the 'low-priority' queue for batch processing.

    Args:
        message: Message to process

    Returns:
        Processed message
    """
    time.sleep(0.1)  # Simulate slow processing
    return f"background: {message.lower()}"


# ============================================================================
# Stress Test Tasks - Push the system to its limits
# ============================================================================


@task(queue_name="default")
def stress_cpu(duration_seconds: float = 5.0) -> dict[str, Any]:
    """Pure CPU stress test - burns CPU for specified duration.

    Args:
        duration_seconds: How long to burn CPU

    Returns:
        Execution statistics
    """
    import hashlib

    start = time.time()
    iterations = 0
    data = b"stress test data" * 100

    while (time.time() - start) < duration_seconds:
        # CPU-intensive hash computation
        for _ in range(1000):
            hashlib.sha256(data).hexdigest()
            iterations += 1

    elapsed = time.time() - start
    return {
        "duration_requested": duration_seconds,
        "actual_duration": round(elapsed, 4),
        "hash_iterations": iterations,
        "hashes_per_second": round(iterations / elapsed, 2),
    }


@task(queue_name="default")
def stress_memory(size_mb: int = 100) -> dict[str, Any]:
    """Memory stress test - allocates and processes large data.

    Args:
        size_mb: Amount of memory to allocate in MB

    Returns:
        Processing statistics
    """
    import sys

    start = time.time()

    # Allocate large list
    chunk_size = 1024 * 1024  # 1MB chunks
    chunks = []
    for _ in range(size_mb):
        chunks.append(bytearray(chunk_size))

    alloc_time = time.time() - start

    # Process the data (sum bytes)
    process_start = time.time()
    total = sum(sum(chunk) for chunk in chunks)
    process_time = time.time() - process_start

    # Get actual memory size
    actual_size = sum(sys.getsizeof(chunk) for chunk in chunks)

    # Clean up
    del chunks

    return {
        "requested_mb": size_mb,
        "actual_bytes": actual_size,
        "allocation_seconds": round(alloc_time, 4),
        "processing_seconds": round(process_time, 4),
        "total_seconds": round(time.time() - start, 4),
        "checksum": total,
    }


@task(queue_name="default")
def stress_nested_compute(depth: int = 10, width: int = 100) -> dict[str, Any]:
    """Nested computation stress test - recursive-like computation.

    Args:
        depth: Depth of nested loops
        width: Width of each loop level

    Returns:
        Computation statistics
    """
    start = time.time()

    def compute_level(d: int, acc: int) -> int:
        if d <= 0:
            return acc
        total = 0
        for i in range(width):
            total += compute_level(d - 1, acc + i)
        return total

    # Limit depth to avoid stack overflow
    safe_depth = min(depth, 15)
    safe_width = min(width, 50) if safe_depth > 10 else width

    result = compute_level(safe_depth, 0)
    elapsed = time.time() - start

    return {
        "depth": safe_depth,
        "width": safe_width,
        "result_hash": hash(result) % (10**9),  # Avoid huge int serialization
        "elapsed_seconds": round(elapsed, 4),
    }


@task(queue_name="default")
def stress_prime_search(start: int = 1000000, count: int = 100) -> dict[str, Any]:
    """Find prime numbers - CPU intensive search.

    Args:
        start: Starting number to search from
        count: How many primes to find

    Returns:
        Primes found and statistics
    """
    import math

    start_time = time.time()

    def is_prime(n: int) -> bool:
        if n < 2:
            return False
        if n == 2:
            return True
        if n % 2 == 0:
            return False
        for i in range(3, int(math.sqrt(n)) + 1, 2):
            if n % i == 0:
                return False
        return True

    primes = []
    current = start
    checked = 0

    while len(primes) < count:
        checked += 1
        if is_prime(current):
            primes.append(current)
        current += 1

    elapsed = time.time() - start_time

    return {
        "start": start,
        "count": count,
        "numbers_checked": checked,
        "primes_found": len(primes),
        "first_5": primes[:5],
        "last_5": primes[-5:],
        "elapsed_seconds": round(elapsed, 4),
        "checks_per_second": round(checked / elapsed, 2),
    }


@task(queue_name="default")
def stress_json_payload(size_kb: int = 100, depth: int = 5) -> dict[str, Any]:
    """Create and process large nested JSON structures.

    Args:
        size_kb: Approximate target size in KB
        depth: Nesting depth of structures

    Returns:
        Structure statistics
    """
    import json

    start = time.time()

    def build_nested(d: int, target_size: int) -> dict[str, Any]:
        if d <= 0:
            return {"leaf": "x" * min(target_size, 1000)}

        child_count = max(2, target_size // (d * 100))
        children = {}
        for i in range(child_count):
            children[f"child_{i}"] = build_nested(d - 1, target_size // child_count)
        return {"level": d, "children": children}

    data = build_nested(min(depth, 10), size_kb * 1024)
    build_time = time.time() - start

    # Serialize and measure
    serialize_start = time.time()
    serialized = json.dumps(data)
    serialize_time = time.time() - serialize_start

    # Deserialize
    deserialize_start = time.time()
    _ = json.loads(serialized)
    deserialize_time = time.time() - deserialize_start

    return {
        "target_size_kb": size_kb,
        "actual_size_bytes": len(serialized),
        "actual_size_kb": round(len(serialized) / 1024, 2),
        "depth": depth,
        "build_seconds": round(build_time, 4),
        "serialize_seconds": round(serialize_time, 4),
        "deserialize_seconds": round(deserialize_time, 4),
        "total_seconds": round(time.time() - start, 4),
    }


@task(queue_name="default")
def stress_concurrent_simulation(
    task_count: int = 100,
    task_duration_ms: int = 10,
) -> dict[str, Any]:
    """Simulate many small tasks to test throughput.

    This doesn't actually create subtasks, but simulates the
    overhead of many small computations.

    Args:
        task_count: Number of simulated tasks
        task_duration_ms: Duration of each task in milliseconds

    Returns:
        Throughput statistics
    """
    import hashlib

    start = time.time()
    results = []

    for i in range(task_count):
        task_start = time.time()

        # Simulate some work
        data = f"task_{i}_data".encode()
        result = hashlib.sha256(data).hexdigest()

        if task_duration_ms > 0:
            # Busy-wait to simulate computation
            while (time.time() - task_start) * 1000 < task_duration_ms:
                _ = hashlib.md5(data).hexdigest()

        results.append(result[:8])

    elapsed = time.time() - start

    return {
        "task_count": task_count,
        "task_duration_ms": task_duration_ms,
        "total_seconds": round(elapsed, 4),
        "tasks_per_second": round(task_count / elapsed, 2),
        "avg_task_ms": round(elapsed * 1000 / task_count, 4),
        "sample_results": results[:5],
    }


