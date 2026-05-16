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

    def test_validate_invalid_result_storage_backend(self) -> None:
        """Test that unknown RESULT_STORAGE_BACKEND raises error."""
        with pytest.raises(ImproperlyConfigured, match="RESULT_STORAGE_BACKEND"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RESULT_STORAGE_BACKEND": "unknown",
                }
            )

    def test_validate_filesystem_storage_requires_path(self) -> None:
        """Filesystem result storage requires a configured filesystem path."""
        with pytest.raises(ImproperlyConfigured, match="RESULT_STORAGE_FILESYSTEM_PATH"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RESULT_STORAGE_BACKEND": "filesystem",
                }
            )

    def test_validate_filesystem_storage_with_path(self) -> None:
        """Filesystem result storage passes validation when path is set."""
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "RESULT_STORAGE_BACKEND": "filesystem",
                "RESULT_STORAGE_FILESYSTEM_PATH": "/tmp/django-ray-results",
            }
        )

    def test_validate_s3_storage_requires_bucket(self) -> None:
        """S3 result storage requires a configured bucket."""
        with pytest.raises(ImproperlyConfigured, match="RESULT_STORAGE_S3_BUCKET"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RESULT_STORAGE_BACKEND": "s3",
                }
            )

    def test_validate_s3_storage_with_bucket(self) -> None:
        """S3 result storage passes validation when bucket is set."""
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "RESULT_STORAGE_BACKEND": "s3",
                "RESULT_STORAGE_S3_BUCKET": "django-ray-results",
            }
        )

    def test_validate_gcs_storage_requires_bucket(self) -> None:
        """GCS result storage requires a configured bucket."""
        with pytest.raises(ImproperlyConfigured, match="RESULT_STORAGE_GCS_BUCKET"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RESULT_STORAGE_BACKEND": "gcs",
                }
            )

    def test_validate_gcs_storage_with_bucket(self) -> None:
        """GCS result storage passes validation when bucket is set."""
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "RESULT_STORAGE_BACKEND": "gcs",
                "RESULT_STORAGE_GCS_BUCKET": "django-ray-results",
            }
        )
