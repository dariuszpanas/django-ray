"""Race-safe identity and persistence for durable workflow progress."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Case, F, Func, IntegerField, Q, TextField, Value, When
from django.utils import timezone

from django_ray.models import RayTaskExecution, TaskState, WorkflowProgressRunStorage
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
    effective_plan_selection_reporting_policy,
    validate_plan_selection_manifest,
)
from django_ray.workflow_progress_summary import (
    WORKFLOW_PROGRESS_LEGACY_MAX_BYTES,
    WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES,
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    WorkflowProgressSummaryError,
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)

MAX_PLAN_SELECTION_BYTES = 16 * 1024
_MAX_RUN_IDENTITY_INTEGER = (1 << 63) - 1
_MAX_SUMMARY_REVISION = (1 << 63) - 1
_RUN_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "task_execution_pk",
        "attempt_number",
        "execution_generation",
    }
)


class WorkflowProgressSummaryConflictError(RuntimeError):
    """Raised when a valid summary conflicts with already accepted state."""


class WorkflowProgressReadSource(StrEnum):
    """Durable field selected by the rolling compatibility reader."""

    NONE = "none"
    SUMMARY = "summary"
    LEGACY = "legacy"


class WorkflowProgressDiagnosticCode(StrEnum):
    """Stable bounded reasons why durable progress could not be decoded."""

    SUMMARY_OVERSIZED = "SUMMARY_OVERSIZED"
    LEGACY_OVERSIZED = "LEGACY_OVERSIZED"
    MALFORMED_JSON = "MALFORMED_JSON"
    INVALID_SHAPE = "INVALID_SHAPE"
    INVALID_VERSION = "INVALID_VERSION"
    UNKNOWN_VERSION = "UNKNOWN_VERSION"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    SUMMARY_INVALID = "SUMMARY_INVALID"
    ROW_MISSING = "ROW_MISSING"


@dataclass(frozen=True)
class WorkflowProgressReadResult:
    """One bounded compatibility-read outcome without untrusted diagnostics."""

    source: WorkflowProgressReadSource
    payload: dict[str, Any] | None = None
    schema_version: int | None = None
    diagnostic_code: WorkflowProgressDiagnosticCode | None = None
    diagnostic_message: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the selected field decoded successfully or was absent."""
        return self.diagnostic_code is None


@dataclass(frozen=True)
class _BoundedProgressFields:
    row_exists: bool
    summary_present: bool
    summary_bytes: int | None
    summary_text: str | None
    legacy_present: bool
    legacy_bytes: int | None
    legacy_text: str | None
    task_execution_pk: int | None
    task_id: str
    attempt_number: int
    execution_generation: int
    workflow_run_id: str | None


class _OctetLength(Func):
    function = "OCTET_LENGTH"
    output_field = IntegerField()

    def as_sqlite(self, compiler, connection, **extra_context):
        return self.as_sql(
            compiler,
            connection,
            template="LENGTH(CAST(%(expressions)s AS BLOB))",
            **extra_context,
        )

    def as_oracle(self, compiler, connection, **extra_context):
        return self.as_sql(
            compiler,
            connection,
            function="LENGTHB",
            **extra_context,
        )


def _diagnostic(
    source: WorkflowProgressReadSource,
    code: WorkflowProgressDiagnosticCode,
    message: str,
    *,
    schema_version: int | None = None,
) -> WorkflowProgressReadResult:
    return WorkflowProgressReadResult(
        source=source,
        schema_version=schema_version,
        diagnostic_code=code,
        diagnostic_message=message,
    )


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
        previous_run_id = execution.workflow_run_id
        if previous_run_id is not None and str(previous_run_id) != identity.run_id:
            WorkflowProgressRunStorage.objects.filter(
                execution=execution,
                attempt_number=execution.attempt_number,
                execution_generation=execution.execution_generation,
                run_id=previous_run_id,
            ).delete()
        update_fields = [
            "workflow_run_id",
            "progress_data",
            "workflow_progress_summary_json",
        ]
        execution.workflow_run_id = identity.run_id
        execution.progress_data = None
        execution.workflow_progress_summary_json = None
        if plan is not None and selection is not None:
            update_fields.extend(_pin_plan_fields(execution, plan, selection))
        execution.last_heartbeat_at = timezone.now()
        update_fields.append("last_heartbeat_at")
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
        execution.last_heartbeat_at = timezone.now()
        update_fields.append("last_heartbeat_at")
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


