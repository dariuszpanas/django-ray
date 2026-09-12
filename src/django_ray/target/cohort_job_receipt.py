"""Pure, bounded pending evidence from a fixed current-cohort probe Job.

Neither a receipt, its digest, nor a locally constructed value authenticates
the driver or grants eligibility. Publication must independently bind the
reserved request, actual Jobs record, exact live lease and manager-held nonce.
This module performs no clock I/O, Django setup, persistence or Ray calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Never

from django_ray.runtime.cohort_job import (
    CohortProbeJobRequest,
    VerifiedCohortJobProbe,
    decode_probe_job_request,
    encode_probe_job_request,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.target.attestation import (
    RAY_CLUSTER_ATTESTATION_MAX_BYTES,
    RAY_TARGET_ATTESTATION_MAX_COUNTER,
    RAY_TARGET_ATTESTATION_MAX_NODES,
    RayClusterAttestation,
    RayTargetExpectation,
    compare_ray_target_attestation,
    decode_ray_cluster_attestation,
    encode_ray_cluster_attestation,
)
from django_ray.target.cohort_probe import derive_cohort_target_key

COHORT_JOB_RECEIPT_SCHEMA = "django-ray.cohort-probe-job-receipt"
COHORT_JOB_RECEIPT_SCHEMA_VERSION = 1
COHORT_JOB_RECEIPT_MAX_BYTES = RAY_CLUSTER_ATTESTATION_MAX_BYTES + 16 * 1024
COHORT_JOB_RECEIPT_MAX_DEPTH = 12
NATIVE_RAY_JOB_ID_HEX_CHARS = 8

_DOMAIN = b"django-ray/cohort-probe-job-receipt/v1\x00"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_NATIVE_JOB_ID = re.compile(r"[0-9a-f]{8}")
_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "request",
        "request_digest",
        "submission_id",
        "native_job_id",
        "observed_package_version",
        "attestation",
        "collected_at",
    }
)


class CohortJobReceiptRejection(StrEnum):
    INVALID = "invalid"
    RESOURCE_LIMIT = "resource_limit"
    NONCANONICAL = "noncanonical"
    BINDING_MISMATCH = "binding_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    OBSERVATION_WINDOW_INVALID = "observation_window_invalid"


class CohortJobReceiptError(ValueError):
    """A fixed refusal that never retains receipt bytes or configuration."""

    def __init__(self, classification: CohortJobReceiptRejection) -> None:
        self.classification = classification
        super().__init__(f"Cohort Job receipt rejected: {classification.value}")


@dataclass(frozen=True, slots=True)
class CohortJobReceipt:
    """Pending observations only; the complete request contains no nonce."""

    request: CohortProbeJobRequest = field(repr=False)
    request_digest: str
    submission_id: str
    native_job_id: str
    observed_package_version: str
    attestation: RayClusterAttestation = field(repr=False)
    collected_at: datetime


def _reject(classification: CohortJobReceiptRejection) -> Never:
    raise CohortJobReceiptError(classification) from None


def is_canonical_native_ray_job_id(value: object) -> bool:
    """Require Ray 2.58's four-byte lowercase hex JobID, excluding its nil ID."""
    return (
        type(value) is str and _NATIVE_JOB_ID.fullmatch(value) is not None and value != "ffffffff"
    )


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _reject(CohortJobReceiptRejection.INVALID)
    return value


def _timestamp(value: object) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        _reject(CohortJobReceiptRejection.INVALID)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str or len(value) != 27 or not value.endswith("Z"):
        _reject(CohortJobReceiptRejection.INVALID)
    parsed = datetime.fromisoformat(value)
    if _timestamp(parsed) != value:
        _reject(CohortJobReceiptRejection.NONCANONICAL)
    return parsed


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 1 <= max_bytes <= COHORT_JOB_RECEIPT_MAX_BYTES:
        _reject(CohortJobReceiptRejection.RESOURCE_LIMIT)


