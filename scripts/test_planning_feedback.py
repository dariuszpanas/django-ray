"""Pure comparison of authenticated publications from one shared full execution.

This records publication lead, not selective execution savings or merge readiness.
The caller authenticates publication artifacts and evaluates normalized receipts.
No input document can establish either authority by containing matching digests.
"""

from __future__ import annotations

import hashlib
import json

from scripts import test_planning_experiment as observation

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
ARMS = ("current", "proposed", "full")
NATIVE = {("qualification.native", name) for name in ("native", "jobs", "jobs-crash", "core-loss")}
CATALOG = NATIVE | {
    (f"qualification.{name}", "default")
    for name in ("application", "latency", "cancellation", "monitoring")
}
OBSERVATION_FIELDS = {
    "test-experiment-observation-version",
    "activation",
    "authority",
    "identities",
    "experiment_id",
    "arm",
    "fingerprints",
    "cache_state",
    "timing",
    "metrics",
    "cells",
    "artifacts",
    "incomplete_reason",
    "comparison",
}
MEASUREMENT_FIELDS = {
    "experiment_id",
    "arm",
    "fingerprints",
    "cache_state",
    "timing",
    "cells",
    "artifacts",
    "incomplete_reason",
}


class FeedbackError(ValueError):
    """Invalid or mismatched publication records; fixed safe diagnostics only."""


def _require(condition, message):
    if not condition:
        raise FeedbackError(message)


def _selected(value):
    selected = set()
    for raw in observation._array(value, len(CATALOG)):
        cell = observation._object(raw, {"suite", "variant"})
        key = (observation._token(cell["suite"], 64), observation._token(cell["variant"], 64))
        _require(key in CATALOG and key not in selected, "Invalid feedback selection")
        selected.add(key)
    _require(not (selected & NATIVE) or NATIVE <= selected, "Incomplete feedback native matrix")
    return sorted(selected)


def _cell_records(keys):
    return [{"suite": suite, "variant": variant} for suite, variant in sorted(keys)]


def _expected(value):
    raw = observation._object(
        value,
        {
            "execution_commit",
            "execution_tree",
            "adapter_source",
            "run_id",
            "attempt",
            "bindings_sha256",
            "arms",
        },
    )
    source = observation._source(
        {key: raw[key] for key in ("execution_commit", "execution_tree")}, adapter=True
    )
    arms = observation._object(raw["arms"], ARMS)
    checked = {}
    for name in ARMS:
        arm = observation._object(arms[name], {"policy_sha256", "plan_digest", "selected"})
        checked[name] = {
            "policy_sha256": observation._digest(arm["policy_sha256"]),
            "plan_digest": observation._digest(arm["plan_digest"]),
            "selected": _cell_records(_selected(arm["selected"])),
        }
    _require(_selected(checked["full"]["selected"]) == sorted(CATALOG), "Incomplete full oracle")
    _require(
        checked["full"]["policy_sha256"] == checked["proposed"]["policy_sha256"],
        "Full and proposed feedback policies differ",
    )
    return {
        **source,
        "adapter_source": observation._source(raw["adapter_source"], adapter=True),
        "run_id": observation._token(raw["run_id"]),
        "attempt": observation._integer(raw["attempt"]),
        "bindings_sha256": observation._digest(raw["bindings_sha256"]),
        "arms": checked,
    }


