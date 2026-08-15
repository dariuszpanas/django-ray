"""Build and publish deterministic line-coverage debt reports."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
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
TRUSTED_TRACKER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
MAX_COMMENT_BYTES = 64_000
MAX_PHASE_LOG_BYTES = 256 * 1024
MAX_PHASE_TIMING_BYTES = 16 * 1024 * 1024
PHASE_OUTPUT_DRAIN_TIMEOUT_SECONDS = 2.0
PHASE_FORCED_SHUTDOWN_TIMEOUT_SECONDS = 5.0
PHASE_REPORT_SCHEMA_VERSION = 1
TEST_TIMING_SCHEMA_VERSION = 4
ARTIFACT_NAMES = (
    "coverage.py.json",
    "coverage-debt.json",
    "coverage-debt.md",
    "coverage-phases.json",
    "coverage-phases.md",
    "coverage-default-resources.log",
    "coverage-local-ray.log",
    "local-ray-timing.json",
)
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
class CoveragePhase:
    """One isolated coverage collection phase with bounded diagnostics."""

    name: str
    selection: str
    coverage_mode: str
    timeout_seconds: float
    command: tuple[str, ...]
    log_path: Path
    timing_path: Path | None = None


@dataclass
class _OwnedPhaseProcess:
    """One phase launcher and the platform boundary containing its descendants."""

    process: subprocess.Popen[bytes]
    windows_job_handle: int | None = None


class _BoundedPhaseOutput:
    """Continuously drain subprocess output while retaining only its tail."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.output_bytes = 0
        self.tail = bytearray()
        self.error: str | None = None
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        """Count one output chunk and retain at most the configured tail."""
        with self._lock:
            self.output_bytes += len(chunk)
            self.tail.extend(chunk)
            overflow = len(self.tail) - self.max_bytes
            if overflow > 0:
                del self.tail[:overflow]

    def consume(self, stream: Any) -> None:
        """Drain a binary subprocess pipe until every writer closes it."""
        try:
            while chunk := stream.read(64 * 1024):
                self.append(chunk)
        except (OSError, ValueError) as error:
            with self._lock:
                self.error = " ".join(str(error).split())[:1_000]
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def mark_error(self, message: str) -> None:
        """Retain a bounded capture failure without replacing an earlier one."""
        with self._lock:
            if self.error is None:
                self.error = " ".join(message.split())[:1_000]

    def snapshot(self) -> tuple[int, bytes, str | None]:
        """Return a stable output count, retained tail, and capture error."""
        with self._lock:
            return self.output_bytes, bytes(self.tail), self.error


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


