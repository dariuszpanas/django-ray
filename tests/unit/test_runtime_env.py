"""Tests for RuntimeEnv profile resolution and durable snapshots."""

from __future__ import annotations

import base64
import json
import traceback
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.runtime_env import (
    RuntimeEnvSnapshotError,
    _ray_runtime_env_default_excludes,
    normalize_runtime_env,
    prepare_runtime_env_for_ray_core,
    resolve_runtime_env_profile,
    runtime_env_for_execution,
    runtime_env_for_storage,
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


def _encryption_config(*, mode: str = "encrypted") -> dict:
    key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
    return {
        "RUNTIME_ENV_STORAGE_MODE": mode,
        "RUNTIME_ENV_ENCRYPTION_KEYS": {"test-key-1": key},
        "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": "test-key-1",
        "RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK": False,
    }


def _assert_snapshot_error_is_sanitized(
    error: RuntimeEnvSnapshotError,
    marker: str,
) -> None:
    formatted = "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__,
        )
    )
    assert marker not in str(error)
    assert marker not in repr(error)
    assert marker not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


def test_normalization_is_canonical_and_content_addressed() -> None:
    first = normalize_runtime_env({"env_vars": {"B": "2", "A": "1"}})
    second = normalize_runtime_env({"env_vars": {"A": "1", "B": "2"}})

    assert first.serialized == second.serialized
    assert first.digest == second.digest
    assert len(first.digest) == 64


def test_storage_seam_preserves_canonical_snapshot_identity() -> None:
    resolved = normalize_runtime_env(
        {"env_vars": {"B": "2", "A": "1"}},
        profile="thin",
    )

    stored = runtime_env_for_storage(resolved, task_id="task-6")
    loaded = runtime_env_for_execution(
        SimpleNamespace(
            pk=6,
            task_id="task-6",
            runtime_env_profile=stored.profile,
            runtime_env_json=stored.serialized,
            runtime_env_hash=stored.digest,
        )
    )

    assert stored.serialized == '{"env_vars":{"A":"1","B":"2"}}'
    assert loaded == resolved


def test_runtime_env_value_reprs_omit_plaintext_and_raw_storage() -> None:
    marker = "arbitrary-runtime-env-repr-secret-5d23"
    resolved = normalize_runtime_env(
        {"env_vars": {"API_TOKEN": marker}},
        profile="thin",
    )
    plaintext = runtime_env_for_storage(
        resolved,
        task_id="repr-plaintext-task",
        config={"RUNTIME_ENV_STORAGE_MODE": "plaintext"},
    )
    encrypted = runtime_env_for_storage(
        resolved,
        task_id="repr-encrypted-task",
        config=_encryption_config(),
    )

    for value in (resolved, plaintext, encrypted):
        diagnostic = repr(value)
        assert marker not in diagnostic
        assert resolved.serialized not in diagnostic
        assert encrypted.serialized not in diagnostic


def test_encrypted_storage_is_randomized_and_dual_read_preserves_plaintext_identity() -> None:
    marker = "arbitrary-runtime-env-secret-7c4e2a91"
    resolved = normalize_runtime_env(
        {"env_vars": {"API_TOKEN": marker}},
        profile="thin",
    )
    encrypted_config = _encryption_config()

    first = runtime_env_for_storage(
        resolved,
        task_id="encrypted-task-1",
        config=encrypted_config,
    )
    second = runtime_env_for_storage(
        resolved,
        task_id="encrypted-task-1",
        config=encrypted_config,
    )

    assert first.digest == second.digest == resolved.digest
    assert first.serialized != second.serialized
    assert marker not in first.serialized
    envelope = json.loads(first.serialized)
    assert set(envelope) == {
        "algorithm",
        "ciphertext",
        "format",
        "key_id",
        "nonce",
        "version",
    }
    assert envelope["format"] == "django-ray.runtime-env.encrypted"

    rollback_config = _encryption_config(mode="plaintext")
    loaded = runtime_env_for_execution(
        SimpleNamespace(
            pk=13,
            task_id="encrypted-task-1",
            runtime_env_profile=first.profile,
            runtime_env_json=first.serialized,
            runtime_env_hash=first.digest,
        ),
        config=rollback_config,
    )

    assert loaded == resolved


