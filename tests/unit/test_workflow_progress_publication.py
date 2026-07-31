from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

import django_ray.workflow_progress_publication as publication
from django_ray.models import (
    RayTaskExecution,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_plans import PlanEligibility
from django_ray.workflow_progress_reads import (
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)
from django_ray.workflow_progress_summary import (
    deserialize_workflow_progress_summary,
)

RUN_ID = "00000000-0000-0000-0000-000000000212"
FINGERPRINT = "sha256:" + "a" * 64


def _identity(task_execution_pk: int = 212) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=task_execution_pk,
        attempt_number=1,
        execution_generation=1,
        run_id=RUN_ID,
    )


def _accepted_by_kind() -> dict[str, int]:
    return {
        "initialized": 1,
        "node_registered": 1,
        "edges_registered": 1,
        "map_registered": 1,
        "submitted": 0,
        "started": 1,
        "application_progress": 1,
        "map_progress": 1,
        "completed": 2,
        "failed": 0,
    }


def _ingress_cost(snapshot: dict[str, Any]) -> dict[str, Any]:
    decoded_by_kind = dict(snapshot["ingress"]["accepted_by_kind"])
    decoded_by_kind["initialized"] = 0
    decoded_calls = sum(decoded_by_kind.values())
    return {
        "schema_version": 1,
        "saturated": False,
        "initialization": {
            "wire_bytes": 128,
            "handler_wall_ns": 20,
            "handler_cpu_ns": 10,
        },
        "ingest": {
            "calls_received": snapshot["ingress"]["accepted"] - 1,
            "wire_bytes_received": 1_024,
            "decoded_calls": decoded_calls,
            "post_disable_calls": 0,
            "decoded_by_kind": decoded_by_kind,
            "handler_wall_ns_total": 160,
            "handler_wall_ns_max": 20,
            "handler_cpu_ns_total": 80,
            "handler_cpu_ns_max": 10,
        },
        "delivery_delay": {
            "samples": decoded_calls,
            "total_us": 400,
            "max_us": 50,
            "negative_clock_samples": 0,
        },
        "snapshot": {
            "calls": 1,
            "build_wall_ns_total": 100,
            "build_wall_ns_max": 100,
            "build_cpu_ns_total": 50,
            "build_cpu_ns_max": 50,
        },
    }


def _erase_decoded_cost(ingress: dict[str, Any]) -> None:
    cost = ingress["cost"]
    cost["ingest"]["decoded_calls"] = 0
    cost["ingest"]["decoded_by_kind"] = dict.fromkeys(
        cost["ingest"]["decoded_by_kind"],
        0,
    )
    cost["delivery_delay"].update(
        {
            "samples": 0,
            "total_us": 0,
            "max_us": 0,
            "negative_clock_samples": 0,
        }
    )


def _saturate_unrelated_and_erase_decoded_cost(
    ingress: dict[str, Any],
) -> None:
    _erase_decoded_cost(ingress)
    ingress["cost"]["saturated"] = True
    ingress["cost"]["initialization"]["handler_wall_ns"] = (1 << 63) - 1


def _make_zero_delivery_samples_with_nonzero_timing(
    ingress: dict[str, Any],
) -> None:
    delivery = ingress["cost"]["delivery_delay"]
    delivery["samples"] = 0
    delivery["negative_clock_samples"] = ingress["cost"]["ingest"]["decoded_calls"]
    delivery["total_us"] = 1
    delivery["max_us"] = 1


