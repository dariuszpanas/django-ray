"""Describe and execute finite, source-owned Linux regression assertions.

Admission, service provisioning, ordering and resource release belong to the
caller. This module runs exactly one stage per invocation and never provisions
infrastructure. Pytest selection and receipts remain owned by the taxonomy.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import test_suite_inventory as inventory  # noqa: E402
from scripts.coverage_debt import CoveragePhase, run_coverage_phase  # noqa: E402
from scripts.require_linux import require_linux  # noqa: E402
from scripts.test_suite_taxonomy import (  # noqa: E402
    CollectedTest,
    InventoryError,
    Manifest,
    collection_contract_digest,
    load_manifest,
    nodeid_digest,
    require_unique_nodeids,
)

SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_STAGE_ARTIFACT_BYTES = 3 * MAX_ARTIFACT_BYTES
MAX_RAW_ARTIFACT_BYTES = 64 * MAX_ARTIFACT_BYTES
TAXONOMY = ROOT / ".github/test-suite-taxonomy.json"
SAMPLE_COVERAGE_PATHS = frozenset(
    {"testproject/api.py", "testproject/views.py", "testproject/urls.py"}
)


@dataclass(frozen=True)
class Stage:
    """A resource request for the caller, never an implicit admission decision."""

    id: str
    timeout_seconds: int
    cpu_millis: int = 1000
    memory_mib: int = 2048
    pids: int = 256
    scratch_mib: int = 2048
    lane: str | None = None
    coverage: str | None = None
    services: tuple[str, ...] = ()


STAGES = (
    Stage("collection", 180),
    Stage("static", 600),
    Stage("hermetic", 1200, lane="hermetic", coverage="source"),
    Stage("sqlite-django", 1200, lane="sqlite-django", coverage="source"),
    Stage("local-ray", 900, 3000, 6144, 1024, 7168, "local-ray", "source"),
    Stage(
        "compiled-graph-opt-in",
        300,
        3000,
        6144,
        1024,
        7168,
        "compiled-graph-opt-in",
        "source",
    ),
    Stage("postgresql", 1200, lane="postgresql", coverage="source", services=("postgresql",)),
    Stage("testproject", 600, lane="testproject-contract", coverage="testproject"),
    Stage("docs-build", 600, 2000, 4096, scratch_mib=4096),
)
AGGREGATE = Stage("aggregate", 300)
PARTITION_LANES = tuple(stage.lane for stage in STAGES if stage.coverage == "source")


def stage_record(stage: Stage) -> dict[str, Any]:
    record = asdict(stage)
    record["services"] = list(stage.services)
    return record


def stage_by_id(stage_id: str) -> Stage:
    for stage in (*STAGES, AGGREGATE):
        if stage.id == stage_id:
            return stage
    raise InventoryError(f"unknown Linux test stage: {stage_id}")


def source_identity(root: Path, source_manifest: Path | None) -> dict[str, object]:
    """Use the same sealed source identity as the existing inventory runner."""
    if source_manifest is None:
        return inventory._source_digest(root, root / ".github/test-suite-taxonomy.json")
    from scripts.test_suite_source import source_digest

    return source_digest(root, root / ".github/test-suite-taxonomy.json", source_manifest)


def catalogue(manifest: Manifest) -> dict[str, Any]:
    records = []
    for stage in (*STAGES, AGGREGATE):
        record = stage_record(stage)
        record["linux_required"] = True
        record["execution"] = "serial"
        record["resource_enforcement"] = "caller must admit and enforce all declared limits"
        if stage.lane:
            group = manifest.group(stage.lane)
            record["selection"] = group.selection.as_mapping()
            record["skip_policy"] = asdict(group.skip_policy)
        records.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "current-interpreter Linux regression plus real PostgreSQL and sample boundary",
        "complete_hosted_ci": False,
        "stages": records,
        "partition": {
            "reference_lane": "supported-python",
            "lanes": list(PARTITION_LANES),
            "intentional_rerun": "testproject-contract",
        },
        "coverage_floors": {"source": 95, "worker": 90, "ray_job": 90, "testproject": 80},
        "prerequisites": {
            "environment": "uv sync --frozen --extra postgres; npm ci --ignore-scripts",
            "executables": ["node", "npm", "make", "git", "kubectl"],
            "kubectl_use": "local Kustomize rendering only; no cluster access",
            "source_manifest": "required for immutable archives without Git metadata",
        },
        "artifact_limits": {
            "single_bytes": MAX_ARTIFACT_BYTES,
            "stage_total_bytes": MAX_STAGE_ARTIFACT_BYTES,
            "uncompressed_bytes": MAX_RAW_ARTIFACT_BYTES,
        },
        "separate_required_evidence": [
            {
                "id": "live-cluster",
                "lane": "live-cluster",
                "boundary": "admitted two-node Ray cluster",
            },
            {"id": "application", "boundary": "source-owned application qualification matrix"},
            {
                "id": "hosted-matrix",
                "boundary": "supported Python/dependencies, Compose, Ray Data and installed wheel",
            },
        ],
    }


def partition_evidence(items: list[CollectedTest], manifest: Manifest) -> dict[str, Any]:
    """Prove exact coverage of the reference selection with no duplicate owner."""
    require_unique_nodeids([item.nodeid for item in items], "Linux plan collection")
    expected = {
        item.nodeid for item in items if manifest.group("supported-python").selection.matches(item)
    }
    owned: dict[str, str] = {}
    selections = {}
    for lane in PARTITION_LANES:
        assert lane is not None
        selected = [item for item in items if manifest.group(lane).selection.matches(item)]
        if not selected:
            raise InventoryError(f"required partition lane is empty: {lane}")
        for item in selected:
            if item.nodeid in owned:
                raise InventoryError(f"partition has duplicate owner: {item.nodeid}")
            owned[item.nodeid] = lane
        selections[lane] = {
            "count": len(selected),
            "nodeid_digest": nodeid_digest([item.nodeid for item in selected]),
            "contract_digest": collection_contract_digest(selected),
        }
    if set(owned) != expected:
        raise InventoryError("partition does not equal the complete supported-python selection")
    return {
        "reference_lane": "supported-python",
        "selected_count": len(expected),
        "nodeid_digest": nodeid_digest(sorted(expected)),
        "disjoint": True,
        "complete": True,
        "selections": selections,
    }


def _write(path: Path, value: object) -> None:
    inventory._write_json(path, value)


def _read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise InventoryError(f"receipt exceeds byte limit: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InventoryError("receipt must be an object")
    return value


def _compressed(path: Path) -> bytes:
    if path.stat().st_size > MAX_RAW_ARTIFACT_BYTES:
        raise InventoryError(f"raw artifact exceeds byte limit: {path.name}")
    encoded = gzip.compress(path.read_bytes(), mtime=0)
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise InventoryError(f"compressed artifact exceeds byte limit: {path.name}")
    return encoded


def _uncompressed(path: Path) -> bytes:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise InventoryError(f"compressed artifact exceeds byte limit: {path.name}")
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAX_RAW_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_RAW_ARTIFACT_BYTES:
        raise InventoryError("expanded artifact exceeds byte limit")
    return raw


def export_artifacts(output: Path) -> dict[str, Any]:
    """Expose only bounded, compressed data and its content-addressed receipt."""
    artifacts: dict[str, Any] = {}
    for name in ("timing.json", "collection.json", "coverage.data", "summary.json"):
        path = output / name
        if not path.exists():
            continue
        raw_bytes = path.stat().st_size
        encoded = _compressed(path)
        compressed = output / (name + ".gz")
        compressed.write_bytes(encoded)
        artifacts[compressed.name] = {
            "bytes": len(encoded),
            "uncompressed_bytes": raw_bytes,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    if (
        sum(value["bytes"] for value in artifacts.values())
        > MAX_STAGE_ARTIFACT_BYTES - MAX_ARTIFACT_BYTES
    ):
        raise InventoryError("stage artifacts leave no room for the receipt and bounded log")
    return artifacts


def pytest_arguments(stage: Stage) -> list[str]:
    targets = (
        ["src"]
        if stage.coverage == "source"
        else ["testproject.api", "testproject.views", "testproject.urls"]
    )
    return [
        *(f"--cov={target}" for target in targets),
        "--cov-config=pyproject.toml",
        "--cov-report=",
        "--cov-fail-under=80" if stage.coverage == "testproject" else "--cov-fail-under=0",
        "--maxfail=1",
        "-q",
    ]


def _environment(stage: Stage) -> None:
    inventory._reject_pytest_environment()
    if any(
        os.environ.get(name, "").strip()
        for name in ("RAY_ADDRESS", "RAY_NAMESPACE", "DJANGO_RAY_LIVE_CLUSTER")
    ):
        raise InventoryError("Linux regression stages reject ambient external Ray configuration")
    if stage.id != "docs-build":
        # Collection-time module skips otherwise make the reference inventory
        # itself incomplete, before the taxonomy can account for missing work.
        for executable in ("node", "npm", "make", "git", "kubectl"):
            if shutil.which(executable) is None:
                raise InventoryError(f"required stage executable is unavailable: {executable}")
    if stage.id == "postgresql":
        for name in (
            "DATABASE_HOST",
            "DATABASE_NAME",
            "DATABASE_USER",
            "DATABASE_PASSWORD",
            "DATABASE_TEST_NAME",
        ):
            if not os.environ.get(name, "").strip():
                raise InventoryError(f"PostgreSQL stage requires explicit {name}")
        if os.environ["DATABASE_TEST_NAME"] == os.environ["DATABASE_NAME"]:
            raise InventoryError("PostgreSQL stage requires a distinct disposable test database")
        os.environ["DJANGO_SETTINGS_MODULE"] = "tests.postgres_settings"


def commands(stage: Stage) -> list[list[str]]:
    if stage.id == "static":
        return [
            ["make", "commit-policy-test"],
            ["ruff", "format", "--check", "."],
            ["ruff", "check", "."],
            ["ty", "check"],
            [sys.executable, "scripts/audit_runtime_dependencies.py"],
        ]
    if stage.id == "docs-build":
        return [["zensical", "build", "--strict", "--clean"], ["uv", "build"]]
    if stage.id == "testproject":
        return [[sys.executable, "testproject/manage.py", "check"]]
    return []


def execute(stage: Stage, output: Path, source_manifest: Path | None, evidence: Path | None) -> int:
    """The single contained child; all its descendants share the outer deadline."""
    require_linux()
    _environment(stage)
    os.environ["COVERAGE_FILE"] = str(output / "coverage.data")
    if stage.id == "aggregate":
        if evidence is None:
            raise InventoryError("aggregate requires --evidence-dir")
        aggregate_evidence(ROOT, evidence, output, source_manifest)
        return 0
    manifest = load_manifest(TAXONOMY)
    if stage.id == "collection":
        items = inventory.collect_tests(ROOT)
        _write(
            output / "collection.json",
            {
                "source": source_identity(ROOT, source_manifest),
                "items": [asdict(item) for item in items],
                "partition": partition_evidence(items, manifest),
            },
        )
        return 0
    for command in commands(stage):
        subprocess.run(command, cwd=ROOT, check=True)
    if stage.lane:
        arguments = [] if source_manifest is None else ["--source-manifest", str(source_manifest)]
        return inventory.main(
            [
                *arguments,
                "run",
                "--lane",
                stage.lane,
                "--observation",
                "linux-test-plan",
                "--variant",
                "current-interpreter",
                "--timing-output",
                str(output / "timing.json"),
                "--external-note",
                "Admission and provisioning are external to this source assertion",
                "--",
                *pytest_arguments(stage),
            ]
        )
    return 0


def _verified_artifact(directory: Path, receipt: dict[str, Any], name: str) -> bytes:
    record = receipt.get("artifacts", {}).get(name)
    path = directory / name
    if not isinstance(record, dict) or not path.is_file() or path.is_symlink():
        raise InventoryError(f"missing required artifact: {name}")
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise InventoryError(f"compressed artifact exceeds byte limit: {name}")
    if path.stat().st_size != record.get("bytes") or hashlib.sha256(
        path.read_bytes()
    ).hexdigest() != record.get("sha256"):
        raise InventoryError(f"artifact digest mismatch: {name}")
    raw = _uncompressed(path)
    if len(raw) != record.get("uncompressed_bytes"):
        raise InventoryError(f"artifact expanded size mismatch: {name}")
    return raw


def validate_receipts(
    root: Path, evidence: Path, source_manifest: Path | None
) -> tuple[dict[str, Any], list[tuple[Stage, dict[str, Any], bytes]]]:
    """Revalidate every receipt against current source and exact collection."""
    source = source_identity(root, source_manifest)
    manifest = load_manifest(root / ".github/test-suite-taxonomy.json")
    items = inventory.collect_tests(root)
    partition = partition_evidence(items, manifest)
    coverage = []
    summaries = {}
    for stage in STAGES:
        directory = evidence / stage.id
        receipt = _read(directory / "receipt.json")
        if (
            receipt.get("schema_version") != SCHEMA_VERSION
            or receipt.get("stage") != stage_record(stage)
            or receipt.get("source") != source
            or receipt.get("source_after") != source
            or receipt.get("outcome") != "passed"
            or receipt.get("process", {}).get("outcome") != "passed"
            or receipt.get("process", {}).get("exit_code") != 0
            or receipt.get("errors") != []
            or receipt.get("environment") != runtime_identity()
            or receipt.get("assertion_commands") != commands(stage)
        ):
            raise InventoryError(f"stage receipt is incomplete, failed or stale: {stage.id}")
        _validate_process(stage, receipt["process"])
        if stage.id == "collection":
            collected = json.loads(_verified_artifact(directory, receipt, "collection.json.gz"))
            if (
                collected.get("source") != source
                or collected.get("partition") != partition
                or collected.get("items")
                != json.loads(json.dumps([asdict(item) for item in items]))
            ):
                raise InventoryError("collection receipt differs from current exact collection")
        if stage.lane:
            timing = json.loads(_verified_artifact(directory, receipt, "timing.json.gz"))
            if timing.get("lane") != stage.lane or timing.get(
                "pytest_arguments"
            ) != pytest_arguments(stage):
                raise InventoryError(
                    f"stage changed its required selection or coverage command: {stage.id}"
                )
            inventory._validate_timing_record(timing, source, manifest, items)
            environment = receipt["environment"]
            if any(
                timing.get("environment", {}).get(nested) != environment[outer]
                for nested, outer in (
                    ("python", "python"),
                    ("packages", "packages"),
                    ("platform", "platform_description"),
                )
            ):
                raise InventoryError(f"timing environment differs from its stage: {stage.id}")
            summaries[stage.id] = timing["pytest"]["outcomes"]
            raw = _verified_artifact(directory, receipt, "coverage.data.gz")
            coverage.append((stage, receipt, raw))
    return {"source": source, "partition": partition, "outcomes": summaries}, coverage


def _validate_process(stage: Stage, process: dict[str, Any]) -> None:
    expected = {
        "name": stage.id,
        "selection": stage.lane or stage.id,
        "coverage_mode": stage.coverage or "none",
        "timeout_seconds": stage.timeout_seconds,
        "timed_out": False,
    }
    if any(process.get(key) != value for key, value in expected.items()) or any(
        key not in process or process[key] is not None
        for key in (
            "launch_error",
            "termination_error",
            "cleanup_error",
            "capture_error",
            "timing_error",
        )
    ):
        raise InventoryError(f"stage process contract is incomplete or inconsistent: {stage.id}")
    if process["timed_out"] is not False:
        raise InventoryError(f"stage process did not establish a clean deadline: {stage.id}")


def _remap_coverage(root: Path, output: Path, stage: Stage, receipt: dict[str, Any], raw: bytes):
    from coverage import CoverageData
    from coverage.exceptions import CoverageException

    path = output / f"input-{stage.id}.data"
    path.write_bytes(raw)
    shard = CoverageData(basename=str(path))
    try:
        shard.read()
    except CoverageException as error:
        raise InventoryError(f"stage coverage data is invalid: {stage.id}") from error
    if shard.has_arcs() or not shard.measured_files():
        raise InventoryError(f"stage must contain measured line coverage: {stage.id}")
    source_root = str(receipt["source_root"]).replace("\\", "/").rstrip("/") + "/"
    remapped = CoverageData(basename=str(output / f"mapped-{stage.id}.data"))
    paths = set()
    for measured in shard.measured_files():
        normalized = measured.replace("\\", "/")
        relative = normalized.removeprefix(source_root)
        allowed = (
            relative in SAMPLE_COVERAGE_PATHS
            if stage.coverage == "testproject"
            else relative.startswith("src/")
        )
        if not allowed or ".." in Path(relative).parts or not (root / relative).is_file():
            raise InventoryError(f"coverage shard contains paths outside its source: {stage.id}")
        paths.add(relative)
        remapped.add_lines({str((root / relative).resolve()): shard.lines(measured) or []})
    if stage.coverage == "testproject" and paths != SAMPLE_COVERAGE_PATHS:
        raise InventoryError("sample coverage must include every required module")
    return remapped


def aggregate_evidence(
    root: Path, evidence: Path, output: Path, source_manifest: Path | None
) -> None:
    """Combine validated line data with portable paths; never waive final floors."""
    from coverage import Coverage, CoverageData

    summary, shards = validate_receipts(root, evidence, source_manifest)
    combined = CoverageData(basename=str(output / "coverage.data"))
    sample = None
    for stage, receipt, raw in shards:
        shard = _remap_coverage(root, output, stage, receipt, raw)
        if stage.coverage == "testproject":
            shard.write()
            sample = Coverage(
                data_file=shard.data_filename(), config_file=str(root / "pyproject.toml")
            )
            sample.load()
        else:
            combined.update(shard)
    if sample is None:
        raise InventoryError("aggregate requires the independently verified sample coverage shard")
    combined.write()
    coverage = Coverage(
        data_file=str(output / "coverage.data"), config_file=str(root / "pyproject.toml")
    )
    coverage.load()
    checks = (
        ("source", None, 95),
        ("worker", ["src/django_ray/management/commands/django_ray_worker.py"], 90),
        ("ray_job", ["src/django_ray/runner/ray_job.py"], 90),
    )
    floors = {}
    for name, include, floor in checks:
        observed = coverage.report(include=include)
        floors[name] = {"observed_percent": observed, "minimum_percent": floor}
        # Match coverage.py's configured two-decimal gate semantics.
        if round(observed, 2) < floor:
            raise InventoryError(f"aggregate {name} coverage is below its required floor")
    observed = sample.report(include=sorted(SAMPLE_COVERAGE_PATHS))
    floors["testproject"] = {"observed_percent": observed, "minimum_percent": 80}
    if round(observed, 2) < 80:
        raise InventoryError("aggregate testproject coverage is below its required floor")
    summary.update({"coverage": floors, "complete_hosted_ci": False, "outcome": "passed"})
    _write(output / "summary.json", summary)


def run_stage(
    stage: Stage, output: Path, source_manifest: Path | None, evidence: Path | None
) -> int:
    require_linux()
    output = output.resolve()
    # New evidence per attempt prevents old data from surviving a failed run.
    if output.exists():
        raise InventoryError("stage output directory must not already exist")
    inventory._validate_output_path(ROOT, output, "stage output")
    if source_manifest is not None and output.is_relative_to(ROOT):
        raise InventoryError("archive execution requires stage output outside the source root")
    before = source_identity(ROOT, source_manifest)
    output.mkdir(parents=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_execute",
        "--stage",
        stage.id,
        "--output-dir",
        str(output),
    ]
    if source_manifest:
        command.extend(["--source-manifest", str(source_manifest.resolve())])
    if evidence:
        command.extend(["--evidence-dir", str(evidence.resolve())])
    phase = CoveragePhase(
        name=stage.id,
        selection=stage.lane or stage.id,
        coverage_mode=stage.coverage or "none",
        timeout_seconds=stage.timeout_seconds,
        command=tuple(command),
        log_path=output / "stage.log",
    )
    process = run_coverage_phase(ROOT, phase)
    outcome = process["outcome"]
    errors = []
    try:
        after = source_identity(ROOT, source_manifest)
    except ValueError as error:
        after = None
        errors.append(str(error))
    if after != before:
        errors.append("source changed during the stage")
    try:
        artifacts = export_artifacts(output)
        if process["outcome"] == "passed" and stage.lane:
            timing = json.loads(_uncompressed(output / "timing.json.gz"))
            if (
                timing.get("integrity", {}).get("valid") is not True
                or not (output / "coverage.data.gz").is_file()
            ):
                errors.append("stage lacks successful taxonomy and coverage evidence")
    except (OSError, ValueError) as error:
        artifacts = {}
        errors.append(str(error))
    if errors:
        outcome = "invalid-evidence"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage_record(stage),
        "source": before,
        "source_after": after,
        "source_root": str(ROOT),
        "environment": runtime_identity(),
        "command": command,
        "assertion_commands": commands(stage),
        "process": process,
        "outcome": outcome,
        "errors": errors,
        "artifacts": artifacts,
    }
    _write(output / "receipt.json", receipt)
    if (output / "receipt.json").stat().st_size > MAX_ARTIFACT_BYTES:
        raise InventoryError("receipt exceeds its artifact byte limit")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if outcome == "passed" else 1


def runtime_identity() -> dict[str, Any]:
    """Prevent mixing interpreter or dependency variants in one coverage proof."""
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "platform_description": platform.platform(),
        "packages": {package: version(package) for package in inventory.ENVIRONMENT_PACKAGES},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    catalogue_parser = subparsers.add_parser("catalogue")
    catalogue_parser.add_argument("--source-manifest", type=Path)
    for name in ("run", "aggregate", "_execute"):
        sub = subparsers.add_parser(name)
        if name != "aggregate":
            sub.add_argument("--stage", required=True)
        sub.add_argument("--output-dir", type=Path, required=True)
        sub.add_argument("--source-manifest", type=Path)
        sub.add_argument("--evidence-dir", type=Path, required=name == "aggregate")
    args = parser.parse_args(argv)
    try:
        if args.command == "catalogue":
            result = catalogue(load_manifest(TAXONOMY))
            result["source"] = source_identity(ROOT, args.source_manifest)
            print(json.dumps(result, indent=2))
            return 0
        stage = stage_by_id("aggregate" if args.command == "aggregate" else args.stage)
        action = execute if args.command == "_execute" else run_stage
        return action(stage, args.output_dir, args.source_manifest, args.evidence_dir)
    except (InventoryError, OSError, subprocess.CalledProcessError) as error:
        print(f"Linux test stage failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
