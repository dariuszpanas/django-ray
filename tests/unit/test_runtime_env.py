"""Tests for RuntimeEnv profile resolution and durable snapshots."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.runtime_env import (
    normalize_runtime_env,
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
