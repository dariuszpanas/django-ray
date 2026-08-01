"""Tests for bounded regex matching over optional terminal-final tokens."""

from __future__ import annotations

import itertools
import re
import traceback
from dataclasses import dataclass, replace

import pytest

import django_ray.redaction_patterns as redaction_patterns
from django_ray.redaction_patterns import (
    DEFAULT_REDACTION_PATTERN_LIMITS,
    OptionalRedactionToken,
    RedactionPatternError,
    RedactionPatternLimits,
    RedactionPatternSearchMetrics,
    RedactionPatternWorkBudget,
    compile_redaction_patterns,
)


def _candidates(tokens: tuple[str | OptionalRedactionToken, ...]) -> tuple[str, ...]:
    candidates = [""]
    for token in tokens:
        if isinstance(token, str):
            candidates = [candidate + token for candidate in candidates]
        else:
            candidates = [
                projection
                for candidate in candidates
                for projection in (candidate, candidate + token.value)
            ]
    return tuple(candidates)


def _brute_force_search(
    patterns: tuple[str, ...],
    tokens: tuple[str | OptionalRedactionToken, ...],
) -> bool:
    return any(
        re.search(pattern, candidate, re.IGNORECASE) is not None
        for candidate in _candidates(tokens)
        for pattern in patterns
    )


@pytest.mark.parametrize(
    "pattern",
    (
        r"password",
        r"api[_-]?key",
        r"(?:customer|client)_email",
        r"customer_.*_email",
        r"bearer\s+\S+",
        r"token.{0,8}",
        r"(ab+|cd{2,4})e?",
        r"[^0-9]{2}\d+",
        r"\x70\u0061\N{LATIN SMALL LETTER S}+",
        r"a\+b\\c",
        r"(foo|bar)+baz",
        r"(a+)+b",
        r"colou?r",
        r"a{2,}",
        r"a{,3}b",
        r"a+?b",
        r"[]a]",
        r"[\]]",
        r"\012",
        r"\U00000070ass",
    ),
)
def test_consuming_regular_subset_has_python_search_parity(pattern: str) -> None:
    program = compile_redaction_patterns(pattern)
    values = (
        "",
        "ordinary",
        "PASSWORD=hunter2",
        "api-key",
        "api_key",
        "customer_primary_email",
        "client_email",
        "bearer opaque-value",
        "token12345678",
        "token123456789",
        "abbe",
        "cddde",
        "AZ42",
        "passss",
        "a+b\\c",
        "foobarbarbaz",
        "aaaaab",
        "colour color",
        "aaab",
        "a\nb",
        "\x00",
    )

    for value in values:
        assert program.search(value) is (re.search(pattern, value, re.IGNORECASE) is not None)


@pytest.mark.parametrize(
    ("pattern", "value"),
    (
        ("i", "\u0130"),
        ("i", "\u0131"),
        ("s", "\u017f"),
        ("k", "\u212a"),
        ("[a-z]+", "\u0130\u0131\u017f\u212a"),
        ("[I]", "\u0131"),
    ),
)
def test_unicode_ignorecase_matches_python_special_cases(pattern: str, value: str) -> None:
    assert re.search(pattern, value, re.IGNORECASE) is not None
    assert compile_redaction_patterns(pattern).search(value)


@pytest.mark.parametrize(
    "pattern",
    (
        r"^secret",
        r"secret$",
        r"\Asecret",
        r"secret\Z",
        r"\bsecret\b",
        r"secret\B",
        r"(?=secret)",
        r"(?!secret)",
        r"(?<=secret)x",
        r"(?<!secret)x",
        r"(?i:secret)",
        r"(?P<label>secret)",
        r"(?P<label>a)(?P=label)",
        r"(a)?(?(1)b|c)",
        r"(?>secret)",
        r"(a)\1",
        r"a++",
        r"a*+",
        r"a{1,3}+",
    ),
)
def test_context_sensitive_and_non_regular_syntax_is_rejected(pattern: str) -> None:
    with pytest.raises(RedactionPatternError, match="configured pattern at index 0"):
        compile_redaction_patterns(pattern)


@pytest.mark.parametrize("pattern", ("", r"a*", r"a?", r"foo|", r"(?:a{0})", r"(a?)+"))
def test_patterns_which_can_match_empty_text_are_rejected(pattern: str) -> None:
    with pytest.raises(RedactionPatternError, match="configured pattern at index 0"):
        compile_redaction_patterns(pattern)


def test_errors_identify_index_and_reason_without_echoing_pattern_source() -> None:
    marker = "customer-private-pattern"
    with pytest.raises(RedactionPatternError) as captured:
        compile_redaction_patterns(("ordinary", f"(?={marker})"))

    message = str(captured.value)
    assert "configured pattern at index 1" in message
    assert "not supported" in message
    assert marker not in message