def _snapshot(identity: WorkflowRunIdentity) -> dict[str, Any]:
    accepted_by_kind = _accepted_by_kind()
    accepted = sum(accepted_by_kind.values())
    fanout = {
        "max_concurrency": 2,
        "max_items": 2,
        "submitted_items": 2,
        "completed_items": 2,
        "in_flight_items": 0,
        "input_exhausted": True,
    }
    snapshot = {
        "schema_version": 2,
        "workflow_id": f"django-ray:{identity.task_execution_pk}",
        "run_identity": identity.as_dict(),
        "plan": {
            "plan_format": "django-ray.workflow-plan",
            "plan_format_version": 1,
            "fingerprint": FINGERPRINT,
            "definition_name": "publication-pilot",
            "definition_revision": "v1",
            "topology_class": "dynamic",
            "node_count": 1,
        },
        "revision": accepted - 1,
        "state": "SUCCEEDED",
        "total_nodes": 2,
        "completed_nodes": 2,
        "failed_nodes": 0,
        "running_nodes": 0,
        "pending_nodes": 0,
        "progress_percent": 100.0,
        "started_at": 1_785_365_100.0,
        "updated_at": 1_785_365_104.0,
        "graph": {
            "nodes": [
                {
                    "node_id": "0.0",
                    "kind": "task",
                    "label": "prepare_items",
                    "callable_path": "tests.unit.test_workflows.report_then_make_range",
                    "dependencies": [],
                    "runtime_env": {"mode": "inherit"},
                    "ray_options": {},
                    "state": "SUCCEEDED",
                    "progress": {
                        "current": 2.0,
                        "total": 2.0,
                        "percent": 100.0,
                        "message": "Prepared items",
                        "metrics": {"items": 2},
                        "updated_at": 1_785_365_103.0,
                    },
                    "execution": {
                        "ray_task_id": "task-1",
                        "ray_job_id": "job-1",
                        "ray_node_id": "node-1",
                        "ray_worker_id": "worker-1",
                        "assigned_resources": {"CPU": 1.0},
                    },
                    "started_at": 1_785_365_101.0,
                    "finished_at": 1_785_365_103.0,
                    "error": None,
                },
                {
                    "node_id": "0.1",
                    "kind": "map",
                    "label": "bounded-map:increment",
                    "callable_path": None,
                    "dependencies": ["0.0"],
                    "runtime_env": {"mode": "inherit"},
                    "ray_options": {},
                    "state": "SUCCEEDED",
                    "progress": {
                        "current": 2.0,
                        "total": 2.0,
                        "percent": 100.0,
                        "message": "Collecting bounded map results",
                        "metrics": fanout,
                        "updated_at": 1_785_365_103.5,
                    },
                    "execution": {},
                    "started_at": 1_785_365_101.5,
                    "finished_at": 1_785_365_103.5,
                    "error": None,
                    "fanout": fanout,
                },
            ],
            "edges": [{"source": "0.0", "target": "0.1"}],
        },
        "recent_events": [
            {
                "node_id": "0.0",
                "event": "STARTED",
                "state": "RUNNING",
                "label": "prepare_items",
                "timestamp": 1_785_365_101.0,
            },
            {
                "node_id": "0.0",
                "event": "COMPLETED",
                "state": "SUCCEEDED",
                "label": "prepare_items",
                "timestamp": 1_785_365_103.0,
            },
            {
                "node_id": "0.1",
                "event": "STARTED",
                "state": "RUNNING",
                "label": "bounded-map:increment",
                "timestamp": 1_785_365_101.5,
            },
            {
                "node_id": "0.1",
                "event": "COMPLETED",
                "state": "SUCCEEDED",
                "label": "bounded-map:increment",
                "timestamp": 1_785_365_103.5,
            },
        ],
        "ingress": {
            "accepted": accepted,
            "rejected": 0,
            "truncated": 0,
            "accepted_by_kind": accepted_by_kind,
            "rejected_by_reason": {
                "protocol_error": 0,
                "fence_mismatch": 0,
                "unexpected_initialized": 0,
                "node_limit": 0,
                "edge_limit": 0,
                "retained_bytes_limit": 0,
            },
            "retained_bytes": 1,
            "retained_nodes": 2,
            "retained_edges": 1,
        },
    }
    _refresh_retained_bytes(snapshot)
    return snapshot


