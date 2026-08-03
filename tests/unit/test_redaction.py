"""Tests for sensitive-data handling in operational output."""

from __future__ import annotations

from typing import SupportsIndex

import pytest

from django_ray.redaction import (
    REDACTED,
    normalize_terminal_text,
    redact_exception,
    redact_text,
    redact_value,
    result_metadata,
    safe_json_dumps,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            '\x1b[36mray::task()\x1b[39m\r\nFile "worker.py", line 1',
            'ray::task()\nFile "worker.py", line 1',
        ),
        (
            "open \x1b]8;;https://example.test/private\x1b\\documentation\x1b]8;;\x1b\\ now",
            "open documentation now",
        ),
        ("before\x1bPprivate-device-data\x1b\\after", "beforeafter"),
        ("before\x98private-start-of-string-data\x9cafter", "beforeafter"),
        ("before\x9fprivate-application-data\x9cafter", "beforeafter"),
        ("before\x9evisible\x9dhidden\x07tail", "beforevisibletail"),
        ("before\x1bPvisible\x1b]hidden\x07tail", "beforevisibletail"),
        ("before\x9b31mafter", "beforeafter"),
        ("before\x1b[1 qafter", "beforeafter"),
        ("bell\x1b]0;private title\x07after", "bellafter"),
        ("alternate\x1b(0after", "alternateafter"),
        ("clear\x1b[2J\x1b[Hscreen\x00\x08\x7f", "clearscreen"),
        ("first\rprogress\r\nlast\t✓", "first\nprogress\nlast\t✓"),
        ("broken\x1b\nnext", "broken\nnext"),
        ("incomplete\x1b[", "incomplete["),
        ("trailing\x1b", "trailing"),
        ("before\x1b[31\nmalformed\x1b]unterminated", "before[31\nmalformedunterminated"),
    ],
)
def test_normalize_terminal_text_removes_controls_without_flattening_lines(
    value: str,
    expected: str,
) -> None:
    assert normalize_terminal_text(value) == expected


def test_terminal_sequences_cannot_split_default_or_custom_redaction_patterns() -> None:
    default_split = "pass\x1b[31mword=do-not-expose"
    malformed_csi_split = "pass\x1b[31\nword=do-not-expose"
    malformed_c1_csi_split = "pass\x9b31\nword=do-not-expose"
    control_split = "pass\x00word=do-not-expose"
    escape_final_split = "pass\x1bword=do-not-expose"
    custom_split = "customer\x1b]8;;https://example.test\x1b\\_email=ada@example.test"

    assert redact_text(default_split) == REDACTED
    assert redact_text(malformed_csi_split) == REDACTED
    assert redact_text(malformed_c1_csi_split) == REDACTED
    assert redact_text(control_split) == REDACTED
    assert redact_text(escape_final_split) == REDACTED
    assert redact_text(custom_split, patterns=[r"customer_email"]) == REDACTED
    assert redact_value({"diagnostic": default_split}) == {"diagnostic": REDACTED}


@pytest.mark.parametrize(
    "value",
    (
        "p\x9bass\x9dx\x9cword",
        "p\x1b[ass\x1b]x\x1b\\word",
        "pass\x1b[31\n\x1b]x\x07word",
    ),
    ids=("C1", "seven-bit", "malformed-CSI"),
)
def test_mixed_terminal_sequence_families_compose_for_redaction(value: str) -> None:
    normalized = normalize_terminal_text(value)

    assert "password" not in normalized
    assert redact_text(value) == REDACTED
    assert redact_value({value: "hunter2"}) == {normalized: REDACTED}


@pytest.mark.parametrize(
    "control",
    (
        "\u00ad",
        "\u200b",
        "\u202e",
        "\u2060",
        "\u2066",
        "\ufeff",
        "\ud800",
    ),
)
def test_unsafe_unicode_controls_are_inert_and_cannot_split_patterns(control: str) -> None:
    value = f"pass{control}word=hunter2"

    assert normalize_terminal_text(value) == "password=hunter2"
    assert redact_text(value) == REDACTED
    assert redact_value({value.split("=")[0]: "hunter2"}) == {"password": REDACTED}


