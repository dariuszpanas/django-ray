"""Main polling loop, signal handling, heartbeat, and ``--drain`` coordination."""

from __future__ import annotations

import signal as stdlib_signal
import time as stdlib_time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from threading import current_thread, main_thread
from types import FrameType
from typing import Any

from django.db import InterfaceError, OperationalError, transaction

from django_ray import lease_policy
from django_ray.conf.settings import get_settings as default_get_settings
from django_ray.management.diagnostics import render_console_diagnostic
from django_ray.worker import (
    DB_BACKOFF_INITIAL_SECONDS,
    DB_BACKOFF_MAX_SECONDS,
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    SHUTDOWN_WAIT_SLICE_SECONDS,
    active_queue_task_counts,
    close_old_db_connections,
    command_dep,
    in_flight_task_count,
    is_transient_db_error,
)


def setup_signal_handlers(command: Any) -> None:
    """Setup signal handlers for graceful shutdown."""
    sig = command_dep(command, "signal", stdlib_signal)
    sig.signal(sig.SIGTERM, command.handle_shutdown_signal)
    sig.signal(sig.SIGINT, command.handle_shutdown_signal)


@contextmanager
def preserve_shutdown_signal_handlers(command: Any) -> Generator[None, None, None]:
    """Restore only this command's main-thread handlers after Ray init."""
    sig = command_dep(command, "signal", stdlib_signal)
    current_thread_fn = command_dep(command, "current_thread", current_thread)
    main_thread_fn = command_dep(command, "main_thread", main_thread)
    owned_signals = (
        [
            signum
            for signum in (sig.SIGTERM, sig.SIGINT)
            if sig.getsignal(signum) == command.handle_shutdown_signal
        ]
        if current_thread_fn() is main_thread_fn()
        else []
    )
    try:
        yield
    finally:
        for signum in owned_signals:
            sig.signal(signum, command.handle_shutdown_signal)


def handle_shutdown_signal(command: Any, signum: int, frame: FrameType | None) -> None:
    """Handle shutdown signals in a signal-safe, non-blocking manner."""
    del frame
    if command.shutdown_requested:
        command._shutdown_event.set()
        return
    command.shutdown_requested = True
    command.shutdown_signal = signum
    command.shutdown_exit_code = 128 + signum
    command._shutdown_event.set()


def log_received_shutdown_signal(command: Any) -> None:
    """Emit signal shutdown notice outside the asynchronous signal handler."""
    if command.shutdown_signal is not None and not command._signal_logged:
        command._signal_logged = True
        command.stdout.write(
            command.style.WARNING(f"\nReceived signal {command.shutdown_signal}, shutting down...")
        )


def within_transient_db_recovery_budget(command: Any) -> bool:
    """Return whether a transient DB blip is still safely within lease TTL."""
    if command._last_successful_heartbeat_monotonic <= 0.0:
        return False
    get_settings_fn = command_dep(command, "get_settings", default_get_settings)
    time_mod = command_dep(command, "time", stdlib_time)
    lease_seconds = float(
        command._lease_policy_seconds or get_settings_fn().get("WORKER_LEASE_SECONDS", 60)
    )
    budget = max(1.0, lease_seconds - 2.0)
    return (time_mod.monotonic() - command._last_successful_heartbeat_monotonic) < budget


def wait_for_poll_deadline(command: Any, timeout: float) -> None:
    """Wait for the next loop deadline or a graceful-shutdown wakeup."""
    time_mod = command_dep(command, "time", stdlib_time)
    deadline = time_mod.monotonic() + timeout
    while not command.shutdown_requested:
        remaining = deadline - time_mod.monotonic()
        if remaining <= 0:
            return
        if command._shutdown_event.wait(min(remaining, SHUTDOWN_WAIT_SLICE_SECONDS)):
            return