def _refresh_retained_bytes(snapshot: dict[str, Any]) -> None:
    nodes = []
    for value in snapshot["graph"]["nodes"]:
        node = deepcopy(value)
        node["dependencies"] = []
        nodes.append(node)
    retained_state = {
        "edges": snapshot["graph"]["edges"],
        "nodes": nodes,
        "plan": snapshot["plan"],
        "recent_events": snapshot["recent_events"],
    }
    snapshot["ingress"]["retained_bytes"] = len(
        json.dumps(
            retained_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _failed_snapshot(identity: WorkflowRunIdentity) -> dict[str, Any]:
    snapshot = _snapshot(identity)
    snapshot["state"] = "FAILED"
    snapshot["completed_nodes"] = 1
    snapshot["failed_nodes"] = 1
    failed = snapshot["graph"]["nodes"][1]
    failed["state"] = "FAILED"
    failed["error"] = "middle branch failed"
    snapshot["recent_events"][-1].update(
        {
            "event": "FAILED",
            "state": "FAILED",
        }
    )
    snapshot["ingress"]["accepted_by_kind"]["completed"] = 1
    snapshot["ingress"]["accepted_by_kind"]["failed"] = 1
    _refresh_retained_bytes(snapshot)
    return snapshot


def _execution() -> tuple[RayTaskExecution, WorkflowRunIdentity]:
    selection = PlanEligibility(("dynamic_tasks",), (), 0).select(
        "dynamic_tasks",
        requested_policy="auto",
        reporting_policy="full",
    )
    execution = RayTaskExecution.objects.create(
        task_id="workflow-schema-v3-pilot",
        callable_path="tests.unit.test_workflows.run_nested_workflow",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=RUN_ID,
        workflow_plan_fingerprint=FINGERPRINT,
        workflow_plan_selection=json.dumps(
            selection.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return execution, _identity(execution.pk)


def _allow(execution: RayTaskExecution) -> bool:
    del execution
    return True


def test_terminal_adapter_splits_topology_detail_and_groups_events() -> None:
    identity = _identity()

    prepared = publication.prepare_terminal_workflow_progress_publication(
        identity,
        _snapshot(identity),
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
    )

    assert prepared.topology.node_ids == frozenset({"0.0", "0.1"})
    assert prepared.topology.map_node_ids == frozenset({"0.1"})
    assert prepared.topology.edges == (("0.0", "0.1"),)
    details = {record.node_id: json.loads(record.payload) for record in prepared.detail.records}
    assert details["0.0"]["execution"]["ray_task_id"] == "task-1"
    assert details["0.1"]["execution"] is None
    assert details["0.1"]["fanout"]["completed_items"] == 2
    assert [event["event"] for event in details["0.0"]["recent_events"]] == [
        "STARTED",
        "COMPLETED",
    ]
    assert all(
        "node_id" not in event for detail in details.values() for event in detail["recent_events"]
    )
    assert prepared.summary["node_counts"] == {
        "declared": None,
        "discovered": 2,
        "retained_topology": 0,
        "retained_detail": 0,
        "pending": 0,
        "running": 0,
        "succeeded": 2,
        "failed": 0,
    }
    assert prepared.summary["selected_strategy"] == "dynamic_tasks"
    assert prepared.summary["plan_fingerprint"] == FINGERPRINT
    assert prepared.summary["limits_profile"] == "schema-v3-pilot-v1"
    assert prepared.summary["summary_revision"] == 1


def test_terminal_adapter_accepts_historical_ingress_without_cost() -> None:
    identity = _identity()
    snapshot = _snapshot(identity)

    assert "cost" not in snapshot["ingress"]
    prepared = publication.prepare_terminal_workflow_progress_publication(
        identity,
        snapshot,
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
    )

    assert prepared.summary["state"] == "SUCCEEDED"


def test_terminal_adapter_accepts_strict_optional_ingress_cost() -> None:
    identity = _identity()
    snapshot = _snapshot(identity)
    snapshot["ingress"]["cost"] = _ingress_cost(snapshot)

    prepared = publication.prepare_terminal_workflow_progress_publication(
        identity,
        snapshot,
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
    )

    assert prepared.summary["state"] == "SUCCEEDED"


def test_terminal_adapter_accepts_consistent_saturated_ingress_cost() -> None:
    identity = _identity()
    snapshot = _snapshot(identity)
    snapshot["ingress"]["cost"] = _ingress_cost(snapshot)
    snapshot["ingress"]["cost"]["saturated"] = True
    snapshot["ingress"]["cost"]["initialization"]["handler_wall_ns"] = (1 << 63) - 1

    prepared = publication.prepare_terminal_workflow_progress_publication(
        identity,
        snapshot,
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
    )

    assert prepared.summary["state"] == "SUCCEEDED"


def test_terminal_adapter_applies_active_counter_and_wire_limits_to_cost() -> None:
    identity = _identity()
    limits = replace(
        publication.WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
        identity_max_integer=1_024,
        event_wire_max_bytes=128,
    )
    snapshot = _snapshot(identity)
    snapshot["ingress"]["cost"] = _ingress_cost(snapshot)

    prepared = publication.prepare_terminal_workflow_progress_publication(
        identity,
        snapshot,
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
        limits=limits,
    )
    assert prepared.summary["state"] == "SUCCEEDED"

    snapshot["ingress"]["cost"]["initialization"]["handler_wall_ns"] = 1_025
    with pytest.raises(publication.WorkflowProgressPilotError) as rejected:
        publication.prepare_terminal_workflow_progress_publication(
            identity,
            snapshot,
            plan_fingerprint=FINGERPRINT,
            selected_strategy="dynamic_tasks",
            reporting_policy="full",
            detail_days=7,
            limits=limits,
        )

    assert rejected.value.reason is publication.WorkflowProgressPilotReason.INVALID_SNAPSHOT


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda ingress: ingress.update({"cost": []}),
            id="malformed-cost",
        ),
        pytest.param(
            lambda ingress: ingress.update({"cost": None}),
            id="explicit-null-cost",
        ),
        pytest.param(
            lambda ingress: ingress["cost"].pop("snapshot"),
            id="missing-cost-field",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["initialization"].update({"handler_wall_ns": -1}),
            id="negative-counter",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["initialization"].update(
                {
                    "wire_bytes": (
                        publication.WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.event_wire_max_bytes
                        + 1
                    )
                }
            ),
            id="initialization-wire-exceeds-event-bound",
        ),
        pytest.param(
            lambda ingress: ingress["cost"].update({"unknown": 0}),
            id="unknown-cost-field",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["ingest"].update(
                {"calls_received": ingress["cost"]["ingest"]["calls_received"] + 1}
            ),
            id="calls-received-inconsistent",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["ingest"].update(
                {"decoded_calls": ingress["cost"]["ingest"]["decoded_calls"] + 1}
            ),
            id="decoded-calls-inconsistent",
        ),
        pytest.param(
            _erase_decoded_cost,
            id="accepted-calls-erased-from-decoded-evidence",
        ),
        pytest.param(
            _saturate_unrelated_and_erase_decoded_cost,
            id="unrelated-saturation-does-not-bypass-arithmetic",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["ingest"].update(
                {"post_disable_calls": (ingress["cost"]["ingest"]["calls_received"] + 1)}
            ),
            id="post-disable-exceeds-calls",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["ingest"].update({"wire_bytes_received": 0}),
            id="received-calls-without-wire-bytes",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["ingest"].update(
                {
                    "wire_bytes_received": (
                        ingress["cost"]["ingest"]["decoded_calls"]
                        * publication.WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS.event_wire_max_bytes
                        + 1
                    )
                }
            ),
            id="received-wire-exceeds-per-call-bound",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["ingest"].update({"handler_wall_ns_total": 161}),
            id="handler-total-exceeds-call-count-times-maximum",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["delivery_delay"].update(
                {"samples": ingress["cost"]["delivery_delay"]["samples"] - 1}
            ),
            id="delivery-samples-inconsistent",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["snapshot"].update(
                {"build_wall_ns_max": ingress["cost"]["snapshot"]["build_wall_ns_total"] + 1}
            ),
            id="maximum-exceeds-total",
        ),
        pytest.param(
            lambda ingress: ingress["cost"]["snapshot"].update({"build_wall_ns_total": 101}),
            id="single-snapshot-total-differs-from-maximum",
        ),
        pytest.param(
            _make_zero_delivery_samples_with_nonzero_timing,
            id="zero-sample-delivery-has-nonzero-timing",
        ),
        pytest.param(
            lambda ingress: ingress["cost"].update({"saturated": True}),
            id="saturated-without-capped-counter",
        ),
    ],
)
def test_terminal_adapter_rejects_invalid_optional_ingress_cost(mutate) -> None:
    identity = _identity()
    snapshot = _snapshot(identity)
    snapshot["ingress"]["cost"] = _ingress_cost(snapshot)
    mutate(snapshot["ingress"])

    with pytest.raises(publication.WorkflowProgressPilotError) as rejected:
        publication.prepare_terminal_workflow_progress_publication(
            identity,
            snapshot,
            plan_fingerprint=FINGERPRINT,
            selected_strategy="dynamic_tasks",
            reporting_policy="full",
            detail_days=7,
        )

    assert rejected.value.reason is publication.WorkflowProgressPilotReason.INVALID_SNAPSHOT


def test_terminal_adapter_preserves_a_mid_graph_failure() -> None:
    identity = _identity()

    prepared = publication.prepare_terminal_workflow_progress_publication(
        identity,
        _failed_snapshot(identity),
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        reporting_policy="full",
        detail_days=7,
    )

    details = {record.node_id: json.loads(record.payload) for record in prepared.detail.records}
    assert prepared.summary["state"] == "FAILED"
    assert prepared.summary["node_counts"]["failed"] == 1
    assert details["0.1"]["state"] == "FAILED"
    assert details["0.1"]["error"] == "middle branch failed"
    assert details["0.1"]["recent_events"][-1]["event"] == "FAILED"


@pytest.mark.parametrize(
    ("outcome", "expected_percent"),
    [("SUCCEEDED", 100.0), ("FAILED", 0.0)],
)
def test_terminal_only_adapter_reports_declared_counts_without_discovery(
    outcome: str,
    expected_percent: float,
) -> None:
    identity = _identity()

    summary = publication.prepare_terminal_only_workflow_progress_summary(
        identity,
        plan_fingerprint=FINGERPRINT,
        selected_strategy="dynamic_tasks",
        declared_node_count=12,
        declared_edge_count=11,
        outcome=outcome,
        started_at=1_785_365_100.0,
        finished_at=1_785_365_104.0,
        detail_days=7,
    )

    assert summary["reporting_policy"] == "terminal_only"
    assert summary["state"] == outcome
    assert summary["node_counts"] == {
        "declared": 12,
        "discovered": 0,
        "retained_topology": 0,
        "retained_detail": 0,
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert summary["edge_counts"] == {
        "declared": 11,
        "discovered": 0,
        "retained_topology": 0,
    }
    assert summary["progress_percent"] == expected_percent
    assert summary["topology_version"] is None
    assert summary["detail_revision"] is None
    assert summary["detail"] == {
        "availability": "OMITTED_BY_POLICY",
        "complete": False,
        "truncation_reasons": [],
    }
    assert summary["storage"] == {"kind": "database", "manifest_id": None}
    assert summary["retention"] == {
        "detail_days": 7,
        "detail_expires_at": None,
    }
    assert summary["terminal"]["outcome"] == outcome
    assert summary["terminal"]["finished_at"] == summary["timestamps"]["finished_at"]
    assert (
        deserialize_workflow_progress_summary(
            json.dumps(summary),
            expected_identity=identity,
        )["state"]
        == outcome
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda value: value["ingress"].update(
                {
                    "rejected": 1,
                    "rejected_by_reason": {
                        **value["ingress"]["rejected_by_reason"],
                        "node_limit": 1,
                    },
                }
            ),
            publication.WorkflowProgressPilotReason.INGRESS_REJECTED,
        ),
        (
            lambda value: value["ingress"].update({"truncated": 1}),
            publication.WorkflowProgressPilotReason.INGRESS_TRUNCATED,
        ),
        (
            lambda value: value.update({"completed_nodes": 1}),
            publication.WorkflowProgressPilotReason.INVALID_SNAPSHOT,
        ),
        (
            lambda value: value["graph"]["edges"].append({"source": "missing", "target": "0.1"}),
            publication.WorkflowProgressPilotReason.INVALID_SNAPSHOT,
        ),
        (
            lambda value: value["ingress"].update(
                {"retained_bytes": value["ingress"]["retained_bytes"] + 1}
            ),
            publication.WorkflowProgressPilotReason.INVALID_SNAPSHOT,
        ),
    ],
)
def test_terminal_adapter_fails_closed_on_incomplete_evidence(
    mutate,
    reason: publication.WorkflowProgressPilotReason,
) -> None:
    identity = _identity()
    snapshot = _snapshot(identity)
    mutate(snapshot)

    with pytest.raises(publication.WorkflowProgressPilotError) as rejected:
        publication.prepare_terminal_workflow_progress_publication(
            identity,
            snapshot,
            plan_fingerprint=FINGERPRINT,
            selected_strategy="dynamic_tasks",
            reporting_policy="full",
            detail_days=7,
        )

    assert rejected.value.reason is reason


