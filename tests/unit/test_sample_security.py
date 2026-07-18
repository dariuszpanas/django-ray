"""Tests for the bundled sample project's production security contract."""

from __future__ import annotations

import os
import subprocess
import sys


def _import_settings(**environment: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({key: value for key, value in environment.items() if value is not None})
    for key, value in environment.items():
        if value is None:
            env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-c", "import testproject.settings"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_production_settings_reject_missing_secret_and_api_token() -> None:
    result = _import_settings(
        DJANGO_DEPLOYMENT_MODE="production",
        DJANGO_SECRET_KEY=None,
        DJANGO_API_TOKEN=None,
        DJANGO_DEBUG="False",
        DJANGO_ALLOWED_HOSTS="app.example.com",
    )

    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_production_settings_reject_debug_and_wildcard_hosts() -> None:
    result = _import_settings(
        DJANGO_DEPLOYMENT_MODE="production",
        DJANGO_SECRET_KEY="abcDEF123!@#xyz9876543210_random_value_for_production_1234567890",
        DJANGO_API_TOKEN="tT9!random-api-token-1234567890-abcdefgh",
        DJANGO_DEBUG="True",
        DJANGO_ALLOWED_HOSTS="*",
    )

    assert result.returncode != 0
    assert "DJANGO_DEBUG" in result.stderr


def test_production_settings_reject_obvious_credential_placeholders() -> None:
    result = _import_settings(
        DJANGO_DEPLOYMENT_MODE="production",
        DJANGO_SECRET_KEY="placeholder-secret-value-with-more-than-50-characters-1234567890",
        DJANGO_API_TOKEN="example-api-token-value-with-more-than-32-characters",
        DJANGO_DEBUG="False",
        DJANGO_ALLOWED_HOSTS="app.example.com",
    )

    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_production_settings_accept_explicit_secure_values() -> None:
    result = _import_settings(
        DJANGO_DEPLOYMENT_MODE="production",
        DJANGO_SECRET_KEY="abcDEF123!@#xyz9876543210_random_value_for_production_1234567890",
        DJANGO_API_TOKEN="tT9!random-api-token-1234567890-abcdefgh",
        DJANGO_DEBUG="False",
        DJANGO_ALLOWED_HOSTS="app.example.com,api.example.com",
    )

    assert result.returncode == 0, result.stderr
