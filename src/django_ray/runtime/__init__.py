"""Runtime execution components for django-ray.

This module contains the entrypoint and utilities that Ray calls
to execute Django Tasks.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django_ray.runtime.entrypoint import execute_task
    from django_ray.runtime.import_utils import import_callable
    from django_ray.runtime.serialization import deserialize_args, serialize_args

__all__ = [
    "execute_task",
    "import_callable",
    "serialize_args",
    "deserialize_args",
]


def __getattr__(name: str) -> Any:
    # Importing a private pre-Django guard must not initialize the application.
    # Existing public exports resolve to their original objects when requested.
    modules = {
        "execute_task": "entrypoint",
        "import_callable": "import_utils",
        "serialize_args": "serialization",
        "deserialize_args": "serialization",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{modules[name]}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
