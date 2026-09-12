"""Pure, canonical protocol-3 current-cohort claims and derived leaf claims.

These codecs do not activate a protocol or target route. A cohort evidence
digest identifies an independently persisted fact; the separate wire digest
also covers that fact's row ID and the complete claim. Digests are integrity
bindings, not authentication. Authoritative callers must supply independently
held expected bindings when decoding.

A leaf retains its validated outer claim's digest instead of duplicating the
cluster attestation. It cannot independently prove cluster membership. A local
runtime mismatch never proves that an outer task or its siblings did not run.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Never, cast

from packaging.version import InvalidVersion, Version

from django_ray.execution_codec import (
    NESTED_EXECUTION_REQUEST_MAX_BYTES,
    NESTED_EXECUTION_REQUEST_MAX_DEPTH,
    ExecutionIdentity,
    is_valid_execution_identity,
)
from django_ray.execution_protocol import COHORT_EXECUTION_PROTOCOL_VERSION
from django_ray.target.attestation import (
    RAY_CLUSTER_ATTESTATION_MAX_BYTES,
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RAY_TARGET_ATTESTATION_MAX_NODES,
    RAY_TARGET_EXPECTATION_MAX_BYTES,
    RayClusterAttestation,
    RayRuntimeVersion,
    RayTargetAttestationEncodeError,
    RayTargetAttestationError,
    RayTargetExpectation,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    decode_ray_target_expectation,
    encode_ray_cluster_attestation,
    encode_ray_target_expectation,
    ray_target_expectation_digest,
)

COHORT_CONTRACT_SCHEMA = "django-ray.cohort-execution-contract"
COHORT_LEAF_SCHEMA = "django-ray.cohort-leaf-contract"
COHORT_CONTRACT_SCHEMA_VERSION = 1
COHORT_CONTRACT_MAX_BYTES = (
    RAY_CLUSTER_ATTESTATION_MAX_BYTES
    + RAY_TARGET_EXPECTATION_MAX_BYTES
    + NESTED_EXECUTION_REQUEST_MAX_BYTES
)
COHORT_LEAF_MAX_BYTES = NESTED_EXECUTION_REQUEST_MAX_BYTES

_CONTRACT_DOMAIN = b"django-ray/cohort-execution-contract/v3\x00"
_LEAF_DOMAIN = b"django-ray/cohort-leaf-contract/v3\x00"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY_KEYS = frozenset(
    {"task_execution_pk", "task_id", "attempt_number", "execution_generation"}
)
_COMMON_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "execution_protocol_version",
        "identity",
        "expected_django_ray_version",
        "target_binding_id",
        "cohort_evidence_id",
        "cohort_evidence_digest",
        "claimed_at",
        "target_expectation",
        "target_expectation_digest",
        "claim_attestation_digest",
    }
)


@dataclass(frozen=True, slots=True)
class CohortExecutionContract:
    """One complete immutable claim, including all-node admission evidence."""

    identity: ExecutionIdentity
    expected_django_ray_version: str
    target_binding_id: int
    cohort_evidence_id: int
    cohort_evidence_digest: str
    claimed_at: datetime
    target_expectation: RayTargetExpectation
    target_expectation_digest: str
    claim_attestation: RayClusterAttestation
    claim_attestation_digest: str


@dataclass(frozen=True, slots=True)
class CohortLeafContract:
    """Bounded descendant claim derived only from a validated outer claim."""

    identity: ExecutionIdentity
    expected_django_ray_version: str
    target_binding_id: int
    cohort_evidence_id: int
    cohort_evidence_digest: str
    claimed_at: datetime
    target_expectation: RayTargetExpectation
    target_expectation_digest: str
    claim_attestation_digest: str
    claim_membership_digest: str
    outer_contract_digest: str


class CohortContractRejection(StrEnum):
    INVALID = "invalid"
    RESOURCE_LIMIT = "resource_limit"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    NONCANONICAL = "noncanonical"
    EXPECTATION_MISMATCH = "expectation_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    BINDING_MISMATCH = "binding_mismatch"
    CLAIM_WINDOW_INVALID = "claim_window_invalid"
    INVALID_OBSERVATION = "invalid_observation"


class CohortContractError(ValueError):
    """Fixed classification only; no request, identity, or credential echo."""

    def __init__(self, classification: CohortContractRejection) -> None:
        self.classification = classification
        super().__init__(f"Cohort contract rejected: {classification.value}")


class CohortRuntimeMismatch(StrEnum):
    PACKAGE_VERSION_MISMATCH = "package_version_mismatch"
    CLUSTER_SESSION_MISMATCH = "cluster_session_mismatch"
    RAY_VERSION_MISMATCH = "ray_version_mismatch"
    PYTHON_IMPLEMENTATION_MISMATCH = "python_implementation_mismatch"
    PYTHON_VERSION_MISMATCH = "python_version_mismatch"


def _reject(reason: CohortContractRejection) -> Never:
    raise CohortContractError(reason) from None


def _positive(value: object) -> int:
    if type(value) is not int or not 0 < value <= RAY_TARGET_ATTESTATION_MAX_COUNTER:
        _reject(CohortContractRejection.INVALID)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _reject(CohortContractRejection.INVALID)
    return value


def _package_version(value: object) -> str:
    if type(value) is not str or not 0 < len(value) <= 128:
        _reject(CohortContractRejection.INVALID)
    try:
        canonical = str(Version(value))
    except InvalidVersion:
        _reject(CohortContractRejection.INVALID)
    if canonical != value:
        _reject(CohortContractRejection.NONCANONICAL)
    return value


def _timestamp(value: object) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        _reject(CohortContractRejection.INVALID)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str or len(value) != 27 or not value.endswith("Z"):
        _reject(CohortContractRejection.INVALID)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _reject(CohortContractRejection.INVALID)
    if _timestamp(parsed) != value:
        _reject(CohortContractRejection.NONCANONICAL)
    return parsed


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _wire(contract: CohortExecutionContract | CohortLeafContract) -> dict[str, Any]:
    if type(contract) not in {CohortExecutionContract, CohortLeafContract}:
        _reject(CohortContractRejection.INVALID)
    identity = contract.identity
    if type(identity) is not ExecutionIdentity or not is_valid_execution_identity(identity):
        _reject(CohortContractRejection.INVALID)
    _positive(identity.execution_generation)
    _positive(contract.target_binding_id)
    _positive(contract.cohort_evidence_id)
    _digest(contract.cohort_evidence_digest)
    _digest(contract.target_expectation_digest)
    _digest(contract.claim_attestation_digest)
    _package_version(contract.expected_django_ray_version)
    claimed_at = _timestamp(contract.claimed_at)
    expectation = encode_ray_target_expectation(contract.target_expectation)
    if contract.target_expectation_digest != ray_target_expectation_digest(
        contract.target_expectation
    ):
        _reject(CohortContractRejection.DIGEST_MISMATCH)
    leaf = isinstance(contract, CohortLeafContract)
    wire = {
        "schema": COHORT_LEAF_SCHEMA if leaf else COHORT_CONTRACT_SCHEMA,
        "schema_version": COHORT_CONTRACT_SCHEMA_VERSION,
        "execution_protocol_version": COHORT_EXECUTION_PROTOCOL_VERSION,
        "identity": {key: getattr(identity, key) for key in _IDENTITY_KEYS},
        "expected_django_ray_version": contract.expected_django_ray_version,
        "target_binding_id": contract.target_binding_id,
        "cohort_evidence_id": contract.cohort_evidence_id,
        "cohort_evidence_digest": contract.cohort_evidence_digest,
        "claimed_at": claimed_at,
        "target_expectation": json.loads(expectation),
        "target_expectation_digest": contract.target_expectation_digest,
        "claim_attestation_digest": contract.claim_attestation_digest,
    }
    if isinstance(contract, CohortLeafContract):
        wire["outer_contract_digest"] = _digest(contract.outer_contract_digest)
        wire["claim_membership_digest"] = _digest(contract.claim_membership_digest)
    else:
        attestation = encode_ray_cluster_attestation(contract.claim_attestation)
        if contract.claim_attestation.expectation != contract.target_expectation:
            _reject(CohortContractRejection.EXPECTATION_MISMATCH)
        if contract.claim_attestation.attestation_digest != contract.claim_attestation_digest:
            _reject(CohortContractRejection.DIGEST_MISMATCH)
        try:
            compare_ray_target_attestation(
                contract.target_expectation, contract.claim_attestation, now=contract.claimed_at
            )
        except RayTargetAttestationError:
            _reject(CohortContractRejection.CLAIM_WINDOW_INVALID)
        wire["claim_attestation"] = json.loads(attestation)
    return wire


def _limit(max_bytes: int, *, leaf: bool) -> None:
    ceiling = COHORT_LEAF_MAX_BYTES if leaf else COHORT_CONTRACT_MAX_BYTES
    if type(max_bytes) is not int or not 0 < max_bytes <= ceiling:
        _reject(CohortContractRejection.RESOURCE_LIMIT)


def _encode(
    contract: CohortExecutionContract | CohortLeafContract, *, leaf: bool, max_bytes: int
) -> str:
    _limit(max_bytes, leaf=leaf)
    if type(contract) is not (CohortLeafContract if leaf else CohortExecutionContract):
        _reject(CohortContractRejection.INVALID)
    try:
        serialized = _canonical(_wire(contract))
        if len(serialized.encode("utf-8")) > max_bytes:
            _reject(CohortContractRejection.RESOURCE_LIMIT)
        return serialized
    except CohortContractError:
        raise
    except (
        RayTargetAttestationEncodeError,
        RayTargetAttestationError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        _reject(CohortContractRejection.INVALID)


def encode_cohort_execution_contract(
    contract: CohortExecutionContract, *, max_bytes: int = COHORT_CONTRACT_MAX_BYTES
) -> str:
    return _encode(contract, leaf=False, max_bytes=max_bytes)


def encode_cohort_leaf_contract(
    contract: CohortLeafContract, *, max_bytes: int = COHORT_LEAF_MAX_BYTES
) -> str:
    return _encode(contract, leaf=True, max_bytes=max_bytes)


def cohort_execution_contract_digest(contract: CohortExecutionContract) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            _CONTRACT_DOMAIN + encode_cohort_execution_contract(contract).encode("utf-8")
        ).hexdigest()
    )


def cohort_leaf_contract_digest(contract: CohortLeafContract) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            _LEAF_DOMAIN + encode_cohort_leaf_contract(contract).encode("utf-8")
        ).hexdigest()
    )


def derive_cohort_leaf_contract(contract: CohortExecutionContract) -> CohortLeafContract:
    """Validate the full claim before deriving a compact descendant binding."""
    outer_digest = cohort_execution_contract_digest(contract)
    leaf = CohortLeafContract(
        identity=contract.identity,
        expected_django_ray_version=contract.expected_django_ray_version,
        target_binding_id=contract.target_binding_id,
        cohort_evidence_id=contract.cohort_evidence_id,
        cohort_evidence_digest=contract.cohort_evidence_digest,
        claimed_at=contract.claimed_at,
        target_expectation=contract.target_expectation,
        target_expectation_digest=contract.target_expectation_digest,
        claim_attestation_digest=contract.claim_attestation_digest,
        claim_membership_digest=contract.claim_attestation.membership_digest,
        outer_contract_digest=outer_digest,
    )
    encode_cohort_leaf_contract(leaf)
    return leaf


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _reject(CohortContractRejection.INVALID)
        value[key] = item
    return value


def _parse_int(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        _reject(CohortContractRejection.RESOURCE_LIMIT)
    result = int(value)
    if abs(result) > RAY_TARGET_ATTESTATION_MAX_COUNTER:
        _reject(CohortContractRejection.RESOURCE_LIMIT)
    return result


def _reject_number(_value: str) -> Never:
    _reject(CohortContractRejection.INVALID)


def _load(serialized: object, *, leaf: bool, max_bytes: int) -> dict[str, Any]:
    _limit(max_bytes, leaf=leaf)
    if type(serialized) is not str:
        _reject(CohortContractRejection.INVALID)
    if len(serialized) > max_bytes or len(serialized.encode("utf-8")) > max_bytes:
        _reject(CohortContractRejection.RESOURCE_LIMIT)
    depth = items = 0
    quoted = escaped = False
    for char in serialized:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            items += 1
        elif char in "]}":
            depth -= 1
        elif char in ",:":
            items += 1
        if (
            depth > NESTED_EXECUTION_REQUEST_MAX_DEPTH
            or items > RAY_TARGET_ATTESTATION_MAX_NODES * 128
        ):
            _reject(CohortContractRejection.RESOURCE_LIMIT)
    result = json.loads(
        serialized,
        object_pairs_hook=_unique_object,
        parse_int=_parse_int,
        parse_float=_reject_number,
        parse_constant=_reject_number,
    )
    if type(result) is not dict:
        _reject(CohortContractRejection.INVALID)
    return result


def _decode(
    serialized: object, *, leaf: bool, max_bytes: int, expected: dict[str, object]
) -> CohortExecutionContract | CohortLeafContract:
    try:
        value = _load(serialized, leaf=leaf, max_bytes=max_bytes)
        if (
            value.get("schema") != (COHORT_LEAF_SCHEMA if leaf else COHORT_CONTRACT_SCHEMA)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != COHORT_CONTRACT_SCHEMA_VERSION
            or type(value.get("execution_protocol_version")) is not int
            or value["execution_protocol_version"] != COHORT_EXECUTION_PROTOCOL_VERSION
        ):
            _reject(CohortContractRejection.UNSUPPORTED_SCHEMA)
        extra = "outer_contract_digest" if leaf else "claim_attestation"
        extra_keys = {extra, "claim_membership_digest"} if leaf else {extra}
        if set(value) != _COMMON_KEYS | extra_keys:
            _reject(CohortContractRejection.INVALID)
        identity = value["identity"]
        if type(identity) is not dict or set(identity) != _IDENTITY_KEYS:
            _reject(CohortContractRejection.INVALID)
        common = {
            "identity": ExecutionIdentity(**identity),
            "expected_django_ray_version": value["expected_django_ray_version"],
            "target_binding_id": value["target_binding_id"],
            "cohort_evidence_id": value["cohort_evidence_id"],
            "cohort_evidence_digest": value["cohort_evidence_digest"],
            "claimed_at": _parse_timestamp(value["claimed_at"]),
            "target_expectation": decode_ray_target_expectation(
                _canonical(value["target_expectation"])
            ),
            "target_expectation_digest": value["target_expectation_digest"],
            "claim_attestation_digest": value["claim_attestation_digest"],
        }
        contract: CohortExecutionContract | CohortLeafContract
        if leaf:
            contract = CohortLeafContract(
                **common,
                claim_membership_digest=value["claim_membership_digest"],
                outer_contract_digest=value[extra],
            )
        else:
            contract = CohortExecutionContract(
                **common, claim_attestation=decode_ray_cluster_attestation(_canonical(value[extra]))
            )
        canonical = _encode(contract, leaf=leaf, max_bytes=max_bytes)
        if canonical != serialized:
            _reject(CohortContractRejection.NONCANONICAL)
        for key, wanted in expected.items():
            if wanted is None:
                continue
            if key == "contract_digest":
                _digest(wanted)
                domain = _LEAF_DOMAIN if leaf else _CONTRACT_DOMAIN
                actual = "sha256:" + hashlib.sha256(domain + canonical.encode("utf-8")).hexdigest()
                if actual != wanted:
                    _reject(CohortContractRejection.BINDING_MISMATCH)
                continue
            if key in {"target_binding_id", "cohort_evidence_id"}:
                _positive(wanted)
            elif key == "claimed_at":
                _timestamp(wanted)
            elif key == "identity":
                if type(wanted) is not ExecutionIdentity or not is_valid_execution_identity(wanted):
                    _reject(CohortContractRejection.INVALID)
                _positive(wanted.execution_generation)
            else:
                _digest(wanted)
            if getattr(contract, key) != wanted:
                _reject(CohortContractRejection.BINDING_MISMATCH)
        return contract
    except CohortContractError:
        raise
    except (
        RayTargetAttestationEncodeError,
        RayTargetAttestationError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
    ):
        _reject(CohortContractRejection.INVALID)


def decode_cohort_execution_contract(
    serialized: object,
    *,
    max_bytes: int = COHORT_CONTRACT_MAX_BYTES,
    expected_identity: ExecutionIdentity | None = None,
    expected_target_binding_id: int | None = None,
    expected_cohort_evidence_id: int | None = None,
    expected_cohort_evidence_digest: str | None = None,
    expected_claimed_at: datetime | None = None,
    expected_target_expectation_digest: str | None = None,
    expected_claim_attestation_digest: str | None = None,
    expected_contract_digest: str | None = None,
) -> CohortExecutionContract:
    return cast(
        CohortExecutionContract,
        _decode(
            serialized,
            leaf=False,
            max_bytes=max_bytes,
            expected={
                "identity": expected_identity,
                "target_binding_id": expected_target_binding_id,
                "cohort_evidence_id": expected_cohort_evidence_id,
                "cohort_evidence_digest": expected_cohort_evidence_digest,
                "claimed_at": expected_claimed_at,
                "target_expectation_digest": expected_target_expectation_digest,
                "claim_attestation_digest": expected_claim_attestation_digest,
                "contract_digest": expected_contract_digest,
            },
        ),
    )


def decode_cohort_leaf_contract(
    serialized: object,
    *,
    max_bytes: int = COHORT_LEAF_MAX_BYTES,
    expected_identity: ExecutionIdentity | None = None,
    expected_target_binding_id: int | None = None,
    expected_cohort_evidence_id: int | None = None,
    expected_cohort_evidence_digest: str | None = None,
    expected_claimed_at: datetime | None = None,
    expected_target_expectation_digest: str | None = None,
    expected_claim_attestation_digest: str | None = None,
    expected_claim_membership_digest: str | None = None,
    expected_outer_contract_digest: str | None = None,
    expected_contract_digest: str | None = None,
) -> CohortLeafContract:
    return cast(
        CohortLeafContract,
        _decode(
            serialized,
            leaf=True,
            max_bytes=max_bytes,
            expected={
                "identity": expected_identity,
                "target_binding_id": expected_target_binding_id,
                "cohort_evidence_id": expected_cohort_evidence_id,
                "cohort_evidence_digest": expected_cohort_evidence_digest,
                "claimed_at": expected_claimed_at,
                "target_expectation_digest": expected_target_expectation_digest,
                "claim_attestation_digest": expected_claim_attestation_digest,
                "claim_membership_digest": expected_claim_membership_digest,
                "outer_contract_digest": expected_outer_contract_digest,
                "contract_digest": expected_contract_digest,
            },
        ),
    )


def compare_cohort_runtime(
    contract: CohortExecutionContract | CohortLeafContract,
    *,
    actual_django_ray_version: str,
    actual_runtime: RayRuntimeVersion,
    actual_cluster_session: str,
) -> CohortRuntimeMismatch | None:
    """Compare point observations; invalid observations are errors, not mismatch proof.

    No clock is read and claim expiry is not execution expiry. A matching tuple
    does not authenticate the observation or establish current membership, and
    no result implies anything about outer/sibling application invocation.
    """
    _encode(
        contract,
        leaf=isinstance(contract, CohortLeafContract),
        max_bytes=COHORT_LEAF_MAX_BYTES
        if isinstance(contract, CohortLeafContract)
        else COHORT_CONTRACT_MAX_BYTES,
    )
    expected = contract.target_expectation
    try:
        _package_version(actual_django_ray_version)
        encode_ray_target_expectation(
            replace(expected, runtime=actual_runtime, cluster_session=actual_cluster_session)
        )
    except (CohortContractError, RayTargetAttestationEncodeError):
        _reject(CohortContractRejection.INVALID_OBSERVATION)
    if actual_django_ray_version != contract.expected_django_ray_version:
        return CohortRuntimeMismatch.PACKAGE_VERSION_MISMATCH
    if actual_cluster_session != expected.cluster_session:
        return CohortRuntimeMismatch.CLUSTER_SESSION_MISMATCH
    if (actual_runtime.ray_major, actual_runtime.ray_minor, actual_runtime.ray_patch) != (
        expected.runtime.ray_major,
        expected.runtime.ray_minor,
        expected.runtime.ray_patch,
    ):
        return CohortRuntimeMismatch.RAY_VERSION_MISMATCH
    if actual_runtime.python_implementation != expected.runtime.python_implementation:
        return CohortRuntimeMismatch.PYTHON_IMPLEMENTATION_MISMATCH
    if (actual_runtime.python_major, actual_runtime.python_minor, actual_runtime.python_patch) != (
        expected.runtime.python_major,
        expected.runtime.python_minor,
        expected.runtime.python_patch,
    ):
        return CohortRuntimeMismatch.PYTHON_VERSION_MISMATCH
    return None
