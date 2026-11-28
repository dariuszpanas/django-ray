"""Safely purge retained external task-input and Ray Job request payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Case, F, Q, When
from django.db.models.functions import Substr
from django.db.utils import ConnectionDoesNotExist
from django.utils import timezone

from django_ray.management.diagnostics import render_exception_type_label
from django_ray.management.purge_cursor import (
    RETENTION_MAX_DAYS,
    InvalidPurgeCursorError,
    PurgeCursor,
    decode_purge_cursor,
    encode_purge_cursor,
)
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
    TaskState,
)
from django_ray.payload_registry import (
    DEFAULT_INVENTORY_MAX_OBJECTS,
    DEFAULT_INVENTORY_MAX_PAGES,
    DEFAULT_UNREGISTERED_GRACE_SECONDS,
    MAX_INVENTORY_MAX_OBJECTS,
    MAX_INVENTORY_MAX_PAGES,
    MAX_UNREGISTERED_GRACE_SECONDS,
    MIN_UNREGISTERED_GRACE_SECONDS,
    _has_other_payload_registry_reference,
    _payload_registry_references,
    discover_and_reap_unregistered_payloads,
)
from django_ray.ray_job_request_storage import (
    RayJobRequestStorageError,
    _native_request_reference,
)
from django_ray.result_storage import ResultStorageError

TERMINAL_STATES = (
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.LOST,
    TaskState.EXPIRED,
)
DEFAULT_CANDIDATES = 100
MAX_CANDIDATES = 1000
DEFAULT_ATTACHMENTS = 100
MAX_ATTACHMENTS = 1000
DEFAULT_DETAILS = 20
MAX_DETAILS = 100
DEFAULT_UNTIL_EMPTY_MAX_BATCHES = 100
MAX_BATCHES = 1000
OUTPUT_MAX_BYTES = 16384
# Reserve space for the bounded encrypted cursor, counters, and fixed diagnostics.
DETAIL_MAX_BYTES = 8192
# Minimum grace floor protecting freshly registered inputs between split enqueue
# transactions when operators pass --retention-days 0 (R3-007).
UNATTACHED_ENQUEUE_GRACE_SECONDS = 60


@dataclass(frozen=True)
class _PurgeExecution:
    """Bounded locked-row values without deferred model fields."""

    state_is_terminal: bool
    finished_at: datetime | None
    input_reference: str | None
    ray_job_request_reference: str | None
    references_complete: bool


def _manager_for_database(model: Any, using: str) -> Any:
    manager = model.objects
    if using != DEFAULT_DB_ALIAS or hasattr(manager, "using"):
        return manager.using(using)
    return manager


class Command(BaseCommand):
    """Report or delete registered payloads beyond the selected retention window."""

    help = (
        "Report or purge external task-input and Ray Job request payloads whose "
        "references are all old and terminal."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--retention-days",
            type=int,
            default=None,
            help="Retain payloads used within this many days (default: 30).",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete eligible objects. Without this flag the command is a dry run.",
        )
        parser.add_argument(
            "--max-candidates",
            type=int,
            default=DEFAULT_CANDIDATES,
            help=(
                "Maximum registered payload candidates to inspect per batch "
                f"(default: {DEFAULT_CANDIDATES}; maximum: {MAX_CANDIDATES})."
            ),
        )
        parser.add_argument(
            "--max-attachments",
            type=int,
            default=DEFAULT_ATTACHMENTS,
            help=(
                "Maximum attached executions inspected per payload before marking "
                f"incomplete (default: {DEFAULT_ATTACHMENTS}; maximum: {MAX_ATTACHMENTS})."
            ),
        )
        parser.add_argument(
            "--max-details",
            type=int,
            default=DEFAULT_DETAILS,
            help=(
                "Maximum per-payload detail lines included in the bounded report "
                f"(default: {DEFAULT_DETAILS}; maximum: {MAX_DETAILS})."
            ),
        )
        parser.add_argument(
            "--cursor",
            help="Continue a report with its fixed cutoff and mode.",
        )
        parser.add_argument(
            "--until-empty",
            action="store_true",
            help=(
                "Continue across candidate batches until no more eligible rows remain "
                "or --max-batches is reached (avoids shell --cursor passing in cron)."
            ),
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            default=None,
            help=(
                "Maximum candidate batches to process in one invocation "
                f"(default: 1; default with --until-empty: {DEFAULT_UNTIL_EMPTY_MAX_BATCHES}; "
                f"maximum: {MAX_BATCHES})."
            ),
        )
        parser.add_argument(
            "--discover-unregistered",
            action="store_true",
            help="Inventory configured input storage for unregistered orphan objects.",
        )
        parser.add_argument(
            "--delete-unregistered",
            action="store_true",
            help=(
                "Revalidate and delete eligible unregistered objects "
                "(requires --discover-unregistered and --delete)."
            ),
        )
        parser.add_argument(
            "--grace-seconds",
            type=int,
            default=DEFAULT_UNREGISTERED_GRACE_SECONDS,
            help=(
                "Grace period in seconds before an unregistered object is eligible "
                f"(default: {DEFAULT_UNREGISTERED_GRACE_SECONDS})."
            ),
        )
        parser.add_argument(
            "--max-inventory-objects",
            type=int,
            default=DEFAULT_INVENTORY_MAX_OBJECTS,
            help=(
                "Maximum storage objects scanned during unregistered inventory "
                f"(default: {DEFAULT_INVENTORY_MAX_OBJECTS}; "
                f"maximum: {MAX_INVENTORY_MAX_OBJECTS})."
            ),
        )
        parser.add_argument(
            "--max-inventory-pages",
            type=int,
            default=DEFAULT_INVENTORY_MAX_PAGES,
            help=(
                "Maximum backend listing pages scanned during unregistered inventory "
                f"(default: {DEFAULT_INVENTORY_MAX_PAGES}; maximum: {MAX_INVENTORY_MAX_PAGES})."
            ),
        )
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help=f"Django database alias to purge (default: {DEFAULT_DB_ALIAS}).",
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
    def _budget(options: dict[str, Any], name: str, maximum: int, *, minimum: int = 1) -> int:
        value = options[name]
        if type(value) is not int or not minimum <= value <= maximum:
            option = name.replace("_", "-")
            raise CommandError(f"--{option} must be between {minimum} and {maximum}")
        return value

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
        candidate_limit = self._budget(options, "max_candidates", MAX_CANDIDATES)
        attachment_limit = self._budget(options, "max_attachments", MAX_ATTACHMENTS)
        detail_limit = self._budget(options, "max_details", MAX_DETAILS, minimum=0)
        retention_days = options["retention_days"]
        if retention_days is not None and (
            type(retention_days) is not int or not 0 <= retention_days <= RETENTION_MAX_DAYS
        ):
            raise CommandError(
                f"--retention-days must be zero or greater, at most {RETENTION_MAX_DAYS}"
            )
        delete = bool(options["delete"])
        until_empty = bool(options.get("until_empty"))
        raw_max_batches = options.get("max_batches")
        if raw_max_batches is not None:
            if type(raw_max_batches) is not int or not 1 <= raw_max_batches <= MAX_BATCHES:
                raise CommandError(f"--max-batches must be between 1 and {MAX_BATCHES}")
            max_batches = raw_max_batches
        else:
            max_batches = DEFAULT_UNTIL_EMPTY_MAX_BATCHES if until_empty else 1

        discover_unregistered = bool(options.get("discover_unregistered"))
        delete_unregistered = bool(options.get("delete_unregistered"))
        if delete_unregistered and (not discover_unregistered or not delete):
            raise CommandError(
                "--delete-unregistered requires both --discover-unregistered and --delete"
            )
        grace_seconds = self._budget(
            options,
            "grace_seconds",
            MAX_UNREGISTERED_GRACE_SECONDS,
            minimum=MIN_UNREGISTERED_GRACE_SECONDS,
        )
        max_inventory_objects = self._budget(
            options,
            "max_inventory_objects",
            MAX_INVENTORY_MAX_OBJECTS,
        )
        max_inventory_pages = self._budget(
            options,
            "max_inventory_pages",
            MAX_INVENTORY_MAX_PAGES,
        )
        database = self._validate_database(options.get("database", DEFAULT_DB_ALIAS))
        output_format = "json" if options.get("json_output") else str(options["format"])

        after = ""
        if options["cursor"] is not None:
            try:
                cursor = decode_purge_cursor(options["cursor"])
            except InvalidPurgeCursorError:
                raise CommandError("Invalid purge cursor; start a fresh pass.") from None
            if (
                delete != cursor.delete
                or database != cursor.database
                or (retention_days is not None and retention_days != cursor.retention_days)
            ):
                raise CommandError(
                    "Purge cursor conflicts with deletion mode, retention days, or database."
                )
            after, cutoff, retention_days = cursor.after, cursor.cutoff, cursor.retention_days
        else:
            retention_days = 30 if retention_days is None else retention_days
            now = timezone.now()
            cutoff = min(
                now - timedelta(days=retention_days),
                now - timedelta(seconds=UNATTACHED_ENQUEUE_GRACE_SECONDS),
            )

        counts = dict.fromkeys(
            ("processed", "eligible", "purged", "retained", "failed", "incomplete"), 0
        )
        details: list[dict[str, str]] = []
        detail_bytes = 0
        has_more = False
        next_cursor: str | None = None

        for _batch_index in range(max_batches):
            # One extra PK proves overflow without materializing the registry. A
            # continuation retains the PK itself even if that row later disappears.
            candidate_keys = list(
                _manager_for_database(TaskInputPayload, database)
                .filter(
                    state=InputPayloadState.ACTIVE,
                    last_used_at__lte=cutoff,
                    pk__gt=after,
                )
                .order_by("pk")
                .annotate(bounded_key=Substr("pk", 1, 501))
                .annotate(key_complete=Case(When(pk=F("bounded_key"), then=True), default=False))
                .values_list("bounded_key", "key_complete")[: candidate_limit + 1]
            )
            # SQLite does not enforce CharField lengths. Reject a corrupt key before
            # loading it in full, advancing a cursor past truncated text, or deleting
            # any object from this batch. SQLite substr also truncates at NUL.
            if any(not complete or len(reference) > 500 for reference, complete in candidate_keys):
                raise CommandError("Cannot continue this registry; invalid primary key.")
            references = [reference for reference, _ in candidate_keys]
            batch_has_more = len(references) > candidate_limit
            references = references[:candidate_limit]
            # Prepare the continuation before mutations, so a malformed persisted PK
            # cannot cause a late reporting failure after some objects were deleted.
            batch_next_cursor: str | None = None
            if batch_has_more:
                try:
                    batch_next_cursor = encode_purge_cursor(
                        PurgeCursor(
                            references[-1],
                            cutoff,
                            retention_days,
                            delete,
                            database=database,
                        )
                    )
                except InvalidPurgeCursorError:
                    raise CommandError(
                        "Cannot continue this registry; invalid primary key."
                    ) from None

            for reference in references:
                process_kwargs: dict[str, Any] = {
                    "cutoff": cutoff,
                    "delete": delete,
                    "max_attachments": attachment_limit,
                }
                if database != DEFAULT_DB_ALIAS:
                    process_kwargs["using"] = database
                outcome = self._process_reference(reference, **process_kwargs)
                counts["processed"] += 1
                if outcome in {"eligible", "purged", "failed"}:
                    counts["eligible"] += 1
                if outcome in {"purged", "failed", "incomplete"}:
                    counts[outcome] += 1
                if outcome != "purged":
                    counts["retained"] += 1
                if len(details) < detail_limit:
                    detail = {
                        "reference_sha256": self._reference_fingerprint(reference),
                        "outcome": outcome,
                    }
                    size = len(json.dumps(detail, separators=(",", ":")).encode("utf-8")) + 2
                    if detail_bytes + size <= DETAIL_MAX_BYTES:
                        details.append(detail)
                        detail_bytes += size

            has_more = batch_has_more
            next_cursor = batch_next_cursor
            if not batch_has_more or not references:
                break
            after = references[-1]

        inventory_report = None
        inventory_error = None
        if discover_unregistered:
            try:
                inventory_kwargs: dict[str, Any] = {
                    "grace_seconds": grace_seconds,
                    "max_objects": max_inventory_objects,
                    "max_pages": max_inventory_pages,
                    "max_details": detail_limit,
                    "delete_unregistered": delete_unregistered,
                }
                if database != DEFAULT_DB_ALIAS:
                    inventory_kwargs["using"] = database
                inventory_report = discover_and_reap_unregistered_payloads(**inventory_kwargs)
            except Exception:
                # Registered mutations have already committed independently.
                # Preserve their report without inventing unknown inventory counts.
                inventory_error = "inventory_failed"

        mode = "delete" if delete else "dry-run"
        report: dict[str, Any] = {
            "schema_version": 1,
            "mode": mode,
            "cutoff": cutoff.isoformat(),
            "retention_days": retention_days,
            "counts": counts,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "details": details,
            "details_omitted": counts["processed"] - len(details),
            "limits": {
                "candidates": candidate_limit,
                "attachments_per_payload": attachment_limit,
                "details": detail_limit,
                "output_bytes": OUTPUT_MAX_BYTES,
            },
        }
        if inventory_report is not None:
            report["unregistered_inventory"] = inventory_report.as_dict()
        if inventory_error is not None:
            report["inventory_error"] = inventory_error
        if output_format == "json":
            rendered = json.dumps(report, ensure_ascii=True, separators=(",", ":"))
        else:
            lines = [
                f"Input payload purge {mode}: {counts['eligible']} eligible, "
                f"{counts['purged']} purged, {counts['failed']} failed.",
                f"Processed {counts['processed']}; retained {counts['retained']}; "
                f"incomplete {counts['incomplete']}; details omitted {report['details_omitted']}.",
                f"Cutoff: {report['cutoff']}; more candidates: {str(has_more).lower()}.",
            ]
            lines.extend(
                f"reference_sha256={detail['reference_sha256']} outcome={detail['outcome']}"
                for detail in details
            )
            if inventory_report is not None:
                inv_counts = inventory_report.counts
                lines.append(
                    f"Unregistered inventory ({inventory_report.backend}): "
                    f"scanned {inv_counts['scanned']}, "
                    f"eligible {inv_counts['unregistered_eligible']}, "
                    f"recent {inv_counts['unregistered_recent']}, "
                    f"purged {inv_counts['purged']}, "
                    f"ambiguous {inv_counts['ambiguous']}, "
                    f"malformed {inv_counts['malformed_key']}, "
                    f"missing {inv_counts['registered_missing']}, "
                    f"failed {inv_counts['failed']}."
                )
            if inventory_error is not None:
                lines.append("Unregistered inventory: inventory_failed; outcomes unavailable.")
            if next_cursor is not None:
                lines.append(f"Next cursor: {next_cursor}")
            rendered = "\n".join(lines)
        # All fields and detail allocation were bounded before rendering. Keep
        # room for the newline and one fixed CommandError on stderr as well.
        if len(rendered.encode("utf-8")) > OUTPUT_MAX_BYTES - 256:
            raise CommandError("Purge report exceeded its output bound.")
        self.stdout.write(rendered)
        if inventory_error is not None:
            raise CommandError("Unregistered payload inventory failed; inspect the partial report.")
        inventory_failed = inventory_report.counts["failed"] if inventory_report is not None else 0
        if counts["failed"] or inventory_failed:
            raise CommandError(
                f"Failed to purge {counts['failed'] + inventory_failed} input payload(s); see cleanup_error"
            )
        inventory_incomplete = (
            inventory_report.has_more
            or inventory_report.counts["ambiguous"] > 0
            or inventory_report.counts["malformed_key"] > 0
            or inventory_report.counts["registered_missing"] > 0
            if inventory_report is not None
            else False
        )
        if has_more or counts["incomplete"] or inventory_incomplete:
            raise CommandError("Purge pass incomplete; inspect the bounded report.", returncode=2)

    def _process_reference(
        self,
        reference: str,
        *,
        cutoff: Any,
        delete: bool,
        max_attachments: int = DEFAULT_ATTACHMENTS,
        using: str = DEFAULT_DB_ALIAS,
    ) -> str:
        if type(max_attachments) is not int or not 1 <= max_attachments <= MAX_ATTACHMENTS:
            raise CommandError(f"--max-attachments must be between 1 and {MAX_ATTACHMENTS}")
        with transaction.atomic(using=using):
            payload = (
                TaskInputPayload.objects.using(using)
                .select_for_update()
                .filter(reference=reference, state=InputPayloadState.ACTIVE)
                .only("reference", "state", "last_used_at", "payload_kind")
                .first()
            )
            grace_cutoff = timezone.now() - timedelta(seconds=UNATTACHED_ENQUEUE_GRACE_SECONDS)
            if (
                payload is None
                or payload.last_used_at > cutoff
                or payload.last_used_at > grace_cutoff
            ):
                return "skipped"

            try:
                if payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST:
                    _native_request_reference(reference)
                references = _payload_registry_references(reference)
            except (ResultStorageError, RayJobRequestStorageError):
                return "incomplete"
            # Every kind/state can own the same physical object through a
            # different supported spelling. Producers lock existing aliases
            # before insertion; this check takes no second row lock.
            if _has_other_payload_registry_reference(reference, references, using=using):
                return "incomplete"

            executions = [
                _PurgeExecution(*row)
                for row in RayTaskExecution.objects.using(using)
                .select_for_update()
                .filter(Q(input_reference=reference) | Q(ray_job_request_reference=reference))
                .annotate(
                    state_is_terminal=Case(
                        When(state__in=TERMINAL_STATES, then=True), default=False
                    ),
                    bounded_input=Substr("input_reference", 1, 501),
                    bounded_request=Substr("ray_job_request_reference", 1, 501),
                )
                .annotate(
                    references_complete=Case(
                        When(
                            (
                                Q(input_reference__isnull=True)
                                | Q(input_reference=F("bounded_input"))
                            )
                            & (
                                Q(ray_job_request_reference__isnull=True)
                                | Q(ray_job_request_reference=F("bounded_request"))
                            ),
                            then=True,
                        ),
                        default=False,
                    )
                )
                .values_list(
                    "state_is_terminal",
                    "finished_at",
                    "bounded_input",
                    "bounded_request",
                    "references_complete",
                )
                .order_by("pk")[: max_attachments + 1]
            ]
            if len(executions) > max_attachments:
                return "incomplete"
            if not self._all_references_are_old_and_terminal(
                executions,
                reference=reference,
                payload_kind=str(payload.payload_kind),
                cutoff=cutoff,
            ):
                return "skipped"

            if not delete:
                return "eligible"

            try:
                from django_ray.input_storage import delete_input_reference

                if payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST:
                    from django_ray.ray_job_request_storage import delete_ray_job_request_reference

                    delete_ray_job_request_reference(reference)
                else:
                    delete_input_reference(reference)
            except Exception as error:
                payload.cleanup_error = self._format_cleanup_error(error)
                payload.save(using=using, update_fields=["cleanup_error"])
                return "failed"

            payload.state = InputPayloadState.PURGED
            payload.purged_at = timezone.now()
            payload.cleanup_error = ""
            payload.save(using=using, update_fields=["state", "purged_at", "cleanup_error"])
            return "purged"

    @staticmethod
    def _all_references_are_old_and_terminal(
        executions: list[_PurgeExecution],
        *,
        reference: str,
        payload_kind: str,
        cutoff: Any,
    ) -> bool:
        if payload_kind not in InputPayloadKind.values:
            return False
        return all(
            Command._reference_matches_payload_kind(
                execution,
                reference=reference,
                payload_kind=payload_kind,
            )
            and execution.state_is_terminal
            and execution.finished_at is not None
            and execution.finished_at <= cutoff
            for execution in executions
        )

    @staticmethod
    def _reference_matches_payload_kind(
        execution: _PurgeExecution,
        *,
        reference: str,
        payload_kind: str,
    ) -> bool:
        # SQLite substr stops at NUL as well as the explicit length. Require the
        # raw values to equal their projections before exact Python comparisons.
        if not execution.references_complete or any(
            value is not None and len(value) > 500
            for value in (execution.input_reference, execution.ray_job_request_reference)
        ):
            return False
        input_matches = execution.input_reference == reference
        request_matches = execution.ray_job_request_reference == reference
        if input_matches == request_matches:
            # Neither column should be possible after the filtered lock query;
            # both columns indicate ambiguous ownership. Retain in either case.
            return False
        if payload_kind == InputPayloadKind.TASK_INPUT:
            return input_matches
        if payload_kind == InputPayloadKind.RAY_JOB_REQUEST:
            return request_matches
        return False

    @staticmethod
    def _format_cleanup_error(error: Exception) -> str:
        return render_exception_type_label(error)

    @staticmethod
    def _reference_fingerprint(reference: str) -> str:
        encoded = reference.encode("utf-8", errors="surrogatepass")
        return hashlib.sha256(encoded).hexdigest()[:16]
