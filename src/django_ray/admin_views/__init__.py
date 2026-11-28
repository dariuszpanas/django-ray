"""Focused Django Admin view, action, badge, and sensitive-data modules."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "actions",
    "badges",
    "diagnostics",
    "sensitive",
]

_SUBMODULES = frozenset(__all__)


def __getattr__(name: str) -> Any:
    """Lazily expose admin_views submodules without eager import-time coupling."""
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