def test_encrypted_no_profile_snapshot_cannot_enter_legacy_fallback_when_hash_is_missing(
    settings,
) -> None:
    settings.DJANGO_RAY = _config()
    marker = "arbitrary-runtime-env-secret-missing-hash-9ac1"
    resolved = normalize_runtime_env({"env_vars": {"API_TOKEN": marker}})
    config = _encryption_config()
    stored = runtime_env_for_storage(
        resolved,
        task_id="encrypted-missing-hash-task",
        config=config,
    )

    with pytest.raises(RuntimeEnvSnapshotError, match="incomplete identity") as exc_info:
        runtime_env_for_execution(
            SimpleNamespace(
                pk=14,
                task_id="encrypted-missing-hash-task",
                runtime_env_profile=None,
                runtime_env_json=stored.serialized,
                runtime_env_hash="",
            ),
            config=config,
        )

    _assert_snapshot_error_is_sanitized(exc_info.value, marker)
    assert stored.serialized not in str(exc_info.value)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"task_id": "transplanted-task"}, "authentication failed"),
        ({"runtime_env_profile": "other-profile"}, "authentication failed"),
        ({"runtime_env_hash": "0" * 64}, "authentication failed"),
    ],
)
def test_encrypted_snapshot_aad_tampering_fails_closed_without_disclosure(
    mutation,
    message,
) -> None:
    marker = "arbitrary-runtime-env-secret-aad-6f81"
    resolved = normalize_runtime_env(
        {"env_vars": {"API_TOKEN": marker}},
        profile="thin",
    )
    config = _encryption_config()
    stored = runtime_env_for_storage(
        resolved,
        task_id="aad-bound-task",
        config=config,
    )
    execution_fields = {
        "pk": 14,
        "task_id": "aad-bound-task",
        "runtime_env_profile": stored.profile,
        "runtime_env_json": stored.serialized,
        "runtime_env_hash": stored.digest,
        **mutation,
    }

    with pytest.raises(RuntimeEnvSnapshotError, match=message) as exc_info:
        runtime_env_for_execution(
            SimpleNamespace(**execution_fields),
            config=config,
        )

    _assert_snapshot_error_is_sanitized(exc_info.value, marker)
    assert stored.serialized not in str(exc_info.value)


def test_encrypted_snapshot_ciphertext_and_key_id_tampering_fail_closed() -> None:
    marker = "arbitrary-runtime-env-secret-envelope-9e14"
    resolved = normalize_runtime_env(
        {"env_vars": {"API_TOKEN": marker}},
        profile="thin",
    )
    config = _encryption_config()
    stored = runtime_env_for_storage(
        resolved,
        task_id="envelope-bound-task",
        config=config,
    )
    envelope = json.loads(stored.serialized)

    ciphertext_tampered = {**envelope}
    first = ciphertext_tampered["ciphertext"][0]
    ciphertext_tampered["ciphertext"] = ("A" if first != "A" else "B") + ciphertext_tampered[
        "ciphertext"
    ][1:]
    unknown_key = {**envelope, "key_id": "missing-key"}

    for candidate, message in (
        (ciphertext_tampered, "authentication failed"),
        (unknown_key, "decryption key is unavailable"),
    ):
        serialized = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        with pytest.raises(RuntimeEnvSnapshotError, match=message) as exc_info:
            runtime_env_for_execution(
                SimpleNamespace(
                    pk=15,
                    task_id="envelope-bound-task",
                    runtime_env_profile=stored.profile,
                    runtime_env_json=serialized,
                    runtime_env_hash=stored.digest,
                ),
                config=config,
            )

        _assert_snapshot_error_is_sanitized(exc_info.value, marker)
        assert serialized not in str(exc_info.value)


def test_plaintext_snapshot_remains_readable_while_encrypted_writes_are_enabled() -> None:
    resolved = normalize_runtime_env(
        {"env_vars": {"MODE": "plaintext-row"}},
        profile="thin",
    )

    loaded = runtime_env_for_execution(
        SimpleNamespace(
            pk=16,
            task_id="plaintext-task",
            runtime_env_profile=resolved.profile,
            runtime_env_json=resolved.serialized,
            runtime_env_hash=resolved.digest,
        ),
        config=_encryption_config(),
    )

    assert loaded == resolved


