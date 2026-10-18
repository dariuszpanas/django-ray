"""Offline measurement validation never invents execution or savings evidence."""

import copy
import json

import pytest

from scripts import test_planning_experiment as experiment


def identities():
    return {
        "source": {
            "base_commit": "a" * 40,
            "head_commit": "b" * 40,
            "comparison_commit": "a" * 40,
            "execution_commit": "b" * 40,
            "execution_tree": "c" * 40,
        },
        "adapter_source": {"execution_commit": "d" * 40, "execution_tree": "e" * 40},
        **dict.fromkeys(
            (
                "policy_sha256",
                "bindings_sha256",
                "plan_digest",
                "results_sha256",
                "evaluation_sha256",
            ),
            "f" * 64,
        ),
        "run_id": "run-123",
        "attempt": 1,
    }


def measurements():
    return {
        "experiment_id": "discovery-01",
        "arm": "proposed",
        "fingerprints": dict.fromkeys(
            ("environment_sha256", "images_sha256", "dependencies_sha256")
        ),
        "cache_state": "unknown",
        "timing": dict.fromkeys(experiment.TIMES),
        "cells": [
            {
                "suite": "qualification.application",
                "variant": "default",
                "outcome": "passed",
                "evidence_sha256": "a" * 64,
                "attempts": [
                    {
                        "job_id": "job-123",
                        "run_id": "run-123",
                        "attempt": 1,
                        "retry_ordinal": 1,
                        "outcome": "passed",
                        "runner_seconds": None,
                    }
                ],
            }
        ],
        "artifacts": [{"name": "application", "sha256": "b" * 64}],
        "incomplete_reason": None,
    }


def test_unknown_measurements_stay_unknown_and_never_imply_comparison():
    report = experiment.build_observation(identities(), measurements())
    assert report["test-experiment-observation-version"] == 1
    assert report["activation"] == "disabled"
    assert report["authority"] == "advisory-measurement-only"
    assert report["comparison"] is None
    assert all(value is None for value in report["metrics"].values())
    assert json.loads(experiment.encode_observation(report)) == report


def test_actual_intervals_and_all_retry_costs_are_retained():
    data = measurements()
    data["timing"].update(
        dispatched_at="2026-10-17T01:00:01Z",
        workflow_created_at="2026-10-17T01:00:03Z",
        feedback_at="2026-10-17T01:00:13.250000Z",
        candidate_ready_at="2026-10-17T01:00:00Z",
        required_ci_ready_at="2026-10-17T01:00:20Z",
        first_failure_at="2026-10-17T01:00:05Z",
    )
    data["cells"][0]["attempts"] = [
        {
            "job_id": "job-old",
            "run_id": "run-123",
            "attempt": 1,
            "retry_ordinal": 1,
            "outcome": "failed",
            "runner_seconds": 3.25,
        },
        {
            "job_id": "job-new",
            "run_id": "run-123",
            "attempt": 1,
            "retry_ordinal": 2,
            "outcome": "passed",
            "runner_seconds": 5,
        },
    ]
    report = experiment.build_observation(identities(), data)
    assert report["metrics"] == {
        "dispatch_to_creation_seconds": 2,
        "advisory_feedback_seconds": 10.25,
        "developer_feedback_seconds": 13.25,
        "required_ci_readiness_seconds": 20,
        "first_failure_seconds": 2,
        "runner_job_seconds": 8.25,
        "billing_seconds": None,
    }
    assert len(report["cells"][0]["attempts"]) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("run_id", "other-run"), ("attempt", 2), ("attempt", True), ("attempt", "1")],
)
def test_job_execution_identity_must_match_the_observed_run_attempt(field, value):
    data = measurements()
    data["cells"][0]["attempts"][0][field] = value
    with pytest.raises(experiment.ObservationError):
        experiment.build_observation(identities(), data)


@pytest.mark.parametrize("field", ["run_id", "attempt"])
def test_job_execution_identity_cannot_be_inferred_from_an_aggregate(field):
    data = measurements()
    del data["cells"][0]["attempts"][0][field]
    with pytest.raises(experiment.ObservationError, match="fields"):
        experiment.build_observation(identities(), data)


def test_same_job_cannot_be_recounted_under_a_different_retry_ordinal():
    data = measurements()
    repeated = copy.deepcopy(data["cells"][0]["attempts"][0])
    repeated["retry_ordinal"] = 2
    data["cells"][0]["attempts"].append(repeated)
    with pytest.raises(experiment.ObservationError, match="Duplicate observation job"):
        experiment.build_observation(identities(), data)


def test_success_carried_from_an_older_attempt_requires_a_separate_observation():
    first_context, first_data = identities(), measurements()
    first_data["cells"][0]["suite"] = "qualification.latency"
    first = experiment.build_observation(first_context, first_data)

    second_context, second_data = identities(), measurements()
    second_context.update(attempt=2, plan_digest="1" * 64)
    second_data["cells"][0]["attempts"][0].update(job_id="job-456", attempt=2)
    second = experiment.build_observation(second_context, second_data)
    assert first["identities"]["attempt"] == 1
    assert second["identities"]["attempt"] == 2
    assert first["metrics"]["runner_job_seconds"] is None
    assert second["metrics"]["runner_job_seconds"] is None
    assert first["comparison"] is second["comparison"] is None

    second_data["cells"].extend(first_data["cells"])
    with pytest.raises(experiment.ObservationError, match="exact execution run and attempt"):
        experiment.build_observation(second_context, second_data)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 10**1000, "1", 2_678_401])
