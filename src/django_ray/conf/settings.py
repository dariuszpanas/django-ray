"""Settings management for django-ray."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage
from django.core.exceptions import ImproperlyConfigured

from django_ray.conf.defaults import DEFAULTS
from django_ray.conf.validation import (
    _runtime_env_has_env_vars,
)
from django_ray.conf.validation import (
    check_django_ray_settings as _check_django_ray_settings,
)
from django_ray.conf.validation import (
    register_system_checks as _register_system_checks,
)
from django_ray.conf.validation import (
    validate_ray_job_request_storage_settings as _validate_ray_job_request_storage_settings,
)
from django_ray.conf.validation import (
    validate_settings as _validate_settings,
)


def get_settings() -> dict[str, Any]:
    """Get merged django-ray settings.

    Returns settings from Django's DJANGO_RAY setting merged with defaults.

    Returns:
        Dictionary of settings.

    Raises:
        ImproperlyConfigured: If DJANGO_RAY is configured as a non-mapping.
    """
    user_settings = getattr(settings, "DJANGO_RAY", {})
    if not isinstance(user_settings, Mapping):
        raise ImproperlyConfigured(
            f"django-ray: DJANGO_RAY setting must be a mapping, got {type(user_settings).__name__}"
        )
    merged = {**DEFAULTS, **user_settings}
    return merged


def validate_ray_job_request_storage_settings(
    config: Mapping[str, Any] | None = None,
) -> None:
    """Require a retrievable request store before a Ray Job worker advertises capacity."""
    _validate_ray_job_request_storage_settings(
        config,
        _get_settings=get_settings,
        _validate=validate_settings,
    )


def validate_settings(
    config: Mapping[str, Any] | None = None,
    *,
    require_ray_address: bool = False,
) -> None:
    """Validate django-ray settings."""
    _validate_settings(
        config,
        require_ray_address=require_ray_address,
        _get_settings=get_settings,
    )


def check_django_ray_settings(
    app_configs: Sequence[Any] | None = None,
    **kwargs: Any,
) -> list[CheckMessage]:
    """Django system checks for django-ray configuration safety and defaults."""
    return _check_django_ray_settings(
        app_configs,
        _get_settings=get_settings,
        _validate=validate_settings,
        **kwargs,
    )


def register_system_checks() -> None:
    """Register django-ray configuration checks with Django's system check framework."""
    _register_system_checks(check_django_ray_settings)


__all__ = [
    "_runtime_env_has_env_vars",
    "check_django_ray_settings",
    "get_settings",
    "register_system_checks",
    "validate_ray_job_request_storage_settings",
    "validate_settings",
]
