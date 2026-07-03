"""Runtime environment profiles and durable execution snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
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
        profile=getattr(task_execution, "runtime_env_profile", None) or None,
        source=f"task {task_execution.pk} RuntimeEnv",
    )
    stored_digest = getattr(task_execution, "runtime_env_hash", "")
    if stored_digest and stored_digest != resolved.digest:
        raise ImproperlyConfigured(
            f"django-ray: Task {task_execution.pk} RuntimeEnv snapshot hash does not match"
        )
    return resolved


__all__ = [
    "ResolvedRuntimeEnv",
    "normalize_runtime_env",
    "resolve_runtime_env_profile",
    "runtime_env_for_execution",
    "validate_runtime_env_profiles",
]
