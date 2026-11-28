"""Declarative task contract decorator layered cleanly over Django 6 Tasks.

``@ray_task`` wraps ``django.tasks.task`` so decorated callables remain genuine
``django.tasks.base.Task`` instances (compatible with ``.enqueue()``,
``.using()``, and ``resolve_task_callable()``) while carrying an immutable,
eagerly validated ``RayTaskContract`` for Ray scheduling, retry, timeout, and
RuntimeEnv policy introspection.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, overload

from django.tasks import task as django_task
from django.tasks.base import (
    DEFAULT_TASK_BACKEND_ALIAS,
    DEFAULT_TASK_PRIORITY,
    DEFAULT_TASK_QUEUE_NAME,
    Task,
)

from django_ray._result_tasks import _require_executable_task

_RAY_TASK_CONTRACT_ATTR = "__django_ray_task_contract__"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_MAX_QUEUE_NAME_LEN = 100
_MIN_PRIORITY = -100
_MAX_PRIORITY = 100
_MIN_MAX_ATTEMPTS = 1
_MAX_MAX_ATTEMPTS = 100
_MIN_RETRY_BACKOFF_SECONDS = 0
_MAX_RETRY_BACKOFF_SECONDS = 3600


class _UnsetType:
    """Sentinel distinguishing omitted decorator parameters from explicit values."""

    __slots__ = ()


_UNSET = _UnsetType()


class RayTaskContractError(ValueError):
    """Raised when a ``RayTaskContract`` or ``@ray_task`` declaration is invalid."""


def _validate_non_negative_resource(name: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RayTaskContractError(f"{name} must be a finite non-negative number or None")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise RayTaskContractError(f"{name} must be a finite non-negative number or None")
    return normalized


def _validate_positive_int(
    name: str,
    value: object,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        if maximum is not None:
            raise RayTaskContractError(
                f"{name} must be an integer between {minimum} and {maximum}, or None"
            )
        raise RayTaskContractError(f"{name} must be a positive integer or None")
    return value


def _validate_strict_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise RayTaskContractError(f"{name} must be a boolean")
    return value


def _validate_non_empty_name(
    name: str,
    value: object,
    *,
    max_length: int | None = None,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RayTaskContractError(f"{name} must be a non-empty string without outer whitespace")
    if max_length is not None and len(value) > max_length:
        raise RayTaskContractError(f"{name} must be at most {max_length} characters")
    return value


def _validate_runtime_env_profile(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _PROFILE_NAME_RE.fullmatch(value) is None:
        raise RayTaskContractError(
            "runtime_env_profile must be None or 1-100 characters matching "
            "'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$'"
        )
    return value


@dataclass(frozen=True, slots=True)
class RayTaskContract:
    """Immutable, eagerly validated execution contract for a ``@ray_task``.

    Describes per-task Ray scheduling resources, retry policy, execution
    timeout, RuntimeEnv profile, and Django Tasks routing defaults without
    altering the underlying ``django.tasks.base.Task`` runtime type.
    """

    num_cpus: float | None = None
    num_gpus: float | None = None
    memory: int | None = None
    max_attempts: int | None = None
    retry_backoff_seconds: int | None = None
    retry_jitter: bool = True
    timeout_seconds: int | None = None
    runtime_env_profile: str | None = None
    queue_name: str = DEFAULT_TASK_QUEUE_NAME
    priority: int = DEFAULT_TASK_PRIORITY
    takes_context: bool = False
    backend: str = DEFAULT_TASK_BACKEND_ALIAS

    def __post_init__(self) -> None:
        normalized_cpus = _validate_non_negative_resource("num_cpus", self.num_cpus)
        normalized_gpus = _validate_non_negative_resource("num_gpus", self.num_gpus)
        normalized_memory = _validate_positive_int("memory", self.memory, minimum=1)
        normalized_attempts = _validate_positive_int(
            "max_attempts",
            self.max_attempts,
            minimum=_MIN_MAX_ATTEMPTS,
            maximum=_MAX_MAX_ATTEMPTS,
        )
        normalized_backoff = _validate_positive_int(
            "retry_backoff_seconds",
            self.retry_backoff_seconds,
            minimum=_MIN_RETRY_BACKOFF_SECONDS,
            maximum=_MAX_RETRY_BACKOFF_SECONDS,
        )
        normalized_jitter = _validate_strict_bool("retry_jitter", self.retry_jitter)
        normalized_timeout = _validate_positive_int(
            "timeout_seconds",
            self.timeout_seconds,
            minimum=1,
        )
        normalized_profile = _validate_runtime_env_profile(self.runtime_env_profile)
        normalized_queue = _validate_non_empty_name(
            "queue_name",
            self.queue_name,
            max_length=_MAX_QUEUE_NAME_LEN,
        )
        if type(self.priority) is not int or not _MIN_PRIORITY <= self.priority <= _MAX_PRIORITY:
            raise RayTaskContractError(
                f"priority must be an integer between {_MIN_PRIORITY} and {_MAX_PRIORITY}"
            )
        normalized_takes_context = _validate_strict_bool("takes_context", self.takes_context)
        normalized_backend = _validate_non_empty_name("backend", self.backend)

        object.__setattr__(self, "num_cpus", normalized_cpus)
        object.__setattr__(self, "num_gpus", normalized_gpus)
        object.__setattr__(self, "memory", normalized_memory)
        object.__setattr__(self, "max_attempts", normalized_attempts)
        object.__setattr__(self, "retry_backoff_seconds", normalized_backoff)
        object.__setattr__(self, "retry_jitter", normalized_jitter)
        object.__setattr__(self, "timeout_seconds", normalized_timeout)
        object.__setattr__(self, "runtime_env_profile", normalized_profile)
        object.__setattr__(self, "queue_name", normalized_queue)
        object.__setattr__(self, "takes_context", normalized_takes_context)
        object.__setattr__(self, "backend", normalized_backend)

    def to_ray_options(self) -> dict[str, Any]:
        """Return non-None Ray scheduling resource options for this task."""
        options: dict[str, Any] = {}
        if self.num_cpus is not None:
            options["num_cpus"] = self.num_cpus
        if self.num_gpus is not None:
            options["num_gpus"] = self.num_gpus
        if self.memory is not None:
            options["memory"] = self.memory
        return options

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical, JSON-safe dictionary snapshot of this contract."""
        return {
            "backend": self.backend,
            "max_attempts": self.max_attempts,
            "memory": self.memory,
            "num_cpus": self.num_cpus,
            "num_gpus": self.num_gpus,
            "priority": self.priority,
            "queue_name": self.queue_name,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "retry_jitter": self.retry_jitter,
            "runtime_env_profile": self.runtime_env_profile,
            "takes_context": self.takes_context,
            "timeout_seconds": self.timeout_seconds,
        }


