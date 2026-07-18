"""Settings management for django-ray."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_ray.conf.defaults import DEFAULTS


def get_settings() -> dict[str, Any]:
    """Get merged django-ray settings.

    Returns settings from Django's DJANGO_RAY setting merged with defaults.

    Returns:
        Dictionary of settings.
    """
    user_settings = getattr(settings, "DJANGO_RAY", {})
    merged = {**DEFAULTS, **user_settings}
    return merged


def validate_settings(config: dict[str, Any] | None = None) -> None:
    """Validate django-ray settings.

    Args:
        config: Settings dict to validate. If None, uses get_settings().

    Raises:
        ImproperlyConfigured: If settings are invalid.
    """
    if config is None:
        config = get_settings()

    # Required settings
    ray_address = config.get("RAY_ADDRESS")
    if not isinstance(ray_address, str) or not ray_address.strip():
        raise ImproperlyConfigured(
            "django-ray: RAY_ADDRESS is required and must be a non-empty string in DJANGO_RAY settings. "
            "Example: DJANGO_RAY = {'RAY_ADDRESS': 'ray://localhost:10001'}"
        )

    state_api_address = config.get("RAY_STATE_API_ADDRESS")
    if state_api_address is not None and not isinstance(state_api_address, str):
        raise ImproperlyConfigured("django-ray: RAY_STATE_API_ADDRESS must be a string or None")

    # Validate runner choice
    valid_runners = ("ray_job", "ray_core")
    runner = config.get("RUNNER", "ray_job")
    if not isinstance(runner, str) or runner not in valid_runners:
        raise ImproperlyConfigured(
            f"django-ray: RUNNER must be one of {valid_runners}, got '{runner}'"
        )

    from django_ray.runtime.runtime_env import validate_runtime_env_profiles

    validate_runtime_env_profiles(config)

    # Validate numeric settings
    numeric_settings = [
        ("DEFAULT_CONCURRENCY", 1, 1000),
        ("MAX_TASK_ATTEMPTS", 1, 100),
        ("RETRY_BACKOFF_SECONDS", 0, 3600),
        ("STUCK_TASK_TIMEOUT_SECONDS", 30, 86400),
        ("WORKER_LEASE_SECONDS", 1, 86400),
        ("WORKER_HEARTBEAT_SECONDS", 1, 86400),
        ("TASK_MONITOR_HEARTBEAT_SECONDS", 1, 300),
        ("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1, 300),
        ("RAY_STATE_API_TIMEOUT_SECONDS", 1, 60),
        ("MAX_RESULT_SIZE_BYTES", 1024, 100 * 1024 * 1024),
    ]

    for name, min_val, max_val in numeric_settings:
        if name not in config:
            continue
        value = config[name]
        if type(value) is not int or value < min_val or value > max_val:
            raise ImproperlyConfigured(
                f"django-ray: {name} must be an integer between {min_val} and {max_val}"
            )

    # Heartbeats must occur before the lease or stuck-task windows expire. Use
    # defaults for omitted values so partial test/config dictionaries receive
    # the same relationship checks as the merged runtime settings.
    worker_lease_seconds = config.get("WORKER_LEASE_SECONDS", DEFAULTS["WORKER_LEASE_SECONDS"])
    worker_heartbeat_seconds = config.get(
        "WORKER_HEARTBEAT_SECONDS", DEFAULTS["WORKER_HEARTBEAT_SECONDS"]
    )
    if worker_heartbeat_seconds >= worker_lease_seconds:
        raise ImproperlyConfigured(
            "django-ray: WORKER_HEARTBEAT_SECONDS must be less than WORKER_LEASE_SECONDS"
        )

    stuck_timeout_seconds = config.get(
        "STUCK_TASK_TIMEOUT_SECONDS", DEFAULTS["STUCK_TASK_TIMEOUT_SECONDS"]
    )
    task_monitor_heartbeat_seconds = config.get(
        "TASK_MONITOR_HEARTBEAT_SECONDS", DEFAULTS["TASK_MONITOR_HEARTBEAT_SECONDS"]
    )
    if task_monitor_heartbeat_seconds >= stuck_timeout_seconds:
        raise ImproperlyConfigured(
            "django-ray: TASK_MONITOR_HEARTBEAT_SECONDS must be less than "
            "STUCK_TASK_TIMEOUT_SECONDS"
        )

    denylist = config.get("RETRY_EXCEPTION_DENYLIST", [])
    if not isinstance(denylist, list) or any(not isinstance(entry, str) for entry in denylist):
        raise ImproperlyConfigured("django-ray: RETRY_EXCEPTION_DENYLIST must be a list of strings")

    result_storage_backend = config.get("RESULT_STORAGE_BACKEND", "digest")
    valid_result_storage_backends = ("digest", "filesystem", "s3", "gcs")
    if (
        not isinstance(result_storage_backend, str)
        or result_storage_backend not in valid_result_storage_backends
    ):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_BACKEND must be one of "
            f"{valid_result_storage_backends}, got '{result_storage_backend}'"
        )

    optional_string_settings = (
        "RESULT_STORAGE_FILESYSTEM_PATH",
        "RESULT_STORAGE_S3_BUCKET",
        "RESULT_STORAGE_S3_PREFIX",
        "RESULT_STORAGE_S3_REGION",
        "RESULT_STORAGE_S3_ENDPOINT_URL",
        "RESULT_STORAGE_GCS_BUCKET",
        "RESULT_STORAGE_GCS_PREFIX",
    )
    for name in optional_string_settings:
        value = config.get(name)
        if value is not None and not isinstance(value, str):
            raise ImproperlyConfigured(f"django-ray: {name} must be a string or None")

    if result_storage_backend == "filesystem" and not config.get("RESULT_STORAGE_FILESYSTEM_PATH"):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_FILESYSTEM_PATH is required when "
            "RESULT_STORAGE_BACKEND='filesystem'"
        )
    if result_storage_backend == "s3" and not config.get("RESULT_STORAGE_S3_BUCKET"):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_S3_BUCKET is required when RESULT_STORAGE_BACKEND='s3'"
        )
    if result_storage_backend == "gcs" and not config.get("RESULT_STORAGE_GCS_BUCKET"):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_GCS_BUCKET is required when RESULT_STORAGE_BACKEND='gcs'"
        )

    redact_patterns = config.get("REDACT_PATTERNS")
    if redact_patterns is not None:
        if isinstance(redact_patterns, str):
            redact_patterns = (redact_patterns,)
        elif not isinstance(redact_patterns, Sequence) or isinstance(
            redact_patterns, bytes | bytearray
        ):
            raise ImproperlyConfigured(
                "django-ray: REDACT_PATTERNS must be a string or a sequence of regex strings"
            )
        for pattern in redact_patterns:
            if not isinstance(pattern, str) or not pattern:
                raise ImproperlyConfigured(
                    "django-ray: REDACT_PATTERNS entries must be non-empty strings"
                )
            try:
                re.compile(pattern)
            except re.error as error:
                raise ImproperlyConfigured(
                    f"django-ray: REDACT_PATTERNS contains invalid regex {pattern!r}: {error}"
                ) from error
