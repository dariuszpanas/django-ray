"""Bounded maintenance scans, stuck/timeout recovery, cancellation finalization, and lease cleanup."""

from __future__ import annotations

import time as stdlib_time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from django_ray.lifecycle import retry_task as default_retry_task
from django_ray.management.diagnostics import render_console_diagnostic
from django_ray.models import CancellationStatus, TaskState
from django_ray.redaction import materialize_exception_message
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    PreparedRemoteCancellation,
)
from django_ray.runner.cancellation import (
    finalize_cancellation as default_finalize_cancellation,
)
from django_ray.runner.cancellation import (
    prepare_remote_cancellation as default_prepare_remote_cancellation,
)
from django_ray.runner.cancellation import (
    request_remote_cancellation as default_request_remote_cancellation,
)
from django_ray.runner.leasing import live_lease_freshness_q
from django_ray.runner.maintenance import (
    MAINTENANCE_CONTINUATION_SECONDS,
    MaintenanceCandidate,
    MaintenanceScan,
)
from django_ray.runner.reconciliation import (
    is_task_stuck as default_is_task_stuck,
)
from django_ray.runner.reconciliation import (
    is_task_timed_out as default_is_task_timed_out,
)
from django_ray.runner.reconciliation import (
    mark_task_timed_out as default_mark_task_timed_out,
)
from django_ray.runner.retry import resolve_task_contract
from django_ray.runner.retry import should_retry as default_should_retry
from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError
from django_ray.worker import (
    BUILTIN_DATETIME,
    DEFAULT_MARK_TASK_LOST,
    DEFERRED_MAINTENANCE_SCAN_FIELDS,
    command_dep,
    lease_model,
    task_model,
)


def defer_maintenance_scan_fields(queryset: Any) -> Any:
    """Defer heavy JSON/diagnostic columns during bounded maintenance scans."""
    from django.db.models import QuerySet

    if isinstance(queryset, QuerySet):
        return queryset.defer(*DEFERRED_MAINTENANCE_SCAN_FIELDS)
    return queryset


def maintenance_scan_page(
    command: Any,
    kind: str,
    queryset: Any,
    queues: Sequence[str] | None,
    supported_protocols: Any,
) -> Any:
    """Return the next bounded page of candidates for a maintenance scan kind."""
    scan = command._maintenance_scans.setdefault(kind, MaintenanceScan())
    scope = (
        queryset.db,
        None if queues is None else tuple(sorted(set(queues))),
        supported_protocols.minimum,
        supported_protocols.maximum,
    )
    return scan.page(queryset, scope=scope)


def maintenance_deadline(command: Any, kind: str, interval: float) -> float:
    """Compute the next monotonic deadline for a maintenance scan kind."""
    time_mod = command_dep(command, "time", stdlib_time)
    pending = command._maintenance_scans.get(kind)
    continuing = bool(pending and pending.pending)
    if kind == "orphan":
        continuing = continuing or command._active_reconciliation_pending
    return time_mod.monotonic() + (MAINTENANCE_CONTINUATION_SECONDS if continuing else interval)


def maintenance_active_workers(command: Any, candidates: Any) -> set[str]:
    """Advisory liveness for only this page's bounded owner identities."""
    from django_ray.lease_policy import lease_policy_observation

    lease_cls = lease_model(command)
    owners = {candidate.owner for candidate in candidates if candidate.usable and candidate.owner}
    if not owners:
        return set()
    with lease_policy_observation() as policy:
        return set(
            lease_cls.objects.filter(
                live_lease_freshness_q(policy.duration),
                worker_id__in=owners,
                is_active=True,
            ).values_list("worker_id", flat=True)
        )


def _apply_contract_timeout_if_unset(task: Any) -> None:
    """Apply ``@ray_task(timeout_seconds=...)`` when the row lacks an explicit timeout."""
    if getattr(task, "timeout_seconds", None) is not None:
        return
    contract = resolve_task_contract(task)
    if contract is not None and contract.timeout_seconds is not None:
        task.timeout_seconds = contract.timeout_seconds


