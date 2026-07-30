"""Tests for the test project's user-facing workflow example."""

from __future__ import annotations

from typing import Any

import pytest

from testproject.apps.cluster_tasks import workflows
from testproject.apps.cluster_tasks.workflows import (
    COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE,
    ComplexWorkflowFixtureError,
    build_branch_work_items,
    build_complex_config,
    inspect_runtime_environment,
    run_complex_branch_workflow,
    run_cpu_fanout_workflow,
    run_cpu_work_item,
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


def test_complex_workflow_default_config_remains_unchanged() -> None:
    config = build_complex_config(3, 2, 0.01, 0.02)

    assert config == {
        "fast": {"items": 3, "seconds": 0.01},
        "slow": {"items": 2, "seconds": 0.02},
    }
    assert build_branch_work_items(config, "fast") == [
        {"item_id": 0, "seconds_per_item": 0.01, "branch": "fast"},
        {"item_id": 1, "seconds_per_item": 0.01, "branch": "fast"},
        {"item_id": 2, "seconds_per_item": 0.01, "branch": "fast"},
    ]


def test_complex_workflow_terminal_only_selection_is_per_invocation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Configured:
        def run(self, *args: Any, **kwargs: Any) -> dict[str, list[object]]:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"branches": []}

    class _Workflow:
        def with_progress_reporting(self, policy: str) -> _Configured:
            captured["policy"] = policy
            return _Configured()

    monkeypatch.setattr(workflows, "complex_branch_workflow", _Workflow())

    result = workflows.run_complex_branch_workflow(
        2,
        1,
        0.01,
        0.02,
        reporting_policy="terminal_only",
        use_ray=True,
    )

    assert captured == {
        "policy": "terminal_only",
        "args": (2, 1, 0.01, 0.02),
        "kwargs": {"use_ray": True},
    }
    assert result["workflow_elapsed_seconds"] >= 0


def test_complex_workflow_failure_selects_one_stable_leaf_and_keeps_sibling_valid() -> None:
    config = build_complex_config(3, 2, 0.01, 0.01, "fast", 1)
    fast_items = build_branch_work_items(config, "fast")
    slow_items = build_branch_work_items(config, "slow")

    assert [
        item["item_id"] for item in fast_items if item.get("_fail_complex_workflow_fixture") is True
    ] == [1]
    assert all("_fail_complex_workflow_fixture" not in item for item in slow_items)
    sibling_result = run_cpu_work_item(slow_items[0])
    assert sibling_result["item_id"] == 0
    with pytest.raises(
        ComplexWorkflowFixtureError,
        match=COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE,
    ):
        run_cpu_work_item(fast_items[1])


def test_complex_workflow_failure_control_reaches_selected_leaf_locally() -> None:
    with pytest.raises(
        ComplexWorkflowFixtureError,
        match=COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE,
    ):
        run_complex_branch_workflow(
            fast_items=2,
            slow_items=2,
            fast_seconds=0.01,
            slow_seconds=0.01,
            failure_branch="slow",
            failure_item=1,
            use_ray=False,
        )


def test_runtime_env_cache_benchmark_has_local_fallback(settings) -> None:
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RUNTIME_ENV_PROFILES": {"thin": {}},
    }
    result = run_runtime_env_cache_benchmark(
        "thin",
        repeats=2,
        use_ray=False,
    )

    assert result["runtime_env_profile"] == "thin"
    assert [run["run"] for run in result["runs"]] == [1, 2]


def test_runtime_env_probe_reads_distribution_metadata(monkeypatch) -> None:
    monkeypatch.setenv(
        "DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE",
        "django-ray-runtime-env-encryption-canary-v1-7c4e2a91",
    )
    monkeypatch.setattr("importlib.metadata.version", lambda package: f"{package}-version")

    result = inspect_runtime_environment("sample-package")

    assert result["package_version"] == "sample-package-version"
    assert result["storage_encryption_verified"] is True


def test_runtime_env_probe_handles_missing_distribution(monkeypatch) -> None:
    monkeypatch.delenv("DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE", raising=False)

    def _missing(package: str) -> str:
        from importlib.metadata import PackageNotFoundError

        raise PackageNotFoundError(package)

    monkeypatch.setattr("importlib.metadata.version", _missing)

    result = inspect_runtime_environment("missing-package")

    assert result["package_version"] == "not installed"
    assert result["storage_encryption_verified"] is False
