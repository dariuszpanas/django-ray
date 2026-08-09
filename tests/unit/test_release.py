"""Tests for release version validation helpers."""

from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import scripts.validate_release as release
import scripts.verify_release_source as release_source
from scripts.validate_release import (
    normalize_version,
    validate_compiled_graph_capability_review,
    validate_one_zero_readiness,
    validate_release_version,
    validate_testpypi_candidate,
)

ROOT = Path(__file__).parents[2]
_EXPECTED_READINESS_CRITERIA_V1_OWNERS = {
    "adoption.documentation-review": "external",
    "adoption.external-applications": "external",
    "adoption.kuberay-deployment": "external",
    "adoption.observation-window": "maintainer",
    "adoption.preserved-state-upgrade": "external",
    "operations.operator-controls": "repository",
    "operations.preserved-data-upgrade": "repository",
    "operations.protocol-fencing": "repository",
    "operations.soak-certification": "repository",
    "product.compatibility-matrix": "repository",
    "product.experimental-boundary": "repository",
    "product.no-severe-defects": "maintainer",
    "product.stable-api": "repository",
    "release.final-evidence": "repository",
    "support.contributor-triage": "repository",
    "support.maintainer-capacity": "maintainer",
    "support.release-and-rollback": "repository",
    "support.security-fix-policy": "maintainer",
    "support.vulnerability-reporting": "repository",
}


def _write_readiness_fixture(
    root: Path,
    *,
    version: str = "0.9.0",
    decision_status: str = "tracking",
    criterion_status: str = "pending",
    classifiers: tuple[str, ...] = ("Development Status :: 4 - Beta",),
    accepted_on: date | None = None,
) -> None:
    accepted = decision_status == "accepted"
    if accepted != (accepted_on is not None):
        raise ValueError("accepted readiness fixtures need exactly one injected acceptance date")
    classifier_lines = "\n".join(f'    "{classifier}",' for classifier in classifiers)
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\nclassifiers = [\n{classifier_lines}\n]\n',
        encoding="utf-8",
    )
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "stability.md").write_text("candidate policy\n", encoding="utf-8")
    (docs / "one-zero-readiness.md").write_text(
        "# Readiness\n\n"
        + "\n".join(
            f"<!-- readiness-criterion: {identifier} -->"
            for identifier in _EXPECTED_READINESS_CRITERIA_V1_OWNERS
        )
        + "\n",
        encoding="utf-8",
    )
    contracts = root / "tests" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "one_zero_readiness_v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contract": "django-ray-1.0-readiness-v1",
                "target_version": "1.0.0",
                "decision": {
                    "status": decision_status,
                    "accepted_on": accepted_on.isoformat() if accepted_on is not None else None,
                    "accepted_by": "maintainer" if accepted else None,
                },
                "criteria": [
                    {
                        "id": identifier,
                        "category": identifier.partition(".")[0],
                        "required": True,
                        "owner": owner,
                        "status": criterion_status,
                        "evidence": [{"kind": "path", "value": "docs/stability.md"}],
                        "disposition": (
                            f"Accept the checked {identifier} criterion before publishing version one."
                        ),
                    }
                    for identifier, owner in _EXPECTED_READINESS_CRITERIA_V1_OWNERS.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "--quiet", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "--", "docs/stability.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=django-ray tests",
            "-c",
            "user.email=tests@django-ray.invalid",
            "commit",
            "--quiet",
            "-m",
            "test: retain readiness evidence",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


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
    assert release._read_pyproject_version(ROOT) == "0.5.0"
    assert release._read_module_version(ROOT) == "0.5.0"
    assert release._read_lock_version(ROOT) == "0.5.0"


def test_readiness_v1_contract_latches_criterion_ownership() -> None:
    assert release._READINESS_CRITERIA_V1_OWNERS == (_EXPECTED_READINESS_CRITERIA_V1_OWNERS)


def test_repository_one_zero_readiness_registry_is_valid_and_tracking() -> None:
    record = validate_one_zero_readiness(ROOT)

    assert record["decision"] == {
        "status": "tracking",
        "accepted_on": None,
        "accepted_by": None,
    }
    assert {criterion["status"] for criterion in record["criteria"]} == {
        "pending",
        "satisfied",
    }


def test_readiness_registry_latches_the_beta_classifier(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path, classifiers=())

    with pytest.raises(ValueError, match="Beta classifier cannot be removed"):
        validate_one_zero_readiness(tmp_path)


def test_accepted_readiness_rejects_incomplete_required_criteria(tmp_path: Path) -> None:
    as_of = date.today()
    _write_readiness_fixture(tmp_path, decision_status="accepted", accepted_on=as_of)

    with pytest.raises(ValueError, match="incomplete required criteria"):
        validate_one_zero_readiness(tmp_path, as_of=as_of)


def test_one_x_requires_accepted_readiness_and_production_classifier(tmp_path: Path) -> None:
    as_of = date.today()
    _write_readiness_fixture(
        tmp_path,
        version="1.0.0",
        decision_status="accepted",
        criterion_status="satisfied",
        classifiers=("Development Status :: 5 - Production/Stable",),
        accepted_on=as_of,
    )

    record = validate_one_zero_readiness(tmp_path, require_accepted=True, as_of=as_of)

    assert record["decision"]["status"] == "accepted"
    assert record["decision"]["accepted_on"] == as_of.isoformat()


def test_accepted_readiness_rejects_satisfied_criterion_without_evidence(
    tmp_path: Path,
) -> None:
    as_of = date.today()
    _write_readiness_fixture(
        tmp_path,
        decision_status="accepted",
        criterion_status="satisfied",
        classifiers=("Development Status :: 5 - Production/Stable",),
        accepted_on=as_of,
    )
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["owner"] = "external"
    record["criteria"][0]["evidence"] = []
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="needs evidence"):
        validate_one_zero_readiness(tmp_path, as_of=as_of)


