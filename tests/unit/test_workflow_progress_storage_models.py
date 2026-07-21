"""Schema invariants for bounded normalized workflow-progress storage."""

from __future__ import annotations

import hashlib

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import RestrictedError
from django.utils import timezone

from django_ray.models import (
    RayTaskExecution,
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressNodeState,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyCollection,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)


def _execution(task_id: str = "workflow-storage-models") -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="tests.unit.test_workflows.increment",
        state=TaskState.RUNNING,
        attempt_number=2,
        execution_generation=3,
        workflow_run_id="00000000-0000-0000-0000-000000000126",
    )


def _run_storage(
    execution: RayTaskExecution,
    *,
    with_detail: bytes | None = None,
    detail_state: WorkflowProgressNodeState = WorkflowProgressNodeState.PENDING,
    detail_truncated: bool = False,
) -> WorkflowProgressRunStorage:
    has_detail = with_detail is not None
    size = len(with_detail) if has_detail else 0
    return WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id=execution.workflow_run_id,
        detail_revision=1 if has_detail else None,
        detail_node_count=1 if has_detail else 0,
        detail_pending_count=int(has_detail and detail_state == WorkflowProgressNodeState.PENDING),
        detail_running_count=int(has_detail and detail_state == WorkflowProgressNodeState.RUNNING),
        detail_succeeded_count=int(
            has_detail and detail_state == WorkflowProgressNodeState.SUCCEEDED
        ),
        detail_failed_count=int(has_detail and detail_state == WorkflowProgressNodeState.FAILED),
        detail_truncated_count=int(has_detail and detail_truncated),
        detail_event_count=1 if has_detail else 0,
        detail_truncation_reasons="RECORD_SIZE_LIMIT" if detail_truncated else "",
        detail_encoded_bytes=size,
        detail_decoded_bytes=size,
    )


def _page(run_storage: WorkflowProgressRunStorage, payload: bytes) -> WorkflowProgressTopologyPage:
    return WorkflowProgressTopologyPage.objects.create(
        run_storage=run_storage,
        digest=hashlib.sha256(payload).hexdigest(),
        collection=WorkflowProgressTopologyCollection.NODE,
        payload=payload,
        item_count=1,
        encoded_bytes=len(payload),
        decoded_bytes=len(payload),
    )


def _manifest(
    run_storage: WorkflowProgressRunStorage,
    *,
    version: int,
    slot: str,
    digest: str,
    encoded_bytes: int,
) -> WorkflowProgressTopologyManifest:
    return WorkflowProgressTopologyManifest.objects.create(
        run_storage=run_storage,
        topology_version=version,
        slot=slot,
        manifest_digest=digest,
        payload=b'{"pages":[]}',
        node_count=1,
        edge_count=0,
        node_page_count=1,
        edge_page_count=0,
        encoded_bytes=encoded_bytes,
        decoded_bytes=encoded_bytes,
        published_at=(timezone.now() if slot == WorkflowProgressTopologySlot.CURRENT else None),
    )


