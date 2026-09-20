"""Reject misleading phase baselines without requiring native Ray."""

import pytest

from qualification.docker.scenario import QualificationError
from qualification.latency.contract import validate_probe
from qualification.latency.phases import PHASE_FIELDS, phase_durations
from tests.unit.test_latency_qualification import receipt


def test_remote_start_can_precede_submission_acknowledgement():
    stamps = dict(zip(PHASE_FIELDS, range(1, len(PHASE_FIELDS) + 1), strict=True))
    stamps["submission_acknowledged_ns"] = stamps["callable_started_ns"] + 1
    durations = phase_durations(stamps)
    assert durations["submission_ack_seconds"] == 3 / 1e9
    assert durations["submission_to_callable_seconds"] == 2 / 1e9


def test_consumer_can_observe_commit_before_producer_records_write_return():
    stamps = dict(zip(PHASE_FIELDS, range(1, len(PHASE_FIELDS) + 1), strict=True))
    stamps["persistence_started_ns"] = stamps["receipt_write_started_ns"]
    durations = phase_durations(stamps)
    assert durations["receipt_wait_lower_seconds"] == 0
    assert durations["receipt_wait_upper_seconds"] == 0


@pytest.mark.parametrize(
    "mutation", ["missing", "boolean", "order", "extra", "duration", "boolean-duration", "identity"]
)
def test_phase_receipt_rejects_incomplete_or_forged_evidence(mutation):
    value = receipt("/installed/module")
    phases = value["cases"][0]["tasks"][0]["phases"]
    stamps = phases["stamps"]
    if mutation == "missing":
        stamps.pop("submission_acknowledged_ns")
    elif mutation == "boolean":
        stamps["claim_observed_ns"] = True
    elif mutation == "order":
        stamps["persistence_started_ns"] = stamps["receipt_write_started_ns"] - 1
    elif mutation == "extra":
        stamps["payload"] = "must not enter the receipt"
    elif mutation == "duration":
        phases["durations"]["receipt_wait_upper_seconds"] = 0
    elif mutation == "boolean-duration":
        stamps["enqueue_returned_ns"] = stamps["enqueue_started_ns"]
        phases["durations"] = phase_durations(stamps)
        phases["durations"]["enqueue_seconds"] = False
    else:
        stamps["terminal_observed_ns"] += 1
        phases["durations"] = phase_durations(stamps)
    with pytest.raises(QualificationError):
        validate_probe(value, expected_module="/installed/module")
