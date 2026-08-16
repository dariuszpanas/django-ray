"""Compatibility helpers for physically relocated workflow definitions."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any


def preserve_legacy_module_identity(
    namespace: dict[str, Any],
    *,
    exports: Iterable[str],
    legacy_module: str,
) -> None:
    """Keep released pickle identities while definitions move into packages."""
    defining_module = namespace["__name__"]
    for name in exports:
        value = namespace[name]
        if (
            inspect.isclass(value) or inspect.isfunction(value)
        ) and value.__module__ == defining_module:
            value.__module__ = legacy_module


__all__ = ["preserve_legacy_module_identity"]