def _can_prefetch_maintenance_page(command: Any) -> bool:
    """Use batched page prefetch only when ``_maintenance_snapshot`` is unpatched."""
    bound = getattr(command, "_maintenance_snapshot", None)
    default = getattr(type(command), "_maintenance_snapshot", None)
    return bound is not None and getattr(bound, "__func__", None) is default


def _prefetch_maintenance_page(
    command: Any,
    kind: str,
    queryset: Any,
    candidates: Sequence[MaintenanceCandidate],
    active_worker_ids: set[str],
) -> dict[int, Any] | None:
    """Fetch one bounded page of maintenance candidates in a single ``IN`` query."""
    if not _can_prefetch_maintenance_page(command):
        return None
    eligible_pks = [
        candidate.pk
        for candidate in candidates
        if candidate.usable
        and not (
            (
                kind == "orphan"
                and (candidate.owner == command.worker_id or candidate.owner in active_worker_ids)
            )
            or (
                kind != "orphan"
                and candidate.owner != command.worker_id
                and candidate.owner in active_worker_ids
            )
        )
    ]
    if len(eligible_pks) <= 1:
        return None
    selected = command._defer_maintenance_scan_fields(queryset.filter(pk__in=eligible_pks))
    if kind == "running":
        selected = selected.defer("completion_data")
    try:
        return {int(row.pk): row for row in selected}
    except Exception:
        return None


def _candidate_matches_snapshot(task: Any, candidate: MaintenanceCandidate) -> bool:
    """Confirm a prefetched row still matches the exact scanned candidate identity."""
    for field_name, expected in candidate.filters().items():
        if getattr(task, field_name, None) != expected:
            return False
    return True


def maintenance_snapshot(
    command: Any,
    kind: str,
    queryset: Any,
    candidate: MaintenanceCandidate,
) -> Any:
    """Advance the scan cursor and read one fresh candidate row."""
    command._maintenance_scans[kind].advance(candidate)
    if not candidate.usable:
        return None
    batch = getattr(command, "_maintenance_snapshot_batch", None)
    if isinstance(batch, tuple) and len(batch) == 2 and batch[0] == kind and batch[1] is not None:
        task = batch[1].get(candidate.pk)
        if task is None or not _candidate_matches_snapshot(task, candidate):
            return None
        if kind == "running":
            task.completion_data = None
        return task
    selected = command._defer_maintenance_scan_fields(queryset.filter(**candidate.filters()))
    if kind == "running":
        selected = selected.defer("completion_data")
    try:
        task = selected.first()
    except Exception:
        command.logger.warning(
            "Maintenance candidate snapshot could not be read",
            extra={"task_pk": candidate.pk},
        )
        return None
    if task is not None and kind == "running":
        task.completion_data = None
    return task


def request_timeout_cancellation(command: Any, task: Any) -> CancellationOutcome:
    """Stop a timed-out execution by its exact recorded backend identity."""
    return command._request_cancellation_for_task(task)


