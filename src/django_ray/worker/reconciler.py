"""Completion decoding, Ray Core polling, and Ray Job reconciliation."""

from __future__ import annotations

import time as stdlib_time
from collections.abc import Sequence
from datetime import UTC, datetime
from heapq import nsmallest
from typing import Any

from django.db import DatabaseError, InterfaceError, transaction
from django.db.models import Q

from django_ray.execution_codec import (
    ExecutionCompletionDecodeError,
    ExecutionCompletionRejection,
    ExecutionCompletionSource,
    ExecutionIdentity,
)
from django_ray.execution_codec import (
    decode_execution_completion as default_decode_execution_completion,
)
from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE, ExecutionProtocolRange
from django_ray.lifecycle import (
    cancel_task as default_cancel_task,
)
from django_ray.lifecycle import (
    record_lost as default_record_lost,
)
from django_ray.management.diagnostics import render_console_diagnostic
from django_ray.models import CancellationStatus, TaskState
from django_ray.ray_job_protocol import (
    RayJobRequestBindingError,
    RayJobRequestExpectation,
    RayJobRequestReferenceExpectation,
    is_rq2_ray_job_submission_id,
    is_strict_ray_job_submission_id,
    is_valid_strict_ray_job_submission_id,
    parse_ray_job_request_metadata,
    ray_job_metadata_has_strict_marker,
    validate_ray_job_request_expectation,
    validate_ray_job_request_reference_expectation,
)
from django_ray.redaction import materialize_exception_message
from django_ray.runner.cancellation import CancellationOutcome
from django_ray.runner.cancellation import (
    prepare_remote_cancellation as default_prepare_remote_cancellation,
)
from django_ray.runner.cancellation import (
    request_remote_cancellation as default_request_remote_cancellation,
)
from django_ray.runner.maintenance import MAINTENANCE_PAGE_SIZE
from django_ray.runner.ray_core import RayCoreHandle, RayCoreRunner
from django_ray.runner.reconciliation import (
    get_stuck_timeout as default_get_stuck_timeout,
)
from django_ray.runner.reconciliation import (
    is_task_stuck as default_is_task_stuck,
)
from django_ray.runner.reconciliation import (
    is_task_timed_out as default_is_task_timed_out,
)
from django_ray.worker import (
    BUILTIN_DATETIME,
    InspectedExecutionCompletion,
    close_old_db_connections,
    command_dep,
    is_transient_db_error,
    task_model,
)