@pytest.mark.parametrize(
    "custom_plugin_field",
    ["format", "version", "algorithm", "key_id", "nonce", "ciphertext"],
)
def test_runtime_env_allows_individual_generic_custom_plugin_fields(
    custom_plugin_field,
) -> None:
    resolved = normalize_runtime_env({custom_plugin_field: "application-value"})

    assert resolved.spec == {custom_plugin_field: "application-value"}


def test_runtime_env_rejects_the_namespaced_storage_discriminator() -> None:
    with pytest.raises(ImproperlyConfigured, match="reserved"):
        normalize_runtime_env({"format": "django-ray.runtime-env.encrypted"})


def test_storage_seam_rejects_an_internally_inconsistent_resolution() -> None:
    resolved = normalize_runtime_env({"env_vars": {"MODE": "thin"}})
    marker = "arbitrary-customer-marker-7cf3"
    inconsistent = replace(
        resolved,
        spec={"env_vars": {"MODE": marker}},
    )

    with pytest.raises(RuntimeEnvSnapshotError, match="snapshot is inconsistent") as exc_info:
        runtime_env_for_storage(inconsistent, task_id="task-inconsistent")

    _assert_snapshot_error_is_sanitized(exc_info.value, marker)


def test_storage_seam_rejects_non_ascii_digest_without_leaking_it() -> None:
    resolved = normalize_runtime_env({})
    marker = "é" * 64

    with pytest.raises(RuntimeEnvSnapshotError, match="snapshot is inconsistent") as exc_info:
        runtime_env_for_storage(
            replace(resolved, digest=marker),
            task_id="task-invalid-digest",
        )

    _assert_snapshot_error_is_sanitized(exc_info.value, marker)


def test_storage_seam_sanitizes_normalization_failures() -> None:
    resolved = normalize_runtime_env({})
    marker = "arbitrary-storage-secret-39f2"
    invalid = replace(resolved, spec={"pip": {marker}})

    with pytest.raises(RuntimeEnvSnapshotError, match="snapshot is invalid") as exc_info:
        runtime_env_for_storage(invalid, task_id="task-invalid")

    _assert_snapshot_error_is_sanitized(exc_info.value, marker)


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
        runtime_env_json=normalize_runtime_env(
            {"env_vars": {"MODE": "changed"}},
            profile="thin",
        ).serialized,
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


def test_legacy_execution_sanitizes_default_resolution_failures(monkeypatch) -> None:
    from django_ray.runtime import runtime_env as runtime_env_module

    marker = "arbitrary-legacy-secret-18a7"
    monkeypatch.setattr(
        runtime_env_module,
        "resolve_runtime_env_profile",
        lambda: (_ for _ in ()).throw(ImproperlyConfigured(marker)),
    )
    execution = SimpleNamespace(
        pk=8,
        runtime_env_profile=None,
        runtime_env_json="{}",
        runtime_env_hash="",
    )

    with pytest.raises(
        RuntimeEnvSnapshotError,
        match="Legacy RuntimeEnv fallback could not be resolved",
    ) as exc_info:
        runtime_env_for_execution(execution)

    _assert_snapshot_error_is_sanitized(exc_info.value, marker)


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


