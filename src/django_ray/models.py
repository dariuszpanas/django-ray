"""Django models for django-ray task tracking."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.core.validators import (
    MaxLengthValidator,
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models
from django.utils import timezone

from django_ray.execution_protocol import (
    EXECUTION_METADATA_SCHEMA_VERSION,
    EXECUTION_PROTOCOL_VERSION,
    LEGACY_EXECUTION_METADATA_SCHEMA_VERSION,
    LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
    PROTOCOL_POLICY_SCHEMA_VERSION,
    WORKER_CAPABILITY_SCHEMA_VERSION,
)
from django_ray.target_attestation import (
    RAY_CLUSTER_ATTESTATION_MAX_BYTES,
    RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS,
    RAY_TARGET_EXPECTATION_MAX_BYTES,
    RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
    RayRunnerFamily,
)

_SHA256_HEX_VALIDATOR = RegexValidator(
    regex=r"^[0-9a-f]{64}$",
    message="Value must be a lowercase hexadecimal SHA-256 digest.",
)
_TAGGED_SHA256_HEX_VALIDATOR = RegexValidator(
    regex=r"\Asha256:[0-9a-f]{64}\Z",
    message="Value must be a canonical tagged lowercase SHA-256 digest.",
)
_RAY_TARGET_KEY_VALIDATOR = RegexValidator(
    regex=r"\A[a-z0-9][a-z0-9_.-]{0,127}\Z",
    message="Value must be a canonical Ray target key.",
)
_RAY_CLUSTER_SESSION_VALIDATOR = RegexValidator(
    regex=r"\Asession_[A-Za-z0-9][A-Za-z0-9_.-]{0,247}\Z",
    message="Value must be a canonical Ray cluster session name.",
)
_PYTHON_IMPLEMENTATION_VALIDATOR = RegexValidator(
    regex=r"\A[a-z][a-z0-9_.-]{0,63}\Z",
    message="Value must be a normalized Python implementation identifier.",
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
_WORKFLOW_RUN_NAMESPACE_MAX = (1 << 63) - 1
_WORKFLOW_RUN_SEQUENCE_MAX = (1 << 59) - 1


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


class InputPayloadKind(models.TextChoices):
    """Purpose of one durable external payload."""

    TASK_INPUT = "task_input", "Task input"
    RAY_JOB_REQUEST = "ray_job_request", "Ray Job request"


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


class RayTargetDesiredState(models.TextChoices):
    """Operator policy state for one dormant Ray target."""

    ACTIVE = "active", "Active"
    DRAINING = "draining", "Draining"
    RETIRED = "retired", "Retired"


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
    metadata_schema_version = models.PositiveSmallIntegerField(
        default=EXECUTION_METADATA_SCHEMA_VERSION,
        db_default=LEGACY_EXECUTION_METADATA_SCHEMA_VERSION,
        editable=False,
        validators=[MaxValueValidator(EXECUTION_METADATA_SCHEMA_VERSION)],
        help_text="Schema version for execution metadata stored on this row",
    )
    execution_protocol_version = models.PositiveSmallIntegerField(
        default=EXECUTION_PROTOCOL_VERSION,
        db_default=EXECUTION_PROTOCOL_VERSION,
        db_index=True,
        editable=False,
        validators=[MinValueValidator(1)],
        help_text="Immutable durable execution protocol selected when the task was created",
    )
    created_with_django_ray_version = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        editable=False,
        help_text="Diagnostic django-ray package version that created this task",
    )
    managed_with_django_ray_version = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        editable=False,
        help_text="Diagnostic django-ray package version managing the current attempt",
    )
    executor_django_ray_version = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        editable=False,
        help_text="Diagnostic django-ray package version that executed the current attempt",
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
    ray_job_request_reference = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        db_index=True,
        help_text="Reference to the durable Ray Job submission request",
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
    workflow_run_namespace = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        editable=False,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(_WORKFLOW_RUN_NAMESPACE_MAX),
        ],
        help_text="Database-unique opaque namespace for workflow run IDs",
    )
    workflow_run_sequence = models.PositiveBigIntegerField(
        default=0,
        db_default=0,
        editable=False,
        validators=[MaxValueValidator(_WORKFLOW_RUN_SEQUENCE_MAX)],
        help_text="Monotonic database allocation sequence for workflow run IDs",
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
        permissions = [
            (
                "view_sensitive_task_data",
                "Can view unredacted task data",
            )
        ]
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
            models.CheckConstraint(
                condition=models.Q(
                    metadata_schema_version__in=(
                        LEGACY_EXECUTION_METADATA_SCHEMA_VERSION,
                        EXECUTION_METADATA_SCHEMA_VERSION,
                    )
                ),
                name="ray_task_metadata_schema_known",
            ),
            models.CheckConstraint(
                condition=models.Q(execution_protocol_version__gte=1),
                name="ray_task_protocol_positive",
            ),
            models.UniqueConstraint(
                fields=["task_id"],
                name="ray_task_id_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(priority__gte=-100, priority__lte=100),
                name="ray_task_priority_valid_range",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    workflow_run_sequence__gte=0,
                    workflow_run_sequence__lte=_WORKFLOW_RUN_SEQUENCE_MAX,
                ),
                name="ray_task_wf_run_seq_cap",
            ),
            models.UniqueConstraint(
                fields=["workflow_run_namespace"],
                name="ray_task_wf_run_ns_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(workflow_run_namespace__isnull=True)
                    | models.Q(
                        workflow_run_namespace__gte=1,
                        workflow_run_namespace__lte=_WORKFLOW_RUN_NAMESPACE_MAX,
                    )
                ),
                name="ray_task_wf_run_ns_range",
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
    payload_kind = models.CharField(
        max_length=32,
        choices=InputPayloadKind.choices,
        default=InputPayloadKind.TASK_INPUT,
        db_default=InputPayloadKind.TASK_INPUT,
    )
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
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    payload_kind__in=(
                        InputPayloadKind.TASK_INPUT,
                        InputPayloadKind.RAY_JOB_REQUEST,
                    )
                ),
                name="ray_input_payload_kind_valid",
            )
        ]
        verbose_name = "Task Input Payload"
        verbose_name_plural = "Task Input Payloads"

    def __str__(self) -> str:
        return (
            f"{self.backend} {self.get_payload_kind_display()} "
            f"{str(self.digest)[:12]} ({self.state})"
        )


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
    execution_protocol_version = models.PositiveSmallIntegerField(
        default=EXECUTION_PROTOCOL_VERSION,
        db_default=EXECUTION_PROTOCOL_VERSION,
        editable=False,
        validators=[MinValueValidator(1)],
        help_text="Durable execution protocol archived for this attempt",
    )
    managed_with_django_ray_version = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        editable=False,
        help_text="Diagnostic django-ray package version that managed this attempt",
    )
    executor_django_ray_version = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        editable=False,
        help_text="Diagnostic django-ray package version that executed this attempt",
    )
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
            models.CheckConstraint(
                condition=models.Q(execution_protocol_version__gte=1),
                name="ray_attempt_protocol_positive",
            ),
            models.UniqueConstraint(
                fields=["execution", "attempt_number"],
                name="ray_task_attempt_unique_number",
            ),
        ]
        indexes = [models.Index(fields=["execution", "attempt_number"])]

    def __str__(self) -> str:
        return f"{self.execution_id} attempt {self.attempt_number} ({self.state})"


class LegacyWorkerAdmissionToken(models.Model):
    """Database token required for an unaware pre-capability worker lease."""

    singleton_key = models.PositiveSmallIntegerField(
        primary_key=True,
        default=1,
        db_default=1,
        editable=False,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(singleton_key=1),
                name="ray_legacy_admission_singleton",
            )
        ]
        verbose_name = "Legacy Worker Admission Token"
        verbose_name_plural = "Legacy Worker Admission Tokens"

    def __str__(self) -> str:
        return "Legacy worker admission is available"


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
    capability_schema_version = models.PositiveSmallIntegerField(
        default=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
        db_default=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
        editable=False,
        help_text="Worker capability advertisement schema; zero identifies a legacy lease",
    )
    django_ray_version = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        editable=False,
        help_text="Diagnostic django-ray package version advertised by this worker",
    )
    min_supported_execution_protocol_version = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        editable=False,
        validators=[MinValueValidator(1)],
        help_text="Lowest durable execution protocol explicitly supported by this worker",
    )
    max_supported_execution_protocol_version = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        editable=False,
        validators=[MinValueValidator(1)],
        help_text="Highest durable execution protocol explicitly supported by this worker",
    )
    legacy_admission_token = models.ForeignKey(
        LegacyWorkerAdmissionToken,
        on_delete=models.PROTECT,
        related_name="worker_leases",
        null=True,
        blank=True,
        default=1,
        db_default=1,
        editable=False,
        help_text="Admission token held only by a legacy capability-unaware worker lease",
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
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        capability_schema_version=LEGACY_WORKER_CAPABILITY_SCHEMA_VERSION,
                        django_ray_version__isnull=True,
                        min_supported_execution_protocol_version__isnull=True,
                        max_supported_execution_protocol_version__isnull=True,
                    )
                    & (models.Q(is_active=False) | models.Q(legacy_admission_token__isnull=False))
                    | models.Q(
                        capability_schema_version=WORKER_CAPABILITY_SCHEMA_VERSION,
                        legacy_admission_token__isnull=True,
                        min_supported_execution_protocol_version__isnull=False,
                        max_supported_execution_protocol_version__isnull=False,
                        min_supported_execution_protocol_version__gte=1,
                        max_supported_execution_protocol_version__gte=models.F(
                            "min_supported_execution_protocol_version"
                        ),
                    )
                ),
                name="ray_worker_capability_valid",
            )
        ]
        indexes = [
            models.Index(
                fields=[
                    "is_active",
                    "capability_schema_version",
                    "min_supported_execution_protocol_version",
                    "max_supported_execution_protocol_version",
                ],
                name="ray_worker_protocol_idx",
            )
        ]
        verbose_name = "Task Worker Lease"
        verbose_name_plural = "Task Worker Leases"

    def __str__(self) -> str:
        status = "active" if self.is_active else "inactive"
        worker_id = str(self.worker_id)
        return f"Worker {worker_id[:8]}... on {self.hostname} ({status})"


class TaskExecutionProtocolPolicy(models.Model):
    """Singleton rollout policy for the durable execution protocol.

    The first schema migration seeds protocol v1 with legacy worker admission
    open.  Mutation and activation are intentionally reserved for the later
    fenced rollout service rather than ordinary model or Admin writes.
    """

    singleton_key = models.PositiveSmallIntegerField(
        primary_key=True,
        default=1,
        db_default=1,
        editable=False,
    )
    schema_version = models.PositiveSmallIntegerField(
        default=PROTOCOL_POLICY_SCHEMA_VERSION,
        db_default=PROTOCOL_POLICY_SCHEMA_VERSION,
        editable=False,
        validators=[MinValueValidator(1)],
    )
    active_write_protocol_version = models.PositiveSmallIntegerField(
        default=EXECUTION_PROTOCOL_VERSION,
        db_default=EXECUTION_PROTOCOL_VERSION,
        editable=False,
        validators=[MinValueValidator(1)],
    )
    legacy_worker_admission_enabled = models.BooleanField(
        default=True,
        db_default=True,
        editable=False,
    )
    revision = models.PositiveBigIntegerField(
        default=1,
        db_default=1,
        editable=False,
        validators=[MinValueValidator(1)],
    )
    updated_at = models.DateTimeField(
        default=timezone.now,
        editable=False,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(singleton_key=1),
                name="ray_protocol_policy_singleton",
            ),
            models.CheckConstraint(
                condition=models.Q(schema_version=PROTOCOL_POLICY_SCHEMA_VERSION),
                name="ray_protocol_policy_schema",
            ),
            models.CheckConstraint(
                condition=models.Q(active_write_protocol_version__gte=1),
                name="ray_protocol_policy_active",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name="ray_protocol_policy_revision",
            ),
        ]
        verbose_name = "Task Execution Protocol Policy"
        verbose_name_plural = "Task Execution Protocol Policies"

    def __str__(self) -> str:
        legacy = "open" if self.legacy_worker_admission_enabled else "closed"
        return (
            f"Execution protocol v{self.active_write_protocol_version} "
            f"(legacy admission {legacy}, revision {self.revision})"
        )


class RayTarget(models.Model):
    """Immutable identity and exact runtime tuple for one dormant Ray target."""

    target_key = models.CharField(
        primary_key=True,
        max_length=128,
        editable=False,
        validators=[_RAY_TARGET_KEY_VALIDATOR],
        help_text="Stable operator key for this Ray target",
    )
    runner_family = models.CharField(
        max_length=16,
        choices=[(family.value, family.value) for family in RayRunnerFamily],
        editable=False,
        help_text="Ray submission family bound to this target",
    )
    cluster_session = models.CharField(
        max_length=256,
        editable=False,
        validators=[_RAY_CLUSTER_SESSION_VALIDATOR],
        help_text="Public Ray session name identifying the exact cluster instance",
    )
    ray_major = models.PositiveBigIntegerField(
        editable=False,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER),
        ],
    )
    ray_minor = models.PositiveBigIntegerField(
        editable=False,
        validators=[MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER)],
    )
    ray_patch = models.PositiveBigIntegerField(
        editable=False,
        validators=[MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER)],
    )
    python_implementation = models.CharField(
        max_length=64,
        editable=False,
        validators=[_PYTHON_IMPLEMENTATION_VALIDATOR],
        help_text="Normalized Python implementation identifier",
    )
    python_major = models.PositiveBigIntegerField(
        editable=False,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER),
        ],
    )
    python_minor = models.PositiveBigIntegerField(
        editable=False,
        validators=[MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER)],
    )
    python_patch = models.PositiveBigIntegerField(
        editable=False,
        validators=[MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER)],
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    runner_family__in=tuple(family.value for family in RayRunnerFamily)
                ),
                name="ray_target_runner_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    ray_major__gte=1,
                    ray_major__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                    ray_minor__gte=0,
                    ray_minor__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                    ray_patch__gte=0,
                    ray_patch__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                    python_major__gte=1,
                    python_major__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                    python_minor__gte=0,
                    python_minor__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                    python_patch__gte=0,
                    python_patch__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                ),
                name="ray_target_runtime_valid",
            ),
            models.UniqueConstraint(
                fields=("runner_family", "cluster_session"),
                name="ray_target_instance_uniq",
            ),
        ]
        verbose_name = "Ray Target"
        verbose_name_plural = "Ray Targets"

    def __str__(self) -> str:
        return str(self.target_key)


class RayTargetPolicyRevision(models.Model):
    """One immutable operator policy revision for a dormant Ray target."""

    target = models.ForeignKey(
        RayTarget,
        on_delete=models.PROTECT,
        related_name="policy_revisions",
        editable=False,
    )
    revision = models.PositiveBigIntegerField(
        editable=False,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER),
        ],
    )
    desired_state = models.CharField(
        max_length=16,
        choices=RayTargetDesiredState.choices,
        editable=False,
    )
    expectation_schema_version = models.PositiveSmallIntegerField(
        editable=False,
        validators=[MinValueValidator(1)],
    )
    expectation_json = models.TextField(
        editable=False,
        validators=[MaxLengthValidator(RAY_TARGET_EXPECTATION_MAX_BYTES)],
        help_text="Canonical Ray target expectation JSON",
    )
    expectation_digest = models.CharField(
        max_length=71,
        editable=False,
        validators=[_TAGGED_SHA256_HEX_VALIDATOR],
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    expectation_schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION
                ),
                name="ray_tpolicy_schema_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(desired_state__in=RayTargetDesiredState.values),
                name="ray_tpolicy_state_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    revision__gte=1,
                    revision__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                ),
                name="ray_tpolicy_revision_valid",
            ),
            models.UniqueConstraint(
                fields=("target", "revision"),
                name="ray_tpolicy_target_rev_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("target", "-revision"),
                name="ray_tpolicy_latest_idx",
            )
        ]
        verbose_name = "Ray Target Policy Revision"
        verbose_name_plural = "Ray Target Policy Revisions"

    def __str__(self) -> str:
        return f"{self.target_id} policy revision {self.revision}"


class RayTargetAttestationRevision(models.Model):
    """One immutable bounded attestation revision for a target policy."""

    policy = models.ForeignKey(
        RayTargetPolicyRevision,
        on_delete=models.PROTECT,
        related_name="attestation_revisions",
        editable=False,
    )
    revision = models.PositiveBigIntegerField(
        editable=False,
        validators=[
            MinValueValidator(1),
            MaxValueValidator(RAY_TARGET_ATTESTATION_MAX_COUNTER),
        ],
    )
    attestation_schema_version = models.PositiveSmallIntegerField(
        editable=False,
        validators=[MinValueValidator(1)],
    )
    attestation_json = models.TextField(
        editable=False,
        validators=[MaxLengthValidator(RAY_CLUSTER_ATTESTATION_MAX_BYTES)],
        help_text="Canonical Ray cluster attestation JSON",
    )
    expectation_digest = models.CharField(
        max_length=71,
        editable=False,
        validators=[_TAGGED_SHA256_HEX_VALIDATOR],
    )
    membership_digest = models.CharField(
        max_length=71,
        editable=False,
        validators=[_TAGGED_SHA256_HEX_VALIDATOR],
    )
    attestation_digest = models.CharField(
        max_length=71,
        editable=False,
        validators=[_TAGGED_SHA256_HEX_VALIDATOR],
    )
    observed_at = models.DateTimeField(editable=False)
    expires_at = models.DateTimeField(editable=False)
    recorded_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    attestation_schema_version=RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION
                ),
                name="ray_tattest_schema_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    revision__gte=1,
                    revision__lte=RAY_TARGET_ATTESTATION_MAX_COUNTER,
                ),
                name="ray_tattest_revision_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(expires_at__gt=models.F("observed_at"))
                    & models.Q(
                        expires_at__lte=models.F("observed_at")
                        + timedelta(seconds=RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS)
                    )
                    & models.Q(recorded_at__gte=models.F("observed_at"))
                    & models.Q(recorded_at__lt=models.F("expires_at"))
                ),
                name="ray_tattest_window_valid",
            ),
            models.UniqueConstraint(
                fields=("policy", "revision"),
                name="ray_tattest_policy_rev_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("policy", "-revision"),
                name="ray_tattest_latest_idx",
            ),
            models.Index(
                fields=("expires_at",),
                name="ray_tattest_expiry_idx",
            ),
        ]
        verbose_name = "Ray Target Attestation Revision"
        verbose_name_plural = "Ray Target Attestation Revisions"

    def __str__(self) -> str:
        return f"policy {self.policy_id} attestation revision {self.revision}"
