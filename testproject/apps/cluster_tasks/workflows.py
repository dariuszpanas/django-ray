"""Ray-native workflow examples for the test project.

This module deliberately has no Django imports so its lightweight steps can be
imported by Ray workers without bootstrapping Django.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
import os
import platform
import time
from collections.abc import Callable
from typing import Any, Literal

from django_ray.workflows import Step, chain, group, map_step, report_progress, step

COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE = "Intentional complex workflow fixture failure"
WORKFLOW_SHOWCASE_FIXTURE_ERROR_MESSAGE = "Intentional workflow showcase reserve_inventory failure"
WORKFLOW_SHOWCASE_MAX_ITEMS = 8
WORKFLOW_SHOWCASE_MAX_WORK_SECONDS = 1.0
WORKFLOW_SHOWCASE_FAILURE_STAGE = "reserve_inventory"
WORKFLOW_RECOVERY_EARLY_STAGE = "build_order_batch"
WORKFLOW_RECOVERY_MID_STAGE = "join_order_inputs"
WORKFLOW_RECOVERY_SUCCESS_STAGE = "complete"
WORKFLOW_RECOVERY_STAGES = frozenset(
    {
        WORKFLOW_RECOVERY_EARLY_STAGE,
        WORKFLOW_RECOVERY_MID_STAGE,
        WORKFLOW_RECOVERY_SUCCESS_STAGE,
    }
)
WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE = (
    "Intentional workflow recovery failure at build_order_batch on durable attempt 1"
)
WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE = (
    "Intentional workflow recovery failure at join_order_inputs on durable attempt 2"
)


class ComplexWorkflowFixtureError(RuntimeError):
    """Stable testproject-only failure used to exercise workflow diagnostics."""


class WorkflowShowcaseFixtureError(RuntimeError):
    """Stable testproject-only failure for the order-fulfillment showcase."""


class WorkflowRecoveryEarlyFixtureError(RuntimeError):
    """Stable retryable failure for the recovery showcase's first attempt."""


class WorkflowRecoveryMidFixtureError(RuntimeError):
    """Stable retryable failure for the recovery showcase's second attempt."""


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
    reporting_policy: Literal["full", "terminal_only", "disabled"] | None = None,
    use_ray: bool | None = None,
) -> dict[str, Any]:
    """Run nested branches with an optional per-invocation reporting policy."""
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


def validate_order_fulfillment_showcase_inputs(
    *,
    item_count: int,
    work_seconds: float,
    failure_stage: str | None,
    failure_item: int | None,
) -> None:
    """Validate the deliberately small, visible workflow showcase."""
    if isinstance(item_count, bool) or not isinstance(item_count, int):
        raise ValueError("item_count must be an integer")
    if not 1 <= item_count <= WORKFLOW_SHOWCASE_MAX_ITEMS:
        raise ValueError(f"item_count must be between 1 and {WORKFLOW_SHOWCASE_MAX_ITEMS}")
    if isinstance(work_seconds, bool) or not isinstance(work_seconds, int | float):
        raise ValueError("work_seconds must be a number")
    if not math.isfinite(work_seconds) or not (
        0 <= work_seconds <= WORKFLOW_SHOWCASE_MAX_WORK_SECONDS
    ):
        raise ValueError(
            f"work_seconds must be finite and between 0 and {WORKFLOW_SHOWCASE_MAX_WORK_SECONDS:g}"
        )
    if (failure_stage is None) != (failure_item is None):
        raise ValueError("failure_stage and failure_item must be provided together")
    if failure_stage is None:
        return
    if failure_stage != WORKFLOW_SHOWCASE_FAILURE_STAGE:
        raise ValueError(f"failure_stage must be '{WORKFLOW_SHOWCASE_FAILURE_STAGE}'")
    if isinstance(failure_item, bool) or not isinstance(failure_item, int):
        raise ValueError("failure_item must be an integer")
    if not 0 <= failure_item < item_count:
        raise ValueError("failure_item must select an item in the order batch")


