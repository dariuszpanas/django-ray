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
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django_ray.redaction_patterns import (
    RedactionPatternError,
    RedactionPatternProgram,
    RedactionPatternWorkBudget,
    compile_redaction_patterns,
)

REDACTED = "[REDACTED]"

_ESC = "\x1b"
_C1_CSI = "\x9b"
_C1_ST = "\x9c"
_CANCEL_CONTROL_CHARACTERS = frozenset({"\x18", "\x1a"})
_STRING_CONTROL_INTRODUCERS = {
    "\x90": False,  # DCS
    "\x98": False,  # SOS
    "\x9d": True,  # OSC
    "\x9e": False,  # PM
    "\x9f": False,  # APC
}
_ESC_STRING_CONTROL_INTRODUCERS = {
    "P": False,  # DCS
    "]": True,  # OSC
    "^": False,  # PM
    "_": False,  # APC
    "X": False,  # SOS
}
_TERMINAL_SEQUENCE_MAX_CHARS = 4096
_REDACTION_TEXT_MAX_CHARS = 64 * 1024
_REDACTION_VALUE_MAX_ITEMS = 4096
_RESULT_TYPE_MAX_CHARS = 256
_RESULT_TYPE_MAX_BYTES = 256

# Frozen from Unicode 16.0's ``Cf`` assignments so supported Python versions
# make the same terminal-safety decision. The source tables are UnicodeData.txt
# and DerivedCoreProperties.txt in the Unicode 16.0 UCD. Join controls and
# emoji tag characters are explicitly preserved below because they shape
# legitimate text without changing terminal state.
_UNSAFE_TERMINAL_FORMAT_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

# DerivedCoreProperties.txt, property ``Default_Ignorable_Code_Point``.
_DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x034F, 0x034F),
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
_UNSAFE_TERMINAL_FORMAT_RANGE_STARTS = tuple(
    start for start, _end in _UNSAFE_TERMINAL_FORMAT_RANGES
)
_DEFAULT_IGNORABLE_RANGE_STARTS = tuple(start for start, _end in _DEFAULT_IGNORABLE_RANGES)

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
) -> RedactionPatternProgram:
    """Compile the bounded built-in plus configured redaction program."""
    if patterns is None:
        try:
            from django.core.exceptions import ImproperlyConfigured
        except ModuleNotFoundError as error:
            if error.name != "django":  # pragma: no cover - defensive import boundary
                raise
            patterns = None
        else:
            try:
                from django_ray.conf.settings import get_settings

                patterns = get_settings().get("REDACT_PATTERNS")
            except ImproperlyConfigured:
                patterns = None
    configured: Sequence[str] | str = () if patterns is None else patterns
    return compile_redaction_patterns(
        configured,
        builtin_patterns=DEFAULT_REDACT_PATTERNS,
    )


def _codepoint_in_ranges(
    codepoint: int,
    ranges: tuple[tuple[int, int], ...],
    starts: tuple[int, ...],
) -> bool:
    index = bisect_right(starts, codepoint) - 1
    return index >= 0 and codepoint <= ranges[index][1]


def _is_terminal_display_control(character: str) -> bool:
    """Return whether a code point is unsafe in inert diagnostic display."""
    codepoint = ord(character)
    if codepoint < 0x20 or codepoint == 0x7F:
        return True
    if codepoint < 0x80:
        return False
    if 0x80 <= codepoint <= 0x9F:
        return True
    if character in {"\u200c", "\u200d"} or codepoint == 0xE0001 or 0xE0020 <= codepoint <= 0xE007F:
        # Join controls shape legitimate scripts and emoji without changing
        # terminal state. Emoji tag characters have the same safe boundary.
        # Matching removes both through the stricter helper.
        return False
    if 0xD800 <= codepoint <= 0xDFFF:
        return True
    return _codepoint_in_ranges(
        codepoint,
        _UNSAFE_TERMINAL_FORMAT_RANGES,
        _UNSAFE_TERMINAL_FORMAT_RANGE_STARTS,
    )


