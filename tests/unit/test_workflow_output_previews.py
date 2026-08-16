from __future__ import annotations

from typing import Any

import pytest

import django_ray.workflow.previews as previews_module
from django_ray.redaction import REDACTED
from django_ray.workflow.previews import (
    WORKFLOW_OUTPUT_PREVIEW_MAX_ENCODED_BYTES,
    WorkflowOutputPreviewAvailability,
    WorkflowOutputPreviewError,
    prepare_workflow_output_preview,
    project_workflow_output_preview,
    read_workflow_output_preview,
    unavailable_workflow_output_preview,
    validate_workflow_output_preview,
)


def preview_identity(value: Any) -> Any:
    return value


def preview_failure(_value: Any) -> Any:
    raise RuntimeError("projection failure must not replace task success")


def preview_interrupt(_value: Any) -> Any:
    raise KeyboardInterrupt("projection cancellation must propagate")


async def async_preview(_value: Any) -> dict[str, bool]:
    return {"unexpected": True}


class _NoRepresentation:
    def __repr__(self) -> str:
        raise AssertionError("output preview must never call repr")


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        42,
        4.5,
        "ready",
        ["ready", 2],
        list(range(16)),
        {"status": "ready", "rows": 2},
        {f"key-{index}": index for index in range(15)},
        {"a": {"b": {"c": {"d": True}}}},
        {"k" * 64: "x" * 256},
    ],
)
def test_explicit_projection_accepts_only_bounded_json_values(value: Any) -> None:
    preview = prepare_workflow_output_preview(value)

    assert preview == {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": value,
    }
    assert validate_workflow_output_preview(preview) == preview


def test_canonical_numeric_spelling_remains_within_the_backend_contract() -> None:
    value = {
        "numbers": [1e-6] * 16,
        "a": "x" * 200,
        "b": "y" * 142,
    }

    preview = prepare_workflow_output_preview(value)

    assert preview == {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": value,
    }


def test_configured_redaction_is_applied_before_preview_publication() -> None:
    preview = prepare_workflow_output_preview(
        {
            "order_id": "order-1",
            "api_token=never-persist-key-text": "never-persist-this",
            "message": "password=never-persist-this-either",
        }
    )

    assert preview == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": {
            "<redacted>": REDACTED,
            "message": REDACTED,
            "order_id": "order-1",
        },
    }
    assert "never-persist" not in str(preview)
    assert validate_workflow_output_preview(preview) == preview


def test_terminal_formatting_is_normalized_without_becoming_redacted() -> None:
    preview = prepare_workflow_output_preview(
        {
            "\x1b[36mstatus\x1b[0m": "\x1b[32mOK\x1b[0m",
            "results": ["\x1b[33mready\x1b[0m"],
        }
    )

    assert preview == {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"status": "OK", "results": ["ready"]},
    }
    assert validate_workflow_output_preview(preview) == preview


def test_validation_and_historical_reads_use_the_terminal_normalized_baseline() -> None:
    stored = {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"status": "\x1b[32mOK\x1b[0m"},
    }
    expected = {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"status": "OK"},
    }

    assert validate_workflow_output_preview(stored) == expected
    assert read_workflow_output_preview(stored) == expected


def test_terminal_formatting_cannot_hide_sensitive_preview_text() -> None:
    preview = prepare_workflow_output_preview(
        {"message": "pass\x1b[31mword=must-not-cross-the-boundary"}
    )

    assert preview == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": {"message": REDACTED},
    }
    assert "must-not-cross" not in str(preview)
    assert validate_workflow_output_preview(preview) == preview


def test_literal_redaction_marker_is_never_presented_as_available() -> None:
    preview = prepare_workflow_output_preview(REDACTED)

    assert preview == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": REDACTED,
    }
    assert validate_workflow_output_preview(preview) == preview


@pytest.mark.parametrize(
    "value",
    [
        b"raw-bytes",
        bytearray(b"raw-bytes"),
        ("tuple",),
        {"set": {1}},
        {1: "non-string-key"},
        float("nan"),
        preview_identity,
        _NoRepresentation(),
    ],
)
def test_unsupported_or_runtime_owned_values_fail_closed_without_repr(value: Any) -> None:
    assert prepare_workflow_output_preview(value) == {
        "schema_version": 1,
        "availability": "UNSUPPORTED",
        "value": None,
    }


