"""Django models for django-ray task tracking."""

from __future__ import annotations

from uuid import uuid4

from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models
from django.utils import timezone

_SHA256_HEX_VALIDATOR = RegexValidator(
    regex=r"^[0-9a-f]{64}$",
    message="Value must be a lowercase hexadecimal SHA-256 digest.",
)
_WORKFLOW_TOPOLOGY_NODE_LIMIT = 25_000
_WORKFLOW_TOPOLOGY_EDGE_LIMIT = 100_000
_WORKFLOW_TOPOLOGY_ENCODED_BYTES_LIMIT = 16 * 1024 * 1024
_WORKFLOW_TOPOLOGY_DECODED_BYTES_LIMIT = 32 * 1024 * 1024
_WORKFLOW_TOPOLOGY_PAGE_ITEMS_LIMIT = 256
_WORKFLOW_TOPOLOGY_MANIFEST_BYTES_LIMIT = 256 * 1024
_WORKFLOW_TOPOLOGY_PAGE_ENCODED_BYTES_LIMIT = 256 * 1024
_WORKFLOW_TOPOLOGY_PAGE_DECODED_BYTES_LIMIT = 1024 * 1024
_WORKFLOW_DETAIL_NODE_LIMIT = 25_000
_WORKFLOW_DETAIL_EVENT_LIMIT = 32
_WORKFLOW_DETAIL_ENCODED_BYTES_LIMIT = 8 * 1024 * 1024
_WORKFLOW_DETAIL_DECODED_BYTES_LIMIT = 16 * 1024 * 1024
_WORKFLOW_DETAIL_RECORD_BYTES_LIMIT = 16 * 1024


class TaskState(models.TextChoices):
    """Possible states for a task execution."""

    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"
    CANCELLING = "CANCELLING", "Cancelling"
    LOST = "LOST", "Lost"
    EXPIRED = "EXPIRED", "Expired"


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


class WorkflowProgressTopologySlot(models.TextChoices):
    """One retained immutable topology role within a workflow run."""

    CURRENT = "CURRENT", "Current"
    PENDING = "PENDING", "Pending"


class WorkflowProgressTopologyCollection(models.TextChoices):
    """Record collection stored by one immutable topology page."""

    NODE = "NODE", "Node"
    EDGE = "EDGE", "Edge"


class WorkflowProgressTopologyEncoding(models.TextChoices):
    """Protocol-v1 topology-page payload encoding."""

    IDENTITY = "identity", "Identity"


class WorkflowProgressNodeState(models.TextChoices):
    """Normalized latest-state vocabulary for a retained workflow node."""

    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class RayTaskExecution(models.Model):
    """Tracks the execution of a Django Task on Ray.

    This is the canonical source of truth for task state.
    """

    # Task identification
    task_id = models.CharField(
        max_length=255,
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

    # Ray routing and submitted-job tracking
    ray_target_address = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Immutable Ray cluster target selected when this task was enqueued",
    )
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
    queue_timeout_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Snapshotted queued-wait budget in seconds (None = unlimited)",
    )
    queue_deadline_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Absolute instant at which queued work expires",
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
                fields=["state", "queue_name", "queue_deadline_at"],
                name="ray_task_expiry_idx",
            ),
            models.Index(
                fields=["state", "last_heartbeat_at"],
                name="ray_task_heartbeat_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["task_id"],
                name="ray_task_id_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(priority__gte=-100, priority__lte=100),
                name="ray_task_priority_valid_range",
            ),
        ]
        verbose_name = "Ray Task Execution"
        verbose_name_plural = "Ray Task Executions"

    def __str__(self) -> str:
        return f"{self.callable_path} ({self.state})"


