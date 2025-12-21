"""Utilities for redacting sensitive data in logs and results."""

from __future__ import annotations

import re
from typing import Any

from django_ray.conf.settings import get_settings

# Default patterns for sensitive keys
DEFAULT_SENSITIVE_PATTERNS = [
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"api_key", re.IGNORECASE),
    re.compile(r"apikey", re.IGNORECASE),
    re.compile(r"auth", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
]

REDACTED_VALUE = "[REDACTED]"


def is_sensitive_key(key: str) -> bool:
    """Check if a key matches sensitive patterns.

    Args:
        key: The key name to check.

    Returns:
        True if the key matches a sensitive pattern.
    """
    settings = get_settings()
    patterns = settings.get("REDACT_PATTERNS") or DEFAULT_SENSITIVE_PATTERNS

    for pattern in patterns:
        if isinstance(pattern, re.Pattern):
            if pattern.search(key):
                return True
        elif isinstance(pattern, str):
            if pattern.lower() in key.lower():
                return True

    return False


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive values from a dictionary.

    Args:
        data: Dictionary to redact.

    Returns:
        New dictionary with sensitive values redacted.
    """
    result = {}
    for key, value in data.items():
        if is_sensitive_key(str(key)):
            result[key] = REDACTED_VALUE
        elif isinstance(value, dict):
            result[key] = redact_dict(value)
        elif isinstance(value, list):
            result[key] = redact_list(value)
        else:
            result[key] = value
    return result


def redact_list(data: list[Any]) -> list[Any]:
    """Redact sensitive values from a list.

    Args:
        data: List to redact.

    Returns:
        New list with sensitive values in nested dicts redacted.
    """
    result = []
    for item in data:
        if isinstance(item, dict):
            result.append(redact_dict(item))
        elif isinstance(item, list):
            result.append(redact_list(item))
        else:
            result.append(item)
    return result
