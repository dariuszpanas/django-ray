"""Tests for the test project's user-facing workflow example."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from django_ray.workflow.plans import materialize_workflow_plan
from django_ray.workflows import Step, _Executor
from testproject.apps.cluster_tasks import workflows
from testproject.apps.cluster_tasks.workflows import (
    COMPLEX_WORKFLOW_FIXTURE_ERROR_MESSAGE,
    ComplexWorkflowFixtureError,
    build_branch_work_items,
    build_complex_config,
    inspect_runtime_environment,
    order_fulfillment_showcase_workflow,
    run_complex_branch_workflow,
    run_cpu_fanout_workflow,
    run_cpu_work_item,
    run_order_fulfillment_showcase_workflow,
    run_runtime_env_cache_benchmark,
)


@dataclass
class _ShowcaseGraphExecutor(_Executor):
    """Execute locally while retaining the runtime graph visible to Admin."""

    nodes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def submit_step(
        self,
        signature: Step,
        input_args: tuple[Any, ...],
        input_kwargs: dict[str, Any],
        node_id: str,
        dependencies: tuple[str, ...],
    ) -> Any:
        self.nodes[node_id] = dependencies
        self.labels[node_id] = signature.callable_path.rsplit(".", 1)[-1]
        module_path, callable_name = signature.callable_path.rsplit(".", 1)
        callable_obj = getattr(importlib.import_module(module_path), callable_name)
        return callable_obj(
            *input_args,
            *signature.bound_args,
            **{**input_kwargs, **signature.bound_kwargs},
        )

    def collect(self, values: list[Any]) -> list[Any]:
        return values

    def resolve(self, value: Any) -> Any:
        return value


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


@pytest.mark.parametrize("reporting_policy", ["terminal_only", "disabled"])
def test_complex_workflow_reporting_policy_selection_is_per_invocation(
    monkeypatch,
    reporting_policy: str,
) -> None:
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
        reporting_policy=reporting_policy,
        use_ray=True,
    )

    assert captured == {
        "policy": reporting_policy,
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


@pytest.mark.parametrize(
    ("item_count", "reserved_units", "total_cents"),
    [
        (1, 1, 1_000),
        (3, 4, 4_400),
    ],
)
def test_order_fulfillment_showcase_returns_one_compact_deterministic_result(
    item_count: int,
    reserved_units: int,
    total_cents: int,
) -> None:
    result = run_order_fulfillment_showcase_workflow(
        item_count,
        0,
        use_ray=False,
    )

    assert result == {
        "engine": "django-ray-workflow",
        "workflow": "order-fulfillment-showcase",
        "durability_boundary": "single RayTaskExecution",
        "order_id": "showcase-order-0001",
        "status": "FULFILLED",
        "item_count": item_count,
        "reserved_units": reserved_units,
        "currency": "USD",
        "total_cents": total_cents,
        "risk": "LOW",
        "recommendation": "PRIORITY_FULFILLMENT",
        "decision": "APPROVED",
        "sinks": {
            "primary": "WRITTEN",
            "audit": "WRITTEN",
            "notification": "SENT",
        },
    }
    assert set(result) == {
        "engine",
        "workflow",
        "durability_boundary",
        "order_id",
        "status",
        "item_count",
        "reserved_units",
        "currency",
        "total_cents",
        "risk",
        "recommendation",
        "decision",
        "sinks",
    }


def test_order_fulfillment_showcase_projects_a_clear_commercial_output() -> None:
    preview = workflows.preview_order_fulfillment_output(
        {
            "order_id": "showcase-order-0001",
            "currency": "USD",
            "total_cents": 4_400,
            "risk": "LOW",
            "risk_score": 0,
            "recommendation": "PRIORITY_FULFILLMENT",
        }
    )

    assert preview == {
        "order_id": "showcase-order-0001",
        "currency": "USD",
        "total_cents": 4_400,
        "risk": "LOW",
        "recommendation": "PRIORITY_FULFILLMENT",
    }
    assert "risk_score" not in preview


def test_order_fulfillment_showcase_map_previews_are_exact_and_bounded() -> None:
    assert workflows.preview_order_item_validation(
        {"item_id": 2, "valid": True, "sku": "must-not-cross"}
    ) == {"item_id": 2, "valid": True}
    assert workflows.preview_inventory_reservation(
        {
            "item_id": 2,
            "reserved_units": 3,
            "commercial": {"must_not_cross": True},
        }
    ) == {"item_id": 2, "reserved_units": 3}

    with pytest.raises(ValueError, match="validation preview"):
        workflows.preview_order_item_validation({"item_id": "2", "valid": True})
    with pytest.raises(ValueError, match="reservation preview"):
        workflows.preview_inventory_reservation({"item_id": 2})
    with pytest.raises(RuntimeError, match="intentional showcase"):
        workflows.preview_showcase_diagnostic_failure({"completed_orders": 12})


def test_order_fulfillment_showcase_failure_reaches_selected_leaf_locally() -> None:
    with pytest.raises(
        workflows.WorkflowShowcaseFixtureError,
        match=workflows.workflow_showcase_fixture_error_message(1),
    ):
        run_order_fulfillment_showcase_workflow(
            3,
            0,
            failure_stage="reserve_inventory",
            failure_item=1,
            use_ray=False,
        )


@pytest.mark.parametrize(
    ("item_count", "expected_nodes", "expected_edges", "layer_widths"),
    [
        (1, 21, 28, [1, 4, 2, 1, 4, 1, 1, 1, 1, 1, 3, 1]),
        (3, 25, 36, [1, 4, 4, 1, 4, 1, 1, 1, 3, 1, 3, 1]),
    ],
)
def test_order_fulfillment_showcase_has_stable_repeated_split_join_topology(
    item_count: int,
    expected_nodes: int,
    expected_edges: int,
    layer_widths: list[int],
) -> None:
    executor = _ShowcaseGraphExecutor()

    submission = order_fulfillment_showcase_workflow._submit(
        executor,
        (item_count, 0, None, None),
        {},
        "0",
        (),
    )

    layers: dict[str, int] = {}
    for node_id, dependencies in executor.nodes.items():
        layers[node_id] = (
            max(layers[dependency] for dependency in dependencies) + 1 if dependencies else 0
        )
    assert submission.terminal_node_ids == ("0.8",)
    assert len(executor.nodes) == expected_nodes
    assert sum(len(dependencies) for dependencies in executor.nodes.values()) == (expected_edges)
    assert max(layers.values()) + 1 == 12
    assert [
        sum(layer == layer_index for layer in layers.values()) for layer_index in range(12)
    ] == layer_widths

    validation_nodes = tuple(f"0.1.g0.1.m{index}" for index in range(item_count))
    reservation_nodes = tuple(f"0.5.m{index}" for index in range(item_count))
    assert executor.nodes["0.2"] == (
        *validation_nodes,
        "0.1.g1.1",
        "0.1.g2",
    )
    assert executor.nodes["0.4"] == ("0.3.g0", "0.3.g1.1")
    assert all(executor.nodes[node_id] == ("0.4",) for node_id in reservation_nodes)
    assert executor.nodes["0.6"] == reservation_nodes
    assert executor.nodes["0.8"] == ("0.7.g0", "0.7.g1", "0.7.g2")
    assert executor.labels["0.4"] == "attach_commercial_context_to_reservations"
    assert executor.labels["0.5.m0"] == "reserve_inventory"
    assert executor.labels["0.6"] == "join_fulfillment_decision"
    assert executor.labels["0.8"] == "finalize_order_fulfillment"

    def ancestors(node_id: str) -> set[str]:
        found: set[str] = set()
        pending = list(executor.nodes[node_id])
        while pending:
            dependency = pending.pop()
            if dependency in found:
                continue
            found.add(dependency)
            pending.extend(executor.nodes[dependency])
        return found

    commercial_nodes = {
        "0.3.g1.0.g0",
        "0.3.g1.0.g1.0.g0",
        "0.3.g1.0.g1.0.g1",
        "0.3.g1.0.g1.1",
        "0.3.g1.1",
    }
    assert commercial_nodes <= ancestors("0.5.m0")
    assert {node_id for node_id in executor.nodes if "0.5.m0" in ancestors(node_id)} == {
        "0.6",
        "0.7.g0",
        "0.7.g1",
        "0.7.g2",
        "0.8",
    }


def test_order_fulfillment_showcase_materializes_stable_business_callables() -> None:
    manifest = materialize_workflow_plan(
        order_fulfillment_showcase_workflow,
        invocation_args=(3, 0.05, None, None),
        invocation_kwargs={},
    ).plan.as_dict()

    assert manifest["topology"]["class"] == "dynamic"
    assert len(manifest["nodes"]) == 31
    assert len(manifest["edges"]) == 38
    assert [
        callable_entry["import_path"].rsplit(".", 1)[-1] for callable_entry in manifest["callables"]
    ] == [
        "build_order_batch",
        "preview_order_fulfillment_output",
        "select_validation_items",
        "validate_order_item",
        "preview_order_item_validation",
        "load_customer_profile",
        "load_customer_history",
        "preview_showcase_diagnostic_failure",
        "join_customer_context",
        "load_inventory_snapshot",
        "join_order_inputs",
        "select_reservation_items",
        "calculate_order_price",
        "score_order_risk",
        "build_order_recommendation",
        "join_risk_recommendation",
        "join_commercial_context",
        "attach_commercial_context_to_reservations",
        "reserve_inventory",
        "preview_inventory_reservation",
        "join_fulfillment_decision",
        "write_primary_order",
        "write_audit_record",
        "send_order_notification",
        "finalize_order_fulfillment",
    ]
    preview_by_node = {
        node["id"]: node["output_preview"] for node in manifest["nodes"] if "output_preview" in node
    }
    assert set(preview_by_node) == {node["id"] for node in manifest["nodes"] if "callable" in node}
    assert preview_by_node["0.1.g0.1.m*"] == {
        "mode": "author_projection",
        "callable": {"ref": "callable:4"},
        "limits_profile": "v1",
    }
    assert preview_by_node["0.1.g1.0.g1"] == {
        "mode": "author_projection",
        "callable": {"ref": "callable:7"},
        "limits_profile": "v1",
    }
    assert preview_by_node["0.5.m*"] == {
        "mode": "author_projection",
        "callable": {"ref": "callable:19"},
        "limits_profile": "v1",
    }
    assert all(
        contract["mode"] == "author_projection" and contract["limits_profile"] == "v1"
        for contract in preview_by_node.values()
    )
    reserve_template = next(node for node in manifest["nodes"] if node["id"] == "0.5.m*")
    assert reserve_template["ray_options"] == {
        "max_retries": 0,
        "num_cpus": 0.1,
    }


def test_order_fulfillment_showcase_always_selects_full_reporting(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    class _ConfiguredWorkflow:
        def run(self, *args: Any, **kwargs: Any) -> dict[str, str]:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"status": "ok"}

    class _Workflow:
        def with_progress_reporting(self, policy: str) -> _ConfiguredWorkflow:
            captured["policy"] = policy
            return _ConfiguredWorkflow()

    monkeypatch.setattr(
        workflows,
        "order_fulfillment_showcase_workflow",
        _Workflow(),
    )

    assert run_order_fulfillment_showcase_workflow(3, 0.05, use_ray=True) == {"status": "ok"}
    assert captured == {
        "policy": "full",
        "args": (3, 0.05, None, None),
        "kwargs": {"use_ray": True},
    }


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
