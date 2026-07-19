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
    return None


def validate_message(message: str, *, label: str) -> list[str]:
    """Validate a full commit message, including its explanatory body."""
    lines = message.strip().splitlines()
    if not lines:
        return [f"{label} is empty; provide a Conventional Commit header and descriptive body."]

    errors: list[str] = []
    if error := validate_header(lines[0], label=label):
        errors.append(error)
    if not "\n".join(lines[1:]).strip():
        errors.append(
            f"{label} must include a descriptive body after the Conventional Commit header."
        )
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
