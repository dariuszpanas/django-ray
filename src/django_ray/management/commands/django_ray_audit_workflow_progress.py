"""Audit normalized workflow-progress detail for one exact run."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_ray.management.diagnostics import render_console_diagnostic
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.storage import (
    WorkflowProgressStorageError,
    WorkflowProgressStorageIntegrityError,
    audit_workflow_progress_detail_storage,
)


class Command(BaseCommand):
    """Run one read-only, bounded whole-run detail audit."""

    help = "Audit normalized workflow detail for one exact run without changing storage."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--task-execution-pk",
            type=int,
            required=True,
            help="Primary key of the RayTaskExecution row to audit (required).",
        )
        parser.add_argument(
            "--attempt-number",
            type=int,
            required=True,
            help="Attempt number of the workflow run to audit (required).",
        )
        parser.add_argument(
            "--execution-generation",
            type=int,
            required=True,
            help="Execution generation fencing token of the workflow run to audit (required).",
        )
        parser.add_argument(
            "--run-id",
            required=True,
            help="Workflow run UUID to audit (required).",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Django database alias to audit (default: default).",
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
            dest="as_json",
            help="Emit JSON output (alias for --format json).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        use_json = bool(options.get("as_json")) or options.get("format", "text") == "json"
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
            diagnostic = render_console_diagnostic(error)
            raise CommandError(f"Workflow progress detail audit failed: {diagnostic}") from None
        if use_json:
            payload = {
                "status": "passed",
                "task_execution_pk": identity.task_execution_pk,
                "attempt_number": identity.attempt_number,
                "execution_generation": identity.execution_generation,
                "run_id": identity.run_id,
                "topology_version": result.topology_version,
                "detail_revision": result.detail_revision,
                "nodes": result.node_count,
                "encoded_bytes": result.encoded_bytes,
                "decoded_bytes": result.decoded_bytes,
                "events": result.event_count,
                "truncated": result.truncated_count,
                "states": dict(result.state_counts),
            }
            self.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            return
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
