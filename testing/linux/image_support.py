"""Prepare one disposable source directory for the existing Linux stage command."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAX_BUILD_CACHE_BYTES = 128 * 1024 * 1024
SOURCE = Path("/opt/test-source")
INPUTS = Path("/opt/test-inputs")
WORKSPACE = Path("/workspace")
CACHE = Path("/opt/test-build-cache")
WRITABLE_CACHE = Path("/tmp/linux-test-build-cache")


def tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def prepare_directory(source: Path, destination: Path) -> None:
    """A new writable directory cannot reuse another invocation's source or evidence."""
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError(f"destination must be an empty real directory: {destination}")
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)


def initialize_runtime_repository(workspace: Path) -> None:
    """Create a source-only Git index for repository-sensitive assertions."""
    subprocess.run(["git", "init", "--quiet"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "linux-test-runner"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "linux-test-runner@localhost"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["git", "add", "--all"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--quiet",
            "-m",
            "sealed candidate archive",
        ],
        cwd=workspace,
        check=True,
    )


def record_environment(output: Path) -> None:
    """Retain build inputs and observed tools; this receipt claims no test execution."""
    if output.exists():
        raise ValueError("image environment receipt must not already exist")
    cache_bytes = tree_bytes(CACHE)
    if cache_bytes > MAX_BUILD_CACHE_BYTES:
        raise ValueError("prepared build cache exceeds its 128 MiB scratch allowance")
    versions = {}
    for executable, arguments in (
        ("python", ["--version"]),
        ("uv", ["--version"]),
        ("node", ["--version"]),
        ("npm", ["--version"]),
        ("git", ["--version"]),
        ("make", ["--version"]),
        ("kubectl", ["version", "--client", "-o", "json"]),
    ):
        versions[executable] = subprocess.check_output(
            [executable, *arguments], text=True, timeout=30
        ).strip()
    files = ("source.tar", "source-manifest.json", "build-constraints.txt")
    receipt = {
        "schema_version": 1,
        "execution": "not_run",
        "images": {name: os.environ[name] for name in ("PYTHON_IMAGE", "NODE_IMAGE", "UV_IMAGE")},
        "debian_snapshot": os.environ["DEBIAN_SNAPSHOT"],
        "inputs": {
            name: hashlib.sha256((INPUTS / name).read_bytes()).hexdigest() for name in files
        },
        "tools": versions,
        "build_cache_bytes": cache_bytes,
    }
    output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def execute(arguments: list[str]) -> None:
    if not arguments or arguments[0] not in {"catalogue", "run", "aggregate"}:
        raise ValueError("select catalogue, run, or aggregate")
    if "--source-manifest" in arguments or any(
        value.startswith("--source-manifest=") for value in arguments
    ):
        raise ValueError("the image supplies its own sealed source manifest")
    prepare_directory(SOURCE, WORKSPACE)
    initialize_runtime_repository(WORKSPACE)
    prepare_directory(CACHE, WRITABLE_CACHE)
    os.environ["UV_CACHE_DIR"] = str(WRITABLE_CACHE)
    os.chdir(WORKSPACE)
    command = [
        sys.executable,
        str(WORKSPACE / "scripts/linux_test_plan.py"),
        *arguments,
        "--source-manifest",
        str(INPUTS / "source-manifest.json"),
    ]
    os.execv(sys.executable, command)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("record").add_argument("--output", type=Path, required=True)
    commands.add_parser("execute").add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "record":
        record_environment(args.output)
    else:
        execute(args.arguments)


if __name__ == "__main__":
    main()
