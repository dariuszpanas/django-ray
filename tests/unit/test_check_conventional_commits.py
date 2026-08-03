from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_conventional_commits.py"
TEMPLATE = Path(__file__).parents[2] / ".gitmessage"
WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "commit-messages.yml"
AGENT_GUIDANCE = Path(__file__).parents[2] / "AGENTS.md"
CONTRIBUTING = Path(__file__).parents[2] / "CONTRIBUTING.md"
CONTRIBUTING_DOCS = Path(__file__).parents[2] / "docs" / "contributing.md"
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

DEPENDABOT_MESSAGE = """chore(ci): bump example/action from 1.0.0 to 1.0.1

Bumps [example/action](https://github.com/example/action) from 1.0.0 to 1.0.1.
- [Release notes](https://github.com/example/action/releases)
- [Commits](https://github.com/example/action/compare/1111111111111111111111111111111111111111...2222222222222222222222222222222222222222)

---
updated-dependencies:
- dependency-name: example/action
  dependency-version: 1.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

DEPENDABOT_GROUP_MESSAGE = """chore(deps): bump the python group with 2 updates

Bumps the python group with 2 updates:

| Package | From | To |
| --- | --- | --- |
| [example-one](https://example.com/one) | `1.0.0` | `1.0.1` |
| [example-two](https://example.com/two) | `2.0.0` | `2.1.0` |

---
updated-dependencies:
- dependency-name: example-one
  dependency-version: 1.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: python
- dependency-name: example-two
  dependency-version: 2.1.0
  dependency-type: direct:development
  update-type: version-update:semver-minor
  dependency-group: python
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

DEPENDABOT_DIRECTORY_GROUP_MESSAGE = (
    DEPENDABOT_GROUP_MESSAGE.replace(
        "chore(deps): bump the python group with 2 updates",
        "chore(deps): bump the python-minor-patch group across 1 directory with 2 updates",
        1,
    )
    .replace(
        "Bumps the python group with 2 updates:",
        "Bumps the python-minor-patch group with 2 updates in the / directory:",
        1,
    )
    .replace("dependency-group: python", "dependency-group: python-minor-patch")
)


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


def test_validate_rejects_header_without_descriptive_body() -> None:
    errors = CHECKER.validate_message("fix: close the lease", label="Commit 1")

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_rejects_body_without_enough_context() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\nPrevent duplicate lease cleanup.",
        label="Commit 1",
    )

    assert any("8 or more prose words" in error for error in errors)


def test_validate_accepts_meaningful_unstructured_body() -> None:
    message = (
        "fix: close the lease\n\n"
        "Keep active lease ownership to stop duplicate cleanup during recovery.\n\n"
        "Focused lease recovery tests passed."
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_treats_structured_sections_as_optional_guidance() -> None:
    message = """fix: close the lease

## Rationale

Preserve active lease ownership so cleanup cannot race worker recovery.

Validation: not run because this fixture checks optional headings only.
"""

    assert CHECKER.validate_message(message, label="Commit 1") == []


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ("chore(ci): bump example/action from 1.0.0 to 1.0.1", DEPENDABOT_MESSAGE),
        ("chore(deps): bump the python group with 2 updates", DEPENDABOT_GROUP_MESSAGE),
        (
            "chore(deps): bump the python-minor-patch group across 1 directory with 2 updates",
            DEPENDABOT_DIRECTORY_GROUP_MESSAGE,
        ),
    ],
)
def test_validate_accepts_descriptive_dependabot_generated_body(title: str, message: str) -> None:
    assert CHECKER.validate(title=title, commits=[message], commit_range=None) == []