@pytest.mark.parametrize("control", ("\u034f", "\u200c", "\u200d", "\ufe0f"))
def test_harmless_unicode_shaping_is_preserved_but_cannot_split_patterns(control: str) -> None:
    value = f"pass{control}word=hunter2"

    assert normalize_terminal_text(value) == value
    assert redact_text(value) == REDACTED
    assert redact_value({value.split("=")[0]: "hunter2"}) == {
        f"pass{control}word": REDACTED,
    }


def test_unicode_graphemes_and_private_use_glyphs_survive_inert_display() -> None:
    england_flag = "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
    value = f"status 👩‍💻 ❤️ {england_flag} icon=\ue000"

    assert normalize_terminal_text(value) == value


def test_redaction_fails_closed_before_projecting_oversized_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.redaction as redaction

    monkeypatch.setattr(redaction, "_REDACTION_TEXT_MAX_CHARS", 8)

    assert redact_text("ordinary-text") == REDACTED
    assert redact_value("ordinary-text") == REDACTED
    assert redact_value({"oversized-key": "value"}) == {"<redacted>": REDACTED}


def test_many_safe_sgr_sequences_do_not_trigger_ambiguity_redaction() -> None:
    value = "".join(f"\x1b[{30 + index % 8}msegment-{index}\x1b[0m" for index in range(64))

    assert redact_text(value) == "".join(f"segment-{index}" for index in range(64))


@pytest.mark.parametrize(
    ("value", "patterns", "expected"),
    (
        ("hello\x1b[Hworld\x1b[H", None, "helloworld"),
        ("\x1b[31mordinary\x1b[0m", [r"email"], "ordinary"),
    ),
)
def test_optional_terminal_finals_do_not_redact_safe_formatted_text(
    value: str,
    patterns: list[str] | None,
    expected: str,
) -> None:
    assert redact_text(value, patterns=patterns) == expected


@pytest.mark.parametrize(
    ("value", "pattern"),
    (
        ("clienti\x1b[1d", r"client(id)+"),
        ("customer_foo_emai\x1b[1l", r"customer_.*_email"),
        ("A\\\x1b[x\x1b[qB", r"A\\.B"),
    ),
)
def test_consuming_custom_patterns_match_selective_terminal_final_paths(
    value: str,
    pattern: str,
) -> None:
    assert redact_text(value, patterns=[pattern]) == REDACTED


@pytest.mark.parametrize(
    "pattern",
    (r"customer_.*_email", r"bearer\s+\S+", r"token.{0,8}", r"(?:customer|client)_email"),
)
def test_complex_custom_patterns_do_not_hide_safe_ansi_diagnostics(pattern: str) -> None:
    assert redact_text("\x1b[31mordinary message\x1b[0m", patterns=[pattern]) == (
        "ordinary message"
    )


def test_many_cursor_finals_use_linear_lattice_matching_without_false_redaction() -> None:
    value = "".join("chunk\x1b[2A" for _ in range(7))

    assert redact_text(value) == "chunk" * 7


@pytest.mark.parametrize("introducer", ("]", "P", "^", "_", "X"))
def test_malformed_seven_bit_string_controls_cannot_split_redaction_patterns(
    introducer: str,
) -> None:
    value = f"pass\x1b{introducer}word=do-not-expose"

    assert normalize_terminal_text(value) == "password=do-not-expose"
    assert redact_text(value) == REDACTED


@pytest.mark.parametrize("introducer", ("\x90", "\x98", "\x9d", "\x9e", "\x9f"))
def test_malformed_c1_string_controls_have_symmetric_fail_closed_matching(
    introducer: str,
) -> None:
    value = f"pass{introducer}word=do-not-expose"

    assert normalize_terminal_text(value) == "password=do-not-expose"
    assert redact_text(value) == REDACTED


