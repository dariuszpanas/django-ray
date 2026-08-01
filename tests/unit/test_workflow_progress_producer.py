"""Leaf-local workflow progress producer bounds."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from django_ray.workflow_progress_limits import WORKFLOW_PROGRESS_LIMITS_V1
from django_ray.workflow_progress_producer import (
    WorkflowProgressProducerAck,
    WorkflowProgressProducerSession,
)
from django_ray.workflow_progress_protocol import (
    WorkflowProgressEventKind,
    decode_workflow_progress_event,
)

_RUN_IDENTITY = {
    "schema_version": 1,
    "task_execution_pk": 1,
    "attempt_number": 1,
    "execution_generation": 1,
    "run_id": "00000000-0000-0000-0000-000000000259",
}


class _Ingest:
    def __init__(self, *, fail: bool = False, synchronous: bool = False) -> None:
        self.calls: list[bytes] = []
        self.fail = fail
        self.synchronous = synchronous

    def remote(self, wire: bytes) -> Any:
        if self.fail:
            raise RuntimeError("actor unavailable")
        self.calls.append(wire)
        return None if self.synchronous else object()


class _Actor:
    def __init__(self, *, fail: bool = False, synchronous: bool = False) -> None:
        self.ingest = _Ingest(fail=fail, synchronous=synchronous)


class _BlockedRayIngestActor:
    def __init__(self, run_identity: dict[str, Any]) -> None:
        self.run_identity = run_identity
        self.released = asyncio.Event()
        self.received = asyncio.Condition()
        self.arrival_index = 0
        self.events: list[tuple[int, str, dict[str, Any]]] = []

    async def ingest(self, wire: bytes) -> bool:
        event = decode_workflow_progress_event(
            wire,
            expected_run_identity=self.run_identity,
        )
        self.arrival_index += 1
        arrival_index = self.arrival_index
        await self.released.wait()
        async with self.received:
            self.events.append(
                (
                    arrival_index,
                    event.kind.value,
                    event.payload,
                )
            )
            self.received.notify_all()
        return True

    async def release_and_collect(
        self,
        expected_events: int,
    ) -> list[tuple[int, str, dict[str, Any]]]:
        self.released.set()
        async with self.received:
            await asyncio.wait_for(
                self.received.wait_for(lambda: len(self.events) == expected_events),
                timeout=10,
            )
        return sorted(self.events, key=lambda event: event[0])


def _run_bounded_context_leaf(
    actor: Any,
    run_identity: dict[str, Any],
    node_id: str,
    updates: int,
) -> str:
    from django_ray.runtime.context import (
        report_workflow_progress,
        workflow_step_execution,
    )

    with workflow_step_execution(actor, node_id, run_identity):
        for current in range(1, updates + 1):
            assert report_workflow_progress(current, updates)
    return node_id


@pytest.fixture(scope="module")
def ray_runtime() -> Iterator[Any]:
    import ray

    assert not ray.is_initialized()
    ray.init(address="local", include_dashboard=False, num_cpus=2)
    try:
        yield ray
    finally:
        ray.shutdown()


def _decoded(actor: _Actor) -> list[Any]:
    return [
        decode_workflow_progress_event(
            wire,
            expected_run_identity=_RUN_IDENTITY,
        )
        for wire in actor.ingest.calls
    ]


def test_never_ready_actor_keeps_one_call_and_one_latest_slot() -> None:
    actor = _Actor()
    session = WorkflowProgressProducerSession(
        actor,
        _RUN_IDENTITY,
        "leaf",
        ack_poller=lambda _reference: WorkflowProgressProducerAck.PENDING,
    )

    for current in range(1, 1_001):
        assert session.offer(current, 1_000, metrics={"current": current})

    assert len(actor.ingest.calls) == 1
    report = session.finish()
    assert len(actor.ingest.calls) == 2
    events = _decoded(actor)
    assert [event.kind for event in events] == [
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
    ]
    assert events[0].payload["current"] == 1.0
    assert events[1].payload["current"] == 1_000.0
    assert report == {
        "schema_version": 1,
        "saturated": False,
        "offered": 1_000,
        "submitted": 2,
        "superseded": 998,
        "locally_dropped": 0,
        "acknowledged": 0,
        "actor_rejected": 0,
        "ack_failed": 0,
        "pending_acknowledgements": 2,
        "terminal_handoff": "submitted",
    }


def test_ready_acknowledgements_preserve_full_mode_emission_rate() -> None:
    actor = _Actor()
    session = WorkflowProgressProducerSession(
        actor,
        _RUN_IDENTITY,
        "leaf",
        ack_poller=lambda _reference: WorkflowProgressProducerAck.ACKNOWLEDGED,
    )

    assert session.offer(1, 3)
    assert session.offer(2, 3)
    assert session.offer(3, 3)

    report = session.finish()
    assert len(actor.ingest.calls) == 3
    assert report["offered"] == 3
    assert report["submitted"] == 3
    assert report["superseded"] == 0
    assert report["acknowledged"] == 3
    assert report["pending_acknowledgements"] == 0
    assert report["terminal_handoff"] == "not_needed"


def test_synchronous_adapter_counts_immediate_acknowledgement() -> None:
    actor = _Actor(synchronous=True)
    session = WorkflowProgressProducerSession(actor, _RUN_IDENTITY, "leaf")

    assert session.offer(1, 1)

    report = session.finish()
    assert report["submitted"] == 1
    assert report["acknowledged"] == 1
    assert report["pending_acknowledgements"] == 0


@pytest.mark.parametrize(
    ("ack", "counter"),
    [
        (WorkflowProgressProducerAck.ACTOR_REJECTED, "actor_rejected"),
        (WorkflowProgressProducerAck.ACK_FAILED, "ack_failed"),
    ],
)
def test_terminal_actor_failure_discards_only_replaceable_progress(
    ack: WorkflowProgressProducerAck,
    counter: str,
) -> None:
    actor = _Actor()
    statuses = iter([WorkflowProgressProducerAck.PENDING, ack])
    session = WorkflowProgressProducerSession(
        actor,
        _RUN_IDENTITY,
        "leaf",
        ack_poller=lambda _reference: next(statuses),
    )

    assert session.offer(1, 3)
    assert session.offer(2, 3)

    report = session.finish()
    assert len(actor.ingest.calls) == 1
    assert report[counter] == 1
    assert report["locally_dropped"] == 1
    assert report["terminal_handoff"] == "actor_unavailable"
    assert report["offered"] == (
        report["submitted"] + report["superseded"] + report["locally_dropped"]
    )


def test_submission_failure_is_best_effort_and_reconciled() -> None:
    actor = _Actor(fail=True)
    session = WorkflowProgressProducerSession(actor, _RUN_IDENTITY, "leaf")

    assert session.offer(1, 1) is False

    report = session.finish()
    assert report["offered"] == 1
    assert report["submitted"] == 0
    assert report["locally_dropped"] == 1
    assert report["pending_acknowledgements"] == 0


def test_invalid_value_never_enters_the_slot_or_crosses_actor() -> None:
    actor = _Actor()
    session = WorkflowProgressProducerSession(actor, _RUN_IDENTITY, "leaf")

    with pytest.raises(ValueError, match="must be a scalar"):
        session.offer(1, 2, metrics={"invalid": object()})

    assert actor.ingest.calls == []
    assert session.finish()["offered"] == 0


def test_metric_keys_are_normalized_before_crossing_the_producer_boundary() -> None:
    actor = _Actor()
    session = WorkflowProgressProducerSession(actor, _RUN_IDENTITY, "leaf")

    assert session.offer(1, 2, metrics={"\x1b[32mrows\x1b[0m": 12})

    event = _decoded(actor)[0]
    assert event.payload["metrics"] == {"rows": 12}
    assert b"\x1b" not in actor.ingest.calls[0]


def test_producer_rejects_colliding_normalized_metric_keys_before_submission() -> None:
    actor = _Actor()
    session = WorkflowProgressProducerSession(actor, _RUN_IDENTITY, "leaf")

    with pytest.raises(ValueError, match="duplicate normalized"):
        session.offer(
            1,
            2,
            metrics={"rows": 12, "\x1b[32mrows\x1b[0m": 13},
        )

    assert actor.ingest.calls == []
    assert session.finish()["offered"] == 0


def test_counter_saturation_is_explicit_and_finish_is_idempotent() -> None:
    actor = _Actor()
    limits = replace(WORKFLOW_PROGRESS_LIMITS_V1, identity_max_integer=3)
    session = WorkflowProgressProducerSession(
        actor,
        _RUN_IDENTITY,
        "leaf",
        limits=limits,
        ack_poller=lambda _reference: WorkflowProgressProducerAck.PENDING,
    )

    for current in range(1, 6):
        assert session.offer(current, 5)

    first = session.finish()
    second = session.finish()
    assert first == second
    assert first["saturated"] is True
    assert first["offered"] == 3
    assert first["superseded"] == 3
    assert first["submitted"] == 2
    assert first["pending_acknowledgements"] == 2
    assert session.offer(5, 5) is False


@pytest.mark.real_ray
def test_forked_ray_leaves_remain_independently_bounded(
    ray_runtime: Any,
) -> None:
    leaf_count = 4
    updates = 100
    actor = ray_runtime.remote(num_cpus=0, max_concurrency=16)(_BlockedRayIngestActor).remote(
        _RUN_IDENTITY
    )
    remote_leaf = ray_runtime.remote(_run_bounded_context_leaf)

    leaf_ids = [f"leaf-{index}" for index in range(leaf_count)]
    results = ray_runtime.get(
        [
            remote_leaf.remote(
                actor,
                _RUN_IDENTITY,
                node_id,
                updates,
            )
            for node_id in leaf_ids
        ]
    )
    events = ray_runtime.get(
        actor.release_and_collect.remote(3 * leaf_count),
    )

    assert sorted(results) == leaf_ids
    application_values = {node_id: [] for node_id in leaf_ids}
    producer_reports = []
    for _, kind, payload in events:
        if kind == WorkflowProgressEventKind.APPLICATION_PROGRESS.value:
            application_values[payload["node_id"]].append(payload["current"])
        elif kind == WorkflowProgressEventKind.PRODUCER_REPORT.value:
            producer_reports.append(payload)
    # Concurrent async actor calls may begin in either order; this test owns the
    # retained values, while test_remote.py covers producer submission order.
    assert {node_id: sorted(values) for node_id, values in application_values.items()} == {
        node_id: [1.0, float(updates)] for node_id in leaf_ids
    }
    assert len(producer_reports) == leaf_count
    for report in producer_reports:
        assert report == {
            "schema_version": 1,
            "saturated": False,
            "offered": updates,
            "submitted": 2,
            "superseded": updates - 2,
            "locally_dropped": 0,
            "acknowledged": 0,
            "actor_rejected": 0,
            "ack_failed": 0,
            "pending_acknowledgements": 2,
            "terminal_handoff": "submitted",
        }


@pytest.mark.real_ray
def test_real_ray_blocked_actor_cannot_build_one_leaf_pending_chain(
    ray_runtime: Any,
) -> None:
    actor = ray_runtime.remote(num_cpus=0, max_concurrency=4)(_BlockedRayIngestActor).remote(
        _RUN_IDENTITY
    )
    session = WorkflowProgressProducerSession(actor, _RUN_IDENTITY, "leaf")

    for current in range(1, 1_001):
        assert session.offer(current, 1_000)

    report = session.finish()
    events = ray_runtime.get(actor.release_and_collect.remote(2))
    assert report["offered"] == 1_000
    assert report["submitted"] == 2
    assert report["superseded"] == 998
    assert report["pending_acknowledgements"] == 2
    # Async actor arrival order is deliberately outside this backpressure test.
    assert sorted(
        payload["current"]
        for _, kind, payload in events
        if kind == WorkflowProgressEventKind.APPLICATION_PROGRESS.value
    ) == [1.0, 1_000.0]
