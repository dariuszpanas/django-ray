"""Small, defensive helpers for keeping task data out of operational output.

Task arguments, return values, and exception messages are application data.  The
helpers in this module are deliberately conservative: mappings redact values
whose keys look sensitive, strings matching a configured expression are
replaced as a whole, and objects which cannot be represented as JSON are shown
only by type.  Redaction is intended for logs and operator-facing views; it is
not encryption or a replacement for access control on the task database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"

# These are intentionally key/value expressions rather than a list of concrete
# secrets.  Applications should add domain-specific expressions through
# DJANGO_RAY["REDACT_PATTERNS"].
DEFAULT_REDACT_PATTERNS: tuple[str, ...] = (
    r"password",
    r"passwd",
    r"secret",
    r"token",
    r"api[_-]?key",
    r"authorization",
    r"cookie",
    r"credential",
    r"private[_-]?key",
)


def _configured_patterns(
    patterns: Sequence[str] | str | None = None,
) -> tuple[re.Pattern[str], ...]:
    """Compile configured patterns, falling back safely outside Django."""
    if patterns is None:
        try:
            from django_ray.conf.settings import get_settings

            patterns = get_settings().get("REDACT_PATTERNS")
        except Exception:
            patterns = None
    if patterns is None:
        patterns = DEFAULT_REDACT_PATTERNS
    elif isinstance(patterns, str):
        patterns = (*DEFAULT_REDACT_PATTERNS, patterns)
    else:
        patterns = (*DEFAULT_REDACT_PATTERNS, *patterns)
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def _matches(value: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def _safe_type(value: Any) -> str:
    value_type = type(value)
    return f"<{value_type.__module__}.{value_type.__qualname__}>"


def redact_value(
    value: Any,
    *,
    patterns: Sequence[str] | str | None = None,
    _compiled: tuple[re.Pattern[str], ...] | None = None,
    _depth: int = 0,
) -> Any:
    """Return a JSON-compatible value with configured sensitive data removed.

    Nested mappings and sequences are traversed.  A mapping key matching a
    pattern redacts its value, while a matching string is replaced in full.
    Cycles and very deep objects are represented by a type marker.
    """
    compiled = _configured_patterns(patterns) if _compiled is None else _compiled
    if _depth > 20:
        return "<max-depth>"
    if isinstance(value, str):
        return REDACTED if _matches(value, compiled) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            output[key_text] = (
                REDACTED
                if _matches(key_text, compiled)
                else redact_value(item, _compiled=compiled, _depth=_depth + 1)
            )
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item, _compiled=compiled, _depth=_depth + 1) for item in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return _safe_type(value)
    return _safe_type(value)


def redact_text(value: Any, *, patterns: Sequence[str] | str | None = None) -> str:
    """Redact a message or exception string without exposing arbitrary objects."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, BaseException):
        text = f"{type(value).__name__}: {value}"
    else:
        text = str(value)
    compiled = _configured_patterns(patterns)
    return REDACTED if _matches(text, compiled) else text


def redact_exception(value: BaseException, *, patterns: Sequence[str] | str | None = None) -> str:
    """Return a compact, redacted exception description for structured logs."""
    return f"{type(value).__name__}: {redact_text(str(value), patterns=patterns)}"


def safe_json_dumps(value: Any, *, patterns: Sequence[str] | str | None = None) -> str:
    """Serialize redacted data without falling back to secret-bearing ``str``."""
    return json.dumps(redact_value(value, patterns=patterns), default=_safe_type)


def result_metadata(value: Any) -> dict[str, Any]:
    """Return bounded metadata for a result without serializing its contents."""
    try:
        size_bytes = len(json.dumps(value, default=_safe_type).encode("utf-8"))
    except Exception:
        size_bytes = None
    value_type = type(value)
    return {
        "result_type": f"{value_type.__module__}.{value_type.__qualname__}",
        "result_size_bytes": size_bytes,
    }


__all__ = [
    "DEFAULT_REDACT_PATTERNS",
    "REDACTED",
    "redact_exception",
    "redact_text",
    "redact_value",
    "result_metadata",
    "safe_json_dumps",
]
