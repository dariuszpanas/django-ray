"""Check an explicit committed candidate with the pinned YAGA policies."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YAGA = ("uvx", "--from", "yaga-cli==0.2.0", "--with", "typos==1.50.2", "yaga")


def git_output(*arguments: str) -> str:
    """Read Git values without interpreting branch names as shell code."""
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def main() -> int:
    try:
        if git_output("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Commit tracked changes before checking the candidate policies.")
        revision = git_output(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{os.environ.get('YAGA_REVISION', 'HEAD')}^{{commit}}",
        )
        if revision != git_output("rev-parse", "HEAD"):
            raise ValueError("YAGA_REVISION must match the checked-out HEAD and workflow bytes.")
        base = git_output(
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{os.environ.get('YAGA_BASE', 'origin/main')}^{{commit}}",
        )
        branch = os.environ.get("YAGA_BRANCH") or git_output("branch", "--show-current")
        if not branch:
            raise ValueError("Set YAGA_BRANCH explicitly for a detached checkout.")
        output_format = os.environ.get("YAGA_FORMAT", "text")
        commands = (
            ("repo", "check", "--plan", ".yaga/checks/repository.toml", "--revision", revision),
            ("branch", "check", "--policy", ".yaga/branch-policy.toml", "--name", branch),
            (
                "change",
                "check",
                "--policy",
                ".yaga/change-policy.toml",
                "--range",
                f"{base}...{revision}",
            ),
        )
        outcomes = [
            subprocess.run(
                [*YAGA, *command, "--format", output_format], cwd=ROOT, check=False
            ).returncode
            for command in commands
        ]
        return 2 if any(code not in (0, 1) for code in outcomes) else max(outcomes)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Repository policy input failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