def get_ray_task_contract(task_or_func: Any) -> RayTaskContract | None:
    """Return the immutable ``RayTaskContract`` attached to a task or function.

    When passed a Django ``Task`` created (or modified via ``.using(...)``) from
    a ``@ray_task``-decorated callable, the returned contract reflects the
    ``Task`` instance's current ``queue_name``, ``priority``, ``backend``, and
    ``takes_context`` attributes. Returns ``None`` for plain tasks or functions.
    """
    if isinstance(task_or_func, Task):
        base = getattr(task_or_func.func, _RAY_TASK_CONTRACT_ATTR, None)
        if not isinstance(base, RayTaskContract):
            return None
        if (
            base.queue_name == task_or_func.queue_name
            and base.priority == task_or_func.priority
            and base.backend == task_or_func.backend
            and base.takes_context == task_or_func.takes_context
        ):
            return base
        return replace(
            base,
            queue_name=task_or_func.queue_name,
            priority=task_or_func.priority,
            backend=task_or_func.backend,
            takes_context=task_or_func.takes_context,
        )

    contract = getattr(task_or_func, _RAY_TASK_CONTRACT_ATTR, None)
    return contract if isinstance(contract, RayTaskContract) else None


@overload
def ray_task(
    function: Callable[..., Any] | Task,
    *,
    num_cpus: int | float | None = None,
    num_gpus: int | float | None = None,
    memory: int | None = None,
    max_attempts: int | None = None,
    retry_backoff_seconds: int | None = None,
    retry_jitter: bool = True,
    timeout_seconds: int | None = None,
    runtime_env_profile: str | None = None,
    queue_name: str = DEFAULT_TASK_QUEUE_NAME,
    priority: int = DEFAULT_TASK_PRIORITY,
    takes_context: bool = False,
    backend: str = DEFAULT_TASK_BACKEND_ALIAS,
) -> Task: ...


