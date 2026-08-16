"""Protocol boundaries for normalized workflow topology and latest-state records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

import django_ray.workflow_progress_storage as storage
from django_ray.redaction import REDACTED
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.summary import WorkflowProgressTruncationReason


def _identity() -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=126,
        attempt_number=2,
        execution_generation=3,
        run_id="00000000-0000-0000-0000-000000000126",
    )


def _node(
    node_id: str,
    *,
    kind: str = "task",
    label: str | None = None,
    runtime_env: dict[str, Any] | None = None,
    ray_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": kind,
        "label": label or f"Node {node_id}",
        "callable_path": "app.jobs.sync_resource",
        "runtime_env": runtime_env or {},
        "ray_options": ray_options or {},
    }


def _edge(source: str, target: str) -> dict[str, str]:
    return {"source": source, "target": target}


def _invocation(
    identity: WorkflowRunIdentity,
    invocation_id: str = "namespace-sync:batch-7",
) -> dict[str, Any]:
    return {**identity.as_dict(), "invocation_id": invocation_id}


def _detail(node_id: str, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "node_id": node_id,
        "invocation_identity": None,
        "state": "PENDING",
        "progress": None,
        "execution": None,
        "fanout": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "recent_events": [],
    }
    value.update(overrides)
    state = value["state"]
    if state == "RUNNING":
        value["started_at"] = overrides.get("started_at", "2026-07-20T12:00:00Z")
    elif state == "SUCCEEDED":
        value["started_at"] = overrides.get("started_at", "2026-07-20T12:00:00Z")
        value["finished_at"] = overrides.get("finished_at", "2026-07-20T12:00:01Z")
    elif state == "FAILED":
        value["started_at"] = overrides.get("started_at", "2026-07-20T12:00:00Z")
        value["finished_at"] = overrides.get("finished_at", "2026-07-20T12:00:01Z")
        value["error"] = overrides.get("error", "node failed")
    return value


def _detail_v2(
    node_id: str,
    *,
    output_preview: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    value = _detail(node_id, **overrides)
    value["schema_version"] = 2
    value["output_preview"] = output_preview or {
        "schema_version": 1,
        "availability": "NOT_REQUESTED",
        "value": None,
    }
    return value


def _event(index: int, *, prefix: str = "event", minute: int = 0) -> dict[str, Any]:
    return {
        "event": "STATE_CHANGE",
        "state": "RUNNING",
        "label": f"{prefix}-{index:02d}",
        "timestamp": f"2026-07-20T12:{minute:02d}:{index:02d}Z",
    }


def _page_records(
    topology: storage.PreparedWorkflowProgressTopology,
    collection: storage.WorkflowProgressTopologyCollection,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page in topology.pages:
        if page.collection is collection:
            records.extend(json.loads(page.payload)["records"])
    return records


def _decoded_detail(record: storage.PreparedWorkflowProgressNodeDetail) -> dict[str, Any]:
    return json.loads(record.payload)


def test_topology_is_canonical_across_collection_and_mapping_order() -> None:
    identity = _identity()
    nodes = [
        _node(
            "node-b",
            runtime_env={"pip": ["httpx", "django"], "env_vars": {"SAFE": "yes"}},
            ray_options={"resources": {"sync": 1, "database": 0.5}},
        ),
        _node("node-a", runtime_env={"working_dir": "s3://code"}),
    ]
    edges = [_edge("node-b", "node-a"), _edge("node-a", "node-b")]
    reordered_nodes = []
    for node in reversed(nodes):
        reordered = dict(reversed(tuple(node.items())))
        reordered["runtime_env"] = dict(reversed(tuple(node["runtime_env"].items())))
        reordered["ray_options"] = dict(reversed(tuple(node["ray_options"].items())))
        reordered_nodes.append(reordered)

    first = storage.prepare_workflow_progress_topology(identity, 4, nodes, edges)
    second = storage.prepare_workflow_progress_topology(
        identity,
        4,
        reordered_nodes,
        reversed(edges),
    )

    assert first == second
    assert first.manifest_payload == second.manifest_payload
    assert first.manifest_digest == second.manifest_digest
    assert [page.payload for page in first.pages] == [page.payload for page in second.pages]
    assert [page.digest for page in first.pages] == [page.digest for page in second.pages]
    assert [
        node["node_id"]
        for node in _page_records(first, storage.WorkflowProgressTopologyCollection.NODE)
    ] == [
        "node-a",
        "node-b",
    ]
    assert _page_records(first, storage.WorkflowProgressTopologyCollection.EDGE) == [
        {"source": "node-a", "target": "node-b"},
        {"source": "node-b", "target": "node-a"},
    ]


def test_topology_exact_shapes_redact_metadata_before_storage() -> None:
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [
            _node(
                "node-a",
                label="password=alpha",
                runtime_env={
                    "env_vars": {"API_KEY": "alpha", "VISIBLE": "yes"},
                    "note": "access_token=alpha",
                },
                ray_options={
                    "metadata": {"credential": "alpha", "queue": "ordinary"},
                },
            ),
            _node("node-b"),
        ],
        [_edge("node-a", "node-b")],
    )

    node = _page_records(topology, storage.WorkflowProgressTopologyCollection.NODE)[0]
    edge = _page_records(topology, storage.WorkflowProgressTopologyCollection.EDGE)[0]
    assert set(node) == {
        "callable_path",
        "kind",
        "label",
        "node_id",
        "ray_options",
        "runtime_env",
    }
    assert node["label"] == REDACTED
    assert node["runtime_env"] == {
        "env_vars": {"VISIBLE": "yes"},
        "note": REDACTED,
    }
    assert node["ray_options"] == {"metadata": {"queue": "ordinary"}}
    assert edge == {"source": "node-a", "target": "node-b"}
    assert b"alpha" not in b"".join(page.payload for page in topology.pages)
    assert WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value in (topology.truncation_reasons)


def test_storage_persists_only_normalized_metadata_metric_and_resource_keys() -> None:
    identity = _identity()
    topology = storage.prepare_workflow_progress_topology(
        identity,
        1,
        [
            _node(
                "node-a",
                runtime_env={"\x1b[31mprofile\x1b[0m": "default"},
                ray_options={"metadata": {"\x9dsafe\x18queue": "ordinary"}},
            )
        ],
        [],
    )
    detail = storage.prepare_workflow_progress_node_detail(
        _detail(
            "node-a",
            state="RUNNING",
            progress={
                "current": 1,
                "total": 2,
                "percent": 50,
                "message": None,
                "metrics": {"\x1b[32mrows\x1b[0m": 12},
                "updated_at": "2026-07-20T12:00:00Z",
            },
            execution={
                "ray_task_id": None,
                "ray_job_id": None,
                "ray_node_id": None,
                "ray_worker_id": None,
                "assigned_resources": {"\x1b[33mCPU\x1b[0m": 1.0},
            },
        ),
        identity=identity,
    )

    node = _page_records(topology, storage.WorkflowProgressTopologyCollection.NODE)[0]
    decoded = _decoded_detail(detail)
    assert node["runtime_env"] == {"profile": "default"}
    assert node["ray_options"] == {"metadata": {"queue": "ordinary"}}
    assert decoded["progress"]["metrics"] == {"rows": 12}
    assert decoded["execution"]["assigned_resources"] == {"CPU": 1.0}
    assert b"\x1b" not in b"".join(page.payload for page in topology.pages)
    assert b"\x1b" not in detail.payload


@pytest.mark.parametrize("location", ("metadata", "metrics", "resources"))
def test_storage_rejects_duplicate_normalized_mapping_keys(location: str) -> None:
    identity = _identity()

    with pytest.raises(storage.WorkflowProgressStorageError, match="duplicate normalized"):
        if location == "metadata":
            storage.prepare_workflow_progress_topology(
                identity,
                1,
                [
                    _node(
                        "node-a",
                        runtime_env={
                            "profile": "first",
                            "\x1b[31mprofile\x1b[0m": "second",
                        },
                    )
                ],
                [],
            )
        elif location == "metrics":
            storage.prepare_workflow_progress_node_detail(
                _detail(
                    "node-a",
                    state="RUNNING",
                    progress={
                        "current": 1,
                        "total": 2,
                        "percent": 50,
                        "message": None,
                        "metrics": {"rows": 12, "\x1b[32mrows\x1b[0m": 13},
                        "updated_at": "2026-07-20T12:00:00Z",
                    },
                ),
                identity=identity,
            )
        else:
            storage.prepare_workflow_progress_node_detail(
                _detail(
                    "node-a",
                    execution={
                        "ray_task_id": None,
                        "ray_job_id": None,
                        "ray_node_id": None,
                        "ray_worker_id": None,
                        "assigned_resources": {
                            "CPU": 1.0,
                            "\x1b[33mCPU\x1b[0m": 2.0,
                        },
                    },
                ),
                identity=identity,
            )


@pytest.mark.parametrize("shape", ["extra", "missing"])
def test_topology_requires_exact_node_and_edge_shapes(shape: str) -> None:
    node = _node("node-a")
    edge: dict[str, Any] = _edge("node-a", "node-b")
    if shape == "extra":
        node["unexpected"] = True
        edge["unexpected"] = True
    else:
        node.pop("runtime_env")
        edge.pop("target")

    with pytest.raises(storage.WorkflowProgressStorageError, match="exact protocol fields"):
        storage.prepare_workflow_progress_topology(_identity(), 1, [node], [])
    with pytest.raises(storage.WorkflowProgressStorageError, match="exact protocol fields"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            [_node("node-a"), _node("node-b")],
            [edge],
        )


@pytest.mark.parametrize(
    "location",
    ["node", "edge", "detail", "invocation", "execution"],
)
def test_secret_like_values_are_rejected_from_identity_fields(location: str) -> None:
    identity = _identity()

    with pytest.raises(storage.WorkflowProgressStorageError, match="resembles sensitive data"):
        if location == "node":
            storage.prepare_workflow_progress_topology(identity, 1, [_node("api_key")], [])
        elif location == "edge":
            storage.prepare_workflow_progress_topology(
                identity,
                1,
                [_node("node-a"), _node("node-b")],
                [_edge("token", "node-b")],
            )
        elif location == "detail":
            storage.prepare_workflow_progress_node_detail(
                _detail("private_key"),
                identity=identity,
            )
        elif location == "invocation":
            storage.prepare_workflow_progress_node_detail(
                _detail("node-a", invocation_identity=_invocation(identity, "secret-value")),
                identity=identity,
            )
        else:
            storage.prepare_workflow_progress_node_detail(
                _detail(
                    "node-a",
                    execution={
                        "ray_task_id": "authorization-token",
                        "ray_job_id": None,
                        "ray_node_id": None,
                        "ray_worker_id": None,
                        "assigned_resources": {},
                    },
                ),
                identity=identity,
            )


def test_invalid_unicode_is_rejected_before_canonical_encoding() -> None:
    invalid = "invalid-\ud800"

    with pytest.raises(storage.WorkflowProgressStorageError, match="valid UTF-8"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            [_node("node-a", runtime_env={"value": invalid})],
            [],
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="valid UTF-8"):
        storage.prepare_workflow_progress_node_detail(
            _detail(
                "node-a",
                progress={
                    "current": 0,
                    "total": 1,
                    "percent": 0,
                    "message": invalid,
                    "metrics": {},
                    "updated_at": "2026-07-20T12:00:00Z",
                },
            ),
            identity=_identity(),
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_rejected_everywhere(value: float) -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="finite"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            [_node("node-a", runtime_env={"value": value})],
            [],
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="finite"):
        storage.prepare_workflow_progress_node_detail(
            _detail(
                "node-a",
                progress={
                    "current": value,
                    "total": 1,
                    "percent": 0,
                    "message": None,
                    "metrics": {},
                    "updated_at": "2026-07-20T12:00:00Z",
                },
            ),
            identity=_identity(),
        )


def test_metadata_nesting_is_bounded() -> None:
    nested: dict[str, Any] = {}
    for _ in range(storage.WORKFLOW_PROGRESS_VALUE_MAX_DEPTH + 1):
        nested = {"child": nested}

    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a", runtime_env=nested)],
        [],
    )

    assert topology.observed_node_count == 1
    assert topology.retained_node_count == 0
    assert topology.truncation_reasons == (
        WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
    )


@pytest.mark.parametrize(
    "component",
    ["detail", "invocation", "progress", "execution", "fanout", "event"],
)
def test_node_detail_nested_objects_require_exact_fields(component: str) -> None:
    identity = _identity()
    value = _detail("node-a")
    if component == "detail":
        value["unexpected"] = True
    elif component == "invocation":
        value["invocation_identity"] = {**_invocation(identity), "unexpected": True}
    elif component == "progress":
        value["progress"] = {
            "current": 0,
            "total": 1,
            "percent": 0,
            "message": None,
            "metrics": {},
            "updated_at": "2026-07-20T12:00:00Z",
            "unexpected": True,
        }
    elif component == "execution":
        value["execution"] = {
            "ray_task_id": None,
            "ray_job_id": None,
            "ray_node_id": None,
            "ray_worker_id": None,
            "assigned_resources": {},
            "unexpected": True,
        }
    elif component == "fanout":
        value["fanout"] = {
            "max_concurrency": None,
            "max_items": None,
            "submitted_items": 0,
            "completed_items": 0,
            "in_flight_items": 0,
            "input_exhausted": False,
            "unexpected": True,
        }
    else:
        value["recent_events"] = [{**_event(0), "unexpected": True}]

    with pytest.raises(storage.WorkflowProgressStorageError, match="exact protocol fields"):
        storage.prepare_workflow_progress_node_detail(value, identity=identity)


def test_observed_topology_is_distinct_from_deterministically_retained_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS", 1)

    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-b"), _node("node-a")],
        [_edge("node-a", "node-b")],
    )

    assert topology.observed_node_ids == frozenset({"node-a", "node-b"})
    assert topology.observed_node_count == 2
    assert topology.observed_edge_count == 1
    assert topology.node_ids == frozenset({"node-a"})
    assert topology.retained_node_count == 1
    assert topology.retained_edge_count == 0
    assert topology.truncation_reasons == (WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value,)


def test_topology_rejects_unknown_duplicate_and_empty_edge_endpoints() -> None:
    nodes = [_node("node-a"), _node("node-b")]

    with pytest.raises(storage.WorkflowProgressStorageError, match="unknown node_id"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            nodes,
            [_edge("node-a", "missing")],
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="duplicate edge"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            nodes,
            [_edge("node-a", "node-b"), _edge("node-a", "node-b")],
        )
    with pytest.raises(storage.WorkflowProgressStorageError):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            nodes,
            [_edge("", "node-b")],
        )


def test_manifest_lists_node_pages_before_edge_pages_with_identity_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS", 1)
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        7,
        [_node("node-c"), _node("node-a"), _node("node-b")],
        [_edge("node-b", "node-c"), _edge("node-a", "node-b")],
    )

    manifest = json.loads(topology.manifest_payload)
    descriptors = manifest["pages"]
    assert [item["collection"] for item in descriptors] == [
        "NODE",
        "NODE",
        "NODE",
        "EDGE",
        "EDGE",
    ]
    assert [item["page_index"] for item in descriptors] == [0, 1, 2, 0, 1]
    assert {item["encoding"] for item in descriptors} == {"identity"}
    assert manifest["topology_version"] == 7
    for descriptor, page in zip(descriptors, topology.pages, strict=True):
        decoded_page = json.loads(page.payload)
        assert descriptor["digest"] == page.digest
        assert descriptor["encoded_bytes"] == page.encoded_bytes == len(page.payload)
        assert descriptor["decoded_bytes"] == page.decoded_bytes == len(page.payload)
        assert decoded_page["collection"] == descriptor["collection"]
        assert decoded_page["schema_version"] == 1


def test_invocation_identity_is_optional_and_accepts_a_bounded_non_uuid_id() -> None:
    identity = _identity()
    invocation_id = "i" * 128
    multibyte_invocation_id = "é" * 128

    absent = storage.prepare_workflow_progress_node_detail(
        _detail("node-a"),
        identity=identity,
    )
    present = storage.prepare_workflow_progress_node_detail(
        _detail("node-a", invocation_identity=_invocation(identity, invocation_id)),
        identity=identity,
    )
    multibyte = storage.prepare_workflow_progress_node_detail(
        _detail(
            "node-a",
            invocation_identity=_invocation(identity, multibyte_invocation_id),
        ),
        identity=identity,
    )

    assert absent.invocation_id is None
    assert _decoded_detail(absent)["invocation_identity"] is None
    assert present.invocation_id == invocation_id
    assert multibyte.invocation_id == multibyte_invocation_id
    assert _decoded_detail(present)["invocation_identity"] == {
        **identity.as_dict(),
        "invocation_id": invocation_id,
    }
    with pytest.raises(storage.WorkflowProgressStorageLimitError):
        storage.prepare_workflow_progress_node_detail(
            _detail(
                "node-a",
                invocation_identity=_invocation(identity, "i" * 129),
            ),
            identity=identity,
        )


@pytest.mark.parametrize("schema_version", [True, 0, 2, "1"])
def test_node_detail_requires_protocol_v1_input(schema_version: Any) -> None:
    value = _detail("node-a")
    value["schema_version"] = schema_version

    with pytest.raises(storage.WorkflowProgressStorageError, match="schema_version"):
        storage.prepare_workflow_progress_node_detail(value, identity=_identity())


def test_node_detail_v2_persists_a_bounded_available_output_preview() -> None:
    record = storage.prepare_workflow_progress_node_detail(
        _detail_v2(
            "node-a",
            state="SUCCEEDED",
            output_preview={
                "schema_version": 1,
                "availability": "AVAILABLE",
                "value": {"item_count": 3, "status": "ready"},
            },
        ),
        identity=_identity(),
    )

    assert _decoded_detail(record)["output_preview"] == {
        "schema_version": 1,
        "availability": "AVAILABLE",
        "value": {"item_count": 3, "status": "ready"},
    }
    assert _decoded_detail(record)["schema_version"] == 2
    assert (
        storage.prepare_workflow_progress_node_detail(
            _decoded_detail(record),
            identity=_identity(),
        )
        == record
    )


def test_node_detail_v1_round_trip_remains_byte_and_digest_stable() -> None:
    legacy = storage.prepare_workflow_progress_node_detail(
        _detail("node-a", state="SUCCEEDED"),
        identity=_identity(),
    )
    reread = storage.prepare_workflow_progress_node_detail(
        _decoded_detail(legacy),
        identity=_identity(),
    )

    assert legacy == reread
    assert legacy.payload == reread.payload
    assert legacy.digest == reread.digest
    assert _decoded_detail(legacy)["schema_version"] == 1
    assert "output_preview" not in _decoded_detail(legacy)


@pytest.mark.parametrize(
    "output_preview",
    [
        pytest.param(
            {
                "schema_version": 1,
                "availability": "AVAILABLE",
                "value": {"api_key": "unredacted"},
            },
            id="unredacted",
        ),
        pytest.param(
            {
                "schema_version": 1,
                "availability": "AVAILABLE",
                "value": b"not-json",
            },
            id="unsupported-type",
        ),
        pytest.param(
            {
                "schema_version": 1,
                "availability": "AVAILABLE",
                "value": "x" * 257,
            },
            id="oversized-string",
        ),
        pytest.param(
            {
                "schema_version": 1,
                "availability": "PENDING",
                "value": {"unexpected": True},
            },
            id="value-with-unavailable-status",
        ),
    ],
)
def test_node_detail_v2_rejects_invalid_output_previews(
    output_preview: dict[str, Any],
) -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="output preview"):
        storage.prepare_workflow_progress_node_detail(
            _detail_v2("node-a", state="SUCCEEDED", output_preview=output_preview),
            identity=_identity(),
        )


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "FAILED"])
def test_node_detail_v2_never_attaches_preview_value_to_non_success(
    state: str,
) -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="non-successful"):
        storage.prepare_workflow_progress_node_detail(
            _detail_v2(
                "node-a",
                state=state,
                output_preview={
                    "schema_version": 1,
                    "availability": "AVAILABLE",
                    "value": {"status": "must not persist"},
                },
            ),
            identity=_identity(),
        )


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED"])
def test_node_detail_v2_rejects_pending_preview_on_terminal_node(state: str) -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="cannot remain pending"):
        storage.prepare_workflow_progress_node_detail(
            _detail_v2(
                "node-a",
                state=state,
                output_preview={
                    "schema_version": 1,
                    "availability": "PENDING",
                    "value": None,
                },
            ),
            identity=_identity(),
        )


def test_normalized_node_detail_is_codec_idempotent() -> None:
    identity = _identity()
    value = _detail(
        "node-a",
        invocation_identity=_invocation(identity),
        state="RUNNING",
        recent_events=[_event(1), _event(0)],
    )

    first = storage.prepare_workflow_progress_node_detail(value, identity=identity)
    second = storage.prepare_workflow_progress_node_detail(
        _decoded_detail(first),
        identity=identity,
    )

    assert first == second


@pytest.mark.parametrize(
    ("field", "mismatch"),
    [
        ("task_execution_pk", 127),
        ("attempt_number", 3),
        ("execution_generation", 4),
        ("run_id", "00000000-0000-0000-0000-000000000127"),
    ],
)
def test_invocation_identity_rejects_a_mismatched_parent(field: str, mismatch: Any) -> None:
    identity = _identity()
    invocation = _invocation(identity)
    invocation[field] = mismatch

    with pytest.raises(
        storage.WorkflowProgressStorageError, match="complete workflow run identity"
    ):
        storage.prepare_workflow_progress_node_detail(
            _detail("node-a", invocation_identity=invocation),
            identity=identity,
        )


def test_node_detail_normalizes_progress_execution_fanout_and_events() -> None:
    identity = _identity()
    record = storage.prepare_workflow_progress_node_detail(
        _detail(
            "node-a",
            invocation_identity=_invocation(identity),
            state="RUNNING",
            progress={
                "current": 1,
                "total": 4,
                "percent": 25,
                "message": "namespace one of four",
                "metrics": {"objects": 12, "ratio": 0.25, "api_key": "alpha"},
                "updated_at": "2026-07-20T12:00:00Z",
            },
            execution={
                "ray_task_id": "ray-task-1",
                "ray_job_id": "ray-job-1",
                "ray_node_id": "ray-node-1",
                "ray_worker_id": "ray-worker-1",
                "assigned_resources": {"CPU": 1.0, "custom": 0.5},
            },
            fanout={
                "max_concurrency": 2,
                "max_items": 10,
                "submitted_items": 3,
                "completed_items": 2,
                "in_flight_items": 1,
                "input_exhausted": False,
            },
            started_at="2026-07-20T12:00:00Z",
            recent_events=[_event(0)],
        ),
        identity=identity,
    )

    decoded = _decoded_detail(record)
    assert decoded["progress"] == {
        "current": 1.0,
        "message": "namespace one of four",
        "metrics": {"objects": 12, "ratio": 0.25},
        "percent": 25.0,
        "total": 4.0,
        "updated_at": "2026-07-20T12:00:00Z",
    }
    assert decoded["execution"] == {
        "assigned_resources": {"CPU": 1.0, "custom": 0.5},
        "ray_job_id": "ray-job-1",
        "ray_node_id": "ray-node-1",
        "ray_task_id": "ray-task-1",
        "ray_worker_id": "ray-worker-1",
    }
    assert decoded["fanout"] == {
        "completed_items": 2,
        "in_flight_items": 1,
        "input_exhausted": False,
        "max_concurrency": 2,
        "max_items": 10,
        "submitted_items": 3,
    }
    assert decoded["recent_events"] == [_event(0)]
    assert decoded["error"] is None
    assert record.event_count == 1
    assert record.state == "RUNNING"


@pytest.mark.parametrize("resource_value", [None, True, "1"])
def test_assigned_resources_are_numeric_only(resource_value: Any) -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="assigned resources"):
        storage.prepare_workflow_progress_node_detail(
            _detail(
                "node-a",
                execution={
                    "ray_task_id": None,
                    "ray_job_id": None,
                    "ray_node_id": None,
                    "ray_worker_id": None,
                    "assigned_resources": {"CPU": resource_value},
                },
            ),
            identity=_identity(),
        )


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "SUCCEEDED", "FAILED"])
def test_node_detail_accepts_all_four_protocol_states(state: str) -> None:
    record = storage.prepare_workflow_progress_node_detail(
        _detail("node-a", state=state),
        identity=_identity(),
    )

    assert record.state == state
    assert _decoded_detail(record)["state"] == state


@pytest.mark.parametrize("state", ["CANCELLED", "LOST", "UNKNOWN", None])
def test_recent_event_state_uses_the_same_four_state_vocabulary(state: Any) -> None:
    event = _event(0)
    event["state"] = state

    with pytest.raises(storage.WorkflowProgressStorageError, match="event state"):
        storage.prepare_workflow_progress_node_detail(
            _detail("node-a", recent_events=[event]),
            identity=_identity(),
        )


@pytest.mark.parametrize(
    "fanout",
    [
        {
            "max_concurrency": 1,
            "max_items": 10,
            "submitted_items": 2,
            "completed_items": 0,
            "in_flight_items": 2,
            "input_exhausted": False,
        },
        {
            "max_concurrency": None,
            "max_items": 1,
            "submitted_items": 2,
            "completed_items": 1,
            "in_flight_items": 1,
            "input_exhausted": True,
        },
        {
            "max_concurrency": None,
            "max_items": None,
            "submitted_items": 2,
            "completed_items": 2,
            "in_flight_items": 1,
            "input_exhausted": True,
        },
    ],
)
def test_node_detail_rejects_inconsistent_fanout(fanout: dict[str, Any]) -> None:
    with pytest.raises(storage.WorkflowProgressStorageError, match="fanout"):
        storage.prepare_workflow_progress_node_detail(
            _detail("node-a", fanout=fanout),
            identity=_identity(),
        )


def test_fanout_presence_matches_the_topology_map_kind() -> None:
    identity = _identity()
    topology = storage.prepare_workflow_progress_topology(
        identity,
        1,
        [_node("map-node", kind="map"), _node("task-node")],
        [],
    )
    fanout = {
        "max_concurrency": 2,
        "max_items": 10,
        "submitted_items": 1,
        "completed_items": 0,
        "in_flight_items": 1,
        "input_exhausted": False,
    }

    valid = storage.prepare_workflow_progress_detail(
        [_detail("task-node"), _detail("map-node", fanout=fanout)],
        topology=topology,
    )

    assert len(valid.records) == 2
    with pytest.raises(storage.WorkflowProgressStorageError, match="fanout"):
        storage.prepare_workflow_progress_detail(
            [_detail("task-node", fanout=fanout), _detail("map-node", fanout=fanout)],
            topology=topology,
        )
    with pytest.raises(storage.WorkflowProgressStorageError, match="fanout"):
        storage.prepare_workflow_progress_detail(
            [_detail("task-node"), _detail("map-node")],
            topology=topology,
        )


def test_full_policy_requires_one_detail_per_observed_topology_node() -> None:
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-b")],
        [],
    )

    with pytest.raises(storage.WorkflowProgressStorageError, match="one record per observed"):
        storage.prepare_workflow_progress_detail(
            [_detail("node-a")],
            topology=topology,
        )
    sampled = storage.prepare_workflow_progress_detail(
        [_detail("node-a")],
        topology=topology,
        reporting_policy="sampled",
    )

    assert [record.node_id for record in sampled.records] == ["node-a"]
    assert WorkflowProgressTruncationReason.REPORTING_POLICY.value in sampled.truncation_reasons


def test_redaction_precedes_digest_and_does_not_create_a_secret_hash_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_METRICS_MAX_ENCODED_BYTES", 1)

    def prepare(secret: str) -> storage.PreparedWorkflowProgressNodeDetail:
        return storage.prepare_workflow_progress_node_detail(
            _detail(
                "node-a",
                progress={
                    "current": 0,
                    "total": 1,
                    "percent": 0,
                    "message": f"password={secret}",
                    "metrics": {"api_key": secret, "safe": "visible"},
                    "updated_at": "2026-07-20T12:00:00Z",
                },
            ),
            identity=_identity(),
        )

    alpha = prepare("alpha-value")
    beta = prepare("beta-value")

    assert alpha.payload == beta.payload
    assert alpha.digest == beta.digest
    assert b"alpha-value" not in alpha.payload
    assert b"beta-value" not in beta.payload
    for secret in (b"alpha-value", b"beta-value"):
        assert hashlib.sha256(secret).hexdigest().encode() not in alpha.payload
    metrics = _decoded_detail(alpha)["progress"]["metrics"]
    assert set(metrics) == {"_omitted"}
    assert metrics["_omitted"].startswith("sha256:")


def test_sensitive_metadata_and_metric_keys_are_omitted_before_digesting() -> None:
    def prepare(
        secret: str,
    ) -> tuple[
        storage.PreparedWorkflowProgressTopology,
        storage.PreparedWorkflowProgressNodeDetail,
    ]:
        node = _node("node-a")
        node["runtime_env"] = {f"password={secret}": "value", "safe": "visible"}
        topology = storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            [node],
            [],
        )
        detail = storage.prepare_workflow_progress_node_detail(
            _detail(
                "node-a",
                progress={
                    "current": 0,
                    "total": 1,
                    "percent": 0,
                    "message": None,
                    "metrics": {f"password={secret}": 1, "safe": 2},
                    "updated_at": "2026-07-20T12:00:00Z",
                },
            ),
            identity=_identity(),
        )
        return topology, detail

    alpha_topology, alpha_detail = prepare("alpha")
    beta_topology, beta_detail = prepare("beta")

    assert alpha_topology.manifest_payload == beta_topology.manifest_payload
    assert [page.payload for page in alpha_topology.pages] == [
        page.payload for page in beta_topology.pages
    ]
    assert alpha_topology.manifest_digest == beta_topology.manifest_digest
    assert alpha_detail.payload == beta_detail.payload
    assert alpha_detail.digest == beta_detail.digest
    assert alpha_detail.truncated
    assert WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value in (
        alpha_topology.truncation_reasons
    )
    durable_bytes = (
        alpha_topology.manifest_payload
        + b"".join(page.payload for page in alpha_topology.pages)
        + alpha_detail.payload
    )
    assert b"alpha" not in durable_bytes
    assert b"beta" not in durable_bytes


def test_page_item_and_encoded_byte_boundaries_are_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS", 1)
    baseline = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a")],
        [],
    )
    exact_page_bytes = baseline.pages[0].encoded_bytes
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS", 2)
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES",
        exact_page_bytes,
    )

    exact = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-b")],
        [],
    )

    assert [page.item_count for page in exact.pages] == [1, 1]
    assert all(page.encoded_bytes <= exact_page_bytes for page in exact.pages)
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES",
        exact_page_bytes - 1,
    )
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="cannot fit"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            [_node("node-a")],
            [],
        )


def test_page_decoded_byte_boundary_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a")],
        [],
    )
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES",
        baseline.pages[0].decoded_bytes - 1,
    )

    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="cannot fit"):
        storage.prepare_workflow_progress_topology(
            _identity(),
            1,
            [_node("node-a")],
            [],
        )


def test_topology_count_boundaries_are_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS", 2)
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS", 1)
    exact = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-b")],
        [_edge("node-a", "node-b")],
    )
    over = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-c"), _node("node-b"), _node("node-a")],
        [_edge("node-a", "node-b"), _edge("node-b", "node-a")],
    )

    assert exact.retained_node_count == 2
    assert exact.retained_edge_count == 1
    assert exact.truncation_reasons == ()
    assert over.retained_node_count == 2
    assert over.retained_edge_count == 1
    assert over.truncation_reasons == (
        WorkflowProgressTruncationReason.EDGE_COUNT_LIMIT.value,
        WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value,
    )


@pytest.mark.parametrize(
    ("constant", "size_attribute", "reason"),
    [
        (
            "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
            "encoded_bytes",
            WorkflowProgressTruncationReason.TOPOLOGY_ENCODED_BYTES.value,
        ),
        (
            "WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES",
            "decoded_bytes",
            WorkflowProgressTruncationReason.TOPOLOGY_DECODED_BYTES.value,
        ),
    ],
)
def test_topology_total_byte_boundaries_are_inclusive(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    size_attribute: str,
    reason: str,
) -> None:
    baseline = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a")],
        [],
    )
    exact_size = getattr(baseline, size_attribute)
    monkeypatch.setattr(storage, constant, exact_size)
    exact = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a")],
        [],
    )
    monkeypatch.setattr(storage, constant, exact_size - 1)
    truncated = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a")],
        [],
    )

    assert exact.retained_node_count == 1
    assert reason not in exact.truncation_reasons
    assert truncated.retained_node_count == 0
    assert reason in truncated.truncation_reasons


def test_node_detail_record_byte_boundary_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _detail("node-a")
    baseline = storage.prepare_workflow_progress_node_detail(value, identity=_identity())
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES",
        baseline.encoded_bytes,
    )

    exact = storage.prepare_workflow_progress_node_detail(value, identity=_identity())

    assert exact.encoded_bytes == baseline.encoded_bytes
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_RECORD_MAX_ENCODED_BYTES",
        baseline.encoded_bytes - 1,
    )
    with pytest.raises(storage.WorkflowProgressStorageLimitError, match="record byte limit"):
        storage.prepare_workflow_progress_node_detail(value, identity=_identity())


def test_detail_count_limit_selects_the_same_sorted_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-b"), _node("node-c")],
        [],
    )
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS", 2)

    forward = storage.prepare_workflow_progress_detail(
        [_detail("node-a"), _detail("node-b"), _detail("node-c")],
        topology=topology,
    )
    reverse = storage.prepare_workflow_progress_detail(
        [_detail("node-c"), _detail("node-b"), _detail("node-a")],
        topology=topology,
    )

    assert forward == reverse
    assert [record.node_id for record in forward.records] == ["node-a", "node-b"]
    assert forward.observed_count == 3
    assert WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value in (forward.truncation_reasons)


@pytest.mark.parametrize(
    ("constant", "combined", "reason"),
    [
        (
            "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
            False,
            WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value,
        ),
        (
            "WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES",
            False,
            WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value,
        ),
        (
            "WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES",
            True,
            WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value,
        ),
        (
            "WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES",
            True,
            WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value,
        ),
    ],
)
def test_detail_byte_boundaries_are_inclusive(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    combined: bool,
    reason: str,
) -> None:
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-b")],
        [],
    )
    records = [
        storage.prepare_workflow_progress_node_detail(_detail("node-a"), identity=_identity()),
        storage.prepare_workflow_progress_node_detail(_detail("node-b"), identity=_identity()),
    ]
    size_attribute = "decoded_bytes" if "DECODED" in constant else "encoded_bytes"
    exact_size = sum(getattr(record, size_attribute) for record in records)
    if combined:
        exact_size += getattr(topology, size_attribute)
    monkeypatch.setattr(storage, constant, exact_size)
    exact = storage.prepare_workflow_progress_detail(
        [_detail("node-b"), _detail("node-a")],
        topology=topology,
    )
    monkeypatch.setattr(storage, constant, exact_size - 1)
    truncated = storage.prepare_workflow_progress_detail(
        [_detail("node-b"), _detail("node-a")],
        topology=topology,
    )

    assert [record.node_id for record in exact.records] == ["node-a", "node-b"]
    assert reason not in exact.truncation_reasons
    assert [record.node_id for record in truncated.records] == ["node-a"]
    assert reason in truncated.truncation_reasons


def test_detail_retains_the_newest_events_under_one_global_budget() -> None:
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-b")],
        [],
    )
    events_a = [_event(index, prefix="a", minute=0) for index in range(20)]
    events_b = [_event(index, prefix="b", minute=1) for index in range(20)]

    detail = storage.prepare_workflow_progress_detail(
        [
            _detail("node-b", recent_events=events_b),
            _detail("node-a", recent_events=events_a),
        ],
        topology=topology,
    )

    assert [record.event_count for record in detail.records] == [12, 20]
    assert _decoded_detail(detail.records[0])["recent_events"] == events_a[-12:]
    assert _decoded_detail(detail.records[1])["recent_events"] == events_b
    assert sum(record.event_count for record in detail.records) == 32
    assert WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value in (detail.truncation_reasons)


def test_omitted_initial_row_cannot_evict_events_from_retained_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-z")],
        [],
    )
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS", 1)
    retained_event = _event(0, prefix="retained", minute=0)
    omitted_events = [_event(index, prefix="omitted", minute=1) for index in range(32)]

    detail = storage.prepare_workflow_progress_detail(
        [
            _detail("node-z", recent_events=omitted_events),
            _detail("node-a", recent_events=[retained_event]),
        ],
        topology=topology,
    )

    assert [record.node_id for record in detail.records] == ["node-a"]
    assert _decoded_detail(detail.records[0])["recent_events"] == [retained_event]
    assert detail.records[0].event_count == 1
    assert WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value in (detail.truncation_reasons)


def test_multibyte_identity_limits_count_utf8_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES", 4)

    exact = storage.prepare_workflow_progress_node_detail(
        _detail("éé"),
        identity=_identity(),
    )

    assert exact.node_id == "éé"
    assert exact.node_key == hashlib.sha256("éé".encode()).hexdigest()
    with pytest.raises(storage.WorkflowProgressStorageLimitError):
        storage.prepare_workflow_progress_node_detail(
            _detail("ééa"),
            identity=_identity(),
        )


def test_detail_rejects_unknown_nodes_but_omits_nodes_not_retained_by_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS", 1)
    topology = storage.prepare_workflow_progress_topology(
        _identity(),
        1,
        [_node("node-a"), _node("node-b")],
        [],
    )

    retained = storage.prepare_workflow_progress_detail(
        [_detail("node-b"), _detail("node-a")],
        topology=topology,
    )

    assert retained.observed_count == 2
    assert [record.node_id for record in retained.records] == ["node-a"]
    with pytest.raises(storage.WorkflowProgressStorageError, match="unknown topology node_id"):
        storage.prepare_workflow_progress_detail(
            [_detail("unknown")],
            topology=topology,
        )


def test_detail_digests_and_selection_are_independent_of_input_mapping_order() -> None:
    identity = _identity()
    topology = storage.prepare_workflow_progress_topology(
        identity,
        1,
        [_node("node-a"), _node("node-b")],
        [],
    )
    values = [
        _detail("node-a", state="RUNNING", recent_events=[_event(0)]),
        _detail("node-b", state="SUCCEEDED"),
    ]
    reordered = [dict(reversed(tuple(value.items()))) for value in reversed(values)]

    first = storage.prepare_workflow_progress_detail(values, topology=topology)
    second = storage.prepare_workflow_progress_detail(reordered, topology=topology)

    assert first == second
    assert [record.digest for record in first.records] == [
        record.digest for record in second.records
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda value: value["progress"].update(current=2, total=1),
            id="progress-current-exceeds-total",
        ),
        pytest.param(
            lambda value: value["progress"].update(percent=101),
            id="progress-percent-exceeds-100",
        ),
        pytest.param(
            lambda value: value["progress"].update(current=1, total=4, percent=26),
            id="progress-percent-does-not-match-counters",
        ),
        pytest.param(
            lambda value: value["progress"].update(current=0, total=0, percent=0),
            id="empty-progress-is-not-complete",
        ),
        pytest.param(
            lambda value: value.update(
                started_at="2026-07-20T12:00:01Z",
                finished_at="2026-07-20T12:00:00Z",
            ),
            id="finish-precedes-start",
        ),
    ],
)
def test_detail_rejects_inconsistent_progress_and_timestamps(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    value = _detail(
        "node-a",
        progress={
            "current": 1,
            "total": 1,
            "percent": 100,
            "message": None,
            "metrics": {},
            "updated_at": "2026-07-20T12:00:00Z",
        },
    )
    mutate(value)

    with pytest.raises(storage.WorkflowProgressStorageError):
        storage.prepare_workflow_progress_node_detail(value, identity=_identity())


def test_zero_total_progress_has_one_unambiguous_complete_encoding() -> None:
    value = _detail(
        "node-a",
        progress={
            "current": 0,
            "total": 0,
            "percent": 100,
            "message": None,
            "metrics": {},
            "updated_at": "2026-07-20T12:00:00Z",
        },
    )

    record = storage.prepare_workflow_progress_node_detail(value, identity=_identity())

    assert _decoded_detail(record)["progress"]["percent"] == 100.0
