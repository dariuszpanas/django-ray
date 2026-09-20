"""Fixed phase observations from one fixture's shared monotonic clock."""

from __future__ import annotations

from qualification.docker.scenario import QualificationError

PHASE_FIELDS = (
    "enqueue_started_ns",
    "enqueue_returned_ns",
    "claim_observed_ns",
    "submission_started_ns",
    "submission_acknowledged_ns",
    "callable_started_ns",
    "released_ns",
    "callable_finished_ns",
    "receipt_write_started_ns",
    "receipt_committed_ns",
    "persistence_started_ns",
    "persistence_committed_ns",
    "terminal_observed_ns",
)


def phase_durations(stamps: dict) -> dict:
    """Reject impossible partial orders without assuming acknowledgement first."""
    if not isinstance(stamps, dict) or set(stamps) != set(PHASE_FIELDS):
        raise QualificationError("latency-phase-shape")
    if any(type(value) is not int or value <= 0 for value in stamps.values()):
        raise QualificationError("latency-phase-clock")
    # The remote callable can start before submit_job returns to its client.
    paths = (
        PHASE_FIELDS[:5] + ("released_ns",),
        ("submission_started_ns",) + PHASE_FIELDS[5:10],
        ("receipt_write_started_ns",) + PHASE_FIELDS[10:],
        ("receipt_committed_ns", "terminal_observed_ns"),
    )
    if any(
        stamps[earlier] > stamps[later]
        for path in paths
        for earlier, later in zip(path, path[1:], strict=False)
    ):
        raise QualificationError("latency-phase-order")
    intervals = {
        "enqueue_seconds": ("enqueue_started_ns", "enqueue_returned_ns"),
        "queue_to_claim_observation_seconds": ("enqueue_returned_ns", "claim_observed_ns"),
        "claim_to_submission_seconds": ("claim_observed_ns", "submission_started_ns"),
        "submission_ack_seconds": ("submission_started_ns", "submission_acknowledged_ns"),
        "submission_to_callable_seconds": ("submission_started_ns", "callable_started_ns"),
        "deliberate_hold_seconds": ("callable_started_ns", "released_ns"),
        "released_callable_seconds": ("released_ns", "callable_finished_ns"),
        "completion_preparation_seconds": ("callable_finished_ns", "receipt_write_started_ns"),
        "completion_write_seconds": ("receipt_write_started_ns", "receipt_committed_ns"),
        "receipt_wait_upper_seconds": ("receipt_write_started_ns", "persistence_started_ns"),
        "persistence_seconds": ("persistence_started_ns", "persistence_committed_ns"),
        "terminal_observation_seconds": ("persistence_committed_ns", "terminal_observed_ns"),
    }
    result = {name: (stamps[end] - stamps[start]) / 1e9 for name, (start, end) in intervals.items()}
    result["receipt_wait_lower_seconds"] = (
        max(0, stamps["persistence_started_ns"] - stamps["receipt_committed_ns"]) / 1e9
    )
    return result
