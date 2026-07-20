"""Execution context shared by durable tasks and nested workflows."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

WORKFLOW_PROGRESS_SCHEMA_VERSION = 2
WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DurableTaskContext:
    """Identity inherited by workflows running inside one durable task."""

    task_pk: int
    attempt_number: int | None = None
    execution_generation: int | None = None
    runtime_env_profile: str | None = None
    runtime_env_hash: str = ""
    runtime_env_plan_identity: dict[str, Any] | None = None
    ray_job_driver: bool = False
    compiled_graph_submission_transport: str | None = None


@dataclass(frozen=True)
class WorkflowRunIdentity:
    """Immutable identity for one workflow invocation in a durable task run."""

    task_execution_pk: int
    attempt_number: int
    execution_generation: int
    run_id: str

    @classmethod
    def create(cls, task_context: DurableTaskContext) -> WorkflowRunIdentity | None:
        """Create an invocation identity when the durable context is fenceable."""
        if task_context.attempt_number is None or task_context.execution_generation is None:
            return None
        return cls(
            task_execution_pk=task_context.task_pk,
            attempt_number=task_context.attempt_number,
            execution_generation=task_context.execution_generation,
            run_id=str(uuid4()),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the versioned JSON representation stored with snapshots."""
        return {
            "schema_version": WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_execution_pk": self.task_execution_pk,
            "attempt_number": self.attempt_number,
            "execution_generation": self.execution_generation,
        }


@dataclass(frozen=True)
class WorkflowStepContext:
    """Progress channel available to a running workflow leaf."""

    progress_actor: Any | None
    node_id: str
    run_identity: dict[str, Any] | None = None


_current_task: ContextVar[DurableTaskContext | None] = ContextVar(
    "django_ray_durable_task",
    default=None,
)
_current_workflow_step: ContextVar[WorkflowStepContext | None] = ContextVar(
    "django_ray_workflow_step",
    default=None,
)


def get_current_task_execution_pk() -> int | None:
    """Return the durable task primary key for the current Ray execution."""
    context = _current_task.get()
    return context.task_pk if context is not None else None


def get_current_task_context() -> DurableTaskContext | None:
    """Return metadata for the current durable task."""
    return _current_task.get()


def get_current_workflow_run_identity() -> dict[str, Any] | None:
    """Return a detached workflow-run identity inside a running leaf."""
    context = _current_workflow_step.get()
    if context is None or context.run_identity is None:
        return None
    return dict(context.run_identity)


@contextmanager
def durable_task_execution(
    task_pk: int,
    *,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
    runtime_env_profile: str | None = None,
    runtime_env_hash: str = "",
    runtime_env_plan_identity: dict[str, Any] | None = None,
    ray_job_driver: bool = False,
    compiled_graph_submission_transport: str | None = None,
) -> Iterator[None]:
    """Expose a durable task identity to nested workflow coordination."""
    token = _current_task.set(
        DurableTaskContext(
            task_pk=task_pk,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
            runtime_env_profile=runtime_env_profile,
            runtime_env_hash=runtime_env_hash,
            runtime_env_plan_identity=(
                json.loads(json.dumps(runtime_env_plan_identity))
                if runtime_env_plan_identity is not None
                else None
            ),
            ray_job_driver=ray_job_driver,
            compiled_graph_submission_transport=compiled_graph_submission_transport,
        )
    )
    try:
        yield
    finally:
        _current_task.reset(token)


@contextmanager
def workflow_step_execution(
    progress_actor: Any | None,
    node_id: str,
    run_identity: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Expose the progress actor to code running inside one workflow step."""
    if progress_actor is None and run_identity is None:
        yield
        return

    token = _current_workflow_step.set(
        WorkflowStepContext(
            progress_actor=progress_actor,
            node_id=node_id,
            run_identity=run_identity,
        )
    )
    try:
        yield
    finally:
        _current_workflow_step.reset(token)


def report_workflow_progress(
    current: int | float,
    total: int | float,
    *,
    message: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> bool:
    """Report application-level progress from inside a workflow step."""
    context = _current_workflow_step.get()
    if context is None or context.progress_actor is None:
        return False
    if total <= 0:
        raise ValueError("total must be greater than zero")
    if current < 0 or current > total:
        raise ValueError("current must be between zero and total")
    try:
        safe_metrics = json.loads(json.dumps({} if metrics is None else metrics))
    except (TypeError, ValueError) as error:
        raise ValueError("progress metrics must be JSON-serializable") from error

    context.progress_actor.progress.remote(
        context.node_id,
        float(current),
        float(total),
        message,
        safe_metrics,
    )
    return True