def coverage_phases(
    output_dir: Path,
    *,
    default_timeout_seconds: float,
    local_ray_timeout_seconds: float,
) -> tuple[CoveragePhase, CoveragePhase]:
    """Build the exact ordered coverage phases used locally and in automation."""
    for label, value in (
        ("default-resource", default_timeout_seconds),
        ("local-Ray", local_ray_timeout_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise CoverageDebtError(f"{label} timeout must be finite and positive")

    coverage_arguments = (
        "--cov=src",
        "--cov-config=pyproject.toml",
        "--cov-report=",
        "--cov-fail-under=0",
        "--maxfail=1",
    )
    default_resources = CoveragePhase(
        name="default-resources",
        selection="not real_ray and not live_cluster and not postgresql",
        coverage_mode="replace",
        timeout_seconds=default_timeout_seconds,
        command=(
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not real_ray and not live_cluster and not postgresql",
            *coverage_arguments,
            "-q",
        ),
        log_path=output_dir / "coverage-default-resources.log",
    )
    local_ray_timing = output_dir / "local-ray-timing.json"
    local_ray = CoveragePhase(
        name="local-ray",
        selection="taxonomy lane local-ray (compiled_graph_opt_in excluded)",
        coverage_mode="append",
        timeout_seconds=local_ray_timeout_seconds,
        command=(
            sys.executable,
            "scripts/test_suite_inventory.py",
            "run",
            "--lane",
            "local-ray",
            "--observation",
            "coverage-debt-monthly",
            "--variant",
            "locked-dependencies",
            "--timing-output",
            str(local_ray_timing),
            "--external-note",
            "uv environment already synchronized; setup time excluded",
            "--",
            *coverage_arguments,
            "--cov-append",
            "-vv",
        ),
        log_path=output_dir / "coverage-local-ray.log",
        timing_path=local_ray_timing,
    )
    return default_resources, local_ray


def _windows_process_handle(process: subprocess.Popen[bytes]) -> int:
    """Return a Windows ``Popen`` process handle without widening its public API."""
    try:
        return int(vars(process)["_handle"])
    except (KeyError, TypeError, ValueError) as error:  # pragma: no cover - Windows invariant
        raise CoverageDebtError(
            "Windows coverage phase omitted its native process handle"
        ) from error


def _create_windows_phase_job(process: subprocess.Popen[bytes]) -> int:
    """Contain a suspended Windows phase in a kill-on-close Job."""
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_job(None, None)
    if not handle:
        raise CoverageDebtError(
            f"could not create a Windows coverage phase Job: error {ctypes.get_last_error()}"
        )
    information = ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00002000
    if not set_information(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
        error_code = ctypes.get_last_error()
        close_handle(handle)
        raise CoverageDebtError(
            f"could not configure a Windows coverage phase Job: error {error_code}"
        )
    if not assign_process(
        handle,
        wintypes.HANDLE(_windows_process_handle(process)),
    ):
        error_code = ctypes.get_last_error()
        close_handle(handle)
        raise CoverageDebtError(f"could not contain a Windows coverage phase: error {error_code}")
    return int(handle)


def _resume_windows_phase(process: subprocess.Popen[bytes]) -> None:
    """Resume a Windows phase only after its Job boundary is installed."""
    import ctypes
    from ctypes import wintypes

    resume_process = ctypes.WinDLL("ntdll").NtResumeProcess
    resume_process.argtypes = [wintypes.HANDLE]
    resume_process.restype = ctypes.c_long
    status = int(resume_process(wintypes.HANDLE(_windows_process_handle(process))))
    if status != 0:
        raise CoverageDebtError(
            f"could not resume a contained Windows coverage phase: status {status}"
        )


def _windows_job_active_processes(handle: int) -> int:
    """Return the number of live processes retained by one Windows Job."""
    import ctypes
    from ctypes import wintypes

    class BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("total_user_time", ctypes.c_longlong),
            ("total_kernel_time", ctypes.c_longlong),
            ("this_period_total_user_time", ctypes.c_longlong),
            ("this_period_total_kernel_time", ctypes.c_longlong),
            ("total_page_fault_count", wintypes.DWORD),
            ("total_processes", wintypes.DWORD),
            ("active_processes", wintypes.DWORD),
            ("total_terminated_processes", wintypes.DWORD),
        ]

    query_information = ctypes.WinDLL("kernel32", use_last_error=True).QueryInformationJobObject
    query_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    query_information.restype = wintypes.BOOL
    information = BasicAccountingInformation()
    if not query_information(
        wintypes.HANDLE(handle),
        1,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    ):
        raise CoverageDebtError(
            f"could not query a Windows coverage phase Job: error {ctypes.get_last_error()}"
        )
    return int(information.active_processes)


def _terminate_windows_phase_job(handle: int) -> None:
    """Forcibly stop every live process retained by one Windows Job."""
    import ctypes
    from ctypes import wintypes

    terminate_job = ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject
    terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_job.restype = wintypes.BOOL
    if not terminate_job(wintypes.HANDLE(handle), 1):
        raise CoverageDebtError(
            f"could not terminate a Windows coverage phase Job: error {ctypes.get_last_error()}"
        )


def _close_windows_phase_job(handle: int) -> None:
    """Close a Windows Job handle, activating its final kill-on-close fence."""
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise CoverageDebtError(
            f"could not close a Windows coverage phase Job: error {ctypes.get_last_error()}"
        )


def _phase_environment() -> dict[str, str]:
    """Return the deterministic environment inherited by isolated coverage phases."""
    environment = os.environ.copy()
    # Local-Ray coverage reuses the synchronized driver instead of asking Ray's
    # uv hook to create a second packaged environment without installed dependencies.
    environment["RAY_ENABLE_UV_RUN_RUNTIME_ENV"] = "0"
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    for name in tuple(environment):
        if re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_\d+", name):
            environment.pop(name)
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
    environment["GIT_CONFIG_VALUE_0"] = "false"
    return environment


def _launch_phase(root: Path, phase: CoveragePhase) -> _OwnedPhaseProcess:
    launch_options: dict[str, Any] = {"env": _phase_environment()}
    if os.name == "posix":
        launch_options["start_new_session"] = True
    elif os.name == "nt":
        launch_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004  # CREATE_SUSPENDED
        )
    else:
        raise CoverageDebtError(f"unsupported coverage phase platform: {os.name}")
    process = cast(
        subprocess.Popen[bytes],
        subprocess.Popen(
            phase.command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **launch_options,
        ),
    )
    windows_job_handle: int | None = None
    try:
        if os.name == "nt":
            windows_job_handle = _create_windows_phase_job(process)
            _resume_windows_phase(process)
    except BaseException:
        try:
            if windows_job_handle is not None:
                _terminate_windows_phase_job(windows_job_handle)
            else:
                process.kill()
            process.wait(timeout=PHASE_FORCED_SHUTDOWN_TIMEOUT_SECONDS)
        finally:
            if windows_job_handle is not None:
                _close_windows_phase_job(windows_job_handle)
            if process.stdout is not None:
                process.stdout.close()
        raise
    return _OwnedPhaseProcess(process=process, windows_job_handle=windows_job_handle)


def _owned_process_tree_is_active(owned: _OwnedPhaseProcess) -> bool:
    """Return whether the retained platform boundary still contains live work."""
    if os.name == "posix":
        try:
            os.killpg(owned.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    if os.name == "nt":
        if owned.windows_job_handle is None:  # pragma: no cover - construction is fail closed
            raise CoverageDebtError("Windows coverage phase has no process-tree containment Job")
        return _windows_job_active_processes(owned.windows_job_handle) > 0
    raise CoverageDebtError(f"unsupported coverage phase platform: {os.name}")


def _close_owned_process_boundary(owned: _OwnedPhaseProcess) -> str | None:
    """Release the retained Windows Job handle after phase shutdown."""
    if owned.windows_job_handle is None:
        return None
    handle = owned.windows_job_handle
    owned.windows_job_handle = None
    try:
        _close_windows_phase_job(handle)
    except CoverageDebtError as error:
        return " ".join(str(error).split())[:1_000]
    return None


def _terminate_owned_process_tree(owned: _OwnedPhaseProcess) -> str | None:
    """Terminate only the subprocess tree launched for one coverage phase."""
    process = owned.process
    error_message: str | None = None
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            error_message = str(error)
    elif os.name == "nt" and owned.windows_job_handle is not None:
        try:
            _terminate_windows_phase_job(owned.windows_job_handle)
        except CoverageDebtError as error:
            error_message = str(error)
    elif os.name == "nt":  # pragma: no cover - construction is fail closed
        error_message = "Windows coverage phase has no process-tree containment Job"
    else:
        error_message = f"unsupported coverage phase platform: {os.name}"

    if error_message is not None and process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=PHASE_FORCED_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=PHASE_FORCED_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return "owned coverage phase process tree did not terminate"
    deadline = time.monotonic() + PHASE_FORCED_SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if not _owned_process_tree_is_active(owned):
                break
        except CoverageDebtError as error:
            error_message = str(error)
            break
        time.sleep(0.05)
    else:
        return "owned coverage phase process tree did not terminate"
    return None if error_message is None else " ".join(error_message.split())[:1_000]


def _settle_completed_phase_tree(
    owned: _OwnedPhaseProcess,
    reader: threading.Thread,
) -> tuple[bool, str | None, str | None]:
    """Allow orderly exit, then fail closed on post-launcher descendants."""
    deadline = time.monotonic() + PHASE_OUTPUT_DRAIN_TIMEOUT_SECONDS
    tree_active = True
    query_error: str | None = None
    while time.monotonic() < deadline:
        reader.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            tree_active = _owned_process_tree_is_active(owned)
        except CoverageDebtError as error:
            query_error = " ".join(str(error).split())[:1_000]
            break
        if not tree_active and not reader.is_alive():
            return False, None, None

    if query_error is None:
        try:
            tree_active = _owned_process_tree_is_active(owned)
        except CoverageDebtError as error:
            query_error = " ".join(str(error).split())[:1_000]
    descendants_terminated = tree_active
    cleanup_error = query_error
    if tree_active:
        cleanup_error = "owned coverage phase descendants outlived the launcher"
    termination_error = (
        _terminate_owned_process_tree(owned)
        if tree_active or query_error is not None or reader.is_alive()
        else None
    )
    return descendants_terminated, cleanup_error, termination_error


def _phase_log(
    tail: bytes,
    output_bytes: int,
    phase: CoveragePhase,
    *,
    outcome: str,
    exit_code: int | None,
    timed_out: bool,
    termination_error: str | None,
    cleanup_error: str | None,
    descendants_terminated: bool,
    capture_error: str | None,
    timing_error: str | None,
) -> bool:
    truncated = output_bytes > len(tail)
    body = tail.decode("utf-8", errors="replace")
    header = "\n".join(
        (
            f"phase: {phase.name}",
            f"selection: {phase.selection}",
            f"coverage_mode: {phase.coverage_mode}",
            f"outcome: {outcome}",
            f"timeout_seconds: {phase.timeout_seconds:g}",
            f"timed_out: {str(timed_out).lower()}",
            f"exit_code: {'unavailable' if exit_code is None else exit_code}",
            f"output_bytes: {output_bytes}",
            f"tail_truncated: {str(truncated).lower()}",
            (
                "termination_error: none"
                if termination_error is None
                else f"termination_error: {termination_error}"
            ),
            "cleanup_error: none" if cleanup_error is None else f"cleanup_error: {cleanup_error}",
            f"post_exit_descendants_terminated: {str(descendants_terminated).lower()}",
            "capture_error: none" if capture_error is None else f"capture_error: {capture_error}",
            "timing_error: none" if timing_error is None else f"timing_error: {timing_error}",
            "",
        )
    )
    rendered = header + body
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    _atomic_write(phase.log_path, rendered)
    print(f"Coverage-debt phase {phase.name}: {outcome}.")
    if body:
        print(body, end="" if body.endswith("\n") else "\n")
    return truncated


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON number: {constant}")


def _timing_evidence_error(path: Path, *, expected_lane: str) -> str | None:
    """Return why required timing evidence is unusable, or ``None``."""
    try:
        path_metadata = path.lstat()
    except OSError:
        return "required timing evidence was not created"
    if not stat.S_ISREG(path_metadata.st_mode):
        return "required timing evidence is not a regular file"
    if path_metadata.st_size <= 0 or path_metadata.st_size > MAX_PHASE_TIMING_BYTES:
        return "required timing evidence has an invalid size"
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return "required timing evidence is not a regular file"
        if metadata.st_size <= 0 or metadata.st_size > MAX_PHASE_TIMING_BYTES:
            return "required timing evidence has an invalid size"
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            payload = stream.read(MAX_PHASE_TIMING_BYTES + 1)
    except OSError:
        return (
            "required timing evidence was not created"
            if not path.exists()
            else "required timing evidence is not a readable regular file"
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not payload or len(payload) > MAX_PHASE_TIMING_BYTES:
        return "required timing evidence has an invalid size"
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError):
        return "required timing evidence is not valid JSON"
    if not isinstance(value, dict):
        return "required timing evidence must be a JSON object"
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != TEST_TIMING_SCHEMA_VERSION
    ):
        return "required timing evidence has an unsupported schema"
    if value.get("lane") != expected_lane:
        return "required timing evidence identifies a different phase"
    if (
        value.get("observation") != "coverage-debt-monthly"
        or value.get("variant") != "locked-dependencies"
    ):
        return "required timing evidence identifies a different observation"
    source = value.get("source")
    if (
        not isinstance(source, dict)
        or source.get("algorithm") != "sha256"
        or not isinstance(source.get("digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["digest"]) is None
        or type(source.get("file_count")) is not int
        or source["file_count"] < 1
        or value.get("source_after_digest") != source["digest"]
    ):
        return "required timing evidence does not preserve its source fence"
    selection = value.get("selection")
    skip_policy = value.get("skip_policy")
    collection = value.get("collection")
    if (
        not isinstance(selection, str)
        or not selection.strip()
        or not isinstance(skip_policy, dict)
        or skip_policy.get("mode") != "forbid"
        or not isinstance(collection, dict)
        or collection.get("mode") != "serial"
        or collection.get("valid") is not True
        or collection.get("errors") != []
    ):
        return "required timing evidence does not preserve the skip-forbidden selection"
    integrity = value.get("integrity")
    pytest_record = value.get("pytest")
    if (
        not isinstance(integrity, dict)
        or integrity.get("valid") is not True
        or integrity.get("errors") != []
        or not isinstance(pytest_record, dict)
        or type(pytest_record.get("exit_code")) is not int
        or pytest_record.get("exit_code") != 0
    ):
        return "required timing evidence does not prove a complete passing phase"
    counts = (
        pytest_record.get("selected_count"),
        pytest_record.get("completed_count"),
        pytest_record.get("logfinished_count"),
        collection.get("selected_count"),
    )
    if (
        any(type(count) is not int or count < 1 for count in counts)
        or len(set(counts)) != 1
        or pytest_record.get("coverage_enabled") is not True
    ):
        return "required timing evidence does not prove every selected test completed"
    selected_count = cast(int, counts[0])
    outcomes = pytest_record.get("outcomes")
    outcome_names = ("failed", "passed", "skipped", "xfailed", "xpassed")
    if (
        not isinstance(outcomes, dict)
        or set(outcomes) != set(outcome_names)
        or any(type(outcomes.get(name)) is not int or outcomes[name] < 0 for name in outcome_names)
        or sum(outcomes[name] for name in outcome_names) != selected_count
        or outcomes["failed"] != 0
        or outcomes["skipped"] != 0
        or outcomes["xfailed"] != 0
    ):
        return "required timing evidence outcomes violate the passing skip-forbidden contract"
    test_outcomes = value.get("test_outcomes")
    observed_outcomes = dict.fromkeys(outcome_names, 0)
    observed_nodeids: set[str] = set()
    if not isinstance(test_outcomes, list) or len(test_outcomes) != selected_count:
        return "required timing evidence omits exact selected-test outcomes"
    for record in test_outcomes:
        if not isinstance(record, dict):
            return "required timing evidence has an invalid selected-test outcome"
        nodeid = record.get("nodeid")
        outcome = record.get("outcome")
        if (
            not isinstance(nodeid, str)
            or not nodeid
            or nodeid in observed_nodeids
            or outcome not in observed_outcomes
        ):
            return "required timing evidence has an invalid selected-test outcome"
        observed_nodeids.add(nodeid)
        observed_outcomes[cast(str, outcome)] += 1
    if observed_outcomes != outcomes or value.get("skipped_tests") != []:
        return "required timing evidence selected-test outcomes are inconsistent"
    pytest_arguments = value.get("pytest_arguments")
    required_arguments = {
        "--cov=src",
        "--cov-config=pyproject.toml",
        "--cov-report=",
        "--cov-fail-under=0",
        "--cov-append",
        "--maxfail=1",
    }
    if (
        not isinstance(pytest_arguments, list)
        or not all(isinstance(argument, str) for argument in pytest_arguments)
        or not required_arguments <= set(pytest_arguments)
        or "--no-cov" in pytest_arguments
    ):
        return "required timing evidence does not identify the append-coverage invocation"
    return None


def run_coverage_phase(root: Path, phase: CoveragePhase) -> dict[str, object]:
    """Run one phase with a hard process-tree deadline and a capped log tail."""
    phase.log_path.parent.mkdir(parents=True, exist_ok=True)
    if phase.timing_path is not None:
        phase.timing_path.unlink(missing_ok=True)
    started = time.monotonic()
    timed_out = False
    termination_error: str | None = None
    cleanup_error: str | None = None
    descendants_terminated = False
    exit_code: int | None = None
    launch_error: str | None = None
    capture = _BoundedPhaseOutput(MAX_PHASE_LOG_BYTES)
    try:
        owned = _launch_phase(root, phase)
    except (OSError, CoverageDebtError) as error:
        launch_error = " ".join(str(error).split())[:1_000]
        capture.append((launch_error + "\n").encode())
        outcome = "launch-error"
    else:
        process = owned.process
        if process.stdout is None:  # pragma: no cover - guaranteed by _launch_phase
            _terminate_owned_process_tree(owned)
            _close_owned_process_boundary(owned)
            raise CoverageDebtError("coverage phase output pipe was not created")
        reader = threading.Thread(
            target=capture.consume,
            args=(process.stdout,),
            name=f"coverage-debt-{phase.name}-output",
            daemon=True,
        )
        try:
            reader.start()
        except BaseException as error:
            termination_error = _terminate_owned_process_tree(owned)
            boundary_error = _close_owned_process_boundary(owned)
            if boundary_error is not None and termination_error is None:
                termination_error = boundary_error
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
            if not isinstance(error, (OSError, RuntimeError)):
                raise
            launch_error = " ".join(
                f"coverage phase output reader failed to start: {error}".split()
            )[:1_000]
            capture.append((launch_error + "\n").encode())
            exit_code = process.returncode
            outcome = "launch-error"
        else:
            try:
                try:
                    process.wait(timeout=phase.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    termination_error = _terminate_owned_process_tree(owned)
                else:
                    (
                        descendants_terminated,
                        cleanup_error,
                        termination_error,
                    ) = _settle_completed_phase_tree(owned, reader)
            except BaseException:
                _terminate_owned_process_tree(owned)
                raise
            finally:
                boundary_error = _close_owned_process_boundary(owned)
                if boundary_error is not None and termination_error is None:
                    termination_error = boundary_error
                reader.join(timeout=PHASE_FORCED_SHUTDOWN_TIMEOUT_SECONDS)
                if reader.is_alive():
                    try:
                        process.stdout.close()
                    except (OSError, ValueError):
                        pass
                    reader.join(timeout=0.1)
                if reader.is_alive():
                    capture.mark_error("owned coverage phase output reader did not stop")
            exit_code = process.returncode
            _, _, capture_error = capture.snapshot()
            if timed_out:
                outcome = "timed-out"
            elif exit_code != 0:
                outcome = "failed"
            elif cleanup_error is not None or termination_error is not None:
                outcome = "cleanup-error"
            elif capture_error is not None:
                outcome = "capture-error"
            else:
                outcome = "passed"

    timing_error = (
        None
        if phase.timing_path is None
        else _timing_evidence_error(phase.timing_path, expected_lane=phase.name)
    )
    if outcome == "passed" and timing_error is not None:
        outcome = "invalid-timing-evidence"
    output_bytes, tail, capture_error = capture.snapshot()
    log_truncated = _phase_log(
        tail,
        output_bytes,
        phase,
        outcome=outcome,
        exit_code=exit_code,
        timed_out=timed_out,
        termination_error=termination_error,
        cleanup_error=cleanup_error,
        descendants_terminated=descendants_terminated,
        capture_error=capture_error,
        timing_error=timing_error,
    )

    elapsed_seconds = round(time.monotonic() - started, 6)
    return {
        "name": phase.name,
        "selection": phase.selection,
        "coverage_mode": phase.coverage_mode,
        "timeout_seconds": phase.timeout_seconds,
        "elapsed_seconds": elapsed_seconds,
        "outcome": outcome,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "termination_error": termination_error,
        "cleanup_error": cleanup_error,
        "post_exit_descendants_terminated": descendants_terminated,
        "capture_error": capture_error,
        "output_bytes": output_bytes,
        "retained_output_bytes": len(tail),
        "log_truncated": log_truncated,
        "log_path": phase.log_path.as_posix(),
        "timing_path": None if phase.timing_path is None else phase.timing_path.as_posix(),
        "timing_evidence": (
            phase.timing_path is not None and outcome == "passed" and timing_error is None
        ),
        "timing_error": timing_error,
    }


def _render_phase_markdown(
    records: list[dict[str, object]], *, complete: bool, failure: str | None
) -> str:
    lines = [
        "# Coverage-debt phase diagnostics",
        "",
        f"Overall phase collection: **{'complete' if complete else 'incomplete'}**.",
        "",
        "| Phase | Selection | Coverage | Outcome | Limit | Duration | Exit |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for record in records:
        exit_code = record["exit_code"]
        lines.append(
            f"| `{record['name']}` | {_markdown_cell(record['selection'])} | "
            f"{record['coverage_mode']} | {record['outcome']} | "
            f"{record['timeout_seconds']}s | {record['elapsed_seconds']}s | "
            f"{'-' if exit_code is None else exit_code} |"
        )
    if not records:
        lines.append("| _No phase started_ | - | - | incomplete | - | - | - |")
    lines.extend(
        [
            "",
            (
                f"Failure: `{_markdown_cell(failure)}`"
                if failure is not None
                else "Each phase log retains at most 256 KiB of output."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _write_phase_summary(
    output_dir: Path,
    records: list[dict[str, object]],
    *,
    complete: bool,
    failure: str | None,
) -> None:
    payload = {
        "schema_version": PHASE_REPORT_SCHEMA_VERSION,
        "complete": complete,
        "failure": failure,
        "phases": records,
    }
    _atomic_write(
        output_dir / "coverage-phases.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(
        output_dir / "coverage-phases.md",
        _render_phase_markdown(records, complete=complete, failure=failure),
    )


def run_coverage_phases(
    root: Path,
    output_dir: Path,
    *,
    default_timeout_seconds: float,
    local_ray_timeout_seconds: float,
) -> None:
    """Collect replace-then-append coverage with durable per-phase diagnostics."""
    phases = coverage_phases(
        output_dir,
        default_timeout_seconds=default_timeout_seconds,
        local_ray_timeout_seconds=local_ray_timeout_seconds,
    )
    records: list[dict[str, object]] = []
    _write_phase_summary(output_dir, records, complete=False, failure=None)
    try:
        erased = subprocess.run(
            [sys.executable, "-m", "coverage", "erase"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        failure = "cannot erase prior coverage data: " + " ".join(str(error).split())[:1_000]
        _write_phase_summary(output_dir, records, complete=False, failure=failure)
        raise CoverageDebtError(failure) from error
    if erased.returncode != 0:
        detail = " ".join((erased.stderr or erased.stdout).split())[:1_000]
        failure = f"coverage erase failed with exit code {erased.returncode}: {detail}"
        _write_phase_summary(output_dir, records, complete=False, failure=failure)
        raise CoverageDebtError(failure)

    for phase in phases:
        record = run_coverage_phase(root, phase)
        records.append(record)
        if record["outcome"] != "passed":
            failure = f"phase {phase.name} ended with outcome {record['outcome']}"
            _write_phase_summary(output_dir, records, complete=False, failure=failure)
            raise CoverageDebtError(failure)
        _write_phase_summary(output_dir, records, complete=False, failure=None)
    _write_phase_summary(output_dir, records, complete=True, failure=None)


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
        if issue.get("author_association") not in TRUSTED_TRACKER_ASSOCIATIONS:
            continue
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
        author = comment.get("user")
        if not isinstance(author, dict) or author.get("login") != expected_comment_author:
            continue
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

    phases = subparsers.add_parser(
        "run-phases", help="collect default-resource and bounded local-Ray coverage"
    )
    phases.add_argument("--output-dir", type=Path, required=True)
    phases.add_argument("--default-timeout-seconds", type=float, required=True)
    phases.add_argument("--local-ray-timeout-seconds", type=float, required=True)

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
        if arguments.command == "run-phases":
            run_coverage_phases(
                Path.cwd(),
                arguments.output_dir,
                default_timeout_seconds=arguments.default_timeout_seconds,
                local_ray_timeout_seconds=arguments.local_ray_timeout_seconds,
            )
            print("Coverage-debt phases completed with combined coverage data.")
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
