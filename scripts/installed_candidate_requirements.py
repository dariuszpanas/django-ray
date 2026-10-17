"""Export the resolved candidate dependencies for an independent advisory audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version


def requirements(inventory: object, *, candidate_version: str) -> str:
    """Exclude only the exact unpublished project; audit every installed dependency."""
    if not isinstance(inventory, list) or not 1 <= len(inventory) <= 512:
        raise ValueError("expected bounded installed-distribution inventory")
    packages: dict[str, str] = {}
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            raise ValueError("invalid installed-distribution record")
        if not isinstance(item["name"], str) or not isinstance(item["version"], str):
            raise ValueError("invalid installed-distribution identity")
        name = canonicalize_name(item["name"], validate=True)
        version = str(Version(item["version"]))
        if name in packages:
            raise ValueError("duplicate installed-distribution identity")
        packages[name] = version
    if packages.pop("django-ray", None) != str(Version(candidate_version)):
        raise ValueError("installed candidate version does not match")
    if not packages:
        raise ValueError("installed candidate has no dependencies to audit")
    return "".join(f"{name}=={version}\n" for name, version in sorted(packages.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.inventory.stat().st_size > 1024 * 1024:
        raise ValueError("installed-distribution inventory exceeds byte budget")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    args.output.write_text(
        requirements(inventory, candidate_version=args.version), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
