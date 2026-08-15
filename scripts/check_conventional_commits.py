#!/usr/bin/env python3
"""Validate descriptive Conventional Commits for pull requests and rebase auto-merge."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_HEADER_RE = re.compile(
    r"^(?P<type>[a-z][a-z0-9-]*)(?:\((?P<scope>[^()\r\n]+)\))?(?P<breaking>!)?: (?P<summary>\S.*)$"
)
_ALLOWED_TYPES = frozenset(
    {
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "style",
        "test",
    }
)
_MAX_LINE_LENGTH = 72
_MAX_VALIDATION_CANDIDATE_CHARS = 4096
_MAX_VALIDATION_CANDIDATES = 64
_MARKDOWN_LINK_RE = re.compile(r"\[([^]\r\n]+)\]\(https?://[^)\s]+\)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_TABLE_DELIMITER_CELL_RE = re.compile(r"^:?-{3,}:?$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<name>\S.*?)(?:\s+#+)?$")
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_BLOCK_STRUCTURE_RE = re.compile(
    r"^(?: {4}|\t| {0,3}(?:>|#{1,6}\s|[-+*]\s+|\d+[.)]\s+|`{3,}|~{3,}))"
)
_VALIDATION_LABEL_RE = re.compile(
    r"^(?:tests?|testing|validation|verification|checks?|all checks)\s*[:=-]\s*",
    re.IGNORECASE,
)
_VALIDATION_COMMAND_RE = re.compile(
    r"^(?:"
    r"uv\s+run\s+\S+|"
    r"python\s+-m\s+(?:pytest|mypy)\b|"
    r"pytest\b|"
    r"ruff\s+(?:check|format)\b|"
    r"ty\s+check\b|"
    r"mypy\b|"
    r"make\s+(?:ci|check|test\S*|lint\S*|typecheck|docs\S*|build|format|fix)\b"
    r")"
)
_VALIDATION_RESULT_RE = re.compile(
    r"^(?:all\s+)?(?:tests?|checks?)\s+"
    r"(?:passed|succeeded|completed|are\s+green)\b",
    re.IGNORECASE,
)
_VALIDATION_STATUS_RE = re.compile(
    r"^[A-Za-z0-9_. /+()\-]+:\s*"
    r"(?:pass(?:ed)?|fail(?:ed)?|success(?:ful)?|succeeded|skipped|green|ok|not run)"
    r"(?:[.!]|\s+\([^)]*\)|\s+(?:in|on|with)\b.*)?$",
    re.IGNORECASE,
)
_VALIDATION_SECTION_NAMES = frozenset({"checks", "testing", "tests", "validation", "verification"})
_VALIDATION_OUTCOME_TRAIL_RE = re.compile(
    r"(?:\b(?:clean|completed|failed|green|ok|passed|succeeded|"
    r"successful(?:ly)?)\b|"
    r"\bno (?:errors?|issues?) found\b)"
    r"(?:[.!]|\s+\([^)]*\)|\s+(?:as|in|on|with)\b.*)?$",
    re.IGNORECASE,
)
_VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE = re.compile(
    r"(?:\b(?:clean|completed|fail(?:ed|ure)?|green|ok|pass(?:ed)?|"
    r"succeed(?:ed)?|success(?:ful(?:ly)?)?)\b|"
    r"\bno (?:errors?|issues?) found\b)"
    r"(?:[.!]|\s+\([^)]*\)|\s+(?:as|in|on|with)\b.*)?$",
    re.IGNORECASE,
)
_VALIDATION_COUNT_RESULT_RE = re.compile(
    r"\b(?:exit code \d+|\d+\s+(?:deselected|errors?|failed|passed|skipped|"
    r"warnings?|xfailed|xpassed)|no failures|\d+\s+files? left unchanged)\b",
    re.IGNORECASE,
)
_VALIDATION_EXECUTED_COUNT_RE = re.compile(
    r"\b\d+\s+(?:errors?|failed|passed|xfailed|xpassed)\b",
    re.IGNORECASE,
)
_VALIDATION_MODAL_RESULT_RE = re.compile(
    r"\b(?:can|could|expected to|may|might|must|should|will|would)\s+"
    r"(?:have\s+)?(?:fail(?:ed)?|pass(?:ed)?|succeed(?:ed)?)\b",
    re.IGNORECASE,
)
_VALIDATION_SUBJECT_RE = re.compile(
    r"(?:\b(?:build|coverage|docs?|documentation|gate|lint|matrix|package|"
    r"pytest|ruff|suite|tests?|checks?|typecheck|typing)\b|\bcommit messages\b)"
    r"(?:\s+(?:across|against|for|in|on|under|with)\s+\S+(?:\s+\S+){0,7})?"
    r"(?:\s+(?:are|have been|is|was|were))?$",
    re.IGNORECASE,
)
_VALIDATION_NARRATIVE_RE = re.compile(
    r"\b(?:allegedly|describes?|documents?|explains?|if|meant|means?|perhaps|"
    r"possibly|records?|reportedly|verifies?|when|why)\b|"
    r"\b(?:earlier|historical|previous|prior)(?!-)\b",
    re.IGNORECASE,
)
_GENERIC_VALIDATION_SUBJECT_RE = re.compile(
    r"^(?:(?:all|the)\s+)?(?:tests?|checks?)$",
    re.IGNORECASE,
)
_VALIDATION_NOT_RUN_RE = re.compile(
    r"\b(?:not run|not executed|not applicable)\b(?P<suffix>.*)$",
    re.IGNORECASE,
)
_VALIDATION_SKIPPED_RE = re.compile(r"\bskipped\b(?P<suffix>.*)$", re.IGNORECASE)
_VALIDATION_REASON_RE = re.compile(
    r"^(?:(?:because|as|due to)\s+|[:;\-\N{EM DASH}]\s*|\()"
    r"(?P<reason>\S.*?)(?:\))?[.!]?$",
    re.IGNORECASE,
)
_GENERIC_VALIDATION_REASON_RE = re.compile(
    r"^(?:reasons?|not (?:applicable|needed|required)|"
    r"(?:it|this|checks?|tests?|validation)\s+(?:(?:was|were)\s+)?"
    r"(?:not run|skipped)|skipped)$",
    re.IGNORECASE,
)
_TEMPLATE_TOKEN_RE = re.compile(r"<(?:command|result|describe[^>]*)>", re.IGNORECASE)
_PLACEHOLDER_PREFIX_RE = re.compile(
    r"^(?:(?:wip|todo|tbd)(?:\s*(?::|-|\N{EM DASH})\s*|\s+)"
    r"|(?:work[ -]in[ -]progress|placeholder)\s*(?::|-|\N{EM DASH})\s*)\S",
    re.IGNORECASE,
)
_DEVELOPMENT_ONLY_RE = re.compile(
    r"\b(?:"
    r"(?:address(?:ed|es|ing)?|apply|applied|fix(?:ed|es|ing)?)\s+"
    r"(?:the\s+)?(?:latest\s+)?(?:review(?:er)?\s+)?"
    r"(?:feedback|comments?|changes)"
    r"|fix(?:ed|es|ing)?\s+(?:ci|tests?|lint)(?:\s+(?:failures?|errors?))?"
    r"|(?:ci|tests?|lint)\s+fix(?:es)?"
    r")\b",
    re.IGNORECASE,
)
_DEPENDABOT_METADATA_RECORD_RE = re.compile(r"^- dependency-name: (?P<value>\S+)$")
_DEPENDABOT_METADATA_FIELD_RE = re.compile(r"^  (?P<key>[a-z][a-z-]*): (?P<value>\S+)$")
_DEPENDABOT_REQUIRED_FIELDS = frozenset(
    {"dependency-name", "dependency-version", "dependency-type", "update-type"}
)
_DEPENDABOT_ALLOWED_FIELDS = _DEPENDABOT_REQUIRED_FIELDS | {"dependency-group"}
_DEPENDABOT_METADATA_PREFIXES = tuple(
    ["- dependency-name:"] + [f"  {field}:" for field in sorted(_DEPENDABOT_ALLOWED_FIELDS)]
)
_DEPENDABOT_SIGNOFF = "Signed-off-by: dependabot[bot] <support@github.com>"
_DEPENDABOT_BUMP_RE = re.compile(
    r"^Bumps (?P<dependency>\S+) from (?P<old>\S+) to (?P<new>\S+?)\.?$",
    re.IGNORECASE,
)
_DEPENDABOT_SINGLE_SUMMARY_RE = re.compile(
    r"^bump (?P<dependency>\S+) from (?P<old>\S+) to (?P<new>\S+)$",
    re.IGNORECASE,
)
_DEPENDABOT_GROUP_SUMMARY_RE = re.compile(
    r"^bump the (?P<group>\S+) group"
    r"(?: across \d+ director(?:y|ies))? with (?P<count>\d+) updates$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s+)?")
_ISSUE_TRAILER_RE = re.compile(
    r"^(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\s+#\d+(?:\s*,\s*#\d+)*$",
    re.IGNORECASE,
)
_KNOWN_TRAILER_RE = re.compile(
    r"^(?:BREAKING CHANGE|BREAKING-CHANGE|Co-authored-by|Signed-off-by|Reviewed-by|"
    r"Acked-by|Tested-by|Reported-by|Suggested-by|Helped-by): \S",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(
    r"""
    ^(?:
        <[^>]*>
        | \[[^]]*\]
        | \.{3}
        | …
        | wip
        | work[ -]in[ -]progress
        | todo
        | tbd
        | n/?a
        | none
        | pending
        | placeholder
        | temp(?:orary)?
        | changes?
        | updates?
        | misc(?:ellaneous)?
        | more[ -]changes
        | (?:iteration|round|pass|attempt)(?:\s*\#?\d+)?
        | (?:address(?:ed|es)?|apply|applied)\s+
          (?:the\s+)?(?:latest\s+)?(?:review(?:er)?\s+)?
          (?:feedback|comments?|changes)
        | fix(?:ed|es)?\s+(?:review\s+)?(?:feedback|comments?)
        | fix(?:ed|es)?\s+(?:ci|tests?|lint)
        | (?:ci|tests?|lint)\s+fix(?:es)?
        | cleanup
        | polish
    )[.!]?
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)
_MIN_BODY_WORDS = 8


