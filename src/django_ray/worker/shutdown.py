"""Graceful shutdown handoff, lease release, and Ray disconnect."""

from __future__ import annotations

import copy
import queue
import threading
from datetime import UTC
from typing import Any

from django_ray.lifecycle import cancel_task as default_cancel_task
from django_ray.management.diagnostics import render_console_diagnostic
from django_ray.models import CancellationStatus, TaskState
from django_ray.redaction import materialize_exception_message
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.worker import (
    BUILTIN_DATETIME,
    close_old_db_connections,
    command_dep,
    task_model,
)


def _unpack_drained_ray_core_completions(
    drained: Any,
) -> tuple[tuple[Any, ...], tuple[Any, ...] | None]:
    """Unpack ``drain_ready_for_shutdown()`` 2-tuple or legacy completion sequence."""
    if not drained:
        return (), None
    if (
        isinstance(drained, tuple)
        and len(drained) == 2
        and isinstance(drained[0], (list, tuple))
        and isinstance(drained[1], (list, tuple))
    ):
        return tuple(drained[0]), tuple(drained[1])
    return tuple(drained), None


def _shutdown_ray_with_timeout(timeout_seconds: float = 5.0) -> bool:
    """Disconnect Ray inside a bounded window so shutdown never hangs forever."""
    import ray

    if not ray.is_initialized():
        return False

    result_queue: queue.Queue[tuple[str, BaseException | None]] = queue.Queue(maxsize=1)

    def _run_shutdown() -> None:
        try:
            ray.shutdown()
        except BaseException as exc:  # noqa: BLE001 - preserve exact shutdown error
            result_queue.put(("error", exc))
        else:
            result_queue.put(("ok", None))

    thread = threading.Thread(target=_run_shutdown, name="django-ray-shutdown", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(f"Ray shutdown timed out after {timeout_seconds:g}s")
    status, error = result_queue.get_nowait()
    if status == "error" and error is not None:
        raise error
    return True


def prepare_shutdown_handoff(command: Any) -> None:
    """Make in-flight work safe for the next worker before disconnecting."""
    if command.execution_mode == "sync" or command.lease_ownership_lost:
        return

    task_cls = task_model(command)
    cancel_task_fn = command_dep(command, "cancel_task", default_cancel_task)
    dt = command_dep(command, "datetime", BUILTIN_DATETIME)

    if command.execution_mode in ("local", "cluster") and command.ray_core_runner:
        runner = command.ray_core_runner
        drain_ready = getattr(runner, "drain_ready_for_shutdown", None)
        acknowledge_completed = getattr(runner, "acknowledge_completed", None)
        remaining_handles: tuple[Any, ...] | None = None
        if callable(drain_ready):
            try:
                ready_completions, remaining_handles = _unpack_drained_ray_core_completions(
                    drain_ready()
                )
            except Exception as exc:
                command.stdout.write(
                    command.style.WARNING(
                        "  Could not drain ready Ray Core completions during shutdown: "
                        f"{render_console_diagnostic(exc)}"
                    )
                )
                ready_completions = ()
                remaining_handles = None
            for completion in ready_completions:
                if command.lease_ownership_lost:
                    break
                applied = command._apply_ray_core_completion(completion)
                if applied and callable(acknowledge_completed):
                    handle = getattr(completion, "handle", None)
                    if handle is not None:
                        acknowledge_completed(handle)

        pending_handles = (
            remaining_handles
            if remaining_handles is not None
            else tuple(runner.pending_task_handles)
        )
        for pending_handle in pending_handles:
            if command.lease_ownership_lost:
                break
            try:
                task = task_cls.objects.get(pk=pending_handle.task_pk)
            except task_cls.DoesNotExist:
                continue
            if (
                task.state != TaskState.RUNNING
                or task.attempt_number != pending_handle.attempt_number
                or task.execution_generation != pending_handle.execution_generation
            ):
                continue

            # Phase 1: prove live lease and task ownership before releasing DB locks.
            verified_task: Any = None
            with command._authoritative_task_owner(
                task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is not None:
                    verified_task = copy.copy(owned.execution)
            if verified_task is None:
                continue

            # Phase 2: execute Ray Core cancellation strictly outside DB locks (R2-007).
            try:
                cancel_with_status = getattr(
                    runner,
                    "cancel_pending_with_status",
                    None,
                )
                if callable(cancel_with_status):
                    outcome = cancel_with_status(pending_handle)
                else:
                    accepted = runner.cancel_pending(pending_handle)
                    outcome = CancellationOutcome(
                        CancellationOutcomeStatus.REQUESTED
                        if accepted
                        else CancellationOutcomeStatus.INDETERMINATE
                    )
            except Exception as exc:
                outcome = CancellationOutcome(
                    CancellationOutcomeStatus.INDETERMINATE,
                    f"Ray Core shutdown cancellation failed: {materialize_exception_message(exc)}",
                )

            # Phase 3: re-acquire authoritative fence to persist decisive outcome (R2-001).
            finalized_cancelled = False
            transitioned_cancelling = False
            with command._authoritative_task_owner(
                verified_task,
                expected_state=TaskState.RUNNING,
                allow_takeover=False,
                require_completion_data_match=True,
            ) as owned:
                if owned is not None:
                    current = owned.execution
                    recorded_status = CancellationStatus(outcome.status.value)
                    if outcome.status in (
                        CancellationOutcomeStatus.REQUESTED,
                        CancellationOutcomeStatus.NOT_APPLICABLE,
                    ):
                        finalized_cancelled = cancel_task_fn(
                            current,
                            allowed_states=(TaskState.RUNNING, TaskState.CANCELLING),
                            expected_worker_id=command.worker_id,
                            expected_attempt_number=current.attempt_number,
                            expected_execution_generation=current.execution_generation,
                            expected_completion_data=current.completion_data,
                            require_completion_data_match=True,
                            cancellation_status=recorded_status,
                            cancellation_error=outcome.message,
                            supported_protocols=owned.supported_protocols,
                        )
                    else:
                        current.state = TaskState.CANCELLING
                        current.cancellation_status = recorded_status
                        current.cancellation_error = outcome.message
                        current.save(
                            update_fields=[
                                "state",
                                "cancellation_status",
                                "cancellation_error",
                            ]
                        )
                        transitioned_cancelling = True
            if finalized_cancelled:
                command.stdout.write(
                    command.style.WARNING(f"  Task {task.pk} cancelled during shutdown")
                )
            elif transitioned_cancelling:
                command.stdout.write(
                    command.style.WARNING(f"  Task {task.pk} marked CANCELLING during shutdown")
                )

    if command.execution_mode == "ray":
        for task_pk, ray_job_id in list(command.active_tasks.items()):
            if command.lease_ownership_lost:
                break
            identity = command.active_task_identities.get(task_pk)
            handed_off = False
            if identity is not None:
                try:
                    task = task_cls.objects.get(pk=task_pk)
                except task_cls.DoesNotExist:
                    task = None
                if task is not None and (
                    str(task.ray_job_id or "") == ray_job_id
                    and (task.attempt_number, task.execution_generation) == identity
                ):
                    with command._authoritative_task_owner(
                        task,
                        expected_state=TaskState.RUNNING,
                        allow_takeover=False,
                        require_completion_data_match=True,
                    ) as owned:
                        if owned is not None:
                            current = owned.execution
                            current.claimed_by_worker = None
                            current.last_heartbeat_at = dt.now(UTC)
                            current.save(update_fields=["claimed_by_worker", "last_heartbeat_at"])
                            handed_off = True
            if handed_off:
                command.stdout.write(
                    command.style.NOTICE(
                        f"  Ray Job task {task_pk} handed off for continued monitoring"
                    )
                )
            command.active_tasks.pop(task_pk, None)
            command.active_task_identities.pop(task_pk, None)


def shutdown(command: Any) -> None:
    """Perform graceful shutdown."""
    command._log_received_shutdown_signal()
    close_old_db_connections(command)
    cleanup_failed = False
    try:
        command._prepare_shutdown_handoff()
    except Exception:
        close_old_db_connections(command)
        command.logger.error("worker shutdown handoff failed", exc_info=True)
        command.stdout.write(
            command.style.ERROR("  Failed to prepare task handoff; continuing cleanup")
        )
        cleanup_failed = True
        if command.shutdown_exit_code is None or command.shutdown_exit_code == 0:
            command.shutdown_exit_code = 1

    sync_runner = getattr(command, "sync_runner", None)
    if sync_runner is not None and callable(getattr(sync_runner, "prepare_shutdown", None)):
        try:
            sync_runner.prepare_shutdown()
        except Exception:
            command.logger.warning("Failed to clean up sync runner state", exc_info=True)

    if command.lease_identity is not None:
        try:
            from django_ray.runner.leasing import release_lease

            if release_lease(command.lease_identity):
                command.stdout.write("  Lease released (marked inactive)")
            else:
                cleanup_failed = True
                if command.shutdown_exit_code is None or command.shutdown_exit_code == 0:
                    command.shutdown_exit_code = 1
                command.stdout.write("  Lease release skipped (ownership fence did not match)")
        except Exception:
            close_old_db_connections(command)
            cleanup_failed = True
            if command.shutdown_exit_code is None or command.shutdown_exit_code == 0:
                command.shutdown_exit_code = 1
            command.logger.error("worker lease release failed", exc_info=True)
            command.stdout.write("  Failed to release lease; see worker logs")

    if command.execution_mode in ("local", "cluster"):
        try:
            if _shutdown_ray_with_timeout(
                timeout_seconds=float(getattr(command, "_ray_shutdown_timeout", 5.0))
            ):
                command.stdout.write("  Ray connection closed")
        except Exception:
            cleanup_failed = True
            if command.shutdown_exit_code is None or command.shutdown_exit_code == 0:
                command.shutdown_exit_code = 1
            command.logger.error("worker Ray disconnect failed", exc_info=True)
            command.stdout.write("  Failed to close Ray connection; see worker logs")

    failed_without_signal = (
        command.shutdown_exit_code is not None
        and command.shutdown_exit_code != 0
        and command.shutdown_signal is None
    )
    if cleanup_failed or failed_without_signal:
        command.stdout.write(
            command.style.WARNING(f"\nWorker {command.worker_id} shut down with errors")
        )
    else:
        command.stdout.write(
            command.style.SUCCESS(f"\nWorker {command.worker_id} shut down cleanly")
        )
