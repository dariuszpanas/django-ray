"""Utilities for importing task callables by path."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable


def import_callable(dotted_path: str) -> Callable[..., Any]:
    """Import a callable from a dotted path.

    Args:
        dotted_path: Full dotted path to the callable (e.g., 'myapp.tasks.my_task').

    Returns:
        The imported callable.

    Raises:
        ImportError: If the module cannot be imported.
        AttributeError: If the callable is not found in the module.
        TypeError: If the imported object is not callable.
    """
    if "." not in dotted_path:
        raise ImportError(
            f"Invalid callable path '{dotted_path}': must contain at least one dot"
        )

    module_path, callable_name = dotted_path.rsplit(".", 1)

    try:
        module = import_module(module_path)
    except ImportError as e:
        raise ImportError(f"Could not import module '{module_path}': {e}") from e

    try:
        callable_obj = getattr(module, callable_name)
    except AttributeError as e:
        raise AttributeError(
            f"Module '{module_path}' has no attribute '{callable_name}'"
        ) from e

    if not callable(callable_obj):
        raise TypeError(f"'{dotted_path}' is not callable")

    return callable_obj
