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
_SECTION_RE = re.compile(r"^## (?P<name>[A-Za-z0-9][A-Za-z0-9 /&+.-]*)$")
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
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
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
_MIN_SECTION_WORDS = 4


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
    """Remove list markup before checking whether prose is meaningful."""
    return _BULLET_RE.sub("", line.strip()).strip()


def _is_placeholder(line: str) -> bool:
    """Return whether one complete line is development-only placeholder prose."""
    return bool(_PLACEHOLDER_RE.fullmatch(_strip_markup(line)))


def _is_trailer(line: str) -> bool:
    """Return whether a line is commit metadata rather than explanatory prose."""
    stripped = line.strip()
    return bool(_ISSUE_TRAILER_RE.fullmatch(stripped) or _KNOWN_TRAILER_RE.match(stripped))


def _meaningful_lines(lines: Sequence[str]) -> list[str]:
    """Return explanatory section lines, excluding headings and trailers."""
    meaningful: list[str] = []
    in_html_comment = False
    for line in lines:
        stripped = _strip_markup(line)
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue
        if stripped.startswith("<!--"):
            in_html_comment = "-->" not in stripped
            continue
        if not stripped or stripped.startswith(";") or _is_trailer(stripped):
            continue
        meaningful.append(stripped)
    return meaningful


def _word_count(lines: Sequence[str]) -> int:
    return sum(len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", line)) for line in lines)


def _normalized_prose(lines: Sequence[str]) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", " ".join(lines).casefold()))


def _scan_section_headings(body: Sequence[str]) -> tuple[list[tuple[int, str]], list[str]]:
    """Find real Markdown sections while ignoring code and quoted examples."""
    headings: list[tuple[int, str]] = []
    malformed: list[str] = []
    fence_marker: str | None = None
    in_html_comment = False
    for index, line in enumerate(body):
        stripped = line.lstrip()
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue
        if stripped.startswith("<!--"):
            in_html_comment = "-->" not in stripped
            continue
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
        if match := _SECTION_RE.fullmatch(line):
            headings.append((index, match.group("name")))
        elif line.startswith("##"):
            malformed.append(line)
    return headings, malformed


def _validate_sections(lines: Sequence[str], *, label: str, summary: str | None) -> list[str]:
    """Validate the canonical Summary/optional/Validation commit body."""
    errors: list[str] = []
    if len(lines) < 2 or lines[1] != "":
        errors.append(f"{label} must separate its header and body with a blank line.")

    body = list(lines[2:]) if len(lines) >= 2 and lines[1] == "" else list(lines[1:])
    headings, malformed_headings = _scan_section_headings(body)
    for heading in malformed_headings:
        errors.append(f"{label} has malformed section heading {heading!r}; use '## Section name'.")

    if not headings:
        errors.append(
            f"{label} must use structured sections: '## Summary' followed by '## Validation'."
        )
        return errors

    if any(line.strip() for line in body[: headings[0][0]]):
        errors.append(f"{label} must begin its body with the '## Summary' section.")

    names = [name for _, name in headings]
    for name in sorted(set(names)):
        if names.count(name) > 1:
            errors.append(f"{label} contains duplicate '## {name}' sections.")

    summary_positions = [index for index, name in enumerate(names) if name == "Summary"]
    validation_positions = [index for index, name in enumerate(names) if name == "Validation"]
    if not summary_positions:
        errors.append(f"{label} is missing the required '## Summary' section.")
    if not validation_positions:
        errors.append(f"{label} is missing the required '## Validation' section.")
    if summary_positions and summary_positions[0] != 0:
        errors.append(f"{label} must place '## Summary' before optional sections.")
    if validation_positions and validation_positions[-1] != len(headings) - 1:
        errors.append(f"{label} must place '## Validation' after optional sections.")
    if (
        summary_positions
        and validation_positions
        and summary_positions[0] > validation_positions[0]
    ):
        errors.append(f"{label} must place '## Summary' before '## Validation'.")

    for heading_index, (line_index, name) in enumerate(headings):
        end = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else len(body)
        section_lines = body[line_index + 1 : end]
        meaningful = _meaningful_lines(section_lines)
        placeholder_only = bool(meaningful) and all(_is_placeholder(line) for line in meaningful)
        if placeholder_only:
            errors.append(
                f"{label} section '## {name}' contains only placeholder content. "
                "Describe the durable outcome instead."
            )
        descriptive = [] if placeholder_only else meaningful
        if _word_count(descriptive) < _MIN_SECTION_WORDS:
            errors.append(
                f"{label} section '## {name}' must contain meaningful content "
                f"({_MIN_SECTION_WORDS} or more words outside headings and trailers)."
            )
        if name == "Summary" and summary is not None:
            if descriptive and _normalized_prose(descriptive) == _normalized_prose([summary]):
                errors.append(
                    f"{label} section '## Summary' only repeats the header summary. "
                    "Explain the concrete change and why it belongs in history."
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
    errors.extend(_validate_sections(lines, label=label, summary=summary))
    for line_number, line in enumerate(lines, start=1):
        if len(line) > _MAX_LINE_LENGTH:
            errors.append(
                f"{label} line {line_number} exceeds {_MAX_LINE_LENGTH} characters "
                f"({len(line)}). Wrap commit-message lines for narrow terminals."
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
