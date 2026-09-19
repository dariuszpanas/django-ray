"""Check versioned and new non-ignored files with one pinned Typos policy."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    paths = sorted({path for path in files if path and (ROOT / os.fsdecode(path)).is_file()})
    if any(b"\n" in path or b"\r" in path for path in paths):
        raise ValueError("spelling input paths cannot contain line breaks")
    return subprocess.run(
        [
            "uvx",
            "--from",
            "typos==1.50.2",
            "typos",
            "--file-list",
            "-",
            "--isolated",
            "--config",
            "_typos.toml",
            "--force-exclude",
        ],
        cwd=ROOT,
        input=b"\n".join(paths) + b"\n",
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