@pytest.mark.parametrize(
    ("ingress_field", "reason"),
    [
        (
            "rejected",
            publication.WorkflowProgressPilotReason.INGRESS_REJECTED,
        ),
        (
            "truncated",
            publication.WorkflowProgressPilotReason.INGRESS_TRUNCATED,
        ),
    ],
)
def test_ingress_failure_reason_precedes_incomplete_terminal_evidence(
    ingress_field: str,
    reason: publication.WorkflowProgressPilotReason,
) -> None:
    identity = _identity()
    snapshot = _snapshot(identity)
    snapshot.update(
        {
            "state": "RUNNING",
            "completed_nodes": 0,
            "failed_nodes": 0,
            "pending_nodes": 2,
            "progress_percent": 0.0,
        }
    )
    snapshot["ingress"][ingress_field] = 1
    if ingress_field == "rejected":
        snapshot["ingress"]["rejected_by_reason"]["protocol_error"] = 1

    with pytest.raises(publication.WorkflowProgressPilotError) as rejected:
        publication.prepare_terminal_workflow_progress_publication(
            identity,
            snapshot,
            plan_fingerprint=FINGERPRINT,
            selected_strategy="dynamic_tasks",
            reporting_policy="full",
            detail_days=7,
        )

    assert rejected.value.reason is reason