def _publication(value, name, expected):
    if value is None:
        return None
    raw = observation._object(
        value,
        {
            "observation",
            "observation_sha256",
            "publication_id",
            "published_at",
            "publication_time_source",
            "publication_sha256",
        },
    )
    document = observation._object(raw["observation"], OBSERVATION_FIELDS)
    encoded = observation.encode_observation(document)
    rebuilt = observation.build_observation(
        document["identities"], {key: document[key] for key in MEASUREMENT_FIELDS}
    )
    _require(encoded == observation.encode_observation(rebuilt), "Invalid frozen observation")
    _require(rebuilt["arm"] == name, "Feedback arm identity differs")
    _require(
        hashlib.sha256(encoded).hexdigest() == observation._digest(raw["observation_sha256"]),
        "Feedback observation digest differs",
    )
    identity = rebuilt["identities"]
    for key in ("execution_commit", "execution_tree"):
        _require(identity["source"][key] == expected[key], "Feedback source identity differs")
    for key in ("adapter_source", "run_id", "attempt", "bindings_sha256"):
        _require(identity[key] == expected[key], "Feedback execution identity differs")
    for key in ("policy_sha256", "plan_digest"):
        _require(identity[key] == expected["arms"][name][key], "Feedback plan identity differs")
    keys = [(cell["suite"], cell["variant"]) for cell in rebuilt["cells"]]
    _require(keys == _selected(expected["arms"][name]["selected"]), "Feedback cells differ")
    published = observation._timestamp(raw["published_at"])
    _require(
        raw["publication_time_source"]
        == ("successful-upload-step-completion" if published is not None else None),
        "Feedback publication timing requires a confirmed upload boundary",
    )
    if published is not None:
        observation._interval(
            observation._timestamp(rebuilt["timing"]["workflow_created_at"]), published
        )
        observation._interval(
            observation._timestamp(rebuilt["timing"]["first_failure_at"]), published
        )
    feedback = rebuilt["timing"]["feedback_at"]
    _require(
        feedback is None or feedback == raw["published_at"], "Feedback publication time differs"
    )
    return {
        "observation": rebuilt,
        "observation_sha256": raw["observation_sha256"],
        "publication_id": observation._token(raw["publication_id"]),
        "published_at": raw["published_at"],
        "publication_time_source": raw["publication_time_source"],
        "publication_sha256": observation._digest(raw["publication_sha256"]),
    }


def _publication_identity(value):
    return tuple(
        value[key]
        for key in (
            "publication_id",
            "published_at",
            "publication_time_source",
            "publication_sha256",
        )
    )


def _correlate(publications, expected):
    seen_cells = {}
    common = None
    for name in ARMS:
        publication = publications[name]
        if publication is None:
            continue
        document = publication["observation"]
        identities = (
            document["experiment_id"],
            document["fingerprints"],
            document["cache_state"],
            document["timing"]["workflow_created_at"],
            document["timing"]["candidate_ready_at"],
        )
        if common is not None:
            _require(common == identities, "Shared execution observation context differs")
        common = identities
        for cell in document["cells"]:
            key = (cell["suite"], cell["variant"])
            if key in seen_cells:
                _require(seen_cells[key] == cell, "Shared execution cell evidence differs")
            seen_cells[key] = cell
        for other in ARMS:
            if other == name or publications[other] is None:
                continue
            same_selection = (
                expected["arms"][name]["selected"] == expected["arms"][other]["selected"]
            )
            same_publication = _publication_identity(publication) == _publication_identity(
                publications[other]
            )
            _require(
                not same_selection or same_publication,
                "Identical selections require one publication",
            )
            _require(
                same_selection
                or publication["publication_id"] != publications[other]["publication_id"],
                "Distinct selections cannot share a publication",
            )


def _failed(cell):
    return cell["outcome"] == "failed" or any(
        item["outcome"] == "failed" for item in cell["attempts"]
    )


