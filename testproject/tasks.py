"""Sample tasks for testing django-ray.

These tasks use Django 6's native @task decorator, which integrates with
the django_ray.backends.RayTaskBackend for distributed execution on Ray.

Usage:
    from testproject.tasks import add_numbers

    # Enqueue for background execution on Ray
    result = add_numbers.enqueue(1, 2)

    # Check status
    result.refresh()
    print(result.status)  # TaskResultStatus.SUCCESSFUL

    # Get return value (blocks if not finished)
    print(result.return_value)  # 3
"""

from __future__ import annotations

import time

from django.tasks import task


@task
def add_numbers(a: int, b: int) -> int:
    """Simple task that adds two numbers."""
    return a + b


@task
def multiply_numbers(a: int, b: int) -> int:
    """Simple task that multiplies two numbers."""
    return a * b


@task
def slow_task(seconds: float = 1.0) -> str:
    """Task that takes some time to complete."""
    time.sleep(seconds)
    return f"Slept for {seconds} seconds"


@task
def failing_task() -> None:
    """Task that always fails."""
    raise ValueError("This task is designed to fail!")


@task
def echo_task(*args, **kwargs) -> dict:
    """Task that echoes back its arguments."""
    return {
        "args": list(args),
        "kwargs": kwargs,
    }


@task
def cpu_intensive_task(n: int = 1000000) -> int:
    """CPU-intensive task for testing."""
    total = 0
    for i in range(n):
        total += i * i
    return total
