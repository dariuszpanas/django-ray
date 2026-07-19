"""Tests for release version validation helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_release import normalize_version, validate_release_version

ROOT = Path(__file__).parents[2]


def test_normalize_version_accepts_tag_and_manual_forms() -> None:
    assert normalize_version("v0.3.0") == "0.3.0"
    assert normalize_version("0.3.0-rc1") == "0.3.0-rc1"


def test_normalize_version_rejects_unversioned_refs() -> None:
    with pytest.raises(ValueError, match="must look like"):
        normalize_version("main")


def test_release_versions_match_repository_sources() -> None:
    assert validate_release_version(ROOT, "v0.3.1") == "0.3.1"


def test_release_version_mismatch_is_actionable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.3.1"\n', encoding="utf-8")
    module = tmp_path / "src" / "django_ray"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text('__version__ = "0.3.1"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="do not agree"):
        validate_release_version(tmp_path, "v0.3.0")
