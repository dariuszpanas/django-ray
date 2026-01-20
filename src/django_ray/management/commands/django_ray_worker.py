"""Django management command for running the django-ray worker."""

from __future__ import annotations

import json
import signal
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from django_ray.conf.settings import get_settings
from django_ray.logging import get_worker_logger
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import finalize_cancellation
from django_ray.runner.leasing import generate_worker_id, get_heartbeat_interval
from django_ray.runner.ray_core import RayCoreRunner
from django_ray.runner.reconciliation import (
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

        concurrency = options.get("concurrency")
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
            self.execution_mode = "ray"

        settings = get_settings()
        if concurrency is None:
            concurrency = settings.get("DEFAULT_CONCURRENCY", 10)

        self.setup_signal_handlers()

        self.stdout.write(self.style.SUCCESS(f"Starting django-ray worker {self.worker_id}"))
        self.stdout.write(f"  Queues: {', '.join(queues)}")
        self.stdout.write(f"  Concurrency: {concurrency}")
        self.stdout.write(f"  Mode: {self.execution_mode}")

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
            self.stdout.write("\nShutdown requested via keyboard interrupt")
        finally:
            self.shutdown()

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

    def handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        """Handle shutdown signals."""
        self.stdout.write(self.style.WARNING(f"\nReceived signal {signum}, shutting down..."))
        self.shutdown_requested = True

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

    def _check_ray_connection(self) -> None:
        """Check if Ray connection is healthy and reconnect if needed."""
        import concurrent.futures

        import ray

        def _check_resources():
            """Check cluster resources with timeout protection."""
            return ray.cluster_resources()

        try:
            # Quick health check - try to get cluster resources with timeout
            if ray.is_initialized():
                # Use a thread with timeout to avoid blocking forever
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_check_resources)
                    try:
                        future.result(timeout=10)  # 10 second timeout
                        return  # Connection is healthy
                    except concurrent.futures.TimeoutError:
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
        """Mark tasks with stale Ray Core references as LOST so they can be retried."""
        if not self.ray_core_runner or self.ray_core_runner.pending_count == 0:
            return

        task_ids = list(self.ray_core_runner._pending_tasks.keys())
        now = datetime.now(UTC)

        count = RayTaskExecution.objects.filter(
            pk__in=task_ids,
            state=TaskState.RUNNING,
        ).update(
            state=TaskState.LOST,
            finished_at=now,
            error_message="Ray connection lost - task state unknown",
        )

        # Clear the runner's pending tasks
        self.ray_core_runner._pending_tasks.clear()

        if count > 0:
            self.stdout.write(self.style.WARNING(f"  Marked {count} running tasks as LOST"))

    def claim_and_process_tasks(self, queues: Sequence[str], concurrency: int) -> None:
        """Claim and submit tasks for execution.

        Args:
            queues: Sequence of queue names to process (not modified).
            concurrency: Maximum concurrent tasks.
        """
        # Check how many slots are available
        ray_core_pending = self.ray_core_runner.pending_count if self.ray_core_runner else 0
        active_count = len(self.active_tasks) + ray_core_pending
        available_slots = concurrency - active_count
        if available_slots <= 0:
            return

        # Claim tasks from any of the specified queues
        now = datetime.now(UTC)

        # Build priority ordering: high-priority=0, default/normal=1, low-priority=2
        # This ensures high-priority tasks are processed first, then default, then low
        from django.db.models import Case, IntegerField, Value, When

        priority_order = Case(
            When(queue_name="high-priority", then=Value(0)),
            When(queue_name="urgent", then=Value(0)),
            When(queue_name="low-priority", then=Value(2)),
            When(queue_name="background", then=Value(2)),
            When(queue_name="batch", then=Value(2)),
            default=Value(1),  # default, ml, sync, and others get normal priority
            output_field=IntegerField(),
        )

        with transaction.atomic():
            # Find queued tasks that are ready to run (run_after is null)
            # Order by priority first, then by created_at for FIFO within same priority
            tasks = list(
                RayTaskExecution.objects.select_for_update(skip_locked=True)
                .filter(
                    state=TaskState.QUEUED,
                    queue_name__in=queues,
                )
                .filter(
                    # run_after is null OR run_after <= now
                    run_after__isnull=True,
                )
                .annotate(priority=priority_order)
                .order_by("priority", "created_at")[:available_slots]
            )

            # Also get tasks with run_after <= now
            if len(tasks) < available_slots:
                more_tasks = list(
                    RayTaskExecution.objects.select_for_update(skip_locked=True)
                    .filter(
                        state=TaskState.QUEUED,
                        queue_name__in=queues,
                        run_after__lte=now,
                    )
                    .annotate(priority=priority_order)
                    .order_by("priority", "created_at")[: available_slots - len(tasks)]
                )
                tasks.extend(more_tasks)

            for task in tasks:
                task.state = TaskState.RUNNING
                task.started_at = now
                task.claimed_by_worker = self.worker_id
                task.save(update_fields=["state", "started_at", "claimed_by_worker"])

        # Process each claimed task
        for task in tasks:
            self.process_task(task)

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

            now = datetime.now(UTC)
            if result["success"]:
                task.state = TaskState.SUCCEEDED
                task.result_data = json.dumps(result["result"])
                task.finished_at = now
                self.stdout.write(
                    self.style.SUCCESS(f"  Task {task.pk} succeeded: {result['result']}")
                )
                task.save(
                    update_fields=[
                        "state",
                        "result_data",
                        "finished_at",
                    ]
                )
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

    def _handle_task_failure(
        self,
        task: RayTaskExecution,
        error_message: str,
        error_traceback: str | None = None,
        exception_type: str | None = None,
    ) -> None:
        """Handle a failed task, potentially scheduling a retry.

        Args:
            task: The failed task.
            error_message: The error message.
            error_traceback: The full traceback (optional).
            exception_type: The exception class name (optional).
        """
        # Check if we should retry
        retry_decision = should_retry(task, exception_type)

        if retry_decision.should_retry:
            # Schedule retry
            task.state = TaskState.QUEUED
            task.attempt_number = int(task.attempt_number) + 1
            task.run_after = retry_decision.next_attempt_at
            task.error_message = error_message
            task.error_traceback = error_traceback
            task.started_at = None
            task.finished_at = None
            task.claimed_by_worker = None
            task.save(
                update_fields=[
                    "state",
                    "attempt_number",
                    "run_after",
                    "error_message",
                    "error_traceback",
                    "started_at",
                    "finished_at",
                    "claimed_by_worker",
                ]
            )
            self.stdout.write(
                self.style.WARNING(
                    f"  Task {task.pk} failed, scheduling retry #{task.attempt_number} "
                    f"at {retry_decision.next_attempt_at}: {error_message}"
                )
            )
        else:
            # Final failure
            task.state = TaskState.FAILED
            task.error_message = error_message
            task.error_traceback = error_traceback
            task.finished_at = datetime.now(UTC)
            task.save(
                update_fields=[
                    "state",
                    "error_message",
                    "error_traceback",
                    "finished_at",
                ]
            )
            reason = retry_decision.reason or "No retry configured"
            self.stdout.write(
                self.style.ERROR(f"  Task {task.pk} failed permanently ({reason}): {error_message}")
            )

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
            for task_pk in list(self.ray_core_runner._pending_tasks.keys()):
                try:
                    task = RayTaskExecution.objects.get(pk=task_pk)
                    self._handle_task_failure(
                        task,
                        error_message="Ray connection lost",
                        exception_type="RayConnectionError",
                    )
                except RayTaskExecution.DoesNotExist:
                    pass
            self.ray_core_runner._pending_tasks.clear()
            return

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

                now = datetime.now(UTC)
                if result.get("success"):
                    task.state = TaskState.SUCCEEDED
                    task.result_data = json.dumps(result.get("result"))
                    task.finished_at = now
                    task.save(
                        update_fields=[
                            "state",
                            "result_data",
                            "finished_at",
                        ]
                    )
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
            task.state = TaskState.FAILED
            task.error_message = f"Failed to submit to Ray: {e}"
            task.finished_at = datetime.now(UTC)
            task.save(update_fields=["state", "error_message", "finished_at"])
            self.stdout.write(self.style.ERROR(f"  Task {task.pk} failed to submit: {e}"))

    def reconcile_tasks(self) -> None:
        """Reconcile task states with Ray."""
        if self.sync_mode or not self.active_tasks:
            return

        from django_ray.runner.base import JobStatus, SubmissionHandle
        from django_ray.runner.ray_job import RayJobRunner

        runner = RayJobRunner()
        completed_tasks: list[int] = []

        for task_pk, ray_job_id in self.active_tasks.items():
            try:
                task = RayTaskExecution.objects.get(pk=task_pk)

                # Skip reconciliation if task was cancelled externally
                if task.state in (TaskState.CANCELLED, TaskState.CANCELLING):
                    # Finalize cancellation if still in CANCELLING state
                    if task.state == TaskState.CANCELLING:
                        task.state = TaskState.CANCELLED
                        task.finished_at = datetime.now(UTC)
                        task.save(update_fields=["state", "finished_at"])
                    completed_tasks.append(task_pk)
                    self.stdout.write(self.style.WARNING(f"\nTask {task_pk} was cancelled"))
                    continue

                handle = SubmissionHandle(
                    ray_job_id=ray_job_id,
                    ray_address=task.ray_address or "",
                    submitted_at=task.started_at or datetime.now(UTC),
                )

                job_info = runner.get_status(handle)

                if job_info.status == JobStatus.SUCCEEDED:
                    # Get logs which contain the result
                    logs = runner.get_logs(handle)
                    task.state = TaskState.SUCCEEDED
                    task.finished_at = datetime.now(UTC)
                    if logs:
                        # Parse result from logs (last line is JSON result)
                        try:
                            lines = logs.strip().split("\n")
                            result = json.loads(lines[-1])
                            if result.get("success"):
                                task.result_data = json.dumps(result.get("result"))
                            else:
                                task.error_message = result.get("error")
                                task.error_traceback = result.get("traceback")
                                task.state = TaskState.FAILED
                        except (json.JSONDecodeError, IndexError):
                            task.result_data = logs
                    task.save()
                    completed_tasks.append(task_pk)
                    self.stdout.write(self.style.SUCCESS(f"\nTask {task_pk} completed"))

                elif job_info.status == JobStatus.FAILED:
                    logs = runner.get_logs(handle)
                    task.state = TaskState.FAILED
                    task.finished_at = datetime.now(UTC)
                    task.error_message = job_info.message or "Ray job failed"
                    if logs:
                        task.error_traceback = logs
                    task.save()
                    completed_tasks.append(task_pk)
                    self.stdout.write(
                        self.style.ERROR(f"\nTask {task_pk} failed: {job_info.message}")
                    )

                elif job_info.status == JobStatus.STOPPED:
                    task.state = TaskState.CANCELLED
                    task.finished_at = datetime.now(UTC)
                    task.save()
                    completed_tasks.append(task_pk)
                    self.stdout.write(self.style.WARNING(f"\nTask {task_pk} was stopped"))

            except RayTaskExecution.DoesNotExist:
                completed_tasks.append(task_pk)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"\nError reconciling task {task_pk}: {e}"))

        # Remove completed tasks from active list
        for task_pk in completed_tasks:
            self.active_tasks.pop(task_pk, None)

    def detect_stuck_tasks(self) -> None:
        """Detect and mark stuck tasks as LOST.

        This checks for tasks that have been RUNNING for too long without
        heartbeats, which indicates the worker processing them may have crashed.
        """
        # Only check tasks claimed by this worker
        running_tasks = RayTaskExecution.objects.filter(
            state=TaskState.RUNNING,
            claimed_by_worker=self.worker_id,
        )

        stuck_count = 0
        timeout_count = 0
        for task in running_tasks:
            # Check for timeout first (applies to all tasks)
            if is_task_timed_out(task):
                self.stdout.write(
                    self.style.WARNING(f"\nTask {task.pk} timed out after {task.timeout_seconds}s")
                )
                # Cancel the running task if we're tracking it
                if self.ray_core_runner and task.pk in self.ray_core_runner._pending_tasks:
                    self.ray_core_runner.cancel(
                        SubmissionHandle(
                            ray_job_id=f"ray_core:{task.pk}",
                            ray_address="",
                            submitted_at=task.started_at or datetime.now(UTC),
                        )
                    )
                if task.pk in self.active_tasks:
                    del self.active_tasks[task.pk]

                mark_task_timed_out(task)
                timeout_count += 1
                continue

            # Skip tasks we're actively tracking for stuck check (they're still running)
            ray_core_pending = self.ray_core_runner._pending_tasks if self.ray_core_runner else {}
            if task.pk in ray_core_pending or task.pk in self.active_tasks:
                continue

            # Check if task is stuck using the reconciliation logic
            if is_task_stuck(task):
                self.stdout.write(
                    self.style.WARNING(f"\nTask {task.pk} appears stuck, marking as LOST")
                )
                mark_task_lost(task)

                # Check if we should retry the lost task
                retry_decision = should_retry(task, exception_type="TaskLost")
                if retry_decision.should_retry:
                    task.state = TaskState.QUEUED
                    task.attempt_number += 1
                    task.run_after = retry_decision.next_attempt_at
                    task.started_at = None
                    task.claimed_by_worker = None
                    task.save(
                        update_fields=[
                            "state",
                            "attempt_number",
                            "run_after",
                            "started_at",
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

        if stuck_count > 0:
            self.stdout.write(self.style.WARNING(f"Detected {stuck_count} stuck task(s)"))
        if timeout_count > 0:
            self.stdout.write(self.style.WARNING(f"Detected {timeout_count} timed out task(s)"))

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

    def process_cancellations(self) -> None:
        """Process tasks that have been requested for cancellation.

        This checks for tasks in CANCELLING state and finalizes their cancellation.
        """
        cancelling_tasks = RayTaskExecution.objects.filter(
            state=TaskState.CANCELLING,
            claimed_by_worker=self.worker_id,
        )

        for task in cancelling_tasks:
            self.stdout.write(self.style.WARNING(f"\nFinalizing cancellation for task {task.pk}"))

            # Remove from our tracking if present
            if self.ray_core_runner and task.pk in self.ray_core_runner._pending_tasks:
                # Try to cancel the Ray task
                self.ray_core_runner.cancel(
                    SubmissionHandle(
                        ray_job_id=f"ray_core:{task.pk}",
                        ray_address="",
                        submitted_at=task.started_at or datetime.now(UTC),
                    )
                )

            if task.pk in self.active_tasks:
                del self.active_tasks[task.pk]

            # Finalize the cancellation
            finalize_cancellation(task)
            self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} cancelled"))

    def shutdown(self) -> None:
        """Perform graceful shutdown."""
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
