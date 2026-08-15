"""Strict, opt-in workflow node output previews.

Preview projection runs beside the workflow leaf that already owns the result.
Only an explicitly returned, bounded JSON value may cross the progress channel;
arbitrary task results, Ray handles, and diagnostic ``repr`` output never do.
"""

from __future__ import annotations

import inspect
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

from django_ray.redaction import REDACTED, normalize_terminal_text, redact_value

WORKFLOW_OUTPUT_PREVIEW_SCHEMA_VERSION = 1
WORKFLOW_OUTPUT_PREVIEW_LIMITS_PROFILE = "v1"
WORKFLOW_OUTPUT_PREVIEW_MAX_DEPTH = 4
WORKFLOW_OUTPUT_PREVIEW_MAX_ITEMS = 32
WORKFLOW_OUTPUT_PREVIEW_MAX_MAPPING_ITEMS = 15
WORKFLOW_OUTPUT_PREVIEW_MAX_SEQUENCE_ITEMS = 16
WORKFLOW_OUTPUT_PREVIEW_MAX_KEY_BYTES = 64
WORKFLOW_OUTPUT_PREVIEW_MAX_STRING_BYTES = 256
WORKFLOW_OUTPUT_PREVIEW_MAX_ENCODED_BYTES = 512
WORKFLOW_OUTPUT_PREVIEW_MAX_DECODED_BYTES = 2 * 1024
WORKFLOW_OUTPUT_PREVIEW_MAX_INTEGER = (1 << 53) - 1

_PREVIEW_FIELDS = frozenset({"schema_version", "availability", "value"})
_VALUE_AVAILABILITIES = frozenset({"AVAILABLE", "REDACTED"})


class WorkflowOutputPreviewAvailability(StrEnum):
    """Stable reasons a node output preview is or is not present."""

    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    AVAILABLE = "AVAILABLE"
    REDACTED = "REDACTED"
    TOO_LARGE = "TOO_LARGE"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    OMITTED_BY_POLICY = "OMITTED_BY_POLICY"


class WorkflowOutputPreviewError(ValueError):
    """Raised when an untrusted preview envelope violates the public contract."""


class _PreviewLimitError(WorkflowOutputPreviewError):
    pass


class _PreviewUnsupportedError(WorkflowOutputPreviewError):
    pass


@dataclass
class _PreviewBudget:
    items: int = 0
    decoded_bytes: int = 0

    def consume(self, *, items: int = 1, decoded_bytes: int = 0) -> None:
        self.items += items
        self.decoded_bytes += decoded_bytes
        if self.items > WORKFLOW_OUTPUT_PREVIEW_MAX_ITEMS:
            raise _PreviewLimitError("workflow output preview exceeds its item limit")
        if self.decoded_bytes > WORKFLOW_OUTPUT_PREVIEW_MAX_DECODED_BYTES:
            raise _PreviewLimitError("workflow output preview exceeds its decoded byte limit")


def _unsupported(message: str) -> NoReturn:
    raise _PreviewUnsupportedError(message)


