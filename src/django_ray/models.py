"""Django models for django-ray task tracking."""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
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


class InputPayloadState(models.TextChoices):
    """Retention state for one durable external task-input payload."""

    ACTIVE = "ACTIVE", "Active"
    PURGED = "PURGED", "Purged"


class CancellationStatus(models.TextChoices):
    """Outcome of a remote cancellation request."""

    REQUESTED = "REQUESTED", "Stop requested"
    FAILED = "FAILED", "Stop request failed"
    INDETERMINATE = "INDETERMINATE", "Stop request indeterminate"
    NOT_APPLICABLE = "NOT_APPLICABLE", "No remote job to stop"


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
    priority = models.SmallIntegerField(
        default=0,
        validators=[MinValueValidator(-100), MaxValueValidator(100)],
        help_text="Django task priority (-100 to 100; larger values run sooner)",
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
    execution_generation = models.PositiveBigIntegerField(
        default=0,
        help_text="Monotonic token identifying the current execution generation",
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
    runtime_env_profile = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Named RuntimeEnv profile selected when this task was enqueued",
    )
    runtime_env_json = models.TextField(
        default="{}",
        help_text="Immutable JSON snapshot of the Ray RuntimeEnv used for this task",
    )
    runtime_env_hash = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="SHA-256 identity of the RuntimeEnv snapshot",
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
    input_reference = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        db_index=True,
        help_text="Reference to a durable external task-input envelope",
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
    progress_data = models.TextField(
        null=True,
        blank=True,
        help_text="JSON workflow progress snapshot for the durable outer task",
    )
    workflow_progress_summary_json = models.TextField(
        null=True,
        blank=True,
        editable=False,
        help_text="Bounded canonical schema-v3 workflow progress summary",
    )
    workflow_run_id = models.UUIDField(
        null=True,
        blank=True,
        editable=False,
        help_text="Current workflow invocation allowed to persist progress",
    )
    workflow_plan_fingerprint = models.CharField(
        max_length=71,
        null=True,
        blank=True,
        help_text="Pinned secret-free effective workflow plan identity",
    )
    workflow_plan_pinned_attempt = models.PositiveIntegerField(
        null=True,
        blank=True,
        editable=False,
        help_text="Attempt that first pinned the effective workflow plan identity",
    )
    workflow_plan_json = models.TextField(
        null=True,
        blank=True,
        help_text="Bounded canonical secret-free effective workflow plan",
    )
    workflow_plan_selection = models.TextField(
        null=True,
        blank=True,
        help_text="Bounded strategy eligibility and selection metadata",
    )
    completion_data = models.TextField(
        null=True,
        blank=True,
        help_text="JSON completion envelope durably written by the Ray Job driver",
    )

    # Cancellation tracking
    cancellation_status = models.CharField(
        max_length=20,
        choices=CancellationStatus.choices,
        null=True,
        blank=True,
        help_text="Outcome of the most recent remote cancellation request",
    )
    cancellation_error = models.TextField(
        null=True,
        blank=True,
        help_text="Details when remote cancellation failed or was indeterminate",
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
        constraints = [
            models.CheckConstraint(
                condition=models.Q(priority__gte=-100, priority__lte=100),
                name="ray_task_priority_valid_range",
            )
        ]
        verbose_name = "Ray Task Execution"
        verbose_name_plural = "Ray Task Executions"

    def __str__(self) -> str:
        return f"{self.callable_path} ({self.state})"


class TaskInputPayload(models.Model):
    """Durable registry and cleanup tombstone for a shared input payload."""

    reference = models.CharField(max_length=500, primary_key=True)
    backend = models.CharField(max_length=32)
    digest = models.CharField(max_length=64, db_index=True)
    size_bytes = models.PositiveBigIntegerField()
    envelope_version = models.PositiveSmallIntegerField()
    state = models.CharField(
        max_length=20,
        choices=InputPayloadState.choices,
        default=InputPayloadState.ACTIVE,
        db_index=True,
    )
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(default=timezone.now, db_index=True)
    purged_at = models.DateTimeField(null=True, blank=True)
    cleanup_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["created_at", "reference"]
        indexes = [
            models.Index(
                fields=["state", "last_used_at"],
                name="ray_input_cleanup_idx",
            )
        ]
        verbose_name = "Task Input Payload"
        verbose_name_plural = "Task Input Payloads"

    def __str__(self) -> str:
        return f"{self.backend} input {str(self.digest)[:12]} ({self.state})"


class TaskAttempt(models.Model):
    """Immutable-ish diagnostics for one execution attempt.

    ``RayTaskExecution`` remains the current durable snapshot while this model
    preserves the terminal outcome and diagnostics of each completed attempt.
    Rows are keyed by execution and the one-based attempt number.
    """

    execution = models.ForeignKey(
        RayTaskExecution,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField()
    state = models.CharField(max_length=20, choices=TaskState.choices)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    error_traceback = models.TextField(null=True, blank=True)
    result_data = models.TextField(null=True, blank=True)
    result_reference = models.CharField(max_length=500, null=True, blank=True)
    workflow_progress_summary_json = models.TextField(
        null=True,
        blank=True,
        editable=False,
        help_text="Bounded terminal workflow progress summary for this attempt",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["attempt_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["execution", "attempt_number"],
                name="ray_task_attempt_unique_number",
            )
        ]
        indexes = [models.Index(fields=["execution", "attempt_number"])]

    def __str__(self) -> str:
        return f"{self.execution_id} attempt {self.attempt_number} ({self.state})"


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
        worker_id = str(self.worker_id)
        return f"Worker {worker_id[:8]}... on {self.hostname} ({status})"
