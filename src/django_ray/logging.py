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


_STDLIB_LOG_KWARGS: frozenset[str] = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})
_RESERVED_LOG_RECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys() | {"message", "asctime"}
)


def _safe_log_record_extra(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Prefix keys that collide with ``logging.LogRecord`` attributes."""
    safe: dict[str, Any] = {}
    for key, value in extra.items():
        target = key
        if target in _RESERVED_LOG_RECORD_ATTRS:
            target = f"ray_{key}"
            while target in _RESERVED_LOG_RECORD_ATTRS or target in safe:
                target = f"ray_{target}"
        safe[target] = value
    return safe


def _extract_exc_info(exc_info: object) -> tuple[BaseException | None, object]:
    """Normalize ``exc_info`` (bool, BaseException, or 3-tuple) without dropping instances."""
    if not exc_info:
        return None, None
    if exc_info is True:
        resolved = sys.exc_info()
        exception = (
            resolved[1] if len(resolved) > 1 and isinstance(resolved[1], BaseException) else None
        )
        return exception, resolved if exception is not None else None
    if isinstance(exc_info, BaseException):
        return exc_info, (type(exc_info), exc_info, exc_info.__traceback__)
    if isinstance(exc_info, tuple) and len(exc_info) > 1:
        exception = exc_info[1] if isinstance(exc_info[1], BaseException) else None
        return exception, exc_info if exception is not None else None
    return None, None


class StructuredLogAdapter(logging.LoggerAdapter):
    """Log adapter that adds structured context to log messages.

    This adapter formats log messages with JSON-structured extra data
    for better parsing by log aggregators (ELK, Splunk, etc.) and supports
    both ``extra={...}`` and keyword-argument structured context.
    """

    def __init__(
        self,
        logger: logging.Logger,
        extra: Mapping[str, object] | None = None,
        *,
        preserve_exc_info: bool = False,
    ) -> None:
        super().__init__(logger, extra)
        self.preserve_exc_info = preserve_exc_info

    def _process_message(
        self,
        msg: object,
        kwargs: MutableMapping[str, Any],
        *,
        already_redacted: bool,
    ) -> tuple[object, MutableMapping[str, Any]]:
        # Collect documented call-time keyword arguments (e.g., worker_id="abc")
        # alongside explicit ``extra={...}`` without leaking unexpected kwargs
        # into ``logging.Logger._log()``.
        raw_extra = kwargs.get("extra")
        kwarg_extra: dict[str, Any] = {}
        for key in list(kwargs):
            if key not in _STDLIB_LOG_KWARGS:
                kwarg_extra[key] = kwargs.pop(key)

        if kwarg_extra:
            if isinstance(raw_extra, Mapping):
                call_extra: object = {**raw_extra, **kwarg_extra}
            else:
                call_extra = kwarg_extra
        else:
            call_extra = raw_extra if raw_extra is not None else {}

        if not already_redacted:
            msg = redact_text(msg)

        # Normalize bool, BaseException instance, and 3-tuple exc_info forms so
        # exception instances are never silently dropped. By default, retain a
        # compact redacted exception description and clear raw exc_info so
        # unredacted tracebacks do not leak into log streams; opt in via
        # ``preserve_exc_info=True`` when downstream handlers (e.g. Sentry)
        # require the normalized exc_info tuple.
        exc_info = kwargs.get("exc_info")
        exception, normalized_exc_info = _extract_exc_info(exc_info)
        exception_description: str | None = None
        if exc_info:
            if exception is not None:
                exception_description = redact_exception(exception)
            kwargs["exc_info"] = normalized_exc_info if self.preserve_exc_info else None

        redacted_extra = _redacted_combined_extra(self.extra, call_extra)
        if exception_description is not None:
            redacted_extra["exception"] = exception_description

        # Format structured data as JSON suffix
        if redacted_extra:
            try:
                json_extra = json.dumps(redacted_extra, default=str)
                msg = f"{msg} | {json_extra}"
            except (TypeError, ValueError):
                # This should be unreachable because redact_value converts
                # non-JSON objects to type markers, but keep logging resilient.
                msg = f"{msg} | {redacted_extra}"

        kwargs["extra"] = _safe_log_record_extra(redacted_extra)
        return msg, kwargs

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
        return self._process_message(msg, kwargs, already_redacted=False)

    def log(self, level: int, msg: object, *args: Any, **kwargs: Any) -> None:
        """Redact format arguments once before delegating to ``logging``."""
        if self.isEnabledFor(level):
            formatted = _format_redacted_message(msg, args)
            processed_msg, processed_kwargs = self._process_message(
                formatted, kwargs, already_redacted=True
            )
            self.logger.log(level, processed_msg, **processed_kwargs)


def get_logger(
    name: str,
    *,
    preserve_exc_info: bool = False,
    **extra: Any,
) -> StructuredLogAdapter:
    """Get a structured logger for the given module.

    Args:
        name: Logger name (typically __name__).
        preserve_exc_info: When True, preserve normalized ``exc_info`` tuples on
            emitted LogRecords for handlers such as Sentry while still recording
            redacted exception summaries in structured context (default: False).
        **extra: Default extra context to include in all messages.

    Returns:
        StructuredLogAdapter instance.

    Example:
        logger = get_logger(__name__, component="worker")
        logger.info("Starting", worker_id="abc123")
        # Output: Starting | {"component": "worker", "worker_id": "abc123"}
    """
    base_logger = logging.getLogger(name)
    return StructuredLogAdapter(base_logger, extra, preserve_exc_info=preserve_exc_info)


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
def configure_default_logging(
    level: int = logging.INFO,
    *,
    propagate: bool | None = None,
) -> None:
    """Configure default logging for django-ray without hijacking root handlers.

    Installs a fallback StreamHandler only when neither the ``django_ray``
    logger nor the root logger already has handlers configured. Never disables
    ``propagate`` unless ``propagate=False`` is explicitly requested.

    Args:
        level: The logging level (default: INFO).
        propagate: Optional explicit value for ``logger.propagate``.
    """
    logger = logging.getLogger("django_ray")
    root_logger = logging.getLogger()
    if not logger.handlers and not root_logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)
    elif logger.level == logging.NOTSET:
        logger.setLevel(level)
    if propagate is not None:
        logger.propagate = propagate
