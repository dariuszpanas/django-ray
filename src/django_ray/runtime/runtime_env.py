"""Runtime environment profiles and durable execution snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from django_ray.models import RayTaskExecution

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_APPENDABLE_FIELDS = {"excludes", "pip", "py_modules", "uv"}


@dataclass(frozen=True)
class ResolvedRuntimeEnv:
    """A JSON-safe RuntimeEnv with a stable content identity."""

    profile: str | None
    spec: dict[str, Any]
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
    except (TypeError, ValueError) as error:
        raise ImproperlyConfigured(
            f"django-ray: {source} must contain only JSON-serializable values: {error}"
        ) from error

    normalized = json.loads(serialized)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
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


def runtime_env_for_execution(task_execution: RayTaskExecution) -> ResolvedRuntimeEnv:
    """Load and verify the immutable RuntimeEnv snapshot on an execution."""
    profile = getattr(task_execution, "runtime_env_profile", None) or None
    stored_digest = getattr(task_execution, "runtime_env_hash", "")
    if not stored_digest and profile is None:
        # Migration 0002 backfills pre-0.3 rows with "{}" but cannot reconstruct
        # the RuntimeEnv that would previously have been resolved at submission.
        # No digest/profile is the durable legacy marker; new empty snapshots
        # still carry the SHA-256 digest of their canonical "{}" payload.
        return resolve_runtime_env_profile()

    serialized = getattr(task_execution, "runtime_env_json", None)
    if not serialized:
        return normalize_runtime_env({})

    try:
        spec = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as error:
        raise ImproperlyConfigured(
            f"django-ray: Task {task_execution.pk} has invalid runtime_env_json"
        ) from error

    resolved = normalize_runtime_env(
        spec,
        profile=profile,
        source=f"task {task_execution.pk} RuntimeEnv",
    )
    if stored_digest and stored_digest != resolved.digest:
        raise ImproperlyConfigured(
            f"django-ray: Task {task_execution.pk} RuntimeEnv snapshot hash does not match"
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
    "normalize_runtime_env",
    "prepare_runtime_env_for_ray_core",
    "resolve_runtime_env_profile",
    "runtime_env_for_execution",
    "validate_runtime_env_profiles",
]