def _is_terminal_matching_control(character: str) -> bool:
    """Return whether a zero-width code point must disappear for matching."""
    codepoint = ord(character)
    if _is_terminal_display_control(character):
        return True
    if codepoint < _DEFAULT_IGNORABLE_RANGES[0][0]:
        return False
    return _codepoint_in_ranges(
        codepoint,
        _DEFAULT_IGNORABLE_RANGES,
        _DEFAULT_IGNORABLE_RANGE_STARTS,
    )


def _is_inert_terminal_text(value: str) -> bool:
    """Return whether text can bypass every terminal projection unchanged."""
    for character in value:
        if character in {"\t", "\n"}:
            continue
        if character == "\r" or _is_terminal_matching_control(character):
            return False
    return True


@dataclass(frozen=True, slots=True)
class _OptionalTerminalFinal:
    """A printable byte which may be syntax or hidden sensitive text."""

    value: str


@dataclass(frozen=True, slots=True)
class _TerminalTextProjections:
    """Inert display text and conservative redaction comparison forms."""

    normalized: str
    normalized_without_controls: str
    controls_removed: str
    matching_tokens: tuple[str | _OptionalTerminalFinal, ...]


class _RedactionValueLimitError(Exception):
    """Stop one structured projection after its aggregate safety bound."""


@dataclass(slots=True)
class _RedactionValueBudget:
    """One root-value budget shared by mapping keys and nested values."""

    pattern_work: RedactionPatternWorkBudget
    item_count: int = 0
    text_characters: int = 0

    def consume_item(self) -> None:
        self.item_count += 1
        if self.item_count > _REDACTION_VALUE_MAX_ITEMS:
            raise _RedactionValueLimitError

    def consume_text(self, value: str) -> None:
        self.text_characters += len(value)
        if self.text_characters > _REDACTION_TEXT_MAX_CHARS:
            raise _RedactionValueLimitError


class _InertTextBuffer:
    """Accumulate printable text while normalizing adjacent CRLF once."""

    __slots__ = ("compact", "display", "skip_lf_after_cr")

    def __init__(self, *, collect_compact: bool) -> None:
        self.display: list[str] = []
        self.compact: list[str] | None = [] if collect_compact else None
        self.skip_lf_after_cr = False

    def append(self, character: str) -> None:
        """Append one raw character using the inert display policy."""
        if character == "\r":
            self.display.append("\n")
            self.skip_lf_after_cr = True
            return
        if character == "\n":
            if not self.skip_lf_after_cr:
                self.display.append("\n")
            self.skip_lf_after_cr = False
            return

        if character == "\t":
            self.skip_lf_after_cr = False
            self.display.append(character)
            return
        if _is_terminal_display_control(character):
            return
        self.skip_lf_after_cr = False
        self.display.append(character)
        if self.compact is not None:
            self.compact.append(character)

    def clear(self) -> None:
        self.display.clear()
        if self.compact is not None:
            self.compact.clear()
        self.skip_lf_after_cr = False

    def extend_from(self, other: _InertTextBuffer) -> None:
        self.display.extend(other.display)
        if self.compact is not None:
            assert other.compact is not None
            self.compact.extend(other.compact)
        self.skip_lf_after_cr = other.skip_lf_after_cr