@pytest.mark.parametrize("cancel", ("\x18", "\x1a"), ids=("CAN", "SUB"))
@pytest.mark.parametrize(
    ("introducer", "terminator"),
    (
        ("\x1b]", "\x1b\\"),
        ("\x1bP", "\x1b\\"),
        ("\x1b^", "\x1b\\"),
        ("\x1b_", "\x1b\\"),
        ("\x1bX", "\x1b\\"),
        ("\x9d", "\x9c"),
        ("\x90", "\x9c"),
        ("\x9e", "\x9c"),
        ("\x9f", "\x9c"),
        ("\x98", "\x9c"),
    ),
    ids=(
        "ESC-OSC",
        "ESC-DCS",
        "ESC-PM",
        "ESC-APC",
        "ESC-SOS",
        "C1-OSC",
        "C1-DCS",
        "C1-PM",
        "C1-APC",
        "C1-SOS",
    ),
)
def test_control_string_cancel_discards_hidden_payload_and_resumes_text(
    introducer: str,
    terminator: str,
    cancel: str,
) -> None:
    for cancel_fragment in (cancel, f"\x1b{cancel}"):
        terminators = (terminator, "\x07") if introducer in {"\x1b]", "\x9d"} else (terminator,)
        split_secret = f"pass{introducer}hidden{cancel_fragment}word=do-not-expose"

        for trailing_terminator in terminators:
            value = f"before{introducer}hidden{cancel_fragment}visible{trailing_terminator}tail"
            assert normalize_terminal_text(value) == "beforevisibletail"
        assert normalize_terminal_text(split_secret) == "password=do-not-expose"
        assert redact_text(split_secret) == REDACTED


@pytest.mark.parametrize("embedded", ("\x00", "\x0e", "\x0f"), ids=("NUL", "SO", "SI"))
@pytest.mark.parametrize("introducer", ("\x1b[31", "\x9b31"), ids=("ESC-CSI", "C1-CSI"))
def test_embedded_zero_width_controls_cannot_hide_a_csi_final_from_redaction(
    introducer: str,
    embedded: str,
) -> None:
    value = f"pass{introducer}{embedded}word=do-not-expose"

    assert normalize_terminal_text(value) == "passord=do-not-expose"
    assert redact_text(value) == REDACTED
    assert redact_value({value.split("=")[0]: "leaked-value"}) == {
        "passord": REDACTED,
    }


@pytest.mark.parametrize("embedded", ("\x00", "\x0e", "\x0f"), ids=("NUL", "SO", "SI"))
def test_embedded_zero_width_controls_cannot_hide_an_escape_final_from_redaction(
    embedded: str,
) -> None:
    value = f"pass\x1b({embedded}word=do-not-expose"

    assert normalize_terminal_text(value) == "passord=do-not-expose"
    assert redact_text(value) == REDACTED


@pytest.mark.parametrize("cancel", ("\x18", "\x1a"), ids=("CAN", "SUB"))
@pytest.mark.parametrize("sequence", ("\x1b[31", "\x9b31", "\x1b("))
def test_sequence_cancellation_discards_control_bytes_and_resumes_redaction(
    sequence: str,
    cancel: str,
) -> None:
    value = f"pass{sequence}{cancel}word=do-not-expose"

    assert normalize_terminal_text(value) == "password=do-not-expose"
    assert redact_text(value) == REDACTED


@pytest.mark.parametrize(
    "control",
    ("\x00", "\x0e", "\x1b[0m", "\x9b0m", "\x1b]0;title\x07"),
    ids=("NUL", "SO", "ESC-CSI", "C1-CSI", "OSC"),
)
def test_stripped_controls_between_cr_and_lf_do_not_create_blank_lines(control: str) -> None:
    assert normalize_terminal_text(f"before\r{control}\nafter") == "before\nafter"


