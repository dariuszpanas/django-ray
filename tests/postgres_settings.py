"""Reusable Django settings for the PostgreSQL coordination test gate."""

from __future__ import annotations

import os

from testproject.settings import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DATABASE_NAME", "django_ray"),
        "USER": os.environ.get("DATABASE_USER", "django_ray"),
        "PASSWORD": os.environ.get("DATABASE_PASSWORD", "django_ray"),
        "HOST": os.environ.get("DATABASE_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DATABASE_PORT", "5432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 10},
        "TEST": {
            "NAME": os.environ.get("DATABASE_TEST_NAME", "test_django_ray"),
        },
    }
}

# API tests are not part of this gate, but keeping the sample token deterministic
# makes these settings safe to reuse for broader local PostgreSQL test runs.
DJANGO_API_TOKEN = "test-api-token-for-postgresql-pytest"
