"""Settings management for django-ray."""

from __future__ import annotations

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
    if not config.get("RAY_ADDRESS"):
        raise ImproperlyConfigured(
            "django-ray: RAY_ADDRESS is required in DJANGO_RAY settings. "
            "Example: DJANGO_RAY = {'RAY_ADDRESS': 'ray://localhost:10001'}"
        )

    state_api_address = config.get("RAY_STATE_API_ADDRESS")
    if state_api_address is not None and not isinstance(state_api_address, str):
        raise ImproperlyConfigured("django-ray: RAY_STATE_API_ADDRESS must be a string or None")

    # Validate runner choice
    valid_runners = ("ray_job", "ray_core")
    runner = config.get("RUNNER", "ray_job")
    if runner not in valid_runners:
        raise ImproperlyConfigured(
            f"django-ray: RUNNER must be one of {valid_runners}, got '{runner}'"
        )

    from django_ray.runtime.runtime_env import validate_runtime_env_profiles

    validate_runtime_env_profiles(config)

    # Validate numeric settings
    numeric_settings = [
        ("DEFAULT_CONCURRENCY", 1, 1000),
        ("MAX_TASK_ATTEMPTS", 1, 100),
        ("STUCK_TASK_TIMEOUT_SECONDS", 30, 86400),
        ("TASK_MONITOR_HEARTBEAT_SECONDS", 1, 300),
        ("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1, 300),
        ("RAY_STATE_API_TIMEOUT_SECONDS", 1, 60),
        ("MAX_RESULT_SIZE_BYTES", 1024, 100 * 1024 * 1024),
    ]

    for name, min_val, max_val in numeric_settings:
        value = config.get(name)
        if value is not None:
            if not isinstance(value, int) or value < min_val or value > max_val:
                raise ImproperlyConfigured(
                    f"django-ray: {name} must be an integer between {min_val} and {max_val}"
                )

    result_storage_backend = config.get("RESULT_STORAGE_BACKEND", "digest")
    valid_result_storage_backends = ("digest", "filesystem", "s3", "gcs")
    if result_storage_backend not in valid_result_storage_backends:
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_BACKEND must be one of "
            f"{valid_result_storage_backends}, got '{result_storage_backend}'"
        )

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
