"""Validate paired pytest-xdist benchmark and coverage evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

PAIR_SCHEMA_VERSION = 1
AGGREGATE_SCHEMA_VERSION = 2
RUN_SCHEMA_VERSION = 1
TIMING_SCHEMA_VERSION = 3
RAY_RESIDUE_SCHEMA_VERSION = 2
OWNED_TEMP_SCAN_LIMIT = 10_000
PHASES = ("hermetic", "sqlite-django", "local-ray", "default-serial-remainder")
SERIAL_EXECUTION = {
    "mode": "serial",
    "workers": 0,
    "distribution": "no",
    "max_worker_restart": 0,
}
XDIST_EXECUTION = {
    "mode": "xdist",
    "workers": 2,
    "distribution": "worksteal",
    "max_worker_restart": 0,
}
GLOBAL_COVERAGE_MIN = 95.0
SOURCE_COVERAGE_PREFIX = "src/django_ray/"
MODULE_COVERAGE_FLOORS = {
    "src/django_ray/management/commands/django_ray_worker.py": 90.0,
    "src/django_ray/runner/ray_job.py": 90.0,
}
TESTPROJECT_COVERAGE_PATHS = (
    "testproject/api.py",
    "testproject/views.py",
    "testproject/urls.py",
)
TESTPROJECT_COVERAGE_MIN = 80.0
RETENTION_IMPROVEMENT_MIN = 25.0
OUTCOME_NAMES = frozenset({"passed", "failed", "skipped", "xfailed", "xpassed"})
BENCHMARK_WORKFLOW_PATH = ".github/workflows/pytest-xdist-retention.yml"
BENCHMARK_PAIR_JOB_ID = "pytest-xdist-benchmark-pair"


class BenchmarkError(ValueError):
    """Raised when benchmark evidence is incomplete or inconsistent."""


def prepare_output(directory: Path, root: Path | None = None) -> None:
    """Create a fresh output directory and reject every stale artifact."""
    if os.environ.get("DJANGO_RAY_RUN_COMPILED_SESSION_TOPOLOGY_PROBE", "").strip():
        raise BenchmarkError(
            "phased benchmark requires DJANGO_RAY_RUN_COMPILED_SESSION_TOPOLOGY_PROBE unset"
        )
    repository = (root or Path.cwd()).resolve()
    resolved = directory.resolve()
    try:
        resolved.relative_to(repository)
    except ValueError as error:
        raise BenchmarkError(
            "benchmark output directory must stay inside the repository"
        ) from error
    if resolved == repository:
        raise BenchmarkError("benchmark output directory cannot be the repository root")
    if resolved.exists() and any(resolved.iterdir()):
        raise BenchmarkError("benchmark output directory must be new or empty")
    ignored_probe = resolved / ".django-ray-benchmark-output"
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(ignored_probe)],
        cwd=repository,
        check=False,
    )
    if ignored.returncode != 0:
        raise BenchmarkError("benchmark output directory must be ignored by Git")
    resolved.mkdir(parents=True, exist_ok=True)


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON number: {constant}")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"cannot load {label} from {path}") from error
    if not isinstance(value, dict):
        raise BenchmarkError(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def _render_json(value: object) -> str:
    try:
        return json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as error:
        raise BenchmarkError("benchmark evidence is not portable JSON") from error


def _git_head(root: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root or Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != 40:
        raise BenchmarkError("cannot resolve the checkout Git commit identity")
    return value


def _git_tree(root: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=root or Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != 40:
        raise BenchmarkError("cannot resolve the checkout Git tree identity")
    return value


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, pending_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".pending", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
        Path(pending_name).replace(path)
    finally:
        Path(pending_name).unlink(missing_ok=True)


def _strict_mapping(value: object, expected: dict[str, object], label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise BenchmarkError(f"{label} must declare the complete fixed policy")
    for field, expected_value in expected.items():
        actual_value = value.get(field)
        if type(actual_value) is not type(expected_value) or actual_value != expected_value:
            raise BenchmarkError(f"{label} differs from the fixed policy")


def _require_schema_version(value: object, expected: int, label: str) -> None:
    if type(value) is not int or value != expected:
        raise BenchmarkError(f"{label} has the wrong schema version")


def _nonnegative_number(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise BenchmarkError(f"{label} must be a finite nonnegative number")
    return float(value)


def _source_digest(timing: dict[str, Any], label: str) -> str:
    source = timing.get("source")
    if not isinstance(source, dict):
        raise BenchmarkError(f"{label} needs source-fence evidence")
    digest = source.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise BenchmarkError(f"{label} needs a SHA-256 source digest")
    if timing.get("source_after_digest") != digest:
        raise BenchmarkError(f"{label} source changed during execution")
    return digest


def _outcomes(timing: dict[str, Any], label: str) -> dict[str, str]:
    records = timing.get("test_outcomes")
    if not isinstance(records, list) or not records:
        raise BenchmarkError(f"{label} needs exact per-test outcomes")
    outcomes: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            raise BenchmarkError(f"{label} test outcomes must be objects")
        nodeid = record.get("nodeid")
        outcome = record.get("outcome")
        if not isinstance(nodeid, str) or not nodeid or outcome not in OUTCOME_NAMES:
            raise BenchmarkError(f"{label} test outcomes are incomplete")
        if nodeid in outcomes:
            raise BenchmarkError(f"{label} test outcome node IDs must be unique")
        outcomes[nodeid.replace("\\", "/")] = outcome
    pytest_record = timing.get("pytest")
    if not isinstance(pytest_record, dict) or pytest_record.get("completed_count") != len(outcomes):
        raise BenchmarkError(f"{label} outcomes do not match completed work")
    return outcomes


def _nodeid_digest(nodeids: set[str]) -> str:
    encoded = json.dumps(sorted(nodeids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_phase(directory: Path, phase: str, execution: str) -> dict[str, Any]:
    label = f"{execution} {phase} timing"
    timing = _load_json(directory / f"{phase}.json", label)
    _require_schema_version(timing.get("schema_version"), TIMING_SCHEMA_VERSION, label)
    if timing.get("lane") != phase:
        raise BenchmarkError(f"{label} has the wrong lane")
    if timing.get("integrity") != {"valid": True, "errors": []}:
        raise BenchmarkError(f"{label} failed execution-integrity checks")
    collection = timing.get("collection")
    if not isinstance(collection, dict) or collection.get("valid") is not True:
        raise BenchmarkError(f"{label} failed collection-integrity checks")
    expected_execution = (
        XDIST_EXECUTION if execution == "xdist" and phase == "hermetic" else SERIAL_EXECUTION
    )
    _strict_mapping(timing.get("execution"), expected_execution, f"{label} execution")
    _strict_mapping(collection.get("execution"), expected_execution, f"{label} collection")
    if collection.get("mode") != expected_execution["mode"]:
        raise BenchmarkError(f"{label} collection mode differs from execution")
    _source_digest(timing, label)
    _outcomes(timing, label)
    return timing


def _validate_inventory(
    directory: Path,
    timings: list[dict[str, Any]],
    label: str,
) -> dict[str, Any]:
    inventory = _load_json(directory / "inventory.json", f"{label} merged inventory")
    _require_schema_version(
        inventory.get("schema_version"), TIMING_SCHEMA_VERSION, f"{label} merged inventory"
    )
    inventory_timings = inventory.get("timings")
    if (
        not isinstance(inventory_timings, list)
        or len(inventory_timings) != len(PHASES)
        or any(not isinstance(timing, dict) for timing in inventory_timings)
    ):
        raise BenchmarkError(f"{label} merged inventory omits timing evidence")
    expected_ids = [timing.get("sample_id") for timing in timings]
    actual_ids = [timing.get("sample_id") for timing in inventory_timings]
    if (
        any(not isinstance(sample_id, str) or not sample_id for sample_id in expected_ids)
        or any(not isinstance(sample_id, str) or not sample_id for sample_id in actual_ids)
        or len(set(expected_ids)) != len(PHASES)
        or len(set(actual_ids)) != len(PHASES)
    ):
        raise BenchmarkError(f"{label} merged inventory timing identities are not unique")
    expected_by_id = {timing.get("sample_id"): timing for timing in timings}
    actual_by_id = {
        timing.get("sample_id"): timing for timing in cast(list[dict[str, Any]], inventory_timings)
    }
    if len(expected_by_id) != len(PHASES) or actual_by_id != expected_by_id:
        raise BenchmarkError(f"{label} merged inventory does not validate every canonical phase")
    if not (directory / "inventory.md").is_file():
        raise BenchmarkError(f"{label} merged inventory Markdown is missing")
    source = inventory.get("source")
    if not isinstance(source, dict) or source.get("digest") != _source_digest(
        timings[0], f"{label} inventory"
    ):
        raise BenchmarkError(f"{label} merged inventory source fence differs from its timings")

    phase_nodeids: set[str] = set()
    for phase, timing in zip(PHASES, timings, strict=True):
        phase_outcomes = _outcomes(timing, f"{label} {phase}")
        if phase == "default-serial-remainder" and set(phase_outcomes.values()) != {"skipped"}:
            raise BenchmarkError(
                f"{label} default serial remainder must retain only intentional skips"
            )
        nodeids = set(phase_outcomes)
        if phase_nodeids & nodeids:
            raise BenchmarkError(f"{label} canonical phases select overlapping node IDs")
        phase_nodeids.update(nodeids)
    groups = inventory.get("groups")
    if not isinstance(groups, list):
        raise BenchmarkError(f"{label} merged inventory omits taxonomy groups")
    supported = [
        group
        for group in groups
        if isinstance(group, dict) and group.get("id") == "supported-python"
    ]
    if len(supported) != 1:
        raise BenchmarkError(f"{label} merged inventory omits supported-python identity")
    supported_group = supported[0]
    if supported_group.get("selected_count") != len(phase_nodeids) or supported_group.get(
        "nodeid_digest"
    ) != _nodeid_digest(phase_nodeids):
        raise BenchmarkError(
            f"{label} canonical phase union differs from the supported-python selection"
        )
    return inventory


def _integer_lines(value: object, label: str) -> frozenset[int]:
    if not isinstance(value, list) or any(
        isinstance(line, bool) or not isinstance(line, int) or line < 1 for line in value
    ):
        raise BenchmarkError(f"{label} must be a list of positive line numbers")
    if len(value) != len(set(value)):
        raise BenchmarkError(f"{label} contains duplicate line numbers")
    return frozenset(cast(list[int], value))


def _line_percent(executed: frozenset[int], statements: frozenset[int]) -> float:
    return 100.0 if not statements else 100.0 * len(executed) / len(statements)


def _coverage_line_digest(
    files: dict[str, dict[str, frozenset[int]]],
    field: str,
) -> str:
    payload = {path: sorted(record[field]) for path, record in sorted(files.items())}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _coverage(directory: Path, label: str) -> dict[str, Any]:
    document = _load_json(directory / "coverage.json", f"{label} coverage")
    files = document.get("files")
    if not isinstance(files, dict) or not files:
        raise BenchmarkError(f"{label} coverage needs file-level evidence")
    normalized: dict[str, dict[str, frozenset[int]]] = {}
    for raw_path, raw_record in files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_record, dict):
            raise BenchmarkError(f"{label} coverage file entries are invalid")
        path = raw_path.replace("\\", "/")
        executed = _integer_lines(raw_record.get("executed_lines"), f"{label} {path} executed")
        missing = _integer_lines(raw_record.get("missing_lines"), f"{label} {path} missing")
        excluded = _integer_lines(raw_record.get("excluded_lines"), f"{label} {path} excluded")
        if executed & missing:
            raise BenchmarkError(f"{label} {path} executes and misses the same line")
        normalized[path] = {
            "executed": executed,
            "missing": missing,
            "excluded": excluded,
            "statements": executed | missing,
        }
    source_files = {
        path: record
        for path, record in normalized.items()
        if path.startswith(SOURCE_COVERAGE_PREFIX)
    }
    if not source_files:
        raise BenchmarkError(f"{label} coverage omits django-ray source")
    statements = sum(len(record["statements"]) for record in source_files.values())
    covered = sum(len(record["executed"]) for record in source_files.values())
    global_percent = 100.0 if not statements else 100.0 * covered / statements
    if global_percent + 1e-9 < GLOBAL_COVERAGE_MIN:
        raise BenchmarkError(
            f"{label} django-ray source coverage is below {GLOBAL_COVERAGE_MIN:.0f}%"
        )
    module_percent: dict[str, float] = {}
    for path, floor in MODULE_COVERAGE_FLOORS.items():
        record = normalized.get(path)
        if record is None:
            raise BenchmarkError(f"{label} coverage omits floor module {path}")
        percent = _line_percent(record["executed"], record["statements"])
        if percent + 1e-9 < floor:
            raise BenchmarkError(f"{label} coverage for {path} is below {floor:.0f}%")
        module_percent[path] = percent
    missing_testproject = [path for path in TESTPROJECT_COVERAGE_PATHS if path not in normalized]
    if missing_testproject:
        raise BenchmarkError(
            f"{label} coverage omits testproject floor modules: " + ", ".join(missing_testproject)
        )
    testproject_records = [normalized[path] for path in TESTPROJECT_COVERAGE_PATHS]
    testproject_statements = sum(len(record["statements"]) for record in testproject_records)
    testproject_covered = sum(len(record["executed"]) for record in testproject_records)
    testproject_percent = (
        100.0
        if not testproject_statements
        else 100.0 * testproject_covered / testproject_statements
    )
    if testproject_percent + 1e-9 < TESTPROJECT_COVERAGE_MIN:
        raise BenchmarkError(
            f"{label} combined testproject coverage is below {TESTPROJECT_COVERAGE_MIN:.0f}%"
        )
    xml_path = directory / "coverage.xml"
    try:
        ET.parse(xml_path)
    except (OSError, ET.ParseError) as error:
        raise BenchmarkError(f"{label} coverage XML is missing or invalid") from error
    return {
        "files": normalized,
        "covered_lines": covered,
        "missing_lines": statements - covered,
        "statement_lines": statements,
        "percent": global_percent,
        "source_prefix": SOURCE_COVERAGE_PREFIX,
        "module_percent": module_percent,
        "testproject": {
            "paths": list(TESTPROJECT_COVERAGE_PATHS),
            "covered_lines": testproject_covered,
            "missing_lines": testproject_statements - testproject_covered,
            "statement_lines": testproject_statements,
            "percent": testproject_percent,
        },
        "statement_line_digest": _coverage_line_digest(normalized, "statements"),
        "covered_line_digest": _coverage_line_digest(normalized, "executed"),
        "missing_line_digest": _coverage_line_digest(normalized, "missing"),
        "excluded_line_digest": _coverage_line_digest(normalized, "excluded"),
    }


def _compare_coverage(serial: dict[str, Any], xdist: dict[str, Any]) -> None:
    serial_files = cast(dict[str, dict[str, frozenset[int]]], serial["files"])
    xdist_files = cast(dict[str, dict[str, frozenset[int]]], xdist["files"])
    if set(serial_files) != set(xdist_files):
        raise BenchmarkError("serial and xdist coverage file sets differ")
    for path in sorted(serial_files):
        serial_record = serial_files[path]
        xdist_record = xdist_files[path]
        if serial_record["statements"] != xdist_record["statements"]:
            raise BenchmarkError(f"serial and xdist statement lines differ for {path}")
        if serial_record["excluded"] != xdist_record["excluded"]:
            raise BenchmarkError(f"serial and xdist excluded lines differ for {path}")
        if not serial_record["executed"] <= xdist_record["executed"]:
            raise BenchmarkError(f"xdist combined coverage regresses covered lines for {path}")


def _phase_parity(
    serial: dict[str, Any],
    xdist: dict[str, Any],
    phase: str,
) -> dict[str, object]:
    serial_collection = cast(dict[str, Any], serial["collection"])
    xdist_collection = cast(dict[str, Any], xdist["collection"])
    for field in (
        "selected_count",
        "deselected_count",
        "nodeid_digest",
        "contract_digest",
        "collected_count",
        "collected_nodeid_digest",
        "collected_contract_digest",
    ):
        if type(serial_collection.get(field)) is not type(xdist_collection.get(field)) or (
            serial_collection.get(field) != xdist_collection.get(field)
        ):
            raise BenchmarkError(f"serial and xdist {phase} {field} evidence differs")
    serial_outcomes = _outcomes(serial, f"serial {phase}")
    xdist_outcomes = _outcomes(xdist, f"xdist {phase}")
    if serial_outcomes != xdist_outcomes:
        raise BenchmarkError(f"serial and xdist {phase} exact outcomes differ")
    return {
        "selected_count": serial_collection["selected_count"],
        "outcomes": dict(sorted(serial_outcomes.items())),
    }


def _phase_seconds(timing: dict[str, Any], field: str, label: str) -> float:
    pytest_record = timing.get("pytest")
    if not isinstance(pytest_record, dict):
        raise BenchmarkError(f"{label} needs pytest timing evidence")
    return _nonnegative_number(pytest_record.get(field), f"{label} {field}")


def record_run(
    execution: str,
    *,
    started_ns: int,
    finished_ns: int,
    repository: str,
    sha: str,
    tree_sha: str,
    run_id: str,
    run_attempt: int,
    job: str,
    runner_os: str,
    runner_image_os: str,
    runner_image_version: str,
) -> dict[str, Any]:
    """Record the outer canonical plan interval separately from pytest timing."""
    if execution not in {"serial", "xdist"}:
        raise BenchmarkError("canonical run execution must be serial or xdist")
    if (
        isinstance(started_ns, bool)
        or isinstance(finished_ns, bool)
        or not isinstance(started_ns, int)
        or not isinstance(finished_ns, int)
        or started_ns < 0
        or finished_ns <= started_ns
    ):
        raise BenchmarkError("canonical run nanosecond interval is invalid")
    text_fields = {
        "repository": repository,
        "sha": sha,
        "tree_sha": tree_sha,
        "run_id": run_id,
        "job": job,
        "runner_os": runner_os,
        "runner_image_os": runner_image_os,
        "runner_image_version": runner_image_version,
    }
    if any(not isinstance(value, str) or not value.strip() for value in text_fields.values()):
        raise BenchmarkError("canonical run GitHub identity fields must be non-empty")
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha.lower()):
        raise BenchmarkError("canonical run GitHub SHA must be a full commit identity")
    if len(tree_sha) != 40 or any(
        character not in "0123456789abcdef" for character in tree_sha.lower()
    ):
        raise BenchmarkError("canonical run Git tree SHA must be a full tree identity")
    if not run_id.isdecimal():
        raise BenchmarkError("canonical run GitHub run ID must be numeric")
    if job != BENCHMARK_PAIR_JOB_ID:
        raise BenchmarkError(f"canonical run GitHub job must be {BENCHMARK_PAIR_JOB_ID}")
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
        raise BenchmarkError("canonical run GitHub attempt must be a positive integer")
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "execution": execution,
        "started_ns": started_ns,
        "finished_ns": finished_ns,
        "wall_seconds": round((finished_ns - started_ns) / 1_000_000_000, 6),
        "github": {
            **text_fields,
            "run_attempt": run_attempt,
        },
    }


def _run_record(directory: Path, execution: str) -> dict[str, Any]:
    report = _load_json(directory / "run.json", f"{execution} canonical run")
    _require_schema_version(
        report.get("schema_version"), RUN_SCHEMA_VERSION, f"{execution} canonical run"
    )
    if report.get("execution") != execution:
        raise BenchmarkError(f"{execution} canonical run has the wrong execution")
    started_ns = report.get("started_ns")
    finished_ns = report.get("finished_ns")
    wall_seconds = report.get("wall_seconds")
    if (
        type(started_ns) is not int
        or type(finished_ns) is not int
        or finished_ns <= started_ns
        or _nonnegative_number(wall_seconds, f"{execution} canonical wall") <= 0
        or not math.isclose(
            cast(float, wall_seconds),
            (finished_ns - started_ns) / 1_000_000_000,
            abs_tol=0.000001,
        )
    ):
        raise BenchmarkError(f"{execution} canonical run interval is inconsistent")
    github = report.get("github")
    if not isinstance(github, dict):
        raise BenchmarkError(f"{execution} canonical run omits GitHub identity")
    expected_fields = {
        "repository",
        "sha",
        "tree_sha",
        "run_id",
        "run_attempt",
        "job",
        "runner_os",
        "runner_image_os",
        "runner_image_version",
    }
    if (
        set(github) != expected_fields
        or github.get("runner_os") != "Linux"
        or github.get("job") != BENCHMARK_PAIR_JOB_ID
    ):
        raise BenchmarkError(f"{execution} canonical run is not Linux GitHub evidence")
    record_run(
        execution,
        started_ns=started_ns,
        finished_ns=finished_ns,
        repository=cast(str, github.get("repository")),
        sha=cast(str, github.get("sha")),
        tree_sha=cast(str, github.get("tree_sha")),
        run_id=cast(str, github.get("run_id")),
        run_attempt=cast(int, github.get("run_attempt")),
        job=cast(str, github.get("job")),
        runner_os=cast(str, github.get("runner_os")),
        runner_image_os=cast(str, github.get("runner_image_os")),
        runner_image_version=cast(str, github.get("runner_image_version")),
    )
    return report


def _environment_identity(timings: list[dict[str, Any]], label: str) -> dict[str, Any]:
    environments = [timing.get("environment") for timing in timings]
    if not environments or not isinstance(environments[0], dict):
        raise BenchmarkError(f"{label} timings omit environment identity")
    if any(environment != environments[0] for environment in environments[1:]):
        raise BenchmarkError(f"{label} canonical phases use different environments")
    environment = cast(dict[str, Any], environments[0])
    expected_fields = {
        "django_settings_module",
        "python",
        "platform",
        "packages",
        "processor_count",
    }
    if set(environment) != expected_fields:
        raise BenchmarkError(f"{label} environment identity is incomplete")
    if environment.get("django_settings_module") != "unset":
        raise BenchmarkError(f"{label} canonical phases must use default Django settings")
    if not isinstance(environment.get("packages"), dict):
        raise BenchmarkError(f"{label} environment package identity is incomplete")
    return environment


def _residue_evidence(directory: Path, label: str) -> dict[str, Any]:
    report = _load_json(directory / "ray-residue.json", f"{label} Ray residue evidence")
    _require_schema_version(
        report.get("schema_version"),
        RAY_RESIDUE_SCHEMA_VERSION,
        f"{label} Ray residue evidence",
    )
    if (
        set(report)
        != {
            "schema_version",
            "valid",
            "errors",
            "additions",
            "owned_temp",
            "guard",
        }
        or report.get("valid") is not True
        or report.get("errors") != []
    ):
        raise BenchmarkError(f"{label} canonical plan left Ray residue")
    additions = report.get("additions")
    if (
        not isinstance(additions, dict)
        or set(additions) != {"processes", "listeners", "shared_memory", "global_temp"}
        or any(additions.get(field) != [] for field in additions)
    ):
        raise BenchmarkError(f"{label} Ray residue additions are not empty")
    owned_temp = report.get("owned_temp")
    if not isinstance(owned_temp, dict) or set(owned_temp) != {
        "entries_observed",
        "scan_limit",
        "scan_truncated",
        "scan_error",
        "removed",
        "exists_after",
    }:
        raise BenchmarkError(f"{label} owned Ray temporary evidence is incomplete")
    entries_observed = owned_temp.get("entries_observed")
    scan_limit = owned_temp.get("scan_limit")
    scan_truncated = owned_temp.get("scan_truncated")
    scan_error = owned_temp.get("scan_error")
    complete_scan = (
        scan_error is None
        and type(entries_observed) is int
        and 0 <= entries_observed <= OWNED_TEMP_SCAN_LIMIT + 1
        and type(scan_truncated) is bool
        and scan_truncated == (entries_observed == OWNED_TEMP_SCAN_LIMIT + 1)
    )
    failed_scan = (
        isinstance(scan_error, str)
        and 0 < len(scan_error) <= 500
        and entries_observed is None
        and scan_truncated is None
    )
    if (
        type(scan_limit) is not int
        or scan_limit != OWNED_TEMP_SCAN_LIMIT
        or not (complete_scan or failed_scan)
        or owned_temp.get("removed") is not True
        or owned_temp.get("exists_after") is not False
    ):
        raise BenchmarkError(f"{label} owned Ray temporary state still exists")
    guard = report.get("guard")
    if not isinstance(guard, dict) or set(guard) != {
        "body_returncode",
        "cleanup_returncode",
    }:
        raise BenchmarkError(f"{label} canonical guard evidence is incomplete")
    if any(type(guard.get(field)) is not int or guard.get(field) != 0 for field in guard):
        raise BenchmarkError(f"{label} canonical body or cleanup did not complete successfully")
    return report


def compare_pair(
    serial_directory: Path,
    xdist_directory: Path,
    *,
    sample: str,
    order: str,
) -> dict[str, Any]:
    """Compare one same-runner serial/xdist sample pair."""
    if not sample.strip():
        raise BenchmarkError("paired benchmark sample identity must be non-empty")
    if order not in {"serial-xdist", "xdist-serial"}:
        raise BenchmarkError("paired benchmark order must be serial-xdist or xdist-serial")
    serial_timings = [_load_phase(serial_directory, phase, "serial") for phase in PHASES]
    xdist_timings = [_load_phase(xdist_directory, phase, "xdist") for phase in PHASES]
    _validate_inventory(serial_directory, serial_timings, "serial")
    _validate_inventory(xdist_directory, xdist_timings, "xdist")
    serial_environment = _environment_identity(serial_timings, "serial")
    xdist_environment = _environment_identity(xdist_timings, "xdist")
    if serial_environment != xdist_environment:
        raise BenchmarkError("serial and xdist canonical environments differ")
    serial_run = _run_record(serial_directory, "serial")
    xdist_run = _run_record(xdist_directory, "xdist")
    if serial_run.get("github") != xdist_run.get("github"):
        raise BenchmarkError("serial and xdist plans must share one GitHub execution identity")
    serial_residue = _residue_evidence(serial_directory, "serial")
    xdist_residue = _residue_evidence(xdist_directory, "xdist")
    source_digests = {
        _source_digest(timing, f"{execution} {phase}")
        for execution, timings in (("serial", serial_timings), ("xdist", xdist_timings))
        for phase, timing in zip(PHASES, timings, strict=True)
    }
    if len(source_digests) != 1:
        raise BenchmarkError("paired benchmark phases do not use the same source fence")
    parity = {
        phase: _phase_parity(serial, xdist, phase)
        for phase, serial, xdist in zip(PHASES, serial_timings, xdist_timings, strict=True)
    }
    serial_coverage = _coverage(serial_directory, "serial")
    xdist_coverage = _coverage(xdist_directory, "xdist")
    _compare_coverage(serial_coverage, xdist_coverage)
    serial_wall = _phase_seconds(
        serial_timings[0], "execution_wall_seconds", "serial hermetic timing"
    )
    xdist_wall = _phase_seconds(xdist_timings[0], "execution_wall_seconds", "xdist hermetic timing")
    if serial_wall <= 0 or xdist_wall <= 0:
        raise BenchmarkError("serial and xdist hermetic execution wall times must be positive")
    serial_work = sum(
        _phase_seconds(serial_timings[0], field, "serial hermetic timing")
        for field in ("setup_phase_seconds", "call_phase_seconds", "teardown_phase_seconds")
    )
    xdist_work = sum(
        _phase_seconds(xdist_timings[0], field, "xdist hermetic timing")
        for field in ("setup_phase_seconds", "call_phase_seconds", "teardown_phase_seconds")
    )
    improvement = 100.0 * (serial_wall - xdist_wall) / serial_wall
    serial_plan_wall = _nonnegative_number(
        serial_run.get("wall_seconds"), "serial canonical plan wall"
    )
    xdist_plan_wall = _nonnegative_number(
        xdist_run.get("wall_seconds"), "xdist canonical plan wall"
    )
    canonical_improvement = 100.0 * (serial_plan_wall - xdist_plan_wall) / serial_plan_wall
    return {
        "schema_version": PAIR_SCHEMA_VERSION,
        "sample": sample.strip(),
        "order": order,
        "source_digest": next(iter(source_digests)),
        "github": serial_run["github"],
        "environment": serial_environment,
        "integrity": {"valid": True, "errors": []},
        "canonical_parity": parity,
        "performance": {
            "metric": "hermetic pytest execution wall",
            "serial_seconds": round(serial_wall, 6),
            "xdist_seconds": round(xdist_wall, 6),
            "improvement_percent": round(improvement, 6),
            "serial_summed_test_work_seconds": round(serial_work, 6),
            "xdist_summed_test_work_seconds": round(xdist_work, 6),
            "serial_canonical_plan_seconds": round(serial_plan_wall, 6),
            "xdist_canonical_plan_seconds": round(xdist_plan_wall, 6),
            "canonical_plan_improvement_percent": round(canonical_improvement, 6),
            "hermetic_metric_excludes": [
                "workflow queue",
                "environment setup",
                "serial support phases",
                "coverage reporting",
            ],
            "canonical_plan_scope": (
                "all canonical phases, merged evidence, and final coverage gates; "
                "workflow queue and environment setup excluded"
            ),
        },
        "external_intervals": {
            "serial": serial_timings[0].get("external"),
            "xdist": xdist_timings[0].get("external"),
        },
        "ray_cleanup": {
            "serial": serial_residue,
            "xdist": xdist_residue,
        },
        "coverage": {
            "non_regression": True,
            "serial": {key: value for key, value in serial_coverage.items() if key != "files"},
            "xdist": {key: value for key, value in xdist_coverage.items() if key != "files"},
        },
    }


def aggregate_pairs(
    pairs: list[dict[str, Any]],
    *,
    repository: str,
    sha: str,
    tree_sha: str,
) -> dict[str, Any]:
    """Build a three-or-more-sample retention decision."""
    if not repository.strip():
        raise BenchmarkError("aggregate repository identity must be non-empty")
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha.lower()):
        raise BenchmarkError("aggregate SHA must be the exact current Git commit")
    if len(tree_sha) != 40 or any(
        character not in "0123456789abcdef" for character in tree_sha.lower()
    ):
        raise BenchmarkError("aggregate tree SHA must be the exact current Git tree")
    if len(pairs) < 3:
        raise BenchmarkError("retention evidence needs at least three fresh-runner pairs")
    samples: set[str] = set()
    orders: set[str] = set()
    sources: set[str] = set()
    repositories: set[str] = set()
    github_shas: set[str] = set()
    github_tree_shas: set[str] = set()
    run_ids: set[str] = set()
    run_orders: list[tuple[int, str]] = []
    environments: set[str] = set()
    runner_image_oses: set[str] = set()
    runner_image_versions: set[str] = set()
    parity_identities: set[str] = set()
    coverage_identities: set[str] = set()
    serial_seconds: list[float] = []
    xdist_seconds: list[float] = []
    serial_plan_seconds: list[float] = []
    xdist_plan_seconds: list[float] = []
    rendered_samples: list[dict[str, object]] = []
    for pair in pairs:
        _require_schema_version(
            pair.get("schema_version"), PAIR_SCHEMA_VERSION, "paired benchmark evidence"
        )
        if pair.get("integrity") != {"valid": True, "errors": []}:
            raise BenchmarkError("paired benchmark evidence failed integrity")
        sample = pair.get("sample")
        order = pair.get("order")
        source = pair.get("source_digest")
        if not isinstance(sample, str) or not sample or sample in samples:
            raise BenchmarkError("paired benchmark sample identities must be unique")
        if order not in {"serial-xdist", "xdist-serial"}:
            raise BenchmarkError("paired benchmark evidence has an invalid order")
        if not isinstance(source, str) or len(source) != 64:
            raise BenchmarkError("paired benchmark evidence has an invalid source digest")
        coverage = pair.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("non_regression") is not True:
            raise BenchmarkError("paired benchmark coverage is not equal or better")
        parity = pair.get("canonical_parity")
        if not isinstance(parity, dict) or set(parity) != set(PHASES):
            raise BenchmarkError("paired benchmark omits canonical phase parity")
        github = pair.get("github")
        if not isinstance(github, dict) or set(github) != {
            "repository",
            "sha",
            "tree_sha",
            "run_id",
            "run_attempt",
            "job",
            "runner_os",
            "runner_image_os",
            "runner_image_version",
        }:
            raise BenchmarkError("paired benchmark omits exact GitHub execution identity")
        pair_repository = github.get("repository")
        github_sha = github.get("sha")
        github_tree_sha = github.get("tree_sha")
        run_id = github.get("run_id")
        run_attempt = github.get("run_attempt")
        runner_image_os = github.get("runner_image_os")
        runner_image_version = github.get("runner_image_version")
        if (
            not isinstance(pair_repository, str)
            or not pair_repository
            or not isinstance(github_sha, str)
            or len(github_sha) != 40
            or any(character not in "0123456789abcdef" for character in github_sha.lower())
            or not isinstance(github_tree_sha, str)
            or len(github_tree_sha) != 40
            or any(character not in "0123456789abcdef" for character in github_tree_sha.lower())
            or not isinstance(run_id, str)
            or not run_id.isdecimal()
            or type(run_attempt) is not int
            or run_attempt < 1
            or github.get("job") != BENCHMARK_PAIR_JOB_ID
            or github.get("runner_os") != "Linux"
            or not isinstance(runner_image_os, str)
            or not runner_image_os
            or not isinstance(runner_image_version, str)
            or not runner_image_version
        ):
            raise BenchmarkError("paired benchmark GitHub execution identity is invalid")
        environment = pair.get("environment")
        if not isinstance(environment, dict):
            raise BenchmarkError("paired benchmark omits environment/package identity")
        performance = pair.get("performance")
        if not isinstance(performance, dict):
            raise BenchmarkError("paired benchmark omits performance evidence")
        serial = _nonnegative_number(performance.get("serial_seconds"), "serial pair time")
        xdist = _nonnegative_number(performance.get("xdist_seconds"), "xdist pair time")
        if serial <= 0 or xdist <= 0:
            raise BenchmarkError("serial and xdist pair times must be positive")
        serial_plan = _nonnegative_number(
            performance.get("serial_canonical_plan_seconds"), "serial canonical plan time"
        )
        xdist_plan = _nonnegative_number(
            performance.get("xdist_canonical_plan_seconds"), "xdist canonical plan time"
        )
        if serial_plan <= 0 or xdist_plan <= 0:
            raise BenchmarkError("canonical plan times must be positive")
        samples.add(sample)
        orders.add(cast(str, order))
        sources.add(source)
        repositories.add(pair_repository)
        github_shas.add(github_sha)
        github_tree_shas.add(github_tree_sha)
        if run_id in run_ids:
            raise BenchmarkError("retention evidence needs three distinct GitHub run IDs")
        run_ids.add(run_id)
        run_orders.append((int(run_id), cast(str, order)))
        environments.add(json.dumps(environment, separators=(",", ":"), sort_keys=True))
        runner_image_oses.add(runner_image_os)
        runner_image_versions.add(runner_image_version)
        parity_identities.add(json.dumps(parity, separators=(",", ":"), sort_keys=True))
        coverage_identities.add(json.dumps(coverage, separators=(",", ":"), sort_keys=True))
        serial_seconds.append(serial)
        xdist_seconds.append(xdist)
        serial_plan_seconds.append(serial_plan)
        xdist_plan_seconds.append(xdist_plan)
        rendered_samples.append(
            {
                "sample": sample,
                "order": order,
                "github_run_id": run_id,
                "github_run_attempt": run_attempt,
                "serial_seconds": serial,
                "xdist_seconds": xdist,
                "serial_canonical_plan_seconds": serial_plan,
                "xdist_canonical_plan_seconds": xdist_plan,
            }
        )
    if len(sources) != 1:
        raise BenchmarkError("fresh-runner pairs do not use the same source fence")
    if orders != {"serial-xdist", "xdist-serial"}:
        raise BenchmarkError("fresh-runner pairs must alternate serial and xdist order")
    ordered_modes = [order for _run_id, order in sorted(run_orders)]
    if any(left == right for left, right in zip(ordered_modes, ordered_modes[1:], strict=False)):
        raise BenchmarkError("fresh-runner pairs must alternate serial and xdist order")
    if len(repositories) != 1 or len(github_shas) != 1 or len(github_tree_shas) != 1:
        raise BenchmarkError("fresh-runner pairs do not use the same repository commit and tree")
    if repositories != {repository} or github_shas != {sha} or github_tree_shas != {tree_sha}:
        raise BenchmarkError("paired evidence does not match the aggregate checkout identity")
    if len(run_ids) < 3:
        raise BenchmarkError("retention evidence needs three distinct GitHub run IDs")
    if len(environments) != 1:
        raise BenchmarkError("fresh-runner pairs use different environment/package identities")
    if len(runner_image_oses) != 1:
        raise BenchmarkError("fresh-runner pairs use different runner image operating systems")
    if len(parity_identities) != 1:
        raise BenchmarkError("canonical node outcomes differ across GitHub run IDs")
    if len(coverage_identities) != 1:
        raise BenchmarkError("combined coverage line sets differ across GitHub run IDs")
    environment_identity = json.loads(next(iter(environments)))
    runner_image_os = next(iter(runner_image_oses))
    serial_median = float(statistics.median(serial_seconds))
    xdist_median = float(statistics.median(xdist_seconds))
    improvement = 100.0 * (serial_median - xdist_median) / serial_median
    serial_plan_median = float(statistics.median(serial_plan_seconds))
    xdist_plan_median = float(statistics.median(xdist_plan_seconds))
    plan_improvement = 100.0 * (serial_plan_median - xdist_plan_median) / serial_plan_median
    reasons: list[str] = []
    if improvement + 1e-9 < RETENTION_IMPROVEMENT_MIN:
        reasons.append(
            f"median hermetic execution-wall improvement is below {RETENTION_IMPROVEMENT_MIN:.0f}%"
        )
    if plan_improvement < -1e-9:
        reasons.append("median full canonical plan wall regresses under xdist")
    eligible = not reasons
    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "source_digest": next(iter(sources)),
        "github": {
            "repository": repository,
            "sha": sha,
            "tree_sha": tree_sha,
            "pair_job": BENCHMARK_PAIR_JOB_ID,
            "runner_os": "Linux",
        },
        "runner_image": {
            "os": runner_image_os,
            "versions": sorted(runner_image_versions),
        },
        "environment": environment_identity,
        "sample_count": len(pairs),
        "samples": sorted(rendered_samples, key=lambda record: cast(str, record["sample"])),
        "integrity": {"valid": True, "errors": []},
        "retention": {
            "eligible": eligible,
            "decision": "retain bounded xdist" if eligible else "reject bounded xdist",
            "minimum_improvement_percent": RETENTION_IMPROVEMENT_MIN,
            "serial_median_seconds": round(serial_median, 6),
            "xdist_median_seconds": round(xdist_median, 6),
            "median_improvement_percent": round(improvement, 6),
            "serial_canonical_plan_median_seconds": round(serial_plan_median, 6),
            "xdist_canonical_plan_median_seconds": round(xdist_plan_median, 6),
            "canonical_plan_median_improvement_percent": round(plan_improvement, 6),
            "reasons": reasons,
        },
    }


def render_pair_markdown(report: dict[str, Any]) -> str:
    performance = cast(dict[str, Any], report["performance"])
    coverage = cast(dict[str, Any], report["coverage"])
    serial_coverage = cast(dict[str, Any], coverage["serial"])
    xdist_coverage = cast(dict[str, Any], coverage["xdist"])
    return "\n".join(
        [
            f"# Pytest-xdist pair `{report['sample']}`",
            "",
            f"Run order: `{report['order']}`.",
            "",
            "| Metric | Serial | xdist |",
            "|---|---:|---:|",
            f"| Hermetic execution wall | {performance['serial_seconds']:.3f}s | "
            f"{performance['xdist_seconds']:.3f}s |",
            f"| Full canonical plan wall | "
            f"{performance['serial_canonical_plan_seconds']:.3f}s | "
            f"{performance['xdist_canonical_plan_seconds']:.3f}s |",
            f"| django-ray source covered lines | {serial_coverage['covered_lines']} | "
            f"{xdist_coverage['covered_lines']} |",
            f"| django-ray source missing lines | {serial_coverage['missing_lines']} | "
            f"{xdist_coverage['missing_lines']} |",
            f"| Combined testproject coverage | "
            f"{serial_coverage['testproject']['percent']:.2f}% | "
            f"{xdist_coverage['testproject']['percent']:.2f}% |",
            "",
            f"Hermetic improvement: **{performance['improvement_percent']:.2f}%**.",
            f"Full-plan improvement: **{performance['canonical_plan_improvement_percent']:.2f}%**.",
            "Queue and environment setup are excluded from both wall metrics; the full plan "
            "includes every canonical phase, evidence validation, and final coverage reporting.",
            "",
        ]
    )


def render_aggregate_markdown(report: dict[str, Any]) -> str:
    retention = cast(dict[str, Any], report["retention"])
    github = cast(dict[str, Any], report["github"])
    runner_image = cast(dict[str, Any], report["runner_image"])
    runner_versions = ", ".join(
        f"`{version}`" for version in cast(list[str], runner_image["versions"])
    )
    lines = [
        "# Bounded pytest-xdist retention decision",
        "",
        f"Decision: **{retention['decision']}**.",
        f"Candidate commit: `{github['repository']}@{github['sha']}`.",
        f"Candidate Git tree: `{github['tree_sha']}`.",
        f"Source digest: `{report['source_digest']}`.",
        f"Runner image: `{runner_image['os']}` ({runner_versions}).",
        "",
        "| Sample | Run/attempt | Order | Hermetic serial | Hermetic xdist | Plan serial | Plan xdist |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for sample in cast(list[dict[str, Any]], report["samples"]):
        lines.append(
            f"| `{sample['sample']}` | `{sample['github_run_id']}/{sample['github_run_attempt']}` | "
            f"`{sample['order']}` | "
            f"{sample['serial_seconds']:.3f}s | {sample['xdist_seconds']:.3f}s | "
            f"{sample['serial_canonical_plan_seconds']:.3f}s | "
            f"{sample['xdist_canonical_plan_seconds']:.3f}s |"
        )
    lines.extend(
        [
            "",
            f"Median serial wall: **{retention['serial_median_seconds']:.3f}s**.",
            f"Median xdist wall: **{retention['xdist_median_seconds']:.3f}s**.",
            f"Median improvement: **{retention['median_improvement_percent']:.2f}%** "
            f"(required: {retention['minimum_improvement_percent']:.0f}%).",
            f"Median full-plan serial wall: "
            f"**{retention['serial_canonical_plan_median_seconds']:.3f}s**.",
            f"Median full-plan xdist wall: "
            f"**{retention['xdist_canonical_plan_median_seconds']:.3f}s**.",
            f"Median full-plan improvement: "
            f"**{retention['canonical_plan_median_improvement_percent']:.2f}%** "
            "(must not regress).",
            "",
        ]
    )
    for reason in retention["reasons"]:
        lines.append(f"- {reason}")
    if retention["reasons"]:
        lines.append("")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="create an isolated output directory")
    prepare.add_argument("--output-dir", type=Path, required=True)
    record = subparsers.add_parser("record-run", help="record one outer canonical plan interval")
    record.add_argument("--execution", choices=("serial", "xdist"), required=True)
    record.add_argument("--started-ns", type=int, required=True)
    record.add_argument("--finished-ns", type=int, required=True)
    record.add_argument("--repository", required=True)
    record.add_argument("--sha", required=True)
    record.add_argument("--tree-sha", required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--run-attempt", type=int, required=True)
    record.add_argument("--job", required=True)
    record.add_argument("--runner-os", required=True)
    record.add_argument("--runner-image-os", required=True)
    record.add_argument("--runner-image-version", required=True)
    record.add_argument("--output", type=Path, required=True)
    compare = subparsers.add_parser("compare", help="validate one serial/xdist pair")
    compare.add_argument("--serial-dir", type=Path, required=True)
    compare.add_argument("--xdist-dir", type=Path, required=True)
    compare.add_argument("--sample", required=True)
    compare.add_argument("--order", choices=("serial-xdist", "xdist-serial"), required=True)
    compare.add_argument("--json-output", type=Path, required=True)
    compare.add_argument("--markdown-output", type=Path, required=True)
    aggregate = subparsers.add_parser("aggregate", help="build the retention decision")
    aggregate.add_argument("--pair", type=Path, action="append", required=True)
    aggregate.add_argument("--repository", required=True)
    aggregate.add_argument("--sha", required=True)
    aggregate.add_argument("--tree-sha", required=True)
    aggregate.add_argument("--json-output", type=Path, required=True)
    aggregate.add_argument("--markdown-output", type=Path, required=True)
    aggregate.add_argument("--require-retention", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            prepare_output(arguments.output_dir)
            return 0
        if arguments.command == "record-run":
            if arguments.sha != _git_head():
                raise BenchmarkError("recorded run SHA differs from git rev-parse HEAD")
            if arguments.tree_sha != _git_tree():
                raise BenchmarkError("recorded run tree SHA differs from git rev-parse HEAD^{tree}")
            report = record_run(
                arguments.execution,
                started_ns=arguments.started_ns,
                finished_ns=arguments.finished_ns,
                repository=arguments.repository,
                sha=arguments.sha,
                tree_sha=arguments.tree_sha,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                job=arguments.job,
                runner_os=arguments.runner_os,
                runner_image_os=arguments.runner_image_os,
                runner_image_version=arguments.runner_image_version,
            )
            _write_text(arguments.output, _render_json(report))
            return 0
        if arguments.command == "compare":
            report = compare_pair(
                arguments.serial_dir,
                arguments.xdist_dir,
                sample=arguments.sample,
                order=arguments.order,
            )
            _write_text(arguments.json_output, _render_json(report))
            _write_text(arguments.markdown_output, render_pair_markdown(report))
            return 0
        pairs = [_load_json(path, "paired benchmark evidence") for path in arguments.pair]
        if arguments.sha != _git_head():
            raise BenchmarkError("aggregate SHA differs from git rev-parse HEAD")
        if arguments.tree_sha != _git_tree():
            raise BenchmarkError("aggregate tree SHA differs from git rev-parse HEAD^{tree}")
        report = aggregate_pairs(
            pairs,
            repository=arguments.repository,
            sha=arguments.sha,
            tree_sha=arguments.tree_sha,
        )
        _write_text(arguments.json_output, _render_json(report))
        _write_text(arguments.markdown_output, render_aggregate_markdown(report))
        if arguments.require_retention and not report["retention"]["eligible"]:
            return 3
        return 0
    except BenchmarkError as error:
        print(f"test suite benchmark: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
