"""Pure protocol-3 claim facts; integrity digests confer no execution authority.

The evidence row ID is deliberately absent from these facts. A later transport
binds that independently returned ID together with the fact digest. Sync has a
local interpreter observation and never invents Ray membership or a target.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Never
from urllib.parse import urlsplit

from django_ray.execution_codec import ExecutionIdentity, is_valid_execution_identity
from django_ray.target.attestation import RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS
from django_ray.target.cohort_contract import (
    CohortContractError,
    _digest,
    _package_version,
    _parse_timestamp,
    _positive,
    _timestamp,
)
from django_ray.target.cohort_intent import (
    CohortIntentError,
    _endpoint,
    cohort_intent_digest,
    decode_cohort_intent,
)

COHORT_CLAIM_SCHEMA_VERSION = 2
COHORT_BINDING_SCHEMA_VERSION = 2
COHORT_CLAIM_MAX_BYTES = 16 * 1024
_MAX = (1 << 63) - 1
_DOMAIN = b"django-ray/cohort-claim-facts/v3/schema2\x00"
_SNAPSHOT_DOMAIN = b"django-ray/cohort-task-runtime-env-snapshot/v3\x00"


class CohortRunnerFamily(StrEnum):
    SYNC = "sync"
    RAY_CORE = "ray_core"
    RAY_JOB = "ray_job"


class CohortClaimDisposition(StrEnum):
    OPEN = "OPEN"
    HELD = "HELD"
    RESOLVED = "RESOLVED"


class CohortHoldReason(StrEnum):
    PACKAGE_MISMATCH = "package_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    MEMBERSHIP_MISMATCH = "membership_mismatch"
    INVALID_COMPLETION = "invalid_completion"
    TRANSPORT_UNCERTAIN = "transport_uncertain"
    OWNER_LOST = "owner_lost"
    DISPATCH_UNCERTAIN = "dispatch_uncertain"
    CANCELLATION_UNCERTAIN = "cancellation_uncertain"


class CohortHoldBoundary(StrEnum):
    OUTER = "outer"
    NESTED = "nested"
    CONTROL = "control"


class CohortResolutionKind(StrEnum):
    APPLICATION_COMPLETED = "application_completed"
    VERIFIED_NOT_INVOKED = "verified_not_invoked"
    VERIFIED_CANCELLED = "verified_cancelled"


class CohortClaimError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid current-cohort claim")


@dataclass(frozen=True, slots=True)
class CohortPythonVersion:
    implementation: str
    major: int
    minor: int
    patch: int


@dataclass(frozen=True, slots=True)
class CohortManagerRuntime:
    package_version: str
    python: CohortPythonVersion
    ray_version: tuple[int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class CohortBindingSpec:
    runner_family: CohortRunnerFamily
    package_version: str
    target_policy_id: int | None = None
    sync_python: CohortPythonVersion | None = None


@dataclass(frozen=True, slots=True)
class CohortCapabilitySnapshot:
    capability_id: int
    schema_version: int
    revision: int
    advertised_at: datetime


@dataclass(frozen=True, slots=True)
class CohortJobQualificationProvenance:
    """Immutable endpoint audit facts, never independently a positive cache.

    Only an authenticated publisher result seeds manager qualification. Its
    endpoint proof may precede the current shared proof for the same target;
    neither that newer proof nor this locally constructible value renews it.
    """

    configuration_digest: str
    jobs_endpoint: str
    challenge_id: int
    request_revision: int
    consumed_challenge_revision: int
    challenge_issued_at: datetime
    challenge_expires_at: datetime
    consumed_at: datetime
    request_digest: str
    receipt_digest: str
    submission_id: str
    native_job_id: str
    entrypoint_digest: str
    submitted_control_runtime_env_digest: str
    endpoint_expectation_digest: str
    endpoint_attestation_digest: str
    endpoint_membership_digest: str
    endpoint_observed_at: datetime
    endpoint_expires_at: datetime
    receipt_received_at: datetime


_QUALIFICATION_TIMES = (
    "challenge_issued_at",
    "challenge_expires_at",
    "consumed_at",
    "endpoint_observed_at",
    "endpoint_expires_at",
    "receipt_received_at",
)


@dataclass(frozen=True, slots=True)
class CohortClaimFacts:
    identity: ExecutionIdentity
    binding_id: int
    binding: CohortBindingSpec
    manager: CohortManagerRuntime
    worker_lease_id: str
    worker_lease_hostname: str
    worker_lease_pid: int
    worker_lease_started_at: datetime
    intent_json: str
    intent_digest: str
    runtime_env_profile: str | None
    runtime_env_hash: str | None
    runtime_env_snapshot_digest: str
    claimed_at: datetime
    target_policy_id: int | None = None
    claim_attestation_id: int | None = None
    target_expectation_digest: str | None = None
    claim_attestation_digest: str | None = None
    capability: CohortCapabilitySnapshot | None = None
    job_qualification: CohortJobQualificationProvenance | None = None


def _reject() -> Never:
    raise CohortClaimError from None


def _text(value: object, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or "\x00" in value:
        _reject()
    try:
        value.encode("utf-8")
    except UnicodeError:
        _reject()
    return value


def validate_cohort_python(value: CohortPythonVersion) -> None:
    if type(value) is not CohortPythonVersion or (
        type(value.implementation) is not str
        or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value.implementation) is None
    ):
        _reject()
    for index, number in enumerate((value.major, value.minor, value.patch)):
        if type(number) is not int or not (1 if index == 0 else 0) <= number <= _MAX:
            _reject()


def validate_cohort_job_qualification(value: CohortJobQualificationProvenance) -> None:
    """Validate bounded snapshot structure without I/O or granting authority."""
    try:
        if type(value) is not CohortJobQualificationProvenance:
            _reject()
        for name in (
            "configuration_digest",
            "request_digest",
            "receipt_digest",
            "entrypoint_digest",
            "submitted_control_runtime_env_digest",
            "endpoint_expectation_digest",
            "endpoint_attestation_digest",
            "endpoint_membership_digest",
        ):
            _digest(getattr(value, name))
        endpoint = _endpoint(value.jobs_endpoint)
        if (
            not endpoint.startswith(("http://", "https://"))
            or urlsplit(endpoint).scheme not in {"http", "https"}
            or any(marker in endpoint for marker in ("?", "#", "%", "\\"))
        ):
            _reject()
        for name in ("challenge_id", "request_revision", "consumed_challenge_revision"):
            _positive(getattr(value, name))
        if value.consumed_challenge_revision != value.request_revision + 1:
            _reject()
        if (
            type(value.submission_id) is not str
            or value.submission_id
            != "django-ray-cohort-probe-" + value.request_digest.removeprefix("sha256:")
        ):
            _reject()
        if (
            type(value.native_job_id) is not str
            or re.fullmatch(r"[0-9a-f]{8}", value.native_job_id) is None
            or value.native_job_id == "ffffffff"
        ):
            _reject()
        for name in _QUALIFICATION_TIMES:
            _timestamp(getattr(value, name))
        if (
            not timedelta(0)
            < value.challenge_expires_at - value.challenge_issued_at
            <= timedelta(seconds=600)
            or not timedelta(0)
            < value.endpoint_expires_at - value.endpoint_observed_at
            <= timedelta(seconds=RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS)
            or not value.challenge_issued_at
            <= value.endpoint_observed_at
            <= value.receipt_received_at
            <= value.consumed_at
            or value.consumed_at >= min(value.challenge_expires_at, value.endpoint_expires_at)
        ):
            _reject()
    except (CohortContractError, CohortIntentError, TypeError, AttributeError, OverflowError):
        _reject()


def validate_cohort_binding(value: CohortBindingSpec) -> None:
    try:
        if (
            type(value) is not CohortBindingSpec
            or type(value.runner_family) is not CohortRunnerFamily
        ):
            _reject()
        _package_version(value.package_version)
        if value.runner_family is CohortRunnerFamily.SYNC:
            if value.target_policy_id is not None or value.sync_python is None:
                _reject()
            validate_cohort_python(value.sync_python)
        else:
            _positive(value.target_policy_id)
            if value.sync_python is not None:
                _reject()
    except CohortContractError:
        _reject()


def _wire(facts: CohortClaimFacts) -> dict[str, Any]:
    try:
        if type(facts) is not CohortClaimFacts or not is_valid_execution_identity(facts.identity):
            _reject()
        if facts.identity.attempt_number > (1 << 31) - 1:
            _reject()
        _positive(facts.identity.execution_generation)
        _positive(facts.binding_id)
        if facts.binding_id != facts.identity.task_execution_pk:
            _reject()
        validate_cohort_binding(facts.binding)
        manager = facts.manager
        if type(manager) is not CohortManagerRuntime:
            _reject()
        _package_version(manager.package_version)
        validate_cohort_python(manager.python)
        if manager.package_version != facts.binding.package_version:
            _reject()
        _text(facts.worker_lease_id, 255)
        _text(facts.worker_lease_hostname, 255)
        _positive(facts.worker_lease_pid)
        if facts.worker_lease_pid > (1 << 31) - 1:
            _reject()
        _timestamp(facts.worker_lease_started_at)
        _timestamp(facts.claimed_at)
        if facts.worker_lease_started_at > facts.claimed_at:
            _reject()
        intent = decode_cohort_intent(facts.intent_json)
        if cohort_intent_digest(intent) != facts.intent_digest or (
            intent.package_version != facts.binding.package_version
        ):
            _reject()
        if facts.runtime_env_profile is not None and facts.runtime_env_profile != "":
            _text(facts.runtime_env_profile, 100)
        if facts.runtime_env_hash is not None and (
            type(facts.runtime_env_hash) is not str
            or re.fullmatch(r"(?:[0-9a-f]{64})?", facts.runtime_env_hash) is None
        ):
            _reject()
        _digest(facts.runtime_env_snapshot_digest)
        if facts.binding.runner_family is CohortRunnerFamily.RAY_JOB:
            qualification = facts.job_qualification
            validate_cohort_job_qualification(qualification)
            assert qualification is not None
            if (
                qualification.configuration_digest != intent.configuration_digest
                or qualification.endpoint_expectation_digest != facts.target_expectation_digest
                or qualification.challenge_issued_at < facts.worker_lease_started_at
                or not qualification.consumed_at
                <= facts.claimed_at
                < min(qualification.challenge_expires_at, qualification.endpoint_expires_at)
            ):
                _reject()
        elif facts.job_qualification is not None:
            _reject()
        ray_fields = (
            facts.target_policy_id,
            facts.claim_attestation_id,
            facts.target_expectation_digest,
            facts.claim_attestation_digest,
            facts.capability,
        )
        if facts.binding.runner_family is CohortRunnerFamily.SYNC:
            if any(field is not None for field in ray_fields) or manager.ray_version is not None:
                _reject()
            if manager.python != facts.binding.sync_python:
                _reject()
            if intent.selection_policy.value == "jobs_only":
                _reject()
        else:
            if any(field is None for field in ray_fields):
                _reject()
            _positive(facts.target_policy_id)
            _positive(facts.claim_attestation_id)
            _digest(facts.target_expectation_digest)
            _digest(facts.claim_attestation_digest)
            if type(manager.ray_version) is not tuple or len(manager.ray_version) != 3:
                _reject()
            for index, number in enumerate(manager.ray_version):
                if type(number) is not int or not (1 if index == 0 else 0) <= number <= _MAX:
                    _reject()
            capability = facts.capability
            if (
                type(capability) is not CohortCapabilitySnapshot
                or type(capability.schema_version) is not int
                or capability.schema_version != 1
            ):
                _reject()
            _positive(capability.capability_id)
            _positive(capability.revision)
            _timestamp(capability.advertised_at)
            if not facts.worker_lease_started_at <= capability.advertised_at <= facts.claimed_at:
                _reject()
            if (
                intent.selection_policy.value == "jobs_only"
                and facts.binding.runner_family is not CohortRunnerFamily.RAY_JOB
            ):
                _reject()
        wire = asdict(facts)
        wire["schema"] = "django-ray.cohort-claim-facts"
        wire["schema_version"] = COHORT_CLAIM_SCHEMA_VERSION
        wire["execution_protocol_version"] = 3
        wire["worker_lease_started_at"] = _timestamp(facts.worker_lease_started_at)
        wire["claimed_at"] = _timestamp(facts.claimed_at)
        if facts.capability is not None:
            wire["capability"]["advertised_at"] = _timestamp(facts.capability.advertised_at)
        if facts.job_qualification is not None:
            for name in _QUALIFICATION_TIMES:
                wire["job_qualification"][name] = _timestamp(getattr(facts.job_qualification, name))
        return wire
    except (CohortContractError, CohortIntentError, TypeError, AttributeError, OverflowError):
        _reject()


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def encode_cohort_claim_facts(
    facts: CohortClaimFacts, *, max_bytes: int = COHORT_CLAIM_MAX_BYTES
) -> str:
    if type(max_bytes) is not int or not 1 <= max_bytes <= COHORT_CLAIM_MAX_BYTES:
        _reject()
    serialized = _canonical(_wire(facts))
    if len(serialized.encode("ascii")) > max_bytes:
        _reject()
    return serialized


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _reject()
        value[key] = item
    return value


def _number(value: str) -> int:
    if len(value) > 20:
        _reject()
    return int(value)


def _noninteger(_value: str) -> Never:
    _reject()


def decode_cohort_claim_facts(
    serialized: str, *, expected_digest: str | None = None, max_bytes: int = COHORT_CLAIM_MAX_BYTES
) -> CohortClaimFacts:
    if type(max_bytes) is not int or not 1 <= max_bytes <= COHORT_CLAIM_MAX_BYTES:
        _reject()
    if type(serialized) is not str or len(serialized) > max_bytes:
        _reject()
    try:
        if len(serialized.encode("utf-8")) > max_bytes:
            _reject()
        value = json.loads(
            serialized,
            object_pairs_hook=_object,
            parse_int=_number,
            parse_float=_noninteger,
            parse_constant=_noninteger,
        )
        if type(value) is not dict or value.pop("schema") != "django-ray.cohort-claim-facts":
            _reject()
        for name, expected in (
            ("schema_version", COHORT_CLAIM_SCHEMA_VERSION),
            ("execution_protocol_version", 3),
        ):
            actual = value.pop(name)
            if type(actual) is not int or actual != expected:
                _reject()
        value["identity"] = ExecutionIdentity(**value["identity"])
        binding = value["binding"]
        binding["runner_family"] = CohortRunnerFamily(binding["runner_family"])
        if binding["sync_python"] is not None:
            binding["sync_python"] = CohortPythonVersion(**binding["sync_python"])
        value["binding"] = CohortBindingSpec(**binding)
        manager = value["manager"]
        manager["python"] = CohortPythonVersion(**manager["python"])
        if manager["ray_version"] is not None:
            if type(manager["ray_version"]) is not list:
                _reject()
            manager["ray_version"] = tuple(manager["ray_version"])
        value["manager"] = CohortManagerRuntime(**manager)
        for name in ("worker_lease_started_at", "claimed_at"):
            value[name] = _parse_timestamp(value[name])
        if value["capability"] is not None:
            value["capability"]["advertised_at"] = _parse_timestamp(
                value["capability"]["advertised_at"]
            )
            value["capability"] = CohortCapabilitySnapshot(**value["capability"])
        if value["job_qualification"] is not None:
            for name in _QUALIFICATION_TIMES:
                value["job_qualification"][name] = _parse_timestamp(
                    value["job_qualification"][name]
                )
            value["job_qualification"] = CohortJobQualificationProvenance(
                **value["job_qualification"]
            )
        facts = CohortClaimFacts(**value)
        if encode_cohort_claim_facts(facts, max_bytes=max_bytes) != serialized:
            _reject()
        if expected_digest is not None and _digest(expected_digest) != cohort_claim_facts_digest(
            facts
        ):
            _reject()
        return facts
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
        UnicodeError,
    ):
        _reject()


def cohort_claim_facts_digest(facts: CohortClaimFacts) -> str:
    return (
        "sha256:"
        + hashlib.sha256(_DOMAIN + encode_cohort_claim_facts(facts).encode("ascii")).hexdigest()
    )


def cohort_task_runtime_env_snapshot_digest(
    *, profile: str | None, serialized: str | None, digest: str | None
) -> str:
    """Bind stored bytes without interpreting, decrypting or resolving their contents."""
    hasher = hashlib.sha256(_SNAPSHOT_DOMAIN)
    # Existing RuntimeEnv storage has no raw-byte cap. Frame the three exact
    # strings by type and Unicode-codepoint count, then feed UTF-8 chunks. This
    # adds neither a storage restriction nor a second full encoded allocation.
    try:
        for name, value, maximum in (
            ("profile", profile, 100),
            ("serialized", serialized, None),
            ("digest", digest, 64),
        ):
            hasher.update(name.encode("ascii") + b"\x00")
            if value is None:
                hasher.update(b"N")
                continue
            if type(value) is not str or maximum is not None and len(value) > maximum:
                _reject()
            hasher.update(b"S" + len(value).to_bytes(8, "big"))
            for offset in range(0, len(value), 16 * 1024):
                hasher.update(value[offset : offset + 16 * 1024].encode("utf-8"))
    except (UnicodeError, OverflowError):
        _reject()
    return "sha256:" + hasher.hexdigest()