def test_invalid_python_regex_does_not_echo_source() -> None:
    marker = "private-pattern"
    with pytest.raises(RedactionPatternError) as captured:
        compile_redaction_patterns(("ordinary", f"[{marker}"))

    message = str(captured.value)
    assert "configured pattern at index 1" in message
    assert "invalid regular-expression syntax" in message
    assert marker not in message


@pytest.mark.parametrize(
    "source",
    (
        "[private-pattern",
        "[a&&b]",
        "private-pattern\ud800",
    ),
    ids=("regex-error", "future-syntax", "invalid-utf8"),
)
def test_rejected_sources_are_not_retained_by_exception_chaining(source: str) -> None:
    with pytest.raises(RedactionPatternError) as captured:
        compile_redaction_patterns(source)

    error = captured.value
    rendered = "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "private-pattern" not in str(error)
    assert "private-pattern" not in rendered


@pytest.mark.parametrize("source", ("[a&&b]", "[a||b]", "[a~~b]", "[[]"))
def test_ambiguous_future_character_class_syntax_is_always_rejected(source: str) -> None:
    for _ in range(2):
        with pytest.raises(RedactionPatternError, match="configured pattern at index 0"):
            compile_redaction_patterns(source)


def test_utf8_source_limits_and_separate_builtin_allowances_are_exact() -> None:
    limits = replace(
        DEFAULT_REDACTION_PATTERN_LIMITS,
        max_pattern_source_bytes=8,
        max_configured_source_bytes=8,
        max_program_source_bytes=16,
    )

    exact = compile_redaction_patterns("\u00e9" * 4, builtin_patterns="builtin8", limits=limits)
    assert exact.configured_pattern_count == 1
    assert exact.builtin_pattern_count == 1

    with pytest.raises(RedactionPatternError, match="configured pattern at index 0"):
        compile_redaction_patterns("\u00e9" * 5, limits=limits)
    with pytest.raises(RedactionPatternError, match="configured source exceeds 8 UTF-8 bytes"):
        compile_redaction_patterns(("abcd", "efghi"), limits=limits)
    with pytest.raises(RedactionPatternError, match="combined program source exceeds 16"):
        compile_redaction_patterns("abcdefgh", builtin_patterns=("12345678", "x"), limits=limits)


def test_configured_and_combined_count_caps_are_independent() -> None:
    limits = replace(
        DEFAULT_REDACTION_PATTERN_LIMITS,
        max_configured_patterns=2,
        max_program_patterns=3,
    )

    program = compile_redaction_patterns(("one", "two"), builtin_patterns=("zero",), limits=limits)
    assert program.pattern_count == 3

    with pytest.raises(RedactionPatternError, match="configured redaction pattern count"):
        compile_redaction_patterns(("one", "two", "three"), limits=limits)
    with pytest.raises(RedactionPatternError, match="combined redaction pattern count"):
        compile_redaction_patterns(("one", "two"), builtin_patterns=("zero", "base"), limits=limits)


def test_pattern_count_limit_stops_a_lazy_source_without_materializing_it() -> None:
    reads = 0

    def patterns():
        nonlocal reads
        while True:
            reads += 1
            yield f"pattern-{reads}"

    with pytest.raises(RedactionPatternError, match="configured redaction pattern count"):
        compile_redaction_patterns(patterns())  # type: ignore[arg-type]

    assert reads == DEFAULT_REDACTION_PATTERN_LIMITS.max_configured_patterns + 1


def test_repeat_group_and_state_limits_are_enforced() -> None:
    with pytest.raises(RedactionPatternError, match="repeat bound exceeds 2"):
        compile_redaction_patterns(
            "a{3}", limits=replace(DEFAULT_REDACTION_PATTERN_LIMITS, max_repeat=2)
        )
    with pytest.raises(RedactionPatternError, match="group nesting exceeds 1"):
        compile_redaction_patterns(
            "((a))", limits=replace(DEFAULT_REDACTION_PATTERN_LIMITS, max_group_depth=1)
        )
    with pytest.raises(RedactionPatternError, match="NFA exceeds 4 states"):
        compile_redaction_patterns(
            "abc", limits=replace(DEFAULT_REDACTION_PATTERN_LIMITS, max_states_per_pattern=4)
        )
    with pytest.raises(RedactionPatternError, match="aggregate NFA exceeds 8 states"):
        compile_redaction_patterns(
            ("ab", "cd"),
            limits=replace(DEFAULT_REDACTION_PATTERN_LIMITS, max_total_states=8),
        )


def test_limits_require_positive_exact_integers() -> None:
    with pytest.raises(ValueError, match="max_repeat"):
        replace(DEFAULT_REDACTION_PATTERN_LIMITS, max_repeat=0)
    with pytest.raises(ValueError, match="max_repeat"):
        RedactionPatternLimits(max_repeat=True)


