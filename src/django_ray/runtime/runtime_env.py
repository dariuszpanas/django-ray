"""Runtime environment profiles and durable execution snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured

from django_ray.runtime.runtime_env_encryption import (
    RuntimeEnvEncryptionError,
    is_runtime_env_encryption_envelope_candidate,
    protect_runtime_env_snapshot,
    unprotect_runtime_env_snapshot,
    validate_runtime_env_encryption_settings,
)

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_APPENDABLE_FIELDS = {"excludes", "pip", "py_modules", "uv"}


class RuntimeEnvSnapshotError(ImproperlyConfigured):
    """Raised when a persisted RuntimeEnv snapshot cannot be trusted."""


@dataclass(frozen=True, repr=False)
class ResolvedRuntimeEnv:
    """A JSON-safe RuntimeEnv with a stable content identity."""

    profile: str | None
    spec: dict[str, Any]
    serialized: str
    digest: str


@dataclass(frozen=True, repr=False)
class RuntimeEnvStorageFields:
    """Versionable model-field values for one resolved RuntimeEnv."""

    profile: str | None
    serialized: str
    digest: str


def normalize_runtime_env(
    spec: dict[str, Any] | None,
    *,
    profile: str | None = None,
    source: str = "runtime environment",
) -> ResolvedRuntimeEnv:
    """Validate and canonicalize a Ray RuntimeEnv mapping."""
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise ImproperlyConfigured(f"django-ray: {source} must be a dictionary")

    env_vars = spec.get("env_vars")
    if env_vars is not None and (
        not isinstance(env_vars, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in env_vars.items()
        )
    ):
        raise ImproperlyConfigured(
            f"django-ray: {source}.env_vars must map string names to string values"
        )

    for field_name in ("working_dir", "image_uri", "py_executable"):
        value = spec.get(field_name)
        if value is not None and not isinstance(value, str):
            raise ImproperlyConfigured(f"django-ray: {source}.{field_name} must be a string")

    py_modules = spec.get("py_modules")
    if py_modules is not None and (
        not isinstance(py_modules, list)
        or any(not isinstance(module, str) for module in py_modules)
    ):
        raise ImproperlyConfigured(
            f"django-ray: {source}.py_modules must be a list of paths or URIs"
        )

    setup_hook = spec.get("worker_process_setup_hook")
    if setup_hook is not None and not isinstance(setup_hook, str):
        raise ImproperlyConfigured(
            f"django-ray: {source}.worker_process_setup_hook must be an import path string; "
            "callables cannot be stored durably"
        )

    try:
        serialized = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise ImproperlyConfigured(
            f"django-ray: {source} must contain only JSON-serializable values: {error}"
        ) from error

    normalized = json.loads(serialized)
    if is_runtime_env_encryption_envelope_candidate(normalized):
        raise ImproperlyConfigured(
            f"django-ray: {source} uses a top-level field reserved for "
            "RuntimeEnv storage encryption"
        )
    digest = hashlib.sha256(encoded).hexdigest()
    return ResolvedRuntimeEnv(
        profile=profile,
        spec=normalized,
        serialized=serialized,
        digest=digest,
    )


def validate_runtime_env_profiles(config: dict[str, Any]) -> None:
    """Validate all configured profiles and the selected default."""
    profiles = config.get("RUNTIME_ENV_PROFILES", {})
    if not isinstance(profiles, dict):
        raise ImproperlyConfigured("django-ray: RUNTIME_ENV_PROFILES must be a dictionary")

    for name in profiles:
        if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
            raise ImproperlyConfigured(
                "django-ray: RuntimeEnv profile names must be 1-100 characters and use only "
                "letters, numbers, '.', '_', or '-'"
            )
        normalize_runtime_env(
            _resolve_profile_spec(name, profiles, stack=()),
            profile=name,
            source=f"RUNTIME_ENV_PROFILES[{name!r}]",
        )

    normalize_runtime_env(config.get("RAY_RUNTIME_ENV", {}), source="RAY_RUNTIME_ENV")

    default_profile = config.get("DEFAULT_RUNTIME_ENV_PROFILE")
    if default_profile is not None:
        if not isinstance(default_profile, str) or default_profile not in profiles:
            raise ImproperlyConfigured(
                "django-ray: DEFAULT_RUNTIME_ENV_PROFILE must name an entry in RUNTIME_ENV_PROFILES"
            )


def resolve_runtime_env_profile(
    profile: str | None = None,
    *,
    config: dict[str, Any] | None = None,
    inline_spec: dict[str, Any] | None = None,
) -> ResolvedRuntimeEnv:
    """Resolve a named profile, inline backend spec, or configured default."""
    if config is None:
        from django_ray.conf.settings import get_settings

        config = get_settings()

    if profile is not None and not isinstance(profile, str):
        raise ImproperlyConfigured("django-ray: RUNTIME_ENV_PROFILE must be a string")

    if profile is not None and inline_spec is not None:
        raise ImproperlyConfigured(
            "django-ray: RUNTIME_ENV_PROFILE and RAY_RUNTIME_ENV cannot both be set "
            "on the same task backend"
        )

    if profile is not None:
        profiles = config.get("RUNTIME_ENV_PROFILES", {})
        if profile not in profiles:
            available = ", ".join(sorted(profiles)) or "(none)"
            raise ImproperlyConfigured(
                f"django-ray: Unknown RuntimeEnv profile {profile!r}; available: {available}"
            )
        return normalize_runtime_env(
            _resolve_profile_spec(profile, profiles, stack=()),
            profile=profile,
            source=f"RUNTIME_ENV_PROFILES[{profile!r}]",
        )

    if inline_spec is not None:
        return normalize_runtime_env(inline_spec, source="task backend RAY_RUNTIME_ENV")

    default_profile = config.get("DEFAULT_RUNTIME_ENV_PROFILE")
    if default_profile:
        return resolve_runtime_env_profile(default_profile, config=config)

    return normalize_runtime_env(config.get("RAY_RUNTIME_ENV", {}), source="RAY_RUNTIME_ENV")


def _resolve_profile_spec(
    profile: str,
    profiles: dict[str, Any],
    *,
    stack: tuple[str, ...],
) -> dict[str, Any]:
    """Resolve optional profile inheritance into one Ray RuntimeEnv mapping."""
    if profile in stack:
        cycle = " -> ".join((*stack, profile))
        raise ImproperlyConfigured(f"django-ray: RuntimeEnv profile inheritance cycle: {cycle}")

    definition = profiles[profile]
    if not isinstance(definition, dict):
        raise ImproperlyConfigured(
            f"django-ray: RUNTIME_ENV_PROFILES[{profile!r}] must be a dictionary"
        )

    is_composed = "extends" in definition or "runtime_env" in definition
    if not is_composed:
        return definition

    unknown = set(definition) - {"extends", "runtime_env"}
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ImproperlyConfigured(
            f"django-ray: Composed RuntimeEnv profile {profile!r} has unexpected fields: {fields}"
        )

    parent = definition.get("extends")
    if not isinstance(parent, str) or parent not in profiles:
        raise ImproperlyConfigured(
            f"django-ray: RuntimeEnv profile {profile!r} extends an unknown profile {parent!r}"
        )
    child = definition.get("runtime_env", {})
    if not isinstance(child, dict):
        raise ImproperlyConfigured(
            f"django-ray: RuntimeEnv profile {profile!r}.runtime_env must be a dictionary"
        )

    parent_spec = _resolve_profile_spec(parent, profiles, stack=(*stack, profile))
    return _merge_runtime_envs(parent_spec, child)


def _merge_runtime_envs(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    merged = {**parent}
    for key, value in child.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = {**existing, **value}
        elif key in _APPENDABLE_FIELDS and isinstance(existing, list) and isinstance(value, list):
            merged[key] = [*existing, *value]
        else:
            merged[key] = value
    return merged


def runtime_env_for_storage(
    runtime_env: ResolvedRuntimeEnv,
    *,
    task_id: str,
    config: dict[str, Any] | None = None,
) -> RuntimeEnvStorageFields:
    """Verify one resolved RuntimeEnv before persisting its durable snapshot."""
    if not isinstance(runtime_env, ResolvedRuntimeEnv):
        raise RuntimeEnvSnapshotError(
            "django-ray: Resolved RuntimeEnv storage snapshot has an invalid type"
        )
    profile = runtime_env.profile
    if profile is not None and (
        not isinstance(profile, str) or not _PROFILE_NAME.fullmatch(profile)
    ):
        raise RuntimeEnvSnapshotError(
            "django-ray: Resolved RuntimeEnv storage snapshot has invalid profile metadata"
        )

    canonical: ResolvedRuntimeEnv | None
    try:
        canonical = normalize_runtime_env(
            runtime_env.spec,
            profile=profile,
            source="resolved RuntimeEnv storage snapshot",
        )
    except ImproperlyConfigured:
        canonical = None
    if canonical is None:
        raise RuntimeEnvSnapshotError("django-ray: Resolved RuntimeEnv storage snapshot is invalid")

    if (
        not isinstance(runtime_env.serialized, str)
        or not isinstance(runtime_env.digest, str)
        or not _SHA256_DIGEST.fullmatch(runtime_env.digest)
        or canonical.serialized != runtime_env.serialized
        or not hmac.compare_digest(canonical.digest, runtime_env.digest)
    ):
        raise RuntimeEnvSnapshotError(
            "django-ray: Resolved RuntimeEnv storage snapshot is inconsistent"
        )

    if config is None:
        from django_ray.conf.settings import get_settings

        config = get_settings()
    encryption = validate_runtime_env_encryption_settings(config)
    protected: str | None
    try:
        protected = protect_runtime_env_snapshot(
            canonical.serialized,
            task_id=task_id,
            profile=profile,
            digest=canonical.digest,
            encryption=encryption,
        )
    except RuntimeEnvEncryptionError:
        protected = None
    if protected is None:
        raise RuntimeEnvSnapshotError(
            "django-ray: Resolved RuntimeEnv storage snapshot encryption failed"
        )
    return RuntimeEnvStorageFields(
        profile=profile,
        serialized=protected,
        digest=canonical.digest,
    )


def runtime_env_for_execution(
    task_execution: RayTaskExecution,
    *,
    config: dict[str, Any] | None = None,
) -> ResolvedRuntimeEnv:
    """Load and verify the immutable RuntimeEnv snapshot on an execution."""
    task_pk = getattr(task_execution, "pk", None)
    task_label = f" for task {task_pk}" if isinstance(task_pk, int) else ""
    raw_stored_profile = getattr(task_execution, "runtime_env_profile", None)
    stored_profile_was_empty = raw_stored_profile == ""
    stored_profile = raw_stored_profile
    if stored_profile_was_empty:
        stored_profile = None
    stored_digest = getattr(task_execution, "runtime_env_hash", "")
    if stored_digest is None:
        stored_digest = ""
    if not isinstance(stored_profile, (str, type(None))) or not isinstance(stored_digest, str):
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} has malformed identity metadata"
        )
    profile = stored_profile
    serialized = getattr(task_execution, "runtime_env_json", None)
    if not isinstance(serialized, str) or not serialized.strip():
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} is missing"
        )
    if not stored_digest and profile is None:
        # Migration 0002 backfills pre-0.3 rows with "{}" but cannot reconstruct
        # the RuntimeEnv that would previously have been resolved at submission.
        # Its exact "{}" payload plus no digest/profile is the durable legacy
        # marker; new empty snapshots still carry the SHA-256 digest of their
        # canonical "{}" payload.
        if serialized != "{}":
            raise RuntimeEnvSnapshotError(
                f"django-ray: Persisted RuntimeEnv snapshot{task_label} has an incomplete identity"
            )
        legacy_runtime_env: ResolvedRuntimeEnv | None
        try:
            legacy_runtime_env = resolve_runtime_env_profile()
        except ImproperlyConfigured:
            legacy_runtime_env = None
        if legacy_runtime_env is None:
            raise RuntimeEnvSnapshotError(
                "django-ray: Legacy RuntimeEnv fallback could not be resolved"
            )
        return legacy_runtime_env

    if profile is not None and not _PROFILE_NAME.fullmatch(profile):
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} has malformed profile metadata"
        )
    if not _SHA256_DIGEST.fullmatch(stored_digest):
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} has an incomplete identity"
        )

    decoded = True
    try:
        spec = json.loads(serialized)
    except (TypeError, ValueError, RecursionError):
        decoded = False
        spec = None
    if not decoded:
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} is malformed"
        )
    if not isinstance(spec, dict):
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} is not a mapping"
        )

    encrypted = is_runtime_env_encryption_envelope_candidate(spec)
    plaintext_serialized = serialized
    if encrypted:
        task_id = getattr(task_execution, "task_id", None)
        if stored_profile_was_empty or not isinstance(task_id, str) or not 1 <= len(task_id) <= 255:
            raise RuntimeEnvSnapshotError(
                f"django-ray: Persisted RuntimeEnv snapshot{task_label} "
                "has malformed encryption identity"
            )
        if config is None:
            from django_ray.conf.settings import get_settings

            config = get_settings()
        encryption_error: str | None = None
        try:
            encryption = validate_runtime_env_encryption_settings(config)
            plaintext_serialized = unprotect_runtime_env_snapshot(
                serialized,
                task_id=task_id,
                profile=profile,
                digest=stored_digest,
                encryption=encryption,
            )
        except RuntimeEnvEncryptionError as error:
            encryption_error = str(error)
        if encryption_error is not None:
            raise RuntimeEnvSnapshotError(
                f"django-ray: Persisted RuntimeEnv snapshot{task_label}: {encryption_error}"
            )
        try:
            spec = json.loads(plaintext_serialized)
        except (TypeError, ValueError, RecursionError):
            spec = None
        if not isinstance(spec, dict):
            raise RuntimeEnvSnapshotError(
                f"django-ray: Persisted RuntimeEnv snapshot{task_label} "
                "decrypted payload is invalid"
            )

    resolved: ResolvedRuntimeEnv | None
    try:
        resolved = normalize_runtime_env(
            spec,
            profile=profile,
            source=f"persisted RuntimeEnv snapshot{task_label}",
        )
    except ImproperlyConfigured:
        resolved = None
    if resolved is None:
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} is invalid"
        )
    if plaintext_serialized != resolved.serialized:
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} "
            + ("decrypted payload is invalid" if encrypted else "is not canonical")
        )
    if not hmac.compare_digest(stored_digest, resolved.digest):
        raise RuntimeEnvSnapshotError(
            f"django-ray: Persisted RuntimeEnv snapshot{task_label} hash does not match"
        )
    return resolved


def prepare_runtime_env_for_ray_core(
    runtime_env: ResolvedRuntimeEnv,
) -> dict[str, Any]:
    """Upload local code paths and return a per-task-compatible RuntimeEnv.

    Ray only accepts local ``working_dir`` and ``py_modules`` paths at job
    initialization. Ray Core task options require URIs, so direct Ray drivers
    upload local paths to Ray's content-addressed GCS package store first.
    """
    spec = deepcopy(runtime_env.spec)
    if not _contains_local_code_path(spec):
        return spec

    try:
        from ray.util.client import ray as ray_client

        if ray_client.is_connected():
            raise ImproperlyConfigured(
                "django-ray: Per-task RuntimeEnv local paths require a direct Ray "
                "connection. Use a GCS/HTTPS/S3 URI or connect the task manager "
                "to the cluster's GCS address instead of ray:// Ray Client."
            )

        from ray._private.runtime_env.py_modules import upload_py_modules_if_needed
        from ray._private.runtime_env.working_dir import upload_working_dir_if_needed

        with tempfile.TemporaryDirectory(prefix="django-ray-runtime-env-") as scratch_dir:
            spec = upload_working_dir_if_needed(
                spec,
                include_gitignore=True,
                scratch_dir=scratch_dir,
            )
            spec = upload_py_modules_if_needed(
                spec,
                include_gitignore=True,
                scratch_dir=scratch_dir,
            )
    except ImproperlyConfigured:
        raise
    except Exception as error:
        raise ImproperlyConfigured(
            f"django-ray: Failed to package local RuntimeEnv paths: {error}"
        ) from error
    return spec


def _ray_runtime_env_default_excludes() -> list[str]:
    """Return the default package exclusions supported by the installed Ray."""
    from ray._private import ray_constants

    get_default_excludes = getattr(
        ray_constants,
        "get_runtime_env_default_excludes",
        None,
    )
    if get_default_excludes is None:
        return []
    # The Ray getter evaluates its environment override on each invocation.
    return list(get_default_excludes())


@contextmanager
def snapshot_local_runtime_env(
    runtime_env: ResolvedRuntimeEnv,
) -> Iterator[ResolvedRuntimeEnv]:
    """Yield an immutable temporary snapshot for every local code path.

    Directory inputs are packaged with Ray's ignore/exclude semantics and then
    re-extracted into private temporary directories. File inputs are copied
    under their original filename. The caller keeps this context open until Ray
    has uploaded or accepted the RuntimeEnv.
    """
    if not _contains_local_code_path(runtime_env.spec):
        yield runtime_env
        return

    from ray._private.ray_constants import RAY_RUNTIME_ENV_IGNORE_GITIGNORE
    from ray._private.runtime_env.packaging import create_package, unzip_package

    include_gitignore = os.environ.get(RAY_RUNTIME_ENV_IGNORE_GITIGNORE, "0") != "1"
    spec = deepcopy(runtime_env.spec)
    excludes = spec.get("excludes")
    if excludes is not None and (
        not isinstance(excludes, list) or any(not isinstance(item, str) for item in excludes)
    ):
        raise ImproperlyConfigured(
            "django-ray: RuntimeEnv excludes must be a list of string patterns"
        )

    with tempfile.TemporaryDirectory(prefix="django-ray-runtime-env-snapshot-") as scratch:
        scratch_path = Path(scratch)

        def snapshot_path(
            value: str,
            *,
            label: str,
            include_parent_dir: bool,
            package_excludes: list[str] | None,
            preserve_directory: bool,
        ) -> str:
            source = Path(value)
            if not source.exists():
                return value
            if source.is_dir():
                target = scratch_path / f"{label}.zip"
                create_package(
                    str(source),
                    target,
                    include_gitignore=include_gitignore,
                    include_parent_dir=include_parent_dir,
                    excludes=package_excludes,
                )
                if preserve_directory:
                    extracted = scratch_path / f"{label}-directory"
                    unzip_package(
                        str(target),
                        str(extracted),
                        remove_top_level_directory=False,
                        unlink_zip=False,
                    )
                    snapshotted_directory = (
                        extracted / source.name if include_parent_dir else extracted
                    )
                    snapshotted_directory.mkdir(exist_ok=True)
                    return str(snapshotted_directory)
                return str(target)
            target_directory = scratch_path / label
            target_directory.mkdir()
            target = target_directory / source.name
            shutil.copy2(source, target)
            return str(target)

        working_dir = spec.get("working_dir")
        if isinstance(working_dir, str):
            working_dir_excludes = _ray_runtime_env_default_excludes() + list(excludes or [])
            spec["working_dir"] = snapshot_path(
                working_dir,
                label="working-dir",
                include_parent_dir=False,
                package_excludes=working_dir_excludes,
                preserve_directory=True,
            )
        py_modules = spec.get("py_modules")
        if isinstance(py_modules, list):
            spec["py_modules"] = [
                snapshot_path(
                    module,
                    label=f"py-module-{index}",
                    include_parent_dir=True,
                    package_excludes=excludes,
                    preserve_directory=True,
                )
                for index, module in enumerate(py_modules)
            ]
        yield normalize_runtime_env(
            spec,
            profile=runtime_env.profile,
            source="snapshotted RuntimeEnv",
        )


def _contains_local_code_path(spec: dict[str, Any]) -> bool:
    working_dir = spec.get("working_dir")
    if isinstance(working_dir, str) and Path(working_dir).exists():
        return True
    py_modules = spec.get("py_modules", [])
    return isinstance(py_modules, list) and any(
        isinstance(module, str) and Path(module).exists() for module in py_modules
    )


__all__ = [
    "ResolvedRuntimeEnv",
    "RuntimeEnvSnapshotError",
    "RuntimeEnvStorageFields",
    "normalize_runtime_env",
    "prepare_runtime_env_for_ray_core",
    "resolve_runtime_env_profile",
    "runtime_env_for_execution",
    "runtime_env_for_storage",
    "snapshot_local_runtime_env",
    "validate_runtime_env_profiles",
]
