"""Build and publish deterministic line-coverage debt reports."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

REPORT_SCHEMA_VERSION = 1
TRACKER_STATE_SCHEMA_VERSION = 1
TRACKER_MARKER = "<!-- django-ray:coverage-debt-tracker -->"
REPORT_COMMENT_MARKER = "<!-- django-ray:coverage-debt-latest-report -->"
STATE_START_MARKER = "<!-- django-ray:coverage-debt-state\n"
STATE_END_MARKER = "\n--><!-- /django-ray:coverage-debt-state -->"
MAX_COMMENT_BYTES = 64_000
ARTIFACT_NAMES = ("coverage.py.json", "coverage-debt.json", "coverage-debt.md")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CATEGORY_LABELS = {
    "testable-behavior": "Testable behavior",
    "environment-specific": "Environment-specific",
    "upstream-native-constraint": "Upstream/native constraint",
    "defensive-invariant": "Defensive invariant",
    "dead-or-non-behavioral-code": "Dead or non-behavioral code",
}


class CoverageDebtError(ValueError):
    """Raised when coverage evidence or tracker state is unsafe to publish."""


@dataclass(frozen=True)
class Classification:
    """Review-policy classification for one or more uncovered lines."""

    category: str
    label: str
    rationale: str


@dataclass(frozen=True)
class ClassificationOverride:
    """A classification that applies to an inclusive source-line span."""

    start: int
    end: int
    classification: Classification


@dataclass(frozen=True)
class FileClassification:
    """Default and range-specific classifications for one source file."""

    default: Classification
    overrides: tuple[ClassificationOverride, ...]

    def for_line(self, line: int) -> Classification:
        for override in self.overrides:
            if override.start <= line <= override.end:
                return override.classification
        return self.default


@dataclass(frozen=True)
class Measurement:
    """Exact line-coverage totals retained in the tracker history."""

    source_commit: str
    statements: int
    covered_lines: int
    missed_lines: int
    coverage_percent: str

    @classmethod
    def from_report(cls, report: dict[str, Any]) -> Measurement:
        if report.get("schema_version") != REPORT_SCHEMA_VERSION:
            raise CoverageDebtError("unsupported coverage-debt report schema")
        if report.get("metric") != "line":
            raise CoverageDebtError("coverage-debt reports must use the line metric")
        totals = _mapping(report.get("totals"), "report totals")
        return cls.from_mapping(
            {
                "source_commit": report.get("source_commit"),
                "statements": totals.get("statements"),
                "covered_lines": totals.get("covered_lines"),
                "missed_lines": totals.get("missed_lines"),
                "coverage_percent": totals.get("coverage_percent"),
            }
        )

    @classmethod
    def from_mapping(cls, value: object) -> Measurement:
        record = _mapping(value, "coverage measurement")
        source_commit = record.get("source_commit")
        coverage_percent = record.get("coverage_percent")
        if not isinstance(source_commit, str) or not COMMIT_RE.fullmatch(source_commit):
            raise CoverageDebtError("coverage measurement needs a full lowercase commit SHA")
        if not isinstance(coverage_percent, str) or not re.fullmatch(
            r"(?:100\.00|\d{1,2}\.\d{2})", coverage_percent
        ):
            raise CoverageDebtError("coverage measurement needs a two-decimal percentage")
        statements = _non_negative_int(record.get("statements"), "measurement statements")
        covered_lines = _non_negative_int(record.get("covered_lines"), "measurement covered_lines")
        missed_lines = _non_negative_int(record.get("missed_lines"), "measurement missed_lines")
        if covered_lines + missed_lines != statements:
            raise CoverageDebtError("coverage measurement totals do not add up")
        return cls(
            source_commit=source_commit,
            statements=statements,
            covered_lines=covered_lines,
            missed_lines=missed_lines,
            coverage_percent=coverage_percent,
        )


class TrackerApi(Protocol):
    """Small GitHub API surface used by the idempotent tracker updater."""

    def paginate(self, path: str) -> list[dict[str, Any]]: ...

    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object: ...


class GitHubApi:
    """Minimal authenticated GitHub REST client with bounded pagination."""

    def __init__(self, token: str, *, base_url: str = "https://api.github.com") -> None:
        if not token:
            raise CoverageDebtError("GITHUB_TOKEN is required to update the tracker")
        self._token = token
        self._base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> object:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "django-ray-coverage-debt",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                response_body = response.read()
        except urllib.error.HTTPError as error:
            raise CoverageDebtError(
                f"GitHub API {method} {path} failed with HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            raise CoverageDebtError(f"GitHub API {method} {path} failed: {error.reason}") from error
        if not response_body:
            return None
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as error:
            raise CoverageDebtError(f"GitHub API {method} {path} returned invalid JSON") from error

    def paginate(self, path: str) -> list[dict[str, Any]]:
        separator = "&" if "?" in path else "?"
        records: list[dict[str, Any]] = []
        for page in range(1, 101):
            response = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(response, list) or not all(
                isinstance(item, dict) for item in response
            ):
                raise CoverageDebtError(
                    f"GitHub API pagination returned an invalid page for {path}"
                )
            typed_response = cast(list[dict[str, Any]], response)
            records.extend(typed_response)
            if len(typed_response) < 100:
                return records
        raise CoverageDebtError(f"GitHub API pagination exceeded 100 pages for {path}")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CoverageDebtError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoverageDebtError(f"{label} must be a non-negative integer")
    return value


def _classification(value: object, label: str) -> Classification:
    record = _mapping(value, label)
    category = record.get("category")
    rationale = record.get("rationale")
    if not isinstance(category, str) or category not in CATEGORY_LABELS:
        allowed = ", ".join(sorted(CATEGORY_LABELS))
        raise CoverageDebtError(f"{label} category must be one of: {allowed}")
    if not isinstance(rationale, str) or not rationale.strip():
        raise CoverageDebtError(f"{label} needs a non-empty rationale")
    return Classification(
        category=category,
        label=CATEGORY_LABELS[category],
        rationale=" ".join(rationale.split()),
    )


def _line_span(value: object, label: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d*(?:-[1-9]\d*)?", value):
        raise CoverageDebtError(f"{label} must be a positive line or inclusive range")
    start_text, separator, end_text = value.partition("-")
    start = int(start_text)
    end = int(end_text) if separator else start
    if end < start:
        raise CoverageDebtError(f"{label} range ends before it starts")
    return start, end


def load_classifications(path: Path) -> dict[str, FileClassification]:
    """Load explicit per-file classifications and validate range ownership."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CoverageDebtError(f"cannot load coverage classifications from {path}") from error
    root = _mapping(document, "coverage classification manifest")
    if root.get("schema_version") != 1:
        raise CoverageDebtError("unsupported coverage classification schema")
    file_records = _mapping(root.get("files"), "coverage classification files")
    result: dict[str, FileClassification] = {}
    for raw_path, raw_record in file_records.items():
        normalized_path = _normalize_path(raw_path)
        if normalized_path != raw_path or not normalized_path.startswith("src/django_ray/"):
            raise CoverageDebtError(f"classification path is not canonical: {raw_path!r}")
        record = _mapping(raw_record, f"classification for {raw_path}")
        default = _classification(record.get("default"), f"default classification for {raw_path}")
        raw_overrides = record.get("overrides", [])
        if not isinstance(raw_overrides, list):
            raise CoverageDebtError(f"classification overrides for {raw_path} must be a list")
        overrides: list[ClassificationOverride] = []
        for override_index, raw_override in enumerate(raw_overrides):
            override = _mapping(raw_override, f"override {override_index} for {raw_path}")
            classification = _classification(override, f"override {override_index} for {raw_path}")
            raw_ranges = override.get("ranges")
            if not isinstance(raw_ranges, list) or not raw_ranges:
                raise CoverageDebtError(
                    f"override {override_index} for {raw_path} needs at least one range"
                )
            for range_index, raw_range in enumerate(raw_ranges):
                start, end = _line_span(
                    raw_range, f"override {override_index} range {range_index} for {raw_path}"
                )
                if any(start <= existing.end and existing.start <= end for existing in overrides):
                    raise CoverageDebtError(f"classification overrides overlap for {raw_path}")
                overrides.append(
                    ClassificationOverride(
                        start=start,
                        end=end,
                        classification=classification,
                    )
                )
        result[normalized_path] = FileClassification(
            default=default,
            overrides=tuple(sorted(overrides, key=lambda item: (item.start, item.end))),
        )
    return result


