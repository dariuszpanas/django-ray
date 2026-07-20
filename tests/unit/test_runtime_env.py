"""Tests for RuntimeEnv profile resolution and durable snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.runtime_env import (
    _ray_runtime_env_default_excludes,
    normalize_runtime_env,
    prepare_runtime_env_for_ray_core,
    resolve_runtime_env_profile,
    runtime_env_for_execution,
    snapshot_local_runtime_env,
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


def test_runtime_env_merge_overrides_scalar_values() -> None:
    from django_ray.runtime.runtime_env import _merge_runtime_envs

    assert _merge_runtime_envs({"image_uri": "old"}, {"image_uri": "new"}) == {"image_uri": "new"}


def test_normalization_accepts_none() -> None:
    assert normalize_runtime_env(None).spec == {}


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("not-a-dict", "must be a dictionary"),
        ({"env_vars": {"VALID": 1}}, "env_vars must map"),
        ({"working_dir": 1}, "working_dir must be a string"),
        ({"image_uri": []}, "image_uri must be a string"),
        ({"py_executable": object()}, "py_executable must be a string"),
        ({"py_modules": ["valid", 1]}, "py_modules must be a list"),
        ({"pip": {object()}}, "JSON-serializable"),
    ],
)
def test_normalization_rejects_invalid_fields(spec, message) -> None:
    with pytest.raises(ImproperlyConfigured, match=message):
        normalize_runtime_env(spec)


def test_resolve_named_and_default_profiles() -> None:
    named = resolve_runtime_env_profile("numpy", config=_config())
    default = resolve_runtime_env_profile(config=_config())

    assert named.profile == "numpy"
    assert named.spec["pip"] == ["numpy==2.3.5"]
    assert default.profile == "thin"


def test_resolve_inline_and_legacy_default_environments() -> None:
    inline = resolve_runtime_env_profile(
        config={"RUNTIME_ENV_PROFILES": {}, "RAY_RUNTIME_ENV": {}},
        inline_spec={"env_vars": {"INLINE": "1"}},
    )
    legacy = resolve_runtime_env_profile(
        config={
            "RUNTIME_ENV_PROFILES": {},
            "RAY_RUNTIME_ENV": {"env_vars": {"LEGACY": "1"}},
        }
    )

    assert inline.spec == {"env_vars": {"INLINE": "1"}}
    assert legacy.spec == {"env_vars": {"LEGACY": "1"}}


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


@pytest.mark.parametrize(
    ("profiles", "message"),
    [
        ({"bad": "not-a-dict"}, "must be a dictionary"),
        (
            {"bad": {"extends": "missing", "runtime_env": {}}},
            "extends an unknown profile",
        ),
        (
            {"base": {}, "bad": {"extends": "base", "runtime_env": "invalid"}},
            "runtime_env must be a dictionary",
        ),
        (
            {"base": {}, "bad": {"extends": "base", "runtime_env": {}, "extra": True}},
            "unexpected fields",
        ),
    ],
)
def test_profile_composition_rejects_invalid_definitions(profiles, message) -> None:
    with pytest.raises(ImproperlyConfigured, match=message):
        validate_runtime_env_profiles(
            {
                "RAY_RUNTIME_ENV": {},
                "RUNTIME_ENV_PROFILES": profiles,
            }
        )


def test_profile_validation_rejects_invalid_container_and_default() -> None:
    with pytest.raises(ImproperlyConfigured, match="RUNTIME_ENV_PROFILES must be"):
        validate_runtime_env_profiles({"RUNTIME_ENV_PROFILES": []})
    with pytest.raises(ImproperlyConfigured, match="profile names"):
        validate_runtime_env_profiles({"RUNTIME_ENV_PROFILES": {"bad name": {}}})
    with pytest.raises(ImproperlyConfigured, match="DEFAULT_RUNTIME_ENV_PROFILE"):
        validate_runtime_env_profiles(
            {
                "RUNTIME_ENV_PROFILES": {"thin": {}},
                "DEFAULT_RUNTIME_ENV_PROFILE": "missing",
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


def test_execution_snapshot_handles_missing_and_invalid_json() -> None:
    missing = SimpleNamespace(
        pk=10,
        runtime_env_profile="thin",
        runtime_env_json="",
        runtime_env_hash="",
    )
    invalid = SimpleNamespace(
        pk=11,
        runtime_env_profile="thin",
        runtime_env_json="{",
        runtime_env_hash="digest",
    )

    assert runtime_env_for_execution(missing).spec == {}
    with pytest.raises(ImproperlyConfigured, match="invalid runtime_env_json"):
        runtime_env_for_execution(invalid)


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


def test_ray_runtime_env_default_excludes_falls_back_when_getter_is_missing(
    monkeypatch,
) -> None:
    from ray._private import ray_constants

    monkeypatch.delattr(
        ray_constants,
        "get_runtime_env_default_excludes",
        raising=False,
    )

    assert _ray_runtime_env_default_excludes() == []


def test_ray_runtime_env_default_excludes_delegates_each_call(monkeypatch) -> None:
    from ray._private import ray_constants

    values = iter((["first"], ["second"]))
    monkeypatch.setattr(
        ray_constants,
        "get_runtime_env_default_excludes",
        lambda: next(values),
        raising=False,
    )

    assert _ray_runtime_env_default_excludes() == ["first"]
    assert _ray_runtime_env_default_excludes() == ["second"]


def test_local_snapshot_preserves_ray_exclusion_and_py_module_semantics(
    monkeypatch,
    tmp_path,
) -> None:
    from ray._private import ray_constants

    monkeypatch.delenv("RAY_OVERRIDE_RUNTIME_ENV_DEFAULT_EXCLUDES", raising=False)
    monkeypatch.setattr(
        ray_constants,
        "get_runtime_env_default_excludes",
        lambda: ["venv"],
        raising=False,
    )
    working_dir = tmp_path / "working-dir"
    working_dir.mkdir()
    (working_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    virtualenv = working_dir / "venv"
    virtualenv.mkdir()
    (virtualenv / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")
    mercurial = working_dir / ".hg"
    mercurial.mkdir()
    (mercurial / "included").write_text("revision\n", encoding="utf-8")
    py_module = tmp_path / "shared_module"
    py_module.mkdir()
    (py_module / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")
    resolved = normalize_runtime_env(
        {
            "working_dir": str(working_dir),
            "py_modules": [str(py_module)],
        }
    )

    with snapshot_local_runtime_env(resolved) as snapshot:
        snapshotted_working_dir = Path(snapshot.spec["working_dir"])
        snapshotted_py_module = snapshot.spec["py_modules"][0]

        assert (snapshotted_working_dir / "app.py").is_file()
        assert (snapshotted_working_dir / ".hg" / "included").is_file()
        assert not (snapshotted_working_dir / "venv" / "ignored.py").exists()
        assert snapshotted_py_module.endswith("shared_module")
        assert (Path(snapshotted_py_module) / "__init__.py").is_file()


def test_prepare_runtime_env_rejects_local_paths_over_ray_client(
    monkeypatch,
    tmp_path,
) -> None:
    resolved = normalize_runtime_env({"working_dir": str(tmp_path)})
    monkeypatch.setattr("ray.util.client.ray.is_connected", lambda: True)

    with pytest.raises(ImproperlyConfigured, match="direct Ray connection"):
        prepare_runtime_env_for_ray_core(resolved)


def test_prepare_runtime_env_wraps_packaging_errors(monkeypatch, tmp_path) -> None:
    resolved = normalize_runtime_env({"py_modules": [str(tmp_path)]})
    monkeypatch.setattr("ray.util.client.ray.is_connected", lambda: False)
    monkeypatch.setattr(
        "ray._private.runtime_env.working_dir.upload_working_dir_if_needed",
        lambda spec, **kwargs: (_ for _ in ()).throw(RuntimeError("upload failed")),
    )

    with pytest.raises(ImproperlyConfigured, match="upload failed"):
        prepare_runtime_env_for_ray_core(resolved)
