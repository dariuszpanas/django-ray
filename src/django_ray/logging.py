"""Structured logging for django-ray.

This module provides consistent logging throughout the django-ray package
with structured context for better debugging and monitoring.

Usage:
    from django_ray.logging import get_logger

    logger = get_logger(__name__)
    logger.info("Task started", task_id=task.pk, callable=task.callable_path)
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

from django_ray.redaction import REDACTED, redact_exception, redact_text, redact_value


def _redacted_combined_extra(adapter_extra: object, call_extra: object) -> dict[str, Any]:
    """Redact both extra mappings under one aggregate traversal budget."""
    sources = {
        "adapter": adapter_extra if isinstance(adapter_extra, Mapping) else {},
        "call": call_extra if isinstance(call_extra, Mapping) else {},
    }
    redacted = redact_value(sources)
    if not isinstance(redacted, dict) or set(redacted) != {"adapter", "call"}:
        return {"diagnostic": REDACTED}
    adapter = redacted["adapter"]
    call = redacted["call"]
    if not isinstance(adapter, dict) or not isinstance(call, dict):
        return {"diagnostic": REDACTED}
    return {key: value for key, value in {**adapter, **call}.items() if value is not None}


def _format_redacted_message(msg: object, args: tuple[Any, ...]) -> str:
    """Safely consume logging placeholders before terminal normalization.

    Formatting the redacted arguments against the original template preserves
    Python logging's positional ordering when a complete terminal sequence
    contains a placeholder which disappears from the rendered message. The
    formatted text is normalized and redacted before any handler receives it.
    """
    template = str(msg)
    if not args:
        return redact_text(template)

    redacted_args = redact_value(args)
    safe_args = tuple(redacted_args) if isinstance(redacted_args, list) else ()
    format_values: object = safe_args
    if len(safe_args) == 1 and isinstance(safe_args[0], Mapping):
        format_values = safe_args[0]
    try:
        formatted = template % format_values
    except (KeyError, OverflowError, TypeError, ValueError):
        # Keep logging resilient without handing an unsafe or mismatched
        # argument tuple to a downstream handler.
        formatted = template
    return redact_text(formatted)


class StructuredLogAdapter(logging.LoggerAdapter):
    """Log adapter that adds structured context to log messages.

    This adapter formats log messages with JSON-structured extra data
    for better parsing by log aggregators (ELK, Splunk, etc.).
    """

    def process(
        self, msg: object, kwargs: MutableMapping[str, Any]
    ) -> tuple[object, MutableMapping[str, Any]]:
        """Process log message with structured context.

        Args:
            msg: The log message.
            kwargs: Keyword arguments passed to the logger.

        Returns:
            Tuple of (formatted message, kwargs).
        """
        # Extract extra fields from kwargs.  Redaction is applied before the
        # values are handed to the logging framework so downstream handlers do
        # not receive the original sensitive payload either.
        extra = kwargs.get("extra", {})

        # Merge adapter's extra with call-time extra
        msg = redact_text(msg)

        # Python's logging formatter appends exc_info outside ``msg``.  Keep a
        # compact, redacted exception description and suppress the raw
        # traceback, which can contain credentials or personal data.
        exc_info = kwargs.get("exc_info")
        exception_description: str | None = None
        if exc_info:
            if exc_info is True:
                exc_info = sys.exc_info()
            exception = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
            if isinstance(exception, BaseException):
                exception_description = redact_exception(exception)
            kwargs["exc_info"] = None

        redacted_extra = _redacted_combined_extra(self.extra, extra)
        if exception_description is not None:
            redacted_extra["exception"] = exception_description

        # Format structured data as JSON suffix
        if redacted_extra:
            # Filter out None values
            try:
                json_extra = json.dumps(redacted_extra, default=str)
                msg = f"{msg} | {json_extra}"
            except (TypeError, ValueError):
                # This should be unreachable because redact_value converts
                # non-JSON objects to type markers, but keep logging resilient.
                msg = f"{msg} | {redacted_extra}"

        kwargs["extra"] = redacted_extra
        return msg, kwargs

    def log(self, level: int, msg: object, *args: Any, **kwargs: Any) -> None:
        """Redact format arguments before delegating to ``logging``."""
        if self.isEnabledFor(level):
            super().log(level, _format_redacted_message(msg, args), **kwargs)


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
        logger.propagate = False