def mark_task_monitor_heartbeat(
    command: Any,
    task: Any,
    *,
    now: datetime | None = None,
    ray_job_id: str | None = None,
    expected_worker_id: str | None = None,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> None:
    """Record that a running task is still being actively monitored."""
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    task_cls = task_model(command)

    heartbeat_time = now or dt.now(UTC)
    filters: dict[str, Any] = {"pk": task.pk, "state": TaskState.RUNNING}
    if ray_job_id is not None:
        filters["ray_job_id"] = ray_job_id
    if expected_worker_id is not None:
        filters["claimed_by_worker"] = expected_worker_id
    if attempt_number is not None:
        filters["attempt_number"] = attempt_number
    if execution_generation is not None:
        filters["execution_generation"] = execution_generation
    updated = task_cls.objects.filter(**filters).update(last_heartbeat_at=heartbeat_time)
    if updated:
        task.last_heartbeat_at = heartbeat_time


def partition_ray_core_handles(
    command: Any,
    runner: RayCoreRunner,
    handles: tuple[RayCoreHandle, ...],
    *,
    heartbeat_at: datetime | None = None,
) -> tuple[RayCoreHandle, ...] | None:
    """Authorize exact local handles and retire every ineligible capability."""
    if not handles:
        return ()

    task_cls = task_model(command)
    eligible_handles: list[RayCoreHandle] = []
    with transaction.atomic():
        lease = command._lock_authoritative_leases(
            source_worker_id=command.worker_id,
            allow_takeover=False,
            renew_heartbeat=False,
        )
        if lease is None:
            return None
        supported_protocols = command._explicit_protocol_range(lease)
        if supported_protocols is None:
            return None

        rows_by_pk = {
            int(row.pk): row
            for row in task_cls.objects.filter(pk__in=[handle.task_pk for handle in handles]).only(
                "pk",
                "state",
                "claimed_by_worker",
                "attempt_number",
                "execution_generation",
                "execution_protocol_version",
            )
        }
        heartbeat_groups: dict[tuple[int, int, int], list[int]] = {}
        for handle in handles:
            row = rows_by_pk.get(handle.task_pk)
            if (
                row is None
                or str(row.claimed_by_worker or "") != command.worker_id
                or row.state not in (TaskState.RUNNING, TaskState.CANCELLING)
                or int(row.attempt_number) != handle.attempt_number
                or int(row.execution_generation) != handle.execution_generation
                or not supported_protocols.supports(int(row.execution_protocol_version))
            ):
                continue
            eligible_handles.append(handle)
            if heartbeat_at is not None and row.state == TaskState.RUNNING:
                identity = (
                    handle.attempt_number,
                    handle.execution_generation,
                    int(row.execution_protocol_version),
                )
                heartbeat_groups.setdefault(identity, []).append(handle.task_pk)

        for (
            attempt_number,
            execution_generation,
            execution_protocol_version,
        ), task_ids in heartbeat_groups.items():
            task_cls.objects.filter(
                pk__in=task_ids,
                state=TaskState.RUNNING,
                claimed_by_worker=command.worker_id,
                attempt_number=attempt_number,
                execution_generation=execution_generation,
                execution_protocol_version=execution_protocol_version,
            ).update(last_heartbeat_at=heartbeat_at)

    eligible_handle_ids = {id(handle) for handle in eligible_handles}
    retired = 0
    for handle in handles:
        if id(handle) not in eligible_handle_ids:
            retired += int(runner.retire_pending_handle(handle))
    if retired:
        command.stdout.write(
            command.style.NOTICE(f"\n  Retired {retired} stale or unsupported Ray Core handle(s)")
        )
    return tuple(eligible_handles)


def completion_envelope_grace_expired(
    command: Any,
    task: Any,
    *,
    now: datetime,
) -> bool:
    """Return whether a terminal Ray Job has waited too long for its envelope."""
    get_timeout_fn = command_dep(command, "get_stuck_timeout", default_get_stuck_timeout)
    last_activity = task.last_heartbeat_at or task.started_at or task.created_at
    last_activity_dt: datetime = last_activity
    return now - last_activity_dt > get_timeout_fn()


def execution_completion_identity(task: Any) -> ExecutionIdentity:
    """Return the exact durable identity a completion must echo."""
    assert task.pk is not None
    return ExecutionIdentity(
        task_execution_pk=int(task.pk),
        task_id=str(task.task_id),
        attempt_number=int(task.attempt_number),
        execution_generation=int(task.execution_generation),
    )


def inspect_execution_completion(
    target: Any,
    serialized: object,
    task: Any,
    *,
    supported_protocols: ExecutionProtocolRange,
) -> InspectedExecutionCompletion:
    """Decode one exact completion before any result or reference side effect."""
    decode_fn = command_dep(
        target, "decode_execution_completion", default_decode_execution_completion
    )
    try:
        decoded = decode_fn(
            serialized,
            expected_identity=execution_completion_identity(task),
            expected_execution_protocol_version=int(task.execution_protocol_version),
            supported_protocols=supported_protocols,
        )
    except ExecutionCompletionDecodeError as error:
        return InspectedExecutionCompletion(
            decoded=None,
            prepared_result_reference=None,
            rejection=error.classification,
            requires_nonretryable_disposition=error.requires_nonretryable_disposition,
        )

    prepared_result_reference: str | None = None
    result_reference = decoded.completion.result_reference
    if decoded.completion.success and result_reference is not None:
        from django_ray.result_storage import (
            ResultStorageError,
            canonicalize_result_reference,
        )

        try:
            prepared_result_reference = canonicalize_result_reference(result_reference)
        except ResultStorageError:
            versioned = decoded.source is ExecutionCompletionSource.ACCEPTED_VERSIONED_V1
            return InspectedExecutionCompletion(
                decoded=None,
                prepared_result_reference=None,
                rejection=(
                    ExecutionCompletionRejection.INVALID_VERSIONED
                    if versioned
                    else ExecutionCompletionRejection.MALFORMED_LEGACY
                ),
                requires_nonretryable_disposition=versioned,
            )

    return InspectedExecutionCompletion(
        decoded=decoded,
        prepared_result_reference=prepared_result_reference,
        rejection=None,
        requires_nonretryable_disposition=False,
    )


def completion_rejection_policy(
    inspection: InspectedExecutionCompletion,
) -> tuple[str, str, bool | None]:
    """Map one fixed codec rejection to bounded lifecycle policy."""
    assert inspection.rejection is not None
    code = inspection.rejection.value
    if inspection.requires_nonretryable_disposition:
        return (
            f"Execution completion rejected ({code})",
            "RayCompletionIncompatible",
            False,
        )
    return (
        f"Legacy execution completion rejected ({code})",
        "RayCompletionMalformed",
        None,
    )


def mark_stale_ray_core_tasks_as_lost(command: Any) -> None:
    """Route stale Ray Core references through the normal retry/failure path."""
    if not command.ray_core_runner or command.ray_core_runner.pending_count == 0:
        return

    runner = command.ray_core_runner
    pending_handles = runner.pending_task_handles
    eligible_handles = command._partition_ray_core_handles(runner, pending_handles)
    if eligible_handles is None:
        return
    command._terminalize_lost_ray_core_handles(
        runner,
        eligible_handles,
        error_message="Ray connection lost - task state unknown",
    )


def terminalize_lost_ray_core_handles(
    command: Any,
    runner: RayCoreRunner,
    handles: tuple[RayCoreHandle, ...],
    *,
    error_message: str,
) -> int:
    """Terminalize authorized lost handles under the central lease fence."""
    task_cls = task_model(command)
    count = 0
    current_handles = {handle.task_pk: handle for handle in runner.pending_task_handles}
    for handle in handles:
        if current_handles.get(handle.task_pk) is not handle:
            continue
        try:
            task = task_cls.objects.get(pk=handle.task_pk)
        except task_cls.DoesNotExist:
            runner.retire_pending_handle(handle)
            continue

        if (
            task.state != TaskState.RUNNING
            or int(task.attempt_number) != handle.attempt_number
            or int(task.execution_generation) != handle.execution_generation
        ):
            runner.retire_pending_handle(handle)
            continue

        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is not None:
                current = owned.execution
                handled = command._handle_task_failure(
                    current,
                    error_message=error_message,
                    exception_type="RayConnectionError",
                    expected_claimed_by_worker=command.worker_id,
                    expected_attempt_number=handle.attempt_number,
                    expected_execution_generation=handle.execution_generation,
                    expected_completion_data=current.completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                )
                if handled:
                    count += 1

        runner.retire_pending_handle(handle)

    if count:
        command.stdout.write(
            command.style.WARNING(
                f"  Routed {count} stale Ray Core task(s) through lifecycle handling"
            )
        )
    return count


def finalize_ray_core_cancellation(
    command: Any,
    *,
    task_pk: int,
    attempt_number: int,
    execution_generation: int,
) -> bool:
    """Finalize one exact cancellation after a Ray Core completion."""
    task_cls = task_model(command)
    cancel_task_fn = command_dep(command, "cancel_task", default_cancel_task)

    task = (
        task_cls.objects.filter(
            pk=task_pk,
            state=TaskState.CANCELLING,
            claimed_by_worker=command.worker_id,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
        )
        .only(
            "pk",
            "state",
            "claimed_by_worker",
            "ray_job_id",
            "ray_address",
            "attempt_number",
            "execution_generation",
            "execution_protocol_version",
            "started_at",
            "last_heartbeat_at",
            "completion_data",
        )
        .first()
    )
    if task is None:
        return False

    with command._authoritative_task_owner(
        task,
        expected_state=TaskState.CANCELLING,
        allow_takeover=False,
        require_completion_data_match=True,
    ) as owned:
        if owned is None:
            return False
        current = owned.execution
        return cancel_task_fn(
            current,
            expected_worker_id=command.worker_id,
            expected_attempt_number=attempt_number,
            expected_execution_generation=execution_generation,
            expected_completion_data=current.completion_data,
            require_completion_data_match=True,
            supported_protocols=owned.supported_protocols,
        )


def apply_ray_core_completion(command: Any, completion: Any) -> bool:
    """Persist one Ray Core completion and return whether it may be acknowledged."""
    task_cls = task_model(command)
    task_pk = completion.task_pk
    attempt_number = completion.attempt_number
    execution_generation = completion.execution_generation
    try:
        task = task_cls.objects.get(pk=task_pk)

        if (
            task.attempt_number != attempt_number
            or task.execution_generation != execution_generation
        ):
            command.stdout.write(
                command.style.NOTICE(
                    f"\n  Ignoring stale Ray Core result for task {task.pk} "
                    f"attempt {attempt_number}, generation {execution_generation}"
                )
            )
            return True

        if task.state in (TaskState.CANCELLED, TaskState.CANCELLING):
            if task.state == TaskState.CANCELLING:
                command._finalize_ray_core_cancellation(
                    task_pk=task_pk,
                    attempt_number=attempt_number,
                    execution_generation=execution_generation,
                )
            command.stdout.write(command.style.WARNING(f"\n  Task {task.pk} was cancelled"))
            return True

        if task.state != TaskState.RUNNING:
            command.stdout.write(
                command.style.NOTICE(
                    f"\n  Ignoring Ray Core result for task {task.pk} in state {task.state}"
                )
            )
            return True

        pre_inspection = command._inspect_execution_completion(
            completion.result_json,
            task,
            supported_protocols=SUPPORTED_EXECUTION_PROTOCOL_RANGE,
        )
        pre_stored = False
        pre_storage_attempted = False
        pre_result_data: str | None = None
        pre_result_reference: str | None = pre_inspection.prepared_result_reference
        pre_diagnostics: tuple[str, ...] = ()
        pre_storage_error: Exception | None = None
        if pre_inspection.decoded is not None and pre_inspection.decoded.completion.success:
            (
                eligible,
                pre_stored,
                pre_result_data,
                pre_result_reference,
                pre_diagnostics,
                pre_storage_error,
            ) = command._prepare_result_outside_lock(
                task,
                pre_inspection.decoded.completion.result,
                prepared_result_reference=pre_inspection.prepared_result_reference,
                expected_claimed_by_worker=command.worker_id,
                expected_attempt_number=attempt_number,
                expected_execution_generation=execution_generation,
                expected_completion_data=task.completion_data,
                require_completion_data_match=True,
            )
            if not eligible:
                if not command.lease_ownership_lost:
                    cancelled = command._finalize_ray_core_cancellation(
                        task_pk=task_pk,
                        attempt_number=attempt_number,
                        execution_generation=execution_generation,
                    )
                    if cancelled:
                        command.stdout.write(
                            command.style.WARNING(f"\n  Task {task.pk} was cancelled")
                        )
                        return True
                command.stdout.write(
                    command.style.NOTICE(
                        f"\n  Task {task.pk} changed before its Ray Core result could be applied"
                    )
                )
                return True
            pre_storage_attempted = pre_stored or pre_storage_error is not None

        completion_succeeded = False
        persisted = False
        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                if not command.lease_ownership_lost:
                    cancelled = command._finalize_ray_core_cancellation(
                        task_pk=task_pk,
                        attempt_number=attempt_number,
                        execution_generation=execution_generation,
                    )
                    if cancelled:
                        command.stdout.write(
                            command.style.WARNING(f"\n  Task {task.pk} was cancelled")
                        )
                        return True
                if pre_storage_attempted:
                    command.stdout.write(
                        command.style.NOTICE(
                            f"\n  Task {task.pk} changed while its Ray Core "
                            "result was being applied"
                        )
                    )
                else:
                    command.stdout.write(
                        command.style.NOTICE(
                            f"\n  Task {task.pk} changed before its Ray Core "
                            "result could be applied"
                        )
                    )
                return True

            current = owned.execution
            inspection = command._inspect_execution_completion(
                completion.result_json,
                current,
                supported_protocols=owned.supported_protocols,
            )
            decoded = inspection.decoded
            if decoded is None:
                error_message, exception_type, retryable = command._completion_rejection_policy(
                    inspection
                )
                persisted = command._handle_task_failure(
                    current,
                    error_message=error_message,
                    exception_type=exception_type,
                    retryable=retryable,
                    expected_claimed_by_worker=command.worker_id,
                    expected_attempt_number=attempt_number,
                    expected_execution_generation=execution_generation,
                    expected_completion_data=current.completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                )
            elif decoded.completion.success:
                accepted_completion = decoded.completion
                if pre_storage_error is not None:
                    persisted = command._handle_task_failure(
                        current,
                        error_message=(
                            "Failed to persist task result to external storage: "
                            f"{materialize_exception_message(pre_storage_error)}"
                        ),
                        exception_type="ResultStoragePersistenceError",
                        retryable=False,
                        expected_claimed_by_worker=command.worker_id,
                        expected_attempt_number=attempt_number,
                        expected_execution_generation=execution_generation,
                        expected_completion_data=current.completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=(
                            accepted_completion.executor_django_ray_version
                        ),
                    )
                else:
                    completion_succeeded = True
                    persisted = command._store_and_succeed_task(
                        current,
                        accepted_completion.result,
                        prepared_result_data=pre_result_data if pre_stored else None,
                        prepared_result_reference=(
                            pre_result_reference
                            if pre_stored
                            else inspection.prepared_result_reference
                        ),
                        _result_already_stored=pre_stored,
                        _storage_diagnostics=pre_diagnostics,
                        _already_locked=True,
                        expected_claimed_by_worker=command.worker_id,
                        expected_attempt_number=attempt_number,
                        expected_execution_generation=execution_generation,
                        expected_completion_data=current.completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=(
                            accepted_completion.executor_django_ray_version
                        ),
                    )
            else:
                accepted_completion = decoded.completion
                persisted = command._handle_task_failure(
                    current,
                    error_message=accepted_completion.error or "Unknown error",
                    error_traceback=accepted_completion.traceback,
                    exception_type=accepted_completion.exception_type,
                    retryable=accepted_completion.retryable,
                    expected_claimed_by_worker=command.worker_id,
                    expected_attempt_number=attempt_number,
                    expected_execution_generation=execution_generation,
                    expected_completion_data=current.completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                    executor_django_ray_version=accepted_completion.executor_django_ray_version,
                )

        if not persisted:
            command.stdout.write(
                command.style.NOTICE(
                    f"\n  Task {task.pk} changed while its Ray Core result was being applied"
                )
            )
        elif completion_succeeded:
            command.stdout.write(command.style.SUCCESS(f"\n  Task {task.pk} completed"))
        return True

    except task_cls.DoesNotExist:
        command.stdout.write(command.style.WARNING(f"\n  Task {task_pk} not found in database"))
        return True
    except (DatabaseError, InterfaceError) as e:
        close_old_db_connections(command)
        diagnostic = render_console_diagnostic(e)
        command.stdout.write(
            command.style.ERROR(f"\n  Database error processing task {task_pk}: {diagnostic}")
        )
        return False
    except Exception as e:
        if is_transient_db_error(e):
            close_old_db_connections(command)
            diagnostic = render_console_diagnostic(e)
            command.stdout.write(
                command.style.ERROR(
                    f"\n  Transient lease/database error processing task {task_pk}: {diagnostic}"
                )
            )
            return False
        diagnostic = render_console_diagnostic(e)
        command.stdout.write(
            command.style.ERROR(f"\n  Error processing task {task_pk} result: {diagnostic}")
        )
        return True


def poll_ray_core_tasks(command: Any) -> int:
    """Poll for completed Ray Core tasks and update their status."""
    if command.ray_core_runner is None or command.ray_core_runner.pending_count == 0:
        command._last_partitioned_handles = ()
        return 0

    time_mod = command_dep(command, "time", stdlib_time)
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)

    runner = command.ray_core_runner
    monitored_handles = runner.pending_task_handles
    monitor_time = time_mod.monotonic()
    heartbeat_due = (
        command.last_task_monitor_heartbeat == 0.0
        or (monitor_time - command.last_task_monitor_heartbeat)
        >= command.task_monitor_heartbeat_interval
    )
    needs_partition = (
        heartbeat_due
        or monitored_handles != command._last_partitioned_handles
        or command._last_ray_core_authority_check == 0.0
        or (monitor_time - command._last_ray_core_authority_check)
        >= command.task_monitor_heartbeat_interval
    )

    retired_count = 0
    if needs_partition:
        eligible_handles = command._partition_ray_core_handles(
            runner,
            monitored_handles,
            heartbeat_at=dt.now(UTC) if heartbeat_due else None,
        )
        if eligible_handles is None:
            return 0
        retired_count = len(monitored_handles) - len(eligible_handles)
        command._last_partitioned_handles = eligible_handles
        command._last_ray_core_authority_check = monitor_time
        if heartbeat_due:
            command.last_task_monitor_heartbeat = monitor_time
    else:
        eligible_handles = monitored_handles

    if not eligible_handles:
        return retired_count

    import ray

    if not ray.is_initialized():
        command.stdout.write(command.style.WARNING("\nRay disconnected, retiring pending tasks..."))
        command._terminalize_lost_ray_core_handles(
            runner,
            eligible_handles,
            error_message="Ray connection lost",
        )
        command._last_partitioned_handles = ()
        return retired_count + len(eligible_handles)

    peek_completed = getattr(runner, "peek_completed", None)
    acknowledge_completed = getattr(runner, "acknowledge_completed", None)
    use_peek_ack = callable(peek_completed) and callable(acknowledge_completed)
    handles_by_key = {
        (h.task_pk, h.attempt_number, h.execution_generation): h for h in eligible_handles
    }

    try:
        if use_peek_ack:
            completed = peek_completed(eligible_handles)
        else:
            completed = runner.poll_completed(eligible_handles)
    except Exception as e:
        diagnostic = render_console_diagnostic(e)
        command.stdout.write(command.style.ERROR(f"\nError polling Ray Core tasks: {diagnostic}"))
        return retired_count

    if not completed:
        return retired_count

    processed_count = 0
    for completion in completed:
        acknowledged = command._apply_ray_core_completion(completion)
        if acknowledged:
            processed_count += 1
            if use_peek_ack:
                key = (
                    completion.task_pk,
                    completion.attempt_number,
                    completion.execution_generation,
                )
                matched_handle = handles_by_key.get(key)
                if matched_handle is not None:
                    acknowledge_completed(matched_handle)

    command._last_partitioned_handles = (
        runner.pending_task_handles if runner.pending_count > 0 else ()
    )
    return retired_count + processed_count


