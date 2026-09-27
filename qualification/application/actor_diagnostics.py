"""Bounded, diagnostic-only Ray State reads; never contact or warm an actor.

Run inside the existing head after failure, under the caller's process deadline.
State data can lag or be missing; actor records do not expose startup timings.
"""

from __future__ import annotations

import json
import math
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

RECORD_LIMIT = 32
RESPONSE_MAX_BYTES = 128 * 1024
OUTPUT_MAX_BYTES = 16 * 1024
HTTP_TIMEOUT_SECONDS = 3
QUERIES = (
    ("actors", "class_name", "WorkflowProgressActor"),
    ("tasks", "func_or_class_name", "WorkflowProgressActor.snapshot"),
    ("nodes", "", ""),
)
FIELDS = {
    "actors": ("actor_id", "class_name", "state", "job_id", "node_id", "pid"),
    "tasks": (
        "task_id",
        "attempt_number",
        "state",
        "job_id",
        "actor_id",
        "type",
        "func_or_class_name",
        "node_id",
        "worker_id",
        "worker_pid",
        "creation_time_ms",
        "start_time_ms",
        "end_time_ms",
    ),
    "nodes": ("node_id", "node_ip", "state", "start_time_ms", "end_time_ms"),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _scalar(value) -> bool:
    return (
        value is None
        or type(value) is bool
        or (type(value) in {int, float} and math.isfinite(value) and abs(value) < 1e20)
        or (type(value) is str and len(value) <= 128 and value.isprintable())
    )


def encode(receipt: dict) -> bytes:
    """Serialize only the sanitized receipt, including its terminating newline."""
    return (json.dumps(receipt, separators=(",", ":"), allow_nan=False) + "\n").encode()


def collect(opener=None) -> dict:
    """Make exactly three bounded GET attempts; failures never become gate proof."""
    opener = opener or build_opener(ProxyHandler({}), NoRedirect())
    receipt = {"schema_version": 1, "diagnostic_only": True, "resources": {}}
    resources = receipt["resources"]
    for kind, key, value in QUERIES:
        result = {"status": "unavailable", "records": []}
        resources[kind] = result
        params = {"limit": RECORD_LIMIT, "timeout": 2, "detail": "true"}
        if key:
            params.update(filter_keys=key, filter_predicates="=", filter_values=value)
        try:
            with opener.open(
                "http://127.0.0.1:8265/api/v0/" + kind + "?" + urlencode(params),
                timeout=HTTP_TIMEOUT_SECONDS,
            ) as response:
                raw = response.read(RESPONSE_MAX_BYTES + 1)
            if len(raw) > RESPONSE_MAX_BYTES:
                continue
            payload = json.loads(raw)
            if payload["result"] is not True:
                continue
            data = payload["data"]["result"]
            rows = data["result"]
            counts = [data[name] for name in ("total", "num_after_truncation", "num_filtered")]
            if not isinstance(rows, list) or any(type(n) is not int or n < 0 for n in counts):
                continue
            partial = bool(data.get("partial_failure_warning")) or counts[0] > counts[1]
            partial |= counts[2] > len(rows) or len(rows) > RECORD_LIMIT
            for row in rows[:RECORD_LIMIT]:
                if not isinstance(row, dict) or (key and row.get(key) != value):
                    partial = True
                    continue
                clean = {
                    field: row[field]
                    for field in FIELDS[kind]
                    if field in row and _scalar(row[field])
                }
                partial |= any(field in row and field not in clean for field in FIELDS[kind])
                if clean:
                    result["records"].append(clean)
                else:
                    partial = True
            result["status"] = "partial" if partial else "available"
        except Exception:
            # Never emit HTTP bodies, exception strings, runtime environments or reprs.
            pass
    receipt["status"] = (
        "available" if all(r["status"] == "available" for r in resources.values()) else "partial"
    )
    if all(r["status"] == "unavailable" for r in resources.values()):
        receipt["status"] = "unavailable"
    while len(encode(receipt)) > OUTPUT_MAX_BYTES:
        largest = max(resources.values(), key=lambda result: len(encode(result)))
        largest["records"].pop()
        largest["status"] = receipt["status"] = "partial"
    return receipt


def main() -> None:
    print(encode(collect()).decode(), end="")


if __name__ == "__main__":
    main()
