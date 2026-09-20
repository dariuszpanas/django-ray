"""Exercise consumer commit policy through the installed YAGA CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from scripts.check_commits import ROOT, YAGA_COMMAND

BODY = (
    "Keep lease ownership attached to the active worker generation so stale\n"
    "workers cannot publish a late result after replacement. This preserves\n"
    "deterministic recovery across all supported execution backends."
)
VALIDATION = "Validation: focused commit policy tests passed."


def message(header: str, body: str = BODY, footer: str | None = VALIDATION) -> str:
    sections = [header, body]
    if footer is not None:
        sections.append(footer)
    return "\n\n".join(sections) + "\n"


def cases() -> list[tuple[str, str, str, int]]:
    """Retain the structural fixtures previously exercised through commitlint."""
    header = "fix(worker): preserve lease ownership"
    fixtures = [
        ("descriptive", "message", message(header), 0),
        ("header-only", "message", header, 1),
        ("short-body", "message", message(header, "Keep active lease ownership stable."), 1),
        ("missing-validation", "message", message(header, footer=None), 1),
        ("empty-validation", "message", message(header, footer="Validation:"), 1),
        (
            "long-prose",
            "message",
            message(
                header,
                BODY
                + "\nThis deliberately overlong prose line exceeds the repository's narrow history limit.",
            ),
            1,
        ),
        (
            "long-url",
            "message",
            message(
                header,
                BODY
                + "\nhttps://example.com/this/is/a/deliberately/long/reference/path/that/exceeds/seventy-two/characters",
            ),
            0,
        ),
        ("breaking-marker", "message", message("feat(worker)!: retire legacy lease ownership"), 0),
        ("fixup", "message", message("fixup! " + header), 1),
        ("revert", "message", message('Revert "' + header + '"'), 1),
        ("valid-title", "title", header, 0),
        ("invalid-title", "title", "Preserve lease ownership", 1),
    ]
    for separator in ("BREAKING CHANGE", "BREAKING-CHANGE"):
        fixtures.append(
            (
                separator,
                "message",
                message(
                    "feat(worker): replace the legacy ownership protocol",
                    footer=separator
                    + ": workers must send the current ownership generation.\n"
                    + VALIDATION,
                ),
                0,
            )
        )
    return fixtures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-python",
        type=Path,
        help="Qualify an explicitly installed candidate instead of the pinned release.",
    )
    args = parser.parse_args()
    command = list(YAGA_COMMAND)
    if args.candidate_python is not None:
        command = [
            str(args.candidate_python.resolve()),
            "-I",
            "-m",
            "yaga",
            "commit",
            "check",
            "--config",
            str(ROOT / ".yaga.toml"),
        ]
    failures = 0
    for name, mode, value, expected in cases():
        result = subprocess.run(
            [*command, "--" + mode, value],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )
        passed = result.returncode == expected
        print(
            f"{name}: {'PASS' if passed else 'FAIL'} (exit {result.returncode}, expected {expected})"
        )
        if not passed:
            failures += 1
            print(result.stdout + result.stderr, file=sys.stderr)
    # Exercise the public Make boundary as well as direct CLI semantics.
    # Candidate-only verification leaves the current Make implementation alone.
    if args.candidate_python is None:
        for title, expected in (
            ("fix(worker): preserve lease ownership", 0),
            ("Preserve lease ownership", 1),
            (None, 1),
        ):
            env = os.environ.copy()
            env.pop("PR_TITLE", None)
            if title is not None:
                env["PR_TITLE"] = title
            result = subprocess.run(
                ["make", "commit-title-check"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )
            passed = (result.returncode == 0) == (expected == 0)
            if title is None:
                passed = passed and "PR_TITLE is required" in result.stdout + result.stderr
            print(f"Make title {title!r}: {'PASS' if passed else 'FAIL'}")
            if not passed:
                failures += 1
                print(result.stdout + result.stderr, file=sys.stderr)
    return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