def test_mapping_keys_are_normalized_and_matched_with_fail_closed_projections() -> None:
    rendered = redact_value(
        {
            "pass\x1b]word": "default-sensitive-value",
            "api\x9f_key": "c1-sensitive-value",
            "customer\x1b^_email": "custom-sensitive-value",
            "safe\x1b[31m_key": "visible",
        },
        patterns=[r"customer_email"],
    )

    assert rendered == {
        "password": REDACTED,
        "api_key": REDACTED,
        "customer_email": REDACTED,
        "safe_key": "visible",
    }
    assert "\x1b" not in str(rendered)
    assert not any(0x7F <= ord(character) <= 0x9F for character in str(rendered))


@pytest.mark.parametrize("sensitive_first", (False, True))
def test_normalized_mapping_key_collisions_remain_redacted(
    sensitive_first: bool,
) -> None:
    sensitive = ("\x1b[31mvisible\x1b[0m", "must-not-win")
    ordinary = ("visible", "ordinary-value")
    items = (sensitive, ordinary) if sensitive_first else (ordinary, sensitive)

    assert redact_value(dict(items), patterns=[r"\x1b"]) == {"visible": REDACTED}


def test_terminal_parser_work_grows_linearly_for_repeated_unterminated_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.redaction as redaction

    class CountingText(str):
        accesses: int

        def __new__(cls, value: str) -> CountingText:
            instance = super().__new__(cls, value)
            instance.accesses = 0
            return instance

        def __getitem__(
            self,
            key: SupportsIndex | slice[SupportsIndex | None],
        ) -> str:
            self.accesses += 1
            return super().__getitem__(key)

    small = CountingText("\x1b]" * 128)
    large = CountingText("\x1b]" * 256)
    feed_calls = 0
    original_feed = redaction._TerminalDisplayNormalizer.feed

    def counted_feed(
        parser: redaction._TerminalDisplayNormalizer,
        character: str,
    ) -> None:
        nonlocal feed_calls
        feed_calls += 1
        original_feed(parser, character)

    monkeypatch.setattr(redaction._TerminalDisplayNormalizer, "feed", counted_feed)

    assert normalize_terminal_text(small) == "]" * 126
    small_feed_calls = feed_calls
    feed_calls = 0
    assert normalize_terminal_text(large) == "]" * 254
    assert small.accesses <= len(small) * 2
    assert large.accesses <= len(large) * 2
    assert large.accesses <= (small.accesses * 3) + 4
    assert small_feed_calls <= (len(small) * 3) + 2
    assert feed_calls <= (len(large) * 3) + 2
    assert feed_calls <= (small_feed_calls * 3) + 4


def test_bounded_parser_preserves_malformed_sequence_tails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.redaction as redaction

    monkeypatch.setattr(redaction, "_TERMINAL_SEQUENCE_MAX_CHARS", 3)

    assert normalize_terminal_text("a\x1b]xyzb") == "axyzb"
    assert normalize_terminal_text("a\x1b]xy\x1bb") == "axyb"
    assert normalize_terminal_text("a\x1b]x\x1b\x1bb") == "axb"
    assert normalize_terminal_text("a\x1b]x\x1bqb") == "axb"
    assert redact_text("a\x1b]xyzb", patterns=["never-match"]) == "axyzb"
    assert redact_text("a\x1b]xy\x1bb", patterns=["never-match"]) == "axyb"
    assert redact_text("a\x1b]x\x1b\x1bb", patterns=["never-match"]) == "axb"
    assert redact_text("a\x1b]x\x1bqb", patterns=["never-match"]) == "axb"
    assert redact_text("a\x1b]x\x1b", patterns=["never-match"]) == "ax"
    assert redact_text("a\x1b]x\x1b\x1b", patterns=["never-match"]) == "ax"