def refresh_workflow_run_activity(identity: WorkflowRunIdentity) -> bool:
    """Refresh activity only while the exact claimed run still owns execution."""
    updated = RayTaskExecution.objects.filter(
        pk=identity.task_execution_pk,
        state=TaskState.RUNNING,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        workflow_run_id=identity.run_id,
    ).update(last_heartbeat_at=timezone.now())
    return updated == 1


def _bounded_progress_fields(execution: Any) -> _BoundedProgressFields:
    """Read the preferred progress field without returning oversized text."""
    if isinstance(execution, RayTaskExecution) and execution.pk is not None:
        database = execution._state.db or "default"
        rows = (
            RayTaskExecution.objects.using(database)
            .filter(pk=execution.pk)
            .annotate(
                _summary_bytes=_OctetLength("workflow_progress_summary_json"),
                _legacy_bytes=Case(
                    When(
                        workflow_progress_summary_json__isnull=True,
                        then=_OctetLength("progress_data"),
                    ),
                    default=Value(None),
                    output_field=IntegerField(),
                ),
            )
            .annotate(
                _summary_text=Case(
                    When(
                        _summary_bytes__lte=WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES,
                        then=F("workflow_progress_summary_json"),
                    ),
                    default=Value(None),
                    output_field=TextField(),
                ),
                _legacy_text=Case(
                    When(
                        Q(workflow_progress_summary_json__isnull=True)
                        & Q(_legacy_bytes__lte=WORKFLOW_PROGRESS_LEGACY_MAX_BYTES),
                        then=F("progress_data"),
                    ),
                    default=Value(None),
                    output_field=TextField(),
                ),
            )
            .values(
                "pk",
                "task_id",
                "attempt_number",
                "execution_generation",
                "workflow_run_id",
                "_summary_bytes",
                "_summary_text",
                "_legacy_bytes",
                "_legacy_text",
            )
        )
        row = rows.first()
        if row is None:
            cached = execution.__dict__
            return _BoundedProgressFields(
                row_exists=False,
                summary_present=False,
                summary_bytes=None,
                summary_text=None,
                legacy_present=False,
                legacy_bytes=None,
                legacy_text=None,
                task_execution_pk=execution.pk,
                task_id=str(cached.get("task_id", "unknown"))[:255],
                attempt_number=int(cached.get("attempt_number", 0)),
                execution_generation=int(cached.get("execution_generation", 0)),
                workflow_run_id=None,
            )
        return _BoundedProgressFields(
            row_exists=True,
            summary_present=row["_summary_bytes"] is not None,
            summary_bytes=row["_summary_bytes"],
            summary_text=row["_summary_text"],
            legacy_present=(row["_legacy_bytes"] is not None and row["_legacy_bytes"] > 0),
            legacy_bytes=row["_legacy_bytes"],
            legacy_text=row["_legacy_text"],
            task_execution_pk=row["pk"],
            task_id=str(row["task_id"])[:255],
            attempt_number=row["attempt_number"],
            execution_generation=row["execution_generation"],
            workflow_run_id=(
                str(row["workflow_run_id"]) if row["workflow_run_id"] is not None else None
            ),
        )

    summary = getattr(execution, "workflow_progress_summary_json", None)
    legacy = getattr(execution, "progress_data", None)
    workflow_run_id = getattr(execution, "workflow_run_id", None)
    summary_bytes = len(summary.encode("utf-8")) if isinstance(summary, str) else None
    legacy_bytes = (
        len(legacy.encode("utf-8")) if isinstance(legacy, str) and summary is None else None
    )
    return _BoundedProgressFields(
        row_exists=True,
        summary_present=summary is not None,
        summary_bytes=summary_bytes,
        summary_text=(
            summary
            if summary_bytes is not None and summary_bytes <= WORKFLOW_PROGRESS_SUMMARY_MAX_BYTES
            else None
        ),
        legacy_present=legacy_bytes is not None and legacy_bytes > 0,
        legacy_bytes=legacy_bytes,
        legacy_text=(
            legacy
            if legacy_bytes is not None and legacy_bytes <= WORKFLOW_PROGRESS_LEGACY_MAX_BYTES
            else None
        ),
        task_execution_pk=getattr(execution, "pk", None),
        task_id=str(getattr(execution, "task_id", "unknown"))[:255],
        attempt_number=int(getattr(execution, "attempt_number", 0)),
        execution_generation=int(getattr(execution, "execution_generation", 0)),
        workflow_run_id=(str(workflow_run_id) if workflow_run_id is not None else None),
    )