def send_heartbeat(command: Any) -> None:
    """Send worker heartbeat, update lease, check Ray health, and log structured status."""
    identity = command.lease_identity
    if identity is None:
        command._request_shutdown_for_lease_loss("worker lease was never acquired")
        return

    time_mod = command_dep(command, "time", stdlib_time)

    try:
        with transaction.atomic():
            updated = command._lock_authoritative_leases(
                source_worker_id=command.worker_id,
                allow_takeover=False,
            )
    except Exception as error:
        if is_transient_db_error(error) and command._within_transient_db_recovery_budget():
            close_old_db_connections(command)
            delay = command._db_backoff_delay
            command._db_backoff_delay = min(command._db_backoff_delay * 2.0, DB_BACKOFF_MAX_SECONDS)
            command._heartbeat_retry_after = time_mod.monotonic() + delay
            command.logger.warning(
                "Transient database error during worker heartbeat; retrying within lease TTL",
                exc_info=True,
            )
            return
        command._request_shutdown_for_lease_loss(
            "worker lease heartbeat failed",
            error=error,
        )
        return

    if updated:
        command._last_successful_heartbeat_monotonic = time_mod.monotonic()
        command._db_backoff_delay = DB_BACKOFF_INITIAL_SECONDS
        command._heartbeat_retry_after = 0.0
    else:
        return

    if command.shutdown_requested:
        return

    if command.execution_mode in ("local", "cluster"):
        command._check_ray_connection()

    if hasattr(command, "_heartbeat_count"):
        command._heartbeat_count += 1
    else:
        command._heartbeat_count = 1

    active = in_flight_task_count(command)
    idle_time = (
        time_mod.time() - command.last_task_processed if command.last_task_processed > 0 else 0.0
    )
    draining = bool(getattr(command, "drain_mode", False))

    command.logger.debug(
        "Worker heartbeat",
        heartbeat_count=command._heartbeat_count,
        tasks_processed=command.tasks_processed_count,
        active_tasks=active,
        idle_seconds=round(idle_time, 1),
        draining=draining,
    )

    # Periodic structured/throttled status summary every 4th heartbeat (~60s at
    # default 15s heartbeat) without raw '.' stdout dot spam (F-093).
    if command._heartbeat_count % 4 == 0:
        command.logger.info(
            "Worker status",
            heartbeat_count=command._heartbeat_count,
            tasks_processed=command.tasks_processed_count,
            active_tasks=active,
            idle_seconds=round(idle_time, 1),
            draining=draining,
        )
        drain_suffix = ", draining=true" if draining else ""
        command._write_worker_output(
            f"[Status] tasks_processed={command.tasks_processed_count}, "
            f"active={active}, idle={idle_time:.0f}s{drain_suffix}"
        )
        command.stdout.flush()