def validate_header(header: str, *, label: str) -> str | None:
    """Return an actionable error for an invalid Conventional Commit header."""
    match = _HEADER_RE.fullmatch(header.strip())
    if match is None:
        return (
            f"{label} is not a Conventional Commit header: {header!r}. "
            "Expected <type>[optional scope][!]: <imperative summary>."
        )
    if match.group("type") not in _ALLOWED_TYPES:
        allowed = ", ".join(sorted(_ALLOWED_TYPES))
        return f"{label} uses unsupported type {match.group('type')!r}; use one of: {allowed}."
    if _is_placeholder(match.group("summary")):
        return (
            f"{label} uses a development placeholder as its summary: "
            f"{match.group('summary')!r}. Describe the durable change instead."
        )
    return None


def _strip_markup(line: str) -> str:
    """Return visible prose without list markers or unwrappable destinations."""
    stripped = _BULLET_RE.sub("", line.strip()).strip()
    stripped = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), stripped)
    stripped = _URL_RE.sub("", stripped)
    return re.sub(r"[`*_~]+", "", stripped).strip()


def _is_placeholder(line: str) -> bool:
    """Return whether one complete line is development-only placeholder prose."""
    visible = _strip_markup(line)
    return bool(_PLACEHOLDER_RE.fullmatch(visible) or _PLACEHOLDER_PREFIX_RE.match(visible))


