"""Tests for paired canonical pytest-xdist benchmark evidence."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from scripts import ray_residue
from scripts import test_suite_benchmark as benchmark

ROOT = Path(__file__).parents[2]


def _execution(mode: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        copy.deepcopy(benchmark.XDIST_EXECUTION if mode == "xdist" else benchmark.SERIAL_EXECUTION),
    )


def _timing(phase: str, execution: str, wall: float) -> dict[str, Any]:
    mode = "xdist" if phase == "hermetic" and execution == "xdist" else "serial"
    outcomes = {
        "hermetic": [
            {"nodeid": "tests/unit/test_sample.py::test_a", "outcome": "passed"},
            {"nodeid": "tests/unit/test_sample.py::test_b", "outcome": "passed"},
        ],
        "sqlite-django": [{"nodeid": "tests/unit/test_database.py::test_db", "outcome": "passed"}],
        "local-ray": [{"nodeid": "tests/unit/test_ray.py::test_ray", "outcome": "passed"}],
        "default-serial-remainder": [
            {"nodeid": "tests/integration/test_postgresql.py::test_backend", "outcome": "skipped"},
            {"nodeid": "tests/unit/test_compiled.py::test_opt_in", "outcome": "skipped"},
        ],
    }[phase]
    selected_count = len(outcomes)
    collection = {
        "mode": mode,
        "execution": _execution(mode),
        "selected_count": selected_count,
        "deselected_count": 6 - selected_count,
        "nodeid_digest": f"{phase}-selected",
        "contract_digest": f"{phase}-contract",
        "collected_count": 6,
        "collected_nodeid_digest": "full-nodeids",
        "collected_contract_digest": "full-contract",
        "worker_collections": [],
        "valid": True,
        "errors": [],
    }
    if mode == "xdist":
        collection["worker_collections"] = [
            {"worker": "gw0"},
            {"worker": "gw1"},
        ]
    return {
        "schema_version": 3,
        "sample_id": f"{execution}-{phase}",
        "lane": phase,
        "source": {"digest": "a" * 64},
        "source_after_digest": "a" * 64,
        "execution": _execution(mode),
        "collection": collection,
        "integrity": {"valid": True, "errors": []},
        "test_outcomes": outcomes,
        "external": {
            "runner_queue_seconds": None,
            "environment_setup_seconds": None,
            "note": "External intervals are excluded.",
        },
        "environment": {
            "django_settings_module": "unset",
            "python": "3.12.12",
            "platform": "Linux-fixture-x86_64",
            "packages": {
                "coverage": "7.1.0",
                "django": "6.0.7",
                "pytest": "9.1.1",
                "pytest-cov": "7.1.0",
                "pytest-django": "4.12.0",
                "pytest-xdist": "3.8.0",
                "ray": "2.56.1",
            },
            "processor_count": 4,
        },
        "pytest": {
            "completed_count": selected_count,
            "execution_wall_seconds": wall,
            "setup_phase_seconds": wall * 0.2,
            "call_phase_seconds": wall * 0.6,
            "teardown_phase_seconds": wall * 0.1,
        },
    }


def _coverage(extra_core_line: bool) -> dict[str, object]:
    core_executed = list(range(1, 81 if extra_core_line else 80))
    core_missing = [] if extra_core_line else [80]
    return {
        "meta": {"version": "fixture"},
        "files": {
            "src/django_ray/management/commands/django_ray_worker.py": {
                "executed_lines": list(range(1, 10)),
                "missing_lines": [10],
                "excluded_lines": [],
            },
            "src/django_ray/runner/ray_job.py": {
                "executed_lines": list(range(1, 10)),
                "missing_lines": [10],
                "excluded_lines": [],
            },
            "src/django_ray/core.py": {
                "executed_lines": core_executed,
                "missing_lines": core_missing,
                "excluded_lines": [],
            },
        },
    }


def _evidence_directory(
    root: Path,
    execution: str,
    *,
    hermetic_wall: float,
    improved_coverage: bool,
    canonical_wall: float,
    run_id: str = "1001",
) -> Path:
    root.mkdir(parents=True)
    timings = [
        _timing("hermetic", execution, hermetic_wall),
        _timing("sqlite-django", execution, 2.0),
        _timing("local-ray", execution, 3.0),
        _timing("default-serial-remainder", execution, 1.0),
    ]
    for phase, timing in zip(benchmark.PHASES, timings, strict=True):
        (root / f"{phase}.json").write_text(json.dumps(timing), encoding="utf-8")
    (root / "inventory.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "source": {"digest": "a" * 64},
                "timings": timings,
                "groups": [
                    {
                        "id": "supported-python",
                        "selected_count": sum(len(timing["test_outcomes"]) for timing in timings),
                        "nodeid_digest": benchmark._nodeid_digest(
                            {
                                outcome["nodeid"]
                                for timing in timings
                                for outcome in timing["test_outcomes"]
                            }
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "inventory.md").write_text("# Validated inventory\n", encoding="utf-8")
    (root / "coverage.json").write_text(json.dumps(_coverage(improved_coverage)), encoding="utf-8")
    (root / "coverage.xml").write_text("<coverage />\n", encoding="utf-8")
    started_ns = 1_000_000_000
    run = benchmark.record_run(
        execution,
        started_ns=started_ns,
        finished_ns=started_ns + int(canonical_wall * 1_000_000_000),
        repository="dariuszpanas/django-ray",
        sha="b" * 40,
        tree_sha="c" * 40,
        run_id=run_id,
        run_attempt=1,
        job="pytest-xdist-benchmark-pair",
        runner_os="Linux",
        runner_image_os="ubuntu24",
        runner_image_version="20260720.1",
    )
    (root / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (root / "ray-residue.json").write_text(
        json.dumps(
            {
                "schema_version": benchmark.RAY_RESIDUE_SCHEMA_VERSION,
                "valid": True,
                "errors": [],
                "additions": {
                    "processes": [],
                    "listeners": [],
                    "shared_memory": [],
                    "global_temp": [],
                },
                "owned_temp": {
                    "entries_observed": 12,
                    "scan_limit": benchmark.OWNED_TEMP_SCAN_LIMIT,
                    "scan_truncated": False,
                    "scan_error": None,
                    "removed": True,
                    "exists_after": False,
                },
                "guard": {
                    "body_returncode": 0,
                    "cleanup_returncode": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_compare_pair_requires_exact_canonical_parity_and_coverage_non_regression(
    tmp_path: Path,
) -> None:
    serial = _evidence_directory(
        tmp_path / "serial",
        "serial",
        hermetic_wall=10.0,
        improved_coverage=False,
        canonical_wall=20.0,
    )
    xdist = _evidence_directory(
        tmp_path / "xdist",
        "xdist",
        hermetic_wall=7.0,
        improved_coverage=True,
        canonical_wall=18.0,
    )

    report = benchmark.compare_pair(
        serial,
        xdist,
        sample="sample-1",
        order="serial-xdist",
    )

    assert report["integrity"] == {"valid": True, "errors": []}
    assert report["performance"]["improvement_percent"] == 30.0
    assert report["coverage"]["non_regression"] is True
    assert set(report["canonical_parity"]) == set(benchmark.PHASES)
    assert benchmark.OWNED_TEMP_SCAN_LIMIT == ray_residue.OWNED_TEMP_SCAN_LIMIT

    residue_path = xdist / "ray-residue.json"
    residue = json.loads(residue_path.read_text(encoding="utf-8"))
    residue["owned_temp"]["entries_observed"] = benchmark.OWNED_TEMP_SCAN_LIMIT + 1
    residue["owned_temp"]["scan_truncated"] = True
    residue_path.write_text(json.dumps(residue), encoding="utf-8")
    truncated_report = benchmark.compare_pair(
        serial,
        xdist,
        sample="truncated-owned-diagnostic",
        order="serial-xdist",
    )
    assert truncated_report["integrity"] == {"valid": True, "errors": []}

    residue["owned_temp"]["entries_observed"] = 12
    residue_path.write_text(json.dumps(residue), encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkError, match="Ray temporary"):
        benchmark.compare_pair(serial, xdist, sample="inconsistent-scan", order="serial-xdist")

    residue["owned_temp"].update(
        {
            "entries_observed": None,
            "scan_truncated": None,
            "scan_error": "scanner unavailable",
        }
    )
    residue_path.write_text(json.dumps(residue), encoding="utf-8")
    scan_error_report = benchmark.compare_pair(
        serial,
        xdist,
        sample="scan-error-diagnostic",
        order="serial-xdist",
    )
    assert scan_error_report["integrity"] == {"valid": True, "errors": []}

    for invalid_status in (7, False):
        residue["guard"]["body_returncode"] = invalid_status
        residue_path.write_text(json.dumps(residue), encoding="utf-8")
        with pytest.raises(benchmark.BenchmarkError, match="canonical body"):
            benchmark.compare_pair(
                serial,
                xdist,
                sample="invalid-guard",
                order="serial-xdist",
            )


@pytest.mark.parametrize(
    "mutation",
    ("extra", "missing-owned", "coerced-limit"),
)
def test_compare_pair_rejects_malformed_residue_schema(
    tmp_path: Path,
    mutation: str,
) -> None:
    serial = _evidence_directory(
        tmp_path / "serial",
        "serial",
        hermetic_wall=10.0,
        improved_coverage=True,
        canonical_wall=20.0,
    )
    xdist = _evidence_directory(
        tmp_path / "xdist",
        "xdist",
        hermetic_wall=7.0,
        improved_coverage=True,
        canonical_wall=18.0,
    )
    residue_path = xdist / "ray-residue.json"
    residue = json.loads(residue_path.read_text(encoding="utf-8"))
    if mutation == "extra":
        residue["unexpected"] = True
    elif mutation == "missing-owned":
        residue["owned_temp"].pop("removed")
    else:
        residue["owned_temp"]["scan_limit"] = float(benchmark.OWNED_TEMP_SCAN_LIMIT)
    residue_path.write_text(json.dumps(residue), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="Ray temporary|Ray residue"):
        benchmark.compare_pair(serial, xdist, sample="bad-residue", order="serial-xdist")


@pytest.mark.parametrize(
    ("filename", "coerced_schema"),
    (
        ("hermetic.json", 3.0),
        ("inventory.json", 3.0),
        ("run.json", True),
        ("ray-residue.json", 1),
    ),
)
def test_compare_pair_rejects_coerced_schema_versions(
    tmp_path: Path,
    filename: str,
    coerced_schema: object,
) -> None:
    serial = _evidence_directory(
        tmp_path / "serial",
        "serial",
        hermetic_wall=10.0,
        improved_coverage=True,
        canonical_wall=20.0,
    )
    xdist = _evidence_directory(
        tmp_path / "xdist",
        "xdist",
        hermetic_wall=7.0,
        improved_coverage=True,
        canonical_wall=18.0,
    )
    evidence_path = xdist / filename
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["schema_version"] = coerced_schema
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="schema version"):
        benchmark.compare_pair(serial, xdist, sample="coerced", order="serial-xdist")


def test_compare_pair_rejects_exact_outcome_or_covered_line_regression(tmp_path: Path) -> None:
    serial = _evidence_directory(
        tmp_path / "serial",
        "serial",
        hermetic_wall=10.0,
        improved_coverage=True,
        canonical_wall=20.0,
    )
    xdist = _evidence_directory(
        tmp_path / "xdist",
        "xdist",
        hermetic_wall=7.0,
        improved_coverage=True,
        canonical_wall=18.0,
    )
    xdist_timing_path = xdist / "hermetic.json"
    xdist_timing = json.loads(xdist_timing_path.read_text(encoding="utf-8"))
    xdist_timing["test_outcomes"][0]["outcome"] = "skipped"
    xdist_timing_path.write_text(json.dumps(xdist_timing), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="merged inventory"):
        benchmark.compare_pair(serial, xdist, sample="outcome-drift", order="serial-xdist")

    inventory_path = xdist / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory_timing = next(
        timing for timing in inventory["timings"] if timing["sample_id"] == "xdist-hermetic"
    )
    inventory_timing["test_outcomes"][0]["outcome"] = "skipped"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkError, match="exact outcomes differ"):
        benchmark.compare_pair(serial, xdist, sample="outcome-drift", order="serial-xdist")

    xdist_timing["test_outcomes"][0]["outcome"] = "passed"
    xdist_timing_path.write_text(json.dumps(xdist_timing), encoding="utf-8")
    inventory_timing["test_outcomes"][0]["outcome"] = "passed"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    coverage_path = xdist / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["files"]["src/django_ray/core.py"]["executed_lines"].remove(80)
    coverage["files"]["src/django_ray/core.py"]["missing_lines"] = [80]
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="regresses covered lines"):
        benchmark.compare_pair(serial, xdist, sample="coverage-drift", order="serial-xdist")


def test_compare_pair_rejects_zero_xdist_hermetic_wall(tmp_path: Path) -> None:
    serial = _evidence_directory(
        tmp_path / "serial",
        "serial",
        hermetic_wall=10.0,
        improved_coverage=True,
        canonical_wall=20.0,
    )
    xdist = _evidence_directory(
        tmp_path / "xdist",
        "xdist",
        hermetic_wall=0.0,
        improved_coverage=True,
        canonical_wall=18.0,
    )

    with pytest.raises(benchmark.BenchmarkError, match="wall times must be positive"):
        benchmark.compare_pair(serial, xdist, sample="zero-wall", order="serial-xdist")


def test_compare_pair_rejects_malformed_merged_timing_identity(tmp_path: Path) -> None:
    serial = _evidence_directory(
        tmp_path / "serial",
        "serial",
        hermetic_wall=20.0,
        improved_coverage=True,
        canonical_wall=40.0,
        run_id="101",
    )
    xdist = _evidence_directory(
        tmp_path / "xdist",
        "xdist",
        hermetic_wall=12.0,
        improved_coverage=True,
        canonical_wall=38.0,
        run_id="101",
    )
    inventory_path = xdist / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["timings"][0]["sample_id"] = ["not", "hashable"]
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="timing identities"):
        benchmark.compare_pair(serial, xdist, sample="malformed-id", order="serial-xdist")


def test_aggregate_uses_three_fresh_pairs_and_median_retention_threshold(
    tmp_path: Path,
) -> None:
    serial = _evidence_directory(
        tmp_path / "serial",
        "serial",
        hermetic_wall=10.0,
        improved_coverage=False,
        canonical_wall=20.0,
    )
    xdist = _evidence_directory(
        tmp_path / "xdist",
        "xdist",
        hermetic_wall=7.0,
        improved_coverage=True,
        canonical_wall=18.0,
    )
    base = benchmark.compare_pair(serial, xdist, sample="1", order="serial-xdist")
    pairs = []
    for sample, order, serial_seconds, xdist_seconds in (
        ("1", "serial-xdist", 10.0, 7.0),
        ("2", "xdist-serial", 11.0, 8.0),
        ("3", "serial-xdist", 12.0, 8.0),
    ):
        pair = copy.deepcopy(base)
        pair["sample"] = sample
        pair["order"] = order
        pair["github"]["run_id"] = str(1000 + int(sample))
        pair["performance"]["serial_seconds"] = serial_seconds
        pair["performance"]["xdist_seconds"] = xdist_seconds
        if sample == "2":
            pair["github"]["runner_image_version"] = "20260721.1"
        pairs.append(pair)

    report = benchmark.aggregate_pairs(
        pairs,
        repository="dariuszpanas/django-ray",
        sha="b" * 40,
        tree_sha="c" * 40,
    )

    assert report["sample_count"] == 3
    assert report["retention"]["eligible"] is True
    assert report["retention"]["serial_median_seconds"] == 11.0
    assert report["retention"]["xdist_median_seconds"] == 8.0
    assert report["retention"]["median_improvement_percent"] > 25.0
    assert report["retention"]["canonical_plan_median_improvement_percent"] > 0
    assert report["github"] == {
        "repository": "dariuszpanas/django-ray",
        "sha": "b" * 40,
        "tree_sha": "c" * 40,
        "pair_job": "pytest-xdist-benchmark-pair",
        "runner_os": "Linux",
    }
    assert report["schema_version"] == 2
    assert report["runner_image"] == {
        "os": "ubuntu24",
        "versions": ["20260720.1", "20260721.1"],
    }
    assert report["environment"]["packages"]["pytest-xdist"] == "3.8.0"
    assert {sample["github_run_attempt"] for sample in report["samples"]} == {1}
    aggregate_markdown = benchmark.render_aggregate_markdown(report)
    assert "b" * 40 in aggregate_markdown
    assert "c" * 40 in aggregate_markdown
    assert "a" * 64 in aggregate_markdown
    assert "`ubuntu24` (`20260720.1`, `20260721.1`)" in aggregate_markdown

    foreign = copy.deepcopy(pairs)
    for pair in foreign:
        pair["github"]["repository"] = "someone-else/django-ray"
    with pytest.raises(benchmark.BenchmarkError, match="aggregate checkout identity"):
        benchmark.aggregate_pairs(
            foreign,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    wrong_tree = copy.deepcopy(pairs)
    wrong_tree[2]["github"]["tree_sha"] = "d" * 40
    with pytest.raises(benchmark.BenchmarkError, match="same repository commit and tree"):
        benchmark.aggregate_pairs(
            wrong_tree,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    coerced_pair_schema = copy.deepcopy(pairs)
    coerced_pair_schema[0]["schema_version"] = True
    with pytest.raises(benchmark.BenchmarkError, match="schema version"):
        benchmark.aggregate_pairs(
            coerced_pair_schema,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    nonalternating = copy.deepcopy(pairs)
    nonalternating[1]["order"] = "serial-xdist"
    nonalternating[2]["order"] = "xdist-serial"
    with pytest.raises(benchmark.BenchmarkError, match="must alternate"):
        benchmark.aggregate_pairs(
            nonalternating,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    zero_xdist = copy.deepcopy(pairs)
    zero_xdist[0]["performance"]["xdist_seconds"] = 0.0
    with pytest.raises(benchmark.BenchmarkError, match="pair times must be positive"):
        benchmark.aggregate_pairs(
            zero_xdist,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    flaky = copy.deepcopy(pairs)
    flaky[1]["canonical_parity"]["hermetic"]["outcomes"]["tests/unit/test_sample.py::test_a"] = (
        "skipped"
    )
    with pytest.raises(benchmark.BenchmarkError, match="outcomes differ across"):
        benchmark.aggregate_pairs(
            flaky,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    unstable_coverage = copy.deepcopy(pairs)
    unstable_coverage[2]["coverage"]["xdist"]["covered_line_digest"] = "c" * 64
    with pytest.raises(benchmark.BenchmarkError, match="coverage line sets differ"):
        benchmark.aggregate_pairs(
            unstable_coverage,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    foreign_runner_os = copy.deepcopy(pairs)
    foreign_runner_os[1]["github"]["runner_image_os"] = "ubuntu22"
    with pytest.raises(benchmark.BenchmarkError, match="runner image operating systems"):
        benchmark.aggregate_pairs(
            foreign_runner_os,
            repository="dariuszpanas/django-ray",
            sha="b" * 40,
            tree_sha="c" * 40,
        )

    regressed = copy.deepcopy(pairs)
    for pair in regressed:
        pair["performance"]["xdist_canonical_plan_seconds"] = 21.0
    rejected = benchmark.aggregate_pairs(
        regressed,
        repository="dariuszpanas/django-ray",
        sha="b" * 40,
        tree_sha="c" * 40,
    )
    assert rejected["retention"]["eligible"] is False
    assert "full canonical plan wall" in " ".join(rejected["retention"]["reasons"])


def test_phased_make_target_and_workflow_stay_opt_in() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    public = makefile.split("test-cov-phased:", maxsplit=1)[1].split("# Internal body", maxsplit=1)[
        0
    ]
    body = makefile.split("_test-cov-phased-body:", maxsplit=1)[1].split(
        "# Collect exact execution-contract", maxsplit=1
    )[0]
    phased = public + body
    ci_target = makefile.split("\nci:\n", maxsplit=1)[1].split("\n# Build the package", maxsplit=1)[
        0
    ]

    assert phased.count("coverage erase") == 1
    assert phased.count("--cov-append") == 4
    assert phased.count("--cov-fail-under=0") == 4
    assert phased.count("--coverage-file") == 4
    assert phased.count("--ray-tmp-dir") == 4
    assert phased.count("--data-file=") == 6
    assert "COVERAGE_FILE=" not in phased
    assert "RAY_TMPDIR=" not in phased
    assert "--lane hermetic" in phased
    assert "--lane sqlite-django" in phased
    assert "--lane local-ray" in phased
    assert "--lane default-serial-remainder" in phased
    assert "scripts/ray_residue.py guard" in public
    assert "$(MAKE) --no-print-directory _test-cov-phased-body" in public
    assert "coverage erase" not in public
    assert "scripts/ray_residue.py assert-clean" not in phased
    assert body.lstrip().startswith("@python scripts/ray_residue.py verify-guard --output-dir ")
    assert body.index("verify-guard") < body.index("coverage erase")
    assert (
        "TEST_SUITE_RAY_TMP_DIR ?= $(abspath $(TEST_SUITE_PHASED_OUTPUT_DIR)/ray-tmp)" in makefile
    )
    assert phased.count('"$(TEST_SUITE_RAY_TMP_DIR)"') == 4
    assert '--owned-temp-dir "$(TEST_SUITE_PHASED_OUTPUT_DIR)/ray-tmp"' in public
    assert phased.index("--lane local-ray") < phased.index("coverage report --data-file=")
    assert "uv run" not in phased
    assert "test-cov-phased" not in ci_target
    assert '-m "not live_cluster"' in ci_target
    assert "artifacts/test-suite-phased-coverage/" in gitignore
    assert "artifacts/pytest-xdist-*/" in gitignore

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert "workflow_dispatch" in triggers
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["xdist_benchmark_mode"]["options"] == ["off", "pair", "aggregate"]
    pair_step = next(
        step
        for step in workflow["jobs"]["pytest-xdist-benchmark-pair"]["steps"]
        if step.get("name") == "Run same-SHA canonical pair"
    )
    assert "make test-cov-phased" in pair_step["run"]
    assert "record-run" in pair_step["run"]
    assert "--tree-sha \"$(git rev-parse 'HEAD^{tree}')\"" in pair_step["run"]
    assert 'for execution in "${executions[@]}"' in pair_step["run"]
    assert "trap cleanup_ray_aliases EXIT" in pair_step["run"]
    assert 'for ray_alias in "${ray_aliases[@]}"' in pair_step["run"]
    assert 'if [[ -e "$ray_alias" || -L "$ray_alias" ]]' in pair_step["run"]
    assert 'elif [[ -e "$ray_alias" ]]' in pair_step["run"]
    assert "refusing to remove non-symlink Ray alias" in pair_step["run"]
    assert 'serial) ray_alias="/tmp/drs"' in pair_step["run"]
    assert 'xdist) ray_alias="/tmp/drx"' in pair_step["run"]
    assert max(len(path) for path in ("/tmp/drs/ray-tmp", "/tmp/drx/ray-tmp")) <= 20
    assert 'ln -s "$PWD/$output" "$ray_alias"' in pair_step["run"]
    assert 'ray_aliases+=("$ray_alias")' in pair_step["run"]
    assert 'TEST_SUITE_RAY_TMP_DIR="${ray_alias}/ray-tmp"' in pair_step["run"]
    assert 'unlink -- "$ray_alias"' in pair_step["run"]
    assert pair_step["run"].index('mkdir -p "$output"') < pair_step["run"].index(
        'ln -s "$PWD/$output" "$ray_alias"'
    )
    assert pair_step["run"].index('ln -s "$PWD/$output" "$ray_alias"') < pair_step["run"].index(
        'ray_aliases+=("$ray_alias")'
    )
    assert pair_step["run"].index('ln -s "$PWD/$output" "$ray_alias"') < pair_step["run"].index(
        'started_ns="$(date +%s%N)"'
    )
    summary_step = next(
        step
        for step in workflow["jobs"]["pytest-xdist-benchmark-pair"]["steps"]
        if step.get("name") == "Add pair summary"
    )
    assert "Ray cleanup evidence" in summary_step["run"]
    assert 'cat "$residue"' in summary_step["run"]
    upload_step = next(
        step
        for step in workflow["jobs"]["pytest-xdist-benchmark-pair"]["steps"]
        if step.get("name") == "Upload paired benchmark evidence"
    )
    assert (
        "!artifacts/pytest-xdist-benchmark/${{ github.run_id }}/**/ray-tmp/**"
        in upload_step["with"]["path"]
    )
    aggregate_step = next(
        step
        for step in workflow["jobs"]["pytest-xdist-benchmark-aggregate"]["steps"]
        if step.get("name") == "Aggregate retention evidence"
    )
    assert "--require-retention" in aggregate_step["run"]
    assert '--repository "$GITHUB_REPOSITORY"' in aggregate_step["run"]
    assert '--sha "$GITHUB_SHA"' in aggregate_step["run"]
    assert "--tree-sha \"$(git rev-parse 'HEAD^{tree}')\"" in aggregate_step["run"]
    assert "pytest-xdist-benchmark-pair" not in workflow["jobs"]["ci-gate"]["needs"]


def test_prepare_rejects_stale_output_and_compiled_graph_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    output = root / "artifacts" / "benchmark"
    output.mkdir(parents=True)
    (output / "stale.json").write_text("{}", encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="new or empty"):
        benchmark.prepare_output(output, root)

    (output / "stale.json").unlink()
    monkeypatch.setenv("DJANGO_RAY_RUN_COMPILED_SESSION_TOPOLOGY_PROBE", "1")
    with pytest.raises(benchmark.BenchmarkError, match="requires.*unset"):
        benchmark.prepare_output(output, root)


def test_prepare_requires_a_fresh_git_ignored_output(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    (root / ".gitignore").write_text("artifacts/ignored/\n", encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="must be ignored"):
        benchmark.prepare_output(root / "artifacts" / "visible", root)

    ignored = root / "artifacts" / "ignored" / "sample"
    benchmark.prepare_output(ignored, root)

    assert ignored.is_dir()


def test_record_run_cli_binds_github_sha_to_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "_git_head", lambda _root=None: "d" * 40)
    monkeypatch.setattr(benchmark, "_git_tree", lambda _root=None: "e" * 40)
    output = tmp_path / "run.json"
    arguments = [
        "record-run",
        "--execution",
        "serial",
        "--started-ns",
        "1000000000",
        "--finished-ns",
        "2000000000",
        "--repository",
        "dariuszpanas/django-ray",
        "--sha",
        "c" * 40,
        "--tree-sha",
        "f" * 40,
        "--run-id",
        "123",
        "--run-attempt",
        "1",
        "--job",
        "pytest-xdist-benchmark-pair",
        "--runner-os",
        "Linux",
        "--runner-image-os",
        "ubuntu24",
        "--runner-image-version",
        "fixture",
        "--output",
        str(output),
    ]

    assert benchmark.main(arguments) == 2
    assert not output.exists()
    arguments[arguments.index("c" * 40)] = "d" * 40
    assert benchmark.main(arguments) == 2
    assert not output.exists()
    arguments[arguments.index("f" * 40)] = "e" * 40
    assert benchmark.main(arguments) == 0
    github = json.loads(output.read_text(encoding="utf-8"))["github"]
    assert github["sha"] == "d" * 40
    assert github["tree_sha"] == "e" * 40
