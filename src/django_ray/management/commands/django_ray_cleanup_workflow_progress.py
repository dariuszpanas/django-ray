"""Safely clean expired and orphaned workflow-progress storage."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.utils import ConnectionDoesNotExist

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
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help=f"Django database alias to clean (default: {DEFAULT_DB_ALIAS}).",
        )
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="Output format: text or json (default: text).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Emit structured JSON output (shorthand for --format=json).",
        )

    @staticmethod
    def _validate_database(alias: Any) -> str:
        if type(alias) is not str or not alias:
            raise CommandError("--database must be a non-empty database alias.")
        try:
            connections[alias]
        except ConnectionDoesNotExist:
            raise CommandError(f"Unknown database alias: {alias}") from None
        return alias

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        raw_batch_size = options["batch_size"]
        if (
            type(raw_batch_size) is not int
            or not 1 <= raw_batch_size <= WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE
        ):
            raise CommandError(
                f"--batch-size must be between 1 and {WORKFLOW_PROGRESS_CLEANUP_MAX_BATCH_SIZE}"
            )
        batch_size = raw_batch_size
        database = self._validate_database(options["database"])
        delete = bool(options["delete"])
        output_format = "json" if options.get("json_output") else str(options["format"])
        report = cleanup_workflow_progress_storage(
            delete=delete,
            batch_size=batch_size,
            using=database,
        )
        mode = "delete" if delete else "dry-run"
        expired_runs = report.count(WorkflowProgressCleanupKind.EXPIRED_RUN)
        pending_manifests = report.count(WorkflowProgressCleanupKind.PENDING_MANIFEST)
        orphan_pages = report.count(WorkflowProgressCleanupKind.ORPHAN_PAGE)
        empty_runs = report.count(WorkflowProgressCleanupKind.EMPTY_RUN)

        if output_format == "json":
            payload = {
                "schema_version": 1,
                "mode": mode,
                "database": database,
                "batch_size": batch_size,
                "counts": {
                    "eligible": report.eligible_count,
                    "deleted": report.deleted_count,
                    "failed": report.failed_count,
                    "skipped": report.skipped_count,
                    "expired_runs": expired_runs,
                    "pending_manifests": pending_manifests,
                    "orphan_pages": orphan_pages,
                    "empty_runs": empty_runs,
                },
            }
            self.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        else:
            self.stdout.write(
                "Workflow progress cleanup "
                f"{mode}: {report.eligible_count} eligible, "
                f"{report.deleted_count} deleted, {report.failed_count} failed, "
                f"{report.skipped_count} skipped "
                "("
                f"{expired_runs} expired runs, "
                f"{pending_manifests} pending manifests, "
                f"{orphan_pages} orphan pages, "
                f"{empty_runs} empty runs"
                ")."
            )
        if report.failed_count:
            raise CommandError(
                f"Failed to clean {report.failed_count} workflow progress item(s); "
                "see bounded cleanup_error diagnostics"
            )
