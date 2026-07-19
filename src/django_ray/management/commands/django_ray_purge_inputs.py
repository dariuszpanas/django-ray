"""Safely purge retained external task-input payloads."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from django_ray.models import InputPayloadState, RayTaskExecution, TaskInputPayload, TaskState

TERMINAL_STATES = (
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.LOST,
)


class Command(BaseCommand):
    """Report or delete input payloads beyond the operator-selected retention window."""

    help = "Report or purge external task inputs whose references are all old and terminal."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--retention-days",
            type=int,
            default=30,
            help="Retain payloads used within this many days (default: 30).",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete eligible objects. Without this flag the command is a dry run.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        retention_days = int(options["retention_days"])
        if retention_days < 0:
            raise CommandError("--retention-days must be zero or greater")

        delete = bool(options["delete"])
        cutoff = timezone.now() - timedelta(days=retention_days)
        references = list(
            TaskInputPayload.objects.filter(
                state=InputPayloadState.ACTIVE,
                last_used_at__lte=cutoff,
            ).values_list("reference", flat=True)
        )

        eligible = 0
        purged = 0
        failures = 0
        for reference in references:
            outcome = self._process_reference(
                str(reference),
                cutoff=cutoff,
                delete=delete,
            )
            if outcome == "eligible":
                eligible += 1
            elif outcome == "purged":
                eligible += 1
                purged += 1
            elif outcome == "failed":
                eligible += 1
                failures += 1

        mode = "delete" if delete else "dry-run"
        self.stdout.write(
            f"Input payload purge {mode}: {eligible} eligible, {purged} purged, {failures} failed."
        )
        if failures:
            raise CommandError(f"Failed to purge {failures} input payload(s); see cleanup_error")

    def _process_reference(self, reference: str, *, cutoff: Any, delete: bool) -> str:
        with transaction.atomic():
            payload = (
                TaskInputPayload.objects.select_for_update()
                .filter(reference=reference, state=InputPayloadState.ACTIVE)
                .first()
            )
            if payload is None or payload.last_used_at > cutoff:
                return "skipped"

            executions = list(
                RayTaskExecution.objects.select_for_update()
                .filter(input_reference=reference)
                .only("pk", "state", "finished_at")
            )
            if not self._all_references_are_old_and_terminal(executions, cutoff=cutoff):
                return "skipped"

            if not delete:
                self.stdout.write(f"Would purge input payload {reference}")
                return "eligible"

            try:
                from django_ray.input_storage import delete_input_reference

                delete_input_reference(reference)
            except Exception as error:
                payload.cleanup_error = self._format_cleanup_error(error)
                payload.save(update_fields=["cleanup_error"])
                self.stderr.write(f"Failed to purge input payload {reference}: {error}")
                return "failed"

            payload.state = InputPayloadState.PURGED
            payload.purged_at = timezone.now()
            payload.cleanup_error = ""
            payload.save(update_fields=["state", "purged_at", "cleanup_error"])
            self.stdout.write(self.style.SUCCESS(f"Purged input payload {reference}"))
            return "purged"

    @staticmethod
    def _all_references_are_old_and_terminal(
        executions: list[RayTaskExecution],
        *,
        cutoff: Any,
    ) -> bool:
        return all(
            execution.state in TERMINAL_STATES
            and execution.finished_at is not None
            and execution.finished_at <= cutoff
            for execution in executions
        )

    @staticmethod
    def _format_cleanup_error(error: Exception) -> str:
        message = f"{type(error).__name__}: {error}"
        return message[:2000]
