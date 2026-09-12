"""Dormant fixed Jobs probe driver; a pending receipt never grants eligibility.

The manager must reserve this exact command and uploaded/normalized RuntimeEnv
before submission, using a qualified fixed probe profile. Package versions and
transport digests do not attest imported source bytes or protect interpreter or
dependency/setup hooks which execute before this module. Ordinary RuntimeEnv
semantics are unchanged. The manager retains the consumption nonce and owns the
external deadline and exact Job cancellation; DNS, Ray initialization/cleanup,
and Django setup cannot all be forcibly interrupted by this process.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Never

from django_ray.execution_codec import _preparse_json_scan, _unique_object, _validate_json_tree
from django_ray.runtime.cohort_job import (
    CohortProbeJobRequest,
    decode_probe_job_request,
    encode_probe_job_request,
    probe_job_metadata,
    probe_job_request_digest,
    probe_job_submission_id,
)
from django_ray.target.cohort_intent import _digest
from django_ray.target.cohort_job_control import (
    cohort_probe_entrypoint_digest,
    cohort_probe_submitted_runtime_env_digest,
)
from django_ray.target.cohort_job_http import _arguments

if TYPE_CHECKING:
    from ray.dashboard.modules.job.pydantic_models import JobDetails

    from django_ray.target.cohort_job_receipt import CohortJobReceipt

COHORT_PROBE_JOB_LAUNCH_SCHEMA = "django-ray.cohort-probe-job-launch"
COHORT_PROBE_JOB_LAUNCH_MAX_BYTES = 10 * 1024
_ARGUMENT_MAX_BYTES = (COHORT_PROBE_JOB_LAUNCH_MAX_BYTES * 4 + 2) // 3
_COMMAND = "python -m django_ray.runtime.cohort_job_entrypoint --probe-launch-b64 "
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_BASE64 = re.compile(r"[A-Za-z0-9_-]+")
_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "request",
        "request_digest",
        "jobs_endpoint",
        "submitted_runtime_env_digest",
        "django_settings_module",
    }
)


class CohortJobEntrypointReason(StrEnum):
    INVALID_LAUNCH = "invalid_launch"
    RESOURCE_LIMIT = "resource_limit"
    NONCANONICAL = "noncanonical"
    DJANGO_ALREADY_IMPORTED = "django_already_imported"
    SETTINGS_MISMATCH = "settings_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    EXISTING_CONNECTION = "existing_connection"
    JOB_UNAVAILABLE = "job_unavailable"
    JOB_MISMATCH = "job_mismatch"
    PROBE_FAILED = "probe_failed"
    REQUEST_EXPIRED = "request_expired"
    CLOCK_REGRESSION = "clock_regression"
    CLOCK_UNAVAILABLE = "clock_unavailable"
    BOOTSTRAP_FAILED = "bootstrap_failed"
    RECEIPT_REFUSED = "receipt_refused"
    UNEXPECTED_FAILURE = "unexpected_failure"
    INTERRUPTED = "interrupted"


class CohortJobEntrypointError(RuntimeError):
    """A fixed failure without rejected argv, endpoint, settings, or exception text."""

    def __init__(self, reason: CohortJobEntrypointReason) -> None:
        if type(reason) is not CohortJobEntrypointReason:
            raise TypeError("invalid cohort Job entrypoint reason")
        self.reason = reason
        super().__init__(f"Cohort probe driver refused: {reason.value}")


@dataclass(frozen=True, slots=True)
class CohortProbeJobLaunch:
    request: CohortProbeJobRequest = field(repr=False)
    request_digest: str
    jobs_endpoint: str = field(repr=False)
    submitted_runtime_env_digest: str
    django_settings_module: str = field(repr=False)


def _reject(reason: CohortJobEntrypointReason) -> Never:
    raise CohortJobEntrypointError(reason) from None


def _bounded(serialized: object, max_bytes: int) -> str:
    if type(max_bytes) is not int or not 1 <= max_bytes <= COHORT_PROBE_JOB_LAUNCH_MAX_BYTES:
        _reject(CohortJobEntrypointReason.RESOURCE_LIMIT)
    if type(serialized) is not str:
        _reject(CohortJobEntrypointReason.INVALID_LAUNCH)
    if len(serialized) > max_bytes or len(serialized.encode("utf-8")) > max_bytes:
        _reject(CohortJobEntrypointReason.RESOURCE_LIMIT)
    return serialized


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def encode_probe_job_launch(
    launch: CohortProbeJobLaunch, *, max_bytes: int = COHORT_PROBE_JOB_LAUNCH_MAX_BYTES
) -> str:
    """Encode only launch bindings; the manager nonce never leaves its owner."""
    try:
        if type(launch) is not CohortProbeJobLaunch:
            _reject(CohortJobEntrypointReason.INVALID_LAUNCH)
        request_json = encode_probe_job_request(launch.request)
        if _digest(launch.request_digest) != probe_job_request_digest(launch.request):
            _reject(CohortJobEntrypointReason.INVALID_LAUNCH)
        _digest(launch.submitted_runtime_env_digest)
        # Reuse the HTTP transport's independent endpoint and owned-handle policy.
        # Preserve spelling in the launch rather than adopting its URL join form.
        _arguments(launch.jobs_endpoint, probe_job_submission_id(launch.request), 5.0)
        module = launch.django_settings_module
        if type(module) is not str or len(module) > 255 or _MODULE.fullmatch(module) is None:
            _reject(CohortJobEntrypointReason.INVALID_LAUNCH)
        return _bounded(
            _canonical(
                {
                    "schema": COHORT_PROBE_JOB_LAUNCH_SCHEMA,
                    "schema_version": 1,
                    "request": json.loads(request_json),
                    "request_digest": launch.request_digest,
                    "jobs_endpoint": launch.jobs_endpoint,
                    "submitted_runtime_env_digest": launch.submitted_runtime_env_digest,
                    "django_settings_module": module,
                }
            ),
            max_bytes,
        )
    except CohortJobEntrypointError:
        raise
    except (TypeError, ValueError, RuntimeError, AttributeError, RecursionError):
        _reject(CohortJobEntrypointReason.INVALID_LAUNCH)


def _reject_number(_value: str) -> Never:
    _reject(CohortJobEntrypointReason.INVALID_LAUNCH)


def decode_probe_job_launch(
    serialized: object, *, max_bytes: int = COHORT_PROBE_JOB_LAUNCH_MAX_BYTES
) -> CohortProbeJobLaunch:
    try:
        serialized = _bounded(serialized, max_bytes)
        _preparse_json_scan(
            serialized, reserved_marker_keys=frozenset({"schema"}), max_depth=8, max_nodes=512
        )
        body = json.loads(
            serialized,
            object_pairs_hook=_unique_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        _validate_json_tree(
            body, allow_nonfinite=False, allow_nul=False, max_depth=8, max_nodes=512
        )
        if (
            type(body) is not dict
            or body.keys() != _KEYS
            or body["schema"] != COHORT_PROBE_JOB_LAUNCH_SCHEMA
            or type(body["schema_version"]) is not int
            or body["schema_version"] != 1
        ):
            _reject(CohortJobEntrypointReason.INVALID_LAUNCH)
        launch = CohortProbeJobLaunch(
            request=decode_probe_job_request(_canonical(body["request"])),
            request_digest=body["request_digest"],
            jobs_endpoint=body["jobs_endpoint"],
            submitted_runtime_env_digest=body["submitted_runtime_env_digest"],
            django_settings_module=body["django_settings_module"],
        )
        if encode_probe_job_launch(launch, max_bytes=max_bytes) != serialized:
            _reject(CohortJobEntrypointReason.NONCANONICAL)
        return launch
    except CohortJobEntrypointError:
        raise
    except (TypeError, ValueError, RuntimeError, AttributeError, RecursionError):
        _reject(CohortJobEntrypointReason.INVALID_LAUNCH)


def probe_job_launch_entrypoint(launch: CohortProbeJobLaunch) -> str:
    encoded = base64.urlsafe_b64encode(encode_probe_job_launch(launch).encode("ascii"))
    command = _COMMAND + encoded.decode("ascii").rstrip("=")
    cohort_probe_entrypoint_digest(command)
    return command


def parse_probe_job_launch_argv(argv: Sequence[str]) -> CohortProbeJobLaunch:
    try:
        if (
            type(argv) not in {list, tuple}
            or len(argv) != 2
            or type(argv[0]) is not str
            or argv[0] != "--probe-launch-b64"
            or type(argv[1]) is not str
        ):
            _reject(CohortJobEntrypointReason.INVALID_LAUNCH)
        encoded = argv[1]
        if len(encoded) > _ARGUMENT_MAX_BYTES:
            _reject(CohortJobEntrypointReason.RESOURCE_LIMIT)
        if _BASE64.fullmatch(encoded) is None:
            _reject(CohortJobEntrypointReason.INVALID_LAUNCH)
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
            _reject(CohortJobEntrypointReason.NONCANONICAL)
        return decode_probe_job_launch(raw.decode("utf-8"))
    except CohortJobEntrypointError:
        raise
    except (TypeError, ValueError, IndexError):
        _reject(CohortJobEntrypointReason.INVALID_LAUNCH)


def _now() -> datetime:
    return datetime.now(UTC)


class _Window:
    def __init__(self, request: CohortProbeJobRequest) -> None:
        self.request = request
        self.last_wall: datetime | None = None
        self.last_monotonic: float | None = None
        self.deadline: float | None = None
        self.check()

    def check(self) -> datetime:
        now, current = _now(), time.monotonic()
        if (
            type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or type(current) not in {float, int}
            or not math.isfinite(current)
        ):
            _reject(CohortJobEntrypointReason.CLOCK_UNAVAILABLE)
        if (self.last_wall is not None and now < self.last_wall) or (
            self.last_monotonic is not None and current < self.last_monotonic
        ):
            _reject(CohortJobEntrypointReason.CLOCK_REGRESSION)
        if not self.request.issued_at <= now < self.request.expires_at or (
            self.deadline is not None and current >= self.deadline
        ):
            _reject(CohortJobEntrypointReason.REQUEST_EXPIRED)
        if self.deadline is None:
            self.deadline = current + (self.request.expires_at - now).total_seconds()
        self.last_wall, self.last_monotonic = now, current
        return now

    def remaining(self) -> float:
        now = self.check()
        assert self.deadline is not None and self.last_monotonic is not None
        return min(
            (self.request.expires_at - now).total_seconds(), self.deadline - self.last_monotonic
        )


def _ensure_no_django() -> None:
    if any(name == "django" or name.startswith("django.") for name in sys.modules):
        _reject(CohortJobEntrypointReason.DJANGO_ALREADY_IMPORTED)


def _settings_environment(launch: CohortProbeJobLaunch) -> None:
    if os.environ.get("DJANGO_SETTINGS_MODULE") != launch.django_settings_module:
        _reject(CohortJobEntrypointReason.SETTINGS_MISMATCH)


def _runtime_preflight(launch: CohortProbeJobLaunch) -> None:
    from django_ray import __version__

    if __version__ != launch.request.expected_package_version:
        _reject(CohortJobEntrypointReason.RUNTIME_MISMATCH)
    try:
        import ray

        from django_ray.target.cohort_runtime import _local_runtime
        from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

        package, runtime = _local_runtime(ray)
        if (
            package != launch.request.expected_package_version
            or runtime != launch.request.expected_runtime
            or ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION
        ):
            _reject(CohortJobEntrypointReason.RUNTIME_MISMATCH)
        initialized = ray.is_initialized()
        if initialized is True:
            _reject(CohortJobEntrypointReason.EXISTING_CONNECTION)
        if initialized is not False:
            _reject(CohortJobEntrypointReason.RUNTIME_UNAVAILABLE)
    except CohortJobEntrypointError:
        raise
    except Exception:
        _reject(CohortJobEntrypointReason.RUNTIME_UNAVAILABLE)


def _fetch_running_job(
    launch: CohortProbeJobLaunch, window: _Window, *, native_job_id: str | None = None
) -> JobDetails:
    try:
        from ray.dashboard.modules.job.common import JobStatus
        from ray.dashboard.modules.job.pydantic_models import JobDetails, JobType

        from django_ray.target.cohort_job_http import fetch_reserved_cohort_job_details

        details = fetch_reserved_cohort_job_details(
            launch.jobs_endpoint,
            probe_job_submission_id(launch.request),
            timeout_seconds=min(5.0, window.remaining()),
        )
    except CohortJobEntrypointError:
        raise
    except Exception:
        _reject(CohortJobEntrypointReason.JOB_UNAVAILABLE)
    window.check()
    try:
        if (
            type(details) is not JobDetails
            or details.type is not JobType.SUBMISSION
            or details.status is not JobStatus.RUNNING
            or details.submission_id != probe_job_submission_id(launch.request)
            or details.metadata != probe_job_metadata(launch.request)
            or details.entrypoint != probe_job_launch_entrypoint(launch)
            or type(details.runtime_env) is not dict
            or cohort_probe_submitted_runtime_env_digest(details.runtime_env)
            != launch.submitted_runtime_env_digest
            or type(details.runtime_env.get("env_vars")) is not dict
            or details.runtime_env["env_vars"].get("DJANGO_SETTINGS_MODULE")
            != launch.django_settings_module
            or (
                native_job_id is not None
                and (
                    details.job_id != native_job_id
                    or (details.driver_info is not None and details.driver_info.id != native_job_id)
                )
            )
        ):
            _reject(CohortJobEntrypointReason.JOB_MISMATCH)
        return details
    except CohortJobEntrypointError:
        raise
    except (TypeError, ValueError, RuntimeError, AttributeError):
        _reject(CohortJobEntrypointReason.JOB_MISMATCH)


def _validate_receipt(
    launch: CohortProbeJobLaunch, receipt: CohortJobReceipt, window: _Window, began: datetime
) -> None:
    from django_ray.target.attestation import compare_ray_target_attestation

    try:
        now = window.check()
        if (
            receipt.request != launch.request
            or receipt.request_digest != launch.request_digest
            or receipt.submission_id != probe_job_submission_id(launch.request)
            or not began <= receipt.attestation.observed_at <= receipt.collected_at <= now
        ):
            _reject(CohortJobEntrypointReason.PROBE_FAILED)
        compare_ray_target_attestation(
            receipt.attestation.expectation, receipt.attestation, now=now
        )
    except CohortJobEntrypointError:
        raise
    except (TypeError, ValueError, RuntimeError, AttributeError):
        _reject(CohortJobEntrypointReason.PROBE_FAILED)


def _bootstrap_and_write(
    launch: CohortProbeJobLaunch, receipt: CohortJobReceipt, window: _Window, began: datetime
) -> None:
    _ensure_no_django()
    _settings_environment(launch)
    # Never inherit a different module through setdefault or a preconfigured app.
    os.environ["DJANGO_SETTINGS_MODULE"] = launch.django_settings_module
    _validate_receipt(launch, receipt, window, began)
    connections = None
    reason = CohortJobEntrypointReason.BOOTSTRAP_FAILED
    try:
        import django
        from django.apps import apps
        from django.conf import settings
        from django.db import connections

        if (
            settings.configured
            or apps.ready
            or apps.apps_ready
            or apps.models_ready
            or apps.loading
        ):
            _reject(CohortJobEntrypointReason.DJANGO_ALREADY_IMPORTED)
        django.setup()
        if (
            not apps.ready
            or not settings.configured
            or settings.SETTINGS_MODULE != launch.django_settings_module
        ):
            _reject(CohortJobEntrypointReason.SETTINGS_MISMATCH)
        _settings_environment(launch)
        _runtime_preflight(launch)
        _validate_receipt(launch, receipt, window, began)
        reason = CohortJobEntrypointReason.RECEIPT_REFUSED
        from django_ray.target.cohort_job_receipt_storage import write_cohort_job_receipt

        result = write_cohort_job_receipt(receipt, using="default")
        if type(result) is not bool:
            _reject(reason)
        window.check()
    except CohortJobEntrypointError:
        raise
    except Exception:
        _reject(reason)
    finally:
        if connections is not None:
            try:
                connections.close_all()
            except Exception:
                _reject(CohortJobEntrypointReason.RECEIPT_REFUSED)
    window.check()


def run_probe_job_launch(launch: CohortProbeJobLaunch) -> None:
    """Corroborate the actual running Job, then write only a pending receipt."""
    launch = decode_probe_job_launch(encode_probe_job_launch(launch))
    _ensure_no_django()
    _settings_environment(launch)
    window = _Window(launch.request)
    _runtime_preflight(launch)
    details = _fetch_running_job(launch, window)
    _ensure_no_django()
    _settings_environment(launch)
    began = window.check()
    try:
        from django_ray.runtime.cohort_job import collect_verified_probe
        from django_ray.target.cohort_job_receipt import cohort_job_receipt_from_probe

        proof = collect_verified_probe(
            encode_probe_job_request(launch.request),
            jobs_submission_id=details.submission_id,
            jobs_metadata=details.metadata,
            environment=os.environ,
        )
        receipt = cohort_job_receipt_from_probe(proof)
    except Exception:
        _reject(CohortJobEntrypointReason.PROBE_FAILED)
    _ensure_no_django()
    _validate_receipt(launch, receipt, window, began)
    _runtime_preflight(launch)
    _fetch_running_job(launch, window, native_job_id=receipt.native_job_id)
    _bootstrap_and_write(launch, receipt, window, began)


def main(argv: Sequence[str] | None = None) -> int:
    """Return fixed redacted failures; success acknowledges storage, not eligibility."""
    try:
        run_probe_job_launch(parse_probe_job_launch_argv(sys.argv[1:] if argv is None else argv))
        return 0
    except CohortJobEntrypointError as error:
        reason = error.reason
    except KeyboardInterrupt:
        reason = CohortJobEntrypointReason.INTERRUPTED
    except (Exception, SystemExit):
        reason = CohortJobEntrypointReason.UNEXPECTED_FAILURE
    print(f"Cohort probe driver refused: {reason.value}", file=sys.stderr)
    return 130 if reason is CohortJobEntrypointReason.INTERRUPTED else 1


if __name__ == "__main__":
    raise SystemExit(main())
