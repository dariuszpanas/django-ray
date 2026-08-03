"""Bounded display helpers for package-owned management commands.

Terminal parsing and configured-pattern matching belong to the package-wide
redaction boundary.  This module only materializes the narrow values accepted
by management commands and applies their smaller console byte and item caps.
"""

from __future__ import annotations

from django_ray.redaction import redact_text, safe_exception_type_name

CONSOLE_DIAGNOSTIC_MAX_BYTES = 4096
_CONSOLE_RESOURCE_MAX_ITEMS = 256
_TRUNCATED = " [TRUNCATED]"


def _truncate_utf8(value: str, *, limit: int = CONSOLE_DIAGNOSTIC_MAX_BYTES) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    suffix = _TRUNCATED.encode("ascii")
    prefix = encoded[: limit - len(suffix)].decode("utf-8", errors="ignore")
    return f"{prefix}{_TRUNCATED}"


def _materialize_diagnostic(value: str) -> str:
    """Materialize one accepted string without invoking an override."""
    if type(value) is str:
        return value
    if isinstance(value, str):
        return str.__str__(value)
    return "unsupported diagnostic value"  # pragma: no cover - type contract defense


def render_console_diagnostic(value: BaseException | str) -> str:
    """Render one untrusted diagnostic through the shared redaction boundary."""
    # Redact the complete accepted input before applying the smaller console
    # cap.  ``redact_text`` owns its aggregate matcher/input budgets and fails
    # closed when they are exhausted, so truncation cannot hide a configured
    # pattern or create a partial unredacted projection.
    materialized: BaseException | str = (
        value if isinstance(value, BaseException) else _materialize_diagnostic(value)
    )
    return _truncate_utf8(redact_text(materialized))


def render_exception_type_label(value: BaseException) -> str:
    """Render a type-only management diagnostic without invoking its message."""
    return redact_text(safe_exception_type_name(value))


def render_console_resource_summary(value: object) -> str:
    """Render bounded Ray resource names and numeric counts for the console."""
    if type(value) is not dict:
        return "<invalid Ray resource summary>"

    rendered_items: list[str] = []
    rendered_bytes = 2  # Opening and closing braces.
    for index, (name, count) in enumerate(value.items()):
        if index >= _CONSOLE_RESOURCE_MAX_ITEMS:
            rendered_items.append("...")
            break
        if type(name) is not str or type(count) not in {int, float}:
            rendered_item = "<invalid Ray resource entry>"
        else:
            safe_name = render_console_diagnostic(name)
            rendered_item = f"{safe_name!r}: {count!r}"
        rendered_items.append(rendered_item)
        rendered_bytes += len(rendered_item.encode("utf-8", errors="replace")) + 2
        if rendered_bytes > CONSOLE_DIAGNOSTIC_MAX_BYTES:
            break

    return _truncate_utf8("{" + ", ".join(rendered_items) + "}")


__all__ = [
    "CONSOLE_DIAGNOSTIC_MAX_BYTES",
    "render_console_diagnostic",
    "render_console_resource_summary",
    "render_exception_type_label",
]
