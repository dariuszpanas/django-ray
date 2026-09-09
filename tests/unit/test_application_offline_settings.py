"""Explicit qualification settings preserve ordinary sample deployment behavior."""

import base64
import importlib.util
import os
import runpy
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

import testproject
from django_ray.runtime.runtime_env import resolve_runtime_env_profile


@pytest.fixture
def environment(monkeypatch):
    values = {
        "DJANGO_DEPLOYMENT_MODE": "production",
        "DJANGO_DEBUG": "false",
        "DJANGO_SECRET_KEY": "QualificationSecretForUnitTests0123456789" * 2,
        "DJANGO_API_TOKEN": "QualificationApiTokenForUnitTests0123456789",
        "DJANGO_ALLOWED_HOSTS": "django-web,localhost",
        "DATABASE_ENGINE": "django.db.backends.postgresql",
        "DATABASE_HOST": "postgres",
        "RAY_ADDRESS": "ray://ray-head:10001",
        "DJANGO_RAY_RECOVERY_WORKING_DIR": "/runtime/recovery.zip",
        "DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY": base64.urlsafe_b64encode(bytes(range(32)))
        .rstrip(b"=")
        .decode(),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def load_settings():
    # Load the real base with this test's environment, preserving any module used
    # by another test. Never replace the process's configured Django settings.
    previous = sys.modules.pop("testproject.settings", None)
    previous_attribute = vars(testproject).pop("settings", None)
    try:
        return runpy.run_module("testproject.settings_qualification")
    finally:
        sys.modules.pop("testproject.settings", None)
        if previous is not None:
            sys.modules["testproject.settings"] = previous
        vars(testproject).pop("settings", None)
        if previous_attribute is not None:
            testproject.settings = previous_attribute


def test_offline_profile_reuses_locked_archive_without_mutating_base(environment):
    config = load_settings()
    base = config["_base"]
    project = config["DJANGO_RAY"]["RUNTIME_ENV_PROFILES"]["project"]
    assert project["working_dir"] == "/runtime/recovery.zip"
    assert "pip" not in project
    assert "PYTHONPATH" not in project["env_vars"]
    assert project["env_vars"]["DJANGO_RAY_RUNTIME_ENV_STORAGE_PROBE"].startswith(
        "django-ray-runtime-env-encryption-canary-v1-"
    )
    assert "pip" in base.DJANGO_RAY["RUNTIME_ENV_PROFILES"]["project"]
    assert base.DJANGO_RAY["RUNTIME_ENV_STORAGE_MODE"] == "plaintext"
    assert config["TASKS"] == base.TASKS
    assert config["INSTALLED_APPS"] == base.INSTALLED_APPS
    assert config["MIDDLEWARE"] == base.MIDDLEWARE
    assert config["DJANGO_RAY"]["RUNTIME_ENV_STORAGE_MODE"] == "encrypted"
    assert config["DJANGO_RAY"]["RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY"] == "qualification"
    assert config["DJANGO_RAY"]["RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK"] is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("DJANGO_DEPLOYMENT_MODE", "demo"),
        ("DJANGO_DEBUG", "true"),
        ("DJANGO_SECRET_KEY", "too-short"),
        ("DJANGO_API_TOKEN", "too-short"),
        ("DJANGO_ALLOWED_HOSTS", "*"),
        ("DATABASE_ENGINE", "django.db.backends.sqlite3"),
        ("RAY_ADDRESS", "auto"),
        ("RAY_ADDRESS", "ray-head:6379"),
        ("RAY_ADDRESS", "ray://user:password@ray-head:10001"),
        ("RAY_ADDRESS", "ray://ray-head:10001?"),
        ("DJANGO_RAY_RECOVERY_WORKING_DIR", ""),
        ("DJANGO_RAY_RECOVERY_WORKING_DIR", "C:/runtime/recovery.zip"),
        ("DJANGO_RAY_RECOVERY_WORKING_DIR", "/runtime/../recovery.zip"),
        ("DJANGO_RAY_RECOVERY_WORKING_DIR", "/runtime//recovery.zip"),
        ("DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY", ""),
        ("DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY", "A" * 43 + "="),
    ],
)
def test_qualification_rejects_implicit_or_weakened_configuration(
    environment, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises((ImproperlyConfigured, ValueError)):
        load_settings()


@pytest.mark.parametrize("profile", ["project", "thin"])
def test_remote_profile_preserves_explicit_target_and_mounted_settings(
    environment, monkeypatch, profile
):
    driver = load_settings()
    runtime = resolve_runtime_env_profile(profile, config=driver["DJANGO_RAY"])
    # Generic Ray Pods inherit the application's config and Secret, but their
    # node-local address need not be the driver's Ray Client endpoint.
    monkeypatch.setenv("RAY_ADDRESS", "ray-head:6379")
    with pytest.raises(ImproperlyConfigured, match="explicit Ray Client target"):
        load_settings()
    for name, value in runtime.spec["env_vars"].items():
        monkeypatch.setenv(name, value)
    remote = load_settings()
    assert remote["DJANGO_RAY"]["RAY_ADDRESS"] == environment["RAY_ADDRESS"]
    assert (
        remote["DJANGO_RAY"]["RUNTIME_ENV_ENCRYPTION_KEYS"]
        == driver["DJANGO_RAY"]["RUNTIME_ENV_ENCRYPTION_KEYS"]
    )
    assert remote["DATABASES"] == driver["DATABASES"]
    assert runtime.spec["working_dir"] == environment["DJANGO_RAY_RECOVERY_WORKING_DIR"]
    assert "DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY" not in runtime.spec["env_vars"]
    assert "pip" not in runtime.spec and "PYTHONPATH" not in runtime.spec["env_vars"]
    assert os.environ["DJANGO_RAY_RUNTIME_ENV"] == profile
    monkeypatch.delenv("DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY")
    with pytest.raises(ImproperlyConfigured):
        load_settings()


@pytest.mark.postgresql
@pytest.mark.parametrize("profile", [None, "project", "thin"])
def test_qualification_settings_bootstrap_without_database_or_ray_start(environment, profile):
    # The ordinary dependency lanes intentionally omit the PostgreSQL extra.
    # make test-postgres requires this bootstrap in its no-skip evidence lane.
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip("Application bootstrap requires the optional PostgreSQL driver")
    child_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO", "DATABASE", "RAY_"))
    }
    child_environment.update(environment)
    child_environment.update(
        DJANGO_SETTINGS_MODULE="testproject.settings_qualification",
        DATABASE_HOST="qualification-database.invalid",
        DATABASE_PASSWORD="UnitTestDatabaseCredential",
    )
    if profile is not None:
        runtime = resolve_runtime_env_profile(profile, config=load_settings()["DJANGO_RAY"])
        child_environment["RAY_ADDRESS"] = "ray-head:6379"
        child_environment.update(runtime.spec["env_vars"])
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; django.setup(); import ray; assert not ray.is_initialized(); print('qualification-settings-ready')",
        ],
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "qualification-settings-ready"
