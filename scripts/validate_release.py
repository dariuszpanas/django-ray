"""Validate that a release ref matches every package version source."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

_VERSION_RE = re.compile(r"^v?(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")


def _read_pyproject_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _read_module_version(root: Path) -> str:
    source = (root / "src" / "django_ray" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("src/django_ray/__init__.py does not define __version__")


def normalize_version(value: str) -> str:
    """Return a tag/input version without its optional leading ``v``."""
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"release version must look like vX.Y.Z (received {value!r})")
    return match.group("version")


def validate_release_version(root: Path, requested: str) -> str:
    """Validate a tag/manual input against pyproject and package versions."""
    requested_version = normalize_version(requested)
    pyproject_version = _read_pyproject_version(root)
    module_version = _read_module_version(root)
    versions = {
        "release ref": requested_version,
        "pyproject.toml": pyproject_version,
        "django_ray.__version__": module_version,
    }
    if len(set(versions.values())) != 1:
        details = ", ".join(f"{name}={version}" for name, version in versions.items())
        raise ValueError(f"release versions do not agree: {details}")
    return requested_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="tag or manual release version, such as v0.3.0")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        print(validate_release_version(args.root, args.version))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Release validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