def detect_stuck_tasks(command: Any, queues: Sequence[str] | None = None) -> int:
    """Detect timed-out or stuck tasks and transition them safely."""
    if command.shutdown_requested:
        return 0

    supported_protocols = command._authoritative_protocol_range_for_scan()
    if supported_protocols is None:
        return 0

    task_cls = task_model(command)
    is_timed_out_fn = command_dep(command, "is_task_timed_out", default_is_task_timed_out)
    is_stuck_fn = command_dep(command, "is_task_stuck", default_is_task_stuck)
    mark_timed_out_fn = command_dep(command, "mark_task_timed_out", default_mark_task_timed_out)
    mark_lost_fn = command_dep(command, "mark_task_lost", DEFAULT_MARK_TASK_LOST)
    should_retry_fn = command_dep(command, "should_retry", default_should_retry)
    retry_task_fn = command_dep(command, "retry_task", default_retry_task)

    running_tasks = command._defer_maintenance_scan_fields(
        task_cls.objects.filter(
            state=TaskState.RUNNING,
            completion_data__isnull=True,
            execution_protocol_version__gte=supported_protocols.minimum,
            execution_protocol_version__lte=supported_protocols.maximum,
        )
    )
    if queues is not None:
        running_tasks = running_tasks.filter(queue_name__in=queues)

    candidates = command._maintenance_scan_page(
        "running", running_tasks, queues, supported_protocols
    )
    active_worker_ids = command._maintenance_active_workers(candidates)
    stuck_count = 0
    timeout_count = 0
    orphan_recovered_count = 0
    prefetched = _prefetch_maintenance_page(
        command, "running", running_tasks, candidates, active_worker_ids
    )
    previous_batch = getattr(command, "_maintenance_snapshot_batch", None)
    command._maintenance_snapshot_batch = ("running", prefetched)
    try:
        for candidate in candidates:
            if command.shutdown_requested:
                break
            if candidate.owner != command.worker_id and candidate.owner in active_worker_ids:
                command._maintenance_scans["running"].advance(candidate)
                continue
            task = command._maintenance_snapshot("running", running_tasks, candidate)
            if task is None:
                continue
            _apply_contract_timeout_if_unset(task)
            task_worker_id = str(task.claimed_by_worker) if task.claimed_by_worker else None
            claimed_by_this_worker = task_worker_id == command.worker_id
            claimed_by_active_worker = bool(task_worker_id) and task_worker_id in active_worker_ids
            pending_ray_core_handle = (
                command.ray_core_runner.get_pending_handle(
                    task.pk,
                    attempt_number=int(task.attempt_number),
                    execution_generation=int(task.execution_generation),
                )
                if command.ray_core_runner is not None and claimed_by_this_worker
                else None
            )
            pending_ray_core_identity_matches = bool(
                pending_ray_core_handle
                and task.attempt_number == pending_ray_core_handle.attempt_number
                and task.execution_generation == pending_ray_core_handle.execution_generation
            )
            active_ray_job_identity_matches = bool(
                command.active_tasks.get(task.pk) == str(task.ray_job_id or "")
                and command.active_task_identities.get(task.pk)
                == (int(task.attempt_number), int(task.execution_generation))
            )

            if task.completion_data is not None:
                continue

            if not claimed_by_this_worker and claimed_by_active_worker:
                continue

            if is_timed_out_fn(task):
                marked_timed_out = False
                adopted = False
                current: Any = None
                prep_key = command._prepare_remote_cancellation_for_task(task)
                try:
                    # Hold the authoritative execution row lock across remote stop
                    # and terminal write so concurrent recoverers serialize cleanly.
                    with command._authoritative_task_owner(
                        task,
                        expected_state=TaskState.RUNNING,
                        allow_takeover=True,
                        require_completion_data_match=True,
                    ) as owned:
                        if owned is None:
                            if command.shutdown_requested:
                                break
                            continue
                        current = owned.execution
                        _apply_contract_timeout_if_unset(current)
                        if current.completion_data is not None or not is_timed_out_fn(current):
                            continue
                        adopted = owned.adopted
                        command.stdout.write(
                            command.style.WARNING(
                                f"\nTask {current.pk} timed out after {current.timeout_seconds}s"
                            )
                        )
                        cancellation = command._request_timeout_cancellation(current)
                        marked_timed_out = mark_timed_out_fn(
                            current,
                            cancellation_status=CancellationStatus(cancellation.status.value),
                            cancellation_error=cancellation.message,
                            expected_ray_job_id=(
                                str(current.ray_job_id) if current.ray_job_id else None
                            ),
                            expected_claimed_by_worker=command.worker_id,
                            expected_attempt_number=current.attempt_number,
                            expected_execution_generation=current.execution_generation,
                            expected_completion_data=None,
                            require_completion_data_match=True,
                            supported_protocols=owned.supported_protocols,
                        )
                finally:
                    if prep_key is not None:
                        command._prepared_cancellation_cache.pop(prep_key, None)
                if command.shutdown_requested:
                    break
                if marked_timed_out:
                    assert current is not None
                    task.__dict__.update(current.__dict__)
                    if task.pk in command.active_tasks:
                        del command.active_tasks[task.pk]
                        command.active_task_identities.pop(task.pk, None)
                    timeout_count += 1
                    if adopted:
                        orphan_recovered_count += 1
                else:
                    command.stdout.write(
                        command.style.NOTICE(
                            f"\nTask {task.pk} completed while timeout cancellation was in flight"
                        )
                    )
                continue

            if str(task.ray_job_id or "").startswith("raysubmit_"):
                continue

            if claimed_by_this_worker and (
                pending_ray_core_identity_matches or active_ray_job_identity_matches
            ):
                continue

            if is_stuck_fn(task):
                recovered_stuck = False
                adopted = False
                current = None
                retried: Any = None
                retry_decision: Any = None
                with command._authoritative_task_owner(
                    task,
                    expected_state=TaskState.RUNNING,
                    allow_takeover=True,
                    require_completion_data_match=True,
                ) as owned:
                    if owned is None:
                        if command.shutdown_requested:
                            break
                        continue
                    current = owned.execution
                    if current.completion_data is not None or not is_stuck_fn(current):
                        continue
                    adopted = owned.adopted

                    if not adopted:
                        command.stdout.write(
                            command.style.WARNING(
                                f"\nTask {current.pk} appears stuck, marking as LOST"
                            )
                        )
                    else:
                        owner = task_worker_id or "unknown-worker"
                        command.stdout.write(
                            command.style.WARNING(
                                f"\nTask {current.pk} from inactive worker {owner} "
                                "appears stuck, marking as LOST"
                            )
                        )

                    lost_marked = (
                        mark_lost_fn(
                            current,
                            supported_protocols=owned.supported_protocols,
                        )
                        if mark_lost_fn is DEFAULT_MARK_TASK_LOST
                        else mark_lost_fn(current)
                    )
                    if not lost_marked:
                        continue
                    recovered_stuck = True

                    retry_decision = should_retry_fn(current, exception_type="TaskLost")
                    if retry_decision.should_retry:
                        try:
                            retried = retry_task_fn(
                                current.pk,
                                allowed_states=(TaskState.LOST,),
                                next_attempt_at=retry_decision.next_attempt_at,
                                expected_attempt_number=current.attempt_number,
                                expected_execution_generation=current.execution_generation,
                            )
                        except RuntimeEnvSnapshotError as error:
                            diagnostic = render_console_diagnostic(error)
                            command.stdout.write(
                                command.style.ERROR(f"  Automatic retry blocked: {diagnostic}")
                            )

                if command.shutdown_requested:
                    break
                if not recovered_stuck:
                    continue
                if retried is not None:
                    task = retried
                    assert retry_decision is not None
                    command.stdout.write(
                        command.style.NOTICE(
                            f"  Scheduling retry #{task.attempt_number} "
                            f"at {retry_decision.next_attempt_at}"
                        )
                    )
                elif current is not None:
                    task.__dict__.update(current.__dict__)

                stuck_count += 1
                if adopted:
                    orphan_recovered_count += 1
    finally:
        command._maintenance_snapshot_batch = previous_batch

    if stuck_count > 0:
        command.stdout.write(command.style.WARNING(f"Detected {stuck_count} stuck task(s)"))
    if timeout_count > 0:
        command.stdout.write(command.style.WARNING(f"Detected {timeout_count} timed out task(s)"))
    if orphan_recovered_count > 0:
        command.stdout.write(
            command.style.NOTICE(
                f"Recovered {orphan_recovered_count} task(s) from inactive or missing workers"
            )
        )

    return stuck_count + timeout_count