def _oracle(publications, expected):
    full = publications["full"]
    if full is None:
        return {
            "status": "unavailable",
            "failed_cells": [],
            "incomplete_cells": [],
            "excluded_failures": None,
        }
    cells = full["observation"]["cells"]
    failed = {(cell["suite"], cell["variant"]) for cell in cells if _failed(cell)}
    incomplete = {
        (cell["suite"], cell["variant"])
        for cell in cells
        if cell["outcome"] not in {"passed", "failed"}
        or any(attempt["outcome"] not in {"passed", "failed"} for attempt in cell["attempts"])
    }
    status = "failed" if failed else "incomplete" if incomplete else "passed"
    if full["observation"]["incomplete_reason"] is not None and status == "passed":
        status = "incomplete"
    return {
        "status": status,
        "failed_cells": _cell_records(failed),
        "incomplete_cells": _cell_records(incomplete),
        "excluded_failures": {
            name: _cell_records(failed - set(_selected(expected["arms"][name]["selected"])))
            for name in ("current", "proposed")
        },
    }


def _comparison(publications, oracle, baseline, selected):
    left, right = publications[baseline], publications[selected]
    reasons = []
    lead = None
    if left is None or right is None:
        reasons.append("missing-publication")
    else:
        if bool(left["observation"]["cells"]) != bool(right["observation"]["cells"]):
            reasons.append("empty-selection-comparator")
        left_time, right_time = (
            observation._timestamp(item["published_at"]) for item in (left, right)
        )
        if left_time is None or right_time is None:
            reasons.append("unobserved-publication-time")
        else:
            lead = (left_time - right_time).total_seconds()
            observation._duration(abs(lead))
        if any(
            item["observation"]["incomplete_reason"] is not None
            or any(cell["outcome"] != "passed" for cell in item["observation"]["cells"])
            for item in (left, right)
        ):
            reasons.append("nonpassing-selection")
    if oracle["status"] != "passed":
        reasons.append("full-oracle-" + oracle["status"])
    return {
        "baseline": baseline,
        "selected": selected,
        "raw_publication_lead_seconds": lead,
        "validated_publication_lead_seconds": lead if not reasons else None,
        "unavailable_reasons": reasons,
    }


def build_feedback_report(expected: object, publications: object) -> dict:
    """Correlate caller-authenticated shared-run publications; never authorize execution.

    Observation hashes cover exact canonical observation bytes including newline.
    Publication hashes cover original provider artifact ZIPs. Publication times
    come from the authenticated successful upload step's completion after
    validation and upload finalization, never artifact creation or job estimates.
    Each alias has its own arm/plan observation inside the same published archive.
    """
    try:
        checked = _expected(expected)
        raw = observation._object(publications, ARMS)
        retained = {name: _publication(raw[name], name, checked) for name in ARMS}
        _correlate(retained, checked)
        oracle = _oracle(retained, checked)
        report = {
            "test-feedback-version": 1,
            "method": "shared-full-run-publication",
            "activation": "disabled",
            "selective_execution": "disabled",
            "authority": "advisory-measurement-only",
            "expected": checked,
            "publications": retained,
            "full_oracle": oracle,
            "comparisons": {
                "current_vs_proposed": _comparison(retained, oracle, "current", "proposed"),
                "full_vs_proposed": _comparison(retained, oracle, "full", "proposed"),
                "full_vs_current": _comparison(retained, oracle, "full", "current"),
            },
            "unmeasured": {
                "selective_execution_wall_savings_seconds": None,
                "selective_execution_runner_savings_seconds": None,
                "merge_readiness_savings_seconds": None,
                "independent_arm_causal_effect": None,
            },
        }
        encode_feedback_report(report)
        return report
    except (observation.ObservationError, TypeError, KeyError, UnicodeError, RecursionError):
        raise FeedbackError("Invalid bounded feedback input") from None


def encode_feedback_report(document: dict) -> bytes:
    """Encode a complete feedback artifact, including newline, within 4 MiB."""
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        chunks = []
        remaining = MAX_DOCUMENT_BYTES - 1
        for part in encoder.iterencode(document):
            raw = part.encode("utf-8")
            remaining -= len(raw)
            _require(remaining >= 0, "Feedback document exceeds its byte limit")
            chunks.append(raw)
        return b"".join(chunks) + b"\n"
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise FeedbackError("Cannot encode bounded feedback JSON") from None
