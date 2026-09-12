"""Pure producer intent for the current coordinated-upgrade cohort.

An intent binds the package and reviewed backend declaration before input
preparation. It is not a runner selection, cluster observation, or claim
authorization. Ordinary backends retain worker-selected Sync/Core/Jobs modes;
the first verified claim must separately persist its actual execution binding.

Admission compares a finite backend/endpoint/selection/trust declaration. The
separate task RuntimeEnv digest records the producer's original bounded logical
observation; it is not a manager eligibility key or proof of imported source.
This module does not compare observations made on different hosts, require a
reusable identity, or change ordinary RuntimeEnv integrity/snapshot checks.
Addresses are hashed exactly as declared, without resolution or normalization.
These integrity digests do not authenticate a producer or a remote executor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Never
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

from django_ray.execution_protocol import COHORT_EXECUTION_PROTOCOL_VERSION
from django_ray.runtime_env_transport import (
    _canonical_bytes,
    _domain_digest,
    normalize_runtime_env_trust_identity,
)

COHORT_INTENT_SCHEMA = "django-ray.cohort-intent"
COHORT_INTENT_SCHEMA_VERSION = 2
COHORT_INTENT_EXECUTION_PROTOCOL_VERSION = COHORT_EXECUTION_PROTOCOL_VERSION
COHORT_INTENT_MAX_BYTES = 2048

_INTENT_DOMAIN = b"django-ray/cohort-intent/v3/schema2\x00"
_DECLARATION_DOMAIN = b"django-ray/cohort-execution-declaration/v2\x00"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "execution_protocol_version",
        "package_version",
        "backend_alias",
        "configuration_digest",
        "runtime_env_identity_digest",
        "selection_policy",
    }
)


class CohortSelectionPolicy(StrEnum):
    WORKER_SELECTED = "worker_selected"
    JOBS_ONLY = "jobs_only"


@dataclass(frozen=True, slots=True)
class CohortExecutionDeclaration:
    """Finite admission inputs owned by current backend configuration.

    Task RuntimeEnv values and logical identities do not belong here. Current
    trust uses the existing optional trust mapping; no revision is required.
    No worker CLI mode is present because ordinary producers do not select it.
    """

    backend_alias: str
    ray_address: str = field(repr=False)
    ray_job_only: bool
    trust_identity: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class CohortIntent:
    package_version: str
    backend_alias: str
    configuration_digest: str
    runtime_env_identity_digest: str
    selection_policy: CohortSelectionPolicy


class CohortIntentRejection(StrEnum):
    INVALID = "invalid"
    RESOURCE_LIMIT = "resource_limit"
    NONCANONICAL = "noncanonical"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    DIGEST_MISMATCH = "digest_mismatch"


class CohortIntentError(ValueError):
    """Fixed classifications without echoing configuration or encoded input."""

    def __init__(self, classification: CohortIntentRejection) -> None:
        self.classification = classification
        super().__init__(f"Cohort intent rejected: {classification.value}")


class CohortIntentMismatch(StrEnum):
    PACKAGE_VERSION = "package_version_mismatch"
    BACKEND_ALIAS = "backend_alias_mismatch"
    SELECTION_POLICY = "selection_policy_mismatch"
    CONFIGURATION = "configuration_mismatch"
    RUNTIME_ENV_OBSERVATION = "runtime_env_observation_mismatch"


def _reject(reason: CohortIntentRejection) -> Never:
    raise CohortIntentError(reason) from None


def _text(value: object, *, limit: int) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= limit
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        _reject(CohortIntentRejection.INVALID)
    return value


def _package_version(value: object) -> str:
    value = _text(value, limit=128)
    try:
        canonical = str(Version(value))
    except InvalidVersion:
        _reject(CohortIntentRejection.INVALID)
    if canonical != value:
        _reject(CohortIntentRejection.NONCANONICAL)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _reject(CohortIntentRejection.INVALID)
    return value


def _endpoint(value: object) -> str:
    value = _text(value, limit=255)
    # URL userinfo/query/fragment can carry credentials. Accept only a plain
    # endpoint; token authentication is supplied independently by the owner.
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            _reject(CohortIntentRejection.INVALID)
    except ValueError:
        _reject(CohortIntentRejection.INVALID)
    return value


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _trust_identity_digest(trust_identity: Mapping[str, str]) -> str:
    try:
        normalized = normalize_runtime_env_trust_identity(trust_identity)
        return _domain_digest(b"django-ray.workflow-plan-trust-v1\0", _canonical_bytes(normalized))
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _reject(CohortIntentRejection.INVALID)


def cohort_declaration_digest(declaration: CohortExecutionDeclaration) -> str:
    """Digest finite admission fields; retain endpoint spelling and current trust."""
    if (
        type(declaration) is not CohortExecutionDeclaration
        or type(declaration.ray_job_only) is not bool
    ):
        _reject(CohortIntentRejection.INVALID)
    body = {
        "backend_alias": _text(declaration.backend_alias, limit=128),
        "ray_address": _endpoint(declaration.ray_address),
        "ray_job_only": declaration.ray_job_only,
        "trust_identity_digest": _trust_identity_digest(declaration.trust_identity),
    }
    return "sha256:" + hashlib.sha256(_DECLARATION_DOMAIN + _canonical(body).encode()).hexdigest()


def build_cohort_intent(
    declaration: CohortExecutionDeclaration,
    *,
    package_version: str,
    runtime_env_identity_digest: str,
) -> CohortIntent:
    """Bind admission and the original task observation before input preparation.

    The logical digest is supplied independently by the producer. No RuntimeEnv
    contents, local files, current observations or reusability flags are read.
    """
    configuration_digest = cohort_declaration_digest(declaration)
    return CohortIntent(
        package_version=_package_version(package_version),
        backend_alias=declaration.backend_alias,
        configuration_digest=configuration_digest,
        runtime_env_identity_digest=_digest(runtime_env_identity_digest),
        selection_policy=(
            CohortSelectionPolicy.JOBS_ONLY
            if declaration.ray_job_only
            else CohortSelectionPolicy.WORKER_SELECTED
        ),
    )


def _wire(intent: CohortIntent) -> dict[str, object]:
    if (
        type(intent) is not CohortIntent
        or type(intent.selection_policy) is not CohortSelectionPolicy
    ):
        _reject(CohortIntentRejection.INVALID)
    return {
        "schema": COHORT_INTENT_SCHEMA,
        "schema_version": COHORT_INTENT_SCHEMA_VERSION,
        "execution_protocol_version": COHORT_INTENT_EXECUTION_PROTOCOL_VERSION,
        "package_version": _package_version(intent.package_version),
        "backend_alias": _text(intent.backend_alias, limit=128),
        "configuration_digest": _digest(intent.configuration_digest),
        "runtime_env_identity_digest": _digest(intent.runtime_env_identity_digest),
        "selection_policy": intent.selection_policy.value,
    }


def _limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 0 < max_bytes <= COHORT_INTENT_MAX_BYTES:
        _reject(CohortIntentRejection.RESOURCE_LIMIT)


def encode_cohort_intent(intent: CohortIntent, *, max_bytes: int = COHORT_INTENT_MAX_BYTES) -> str:
    _limit(max_bytes)
    encoded = _canonical(_wire(intent))
    if len(encoded.encode()) > max_bytes:
        _reject(CohortIntentRejection.RESOURCE_LIMIT)
    return encoded


def cohort_intent_digest(intent: CohortIntent) -> str:
    return (
        "sha256:"
        + hashlib.sha256(_INTENT_DOMAIN + encode_cohort_intent(intent).encode()).hexdigest()
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject(CohortIntentRejection.INVALID)
        result[key] = value
    return result


def decode_cohort_intent(
    serialized: object,
    *,
    max_bytes: int = COHORT_INTENT_MAX_BYTES,
    expected_digest: str | None = None,
) -> CohortIntent:
    """Decode canonical intent; optional digest comes from independent storage."""
    _limit(max_bytes)
    if type(serialized) is not str:
        _reject(CohortIntentRejection.INVALID)
    try:
        if len(serialized) > max_bytes or len(serialized.encode()) > max_bytes:
            _reject(CohortIntentRejection.RESOURCE_LIMIT)
        value = json.loads(serialized, object_pairs_hook=_unique_object)
        if type(value) is not dict:
            _reject(CohortIntentRejection.INVALID)
        if (
            value.get("schema") != COHORT_INTENT_SCHEMA
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != COHORT_INTENT_SCHEMA_VERSION
            or type(value.get("execution_protocol_version")) is not int
            or value["execution_protocol_version"] != COHORT_INTENT_EXECUTION_PROTOCOL_VERSION
        ):
            _reject(CohortIntentRejection.UNSUPPORTED_SCHEMA)
        if set(value) != _KEYS:
            _reject(CohortIntentRejection.INVALID)
        intent = CohortIntent(
            package_version=value["package_version"],
            backend_alias=value["backend_alias"],
            configuration_digest=value["configuration_digest"],
            runtime_env_identity_digest=value["runtime_env_identity_digest"],
            selection_policy=CohortSelectionPolicy(value["selection_policy"]),
        )
        if encode_cohort_intent(intent, max_bytes=max_bytes) != serialized:
            _reject(CohortIntentRejection.NONCANONICAL)
        if expected_digest is not None and _digest(expected_digest) != cohort_intent_digest(intent):
            _reject(CohortIntentRejection.DIGEST_MISMATCH)
        return intent
    except CohortIntentError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _reject(CohortIntentRejection.INVALID)


def match_cohort_intent(
    intent: CohortIntent,
    declaration: CohortExecutionDeclaration,
    *,
    package_version: str,
    expected_runtime_env_identity_digest: str | None = None,
) -> CohortIntentMismatch | None:
    """Match admission, optionally checking the original task observation.

    Omitting the observation checks only manager admission. Supplying it checks
    an independently retained original digest, not new RuntimeEnv observations
    or whether unknown content has become observable on a different host.
    """
    encode_cohort_intent(intent)
    current = build_cohort_intent(
        declaration,
        package_version=package_version,
        runtime_env_identity_digest=(
            intent.runtime_env_identity_digest
            if expected_runtime_env_identity_digest is None
            else expected_runtime_env_identity_digest
        ),
    )
    for field_name, reason in (
        ("package_version", CohortIntentMismatch.PACKAGE_VERSION),
        ("backend_alias", CohortIntentMismatch.BACKEND_ALIAS),
        ("selection_policy", CohortIntentMismatch.SELECTION_POLICY),
        ("configuration_digest", CohortIntentMismatch.CONFIGURATION),
        ("runtime_env_identity_digest", CohortIntentMismatch.RUNTIME_ENV_OBSERVATION),
    ):
        if getattr(intent, field_name) != getattr(current, field_name):
            return reason
    return None
