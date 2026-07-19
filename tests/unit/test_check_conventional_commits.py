from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_conventional_commits.py"
SPEC = importlib.util.spec_from_file_location("check_conventional_commits", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def test_validate_accepts_supported_title_and_multiple_commits() -> None:
    assert (
        CHECKER.validate(
            title="feat(worker): preserve task ownership",
            commits=["feat(worker): preserve task ownership", "test: cover lease recovery"],
            commit_range=None,
        )
        == []
    )


def test_validate_accepts_breaking_change_header() -> None:
    assert (
        CHECKER.validate(
            title="feat!: remove legacy worker protocol",
            commits=["feat!: remove legacy worker protocol"],
            commit_range=None,
        )
        == []
    )


def test_validate_reports_invalid_title_and_commit_type() -> None:
    errors = CHECKER.validate(
        title="WIP changes",
        commits=["wip: temporary debugging"],
        commit_range=None,
    )

    assert len(errors) == 2
    assert "PR title" in errors[0]
    assert "unsupported type" in errors[1]


def test_validate_requires_commit_headers() -> None:
    errors = CHECKER.validate(title="docs: update workflow guide", commits=[], commit_range=None)

    assert errors == ["No commit headers were found to validate."]


def test_validate_reads_commit_subjects_from_json_file(tmp_path: Path) -> None:
    subjects = tmp_path / "subjects.json"
    subjects.write_text('["fix: close the lease", "docs: explain queueing"]', encoding="utf-8")

    assert (
        CHECKER.validate(
            title="fix: close the lease",
            commits=[],
            commit_range=None,
            commit_json_file=str(subjects),
        )
        == []
    )
