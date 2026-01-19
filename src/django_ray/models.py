"""Django models for django-ray task tracking."""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class TaskState(models.TextChoices):
    """Possible states for a task execution."""

    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"
    CANCELLING = "CANCELLING", "Cancelling"
    LOST = "LOST", "Lost"


class RayTaskExecution(models.Model):
    """Tracks the execution of a Django Task on Ray.

    This is the canonical source of truth for task state.
    """

    # Task identification
    task_id = models.CharField(
        max_length=255,
        db_index=True,
        help_text="ID from Django Tasks",
    )
    callable_path = models.CharField(
        max_length=500,
        help_text="Dotted path to the task callable",
    )
    queue_name = models.CharField(
        max_length=100,
        default="default",
        db_index=True,
        help_text="Queue this task belongs to",
    )

    # State tracking
    state = models.CharField(
        max_length=20,
        choices=TaskState.choices,
        default=TaskState.QUEUED,
        db_index=True,
    )
    attempt_number = models.PositiveIntegerField(
        default=1,
        help_text="Current attempt number",
    )

    # Ray job tracking
    ray_job_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        help_text="Ray Job ID",
    )
    ray_address = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Ray cluster address used",
    )

    # Timing
    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )
    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    finished_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    last_heartbeat_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    run_after = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Don't run before this time (for delayed/retry)",
    )
    timeout_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum execution time in seconds (None = no timeout)",
    )

    # Worker tracking
    claimed_by_worker = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Worker ID that claimed this task",
    )

    # Arguments (serialized JSON)
    args_json = models.TextField(
        default="[]",
        help_text="JSON-serialized positional arguments",
    )
    kwargs_json = models.TextField(
        default="{}",
        help_text="JSON-serialized keyword arguments",
    )

    # Results
    result_data = models.TextField(
        null=True,
        blank=True,
        help_text="JSON-serialized result (for small results)",
    )
    result_reference = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Reference to external result storage",
    )

    # Error tracking
    error_message = models.TextField(
        null=True,
        blank=True,
    )
    error_traceback = models.TextField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["state", "queue_name", "run_after"],
                name="ray_task_claimable_idx",
            ),
            models.Index(
                fields=["state", "last_heartbeat_at"],
                name="ray_task_heartbeat_idx",
            ),
        ]
        verbose_name = "Ray Task Execution"
        verbose_name_plural = "Ray Task Executions"

    def __str__(self) -> str:
        return f"{self.callable_path} ({self.state})"


class TaskWorkerLease(models.Model):
    """Tracks active Django task worker processes for coordination.

    This model tracks workers running the `django_ray_worker` management command,
    NOT Ray cluster workers. These Django workers:
    - Claim tasks from the database
    - Submit them to Ray for execution
    - Update task status when complete

    The lease enables detection of crashed workers through heartbeat expiration.
    Workers are marked inactive rather than deleted to preserve history.
    """

    worker_id = models.CharField(
        max_length=255,
        primary_key=True,
        help_text="Unique identifier for the worker process",
    )
    hostname = models.CharField(
        max_length=255,
        help_text="Machine hostname where the worker is running",
    )
    pid = models.PositiveIntegerField(
        help_text="Process ID of the worker",
    )
    queue_name = models.CharField(
        max_length=100,
        default="default",
        db_index=True,
        help_text="Queue(s) this worker is processing (informational only)",
    )
    started_at = models.DateTimeField(
        default=timezone.now,
        help_text="When the worker started",
    )
    last_heartbeat_at = models.DateTimeField(
        default=timezone.now,
        help_text="Last heartbeat from the worker",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether the worker is currently active (False = shutdown or expired)",
    )
    stopped_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the worker was stopped or marked inactive",
    )

    class Meta:
        verbose_name = "Task Worker Lease"
        verbose_name_plural = "Task Worker Leases"

    def __str__(self) -> str:
        status = "active" if self.is_active else "inactive"
        return f"Worker {self.worker_id[:8]}... on {self.hostname} ({status})"