def cleanup_expired_leases(command: Any) -> int:
    """Clean up expired worker leases from other workers."""
    from django_ray.runner.leasing import cleanup_expired_leases as run_lease_cleanup

    try:
        deleted_count = run_lease_cleanup()
        if deleted_count > 0:
            command.stdout.write(
                command.style.NOTICE(f"\nCleaned up {deleted_count} expired worker lease(s)")
            )
        return deleted_count
    except Exception:
        command.logger.warning("Failed to cleanup expired leases", exc_info=True)
        return 0


def claim_orphaned_cancellation(command: Any, task: Any, *, now: datetime) -> bool:
    """Fence a stale owner before taking over its cancellation."""
    current = command._take_over_task_if_owner_stale(task, now=now)
    return current is not None and current.state == TaskState.CANCELLING


def prepare_remote_cancellation_for_task(command: Any, task: Any) -> tuple[str, str] | None:
    """Resolve a Ray Job cancellation client outside DB row locks."""
    ray_job_id = str(task.ray_job_id or "")
    if not ray_job_id.startswith("raysubmit_"):
        return None
    if not command._preflight_own_lease_active(mark_lost=False):
        return None
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    prepare_cancel_fn = command_dep(
        command, "prepare_remote_cancellation", default_prepare_remote_cancellation
    )

    ray_address = str(task.ray_address or "")
    key = (ray_job_id, ray_address)
    handle = SubmissionHandle(
        ray_job_id=ray_job_id,
        ray_address=ray_address,
        submitted_at=task.started_at or dt.now(UTC),
    )
    try:
        from django_ray.runner.ray_job import RayJobRunner

        runner = RayJobRunner()
    except Exception as exc:
        command._prepared_cancellation_cache[key] = (
            None,
            PreparedRemoteCancellation(
                supported=True,
                error=CancellationOutcome(
                    CancellationOutcomeStatus.INDETERMINATE,
                    f"Could not cancel Ray Job: {materialize_exception_message(exc)}",
                ),
            ),
        )
        return key
    prepared = prepare_cancel_fn(runner, handle)
    command._prepared_cancellation_cache[key] = (runner, prepared)
    return key


