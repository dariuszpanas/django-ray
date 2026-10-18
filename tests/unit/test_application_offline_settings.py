"""Explicit qualification settings preserve ordinary sample deployment behavior."""

import base64
import builtins
import importlib.util
import os
import runpy
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

import testproject
from django_ray.conf.defaults import DEFAULTS
from django_ray.runtime.runtime_env import normalize_runtime_env, resolve_runtime_env_profile
from django_ray.workflow.plans import runtime_env_plan_identity


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.delenv("DJANGO_RAY_QUALIFICATION_CLIENT_ADDRESS", raising=False)
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
    assert config["DJANGO_RAY"]["RUNTIME_ENV_STORAGE_MODE"] == "required"
    assert config["DJANGO_RAY"]["RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY"] == "qualification"
    assert config["DJANGO_RAY"]["RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK"] is False


@pytest.mark.parametrize("generation", ["before", "after"])
@pytest.mark.parametrize("common_lease_seconds", [None, 120])
def test_jobs_overlay_isolated_from_core_with_offline_remote_settings(
    environment, monkeypatch, generation, common_lease_seconds
):
    core = load_settings()
    if common_lease_seconds is not None:
        core["DJANGO_RAY"]["WORKER_LEASE_SECONDS"] = common_lease_seconds
    base = SimpleNamespace(**{key: value for key, value in core.items() if key.isupper()})
    monkeypatch.setattr(testproject, "settings_qualification", base, raising=False)
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2", "owned-application-job")
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2_GENERATION", generation)
    jobs = runpy.run_module("testproject.settings_qualification_jobs")
    from django_ray.conf.settings import validate_ray_job_request_storage_settings

    validate_ray_job_request_storage_settings(jobs["DJANGO_RAY"])
    assert "rq2-qualification" not in core["TASKS"]
    assert core["DJANGO_RAY"]["RAY_ADDRESS"] == "ray://ray-head:10001"
    assert jobs["DJANGO_RAY"]["RAY_ADDRESS"] == "http://ray-head:8265"
    assert jobs["DJANGO_RAY"]["MAX_TASK_ATTEMPTS"] == 3
    assert jobs["DJANGO_RAY"]["INPUT_STORAGE_FILESYSTEM_PATH"] == "/rq2/requests"
    assert jobs["DJANGO_RAY"]["WORKER_HEARTBEAT_SECONDS"] == 2
    assert jobs["DJANGO_RAY"].get("WORKER_LEASE_SECONDS", DEFAULTS["WORKER_LEASE_SECONDS"]) == (
        core["DJANGO_RAY"].get("WORKER_LEASE_SECONDS", DEFAULTS["WORKER_LEASE_SECONDS"])
    )
    assert jobs["DJANGO_RAY"]["STUCK_TASK_TIMEOUT_SECONDS"] == 30
    for profile in ("rq2-qualification", "rq2-missing"):
        resolved = resolve_runtime_env_profile(profile, config=jobs["DJANGO_RAY"])
        assert resolved.spec["working_dir"] == "/runtime/recovery.zip"
        assert "pip" not in resolved.spec
        env = resolved.spec["env_vars"]
        assert env["DJANGO_SETTINGS_MODULE"] == "testproject.settings_qualification_jobs"
        assert env["RAY_ADDRESS"] == "ray://ray-head:10001"
        assert env["DJANGO_RAY_QUALIFICATION_CLIENT_ADDRESS"] == "ray://ray-head:10001"
        assert env["DJANGO_RAY_QUALIFICATION_RQ2_GENERATION"] == generation
        assert "DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY" not in env
        assert ("DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER" in env) == (profile == "rq2-missing")


