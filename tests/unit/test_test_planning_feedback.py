"""One shared full run measures publication lead, never selective-run savings."""

import copy
import hashlib
import json

import pytest

from scripts import test_planning_experiment as observation
from scripts import test_planning_feedback as feedback


def _keys(keys):
    return [{"suite": suite, "variant": variant} for suite, variant in sorted(keys)]


def _case(*, identical=False, empty=False):
    application = ("qualification.application", "default")
    latency = ("qualification.latency", "default")
    selections = {
        "current": set() if empty else {application, latency},
        "proposed": set() if empty else {latency},
        "full": feedback.CATALOG,
    }
    if identical:
        selections = dict.fromkeys(feedback.ARMS, feedback.CATALOG)
    expected = {
        "execution_commit": "a" * 40,
        "execution_tree": "b" * 40,
        "adapter_source": {"execution_commit": "a" * 40, "execution_tree": "b" * 40},
        "run_id": "run-123",
        "attempt": 1,
        "bindings_sha256": "c" * 64,
        "arms": {
            name: {
                "policy_sha256": "d" * 64 if name == "current" else "e" * 64,
                "plan_digest": str(index + 1) * 64,
                "selected": _keys(selections[name]),
            }
            for index, name in enumerate(feedback.ARMS)
        },
    }
    cells = {
        key: {
            "suite": key[0],
            "variant": key[1],
            "outcome": "passed",
            "evidence_sha256": str(index + 1) * 64,
            "attempts": [
                {
                    "job_id": f"job-{index}",
                    "run_id": "run-123",
                    "attempt": 1,
                    "retry_ordinal": 1,
                    "outcome": "passed",
                    "runner_seconds": None,
                }
            ],
        }
        for index, key in enumerate(sorted(feedback.CATALOG))
    }
    publications = {}
    for index, name in enumerate(feedback.ARMS):
        identity = {
            "source": {
                "base_commit": None,
                "head_commit": None,
                "comparison_commit": None,
                "execution_commit": expected["execution_commit"],
                "execution_tree": expected["execution_tree"],
            },
            **{
                key: expected[key]
                for key in ("adapter_source", "run_id", "attempt", "bindings_sha256")
            },
            **{key: expected["arms"][name][key] for key in ("policy_sha256", "plan_digest")},
            "results_sha256": "f" * 64,
            "evaluation_sha256": "a" * 64,
        }
        measurements = {
            "experiment_id": "shared-run-123",
            "arm": name,
            "fingerprints": dict.fromkeys(
                ("environment_sha256", "images_sha256", "dependencies_sha256")
            ),
            "cache_state": "unknown",
            "timing": {
                **dict.fromkeys(observation.TIMES),
                "workflow_created_at": "2026-10-18T00:00:00Z",
            },
            "cells": [copy.deepcopy(cells[key]) for key in sorted(selections[name])],
            "artifacts": [],
            "incomplete_reason": None,
        }
        built = observation.build_observation(identity, measurements)
        published_index = 2 if identical else 0 if empty and name != "full" else index
        publications[name] = {
            "observation": built,
            "observation_sha256": hashlib.sha256(observation.encode_observation(built)).hexdigest(),
            "publication_id": f"artifact-{published_index}",
            "publication_time_source": "successful-upload-step-completion",
            "published_at": [
                "2026-10-18T00:00:20Z",
                "2026-10-18T00:00:10Z",
                "2026-10-18T00:00:30Z",
            ][published_index],
            "publication_sha256": str(published_index + 1) * 64,
        }
    return expected, publications


def _rebuild(publication):
    raw = publication["observation"]
    publication["observation"] = observation.build_observation(
        raw["identities"], {name: raw[name] for name in feedback.MEASUREMENT_FIELDS}
    )
    publication["observation_sha256"] = hashlib.sha256(
        observation.encode_observation(publication["observation"])
    ).hexdigest()


def test_shared_execution_reports_actual_signed_publication_lead_only():
    expected, publications = _case()
    report = feedback.build_feedback_report(expected, publications)
    assert report["test-feedback-version"] == 1
    assert report["activation"] == report["selective_execution"] == "disabled"
    assert report["full_oracle"]["status"] == "passed"
    assert report["comparisons"]["current_vs_proposed"]["validated_publication_lead_seconds"] == 10
    assert report["comparisons"]["full_vs_proposed"]["validated_publication_lead_seconds"] == 20
    assert report["comparisons"]["full_vs_current"]["validated_publication_lead_seconds"] == 10
    assert set(report["unmeasured"].values()) == {None}
    assert all(
        item["observation"]["comparison"] is None for item in report["publications"].values()
    )
    assert json.loads(feedback.encode_feedback_report(report)) == report


def test_later_proposed_publication_is_retained_as_negative_lead():
    expected, publications = _case()
    publications["proposed"]["published_at"] = "2026-10-18T00:00:25Z"
    report = feedback.build_feedback_report(expected, publications)
    assert report["comparisons"]["current_vs_proposed"]["validated_publication_lead_seconds"] == -5


