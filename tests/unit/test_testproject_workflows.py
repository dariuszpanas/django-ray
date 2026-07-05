"""Tests for the test project's user-facing workflow example."""

from __future__ import annotations

from testproject.apps.cluster_tasks.workflows import (
    inspect_runtime_environment,
    run_complex_branch_workflow,
    run_cpu_fanout_workflow,
    run_runtime_env_cache_benchmark,
)


def test_cpu_fanout_workflow_runs_locally() -> None:
    result = run_cpu_fanout_workflow(
        num_items=4,
        seconds_per_item=0.01,
        use_ray=False,
    )

    assert result["engine"] == "django-ray-workflow"
    assert result["durability_boundary"] == "single RayTaskExecution"
    assert result["leaf_tasks"] == 4
    assert result["total_leaf_seconds"] >= 0.04
    assert result["workflow_elapsed_seconds"] >= 0.04
    assert [item["item_id"] for item in result["items"]] == [0, 1, 2, 3]


def test_complex_workflow_runs_nested_branches_locally() -> None:
    result = run_complex_branch_workflow(
        fast_items=3,
        slow_items=2,
        fast_seconds=0.01,
        slow_seconds=0.02,
        use_ray=False,
    )

    assert result["shape"] == "chain(group(chain(map), chain(map)), step)"
    assert result["total_leaf_tasks"] == 5
    assert [branch["branch"] for branch in result["branches"]] == ["fast", "slow"]


def test_runtime_env_cache_benchmark_has_local_fallback() -> None:
    result = run_runtime_env_cache_benchmark(
        "thin",
        repeats=2,
        use_ray=False,
    )

    assert result["runtime_env_profile"] == "thin"
    assert [run["run"] for run in result["runs"]] == [1, 2]


def test_runtime_env_probe_reads_distribution_metadata(monkeypatch) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda package: f"{package}-version")

    result = inspect_runtime_environment("sample-package")

    assert result["package_version"] == "sample-package-version"


def test_runtime_env_probe_handles_missing_distribution(monkeypatch) -> None:
    def _missing(package: str) -> str:
        from importlib.metadata import PackageNotFoundError

        raise PackageNotFoundError(package)

    monkeypatch.setattr("importlib.metadata.version", _missing)

    result = inspect_runtime_environment("missing-package")

    assert result["package_version"] == "not installed"
