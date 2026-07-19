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

import asyncio
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
async def async_add_numbers(a: int, b: int) -> int:
    """Add two numbers after crossing a real coroutine scheduling point."""
    await asyncio.sleep(0)
    return a + b


@task
async def async_context_probe(
    value: str,
    *,
    load_execution: bool = False,
) -> dict[str, int | str | bool | None]:
    """Demonstrate durable context propagation and optional async ORM access."""
    from django_ray.runtime.context import get_current_task_context, get_current_task_execution_pk

    context_before = get_current_task_context()
    execution_id_before = get_current_task_execution_pk()
    await asyncio.sleep(0)
    context_after = get_current_task_context()
    execution_id_after = get_current_task_execution_pk()
    task_id = None
    if load_execution:
        if execution_id_after is None:
            raise RuntimeError("async_context_probe requires a durable task execution context")
        from django_ray.models import RayTaskExecution

        execution = await RayTaskExecution.objects.only("task_id").aget(pk=execution_id_after)
        task_id = execution.task_id

    return {
        "value": value,
        "execution_id_before": execution_id_before,
        "execution_id_after": execution_id_after,
        "ray_job_driver_before": (
            context_before.ray_job_driver if context_before is not None else None
        ),
        "ray_job_driver_after": context_after.ray_job_driver if context_after is not None else None,
        "task_id": task_id,
        "active_task_count": len(asyncio.all_tasks()),
        "loop_running": asyncio.get_running_loop().is_running(),
    }


@task
async def async_failing_task(*, no_retry: bool = False) -> None:
    """Raise a retryable or denylisted exception from inside a coroutine."""
    await asyncio.sleep(0)
    if no_retry:
        raise NoRetryError("Async task requested a permanent failure")
    raise ValueError("Async task requested a retryable failure")


@task
async def async_slow_task(seconds: float = 0.01) -> str:
    """Wait without blocking the task's event loop."""
    await asyncio.sleep(seconds)
    return f"Awaited for {seconds} seconds"


@task
def intermittent_task(fail_until_attempt: int = 3) -> dict:
    """Task that fails until a certain attempt number, then succeeds.

    NOTE: This task will auto-retry based on MAX_TASK_ATTEMPTS setting.
    To test manual retry, use failing_task_no_retry instead, or set
    MAX_TASK_ATTEMPTS=1 in your settings.

    Args:
        fail_until_attempt: Succeed on this attempt number (default: 3)

    Returns:
        dict with attempt info on success
    """
    from django_ray.models import RayTaskExecution
    from django_ray.runtime.context import get_current_task_execution_pk

    # Get current attempt from database
    execution_id = get_current_task_execution_pk()
    if execution_id is None:
        raise RuntimeError("intermittent_task requires a durable task execution context")
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
