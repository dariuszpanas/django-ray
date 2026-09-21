"""Fixed counters for one sealed successful leaf invocation, not all retries."""

from typing import Any

from django_ray.workflow.progress.limits import WorkflowProgressLimits

CAPTURE_COUNTERS = ("offered", "accepted", "rejected", "superseded", "canonical_bytes")


class TerminalCaptureCounters:
    """Fixed-cardinality totals; callers admit each successful leaf once."""

    def __init__(self, *, limits: WorkflowProgressLimits):
        self.limits = limits
        self._value: dict[str, Any] = {
            "schema_version": 1,
            "scope": "successful_final_invocations",
            "reports": 0,
            **dict.fromkeys((*CAPTURE_COUNTERS, "retained", "retained_bytes"), 0),
            "saturated": False,
        }

    def add(self, report: Any) -> None:
        report = normalize_capture_report(report, limits=self.limits)
        if self._value["reports"] >= self.limits.topology_node_max_items:
            raise ValueError("Terminal capture report admission exceeded")
        self._value["reports"] += 1
        self._value["saturated"] |= report["saturated"]
        for name in (*CAPTURE_COUNTERS, "retained", "retained_bytes"):
            total = self._value[name] + report[name]
            self._value["saturated"] |= total > self.limits.identity_max_integer
            self._value[name] = min(total, self.limits.identity_max_integer)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._value)


def normalize_capture_totals(value: Any, *, limits: WorkflowProgressLimits) -> dict[str, Any]:
    """Validate aggregate successful-invocation evidence without identities."""
    template = TerminalCaptureCounters(limits=limits).snapshot()
    if not isinstance(value, dict) or set(value) != set(template):
        raise ValueError("Invalid terminal capture total fields")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["scope"] != template["scope"]
        or type(value["saturated"]) is not bool
        or type(value["reports"]) is not int
        or not 0 <= value["reports"] <= limits.topology_node_max_items
    ):
        raise ValueError("Invalid terminal capture total identity")
    maximum = limits.identity_max_integer
    for name in (*CAPTURE_COUNTERS, "retained", "retained_bytes"):
        if type(value[name]) is not int or not 0 <= value[name] <= maximum:
            raise ValueError("Invalid terminal capture total counter")
    if (
        value["retained"] > value["reports"]
        or value["retained_bytes"] > value["retained"] * limits.event_wire_max_bytes
        or value["canonical_bytes"] < value["retained_bytes"]
        or (value["retained_bytes"] > 0) != bool(value["retained"])
        or (value["accepted"] > 0) != bool(value["retained"])
        or value["offered"] != min(maximum, value["accepted"] + value["rejected"])
        or value["accepted"] != min(maximum, value["superseded"] + value["retained"])
        or (not value["reports"] and any(value[name] for name in CAPTURE_COUNTERS))
        or (
            not value["saturated"]
            and (
                value["accepted"] + value["rejected"] > maximum
                or value["superseded"] + value["retained"] > maximum
            )
        )
    ):
        raise ValueError("Inconsistent terminal capture totals")
    return dict(value)


def normalize_capture_report(value: Any, *, limits: WorkflowProgressLimits) -> dict[str, Any]:
    """Reject unknown fields and inconsistent counts without exporting values."""
    keys = {
        "schema_version",
        "transport",
        *CAPTURE_COUNTERS,
        "retained",
        "retained_bytes",
        "saturated",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid terminal capture report fields")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["transport"] != "terminal_capture"
        or type(value["saturated"]) is not bool
    ):
        raise ValueError("Invalid terminal capture report identity")
    maximum = limits.identity_max_integer
    for name in CAPTURE_COUNTERS:
        if type(value[name]) is not int or not 0 <= value[name] <= maximum:
            raise ValueError("Invalid terminal capture counter")
    retained, size = value["retained"], value["retained_bytes"]
    if (
        type(retained) is not int
        or retained not in (0, 1)
        or type(size) is not int
        or not 0 <= size <= limits.event_wire_max_bytes
        or (size > 0) != bool(retained)
        or (value["accepted"] > 0) != bool(retained)
        or value["offered"] != min(maximum, value["accepted"] + value["rejected"])
        or value["accepted"] != min(maximum, value["superseded"] + retained)
        or value["canonical_bytes"] < min(maximum, size)
        or (
            not value["saturated"]
            and (
                value["accepted"] + value["rejected"] > maximum
                or value["superseded"] + retained > maximum
            )
        )
    ):
        raise ValueError("Inconsistent terminal capture counters")
    return dict(value)
