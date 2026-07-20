"""Race-safe identity and persistence for durable workflow progress."""

from __future__ import annotations

import json
from typing import Any

from django.db import transaction

from django_ray.models import RayTaskExecution, TaskState
from django_ray.runtime.context import (
    WORKFLOW_PROGRESS_SCHEMA_VERSION,
    WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION,
    WorkflowRunIdentity,
)
from django_ray.workflow_plans import (
    MAX_PLAN_BYTES,
    EffectiveWorkflowPlan,
    PlanSelection,
    WorkflowPlanMismatchError,
    validate_plan_selection_manifest,
)

MAX_PLAN_SELECTION_BYTES = 16 * 1024


def claim_workflow_run(
    identity: WorkflowRunIdentity,
    *,
    plan: EffectiveWorkflowPlan | None = None,
    selection: PlanSelection | None = None,
) -> bool:
    """Claim current progress ownership for one running workflow invocation."""
    if (plan is None) != (selection is None):
        raise ValueError("workflow plan and selection must be supplied together")
    with transaction.atomic():
        execution = (
            RayTaskExecution.objects.select_for_update()
            .filter(
                pk=identity.task_execution_pk,
                state=TaskState.RUNNING,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
            )
            .first()
        )
        if execution is None:
            return False
        update_fields = ["workflow_run_id", "progress_data"]
        execution.workflow_run_id = identity.run_id
        execution.progress_data = None
        if plan is not None and selection is not None:
            update_fields.extend(_pin_plan_fields(execution, plan, selection))
        execution.save(update_fields=list(dict.fromkeys(update_fields)))
        return True


def pin_workflow_plan(
    task_context: Any,
    plan: EffectiveWorkflowPlan,
    selection: PlanSelection,
) -> bool:
    """Pin or verify one plan without requiring node-level progress reporting."""
    if task_context.attempt_number is None or task_context.execution_generation is None:
        return False
    with transaction.atomic():
        execution = (
            RayTaskExecution.objects.select_for_update()
            .filter(
                pk=task_context.task_pk,
                state=TaskState.RUNNING,
                attempt_number=task_context.attempt_number,
                execution_generation=task_context.execution_generation,
            )
            .first()
        )
        if execution is None:
            return False
        update_fields = _pin_plan_fields(execution, plan, selection)
        if update_fields:
            execution.save(update_fields=list(dict.fromkeys(update_fields)))
        return True


def workflow_run_is_current(identity: WorkflowRunIdentity) -> bool:
    """Return whether an exact claimed run may still submit workflow leaves."""
    return RayTaskExecution.objects.filter(
        pk=identity.task_execution_pk,
        state=TaskState.RUNNING,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        workflow_run_id=identity.run_id,
    ).exists()


def _pin_plan_fields(
    execution: RayTaskExecution,
    plan: EffectiveWorkflowPlan,
    selection: PlanSelection,
) -> list[str]:
    if len(plan.canonical_json.encode("utf-8")) > MAX_PLAN_BYTES:
        raise ValueError("workflow plan exceeds persistence limit")
    selection_manifest = validate_plan_selection_manifest(selection.as_dict())
    selection_json = json.dumps(
        selection_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(selection_json.encode("utf-8")) > MAX_PLAN_SELECTION_BYTES:
        raise ValueError("workflow plan selection exceeds persistence limit")
    pinned = execution.workflow_plan_fingerprint
    if pinned and pinned != plan.fingerprint:
        raise WorkflowPlanMismatchError(
            "Workflow retry materialized a different effective plan: "
            f"pinned={pinned}, current={plan.fingerprint}. Enqueue changed work as a new task."
        )
    if pinned and execution.workflow_plan_json != plan.canonical_json:
        raise WorkflowPlanMismatchError(
            "Pinned workflow plan manifest does not match its effective plan identity"
        )
    update_fields: list[str] = []
    if not pinned:
        execution.workflow_plan_fingerprint = plan.fingerprint
        execution.workflow_plan_json = plan.canonical_json
        execution.workflow_plan_pinned_attempt = execution.attempt_number
        update_fields.extend(
            [
                "workflow_plan_fingerprint",
                "workflow_plan_json",
                "workflow_plan_pinned_attempt",
            ]
        )
    else:
        pinned_attempt = execution.workflow_plan_pinned_attempt
        current_attempt = execution.attempt_number
        if pinned_attempt is None:
            if not plan.retry_safe:
                raise WorkflowPlanMismatchError(_retry_unsafe_plan_message(plan))
            execution.workflow_plan_pinned_attempt = current_attempt
            update_fields.append("workflow_plan_pinned_attempt")
        elif pinned_attempt != current_attempt and not plan.retry_safe:
            raise WorkflowPlanMismatchError(_retry_unsafe_plan_message(plan))
    execution.workflow_plan_selection = selection_json
    update_fields.append("workflow_plan_selection")
    return update_fields


def _retry_unsafe_plan_message(plan: EffectiveWorkflowPlan) -> str:
    paths = [path[:160] for path in plan.retry_unsafe_paths[:5]]
    detail = ", ".join(paths) if paths else "retry_safety"
    retry_safety = plan.manifest.get("retry_safety", {})
    total = retry_safety.get("total_retry_unsafe_paths", len(paths))
    if isinstance(total, int) and not isinstance(total, bool) and total > len(paths):
        detail += f", and {total - len(paths)} more"
    return (
        "Workflow retry cannot verify runtime environment bindings represented only "
        f"by secret-free runtime metadata (retry-unsafe paths: {detail}). Declare the "
        "appropriate non-secret environment or credential revision, use immutable "
        "content-addressed inputs, or enqueue the work as a new task."
    )


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
    "pin_workflow_plan",
    "persist_workflow_progress",
    "workflow_run_is_current",
]