def test_terminal_adapter_applies_the_same_strict_pilot_admission_profile() -> None:
    identity = _identity()
    strict = replace(
        publication.WORKFLOW_PROGRESS_SCHEMA_V3_PILOT_LIMITS,
        topology_node_max_items=1,
        detail_max_items=1,
    )

    with pytest.raises(publication.WorkflowProgressPilotError) as rejected:
        publication.prepare_terminal_workflow_progress_publication(
            identity,
            _snapshot(identity),
            plan_fingerprint=FINGERPRINT,
            selected_strategy="dynamic_tasks",
            reporting_policy="full",
            detail_days=7,
            limits=strict,
        )

    assert rejected.value.reason is publication.WorkflowProgressPilotReason.ADMISSION_LIMIT


@pytest.mark.django_db
def test_terminal_publication_uses_the_pinned_plan_and_real_bounded_readers() -> None:
    execution, identity = _execution()

    result = publication.publish_terminal_workflow_progress(
        identity,
        _snapshot(identity),
        detail_days=7,
    )

    assert result.accepted
    assert result.reason is publication.WorkflowProgressPilotReason.PUBLISHED
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is not None
    summary = deserialize_workflow_progress_summary(
        execution.workflow_progress_summary_json,
        expected_identity=identity,
    )
    assert summary["state"] == "SUCCEEDED"
    assert summary["detail"]["availability"] == "AVAILABLE"
    assert summary["storage"]["manifest_id"] is not None
    run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
    assert run_storage.detail_node_count == 2
    assert run_storage.detail_succeeded_count == 2
    assert (
        WorkflowProgressTopologyManifest.objects.get(run_storage=run_storage).slot
        == WorkflowProgressTopologySlot.CURRENT
    )
    assert WorkflowProgressNodeDetail.objects.filter(run_storage=run_storage).count() == 2

    public_summary = get_workflow_progress_summary(execution, authorize=_allow)
    nodes = list_workflow_topology_nodes(execution, authorize=_allow)
    edges = list_workflow_topology_edges(execution, authorize=_allow)
    details = list_workflow_node_details(execution, authorize=_allow)
    assert public_summary["availability"] == "AVAILABLE"
    assert nodes["returned_count"] == 2
    assert edges["returned_count"] == 1
    assert details["returned_count"] == 2


