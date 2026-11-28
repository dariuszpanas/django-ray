"""Unit tests for zero-surprise defaults, settings validation, system checks, and AppConfig."""

from __future__ import annotations

import base64

import pytest
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured

import django_ray
from django_ray.apps import DjangoRayConfig, _extract_manage_command
from django_ray.conf.defaults import DEFAULTS
from django_ray.conf.settings import (
    check_django_ray_settings,
    get_settings,
    validate_settings,
)


class TestZeroSurpriseDefaultsAndValidation:
    """Zero-surprise defaults and strict validation boundaries (F-002, F-021, F-035)."""

    def test_zero_surprise_defaults(self) -> None:
        assert DEFAULTS["RUNNER"] == "ray_core"
        assert DEFAULTS["RAY_ADDRESS"] == "auto"
        assert DEFAULTS["WORKER_POLL_INTERVAL_SECONDS"] == 0.1
        assert DEFAULTS["WORKER_POLL_MAX_INTERVAL_SECONDS"] == 1.0
        assert DEFAULTS["RETRY_BACKOFF_JITTER_RATIO"] == 0.1

    def test_zero_config_settings_validate_cleanly(self, settings) -> None:
        if hasattr(settings, "DJANGO_RAY"):
            delattr(settings, "DJANGO_RAY")

        merged = get_settings()
        assert merged["RUNNER"] == "ray_core"
        assert merged["RAY_ADDRESS"] == "auto"
        assert merged["WORKER_POLL_MAX_INTERVAL_SECONDS"] == 1.0
        validate_settings()

    def test_lazy_ray_address_allows_none_by_default_and_hints_auto_or_http_when_required(
        self,
    ) -> None:
        validate_settings({})
        validate_settings({"RAY_ADDRESS": None})

        with pytest.raises(ImproperlyConfigured) as exc_info:
            validate_settings({"RAY_ADDRESS": None}, require_ray_address=True)
        message = str(exc_info.value)
        assert "'auto'" in message
        assert "http://ray-head:8265" in message
        assert "ray://localhost:10001" not in message

    def test_use_tz_false_is_rejected(self, settings) -> None:
        settings.USE_TZ = False
        with pytest.raises(ImproperlyConfigured, match="USE_TZ must be True"):
            validate_settings({"RAY_ADDRESS": "auto"})

    @pytest.mark.parametrize("bad_value", [None, [], "auto", 123, True])
    def test_non_mapping_django_ray_setting_is_rejected(self, settings, bad_value: object) -> None:
        settings.DJANGO_RAY = bad_value
        with pytest.raises(ImproperlyConfigured, match="must be a mapping"):
            get_settings()
        with pytest.raises(ImproperlyConfigured, match="must be a mapping"):
            validate_settings(bad_value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_addr", ["", "   "])
    def test_blank_ray_state_api_address_is_rejected(self, bad_addr: str) -> None:
        with pytest.raises(ImproperlyConfigured, match="RAY_STATE_API_ADDRESS"):
            validate_settings({"RAY_ADDRESS": "auto", "RAY_STATE_API_ADDRESS": bad_addr})

    @pytest.mark.parametrize(
        "ratio",
        [-0.01, 1.01, True, False, "0.1", float("nan"), float("inf")],
    )
    def test_invalid_retry_backoff_jitter_ratio_is_rejected(self, ratio: object) -> None:
        with pytest.raises(ImproperlyConfigured, match="RETRY_BACKOFF_JITTER_RATIO"):
            validate_settings({"RAY_ADDRESS": "auto", "RETRY_BACKOFF_JITTER_RATIO": ratio})

    @pytest.mark.parametrize("ratio", [0, 0.0, 0.1, 0.5, 1, 1.0])
    def test_valid_retry_backoff_jitter_ratio_is_accepted(self, ratio: float) -> None:
        validate_settings({"RAY_ADDRESS": "auto", "RETRY_BACKOFF_JITTER_RATIO": ratio})


class TestSystemChecks:
    """Django system checks W002 (digest stub) and W003 (plaintext env_vars) (F-035, F-092)."""

    def test_w002_emitted_for_digest_result_storage_and_cleared_for_filesystem(
        self, settings, tmp_path
    ) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "RESULT_STORAGE_BACKEND": "digest"}
        ids = {msg.id for msg in check_django_ray_settings()}
        assert "django_ray.W002" in ids

        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        }
        ids_fs = {msg.id for msg in check_django_ray_settings()}
        assert "django_ray.W002" not in ids_fs

    def test_w003_emitted_for_plaintext_env_vars_without_leaking_values(
        self, settings, tmp_path
    ) -> None:
        secret_marker = "super-secret-plaintext-env-value-991a"
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "auto",
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
            "RUNTIME_ENV_STORAGE_MODE": "plaintext",
            "RUNTIME_ENV_PROFILES": {
                "analytics": {"env_vars": {"API_SECRET": secret_marker}},
            },
        }

        messages = check_django_ray_settings()
        w003 = [msg for msg in messages if msg.id == "django_ray.W003"]
        assert len(w003) == 1
        assert "analytics" in w003[0].msg
        assert secret_marker not in w003[0].msg
        assert secret_marker not in (w003[0].hint or "")

        # Encrypted storage clears W003.
        key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "RUNTIME_ENV_STORAGE_MODE": "encrypted",
            "RUNTIME_ENV_ENCRYPTION_KEYS": {"k1": key},
            "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "k1",
        }
        assert not [msg for msg in check_django_ray_settings() if msg.id == "django_ray.W003"]

    def test_system_checks_are_registered_with_django_check_framework(self, settings) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "RESULT_STORAGE_BACKEND": "digest"}
        all_ids = {msg.id for msg in run_checks()}
        assert "django_ray.W002" in all_ids

    def test_invalid_settings_surface_as_e002_in_system_check(self, settings) -> None:
        settings.DJANGO_RAY = {"RAY_ADDRESS": "auto", "RUNNER": "invalid"}
        messages = check_django_ray_settings()
        assert len(messages) == 1
        assert messages[0].id == "django_ray.E002"


