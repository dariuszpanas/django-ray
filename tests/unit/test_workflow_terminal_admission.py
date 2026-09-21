"""Aggregate terminal admission and honest summary-only degradation."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS
from django_ray.workflow.progress.protocol import WorkflowProgressEventKind
from django_ray.workflows import _RayExecutor
from tests.unit.test_workflow_progress_publication import _execution, _identity


def _executor(monkeypatch):
    executor = object.__new__(_RayExecutor)
    executor.workflow_run_identity = _identity()
    executor.workflow_progress_limits = replace(
        WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
        topology_node_max_items=2,
        topology_edge_max_items=2,
    )
    executor.progress_actor = object()
    calls = []
    monkeypatch.setattr(
        "django_ray.workflow.progress.protocol.send_workflow_progress_event",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return executor, calls


def test_node_admission_is_lifetime_bounded_even_after_markers_are_consumed(monkeypatch):
    executor, calls = _executor(monkeypatch)
    for index in range(1000):
        executor._send_progress_event(
            executor.progress_actor,
            WorkflowProgressEventKind.NODE_REGISTERED,
            {"node_id": str(index)},
        )
    assert len(calls) == 2
    assert executor._progress_node_ids == {"0", "1"}
    assert executor._progress_admission_exhausted


def test_edge_admission_stops_before_exceeding_aggregate_limit(monkeypatch):
    executor, calls = _executor(monkeypatch)
    for index in range(1000):
        executor._send_progress_event(
            executor.progress_actor,
            WorkflowProgressEventKind.EDGES_REGISTERED,
            {"edges": [{"source": str(index), "target": "last"}]},
        )
    assert len(calls) == executor._progress_edge_count == 2
    assert executor._progress_admission_exhausted


def test_slow_map_reporting_is_bounded_across_lifetime_admission(monkeypatch):
    from django_ray.workflow.progress.producer import WorkflowProgressProducerAck

    executor, _ = _executor(monkeypatch)
    wires = []

    def ingest(wire):
        wires.append(wire)
        return object()

    executor.progress_actor = SimpleNamespace(ingest=SimpleNamespace(remote=ingest))
    executor._progress_suppression_depth = 0
    executor._map_progress_producers = {}
    executor._map_progress_sent_at = {}
    monkeypatch.setattr(
        "django_ray.workflow.progress.producer._poll_ray_ack",
        lambda _ref: WorkflowProgressProducerAck.PENDING,
    )
    for index in range(100):
        node = str(index)
        executor.map_started(node, "map", (), max_concurrency=1, max_items=1000)
        for completed in range(1000):
            executor.map_progress(
                node,
                "map",
                submitted=1000,
                completed=completed,
                input_exhausted=True,
                force=True,
            )
        executor.map_finished(
            node,
            "map",
            submitted=1000,
            completed=1000,
            input_exhausted=True,
        )
    limit = executor.workflow_progress_limits.topology_node_max_items
    # One unresolved application call plus one final handoff per admitted map;
    # retired maps cannot free admission and multiply the outstanding budget.
    assert len(executor._map_progress_producers) == limit
    assert len(wires) == 2 * limit
    assert all(
        len(wire) <= executor.workflow_progress_limits.event_wire_max_bytes for wire in wires
    )
    assert (
        sum(len(wire) for wire in wires)
        <= 2 * limit * executor.workflow_progress_limits.event_wire_max_bytes
    )
    assert executor._progress_admission_exhausted


@pytest.mark.real_ray
@pytest.mark.parametrize("unavailable", [False, True])
def test_native_map_lifetime_admission_bounds_pending_calls(ray_cluster, unavailable):
    """Retired maps cannot multiply unresolved calls beyond lifetime admission."""
    import asyncio
    import time

    from django_ray.workflow.progress.protocol import decode_workflow_progress_event
    from tests.unit.test_workflow_retry_settlement import (
        _native_bound_executor,
        _wait_for_actor_death,
    )

    class Collector:
        def __init__(self):
            self.events = []
            self.release = asyncio.Event()

        async def ingest(self, wire):
            event = decode_workflow_progress_event(wire)
            self.events.append((event.kind.value, len(wire)))
            if event.kind is WorkflowProgressEventKind.MAP_PROGRESS:
                await self.release.wait()
            return True

        async def observed(self):
            return self.events

    collector = ray_cluster.remote(num_cpus=0, max_restarts=0, max_concurrency=32)(
        Collector
    ).remote()
    try:
        assert ray_cluster.get(collector.observed.remote(), timeout=10) == []
        executor = _native_bound_executor(collector)
        executor.workflow_progress_limits = replace(
            WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS, topology_node_max_items=4
        )
        limit = executor.workflow_progress_limits.topology_node_max_items
        for index in range(limit):
            executor.map_started(str(index), "map", (), max_concurrency=1, max_items=1000)
        # Interleave every producer while each acknowledgement stays unresolved.
        for completed in range(1000):
            for index in range(limit):
                executor.map_progress(
                    str(index),
                    "map",
                    submitted=1000,
                    completed=completed,
                    input_exhausted=True,
                    force=True,
                )

        def observe_count(kind, expected):
            deadline = time.monotonic() + 10
            while True:
                events = ray_cluster.get(collector.observed.remote(), timeout=10)
                count = sum(name == kind for name, _size in events)
                assert count <= expected
                if count == expected:
                    return events
                assert time.monotonic() < deadline, "collector did not observe bounded traffic"
                time.sleep(0.01)

        events = observe_count("map_progress", limit)
        assert sum(size for kind, size in events if kind == "map_progress") <= (
            limit * executor.workflow_progress_limits.event_wire_max_bytes
        )
        if unavailable:
            ray_cluster.kill(collector, no_restart=True)
            _wait_for_actor_death(ray_cluster, collector, "observed")
            for producer in executor._map_progress_producers.values():
                with pytest.raises(ray_cluster.exceptions.RayActorError):
                    ray_cluster.get(producer._outstanding, timeout=10)
        for index in range(limit):
            executor.map_finished(
                str(index), "map", submitted=1000, completed=1000, input_exhausted=True
            )
        # All maps have finished, but their admission is never returned.
        for index in range(limit, limit + 100):
            executor.map_started(str(index), "map", (), max_concurrency=1, max_items=1000)
            executor.map_progress(
                str(index),
                "map",
                submitted=1000,
                completed=1000,
                input_exhausted=True,
                force=True,
            )
            executor.map_finished(
                str(index), "map", submitted=1000, completed=1000, input_exhausted=True
            )
        assert executor._progress_admission_exhausted
        assert len(executor._progress_node_ids) == len(executor._map_progress_producers) == limit
        assert executor._leaf_final_outcomes == {str(index): True for index in range(limit)}
        reports = [producer.finish() for producer in executor._map_progress_producers.values()]
        assert sum(report["submitted"] for report in reports) <= 2 * limit
        assert sum(report["pending_acknowledgements"] for report in reports) <= 2 * limit
        assert all(report["offered"] == 1001 for report in reports)
        assert all(report["saturated"] is False for report in reports)
        if not unavailable:
            observe_count("completed", limit)
            observe_count("map_progress", 2 * limit)
            observe_count("producer_report", limit)
            events = observe_count("map_registered", limit)
            kinds = [kind for kind, _size in events]
            assert kinds.count("map_registered") == kinds.count("producer_report") == limit
            assert kinds.count("map_progress") == 2 * limit
            assert len(events) == 5 * limit
            assert sum(size for kind, size in events if kind == "map_progress") <= (
                2 * limit * executor.workflow_progress_limits.event_wire_max_bytes
            )
            assert all(report["pending_acknowledgements"] == 2 for report in reports)
            assert all(report["terminal_handoff"] == "submitted" for report in reports)
        else:
            assert all(report["ack_failed"] == 1 for report in reports)
            assert all(report["submitted"] == report["locally_dropped"] == 1 for report in reports)
            assert all(report["pending_acknowledgements"] == 0 for report in reports)
            assert all(report["terminal_handoff"] == "not_needed" for report in reports)
    finally:
        ray_cluster.kill(collector, no_restart=True)


@pytest.mark.django_db
@pytest.mark.parametrize("outcome", ["SUCCEEDED", "FAILED"])
@pytest.mark.parametrize("limit_exceeded", [False, True])
def test_unavailable_terminal_summary_preserves_outcome_without_partial_graph(
    outcome, limit_exceeded
):
    from django_ray.models import WorkflowProgressTopologyManifest
    from django_ray.workflow.admin_graph import (
        AdminWorkflowGraphError,
        inspect_admin_workflow_graph_summary,
    )
    from django_ray.workflow.progress.publication import (
        publish_unavailable_terminal_workflow_progress,
    )
    from django_ray.workflow.progress.reads import get_workflow_progress_summary

    execution, identity = _execution()
    assert publish_unavailable_terminal_workflow_progress(
        identity,
        outcome=outcome,
        started_at=1.0,
        finished_at=2.0,
        detail_days=7,
        limit_exceeded=limit_exceeded,
    )
    envelope = get_workflow_progress_summary(execution, authorize=lambda _: True)
    expected = "LIMIT_EXCEEDED" if limit_exceeded else "NOT_REPORTED"
    assert envelope["availability"] == expected
    summary = envelope["summary"]
    assert summary["state"] == summary["terminal"]["outcome"] == outcome
    assert summary["node_counts"]["declared"] is None
    assert summary["node_counts"]["discovered"] == 0
    assert summary["topology_version"] is summary["detail_revision"] is None
    assert not WorkflowProgressTopologyManifest.objects.exists()
    with pytest.raises(AdminWorkflowGraphError):
        inspect_admin_workflow_graph_summary(envelope)


@pytest.mark.django_db
def test_unavailable_summary_refuses_stale_attempt():
    from django_ray.workflow.progress.publication import (
        WorkflowProgressPilotError,
        publish_unavailable_terminal_workflow_progress,
    )

    execution, identity = _execution()
    type(execution).objects.filter(pk=execution.pk).update(
        execution_generation=identity.execution_generation + 1
    )
    with pytest.raises(WorkflowProgressPilotError, match="stale_fence"):
        publish_unavailable_terminal_workflow_progress(
            identity,
            outcome="SUCCEEDED",
            started_at=1.0,
            finished_at=2.0,
            detail_days=7,
            limit_exceeded=True,
        )
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is None


@pytest.mark.parametrize("failed", [False, True])
def test_overflow_finishes_without_waiting_for_a_partial_snapshot(monkeypatch, failed):
    executor, _ = _executor(monkeypatch)
    executor._progress_admission_exhausted = True
    calls = []
    monkeypatch.setattr(
        executor, "_publish_unavailable_terminal_progress", lambda **kwargs: calls.append(kwargs)
    )
    monkeypatch.setattr(executor, "_disable_progress_reporting", lambda: calls.append("disabled"))
    executor.finish_progress(failed=failed)
    assert calls == [{"failed": failed, "limit_exceeded": True}, "disabled"]


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("lost_during_flush", [False, True])
def test_collector_loss_attempts_one_unavailable_summary(monkeypatch, failed, lost_during_flush):
    executor, _ = _executor(monkeypatch)
    calls = []
    monkeypatch.setattr(
        executor, "_publish_unavailable_terminal_progress", lambda **kwargs: calls.append(kwargs)
    )

    def lose_collector(**kwargs):
        executor.progress_actor = None
        return None

    monkeypatch.setattr(executor, "_flush_progress", lose_collector)
    if not lost_during_flush:
        executor.progress_actor = None
    executor.finish_progress(failed=failed)
    executor.finish_progress(failed=failed)
    assert calls == [{"failed": failed, "limit_exceeded": False}]


def test_repeated_finish_cannot_replace_a_published_graph_with_unavailable_summary(monkeypatch):
    executor, _ = _executor(monkeypatch)
    calls = []
    monkeypatch.setattr(
        executor,
        "_flush_progress",
        lambda **kwargs: {"completed_nodes": 1, "failed_nodes": 0, "total_nodes": 1},
    )
    monkeypatch.setattr(
        executor, "_publish_terminal_progress", lambda _: calls.append("graph") or True
    )
    monkeypatch.setattr(
        executor,
        "_publish_unavailable_terminal_progress",
        lambda **kwargs: calls.append("unavailable"),
    )
    executor.finish_progress()
    executor.finish_progress()
    assert calls == ["graph"]


@pytest.mark.parametrize("policy", ["disabled", "terminal_only"])
def test_actor_free_policies_do_not_attempt_full_mode_fallback(monkeypatch, policy):
    executor, _ = _executor(monkeypatch)
    executor.reporting_policy = policy
    executor.progress_actor = None
    calls = []
    monkeypatch.setattr(
        executor, "_publish_unavailable_terminal_progress", lambda **kwargs: calls.append(kwargs)
    )
    executor.finish_progress()
    assert calls == []
