"""Strict, bounded contracts for dormant Ray target attestation.

This module deliberately has no Django or Ray imports.  A transport-specific
probe may collect raw facts, but only these canonical values cross the trust
boundary.  Resource-state versions are observation fences, not membership
epochs: they may advance while the schedulable node set remains unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Never

RAY_TARGET_EXPECTATION_SCHEMA = "django-ray.ray-target-expectation"
RAY_TARGET_EXPECTATION_SCHEMA_VERSION = 1
RAY_CLUSTER_ATTESTATION_SCHEMA = "django-ray.ray-cluster-attestation"
RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION = 1

RAY_TARGET_EXPECTATION_MAX_BYTES = 16 * 1024
RAY_CLUSTER_ATTESTATION_MAX_BYTES = 1024 * 1024
RAY_TARGET_ATTESTATION_MAX_NODES = 256
RAY_TARGET_ATTESTATION_MAX_TEXT_BYTES = 256
RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS = 3600
RAY_TARGET_ATTESTATION_MAX_COUNTER = (1 << 63) - 1
RAY_NODE_ID_HEX_CHARS = 56

_NODE_ID = re.compile(r"[0-9a-f]{56}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TARGET_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_CLUSTER_SESSION = re.compile(r"session_[A-Za-z0-9][A-Za-z0-9_.-]{0,247}")
_PYTHON_IMPLEMENTATION = re.compile(r"[a-z][a-z0-9_.-]{0,63}")

_EXPECTATION_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "target_key",
        "runner_family",
        "cluster_session",
        "policy_revision",
        "runtime",
    }
)
_RUNTIME_KEYS = frozenset(
    {
        "ray_major",
        "ray_minor",
        "ray_patch",
        "python_implementation",
        "python_major",
        "python_minor",
        "python_patch",
    }
)
_NODE_VERSION_KEYS = frozenset({"node_id", "node_state_version"})
_BOUNDARY_KEYS = frozenset(
    {
        "resource_state_version_before",
        "resource_state_version_after",
        "node_state_versions_before",
        "node_state_versions_after",
    }
)
_OBSERVATION_KEYS = frozenset({"node_id", "cluster_session", "runtime", "observation_digest"})
_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "expectation",
        "expectation_digest",
        "boundary",
        "membership_digest",
        "nodes",
        "observed_at",
        "expires_at",
        "attestation_digest",
    }
)

_EXPECTATION_DOMAIN = b"django-ray/ray-target-expectation/v1\x00"
_OBSERVATION_DOMAIN = b"django-ray/ray-node-observation/v1\x00"
_MEMBERSHIP_DOMAIN = b"django-ray/ray-membership/v1\x00"
_ATTESTATION_DOMAIN = b"django-ray/ray-cluster-attestation/v1\x00"


class RayRunnerFamily(StrEnum):
    """Supported Ray submission families."""

    RAY_CORE = "ray_core"
    RAY_JOB = "ray_job"


@dataclass(frozen=True, slots=True)
class RayRuntimeVersion:
    """Exact normalized Ray and Python runtime tuple."""

    ray_major: int
    ray_minor: int
    ray_patch: int
    python_implementation: str
    python_major: int
    python_minor: int
    python_patch: int


@dataclass(frozen=True, slots=True)
class RayTargetExpectation:
    """Trusted target identity that an attestation must echo exactly."""

    target_key: str
    runner_family: RayRunnerFamily
    cluster_session: str
    policy_revision: int
    runtime: RayRuntimeVersion


@dataclass(frozen=True, slots=True)
class RayNodeStateVersion:
    """One schedulable node's state version at an observation fence."""

    node_id: str
    node_state_version: int


@dataclass(frozen=True, slots=True)
class RayNodeObservation:
    """Runtime identity observed by code executing on one exact Ray node."""

    node_id: str
    cluster_session: str
    runtime: RayRuntimeVersion
    observation_digest: str


@dataclass(frozen=True, slots=True)
class RayObservationBoundary:
    """Before/after resource-state fences around per-node observation."""

    resource_state_version_before: int
    resource_state_version_after: int
    node_state_versions_before: tuple[RayNodeStateVersion, ...]
    node_state_versions_after: tuple[RayNodeStateVersion, ...]


@dataclass(frozen=True, slots=True)
class RayClusterAttestation:
    """Canonical result of observing every schedulable node in one boundary."""

    expectation: RayTargetExpectation
    expectation_digest: str
    boundary: RayObservationBoundary
    membership_digest: str
    nodes: tuple[RayNodeObservation, ...]
    observed_at: datetime
    expires_at: datetime
    attestation_digest: str


