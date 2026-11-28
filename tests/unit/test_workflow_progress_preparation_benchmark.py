"""Subprocess and schema-v2 contract tests for topology preparation benchmarks."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts import benchmark_workflow_progress_preparation as benchmark


@pytest.mark.skipif(sys.platform != "linux", reason="benchmark execution requires Linux")
def test_production_topology_subprocess_benchmark_reports_resource_boundary(
    tmp_path: Path,
) -> None:
    output = tmp_path / "production-topology-report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(benchmark.__file__).resolve()),
            "--implementation",
            "production-topology",
            "--nodes",
            "32",
            "--profiles",
            "sparse",
            "high-edge",
            "--high-edge-factor",
            "2",
            "--spill-max-bytes",
            str(16 * 1024 * 1024),
            "--workspace-parent",
            str(tmp_path / "workspaces"),
            "--output",
            str(output),
        ],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        "production topology benchmark failed"
        f"\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["schema_version"] == 2
    assert report["implementation"] == "production-topology"
    assert report["required_scale"] is False
    assert "bounded pre-legacy checkpoint" in report["memory_evidence_contract"]
    assert report["source_snapshot_before"] == report["source_snapshot_after"]
    assert report["forced_termination"]["parent_watchdog"] == "removed"
    assert [case["profile"] for case in report["cases"]] == ["sparse", "high-edge"]
    for case in report["cases"]:
        assert case["implementation"] == "production-topology"
        assert case["observed_nodes"] == 32
        assert case["observed_detail"] is None
        assert case["retained_detail"] is None
        assert case["detail_encoded_bytes"] is None
        assert case["detail_decoded_bytes"] is None
        assert case["detail_truncation_reasons"] == []
        assert case["legacy_observed_node_ids"] == 32
        assert case["spill_items"] == case["observed_nodes"] + case["observed_edges"]
        assert 0 < case["spill_peak_bytes"] <= 16 * 1024 * 1024
        assert case["tracemalloc_peak_bytes"] > 0
        assert case["peak_rss_bytes"] is None or case["peak_rss_bytes"] > 0
        assert 0 < case["bounded_phase_tracemalloc_current_bytes"]
        assert (
            case["bounded_phase_tracemalloc_current_bytes"]
            <= case["bounded_phase_tracemalloc_peak_bytes"]
            <= case["tracemalloc_peak_bytes"]
        )
        bounded_rss = case["bounded_phase_rss_measurement"]
        assert bounded_rss["peak_bytes"] == case["bounded_phase_peak_rss_bytes"]
        assert "bounded topology preparation" in bounded_rss["scope"]
        if case["bounded_phase_peak_rss_bytes"] is not None:
            assert case["bounded_phase_peak_rss_bytes"] > 0
        if case["peak_rss_bytes"] is not None:
            assert (
                case["bounded_phase_peak_rss_bytes"] is None
                or case["peak_rss_bytes"] >= case["bounded_phase_peak_rss_bytes"]
            )
        assert case["end_to_end_tracemalloc_peak_bytes"] == case["tracemalloc_peak_bytes"]
        assert case["end_to_end_peak_rss_bytes"] == case["peak_rss_bytes"]
        assert "phase-separated" in case["resident_contract"]
        assert "legacy O(observed)" in case["resident_contract"]
        assert case["cleanup"] == {
            "worker_context": "removed",
            "workspace_exists_after_context": False,
            "parent_watchdog": "removed",
            "scenario_root_exists_after_parent": False,
        }
        assert all("TEMP B-TREE" not in plan.upper() for plan in case["query_plans"])
    assert not list((tmp_path / "workspaces").iterdir())


def _benchmark_filesystem() -> dict[str, Any]:
    return {
        "identity_sha256": "a" * 64,
        "identity_method": "sha256(platform, st_dev)",
        "filesystem_type": "testfs",
        "allocation_block_bytes": 4096,
    }


def _benchmark_rss_measurement(*, peak: int, scope: str) -> dict[str, Any]:
    baseline = peak - 100
    return {
        "peak_bytes": peak,
        "method": "psutil.Process.memory_info().rss sampling",
        "scope": scope,
        "baseline_bytes": baseline,
        "baseline_current_bytes": baseline,
        "baseline_high_water_bytes": None,
        "sample_interval_seconds": 0.01,
        "sampled_peak_bytes": peak,
        "process_high_water_bytes": None,
    }


def _benchmark_budgets(*, implementation: str) -> dict[str, Any]:
    budgets = {
        "page_bytes": 4096,
        "cache_bytes": 8 * 1024 * 1024,
        "mmap_bytes": 0,
        "max_spill_bytes": 16 * 1024 * 1024,
        "control_reserve_bytes": 4 * 1024 * 1024,
        "max_node_items": 1_000_000,
        "max_edge_items": 4_000_000,
        "batch_max_items": 256,
        "batch_max_decoded_bytes": 4 * 1024 * 1024,
    }
    if implementation == "prototype-composite":
        budgets["max_detail_items"] = 1_000_000
    return budgets


def _benchmark_memory_case(
    *,
    implementation: str = "production-topology",
    observed_nodes: int = 25_000,
    profile: str = "sparse",
) -> dict[str, Any]:
    observed_edges = observed_nodes - 1 if profile == "sparse" else observed_nodes * 8
    production = implementation == "production-topology"
    budgets = _benchmark_budgets(implementation=implementation)
    limits = benchmark._v1_output_limits()
    retained_nodes = min(observed_nodes, limits["topology_node_max_items"])
    if profile == "sparse":
        eligible_edges = retained_nodes - 1
    elif retained_nodes == observed_nodes:
        eligible_edges = observed_edges
    else:
        factor = observed_edges // observed_nodes
        retained_offsets = min(factor, retained_nodes - 1)
        eligible_edges = (
            retained_offsets * retained_nodes - retained_offsets * (retained_offsets + 1) // 2
        )
    retained_edges = min(eligible_edges, limits["topology_edge_max_items"])
    topology_reasons = sorted(
        reason
        for reason, truncated in (
            ("node_count_limit", observed_nodes > limits["topology_node_max_items"]),
            ("edge_count_limit", eligible_edges > limits["topology_edge_max_items"]),
        )
        if truncated
    )
    detail_reasons = [] if production else topology_reasons
    topology_pages = (retained_nodes + limits["topology_page_max_items"] - 1) // limits[
        "topology_page_max_items"
    ]
    if retained_edges:
        topology_pages += (retained_edges + limits["topology_page_max_items"] - 1) // limits[
            "topology_page_max_items"
        ]
    case: dict[str, Any] = {
        "implementation": implementation,
        "observed_nodes": observed_nodes,
        "observed_edges": observed_edges,
        "observed_detail": None if production else observed_nodes,
        "retained_nodes": retained_nodes,
        "retained_edges": retained_edges,
        "retained_detail": None if production else min(observed_nodes, 25_000),
        "topology_pages": topology_pages,
        "topology_encoded_bytes": 1_000,
        "topology_decoded_bytes": 1_000,
        "detail_encoded_bytes": None if production else 500,
        "detail_decoded_bytes": None if production else 500,
        "legacy_observed_node_ids": observed_nodes if production else None,
        "manifest_digest": "b" * 64,
        "truncation_reasons": topology_reasons,
        "topology_truncation_reasons": topology_reasons,
        "detail_truncation_reasons": detail_reasons,
        "wall_seconds": 1.0,
        "cpu_seconds": 0.5,
        "profile": profile,
        "tracemalloc_peak_bytes": 200,
        "peak_rss_bytes": 400,
        "end_to_end_tracemalloc_peak_bytes": 200,
        "end_to_end_peak_rss_bytes": 400,
        "rss_measurement": _benchmark_rss_measurement(
            peak=400,
            scope="preparation measurement window",
        ),
        "bounded_phase_tracemalloc_current_bytes": None,
        "bounded_phase_tracemalloc_peak_bytes": None,
        "bounded_phase_peak_rss_bytes": None,
        "bounded_phase_rss_measurement": None,
        "spill_peak_bytes": 8 * 1024,
        "spill_items": observed_nodes + observed_edges + (0 if production else observed_nodes),
        "cleanup": {
            "worker_context": "removed",
            "workspace_exists_after_context": False,
            "parent_watchdog": "removed",
            "scenario_root_exists_after_parent": False,
        },
        "budgets": budgets,
        "v1_output_limits": limits,
        "sqlite_pragmas": {
            "page_size": 4096,
            "cache_size": -8192,
            "mmap_size": 0,
            "temp_store": 2,
            "journal_mode": "off",
            "synchronous": 0,
            "locking_mode": "exclusive",
            "foreign_keys": 1,
            "trusted_schema": 0,
            "max_page_count": 3072,
        },
        "query_plans": [
            "SCAN nodes",
            "SCAN e",
            "SEARCH source_node USING PRIMARY KEY (node_id=?)",
            "SEARCH target_node USING PRIMARY KEY (node_id=?)",
            "SCAN nodes" if production else "SCAN detail",
        ],
        "resident_contract": (
            benchmark._PRODUCTION_RESIDENT_CONTRACT
            if production
            else benchmark._PROTOTYPE_RESIDENT_CONTRACT
        ),
        "environment": {
            "platform": "test-platform",
            "python": "3.12.0",
            "python_implementation": "CPython",
            "sqlite": "3.45.0",
            "django": "6.0.0",
            "django_ray": "0.3.1",
            "pid": 141,
            "filesystem": _benchmark_filesystem(),
        },
    }
    if production:
        case.update(
            {
                "bounded_phase_tracemalloc_current_bytes": 50,
                "bounded_phase_tracemalloc_peak_bytes": 100,
                "bounded_phase_peak_rss_bytes": 300,
                "bounded_phase_rss_measurement": _benchmark_rss_measurement(
                    peak=300,
                    scope="measurement window through bounded topology preparation",
                ),
            }
        )
    return case


def _benchmark_v2_report(
    *,
    implementation: str = "production-topology",
    required_scale: bool = False,
) -> dict[str, Any]:
    cases = [_benchmark_memory_case(implementation=implementation)]
    if required_scale:
        cases = [
            _benchmark_memory_case(
                implementation=implementation,
                observed_nodes=nodes,
                profile=profile,
            )
            for nodes in benchmark.REQUIRED_SCALE_NODES
            for profile in benchmark.DEFAULT_PROFILES
        ]
    return {
        "schema_version": 2,
        "implementation": implementation,
        "required_scale": required_scale,
        "created_at": "2026-07-21T00:00:00Z",
        "source_revision": "1" * 40,
        "source_dirty": False,
        "implementation_digest": "2" * 64,
        "source_snapshot_before": {
            "revision": "1" * 40,
            "dirty": False,
            "implementation_digest": "2" * 64,
        },
        "source_snapshot_after": {
            "revision": "1" * 40,
            "dirty": False,
            "implementation_digest": "2" * 64,
        },
        "command": "benchmark --test",
        "cases": cases,
        "forced_termination": {
            "outcome": "forcibly-terminated",
            "readiness_observed": True,
            "workspace_open_before_kill": True,
            "worker_returncode": -9,
            "durable_candidate_exists_before_cleanup": False,
            "durable_candidate_exists_after_cleanup": False,
            "parent_watchdog": "removed",
            "scenario_root_exists_after_parent": False,
            "filesystem": _benchmark_filesystem(),
        },
        "memory_evidence_contract": (
            benchmark._PRODUCTION_MEMORY_EVIDENCE_CONTRACT
            if implementation == "production-topology"
            else benchmark._PROTOTYPE_MEMORY_EVIDENCE_CONTRACT
        ),
        "cleanup_contract": benchmark._CLEANUP_CONTRACT,
    }


def test_benchmark_v2_is_additive_and_retains_legacy_memory_fields() -> None:
    report = _benchmark_v2_report()
    report["future_top_level_field"] = {"ignored": True}
    report["source_snapshot_before"]["future_source_field"] = "before"
    report["source_snapshot_after"]["future_source_field"] = "after"
    report["cases"][0]["future_case_field"] = "ignored"
    report["cases"][0]["cleanup"]["future_cleanup_field"] = "ignored"
    report["cases"][0]["budgets"]["future_budget_field"] = 1
    report["cases"][0]["v1_output_limits"]["future_limit_field"] = 2
    report["cases"][0]["rss_measurement"]["future_rss_field"] = 3
    report["cases"][0]["environment"]["future_environment_field"] = "ignored"
    report["cases"][0]["environment"]["filesystem"]["future_filesystem_field"] = 4
    report["forced_termination"]["future_forced_field"] = "ignored"

    benchmark._validate_report(report)

    case = report["cases"][0]
    assert case["tracemalloc_peak_bytes"] == case["end_to_end_tracemalloc_peak_bytes"]
    assert case["peak_rss_bytes"] == case["end_to_end_peak_rss_bytes"]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("schema_version", "v2 report is incomplete"),
        ("memory_evidence_contract", "v2 report is incomplete"),
    ],
)
def test_benchmark_v2_rejects_missing_required_report_fields(
    field: str,
    message: str,
) -> None:
    report = _benchmark_v2_report()
    del report[field]

    with pytest.raises(RuntimeError, match=message):
        benchmark._validate_report(report)


def test_benchmark_v2_rejects_unsupported_schema_and_missing_case_fields() -> None:
    report = _benchmark_v2_report()
    report["schema_version"] = 1
    with pytest.raises(RuntimeError, match="report schema is unsupported"):
        benchmark._validate_report(report)

    report = _benchmark_v2_report()
    del report["cases"][0]["bounded_phase_rss_measurement"]
    with pytest.raises(RuntimeError, match="case evidence is incomplete"):
        benchmark._validate_report(report)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            {"end_to_end_tracemalloc_peak_bytes": 201},
            "end-to-end memory evidence is inconsistent",
        ),
        (
            {"bounded_phase_tracemalloc_current_bytes": 101},
            "bounded tracemalloc evidence is invalid",
        ),
        (
            {"bounded_phase_peak_rss_bytes": 301},
            "bounded RSS evidence is invalid",
        ),
        (
            {
                "bounded_phase_peak_rss_bytes": 500,
                "bounded_phase_rss_measurement": _benchmark_rss_measurement(
                    peak=500,
                    scope="measurement window through bounded topology preparation",
                ),
            },
            "RSS peaks are not monotonic",
        ),
    ],
)
def test_benchmark_v2_rejects_inconsistent_production_memory_evidence(
    mutation: dict[str, Any],
    message: str,
) -> None:
    report = _benchmark_v2_report()
    report["cases"][0].update(deepcopy(mutation))

    with pytest.raises(RuntimeError, match=message):
        benchmark._validate_report(report)


def test_benchmark_required_scale_requires_exact_complete_matrix() -> None:
    report = _benchmark_v2_report(required_scale=True)
    benchmark._validate_report(report)

    missing = deepcopy(report)
    missing["cases"].pop()
    with pytest.raises(RuntimeError, match="required-scale matrix is incomplete"):
        benchmark._validate_report(missing)

    duplicate = deepcopy(report)
    duplicate["cases"][-1] = deepcopy(duplicate["cases"][0])
    with pytest.raises(RuntimeError, match="case matrix contains duplicates"):
        benchmark._validate_report(duplicate)

    incomplete_phase = deepcopy(report)
    del incomplete_phase["cases"][-1]["bounded_phase_rss_measurement"]
    with pytest.raises(RuntimeError, match="case evidence is incomplete"):
        benchmark._validate_report(incomplete_phase)


def test_benchmark_v2_requires_every_declared_evidence_field() -> None:
    report = _benchmark_v2_report()

    for field in benchmark._REPORT_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete[field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)

    for field in benchmark._SOURCE_SNAPSHOT_REQUIRED_FIELDS:
        for snapshot_name in ("source_snapshot_before", "source_snapshot_after"):
            incomplete = deepcopy(report)
            del incomplete[snapshot_name][field]
            with pytest.raises(RuntimeError):
                benchmark._validate_report(incomplete)

    for field in benchmark._CASE_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete["cases"][0][field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)

    nested_fields = (
        ("cleanup", benchmark._CLEANUP_REQUIRED_FIELDS),
        ("budgets", benchmark._COMMON_BUDGET_REQUIRED_FIELDS),
        ("v1_output_limits", benchmark._V1_OUTPUT_LIMIT_REQUIRED_FIELDS),
        ("sqlite_pragmas", benchmark._SQLITE_PRAGMA_REQUIRED_FIELDS),
        ("environment", benchmark._ENVIRONMENT_REQUIRED_FIELDS),
        ("rss_measurement", benchmark._RSS_REQUIRED_FIELDS),
        ("bounded_phase_rss_measurement", benchmark._RSS_REQUIRED_FIELDS),
    )
    for container, fields in nested_fields:
        for field in fields:
            incomplete = deepcopy(report)
            del incomplete["cases"][0][container][field]
            with pytest.raises(RuntimeError):
                benchmark._validate_report(incomplete)

    for container in ("environment",):
        for field in benchmark._FILESYSTEM_REQUIRED_FIELDS:
            incomplete = deepcopy(report)
            del incomplete["cases"][0][container]["filesystem"][field]
            with pytest.raises(RuntimeError):
                benchmark._validate_report(incomplete)

    for field in benchmark._FORCED_TERMINATION_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete["forced_termination"][field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)
    for field in benchmark._FILESYSTEM_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete["forced_termination"]["filesystem"][field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)


@pytest.mark.parametrize(
    ("cleanup_field", "failed_value"),
    [
        ("worker_context", "refused"),
        ("workspace_exists_after_context", True),
        ("parent_watchdog", "failed"),
        ("scenario_root_exists_after_parent", True),
    ],
)
def test_benchmark_v2_rejects_failed_cleanup_even_in_complete_required_matrix(
    cleanup_field: str,
    failed_value: object,
) -> None:
    for required_scale in (False, True):
        report = _benchmark_v2_report(required_scale=required_scale)
        for case in report["cases"]:
            case["cleanup"][cleanup_field] = failed_value

        with pytest.raises(RuntimeError, match="cleanup did not succeed"):
            benchmark._validate_report(report)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), 2.0),
        (("source_dirty",), 0),
        (("cases", 0, "observed_nodes"), 25_000.0),
        (("cases", 0, "cleanup", "workspace_exists_after_context"), 0),
        (("cases", 0, "sqlite_pragmas", "synchronous"), False),
        (("cases", 0, "v1_output_limits", "storage_protocol_version"), True),
        (("cases", 0, "rss_measurement", "peak_bytes"), 400.0),
        (("forced_termination", "readiness_observed"), 1),
    ],
)
def test_benchmark_v2_rejects_cross_type_equal_substitutions(
    path: tuple[str | int, ...],
    replacement: object,
) -> None:
    report = _benchmark_v2_report()
    target: Any = report
    for part in path[:-1]:
        target = target[part]
    original = target[path[-1]]
    assert replacement == original
    assert type(replacement) is not type(original)
    target[path[-1]] = replacement

    with pytest.raises(RuntimeError):
        benchmark._validate_report(report)


def test_benchmark_v2_rejects_inconsistent_source_limits_and_filesystem() -> None:
    mutations = []

    source = _benchmark_v2_report()
    source["source_snapshot_after"]["revision"] = "3" * 40
    mutations.append(source)

    limits = _benchmark_v2_report(required_scale=True)
    limits["cases"][-1]["budgets"]["cache_bytes"] //= 2
    limits["cases"][-1]["sqlite_pragmas"]["cache_size"] //= 2
    mutations.append(limits)

    filesystem = _benchmark_v2_report()
    filesystem["forced_termination"]["filesystem"]["identity_sha256"] = "c" * 64
    mutations.append(filesystem)

    for report in mutations:
        with pytest.raises(RuntimeError):
            benchmark._validate_report(report)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("retained_nodes", 24_999),
        ("retained_edges", 24_998),
        ("topology_truncation_reasons", ["node_count_limit"]),
        ("topology_pages", 1),
        ("topology_decoded_bytes", 999),
        ("wall_seconds", 0.0),
        ("cpu_seconds", 0.0),
    ],
)
def test_benchmark_v2_rejects_inconsistent_generated_case_evidence(
    field: str,
    replacement: object,
) -> None:
    report = _benchmark_v2_report()
    report["cases"][0][field] = replacement

    with pytest.raises(RuntimeError):
        benchmark._validate_report(report)


def test_benchmark_v2_rejects_retired_prototype_implementation() -> None:
    report = _benchmark_v2_report(implementation="prototype-composite")
    with pytest.raises(RuntimeError, match="preparation benchmark implementation is invalid"):
        benchmark._validate_report(report)


def test_benchmark_v2_rejects_impossible_budget_limit_rss_and_environment_evidence() -> None:
    mutations = []

    budget = _benchmark_v2_report()
    budget["cases"][0]["budgets"]["cache_bytes"] = 16 * 1024 * 1024
    budget["cases"][0]["sqlite_pragmas"]["cache_size"] = -16 * 1024
    mutations.append(budget)

    limits = _benchmark_v2_report()
    limits["cases"][0]["v1_output_limits"]["combined_max_encoded_bytes"] = 1
    mutations.append(limits)

    rss = _benchmark_v2_report()
    rss["cases"][0]["rss_measurement"]["scope"] = "some memory scope"
    mutations.append(rss)

    environment = _benchmark_v2_report(required_scale=True)
    environment["cases"][-1]["environment"]["python"] = "different"
    mutations.append(environment)

    for report in mutations:
        with pytest.raises(RuntimeError):
            benchmark._validate_report(report)