@pytest.mark.django_db
def test_run_scoped_pages_are_reusable_restricted_and_task_cascaded() -> None:
    assert set(WorkflowProgressNodeState.values) == {
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
    }
    detail_payload = b'{"node_id":"node-a","state":"RUNNING"}'
    topology_payload = b'{"items":[{"node_id":"node-a"}]}'
    execution = _execution()
    run_storage = _run_storage(
        execution,
        with_detail=detail_payload,
        detail_state=WorkflowProgressNodeState.RUNNING,
        detail_truncated=True,
    )
    page = _page(run_storage, topology_payload)
    current = _manifest(
        run_storage,
        version=1,
        slot=WorkflowProgressTopologySlot.CURRENT,
        digest="a" * 64,
        encoded_bytes=len(topology_payload),
    )
    pending = _manifest(
        run_storage,
        version=2,
        slot=WorkflowProgressTopologySlot.PENDING,
        digest="b" * 64,
        encoded_bytes=len(topology_payload),
    )
    for manifest in (current, pending):
        WorkflowProgressTopologyManifestPage.objects.create(
            manifest=manifest,
            page=page,
            collection=WorkflowProgressTopologyCollection.NODE,
            page_index=0,
        )
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=hashlib.sha256(b"node-a").hexdigest(),
        node_id="node-a",
        invocation_id="00000000-0000-0000-0000-000000000127",
        state=WorkflowProgressNodeState.RUNNING,
        event_count=1,
        truncated=True,
        payload=detail_payload,
        digest=hashlib.sha256(detail_payload).hexdigest(),
        encoded_bytes=len(detail_payload),
        decoded_bytes=len(detail_payload),
        last_topology_version=2,
        last_detail_revision=1,
    )
    assert run_storage.detail_truncation_reasons == "RECORD_SIZE_LIMIT"

    with pytest.raises(RestrictedError):
        page.delete()

    execution.delete()

    assert WorkflowProgressRunStorage.objects.count() == 0
    assert WorkflowProgressTopologyManifest.objects.count() == 0
    assert WorkflowProgressTopologyPage.objects.count() == 0
    assert WorkflowProgressTopologyManifestPage.objects.count() == 0
    assert WorkflowProgressNodeDetail.objects.count() == 0


