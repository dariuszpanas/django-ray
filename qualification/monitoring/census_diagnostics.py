"""Closed failure details for the final monitoring pod census only."""

from __future__ import annotations

import json

MAX_BYTES = 512
REASONS = {
    "pod_fetch": frozenset({"query_failed"}),
    "pod_census": frozenset(
        {
            "malformed_payload",
            "pod_count",
            "pod_phase",
            "pod_ready_condition",
            "unexpected_component",
            "duplicate_component",
            "spec_image_mismatch",
            "missing_container_status",
            "container_restart_count",
            "container_not_ready",
            "image_id_invalid",
            "init_restart_count",
            "init_not_successful",
            "component_set_incomplete",
            "unclassified",
        }
    ),
}


def valid(value: object) -> bool:
    """Accept only a small fixed record, never caller-supplied diagnostic text."""
    if not isinstance(value, dict) or set(value) != {"schema_version", "stage", "reason"}:
        return False
    stage, reason = value["stage"], value["reason"]
    return (
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and type(stage) is str
        and stage in REASONS
        and type(reason) is str
        and reason in REASONS[stage]
        and len(json.dumps(value, separators=(",", ":")).encode("utf-8")) <= MAX_BYTES
    )


def record(stage: str, reason: str) -> dict:
    value = {"schema_version": 1, "stage": stage, "reason": reason}
    if not valid(value):
        raise ValueError("Invalid monitoring census diagnostic")
    return value


class CensusError(ValueError):
    """Preserve validation failure while carrying one closed reason code."""

    def __init__(self, reason: str, message: str):
        record("pod_census", reason)
        self.reason = reason
        super().__init__(message)
