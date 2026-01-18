"""Structured logging for django-ray.

This module provides consistent logging throughout the django-ray package
with structured context for better debugging and monitoring.

Usage:
    from django_ray.logging import get_logger

    logger = get_logger(__name__)
    logger.info("Task started", task_id=task.pk, callable=task.callable_path)
"""

from __future__ import annotations

import logging
import json
from typing import Any


class StructuredLogAdapter(logging.LoggerAdapter):
    """Log adapter that adds structured context to log messages.

    This adapter formats log messages with JSON-structured extra data
    for better parsing by log aggregators (ELK, Splunk, etc.).
    """

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Process log message with structured context.

        Args:
            msg: The log message.
            kwargs: Keyword arguments passed to the logger.

        Returns:
            Tuple of (formatted message, kwargs).
        """
        # Extract extra fields from kwargs
        extra = kwargs.get("extra", {})

        # Merge adapter's extra with call-time extra
        combined_extra = {**self.extra, **extra}  # type: ignore

        # Format structured data as JSON suffix
        if combined_extra:
            # Filter out None values
            filtered = {k: v for k, v in combined_extra.items() if v is not None}
            if filtered:
                try:
                    json_extra = json.dumps(filtered, default=str)
                    msg = f"{msg} | {json_extra}"
                except (TypeError, ValueError):
                    # Fallback if JSON serialization fails
                    msg = f"{msg} | {filtered}"

        kwargs["extra"] = combined_extra
        return msg, kwargs


def get_logger(name: str, **extra: Any) -> StructuredLogAdapter:
    """Get a structured logger for the given module.

    Args:
        name: Logger name (typically __name__).
        **extra: Default extra context to include in all messages.

    Returns:
        StructuredLogAdapter instance.

    Example:
        logger = get_logger(__name__, component="worker")
        logger.info("Starting", worker_id="abc123")
        # Output: Starting | {"component": "worker", "worker_id": "abc123"}
    """
    base_logger = logging.getLogger(name)
    return StructuredLogAdapter(base_logger, extra)


# Pre-configured loggers for common components
def get_worker_logger(worker_id: str) -> StructuredLogAdapter:
    """Get a logger for the worker component.

    Args:
        worker_id: The worker's unique identifier.

    Returns:
        Logger with worker context.
    """
    return get_logger(
        "django_ray.worker",
        component="worker",
        worker_id=worker_id,
    )


def get_task_logger(task_id: str | int, callable_path: str) -> StructuredLogAdapter:
    """Get a logger for task execution.

    Args:
        task_id: The task's unique identifier.
        callable_path: The dotted path to the task callable.

    Returns:
        Logger with task context.
    """
    return get_logger(
        "django_ray.task",
        component="task",
        task_id=str(task_id),
        callable_path=callable_path,
    )


def get_backend_logger() -> StructuredLogAdapter:
    """Get a logger for the task backend.

    Returns:
        Logger with backend context.
    """
    return get_logger(
        "django_ray.backend",
        component="backend",
    )


# Configure default logging format if not already configured
def configure_default_logging(level: int = logging.INFO) -> None:
    """Configure default logging for django-ray.

    This sets up a basic logging configuration if none exists.
    Should be called early in application startup.

    Args:
        level: The logging level (default: INFO).
    """
    # Check if django_ray logger already has handlers
    logger = logging.getLogger("django_ray")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)

