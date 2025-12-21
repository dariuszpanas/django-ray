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

    # Validate runner choice
    valid_runners = ("ray_job", "ray_core")
    runner = config.get("RUNNER", "ray_job")
    if runner not in valid_runners:
        raise ImproperlyConfigured(
            f"django-ray: RUNNER must be one of {valid_runners}, got '{runner}'"
        )

    # Validate numeric settings
    numeric_settings = [
        ("DEFAULT_CONCURRENCY", 1, 1000),
        ("MAX_TASK_ATTEMPTS", 1, 100),
        ("STUCK_TASK_TIMEOUT_SECONDS", 30, 86400),
        ("MAX_RESULT_SIZE_BYTES", 1024, 100 * 1024 * 1024),
    ]

    for name, min_val, max_val in numeric_settings:
        value = config.get(name)
        if value is not None:
            if not isinstance(value, int) or value < min_val or value > max_val:
                raise ImproperlyConfigured(
                    f"django-ray: {name} must be an integer between {min_val} and {max_val}"
                )
