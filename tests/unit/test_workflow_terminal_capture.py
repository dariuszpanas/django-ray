"""Terminal capture stays local, bounded and immutable after invocation exit."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from django_ray.workflow.progress.limits import WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS
from django_ray.workflow.progress.protocol import decode_workflow_progress_event
from django_ray.workflow.progress.terminal_capture import TerminalWorkflowProgressCapture
from tests.unit.test_remote import _WORKFLOW_RUN_IDENTITY


def capture():
    return TerminalWorkflowProgressCapture(
        _WORKFLOW_RUN_IDENTITY,
        "0.0",
        limits=WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    )


def test_retains_only_last_canonical_value_and_refuses_late_offers():
    session = capture()
    for value in range(1000):
        assert session.offer(value, 1000, message="last value")
    session.finish()
    wire = session.terminal_progress_wire()
    assert len(wire) <= WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.event_wire_max_bytes
    assert decode_workflow_progress_event(wire).payload["current"] == 999
    assert not session.offer(1000, 1000)
    assert session.terminal_progress_wire() is wire


def test_copied_invocation_contexts_share_one_sealed_slot():
    session = capture()
    with ThreadPoolExecutor(max_workers=4) as pool:
        offered = list(pool.map(lambda value: session.offer(value, 100), range(100)))
    assert all(offered)
    session.finish()
    wire = session.terminal_progress_wire()
    assert 0 <= decode_workflow_progress_event(wire).payload["current"] < 100
    assert not session.offer(100, 100)
    assert session.terminal_progress_wire() == wire


def test_retry_capture_cannot_inherit_previous_invocation_detail():
    failed, succeeded = capture(), capture()
    failed.offer(1, 2, message="failed invocation")
    failed.finish()
    succeeded.finish()
    assert succeeded.terminal_progress_wire() is None
    assert failed.terminal_progress_wire() is not None


def test_unsealed_or_invalid_capture_cannot_produce_a_final_value():
    session = capture()
    with pytest.raises(RuntimeError, match="not sealed"):
        session.terminal_progress_wire()
    with pytest.raises(ValueError):
        session.offer(float("nan"), 1)
    session.finish()
    assert session.terminal_progress_wire() is None


def test_capture_counters_reconcile_without_retaining_values_or_identity():
    session = capture()
    for value in range(3):
        session.offer(value, 3, message="private message", metrics={"secret": "private value"})
    with pytest.raises(ValueError):
        session.offer(float("nan"), 3)
    report = session.finish()
    assert set(report) == {
        "schema_version",
        "transport",
        "offered",
        "accepted",
        "rejected",
        "superseded",
        "canonical_bytes",
        "retained",
        "retained_bytes",
        "saturated",
    }
    assert report["offered"] == report["accepted"] + report["rejected"] == 4
    assert report["accepted"] == report["superseded"] + report["retained"] == 3
    assert report["retained_bytes"] == len(session.terminal_progress_wire())
    assert report["canonical_bytes"] >= report["retained_bytes"] > 0
    assert report["saturated"] is False
    assert "private" not in str(report)
    report["offered"] = 0
    assert not session.offer(3, 3)
    assert session.finish()["offered"] == 4


def test_capture_counters_saturate_without_changing_the_final_value():
    limits = replace(WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS, identity_max_integer=1024)
    session = TerminalWorkflowProgressCapture(_WORKFLOW_RUN_IDENTITY, "0.0", limits=limits)
    for value in range(1050):
        session.offer(value, 1050)
    report = session.finish()
    assert report["saturated"] is True
    assert report["offered"] == report["accepted"] == report["canonical_bytes"] == 1024
    assert (
        decode_workflow_progress_event(session.terminal_progress_wire(), limits=limits).payload[
            "current"
        ]
        == 1049
    )


@pytest.mark.parametrize("change", ["extra", "bool", "negative", "inconsistent", "oversized"])
def test_capture_report_rejects_corrupt_or_unbounded_metadata(change):
    from django_ray.workflow.progress.capture_diagnostics import normalize_capture_report

    session = capture()
    session.offer(1, 2)
    report = session.finish()
    if change == "extra":
        report["private"] = "secret"
    elif change == "bool":
        report["accepted"] = True
    elif change == "negative":
        report["rejected"] = -1
    elif change == "inconsistent":
        report["offered"] += 1
    else:
        report["retained_bytes"] = WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.event_wire_max_bytes + 1
    with pytest.raises(ValueError) as error:
        normalize_capture_report(report, limits=WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS)
    assert "secret" not in str(error.value)


def test_final_outcome_carries_capture_report_without_an_actor_handle(monkeypatch):
    from django_ray.runtime.context import report_workflow_progress
    from tests.unit.test_remote import _execute_bound_workflow_step

    def callback():
        report_workflow_progress(1, 2)
        report_workflow_progress(2, 2)
        return 42

    monkeypatch.setattr("django_ray.runtime.import_utils.import_callable", lambda _path: callback)
    result, marker = _execute_bound_workflow_step(
        "tests.unit.test_remote.workflow_target",
        False,
        (),
        {},
        {},
        _WORKFLOW_RUN_IDENTITY["task_execution_pk"],
        None,
        "0.0",
        workflow_run_identity=_WORKFLOW_RUN_IDENTITY,
        return_outcome_marker=True,
    )
    assert result == 42
    event = decode_workflow_progress_event(marker)
    report = event.payload["capture_report"]
    assert report["offered"] == report["accepted"] == 2
    assert report["superseded"] == report["retained"] == 1
    assert report["transport"] == "terminal_capture"

    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind
    from django_ray.workflow.progress.publication import _validate_ingress_envelope
    from tests.unit.test_remote import _progress_actor, _progress_wire

    collector = _progress_actor(limits=WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS)
    assert collector.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "0.0",
                "label": "capture",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )
    assert collector.ingest(marker)
    first = collector.snapshot()["ingress"]["capture"]
    assert collector.ingest(marker)
    snapshot = collector.snapshot()
    assert snapshot["ingress"]["capture"] == first
    assert first["scope"] == "successful_final_invocations"
    assert first["reports"] == 1
    assert first["offered"] == 2
    assert snapshot["ingress"]["accepted_by_kind"]["application_progress"] == 0
    _validate_ingress_envelope(
        snapshot["ingress"],
        revision=snapshot["revision"],
        limits=WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    )
    # Final captures can be evicted for lifecycle capacity despite having no
    # application-progress RPC. Their settlement must count as display ingress.
    snapshot["ingress"]["replaceable"] = {
        "evicted_nodes": 1,
        "evicted_events": 0,
        "dropped_updates": 0,
    }
    _validate_ingress_envelope(
        snapshot["ingress"],
        revision=snapshot["revision"],
        limits=WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
    )
    snapshot["ingress"]["replaceable"]["evicted_nodes"] = 3
    with pytest.raises(ValueError):
        _validate_ingress_envelope(
            snapshot["ingress"],
            revision=snapshot["revision"],
            limits=WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
        )


def test_capture_aggregate_is_bounded_saturating_and_detached():
    from django_ray.workflow.progress.capture_diagnostics import (
        TerminalCaptureCounters,
        normalize_capture_totals,
    )

    limits = replace(
        WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
        identity_max_integer=1024,
        topology_node_max_items=2,
    )
    totals = TerminalCaptureCounters(limits=limits)
    session = TerminalWorkflowProgressCapture(_WORKFLOW_RUN_IDENTITY, "0.0", limits=limits)
    for value in range(600):
        session.offer(value, 600)
    report = session.finish()
    totals.add(report)
    totals.add(report)
    snapshot = totals.snapshot()
    assert normalize_capture_totals(snapshot, limits=limits) == snapshot
    assert snapshot["reports"] == snapshot["retained"] == 2
    assert snapshot["offered"] == snapshot["accepted"] == snapshot["superseded"] == 1024
    assert snapshot["saturated"] is True
    with pytest.raises(ValueError, match="admission exceeded"):
        totals.add(report)
    assert totals.snapshot() == snapshot
    snapshot["reports"] = 0
    assert totals.snapshot()["reports"] == 2


def test_final_capture_eviction_preserves_settlement_and_valid_diagnostics():
    from django_ray.workflow.progress.protocol import WorkflowProgressEventKind as Kind
    from django_ray.workflow.progress.publication import _validate_ingress_envelope
    from tests.unit.test_remote import _progress_actor, _progress_wire

    limits = replace(WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS, combined_max_decoded_bytes=1400)
    collector = _progress_actor(limits=limits)
    assert collector.ingest(
        _progress_wire(
            Kind.NODE_REGISTERED,
            {
                "node_id": "0.0",
                "label": "capture",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )
    session = capture()
    session.offer(1, 1, message="x" * 1000)
    report = session.finish()
    progress = decode_workflow_progress_event(session.terminal_progress_wire()).payload
    assert collector.ingest(
        _progress_wire(
            Kind.NODE_SETTLED,
            {
                "node_id": "0.0",
                "state": "SUCCEEDED",
                "error": None,
                "progress": progress,
                "capture_report": report,
                "execution": None,
                "output_preview": None,
            },
        )
    )
    snapshot = collector.snapshot()
    assert snapshot["graph"]["nodes"][0]["state"] == "SUCCEEDED"
    assert snapshot["graph"]["nodes"][0]["progress"] is None
    assert snapshot["ingress"]["replaceable"]["evicted_nodes"] == 1
    assert snapshot["ingress"]["rejected"] == 0
    assert snapshot["ingress"]["accepted_by_kind"]["application_progress"] == 0
    assert snapshot["ingress"]["capture"]["reports"] == 1
    _validate_ingress_envelope(snapshot["ingress"], revision=snapshot["revision"], limits=limits)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reports", 0),
        ("reports", True),
        ("scope", "all_retry_invocations"),
        ("retained", 2),
        ("retained_bytes", 0),
        ("canonical_bytes", 0),
        ("offered", 100),
        ("private", "secret"),
    ],
)
def test_capture_aggregate_rejects_inconsistent_or_unbounded_evidence(field, value):
    from django_ray.workflow.progress.capture_diagnostics import (
        TerminalCaptureCounters,
        normalize_capture_totals,
    )

    limits = WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS
    totals = TerminalCaptureCounters(limits=limits)
    session = capture()
    session.offer(1, 2)
    totals.add(session.finish())
    damaged = totals.snapshot()
    damaged[field] = value
    with pytest.raises(ValueError) as error:
        normalize_capture_totals(damaged, limits=limits)
    assert "secret" not in str(error.value)
