"""Coverage-debt report, classification, and tracker policy tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.coverage_debt import (
    REPORT_COMMENT_MARKER,
    TRACKER_MARKER,
    CoverageDebtError,
    _parse_tracker_state,
    build_report,
    prepare_output_directory,
    render_markdown,
    update_tracker,
)

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "coverage-debt.yml"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _coverage_file(
    *, statements: int, covered: int, missing: list[int], percent: str
) -> dict[str, object]:
    return {
        "executed_lines": list(range(1, covered + 1)),
        "missing_lines": missing,
        "excluded_lines": [],
        "summary": {
            "covered_lines": covered,
            "num_statements": statements,
            "percent_covered": float(percent),
            "percent_covered_display": percent,
            "missing_lines": len(missing),
            "excluded_lines": 0,
        },
    }


def _report(
    source_commit: str = "a" * 40,
    *,
    statements: int = 100,
    covered: int = 96,
) -> dict[str, Any]:
    missed = statements - covered
    percent = f"{covered / statements * 100:.2f}" if statements else "100.00"
    return {
        "schema_version": 1,
        "metric": "line",
        "source_commit": source_commit,
        "central_configuration": {
            "path": "pyproject.toml",
            "precision": 2,
            "fail_under": 95,
        },
        "totals": {
            "statements": statements,
            "covered_lines": covered,
            "missed_lines": missed,
            "coverage_percent": percent,
        },
        "files": [],
    }


class FakeTrackerApi:
    def __init__(
        self,
        *,
        issues: list[dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issues = issues or [{"number": 122, "body": TRACKER_MARKER}]
        self.comments = comments or []
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def paginate(self, path: str) -> list[dict[str, Any]]:
        if path.endswith("/issues?state=all"):
            return list(self.issues)
        if path.endswith("/issues/122/comments"):
            return list(self.comments)
        raise AssertionError(f"unexpected pagination path: {path}")

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> object:
        self.requests.append((method, path, payload))
        assert payload is not None
        body = payload["body"]
        assert isinstance(body, str)
        if method == "POST":
            assert path.endswith("/issues/122/comments")
            self.comments.append(
                {"id": 501, "body": body, "user": {"login": "github-actions[bot]"}}
            )
            return self.comments[-1]
        if method == "PATCH":
            assert path.endswith("/issues/comments/501")
            self.comments[0]["body"] = body
            return self.comments[0]
        raise AssertionError(f"unexpected request: {method} {path}")


def test_prepare_output_removes_only_owned_stale_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "coverage-debt"
    output.mkdir()
    owned = ["coverage.py.json", "coverage-debt.json", "coverage-debt.md"]
    for name in owned:
        (output / name).write_text("stale", encoding="utf-8")
        (output / f".{name}.pending").write_text("partial", encoding="utf-8")
    unrelated = output / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    prepare_output_directory(output)

    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert all(not (output / name).exists() for name in owned)
    assert all(not (output / f".{name}.pending").exists() for name in owned)


def test_report_records_exact_sorted_ranges_and_review_classifications(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.json"
    classifications_path = tmp_path / "classifications.json"
    pyproject_path = tmp_path / "pyproject.toml"
    _write_json(
        coverage_path,
        {
            "meta": {"branch_coverage": False},
            "files": {
                "src\\django_ray\\small.py": _coverage_file(
                    statements=5, covered=4, missing=[9], percent="80.00"
                ),
                "src/django_ray/larger.py": _coverage_file(
                    statements=8, covered=4, missing=[2, 3, 4, 8], percent="50.00"
                ),
            },
            "totals": {
                "covered_lines": 8,
                "num_statements": 13,
                "percent_covered_display": "61.54",
                "missing_lines": 5,
            },
        },
    )
    _write_json(
        classifications_path,
        {
            "schema_version": 1,
            "files": {
                "src/django_ray/larger.py": {
                    "default": {
                        "category": "testable-behavior",
                        "rationale": "Assert the deterministic contract.",
                    },
                    "overrides": [
                        {
                            "ranges": ["3-4"],
                            "category": "environment-specific",
                            "rationale": "Exercise the matching runtime.",
                        }
                    ],
                },
                "src/django_ray/small.py": {
                    "default": {
                        "category": "defensive-invariant",
                        "rationale": "Prove or remove the guard.",
                    }
                },
            },
        },
    )
    pyproject_path.write_text(
        "[tool.coverage.report]\nfail_under = 95\nprecision = 2\n", encoding="utf-8"
    )

    report = build_report(
        coverage_path,
        classifications_path,
        pyproject_path,
        "a" * 40,
    )

    assert report["metric"] == "line"
    assert report["totals"] == {
        "statements": 13,
        "covered_lines": 8,
        "missed_lines": 5,
        "coverage_percent": "61.54",
    }
    assert [file_record["path"] for file_record in report["files"]] == [
        "src/django_ray/larger.py",
        "src/django_ray/small.py",
    ]
    assert [
        (
            uncovered["display"],
            uncovered["classification"]["category"],
        )
        for uncovered in report["files"][0]["uncovered_ranges"]
    ] == [
        ("2", "testable-behavior"),
        ("3-4", "environment-specific"),
        ("8", "testable-behavior"),
    ]
    markdown = render_markdown(report)
    assert "line coverage" in markdown
    assert "Branch coverage is a separate follow-up" in markdown
    assert markdown.index("larger.py") < markdown.index("small.py")


@pytest.mark.parametrize(
    ("branch_coverage", "classify_small", "message"),
    [
        (True, True, "line coverage only"),
        (False, False, "has no review-policy classification"),
    ],
)
def test_report_fails_closed_for_wrong_metric_or_unclassified_debt(
    tmp_path: Path,
    branch_coverage: bool,
    classify_small: bool,
    message: str,
) -> None:
    coverage_path = tmp_path / "coverage.json"
    classifications_path = tmp_path / "classifications.json"
    pyproject_path = tmp_path / "pyproject.toml"
    _write_json(
        coverage_path,
        {
            "meta": {"branch_coverage": branch_coverage},
            "files": {
                "src/django_ray/small.py": _coverage_file(
                    statements=1, covered=0, missing=[1], percent="0.00"
                )
            },
            "totals": {
                "covered_lines": 0,
                "num_statements": 1,
                "percent_covered_display": "0.00",
                "missing_lines": 1,
            },
        },
    )
    files = (
        {
            "src/django_ray/small.py": {
                "default": {
                    "category": "testable-behavior",
                    "rationale": "Assert the contract.",
                }
            }
        }
        if classify_small
        else {}
    )
    _write_json(classifications_path, {"schema_version": 1, "files": files})
    pyproject_path.write_text(
        "[tool.coverage.report]\nfail_under = 95\nprecision = 2\n", encoding="utf-8"
    )

    with pytest.raises(CoverageDebtError, match=message):
        build_report(coverage_path, classifications_path, pyproject_path, "a" * 40)


def test_identical_runs_reuse_one_bot_comment_and_seed_all_measurements() -> None:
    api = FakeTrackerApi()
    report = _report()

    assert update_tracker(api, "dariuszpanas/django-ray", report) == "created"
    first_body = api.comments[0]["body"]
    assert update_tracker(api, "dariuszpanas/django-ray", report) == "updated"

    assert len(api.comments) == 1
    assert [request[0] for request in api.requests] == ["POST", "PATCH"]
    assert api.comments[0]["body"] == first_body
    state = _parse_tracker_state(api.comments[0]["body"])
    assert state["current"] == state["previous"] == state["best"]
    assert api.comments[0]["body"].count(REPORT_COMMENT_MARKER) == 1


def test_tracker_moves_current_to_previous_and_retains_exact_high_water() -> None:
    api = FakeTrackerApi()
    first = _report("a" * 40, covered=97)
    lower = _report("b" * 40, covered=96)

    update_tracker(api, "dariuszpanas/django-ray", first)
    update_tracker(api, "dariuszpanas/django-ray", lower)

    state = _parse_tracker_state(api.comments[0]["body"])
    assert state["current"].source_commit == "b" * 40
    assert state["previous"].source_commit == "a" * 40
    assert state["best"].source_commit == "a" * 40
    assert "High water" in api.comments[0]["body"]


def test_tracker_refuses_duplicate_issue_markers_before_any_write() -> None:
    api = FakeTrackerApi(
        issues=[
            {"number": 122, "body": TRACKER_MARKER},
            {"number": 123, "body": TRACKER_MARKER},
        ]
    )

    with pytest.raises(CoverageDebtError, match="exactly one coverage-debt tracker marker"):
        update_tracker(api, "dariuszpanas/django-ray", _report())

    assert api.requests == []


def test_tracker_refuses_duplicate_or_non_bot_report_comments() -> None:
    duplicate_api = FakeTrackerApi(
        comments=[
            {"id": 501, "body": REPORT_COMMENT_MARKER, "user": {"login": "github-actions[bot]"}},
            {"id": 502, "body": REPORT_COMMENT_MARKER, "user": {"login": "github-actions[bot]"}},
        ]
    )
    with pytest.raises(CoverageDebtError, match="multiple coverage-debt latest-report markers"):
        update_tracker(duplicate_api, "dariuszpanas/django-ray", _report())
    assert duplicate_api.requests == []

    human_api = FakeTrackerApi(
        comments=[{"id": 501, "body": REPORT_COMMENT_MARKER, "user": {"login": "maintainer"}}]
    )
    with pytest.raises(CoverageDebtError, match="not owned by the expected bot"):
        update_tracker(human_api, "dariuszpanas/django-ray", _report())
    assert human_api.requests == []


def test_monthly_workflow_and_make_target_preserve_coverage_policy() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    events = workflow["on"]
    cron = events["schedule"][0]["cron"]
    permissions = workflow["permissions"]
    job = workflow["jobs"]["report"]
    steps = job["steps"]

    assert cron.split()[2] == "1"
    assert "workflow_dispatch" in events
    assert permissions == {"contents": "read", "issues": "write"}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["env"]["COVERAGE_DEBT_SOURCE_COMMIT"] == "${{ github.sha }}"
    assert any(step.get("run") == "uv python install 3.12" for step in steps)
    assert any("make coverage-debt" in step.get("run", "") for step in steps)
    assert any("GITHUB_STEP_SUMMARY" in step.get("run", "") for step in steps)
    upload = next(step for step in steps if step.get("name") == "Upload coverage-debt evidence")
    assert "coverage-debt.json" in upload["with"]["path"]
    assert "coverage-debt.md" in upload["with"]["path"]
    update = next(
        step for step in steps if step.get("name") == "Update the marked coverage-debt tracker"
    )
    assert update["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("coverage-debt:\n", maxsplit=1)[1].split(
        "\n# Run formatting", maxsplit=1
    )[0]
    assert 'pytest -m "not live_cluster"' in target
    assert "not real_ray" not in target
    assert "--cov-config=pyproject.toml" in target
    assert "--fail-under=$(COVERAGE_WORKER_MIN)" in target
    assert "--fail-under=$(COVERAGE_RAY_JOB_MIN)" in target
    assert "COVERAGE_GLOBAL_MIN ?= 95" in makefile
    assert "COVERAGE_WORKER_MIN ?= 90" in makefile
    assert "COVERAGE_RAY_JOB_MIN ?= 90" in makefile
    assert "COVERAGE_TESTPROJECT_MIN ?= 80" in makefile

    docs = (ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
    assert "line coverage only" in docs
    assert "separate follow-up" in docs
    assert all(
        label in docs
        for label in (
            "Testable behavior",
            "Environment-specific",
            "Upstream/native constraint",
            "Defensive invariant",
            "Dead or non-behavioral code",
        )
    )
