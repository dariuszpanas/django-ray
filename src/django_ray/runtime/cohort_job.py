"""Dormant, fixed Jobs carrier for a pending current-cohort probe.

The digest binds independently delivered Jobs metadata to one challenge and
submission, not to a generic application entrypoint. It is integrity evidence,
not authorization. Only the separate database completion transaction may
consume the exact live lease/nonce and publish positive target capability.
That nonce remains with the issuing manager and is never part of this remote
carrier. A future driver receipt can carry observations, not consumption
authority.

No command-line entrypoint, Django import, application input, database write,
or success log is provided here. The manager must separately bound and stop
the known probe Job: Ray initialization itself has no reliable deadline.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Never
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version

from django_ray.target.attestation import (
    RayClusterAttestation,
    RayRunnerFamily,
    RayRuntimeVersion,
    RayTargetExpectation,
    compare_ray_target_attestation,
    decode_ray_target_expectation,
    encode_ray_target_expectation,
)
from django_ray.target.cohort_probe import derive_cohort_target_key

COHORT_PROBE_JOB_SCHEMA = "django-ray.cohort-probe-job"
COHORT_PROBE_JOB_MAX_BYTES = 8192
COHORT_PROBE_JOB_METADATA_KIND = "django_ray_cohort_probe"
COHORT_PROBE_JOB_METADATA_DIGEST = "django_ray_cohort_probe_digest"
COHORT_PROBE_JOB_SCHEMA_VERSION = 2
_DOMAIN = b"django-ray/cohort-probe-job/v2\x00"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "challenge_id",
        "challenge_revision",
        "lease",
        "configuration_digest",
        "target_key",
        "runner_family",
        "expected_package_version",
        "expected_runtime",
        "expected_cluster_session",
        "expected_target_policy_id",
        "policy_revision",
        "issued_at",
        "expires_at",
    }
)
_LEASE_KEYS = frozenset({"worker_id", "hostname", "pid", "started_at"})
_RUNTIME_KEYS = frozenset(asdict(RayRuntimeVersion(0, 0, 0, "cpython", 0, 0, 0)))


class CohortProbeJobReason(StrEnum):
    INVALID = "invalid"
    RESOURCE_LIMIT = "resource_limit"
    NONCANONICAL = "noncanonical"
    METADATA_MISMATCH = "metadata_mismatch"
    INVALID_GCS_ADDRESS = "invalid_gcs_address"
    NOT_YET_VALID = "not_yet_valid"
    EXPIRED = "expired"
    CLOCK_REGRESSION = "clock_regression"
    RUNTIME_MISMATCH = "runtime_mismatch"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    EXISTING_CONNECTION = "existing_connection"
    PROBE_FAILED = "probe_failed"


class CohortProbeJobError(ValueError):
    """Fixed failures that never expose carrier, nonce, metadata, or endpoints."""

    def __init__(self, reason: CohortProbeJobReason) -> None:
        self.reason = reason
        super().__init__(f"Cohort probe Job refused: {reason.value}")


@dataclass(frozen=True, slots=True)
class CohortProbeJobLease:
    """Exact lease snapshot, without importing the Django-backed lease service."""

    worker_id: str
    hostname: str
    pid: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class CohortProbeJobRequest:
    """Schema-two discovery binds the key derivation rule, never a chosen key.

    First discovery has null key/session/policy and policy revision one. A
    refresh carries the exact retained key, session and current policy. Draft
    schema-one carriers are rejected rather than reinterpreted or rehashed.
    """

    challenge_id: int
    challenge_revision: int
    lease: CohortProbeJobLease
    configuration_digest: str
    target_key: str | None
    runner_family: RayRunnerFamily
    expected_package_version: str
    expected_runtime: RayRuntimeVersion
    expected_cluster_session: str | None
    expected_target_policy_id: int | None
    policy_revision: int
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedCohortJobProbe:
    """Collected evidence only; never a durable claim or permission receipt."""

    request: CohortProbeJobRequest = field(repr=False)
    request_digest: str
    submission_id: str
    attestation: RayClusterAttestation
    native_job_id: str
    observed_package_version: str
    collected_at: datetime


def _reject(reason: CohortProbeJobReason) -> Never:
    raise CohortProbeJobError(reason) from None


def _positive(value: object, *, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        _reject(CohortProbeJobReason.INVALID)
    return value


def _text(value: object, *, maximum: int = 255) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        _reject(CohortProbeJobReason.INVALID)
    return value


def _timestamp(value: object) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        _reject(CohortProbeJobReason.INVALID)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str or len(value) != 27:
        _reject(CohortProbeJobReason.INVALID)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _reject(CohortProbeJobReason.INVALID)
    if _timestamp(parsed) != value:
        _reject(CohortProbeJobReason.NONCANONICAL)
    return parsed


def _expectation(request: CohortProbeJobRequest, session: str) -> RayTargetExpectation:
    return RayTargetExpectation(
        request.target_key
        if request.target_key is not None
        else derive_cohort_target_key(request.runner_family, session),
        request.runner_family,
        session,
        request.policy_revision,
        request.expected_runtime,
    )


def _wire(request: CohortProbeJobRequest) -> dict[str, object]:
    if (
        type(request) is not CohortProbeJobRequest
        or type(request.lease) is not CohortProbeJobLease
        or type(request.expected_runtime) is not RayRuntimeVersion
        or request.runner_family is not RayRunnerFamily.RAY_JOB
        or type(request.configuration_digest) is not str
        or _DIGEST.fullmatch(request.configuration_digest) is None
    ):
        _reject(CohortProbeJobReason.INVALID)
    version = _text(request.expected_package_version, maximum=128)
    try:
        if str(Version(version)) != version:
            _reject(CohortProbeJobReason.NONCANONICAL)
        # Reuse the exact bounded runtime/session/target contract, with a local
        # validation placeholder only when first discovery has no session yet.
        expected = _expectation(request, request.expected_cluster_session or "session_validation")
        decode_ray_target_expectation(encode_ray_target_expectation(expected))
    except CohortProbeJobError:
        raise
    except (InvalidVersion, TypeError, ValueError):
        _reject(CohortProbeJobReason.INVALID)
    if request.expected_cluster_session is None:
        if (
            request.target_key is not None
            or request.expected_target_policy_id is not None
            or request.policy_revision != 1
        ):
            _reject(CohortProbeJobReason.INVALID)
    else:
        if (
            type(request.expected_cluster_session) is not str
            or not request.expected_cluster_session
            or request.target_key is None
        ):
            _reject(CohortProbeJobReason.INVALID)
        _positive(request.expected_target_policy_id)
    lease = request.lease
    issued_at, expires_at = _timestamp(request.issued_at), _timestamp(request.expires_at)
    started_at = _timestamp(lease.started_at)
    if (
        not timedelta(0) < request.expires_at - request.issued_at <= timedelta(seconds=600)
        or lease.started_at > request.issued_at
    ):
        _reject(CohortProbeJobReason.INVALID)
    return {
        "schema": COHORT_PROBE_JOB_SCHEMA,
        "schema_version": COHORT_PROBE_JOB_SCHEMA_VERSION,
        "challenge_id": _positive(request.challenge_id),
        "challenge_revision": _positive(request.challenge_revision),
        "lease": {
            "worker_id": _text(lease.worker_id),
            "hostname": _text(lease.hostname),
            "pid": _positive(lease.pid, maximum=(1 << 31) - 1),
            "started_at": started_at,
        },
        "configuration_digest": request.configuration_digest,
        "target_key": request.target_key,
        "runner_family": request.runner_family.value,
        "expected_package_version": version,
        "expected_runtime": asdict(request.expected_runtime),
        "expected_cluster_session": request.expected_cluster_session,
        "expected_target_policy_id": request.expected_target_policy_id,
        "policy_revision": _positive(request.policy_revision),
        "issued_at": issued_at,
        "expires_at": expires_at,
    }


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _bounded(serialized: object, max_bytes: int, *, upper: int) -> str:
    if type(max_bytes) is not int or not 1 <= max_bytes <= upper:
        _reject(CohortProbeJobReason.RESOURCE_LIMIT)
    if type(serialized) is not str:
        _reject(CohortProbeJobReason.INVALID)
    if len(serialized) > max_bytes:
        _reject(CohortProbeJobReason.RESOURCE_LIMIT)
    try:
        if len(serialized.encode("utf-8")) > max_bytes:
            _reject(CohortProbeJobReason.RESOURCE_LIMIT)
    except UnicodeError:
        _reject(CohortProbeJobReason.INVALID)
    return serialized


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject(CohortProbeJobReason.INVALID)
        result[key] = value
    return result


def _parse(serialized: str) -> dict[str, object]:
    try:
        value = json.loads(serialized, object_pairs_hook=_object)
    except (ValueError, RecursionError):
        _reject(CohortProbeJobReason.INVALID)
    if type(value) is not dict:
        _reject(CohortProbeJobReason.INVALID)
    return value


def encode_probe_job_request(
    request: CohortProbeJobRequest,
    *,
    max_bytes: int = COHORT_PROBE_JOB_MAX_BYTES,
) -> str:
    """Encode bounded probe bindings without the manager's consumption nonce."""
    return _bounded(_canonical(_wire(request)), max_bytes, upper=COHORT_PROBE_JOB_MAX_BYTES)