class WorkflowProgressRunStorage(models.Model):
    """Run-scoped aggregate and retention state for normalized progress detail."""

    execution = models.ForeignKey(
        RayTaskExecution,
        on_delete=models.CASCADE,
        related_name="workflow_progress_runs",
    )
    attempt_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    execution_generation = models.PositiveBigIntegerField()
    run_id = models.UUIDField()
    detail_revision = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    detail_node_count = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_NODE_LIMIT)],
    )
    detail_pending_count = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_NODE_LIMIT)],
    )
    detail_running_count = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_NODE_LIMIT)],
    )
    detail_succeeded_count = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_NODE_LIMIT)],
    )
    detail_failed_count = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_NODE_LIMIT)],
    )
    detail_truncated_count = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_NODE_LIMIT)],
    )
    detail_event_count = models.PositiveIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_EVENT_LIMIT)],
    )
    detail_truncation_reasons = models.CharField(
        max_length=256,
        blank=True,
        default="",
    )
    detail_encoded_bytes = models.PositiveBigIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_ENCODED_BYTES_LIMIT)],
    )
    detail_decoded_bytes = models.PositiveBigIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_DECODED_BYTES_LIMIT)],
    )
    detail_retention_days = models.PositiveSmallIntegerField(
        default=7,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
    )
    detail_expires_at = models.DateTimeField(null=True, blank=True)
    cleanup_error = models.CharField(
        max_length=2000,
        null=True,
        blank=True,
        editable=False,
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["execution", "attempt_number", "execution_generation", "run_id"],
                name="ray_wf_run_identity_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_number__gte=1),
                name="ray_wf_run_attempt_pos",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(detail_revision__isnull=True) | models.Q(detail_revision__gte=1)
                ),
                name="ray_wf_run_detail_rev_pos",
            ),
            models.CheckConstraint(
                condition=models.Q(detail_node_count__lte=_WORKFLOW_DETAIL_NODE_LIMIT),
                name="ray_wf_run_detail_count_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    detail_pending_count__lte=_WORKFLOW_DETAIL_NODE_LIMIT,
                    detail_running_count__lte=_WORKFLOW_DETAIL_NODE_LIMIT,
                    detail_succeeded_count__lte=_WORKFLOW_DETAIL_NODE_LIMIT,
                    detail_failed_count__lte=_WORKFLOW_DETAIL_NODE_LIMIT,
                ),
                name="ray_wf_run_state_counts_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    detail_node_count=(
                        models.F("detail_pending_count")
                        + models.F("detail_running_count")
                        + models.F("detail_succeeded_count")
                        + models.F("detail_failed_count")
                    )
                ),
                name="ray_wf_run_state_counts_sum",
            ),
            models.CheckConstraint(
                condition=models.Q(detail_truncated_count__lte=models.F("detail_node_count")),
                name="ray_wf_run_truncated_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(detail_event_count__lte=_WORKFLOW_DETAIL_EVENT_LIMIT),
                name="ray_wf_run_event_count_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(detail_encoded_bytes__lte=_WORKFLOW_DETAIL_ENCODED_BYTES_LIMIT),
                name="ray_wf_run_encoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(detail_decoded_bytes__lte=_WORKFLOW_DETAIL_DECODED_BYTES_LIMIT),
                name="ray_wf_run_decoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(detail_decoded_bytes=models.F("detail_encoded_bytes")),
                name="ray_wf_run_detail_size_eq",
            ),
            models.CheckConstraint(
                condition=models.Q(detail_retention_days__gte=0, detail_retention_days__lte=30),
                name="ray_wf_run_retention_days",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        detail_node_count=0,
                        detail_pending_count=0,
                        detail_running_count=0,
                        detail_succeeded_count=0,
                        detail_failed_count=0,
                        detail_truncated_count=0,
                        detail_event_count=0,
                        detail_encoded_bytes=0,
                        detail_decoded_bytes=0,
                    )
                    | models.Q(
                        detail_node_count__gte=1,
                        detail_encoded_bytes__gte=1,
                        detail_decoded_bytes__gte=1,
                    )
                ),
                name="ray_wf_run_detail_totals",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(detail_revision__isnull=False)
                    | models.Q(
                        detail_node_count=0,
                        detail_pending_count=0,
                        detail_running_count=0,
                        detail_succeeded_count=0,
                        detail_failed_count=0,
                        detail_truncated_count=0,
                        detail_event_count=0,
                        detail_truncation_reasons="",
                        detail_encoded_bytes=0,
                        detail_decoded_bytes=0,
                    )
                ),
                name="ray_wf_run_unpub_empty",
            ),
        ]
        indexes = [
            models.Index(
                fields=["detail_expires_at", "id"],
                name="ray_wf_run_expiry_idx",
            )
        ]


