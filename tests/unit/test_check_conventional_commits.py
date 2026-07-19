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
            commits=[
                "feat(worker): preserve task ownership\n\nKeep ownership tied to the active lease.",
                "test: cover lease recovery\n\nExercise the expired-worker recovery path.",
            ],
            commit_range=None,
        )
        == []
    )


def test_validate_accepts_breaking_change_header() -> None:
    assert (
        CHECKER.validate(
            title="feat!: remove legacy worker protocol",
            commits=[
                "feat!: remove legacy worker protocol\n\nBREAKING CHANGE: require the new protocol."
            ],
            commit_range=None,
        )
        == []
    )


def test_validate_reports_invalid_title_and_commit_type() -> None:
    errors = CHECKER.validate(
        title="WIP changes",
        commits=["wip: temporary debugging\n\nThis is not ready for review."],
        commit_range=None,
    )

    assert len(errors) == 2
    assert "PR title" in errors[0]
    assert "unsupported type" in errors[1]


def test_validate_requires_commit_headers() -> None:
    errors = CHECKER.validate(title="docs: update workflow guide", commits=[], commit_range=None)

    assert errors == ["No commit headers were found to validate."]


def test_validate_rejects_one_line_commit() -> None:
    errors = CHECKER.validate(
        title="fix: close the lease",
        commits=["fix: close the lease"],
        commit_range=None,
    )

    assert errors == [
        "Commit 1 must include a descriptive body after the Conventional Commit header."
    ]


def test_validate_rejects_overlong_commit_message_line() -> None:
    errors = CHECKER.validate(
        title="fix: close the lease",
        commits=["fix: close the lease\n\n" + ("x" * 73)],
        commit_range=None,
    )

    assert errors == [
        "Commit 1 line 3 exceeds 72 characters (73). "
        "Wrap commit-message lines for narrow terminals."
    ]


def test_validate_reads_full_commit_messages_from_json_file(tmp_path: Path) -> None:
    messages = tmp_path / "messages.json"
    messages.write_text(
        '["fix: close the lease\\n\\nPrevent duplicate lease cleanup.", '
        '"docs: explain queueing\\n\\nDescribe queue selection."]',
        encoding="utf-8",
    )

    assert (
        CHECKER.validate(
            title="fix: close the lease",
            commits=[],
            commit_range=None,
            commit_json_file=str(messages),
        )
        == []
    )


def test_validate_reads_full_commit_message_from_file(tmp_path: Path) -> None:
    message = tmp_path / "message.txt"
    message.write_text("fix: close the lease\n\nPrevent duplicate lease cleanup.", encoding="utf-8")

    assert (
        CHECKER.validate(
            title="fix: close the lease",
            commits=[],
            commit_range=None,
            commit_file=str(message),
        )
        == []
    )
