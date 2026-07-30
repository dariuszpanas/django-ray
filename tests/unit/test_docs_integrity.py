"""Contracts that keep the growing live documentation discoverable and current."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from django_ray.conf.defaults import DEFAULTS

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
SOURCE_ONLY_PAGES = {Path("README.md")}
EVIDENCE_DIRECTORIES = {"benchmarks", "investigations"}
SETTINGS_OUTSIDE_DJANGO_RAY = {"RAY_DASHBOARD_URL"}


def test_repository_llms_guide_matches_published_copy() -> None:
    assert (ROOT / "llms.txt").read_bytes() == (DOCS / "llms.txt").read_bytes()


def _nav_markdown_paths(value: Any) -> set[Path]:
    if isinstance(value, str):
        return {Path(value)} if value.endswith(".md") else set()
    if isinstance(value, list):
        return set().union(*(_nav_markdown_paths(item) for item in value))
    if isinstance(value, dict):
        return set().union(*(_nav_markdown_paths(item) for item in value.values()))
    return set()


def test_live_documentation_pages_are_navigable_or_source_only() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text(encoding="utf-8"))
    nav_paths = _nav_markdown_paths(config["project"]["nav"])
    all_pages = {path.relative_to(DOCS) for path in DOCS.rglob("*.md")}
    live_pages = {
        path for path in all_pages if not path.parts or path.parts[0] not in EVIDENCE_DIRECTORIES
    }

    assert nav_paths <= all_pages
    assert live_pages - nav_paths == SOURCE_ONLY_PAGES


def test_settings_reference_tracks_every_package_default() -> None:
    reference = (DOCS / "reference" / "settings.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^### ([A-Z][A-Z0-9_]*)\s*$", reference, re.MULTILINE))

    assert documented == set(DEFAULTS) | SETTINGS_OUTSIDE_DJANGO_RAY


def test_management_commands_are_discoverable_in_live_guides() -> None:
    command_directory = ROOT / "src" / "django_ray" / "management" / "commands"
    commands = {path.stem for path in command_directory.glob("*.py") if path.name != "__init__.py"}
    live_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in DOCS.rglob("*")
        if path.suffix in {".md", ".txt"}
        and path.name not in {"README.md", "changelog.md"}
        and (
            not path.relative_to(DOCS).parts
            or path.relative_to(DOCS).parts[0] not in EVIDENCE_DIRECTORIES
        )
    )

    assert {command for command in commands if command not in live_text} == set()