def run_loop(
    command: Any,
    queues: Sequence[str],
    concurrency: int,
    heartbeat_interval: float,
) -> None:
    """Run the main worker loop, including graceful ``--drain`` termination."""
    time_mod = command_dep(command, "time", stdlib_time)
    now = time_mod.monotonic()
    next_heartbeat = now
    next_completion_poll = now
    next_ray_job_completion_poll = now
    next_claim = now
    next_reconciliation = now
    next_timeout_check = now
    next_cancellation = now
    next_lease_cleanup = now

    drain_mode = bool(getattr(command, "drain_mode", False))
    drain_timeout = float(getattr(command, "drain_timeout_seconds", DEFAULT_DRAIN_TIMEOUT_SECONDS))
    drain_deadline = now + drain_timeout if drain_mode else None
    if drain_mode:
        command.drain_timed_out = False
        command.logger.info(
            "Worker entering drain mode",
            drain_timeout_seconds=drain_timeout,
            active_tasks=in_flight_task_count(command),
        )
        command._write_worker_output(
            command.style.NOTICE(
                f"Drain mode active (timeout={drain_timeout:g}s); no new tasks will be claimed"
            )
        )

    while not command.shutdown_requested:
        close_old_db_connections(command)
        current_time = time_mod.monotonic()
        activity = False
        claim_due = not drain_mode and current_time >= next_claim

        try:
            if current_time >= next_heartbeat:
                command._heartbeat_retry_after = 0.0
                command.send_heartbeat()
                if command._heartbeat_retry_after > 0.0:
                    next_heartbeat = command._heartbeat_retry_after
                else:
                    next_heartbeat = current_time + heartbeat_interval

            if command.shutdown_requested:
                break

            if (
                current_time >= next_completion_poll
                and command.execution_mode in ("local", "cluster")
                and command.ray_core_runner
            ):
                activity = bool(command.poll_ray_core_tasks()) or activity
                next_completion_poll = current_time + command.completion_poll_interval

            if command.shutdown_requested:
                break

            if (
                current_time >= next_ray_job_completion_poll
                and command.active_tasks
                and not command.sync_mode
            ):
                completed = command.poll_ray_job_completions()
                activity = bool(completed) or activity
                claim_due = (not drain_mode) and (bool(completed) or claim_due)
                next_ray_job_completion_poll = (
                    time_mod.monotonic() + command.ray_job_completion_poll_interval
                )
            if command.shutdown_requested:
                break

            if claim_due:
                activity = bool(command.claim_and_process_tasks(queues, concurrency)) or activity

            if command.shutdown_requested:
                break

            if current_time >= next_cancellation:
                activity = bool(command.process_cancellations(queues)) or activity
                next_cancellation = command._maintenance_deadline(
                    "cancelling", command.cancellation_interval
                )
            if command.shutdown_requested:
                break

            if current_time >= next_reconciliation:
                activity = bool(command.reconcile_tasks(queues)) or activity
                command.last_reconciliation = current_time
                next_reconciliation = command._maintenance_deadline(
                    "orphan", command.reconciliation_interval
                )
            if command.shutdown_requested:
                break

            if current_time >= next_timeout_check:
                activity = bool(command.detect_stuck_tasks(queues)) or activity
                next_timeout_check = command._maintenance_deadline(
                    "running", command.timeout_check_interval
                )
            if command.shutdown_requested:
                break

            if current_time >= next_lease_cleanup:
                activity = bool(command.cleanup_expired_leases()) or activity
                next_lease_cleanup = current_time + command.lease_cleanup_interval

            if drain_mode:
                remaining_in_flight = in_flight_task_count(command)
                remaining_queued, remaining_active_db = active_queue_task_counts(command, queues)
                if remaining_in_flight == 0 and remaining_queued == 0 and remaining_active_db == 0:
                    command.drain_timed_out = False
                    if command.shutdown_exit_code is None:
                        command.shutdown_exit_code = 0
                    command.logger.info(
                        "Worker drain completed cleanly",
                        active_tasks=0,
                        queued_tasks=0,
                        active_queue_tasks=0,
                    )
                    command._write_worker_output(
                        command.style.SUCCESS(
                            f"Drain complete for worker {command.worker_id}; exiting cleanly"
                        )
                    )
                    break
                assert drain_deadline is not None
                if time_mod.monotonic() >= drain_deadline:
                    command.drain_timed_out = True
                    if command.shutdown_exit_code is None or command.shutdown_exit_code == 0:
                        command.shutdown_exit_code = 1
                    command.logger.warning(
                        "Worker drain timed out with remaining work",
                        drain_timeout_seconds=drain_timeout,
                        active_tasks=remaining_in_flight,
                        queued_tasks=remaining_queued,
                        active_queue_tasks=remaining_active_db,
                    )
                    command.stdout.write(
                        command.style.WARNING(
                            f"Drain timed out after {drain_timeout:g}s with "
                            f"{remaining_in_flight} in-flight task(s) remaining "
                            f"(queue backlog: queued={remaining_queued}, "
                            f"active={remaining_active_db})"
                        )
                    )
                    break
        except (OperationalError, InterfaceError, lease_policy.LeasePolicyError) as error:
            if not is_transient_db_error(error):
                raise
            close_old_db_connections(command)
            if not command._within_transient_db_recovery_budget():
                command._request_shutdown_for_lease_loss(
                    "database remained unreachable within worker lease TTL",
                    error=error,
                )
                break
            delay = min(command._db_backoff_delay, max(0.5, heartbeat_interval / 2.0))
            command._db_backoff_delay = min(command._db_backoff_delay * 2.0, DB_BACKOFF_MAX_SECONDS)
            command.logger.warning(
                "Transient database error in worker loop; backing off",
                exc_info=True,
            )
            command.stdout.write(
                command.style.WARNING(
                    f"\nTransient database error ({render_console_diagnostic(error)}); "
                    f"retrying in {delay:g}s"
                )
            )
            command._wait_for_poll_deadline(delay)
            next_heartbeat = min(next_heartbeat, time_mod.monotonic())
            continue

        if not drain_mode:
            if claim_due:
                next_claim = current_time + command.polling_policy.next_delay(activity=activity)
            elif activity:
                next_claim = min(
                    next_claim,
                    current_time + command.polling_policy.next_delay(activity=True),
                )

        deadlines = [
            next_heartbeat,
            next_reconciliation,
            next_timeout_check,
            next_cancellation,
            next_lease_cleanup,
        ]
        if not drain_mode:
            deadlines.append(next_claim)
        elif drain_deadline is not None:
            deadlines.append(drain_deadline)
            drain_poll_step = min(max(float(command.poll_base_interval), 0.1), 0.5)
            deadlines.append(current_time + drain_poll_step)
        if (
            command.execution_mode in ("local", "cluster")
            and command.ray_core_runner
            and getattr(command.ray_core_runner, "pending_count", 0) > 0
        ):
            deadlines.append(next_completion_poll)
        if command.active_tasks and not command.sync_mode:
            deadlines.append(next_ray_job_completion_poll)

        sleep_seconds = max(0.0, min(deadlines) - time_mod.monotonic())
        command._wait_for_poll_deadline(sleep_seconds)

    command._log_received_shutdown_signal()