def test_malformed_ordinary_escape_sequences_preserve_printable_tails() -> None:
    bounded_intermediates = " " * 32

    assert normalize_terminal_text(f"a\x1b{bounded_intermediates}b") == (
        f"a{bounded_intermediates}b"
    )
    assert normalize_terminal_text("a\x1b(\nnext") == "a(\nnext"
    assert normalize_terminal_text("a\x1b]tail\x1b") == "atail"
    assert normalize_terminal_text("a\x1b\x18b") == "ab"
    assert normalize_terminal_text("a\x1b\x1bb") == "a"
    assert normalize_terminal_text("a\x1b\x01b") == "a"
    assert normalize_terminal_text("a\x1b(\x1bb") == "a"
    assert normalize_terminal_text("a\x1b[31\x1bb") == "a"


def test_redact_value_handles_nested_mappings_and_sequences() -> None:
    value = {
        "user": {"name": "Ada", "password": "correct-horse"},
        "items": [{"access_token": "secret-token", "value": 3}],
    }

    assert redact_value(value) == {
        "user": {"name": "Ada", "password": REDACTED},
        "items": [{"access_token": REDACTED, "value": 3}],
    }


def test_redact_value_shares_item_and_text_limits_across_the_root_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.redaction as redaction

    monkeypatch.setattr(redaction, "_REDACTION_VALUE_MAX_ITEMS", 3)
    assert redact_value(["one", "two", "three"]) == [REDACTED]

    monkeypatch.setattr(redaction, "_REDACTION_VALUE_MAX_ITEMS", 4096)
    monkeypatch.setattr(redaction, "_REDACTION_TEXT_MAX_CHARS", 8)
    assert redact_value({"a": "1234", "b": "5678"}) == {"<redacted>": REDACTED}


def test_terminal_normalization_uses_a_frozen_unicode_safety_table() -> None:
    # U+2EBF0 was unassigned in Unicode 15 and became a CJK letter in Unicode
    # 15.1. Its display result must not depend on the interpreter's database.
    value = "before\U0002ebf0after"

    assert normalize_terminal_text(value) == value


def test_complete_unicode_16_default_ignorables_cannot_split_a_secret_marker() -> None:
    ranges = (
        (0x00AD, 0x00AD),
        (0x034F, 0x034F),
        (0x061C, 0x061C),
        (0x115F, 0x1160),
        (0x17B4, 0x17B5),
        (0x180B, 0x180F),
        (0x200B, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x206F),
        (0x3164, 0x3164),
        (0xFE00, 0xFE0F),
        (0xFEFF, 0xFEFF),
        (0xFFA0, 0xFFA0),
        (0xFFF0, 0xFFF8),
        (0x1BCA0, 0x1BCA3),
        (0x1D173, 0x1D17A),
        (0xE0000, 0xE0FFF),
    )
    separators = "".join(
        chr(codepoint) for start, end in ranges for codepoint in range(start, end + 1)
    )

    assert normalize_terminal_text("before\ufff0after") == "before\ufff0after"
    assert redact_text(f"pass{separators}word=do-not-expose") == REDACTED


def test_string_subclasses_are_coerced_without_invoking_an_override() -> None:
    class StringSubclass(str):
        def __str__(self) -> str:
            raise AssertionError("string-subclass override must not run")

    value = StringSubclass("ordinary diagnostic")

    assert type(normalize_terminal_text(value)) is str
    assert normalize_terminal_text(value) == "ordinary diagnostic"
    assert redact_text(value) == "ordinary diagnostic"
    assert redact_value(value) == "ordinary diagnostic"


def test_redact_value_redacts_matching_strings_and_custom_patterns() -> None:
    assert (
        redact_text("customer email=ada@example.test", patterns=[r"ada@example\.test"]) == REDACTED
    )
    assert redact_value({"note": "do-not-share"}, patterns=[r"do-not-share"]) == {"note": REDACTED}


def test_redact_exception_does_not_expose_secret() -> None:
    error = RuntimeError("access-token=abc123")

    rendered = redact_exception(error, patterns=[r"access-token"])

    assert rendered == REDACTED
    assert "abc123" not in rendered