def adopt_orphaned_ray_job_task(command: Any, task: Any, *, now: datetime) -> bool:
    """Fence a stale owner and adopt its Ray Job for reconciliation."""
    current = command._take_over_task_if_owner_stale(task, now=now)
    if current is None or current.state != TaskState.RUNNING:
        return False
    if task.ray_job_id:
        command.active_tasks[task.pk] = str(task.ray_job_id)
        command.active_task_identities[task.pk] = (
            int(task.attempt_number),
            int(task.execution_generation),
        )
    return True


def retire_active_ray_job_tracking(
    command: Any,
    task_pk: int,
    *,
    ray_job_id: str,
    identity: tuple[int, int],
) -> None:
    """Forget active tracking only when the exact tracked identity still matches."""
    if command.active_tasks.get(task_pk) != ray_job_id:
        return
    tracked_identity = command.active_task_identities.get(task_pk)
    if tracked_identity is not None and tracked_identity != identity:
        return
    command.active_tasks.pop(task_pk, None)
    command.active_task_identities.pop(task_pk, None)


def reconcile_ray_job_task(
    command: Any,
    task: Any,
    runner: Any,
    *,
    ray_job_id: str,
    completed_tasks: list[int],
    orphaned: bool,
    tracked_identity: tuple[int, int] | None = None,
    completion_only: bool = False,
) -> None:
    """Reconcile a single Ray Job task from either active or orphaned tracking."""
    from django_ray.runner.base import JobStatus

    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    task_cls = task_model(command)
    is_timed_out_fn = command_dep(command, "is_task_timed_out", default_is_task_timed_out)
    is_stuck_fn = command_dep(command, "is_task_stuck", default_is_task_stuck)
    prepare_cancel_fn = command_dep(
        command, "prepare_remote_cancellation", default_prepare_remote_cancellation
    )
    request_cancel_fn = command_dep(
        command, "request_remote_cancellation", default_request_remote_cancellation
    )
    record_lost_fn = command_dep(command, "record_lost", default_record_lost)
    cancel_task_fn = command_dep(command, "cancel_task", default_cancel_task)

    task_identity = (int(task.attempt_number), int(task.execution_generation))
    expected_identity = tracked_identity or task_identity
    expected_worker_id = command.worker_id

    def complete_tracking() -> None:
        completed_tasks.append(task.pk)
        command._retire_active_ray_job_tracking(
            task.pk,
            ray_job_id=ray_job_id,
            identity=expected_identity,
        )

    if task_identity != expected_identity:
        command._retire_active_ray_job_tracking(
            task.pk,
            ray_job_id=ray_job_id,
            identity=expected_identity,
        )
        return

    if completion_only and task.claimed_by_worker is None:
        return
    if task.claimed_by_worker is None and not command._adopt_orphaned_ray_job_task(
        task,
        now=dt.now(UTC),
    ):
        command._retire_active_ray_job_tracking(
            task.pk,
            ray_job_id=ray_job_id,
            identity=expected_identity,
        )
        return

    if str(task.claimed_by_worker or "") != expected_worker_id:
        command._retire_active_ray_job_tracking(
            task.pk,
            ray_job_id=ray_job_id,
            identity=expected_identity,
        )
        return

    if task.state == TaskState.CANCELLING:
        return
    if task.state == TaskState.CANCELLED:
        complete_tracking()
        command.stdout.write(command.style.WARNING(f"\nTask {task.pk} was cancelled"))
        return
    if task.state != TaskState.RUNNING:
        complete_tracking()
        return

    expected_attempt_number, expected_execution_generation = expected_identity
    handle = command._build_submission_handle(task, ray_job_id)
    strict_ray_job = is_strict_ray_job_submission_id(ray_job_id)
    rq2_ray_job = is_rq2_ray_job_submission_id(ray_job_id)

    def consume_valid_completion(
        completion_data: str | None,
    ) -> tuple[bool, InspectedExecutionCompletion | None]:
        if completion_data is None:
            return False, None

        pre_inspection = command._inspect_execution_completion(
            completion_data,
            task,
            supported_protocols=SUPPORTED_EXECUTION_PROTOCOL_RANGE,
        )
        pre_stored = False
        pre_result_data: str | None = None
        pre_result_reference: str | None = pre_inspection.prepared_result_reference
        pre_diagnostics: tuple[str, ...] = ()
        pre_storage_error: Exception | None = None
        if pre_inspection.decoded is not None and pre_inspection.decoded.completion.success:
            (
                eligible,
                pre_stored,
                pre_result_data,
                pre_result_reference,
                pre_diagnostics,
                pre_storage_error,
            ) = command._prepare_result_outside_lock(
                task,
                pre_inspection.decoded.completion.result,
                prepared_result_reference=pre_inspection.prepared_result_reference,
                expected_ray_job_id=ray_job_id,
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=completion_data,
                require_completion_data_match=True,
            )
            if not eligible:
                return True, None

        handled = False
        current: Any = None
        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                return True, None
            current = owned.execution
            inspection = command._inspect_execution_completion(
                completion_data,
                current,
                supported_protocols=owned.supported_protocols,
            )
            decoded = inspection.decoded
            if decoded is None:
                return False, inspection
            accepted_completion = decoded.completion
            if accepted_completion.success:
                if pre_storage_error is not None:
                    handled = command._handle_task_failure(
                        current,
                        error_message=(
                            "Failed to persist task result to external storage: "
                            f"{materialize_exception_message(pre_storage_error)}"
                        ),
                        exception_type="ResultStoragePersistenceError",
                        retryable=False,
                        expected_ray_job_id=ray_job_id,
                        expected_claimed_by_worker=expected_worker_id,
                        expected_attempt_number=expected_attempt_number,
                        expected_execution_generation=expected_execution_generation,
                        expected_completion_data=completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=(
                            accepted_completion.executor_django_ray_version
                        ),
                    )
                else:
                    handled = command._store_and_succeed_task(
                        current,
                        accepted_completion.result,
                        prepared_result_data=pre_result_data if pre_stored else None,
                        prepared_result_reference=(
                            pre_result_reference
                            if pre_stored
                            else inspection.prepared_result_reference
                        ),
                        _result_already_stored=pre_stored,
                        _storage_diagnostics=pre_diagnostics,
                        _already_locked=True,
                        expected_ray_job_id=ray_job_id,
                        expected_claimed_by_worker=expected_worker_id,
                        expected_attempt_number=expected_attempt_number,
                        expected_execution_generation=expected_execution_generation,
                        expected_completion_data=completion_data,
                        require_completion_data_match=True,
                        supported_protocols=owned.supported_protocols,
                        executor_django_ray_version=(
                            accepted_completion.executor_django_ray_version
                        ),
                    )
            else:
                handled = command._handle_task_failure(
                    current,
                    error_message=accepted_completion.error or "Unknown error",
                    error_traceback=accepted_completion.traceback,
                    exception_type=accepted_completion.exception_type,
                    retryable=accepted_completion.retryable,
                    expected_ray_job_id=ray_job_id,
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=completion_data,
                    require_completion_data_match=True,
                    supported_protocols=owned.supported_protocols,
                    executor_django_ray_version=accepted_completion.executor_django_ray_version,
                )
            if handled is False:
                return True, None

        assert current is not None
        task.__dict__.update(current.__dict__)
        if accepted_completion.success:
            command.stdout.write(command.style.SUCCESS(f"\nTask {task.pk} completed"))
        else:
            command.stdout.write(
                command.style.WARNING(
                    f"\nTask {task.pk} returned failure envelope, handling via retry policy"
                )
            )
        complete_tracking()
        return True, None

    completion_consumed, completion_inspection = consume_valid_completion(task.completion_data)
    if completion_consumed or completion_only:
        return

    assert runner is not None
    job_info = runner.get_status(handle)
    now = dt.now(UTC)

    try:
        task.refresh_from_db(
            fields=[
                "state",
                "completion_data",
                "ray_job_id",
                "ray_address",
                "ray_job_request_reference",
                "claimed_by_worker",
                "attempt_number",
                "execution_generation",
                "execution_protocol_version",
                "started_at",
                "last_heartbeat_at",
            ]
        )
    except task_cls.DoesNotExist:
        complete_tracking()
        return

    if (
        str(task.ray_job_id or "") != ray_job_id
        or str(task.claimed_by_worker or "") != expected_worker_id
        or task.attempt_number != expected_attempt_number
        or task.execution_generation != expected_execution_generation
    ):
        command._retire_active_ray_job_tracking(
            task.pk,
            ray_job_id=ray_job_id,
            identity=expected_identity,
        )
        return

    if task.state != TaskState.RUNNING:
        complete_tracking()
        return

    def resolve_stale_untrusted_execution(
        *,
        expected_completion_data: str | None,
        error_message: str,
        log_detail: str,
        require_stale: bool = True,
        binding_validator: Any | None = None,
    ) -> bool:
        timeout_recovery_owns_task = expected_completion_data is None and is_timed_out_fn(task)
        if require_stale and (timeout_recovery_owns_task or not is_stuck_fn(task)):
            return False

        prepared_cancellation = prepare_cancel_fn(runner, handle)

        # Hold the authoritative execution row lock across remote stop and
        # durable LOST + cancellation outcome persistence so no concurrent
        # worker can mutate or adopt the row while stop is in flight.
        current: Any = None
        cancellation: CancellationOutcome | None = None
        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                return False
            current = owned.execution
            timeout_recovery_owns_task = expected_completion_data is None and is_timed_out_fn(
                current
            )
            if require_stale and (timeout_recovery_owns_task or not is_stuck_fn(current)):
                return False
            if binding_validator is not None and binding_validator(current) is None:
                return False
            cancellation = request_cancel_fn(
                runner,
                handle,
                prepared=prepared_cancellation,
            )
            status_value = CancellationStatus(cancellation.status.value)
            if not record_lost_fn(
                current,
                error_message=error_message,
                expected_completion_data=expected_completion_data,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                supported_protocols=owned.supported_protocols,
            ):
                return False
            updated_rows = task_cls.objects.filter(
                pk=current.pk,
                state=TaskState.LOST,
                attempt_number=expected_attempt_number,
                execution_generation=expected_execution_generation,
            ).update(
                cancellation_status=status_value,
                cancellation_error=cancellation.message,
            )
            if updated_rows:
                current.cancellation_status = status_value
                current.cancellation_error = cancellation.message

        assert current is not None and cancellation is not None
        task.__dict__.update(current.__dict__)
        complete_tracking()
        command.stdout.write(
            command.style.ERROR(
                f"\nTask {task.pk} {log_detail}; exact best-effort stop outcome was "
                f"{cancellation.status.value} and automatic retry was suppressed"
            )
        )
        return True

    def handle_failure_authoritatively(
        *,
        error_message: str,
        exception_type: str,
        expected_completion_data: str | None,
        error_traceback: str | None = None,
        retryable: bool | None = None,
    ) -> bool:
        current: Any = None
        handled = False
        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                return False
            current = owned.execution
            handled = command._handle_task_failure(
                current,
                error_message=error_message,
                error_traceback=error_traceback,
                exception_type=exception_type,
                retryable=retryable,
                expected_ray_job_id=ray_job_id,
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=expected_completion_data,
                require_completion_data_match=True,
                supported_protocols=owned.supported_protocols,
            )

        if handled:
            assert current is not None
            task.__dict__.update(current.__dict__)
        return handled

    completion_data = task.completion_data

    strict_metadata_marker = ray_job_metadata_has_strict_marker(getattr(job_info, "metadata", None))
    completion_consumed, completion_inspection = consume_valid_completion(completion_data)
    if completion_consumed:
        return

    def strict_binding_rejection(candidate: Any) -> str | None:
        if strict_metadata_marker and not strict_ray_job:
            return "unexpected_strict_metadata"
        if not strict_ray_job:
            return None
        if not is_valid_strict_ray_job_submission_id(ray_job_id):
            return "invalid_submission_id"
        if job_info.job_id != ray_job_id:
            return "job_id_mismatch"
        if job_info.status == JobStatus.UNKNOWN and job_info.metadata is None:
            return None
        try:
            expectation = parse_ray_job_request_metadata(
                job_info.metadata,
                required=True,
            )
            assert expectation is not None
            if rq2_ray_job:
                if not isinstance(expectation, RayJobRequestReferenceExpectation):
                    return "invalid"
                request_reference = candidate.ray_job_request_reference
                if not request_reference:
                    return "missing"
                from django_ray.ray_job_request_storage import (
                    RayJobRequestStorageError,
                    ray_job_request_reference_content_identity,
                )

                try:
                    request_digest, request_size_bytes = ray_job_request_reference_content_identity(
                        request_reference
                    )
                except RayJobRequestStorageError as exc:
                    return exc.classification.value
                validate_ray_job_request_reference_expectation(
                    expectation,
                    expected_identity=command._execution_completion_identity(candidate),
                    expected_execution_protocol_version=int(candidate.execution_protocol_version),
                    expected_request_sha256=request_digest,
                    expected_request_size_bytes=request_size_bytes,
                    expected_submission_id=ray_job_id,
                    request_reference=request_reference,
                )
            else:
                if not isinstance(expectation, RayJobRequestExpectation):
                    return "invalid"
                validate_ray_job_request_expectation(
                    expectation,
                    expected_identity=command._execution_completion_identity(candidate),
                    expected_execution_protocol_version=int(candidate.execution_protocol_version),
                )
        except RayJobRequestBindingError as exc:
            return exc.classification.value
        return None

    binding_rejection = strict_binding_rejection(task)
    if binding_rejection is not None:
        legacy_marker_conflict = not strict_ray_job
        resolve_stale_untrusted_execution(
            expected_completion_data=completion_data,
            error_message=(
                f"Strict Ray Job request binding could not be verified ({binding_rejection})"
            ),
            log_detail=(
                "had strict request metadata attached to a legacy submission ID"
                if legacy_marker_conflict
                else f"had an untrusted strict request binding ({binding_rejection})"
            ),
            require_stale=False,
            binding_validator=strict_binding_rejection,
        )
        return

    strict_terminal = strict_ray_job and job_info.status in (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.STOPPED,
    )
    if strict_terminal:
        if not command._completion_envelope_grace_expired(task, now=now):
            command.stdout.write(
                command.style.NOTICE(
                    f"\nTask {task.pk} strict Ray job {job_info.status.value.lower()}; "
                    "waiting for an exact completion envelope"
                )
            )
            return
        handled = handle_failure_authoritatively(
            error_message="Strict Ray Job terminated without an exact completion envelope",
            exception_type="RayJobCompletionMissing",
            expected_completion_data=completion_data,
            retryable=False,
        )
        if handled:
            complete_tracking()
            command.stdout.write(
                command.style.ERROR(
                    f"\nTask {task.pk} strict Ray job exceeded the completion "
                    "envelope grace period without automatic retry"
                )
            )
        return

    if completion_data is None and job_info.status in (
        JobStatus.PENDING,
        JobStatus.RUNNING,
    ):
        pending_batch = getattr(command, "_pending_ray_job_heartbeats", None)
        if isinstance(pending_batch, list):
            pending_batch.append(
                (
                    task,
                    ray_job_id,
                    expected_worker_id,
                    expected_attempt_number,
                    expected_execution_generation,
                    now,
                )
            )
            return
        current: Any = None
        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                return
            current = owned.execution
            command._mark_task_monitor_heartbeat(
                current,
                now=now,
                ray_job_id=ray_job_id,
                expected_worker_id=expected_worker_id,
                attempt_number=expected_attempt_number,
                execution_generation=expected_execution_generation,
            )
        task.__dict__.update(current.__dict__)
        return

    if completion_data is not None:
        assert completion_inspection is not None
        assert completion_inspection.decoded is None
        assert completion_inspection.rejection is not None
        completion_error = (
            "malformed"
            if completion_inspection.rejection is ExecutionCompletionRejection.MALFORMED_LEGACY
            else completion_inspection.rejection.value.replace("_", " ")
        )

        if completion_inspection.requires_nonretryable_disposition:
            error_message, exception_type, retryable = command._completion_rejection_policy(
                completion_inspection
            )
            if job_info.status in (
                JobStatus.UNKNOWN,
                JobStatus.PENDING,
                JobStatus.RUNNING,
            ):
                resolve_stale_untrusted_execution(
                    expected_completion_data=completion_data,
                    error_message=error_message,
                    log_detail="published a non-retryable incompatible completion",
                    require_stale=False,
                )
                return
            handled = handle_failure_authoritatively(
                error_message=error_message,
                exception_type=exception_type,
                expected_completion_data=completion_data,
                retryable=retryable,
            )
            if handled:
                complete_tracking()
                command.stdout.write(
                    command.style.ERROR(
                        f"\nTask {task.pk} terminalized a non-retryable incompatible "
                        "completion without automatic retry"
                    )
                )
            return

        if completion_inspection.rejection is not None:
            if job_info.status == JobStatus.UNKNOWN:
                if resolve_stale_untrusted_execution(
                    expected_completion_data=completion_data,
                    error_message="Ray Job status remained UNKNOWN past the stuck-task timeout",
                    log_detail="Ray job status remained unknown past the stuck-task timeout",
                ):
                    return
                command.stdout.write(
                    command.style.WARNING(
                        f"\nTask {task.pk} has a {completion_error} completion envelope "
                        "while Ray job status is unknown; waiting for bounded recovery"
                    )
                )
                return
            if job_info.status in (JobStatus.PENDING, JobStatus.RUNNING):
                if resolve_stale_untrusted_execution(
                    expected_completion_data=completion_data,
                    error_message=(
                        f"Ray Job produced a {completion_error} completion envelope "
                        f"while still {job_info.status.value}"
                    ),
                    log_detail=(
                        f"had a {completion_error} completion envelope while Ray "
                        f"still reported {job_info.status.value}"
                    ),
                ):
                    return
                command.stdout.write(
                    command.style.WARNING(
                        f"\nTask {task.pk} has a {completion_error} completion envelope "
                        f"while Ray still reports {job_info.status.value}; waiting for "
                        "bounded recovery"
                    )
                )
                return
            if command._completion_envelope_grace_expired(task, now=now):
                resolve_stale_untrusted_execution(
                    expected_completion_data=completion_data,
                    error_message=(
                        "Legacy Ray Job has no trusted completion after its grace period; "
                        "application effects are unknown and automatic retry is suppressed"
                    ),
                    log_detail="has an uncertain legacy completion outcome",
                    require_stale=False,
                )
            else:
                command.stdout.write(
                    command.style.WARNING(
                        f"\nTask {task.pk} has a {completion_error} completion envelope; "
                        "waiting for a valid update"
                    )
                )
            return

        raise AssertionError("valid completion envelope was not consumed")

    if job_info.status == JobStatus.SUCCEEDED:
        if command._completion_envelope_grace_expired(task, now=now):
            resolve_stale_untrusted_execution(
                expected_completion_data=completion_data,
                error_message=(
                    "Legacy Ray Job has no trusted completion after its grace period; "
                    "application effects are unknown and automatic retry is suppressed"
                ),
                log_detail="has an uncertain legacy completion outcome",
                require_stale=False,
            )
            return
        command.stdout.write(
            command.style.NOTICE(
                f"\nTask {task.pk} Ray job succeeded; waiting for completion envelope"
            )
        )
        return

    if job_info.status == JobStatus.FAILED:
        resolve_stale_untrusted_execution(
            expected_completion_data=completion_data,
            error_message=(
                "Legacy Ray Job failed without a trusted completion; "
                "application effects are unknown and automatic retry is suppressed"
            ),
            log_detail="has an uncertain legacy Ray Job outcome",
            require_stale=False,
        )
        return

    if job_info.status == JobStatus.STOPPED:
        current = None
        cancelled = False
        with command._authoritative_task_owner(
            task,
            expected_state=TaskState.RUNNING,
            allow_takeover=False,
            require_completion_data_match=True,
        ) as owned:
            if owned is None:
                return
            current = owned.execution
            cancelled = cancel_task_fn(
                current,
                allowed_states=(TaskState.RUNNING,),
                expected_worker_id=expected_worker_id,
                expected_ray_job_id=ray_job_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=completion_data,
                require_completion_data_match=True,
                supported_protocols=owned.supported_protocols,
            )
            if not cancelled:
                return
        assert current is not None
        task.__dict__.update(current.__dict__)
        complete_tracking()
        command.stdout.write(command.style.WARNING(f"\nTask {task.pk} was stopped"))
        return

    if job_info.status == JobStatus.UNKNOWN and resolve_stale_untrusted_execution(
        expected_completion_data=None,
        error_message="Ray Job status remained UNKNOWN past the stuck-task timeout",
        log_detail="Ray job status remained unknown past the stuck-task timeout",
    ):
        return

    scope = "orphaned " if orphaned else ""
    diagnostic = render_console_diagnostic(job_info.message or "no details")
    command.stdout.write(
        command.style.WARNING(f"\nTask {task.pk} {scope}Ray job status is unknown: {diagnostic}")
    )


