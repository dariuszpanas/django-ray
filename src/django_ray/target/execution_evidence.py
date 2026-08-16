"""Canonical protocol-2 target execution claim evidence.

The database row ID is deliberately outside this digest. Transport carries
that positive ID separately, while the remote observation proof binds both the
ID and the claim digest. This module has no Django or Ray dependency so the
same strict codec can be used at claim, persistence, and transport boundaries.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Never

from django_ray.execution_protocol import TARGET_EXECUTION_PROTOCOL_VERSION

__all__ = [
    "RAY_TASK_TARGET_EXECUTION_EVIDENCE_MAX_BYTES",
    "RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA",
    "RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA_VERSION",
    "RAY_TASK_TARGET_EXECUTION_PROTOCOL_VERSION",
    "RayTaskTargetExecutionEvidenceClaim",
    "RayTaskTargetExecutionEvidenceError",
    "decode_ray_task_target_execution_evidence",
    "encode_ray_task_target_execution_evidence",
    "ray_task_target_execution_evidence_digest",
]

RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA = "django-ray.ray-task-target-execution-evidence"
RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA_VERSION = 1
RAY_TASK_TARGET_EXECUTION_PROTOCOL_VERSION = TARGET_EXECUTION_PROTOCOL_VERSION
RAY_TASK_TARGET_EXECUTION_EVIDENCE_MAX_BYTES = 32 * 1024

_MAX_COUNTER = (1 << 63) - 1
_MAX_POSITIVE_INTEGER = (1 << 31) - 1
_DOMAIN = b"django-ray/ray-task-target-execution-evidence/v1\x00"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TARGET_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_BACKEND_ALIAS = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_PYTHON_IMPLEMENTATION = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_RUNNER_FAMILIES = frozenset({"ray_core"})
_WIRE_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "execution_protocol_version",
        "execution_id",
        "task_id",
        "attempt_number",
        "execution_generation",
        "route_selection_id",
        "route_backend_alias",
        "route_revision_id",
        "route_revision",
        "selected_target_policy_id",
        "target_id",
        "target_policy_id",
        "claim_attestation_id",
        "target_expectation_digest",
        "claim_attestation_digest",
        "worker_target_capability_id",
        "worker_target_capability_schema_version",
        "worker_target_capability_revision",
        "worker_target_capability_advertised_at",
        "worker_lease_id",
        "worker_lease_hostname",
        "worker_lease_pid",
        "worker_lease_started_at",
        "runner_family",
        "manager_ray_major",
        "manager_ray_minor",
        "manager_ray_patch",
        "manager_python_implementation",
        "manager_python_major",
        "manager_python_minor",
        "manager_python_patch",
        "claimed_at",
    }
)


@dataclass(frozen=True, slots=True)
class RayTaskTargetExecutionEvidenceClaim:
    """Every immutable value covered by one generation claim digest."""

    execution_id: int
    task_id: str
    attempt_number: int
    execution_generation: int
    route_selection_id: int
    route_backend_alias: str
    route_revision_id: int
    route_revision: int
    selected_target_policy_id: int
    target_id: str
    target_policy_id: int
    claim_attestation_id: int
    target_expectation_digest: str
    claim_attestation_digest: str
    worker_target_capability_id: int
    worker_target_capability_schema_version: int
    worker_target_capability_revision: int
    worker_target_capability_advertised_at: datetime
    worker_lease_id: str
    worker_lease_hostname: str
    worker_lease_pid: int
    worker_lease_started_at: datetime
    runner_family: str
    manager_ray_major: int
    manager_ray_minor: int
    manager_ray_patch: int
    manager_python_implementation: str
    manager_python_major: int
    manager_python_minor: int
    manager_python_patch: int
    claimed_at: datetime
    schema_version: int = RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA_VERSION
    execution_protocol_version: int = RAY_TASK_TARGET_EXECUTION_PROTOCOL_VERSION


class RayTaskTargetExecutionEvidenceError(ValueError):
    """Reject malformed or noncanonical claim evidence without echoing it."""

    def __init__(self) -> None:
        super().__init__("Ray task target execution evidence is invalid")


class _Invalid(ValueError):  # noqa: N818 - private validation sentinel
    pass


class _DuplicateKey(ValueError):  # noqa: N818 - private parser sentinel
    pass


def _reject() -> Never:
    raise RayTaskTargetExecutionEvidenceError from None


def _counter(value: object, *, positive: bool = False, maximum: int = _MAX_COUNTER) -> int:
    if type(value) is not int:
        raise _Invalid
    minimum = 1 if positive else 0
    if value < minimum or value > maximum:
        raise _Invalid
    return value


def _text(value: object, *, maximum_bytes: int) -> str:
    if type(value) is not str or not value or unicodedata.normalize("NFC", value) != value:
        raise _Invalid
    if any(
        not character.isprintable() or unicodedata.category(character) == "Cs"
        for character in value
    ):
        raise _Invalid
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _Invalid from error
    if len(encoded) > maximum_bytes:
        raise _Invalid
    return value


def _identity_text(value: object, *, maximum_characters: int) -> str:
    if type(value) is not str or not value or len(value) > maximum_characters or "\x00" in value:
        raise _Invalid
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _Invalid from error
    return value


def _pattern(value: object, pattern: re.Pattern[str], *, maximum_bytes: int) -> str:
    normalized = _text(value, maximum_bytes=maximum_bytes)
    if pattern.fullmatch(normalized) is None:
        raise _Invalid
    return normalized


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _Invalid
    return value


def _utc_datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _Invalid
    normalized = value.astimezone(UTC)
    if not 1 <= normalized.year <= 9999:
        raise _Invalid
    return normalized


def _timestamp(value: datetime) -> str:
    return _utc_datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decode_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise _Invalid
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise _Invalid from error
    parsed = _utc_datetime(parsed)
    if _timestamp(parsed) != value:
        raise _Invalid
    return parsed


def _claim(value: object) -> RayTaskTargetExecutionEvidenceClaim:
    if type(value) is not RayTaskTargetExecutionEvidenceClaim:
        raise _Invalid
    execution_id = _counter(value.execution_id, positive=True)
    attempt_number = _counter(
        value.attempt_number,
        positive=True,
        maximum=_MAX_POSITIVE_INTEGER,
    )
    execution_generation = _counter(value.execution_generation, positive=True)
    route_selection_id = _counter(value.route_selection_id, positive=True)
    route_revision_id = _counter(value.route_revision_id, positive=True)
    route_revision = _counter(value.route_revision, positive=True)
    selected_target_policy_id = _counter(value.selected_target_policy_id, positive=True)
    target_policy_id = _counter(value.target_policy_id, positive=True)
    claim_attestation_id = _counter(value.claim_attestation_id, positive=True)
    capability_id = _counter(value.worker_target_capability_id, positive=True)
    capability_revision = _counter(value.worker_target_capability_revision, positive=True)
    lease_pid = _counter(
        value.worker_lease_pid,
        positive=True,
        maximum=_MAX_POSITIVE_INTEGER,
    )
    manager_ray_major = _counter(value.manager_ray_major, positive=True)
    manager_ray_minor = _counter(value.manager_ray_minor)
    manager_ray_patch = _counter(value.manager_ray_patch)
    manager_python_major = _counter(value.manager_python_major, positive=True)
    manager_python_minor = _counter(value.manager_python_minor)
    manager_python_patch = _counter(value.manager_python_patch)
    capability_advertised_at = _utc_datetime(value.worker_target_capability_advertised_at)
    lease_started_at = _utc_datetime(value.worker_lease_started_at)
    claimed_at = _utc_datetime(value.claimed_at)
    if lease_started_at > capability_advertised_at or capability_advertised_at > claimed_at:
        raise _Invalid
    if value.schema_version != RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA_VERSION:
        raise _Invalid
    if value.execution_protocol_version != RAY_TASK_TARGET_EXECUTION_PROTOCOL_VERSION:
        raise _Invalid
    if value.worker_target_capability_schema_version != 1:
        raise _Invalid
    runner_family = _text(value.runner_family, maximum_bytes=16)
    if runner_family not in _RUNNER_FAMILIES:
        raise _Invalid
    return RayTaskTargetExecutionEvidenceClaim(
        execution_id=execution_id,
        task_id=_identity_text(value.task_id, maximum_characters=255),
        attempt_number=attempt_number,
        execution_generation=execution_generation,
        route_selection_id=route_selection_id,
        route_backend_alias=_pattern(
            value.route_backend_alias,
            _BACKEND_ALIAS,
            maximum_bytes=128,
        ),
        route_revision_id=route_revision_id,
        route_revision=route_revision,
        selected_target_policy_id=selected_target_policy_id,
        target_id=_pattern(value.target_id, _TARGET_KEY, maximum_bytes=128),
        target_policy_id=target_policy_id,
        claim_attestation_id=claim_attestation_id,
        target_expectation_digest=_digest(value.target_expectation_digest),
        claim_attestation_digest=_digest(value.claim_attestation_digest),
        worker_target_capability_id=capability_id,
        worker_target_capability_schema_version=1,
        worker_target_capability_revision=capability_revision,
        worker_target_capability_advertised_at=capability_advertised_at,
        worker_lease_id=_identity_text(value.worker_lease_id, maximum_characters=255),
        worker_lease_hostname=_identity_text(
            value.worker_lease_hostname,
            maximum_characters=255,
        ),
        worker_lease_pid=lease_pid,
        worker_lease_started_at=lease_started_at,
        runner_family=runner_family,
        manager_ray_major=manager_ray_major,
        manager_ray_minor=manager_ray_minor,
        manager_ray_patch=manager_ray_patch,
        manager_python_implementation=_pattern(
            value.manager_python_implementation,
            _PYTHON_IMPLEMENTATION,
            maximum_bytes=64,
        ),
        manager_python_major=manager_python_major,
        manager_python_minor=manager_python_minor,
        manager_python_patch=manager_python_patch,
        claimed_at=claimed_at,
        schema_version=RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA_VERSION,
        execution_protocol_version=RAY_TASK_TARGET_EXECUTION_PROTOCOL_VERSION,
    )


def _wire(value: RayTaskTargetExecutionEvidenceClaim) -> dict[str, object]:
    value = _claim(value)
    return {
        "schema": RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA,
        "schema_version": value.schema_version,
        "execution_protocol_version": value.execution_protocol_version,
        "execution_id": value.execution_id,
        "task_id": value.task_id,
        "attempt_number": value.attempt_number,
        "execution_generation": value.execution_generation,
        "route_selection_id": value.route_selection_id,
        "route_backend_alias": value.route_backend_alias,
        "route_revision_id": value.route_revision_id,
        "route_revision": value.route_revision,
        "selected_target_policy_id": value.selected_target_policy_id,
        "target_id": value.target_id,
        "target_policy_id": value.target_policy_id,
        "claim_attestation_id": value.claim_attestation_id,
        "target_expectation_digest": value.target_expectation_digest,
        "claim_attestation_digest": value.claim_attestation_digest,
        "worker_target_capability_id": value.worker_target_capability_id,
        "worker_target_capability_schema_version": (value.worker_target_capability_schema_version),
        "worker_target_capability_revision": value.worker_target_capability_revision,
        "worker_target_capability_advertised_at": _timestamp(
            value.worker_target_capability_advertised_at
        ),
        "worker_lease_id": value.worker_lease_id,
        "worker_lease_hostname": value.worker_lease_hostname,
        "worker_lease_pid": value.worker_lease_pid,
        "worker_lease_started_at": _timestamp(value.worker_lease_started_at),
        "runner_family": value.runner_family,
        "manager_ray_major": value.manager_ray_major,
        "manager_ray_minor": value.manager_ray_minor,
        "manager_ray_patch": value.manager_ray_patch,
        "manager_python_implementation": value.manager_python_implementation,
        "manager_python_major": value.manager_python_major,
        "manager_python_minor": value.manager_python_minor,
        "manager_python_patch": value.manager_python_patch,
        "claimed_at": _timestamp(value.claimed_at),
    }


def encode_ray_task_target_execution_evidence(
    value: RayTaskTargetExecutionEvidenceClaim,
) -> str:
    """Encode one claim to its strict canonical JSON representation."""
    try:
        encoded = json.dumps(
            _wire(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > RAY_TASK_TARGET_EXECUTION_EVIDENCE_MAX_BYTES:
            raise _Invalid
        return encoded
    except RayTaskTargetExecutionEvidenceError:
        raise
    except Exception:
        _reject()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _constant(_value: str) -> Never:
    raise _Invalid


def decode_ray_task_target_execution_evidence(
    payload: str,
) -> RayTaskTargetExecutionEvidenceClaim:
    """Decode only the exact canonical protocol-2 evidence representation."""
    try:
        if type(payload) is not str:
            raise _Invalid
        raw = payload.encode("utf-8")
        if not raw or len(raw) > RAY_TASK_TARGET_EXECUTION_EVIDENCE_MAX_BYTES:
            raise _Invalid
        decoded = json.loads(
            payload,
            object_pairs_hook=_object,
            parse_constant=_constant,
        )
        if type(decoded) is not dict or frozenset(decoded) != _WIRE_KEYS:
            raise _Invalid
        if (
            decoded["schema"] != RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA
            or decoded["schema_version"] != RAY_TASK_TARGET_EXECUTION_EVIDENCE_SCHEMA_VERSION
            or decoded["execution_protocol_version"] != RAY_TASK_TARGET_EXECUTION_PROTOCOL_VERSION
        ):
            raise _Invalid
        value = _claim(
            RayTaskTargetExecutionEvidenceClaim(
                execution_id=decoded["execution_id"],
                task_id=decoded["task_id"],
                attempt_number=decoded["attempt_number"],
                execution_generation=decoded["execution_generation"],
                route_selection_id=decoded["route_selection_id"],
                route_backend_alias=decoded["route_backend_alias"],
                route_revision_id=decoded["route_revision_id"],
                route_revision=decoded["route_revision"],
                selected_target_policy_id=decoded["selected_target_policy_id"],
                target_id=decoded["target_id"],
                target_policy_id=decoded["target_policy_id"],
                claim_attestation_id=decoded["claim_attestation_id"],
                target_expectation_digest=decoded["target_expectation_digest"],
                claim_attestation_digest=decoded["claim_attestation_digest"],
                worker_target_capability_id=decoded["worker_target_capability_id"],
                worker_target_capability_schema_version=decoded[
                    "worker_target_capability_schema_version"
                ],
                worker_target_capability_revision=decoded["worker_target_capability_revision"],
                worker_target_capability_advertised_at=_decode_timestamp(
                    decoded["worker_target_capability_advertised_at"]
                ),
                worker_lease_id=decoded["worker_lease_id"],
                worker_lease_hostname=decoded["worker_lease_hostname"],
                worker_lease_pid=decoded["worker_lease_pid"],
                worker_lease_started_at=_decode_timestamp(decoded["worker_lease_started_at"]),
                runner_family=decoded["runner_family"],
                manager_ray_major=decoded["manager_ray_major"],
                manager_ray_minor=decoded["manager_ray_minor"],
                manager_ray_patch=decoded["manager_ray_patch"],
                manager_python_implementation=decoded["manager_python_implementation"],
                manager_python_major=decoded["manager_python_major"],
                manager_python_minor=decoded["manager_python_minor"],
                manager_python_patch=decoded["manager_python_patch"],
                claimed_at=_decode_timestamp(decoded["claimed_at"]),
                schema_version=decoded["schema_version"],
                execution_protocol_version=decoded["execution_protocol_version"],
            )
        )
        if encode_ray_task_target_execution_evidence(value) != payload:
            raise _Invalid
        return value
    except RayTaskTargetExecutionEvidenceError:
        raise
    except Exception:
        _reject()


def ray_task_target_execution_evidence_digest(
    value: RayTaskTargetExecutionEvidenceClaim,
) -> str:
    """Return the domain-separated digest of every immutable claim snapshot."""
    encoded = encode_ray_task_target_execution_evidence(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(_DOMAIN + encoded).hexdigest()}"