def decode_probe_job_request(
    serialized: object,
    *,
    max_bytes: int = COHORT_PROBE_JOB_MAX_BYTES,
) -> CohortProbeJobRequest:
    serialized = _bounded(serialized, max_bytes, upper=COHORT_PROBE_JOB_MAX_BYTES)
    body = _parse(serialized)
    lease, runtime = body.get("lease"), body.get("expected_runtime")
    if (
        body.keys() != _KEYS
        or body.get("schema") != COHORT_PROBE_JOB_SCHEMA
        or type(body.get("schema_version")) is not int
        or body["schema_version"] != COHORT_PROBE_JOB_SCHEMA_VERSION
        or type(lease) is not dict
        or lease.keys() != _LEASE_KEYS
        or type(runtime) is not dict
        or runtime.keys() != _RUNTIME_KEYS
    ):
        _reject(CohortProbeJobReason.INVALID)
    try:
        request = CohortProbeJobRequest(
            challenge_id=body["challenge_id"],
            challenge_revision=body["challenge_revision"],
            lease=CohortProbeJobLease(
                lease["worker_id"],
                lease["hostname"],
                lease["pid"],
                _parse_timestamp(lease["started_at"]),
            ),
            configuration_digest=body["configuration_digest"],
            target_key=body["target_key"],
            runner_family=RayRunnerFamily(body["runner_family"]),
            expected_package_version=body["expected_package_version"],
            expected_runtime=RayRuntimeVersion(**runtime),
            expected_cluster_session=body["expected_cluster_session"],
            expected_target_policy_id=body["expected_target_policy_id"],
            policy_revision=body["policy_revision"],
            issued_at=_parse_timestamp(body["issued_at"]),
            expires_at=_parse_timestamp(body["expires_at"]),
        )
    except (TypeError, ValueError):
        _reject(CohortProbeJobReason.INVALID)
    if encode_probe_job_request(request, max_bytes=max_bytes) != serialized:
        _reject(CohortProbeJobReason.NONCANONICAL)
    return request