def _is_trailer(line: str) -> bool:
    """Return whether a line is commit metadata rather than explanatory prose."""
    stripped = line.strip()
    return bool(_ISSUE_TRAILER_RE.fullmatch(stripped) or _KNOWN_TRAILER_RE.match(stripped))


def _meaningful_lines(lines: Sequence[str]) -> list[str]:
    """Return visible body lines, excluding headings, comments, and trailers."""
    meaningful: list[str] = []
    in_html_comment = False
    setext_lines = _setext_heading_lines(lines)
    for index, line in enumerate(lines):
        stripped = _strip_markup(line)
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue
        if stripped.startswith("<!--"):
            in_html_comment = "-->" not in stripped
            continue
        if (
            not stripped
            or index in setext_lines
            or stripped.startswith(";")
            or _HEADING_RE.fullmatch(stripped)
            or _is_trailer(stripped)
        ):
            continue
        meaningful.append(stripped)
    return meaningful


def _context_lines(lines: Sequence[str]) -> list[str]:
    """Return change-context prose, excluding mechanical validation evidence."""
    context: list[str] = []
    validation_section = False
    in_html_comment = False
    setext_lines = _setext_heading_lines(lines)
    for index, line in enumerate(lines):
        raw = line.strip()
        if in_html_comment:
            if "-->" in raw:
                in_html_comment = False
            continue
        if raw.startswith("<!--"):
            in_html_comment = "-->" not in raw
            continue
        if index in setext_lines:
            if index + 1 < len(lines) and _SETEXT_UNDERLINE_RE.fullmatch(lines[index + 1]):
                validation_section = _strip_markup(raw).casefold() in _VALIDATION_SECTION_NAMES
            continue
        if heading := _HEADING_RE.fullmatch(raw):
            section_name = _strip_markup(heading.group("name")).casefold()
            validation_section = section_name in _VALIDATION_SECTION_NAMES
            continue

        visible = _strip_markup(line)
        if (
            not visible
            or visible.startswith(";")
            or _is_trailer(visible)
            or validation_section
            or _VALIDATION_LABEL_RE.match(visible)
            or _VALIDATION_COMMAND_RE.match(visible)
            or _VALIDATION_RESULT_RE.match(visible)
            or _VALIDATION_STATUS_RE.match(visible)
        ):
            continue
        context.append(visible)
    return context


def _has_explicit_not_run_reason(text: str) -> bool:
    """Require a concrete reason after a validation not-run statement."""
    match = _VALIDATION_NOT_RUN_RE.search(text)
    if match is None or not (
        reason_match := _VALIDATION_REASON_RE.match(match.group("suffix").strip())
    ):
        return False
    reason = _strip_markup(reason_match.group("reason")).strip(" ().")
    return bool(reason) and not (
        _is_placeholder(reason) or _GENERIC_VALIDATION_REASON_RE.fullmatch(reason)
    )


