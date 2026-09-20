"""Reject misleading cost windows before emitting comparative evidence."""

import json

import pytest

from qualification.docker.scenario import QualificationError
from qualification.latency.cost import (
    MAX_COST_SNAPSHOTS,
    ManagerCostSnapshots,
    completion_window_cost,
)


def snapshot(**changes):
    return {
        "manager": "control",
        "at_ns": 1_000_000_000,
        "queries": 20,
        "query_seconds": 0.25,
        "api_requests": 3,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"manager": "replacement"},
        {"at_ns": 1_000_000_000},
        {"at_ns": True},
        {"queries": 19},
        {"queries": True},
        {"query_seconds": float("nan")},
        {"query_seconds": float("inf")},
        {"query_seconds": 0.1},
        {"api_requests": 2},
        {"unexpected": "value"},
    ],
)
def test_rejects_replacement_reset_or_invalid_observations(changes):
    with pytest.raises(QualificationError):
        completion_window_cost(snapshot(), snapshot(at_ns=2_000_000_000) | changes)


def test_excludes_cumulative_startup_cost_and_allows_zero_network_requests():
    assert completion_window_cost(
        snapshot(), snapshot(at_ns=3_000_000_000, queries=24, query_seconds=0.5)
    ) == {
        "elapsed_seconds": 2.0,
        "queries": 4,
        "query_seconds": 0.25,
        "api_requests": 0,
    }


def test_snapshots_are_ordered_create_only_and_bounded(tmp_path):
    observer = ManagerCostSnapshots(tmp_path, "control")
    counters = {"queries": 10, "query_seconds": 0.25, "secret_sql": "never retain"}
    (tmp_path / "control-cost-02.request").touch()
    observer.observe("worker", counters)
    assert not list(tmp_path.glob("*.json"))
    for sequence in range(1, MAX_COST_SNAPSHOTS + 2):
        (tmp_path / f"control-cost-{sequence:02d}.request").touch(exist_ok=True)
        observer.observe("worker", counters)
    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == MAX_COST_SNAPSHOTS
    first_path = tmp_path / "control-cost-01.json"
    first = first_path.read_bytes()
    value = json.loads(first)
    assert set(value) == {"manager", "at_ns", "queries", "query_seconds", "api_requests"}
    assert value["manager"] == "worker"
    assert value["queries"] == 10
    assert value["at_ns"] > 0
    observer.observe("replacement", counters)
    assert first_path.read_bytes() == first


def test_existing_receipt_is_never_overwritten(tmp_path):
    (tmp_path / "control-cost-01.request").touch()
    path = tmp_path / "control-cost-01.json"
    path.write_text("retained")
    with pytest.raises(FileExistsError):
        ManagerCostSnapshots(tmp_path, "control").observe(
            "worker", {"queries": 1, "query_seconds": 0.0}
        )
    assert path.read_text() == "retained"
