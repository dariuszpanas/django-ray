"""Django app configuration for django-ray."""

import os
import sys

from django.apps import AppConfig


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

        # Allow migration/static collection flows before full runtime config is available.
        skip_commands = {
            "migrate",
            "makemigrations",
            "showmigrations",
            "collectstatic",
        }
        if len(sys.argv) >= 2 and sys.argv[1] in skip_commands:
            return True

        return False

    def ready(self) -> None:
        """Initialize the app when Django starts."""
        from django.core.exceptions import ImproperlyConfigured

        from django_ray.conf.settings import validate_settings

        try:
            validate_settings()
        except ImproperlyConfigured:
            if self._should_skip_validation():
                return
            raise
