"""Bounded definition checks for the plan overflow qualification workload."""

import pytest

from django_ray.workflow.plans import MAX_PLAN_NODES, materialize_workflow_plan
from testproject.apps.cluster_tasks.workflows import (
    build_plan_overflow_workflow,
    plan_overflow_identity,
)


def test_fixture_crosses_real_plan_cap_without_changing_result():
    workflow = build_plan_overflow_workflow()
    assert len(workflow.signatures) == MAX_PLAN_NODES + 1 == 65
    assert workflow.run(42, use_ray=False) == 42
    materialized = materialize_workflow_plan(workflow, invocation_args=(42,))
    manifest = materialized.plan.as_dict()
    assert manifest["nodes"] == []
    assert manifest["snapshot"]["state"] == "overflow"
    assert "node_limit" in manifest["snapshot"]["reasons"]
    assert manifest["snapshot"]["observed_node_count"] == 65


@pytest.mark.parametrize("value", [True, 41, "42", [42], None])
def test_oversized_fixture_rejects_unbounded_or_wrong_payload(value):
    with pytest.raises(ValueError, match="fixed scalar"):
        plan_overflow_identity(value)


def overflow_graph():
    return {
        "nodes": [{"id": f"0.{i}", "state": "SUCCEEDED"} for i in range(65)],
        "edges": [{"source": f"0.{i}", "target": f"0.{i + 1}"} for i in range(64)],
    }


def test_plan_overflow_graph_preserves_executed_chain():
    from qualification.application.workflow_fixtures import verify_plan_overflow_graph

    verify_plan_overflow_graph(overflow_graph())


@pytest.mark.parametrize(
    "change", ["empty", "missing-node", "duplicate-node", "state", "missing-edge", "reversed-edge"]
)
def test_plan_overflow_graph_rejects_incomplete_or_wrong_execution(change):
    from qualification.application.workflow_fixtures import verify_plan_overflow_graph

    graph = overflow_graph()
    if change == "empty":
        graph = {"nodes": [], "edges": []}
    elif change == "missing-node":
        graph["nodes"].pop()
    elif change == "duplicate-node":
        graph["nodes"][-1] = graph["nodes"][0].copy()
    elif change == "state":
        graph["nodes"][-1]["state"] = "RUNNING"
    elif change == "missing-edge":
        graph["edges"].pop()
    else:
        graph["edges"][0] = {"source": "0.1", "target": "0.0"}
    with pytest.raises(ValueError, match="Plan overflow fixture"):
        verify_plan_overflow_graph(graph)


def test_plan_overflow_manifest_verifies_real_compiler_output():
    from qualification.application.workflow_fixtures import verify_plan_overflow_manifest

    manifest = materialize_workflow_plan(
        build_plan_overflow_workflow(), invocation_args=(42,)
    ).plan.as_dict()
    verify_plan_overflow_manifest(manifest)
    manifest["snapshot"]["observed_node_count"] = 64
    with pytest.raises(ValueError, match="expected sentinel"):
        verify_plan_overflow_manifest(manifest)


@pytest.mark.django_db
@pytest.mark.parametrize("result", ["42", "41", "true", None])
def test_stored_overflow_requires_the_fixed_durable_result(result):
    from django_ray.models import RayTaskExecution
    from qualification.application.run_workflows import verify_plan_overflow_storage

    plan = materialize_workflow_plan(build_plan_overflow_workflow(), invocation_args=(42,)).plan
    row = RayTaskExecution.objects.create(
        task_id="overflow-result-test",
        callable_path="testproject.apps.cluster_tasks.tasks.plan_overflow_workflow_qualification",
        state="SUCCEEDED",
        workflow_plan_json=plan.canonical_json,
        result_data=result,
    )
    if result == "42":
        verify_plan_overflow_storage(row.pk)
    else:
        with pytest.raises(RayTaskExecution.DoesNotExist):
            verify_plan_overflow_storage(row.pk)
