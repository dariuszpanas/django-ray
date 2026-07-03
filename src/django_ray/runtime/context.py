"""Execution context shared by durable tasks and nested workflows."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_task_execution_pk: ContextVar[int | None] = ContextVar(
    "django_ray_task_execution_pk",
    default=None,
)


def get_current_task_execution_pk() -> int | None:
    """Return the durable task primary key for the current Ray execution."""
    return _current_task_execution_pk.get()


@contextmanager
def durable_task_execution(task_pk: int) -> Iterator[None]:
    """Expose a durable task identity to nested workflow coordination."""
    token = _current_task_execution_pk.set(task_pk)
    try:
        yield
    finally:
        _current_task_execution_pk.reset(token)
