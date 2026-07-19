from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_conventional_commits.py"
TEMPLATE = Path(__file__).parents[2] / ".gitmessage"
SPEC = importlib.util.spec_from_file_location("check_conventional_commits", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)

VALID_MESSAGE = """fix(worker): preserve task ownership

## Summary

Keep task ownership tied to the worker's active lease.

## Validation

- `uv run pytest tests/unit/test_worker.py`: passed.
"""


def test_validate_accepts_structured_commits_and_optional_sections() -> None:
    assert (
        CHECKER.validate(
            title="feat(worker): preserve task ownership",
            commits=[
                """feat(worker): preserve task ownership

## Summary

Keep task ownership tied to the active worker lease.

## Operational note

Prevent expired workers from retaining durable task ownership.

## Validation

- Focused worker lease tests passed.
""",
                """test: cover lease recovery

## Summary

Exercise the expired-worker recovery path under deterministic timing.

## Validation

- Focused lease recovery tests passed.
""",
            ],
            commit_range=None,
        )
        == []
    )


def test_validate_accepts_breaking_change_with_footer() -> None:
    assert (
        CHECKER.validate(
            title="feat!: remove legacy worker protocol",
            commits=[
                """feat!: remove legacy worker protocol

## Summary

Require every worker to use the current durable protocol.

## Migration

Upgrade all workers before deploying the new release.

## Validation

- Protocol compatibility tests passed.

BREAKING CHANGE: legacy workers can no longer claim tasks.
"""
            ],
            commit_range=None,
        )
        == []
    )


def test_validate_accepts_bang_header_without_breaking_change_footer() -> None:
    assert (
        CHECKER.validate_message(
            VALID_MESSAGE.replace("fix(worker):", "fix(worker)!:"),
            label="Commit 1",
        )
        == []
    )


def test_validate_reports_invalid_title_and_commit_type() -> None:
    errors = CHECKER.validate(
        title="WIP changes",
        commits=[VALID_MESSAGE.replace("fix(worker):", "wip:")],
        commit_range=None,
    )

    assert len(errors) == 2
    assert "PR title" in errors[0]
    assert "unsupported type" in errors[1]


def test_validate_requires_commit_messages() -> None:
    errors = CHECKER.validate(title="docs: update workflow guide", commits=[], commit_range=None)

    assert errors == ["No commit headers were found to validate."]


def test_validate_rejects_bare_sentence_body() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\nPrevent duplicate lease cleanup.",
        label="Commit 1",
    )

    assert errors == [
        "Commit 1 must use structured sections: '## Summary' followed by '## Validation'."
    ]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            """fix: close the lease

## Summary

Prevent duplicate lease cleanup during worker recovery.
""",
            "missing the required '## Validation' section",
        ),
        (
            """fix: close the lease

## Validation

- Focused lease recovery tests passed.
""",
            "missing the required '## Summary' section",
        ),
        (
            """fix: close the lease

## Summary


## Validation

- Focused lease recovery tests passed.
""",
            "section '## Summary' must contain meaningful content",
        ),
        (
            """fix: close the lease

## Summary

Prevent duplicate lease cleanup during worker recovery.

## Summary

Preserve the active lease ownership invariant during cleanup.

## Validation

- Focused lease recovery tests passed.
""",
            "contains duplicate '## Summary' sections",
        ),
        (
            """fix: close the lease

## Validation

- Focused lease recovery tests passed.

## Summary

Prevent duplicate lease cleanup during worker recovery.
""",
            "must place '## Summary' before '## Validation'",
        ),
    ],
)
def test_validate_rejects_malformed_required_sections(message: str, expected: str) -> None:
    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    "placeholder",
    [
        "WIP",
        "iteration 3",
        "Address review feedback.",
        "Updates.",
        "Fix CI.",
        "<describe the durable change>",
        "[describe the durable change]",
        "...",
    ],
)
def test_validate_rejects_placeholder_section_content(placeholder: str) -> None:
    errors = CHECKER.validate_message(
        VALID_MESSAGE.replace(
            "Keep task ownership tied to the worker's active lease.", placeholder
        ),
        label="Commit 1",
    )

    assert any("contains only placeholder content" in error for error in errors)


def test_validate_accepts_placeholder_looking_line_with_substantive_prose() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "WIP\n\nKeep task ownership tied to the worker's active lease.",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_rejects_html_comment_only_section() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "<!-- describe the durable change here -->",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("section '## Summary' must contain meaningful content" in error for error in errors)