def _expected_identity(fields: _BoundedProgressFields) -> WorkflowRunIdentity | None:
    if fields.task_execution_pk is None or fields.workflow_run_id is None:
        return None
    return WorkflowRunIdentity(
        task_execution_pk=fields.task_execution_pk,
        attempt_number=fields.attempt_number,
        execution_generation=fields.execution_generation,
        run_id=fields.workflow_run_id,
    )


def _legacy_v2_identity_matches(
    value: Any,
    expected_identity: WorkflowRunIdentity | None,
) -> bool:
    if (
        expected_identity is None
        or not isinstance(value, dict)
        or set(value) != _RUN_IDENTITY_KEYS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION
    ):
        return False
    for field, positive in (
        ("task_execution_pk", True),
        ("attempt_number", True),
        ("execution_generation", False),
    ):
        item = value[field]
        minimum = 1 if positive else 0
        if type(item) is not int or not minimum <= item <= _MAX_RUN_IDENTITY_INTEGER:
            return False
    run_id = value["run_id"]
    if not isinstance(run_id, str):
        return False
    try:
        if str(UUID(run_id)) != run_id:
            return False
    except (AttributeError, ValueError):
        return False
    return value == expected_identity.as_dict()


def read_workflow_progress(execution: Any) -> WorkflowProgressReadResult:
    """Read schema v1/v2/v3 progress through bounded database expressions."""
    fields = _bounded_progress_fields(execution)
    expected_identity = _expected_identity(fields)
    if not fields.row_exists:
        return _diagnostic(
            WorkflowProgressReadSource.NONE,
            WorkflowProgressDiagnosticCode.ROW_MISSING,
            "workflow task row no longer exists",
        )

    if fields.summary_present:
        if fields.summary_text is None:
            return _diagnostic(
                WorkflowProgressReadSource.SUMMARY,
                WorkflowProgressDiagnosticCode.SUMMARY_OVERSIZED,
                "workflow progress summary exceeds its 16 KiB limit",
                schema_version=WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
            )
        if expected_identity is None:
            return _diagnostic(
                WorkflowProgressReadSource.SUMMARY,
                WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH,
                "workflow progress summary belongs to another run",
                schema_version=WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
            )
        try:
            summary = deserialize_workflow_progress_summary(fields.summary_text)
            if serialize_workflow_progress_summary(summary) != fields.summary_text:
                raise WorkflowProgressSummaryError("workflow progress summary is not canonical")
        except WorkflowProgressSummaryError:
            return _diagnostic(
                WorkflowProgressReadSource.SUMMARY,
                WorkflowProgressDiagnosticCode.SUMMARY_INVALID,
                "workflow progress summary failed bounded schema validation",
                schema_version=WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
            )
        if summary["run_identity"] != expected_identity.as_dict():
            return _diagnostic(
                WorkflowProgressReadSource.SUMMARY,
                WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH,
                "workflow progress summary belongs to another run",
                schema_version=WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
            )
        return WorkflowProgressReadResult(
            source=WorkflowProgressReadSource.SUMMARY,
            payload=summary,
            schema_version=WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
        )

    if not fields.legacy_present:
        return WorkflowProgressReadResult(source=WorkflowProgressReadSource.NONE)
    if fields.legacy_text is None:
        return _diagnostic(
            WorkflowProgressReadSource.LEGACY,
            WorkflowProgressDiagnosticCode.LEGACY_OVERSIZED,
            "legacy workflow progress exceeds its 64 MiB compatibility limit",
        )
    try:
        progress = json.loads(fields.legacy_text)
    except (ValueError, RecursionError):
        return _diagnostic(
            WorkflowProgressReadSource.LEGACY,
            WorkflowProgressDiagnosticCode.MALFORMED_JSON,
            "contains invalid workflow progress JSON",
        )
    if not isinstance(progress, dict):
        return _diagnostic(
            WorkflowProgressReadSource.LEGACY,
            WorkflowProgressDiagnosticCode.INVALID_SHAPE,
            "workflow progress must be a JSON object",
        )
    schema_version = progress.get("schema_version", 1)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        return _diagnostic(
            WorkflowProgressReadSource.LEGACY,
            WorkflowProgressDiagnosticCode.INVALID_VERSION,
            "workflow progress has an invalid schema version",
        )
    if schema_version not in {1, WORKFLOW_PROGRESS_SCHEMA_VERSION}:
        return _diagnostic(
            WorkflowProgressReadSource.LEGACY,
            WorkflowProgressDiagnosticCode.UNKNOWN_VERSION,
            "workflow progress has an unsupported schema version",
            schema_version=schema_version,
        )
    if schema_version == WORKFLOW_PROGRESS_SCHEMA_VERSION:
        identity = progress.get("run_identity")
        if not isinstance(identity, dict):
            return _diagnostic(
                WorkflowProgressReadSource.LEGACY,
                WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH,
                "workflow progress must contain a run identity",
                schema_version=schema_version,
            )
        if not _legacy_v2_identity_matches(identity, expected_identity):
            return _diagnostic(
                WorkflowProgressReadSource.LEGACY,
                WorkflowProgressDiagnosticCode.IDENTITY_MISMATCH,
                "workflow progress belongs to another run",
                schema_version=schema_version,
            )
    return WorkflowProgressReadResult(
        source=WorkflowProgressReadSource.LEGACY,
        payload=progress,
        schema_version=schema_version,
    )


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