def test_accepted_readiness_rejects_future_acceptance_date(tmp_path: Path) -> None:
    as_of = date.today()
    accepted_on = as_of + timedelta(days=1)
    _write_readiness_fixture(
        tmp_path,
        decision_status="accepted",
        criterion_status="satisfied",
        classifiers=("Development Status :: 5 - Production/Stable",),
        accepted_on=accepted_on,
    )

    with pytest.raises(ValueError, match="cannot be in the future"):
        validate_one_zero_readiness(tmp_path, as_of=as_of)

    record = validate_one_zero_readiness(tmp_path, as_of=accepted_on)
    assert record["decision"]["accepted_on"] == accepted_on.isoformat()


def test_readiness_category_must_match_criterion_id(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path)
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["category"] = "product"
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="category must match its id"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_v1_rejects_removed_criterion_and_matching_doc_marker(
    tmp_path: Path,
) -> None:
    _write_readiness_fixture(tmp_path)
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    removed = record["criteria"].pop(0)
    registry.write_text(json.dumps(record), encoding="utf-8")
    readiness_doc = tmp_path / "docs" / "one-zero-readiness.md"
    marker = f"<!-- readiness-criterion: {removed['id']} -->\n"
    readiness_doc.write_text(
        readiness_doc.read_text(encoding="utf-8").replace(marker, ""),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly match the v1 contract"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_v1_rejects_optional_or_reowned_criterion(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path)
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["required"] = False
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="must remain required in v1"):
        validate_one_zero_readiness(tmp_path)

    record["criteria"][0]["required"] = True
    record["criteria"][0]["owner"] = "maintainer"
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="owner must match the v1 contract"):
        validate_one_zero_readiness(tmp_path)


