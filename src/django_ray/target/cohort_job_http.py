"""Bounded HTTP transport for the private, reserved Ray 2.58 probe Job.

Ray's ordinary SDK eagerly reads response.text even for streamed requests.
Construct only its authentication/TLS configuration, then read the fixed Jobs
endpoint directly. Requests connection/read timeouts and a monotonic budget
bound progressive body reads; this does not forcibly interrupt OS DNS or
replace the owning manager's external deadline. Call outside DB transactions.
"""

from __future__ import annotations

import json
import math
import re
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Never
from urllib.parse import urlsplit

from django_ray.execution_codec import _validate_json_tree
from django_ray.target.cohort_intent import _endpoint
from django_ray.target.probe import RAY_TARGET_PROBE_RAY_VERSION

if TYPE_CHECKING:
    from ray.dashboard.modules.job.pydantic_models import JobDetails

COHORT_JOB_HTTP_MAX_BYTES = 128 * 1024
COHORT_JOB_HTTP_MAX_DEPTH = 16
COHORT_JOB_HTTP_MAX_NODES = 8192
COHORT_JOB_HTTP_MAX_TIMEOUT_SECONDS = 5.0
_SUBMISSION_ID = re.compile(r"django-ray-cohort-probe-[0-9a-f]{64}")


class CohortJobHttpReason(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    INVALID_RESPONSE = "invalid_response"
    RESOURCE_LIMIT = "resource_limit"
    TIMEOUT = "timeout"
    CLOCK_REGRESSION = "clock_regression"


class CohortJobHttpError(RuntimeError):
    """Fixed redacted transport failures, never server or credential text."""

    def __init__(self, reason: CohortJobHttpReason) -> None:
        if type(reason) is not CohortJobHttpReason:
            raise TypeError("invalid cohort Job HTTP reason")
        self.reason = reason
        super().__init__(f"Cohort Job HTTP refused: {reason.value}")


def _reject(reason: CohortJobHttpReason) -> Never:
    raise CohortJobHttpError(reason) from None


class _Budget:
    def __init__(self, seconds: float) -> None:
        self.last = time.monotonic()
        self.deadline = self.last + seconds

    def remaining(self) -> float:
        now = time.monotonic()
        if now < self.last:
            _reject(CohortJobHttpReason.CLOCK_REGRESSION)
        self.last = now
        remaining = self.deadline - now
        if remaining <= 0:
            _reject(CohortJobHttpReason.TIMEOUT)
        return remaining


def _arguments(endpoint: object, submission_id: object, timeout: object) -> tuple[str, str, float]:
    try:
        if type(timeout) not in {int, float}:
            _reject(CohortJobHttpReason.INVALID_ARGUMENT)
        assert isinstance(timeout, (int, float))
        value = _endpoint(endpoint)
        parsed = urlsplit(value)
        if (
            not value.startswith(("http://", "https://"))
            or parsed.scheme not in {"http", "https"}
            or any(marker in value for marker in ("?", "#", "%", "\\"))
            or type(submission_id) is not str
            or _SUBMISSION_ID.fullmatch(submission_id) is None
            or not math.isfinite(timeout)
            or not 0 < timeout <= COHORT_JOB_HTTP_MAX_TIMEOUT_SECONDS
        ):
            _reject(CohortJobHttpReason.INVALID_ARGUMENT)
        return value.rstrip("/"), submission_id, float(timeout)
    except CohortJobHttpError:
        raise
    except (TypeError, ValueError, OverflowError):
        _reject(CohortJobHttpReason.INVALID_ARGUMENT)


def _content_length(response: Any) -> int | None:
    # The fixed Ray JSON endpoint needs neither compression nor chunk framing.
    # Refusing them avoids decompression expansion and an unbounded chunk-size
    # line read inside HTTP libraries before the next progress-budget check.
    encoding = response.headers.get("Content-Encoding")
    if encoding is not None and (type(encoding) is not str or encoding.lower() != "identity"):
        _reject(CohortJobHttpReason.INVALID_RESPONSE)
    if response.headers.get("Transfer-Encoding") is not None or response.raw.chunked:
        _reject(CohortJobHttpReason.INVALID_RESPONSE)
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    if (
        type(value) is not str
        or not 0 < len(value) <= 20
        or not value.isascii()
        or not value.isdecimal()
    ):
        _reject(CohortJobHttpReason.INVALID_RESPONSE)
    length = int(value)
    if length > COHORT_JOB_HTTP_MAX_BYTES:
        _reject(CohortJobHttpReason.RESOURCE_LIMIT)
    return length


def _set_read_timeout(raw: Any, seconds: float) -> None:
    connection = raw.connection
    sock = getattr(connection, "sock", None)
    if sock is None:
        # HTTPConnection drops its own socket reference for Connection: close,
        # but the standard HTTPResponse BufferedReader still owns that socket.
        fp = getattr(getattr(raw, "_fp", None), "fp", None)
        sock = getattr(getattr(fp, "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(seconds)
    elif not raw.closed:
        # Do not silently lose the shrinking read timeout on an unsupported
        # HTTP transport. An already closed response can only return buffered EOF.
        _reject(CohortJobHttpReason.TRANSPORT_UNAVAILABLE)


def _read_body(response: Any, budget: _Budget) -> bytes:
    expected_length = _content_length(response)
    payload = bytearray()
    while True:
        _set_read_timeout(response.raw, budget.remaining())
        chunk = response.raw.read(1, decode_content=False)
        budget.remaining()
        if type(chunk) is not bytes or len(chunk) > 1:
            _reject(CohortJobHttpReason.INVALID_RESPONSE)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > COHORT_JOB_HTTP_MAX_BYTES:
            _reject(CohortJobHttpReason.RESOURCE_LIMIT)
    if expected_length is not None and len(payload) != expected_length:
        _reject(CohortJobHttpReason.INVALID_RESPONSE)
    return bytes(payload)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _reject(CohortJobHttpReason.INVALID_RESPONSE)
        value[key] = item
    return value


def _parse_int(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        _reject(CohortJobHttpReason.RESOURCE_LIMIT)
    parsed = int(value)
    if abs(parsed) > (1 << 63) - 1:
        _reject(CohortJobHttpReason.RESOURCE_LIMIT)
    return parsed


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject(CohortJobHttpReason.INVALID_RESPONSE)
    return parsed


def _reject_constant(_value: str) -> Never:
    _reject(CohortJobHttpReason.INVALID_RESPONSE)


def _decode_details(payload: bytes, submission_id: str) -> JobDetails:
    try:
        serialized = payload.decode("utf-8")
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
            if depth > COHORT_JOB_HTTP_MAX_DEPTH or items > COHORT_JOB_HTTP_MAX_NODES * 2:
                _reject(CohortJobHttpReason.RESOURCE_LIMIT)
        value = json.loads(
            serialized,
            object_pairs_hook=_unique_object,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
        if type(value) is not dict:
            _reject(CohortJobHttpReason.INVALID_RESPONSE)
        _validate_json_tree(
            value,
            allow_nonfinite=False,
            allow_nul=False,
            max_depth=COHORT_JOB_HTTP_MAX_DEPTH,
            max_nodes=COHORT_JOB_HTTP_MAX_NODES,
            max_string_bytes=COHORT_JOB_HTTP_MAX_BYTES,
        )
        from ray.dashboard.modules.job.pydantic_models import JobDetails

        if JobDetails is None:
            _reject(CohortJobHttpReason.UNSUPPORTED_RUNTIME)
        # Strict JSON mode permits the API's enum strings, while refusing
        # coercion of strings or booleans into numeric JobDetails fields.
        details = JobDetails.model_validate_json(serialized, strict=True)
        if details.submission_id != submission_id:
            _reject(CohortJobHttpReason.INVALID_RESPONSE)
        return details
    except CohortJobHttpError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError):
        _reject(CohortJobHttpReason.INVALID_RESPONSE)


def fetch_reserved_cohort_job_details(
    jobs_endpoint: str,
    submission_id: str,
    *,
    timeout_seconds: float = COHORT_JOB_HTTP_MAX_TIMEOUT_SECONDS,
) -> JobDetails:
    """Fetch one owned handle with bounded bytes and no implicit endpoint routing.

    Returned JobDetails still require the inspector's reservation/receipt and
    runtime checks. This HTTP helper alone does not grant positive eligibility.
    """
    endpoint, submission_id, timeout = _arguments(jobs_endpoint, submission_id, timeout_seconds)
    budget = _Budget(timeout)
    try:
        import ray
        import requests
        from ray.dashboard.modules.dashboard_sdk import SubmissionClient

        if ray.__version__ != RAY_TARGET_PROBE_RAY_VERSION:
            _reject(CohortJobHttpReason.UNSUPPORTED_RUNTIME)
        # SubmissionClient's constructor loads auth/cookies/TLS settings only;
        # JobSubmissionClient and django-ray's normal client also probe /api/version.
        client = SubmissionClient(address=endpoint)
        if client._address != endpoint:
            _reject(CohortJobHttpReason.INVALID_ARGUMENT)
        headers = dict(client._headers)
        headers["Accept-Encoding"] = "identity"
        with requests.Session() as session:
            session.trust_env = False
            session.proxies.clear()
            remaining = budget.remaining()
            response = session.get(
                endpoint + "/api/jobs/" + submission_id,
                headers=headers,
                cookies=client._cookies,
                verify=client._verify,
                proxies={},
                stream=True,
                allow_redirects=False,
                timeout=(remaining, remaining),
            )
            try:
                budget.remaining()
                if type(response.status_code) is not int or response.status_code != 200:
                    _reject(CohortJobHttpReason.TRANSPORT_UNAVAILABLE)
                payload = _read_body(response, budget)
            finally:
                response.close()
        budget.remaining()
        details = _decode_details(payload, submission_id)
        budget.remaining()
        return details
    except CohortJobHttpError:
        raise
    except Exception:
        _reject(CohortJobHttpReason.TRANSPORT_UNAVAILABLE)