def _validation_blocks(
    lines: Sequence[str],
    *,
    metadata_candidate_bounds: tuple[int, int] | None,
) -> list[tuple[tuple[str, ...], bool, tuple[int, ...]]]:
    """Return top-level logical body blocks and validation-section state."""
    blocks: list[tuple[tuple[str, ...], bool, tuple[int, ...]]] = []
    parts: list[str] = []
    part_indexes: list[int] = []
    parts_are_validation = False
    validation_section = False
    in_html_comment = False
    setext_lines = _setext_heading_lines(lines)
    top_level_indexes = _top_level_line_indexes(lines)
    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1

    def flush() -> None:
        nonlocal part_indexes, parts
        if parts:
            blocks.append((tuple(parts), parts_are_validation, tuple(part_indexes)))
            parts = []
            part_indexes = []

    for index, line in enumerate(lines):
        raw = line.strip()
        if index < body_start:
            continue
        if in_html_comment:
            if "-->" in raw:
                in_html_comment = False
            continue
        if raw.startswith("<!--"):
            flush()
            in_html_comment = "-->" not in raw
            continue
        if index not in top_level_indexes or (
            metadata_candidate_bounds is not None
            and metadata_candidate_bounds[0] <= index <= metadata_candidate_bounds[1]
        ):
            flush()
            continue
        if index in setext_lines:
            flush()
            if index + 1 < len(lines) and _SETEXT_UNDERLINE_RE.fullmatch(lines[index + 1]):
                validation_section = _strip_markup(raw).casefold() in _VALIDATION_SECTION_NAMES
            continue
        if heading := _HEADING_RE.fullmatch(raw):
            flush()
            validation_section = (
                _strip_markup(heading.group("name")).casefold() in _VALIDATION_SECTION_NAMES
            )
            continue

        visible = _strip_markup(line)
        if not visible or visible.startswith(";") or _is_trailer(visible):
            flush()
            continue

        starts_record = bool(
            _BULLET_RE.match(line)
            or _VALIDATION_LABEL_RE.match(visible)
            or _VALIDATION_COMMAND_RE.match(visible)
        )
        if parts and (starts_record or parts_are_validation != validation_section):
            flush()
        if not parts:
            parts_are_validation = validation_section
        parts.append(visible)
        part_indexes.append(index)
    flush()
    return blocks


def _has_concrete_command_result(candidate: str, command_match: re.Match[str] | None) -> bool:
    """Accept an explicit result attached to a recognized command."""
    if command_match is None:
        return False
    result_match = re.search(r":\s+(?P<result>\S.*)$", candidate)
    if result_match is None:
        return False
    result = _strip_markup(result_match.group("result")).strip()
    return bool(
        result
        and not _is_placeholder(result)
        and not _VALIDATION_MODAL_RESULT_RE.search(result)
        and (
            _VALIDATION_COUNT_RESULT_RE.search(result)
            or _VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE.search(result)
        )
    )


def _has_unstructured_validation_subject(subject: str) -> bool:
    """Reject narrative prose while accepting a concise named check or suite."""
    return bool(
        _VALIDATION_SUBJECT_RE.search(subject) and not _VALIDATION_NARRATIVE_RE.search(subject)
    )


def _is_validation_record(candidate: str, *, validation_section: bool) -> bool:
    """Return whether one logical candidate records a concrete validation result."""
    label_match = _VALIDATION_LABEL_RE.match(candidate)
    explicit_context = validation_section or label_match is not None
    if label_match:
        candidate = candidate[label_match.end() :].strip()
    command_match = _VALIDATION_COMMAND_RE.match(candidate)

    if not_run_match := _VALIDATION_NOT_RUN_RE.search(candidate):
        not_run_subject = candidate[: not_run_match.start()].strip(" :=-\N{EM DASH}")
        return bool(
            (
                explicit_context
                or command_match
                or _has_unstructured_validation_subject(not_run_subject)
            )
            and _has_explicit_not_run_reason(candidate)
        )

    if skipped_match := _VALIDATION_SKIPPED_RE.search(candidate):
        if not _VALIDATION_EXECUTED_COUNT_RE.search(candidate):
            skipped_subject = candidate[: skipped_match.start()].strip(" :=-\N{EM DASH}")
            return bool(
                (
                    explicit_context
                    or command_match
                    or _has_unstructured_validation_subject(skipped_subject)
                )
                and _has_explicit_not_run_reason(f"not run {skipped_match.group('suffix')}")
            )

    if command_match:
        return _has_concrete_command_result(candidate, command_match)

    if count_result := _VALIDATION_COUNT_RESULT_RE.search(candidate):
        subject = candidate[: count_result.start()].strip(" :=-\N{EM DASH}")
        normalized_subject = " ".join(subject.casefold().split())
        if subject and _GENERIC_VALIDATION_SUBJECT_RE.fullmatch(normalized_subject):
            return False
        return bool(explicit_context or _has_unstructured_validation_subject(subject))

    outcome = _VALIDATION_OUTCOME_TRAIL_RE.search(candidate)
    if outcome is None and explicit_context:
        outcome = _VALIDATION_EXPLICIT_OUTCOME_TRAIL_RE.search(candidate)
    if outcome is None or _VALIDATION_MODAL_RESULT_RE.search(candidate):
        return False
    subject = candidate[: outcome.start()].strip(" :=-\N{EM DASH}")
    normalized_subject = " ".join(subject.casefold().split())
    if not subject or _GENERIC_VALIDATION_SUBJECT_RE.fullmatch(normalized_subject):
        return False
    return bool(explicit_context or _has_unstructured_validation_subject(subject))


