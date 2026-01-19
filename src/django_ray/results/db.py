"""Database-backed result storage."""

from __future__ import annotations

import json
from typing import Any

from django_ray.conf.settings import get_settings
from django_ray.results.base import BaseResultStore, ResultTooLargeError


class DatabaseResultStore(BaseResultStore):
    """Store results in the Django database.

    This store saves results directly in the RayTaskExecution model.
    It enforces a maximum size limit to prevent database bloat.
    """

    def __init__(self) -> None:
        """Initialize the database result store."""
        settings = get_settings()
        # Default 1MB limit
        self.max_size_bytes = settings.get("MAX_RESULT_SIZE_BYTES", 1024 * 1024)

    def store(self, task_id: str, result: Any) -> str:
        """Store result in the task execution record."""
        from django_ray.models import RayTaskExecution

        try:
            serialized = json.dumps(result)
        except (TypeError, ValueError) as e:
            raise ResultTooLargeError(f"Result is not JSON-serializable: {e}") from e

        if len(serialized.encode("utf-8")) > self.max_size_bytes:
            raise ResultTooLargeError(f"Result exceeds maximum size of {self.max_size_bytes} bytes")

        task = RayTaskExecution.objects.get(pk=task_id)  # type: ignore[attr-defined]
        task.result_data = serialized
        task.save(update_fields=["result_data"])

        return f"db:{task_id}"

    def retrieve(self, reference: str) -> Any:
        """Retrieve result from the task execution record."""
        from django_ray.models import RayTaskExecution

        if not reference.startswith("db:"):
            raise KeyError(f"Invalid reference format: {reference}")

        task_id = reference[3:]  # Remove "db:" prefix

        try:
            task = RayTaskExecution.objects.get(pk=task_id)  # type: ignore[attr-defined]
        except RayTaskExecution.DoesNotExist:  # type: ignore[attr-defined]
            raise KeyError(f"Task execution not found: {task_id}")

        if task.result_data is None:
            raise KeyError(f"No result stored for task: {task_id}")

        return json.loads(task.result_data)

    def delete(self, reference: str) -> bool:
        """Clear result from the task execution record."""
        from django_ray.models import RayTaskExecution

        if not reference.startswith("db:"):
            return False

        task_id = reference[3:]

        try:
            task = RayTaskExecution.objects.get(pk=task_id)  # type: ignore[attr-defined]
            task.result_data = None
            task.save(update_fields=["result_data"])
            return True
        except RayTaskExecution.DoesNotExist:  # type: ignore[attr-defined]
            return False