@pytest.mark.django_db
def test_failed_publication_discards_its_pending_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, identity = _execution()

    def fail_publication(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(
        publication,
        "persist_workflow_progress_publication",
        fail_publication,
    )

    result = publication.publish_terminal_workflow_progress(
        identity,
        _snapshot(identity),
        detail_days=7,
    )

    assert not result.accepted
    assert result.reason is publication.WorkflowProgressPilotReason.PUBLICATION_FAILED
    assert not WorkflowProgressTopologyManifest.objects.filter(
        run_storage__execution=execution
    ).exists()
    assert not WorkflowProgressTopologyPage.objects.filter(
        run_storage__execution=execution
    ).exists()
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_stale_and_unpinned_runs_never_stage_topology() -> None:
    execution, identity = _execution()
    stale_snapshot = _snapshot(identity)
    execution.state = TaskState.SUCCEEDED
    execution.save(update_fields=["state"])

    stale = publication.publish_terminal_workflow_progress(
        identity,
        stale_snapshot,
        detail_days=7,
    )

    assert stale.reason is publication.WorkflowProgressPilotReason.STALE_FENCE
    assert not WorkflowProgressRunStorage.objects.filter(execution=execution).exists()

    execution.state = TaskState.RUNNING
    execution.workflow_plan_selection = "{"
    execution.save(update_fields=["state", "workflow_plan_selection"])
    invalid = publication.publish_terminal_workflow_progress(
        identity,
        deepcopy(stale_snapshot),
        detail_days=7,
    )
    assert invalid.reason is publication.WorkflowProgressPilotReason.INVALID_SELECTION
    assert not WorkflowProgressRunStorage.objects.filter(execution=execution).exists()
