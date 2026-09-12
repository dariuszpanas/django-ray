"""Private canonical Sync claim; no Ray import or synthetic cluster identity."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Never

from django_ray.execution_codec import ExecutionIdentity, is_valid_execution_identity
from django_ray.target.cohort_claim import CohortPythonVersion, validate_cohort_python
from django_ray.target.cohort_contract import (
    CohortContractError,
    CohortContractRejection,
    _digest,
    _package_version,
    _parse_timestamp,
    _positive,
    _timestamp,
)

_SCHEMA = "django-ray.cohort-sync-contract"
_DOMAIN = b"django-ray/cohort-sync-contract/v3\x00"
_MAX_BYTES = 4096


@dataclass(frozen=True, slots=True)
class CohortSyncContract:
    identity: ExecutionIdentity
    expected_django_ray_version: str
    target_binding_id: int
    cohort_evidence_id: int
    cohort_evidence_digest: str
    claimed_at: datetime
    python: CohortPythonVersion


def _invalid() -> Never:
    raise CohortContractError(CohortContractRejection.INVALID) from None


def encode_cohort_sync_contract(contract: CohortSyncContract) -> str:
    try:
        if type(contract) is not CohortSyncContract or not is_valid_execution_identity(
            contract.identity
        ):
            _invalid()
        _package_version(contract.expected_django_ray_version)
        _positive(contract.target_binding_id)
        _positive(contract.cohort_evidence_id)
        _digest(contract.cohort_evidence_digest)
        validate_cohort_python(contract.python)
        if contract.target_binding_id != contract.identity.task_execution_pk:
            _invalid()
        value = asdict(contract)
        value.update(schema=_SCHEMA, schema_version=1, execution_protocol_version=3)
        value["claimed_at"] = _timestamp(contract.claimed_at)
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(result.encode("ascii")) > _MAX_BYTES:
            _invalid()
        return result
    except (ValueError, TypeError, AttributeError, OverflowError):
        _invalid()


def cohort_sync_contract_digest(contract: CohortSyncContract) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            _DOMAIN + encode_cohort_sync_contract(contract).encode("ascii")
        ).hexdigest()
    )


def decode_cohort_sync_contract(
    serialized, *, expected_identity=None, expected_contract_digest=None
):
    try:
        if type(serialized) is not str or len(serialized.encode("utf-8")) > _MAX_BYTES:
            _invalid()
        # Canonical re-encoding below also rejects duplicate keys and extra fields.
        value = json.loads(serialized)
        if type(value) is not dict:
            _invalid()
        contract = CohortSyncContract(
            ExecutionIdentity(**value["identity"]),
            value["expected_django_ray_version"],
            value["target_binding_id"],
            value["cohort_evidence_id"],
            value["cohort_evidence_digest"],
            _parse_timestamp(value["claimed_at"]),
            CohortPythonVersion(**value["python"]),
        )
        if encode_cohort_sync_contract(contract) != serialized:
            _invalid()
        if expected_identity is not None and contract.identity != expected_identity:
            _invalid()
        if expected_contract_digest is not None:
            _digest(expected_contract_digest)
            if cohort_sync_contract_digest(contract) != expected_contract_digest:
                _invalid()
        return contract
    except (KeyError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        _invalid()


def verify_cohort_sync_runtime(contract: CohortSyncContract) -> str | None:
    """Return only a fixed local mismatch before importing application code."""
    contract = decode_cohort_sync_contract(encode_cohort_sync_contract(contract))
    from django_ray import __version__

    if __version__ != contract.expected_django_ray_version:
        return "package_mismatch"
    actual = CohortPythonVersion(
        platform.python_implementation().lower(),
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    return None if actual == contract.python else "runtime_mismatch"
