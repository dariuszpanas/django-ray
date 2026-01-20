"""Configuration module for django-ray."""

from django_ray.conf.defaults import DEFAULTS
from django_ray.conf.settings import get_settings, validate_settings

__all__ = [
    "DEFAULTS",
    "get_settings",
    "validate_settings",
]
