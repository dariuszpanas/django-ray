"""Tests for sensitive-data handling in operational output."""

from __future__ import annotations

from django_ray.redaction import (
    REDACTED,
    redact_exception,
    redact_text,
    redact_value,
    result_metadata,
    safe_json_dumps,
)


def test_redact_value_handles_nested_mappings_and_sequences() -> None:
    value = {
        "user": {"name": "Ada", "password": "correct-horse"},
        "items": [{"access_token": "secret-token", "value": 3}],
    }

    assert redact_value(value) == {
        "user": {"name": "Ada", "password": REDACTED},
        "items": [{"access_token": REDACTED, "value": 3}],
    }


def test_redact_value_redacts_matching_strings_and_custom_patterns() -> None:
    assert (
        redact_text("customer email=ada@example.test", patterns=[r"ada@example\.test"]) == REDACTED
    )
    assert redact_value({"note": "do-not-share"}, patterns=[r"do-not-share"]) == {"note": REDACTED}


def test_redact_exception_does_not_expose_secret() -> None:
    error = RuntimeError("access-token=abc123")

    rendered = redact_exception(error, patterns=[r"access-token"])

    assert rendered == f"RuntimeError: {REDACTED}"
    assert "abc123" not in rendered


def test_non_json_values_are_represented_by_type_only() -> None:
    class SecretObject:
        def __str__(self) -> str:
            return "password=abc123"

    rendered = redact_value({"payload": SecretObject()})

    assert rendered["payload"].endswith(".SecretObject>")
    assert "abc123" not in str(rendered)


def test_result_metadata_is_bounded() -> None:
    metadata = result_metadata({"secret": "value", "items": [1, 2, 3]})

    assert metadata["result_type"] == "builtins.dict"
    assert isinstance(metadata["result_size_bytes"], int)
    assert "value" not in str(metadata)


def test_redaction_handles_depth_binary_values_and_exception_text() -> None:
    nested: object = "leaf"
    for _ in range(22):
        nested = [nested]

    redacted_nested = redact_value(nested)
    while isinstance(redacted_nested, list):
        redacted_nested = redacted_nested[0]
    assert redacted_nested == "<max-depth>"
    assert redact_value(b"binary-value").endswith(".bytes>")
    assert redact_text(ValueError("password=secret")) == REDACTED
    assert safe_json_dumps({"token": "secret", "number": 3}) == (
        '{"token": "[REDACTED]", "number": 3}'
    )


def test_result_metadata_handles_unserializable_values(monkeypatch) -> None:
    import django_ray.redaction as redaction

    monkeypatch.setattr(
        redaction.json, "dumps", lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError)
    )

    metadata = result_metadata(object())

    assert metadata["result_type"] == "builtins.object"
    assert metadata["result_size_bytes"] is None


def test_configured_patterns_use_django_settings_and_accept_custom_string(monkeypatch) -> None:
    import django_ray.conf.settings as ray_settings

    monkeypatch.setattr(ray_settings, "get_settings", lambda: {"REDACT_PATTERNS": [r"private"]})

    assert redact_text("private message") == REDACTED
    assert redact_text("customer-id=42", patterns=r"customer-id") == REDACTED


def test_configured_patterns_fall_back_when_settings_are_unavailable(monkeypatch) -> None:
    import django_ray.conf.settings as ray_settings

    monkeypatch.setattr(
        ray_settings,
        "get_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )

    assert redact_text("password=secret") == REDACTED
    assert redact_text(42) == "42"
