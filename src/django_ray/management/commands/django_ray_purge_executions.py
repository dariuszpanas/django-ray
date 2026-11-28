"""Safely purge old terminal task executions, attempts, inactive leases, and external results."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DEFAULT_DB_ALIAS, connections, models, transaction
from django.db.models import ProtectedError, Q
from django.db.utils import ConnectionDoesNotExist
from django.utils import timezone

from django_ray.management.diagnostics import render_exception_type_label
from django_ray.management.purge_cursor import (
    RETENTION_MAX_DAYS,
    ExecutionPurgeCursor,
    InvalidPurgeCursorError,
    decode_execution_purge_cursor,
    encode_execution_purge_cursor,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
)

TERMINAL_STATES = (
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELLED,
    TaskState.LOST,
    TaskState.EXPIRED,
)
DEFAULT_OLDER_THAN_DAYS = 30
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 1000
DEFAULT_UNTIL_EMPTY_MAX_BATCHES = 100
MAX_BATCHES = 1000
_RAW_DELETE_CHUNK_SIZE = 500
_DIGEST_RESULT_PREFIX = "oversize://"
OUTPUT_MAX_BYTES = 16384


@dataclass(frozen=True)
class _ExecutionBatchPlan:
    """One bounded batch of candidate terminal executions and external result refs."""

    candidate_pks: tuple[int, ...]
    last_pk: int
    last_finished_at: datetime | None
    has_more: bool
    protected_pks: frozenset[int]
    attempts_by_execution: dict[int, int]
    result_refs_by_execution: dict[int, frozenset[str]]
    deletable_refs: tuple[str, ...]
    shared_refs: frozenset[str]


def _delete_queryset_in_chunks(queryset: Any, *, using: str) -> int:
    """Delete rows in bounded PK chunks without loading Django's cascade Collector."""
    model = queryset.model
    deleted_total = 0
    while True:
        pks = list(queryset.order_by("pk").values_list("pk", flat=True)[:_RAW_DELETE_CHUNK_SIZE])
        if not pks:
            return deleted_total
        deleted = model.objects.using(using).filter(pk__in=pks)._raw_delete(using)
        deleted_total += int(deleted or 0)


def _is_external_result_reference(reference: str | None) -> bool:
    return bool(
        isinstance(reference, str) and reference and not reference.startswith(_DIGEST_RESULT_PREFIX)
    )


def _normalize_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if timezone.is_naive(value):
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _result_blob_modified_at(reference: str) -> datetime | None:
    """Return the external result blob's UTC mtime/LastModified, or None if unavailable/missing."""
    import django_ray.result_storage as result_storage_module
    from django_ray._blob_storage import is_provider_not_found
    from django_ray.result_storage import (
        FilesystemResultStorage,
        GCSResultStorage,
        ResultStorageError,
        S3ResultStorage,
        _parse_result_reference,
        _validate_object_reference,
    )

    delete_helper = getattr(result_storage_module, "delete_result_reference", None)
    custom_delete_hook = (
        callable(delete_helper)
        and getattr(delete_helper, "__module__", "") != "django_ray.result_storage"
    )
    try:
        backend = result_storage_module.get_result_storage_backend_for_reference(reference)
    except ResultStorageError:
        if custom_delete_hook:
            return None
        raise

    if isinstance(backend, FilesystemResultStorage):
        metadata = backend._filesystem_reference(reference)
        full_path = backend._path_for_reference(metadata)
        try:
            st = full_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ResultStorageError("Failed to inspect filesystem result blob age") from error
        if not stat.S_ISREG(st.st_mode):
            return None
        return datetime.fromtimestamp(st.st_mtime_ns / 1_000_000_000, tz=UTC)

    if isinstance(backend, S3ResultStorage):
        metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
        bucket, key = _validate_object_reference(
            metadata,
            scheme="s3",
            bucket=backend.bucket,
            prefix=backend.prefix,
        )
        head_fn = getattr(backend.client, "head_object", None)
        if not callable(head_fn):
            return None
        try:
            response = head_fn(Bucket=bucket, Key=key)
        except Exception as error:
            if is_provider_not_found(error):
                return None
            raise ResultStorageError("Failed to inspect S3 result blob age") from error
        if isinstance(response, dict) and "LastModified" in response:
            return _normalize_timestamp(response.get("LastModified"))
        return None

    if isinstance(backend, GCSResultStorage):
        metadata = _parse_result_reference(reference, allow_encoding_legacy=True)
        bucket, key = _validate_object_reference(
            metadata,
            scheme="gs",
            bucket=backend.bucket,
            prefix=backend.prefix,
        )
        try:
            blob = backend.client.bucket(bucket).blob(key)
            blob.reload()
        except Exception as error:
            if is_provider_not_found(error):
                return None
            raise ResultStorageError("Failed to inspect GCS result blob age") from error
        raw_updated = getattr(blob, "updated", None)
        if raw_updated is None:
            raw_updated = getattr(blob, "time_created", None)
        return _normalize_timestamp(raw_updated)

    return None


