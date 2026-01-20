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
    """Task that always fails (will be auto-retried based on MAX_TASK_ATTEMPTS)."""
    raise ValueError("This task is designed to fail!")


class NoRetryError(Exception):
    """Exception that won't trigger automatic retry.

    Add 'testproject.tasks.NoRetryError' to RETRY_EXCEPTION_DENYLIST in settings.
    """


@task
def failing_task_no_retry() -> None:
    """Task that fails and won't be auto-retried.

    Uses NoRetryError which should be in RETRY_EXCEPTION_DENYLIST.
    Use this to test manual retry via admin.
    """
    raise NoRetryError("This task failed and won't auto-retry. Use admin to retry manually.")


@task
def intermittent_task(execution_id: int, fail_until_attempt: int = 3) -> dict:
    """Task that fails until a certain attempt number, then succeeds.

    NOTE: This task will auto-retry based on MAX_TASK_ATTEMPTS setting.
    To test manual retry, use failing_task_no_retry instead, or set
    MAX_TASK_ATTEMPTS=1 in your settings.

    Args:
        execution_id: The RayTaskExecution.pk to look up attempt number
        fail_until_attempt: Succeed on this attempt number (default: 3)

    Returns:
        dict with attempt info on success
    """
    from django_ray.models import RayTaskExecution

    # Get current attempt from database
    execution = RayTaskExecution.objects.get(pk=execution_id)
    current_attempt = execution.attempt_number

    if current_attempt < fail_until_attempt:
        raise RuntimeError(
            f"Intermittent failure: attempt {current_attempt}/{fail_until_attempt}. "
            f"Will succeed on attempt {fail_until_attempt}."
        )

    return {
        "success": True,
        "attempts_needed": current_attempt,
        "execution_id": execution_id,
    }


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
