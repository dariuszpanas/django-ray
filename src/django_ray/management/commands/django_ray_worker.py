"""Django management command for running the django-ray worker."""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from django_ray.conf.settings import get_settings
from django_ray.lifecycle import record_failure, succeed_task
from django_ray.logging import get_worker_logger
from django_ray.models import CancellationStatus, RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    finalize_cancellation,
    request_remote_cancellation,
)
from django_ray.runner.leasing import generate_worker_id, get_heartbeat_interval
from django_ray.runner.ray_core import RayCoreRunner
from django_ray.runner.reconciliation import (
    get_stuck_timeout,
    is_task_stuck,
    is_task_timed_out,
    mark_task_lost,
    mark_task_timed_out,
)
from django_ray.runner.retry import should_retry


class Command(BaseCommand):
    """Run a django-ray worker process."""

    help = "Run a django-ray worker that claims and executes tasks on Ray"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shutdown_requested = False
        self.worker_id = generate_worker_id()
        self.logger = get_worker_logger(self.worker_id)
        self.active_tasks: dict[int, str] = {}  # task_pk -> ray_job_id (for Ray Job API mode)
        self.ray_core_runner: RayCoreRunner | None = None  # For local/cluster modes
        self.last_reconciliation = 0.0  # Last time we ran stuck task detection
        self.reconciliation_interval = 30.0  # Check for stuck tasks every 30 seconds
        self.lease: TaskWorkerLease | None = None  # Worker lease for coordination
        self.lease_queue_name: str = "default"  # Queue name for lease recreation
        self.last_task_processed = 0.0  # Last time we processed a task
        self.tasks_processed_count = 0  # Total tasks processed
        self.task_monitor_heartbeat_interval = 15.0
        self.last_task_monitor_heartbeat = 0.0
        # A signal requests a graceful handoff.  Keep the signal number so the
        # command-line entrypoint can preserve the conventional 128+N status.
        self.shutdown_signal: int | None = None
        self.shutdown_exit_code: int | None = None
        self.verbosity = 1
        self.execution_mode = "sync"
        self.sync_mode = False
        self.local_mode = False
        self.cluster_address: str | None = None

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--queue",
            type=str,
            default=None,
            help="Queue name to process (default: default). Use comma-separated for multiple queues.",
        )
        parser.add_argument(
            "--queues",
            type=str,
            nargs="+",
            default=None,
            help="Queue names to process (space-separated). Alternative to --queue.",
        )
        parser.add_argument(
            "--all-queues",
            action="store_true",
            help="Process tasks from all configured queues.",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=None,
            help="Maximum concurrent tasks (default: from settings)",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Run tasks synchronously (without Ray, for testing)",
        )
        parser.add_argument(
            "--local",
            action="store_true",
            help="Run with local Ray instance (starts Ray automatically)",
        )
        parser.add_argument(
            "--cluster",
            type=str,
            default=None,
            help="Connect to a Ray cluster at the specified address (e.g., ray://localhost:10001)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Run the worker loop."""
        # Parse queue arguments - support multiple ways to specify queues
        queues = self._parse_queues(options)

        settings = get_settings()
        self.verbosity = int(options.get("verbosity", 1))
        concurrency = options.get("concurrency")
        if concurrency is not None and (
            type(concurrency) is not int or not 1 <= concurrency <= 1000
        ):
            raise CommandError("--concurrency must be an integer between 1 and 1000")
        self.sync_mode = options.get("sync", False)
        self.local_mode = options.get("local", False)
        self.cluster_address = options.get("cluster")

        # Determine execution mode
        if self.sync_mode:
            self.execution_mode = "sync"
        elif self.local_mode:
            self.execution_mode = "local"
            try:
                self._init_local_ray()
                # Initialize RayCoreRunner for task submission via @ray.remote
                self.ray_core_runner = RayCoreRunner()
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Initial Ray init failed: {e}"))
                self.stdout.write("Will retry connection during operation...")
        elif self.cluster_address:
            self.execution_mode = "cluster"
            try:
                self._init_cluster_ray(self.cluster_address)
                # Initialize RayCoreRunner for task submission via @ray.remote
                self.ray_core_runner = RayCoreRunner()
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Initial cluster connection failed: {e}"))
                self.stdout.write("Will retry connection during operation...")
        else:
            default_mode, default_cluster_address = self._get_default_execution_mode(settings)
            self.execution_mode = default_mode

            if self.execution_mode == "local":
                try:
                    self._init_local_ray()
                    self.ray_core_runner = RayCoreRunner()
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"Initial Ray init failed: {e}"))
                    self.stdout.write("Will retry connection during operation...")
            elif self.execution_mode == "cluster":
                assert default_cluster_address is not None, (
                    "_get_default_execution_mode() returns an address with cluster mode"
                )
                self.cluster_address = default_cluster_address
                try:
                    self._init_cluster_ray(self.cluster_address)
                    self.ray_core_runner = RayCoreRunner()
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"Initial cluster connection failed: {e}"))
                    self.stdout.write("Will retry connection during operation...")

        if concurrency is None:
            concurrency = settings.get("DEFAULT_CONCURRENCY", 10)
        self.task_monitor_heartbeat_interval = float(
            settings.get("TASK_MONITOR_HEARTBEAT_SECONDS", 15)
        )

        self.setup_signal_handlers()

        self._write_worker_output(
            self.style.SUCCESS(f"Starting django-ray worker {self.worker_id}")
        )
        self._write_worker_output(f"  Queues: {', '.join(queues)}")
        self._write_worker_output(f"  Concurrency: {concurrency}")
        self._write_worker_output(f"  Mode: {self.execution_mode}")

        heartbeat_interval = get_heartbeat_interval().total_seconds()

        # Create worker lease for distributed coordination (use first queue for lease)
        self._create_lease(queues[0] if len(queues) == 1 else ",".join(queues))

        try:
            self.run_loop(
                queues=queues,
                concurrency=concurrency,
                heartbeat_interval=heartbeat_interval,
            )
        except KeyboardInterrupt:
            self.shutdown_signal = signal.SIGINT
            self.shutdown_exit_code = 128 + signal.SIGINT
            self.shutdown_requested = True
            self.stdout.write("\nShutdown requested via keyboard interrupt")
        finally:
            self.shutdown()

        # ``BaseCommand.run_from_argv`` does not use the return value from
        # ``handle``.  Raise only for the real CLI path so direct ``handle``
        # calls (and ``call_command`` users) retain normal Python semantics.
        if self.shutdown_exit_code is not None and getattr(
            self, "_called_from_command_line", False
        ):
            raise SystemExit(self.shutdown_exit_code)

    def _get_default_execution_mode(self, settings: dict[str, Any]) -> tuple[str, str | None]:
        """Resolve default worker mode from settings when no CLI mode flag is set.

        Returns:
            Tuple of (execution_mode, cluster_address).
        """
        runner = settings.get("RUNNER", "ray_job")
        if runner == "ray_core":
            ray_address = settings.get("RAY_ADDRESS")
            if ray_address and ray_address != "auto":
                return "cluster", str(ray_address)
            return "local", None

        # ray_job default
        return "ray", None

    def _parse_queues(self, options: dict[str, Any]) -> list[str]:
        """Parse queue arguments from command options.

        Supports multiple ways to specify queues:
        - --queue default (single queue)
        - --queue default,high-priority,low-priority (comma-separated)
        - --queues default high-priority low-priority (space-separated)
        - --all-queues (all configured queues from TASKS setting)

        Args:
            options: Command options dictionary.

        Returns:
            List of queue names to process.
        """
        from django.conf import settings as django_settings

        # Check for --all-queues flag first
        if options.get("all_queues"):
            tasks_config = getattr(django_settings, "TASKS", {})
            default_backend = tasks_config.get("default", {})
            configured_queues = default_backend.get("QUEUES", ["default"])
            self.stdout.write(
                self.style.NOTICE(f"Processing all configured queues: {configured_queues}")
            )
            return list(configured_queues)

        # Check for --queues (space-separated list)
        if options.get("queues"):
            return options["queues"]

        # Check for --queue (single or comma-separated)
        queue_arg = options.get("queue")
        if queue_arg:
            if "," in queue_arg:
                return [q.strip() for q in queue_arg.split(",") if q.strip()]
            return [queue_arg]

        # Default to "default" queue
        return ["default"]

    def _init_local_ray(self) -> None:
        """Initialize a local Ray instance."""
        import os
        import sys

        import ray

        # Clear RAY_ADDRESS to ensure we start a fresh local instance
        if "RAY_ADDRESS" in os.environ:
            self.stdout.write(
                self.style.WARNING(
                    f"Clearing RAY_ADDRESS={os.environ['RAY_ADDRESS']} for local mode"
                )
            )
            del os.environ["RAY_ADDRESS"]

        # Disable Ray's uv runtime env hook - it causes issues on Windows
        # when Ray tries to spawn workers with 'uv run' which may not be in PATH
        if "RAY_RUNTIME_ENV_HOOK" in os.environ:
            del os.environ["RAY_RUNTIME_ENV_HOOK"]

        if not ray.is_initialized():
            self.stdout.write("Initializing local Ray instance...")
            ray.init(
                ignore_reinit_error=True,
                # Enable dashboard with task visibility
                dashboard_host="127.0.0.1",
                dashboard_port=8265,
                include_dashboard=True,
                # Use the current Python executable for workers
                runtime_env={"env_vars": {"PYTHONPATH": os.pathsep.join(sys.path)}},
                # Enable task/actor events for dashboard
                _system_config={
                    "enable_timeline": True,
                    "task_events_report_interval_ms": 100,
                },
            )
            self.stdout.write(self.style.SUCCESS("Ray initialized"))
            self.stdout.write(self.style.SUCCESS("  Dashboard: http://127.0.0.1:8265"))

    def _init_cluster_ray(self, address: str) -> None:
        """Connect to a remote Ray cluster.

        Args:
            address: Ray cluster address (e.g., 'ray://localhost:10001')

        Raises:
            Exception: If connection fails (caller should handle retry)
        """
        import ray

        # Shutdown any existing connection first
        if ray.is_initialized():
            ray.shutdown()

        self.stdout.write(f"Connecting to Ray cluster at {address}...")
        ray.init(
            address=address,
            ignore_reinit_error=True,
        )
        self.stdout.write(self.style.SUCCESS("Connected to Ray cluster"))
        # Show cluster resources
        resources = ray.cluster_resources()
        self.stdout.write(f"  Cluster resources: {resources}")

    def _create_lease(self, queue: str) -> None:
        """Create a worker lease for distributed coordination.

        The lease tracks active workers and enables detection of
        crashed workers through heartbeat expiration.

        Args:
            queue: The queue this worker is processing.
        """
        import os
        import socket

        from django.utils import timezone

        # Store queue for potential lease recreation
        self.lease_queue_name = queue

        try:
            # Use update_or_create in case this worker_id already exists
            # (e.g., from a previous run that didn't clean up properly)
            self.lease, created = TaskWorkerLease.objects.update_or_create(
                worker_id=self.worker_id,
                defaults={
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "queue_name": queue,
                    "last_heartbeat_at": timezone.now(),
                    "is_active": True,
                    "stopped_at": None,
                },
            )
            action = "created" if created else "reactivated"
            self.stdout.write(self.style.SUCCESS(f"  Lease {action}: {self.worker_id}"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Failed to create lease: {e}"))
            # Continue without lease - worker will still function

    def setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self.handle_shutdown_signal)
        signal.signal(signal.SIGINT, self.handle_shutdown_signal)

    def _write_worker_output(self, message: str, *, minimum_verbosity: int = 1) -> None:
        """Write informational worker output when Django verbosity permits it."""
        if self.verbosity >= minimum_verbosity:
            self.stdout.write(message)

    def handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        """Handle shutdown signals."""
        del frame
        if self.shutdown_requested:
            return
        self.stdout.write(self.style.WARNING(f"\nReceived signal {signum}, shutting down..."))
        self.shutdown_requested = True
        self.shutdown_signal = signum
        self.shutdown_exit_code = 128 + signum

    def run_loop(
        self,
        queues: Sequence[str],
        concurrency: int,
        heartbeat_interval: float,
    ) -> None:
        """Run the main worker loop.

        Args:
            queues: Sequence of queue names to process (not modified).
            concurrency: Maximum concurrent tasks.
            heartbeat_interval: Seconds between heartbeats.
        """
        last_heartbeat = 0.0

        while not self.shutdown_requested:
            current_time = time.time()

            # Heartbeat
            if current_time - last_heartbeat >= heartbeat_interval:
                self.send_heartbeat()
                last_heartbeat = current_time

            # Poll for completed Ray Core tasks (local/cluster modes)
            if self.execution_mode in ("local", "cluster") and self.ray_core_runner:
                self.poll_ray_core_tasks()

            # A signal may arrive while heartbeat/polling is in progress.  Do
            # not claim another task once shutdown has begun.
            if self.shutdown_requested:
                break

            # Claim and process tasks from all queues
            self.claim_and_process_tasks(queues, concurrency)

            # Reconcile stuck tasks (periodically)
            if current_time - self.last_reconciliation >= self.reconciliation_interval:
                self.reconcile_tasks()
                self.detect_stuck_tasks()
                self.process_cancellations()
                self.cleanup_expired_leases()
                self.last_reconciliation = current_time

            # Sleep briefly to avoid busy-waiting
            time.sleep(0.1)

    def send_heartbeat(self) -> None:
        """Send worker heartbeat, update lease, and check Ray connection."""
        from django.utils import timezone

        # Update worker lease if we have one, or try to create one if missing
        if self.lease is not None:
            try:
                # Refresh from DB to check if lease still exists
                self.lease.refresh_from_db()

                # Check if lease was marked inactive (by cleanup or manually)
                if not self.lease.is_active:
                    self.stdout.write(
                        self.style.WARNING("\nLease was marked inactive, reactivating...")
                    )
                    self._recreate_lease()
                else:
                    # Normal heartbeat update
                    self.lease.last_heartbeat_at = timezone.now()
                    self.lease.save(update_fields=["last_heartbeat_at"])
            except TaskWorkerLease.DoesNotExist:
                # Lease was deleted - recreate it
                self.stdout.write(self.style.WARNING("\nLease was deleted, recreating..."))
                self._recreate_lease()
            except Exception as e:
                # Database error - try to recreate lease on next heartbeat
                self.stdout.write(self.style.WARNING(f"\nHeartbeat failed: {e}"))
        else:
            # No lease exists - try to create one
            self._recreate_lease()

        # Check Ray connection health for local/cluster modes
        if self.execution_mode in ("local", "cluster"):
            self._check_ray_connection()

        # Periodic status output (every ~60 seconds based on 15s heartbeat)
        if hasattr(self, "_heartbeat_count"):
            self._heartbeat_count += 1
        else:
            self._heartbeat_count = 1

        if self._heartbeat_count % 4 == 0:  # Every 4th heartbeat (~60 seconds)
            ray_core_pending = self.ray_core_runner.pending_count if self.ray_core_runner else 0
            active = len(self.active_tasks) + ray_core_pending
            idle_time = (
                time.time() - self.last_task_processed if self.last_task_processed > 0 else 0
            )
            self.stdout.write(
                f"\n[Status] tasks_processed={self.tasks_processed_count}, "
                f"active={active}, idle={idle_time:.0f}s"
            )
        else:
            self.stdout.write(".", ending="")
        self.stdout.flush()

    def _recreate_lease(self) -> None:
        """Recreate the worker lease after it was deleted or marked inactive."""
        import os
        import socket

        from django.utils import timezone

        queue_name = getattr(self, "lease_queue_name", "default")

        try:
            # Use update_or_create to handle race conditions
            # This will reactivate an inactive lease or create a new one
            self.lease, created = TaskWorkerLease.objects.update_or_create(
                worker_id=self.worker_id,
                defaults={
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "queue_name": queue_name,
                    "last_heartbeat_at": timezone.now(),
                    "is_active": True,
                    "stopped_at": None,
                },
            )
            action = "created" if created else "reactivated"
            self.stdout.write(self.style.SUCCESS(f"  Lease {action}: {self.worker_id}"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Failed to recreate lease: {e}"))

    def _update_lease_heartbeat(self) -> None:
        """Update lease heartbeat without full heartbeat logic.

        This is called before each task execution to ensure the lease
        doesn't expire during long-running tasks.
        """
        from django.utils import timezone

        if self.lease is None:
            return

        try:
            TaskWorkerLease.objects.filter(worker_id=self.worker_id).update(
                last_heartbeat_at=timezone.now()
            )
        except Exception:
            # Best effort - will be handled by regular heartbeat
            pass

    def _mark_task_monitor_heartbeat(
        self,
        task: RayTaskExecution,
        *,
        now: datetime | None = None,
        ray_job_id: str | None = None,
        execution_generation: int | None = None,
    ) -> None:
        """Record that a running task is still being actively monitored."""
        heartbeat_time = now or datetime.now(UTC)
        filters: dict[str, Any] = {"pk": task.pk, "state": TaskState.RUNNING}
        if ray_job_id is not None:
            filters["ray_job_id"] = ray_job_id
        if execution_generation is not None:
            filters["execution_generation"] = execution_generation
        updated = RayTaskExecution.objects.filter(**filters).update(
            last_heartbeat_at=heartbeat_time
        )
        if updated:
            task.last_heartbeat_at = heartbeat_time

    def _completion_envelope_grace_expired(
        self,
        task: RayTaskExecution,
        *,
        now: datetime,
    ) -> bool:
        """Return whether a terminal Ray Job has waited too long for its envelope."""
        last_activity = task.last_heartbeat_at or task.started_at or task.created_at
        last_activity_dt: datetime = last_activity  # type: ignore[assignment]
        return now - last_activity_dt > get_stuck_timeout()

    @staticmethod
    def _is_valid_completion_envelope(result: Any) -> bool:
        """Validate the required shape before applying a completion envelope."""
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            return False
        if "result" not in result:
            return False

        if result["success"]:
            result_reference = result.get("result_reference")
            if result_reference is None:
                return True
            from django_ray.result_storage import is_valid_result_reference

            return is_valid_result_reference(result_reference)

        if not isinstance(result.get("error"), str):
            return False
        return all(
            value is None or isinstance(value, str)
            for key in ("traceback", "exception_type")
            for value in [result.get(key)]
        )

    def _get_ray_cluster_resources_with_timeout(
        self, timeout_seconds: float
    ) -> dict[str, Any] | None:
        """Return Ray cluster resources or None when the check times out."""
        import queue
        import threading

        import ray

        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def _check_resources() -> None:
            try:
                result_queue.put(("ok", ray.cluster_resources()))
            except Exception as e:
                result_queue.put(("error", e))

        thread = threading.Thread(target=_check_resources, daemon=True)
        thread.start()
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            return None

        status, payload = result_queue.get_nowait()
        if status == "error":
            raise payload
        return payload

    def _check_ray_connection(self) -> None:
        """Check if Ray connection is healthy and reconnect if needed."""
        import ray

        try:
            # Quick health check - try to get cluster resources with timeout
            if ray.is_initialized():
                resources = self._get_ray_cluster_resources_with_timeout(timeout_seconds=10)
                if resources is not None:
                    return  # Connection is healthy
                self.stdout.write(self.style.WARNING("\nRay health check timed out"))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"\nRay connection lost: {e}"))

        # Connection is broken or Ray is not initialized - try to reconnect
        self._reconnect_ray()

    def _reconnect_ray(self) -> None:
        """Attempt to reconnect to Ray cluster."""
        import ray

        self.stdout.write(self.style.WARNING("\nAttempting to reconnect to Ray..."))

        # First, shutdown existing connection if any
        try:
            if ray.is_initialized():
                ray.shutdown()
                self.stdout.write("  Shut down existing Ray connection")
        except Exception as e:
            self.stdout.write(f"  Error during shutdown: {e}")

        # Wait a moment before reconnecting
        time.sleep(2)

        # Reconnect based on execution mode
        max_retries = 5
        retry_delay = 5  # seconds

        for attempt in range(1, max_retries + 1):
            try:
                if self.execution_mode == "local":
                    self._init_local_ray()
                elif self.execution_mode == "cluster" and self.cluster_address:
                    self._init_cluster_ray(self.cluster_address)

                # Verify connection
                if ray.is_initialized():
                    resources = ray.cluster_resources()
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"\n  Reconnected to Ray (attempt {attempt}/{max_retries})"
                        )
                    )
                    self.stdout.write(f"  Cluster resources: {resources}")

                    # Clear any stale Ray task references - they're invalid now
                    if self.ray_core_runner and self.ray_core_runner.pending_count > 0:
                        stale_count = self.ray_core_runner.pending_count
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Clearing {stale_count} stale Ray task references"
                            )
                        )
                        # Mark these tasks as LOST so they can be retried
                        self._mark_stale_ray_core_tasks_as_lost()

                    # Reinitialize the runner with the new connection
                    self.ray_core_runner = RayCoreRunner()

                    return  # Success!

            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Reconnection attempt {attempt}/{max_retries} failed: {e}"
                    )
                )
                if attempt < max_retries:
                    self.stdout.write(f"  Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)  # Exponential backoff, max 60s

        self.stdout.write(
            self.style.ERROR(
                f"\n  Failed to reconnect after {max_retries} attempts. "
                "Worker will continue but Ray tasks will fail."
            )
        )

    def _mark_stale_ray_core_tasks_as_lost(self) -> None:
        """Route stale Ray Core references through the normal retry/failure path."""
        if not self.ray_core_runner or self.ray_core_runner.pending_count == 0:
            return

        task_ids = list(self.ray_core_runner.pending_task_ids)
        count = 0
        for task in RayTaskExecution.objects.filter(
            pk__in=task_ids,
            state=TaskState.RUNNING,
        ):
            self._handle_task_failure(
                task,
                error_message="Ray connection lost - task state unknown",
                exception_type="RayConnectionError",
            )
            count += 1

        # Clear the runner's pending tasks
        self.ray_core_runner.clear_pending_tasks()

        if count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"  Routed {count} stale Ray Core task(s) through retry/failure handling"
                )
            )

    def claim_and_process_tasks(self, queues: Sequence[str], concurrency: int) -> None:
        """Claim and submit tasks for execution.

        Args:
            queues: Sequence of queue names to process (not modified).
            concurrency: Maximum concurrent tasks.
        """
        if self.shutdown_requested:
            return

        # Check how many slots are available
        ray_core_pending = self.ray_core_runner.pending_count if self.ray_core_runner else 0
        active_count = len(self.active_tasks) + ray_core_pending
        available_slots = concurrency - active_count
        if available_slots <= 0:
            return

        # Claim tasks from any of the specified queues
        now = datetime.now(UTC)

        from django.db.models import Q

        with transaction.atomic():
            # A single query keeps immediate and delayed/retried work in the same
            # priority order. Queue names only select workload-isolation boundaries.
            tasks = list(
                RayTaskExecution.objects.select_for_update(skip_locked=True)
                .filter(
                    state=TaskState.QUEUED,
                    queue_name__in=queues,
                )
                .filter(Q(run_after__isnull=True) | Q(run_after__lte=now))
                .order_by("-priority", "created_at", "pk")[:available_slots]
            )

            for task in tasks:
                task.state = TaskState.RUNNING
                task.started_at = now
                task.last_heartbeat_at = now
                task.claimed_by_worker = self.worker_id
                task.execution_generation = int(task.execution_generation) + 1
                task.completion_data = None
                task.ray_job_id = None
                task.ray_address = None
                task.save(
                    update_fields=[
                        "state",
                        "started_at",
                        "last_heartbeat_at",
                        "claimed_by_worker",
                        "execution_generation",
                        "completion_data",
                        "ray_job_id",
                        "ray_address",
                    ]
                )

        # Process each claimed task
        for task in tasks:
            if self.shutdown_requested and self.execution_mode != "sync":
                self._handoff_unsubmitted_task(task)
                continue
            self.process_task(task)

    def _handoff_unsubmitted_task(self, task: RayTaskExecution) -> None:
        """Return a just-claimed task to durable reconciliation on shutdown."""
        updated = RayTaskExecution.objects.filter(
            pk=task.pk,
            state=TaskState.RUNNING,
            claimed_by_worker=self.worker_id,
        ).update(
            state=TaskState.QUEUED,
            started_at=None,
            claimed_by_worker=None,
            last_heartbeat_at=None,
            ray_job_id=None,
            ray_address=None,
        )
        if updated:
            self.stdout.write(
                self.style.NOTICE(f"  Task {task.pk} handed off before remote submission")
            )

    def process_task(self, task: RayTaskExecution) -> None:
        """Process a single task."""
        self.stdout.write(self.style.NOTICE(f"\nProcessing task {task.pk}: {task.callable_path}"))

        # Update heartbeat before task execution to prevent lease expiration
        # during long-running tasks
        self._update_lease_heartbeat()

        # Track task processing
        self.last_task_processed = time.time()
        self.tasks_processed_count += 1

        if self.execution_mode == "sync":
            # Execute without Ray - purely synchronous
            self.execute_task_sync(task)
        elif self.execution_mode in ("local", "cluster"):
            # Submit to Ray cluster via @ray.remote (RayCoreRunner)
            # Tasks run on Ray workers, enabling distributed computing
            self.submit_task_to_ray_core(task)
        else:
            # Submit via Ray Job Submission API (process isolation)
            self.submit_task_to_ray(task)

    def execute_task_sync(self, task: RayTaskExecution) -> None:
        """Execute a task synchronously (without Ray)."""
        from django_ray.runtime.entrypoint import execute_task

        try:
            result_json = execute_task(
                callable_path=task.callable_path,
                serialized_args=task.args_json,
                serialized_kwargs=task.kwargs_json,
            )
            result = json.loads(result_json)

            if result["success"]:
                self._store_task_result(task, result["result"])
                self.stdout.write(
                    self.style.SUCCESS(f"  Task {task.pk} succeeded: {result['result']}")
                )
                if not succeed_task(
                    task,
                    result_data=task.result_data,
                    result_reference=task.result_reference,
                ):
                    return
            else:
                # Task failed - check if we should retry
                self._handle_task_failure(
                    task,
                    error_message=result["error"],
                    error_traceback=result.get("traceback"),
                    exception_type=result.get("exception_type"),
                )

        except Exception as e:
            self._handle_task_failure(
                task,
                error_message=str(e),
                exception_type=type(e).__name__,
            )

    def _store_task_result(self, task: RayTaskExecution, result_value: Any) -> None:
        """Store task result inline or as a reference if result is too large.

        Args:
            task: The task to update with result fields.
            result_value: The Python value to serialize and store.
        """
        from django_ray.result_storage import (
            DigestResultStorage,
            ResultStorageError,
            get_result_storage_backend,
        )

        settings = get_settings()
        max_result_size = int(settings.get("MAX_RESULT_SIZE_BYTES", 1024 * 1024))

        serialized_result = json.dumps(result_value)
        result_size_bytes = len(serialized_result.encode("utf-8"))

        if result_size_bytes <= max_result_size:
            task.result_data = serialized_result
            task.result_reference = None
            return

        task.result_data = None

        try:
            backend = get_result_storage_backend(settings)
            task.result_reference = backend.store(serialized_result=serialized_result)
        except ResultStorageError as e:
            # Preserve success semantics if external storage is unavailable.
            task.result_reference = DigestResultStorage().store(serialized_result=serialized_result)
            self.stdout.write(
                self.style.WARNING(
                    f"  Result storage backend failed ({e}); "
                    "falling back to digest-only result_reference"
                )
            )

        self.stdout.write(
            self.style.WARNING(
                f"  Task {task.pk} result is {result_size_bytes} bytes "
                f"(max={max_result_size}); stored result_reference"
            )
        )

    def _handle_task_failure(
        self,
        task: RayTaskExecution,
        error_message: str,
        error_traceback: str | None = None,
        exception_type: str | None = None,
        *,
        expected_ray_job_id: str | None = None,
        expected_execution_generation: int | None = None,
    ) -> bool:
        """Handle a failed task, potentially scheduling a retry.

        Args:
            task: The failed task.
            error_message: The error message.
            error_traceback: The full traceback (optional).
            exception_type: The exception class name (optional).
        """
        # Check if we should retry
        retry_decision = should_retry(task, exception_type)

        handled = record_failure(
            task,
            error_message=error_message,
            error_traceback=error_traceback,
            retry=retry_decision.should_retry,
            next_attempt_at=retry_decision.next_attempt_at,
            expected_ray_job_id=expected_ray_job_id,
            expected_execution_generation=expected_execution_generation,
        )
        if not handled:
            return False
        if retry_decision.should_retry:
            self.stdout.write(
                self.style.WARNING(
                    f"  Task {task.pk} failed, scheduling retry "
                    f"at {retry_decision.next_attempt_at}: {error_message}"
                )
            )
        else:
            reason = retry_decision.reason or "No retry configured"
            self.stdout.write(
                self.style.ERROR(f"  Task {task.pk} failed permanently ({reason}): {error_message}")
            )
        return True

    def submit_task_to_ray_core(self, task: RayTaskExecution) -> None:
        """Submit a task to Ray via @ray.remote (RayCoreRunner).

        This submits tasks to Ray workers using Ray Core remote functions,
        providing lower latency than Ray Job API while still executing
        on the Ray cluster.

        Args:
            task: The task execution to submit.
        """
        import ray

        from django_ray.runtime.serialization import deserialize_args

        # Ensure Ray is connected and runner is available
        if not ray.is_initialized():
            self.stdout.write(self.style.WARNING("  Ray not initialized, attempting to connect..."))
            self._reconnect_ray()

            if not ray.is_initialized():
                self._handle_task_failure(
                    task,
                    error_message="Ray cluster not available",
                    exception_type="RayConnectionError",
                )
                return

        # Ensure runner is initialized
        if self.ray_core_runner is None:
            self.ray_core_runner = RayCoreRunner()

        try:
            args = deserialize_args(task.args_json)
            kwargs = deserialize_args(task.kwargs_json)

            handle = self.ray_core_runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=tuple(args),
                kwargs=kwargs,
            )

            # Update task with tracking info
            task.ray_job_id = handle.ray_job_id  # "ray_core:{pk}"
            task.ray_address = handle.ray_address
            task.save(update_fields=["ray_job_id", "ray_address"])

            self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} submitted to Ray Core (async)"))

        except Exception as e:
            import traceback

            self._handle_task_failure(
                task,
                error_message=f"Failed to submit to Ray Core: {e}",
                error_traceback=traceback.format_exc(),
                exception_type=type(e).__name__,
            )

    def poll_ray_core_tasks(self) -> None:
        """Poll for completed Ray Core tasks and update their status.

        Uses RayCoreRunner.poll_completed() for efficient batch polling.
        """
        if self.ray_core_runner is None or self.ray_core_runner.pending_count == 0:
            return

        import ray

        # Check if Ray is still connected
        if not ray.is_initialized():
            self.stdout.write(self.style.WARNING("\nRay disconnected, clearing pending tasks..."))
            # Mark all pending tasks as needing retry
            for task_pk in self.ray_core_runner.pending_task_ids:
                try:
                    task = RayTaskExecution.objects.get(pk=task_pk)
                    self._handle_task_failure(
                        task,
                        error_message="Ray connection lost",
                        exception_type="RayConnectionError",
                    )
                except RayTaskExecution.DoesNotExist:
                    pass
            self.ray_core_runner.clear_pending_tasks()
            return

        monitored_task_ids = list(self.ray_core_runner.pending_task_ids)
        monitor_time = time.monotonic()
        if (
            monitored_task_ids
            and monitor_time - self.last_task_monitor_heartbeat
            >= self.task_monitor_heartbeat_interval
        ):
            heartbeat_time = datetime.now(UTC)
            RayTaskExecution.objects.filter(
                pk__in=monitored_task_ids,
                state=TaskState.RUNNING,
            ).update(last_heartbeat_at=heartbeat_time)
            self.last_task_monitor_heartbeat = monitor_time

        # Poll for completed tasks
        try:
            completed = self.ray_core_runner.poll_completed()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\nError polling Ray Core tasks: {e}"))
            return

        for task_pk, result_json in completed:
            try:
                task = RayTaskExecution.objects.get(pk=task_pk)

                # Skip if task was cancelled externally
                if task.state in (TaskState.CANCELLED, TaskState.CANCELLING):
                    if task.state == TaskState.CANCELLING:
                        task.state = TaskState.CANCELLED
                        task.finished_at = datetime.now(UTC)
                        task.save(update_fields=["state", "finished_at"])
                    self.stdout.write(self.style.WARNING(f"\n  Task {task.pk} was cancelled"))
                    continue

                result = json.loads(result_json)

                if result.get("success"):
                    self._store_task_result(task, result.get("result"))
                    if not succeed_task(
                        task,
                        result_data=task.result_data,
                        result_reference=task.result_reference,
                    ):
                        return
                    self.stdout.write(
                        self.style.SUCCESS(f"\n  Task {task.pk} completed: {result.get('result')}")
                    )
                else:
                    self._handle_task_failure(
                        task,
                        error_message=result.get("error", "Unknown error"),
                        error_traceback=result.get("traceback"),
                        exception_type=result.get("exception_type"),
                    )

            except RayTaskExecution.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"\n  Task {task_pk} not found in database"))
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"\n  Error processing task {task_pk} result: {e}")
                )

    def submit_task_to_ray(self, task: RayTaskExecution) -> None:
        """Submit a task to Ray for execution."""
        from django_ray.runner.ray_job import RayJobRunner
        from django_ray.runtime.serialization import deserialize_args

        try:
            runner = RayJobRunner()
            args = deserialize_args(task.args_json)
            kwargs = deserialize_args(task.kwargs_json)

            handle = runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=tuple(args),
                kwargs=kwargs,
            )

            # Update task with Ray job info
            task.ray_job_id = handle.ray_job_id
            task.ray_address = handle.ray_address
            task.save(update_fields=["ray_job_id", "ray_address"])

            # Track active task
            self.active_tasks[task.pk] = handle.ray_job_id

            self.stdout.write(
                self.style.SUCCESS(f"  Task {task.pk} submitted as Ray job {handle.ray_job_id}")
            )

        except Exception as e:
            import traceback

            self._handle_task_failure(
                task,
                error_message=f"Failed to submit to Ray: {e}",
                error_traceback=traceback.format_exc(),
                exception_type=type(e).__name__,
            )

    def _build_submission_handle(
        self,
        task: RayTaskExecution,
        ray_job_id: str,
    ) -> SubmissionHandle:
        """Build a submission handle from persisted task metadata."""
        return SubmissionHandle(
            ray_job_id=ray_job_id,
            ray_address=task.ray_address or "",
            submitted_at=task.started_at or datetime.now(UTC),
        )

    def _adopt_orphaned_ray_job_task(self, task: RayTaskExecution, *, now: datetime) -> bool:
        """Take ownership of an orphaned Ray Job task for continued reconciliation."""
        filter_kwargs: dict[str, Any] = {
            "pk": task.pk,
            "state": TaskState.RUNNING,
            "ray_job_id": task.ray_job_id,
        }
        if task.claimed_by_worker:
            filter_kwargs["claimed_by_worker"] = task.claimed_by_worker
        else:
            filter_kwargs["claimed_by_worker__isnull"] = True

        updated = RayTaskExecution.objects.filter(**filter_kwargs).update(
            claimed_by_worker=self.worker_id,
            last_heartbeat_at=now,
        )
        if not updated:
            return False

        task.claimed_by_worker = self.worker_id
        task.last_heartbeat_at = now
        if task.ray_job_id:
            self.active_tasks[task.pk] = str(task.ray_job_id)
        return True

    def _reconcile_ray_job_task(
        self,
        task: RayTaskExecution,
        runner: Any,
        *,
        ray_job_id: str,
        completed_tasks: list[int],
        orphaned: bool,
    ) -> None:
        """Reconcile a single Ray Job task from either active or orphaned tracking."""
        from django_ray.runner.base import JobStatus

        # Skip reconciliation if task was cancelled externally.
        if task.state in (TaskState.CANCELLED, TaskState.CANCELLING):
            if task.state == TaskState.CANCELLING:
                task.state = TaskState.CANCELLED
                task.finished_at = datetime.now(UTC)
                task.save(update_fields=["state", "finished_at"])
            completed_tasks.append(task.pk)
            self.stdout.write(self.style.WARNING(f"\nTask {task.pk} was cancelled"))
            return

        handle = self._build_submission_handle(task, ray_job_id)
        job_info = runner.get_status(handle)
        now = datetime.now(UTC)

        # Refresh after the status RPC: the execution may have been retried or
        # claimed elsewhere while the RPC was in flight.
        try:
            task.refresh_from_db(
                fields=["state", "completion_data", "ray_job_id", "execution_generation"]
            )
        except RayTaskExecution.DoesNotExist:
            completed_tasks.append(task.pk)
            return

        # Never reconcile an old Ray Job against a replacement execution.
        if str(task.ray_job_id or "") != ray_job_id:
            if self.active_tasks.get(task.pk) == ray_job_id:
                self.active_tasks.pop(task.pk, None)
            return

        if job_info.status in (JobStatus.PENDING, JobStatus.RUNNING):
            self._mark_task_monitor_heartbeat(
                task,
                now=now,
                ray_job_id=ray_job_id,
                execution_generation=task.execution_generation,
            )
            if orphaned and self._adopt_orphaned_ray_job_task(task, now=now):
                self.stdout.write(
                    self.style.NOTICE(
                        f"\nAdopted orphaned Ray job task {task.pk} for continued monitoring"
                    )
                )
            return

        completion_data: str | None = None
        if job_info.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED):
            if task.state in (TaskState.CANCELLED, TaskState.CANCELLING):
                if task.state == TaskState.CANCELLING:
                    task.state = TaskState.CANCELLED
                    task.finished_at = now
                    task.save(update_fields=["state", "finished_at"])
                completed_tasks.append(task.pk)
                return
            if task.state != TaskState.RUNNING:
                completed_tasks.append(task.pk)
                return

            completion_data = task.completion_data

        if completion_data:
            completion_error: str | None = None
            try:
                result = json.loads(completion_data)
            except (TypeError, json.JSONDecodeError):
                completion_error = "malformed"

            if completion_error is None and not self._is_valid_completion_envelope(result):
                completion_error = "invalid"

            if completion_error is not None:
                if self._completion_envelope_grace_expired(task, now=now):
                    handled = self._handle_task_failure(
                        task,
                        error_message=f"Ray Job produced a {completion_error} completion envelope",
                        exception_type="RayCompletionMalformed",
                        expected_ray_job_id=ray_job_id,
                        expected_execution_generation=task.execution_generation,
                    )
                    if handled is False:
                        return
                    completed_tasks.append(task.pk)
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} exceeded the completion envelope grace period"
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} has a {completion_error} completion envelope; "
                            "waiting for a valid update"
                        )
                    )
                return

            if result["success"]:
                if result.get("result_reference"):
                    task.result_data = None
                    task.result_reference = result["result_reference"]
                else:
                    self._store_task_result(task, result.get("result"))
                if not succeed_task(
                    task,
                    result_data=task.result_data,
                    result_reference=task.result_reference,
                    expected_ray_job_id=ray_job_id,
                    expected_execution_generation=task.execution_generation,
                ):
                    return
                self.stdout.write(self.style.SUCCESS(f"\nTask {task.pk} completed"))
            else:
                handled = self._handle_task_failure(
                    task,
                    error_message=result["error"],
                    error_traceback=result.get("traceback"),
                    exception_type=result.get("exception_type"),
                    expected_ray_job_id=ray_job_id,
                    expected_execution_generation=task.execution_generation,
                )
                if handled is False:
                    return
                self.stdout.write(
                    self.style.WARNING(
                        f"\nTask {task.pk} returned failure envelope, handling via retry policy"
                    )
                )
            completed_tasks.append(task.pk)
            return

        if job_info.status == JobStatus.SUCCEEDED:
            # Ray Job logs are diagnostic only. Missing envelopes remain
            # non-terminal until the bounded grace period expires.
            if self._completion_envelope_grace_expired(task, now=now):
                handled = self._handle_task_failure(
                    task,
                    error_message="Ray Job completed without a completion envelope",
                    exception_type="RayCompletionUnknown",
                    expected_ray_job_id=ray_job_id,
                    expected_execution_generation=task.execution_generation,
                )
                if handled is False:
                    return
                completed_tasks.append(task.pk)
                self.stdout.write(
                    self.style.WARNING(
                        f"\nTask {task.pk} exceeded the completion envelope grace period"
                    )
                )
                return
            self.stdout.write(
                self.style.NOTICE(
                    f"\nTask {task.pk} Ray job succeeded; waiting for completion envelope"
                )
            )
            return

        if job_info.status == JobStatus.FAILED:
            logs = runner.get_logs(handle)
            handled = self._handle_task_failure(
                task,
                error_message=job_info.message or "Ray job failed",
                error_traceback=logs,
                exception_type="RayJobFailed",
                expected_ray_job_id=ray_job_id,
                expected_execution_generation=task.execution_generation,
            )
            if handled is False:
                return
            completed_tasks.append(task.pk)
            self.stdout.write(self.style.ERROR(f"\nTask {task.pk} failed: {job_info.message}"))
            return

        if job_info.status == JobStatus.STOPPED:
            updated = RayTaskExecution.objects.filter(
                pk=task.pk,
                state=TaskState.RUNNING,
                ray_job_id=ray_job_id,
                execution_generation=task.execution_generation,
            ).update(state=TaskState.CANCELLED, finished_at=now)
            if not updated:
                return
            task.state = TaskState.CANCELLED
            task.finished_at = now
            completed_tasks.append(task.pk)
            self.stdout.write(self.style.WARNING(f"\nTask {task.pk} was stopped"))
            return

        scope = "orphaned " if orphaned else ""
        self.stdout.write(
            self.style.WARNING(
                f"\nTask {task.pk} {scope}Ray job status is unknown: {job_info.message or 'no details'}"
            )
        )

    def _request_timeout_cancellation(self, task: RayTaskExecution) -> CancellationOutcome:
        """Stop a timed-out remote Ray Job before making its row terminal."""
        ray_job_id = str(task.ray_job_id or "")
        if not ray_job_id or not ray_job_id.startswith("raysubmit_"):
            return CancellationOutcome(CancellationOutcomeStatus.NOT_APPLICABLE)

        try:
            from django_ray.runner.ray_job import RayJobRunner

            runner = RayJobRunner()
            handle = self._build_submission_handle(task, ray_job_id)
            return request_remote_cancellation(runner, handle)
        except Exception as exc:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Could not create or call the Ray Job cancellation client: {exc}",
            )

    def reconcile_tasks(self) -> None:
        """Reconcile task states with Ray."""
        if self.sync_mode:
            return

        from django_ray.runner.leasing import get_active_workers
        from django_ray.runner.ray_job import RayJobRunner

        runner = RayJobRunner()
        completed_tasks: list[int] = []
        reconciled_task_ids: set[int] = set()

        for task_pk, ray_job_id in list(self.active_tasks.items()):
            try:
                task = RayTaskExecution.objects.get(pk=task_pk)
                reconciled_task_ids.add(task.pk)
                self._reconcile_ray_job_task(
                    task,
                    runner,
                    ray_job_id=ray_job_id,
                    completed_tasks=completed_tasks,
                    orphaned=False,
                )

            except RayTaskExecution.DoesNotExist:
                completed_tasks.append(task_pk)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"\nError reconciling task {task_pk}: {e}"))

        active_worker_ids = {str(lease.worker_id) for lease in get_active_workers()}
        orphaned_tasks = RayTaskExecution.objects.filter(
            state=TaskState.RUNNING,
            ray_job_id__startswith="raysubmit_",
        ).exclude(pk__in=reconciled_task_ids)

        for task in orphaned_tasks:
            task_worker_id = str(task.claimed_by_worker) if task.claimed_by_worker else None
            if task_worker_id == self.worker_id:
                continue
            if task_worker_id and task_worker_id in active_worker_ids:
                continue

            try:
                self._reconcile_ray_job_task(
                    task,
                    runner,
                    ray_job_id=str(task.ray_job_id or ""),
                    completed_tasks=completed_tasks,
                    orphaned=True,
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"\nError reconciling orphaned task {task.pk}: {e}")
                )

        # Remove completed tasks from active list
        for task_pk in completed_tasks:
            tracked_ray_job_id = self.active_tasks.get(task_pk)
            if tracked_ray_job_id is None:
                continue
            current = (
                RayTaskExecution.objects.filter(pk=task_pk).values("state", "ray_job_id").first()
            )
            if current is None or current["state"] != TaskState.RUNNING:
                self.active_tasks.pop(task_pk, None)
            elif str(current["ray_job_id"] or "") == tracked_ray_job_id:
                self.active_tasks.pop(task_pk, None)

    def detect_stuck_tasks(self) -> None:
        """Detect and mark stuck tasks as LOST.

        This checks for tasks that have been RUNNING for too long without
        heartbeats, which indicates the worker processing them may have crashed.
        """
        from django_ray.runner.leasing import get_active_workers

        # Check all running tasks. For tasks owned by active workers, skip recovery
        # and let the owning worker manage its own in-flight work.
        running_tasks = RayTaskExecution.objects.filter(
            state=TaskState.RUNNING,
        )

        active_worker_ids = {str(lease.worker_id) for lease in get_active_workers()}
        ray_core_pending = (
            set(self.ray_core_runner.pending_task_ids) if self.ray_core_runner else set()
        )

        stuck_count = 0
        timeout_count = 0
        orphan_recovered_count = 0
        for task in running_tasks:
            task_worker_id = str(task.claimed_by_worker) if task.claimed_by_worker else None
            claimed_by_this_worker = task_worker_id == self.worker_id
            claimed_by_active_worker = bool(task_worker_id) and task_worker_id in active_worker_ids

            # Leave tasks owned by healthy workers alone.
            if not claimed_by_this_worker and claimed_by_active_worker:
                continue

            # Check for timeout first (applies to all tasks)
            if is_task_timed_out(task):
                self.stdout.write(
                    self.style.WARNING(f"\nTask {task.pk} timed out after {task.timeout_seconds}s")
                )
                # Cancel the running task if we're tracking it
                if self.ray_core_runner and task.pk in self.ray_core_runner.pending_task_ids:
                    self.ray_core_runner.cancel(
                        SubmissionHandle(
                            ray_job_id=f"ray_core:{task.pk}",
                            ray_address="",
                            submitted_at=task.started_at or datetime.now(UTC),
                        )
                    )

                # A Ray Job is not represented in ray_core_runner. Request its
                # remote stop first, then conditionally finalize the timeout so
                # a concurrent completion cannot be overwritten.
                cancellation = self._request_timeout_cancellation(task)
                marked_timed_out = mark_task_timed_out(
                    task,
                    cancellation_status=CancellationStatus(cancellation.status.value),
                    cancellation_error=cancellation.message,
                    expected_ray_job_id=str(task.ray_job_id) if task.ray_job_id else None,
                    expected_execution_generation=task.execution_generation,
                )
                if marked_timed_out:
                    if task.pk in self.active_tasks:
                        del self.active_tasks[task.pk]
                    timeout_count += 1
                    if not claimed_by_this_worker:
                        orphan_recovered_count += 1
                else:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"\nTask {task.pk} completed while timeout cancellation was in flight"
                        )
                    )
                continue

            tracked_by_this_worker = claimed_by_this_worker and (
                task.pk in ray_core_pending or task.pk in self.active_tasks
            )

            # Skip tasks we are actively monitoring while their monitor heartbeat is fresh.
            if tracked_by_this_worker and not is_task_stuck(task):
                continue

            # Check if task is stuck using the reconciliation logic
            if is_task_stuck(task):
                if claimed_by_this_worker:
                    self.stdout.write(
                        self.style.WARNING(f"\nTask {task.pk} appears stuck, marking as LOST")
                    )
                else:
                    owner = task_worker_id or "unknown-worker"
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} from inactive worker {owner} appears stuck, "
                            "marking as LOST"
                        )
                    )

                mark_task_lost(task)

                # Check if we should retry the lost task
                retry_decision = should_retry(task, exception_type="TaskLost")
                if retry_decision.should_retry:
                    task.state = TaskState.QUEUED
                    task.attempt_number += 1
                    task.run_after = retry_decision.next_attempt_at
                    task.started_at = None
                    task.finished_at = None
                    task.claimed_by_worker = None
                    task.save(
                        update_fields=[
                            "state",
                            "attempt_number",
                            "run_after",
                            "started_at",
                            "finished_at",
                            "claimed_by_worker",
                        ]
                    )
                    self.stdout.write(
                        self.style.NOTICE(
                            f"  Scheduling retry #{task.attempt_number} "
                            f"at {retry_decision.next_attempt_at}"
                        )
                    )

                stuck_count += 1
                if not claimed_by_this_worker:
                    orphan_recovered_count += 1

        if stuck_count > 0:
            self.stdout.write(self.style.WARNING(f"Detected {stuck_count} stuck task(s)"))
        if timeout_count > 0:
            self.stdout.write(self.style.WARNING(f"Detected {timeout_count} timed out task(s)"))
        if orphan_recovered_count > 0:
            self.stdout.write(
                self.style.NOTICE(
                    f"Recovered {orphan_recovered_count} task(s) from inactive or missing workers"
                )
            )

    def cleanup_expired_leases(self) -> None:
        """Clean up expired worker leases from other workers.

        This helps keep the TaskWorkerLease table clean by removing
        records from workers that have crashed without graceful shutdown.
        """
        from django_ray.runner.leasing import cleanup_expired_leases

        try:
            deleted_count = cleanup_expired_leases()
            if deleted_count > 0:
                self.stdout.write(
                    self.style.NOTICE(f"\nCleaned up {deleted_count} expired worker lease(s)")
                )
        except Exception as e:
            # Don't fail on lease cleanup errors
            self.logger.warning(f"Failed to cleanup expired leases: {e}")

    def _claim_orphaned_cancellation(
        self,
        task: RayTaskExecution,
        *,
        active_worker_ids: set[str],
        now: datetime,
    ) -> bool:
        """Conditionally take ownership of a cancellation from a dead worker."""
        owner = str(task.claimed_by_worker) if task.claimed_by_worker else None
        if owner == self.worker_id:
            return True
        if owner and owner in active_worker_ids:
            return False

        filters: dict[str, object] = {
            "pk": task.pk,
            "state": TaskState.CANCELLING,
        }
        if owner:
            filters["claimed_by_worker"] = owner
        else:
            filters["claimed_by_worker__isnull"] = True

        updated = RayTaskExecution.objects.filter(**filters).update(
            claimed_by_worker=self.worker_id,
            last_heartbeat_at=now,
        )
        if not updated:
            return False

        task.claimed_by_worker = self.worker_id
        task.last_heartbeat_at = now
        return True

    def _request_cancellation_for_task(self, task: RayTaskExecution) -> CancellationOutcome:
        """Best-effort cancellation using the backend recorded on the task."""
        ray_job_id = str(task.ray_job_id or "")
        handle = SubmissionHandle(
            ray_job_id=ray_job_id or f"ray_core:{task.pk}",
            ray_address=str(task.ray_address or ""),
            submitted_at=task.started_at or datetime.now(UTC),
        )

        if ray_job_id.startswith("raysubmit_"):
            try:
                from django_ray.runner.ray_job import RayJobRunner

                return request_remote_cancellation(RayJobRunner(), handle)
            except Exception as exc:
                return CancellationOutcome(
                    CancellationOutcomeStatus.INDETERMINATE,
                    f"Could not cancel Ray Job: {exc}",
                )

        if self.ray_core_runner is not None and (
            ray_job_id or task.pk in self.ray_core_runner.pending_task_ids
        ):
            try:
                accepted = self.ray_core_runner.cancel(handle)
            except Exception as exc:
                return CancellationOutcome(
                    CancellationOutcomeStatus.INDETERMINATE,
                    f"Could not cancel Ray Core task: {exc}",
                )
            if accepted:
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)
            return CancellationOutcome(
                CancellationOutcomeStatus.FAILED,
                "Ray Core cancellation API rejected the stop request",
            )

        if ray_job_id:
            return CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                "Ray Core runner unavailable while recovering cancellation",
            )

        return CancellationOutcome(CancellationOutcomeStatus.NOT_APPLICABLE)

    def process_cancellations(self) -> None:
        """Adopt and finalize cancellation requests left by dead workers."""
        from django_ray.runner.leasing import get_active_workers

        active_worker_ids = {str(lease.worker_id) for lease in get_active_workers()}
        cancelling_tasks = RayTaskExecution.objects.filter(state=TaskState.CANCELLING)

        for task in cancelling_tasks:
            now = datetime.now(UTC)
            if not self._claim_orphaned_cancellation(
                task,
                active_worker_ids=active_worker_ids,
                now=now,
            ):
                continue

            self.stdout.write(self.style.WARNING(f"\nFinalizing cancellation for task {task.pk}"))
            cancellation = self._request_cancellation_for_task(task)

            # Remove from our tracking if present. A stale completion callback
            # cannot overwrite this row because finalization is conditional on
            # both CANCELLING state and this worker's ownership.
            if task.pk in self.active_tasks:
                del self.active_tasks[task.pk]

            finalized = finalize_cancellation(
                task,
                expected_worker_id=self.worker_id,
                cancellation_status=CancellationStatus(cancellation.status.value),
                cancellation_error=cancellation.message,
            )
            if finalized:
                self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} cancelled"))

    def _prepare_shutdown_handoff(self) -> None:
        """Make in-flight work safe for the next worker before disconnecting.

        Synchronous execution is allowed to finish because it is already
        running in this process.  Ray Job submissions are durable by ID, so
        they remain running and are released for another worker to reconcile.
        Ray Core ObjectRefs belong to this driver; request cancellation and
        persist ``CANCELLING`` so a subsequent worker can finalize the row.
        """
        if self.execution_mode == "sync":
            return

        now = datetime.now(UTC)

        # Ray Core work cannot be recovered after this driver's Ray connection
        # is closed.  Ask Ray to stop it, then persist the cancellation intent.
        if self.execution_mode in ("local", "cluster") and self.ray_core_runner:
            for task_pk in self.ray_core_runner.pending_task_ids:
                try:
                    task = RayTaskExecution.objects.get(pk=task_pk)
                except RayTaskExecution.DoesNotExist:
                    continue
                if task.state != TaskState.RUNNING:
                    continue
                outcome = CancellationOutcome(CancellationOutcomeStatus.INDETERMINATE)
                try:
                    accepted = self.ray_core_runner.cancel(
                        SubmissionHandle(
                            ray_job_id=f"ray_core:{task.pk}",
                            ray_address="",
                            submitted_at=task.started_at or now,
                        )
                    )
                    outcome = CancellationOutcome(
                        CancellationOutcomeStatus.REQUESTED
                        if accepted
                        else CancellationOutcomeStatus.FAILED
                    )
                except Exception as exc:
                    outcome = CancellationOutcome(
                        CancellationOutcomeStatus.INDETERMINATE,
                        f"Ray Core shutdown cancellation failed: {exc}",
                    )
                RayTaskExecution.objects.filter(
                    pk=task.pk,
                    state=TaskState.RUNNING,
                    claimed_by_worker=self.worker_id,
                ).update(
                    state=TaskState.CANCELLING,
                    cancellation_status=CancellationStatus(outcome.status.value),
                    cancellation_error=outcome.message,
                )
                self.stdout.write(
                    self.style.WARNING(f"  Task {task.pk} marked CANCELLING during shutdown")
                )

        # Ray Jobs continue independently of this process.  Drop ownership so
        # another worker can adopt and reconcile their persisted job IDs.
        if self.execution_mode == "ray":
            for task_pk, ray_job_id in list(self.active_tasks.items()):
                updated = RayTaskExecution.objects.filter(
                    pk=task_pk,
                    state=TaskState.RUNNING,
                    claimed_by_worker=self.worker_id,
                    ray_job_id=ray_job_id,
                ).update(claimed_by_worker=None, last_heartbeat_at=now)
                if updated:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"  Ray Job task {task_pk} handed off for continued monitoring"
                        )
                    )
                self.active_tasks.pop(task_pk, None)

    def shutdown(self) -> None:
        """Perform graceful shutdown."""
        self._prepare_shutdown_handoff()
        # Mark worker lease as inactive to signal we're gone
        if self.lease is not None:
            try:
                from django_ray.runner.leasing import release_lease

                release_lease(self.worker_id)
                self.stdout.write("  Lease released (marked inactive)")
            except Exception as e:
                self.stdout.write(f"  Failed to release lease: {e}")

        # Disconnect from Ray cluster
        if self.execution_mode in ("local", "cluster"):
            try:
                import ray

                if ray.is_initialized():
                    ray.shutdown()
                    self.stdout.write("  Ray connection closed")
            except Exception as e:
                self.stdout.write(f"  Failed to close Ray connection: {e}")

        self.stdout.write(self.style.SUCCESS(f"\nWorker {self.worker_id} shut down cleanly"))
