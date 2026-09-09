"""Private, import-free task identity projection for durable result reads."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any, NoReturn
from weakref import WeakValueDictionary

from django.tasks.base import Task

_READ_ONLY_MESSAGE = (
    "A task reconstructed from a durable result is read-only; "
    "use an explicit application Task declaration to execute or enqueue work."
)

# Django's validator only accepts module-level Python functions, all of which
# support weak references. Historical reads never add entries. The latest
# successful declaration wins for an alias/path, including deliberate reloads.
_trusted_functions: WeakValueDictionary[tuple[str, str], Callable[..., Any]] = WeakValueDictionary()
_registry_lock = RLock()


def _register_validated_task(alias: str, task: Task) -> None:
    """Remember a trusted declaration only after backend validation succeeds."""
    with _registry_lock:
        _trusted_functions[alias, task.module_path] = task.func


def _refuse_execution() -> NoReturn:
    raise TypeError(_READ_ONLY_MESSAGE)


@dataclass(frozen=True, slots=True)
class _UnknownResultFunction:
    """An inert identity, comparable across reads without registering a path."""

    backend: str
    path: str

    def __call__(self, *args: Any, **kwargs: Any) -> NoReturn:
        _refuse_execution()


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReadOnlyResultTask(Task):
    _stored_callable_path: str

    def __post_init__(self) -> None:
        # A historical record need not satisfy today's executable task/queue
        # configuration. Do not validate or register a read projection.
        pass

    @property
    def module_path(self) -> str:
        return self._stored_callable_path

    @property
    def name(self) -> str:
        return self._stored_callable_path.rsplit(".", 1)[-1]

    def using(self, **kwargs: Any) -> NoReturn:
        _refuse_execution()

    def enqueue(self, *args: Any, **kwargs: Any) -> NoReturn:
        _refuse_execution()

    async def aenqueue(self, *args: Any, **kwargs: Any) -> NoReturn:
        _refuse_execution()

    def call(self, *args: Any, **kwargs: Any) -> NoReturn:
        _refuse_execution()

    async def acall(self, *args: Any, **kwargs: Any) -> NoReturn:
        _refuse_execution()

    def __reduce__(self) -> NoReturn:
        # Django's Task reduction normally turns module_path back into an import
        # on restoration. Result metadata must never become that instruction.
        _refuse_execution()

    @classmethod
    def _reconstruct(cls, kwargs: Any) -> NoReturn:
        _refuse_execution()


def _require_executable_task(task: Task) -> None:
    # Also cover backend.enqueue(result.task, ...), the async bridge and an
    # explicit call to a base Task method, which bypass subclass overrides.
    if isinstance(task, _ReadOnlyResultTask):
        _refuse_execution()


def _project_result_task(
    *,
    alias: str,
    callable_path: str,
    priority: int,
    queue_name: str,
    run_after: datetime | None,
) -> Task:
    with _registry_lock:
        func = _trusted_functions.get((alias, callable_path))
    return _ReadOnlyResultTask(
        func=func if func is not None else _UnknownResultFunction(alias, callable_path),
        backend=alias,
        priority=priority,
        queue_name=queue_name,
        run_after=run_after,
        _stored_callable_path=callable_path,
    )