def request_cancellation_for_task(command: Any, task: Any) -> CancellationOutcome:
    """Best-effort cancellation using the backend recorded on the task."""
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    request_cancel_fn = command_dep(
        command, "request_remote_cancellation", default_request_remote_cancellation
    )

    ray_job_id = str(task.ray_job_id or "")
    ray_address = str(task.ray_address or "")
    handle = SubmissionHandle(
        ray_job_id=ray_job_id or f"ray_core:{task.pk}",
        ray_address=ray_address,
        submitted_at=task.started_at or dt.now(UTC),
    )

    if ray_job_id.startswith("raysubmit_"):
        cached = command._prepared_cancellation_cache.pop((ray_job_id, ray_address), None)
        if cached is not None:
            cached_runner, prepared = cached
            if prepared.error is not None:
                return prepared.error
            return request_cancel_fn(cached_runner, handle, prepared=prepared)
        try:
            from django_ray.runner.ray_job import RayJobRunner

            return request_cancel_fn(RayJobRunner(), handle)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Could not cancel Ray Job: {materialize_exception_message(exc)}",
            )

    if command.ray_core_runner is not None:
        pending_handle = command.ray_core_runner.get_pending_handle(
            task.pk,
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )
    else:
        pending_handle = None

    if pending_handle is not None:
        try:
            assert command.ray_core_runner is not None
            return command.ray_core_runner.cancel_pending_with_status(pending_handle)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Could not cancel Ray Core task: {materialize_exception_message(exc)}",
            )

    if ray_job_id:
        existing_status = getattr(task, "cancellation_status", None)
        if existing_status in (
            CancellationStatus.REQUESTED,
            CancellationStatus.FAILED,
            CancellationStatus.NOT_APPLICABLE,
        ):
            return CancellationOutcome(
                CancellationOutcomeStatus(str(existing_status)),
                getattr(task, "cancellation_error", None),
            )
        return CancellationOutcome(
            CancellationOutcomeStatus.INDETERMINATE,
            "Exact Ray Core handle unavailable while recovering cancellation",
        )

    return CancellationOutcome(CancellationOutcomeStatus.NOT_APPLICABLE)