def poll_ray_job_completions(command: Any) -> int:
    """Consume at most 32 active durable receipts without a Ray control RPC."""
    if command.sync_mode or command.shutdown_requested or not command.active_tasks:
        return 0

    task_cls = task_model(command)
    task_ids = nsmallest(
        32,
        (
            task_pk
            for task_pk in command.active_tasks
            if command._ray_job_completion_cursor < task_pk <= command._ray_job_completion_cycle_end
        ),
    )
    if not task_ids:
        command._ray_job_completion_cycle_end = max(command.active_tasks)
        task_ids = nsmallest(32, command.active_tasks)
    command._ray_job_completion_cursor = task_ids[-1]
    tracking = {
        task_pk: (command.active_tasks[task_pk], command.active_task_identities[task_pk])
        for task_pk in task_ids
        if task_pk in command.active_task_identities
    }
    if not tracking:
        return 0

    completed_tasks: list[int] = []
    try:
        tasks = task_cls.objects.filter(
            pk__in=tracking,
            completion_data__isnull=False,
        ).only(
            "pk",
            "task_id",
            "state",
            "claimed_by_worker",
            "attempt_number",
            "execution_generation",
            "execution_protocol_version",
            "ray_job_id",
            "ray_address",
            "started_at",
            "last_heartbeat_at",
            "completion_data",
        )
        for task in tasks:
            if command.shutdown_requested:
                break
            ray_job_id, tracked_identity = tracking[task.pk]
            try:
                command._reconcile_ray_job_task(
                    task,
                    None,
                    ray_job_id=ray_job_id,
                    completed_tasks=completed_tasks,
                    orphaned=False,
                    tracked_identity=tracked_identity,
                    completion_only=True,
                )
            except Exception as error:
                diagnostic = render_console_diagnostic(error)
                command.stdout.write(
                    command.style.ERROR(
                        f"\nError consuming task {task.pk} completion: {diagnostic}"
                    )
                )
    except Exception as error:
        diagnostic = render_console_diagnostic(error)
        command.stdout.write(
            command.style.ERROR(f"\nError polling Ray Job completions: {diagnostic}")
        )
    return len(completed_tasks)


