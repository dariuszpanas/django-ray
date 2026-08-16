"""Boundary and corruption coverage for workflow-progress protocol storage."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from django.db import IntegrityError as DjangoIntegrityError
from django.db.models import QuerySet
from django.utils import timezone

import django_ray.lifecycle as lifecycle
import django_ray.workflow_progress_storage as storage
from django_ray.models import (
    InputPayloadState,
    RayTaskExecution,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologySlot,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.summary import WorkflowProgressTruncationReason
from tests.workflow_progress_storage_helpers import (
    publish_initial_workflow,
    workflow_detail,
    workflow_node,
    workflow_node_id,
    workflow_summary,
)

RUN_ID = "00000000-0000-0000-0000-000000000226"


def _identity(
    *,
    task_execution_pk: int = 226,
    run_id: str = RUN_ID,
) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=task_execution_pk,
        attempt_number=2,
        execution_generation=3,
        run_id=run_id,
    )


def _execution(*, task_id: str = "workflow-storage-boundaries") -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=3,
        workflow_run_id=RUN_ID,
    )


def _execution_identity(execution: RayTaskExecution) -> WorkflowRunIdentity:
    assert execution.workflow_run_id is not None
    return WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=str(execution.workflow_run_id),
    )


def _node(node_id: str, *, kind: str = "task") -> dict[str, object]:
    value = workflow_node(node_id)
    value["kind"] = kind
    return value


def _event(
    index: int = 0,
    *,
    state: str = "RUNNING",
    label: str | None = None,
) -> dict[str, object]:
    return {
        "event": "STATE_CHANGE",
        "state": state,
        "label": label or f"event {index}",
        "timestamp": f"2026-07-20T12:00:{index % 60:02d}Z",
    }


def _detail_value(
    node_id: str,
    *,
    state: str = "PENDING",
    kind: str = "task",
) -> dict[str, object]:
    value = workflow_detail(node_id, state=state)
    if state == "SUCCEEDED":
        value["started_at"] = "2026-07-20T12:00:00Z"
        value["finished_at"] = "2026-07-20T12:00:02Z"
    elif state == "FAILED":
        value["started_at"] = "2026-07-20T12:00:00Z"
        value["finished_at"] = "2026-07-20T12:00:02Z"
        value["error"] = "node failed"
    if kind == "map":
        value["fanout"] = {
            "max_concurrency": 4,
            "max_items": 100,
            "submitted_items": 0,
            "completed_items": 0,
            "in_flight_items": 0,
            "input_exhausted": state == "SUCCEEDED",
        }
    return value


def _topology(
    identity: WorkflowRunIdentity,
    *,
    version: int = 1,
    node_ids: tuple[str, ...] = ("node-a", "node-b"),
    kinds: dict[str, str] | None = None,
    edges: tuple[tuple[str, str], ...] = (("node-a", "node-b"),),
) -> storage.PreparedWorkflowProgressTopology:
    node_kinds = kinds or {}
    return storage.prepare_workflow_progress_topology(
        identity,
        version,
        [_node(node_id, kind=node_kinds.get(node_id, "task")) for node_id in node_ids],
        [{"source": source, "target": target} for source, target in edges],
    )


def _prepared_detail(
    identity: WorkflowRunIdentity,
    node_id: str,
    *,
    state: str = "PENDING",
    kind: str = "task",
) -> storage.PreparedWorkflowProgressNodeDetail:
    return storage.prepare_workflow_progress_node_detail(
        _detail_value(node_id, state=state, kind=kind),
        identity=identity,
    )


def _page_with_payload(
    page: storage.PreparedWorkflowProgressTopologyPage,
    payload: bytes,
    *,
    item_count: int | None = None,
) -> storage.PreparedWorkflowProgressTopologyPage:
    return replace(
        page,
        payload=payload,
        digest=storage._digest(storage._PAGE_DOMAIN, payload),
        item_count=page.item_count if item_count is None else item_count,
        encoded_bytes=len(payload),
        decoded_bytes=len(payload),
    )


def _verified_topology(
    *,
    node_count: int = 2,
    edge_count: int = 0,
) -> storage.VerifiedWorkflowProgressTopology:
    node_ids = tuple(f"node-{index}" for index in range(node_count))
    return storage.VerifiedWorkflowProgressTopology(
        manifest_id="00000000-0000-0000-0000-000000000001",
        run_storage_id=1,
        topology_version=1,
        slot=WorkflowProgressTopologySlot.CURRENT,
        node_ids=frozenset(node_ids),
        node_kinds=tuple((node_id, "task") for node_id in node_ids),
        edges=(),
        node_count=node_count,
        edge_count=edge_count,
        encoded_bytes=1,
        decoded_bytes=1,
        truncation_reasons=(),
    )


def _verified_prepared_topology(
    topology: storage.PreparedWorkflowProgressTopology,
    *,
    manifest_id: str,
    run_storage_id: int,
    slot: str,
) -> storage.VerifiedWorkflowProgressTopology:
    return storage.VerifiedWorkflowProgressTopology(
        manifest_id=manifest_id,
        run_storage_id=run_storage_id,
        topology_version=topology.topology_version,
        slot=slot,
        node_ids=topology.node_ids,
        node_kinds=topology.node_kinds,
        edges=topology.edges,
        node_count=topology.retained_node_count,
        edge_count=topology.retained_edge_count,
        encoded_bytes=topology.encoded_bytes,
        decoded_bytes=topology.decoded_bytes,
        truncation_reasons=topology.truncation_reasons,
        map_node_ids=topology.map_node_ids,
    )


def test_low_level_protocol_helpers_reject_noncanonical_and_unbounded_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="record byte budget"):
        storage._MetadataBudget(remaining_bytes=0).consume(1, "metadata")

    expression = storage._BlobOctetLength("payload")
    monkeypatch.setattr(expression, "as_sql", lambda *args, **kwargs: ("LENGTHB(x)", []))
    assert expression.as_oracle(None, None) == ("LENGTHB(x)", [])

    with pytest.raises(storage.WorkflowProgressStorageError, match="reasons are invalid"):
        storage._encode_truncation_reasons(["unknown"])
    with pytest.raises(storage.WorkflowProgressStorageError, match="reasons are invalid"):
        storage._decode_truncation_reasons(1, stored=False)
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="reasons are invalid"):
        storage._decode_truncation_reasons("unknown", stored=True)
    with pytest.raises(storage.WorkflowProgressStorageError, match="not canonical"):
        storage._decode_truncation_reasons("reporting_policy,detail_count_limit", stored=False)

    assert storage._as_bytes(memoryview(b"value"), "payload") == b"value"
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="not binary"):
        storage._as_bytes("value", "payload")
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="not valid JSON"):
        storage._decode_canonical_payload(b"\xff", "payload")
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="not canonical JSON"):
        storage._decode_canonical_payload(b'{"value":NaN}', "payload")
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="not canonical JSON"):
        storage._decode_canonical_payload(b'{ "value": 1 }', "payload")
    with pytest.raises(storage.WorkflowProgressStorageError, match="not canonical JSON"):
        storage._canonical_json_bytes(object())
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be text"):
        storage._utf8_bytes(1, "value")

    assert storage._bounded_text(None, "value", max_bytes=1, nullable=True) is None
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be text"):
        storage._bounded_text(1, "value", max_bytes=1)
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="between 1 and 1"):
        storage._bounded_text("", "value", max_bytes=1)
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="between 1 and 1"):
        storage._bounded_text("ab", "value", max_bytes=1)
    assert storage._bounded_text("ok", "value", max_bytes=2) == "ok"
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be text"):
        storage._bounded_identity_text(1, "identity", max_bytes=1)
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be text"):
        storage._bounded_identity_characters(1, "identity", max_characters=1)
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be text"):
        storage._bounded_redacted_text(1, "message", max_bytes=1)
    monkeypatch.setattr(storage, "redact_text", lambda value: value * 2)
    assert storage._bounded_redacted_text("ab", "message", max_bytes=3) == (
        storage._OMITTED_OVERSIZED,
        True,
    )


def test_prepared_topology_capabilities_reject_foreign_values_and_register_valid_copies() -> None:
    topology = _topology(_identity(), node_ids=("node-a",), edges=())
    copied = replace(topology, _capability_token=None)
    assert not storage._prepared_topology_capability_matches(copied)
    with pytest.raises(storage.WorkflowProgressStorageError, match="package-owned evidence type"):
        storage._validate_prepared_topology_reference(object())
    storage._validate_prepared_topology_reference(copied)
    assert storage._prepared_topology_capability_matches(copied)


def test_stored_detail_requires_authenticated_truncation_evidence() -> None:
    identity = _identity()
    durable = _detail_value("node-a")
    durable["truncated"] = 1
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be a boolean"):
        storage._prepare_workflow_progress_node_detail(
            durable,
            identity=identity,
            allow_stored_truncation=True,
        )

    deterministically_truncated = _detail_value("node-a", state="FAILED")
    deterministically_truncated["error"] = "x" * (storage.WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES + 1)
    deterministically_truncated["truncated"] = False
    with pytest.raises(storage.WorkflowProgressStorageError, match="suppresses deterministic"):
        storage._prepare_workflow_progress_node_detail(
            deterministically_truncated,
            identity=identity,
            allow_stored_truncation=True,
        )


def test_identity_number_and_scalar_boundaries_are_defensive() -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="WorkflowRunIdentity"):
        storage._validate_run_identity(object())
    with pytest.raises(storage.WorkflowProgressStorageError, match="outside the durable range"):
        storage._validate_run_identity(replace(_identity(), task_execution_pk=0))
    with pytest.raises(storage.WorkflowProgressStorageError, match="canonical UUID"):
        storage._validate_run_identity(replace(_identity(), run_id=1))
    with pytest.raises(storage.WorkflowProgressStorageError, match="canonical UUID"):
        storage._validate_run_identity(
            replace(_identity(), run_id="AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA")
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="canonical UUID"):
        storage._validate_run_identity(replace(_identity(), run_id="not-a-uuid"))
    with pytest.raises(storage.WorkflowProgressStorageError, match="finite number"):
        storage._finite_number(10**400, "number")
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be an integer"):
        storage._bounded_int(True, "count")
    with pytest.raises(storage.WorkflowProgressStorageError, match="durable range"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            storage.WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER + 1,
            (),
            (),
        )


def test_metadata_timestamp_and_identifier_boundaries_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert storage._normalize_metadata(None, "metadata") == (None, False)
    assert storage._normalize_metadata(True, "metadata") == (True, False)
    with pytest.raises(storage.WorkflowProgressStorageError, match="outside the durable range"):
        storage._normalize_metadata(1 << 64, "metadata")
    oversized, truncated = storage._normalize_metadata(
        "x" * (storage.WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES + 1),
        "metadata",
    )
    assert (oversized, truncated) == (storage._OMITTED_OVERSIZED, True)
    with pytest.raises(storage.WorkflowProgressStorageError, match="object keys must be text"):
        storage._normalize_metadata({1: "value"}, "metadata")
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="oversized key"):
        storage._normalize_metadata({"": "value"}, "metadata")
    with pytest.raises(storage.WorkflowProgressStorageError, match="only JSON values"):
        storage._normalize_metadata({1, 2}, "metadata")
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be an object"):
        storage._normalize_metadata_object([], "metadata")

    assert storage._timestamp(0, "timestamp") == "1970-01-01T00:00:00Z"
    with pytest.raises(storage.WorkflowProgressStorageError, match="outside the timestamp range"):
        storage._timestamp(1e20, "timestamp")
    with pytest.raises(storage.WorkflowProgressStorageError, match="bounded UTC timestamp"):
        storage._timestamp(1j, "timestamp")
    with pytest.raises(storage.WorkflowProgressStorageError, match="bounded UTC timestamp"):
        storage._timestamp("not-a-timeZ", "timestamp")
    with pytest.raises(storage.WorkflowProgressStorageError, match="canonical UTC encoding"):
        storage._timestamp("2026-07-20T12:00:00.000000Z", "timestamp")
    assert storage._identifier(None, "identifier", nullable=True) is None
    with pytest.raises(storage.WorkflowProgressStorageError, match="protocol identifier"):
        storage._identifier("contains spaces", "identifier")

    invocation = _identity().as_dict()
    invocation["invocation_id"] = "invocation-1"
    invocation["schema_version"] = 2
    with pytest.raises(storage.WorkflowProgressStorageError, match="schema_version"):
        storage._normalize_invocation_identity(invocation, identity=_identity())

    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES", 2)
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="byte limit"):
        storage._assigned_resources({"CPU": 1}, "resources")


def test_metrics_and_resource_shapes_enforce_scalar_protocol_limits() -> None:
    too_many = {f"metric-{index}": index for index in range(33)}
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="metrics item limit"):
        storage._metrics(too_many, "metrics")
    with pytest.raises(storage.WorkflowProgressStorageError, match="keys must be text"):
        storage._metrics({1: 1}, "metrics")
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="oversized key"):
        storage._metrics({"x" * 65: 1}, "metrics")
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be finite"):
        storage._metrics({"latency": float("inf")}, "metrics")
    normalized, truncated = storage._metrics({"message": "x" * 257}, "metrics")
    assert normalized == {"message": storage._OMITTED_OVERSIZED}
    assert truncated
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be a scalar"):
        storage._metrics({"nested": {}}, "metrics")

    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="resource item limit"):
        storage._assigned_resources(too_many, "resources")
    with pytest.raises(storage.WorkflowProgressStorageError, match="keys must be text"):
        storage._assigned_resources({1: 1}, "resources")
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="oversized key"):
        storage._assigned_resources({"x" * 65: 1}, "resources")
    with pytest.raises(storage.WorkflowProgressStorageError, match="sensitive-looking key"):
        storage._assigned_resources({"api_key": 1}, "resources")


def test_topology_normalization_omits_indivisible_records_and_tracks_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    with pytest.raises(storage.WorkflowProgressStorageError, match="positive integer"):
        storage.prepare_workflow_progress_topology(identity, 0, (), ())
    with pytest.raises(storage.WorkflowProgressStorageError, match="kind is unsupported"):
        storage.prepare_workflow_progress_topology(
            identity,
            1,
            [_node("node-a", kind="actor")],
            (),
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="duplicate node_id"):
        storage.prepare_workflow_progress_topology(
            identity,
            1,
            [_node("node-a"), _node("node-a")],
            (),
        )

    oversized_node = _node("x" * (storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES + 1))
    omitted_node = storage.prepare_workflow_progress_topology(identity, 1, [oversized_node], ())
    assert omitted_node.retained_node_count == 0
    assert WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT in omitted_node.truncation_reasons

    truncated_node = _node("node-a")
    truncated_node["label"] = "x" * (storage.WORKFLOW_PROGRESS_LABEL_MAX_BYTES + 1)
    truncated = storage.prepare_workflow_progress_topology(identity, 1, [truncated_node], ())
    assert WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT in truncated.truncation_reasons

    omitted_edge = storage.prepare_workflow_progress_topology(
        identity,
        1,
        [_node("node-a")],
        [{"source": "x" * (storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES + 1), "target": "node-a"}],
    )
    assert omitted_edge.observed_edge_count == 1
    assert omitted_edge.retained_edge_count == 0

    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES", 100)
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="topology node exceeds"):
        storage._normalize_topology_node(_node("node-a"))
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES", 10)
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="topology edge exceeds"):
        storage._normalize_topology_edge({"source": "node-a", "target": "node-b"})


def test_node_detail_boundary_failures_cover_lifecycle_events_and_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    with pytest.raises(storage.WorkflowProgressStorageError, match="state is unsupported"):
        storage.prepare_workflow_progress_node_detail(
            {**_detail_value("node-a"), "state": "LOST"},
            identity=identity,
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="recent_events must be a list"):
        storage.prepare_workflow_progress_node_detail(
            {**_detail_value("node-a"), "recent_events": ()},
            identity=identity,
        )
    many_events = {**_detail_value("node-a"), "recent_events": [_event(i) for i in range(33)]}
    assert storage.prepare_workflow_progress_node_detail(
        many_events,
        identity=identity,
    ).truncated

    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_EVENT_MAX_ENCODED_BYTES", 1)
    omitted_event = storage.prepare_workflow_progress_node_detail(
        {**_detail_value("node-a"), "recent_events": [_event()]},
        identity=identity,
    )
    assert omitted_event.event_count == 0
    assert omitted_event.truncated

    bad_fanout = _detail_value("node-a", kind="map")
    assert isinstance(bad_fanout["fanout"], dict)
    bad_fanout["fanout"]["input_exhausted"] = 1
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be boolean"):
        storage.prepare_workflow_progress_node_detail(bad_fanout, identity=identity)


@pytest.mark.parametrize(
    ("state", "updates", "message"),
    [
        ("RUNNING", {"started_at": None}, "running node detail"),
        ("SUCCEEDED", {"finished_at": None}, "successful node detail"),
        ("FAILED", {"error": None}, "failed node detail"),
        (
            "SUCCEEDED",
            {
                "started_at": "2026-07-20T12:00:03Z",
                "finished_at": "2026-07-20T12:00:02Z",
            },
            "precedes",
        ),
    ],
)
def test_node_detail_rejects_inconsistent_lifecycle_shapes(
    state: str,
    updates: dict[str, object],
    message: str,
) -> None:
    value = _detail_value("node-a", state=state)
    value.update(updates)
    with pytest.raises(storage.WorkflowProgressStorageError, match=message):
        storage.prepare_workflow_progress_node_detail(value, identity=_identity())


@pytest.mark.parametrize(
    ("state", "updated_at", "percent", "message"),
    [
        ("RUNNING", "2026-07-20T11:59:59Z", 50.0, "predates"),
        ("SUCCEEDED", "2026-07-20T12:00:03Z", 100.0, "follows"),
        ("SUCCEEDED", "2026-07-20T12:00:01Z", 50.0, "must be complete"),
    ],
)
def test_node_progress_timestamp_and_completion_must_match_lifecycle(
    state: str,
    updated_at: str,
    percent: float,
    message: str,
) -> None:
    value = _detail_value("node-a", state=state)
    value["progress"] = {
        "current": percent,
        "total": 100.0,
        "percent": percent,
        "message": None,
        "metrics": {},
        "updated_at": updated_at,
    }
    with pytest.raises(storage.WorkflowProgressStorageError, match=message):
        storage.prepare_workflow_progress_node_detail(value, identity=_identity())


def test_successful_map_detail_requires_a_fully_drained_fanout() -> None:
    value = _detail_value("node-a", state="SUCCEEDED", kind="map")
    assert isinstance(value["fanout"], dict)
    value["fanout"].update(
        submitted_items=1,
        completed_items=0,
        in_flight_items=1,
        input_exhausted=False,
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="fully drained"):
        storage.prepare_workflow_progress_node_detail(value, identity=_identity())


def test_initial_detail_rejects_policy_and_duplicate_records_and_tracks_omissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(_identity(), node_ids=("node-a",), edges=())
    with pytest.raises(storage.WorkflowProgressStorageError, match="reporting policy"):
        storage.prepare_workflow_progress_detail((), topology=topology, reporting_policy="unknown")
    with pytest.raises(storage.WorkflowProgressStorageError, match="duplicate node_id"):
        storage.prepare_workflow_progress_detail(
            [_detail_value("node-a"), _detail_value("node-a")],
            topology=topology,
        )

    truncated_value = _detail_value("node-a", state="FAILED")
    truncated_value["error"] = "x" * (storage.WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES + 1)
    truncated = storage.prepare_workflow_progress_detail([truncated_value], topology=topology)
    assert WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT in truncated.truncation_reasons

    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES", 100)
    omitted = storage.prepare_workflow_progress_detail(
        [_detail_value("node-a")],
        topology=topology,
    )
    assert omitted.records == ()
    assert WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT in omitted.truncation_reasons


def test_prepared_topology_verifier_rejects_mutated_page_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(_identity())
    node_page, edge_page = topology.pages

    invalid_cases = [
        (replace(topology, topology_version=0), "version"),
        (replace(topology, manifest_digest="0" * 64), "manifest digest"),
        (replace(topology, pages=(edge_page, node_page)), "canonically ordered"),
        (
            replace(topology, pages=(replace(node_page, page_index=1), edge_page)),
            "indexes are not contiguous",
        ),
        (
            replace(topology, pages=(replace(node_page, item_count=0), edge_page)),
            "item count",
        ),
        (
            replace(topology, pages=(replace(node_page, encoded_bytes=0), edge_page)),
            "page sizes",
        ),
        (
            replace(topology, pages=(replace(node_page, digest="0" * 64), edge_page)),
            "page digest",
        ),
    ]
    for candidate, message in invalid_cases:
        with pytest.raises(storage.WorkflowProgressStorageError, match=message):
            storage._verify_prepared_topology(candidate)

    invalid_json_page = _page_with_payload(node_page, b"not-json")
    with pytest.raises(storage.WorkflowProgressStorageError, match="not valid JSON"):
        storage._verify_prepared_topology(replace(topology, pages=(invalid_json_page, edge_page)))
    empty_page = _page_with_payload(node_page, b"{}")
    with pytest.raises(storage.WorkflowProgressStorageError, match="page envelope"):
        storage._verify_prepared_topology(replace(topology, pages=(empty_page, edge_page)))

    oversized_manifest = b"x" * (storage.WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES + 1)
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="manifest is oversized"):
        storage._verify_prepared_topology(replace(topology, manifest_payload=oversized_manifest))
    with pytest.raises(storage.WorkflowProgressStorageError, match="collection is invalid"):
        storage._verify_prepared_topology(
            replace(topology, pages=(replace(node_page, collection="UNKNOWN"),))
        )

    nonnormalized_value = json.loads(node_page.payload)
    nonnormalized_value["records"][0]["label"] = "x" * (
        storage.WORKFLOW_PROGRESS_LABEL_MAX_BYTES + 1
    )
    nonnormalized_payload = storage._canonical_json_bytes(nonnormalized_value)
    nonnormalized_page = _page_with_payload(node_page, nonnormalized_payload)
    with pytest.raises(storage.WorkflowProgressStorageError, match="node is not normalized"):
        storage._verify_prepared_topology(replace(topology, pages=(nonnormalized_page, edge_page)))

    page_value = json.loads(node_page.payload)
    page_value["records"].reverse()
    reordered_payload = storage._canonical_json_bytes(page_value)
    reordered_page = _page_with_payload(node_page, reordered_payload)
    with pytest.raises(storage.WorkflowProgressStorageError, match="node order"):
        storage._verify_prepared_topology(replace(topology, pages=(reordered_page, edge_page)))

    edge_value = json.loads(edge_page.payload)
    edge_value["records"][0]["source"] = "missing"
    invalid_edge_payload = storage._canonical_json_bytes(edge_value)
    invalid_edge_page = _page_with_payload(edge_page, invalid_edge_payload)
    with pytest.raises(storage.WorkflowProgressStorageError, match="omitted node"):
        storage._verify_prepared_topology(replace(topology, pages=(node_page, invalid_edge_page)))

    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
        topology.encoded_bytes - 1,
    )
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="encoded limit"):
        storage._verify_prepared_topology(topology)


def test_prepared_topology_verifier_rejects_manifest_and_summary_evidence() -> None:
    topology = _topology(_identity())
    malformed_payload = b"not-json"
    with pytest.raises(storage.WorkflowProgressStorageError, match="not valid JSON"):
        storage._verify_prepared_topology(
            replace(
                topology,
                manifest_payload=malformed_payload,
                manifest_digest=storage._digest(storage._MANIFEST_DOMAIN, malformed_payload),
            )
        )
    invalid_payload = b"[]"
    with pytest.raises(storage.WorkflowProgressStorageError, match="manifest envelope"):
        storage._verify_prepared_topology(
            replace(
                topology,
                manifest_payload=invalid_payload,
                manifest_digest=storage._digest(storage._MANIFEST_DOMAIN, invalid_payload),
            )
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="evidence is inconsistent"):
        storage._verify_prepared_topology(replace(topology, retained_node_count=1))
    manifest = json.loads(topology.manifest_payload)
    manifest["truncation_reasons"] = ["unknown"]
    invalid_reason_payload = storage._canonical_json_bytes(manifest)
    size_delta = len(invalid_reason_payload) - len(topology.manifest_payload)
    with pytest.raises(storage.WorkflowProgressStorageError, match="truncation reasons"):
        storage._verify_prepared_topology(
            replace(
                topology,
                manifest_payload=invalid_reason_payload,
                manifest_digest=storage._digest(storage._MANIFEST_DOMAIN, invalid_reason_payload),
                encoded_bytes=topology.encoded_bytes + size_delta,
                decoded_bytes=topology.decoded_bytes + size_delta,
                truncation_reasons=("unknown",),
            )
        )


def test_prepared_topology_capability_revalidates_a_forged_copy() -> None:
    topology = _topology(_identity(), node_ids=("node-a",), edges=())
    storage._validate_prepared_topology_reference(topology)

    forged = replace(
        topology,
        node_kinds=(("node-a", "map"),),
        map_node_ids=frozenset({"node-a"}),
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="evidence is inconsistent"):
        storage._validate_prepared_topology_reference(forged)


def test_prepared_topology_verifier_rejects_noncanonical_edge_order_and_decoded_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(
        _identity(),
        node_ids=("node-a", "node-b", "node-c"),
        edges=(("node-a", "node-b"), ("node-a", "node-c")),
    )
    node_page, edge_page = topology.pages
    edge_value = json.loads(edge_page.payload)
    edge_value["records"].reverse()
    reordered_edge = _page_with_payload(
        edge_page,
        storage._canonical_json_bytes(edge_value),
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="edge order"):
        storage._verify_prepared_topology(replace(topology, pages=(node_page, reordered_edge)))

    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES",
        topology.decoded_bytes - 1,
    )
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="decoded limit"):
        storage._verify_prepared_topology(topology)


def test_prepared_detail_verifier_rejects_mutated_payload_and_aggregate_evidence() -> None:
    identity = _identity()
    record = _prepared_detail(identity, "node-a")
    with pytest.raises(storage.WorkflowProgressStorageError, match="evidence is inconsistent"):
        storage._verify_prepared_node_detail(replace(record, event_count=33), identity=identity)

    invalid_payload = b"not-json"
    invalid_record = replace(
        record,
        payload=invalid_payload,
        digest=storage._digest(storage._DETAIL_DOMAIN, invalid_payload),
        encoded_bytes=len(invalid_payload),
        decoded_bytes=len(invalid_payload),
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="not valid JSON"):
        storage._verify_prepared_node_detail(invalid_record, identity=identity)

    list_payload = b"[]"
    list_record = replace(
        record,
        payload=list_payload,
        digest=storage._digest(storage._DETAIL_DOMAIN, list_payload),
        encoded_bytes=len(list_payload),
        decoded_bytes=len(list_payload),
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be an object"):
        storage._verify_prepared_node_detail(list_record, identity=identity)
    with pytest.raises(storage.WorkflowProgressStorageError, match="not normalized"):
        storage._verify_prepared_node_detail(replace(record, state="RUNNING"), identity=identity)

    aggregate = storage.PreparedWorkflowProgressDetail(
        records=(record,),
        observed_count=1,
        encoded_bytes=record.encoded_bytes,
        decoded_bytes=record.decoded_bytes,
        truncation_reasons=(),
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="evidence is inconsistent"):
        storage._verify_prepared_detail(replace(aggregate, observed_count=0), identity=identity)
    duplicate = replace(
        aggregate,
        records=(record, record),
        observed_count=2,
        encoded_bytes=record.encoded_bytes * 2,
        decoded_bytes=record.decoded_bytes * 2,
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="repeats a node"):
        storage._verify_prepared_detail(duplicate, identity=identity)


def test_manifest_identifier_and_descriptor_bounds_reject_corrupt_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="canonical UUID"):
        storage._manifest_uuid("not-a-uuid")
    with pytest.raises(storage.WorkflowProgressStorageError, match="canonical UUID"):
        storage._manifest_uuid("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA")
    assert storage._manifest_uuid(storage.UUID(RUN_ID)) == RUN_ID

    topology = _topology(_identity())
    manifest = json.loads(topology.manifest_payload)
    row = {
        "topology_version": topology.topology_version,
        "node_count": topology.retained_node_count,
        "edge_count": topology.retained_edge_count,
        "node_page_count": 1,
        "edge_page_count": 1,
        "encoded_bytes": topology.encoded_bytes,
        "decoded_bytes": topology.decoded_bytes,
        "truncation_reasons": storage._encode_truncation_reasons(topology.truncation_reasons),
    }
    payload_octets = len(topology.manifest_payload)

    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="envelope"):
        storage._bounded_manifest_descriptors(
            {},
            row,
            identity=topology.identity,
            payload_octets=payload_octets,
        )
    invalid_row = {**row, "node_page_count": -1}
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="relational metadata"):
        storage._bounded_manifest_descriptors(
            manifest,
            invalid_row,
            identity=topology.identity,
            payload_octets=payload_octets,
        )

    invalid_descriptor_manifest = deepcopy(manifest)
    invalid_descriptor_manifest["pages"][0]["page_index"] = 3
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="page descriptor"):
        storage._bounded_manifest_descriptors(
            invalid_descriptor_manifest,
            row,
            identity=topology.identity,
            payload_octets=payload_octets,
        )

    non_object_descriptor = deepcopy(manifest)
    non_object_descriptor["pages"][0] = []
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="page descriptor"):
        storage._bounded_manifest_descriptors(
            non_object_descriptor,
            row,
            identity=topology.identity,
            payload_octets=payload_octets,
        )

    mismatched_row = {**row, "node_page_count": 2}
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="bounds conflict"):
        storage._bounded_manifest_descriptors(
            manifest,
            mismatched_row,
            identity=topology.identity,
            payload_octets=payload_octets,
        )

    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
        topology.encoded_bytes - 1,
    )
    aggregate_row = {**row, "encoded_bytes": topology.encoded_bytes - 1}
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="aggregate limits"):
        storage._bounded_manifest_descriptors(
            manifest,
            aggregate_row,
            identity=topology.identity,
            payload_octets=payload_octets,
        )


@pytest.mark.django_db
def test_manifest_verifier_rejects_an_expected_identity_from_another_run() -> None:
    published = publish_initial_workflow(1, case_id=226)
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="another run"):
        storage.verify_workflow_progress_topology_manifest(
            published.manifest_id,
            expected_identity=replace(published.identity, execution_generation=2),
        )


@pytest.mark.django_db
def test_manifest_and_page_verifiers_report_missing_or_oversized_storage() -> None:
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="manifest is missing"):
        storage.verify_workflow_progress_topology_manifest(RUN_ID)

    execution = _execution(task_id="stored-page-bounds")
    topology = _topology(_execution_identity(execution), node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    manifest = WorkflowProgressTopologyManifest.objects.get(pk=manifest_id)
    page = manifest.page_links.get().page
    assert not storage._stored_page_matches(
        page.pk + 10_000,
        topology.pages[0],
        run_storage_id=manifest.run_storage_id,
        using="default",
    )
    page.payload = b"x" * (storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES + 1)
    page.save(update_fields=["payload"])
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="page is oversized"):
        storage._stored_page_matches(
            page.pk,
            topology.pages[0],
            run_storage_id=manifest.run_storage_id,
            using="default",
        )


@pytest.mark.django_db
def test_staging_maps_post_write_verification_failure_to_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution(task_id="stage-post-write-verification")
    topology = _topology(_execution_identity(execution))
    monkeypatch.setattr(storage, "_stored_manifest_matches_prepared", lambda *args, **kwargs: False)
    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError, match="post-write verification"
    ):
        storage.stage_workflow_progress_topology(topology)


@pytest.mark.django_db
@pytest.mark.parametrize("raced_model", ["page", "manifest"])
def test_staging_maps_database_creation_races_to_protocol_errors(
    monkeypatch: pytest.MonkeyPatch,
    raced_model: str,
) -> None:
    execution = _execution(task_id=f"stage-{raced_model}-race")
    topology = _topology(_execution_identity(execution), node_ids=("node-a",), edges=())
    original_create = QuerySet.create

    def raced_create(queryset: QuerySet, **kwargs: Any) -> Any:
        model = (
            storage.WorkflowProgressTopologyPage
            if raced_model == "page"
            else storage.WorkflowProgressTopologyManifest
        )
        if queryset.model is model:
            raise DjangoIntegrityError("simulated concurrent insert")
        return original_create(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, "create", raced_create)
    expected_error = (
        storage.WorkflowProgressStorageIntegrityError
        if raced_model == "page"
        else storage.WorkflowProgressStorageConflictError
    )
    with pytest.raises(expected_error, match="concurrent topology"):
        storage.stage_workflow_progress_topology(topology)


@pytest.mark.django_db
def test_discard_returns_false_when_the_exact_run_storage_does_not_exist() -> None:
    assert not storage.discard_workflow_progress_topology_candidate(
        _identity(task_execution_pk=999)
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "corruption",
    [
        "oversized",
        "digest",
        "non_object",
        "invalid_protocol",
        "not_normalized",
        "truncated_flag",
    ],
)
def test_touched_detail_reader_rejects_corrupt_durable_rows(corruption: str) -> None:
    published = publish_initial_workflow(1, case_id=227)
    node_id = workflow_node_id(0)
    row = WorkflowProgressNodeDetail.objects.get(run_storage__execution=published.execution)
    updates: dict[str, object]
    if corruption == "oversized":
        updates = {"payload": b"x" * (storage.WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES + 1)}
        message = "oversized"
    elif corruption == "digest":
        updates = {"digest": "0" * 64}
        message = "metadata is invalid"
    elif corruption == "non_object":
        payload = b"[]"
        updates = {
            "payload": payload,
            "digest": storage._digest(storage._DETAIL_DOMAIN, payload),
            "encoded_bytes": len(payload),
            "decoded_bytes": len(payload),
        }
        message = "must be an object"
    elif corruption == "invalid_protocol":
        value = json.loads(bytes(row.payload))
        value["state"] = "LOST"
        payload = storage._canonical_json_bytes(value)
        updates = {
            "payload": payload,
            "digest": storage._digest(storage._DETAIL_DOMAIN, payload),
            "encoded_bytes": len(payload),
            "decoded_bytes": len(payload),
        }
        message = "failed protocol validation"
    elif corruption == "not_normalized":
        updates = {"state": "RUNNING"}
        message = "not normalized"
    else:
        updates = {"truncated": not row.truncated}
        message = "not normalized"
    WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(**updates)
    run_storage = WorkflowProgressRunStorage.objects.get(execution=published.execution)
    node_key = storage.hashlib.sha256(node_id.encode()).hexdigest()
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match=message):
        storage._verified_touched_node_rows(
            run_storage,
            node_ids={node_id},
            node_keys={node_key},
            identity=published.identity,
            using="default",
        )


def test_sparse_event_rebalancing_retains_only_the_newest_run_global_events() -> None:
    identity = _identity()
    old_value = _detail_value("node-old", state="RUNNING")
    old_value["recent_events"] = [_event(index) for index in range(32)]
    old_record = storage.prepare_workflow_progress_node_detail(old_value, identity=identity)
    new_value = _detail_value("node-new", state="RUNNING")
    new_value["recent_events"] = [
        {
            "event": "STATE_CHANGE",
            "state": "RUNNING",
            "label": "newest",
            "timestamp": "2026-07-20T12:01:00Z",
        }
    ]
    new_record = storage.prepare_workflow_progress_node_detail(new_value, identity=identity)
    rebalanced = storage._rebalance_sparse_recent_events(
        [new_record],
        {
            old_record.node_key: {
                "node_id": old_record.node_id,
                "event_count": old_record.event_count,
                "prepared": old_record,
            }
        },
        removal_ids=set(),
    )
    assert sum(record.event_count for record in rebalanced) == 32
    assert any(record.node_id == "node-new" for record in rebalanced)
    assert any(record.truncated for record in rebalanced if record.node_id == "node-old")


def test_sparse_event_rebalancing_rejects_non_object_prepared_payload() -> None:
    record = storage.PreparedWorkflowProgressNodeDetail(
        node_id="node-a",
        node_key="0" * 64,
        state="PENDING",
        invocation_id=None,
        payload=b"[]",
        digest="0" * 64,
        encoded_bytes=2,
        decoded_bytes=2,
        event_count=0,
        truncated=False,
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be an object"):
        storage._rebalance_sparse_recent_events([record], {}, removal_ids=set())


def test_storage_bound_summary_rejects_missing_and_conflicting_storage_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    topology = _verified_topology()
    summary = workflow_summary(identity, summary_revision=1, node_count=2, running_count=0)
    arguments = {
        "identity": identity,
        "topology": topology,
        "detail_revision": 1,
        "detail_node_count": 2,
        "detail_state_counts": {"PENDING": 2, "RUNNING": 0, "SUCCEEDED": 0, "FAILED": 0},
        "detail_truncated_count": 0,
        "storage_reasons": set(),
    }
    with pytest.raises(storage.WorkflowProgressStorageError, match="must be an object"):
        storage._storage_bound_summary([], **arguments)
    with pytest.raises(storage.WorkflowProgressStorageError, match="missing storage-owned"):
        storage._storage_bound_summary({}, **arguments)

    invalid_counts = deepcopy(summary)
    invalid_counts["node_counts"]["discovered"] = "2"
    with pytest.raises(storage.WorkflowProgressStorageError, match="discovered counts"):
        storage._storage_bound_summary(invalid_counts, **arguments)
    with pytest.raises(storage.WorkflowProgressStorageConflictError, match="prepared evidence"):
        storage._storage_bound_summary(summary, observed_node_count=1, **arguments)

    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: {"WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS": 31},
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="integer from 0 through 30"):
        storage._storage_bound_summary(summary, **arguments)


def test_storage_bound_summary_infers_detail_omission_and_checks_states() -> None:
    identity = _identity()
    topology = _verified_topology()
    summary = workflow_summary(identity, summary_revision=1, node_count=2, running_count=0)
    base = {
        "identity": identity,
        "topology": topology,
        "detail_revision": 1,
        "detail_node_count": 1,
        "detail_state_counts": {"PENDING": 1, "RUNNING": 0, "SUCCEEDED": 0, "FAILED": 0},
        "detail_truncated_count": 0,
        "storage_reasons": set(),
    }
    normalized, _ = storage._storage_bound_summary(summary, **base)
    assert normalized["detail"]["truncation_reasons"] == ["detail_count_limit"]

    invalid_reasons = deepcopy(summary)
    invalid_reasons["detail"]["truncation_reasons"] = [1]
    with pytest.raises(storage.WorkflowProgressStorageError, match="list of strings"):
        storage._storage_bound_summary(invalid_reasons, **base)
    negative_states = deepcopy(summary)
    negative_states["node_counts"]["pending"] = -1
    with pytest.raises(storage.WorkflowProgressStorageError, match="non-negative integers"):
        storage._storage_bound_summary(negative_states, **base)
    with pytest.raises(
        storage.WorkflowProgressStorageConflictError, match="retained workflow node states"
    ):
        storage._storage_bound_summary(
            summary,
            **{
                **base,
                "detail_state_counts": {
                    "PENDING": 1,
                    "RUNNING": 1,
                    "SUCCEEDED": 0,
                    "FAILED": 0,
                },
            },
        )


def test_storage_bound_summary_rejects_terminal_and_serializer_conflicts() -> None:
    identity = _identity()
    topology = _verified_topology(node_count=0)
    summary = workflow_summary(identity, summary_revision=1, node_count=0, running_count=0)
    arguments = {
        "identity": identity,
        "topology": topology,
        "detail_revision": 1,
        "detail_node_count": 0,
        "detail_state_counts": {"PENDING": 0, "RUNNING": 0, "SUCCEEDED": 0, "FAILED": 0},
        "detail_truncated_count": 0,
        "storage_reasons": set(),
    }
    terminal = deepcopy(summary)
    terminal["state"] = "SUCCEEDED"
    terminal["terminal"] = {"outcome": "SUCCEEDED", "finished_at": None}
    with pytest.raises(storage.WorkflowProgressStorageConflictError, match="completion metadata"):
        storage._storage_bound_summary(terminal, **arguments)

    invalid_summary = deepcopy(summary)
    invalid_summary["reporting_policy"] = "unknown"
    with pytest.raises(
        storage.WorkflowProgressStorageConflictError, match="conflicts with stored detail"
    ):
        storage._storage_bound_summary(invalid_summary, **arguments)


def test_publication_preflight_rejects_mismatched_and_ambiguous_inputs() -> None:
    identity = _identity()
    topology = _topology(identity, node_ids=("node-a",), edges=())
    other_identity = replace(identity, execution_generation=4)
    other_topology = _topology(other_identity, node_ids=("node-a",), edges=())
    summary = workflow_summary(identity, summary_revision=1, node_count=1, running_count=0)
    with pytest.raises(storage.WorkflowProgressStorageError, match="identity does not match"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=RUN_ID,
            prepared_topology=other_topology,
        )

    record = _prepared_detail(identity, "node-a")
    aggregate = storage.PreparedWorkflowProgressDetail(
        records=(record,),
        observed_count=1,
        encoded_bytes=record.encoded_bytes,
        decoded_bytes=record.decoded_bytes,
        truncation_reasons=(),
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="mutually exclusive"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=RUN_ID,
            prepared_detail=aggregate,
            detail_records=[record],
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="duplicate nodes"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=RUN_ID,
            detail_records=[record, record],
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="repeats a removed node"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=RUN_ID,
            remove_node_ids=["node-a", "node-a"],
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="update and remove"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=RUN_ID,
            detail_records=[record],
            remove_node_ids=["node-a"],
        )
    assert topology.identity == identity


@pytest.mark.django_db
def test_publication_rejects_missing_run_and_manifest_storage() -> None:
    execution = _execution(task_id="publication-missing-storage")
    identity = _execution_identity(execution)
    summary = workflow_summary(identity, summary_revision=1, node_count=0, running_count=0)
    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError, match="run storage is missing"
    ):
        storage.persist_workflow_progress_publication(identity, summary, manifest_id=RUN_ID)

    topology = _topology(identity, node_ids=("node-a",), edges=())
    assert storage.stage_workflow_progress_topology(topology) is not None
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="target is missing"):
        storage.persist_workflow_progress_publication(
            identity,
            workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
            manifest_id="00000000-0000-0000-0000-000000000227",
        )


@pytest.mark.django_db
def test_publication_rejects_prepared_topology_detail_and_removal_conflicts() -> None:
    execution = _execution(task_id="publication-topology-conflicts")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    divergent = _topology(identity, node_ids=("node-b",), edges=())
    summary = workflow_summary(identity, summary_revision=1, node_count=1, running_count=0)
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="immutable evidence"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=manifest_id,
            prepared_topology=divergent,
        )
    unknown = _prepared_detail(identity, "node-b")
    with pytest.raises(storage.WorkflowProgressStorageConflictError, match="not present"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=manifest_id,
            detail_records=[unknown],
        )
    map_detail = _prepared_detail(identity, "node-a", kind="map")
    with pytest.raises(storage.WorkflowProgressStorageConflictError, match="fanout conflicts"):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=manifest_id,
            detail_records=[map_detail],
        )
    with pytest.raises(
        storage.WorkflowProgressStorageConflictError, match="removal is not present"
    ):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=manifest_id,
            remove_node_ids=["node-b"],
        )


@pytest.mark.django_db
@pytest.mark.parametrize("invalid_target", ["run_storage", "slot"])
def test_publication_revalidates_verified_target_ownership_and_slot(
    monkeypatch: pytest.MonkeyPatch,
    invalid_target: str,
) -> None:
    execution = _execution(task_id=f"publication-target-{invalid_target}")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
    target = _verified_prepared_topology(
        topology,
        manifest_id=manifest_id,
        run_storage_id=run_storage.pk,
        slot=WorkflowProgressTopologySlot.PENDING,
    )
    if invalid_target == "run_storage":
        target = replace(target, run_storage_id=run_storage.pk + 1)
        message = "belongs to another run storage"
    else:
        target = replace(target, slot="INVALID")
        message = "invalid slot"
    monkeypatch.setattr(
        storage,
        "verify_workflow_progress_topology_manifest",
        lambda *args, **kwargs: target,
    )
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match=message):
        storage.persist_workflow_progress_publication(
            identity,
            workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
            manifest_id=manifest_id,
        )


@pytest.mark.django_db
def test_publication_rejects_a_nonadvancing_pending_topology_after_locked_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution(task_id="publication-nonadvancing-pending")
    identity = _execution_identity(execution)
    current_topology = _topology(identity, version=1, node_ids=("node-a",), edges=())
    current_id = storage.stage_workflow_progress_topology(current_topology)
    assert current_id is not None
    WorkflowProgressTopologyManifest.objects.filter(pk=current_id).update(
        slot=WorkflowProgressTopologySlot.CURRENT,
        published_at=timezone.now(),
    )
    pending_topology = _topology(identity, version=2, node_ids=("node-a",), edges=())
    pending_id = storage.stage_workflow_progress_topology(pending_topology)
    assert pending_id is not None
    run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
    current = _verified_prepared_topology(
        replace(current_topology, topology_version=2),
        manifest_id=current_id,
        run_storage_id=run_storage.pk,
        slot=WorkflowProgressTopologySlot.CURRENT,
    )
    pending = _verified_prepared_topology(
        replace(pending_topology, topology_version=1),
        manifest_id=pending_id,
        run_storage_id=run_storage.pk,
        slot=WorkflowProgressTopologySlot.PENDING,
    )

    def verify(manifest_id: str, **kwargs: Any) -> storage.VerifiedWorkflowProgressTopology:
        return pending if manifest_id == pending_id else current

    monkeypatch.setattr(storage, "verify_workflow_progress_topology_manifest", verify)
    with pytest.raises(storage.WorkflowProgressStorageConflictError, match="does not advance"):
        storage.persist_workflow_progress_publication(
            identity,
            workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
            manifest_id=pending_id,
        )


@pytest.mark.django_db
def test_publication_rejects_reused_initial_evidence_and_corrupt_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publish_initial_workflow(1, case_id=228)
    node_id = workflow_node_id(0)
    record = _prepared_detail(published.identity, node_id)
    aggregate = storage.PreparedWorkflowProgressDetail(
        records=(record,),
        observed_count=1,
        encoded_bytes=record.encoded_bytes,
        decoded_bytes=record.decoded_bytes,
        truncation_reasons=(),
    )
    summary = workflow_summary(
        published.identity,
        summary_revision=2,
        node_count=1,
        running_count=0,
    )
    with pytest.raises(storage.WorkflowProgressStorageConflictError, match="initial publication"):
        storage.persist_workflow_progress_publication(
            published.identity,
            summary,
            manifest_id=published.manifest_id,
            prepared_topology=published.topology,
            prepared_detail=aggregate,
        )

    run_storage = WorkflowProgressRunStorage.objects.get(execution=published.execution)
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
        run_storage.detail_encoded_bytes - 1,
    )
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="aggregates violate"):
        storage.persist_workflow_progress_publication(
            published.identity,
            summary,
            manifest_id=published.manifest_id,
            prepared_topology=published.topology,
        )


@pytest.mark.django_db
def test_publication_accepts_removing_a_topology_node_without_stored_detail() -> None:
    execution = _execution(task_id="remove-without-stored-detail")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    result = storage.persist_workflow_progress_publication(
        identity,
        workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
        manifest_id=manifest_id,
        prepared_topology=topology,
        remove_node_ids=["node-a"],
    )
    assert result.accepted
    assert result.removed_node_count == 0


@pytest.mark.django_db
def test_publication_refuses_to_advance_an_exhausted_detail_revision() -> None:
    published = publish_initial_workflow(1, case_id=229)
    WorkflowProgressRunStorage.objects.filter(execution=published.execution).update(
        detail_revision=storage.WORKFLOW_PROGRESS_IDENTITY_MAX_INTEGER
    )
    node_id = workflow_node_id(0)
    running = _prepared_detail(published.identity, node_id, state="RUNNING")
    with pytest.raises(storage.WorkflowProgressStorageConflictError, match="cannot advance"):
        storage.persist_workflow_progress_publication(
            published.identity,
            workflow_summary(
                published.identity,
                summary_revision=2,
                node_count=1,
                running_count=1,
            ),
            manifest_id=published.manifest_id,
            prepared_topology=published.topology,
            detail_records=[running],
        )


@pytest.mark.django_db
def test_publication_rejects_more_than_the_global_event_row_bound() -> None:
    execution = _execution(task_id="publication-event-row-corruption")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
    rows: list[WorkflowProgressNodeDetail] = []
    for index in range(storage.WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS + 1):
        node_id = f"corrupt-event-node-{index:02d}"
        value = _detail_value(node_id, state="RUNNING")
        value["recent_events"] = [_event(index)]
        record = storage.prepare_workflow_progress_node_detail(value, identity=identity)
        rows.append(
            WorkflowProgressNodeDetail(
                run_storage=run_storage,
                node_key=record.node_key,
                node_id=record.node_id,
                invocation_id=record.invocation_id,
                state=record.state,
                truncated=record.truncated,
                payload=record.payload,
                digest=record.digest,
                encoded_bytes=record.encoded_bytes,
                decoded_bytes=record.decoded_bytes,
                event_count=record.event_count,
                last_topology_version=1,
                last_detail_revision=1,
                updated_at=timezone.now(),
            )
        )
    WorkflowProgressNodeDetail.objects.bulk_create(rows)
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="event-row bound"):
        storage.persist_workflow_progress_publication(
            identity,
            workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
            manifest_id=manifest_id,
            prepared_topology=topology,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("conflict", ["unknown_node", "fanout", "event_count"])
def test_publication_rechecks_records_returned_by_sparse_event_rebalancing(
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
) -> None:
    execution = _execution(task_id=f"publication-rebalanced-{conflict}")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    if conflict == "unknown_node":
        record = _prepared_detail(identity, "node-b")
        expected_error: type[Exception] | None = storage.WorkflowProgressStorageConflictError
        message = "not present"
    elif conflict == "fanout":
        record = _prepared_detail(identity, "node-a", kind="map")
        expected_error = storage.WorkflowProgressStorageConflictError
        message = "fanout conflicts"
    else:
        record = replace(_prepared_detail(identity, "node-a"), event_count=33)
        expected_error = storage.WorkflowProgressStorageError
        message = "evidence is inconsistent"
    monkeypatch.setattr(
        storage,
        "_rebalance_sparse_recent_events",
        lambda *args, **kwargs: [record],
    )

    def publication() -> storage.WorkflowProgressPublicationResult:
        return storage.persist_workflow_progress_publication(
            identity,
            workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
            manifest_id=manifest_id,
            prepared_topology=topology,
        )

    with pytest.raises(expected_error, match=message):
        publication()


@pytest.mark.django_db
@pytest.mark.parametrize("limit_kind", ["encoded", "decoded"])
def test_publication_omits_a_record_that_crosses_a_detail_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
    limit_kind: str,
) -> None:
    execution = _execution(task_id=f"publication-{limit_kind}-budget")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    record = _prepared_detail(identity, "node-a")
    if limit_kind == "encoded":
        monkeypatch.setattr(
            storage,
            "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
            record.encoded_bytes - 1,
        )
        expected_reason = "detail_encoded_bytes"
    else:
        monkeypatch.setattr(
            storage,
            "WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES",
            record.decoded_bytes - 1,
        )
        expected_reason = "detail_decoded_bytes"
    result = storage.persist_workflow_progress_publication(
        identity,
        workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
        manifest_id=manifest_id,
        prepared_topology=topology,
        detail_records=[record],
    )
    assert result.accepted
    assert result.summary is not None
    assert expected_reason in result.summary["detail"]["truncation_reasons"]


@pytest.mark.django_db
def test_publication_rejects_an_unaccounted_durable_row_before_aggregate_delta() -> None:
    execution = _execution(task_id="publication-aggregate-underflow")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    run_storage = WorkflowProgressRunStorage.objects.get(execution=execution)
    record = _prepared_detail(identity, "node-a")
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=record.node_key,
        node_id=record.node_id,
        invocation_id=record.invocation_id,
        state=record.state,
        truncated=record.truncated,
        payload=record.payload,
        digest=record.digest,
        encoded_bytes=record.encoded_bytes,
        decoded_bytes=record.decoded_bytes,
        event_count=record.event_count,
        last_topology_version=1,
        last_detail_revision=1,
        updated_at=timezone.now(),
    )
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="publication epochs"):
        storage.persist_workflow_progress_publication(
            identity,
            workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
            manifest_id=manifest_id,
            prepared_topology=topology,
            remove_node_ids=["node-a"],
        )


@pytest.mark.django_db
def test_prepared_initial_detail_must_retain_topology_truncation_evidence() -> None:
    execution = _execution(task_id="publication-truncation-evidence")
    identity = _execution_identity(execution)
    oversized_node_id = "x" * (storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES + 1)
    topology = storage.prepare_workflow_progress_topology(
        identity,
        1,
        [_node(oversized_node_id)],
        (),
    )
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    incomplete_evidence = storage.PreparedWorkflowProgressDetail(
        records=(),
        observed_count=1,
        encoded_bytes=0,
        decoded_bytes=0,
        truncation_reasons=(),
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match="omits topology truncation"):
        storage.persist_workflow_progress_publication(
            identity,
            workflow_summary(identity, summary_revision=1, node_count=1, running_count=0),
            manifest_id=manifest_id,
            prepared_topology=topology,
            prepared_detail=incomplete_evidence,
        )


@pytest.mark.django_db
def test_current_manifest_fast_path_requires_published_slot_metadata() -> None:
    published = publish_initial_workflow(1, case_id=230)
    manifest = WorkflowProgressTopologyManifest.objects.get(pk=published.manifest_id)
    manifest.published_at = None
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="immutable evidence"):
        storage._trusted_current_topology(
            manifest,
            published.topology,
            run_storage_id=manifest.run_storage_id,
        )


@pytest.mark.django_db
def test_terminal_expiry_stamping_ignores_missing_identity_and_invalid_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="expiry-without-run",
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=None,
    )
    assert not storage.stamp_workflow_progress_detail_expiry_locked(execution, "{}")

    execution.workflow_run_id = RUN_ID
    execution.save(update_fields=["workflow_run_id"])
    assert not storage.stamp_workflow_progress_detail_expiry_locked(execution, "{}")

    monkeypatch.setattr(
        storage, "deserialize_workflow_progress_summary", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        storage, "serialize_workflow_progress_summary", lambda *args, **kwargs: "other"
    )
    assert not storage.stamp_workflow_progress_detail_expiry_locked(execution, "{}")


@pytest.mark.django_db
def test_terminal_expiry_fallback_normalizes_naive_time_and_is_idempotent() -> None:
    published = publish_initial_workflow(1, case_id=231)
    published.execution.state = TaskState.FAILED
    published.execution.finished_at = datetime(2026, 7, 20, 12, 0, 0)
    assert storage.stamp_workflow_progress_detail_expiry_locked(published.execution, None)
    run_storage = WorkflowProgressRunStorage.objects.get(execution=published.execution)
    expected = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
    assert run_storage.detail_expires_at == expected
    assert storage.stamp_workflow_progress_detail_expiry_locked(published.execution, None)


@pytest.mark.django_db
def test_audit_reports_missing_task_current_without_detail_and_missing_summary() -> None:
    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError, match="audit task is missing"
    ):
        storage.audit_workflow_progress_detail_storage(_identity(task_execution_pk=999_999))

    execution = _execution(task_id="audit-current-without-detail")
    identity = _execution_identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    WorkflowProgressTopologyManifest.objects.filter(pk=manifest_id).update(
        slot=WorkflowProgressTopologySlot.CURRENT,
        published_at=timezone.now(),
    )
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="no detail revision"):
        storage.audit_workflow_progress_detail_storage(identity)

    published = publish_initial_workflow(1, case_id=232)
    RayTaskExecution.objects.filter(pk=published.execution.pk).update(
        workflow_progress_summary_json=None
    )
    with pytest.raises(storage.WorkflowProgressStorageIntegrityError, match="no canonical summary"):
        storage.audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_lifecycle_stale_fences_cover_success_and_cancellation_filters() -> None:
    assert lifecycle._canonical_utc(datetime(2026, 7, 20, 12, 0, 0)).endswith("Z")
    execution = _execution(task_id="lifecycle-stale-success")
    assert not lifecycle.succeed_task(
        execution,
        result_data=None,
        result_reference=None,
        expected_ray_job_id="missing-job",
        expected_execution_generation=execution.execution_generation,
    )

    cancelling = _execution(task_id="lifecycle-stale-cancellation")
    cancelling.state = TaskState.CANCELLING
    cancelling.save(update_fields=["state"])
    assert not lifecycle.cancel_task(
        cancelling,
        expected_worker_id="missing-worker",
        expected_ray_job_id="missing-job",
        expected_execution_generation=cancelling.execution_generation,
    )


def test_model_diagnostics_have_stable_human_readable_labels() -> None:
    execution = RayTaskExecution(callable_path="app.jobs.sync", state=TaskState.RUNNING)
    assert str(execution) == "app.jobs.sync (RUNNING)"
    payload = TaskInputPayload(
        reference="database://payload",
        backend="database",
        digest="abcdef0123456789" * 4,
        size_bytes=10,
        envelope_version=1,
        state=InputPayloadState.ACTIVE,
    )
    assert str(payload) == "database Task input abcdef012345 (ACTIVE)"
    lease = TaskWorkerLease(
        worker_id="12345678-worker",
        hostname="worker.example",
        pid=42,
        is_active=False,
    )
    assert str(lease) == "Worker 12345678... on worker.example (inactive)"