class _CollapsedIntroducerProjector:
    """Build a fail-closed form without terminal control introducers.

    Unlike the display normalizer, CSI parameter/intermediate bytes are omitted
    while their printable final byte is retained. Complete control-string
    payloads are discarded, while an incomplete payload is recursively
    projected as ordinary text. Those choices must be composed in one parser:
    independent projections can otherwise miss a sensitive token split across
    both sequence families. This form is used only for matching and is never
    displayed.
    """

    __slots__ = (
        "bell_terminated",
        "output",
        "sequence_count",
        "state",
        "string_fallback",
        "string_fallback_depth",
    )

    def __init__(self, *, string_fallback_depth: int = 0) -> None:
        self.output: list[str | _OptionalTerminalFinal] = []
        self.state = "text"
        self.string_fallback_depth = string_fallback_depth
        self.string_fallback: _CollapsedIntroducerProjector | None = None
        self.sequence_count = 0
        self.bell_terminated = False

    def _start_string(self, *, bell_terminated: bool) -> None:
        self.string_fallback = _CollapsedIntroducerProjector(
            string_fallback_depth=self.string_fallback_depth + 1,
        )
        self.state = "string"
        self.sequence_count = 0
        self.bell_terminated = bell_terminated

    def _complete_string(self) -> None:
        self.string_fallback = None
        self.state = "text"
        self.sequence_count = 0

    def _flush_incomplete_string(self) -> None:
        assert self.string_fallback is not None
        self.output.extend(self.string_fallback.finish())
        self.string_fallback = None
        self.state = "text"
        self.sequence_count = 0

    def _from_text(self, character: str) -> None:
        if character == _ESC:
            self.state = "escape"
            return
        if character == _C1_CSI:
            self.state = "csi"
            return
        if character in _STRING_CONTROL_INTRODUCERS:
            if self.string_fallback_depth == 0 or (
                self.string_fallback_depth == 1 and character == "\x9d"
            ):
                self._start_string(
                    bell_terminated=_STRING_CONTROL_INTRODUCERS[character],
                )
            return
        codepoint = ord(character)
        if codepoint >= 0x20 and not _is_terminal_matching_control(character):
            self.output.append(character)
        self.state = "text"

    def feed(self, character: str) -> None:
        if self.state == "text":
            self._from_text(character)
            return
        if self.state == "escape":
            if character in _CANCEL_CONTROL_CHARACTERS:
                self.state = "text"
                return
            if character == _ESC:
                return
            codepoint = ord(character)
            if _is_ignored_sequence_control(character):
                return
            if character == "[":
                self.state = "csi"
            elif character in _ESC_STRING_CONTROL_INTRODUCERS:
                if self.string_fallback_depth == 0 or (
                    self.string_fallback_depth == 1 and character == "]"
                ):
                    self._start_string(
                        bell_terminated=_ESC_STRING_CONTROL_INTRODUCERS[character],
                    )
                else:
                    self.state = "text"
            elif character == "\\":
                self.state = "text"
            elif 0x20 <= codepoint <= 0x2F:
                self.state = "escape_sequence"
            elif 0x30 <= codepoint <= 0x7E:
                self.state = "text"
                self.output.append(_OptionalTerminalFinal(character))
            else:
                self.state = "text"
                self._from_text(character)
            return

        if self.state == "string":
            if character in _CANCEL_CONTROL_CHARACTERS:
                self._complete_string()
                return
            if character == _C1_ST or (self.bell_terminated and character == "\x07"):
                self._complete_string()
                return
            if character == _ESC:
                self.sequence_count += 1
                if self.sequence_count >= _TERMINAL_SEQUENCE_MAX_CHARS:
                    assert self.string_fallback is not None
                    self.string_fallback.feed(character)
                    self._flush_incomplete_string()
                else:
                    self.state = "string_escape"
                return
            assert self.string_fallback is not None
            self.string_fallback.feed(character)
            self.sequence_count += 1
            if self.sequence_count >= _TERMINAL_SEQUENCE_MAX_CHARS:
                self._flush_incomplete_string()
            return
        if self.state == "string_escape":
            if character in _CANCEL_CONTROL_CHARACTERS:
                self._complete_string()
                return
            if (
                character == "\\"
                or character == _C1_ST
                or (self.bell_terminated and character == "\x07")
            ):
                self._complete_string()
                return
            assert self.string_fallback is not None
            if character == _ESC:
                self.sequence_count += 1
                if self.sequence_count >= _TERMINAL_SEQUENCE_MAX_CHARS:
                    self.string_fallback.feed(character)
                    self._flush_incomplete_string()
                return
            self.string_fallback.feed(_ESC)
            self.string_fallback.feed(character)
            self.sequence_count += 1
            if self.sequence_count >= _TERMINAL_SEQUENCE_MAX_CHARS:
                self._flush_incomplete_string()
            else:
                self.state = "string"
            return

        codepoint = ord(character)
        if character in _CANCEL_CONTROL_CHARACTERS:
            self.state = "text"
            return
        if character == _ESC:
            self.state = "escape"
            return
        if _is_ignored_sequence_control(character):
            return
        if self.state == "escape_sequence":
            if 0x20 <= codepoint <= 0x2F:
                return
            self.state = "text"
            if 0x30 <= codepoint <= 0x7E:
                self.output.append(_OptionalTerminalFinal(character))
            else:
                self._from_text(character)
            return
        if 0x20 <= codepoint <= 0x3F:
            return
        self.state = "text"
        if 0x40 <= codepoint <= 0x7E:
            # Keep the final byte in this matching-only projection. A control
            # sequence can otherwise consume one character from a sensitive
            # token even though the inert display correctly removes it.
            self.output.append(_OptionalTerminalFinal(character))
        else:
            self._from_text(character)

    def finish(self) -> list[str | _OptionalTerminalFinal]:
        if self.state == "string_escape":
            assert self.string_fallback is not None
            self.string_fallback.feed(_ESC)
        if self.state in {"string", "string_escape"}:
            self._flush_incomplete_string()
        compacted: list[str | _OptionalTerminalFinal] = []
        text: list[str] = []
        for token in self.output:
            if type(token) is str:
                text.append(token)
                continue
            if text:
                compacted.append("".join(text))
                text.clear()
            compacted.append(token)
        if text:
            compacted.append("".join(text))
        return compacted