class RayTargetAttestationRejection(StrEnum):
    """Fixed, redaction-safe rejection classifications."""

    INVALID = "invalid"
    RESOURCE_LIMIT = "resource_limit"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    NONCANONICAL = "noncanonical"
    TARGET_KEY_MISMATCH = "target_key_mismatch"
    RUNNER_FAMILY_MISMATCH = "runner_family_mismatch"
    CLUSTER_SESSION_MISMATCH = "cluster_session_mismatch"
    POLICY_REVISION_MISMATCH = "policy_revision_mismatch"
    RAY_VERSION_MISMATCH = "ray_version_mismatch"
    PYTHON_IMPLEMENTATION_MISMATCH = "python_implementation_mismatch"
    PYTHON_VERSION_MISMATCH = "python_version_mismatch"
    EXPECTATION_DIGEST_MISMATCH = "expectation_digest_mismatch"
    OBSERVATION_DIGEST_MISMATCH = "observation_digest_mismatch"
    MEMBERSHIP_MISMATCH = "membership_mismatch"
    ATTESTATION_DIGEST_MISMATCH = "attestation_digest_mismatch"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"


class RayTargetAttestationError(ValueError):
    """Reject untrusted attestation data without retaining its contents."""

    def __init__(self, classification: RayTargetAttestationRejection) -> None:
        self.classification = classification
        super().__init__(f"Ray target attestation rejected: {classification.value}")


class RayTargetAttestationEncodeError(ValueError):
    """Report a fixed error when a local value cannot be encoded."""

    def __init__(self) -> None:
        super().__init__("Ray target attestation encoding failed")


class _Invalid(ValueError):  # noqa: N818 - private control-flow sentinel
    pass


class _ResourceLimit(ValueError):  # noqa: N818 - private control-flow sentinel
    pass


class _DuplicateKey(ValueError):  # noqa: N818 - private parser sentinel
    pass


def _reject(classification: RayTargetAttestationRejection) -> Never:
    raise RayTargetAttestationError(classification)


