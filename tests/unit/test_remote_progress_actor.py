"""Unit tests for WorkflowProgressActor ingestion, retention, and cost bounds."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

import django_ray.runtime.remote as remote_module
from django_ray.runtime.remote import (
    WorkflowProgressActor,
)
from django_ray.workflow.progress.protocol import (
    WORKFLOW_PROGRESS_LIMITS_V1,
    WorkflowProgressEventKind,
    WorkflowProgressProtocolError,
    decode_workflow_progress_event,
)
from tests.unit._remote_helpers import (
    _OCCURRED_AT,
    _WORKFLOW_PLAN,
    _WORKFLOW_RUN_IDENTITY,
    _producer_report,
    _progress_actor,
    _progress_wire,
)


def test_progress_actor_ingests_bounded_events_and_preserves_terminal_state() -> None:
    actor = _progress_actor()
    events = [
        _progress_wire(
            WorkflowProgressEventKind.EDGES_REGISTERED,
            {"edges": [{"source": "0.0", "target": "0.1"}]},
        ),
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "0.0",
                "label": "first",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {"num_cpus": 1},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "0.1",
                "label": "second",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.STARTED,
            {
                "node_id": "0.0",
                "label": "first",
                "execution": {"ray_task_id": "task-1"},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            {
                "node_id": "0.0",
                "current": 1.0,
                "total": 2.0,
                "message": "half",
                "metrics": {"rows": 1},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.COMPLETED,
            {"node_id": "0.0", "label": "first"},
        ),
        _progress_wire(
            WorkflowProgressEventKind.STARTED,
            {
                "node_id": "0.0",
                "label": "late",
                "execution": {"ray_task_id": "task-late"},
            },
        ),
        _progress_wire(
            WorkflowProgressEventKind.FAILED,
            {"node_id": "0.1", "label": "second", "error": "bounded failure"},
        ),
    ]

    assert all(actor.ingest(event) for event in events)

    snapshot = actor.snapshot()
    first, second = snapshot["graph"]["nodes"]
    assert snapshot["schema_version"] == 2
    assert snapshot["run_identity"] == _WORKFLOW_RUN_IDENTITY
    assert snapshot["plan"] == _WORKFLOW_PLAN
    assert snapshot["state"] == "FAILED"
    assert snapshot["graph"]["edges"] == [{"source": "0.0", "target": "0.1"}]
    assert first["dependencies"] == []
    assert first["state"] == "SUCCEEDED"
    assert first["label"] == "late"
    assert first["execution"]["ray_task_id"] == "task-late"
    assert first["progress"]["current"] == 2.0
    assert first["progress"]["percent"] == 100.0
    assert first["started_at"] == _OCCURRED_AT.timestamp()
    assert first["finished_at"] == _OCCURRED_AT.timestamp()
    assert second["dependencies"] == ["0.0"]
    assert second["state"] == "FAILED"
    assert second["error"] == "bounded failure"
    assert [event["event"] for event in snapshot["recent_events"]] == [
        "STARTED",
        "PROGRESS",
        "COMPLETED",
        "FAILED",
    ]
    assert snapshot["ingress"]["accepted"] == 1 + len(events)
    assert snapshot["ingress"]["rejected"] == 0
    assert snapshot["ingress"]["retained_nodes"] == 2
    assert snapshot["ingress"]["retained_edges"] == 1
    cost = snapshot["ingress"]["cost"]
    assert cost["schema_version"] == 1
    assert cost["saturated"] is False
    assert cost["initialization"]["wire_bytes"] > 0
    assert cost["ingest"]["calls_received"] == len(events)
    assert cost["ingest"]["decoded_calls"] == len(events)
    assert cost["ingest"]["wire_bytes_received"] == sum(map(len, events))
    assert cost["ingest"]["post_disable_calls"] == 0
    assert cost["ingest"]["decoded_by_kind"]["failed"] == 1
    assert cost["delivery_delay"]["samples"] == len(events)
    assert cost["delivery_delay"]["negative_clock_samples"] == 0
    assert cost["snapshot"]["calls"] == 1


def test_progress_actor_aggregates_fixed_producer_reports_without_graph_mutation() -> None:
    actor = _progress_actor()
    baseline = actor.snapshot()
    reports = [
        _progress_wire(
            WorkflowProgressEventKind.PRODUCER_REPORT,
            _producer_report(),
        ),
        _progress_wire(
            WorkflowProgressEventKind.PRODUCER_REPORT,
            _producer_report(
                offered=3,
                submitted=1,
                superseded=1,
                locally_dropped=1,
                acknowledged=0,
                actor_rejected=1,
                pending_acknowledgements=0,
                terminal_handoff="failed",
            ),
        ),
    ]

    assert all(actor.ingest(report) for report in reports)

    snapshot = actor.snapshot()
    assert snapshot["graph"] == baseline["graph"]
    assert snapshot["recent_events"] == baseline["recent_events"]
    assert snapshot["ingress"]["retained_bytes"] == baseline["ingress"]["retained_bytes"]
    assert snapshot["ingress"]["accepted_by_kind"]["producer_report"] == 2
    assert snapshot["ingress"]["cost"]["ingest"]["decoded_by_kind"]["producer_report"] == 2
    assert snapshot["ingress"]["producer"] == {
        "schema_version": 1,
        "saturated": False,
        "reports": 2,
        "offered": 8,
        "submitted": 3,
        "superseded": 3,
        "locally_dropped": 2,
        "acknowledged": 1,
        "actor_rejected": 1,
        "ack_failed": 0,
        "pending_acknowledgements": 1,
        "terminal_handoffs": {
            "not_needed": 0,
            "submitted": 1,
            "failed": 1,
            "actor_unavailable": 0,
        },
    }


def test_progress_actor_saturates_producer_report_aggregates() -> None:
    limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        identity_max_integer=9,
    )
    actor = _progress_actor(limits=limits)
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.PRODUCER_REPORT,
            _producer_report(),
            limits=limits,
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.PRODUCER_REPORT,
            _producer_report(),
            limits=limits,
        )
    )

    producer = actor.snapshot()["ingress"]["producer"]
    assert producer["saturated"] is True
    assert producer["reports"] == 2
    assert producer["offered"] == limits.identity_max_integer
    assert producer["terminal_handoffs"] == {
        "not_needed": 0,
        "submitted": 2,
        "failed": 0,
        "actor_unavailable": 0,
    }


def test_progress_actor_requires_one_canonical_initialization_event() -> None:
    with pytest.raises(WorkflowProgressProtocolError, match="requires an initialized event"):
        WorkflowProgressActor(
            _progress_wire(
                WorkflowProgressEventKind.COMPLETED,
                {"node_id": "not-initialized", "label": "not-initialized"},
            )
        )
    with pytest.raises(WorkflowProgressProtocolError):
        WorkflowProgressActor(b'{"invalid":"initialization"}')
    legacy_constructor: Any = WorkflowProgressActor
    with pytest.raises(TypeError):
        legacy_constructor(
            task_execution_pk=9,
            attempt_number=2,
            execution_generation=6,
            workflow_run_id=_WORKFLOW_RUN_IDENTITY["run_id"],
            plan_summary=_WORKFLOW_PLAN,
        )


def test_progress_actor_ray_surface_exposes_only_ingest_and_controls() -> None:
    import ray

    assert not ray.is_initialized()
    remote_actor = ray.remote(num_cpus=0)(WorkflowProgressActor)
    metadata: Any = remote_actor.__ray_metadata__
    ray_internal_methods = {
        "__init__",
        "__ray_call__",
        "__ray_ready__",
        "__ray_terminate__",
    }

    assert set(metadata.method_meta.methods) - ray_internal_methods == {
        "ingest",
        "disable",
        "snapshot",
    }
    assert not ray.is_initialized()


def test_progress_actor_tolerates_out_of_order_node_placeholders() -> None:
    actor = _progress_actor()
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.MAP_PROGRESS,
            {
                "node_id": "map-first",
                "label": "map:first",
                "submitted": 3,
                "completed": 1,
                "input_exhausted": False,
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.COMPLETED,
            {"node_id": "terminal-first", "label": "terminal:first"},
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.STARTED,
            {
                "node_id": "terminal-first",
                "label": "late-start",
                "execution": {"ray_task_id": "late-task"},
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.SUBMITTED,
            {
                "node_id": "terminal-first",
                "label": "late-submitted",
                "ray_task_id": "late-submitted-task",
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "terminal-first",
                "label": "registered-late",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )

    map_node, terminal_node = actor.snapshot()["graph"]["nodes"]
    assert map_node["node_id"] == "map-first"
    assert map_node["kind"] == "map"
    assert map_node["state"] == "RUNNING"
    assert map_node["fanout"]["in_flight_items"] == 2
    assert terminal_node["node_id"] == "terminal-first"
    assert terminal_node["state"] == "SUCCEEDED"
    assert terminal_node["label"] == "registered-late"
    assert terminal_node["execution"]["ray_task_id"] == "late-submitted-task"
    assert [event["event"] for event in actor.snapshot()["recent_events"]] == [
        "PROGRESS",
        "COMPLETED",
    ]


def test_progress_actor_retains_first_terminal_output_preview() -> None:
    actor = _progress_actor()
    node_id = "previewed"
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": node_id,
                "output_preview": {
                    "schema_version": 1,
                    "availability": "PENDING",
                    "value": None,
                },
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": node_id,
                "output_preview": {
                    "schema_version": 1,
                    "availability": "AVAILABLE",
                    "value": {"items": 3, "status": "ready"},
                },
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": node_id,
                "output_preview": {
                    "schema_version": 1,
                    "availability": "AVAILABLE",
                    "value": {"status": "late replacement"},
                },
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.COMPLETED,
            {"node_id": node_id, "label": "Previewed"},
        )
    )

    [node] = actor.snapshot()["graph"]["nodes"]
    assert node["state"] == "SUCCEEDED"
    assert node["output_preview"] == {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"items": 3, "status": "ready"},
    }


def test_progress_actor_never_attaches_a_preview_value_to_failed_node() -> None:
    actor = _progress_actor()
    node_id = "failed-preview"
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": node_id,
                "output_preview": {
                    "schema_version": 1,
                    "availability": "AVAILABLE",
                    "value": {"status": "computed before failure"},
                },
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.FAILED,
            {
                "node_id": node_id,
                "label": "Failed preview",
                "error": "application failure",
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": node_id,
                "output_preview": {
                    "schema_version": 1,
                    "availability": "AVAILABLE",
                    "value": {"must_not": "be retained"},
                },
            },
        )
    )

    [node] = actor.snapshot()["graph"]["nodes"]
    assert node["state"] == "FAILED"
    assert node["output_preview"] == {
        "schema_version": 1,
        "availability": "UNAVAILABLE",
        "value": None,
    }
    assert "must_not" not in json.dumps(node)


def test_progress_actor_terminalizes_pending_previews_without_byte_drift() -> None:
    actor = _progress_actor()
    pending_node_id = "blocked-preview"
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": pending_node_id,
                "output_preview": {
                    "schema_version": 1,
                    "availability": "PENDING",
                    "value": None,
                },
            },
        )
    )
    pending_retained_bytes = actor.snapshot()["ingress"]["retained_bytes"]
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.FAILED,
            {
                "node_id": "failed-upstream",
                "label": "Failed upstream",
                "error": "application failure",
            },
        )
    )

    snapshot = actor.snapshot()
    nodes_by_id = {node["node_id"]: node for node in snapshot["graph"]["nodes"]}
    assert nodes_by_id[pending_node_id]["output_preview"] == {
        "schema_version": 1,
        "availability": "UNAVAILABLE",
        "value": None,
    }
    retained_state = {
        "edges": snapshot["graph"]["edges"],
        "nodes": [dict(node, dependencies=[]) for node in snapshot["graph"]["nodes"]],
        "plan": snapshot["plan"],
        "recent_events": snapshot["recent_events"],
    }
    assert snapshot["ingress"]["retained_bytes"] == len(
        json.dumps(
            retained_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert snapshot["ingress"]["retained_bytes"] > pending_retained_bytes

    from django_ray.runtime.context import WorkflowRunIdentity
    from django_ray.workflow.progress.publication import (
        prepare_terminal_workflow_progress_publication,
    )

    prepared = prepare_terminal_workflow_progress_publication(
        WorkflowRunIdentity(
            task_execution_pk=int(_WORKFLOW_RUN_IDENTITY["task_execution_pk"]),
            attempt_number=int(_WORKFLOW_RUN_IDENTITY["attempt_number"]),
            execution_generation=int(_WORKFLOW_RUN_IDENTITY["execution_generation"]),
            run_id=str(_WORKFLOW_RUN_IDENTITY["run_id"]),
        ),
        snapshot,
        plan_fingerprint=str(_WORKFLOW_PLAN["fingerprint"]),
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
    )
    assert len(prepared.detail.records) == 2
    assert prepared.summary["node_counts"]["failed"] == 1


def test_progress_actor_reserves_and_releases_terminal_pending_preview_bytes() -> None:
    baseline = _progress_actor()
    pending_event = _progress_wire(
        WorkflowProgressEventKind.OUTPUT_PREVIEW,
        {
            "node_id": "bounded-preview",
            "output_preview": {
                "schema_version": 1,
                "availability": "PENDING",
                "value": None,
            },
        },
    )
    assert baseline.ingest(pending_event)
    pending_retained_bytes = baseline.snapshot()["ingress"]["retained_bytes"]
    rejecting_limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        combined_max_decoded_bytes=pending_retained_bytes + 3,
    )
    rejecting_actor = _progress_actor(limits=rejecting_limits)

    assert (
        rejecting_actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.OUTPUT_PREVIEW,
                {
                    "node_id": "bounded-preview",
                    "output_preview": {
                        "schema_version": 1,
                        "availability": "PENDING",
                        "value": None,
                    },
                },
                limits=rejecting_limits,
            )
        )
        is False
    )
    snapshot = rejecting_actor.snapshot()
    assert snapshot["graph"]["nodes"] == []
    assert snapshot["ingress"]["rejected_by_reason"]["retained_bytes_limit"] == 1

    admitting_limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        combined_max_decoded_bytes=pending_retained_bytes + 4,
    )
    actor = _progress_actor(limits=admitting_limits)
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": "bounded-preview",
                "output_preview": {
                    "schema_version": 1,
                    "availability": "PENDING",
                    "value": None,
                },
            },
            limits=admitting_limits,
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.OUTPUT_PREVIEW,
            {
                "node_id": "bounded-preview",
                "output_preview": {
                    "schema_version": 1,
                    "availability": "TOO_LARGE",
                    "value": None,
                },
            },
            limits=admitting_limits,
        )
    )

    snapshot = actor.snapshot()
    assert snapshot["graph"]["nodes"][0]["output_preview"]["availability"] == "TOO_LARGE"
    assert snapshot["ingress"]["retained_bytes"] == pending_retained_bytes + (
        len("TOO_LARGE") - len("PENDING")
    )
    assert snapshot["ingress"]["retained_bytes"] <= admitting_limits.combined_max_decoded_bytes


def test_progress_actor_fences_output_preview_before_mutation() -> None:
    actor = _progress_actor()
    baseline = actor.snapshot()
    wrong_identity = {
        **_WORKFLOW_RUN_IDENTITY,
        "attempt_number": int(_WORKFLOW_RUN_IDENTITY["attempt_number"]) + 1,
    }

    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.OUTPUT_PREVIEW,
                {
                    "node_id": "wrong-attempt",
                    "output_preview": {
                        "schema_version": 1,
                        "availability": "AVAILABLE",
                        "value": {"marker": "must not cross the fence"},
                    },
                },
                run_identity=wrong_identity,
            )
        )
        is False
    )

    snapshot = actor.snapshot()
    assert snapshot["graph"] == baseline["graph"]
    assert snapshot["revision"] == baseline["revision"]
    assert snapshot["ingress"]["rejected_by_reason"]["fence_mismatch"] == 1
    assert "must not cross the fence" not in json.dumps(snapshot)


def test_progress_actor_rejects_before_mutation_and_exposes_no_legacy_rpc() -> None:
    actor = _progress_actor()
    baseline = actor.snapshot()
    wrong_identity = {
        **_WORKFLOW_RUN_IDENTITY,
        "run_id": "00000000-0000-0000-0000-000000000218",
    }
    wrong_fence_malformed = json.loads(
        _progress_wire(
            WorkflowProgressEventKind.FAILED,
            {
                "node_id": "wrong-fence-malformed",
                "label": "wrong-fence-malformed",
                "error": "wrong-fence-malformed-secret",
            },
            run_identity=wrong_identity,
        )
    )
    wrong_fence_malformed["payload"] = {"invalid": "wrong-fence-payload-secret"}
    wrong_fence_malformed_wire = json.dumps(
        wrong_fence_malformed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert actor.ingest(b'{"secret":"rejected-value"}') is False
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.FAILED,
                {
                    "node_id": "wrong-fence",
                    "label": "wrong",
                    "error": "wrong-fence-secret",
                },
                run_identity=wrong_identity,
            )
        )
        is False
    )
    assert actor.ingest(wrong_fence_malformed_wire) is False
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.INITIALIZED,
                {"plan": _WORKFLOW_PLAN},
            )
        )
        is False
    )

    snapshot = actor.snapshot()
    assert snapshot["revision"] == baseline["revision"]
    assert snapshot["updated_at"] == baseline["updated_at"]
    assert snapshot["graph"] == baseline["graph"]
    assert snapshot["recent_events"] == baseline["recent_events"]
    assert snapshot["ingress"]["retained_bytes"] == baseline["ingress"]["retained_bytes"]
    assert snapshot["ingress"]["rejected"] == 4
    assert snapshot["ingress"]["rejected_by_reason"]["protocol_error"] == 1
    assert snapshot["ingress"]["rejected_by_reason"]["fence_mismatch"] == 2
    assert snapshot["ingress"]["rejected_by_reason"]["unexpected_initialized"] == 1
    assert set(snapshot["ingress"]["rejected_by_reason"]) == {
        "protocol_error",
        "fence_mismatch",
        "unexpected_initialized",
        "node_limit",
        "edge_limit",
        "retained_bytes_limit",
    }
    assert "rejected-value" not in json.dumps(snapshot)
    assert "wrong-fence-secret" not in json.dumps(snapshot)
    assert "wrong-fence-payload-secret" not in json.dumps(snapshot)
    assert {name for name, value in vars(WorkflowProgressActor).items() if callable(value)} == {
        "__init__",
        "ingest",
        "disable",
        "snapshot",
    }
    for legacy_method in (
        "register",
        "register_map",
        "submitted",
        "started",
        "progress",
        "map_progress",
        "completed",
        "failed",
    ):
        assert not hasattr(actor, legacy_method)


def test_progress_actor_replacement_subtracts_prior_canonical_size() -> None:
    actor = _progress_actor()
    long_label = "long-" + ("x" * 200)
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "replace",
                "label": long_label,
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )
    retained_with_long_label = actor.snapshot()["ingress"]["retained_bytes"]

    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "replace",
                "label": "short",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )

    snapshot = actor.snapshot()
    retained_state = {
        "edges": snapshot["graph"]["edges"],
        "nodes": snapshot["graph"]["nodes"],
        "plan": snapshot["plan"],
        "recent_events": snapshot["recent_events"],
    }
    assert snapshot["ingress"]["retained_nodes"] == 1
    assert snapshot["ingress"]["retained_bytes"] < retained_with_long_label
    assert snapshot["ingress"]["retained_bytes"] == len(
        json.dumps(
            retained_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert snapshot["graph"]["nodes"][0]["label"] == "short"


def test_progress_actor_rejects_cardinality_overflow_with_fixed_reasons() -> None:
    limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        topology_node_max_items=1,
        topology_edge_max_items=1,
        recent_event_max_items=2,
        combined_max_decoded_bytes=4096,
    )
    actor = _progress_actor(limits=limits)
    node_payload = {
        "node_id": "bounded",
        "label": "bounded",
        "callable_path": "tests.unit.test_remote.workflow_target",
        "runtime_env": {"mode": "inherit"},
        "ray_options": {},
    }

    assert actor.ingest(_progress_wire(WorkflowProgressEventKind.NODE_REGISTERED, node_payload))
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.NODE_REGISTERED,
                {**node_payload, "node_id": "overflow-node"},
            )
        )
        is False
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.EDGES_REGISTERED,
            {"edges": [{"source": "bounded", "target": "edge-one"}]},
        )
    )
    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.EDGES_REGISTERED,
                {"edges": [{"source": "bounded", "target": "edge-two"}]},
            )
        )
        is False
    )
    for index in range(4):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.SUBMITTED,
                {
                    "node_id": "bounded",
                    "label": f"bounded-{index}",
                    "ray_task_id": f"task-{index}",
                },
            )
        )

    snapshot = actor.snapshot()
    assert snapshot["ingress"]["retained_nodes"] == 1
    assert snapshot["ingress"]["retained_edges"] == 1
    assert snapshot["ingress"]["retained_bytes"] <= limits.combined_max_decoded_bytes
    assert snapshot["ingress"]["rejected"] == 2
    assert snapshot["ingress"]["rejected_by_reason"]["node_limit"] == 1
    assert snapshot["ingress"]["rejected_by_reason"]["edge_limit"] == 1
    assert len(snapshot["recent_events"]) == 2
    assert "overflow-node" not in json.dumps(snapshot)
    assert "edge-two" not in json.dumps(snapshot)


def test_progress_actor_rejects_replacement_over_retained_byte_limit() -> None:
    limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        combined_max_decoded_bytes=800,
    )
    actor = _progress_actor(limits=limits)
    payload = {
        "node_id": "replace",
        "label": "short",
        "callable_path": "tests.unit.test_remote.workflow_target",
        "runtime_env": {"mode": "inherit"},
        "ray_options": {},
    }
    assert actor.ingest(_progress_wire(WorkflowProgressEventKind.NODE_REGISTERED, payload))
    before = actor.snapshot()
    oversized_label = "never-retain-" + ("x" * 480)

    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.NODE_REGISTERED,
                {**payload, "label": oversized_label},
            )
        )
        is False
    )

    snapshot = actor.snapshot()
    assert snapshot["revision"] == before["revision"]
    assert snapshot["updated_at"] == before["updated_at"]
    assert snapshot["graph"] == before["graph"]
    assert snapshot["ingress"]["retained_bytes"] == before["ingress"]["retained_bytes"]
    assert snapshot["ingress"]["rejected_by_reason"]["retained_bytes_limit"] == 1
    assert oversized_label not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "terminal_kind", [WorkflowProgressEventKind.FAILED, WorkflowProgressEventKind.COMPLETED]
)
@pytest.mark.parametrize(
    "map_progress,other_node,byte_limit",
    [(False, False, 1350), (False, True, 1700), (True, False, 1300)],
)
@pytest.mark.parametrize("submitted", [1000, WORKFLOW_PROGRESS_LIMITS_V1.identity_max_integer])
def test_replaceable_progress_cannot_displace_terminal_state(
    terminal_kind: WorkflowProgressEventKind,
    map_progress: bool,
    other_node: bool,
    byte_limit: int,
    submitted: int,
) -> None:
    """A terminal event that fits alone must also fit after progress pressure."""
    limits = replace(WORKFLOW_PROGRESS_LIMITS_V1, combined_max_decoded_bytes=byte_limit)
    for with_progress in (False, True):
        actor = _progress_actor(limits=limits)
        for node_id in ["leaf", "other"] if other_node else ["leaf"]:
            if map_progress:
                registration_kind = WorkflowProgressEventKind.MAP_REGISTERED
                registration = {
                    "node_id": node_id,
                    "label": node_id,
                    "max_concurrency": 1,
                    "max_items": None,
                }
            else:
                registration_kind = WorkflowProgressEventKind.NODE_REGISTERED
                registration = {
                    "node_id": node_id,
                    "label": node_id,
                    "callable_path": "tests.unit.test_remote.workflow_target",
                    "runtime_env": {"mode": "inherit"},
                    "ray_options": {},
                }
            assert actor.ingest(_progress_wire(registration_kind, registration))
        if not map_progress:
            assert actor.ingest(
                _progress_wire(
                    WorkflowProgressEventKind.STARTED,
                    {
                        "node_id": "leaf",
                        "label": "leaf",
                        "execution": {
                            "assigned_resources": {},
                            "ray_job_id": None,
                            "ray_node_id": None,
                            "ray_task_id": None,
                            "ray_worker_id": None,
                        },
                    },
                )
            )
        if with_progress:
            node_id = "other" if other_node else "leaf"
            if map_progress:
                progress_kind = WorkflowProgressEventKind.MAP_PROGRESS
                progress = {
                    "node_id": node_id,
                    "label": node_id,
                    "submitted": submitted,
                    "completed": submitted - 1,
                    "input_exhausted": True,
                }
            else:
                progress_kind = WorkflowProgressEventKind.APPLICATION_PROGRESS
                progress = {
                    "node_id": node_id,
                    "current": 1.0,
                    "total": 2.0,
                    "message": "x" * 400,
                    "metrics": {},
                }
            # Valid display data can be dropped or evicted under pressure;
            # neither path may invalidate the complete lifecycle snapshot.
            assert actor.ingest(_progress_wire(progress_kind, progress))
        terminal = {"node_id": "leaf", "label": "leaf"}
        if terminal_kind is WorkflowProgressEventKind.FAILED:
            terminal["error"] = "fixture failure"
        assert actor.ingest(_progress_wire(terminal_kind, terminal))
        snapshot = actor.snapshot()
        assert snapshot["ingress"]["retained_bytes"] <= byte_limit
        assert snapshot["ingress"]["rejected"] == 0
        leaf = next(node for node in snapshot["graph"]["nodes"] if node["node_id"] == "leaf")
        assert leaf["state"] == (
            "FAILED" if terminal_kind is WorkflowProgressEventKind.FAILED else "SUCCEEDED"
        )
        if map_progress and with_progress:
            assert leaf["fanout"]["submitted_items"] == submitted
        if not other_node:
            from django_ray.runtime.context import WorkflowRunIdentity
            from django_ray.workflow.progress.publication import (
                prepare_terminal_workflow_progress_publication,
            )

            prepared = prepare_terminal_workflow_progress_publication(
                WorkflowRunIdentity(
                    task_execution_pk=int(_WORKFLOW_RUN_IDENTITY["task_execution_pk"]),
                    attempt_number=int(_WORKFLOW_RUN_IDENTITY["attempt_number"]),
                    execution_generation=int(_WORKFLOW_RUN_IDENTITY["execution_generation"]),
                    run_id=str(_WORKFLOW_RUN_IDENTITY["run_id"]),
                ),
                snapshot,
                plan_fingerprint=str(_WORKFLOW_PLAN["fingerprint"]),
                selected_strategy="dynamic_tasks",
                reporting_policy="full",
                detail_days=7,
            )
            assert prepared.summary["state"] == leaf["state"]
            detail = json.loads(prepared.detail.records[0].payload)
            assert detail["state"] == leaf["state"]
            if map_progress and with_progress:
                assert detail["fanout"]["submitted_items"] == submitted


@pytest.mark.parametrize("structural_kind", ["node", "edges"])
def test_progress_actor_preserves_structural_admission_at_byte_boundary(structural_kind) -> None:
    registration = _progress_wire(
        WorkflowProgressEventKind.NODE_REGISTERED,
        {
            "node_id": "leaf",
            "label": "leaf",
            "callable_path": "tests.unit.test_remote.workflow_target",
            "runtime_env": {"mode": "inherit"},
            "ray_options": {},
        },
    )
    if structural_kind == "node":
        structural = _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "next",
                "label": "next",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    else:
        structural = _progress_wire(
            WorkflowProgressEventKind.EDGES_REGISTERED,
            {"edges": [{"source": "leaf", "target": f"child-{index}"} for index in range(16)]},
        )
    baseline = _progress_actor()
    assert baseline.ingest(registration)
    assert baseline.ingest(structural)
    expected = baseline.snapshot()
    minimum = expected["ingress"]["retained_bytes"]
    evictions = 0
    for byte_limit in (minimum, minimum + 1, minimum + 200, minimum + 500):
        actor = _progress_actor(
            limits=replace(WORKFLOW_PROGRESS_LIMITS_V1, combined_max_decoded_bytes=byte_limit)
        )
        assert actor.ingest(registration)
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.APPLICATION_PROGRESS,
                {
                    "node_id": "leaf",
                    "current": 1.0,
                    "total": 2.0,
                    "message": "x" * 200,
                    "metrics": {},
                },
            )
        )
        assert actor.ingest(structural)
        actual = actor.snapshot()
        assert actual["ingress"]["rejected"] == 0
        assert actual["ingress"]["retained_bytes"] <= byte_limit
        assert actual["graph"]["edges"] == expected["graph"]["edges"]
        assert [dict(node, progress=None) for node in actual["graph"]["nodes"]] == expected[
            "graph"
        ]["nodes"]
        evictions += actual["ingress"]["replaceable"]["evicted_nodes"]
    assert evictions > 0


def test_progress_actor_saturates_display_drop_counters_without_rejection() -> None:
    actor = _progress_actor(
        limits=replace(
            WORKFLOW_PROGRESS_LIMITS_V1,
            combined_max_decoded_bytes=900,
            identity_max_integer=9,
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "leaf",
                "label": "leaf",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )
    wire = _progress_wire(
        WorkflowProgressEventKind.APPLICATION_PROGRESS,
        {"node_id": "leaf", "current": 1.0, "total": 2.0, "message": "x" * 400, "metrics": {}},
    )
    for _ in range(20):
        assert actor.ingest(wire)
    snapshot = actor.snapshot()
    assert snapshot["ingress"]["replaceable"] == {
        "evicted_nodes": 0,
        "evicted_events": 0,
        "dropped_updates": 9,
    }
    assert snapshot["ingress"]["cost"]["saturated"] is True
    assert snapshot["ingress"]["rejected"] == 0
    assert snapshot["ingress"]["retained_bytes"] <= 900
    assert snapshot["graph"]["nodes"][0]["progress"] is None


def test_progress_actor_rejects_map_counts_that_cannot_be_retained() -> None:
    actor = _progress_actor(
        limits=replace(WORKFLOW_PROGRESS_LIMITS_V1, combined_max_decoded_bytes=600)
    )
    before = actor.snapshot()
    assert not actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.MAP_PROGRESS,
            {
                "node_id": "unregistered-map",
                "label": "map",
                "submitted": 1000,
                "completed": 1,
                "input_exhausted": True,
            },
        )
    )
    after = actor.snapshot()
    assert after["graph"] == before["graph"]
    assert after["revision"] == before["revision"]
    assert after["ingress"]["rejected"] == before["ingress"]["rejected"] + 1
    assert after["ingress"]["replaceable"]["dropped_updates"] == 0


def test_progress_actor_preserves_progress_when_terminal_data_cannot_fit() -> None:
    limits = replace(WORKFLOW_PROGRESS_LIMITS_V1, combined_max_decoded_bytes=1400)
    actor = _progress_actor(limits=limits)
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.NODE_REGISTERED,
            {
                "node_id": "leaf",
                "label": "leaf",
                "callable_path": "tests.unit.test_remote.workflow_target",
                "runtime_env": {"mode": "inherit"},
                "ray_options": {},
            },
        )
    )
    assert actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.APPLICATION_PROGRESS,
            {"node_id": "leaf", "current": 1.0, "total": 2.0, "message": "x" * 400, "metrics": {}},
        )
    )
    before = actor.snapshot()
    assert not actor.ingest(
        _progress_wire(
            WorkflowProgressEventKind.FAILED,
            {"node_id": "leaf", "label": "leaf", "error": "e" * 2000},
        )
    )
    after = actor.snapshot()
    assert after["graph"] == before["graph"]
    assert after["recent_events"] == before["recent_events"]
    assert after["revision"] == before["revision"]
    assert after["ingress"]["retained_bytes"] == before["ingress"]["retained_bytes"]
    assert after["ingress"]["replaceable"] == before["ingress"]["replaceable"]


def test_progress_actor_counts_protocol_truncation_without_raw_retention() -> None:
    actor = _progress_actor()
    raw_error = "sensitive-" + ("x" * 5000)
    wire = _progress_wire(
        WorkflowProgressEventKind.FAILED,
        {
            "node_id": "failed",
            "label": "failed",
            "error": raw_error,
        },
    )
    decoded = decode_workflow_progress_event(wire)
    assert decoded.truncated is True

    assert actor.ingest(wire)

    snapshot = actor.snapshot()
    assert snapshot["ingress"]["truncated"] == 1
    assert raw_error not in json.dumps(snapshot)
    assert snapshot["graph"]["nodes"][0]["error"] != raw_error
    cost = snapshot["ingress"]["cost"]
    assert cost["ingest"]["calls_received"] == 1
    assert cost["ingest"]["wire_bytes_received"] == len(wire)
    assert cost["ingest"]["decoded_by_kind"]["failed"] == 1
    assert raw_error not in json.dumps(cost)


def test_progress_actor_ingress_counters_saturate_at_injected_identity_limit() -> None:
    limits = replace(
        WORKFLOW_PROGRESS_LIMITS_V1,
        identity_max_integer=9,
    )
    actor = _progress_actor(limits=limits)
    for index in range(20):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.SUBMITTED,
                {
                    "node_id": "bounded-counter",
                    "label": "bounded-counter",
                    "ray_task_id": f"task-{index}",
                },
            )
        )
    for _index in range(20):
        assert actor.ingest(b'{"invalid":"counter-secret"}') is False
    for index in range(20):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.FAILED,
                {
                    "node_id": f"failed-{index}",
                    "label": f"failed-{index}",
                    "error": "x" * 5000,
                },
            )
        )

    ingress = actor.snapshot()["ingress"]
    assert ingress["accepted"] == limits.identity_max_integer
    assert ingress["rejected"] == limits.identity_max_integer
    assert ingress["truncated"] == limits.identity_max_integer
    assert (
        ingress["accepted_by_kind"][WorkflowProgressEventKind.SUBMITTED.value]
        == limits.identity_max_integer
    )
    assert ingress["rejected_by_reason"]["protocol_error"] == limits.identity_max_integer
    cost = ingress["cost"]
    assert cost["saturated"] is True
    assert cost["initialization"]["wire_bytes"] == limits.identity_max_integer
    assert cost["ingest"]["calls_received"] == limits.identity_max_integer
    assert cost["ingest"]["wire_bytes_received"] == limits.identity_max_integer
    assert cost["ingest"]["decoded_calls"] == limits.identity_max_integer
    assert (
        cost["ingest"]["decoded_by_kind"][WorkflowProgressEventKind.SUBMITTED.value]
        == limits.identity_max_integer
    )


def test_progress_actor_recent_events_are_a_fixed_ring() -> None:
    actor = _progress_actor()
    for index in range(40):
        assert actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.SUBMITTED,
                {
                    "node_id": "ring",
                    "label": f"ring-{index}",
                    "ray_task_id": f"task-{index}",
                },
            )
        )

    snapshot = actor.snapshot()
    assert len(snapshot["recent_events"]) == 32
    assert snapshot["recent_events"][0]["label"] == "ring-8"
    assert snapshot["recent_events"][-1]["label"] == "ring-39"


def test_progress_actor_drains_ingests_after_disable_without_diagnostics() -> None:
    actor = _progress_actor()
    before = actor.snapshot()

    actor.disable()

    assert (
        actor.ingest(
            _progress_wire(
                WorkflowProgressEventKind.FAILED,
                {"node_id": "late", "label": "late", "error": "late-secret"},
            )
        )
        is False
    )
    after = actor.snapshot()
    assert after["revision"] == before["revision"]
    assert after["updated_at"] == before["updated_at"]
    assert after["graph"] == before["graph"]
    assert after["recent_events"] == before["recent_events"]
    assert after["ingress"]["accepted"] == before["ingress"]["accepted"]
    assert after["ingress"]["rejected"] == before["ingress"]["rejected"]
    assert after["ingress"]["retained_bytes"] == before["ingress"]["retained_bytes"]
    before_cost = before["ingress"]["cost"]
    after_cost = after["ingress"]["cost"]
    assert after_cost["ingest"]["calls_received"] == (before_cost["ingest"]["calls_received"] + 1)
    assert after_cost["ingest"]["post_disable_calls"] == (
        before_cost["ingest"]["post_disable_calls"] + 1
    )
    assert after_cost["snapshot"]["calls"] == before_cost["snapshot"]["calls"] + 1
    assert "late-secret" not in json.dumps(after_cost)


def test_progress_actor_cost_is_fixed_bounded_and_actor_observed(monkeypatch) -> None:
    actor = _progress_actor()
    wall_ticks = iter([0, 10, 20, 35, 40, 60, 70, 80, 90, 105, 110, 140])
    cpu_ticks = iter([0, 4, 10, 16, 20, 27, 30, 32, 40, 45, 50, 59])
    received_ticks = iter([1_000, 1_000, 1_000, 1_000, 1_000])
    occurred_ticks = iter([900, 1_100])
    monkeypatch.setattr(remote_module, "_wall_time_ns", lambda: next(wall_ticks))
    monkeypatch.setattr(remote_module, "_process_cpu_ns", lambda: next(cpu_ticks))
    monkeypatch.setattr(remote_module, "_utc_time_us", lambda: next(received_ticks))
    monkeypatch.setattr(
        remote_module,
        "_event_timestamp_us",
        lambda _value: next(occurred_ticks),
    )

    accepted_wire = _progress_wire(
        WorkflowProgressEventKind.SUBMITTED,
        {
            "node_id": "accepted",
            "label": "accepted",
            "ray_task_id": "task-accepted",
        },
    )
    accepted_negative_clock_wire = _progress_wire(
        WorkflowProgressEventKind.COMPLETED,
        {
            "node_id": "accepted",
            "label": "accepted",
        },
    )
    wrong_fence_wire = _progress_wire(
        WorkflowProgressEventKind.FAILED,
        {
            "node_id": "wrong-fence",
            "label": "wrong-fence",
            "error": "must-not-enter-cost",
        },
        run_identity={
            **_WORKFLOW_RUN_IDENTITY,
            "run_id": "00000000-0000-0000-0000-000000000218",
        },
    )
    malformed_wire = b'{"private":"must-not-enter-cost"}'
    post_disable_wire = _progress_wire(
        WorkflowProgressEventKind.COMPLETED,
        {
            "node_id": "post-disable",
            "label": "post-disable",
        },
    )

    assert actor.ingest(accepted_wire) is True
    assert actor.ingest(accepted_negative_clock_wire) is True
    assert actor.ingest(wrong_fence_wire) is False
    assert actor.ingest(malformed_wire) is False
    actor.disable()
    assert actor.ingest(post_disable_wire) is False

    ingress = actor.snapshot()["ingress"]
    cost = ingress["cost"]
    assert set(cost) == {
        "schema_version",
        "saturated",
        "initialization",
        "ingest",
        "delivery_delay",
        "snapshot",
    }
    assert cost["saturated"] is False
    assert cost["ingest"] == {
        "calls_received": 5,
        "wire_bytes_received": sum(
            map(
                len,
                (
                    accepted_wire,
                    accepted_negative_clock_wire,
                    wrong_fence_wire,
                    malformed_wire,
                    post_disable_wire,
                ),
            )
        ),
        "decoded_calls": 2,
        "post_disable_calls": 1,
        "decoded_by_kind": {
            kind.value: int(
                kind
                in {
                    WorkflowProgressEventKind.SUBMITTED,
                    WorkflowProgressEventKind.COMPLETED,
                }
            )
            for kind in WorkflowProgressEventKind
        },
        "handler_wall_ns_total": 70,
        "handler_wall_ns_max": 20,
        "handler_cpu_ns_total": 24,
        "handler_cpu_ns_max": 7,
    }
    assert cost["delivery_delay"] == {
        "samples": 1,
        "total_us": 100,
        "max_us": 100,
        "negative_clock_samples": 1,
    }
    assert cost["snapshot"] == {
        "calls": 1,
        "build_wall_ns_total": 30,
        "build_wall_ns_max": 30,
        "build_cpu_ns_total": 9,
        "build_cpu_ns_max": 9,
    }
    assert ingress["accepted"] - 1 == 2
    assert ingress["rejected"] == 2
    assert (
        cost["ingest"]["calls_received"]
        == ingress["accepted"] - 1 + ingress["rejected"] + cost["ingest"]["post_disable_calls"]
    )
    serialized_cost = json.dumps(cost)
    assert "must-not-enter-cost" not in serialized_cost
    assert "accepted" not in serialized_cost
