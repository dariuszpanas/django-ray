"""Finite failure-artifact projections; never retain free-form process output."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime

from qualification.application.http_context import HttpObserver, HttpSubstage
from qualification.application.run_api import HttpFailureCode, HttpTransportFailure
from qualification.application.worker_readiness_diagnostics import parse_report

PHASES = {"first", "existing", "rq2", "showcase", "protocol", "cleanup", "generation"}
COMMANDS = {"first", "nodes", "core", "workflows", "rq2", "showcase", "protocol"}
REASONS = {
    "timeout",
    "process_error",
    "nonzero_exit",
    "terminated",
    "interrupted",
    "process_group_stop_incomplete",
    "incomplete_command_set",
}
EVENT_REASONS = {
    "Scheduled",
    "Pulled",
    "Pulling",
    "Created",
    "Started",
    "Killing",
    "BackOff",
    "Failed",
    "FailedCreate",
    "FailedScheduling",
    "Unhealthy",
    "SuccessfulCreate",
    "SuccessfulDelete",
}
STATES = {
    "Pending",
    "Running",
    "Succeeded",
    "Failed",
    "Unknown",
    "ready",
    "suspended",
    "failed",
    "pending",
    "terminating",
}
WORKLOAD_ROLES = frozenset(
    {
        "django-web",
        "django-manager",
        "postgres",
        "ray-head",
        "ray-worker",
        "assert-before",
        "assert-after",
        "reporting-benchmark",
    }
)
CONTAINER_ROLES = frozenset(
    {"web", "manager", "postgres", "ray-head", "ray-worker", "assertions", "setup", "ray-tmp"}
)
MANAGER_READINESS_COMMAND = (
    "/bin/sh -c exec python testproject/manage.py django_ray_worker_ready "
    '--queue "$DJANGO_RAY_QUEUES" --hostname "$DJANGO_RAY_POD_NAME" --json'
)
MANAGER_READINESS_TIMEOUT = (
    "Readiness probe failed: command timed out: "
    + json.dumps(MANAGER_READINESS_COMMAND)
    + " timed out after 10s"
)
READINESS_PREFIX = "Readiness probe failed: "
READINESS_SUFFIX = "\nCommandError: Worker lease is not ready.\n"


def _timestamp(value):
    """Keep only valid Kubernetes UTC timestamps, never arbitrary event text."""
    if (
        type(value) is not str
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value) is None
    ):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _workload_role(value):
    """Map owned fixed Pod-name prefixes to roles without emitting their names."""
    if type(value) is str and len(value) <= 253:
        for role in WORKLOAD_ROLES:
            if re.fullmatch(re.escape(role) + r"(?:-[a-z0-9]+)*", value):
                return role
    return "unavailable"


def _probe_failure(item):
    """Classify canonical kubelet prefixes; never infer timeouts from free text."""
    message = item.get("message")
    if item.get("reason") != "Unhealthy" or type(message) is not str:
        return "unavailable"
    for probe in ("readiness", "liveness", "startup"):
        if message.startswith(f"{probe.capitalize()} probe failed: "):
            return f"{probe}_failed"
        if message.startswith(
            f"{probe.capitalize()} probe errored and resulted in unknown state: "
        ):
            return f"{probe}_errored"
    return "unavailable"


def _readiness_detail(item, reference):
    """Classify only the pinned manager probe grammar; never retain its text."""
    unknown = {"kind": "unknown", "status": "unknown", "reason": "unknown"}
    message = item.get("message")
    if (
        item.get("type") != "Warning"
        or item.get("reason") != "Unhealthy"
        or reference.get("kind") != "Pod"
        or _workload_role(reference.get("name")) != "django-manager"
        or reference.get("fieldPath") != "spec.containers{manager}"
        or type(message) is not str
        or len(message) > 512
    ):
        return unknown
    try:
        if len(message.encode()) > 512:
            return unknown
    except UnicodeError:
        return unknown
    # Kubernetes v1.35.8 CRI timeout formatting for the fixed fixture argv.
    # Unknown runtimes, command changes and arbitrary timeout phrases stay unknown.
    if message == MANAGER_READINESS_TIMEOUT:
        return {"kind": "exec_timeout", "status": "unknown", "reason": "unknown"}
    if not (message.startswith(READINESS_PREFIX) and message.endswith(READINESS_SUFFIX)):
        return unknown
    report = parse_report(message[len(READINESS_PREFIX) : -len(READINESS_SUFFIX)].encode())
    if report is None or report["status"] == "ready":
        return unknown
    # Events carry no independent process-exit observation.
    return {"kind": "worker_report", **report}


STAGES = {
    "configuration",
    "receipt",
    "application_api",
    "api_execution_ownership",
    "remote_runtime_env",
    "probe_execution_ownership",
    "dashboard_proxy",
    "runtime_env_refusal",
    "runtime_env_maintenance",
    "lease_policy",
    "protected_runtime_surfaces",
    "ordinary_result_presentation",
    "collector-pressure",
    "recovery",
    "plan-overflow",
    "admin-display-limit",
    "retry-success",
    "retry-exhausted",
    "retry-unlimited",
    *(
        f"{policy}-{outcome}"
        for policy in ("full", "terminal_only", "disabled")
        for outcome in ("success", "failure")
    ),
}

RECEIPT_FIELDS = {
    "application_setup": {
        "postgresql_major",
        "migrations_applied",
        "static_collected",
        "administrator_created",
        "archives",
    },
    "generic_ray_nodes": {"cold_replacement", "previous_receipt_sha256", "observations"},
    "application_core": {
        "failed_stage",
        "api",
        "executions",
        "dashboard",
        "runtime_env_refusals",
        "runtime_env_maintenance",
        "lease_policy",
        "protected_runtime",
        "ordinary_results",
    },
    "workflow_api_admin": {
        "complete_workflow_gate",
        "failed_stage",
        "http_transport_recoveries",
        "publisher",
        "observations",
        "collector_pressure",
    },
    "first_workflow_pair": {
        "complete_workflow_gate",
        "ordering",
        "inputs",
        "terminal_flush_timeout_seconds",
        "qualification_profile",
        "trials",
    },
    "application_rq2": {"generation", "budget_seconds", "assertions", "diagnostics"},
    "workflow_reporting_benchmark": {"report"},
    "application_showcase_failure": {
        "generation",
        "complete_workflow_gate",
        "failed_stage",
        "observation",
    },
    "application_protocol": {
        "generation",
        "budget_seconds",
        "assertions",
        "failure_phase",
        "failure_type",
        "cleanup_failed",
    },
}


def passed_receipt(value: dict) -> dict:
    """The collector writes parsed source receipts, never appended log fields."""
    allowed = RECEIPT_FIELDS.get(value.get("layer"))
    common = {"schema_version", "layer", "status", "complete_application_gate"}
    if allowed is None or set(value) - (allowed | common):
        raise ValueError("Application receipt contains unknown fields")
    return value


def number(value, maximum=1000000):
    return (
        value
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= maximum
        else None
    )


def choice(value, allowed):
    return value if type(value) is str and value in allowed else "unavailable"


def http(value):
    if not isinstance(value, dict):
        return None
    result = {
        "code": choice(value.get("code"), set(HttpFailureCode)),
        "status": value.get("status")
        if type(value.get("status")) is int and 100 <= value["status"] <= 599
        else None,
    }
    for name, allowed in (
        ("transport_failure", set(HttpTransportFailure)),
        ("observer", set(HttpObserver)),
        ("request_substage", set(HttpSubstage)),
        ("method", {"GET", "POST", "DELETE"}),
        ("retry_policy", {"none", "one_get_pre_status_disconnect_or_reset"}),
    ):
        if name in value:
            result[name] = choice(value[name], allowed)
    for name in (
        "configured_timeout_seconds",
        "attempt_timeout_seconds",
        "retry_remaining_seconds",
        "observer_remaining_seconds",
        "outer_remaining_seconds",
    ):
        if name in value:
            result[name] = number(value[name], 600)
    if "attempt" in value:
        result["attempt"] = number(value["attempt"], 2)
    # Older WorkflowHttpError carries a finite semantic endpoint, not a URL.
    if "endpoint" in value:
        result["endpoint"] = choice(
            value["endpoint"],
            {
                "summary",
                "admin_graph",
                "topology_nodes",
                "topology_edges",
                "node_details",
                "node_detail",
            },
        )
    return result


def lines(raw: bytes) -> list[dict]:
    if len(raw) > 65536:
        return []
    projected = []
    for line in raw.splitlines():
        if len(projected) >= 64:
            break
        if len(line) > 16384:
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(value, dict):
            continue
        if type(value.get("generation_phase")) is str and value["generation_phase"] in PHASES:
            item = {
                "generation_phase": value["generation_phase"],
                "status": choice(value.get("status"), {"started", "passed", "failed"}),
            }
            for name, allowed in (("command", COMMANDS), ("reason", REASONS)):
                if name in value:
                    item[name] = choice(value[name], allowed)
            for name in ("budget_seconds", "elapsed_seconds", "returncode"):
                if name in value:
                    item[name] = number(value[name], 3600)
            projected.append(item)
        elif type(value.get("layer")) is str and value["layer"] in {
            "application_core",
            "workflow_api_admin",
            "application_rq2",
            "first_workflow_pair",
            "application_showcase_failure",
            "application_protocol",
        }:
            item = {
                "layer": value["layer"],
                "status": choice(value.get("status"), {"passed", "failed"}),
            }
            if "failed_stage" in value:
                allowed = (
                    {"bootstrap", "observe", "receipt"}
                    if value["layer"] == "application_showcase_failure"
                    else STAGES
                )
                item["failed_stage"] = choice(value["failed_stage"], allowed)
            if value["layer"] == "application_showcase_failure" and "failure_kind" in value:
                item["failure_kind"] = choice(value["failure_kind"], {"timeout", "validation"})
            if value["layer"] == "application_protocol":
                item["failure_phase"] = choice(
                    value.get("failure_phase"),
                    {
                        "preflight",
                        "bootstrap",
                        "baseline",
                        "seed",
                        "shared_mount",
                        "heartbeat",
                        "strict_refusal",
                        "task_status",
                        "execution_list",
                        "metrics",
                        "protocol_status",
                        "preservation",
                        "remote_cleanup",
                        "database_cleanup",
                        "files_cleanup",
                        "receipt",
                    },
                )
                item["failure_type"] = choice(
                    value.get("failure_type"), {"timeout", "validation", "unavailable"}
                )
                item["cleanup_failed"] = value.get("cleanup_failed") is True
            if "failure_code" in value:
                from qualification.application.run_core import CoreEvidenceFailure

                item["failure_code"] = choice(
                    value["failure_code"], {*CoreEvidenceFailure, "unclassified"}
                )
            counters = value.get("api_diagnostics")
            if isinstance(counters, dict):
                item["api_diagnostics"] = {
                    "requests": number(counters.get("requests")),
                    "last_http_status": counters.get("last_http_status")
                    if type(counters.get("last_http_status")) is int
                    and 100 <= counters["last_http_status"] <= 599
                    else None,
                }
            if "http_failure" in value:
                item["http_failure"] = http(value["http_failure"])
            if "runtime_env_maintenance" in value:
                from qualification.application.runtime_env_maintenance import FACTS

                original = value["runtime_env_maintenance"]
                if isinstance(original, dict):
                    item["runtime_env_maintenance"] = {
                        case: {
                            name: isinstance(original.get(case), dict)
                            and original[case].get(name) is True
                            for name in names
                        }
                        for case, names in FACTS.items()
                    }
            if "lease_policy" in value:
                from qualification.application.lease_policy import FACTS as LEASE_FACTS

                original = value["lease_policy"]
                if isinstance(original, dict):
                    item["lease_policy"] = {
                        name: original.get(name) is True for name in LEASE_FACTS
                    }
            if value["layer"] == "application_rq2":
                from qualification.application.run_rq2 import Diagnostics

                original = value.get("diagnostics")
                if isinstance(original, dict):
                    diagnostic = Diagnostics()
                    for key in (
                        "phase",
                        "failure_phase",
                        "failure_type",
                        "manager_at_failure",
                        "cleanup_failure_type",
                        "manager_after_cleanup",
                        "start_observation",
                    ):
                        if key in original:
                            setattr(diagnostic, key, original[key])
                    item["diagnostics"] = diagnostic.snapshot()
            projected.append(item)
    return projected


def resources(kind: str, items: list) -> list:
    if not isinstance(items, list):
        return []
    result = []
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        if kind == "events":
            reference = item.get("involvedObject")
            if not isinstance(reference, dict):
                reference = {}
            series = item.get("series")
            if not isinstance(series, dict):
                series = {}
            result.append(
                {
                    "role": _workload_role(reference.get("name"))
                    if reference.get("kind") == "Pod"
                    else "unavailable",
                    "observed_at": observed_at,
                    "type": choice(item.get("type"), {"Normal", "Warning"}),
                    "reason": choice(item.get("reason"), EVENT_REASONS),
                    "count": number(item.get("count")),
                    "probe_failure": _probe_failure(item),
                    "readiness_detail": _readiness_detail(item, reference),
                    "first_timestamp": _timestamp(item.get("firstTimestamp")),
                    "last_timestamp": _timestamp(item.get("lastTimestamp")),
                    "event_time": _timestamp(item.get("eventTime")),
                    "series_last_observed_time": _timestamp(series.get("lastObservedTime")),
                }
            )
            continue
        status = item.get("status", {})
        if not isinstance(status, dict):
            status = {}
        entry = {"state": choice(status.get("phase", status.get("state")), STATES)}
        if kind == "pods":
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            entry.update(role=_workload_role(metadata.get("name")), observed_at=observed_at)
            conditions = status.get("conditions")
            entry["conditions"] = [
                {
                    "type": condition["type"],
                    "status": choice(condition.get("status"), {"True", "False", "Unknown"}),
                    "last_transition_time": _timestamp(condition.get("lastTransitionTime")),
                }
                for condition in (conditions[:16] if isinstance(conditions, list) else [])
                if isinstance(condition, dict)
                and type(condition.get("type")) is str
                and condition["type"] in {"Ready", "ContainersReady", "Initialized", "PodScheduled"}
            ]
        containers = []
        statuses = []
        for field in ("containerStatuses", "initContainerStatuses"):
            if isinstance(status.get(field), list):
                statuses += status[field][:8]
        for container in statuses[:8]:
            if not isinstance(container, dict):
                continue
            state = container.get("state", {})
            if not isinstance(state, dict):
                state = {}
            done = state.get("terminated", {})
            if not isinstance(done, dict):
                done = {}
            containers.append(
                {
                    "role": choice(container.get("name"), CONTAINER_ROLES),
                    "ready": container.get("ready") is True,
                    "restart_count": number(container.get("restartCount")),
                    "state": next(
                        (key for key in ("running", "waiting", "terminated") if key in state),
                        "unavailable",
                    ),
                    "exit_code": number(done.get("exitCode"), 255),
                    "signal": number(done.get("signal"), 64),
                }
            )
        entry["containers"] = containers
        result.append(entry)
    return result


def actor(value):
    from qualification.application.actor_diagnostics import FIELDS

    if not isinstance(value, dict) or value.get("diagnostic_only") is not True:
        raise ValueError("Invalid actor diagnostic receipt")
    resources = value.get("resources")
    if not isinstance(resources, dict) or set(resources) != set(FIELDS):
        raise ValueError("Invalid actor diagnostic resources")
    projected = {
        "schema_version": 1,
        "diagnostic_only": True,
        "status": choice(value.get("status"), {"available", "partial", "unavailable"}),
        "resources": {},
    }
    for kind, fields in FIELDS.items():
        selected = resources[kind]
        if (
            not isinstance(selected, dict)
            or not isinstance(selected.get("records"), list)
            or len(selected["records"]) > 32
        ):
            raise ValueError("Invalid actor diagnostic record count")
        records = []
        for record in selected["records"]:
            if not isinstance(record, dict):
                raise ValueError("Invalid actor diagnostic record")
            clean = {}
            for key in fields:
                raw = record.get(key)
                if key.endswith("_id"):
                    clean[key] = (
                        raw
                        if isinstance(raw, str) and re.fullmatch(r"[0-9a-f]{8,64}", raw)
                        else None
                    )
                elif key == "state":
                    clean[key] = choice(
                        raw,
                        {
                            "ALIVE",
                            "DEAD",
                            "PENDING_CREATION",
                            "DEPENDENCIES_UNREADY",
                            "PENDING_NODE_ASSIGNMENT",
                            "SUBMITTED_TO_WORKER",
                            "RUNNING",
                            "FINISHED",
                            "FAILED",
                            "PENDING_ARGS_AVAIL",
                            "PENDING_OBJ_STORE_MEM_AVAIL",
                        },
                    )
                elif key in {"class_name", "func_or_class_name"}:
                    clean[key] = choice(
                        raw, {"WorkflowProgressActor", "WorkflowProgressActor.snapshot"}
                    )
                elif key == "type":
                    clean[key] = choice(raw, {"ACTOR_TASK", "ACTOR_CREATION_TASK", "NORMAL_TASK"})
                elif key != "node_ip":
                    clean[key] = number(raw, 1e16)
            records.append(clean)
        projected["resources"][kind] = {
            "status": choice(selected.get("status"), {"available", "partial", "unavailable"}),
            "records": records,
        }
    return projected
