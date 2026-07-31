"""Tests for release version validation helpers."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import scripts.validate_release as release
import scripts.verify_release_source as release_source
from scripts.validate_release import (
    normalize_version,
    validate_compiled_graph_capability_review,
    validate_release_version,
    validate_testpypi_candidate,
)

ROOT = Path(__file__).parents[2]


def test_local_python_version_allows_patch_updates() -> None:
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"


def test_normalize_version_accepts_tag_and_manual_forms() -> None:
    assert normalize_version("v0.3.0") == "0.3.0"
    assert normalize_version("0.3.0-rc1") == "0.3.0-rc1"


def test_normalize_version_rejects_unversioned_refs() -> None:
    with pytest.raises(ValueError, match="must look like"):
        normalize_version("main")


def test_semantic_version_order_places_final_after_prereleases() -> None:
    versions = [
        "0.4.0-rc.2",
        "0.4.0-beta.2",
        "0.4.0",
        "0.4.0-rc.10",
    ]

    assert sorted(versions, key=release._semantic_version_key, reverse=True) == [
        "0.4.0",
        "0.4.0-rc.10",
        "0.4.0-rc.2",
        "0.4.0-beta.2",
    ]


def test_release_versions_match_repository_sources() -> None:
    assert release._read_pyproject_version(ROOT) == "0.4.0"
    assert release._read_module_version(ROOT) == "0.4.0"
    assert release._read_lock_version(ROOT) == "0.4.0"


def test_release_version_mismatch_is_actionable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.3.1"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "django-ray"\nversion = "0.3.1"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    module = tmp_path / "src" / "django_ray"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text('__version__ = "0.3.1"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="do not agree"):
        validate_release_version(tmp_path, "v0.3.0")


def test_release_lock_version_mismatch_is_actionable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.4.0"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "django-ray"\nversion = "0.3.1"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    module = tmp_path / "src" / "django_ray"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text('__version__ = "0.4.0"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"uv\.lock=0\.3\.1"):
        validate_release_version(tmp_path, "v0.4.0")


def test_release_changelog_requires_an_empty_unreleased_section(tmp_path: Path) -> None:
    changelog = tmp_path / "docs" / "changelog.md"
    changelog.parent.mkdir()
    changelog.write_text(
        "## [Unreleased]\n\n- pending\n\n"
        "## [0.4.0] - 2026-07-28\n\n- ready\n\n"
        "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
        "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unreleased changelog section must be empty"):
        release._validate_changelog_release(tmp_path, "0.4.0")


def _write_development_changelog(
    root: Path,
    *,
    current_version: str,
    changelog: str,
) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{current_version}"\n',
        encoding="utf-8",
    )
    docs = root / "docs"
    docs.mkdir()
    (docs / "changelog.md").write_text(changelog, encoding="utf-8")


def test_development_changelog_accepts_all_current_work_as_unreleased(tmp_path: Path) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.4.0",
        changelog=(
            "## [Unreleased]\n\n### Added\n\n- pending\n\n"
            "## [0.3.1] - 2026-07-18\n\n- released\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...HEAD\n"
            "[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n"
        ),
    )

    assert (
        release._validate_changelog_development(
            tmp_path,
            as_of=date(2026, 7, 29),
            released_versions={"0.3.1"},
        )
        is False
    )


def test_development_changelog_rejects_mixed_current_release_and_unreleased(
    tmp_path: Path,
) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.4.0",
        changelog=(
            "## [Unreleased]\n\n### Added\n\n- still pending\n\n"
            "## [0.4.0] - 2026-07-29\n\n- supposedly released\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
            "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n"
        ),
    )

    with pytest.raises(ValueError, match="cannot be dated while Unreleased still contains"):
        release._validate_changelog_development(tmp_path, as_of=date(2026, 7, 29))


def test_development_changelog_rejects_future_release_date(tmp_path: Path) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.4.0",
        changelog=(
            "## [Unreleased]\n\n"
            "## [0.4.0] - 2026-08-03\n\n- ready later\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
            "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n"
        ),
    )

    with pytest.raises(ValueError, match=r"future-dated 2026-08-03"):
        release._validate_changelog_development(tmp_path, as_of=date(2026, 7, 29))


def test_development_changelog_rejects_dated_heading_without_git_tag(
    tmp_path: Path,
) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.4.0",
        changelog=(
            "## [Unreleased]\n\n### Added\n\n- pending\n\n"
            "## [0.3.2] - 2026-07-28\n\n- not actually released\n\n"
            "## [0.3.1] - 2026-07-18\n\n- released\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.3.2...HEAD\n"
            "[0.3.2]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.3.2\n"
            "[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n"
        ),
    )

    with pytest.raises(ValueError, match=r"missing v0\.3\.2"):
        release._validate_changelog_development(
            tmp_path,
            as_of=date(2026, 7, 29),
            released_versions={"0.3.1"},
        )


def test_development_changelog_rejects_release_tag_missing_from_changelog(
    tmp_path: Path,
) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.4.1",
        changelog=(
            "## [Unreleased]\n\n### Added\n\n- pending\n\n"
            "## [0.3.1] - 2026-07-18\n\n- released\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...HEAD\n"
            "[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n"
        ),
    )

    with pytest.raises(ValueError, match=r"missing \[0\.4\.0\]"):
        release._validate_changelog_development(
            tmp_path,
            as_of=date(2026, 7, 29),
            released_versions={"0.3.1", "0.4.0"},
        )


def test_development_changelog_rejects_release_headings_out_of_version_order(
    tmp_path: Path,
) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.5.0",
        changelog=(
            "## [Unreleased]\n\n### Added\n\n- pending\n\n"
            "## [0.3.1] - 2026-07-18\n\n- older\n\n"
            "## [0.4.0] - 2026-07-28\n\n- newer but misplaced\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...HEAD\n"
            "[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n"
            "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n"
        ),
    )

    with pytest.raises(ValueError, match="newest version first"):
        release._validate_changelog_development(
            tmp_path,
            as_of=date(2026, 7, 29),
            released_versions={"0.3.1", "0.4.0"},
        )


def test_development_changelog_rejects_release_link_that_skips_previous_version(
    tmp_path: Path,
) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.5.0",
        changelog=(
            "## [Unreleased]\n\n### Added\n\n- pending\n\n"
            "## [0.4.0] - 2026-07-29\n\n- released\n\n"
            "## [0.3.1] - 2026-07-18\n\n- previous\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
            "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.1.0...v0.4.0\n"
            "[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n"
        ),
    )

    with pytest.raises(ValueError, match=r"must compare v0\.3\.1 with v0\.4\.0"):
        release._validate_changelog_development(
            tmp_path,
            as_of=date(2026, 7, 29),
            released_versions={"0.3.1", "0.4.0"},
        )


def test_development_changelog_allows_one_explicit_release_candidate_before_tag(
    tmp_path: Path,
) -> None:
    _write_development_changelog(
        tmp_path,
        current_version="0.4.0",
        changelog=(
            "## [Unreleased]\n\n"
            "## [0.4.0] - 2026-07-29\n\n- ready\n\n"
            "## [0.3.1] - 2026-07-18\n\n- released\n\n"
            "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
            "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n"
            "[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n"
        ),
    )

    assert release._validate_changelog_development(
        tmp_path,
        as_of=date(2026, 7, 29),
        released_versions={"0.3.1"},
        pending_release_version="0.4.0",
    )


def test_git_release_versions_require_the_requested_root_to_be_checkout_top(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout_root = tmp_path / "checkout"
    source_root = checkout_root / "source-archive"
    source_root.mkdir(parents=True)

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        return release.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{checkout_root}\n",
            stderr="",
        )

    monkeypatch.setattr(release.subprocess, "run", fake_run)

    assert release._read_git_release_versions(source_root) is None
    with pytest.raises(ValueError, match="nested inside a different Git checkout"):
        release._read_git_release_versions(source_root, require_complete=True)


def test_git_release_versions_allow_source_archive_without_git_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        return release.subprocess.CompletedProcess(
            args=command,
            returncode=128,
            stdout="",
            stderr="not a git repository",
        )

    monkeypatch.setattr(release.subprocess, "run", fake_run)

    assert release._read_git_release_versions(tmp_path) is None
    with pytest.raises(ValueError, match="not a Git checkout"):
        release._read_git_release_versions(tmp_path, require_complete=True)


def test_git_release_versions_reject_shallow_metadata_when_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        "--show-toplevel": f"{tmp_path}\n",
        "--is-shallow-repository": "true\n",
    }

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        return release.subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=responses[command[-1]],
            stderr="",
        )

    monkeypatch.setattr(release.subprocess, "run", fake_run)

    assert release._read_git_release_versions(tmp_path) is None
    with pytest.raises(ValueError, match="checkout is shallow"):
        release._read_git_release_versions(tmp_path, require_complete=True)


def test_git_release_versions_read_semantic_tags_from_complete_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        "--show-toplevel": f"{tmp_path}\n",
        "--is-shallow-repository": "false\n",
        "v*": "v0.3.1\nv0.4.0-rc.1\nversion-next\n",
    }

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        return release.subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=responses[command[-1]],
            stderr="",
        )

    monkeypatch.setattr(release.subprocess, "run", fake_run)

    assert release._read_git_release_versions(tmp_path, require_complete=True) == {
        "0.3.1",
        "0.4.0-rc.1",
    }


def test_repository_development_changelog_is_consistent() -> None:
    release._validate_changelog_development(ROOT)


def _write_testpypi_candidate_fixture(root: Path, *, lock_version: str = "0.4.0") -> None:
    (root / "pyproject.toml").write_text('[project]\nversion = "0.4.0"\n', encoding="utf-8")
    (root / "uv.lock").write_text(
        "[[package]]\n"
        'name = "django-ray"\n'
        f'version = "{lock_version}"\n'
        'source = { editable = "." }\n',
        encoding="utf-8",
    )
    module = root / "src" / "django_ray"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text('__version__ = "0.4.0"\n', encoding="utf-8")
    changelog = root / "docs" / "changelog.md"
    changelog.parent.mkdir()
    changelog.write_text(
        "## [Unreleased]\n\n### Added\n\n- still being hardened\n\n"
        "## [0.3.1] - 2026-07-18\n\n- released\n\n"
        "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...HEAD\n"
        "[0.3.1]: https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n",
        encoding="utf-8",
    )


def _mock_testpypi_candidate_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    released_versions: set[str],
) -> None:
    monkeypatch.setattr(
        release,
        "_read_git_release_versions",
        lambda _root, *, require_complete: released_versions,
    )
    monkeypatch.setattr(
        release,
        "validate_compiled_graph_capability_review",
        lambda _root: Path("review.json"),
    )


def test_testpypi_candidate_accepts_unreleased_tree_while_production_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_testpypi_candidate_fixture(tmp_path)
    _mock_testpypi_candidate_dependencies(
        monkeypatch,
        released_versions={"0.3.1"},
    )

    assert validate_testpypi_candidate(tmp_path, "v0.4.0") == "0.4.0"
    with pytest.raises(ValueError, match=r"one dated \[0\.4\.0\] release heading"):
        validate_release_version(tmp_path, "v0.4.0")


def test_testpypi_candidate_rejects_mismatched_version_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_testpypi_candidate_fixture(tmp_path, lock_version="0.3.1")
    _mock_testpypi_candidate_dependencies(
        monkeypatch,
        released_versions={"0.3.1"},
    )

    with pytest.raises(ValueError, match=r"uv\.lock=0\.3\.1"):
        validate_testpypi_candidate(tmp_path, "0.4.0")


def test_testpypi_candidate_rejects_incomplete_tag_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_testpypi_candidate_fixture(tmp_path)
    _mock_testpypi_candidate_dependencies(
        monkeypatch,
        released_versions={"0.3.1", "0.3.2"},
    )

    with pytest.raises(ValueError, match=r"missing \[0\.3\.2\]"):
        validate_testpypi_candidate(tmp_path, "0.4.0")


def test_testpypi_candidate_rejects_already_tagged_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_testpypi_candidate_fixture(tmp_path)
    _mock_testpypi_candidate_dependencies(
        monkeypatch,
        released_versions={"0.3.1", "0.4.0"},
    )

    with pytest.raises(ValueError, match=r"v0\.4\.0 is already tagged"):
        validate_testpypi_candidate(tmp_path, "0.4.0")


def test_testpypi_candidate_also_accepts_strict_ready_untagged_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_testpypi_candidate_fixture(tmp_path)
    changelog = tmp_path / "docs" / "changelog.md"
    changelog.write_text(
        (
            "## [Unreleased]\n\n"
            "## [0.4.0] - 2026-07-30\n\n- ready\n\n"
            "## [0.3.1] - 2026-07-18\n\n- released\n\n"
            "[Unreleased]: "
            "https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
            "[0.4.0]: "
            "https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n"
            "[0.3.1]: "
            "https://github.com/dariuszpanas/django-ray/compare/v0.3.0...v0.3.1\n"
        ),
        encoding="utf-8",
    )
    _mock_testpypi_candidate_dependencies(
        monkeypatch,
        released_versions={"0.3.1"},
    )

    assert validate_testpypi_candidate(tmp_path, "0.4.0") == "0.4.0"


def test_release_candidate_validation_accepts_consistent_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.4.0"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "django-ray"\nversion = "0.4.0"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    module = tmp_path / "src" / "django_ray"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text('__version__ = "0.4.0"\n', encoding="utf-8")
    changelog = tmp_path / "docs" / "changelog.md"
    changelog.parent.mkdir()
    changelog.write_text(
        "## [Unreleased]\n\n"
        "## [0.4.0] - 2026-07-28\n\n- ready\n\n"
        "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
        "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        release,
        "validate_compiled_graph_capability_review",
        lambda _root: tmp_path / "review.json",
    )

    assert validate_release_version(tmp_path, "v0.4.0") == "0.4.0"


def test_manual_release_source_requires_all_identities_to_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_sha = "a" * 40

    def fake_git(_root: Path, *arguments: str) -> str:
        return {
            ("rev-parse", "HEAD"): candidate_sha,
            ("rev-parse", "refs/remotes/origin/main^{commit}"): candidate_sha,
        }[arguments]

    monkeypatch.setattr(release_source, "_git", fake_git)

    assert (
        release_source.verify_manual_candidate_source(
            tmp_path,
            candidate_sha=candidate_sha,
            event_sha=candidate_sha,
        )
        == candidate_sha
    )


@pytest.mark.parametrize(
    ("candidate_sha", "event_sha", "head_sha", "main_sha"),
    [
        ("a" * 40, "b" * 40, "a" * 40, "a" * 40),
        ("a" * 40, "a" * 40, "b" * 40, "a" * 40),
        ("a" * 40, "a" * 40, "a" * 40, "b" * 40),
    ],
)
def test_manual_release_source_rejects_each_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_sha: str,
    event_sha: str,
    head_sha: str,
    main_sha: str,
) -> None:
    def fake_git(_root: Path, *arguments: str) -> str:
        return {
            ("rev-parse", "HEAD"): head_sha,
            ("rev-parse", "refs/remotes/origin/main^{commit}"): main_sha,
        }[arguments]

    monkeypatch.setattr(release_source, "_git", fake_git)

    with pytest.raises(ValueError, match="source identities do not agree"):
        release_source.verify_manual_candidate_source(
            tmp_path,
            candidate_sha=candidate_sha,
            event_sha=event_sha,
        )


def test_manual_release_source_rejects_non_full_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        release_source.verify_manual_candidate_source(
            tmp_path,
            candidate_sha="abc123",
            event_sha="a" * 40,
        )


def test_production_release_source_requires_annotated_matching_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_sha = "a" * 40

    def fake_git(_root: Path, *arguments: str) -> str:
        return {
            ("cat-file", "-t", "refs/tags/v0.4.0"): "tag",
            ("rev-parse", "HEAD"): commit_sha,
            ("rev-parse", "refs/tags/v0.4.0^{commit}"): commit_sha,
            ("rev-parse", "refs/remotes/origin/main^{commit}"): commit_sha,
        }[arguments]

    monkeypatch.setattr(release_source, "_git", fake_git)

    assert (
        release_source.verify_production_tag_source(
            tmp_path,
            tag="v0.4.0",
            event_sha=commit_sha,
        )
        == "v0.4.0"
    )


@pytest.mark.parametrize(
    ("event_sha", "head_sha", "tag_sha", "main_sha"),
    [
        ("b" * 40, "a" * 40, "a" * 40, "a" * 40),
        ("a" * 40, "b" * 40, "a" * 40, "a" * 40),
        ("a" * 40, "a" * 40, "b" * 40, "a" * 40),
        ("a" * 40, "a" * 40, "a" * 40, "b" * 40),
    ],
)
def test_production_release_source_rejects_each_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_sha: str,
    head_sha: str,
    tag_sha: str,
    main_sha: str,
) -> None:
    def fake_git(_root: Path, *arguments: str) -> str:
        return {
            ("cat-file", "-t", "refs/tags/v0.4.0"): "tag",
            ("rev-parse", "HEAD"): head_sha,
            ("rev-parse", "refs/tags/v0.4.0^{commit}"): tag_sha,
            ("rev-parse", "refs/remotes/origin/main^{commit}"): main_sha,
        }[arguments]

    monkeypatch.setattr(release_source, "_git", fake_git)

    with pytest.raises(ValueError, match="source identities do not agree"):
        release_source.verify_production_tag_source(
            tmp_path,
            tag="v0.4.0",
            event_sha=event_sha,
        )


def test_production_release_source_rejects_lightweight_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_source, "_git", lambda *_args: "commit")

    with pytest.raises(ValueError, match="must be annotated"):
        release_source.verify_production_tag_source(
            tmp_path,
            tag="v0.4.0",
            event_sha="a" * 40,
        )


def test_latest_compiled_graph_review_matches_fail_closed_runtime_policy() -> None:
    path = validate_compiled_graph_capability_review(ROOT)

    assert path.name == "compiled-graph-capability-review-2026-07-20.json"


def test_no_promotion_review_remains_safe_after_artifact_expiry() -> None:
    path = validate_compiled_graph_capability_review(ROOT, as_of=date(2030, 1, 1))

    assert path.name == "compiled-graph-capability-review-2026-07-20.json"


def _review_record() -> dict[str, Any]:
    path = ROOT / "docs" / "investigations" / "compiled-graph-capability-review-2026-07-20.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_review(root: Path, record: dict[str, Any]) -> None:
    directory = root / "docs" / "investigations"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "compiled-graph-capability-review-2026-07-20.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def _promoted_capability() -> dict[str, str]:
    return {
        "ray_version": "2.53.0",
        "python_version": "3.12.13",
        "operating_system": "linux",
        "architecture": "x86_64",
        "python_implementation": "cpython",
        "python_abi": "cpython-312-x86_64-linux-gnu",
        "dependency_profile": "ray=2.53.0;numpy=2.5.1;cupy-cuda12x=14.1.1",
        "platform_profile": "Linux-6.17.0-x86_64-with-glibc2.39",
        "libc_profile": "glibc-2.39",
        "container_profile": "ghcr.io/example/django-ray@sha256:container",
        "deployment_profile": f"sha256:{'a' * 64}",
        "shared_memory_profile": "tmpfs:/dev/shm:size=8Gi",
        "object_store_profile": "memory=4Gi;spill=disabled",
        "topology": "nested-ray-task",
        "submission_transport": "direct-ray-core",
        "transport": "cpu-shared-memory",
    }


def _promotion_review(capability: dict[str, str]) -> dict[str, Any]:
    record = _review_record()
    evidence_id = record["artifacts"][0]["evidence_id"]
    record["artifacts"][0]["observed_capability"] = capability
    record["artifacts"][0]["observation"].update(
        {
            "native_probe_status": "success",
            "result_verified": True,
            "adapter_eligible": False,
            "adapter_reason": "CANDIDATE_REQUIRES_SMOKE",
            "missing_dimensions": [],
        }
    )
    record["decision"] = "promote"
    record["verified_capability_rows"] = [
        {
            "capability": capability,
            "evidence_ids": [evidence_id],
            "reviewed_on": "2026-07-20",
            "revalidate_on_or_before": "2026-10-18",
            "quarantined": False,
        }
    ]
    return record


def test_future_promotion_requires_exact_runtime_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    record = _promotion_review(capability)
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, [capability]))
    assert record["artifacts"][0]["observation"]["adapter_eligible"] is False

    assert validate_compiled_graph_capability_review(
        tmp_path, as_of=date(2026, 7, 21)
    ).name.endswith("2026-07-20.json")

    neighbor = {**capability, "ray_version": "2.53.1"}
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, [neighbor]))
    with pytest.raises(ValueError, match="exactly match"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))

    record = _promotion_review(capability)
    record["artifacts"][0]["observed_capability"] = {
        **capability,
        "python_version": "3.12.14",
    }
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, [capability]))
    with pytest.raises(ValueError, match="retained evidence"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))


def test_future_promotion_requires_successful_verified_complete_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, [capability]))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["native_probe_status"] = "native_crash"
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="successful native probe"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["result_verified"] = False
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="verify its native result"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["missing_dimensions"] = ["deployment_profile"]
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="unresolved capability dimensions"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["adapter_reason"] = "INCOMPLETE_CAPABILITY_CONTEXT"
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="complete unpromoted candidate"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))


def test_observed_capability_must_match_artifact_and_probe_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["topology"] = "direct-driver"
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, [capability]))

    with pytest.raises(ValueError, match="conflicts with its observation"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))


def test_future_promotion_requires_evidence_and_fresh_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["evidence_ids"] = []
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, [capability]))

    with pytest.raises(ValueError, match="at least one evidence ID"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))

    record = _promotion_review(capability)
    record["verified_capability_rows"][0].pop("reviewed_on")
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="row reviewed_on must be an ISO date"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))

    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["revalidate_on_or_before"] = "2026-07-21"
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="requires revalidation"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 22))


def test_future_promotion_rejects_expired_or_quarantined_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["revalidate_on_or_before"] = "2026-12-31"
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, [capability]))

    with pytest.raises(ValueError, match="expired evidence"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 10, 19))

    record = _promotion_review(capability)
    evidence_id = record["artifacts"][0]["evidence_id"]
    record["artifacts"][0]["quarantined"] = True
    record["quarantined_evidence_ids"] = [evidence_id]
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="quarantined evidence"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))

    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["quarantined"] = True
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="rows cannot remain verified"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 7, 21))


def test_no_promotion_may_retain_expired_quarantined_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _review_record()
    evidence_id = record["artifacts"][0]["evidence_id"]
    record["artifacts"][0]["quarantined"] = True
    record["quarantined_evidence_ids"] = [evidence_id]
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (2, 2, []))

    validate_compiled_graph_capability_review(tmp_path, as_of=date(2030, 1, 1))
