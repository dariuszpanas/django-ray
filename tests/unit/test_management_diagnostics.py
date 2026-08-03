"""Tests for management-command diagnostic rendering boundaries."""

from __future__ import annotations

import pytest
from django.test import override_settings

import django_ray.management.diagnostics as diagnostics
from django_ray.management.diagnostics import (
    CONSOLE_DIAGNOSTIC_MAX_BYTES,
    render_console_diagnostic,
    render_console_resource_summary,
    render_exception_type_label,
)
from django_ray.redaction import REDACTED


@pytest.mark.parametrize(
    "value",
    (
        "pass\x1b[31mword=do-not-expose",
        "pass\x9b31mword=do-not-expose",
        "pass\x00word=do-not-expose",
        "pass\u202eword=do-not-expose",
        "pass\u034fword=do-not-expose",
    ),
    ids=("CSI", "C1-CSI", "C0", "bidi", "default-ignorable"),
)
def test_controls_cannot_split_default_secret_patterns(value: str) -> None:
    assert render_console_diagnostic(value) == REDACTED


def test_mixed_optional_terminal_finals_cannot_split_secret_pattern() -> None:
    value = "pass\x1b[31wor\x1b7d=synthetic-value"

    assert render_console_diagnostic(value) == REDACTED


def test_hidden_sensitive_terminal_payload_fails_closed() -> None:
    value = "before\x1b]0;password=do-not-expose\x07after"

    assert render_console_diagnostic(value) == REDACTED


def test_incomplete_control_keeps_printable_diagnostic_text_inert() -> None:
    rendered = render_console_diagnostic("visible\x1b]provider detail")

    assert rendered == "visibleprovider detail"
    assert "\x1b" not in rendered


def test_line_controls_are_normalized_without_flattening_diagnostics() -> None:
    assert render_console_diagnostic("a\r\nb\rc\t\nd") == "a\nb\nc\t\nd"


@override_settings(DJANGO_RAY={"REDACT_PATTERNS": [r"customer_email="]})
def test_custom_pattern_matches_across_terminal_hyperlink() -> None:
    value = "customer\x1b]8;;https://example.test\x1b\\_email=ada@example.test"

    assert render_console_diagnostic(value) == REDACTED


@override_settings(DJANGO_RAY={"REDACT_PATTERNS": [r"SENSITIVE_END_MARKER"]})
def test_output_cap_is_applied_only_after_complete_pattern_evaluation() -> None:
    value = "synthetic-private-prefix|" + ("x" * 17_000) + "|SENSITIVE_END_MARKER"

    assert render_console_diagnostic(value) == REDACTED


def test_exception_message_is_materialized_once() -> None:
    calls = 0

    class LazyError(RuntimeError):
        def __str__(self) -> str:
            nonlocal calls
            calls += 1
            return "provider unavailable"

    assert render_console_diagnostic(LazyError()) == "LazyError: provider unavailable"
    assert calls == 1


def test_string_subclass_override_is_not_invoked() -> None:
    class Diagnostic(str):
        def __str__(self) -> str:
            raise AssertionError("string subclass override must not run")

    assert render_console_diagnostic(Diagnostic("ordinary")) == "ordinary"


def test_exception_rendering_failure_has_a_fixed_fallback() -> None:
    class BrokenError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("secondary secret")

    assert render_console_diagnostic(BrokenError()) == (
        "BrokenError: exception message unavailable"
    )


def test_rendered_diagnostics_have_an_exact_utf8_byte_ceiling() -> None:
    rendered = render_console_diagnostic("é" * 20_000)

    assert len(rendered.encode("utf-8")) <= CONSOLE_DIAGNOSTIC_MAX_BYTES
    assert rendered.endswith(" [TRUNCATED]")


def test_oversized_shared_redaction_input_fails_closed_before_console_cap() -> None:
    assert render_console_diagnostic("ordinary" * 10_000) == REDACTED


def test_exception_type_label_rejects_terminal_controls() -> None:
    unsafe_error = type("Unsafe\x1b[31mError", (RuntimeError,), {})()

    assert render_exception_type_label(unsafe_error) == "Exception"
    assert render_exception_type_label(RuntimeError()) == "RuntimeError"


@override_settings(DJANGO_RAY={"REDACT_PATTERNS": [r"TenantCanaryError"]})
def test_exception_type_only_label_uses_configured_redaction() -> None:
    error = type("TenantCanaryError", (RuntimeError,), {})("ordinary")

    assert render_exception_type_label(error) == REDACTED


@override_settings(DJANGO_RAY={"REDACT_PATTERNS": [r"BoundaryCanaryError: provider marker"]})
def test_exception_type_and_message_share_one_redaction_boundary() -> None:
    error = type("BoundaryCanaryError", (RuntimeError,), {})("provider marker")

    assert render_console_diagnostic(error) == REDACTED


@override_settings(DJANGO_RAY={"REDACT_PATTERNS": [r"tenant-sensitive-label"]})
def test_ray_resource_summary_redacts_names_and_preserves_numeric_counts() -> None:
    rendered = render_console_resource_summary(
        {
            "GPU": 2.0,
            "tenant-sensitive-label": 1.0,
            "custom\x1b[31m_resource": 3,
        }
    )

    assert rendered == "{'GPU': 2.0, '[REDACTED]': 1.0, 'custom_resource': 3}"
    assert "tenant-sensitive-label" not in rendered
    assert "\x1b" not in rendered


def test_ray_resource_summary_rejects_invalid_shapes_and_bounds_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert render_console_resource_summary([]) == "<invalid Ray resource summary>"
    assert render_console_resource_summary({1: "two"}) == "{<invalid Ray resource entry>}"
    monkeypatch.setattr(diagnostics, "_CONSOLE_RESOURCE_MAX_ITEMS", 1)
    assert render_console_resource_summary({"CPU": 1.0, "GPU": 2.0}) == "{'CPU': 1.0, ...}"


def test_ray_resource_summary_stops_after_console_byte_cap() -> None:
    resources = {"x" * 10_000: 1.0, "must-not-be-read": 2.0}

    rendered = render_console_resource_summary(resources)

    assert len(rendered.encode("utf-8")) <= CONSOLE_DIAGNOSTIC_MAX_BYTES
    assert rendered.endswith(" [TRUNCATED]")
    assert "must-not-be-read" not in rendered
