"""Finite wire and public report contracts for observational inventory."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from django_ray.doctor.contracts import CODES as PROCESS_CODES

OUTPUT_BYTES = 16384
OUTCOMES = (
    "registered_active",
    "registered_tombstone",
    "unregistered_recent",
    "unregistered_aged",
    "ambiguous",
    "malformed",
    "unavailable",
)
CODES = PROCESS_CODES | {
    "filesystem_required",
    "filesystem_unavailable",
    "entry_budget_exhausted",
    "object_budget_exhausted",
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SHARD = re.compile(r"[0-9a-f]{4}\Z")


@dataclass(frozen=True)
class Limits:
    total_seconds: float = 15
    max_entries: int = 1000
    max_objects: int = 100
    max_details: int = 20
    grace_hours: int = 24
    shard: str | None = None

    def __post_init__(self):
        if (
            type(self.total_seconds) not in {int, float}
            or not math.isfinite(self.total_seconds)
            or not 5 <= self.total_seconds <= 60
        ):
            raise ValueError("Invalid inventory time budget")
        for value, low, high in (
            (self.max_entries, 1, 10000),
            (self.max_objects, 1, 500),
            (self.max_details, 0, 100),
            (self.grace_hours, 0, 87600),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid inventory limit")
        if self.shard is not None and (
            type(self.shard) is not str or _SHARD.fullmatch(self.shard) is None
        ):
            raise ValueError("Inventory shard must be four lowercase hexadecimal characters")


class Frames:
    """Validate both child boundaries before any data reaches an operator."""

    def __init__(self, limits: Limits):
        self.limits = limits
        self.details: list[dict] = []
        self.summary: dict | None = None

    def receive(self, raw: bytes) -> dict:
        value = json.loads(raw)
        if type(value) is not dict or self.summary is not None:
            raise ValueError("Invalid inventory frame")
        if value.get("kind") == "detail":
            if (
                set(value) != {"kind", "object_sha256", "outcome"}
                or type(value["object_sha256"]) is not str
                or _HEX.fullmatch(value["object_sha256"]) is None
                or value["outcome"] not in OUTCOMES
                or len(self.details) >= self.limits.max_details
            ):
                raise ValueError("Invalid inventory detail")
            self.details.append({key: value[key] for key in ("object_sha256", "outcome")})
        elif value.get("kind") == "summary":
            if (
                set(value) != {"kind", "code", "entries", "objects", "counts"}
                or value["code"] not in CODES
                or type(value["entries"]) is not int
                or not 0 <= value["entries"] <= self.limits.max_entries
                or type(value["objects"]) is not int
                or not 0 <= value["objects"] <= self.limits.max_objects
                or type(value["counts"]) is not dict
                or set(value["counts"]) != set(OUTCOMES)
                or any(
                    type(count) is not int or not 0 <= count <= self.limits.max_entries
                    for count in value["counts"].values()
                )
                or sum(value["counts"].values()) > value["entries"]
                or len(self.details) > sum(value["counts"].values())
            ):
                raise ValueError("Invalid inventory summary")
            self.summary = value
        else:
            raise ValueError("Invalid inventory frame kind")
        return value


def report(frames: Frames, *, code: str, cleanup: bool, elapsed: float) -> dict:
    partial_counts = dict.fromkeys(OUTCOMES, 0)
    for detail in frames.details:
        partial_counts[detail["outcome"]] += 1
    summary = frames.summary or {
        "code": code,
        "entries": len(frames.details),
        "objects": 0,
        "counts": partial_counts,
    }
    if code == "observed":
        code = summary["code"] if frames.summary is not None else "probe_protocol_invalid"
    if not cleanup:
        code = "cleanup_unconfirmed"
    complete = code == "observed"
    return {
        "schema": "django-ray.payload-inventory",
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "mode": "read-only",
        "backend": "filesystem",
        "code": code,
        "complete": complete,
        "cleanup_confirmed": cleanup,
        "exit_code": 0 if complete else 2,
        "elapsed_seconds": round(max(0, elapsed), 3),
        "entries": summary["entries"],
        "objects": summary["objects"],
        "counts": summary["counts"],
        "counts_complete": frames.summary is not None,
        "details": frames.details,
        "details_omitted": max(0, sum(summary["counts"].values()) - len(frames.details)),
        "limits": {**asdict(frames.limits), "output_bytes": OUTPUT_BYTES},
    }


def render(value: dict, *, as_json: bool) -> str:
    if as_json:
        rendered = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    else:
        lines = [
            f"Payload inventory read-only: {value['code']}; complete={str(value['complete']).lower()}; "
            f"cleanup_confirmed={str(value['cleanup_confirmed']).lower()}.",
            f"Entries {value['entries']}; objects {value['objects']}; "
            f"details omitted {value['details_omitted']}.",
            " ".join(f"{name}={count}" for name, count in value["counts"].items()),
        ]
        lines.extend(
            f"object_sha256={item['object_sha256']} outcome={item['outcome']}"
            for item in value["details"]
        )
        rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > OUTPUT_BYTES - 256:
        raise ValueError("Inventory output limit exceeded")
    return rendered