def workflow_showcase_fixture_error_message(item_id: int) -> str:
    """Return the stable error text shared by local and Ray-wrapped failures."""
    return f"{WORKFLOW_SHOWCASE_FIXTURE_ERROR_MESSAGE} at item {item_id}"


def build_order_batch(
    item_count: int,
    work_seconds: float,
    failure_stage: str | None = None,
    failure_item: int | None = None,
) -> dict[str, Any]:
    """Build one deterministic bounded order batch for repeated DAG joins."""
    validate_order_fulfillment_showcase_inputs(
        item_count=item_count,
        work_seconds=work_seconds,
        failure_stage=failure_stage,
        failure_item=failure_item,
    )
    return {
        "order_id": "showcase-order-0001",
        "customer_id": "showcase-customer-0042",
        "items": [
            {
                "item_id": item_id,
                "sku": f"SKU-{item_id + 1:03d}",
                "quantity": (item_id % 2) + 1,
                "unit_price_cents": 1_000 + (item_id * 100),
            }
            for item_id in range(item_count)
        ],
        "work_seconds": float(work_seconds),
        "failure": (
            {"stage": failure_stage, "item_id": failure_item} if failure_stage is not None else None
        ),
    }


def build_recovery_order_batch(
    item_count: int,
    work_seconds: float,
    recovery_stage: str,
) -> dict[str, Any]:
    """Build the recovery batch or fail at the deterministic entry boundary."""
    if recovery_stage not in WORKFLOW_RECOVERY_STAGES:
        raise ValueError("recovery_stage is not a supported showcase stage")
    if recovery_stage == WORKFLOW_RECOVERY_EARLY_STAGE:
        raise WorkflowRecoveryEarlyFixtureError(WORKFLOW_RECOVERY_EARLY_FAILURE_MESSAGE)
    batch = build_order_batch(item_count, work_seconds)
    batch["recovery_stage"] = recovery_stage
    return batch