def probe_job_request_digest(request: CohortProbeJobRequest) -> str:
    return (
        "sha256:" + hashlib.sha256(_DOMAIN + encode_probe_job_request(request).encode()).hexdigest()
    )


def probe_job_metadata(request: CohortProbeJobRequest) -> dict[str, str]:
    """Fixed custom metadata; never override Ray's reserved submission ID."""
    return {
        COHORT_PROBE_JOB_METADATA_KIND: "1",
        COHORT_PROBE_JOB_METADATA_DIGEST: probe_job_request_digest(request),
    }


def probe_job_submission_id(request: CohortProbeJobRequest) -> str:
    """Manager's deterministic Jobs API submission handle, separate from metadata."""
    return "django-ray-cohort-probe-" + probe_job_request_digest(request).removeprefix("sha256:")


def _verify_metadata(
    request: CohortProbeJobRequest,
    submission_id: str,
    metadata: Mapping[str, str],
    environment: Mapping[str, str],
) -> dict[str, str]:
    expected = probe_job_metadata(request)
    if (
        type(submission_id) is not str
        or submission_id != probe_job_submission_id(request)
        or not isinstance(metadata, Mapping)
        or metadata.keys() != expected.keys()
        or any(metadata.get(key) != value for key, value in expected.items())
    ):
        _reject(CohortProbeJobReason.METADATA_MISMATCH)
    if not isinstance(environment, Mapping):
        _reject(CohortProbeJobReason.METADATA_MISMATCH)
    try:
        # Ray JobSupervisor supplies this separately from the entrypoint. Do
        # not accept caller metadata synthesized solely from the carrier.
        config = _parse(
            _bounded(environment.get("RAY_JOB_CONFIG_JSON_ENV_VAR"), 65536, upper=65536)
        )
        injected = config.get("metadata")
        if (
            config.keys() != {"metadata", "runtime_env"}
            or type(config["runtime_env"]) is not dict
            or type(injected) is not dict
            or any(injected.get(key) != value for key, value in expected.items())
            or injected.get("job_submission_id") != submission_id
        ):
            _reject(CohortProbeJobReason.METADATA_MISMATCH)
    except CohortProbeJobError:
        _reject(CohortProbeJobReason.METADATA_MISMATCH)
    return expected


def _gcs_address(environment: Mapping[str, str]) -> str:
    value = environment.get("RAY_ADDRESS")
    try:
        value = _text(value)
        parsed = urlsplit("//" + value)
        # JobSupervisor injects a resolved GCS host:port, never "auto", a
        # dashboard HTTP URL, a Ray Client URI, userinfo, or an application path.
        if (
            "://" in value
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or not parsed.hostname
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or (":" not in parsed.hostname and _HOST.fullmatch(parsed.hostname) is None)
        ):
            _reject(CohortProbeJobReason.INVALID_GCS_ADDRESS)
    except (ValueError, TypeError):
        _reject(CohortProbeJobReason.INVALID_GCS_ADDRESS)
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _within_window(request: CohortProbeJobRequest, *, previous: datetime | None = None) -> datetime:
    now = _now()
    _timestamp(now)
    if previous is not None and now < previous:
        _reject(CohortProbeJobReason.CLOCK_REGRESSION)
    if now < request.issued_at:
        _reject(CohortProbeJobReason.NOT_YET_VALID)
    if now >= request.expires_at:
        _reject(CohortProbeJobReason.EXPIRED)
    return now


