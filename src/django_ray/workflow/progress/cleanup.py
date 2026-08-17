"""Retention and orphan cleanup for normalized workflow-progress storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from django_ray.models import (
    RayTaskExecution,
    TaskState,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)

WORKFLOW_PROGRESS_ORPHAN_GRACE_PERIOD = timedelta(hours=1)
WORKFLOW_PROGRESS_CLEANUP_DEFAULT_BATCH_SIZE = 100
WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE = 1000
_CLEANUP_DIAGNOSTIC_MAX_CHARS = 2000
_TERMINAL_STATES = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.LOST,
        TaskState.EXPIRED,
    }
)


class WorkflowProgressCleanupKind(StrEnum):
    """Storage object classifications processed by cleanup."""

    EXPIRED_RUN = "expired_run"
    PENDING_MANIFEST = "pending_manifest"
    ORPHAN_PAGE = "orphan_page"
    EMPTY_RUN = "empty_run"


class WorkflowProgressCleanupOutcome(StrEnum):
    """Result of re-checking one cleanup candidate under row locks."""

    ELIGIBLE = "eligible"
    DELETED = "deleted"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkflowProgressCleanupItem:
    """Bounded, payload-free result for one cleanup candidate."""

    kind: WorkflowProgressCleanupKind
    identifier: str
    outcome: WorkflowProgressCleanupOutcome
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowProgressCleanupReport:
    """Results from one bounded pass over each cleanup candidate class."""

    delete: bool
    pending_cutoff: datetime
    items: tuple[WorkflowProgressCleanupItem, ...]

    @property
    def eligible_count(self) -> int:
        """Return candidates that remained eligible when locked."""
        return sum(
            item.outcome
            in {
                WorkflowProgressCleanupOutcome.ELIGIBLE,
                WorkflowProgressCleanupOutcome.DELETED,
                WorkflowProgressCleanupOutcome.FAILED,
            }
            for item in self.items
        )

    @property
    def deleted_count(self) -> int:
        """Return successfully deleted candidates."""
        return sum(item.outcome is WorkflowProgressCleanupOutcome.DELETED for item in self.items)

    @property
    def failed_count(self) -> int:
        """Return candidates whose isolated cleanup transaction failed."""
        return sum(item.outcome is WorkflowProgressCleanupOutcome.FAILED for item in self.items)

    @property
    def skipped_count(self) -> int:
        """Return candidates that stopped qualifying before their row locks were held."""
        return sum(item.outcome is WorkflowProgressCleanupOutcome.SKIPPED for item in self.items)

    def count(
        self,
        kind: WorkflowProgressCleanupKind,
        outcome: WorkflowProgressCleanupOutcome | None = None,
    ) -> int:
        """Count results by kind and, optionally, outcome."""
        return sum(
            item.kind is kind and (outcome is None or item.outcome is outcome)
            for item in self.items
        )


def _validate_batch_size(batch_size: int) -> int:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ValueError("workflow progress cleanup batch size must be an integer")
    if not 1 <= batch_size <= WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE:
        raise ValueError(
            "workflow progress cleanup batch size must be between 1 and "
            f"{WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE}"
        )
    return batch_size


def _validate_now(now: datetime) -> datetime:
    if not isinstance(now, datetime) or timezone.is_naive(now):
        raise ValueError("workflow progress cleanup time must be timezone-aware")
    return now


def _is_exact_active_current(
    execution: RayTaskExecution,
    run_storage: WorkflowProgressRunStorage,
) -> bool:
    return (
        execution.state not in _TERMINAL_STATES
        and execution.workflow_run_id is not None
        and execution.attempt_number == run_storage.attempt_number
        and execution.execution_generation == run_storage.execution_generation
        and execution.workflow_run_id == run_storage.run_id
    )


def _cleanup_diagnostic(code: str, error: Exception) -> str:
    """Return a bounded diagnostic without retaining an exception message or payload."""
    error_type = re.sub(r"[^A-Za-z0-9_.-]", "_", type(error).__name__)[:128]
    return f"{code}: {error_type}; message=<redacted>"[:_CLEANUP_DIAGNOSTIC_MAX_CHARS]


def _orphan_page_failure_code(page_id: int) -> str:
    return f"ORPHAN_PAGE_DELETE_FAILED[page_id={page_id}]"


def _lock_task(
    execution_id: int,
    *,
    using: str,
) -> RayTaskExecution | None:
    return (
        RayTaskExecution.objects.using(using)
        .select_for_update()
        .only(
            "pk",
            "state",
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
        )
        .filter(pk=execution_id)
        .first()
    )


def _record_run_failure(
    execution_id: int,
    run_storage_id: int,
    diagnostic: str,
    *,
    using: str,
) -> None:
    try:
        with transaction.atomic(using=using):
            execution = _lock_task(execution_id, using=using)
            if execution is None:
                return
            run_storage = (
                WorkflowProgressRunStorage.objects.using(using)
                .select_for_update()
                .only("pk", "execution_id", "cleanup_error")
                .filter(pk=run_storage_id, execution_id=execution.pk)
                .first()
            )
            if run_storage is None:
                return
            run_storage.cleanup_error = diagnostic
            run_storage.save(update_fields=["cleanup_error"])
    except Exception:
        # Cleanup diagnostics are best effort. The original bounded failure remains
        # in the returned report even if the database cannot record it.
        return


def _record_manifest_failure(
    execution_id: int,
    run_storage_id: int,
    manifest_id: str,
    diagnostic: str,
    *,
    using: str,
) -> None:
    try:
        with transaction.atomic(using=using):
            execution = _lock_task(execution_id, using=using)
            if execution is None:
                return
            run_storage = (
                WorkflowProgressRunStorage.objects.using(using)
                .select_for_update()
                .only("pk", "execution_id")
                .filter(pk=run_storage_id, execution_id=execution.pk)
                .first()
            )
            if run_storage is None:
                return
            manifest = (
                WorkflowProgressTopologyManifest.objects.using(using)
                .select_for_update()
                .only("pk", "run_storage_id", "slot", "cleanup_error")
                .filter(
                    pk=manifest_id,
                    run_storage=run_storage,
                    slot=WorkflowProgressTopologySlot.PENDING,
                )
                .first()
            )
            if manifest is None:
                return
            manifest.cleanup_error = diagnostic
            manifest.save(update_fields=["cleanup_error"])
    except Exception:
        return


def _process_expired_run(
    execution_id: int,
    run_storage_id: int,
    *,
    now: datetime,
    delete: bool,
    using: str,
) -> WorkflowProgressCleanupItem:
    identifier = str(run_storage_id)
    try:
        with transaction.atomic(using=using):
            execution = _lock_task(execution_id, using=using)
            if execution is None:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.EXPIRED_RUN,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            run_storage = (
                WorkflowProgressRunStorage.objects.using(using)
                .select_for_update()
                .only(
                    "pk",
                    "execution_id",
                    "attempt_number",
                    "execution_generation",
                    "run_id",
                    "detail_expires_at",
                )
                .filter(pk=run_storage_id, execution_id=execution.pk)
                .first()
            )
            if (
                run_storage is None
                or run_storage.detail_expires_at is None
                or run_storage.detail_expires_at > now
                or _is_exact_active_current(execution, run_storage)
            ):
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.EXPIRED_RUN,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            if not delete:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.EXPIRED_RUN,
                    identifier,
                    WorkflowProgressCleanupOutcome.ELIGIBLE,
                )

            # Lock descendants in the package-wide task -> run -> manifest/page
            # order before the cascading run deletion.
            tuple(
                WorkflowProgressTopologyManifest.objects.using(using)
                .select_for_update()
                .filter(run_storage=run_storage)
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            tuple(
                WorkflowProgressTopologyPage.objects.using(using)
                .select_for_update()
                .filter(run_storage=run_storage)
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            run_storage.delete(using=using)
            return WorkflowProgressCleanupItem(
                WorkflowProgressCleanupKind.EXPIRED_RUN,
                identifier,
                WorkflowProgressCleanupOutcome.DELETED,
            )
    except Exception as error:
        diagnostic = _cleanup_diagnostic("EXPIRED_RUN_DELETE_FAILED", error)
        _record_run_failure(
            execution_id,
            run_storage_id,
            diagnostic,
            using=using,
        )
        return WorkflowProgressCleanupItem(
            WorkflowProgressCleanupKind.EXPIRED_RUN,
            identifier,
            WorkflowProgressCleanupOutcome.FAILED,
            diagnostic,
        )


def _process_pending_manifest(
    execution_id: int,
    run_storage_id: int,
    manifest_id: str,
    *,
    cutoff: datetime,
    delete: bool,
    using: str,
) -> WorkflowProgressCleanupItem:
    identifier = str(manifest_id)
    try:
        with transaction.atomic(using=using):
            execution = _lock_task(execution_id, using=using)
            if execution is None:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.PENDING_MANIFEST,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            run_storage = (
                WorkflowProgressRunStorage.objects.using(using)
                .select_for_update()
                .only("pk", "execution_id")
                .filter(pk=run_storage_id, execution_id=execution.pk)
                .first()
            )
            if run_storage is None:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.PENDING_MANIFEST,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            manifest = (
                WorkflowProgressTopologyManifest.objects.using(using)
                .select_for_update()
                .only("pk", "run_storage_id", "slot", "created_at", "cleanup_error")
                .filter(pk=manifest_id, run_storage=run_storage)
                .first()
            )
            if (
                manifest is None
                or manifest.slot != WorkflowProgressTopologySlot.PENDING
                or manifest.created_at > cutoff
            ):
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.PENDING_MANIFEST,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            if not delete:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.PENDING_MANIFEST,
                    identifier,
                    WorkflowProgressCleanupOutcome.ELIGIBLE,
                )
            manifest.delete(using=using)
            return WorkflowProgressCleanupItem(
                WorkflowProgressCleanupKind.PENDING_MANIFEST,
                identifier,
                WorkflowProgressCleanupOutcome.DELETED,
            )
    except Exception as error:
        diagnostic = _cleanup_diagnostic("PENDING_MANIFEST_DELETE_FAILED", error)
        _record_manifest_failure(
            execution_id,
            run_storage_id,
            manifest_id,
            diagnostic,
            using=using,
        )
        return WorkflowProgressCleanupItem(
            WorkflowProgressCleanupKind.PENDING_MANIFEST,
            identifier,
            WorkflowProgressCleanupOutcome.FAILED,
            diagnostic,
        )


def _process_orphan_page(
    execution_id: int,
    run_storage_id: int,
    page_id: int,
    *,
    cutoff: datetime,
    delete: bool,
    using: str,
) -> WorkflowProgressCleanupItem:
    identifier = str(page_id)
    try:
        with transaction.atomic(using=using):
            execution = _lock_task(execution_id, using=using)
            if execution is None:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.ORPHAN_PAGE,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            run_storage = (
                WorkflowProgressRunStorage.objects.using(using)
                .select_for_update()
                .only("pk", "execution_id", "cleanup_error")
                .filter(pk=run_storage_id, execution_id=execution.pk)
                .first()
            )
            if run_storage is None:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.ORPHAN_PAGE,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            page = (
                WorkflowProgressTopologyPage.objects.using(using)
                .select_for_update()
                .only("pk", "run_storage_id", "created_at")
                .filter(pk=page_id, run_storage=run_storage)
                .first()
            )
            if (
                page is None
                or page.created_at > cutoff
                or page.manifest_links.using(using).exists()
            ):
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.ORPHAN_PAGE,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            if not delete:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.ORPHAN_PAGE,
                    identifier,
                    WorkflowProgressCleanupOutcome.ELIGIBLE,
                )
            prior_page_failure = _orphan_page_failure_code(page_id)
            if run_storage.cleanup_error is not None and run_storage.cleanup_error.startswith(
                prior_page_failure
            ):
                run_storage.cleanup_error = None
                run_storage.save(update_fields=["cleanup_error"])
            page.delete(using=using)
            return WorkflowProgressCleanupItem(
                WorkflowProgressCleanupKind.ORPHAN_PAGE,
                identifier,
                WorkflowProgressCleanupOutcome.DELETED,
            )
    except Exception as error:
        diagnostic = _cleanup_diagnostic(_orphan_page_failure_code(page_id), error)
        _record_run_failure(
            execution_id,
            run_storage_id,
            diagnostic,
            using=using,
        )
        return WorkflowProgressCleanupItem(
            WorkflowProgressCleanupKind.ORPHAN_PAGE,
            identifier,
            WorkflowProgressCleanupOutcome.FAILED,
            diagnostic,
        )


def _process_empty_run(
    execution_id: int,
    run_storage_id: int,
    *,
    delete: bool,
    using: str,
) -> WorkflowProgressCleanupItem:
    """Delete an inactive run after every durable child has disappeared."""
    identifier = str(run_storage_id)
    try:
        with transaction.atomic(using=using):
            execution = _lock_task(execution_id, using=using)
            if execution is None:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.EMPTY_RUN,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            run_storage = (
                WorkflowProgressRunStorage.objects.using(using)
                .select_for_update()
                .only(
                    "pk",
                    "execution_id",
                    "attempt_number",
                    "execution_generation",
                    "run_id",
                    "detail_revision",
                )
                .filter(pk=run_storage_id, execution_id=execution.pk)
                .first()
            )
            if (
                run_storage is None
                or _is_exact_active_current(execution, run_storage)
                or run_storage.detail_revision is not None
                or run_storage.topology_manifests.using(using).exists()
                or run_storage.topology_pages.using(using).exists()
                or run_storage.node_details.using(using).exists()
            ):
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.EMPTY_RUN,
                    identifier,
                    WorkflowProgressCleanupOutcome.SKIPPED,
                )
            if not delete:
                return WorkflowProgressCleanupItem(
                    WorkflowProgressCleanupKind.EMPTY_RUN,
                    identifier,
                    WorkflowProgressCleanupOutcome.ELIGIBLE,
                )
            run_storage.delete(using=using)
            return WorkflowProgressCleanupItem(
                WorkflowProgressCleanupKind.EMPTY_RUN,
                identifier,
                WorkflowProgressCleanupOutcome.DELETED,
            )
    except Exception as error:
        diagnostic = _cleanup_diagnostic("EMPTY_RUN_DELETE_FAILED", error)
        _record_run_failure(
            execution_id,
            run_storage_id,
            diagnostic,
            using=using,
        )
        return WorkflowProgressCleanupItem(
            WorkflowProgressCleanupKind.EMPTY_RUN,
            identifier,
            WorkflowProgressCleanupOutcome.FAILED,
            diagnostic,
        )


def cleanup_workflow_progress_storage(
    *,
    delete: bool = False,
    batch_size: int = WORKFLOW_PROGRESS_CLEANUP_DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
    using: str = "default",
) -> WorkflowProgressCleanupReport:
    """Re-check and optionally delete one bounded batch of cleanup candidates.

    The batch limit applies independently to expired runs, pending manifests,
    orphan pages, and inactive empty runs, bounding one pass to at most four
    times ``batch_size`` items. Within each class, candidates without a previous
    cleanup failure run first so one permanent failure cannot starve newer work.
    """
    batch_size = _validate_batch_size(batch_size)
    if not isinstance(delete, bool):
        raise ValueError("workflow progress cleanup delete flag must be a boolean")
    observed_at = _validate_now(now or timezone.now())
    pending_cutoff = observed_at - WORKFLOW_PROGRESS_ORPHAN_GRACE_PERIOD
    items: list[WorkflowProgressCleanupItem] = []

    active_current = ~Q(execution__state__in=_TERMINAL_STATES) & Q(
        execution__workflow_run_id__isnull=False,
        attempt_number=F("execution__attempt_number"),
        execution_generation=F("execution__execution_generation"),
        run_id=F("execution__workflow_run_id"),
    )
    run_candidates = tuple(
        WorkflowProgressRunStorage.objects.using(using)
        .filter(detail_expires_at__lte=observed_at)
        .filter(~active_current)
        .order_by(F("cleanup_error").asc(nulls_first=True), "detail_expires_at", "pk")
        .values_list("execution_id", "pk")[:batch_size]
    )
    for execution_id, run_storage_id in run_candidates:
        items.append(
            _process_expired_run(
                execution_id,
                run_storage_id,
                now=observed_at,
                delete=delete,
                using=using,
            )
        )

    manifest_candidates = tuple(
        WorkflowProgressTopologyManifest.objects.using(using)
        .filter(
            slot=WorkflowProgressTopologySlot.PENDING,
            created_at__lte=pending_cutoff,
        )
        .order_by(F("cleanup_error").asc(nulls_first=True), "created_at", "pk")
        .values_list("run_storage__execution_id", "run_storage_id", "pk")[:batch_size]
    )
    for execution_id, run_storage_id, manifest_id in manifest_candidates:
        items.append(
            _process_pending_manifest(
                execution_id,
                run_storage_id,
                str(manifest_id),
                cutoff=pending_cutoff,
                delete=delete,
                using=using,
            )
        )

    page_candidates = tuple(
        WorkflowProgressTopologyPage.objects.using(using)
        .filter(created_at__lte=pending_cutoff, manifest_links__isnull=True)
        .order_by(
            F("run_storage__cleanup_error").asc(nulls_first=True),
            "created_at",
            "pk",
        )
        .values_list("run_storage__execution_id", "run_storage_id", "pk")[:batch_size]
    )
    for execution_id, run_storage_id, page_id in page_candidates:
        items.append(
            _process_orphan_page(
                execution_id,
                run_storage_id,
                page_id,
                cutoff=pending_cutoff,
                delete=delete,
                using=using,
            )
        )

    empty_run_candidates = tuple(
        WorkflowProgressRunStorage.objects.using(using)
        .filter(
            detail_revision__isnull=True,
            topology_manifests__isnull=True,
            topology_pages__isnull=True,
            node_details__isnull=True,
        )
        .filter(Q(detail_expires_at__isnull=True) | Q(detail_expires_at__gt=observed_at))
        .filter(~active_current)
        .order_by(F("cleanup_error").asc(nulls_first=True), "created_at", "pk")
        .values_list("execution_id", "pk")[:batch_size]
    )
    for execution_id, run_storage_id in empty_run_candidates:
        items.append(
            _process_empty_run(
                execution_id,
                run_storage_id,
                delete=delete,
                using=using,
            )
        )

    return WorkflowProgressCleanupReport(
        delete=delete,
        pending_cutoff=pending_cutoff,
        items=tuple(items),
    )


__all__ = [
    "WORKFLOW_PROGRESS_CLEANUP_DEFAULT_BATCH_SIZE",
    "WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE",
    "WORKFLOW_PROGRESS_ORPHAN_GRACE_PERIOD",
    "WorkflowProgressCleanupItem",
    "WorkflowProgressCleanupKind",
    "WorkflowProgressCleanupOutcome",
    "WorkflowProgressCleanupReport",
    "cleanup_workflow_progress_storage",
]
