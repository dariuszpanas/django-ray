"""Bounded regular-expression matching over ambiguous terminal text.

Terminal control sequences can make one printable final byte ambiguous: it may
be terminal syntax, or it may be part of text which a fail-closed redaction
policy must inspect.  Enumerating every keep/drop combination is exponential.
This module instead compiles a deliberately regular subset of Python's regex
syntax to a Thompson NFA and evaluates that NFA directly over the token lattice.

The compiler rejects assertions and other context-sensitive or non-regular
constructs.  Accepted patterns have exact existence semantics under
``re.IGNORECASE`` and are matched in work proportional to input length times
the configured NFA-state bound.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Never

REDACTION_PATTERN_MAX_CONFIGURED_COUNT = 64
REDACTION_PATTERN_MAX_PROGRAM_COUNT = 80
REDACTION_PATTERN_MAX_SOURCE_BYTES = 256
REDACTION_PATTERN_MAX_CONFIGURED_SOURCE_BYTES = 4096
REDACTION_PATTERN_MAX_PROGRAM_SOURCE_BYTES = 8192
REDACTION_PATTERN_MAX_STATES = 512
REDACTION_PATTERN_MAX_TOTAL_STATES = 2048
REDACTION_PATTERN_MAX_REPEAT = 128
REDACTION_PATTERN_MAX_GROUP_DEPTH = 32
REDACTION_PATTERN_MAX_PREDICATE_CACHE_ENTRIES = 256
REDACTION_PATTERN_MAX_TRANSITION_CACHE_ENTRIES = 256
REDACTION_PATTERN_MAX_WORK_UNITS_PER_VALUE = 250_000


class RedactionPatternError(ValueError):
    """Raised when a redaction pattern is invalid, unsupported, or oversized."""


@dataclass(frozen=True, slots=True)
class RedactionPatternLimits:
    """Explicit compiler bounds for one immutable redaction program."""

    max_configured_patterns: int = REDACTION_PATTERN_MAX_CONFIGURED_COUNT
    max_program_patterns: int = REDACTION_PATTERN_MAX_PROGRAM_COUNT
    max_pattern_source_bytes: int = REDACTION_PATTERN_MAX_SOURCE_BYTES
    max_configured_source_bytes: int = REDACTION_PATTERN_MAX_CONFIGURED_SOURCE_BYTES
    max_program_source_bytes: int = REDACTION_PATTERN_MAX_PROGRAM_SOURCE_BYTES
    max_states_per_pattern: int = REDACTION_PATTERN_MAX_STATES
    max_total_states: int = REDACTION_PATTERN_MAX_TOTAL_STATES
    max_repeat: int = REDACTION_PATTERN_MAX_REPEAT
    max_group_depth: int = REDACTION_PATTERN_MAX_GROUP_DEPTH
    max_predicate_cache_entries: int = REDACTION_PATTERN_MAX_PREDICATE_CACHE_ENTRIES
    max_transition_cache_entries: int = REDACTION_PATTERN_MAX_TRANSITION_CACHE_ENTRIES
    max_work_units_per_value: int = REDACTION_PATTERN_MAX_WORK_UNITS_PER_VALUE

    def __post_init__(self) -> None:
        for name in (
            "max_configured_patterns",
            "max_program_patterns",
            "max_pattern_source_bytes",
            "max_configured_source_bytes",
            "max_program_source_bytes",
            "max_states_per_pattern",
            "max_total_states",
            "max_repeat",
            "max_group_depth",
            "max_predicate_cache_entries",
            "max_transition_cache_entries",
            "max_work_units_per_value",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_REDACTION_PATTERN_LIMITS = RedactionPatternLimits()


@dataclass(frozen=True, slots=True)
class OptionalRedactionToken:
    """One optional character in an ambiguous input token lattice."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or len(self.value) != 1:
            raise ValueError("optional redaction tokens must contain exactly one character")


