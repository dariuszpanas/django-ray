"""Shared helpers and fixtures for module-level Ray task and workflow tests."""

from __future__ import annotations

from datetime import UTC, datetime

from django_ray.execution_codec import (
    ExecutionIdentity,
    ExecutionRequest,
    NestedCallableBindingKind,
    NestedExecutionBoundaryKind,
    NestedExecutionRequest,
    NestedWorkflowBoundaryIdentity,
    encode_nested_execution_request,
)
from django_ray.runtime.remote import (
    WorkflowProgressActor,
    execute_django_task_remote,
    execute_workflow_step_remote,
)
from django_ray.workflow.progress.protocol import (
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressEventKind,
    WorkflowProgressLimits,
    prepare_workflow_progress_event,
)


class _RemoteMethod:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def remote(self, *args) -> None:
        self.calls.append(args)


class _ProgressActor:
    def __init__(self) -> None:
        self.ingest = _RemoteMethod()


_WORKFLOW_RUN_IDENTITY = {
    "schema_version": 1,
    "run_id": "00000000-0000-0000-0000-000000000217",
    "task_execution_pk": 9,
    "attempt_number": 2,
    "execution_generation": 6,
}
_WORKFLOW_PLAN = {
    "plan_format": "django-ray.workflow-plan",
    "plan_format_version": 1,
    "fingerprint": f"sha256:{'a' * 64}",
    "definition_name": "workflow:test",
    "definition_revision": f"sha256:{'b' * 64}",
    "topology_class": "static",
    "node_count": 2,
}
_OCCURRED_AT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
_STRICT_IDENTITY = ExecutionIdentity(
    task_execution_pk=217,
    task_id="00000000-0000-4000-8000-000000000217",
    attempt_number=3,
    execution_generation=6,
)


def _strict_execution_request(
    *,
    identity: ExecutionIdentity = _STRICT_IDENTITY,
) -> ExecutionRequest:
    return ExecutionRequest(
        identity=identity,
        execution_protocol_version=1,
        callable_path="tests.strict_task",
        transport_version=1,
        serialized_args='["value"]',
        serialized_kwargs='{"increment":2}',
        input_reference=None,
        runtime_env_profile="thin",
        runtime_env_hash="a" * 64,
        runtime_env_plan_identity={"profile": "thin", "schema_version": 1},
        compiled_graph_submission_transport="direct-ray-core",
    )


def _execute_strict_request(
    serialized: str,
    *,
    identity: ExecutionIdentity = _STRICT_IDENTITY,
    expected_protocol: int = 1,
) -> str:
    return execute_django_task_remote(
        serialized,
        expected_task_execution_pk=identity.task_execution_pk,
        expected_task_id=identity.task_id,
        expected_attempt_number=identity.attempt_number,
        expected_execution_generation=identity.execution_generation,
        expected_execution_protocol_version=expected_protocol,
    )


def _strict_nested_workflow_request(
    *,
    callable_path: str,
    workflow_run_id: str,
    node_id: str,
    output_preview_path: str | None = None,
    identity: ExecutionIdentity = _STRICT_IDENTITY,
) -> tuple[str, dict[str, object]]:
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.workflow.plans import runtime_env_plan_identity

    runtime_identity = runtime_env_plan_identity(
        normalize_runtime_env({"env_vars": {"NESTED_TEST": "1"}})
    ).as_transport_dict()
    serialized = encode_nested_execution_request(
        NestedExecutionRequest(
            outer_identity=identity,
            execution_protocol_version=1,
            boundary_kind=NestedExecutionBoundaryKind.WORKFLOW_STEP,
            boundary_identity=NestedWorkflowBoundaryIdentity(
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            ),
            callable_binding_kind=NestedCallableBindingKind.PATH,
            callable_binding=callable_path,
            output_preview_callable_path=output_preview_path,
            runtime_env_plan_identity=runtime_identity,
            runtime_env_plan_digest=str(runtime_identity["digest"]),
            runtime_env_transport_digest=str(runtime_identity["transport_digest"]),
        )
    )
    return serialized, runtime_identity


def _strict_nested_workflow_kwargs(
    serialized: str,
    runtime_identity: dict[str, object],
    *,
    workflow_run_id: str,
    node_id: str,
    identity: ExecutionIdentity = _STRICT_IDENTITY,
) -> dict[str, object]:
    return {
        "nested_execution_request": serialized,
        "expected_outer_task_execution_pk": identity.task_execution_pk,
        "expected_outer_task_id": identity.task_id,
        "expected_outer_attempt_number": identity.attempt_number,
        "expected_outer_execution_generation": identity.execution_generation,
        "expected_execution_protocol_version": 1,
        "expected_workflow_run_id": workflow_run_id,
        "expected_node_id": node_id,
        "expected_runtime_env_plan_digest": runtime_identity["digest"],
        "expected_runtime_env_transport_digest": runtime_identity["transport_digest"],
    }


def _execute_bound_workflow_step(*args, **kwargs):
    run = kwargs.get("workflow_run_identity") or {}
    identity = ExecutionIdentity(
        task_execution_pk=args[5],
        task_id=f"workflow-fixture-{args[5]}",
        attempt_number=run.get("attempt_number", 1),
        execution_generation=run.get("execution_generation", 1),
    )
    run_id = run.get("run_id", "00000000-0000-4000-8000-000000000555")
    serialized, runtime = _strict_nested_workflow_request(
        callable_path=args[0],
        workflow_run_id=run_id,
        node_id=args[7],
        output_preview_path=kwargs.get("output_preview_path"),
        identity=identity,
    )
    return execute_workflow_step_remote(
        *args,
        **kwargs,
        **_strict_nested_workflow_kwargs(
            serialized,
            runtime,
            workflow_run_id=run_id,
            node_id=args[7],
            identity=identity,
        ),
    )


def _progress_wire(
    kind: WorkflowProgressEventKind,
    payload: dict[str, object],
    *,
    run_identity: dict[str, object] | None = None,
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
) -> bytes:
    return prepare_workflow_progress_event(
        _WORKFLOW_RUN_IDENTITY if run_identity is None else run_identity,
        kind,
        payload,
        occurred_at=_OCCURRED_AT,
        limits=limits,
    )


def _progress_actor(
    *,
    limits: WorkflowProgressLimits = WORKFLOW_PROGRESS_LIMITS_V1,
) -> WorkflowProgressActor:
    return WorkflowProgressActor(
        _progress_wire(
            WorkflowProgressEventKind.INITIALIZED,
            {"plan": _WORKFLOW_PLAN},
        ),
        limits=limits,
    )


def _producer_report(**overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "saturated": False,
        "offered": 5,
        "submitted": 2,
        "superseded": 2,
        "locally_dropped": 1,
        "acknowledged": 1,
        "actor_rejected": 0,
        "ack_failed": 0,
        "pending_acknowledgements": 1,
        "terminal_handoff": "submitted",
        **overrides,
    }
