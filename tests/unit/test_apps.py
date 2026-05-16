"""Unit tests for Django app startup validation policy."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

import django_ray as django_ray_module
from django_ray.apps import DjangoRayConfig


def _make_config() -> DjangoRayConfig:
    return DjangoRayConfig("django_ray", django_ray_module)


def _raise(exception: Exception) -> None:
    raise exception


class TestStartupValidationPolicy:
    """Tests for startup validation strictness and skip conditions."""

    def test_ready_fails_fast_for_invalid_config_on_normal_startup(self, monkeypatch) -> None:
        monkeypatch.delenv("DJANGO_RAY_SKIP_VALIDATION", raising=False)
        monkeypatch.setattr("django_ray.apps.sys.argv", ["manage.py", "runserver"])
        monkeypatch.setattr(
            "django_ray.conf.settings.validate_settings",
            lambda: _raise(ImproperlyConfigured("invalid config")),
        )

        with pytest.raises(ImproperlyConfigured, match="invalid config"):
            _make_config().ready()

    def test_ready_skips_invalid_config_during_migrations(self, monkeypatch) -> None:
        monkeypatch.delenv("DJANGO_RAY_SKIP_VALIDATION", raising=False)
        monkeypatch.setattr("django_ray.apps.sys.argv", ["manage.py", "migrate"])
        monkeypatch.setattr(
            "django_ray.conf.settings.validate_settings",
            lambda: _raise(ImproperlyConfigured("invalid config")),
        )

        _make_config().ready()

    def test_ready_skips_invalid_config_with_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("DJANGO_RAY_SKIP_VALIDATION", "true")
        monkeypatch.setattr("django_ray.apps.sys.argv", ["manage.py", "runserver"])
        monkeypatch.setattr(
            "django_ray.conf.settings.validate_settings",
            lambda: _raise(ImproperlyConfigured("invalid config")),
        )

        _make_config().ready()

    def test_ready_does_not_swallow_non_config_errors(self, monkeypatch) -> None:
        monkeypatch.setenv("DJANGO_RAY_SKIP_VALIDATION", "true")
        monkeypatch.setattr("django_ray.apps.sys.argv", ["manage.py", "migrate"])
        monkeypatch.setattr(
            "django_ray.conf.settings.validate_settings",
            lambda: _raise(RuntimeError("boom")),
        )

        with pytest.raises(RuntimeError, match="boom"):
            _make_config().ready()