def test_validate_does_not_exempt_noncanonical_generated_header() -> None:
    message = DEPENDABOT_MESSAGE.replace(
        "chore(ci): bump example/action from 1.0.0 to 1.0.1",
        "chore(ci): bump other/action from 1.0.0 to 1.0.1",
        1,
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("must record validation evidence" in error for error in errors)


@pytest.mark.parametrize(
    "message",
    [
        DEPENDABOT_DIRECTORY_GROUP_MESSAGE.replace("with 2 updates", "with 3 updates", 1),
        DEPENDABOT_DIRECTORY_GROUP_MESSAGE.replace(
            "python-minor-patch group", "python-minor-patch-other group", 1
        ),
        DEPENDABOT_DIRECTORY_GROUP_MESSAGE.replace(
            "Signed-off-by: dependabot[bot] <support@github.com>",
            "Signed-off-by: unrecognized[bot] <support@example.com>",
        ),
    ],
    ids=["count", "group", "metadata"],
)
def test_validate_generated_header_exemption_requires_matching_metadata(message: str) -> None:
    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("line 1 exceeds 72 characters" in error for error in errors)


def test_validate_dependabot_message_still_requires_valid_header() -> None:
    errors = CHECKER.validate(
        title="chore(ci): bump example/action from 1.0.0 to 1.0.1",
        commits=[DEPENDABOT_MESSAGE.replace("chore(ci):", "dependencies:", 1)],
        commit_range=None,
    )

    assert any("uses unsupported type 'dependencies'" in error for error in errors)


def test_validate_dependabot_message_still_requires_valid_pr_title() -> None:
    errors = CHECKER.validate(
        title="Dependabot update",
        commits=[DEPENDABOT_MESSAGE],
        commit_range=None,
    )

    assert any("PR title is not a Conventional Commit header" in error for error in errors)


@pytest.mark.parametrize(
    "placeholder",
    [
        "WIP",
        "**WIP**",
        "iteration 3",
        "Address review feedback.",
        "Updates.",
        "Fix CI.",
        "<describe the durable change>",
        "[describe the durable change]",
        "...",
    ],
)
def test_validate_rejects_placeholder_body_content(placeholder: str) -> None:
    message = (
        "fix: close the lease\n\n"
        f"{placeholder}\n\n"
        "Preserve active lease ownership during deterministic worker recovery."
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("contains placeholder content" in error for error in errors)


def test_validate_rejects_html_comment_only_body() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\n<!-- describe the durable change here -->",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_rejects_unexpanded_tracked_template_tokens() -> None:
    rendered = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("<type>[optional scope][!]: <imperative summary>", "fix: close the lease")
        .replace(
            "<Describe the observable durable change and why it is needed.>",
            "Prevent duplicate cleanup during deterministic worker recovery.",
        )
        .replace(
            "<Describe important invariants, non-goals, and compatibility impact.>",
            "Preserve active lease ownership across supported workers.",
        )
        .replace(
            "<Describe migration, rollout, or activation details when applicable.>",
            "No migration or activation change is required.",
        )
        .replace(
            "<Describe repository-local ADRs, modules, tests, or docs to open.>",
            "Inspect tests/unit/test_worker.py for the recovery contract.",
        )
    )
    rendered = "\n".join(line for line in rendered.splitlines() if not line.startswith(";"))

    errors = CHECKER.validate_message(rendered, label="Commit 1")

    assert any("<command>: <result>" in error for error in errors)


def test_validate_rejects_development_placeholder_split_across_lines() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\nAddress review\nfeedback.",
        label="Commit 1",
    )

    assert any("contains placeholder content" in error for error in errors)


def test_validate_counts_plain_validation_prose_as_context() -> None:
    message = """fix: close the lease

Prevent duplicate cleanup while preserving ownership during recovery.

Tests: focused lease recovery checks completed successfully.
"""

    assert CHECKER.validate_message(message, label="Commit 1") == []