def _is_ignored_sequence_control(character: str) -> bool:
    """Return whether a zero-width control may occur inside a sequence."""
    return character not in {"\t", "\n", "\r", _ESC} and _is_terminal_matching_control(
        character,
    )


class _TerminalDisplayNormalizer:
    """Streaming bounded terminal parser used by every diagnostic boundary."""

    __slots__ = (
        "bell_terminated",
        "collect_compact",
        "csi_in_intermediates",
        "fallback",
        "output",
        "sequence_count",
        "sequence_limit",
        "state",
        "string_fallback_depth",
        "string_fallback",
    )

    def __init__(
        self,
        *,
        string_fallback_depth: int = 0,
        collect_compact: bool = True,
    ) -> None:
        self.collect_compact = collect_compact
        self.output = _InertTextBuffer(collect_compact=collect_compact)
        self.fallback = _InertTextBuffer(collect_compact=collect_compact)
        self.state = "text"
        self.string_fallback_depth = string_fallback_depth
        self.sequence_count = 0
        self.sequence_limit = 0
        self.csi_in_intermediates = False
        self.bell_terminated = False
        self.string_fallback: _TerminalDisplayNormalizer | None = None

    def _start_text(self, character: str) -> None:
        if character == _ESC:
            self.state = "escape"
            return
        if character == _C1_CSI:
            self._start_csi(seven_bit=False)
            return
        if character in _STRING_CONTROL_INTRODUCERS:
            if self.string_fallback_depth == 0 or (
                self.string_fallback_depth == 1 and character == "\x9d"
            ):
                self._start_string(
                    bell_terminated=_STRING_CONTROL_INTRODUCERS[character],
                )
            return
        self.output.append(character)

    def _start_csi(self, *, seven_bit: bool) -> None:
        self.fallback.clear()
        if seven_bit:
            self.fallback.append("[")
        self.state = "csi"
        self.sequence_count = 0
        self.sequence_limit = _TERMINAL_SEQUENCE_MAX_CHARS
        self.csi_in_intermediates = False

    def _start_string(
        self,
        *,
        bell_terminated: bool,
    ) -> None:
        self.fallback.clear()
        self.string_fallback = _TerminalDisplayNormalizer(
            string_fallback_depth=self.string_fallback_depth + 1,
            collect_compact=self.collect_compact,
        )
        self.state = "string"
        self.sequence_count = 0
        self.sequence_limit = _TERMINAL_SEQUENCE_MAX_CHARS
        self.bell_terminated = bell_terminated

    def _start_escape_sequence(self, character: str) -> None:
        self.fallback.clear()
        self.fallback.append(character)
        self.state = "escape_sequence"
        self.sequence_count = 1
        self.sequence_limit = 32

    def _complete_sequence(self) -> None:
        self.fallback.clear()
        self.string_fallback = None
        self.state = "text"
        self.sequence_count = 0

    def _flush_malformed_sequence(self) -> None:
        if self.state in {"string", "string_escape"}:
            assert self.string_fallback is not None
            self.string_fallback._finish_pending()
            self.output.extend_from(self.string_fallback.output)
            self.string_fallback = None
        else:
            self.output.extend_from(self.fallback)
        self.fallback.clear()
        self.state = "text"
        self.sequence_count = 0

    def _append_bounded_fallback(self, character: str, *, next_state: str) -> None:
        self.fallback.append(character)
        self.sequence_count += 1
        if self.sequence_count >= self.sequence_limit:
            self._flush_malformed_sequence()
        else:
            self.state = next_state

    def _feed_escape(self, character: str) -> None:
        if character in _CANCEL_CONTROL_CHARACTERS:
            self.state = "text"
            return
        if character == _ESC:
            return
        if _is_ignored_sequence_control(character):
            return
        codepoint = ord(character)
        if character == "[":
            self._start_csi(seven_bit=True)
        elif character in _ESC_STRING_CONTROL_INTRODUCERS:
            if self.string_fallback_depth == 0 or (
                self.string_fallback_depth == 1 and character == "]"
            ):
                self._start_string(
                    bell_terminated=_ESC_STRING_CONTROL_INTRODUCERS[character],
                )
            else:
                self.output.append(character)
                self.state = "text"
        elif 0x20 <= codepoint <= 0x2F:
            self._start_escape_sequence(character)
        elif 0x30 <= codepoint <= 0x7E:
            self.state = "text"
        else:
            self.state = "text"
            self._start_text(character)

    def _feed_escape_sequence(self, character: str) -> None:
        if character in _CANCEL_CONTROL_CHARACTERS:
            self._complete_sequence()
            return
        if character == _ESC:
            self._complete_sequence()
            self.state = "escape"
            return
        if _is_ignored_sequence_control(character):
            return
        codepoint = ord(character)
        if 0x20 <= codepoint <= 0x2F:
            self._append_bounded_fallback(character, next_state="escape_sequence")
        elif 0x30 <= codepoint <= 0x7E:
            self._complete_sequence()
        else:
            self._flush_malformed_sequence()
            self._start_text(character)

    def _feed_csi(self, character: str) -> None:
        if character in _CANCEL_CONTROL_CHARACTERS:
            self._complete_sequence()
            return
        if character == _ESC:
            self._complete_sequence()
            self.state = "escape"
            return
        if _is_ignored_sequence_control(character):
            return
        codepoint = ord(character)
        if not self.csi_in_intermediates and 0x30 <= codepoint <= 0x3F:
            self._append_bounded_fallback(character, next_state="csi")
            return
        if 0x20 <= codepoint <= 0x2F:
            self.csi_in_intermediates = True
            self._append_bounded_fallback(character, next_state="csi")
            return
        if 0x40 <= codepoint <= 0x7E:
            self._complete_sequence()
            return
        self._flush_malformed_sequence()
        self._start_text(character)

    def _feed_string(self, character: str) -> None:
        if character in _CANCEL_CONTROL_CHARACTERS:
            # CAN and SUB abort the control string. Its buffered payload stays
            # hidden, while subsequent bytes resume ordinary text parsing.
            self._complete_sequence()
            return
        if character == _C1_ST or (self.bell_terminated and character == "\x07"):
            self._complete_sequence()
            return
        if character == _ESC:
            self.sequence_count += 1
            if self.sequence_count >= self.sequence_limit:
                assert self.string_fallback is not None
                self.string_fallback.feed(character)
                self._flush_malformed_sequence()
            else:
                self.state = "string_escape"
            return
        assert self.string_fallback is not None
        self.string_fallback.feed(character)
        self.sequence_count += 1
        if self.sequence_count >= self.sequence_limit:
            self._flush_malformed_sequence()

    def _feed_string_escape(self, character: str) -> None:
        if character in _CANCEL_CONTROL_CHARACTERS:
            self._complete_sequence()
            return
        if (
            character == "\\"
            or character == _C1_ST
            or (self.bell_terminated and character == "\x07")
        ):
            self._complete_sequence()
            return
        assert self.string_fallback is not None
        self.string_fallback.feed(_ESC)
        if character == _ESC:
            self.sequence_count += 1
            if self.sequence_count >= self.sequence_limit:
                self.string_fallback.feed(character)
                self._flush_malformed_sequence()
            return
        self.string_fallback.feed(character)
        self.sequence_count += 1
        if self.sequence_count >= self.sequence_limit:
            self._flush_malformed_sequence()
        else:
            self.state = "string"

    def feed(self, character: str) -> None:
        if self.state == "text":
            self._start_text(character)
        elif self.state == "escape":
            self._feed_escape(character)
        elif self.state == "escape_sequence":
            self._feed_escape_sequence(character)
        elif self.state == "csi":
            self._feed_csi(character)
        elif self.state == "string":
            self._feed_string(character)
        else:
            self._feed_string_escape(character)

    def _finish_pending(self) -> None:
        if self.state == "string_escape":
            assert self.string_fallback is not None
            self.string_fallback.feed(_ESC)
        if self.state in {"escape_sequence", "csi", "string", "string_escape"}:
            self._flush_malformed_sequence()

    def finish(self) -> tuple[str, str]:
        self._finish_pending()
        compact = "".join(self.output.compact) if self.output.compact is not None else ""
        return "".join(self.output.display), compact


