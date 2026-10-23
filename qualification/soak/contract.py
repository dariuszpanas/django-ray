"""Bounded soak observations, distinct from actual execution or release acceptance.

Only the standard library is used. This module reads no cluster, database, or
task payload. Assertion producers and the admitting execution provider must
establish the facts; validating a JSON document cannot establish their truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

CATALOGUE_PATH = Path(__file__).with_name("profiles_v1.json")
MAX_REPORT_BYTES = 32 * 1024 * 1024
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "LOST", "EXPIRED"}
STATES = TERMINAL_STATES | {"QUEUED", "RUNNING", "CANCELLING"}
RESIDUE = {"processes", "listeners", "leases", "ray_jobs", "temporary_files", "external_artifacts"}
VERSION_FIELDS = {"python", "django", "ray", "postgresql", "kubernetes", "kuberay"}
IMAGE_FIELDS = {"application", "ray", "postgresql", "prometheus", "grafana", "kuberay_operator"}
FAILURES = {"setup", "task", "fault", "resource", "retention", "cleanup", "http", "identity"}


class ContractError(ValueError):
    """A fixed diagnostic category, never input text or dependency diagnostics."""


def require(condition, code):
    if not condition:
        raise ContractError(code)


def shape(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "invalid-shape")


def number(value, maximum, *, integer=False):
    require(
        (type(value) is int if integer else type(value) in (int, float))
        and 0 <= value <= maximum
        and math.isfinite(value),
        "invalid-number",
    )


def flags(value, keys):
    require(all(type(value[key]) is bool for key in keys), "invalid-flag")


def choice(value, allowed, code):
    require(isinstance(value, str) and value in allowed, code)


def digest(value, length=64):
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value),
        "invalid-digest",
    )


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate-json-key")
        result[key] = value
    return result


def _decode(raw, limit):
    require(isinstance(raw, bytes) and len(raw) <= limit, "report-byte-limit")
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ContractError("invalid-json") from error


def catalogue():
    """Return the source-owned proposed parameters, never a mutable shared default."""
    return _decode(CATALOGUE_PATH.read_bytes(), 64 * 1024)


def profile(name):
    data = catalogue()
    require(isinstance(name, str) and name in data["profiles"], "unknown-profile")
    return data["profiles"][name]


def profile_digest(name):
    """Bind catalogue, assertion owners, resource limits and selected profile."""
    data = catalogue()
    data["profiles"] = {name: profile(name)}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _source(value):
    shape(
        value,
        {"commit", "tree", "archive_sha256", "wheel_sha256", "images", "versions", "platform"},
    )
    require(value["platform"] == "linux", "unsupported-platform")
    for key in ("commit", "tree"):
        digest(value[key], 40)
    for key in ("archive_sha256", "wheel_sha256"):
        digest(value[key])
    shape(value["images"], IMAGE_FIELDS)
    for image in value["images"].values():
        digest(image)
    shape(value["versions"], VERSION_FIELDS)
    for version in value["versions"].values():
        require(
            isinstance(version, str) and re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,63}", version),
            "invalid-version",
        )


def _tasks(tasks, limits, kinds):
    require(isinstance(tasks, list) and len(tasks) <= limits["task_count"], "task-limit")
    identities = set()
    for task in tasks:
        shape(
            task,
            {
                "id",
                "kind",
                "runner",
                "submitted_seconds",
                "terminal_seconds",
                "state",
                "attempts",
                "attempt_history",
                "reconciliations",
                "ownership_transfers",
                "outcome_verified",
                "remote_quiescent",
                "uncertain",
            },
        )
        number(task["id"], limits["task_count"], integer=True)
        require(task["id"] > 0 and task["id"] not in identities, "task-identity")
        identities.add(task["id"])
        choice(task["kind"], kinds, "task-kind")
        choice(task["runner"], {"core", "job"}, "task-kind")
        choice(task["state"], STATES, "task-state")
        number(task["submitted_seconds"], 86400)
        if task["terminal_seconds"] is not None:
            number(task["terminal_seconds"], 86400)
            require(task["terminal_seconds"] >= task["submitted_seconds"], "task-chronology")
        for key in ("attempts", "reconciliations", "ownership_transfers"):
            number(task[key], 64, integer=True)
        history = task["attempt_history"]
        require(isinstance(history, list) and len(history) <= 64, "attempt-limit")
        for attempt in history:
            shape(attempt, {"number", "state", "started", "generation"})
            number(attempt["number"], 64, integer=True)
            number(attempt["generation"], 2**31 - 1, integer=True)
            choice(attempt["state"], STATES, "attempt-state")
            flags(attempt, ("started",))
        flags(task, ("outcome_verified", "remote_quiescent", "uncertain"))


def _faults(values, limits, kinds):
    require(
        isinstance(values, list) and len(values) <= len(kinds) * limits["fault_repetitions"],
        "fault-limit",
    )
    identities = set()
    previous = 0
    for fault in values:
        shape(
            fault,
            {
                "kind",
                "repetition",
                "started_seconds",
                "injected_seconds",
                "recovered_seconds",
                "outcome_verified",
                "remote_quiescent",
                "uncertain",
            },
        )
        choice(fault["kind"], kinds, "fault-kind")
        number(fault["repetition"], limits["fault_repetitions"], integer=True)
        identity = (fault["kind"], fault["repetition"])
        require(fault["repetition"] > 0 and identity not in identities, "fault-identity")
        identities.add(identity)
        number(fault["started_seconds"], 86400)
        require(previous <= fault["started_seconds"], "fault-chronology")
        previous = fault["started_seconds"]
        for key in ("injected_seconds", "recovered_seconds"):
            if fault[key] is not None:
                number(fault[key], 86400)
                require(previous <= fault[key], "fault-chronology")
                previous = fault[key]
        require(
            fault["recovered_seconds"] is None or fault["injected_seconds"] is not None,
            "fault-chronology",
        )
        flags(fault, ("outcome_verified", "remote_quiescent", "uncertain"))


def _http(value, limits):
    shape(value, {"historical_attribution", "supported_core_disposition", "observations"})
    require(value["historical_attribution"] == "unresolved", "historical-attribution")
    choice(
        value["supported_core_disposition"],
        {
            "pending",
            "no_unexpected_outcomes",
            "unresolved_defect",
        },
        "http-disposition",
    )
    rows = value["observations"]
    require(isinstance(rows, list) and len(rows) <= 128 * limits["fault_repetitions"], "http-limit")
    for row in rows:
        shape(
            row,
            {
                "repetition",
                "observer",
                "request_substage",
                "phase",
                "status",
                "transport",
                "configured_timeout_seconds",
                "attempt_timeout_seconds",
                "retry_policy",
                "outer_remaining_seconds",
                "observer_remaining_seconds",
                "expected",
            },
        )
        number(row["repetition"], limits["fault_repetitions"], integer=True)
        require(row["repetition"] > 0, "http-repetition")
        choice(
            row["observer"],
            {
                "application_api",
                "application_core",
                "first_workflow",
                "workflow",
                "nested_admin",
            },
            "http-observer",
        )
        choice(
            row["request_substage"],
            {
                "task_status",
                "workflow_summary",
                "workflow_collection",
                "admin_graph",
                "admin_page",
                "other",
            },
            "http-substage",
        )
        choice(
            row["phase"],
            {"complete", "request_failed", "response_headers_failed", "body_read_failed"},
            "http-phase",
        )
        choice(
            row["transport"],
            {
                "none",
                "remote_disconnected",
                "connection_reset",
                "timeout",
                "http_protocol_error",
                "os_error",
                "invalid_value",
            },
            "http-transport",
        )
        choice(
            row["retry_policy"], {"none", "one_get_pre_status_disconnect_or_reset"}, "http-retry"
        )
        number(row["status"], 599, integer=True)
        require(row["status"] == 0 or row["status"] >= 100, "http-status")
        for key in (
            "configured_timeout_seconds",
            "attempt_timeout_seconds",
            "outer_remaining_seconds",
            "observer_remaining_seconds",
        ):
            if row[key] is not None:
                number(row[key], limits["duration_seconds"])
        flags(row, ("expected",))


def validate_report(value):
    """Validate structure, bounds and chronology; retain failed/partial observations."""
    shape(
        value,
        {
            "schema_version",
            "profile",
            "profile_sha256",
            "source",
            "status",
            "failure_codes",
            "elapsed",
            "cold_ray",
            "submitted_count",
            "admission",
            "tasks",
            "faults",
            "resources",
            "retention",
            "cleanup",
            "http",
        },
    )
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "report-version")
    limits = profile(value["profile"])
    require(value["profile_sha256"] == profile_digest(value["profile"]), "profile-mismatch")
    _source(value["source"])
    choice(value["status"], {"passed", "failed", "partial"}, "report-status")
    codes = value["failure_codes"]
    require(isinstance(codes, list) and len(codes) <= len(FAILURES), "failure-codes")
    require(all(isinstance(code, str) and code in FAILURES for code in codes), "failure-codes")
    require(len(set(codes)) == len(codes), "failure-codes")
    shape(value["elapsed"], {"setup", "workload", "cleanup"})
    for seconds in value["elapsed"].values():
        number(seconds, 86400)
    flags(value, ("cold_ray",))
    number(value["submitted_count"], 1000000, integer=True)
    shape(value["admission"], {"verified", "limits"})
    flags(value["admission"], ("verified",))
    if value["admission"]["limits"] is not None:
        shape(value["admission"]["limits"], limits["admission_limits"])
        for observed in value["admission"]["limits"].values():
            number(observed, 2**63 - 1, integer=True)
    data = catalogue()
    _tasks(value["tasks"], limits, data["workloads"])
    _faults(value["faults"], limits, data["faults"])
    shape(value["resources"], limits["resources"])
    max_samples = limits["duration_seconds"] // limits["sample_interval_seconds"] + 1
    for samples in value["resources"].values():
        require(isinstance(samples, list) and len(samples) <= max_samples, "sample-limit")
        previous = -1
        for sample in samples:
            shape(sample, {"at_seconds", "value"})
            number(sample["at_seconds"], 86400)
            number(sample["value"], 2**63 - 1, integer=True)
            require(sample["at_seconds"] > previous, "sample-chronology")
            previous = sample["at_seconds"]
    retention = value["retention"]
    shape(
        retention,
        {
            "runs",
            "failed_runs",
            "deleted_input_objects",
            "deleted_workflow_details",
            "retained_task_summaries",
            "retained_attempt_summaries",
            "remaining_eligible_objects",
            "reclaimable_bytes",
            "terminal_outcomes_preserved",
        },
    )
    for key, observed in retention.items():
        if key != "terminal_outcomes_preserved":
            number(observed, 2**63 - 1, integer=True)
    flags(retention, ("terminal_outcomes_preserved",))
    shape(value["cleanup"], {"verified", "remaining", "retained_payload_bytes"})
    flags(value["cleanup"], ("verified",))
    shape(value["cleanup"]["remaining"], RESIDUE)
    for count in value["cleanup"]["remaining"].values():
        number(count, 2**31 - 1, integer=True)
    number(value["cleanup"]["retained_payload_bytes"], 2**63 - 1, integer=True)
    _http(value["http"], limits)
    return value


def parse_report(raw):
    """Bound input before parsing and reject duplicate keys and oversized profiles."""
    value = validate_report(_decode(raw, MAX_REPORT_BYTES))
    require(len(raw) <= profile(value["profile"])["report_max_bytes"], "report-byte-limit")
    return value


def require_eligible(value, *, expected_source, expected_profile):
    """Check report eligibility, not evidence authenticity or 1.0 acceptance.

    The caller must supply independently verified candidate/profile identity;
    never copy these expected values from an untrusted report.
    """
    validate_report(value)
    _source(expected_source)
    require(
        value["source"] == expected_source and value["profile"] == expected_profile,
        "candidate-mismatch",
    )
    limits = profile(expected_profile)
    data = catalogue()
    require(
        value["status"] == "passed" and value["failure_codes"] == [] and value["cold_ray"],
        "incomplete-run",
    )
    require(
        value["elapsed"]["setup"] <= limits["setup_timeout_seconds"]
        and value["elapsed"]["cleanup"] <= limits["cleanup_timeout_seconds"],
        "phase-deadline",
    )
    require(value["elapsed"]["workload"] == limits["duration_seconds"], "workload-duration")
    require(
        value["admission"]["verified"]
        and value["admission"]["limits"] == limits["admission_limits"],
        "unverified-admission",
    )
    tasks = value["tasks"]
    require(
        len(tasks) == value["submitted_count"] == limits["task_count"], "incomplete-task-ledger"
    )
    require(
        {task["id"] for task in tasks} == set(range(1, len(tasks) + 1)), "incomplete-task-ledger"
    )
    counts = Counter((task["kind"], task["runner"]) for task in tasks)
    for kind, definition in data["workloads"].items():
        for runner in ("core", "job"):
            require(
                counts[kind, runner] == limits["task_count"] * definition["weight"] // 20,
                "workload-mix",
            )
    ordered = sorted(tasks, key=lambda task: task["submitted_seconds"])
    for index, task in enumerate(ordered):
        require(
            task["state"] in TERMINAL_STATES
            and task["terminal_seconds"] is not None
            and task["outcome_verified"]
            and task["remote_quiescent"]
            and not task["uncertain"],
            "unexplained-task",
        )
        require(
            task["state"] in limits["expected_terminal_states"][task["kind"]],
            "unexpected-task-outcome",
        )
        require(
            task["submitted_seconds"] < limits["submission_window_seconds"]
            and task["terminal_seconds"]
            <= limits["submission_window_seconds"] + limits["settlement_seconds"],
            "task-deadline",
        )
        history = task["attempt_history"]
        require(
            task["attempts"] >= (2 if task["kind"] == "retry" else 1)
            and len(history) == task["attempts"],
            "incomplete-attempt-history",
        )
        require(
            [attempt["number"] for attempt in history] == list(range(1, task["attempts"] + 1))
            and history[-1]["state"] == task["state"],
            "incomplete-attempt-history",
        )
        for attempt in history:
            require(
                attempt["state"] in TERMINAL_STATES
                and (attempt["started"] or attempt["state"] in {"EXPIRED", "CANCELLED"}),
                "incomplete-attempt-history",
            )
        require(
            all(
                later["generation"] > earlier["generation"]
                for earlier, later in zip(history, history[1:], strict=False)
            ),
            "attempt-generation",
        )
        if index:
            require(
                task["submitted_seconds"] - ordered[index - 1]["submitted_seconds"]
                >= limits["submission_interval_seconds"],
                "submission-rate",
            )
    expected_faults = {
        (kind, repeat)
        for kind in data["faults"]
        for repeat in range(1, limits["fault_repetitions"] + 1)
    }
    require(
        {(fault["kind"], fault["repetition"]) for fault in value["faults"]} == expected_faults,
        "missing-fault",
    )
    for fault in value["faults"]:
        require(
            fault["injected_seconds"] is not None and fault["recovered_seconds"] is not None,
            "incomplete-fault",
        )
        require(
            fault["outcome_verified"] and fault["remote_quiescent"] and not fault["uncertain"],
            "unexplained-fault",
        )
        require(
            fault["started_seconds"] >= limits["warmup_seconds"]
            and fault["recovered_seconds"] <= limits["submission_window_seconds"],
            "fault-window",
        )
        require(
            fault["injected_seconds"] - fault["started_seconds"] <= limits["fault_timeout_seconds"]
            and fault["recovered_seconds"] - fault["injected_seconds"]
            <= limits["recovery_timeout_seconds"],
            "fault-deadline",
        )
    timestamps = list(range(0, limits["duration_seconds"] + 1, limits["sample_interval_seconds"]))
    start = limits["duration_seconds"] - limits["stable_window_seconds"]
    for kind, samples in value["resources"].items():
        require(
            [sample["at_seconds"] for sample in samples] == timestamps, "missing-resource-sample"
        )
        ceiling = limits["resources"][kind]
        require(max(sample["value"] for sample in samples) <= ceiling["maximum"], "resource-limit")
        earlier = max(
            sample["value"]
            for sample in samples
            if limits["warmup_seconds"] - limits["baseline_window_seconds"]
            <= sample["at_seconds"]
            <= limits["warmup_seconds"]
        )
        later = max(sample["value"] for sample in samples if sample["at_seconds"] >= start)
        require(later - earlier <= ceiling["growth"], "resource-growth")
        if kind == "active_tasks":
            require(later == 0, "unsettled-tasks")
        if kind in {"unhealthy_scrape_targets", "prometheus_series"}:
            for sample in samples:
                recovering = any(
                    fault["started_seconds"] <= sample["at_seconds"] <= fault["recovered_seconds"]
                    for fault in value["faults"]
                )
                if not recovering:
                    require(
                        sample["value"] == 0
                        if kind == "unhealthy_scrape_targets"
                        else sample["value"] > 0,
                        "unhealthy-monitoring",
                    )
    retained = value["retention"]
    require(
        retained["runs"] >= limits["duration_seconds"] // limits["retention_interval_seconds"]
        and retained["failed_runs"] == 0
        and retained["deleted_input_objects"] > 0
        and retained["deleted_workflow_details"] > 0
        and retained["retained_task_summaries"] == len(tasks)
        and retained["retained_attempt_summaries"] == sum(task["attempts"] for task in tasks)
        and retained["remaining_eligible_objects"] == 0
        and retained["reclaimable_bytes"] <= limits["reclaimable_bytes"]
        and retained["terminal_outcomes_preserved"],
        "incomplete-retention",
    )
    cleanup = value["cleanup"]
    require(
        cleanup["verified"]
        and not any(cleanup["remaining"].values())
        and cleanup["retained_payload_bytes"] == 0,
        "incomplete-cleanup",
    )
    http = value["http"]
    require(http["supported_core_disposition"] == "no_unexpected_outcomes", "unresolved-http")
    require(
        {row["repetition"] for row in http["observations"]}
        == set(range(1, limits["fault_repetitions"] + 1)),
        "missing-http-observation",
    )
    for row in http["observations"]:
        require(
            all(row[key] == expected for key, expected in limits["expected_http"].items()),
            "unexpected-http-outcome",
        )
        require(
            row["expected"]
            and all(
                row[key] is not None
                for key in (
                    "configured_timeout_seconds",
                    "attempt_timeout_seconds",
                    "outer_remaining_seconds",
                    "observer_remaining_seconds",
                )
            ),
            "unexpected-or-incomplete-http",
        )
        require(
            0 < row["attempt_timeout_seconds"] <= row["configured_timeout_seconds"],
            "http-timeout-budget",
        )
    return value
