"""Entrypoint for Ray to execute Django Tasks.

This module bootstraps Django and executes the task callable.
"""

from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass
from typing import Any

import django
from django.apps import apps


@dataclass
class TaskResult:
    """Structured result from task execution."""

    success: bool
    result: Any | None = None
    error: str | None = None
    traceback: str | None = None


def bootstrap_django() -> None:
    """Bootstrap Django environment for task execution."""
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if not settings_module:
        raise RuntimeError("DJANGO_SETTINGS_MODULE environment variable is not set")

    if not apps.ready:
        django.setup()


def execute_task(
    callable_path: str,
    serialized_args: str,
    serialized_kwargs: str,
) -> str:
    """Execute a Django Task and return JSON result.

    Args:
        callable_path: Dotted path to the task callable.
        serialized_args: JSON-serialized positional arguments.
        serialized_kwargs: JSON-serialized keyword arguments.

    Returns:
        JSON-serialized TaskResult.
    """
    from django_ray.runtime.import_utils import import_callable
    from django_ray.runtime.serialization import deserialize_args

    try:
        bootstrap_django()

        callable_obj = import_callable(callable_path)
        args = deserialize_args(serialized_args)
        kwargs = deserialize_args(serialized_kwargs)

        result = callable_obj(*args, **kwargs)

        return json.dumps(
            {
                "success": True,
                "result": result,
                "error": None,
                "traceback": None,
            }
        )

    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "result": None,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
        )