@pytest.mark.parametrize("empty", [False, True])
def test_identical_or_empty_selections_share_publication_and_have_zero_lead(empty):
    expected, publications = _case(identical=not empty, empty=empty)
    report = feedback.build_feedback_report(expected, publications)
    assert report["comparisons"]["current_vs_proposed"]["validated_publication_lead_seconds"] == 0
    publications["proposed"]["published_at"] = "2026-10-18T00:00:31Z"
    with pytest.raises(feedback.FeedbackError, match="one publication"):
        feedback.build_feedback_report(expected, publications)


def test_empty_baseline_is_not_a_measured_benefit_comparator():
    expected, publications = _case(empty=True)
    report = feedback.build_feedback_report(expected, publications)
    comparison = report["comparisons"]["full_vs_current"]
    assert comparison["raw_publication_lead_seconds"] == 10
    assert comparison["validated_publication_lead_seconds"] is None
    assert comparison["unavailable_reasons"] == ["empty-selection-comparator"]


@pytest.mark.parametrize("field", ["publication_id", "published_at", "publication_sha256"])
def test_identical_selection_aliases_must_retain_one_exact_publication(field):
    expected, publications = _case(identical=True)
    publications["proposed"][field] = {
        "publication_id": "other",
        "published_at": "2026-10-18T00:00:31Z",
        "publication_sha256": "9" * 64,
    }[field]
    with pytest.raises(feedback.FeedbackError):
        feedback.build_feedback_report(expected, publications)


def test_distinct_selections_cannot_rename_one_publication_as_two():
    expected, publications = _case()
    publications["proposed"]["publication_id"] = publications["current"]["publication_id"]
    with pytest.raises(feedback.FeedbackError, match="Distinct selections"):
        feedback.build_feedback_report(expected, publications)


@pytest.mark.parametrize("name", feedback.ARMS)
def test_missing_publications_are_explicit_and_do_not_become_success(name):
    expected, publications = _case()
    publications[name] = None
    report = feedback.build_feedback_report(expected, publications)
    comparison = report["comparisons"]["current_vs_proposed"]
    assert comparison["validated_publication_lead_seconds"] is None
    assert comparison["unavailable_reasons"]
    assert report["publications"][name] is None


def test_unobserved_publication_timestamp_remains_unknown():
    expected, publications = _case()
    publications["proposed"]["published_at"] = None
    publications["proposed"]["publication_time_source"] = None
    report = feedback.build_feedback_report(expected, publications)
    comparison = report["comparisons"]["current_vs_proposed"]
    assert comparison["raw_publication_lead_seconds"] is None
    assert comparison["validated_publication_lead_seconds"] is None
    assert comparison["unavailable_reasons"] == ["unobserved-publication-time"]


@pytest.mark.parametrize("source", ["artifact-created-at", "runner-clock", None, True])
def test_creation_or_estimated_time_is_not_confirmed_publication(source):
    expected, publications = _case()
    publications["proposed"]["publication_time_source"] = source
    with pytest.raises(feedback.FeedbackError, match="confirmed upload boundary"):
        feedback.build_feedback_report(expected, publications)


def test_unobserved_timestamp_cannot_claim_a_known_publication_boundary():
    expected, publications = _case()
    publications["proposed"]["published_at"] = None
    with pytest.raises(feedback.FeedbackError, match="confirmed upload boundary"):
        feedback.build_feedback_report(expected, publications)


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "missing", "blocked", "pending"])
def test_full_oracle_nonpassing_excluded_cell_blocks_claim_and_retains_raw_lead(outcome):
    expected, publications = _case()
    full = publications["full"]
    cell = next(
        cell for cell in full["observation"]["cells"] if cell["suite"] == "qualification.monitoring"
    )
    cell["outcome"] = outcome
    if outcome == "missing":
        cell["attempts"] = []
    else:
        cell["attempts"][0]["outcome"] = outcome
    if outcome != "failed":
        full["observation"]["incomplete_reason"] = "Incomplete full oracle."
    _rebuild(full)
    report = feedback.build_feedback_report(expected, publications)
    comparison = report["comparisons"]["current_vs_proposed"]
    assert comparison["raw_publication_lead_seconds"] == 10
    assert comparison["validated_publication_lead_seconds"] is None
    if outcome == "failed":
        assert report["full_oracle"]["excluded_failures"]["proposed"] == [
            {"suite": "qualification.monitoring", "variant": "default"}
        ]
    else:
        assert report["full_oracle"]["status"] == "incomplete"