def _delete_external_result_reference(reference: str) -> bool:
    """Delete one external result blob using the preferred Wave 2 helper when available."""
    import django_ray.result_storage as result_storage_module

    delete_helper = getattr(result_storage_module, "delete_result_reference", None)
    if callable(delete_helper):
        return bool(delete_helper(reference, ignore_missing=True))
    backend = result_storage_module.get_result_storage_backend_for_reference(reference)
    backend.delete(reference=reference)
    return True


class Command(BaseCommand):
    """Bounded, cursor-resumable retention purge for terminal executions and inactive leases."""

    help = (
        "Preview or purge old terminal RayTaskExecution rows, their archived TaskAttempt "
        "records, inactive TaskWorkerLease rows, and unshared external result blobs."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=None,
            help=(
                "Retain terminal executions and inactive leases newer than this many days "
                f"(default: {DEFAULT_OLDER_THAN_DAYS}; range: 0..{RETENTION_MAX_DAYS})."
            ),
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help=(
                "Delete eligible executions, attempts, inactive leases, and unshared "
                "external result blobs. Without this flag the command runs in dry-run mode."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Preview eligible rows and external results without deleting anything "
                "(default behavior; cannot be combined with --delete)."
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=(
                "Maximum executions and inactive leases to inspect per batch "
                f"(default: {DEFAULT_BATCH_SIZE}; maximum: {MAX_BATCH_SIZE})."
            ),
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            default=None,
            help=(
                "Maximum number of batches to execute in one invocation "
                f"(default: 1; default with --until-empty: {DEFAULT_UNTIL_EMPTY_MAX_BATCHES}; "
                f"maximum: {MAX_BATCHES})."
            ),
        )
        parser.add_argument(
            "--until-empty",
            action="store_true",
            help=(
                "Continue across batches until no more eligible rows remain or "
                "--max-batches is reached (recommended for cron jobs to avoid --cursor)."
            ),
        )
        parser.add_argument(
            "--cursor",
            default=None,
            help="Continue an incomplete purge pass with its fixed cutoff and mode.",
        )
        parser.add_argument(
            "--skip-leases",
            action="store_true",
            help="Skip purging inactive TaskWorkerLease rows.",
        )
        parser.add_argument(
            "--skip-results",
            action="store_true",
            help="Skip deleting external result_reference blobs (delete database rows only).",
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
    def _validate_database(alias: Any) -> str:
        if type(alias) is not str or not alias:
            raise CommandError("--database must be a non-empty database alias.")
        try:
            connections[alias]
        except ConnectionDoesNotExist:
            raise CommandError(f"Unknown database alias: {alias}") from None
        return alias

    @staticmethod
    def _validate_positive_budget(value: Any, option: str, maximum: int) -> int:
        if type(value) is not int or not 1 <= value <= maximum:
            raise CommandError(f"{option} must be between 1 and {maximum}")
        return value

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        delete = bool(options.get("delete"))
        dry_run = bool(options.get("dry_run"))
        if delete and dry_run:
            raise CommandError("--delete and --dry-run cannot be used together.")

        batch_size = self._validate_positive_budget(
            options["batch_size"], "--batch-size", MAX_BATCH_SIZE
        )
        until_empty = bool(options.get("until_empty"))
        raw_max_batches = options.get("max_batches")
        if raw_max_batches is not None:
            max_batches = self._validate_positive_budget(
                raw_max_batches, "--max-batches", MAX_BATCHES
            )
        else:
            max_batches = DEFAULT_UNTIL_EMPTY_MAX_BATCHES if until_empty else 1

        older_than_days = options.get("older_than_days")
        if older_than_days is not None and (
            type(older_than_days) is not int or not 0 <= older_than_days <= RETENTION_MAX_DAYS
        ):
            raise CommandError(
                f"--older-than-days must be zero or greater, at most {RETENTION_MAX_DAYS}"
            )
        skip_leases = bool(options.get("skip_leases"))
        skip_results = bool(options.get("skip_results"))
        database = self._validate_database(options.get("database", DEFAULT_DB_ALIAS))
        output_format = "json" if options.get("json_output") else str(options["format"])

        after_execution_pk = 0
        after_finished_at: datetime | None = None
        after_lease_id = ""
        if options.get("cursor") is not None:
            try:
                cursor = decode_execution_purge_cursor(options["cursor"])
            except InvalidPurgeCursorError:
                raise CommandError("Invalid purge cursor; start a fresh pass.") from None
            if (
                delete != cursor.delete
                or skip_leases != cursor.skip_leases
                or skip_results != cursor.skip_results
                or database != cursor.database
                or (older_than_days is not None and older_than_days != cursor.older_than_days)
            ):
                raise CommandError(
                    "Purge cursor conflicts with deletion mode, retention policy, or database."
                )
            after_execution_pk = cursor.after_execution_pk
            after_finished_at = cursor.after_finished_at
            after_lease_id = cursor.after_lease_id
            cutoff = cursor.cutoff
            older_than_days = cursor.older_than_days
        else:
            older_than_days = (
                DEFAULT_OLDER_THAN_DAYS if older_than_days is None else older_than_days
            )
            cutoff = timezone.now() - timedelta(days=older_than_days)

        counts: dict[str, int] = {
            "batches": 0,
            "executions_eligible": 0,
            "executions_deleted": 0,
            "executions_skipped": 0,
            "executions_failed": 0,
            "attempts_eligible": 0,
            "attempts_deleted": 0,
            "results_eligible": 0,
            "results_deleted": 0,
            "results_skipped_shared": 0,
            "results_failed": 0,
            "leases_eligible": 0,
            "leases_deleted": 0,
        }
        error_labels: list[str] = []
        executions_have_more = True
        leases_have_more = not skip_leases

        for _ in range(max_batches):
            if not executions_have_more and not leases_have_more:
                break
            counts["batches"] += 1

            if executions_have_more:
                plan = self._plan_execution_batch(
                    after_pk=after_execution_pk,
                    after_finished_at=after_finished_at,
                    cutoff=cutoff,
                    batch_size=batch_size,
                    skip_results=skip_results or delete,
                    using=database,
                )
                executions_have_more = plan.has_more
                if plan.last_finished_at is not None and (
                    after_finished_at is None
                    or (plan.last_finished_at, plan.last_pk)
                    > (after_finished_at, after_execution_pk)
                ):
                    after_finished_at = plan.last_finished_at
                    after_execution_pk = plan.last_pk
                elif plan.last_finished_at is None and plan.last_pk > after_execution_pk:
                    after_execution_pk = plan.last_pk

                eligible_pks = tuple(
                    pk for pk in plan.candidate_pks if pk not in plan.protected_pks
                )
                counts["executions_skipped"] += len(plan.protected_pks)
                counts["executions_eligible"] += len(eligible_pks)
                counts["attempts_eligible"] += sum(
                    plan.attempts_by_execution.get(pk, 0) for pk in eligible_pks
                )

                if not delete:
                    counts["results_eligible"] += len(plan.deletable_refs)
                    counts["results_skipped_shared"] += len(plan.shared_refs)
                elif eligible_pks:
                    # Phase 2: lock and delete DB rows FIRST, collecting candidate
                    # external result references only from rows confirmed deleted.
                    (
                        deleted_execs,
                        deleted_attempts,
                        skipped_changed,
                        protected_fail,
                        candidate_orphan_refs,
                    ) = self._delete_execution_batch(
                        eligible_pks,
                        cutoff=cutoff,
                        using=database,
                        skip_results=skip_results,
                    )
                    counts["executions_deleted"] += deleted_execs
                    counts["attempts_deleted"] += deleted_attempts
                    counts["executions_skipped"] += skipped_changed
                    counts["executions_failed"] += protected_fail

                    # Phase 3: strictly AFTER DB commit (outside any transaction),
                    # acquire the shared per-reference fence, re-verify zero DB
                    # references and blob mtime/LastModified <= cutoff, then
                    # delete unshared orphans (R3-017).
                    if not skip_results and candidate_orphan_refs:
                        from django_ray.lifecycle import result_reference_fence

                        orphan_refs, still_shared_refs = self._partition_orphan_result_refs(
                            candidate_orphan_refs,
                            using=database,
                        )
                        counts["results_skipped_shared"] += len(still_shared_refs)
                        normalized_cutoff = _normalize_timestamp(cutoff)
                        for ref in orphan_refs:
                            try:
                                with result_reference_fence(ref, using=database):
                                    if self._is_result_ref_referenced(ref, using=database):
                                        counts["results_skipped_shared"] += 1
                                        continue
                                    blob_modified_at = _result_blob_modified_at(ref)
                                    if (
                                        blob_modified_at is not None
                                        and normalized_cutoff is not None
                                        and blob_modified_at > normalized_cutoff
                                    ):
                                        counts["results_skipped_shared"] += 1
                                        continue
                                    counts["results_eligible"] += 1
                                    _delete_external_result_reference(ref)
                            except Exception as error:
                                counts["results_failed"] += 1
                                label = render_exception_type_label(error)
                                if len(error_labels) < 10 and label not in error_labels:
                                    error_labels.append(label)
                            else:
                                counts["results_deleted"] += 1

            if leases_have_more:
                (
                    lease_eligible,
                    lease_deleted,
                    lease_last_id,
                    lease_more,
                ) = self._process_lease_batch(
                    after_lease_id=after_lease_id,
                    cutoff=cutoff,
                    batch_size=batch_size,
                    delete=delete,
                    using=database,
                )
                counts["leases_eligible"] += lease_eligible
                counts["leases_deleted"] += lease_deleted
                leases_have_more = lease_more
                if lease_last_id > after_lease_id:
                    after_lease_id = lease_last_id

            if not executions_have_more and not leases_have_more:
                break

        has_more = executions_have_more or leases_have_more
        next_cursor: str | None = None
        if has_more:
            try:
                next_cursor = encode_execution_purge_cursor(
                    ExecutionPurgeCursor(
                        after_execution_pk=after_execution_pk,
                        after_lease_id=after_lease_id,
                        cutoff=cutoff,
                        older_than_days=older_than_days,
                        delete=delete,
                        skip_leases=skip_leases,
                        skip_results=skip_results,
                        database=database,
                        after_finished_at=after_finished_at,
                    )
                )
            except InvalidPurgeCursorError:
                raise CommandError("Unable to encode execution purge cursor.") from None

        mode = "delete" if delete else "dry-run"
        report: dict[str, Any] = {
            "schema_version": 1,
            "mode": mode,
            "database": database,
            "cutoff": cutoff.isoformat(),
            "older_than_days": older_than_days,
            "counts": counts,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "limits": {
                "batch_size": batch_size,
                "max_batches": max_batches,
                "until_empty": until_empty,
                "skip_leases": skip_leases,
                "skip_results": skip_results,
            },
        }
        if error_labels:
            report["error_types"] = error_labels

        if output_format == "json":
            rendered = json.dumps(report, ensure_ascii=True, separators=(",", ":"))
        else:
            lines = [
                f"Execution purge {mode} (older than {older_than_days}d, cutoff {report['cutoff']}): "
                f"{counts['executions_eligible']} executions eligible, "
                f"{counts['executions_deleted']} deleted, "
                f"{counts['executions_skipped']} skipped, "
                f"{counts['executions_failed']} failed.",
                f"Attempts: {counts['attempts_eligible']} eligible, "
                f"{counts['attempts_deleted']} deleted. "
                f"External results: {counts['results_eligible']} eligible, "
                f"{counts['results_deleted']} deleted, "
                f"{counts['results_skipped_shared']} shared-retained, "
                f"{counts['results_failed']} failed.",
                f"Inactive leases: {counts['leases_eligible']} eligible, "
                f"{counts['leases_deleted']} deleted. "
                f"Batches: {counts['batches']}; more remaining: {str(has_more).lower()}.",
            ]
            if error_labels:
                lines.append(f"Error types: {', '.join(error_labels)}")
            if next_cursor is not None:
                lines.append(f"Next cursor: {next_cursor}")
            rendered = "\n".join(lines)

        if len(rendered.encode("utf-8")) > OUTPUT_MAX_BYTES - 256:
            raise CommandError("Execution purge report exceeded its output bound.")
        self.stdout.write(rendered)

        if counts["results_failed"] or counts["executions_failed"]:
            raise CommandError(
                f"Failed to purge {counts['executions_failed']} execution(s) and "
                f"{counts['results_failed']} external result(s)."
            )
        if has_more:
            raise CommandError(
                "Execution purge pass incomplete; pass --cursor or use --until-empty.",
                returncode=2,
            )

    @staticmethod
    def _plan_execution_batch(
        *,
        after_pk: int,
        cutoff: datetime,
        batch_size: int,
        skip_results: bool,
        using: str,
        after_finished_at: datetime | None = None,
    ) -> _ExecutionBatchPlan:
        """Phase 1: collect bounded terminal candidates ordered by (finished_at, pk)."""
        queryset = RayTaskExecution.objects.using(using).filter(
            state__in=TERMINAL_STATES,
            finished_at__isnull=False,
            finished_at__lte=cutoff,
        )
        if after_finished_at is not None:
            queryset = queryset.filter(finished_at__gte=after_finished_at).filter(
                Q(finished_at__gt=after_finished_at)
                | Q(finished_at=after_finished_at, pk__gt=after_pk)
            )
        elif after_pk > 0:
            queryset = queryset.filter(pk__gt=after_pk)
        rows = list(
            queryset.order_by("finished_at", "pk").values_list(
                "pk", "result_reference", "finished_at"
            )[: batch_size + 1]
        )
        has_more = len(rows) > batch_size
        rows = rows[:batch_size]
        if not rows:
            return _ExecutionBatchPlan(
                candidate_pks=(),
                last_pk=after_pk,
                last_finished_at=after_finished_at,
                has_more=False,
                protected_pks=frozenset(),
                attempts_by_execution={},
                result_refs_by_execution={},
                deletable_refs=(),
                shared_refs=frozenset(),
            )

        candidate_pks = tuple(int(pk) for pk, _ref, _finished in rows)
        last_pk = candidate_pks[-1]
        last_finished_at = _normalize_timestamp(rows[-1][2])

        # Skip any execution referenced by on_delete=PROTECT reverse relations
        # without importing dormant protocol models directly.
        protected: set[int] = set()
        for rel in RayTaskExecution._meta.related_objects:
            if getattr(rel, "on_delete", None) is models.PROTECT:
                attname = rel.field.attname
                related_manager: Any = rel.related_model._default_manager
                protected.update(
                    int(pk)
                    for pk in related_manager.using(using)
                    .filter(**{f"{attname}__in": candidate_pks})
                    .values_list(attname, flat=True)
                )
        eligible_pk_set = set(candidate_pks) - protected

        attempts_by_execution: dict[int, int] = {}
        refs_by_execution: dict[int, set[str]] = {}
        for pk, ref, _finished in rows:
            int_pk = int(pk)
            if int_pk not in eligible_pk_set:
                continue
            if not skip_results and _is_external_result_reference(ref):
                assert isinstance(ref, str)
                refs_by_execution.setdefault(int_pk, set()).add(ref)

        if eligible_pk_set:
            for exec_id, attempt_ref in (
                TaskAttempt.objects.using(using)
                .filter(execution_id__in=eligible_pk_set)
                .values_list("execution_id", "result_reference")
            ):
                int_exec_id = int(exec_id)
                attempts_by_execution[int_exec_id] = attempts_by_execution.get(int_exec_id, 0) + 1
                if not skip_results and _is_external_result_reference(attempt_ref):
                    assert isinstance(attempt_ref, str)
                    refs_by_execution.setdefault(int_exec_id, set()).add(attempt_ref)

        all_candidate_refs: set[str] = set()
        for ref_set in refs_by_execution.values():
            all_candidate_refs.update(ref_set)

        shared_refs: set[str] = set()
        if all_candidate_refs:
            shared_refs.update(
                ref
                for ref in (
                    RayTaskExecution.objects.using(using)
                    .filter(result_reference__in=all_candidate_refs)
                    .exclude(pk__in=eligible_pk_set)
                    .values_list("result_reference", flat=True)
                    .distinct()
                )
                if isinstance(ref, str)
            )
            remaining_refs = all_candidate_refs - shared_refs
            if remaining_refs:
                shared_refs.update(
                    ref
                    for ref in (
                        TaskAttempt.objects.using(using)
                        .filter(result_reference__in=remaining_refs)
                        .exclude(execution_id__in=eligible_pk_set)
                        .values_list("result_reference", flat=True)
                        .distinct()
                    )
                    if isinstance(ref, str)
                )

        deletable_refs = tuple(sorted(all_candidate_refs - shared_refs))
        frozen_refs_by_exec = {
            pk: frozenset(refs - shared_refs) for pk, refs in refs_by_execution.items()
        }
        return _ExecutionBatchPlan(
            candidate_pks=candidate_pks,
            last_pk=last_pk,
            last_finished_at=last_finished_at,
            has_more=has_more,
            protected_pks=frozenset(protected),
            attempts_by_execution=attempts_by_execution,
            result_refs_by_execution=frozen_refs_by_exec,
            deletable_refs=deletable_refs,
            shared_refs=frozenset(shared_refs),
        )

    @staticmethod
    def _partition_orphan_result_refs(
        candidate_refs: frozenset[str],
        *,
        using: str,
    ) -> tuple[tuple[str, ...], frozenset[str]]:
        """Post-commit check: return (unreferenced_orphans, still_referenced_shared)."""
        if not candidate_refs:
            return (), frozenset()
        still_referenced: set[str] = set()
        still_referenced.update(
            ref
            for ref in (
                RayTaskExecution.objects.using(using)
                .filter(result_reference__in=candidate_refs)
                .values_list("result_reference", flat=True)
                .distinct()
            )
            if isinstance(ref, str)
        )
        remaining = candidate_refs - still_referenced
        if remaining:
            still_referenced.update(
                ref
                for ref in (
                    TaskAttempt.objects.using(using)
                    .filter(result_reference__in=remaining)
                    .values_list("result_reference", flat=True)
                    .distinct()
                )
                if isinstance(ref, str)
            )
        orphan_refs = tuple(sorted(candidate_refs - still_referenced))
        return orphan_refs, frozenset(still_referenced)

    @staticmethod
    def _is_result_ref_referenced(reference: str, *, using: str) -> bool:
        """Return True if any execution or attempt row still references ``reference``."""
        refs: set[str] = {reference}
        try:
            from django_ray.result_storage import canonicalize_result_reference

            refs.add(canonicalize_result_reference(reference))
        except Exception:
            pass
        if RayTaskExecution.objects.using(using).filter(result_reference__in=refs).exists():
            return True
        return TaskAttempt.objects.using(using).filter(result_reference__in=refs).exists()

    @staticmethod
    def _delete_execution_batch(
        execution_pks: tuple[int, ...],
        *,
        cutoff: datetime,
        using: str,
        skip_results: bool = False,
    ) -> tuple[int, int, int, int, frozenset[str]]:
        """Phase 2: lock, re-verify fences, collect candidate refs, and delete DB rows first."""
        with transaction.atomic(using=using):
            locked_rows = list(
                RayTaskExecution.objects.using(using)
                .select_for_update()
                .filter(
                    pk__in=execution_pks,
                    state__in=TERMINAL_STATES,
                    finished_at__isnull=False,
                    finished_at__lte=cutoff,
                )
                .order_by("pk")
                .values_list("pk", "result_reference")
            )
            confirmed_pks = [int(pk) for pk, _ in locked_rows]
            skipped_changed = len(execution_pks) - len(confirmed_pks)
            if not confirmed_pks:
                return 0, 0, skipped_changed, 0, frozenset()

            candidate_refs: set[str] = set()
            if not skip_results:
                for _pk, exec_ref in locked_rows:
                    if _is_external_result_reference(exec_ref):
                        assert isinstance(exec_ref, str)
                        candidate_refs.add(exec_ref)
                for attempt_ref in (
                    TaskAttempt.objects.using(using)
                    .filter(execution_id__in=confirmed_pks, result_reference__isnull=False)
                    .values_list("result_reference", flat=True)
                ):
                    if _is_external_result_reference(attempt_ref):
                        assert isinstance(attempt_ref, str)
                        candidate_refs.add(attempt_ref)

            # Clear chunked workflow-progress children in RESTRICT-safe order
            # (ManifestPage before TopologyPage) without loading Django's Collector.
            run_storage_ids = list(
                WorkflowProgressRunStorage.objects.using(using)
                .filter(execution_id__in=confirmed_pks)
                .values_list("pk", flat=True)
            )
            if run_storage_ids:
                _delete_queryset_in_chunks(
                    WorkflowProgressNodeDetail.objects.using(using).filter(
                        run_storage_id__in=run_storage_ids
                    ),
                    using=using,
                )
                _delete_queryset_in_chunks(
                    WorkflowProgressTopologyManifestPage.objects.using(using).filter(
                        manifest__run_storage_id__in=run_storage_ids
                    ),
                    using=using,
                )
                _delete_queryset_in_chunks(
                    WorkflowProgressTopologyManifest.objects.using(using).filter(
                        run_storage_id__in=run_storage_ids
                    ),
                    using=using,
                )
                _delete_queryset_in_chunks(
                    WorkflowProgressTopologyPage.objects.using(using).filter(
                        run_storage_id__in=run_storage_ids
                    ),
                    using=using,
                )
                _delete_queryset_in_chunks(
                    WorkflowProgressRunStorage.objects.using(using).filter(pk__in=run_storage_ids),
                    using=using,
                )

            deleted_attempts = _delete_queryset_in_chunks(
                TaskAttempt.objects.using(using).filter(execution_id__in=confirmed_pks),
                using=using,
            )
            try:
                _total, by_model = (
                    RayTaskExecution.objects.using(using).filter(pk__in=confirmed_pks).delete()
                )
            except ProtectedError:
                transaction.set_rollback(True, using=using)
                return 0, 0, skipped_changed, len(confirmed_pks), frozenset()
            deleted_execs = int(by_model.get(RayTaskExecution._meta.label, 0))
            return deleted_execs, deleted_attempts, skipped_changed, 0, frozenset(candidate_refs)

    @staticmethod
    def _inactive_lease_filter(cutoff: datetime) -> Q:
        return Q(is_active=False) & (
            Q(stopped_at__isnull=False, stopped_at__lte=cutoff)
            | Q(stopped_at__isnull=True, last_heartbeat_at__lte=cutoff)
        )

    def _process_lease_batch(
        self,
        *,
        after_lease_id: str,
        cutoff: datetime,
        batch_size: int,
        delete: bool,
        using: str,
    ) -> tuple[int, int, str, bool]:
        """Select and optionally delete one bounded batch of old inactive worker leases."""
        lease_ids = list(
            TaskWorkerLease.objects.using(using)
            .filter(self._inactive_lease_filter(cutoff), worker_id__gt=after_lease_id)
            .order_by("worker_id")
            .values_list("worker_id", flat=True)[: batch_size + 1]
        )
        has_more = len(lease_ids) > batch_size
        lease_ids = lease_ids[:batch_size]
        if not lease_ids:
            return 0, 0, after_lease_id, False

        last_lease_id = str(lease_ids[-1])
        if not delete:
            return len(lease_ids), 0, last_lease_id, has_more

        with transaction.atomic(using=using):
            locked_ids = list(
                TaskWorkerLease.objects.using(using)
                .select_for_update()
                .filter(self._inactive_lease_filter(cutoff), worker_id__in=lease_ids)
                .order_by("worker_id")
                .values_list("worker_id", flat=True)
            )
            if not locked_ids:
                return 0, 0, last_lease_id, has_more
            _total, by_model = (
                TaskWorkerLease.objects.using(using).filter(worker_id__in=locked_ids).delete()
            )
            deleted_leases = int(by_model.get(TaskWorkerLease._meta.label, 0))
            return len(locked_ids), deleted_leases, last_lease_id, has_more
