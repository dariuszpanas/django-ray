"""Finite HTTP receipt labels and local-process deadline observations."""

from __future__ import annotations

import math
import os
import re
import time
from enum import StrEnum

OUTER_DEADLINE_ENV = "DJANGO_RAY_QUALIFICATION_OBSERVER_DEADLINE"
MAX_OBSERVED_SECONDS = 600


class HttpObserver(StrEnum):
    UNSPECIFIED = "unspecified"
    API = "application_api"
    CORE = "application_core"
    FIRST_WORKFLOW = "first_workflow"
    WORKFLOW = "workflow"
    NESTED_ADMIN = "nested_admin"


class HttpSubstage(StrEnum):
    TASK_STATUS = "task_status"
    WORKFLOW_SUMMARY = "workflow_summary"
    WORKFLOW_COLLECTION = "workflow_collection"
    ADMIN_GRAPH = "admin_graph"
    ADMIN_PAGE = "admin_page"
    OTHER = "other"


def request_substage(path: str) -> HttpSubstage:
    """Classify a validated path without retaining any path or query components."""
    route = path.partition("?")[0]
    if re.fullmatch(r"/api/tasks/[^/]+/?", route):
        return HttpSubstage.TASK_STATUS
    if re.fullmatch(r"/api/cluster/workflows/[^/]+/?", route):
        return HttpSubstage.WORKFLOW_SUMMARY
    if re.fullmatch(
        r"/api/cluster/workflows/[^/]+/(?:topology/nodes|topology/edges|nodes|node-detail)/?",
        route,
    ):
        return HttpSubstage.WORKFLOW_COLLECTION
    if re.fullmatch(r"/admin/django_ray/raytaskexecution/[^/]+/workflow/graph/", route):
        return HttpSubstage.ADMIN_GRAPH
    if route.startswith("/admin/"):
        return HttpSubstage.ADMIN_PAGE
    return HttpSubstage.OTHER


def remaining_seconds(deadline: float | None, now: float) -> float | None:
    """Return bounded numeric observations, never an untrusted context value."""
    if deadline is None or not math.isfinite(deadline) or not math.isfinite(now):
        return None
    remaining = deadline - now
    if remaining > MAX_OBSERVED_SECONDS:
        return None
    return round(max(0.0, remaining), 3)


def local_outer_deadline() -> float | None:
    """Read only a deadline passed by the generation runner on this same host.

    This is diagnostic context, not a new timeout or a cross-node clock value.
    Standalone observers and malformed context have an unknown outer budget.
    """
    raw = os.environ.get(OUTER_DEADLINE_ENV, "")
    if not raw or len(raw) > 32:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if remaining_seconds(value, time.monotonic()) is not None else None
