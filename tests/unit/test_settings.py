"""Unit tests for settings parsing."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.conf.settings import validate_settings


class TestValidateSettings:
    """Tests for validate_settings function."""

    def test_validate_missing_ray_address(self) -> None:
        """Test that missing RAY_ADDRESS raises error."""
        with pytest.raises(ImproperlyConfigured, match="RAY_ADDRESS"):
            validate_settings({"RAY_ADDRESS": None})

    def test_validate_valid_settings(self) -> None:
        """Test that valid settings pass validation."""
        settings = {
            "RAY_ADDRESS": "ray://localhost:10001",
        }
        # Should not raise
        validate_settings(settings)

    def test_validate_invalid_runner(self) -> None:
        """Test that invalid RUNNER raises error."""
        with pytest.raises(ImproperlyConfigured, match="RUNNER"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RUNNER": "invalid_runner",
                }
            )

    def test_validate_invalid_concurrency(self) -> None:
        """Test that invalid DEFAULT_CONCURRENCY raises error."""
        with pytest.raises(ImproperlyConfigured, match="DEFAULT_CONCURRENCY"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "DEFAULT_CONCURRENCY": 0,  # Too low
                }
            )

    def test_validate_invalid_max_attempts(self) -> None:
        """Test that invalid MAX_TASK_ATTEMPTS raises error."""
        with pytest.raises(ImproperlyConfigured, match="MAX_TASK_ATTEMPTS"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "MAX_TASK_ATTEMPTS": 0,  # Too low
                }
            )