@overload
def ray_task(
    function: None = None,
    *,
    num_cpus: int | float | None = None,
    num_gpus: int | float | None = None,
    memory: int | None = None,
    max_attempts: int | None = None,
    retry_backoff_seconds: int | None = None,
    retry_jitter: bool = True,
    timeout_seconds: int | None = None,
    runtime_env_profile: str | None = None,
    queue_name: str = DEFAULT_TASK_QUEUE_NAME,
    priority: int = DEFAULT_TASK_PRIORITY,
    takes_context: bool = False,
    backend: str = DEFAULT_TASK_BACKEND_ALIAS,
) -> Callable[[Callable[..., Any] | Task], Task]: ...


def ray_task(
    function: Callable[..., Any] | Task | None = None,
    *,
    num_cpus: int | float | None = None,
    num_gpus: int | float | None = None,
    memory: int | None = None,
    max_attempts: int | None = None,
    retry_backoff_seconds: int | None = None,
    retry_jitter: bool = True,
    timeout_seconds: int | None = None,
    runtime_env_profile: str | None = None,
    queue_name: str | _UnsetType = _UNSET,
    priority: int | _UnsetType = _UNSET,
    takes_context: bool | _UnsetType = _UNSET,
    backend: str | _UnsetType = _UNSET,
) -> Task | Callable[[Callable[..., Any] | Task], Task]:
    """Decorate a callable as a genuine Django 6 ``Task`` with a ``RayTaskContract``.

    Supports both bare ``@ray_task`` and parameterized ``@ray_task(...)`` usage.
    Contract parameters are validated eagerly when the decorator is constructed.
    """
    resolved_queue = DEFAULT_TASK_QUEUE_NAME if isinstance(queue_name, _UnsetType) else queue_name
    resolved_priority = DEFAULT_TASK_PRIORITY if isinstance(priority, _UnsetType) else priority
    resolved_takes_context = False if isinstance(takes_context, _UnsetType) else takes_context
    resolved_backend = DEFAULT_TASK_BACKEND_ALIAS if isinstance(backend, _UnsetType) else backend

    # Validate eagerly at decoration / factory call time so invalid resources,
    # retry settings, or timeouts fail immediately on import.
    eager_contract = RayTaskContract(
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory,
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_jitter=retry_jitter,
        timeout_seconds=timeout_seconds,
        runtime_env_profile=runtime_env_profile,
        queue_name=resolved_queue,
        priority=resolved_priority,
        takes_context=resolved_takes_context,
        backend=resolved_backend,
    )

    def _decorate(target: Callable[..., Any] | Task) -> Task:
        if isinstance(target, Task):
            _require_executable_task(target)
            raw_func = target.func
            effective_queue = (
                target.queue_name if isinstance(queue_name, _UnsetType) else resolved_queue
            )
            effective_priority = (
                target.priority if isinstance(priority, _UnsetType) else resolved_priority
            )
            effective_takes_context = (
                target.takes_context
                if isinstance(takes_context, _UnsetType)
                else resolved_takes_context
            )
            effective_backend = (
                target.backend if isinstance(backend, _UnsetType) else resolved_backend
            )
        else:
            if not callable(target):
                raise TypeError("@ray_task requires a callable function or Django Task")
            raw_func = target
            effective_queue = resolved_queue
            effective_priority = resolved_priority
            effective_takes_context = resolved_takes_context
            effective_backend = resolved_backend

        effective_contract = replace(
            eager_contract,
            queue_name=effective_queue,
            priority=effective_priority,
            takes_context=effective_takes_context,
            backend=effective_backend,
        )

        wrapped_task = django_task(
            raw_func,
            priority=effective_contract.priority,
            queue_name=effective_contract.queue_name,
            backend=effective_contract.backend,
            takes_context=effective_contract.takes_context,
        )
        setattr(raw_func, _RAY_TASK_CONTRACT_ATTR, effective_contract)
        return wrapped_task

    if function is not None:
        return _decorate(function)
    return _decorate


__all__ = [
    "RayTaskContract",
    "RayTaskContractError",
    "get_ray_task_contract",
    "ray_task",
]