def _terminal_text_projections(
    value: str,
    *,
    include_matching: bool,
) -> _TerminalTextProjections:
    """Scan terminal text once and return bounded display/matching forms."""
    display = _TerminalDisplayNormalizer(collect_compact=include_matching)
    controls_removed: list[str] = []
    introducers = _CollapsedIntroducerProjector() if include_matching else None

    position = 0
    value_length = len(value)
    while position < value_length:
        character = value[position]
        position += 1
        display.feed(character)
        if include_matching:
            codepoint = ord(character)
            if codepoint >= 0x20 and not _is_terminal_matching_control(character):
                controls_removed.append(character)
            assert introducers is not None
            introducers.feed(character)

    normalized, normalized_without_controls = display.finish()
    return _TerminalTextProjections(
        normalized=normalized,
        normalized_without_controls=normalized_without_controls,
        controls_removed="".join(controls_removed),
        matching_tokens=tuple(introducers.finish()) if introducers is not None else (),
    )


def _optional_terminal_character(token: object) -> str | None:
    if isinstance(token, _OptionalTerminalFinal):
        return token.value
    return None


def _mandatory_terminal_text(token: object) -> str:
    if not isinstance(token, str):  # pragma: no cover - projector invariant
        raise TypeError("terminal matching tokens must be text or optional finals")
    return token