def test_failed_retry_cannot_be_hidden_by_a_passing_oracle_cell():
    expected, publications = _case()
    full = publications["full"]
    cell = next(
        cell for cell in full["observation"]["cells"] if cell["suite"] == "qualification.monitoring"
    )
    cell["attempts"].insert(
        0, {**cell["attempts"][0], "job_id": "earlier-job", "outcome": "failed"}
    )
    cell["attempts"][1]["retry_ordinal"] = 2
    _rebuild(full)
    report = feedback.build_feedback_report(expected, publications)
    assert report["full_oracle"]["status"] == "failed"
    assert (
        report["comparisons"]["current_vs_proposed"]["validated_publication_lead_seconds"] is None
    )


@pytest.mark.parametrize("outcome", ["pending", "blocked", "cancelled"])
def test_incomplete_retry_history_cannot_be_hidden_by_a_passing_oracle_cell(outcome):
    expected, publications = _case()
    full = publications["full"]
    cell = next(
        cell for cell in full["observation"]["cells"] if cell["suite"] == "qualification.monitoring"
    )
    cell["attempts"].insert(0, {**cell["attempts"][0], "job_id": "earlier-job", "outcome": outcome})
    cell["attempts"][1]["retry_ordinal"] = 2
    _rebuild(full)
    report = feedback.build_feedback_report(expected, publications)
    assert report["full_oracle"]["status"] == "incomplete"
    assert (
        report["comparisons"]["current_vs_proposed"]["validated_publication_lead_seconds"] is None
    )
    assert report["full_oracle"]["incomplete_cells"] == [
        {"suite": "qualification.monitoring", "variant": "default"}
    ]


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "attempt",
        "bindings_sha256",
        "policy_sha256",
        "plan_digest",
        "source",
        "adapter_source",
    ],
)
def test_observation_identities_must_match_independent_expectations(field):
    expected, publications = _case()
    identity = publications["proposed"]["observation"]["identities"]
    if field == "run_id":
        identity[field] = "other-run"
        for cell in publications["proposed"]["observation"]["cells"]:
            cell["attempts"][0][field] = "other-run"
    elif field == "attempt":
        identity[field] = 2
        for cell in publications["proposed"]["observation"]["cells"]:
            cell["attempts"][0][field] = 2
    elif field in {"source", "adapter_source"}:
        identity[field]["execution_commit"] = "f" * 40
    else:
        identity[field] = "9" * 64
    _rebuild(publications["proposed"])
    with pytest.raises(feedback.FeedbackError):
        feedback.build_feedback_report(expected, publications)


@pytest.mark.parametrize("field", ["evidence_sha256", "attempts"])
def test_shared_cells_must_retain_the_same_actual_evidence_and_jobs(field):
    expected, publications = _case()
    cell = publications["proposed"]["observation"]["cells"][0]
    if field == "evidence_sha256":
        cell[field] = "9" * 64
    else:
        cell[field][0]["job_id"] = "other-job"
    _rebuild(publications["proposed"])
    with pytest.raises(feedback.FeedbackError, match="cell evidence"):
        feedback.build_feedback_report(expected, publications)


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "metric",
        "comparison",
        "unknown",
        "missing-cell",
        "native-matrix",
        "policy",
        "publication-before-run",
        "time-disagrees",
    ],
)
def test_malformed_or_contradictory_feedback_inputs_are_rejected(change):
    expected, publications = _case()
    proposed = publications["proposed"]
    if change == "digest":
        proposed["observation_sha256"] = "0" * 64
    elif change == "metric":
        proposed["observation"]["metrics"]["runner_job_seconds"] = 0
    elif change == "comparison":
        proposed["observation"]["comparison"] = {"speedup": 2}
    elif change == "unknown":
        expected["execute"] = True
    elif change == "missing-cell":
        expected["arms"]["full"]["selected"].pop()
    elif change == "native-matrix":
        expected["arms"]["current"]["selected"] = [
            {"suite": "qualification.native", "variant": "jobs"}
        ]
    elif change == "policy":
        expected["arms"]["full"]["policy_sha256"] = "0" * 64
    elif change == "publication-before-run":
        proposed["published_at"] = "2026-10-17T00:00:00Z"
    else:
        proposed["observation"]["timing"]["feedback_at"] = "2026-10-18T00:00:15Z"
        _rebuild(proposed)
    with pytest.raises(feedback.FeedbackError):
        feedback.build_feedback_report(expected, publications)


def test_complete_output_bound_includes_newline_and_input_is_not_aliased(monkeypatch):
    expected, publications = _case()
    report = feedback.build_feedback_report(expected, publications)
    encoded = feedback.encode_feedback_report(report)
    publications["full"]["observation"]["cells"][0]["attempts"][0]["job_id"] = "changed"
    expected["execution_commit"] = "0" * 40
    assert feedback.encode_feedback_report(report) == encoded
    monkeypatch.setattr(feedback, "MAX_DOCUMENT_BYTES", len(encoded))
    assert feedback.encode_feedback_report(report) == encoded
    monkeypatch.setattr(feedback, "MAX_DOCUMENT_BYTES", len(encoded) - 1)
    with pytest.raises(feedback.FeedbackError):
        feedback.encode_feedback_report(report)
