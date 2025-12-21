"""Serialization utilities for task arguments."""

from __future__ import annotations

import json
from typing import Any


class SerializationError(Exception):
    """Raised when serialization or deserialization fails."""


def serialize_args(data: Any) -> str:
    """Serialize arguments to JSON string.

    Args:
        data: Data to serialize (must be JSON-serializable).

    Returns:
        JSON string representation.

    Raises:
        SerializationError: If data cannot be serialized.
    """
    try:
        return json.dumps(data)
    except (TypeError, ValueError) as e:
        raise SerializationError(f"Cannot serialize data: {e}") from e


def deserialize_args(data: str) -> Any:
    """Deserialize JSON string to Python objects.

    Args:
        data: JSON string to deserialize.

    Returns:
        Deserialized Python object.

    Raises:
        SerializationError: If data cannot be deserialized.
    """
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError) as e:
        raise SerializationError(f"Cannot deserialize data: {e}") from e