def _classify_validation_block(
    parts: Sequence[str], *, validation_section: bool
) -> tuple[bool, str]:
    """Return validation status and any descriptive prefix in a mixed block."""
    block = " ".join(parts)
    if validation_section or _VALIDATION_LABEL_RE.match(block):
        return _is_validation_record(block, validation_section=validation_section), ""

    # Only concise trailing records can be portable validation evidence. Keep
    # suffix classification bounded so punctuation- or line-dense untrusted
    # commit bodies cannot trigger a quadratic series of regex scans.
    window_start = max(0, len(block) - _MAX_VALIDATION_CANDIDATE_CHARS)
    candidate_starts = {0} if window_start == 0 else set()
    offset = 0
    for part in parts[:-1]:
        offset += len(part) + 1
        if offset >= window_start:
            candidate_starts.add(offset)
    candidate_starts.update(
        window_start + match.end() for match in re.finditer(r"(?<=[.!?])\s+", block[window_start:])
    )
    for start in sorted(candidate_starts, reverse=True)[:_MAX_VALIDATION_CANDIDATES]:
        candidate = block[start:].strip()
        if _is_validation_record(candidate, validation_section=False):
            return True, block[:start].strip()
    return False, block


def _analyze_validation(
    lines: Sequence[str],
    *,
    metadata_candidate_bounds: tuple[int, int] | None,
) -> tuple[bool, set[int], list[str]]:
    """Return evidence state, excluded lines, and mixed-block context."""
    has_evidence = False
    indexes: set[int] = set()
    context_prefixes: list[str] = []
    for parts, validation_section, block_indexes in _validation_blocks(
        lines,
        metadata_candidate_bounds=metadata_candidate_bounds,
    ):
        is_validation, context_prefix = _classify_validation_block(
            parts,
            validation_section=validation_section,
        )
        if not is_validation:
            continue
        has_evidence = True
        indexes.update(block_indexes)
        if context_prefix:
            context_prefixes.append(context_prefix)
    return has_evidence, indexes, context_prefixes


def _word_count(lines: Sequence[str]) -> int:
    return sum(len(re.findall(r"[A-Za-z][A-Za-z0-9_-]*", line)) for line in lines)


def _normalized_prose(lines: Sequence[str]) -> str:
    visible = " ".join(_strip_markup(line) for line in lines)
    return " ".join(re.findall(r"[a-z0-9]+", visible.casefold()))


def _setext_heading_lines(lines: Sequence[str]) -> set[int]:
    """Return text and underline indexes for Setext-style headings."""
    headings: set[int] = set()
    for index in range(1, len(lines)):
        if not _SETEXT_UNDERLINE_RE.fullmatch(lines[index]):
            continue
        heading = lines[index - 1].strip()
        if heading and not heading.startswith(("- ", "* ", "+ ", ">", "    ")):
            headings.update({index - 1, index})
    return headings


def _without_summary_repetitions(
    context: Sequence[str], summary: str | None
) -> tuple[list[str], bool]:
    """Remove complete header-summary token sequences from joined body prose."""
    context_tokens = _normalized_prose(context).split()
    summary_tokens = _normalized_prose([summary]).split() if summary is not None else []
    if not summary_tokens:
        return context_tokens, False

    remaining: list[str] = []
    repeated = False
    index = 0
    while index < len(context_tokens):
        if context_tokens[index : index + len(summary_tokens)] == summary_tokens:
            repeated = True
            index += len(summary_tokens)
            continue
        remaining.append(context_tokens[index])
        index += 1
    return remaining, repeated


def _generated_metadata_candidate_bounds(lines: Sequence[str]) -> tuple[int, int] | None:
    """Return syntactic bounds for a generated dependency metadata block."""
    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1
    top_level_indexes = _top_level_line_indexes(lines)
    metadata_header: int | None = None
    for index in range(body_start, len(lines)):
        if index not in top_level_indexes or lines[index].strip() != "updated-dependencies:":
            continue
        if metadata_header is not None:
            return None
        metadata_header = index
    if metadata_header is None:
        return None

    metadata_start = metadata_header - 1
    if (
        metadata_start < body_start
        or metadata_start not in top_level_indexes
        or lines[metadata_start].strip() != "---"
    ):
        return None

    metadata_end: int | None = None
    for index in range(metadata_header + 1, len(lines)):
        if index not in top_level_indexes or lines[index].strip() != "...":
            continue
        if metadata_end is not None:
            return None
        metadata_end = index
    if metadata_end is None:
        return None

    return metadata_start, metadata_end


def _generated_metadata_records(
    lines: Sequence[str], bounds: tuple[int, int] | None
) -> list[dict[str, str]] | None:
    """Parse complete, recognized records from a candidate metadata block."""
    if bounds is None:
        return None
    metadata_start, metadata_end = bounds
    metadata_lines = list(lines[metadata_start + 2 : metadata_end])
    if not metadata_lines:
        return None

    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in metadata_lines:
        if record_match := _DEPENDABOT_METADATA_RECORD_RE.fullmatch(line):
            if current is not None:
                records.append(current)
            current = {"dependency-name": record_match.group("value")}
            continue
        if current is None or not (field_match := _DEPENDABOT_METADATA_FIELD_RE.fullmatch(line)):
            return None
        key = field_match.group("key")
        if key not in _DEPENDABOT_ALLOWED_FIELDS or key in current:
            return None
        current[key] = field_match.group("value")
    if current is not None:
        records.append(current)
    if not records or any(not _DEPENDABOT_REQUIRED_FIELDS <= record.keys() for record in records):
        return None

    return records