def process_cancellations(command: Any, queues: Sequence[str] | None = None) -> int:
    """Adopt cancellations in selected queues, or all queues when omitted."""
    if command.shutdown_requested:
        return 0
    supported_protocols = command._authoritative_protocol_range_for_scan()
    if supported_protocols is None:
        return 0

    task_cls = task_model(command)
    finalize_cancel_fn = command_dep(
        command, "finalize_cancellation", default_finalize_cancellation
    )

    cancelling_tasks = command._defer_maintenance_scan_fields(
        task_cls.objects.filter(
            state=TaskState.CANCELLING,
            execution_protocol_version__gte=supported_protocols.minimum,
            execution_protocol_version__lte=supported_protocols.maximum,
        )
    )
    if queues is not None:
        cancelling_tasks = cancelling_tasks.filter(queue_name__in=queues)
    finalized_count = 0

    candidates = command._maintenance_scan_page(
        "cancelling", cancelling_tasks, queues, supported_protocols
    )
    active_worker_ids = command._maintenance_active_workers(candidates)
    prefetched = _prefetch_maintenance_page(
        command, "cancelling", cancelling_tasks, candidates, active_worker_ids
    )
    previous_batch = getattr(command, "_maintenance_snapshot_batch", None)
    command._maintenance_snapshot_batch = ("cancelling", prefetched)
    try:
        for candidate in candidates:
            if command.shutdown_requested:
                break
            if candidate.owner != command.worker_id and candidate.owner in active_worker_ids:
                command._maintenance_scans["cancelling"].advance(candidate)
                continue
            task = command._maintenance_snapshot("cancelling", cancelling_tasks, candidate)
            if task is None:
                continue
            finalized = False
            current: Any = None
            prep_key = command._prepare_remote_cancellation_for_task(task)
            try:
                # Hold the authoritative execution row lock across remote stop
                # and cancellation finalization so concurrent recoverers serialize.
                with command._authoritative_task_owner(
                    task,
                    expected_state=TaskState.CANCELLING,
                    allow_takeover=True,
                    require_completion_data_match=True,
                ) as owned:
                    if owned is None:
                        if command.shutdown_requested:
                            break
                        continue
                    current = owned.execution
                    command.stdout.write(
                        command.style.WARNING(f"\nFinalizing cancellation for task {current.pk}")
                    )
                    cancellation = command._request_cancellation_for_task(current)
                    finalized = finalize_cancel_fn(
                        current,
                        expected_worker_id=command.worker_id,
                        expected_attempt_number=current.attempt_number,
                        expected_execution_generation=current.execution_generation,
                        cancellation_status=CancellationStatus(cancellation.status.value),
                        cancellation_error=cancellation.message,
                        supported_protocols=owned.supported_protocols,
                    )
            finally:
                if prep_key is not None:
                    command._prepared_cancellation_cache.pop(prep_key, None)
            if command.shutdown_requested:
                break
            if finalized:
                assert current is not None
                task.__dict__.update(current.__dict__)
                command.active_tasks.pop(task.pk, None)
                command.active_task_identities.pop(task.pk, None)
                finalized_count += 1
                command.stdout.write(command.style.SUCCESS(f"  Task {task.pk} cancelled"))
    finally:
        command._maintenance_snapshot_batch = previous_batch

    return finalized_count
