"""Django management command for running the django-ray worker."""

from __future__ import annotations

import json
import signal
import time
from datetime import datetime, timezone
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from django_ray.conf.settings import get_settings
from django_ray.logging import get_worker_logger
from django_ray.models import RayTaskExecution, RayWorkerLease, TaskState
from django_ray.runner.cancellation import finalize_cancellation
from django_ray.runner.leasing import generate_worker_id, get_heartbeat_interval
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
        self.active_tasks: dict[int, str] = {}  # task_pk -> ray_job_id
        self.local_ray_tasks: dict[int, Any] = {}  # task_pk -> ray ObjectRef
        self.last_reconciliation = 0.0  # Last time we ran stuck task detection
        self.reconciliation_interval = 30.0  # Check for stuck tasks every 30 seconds
        self.lease: RayWorkerLease | None = None  # Worker lease for coordination

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--queue",
            type=str,
            default="default",
            help="Queue name to process (default: default)",
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
        queue = options["queue"]
        concurrency = options.get("concurrency")
        self.sync_mode = options.get("sync", False)
        self.local_mode = options.get("local", False)
        self.cluster_address = options.get("cluster")

        # Determine execution mode
        if self.sync_mode:
            self.execution_mode = "sync"
        elif self.local_mode:
            self.execution_mode = "local"
            self._init_local_ray()
        elif self.cluster_address:
            self.execution_mode = "cluster"
            self._init_cluster_ray(self.cluster_address)
        else:
            self.execution_mode = "ray"

        settings = get_settings()
        if concurrency is None:
            concurrency = settings.get("DEFAULT_CONCURRENCY", 10)

        self.setup_signal_handlers()

        self.stdout.write(
            self.style.SUCCESS(f"Starting django-ray worker {self.worker_id}")
        )
        self.stdout.write(f"  Queue: {queue}")
        self.stdout.write(f"  Concurrency: {concurrency}")
        self.stdout.write(f"  Mode: {self.execution_mode}")

        heartbeat_interval = get_heartbeat_interval().total_seconds()

        # Create worker lease for distributed coordination
        self._create_lease(queue)

        try:
            self.run_loop(
                queue=queue,
                concurrency=concurrency,
                heartbeat_interval=heartbeat_interval,
            )
        except KeyboardInterrupt:
            self.stdout.write("\nShutdown requested via keyboard interrupt")
        finally:
            self.shutdown()

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
        """Connect to a remote Ray cluster."""
        import ray

        if not ray.is_initialized():
            self.stdout.write(f"Connecting to Ray cluster at {address}...")
            try:
                ray.init(
                    address=address,
                    ignore_reinit_error=True,
                )
                self.stdout.write(self.style.SUCCESS("Connected to Ray cluster"))
                # Show cluster resources
                resources = ray.cluster_resources()
                self.stdout.write(f"  Cluster resources: {resources}")
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"Failed to connect to Ray cluster: {e}")
                )
                raise

    def _create_lease(self, queue: str) -> None:
        """Create a worker lease for distributed coordination.

        The lease tracks active workers and enables detection of
        crashed workers through heartbeat expiration.

        Args:
            queue: The queue this worker is processing.
        """
        import os
        import socket

        try:
            self.lease = RayWorkerLease.objects.create(
                worker_id=self.worker_id,
                hostname=socket.gethostname(),
                pid=os.getpid(),
                queue_name=queue,
            )
            self.stdout.write(
                self.style.SUCCESS(f"  Lease created: {self.worker_id}")
            )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"  Failed to create lease: {e}")
            )
            # Continue without lease - worker will still function

    def setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self.handle_shutdown_signal)
        signal.signal(signal.SIGINT, self.handle_shutdown_signal)

    def handle_shutdown_signal(self, signum: int, frame: FrameType | None) -> None:
        """Handle shutdown signals."""
        self.stdout.write(
            self.style.WARNING(f"\nReceived signal {signum}, shutting down...")
        )
        self.shutdown_requested = True

    def run_loop(
        self,
        queue: str,
        concurrency: int,
        heartbeat_interval: float,
    ) -> None:
        """Run the main worker loop."""
        last_heartbeat = 0.0

        while not self.shutdown_requested:
            current_time = time.time()

            # Heartbeat
            if current_time - last_heartbeat >= heartbeat_interval:
                self.send_heartbeat()
                last_heartbeat = current_time

            # Poll for completed local Ray tasks
            if self.execution_mode in ("local", "cluster") and self.local_ray_tasks:
                self.poll_local_ray_tasks()

            # Claim and process tasks
            self.claim_and_process_tasks(queue, concurrency)

            # Reconcile stuck tasks (periodically)
            if current_time - self.last_reconciliation >= self.reconciliation_interval:
                self.reconcile_tasks()
                self.detect_stuck_tasks()
                self.process_cancellations()
                self.last_reconciliation = current_time

            # Sleep briefly to avoid busy-waiting
            time.sleep(0.1)

    def send_heartbeat(self) -> None:
        """Send worker heartbeat and update lease."""
        from django.utils import timezone

        # Update worker lease if we have one
        if self.lease is not None:
            try:
                self.lease.last_heartbeat_at = timezone.now()
                self.lease.save(update_fields=["last_heartbeat_at"])
            except Exception:
                pass  # Best effort - don't fail on heartbeat issues

        self.stdout.write(".", ending="")
        self.stdout.flush()

    def claim_and_process_tasks(self, queue: str, concurrency: int) -> None:
        """Claim and submit tasks for execution."""
        # Check how many slots are available
        active_count = len(self.active_tasks) + len(self.local_ray_tasks)
        available_slots = concurrency - active_count
        if available_slots <= 0:
            return

        # Claim tasks
        now = datetime.now(timezone.utc)
        with transaction.atomic():
            # Find queued tasks that are ready to run
            tasks = list(
                RayTaskExecution.objects.select_for_update(skip_locked=True)
                .filter(
                    state=TaskState.QUEUED,
                    queue_name=queue,
                )
                .filter(
                    # run_after is null OR run_after <= now
                    run_after__isnull=True,
                )[:available_slots]
            )

            # Also get tasks with run_after <= now
            if len(tasks) < available_slots:
                more_tasks = list(
                    RayTaskExecution.objects.select_for_update(skip_locked=True).filter(
                        state=TaskState.QUEUED,
                        queue_name=queue,
                        run_after__lte=now,
                    )[: available_slots - len(tasks)]
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
        self.stdout.write(
            self.style.NOTICE(f"\nProcessing task {task.pk}: {task.callable_path}")
        )

        if self.execution_mode == "sync":
            self.execute_task_sync(task)
        elif self.execution_mode in ("local", "cluster"):
            self.execute_task_local_ray(task)
        else:
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

            now = datetime.now(timezone.utc)
            if result["success"]:
                task.state = TaskState.SUCCEEDED
                task.result_data = json.dumps(result["result"])
                task.finished_at = now
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Task {task.pk} succeeded: {result['result']}"
                    )
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
            task.attempt_number += 1
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
            task.finished_at = datetime.now(timezone.utc)
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
                self.style.ERROR(
                    f"  Task {task.pk} failed permanently ({reason}): {error_message}"
                )
            )

    def execute_task_local_ray(self, task: RayTaskExecution) -> None:
        """Submit a task to local Ray (non-blocking)."""
        import ray

        # Extract short name from callable path for dashboard visibility
        task_name = task.callable_path.split(".")[-1] if task.callable_path else "task"

        @ray.remote(name=f"django_ray:{task_name}")
        def run_task(
            callable_path: str, args_json: str, kwargs_json: str, task_id: int
        ) -> str:
            import json
            import sys

            print(f"[Task {task_id}] Starting: {callable_path}", flush=True)

            from django_ray.runtime.entrypoint import execute_task

            result = execute_task(callable_path, args_json, kwargs_json)

            # Print the result so it's visible in Ray dashboard stdout
            parsed = json.loads(result)
            if parsed.get("success"):
                print(f"[Task {task_id}] SUCCESS: {parsed.get('result')}", flush=True)
            else:
                print(
                    f"[Task {task_id}] FAILED: {parsed.get('error')}",
                    file=sys.stderr,
                    flush=True,
                )

            return result

        try:
            self.stdout.write(f"  Submitting to Ray as 'django_ray:{task_name}'...")
            # Submit to Ray WITHOUT blocking - store the reference for later polling
            result_ref = run_task.remote(
                task.callable_path,
                task.args_json,
                task.kwargs_json,
                task.pk,
            )
            # Track the pending task
            self.local_ray_tasks[task.pk] = result_ref
            self.stdout.write(
                self.style.SUCCESS(f"  Task {task.pk} submitted to Ray (async)")
            )

        except Exception as e:
            task.state = TaskState.FAILED
            task.error_message = str(e)
            task.finished_at = datetime.now(timezone.utc)
            task.save(update_fields=["state", "error_message", "finished_at"])
            self.stdout.write(
                self.style.ERROR(f"  Task {task.pk} failed to submit: {e}")
            )

    def poll_local_ray_tasks(self) -> None:
        """Poll for completed local Ray tasks and update their status."""
        import ray

        if not self.local_ray_tasks:
            return

        # Get list of all pending refs
        pending_refs = list(self.local_ray_tasks.values())

        # Check for completed tasks (non-blocking with timeout=0)
        ready_refs, _ = ray.wait(pending_refs, num_returns=len(pending_refs), timeout=0)

        if not ready_refs:
            return

        # Process completed tasks
        for ref in ready_refs:
            # Find the task_pk for this ref
            task_pk = None
            for pk, r in self.local_ray_tasks.items():
                if r == ref:
                    task_pk = pk
                    break

            if task_pk is None:
                continue

            # Remove from tracking
            del self.local_ray_tasks[task_pk]

            # Get the task from DB
            try:
                task = RayTaskExecution.objects.get(pk=task_pk)
            except RayTaskExecution.DoesNotExist:
                continue

            # Get the result
            try:
                result_json = ray.get(ref)
                result = json.loads(result_json)

                now = datetime.now(timezone.utc)
                if result["success"]:
                    task.state = TaskState.SUCCEEDED
                    task.result_data = json.dumps(result["result"])
                    task.finished_at = now
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"\nTask {task.pk} succeeded (Ray): {result['result']}"
                        )
                    )
                    task.save(
                        update_fields=[
                            "state",
                            "result_data",
                            "finished_at",
                        ]
                    )
                else:
                    # Task failed - use retry logic
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

    def submit_task_to_ray(self, task: RayTaskExecution) -> None:
        """Submit a task to Ray for execution."""
        from django_ray.runtime.serialization import deserialize_args
        from django_ray.runner.ray_job import RayJobRunner

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
                self.style.SUCCESS(
                    f"  Task {task.pk} submitted as Ray job {handle.ray_job_id}"
                )
            )

        except Exception as e:
            task.state = TaskState.FAILED
            task.error_message = f"Failed to submit to Ray: {e}"
            task.finished_at = datetime.now(timezone.utc)
            task.save(update_fields=["state", "error_message", "finished_at"])
            self.stdout.write(
                self.style.ERROR(f"  Task {task.pk} failed to submit: {e}")
            )

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
                handle = SubmissionHandle(
                    ray_job_id=ray_job_id,
                    ray_address=task.ray_address or "",
                    submitted_at=task.started_at or datetime.now(timezone.utc),
                )

                job_info = runner.get_status(handle)

                if job_info.status == JobStatus.SUCCEEDED:
                    # Get logs which contain the result
                    logs = runner.get_logs(handle)
                    task.state = TaskState.SUCCEEDED
                    task.finished_at = datetime.now(timezone.utc)
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
                    task.finished_at = datetime.now(timezone.utc)
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
                    task.finished_at = datetime.now(timezone.utc)
                    task.save()
                    completed_tasks.append(task_pk)
                    self.stdout.write(
                        self.style.WARNING(f"\nTask {task_pk} was stopped")
                    )

            except RayTaskExecution.DoesNotExist:
                completed_tasks.append(task_pk)
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"\nError reconciling task {task_pk}: {e}")
                )

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
                    self.style.WARNING(
                        f"\nTask {task.pk} timed out after {task.timeout_seconds}s"
                    )
                )
                # Cancel the running task if we're tracking it
                if task.pk in self.local_ray_tasks:
                    import ray
                    try:
                        ray.cancel(self.local_ray_tasks[task.pk], force=True)
                    except Exception:
                        pass
                    del self.local_ray_tasks[task.pk]
                if task.pk in self.active_tasks:
                    del self.active_tasks[task.pk]

                mark_task_timed_out(task)
                timeout_count += 1
                continue

            # Skip tasks we're actively tracking for stuck check (they're still running)
            if task.pk in self.local_ray_tasks or task.pk in self.active_tasks:
                continue

            # Check if task is stuck using the reconciliation logic
            if is_task_stuck(task):
                self.stdout.write(
                    self.style.WARNING(
                        f"\nTask {task.pk} appears stuck, marking as LOST"
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
            self.stdout.write(
                self.style.WARNING(f"Detected {stuck_count} stuck task(s)")
            )
        if timeout_count > 0:
            self.stdout.write(
                self.style.WARNING(f"Detected {timeout_count} timed out task(s)")
            )

    def process_cancellations(self) -> None:
        """Process tasks that have been requested for cancellation.

        This checks for tasks in CANCELLING state and finalizes their cancellation.
        """
        cancelling_tasks = RayTaskExecution.objects.filter(
            state=TaskState.CANCELLING,
            claimed_by_worker=self.worker_id,
        )

        for task in cancelling_tasks:
            self.stdout.write(
                self.style.WARNING(f"\nFinalizing cancellation for task {task.pk}")
            )

            # Remove from our tracking if present
            if task.pk in self.local_ray_tasks:
                # Try to cancel the Ray task
                import ray

                try:
                    ray.cancel(self.local_ray_tasks[task.pk], force=True)
                except Exception:
                    pass  # Best effort
                del self.local_ray_tasks[task.pk]

            if task.pk in self.active_tasks:
                del self.active_tasks[task.pk]

            # Finalize the cancellation
            finalize_cancellation(task)
            self.stdout.write(
                self.style.SUCCESS(f"  Task {task.pk} cancelled")
            )

    def shutdown(self) -> None:
        """Perform graceful shutdown."""
        # Delete worker lease to signal we're gone
        if self.lease is not None:
            try:
                self.lease.delete()
                self.stdout.write("  Lease released")
            except Exception as e:
                self.stdout.write(f"  Failed to release lease: {e}")

        self.stdout.write(
            self.style.SUCCESS(f"\nWorker {self.worker_id} shut down cleanly")
        )