def _wire(receipt: CohortJobReceipt) -> dict[str, Any]:
    if type(receipt) is not CohortJobReceipt:
        _reject(CohortJobReceiptRejection.INVALID)
    request = receipt.request
    request_json = encode_probe_job_request(request)
    if _digest(receipt.request_digest) != probe_job_request_digest(request):
        _reject(CohortJobReceiptRejection.DIGEST_MISMATCH)
    if type(receipt.submission_id) is not str or receipt.submission_id != probe_job_submission_id(
        request
    ):
        _reject(CohortJobReceiptRejection.BINDING_MISMATCH)
    if not is_canonical_native_ray_job_id(receipt.native_job_id):
        _reject(CohortJobReceiptRejection.INVALID)
    if (
        type(receipt.observed_package_version) is not str
        or receipt.observed_package_version != request.expected_package_version
    ):
        _reject(CohortJobReceiptRejection.RUNTIME_MISMATCH)
    collected_at = _timestamp(receipt.collected_at)
    attestation = receipt.attestation
    attestation_json = encode_ray_cluster_attestation(attestation)
    if not (
        request.issued_at <= attestation.observed_at <= receipt.collected_at
        and receipt.collected_at < request.expires_at
        and receipt.collected_at < attestation.expires_at
    ):
        _reject(CohortJobReceiptRejection.OBSERVATION_WINDOW_INVALID)
    expectation = RayTargetExpectation(
        request.target_key
        if request.target_key is not None
        else derive_cohort_target_key(
            request.runner_family, attestation.expectation.cluster_session
        ),
        request.runner_family,
        request.expected_cluster_session or attestation.expectation.cluster_session,
        request.policy_revision,
        request.expected_runtime,
    )
    if attestation.expectation != expectation:
        _reject(CohortJobReceiptRejection.BINDING_MISMATCH)
    compare_ray_target_attestation(expectation, attestation, now=receipt.collected_at)
    return {
        "schema": COHORT_JOB_RECEIPT_SCHEMA,
        "schema_version": COHORT_JOB_RECEIPT_SCHEMA_VERSION,
        "request": json.loads(request_json),
        "request_digest": receipt.request_digest,
        "submission_id": receipt.submission_id,
        "native_job_id": receipt.native_job_id,
        "observed_package_version": receipt.observed_package_version,
        "attestation": json.loads(attestation_json),
        "collected_at": collected_at,
    }


def encode_cohort_job_receipt(
    receipt: CohortJobReceipt, *, max_bytes: int = COHORT_JOB_RECEIPT_MAX_BYTES
) -> str:
    _limit(max_bytes)
    try:
        serialized = _canonical(_wire(receipt))
        if len(serialized) > max_bytes:
            _reject(CohortJobReceiptRejection.RESOURCE_LIMIT)
        return serialized
    except CohortJobReceiptError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError, AttributeError):
        _reject(CohortJobReceiptRejection.INVALID)


def cohort_job_receipt_digest(receipt: CohortJobReceipt) -> str:
    return (
        "sha256:"
        + hashlib.sha256(_DOMAIN + encode_cohort_job_receipt(receipt).encode("ascii")).hexdigest()
    )


def cohort_job_receipt_from_probe(probe: VerifiedCohortJobProbe) -> CohortJobReceipt:
    """Revalidate typed observations; this conversion does not authenticate them."""
    if type(probe) is not VerifiedCohortJobProbe:
        _reject(CohortJobReceiptRejection.INVALID)
    receipt = CohortJobReceipt(
        request=probe.request,
        request_digest=probe.request_digest,
        submission_id=probe.submission_id,
        native_job_id=probe.native_job_id,
        observed_package_version=probe.observed_package_version,
        attestation=probe.attestation,
        collected_at=probe.collected_at,
    )
    encode_cohort_job_receipt(receipt)
    return receipt


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _reject(CohortJobReceiptRejection.INVALID)
        value[key] = item
    return value


