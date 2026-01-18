"""Tasks for sync mode execution.

These tasks are designed to run synchronously without Ray.
Use the 'sync' queue to route them to a sync worker.

Example:
    # Enqueue a task
    from testproject.apps.sync_tasks.tasks import process_data
    result = process_data.using(queue_name="sync").enqueue(items=[1, 2, 3])

    # Run worker in sync mode
    python manage.py django_ray_worker --sync --queue=sync
"""

from __future__ import annotations

from typing import Any

from django.tasks import task


@task(queue_name="sync")
def simple_calculation(a: int, b: int, operation: str = "add") -> int:
    """Perform a simple calculation.

    Args:
        a: First operand
        b: Second operand
        operation: One of 'add', 'subtract', 'multiply', 'divide'

    Returns:
        Result of the operation
    """
    operations = {
        "add": lambda x, y: x + y,
        "subtract": lambda x, y: x - y,
        "multiply": lambda x, y: x * y,
        "divide": lambda x, y: x // y if y != 0 else 0,
    }
    return operations.get(operation, operations["add"])(a, b)


@task(queue_name="sync")
def process_data(items: list[Any]) -> dict[str, Any]:
    """Process a list of items.

    Demonstrates task with complex input/output serialization.

    Args:
        items: List of items to process

    Returns:
        Dictionary with processing results
    """
    return {
        "count": len(items),
        "items": items,
        "sum": sum(x for x in items if isinstance(x, (int, float))),
        "types": list(set(type(x).__name__ for x in items)),
    }


@task(queue_name="sync")
def validate_email(email: str) -> dict[str, Any]:
    """Validate an email address format.

    Simple validation task that doesn't need Ray.

    Args:
        email: Email address to validate

    Returns:
        Validation result with details
    """
    import re

    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    is_valid = bool(re.match(pattern, email))

    return {
        "email": email,
        "is_valid": is_valid,
        "domain": email.split("@")[1] if "@" in email else None,
    }


@task(queue_name="sync")
def generate_report(data: dict[str, Any]) -> str:
    """Generate a simple text report from data.

    Args:
        data: Dictionary containing report data

    Returns:
        Formatted report string
    """
    lines = ["=" * 40, "REPORT", "=" * 40]

    for key, value in data.items():
        lines.append(f"{key}: {value}")

    lines.append("=" * 40)
    return "\n".join(lines)