@pytest.mark.parametrize(
    "validation",
    [
        "Focused lease recovery tests passed.",
        "Focused lease recovery suite: 12 passed.",
        "`uv run pytest tests/unit/test_worker.py -q`: 12 passed.",
        "`uv run pytest`: 114 passed, 2 skipped in 0.46s.",
        "Focused tests: 114 passed, 2 skipped.",
        "CI Gate: passed.",
        "Commit Messages: passed.",
        "Previous-release compatibility tests passed.",
        "git diff --check: clean.",
        "Tests: focused lease recovery checks completed\nsuccessfully.",
    ],
)
def test_validate_accepts_specific_validation_results(validation: str) -> None:
    message = (
        "fix: close the lease\n\n"
        "Prevent duplicate cleanup while preserving ownership during recovery.\n\n"
        f"{validation}"
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


@pytest.mark.parametrize(
    "validation",
    [
        "Validation: Ray cluster smoke completed successfully.",
        "## Validation\n\nRay cluster smoke completed successfully.",
        "Validation: `kubectl auth can-i get pods`: passed.",
        "Validation: `uv run pytest`: 114 passed, 2 xfailed.",
        "Validation: `uv run ruff format`: 2 files left unchanged.",
        "Validation: `custom smoke command`: exit code 0.",
    ],
)
def test_validate_accepts_repo_specific_results_in_explicit_context(validation: str) -> None:
    message = (
        "fix: close the lease\n\n"
        "Prevent duplicate cleanup while preserving ownership during recovery.\n\n"
        f"{validation}"
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


@pytest.mark.parametrize(
    "validation",
    [
        "Validation: not run because this changes documentation text only.",
        "## Validation\n\nNot run: documentation-only wording change.",
        "Tests were not run because this changes explanatory comments only.",
        "Focused policy tests skipped because this changes documentation only.",
        "uv run pytest skipped because only prose changed.",
        "Validation: not run because this changes only\ndocumentation wording.",
    ],
)
def test_validate_accepts_explicit_not_run_reasons(validation: str) -> None:
    message = (
        "docs: clarify lease ownership\n\n"
        "Explain durable lease ownership for operators investigating recovery.\n\n"
        f"{validation}"
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


@pytest.mark.parametrize(
    "validation",
    [
        "",
        "Validation: not run.",
        "Tests: skipped.",
        "Validation: not run because N/A.",
        "Validation: not run because reasons.",
        "Validation: not run because it was not run.",
        "Focused policy tests skipped.",
        "uv run pytest skipped.",
        "uv run make ci",
        "uv run pytest tests/passed",
        "uv run ruff check tests/green",
        "uv run make ci: pending CI",
        "uv run make ci: will run later",
        "uv run make ci: waiting for CI",
        "uv run make ci: unknown",
        "uv run make ci: not yet",
        "uv run make ci: result pending",
        "uv run make ci: not tested",
        "uv run make ci: untested",
        "uv run make ci: did not test",
        "uv run make ci: not checked",
        "uv run make ci: not performed",
        "uv run make ci: later",
        "uv run make ci: to be run",
        "uv run make ci: queued",
        "uv run make ci: in progress",
        "uv run make ci: awaiting results",
        "uv run make ci: no results",
        "uv run make ci: not available",
        "uv run make ci: deferred",
        "uv run make ci: running now",
        "uv run pytest: details recorded elsewhere.",
        "## Validation",
        "All checks passed.",
        "Windows: passed.",
    ],
)
def test_validate_rejects_missing_or_generic_validation_evidence(validation: str) -> None:
    message = (
        "fix: close the lease\n\n"
        "Prevent duplicate cleanup while preserving ownership during recovery.\n\n"
        f"{validation}"
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("must record validation evidence" in error for error in errors)


@pytest.mark.parametrize(
    "validation",
    [
        "Focused lease recovery tests should pass.",
        "Focused lease recovery tests should have passed.",
        "Tests reportedly passed.",
        "Tests allegedly passed.",
        "Tests perhaps passed.",
        "Tests may already have passed.",
        "Build could possibly have passed.",
        "The documentation explains why recovery tests may fail.",
        "The documentation explains why recovery tests failed.",
        "This documentation records that recovery tests passed.",
        "The prior release recovery tests passed.",
        "The migration completed successfully.",
        "The bug meant recovery tests were not run because CI ignored them.",
        "This change documents why tests were not run because the fixture is static.",
        "Focused recovery tests are important to success.",
    ],
)
def test_validate_rejects_modal_or_explanatory_result_prose(validation: str) -> None:
    message = (
        "fix: close the lease\n\n"
        "Prevent duplicate cleanup while preserving ownership during recovery.\n\n"
        f"{validation}"
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("must record validation evidence" in error for error in errors)


def test_validate_ignores_validation_examples_and_comments() -> None:
    message = """docs: explain validation syntax

Document portable validation records without claiming example commands ran.

```text
uv run make ci: passed
```

> Focused policy tests passed.

    uv run pytest: passed

<!-- Focused documentation checks passed. -->
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("must record validation evidence" in error for error in errors)


@pytest.mark.parametrize(
    "context",
    [
        "Make active lease ownership durable across worker restarts and retries.",
        "Ruff configuration keeps generated project files out of normal checks.",
        "Checks preserve ownership while workers retry interrupted tasks safely.",
    ],
)
def test_validate_counts_command_like_words_as_prose(context: str) -> None:
    message = f"fix: preserve task ownership\n\n{context}\n\nFocused message-policy tests passed."

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_rejects_padded_development_only_prose() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\n"
        "Address review feedback and update tests for supported environments.",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_rejects_padded_ci_fix_prose() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\n"
        "Fix CI failures and update tests across all supported environments.",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


@pytest.mark.parametrize(
    "context",
    [
        "Replace the TODO marker with durable ownership during worker recovery.",
        "Reject placeholder tokens only when they replace durable change context.",
    ],
)
def test_validate_accepts_descriptive_placeholder_discussion(context: str) -> None:
    message = (
        f"fix: preserve task ownership\n\n{context}\n\nFocused placeholder-policy tests passed."
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


@pytest.mark.parametrize(
    "prefix",
    [
        "TODO: add meaningful historical context before this commit is merged.",
        "WIP: finish durable worker recovery tests before merging this change.",
        "TBD - explain durable ownership behavior across every worker backend.",
    ],
)
def test_validate_rejects_placeholder_prefix_body(prefix: str) -> None:
    errors = CHECKER.validate_message(f"fix: preserve task ownership\n\n{prefix}", label="Commit 1")

    assert any("contains placeholder content" in error for error in errors)


@pytest.mark.parametrize("prefix", ["WIP preserve ownership", "TODO: preserve ownership"])
def test_validate_rejects_placeholder_prefix_header(prefix: str) -> None:
    error = CHECKER.validate_header(f"fix: {prefix}", label="Commit 1")

    assert error is not None
    assert "development placeholder" in error


def test_validate_rejects_placeholder_header_summary() -> None:
    errors = CHECKER.validate_message(
        VALID_MESSAGE.replace("preserve task ownership", "iteration 3"),
        label="Commit 1",
    )

    assert any("uses a development placeholder" in error for error in errors)


def test_validate_rejects_summary_that_only_repeats_header() -> None:
    errors = CHECKER.validate_message(
        "fix: preserve task ownership\n\nPreserve task ownership.",
        label="Commit 1",
    )

    assert any("repeats the header summary" in error for error in errors)


def test_validate_rejects_header_summary_repeated_twice() -> None:
    message = (
        "fix: preserve active lease ownership\n\n"
        "Preserve active lease ownership.\n"
        "Preserve active lease ownership."
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("repeats the header summary" in error for error in errors)


def test_validate_rejects_wrapped_header_summary_repetition() -> None:
    message = (
        "fix: preserve active lease ownership across worker recovery\n\n"
        "Preserve active lease ownership\n"
        "across worker recovery."
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("repeats the header summary" in error for error in errors)


def test_validate_rejects_identifier_header_summary_repetition() -> None:
    message = (
        "fix: preserve execution_generation during worker recovery\n\n"
        "Preserve execution_generation during worker recovery.\n"
        "Preserve execution_generation during worker recovery."
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("repeats the header summary" in error for error in errors)


def test_validate_rejects_repeated_summary_with_validation_only_body() -> None:
    message = """fix: preserve active lease ownership

## Summary

Preserve active lease ownership.

## Validation

All checks passed successfully on the supported test matrix.
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("repeats the header summary" in error for error in errors)


def test_validate_rejects_validation_only_body() -> None:
    message = """fix: close the lease

## Validation

All checks passed successfully on the supported test matrix.
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_rejects_unstructured_validation_result_only_body() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\nAll checks passed successfully on supported environments.",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


@pytest.mark.parametrize(
    "body",
    [
        "Focused lease tests across all supported workers and backends passed.",
        "Tests were not run because only explanatory documentation changed here.",
    ],
)
def test_validate_does_not_count_specific_evidence_as_change_context(body: str) -> None:
    errors = CHECKER.validate_message(
        f"fix: close the lease\n\n{body}",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_preserves_context_before_evidence_in_the_same_block() -> None:
    message = (
        "fix: close the lease\n\n"
        "Prevent duplicate cleanup while preserving ownership\n"
        "during recovery. Focused lease recovery tests passed."
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_rejects_validation_matrix_only_body() -> None:
    message = """fix: close the lease

Windows: passed.
Linux: passed.
Python: passed.
Documentation: passed.
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_counts_status_word_inside_descriptive_prose() -> None:
    message = (
        "fix: close the lease\n\n"
        "Database: success depends on durable ownership across worker recovery.\n\n"
        "Focused database recovery tests passed."
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_recognizes_decorated_validation_heading() -> None:
    message = """fix: close the lease

## **Validation**

Recovery checks passed across every supported worker backend.
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("body must contain meaningful context" in error for error in errors)


@pytest.mark.parametrize("level", [1, 3, 6])
def test_validate_does_not_count_atx_heading_as_context(level: int) -> None:
    heading = "#" * level
    errors = CHECKER.validate_message(
        f"fix: close the lease\n\n{heading} This heading alone has more than eight words",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_does_not_count_setext_heading_as_context() -> None:
    errors = CHECKER.validate_message(
        "fix: close the lease\n\n"
        "This heading alone contains more than eight ordinary prose words\n"
        "--------------------------------------------------------------",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_does_not_count_setext_validation_section() -> None:
    message = """fix: close the lease

Validation
==========

All checks passed successfully on every supported environment.
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_does_not_count_numeric_tokens_as_context() -> None:
    errors = CHECKER.validate_message(
        "chore(deps): update versions\n\n1 2 3 4 5 6 7 8",
        label="Commit 1",
    )

    assert any("body must contain meaningful context" in error for error in errors)


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
        assert "line 5 exceeds 72 characters (73 visible characters)" in line_errors[0]


def test_validate_measures_visible_prose_without_markdown_destination() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep the [dependency comparison](https://example.com/"
        + "x" * 120
        + ") available for future release history.",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_accepts_long_raw_url_without_ignoring_surrounding_prose() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency comparisons available for future release history at "
        "https://example.com/" + "x" * 120,
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_rejects_long_markdown_link_label() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        f"[{'x' * 73}](https://example.com/comparison)",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("line 5 exceeds 72 characters" in error for error in errors)


def test_validate_rejects_long_prose_around_markdown_link() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        f"{'x' * 73} [comparison](https://example.com/comparison)",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any(
        "line 5 exceeds 72 characters" in error and "visible characters" in error
        for error in errors
    )


def test_validate_does_not_exempt_content_after_unvalidated_metadata_marker() -> None:
    message = (
        "fix: keep metadata visible\n\n"
        "This body explains why unvalidated metadata must remain visible in history.\n\n"
        "---\n" + "x" * 100
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("line 6 exceeds 72 characters" in error for error in errors)


def test_validate_does_not_wrap_markdown_table_rows() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency changes visible in generated history.\n\n"
        "| Dependency | Previous version | Updated version |\n"
        "| --- | --- | --- |\n"
        "| Dependency with a generated display name | Previous version | Updated version |",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_does_not_wrap_table_rows_without_outer_pipes() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency changes visible in generated history.\n\n"
        "Dependency | Previous version | Updated version\n"
        "--- | --- | ---\n"
        "Dependency with a generated display name | Previous version | Updated version",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_does_not_wrap_one_column_table_rows() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency changes visible in generated history.\n\n"
        "| Dependency |\n"
        "| --- |\n"
        f"| {'generated-dependency-name-' + 'x' * 80} |",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_does_not_split_escaped_table_pipes() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency changes visible in generated history.\n\n"
        "| Dependency | Detail |\n"
        "| --- | --- |\n"
        f"| generated \\| alias {'x' * 80} | current version |",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_does_not_require_body_table_column_count() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency changes visible in generated history.\n\n"
        "| Dependency | Previous | Updated |\n"
        "| --- | --- | --- |\n"
        f"| {'generated-dependency-name-' + 'x' * 80} |",
    )

    assert CHECKER.validate_message(message, label="Commit 1") == []


@pytest.mark.parametrize(
    "block_line",
    [
        "> quoted prose | " + "x" * 90,
        "- list prose | " + "x" * 90,
        "## Heading prose | " + "x" * 90,
        "    indented code | " + "x" * 90,
        "```text | " + "x" * 90,
    ],
)
def test_validate_stops_table_before_block_structure(block_line: str) -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency changes visible in generated history.\n\n"
        "| Dependency | Previous | Updated |\n"
        "| --- | --- | --- |\n"
        "| example | 1.0.0 | 1.0.1 |\n"
        f"{block_line}",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("exceeds 72 characters" in error for error in errors)


def test_validate_requires_matching_table_column_counts() -> None:
    message = VALID_MESSAGE.replace(
        "Keep task ownership tied to the worker's active lease.",
        "Keep dependency changes visible in generated history.\n\n"
        "Dependency with a generated display name | Previous version | Updated version\n"
        "--- | ---",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("exceeds 72 characters" in error for error in errors)


def test_validate_does_not_treat_pipe_wrapped_prose_as_a_table() -> None:
    message = (
        "fix: keep prose wrapping visible\n\n"
        "Explain why ordinary pipe-wrapped prose still belongs to readable history.\n\n"
        f"|{'x' * 100}|"
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("line 5 exceeds 72 characters" in error for error in errors)


def test_validate_does_not_exempt_unrecognized_generated_metadata() -> None:
    message = DEPENDABOT_MESSAGE.replace(
        "  update-type: version-update:semver-patch\n",
        f"  update-type: version-update:semver-patch\n  prose: {'x' * 100}\n",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("exceeds 72 characters" in error for error in errors)


def test_validate_rejects_generated_metadata_with_prose_value() -> None:
    message = DEPENDABOT_MESSAGE.replace(
        "  dependency-version: 1.0.1",
        "  dependency-version: this arbitrary prose must not bypass validation",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("unrecognized generated dependency metadata" in error for error in errors)


def test_validate_detects_placeholder_inside_invalid_generated_metadata() -> None:
    message = DEPENDABOT_MESSAGE.replace(
        "  dependency-version: 1.0.1", "  dependency-version: WIP pending"
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("unrecognized generated dependency metadata" in error for error in errors)


def test_validate_rejects_metadata_shape_without_markers() -> None:
    message = """chore(deps): update generated metadata

updated-dependencies:
- dependency-name: example
  dependency-version: 1.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("unrecognized generated dependency metadata" in error for error in errors)


def test_validate_ignores_generated_metadata_inside_fenced_example() -> None:
    message = """docs: explain generated dependency metadata

Document dependency metadata without changing durable commit behavior.

```yaml
---
updated-dependencies:
- dependency-name: example
  dependency-version: 1.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
...
```

Documentation policy tests passed.
"""

    assert CHECKER.validate_message(message, label="Commit 1") == []


def test_validate_rejects_malformed_metadata_fields_without_markers() -> None:
    message = """chore(deps): update generated metadata

- dependency-name: arbitrary dependency prose
  dependency-version: unfinished version details
  dependency-type: direct production dependency
  update-type: pending semantic update type
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("unrecognized generated dependency metadata" in error for error in errors)


def test_validate_accepts_dependency_named_todo() -> None:
    message = DEPENDABOT_MESSAGE.replace("example/action", "todo")
    title = "chore(deps): bump todo from 1.0.0 to 1.0.1"
    message = message.replace("chore(ci): bump todo from 1.0.0 to 1.0.1", title, 1).replace(
        "todo-with-a-generated-name-that-does-not-wrap-cleanly", "todo"
    )

    assert CHECKER.validate(title=title, commits=[message], commit_range=None) == []


def test_validate_accepts_short_generated_dependency_context() -> None:
    title = "chore(deps): bump ray from 2.0.0 to 2.0.1"
    message = """chore(deps): bump ray from 2.0.0 to 2.0.1

Bumps [ray](https://github.com/ray-project/ray) from 2.0.0 to 2.0.1.
- [Release notes](https://github.com/ray-project/ray/releases)
- [Commits](https://github.com/ray-project/ray/compare/2.0.0...2.0.1)

---
updated-dependencies:
- dependency-name: ray
  dependency-version: 2.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

    assert CHECKER.validate(title=title, commits=[message], commit_range=None) == []


def test_validate_rejects_incomplete_short_generated_dependency_context() -> None:
    message = DEPENDABOT_MESSAGE.replace(
        "Bumps [example/action](https://github.com/example/action) from 1.0.0 to 1.0.1.",
        "Bumps things.",
    )

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("body must contain meaningful context" in error for error in errors)


def test_validate_does_not_count_generated_metadata_as_description() -> None:
    message = """chore(deps): update generated metadata

---
updated-dependencies:
- dependency-name: example
  dependency-version: 1.0.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

    errors = CHECKER.validate_message(message, label="Commit 1")

    assert any("body must contain meaningful context" in error for error in errors)


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


@pytest.mark.parametrize(
    ("context", "uses_title_only"),
    [
        (
            {
                "author_login": "dependabot[bot]",
                "head_ref": "dependabot/uv/python-minor-patch",
                "head_repository": "owner/project",
                "repository": "owner/project",
            },
            True,
        ),
        (
            {
                "author_login": "contributor",
                "head_ref": "dependabot/uv/python-minor-patch",
                "head_repository": "owner/project",
                "repository": "owner/project",
            },
            False,
        ),
        (
            {
                "author_login": "dependabot[bot]",
                "head_ref": "dependabot/uv/python-minor-patch",
                "head_repository": "fork/project",
                "repository": "owner/project",
            },
            False,
        ),
        (
            {
                "author_login": "dependabot[bot]",
                "head_ref": "chore/dependency-update",
                "head_repository": "owner/project",
                "repository": "owner/project",
            },
            False,
        ),
        (
            {
                "author_login": "dependabot[bot]",
                "head_ref": "dependabot/uv/python-minor-patch",
                "head_repository": "",
                "repository": "",
            },
            False,
        ),
    ],
)
def test_pull_request_context_limits_title_only_validation_to_trusted_dependabot(
    context: dict[str, str],
    uses_title_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranges: list[str] = []

    def messages_from_git(commit_range: str) -> list[str]:
        ranges.append(commit_range)
        return ["Update dependencies without a Conventional Commit header"]

    monkeypatch.setattr(CHECKER, "_messages_from_git", messages_from_git)

    errors = CHECKER.validate_pull_request(
        title="chore(deps): update locked dependencies",
        commits=[],
        commit_range="origin/main..HEAD",
        **context,
    )

    if uses_title_only:
        assert errors == []
        assert ranges == []
    else:
        assert any("Commit 1 is not a Conventional Commit header" in error for error in errors)
        assert ranges == ["origin/main..HEAD"]


def test_trusted_dependabot_still_requires_a_valid_pr_title() -> None:
    errors = CHECKER.validate_pull_request(
        title="Update locked dependencies",
        commits=[],
        commit_range="unused",
        author_login="dependabot[bot]",
        head_ref="dependabot/uv/python-minor-patch",
        head_repository="owner/project",
        repository="owner/project",
    )

    assert any("PR title is not a Conventional Commit header" in error for error in errors)


def test_pull_request_context_must_be_complete(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        CHECKER.main(
            [
                "--title",
                "chore(deps): update locked dependencies",
                "--pr-author-login",
                "dependabot[bot]",
            ]
        )
        == 1
    )

    assert capsys.readouterr().err == (
        "::error::Pull request context requires --pr-author-login, --pr-head-ref, "
        "--pr-head-repository, and --repository together.\n"
    )


def test_tracked_template_renders_as_a_valid_commit_message() -> None:
    rendered = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("<type>[optional scope][!]: <imperative summary>", "ci: enforce polished history")
        .replace(
            "<Describe the observable durable change and why it is needed.>",
            "Keep rebase-merged history useful after development ends.",
        )
        .replace(
            "<Describe important invariants, non-goals, and compatibility impact.>",
            "Keep headings optional and preserve generated dependency messages.",
        )
        .replace(
            "<Describe migration, rollout, or activation details when applicable.>",
            "Apply the policy to new commits without rewriting old history.",
        )
        .replace(
            "<Describe repository-local ADRs, modules, tests, or docs to open.>",
            "Inspect scripts/check_conventional_commits.py and its unit tests.",
        )
        .replace("<command>", "uv run pytest")
        .replace("<result>", "passed")
    )
    rendered = "\n".join(line for line in rendered.splitlines() if not line.startswith(";"))

    assert CHECKER.validate_message(rendered, label="Commit 1") == []


def test_portable_history_guidance_distinguishes_commit_and_pr_surfaces() -> None:
    for path in (AGENT_GUIDANCE, CONTRIBUTING, CONTRIBUTING_DOCS):
        guidance = path.read_text(encoding="utf-8")
        normalized = " ".join(guidance.split())
        assert "portable, PR-grade change record" in normalized
        assert "one large atomic commit is valid" in normalized.casefold()
        assert "natural Markdown" in normalized
        assert "artificial hard wrapping" in normalized

    template = TEMPLATE.read_text(encoding="utf-8")
    normalized_template = " ".join(template.split())
    assert "Boundaries and rollout" in template
    assert "Investigation" in template
    assert "not run because <specific reason>" in template
    assert "natural Markdown without artificial hard wrapping" in normalized_template


def test_commit_workflow_scopes_dependabot_to_title_validation() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request_target:" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: read" not in workflow
    assert "gh api" not in workflow
    assert "ref: ${{ github.event.repository.default_branch }}" in workflow
    assert "PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "PR_AUTHOR_LOGIN: ${{ github.event.pull_request.user.login }}" in workflow
    assert "PR_HEAD_REF: ${{ github.event.pull_request.head.ref }}" in workflow
    assert "PR_HEAD_REPOSITORY: ${{ github.event.pull_request.head.repo.full_name }}" in workflow
    assert "REPOSITORY: ${{ github.repository }}" in workflow
    assert '"refs/pull/${PR_NUMBER}/head:${pr_ref}"' in workflow
    assert 'fetched_head="$(git rev-parse "$pr_ref")"' in workflow
    assert 'if [ "$fetched_head" != "$PR_HEAD_SHA" ]; then' in workflow
    assert 'git cat-file -e "${PR_BASE_SHA}^{commit}"' in workflow
    assert "--body-policy" not in workflow
    assert '--range "${PR_BASE_SHA}..${pr_ref}"' in workflow
    assert '--pr-author-login "$PR_AUTHOR_LOGIN"' in workflow
    assert '--pr-head-ref "$PR_HEAD_REF"' in workflow
    assert '--pr-head-repository "$PR_HEAD_REPOSITORY"' in workflow
    assert '--repository "$REPOSITORY"' in workflow
    assert "--title-only" not in workflow

    fetched_head = workflow.index('fetched_head="$(git rev-parse "$pr_ref")"')
    validation = workflow.index("python scripts/check_conventional_commits.py")
    assert fetched_head < validation
