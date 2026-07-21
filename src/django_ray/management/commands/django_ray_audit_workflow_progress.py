"""Audit normalized workflow-progress detail for one exact run."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_progress_storage import (
    WorkflowProgressStorageError,
    WorkflowProgressStorageIntegrityError,
    audit_workflow_progress_detail_storage,
)


class Command(BaseCommand):
    """Run one read-only, bounded whole-run detail audit."""

    help = "Audit normalized workflow detail for one exact run without changing storage."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--task-execution-pk", type=int, required=True)
        parser.add_argument("--attempt-number", type=int, required=True)
        parser.add_argument("--execution-generation", type=int, required=True)
        parser.add_argument("--run-id", required=True)
        parser.add_argument(
            "--database",
            default="default",
            help="Django database alias to audit (default: default).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        identity = WorkflowRunIdentity(
            task_execution_pk=int(options["task_execution_pk"]),
            attempt_number=int(options["attempt_number"]),
            execution_generation=int(options["execution_generation"]),
            run_id=str(options["run_id"]),
        )
        try:
            result = audit_workflow_progress_detail_storage(
                identity,
                using=str(options["database"]),
            )
        except (WorkflowProgressStorageError, WorkflowProgressStorageIntegrityError) as error:
            raise CommandError(f"Workflow progress detail audit failed: {error}") from error
        states = ",".join(f"{state}:{count}" for state, count in result.state_counts)
        self.stdout.write(
            "Workflow progress detail audit passed: "
            f"task_execution_pk={identity.task_execution_pk} "
            f"attempt_number={identity.attempt_number} "
            f"execution_generation={identity.execution_generation} "
            f"run_id={identity.run_id} "
            f"topology_version={result.topology_version} "
            f"detail_revision={result.detail_revision} "
            f"nodes={result.node_count} "
            f"encoded_bytes={result.encoded_bytes} "
            f"decoded_bytes={result.decoded_bytes} "
            f"events={result.event_count} "
            f"truncated={result.truncated_count} "
            f"states={states}."
        )
