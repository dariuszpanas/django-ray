"""Default configuration values for django-ray."""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    # Ray connection
    "RAY_ADDRESS": None,  # Required - e.g., "ray://localhost:10001"
    "RAY_RUNTIME_ENV": {},
    "RAY_STATE_API_ADDRESS": None,
    "RAY_STATE_API_TIMEOUT_SECONDS": 5,
    "RUNTIME_ENV_PROFILES": {},
    "DEFAULT_RUNTIME_ENV_PROFILE": None,
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
    "WORKFLOW_PROGRESS_FLUSH_SECONDS": 1,
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
