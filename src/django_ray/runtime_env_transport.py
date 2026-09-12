"""Django-free normalization and validation of transported RuntimeEnv identity.

The workflow-plan API and pre-Django execution codecs share these exact bounds,
normalization rules, diagnostic checks, and digest domains. A structural check
with ``require_trust_match=False`` never establishes live worker trust.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

RUNTIME_ENV_PLAN_FORMAT = "django-ray.runtime-env-plan"
RUNTIME_ENV_PLAN_FORMAT_VERSION = 1
RUNTIME_ENV_TRANSPORT_DOMAIN_SEPARATOR = b"django-ray.runtime-env-plan-transport-v1\0"
MAX_RUNTIME_ENV_IDENTITY_BYTES = 16 * 1024
MAX_JSON_DEPTH = 16
MAX_MAPPING_ITEMS = 256
MAX_SEQUENCE_ITEMS = 1024
MAX_STRING_CHARS = 2048
MAX_RUNTIME_ENV_DIAGNOSTICS = 16
_RUNTIME_ENV_PATH = re.compile(r"^[A-Za-z0-9_.:*\[\]-]{1,512}$")
_TRUST_FIELDS = (
    "trust_domain",
    "credential_provider",
    "credential_profile",
    "credential_revision",
    "environment_revision",
    "scheduling_revision",
    "service_account_audience",
)


class WorkflowPlanValidationError(ValueError):
    """Raised before submission when a definition cannot be canonicalized safely."""

    __module__ = "django_ray.workflow.plans"


class RuntimeEnvTrustConfigurationError(ValueError):
    """Pure counterpart translated to ImproperlyConfigured by the plans API."""


def _normalize_json(value: Any, *, path: str, depth: int) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise WorkflowPlanValidationError(f"{path} exceeds maximum nesting depth {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowPlanValidationError(f"{path} must not contain a non-finite number")
        return _normalize_number(value)
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise WorkflowPlanValidationError(
                f"{path} string exceeds maximum length {MAX_STRING_CHARS}"
            )
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        if len(value) > MAX_MAPPING_ITEMS:
            raise WorkflowPlanValidationError(
                f"{path} has more than {MAX_MAPPING_ITEMS} mapping entries"
            )
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise WorkflowPlanValidationError(f"{path} mapping keys must be strings")
            key = _normalize_identifier(raw_key)
            if key in normalized:
                raise WorkflowPlanValidationError(
                    f"{path} has duplicate keys after Unicode normalization: {key!r}"
                )
            normalized[key] = _normalize_json(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        if len(value) > MAX_SEQUENCE_ITEMS:
            raise WorkflowPlanValidationError(
                f"{path} has more than {MAX_SEQUENCE_ITEMS} sequence entries"
            )
        return [
            _normalize_json(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise WorkflowPlanValidationError(
        f"{path} contains unsupported process-local value "
        f"{type(value).__module__}.{type(value).__name__}"
    )


def _normalize_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _normalize_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or len(normalized) > MAX_STRING_CHARS:
        raise WorkflowPlanValidationError("Plan identifiers must be non-empty and bounded")
    return normalized


def _canonical_bytes(value: Any) -> bytes:
    normalized = _normalize_json(value, path="$", depth=0)
    return _canonical_json(normalized).encode("utf-8")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _domain_digest(domain: bytes, value: bytes) -> str:
    return f"sha256:{hashlib.sha256(domain + value).hexdigest()}"


def normalize_runtime_env_trust_identity(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise RuntimeEnvTrustConfigurationError(
            "django-ray: WORKFLOW_PLAN_TRUST_IDENTITY must be a mapping"
        )
    unknown = set(value) - set(_TRUST_FIELDS)
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise RuntimeEnvTrustConfigurationError(
            "django-ray: WORKFLOW_PLAN_TRUST_IDENTITY has unsupported fields: " + fields
        )
    result: dict[str, str] = {}
    for key in _TRUST_FIELDS:
        field = value.get(key)
        if field is None:
            continue
        if not isinstance(field, str) or not field or len(field) > 256:
            raise RuntimeEnvTrustConfigurationError(
                f"django-ray: WORKFLOW_PLAN_TRUST_IDENTITY[{key!r}] must be a "
                "non-empty string of at most 256 characters"
            )
        result[key] = _normalize_identifier(field)
    return result


def validate_runtime_env_transport(
    value: Mapping[str, Any],
    *,
    trust_identity: Mapping[str, str] | None = None,
    require_trust_match: bool = True,
) -> dict[str, Any]:
    """Strictly reconstruct a transported identity at the worker boundary.

    ``require_trust_match=False`` is limited to transitive transport of an
    already-bound descriptive identity. It still enforces the exact schema and
    checksum; it does not attest the receiving worker's live trust material.
    """
    if type(require_trust_match) is not bool:
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv trust-match policy must be a boolean"
        )
    if not isinstance(value, Mapping):
        raise WorkflowPlanValidationError("Transported RuntimeEnv identity must be a mapping")
    normalized = _normalize_json(value, path="runtime_env", depth=0)
    expected_fields = {
        "plan_format",
        "plan_format_version",
        "profile",
        "digest",
        "reusable",
        "unresolved_paths",
        "total_unresolved_paths",
        "unresolved_paths_truncated",
        "retry_safe",
        "retry_unsafe_paths",
        "total_retry_unsafe_paths",
        "retry_unsafe_paths_truncated",
        "trust_digest",
        "transport_digest",
    }
    if set(normalized) != expected_fields:
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has an unsupported schema"
        )
    serialized = _canonical_bytes(normalized)
    if len(serialized) > MAX_RUNTIME_ENV_IDENTITY_BYTES:
        raise WorkflowPlanValidationError("Transported RuntimeEnv identity exceeds its byte limit")
    if (
        normalized["plan_format"] != RUNTIME_ENV_PLAN_FORMAT
        or normalized["plan_format_version"] != RUNTIME_ENV_PLAN_FORMAT_VERSION
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has an unsupported format version"
        )
    profile = normalized["profile"]
    if profile is not None and (
        not isinstance(profile, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", profile) is None
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has an invalid profile name"
        )
    for field in ("digest", "trust_digest", "transport_digest"):
        if (
            not isinstance(normalized[field], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", normalized[field]) is None
        ):
            raise WorkflowPlanValidationError(
                f"Transported RuntimeEnv identity has an invalid {field}"
            )
    paths = normalized["unresolved_paths"]
    if (
        not isinstance(paths, list)
        or len(paths) > MAX_RUNTIME_ENV_DIAGNOSTICS
        or any(
            not isinstance(path, str) or _RUNTIME_ENV_PATH.fullmatch(path) is None for path in paths
        )
        or paths != sorted(set(paths))
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has invalid unresolved paths"
        )
    total = normalized["total_unresolved_paths"]
    truncated = normalized["unresolved_paths_truncated"]
    reusable = normalized["reusable"]
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < len(paths)
        or not isinstance(truncated, bool)
        or truncated != (total > len(paths))
        or not isinstance(reusable, bool)
        or reusable != (total == 0)
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has inconsistent eligibility metadata"
        )
    retry_paths = normalized["retry_unsafe_paths"]
    if (
        not isinstance(retry_paths, list)
        or len(retry_paths) > MAX_RUNTIME_ENV_DIAGNOSTICS
        or any(
            not isinstance(path, str) or _RUNTIME_ENV_PATH.fullmatch(path) is None
            for path in retry_paths
        )
        or retry_paths != sorted(set(retry_paths))
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has invalid retry-unsafe paths"
        )
    retry_total = normalized["total_retry_unsafe_paths"]
    retry_truncated = normalized["retry_unsafe_paths_truncated"]
    retry_safe = normalized["retry_safe"]
    if (
        isinstance(retry_total, bool)
        or not isinstance(retry_total, int)
        or retry_total < len(retry_paths)
        or not isinstance(retry_truncated, bool)
        or retry_truncated != (retry_total > len(retry_paths))
        or not isinstance(retry_safe, bool)
        or retry_safe != (retry_total == 0)
        or retry_total > total
        or (reusable and not retry_safe)
    ):
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity has inconsistent retry-safety metadata"
        )
    if require_trust_match:
        trust = normalize_runtime_env_trust_identity(trust_identity or {})
        expected_trust_digest = _domain_digest(
            b"django-ray.workflow-plan-trust-v1\0",
            _canonical_bytes(trust),
        )
        if normalized["trust_digest"] != expected_trust_digest:
            raise WorkflowPlanValidationError(
                "Transported RuntimeEnv identity does not match the worker trust identity"
            )
    payload = dict(normalized)
    transported_digest = payload.pop("transport_digest")
    expected_transport_digest = _domain_digest(
        RUNTIME_ENV_TRANSPORT_DOMAIN_SEPARATOR,
        _canonical_bytes(payload),
    )
    if transported_digest != expected_transport_digest:
        raise WorkflowPlanValidationError(
            "Transported RuntimeEnv identity checksum does not match its payload"
        )
    return normalized