@pytest.mark.django_db
@pytest.mark.parametrize("generation", ["before", "after"])
def test_jobs_cleanup_preserves_healthy_core_lease_on_another_queue(
    environment, monkeypatch, settings, generation
):
    from django_ray.conf.settings import get_settings, validate_settings
    from django_ray.lease_policy import LeasePolicyError
    from django_ray.models import TaskWorkerLease
    from django_ray.runner import leasing

    core = load_settings()
    base = SimpleNamespace(**{key: value for key, value in core.items() if key.isupper()})
    monkeypatch.setattr(testproject, "settings_qualification", base, raising=False)
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2", "owned-application-job")
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2_GENERATION", generation)
    jobs = runpy.run_module("testproject.settings_qualification_jobs")
    settings.DJANGO_RAY = jobs["DJANGO_RAY"]
    validate_settings()
    assert get_settings()["WORKER_LEASE_SECONDS"] == 60
    assert get_settings()["WORKER_HEARTBEAT_SECONDS"] == 2
    assert core["DJANGO_RAY"].get("WORKER_HEARTBEAT_SECONDS", 15) == 15
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(leasing, "datetime", SimpleNamespace(now=lambda _: now))
    for worker_id, queue_name, age in (
        ("core", "default", 10),
        ("jobs", "rq2-qualification", 1),
    ):
        TaskWorkerLease.objects.create(
            worker_id=worker_id,
            hostname="owned-fixture",
            pid=100 if worker_id == "core" else 101,
            queue_name=queue_name,
            started_at=now - timedelta(minutes=1),
            last_heartbeat_at=now - timedelta(seconds=age),
            capability_schema_version=1,
            legacy_admission_token=None,
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
        )
    rows = TaskWorkerLease.objects.order_by("worker_id")
    before = list(rows.values())
    # Exercise the actual cleanup query, not a mocked queue-scoped substitute.
    assert leasing.cleanup_expired_leases() == 0
    assert list(rows.values()) == before

    # The old overlay could expire Core between its ordinary 15-second beats.
    # The common database authority now refuses that mismatched cleanup before
    # any queue's leases are changed.
    settings.DJANGO_RAY = {**jobs["DJANGO_RAY"], "WORKER_LEASE_SECONDS": 6}
    with pytest.raises(LeasePolicyError, match="lease_policy_mismatch"):
        leasing.cleanup_expired_leases()
    assert list(rows.values()) == before


def test_negative_setup_sentinel_precedes_even_a_failed_base_import(tmp_path, monkeypatch):
    (tmp_path / "before").mkdir()
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2", "owned-application-job")
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2_GENERATION", "before")
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER", "missing")
    original = builtins.__import__

    def reject_base(name, global_ns=None, local_ns=None, fromlist=(), level=0):
        if "settings_qualification" in (fromlist or ()):
            raise ImportError("deliberately unavailable base")
        return original(name, global_ns, local_ns, fromlist, level)

    with monkeypatch.context() as isolated:
        isolated.setitem(sys.modules, "pathlib", SimpleNamespace(Path=lambda _: tmp_path))
        isolated.setattr(builtins, "__import__", reject_base)
        with pytest.raises(ImportError, match="deliberately unavailable"):
            runpy.run_module("testproject.settings_qualification_jobs")
    assert (tmp_path / "before" / "missing-setup").is_file()


def test_offline_settings_validate_shipped_image_worker_and_negative_queues(environment):
    from django.test import override_settings

    from testproject.tasks import add_numbers

    config = load_settings()
    with override_settings(TASKS=config["TASKS"]):
        for queue in ("sync", "ml", "low-priority"):
            assert add_numbers.using(queue_name=queue).queue_name == queue


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


@pytest.mark.parametrize("profile", ["project", "thin", "recovery-showcase"])
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


