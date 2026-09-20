"""Validate matched completion-window costs without implying production speedup."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from qualification.docker.scenario import QualificationError

MAX_COST_SNAPSHOTS = 16


class ManagerCostSnapshots:
    """Serve a finite ordered sequence of fixture-owned observation requests."""

    def __init__(self, root: Path, name: str):
        self.root = root
        self.name = name
        self.sequence = 1

    def observe(self, manager: str, counters: dict) -> None:
        if self.sequence > MAX_COST_SNAPSHOTS:
            return
        stem = f"{self.name}-cost-{self.sequence:02d}"
        if not (self.root / f"{stem}.request").exists():
            return
        value = {
            "manager": manager,
            "at_ns": time.monotonic_ns(),
            "queries": counters["queries"],
            "query_seconds": counters["query_seconds"],
            # The parent adds proxy observations from this exact clock window.
            "api_requests": 0,
        }
        with (self.root / f"{stem}.json").open("x") as stream:
            json.dump(value, stream)
        self.sequence += 1


def completion_window_cost(before: dict, after: dict) -> dict:
    """Difference two snapshots from one manager and one monotonic clock."""
    fields = {"manager", "at_ns", "queries", "query_seconds", "api_requests"}
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise QualificationError("latency-cost-shape")
    if set(before) != fields or set(after) != fields:
        raise QualificationError("latency-cost-shape")
    if not isinstance(before["manager"], str) or not before["manager"]:
        raise QualificationError("latency-cost-manager")
    if before["manager"] != after["manager"]:
        raise QualificationError("latency-cost-manager")
    for snapshot in (before, after):
        for field in ("at_ns", "queries", "api_requests"):
            if type(snapshot[field]) is not int or snapshot[field] < 0:
                raise QualificationError("latency-cost-counter")
        seconds = snapshot["query_seconds"]
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise QualificationError("latency-cost-counter")
    if after["at_ns"] <= before["at_ns"]:
        raise QualificationError("latency-cost-clock")
    if any(after[field] < before[field] for field in fields - {"manager"}):
        raise QualificationError("latency-cost-reset")
    return {
        "elapsed_seconds": (after["at_ns"] - before["at_ns"]) / 1e9,
        "queries": after["queries"] - before["queries"],
        "query_seconds": after["query_seconds"] - before["query_seconds"],
        "api_requests": after["api_requests"] - before["api_requests"],
    }