def collect_verified_probe(
    serialized_request: object,
    *,
    jobs_submission_id: str,
    jobs_metadata: Mapping[str, str],
    environment: Mapping[str, str],
) -> VerifiedCohortJobProbe:
    """Collect one request-bound proof in a fresh Jobs driver, without Django.

    ``jobs_submission_id`` and ``jobs_metadata`` must come from the actual
    independently held Jobs API record/known submission handle, never from
    the carrier or driver environment. Caller-supplied records and metadata
    alone are not provenance. ``environment``
    must be the Jobs driver's injected environment, not manager configuration.
    Eventual publication must bind that actual remote Job/known handle outside
    database locks; Ray permits arbitrary user metadata to override its own
    reserved submission ID, which this fixed metadata builder never does.
    The eventual completion service must recheck all database bindings and the
    deadline atomically; neither this value nor a successful Job is permission.
    """
    request = decode_probe_job_request(serialized_request)
    metadata = _verify_metadata(request, jobs_submission_id, jobs_metadata, environment)
    address = _gcs_address(environment)
    began = _within_window(request)
    from django_ray import __version__

    if __version__ != request.expected_package_version:
        _reject(CohortProbeJobReason.RUNTIME_MISMATCH)
    try:
        import ray

        from django_ray.target.cohort_runtime import _local_runtime

        actual_package, actual_runtime = _local_runtime(ray)
        if (
            actual_package != request.expected_package_version
            or actual_runtime != request.expected_runtime
        ):
            _reject(CohortProbeJobReason.RUNTIME_MISMATCH)
        # The existing private collector is qualified for this exact Ray only.
        from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

        if ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION:
            _reject(CohortProbeJobReason.RUNTIME_MISMATCH)
        initialized = ray.is_initialized()
        if initialized is True:
            _reject(CohortProbeJobReason.EXISTING_CONNECTION)
        if initialized is not False:
            _reject(CohortProbeJobReason.RUNTIME_UNAVAILABLE)
    except CohortProbeJobError:
        raise
    except Exception:
        _reject(CohortProbeJobReason.RUNTIME_UNAVAILABLE)
    try:
        try:
            ray.init(address=address, log_to_driver=False)
            connected = _within_window(request, previous=began)
            from django_ray.target.cohort_probe import observe_current_cohort_target

            attestation = observe_current_cohort_target(
                target_key=request.target_key,
                runner_family=request.runner_family,
                expected_django_ray_version=request.expected_package_version,
                expected_runtime=request.expected_runtime,
                expected_cluster_session=request.expected_cluster_session,
                policy_revision=request.policy_revision,
                timeout_seconds=min(30.0, (request.expires_at - connected).total_seconds()),
                max_nodes=64,
            )
            finished = _within_window(request, previous=connected)
            if (
                type(attestation) is not RayClusterAttestation
                or attestation.observed_at < connected
            ):
                _reject(CohortProbeJobReason.PROBE_FAILED)
            expected = _expectation(
                request,
                request.expected_cluster_session or attestation.expectation.cluster_session,
            )
            compare_ray_target_attestation(expected, attestation, now=finished)
            from django_ray.target.cohort_job_receipt import is_canonical_native_ray_job_id

            # Capture the actual connected driver's identity before cleanup.
            # JobSupervisor metadata may contain an arbitrary submission echo.
            native_job_id = ray.get_runtime_context().get_job_id()
            if not is_canonical_native_ray_job_id(native_job_id):
                _reject(CohortProbeJobReason.PROBE_FAILED)
        finally:
            # This function alone owns this fresh connection, including cleanup
            # after partial initialization. Never shut down an existing driver.
            ray.shutdown()
    except CohortProbeJobError:
        raise
    except Exception:
        _reject(CohortProbeJobReason.PROBE_FAILED)
    returned_at = _within_window(request, previous=finished)
    try:
        compare_ray_target_attestation(expected, attestation, now=returned_at)
    except ValueError:
        _reject(CohortProbeJobReason.PROBE_FAILED)
    return VerifiedCohortJobProbe(
        request,
        metadata[COHORT_PROBE_JOB_METADATA_DIGEST],
        jobs_submission_id,
        attestation,
        native_job_id,
        actual_package,
        returned_at,
    )
