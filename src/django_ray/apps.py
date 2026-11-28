"""Django app configuration for django-ray."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from django.apps import AppConfig

_MANAGEMENT_OPTIONS_WITH_VALUE = frozenset(
    {
        "--settings",
        "--pythonpath",
        "-v",
        "--verbosity",
    }
)
_SKIP_VALIDATION_COMMANDS = frozenset(
    {
        "migrate",
        "makemigrations",
        "showmigrations",
        "collectstatic",
        "check",
    }
)


def _extract_manage_command(argv: Sequence[str]) -> str | None:
    """Return the first positional Django management subcommand from argv."""
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token in _MANAGEMENT_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


class DjangoRayConfig(AppConfig):
    """Configuration for django-ray Django app."""

    name = "django_ray"
    verbose_name = "Django Ray"
    default_auto_field = "django.db.models.BigAutoField"

    @staticmethod
    def _should_skip_validation() -> bool:
        """Return True when startup validation should be skipped."""
        # Explicit override for maintenance/bootstrap contexts.
        skip_env = os.environ.get("DJANGO_RAY_SKIP_VALIDATION", "").strip().lower()
        if skip_env in {"1", "true", "yes"}:
            return True

        # Allow migration, static collection, and system check flows before full
        # runtime configuration is available, even when options precede the command.
        command = _extract_manage_command(sys.argv)
        if command in _SKIP_VALIDATION_COMMANDS:
            return True

        return False

    def ready(self) -> None:
        """Initialize the app when Django starts."""
        from django.core.exceptions import ImproperlyConfigured

        from django_ray.conf.settings import register_system_checks, validate_settings

        register_system_checks()

        try:
            validate_settings()
        except ImproperlyConfigured:
            if self._should_skip_validation():
                return
            raise
