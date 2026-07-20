"""Race-safe identity and persistence for durable workflow progress."""

from __future__ import annotations

import json
from typing import Any

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION,
    WorkflowRunIdentity,
)


def claim_workflow_run(identity: WorkflowRunIdentity) -> bool:
    """Claim current progress ownership for one running workflow invocation."""
    updated = RayTaskExecution.objects.filter(
        pk=identity.task_execution_pk,
        state=TaskState.RUNNING,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
    ).update(
        workflow_run_id=identity.run_id,
        progress_data=None,
    )
    return updated == 1


def persist_workflow_progress(
    identity: WorkflowRunIdentity,
    snapshot: dict[str, Any],
) -> bool:
    """Persist a snapshot only while its exact workflow run still owns progress."""
    if snapshot.get("schema_version") != WORKFLOW_PROGRESS_SCHEMA_VERSION:
        raise ValueError("workflow progress snapshot has an unsupported schema version")
    if snapshot.get("run_identity") != identity.as_dict():
        raise ValueError("workflow progress snapshot identity does not match its reporter")

    updated = RayTaskExecution.objects.filter(
        pk=identity.task_execution_pk,
        state=TaskState.RUNNING,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        workflow_run_id=identity.run_id,
    ).update(progress_data=json.dumps(snapshot))
    return updated == 1


__all__ = [
    "WORKFLOW_PROGRESS_SCHEMA_VERSION",
    "WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION",
    "WorkflowRunIdentity",
    "claim_workflow_run",
    "persist_workflow_progress",
]
