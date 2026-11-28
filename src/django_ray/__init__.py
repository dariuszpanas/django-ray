"""Django Ray - Ray.io integration with Django for distributed computing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.8.0"

__all__ = [
    "RayTaskContract",
    "RayTaskContractError",
    "RayTaskResult",
    "ResultUnavailableError",
    "__version__",
    "get_ray_task_contract",
    "ray_task",
]

if TYPE_CHECKING:
    from django_ray.backends import RayTaskResult, ResultUnavailableError
    from django_ray.decorators import (
        RayTaskContract,
        RayTaskContractError,
        get_ray_task_contract,
        ray_task,
    )

_BACKEND_EXPORTS = frozenset({"RayTaskResult", "ResultUnavailableError"})
_DECORATOR_EXPORTS = frozenset(
    {
        "RayTaskContract",
        "RayTaskContractError",
        "get_ray_task_contract",
        "ray_task",
    }
)


def __getattr__(name: str) -> Any:
    if name in _BACKEND_EXPORTS:
        from django_ray import backends

        return getattr(backends, name)
    if name in _DECORATOR_EXPORTS:
        from django_ray import decorators

        return getattr(decorators, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