def _utf8(value: str, name: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _PreviewUnsupportedError(f"{name} must contain valid UTF-8") from error


def _normalize_value(value: Any, *, depth: int, budget: _PreviewBudget) -> Any:
    if depth > WORKFLOW_OUTPUT_PREVIEW_MAX_DEPTH:
        raise _PreviewLimitError("workflow output preview exceeds its depth limit")

    value_type = type(value)
    if value is None:
        budget.consume(decoded_bytes=4)
        return None
    if value_type is bool:
        budget.consume(decoded_bytes=1)
        return value
    if value_type is int:
        if not -WORKFLOW_OUTPUT_PREVIEW_MAX_INTEGER <= value <= WORKFLOW_OUTPUT_PREVIEW_MAX_INTEGER:
            raise _PreviewLimitError("workflow output preview integer exceeds its range")
        budget.consume(decoded_bytes=8)
        return value
    if value_type is float:
        if not math.isfinite(value):
            _unsupported("workflow output preview numbers must be finite")
        if value.is_integer() and abs(value) > WORKFLOW_OUTPUT_PREVIEW_MAX_INTEGER:
            raise _PreviewLimitError(
                "workflow output preview integer-valued number exceeds its range"
            )
        budget.consume(decoded_bytes=8)
        return value
    if value_type is str:
        encoded = _utf8(value, "workflow output preview string")
        if len(encoded) > WORKFLOW_OUTPUT_PREVIEW_MAX_STRING_BYTES:
            raise _PreviewLimitError("workflow output preview contains an oversized string")
        budget.consume(decoded_bytes=16 + len(encoded))
        return value
    if value_type is list:
        if len(value) > WORKFLOW_OUTPUT_PREVIEW_MAX_SEQUENCE_ITEMS:
            raise _PreviewLimitError("workflow output preview sequence exceeds its item limit")
        budget.consume(decoded_bytes=16)
        return [_normalize_value(item, depth=depth + 1, budget=budget) for item in value]
    if value_type is dict:
        if len(value) > WORKFLOW_OUTPUT_PREVIEW_MAX_MAPPING_ITEMS:
            raise _PreviewLimitError("workflow output preview mapping exceeds its item limit")
        budget.consume(decoded_bytes=24)
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                _unsupported("workflow output preview mapping keys must be exact strings")
            encoded_key = _utf8(key, "workflow output preview key")
            if not encoded_key or len(encoded_key) > WORKFLOW_OUTPUT_PREVIEW_MAX_KEY_BYTES:
                raise _PreviewLimitError("workflow output preview contains an oversized key")
            budget.consume(decoded_bytes=8 + len(encoded_key))
            normalized[key] = _normalize_value(item, depth=depth + 1, budget=budget)
        return normalized
    _unsupported("workflow output preview contains an unsupported value type")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise _PreviewUnsupportedError("workflow output preview is not canonical JSON") from error


def _normalize_bounded_value(value: Any) -> Any:
    normalized = _normalize_value(value, depth=0, budget=_PreviewBudget())
    if len(_canonical_bytes(normalized)) > WORKFLOW_OUTPUT_PREVIEW_MAX_ENCODED_BYTES:
        raise _PreviewLimitError("workflow output preview exceeds its encoded byte limit")
    return normalized


def _normalize_terminal_value(value: Any) -> Any:
    """Return the inert display baseline without applying redaction policy."""
    if type(value) is str:
        return normalize_terminal_text(value)
    if type(value) is list:
        return [_normalize_terminal_value(item) for item in value]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = normalize_terminal_text(key)
            normalized_item = _normalize_terminal_value(item)
            # Distinct raw keys can collapse to one inert display key. Mirror
            # redact_value's fail-closed collision behavior without applying
            # the configured redaction patterns to ordinary terminal text.
            normalized[normalized_key] = (
                REDACTED if normalized_key in normalized else normalized_item
            )
        return normalized
    return value


def _preview(availability: str, value: Any = None) -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_OUTPUT_PREVIEW_SCHEMA_VERSION,
        "availability": availability,
        "value": value,
    }


def unavailable_workflow_output_preview(
    availability: WorkflowOutputPreviewAvailability | str,
) -> dict[str, Any]:
    """Build one value-free preview status after validating its availability."""
    try:
        normalized = WorkflowOutputPreviewAvailability(availability)
    except (TypeError, ValueError) as error:
        raise WorkflowOutputPreviewError(
            "workflow output preview availability is unsupported"
        ) from error
    if normalized.value in _VALUE_AVAILABILITIES:
        raise WorkflowOutputPreviewError("available workflow output preview requires a value")
    return _preview(normalized.value)


def prepare_workflow_output_preview(value: Any) -> dict[str, Any]:
    """Prepare one author-projected value without inspecting unsupported objects."""
    try:
        bounded = _normalize_bounded_value(value)
        normalized = _normalize_bounded_value(_normalize_terminal_value(bounded))
        redacted = _normalize_bounded_value(redact_value(bounded))
        if _contains_sensitive_key(bounded):
            return _preview(WorkflowOutputPreviewAvailability.REDACTED.value, REDACTED)
    except _PreviewLimitError:
        return _preview(WorkflowOutputPreviewAvailability.TOO_LARGE.value)
    except _PreviewUnsupportedError:
        return _preview(WorkflowOutputPreviewAvailability.UNSUPPORTED.value)
    except Exception:
        return _preview(WorkflowOutputPreviewAvailability.UNSUPPORTED.value)

    availability = (
        WorkflowOutputPreviewAvailability.REDACTED
        if redacted != normalized or _contains_redaction(redacted)
        else WorkflowOutputPreviewAvailability.AVAILABLE
    )
    return _preview(availability.value, redacted)


def project_workflow_output_preview(projector: Any, result: Any) -> dict[str, Any]:
    """Run one trusted author projector without allowing it to replace task success."""
    try:
        if not callable(projector) or any(
            predicate(projector)
            for predicate in (
                inspect.iscoroutinefunction,
                inspect.isgeneratorfunction,
                inspect.isasyncgenfunction,
            )
        ):
            return _preview(WorkflowOutputPreviewAvailability.UNSUPPORTED.value)
        projected = projector(result)
    except Exception:
        return _preview(WorkflowOutputPreviewAvailability.FAILED.value)
    return prepare_workflow_output_preview(projected)


def _contains_redaction(value: Any) -> bool:
    if value == REDACTED:
        return True
    if type(value) is list:
        return any(_contains_redaction(item) for item in value)
    if type(value) is dict:
        return any(_contains_redaction(item) for item in value.values())
    return False


