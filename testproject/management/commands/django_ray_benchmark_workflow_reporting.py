"""Measure observable cost layers for live workflow reporting policies."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, cast

import django
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction
from django.db.models import Count, Sum

from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)
from django_ray.observability import (
    WorkflowObservabilityError,
    get_workflow_plan,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_progress_publication import (
    WorkflowProgressPilotError,
    prepare_terminal_workflow_progress_publication,
)
from django_ray.workflow_progress_storage import (
    audit_workflow_progress_detail_storage,
    verify_workflow_progress_topology_manifest,
)
from django_ray.workflow_progress_summary import (
    WorkflowProgressDetailAvailability,
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)
from testproject.apps.cluster_tasks.tasks import complex_workflow_benchmark

BENCHMARK_SCHEMA_VERSION = 3
BENCHMARK_ID = "django-ray-live-workflow-reporting-policies"
OPT_IN_ENV = "DJANGO_RAY_RUN_WORKFLOW_REPORTING_BENCHMARK"
EXPECTED_CALLABLE_PATH = "testproject.apps.cluster_tasks.tasks.complex_workflow_benchmark"
EXPECTED_WORKFLOW_SHAPE = "chain(group(chain(map), chain(map)), step)"
EXPECTED_WORKFLOW_DEFINITION = (
    "workflow:testproject.apps.cluster_tasks.workflows.build_complex_config"
)
POLICIES = ("full", "terminal_only", "disabled")
POLICY_ORDERS = (
    ("full", "terminal_only", "disabled"),
    ("terminal_only", "disabled", "full"),
    ("disabled", "full", "terminal_only"),
)
_TERMINAL_STATES = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.LOST,
        TaskState.EXPIRED,
    }
)
_EXPECTED_INGRESS_KINDS = frozenset(
    {
        "initialized",
        "node_registered",
        "edges_registered",
        "submitted",
        "started",
        "completed",
        "failed",
        "application_progress",
        "map_registered",
        "map_progress",
        "producer_report",
    }
)
_EXPECTED_REJECTION_REASONS = frozenset(
    {
        "fence_mismatch",
        "protocol_error",
        "unexpected_initialized",
        "node_limit",
        "edge_limit",
        "retained_bytes_limit",
    }
)
_ACTOR_COST_NUMERIC_FIELDS = {
    "initialization": (
        "wire_bytes",
        "handler_wall_ns",
        "handler_cpu_ns",
    ),
    "ingest": (
        "calls_received",
        "wire_bytes_received",
        "decoded_calls",
        "post_disable_calls",
        "handler_wall_ns_total",
        "handler_wall_ns_max",
        "handler_cpu_ns_total",
        "handler_cpu_ns_max",
    ),
    "delivery_delay": (
        "samples",
        "total_us",
        "max_us",
        "negative_clock_samples",
    ),
    "snapshot": (
        "calls",
        "build_wall_ns_total",
        "build_wall_ns_max",
        "build_cpu_ns_total",
        "build_cpu_ns_max",
    ),
}
_PRODUCER_NUMERIC_FIELDS = (
    "reports",
    "offered",
    "submitted",
    "superseded",
    "locally_dropped",
    "acknowledged",
    "actor_rejected",
    "ack_failed",
    "pending_acknowledgements",
)
_EXPECTED_TERMINAL_HANDOFFS = frozenset(
    {
        "not_needed",
        "submitted",
        "failed",
        "actor_unavailable",
    }
)
_SOURCE_REVISION_NAMES = (
    "DJANGO_RAY_BUILD_REVISION",
    "GITHUB_SHA",
    "GIT_COMMIT",
    "SOURCE_VERSION",
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

Policy = Literal["full", "terminal_only", "disabled"]


class WorkflowReportingBenchmarkError(RuntimeError):
    """Raised when live benchmark evidence is incomplete or inconsistent."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _encoded_bytes(value: str | None) -> int:
    return len((value or "").encode("utf-8"))


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowReportingBenchmarkError(f"{name} must be a non-negative integer")
    return value


