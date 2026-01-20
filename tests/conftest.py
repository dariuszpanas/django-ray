"""Pytest configuration and fixtures for django-ray."""

from __future__ import annotations

import sys
from pathlib import Path

import django
from django.conf import settings

# Add testproject to path so it can be imported
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def pytest_configure(config: object) -> None:
    """Configure Django for testing."""
    if not settings.configured:
        settings.configure(
            DEBUG=True,
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.admin",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django_ray",
                "testproject",
            ],
            ROOT_URLCONF="testproject.urls",
            DJANGO_RAY={
                "RAY_ADDRESS": "ray://localhost:10001",
            },
            # Django 6 Tasks configuration
            TASKS={
                "default": {
                    "BACKEND": "django_ray.backends.RayTaskBackend",
                    "QUEUES": [
                        "default",
                        "high-priority",
                        "low-priority",
                        "sync",
                        "ml",
                    ],
                    "OPTIONS": {
                        "RAY_ADDRESS": "auto",
                    },
                },
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        )

    django.setup()