def test_bad_duration_is_refused(value):
    data = measurements()
    data["cells"][0]["attempts"][0]["runner_seconds"] = value
    with pytest.raises(experiment.ObservationError):
        experiment.build_observation(identities(), data)


@pytest.mark.parametrize("value", ["2026-02-30T00:00:00Z", "2026-10-17T00:00:00+00:00", 1, True])
def test_bad_timestamp_is_refused(value):
    data = measurements()
    data["timing"]["feedback_at"] = value
    with pytest.raises(experiment.ObservationError):
        experiment.build_observation(identities(), data)


def test_reversed_intervals_and_failure_without_failure_evidence_are_refused():
    data = measurements()
    data["timing"].update(
        workflow_created_at="2026-10-17T00:00:10Z", feedback_at="2026-10-17T00:00:00Z"
    )
    with pytest.raises(experiment.ObservationError):
        experiment.build_observation(identities(), data)
    data["timing"].update(
        feedback_at="2026-10-17T00:00:20Z", first_failure_at="2026-10-17T00:00:15Z"
    )
    with pytest.raises(experiment.ObservationError, match="retained failure"):
        experiment.build_observation(identities(), data)


@pytest.mark.parametrize("outcome", ["missing", "pending", "blocked", "cancelled"])
def test_incomplete_observations_need_reason_and_preserve_outcome(outcome):
    data = measurements()
    cell = data["cells"][0]
    cell["outcome"] = outcome
    if outcome == "missing":
        cell["attempts"] = []
    else:
        cell["attempts"][0]["outcome"] = outcome
    with pytest.raises(experiment.ObservationError, match="explicit reason"):
        experiment.build_observation(identities(), data)
    data["incomplete_reason"] = "Evidence collection was interrupted."
    report = experiment.build_observation(identities(), data)
    assert report["cells"][0]["outcome"] == outcome


@pytest.mark.parametrize(
    "change",
    ["duplicate-cell", "duplicate-job", "no-evidence", "greenest", "retry-order", "no-attempt"],
)
def test_cell_corruption_is_refused(change):
    data = measurements()
    cell = data["cells"][0]
    if change == "duplicate-cell":
        data["cells"].append(copy.deepcopy(cell))
    elif change == "duplicate-job":
        other = copy.deepcopy(cell)
        other["suite"] = "qualification.latency"
        data["cells"].append(other)
    elif change == "no-evidence":
        cell["evidence_sha256"] = None
    elif change == "greenest":
        cell["attempts"].append(
            {
                "job_id": "job-124",
                "run_id": "run-123",
                "attempt": 1,
                "retry_ordinal": 2,
                "outcome": "failed",
                "runner_seconds": 1,
            }
        )
    elif change == "retry-order":
        cell["attempts"].append(
            {
                "job_id": "job-124",
                "run_id": "run-123",
                "attempt": 1,
                "retry_ordinal": 1,
                "outcome": "passed",
                "runner_seconds": 1,
            }
        )
    else:
        cell["attempts"] = []
    with pytest.raises(experiment.ObservationError):
        experiment.build_observation(identities(), data)


@pytest.mark.parametrize(
    "change", ["attempt", "digest", "source-width", "head", "partial", "unknown", "run"]
)
def test_identity_corruption_is_refused(change):
    context = identities()
    if change == "attempt":
        context["attempt"] = True
    elif change == "digest":
        context["policy_sha256"] = "A" * 64
    elif change == "source-width":
        context["source"]["execution_tree"] = "c" * 64
    elif change == "head":
        context["source"]["head_commit"] = "d" * 40
    elif change == "partial":
        context["source"]["base_commit"] = None
    elif change == "unknown":
        context["command"] = "execute"
    else:
        context["run_id"] = "unsafe\nrun"
    with pytest.raises(experiment.ObservationError):
        experiment.build_observation(context, measurements())


def test_document_bound_includes_newline_and_does_not_alias_inputs(monkeypatch):
    context, data = identities(), measurements()
    report = experiment.build_observation(context, data)
    encoded = experiment.encode_observation(report)
    context["source"]["execution_commit"] = "d" * 40
    data["cells"][0]["attempts"][0]["outcome"] = "failed"
    assert experiment.encode_observation(report) == encoded
    monkeypatch.setattr(experiment, "MAX_DOCUMENT_BYTES", len(encoded))
    assert experiment.encode_observation(report) == encoded
    monkeypatch.setattr(experiment, "MAX_DOCUMENT_BYTES", len(encoded) - 1)
    with pytest.raises(experiment.ObservationError):
        experiment.encode_observation(report)


def test_unknown_duration_and_partial_timestamps_are_not_zero():
    data = measurements()
    data["timing"]["feedback_at"] = "2026-10-17T00:00:00Z"
    report = experiment.build_observation(identities(), data)
    assert report["metrics"]["advisory_feedback_seconds"] is None
    assert report["metrics"]["developer_feedback_seconds"] is None
    assert report["metrics"]["runner_job_seconds"] is None


@pytest.mark.parametrize("outcome", ["pending", "blocked"])
def test_partial_attempt_duration_does_not_become_a_complete_resource_total(outcome):
    data = measurements()
    data["cells"][0]["outcome"] = outcome
    data["cells"][0]["attempts"][0].update(outcome=outcome, runner_seconds=5)
    data["incomplete_reason"] = "Attempt has not finished."
    report = experiment.build_observation(identities(), data)
    assert report["cells"][0]["attempts"][0]["runner_seconds"] == 5
    assert report["metrics"]["runner_job_seconds"] is None
