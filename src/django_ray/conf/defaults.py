"""Default configuration values for django-ray."""

from __future__ import annotations

from typing import Any

# Runtime-selectable policies. The durable summary protocol also reserves
# sampled reporting for a later producer implementation.
WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES = frozenset({"full", "terminal_only", "disabled"})
TASK_ATTEMPT_ADMIN_MODES = frozenset({"inline", "standalone", "both"})
QUEUE_TIMEOUT_SECONDS_MAX = 2_147_483_647

DEFAULTS: dict[str, Any] = {
    # Ray connection
    # "auto" attaches to a running Ray cluster or starts local Ray in Ray Core
    # mode. Set an explicit Ray Jobs HTTP(S) URL (e.g., "http://ray-head:8265")
    # or Ray Client URL for remote clusters.
    "RAY_ADDRESS": "auto",
    "RAY_RUNTIME_ENV": {},
    "RAY_STATE_API_ADDRESS": None,
    "RAY_STATE_API_TIMEOUT_SECONDS": 5,
    "RUNTIME_ENV_PROFILES": {},
    "DEFAULT_RUNTIME_ENV_PROFILE": None,
    # RuntimeEnv snapshots remain plaintext by default for rolling-upgrade
    # compatibility. Encrypted writes are an explicit deployment choice, while
    # both modes retain dual-read support for existing snapshots.
    "RUNTIME_ENV_STORAGE_MODE": "plaintext",
    "RUNTIME_ENV_ENCRYPTION_KEYS": {},
    "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": None,
    "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
    # Optional, non-secret deployment identities used by workflow plan snapshots.
    # The code revision should be an immutable build, artifact, or image revision.
    "WORKFLOW_PLAN_CODE_REVISION": None,
    "WORKFLOW_PLAN_TRUST_IDENTITY": {},
    # Runner configuration
    "RUNNER": "ray_core",  # "ray_core" or "ray_job"
    # Concurrency
    "DEFAULT_CONCURRENCY": 10,
    # Queued work expires 24 hours after it first becomes eligible. ``None``
    # is an explicit unlimited-backlog policy.
    "QUEUE_TIMEOUT_SECONDS": 24 * 60 * 60,
    # Worker polling
    "WORKER_POLL_INTERVAL_SECONDS": 0.1,
    "WORKER_POLL_MAX_INTERVAL_SECONDS": 1.0,
    "WORKER_CANCELLATION_INTERVAL_SECONDS": 5.0,
    # Security allowlists (None uses safe runtime defaults / explicit backend targets)
    "ALLOWED_CALLABLE_MODULES": None,
    "ALLOWED_RAY_ADDRESSES": None,
    "REQUIRE_STRICT_NESTED_REQUEST": False,
    # Observability & tracing
    "METRICS_CACHE_TTL_SECONDS": 5.0,
    "TRACING_ENABLED": False,
    "TRACING_SAMPLE_RATE": 1.0,
    "TRACING_MAX_WORKFLOW_NODE_SPANS": 100,
    # Retry configuration
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
    "RETRY_BACKOFF_JITTER_RATIO": 0.1,
    "RETRY_EXCEPTION_DENYLIST": [],
    # Reliability
    "STUCK_TASK_TIMEOUT_SECONDS": 300,
    "WORKER_LEASE_SECONDS": 60,
    "WORKER_HEARTBEAT_SECONDS": 15,
    "TASK_MONITOR_HEARTBEAT_SECONDS": 15,
    "WORKFLOW_PROGRESS_REPORTING_POLICY": "full",
    "WORKFLOW_PROGRESS_FLUSH_SECONDS": 1,
    "WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS": 15,
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
    # Startup-validated bounded pattern expressions applied to log messages,
    # structured fields, and ordinary operator-facing task data. ``None``
    # keeps only the built-in patterns, which are always active.
    "REDACT_PATTERNS": None,
}
