"""Fixed copied-source identity; never execute downloaded evidence or manifests."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import stat
import sys
from pathlib import Path

from scripts.runtime_env_growth.evidence import GrowthError, decode, digest, encode

ROOT = Path(__file__).resolve().parents[2]
FIXED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "tests/__init__.py",
    "tests/real_ray_ownership.py",
)


def source_identity(root: Path) -> dict[str, str]:
    """Hash the exact fixed COPY surface, paths included, with finite reads.

    Docker excludes bytecode in the same way. The checkout must independently
    be clean; these digests attest the producer, not any measured extraction.
    """
    records: list[tuple[str, str]] = []
    total = 0
    paths = [root / name for name in FIXED_FILES]
    for name in ("src", "scripts"):
        paths.extend((root / name).rglob("*"))
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise GrowthError("source_identity")
        total += info.st_size
        if total > 64 * 1024**2 or len(records) >= 4096:
            raise GrowthError("source_identity")
        data = path.read_bytes()
        if len(data) != info.st_size:
            raise GrowthError("source_identity")
        records.append((relative, digest(data)))
    if not any(name == "src/django_ray/__init__.py" for name, _ in records):
        raise GrowthError("source_identity")

    def selected(prefixes: tuple[str, ...]) -> str:
        return digest(encode([row for row in records if row[0].startswith(prefixes)]))

    return {
        "copied_source_sha256": selected(("",)),
        "package_sha256": selected(("src/django_ray/",)),
        "implementation_sha256": selected(
            ("scripts/runtime_env_growth/", "scripts/benchmark_runtime_env_growth.py")
        ),
    }


def installed_identity(root: Path = ROOT) -> dict[str, str]:
    """Verify the installed editable package resolves to this measured tree."""
    spec = importlib.util.find_spec("django_ray")
    if (
        spec is None
        or spec.origin is None
        or Path(spec.origin).resolve() != (root / "src/django_ray/__init__.py").resolve()
    ):
        raise GrowthError("source_identity")
    value = source_identity(root)
    seal_path = root / "growth-source.json"
    if seal_path.stat().st_size > 4096:
        raise GrowthError("source_identity")
    seal = decode(seal_path.read_bytes(), 4096)
    if not isinstance(seal, dict) or set(seal) != {*value, "source_commit", "source_tree"}:
        raise GrowthError("source_identity")
    if any(seal.get(key) != expected for key, expected in value.items()) or any(
        not isinstance(seal[key], str) or not re.fullmatch(r"[a-f0-9]{40}", seal[key])
        for key in ("source_commit", "source_tree")
    ):
        raise GrowthError("source_identity")
    return {**seal, "django_ray_version": importlib.metadata.version("django-ray")}


def main() -> int:
    # Fixed Docker build step; no caller-selected path, recipe or executable.
    commit, tree, expected = sys.argv[1:]
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"[a-f0-9]{40}", tree):
        raise GrowthError("source_identity")
    values = source_identity(ROOT)
    if values["copied_source_sha256"] != expected:
        raise GrowthError("source_identity")
    (ROOT / "growth-source.json").write_bytes(
        encode(
            {
                **values,
                "source_commit": commit,
                "source_tree": tree,
            }
        )
    )
    installed_identity()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
