"""Private manager inspection of a reserved Jobs probe outside database locks.

The storage integration must build the snapshot from its exact lease/challenge
reservation and revalidate it during atomic publication. Neither this snapshot
nor the returned value authenticates an arbitrary caller or grants capacity.
The inspector queries the pinned Jobs endpoint itself; caller-supplied Jobs
records, status strings, logs and metadata alone cannot bypass that query.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Never
from urllib.parse import urlsplit

from django_ray.execution_codec import _bounded_json_dumps, _validate_json_tree
from django_ray.runtime.cohort_job import (
    CohortProbeJobRequest,
    decode_probe_job_request,
    encode_probe_job_request,
    probe_job_metadata,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.target.attestation import compare_ray_target_attestation
from django_ray.target.cohort_intent import _digest, _endpoint

if TYPE_CHECKING:
    from django_ray.target.cohort_job_receipt import CohortJobReceipt

COHORT_PROBE_RUNTIME_ENV_MAX_BYTES = 64 * 1024
COHORT_PROBE_ENTRYPOINT_MAX_BYTES = 16 * 1024
_ENTRYPOINT_DOMAIN = b"django-ray/cohort-probe-entrypoint/v1\x00"
_RUNTIME_ENV_DOMAIN = b"django-ray/cohort-probe-submitted-runtime-env/v1\x00"


class CohortJobInspectionReason(StrEnum):
    INVALID_RESERVATION = "invalid_reservation"
    TRANSACTION_OPEN = "transaction_open"
    REQUEST_EXPIRED = "request_expired"
    INVALID_RECEIPT = "invalid_receipt"
    JOB_UNAVAILABLE = "job_unavailable"
    JOB_MISMATCH = "job_mismatch"
    JOB_FAILED = "job_failed"
    LOCAL_RUNTIME_MISMATCH = "local_runtime_mismatch"
    CLOCK_REGRESSION = "clock_regression"


class CohortJobInspectionError(RuntimeError):
    """Fixed refusal without endpoint, RuntimeEnv, server messages or secrets."""

    def __init__(self, reason: CohortJobInspectionReason) -> None:
        if type(reason) is not CohortJobInspectionReason:
            raise TypeError("invalid cohort job inspection reason")
        self.reason = reason
        super().__init__(f"Cohort job inspection refused: {reason.value}")


@dataclass(frozen=True, slots=True)
class CohortJobReservationSnapshot:
    """Detached stored bindings; publication must reread their current rows."""

    request: CohortProbeJobRequest = field(repr=False)
    request_digest: str
    jobs_endpoint: str = field(repr=False)
    entrypoint_digest: str
    submitted_runtime_env_digest: str
    receipt_json: str = field(repr=False)
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class InspectedCohortJobReceipt:
    """A successful endpoint query, still subject to atomic storage validation."""

    reservation: CohortJobReservationSnapshot = field(repr=False)
    receipt: CohortJobReceipt = field(repr=False)
    inspected_at: datetime


def _reject(reason: CohortJobInspectionReason) -> Never:
    raise CohortJobInspectionError(reason) from None


def cohort_probe_entrypoint_digest(entrypoint: object) -> str:
    try:
        if (
            type(entrypoint) is not str
            or not entrypoint
            or any(ord(character) < 32 or ord(character) == 127 for character in entrypoint)
        ):
            raise ValueError
        encoded = entrypoint.encode("utf-8")
        if len(encoded) > COHORT_PROBE_ENTRYPOINT_MAX_BYTES:
            raise ValueError
        return "sha256:" + hashlib.sha256(_ENTRYPOINT_DOMAIN + encoded).hexdigest()
    except (TypeError, ValueError):
        _reject(CohortJobInspectionReason.INVALID_RESERVATION)


def cohort_probe_submitted_runtime_env_digest(runtime_env: object) -> str:
    """Bind the exact normalized/uploaded mapping passed to Jobs submission.

    This transport checksum is not the producer's logical RuntimeEnv identity.
    Call it after local paths have been uploaded and Ray has normalized the
    submitted mapping. Raw mapping values must never enter diagnostics.
    """
    try:
        if type(runtime_env) is not dict:
            raise ValueError
        _validate_json_tree(
            runtime_env,
            allow_nonfinite=False,
            allow_nul=False,
            max_depth=16,
            max_nodes=4096,
            max_string_bytes=COHORT_PROBE_RUNTIME_ENV_MAX_BYTES,
        )
        serialized = _bounded_json_dumps(
            runtime_env, sort_keys=True, max_bytes=COHORT_PROBE_RUNTIME_ENV_MAX_BYTES
        )
        return (
            "sha256:" + hashlib.sha256(_RUNTIME_ENV_DOMAIN + serialized.encode("utf-8")).hexdigest()
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        _reject(CohortJobInspectionReason.INVALID_RESERVATION)


def _now() -> datetime:
    return datetime.now(UTC)


def _outside_transactions() -> None:
    from django.db import connections

    for connection in connections.all(initialized_only=True):
        if connection.in_atomic_block or (
            connection.connection is not None and not connection.get_autocommit()
        ):
            _reject(CohortJobInspectionReason.TRANSACTION_OPEN)


def _validate_snapshot(snapshot: CohortJobReservationSnapshot) -> CohortProbeJobRequest:
    try:
        if type(snapshot) is not CohortJobReservationSnapshot:
            raise ValueError
        request = decode_probe_job_request(encode_probe_job_request(snapshot.request))
        if probe_job_request_digest(request) != _digest(snapshot.request_digest):
            raise ValueError
        endpoint = _endpoint(snapshot.jobs_endpoint)
        if urlsplit(endpoint).scheme not in {"http", "https"} or any(
            marker in endpoint for marker in ("?", "#")
        ):
            raise ValueError
        _digest(snapshot.entrypoint_digest)
        _digest(snapshot.submitted_runtime_env_digest)
        _digest(snapshot.receipt_digest)
        return request
    except (TypeError, ValueError, RuntimeError):
        _reject(CohortJobInspectionReason.INVALID_RESERVATION)


def inspect_reserved_cohort_job(
    snapshot: CohortJobReservationSnapshot,
) -> InspectedCohortJobReceipt | None:
    """Fetch the owned submission and corroborate one stored probe receipt.

    Pending/running Jobs return no proof. All terminal transport failures or
    contradictory observations refuse publication; none is a task outcome.
    The fixed trusted driver, authenticated database receipt, owned submission
    reservation and actual authenticated endpoint query form this boundary.
    Native driver IDs are corroboration, not independent authentication: Ray
    derives driver-to-submission association from its mutable job metadata.
    """
    _outside_transactions()
    request = _validate_snapshot(snapshot)
    began = _now()
    if not request.issued_at <= began < request.expires_at:
        _reject(CohortJobInspectionReason.REQUEST_EXPIRED)
    try:
        from django_ray.target.cohort_job_receipt import decode_cohort_job_receipt

        receipt = decode_cohort_job_receipt(
            snapshot.receipt_json,
            expected_request=request,
            expected_request_digest=snapshot.request_digest,
            expected_submission_id=probe_job_submission_id(request),
            expected_receipt_digest=snapshot.receipt_digest,
        )
        if receipt.collected_at > began:
            raise ValueError
        compare_ray_target_attestation(
            receipt.attestation.expectation, receipt.attestation, now=began
        )
    except (TypeError, ValueError, RuntimeError):
        _reject(CohortJobInspectionReason.INVALID_RECEIPT)

    try:
        import ray
        from ray.dashboard.modules.job.common import JobStatus
        from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

        from django_ray.target.cohort_job_http import fetch_reserved_cohort_job_details
        from django_ray.target.cohort_runtime import _local_runtime
        from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

        package, runtime = _local_runtime(ray)
        if (
            package != request.expected_package_version
            or runtime != request.expected_runtime
            or ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION
        ):
            _reject(CohortJobInspectionReason.LOCAL_RUNTIME_MISMATCH)

        details = fetch_reserved_cohort_job_details(
            snapshot.jobs_endpoint, probe_job_submission_id(request)
        )
    except CohortJobInspectionError:
        raise
    except Exception:
        _reject(CohortJobInspectionReason.JOB_UNAVAILABLE)
    finished = _now()
    if finished < began:
        _reject(CohortJobInspectionReason.CLOCK_REGRESSION)
    if finished >= request.expires_at:
        _reject(CohortJobInspectionReason.REQUEST_EXPIRED)
    try:
        if (
            type(details) is not JobDetails
            or details.type is not JobType.SUBMISSION
            or details.submission_id != probe_job_submission_id(request)
            or details.metadata != probe_job_metadata(request)
            or cohort_probe_entrypoint_digest(details.entrypoint) != snapshot.entrypoint_digest
            or cohort_probe_submitted_runtime_env_digest(details.runtime_env)
            != snapshot.submitted_runtime_env_digest
        ):
            _reject(CohortJobInspectionReason.JOB_MISMATCH)
        if details.status in {JobStatus.PENDING, JobStatus.RUNNING}:
            return None
        if details.status is not JobStatus.SUCCEEDED:
            _reject(CohortJobInspectionReason.JOB_FAILED)
        if details.job_id != receipt.native_job_id or (
            details.driver_info is not None and details.driver_info.id != receipt.native_job_id
        ):
            _reject(CohortJobInspectionReason.JOB_MISMATCH)
        compare_ray_target_attestation(
            receipt.attestation.expectation, receipt.attestation, now=finished
        )
    except CohortJobInspectionError:
        raise
    except (TypeError, ValueError, RuntimeError):
        _reject(CohortJobInspectionReason.JOB_MISMATCH)
    return InspectedCohortJobReceipt(snapshot, receipt, finished)