def _finite_non_negative(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise WorkflowReportingBenchmarkError(f"{name} must be a finite non-negative number")
    return float(value)


def _json_object(value: str | None, name: str) -> dict[str, Any]:
    if not value:
        raise WorkflowReportingBenchmarkError(f"{name} is missing")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as error:
        raise WorkflowReportingBenchmarkError(f"{name} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise WorkflowReportingBenchmarkError(f"{name} must be a JSON object")
    return cast(dict[str, Any], decoded)


def _policy_order(cycle_index: int) -> tuple[str, str, str]:
    if isinstance(cycle_index, bool) or not isinstance(cycle_index, int) or cycle_index < 0:
        raise ValueError("cycle_index must be a non-negative integer")
    return POLICY_ORDERS[cycle_index % len(POLICY_ORDERS)]


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentiles require at least one sample")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be between zero and one")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _distribution(values: Sequence[int | float]) -> dict[str, int | float]:
    samples = [float(value) for value in values]
    if not samples:
        raise WorkflowReportingBenchmarkError("aggregate distributions require samples")
    return {
        "samples": len(samples),
        "median": round(statistics.median(samples), 6),
        "p95_nearest_rank": round(_nearest_rank(samples, 0.95), 6),
        "minimum": round(min(samples), 6),
        "maximum": round(max(samples), 6),
    }


def _workload(
    *,
    fast_items: int,
    slow_items: int,
    fast_seconds: float,
    slow_seconds: float,
) -> dict[str, object]:
    definition: dict[str, object] = {
        "shape": EXPECTED_WORKFLOW_SHAPE,
        "fast_items": fast_items,
        "slow_items": slow_items,
        "fast_seconds": fast_seconds,
        "slow_seconds": slow_seconds,
        "expected_leaf_tasks": fast_items + slow_items,
    }
    definition["fingerprint"] = hashlib.sha256(
        f"v1:{_canonical_json(definition)}".encode()
    ).hexdigest()
    return definition


def _expected_dynamic_topology(
    *,
    fast_items: int,
    slow_items: int,
) -> tuple[int, int]:
    """Return the fixed fixture's expanded actor node and edge counts."""
    leaf_tasks = fast_items + slow_items
    return leaf_tasks + 6, (leaf_tasks * 2) + 4


def _source_revision() -> str:
    for name in _SOURCE_REVISION_NAMES:
        candidate = os.environ.get(name, "")
        if _SHA_RE.fullmatch(candidate):
            return candidate.lower()
    return "unavailable"


def _dependency_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def _database_version() -> str:
    if connection.vendor != "postgresql":
        return "unavailable"
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('server_version')")
        row = cursor.fetchone()
    if not row or not isinstance(row[0], str) or not row[0]:
        raise WorkflowReportingBenchmarkError("PostgreSQL did not report its server version")
    return row[0][:128]


def _environment() -> dict[str, object]:
    return {
        "python": platform.python_version(),
        "django": django.get_version(),
        "django_ray": _dependency_version("django-ray"),
        "ray": _dependency_version("ray"),
        "psycopg": _dependency_version("psycopg"),
        "platform": platform.platform(),
        "database_vendor": connection.vendor,
        "database_version": _database_version(),
        "source_revision": _source_revision(),
        "benchmark_implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _measurement_coverage() -> dict[str, dict[str, str]]:
    return {
        "durable_task_timing": {
            "status": "measured",
            "scope": "database created, started, and finished timestamps",
        },
        "useful_leaf_work": {
            "status": "measured",
            "scope": "bounded testproject result summary only",
        },
        "processed_actor_ingress": {
            "status": "measured",
            "scope": "full-mode retained terminal collector snapshot",
        },
        "actor_observed_logical_ingress": {
            "status": "measured",
            "scope": (
                "calls and logical wire bytes received by the full-mode actor "
                "before its retained terminal snapshot"
            ),
        },
        "producer_progress_sessions": {
            "status": "measured",
            "scope": (
                "actor-accepted fixed-shape leaf reports of valid progress offers, "
                "submissions, local supersession/drop, producer-observed acknowledgements, "
                "and one terminal handoff outcome"
            ),
        },
        "producer_to_actor_delivery_delay": {
            "status": "measured",
            "scope": (
                "decoded event timestamp to actor handler entry; includes serialization, "
                "transport, scheduling, queueing, and clock effects"
            ),
        },
        "actor_handler_and_snapshot_cost": {
            "status": "measured",
            "scope": (
                "actor-process wall and process CPU time through the retained terminal "
                "snapshot; not complete actor lifetime attribution"
            ),
        },
        "actor_creation_count": {
            "status": "derived",
            "scope": "policy contract plus full-mode ingress; not a Ray State count",
        },
        "normalized_storage_rows_and_logical_bytes": {
            "status": "measured",
            "scope": "exact run identity; logical protocol bytes, not PostgreSQL bytes",
        },
        "producer_attempted_rpcs": {
            "status": "partial",
            "scope": (
                "application-progress submissions reported by participating leaves; "
                "a terminal latest-value handoff is included, while structural and "
                "lifecycle events, producer reports, and coordinator snapshot/disable "
                "calls are excluded"
            ),
        },
        "actor_lifetime_rss_and_cpu": {
            "status": "unavailable",
            "reason": (
                "handler CPU is measured, but no bounded lifetime RSS or complete "
                "actor-process sampler exists"
            ),
        },
        "mailbox_depth_and_lag": {
            "status": "unavailable",
            "reason": (
                "end-to-end processed delivery delay is measured but cannot isolate "
                "mailbox depth or pure queue latency"
            ),
        },
        "snapshot_and_disable_rpcs": {
            "status": "partial",
            "scope": (
                "snapshot calls/build cost through retained evidence; disable calls after "
                "the last snapshot remain unavailable"
            ),
        },
        "database_statements_latency_and_wal": {
            "status": "unavailable",
            "reason": "logical row evidence is not physical PostgreSQL attribution",
        },
        "network_traffic": {
            "status": "unavailable",
            "reason": "the live path has no per-workflow network byte counter",
        },
    }


def _run_identity(execution: RayTaskExecution) -> WorkflowRunIdentity:
    if (
        execution.workflow_run_id is None
        or execution.attempt_number < 1
        or execution.execution_generation < 1
    ):
        raise WorkflowReportingBenchmarkError(
            "benchmark execution has no complete workflow run identity"
        )
    return WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=str(execution.workflow_run_id),
    )


def _selection(execution: RayTaskExecution, expected_policy: Policy) -> dict[str, object]:
    try:
        snapshot = get_workflow_plan(execution)
    except WorkflowObservabilityError as error:
        raise WorkflowReportingBenchmarkError(
            f"{expected_policy} execution retained an invalid workflow plan"
        ) from error
    if not isinstance(snapshot, dict):
        raise WorkflowReportingBenchmarkError(
            f"{expected_policy} execution retained no workflow plan"
        )
    selection = snapshot.get("selection")
    manifest = snapshot.get("manifest")
    fingerprint = snapshot.get("fingerprint")
    if (
        not isinstance(selection, dict)
        or not isinstance(manifest, dict)
        or not isinstance(fingerprint, str)
        or fingerprint != execution.workflow_plan_fingerprint
        or selection.get("plan_selection_format") != "django-ray.workflow-plan-selection"
        or selection.get("plan_selection_format_version") != 2
        or selection.get("reporting_policy") != expected_policy
        or selection.get("selected_strategy") != "dynamic_tasks"
    ):
        raise WorkflowReportingBenchmarkError(
            f"{expected_policy} execution selected an unexpected workflow policy or strategy"
        )
    definition = manifest.get("definition")
    topology = manifest.get("topology")
    nodes = manifest.get("nodes")
    edges = manifest.get("edges")
    if (
        not isinstance(definition, dict)
        or definition.get("name") != EXPECTED_WORKFLOW_DEFINITION
        or not isinstance(topology, dict)
        or topology.get("class") != "dynamic"
        or not isinstance(nodes, list)
        or not nodes
        or not isinstance(edges, list)
        or not edges
    ):
        raise WorkflowReportingBenchmarkError(
            f"{expected_policy} execution retained an unexpected workflow plan shape"
        )
    if (
        fingerprint != execution.workflow_plan_fingerprint
        or execution.workflow_plan_pinned_attempt != execution.attempt_number
        or not execution.workflow_plan_json
    ):
        raise WorkflowReportingBenchmarkError(
            f"{expected_policy} execution did not retain its complete plan identity"
        )
    return {
        "reporting_policy": expected_policy,
        "selected_strategy": "dynamic_tasks",
        "plan_fingerprint": fingerprint,
        "plan_node_count": len(nodes),
        "plan_edge_count": len(edges),
    }


def _result_summary(
    execution: RayTaskExecution,
    *,
    fast_items: int,
    slow_items: int,
) -> dict[str, object]:
    result = _json_object(execution.result_data, "workflow result")
    if (
        result.get("engine") != "django-ray-workflow"
        or result.get("shape") != EXPECTED_WORKFLOW_SHAPE
        or result.get("durability_boundary") != "single RayTaskExecution"
        or result.get("total_leaf_tasks") != fast_items + slow_items
    ):
        raise WorkflowReportingBenchmarkError("workflow result does not match the fixed workload")
    branches = result.get("branches")
    if not isinstance(branches, list) or len(branches) != 2:
        raise WorkflowReportingBenchmarkError("workflow result must contain two branch summaries")
    expected_items = {"fast": fast_items, "slow": slow_items}
    seen: set[str] = set()
    useful_seconds = 0.0
    branch_wall_seconds: dict[str, float] = {}
    for index, branch_value in enumerate(branches):
        if not isinstance(branch_value, dict):
            raise WorkflowReportingBenchmarkError(
                f"workflow result branch {index} must be an object"
            )
        branch = cast(dict[str, Any], branch_value)
        name = branch.get("branch")
        if not isinstance(name, str) or name not in expected_items or name in seen:
            raise WorkflowReportingBenchmarkError("workflow result branch identity is invalid")
        if (
            branch.get("engine") != "django-ray-workflow"
            or branch.get("durability_boundary") != "single RayTaskExecution"
            or branch.get("leaf_tasks") != expected_items[name]
        ):
            raise WorkflowReportingBenchmarkError(
                f"workflow result branch {name} does not match the workload"
            )
        useful_seconds += _finite_non_negative(
            branch.get("total_leaf_seconds"),
            f"{name} total_leaf_seconds",
        )
        branch_wall_seconds[name] = round(
            _finite_non_negative(
                branch.get("leaf_wall_seconds"),
                f"{name} leaf_wall_seconds",
            ),
            6,
        )
        items = branch.get("items")
        if not isinstance(items, list) or len(items) != expected_items[name]:
            raise WorkflowReportingBenchmarkError(
                f"workflow result branch {name} item count is invalid"
            )
        seen.add(name)
    if seen != set(expected_items):
        raise WorkflowReportingBenchmarkError("workflow result omitted a branch")
    return {
        "total_leaf_tasks": fast_items + slow_items,
        "useful_leaf_seconds": round(useful_seconds, 6),
        "branch_leaf_wall_seconds": branch_wall_seconds,
        "workflow_elapsed_seconds": round(
            _finite_non_negative(
                result.get("workflow_elapsed_seconds"),
                "workflow_elapsed_seconds",
            ),
            6,
        ),
    }


def _timing(execution: RayTaskExecution) -> dict[str, float]:
    if execution.started_at is None or execution.finished_at is None:
        raise WorkflowReportingBenchmarkError("terminal execution has incomplete timestamps")
    created_at = cast(datetime, execution.created_at)
    started_at = cast(datetime, execution.started_at)
    finished_at = cast(datetime, execution.finished_at)
    if started_at < created_at or finished_at < started_at:
        raise WorkflowReportingBenchmarkError("execution timestamps are out of order")
    return {
        "queue_wait_seconds": round(
            (started_at - created_at).total_seconds(),
            6,
        ),
        "outer_execution_seconds": round(
            (finished_at - started_at).total_seconds(),
            6,
        ),
        "durable_end_to_end_seconds": round(
            (finished_at - created_at).total_seconds(),
            6,
        ),
    }


def _integer_mapping(
    value: object,
    *,
    name: str,
    expected_keys: frozenset[str],
) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise WorkflowReportingBenchmarkError(f"{name} has unexpected fields")
    return {
        key: _non_negative_int(item, f"{name}.{key}")
        for key, item in sorted(cast(dict[str, Any], value).items())
    }


def _ingress(progress: Mapping[str, object]) -> dict[str, object]:
    ingress_value = progress.get("ingress")
    if not isinstance(ingress_value, dict):
        raise WorkflowReportingBenchmarkError("workflow progress ingress is missing")
    ingress = cast(dict[str, Any], ingress_value)
    expected_fields = {
        "accepted",
        "rejected",
        "truncated",
        "accepted_by_kind",
        "rejected_by_reason",
        "retained_bytes",
        "retained_nodes",
        "retained_edges",
        "cost",
    }
    if set(ingress) not in (expected_fields, expected_fields | {"producer"}):
        raise WorkflowReportingBenchmarkError("workflow progress ingress has unexpected fields")
    accepted = _non_negative_int(ingress["accepted"], "ingress.accepted")
    rejected = _non_negative_int(ingress["rejected"], "ingress.rejected")
    accepted_by_kind = _integer_mapping(
        ingress["accepted_by_kind"],
        name="ingress.accepted_by_kind",
        expected_keys=_EXPECTED_INGRESS_KINDS,
    )
    rejected_by_reason = _integer_mapping(
        ingress["rejected_by_reason"],
        name="ingress.rejected_by_reason",
        expected_keys=_EXPECTED_REJECTION_REASONS,
    )
    if (
        sum(accepted_by_kind.values()) != accepted
        or sum(rejected_by_reason.values()) != rejected
        or accepted_by_kind["initialized"] != 1
    ):
        raise WorkflowReportingBenchmarkError("workflow ingress counters are inconsistent")
    cost = _actor_cost(
        ingress["cost"],
        accepted=accepted,
        rejected=rejected,
        accepted_by_kind=accepted_by_kind,
    )
    producer = _producer_progress(ingress["producer"]) if "producer" in ingress else None
    return {
        "collector_events_accepted": accepted,
        "processed_ingest_events": accepted - 1,
        "accepted_by_kind": accepted_by_kind,
        "collector_events_rejected": rejected,
        "rejected_by_reason": rejected_by_reason,
        "truncated": _non_negative_int(ingress["truncated"], "ingress.truncated"),
        "retained_bytes": _non_negative_int(
            ingress["retained_bytes"],
            "ingress.retained_bytes",
        ),
        "retained_nodes": _non_negative_int(
            ingress["retained_nodes"],
            "ingress.retained_nodes",
        ),
        "retained_edges": _non_negative_int(
            ingress["retained_edges"],
            "ingress.retained_edges",
        ),
        "actor_cost": cost,
        "producer": producer,
    }


def _producer_progress(value: object) -> dict[str, object]:
    expected_fields = {
        "schema_version",
        "saturated",
        *_PRODUCER_NUMERIC_FIELDS,
        "terminal_handoffs",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise WorkflowReportingBenchmarkError("ingress.producer has unexpected fields")
    producer = cast(dict[str, Any], value)
    if type(producer["schema_version"]) is not int or producer["schema_version"] != 1:
        raise WorkflowReportingBenchmarkError("ingress.producer schema version is unsupported")
    if type(producer["saturated"]) is not bool:
        raise WorkflowReportingBenchmarkError("ingress.producer saturation evidence is invalid")
    counters = {
        field: _non_negative_int(
            producer[field],
            f"ingress.producer.{field}",
        )
        for field in _PRODUCER_NUMERIC_FIELDS
    }
    terminal_handoffs = _integer_mapping(
        producer["terminal_handoffs"],
        name="ingress.producer.terminal_handoffs",
        expected_keys=_EXPECTED_TERMINAL_HANDOFFS,
    )
    if producer["saturated"]:
        raise WorkflowReportingBenchmarkError(
            "benchmark producer counters saturated before terminal retention"
        )
    nontrivial_terminal_handoffs = (
        terminal_handoffs["submitted"]
        + terminal_handoffs["failed"]
        + terminal_handoffs["actor_unavailable"]
    )
    minimum_terminal_offers = counters["reports"] + nontrivial_terminal_handoffs
    minimum_terminal_submissions = (
        2 * terminal_handoffs["submitted"]
        + terminal_handoffs["failed"]
        + terminal_handoffs["actor_unavailable"]
    )
    if (
        counters["offered"]
        != counters["submitted"] + counters["superseded"] + counters["locally_dropped"]
        or counters["submitted"]
        != counters["acknowledged"]
        + counters["actor_rejected"]
        + counters["ack_failed"]
        + counters["pending_acknowledgements"]
        or sum(terminal_handoffs.values()) != counters["reports"]
        or counters["offered"] < minimum_terminal_offers
        or counters["submitted"] < minimum_terminal_submissions
        or terminal_handoffs["failed"] + terminal_handoffs["actor_unavailable"]
        > counters["locally_dropped"]
        or terminal_handoffs["actor_unavailable"]
        > counters["actor_rejected"] + counters["ack_failed"]
    ):
        raise WorkflowReportingBenchmarkError("ingress.producer counters are inconsistent")
    return {
        "schema_version": 1,
        "saturated": False,
        **counters,
        "terminal_handoffs": terminal_handoffs,
    }


def _actor_cost(
    value: object,
    *,
    accepted: int,
    rejected: int,
    accepted_by_kind: Mapping[str, int],
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "saturated",
        "initialization",
        "ingest",
        "delivery_delay",
        "snapshot",
    }:
        raise WorkflowReportingBenchmarkError("ingress.cost has unexpected fields")
    cost = cast(dict[str, Any], value)
    if type(cost["schema_version"]) is not int or cost["schema_version"] != 1:
        raise WorkflowReportingBenchmarkError("ingress.cost schema version is unsupported")
    if type(cost["saturated"]) is not bool:
        raise WorkflowReportingBenchmarkError("ingress.cost saturation evidence is invalid")
    initialization = _cost_integer_section(
        cost["initialization"],
        name="ingress.cost.initialization",
        expected_keys=frozenset(
            {
                "wire_bytes",
                "handler_wall_ns",
                "handler_cpu_ns",
            }
        ),
    )
    ingest = _cost_integer_section(
        cost["ingest"],
        name="ingress.cost.ingest",
        expected_keys=frozenset(
            {
                "calls_received",
                "wire_bytes_received",
                "decoded_calls",
                "post_disable_calls",
                "handler_wall_ns_total",
                "handler_wall_ns_max",
                "handler_cpu_ns_total",
                "handler_cpu_ns_max",
            }
        ),
        nested_key="decoded_by_kind",
    )
    decoded_by_kind = _integer_mapping(
        cast(dict[str, Any], cost["ingest"])["decoded_by_kind"],
        name="ingress.cost.ingest.decoded_by_kind",
        expected_keys=_EXPECTED_INGRESS_KINDS,
    )
    delivery = _cost_integer_section(
        cost["delivery_delay"],
        name="ingress.cost.delivery_delay",
        expected_keys=frozenset(
            {
                "samples",
                "total_us",
                "max_us",
                "negative_clock_samples",
            }
        ),
    )
    snapshot = _cost_integer_section(
        cost["snapshot"],
        name="ingress.cost.snapshot",
        expected_keys=frozenset(
            {
                "calls",
                "build_wall_ns_total",
                "build_wall_ns_max",
                "build_cpu_ns_total",
                "build_cpu_ns_max",
            }
        ),
    )
    if (
        initialization["wire_bytes"] == 0
        or snapshot["calls"] == 0
        or ingest["handler_wall_ns_max"] > ingest["handler_wall_ns_total"]
        or ingest["handler_cpu_ns_max"] > ingest["handler_cpu_ns_total"]
        or delivery["max_us"] > delivery["total_us"]
        or snapshot["build_wall_ns_max"] > snapshot["build_wall_ns_total"]
        or snapshot["build_cpu_ns_max"] > snapshot["build_cpu_ns_total"]
    ):
        raise WorkflowReportingBenchmarkError("ingress.cost counters are inconsistent")
    if cost["saturated"]:
        raise WorkflowReportingBenchmarkError(
            "benchmark actor cost saturated before terminal retention"
        )
    expected_decoded_by_kind = {
        name: 0 if name == "initialized" else accepted_by_kind[name]
        for name in _EXPECTED_INGRESS_KINDS
    }
    expected_decoded = accepted - 1
    if (
        ingest["calls_received"] != expected_decoded + rejected + ingest["post_disable_calls"]
        or ingest["decoded_calls"] != expected_decoded
        or decoded_by_kind != expected_decoded_by_kind
        or delivery["samples"] + delivery["negative_clock_samples"] != expected_decoded
        or (ingest["calls_received"] > 0 and ingest["wire_bytes_received"] == 0)
    ):
        raise WorkflowReportingBenchmarkError("ingress.cost counters are inconsistent")
    return {
        "schema_version": 1,
        "saturated": False,
        "initialization": initialization,
        "ingest": {
            **ingest,
            "decoded_by_kind": decoded_by_kind,
        },
        "delivery_delay": delivery,
        "snapshot": snapshot,
    }


def _cost_integer_section(
    value: object,
    *,
    name: str,
    expected_keys: frozenset[str],
    nested_key: str | None = None,
) -> dict[str, int]:
    if not isinstance(value, dict):
        raise WorkflowReportingBenchmarkError(f"{name} has unexpected fields")
    keys = set(value)
    required = set(expected_keys)
    if nested_key is not None:
        required.add(nested_key)
    if keys != required:
        raise WorkflowReportingBenchmarkError(f"{name} has unexpected fields")
    return {key: _non_negative_int(value[key], f"{name}.{key}") for key in sorted(expected_keys)}


def _full_snapshot_evidence(
    execution: RayTaskExecution,
    *,
    identity: WorkflowRunIdentity,
    selection: Mapping[str, object],
    fast_items: int,
    slow_items: int,
) -> dict[str, object]:
    progress = _json_object(execution.progress_data, "workflow progress snapshot")
    config = getattr(settings, "DJANGO_RAY", {})
    detail_days = (
        int(config.get("WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS", 7))
        if isinstance(config, Mapping)
        else 7
    )
    try:
        prepared = prepare_terminal_workflow_progress_publication(
            identity,
            progress,
            plan_fingerprint=str(selection["plan_fingerprint"]),
            selected_strategy=str(selection["selected_strategy"]),
            reporting_policy="full",
            detail_days=detail_days,
        )
    except (KeyError, WorkflowProgressPilotError) as error:
        raise WorkflowReportingBenchmarkError(
            "full execution retained an invalid or incomplete terminal actor snapshot"
        ) from error
    expected_nodes, expected_edges = _expected_dynamic_topology(
        fast_items=fast_items,
        slow_items=slow_items,
    )
    topology = prepared.topology
    ingress = _ingress(progress)
    accepted_by_kind_value = ingress["accepted_by_kind"]
    if not isinstance(accepted_by_kind_value, Mapping):
        raise WorkflowReportingBenchmarkError("full execution actor ingress evidence is invalid")
    accepted_by_kind = cast(Mapping[str, object], accepted_by_kind_value)
    application_progress = _non_negative_int(
        accepted_by_kind.get("application_progress"),
        "ingress.accepted_by_kind.application_progress",
    )
    producer_value = ingress.get("producer")
    if not isinstance(producer_value, Mapping):
        raise WorkflowReportingBenchmarkError(
            "full execution retained no producer progress evidence"
        )
    producer_reports = _non_negative_int(
        producer_value.get("reports"),
        "ingress.producer.reports",
    )
    producer_submitted = _non_negative_int(
        producer_value.get("submitted"),
        "ingress.producer.submitted",
    )
    producer_acknowledged = _non_negative_int(
        producer_value.get("acknowledged"),
        "ingress.producer.acknowledged",
    )
    producer_actor_rejected = _non_negative_int(
        producer_value.get("actor_rejected"),
        "ingress.producer.actor_rejected",
    )
    if (
        prepared.summary["state"] != TaskState.SUCCEEDED
        or topology.observed_node_count != expected_nodes
        or topology.retained_node_count != expected_nodes
        or topology.observed_edge_count != expected_edges
        or topology.retained_edge_count != expected_edges
        or prepared.detail.observed_count != expected_nodes
        or len(prepared.detail.records) != expected_nodes
        or ingress["retained_nodes"] != expected_nodes
        or ingress["retained_edges"] != expected_edges
        or accepted_by_kind.get("initialized") != 1
        or accepted_by_kind.get("node_registered") != expected_nodes
        or accepted_by_kind.get("edges_registered") != expected_nodes - 1
        or accepted_by_kind.get("submitted") != expected_nodes
        or accepted_by_kind.get("started") != expected_nodes
        or accepted_by_kind.get("completed") != expected_nodes
        or accepted_by_kind.get("failed") != 0
        or accepted_by_kind.get("producer_report") != producer_reports
        or producer_acknowledged > application_progress
        or application_progress > producer_submitted - producer_actor_rejected
        or application_progress < fast_items + slow_items
        or producer_reports != fast_items + slow_items
    ):
        raise WorkflowReportingBenchmarkError(
            "full execution actor evidence does not match the fixed expanded workload"
        )
    return ingress


def _summary(
    execution: RayTaskExecution,
    *,
    identity: WorkflowRunIdentity,
    expected_policy: Policy,
) -> dict[str, object] | None:
    serialized = execution.workflow_progress_summary_json
    if serialized is None:
        return None
    summary = deserialize_workflow_progress_summary(
        serialized,
        expected_identity=identity,
    )
    if serialize_workflow_progress_summary(summary, expected_identity=identity) != serialized:
        raise WorkflowReportingBenchmarkError("workflow summary is not canonical")
    if (
        summary["reporting_policy"] != expected_policy
        or summary["state"] != TaskState.SUCCEEDED
        or summary["terminal"]["outcome"] != TaskState.SUCCEEDED
        or summary["plan_fingerprint"] != execution.workflow_plan_fingerprint
    ):
        raise WorkflowReportingBenchmarkError("workflow summary conflicts with the execution")
    return {
        "summary_revision": summary["summary_revision"],
        "topology_version": summary["topology_version"],
        "detail_revision": summary["detail_revision"],
        "detail_availability": summary["detail"]["availability"],
        "declared_nodes": summary["node_counts"]["declared"],
        "declared_edges": summary["edge_counts"]["declared"],
    }


def _sum(queryset: Any, field: str) -> int:
    value = queryset.aggregate(total=Sum(field))["total"]
    return int(value or 0)


def _storage(
    execution: RayTaskExecution,
    *,
    identity: WorkflowRunIdentity,
) -> dict[str, object]:
    exact_runs = WorkflowProgressRunStorage.objects.filter(
        execution=execution,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        run_id=identity.run_id,
    )
    all_runs = WorkflowProgressRunStorage.objects.filter(execution=execution)
    if all_runs.count() != exact_runs.count():
        raise WorkflowReportingBenchmarkError(
            "benchmark execution retained storage outside its exact run identity"
        )
    manifests = WorkflowProgressTopologyManifest.objects.filter(run_storage__in=exact_runs)
    pages = WorkflowProgressTopologyPage.objects.filter(run_storage__in=exact_runs)
    links = WorkflowProgressTopologyManifestPage.objects.filter(
        manifest__run_storage__in=exact_runs
    )
    details = WorkflowProgressNodeDetail.objects.filter(run_storage__in=exact_runs)
    current_manifests = manifests.filter(slot=WorkflowProgressTopologySlot.CURRENT)
    pending_manifests = manifests.filter(slot=WorkflowProgressTopologySlot.PENDING)
    page_collection_counts = {
        str(row["collection"]): int(row["rows"])
        for row in pages.values("collection").annotate(rows=Count("pk")).order_by("collection")
    }
    detail_state_counts = {
        str(row["state"]): int(row["rows"])
        for row in details.values("state").annotate(rows=Count("pk")).order_by("state")
    }
    return {
        "run_storage": {
            "rows": exact_runs.count(),
            "detail_encoded_bytes": _sum(exact_runs, "detail_encoded_bytes"),
            "detail_decoded_bytes": _sum(exact_runs, "detail_decoded_bytes"),
        },
        "topology_manifests": {
            "rows": manifests.count(),
            "current_rows": current_manifests.count(),
            "pending_rows": pending_manifests.count(),
            "topology_encoded_bytes": _sum(manifests, "encoded_bytes"),
            "topology_decoded_bytes": _sum(manifests, "decoded_bytes"),
            "node_count": _sum(manifests, "node_count"),
            "edge_count": _sum(manifests, "edge_count"),
            "node_page_count": _sum(manifests, "node_page_count"),
            "edge_page_count": _sum(manifests, "edge_page_count"),
        },
        "topology_pages": {
            "rows": pages.count(),
            "encoded_bytes": _sum(pages, "encoded_bytes"),
            "decoded_bytes": _sum(pages, "decoded_bytes"),
            "unlinked_rows": pages.filter(manifest_links__isnull=True).count(),
            "collection_rows": page_collection_counts,
        },
        "manifest_links": {"rows": links.count()},
        "node_details": {
            "rows": details.count(),
            "encoded_bytes": _sum(details, "encoded_bytes"),
            "decoded_bytes": _sum(details, "decoded_bytes"),
            "state_rows": detail_state_counts,
        },
    }


def _storage_int(storage: Mapping[str, object], section: str, field: str) -> int:
    value = storage.get(section)
    if not isinstance(value, Mapping):
        raise WorkflowReportingBenchmarkError(f"storage section {section} is invalid")
    return _non_negative_int(value.get(field), f"storage.{section}.{field}")


def _validate_storage(
    storage: Mapping[str, object],
    *,
    identity: WorkflowRunIdentity,
    expected_policy: Policy,
    pilot_enabled: bool,
    expected_node_count: int,
    expected_edge_count: int,
) -> None:
    run_rows = _storage_int(storage, "run_storage", "rows")
    manifest_rows = _storage_int(storage, "topology_manifests", "rows")
    page_rows = _storage_int(storage, "topology_pages", "rows")
    link_rows = _storage_int(storage, "manifest_links", "rows")
    detail_rows = _storage_int(storage, "node_details", "rows")
    normalized_rows = run_rows + manifest_rows + page_rows + link_rows + detail_rows
    if expected_policy != "full" or not pilot_enabled:
        if normalized_rows != 0:
            raise WorkflowReportingBenchmarkError(
                f"{expected_policy} execution unexpectedly retained normalized detail"
            )
        return
    if (
        run_rows != 1
        or manifest_rows != 1
        or _storage_int(storage, "topology_manifests", "current_rows") != 1
        or _storage_int(storage, "topology_manifests", "pending_rows") != 0
        or page_rows < 1
        or link_rows
        != (
            _storage_int(storage, "topology_manifests", "node_page_count")
            + _storage_int(storage, "topology_manifests", "edge_page_count")
        )
        or _storage_int(storage, "topology_pages", "unlinked_rows") != 0
        or _storage_int(storage, "topology_manifests", "node_count") != expected_node_count
        or _storage_int(storage, "topology_manifests", "edge_count") != expected_edge_count
        or detail_rows != expected_node_count
        or _storage_int(storage, "run_storage", "detail_encoded_bytes")
        != _storage_int(storage, "node_details", "encoded_bytes")
        or _storage_int(storage, "run_storage", "detail_decoded_bytes")
        != _storage_int(storage, "node_details", "decoded_bytes")
    ):
        raise WorkflowReportingBenchmarkError(
            "full execution normalized storage is incomplete or inconsistent"
        )
    manifest_id = (
        WorkflowProgressTopologyManifest.objects.filter(
            run_storage__execution_id=identity.task_execution_pk,
            run_storage__attempt_number=identity.attempt_number,
            run_storage__execution_generation=identity.execution_generation,
            run_storage__run_id=identity.run_id,
            slot=WorkflowProgressTopologySlot.CURRENT,
        )
        .values_list("pk", flat=True)
        .get()
    )
    verify_workflow_progress_topology_manifest(
        str(manifest_id),
        expected_identity=identity,
    )
    audit_workflow_progress_detail_storage(identity)


def _lifecycle_storage(execution: RayTaskExecution) -> tuple[dict[str, object], int]:
    attempts = TaskAttempt.objects.filter(
        execution=execution,
        attempt_number=execution.attempt_number,
    )
    if attempts.count() != 1 or execution.attempts.count() != 1:
        raise WorkflowReportingBenchmarkError(
            "benchmark execution must retain exactly one archived attempt"
        )
    attempt = attempts.get()
    if (
        attempt.state != TaskState.SUCCEEDED
        or attempt.started_at != execution.started_at
        or attempt.finished_at != execution.finished_at
        or attempt.result_data != execution.result_data
        or attempt.workflow_progress_summary_json != execution.workflow_progress_summary_json
    ):
        raise WorkflowReportingBenchmarkError(
            "archived attempt does not match the terminal execution"
        )
    return (
        {
            "execution_rows": 1,
            "attempt_rows": 1,
            "args_json_bytes": _encoded_bytes(execution.args_json),
            "kwargs_json_bytes": _encoded_bytes(execution.kwargs_json),
            "result_json_bytes": _encoded_bytes(execution.result_data),
            "attempt_result_json_bytes": _encoded_bytes(attempt.result_data),
            "workflow_plan_json_bytes": _encoded_bytes(execution.workflow_plan_json),
            "workflow_plan_selection_bytes": _encoded_bytes(execution.workflow_plan_selection),
            "runtime_env_snapshot_bytes": _encoded_bytes(execution.runtime_env_json),
        },
        _encoded_bytes(attempt.workflow_progress_summary_json),
    )


def _validate_policy_contract(
    *,
    execution: RayTaskExecution,
    policy: Policy,
    pilot_enabled: bool,
    ingress: Mapping[str, object] | None,
    summary: Mapping[str, object] | None,
    storage: Mapping[str, object],
    expected_node_count: int,
    expected_edge_count: int,
) -> None:
    progress_bytes = _encoded_bytes(execution.progress_data)
    summary_bytes = _encoded_bytes(execution.workflow_progress_summary_json)
    if policy == "full":
        if progress_bytes == 0 or ingress is None:
            raise WorkflowReportingBenchmarkError(
                "full reporting retained no observable actor ingress"
            )
        if pilot_enabled:
            if (
                summary is None
                or summary.get("detail_availability")
                != WorkflowProgressDetailAvailability.AVAILABLE.value
                or summary.get("topology_version") is None
                or summary.get("detail_revision") is None
            ):
                raise WorkflowReportingBenchmarkError(
                    "full pilot reporting retained no complete terminal detail"
                )
        elif summary is not None or summary_bytes != 0:
            raise WorkflowReportingBenchmarkError(
                "full package-default reporting unexpectedly published schema v3"
            )
    elif policy == "terminal_only":
        if (
            progress_bytes != 0
            or ingress is not None
            or summary is None
            or summary.get("detail_availability")
            != WorkflowProgressDetailAvailability.OMITTED_BY_POLICY.value
            or summary.get("topology_version") is not None
            or summary.get("detail_revision") is not None
        ):
            raise WorkflowReportingBenchmarkError(
                "terminal-only reporting violated its summary-only contract"
            )
    elif progress_bytes != 0 or summary_bytes != 0 or ingress is not None or summary is not None:
        raise WorkflowReportingBenchmarkError(
            "disabled reporting retained workflow progress or summary data"
        )
    _validate_storage(
        storage,
        identity=_run_identity(execution),
        expected_policy=policy,
        pilot_enabled=pilot_enabled,
        expected_node_count=expected_node_count,
        expected_edge_count=expected_edge_count,
    )


def _sample(
    execution: RayTaskExecution,
    *,
    cycle: int,
    position: int,
    policy: Policy,
    client_poll_seconds: float,
    poll_count: int,
    fast_items: int,
    slow_items: int,
    pilot_enabled: bool,
) -> dict[str, object]:
    if (
        execution.callable_path != EXPECTED_CALLABLE_PATH
        or execution.state != TaskState.SUCCEEDED
        or execution.attempt_number != 1
    ):
        raise WorkflowReportingBenchmarkError(
            f"{policy} benchmark task did not succeed on its first attempt"
        )
    identity = _run_identity(execution)
    selection = _selection(execution, policy)
    result = _result_summary(
        execution,
        fast_items=fast_items,
        slow_items=slow_items,
    )
    expected_node_count, expected_edge_count = _expected_dynamic_topology(
        fast_items=fast_items,
        slow_items=slow_items,
    )
    ingress = (
        _full_snapshot_evidence(
            execution,
            identity=identity,
            selection=selection,
            fast_items=fast_items,
            slow_items=slow_items,
        )
        if policy == "full"
        else None
    )
    summary = _summary(
        execution,
        identity=identity,
        expected_policy=policy,
    )
    storage = _storage(execution, identity=identity)
    _validate_policy_contract(
        execution=execution,
        policy=policy,
        pilot_enabled=pilot_enabled,
        ingress=ingress,
        summary=summary,
        storage=storage,
        expected_node_count=expected_node_count,
        expected_edge_count=expected_edge_count,
    )
    shared_lifecycle_storage, attempt_summary_bytes = _lifecycle_storage(execution)
    return {
        "cycle": cycle,
        "position": position,
        "policy": policy,
        "execution": {
            "pk": execution.pk,
            "admin_path": f"/admin/django_ray/raytaskexecution/{execution.pk}/change/",
            "state": execution.state,
            "attempt_number": execution.attempt_number,
            "execution_generation": execution.execution_generation,
            "run_identity_present": True,
        },
        "selection": selection,
        "timing": _timing(execution),
        "client_polling": {
            "status": "diagnostic",
            "seconds": round(client_poll_seconds, 6),
            "poll_count": poll_count,
        },
        "workload_result": result,
        "reporting": {
            "actor_expected_count": 1 if policy == "full" else 0,
            "actor_count_status": "derived",
            "ingress": ingress,
        },
        "durable_reporting_storage": {
            "progress_data_bytes": _encoded_bytes(execution.progress_data),
            "summary_bytes": _encoded_bytes(execution.workflow_progress_summary_json),
            "attempt_summary_bytes": attempt_summary_bytes,
            "normalized": storage,
        },
        "shared_lifecycle_storage": shared_lifecycle_storage,
        "summary": summary,
    }


def _wait_for_terminal(
    execution_pk: int,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    load_execution: Callable[[int], RayTaskExecution] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[RayTaskExecution, float, int]:
    loader = load_execution or (lambda pk: RayTaskExecution.objects.get(pk=pk))
    started = monotonic()
    deadline = started + timeout_seconds
    polls = 0
    while True:
        execution = loader(execution_pk)
        polls += 1
        if execution.state in _TERMINAL_STATES:
            return execution, monotonic() - started, polls
        now = monotonic()
        if now >= deadline:
            raise WorkflowReportingBenchmarkError(
                f"benchmark execution {execution_pk} did not become terminal "
                f"within {timeout_seconds:g} seconds"
            )
        sleep(min(poll_interval_seconds, max(0.0, deadline - now)))


def _policy_aggregates(samples: Sequence[Mapping[str, object]]) -> dict[str, object]:
    aggregates: dict[str, object] = {}
    for policy in POLICIES:
        selected = [sample for sample in samples if sample.get("policy") == policy]
        if not selected:
            raise WorkflowReportingBenchmarkError(f"benchmark has no {policy} samples")

        policy_aggregate: dict[str, object] = {
            "sample_count": len(selected),
            "outer_execution_seconds": _distribution(
                _aggregate_values(
                    selected,
                    policy=policy,
                    section="timing",
                    field="outer_execution_seconds",
                )
            ),
            "durable_end_to_end_seconds": _distribution(
                _aggregate_values(
                    selected,
                    policy=policy,
                    section="timing",
                    field="durable_end_to_end_seconds",
                )
            ),
            "workflow_elapsed_seconds": _distribution(
                _aggregate_values(
                    selected,
                    policy=policy,
                    section="workload_result",
                    field="workflow_elapsed_seconds",
                )
            ),
            "useful_leaf_seconds": _distribution(
                _aggregate_values(
                    selected,
                    policy=policy,
                    section="workload_result",
                    field="useful_leaf_seconds",
                )
            ),
        }
        if policy == "full":
            policy_aggregate["actor_observed_cost"] = _actor_cost_aggregate(selected)
            policy_aggregate["producer_progress"] = _producer_progress_aggregate(selected)
        else:
            policy_aggregate["actor_observed_cost"] = {
                "status": "not_applicable",
                "reason": f"{policy} reporting creates no progress actor",
            }
            policy_aggregate["producer_progress"] = {
                "status": "not_applicable",
                "reason": f"{policy} reporting has no progress producer session",
            }
        aggregates[policy] = policy_aggregate
    return aggregates


def _actor_cost_aggregate(
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    aggregate: dict[str, object] = {
        "status": "measured",
        "source_schema_version": 1,
    }
    for group, fields in _ACTOR_COST_NUMERIC_FIELDS.items():
        aggregate[group] = {
            field: _distribution(
                _actor_cost_values(
                    samples,
                    group=group,
                    field=field,
                )
            )
            for field in fields
        }
    ingest = aggregate["ingest"]
    if not isinstance(ingest, dict):
        raise AssertionError("actor cost aggregate ingest group must be an object")
    ingest["decoded_by_kind"] = {
        kind: _distribution(
            _actor_cost_values(
                samples,
                group="ingest",
                field="decoded_by_kind",
                nested_field=kind,
            )
        )
        for kind in sorted(_EXPECTED_INGRESS_KINDS)
    }
    return aggregate


def _actor_cost_values(
    samples: Sequence[Mapping[str, object]],
    *,
    group: str,
    field: str,
    nested_field: str | None = None,
) -> list[int]:
    collected: list[int] = []
    for sample in samples:
        reporting = sample.get("reporting")
        ingress = reporting.get("ingress") if isinstance(reporting, Mapping) else None
        actor_cost = ingress.get("actor_cost") if isinstance(ingress, Mapping) else None
        group_value = actor_cost.get(group) if isinstance(actor_cost, Mapping) else None
        if not isinstance(group_value, Mapping):
            raise WorkflowReportingBenchmarkError(f"full actor cost group {group} is invalid")
        value = group_value.get(field)
        field_path = f"{group}.{field}"
        if nested_field is not None:
            if not isinstance(value, Mapping):
                raise WorkflowReportingBenchmarkError(
                    f"full actor cost field {field_path} is invalid"
                )
            value = value.get(nested_field)
            field_path = f"{field_path}.{nested_field}"
        collected.append(
            _non_negative_int(
                value,
                f"full.reporting.ingress.actor_cost.{field_path}",
            )
        )
    return collected


def _producer_progress_aggregate(
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    units: dict[str, object] = dict.fromkeys(_PRODUCER_NUMERIC_FIELDS, "count")
    units["terminal_handoffs"] = dict.fromkeys(
        sorted(_EXPECTED_TERMINAL_HANDOFFS),
        "count",
    )
    return {
        "status": "measured",
        "source_schema_version": 1,
        "units": units,
        **{
            field: _distribution(
                _producer_progress_values(
                    samples,
                    field=field,
                )
            )
            for field in _PRODUCER_NUMERIC_FIELDS
        },
        "terminal_handoffs": {
            outcome: _distribution(
                _producer_progress_values(
                    samples,
                    field="terminal_handoffs",
                    nested_field=outcome,
                )
            )
            for outcome in sorted(_EXPECTED_TERMINAL_HANDOFFS)
        },
    }


def _producer_progress_values(
    samples: Sequence[Mapping[str, object]],
    *,
    field: str,
    nested_field: str | None = None,
) -> list[int]:
    collected: list[int] = []
    for sample in samples:
        reporting = sample.get("reporting")
        ingress = reporting.get("ingress") if isinstance(reporting, Mapping) else None
        producer = ingress.get("producer") if isinstance(ingress, Mapping) else None
        if not isinstance(producer, Mapping):
            raise WorkflowReportingBenchmarkError(
                "full reporting sample has no producer progress evidence"
            )
        value = producer.get(field)
        field_path = field
        if nested_field is not None:
            if not isinstance(value, Mapping):
                raise WorkflowReportingBenchmarkError(
                    f"full producer progress field {field_path} is invalid"
                )
            value = value.get(nested_field)
            field_path = f"{field_path}.{nested_field}"
        collected.append(
            _non_negative_int(
                value,
                f"full.reporting.ingress.producer.{field_path}",
            )
        )
    return collected


def _aggregate_values(
    samples: Sequence[Mapping[str, object]],
    *,
    policy: str,
    section: str,
    field: str,
) -> list[float]:
    collected: list[float] = []
    for sample in samples:
        section_value = sample.get(section)
        if not isinstance(section_value, Mapping):
            raise WorkflowReportingBenchmarkError(f"sample section {section} is invalid")
        collected.append(
            _finite_non_negative(
                section_value.get(field),
                f"{policy}.{section}.{field}",
            )
        )
    return collected


def _validate_complete_report(
    samples: Sequence[Mapping[str, object]],
    *,
    repetitions: int,
) -> str:
    if len(samples) != repetitions * len(POLICIES):
        raise WorkflowReportingBenchmarkError("benchmark sample matrix is incomplete")
    plan_fingerprints: set[str] = set()
    execution_pks: set[int] = set()
    for cycle in range(repetitions):
        cycle_samples = [sample for sample in samples if sample.get("cycle") == cycle + 1]
        if [sample.get("policy") for sample in cycle_samples] != list(_policy_order(cycle)):
            raise WorkflowReportingBenchmarkError(
                f"benchmark cycle {cycle + 1} does not match its counterbalanced order"
            )
        for position, sample in enumerate(cycle_samples, start=1):
            if sample.get("position") != position:
                raise WorkflowReportingBenchmarkError(
                    f"benchmark cycle {cycle + 1} has an invalid sample position"
                )
            selection = sample.get("selection")
            if not isinstance(selection, Mapping):
                raise WorkflowReportingBenchmarkError("benchmark sample selection is invalid")
            fingerprint = selection.get("plan_fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                raise WorkflowReportingBenchmarkError("benchmark sample has no plan fingerprint")
            plan_fingerprints.add(fingerprint)
            execution = sample.get("execution")
            pk = execution.get("pk") if isinstance(execution, Mapping) else None
            if isinstance(pk, bool) or not isinstance(pk, int) or pk < 1:
                raise WorkflowReportingBenchmarkError(
                    "benchmark sample has no valid execution primary key"
                )
            if pk in execution_pks:
                raise WorkflowReportingBenchmarkError(
                    "benchmark samples reused one execution primary key"
                )
            execution_pks.add(pk)
    if len(plan_fingerprints) != 1:
        raise WorkflowReportingBenchmarkError("workflow plan identity drifted during the benchmark")
    return plan_fingerprints.pop()


def _pilot_enabled() -> bool:
    config = getattr(settings, "DJANGO_RAY", {})
    return isinstance(config, Mapping) and config.get("WORKFLOW_PROGRESS_SCHEMA_V3_PILOT") is True


def _run_benchmark(
    *,
    repetitions: int,
    fast_items: int,
    slow_items: int,
    fast_seconds: float,
    slow_seconds: float,
    timeout_seconds: float,
    poll_interval_seconds: float,
    cleanup: bool,
    progress: Callable[[str], None],
) -> dict[str, object]:
    if connection.vendor != "postgresql":
        raise WorkflowReportingBenchmarkError(
            "live workflow reporting benchmark requires PostgreSQL"
        )
    environment_before = _environment()
    pilot_enabled = _pilot_enabled()
    workload = _workload(
        fast_items=fast_items,
        slow_items=slow_items,
        fast_seconds=fast_seconds,
        slow_seconds=slow_seconds,
    )
    samples: list[dict[str, object]] = []
    for cycle_index in range(repetitions):
        for position, policy_value in enumerate(_policy_order(cycle_index), start=1):
            policy = cast(Policy, policy_value)
            progress(
                f"cycle {cycle_index + 1}/{repetitions}, position {position}: enqueue {policy}"
            )
            result = complex_workflow_benchmark.enqueue(
                fast_items=fast_items,
                slow_items=slow_items,
                fast_seconds=fast_seconds,
                slow_seconds=slow_seconds,
                reporting_policy=policy,
            )
            execution = RayTaskExecution.objects.only("pk").get(task_id=result.id)
            terminal, client_poll_seconds, polls = _wait_for_terminal(
                execution.pk,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            sample = _sample(
                terminal,
                cycle=cycle_index + 1,
                position=position,
                policy=policy,
                client_poll_seconds=client_poll_seconds,
                poll_count=polls,
                fast_items=fast_items,
                slow_items=slow_items,
                pilot_enabled=pilot_enabled,
            )
            samples.append(sample)
            progress(
                f"cycle {cycle_index + 1}/{repetitions}, position {position}: "
                f"{policy} succeeded as execution {terminal.pk}"
            )
    plan_fingerprint = _validate_complete_report(samples, repetitions=repetitions)
    environment_after = _environment()
    if environment_after != environment_before:
        raise WorkflowReportingBenchmarkError(
            "benchmark environment or implementation changed during the run"
        )
    cleanup_result = {
        "requested": cleanup,
        "status": "pending" if cleanup else "not_requested",
        "execution_rows_deleted": 0,
        "retained_for_admin_inspection": True,
    }
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark": BENCHMARK_ID,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "configuration": {
            "repetitions": repetitions,
            "policy_orders": [list(_policy_order(index)) for index in range(repetitions)],
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "schema_v3_pilot_enabled": pilot_enabled,
            "execution_scope": (
                "sequential durable tasks on the configured local KubeRay testproject"
            ),
            "primary_timing_source": "durable database timestamps",
            "percentile_method": "nearest-rank",
        },
        "workload": workload,
        "environment": environment_before,
        "source_identity": {
            "workflow_plan_fingerprint": plan_fingerprint,
            "checkout_to_deployment_tree_attestation": "external-local-kuberay-gate",
        },
        "measurement_coverage": _measurement_coverage(),
        "samples": samples,
        "policy_aggregates": _policy_aggregates(samples),
        "cleanup": cleanup_result,
        "interpretation": [
            "This bounded local run is comparative evidence, not a production SLO.",
            "Elapsed-time differences do not prove reporting-layer causality.",
            "Manifest and run aggregate bytes already include their child payload bytes.",
            "Client polling duration is diagnostic and is excluded from policy aggregates.",
        ],
    }


def _owned_execution_pks(report: Mapping[str, object]) -> tuple[int, ...]:
    samples = report.get("samples")
    if not isinstance(samples, list) or not samples:
        raise WorkflowReportingBenchmarkError(
            "benchmark cleanup has no validated execution samples"
        )
    pks: list[int] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise WorkflowReportingBenchmarkError("benchmark cleanup sample is invalid")
        execution = sample.get("execution")
        if not isinstance(execution, Mapping):
            raise WorkflowReportingBenchmarkError("benchmark cleanup execution evidence is invalid")
        pk = execution.get("pk")
        if isinstance(pk, bool) or not isinstance(pk, int) or pk < 1:
            raise WorkflowReportingBenchmarkError(
                "benchmark cleanup execution primary key is invalid"
            )
        pks.append(pk)
    if len(set(pks)) != len(pks):
        raise WorkflowReportingBenchmarkError("benchmark cleanup execution ownership is not unique")
    return tuple(pks)


def _cleanup_owned_executions(report: Mapping[str, object]) -> int:
    pks = _owned_execution_pks(report)
    with transaction.atomic():
        exact = RayTaskExecution.objects.select_for_update().filter(
            pk__in=pks,
            callable_path=EXPECTED_CALLABLE_PATH,
        )
        if exact.count() != len(pks):
            raise WorkflowReportingBenchmarkError(
                "benchmark cleanup could not prove exact execution ownership"
            )
        exact.delete()
        if RayTaskExecution.objects.filter(pk__in=pks).exists():
            raise WorkflowReportingBenchmarkError("benchmark cleanup left an owned execution row")
    return len(pks)


def _serialize_report(report: Mapping[str, object]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _write_new_report(path: Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.write("\n")
    except FileExistsError as error:
        raise WorkflowReportingBenchmarkError(
            f"refusing to overwrite existing file: {path}"
        ) from error


def _replace_report(path: Path, serialized: str) -> None:
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(serialized)
            output.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class Command(BaseCommand):
    """Run the opt-in local-KubeRay workflow reporting benchmark."""

    help = (
        "Compare observable full, terminal-only, and disabled workflow reporting "
        "costs on the bundled local KubeRay testproject."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--repetitions", type=int, default=3)
        parser.add_argument("--fast-items", type=int, default=2)
        parser.add_argument("--slow-items", type=int, default=1)
        parser.add_argument("--fast-seconds", type=float, default=0.01)
        parser.add_argument("--slow-seconds", type=float, default=0.02)
        parser.add_argument("--timeout-seconds", type=float, default=120.0)
        parser.add_argument("--poll-interval-seconds", type=float, default=0.25)
        parser.add_argument("--cleanup", action="store_true")
        parser.add_argument("--output-json", type=Path)

    @staticmethod
    def _bounded_int(value: object, name: str, *, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise CommandError(f"{name} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _bounded_float(
        value: object,
        name: str,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise CommandError(f"{name} must be between {minimum:g} and {maximum:g}")
        return float(value)

    def handle(self, *args: object, **options: object) -> None:
        del args
        if os.environ.get(OPT_IN_ENV, "").strip().lower() not in {"1", "true", "yes"}:
            raise CommandError(f"set {OPT_IN_ENV}=1 to run the live benchmark")
        repetitions = self._bounded_int(
            options["repetitions"],
            "--repetitions",
            minimum=3,
            maximum=30,
        )
        if repetitions % len(POLICY_ORDERS) != 0:
            raise CommandError(
                f"--repetitions must be a multiple of {len(POLICY_ORDERS)} "
                "to preserve the complete counterbalanced order"
            )
        fast_items = self._bounded_int(
            options["fast_items"],
            "--fast-items",
            minimum=1,
            maximum=100,
        )
        slow_items = self._bounded_int(
            options["slow_items"],
            "--slow-items",
            minimum=1,
            maximum=100,
        )
        fast_seconds = self._bounded_float(
            options["fast_seconds"],
            "--fast-seconds",
            minimum=0.01,
            maximum=10,
        )
        slow_seconds = self._bounded_float(
            options["slow_seconds"],
            "--slow-seconds",
            minimum=0.01,
            maximum=10,
        )
        timeout_seconds = self._bounded_float(
            options["timeout_seconds"],
            "--timeout-seconds",
            minimum=1,
            maximum=1800,
        )
        poll_interval_seconds = self._bounded_float(
            options["poll_interval_seconds"],
            "--poll-interval-seconds",
            minimum=0.05,
            maximum=10,
        )
        cleanup = options["cleanup"] is True
        output_path = options.get("output_json")
        if cleanup and output_path is None:
            raise CommandError(
                "--cleanup requires --output-json so evidence is written before deletion"
            )
        if output_path is not None:
            if not isinstance(output_path, Path):
                raise CommandError("--output-json must be a filesystem path")
            if output_path.exists():
                raise CommandError(f"refusing to overwrite existing file: {output_path}")
        try:
            report = _run_benchmark(
                repetitions=repetitions,
                fast_items=fast_items,
                slow_items=slow_items,
                fast_seconds=fast_seconds,
                slow_seconds=slow_seconds,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                cleanup=cleanup,
                progress=self.stderr.write,
            )
        except WorkflowReportingBenchmarkError as error:
            raise CommandError(str(error)) from error
        serialized = _serialize_report(report)
        try:
            if output_path is not None:
                _write_new_report(output_path, serialized)
            if cleanup:
                deleted = _cleanup_owned_executions(report)
                cleanup_result = report.get("cleanup")
                if not isinstance(cleanup_result, dict):
                    raise WorkflowReportingBenchmarkError("benchmark cleanup receipt is invalid")
                cleanup_result.update(
                    status="completed",
                    execution_rows_deleted=deleted,
                    retained_for_admin_inspection=False,
                )
                serialized = _serialize_report(report)
                _replace_report(cast(Path, output_path), serialized)
        except (OSError, WorkflowReportingBenchmarkError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(serialized)
