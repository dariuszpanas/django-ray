"""Tests for RuntimeEnv profile resolution and durable snapshots."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.runtime_env import (
    normalize_runtime_env,
    prepare_runtime_env_for_ray_core,
    resolve_runtime_env_profile,
    runtime_env_for_execution,
    validate_runtime_env_profiles,
)


def _config() -> dict:
    return {
        "RAY_RUNTIME_ENV": {"env_vars": {"SOURCE": "legacy"}},
        "RUNTIME_ENV_PROFILES": {
            "thin": {"env_vars": {"MODE": "thin"}},
            "numpy": {"pip": ["numpy==2.3.5"], "env_vars": {"MODE": "numpy"}},
        },
        "DEFAULT_RUNTIME_ENV_PROFILE": "thin",
    }


def test_normalization_is_canonical_and_content_addressed() -> None:
    first = normalize_runtime_env({"env_vars": {"B": "2", "A": "1"}})
    second = normalize_runtime_env({"env_vars": {"A": "1", "B": "2"}})

    assert first.serialized == second.serialized
    assert first.digest == second.digest
    assert len(first.digest) == 64


def test_resolve_named_and_default_profiles() -> None:
    named = resolve_runtime_env_profile("numpy", config=_config())
    default = resolve_runtime_env_profile(config=_config())

    assert named.profile == "numpy"
    assert named.spec["pip"] == ["numpy==2.3.5"]
    assert default.profile == "thin"


def test_profile_inheritance_merges_environment_and_appends_packages() -> None:
    config = {
        "RAY_RUNTIME_ENV": {},
        "RUNTIME_ENV_PROFILES": {
            "project": {
                "pip": ["django>=6.0"],
                "env_vars": {"BASE": "1", "MODE": "project"},
            },
            "numpy": {
                "extends": "project",
                "runtime_env": {
                    "pip": ["numpy==2.3.5"],
                    "env_vars": {"MODE": "numpy"},
                },
            },
        },
    }

    resolved = resolve_runtime_env_profile("numpy", config=config)

    assert resolved.spec["pip"] == ["django>=6.0", "numpy==2.3.5"]
    assert resolved.spec["env_vars"] == {"BASE": "1", "MODE": "numpy"}


def test_profile_inheritance_cycles_are_rejected() -> None:
    with pytest.raises(ImproperlyConfigured, match="inheritance cycle"):
        validate_runtime_env_profiles(
            {
                "RAY_RUNTIME_ENV": {},
                "RUNTIME_ENV_PROFILES": {
                    "one": {"extends": "two", "runtime_env": {}},
                    "two": {"extends": "one", "runtime_env": {}},
                },
            }
        )


def test_inline_backend_environment_cannot_be_combined_with_profile() -> None:
    with pytest.raises(ImproperlyConfigured, match="cannot both"):
        resolve_runtime_env_profile(
            "thin",
            config=_config(),
            inline_spec={"env_vars": {"X": "1"}},
        )


def test_unknown_profile_lists_available_profiles() -> None:
    with pytest.raises(ImproperlyConfigured, match="available: numpy, thin"):
        resolve_runtime_env_profile("missing", config=_config())


@pytest.mark.parametrize("profile", [1, {}, []])
def test_profile_name_must_be_a_string(profile) -> None:
    with pytest.raises(ImproperlyConfigured, match="RUNTIME_ENV_PROFILE must be a string"):
        resolve_runtime_env_profile(profile, config=_config())


def test_profile_validation_rejects_non_durable_values() -> None:
    with pytest.raises(ImproperlyConfigured, match="import path string"):
        validate_runtime_env_profiles(
            {
                "RAY_RUNTIME_ENV": {},
                "RUNTIME_ENV_PROFILES": {
                    "bad": {"worker_process_setup_hook": lambda: None},
                },
            }
        )


def test_execution_snapshot_detects_tampering() -> None:
    resolved = normalize_runtime_env({"env_vars": {"MODE": "thin"}}, profile="thin")
    execution = SimpleNamespace(
        pk=7,
        runtime_env_profile="thin",
        runtime_env_json=json.dumps({"env_vars": {"MODE": "changed"}}),
        runtime_env_hash=resolved.digest,
    )

    with pytest.raises(ImproperlyConfigured, match="hash does not match"):
        runtime_env_for_execution(execution)


def test_legacy_execution_without_snapshot_identity_uses_current_default(settings) -> None:
    settings.DJANGO_RAY = _config()
    execution = SimpleNamespace(
        pk=8,
        runtime_env_profile=None,
        runtime_env_json="{}",
        runtime_env_hash="",
    )

    resolved = runtime_env_for_execution(execution)

    assert resolved.profile == "thin"
    assert resolved.spec == {"env_vars": {"MODE": "thin"}}


def test_empty_snapshot_with_digest_remains_immutable(settings) -> None:
    settings.DJANGO_RAY = _config()
    empty = normalize_runtime_env({})
    execution = SimpleNamespace(
        pk=9,
        runtime_env_profile=None,
        runtime_env_json=empty.serialized,
        runtime_env_hash=empty.digest,
    )

    resolved = runtime_env_for_execution(execution)

    assert resolved == empty


def test_prepare_runtime_env_uploads_local_working_dir(monkeypatch, tmp_path) -> None:
    resolved = normalize_runtime_env(
        {
            "working_dir": str(tmp_path),
            "excludes": [".git"],
        }
    )
    monkeypatch.setattr("ray.util.client.ray.is_connected", lambda: False)
    monkeypatch.setattr(
        "ray._private.runtime_env.working_dir.upload_working_dir_if_needed",
        lambda spec, **kwargs: {**spec, "working_dir": "gcs://project.zip"},
    )
    monkeypatch.setattr(
        "ray._private.runtime_env.py_modules.upload_py_modules_if_needed",
        lambda spec, **kwargs: spec,
    )

    prepared = prepare_runtime_env_for_ray_core(resolved)

    assert prepared["working_dir"] == "gcs://project.zip"
    assert resolved.spec["working_dir"] == str(tmp_path)


def test_prepare_runtime_env_rejects_local_paths_over_ray_client(
    monkeypatch,
    tmp_path,
) -> None:
    resolved = normalize_runtime_env({"working_dir": str(tmp_path)})
    monkeypatch.setattr("ray.util.client.ray.is_connected", lambda: True)

    with pytest.raises(ImproperlyConfigured, match="direct Ray connection"):
        prepare_runtime_env_for_ray_core(resolved)