def _matches_terminal_text(
    value: str,
    projections: _TerminalTextProjections,
    patterns: RedactionPatternProgram,
    *,
    budget: RedactionPatternWorkBudget,
) -> bool:
    """Match every inert representation under one fail-closed work budget."""
    candidates = tuple(
        dict.fromkeys(
            (
                value,
                projections.normalized,
                projections.controls_removed,
                projections.normalized_without_controls,
            ),
        ),
    )
    if any(patterns.search(candidate, budget=budget) for candidate in candidates):
        return True
    return patterns.search_tokens(
        projections.matching_tokens,
        optional_character=_optional_terminal_character,
        mandatory_text=_mandatory_terminal_text,
        budget=budget,
    )


def _project_redacted_text(
    value: str,
    patterns: RedactionPatternProgram,
    *,
    budget: RedactionPatternWorkBudget | None = None,
) -> tuple[str, bool]:
    """Return one inert projection and whether it must be redacted."""
    if type(value) is not str:
        value = str.__str__(value)
    if budget is None:
        budget = patterns.new_work_budget()
    if _is_inert_terminal_text(value):
        return value, patterns.search(value, budget=budget)
    projections = _terminal_text_projections(value, include_matching=True)
    return projections.normalized, _matches_terminal_text(
        value,
        projections,
        patterns,
        budget=budget,
    )


