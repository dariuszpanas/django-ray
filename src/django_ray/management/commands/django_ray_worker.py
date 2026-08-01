"""Django management command for running the django-ray worker."""

from __future__ import annotations

import json
import random
import signal
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from django_ray.conf.settings import get_settings
from django_ray.lifecycle import (
    cancel_task,
    promote_legacy_ray_target,
    record_failure,
    record_lost,
    retry_task,
    succeed_task,
)
from django_ray.logging import get_worker_logger
from django_ray.models import CancellationStatus, RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    PreparedRemoteCancellation,
    finalize_cancellation,
    prepare_remote_cancellation,
    request_remote_cancellation,
)
from django_ray.runner.leasing import generate_worker_id, get_heartbeat_interval
from django_ray.runner.polling import AdaptivePollingPolicy
from django_ray.runner.ray_core import RayCoreRunner
from django_ray.runner.reconciliation import (
    get_stuck_timeout,
    is_task_stuck,
    is_task_timed_out,
    mark_task_lost,
    mark_task_timed_out,
)
from django_ray.runner.retry import RetryDecision, should_retry
from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    runtime_env_for_execution,
)


class Command(BaseCommand):
    """Run a django-ray worker process."""

    help = "Run a django-ray worker that claims and executes tasks on Ray"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shutdown_requested = False
        self.worker_id = generate_worker_id()
        self.logger = get_worker_logger(self.worker_id)
        self.active_tasks: dict[int, str] = {}  # task_pk -> ray_job_id (for Ray Job API mode)
        self.active_task_identities: dict[int, tuple[int, int]] = {}
        self.ray_core_runner: RayCoreRunner | None = None  # For local/cluster modes
        self.last_reconciliation = 0.0  # Last time we ran stuck task detection
        self.reconciliation_interval = 30.0  # Check for stuck tasks every 30 seconds
        self.timeout_check_interval = 30.0
        self.cancellation_interval = 30.0
        self.lease_cleanup_interval = 30.0
        self.lease: TaskWorkerLease | None = None  # Worker lease for coordination
        self.lease_queue_name: str = "default"  # Queue name for lease recreation
        self.last_task_processed = 0.0  # Last time we processed a task
        self.tasks_processed_count = 0  # Total tasks processed
        self.task_monitor_heartbeat_interval = 15.0
        self.last_task_monitor_heartbeat = 0.0
        self.completion_poll_interval = 0.1
        self.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.1,
            max_interval_seconds=0.1,
            random_value=random.Random(self.worker_id).random,
        )
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
        queue_selection = parser.add_mutually_exclusive_group()
        queue_selection.add_argument(
            "--queue",
            type=str,
            default=None,
            help="Queue name to process (default: default). Use comma-separated for multiple queues.",
        )
        queue_selection.add_argument(
            "--queues",
            type=str,
            nargs="+",
            default=None,
            help="Queue names to process (space-separated). Alternative to --queue.",
        )
        queue_selection.add_argument(
            "--all-queues",
            action="store_true",
            help="Process tasks from all queues configured on django-ray backends.",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=None,
            help="Maximum concurrent tasks (default: from settings)",
        )
        execution_mode = parser.add_mutually_exclusive_group()
        execution_mode.add_argument(
            "--sync",
            action="store_true",
            help="Run tasks synchronously (without Ray, for testing)",
        )
        execution_mode.add_argument(
            "--local",
            action="store_true",
            help="Run with local Ray instance (starts Ray automatically)",
        )
        execution_mode.add_argument(
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
        poll_interval = float(settings.get("WORKER_POLL_INTERVAL_SECONDS", 0.1))
        poll_max_interval = float(settings.get("WORKER_POLL_MAX_INTERVAL_SECONDS", 0.1))
        self.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=poll_interval,
            max_interval_seconds=poll_max_interval,
            random_value=random.Random(self.worker_id).random,
        )

        self.setup_signal_handlers()

        self._write_worker_output(
            self.style.SUCCESS(f"Starting django-ray worker {self.worker_id}")
        )
        self._write_worker_output(f"  Queues: {', '.join(queues)}")
        self._write_worker_output(f"  Concurrency: {concurrency}")
        self._write_worker_output(f"  Mode: {self.execution_mode}")
        self._write_worker_output(
            f"  Polling: {poll_interval:g}s base, {poll_max_interval:g}s maximum"
        )

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
        - --all-queues (all queues configured on RayTaskBackend aliases)

        Args:
            options: Command options dictionary.

        Returns:
            List of queue names to process.
        """
        from django.conf import settings as django_settings
        from django.tasks import DEFAULT_TASK_QUEUE_NAME
        from django.utils.module_loading import import_string

        from django_ray.backends import RayTaskBackend

        # Check for --all-queues flag first
        if options.get("all_queues"):
            tasks_config = getattr(django_settings, "TASKS", {})
            if not isinstance(tasks_config, Mapping):
                raise CommandError("TASKS must be a mapping to resolve --all-queues")

            configured_queues: list[str] = []
            seen_queues: set[str] = set()
            ray_backend_aliases: list[str] = []
            ray_backend_targets: dict[str, str] = {}
            worker_settings = get_settings()
            process_wide_ray_core = bool(
                options.get("local")
                or options.get("cluster")
                or (
                    not options.get("sync")
                    and worker_settings.get("RUNNER", "ray_job") == "ray_core"
                )
            )
            for alias, backend_config in tasks_config.items():
                if not isinstance(alias, str) or not isinstance(backend_config, Mapping):
                    raise CommandError("TASKS aliases must map names to backend settings")
                backend_path = backend_config.get("BACKEND")
                if not isinstance(backend_path, str):
                    raise CommandError(f"TASKS backend {alias!r} must define BACKEND")
                try:
                    backend_class = import_string(backend_path)
                except ImportError as error:
                    raise CommandError(
                        f"Cannot import TASKS backend {alias!r} while resolving --all-queues"
                    ) from error
                if not isinstance(backend_class, type):
                    raise CommandError(f"TASKS backend {alias!r} does not resolve to a class")
                if not issubclass(backend_class, RayTaskBackend):
                    continue

                ray_backend_aliases.append(alias)
                if process_wide_ray_core:
                    backend_options = backend_config.get("OPTIONS", {})
                    if not isinstance(backend_options, Mapping):
                        raise CommandError(f"TASKS backend {alias!r} OPTIONS must be a mapping")
                    ray_target = backend_options.get("RAY_ADDRESS", worker_settings["RAY_ADDRESS"])
                    if not isinstance(ray_target, str) or not ray_target.strip():
                        raise CommandError(
                            f"TASKS backend {alias!r} RAY_ADDRESS must be a non-empty string"
                        )
                    ray_backend_targets[alias] = ray_target

                raw_queues = backend_config.get("QUEUES", [DEFAULT_TASK_QUEUE_NAME])
                if isinstance(raw_queues, (str, bytes)) or not isinstance(
                    raw_queues, Sequence | set | frozenset
                ):
                    raise CommandError(
                        f"TASKS backend {alias!r} QUEUES must be a collection of queue names"
                    )
                if not raw_queues:
                    raise CommandError(
                        f"TASKS backend {alias!r} has no enumerable QUEUES; "
                        "use --queue or --queues explicitly"
                    )
                queue_values = list(raw_queues)
                for queue_name in queue_values:
                    if not isinstance(queue_name, str) or not queue_name.strip():
                        raise CommandError(
                            f"TASKS backend {alias!r} QUEUES must contain non-empty strings"
                        )
                if isinstance(raw_queues, set | frozenset):
                    queue_values.sort()
                for queue_name in queue_values:
                    if queue_name not in seen_queues:
                        configured_queues.append(queue_name)
                        seen_queues.add(queue_name)

            if not ray_backend_aliases:
                raise CommandError("--all-queues found no TASKS backend using RayTaskBackend")
            if len(set(ray_backend_targets.values())) > 1:
                aliases = ", ".join(ray_backend_targets)
                raise CommandError(
                    "--all-queues cannot combine django-ray backends with different "
                    f"RAY_ADDRESS values in Ray Core mode ({aliases}); use --queue or "
                    "--queues for one compatible target, or use Ray Job mode"
                )
            self.stdout.write(
                self.style.NOTICE(
                    "Processing all configured django-ray queues "
                    f"from {ray_backend_aliases}: {configured_queues}"
                )
            )
            return configured_queues

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
        now = time.monotonic()
        next_heartbeat = now
        next_completion_poll = now
        next_claim = now
        next_reconciliation = now
        next_timeout_check = now
        next_cancellation = now
        next_lease_cleanup = now

        while not self.shutdown_requested:
            current_time = time.monotonic()
            activity = False
            claim_due = current_time >= next_claim

            if current_time >= next_heartbeat:
                self.send_heartbeat()
                next_heartbeat = current_time + heartbeat_interval

            if (
                current_time >= next_completion_poll
                and self.execution_mode in ("local", "cluster")
                and self.ray_core_runner
            ):
                activity = bool(self.poll_ray_core_tasks()) or activity
                next_completion_poll = current_time + self.completion_poll_interval

            # A signal may arrive while heartbeat/polling is in progress.  Do
            # not claim another task once shutdown has begun.
            if self.shutdown_requested:
                break

            if claim_due:
                activity = bool(self.claim_and_process_tasks(queues, concurrency)) or activity

            if current_time >= next_cancellation:
                activity = bool(self.process_cancellations()) or activity
                next_cancellation = current_time + self.cancellation_interval

            if current_time >= next_reconciliation:
                activity = bool(self.reconcile_tasks()) or activity
                self.last_reconciliation = current_time
                next_reconciliation = current_time + self.reconciliation_interval

            if current_time >= next_timeout_check:
                activity = bool(self.detect_stuck_tasks()) or activity
                next_timeout_check = current_time + self.timeout_check_interval

            if current_time >= next_lease_cleanup:
                activity = bool(self.cleanup_expired_leases()) or activity
                next_lease_cleanup = current_time + self.lease_cleanup_interval

            if claim_due:
                next_claim = current_time + self.polling_policy.next_delay(activity=activity)
            elif activity:
                next_claim = min(
                    next_claim,
                    current_time + self.polling_policy.next_delay(activity=True),
                )

            deadlines = [
                next_heartbeat,
                next_claim,
                next_reconciliation,
                next_timeout_check,
                next_cancellation,
                next_lease_cleanup,
            ]
            if (
                self.execution_mode in ("local", "cluster")
                and self.ray_core_runner
                and getattr(self.ray_core_runner, "pending_count", 0) > 0
            ):
                deadlines.append(next_completion_poll)

            sleep_seconds = max(0.0, min(deadlines) - time.monotonic())
            time.sleep(sleep_seconds)

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
        attempt_number: int | None = None,
        execution_generation: int | None = None,
    ) -> None:
        """Record that a running task is still being actively monitored."""
        heartbeat_time = now or datetime.now(UTC)
        filters: dict[str, Any] = {"pk": task.pk, "state": TaskState.RUNNING}
        if ray_job_id is not None:
            filters["ray_job_id"] = ray_job_id
        if attempt_number is not None:
            filters["attempt_number"] = attempt_number
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
        retryable = result.get("retryable")
        if retryable is not None and not isinstance(retryable, bool):
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

        pending_handles = self.ray_core_runner.pending_task_handles
        handles_by_task_pk = {handle.task_pk: handle for handle in pending_handles}
        count = 0
        for task in RayTaskExecution.objects.filter(
            pk__in=handles_by_task_pk,
            state=TaskState.RUNNING,
        ):
            handle = handles_by_task_pk[task.pk]
            handled = self._handle_task_failure(
                task,
                error_message="Ray connection lost - task state unknown",
                exception_type="RayConnectionError",
                expected_attempt_number=handle.attempt_number,
                expected_execution_generation=handle.execution_generation,
            )
            if handled:
                count += 1

        # Clear the runner's pending tasks
        self.ray_core_runner.clear_pending_tasks()

        if count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"  Routed {count} stale Ray Core task(s) through retry/failure handling"
                )
            )

    def claim_and_process_tasks(self, queues: Sequence[str], concurrency: int) -> int:
        """Claim and submit tasks for execution.

        Args:
            queues: Sequence of queue names to process (not modified).
            concurrency: Maximum concurrent tasks.
        """
        if self.shutdown_requested:
            return 0

        # Check how many slots are available
        ray_core_pending = self.ray_core_runner.pending_count if self.ray_core_runner else 0
        active_count = len(self.active_tasks) + ray_core_pending
        available_slots = concurrency - active_count
        if available_slots <= 0:
            return 0

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
                task.progress_data = None
                task.workflow_progress_summary_json = None
                task.workflow_run_id = None
                task.workflow_plan_selection = None
                promote_legacy_ray_target(task)
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
                        "progress_data",
                        "workflow_progress_summary_json",
                        "workflow_run_id",
                        "workflow_plan_selection",
                        "ray_job_id",
                        "ray_target_address",
                        "ray_address",
                    ]
                )

        # Process each claimed task
        for task in tasks:
            if self.shutdown_requested and self.execution_mode != "sync":
                self._handoff_unsubmitted_task(task)
                continue
            self.process_task(task)

        return len(tasks)

    def _handoff_unsubmitted_task(self, task: RayTaskExecution) -> None:
        """Return a just-claimed task to durable reconciliation on shutdown."""
        updated = RayTaskExecution.objects.filter(
            pk=task.pk,
            state=TaskState.RUNNING,
            claimed_by_worker=self.worker_id,
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
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

        try:
            runtime_env_for_execution(task)
        except RuntimeEnvSnapshotError as error:
            self._handle_task_failure(
                task,
                error_message=str(error),
                exception_type=type(error).__name__,
                retryable=False,
                expected_claimed_by_worker=task.claimed_by_worker,
                expected_attempt_number=int(task.attempt_number),
                expected_execution_generation=int(task.execution_generation),
            )
            return

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
        from django_ray.conf.settings import get_settings
        from django_ray.runtime.entrypoint import execute_task
        from django_ray.workflow_plans import runtime_env_plan_identity

        expected_attempt_number = int(task.attempt_number)
        expected_execution_generation = int(task.execution_generation)
        try:
            runtime_env = runtime_env_for_execution(task)
            plan_runtime_env_identity = runtime_env_plan_identity(
                runtime_env,
                trust_identity=get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
            )
            result_json = execute_task(
                callable_path=task.callable_path,
                serialized_args=task.args_json,
                serialized_kwargs=task.kwargs_json,
                task_execution_pk=task.pk,
                attempt_number=expected_attempt_number,
                execution_generation=expected_execution_generation,
                runtime_env_profile=runtime_env.profile,
                runtime_env_hash=runtime_env.digest,
                runtime_env_plan_identity=plan_runtime_env_identity.as_transport_dict(),
                input_reference=getattr(task, "input_reference", None),
                ray_job_driver=False,
            )
            result = json.loads(result_json)

            if result["success"]:
                if not RayTaskExecution.objects.filter(
                    pk=task.pk,
                    state=TaskState.RUNNING,
                    attempt_number=expected_attempt_number,
                    execution_generation=expected_execution_generation,
                ).exists():
                    self.stdout.write(
                        self.style.NOTICE(
                            f"  Ignoring stale synchronous result for task {task.pk} "
                            f"attempt {expected_attempt_number}, "
                            f"generation {expected_execution_generation}"
                        )
                    )
                    return
                if not self._store_and_succeed_task(
                    task,
                    result["result"],
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                ):
                    return
                self.stdout.write(
                    self.style.SUCCESS(f"  Task {task.pk} succeeded: {result['result']}")
                )
            else:
                # Task failed - check if we should retry
                self._handle_task_failure(
                    task,
                    error_message=result["error"],
                    error_traceback=result.get("traceback"),
                    exception_type=result.get("exception_type"),
                    retryable=result.get("retryable"),
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                )

        except Exception as e:
            self._handle_task_failure(
                task,
                error_message=str(e),
                exception_type=type(e).__name__,
                retryable=False if isinstance(e, RuntimeEnvSnapshotError) else None,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )

    def _store_task_result(
        self,
        task: RayTaskExecution,
        result_value: Any,
    ) -> tuple[str, ...]:
        """Store task result inline or as a reference if result is too large.

        Args:
            task: The task to update with result fields.
            result_value: The Python value to serialize and store.

        Returns:
            Warning messages to emit only after the durable success commits.
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
            return ()

        task.result_data = None
        diagnostics: list[str] = []

        try:
            backend = get_result_storage_backend(settings)
            task.result_reference = backend.store(serialized_result=serialized_result)
        except ResultStorageError as e:
            # Preserve success semantics if external storage is unavailable.
            task.result_reference = DigestResultStorage().store(serialized_result=serialized_result)
            diagnostics.append(
                f"  Result storage backend failed ({e}); "
                "falling back to digest-only result_reference"
            )

        diagnostics.append(
            f"  Task {task.pk} result is {result_size_bytes} bytes "
            f"(max={max_result_size}); stored result_reference"
        )
        return tuple(diagnostics)

    def _store_and_succeed_task(
        self,
        task: RayTaskExecution,
        result_value: Any,
        *,
        prepared_result_reference: str | None = None,
        expected_ray_job_id: str | None = None,
        expected_attempt_number: int | None = None,
        expected_execution_generation: int | None = None,
        expected_completion_data: str | None = None,
        require_completion_data_match: bool = False,
    ) -> bool:
        """Store and publish one successful result under the execution row lock.

        External result references are content-addressed and may be shared, so
        deleting a reference after losing the terminal transition is unsafe.
        Holding the row lock across storage and the success transition prevents
        cancellation, retry, or replacement from winning in that window.
        """
        filters: dict[str, Any] = {"pk": task.pk, "state": TaskState.RUNNING}
        if expected_ray_job_id is not None:
            filters["ray_job_id"] = expected_ray_job_id
        if expected_attempt_number is not None:
            filters["attempt_number"] = expected_attempt_number
        if expected_execution_generation is not None:
            filters["execution_generation"] = expected_execution_generation
        if require_completion_data_match:
            filters["completion_data"] = expected_completion_data

        diagnostics: tuple[str, ...] = ()
        with transaction.atomic():
            current = RayTaskExecution.objects.select_for_update().filter(**filters).first()
            if current is None:
                return False

            if prepared_result_reference is None:
                diagnostics = self._store_task_result(current, result_value) or ()
            else:
                current.result_data = None
                current.result_reference = prepared_result_reference

            persisted = succeed_task(
                current,
                result_data=current.result_data,
                result_reference=current.result_reference,
                expected_ray_job_id=expected_ray_job_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=expected_completion_data,
                require_completion_data_match=require_completion_data_match,
            )
            if persisted:
                task.__dict__.update(current.__dict__)

        for message in diagnostics:
            self.stdout.write(self.style.WARNING(message))
        return persisted

    def _handle_task_failure(
        self,
        task: RayTaskExecution,
        error_message: str,
        error_traceback: str | None = None,
        exception_type: str | None = None,
        retryable: bool | None = None,
        *,
        expected_ray_job_id: str | None = None,
        expected_claimed_by_worker: str | None = None,
        expected_attempt_number: int | None = None,
        expected_execution_generation: int | None = None,
        expected_completion_data: str | None = None,
        require_completion_data_match: bool = False,
        cancellation_status: str | None = None,
        cancellation_error: str | None = None,
    ) -> bool:
        """Handle a failed task, potentially scheduling a retry.

        Args:
            task: The failed task.
            error_message: The error message.
            error_traceback: The full traceback (optional).
            exception_type: The exception class name (optional).
            retryable: Explicit executor decision for permanent input failures.
        """
        # Check if we should retry
        retry_decision = (
            RetryDecision(should_retry=False, reason="Executor marked failure non-retryable")
            if retryable is False
            else should_retry(task, exception_type)
        )

        try:
            handled = record_failure(
                task,
                error_message=error_message,
                error_traceback=error_traceback,
                retry=retry_decision.should_retry,
                next_attempt_at=retry_decision.next_attempt_at,
                expected_ray_job_id=expected_ray_job_id,
                expected_claimed_by_worker=expected_claimed_by_worker,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=expected_completion_data,
                require_completion_data_match=require_completion_data_match,
                cancellation_status=cancellation_status,
                cancellation_error=cancellation_error,
            )
        except RuntimeEnvSnapshotError as storage_error:
            retry_decision = RetryDecision(
                should_retry=False,
                reason="Persisted RuntimeEnv snapshot failed validation",
            )
            error_message = f"{error_message}\nAutomatic retry blocked: {storage_error}"
            handled = record_failure(
                task,
                error_message=error_message,
                error_traceback=error_traceback,
                retry=False,
                expected_ray_job_id=expected_ray_job_id,
                expected_claimed_by_worker=expected_claimed_by_worker,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=expected_completion_data,
                require_completion_data_match=require_completion_data_match,
                cancellation_status=cancellation_status,
                cancellation_error=cancellation_error,
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

    @staticmethod
    def _persist_submission_tracking(
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
        expected_ray_job_id: str | None,
    ) -> bool:
        """Attach a backend handle only to the execution that submitted it."""
        filters: dict[str, Any] = {
            "pk": task.pk,
            "state": TaskState.RUNNING,
            "attempt_number": expected_attempt_number,
            "execution_generation": expected_execution_generation,
        }
        if expected_worker_id is None:
            filters["claimed_by_worker__isnull"] = True
        else:
            filters["claimed_by_worker"] = expected_worker_id
        if expected_ray_job_id is None:
            filters["ray_job_id__isnull"] = True
        else:
            filters["ray_job_id"] = expected_ray_job_id
        heartbeat_at = datetime.now(UTC)
        updated = RayTaskExecution.objects.filter(**filters).update(
            ray_job_id=handle.ray_job_id,
            ray_address=handle.ray_address,
            last_heartbeat_at=heartbeat_at,
        )
        if not updated:
            return False
        task.ray_job_id = handle.ray_job_id
        task.ray_address = handle.ray_address
        task.last_heartbeat_at = heartbeat_at
        return True

    @staticmethod
    def _release_submission_tracking(
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
    ) -> bool:
        """Release an exact reservation after a definite pre-request failure."""
        filters: dict[str, Any] = {
            "pk": task.pk,
            "state": TaskState.RUNNING,
            "attempt_number": expected_attempt_number,
            "execution_generation": expected_execution_generation,
            "ray_job_id": handle.ray_job_id,
            "ray_address": handle.ray_address,
        }
        if expected_worker_id is None:
            filters["claimed_by_worker__isnull"] = True
        else:
            filters["claimed_by_worker"] = expected_worker_id
        heartbeat_at = datetime.now(UTC)
        updated = RayTaskExecution.objects.filter(**filters).update(
            ray_job_id=None,
            ray_address=None,
            last_heartbeat_at=heartbeat_at,
        )
        if not updated:
            return False
        task.ray_job_id = None
        task.ray_address = None
        task.last_heartbeat_at = heartbeat_at
        return True

    def _cancel_untracked_submission(
        self,
        runner: Any,
        handle: SubmissionHandle,
        *,
        backend_name: str,
        prepared: PreparedRemoteCancellation | None = None,
    ) -> CancellationOutcome:
        """Best-effort stop for a submission whose durable execution changed."""
        try:
            cancellation = request_remote_cancellation(runner, handle, prepared=prepared)
        except Exception as exc:
            cancellation = CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                f"Cancellation request raised {type(exc).__name__}: {exc}",
            )
        message = f": {cancellation.message}" if cancellation.message else ""
        self.stdout.write(
            self.style.WARNING(
                f"  Discarded stale {backend_name} submission {handle.ray_job_id}; "
                f"cancellation {cancellation.status.value}{message}"
            )
        )
        return cancellation

    def _cancel_mismatched_submissions(
        self,
        runner: Any,
        reserved_handle: SubmissionHandle,
        observed_handle: SubmissionHandle,
        *,
        prepared: dict[tuple[str, str], PreparedRemoteCancellation] | None = None,
    ) -> CancellationOutcome:
        """Stop both capabilities when Ray and the durable reservation disagree."""
        handles = {
            (reserved_handle.ray_job_id, reserved_handle.ray_address): reserved_handle,
            (observed_handle.ray_job_id, observed_handle.ray_address): observed_handle,
        }
        outcomes = [
            self._cancel_untracked_submission(
                runner,
                handle,
                backend_name="mismatched Ray Job",
                prepared=(prepared or {}).get((handle.ray_job_id, handle.ray_address)),
            )
            for handle in handles.values()
        ]
        statuses = {outcome.status for outcome in outcomes}
        quiescent_statuses = {
            CancellationOutcomeStatus.REQUESTED,
            CancellationOutcomeStatus.NOT_APPLICABLE,
        }
        if statuses <= quiescent_statuses:
            combined_status = (
                CancellationOutcomeStatus.REQUESTED
                if CancellationOutcomeStatus.REQUESTED in statuses
                else CancellationOutcomeStatus.NOT_APPLICABLE
            )
        elif statuses == {CancellationOutcomeStatus.FAILED}:
            combined_status = CancellationOutcomeStatus.FAILED
        else:
            combined_status = CancellationOutcomeStatus.INDETERMINATE
        messages = [outcome.message for outcome in outcomes if outcome.message]
        return CancellationOutcome(
            combined_status,
            "; ".join(messages) if messages else None,
        )

    @staticmethod
    def _inspect_submission_tracking(
        task: RayTaskExecution,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
    ) -> tuple[str, str | None, str | None]:
        """Classify the durable identity after a submitter loses its CAS."""
        try:
            current = (
                RayTaskExecution.objects.filter(pk=task.pk)
                .values(
                    "state",
                    "claimed_by_worker",
                    "ray_job_id",
                    "ray_address",
                    "attempt_number",
                    "execution_generation",
                    "completion_data",
                )
                .first()
            )
        except Exception as exc:
            return (
                "unknown",
                None,
                f"tracking inspection raised {type(exc).__name__}: {exc}",
            )

        if current is None:
            return ("replaced", None, "the durable execution no longer exists")

        completion_data = current["completion_data"]
        same_identity = (
            str(current["ray_job_id"] or "") == handle.ray_job_id
            and str(current["ray_address"] or "") == handle.ray_address
            and int(current["attempt_number"]) == expected_attempt_number
            and int(current["execution_generation"]) == expected_execution_generation
        )
        if not same_identity:
            return ("replaced", completion_data, "the durable execution identity changed")
        if current["state"] != TaskState.RUNNING:
            return (
                "terminal",
                completion_data,
                f"the durable execution is now {current['state']}",
            )

        current_worker_id = current["claimed_by_worker"]
        if current_worker_id == expected_worker_id:
            return ("owned", completion_data, None)
        return (
            "transferred",
            completion_data,
            f"ownership moved to {current_worker_id or 'an unclaimed reconciler'}",
        )

    def _handle_ray_job_confirmation_loss(
        self,
        task: RayTaskExecution,
        runner: Any,
        handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
        detail: str,
    ) -> None:
        """Resolve a failed post-request CAS without killing an adopted job."""
        disposition, _, inspection_detail = self._inspect_submission_tracking(
            task,
            handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        if disposition == "replaced":
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)
            self._cancel_untracked_submission(
                runner,
                handle,
                backend_name="replaced Ray Job",
            )
        elif disposition in {"transferred", "terminal"}:
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)

        retained = disposition in {"owned", "unknown"}
        action = "retaining exact tracking" if retained else "retiring local tracking"
        explanation = f"; {inspection_detail}" if inspection_detail else ""
        self.stdout.write(
            self.style.WARNING(
                f"  Task {task.pk} could not confirm Ray Job tracking ({detail}); "
                f"{action}{explanation}"
            )
        )

    def _handle_mismatched_ray_job_submission(
        self,
        task: RayTaskExecution,
        runner: Any,
        reserved_handle: SubmissionHandle,
        observed_handle: SubmissionHandle,
        *,
        expected_worker_id: str | None,
        expected_attempt_number: int,
        expected_execution_generation: int,
        error_message: str,
        exception_type: str,
    ) -> None:
        """Fence a returned capability mismatch without cancelling an adopter."""
        owner_filters: dict[str, Any] = {
            "pk": task.pk,
            "state": TaskState.RUNNING,
            "ray_job_id": reserved_handle.ray_job_id,
            "ray_address": reserved_handle.ray_address,
            "attempt_number": expected_attempt_number,
            "execution_generation": expected_execution_generation,
        }
        if expected_worker_id is None:
            owner_filters["claimed_by_worker__isnull"] = True
        else:
            owner_filters["claimed_by_worker"] = expected_worker_id

        handles = {
            (reserved_handle.ray_job_id, reserved_handle.ray_address): reserved_handle,
            (observed_handle.ray_job_id, observed_handle.ray_address): observed_handle,
        }
        prepared_cancellations = {
            identity: prepare_remote_cancellation(runner, handle)
            for identity, handle in handles.items()
        }

        # The row lock makes "still owned" and the exact stop/failure one
        # indivisible decision. A worker whose lease expired cannot cancel the
        # capability after another worker adopts this same execution identity.
        with transaction.atomic():
            current = RayTaskExecution.objects.select_for_update().filter(**owner_filters).first()
            if current is not None:
                if current.completion_data is not None:
                    self._cancel_untracked_submission(
                        runner,
                        observed_handle,
                        backend_name="mismatched Ray Job",
                        prepared=prepared_cancellations[
                            (observed_handle.ray_job_id, observed_handle.ray_address)
                        ],
                    )
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Task {task.pk} Ray Job identity mismatch arrived after "
                            "a durable completion; retaining exact tracking"
                        )
                    )
                    return
                cancellation = self._cancel_mismatched_submissions(
                    runner,
                    reserved_handle,
                    observed_handle,
                    prepared=prepared_cancellations,
                )
                handled = self._handle_task_failure(
                    current,
                    error_message=error_message,
                    exception_type=exception_type,
                    retryable=False,
                    expected_ray_job_id=reserved_handle.ray_job_id,
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=None,
                    require_completion_data_match=True,
                    cancellation_status=cancellation.status.value,
                    cancellation_error=cancellation.message,
                )
                if handled:
                    task_pk = int(task.pk)
                    transaction.on_commit(
                        lambda: self._retire_active_ray_job_tracking(
                            task_pk,
                            ray_job_id=reserved_handle.ray_job_id,
                            identity=(
                                expected_attempt_number,
                                expected_execution_generation,
                            ),
                        )
                    )
                    return

        disposition, _, inspection_detail = self._inspect_submission_tracking(
            task,
            reserved_handle,
            expected_worker_id=expected_worker_id,
            expected_attempt_number=expected_attempt_number,
            expected_execution_generation=expected_execution_generation,
        )
        if disposition == "replaced":
            self._cancel_mismatched_submissions(
                runner,
                reserved_handle,
                observed_handle,
                prepared=prepared_cancellations,
            )
        else:
            self._cancel_untracked_submission(
                runner,
                observed_handle,
                backend_name="mismatched Ray Job",
                prepared=prepared_cancellations[
                    (observed_handle.ray_job_id, observed_handle.ray_address)
                ],
            )

        if disposition in {"replaced", "transferred", "terminal"}:
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)

        retained = disposition in {"owned", "unknown"}
        action = "retaining exact tracking" if retained else "retiring local tracking"
        explanation = f"; {inspection_detail}" if inspection_detail else ""
        self.stdout.write(
            self.style.WARNING(f"  Task {task.pk} Ray Job identity mismatch; {action}{explanation}")
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

        expected_worker_id = task.claimed_by_worker
        expected_attempt_number = int(task.attempt_number)
        expected_execution_generation = int(task.execution_generation)
        expected_ray_job_id = task.ray_job_id

        # Ensure Ray is connected and runner is available
        if not ray.is_initialized():
            self.stdout.write(self.style.WARNING("  Ray not initialized, attempting to connect..."))
            self._reconnect_ray()

            if not ray.is_initialized():
                self._handle_task_failure(
                    task,
                    error_message="Ray cluster not available",
                    exception_type="RayConnectionError",
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                )
                return

        # Ensure runner is initialized
        if self.ray_core_runner is None:
            self.ray_core_runner = RayCoreRunner()
        runner = self.ray_core_runner

        try:
            if task.input_reference:
                args: Any = ()
                kwargs: Any = {}
            else:
                args = deserialize_args(task.args_json)
                kwargs = deserialize_args(task.kwargs_json)

            handle = runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=tuple(args),
                kwargs=kwargs,
            )
        except Exception as e:
            import traceback

            from django_ray.workflow_plans import WorkflowPlanMismatchError

            self._handle_task_failure(
                task,
                error_message=f"Failed to submit to Ray Core: {e}",
                error_traceback=(
                    None if isinstance(e, RuntimeEnvSnapshotError) else traceback.format_exc()
                ),
                exception_type=type(e).__name__,
                retryable=(
                    False
                    if isinstance(e, (RuntimeEnvSnapshotError, WorkflowPlanMismatchError))
                    else None
                ),
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )
            return

        try:
            attached = self._persist_submission_tracking(
                task,
                handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_ray_job_id=expected_ray_job_id,
            )
        except Exception as exc:
            import traceback

            cancellation = self._cancel_untracked_submission(
                runner,
                handle,
                backend_name="Ray Core",
            )
            self._handle_task_failure(
                task,
                error_message=f"Failed to persist Ray Core submission tracking: {exc}",
                error_traceback=traceback.format_exc(),
                exception_type=type(exc).__name__,
                retryable=False,
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                cancellation_status=cancellation.status.value,
                cancellation_error=cancellation.message,
            )
            return

        if not attached:
            self._cancel_untracked_submission(
                runner,
                handle,
                backend_name="Ray Core",
            )
            return

        self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} submitted to Ray Core (async)"))

    def poll_ray_core_tasks(self) -> int:
        """Poll for completed Ray Core tasks and update their status.

        Uses RayCoreRunner.poll_completed() for efficient batch polling.
        """
        if self.ray_core_runner is None or self.ray_core_runner.pending_count == 0:
            return 0

        import ray

        # Check if Ray is still connected
        if not ray.is_initialized():
            self.stdout.write(self.style.WARNING("\nRay disconnected, clearing pending tasks..."))
            # Mark all pending tasks as needing retry
            pending_handles = self.ray_core_runner.pending_task_handles
            for handle in pending_handles:
                try:
                    task = RayTaskExecution.objects.get(pk=handle.task_pk)
                    self._handle_task_failure(
                        task,
                        error_message="Ray connection lost",
                        exception_type="RayConnectionError",
                        expected_attempt_number=handle.attempt_number,
                        expected_execution_generation=handle.execution_generation,
                    )
                except RayTaskExecution.DoesNotExist:
                    pass
            self.ray_core_runner.clear_pending_tasks()
            return len(pending_handles)

        monitored_handles = self.ray_core_runner.pending_task_handles
        monitor_time = time.monotonic()
        if (
            monitored_handles
            and monitor_time - self.last_task_monitor_heartbeat
            >= self.task_monitor_heartbeat_interval
        ):
            heartbeat_time = datetime.now(UTC)
            task_ids_by_identity: dict[tuple[int, int], list[int]] = {}
            for handle in monitored_handles:
                identity = (handle.attempt_number, handle.execution_generation)
                task_ids_by_identity.setdefault(identity, []).append(handle.task_pk)
            for (attempt_number, execution_generation), task_ids in task_ids_by_identity.items():
                RayTaskExecution.objects.filter(
                    pk__in=task_ids,
                    state=TaskState.RUNNING,
                    attempt_number=attempt_number,
                    execution_generation=execution_generation,
                ).update(last_heartbeat_at=heartbeat_time)
            self.last_task_monitor_heartbeat = monitor_time

        # Poll for completed tasks
        try:
            completed = self.ray_core_runner.poll_completed()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\nError polling Ray Core tasks: {e}"))
            return 0

        for completion in completed:
            task_pk = completion.task_pk
            attempt_number = completion.attempt_number
            execution_generation = completion.execution_generation
            try:
                task = RayTaskExecution.objects.get(pk=task_pk)

                if (
                    task.attempt_number != attempt_number
                    or task.execution_generation != execution_generation
                ):
                    self.stdout.write(
                        self.style.NOTICE(
                            f"\n  Ignoring stale Ray Core result for task {task.pk} "
                            f"attempt {attempt_number}, generation {execution_generation}"
                        )
                    )
                    continue

                # Skip if task was cancelled externally
                if task.state in (TaskState.CANCELLED, TaskState.CANCELLING):
                    if task.state == TaskState.CANCELLING:
                        cancel_task(
                            task,
                            expected_attempt_number=attempt_number,
                            expected_execution_generation=execution_generation,
                        )
                    self.stdout.write(self.style.WARNING(f"\n  Task {task.pk} was cancelled"))
                    continue

                if task.state != TaskState.RUNNING:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"\n  Ignoring Ray Core result for task {task.pk} in state {task.state}"
                        )
                    )
                    continue

                result = json.loads(completion.result_json)

                if result.get("success"):
                    if not self._store_and_succeed_task(
                        task,
                        result.get("result"),
                        expected_attempt_number=attempt_number,
                        expected_execution_generation=execution_generation,
                    ):
                        self.stdout.write(
                            self.style.NOTICE(
                                f"\n  Task {task.pk} changed while its Ray Core "
                                "result was being stored"
                            )
                        )
                        continue
                    self.stdout.write(
                        self.style.SUCCESS(f"\n  Task {task.pk} completed: {result.get('result')}")
                    )
                else:
                    self._handle_task_failure(
                        task,
                        error_message=result.get("error", "Unknown error"),
                        error_traceback=result.get("traceback"),
                        exception_type=result.get("exception_type"),
                        retryable=result.get("retryable"),
                        expected_attempt_number=attempt_number,
                        expected_execution_generation=execution_generation,
                    )

            except RayTaskExecution.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"\n  Task {task_pk} not found in database"))
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"\n  Error processing task {task_pk} result: {e}")
                )

        return len(completed)

    def submit_task_to_ray(self, task: RayTaskExecution) -> None:
        """Submit a task to Ray for execution."""
        from django_ray.runner import RayJobSubmissionUncertainError
        from django_ray.runner.ray_job import RayJobRunner
        from django_ray.runtime.serialization import deserialize_args
        from django_ray.workflow_plans import WorkflowPlanMismatchError

        expected_worker_id = task.claimed_by_worker
        expected_attempt_number = int(task.attempt_number)
        expected_execution_generation = int(task.execution_generation)
        expected_ray_job_id = task.ray_job_id

        try:
            runner = RayJobRunner()
            if task.input_reference:
                args: Any = ()
                kwargs: Any = {}
            else:
                args = deserialize_args(task.args_json)
                kwargs = deserialize_args(task.kwargs_json)

        except Exception as e:
            import traceback

            self._handle_task_failure(
                task,
                error_message=f"Failed to submit to Ray: {e}",
                error_traceback=(
                    None if isinstance(e, RuntimeEnvSnapshotError) else traceback.format_exc()
                ),
                exception_type=type(e).__name__,
                retryable=(
                    False
                    if isinstance(e, (RuntimeEnvSnapshotError, WorkflowPlanMismatchError))
                    else None
                ),
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )
            return

        try:
            reserved_handle = runner.submission_handle(task)
            reserved = self._persist_submission_tracking(
                task,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_ray_job_id=expected_ray_job_id,
            )
        except Exception as exc:
            import traceback

            self._handle_task_failure(
                task,
                error_message=f"Failed to reserve Ray Job submission identity: {exc}",
                error_traceback=(
                    None if isinstance(exc, RuntimeEnvSnapshotError) else traceback.format_exc()
                ),
                exception_type=type(exc).__name__,
                retryable=(
                    False
                    if isinstance(exc, (RuntimeEnvSnapshotError, WorkflowPlanMismatchError))
                    else None
                ),
                expected_claimed_by_worker=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )
            return

        if not reserved:
            return

        # The exact identity is durable before the request. A timeout after Ray
        # accepts the request can therefore be reconciled without resubmitting.
        self.active_tasks[task.pk] = reserved_handle.ray_job_id
        self.active_task_identities[task.pk] = (
            expected_attempt_number,
            expected_execution_generation,
        )

        try:
            handle = runner.submit(
                task_execution=task,
                callable_path=task.callable_path,
                args=tuple(args),
                kwargs=kwargs,
            )
        except RayJobSubmissionUncertainError as exc:
            if (
                exc.observed_submission_id is not None
                and exc.observed_submission_id != reserved_handle.ray_job_id
            ):
                mismatched_handle = SubmissionHandle(
                    ray_job_id=exc.observed_submission_id,
                    ray_address=reserved_handle.ray_address,
                    submitted_at=reserved_handle.submitted_at,
                )
                self._handle_mismatched_ray_job_submission(
                    task,
                    runner,
                    reserved_handle,
                    mismatched_handle,
                    error_message=str(exc),
                    exception_type=type(exc).__name__,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                )
                return
            try:
                still_reserved = self._persist_submission_tracking(
                    task,
                    reserved_handle,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_ray_job_id=reserved_handle.ray_job_id,
                )
            except Exception as tracking_exc:
                self._handle_ray_job_confirmation_loss(
                    task,
                    runner,
                    reserved_handle,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    detail=f"{type(tracking_exc).__name__}: {tracking_exc}",
                )
                return
            if not still_reserved:
                self._handle_ray_job_confirmation_loss(
                    task,
                    runner,
                    reserved_handle,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    detail="the reservation confirmation lost its compare-and-swap",
                )
                return
            self.stdout.write(
                self.style.WARNING(
                    f"  Task {task.pk} submission acceptance is uncertain; "
                    f"retaining exact Ray job {exc.submission_id} for reconciliation"
                )
            )
            return
        except Exception as exc:
            import traceback

            released = self._release_submission_tracking(
                task,
                reserved_handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )
            self.active_tasks.pop(task.pk, None)
            self.active_task_identities.pop(task.pk, None)
            if released:
                self._handle_task_failure(
                    task,
                    error_message=f"Failed to submit to Ray: {exc}",
                    error_traceback=(
                        None if isinstance(exc, RuntimeEnvSnapshotError) else traceback.format_exc()
                    ),
                    exception_type=type(exc).__name__,
                    retryable=(
                        False
                        if isinstance(
                            exc,
                            (RuntimeEnvSnapshotError, WorkflowPlanMismatchError),
                        )
                        else None
                    ),
                    expected_claimed_by_worker=expected_worker_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                )
            return

        if (
            handle.ray_job_id != reserved_handle.ray_job_id
            or handle.ray_address != reserved_handle.ray_address
        ):
            self._handle_mismatched_ray_job_submission(
                task,
                runner,
                reserved_handle,
                handle,
                error_message=(
                    "Ray Job runner returned a handle that differs from the "
                    "durably reserved submission identity"
                ),
                exception_type="RayJobSubmissionIdentityError",
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
            )
            return

        try:
            still_reserved = self._persist_submission_tracking(
                task,
                handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_ray_job_id=reserved_handle.ray_job_id,
            )
        except Exception as exc:
            self._handle_ray_job_confirmation_loss(
                task,
                runner,
                handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return
        if not still_reserved:
            self._handle_ray_job_confirmation_loss(
                task,
                runner,
                handle,
                expected_worker_id=expected_worker_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                detail="the post-submit confirmation lost its compare-and-swap",
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"  Task {task.pk} submitted as Ray job {handle.ray_job_id}")
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
            "attempt_number": task.attempt_number,
            "execution_generation": task.execution_generation,
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
            self.active_task_identities[task.pk] = (
                int(task.attempt_number),
                int(task.execution_generation),
            )
        return True

    def _retire_active_ray_job_tracking(
        self,
        task_pk: int,
        *,
        ray_job_id: str,
        identity: tuple[int, int],
    ) -> None:
        """Forget active tracking only when the exact tracked identity still matches."""
        if self.active_tasks.get(task_pk) != ray_job_id:
            return
        tracked_identity = self.active_task_identities.get(task_pk)
        if tracked_identity is not None and tracked_identity != identity:
            return
        self.active_tasks.pop(task_pk, None)
        self.active_task_identities.pop(task_pk, None)

    def _reconcile_ray_job_task(
        self,
        task: RayTaskExecution,
        runner: Any,
        *,
        ray_job_id: str,
        completed_tasks: list[int],
        orphaned: bool,
        tracked_identity: tuple[int, int] | None = None,
    ) -> None:
        """Reconcile a single Ray Job task from either active or orphaned tracking."""
        from django_ray.runner.base import JobStatus

        task_identity = (int(task.attempt_number), int(task.execution_generation))
        expected_identity = tracked_identity or task_identity

        def complete_tracking() -> None:
            completed_tasks.append(task.pk)
            self._retire_active_ray_job_tracking(
                task.pk,
                ray_job_id=ray_job_id,
                identity=expected_identity,
            )

        # Active tracking may outlive a retry that reused the same Ray Job ID.
        # Reject that stale identity before making a status RPC.
        if task_identity != expected_identity:
            self._retire_active_ray_job_tracking(
                task.pk,
                ray_job_id=ray_job_id,
                identity=expected_identity,
            )
            return

        # CANCELLING remains owned by process_cancellations(), which requests
        # status-aware remote interruption before finalizing the durable row.
        if task.state == TaskState.CANCELLING:
            return
        if task.state == TaskState.CANCELLED:
            complete_tracking()
            self.stdout.write(self.style.WARNING(f"\nTask {task.pk} was cancelled"))
            return
        if task.state != TaskState.RUNNING:
            complete_tracking()
            return

        expected_attempt_number, expected_execution_generation = expected_identity
        handle = self._build_submission_handle(task, ray_job_id)

        def consume_valid_completion(completion_data: str | None) -> bool:
            """Apply one valid durable envelope without depending on Ray availability."""
            if completion_data is None:
                return False
            try:
                result = json.loads(completion_data)
            except (TypeError, json.JSONDecodeError):
                return False
            if not self._is_valid_completion_envelope(result):
                return False

            if result["success"]:
                prepared_result_reference = (
                    str(result["result_reference"]) if result.get("result_reference") else None
                )
                if not self._store_and_succeed_task(
                    task,
                    result.get("result"),
                    prepared_result_reference=prepared_result_reference,
                    expected_ray_job_id=ray_job_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=completion_data,
                    require_completion_data_match=True,
                ):
                    return True
                self.stdout.write(self.style.SUCCESS(f"\nTask {task.pk} completed"))
            else:
                handled = self._handle_task_failure(
                    task,
                    error_message=result["error"],
                    error_traceback=result.get("traceback"),
                    exception_type=result.get("exception_type"),
                    retryable=result.get("retryable"),
                    expected_ray_job_id=ray_job_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=completion_data,
                    require_completion_data_match=True,
                )
                if handled is False:
                    return True
                self.stdout.write(
                    self.style.WARNING(
                        f"\nTask {task.pk} returned failure envelope, handling via retry policy"
                    )
                )
            complete_tracking()
            return True

        # The entrypoint's valid durable envelope is authoritative. Consume it
        # before contacting Ray so a control-plane outage cannot strand a task
        # whose terminal result is already safely persisted.
        if consume_valid_completion(task.completion_data):
            return

        job_info = runner.get_status(handle)
        now = datetime.now(UTC)

        # Refresh after the status RPC: the execution may have been retried or
        # claimed elsewhere while the RPC was in flight.
        try:
            task.refresh_from_db(
                fields=[
                    "state",
                    "completion_data",
                    "ray_job_id",
                    "attempt_number",
                    "execution_generation",
                ]
            )
        except RayTaskExecution.DoesNotExist:
            complete_tracking()
            return

        # Never reconcile an old Ray Job against a replacement execution.
        if (
            str(task.ray_job_id or "") != ray_job_id
            or task.attempt_number != expected_attempt_number
            or task.execution_generation != expected_execution_generation
        ):
            self._retire_active_ray_job_tracking(
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
        ) -> bool:
            """Fence an untrusted execution, request its exact stop, and retain LOST."""
            timeout_recovery_owns_task = expected_completion_data is None and is_task_timed_out(
                task
            )
            if timeout_recovery_owns_task or not is_task_stuck(task):
                return False

            prepared_cancellation = prepare_remote_cancellation(runner, handle)

            # Hold the execution lock through the bounded stop request so a
            # manual retry cannot race ahead of its durable outcome.
            with transaction.atomic():
                if not record_lost(
                    task,
                    error_message=error_message,
                    expected_completion_data=expected_completion_data,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                ):
                    return False
                cancellation = request_remote_cancellation(
                    runner,
                    handle,
                    prepared=prepared_cancellation,
                )
                task.cancellation_status = CancellationStatus(cancellation.status.value)
                task.cancellation_error = cancellation.message
                task.save(update_fields=["cancellation_status", "cancellation_error"])

            complete_tracking()
            self.stdout.write(
                self.style.ERROR(
                    f"\nTask {task.pk} {log_detail}; exact best-effort stop outcome was "
                    f"{cancellation.status.value} and automatic retry was suppressed"
                )
            )
            return True

        completion_data = task.completion_data

        # The entrypoint's durable envelope is authoritative even while Ray
        # briefly continues to report PENDING/RUNNING during process teardown.
        # Consume it before monitor heartbeat or timeout logic can obscure it.
        if consume_valid_completion(completion_data):
            return

        if completion_data is None and job_info.status in (
            JobStatus.PENDING,
            JobStatus.RUNNING,
        ):
            self._mark_task_monitor_heartbeat(
                task,
                now=now,
                ray_job_id=ray_job_id,
                attempt_number=expected_attempt_number,
                execution_generation=expected_execution_generation,
            )
            if orphaned and self._adopt_orphaned_ray_job_task(task, now=now):
                self.stdout.write(
                    self.style.NOTICE(
                        f"\nAdopted orphaned Ray job task {task.pk} for continued monitoring"
                    )
                )
            return

        if completion_data is not None:
            completion_error: str | None = None
            try:
                result = json.loads(completion_data)
            except (TypeError, json.JSONDecodeError):
                completion_error = "malformed"

            if completion_error is None and not self._is_valid_completion_envelope(result):
                completion_error = "invalid"

            if completion_error is not None:
                if job_info.status == JobStatus.UNKNOWN:
                    if resolve_stale_untrusted_execution(
                        expected_completion_data=completion_data,
                        error_message=(
                            "Ray Job status remained UNKNOWN past the stuck-task timeout"
                        ),
                        log_detail=("Ray job status remained unknown past the stuck-task timeout"),
                    ):
                        return
                    self.stdout.write(
                        self.style.WARNING(
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
                    self.stdout.write(
                        self.style.WARNING(
                            f"\nTask {task.pk} has a {completion_error} completion envelope "
                            f"while Ray still reports {job_info.status.value}; waiting for "
                            "bounded recovery"
                        )
                    )
                    return
                if self._completion_envelope_grace_expired(task, now=now):
                    handled = self._handle_task_failure(
                        task,
                        error_message=f"Ray Job produced a {completion_error} completion envelope",
                        exception_type="RayCompletionMalformed",
                        expected_ray_job_id=ray_job_id,
                        expected_attempt_number=expected_attempt_number,
                        expected_execution_generation=expected_execution_generation,
                        expected_completion_data=completion_data,
                        require_completion_data_match=True,
                    )
                    if handled is False:
                        return
                    complete_tracking()
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

            raise AssertionError("valid completion envelope was not consumed")

        if job_info.status == JobStatus.SUCCEEDED:
            # Ray Job logs are diagnostic only. Missing envelopes remain
            # non-terminal until the bounded grace period expires.
            if self._completion_envelope_grace_expired(task, now=now):
                handled = self._handle_task_failure(
                    task,
                    error_message="Ray Job completed without a completion envelope",
                    exception_type="RayCompletionUnknown",
                    expected_ray_job_id=ray_job_id,
                    expected_attempt_number=expected_attempt_number,
                    expected_execution_generation=expected_execution_generation,
                    expected_completion_data=completion_data,
                    require_completion_data_match=True,
                )
                if handled is False:
                    return
                complete_tracking()
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
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=completion_data,
                require_completion_data_match=True,
            )
            if handled is False:
                return
            complete_tracking()
            self.stdout.write(self.style.ERROR(f"\nTask {task.pk} failed: {job_info.message}"))
            return

        if job_info.status == JobStatus.STOPPED:
            if not cancel_task(
                task,
                allowed_states=(TaskState.RUNNING,),
                expected_ray_job_id=ray_job_id,
                expected_attempt_number=expected_attempt_number,
                expected_execution_generation=expected_execution_generation,
                expected_completion_data=completion_data,
                require_completion_data_match=True,
            ):
                return
            complete_tracking()
            self.stdout.write(self.style.WARNING(f"\nTask {task.pk} was stopped"))
            return

        if job_info.status == JobStatus.UNKNOWN and resolve_stale_untrusted_execution(
            expected_completion_data=None,
            error_message="Ray Job status remained UNKNOWN past the stuck-task timeout",
            log_detail="Ray job status remained unknown past the stuck-task timeout",
        ):
            return

        scope = "orphaned " if orphaned else ""
        self.stdout.write(
            self.style.WARNING(
                f"\nTask {task.pk} {scope}Ray job status is unknown: {job_info.message or 'no details'}"
            )
        )

    def _request_timeout_cancellation(self, task: RayTaskExecution) -> CancellationOutcome:
        """Stop a timed-out execution by its exact recorded backend identity."""
        return self._request_cancellation_for_task(task)

    def reconcile_tasks(self) -> int:
        """Reconcile task states with Ray."""
        if self.sync_mode:
            return 0

        from django_ray.runner.leasing import get_active_workers
        from django_ray.runner.ray_job import RayJobRunner

        runner = RayJobRunner()
        completed_tasks: list[int] = []
        reconciled_task_ids: set[int] = set()
        active_task_ids_before = set(self.active_tasks)

        for task_pk, ray_job_id in list(self.active_tasks.items()):
            tracked_identity = self.active_task_identities.get(task_pk)
            try:
                task = RayTaskExecution.objects.get(pk=task_pk)
                reconciled_task_ids.add(task.pk)
                self._reconcile_ray_job_task(
                    task,
                    runner,
                    ray_job_id=ray_job_id,
                    completed_tasks=completed_tasks,
                    orphaned=False,
                    tracked_identity=tracked_identity,
                )

            except RayTaskExecution.DoesNotExist:
                completed_tasks.append(task_pk)
                if tracked_identity is not None:
                    self._retire_active_ray_job_tracking(
                        task_pk,
                        ray_job_id=ray_job_id,
                        identity=tracked_identity,
                    )
                elif self.active_tasks.get(task_pk) == ray_job_id:
                    self.active_tasks.pop(task_pk, None)
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

        adopted_count = len(set(self.active_tasks) - active_task_ids_before)
        return len(completed_tasks) + adopted_count

    def detect_stuck_tasks(self) -> int:
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
        ray_core_pending_handles = (
            {handle.task_pk: handle for handle in self.ray_core_runner.pending_task_handles}
            if self.ray_core_runner
            else {}
        )

        stuck_count = 0
        timeout_count = 0
        orphan_recovered_count = 0
        for task in running_tasks:
            task_worker_id = str(task.claimed_by_worker) if task.claimed_by_worker else None
            claimed_by_this_worker = task_worker_id == self.worker_id
            claimed_by_active_worker = bool(task_worker_id) and task_worker_id in active_worker_ids
            pending_ray_core_handle = ray_core_pending_handles.get(task.pk)
            pending_ray_core_identity_matches = bool(
                pending_ray_core_handle
                and task.attempt_number == pending_ray_core_handle.attempt_number
                and task.execution_generation == pending_ray_core_handle.execution_generation
            )
            active_ray_job_identity_matches = bool(
                self.active_tasks.get(task.pk) == str(task.ray_job_id or "")
                and self.active_task_identities.get(task.pk)
                == (int(task.attempt_number), int(task.execution_generation))
            )

            # Ray Job entrypoints publish their terminal envelope while the row
            # is still RUNNING. Reconciliation owns that channel; timeout or
            # generic LOST recovery must not overwrite a preexisting envelope.
            if task.completion_data is not None:
                continue

            # Leave tasks owned by healthy workers alone.
            if not claimed_by_this_worker and claimed_by_active_worker:
                continue

            # Check for timeout first (applies to all tasks)
            if is_task_timed_out(task):
                self.stdout.write(
                    self.style.WARNING(f"\nTask {task.pk} timed out after {task.timeout_seconds}s")
                )
                # Request the exact backend stop first, then conditionally
                # finalize so a concurrent completion cannot be overwritten.
                cancellation = self._request_timeout_cancellation(task)
                marked_timed_out = mark_task_timed_out(
                    task,
                    cancellation_status=CancellationStatus(cancellation.status.value),
                    cancellation_error=cancellation.message,
                    expected_ray_job_id=str(task.ray_job_id) if task.ray_job_id else None,
                    expected_attempt_number=task.attempt_number,
                    expected_execution_generation=task.execution_generation,
                    expected_completion_data=None,
                    require_completion_data_match=True,
                )
                if marked_timed_out:
                    if task.pk in self.active_tasks:
                        del self.active_tasks[task.pk]
                        self.active_task_identities.pop(task.pk, None)
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

            # Persisted Ray Job IDs are recoverable capabilities even when this
            # worker is not currently tracking them. Exact reconciliation owns
            # UNKNOWN/stale resolution and best-effort stop; generic LOST retry
            # must never launch a replacement while that job may still run.
            if str(task.ray_job_id or "").startswith("raysubmit_"):
                continue

            # An exact local Ray Core handle is stronger ownership evidence than
            # its persisted monitor timestamp. Polling owns completion and
            # disconnect recovery for that handle; generic LOST recovery must
            # not requeue the same PK while its ObjectRef remains live.
            if claimed_by_this_worker and (
                pending_ray_core_identity_matches or active_ray_job_identity_matches
            ):
                continue

            tracked_by_this_worker = claimed_by_this_worker and (
                pending_ray_core_identity_matches or active_ray_job_identity_matches
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

                if not mark_task_lost(task):
                    continue

                # Check if we should retry the lost task
                retry_decision = should_retry(task, exception_type="TaskLost")
                if retry_decision.should_retry:
                    try:
                        retried = retry_task(
                            task.pk,
                            allowed_states=(TaskState.LOST,),
                            next_attempt_at=retry_decision.next_attempt_at,
                            expected_attempt_number=task.attempt_number,
                            expected_execution_generation=task.execution_generation,
                        )
                    except RuntimeEnvSnapshotError as error:
                        retried = None
                        self.stdout.write(self.style.ERROR(f"  Automatic retry blocked: {error}"))
                    if retried is not None:
                        task = retried
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

        return stuck_count + timeout_count

    def cleanup_expired_leases(self) -> int:
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
            return deleted_count
        except Exception as e:
            # Don't fail on lease cleanup errors
            self.logger.warning(f"Failed to cleanup expired leases: {e}")
            return 0

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
            "attempt_number": task.attempt_number,
            "execution_generation": task.execution_generation,
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

        if self.ray_core_runner is not None:
            pending_handle = self.ray_core_runner.get_pending_handle(
                task.pk,
                attempt_number=task.attempt_number,
                execution_generation=task.execution_generation,
            )
        else:
            pending_handle = None

        if pending_handle is not None:
            try:
                assert self.ray_core_runner is not None
                accepted = self.ray_core_runner.cancel_pending(pending_handle)
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
                "Exact Ray Core handle unavailable while recovering cancellation",
            )

        return CancellationOutcome(CancellationOutcomeStatus.NOT_APPLICABLE)

    def process_cancellations(self) -> int:
        """Adopt and finalize cancellation requests left by dead workers."""
        from django_ray.runner.leasing import get_active_workers

        active_worker_ids = {str(lease.worker_id) for lease in get_active_workers()}
        cancelling_tasks = RayTaskExecution.objects.filter(state=TaskState.CANCELLING)
        finalized_count = 0

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
                self.active_task_identities.pop(task.pk, None)

            finalized = finalize_cancellation(
                task,
                expected_worker_id=self.worker_id,
                expected_attempt_number=task.attempt_number,
                expected_execution_generation=task.execution_generation,
                cancellation_status=CancellationStatus(cancellation.status.value),
                cancellation_error=cancellation.message,
            )
            if finalized:
                finalized_count += 1
                self.stdout.write(self.style.SUCCESS(f"  Task {task.pk} cancelled"))

        return finalized_count

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
            for pending_handle in self.ray_core_runner.pending_task_handles:
                try:
                    task = RayTaskExecution.objects.get(pk=pending_handle.task_pk)
                except RayTaskExecution.DoesNotExist:
                    continue
                if (
                    task.state != TaskState.RUNNING
                    or task.attempt_number != pending_handle.attempt_number
                    or task.execution_generation != pending_handle.execution_generation
                ):
                    continue
                outcome = CancellationOutcome(CancellationOutcomeStatus.INDETERMINATE)
                try:
                    accepted = self.ray_core_runner.cancel_pending(pending_handle)
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
                    attempt_number=pending_handle.attempt_number,
                    execution_generation=pending_handle.execution_generation,
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
                identity = self.active_task_identities.get(task_pk)
                if identity is None:
                    updated = 0
                else:
                    attempt_number, execution_generation = identity
                    updated = RayTaskExecution.objects.filter(
                        pk=task_pk,
                        state=TaskState.RUNNING,
                        claimed_by_worker=self.worker_id,
                        ray_job_id=ray_job_id,
                        attempt_number=attempt_number,
                        execution_generation=execution_generation,
                    ).update(claimed_by_worker=None, last_heartbeat_at=now)
                if updated:
                    self.stdout.write(
                        self.style.NOTICE(
                            f"  Ray Job task {task_pk} handed off for continued monitoring"
                        )
                    )
                self.active_tasks.pop(task_pk, None)
                self.active_task_identities.pop(task_pk, None)

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
