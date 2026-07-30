"""Ray-native workflow examples for the test project.

This module deliberately has no Django imports so its lightweight steps can be
imported by Ray workers without bootstrapping Django.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import time
from typing import Any, Literal

from django_ray.workflows import chain, group, map_step, report_progress, step

COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE = "Intentional complex workflow fixture failure"


class ComplexWorkflowFixtureError(RuntimeError):
    """Stable testproject-only failure used to exercise workflow diagnostics."""


def build_cpu_work_items(
    num_items: int,
    seconds_per_item: float,
) -> list[dict[str, Any]]:
    """Build the inputs for a dynamic workflow fan-out."""
    return [
        {
            "item_id": item_id,
            "seconds_per_item": seconds_per_item,
        }
        for item_id in range(num_items)
    ]


def run_cpu_work_item(item: dict[str, Any]) -> dict[str, Any]:
    """Burn CPU for one workflow leaf and report its execution details."""
    item_id = int(item["item_id"])
    fail_fixture = item.get("_fail_complex_workflow_fixture") is True
    duration = float(item["seconds_per_item"])
    wall_started_at = time.time()
    started_at = time.perf_counter()
    iterations = 0
    next_report = 0.25
    data = f"workflow_item_{item_id}".encode() * 100

    while (elapsed := time.perf_counter() - started_at) < duration:
        hashlib.sha256(data).digest()
        iterations += 1
        fraction = elapsed / duration
        if fraction >= next_report:
            report_progress(
                min(fraction, 1.0),
                1.0,
                message=f"Processing item {item_id}",
                metrics={"iterations": iterations},
            )
            next_report += 0.25

    elapsed = time.perf_counter() - started_at
    if fail_fixture:
        raise ComplexWorkflowFixtureError(COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE)
    report_progress(
        1.0,
        1.0,
        message=f"Processed item {item_id}",
        metrics={"iterations": iterations},
    )
    return {
        "item_id": item_id,
        "iterations": iterations,
        "elapsed_seconds": round(elapsed, 4),
        "worker_started_at": round(wall_started_at, 6),
    }


def summarize_cpu_workflow(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Gather leaf results and calculate observable parallelism."""
    total_leaf_seconds = sum(float(result["elapsed_seconds"]) for result in results)
    earliest_start = min(
        (float(result["worker_started_at"]) for result in results),
        default=0.0,
    )
    latest_finish = max(
        (
            float(result["worker_started_at"]) + float(result["elapsed_seconds"])
            for result in results
        ),
        default=earliest_start,
    )
    leaf_wall_seconds = max(0.0, latest_finish - earliest_start)

    return {
        "engine": "django-ray-workflow",
        "durability_boundary": "single RayTaskExecution",
        "leaf_tasks": len(results),
        "total_leaf_seconds": round(total_leaf_seconds, 4),
        "leaf_wall_seconds": round(leaf_wall_seconds, 4),
        "effective_parallelism": (
            round(total_leaf_seconds / leaf_wall_seconds, 2) if leaf_wall_seconds else 0
        ),
        "items": results,
    }


cpu_fanout_workflow = chain(
    step(build_cpu_work_items),
    map_step(
        run_cpu_work_item,
        ray_options={"num_cpus": 0.25},
    ),
    step(summarize_cpu_workflow),
)


def run_cpu_fanout_workflow(
    num_items: int,
    seconds_per_item: float,
    *,
    use_ray: bool | None = None,
) -> dict[str, Any]:
    """Run the example workflow and include end-to-end coordinator timing."""
    started_at = time.perf_counter()
    result = cpu_fanout_workflow.run(
        num_items,
        seconds_per_item,
        use_ray=use_ray,
    )
    result["workflow_elapsed_seconds"] = round(time.perf_counter() - started_at, 4)
    return result


def build_complex_config(
    fast_items: int,
    slow_items: int,
    fast_seconds: float,
    slow_seconds: float,
    failure_branch: str | None = None,
    failure_item: int | None = None,
) -> dict[str, Any]:
    """Build shared input for two nested workflow branches."""
    config: dict[str, Any] = {
        "fast": {"items": fast_items, "seconds": fast_seconds},
        "slow": {"items": slow_items, "seconds": slow_seconds},
    }
    if failure_branch is not None:
        config["failure"] = {
            "branch": failure_branch,
            "item": failure_item,
        }
    return config