@dataclass(slots=True)
class RedactionPatternSearchMetrics:
    """Deterministic work counters for tests and operational benchmarks."""

    input_characters: int = 0
    active_state_visits: int = 0
    transition_checks: int = 0
    predicate_evaluations: int = 0
    predicate_cache_peak: int = 0
    transition_cache_hits: int = 0
    transition_cache_peak: int = 0


@dataclass(slots=True)
class RedactionPatternWorkBudget:
    """A cumulative deterministic work allowance shared across projections."""

    limit: int = REDACTION_PATTERN_MAX_WORK_UNITS_PER_VALUE
    used: int = 0
    exhausted: bool = False

    def __post_init__(self) -> None:
        if type(self.limit) is not int or self.limit <= 0:
            raise ValueError("redaction pattern work limit must be a positive integer")
        if type(self.used) is not int or self.used < 0 or self.used > self.limit:
            raise ValueError("redaction pattern work used must be within its limit")
        if type(self.exhausted) is not bool:
            raise ValueError("redaction pattern work exhausted must be a boolean")

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(self, amount: int = 1) -> bool:
        """Consume deterministic units and report whether work may continue."""
        if type(amount) is not int or amount <= 0:
            raise ValueError("redaction pattern work amount must be a positive integer")
        if self.exhausted or amount > self.remaining:
            self.used = self.limit
            self.exhausted = True
            return False
        self.used += amount
        return True


@dataclass(frozen=True, slots=True)
class _Empty:
    pass


@dataclass(frozen=True, slots=True)
class _Atom:
    source: str


@dataclass(frozen=True, slots=True)
class _Concatenation:
    parts: tuple[_Node, ...]


@dataclass(frozen=True, slots=True)
class _Alternation:
    branches: tuple[_Node, ...]


@dataclass(frozen=True, slots=True)
class _Repeat:
    node: _Node
    minimum: int
    maximum: int | None


_Node = _Empty | _Atom | _Concatenation | _Alternation | _Repeat


_BRACED_EXACT = re.compile(r"\{([0-9]+)\}")
_BRACED_RANGE = re.compile(r"\{([0-9]*),([0-9]*)\}")
_HEX = frozenset("0123456789abcdefABCDEF")
_OCTAL = frozenset("01234567")
_ASSERTION_ESCAPES = frozenset({"A", "B", "Z", "b", "z"})
_SHORTHAND_ESCAPES = frozenset("dDsSwW")
_CHARACTER_ESCAPES = frozenset("abfnrtv")