def _generated_metadata_bounds(lines: Sequence[str]) -> tuple[int, int] | None:
    """Return bounds only for a complete, recognized Dependabot metadata block."""
    bounds = _generated_metadata_candidate_bounds(lines)
    if _generated_metadata_records(lines, bounds) is None:
        return None
    assert bounds is not None
    trailers = [line.strip() for line in lines[bounds[1] + 1 :] if line.strip()]
    return bounds if trailers == [_DEPENDABOT_SIGNOFF] else None


def _top_level_line_indexes(lines: Sequence[str]) -> set[int]:
    """Return lines outside fenced, quoted, and indented code examples."""
    indexes: set[int] = set()
    fence_marker: str | None = None
    for index, line in enumerate(lines):
        if fence_match := _FENCE_RE.match(line):
            marker = fence_match.group("marker")[0]
            if fence_marker is None:
                fence_marker = marker
            elif marker == fence_marker:
                fence_marker = None
            continue
        if fence_marker is not None or line.startswith(("    ", "\t")):
            continue
        if re.match(r"^ {0,3}>", line):
            continue
        indexes.add(index)
    return indexes


def _contains_generated_metadata_shape(lines: Sequence[str]) -> bool:
    """Return whether commit text contains dependency metadata-shaped lines."""
    top_level_indexes = _top_level_line_indexes(lines)
    return any(
        lines[index].strip() == "updated-dependencies:"
        or lines[index].startswith(_DEPENDABOT_METADATA_PREFIXES)
        for index in top_level_indexes
    )


def _split_markdown_table_row(line: str) -> list[str] | None:
    """Split a GFM table row on unescaped pipes, including one-cell rows."""
    stripped = line.strip()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    saw_pipe = False
    for character in stripped:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
            saw_pipe = True
        else:
            current.append(character)
    cells.append("".join(current).strip())
    if not saw_pipe:
        return None
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells or None


def _markdown_table_lines(lines: Sequence[str]) -> set[int]:
    """Return line indexes belonging to complete GFM-style tables."""
    table_lines: set[int] = set()
    previous_cells: list[str] | None = None
    in_table = False
    for index, line in enumerate(lines):
        cells = _split_markdown_table_row(line)
        starts_table = bool(
            index > 0
            and previous_cells is not None
            and cells is not None
            and len(previous_cells) == len(cells)
            and all(_MARKDOWN_TABLE_DELIMITER_CELL_RE.fullmatch(cell) for cell in cells)
        )
        if starts_table:
            table_lines.update({index - 1, index})
            in_table = True
        elif in_table:
            if _BLOCK_STRUCTURE_RE.match(line) or cells is None:
                in_table = False
            else:
                table_lines.add(index)
        previous_cells = cells
    return table_lines


def _has_generated_dependency_context(
    context: Sequence[str], records: Sequence[dict[str, str]] | None
) -> bool:
    """Recognize Dependabot's complete short-name prose and links."""
    if records is None or len(records) != 1 or not context:
        return False
    bump = _DEPENDABOT_BUMP_RE.fullmatch(context[0])
    if bump is None:
        return False
    record = records[0]
    if (
        _normalized_prose([bump.group("dependency")])
        != _normalized_prose([record["dependency-name"]])
        or bump.group("new").casefold() != record["dependency-version"].casefold()
    ):
        return False
    labels = {line.casefold().rstrip(":") for line in context[1:]}
    return "commits" in labels and ("release notes" in labels or "changelog" in labels)


def _has_generated_dependency_header(header: str, records: Sequence[dict[str, str]] | None) -> bool:
    """Recognize a canonical Dependabot header backed by validated metadata."""
    if records is None or not records:
        return False
    header_match = _HEADER_RE.fullmatch(header.strip())
    if header_match is None:
        return False
    summary = header_match.group("summary")

    if single_match := _DEPENDABOT_SINGLE_SUMMARY_RE.fullmatch(summary):
        if len(records) != 1:
            return False
        record = records[0]
        return (
            _normalized_prose([single_match.group("dependency")])
            == _normalized_prose([record["dependency-name"]])
            and single_match.group("new").casefold() == record["dependency-version"].casefold()
        )

    if group_match := _DEPENDABOT_GROUP_SUMMARY_RE.fullmatch(summary):
        group = group_match.group("group").casefold()
        count = group_match.group("count").lstrip("0") or "0"
        return count == str(len(records)) and all(
            record.get("dependency-group", "").casefold() == group for record in records
        )
    return False