def normalize_terminal_text(value: str) -> str:
    """Return inert, deterministic text from terminal-formatted diagnostics.

    Complete bounded ANSI CSI and control-string sequences are removed. An
    incomplete or malformed sequence loses only its control introducer, so it
    cannot hide the printable text which follows it. CRLF and lone carriage
    returns become one logical newline. Tabs, newlines, and printable Unicode
    are preserved; the remaining C0/C1 controls and DEL are discarded.
    """
    if type(value) is not str:
        value = str.__str__(value)
    if _is_inert_terminal_text(value):
        return value
    return _terminal_text_projections(value, include_matching=False).normalized


def _safe_type(value: Any) -> str:
    value_type = type(value)
    return f"<{value_type.__module__}.{value_type.__qualname__}>"


def safe_exception_type_name(value: BaseException) -> str:
    """Return a fixed-shape exception label without invoking provider code."""
    try:
        name = type.__getattribute__(type(value), "__name__")
    except Exception:
        return "Exception"
    if (
        type(name) is not str
        or not name
        or len(name) > 128
        or not name.isascii()
        or not name.replace("_", "").isalnum()
    ):
        return "Exception"
    return name


def materialize_exception_message(value: BaseException) -> str:
    """Return an unredacted exception message with a fixed failure fallback.

    This helper makes provider-controlled ``__str__`` implementations inert; it
    is not a display boundary. Callers that expose the returned text must still
    pass the complete diagnostic through ``redact_text``.
    """
    try:
        return str(value)
    except Exception:
        return "exception message unavailable"


def materialize_exception_text(value: BaseException) -> str:
    """Return unredacted bounded type and message text without secondary failure."""
    return f"{safe_exception_type_name(value)}: {materialize_exception_message(value)}"


def _bounded_result_type(value: Any) -> str:
    """Return inert result type metadata without traversing the result value."""
    value_type = type(value)
    try:
        module = type.__getattribute__(value_type, "__module__")
        qualname = type.__getattribute__(value_type, "__qualname__")
    except Exception:
        return "result"
    if (
        type(module) is not str
        or type(qualname) is not str
        or not module
        or not qualname
        or len(module) + 1 + len(qualname) > _RESULT_TYPE_MAX_CHARS
    ):
        return "result"
    rendered = normalize_terminal_text(f"{module}.{qualname}")
    if not rendered or len(rendered.encode("utf-8", errors="replace")) > _RESULT_TYPE_MAX_BYTES:
        return "result"
    return rendered