def test_satisfied_external_readiness_requires_repository_path_evidence(
    tmp_path: Path,
) -> None:
    _write_readiness_fixture(tmp_path)
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["status"] = "satisfied"
    record["criteria"][0]["evidence"] = [
        {
            "kind": "issue",
            "value": "https://github.com/dariuszpanas/django-ray/issues/999999999",
        }
    ]
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="needs repository-path evidence"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_path_evidence_must_be_tracked(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path)
    untracked = tmp_path / "docs" / "generated-evidence.md"
    untracked.write_text("generated after checkout\n", encoding="utf-8")
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["evidence"] = [{"kind": "path", "value": "docs/generated-evidence.md"}]
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="untracked repository path"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_path_evidence_must_be_committed(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path)
    staged = tmp_path / "docs" / "staged-evidence.md"
    staged.write_text("staged after the candidate commit\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "--", "docs/staged-evidence.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["evidence"] = [{"kind": "path", "value": "docs/staged-evidence.md"}]
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="committed in the candidate tree"):
        validate_one_zero_readiness(tmp_path)


@pytest.mark.parametrize("stage_change", [False, True], ids=["worktree", "index"])
def test_readiness_path_evidence_must_match_candidate_tree(
    tmp_path: Path,
    *,
    stage_change: bool,
) -> None:
    _write_readiness_fixture(tmp_path)
    (tmp_path / "docs" / "stability.md").write_text(
        "modified after the candidate commit\n",
        encoding="utf-8",
    )
    if stage_change:
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "--", "docs/stability.md"],
            check=True,
            capture_output=True,
            text=True,
        )

    with pytest.raises(ValueError, match="does not match the candidate tree"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_path_evidence_uses_literal_git_paths(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path)
    untracked = tmp_path / "docs" / "stabilit[y].md"
    untracked.write_text("literal wildcard evidence\n", encoding="utf-8")
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["evidence"] = [{"kind": "path", "value": "docs/stabilit[y].md"}]
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="untracked repository path"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_path_evidence_cannot_escape_repository(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-evidence.md"
    outside.write_text("outside repository\n", encoding="utf-8")
    escaped = tmp_path / "docs" / "escaped-evidence.md"
    try:
        escaped.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - platform capability
        pytest.skip(f"symlink creation is unavailable: {exc}")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "--", "docs/escaped-evidence.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["evidence"] = [{"kind": "path", "value": "docs/escaped-evidence.md"}]
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="must remain inside the repository"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_path_evidence_cannot_link_to_untracked_repository_file(
    tmp_path: Path,
) -> None:
    _write_readiness_fixture(tmp_path)
    generated = tmp_path / "docs" / "generated-evidence.md"
    generated.write_text("generated inside repository\n", encoding="utf-8")
    linked = tmp_path / "docs" / "linked-evidence.md"
    try:
        linked.symlink_to(generated.name)
    except OSError as exc:  # pragma: no cover - platform capability
        pytest.skip(f"symlink creation is unavailable: {exc}")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "--", "docs/linked-evidence.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["criteria"][0]["evidence"] = [{"kind": "path", "value": "docs/linked-evidence.md"}]
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="path must not use symlinks"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_registry_rejects_duplicate_keys_and_boolean_schema(
    tmp_path: Path,
) -> None:
    _write_readiness_fixture(tmp_path)
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    original = registry.read_text(encoding="utf-8")
    registry.write_text(
        original.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON object key 'schema_version'"):
        validate_one_zero_readiness(tmp_path)

    record = json.loads(original)
    record["schema_version"] = True
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version must be 1"):
        validate_one_zero_readiness(tmp_path)


def test_readiness_acceptance_date_requires_canonical_injected_boundary(
    tmp_path: Path,
) -> None:
    as_of = date.today()
    _write_readiness_fixture(
        tmp_path,
        version="1.0.0",
        decision_status="accepted",
        criterion_status="satisfied",
        classifiers=("Development Status :: 5 - Production/Stable",),
        accepted_on=as_of,
    )
    registry = tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json"
    record = json.loads(registry.read_text(encoding="utf-8"))
    record["decision"]["accepted_on"] = as_of.strftime("%Y%m%d")
    registry.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="must use YYYY-MM-DD"):
        validate_one_zero_readiness(tmp_path, as_of=as_of)


def test_one_x_rejects_additional_development_classifier(tmp_path: Path) -> None:
    as_of = date.today()
    _write_readiness_fixture(
        tmp_path,
        version="1.0.0",
        decision_status="accepted",
        criterion_status="satisfied",
        classifiers=(
            "Development Status :: 3 - Alpha",
            "Development Status :: 5 - Production/Stable",
        ),
        accepted_on=as_of,
    )

    with pytest.raises(ValueError, match="only the Production/Stable"):
        validate_one_zero_readiness(tmp_path, as_of=as_of)


def test_readiness_introduction_includes_prerelease_candidates() -> None:
    assert release._readiness_registry_required("0.4.0-rc.1")
    assert not release._readiness_registry_required("0.3.9")


def test_one_x_rejects_tracking_readiness_even_with_beta_metadata(tmp_path: Path) -> None:
    _write_readiness_fixture(tmp_path, version="1.0.0")

    with pytest.raises(ValueError, match="requires an accepted 1.0 readiness decision"):
        validate_one_zero_readiness(tmp_path)


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


def test_development_validator_threads_one_injected_evaluation_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_date = date.today()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.4.0"\n',
        encoding="utf-8",
    )
    observed: list[date] = []

    def validate_readiness(
        _root: Path,
        *,
        require_accepted: bool = False,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        assert require_accepted is False
        assert as_of is not None
        observed.append(as_of)
        return {}

    def validate_changelog(
        _root: Path,
        *,
        as_of: date | None = None,
        released_versions: set[str] | None = None,
        pending_release_version: str | None = None,
    ) -> bool:
        assert released_versions is None
        assert pending_release_version is None
        assert as_of is not None
        observed.append(as_of)
        return False

    monkeypatch.setattr(release, "validate_one_zero_readiness", validate_readiness)
    monkeypatch.setattr(release, "_read_git_release_versions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release, "_validate_changelog_development", validate_changelog)

    release.validate_development_changelog(tmp_path, as_of=evaluation_date)

    assert observed == [evaluation_date, evaluation_date]


def test_development_validator_requires_the_readiness_registry(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.4.0"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="readiness registry is unavailable"):
        release.validate_development_changelog(tmp_path, as_of=date.today())


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
    _write_readiness_fixture(root, version="0.4.0")


def _expect_readiness_evaluation_date(
    monkeypatch: pytest.MonkeyPatch,
    expected: date,
) -> None:
    validate_readiness = release.validate_one_zero_readiness

    def validate_with_expected_date(
        root: Path,
        *,
        require_accepted: bool = False,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        assert as_of == expected
        return validate_readiness(root, require_accepted=require_accepted, as_of=as_of)

    monkeypatch.setattr(release, "validate_one_zero_readiness", validate_with_expected_date)


def _mock_testpypi_candidate_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    released_versions: set[str],
    expected_as_of: date | None = None,
) -> None:
    def accept_compiled_graph_review(_root: Path, *, as_of: date | None) -> Path:
        assert isinstance(as_of, date)
        if expected_as_of is not None:
            assert as_of == expected_as_of
        return Path("review.json")

    monkeypatch.setattr(
        release,
        "_read_git_release_versions",
        lambda _root, *, require_complete: released_versions,
    )
    monkeypatch.setattr(
        release,
        "validate_compiled_graph_capability_review",
        accept_compiled_graph_review,
    )
    if expected_as_of is not None:
        _expect_readiness_evaluation_date(monkeypatch, expected_as_of)


def test_testpypi_candidate_accepts_unreleased_tree_while_production_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    as_of = date.today()
    _write_testpypi_candidate_fixture(tmp_path)
    _mock_testpypi_candidate_dependencies(
        monkeypatch,
        released_versions={"0.3.1"},
        expected_as_of=as_of,
    )

    assert validate_testpypi_candidate(tmp_path, "v0.4.0", as_of=as_of) == "0.4.0"
    with pytest.raises(ValueError, match=r"one dated \[0\.4\.0\] release heading"):
        validate_release_version(tmp_path, "v0.4.0", as_of=as_of)


def test_testpypi_candidate_requires_the_readiness_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_testpypi_candidate_fixture(tmp_path)
    (tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json").unlink()
    _mock_testpypi_candidate_dependencies(
        monkeypatch,
        released_versions={"0.3.1"},
    )

    with pytest.raises(ValueError, match="readiness registry is unavailable"):
        validate_testpypi_candidate(tmp_path, "v0.4.0")


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
    evaluation_date = date.today()
    previous_release_date = evaluation_date - timedelta(days=1)
    _write_testpypi_candidate_fixture(tmp_path)
    changelog = tmp_path / "docs" / "changelog.md"
    changelog.write_text(
        (
            "## [Unreleased]\n\n"
            f"## [0.4.0] - {evaluation_date.isoformat()}\n\n- ready\n\n"
            f"## [0.3.1] - {previous_release_date.isoformat()}\n\n- released\n\n"
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
        expected_as_of=evaluation_date,
    )

    assert validate_testpypi_candidate(tmp_path, "0.4.0", as_of=evaluation_date) == "0.4.0"


def test_release_candidate_validation_accepts_consistent_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluation_date = date.today()
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
        f"## [0.4.0] - {evaluation_date.isoformat()}\n\n- ready\n\n"
        "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
        "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n",
        encoding="utf-8",
    )

    def accept_compiled_graph_review(_root: Path, *, as_of: date | None) -> Path:
        assert as_of == evaluation_date
        return tmp_path / "review.json"

    monkeypatch.setattr(
        release,
        "validate_compiled_graph_capability_review",
        accept_compiled_graph_review,
    )
    _write_readiness_fixture(tmp_path, version="0.4.0")
    _expect_readiness_evaluation_date(monkeypatch, evaluation_date)

    assert validate_release_version(tmp_path, "v0.4.0", as_of=evaluation_date) == "0.4.0"


def test_release_validator_requires_the_readiness_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_date = date.today()
    _write_testpypi_candidate_fixture(tmp_path)
    changelog = tmp_path / "docs" / "changelog.md"
    changelog.write_text(
        "## [Unreleased]\n\n"
        f"## [0.4.0] - {evaluation_date.isoformat()}\n\n- ready\n\n"
        "[Unreleased]: https://github.com/dariuszpanas/django-ray/compare/v0.4.0...HEAD\n"
        "[0.4.0]: https://github.com/dariuszpanas/django-ray/compare/v0.3.1...v0.4.0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "contracts" / "one_zero_readiness_v1.json").unlink()

    def accept_compiled_graph_review(_root: Path, *, as_of: date | None) -> Path:
        assert as_of == evaluation_date
        return tmp_path / "review.json"

    monkeypatch.setattr(
        release,
        "validate_compiled_graph_capability_review",
        accept_compiled_graph_review,
    )

    with pytest.raises(ValueError, match="readiness registry is unavailable"):
        validate_release_version(tmp_path, "v0.4.0", as_of=evaluation_date)


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

    assert path.name == "compiled-graph-capability-review-2026-08-02.json"


def test_no_promotion_review_remains_safe_after_artifact_expiry() -> None:
    path = validate_compiled_graph_capability_review(ROOT, as_of=date(2030, 1, 1))

    assert path.name == "compiled-graph-capability-review-2026-08-02.json"


def _review_record() -> dict[str, Any]:
    path = ROOT / "docs" / "investigations" / "compiled-graph-capability-review-2026-08-02.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_review(root: Path, record: dict[str, Any]) -> None:
    directory = root / "docs" / "investigations"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "compiled-graph-capability-review-2026-08-02.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def _promoted_capability() -> dict[str, str]:
    return {
        "ray_version": "2.56.0",
        "python_version": "3.12.13",
        "operating_system": "linux",
        "architecture": "x86_64",
        "python_implementation": "cpython",
        "python_abi": "cpython-312-x86_64-linux-gnu",
        "dependency_profile": "ray=2.56.0;numpy=2.5.1;cupy-cuda12x=14.1.1",
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
    artifact_index = next(
        index
        for index, artifact in enumerate(record["artifacts"])
        if artifact["ray_version"] == capability["ray_version"]
    )
    record["artifacts"].insert(0, record["artifacts"].pop(artifact_index))
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
            "reviewed_on": "2026-08-02",
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
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, [capability]))
    assert record["artifacts"][0]["observation"]["adapter_eligible"] is False

    assert validate_compiled_graph_capability_review(
        tmp_path, as_of=date(2026, 8, 3)
    ).name.endswith("2026-08-02.json")

    neighbor = {**capability, "ray_version": "2.56.1"}
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, [neighbor]))
    with pytest.raises(ValueError, match="exactly match"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))

    record = _promotion_review(capability)
    record["artifacts"][0]["observed_capability"] = {
        **capability,
        "python_version": "3.12.14",
    }
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, [capability]))
    with pytest.raises(ValueError, match="retained evidence"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))


