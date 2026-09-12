"""Resource-free settings checks, never a PostgreSQL/native upgrade receipt."""

from __future__ import annotations

import base64
import json
import platform
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.backends.base.base import BaseDatabaseWrapper

SETTINGS_PATH = Path(__file__).resolve().parents[2] / "qualification/upgrade/runtime_settings.py"
PREFIX = "DJANGO_RAY_UPGRADE_"


@pytest.fixture
def environment(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in ("inputs", "results", "runtime-effects"):
        (artifacts / name).mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    values = {
        "postgres-password": "fixture-only-postgres-secret",
        "django-secret-key": "fixture-only-django-secret-" + "s" * 32,
        "runtime-env-key": base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode(),
    }
    for name, value in values.items():
        (secrets / name).write_text(value, encoding="utf-8")
    return {
        PREFIX + "BUILD": "candidate",
        PREFIX + "PYTHON_VERSION": platform.python_version(),
        PREFIX + "DATABASE": "primary",
        PREFIX + "PRIMARY_DATABASE": "upgrade_primary",
        PREFIX + "SCRATCH_DATABASE": "upgrade_scratch",
        PREFIX + "POSTGRES_HOST": "upgrade-postgres",
        PREFIX + "POSTGRES_PORT": "5432",
        PREFIX + "POSTGRES_USER": "upgrade_owner",
        PREFIX + "POSTGRES_PASSWORD_FILE": str(secrets / "postgres-password"),
        PREFIX + "CORE_ADDRESS": "upgrade-head:6379",
        PREFIX + "JOBS_ADDRESS": "http://upgrade-head:8265",
        PREFIX + "ARTIFACT_ROOT": str(artifacts),
        PREFIX + "ENCRYPTION_KEY_FILE": str(secrets / "runtime-env-key"),
        "DJANGO_SECRET_KEY_FILE": str(secrets / "django-secret-key"),
    }


@pytest.fixture
def module(environment, monkeypatch):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return runpy.run_path(str(SETTINGS_PATH))


def build(module, environment, *, package="0.5.0", ray="2.58.0", python=None):
    return module["build_runtime_settings"](
        environment,
        package_version=package,
        ray_version=ray,
        python_version=platform.python_version() if python is None else python,
    )


def test_loader_reads_only_explicit_local_configuration_without_connections(
    environment, monkeypatch
):
    import django
    import ray

    def forbidden(*args, **kwargs):
        pytest.fail("settings must not initialize applications or connect")

    monkeypatch.setattr(django, "setup", forbidden)
    monkeypatch.setattr(ray, "init", forbidden)
    monkeypatch.setattr(BaseDatabaseWrapper, "connect", forbidden)
    root = Path(environment[PREFIX + "ARTIFACT_ROOT"])
    before = sorted(path.relative_to(root) for path in root.rglob("*"))
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    result = runpy.run_path(str(SETTINGS_PATH))
    assert result["DATABASES"]["default"]["ENGINE"] == "django.db.backends.postgresql"
    assert result["DATABASES"]["default"]["NAME"] == "upgrade_primary"
    assert result["INSTALLED_APPS"] == ["django_ray"]
    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before


@pytest.mark.parametrize(
    "selector,name", [("primary", "upgrade_primary"), ("scratch", "upgrade_scratch")]
)
def test_explicit_database_selection_remains_one_primary_connection(
    module, environment, selector, name
):
    environment[PREFIX + "DATABASE"] = selector
    config = build(module, environment)
    assert set(config["DATABASES"]) == {"default"}
    assert config["DATABASE_ROUTERS"] == []
    database = config["DATABASES"]["default"]
    assert database["NAME"] == name
    assert database["PASSWORD"] == "fixture-only-postgres-secret"
    assert database["CONN_MAX_AGE"] == 0
    assert database["OPTIONS"] == {
        "connect_timeout": 5,
        "options": "-c statement_timeout=30000 -c lock_timeout=5000",
    }
    profile = config["DJANGO_RAY"]["RUNTIME_ENV_PROFILES"]["upgrade"]["env_vars"]
    assert profile[PREFIX + "DATABASE"] == selector


@pytest.mark.parametrize(
    "role,package,ray", [("baseline", "0.4.0", "2.56.0"), ("candidate", "0.5.0", "2.58.0")]
)
def test_shared_configuration_preserves_released_and_current_compatible_settings(
    module, environment, role, package, ray
):
    from django_ray.conf.settings import validate_settings

    environment[PREFIX + "BUILD"] = role
    config = build(module, environment, package=package, ray=ray)
    validate_settings(config["DJANGO_RAY"])
    assert config["DJANGO_RAY"]["DEFAULT_CONCURRENCY"] == 1
    assert (
        config["DJANGO_RAY"]["WORKER_HEARTBEAT_SECONDS"]
        < config["DJANGO_RAY"]["WORKER_LEASE_SECONDS"]
        < 180
    )
    assert config["DJANGO_RAY"]["MAX_TASK_ATTEMPTS"] == 2
    assert config["DJANGO_RAY"]["RETRY_EXCEPTION_DENYLIST"] == [
        "qualification.upgrade.runtime_tasks.FixtureTerminalError"
    ]
    assert (
        config["DJANGO_RAY"]["MAX_INLINE_INPUT_SIZE_BYTES"]
        == config["DJANGO_RAY"]["MAX_RESULT_SIZE_BYTES"]
        == 1024
    )


def test_profile_carries_only_secret_paths_and_preserves_supervisor_environment(
    module, environment
):
    config = build(module, environment)
    runtime = config["DJANGO_RAY"]["RUNTIME_ENV_PROFILES"]["upgrade"]
    assert set(runtime) == {"env_vars"}
    profile = runtime["env_vars"]
    assert set(profile) == set(environment) | {"DJANGO_SETTINGS_MODULE"}
    assert profile["DJANGO_SETTINGS_MODULE"] == "qualification.upgrade.runtime_settings"
    assert "RAY_ADDRESS" not in profile
    assert "RAY_JOB_CONFIG_JSON_ENV_VAR" not in profile
    assert "DJANGO_SECRET_KEY" not in profile
    for value in (
        config["SECRET_KEY"],
        config["DATABASES"]["default"]["PASSWORD"],
        config["DJANGO_RAY"]["RUNTIME_ENV_ENCRYPTION_KEYS"]["upgrade"],
    ):
        assert value not in json.dumps(runtime)


def test_encrypted_profile_roundtrip_uses_real_storage_codec(module, environment):
    from django_ray.runtime.runtime_env import (
        resolve_runtime_env_profile,
        runtime_env_for_execution,
        runtime_env_for_storage,
    )

    config = build(module, environment)["DJANGO_RAY"]
    resolved = resolve_runtime_env_profile(config=config)
    stored = runtime_env_for_storage(resolved, task_id="upgrade-encrypted", config=config)
    assert json.loads(stored.serialized)["format"] == "django-ray.runtime-env.encrypted"
    assert environment[PREFIX + "CORE_ADDRESS"] not in stored.serialized
    row = SimpleNamespace(
        pk=1,
        task_id="upgrade-encrypted",
        runtime_env_json=stored.serialized,
        runtime_env_profile=stored.profile,
        runtime_env_hash=stored.digest,
    )
    assert runtime_env_for_execution(row, config=config) == resolved


def test_configured_input_and_result_spills_are_readable_files(
    module, environment, settings, monkeypatch
):
    from django_ray.input_storage import load_task_input, prepare_task_input
    from django_ray.result_storage import load_result_reference
    from django_ray.runtime.entrypoint import _prepare_completion_result

    def forbidden(*args, **kwargs):
        pytest.fail("filesystem spill must not connect to the database")

    monkeypatch.setattr(BaseDatabaseWrapper, "connect", forbidden)
    config = build(module, environment)["DJANGO_RAY"]
    settings.DJANGO_RAY = config
    payload = "x" * 2048
    prepared = prepare_task_input((payload,), {}, config=config)
    assert prepared.is_external
    assert prepared.backend == "filesystem"
    assert load_task_input(
        args_json=prepared.args_json,
        kwargs_json=prepared.kwargs_json,
        input_reference=prepared.input_reference,
        config=config,
    ) == ([payload], {})
    inline, reference = _prepare_completion_result(
        payload, task_execution_pk=1, attempt_number=1, execution_generation=1
    )
    assert inline is None
    assert reference is not None and reference.startswith("resultfs://")
    serialized = load_result_reference(reference, config=config)
    assert serialized is not None and json.loads(serialized) == payload
    assert tuple(Path(config["INPUT_STORAGE_FILESYSTEM_PATH"]).rglob("*.json"))
    assert tuple(Path(config["RESULT_STORAGE_FILESYSTEM_PATH"]).rglob("*.json"))


def test_real_current_producer_and_manager_declarations_agree_without_ray_or_database(
    module, environment, settings, monkeypatch
):
    from django_ray.backends import RayTaskBackend
    from django_ray.runner.cohort_configuration import prepare_cohort_worker_configuration
    from django_ray.target.attestation import RayRunnerFamily
    from django_ray.target.cohort_intent import (
        cohort_declaration_digest,
        prepare_cohort_declaration,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("declaration preparation must not connect")

    monkeypatch.setattr(BaseDatabaseWrapper, "connect", forbidden)
    config = build(module, environment)
    settings.DJANGO_RAY = config["DJANGO_RAY"]
    for alias, queue, family, mode, address in (
        ("default", "upgrade-core", RayRunnerFamily.RAY_CORE, "cluster", "upgrade-head:6379"),
        ("jobs", "upgrade-jobs", RayRunnerFamily.RAY_JOB, "ray", "http://upgrade-head:8265"),
    ):
        backend = RayTaskBackend(alias, config["TASKS"][alias])
        declaration = prepare_cohort_declaration(
            alias,
            options=backend._cohort_declaration_options,
            current_settings=config["DJANGO_RAY"],
        )
        plan = prepare_cohort_worker_configuration(
            tasks=config["TASKS"],
            validated_aliases=("default", "jobs"),
            selected_queues=(queue,),
            manager_settings=config["DJANGO_RAY"],
            django_settings_module=module["SETTINGS_MODULE"],
            runner_family=family,
            execution_mode=mode,
            core_address=address if family is RayRunnerFamily.RAY_CORE else None,
        )
        assert len(plan.aliases) == 1
        assert plan.aliases[0].alias == alias
        assert plan.aliases[0].declaration_digest == cohort_declaration_digest(declaration)
        assert declaration.ray_address == backend.ray_target_address == address
        assert declaration.ray_job_only is (alias == "jobs")
        assert plan.job_addresses == ((("jobs", address),) if alias == "jobs" else ())


@pytest.mark.parametrize(
    "missing",
    [
        "BUILD",
        "DATABASE",
        "PRIMARY_DATABASE",
        "SCRATCH_DATABASE",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD_FILE",
        "CORE_ADDRESS",
        "JOBS_ADDRESS",
        "ARTIFACT_ROOT",
        "ENCRYPTION_KEY_FILE",
        "PYTHON_VERSION",
    ],
)
def test_missing_configuration_has_no_fallback(module, environment, missing):
    del environment[PREFIX + missing]
    with pytest.raises(ImproperlyConfigured, match="^invalid-native-upgrade-settings$"):
        build(module, environment)


@pytest.mark.parametrize(
    "changes",
    [
        {"DATABASE": "sqlite"},
        {"DATABASE": "other"},
        {"SCRATCH_DATABASE": "upgrade_primary"},
        {"PRIMARY_DATABASE": "other;drop"},
        {"POSTGRES_HOST": "user@other"},
        {"POSTGRES_PORT": "65536"},
        {"CORE_ADDRESS": "auto"},
        {"CORE_ADDRESS": "http://head:8265"},
        {"JOBS_ADDRESS": "http://user:password@head:8265"},
        {"JOBS_ADDRESS": "http://head:8265/path"},
        {"PYTHON_VERSION": "3.12"},
        {"BUILD": "unknown"},
    ],
)
def test_invalid_routes_and_runtime_declarations_refuse_without_echo(module, environment, changes):
    environment.update({PREFIX + key: value for key, value in changes.items()})
    with pytest.raises(ImproperlyConfigured, match="^invalid-native-upgrade-settings$"):
        build(module, environment)


@pytest.mark.parametrize("kwargs", [{"package": "0.4.0"}, {"ray": "2.56.0"}, {"python": "3.12.1"}])
def test_observed_runtime_must_match_selected_exact_tuple(module, environment, kwargs):
    with pytest.raises(ImproperlyConfigured, match="^invalid-native-upgrade-settings$"):
        build(module, environment, **kwargs)


@pytest.mark.parametrize("kind", ["missing", "directory", "oversized", "invalid-key", "newline"])
def test_secret_files_are_required_regular_bounded_and_validated(
    module, environment, tmp_path, kind
):
    path = tmp_path / "bad-secret"
    field = "DJANGO_SECRET_KEY_FILE"
    if kind == "directory":
        path.mkdir()
    elif kind == "oversized":
        path.write_bytes(b"x" * 4097)
    elif kind == "invalid-key":
        path.write_text("not-an-encryption-key")
        field = PREFIX + "ENCRYPTION_KEY_FILE"
    elif kind == "newline":
        path.write_text("secret\n")
    environment[field] = str(path)
    with pytest.raises(ImproperlyConfigured, match="^invalid-native-upgrade-settings$"):
        build(module, environment)


def test_missing_artifact_directories_are_not_created(module, environment, tmp_path):
    absent = tmp_path / "absent"
    environment[PREFIX + "ARTIFACT_ROOT"] = str(absent)
    with pytest.raises(ImproperlyConfigured, match="^invalid-native-upgrade-settings$"):
        build(module, environment)
    assert not absent.exists()
