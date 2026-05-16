"""Entrypoint for Ray to execute Django Tasks.

This module bootstraps Django and executes the task callable.
"""

from __future__ import annotations

import argparse
import base64
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
    exception_type: str | None = None


def _serialize_error(e: Exception) -> str:
    """Serialize an exception as a task result JSON string."""
    return json.dumps(
        {
            "success": False,
            "result": None,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "exception_type": type(e).__module__ + "." + type(e).__name__,
        }
    )


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
                "exception_type": None,
            }
        )

    except Exception as e:
        return _serialize_error(e)


def execute_task_from_payload(payload_b64: str) -> str:
    """Decode payload and execute the task.

    Args:
        payload_b64: URL-safe base64 JSON payload containing:
            - callable_path
            - serialized_args
            - serialized_kwargs

    Returns:
        JSON-serialized TaskResult.
    """
    try:
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        payload = json.loads(payload_json)
        return execute_task(
            callable_path=payload["callable_path"],
            serialized_args=payload["serialized_args"],
            serialized_kwargs=payload["serialized_kwargs"],
        )
    except Exception as e:
        return _serialize_error(e)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Ray Job execution."""
    parser = argparse.ArgumentParser(description="Execute a django-ray task payload")
    parser.add_argument(
        "--payload-b64",
        required=True,
        help="URL-safe base64 encoded task payload",
    )
    args = parser.parse_args(argv)

    print(execute_task_from_payload(args.payload_b64))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