def _counter(value: object, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise _Invalid
    minimum = 1 if positive else 0
    if value < minimum:
        raise _Invalid
    if value > RAY_TARGET_ATTESTATION_MAX_COUNTER:
        raise _ResourceLimit
    return value


def _bounded_text(value: object, *, max_bytes: int = RAY_TARGET_ATTESTATION_MAX_TEXT_BYTES) -> str:
    if type(value) is not str or not value:
        raise _Invalid
    if any(
        not character.isprintable() or unicodedata.category(character) == "Cs"
        for character in value
    ):
        raise _Invalid
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _Invalid from error
    if size > max_bytes:
        raise _ResourceLimit
    return value


def _node_id(value: object) -> str:
    if type(value) is not str or _NODE_ID.fullmatch(value) is None:
        raise _Invalid
    return value


def _target_key(value: object) -> str:
    value = _bounded_text(value)
    if _TARGET_KEY.fullmatch(value) is None:
        raise _Invalid
    return value


def _cluster_session(value: object) -> str:
    value = _bounded_text(value)
    if _CLUSTER_SESSION.fullmatch(value) is None:
        raise _Invalid
    return value


def _digest_value(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _Invalid
    return value


def _runtime(value: object) -> RayRuntimeVersion:
    if type(value) is not RayRuntimeVersion:
        raise _Invalid
    ray_major = _counter(value.ray_major, positive=True)
    ray_minor = _counter(value.ray_minor)
    ray_patch = _counter(value.ray_patch)
    python_major = _counter(value.python_major, positive=True)
    python_minor = _counter(value.python_minor)
    python_patch = _counter(value.python_patch)
    implementation = _bounded_text(value.python_implementation, max_bytes=64)
    if _PYTHON_IMPLEMENTATION.fullmatch(implementation) is None:
        raise _Invalid
    return RayRuntimeVersion(
        ray_major=ray_major,
        ray_minor=ray_minor,
        ray_patch=ray_patch,
        python_implementation=implementation,
        python_major=python_major,
        python_minor=python_minor,
        python_patch=python_patch,
    )


def _runtime_wire(value: RayRuntimeVersion) -> dict[str, object]:
    value = _runtime(value)
    return {
        "ray_major": value.ray_major,
        "ray_minor": value.ray_minor,
        "ray_patch": value.ray_patch,
        "python_implementation": value.python_implementation,
        "python_major": value.python_major,
        "python_minor": value.python_minor,
        "python_patch": value.python_patch,
    }


def _decode_runtime(value: object) -> RayRuntimeVersion:
    if type(value) is not dict or frozenset(value) != _RUNTIME_KEYS:
        raise _Invalid
    return _runtime(
        RayRuntimeVersion(
            ray_major=value["ray_major"],
            ray_minor=value["ray_minor"],
            ray_patch=value["ray_patch"],
            python_implementation=value["python_implementation"],
            python_major=value["python_major"],
            python_minor=value["python_minor"],
            python_patch=value["python_patch"],
        )
    )


def _expectation(value: object) -> RayTargetExpectation:
    if type(value) is not RayTargetExpectation:
        raise _Invalid
    target_key = _target_key(value.target_key)
    cluster_session = _cluster_session(value.cluster_session)
    policy_revision = _counter(value.policy_revision)
    if type(value.runner_family) is not RayRunnerFamily:
        raise _Invalid
    return RayTargetExpectation(
        target_key=target_key,
        runner_family=value.runner_family,
        cluster_session=cluster_session,
        policy_revision=policy_revision,
        runtime=_runtime(value.runtime),
    )


def _expectation_wire(value: RayTargetExpectation, *, include_schema: bool) -> dict[str, object]:
    value = _expectation(value)
    result: dict[str, object] = {
        "target_key": value.target_key,
        "runner_family": value.runner_family.value,
        "cluster_session": value.cluster_session,
        "policy_revision": value.policy_revision,
        "runtime": _runtime_wire(value.runtime),
    }
    if include_schema:
        result.update(
            schema=RAY_TARGET_EXPECTATION_SCHEMA,
            schema_version=RAY_TARGET_EXPECTATION_SCHEMA_VERSION,
        )
    return result


def _decode_expectation(value: object, *, include_schema: bool) -> RayTargetExpectation:
    expected_keys = (
        _EXPECTATION_KEYS if include_schema else _EXPECTATION_KEYS - {"schema", "schema_version"}
    )
    if type(value) is not dict or frozenset(value) != expected_keys:
        raise _Invalid
    if include_schema and (
        value["schema"] != RAY_TARGET_EXPECTATION_SCHEMA
        or type(value["schema_version"]) is not int
        or value["schema_version"] != RAY_TARGET_EXPECTATION_SCHEMA_VERSION
    ):
        raise _Invalid
    try:
        runner_family = RayRunnerFamily(value["runner_family"])
    except (TypeError, ValueError) as error:
        raise _Invalid from error
    return _expectation(
        RayTargetExpectation(
            target_key=value["target_key"],
            runner_family=runner_family,
            cluster_session=value["cluster_session"],
            policy_revision=value["policy_revision"],
            runtime=_decode_runtime(value["runtime"]),
        )
    )


def _node_state_version(value: object) -> RayNodeStateVersion:
    if type(value) is not RayNodeStateVersion:
        raise _Invalid
    return RayNodeStateVersion(
        node_id=_node_id(value.node_id),
        node_state_version=_counter(value.node_state_version),
    )


def _node_state_version_wire(value: RayNodeStateVersion) -> dict[str, object]:
    value = _node_state_version(value)
    return {"node_id": value.node_id, "node_state_version": value.node_state_version}


def _decode_node_state_version(value: object) -> RayNodeStateVersion:
    if type(value) is not dict or frozenset(value) != _NODE_VERSION_KEYS:
        raise _Invalid
    return _node_state_version(
        RayNodeStateVersion(
            node_id=value["node_id"], node_state_version=value["node_state_version"]
        )
    )


def _strict_node_versions(value: object) -> tuple[RayNodeStateVersion, ...]:
    if type(value) is not tuple:
        raise _Invalid
    if not value or len(value) > RAY_TARGET_ATTESTATION_MAX_NODES:
        if len(value) > RAY_TARGET_ATTESTATION_MAX_NODES:
            raise _ResourceLimit
        raise _Invalid
    normalized = tuple(_node_state_version(item) for item in value)
    ids = tuple(item.node_id for item in normalized)
    if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
        raise _Invalid
    return normalized


def _boundary(value: object) -> RayObservationBoundary:
    if type(value) is not RayObservationBoundary:
        raise _Invalid
    before_resource = _counter(value.resource_state_version_before)
    after_resource = _counter(value.resource_state_version_after)
    before = _strict_node_versions(value.node_state_versions_before)
    after = _strict_node_versions(value.node_state_versions_after)
    if after_resource < before_resource:
        raise _Invalid
    before_ids = tuple(item.node_id for item in before)
    after_ids = tuple(item.node_id for item in after)
    if before_ids != after_ids:
        raise _Invalid
    if any(
        after_item.node_state_version < before_item.node_state_version
        for before_item, after_item in zip(before, after, strict=True)
    ):
        raise _Invalid
    return RayObservationBoundary(
        resource_state_version_before=before_resource,
        resource_state_version_after=after_resource,
        node_state_versions_before=before,
        node_state_versions_after=after,
    )


def _boundary_wire(value: RayObservationBoundary) -> dict[str, object]:
    value = _boundary(value)
    return {
        "resource_state_version_before": value.resource_state_version_before,
        "resource_state_version_after": value.resource_state_version_after,
        "node_state_versions_before": [
            _node_state_version_wire(item) for item in value.node_state_versions_before
        ],
        "node_state_versions_after": [
            _node_state_version_wire(item) for item in value.node_state_versions_after
        ],
    }


def _decode_boundary(value: object) -> RayObservationBoundary:
    if type(value) is not dict or frozenset(value) != _BOUNDARY_KEYS:
        raise _Invalid
    before = value["node_state_versions_before"]
    after = value["node_state_versions_after"]
    if type(before) is not list or type(after) is not list:
        raise _Invalid
    return _boundary(
        RayObservationBoundary(
            resource_state_version_before=value["resource_state_version_before"],
            resource_state_version_after=value["resource_state_version_after"],
            node_state_versions_before=tuple(_decode_node_state_version(item) for item in before),
            node_state_versions_after=tuple(_decode_node_state_version(item) for item in after),
        )
    )


def _observation_body(value: RayNodeObservation) -> dict[str, object]:
    return {
        "node_id": _node_id(value.node_id),
        "cluster_session": _cluster_session(value.cluster_session),
        "runtime": _runtime_wire(value.runtime),
    }


def _observation_wire(value: RayNodeObservation) -> dict[str, object]:
    result = _observation_body(value)
    result["observation_digest"] = _digest_value(value.observation_digest)
    return result


def _decode_observation(value: object) -> RayNodeObservation:
    if type(value) is not dict or frozenset(value) != _OBSERVATION_KEYS:
        raise _Invalid
    return RayNodeObservation(
        node_id=_node_id(value["node_id"]),
        cluster_session=_cluster_session(value["cluster_session"]),
        runtime=_decode_runtime(value["runtime"]),
        observation_digest=_digest_value(value["observation_digest"]),
    )


def _strict_nodes(value: object) -> tuple[RayNodeObservation, ...]:
    if type(value) is not tuple:
        raise _Invalid
    if not value or len(value) > RAY_TARGET_ATTESTATION_MAX_NODES:
        if len(value) > RAY_TARGET_ATTESTATION_MAX_NODES:
            raise _ResourceLimit
        raise _Invalid
    normalized: list[RayNodeObservation] = []
    for item in value:
        if type(item) is not RayNodeObservation:
            raise _Invalid
        normalized.append(
            RayNodeObservation(
                node_id=_node_id(item.node_id),
                cluster_session=_cluster_session(item.cluster_session),
                runtime=_runtime(item.runtime),
                observation_digest=_digest_value(item.observation_digest),
            )
        )
    result = tuple(normalized)
    ids = tuple(item.node_id for item in result)
    if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
        raise _Invalid
    return result


def _utc_datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _Invalid
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    value = _utc_datetime(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


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


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _domain_digest(domain: bytes, value: object) -> str:
    payload = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(domain + payload).hexdigest()}"


def ray_target_expectation_digest(expectation: RayTargetExpectation) -> str:
    """Return the domain-separated digest of one exact target expectation."""
    return _domain_digest(_EXPECTATION_DOMAIN, _expectation_wire(expectation, include_schema=True))


def ray_node_observation_digest(observation: RayNodeObservation) -> str:
    """Return the domain-separated digest of one node observation body."""
    return _domain_digest(_OBSERVATION_DOMAIN, _observation_body(observation))


def ray_membership_digest(boundary: RayObservationBoundary) -> str:
    """Return the digest of the stable canonical schedulable node-ID set."""
    boundary = _boundary(boundary)
    return _domain_digest(
        _MEMBERSHIP_DOMAIN,
        {"node_ids": [item.node_id for item in boundary.node_state_versions_before]},
    )


def _attestation_body(value: RayClusterAttestation) -> dict[str, object]:
    return {
        "schema": RAY_CLUSTER_ATTESTATION_SCHEMA,
        "schema_version": RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION,
        "expectation": _expectation_wire(value.expectation, include_schema=False),
        "expectation_digest": _digest_value(value.expectation_digest),
        "boundary": _boundary_wire(value.boundary),
        "membership_digest": _digest_value(value.membership_digest),
        "nodes": [_observation_wire(item) for item in _strict_nodes(value.nodes)],
        "observed_at": _timestamp(value.observed_at),
        "expires_at": _timestamp(value.expires_at),
    }


def ray_cluster_attestation_digest(attestation: RayClusterAttestation) -> str:
    """Return the domain-separated digest of a full attestation body."""
    return _domain_digest(_ATTESTATION_DOMAIN, _attestation_body(attestation))


def build_ray_node_observation(
    *, node_id: str, cluster_session: str, runtime: RayRuntimeVersion
) -> RayNodeObservation:
    """Build one validated node observation with its canonical digest."""
    temporary = RayNodeObservation(
        node_id=node_id,
        cluster_session=cluster_session,
        runtime=runtime,
        observation_digest="sha256:" + "0" * 64,
    )
    try:
        digest = ray_node_observation_digest(temporary)
    except (_Invalid, _ResourceLimit, TypeError, ValueError, UnicodeError):
        raise RayTargetAttestationEncodeError from None
    return replace(temporary, observation_digest=digest)


def build_ray_observation_boundary(
    *,
    resource_state_version_before: int,
    resource_state_version_after: int,
    node_state_versions_before: tuple[RayNodeStateVersion, ...],
    node_state_versions_after: tuple[RayNodeStateVersion, ...],
) -> RayObservationBoundary:
    """Build a stable-set boundary whose versions may advance."""
    try:
        return _boundary(
            RayObservationBoundary(
                resource_state_version_before=resource_state_version_before,
                resource_state_version_after=resource_state_version_after,
                node_state_versions_before=node_state_versions_before,
                node_state_versions_after=node_state_versions_after,
            )
        )
    except (_Invalid, _ResourceLimit, TypeError, ValueError, UnicodeError):
        raise RayTargetAttestationEncodeError from None


def build_ray_cluster_attestation(
    *,
    expectation: RayTargetExpectation,
    boundary: RayObservationBoundary,
    nodes: tuple[RayNodeObservation, ...],
    observed_at: datetime,
    expires_at: datetime,
) -> RayClusterAttestation:
    """Build a canonical attestation after a complete bounded observation."""
    try:
        expectation = _expectation(expectation)
        boundary = _boundary(boundary)
        nodes = _strict_nodes(nodes)
        observed_at = _utc_datetime(observed_at)
        expires_at = _utc_datetime(expires_at)
        _validate_window(observed_at, expires_at)
        _validate_membership(expectation, boundary, nodes)
        temporary = RayClusterAttestation(
            expectation=expectation,
            expectation_digest=ray_target_expectation_digest(expectation),
            boundary=boundary,
            membership_digest=ray_membership_digest(boundary),
            nodes=nodes,
            observed_at=observed_at,
            expires_at=expires_at,
            attestation_digest="sha256:" + "0" * 64,
        )
        result = replace(
            temporary,
            attestation_digest=ray_cluster_attestation_digest(temporary),
        )
        encoded = _attestation_body(result)
        encoded["attestation_digest"] = result.attestation_digest
        _encode(encoded, max_bytes=RAY_CLUSTER_ATTESTATION_MAX_BYTES)
        return result
    except (_Invalid, _ResourceLimit, TypeError, ValueError, UnicodeError):
        raise RayTargetAttestationEncodeError from None


def _validate_window(observed_at: datetime, expires_at: datetime) -> None:
    if expires_at <= observed_at:
        raise _Invalid
    if (expires_at - observed_at).total_seconds() > RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS:
        raise _ResourceLimit


def _validate_membership(
    expectation: RayTargetExpectation,
    boundary: RayObservationBoundary,
    nodes: tuple[RayNodeObservation, ...],
) -> None:
    expected_ids = tuple(item.node_id for item in boundary.node_state_versions_before)
    if tuple(item.node_id for item in nodes) != expected_ids:
        raise _Invalid
    for node in nodes:
        if (
            node.cluster_session != expectation.cluster_session
            or node.runtime != expectation.runtime
        ):
            raise _Invalid
        if node.observation_digest != ray_node_observation_digest(node):
            raise _Invalid


def _attestation(value: object) -> RayClusterAttestation:
    if type(value) is not RayClusterAttestation:
        raise _Invalid
    expectation = _expectation(value.expectation)
    boundary = _boundary(value.boundary)
    nodes = _strict_nodes(value.nodes)
    observed_at = _utc_datetime(value.observed_at)
    expires_at = _utc_datetime(value.expires_at)
    result = RayClusterAttestation(
        expectation=expectation,
        expectation_digest=_digest_value(value.expectation_digest),
        boundary=boundary,
        membership_digest=_digest_value(value.membership_digest),
        nodes=nodes,
        observed_at=observed_at,
        expires_at=expires_at,
        attestation_digest=_digest_value(value.attestation_digest),
    )
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _bounded_int(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        raise _ResourceLimit
    parsed = int(value)
    if abs(parsed) > RAY_TARGET_ATTESTATION_MAX_COUNTER:
        raise _ResourceLimit
    return parsed


def _reject_constant(_value: str) -> None:
    raise _Invalid


def _loads(serialized: object, *, max_bytes: int) -> object:
    if type(serialized) is not str:
        raise _Invalid
    try:
        encoded_size = len(serialized.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _Invalid from error
    if encoded_size > max_bytes:
        raise _ResourceLimit
    try:
        return json.loads(
            serialized,
            object_pairs_hook=_unique_object,
            parse_int=_bounded_int,
            parse_constant=_reject_constant,
        )
    except (_DuplicateKey, _Invalid, _ResourceLimit):
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise _Invalid from error


def _encode(value: object, *, max_bytes: int) -> str:
    serialized = _canonical_json(value)
    if len(serialized.encode("utf-8")) > max_bytes:
        raise _ResourceLimit
    return serialized


def encode_ray_target_expectation(expectation: RayTargetExpectation) -> str:
    """Encode one exact canonical expectation."""
    try:
        return _encode(
            _expectation_wire(expectation, include_schema=True),
            max_bytes=RAY_TARGET_EXPECTATION_MAX_BYTES,
        )
    except (_Invalid, _ResourceLimit, TypeError, ValueError, UnicodeError):
        raise RayTargetAttestationEncodeError from None


def decode_ray_target_expectation(serialized: object) -> RayTargetExpectation:
    """Decode one canonical expectation with strict framing and key sets."""
    try:
        value = _loads(serialized, max_bytes=RAY_TARGET_EXPECTATION_MAX_BYTES)
        if type(value) is not dict:
            raise _Invalid
        if (
            value.get("schema") != RAY_TARGET_EXPECTATION_SCHEMA
            or value.get("schema_version") != RAY_TARGET_EXPECTATION_SCHEMA_VERSION
        ):
            _reject(RayTargetAttestationRejection.UNSUPPORTED_SCHEMA)
        expectation = _decode_expectation(value, include_schema=True)
        canonical = _encode(
            _expectation_wire(expectation, include_schema=True),
            max_bytes=RAY_TARGET_EXPECTATION_MAX_BYTES,
        )
        if canonical != serialized:
            _reject(RayTargetAttestationRejection.NONCANONICAL)
        return expectation
    except RayTargetAttestationError:
        raise
    except _ResourceLimit:
        _reject(RayTargetAttestationRejection.RESOURCE_LIMIT)
    except (_Invalid, _DuplicateKey, TypeError, ValueError, UnicodeError):
        _reject(RayTargetAttestationRejection.INVALID)


def encode_ray_cluster_attestation(attestation: RayClusterAttestation) -> str:
    """Encode one complete canonical cluster attestation."""
    try:
        attestation = _attestation(attestation)
        _verify_derived_digests(attestation)
        _verify_membership_semantics(attestation)
        _verify_window_semantics(attestation)
        value = _attestation_body(attestation)
        value["attestation_digest"] = attestation.attestation_digest
        return _encode(value, max_bytes=RAY_CLUSTER_ATTESTATION_MAX_BYTES)
    except RayTargetAttestationError:
        raise RayTargetAttestationEncodeError from None
    except (_Invalid, _ResourceLimit, TypeError, ValueError, UnicodeError):
        raise RayTargetAttestationEncodeError from None


def decode_ray_cluster_attestation(serialized: object) -> RayClusterAttestation:
    """Decode one canonical cluster attestation and verify every digest."""
    try:
        value = _loads(serialized, max_bytes=RAY_CLUSTER_ATTESTATION_MAX_BYTES)
        if type(value) is not dict:
            raise _Invalid
        if (
            value.get("schema") != RAY_CLUSTER_ATTESTATION_SCHEMA
            or value.get("schema_version") != RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION
        ):
            _reject(RayTargetAttestationRejection.UNSUPPORTED_SCHEMA)
        if frozenset(value) != _ATTESTATION_KEYS:
            raise _Invalid
        node_values = value["nodes"]
        if type(node_values) is not list:
            raise _Invalid
        attestation = _attestation(
            RayClusterAttestation(
                expectation=_decode_expectation(value["expectation"], include_schema=False),
                expectation_digest=_digest_value(value["expectation_digest"]),
                boundary=_decode_boundary(value["boundary"]),
                membership_digest=_digest_value(value["membership_digest"]),
                nodes=tuple(_decode_observation(item) for item in node_values),
                observed_at=_decode_timestamp(value["observed_at"]),
                expires_at=_decode_timestamp(value["expires_at"]),
                attestation_digest=_digest_value(value["attestation_digest"]),
            )
        )
        canonical_value = _attestation_body(attestation)
        canonical_value["attestation_digest"] = attestation.attestation_digest
        canonical = _encode(canonical_value, max_bytes=RAY_CLUSTER_ATTESTATION_MAX_BYTES)
        if canonical != serialized:
            _reject(RayTargetAttestationRejection.NONCANONICAL)
        _verify_derived_digests(attestation)
        _verify_membership_semantics(attestation)
        _verify_window_semantics(attestation)
        return attestation
    except RayTargetAttestationError:
        raise
    except _ResourceLimit:
        _reject(RayTargetAttestationRejection.RESOURCE_LIMIT)
    except (_Invalid, _DuplicateKey, TypeError, ValueError, UnicodeError):
        _reject(RayTargetAttestationRejection.INVALID)


def _verify_derived_digests(attestation: RayClusterAttestation) -> None:
    if attestation.expectation_digest != ray_target_expectation_digest(attestation.expectation):
        _reject(RayTargetAttestationRejection.EXPECTATION_DIGEST_MISMATCH)
    if any(
        node.observation_digest != ray_node_observation_digest(node) for node in attestation.nodes
    ):
        _reject(RayTargetAttestationRejection.OBSERVATION_DIGEST_MISMATCH)
    if attestation.membership_digest != ray_membership_digest(attestation.boundary):
        _reject(RayTargetAttestationRejection.MEMBERSHIP_MISMATCH)
    if attestation.attestation_digest != ray_cluster_attestation_digest(attestation):
        _reject(RayTargetAttestationRejection.ATTESTATION_DIGEST_MISMATCH)


def _verify_membership_semantics(attestation: RayClusterAttestation) -> None:
    expected_ids = tuple(item.node_id for item in attestation.boundary.node_state_versions_before)
    if tuple(item.node_id for item in attestation.nodes) != expected_ids:
        _reject(RayTargetAttestationRejection.MEMBERSHIP_MISMATCH)
    for node in attestation.nodes:
        if node.cluster_session != attestation.expectation.cluster_session:
            _reject(RayTargetAttestationRejection.CLUSTER_SESSION_MISMATCH)
        if (
            node.runtime.ray_major,
            node.runtime.ray_minor,
            node.runtime.ray_patch,
        ) != (
            attestation.expectation.runtime.ray_major,
            attestation.expectation.runtime.ray_minor,
            attestation.expectation.runtime.ray_patch,
        ):
            _reject(RayTargetAttestationRejection.RAY_VERSION_MISMATCH)
        if (
            node.runtime.python_implementation
            != attestation.expectation.runtime.python_implementation
        ):
            _reject(RayTargetAttestationRejection.PYTHON_IMPLEMENTATION_MISMATCH)
        if (
            node.runtime.python_major,
            node.runtime.python_minor,
            node.runtime.python_patch,
        ) != (
            attestation.expectation.runtime.python_major,
            attestation.expectation.runtime.python_minor,
            attestation.expectation.runtime.python_patch,
        ):
            _reject(RayTargetAttestationRejection.PYTHON_VERSION_MISMATCH)


def _verify_window_semantics(attestation: RayClusterAttestation) -> None:
    try:
        _validate_window(attestation.observed_at, attestation.expires_at)
    except _ResourceLimit:
        _reject(RayTargetAttestationRejection.RESOURCE_LIMIT)
    except _Invalid:
        _reject(RayTargetAttestationRejection.INVALID)


def compare_ray_target_attestation(
    expectation: RayTargetExpectation,
    attestation: RayClusterAttestation,
    *,
    now: datetime,
) -> None:
    """Require an attestation to match one expectation inside its validity window."""
    try:
        expected = _expectation(expectation)
        observed = _attestation(attestation)
        now = _utc_datetime(now)
    except _ResourceLimit:
        _reject(RayTargetAttestationRejection.RESOURCE_LIMIT)
    except (_Invalid, TypeError, ValueError, UnicodeError):
        _reject(RayTargetAttestationRejection.INVALID)

    actual = observed.expectation
    _verify_derived_digests(observed)
    _verify_membership_semantics(observed)
    _verify_window_semantics(observed)
    if actual.target_key != expected.target_key:
        _reject(RayTargetAttestationRejection.TARGET_KEY_MISMATCH)
    if actual.runner_family is not expected.runner_family:
        _reject(RayTargetAttestationRejection.RUNNER_FAMILY_MISMATCH)
    if actual.cluster_session != expected.cluster_session:
        _reject(RayTargetAttestationRejection.CLUSTER_SESSION_MISMATCH)
    if actual.policy_revision != expected.policy_revision:
        _reject(RayTargetAttestationRejection.POLICY_REVISION_MISMATCH)
    if (
        actual.runtime.ray_major,
        actual.runtime.ray_minor,
        actual.runtime.ray_patch,
    ) != (
        expected.runtime.ray_major,
        expected.runtime.ray_minor,
        expected.runtime.ray_patch,
    ):
        _reject(RayTargetAttestationRejection.RAY_VERSION_MISMATCH)
    if actual.runtime.python_implementation != expected.runtime.python_implementation:
        _reject(RayTargetAttestationRejection.PYTHON_IMPLEMENTATION_MISMATCH)
    if (
        actual.runtime.python_major,
        actual.runtime.python_minor,
        actual.runtime.python_patch,
    ) != (
        expected.runtime.python_major,
        expected.runtime.python_minor,
        expected.runtime.python_patch,
    ):
        _reject(RayTargetAttestationRejection.PYTHON_VERSION_MISMATCH)
    if now < observed.observed_at:
        _reject(RayTargetAttestationRejection.NOT_YET_VALID)
    if now >= observed.expires_at:
        _reject(RayTargetAttestationRejection.EXPIRED)


__all__ = [
    "RAY_CLUSTER_ATTESTATION_MAX_BYTES",
    "RAY_CLUSTER_ATTESTATION_SCHEMA",
    "RAY_CLUSTER_ATTESTATION_SCHEMA_VERSION",
    "RAY_NODE_ID_HEX_CHARS",
    "RAY_TARGET_ATTESTATION_MAX_COUNTER",
    "RAY_TARGET_ATTESTATION_MAX_NODES",
    "RAY_TARGET_ATTESTATION_MAX_TEXT_BYTES",
    "RAY_TARGET_ATTESTATION_MAX_TTL_SECONDS",
    "RAY_TARGET_EXPECTATION_MAX_BYTES",
    "RAY_TARGET_EXPECTATION_SCHEMA",
    "RAY_TARGET_EXPECTATION_SCHEMA_VERSION",
    "RayClusterAttestation",
    "RayNodeObservation",
    "RayNodeStateVersion",
    "RayObservationBoundary",
    "RayRunnerFamily",
    "RayRuntimeVersion",
    "RayTargetAttestationEncodeError",
    "RayTargetAttestationError",
    "RayTargetAttestationRejection",
    "RayTargetExpectation",
    "build_ray_cluster_attestation",
    "build_ray_node_observation",
    "build_ray_observation_boundary",
    "compare_ray_target_attestation",
    "decode_ray_cluster_attestation",
    "decode_ray_target_expectation",
    "encode_ray_cluster_attestation",
    "encode_ray_target_expectation",
    "ray_cluster_attestation_digest",
    "ray_membership_digest",
    "ray_node_observation_digest",
    "ray_target_expectation_digest",
]
