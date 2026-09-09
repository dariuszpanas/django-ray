"""Bounded supporting diagnostics for draining legacy Ray Jobs.

These strings are never completion data or retry authority. Keep transport
bounds before JSON decoding and redaction before any durable failure write.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from django_ray.redaction import redact_text

MAX_JOB_DIAGNOSTIC_BYTES = 16 * 1024
OVERSIZED_JOB_DIAGNOSTIC = "[Ray Job logs omitted: diagnostic byte limit exceeded]"
UNAVAILABLE_JOB_DIAGNOSTIC = "[Ray Job logs unavailable or invalid]"
LEGACY_JOB_FAILURE_MESSAGE = "Legacy Ray Job failed without an exact completion envelope"


def sanitize_job_diagnostic(value: Any) -> str | None:
    """Accept bounded UTF-8 text without invoking arbitrary object methods."""
    if type(value) is not str:
        return None
    # Check characters first so encoding an oversized injected value is bounded.
    if len(value) > MAX_JOB_DIAGNOSTIC_BYTES:
        return OVERSIZED_JOB_DIAGNOSTIC
    try:
        if len(value.encode("utf-8")) > MAX_JOB_DIAGNOSTIC_BYTES:
            return OVERSIZED_JOB_DIAGNOSTIC
        sanitized = redact_text(value)
        if len(sanitized.encode("utf-8")) > MAX_JOB_DIAGNOSTIC_BYTES:
            return OVERSIZED_JOB_DIAGNOSTIC
        return sanitized
    except (UnicodeError, ValueError):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate diagnostic JSON key")
        result[key] = value
    return result


def read_job_diagnostic(client: Any, job_id: str, *, timeout: float) -> str | None:
    """Read one bounded log response through the client's pinned HTTP settings.

    Ray's SubmissionClient._do_request reads response.text for authentication
    errors even with stream=True, so using it would defeat this byte ceiling.
    Preserve its endpoint, credentials and TLS verification, but never read an
    error body, follow a redirect, decompress content or call response.json().
    The timeout is the existing control connection/read-inactivity timeout.
    """
    import requests

    try:
        headers = dict(client._headers or {})
        headers["Accept-Encoding"] = "identity"
        with requests.get(
            client._address + "/api/jobs/" + quote(job_id, safe="") + "/logs",
            cookies=client._cookies,
            headers=headers,
            verify=client._verify,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return None
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                return None
            # The extra byte detects an overrun without materializing the body.
            body = response.raw.read(MAX_JOB_DIAGNOSTIC_BYTES + 1, decode_content=False)
            if len(body) > MAX_JOB_DIAGNOSTIC_BYTES:
                return OVERSIZED_JOB_DIAGNOSTIC
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
            if type(payload) is not dict or set(payload) != {"logs"}:
                return None
            return sanitize_job_diagnostic(payload["logs"])
    except Exception:
        # Response/transport exceptions can themselves contain credentials or
        # arbitrary server text. They are neither stored nor logged here.
        return None
