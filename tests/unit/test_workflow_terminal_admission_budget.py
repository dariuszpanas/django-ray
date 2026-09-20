"""Reservations remain charged across delayed results and lost acknowledgements."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.workflow_terminal_admission_budget import Reservation, TerminalAdmissionBudget


def assert_reconciled(budget):
    snapshot = budget.snapshot()
    active = snapshot["active"]
    count = snapshot["counters"]
    assert active["calls"] == sum(active["phases"].values())
    assert active["calls"] <= snapshot["limits"]["calls"]
    assert active["bytes"] <= snapshot["limits"]["bytes"]
    assert count["requested"] == count["admitted"] + count["denied"]
    assert count["admitted"] == count["released"] + active["calls"]
    assert count["released"] == count["omitted"] + count["acknowledged"] + count["actor_rejected"]
    assert count["submitted"] == (
        count["acknowledged"]
        + count["actor_rejected"]
        + active["phases"]["submitted"]
        + active["phases"]["uncertain_submission"]
    )


@pytest.mark.parametrize("accepted", [False, True])
def test_slow_and_uncertain_consumption_cannot_reuse_capacity(accepted):
    budget = TerminalAdmissionBudget(max_calls=2, max_bytes=8)
    first, second = budget.reserve(4), budget.reserve(4)
    assert first is not None and second is not None
    assert budget.reserve(1) is None
    assert budget.result(first, b"data")
    assert budget.submit(first) == b"data"
    assert budget.uncertain(first)
    assert budget.uncertain(second)
    assert budget.reserve(4) is None
    assert_reconciled(budget)
    assert budget.acknowledge(first, accepted=accepted)
    replacement = budget.reserve(4)
    assert replacement is not None
    assert not budget.acknowledge(first, accepted=True)
    assert budget.reserve(1) is None
    assert budget.result(second, None)
    assert budget.result(replacement, b"last")
    assert budget.submit(replacement) == b"last"
    assert budget.acknowledge(replacement, accepted=True)
    assert_reconciled(budget)
    assert budget.snapshot()["active"]["calls"] == 0


def test_parallel_reservations_enforce_the_shared_byte_budget():
    budget = TerminalAdmissionBudget(max_calls=3, max_bytes=8)
    with ThreadPoolExecutor(max_workers=8) as executor:
        tickets = list(executor.map(budget.reserve, [4] * 64))
    assert sum(ticket is not None for ticket in tickets) == 2
    assert budget.snapshot()["peak"] == {"calls": 2, "bytes": 8}
    assert_reconciled(budget)


def test_foreign_and_copied_tickets_cannot_release_another_reservation():
    budget = TerminalAdmissionBudget(max_calls=1, max_bytes=4)
    other = TerminalAdmissionBudget(max_calls=1, max_bytes=4)
    owned = budget.reserve(4)
    foreign = other.reserve(4)
    assert owned is not None and foreign is not None
    for ticket in (foreign, Reservation(owned.capacity)):
        assert not budget.result(ticket, None)
        assert not budget.acknowledge(ticket, accepted=True)
    assert budget.reserve(1) is None
    assert_reconciled(budget)


def test_invalid_metadata_is_not_retained_and_does_not_free_capacity():
    budget = TerminalAdmissionBudget(max_calls=1, max_bytes=4)
    ticket = budget.reserve(4)
    assert ticket is not None
    assert not budget.result(ticket, b"private-payload")
    assert budget.reserve(1) is None
    assert "private" not in json.dumps(budget.snapshot())
    assert_reconciled(budget)


def test_fixed_diagnostics_saturate_without_recycling_live_slots():
    budget = TerminalAdmissionBudget(max_calls=1, max_bytes=4, counter_max=9)
    assert budget.reserve(4) is not None
    for _ in range(20):
        assert budget.reserve(1) is None
    snapshot = budget.snapshot()
    assert snapshot["saturated"] is True
    assert snapshot["counters"]["requested"] == snapshot["counters"]["denied"] == 9
    assert snapshot["active"]["calls"] == 1
    assert snapshot["active"]["bytes"] == 4


@pytest.mark.real_ray
@pytest.mark.parametrize("lose_actor", [False, True])
def test_real_ray_slow_collector_keeps_admission_charged_without_blocking_values(
    ray_cluster, lose_actor
):
    from scripts.workflow_terminal_progress_prototype import execute_with_metadata

    class Collector:
        def __init__(self):
            from threading import Event

            self.started = Event()
            self.allowed = Event()

        def ingest(self, wire):
            self.started.set()
            if not self.allowed.wait(timeout=20):
                raise TimeoutError("bounded collector wait expired")
            return wire == b"last"

        def wait_started(self):
            return self.started.wait(timeout=10)

        def release(self):
            self.allowed.set()

    def callback(value, *, report):
        report(b"last")
        return value + 1

    budget = TerminalAdmissionBudget(max_calls=1, max_bytes=1024)
    ticket = budget.reserve(1024)
    assert ticket is not None
    collector = ray_cluster.remote(num_cpus=0, max_concurrency=2, max_restarts=0)(
        Collector
    ).remote()
    remote = ray_cluster.remote(num_cpus=0.25, max_retries=0)(execute_with_metadata)
    refs = []
    killed = False
    try:
        value, metadata = remote.options(num_returns=2).remote(callback, 40, admitted=True)
        refs.extend([value, metadata])
        assert budget.result(ticket, ray_cluster.get(metadata, timeout=30))
        ack = collector.ingest.remote(budget.submit(ticket))
        assert ray_cluster.get(collector.wait_started.remote(), timeout=15)
        assert ray_cluster.wait([ack], timeout=0)[0] == []
        assert budget.uncertain(ticket)
        assert budget.reserve(1024) is None
        following = remote.remote(callback, value, admitted=False)
        refs.append(following)
        assert ray_cluster.get(following, timeout=10) == 42
        if lose_actor:
            ray_cluster.kill(collector, no_restart=True)
            killed = True
            with pytest.raises(ray_cluster.exceptions.RayActorError):
                ray_cluster.get(ack, timeout=10)
            assert budget.reserve(1024) is None
        else:
            ray_cluster.get(collector.release.remote(), timeout=10)
            assert ray_cluster.get(ack, timeout=10) is True
            assert budget.acknowledge(ticket, accepted=True)
            assert budget.reserve(1024) is not None
        assert_reconciled(budget)
        assert budget.snapshot()["peak"] == {"calls": 1, "bytes": 1024}
    finally:
        for ref in refs:
            ray_cluster.cancel(ref, force=True, recursive=True)
        if not killed:
            ray_cluster.kill(collector, no_restart=True)
