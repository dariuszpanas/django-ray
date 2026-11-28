"""Settings validation and Django system checks for django-ray.

Kept separate from ``django_ray.conf.settings`` so runtime, storage, and
redaction modules can read ``get_settings()`` without creating circular import
dependencies back through settings validation.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags
from django.core.checks import Warning as CheckWarning
from django.core.exceptions import ImproperlyConfigured

from django_ray.conf.defaults import (
    DEFAULTS,
    QUEUE_TIMEOUT_SECONDS_MAX,
    TASK_ATTEMPT_ADMIN_MODES,
    WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES,
)


def _read_merged_settings() -> dict[str, Any]:
    """Read and merge ``settings.DJANGO_RAY`` with defaults without importing settings.py."""
    user_settings = getattr(settings, "DJANGO_RAY", {})
    if not isinstance(user_settings, Mapping):
        raise ImproperlyConfigured(
            f"django-ray: DJANGO_RAY setting must be a mapping, got {type(user_settings).__name__}"
        )
    return {**DEFAULTS, **user_settings}


def validate_ray_job_request_storage_settings(
    config: Mapping[str, Any] | None = None,
    *,
    _get_settings: Callable[[], dict[str, Any]] | None = None,
    _validate: Callable[..., None] | None = None,
) -> None:
    """Require a retrievable request store before a Ray Job worker advertises capacity."""
    loader = _get_settings or _read_merged_settings
    validator = _validate or validate_settings
    resolved = loader() if config is None else config
    validator(resolved, require_ray_address=True)
    if resolved.get("INPUT_STORAGE_BACKEND") is None:
        raise ImproperlyConfigured(
            "django-ray: INPUT_STORAGE_BACKEND must be configured for "
            "Ray Job request-reference transport"
        )


def validate_settings(
    config: Mapping[str, Any] | None = None,
    *,
    require_ray_address: bool = False,
    _get_settings: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """Validate django-ray settings.

    Args:
        config: Settings mapping to validate. If None, loads merged settings.
        require_ray_address: When True, require RAY_ADDRESS to be a non-empty
            string rather than allowing an explicit None (for lazy validation
            in --sync, manage.py check, or per-backend OPTIONS['RAY_ADDRESS']).

    Raises:
        ImproperlyConfigured: If settings are invalid.
    """
    if getattr(settings, "configured", False) and getattr(settings, "USE_TZ", True) is not True:
        raise ImproperlyConfigured(
            "django-ray: Django settings.USE_TZ must be True. Naive datetimes are not supported."
        )

    loader = _get_settings or _read_merged_settings
    if config is None:
        resolved_config = loader()
    elif not isinstance(config, Mapping):
        raise ImproperlyConfigured(
            f"django-ray: DJANGO_RAY setting must be a mapping, got {type(config).__name__}"
        )
    else:
        resolved_config = dict(config)

    config = resolved_config

    # Validate RAY_ADDRESS lazily by default so --sync, manage.py check, and
    # per-backend TASKS[*]['OPTIONS']['RAY_ADDRESS'] do not crash startup.
    ray_address = config["RAY_ADDRESS"] if "RAY_ADDRESS" in config else DEFAULTS["RAY_ADDRESS"]
    if ray_address is None:
        if require_ray_address:
            raise ImproperlyConfigured(
                "django-ray: RAY_ADDRESS is required and must be a non-empty string in "
                "DJANGO_RAY settings. Example: DJANGO_RAY = {'RAY_ADDRESS': 'auto'} "
                "or {'RAY_ADDRESS': 'http://ray-head:8265'}"
            )
    elif not isinstance(ray_address, str) or not ray_address.strip():
        raise ImproperlyConfigured(
            "django-ray: RAY_ADDRESS must be a non-empty string in DJANGO_RAY settings. "
            "Example: DJANGO_RAY = {'RAY_ADDRESS': 'auto'} "
            "or {'RAY_ADDRESS': 'http://ray-head:8265'}"
        )

    state_api_address = config.get("RAY_STATE_API_ADDRESS")
    if state_api_address is not None and (
        not isinstance(state_api_address, str) or not state_api_address.strip()
    ):
        raise ImproperlyConfigured(
            "django-ray: RAY_STATE_API_ADDRESS must be a non-empty string or None"
        )

    # Validate runner choice
    valid_runners = ("ray_job", "ray_core")
    runner = config.get("RUNNER", DEFAULTS["RUNNER"])
    if not isinstance(runner, str) or runner not in valid_runners:
        raise ImproperlyConfigured(
            f"django-ray: RUNNER must be one of {valid_runners}, got '{runner}'"
        )

    reporting_policy = config.get(
        "WORKFLOW_PROGRESS_REPORTING_POLICY",
        DEFAULTS["WORKFLOW_PROGRESS_REPORTING_POLICY"],
    )
    if (
        not isinstance(reporting_policy, str)
        or reporting_policy not in WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES
    ):
        valid_policies = tuple(sorted(WORKFLOW_PROGRESS_RUNTIME_REPORTING_POLICIES))
        raise ImproperlyConfigured(
            "django-ray: WORKFLOW_PROGRESS_REPORTING_POLICY must be one of "
            f"{valid_policies}, got {reporting_policy!r}"
        )

    if "WORKFLOW_PROGRESS_SCHEMA_V3_PILOT" in config:
        raise ImproperlyConfigured(
            "django-ray: WORKFLOW_PROGRESS_SCHEMA_V3_PILOT has been removed. "
            "Full reporting publishes bounded terminal graphs by default; "
            "configure WORKFLOW_PROGRESS_REPORTING_POLICY instead."
        )

    task_attempt_admin_mode = config.get(
        "TASK_ATTEMPT_ADMIN_MODE",
        DEFAULTS["TASK_ATTEMPT_ADMIN_MODE"],
    )
    if (
        not isinstance(task_attempt_admin_mode, str)
        or task_attempt_admin_mode not in TASK_ATTEMPT_ADMIN_MODES
    ):
        valid_modes = tuple(sorted(TASK_ATTEMPT_ADMIN_MODES))
        raise ImproperlyConfigured(
            "django-ray: TASK_ATTEMPT_ADMIN_MODE must be one of "
            f"{valid_modes}, got {task_attempt_admin_mode!r}"
        )

    from django_ray.runtime.runtime_env import validate_runtime_env_profiles
    from django_ray.runtime.runtime_env_encryption import (
        validate_runtime_env_encryption_settings,
    )

    validate_runtime_env_profiles(config)
    validate_runtime_env_encryption_settings(config)

    code_revision = config.get("WORKFLOW_PLAN_CODE_REVISION")
    if code_revision is not None and (
        not isinstance(code_revision, str) or not code_revision or len(code_revision) > 256
    ):
        raise ImproperlyConfigured(
            "django-ray: WORKFLOW_PLAN_CODE_REVISION must be None or a non-empty "
            "string of at most 256 characters"
        )

    trust_identity = config.get("WORKFLOW_PLAN_TRUST_IDENTITY", {})
    if not isinstance(trust_identity, dict):
        raise ImproperlyConfigured("django-ray: WORKFLOW_PLAN_TRUST_IDENTITY must be a mapping")
    allowed_trust_fields = {
        "trust_domain",
        "credential_provider",
        "credential_profile",
        "credential_revision",
        "environment_revision",
        "scheduling_revision",
        "service_account_audience",
    }
    unknown_trust_fields = set(trust_identity) - allowed_trust_fields
    if unknown_trust_fields:
        fields = ", ".join(sorted(str(field) for field in unknown_trust_fields))
        raise ImproperlyConfigured(
            "django-ray: WORKFLOW_PLAN_TRUST_IDENTITY has unsupported fields: " + fields
        )
    for name, value in trust_identity.items():
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ImproperlyConfigured(
                f"django-ray: WORKFLOW_PLAN_TRUST_IDENTITY[{name!r}] must be a "
                "non-empty string of at most 256 characters"
            )

    # Validate numeric settings
    numeric_settings = [
        ("DEFAULT_CONCURRENCY", 1, 1000),
        ("MAX_TASK_ATTEMPTS", 1, 100),
        ("RETRY_BACKOFF_SECONDS", 0, 3600),
        ("STUCK_TASK_TIMEOUT_SECONDS", 30, 86400),
        ("WORKER_LEASE_SECONDS", 1, 86400),
        ("WORKER_HEARTBEAT_SECONDS", 1, 86400),
        ("TASK_MONITOR_HEARTBEAT_SECONDS", 1, 300),
        ("WORKFLOW_PROGRESS_FLUSH_SECONDS", 1, 300),
        ("WORKFLOW_PROGRESS_TERMINAL_FLUSH_TIMEOUT_SECONDS", 1, 60),
        ("WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS", 0, 30),
        ("RAY_STATE_API_TIMEOUT_SECONDS", 1, 60),
        ("MAX_RESULT_SIZE_BYTES", 1024, 100 * 1024 * 1024),
        ("TRACING_MAX_WORKFLOW_NODE_SPANS", 0, 256),
    ]

    queue_timeout = config.get("QUEUE_TIMEOUT_SECONDS")
    if queue_timeout is not None and (
        type(queue_timeout) is not int
        or queue_timeout <= 0
        or queue_timeout > QUEUE_TIMEOUT_SECONDS_MAX
    ):
        raise ImproperlyConfigured(
            "django-ray: QUEUE_TIMEOUT_SECONDS must be None or an integer between "
            f"1 and {QUEUE_TIMEOUT_SECONDS_MAX}"
        )

    for name, min_val, max_val in numeric_settings:
        if name not in config:
            continue
        value = config[name]
        if type(value) is not int or value < min_val or value > max_val:
            raise ImproperlyConfigured(
                f"django-ray: {name} must be an integer between {min_val} and {max_val}"
            )

    max_inline_input_size = config.get("MAX_INLINE_INPUT_SIZE_BYTES")
    if max_inline_input_size is not None and (
        type(max_inline_input_size) is not int
        or max_inline_input_size < 1024
        or max_inline_input_size > 100 * 1024 * 1024
    ):
        raise ImproperlyConfigured(
            "django-ray: MAX_INLINE_INPUT_SIZE_BYTES must be None or an integer "
            "between 1024 and 104857600"
        )

    polling_settings = [
        ("WORKER_POLL_INTERVAL_SECONDS", 0.01, 10.0),
        ("WORKER_POLL_MAX_INTERVAL_SECONDS", 0.01, 60.0),
        ("WORKER_CANCELLATION_INTERVAL_SECONDS", 0.1, 300.0),
        ("METRICS_CACHE_TTL_SECONDS", 0.0, 300.0),
        ("TRACING_SAMPLE_RATE", 0.0, 1.0),
    ]
    for name, min_val, max_val in polling_settings:
        if name not in config:
            continue
        value = config[name]
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < min_val
            or value > max_val
        ):
            raise ImproperlyConfigured(
                f"django-ray: {name} must be a finite number between {min_val} and {max_val}"
            )

    if "TRACING_ENABLED" in config and type(config["TRACING_ENABLED"]) is not bool:
        raise ImproperlyConfigured("django-ray: TRACING_ENABLED must be a boolean")

    if (
        "REQUIRE_STRICT_NESTED_REQUEST" in config
        and type(config["REQUIRE_STRICT_NESTED_REQUEST"]) is not bool
    ):
        raise ImproperlyConfigured("django-ray: REQUIRE_STRICT_NESTED_REQUEST must be a boolean")

    allowed_callable_modules = config.get("ALLOWED_CALLABLE_MODULES")
    if allowed_callable_modules is not None:
        if isinstance(allowed_callable_modules, str | bytes | bytearray) or not isinstance(
            allowed_callable_modules, Sequence | set | frozenset
        ):
            raise ImproperlyConfigured(
                "django-ray: ALLOWED_CALLABLE_MODULES must be None or a sequence of module name strings"
            )
        for entry in allowed_callable_modules:
            if (
                not isinstance(entry, str)
                or not entry
                or len(entry) > 256
                or entry != entry.strip()
                or not all(part.isidentifier() for part in entry.split("."))
            ):
                raise ImproperlyConfigured(
                    f"django-ray: ALLOWED_CALLABLE_MODULES contains invalid module name {entry!r}"
                )

    allowed_ray_addresses = config.get("ALLOWED_RAY_ADDRESSES")
    if allowed_ray_addresses is not None:
        if isinstance(allowed_ray_addresses, str | bytes | bytearray) or not isinstance(
            allowed_ray_addresses, Sequence | set | frozenset
        ):
            raise ImproperlyConfigured(
                "django-ray: ALLOWED_RAY_ADDRESSES must be None or a sequence of Ray address strings"
            )
        for entry in allowed_ray_addresses:
            if (
                not isinstance(entry, str)
                or not entry
                or len(entry) > 2048
                or entry != entry.strip()
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in entry)
            ):
                raise ImproperlyConfigured(
                    f"django-ray: ALLOWED_RAY_ADDRESSES contains invalid Ray address {entry!r}"
                )
            if "://" in entry:
                scheme = entry.split("://", 1)[0].lower()
                if scheme not in ("http", "https", "ray"):
                    raise ImproperlyConfigured(
                        f"django-ray: ALLOWED_RAY_ADDRESSES entry {entry!r} uses unsupported scheme"
                    )

    poll_interval = config.get(
        "WORKER_POLL_INTERVAL_SECONDS", DEFAULTS["WORKER_POLL_INTERVAL_SECONDS"]
    )
    poll_max_interval = config.get(
        "WORKER_POLL_MAX_INTERVAL_SECONDS",
        DEFAULTS["WORKER_POLL_MAX_INTERVAL_SECONDS"],
    )
    if poll_max_interval < poll_interval:
        raise ImproperlyConfigured(
            "django-ray: WORKER_POLL_MAX_INTERVAL_SECONDS must be greater than or equal to "
            "WORKER_POLL_INTERVAL_SECONDS"
        )

    # Heartbeats must occur before the lease or stuck-task windows expire. Use
    # defaults for omitted values so partial test/config dictionaries receive
    # the same relationship checks as the merged runtime settings.
    worker_lease_seconds = config.get("WORKER_LEASE_SECONDS", DEFAULTS["WORKER_LEASE_SECONDS"])
    worker_heartbeat_seconds = config.get(
        "WORKER_HEARTBEAT_SECONDS", DEFAULTS["WORKER_HEARTBEAT_SECONDS"]
    )
    if worker_heartbeat_seconds >= worker_lease_seconds:
        raise ImproperlyConfigured(
            "django-ray: WORKER_HEARTBEAT_SECONDS must be less than WORKER_LEASE_SECONDS"
        )

    stuck_timeout_seconds = config.get(
        "STUCK_TASK_TIMEOUT_SECONDS", DEFAULTS["STUCK_TASK_TIMEOUT_SECONDS"]
    )
    task_monitor_heartbeat_seconds = config.get(
        "TASK_MONITOR_HEARTBEAT_SECONDS", DEFAULTS["TASK_MONITOR_HEARTBEAT_SECONDS"]
    )
    if task_monitor_heartbeat_seconds >= stuck_timeout_seconds:
        raise ImproperlyConfigured(
            "django-ray: TASK_MONITOR_HEARTBEAT_SECONDS must be less than "
            "STUCK_TASK_TIMEOUT_SECONDS"
        )

    if "RETRY_BACKOFF_JITTER_RATIO" in config:
        jitter_ratio = config["RETRY_BACKOFF_JITTER_RATIO"]
        if (
            type(jitter_ratio) not in (int, float)
            or not math.isfinite(jitter_ratio)
            or jitter_ratio < 0.0
            or jitter_ratio > 1.0
        ):
            raise ImproperlyConfigured(
                "django-ray: RETRY_BACKOFF_JITTER_RATIO must be a finite number between 0.0 and 1.0"
            )

    denylist = config.get("RETRY_EXCEPTION_DENYLIST", [])
    if not isinstance(denylist, list) or any(not isinstance(entry, str) for entry in denylist):
        raise ImproperlyConfigured("django-ray: RETRY_EXCEPTION_DENYLIST must be a list of strings")

    result_storage_backend = config.get("RESULT_STORAGE_BACKEND", "digest")
    valid_result_storage_backends = ("digest", "filesystem", "s3", "gcs")
    if (
        not isinstance(result_storage_backend, str)
        or result_storage_backend not in valid_result_storage_backends
    ):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_BACKEND must be one of "
            f"{valid_result_storage_backends}, got '{result_storage_backend}'"
        )

    optional_string_settings = (
        "RESULT_STORAGE_FILESYSTEM_PATH",
        "RESULT_STORAGE_S3_BUCKET",
        "RESULT_STORAGE_S3_PREFIX",
        "RESULT_STORAGE_S3_REGION",
        "RESULT_STORAGE_S3_ENDPOINT_URL",
        "RESULT_STORAGE_GCS_BUCKET",
        "RESULT_STORAGE_GCS_PREFIX",
    )
    for name in optional_string_settings:
        value = config.get(name)
        if value is not None and not isinstance(value, str):
            raise ImproperlyConfigured(f"django-ray: {name} must be a string or None")

    if result_storage_backend == "filesystem" and not config.get("RESULT_STORAGE_FILESYSTEM_PATH"):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_FILESYSTEM_PATH is required when "
            "RESULT_STORAGE_BACKEND='filesystem'"
        )
    if result_storage_backend == "s3" and not config.get("RESULT_STORAGE_S3_BUCKET"):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_S3_BUCKET is required when RESULT_STORAGE_BACKEND='s3'"
        )
    if result_storage_backend == "gcs" and not config.get("RESULT_STORAGE_GCS_BUCKET"):
        raise ImproperlyConfigured(
            "django-ray: RESULT_STORAGE_GCS_BUCKET is required when RESULT_STORAGE_BACKEND='gcs'"
        )

    input_storage_backend = config.get("INPUT_STORAGE_BACKEND")
    valid_input_storage_backends = (None, "filesystem", "s3", "gcs")
    if input_storage_backend not in valid_input_storage_backends:
        raise ImproperlyConfigured(
            "django-ray: INPUT_STORAGE_BACKEND must be None or one of "
            "('filesystem', 's3', 'gcs'); digest-only storage cannot recover task inputs"
        )
    if max_inline_input_size is not None and input_storage_backend is None:
        raise ImproperlyConfigured(
            "django-ray: INPUT_STORAGE_BACKEND must be configured when "
            "MAX_INLINE_INPUT_SIZE_BYTES enables spillover"
        )

    input_string_settings = (
        "INPUT_STORAGE_FILESYSTEM_PATH",
        "INPUT_STORAGE_S3_BUCKET",
        "INPUT_STORAGE_S3_PREFIX",
        "INPUT_STORAGE_S3_REGION",
        "INPUT_STORAGE_S3_ENDPOINT_URL",
        "INPUT_STORAGE_GCS_BUCKET",
        "INPUT_STORAGE_GCS_PREFIX",
    )
    for name in input_string_settings:
        value = config.get(name)
        if value is not None and not isinstance(value, str):
            raise ImproperlyConfigured(f"django-ray: {name} must be a string or None")

    if input_storage_backend == "filesystem" and not config.get("INPUT_STORAGE_FILESYSTEM_PATH"):
        raise ImproperlyConfigured(
            "django-ray: INPUT_STORAGE_FILESYSTEM_PATH is required when "
            "INPUT_STORAGE_BACKEND='filesystem'"
        )
    if input_storage_backend == "s3" and not config.get("INPUT_STORAGE_S3_BUCKET"):
        raise ImproperlyConfigured(
            "django-ray: INPUT_STORAGE_S3_BUCKET is required when INPUT_STORAGE_BACKEND='s3'"
        )
    if input_storage_backend == "gcs" and not config.get("INPUT_STORAGE_GCS_BUCKET"):
        raise ImproperlyConfigured(
            "django-ray: INPUT_STORAGE_GCS_BUCKET is required when INPUT_STORAGE_BACKEND='gcs'"
        )

    # Storage references are durable protocol data. Validate both the active
    # writer and any retained reader namespaces during startup so an invalid
    # writer cannot silently fall back to digest-only references and historical
    # objects do not fail only after a task needs them.
    from django_ray.result_storage import (
        ResultStorageError,
        validate_storage_configuration,
    )

    storage_configuration_error: str | None = None
    try:
        validate_storage_configuration(config)
    except ResultStorageError as error:
        storage_configuration_error = str(error)
    if storage_configuration_error is not None:
        raise ImproperlyConfigured(f"django-ray: {storage_configuration_error}")

    redact_patterns = config.get("REDACT_PATTERNS")
    if redact_patterns is None:
        configured_redact_patterns: Sequence[str] | str = ()
    elif isinstance(redact_patterns, str):
        configured_redact_patterns = redact_patterns
    elif not isinstance(redact_patterns, Sequence) or isinstance(
        redact_patterns, bytes | bytearray
    ):
        raise ImproperlyConfigured(
            "django-ray: REDACT_PATTERNS must be a string or a sequence of pattern strings"
        )
    else:
        configured_redact_patterns = redact_patterns

    from django_ray.redaction import DEFAULT_REDACT_PATTERNS
    from django_ray.redaction_patterns import RedactionPatternError, compile_redaction_patterns

    redaction_error: str | None = None
    try:
        compile_redaction_patterns(
            configured_redact_patterns,
            builtin_patterns=DEFAULT_REDACT_PATTERNS,
        )
    except RedactionPatternError as error:
        redaction_error = str(error)
    if redaction_error is not None:
        raise ImproperlyConfigured(f"django-ray: REDACT_PATTERNS {redaction_error}")


def _runtime_env_has_env_vars(spec: object) -> bool:
    """Return True when a RuntimeEnv specification defines non-empty env_vars."""
    if not isinstance(spec, Mapping):
        return False
    env_vars = spec.get("env_vars")
    return isinstance(env_vars, Mapping) and len(env_vars) > 0


def check_django_ray_settings(
    app_configs: Sequence[Any] | None = None,
    *,
    _get_settings: Callable[[], dict[str, Any]] | None = None,
    _validate: Callable[[], None] | None = None,
    **kwargs: Any,
) -> list[CheckMessage]:
    """Django system checks for django-ray configuration safety and defaults."""
    del app_configs, kwargs
    messages: list[CheckMessage] = []
    loader = _get_settings or _read_merged_settings
    validator = _validate or validate_settings

    try:
        validator()
    except ImproperlyConfigured as error:
        return [
            Error(
                str(error),
                id="django_ray.E002",
            )
        ]

    config = loader()

    result_storage_backend = config.get(
        "RESULT_STORAGE_BACKEND", DEFAULTS["RESULT_STORAGE_BACKEND"]
    )
    if result_storage_backend == "digest":
        max_result_bytes = config.get("MAX_RESULT_SIZE_BYTES", DEFAULTS["MAX_RESULT_SIZE_BYTES"])
        messages.append(
            CheckWarning(
                "RESULT_STORAGE_BACKEND is 'digest'; task results larger than "
                f"MAX_RESULT_SIZE_BYTES ({max_result_bytes} bytes) will be stored as "
                "digest-only stubs and raise ResultUnavailableError when .return_value is read.",
                hint=(
                    "Configure RESULT_STORAGE_BACKEND to 'filesystem', 's3', or 'gcs' for "
                    "retrievable oversized results, or silence django_ray.W002 in "
                    "SILENCED_SYSTEM_CHECKS if digest-only stubs are intentional."
                ),
                id="django_ray.W002",
            )
        )

    storage_mode = config.get("RUNTIME_ENV_STORAGE_MODE", DEFAULTS["RUNTIME_ENV_STORAGE_MODE"])
    if storage_mode == "plaintext":
        plaintext_sources: list[str] = []
        if _runtime_env_has_env_vars(config.get("RAY_RUNTIME_ENV")):
            plaintext_sources.append("DJANGO_RAY['RAY_RUNTIME_ENV']")

        profiles = config.get("RUNTIME_ENV_PROFILES")
        if isinstance(profiles, Mapping):
            for profile_name, profile_spec in sorted(profiles.items()):
                if _runtime_env_has_env_vars(profile_spec):
                    plaintext_sources.append(
                        f"DJANGO_RAY['RUNTIME_ENV_PROFILES'][{profile_name!r}]"
                    )

        tasks_cfg = getattr(settings, "TASKS", None)
        if isinstance(tasks_cfg, Mapping):
            for alias, backend_cfg in sorted(tasks_cfg.items()):
                if not isinstance(backend_cfg, Mapping):
                    continue
                options = backend_cfg.get("OPTIONS")
                if isinstance(options, Mapping) and _runtime_env_has_env_vars(
                    options.get("RAY_RUNTIME_ENV")
                ):
                    plaintext_sources.append(f"TASKS[{alias!r}]['OPTIONS']['RAY_RUNTIME_ENV']")

        if plaintext_sources:
            sources_text = ", ".join(plaintext_sources)
            messages.append(
                CheckWarning(
                    "RUNTIME_ENV_STORAGE_MODE is 'plaintext' while RuntimeEnv configuration "
                    f"defines env_vars ({sources_text}); environment variable values will be "
                    "persisted unencrypted in RayTaskExecution.runtime_env_json.",
                    hint=(
                        "Set RUNTIME_ENV_STORAGE_MODE='encrypted' with "
                        "RUNTIME_ENV_ENCRYPTION_KEYS (or move secrets to pod/vault mounts), "
                        "or silence django_ray.W003 if env_vars contain no sensitive values."
                    ),
                    id="django_ray.W003",
                )
            )

    return messages


def register_system_checks(
    check_fn: Callable[..., list[CheckMessage]] | None = None,
) -> None:
    """Register django-ray configuration checks with Django's system check framework."""
    from django.core.checks import register

    register(Tags.compatibility, Tags.security)(check_fn or check_django_ray_settings)


__all__ = [
    "check_django_ray_settings",
    "register_system_checks",
    "validate_ray_job_request_storage_settings",
    "validate_settings",
]