def active_reconciliation_page(command: Any) -> list[int]:
    """Bound RPC candidates while walking local active Ray Job tracking."""
    if not command.active_tasks:
        command._active_reconciliation_pending = False
        return []
    task_ids = nsmallest(
        MAINTENANCE_PAGE_SIZE + 1,
        (
            pk
            for pk in command.active_tasks
            if command._active_reconciliation_cursor
            < pk
            <= command._active_reconciliation_cycle_end
        ),
    )
    if not task_ids:
        command._active_reconciliation_cursor = 0
        command._active_reconciliation_cycle_end = max(command.active_tasks)
        task_ids = nsmallest(MAINTENANCE_PAGE_SIZE + 1, command.active_tasks)
    command._active_reconciliation_pending = len(task_ids) > MAINTENANCE_PAGE_SIZE
    return task_ids[:MAINTENANCE_PAGE_SIZE]


def _flush_ray_job_monitor_heartbeats(
    command: Any,
    pending: list[tuple[Any, str, str, int, int, datetime]],
) -> None:
    """Persist pending Ray Job monitor heartbeats in one lease-fenced transaction."""
    if not pending or command.lease_ownership_lost or command.shutdown_requested:
        return
    task_cls = task_model(command)
    batch_now = pending[-1][5]
    row_filter = Q()
    for _task, ray_job_id, expected_worker_id, attempt_number, execution_generation, _ts in pending:
        row_filter |= Q(
            pk=_task.pk,
            ray_job_id=ray_job_id,
            claimed_by_worker=expected_worker_id,
            attempt_number=attempt_number,
            execution_generation=execution_generation,
        )

    with transaction.atomic():
        if command.lease_identity is not None:
            lease = command._lock_authoritative_leases(
                source_worker_id=command.worker_id,
                allow_takeover=False,
                renew_heartbeat=False,
            )
            if lease is None:
                return
        updated = task_cls.objects.filter(
            row_filter,
            state=TaskState.RUNNING,
            completion_data__isnull=True,
        ).update(last_heartbeat_at=batch_now)
    if updated:
        for task, *_rest in pending:
            task.last_heartbeat_at = batch_now