def _validate_descriptive_body(
    lines: Sequence[str],
    *,
    label: str,
    summary: str | None,
    metadata_candidate_bounds: tuple[int, int] | None,
    metadata_bounds: tuple[int, int] | None,
    metadata_records: Sequence[dict[str, str]] | None,
    validation_indexes: set[int],
    validation_context_prefixes: Sequence[str],
) -> list[str]:
    """Require useful historical context without prescribing Markdown sections."""
    errors: list[str] = []
    if len(lines) < 2 or lines[1] != "":
        errors.append(f"{label} must separate its header and body with a blank line.")

    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1
    full_body = list(lines[body_start:])
    body = [
        "" if index in validation_indexes else line
        for index, line in enumerate(lines[body_start:], start=body_start)
        if metadata_candidate_bounds is None
        or not metadata_candidate_bounds[0] <= index <= metadata_candidate_bounds[1]
    ]
    placeholder_body = [
        line
        for index, line in enumerate(full_body, start=body_start)
        if metadata_candidate_bounds is None
        or index
        not in {
            metadata_candidate_bounds[0],
            metadata_candidate_bounds[0] + 1,
            metadata_candidate_bounds[1],
        }
    ]
    placeholder_indexes = _top_level_line_indexes(placeholder_body)
    meaningful = _meaningful_lines(
        [
            line if index in placeholder_indexes else ""
            for index, line in enumerate(placeholder_body)
        ]
    )
    placeholders = [
        line for line in meaningful if _is_placeholder(line) or _TEMPLATE_TOKEN_RE.search(line)
    ]
    context = [*_context_lines(body), *validation_context_prefixes]
    joined_context = " ".join(context)
    if joined_context and _is_placeholder(joined_context):
        placeholders.append(joined_context)
    if placeholders:
        errors.append(
            f"{label} body contains placeholder content {placeholders[0]!r}. "
            "Describe the durable change and its context instead."
        )

    scored_context = [_DEVELOPMENT_ONLY_RE.sub(" ", " ".join(context))]
    descriptive_tokens, repeated_summary = _without_summary_repetitions(scored_context, summary)
    descriptive_word_count = _word_count([" ".join(descriptive_tokens)])
    if repeated_summary and descriptive_word_count < _MIN_BODY_WORDS:
        errors.append(
            f"{label} body repeats the header summary without enough additional context. "
            "Explain enough context to understand the change without reading its diff."
        )
    elif descriptive_word_count < _MIN_BODY_WORDS and not (
        metadata_bounds is not None and _has_generated_dependency_context(context, metadata_records)
    ):
        errors.append(
            f"{label} body must contain meaningful context "
            f"({_MIN_BODY_WORDS} or more prose words outside headings, validation, "
            "metadata, and trailers)."
        )
    return errors


def _line_length_errors(
    lines: Sequence[str],
    *,
    label: str,
    metadata_bounds: tuple[int, int] | None = None,
    generated_header: bool = False,
) -> list[str]:
    """Validate wrappable prose while tolerating mechanical Markdown structures."""
    errors: list[str] = []
    table_lines = _markdown_table_lines(lines)
    for index, line in enumerate(lines):
        line_number = index + 1
        if index == 0 and generated_header:
            continue
        if metadata_bounds is not None and metadata_bounds[0] <= index <= metadata_bounds[1]:
            continue

        if index in table_lines or _is_trailer(line):
            continue

        measured = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), line)
        measured = _URL_RE.sub("", measured)
        if len(measured) > _MAX_LINE_LENGTH:
            errors.append(
                f"{label} line {line_number} exceeds {_MAX_LINE_LENGTH} characters "
                f"({len(measured)} visible characters). "
                "Wrap commit-message prose for narrow terminals."
            )
    return errors


def validate_message(message: str, *, label: str) -> list[str]:
    """Validate a full commit message, including its explanatory body."""
    lines = message.rstrip().splitlines()
    if not lines:
        return [f"{label} is empty; provide a Conventional Commit header and descriptive body."]

    errors: list[str] = []
    header_match = _HEADER_RE.fullmatch(lines[0].strip())
    if error := validate_header(lines[0], label=label):
        errors.append(error)
    summary = header_match.group("summary") if header_match is not None else None
    metadata_candidate_bounds = _generated_metadata_candidate_bounds(lines)
    metadata_bounds = _generated_metadata_bounds(lines)
    metadata_records = _generated_metadata_records(lines, metadata_bounds)
    validation_evidence, validation_indexes, validation_context_prefixes = _analyze_validation(
        lines,
        metadata_candidate_bounds=metadata_candidate_bounds,
    )
    generated_dependency_message = metadata_bounds is not None and _has_generated_dependency_header(
        lines[0], metadata_records
    )
    if (metadata_candidate_bounds is not None and metadata_bounds is None) or (
        metadata_candidate_bounds is None and _contains_generated_metadata_shape(lines)
    ):
        errors.append(
            f"{label} contains an unrecognized generated dependency metadata block. "
            "Use complete Dependabot fields with single-token values and its signed-off trailer."
        )
    errors.extend(
        _validate_descriptive_body(
            lines,
            label=label,
            summary=summary,
            metadata_candidate_bounds=metadata_candidate_bounds,
            metadata_bounds=metadata_bounds,
            metadata_records=metadata_records,
            validation_indexes=validation_indexes,
            validation_context_prefixes=validation_context_prefixes,
        )
    )
    if not generated_dependency_message and not validation_evidence:
        errors.append(
            f"{label} must record validation evidence or a specific reason validation "
            "was not run. Include a result such as `uv run make ci: passed` or "
            "`Validation: not run because this changes documentation only`."
        )
    errors.extend(
        _line_length_errors(
            lines,
            label=label,
            metadata_bounds=metadata_bounds,
            generated_header=generated_dependency_message,
        )
    )
    return errors