@pytest.mark.parametrize(
    ("profile", "serialized", "digest", "message"),
    [
        (None, "", "0" * 64, "is missing"),
        (
            "thin",
            '{"env_vars":{"VALUE":"arbitrary-customer-marker-7cf3"}',
            "0" * 64,
            "is malformed",
        ),
        ("thin", '["arbitrary-customer-marker-7cf3"]', "0" * 64, "is not a mapping"),
        (
            "thin",
            '{\n  "env_vars": {\n    "VALUE": "arbitrary-customer-marker-7cf3"\n  }\n}',
            normalize_runtime_env({"env_vars": {"VALUE": "arbitrary-customer-marker-7cf3"}}).digest,
            "is not canonical",
        ),
        (
            "thin",
            '{"env_vars":{"VALUE":1}}',
            "0" * 64,
            "is invalid",
        ),
        (
            "thin",
            '{"env_vars":{"VALUE":"\\ud800"}}',
            "0" * 64,
            "is invalid",
        ),
        (
            "thin",
            '{"env_vars":{"VALUE":"arbitrary-customer-marker-7cf3"}}',
            "",
            "incomplete identity",
        ),
    ],
)
def test_identified_execution_snapshot_fails_closed_without_disclosing_payload(
    profile,
    serialized,
    digest,
    message,
) -> None:
    execution = SimpleNamespace(
        pk=10,
        runtime_env_profile=profile,
        runtime_env_json=serialized,
        runtime_env_hash=digest,
    )

    with pytest.raises(RuntimeEnvSnapshotError, match=message) as exc_info:
        runtime_env_for_execution(execution)

    _assert_snapshot_error_is_sanitized(
        exc_info.value,
        "arbitrary-customer-marker-7cf3",
    )


def test_identified_execution_snapshot_rejects_json_recursion(monkeypatch) -> None:
    from django_ray.runtime import runtime_env as runtime_env_module

    monkeypatch.setattr(
        runtime_env_module.json,
        "loads",
        lambda _serialized: (_ for _ in ()).throw(RecursionError),
    )
    execution = SimpleNamespace(
        pk=10,
        runtime_env_profile=None,
        runtime_env_json='{"config":{}}',
        runtime_env_hash="0" * 64,
    )

    with pytest.raises(RuntimeEnvSnapshotError, match="is malformed") as exc_info:
        runtime_env_for_execution(execution)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_execution_snapshot_rejects_malformed_profile_metadata() -> None:
    empty = normalize_runtime_env({})
    execution = SimpleNamespace(
        pk=11,
        runtime_env_profile="not a valid profile",
        runtime_env_json=empty.serialized,
        runtime_env_hash=empty.digest,
    )

    with pytest.raises(RuntimeEnvSnapshotError, match="malformed profile metadata"):
        runtime_env_for_execution(execution)


def test_unidentified_nonmigration_payload_fails_closed(settings) -> None:
    settings.DJANGO_RAY = _config()
    missing = SimpleNamespace(
        pk=12,
        runtime_env_profile=None,
        runtime_env_json='{"env_vars":{"TOKEN":"legacy-unidentified-marker"}}',
        runtime_env_hash="",
    )

    with pytest.raises(RuntimeEnvSnapshotError, match="incomplete identity") as exc_info:
        runtime_env_for_execution(missing)

    _assert_snapshot_error_is_sanitized(exc_info.value, "legacy-unidentified-marker")


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


def test_prepare_runtime_env_uploads_local_archive_over_ray_client(
    monkeypatch,
    tmp_path,
) -> None:
    archive = tmp_path / "runtime-env.zip"
    archive.write_bytes(b"content-addressed fixture")
    resolved = normalize_runtime_env({"working_dir": str(archive)})
    monkeypatch.setattr("ray.util.client.ray.is_connected", lambda: True)
    monkeypatch.setattr(
        "ray._private.runtime_env.working_dir.upload_working_dir_if_needed",
        lambda spec, **kwargs: {
            **spec,
            "working_dir": f"gcs://_ray_pkg_{'a' * 40}.zip",
        },
    )
    monkeypatch.setattr(
        "ray._private.runtime_env.py_modules.upload_py_modules_if_needed",
        lambda spec, **kwargs: spec,
    )

    prepared = prepare_runtime_env_for_ray_core(resolved)

    assert prepared["working_dir"] == f"gcs://_ray_pkg_{'a' * 40}.zip"


def test_prepare_runtime_env_wraps_packaging_errors(monkeypatch, tmp_path) -> None:
    resolved = normalize_runtime_env({"py_modules": [str(tmp_path)]})
    monkeypatch.setattr("ray.util.client.ray.is_connected", lambda: False)
    monkeypatch.setattr(
        "ray._private.runtime_env.working_dir.upload_working_dir_if_needed",
        lambda spec, **kwargs: (_ for _ in ()).throw(RuntimeError("upload failed")),
    )

    with pytest.raises(ImproperlyConfigured, match="upload failed"):
        prepare_runtime_env_for_ray_core(resolved)