def reconcile_tasks(command: Any, queues: Sequence[str] | None = None) -> int:
    """Reconcile Ray Jobs, optionally fencing orphan adoption by queue."""
    if command.sync_mode or command.shutdown_requested:
        return 0

    supported_protocols = command._authoritative_protocol_range_for_scan()
    if supported_protocols is None:
        return 0

    from django_ray.runner.ray_job import RayJobRunner

    dt = command_dep(command, "datetime", BUILTIN_DATETIME)
    task_cls = task_model(command)

    runner: RayJobRunner | None = None
    completed_tasks: list[int] = []
    reconciled_task_ids: set[int] = set()
    original_active_page = command._active_reconciliation_page()
    new_orphan_task_ids: set[int] = set()
    pending_heartbeats: list[tuple[Any, str, str, int, int, datetime]] = []
    previous_heartbeats = getattr(command, "_pending_ray_job_heartbeats", None)
    command._pending_ray_job_heartbeats = pending_heartbeats
    try:
        for task_pk in original_active_page:
            if command.shutdown_requested:
                break
            command._active_reconciliation_cursor = task_pk
            ray_job_id = command.active_tasks.get(task_pk)
            if ray_job_id is None:
                continue
            tracked_identity = command.active_task_identities.get(task_pk)
            try:
                if runner is None:
                    runner = RayJobRunner()
                task = task_cls.objects.filter(
                    execution_protocol_version__gte=supported_protocols.minimum,
                    execution_protocol_version__lte=supported_protocols.maximum,
                ).get(pk=task_pk)
                reconciled_task_ids.add(task.pk)
                command._reconcile_ray_job_task(
                    task,
                    runner,
                    ray_job_id=ray_job_id,
                    completed_tasks=completed_tasks,
                    orphaned=False,
                    tracked_identity=tracked_identity,
                )

            except task_cls.DoesNotExist:
                completed_tasks.append(task_pk)
                if tracked_identity is not None:
                    command._retire_active_ray_job_tracking(
                        task_pk,
                        ray_job_id=ray_job_id,
                        identity=tracked_identity,
                    )
                elif command.active_tasks.get(task_pk) == ray_job_id:
                    command.active_tasks.pop(task_pk, None)
            except Exception as e:
                diagnostic = render_console_diagnostic(e)
                command.stdout.write(
                    command.style.ERROR(f"\nError reconciling task {task_pk}: {diagnostic}")
                )

        if command.shutdown_requested:
            return len(completed_tasks)

        orphaned_tasks = command._defer_maintenance_scan_fields(
            task_cls.objects.filter(
                state=TaskState.RUNNING,
                ray_job_id__startswith="raysubmit_",
                execution_protocol_version__gte=supported_protocols.minimum,
                execution_protocol_version__lte=supported_protocols.maximum,
            ).exclude(pk__in=reconciled_task_ids)
        )
        if queues is not None:
            orphaned_tasks = orphaned_tasks.filter(queue_name__in=queues)
        candidates = command._maintenance_scan_page(
            "orphan", orphaned_tasks, queues, supported_protocols
        )
        active_worker_ids = command._maintenance_active_workers(candidates)
        for candidate in candidates:
            if command.shutdown_requested:
                break
            if candidate.owner == command.worker_id or candidate.owner in active_worker_ids:
                command._maintenance_scans["orphan"].advance(candidate)
                continue
            task = command._maintenance_snapshot("orphan", orphaned_tasks, candidate)
            if task is None:
                continue
            task_worker_id = str(task.claimed_by_worker) if task.claimed_by_worker else None
            if task_worker_id == command.worker_id:
                continue
            if task_worker_id and task_worker_id in active_worker_ids:
                continue

            try:
                if runner is None:
                    runner = RayJobRunner()
                if task.pk not in command.active_tasks and task.pk not in original_active_page:
                    new_orphan_task_ids.add(task.pk)
                if not command._adopt_orphaned_ray_job_task(task, now=dt.now(UTC)):
                    continue
                command.stdout.write(
                    command.style.NOTICE(
                        f"\nAdopted orphaned Ray job task {task.pk} for continued monitoring"
                    )
                )
                command._reconcile_ray_job_task(
                    task,
                    runner,
                    ray_job_id=str(task.ray_job_id or ""),
                    completed_tasks=completed_tasks,
                    orphaned=True,
                )
            except Exception as e:
                diagnostic = render_console_diagnostic(e)
                command.stdout.write(
                    command.style.ERROR(
                        f"\nError reconciling orphaned task {task.pk}: {diagnostic}"
                    )
                )
    finally:
        command._pending_ray_job_heartbeats = previous_heartbeats
        if pending_heartbeats:
            try:
                _flush_ray_job_monitor_heartbeats(command, pending_heartbeats)
            except Exception as e:
                diagnostic = render_console_diagnostic(e)
                command.stdout.write(
                    command.style.ERROR(
                        f"\nError flushing Ray Job monitor heartbeats: {diagnostic}"
                    )
                )

    adopted_count = sum(task_pk in command.active_tasks for task_pk in new_orphan_task_ids)
    return len(completed_tasks) + adopted_count
