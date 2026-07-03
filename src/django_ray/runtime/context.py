"""Execution context shared by durable tasks and nested workflows."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DurableTaskContext:
    """Identity inherited by workflows running inside one durable task."""

    task_pk: int
    runtime_env_profile: str | None = None
    runtime_env_hash: str = ""
    ray_job_driver: bool = False


@dataclass(frozen=True)
class WorkflowStepContext:
    """Progress channel available to a running workflow leaf."""

    progress_actor: Any
    node_id: str


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


@contextmanager
def durable_task_execution(
    task_pk: int,
    *,
    runtime_env_profile: str | None = None,
    runtime_env_hash: str = "",
    ray_job_driver: bool = False,
) -> Iterator[None]:
    """Expose a durable task identity to nested workflow coordination."""
    token = _current_task.set(
        DurableTaskContext(
            task_pk=task_pk,
            runtime_env_profile=runtime_env_profile,
            runtime_env_hash=runtime_env_hash,
            ray_job_driver=ray_job_driver,
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
) -> Iterator[None]:
    """Expose the progress actor to code running inside one workflow step."""
    if progress_actor is None:
        yield
        return

    token = _current_workflow_step.set(
        WorkflowStepContext(progress_actor=progress_actor, node_id=node_id)
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
    if context is None:
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
