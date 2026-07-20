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

    def test_validate_invalid_task_monitor_heartbeat(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="TASK_MONITOR_HEARTBEAT_SECONDS"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "TASK_MONITOR_HEARTBEAT_SECONDS": 0,
                }
            )

    @pytest.mark.parametrize(
        "setting",
        [
            "DEFAULT_CONCURRENCY",
            "MAX_TASK_ATTEMPTS",
            "RETRY_BACKOFF_SECONDS",
            "STUCK_TASK_TIMEOUT_SECONDS",
            "WORKER_LEASE_SECONDS",
            "WORKER_HEARTBEAT_SECONDS",
            "TASK_MONITOR_HEARTBEAT_SECONDS",
            "WORKFLOW_PROGRESS_FLUSH_SECONDS",
            "RAY_STATE_API_TIMEOUT_SECONDS",
            "MAX_RESULT_SIZE_BYTES",
            "WORKER_POLL_INTERVAL_SECONDS",
            "WORKER_POLL_MAX_INTERVAL_SECONDS",
        ],
    )
    def test_validate_numeric_settings_reject_booleans(self, setting: str) -> None:
        with pytest.raises(ImproperlyConfigured, match=setting):
            validate_settings({"RAY_ADDRESS": "ray://localhost:10001", setting: True})

    @pytest.mark.parametrize("ray_address", [None, "", "   ", 123, True])
    def test_validate_ray_address_requires_non_empty_string(self, ray_address) -> None:
        with pytest.raises(ImproperlyConfigured, match="RAY_ADDRESS"):
            validate_settings({"RAY_ADDRESS": ray_address})

    def test_validate_retry_backoff_boundaries(self) -> None:
        validate_settings({"RAY_ADDRESS": "ray://localhost:10001", "RETRY_BACKOFF_SECONDS": 0})
        validate_settings({"RAY_ADDRESS": "ray://localhost:10001", "RETRY_BACKOFF_SECONDS": 3600})
        with pytest.raises(ImproperlyConfigured, match="RETRY_BACKOFF_SECONDS"):
            validate_settings(
                {"RAY_ADDRESS": "ray://localhost:10001", "RETRY_BACKOFF_SECONDS": 3601}
            )

    @pytest.mark.parametrize(
        "setting,value",
        [
            ("WORKER_POLL_INTERVAL_SECONDS", "0.1"),
            ("WORKER_POLL_INTERVAL_SECONDS", 0.009),
            ("WORKER_POLL_INTERVAL_SECONDS", 10.01),
            ("WORKER_POLL_INTERVAL_SECONDS", float("nan")),
            ("WORKER_POLL_MAX_INTERVAL_SECONDS", "1.0"),
            ("WORKER_POLL_MAX_INTERVAL_SECONDS", 0.009),
            ("WORKER_POLL_MAX_INTERVAL_SECONDS", 60.01),
            ("WORKER_POLL_MAX_INTERVAL_SECONDS", float("inf")),
        ],
    )
    def test_validate_polling_settings_reject_unsafe_values(
        self, setting: str, value: object
    ) -> None:
        with pytest.raises(ImproperlyConfigured, match=setting):
            validate_settings({"RAY_ADDRESS": "ray://localhost:10001", setting: value})

    def test_validate_polling_settings_accept_boundaries(self) -> None:
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "WORKER_POLL_INTERVAL_SECONDS": 0.01,
                "WORKER_POLL_MAX_INTERVAL_SECONDS": 60,
            }
        )
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "WORKER_POLL_INTERVAL_SECONDS": 0.05,
                "WORKER_POLL_MAX_INTERVAL_SECONDS": 0.05,
            }
        )
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "WORKER_POLL_INTERVAL_SECONDS": 10,
                "WORKER_POLL_MAX_INTERVAL_SECONDS": 10.0,
            }
        )

    def test_validate_polling_maximum_must_not_be_below_base(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="must be greater than or equal"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "WORKER_POLL_INTERVAL_SECONDS": 2.0,
                    "WORKER_POLL_MAX_INTERVAL_SECONDS": 1.0,
                }
            )

    def test_validate_worker_heartbeat_must_be_less_than_lease(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="WORKER_HEARTBEAT_SECONDS"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "WORKER_LEASE_SECONDS": 30,
                    "WORKER_HEARTBEAT_SECONDS": 30,
                }
            )

    def test_validate_task_monitor_heartbeat_must_be_less_than_stuck_timeout(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="TASK_MONITOR_HEARTBEAT_SECONDS"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "STUCK_TASK_TIMEOUT_SECONDS": 30,
                    "TASK_MONITOR_HEARTBEAT_SECONDS": 30,
                }
            )

    @pytest.mark.parametrize("denylist", ["ValueError", [ValueError], {"ValueError"}])
    def test_validate_retry_denylist_requires_list_of_strings(self, denylist) -> None:
        with pytest.raises(ImproperlyConfigured, match="RETRY_EXCEPTION_DENYLIST"):
            validate_settings(
                {"RAY_ADDRESS": "ray://localhost:10001", "RETRY_EXCEPTION_DENYLIST": denylist}
            )

    def test_validate_result_storage_scalars(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="RESULT_STORAGE_S3_PREFIX"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RESULT_STORAGE_S3_PREFIX": 123,
                }
            )

    def test_validate_runtime_env_profiles(self) -> None:
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "RUNTIME_ENV_PROFILES": {
                    "thin": {"env_vars": {"MODE": "thin"}},
                },
                "DEFAULT_RUNTIME_ENV_PROFILE": "thin",
            }
        )

    def test_validate_ray_state_api_address_type(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="RAY_STATE_API_ADDRESS"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RAY_STATE_API_ADDRESS": 8265,
                }
            )

    def test_validate_unknown_default_runtime_env_profile(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="DEFAULT_RUNTIME_ENV_PROFILE"):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "RUNTIME_ENV_PROFILES": {},
                    "DEFAULT_RUNTIME_ENV_PROFILE": "missing",
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

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"MAX_INLINE_INPUT_SIZE_BYTES": 100}, "MAX_INLINE_INPUT_SIZE_BYTES"),
            ({"MAX_INLINE_INPUT_SIZE_BYTES": 1024}, "INPUT_STORAGE_BACKEND"),
            ({"INPUT_STORAGE_BACKEND": "digest"}, "digest-only"),
            (
                {
                    "INPUT_STORAGE_BACKEND": "filesystem",
                    "INPUT_STORAGE_FILESYSTEM_PATH": 123,
                },
                "must be a string or None",
            ),
            ({"INPUT_STORAGE_BACKEND": "filesystem"}, "INPUT_STORAGE_FILESYSTEM_PATH"),
            ({"INPUT_STORAGE_BACKEND": "s3"}, "INPUT_STORAGE_S3_BUCKET"),
            ({"INPUT_STORAGE_BACKEND": "gcs"}, "INPUT_STORAGE_GCS_BUCKET"),
        ],
    )
    def test_validate_input_storage_rejects_incomplete_configuration(
        self,
        overrides: dict[str, object],
        message: str,
    ) -> None:
        with pytest.raises(ImproperlyConfigured, match=message):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    **overrides,
                }
            )

    @pytest.mark.parametrize(
        "config",
        [
            {
                "INPUT_STORAGE_BACKEND": "filesystem",
                "INPUT_STORAGE_FILESYSTEM_PATH": "/var/lib/django-ray/inputs",
            },
            {
                "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
                "INPUT_STORAGE_BACKEND": "s3",
                "INPUT_STORAGE_S3_BUCKET": "inputs",
            },
            {
                "INPUT_STORAGE_BACKEND": "gcs",
                "INPUT_STORAGE_GCS_BUCKET": "inputs",
            },
        ],
    )
    def test_validate_input_storage_accepts_retrievable_backends(
        self,
        config: dict[str, object],
    ) -> None:
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                **config,
            }
        )

    @pytest.mark.parametrize(
        "patterns, message",
        [
            (b"password", "must be a string or a sequence"),
            ([""], "entries must be non-empty strings"),
            ([123], "entries must be non-empty strings"),
            (["["], "contains invalid regex"),
        ],
    )
    def test_validate_redact_patterns_rejects_invalid_values(self, patterns, message: str) -> None:
        with pytest.raises(ImproperlyConfigured, match=message):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    "REDACT_PATTERNS": patterns,
                }
            )

    def test_validate_redact_patterns_accepts_a_string_or_sequence(self) -> None:
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "REDACT_PATTERNS": r"access[_-]?token",
            }
        )
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "REDACT_PATTERNS": [r"password", r"api[_-]?key"],
            }
        )

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"WORKFLOW_PLAN_CODE_REVISION": ""}, "WORKFLOW_PLAN_CODE_REVISION"),
            ({"WORKFLOW_PLAN_TRUST_IDENTITY": []}, "must be a mapping"),
            (
                {"WORKFLOW_PLAN_TRUST_IDENTITY": {"token": "must-not-be-accepted"}},
                "unsupported fields",
            ),
            (
                {"WORKFLOW_PLAN_TRUST_IDENTITY": {"credential_revision": ""}},
                "non-empty string",
            ),
        ],
    )
    def test_validate_workflow_plan_identity_rejects_ambiguous_values(
        self,
        overrides: dict[str, object],
        message: str,
    ) -> None:
        with pytest.raises(ImproperlyConfigured, match=message):
            validate_settings(
                {
                    "RAY_ADDRESS": "ray://localhost:10001",
                    **overrides,
                }
            )

    def test_validate_workflow_plan_identity_accepts_non_secret_revisions(self) -> None:
        validate_settings(
            {
                "RAY_ADDRESS": "ray://localhost:10001",
                "WORKFLOW_PLAN_CODE_REVISION": "container:sha256:0123456789abcdef",
                "WORKFLOW_PLAN_TRUST_IDENTITY": {
                    "trust_domain": "cluster:production",
                    "credential_provider": "workload-identity",
                    "credential_revision": "provider-v3",
                    "environment_revision": "namespace-sync-v8",
                    "scheduling_revision": "placement-v2",
                },
            }
        )