class TestAppConfigAndPackageCleanup:
    """AppConfig sys.argv parsing and removal of obsolete default_app_config (F-093)."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["manage.py", "migrate"], "migrate"),
            (["manage.py", "--settings=proj.settings", "migrate"], "migrate"),
            (["manage.py", "--settings", "proj.settings", "collectstatic"], "collectstatic"),
            (["manage.py", "-v", "2", "--no-color", "makemigrations"], "makemigrations"),
            (["manage.py", "--pythonpath", "/app", "check"], "check"),
            (["manage.py", "--", "migrate"], "migrate"),
            (["manage.py", "--settings", "proj.settings"], None),
            (["manage.py", "runserver", "0.0.0.0:8000"], "runserver"),
        ],
    )
    def test_extract_manage_command_handles_preceding_options(
        self, argv: list[str], expected: str | None
    ) -> None:
        assert _extract_manage_command(argv) == expected

    @pytest.mark.parametrize(
        ("argv", "should_skip"),
        [
            (["manage.py", "--settings=prod", "migrate"], True),
            (["manage.py", "--settings", "prod", "collectstatic"], True),
            (["manage.py", "check"], True),
            (["manage.py", "--settings=prod", "runserver"], False),
            (["manage.py", "django_ray_worker"], False),
        ],
    )
    def test_should_skip_validation_respects_command_after_flags(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str], should_skip: bool
    ) -> None:
        monkeypatch.delenv("DJANGO_RAY_SKIP_VALIDATION", raising=False)
        monkeypatch.setattr("django_ray.apps.sys.argv", argv)
        assert DjangoRayConfig._should_skip_validation() is should_skip

    def test_obsolete_default_app_config_removed(self) -> None:
        assert not hasattr(django_ray, "default_app_config")