def _messages_from_git(commit_range: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", "--format=%x1e%B", "--no-merges", commit_range],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "git log failed"
        raise RuntimeError(f"Unable to inspect commit range {commit_range!r}: {detail}") from exc
    return [message.rstrip() for message in result.stdout.split("\x1e") if message.strip()]


def _messages_from_file(path: str) -> list[str]:
    """Read one full commit message from a file."""
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to read commit message file {path!r}: {exc}") from exc
    message = contents.rstrip()
    return [message] if message.strip() else []


def _messages_from_json_file(path: str) -> list[str]:
    """Read a JSON array of full commit messages for newline-safe transport."""
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read commit message file {path!r}: {exc}") from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise RuntimeError(f"Commit message file {path!r} must contain a JSON string array")
    return [value for value in values if value.strip()]


def validate(
    *,
    title: str | None,
    commits: Sequence[str],
    commit_range: str | None,
    commit_file: str | None = None,
    commit_json_file: str | None = None,
    require_commits: bool = True,
) -> list[str]:
    """Validate a PR title and/or commit headers and return all errors."""
    messages = list(commits)
    if commit_range is not None:
        messages.extend(_messages_from_git(commit_range))
    if commit_file is not None:
        messages.extend(_messages_from_file(commit_file))
    if commit_json_file is not None:
        messages.extend(_messages_from_json_file(commit_json_file))

    errors: list[str] = []
    if title is not None:
        if error := validate_header(title, label="PR title"):
            errors.append(error)
    if require_commits and not messages:
        errors.append("No commit headers were found to validate.")
    for index, message in enumerate(messages, start=1):
        errors.extend(validate_message(message, label=f"Commit {index}"))
    return errors


def _is_trusted_dependabot_pr(
    *,
    author_login: str | None,
    head_ref: str | None,
    head_repository: str | None,
    repository: str | None,
) -> bool:
    """Limit title-only validation to GitHub's same-repository Dependabot PRs."""
    return (
        author_login == "dependabot[bot]"
        and bool(repository)
        and head_repository == repository
        and head_ref is not None
        and head_ref.startswith("dependabot/")
    )


def validate_pull_request(
    *,
    title: str | None,
    commits: Sequence[str],
    commit_range: str | None,
    commit_file: str | None = None,
    commit_json_file: str | None = None,
    author_login: str | None = None,
    head_ref: str | None = None,
    head_repository: str | None = None,
    repository: str | None = None,
) -> list[str]:
    """Apply the hosted PR policy while keeping every untrusted path strict."""
    if _is_trusted_dependabot_pr(
        author_login=author_login,
        head_ref=head_ref,
        head_repository=head_repository,
        repository=repository,
    ):
        if title is None:
            return ["Trusted Dependabot pull request validation requires a PR title."]
        return validate(
            title=title,
            commits=[],
            commit_range=None,
            require_commits=False,
        )

    return validate(
        title=title,
        commits=commits,
        commit_range=commit_range,
        commit_file=commit_file,
        commit_json_file=commit_json_file,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", help="Pull request title to validate.")
    parser.add_argument(
        "--commit",
        action="append",
        default=[],
        help="Full commit message to validate; may be supplied multiple times.",
    )
    parser.add_argument(
        "--range",
        dest="commit_range",
        help="Git revision range whose non-merge commit messages should be validated.",
    )
    parser.add_argument(
        "--commit-file",
        help="File containing one full commit message.",
    )
    parser.add_argument(
        "--commit-json-file",
        help="JSON file containing an array of full commit messages.",
    )
    parser.add_argument("--pr-author-login", help="GitHub pull request author login.")
    parser.add_argument("--pr-head-ref", help="GitHub pull request head branch.")
    parser.add_argument("--pr-head-repository", help="GitHub pull request head repository.")
    parser.add_argument("--repository", help="GitHub Actions repository name.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    pr_context = (
        args.pr_author_login,
        args.pr_head_ref,
        args.pr_head_repository,
        args.repository,
    )
    if any(value is not None for value in pr_context) and not all(
        value is not None for value in pr_context
    ):
        print(
            "::error::Pull request context requires --pr-author-login, --pr-head-ref, "
            "--pr-head-repository, and --repository together.",
            file=sys.stderr,
        )
        return 1
    try:
        errors = validate_pull_request(
            title=args.title,
            commits=args.commit,
            commit_range=args.commit_range,
            commit_file=args.commit_file,
            commit_json_file=args.commit_json_file,
            author_login=args.pr_author_login,
            head_ref=args.pr_head_ref,
            head_repository=args.pr_head_repository,
            repository=args.repository,
        )
    except RuntimeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"::error::{error}", file=sys.stderr)
        return 1
    print("Conventional Commit validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
