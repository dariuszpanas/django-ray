"""Synthetic contract cases only; these tests do not execute or certify a soak."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from qualification.soak import contract


def report(name="development"):
    limits = contract.profile(name)
    catalogue = contract.catalogue()
    cycle = [kind for kind, spec in catalogue["workloads"].items() for _ in range(spec["weight"])]
    tasks: list[dict[str, Any]] = []
    for index in range(limits["task_count"]):
        kind = cycle[index % len(cycle)]
        tasks.append(
            {
                "id": index + 1,
                "kind": kind,
                "runner": "core" if index // 10 % 2 == 0 else "job",
                "submitted_seconds": index * 6,
                "terminal_seconds": index * 6 + 1,
                "state": {"cancel": "CANCELLED", "expire": "EXPIRED"}.get(kind, "SUCCEEDED"),
                "attempts": 2 if kind == "retry" else 1,
                "reconciliations": 0,
                "ownership_transfers": 0,
                "attempt_history": [
                    {"number": 1, "state": "FAILED", "started": True, "generation": 0},
                    {"number": 2, "state": "SUCCEEDED", "started": True, "generation": 1},
                ]
                if kind == "retry"
                else [
                    {
                        "number": 1,
                        "state": {"cancel": "CANCELLED", "expire": "EXPIRED"}.get(
                            kind, "SUCCEEDED"
                        ),
                        "started": kind != "expire",
                        "generation": 0,
                    }
                ],
                "outcome_verified": True,
                "remote_quiescent": True,
                "uncertain": False,
            }
        )
    faults = []
    for repeat in range(1, limits["fault_repetitions"] + 1):
        for kind in catalogue["faults"]:
            start = limits["warmup_seconds"] + len(faults) * 130
            faults.append(
                {
                    "kind": kind,
                    "repetition": repeat,
                    "started_seconds": start,
                    "injected_seconds": start + 1,
                    "recovered_seconds": start + 2,
                    "outcome_verified": True,
                    "remote_quiescent": True,
                    "uncertain": False,
                }
            )
    return {
        "schema_version": 1,
        "profile": name,
        "profile_sha256": contract.profile_digest(name),
        "source": {
            "platform": "linux",
            "commit": "a" * 40,
            "tree": "b" * 40,
            "archive_sha256": "c" * 64,
            "wheel_sha256": "d" * 64,
            "images": dict.fromkeys(contract.IMAGE_FIELDS, "e" * 64),
            "versions": dict.fromkeys(contract.VERSION_FIELDS, "1.2.3"),
        },
        "status": "passed",
        "failure_codes": [],
        "cold_ray": True,
        "elapsed": {"setup": 10, "workload": limits["duration_seconds"], "cleanup": 10},
        "submitted_count": len(tasks),
        "admission": {"verified": True, "limits": limits["admission_limits"]},
        "tasks": tasks,
        "faults": faults,
        "resources": {
            kind: [
                {
                    "at_seconds": at,
                    "value": (
                        0
                        if at >= limits["duration_seconds"] - limits["stable_window_seconds"]
                        else 1
                    )
                    if kind == "active_tasks"
                    else 0
                    if kind == "unhealthy_scrape_targets"
                    else 10,
                }
                for at in range(0, limits["duration_seconds"] + 1, 15)
            ]
            for kind in limits["resources"]
        },
        "retention": {
            "runs": limits["duration_seconds"] // 60,
            "failed_runs": 0,
            "deleted_input_objects": 10,
            "deleted_workflow_details": 10,
            "retained_task_summaries": len(tasks),
            "retained_attempt_summaries": sum(task["attempts"] for task in tasks),
            "remaining_eligible_objects": 0,
            "reclaimable_bytes": 0,
            "terminal_outcomes_preserved": True,
        },
        "cleanup": {
            "verified": True,
            "remaining": dict.fromkeys(contract.RESIDUE, 0),
            "retained_payload_bytes": 0,
        },
        "http": {
            "historical_attribution": "unresolved",
            "supported_core_disposition": "no_unexpected_outcomes",
            "observations": [
                {
                    "repetition": repeat,
                    "observer": "application_api",
                    "request_substage": "task_status",
                    "phase": "complete",
                    "status": 200,
                    "transport": "none",
                    "configured_timeout_seconds": 10,
                    "attempt_timeout_seconds": 10,
                    "retry_policy": "none",
                    "outer_remaining_seconds": 100,
                    "observer_remaining_seconds": 60,
                    "expected": True,
                }
                for repeat in range(1, limits["fault_repetitions"] + 1)
            ],
        },
    }


@pytest.fixture
def evidence():
    return report()


def eligible(value, expected=None):
    return contract.require_eligible(
        value, expected_source=expected or value["source"], expected_profile=value["profile"]
    )


@pytest.mark.parametrize("name", ["development", "release"])
def test_complete_synthetic_contract_is_parseable_and_eligible(name):
    value = report(name)
    expected = copy.deepcopy(value["source"])
    parsed = contract.parse_report(json.dumps(value).encode())
    assert eligible(parsed, expected) == value


def test_catalogue_is_proposed_bounded_and_points_to_existing_assertion_owners():
    data = contract.catalogue()
    root = Path(__file__).resolve().parents[2]
    assert data["schema_version"] == 1 and data["status"] == "proposed"
    assert sum(spec["weight"] for spec in data["workloads"].values()) == 10
    for owner in [
        *(spec["owner"] for spec in data["workloads"].values()),
        *data["faults"].values(),
    ]:
        assert (root / owner).is_file(), owner
    for name in data["profiles"]:
        limits = contract.profile(name)
        assert limits["task_count"] % 20 == 0
        assert (
            limits["task_count"] * limits["submission_interval_seconds"]
            <= limits["submission_window_seconds"]
        )
        assert (
            limits["warmup_seconds"]
            + limits["stable_window_seconds"]
            + len(data["faults"])
            * limits["fault_repetitions"]
            * (limits["fault_timeout_seconds"] + limits["recovery_timeout_seconds"])
            <= limits["duration_seconds"]
        )
        assert limits["stable_window_seconds"] % (2 * limits["sample_interval_seconds"]) == 0
        assert limits["input_retention_days"] == limits["workflow_detail_retention_days"] == 0
    assert contract.profile_digest("development") != contract.profile_digest("release")
    copy_of_profile = contract.profile("development")
    copy_of_profile["task_count"] = 0
    assert contract.profile("development")["task_count"] == 400


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_incomplete_failed_observations_remain_retainable(evidence, status):
    evidence.update(status=status, failure_codes=["task"], tasks=[], faults=[])
    evidence["resources"] = dict.fromkeys(evidence["resources"], [])
    evidence["cleanup"]["verified"] = False
    evidence["http"].update(supported_core_disposition="pending", observations=[])
    assert contract.parse_report(json.dumps(evidence).encode()) == evidence
    with pytest.raises(contract.ContractError, match="incomplete-run"):
        eligible(evidence)


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda v: v["tasks"].pop(), "incomplete-task-ledger"),
        (lambda v: v["tasks"][0].update(state="RUNNING"), "unexplained-task"),
        (lambda v: v["tasks"][0].update(terminal_seconds=None), "unexplained-task"),
        (lambda v: v["tasks"][0].update(uncertain=True), "unexplained-task"),
        (lambda v: v["tasks"][0].update(remote_quiescent=False), "unexplained-task"),
        (lambda v: v["tasks"][0].update(outcome_verified=False), "unexplained-task"),
        (lambda v: v["tasks"][0].update(kind="long"), "workload-mix"),
        (lambda v: v["tasks"][1].update(submitted_seconds=1), "submission-rate"),
        (lambda v: v["tasks"][-1].update(terminal_seconds=3601), "task-deadline"),
        (lambda v: v["admission"].update(verified=False), "unverified-admission"),
        (lambda v: v.update(cold_ray=False), "incomplete-run"),
        (lambda v: v["elapsed"].update(setup=601), "phase-deadline"),
        (lambda v: v["elapsed"].update(cleanup=181), "phase-deadline"),
        (lambda v: v["elapsed"].update(workload=3599), "workload-duration"),
        (lambda v: v["faults"].pop(), "missing-fault"),
        (lambda v: v["faults"][0].update(uncertain=True), "unexplained-fault"),
        (lambda v: v["faults"][0].update(started_seconds=299), "fault-window"),
        (lambda v: v["faults"][-1].update(recovered_seconds=2401), "fault-window"),
        (
            lambda v: v["faults"][-1].update(injected_seconds=2132, recovered_seconds=2133),
            "fault-deadline",
        ),
        (lambda v: v["faults"][-1].update(recovered_seconds=2242), "fault-deadline"),
        (lambda v: v["retention"].update(failed_runs=1), "incomplete-retention"),
        (lambda v: v["retention"].update(runs=1), "incomplete-retention"),
        (lambda v: v["retention"].update(deleted_input_objects=0), "incomplete-retention"),
        (lambda v: v["retention"].update(deleted_workflow_details=0), "incomplete-retention"),
        (lambda v: v["retention"].update(retained_task_summaries=0), "incomplete-retention"),
        (lambda v: v["retention"].update(remaining_eligible_objects=1), "incomplete-retention"),
        (lambda v: v["retention"].update(reclaimable_bytes=67108865), "incomplete-retention"),
        (
            lambda v: v["retention"].update(terminal_outcomes_preserved=False),
            "incomplete-retention",
        ),
        (lambda v: v["cleanup"].update(verified=False), "incomplete-cleanup"),
        (lambda v: v["cleanup"].update(retained_payload_bytes=1), "incomplete-cleanup"),
        (
            lambda v: v["http"].update(supported_core_disposition="unresolved_defect"),
            "unresolved-http",
        ),
        (lambda v: v["http"].update(observations=[]), "missing-http-observation"),
        (
            lambda v: v["http"]["observations"][0].update(expected=False),
            "unexpected-or-incomplete-http",
        ),
        (
            lambda v: v["http"]["observations"][0].update(outer_remaining_seconds=None),
            "unexpected-or-incomplete-http",
        ),
    ],
)
def test_partial_observation_never_becomes_eligible(evidence, change, code):
    change(evidence)
    assert contract.validate_report(evidence) is evidence
    with pytest.raises(contract.ContractError, match=code):
        eligible(evidence)


@pytest.mark.parametrize("category", sorted(contract.RESIDUE))
def test_any_owned_residue_blocks_eligibility(evidence, category):
    evidence["cleanup"]["remaining"][category] = 1
    with pytest.raises(contract.ContractError, match="incomplete-cleanup"):
        eligible(evidence)


@pytest.mark.parametrize("category", sorted(contract.profile("development")["resources"]))
@pytest.mark.parametrize(
    "defect,code",
    [
        ("missing", "missing-resource-sample"),
        ("peak", "resource-limit"),
        ("growth", "resource-growth"),
    ],
)
def test_every_resource_category_requires_complete_bounded_stable_measurements(
    evidence, category, defect, code
):
    samples = evidence["resources"][category]
    limits = contract.profile("development")["resources"][category]
    if defect == "missing":
        samples.pop()
    else:
        samples[-1]["value"] = (
            limits["maximum"] + 1
            if defect == "peak"
            else (
                1
                if category == "active_tasks"
                else 0
                if category == "unhealthy_scrape_targets"
                else 10
            )
            + limits["growth"]
            + 1
        )
    assert contract.validate_report(evidence) is evidence
    with pytest.raises(contract.ContractError, match=code):
        eligible(evidence)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(schema_version=True),
        lambda v: v.update(profile="unregistered"),
        lambda v: v.update(profile_sha256="0" * 64),
        lambda v: v["source"].update(platform="windows"),
        lambda v: v["source"]["versions"].update(ray="secret value\n"),
        lambda v: v["source"]["images"].update(ray="not-a-digest"),
        lambda v: v["tasks"][0].update(id=True),
        lambda v: v["tasks"][1].update(id=1),
        lambda v: v["tasks"][0].update(kind=[]),
        lambda v: v["tasks"][0].update(payload="never-retain"),
        lambda v: v["tasks"][0].update(outcome_verified=1),
        lambda v: v["tasks"][0].update(submitted_seconds=float("nan")),
        lambda v: v["tasks"][0].update(submitted_seconds=float("inf")),
        lambda v: v["tasks"][0].update(attempts=10**1000),
        lambda v: v["faults"][1].update(kind=v["faults"][0]["kind"]),
        lambda v: v["faults"][1].update(started_seconds=0),
        lambda v: v["resources"]["memory_bytes"][1].update(at_seconds=0),
        lambda v: v["resources"]["memory_bytes"][0].update(value=True),
        lambda v: v["cleanup"]["remaining"].pop("leases"),
        lambda v: v["http"]["observations"][0].update(response_body="never-retain"),
        lambda v: v["http"].update(historical_attribution="repaired"),
    ],
)
def test_malformed_or_unbounded_fields_are_refused(evidence, change):
    change(evidence)
    with pytest.raises(contract.ContractError):
        contract.validate_report(evidence)


def test_independently_supplied_candidate_and_profile_are_required(evidence):
    expected = copy.deepcopy(evidence["source"])
    evidence["source"]["tree"] = "f" * 40
    with pytest.raises(contract.ContractError, match="candidate-mismatch"):
        eligible(evidence, expected)
    with pytest.raises(contract.ContractError, match="candidate-mismatch"):
        contract.require_eligible(
            evidence, expected_source=evidence["source"], expected_profile="release"
        )


def test_json_byte_bounds_duplicate_keys_and_parse_errors(evidence, monkeypatch):
    for raw in (b'{"schema_version":1,"schema_version":1}', b"{", b"\xff"):
        with pytest.raises(contract.ContractError, match="invalid-json"):
            contract.parse_report(raw)
    monkeypatch.setattr(contract, "MAX_REPORT_BYTES", 10)
    with pytest.raises(contract.ContractError, match="report-byte-limit"):
        contract.parse_report(json.dumps(evidence).encode())


def test_profile_specific_serialized_byte_limit(evidence):
    raw = json.dumps(evidence).encode()
    padded = raw + b" " * (contract.profile("development")["report_max_bytes"] - len(raw) + 1)
    with pytest.raises(contract.ContractError, match="report-byte-limit"):
        contract.parse_report(padded)


@pytest.mark.parametrize(
    "change,code",
    [
        (
            lambda v: v["tasks"][0].update(attempts=0, attempt_history=[]),
            "incomplete-attempt-history",
        ),
        (lambda v: v["tasks"][0]["attempt_history"].clear(), "incomplete-attempt-history"),
        (
            lambda v: v["tasks"][0]["attempt_history"][0].update(number=2),
            "incomplete-attempt-history",
        ),
        (
            lambda v: v["tasks"][0]["attempt_history"][0].update(started=False),
            "incomplete-attempt-history",
        ),
        (lambda v: v["retention"].update(retained_attempt_summaries=0), "incomplete-retention"),
        (lambda v: v["admission"]["limits"].update(pids=0), "unverified-admission"),
        (lambda v: v["tasks"][-1].update(terminal_seconds=3001), "task-deadline"),
        (
            lambda v: v["http"]["observations"][0].update(
                phase="response_headers_failed", transport="timeout", status=0, expected=True
            ),
            "unexpected-http-outcome",
        ),
        (
            lambda v: v["http"]["observations"][0].update(attempt_timeout_seconds=0),
            "http-timeout-budget",
        ),
        (
            lambda v: v["http"]["observations"][0].update(attempt_timeout_seconds=11),
            "http-timeout-budget",
        ),
        (lambda v: v["resources"]["active_tasks"][-1].update(value=1), "unsettled-tasks"),
        (
            lambda v: v["resources"]["unhealthy_scrape_targets"][0].update(value=1),
            "unhealthy-monitoring",
        ),
        (lambda v: v["resources"]["prometheus_series"][0].update(value=0), "unhealthy-monitoring"),
    ],
)
def test_reviewed_false_eligibility_regressions(evidence, change, code):
    change(evidence)
    assert contract.validate_report(evidence) is evidence
    with pytest.raises(contract.ContractError, match=code):
        eligible(evidence)


@pytest.mark.parametrize("field", ["injected_seconds", "recovered_seconds"])
def test_missing_fault_milestones_are_retained_without_inventing_success(evidence, field):
    evidence["faults"][-1][field] = None
    evidence["faults"][-1]["recovered_seconds"] = None
    assert contract.validate_report(evidence) is evidence
    with pytest.raises(contract.ContractError, match="incomplete-fault"):
        eligible(evidence)
    evidence.update(status="failed", failure_codes=["fault"])
    assert contract.parse_report(json.dumps(evidence).encode()) == evidence


@pytest.mark.parametrize(
    "field", ["observer", "request_substage", "phase", "transport", "retry_policy"]
)
def test_http_categories_reject_non_strings_with_fixed_diagnostics(evidence, field):
    evidence["http"]["observations"][0][field] = []
    with pytest.raises(contract.ContractError):
        contract.validate_report(evidence)


def test_expected_transient_monitoring_loss_is_bounded_by_fault_recovery(evidence):
    samples = evidence["resources"]["unhealthy_scrape_targets"]
    # The first declared fault starts at 300 and recovers at 302 seconds.
    samples[20]["value"] = 1
    assert eligible(evidence) is evidence
    samples[21]["value"] = 1
    with pytest.raises(contract.ContractError, match="unhealthy-monitoring"):
        eligible(evidence)


def test_unstarted_cancelled_attempt_is_numbered_and_retained(evidence):
    task = next(task for task in evidence["tasks"] if task["kind"] == "cancel")
    task["attempt_history"][0]["started"] = False
    assert eligible(evidence) is evidence


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda task: task["attempt_history"][1].update(generation=0), "attempt-generation"),
        (
            lambda task: task.update(attempts=1, attempt_history=task["attempt_history"][:1]),
            "incomplete-attempt-history",
        ),
        (lambda task: task["attempt_history"][1].update(number=1), "incomplete-attempt-history"),
        (
            lambda task: task["attempt_history"][1].update(state="FAILED"),
            "incomplete-attempt-history",
        ),
    ],
)
def test_retry_history_cannot_erase_or_duplicate_an_attempt(evidence, change, code):
    task = next(task for task in evidence["tasks"] if task["kind"] == "retry")
    change(task)
    with pytest.raises(contract.ContractError, match=code):
        eligible(evidence)


def test_unobserved_admission_remains_null_in_partial_report(evidence):
    evidence["admission"].update(verified=False, limits=None)
    assert contract.validate_report(evidence) is evidence
    with pytest.raises(contract.ContractError, match="unverified-admission"):
        eligible(evidence)


@pytest.mark.parametrize("state", ["FAILED", "LOST", "CANCELLED", "EXPIRED"])
def test_report_booleans_cannot_authorize_unexpected_terminal_outcomes(evidence, state):
    for task in evidence["tasks"]:
        if task["kind"] in {"short", "long", "retry", "workflow", "runtime_env"}:
            task["state"] = state
            task["attempt_history"][-1]["state"] = state
    assert contract.validate_report(evidence) is evidence
    with pytest.raises(contract.ContractError, match="unexpected-task-outcome"):
        eligible(evidence)
    evidence.update(status="failed", failure_codes=["task"])
    assert contract.parse_report(json.dumps(evidence).encode()) == evidence