@pytest.mark.parametrize("value", ("", "ab", 1, None))
def test_optional_tokens_require_one_exact_character(value: object) -> None:
    with pytest.raises(ValueError, match="exactly one character"):
        OptionalRedactionToken(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    (
        {"limit": 0},
        {"limit": True},
        {"limit": 4, "used": -1},
        {"limit": 4, "used": 5},
        {"limit": 4, "exhausted": 1},
    ),
)
def test_work_budgets_reject_invalid_state(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="redaction pattern work"):
        RedactionPatternWorkBudget(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", (0, -1, True, 1.5))
def test_work_budgets_consume_only_positive_exact_integers(amount: object) -> None:
    with pytest.raises(ValueError, match="amount must be a positive integer"):
        RedactionPatternWorkBudget().consume(amount)  # type: ignore[arg-type]


def test_compilation_is_cached_by_sources_and_limits() -> None:
    first = compile_redaction_patterns(("customer",), builtin_patterns=("password",))
    second = compile_redaction_patterns(("customer",), builtin_patterns=("password",))

    assert first is second


def test_import_star_exports_only_existing_public_names() -> None:
    namespace: dict[str, object] = {}
    exec("from django_ray.redaction_patterns import *", {}, namespace)

    assert set(redaction_patterns.__all__) <= namespace.keys()
    assert all(hasattr(redaction_patterns, name) for name in redaction_patterns.__all__)


@pytest.mark.parametrize(
    ("pattern", "tokens"),
    (
        (r"client(id)+", ("clienti", OptionalRedactionToken("d"))),
        (r"customer_.*_email", ("customer_foo_emai", OptionalRedactionToken("l"))),
        (
            r"bearer\s+\S+",
            ("beare", OptionalRedactionToken("r"), " ", OptionalRedactionToken("x")),
        ),
        (
            r"password",
            (
                "p",
                OptionalRedactionToken("a"),
                "ss",
                OptionalRedactionToken("x"),
                "word",
            ),
        ),
    ),
)
def test_current_selective_terminal_final_bypasses_are_matched(
    pattern: str,
    tokens: tuple[str | OptionalRedactionToken, ...],
) -> None:
    assert _brute_force_search((pattern,), tokens)
    assert compile_redaction_patterns(pattern).search_tokens(tokens)


def test_many_decoy_optional_finals_do_not_require_candidate_enumeration() -> None:
    program = compile_redaction_patterns("clientid")
    tokens = (
        "clienti",
        *(OptionalRedactionToken("X") for _ in range(256)),
        OptionalRedactionToken("d"),
    )
    metrics = RedactionPatternSearchMetrics()

    assert program.search_tokens(tokens, metrics=metrics)
    assert metrics.input_characters == 264
    assert metrics.active_state_visits <= metrics.input_characters * program.state_count


def test_token_lattice_matches_deterministic_brute_force_oracle() -> None:
    patterns = (r"ab", r"a[xy]?b", r"(?:ab|ba)+", r"a.*c", r"\d{1,2}x")
    program = compile_redaction_patterns(patterns)
    alphabet: tuple[str | OptionalRedactionToken, ...] = (
        "a",
        "b",
        OptionalRedactionToken("a"),
        OptionalRedactionToken("x"),
    )

    for length in range(6):
        for tokens in itertools.product(alphabet, repeat=length):
            assert program.search_tokens(tokens) is _brute_force_search(patterns, tokens)


def test_caller_owned_optional_tokens_use_an_adapter_without_coupling() -> None:
    @dataclass(frozen=True, slots=True)
    class CallerOptional:
        character: str

    tokens: tuple[object, ...] = ("clienti", CallerOptional("d"))
    program = compile_redaction_patterns("clientid")

    assert program.search_tokens(
        tokens,
        optional_character=lambda token: (
            token.character if isinstance(token, CallerOptional) else None
        ),
    )


def test_invalid_token_adapters_are_rejected() -> None:
    program = compile_redaction_patterns("clientid")

    with pytest.raises(ValueError, match="exactly one character"):
        program.search_tokens((object(),), optional_character=lambda _token: "too-long")
    with pytest.raises(TypeError, match="mandatory_text"):
        program.search_tokens((object(),))


def test_work_budget_is_cumulative_and_exhaustion_fails_closed() -> None:
    program = compile_redaction_patterns(("password", "customer_.*_email"))
    probe = RedactionPatternWorkBudget(limit=10_000)
    assert not program.search("ordinary", budget=probe)
    first_projection_work = probe.used
    assert first_projection_work > 0

    budget = RedactionPatternWorkBudget(limit=first_projection_work + 1)
    assert not program.search("ordinary", budget=budget)
    assert program.search("second projection", budget=budget)
    assert budget.exhausted
    assert budget.used == budget.limit
    assert program.search("anything", budget=budget)


def test_default_work_budget_is_enforced_without_caller_bookkeeping() -> None:
    limits = replace(DEFAULT_REDACTION_PATTERN_LIMITS, max_work_units_per_value=10)
    program = compile_redaction_patterns("password", limits=limits)

    # Budget exhaustion is a positive match so callers fail closed.
    assert program.search("ordinary diagnostic")


def test_empty_mandatory_token_stream_cannot_evade_work_budget() -> None:
    program = compile_redaction_patterns("password")
    budget = RedactionPatternWorkBudget(limit=10)

    assert program.search_tokens(itertools.repeat(""), budget=budget)
    assert budget.exhausted


def test_default_budget_accepts_a_maximum_sized_ordinary_builtin_scan() -> None:
    builtins = (
        "password",
        "passwd",
        "secret",
        "token",
        "api[_-]?key",
        "authorization",
        "cookie",
        "credential",
        "private[_-]?key",
    )
    program = compile_redaction_patterns(builtin_patterns=builtins)
    budget = program.new_work_budget()

    assert not program.search("z" * (64 * 1024), budget=budget)
    assert not budget.exhausted


def test_predicate_cache_has_a_hard_entry_bound_for_unique_unicode() -> None:
    limits = replace(
        DEFAULT_REDACTION_PATTERN_LIMITS,
        max_predicate_cache_entries=4,
        max_work_units_per_value=100_000,
    )
    program = compile_redaction_patterns(("password", "customer"), limits=limits)
    metrics = RedactionPatternSearchMetrics()
    value = "".join(chr(0x400 + index) for index in range(100))

    assert not program.search(value, metrics=metrics)
    assert metrics.predicate_cache_peak == 4
    assert metrics.predicate_evaluations > 4 * program.pattern_count


def test_transition_cache_bounds_and_accelerates_repeated_adversarial_states() -> None:
    limits = replace(
        DEFAULT_REDACTION_PATTERN_LIMITS,
        max_transition_cache_entries=4,
    )
    program = compile_redaction_patterns(r"(a?){63}b", limits=limits)
    metrics = RedactionPatternSearchMetrics()
    budget = program.new_work_budget()

    assert not program.search("a" * 4096, metrics=metrics, budget=budget)
    assert not budget.exhausted
    assert metrics.transition_cache_hits > 0
    assert metrics.transition_cache_peak <= limits.max_transition_cache_entries


def test_transition_cache_size_does_not_change_matching_semantics() -> None:
    patterns = (r"(a?){8}b", r"client(id)+", r"token.{0,8}")
    default = compile_redaction_patterns(patterns)
    one_entry = compile_redaction_patterns(
        patterns,
        limits=replace(DEFAULT_REDACTION_PATTERN_LIMITS, max_transition_cache_entries=1),
    )
    values = ("a" * 32, "aaaaaaaab", "clientid", "ordinary", "token12345678")

    assert [one_entry.search(value) for value in values] == [
        default.search(value) for value in values
    ]


def test_high_entropy_maximum_input_exhausts_the_bounded_default_work_budget() -> None:
    program = compile_redaction_patterns(
        builtin_patterns=(
            "password",
            "passwd",
            "secret",
            "token",
            "api[_-]?key",
            "authorization",
            "cookie",
            "credential",
            "private[_-]?key",
        )
    )
    budget = program.new_work_budget()
    high_entropy = "".join(chr(0x10000 + index) for index in range(64 * 1024))

    # Exhaustion is a positive match so callers redact instead of spending
    # unbounded CPU on a pathological diagnostic.
    assert program.search(high_entropy, budget=budget)
    assert budget.exhausted
    assert budget.used == budget.limit


def test_instrumented_search_work_scales_linearly() -> None:
    program = compile_redaction_patterns(
        (
            "password",
            "api[_-]?key",
            "customer_.*_email",
            r"bearer\s+\S+",
        )
    )

    def measure(repetitions: int) -> RedactionPatternSearchMetrics:
        tokens = tuple(
            token for _ in range(repetitions) for token in ("z", OptionalRedactionToken("X"))
        )
        metrics = RedactionPatternSearchMetrics()
        budget = program.new_work_budget(limit=10_000_000)
        assert not program.search_tokens(tokens, metrics=metrics, budget=budget)
        assert not budget.exhausted
        return metrics

    small = measure(1000)
    large = measure(2000)

    assert large.input_characters == small.input_characters * 2
    assert large.active_state_visits <= (small.active_state_visits * 2) + program.state_count
    assert large.transition_checks <= (small.transition_checks * 2) + program.state_count
    assert large.predicate_evaluations == small.predicate_evaluations
    assert large.active_state_visits <= large.input_characters * program.state_count