@pytest.mark.parametrize("profile", ["rq2-qualification", "rq2-missing"])
@pytest.mark.parametrize("generation", ["before", "after"])
def test_jobs_settings_accept_supervisor_native_address_without_replacing_it(
    environment, monkeypatch, profile, generation
):
    core = load_settings()
    base = SimpleNamespace(**{key: value for key, value in core.items() if key.isupper()})
    monkeypatch.setattr(testproject, "settings_qualification", base, raising=False)
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2", "owned-application-job")
    monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_RQ2_GENERATION", generation)
    jobs = runpy.run_module("testproject.settings_qualification_jobs")
    runtime = resolve_runtime_env_profile(profile, config=jobs["DJANGO_RAY"])
    child_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DJANGO", "DATABASE", "RAY_"))
    }
    child_environment.update(environment)
    child_environment.update(runtime.spec["env_vars"])
    # Match Ray Jobs' final driver-environment overwrite, after RuntimeEnv.
    child_environment["RAY_ADDRESS"] = "ray-head:6379"
    # The missing-object test must reject before settings bootstrap. Here we
    # inspect its delivered settings without writing that fixture's sentinel.
    child_environment.pop("DJANGO_RAY_QUALIFICATION_RQ2_SETUP_MARKER", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import os
                import sys
                from testproject import settings_qualification_jobs as jobs
                from testproject import settings_qualification as core
                from testproject import settings as ordinary
                from django_ray.conf.defaults import DEFAULTS
                from django_ray.conf.settings import validate_settings
                validate_settings({**DEFAULTS, **jobs.DJANGO_RAY})
                assert os.environ['RAY_ADDRESS'] == 'ray-head:6379'
                assert ordinary.DJANGO_RAY['RAY_ADDRESS'] == 'ray-head:6379'
                assert core.DJANGO_RAY['RAY_ADDRESS'] == 'ray://ray-head:10001'
                assert all(backend['OPTIONS']['RAY_ADDRESS'] == 'ray://ray-head:10001'
                           for backend in core.TASKS.values())
                assert jobs.DJANGO_RAY['RAY_ADDRESS'] == 'http://ray-head:8265'
                assert jobs.TASKS['rq2-qualification']['OPTIONS']['RAY_JOB_ONLY'] is True
                assert jobs.RQ2_GENERATION == os.environ['DJANGO_RAY_QUALIFICATION_RQ2_GENERATION']
                assert 'ray' not in sys.modules
                print('qualification-jobs-settings-ready')
            """),
        ],
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "qualification-jobs-settings-ready"


@pytest.mark.parametrize(
    "client_address",
    [
        None,
        "",
        "auto",
        "ray-head:6379",
        "ray://user:password@ray-head:10001",
        "ray://ray-head:10001?",
    ],
)
def test_supervisor_native_address_requires_valid_separate_client_target(
    environment, monkeypatch, client_address
):
    monkeypatch.setenv("RAY_ADDRESS", "ray-head:6379")
    if client_address is not None:
        monkeypatch.setenv("DJANGO_RAY_QUALIFICATION_CLIENT_ADDRESS", client_address)
    with pytest.raises(ImproperlyConfigured, match="explicit Ray Client target"):
        load_settings()
    assert os.environ["RAY_ADDRESS"] == "ray-head:6379"


def test_recovery_environment_has_retry_safe_nonsecret_identity(environment, tmp_path, monkeypatch):
    config = load_settings()
    runtime = resolve_runtime_env_profile("recovery-showcase", config=config["DJANGO_RAY"])
    archive = tmp_path / "recovery.zip"
    archive.write_bytes(b"bounded-unit-fixture")
    runtime = normalize_runtime_env({**runtime.spec, "working_dir": str(archive)})
    trust = config["DJANGO_RAY"].get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
    assert runtime_env_plan_identity(runtime, trust_identity=trust).retry_safe
    assert not runtime_env_plan_identity(runtime).retry_safe
    monkeypatch.setenv("DJANGO_API_TOKEN", "DifferentQualificationToken01234567890123456789")
    monkeypatch.setenv(
        "DJANGO_RAY_QUALIFICATION_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode(),
    )
    assert load_settings()["DJANGO_RAY"]["WORKFLOW_PLAN_TRUST_IDENTITY"] == trust
    monkeypatch.setenv("RAY_ADDRESS", "ray://replacement-head:10001")
    assert load_settings()["DJANGO_RAY"]["WORKFLOW_PLAN_TRUST_IDENTITY"] != trust


@pytest.mark.postgresql
@pytest.mark.parametrize("profile", [None, "project", "thin", "recovery-showcase"])
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