@pytest.mark.parametrize(
    "example",
    [
        "```text\n## Summary\nDurable change details.\n```",
        "    ## Summary\n    Durable change details.",
        "> ## Summary\n> Durable change details.",
        "<!--\n## Summary\nDurable change details.\n-->",
    ],
)
def test_validate_does_not_treat_example_headings_as_sections(example: str) -> None:
    message = f"fix: close the lease\n\n{example}\n\n## Validation\n\n- Focused tests passed."

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("missing the required '## Summary' section" in error for error in errors)


def test_validate_requires_exact_case_for_required_headings() -> None:
    message = VALID_MESSAGE.replace("## Summary", "## summary")

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("missing the required '## Summary' section" in error for error in errors)


def test_validate_counts_arbitrary_label_as_validation_content() -> None:
    message = VALID_MESSAGE.replace(
        "- `uv run pytest tests/unit/test_worker.py`: passed.",
        "Tests: uv run pytest completed successfully.",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_rejects_placeholder_header_summary() -> None:
    errors = CHECKER.validate_message(
        VALID_MESSAGE.replace("preserve task ownership", "iteration 3"),
        label="Commit 1",
    )

    assert any("uses a development placeholder" in error for error in errors)


def test_validate_rejects_summary_that_only_repeats_header() -> None:
    errors = CHECKER.validate_message(
        VALID_MESSAGE.replace(
            "Keep task ownership tied to the worker's active lease.",
            "Preserve task ownership.",
        ),
        label="Commit 1",
    )

    assert any("only repeats the header summary" in error for error in errors)


def test_validate_rejects_missing_blank_line_after_header() -> None:
    errors = CHECKER.validate_message(
        VALID_MESSAGE.replace("task ownership\n\n## Summary", "task ownership\n## Summary"),
        label="Commit 1",
    )

    assert any("separate its header and body with a blank line" in error for error in errors)


@pytest.mark.parametrize(("length", "has_error"), [(72, False), (73, True)])
def test_validate_commit_message_line_length_boundary(length: int, has_error: bool) -> None:
    errors = CHECKER.validate_message(
        VALID_MESSAGE.replace(
            "Keep task ownership tied to the worker's active lease.", "x" * length
        ),
        label="Commit 1",
    )

    line_errors = [error for error in errors if "line 5 exceeds" in error]
    assert bool(line_errors) is has_error
    if has_error:
        assert "line 5 exceeds 72 characters (73)" in line_errors[0]


def test_validate_accepts_crlf_commit_message() -> None:
    assert (
        CHECKER.validate_message(
            VALID_MESSAGE.replace("\n", "\r\n"),
            label="Commit 1",
        )
        == []
    )


def test_validate_reads_full_commit_messages_from_json_file(tmp_path: Path) -> None:
    messages = tmp_path / "messages.json"
    messages.write_text(__import__("json").dumps([VALID_MESSAGE]), encoding="utf-8")

    assert (
        CHECKER.validate(
            title="fix(worker): preserve task ownership",
            commits=[],
            commit_range=None,
            commit_json_file=str(messages),
        )
        == []
    )


def test_validate_reads_full_commit_message_from_file(tmp_path: Path) -> None:
    message = tmp_path / "message.txt"
    message.write_text(VALID_MESSAGE, encoding="utf-8")

    assert (
        CHECKER.validate(
            title="fix(worker): preserve task ownership",
            commits=[],
            commit_range=None,
            commit_file=str(message),
        )
        == []
    )


def test_validate_reads_full_commit_messages_from_git_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CHECKER, "_messages_from_git", lambda commit_range: [VALID_MESSAGE])

    assert (
        CHECKER.validate(
            title="fix(worker): preserve task ownership",
            commits=[],
            commit_range="origin/main..HEAD",
        )
        == []
    )


def test_tracked_template_renders_as_a_valid_commit_message() -> None:
    rendered = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("<type>[optional scope][!]: <imperative summary>", "ci: enforce polished history")
        .replace("<Describe the concrete durable change.>", "Require structured commit sections.")
        .replace(
            "<Explain the problem, invariant, or outcome that motivates it.>",
            "Keep rebase-merged history useful after development ends.",
        )
        .replace("<command>", "uv run pytest")
        .replace("<result>", "passed")
    )
    rendered = "\n".join(line for line in rendered.splitlines() if not line.startswith(";"))

    assert CHECKER.validate_message(rendered, label="Commit 1") == []