def select_validation_items(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Select compact item inputs for the first dynamic fan-out."""
    return [
        {
            "order_id": batch["order_id"],
            "item_id": item["item_id"],
            "sku": item["sku"],
            "quantity": item["quantity"],
            "work_seconds": batch["work_seconds"],
        }
        for item in batch["items"]
    ]


def validate_order_item(item: dict[str, Any]) -> dict[str, Any]:
    """Validate one order item without retaining the complete order payload."""
    work_seconds = float(item["work_seconds"])
    if work_seconds:
        time.sleep(work_seconds)
    item_id = int(item["item_id"])
    valid = bool(item["sku"]) and int(item["quantity"]) > 0
    report_progress(
        1,
        1,
        message=f"Validated order item {item_id}",
        metrics={"item_id": item_id, "valid": valid},
    )
    return {
        "order_id": item["order_id"],
        "item_id": item_id,
        "valid": valid,
    }


def load_customer_profile(batch: dict[str, Any]) -> dict[str, Any]:
    """Load the profile half of the nested customer-context join."""
    return {
        "order_id": batch["order_id"],
        "customer_id": batch["customer_id"],
        "tier": "GOLD",
        "region": "us-west",
    }


def load_customer_history(batch: dict[str, Any]) -> dict[str, Any]:
    """Load the history half of the nested customer-context join."""
    return {
        "order_id": batch["order_id"],
        "customer_id": batch["customer_id"],
        "completed_orders": 12,
        "chargebacks": 0,
    }


def join_customer_context(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Join the independently loaded profile and history."""
    profile, history = parts
    return {
        "order_id": profile["order_id"],
        "customer_id": profile["customer_id"],
        "tier": profile["tier"],
        "region": profile["region"],
        "completed_orders": history["completed_orders"],
        "chargebacks": history["chargebacks"],
    }


def load_inventory_snapshot(batch: dict[str, Any]) -> dict[str, Any]:
    """Load inventory and carry the bounded order into the next join."""
    return {
        "order_id": batch["order_id"],
        "items": batch["items"],
        "available_units": {
            str(item["item_id"]): int(item["quantity"]) + 2 for item in batch["items"]
        },
        "work_seconds": batch["work_seconds"],
        "failure": batch["failure"],
    }


def load_recovery_inventory_snapshot(batch: dict[str, Any]) -> dict[str, Any]:
    """Carry the fixed recovery stage through the upstream inventory branch."""
    snapshot = load_inventory_snapshot(batch)
    snapshot["recovery_stage"] = batch["recovery_stage"]
    return snapshot


def join_order_inputs(parts: list[Any]) -> dict[str, Any]:
    """Join validation, customer, and inventory inputs."""
    validations, customer, inventory = parts
    if not all(validation["valid"] is True for validation in validations):
        raise ValueError("order showcase validation unexpectedly rejected an item")
    return {
        "order_id": inventory["order_id"],
        "items": inventory["items"],
        "validated_item_ids": [int(validation["item_id"]) for validation in validations],
        "customer": customer,
        "available_units": inventory["available_units"],
        "work_seconds": inventory["work_seconds"],
        "failure": inventory["failure"],
    }


def join_recovery_order_inputs(parts: list[Any]) -> dict[str, Any]:
    """Join completed upstream work or fail at the deterministic midpoint."""
    context = join_order_inputs(parts)
    inventory = parts[2]
    if inventory["recovery_stage"] == WORKFLOW_RECOVERY_MID_STAGE:
        raise WorkflowRecoveryMidFixtureError(WORKFLOW_RECOVERY_MID_FAILURE_MESSAGE)
    return context


def select_reservation_items(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Select one bounded reservation input per order item."""
    failure = context["failure"]
    return [
        {
            "order_id": context["order_id"],
            "item_id": item["item_id"],
            "sku": item["sku"],
            "quantity": item["quantity"],
            "available_units": context["available_units"][str(item["item_id"])],
            "work_seconds": context["work_seconds"],
            "_fail_workflow_showcase_fixture": (
                isinstance(failure, dict)
                and failure.get("stage") == WORKFLOW_SHOWCASE_FAILURE_STAGE
                and failure.get("item_id") == item["item_id"]
            ),
        }
        for item in context["items"]
    ]


def reserve_inventory(item: dict[str, Any]) -> dict[str, Any]:
    """Reserve one item or raise the exact opt-in showcase failure."""
    item_id = int(item["item_id"])
    fail_fixture = item.get("_fail_workflow_showcase_fixture") is True
    report_progress(
        0,
        1,
        message=f"Reserving inventory for item {item_id}",
        metrics={"item_id": item_id},
    )
    work_seconds = float(item["work_seconds"])
    if work_seconds:
        time.sleep(work_seconds)
    if fail_fixture:
        raise WorkflowShowcaseFixtureError(workflow_showcase_fixture_error_message(item_id))
    quantity = int(item["quantity"])
    if quantity > int(item["available_units"]):
        raise ValueError(f"insufficient showcase inventory for item {item_id}")
    report_progress(
        1,
        1,
        message=f"Reserved inventory for item {item_id}",
        metrics={"item_id": item_id, "reserved_units": quantity},
    )
    return {
        "order_id": item["order_id"],
        "item_id": item_id,
        "reserved_units": quantity,
        "commercial": item["commercial"],
    }


def calculate_order_price(context: dict[str, Any]) -> dict[str, Any]:
    """Calculate deterministic order pricing in parallel with risk work."""
    return {
        "order_id": context["order_id"],
        "currency": "USD",
        "total_cents": sum(
            int(item["quantity"]) * int(item["unit_price_cents"]) for item in context["items"]
        ),
    }


def score_order_risk(context: dict[str, Any]) -> dict[str, Any]:
    """Score deterministic customer risk for the nested commercial join."""
    score = int(context["customer"]["chargebacks"]) * 25
    return {
        "order_id": context["order_id"],
        "risk": "LOW" if score < 25 else "REVIEW",
        "risk_score": score,
    }


def build_order_recommendation(context: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic fulfillment recommendation."""
    return {
        "order_id": context["order_id"],
        "recommendation": (
            "PRIORITY_FULFILLMENT"
            if context["customer"]["tier"] == "GOLD"
            else "STANDARD_FULFILLMENT"
        ),
    }


def join_risk_recommendation(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Join risk and recommendation before the price branch converges."""
    risk, recommendation = parts
    return {
        "order_id": risk["order_id"],
        "risk": risk["risk"],
        "risk_score": risk["risk_score"],
        "recommendation": recommendation["recommendation"],
    }


def join_commercial_context(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Join pricing with the nested risk/recommendation result."""
    price, risk_recommendation = parts
    return {
        "order_id": price["order_id"],
        "currency": price["currency"],
        "total_cents": price["total_cents"],
        "risk": risk_recommendation["risk"],
        "risk_score": risk_recommendation["risk_score"],
        "recommendation": risk_recommendation["recommendation"],
    }


def attach_commercial_context_to_reservations(
    parts: list[Any],
) -> list[dict[str, Any]]:
    """Make commercial completion a structural dependency of every reservation."""
    reservation_items, commercial = parts
    if not reservation_items:
        raise ValueError("order showcase reservation items cannot be empty")
    if any(item["order_id"] != commercial["order_id"] for item in reservation_items):
        raise ValueError("order showcase commercial context belongs to another order")
    return [{**item, "commercial": commercial} for item in reservation_items]


def join_fulfillment_decision(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Fan in reservations and derive one fulfillment decision."""
    if not results:
        raise ValueError("order showcase reservation results cannot be empty")
    order_id = results[0]["order_id"]
    commercial = results[0]["commercial"]
    if any(
        result["order_id"] != order_id or result["commercial"] != commercial for result in results
    ):
        raise ValueError("order showcase reservation results disagree")
    reserved_item_ids = [int(result["item_id"]) for result in results]
    reserved_units = sum(int(result["reserved_units"]) for result in results)
    approved = len(reserved_item_ids) == len(set(reserved_item_ids)) and commercial["risk"] == "LOW"
    return {
        "order_id": order_id,
        "item_count": len(results),
        "reserved_units": reserved_units,
        "currency": commercial["currency"],
        "total_cents": commercial["total_cents"],
        "risk": commercial["risk"],
        "recommendation": commercial["recommendation"],
        "decision": "APPROVED" if approved else "REVIEW",
    }


def write_primary_order(decision: dict[str, Any]) -> dict[str, Any]:
    """Simulate the primary persistence sink without external side effects."""
    return {
        "sink": "primary",
        "status": "WRITTEN",
        **decision,
    }


def write_audit_record(decision: dict[str, Any]) -> dict[str, Any]:
    """Simulate the independent audit sink."""
    return {
        "sink": "audit",
        "status": "WRITTEN",
        "order_id": decision["order_id"],
    }


def send_order_notification(decision: dict[str, Any]) -> dict[str, Any]:
    """Simulate the independent notification sink."""
    return {
        "sink": "notification",
        "status": "SENT",
        "order_id": decision["order_id"],
    }


def finalize_order_fulfillment(sinks: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a compact deterministic result after all three sinks join."""
    by_sink = {str(sink["sink"]): sink for sink in sinks}
    if set(by_sink) != {"primary", "audit", "notification"}:
        raise ValueError("order showcase sink set is incomplete")
    primary = by_sink["primary"]
    return {
        "engine": "django-ray-workflow",
        "workflow": "order-fulfillment-showcase",
        "durability_boundary": "single RayTaskExecution",
        "order_id": primary["order_id"],
        "status": "FULFILLED",
        "item_count": primary["item_count"],
        "reserved_units": primary["reserved_units"],
        "currency": primary["currency"],
        "total_cents": primary["total_cents"],
        "risk": primary["risk"],
        "recommendation": primary["recommendation"],
        "decision": primary["decision"],
        "sinks": {name: by_sink[name]["status"] for name in ("primary", "audit", "notification")},
    }


def _showcase_step(callable_obj: Callable[..., Any]) -> Step:
    """Create one lightweight business-labelled showcase step."""
    return step(callable_obj, ray_options={"num_cpus": 0.1})


_validation_showcase_branch = chain(
    _showcase_step(select_validation_items),
    map_step(validate_order_item, ray_options={"num_cpus": 0.1}),
)

_customer_showcase_branch = chain(
    group(
        _showcase_step(load_customer_profile),
        _showcase_step(load_customer_history),
    ),
    _showcase_step(join_customer_context),
)

_risk_recommendation_showcase_branch = chain(
    group(
        _showcase_step(score_order_risk),
        _showcase_step(build_order_recommendation),
    ),
    _showcase_step(join_risk_recommendation),
)

_commercial_showcase_branch = chain(
    group(
        _showcase_step(calculate_order_price),
        _risk_recommendation_showcase_branch,
    ),
    _showcase_step(join_commercial_context),
)

order_fulfillment_showcase_workflow = chain(
    _showcase_step(build_order_batch),
    group(
        _validation_showcase_branch,
        _customer_showcase_branch,
        _showcase_step(load_inventory_snapshot),
    ),
    _showcase_step(join_order_inputs),
    group(
        _showcase_step(select_reservation_items),
        _commercial_showcase_branch,
    ),
    _showcase_step(attach_commercial_context_to_reservations),
    map_step(
        reserve_inventory,
        ray_options={"num_cpus": 0.1, "max_retries": 0},
    ),
    _showcase_step(join_fulfillment_decision),
    group(
        _showcase_step(write_primary_order),
        _showcase_step(write_audit_record),
        _showcase_step(send_order_notification),
    ),
    _showcase_step(finalize_order_fulfillment),
)


order_fulfillment_recovery_showcase_workflow = chain(
    _showcase_step(build_recovery_order_batch),
    group(
        _validation_showcase_branch,
        _customer_showcase_branch,
        _showcase_step(load_recovery_inventory_snapshot),
    ),
    _showcase_step(join_recovery_order_inputs),
    group(
        _showcase_step(select_reservation_items),
        _commercial_showcase_branch,
    ),
    _showcase_step(attach_commercial_context_to_reservations),
    map_step(
        reserve_inventory,
        ray_options={"num_cpus": 0.1, "max_retries": 0},
    ),
    _showcase_step(join_fulfillment_decision),
    group(
        _showcase_step(write_primary_order),
        _showcase_step(write_audit_record),
        _showcase_step(send_order_notification),
    ),
    _showcase_step(finalize_order_fulfillment),
)


def run_order_fulfillment_showcase_workflow(
    item_count: int,
    work_seconds: float,
    *,
    failure_stage: str | None = None,
    failure_item: int | None = None,
    use_ray: bool | None = None,
) -> dict[str, Any]:
    """Run the repeated split/join showcase with full progress reporting."""
    return order_fulfillment_showcase_workflow.with_progress_reporting("full").run(
        item_count,
        work_seconds,
        failure_stage,
        failure_item,
        use_ray=use_ray,
    )


def run_order_fulfillment_recovery_showcase_workflow(
    item_count: int,
    work_seconds: float,
    recovery_stage: str,
    *,
    use_ray: bool | None = None,
) -> dict[str, Any]:
    """Run one attempt of the fixed three-attempt recovery demonstration."""
    return order_fulfillment_recovery_showcase_workflow.with_progress_reporting("full").run(
        item_count,
        work_seconds,
        recovery_stage,
        use_ray=use_ray,
    )


def inspect_runtime_environment(package: str | None = None) -> dict[str, Any]:
    """Return observable details from the Ray worker's active environment."""
    storage_probe_marker = "django-ray-runtime-env-encryption-canary-v1-7c4e2a91"
    package_version = None
    if package:
        try:
            package_version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_version = "not installed"

    return {
        "profile_marker": os.environ.get("DJANGO_RAY_RUNTIME_ENV", "unset"),
        "storage_encryption_verified": (
            os.environ.get("DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE") == storage_probe_marker
        ),
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