@pytest.mark.django_db
def test_run_slot_and_node_keys_are_unique_without_partial_indexes() -> None:
    detail_payload = b'{"node_id":"node-a","state":"PENDING"}'
    topology_payload = b'{"items":[{"node_id":"node-a"}]}'
    execution = _execution("workflow-storage-uniqueness")
    run_storage = _run_storage(execution, with_detail=detail_payload)
    _manifest(
        run_storage,
        version=1,
        slot=WorkflowProgressTopologySlot.PENDING,
        digest="c" * 64,
        encoded_bytes=len(topology_payload),
    )
    node_key = hashlib.sha256(b"node-a").hexdigest()
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=node_key,
        node_id="node-a",
        state=WorkflowProgressNodeState.PENDING,
        event_count=1,
        payload=detail_payload,
        digest=hashlib.sha256(detail_payload).hexdigest(),
        encoded_bytes=len(detail_payload),
        decoded_bytes=len(detail_payload),
        last_topology_version=1,
        last_detail_revision=1,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.create(
            execution=execution,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
            run_id=execution.workflow_run_id,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        _manifest(
            run_storage,
            version=2,
            slot=WorkflowProgressTopologySlot.PENDING,
            digest="d" * 64,
            encoded_bytes=len(topology_payload),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressNodeDetail.objects.create(
            run_storage=run_storage,
            node_key=node_key,
            node_id="NODE-A",
            state=WorkflowProgressNodeState.PENDING,
            event_count=1,
            payload=detail_payload,
            digest=hashlib.sha256(detail_payload).hexdigest(),
            encoded_bytes=len(detail_payload),
            decoded_bytes=len(detail_payload),
            last_topology_version=1,
            last_detail_revision=1,
        )


@pytest.mark.django_db
def test_database_checks_reject_slot_and_byte_accounting_mismatches() -> None:
    execution = _execution("workflow-storage-checks")
    run_storage = _run_storage(execution)
    topology_payload = b'{"items":[{"node_id":"node-a"}]}'
    page = _page(run_storage, topology_payload)
    pending = _manifest(
        run_storage,
        version=1,
        slot=WorkflowProgressTopologySlot.PENDING,
        digest="e" * 64,
        encoded_bytes=len(topology_payload),
    )
    detail_payload = b'{"node_id":"node-a","state":"PENDING"}'
    detail = WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key=hashlib.sha256(b"node-a").hexdigest(),
        node_id="node-a",
        state=WorkflowProgressNodeState.PENDING,
        payload=detail_payload,
        digest=hashlib.sha256(detail_payload).hexdigest(),
        encoded_bytes=len(detail_payload),
        decoded_bytes=len(detail_payload),
        last_topology_version=1,
        last_detail_revision=1,
    )
    aggregate_payload = b'{"node_id":"node-b","state":"PENDING"}'
    aggregate_run = _run_storage(
        _execution("workflow-storage-aggregate-checks"),
        with_detail=aggregate_payload,
        detail_truncated=True,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressTopologyManifest.objects.filter(pk=pending.pk).update(
            published_at=timezone.now()
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressTopologyPage.objects.filter(pk=page.pk).update(
            decoded_bytes=len(topology_payload) + 1
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressNodeDetail.objects.filter(pk=detail.pk).update(event_count=33)
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
            detail_event_count=1,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
            detail_truncation_reasons="RECORD_SIZE_LIMIT",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
            detail_event_count=33,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
            detail_revision=1,
            detail_node_count=1,
            detail_encoded_bytes=1,
            detail_decoded_bytes=2,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.filter(pk=aggregate_run.pk).update(
            detail_pending_count=0
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.filter(pk=aggregate_run.pk).update(
            detail_truncated_count=2
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
            detail_retention_days=31
        )

    WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
        detail_revision=1,
        detail_truncation_reasons="DETAIL_COUNT_LIMIT",
    )
    run_storage.refresh_from_db()
    assert run_storage.detail_truncation_reasons == "DETAIL_COUNT_LIMIT"


@pytest.mark.django_db
def test_binary_payload_and_digest_field_validators_are_bounded() -> None:
    execution = _execution("workflow-storage-validation")
    run_storage = _run_storage(execution)
    invalid_aggregates = WorkflowProgressRunStorage(
        execution=execution,
        attempt_number=execution.attempt_number,
        execution_generation=execution.execution_generation,
        run_id="00000000-0000-0000-0000-000000000128",
        detail_pending_count=25_001,
        detail_truncated_count=25_001,
        detail_event_count=33,
        detail_truncation_reasons="X" * 257,
        detail_retention_days=31,
    )
    oversized = WorkflowProgressTopologyPage(
        run_storage=run_storage,
        digest="A" * 64,
        collection=WorkflowProgressTopologyCollection.NODE,
        payload=b"x" * (256 * 1024 + 1),
        item_count=1,
        encoded_bytes=256 * 1024,
        decoded_bytes=256 * 1024,
    )
    too_many_node_events = WorkflowProgressNodeDetail(
        run_storage=run_storage,
        node_key=hashlib.sha256(b"node-events").hexdigest(),
        node_id="node-events",
        state=WorkflowProgressNodeState.PENDING,
        event_count=33,
        payload=b"{}",
        digest=hashlib.sha256(b"{}").hexdigest(),
        encoded_bytes=2,
        decoded_bytes=2,
        last_topology_version=1,
        last_detail_revision=1,
    )
    oversized_manifest_reasons = _manifest(
        run_storage,
        version=1,
        slot=WorkflowProgressTopologySlot.PENDING,
        digest="f" * 64,
        encoded_bytes=2,
    )
    oversized_manifest_reasons.truncation_reasons = "X" * 257

    with pytest.raises(ValidationError) as event_error:
        invalid_aggregates.full_clean(validate_constraints=False)
    with pytest.raises(ValidationError) as error:
        oversized.full_clean(validate_constraints=False)
    with pytest.raises(ValidationError) as node_event_error:
        too_many_node_events.full_clean(validate_constraints=False)
    with pytest.raises(ValidationError) as manifest_reason_error:
        oversized_manifest_reasons.full_clean(validate_constraints=False)

    assert {
        "detail_pending_count",
        "detail_truncated_count",
        "detail_event_count",
        "detail_truncation_reasons",
        "detail_retention_days",
    } <= set(event_error.value.message_dict)
    assert {"digest", "payload"} <= set(error.value.message_dict)
    assert {"event_count"} <= set(node_event_error.value.message_dict)
    assert {"truncation_reasons"} <= set(manifest_reason_error.value.message_dict)
