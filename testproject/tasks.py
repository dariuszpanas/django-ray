"""Sample tasks for testing django-ray."""

from __future__ import annotations

import time


def add_numbers(a: int, b: int) -> int:
    """Simple task that adds two numbers."""
    return a + b


def multiply_numbers(a: int, b: int) -> int:
    """Simple task that multiplies two numbers."""
    return a * b


def slow_task(seconds: float = 1.0) -> str:
    """Task that takes some time to complete."""
    time.sleep(seconds)
    return f"Slept for {seconds} seconds"


def failing_task() -> None:
    """Task that always fails."""
    raise ValueError("This task is designed to fail!")


def echo_task(*args, **kwargs) -> dict:
    """Task that echoes back its arguments."""
    return {
        "args": list(args),
        "kwargs": kwargs,
    }


def cpu_intensive_task(n: int = 1000000) -> int:
    """CPU-intensive task for testing."""
    total = 0
    for i in range(n):
        total += i * i
    return total