class WorkflowProgressTopologyManifest(models.Model):
    """One immutable current or pending topology manifest for a workflow run."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    run_storage = models.ForeignKey(
        WorkflowProgressRunStorage,
        on_delete=models.CASCADE,
        related_name="topology_manifests",
    )
    topology_version = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    slot = models.CharField(
        max_length=7,
        choices=WorkflowProgressTopologySlot.choices,
    )
    manifest_digest = models.CharField(
        max_length=64,
        validators=[_SHA256_HEX_VALIDATOR],
    )
    truncation_reasons = models.CharField(
        max_length=256,
        blank=True,
        default="",
    )
    payload = models.BinaryField(
        max_length=_WORKFLOW_TOPOLOGY_MANIFEST_BYTES_LIMIT,
        editable=False,
    )
    node_count = models.PositiveIntegerField(
        validators=[MaxValueValidator(_WORKFLOW_TOPOLOGY_NODE_LIMIT)]
    )
    edge_count = models.PositiveIntegerField(
        validators=[MaxValueValidator(_WORKFLOW_TOPOLOGY_EDGE_LIMIT)]
    )
    node_page_count = models.PositiveIntegerField(
        validators=[MaxValueValidator(_WORKFLOW_TOPOLOGY_NODE_LIMIT)]
    )
    edge_page_count = models.PositiveIntegerField(
        validators=[MaxValueValidator(_WORKFLOW_TOPOLOGY_EDGE_LIMIT)]
    )
    encoded_bytes = models.PositiveBigIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_TOPOLOGY_ENCODED_BYTES_LIMIT),
        ]
    )
    decoded_bytes = models.PositiveBigIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_TOPOLOGY_DECODED_BYTES_LIMIT),
        ]
    )
    created_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    cleanup_error = models.CharField(
        max_length=2000,
        null=True,
        blank=True,
        editable=False,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run_storage", "topology_version"],
                name="ray_wf_manifest_ver_uniq",
            ),
            models.UniqueConstraint(
                fields=["run_storage", "slot"],
                name="ray_wf_manifest_slot_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(topology_version__gte=1),
                name="ray_wf_manifest_ver_pos",
            ),
            models.CheckConstraint(
                condition=models.Q(node_count__lte=_WORKFLOW_TOPOLOGY_NODE_LIMIT),
                name="ray_wf_manifest_node_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(edge_count__lte=_WORKFLOW_TOPOLOGY_EDGE_LIMIT),
                name="ray_wf_manifest_edge_cap",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(node_count=0, node_page_count=0)
                    | models.Q(
                        node_count__gte=1,
                        node_page_count__gte=1,
                        node_page_count__lte=models.F("node_count"),
                    )
                ),
                name="ray_wf_manifest_node_pages",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(edge_count=0, edge_page_count=0)
                    | models.Q(
                        edge_count__gte=1,
                        edge_page_count__gte=1,
                        edge_page_count__lte=models.F("edge_count"),
                    )
                ),
                name="ray_wf_manifest_edge_pages",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    encoded_bytes__gte=1,
                    encoded_bytes__lte=_WORKFLOW_TOPOLOGY_ENCODED_BYTES_LIMIT,
                ),
                name="ray_wf_manifest_encoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    decoded_bytes__gte=1,
                    decoded_bytes__lte=_WORKFLOW_TOPOLOGY_DECODED_BYTES_LIMIT,
                ),
                name="ray_wf_manifest_decoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(decoded_bytes=models.F("encoded_bytes")),
                name="ray_wf_manifest_size_eq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        slot=WorkflowProgressTopologySlot.PENDING,
                        published_at__isnull=True,
                    )
                    | models.Q(
                        slot=WorkflowProgressTopologySlot.CURRENT,
                        published_at__isnull=False,
                    )
                ),
                name="ray_wf_manifest_slot_state",
            ),
        ]
        indexes = [
            models.Index(
                fields=["slot", "created_at"],
                name="ray_wf_manifest_gc_idx",
            )
        ]


class WorkflowProgressTopologyPage(models.Model):
    """Run-scoped content-addressed bytes for one immutable topology page."""

    run_storage = models.ForeignKey(
        WorkflowProgressRunStorage,
        on_delete=models.CASCADE,
        related_name="topology_pages",
    )
    digest = models.CharField(
        max_length=64,
        validators=[_SHA256_HEX_VALIDATOR],
    )
    collection = models.CharField(
        max_length=4,
        choices=WorkflowProgressTopologyCollection.choices,
    )
    encoding = models.CharField(
        max_length=16,
        choices=WorkflowProgressTopologyEncoding.choices,
        default=WorkflowProgressTopologyEncoding.IDENTITY,
    )
    payload = models.BinaryField(
        max_length=_WORKFLOW_TOPOLOGY_PAGE_ENCODED_BYTES_LIMIT,
        editable=False,
    )
    item_count = models.PositiveSmallIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_TOPOLOGY_PAGE_ITEMS_LIMIT),
        ]
    )
    encoded_bytes = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_TOPOLOGY_PAGE_ENCODED_BYTES_LIMIT),
        ]
    )
    decoded_bytes = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_TOPOLOGY_PAGE_DECODED_BYTES_LIMIT),
        ]
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run_storage", "digest"],
                name="ray_wf_page_digest_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    item_count__gte=1,
                    item_count__lte=_WORKFLOW_TOPOLOGY_PAGE_ITEMS_LIMIT,
                ),
                name="ray_wf_page_item_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    encoded_bytes__gte=1,
                    encoded_bytes__lte=_WORKFLOW_TOPOLOGY_PAGE_ENCODED_BYTES_LIMIT,
                ),
                name="ray_wf_page_encoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    decoded_bytes__gte=1,
                    decoded_bytes__lte=_WORKFLOW_TOPOLOGY_PAGE_DECODED_BYTES_LIMIT,
                ),
                name="ray_wf_page_decoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(decoded_bytes=models.F("encoded_bytes")),
                name="ray_wf_page_identity_size",
            ),
        ]
        indexes = [
            models.Index(
                fields=["created_at", "id"],
                name="ray_wf_page_gc_idx",
            )
        ]


class WorkflowProgressTopologyManifestPage(models.Model):
    """Ordered reference from one topology manifest to a reusable run page."""

    manifest = models.ForeignKey(
        WorkflowProgressTopologyManifest,
        on_delete=models.CASCADE,
        related_name="page_links",
    )
    page = models.ForeignKey(
        WorkflowProgressTopologyPage,
        on_delete=models.RESTRICT,
        related_name="manifest_links",
    )
    collection = models.CharField(
        max_length=4,
        choices=WorkflowProgressTopologyCollection.choices,
    )
    page_index = models.PositiveIntegerField()

    class Meta:
        ordering = ["collection", "page_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["manifest", "collection", "page_index"],
                name="ray_wf_link_position_uniq",
            ),
            models.UniqueConstraint(
                fields=["manifest", "page"],
                name="ray_wf_link_page_uniq",
            ),
        ]


class WorkflowProgressNodeDetail(models.Model):
    """One bounded latest-state record per stable node within a workflow run."""

    run_storage = models.ForeignKey(
        WorkflowProgressRunStorage,
        on_delete=models.CASCADE,
        related_name="node_details",
    )
    node_key = models.CharField(
        max_length=64,
        validators=[_SHA256_HEX_VALIDATOR],
    )
    node_id = models.CharField(max_length=256)
    invocation_id = models.CharField(max_length=128, null=True, blank=True)
    state = models.CharField(
        max_length=9,
        choices=WorkflowProgressNodeState.choices,
    )
    event_count = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(_WORKFLOW_DETAIL_EVENT_LIMIT)],
    )
    truncated = models.BooleanField(default=False)
    payload = models.BinaryField(
        max_length=_WORKFLOW_DETAIL_RECORD_BYTES_LIMIT,
        editable=False,
    )
    digest = models.CharField(
        max_length=64,
        validators=[_SHA256_HEX_VALIDATOR],
    )
    encoded_bytes = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_DETAIL_RECORD_BYTES_LIMIT),
        ]
    )
    decoded_bytes = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_DETAIL_RECORD_BYTES_LIMIT),
        ]
    )
    last_topology_version = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    last_detail_revision = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["run_storage", "node_key"],
                name="ray_wf_node_key_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(event_count__lte=_WORKFLOW_DETAIL_EVENT_LIMIT),
                name="ray_wf_node_event_count_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    encoded_bytes__gte=1,
                    encoded_bytes__lte=_WORKFLOW_DETAIL_RECORD_BYTES_LIMIT,
                ),
                name="ray_wf_node_encoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    decoded_bytes__gte=1,
                    decoded_bytes__lte=_WORKFLOW_DETAIL_RECORD_BYTES_LIMIT,
                ),
                name="ray_wf_node_decoded_cap",
            ),
            models.CheckConstraint(
                condition=models.Q(decoded_bytes=models.F("encoded_bytes")),
                name="ray_wf_node_identity_size",
            ),
            models.CheckConstraint(
                condition=models.Q(last_topology_version__gte=1),
                name="ray_wf_node_topology_pos",
            ),
            models.CheckConstraint(
                condition=models.Q(last_detail_revision__gte=1),
                name="ray_wf_node_detail_pos",
            ),
        ]
        indexes = [
            models.Index(
                fields=["run_storage", "state", "node_key"],
                name="ray_wf_node_state_idx",
            ),
            models.Index(
                fields=["run_storage", "event_count"],
                name="ray_wf_node_event_idx",
            ),
        ]


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