def redact_value(
    value: Any,
    *,
    patterns: Sequence[str] | str | None = None,
    _compiled: RedactionPatternProgram | None = None,
    _budget: _RedactionValueBudget | None = None,
    _depth: int = 0,
) -> Any:
    """Return a JSON-compatible value with configured sensitive data removed.

    Nested mappings and sequences are traversed.  A mapping key matching a
    pattern redacts its value, while a matching string is replaced in full.
    Cycles and very deep objects are represented by a type marker.
    """
    compiled = _configured_patterns(patterns) if _compiled is None else _compiled
    root = _budget is None
    if _budget is None:
        _budget = _RedactionValueBudget(pattern_work=compiled.new_work_budget())
    try:
        _budget.consume_item()
        if _depth > 20:
            return "<max-depth>"
        if isinstance(value, str):
            _budget.consume_text(value)
            normalized, sensitive = _project_redacted_text(
                value,
                compiled,
                budget=_budget.pattern_work,
            )
            return REDACTED if sensitive else normalized
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, item in value.items():
                _budget.consume_item()
                # Arbitrary mapping-key ``__str__`` implementations are not a
                # safe diagnostic boundary. Preserve exact strings and expose
                # only type metadata for every other key.
                key_text = key if type(key) is str else _safe_type(key)
                _budget.consume_text(key_text)
                normalized_key, sensitive_key = _project_redacted_text(
                    key_text,
                    compiled,
                    budget=_budget.pattern_work,
                )
                normalized_item = (
                    REDACTED
                    if sensitive_key
                    else redact_value(
                        item,
                        _compiled=compiled,
                        _budget=_budget,
                        _depth=_depth + 1,
                    )
                )
                # Distinct raw keys can collapse to the same inert display key.
                # Never let mapping order replace an earlier redaction (or choose
                # arbitrarily between ambiguous values).
                output[normalized_key] = REDACTED if normalized_key in output else normalized_item
            return output
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                redact_value(
                    item,
                    _compiled=compiled,
                    _budget=_budget,
                    _depth=_depth + 1,
                )
                for item in value
            ]
        if isinstance(value, bytes | bytearray | memoryview):
            return _safe_type(value)
        return _safe_type(value)
    except _RedactionValueLimitError:
        if not root:
            raise
        if isinstance(value, Mapping):
            return {"<redacted>": REDACTED}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [REDACTED]
        return REDACTED


def redact_text(value: Any, *, patterns: Sequence[str] | str | None = None) -> str:
    """Redact a message or exception string without exposing arbitrary objects."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, BaseException):
        text = materialize_exception_text(value)
    else:
        text = str(value)
    if len(text) > _REDACTION_TEXT_MAX_CHARS:
        return REDACTED
    compiled = _configured_patterns(patterns)
    normalized, sensitive = _project_redacted_text(text, compiled)
    return REDACTED if sensitive else normalized


def redact_exception(value: BaseException, *, patterns: Sequence[str] | str | None = None) -> str:
    """Return a compact, redacted exception description for structured logs."""
    return redact_text(value, patterns=patterns)


def safe_json_dumps(value: Any, *, patterns: Sequence[str] | str | None = None) -> str:
    """Serialize redacted data without falling back to secret-bearing ``str``."""
    return json.dumps(redact_value(value, patterns=patterns), default=_safe_type)


def result_metadata(
    value: Any,
    *,
    serialized_size_bytes: int | None = None,
) -> dict[str, Any]:
    """Return bounded metadata without traversing or serializing result data.

    Callers may pass a non-negative byte size already computed by the durable
    serialization boundary.  This helper never derives that size by walking
    application-owned values merely for operational output.
    """
    size_bytes = (
        serialized_size_bytes
        if type(serialized_size_bytes) is int and serialized_size_bytes >= 0
        else None
    )
    return {
        "result_type": _bounded_result_type(value),
        "result_size_bytes": size_bytes,
    }


__all__ = [
    "DEFAULT_REDACT_PATTERNS",
    "REDACTED",
    "RedactionPatternError",
    "materialize_exception_message",
    "materialize_exception_text",
    "normalize_terminal_text",
    "redact_exception",
    "redact_text",
    "redact_value",
    "result_metadata",
    "safe_exception_type_name",
    "safe_json_dumps",
]
