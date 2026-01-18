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
def fibonacci(n: int) -> int:
    """Calculate the nth Fibonacci number.

    CPU-intensive recursive calculation.

    Args:
        n: Which Fibonacci number to calculate

    Returns:
        The nth Fibonacci number
    """
    if n <= 1:
        return n

    # Iterative for efficiency
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


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

