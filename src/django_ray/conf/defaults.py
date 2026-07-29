"""Default configuration values for django-ray."""

from __future__ import annotations

from typing import Any

# Runtime-selectable policies. The durable summary protocol also reserves
# sampled and terminal-only values for later producer implementations.
WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES = frozenset({"full", "disabled"})
TASK_ATTEMPT_ADMIN_MODES = frozenset({"inline", "standalone", "both"})

DEFAULTS: dict[str, Any] = {
    # Ray connection
    "RAY_ADDRESS": None,  # Required - e.g., "ray://localhost:10001"
    "RAY_RUNTIME_ENV": {},
    "RAY_STATE_API_ADDRESS": None,
    "RAY_STATE_API_TIMEOUT_SECONDS": 5,
    "RUNTIME_ENV_PROFILES": {},
    "DEFAULT_RUNTIME_ENV_PROFILE": None,
    # Optional, non-secret deployment identities used by workflow plan snapshots.
    # The code revision should be an immutable build, artifact, or image revision.
    "WORKFLOW_PLAN_CODE_REVISION": None,
    "WORKFLOW_PLAN_TRUST_IDENTITY": {},
    # Runner configuration
    "RUNNER": "ray_job",  # "ray_job" or "ray_core"
    # Concurrency
    "DEFAULT_CONCURRENCY": 10,
    # Worker polling
    "WORKER_POLL_INTERVAL_SECONDS": 0.1,
    "WORKER_POLL_MAX_INTERVAL_SECONDS": 0.1,
    # Retry configuration
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
    "RETRY_EXCEPTION_DENYLIST": [],
    # Reliability
    "STUCK_TASK_TIMEOUT_SECONDS": 300,
    "WORKER_LEASE_SECONDS": 60,
    "WORKER_HEARTBEAT_SECONDS": 15,
    "TASK_MONITOR_HEARTBEAT_SECONDS": 15,
    "WORKFLOW_PROGRESS_REPORTING_POLICY": "full",
    "WORKFLOW_PROGRESS_FLUSH_SECONDS": 1,
    "WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS": 7,
    # Admin presentation
    "TASK_ATTEMPT_ADMIN_MODE": "inline",
    # Inputs
    # ``None`` preserves legacy inline args/kwargs storage. Configure a
    # retrievable input backend before setting a spill threshold.
    "MAX_INLINE_INPUT_SIZE_BYTES": None,
    "INPUT_STORAGE_BACKEND": None,  # "filesystem", "s3", or "gcs"
    "INPUT_STORAGE_FILESYSTEM_PATH": None,
    "INPUT_STORAGE_S3_BUCKET": None,
    "INPUT_STORAGE_S3_PREFIX": "django-ray/inputs",
    "INPUT_STORAGE_S3_REGION": None,
    "INPUT_STORAGE_S3_ENDPOINT_URL": None,
    "INPUT_STORAGE_GCS_BUCKET": None,
    "INPUT_STORAGE_GCS_PREFIX": "django-ray/inputs",
    # Results
    "MAX_RESULT_SIZE_BYTES": 1024 * 1024,  # 1MB
    "RESULT_STORAGE_BACKEND": "digest",  # "digest", "filesystem", "s3", "gcs"
    "RESULT_STORAGE_FILESYSTEM_PATH": None,
    "RESULT_STORAGE_S3_BUCKET": None,
    "RESULT_STORAGE_S3_PREFIX": "django-ray/results",
    "RESULT_STORAGE_S3_REGION": None,
    "RESULT_STORAGE_S3_ENDPOINT_URL": None,
    "RESULT_STORAGE_GCS_BUCKET": None,
    "RESULT_STORAGE_GCS_PREFIX": "django-ray/results",
    # Redaction
    # Regex patterns applied to log messages, structured fields, and
    # operator-facing task data.  ``None`` selects the built-in safe defaults.
    "REDACT_PATTERNS": None,
}