def test_future_promotion_requires_successful_verified_complete_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, [capability]))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["native_probe_status"] = "native_crash"
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="successful native probe"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["result_verified"] = False
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="verify its native result"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["missing_dimensions"] = ["deployment_profile"]
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="unresolved capability dimensions"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))

    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["adapter_reason"] = "INCOMPLETE_CAPABILITY_CONTEXT"
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="complete unpromoted candidate"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))


def test_observed_capability_must_match_artifact_and_probe_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    record = _promotion_review(capability)
    record["artifacts"][0]["observation"]["topology"] = "direct-driver"
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, [capability]))

    with pytest.raises(ValueError, match="conflicts with its observation"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))


def test_future_promotion_requires_evidence_and_fresh_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["evidence_ids"] = []
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, [capability]))

    with pytest.raises(ValueError, match="at least one evidence ID"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))

    record = _promotion_review(capability)
    record["verified_capability_rows"][0].pop("reviewed_on")
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="row reviewed_on must be an ISO date"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))

    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["revalidate_on_or_before"] = "2026-08-03"
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="requires revalidation"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 4))


def test_future_promotion_rejects_expired_or_quarantined_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _promoted_capability()
    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["revalidate_on_or_before"] = "2026-12-31"
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, [capability]))

    with pytest.raises(ValueError, match="expired evidence"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 10, 19))

    record = _promotion_review(capability)
    evidence_id = record["artifacts"][0]["evidence_id"]
    record["artifacts"][0]["quarantined"] = True
    record["quarantined_evidence_ids"] = [evidence_id]
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="quarantined evidence"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))

    record = _promotion_review(capability)
    record["verified_capability_rows"][0]["quarantined"] = True
    _write_review(tmp_path, record)
    with pytest.raises(ValueError, match="rows cannot remain verified"):
        validate_compiled_graph_capability_review(tmp_path, as_of=date(2026, 8, 3))


def test_no_promotion_may_retain_expired_quarantined_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _review_record()
    evidence_id = record["artifacts"][0]["evidence_id"]
    record["artifacts"][0]["quarantined"] = True
    record["quarantined_evidence_ids"] = [evidence_id]
    _write_review(tmp_path, record)
    monkeypatch.setattr(release, "_load_runtime_policy", lambda _root: (3, 2, []))

    validate_compiled_graph_capability_review(tmp_path, as_of=date(2030, 1, 1))