def _validate_summary_plan_binding(
    execution: RayTaskExecution,
    summary: dict[str, Any],
) -> None:
    if summary["plan_fingerprint"] != (execution.workflow_plan_fingerprint or None):
        raise WorkflowProgressSummaryConflictError(
            "workflow progress summary does not match the pinned plan fingerprint"
        )
    serialized_selection = execution.workflow_plan_selection
    if serialized_selection is None:
        selected_strategy = None
        reporting_policy = None
    else:
        try:
            selection = validate_plan_selection_manifest(json.loads(serialized_selection))
            reporting_policy = effective_plan_selection_reporting_policy(selection)
        except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
            raise WorkflowProgressSummaryConflictError(
                "pinned workflow strategy selection is invalid"
            ) from error
        selected_strategy = selection["selected_strategy"]
    if summary["selected_strategy"] != selected_strategy:
        raise WorkflowProgressSummaryConflictError(
            "workflow progress summary does not match the pinned execution strategy"
        )
    if reporting_policy is not None and summary["reporting_policy"] != reporting_policy:
        raise WorkflowProgressSummaryConflictError(
            "workflow progress summary does not match the pinned reporting policy"
        )


def _validate_revision_advance(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> None:
    previous_summary_revision = previous["summary_revision"]
    current_summary_revision = current["summary_revision"]
    if current_summary_revision <= previous_summary_revision:
        raise WorkflowProgressSummaryConflictError(
            "workflow progress summary revision did not advance monotonically"
        )
    for field in ("topology_version", "detail_revision"):
        old_revision = previous[field]
        new_revision = current[field]
        if old_revision is not None and (new_revision is None or new_revision < old_revision):
            raise WorkflowProgressSummaryConflictError(f"workflow progress {field} regressed")
    if previous["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}:
        raise WorkflowProgressSummaryConflictError(
            "terminal workflow progress summary cannot advance"
        )
    if current["timestamps"]["started_at"] != previous["timestamps"]["started_at"]:
        raise WorkflowProgressSummaryConflictError("workflow progress start timestamp changed")
    previous_updated_at = datetime.fromisoformat(
        previous["timestamps"]["updated_at"][:-1] + "+00:00"
    )
    current_updated_at = datetime.fromisoformat(current["timestamps"]["updated_at"][:-1] + "+00:00")
    if current_updated_at < previous_updated_at:
        raise WorkflowProgressSummaryConflictError("workflow progress update timestamp regressed")
    if current["topology_version"] == previous["topology_version"] and (
        current["storage"]["manifest_id"] != previous["storage"]["manifest_id"]
    ):
        raise WorkflowProgressSummaryConflictError(
            "workflow progress manifest changed without a topology revision"
        )


def _assign_workflow_progress_summary_locked(
    execution: RayTaskExecution,
    identity: WorkflowRunIdentity,
    serialized_summary: str,
) -> bool:
    """Assign a validated summary to an already locked exact-run row.

    The caller must hold a ``select_for_update()`` lock inside a database
    transaction. This helper exists so #126 can publish detail and its summary
    pointer in the same transaction without duplicating revision rules.
    """
    if (
        execution.pk != identity.task_execution_pk
        or execution.state != TaskState.RUNNING
        or execution.attempt_number != identity.attempt_number
        or execution.execution_generation != identity.execution_generation
        or str(execution.workflow_run_id) != identity.run_id
    ):
        return False
    try:
        summary = deserialize_workflow_progress_summary(
            serialized_summary,
            expected_identity=identity,
        )
    except WorkflowProgressSummaryError as error:
        raise WorkflowProgressSummaryConflictError(
            "workflow progress summary failed bounded schema validation"
        ) from error
    canonical_summary = serialize_workflow_progress_summary(summary, expected_identity=identity)
    if canonical_summary != serialized_summary:
        raise WorkflowProgressSummaryConflictError(
            "workflow progress summary must use canonical JSON encoding"
        )
    if summary["summary_revision"] == _MAX_SUMMARY_REVISION:
        raise WorkflowProgressSummaryConflictError(
            "workflow progress summary must reserve the lifecycle terminal revision"
        )
    _validate_summary_plan_binding(execution, summary)

    activity_at = timezone.now()
    previous_serialized = execution.workflow_progress_summary_json
    if previous_serialized == serialized_summary:
        execution.last_heartbeat_at = activity_at
        execution.save(update_fields=["last_heartbeat_at"])
        return True
    if previous_serialized is not None:
        try:
            previous = deserialize_workflow_progress_summary(
                previous_serialized,
                expected_identity=identity,
            )
        except WorkflowProgressSummaryError as error:
            raise WorkflowProgressSummaryConflictError(
                "accepted workflow progress summary is corrupt"
            ) from error
        if (
            serialize_workflow_progress_summary(previous, expected_identity=identity)
            != previous_serialized
        ):
            raise WorkflowProgressSummaryConflictError(
                "accepted workflow progress summary is not canonical"
            )
        _validate_revision_advance(previous, summary)

    execution.workflow_progress_summary_json = serialized_summary
    execution.last_heartbeat_at = activity_at
    execution.save(update_fields=["workflow_progress_summary_json", "last_heartbeat_at"])
    return True


def persist_workflow_progress_summary(
    identity: WorkflowRunIdentity,
    summary: dict[str, Any],
) -> bool:
    """Publish one summary-only record under the complete stale-writer fence.

    Topology or detail pointers may only be assigned by the package-owned locked
    primitive as part of #126's atomic detail publication transaction.
    """
    serialized = serialize_workflow_progress_summary(summary, expected_identity=identity)
    normalized = deserialize_workflow_progress_summary(serialized, expected_identity=identity)
    if (
        normalized["topology_version"] is not None
        or normalized["detail_revision"] is not None
        or normalized["storage"]["manifest_id"] is not None
    ):
        raise WorkflowProgressSummaryConflictError(
            "workflow detail pointers require an atomic storage publication"
        )
    with transaction.atomic():
        execution = (
            RayTaskExecution.objects.select_for_update()
            .only(
                "pk",
                "state",
                "attempt_number",
                "execution_generation",
                "workflow_run_id",
                "workflow_plan_fingerprint",
                "workflow_plan_selection",
                "workflow_progress_summary_json",
            )
            .filter(
                pk=identity.task_execution_pk,
                state=TaskState.RUNNING,
                attempt_number=identity.attempt_number,
                execution_generation=identity.execution_generation,
                workflow_run_id=identity.run_id,
            )
            .first()
        )
        if execution is None:
            return False
        return _assign_workflow_progress_summary_locked(execution, identity, serialized)


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
    ).update(
        progress_data=json.dumps(snapshot),
        last_heartbeat_at=timezone.now(),
    )
    return updated == 1


__all__ = [
    "WORKFLOW_PROGRESS_SCHEMA_VERSION",
    "WORKFLOW_RUN_IDENTITY_SCHEMA_VERSION",
    "WorkflowRunIdentity",
    "WorkflowProgressDiagnosticCode",
    "WorkflowProgressReadResult",
    "WorkflowProgressReadSource",
    "WorkflowProgressSummaryConflictError",
    "claim_workflow_run",
    "pin_workflow_plan",
    "refresh_workflow_run_activity",
    "persist_workflow_progress",
    "persist_workflow_progress_summary",
    "read_workflow_progress",
    "workflow_run_is_current",
]
