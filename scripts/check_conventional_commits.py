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
                validation_section = _strip_markup(raw).casefold() in {
                    "checks",
                    "testing",
                    "tests",
                    "validation",
                    "verification",
                }
            continue
        if heading := _HEADING_RE.fullmatch(raw):
            section_name = _strip_markup(heading.group("name")).casefold()
            validation_section = section_name in {
                "checks",
                "testing",
                "tests",
                "validation",
                "verification",
            }
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
    metadata_headers = [
        index
        for index in range(body_start, len(lines))
        if index in top_level_indexes and lines[index].strip() == "updated-dependencies:"
    ]
    if len(metadata_headers) != 1:
        return None

    metadata_header = metadata_headers[0]
    metadata_start = metadata_header - 1
    if (
        metadata_start < body_start
        or metadata_start not in top_level_indexes
        or lines[metadata_start].strip() != "---"
    ):
        return None

    metadata_ends = [
        index
        for index in range(metadata_header + 1, len(lines))
        if index in top_level_indexes and lines[index].strip() == "..."
    ]
    if len(metadata_ends) != 1:
        return None
    metadata_end = metadata_ends[0]

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
    for index in range(1, len(lines)):
        header_cells = _split_markdown_table_row(lines[index - 1])
        delimiter_cells = _split_markdown_table_row(lines[index])
        if (
            header_cells is None
            or delimiter_cells is None
            or len(header_cells) != len(delimiter_cells)
            or not all(
                _MARKDOWN_TABLE_DELIMITER_CELL_RE.fullmatch(cell) for cell in delimiter_cells
            )
        ):
            continue
        table_lines.update({index - 1, index})
        row_index = index + 1
        while row_index < len(lines):
            if _BLOCK_STRUCTURE_RE.match(lines[row_index]):
                break
            row_cells = _split_markdown_table_row(lines[row_index])
            if row_cells is None:
                break
            table_lines.add(row_index)
            row_index += 1
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
        return len(records) == int(group_match.group("count")) and all(
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
) -> list[str]:
    """Require useful historical context without prescribing Markdown sections."""
    errors: list[str] = []
    if len(lines) < 2 or lines[1] != "":
        errors.append(f"{label} must separate its header and body with a blank line.")

    body_start = 2 if len(lines) >= 2 and lines[1] == "" else 1
    full_body = list(lines[body_start:])
    body = [
        line
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
    context = _context_lines(body)
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
    lines = message.strip().splitlines()
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
        )
    )
    errors.extend(
        _line_length_errors(
            lines,
            label=label,
            metadata_bounds=metadata_bounds,
            generated_header=_has_generated_dependency_header(lines[0], metadata_records),
        )
    )
    return errors


def _messages_from_git(commit_range: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", "--format=%B%x1e", "--no-merges", commit_range],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "git log failed"
        raise RuntimeError(f"Unable to inspect commit range {commit_range!r}: {detail}") from exc
    return [message.strip() for message in result.stdout.split("\x1e") if message.strip()]


def _messages_from_file(path: str) -> list[str]:
    """Read one full commit message from a file."""
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to read commit message file {path!r}: {exc}") from exc
    message = contents.strip()
    return [message] if message else []


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
    if not messages:
        errors.append("No commit headers were found to validate.")
    for index, message in enumerate(messages, start=1):
        errors.extend(validate_message(message, label=f"Commit {index}"))
    return errors


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        errors = validate(
            title=args.title,
            commits=args.commit,
            commit_range=args.commit_range,
            commit_file=args.commit_file,
            commit_json_file=args.commit_json_file,
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
