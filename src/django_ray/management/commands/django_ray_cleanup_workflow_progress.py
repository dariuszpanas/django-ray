"""Safely clean expired and orphaned workflow-progress storage."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_ray.workflow.progress.cleanup import (
    WORKFLOW_PROGRESS_CLEANUP_DEFAULT_BATCH_SIZE,
    WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE,
    WorkflowProgressCleanupKind,
    cleanup_workflow_progress_storage,
)


class Command(BaseCommand):
    """Preview or perform one bounded workflow-progress cleanup pass."""

    help = "Preview or delete expired workflow detail and stale topology orphans."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--batch-size",
            type=int,
            default=WORKFLOW_PROGRESS_CLEANUP_DEFAULT_BATCH_SIZE,
            help=(
                "Maximum candidates per storage class "
                f"(default: {WORKFLOW_PROGRESS_CLEANUP_DEFAULT_BATCH_SIZE}; "
                f"maximum: {WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE})."
            ),
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete eligible rows. Without this flag the command is a dry run.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = int(options["batch_size"])
        if not 1 <= batch_size <= WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE:
            raise CommandError(
                f"--batch-size must be between 1 and {WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE}"
            )
        delete = bool(options["delete"])
        report = cleanup_workflow_progress_storage(
            delete=delete,
            batch_size=batch_size,
        )
        mode = "delete" if delete else "dry-run"
        self.stdout.write(
            "Workflow progress cleanup "
            f"{mode}: {report.eligible_count} eligible, "
            f"{report.deleted_count} deleted, {report.failed_count} failed, "
            f"{report.skipped_count} skipped "
            "("
            f"{report.count(WorkflowProgressCleanupKind.EXPIRED_RUN)} expired runs, "
            f"{report.count(WorkflowProgressCleanupKind.PENDING_MANIFEST)} "
            "pending manifests, "
            f"{report.count(WorkflowProgressCleanupKind.ORPHAN_PAGE)} orphan pages, "
            f"{report.count(WorkflowProgressCleanupKind.EMPTY_RUN)} empty runs"
            ")."
        )
        if report.failed_count:
            raise CommandError(
                f"Failed to clean {report.failed_count} workflow progress item(s); "
                "see bounded cleanup_error diagnostics"
            )
