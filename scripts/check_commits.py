"""Run the same pinned commit policy from local Make targets and hooks."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YAGA_COMMAND = (
    "uvx",
    "--from",
    "yaga-cli==0.2.0",
    "--with",
    "typos==1.50.2",
    "yaga",
    "commit",
    "check",
    "--config",
    str(ROOT / ".yaga.toml"),
)


def git_output(*arguments: str) -> str:
    """Resolve range input through Git without shell interpretation."""
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("file", "edit"):
        file_parser = modes.add_parser(mode)
        file_parser.add_argument("path", type=Path)
    modes.add_parser("title")
    range_parser = modes.add_parser("range")
    range_parser.add_argument("--base", default="origin/main")
    range_parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(arguments)

    if args.mode == "title":
        title = os.environ.get("PR_TITLE", "")
        if not title.strip():
            parser.error("PR_TITLE is required.")
        selected = ["--title", title]
    elif args.mode in ("file", "edit"):
        selected = [f"--{args.mode}", str(args.path.resolve())]
    else:
        base = git_output("rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}")
        head = git_output("rev-parse", "--verify", "--end-of-options", f"{args.head}^{{commit}}")
        revision_range = f"{base}..{head}"
        # Preserve make commit-check's successful no-op on an empty range.
        # Let YAGA own merge policy and every nonempty range's history checks.
        if not git_output("rev-list", "--max-count=1", revision_range):
            print("No commits selected.")
            return 0
        selected = ["--range", revision_range]
    return subprocess.run([*YAGA_COMMAND, *selected], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