def _contains_sensitive_key(value: Any) -> bool:
    if type(value) is list:
        return any(_contains_sensitive_key(item) for item in value)
    if type(value) is dict:
        return any(
            redact_value(key) == REDACTED or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    return False


def _validate_workflow_output_preview(
    value: Any,
    *,
    enforce_current_redaction: bool,
    apply_current_presentation: bool = True,
) -> dict[str, Any]:
    """Validate one preview under either its stored or current presentation rules."""
    if type(value) is not dict or set(value) != _PREVIEW_FIELDS:
        raise WorkflowOutputPreviewError(
            "workflow output preview must contain the exact protocol fields"
        )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != WORKFLOW_OUTPUT_PREVIEW_SCHEMA_VERSION
    ):
        raise WorkflowOutputPreviewError("workflow output preview schema version is unsupported")
    if type(value["availability"]) is not str:
        raise WorkflowOutputPreviewError("workflow output preview availability is unsupported")
    try:
        availability = WorkflowOutputPreviewAvailability(value["availability"])
    except (TypeError, ValueError) as error:
        raise WorkflowOutputPreviewError(
            "workflow output preview availability is unsupported"
        ) from error

    preview_value = value["value"]
    if availability.value not in _VALUE_AVAILABILITIES:
        if preview_value is not None:
            raise WorkflowOutputPreviewError(
                "unavailable workflow output preview must not contain a value"
            )
        return _preview(availability.value)

    bounded = _normalize_bounded_value(preview_value)
    normalized = (
        _normalize_bounded_value(_normalize_terminal_value(bounded))
        if apply_current_presentation
        else bounded
    )
    if availability is WorkflowOutputPreviewAvailability.REDACTED and not _contains_redaction(
        normalized
    ):
        raise WorkflowOutputPreviewError(
            "redacted workflow output preview lacks redaction evidence"
        )
    if availability is WorkflowOutputPreviewAvailability.AVAILABLE and _contains_redaction(
        normalized
    ):
        raise WorkflowOutputPreviewError(
            "available workflow output preview contains a redaction marker"
        )
    if enforce_current_redaction:
        if _contains_sensitive_key(bounded):
            raise WorkflowOutputPreviewError(
                "workflow output preview contains a sensitive-looking key"
            )
        redacted = _normalize_bounded_value(redact_value(bounded))
        if redacted != normalized:
            raise WorkflowOutputPreviewError(
                "workflow output preview was not redacted before publication"
            )
    return _preview(availability.value, normalized)


def validate_workflow_output_preview(value: Any) -> dict[str, Any]:
    """Strictly validate one preview before publication or untrusted projection."""
    return _validate_workflow_output_preview(value, enforce_current_redaction=True)


def read_workflow_output_preview(value: Any) -> dict[str, Any]:
    """Validate stored bytes and suppress a value newly covered by redaction policy.

    Stored payload authentication happens before this read projection. A policy
    change never rewrites those bytes: if the current policy would redact any
    part of a formerly valid value, readers receive one existing, value-safe
    ``REDACTED`` marker instead of the historical projection.
    """
    preview = _validate_workflow_output_preview(
        value,
        enforce_current_redaction=False,
        apply_current_presentation=False,
    )
    if preview["availability"] not in _VALUE_AVAILABILITIES:
        return preview
    try:
        bounded = _normalize_bounded_value(value["value"])
        normalized = _normalize_bounded_value(_normalize_terminal_value(bounded))
        redacted = _normalize_bounded_value(redact_value(bounded))
        if _contains_sensitive_key(bounded):
            return _preview(WorkflowOutputPreviewAvailability.REDACTED.value, REDACTED)
    except Exception:
        return _preview(WorkflowOutputPreviewAvailability.REDACTED.value, REDACTED)
    if redacted != normalized or (
        preview["availability"] == WorkflowOutputPreviewAvailability.AVAILABLE.value
        and _contains_redaction(redacted)
    ):
        return _preview(WorkflowOutputPreviewAvailability.REDACTED.value, REDACTED)
    return _preview(preview["availability"], redacted)


__all__ = [
    "WORKFLOW_OUTPUT_PREVIEW_LIMITS_PROFILE",
    "WORKFLOW_OUTPUT_PREVIEW_MAX_DECODED_BYTES",
    "WORKFLOW_OUTPUT_PREVIEW_MAX_DEPTH",
    "WORKFLOW_OUTPUT_PREVIEW_MAX_ENCODED_BYTES",
    "WORKFLOW_OUTPUT_PREVIEW_MAX_ITEMS",
    "WORKFLOW_OUTPUT_PREVIEW_SCHEMA_VERSION",
    "WorkflowOutputPreviewAvailability",
    "WorkflowOutputPreviewError",
    "prepare_workflow_output_preview",
    "project_workflow_output_preview",
    "read_workflow_output_preview",
    "unavailable_workflow_output_preview",
    "validate_workflow_output_preview",
]
