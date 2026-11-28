"""Local Ray integration tests for module-level remote task and actor execution."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from django_ray.runtime.remote import WorkflowProgressActor, execute_workflow_step_remote
from django_ray.workflow.progress.protocol import (
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressEventKind,
)
from tests.unit._remote_helpers import (
    _STRICT_IDENTITY,
    _WORKFLOW_PLAN,
    _progress_wire,
    _strict_nested_workflow_kwargs,
    _strict_nested_workflow_request,
)
from tests.unit.test_remote import strict_workflow_full_context_target


@pytest.mark.real_ray
def test_real_ray_strict_workflow_step_round_trip_has_full_context(
    ray_cluster: Any,
) -> None:
    callable_path = (
        f"{strict_workflow_full_context_target.__module__}.strict_workflow_full_context_target"
    )
    workflow_run_id = "00000000-0000-4000-8000-000000000514"
    node_id = "0.strict-real-ray"
    serialized, runtime_identity = _strict_nested_workflow_request(
        callable_path=callable_path,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
    )
    expected_context = {
        "task_pk": _STRICT_IDENTITY.task_execution_pk,
        "attempt_number": _STRICT_IDENTITY.attempt_number,
        "execution_generation": _STRICT_IDENTITY.execution_generation,
        "runtime_env_profile": None,
        "runtime_env_hash": "",
        "runtime_env_plan_identity": runtime_identity,
        "ray_job_driver": False,
        "compiled_graph_submission_transport": "direct-ray-core",
        "task_id": _STRICT_IDENTITY.task_id,
        "execution_protocol_version": 1,
        "strict_execution_request": True,
        "read_only_replay": False,
    }
    run_identity = {
        "schema_version": 1,
        "run_id": workflow_run_id,
        "task_execution_pk": _STRICT_IDENTITY.task_execution_pk,
        "attempt_number": _STRICT_IDENTITY.attempt_number,
        "execution_generation": _STRICT_IDENTITY.execution_generation,
    }
    remote = ray_cluster.remote(num_cpus=0.25)(execute_workflow_step_remote)

    result = ray_cluster.get(
        remote.remote(
            callable_path,
            False,
            (expected_context,),
            {},
            {},
            _STRICT_IDENTITY.task_execution_pk,
            None,
            node_id,
            workflow_run_identity=run_identity,
            allowed_callable_modules=("tests",),
            **_strict_nested_workflow_kwargs(
                serialized,
                runtime_identity,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            ),
        )
    )

    assert result == "strict-context-ready"


@pytest.mark.real_ray
@pytest.mark.parametrize(
    "terminal_kind", [WorkflowProgressEventKind.FAILED, WorkflowProgressEventKind.COMPLETED]
)
def test_real_ray_progress_pressure_preserves_terminal_state(ray_cluster, terminal_kind) -> None:
    limits = replace(WORKFLOW_PROGRESS_LIMITS_V1, combined_max_decoded_bytes=1400)
    actor = ray_cluster.remote(num_cpus=0.25)(WorkflowProgressActor).remote(
        _progress_wire(WorkflowProgressEventKind.INITIALIZED, {"plan": _WORKFLOW_PLAN}),
        limits=limits,
    )
    events = [
        (
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "leaf",
                "label": "leaf",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        ),
        (
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            {"node_id": "leaf", "current": 1.0, "total": 2.0, "message": "x" * 400, "metrics": {}},
        ),
        (
            terminal_kind,
            {"node_id": "leaf", "label": "leaf"}
            | (
                {"error": "fixture failure"}
                if terminal_kind is WorkflowProgressEventKind.FAILED
                else {}
            ),
        ),
    ]
    try:
        for kind, payload in events:
            assert ray_cluster.get(actor.ingest.remote(_progress_wire(kind, payload)), timeout=20)
        snapshot = ray_cluster.get(actor.snapshot.remote(), timeout=20)
        assert snapshot["graph"]["nodes"][0]["state"] == (
            "FAILED" if terminal_kind is WorkflowProgressEventKind.FAILED else "SUCCEEDED"
        )
        assert snapshot["ingress"]["rejected"] == 0
        assert snapshot["ingress"]["retained_bytes"] <= limits.combined_max_decoded_bytes
        assert snapshot["ingress"]["replaceable"]["evicted_nodes"] == 1
    finally:
        ray_cluster.kill(actor, no_restart=True)