def test_redact_exception_survives_message_and_type_rendering_failures() -> None:
    calls = 0

    class BrokenError(RuntimeError):
        def __str__(self) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("secondary password=do-not-expose")

    unsafe_error = type("Unsafe\x1b[31mError", (BrokenError,), {})()

    assert redact_exception(unsafe_error) == "Exception: exception message unavailable"
    assert redact_text(unsafe_error) == "Exception: exception message unavailable"
    assert calls == 2


def test_non_json_values_are_represented_by_type_only() -> None:
    class SecretObject:
        def __str__(self) -> str:
            return "password=abc123"

    rendered = redact_value({"payload": SecretObject()})

    assert rendered["payload"].endswith(".SecretObject>")
    assert "abc123" not in str(rendered)


def test_non_string_mapping_keys_never_invoke_user_string_conversion() -> None:
    class RaisingKey:
        def __str__(self) -> str:
            raise AssertionError("mapping-key string conversion must not run")

    rendered = redact_value({RaisingKey(): "visible"})

    assert list(rendered.values()) == ["visible"]
    assert next(iter(rendered)).endswith(".RaisingKey>")


def test_result_metadata_is_bounded() -> None:
    metadata = result_metadata(
        {"secret": "value", "items": [1, 2, 3]},
        serialized_size_bytes=47,
    )

    assert metadata["result_type"] == "builtins.dict"
    assert metadata["result_size_bytes"] == 47
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


def test_result_metadata_never_serializes_application_values(monkeypatch) -> None:
    import django_ray.redaction as redaction

    monkeypatch.setattr(
        redaction.json,
        "dumps",
        lambda *_args, **_kwargs: pytest.fail("result metadata must not serialize the value"),
    )

    metadata = result_metadata({"large": [object()] * 10_000})

    assert metadata["result_type"] == "builtins.dict"
    assert metadata["result_size_bytes"] is None


def test_result_metadata_normalizes_and_bounds_provider_type_names() -> None:
    unsafe_value = type("Unsafe\x1b[31mResult", (), {})()
    oversized_value = type("x" * 300, (), {})()

    assert "\x1b" not in result_metadata(unsafe_value)["result_type"]
    assert result_metadata(oversized_value)["result_type"] == "result"
    assert result_metadata(None, serialized_size_bytes=-1)["result_size_bytes"] is None


def test_result_metadata_rejects_oversized_type_names_before_normalizing(
    monkeypatch,
) -> None:
    import django_ray.redaction as redaction

    values = (
        type("Result", (), {"__module__": "m" * 257})(),
        type("Q" * 257, (), {})(),
        type("Q" * 128, (), {"__module__": "m" * 128})(),
    )
    monkeypatch.setattr(
        redaction,
        "normalize_terminal_text",
        lambda _value: pytest.fail("oversized type metadata must not be normalized"),
    )

    assert [result_metadata(value)["result_type"] for value in values] == [
        "result",
        "result",
        "result",
    ]


def test_configured_patterns_use_django_settings_and_accept_custom_string(monkeypatch) -> None:
    import django_ray.conf.settings as ray_settings

    monkeypatch.setattr(ray_settings, "get_settings", lambda: {"REDACT_PATTERNS": [r"private"]})

    assert redact_text("private message") == REDACTED
    assert redact_text("customer-id=42", patterns=r"customer-id") == REDACTED


def test_configured_patterns_fall_back_when_django_settings_are_unavailable(monkeypatch) -> None:
    from django.core.exceptions import ImproperlyConfigured

    import django_ray.conf.settings as ray_settings

    monkeypatch.setattr(
        ray_settings,
        "get_settings",
        lambda: (_ for _ in ()).throw(ImproperlyConfigured("settings unavailable")),
    )

    assert redact_text("password=secret") == REDACTED
    assert redact_text(42) == "42"


def test_configured_patterns_do_not_silently_skip_unexpected_settings_errors(
    monkeypatch,
) -> None:
    import django_ray.conf.settings as ray_settings

    monkeypatch.setattr(
        ray_settings,
        "get_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("settings lookup failed")),
    )

    with pytest.raises(RuntimeError, match="settings lookup failed"):
        redact_text("ordinary diagnostic")