def _normalize_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CoverageDebtError("coverage file path must be a non-empty string")
    return value.replace("\\", "/")


def _load_central_coverage_config(path: Path) -> dict[str, object]:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        report = config["tool"]["coverage"]["report"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise CoverageDebtError(
            f"cannot load central coverage configuration from {path}"
        ) from error
    precision = report.get("precision")
    fail_under = report.get("fail_under")
    if precision != 2:
        raise CoverageDebtError("coverage debt reporting requires central two-decimal precision")
    if isinstance(fail_under, bool) or not isinstance(fail_under, (int, float)):
        raise CoverageDebtError("central coverage fail_under must be numeric")
    return {
        "path": path.name,
        "precision": precision,
        "fail_under": fail_under,
    }


def _summary(record: dict[str, Any], label: str) -> dict[str, object]:
    summary = _mapping(record.get("summary"), f"{label} summary")
    statements = _non_negative_int(summary.get("num_statements"), f"{label} statements")
    covered_lines = _non_negative_int(summary.get("covered_lines"), f"{label} covered_lines")
    missed_lines = _non_negative_int(summary.get("missing_lines"), f"{label} missing_lines")
    coverage_percent = summary.get("percent_covered_display")
    if not isinstance(coverage_percent, str) or not re.fullmatch(
        r"(?:100\.00|\d{1,2}\.\d{2})", coverage_percent
    ):
        raise CoverageDebtError(f"{label} percentage is not rendered to two decimals")
    if covered_lines + missed_lines != statements:
        raise CoverageDebtError(f"{label} totals do not add up")
    return {
        "statements": statements,
        "covered_lines": covered_lines,
        "missed_lines": missed_lines,
        "coverage_percent": coverage_percent,
    }


def _uncovered_ranges(
    missing_lines: list[int], classifications: FileClassification
) -> list[dict[str, object]]:
    ranges: list[dict[str, object]] = []
    if not missing_lines:
        return ranges
    start = previous = missing_lines[0]
    classification = classifications.for_line(start)
    for line in missing_lines[1:]:
        next_classification = classifications.for_line(line)
        if line == previous + 1 and next_classification == classification:
            previous = line
            continue
        ranges.append(_range_record(start, previous, classification))
        start = previous = line
        classification = next_classification
    ranges.append(_range_record(start, previous, classification))
    return ranges


def _range_record(start: int, end: int, classification: Classification) -> dict[str, object]:
    return {
        "start": start,
        "end": end,
        "display": str(start) if start == end else f"{start}-{end}",
        "classification": asdict(classification),
    }


def build_report(
    coverage_path: Path,
    classifications_path: Path,
    pyproject_path: Path,
    source_commit: str,
) -> dict[str, Any]:
    """Build one deterministic, exact line-coverage debt report."""
    if not COMMIT_RE.fullmatch(source_commit):
        raise CoverageDebtError("source commit must be a full lowercase SHA")
    try:
        raw = json.loads(coverage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CoverageDebtError(f"cannot load coverage JSON from {coverage_path}") from error
    coverage = _mapping(raw, "coverage JSON")
    metadata = _mapping(coverage.get("meta"), "coverage metadata")
    if metadata.get("branch_coverage") is not False:
        raise CoverageDebtError("coverage debt currently supports line coverage only")
    files = _mapping(coverage.get("files"), "coverage files")
    central_config = _load_central_coverage_config(pyproject_path)
    classifications = load_classifications(classifications_path)

    debt_files: list[dict[str, Any]] = []
    aggregate = {"statements": 0, "covered_lines": 0, "missed_lines": 0}
    for raw_path, raw_file in files.items():
        file_record = _mapping(raw_file, f"coverage file {raw_path}")
        summary = _summary(file_record, f"coverage file {raw_path}")
        for key in aggregate:
            aggregate[key] += cast(int, summary[key])
        missed_lines = cast(int, summary["missed_lines"])
        if missed_lines == 0:
            continue
        normalized_path = _normalize_path(raw_path)
        classification = classifications.get(normalized_path)
        if classification is None:
            raise CoverageDebtError(
                f"uncovered file {normalized_path} has no review-policy classification"
            )
        raw_missing_lines = file_record.get("missing_lines")
        if not isinstance(raw_missing_lines, list) or not all(
            isinstance(line, int) and not isinstance(line, bool) and line > 0
            for line in raw_missing_lines
        ):
            raise CoverageDebtError(f"coverage file {raw_path} has invalid missing_lines")
        missing_line_numbers = sorted(set(cast(list[int], raw_missing_lines)))
        if len(missing_line_numbers) != missed_lines:
            raise CoverageDebtError(f"coverage file {raw_path} missing-line totals do not agree")
        debt_files.append(
            {
                "path": normalized_path,
                **summary,
                "uncovered_ranges": _uncovered_ranges(missing_line_numbers, classification),
            }
        )
    debt_files.sort(key=lambda item: (-cast(int, item["missed_lines"]), item["path"]))

    raw_totals = _mapping(coverage.get("totals"), "coverage totals")
    totals = _summary({"summary": raw_totals}, "coverage totals")
    for key, value in aggregate.items():
        if value != totals[key]:
            raise CoverageDebtError(
                f"coverage file totals for {key} do not match the overall total"
            )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "metric": "line",
        "source_commit": source_commit,
        "central_configuration": central_config,
        "totals": totals,
        "files": debt_files,
    }


def _markdown_cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def render_markdown(report: dict[str, Any]) -> str:
    """Render the machine report as stable Markdown sorted by missed lines."""
    measurement = Measurement.from_report(report)
    config = _mapping(report.get("central_configuration"), "central configuration")
    files = report.get("files")
    if not isinstance(files, list):
        raise CoverageDebtError("report files must be a list")
    lines = [
        "# Coverage debt report",
        "",
        "> This report measures **line coverage**. Branch coverage is a separate follow-up.",
        "",
        "| Source commit | Statements | Covered | Missed | Coverage | Floor | Precision |",
        "|---|---:|---:|---:|---:|---:|---:|",
        (
            f"| `{measurement.source_commit}` | {measurement.statements} | "
            f"{measurement.covered_lines} | {measurement.missed_lines} | "
            f"{measurement.coverage_percent}% | {config.get('fail_under')}% | "
            f"{config.get('precision')} decimals |"
        ),
        "",
        "## Debt by file",
        "",
        "Files are sorted by missed lines, then by path.",
        "",
        "| File | Statements | Covered | Missed | Coverage |",
        "|---|---:|---:|---:|---:|",
    ]
    for raw_file in files:
        file_record = _mapping(raw_file, "report file")
        lines.append(
            f"| `{_markdown_cell(file_record.get('path'))}` | {file_record.get('statements')} | "
            f"{file_record.get('covered_lines')} | {file_record.get('missed_lines')} | "
            f"{file_record.get('coverage_percent')}% |"
        )
    if not files:
        lines.append("| _No uncovered package lines_ | 0 | 0 | 0 | 100.00% |")
    lines.extend(["", "## Uncovered ranges", ""])
    if not files:
        lines.extend(["No uncovered package lines remain.", ""])
    for raw_file in files:
        file_record = _mapping(raw_file, "report file")
        lines.extend(
            [
                f"### `{_markdown_cell(file_record.get('path'))}`",
                "",
                "| Range | Classification | Rationale |",
                "|---:|---|---|",
            ]
        )
        ranges = file_record.get("uncovered_ranges")
        if not isinstance(ranges, list):
            raise CoverageDebtError("report uncovered_ranges must be a list")
        for raw_range in ranges:
            range_record = _mapping(raw_range, "uncovered range")
            classification = _mapping(
                range_record.get("classification"), "uncovered range classification"
            )
            lines.append(
                f"| {range_record.get('display')} | "
                f"{_markdown_cell(classification.get('label'))} | "
                f"{_markdown_cell(classification.get('rationale'))} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    """Atomically replace each deterministic report artifact."""
    _atomic_write(json_path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    _atomic_write(markdown_path, render_markdown(report))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending_path = path.with_name(f".{path.name}.pending")
    try:
        pending_path.write_text(content, encoding="utf-8", newline="\n")
        pending_path.replace(path)
    finally:
        pending_path.unlink(missing_ok=True)


def prepare_output_directory(path: Path) -> None:
    """Remove only this task's prior reports so failures cannot expose stale evidence."""
    path.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACT_NAMES:
        artifact = path / name
        artifact.unlink(missing_ok=True)
        artifact.with_name(f".{artifact.name}.pending").unlink(missing_ok=True)


def _parse_tracker_state(body: str) -> dict[str, Measurement]:
    if body.count(STATE_START_MARKER) != 1 or body.count(STATE_END_MARKER) != 1:
        raise CoverageDebtError("existing coverage-debt comment has invalid state markers")
    state_text = body.split(STATE_START_MARKER, maxsplit=1)[1].split(STATE_END_MARKER, maxsplit=1)[
        0
    ]
    try:
        raw_state = json.loads(state_text)
    except json.JSONDecodeError as error:
        raise CoverageDebtError("existing coverage-debt comment state is invalid JSON") from error
    state = _mapping(raw_state, "coverage-debt tracker state")
    if state.get("schema_version") != TRACKER_STATE_SCHEMA_VERSION:
        raise CoverageDebtError("existing coverage-debt comment has an unsupported state schema")
    return {
        name: Measurement.from_mapping(state.get(name)) for name in ("current", "previous", "best")
    }


def _measurement_is_better(candidate: Measurement, incumbent: Measurement) -> bool:
    if candidate.statements == 0:
        return incumbent.statements != 0
    if incumbent.statements == 0:
        return True
    return (
        candidate.covered_lines * incumbent.statements
        > incumbent.covered_lines * candidate.statements
    )


def _measurement_row(name: str, measurement: Measurement, repository: str) -> str:
    commit_url = f"https://github.com/{repository}/commit/{measurement.source_commit}"
    return (
        f"| {name} | [`{measurement.source_commit[:12]}`]({commit_url}) | "
        f"{measurement.statements} | {measurement.covered_lines} | "
        f"{measurement.missed_lines} | {measurement.coverage_percent}% |"
    )


def _tracker_debt_summary(report: dict[str, Any]) -> str:
    files = report.get("files")
    if not isinstance(files, list):
        raise CoverageDebtError("report files must be a list")
    lines = [
        "## Current uncovered debt",
        "",
        (
            "The uploaded JSON and Markdown artifacts retain each classification rationale. "
            "This durable comment keeps the exact ranges compact."
        ),
        "",
        "| File | Statements | Covered | Missed | Coverage | Classified uncovered ranges |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for raw_file in files:
        file_record = _mapping(raw_file, "report file")
        ranges = file_record.get("uncovered_ranges")
        if not isinstance(ranges, list):
            raise CoverageDebtError("report uncovered_ranges must be a list")
        classified_ranges: dict[str, list[str]] = {}
        for raw_range in ranges:
            range_record = _mapping(raw_range, "uncovered range")
            classification = _mapping(
                range_record.get("classification"), "uncovered range classification"
            )
            label = classification.get("label")
            display = range_record.get("display")
            if not isinstance(label, str) or not isinstance(display, str):
                raise CoverageDebtError("uncovered range has invalid display fields")
            classified_ranges.setdefault(label, []).append(display)
        compact_ranges = "; ".join(
            f"{label}: {', '.join(displays)}" for label, displays in classified_ranges.items()
        )
        lines.append(
            f"| `{_markdown_cell(file_record.get('path'))}` | "
            f"{file_record.get('statements')} | {file_record.get('covered_lines')} | "
            f"{file_record.get('missed_lines')} | {file_record.get('coverage_percent')}% | "
            f"{_markdown_cell(compact_ranges)} |"
        )
    if not files:
        lines.append("| _No uncovered package lines_ | 0 | 0 | 0 | 100.00% | None |")
    return "\n".join(lines)


def render_tracker_comment(
    report: dict[str, Any], state: dict[str, Measurement], repository: str
) -> str:
    """Render one bot-owned latest-report comment with embedded history state."""
    if not REPOSITORY_RE.fullmatch(repository):
        raise CoverageDebtError("repository must use owner/name form")
    detail = _tracker_debt_summary(report)
    serialized_state = {
        "schema_version": TRACKER_STATE_SCHEMA_VERSION,
        **{name: asdict(state[name]) for name in ("current", "previous", "best")},
    }
    body = "\n".join(
        [
            REPORT_COMMENT_MARKER,
            "# Latest coverage-debt report",
            "",
            (
                "This bot-owned comment is replaced in place. The first successful run seeds "
                "current, previous, and best with the same two-decimal line-coverage measurement."
            ),
            "",
            "| Measurement | Source commit | Statements | Covered | Missed | Coverage |",
            "|---|---|---:|---:|---:|---:|",
            _measurement_row("Current", state["current"], repository),
            _measurement_row("Previous", state["previous"], repository),
            _measurement_row("High water", state["best"], repository),
            "",
            detail.rstrip(),
            "",
            STATE_START_MARKER.rstrip("\n"),
            json.dumps(serialized_state, sort_keys=True, separators=(",", ":")),
            STATE_END_MARKER.lstrip("\n"),
            "",
        ]
    )
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise CoverageDebtError("coverage-debt tracker comment exceeds the safe GitHub size bound")
    return body


def _find_tracker_issue(api: TrackerApi, repository: str) -> dict[str, Any]:
    issues = api.paginate(f"/repos/{repository}/issues?state=all")
    matches: list[dict[str, Any]] = []
    occurrences = 0
    for issue in issues:
        body = issue.get("body")
        count = body.count(TRACKER_MARKER) if isinstance(body, str) else 0
        occurrences += count
        if count:
            matches.append(issue)
    if occurrences != 1 or len(matches) != 1:
        raise CoverageDebtError(
            "expected exactly one coverage-debt tracker marker across repository issues; "
            f"found {occurrences} marker(s) in {len(matches)} item(s)"
        )
    tracker = matches[0]
    if "pull_request" in tracker:
        raise CoverageDebtError("coverage-debt tracker marker must belong to an issue, not a PR")
    _non_negative_int(tracker.get("number"), "coverage-debt tracker issue number")
    return tracker


def update_tracker(
    api: TrackerApi,
    repository: str,
    report: dict[str, Any],
    *,
    expected_comment_author: str = "github-actions[bot]",
) -> str:
    """Create or update exactly one bot-owned report comment and return the action."""
    if not REPOSITORY_RE.fullmatch(repository):
        raise CoverageDebtError("repository must use owner/name form")
    current = Measurement.from_report(report)
    tracker = _find_tracker_issue(api, repository)
    issue_number = cast(int, tracker["number"])
    comments = api.paginate(f"/repos/{repository}/issues/{issue_number}/comments")
    matching_comments: list[dict[str, Any]] = []
    occurrences = 0
    for comment in comments:
        body = comment.get("body")
        count = body.count(REPORT_COMMENT_MARKER) if isinstance(body, str) else 0
        occurrences += count
        if count:
            matching_comments.append(comment)
    if occurrences > 1 or len(matching_comments) > 1:
        raise CoverageDebtError(
            "multiple coverage-debt latest-report markers found; refusing to choose a comment"
        )

    existing = matching_comments[0] if matching_comments else None
    if existing is None:
        state = {"current": current, "previous": current, "best": current}
    else:
        author = _mapping(existing.get("user"), "coverage-debt comment author").get("login")
        if author != expected_comment_author:
            raise CoverageDebtError(
                "coverage-debt latest-report comment is not owned by the expected bot"
            )
        existing_body = existing.get("body")
        if not isinstance(existing_body, str):
            raise CoverageDebtError("coverage-debt latest-report comment has no body")
        old_state = _parse_tracker_state(existing_body)
        best = current if _measurement_is_better(current, old_state["best"]) else old_state["best"]
        state = {"current": current, "previous": old_state["current"], "best": best}

    body = render_tracker_comment(report, state, repository)
    if existing is None:
        api.request("POST", f"/repos/{repository}/issues/{issue_number}/comments", {"body": body})
        return "created"
    comment_id = _non_negative_int(existing.get("id"), "coverage-debt comment id")
    api.request("PATCH", f"/repos/{repository}/issues/comments/{comment_id}", {"body": body})
    return "updated"


def _load_report(path: Path) -> dict[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")), "coverage-debt report")
    except (OSError, json.JSONDecodeError) as error:
        raise CoverageDebtError(f"cannot load coverage-debt report from {path}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-output", help="remove prior report artifacts")
    prepare.add_argument("--output-dir", type=Path, required=True)

    render = subparsers.add_parser("render", help="render exact JSON and Markdown reports")
    render.add_argument("--coverage-json", type=Path, required=True)
    render.add_argument("--classifications", type=Path, required=True)
    render.add_argument("--pyproject", type=Path, required=True)
    render.add_argument("--source-commit", required=True)
    render.add_argument("--json-output", type=Path, required=True)
    render.add_argument("--markdown-output", type=Path, required=True)

    tracker = subparsers.add_parser("update-tracker", help="update the single marked issue")
    tracker.add_argument("--report-json", type=Path, required=True)
    tracker.add_argument("--repository", required=True)
    tracker.add_argument("--expected-comment-author", default="github-actions[bot]")
    tracker.add_argument("--token-env", default="GITHUB_TOKEN")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare-output":
            prepare_output_directory(arguments.output_dir)
            print(f"Prepared {arguments.output_dir} for fresh coverage-debt evidence.")
            return 0
        if arguments.command == "render":
            report = build_report(
                arguments.coverage_json,
                arguments.classifications,
                arguments.pyproject,
                arguments.source_commit,
            )
            write_report(report, arguments.json_output, arguments.markdown_output)
            print(
                f"Wrote {arguments.json_output} and {arguments.markdown_output} "
                f"for {report['source_commit']}"
            )
            return 0
        report = _load_report(arguments.report_json)
        token = os.environ.get(arguments.token_env, "")
        action = update_tracker(
            GitHubApi(token),
            arguments.repository,
            report,
            expected_comment_author=arguments.expected_comment_author,
        )
        print(f"Coverage-debt tracker comment {action}.")
        return 0
    except CoverageDebtError as error:
        print(f"coverage debt: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
