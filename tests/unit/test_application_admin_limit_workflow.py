"""The display-limit workload must exceed the real limit with fixed work."""

import pytest

from django_ray.workflow.admin_graph import ADMIN_WORKFLOW_GRAPH_MAX_NODES
from django_ray.workflow.plans import materialize_workflow_plan
from testproject.apps.cluster_tasks.workflows import (
    admin_limit_identity,
    build_admin_limit_workflow,
)


def test_fixed_workload_crosses_admin_limit_and_preserves_result():
    workflow = build_admin_limit_workflow()
    assert len(workflow.signatures) == ADMIN_WORKFLOW_GRAPH_MAX_NODES + 1 == 101
    assert workflow.run(42, use_ray=False) == 42
    plan = materialize_workflow_plan(workflow, invocation_args=(42,)).plan
    assert plan.summary()["node_count"] == 101
    manifest = plan.as_dict()
    assert manifest["snapshot"]["state"] == "overflow"
    assert manifest["snapshot"]["observed_edge_count"] == 100


@pytest.mark.parametrize("value", [True, 41, "42", [42], None])
def test_admin_limit_workload_rejects_other_inputs(value):
    with pytest.raises(ValueError, match="fixed scalar"):
        admin_limit_identity(value)
