"""Persistence, fencing, and bounded-read tests for workflow topology storage."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from uuid import UUID

import pytest
from django.db import connection
from django.db.models import QuerySet
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import django_ray.workflow.progress.preparation as preparation
import django_ray.workflow.progress.runs as progress_module
import django_ray.workflow.progress.storage as storage
from django_ray.models import (
    RayTaskExecution,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyCollection,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)
from django_ray.runtime.context import WorkflowRunIdentity

RUN_ID = "00000000-0000-0000-0000-000000000126"


def _execution(
    *,
    task_id: str = "workflow-storage-persistence",
    run_id: str = RUN_ID,
) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=3,
        workflow_run_id=run_id,
    )


def _identity(execution: RayTaskExecution) -> WorkflowRunIdentity:
    assert execution.workflow_run_id is not None
    return WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=str(execution.workflow_run_id),
    )


def _node(node_id: str, *, kind: str = "task") -> dict[str, object]:
    return {
        "node_id": node_id,
        "kind": kind,
        "label": f"Node {node_id}",
        "callable_path": "app.jobs.sync_resource",
        "runtime_env": {},
        "ray_options": {},
    }


def _edge(source: str, target: str) -> dict[str, str]:
    return {"source": source, "target": target}


def _topology(
    identity: WorkflowRunIdentity,
    version: int = 1,
    *,
    node_ids: tuple[str, ...] = ("node-a", "node-b"),
    edges: tuple[tuple[str, str], ...] = (("node-a", "node-b"),),
    node_kinds: Mapping[str, str] | None = None,
) -> storage.PreparedWorkflowProgressTopology:
    kinds = node_kinds or {}
    return preparation.prepare_workflow_progress_topology(
        identity,
        version,
        [_node(node_id, kind=kinds.get(node_id, "task")) for node_id in node_ids],
        [_edge(source, target) for source, target in edges],
    )


def _detail(
    identity: WorkflowRunIdentity,
    node_id: str,
    *,
    state: str = "PENDING",
    with_event: bool = False,
    node_kind: str = "task",
    force_truncated: bool = False,
) -> storage.PreparedWorkflowProgressNodeDetail:
    started_at = "2026-07-20T12:00:00Z" if state != "PENDING" else None
    finished_at = "2026-07-20T12:00:02Z" if state in {"SUCCEEDED", "FAILED"} else None
    recent_events: list[dict[str, object]] = []
    if with_event:
        recent_events.append(
            {
                "event": "STATE_CHANGE",
                "state": state,
                "label": f"{node_id} entered {state}",
                "timestamp": "2026-07-20T12:00:01Z",
            }
        )
    fanout = None
    if node_kind == "map":
        fanout = {
            "max_concurrency": 4,
            "max_items": 100,
            "submitted_items": 0,
            "completed_items": 0,
            "in_flight_items": 0,
            "input_exhausted": state == "SUCCEEDED",
        }
    error = "node failed" if state == "FAILED" else None
    if force_truncated:
        assert state == "FAILED"
        error = "x" * (storage.WORKFLOW_PROGRESS_MESSAGE_MAX_BYTES + 1)
    return storage.prepare_workflow_progress_node_detail(
        {
            "schema_version": 1,
            "node_id": node_id,
            "invocation_identity": None,
            "state": state,
            "progress": None,
            "execution": None,
            "fanout": fanout,
            "started_at": started_at,
            "finished_at": finished_at,
            "error": error,
            "recent_events": recent_events,
        },
        identity=identity,
    )


def _summary(
    identity: WorkflowRunIdentity,
    *,
    summary_revision: int,
    node_states: tuple[str, ...],
    edge_count: int,
    updated_at: str = "2026-07-20T12:00:01Z",
    workflow_state: str = "RUNNING",
    detail_days: int = 7,
) -> dict[str, object]:
    counts = {
        "PENDING": node_states.count("PENDING"),
        "RUNNING": node_states.count("RUNNING"),
        "SUCCEEDED": node_states.count("SUCCEEDED"),
        "FAILED": node_states.count("FAILED"),
    }
    terminal = workflow_state in {"SUCCEEDED", "FAILED", "CANCELLED", "LOST"}
    finished_at = updated_at if terminal else None
    return {
        "schema_version": 3,
        "storage_protocol_version": 1,
        "run_identity": identity.as_dict(),
        "reporting_policy": "full",
        "selected_strategy": None,
        "plan_fingerprint": None,
        "limits_profile": "v1",
        "summary_revision": summary_revision,
        "topology_version": None,
        "detail_revision": None,
        "state": workflow_state,
        "node_counts": {
            "declared": len(node_states),
            "discovered": len(node_states),
            "retained_topology": 0,
            "retained_detail": 0,
            "pending": counts["PENDING"],
            "running": counts["RUNNING"],
            "succeeded": counts["SUCCEEDED"],
            "failed": counts["FAILED"],
        },
        "edge_counts": {
            "declared": edge_count,
            "discovered": edge_count,
            "retained_topology": 0,
        },
        "progress_percent": 100.0 if workflow_state == "SUCCEEDED" else 0.0,
        "timestamps": {
            "started_at": "2026-07-20T12:00:00Z",
            "updated_at": updated_at,
            "finished_at": finished_at,
        },
        "detail": {
            "availability": "NOT_REPORTED",
            "complete": False,
            "truncation_reasons": [],
        },
        "storage": {"kind": "database", "manifest_id": None},
        "retention": {"detail_days": detail_days, "detail_expires_at": None},
        "terminal": {
            "outcome": workflow_state if terminal else None,
            "finished_at": finished_at,
        },
    }


def _stage_and_publish(
    execution: RayTaskExecution,
    *,
    node_ids: tuple[str, ...] = ("node-a", "node-b"),
    edges: tuple[tuple[str, str], ...] = (("node-a", "node-b"),),
    node_kinds: Mapping[str, str] | None = None,
) -> tuple[
    WorkflowRunIdentity,
    str,
    tuple[storage.PreparedWorkflowProgressNodeDetail, ...],
]:
    identity = _identity(execution)
    topology = _topology(
        identity,
        node_ids=node_ids,
        edges=edges,
        node_kinds=node_kinds,
    )
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    kinds = node_kinds or {}
    details = tuple(
        _detail(identity, node_id, node_kind=kinds.get(node_id, "task")) for node_id in node_ids
    )
    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=tuple("PENDING" for _ in node_ids),
            edge_count=len(edges),
        ),
        manifest_id=manifest_id,
        detail_records=details,
    )
    assert result.accepted
    return identity, manifest_id, details


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("availability", "reporting_policy"),
    [
        ("OMITTED_BY_POLICY", "sampled"),
        ("DISABLED", "disabled"),
    ],
)
def test_summary_only_policy_cannot_publish_detail_storage(
    availability: str,
    reporting_policy: str,
) -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity)
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    summary = _summary(
        identity,
        summary_revision=1,
        node_states=("PENDING", "PENDING"),
        edge_count=1,
    )
    summary["reporting_policy"] = reporting_policy
    summary["detail"]["availability"] = availability  # type: ignore[index]

    with pytest.raises(
        storage.WorkflowProgressStorageConflictError,
        match="summary-only workflow progress",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=manifest_id,
            prepared_topology=topology,
            detail_records=(_detail(identity, "node-a"),),
        )

    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision is None
    assert not WorkflowProgressNodeDetail.objects.exists()
    manifest = WorkflowProgressTopologyManifest.objects.get(pk=manifest_id)
    assert manifest.slot == WorkflowProgressTopologySlot.PENDING
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_pending_prepared_topology_requires_matching_truncation_evidence() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity)
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    WorkflowProgressTopologyManifest.objects.filter(pk=manifest_id).update(
        truncation_reasons="node_count_limit"
    )

    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="truncation evidence is not authenticated",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=1,
                node_states=("PENDING", "PENDING"),
                edge_count=1,
            ),
            manifest_id=manifest_id,
            prepared_topology=topology,
        )

    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision is None
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is None


def _promote(manifest_id: str) -> None:
    WorkflowProgressTopologyManifest.objects.filter(pk=manifest_id).update(
        slot=WorkflowProgressTopologySlot.CURRENT,
        published_at=timezone.now(),
    )


@contextmanager
def _captured_queries() -> Iterator[CaptureQueriesContext]:
    with CaptureQueriesContext(connection) as queries:
        yield queries


@pytest.mark.django_db
@pytest.mark.parametrize(
    "stale_update",
    [
        {"state": TaskState.SUCCEEDED},
        {"attempt_number": 3},
        {"execution_generation": 4},
        {"workflow_run_id": "00000000-0000-0000-0000-000000000127"},
    ],
)
def test_stage_rejects_every_stale_execution_fence_without_creating_a_run(
    stale_update: dict[str, object],
) -> None:
    execution = _execution()
    topology = _topology(_identity(execution))
    RayTaskExecution.objects.filter(pk=execution.pk).update(**stale_update)

    assert storage.stage_workflow_progress_topology(topology) is None
    assert not WorkflowProgressRunStorage.objects.exists()
    assert not WorkflowProgressTopologyManifest.objects.exists()
    assert not WorkflowProgressTopologyPage.objects.exists()


@pytest.mark.django_db
def test_stage_final_fence_rolls_back_without_holding_a_task_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    topology = _topology(_identity(execution))
    original_exists = QuerySet.exists
    original_select_for_update = QuerySet.select_for_update
    execution_fence_reads = 0
    locked_execution_reads = 0

    def turn_stale_after_first_fence(queryset: QuerySet) -> bool:
        nonlocal execution_fence_reads
        result = original_exists(queryset)
        if queryset.model is RayTaskExecution:
            execution_fence_reads += 1
            if execution_fence_reads == 1:
                RayTaskExecution.objects.filter(pk=execution.pk).update(state=TaskState.SUCCEEDED)
        return result

    def track_locked_recheck(queryset: QuerySet, *args: object, **kwargs: object):
        nonlocal locked_execution_reads
        if queryset.model is RayTaskExecution:
            locked_execution_reads += 1
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "exists", turn_stale_after_first_fence)
    monkeypatch.setattr(QuerySet, "select_for_update", track_locked_recheck)

    assert storage.stage_workflow_progress_topology(topology) is None
    assert execution_fence_reads == 2
    assert locked_execution_reads == 0
    assert not WorkflowProgressRunStorage.objects.exists()
    assert not WorkflowProgressTopologyManifest.objects.exists()
    assert not WorkflowProgressTopologyPage.objects.exists()


@pytest.mark.django_db
def test_stage_candidate_writes_never_request_the_lifecycle_task_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    original_select_for_update = QuerySet.select_for_update
    locked_models: list[type[object]] = []

    def track_locks(queryset: QuerySet, *args: object, **kwargs: object):
        locked_models.append(queryset.model)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", track_locks)

    manifest_id = storage.stage_workflow_progress_topology(_topology(_identity(execution)))

    assert manifest_id is not None
    assert RayTaskExecution not in locked_models
    assert WorkflowProgressRunStorage in locked_models
    assert WorkflowProgressTopologyManifest in locked_models


@pytest.mark.django_db
def test_stage_creates_exact_run_and_verifiable_pending_manifest() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity)

    manifest_id = storage.stage_workflow_progress_topology(topology)

    assert manifest_id is not None
    assert str(UUID(manifest_id)) == manifest_id
    run = WorkflowProgressRunStorage.objects.get()
    assert (
        run.execution_id,
        run.attempt_number,
        run.execution_generation,
        str(run.run_id),
    ) == (
        identity.task_execution_pk,
        identity.attempt_number,
        identity.execution_generation,
        identity.run_id,
    )
    assert run.detail_revision is None
    verified = storage.verify_workflow_progress_topology_manifest(
        manifest_id,
        expected_identity=identity,
    )
    assert verified.slot == WorkflowProgressTopologySlot.PENDING
    assert verified.topology_version == 1
    assert verified.node_ids == frozenset({"node-a", "node-b"})
    assert verified.edges == (("node-a", "node-b"),)


@pytest.mark.django_db
def test_stage_is_idempotent_for_same_pending_candidate_and_allows_only_one_pending() -> None:
    execution = _execution()
    identity = _identity(execution)
    first = _topology(identity, 1)

    first_id = storage.stage_workflow_progress_topology(first)
    repeated_id = storage.stage_workflow_progress_topology(first)

    assert repeated_id == first_id
    assert WorkflowProgressRunStorage.objects.count() == 1
    assert WorkflowProgressTopologyManifest.objects.count() == 1
    assert WorkflowProgressTopologyPage.objects.count() == len(first.pages)

    competing = _topology(identity, 2, edges=())
    with pytest.raises(
        storage.WorkflowProgressStorageConflictError,
        match="another topology candidate is already pending",
    ):
        storage.stage_workflow_progress_topology(competing)
    assert WorkflowProgressTopologyManifest.objects.count() == 1


@pytest.mark.django_db
def test_current_manifest_is_idempotent_but_rejects_same_or_lower_divergent_versions() -> None:
    execution = _execution()
    identity = _identity(execution)
    current = _topology(identity, 2)
    current_id = storage.stage_workflow_progress_topology(current)
    assert current_id is not None
    _promote(current_id)

    assert storage.stage_workflow_progress_topology(current) == current_id

    divergent_same_version = _topology(identity, 2, edges=())
    with pytest.raises(
        storage.WorkflowProgressStorageConflictError,
        match="conflicts with the current manifest",
    ):
        storage.stage_workflow_progress_topology(divergent_same_version)

    lower_version = _topology(identity, 1)
    with pytest.raises(
        storage.WorkflowProgressStorageConflictError,
        match="conflicts with the current manifest",
    ):
        storage.stage_workflow_progress_topology(lower_version)

    next_id = storage.stage_workflow_progress_topology(_topology(identity, 3))
    assert next_id is not None
    assert next_id != current_id
    assert set(WorkflowProgressTopologyManifest.objects.values_list("slot", flat=True)) == {
        WorkflowProgressTopologySlot.CURRENT,
        WorkflowProgressTopologySlot.PENDING,
    }


@pytest.mark.django_db
def test_stage_reuses_content_addressed_pages_only_within_the_same_run() -> None:
    first_execution = _execution()
    first_identity = _identity(first_execution)
    first = _topology(first_identity, 1)
    first_manifest_id = storage.stage_workflow_progress_topology(first)
    assert first_manifest_id is not None
    first_page_ids = set(
        WorkflowProgressTopologyManifestPage.objects.filter(
            manifest_id=first_manifest_id
        ).values_list("page_id", flat=True)
    )
    _promote(first_manifest_id)

    second_manifest_id = storage.stage_workflow_progress_topology(_topology(first_identity, 2))
    assert second_manifest_id is not None
    second_page_ids = set(
        WorkflowProgressTopologyManifestPage.objects.filter(
            manifest_id=second_manifest_id
        ).values_list("page_id", flat=True)
    )
    assert second_page_ids == first_page_ids
    assert WorkflowProgressTopologyPage.objects.count() == len(first.pages)

    second_execution = _execution(
        task_id="workflow-storage-second-run",
        run_id="00000000-0000-0000-0000-000000000128",
    )
    second_identity = _identity(second_execution)
    cross_run_manifest_id = storage.stage_workflow_progress_topology(_topology(second_identity, 1))
    assert cross_run_manifest_id is not None
    cross_run_page_ids = set(
        WorkflowProgressTopologyManifestPage.objects.filter(
            manifest_id=cross_run_manifest_id
        ).values_list("page_id", flat=True)
    )
    assert cross_run_page_ids.isdisjoint(first_page_ids)
    assert set(WorkflowProgressTopologyPage.objects.values_list("digest", flat=True)) == {
        page.digest for page in first.pages
    }
    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="belongs to another run",
    ):
        storage.verify_workflow_progress_topology_manifest(
            cross_run_manifest_id,
            expected_identity=first_identity,
        )


@pytest.mark.django_db
def test_verify_rejects_cross_run_page_ownership() -> None:
    first_execution = _execution()
    first_identity = _identity(first_execution)
    first_manifest_id = storage.stage_workflow_progress_topology(_topology(first_identity))
    assert first_manifest_id is not None

    second_execution = _execution(
        task_id="workflow-storage-cross-run-page",
        run_id="00000000-0000-0000-0000-000000000129",
    )
    second_manifest_id = storage.stage_workflow_progress_topology(
        _topology(_identity(second_execution))
    )
    assert second_manifest_id is not None
    foreign_page_id = (
        WorkflowProgressTopologyManifestPage.objects.filter(
            manifest_id=second_manifest_id,
            collection=WorkflowProgressTopologyCollection.NODE,
        )
        .values_list("page_id", flat=True)
        .get()
    )
    WorkflowProgressTopologyManifestPage.objects.filter(
        manifest_id=first_manifest_id,
        collection=WorkflowProgressTopologyCollection.NODE,
    ).update(page_id=foreign_page_id)

    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="ownership or order is invalid",
    ):
        storage.verify_workflow_progress_topology_manifest(
            first_manifest_id,
            expected_identity=first_identity,
        )


@pytest.mark.django_db
def test_stage_detects_content_digest_collision_before_linking_page() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity)
    run = WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=identity.attempt_number,
        execution_generation=identity.execution_generation,
        run_id=identity.run_id,
    )
    prepared_page = topology.pages[0]
    conflicting_payload = bytes(
        byte ^ 1 if index == 0 else byte for index, byte in enumerate(prepared_page.payload)
    )
    WorkflowProgressTopologyPage.objects.create(
        run_storage=run,
        digest=prepared_page.digest,
        collection=prepared_page.collection.value,
        payload=conflicting_payload,
        item_count=prepared_page.item_count,
        encoded_bytes=prepared_page.encoded_bytes,
        decoded_bytes=prepared_page.decoded_bytes,
    )

    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="conflicts with stored bytes",
    ):
        storage.stage_workflow_progress_topology(topology)
    assert not WorkflowProgressTopologyManifest.objects.exists()
    assert WorkflowProgressTopologyPage.objects.count() == 1


@pytest.mark.django_db
def test_verify_detects_manifest_and_page_digest_corruption() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity)
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None

    WorkflowProgressTopologyManifest.objects.filter(pk=manifest_id).update(manifest_digest="0" * 64)
    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="manifest digest is invalid",
    ):
        storage.verify_workflow_progress_topology_manifest(manifest_id)

    WorkflowProgressTopologyManifest.objects.filter(pk=manifest_id).update(
        manifest_digest=topology.manifest_digest
    )
    page = WorkflowProgressTopologyPage.objects.order_by("pk").first()
    assert page is not None
    WorkflowProgressTopologyPage.objects.filter(pk=page.pk).update(digest="0" * 64)
    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="page metadata is invalid",
    ):
        storage.verify_workflow_progress_topology_manifest(manifest_id)


@pytest.mark.django_db
def test_stage_links_node_pages_before_edge_pages_with_independent_indexes() -> None:
    execution = _execution()
    topology = _topology(_identity(execution))

    manifest_id = storage.stage_workflow_progress_topology(topology)

    assert manifest_id is not None
    assert list(
        WorkflowProgressTopologyManifestPage.objects.filter(manifest_id=manifest_id)
        .order_by("pk")
        .values_list("collection", "page_index")
    ) == [
        (WorkflowProgressTopologyCollection.NODE, 0),
        (WorkflowProgressTopologyCollection.EDGE, 0),
    ]


@pytest.mark.django_db
def test_verify_rejects_oversized_manifest_without_selecting_its_blob() -> None:
    execution = _execution()
    manifest_id = storage.stage_workflow_progress_topology(_topology(_identity(execution)))
    assert manifest_id is not None
    oversized = b"x" * (storage.WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES + 1)
    WorkflowProgressTopologyManifest.objects.filter(pk=manifest_id).update(payload=oversized)

    with _captured_queries() as queries:
        with pytest.raises(
            storage.WorkflowProgressStorageIntegrityError,
            match="manifest is oversized",
        ):
            storage.verify_workflow_progress_topology_manifest(manifest_id)

    assert len(queries) == 1
    sql = queries[0]["sql"].upper()
    assert "CASE WHEN" in sql
    assert "LENGTH" in sql
    assert "_BOUNDED_PAYLOAD" in sql


@pytest.mark.django_db
def test_verify_rejects_oversized_page_through_bounded_join_projection() -> None:
    execution = _execution()
    manifest_id = storage.stage_workflow_progress_topology(_topology(_identity(execution)))
    assert manifest_id is not None
    page = WorkflowProgressTopologyPage.objects.order_by("pk").first()
    assert page is not None
    oversized = b"x" * (storage.WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES + 1)
    WorkflowProgressTopologyPage.objects.filter(pk=page.pk).update(payload=oversized)

    with _captured_queries() as queries:
        with pytest.raises(
            storage.WorkflowProgressStorageIntegrityError,
            match="page is oversized",
        ):
            storage.verify_workflow_progress_topology_manifest(manifest_id)

    assert len(queries) == 2
    sql = queries[1]["sql"].upper()
    assert "LENGTH" in sql
    assert "CASE WHEN" not in sql
    assert "_BOUNDED_PAYLOAD" not in sql


@pytest.mark.django_db
def test_manifest_relational_aggregate_mismatch_gates_before_page_queries() -> None:
    execution = _execution()
    manifest_id = storage.stage_workflow_progress_topology(_topology(_identity(execution)))
    assert manifest_id is not None
    manifest = WorkflowProgressTopologyManifest.objects.get(pk=manifest_id)
    WorkflowProgressTopologyManifest.objects.filter(pk=manifest_id).update(
        node_count=manifest.node_count + 1
    )

    with _captured_queries() as queries:
        with pytest.raises(
            storage.WorkflowProgressStorageIntegrityError,
            match="workflow topology relational metadata is invalid",
        ):
            storage.verify_workflow_progress_topology_manifest(manifest_id)

    assert len(queries) == 1
    sql = queries[0]["sql"].upper()
    assert "WORKFLOWPROGRESSTOPOLOGYPAGE" not in sql
    assert "COUNT(" not in sql
    assert "SUM(" not in sql


@pytest.mark.django_db
def test_link_aggregate_mismatch_gates_before_any_page_blob_projection() -> None:
    execution = _execution()
    manifest_id = storage.stage_workflow_progress_topology(_topology(_identity(execution)))
    assert manifest_id is not None
    page = WorkflowProgressTopologyPage.objects.order_by("pk").first()
    assert page is not None
    WorkflowProgressTopologyPage.objects.filter(pk=page.pk).update(
        encoded_bytes=page.encoded_bytes + 1,
        decoded_bytes=page.decoded_bytes + 1,
    )

    with _captured_queries() as queries:
        with pytest.raises(
            storage.WorkflowProgressStorageIntegrityError,
            match="linked-page aggregates are invalid",
        ):
            storage.verify_workflow_progress_topology_manifest(manifest_id)

    assert len(queries) == 2
    aggregate_sql = queries[1]["sql"].upper()
    assert "COUNT(" in aggregate_sql
    assert "SUM(" in aggregate_sql
    assert "LENGTH" in aggregate_sql
    assert "CASE WHEN" not in aggregate_sql
    assert "_BOUNDED_PAYLOAD" not in aggregate_sql


@pytest.mark.django_db
def test_discard_removes_only_the_selected_pending_candidate() -> None:
    execution = _execution()
    identity = _identity(execution)
    current_id = storage.stage_workflow_progress_topology(_topology(identity, 1))
    assert current_id is not None
    _promote(current_id)
    pending_id = storage.stage_workflow_progress_topology(_topology(identity, 2))
    assert pending_id is not None

    assert not storage.discard_workflow_progress_topology_candidate(
        identity,
        manifest_id=current_id,
    )
    assert WorkflowProgressTopologyManifest.objects.filter(pk=pending_id).exists()

    assert storage.discard_workflow_progress_topology_candidate(
        identity,
        manifest_id=pending_id,
    )
    assert WorkflowProgressTopologyManifest.objects.filter(pk=current_id).exists()
    assert not WorkflowProgressTopologyManifest.objects.filter(pk=pending_id).exists()
    assert not storage.discard_workflow_progress_topology_candidate(identity)


@pytest.mark.django_db
def test_discard_keeps_referenced_pages_and_deletes_only_new_orphans() -> None:
    execution = _execution()
    identity = _identity(execution)
    current_id = storage.stage_workflow_progress_topology(_topology(identity, 1, edges=()))
    assert current_id is not None
    _promote(current_id)
    current_page_ids = set(
        WorkflowProgressTopologyManifestPage.objects.filter(manifest_id=current_id).values_list(
            "page_id", flat=True
        )
    )

    pending_id = storage.stage_workflow_progress_topology(_topology(identity, 2))
    assert pending_id is not None
    pending_page_ids = set(
        WorkflowProgressTopologyManifestPage.objects.filter(manifest_id=pending_id).values_list(
            "page_id", flat=True
        )
    )
    orphan_candidates = pending_page_ids - current_page_ids
    assert orphan_candidates

    assert storage.discard_workflow_progress_topology_candidate(identity)
    assert set(WorkflowProgressTopologyPage.objects.values_list("pk", flat=True)) == (
        current_page_ids
    )
    assert not WorkflowProgressTopologyPage.objects.filter(pk__in=orphan_candidates).exists()


@pytest.mark.django_db
def test_task_deletion_cascades_all_staged_storage() -> None:
    execution = _execution()
    manifest_id = storage.stage_workflow_progress_topology(_topology(_identity(execution)))
    assert manifest_id is not None
    assert WorkflowProgressRunStorage.objects.exists()
    assert WorkflowProgressTopologyManifestPage.objects.exists()

    execution.delete()

    assert not WorkflowProgressRunStorage.objects.exists()
    assert not WorkflowProgressTopologyManifest.objects.exists()
    assert not WorkflowProgressTopologyManifestPage.objects.exists()
    assert not WorkflowProgressTopologyPage.objects.exists()


def test_detail_iterable_is_rejected_after_cap_plus_one_without_unbounded_consumption() -> None:
    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=0,
        run_id=RUN_ID,
    )
    record = _detail(identity, "node-a")
    consumed = 0

    def records() -> Iterator[storage.PreparedWorkflowProgressNodeDetail]:
        nonlocal consumed
        while True:
            consumed += 1
            if consumed > storage.WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1:
                raise AssertionError("detail iterable was consumed beyond the rejection witness")
            yield record

    with pytest.raises(
        storage.WorkflowProgressStorageLimitError,
        match="detail publication exceeds the retained-node limit",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            {},
            manifest_id=RUN_ID,
            detail_records=records(),
        )

    assert consumed == storage.WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1


def test_removal_iterable_is_rejected_after_cap_plus_one_without_unbounded_consumption() -> None:
    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=0,
        run_id=RUN_ID,
    )
    consumed = 0

    def removals() -> Iterator[str]:
        nonlocal consumed
        while True:
            consumed += 1
            if consumed > storage.WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1:
                raise AssertionError("removal iterable was consumed beyond the rejection witness")
            yield "node-a"

    with pytest.raises(
        storage.WorkflowProgressStorageLimitError,
        match="detail publication removals exceed the retained-node limit",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            {},
            manifest_id=RUN_ID,
            remove_node_ids=removals(),
        )

    assert consumed == storage.WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS + 1


@pytest.mark.django_db
def test_first_publication_atomically_persists_detail_aggregates_and_summary_pointer() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity)
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    details = (_detail(identity, "node-a"), _detail(identity, "node-b"))

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
        ),
        manifest_id=manifest_id,
        detail_records=details,
    )

    assert result.accepted
    assert result.changed_node_count == 2
    assert result.removed_node_count == 0
    assert result.summary is not None
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 1
    assert run.detail_node_count == 2
    assert run.detail_event_count == 0
    assert run.detail_encoded_bytes == sum(record.encoded_bytes for record in details)
    assert run.detail_decoded_bytes == sum(record.decoded_bytes for record in details)
    assert run.detail_expires_at is None
    manifest = WorkflowProgressTopologyManifest.objects.get(pk=manifest_id)
    assert manifest.slot == WorkflowProgressTopologySlot.CURRENT
    assert manifest.published_at is not None
    rows = list(WorkflowProgressNodeDetail.objects.order_by("node_id"))
    assert [row.node_id for row in rows] == ["node-a", "node-b"]
    assert {row.last_topology_version for row in rows} == {1}
    assert {row.last_detail_revision for row in rows} == {1}

    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is not None
    persisted_summary = json.loads(execution.workflow_progress_summary_json)
    assert persisted_summary == result.summary
    assert persisted_summary["storage"] == {
        "kind": "database",
        "manifest_id": manifest_id,
    }
    assert persisted_summary["topology_version"] == 1
    assert persisted_summary["detail_revision"] == 1
    assert persisted_summary["node_counts"]["retained_topology"] == 2
    assert persisted_summary["node_counts"]["retained_detail"] == 2
    assert persisted_summary["edge_counts"]["retained_topology"] == 1
    assert persisted_summary["detail"] == {
        "availability": "AVAILABLE",
        "complete": True,
        "truncation_reasons": [],
    }


@pytest.mark.django_db
def test_publication_persists_truncated_flag_and_exact_run_state_aggregates() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    detail = _detail(
        identity,
        "node-a",
        state="FAILED",
        force_truncated=True,
    )
    assert detail.truncated

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=("FAILED",),
            edge_count=0,
        ),
        manifest_id=manifest_id,
        prepared_topology=topology,
        detail_records=(detail,),
    )

    assert result.accepted
    row = WorkflowProgressNodeDetail.objects.get()
    assert row.truncated is True
    assert row.state == "FAILED"
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_node_count == 1
    assert run.detail_pending_count == 0
    assert run.detail_running_count == 0
    assert run.detail_succeeded_count == 0
    assert run.detail_failed_count == 1
    assert run.detail_truncated_count == 1
    assert result.summary is not None
    assert result.summary["detail"] == {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["record_size_limit"],
    }

    repeated = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("FAILED",),
            edge_count=0,
            updated_at="2026-07-20T12:00:02Z",
        ),
        manifest_id=manifest_id,
        prepared_topology=topology,
        detail_records=(detail,),
    )
    assert repeated.accepted
    assert repeated.changed_node_count == 0
    run.refresh_from_db()
    assert run.detail_revision == 1
    assert run.detail_truncated_count == 1


@pytest.mark.django_db
def test_available_publication_rejects_stored_state_counts_that_contradict_summary() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None

    with pytest.raises(
        storage.WorkflowProgressStorageConflictError,
        match="retained workflow node states conflict with summary aggregates",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=1,
                node_states=("RUNNING",),
                edge_count=0,
            ),
            manifest_id=manifest_id,
            prepared_topology=topology,
            detail_records=(_detail(identity, "node-a"),),
        )

    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision is None
    assert run.detail_node_count == 0
    assert not WorkflowProgressNodeDetail.objects.exists()
    assert (
        WorkflowProgressTopologyManifest.objects.get(pk=manifest_id).slot
        == WorkflowProgressTopologySlot.PENDING
    )


@pytest.mark.django_db
def test_topology_and_detail_truncation_reasons_are_reported_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    identity = _identity(execution)
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS", 1)
    topology = _topology(identity, node_ids=("node-a", "node-b"), edges=())
    assert topology.truncation_reasons == ("node_count_limit",)
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=("PENDING", "PENDING"),
            edge_count=0,
        ),
        manifest_id=manifest_id,
        prepared_topology=topology,
    )

    assert result.accepted
    assert result.summary is not None
    assert result.summary["detail"]["truncation_reasons"] == [
        "detail_count_limit",
        "node_count_limit",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("node_ids", "sampled_ids"),
    [
        (("node-a", "node-b"), ("node-a",)),
        (("node-a",), ("node-a",)),
    ],
)
def test_sampled_publication_preserves_policy_provenance_even_when_all_rows_fit(
    node_ids: tuple[str, ...],
    sampled_ids: tuple[str, ...],
) -> None:
    execution = _execution(task_id=f"workflow-storage-sampled-{len(node_ids)}")
    identity = _identity(execution)
    topology = _topology(identity, node_ids=node_ids, edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    values: list[dict[str, object]] = []
    for node_id in sampled_ids:
        value = json.loads(_detail(identity, node_id).payload)
        value.pop("truncated")
        values.append(value)
    prepared_detail = storage.prepare_workflow_progress_detail(
        values,
        topology=topology,
        reporting_policy="sampled",
    )
    assert prepared_detail.observed_count == len(node_ids)
    summary = _summary(
        identity,
        summary_revision=1,
        node_states=tuple("PENDING" for _ in node_ids),
        edge_count=0,
    )
    summary["reporting_policy"] = "sampled"

    result = storage.persist_workflow_progress_publication(
        identity,
        summary,
        manifest_id=manifest_id,
        prepared_topology=topology,
        prepared_detail=prepared_detail,
    )

    assert result.accepted
    assert result.summary is not None
    assert result.summary["node_counts"]["discovered"] == len(node_ids)
    assert result.summary["node_counts"]["retained_detail"] == len(sampled_ids)
    assert result.summary["detail"] == {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["reporting_policy"],
    }


@pytest.mark.django_db
def test_publication_is_detail_idempotent_across_same_and_new_summary_revisions() -> None:
    execution = _execution()
    identity, manifest_id, details = _stage_and_publish(execution)

    repeated = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
        ),
        manifest_id=manifest_id,
        detail_records=details,
    )
    assert repeated.accepted
    assert repeated.changed_node_count == 0
    assert repeated.removed_node_count == 0
    assert WorkflowProgressRunStorage.objects.get().detail_revision == 1
    assert set(
        WorkflowProgressNodeDetail.objects.values_list(
            "last_detail_revision",
            flat=True,
        )
    ) == {1}

    summary_only_advance = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
            updated_at="2026-07-20T12:00:02Z",
        ),
        manifest_id=manifest_id,
    )
    assert summary_only_advance.accepted
    assert summary_only_advance.changed_node_count == 0
    assert WorkflowProgressRunStorage.objects.get().detail_revision == 1
    execution.refresh_from_db()
    assert json.loads(execution.workflow_progress_summary_json or "{}")["summary_revision"] == 2


@pytest.mark.django_db
def test_idempotent_publication_batches_more_than_five_hundred_touched_nodes() -> None:
    execution = _execution()
    node_ids = tuple(f"node-{index:04d}" for index in range(501))
    identity, manifest_id, details = _stage_and_publish(
        execution,
        node_ids=node_ids,
        edges=(),
    )

    repeated = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=tuple("PENDING" for _ in node_ids),
            edge_count=0,
        ),
        manifest_id=manifest_id,
        detail_records=details,
    )

    assert repeated.accepted
    assert repeated.changed_node_count == 0
    assert WorkflowProgressRunStorage.objects.get().detail_revision == 1


@pytest.mark.django_db
def test_touched_row_reader_rejects_mismatched_node_id_and_storage_key_sets() -> None:
    execution = _execution()
    identity = _identity(execution)
    manifest_id = storage.stage_workflow_progress_topology(
        _topology(identity, node_ids=("node-a",), edges=())
    )
    assert manifest_id is not None
    run = WorkflowProgressRunStorage.objects.get()

    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="identities do not match their storage keys",
    ):
        storage._verified_touched_node_rows(
            run,
            node_ids={"node-a"},
            node_keys={"0" * 64},
            identity=identity,
            using="default",
        )


@pytest.mark.django_db
def test_touched_row_duplicate_probe_is_limited_before_corrupt_blob_processing() -> None:
    execution = _execution()
    identity, _, _ = _stage_and_publish(
        execution,
        node_ids=("node-a",),
        edges=(),
    )
    run = WorkflowProgressRunStorage.objects.get()
    valid = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run,
        node_key=_detail(identity, "node-b").node_key,
        node_id="node-a",
        invocation_id=valid.invocation_id,
        state=valid.state,
        truncated=valid.truncated,
        payload=b"not-json",
        digest="0" * 64,
        encoded_bytes=len(b"not-json"),
        decoded_bytes=len(b"not-json"),
        event_count=valid.event_count,
        last_topology_version=valid.last_topology_version,
        last_detail_revision=valid.last_detail_revision,
    )

    with _captured_queries() as queries:
        with pytest.raises(
            storage.WorkflowProgressStorageIntegrityError,
            match="detail identities are duplicated",
        ):
            storage._verified_touched_node_rows(
                run,
                node_ids={"node-a"},
                node_keys={valid.node_key},
                identity=identity,
                using="default",
            )

    assert len(queries) == 1
    sql = queries[0]["sql"].upper()
    assert "LIMIT 2" in sql
    assert "CASE WHEN" in sql
    assert "LENGTH" in sql


@pytest.mark.django_db
def test_sparse_publication_updates_only_changed_rows_and_applies_aggregate_deltas() -> None:
    execution = _execution()
    identity, manifest_id, initial_details = _stage_and_publish(execution)
    unchanged_before = WorkflowProgressNodeDetail.objects.get(node_id="node-b")
    changed = _detail(identity, "node-a", state="RUNNING", with_event=True)
    current_topology = _topology(identity)

    with _captured_queries() as queries:
        result = storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=2,
                node_states=("RUNNING", "PENDING"),
                edge_count=1,
                updated_at="2026-07-20T12:00:02Z",
            ),
            manifest_id=manifest_id,
            prepared_topology=current_topology,
            detail_records=(changed,),
        )

    assert result.accepted
    assert result.changed_node_count == 1
    assert result.removed_node_count == 0
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 2
    assert run.detail_node_count == 2
    assert run.detail_event_count == 1
    assert run.detail_encoded_bytes == changed.encoded_bytes + initial_details[1].encoded_bytes
    assert run.detail_decoded_bytes == changed.decoded_bytes + initial_details[1].decoded_bytes
    changed_row = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
    unchanged_after = WorkflowProgressNodeDetail.objects.get(node_id="node-b")
    assert changed_row.last_detail_revision == 2
    assert changed_row.last_topology_version == 1
    assert unchanged_after.last_detail_revision == 1
    assert unchanged_after.updated_at == unchanged_before.updated_at
    assert unchanged_after.payload == unchanged_before.payload
    sql = "\n".join(query["sql"].upper() for query in queries)
    assert "COUNT(" not in sql
    assert "SUM(" not in sql


@pytest.mark.django_db
def test_prepared_current_fast_path_avoids_topology_blobs_and_aggregate_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    identity, manifest_id, _ = _stage_and_publish(execution)
    prepared = _topology(identity)

    def forbid_manifest_verification(*args: object, **kwargs: object) -> None:
        raise AssertionError("current prepared topology must not reread immutable blobs")

    monkeypatch.setattr(
        storage,
        "verify_workflow_progress_topology_manifest",
        forbid_manifest_verification,
    )
    with _captured_queries() as queries:
        result = storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=2,
                node_states=("PENDING", "PENDING"),
                edge_count=1,
                updated_at="2026-07-20T12:00:02Z",
            ),
            manifest_id=manifest_id,
            prepared_topology=prepared,
        )

    assert result.accepted
    assert result.changed_node_count == 0
    sql = "\n".join(query["sql"].upper() for query in queries)
    assert "COUNT(" not in sql
    assert "SUM(" not in sql
    assert '"PAYLOAD"' not in sql


@pytest.mark.django_db
def test_prepared_current_evidence_mismatch_rejects_without_rereading_blobs() -> None:
    execution = _execution()
    identity, manifest_id, _ = _stage_and_publish(execution)
    mismatched = _topology(identity, edges=())

    with _captured_queries() as queries:
        with pytest.raises(
            storage.WorkflowProgressStorageIntegrityError,
            match="current workflow topology conflicts with prepared immutable evidence",
        ):
            storage.persist_workflow_progress_publication(
                identity,
                _summary(
                    identity,
                    summary_revision=2,
                    node_states=("PENDING", "PENDING"),
                    edge_count=0,
                    updated_at="2026-07-20T12:00:02Z",
                ),
                manifest_id=manifest_id,
                prepared_topology=mismatched,
            )

    sql = "\n".join(query["sql"].upper() for query in queries)
    assert "COUNT(" not in sql
    assert "SUM(" not in sql
    assert '"PAYLOAD"' not in sql
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 1
    execution.refresh_from_db()
    assert json.loads(execution.workflow_progress_summary_json or "{}")["summary_revision"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    (
        "node_ids",
        "topology_edges",
        "summary_states",
        "summary_edge_count",
        "retained_detail_ids",
        "initial_reason",
        "expected_reason",
    ),
    [
        (
            ("node-a",),
            (),
            ("PENDING", "PENDING"),
            0,
            ("node-a",),
            "reporting_policy",
            "node_count_limit",
        ),
        (
            ("node-a",),
            (),
            ("PENDING",),
            1,
            ("node-a",),
            "reporting_policy",
            "edge_count_limit",
        ),
        (
            ("node-a", "node-b"),
            (),
            ("PENDING", "PENDING"),
            0,
            ("node-a",),
            "edge_count_limit",
            "detail_count_limit",
        ),
    ],
)
def test_untrusted_truncation_reason_does_not_mask_required_storage_reason(
    node_ids: tuple[str, ...],
    topology_edges: tuple[tuple[str, str], ...],
    summary_states: tuple[str, ...],
    summary_edge_count: int,
    retained_detail_ids: tuple[str, ...],
    initial_reason: str,
    expected_reason: str,
) -> None:
    execution = _execution()
    identity = _identity(execution)
    manifest_id = storage.stage_workflow_progress_topology(
        _topology(
            identity,
            node_ids=node_ids,
            edges=topology_edges,
        )
    )
    assert manifest_id is not None
    summary = _summary(
        identity,
        summary_revision=1,
        node_states=summary_states,
        edge_count=summary_edge_count,
    )
    detail_summary = summary["detail"]
    assert isinstance(detail_summary, dict)
    detail_summary["truncation_reasons"] = [initial_reason]

    result = storage.persist_workflow_progress_publication(
        identity,
        summary,
        manifest_id=manifest_id,
        detail_records=tuple(_detail(identity, node_id) for node_id in retained_detail_ids),
    )

    assert result.accepted
    assert result.summary is not None
    reasons = set(result.summary["detail"]["truncation_reasons"])
    assert initial_reason not in reasons
    assert expected_reason in reasons


@pytest.mark.django_db
def test_topology_advance_sparse_publication_removes_departed_nodes_and_preserves_others() -> None:
    execution = _execution()
    identity, first_manifest_id, _ = _stage_and_publish(execution)
    preserved_before = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
    next_topology = _topology(
        identity,
        2,
        node_ids=("node-a", "node-c"),
        edges=(("node-a", "node-c"),),
    )
    next_manifest_id = storage.stage_workflow_progress_topology(next_topology)
    assert next_manifest_id is not None
    added = _detail(identity, "node-c")

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
            updated_at="2026-07-20T12:00:02Z",
        ),
        manifest_id=next_manifest_id,
        detail_records=(added,),
    )

    assert result.accepted
    assert result.changed_node_count == 1
    assert result.removed_node_count == 1
    assert not WorkflowProgressTopologyManifest.objects.filter(pk=first_manifest_id).exists()
    next_manifest = WorkflowProgressTopologyManifest.objects.get(pk=next_manifest_id)
    assert next_manifest.slot == WorkflowProgressTopologySlot.CURRENT
    assert set(WorkflowProgressNodeDetail.objects.values_list("node_id", flat=True)) == {
        "node-a",
        "node-c",
    }
    preserved_after = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
    assert preserved_after.last_topology_version == 1
    assert preserved_after.last_detail_revision == 1
    assert preserved_after.updated_at == preserved_before.updated_at
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 2
    assert run.detail_node_count == 2
    execution.refresh_from_db()
    summary = json.loads(execution.workflow_progress_summary_json or "{}")
    assert summary["storage"]["manifest_id"] == next_manifest_id
    assert summary["topology_version"] == 2
    assert summary["detail_revision"] == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("initial_kind", "next_kind", "supply_replacement"),
    [
        ("task", "map", False),
        ("map", "task", True),
    ],
)
def test_topology_kind_change_deletes_incompatible_detail_unless_replaced(
    initial_kind: str,
    next_kind: str,
    supply_replacement: bool,
) -> None:
    execution = _execution()
    identity, _, _ = _stage_and_publish(
        execution,
        node_ids=("node-a",),
        edges=(),
        node_kinds={"node-a": initial_kind},
    )
    next_topology = _topology(
        identity,
        2,
        node_ids=("node-a",),
        edges=(),
        node_kinds={"node-a": next_kind},
    )
    next_manifest_id = storage.stage_workflow_progress_topology(next_topology)
    assert next_manifest_id is not None
    replacements = (_detail(identity, "node-a", node_kind=next_kind),) if supply_replacement else ()

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("PENDING",),
            edge_count=0,
            updated_at="2026-07-20T12:00:02Z",
        ),
        manifest_id=next_manifest_id,
        prepared_topology=next_topology,
        detail_records=replacements,
    )

    assert result.accepted
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 2
    if supply_replacement:
        assert result.changed_node_count == 1
        assert result.removed_node_count == 0
        assert run.detail_node_count == 1
        row = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
        assert row.last_topology_version == 2
        decoded = json.loads(bytes(row.payload))
        assert (decoded["fanout"] is not None) == (next_kind == "map")
    else:
        assert result.changed_node_count == 0
        assert result.removed_node_count == 1
        assert run.detail_node_count == 0
        assert not WorkflowProgressNodeDetail.objects.exists()


@pytest.mark.django_db
def test_explicit_sparse_removal_advances_revision_and_aggregate_deltas() -> None:
    execution = _execution()
    identity, manifest_id, details = _stage_and_publish(execution)

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
            updated_at="2026-07-20T12:00:02Z",
        ),
        manifest_id=manifest_id,
        remove_node_ids=("node-b",),
    )

    assert result.accepted
    assert result.changed_node_count == 0
    assert result.removed_node_count == 1
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 2
    assert run.detail_node_count == 1
    assert run.detail_encoded_bytes == details[0].encoded_bytes
    assert run.detail_decoded_bytes == details[0].decoded_bytes
    assert list(WorkflowProgressNodeDetail.objects.values_list("node_id", flat=True)) == ["node-a"]
    assert result.summary is not None
    assert result.summary["detail"]["availability"] == "TRUNCATED"


@pytest.mark.django_db
def test_omitted_new_record_does_not_advance_an_existing_detail_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    identity = _identity(execution)
    manifest_id = storage.stage_workflow_progress_topology(_topology(identity))
    assert manifest_id is not None
    first = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
        ),
        manifest_id=manifest_id,
        detail_records=(_detail(identity, "node-a"),),
    )
    assert first.accepted
    assert WorkflowProgressRunStorage.objects.get().detail_revision == 1
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS", 1)

    omitted = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
            updated_at="2026-07-20T12:00:02Z",
        ),
        manifest_id=manifest_id,
        detail_records=(_detail(identity, "node-b"),),
    )

    assert omitted.accepted
    assert omitted.changed_node_count == 0
    assert omitted.removed_node_count == 0
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 1
    assert run.detail_node_count == 1
    assert list(WorkflowProgressNodeDetail.objects.values_list("node_id", flat=True)) == ["node-a"]
    assert omitted.summary is not None
    assert "detail_count_limit" in omitted.summary["detail"]["truncation_reasons"]


@pytest.mark.django_db
def test_rejected_sparse_row_cannot_evict_events_from_retained_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution(task_id="workflow-storage-rejected-event-eviction")
    identity = _identity(execution)
    manifest_id = storage.stage_workflow_progress_topology(
        _topology(identity, node_ids=("node-a", "node-z"), edges=())
    )
    assert manifest_id is not None
    retained = _detail(identity, "node-a", state="RUNNING", with_event=True)
    initial = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=("RUNNING", "PENDING"),
            edge_count=0,
        ),
        manifest_id=manifest_id,
        detail_records=(retained,),
    )
    assert initial.accepted
    retained_events = json.loads(retained.payload)["recent_events"]

    rejected_value = json.loads(_detail(identity, "node-z", state="RUNNING").payload)
    rejected_value["recent_events"] = [
        {
            "event": "STATE_CHANGE",
            "state": "RUNNING",
            "label": f"newer event {index}",
            "timestamp": f"2026-07-20T12:01:{index:02d}Z",
        }
        for index in range(storage.WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS)
    ]
    rejected = storage.prepare_workflow_progress_node_detail(
        rejected_value,
        identity=identity,
    )
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS", 1)

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("RUNNING", "RUNNING"),
            edge_count=0,
            updated_at="2026-07-20T12:01:32Z",
        ),
        manifest_id=manifest_id,
        detail_records=(rejected,),
    )

    assert result.accepted
    assert result.changed_node_count == 0
    row = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
    assert json.loads(row.payload)["recent_events"] == retained_events
    assert row.event_count == 1
    assert not WorkflowProgressNodeDetail.objects.filter(node_id="node-z").exists()
    assert WorkflowProgressRunStorage.objects.get().detail_revision == 1


@pytest.mark.django_db
def test_rejected_replacement_deletes_old_detail_and_advances_revision_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    identity, manifest_id, details = _stage_and_publish(
        execution,
        node_ids=("node-a",),
        edges=(),
    )
    old = details[0]
    replacement = _detail(identity, "node-a", state="RUNNING", with_event=True)
    assert replacement.encoded_bytes > old.encoded_bytes
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
        old.encoded_bytes,
    )

    rejected = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=2,
            node_states=("RUNNING",),
            edge_count=0,
            updated_at="2026-07-20T12:00:02Z",
        ),
        manifest_id=manifest_id,
        detail_records=(replacement,),
    )

    assert rejected.accepted
    assert rejected.changed_node_count == 0
    assert rejected.removed_node_count == 1
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 2
    assert run.detail_node_count == 0
    assert run.detail_encoded_bytes == 0
    assert run.detail_decoded_bytes == 0
    assert not WorkflowProgressNodeDetail.objects.exists()
    assert rejected.summary is not None
    assert rejected.summary["detail"]["truncation_reasons"] == ["detail_encoded_bytes"]


@pytest.mark.django_db
def test_corrupt_run_aggregate_outside_active_limits_aborts_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    identity, manifest_id, details = _stage_and_publish(
        execution,
        node_ids=("node-a",),
        edges=(),
    )
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_encoded_bytes == details[0].encoded_bytes
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
        run.detail_encoded_bytes - 1,
    )

    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="aggregates violate storage limits",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=2,
                node_states=("PENDING",),
                edge_count=0,
                updated_at="2026-07-20T12:00:02Z",
            ),
            manifest_id=manifest_id,
            detail_records=details,
        )

    run.refresh_from_db()
    assert run.detail_revision == 1
    assert run.detail_encoded_bytes == details[0].encoded_bytes
    assert WorkflowProgressNodeDetail.objects.count() == 1
    execution.refresh_from_db()
    assert json.loads(execution.workflow_progress_summary_json or "{}")["summary_revision"] == 1


@pytest.mark.django_db
def test_node_key_collision_aborts_topology_and_detail_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    identity, current_manifest_id, _ = _stage_and_publish(
        execution,
        node_ids=("node-a",),
        edges=(),
    )
    next_manifest_id = storage.stage_workflow_progress_topology(
        _topology(
            identity,
            2,
            node_ids=("node-a", "node-c"),
            edges=(),
        )
    )
    assert next_manifest_id is not None
    current_row = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
    original_sha256 = storage.hashlib.sha256
    colliding_key = current_row.node_key

    class _CollidingDigest:
        def hexdigest(self) -> str:
            return colliding_key

    def collide_node_c(value: bytes = b""):
        if value == b"node-c":
            return _CollidingDigest()
        return original_sha256(value)

    monkeypatch.setattr(storage.hashlib, "sha256", collide_node_c)
    colliding_detail = _detail(identity, "node-c")

    with pytest.raises(
        storage.WorkflowProgressStorageIntegrityError,
        match="hash collision detected during publication",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=2,
                node_states=("PENDING", "PENDING"),
                edge_count=0,
                updated_at="2026-07-20T12:00:02Z",
            ),
            manifest_id=next_manifest_id,
            detail_records=(colliding_detail,),
        )

    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 1
    assert WorkflowProgressNodeDetail.objects.count() == 1
    assert (
        WorkflowProgressTopologyManifest.objects.get(pk=current_manifest_id).slot
        == WorkflowProgressTopologySlot.CURRENT
    )
    assert (
        WorkflowProgressTopologyManifest.objects.get(pk=next_manifest_id).slot
        == WorkflowProgressTopologySlot.PENDING
    )


@pytest.mark.django_db
def test_summary_failure_rolls_back_detail_upserts_and_manifest_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution()
    identity = _identity(execution)
    manifest_id = storage.stage_workflow_progress_topology(_topology(identity))
    assert manifest_id is not None
    details = (_detail(identity, "node-a"), _detail(identity, "node-b"))
    observed_in_transaction: dict[str, object] = {}

    def reject_summary(*args: object, **kwargs: object) -> bool:
        observed_in_transaction["detail_count"] = WorkflowProgressNodeDetail.objects.count()
        observed_in_transaction["manifest_slot"] = WorkflowProgressTopologyManifest.objects.get(
            pk=manifest_id
        ).slot
        return False

    monkeypatch.setattr(
        progress_module,
        "_assign_workflow_progress_summary_locked",
        reject_summary,
    )

    with pytest.raises(
        storage.WorkflowProgressStorageConflictError,
        match="lost ownership during atomic publication",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=1,
                node_states=("PENDING", "PENDING"),
                edge_count=1,
            ),
            manifest_id=manifest_id,
            detail_records=details,
        )

    assert observed_in_transaction == {
        "detail_count": 2,
        "manifest_slot": WorkflowProgressTopologySlot.CURRENT,
    }
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision is None
    assert run.detail_node_count == 0
    assert not WorkflowProgressNodeDetail.objects.exists()
    manifest = WorkflowProgressTopologyManifest.objects.get(pk=manifest_id)
    assert manifest.slot == WorkflowProgressTopologySlot.PENDING
    assert manifest.published_at is None
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json is None


@pytest.mark.django_db
def test_nonadvancing_summary_revision_rolls_back_a_sparse_detail_change() -> None:
    execution = _execution()
    identity, manifest_id, initial_details = _stage_and_publish(execution)
    changed = _detail(identity, "node-a", state="RUNNING", with_event=True)

    with pytest.raises(
        progress_module.WorkflowProgressSummaryConflictError,
        match="revision did not advance monotonically",
    ):
        storage.persist_workflow_progress_publication(
            identity,
            _summary(
                identity,
                summary_revision=1,
                node_states=("RUNNING", "PENDING"),
                edge_count=1,
                updated_at="2026-07-20T12:00:02Z",
            ),
            manifest_id=manifest_id,
            detail_records=(changed,),
        )

    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision == 1
    assert run.detail_event_count == 0
    row = WorkflowProgressNodeDetail.objects.get(node_id="node-a")
    assert bytes(row.payload) == initial_details[0].payload
    assert row.last_detail_revision == 1


@pytest.mark.django_db
def test_publication_stale_fence_rejects_without_mutating_candidate_or_run() -> None:
    execution = _execution()
    identity = _identity(execution)
    manifest_id = storage.stage_workflow_progress_topology(_topology(identity))
    assert manifest_id is not None
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        execution_generation=identity.execution_generation + 1
    )

    result = storage.persist_workflow_progress_publication(
        identity,
        _summary(
            identity,
            summary_revision=1,
            node_states=("PENDING", "PENDING"),
            edge_count=1,
        ),
        manifest_id=manifest_id,
        detail_records=(_detail(identity, "node-a"),),
    )

    assert not result.accepted
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_revision is None
    assert not WorkflowProgressNodeDetail.objects.exists()
    assert (
        WorkflowProgressTopologyManifest.objects.get(pk=manifest_id).slot
        == WorkflowProgressTopologySlot.PENDING
    )


@pytest.mark.django_db
def test_producer_terminal_publication_binds_retention_to_current_setting() -> None:
    execution = _execution()
    identity = _identity(execution)
    topology = _topology(identity, node_ids=("node-a",), edges=())
    manifest_id = storage.stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    producer_summary = _summary(
        identity,
        summary_revision=1,
        node_states=("SUCCEEDED",),
        edge_count=0,
        updated_at="2026-07-20T12:00:02Z",
        workflow_state="SUCCEEDED",
        detail_days=30,
    )

    with override_settings(
        DJANGO_RAY={
            "RAY_ADDRESS": "ray://localhost:10001",
            "WORKFLOW_PROGRESS_DETAIL_RETENTION_DAYS": 3,
        }
    ):
        result = storage.persist_workflow_progress_publication(
            identity,
            producer_summary,
            manifest_id=manifest_id,
            prepared_topology=topology,
            detail_records=(_detail(identity, "node-a", state="SUCCEEDED"),),
        )

    assert result.accepted
    assert result.summary is not None
    assert result.summary["state"] == "SUCCEEDED"
    assert result.summary["retention"] == {
        "detail_days": 3,
        "detail_expires_at": "2026-07-23T12:00:02Z",
    }
    run = WorkflowProgressRunStorage.objects.get()
    assert run.detail_retention_days == 3
    assert run.detail_expires_at is not None
    assert run.detail_expires_at.isoformat() == "2026-07-23T12:00:02+00:00"
    execution.refresh_from_db()
    persisted = json.loads(execution.workflow_progress_summary_json or "{}")
    assert persisted["retention"] == result.summary["retention"]
