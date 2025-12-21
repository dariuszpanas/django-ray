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


class RayWorkerLease(models.Model):
    """Tracks active worker processes for coordination.

    Optional model for distributed worker coordination.
    """

    worker_id = models.CharField(
        max_length=255,
        primary_key=True,
    )
    hostname = models.CharField(
        max_length=255,
    )
    pid = models.PositiveIntegerField()
    queue_name = models.CharField(
        max_length=100,
        default="default",
        db_index=True,
    )
    started_at = models.DateTimeField(
        default=timezone.now,
    )
    last_heartbeat_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        verbose_name = "Ray Worker Lease"
        verbose_name_plural = "Ray Worker Leases"

    def __str__(self) -> str:
        return f"{self.worker_id} on {self.hostname}"