@pytest.mark.parametrize(
    "value",
    [
        "x" * 257,
        list(range(17)),
        {f"key-{index}": index for index in range(16)},
        {"a": {"b": {"c": {"d": {"e": True}}}}},
        (1 << 53),
        1e20,
        {"text": "x" * WORKFLOW_OUTPUT_PREVIEW_MAX_ENCODED_BYTES},
    ],
)
def test_oversized_projection_returns_one_bounded_reason(value: Any) -> None:
    assert prepare_workflow_output_preview(value) == {
        "schema_version": 1,
        "availability": "TOO_LARGE",
        "value": None,
    }


def test_projector_failure_and_async_projector_cannot_fail_user_work() -> None:
    assert project_workflow_output_preview(preview_failure, {"status": "complete"}) == {
        "schema_version": 1,
        "availability": "FAILED",
        "value": None,
    }
    assert project_workflow_output_preview(async_preview, {"status": "complete"}) == {
        "schema_version": 1,
        "availability": "UNSUPPORTED",
        "value": None,
    }


def test_prepare_isolates_ordinary_diagnostic_failures_but_propagates_interrupts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_redaction(_value: Any) -> Any:
        raise RuntimeError("redaction implementation unavailable")

    monkeypatch.setattr(previews_module, "redact_value", fail_redaction)
    assert prepare_workflow_output_preview({"status": "ready"}) == {
        "schema_version": 1,
        "availability": "UNSUPPORTED",
        "value": None,
    }

    def interrupt_redaction(_value: Any) -> Any:
        raise KeyboardInterrupt("redaction cancelled")

    monkeypatch.setattr(previews_module, "redact_value", interrupt_redaction)
    with pytest.raises(KeyboardInterrupt, match="redaction cancelled"):
        prepare_workflow_output_preview({"status": "ready"})


def test_projector_interrupt_propagates() -> None:
    with pytest.raises(KeyboardInterrupt, match="projection cancellation"):
        project_workflow_output_preview(preview_interrupt, {"status": "complete"})


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"schema_version": True, "availability": "FAILED", "value": None},
        {"schema_version": 1, "availability": "UNKNOWN", "value": None},
        {"schema_version": 1, "availability": "FAILED", "value": "leak"},
        {
            "schema_version": 1,
            "availability": "AVAILABLE",
            "value": {"api_token": "unredacted"},
        },
        {
            "schema_version": 1,
            "availability": "REDACTED",
            "value": {"status": "safe"},
        },
        {
            "schema_version": 1,
            "availability": "REDACTED",
            "value": {"api_token=never-persist-key-text": REDACTED},
        },
    ],
)
def test_untrusted_preview_envelopes_are_revalidated_exactly(value: Any) -> None:
    with pytest.raises(WorkflowOutputPreviewError):
        validate_workflow_output_preview(value)


def test_unavailable_builder_never_accepts_a_value_bearing_status() -> None:
    assert unavailable_workflow_output_preview(
        WorkflowOutputPreviewAvailability.OMITTED_BY_POLICY
    ) == {
        "schema_version": 1,
        "availability": "OMITTED_BY_POLICY",
        "value": None,
    }

    with pytest.raises(WorkflowOutputPreviewError):
        unavailable_workflow_output_preview(WorkflowOutputPreviewAvailability.AVAILABLE)


def test_read_policy_drift_replaces_only_the_historical_value(settings) -> None:
    stored = {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"order_id": "order-1", "region": "newly-sensitive"},
    }
    assert read_workflow_output_preview(stored) == stored

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"newly-sensitive"],
    }

    assert read_workflow_output_preview(stored) == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": REDACTED,
    }
    assert stored["value"] == {
        "order_id": "order-1",
        "region": "newly-sensitive",
    }


def test_read_suppresses_a_historical_sensitive_mapping_key() -> None:
    stored = {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": {"api_token=never-return-key-text": REDACTED},
    }

    preview = read_workflow_output_preview(stored)

    assert preview == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": REDACTED,
    }
    assert "never-return-key-text" not in str(preview)


def test_read_policy_drift_redacts_against_the_raw_terminal_formatted_value(settings) -> None:
    stored = {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"region": "newly-\x1b[31msensitive"},
    }

    assert read_workflow_output_preview(stored) == {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"region": "newly-sensitive"},
    }

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"newly-sensitive"],
    }

    assert read_workflow_output_preview(stored) == {
        "schema_version": 1,
        "availability": "REDACTED",
        "value": REDACTED,
    }
    assert stored["value"] == {"region": "newly-\x1b[31msensitive"}