def build_branch_work_items(
    config: dict[str, Any],
    branch: str,
) -> list[dict[str, Any]]:
    """Expand one branch's dynamic work."""
    branch_config = config[branch]
    failure = config.get("failure")
    items = []
    for item_id in range(int(branch_config["items"])):
        item = {
            "item_id": item_id,
            "seconds_per_item": branch_config["seconds"],
            "branch": branch,
        }
        if (
            isinstance(failure, dict)
            and failure.get("branch") == branch
            and failure.get("item") == item_id
        ):
            item["_fail_complex_workflow_fixture"] = True
        items.append(item)
    return items


def summarize_branch(
    results: list[dict[str, Any]],
    branch: str,
) -> dict[str, Any]:
    """Summarize one nested chain after its dynamic map completes."""
    summary = summarize_cpu_workflow(results)
    summary["branch"] = branch
    return summary


def summarize_complex_workflow(
    branches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Gather both nested branches into a final workflow result."""
    return {
        "engine": "django-ray-workflow",
        "shape": "chain(group(chain(map), chain(map)), step)",
        "durability_boundary": "single RayTaskExecution",
        "total_leaf_tasks": sum(int(branch["leaf_tasks"]) for branch in branches),
        "branches": branches,
    }


fast_branch_workflow = chain(
    step(build_branch_work_items, "fast"),
    map_step(run_cpu_work_item, ray_options={"num_cpus": 0.25}),
    step(summarize_branch, "fast"),
)

slow_branch_workflow = chain(
    step(build_branch_work_items, "slow"),
    map_step(run_cpu_work_item, ray_options={"num_cpus": 0.25}),
    step(summarize_branch, "slow"),
)

complex_branch_workflow = chain(
    step(build_complex_config),
    group(fast_branch_workflow, slow_branch_workflow),
    step(summarize_complex_workflow),
)


def run_complex_branch_workflow(
    fast_items: int,
    slow_items: int,
    fast_seconds: float,
    slow_seconds: float,
    *,
    failure_branch: str | None = None,
    failure_item: int | None = None,
    reporting_policy: Literal["full", "terminal_only"] | None = None,
    use_ray: bool | None = None,
) -> dict[str, Any]:
    """Run nested fast and slow branches and record total wall time."""
    started_at = time.perf_counter()
    workflow = (
        complex_branch_workflow
        if reporting_policy is None
        else complex_branch_workflow.with_progress_reporting(reporting_policy)
    )
    workflow_args: tuple[Any, ...] = (
        fast_items,
        slow_items,
        fast_seconds,
        slow_seconds,
    )
    if failure_branch is not None:
        workflow_args = (*workflow_args, failure_branch, failure_item)
    result = workflow.run(*workflow_args, use_ray=use_ray)
    result["workflow_elapsed_seconds"] = round(time.perf_counter() - started_at, 4)
    return result


def inspect_runtime_environment(package: str | None = None) -> dict[str, Any]:
    """Return observable details from the Ray worker's active environment."""
    package_version = None
    if package:
        try:
            package_version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_version = "not installed"

    return {
        "profile_marker": os.environ.get("DJANGO_RAY_RUNTIME_ENV", "unset"),
        "python_version": platform.python_version(),
        "package": package,
        "package_version": package_version,
    }


def run_runtime_env_cache_benchmark(
    profile: str,
    *,
    repeats: int = 2,
    package: str | None = None,
    use_ray: bool | None = None,
) -> dict[str, Any]:
    """Run the same profile repeatedly to expose cold and cached setup time."""
    runs: list[dict[str, Any]] = []
    probe = step(inspect_runtime_environment, package, runtime_env=profile)
    for index in range(repeats):
        started_at = time.perf_counter()
        details = probe.run(use_ray=use_ray)
        runs.append(
            {
                "run": index + 1,
                "elapsed_seconds": round(time.perf_counter() - started_at, 4),
                **details,
            }
        )

    return {
        "runtime_env_profile": profile,
        "runs": runs,
        "cache_speedup": (
            round(runs[0]["elapsed_seconds"] / runs[-1]["elapsed_seconds"], 2)
            if runs[-1]["elapsed_seconds"] > 0
            else None
        ),
    }