class _Parser:
    """Parse the consuming regular subset after Python validates syntax."""

    def __init__(
        self,
        source: str,
        limits: RedactionPatternLimits,
        *,
        pattern_label: str,
    ) -> None:
        self.source = source
        self.limits = limits
        self.pattern_label = pattern_label
        self.position = 0
        self.group_depth = 0

    def parse(self) -> _Node:
        node = self._parse_alternation()
        if self.position != len(self.source):
            self._fail("unexpected closing group")
        return node

    def _fail(self, message: str, *, position: int | None = None) -> Never:
        offset = self.position if position is None else position
        raise RedactionPatternError(f"{self.pattern_label} at character {offset}: {message}")

    def _parse_alternation(self) -> _Node:
        branches = [self._parse_concatenation()]
        while self._peek() == "|":
            self.position += 1
            branches.append(self._parse_concatenation())
        if len(branches) == 1:
            return branches[0]
        return _Alternation(tuple(branches))

    def _parse_concatenation(self) -> _Node:
        parts: list[_Node] = []
        while self.position < len(self.source) and self._peek() not in {"|", ")"}:
            parts.append(self._parse_piece())
        if not parts:
            return _Empty()
        if len(parts) == 1:
            return parts[0]
        return _Concatenation(tuple(parts))

    def _parse_piece(self) -> _Node:
        node = self._parse_atom()
        repeat = self._parse_repeat()
        if repeat is None:
            return node
        minimum, maximum = repeat
        if self._peek() == "?":
            # Greedy and lazy quantifiers recognize the same language.
            self.position += 1
        elif self._peek() == "+":
            self._fail("possessive quantifiers are not supported")
        return _Repeat(node=node, minimum=minimum, maximum=maximum)

    def _parse_atom(self) -> _Node:
        start = self.position
        character = self._peek()
        if character is None:
            self._fail("expected a consuming atom")
        assert character is not None
        if character == "(":
            return self._parse_group()
        if character == "[":
            return self._parse_character_class()
        if character == "\\":
            return self._parse_escape()
        if character == ".":
            self.position += 1
            return _Atom(".")
        if character in {"^", "$"}:
            self._fail("anchors and zero-width assertions are not supported")
        if character in {"*", "+", "?"}:
            self._fail("quantifier has no consuming atom")
        self.position += 1
        return _Atom(re.escape(self.source[start]))

    def _parse_group(self) -> _Node:
        start = self.position
        self.position += 1
        if self._peek() == "?":
            if not self.source.startswith("?:", self.position):
                self._fail(
                    "lookaround, flags, named groups, conditionals, and atomic groups "
                    "are not supported",
                    position=start,
                )
            self.position += 2
        self.group_depth += 1
        if self.group_depth > self.limits.max_group_depth:
            self._fail(
                f"group nesting exceeds {self.limits.max_group_depth}",
                position=start,
            )
        node = self._parse_alternation()
        if self._peek() != ")":
            self._fail("group is not closed", position=start)
        self.position += 1
        self.group_depth -= 1
        return node

    def _parse_character_class(self) -> _Node:
        start = self.position
        self.position += 1
        if self._peek() == "^":
            self.position += 1
        if self._peek() == "]":
            self.position += 1
        previous_unescaped: str | None = None
        while self.position < len(self.source):
            character = self.source[self.position]
            if character == "\\":
                self.position += 2
                previous_unescaped = None
                continue
            if character == "[" or (
                character in {"&", "-", "|", "~"} and character == previous_unescaped
            ):
                self._fail(
                    "ambiguous future character-class syntax is not supported",
                    position=start,
                )
            self.position += 1
            if character == "]":
                return _Atom(self.source[start : self.position])
            previous_unescaped = character
        self._fail("character class is not closed", position=start)

    def _parse_escape(self) -> _Node:
        start = self.position
        self.position += 1
        escaped = self._peek()
        if escaped is None:
            self._fail("trailing escape", position=start)
        assert escaped is not None
        self.position += 1
        if escaped in _ASSERTION_ESCAPES:
            self._fail(
                "anchors and word-boundary assertions are not supported",
                position=start,
            )
        if escaped.isdigit() and escaped != "0":
            self._fail("numeric backreferences are not supported", position=start)
        if escaped in _SHORTHAND_ESCAPES or escaped in _CHARACTER_ESCAPES:
            return _Atom(self.source[start : self.position])
        if escaped == "0":
            for _ in range(2):
                if self._peek() not in _OCTAL:
                    break
                self.position += 1
            return _Atom(self.source[start : self.position])
        if escaped == "x":
            self._consume_hex_digits(2, start=start)
            return _Atom(self.source[start : self.position])
        if escaped == "u":
            self._consume_hex_digits(4, start=start)
            return _Atom(self.source[start : self.position])
        if escaped == "U":
            self._consume_hex_digits(8, start=start)
            return _Atom(self.source[start : self.position])
        if escaped == "N":
            if self._peek() != "{":
                self._fail("named Unicode escapes must use \\N{...}", position=start)
            closing = self.source.find("}", self.position + 1)
            if closing < 0:
                self._fail("named Unicode escape is not closed", position=start)
            self.position = closing + 1
            return _Atom(self.source[start : self.position])
        if escaped.isalpha():
            self._fail("alphabetic escape is not supported", position=start)
        return _Atom(self.source[start : self.position])

    def _consume_hex_digits(self, count: int, *, start: int) -> None:
        end = self.position + count
        if end > len(self.source) or any(
            character not in _HEX for character in self.source[self.position : end]
        ):
            self._fail("hexadecimal escape is incomplete", position=start)
        self.position = end

    def _parse_repeat(self) -> tuple[int, int | None] | None:
        character = self._peek()
        if character == "?":
            self.position += 1
            return 0, 1
        if character == "*":
            self.position += 1
            return 0, None
        if character == "+":
            self.position += 1
            return 1, None
        if character != "{":
            return None

        exact = _BRACED_EXACT.match(self.source, self.position)
        if exact is not None:
            self.position = exact.end()
            minimum = maximum = int(exact.group(1))
        else:
            ranged = _BRACED_RANGE.match(self.source, self.position)
            if ranged is None or (not ranged.group(1) and not ranged.group(2)):
                return None
            self.position = ranged.end()
            minimum = int(ranged.group(1) or 0)
            maximum = int(ranged.group(2)) if ranged.group(2) else None

        largest = minimum if maximum is None else max(minimum, maximum)
        if largest > self.limits.max_repeat:
            self._fail(
                f"repeat bound exceeds {self.limits.max_repeat}",
                position=self.position,
            )
        return minimum, maximum

    def _peek(self) -> str | None:
        if self.position >= len(self.source):
            return None
        return self.source[self.position]


