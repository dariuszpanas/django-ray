"""Django app configuration for django-ray."""

from django.apps import AppConfig


class DjangoRayConfig(AppConfig):
    """Configuration for django-ray Django app."""

    name = "django_ray"
    verbose_name = "Django Ray"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Initialize the app when Django starts."""
        from django.core.exceptions import ImproperlyConfigured

        from django_ray.conf.settings import validate_settings

        try:
            validate_settings()
        except ImproperlyConfigured:
            # Allow startup without RAY_ADDRESS for migrations, etc.
            pass