def _parse_int(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        _reject(CohortJobReceiptRejection.RESOURCE_LIMIT)
    parsed = int(value)
    if abs(parsed) > RAY_TARGET_ATTESTATION_MAX_COUNTER:
        _reject(CohortJobReceiptRejection.RESOURCE_LIMIT)
    return parsed


def _reject_number(_value: str) -> Never:
    _reject(CohortJobReceiptRejection.INVALID)


def _load(serialized: object, max_bytes: int) -> dict[str, Any]:
    _limit(max_bytes)
    if type(serialized) is not str:
        _reject(CohortJobReceiptRejection.INVALID)
    if len(serialized) > max_bytes or len(serialized.encode("utf-8")) > max_bytes:
        _reject(CohortJobReceiptRejection.RESOURCE_LIMIT)
    depth = items = 0
    quoted = escaped = False
    for character in serialized:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            items += 1
        elif character in "]}":
            depth -= 1
        elif character in ",:":
            items += 1
        if depth > COHORT_JOB_RECEIPT_MAX_DEPTH or items > RAY_TARGET_ATTESTATION_MAX_NODES * 128:
            _reject(CohortJobReceiptRejection.RESOURCE_LIMIT)
    value = json.loads(
        serialized,
        object_pairs_hook=_unique_object,
        parse_int=_parse_int,
        parse_float=_reject_number,
        parse_constant=_reject_number,
    )
    if type(value) is not dict or value.keys() != _KEYS:
        _reject(CohortJobReceiptRejection.INVALID)
    return value


def decode_cohort_job_receipt(
    serialized: object,
    *,
    expected_request: CohortProbeJobRequest | None = None,
    expected_request_digest: str | None = None,
    expected_submission_id: str | None = None,
    expected_receipt_digest: str | None = None,
    max_bytes: int = COHORT_JOB_RECEIPT_MAX_BYTES,
) -> CohortJobReceipt:
    """Validate pending evidence, optionally bound to independent reservation facts.

    Authoritative integration must supply the reservation's expected request,
    request digest and submission ID; none may be derived from this receipt.
    A later publication boundary must separately verify freshness at its own
    current time and the actual authenticated Jobs record.
    """
    try:
        body = _load(serialized, max_bytes)
        if (
            body["schema"] != COHORT_JOB_RECEIPT_SCHEMA
            or type(body["schema_version"]) is not int
            or body["schema_version"] != COHORT_JOB_RECEIPT_SCHEMA_VERSION
        ):
            _reject(CohortJobReceiptRejection.INVALID)
        receipt = CohortJobReceipt(
            request=decode_probe_job_request(_canonical(body["request"])),
            request_digest=body["request_digest"],
            submission_id=body["submission_id"],
            native_job_id=body["native_job_id"],
            observed_package_version=body["observed_package_version"],
            attestation=decode_ray_cluster_attestation(_canonical(body["attestation"])),
            collected_at=_parse_timestamp(body["collected_at"]),
        )
        if encode_cohort_job_receipt(receipt, max_bytes=max_bytes) != serialized:
            _reject(CohortJobReceiptRejection.NONCANONICAL)
        if expected_request is not None and encode_probe_job_request(
            expected_request
        ) != encode_probe_job_request(receipt.request):
            _reject(CohortJobReceiptRejection.BINDING_MISMATCH)
        if (
            expected_request_digest is not None
            and _digest(expected_request_digest) != receipt.request_digest
        ):
            _reject(CohortJobReceiptRejection.DIGEST_MISMATCH)
        if expected_submission_id is not None and (
            type(expected_submission_id) is not str
            or expected_submission_id != receipt.submission_id
        ):
            _reject(CohortJobReceiptRejection.BINDING_MISMATCH)
        if expected_receipt_digest is not None and _digest(
            expected_receipt_digest
        ) != cohort_job_receipt_digest(receipt):
            _reject(CohortJobReceiptRejection.DIGEST_MISMATCH)
        return receipt
    except CohortJobReceiptError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError, AttributeError):
        _reject(CohortJobReceiptRejection.INVALID)