def _nullable(node: _Node) -> bool:
    if isinstance(node, _Empty):
        return True
    if isinstance(node, _Atom):
        return False
    if isinstance(node, _Concatenation):
        return all(_nullable(part) for part in node.parts)
    if isinstance(node, _Alternation):
        return any(_nullable(branch) for branch in node.branches)
    return node.minimum == 0 or _nullable(node.node)


@dataclass(slots=True)
class _MutableState:
    epsilon: list[int] = field(default_factory=list)
    transitions: list[tuple[int, int]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _State:
    epsilon: tuple[int, ...]
    transitions: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class _Predicate:
    source: str
    compiled: re.Pattern[str]

    def matches(self, character: str) -> bool:
        return self.compiled.fullmatch(character) is not None


@dataclass(frozen=True, slots=True)
class _Fragment:
    start: int
    end: int


class _Builder:
    def __init__(self, limits: RedactionPatternLimits) -> None:
        self.limits = limits
        self.states: list[_MutableState] = []
        self.predicates: list[_Predicate] = []
        self.predicate_ids: dict[str, int] = {}
        self.pattern_label = "redaction program"

    def new_state(self) -> int:
        if len(self.states) >= self.limits.max_total_states:
            raise RedactionPatternError(
                f"{self.pattern_label}: aggregate NFA exceeds {self.limits.max_total_states} states"
            )
        self.states.append(_MutableState())
        return len(self.states) - 1

    def add_epsilon(self, source: int, destination: int) -> None:
        self.states[source].epsilon.append(destination)

    def add_transition(self, source: int, predicate: str, destination: int) -> None:
        predicate_id = self.predicate_ids.get(predicate)
        if predicate_id is None:
            failure_message: str | None = None
            compiled: re.Pattern[str] | None = None
            try:
                compiled = re.compile(predicate, re.IGNORECASE)
            except re.error as error:  # pragma: no cover - parser invariant
                failure_message = error.msg
            if failure_message is not None:  # pragma: no cover - parser invariant
                raise RedactionPatternError(
                    f"{self.pattern_label}: internal character predicate is invalid "
                    f"({failure_message})"
                )
            if compiled is None:  # pragma: no cover - defensive invariant
                raise AssertionError("validated redaction predicate did not compile")
            predicate_id = len(self.predicates)
            self.predicates.append(_Predicate(source=predicate, compiled=compiled))
            self.predicate_ids[predicate] = predicate_id
        self.states[source].transitions.append((predicate_id, destination))

    def compile(self, node: _Node) -> _Fragment:
        if isinstance(node, _Empty):
            return self._empty_fragment()
        if isinstance(node, _Atom):
            start = self.new_state()
            end = self.new_state()
            self.add_transition(start, node.source, end)
            return _Fragment(start, end)
        if isinstance(node, _Concatenation):
            return self._concatenate([self.compile(part) for part in node.parts])
        if isinstance(node, _Alternation):
            start = self.new_state()
            end = self.new_state()
            for branch in node.branches:
                fragment = self.compile(branch)
                self.add_epsilon(start, fragment.start)
                self.add_epsilon(fragment.end, end)
            return _Fragment(start, end)
        return self._compile_repeat(node)

    def _empty_fragment(self) -> _Fragment:
        start = self.new_state()
        end = self.new_state()
        self.add_epsilon(start, end)
        return _Fragment(start, end)

    def _concatenate(self, fragments: list[_Fragment]) -> _Fragment:
        if not fragments:
            return self._empty_fragment()
        for left, right in zip(fragments, fragments[1:], strict=False):
            self.add_epsilon(left.end, right.start)
        return _Fragment(fragments[0].start, fragments[-1].end)

    def _optional(self, fragment: _Fragment) -> _Fragment:
        start = self.new_state()
        end = self.new_state()
        self.add_epsilon(start, end)
        self.add_epsilon(start, fragment.start)
        self.add_epsilon(fragment.end, end)
        return _Fragment(start, end)

    def _star(self, fragment: _Fragment) -> _Fragment:
        start = self.new_state()
        end = self.new_state()
        self.add_epsilon(start, end)
        self.add_epsilon(start, fragment.start)
        self.add_epsilon(fragment.end, fragment.start)
        self.add_epsilon(fragment.end, end)
        return _Fragment(start, end)

    def _compile_repeat(self, repeat: _Repeat) -> _Fragment:
        if repeat.maximum is None and repeat.minimum == 0:
            return self._star(self.compile(repeat.node))

        mandatory = [self.compile(repeat.node) for _ in range(repeat.minimum)]
        if repeat.maximum is None:
            if not mandatory:  # pragma: no cover - handled above
                return self._star(self.compile(repeat.node))
            combined = self._concatenate(mandatory)
            final = mandatory[-1]
            end = self.new_state()
            self.add_epsilon(final.end, final.start)
            self.add_epsilon(final.end, end)
            return _Fragment(combined.start, end)

        optional_count = repeat.maximum - repeat.minimum
        optional = [self._optional(self.compile(repeat.node)) for _ in range(optional_count)]
        return self._concatenate([*mandatory, *optional])


def _epsilon_closures(states: tuple[_State, ...]) -> tuple[int, ...]:
    closures: list[int] = []
    for origin in range(len(states)):
        closure = 0
        pending = [origin]
        while pending:
            state = pending.pop()
            bit = 1 << state
            if closure & bit:
                continue
            closure |= bit
            pending.extend(states[state].epsilon)
        closures.append(closure)
    return tuple(closures)


def _default_optional_character(token: object) -> str | None:
    if isinstance(token, OptionalRedactionToken):
        return token.value
    return None


def _default_mandatory_text(token: object) -> str:
    if type(token) is not str:
        raise TypeError(
            "mandatory redaction tokens must be strings or use a mandatory_text adapter"
        )
    return token


@dataclass(frozen=True, slots=True)
class RedactionPatternProgram:
    """One immutable, thread-safe NFA program for configured patterns."""

    builtin_sources: tuple[str, ...]
    configured_sources: tuple[str, ...]
    limits: RedactionPatternLimits
    _states: tuple[_State, ...]
    _predicates: tuple[_Predicate, ...]
    _closures: tuple[int, ...]
    _start_closure: int
    _accept_mask: int

    @property
    def pattern_count(self) -> int:
        return len(self.builtin_sources) + len(self.configured_sources)

    @property
    def builtin_pattern_count(self) -> int:
        return len(self.builtin_sources)

    @property
    def configured_pattern_count(self) -> int:
        return len(self.configured_sources)

    @property
    def state_count(self) -> int:
        return len(self._states)

    def new_work_budget(self, *, limit: int | None = None) -> RedactionPatternWorkBudget:
        """Return a budget suitable for sharing across every value projection."""
        return RedactionPatternWorkBudget(
            limit=self.limits.max_work_units_per_value if limit is None else limit
        )

    def search(
        self,
        value: str,
        *,
        metrics: RedactionPatternSearchMetrics | None = None,
        budget: RedactionPatternWorkBudget | None = None,
    ) -> bool:
        """Return whether any configured pattern searches successfully in text."""
        if type(value) is not str:
            raise TypeError("redaction pattern search input must be text")
        return self.search_tokens((value,), metrics=metrics, budget=budget)

    def search_tokens(
        self,
        tokens: Iterable[object],
        *,
        optional_character: Callable[[object], str | None] = _default_optional_character,
        mandatory_text: Callable[[object], str] = _default_mandatory_text,
        metrics: RedactionPatternSearchMetrics | None = None,
        budget: RedactionPatternWorkBudget | None = None,
    ) -> bool:
        """Search mandatory text and optional-character tokens without enumeration.

        ``optional_character`` adapts a caller-owned token type by returning its
        one optional character, or ``None`` for a mandatory token.  Mandatory
        tokens may contain more than one character and are consumed in order.
        """
        if budget is None:
            budget = self.new_work_budget()
        elif not isinstance(budget, RedactionPatternWorkBudget):
            raise TypeError("budget must be a RedactionPatternWorkBudget instance")
        if budget.exhausted:
            return True
        if self._accept_mask == 0:
            return False

        active = self._start_closure
        predicate_cache: dict[str, int] = {}
        transition_cache: dict[tuple[int, int], int] = {}
        for token in tokens:
            # Charge token dispatch separately so an iterable of empty
            # mandatory chunks cannot evade the per-value work boundary.
            if not budget.consume():
                return True
            optional = optional_character(token)
            if optional is not None:
                if type(optional) is not str or len(optional) != 1:
                    raise ValueError("optional_character must return None or exactly one character")
                if metrics is not None:
                    metrics.input_characters += 1
                if not budget.consume():
                    return True
                kept = self._advance(
                    active,
                    optional,
                    predicate_cache,
                    transition_cache,
                    metrics,
                    budget,
                )
                if kept is None:
                    return True
                active |= kept
                if active & self._accept_mask:
                    return True
                active |= self._start_closure
                continue

            text = mandatory_text(token)
            if type(text) is not str:
                raise TypeError("mandatory_text must return a string")
            for character in text:
                if metrics is not None:
                    metrics.input_characters += 1
                if not budget.consume():
                    return True
                active |= self._start_closure
                advanced = self._advance(
                    active,
                    character,
                    predicate_cache,
                    transition_cache,
                    metrics,
                    budget,
                )
                if advanced is None:
                    return True
                active = advanced
                if active & self._accept_mask:
                    return True
                active |= self._start_closure
        return False

    def _advance(
        self,
        active: int,
        character: str,
        predicate_cache: dict[str, int],
        transition_cache: dict[tuple[int, int], int],
        metrics: RedactionPatternSearchMetrics | None,
        budget: RedactionPatternWorkBudget,
    ) -> int | None:
        matching_predicates = predicate_cache.get(character, -1)
        if matching_predicates < 0:
            matching_predicates = 0
            for predicate_id, predicate in enumerate(self._predicates):
                if not budget.consume():
                    return None
                if metrics is not None:
                    metrics.predicate_evaluations += 1
                if predicate.matches(character):
                    matching_predicates |= 1 << predicate_id
            if len(predicate_cache) < self.limits.max_predicate_cache_entries:
                predicate_cache[character] = matching_predicates
                if metrics is not None:
                    metrics.predicate_cache_peak = max(
                        metrics.predicate_cache_peak,
                        len(predicate_cache),
                    )

        transition_key = (active, matching_predicates)
        cached_advance = transition_cache.get(transition_key)
        if cached_advance is not None:
            if metrics is not None:
                metrics.transition_cache_hits += 1
            return cached_advance

        advanced = 0
        remaining = active
        while remaining:
            if not budget.consume():
                return None
            least_bit = remaining & -remaining
            state_index = least_bit.bit_length() - 1
            remaining ^= least_bit
            if metrics is not None:
                metrics.active_state_visits += 1
            for predicate_id, destination in self._states[state_index].transitions:
                if not budget.consume():
                    return None
                if metrics is not None:
                    metrics.transition_checks += 1
                if matching_predicates & (1 << predicate_id):
                    advanced |= self._closures[destination]
        if len(transition_cache) < self.limits.max_transition_cache_entries:
            transition_cache[transition_key] = advanced
            if metrics is not None:
                metrics.transition_cache_peak = max(
                    metrics.transition_cache_peak,
                    len(transition_cache),
                )
        return advanced


def _bounded_source_tuple(
    patterns: Sequence[str] | str,
    *,
    maximum: int,
    error_message: str,
) -> tuple[object, ...]:
    if isinstance(patterns, str):
        return (patterns,)
    values: list[object] = []
    for source in patterns:
        if len(values) >= maximum:
            raise RedactionPatternError(error_message)
        values.append(source)
    return tuple(values)


def _validate_sources(
    builtin_patterns: Sequence[str] | str,
    configured_patterns: Sequence[str] | str,
    limits: RedactionPatternLimits,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    builtin_values = _bounded_source_tuple(
        builtin_patterns,
        maximum=limits.max_program_patterns,
        error_message=(f"combined redaction pattern count exceeds {limits.max_program_patterns}"),
    )
    configured_values = _bounded_source_tuple(
        configured_patterns,
        maximum=limits.max_configured_patterns,
        error_message=(
            f"configured redaction pattern count exceeds {limits.max_configured_patterns}"
        ),
    )
    if len(builtin_values) + len(configured_values) > limits.max_program_patterns:
        raise RedactionPatternError(
            f"combined redaction pattern count exceeds {limits.max_program_patterns}"
        )

    validated: list[tuple[str, ...]] = []
    program_bytes = 0
    configured_bytes = 0
    for kind, values in (
        ("built-in pattern", builtin_values),
        ("configured pattern", configured_values),
    ):
        kind_sources: list[str] = []
        for index, source in enumerate(values):
            label = f"{kind} at index {index}"
            if type(source) is not str or not source:
                raise RedactionPatternError(f"{label}: source must be non-empty text")
            source_bytes: int | None = None
            try:
                source_bytes = len(source.encode("utf-8"))
            except UnicodeEncodeError:
                pass
            if source_bytes is None:
                # Raise outside the exception handler so the rejected source is
                # not retained through ``__context__`` on the public error.
                raise RedactionPatternError(f"{label}: source must be valid UTF-8")
            if source_bytes > limits.max_pattern_source_bytes:
                raise RedactionPatternError(
                    f"{label}: source exceeds {limits.max_pattern_source_bytes} UTF-8 bytes"
                )
            program_bytes += source_bytes
            if kind == "configured pattern":
                configured_bytes += source_bytes
                if configured_bytes > limits.max_configured_source_bytes:
                    raise RedactionPatternError(
                        f"{label}: configured source exceeds "
                        f"{limits.max_configured_source_bytes} UTF-8 bytes"
                    )
            if program_bytes > limits.max_program_source_bytes:
                raise RedactionPatternError(
                    f"{label}: combined program source exceeds "
                    f"{limits.max_program_source_bytes} UTF-8 bytes"
                )
            kind_sources.append(source)
        validated.append(tuple(kind_sources))
    return validated[0], validated[1]


@lru_cache(maxsize=128)
def _compile_cached(
    builtin_sources: tuple[str, ...],
    configured_sources: tuple[str, ...],
    limits: RedactionPatternLimits,
) -> RedactionPatternProgram:
    builder = _Builder(limits)
    super_start = builder.new_state()
    accept_mask = 0
    entries = (
        *(
            (f"built-in pattern at index {index}", source)
            for index, source in enumerate(builtin_sources)
        ),
        *(
            (f"configured pattern at index {index}", source)
            for index, source in enumerate(configured_sources)
        ),
    )
    for pattern_label, source in entries:
        builder.pattern_label = pattern_label
        compile_failure: str | None = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", FutureWarning)
                re.compile(source, re.IGNORECASE)
        except FutureWarning:
            compile_failure = "ambiguous future regular-expression syntax is not supported"
        except re.error as error:
            position = "unknown" if error.pos is None else str(error.pos)
            compile_failure = f"invalid regular-expression syntax at character {position}"
        if compile_failure is not None:
            # Do not chain the engine exception: it retains the rejected source
            # on attributes even when its message is sanitized.
            raise RedactionPatternError(f"{pattern_label}: {compile_failure}")

        node = _Parser(source, limits, pattern_label=pattern_label).parse()
        if _nullable(node):
            raise RedactionPatternError(f"{pattern_label}: pattern can match empty text")
        pattern_state_start = len(builder.states)
        fragment = builder.compile(node)
        accept = builder.new_state()
        builder.add_epsilon(super_start, fragment.start)
        builder.add_epsilon(fragment.end, accept)
        pattern_states = len(builder.states) - pattern_state_start
        if pattern_states > limits.max_states_per_pattern:
            raise RedactionPatternError(
                f"{pattern_label}: NFA exceeds {limits.max_states_per_pattern} states"
            )
        accept_mask |= 1 << accept

    states = tuple(
        _State(epsilon=tuple(state.epsilon), transitions=tuple(state.transitions))
        for state in builder.states
    )
    closures = _epsilon_closures(states)
    return RedactionPatternProgram(
        builtin_sources=builtin_sources,
        configured_sources=configured_sources,
        limits=limits,
        _states=states,
        _predicates=tuple(builder.predicates),
        _closures=closures,
        _start_closure=closures[super_start],
        _accept_mask=accept_mask,
    )


def compile_redaction_patterns(
    configured_patterns: Sequence[str] | str = (),
    *,
    builtin_patterns: Sequence[str] | str = (),
    limits: RedactionPatternLimits = DEFAULT_REDACTION_PATTERN_LIMITS,
) -> RedactionPatternProgram:
    """Validate and cache one bounded NFA program for redaction patterns."""
    if not isinstance(limits, RedactionPatternLimits):
        raise TypeError("limits must be a RedactionPatternLimits instance")
    builtin_sources, configured_sources = _validate_sources(
        builtin_patterns,
        configured_patterns,
        limits,
    )
    return _compile_cached(builtin_sources, configured_sources, limits)


__all__ = [
    "DEFAULT_REDACTION_PATTERN_LIMITS",
    "OptionalRedactionToken",
    "REDACTION_PATTERN_MAX_CONFIGURED_COUNT",
    "REDACTION_PATTERN_MAX_CONFIGURED_SOURCE_BYTES",
    "REDACTION_PATTERN_MAX_GROUP_DEPTH",
    "REDACTION_PATTERN_MAX_PREDICATE_CACHE_ENTRIES",
    "REDACTION_PATTERN_MAX_PROGRAM_COUNT",
    "REDACTION_PATTERN_MAX_PROGRAM_SOURCE_BYTES",
    "REDACTION_PATTERN_MAX_REPEAT",
    "REDACTION_PATTERN_MAX_SOURCE_BYTES",
    "REDACTION_PATTERN_MAX_STATES",
    "REDACTION_PATTERN_MAX_TOTAL_STATES",
    "REDACTION_PATTERN_MAX_TRANSITION_CACHE_ENTRIES",
    "REDACTION_PATTERN_MAX_WORK_UNITS_PER_VALUE",
    "RedactionPatternError",
    "RedactionPatternLimits",
    "RedactionPatternProgram",
    "RedactionPatternSearchMetrics",
    "RedactionPatternWorkBudget",
    "compile_redaction_patterns",
]
