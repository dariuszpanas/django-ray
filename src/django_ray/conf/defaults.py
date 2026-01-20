"""Default configuration values for django-ray."""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    # Ray connection
    "RAY_ADDRESS": None,  # Required - e.g., "ray://localhost:10001"
    "RAY_RUNTIME_ENV": {},
    # Runner configuration
    "RUNNER": "ray_job",  # "ray_job" or "ray_core"
    # Concurrency
    "DEFAULT_CONCURRENCY": 10,
    # Retry configuration
    "MAX_TASK_ATTEMPTS": 3,
    "RETRY_BACKOFF_SECONDS": 60,
    "RETRY_EXCEPTION_DENYLIST": [],
    # Reliability
    "STUCK_TASK_TIMEOUT_SECONDS": 300,
    "WORKER_LEASE_SECONDS": 60,
    "WORKER_HEARTBEAT_SECONDS": 15,
    # Results
    "MAX_RESULT_SIZE_BYTES": 1024 * 1024,  # 1MB
    # Redaction
    "REDACT_PATTERNS": None,  # Uses defaults if None
}
