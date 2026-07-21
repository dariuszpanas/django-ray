"""Whole-run integrity audits for normalized workflow-progress detail."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_ray.lifecycle import succeed_task
from django_ray.models import WorkflowProgressNodeDetail, WorkflowProgressRunStorage
from django_ray.workflow_progress_storage import (
    WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS,
    WorkflowProgressStorageIntegrityError,
    audit_workflow_progress_detail_storage,
    persist_workflow_progress_publication,
    prepare_workflow_progress_node_detail,
)
from django_ray.workflow_progress_summary import (
    deserialize_workflow_progress_summary,
    serialize_workflow_progress_summary,
)
from tests.workflow_progress_storage_helpers import (
    PublishedWorkflow,
    publish_initial_workflow,
    workflow_detail,
    workflow_summary,
)


def _run_storage(published: PublishedWorkflow) -> WorkflowProgressRunStorage:
    return WorkflowProgressRunStorage.objects.get(
        execution=published.execution,
        attempt_number=published.identity.attempt_number,
        execution_generation=published.identity.execution_generation,
        run_id=published.identity.run_id,
    )


def _events(count: int) -> list[dict[str, object]]:
    return [
        {
            "event": f"event-{index:02d}",
            "state": "PENDING",
            "label": f"Event {index:02d}",
            "timestamp": f"2026-07-20T12:00:{index:02d}Z",
        }
        for index in range(count)
    ]


@pytest.mark.django_db
def test_audit_returns_deterministic_verified_aggregate_evidence() -> None:
    published = publish_initial_workflow(2, case_id=210)
    run_storage = _run_storage(published)

    with CaptureQueriesContext(connection) as captured:
        result = audit_workflow_progress_detail_storage(published.identity)

    assert result.run_storage_id == run_storage.pk
    assert result.topology_version == 1
    assert result.detail_revision == 1
    assert result.node_count == 2
    assert result.encoded_bytes == run_storage.detail_encoded_bytes
    assert result.decoded_bytes == run_storage.detail_decoded_bytes
    assert result.event_count == 0
    assert result.truncated_count == 0
    assert result.state_counts == (
        ("PENDING", 2),
        ("RUNNING", 0),
        ("SUCCEEDED", 0),
        ("FAILED", 0),
    )
    locked_tables = [
        query["sql"].upper()
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
    ]
    task_index = next(index for index, sql in enumerate(locked_tables) if "RAYTASKEXECUTION" in sql)
    run_index = next(
        index for index, sql in enumerate(locked_tables) if "WORKFLOWPROGRESSRUNSTORAGE" in sql
    )
    assert task_index < run_index


@pytest.mark.django_db
def test_audit_binds_an_active_run_to_its_canonical_task_summary() -> None:
    published = publish_initial_workflow(1, case_id=215)
    published.execution.refresh_from_db()
    summary = deserialize_workflow_progress_summary(
        published.execution.workflow_progress_summary_json,
        expected_identity=published.identity,
    )
    summary["summary_revision"] = 2
    summary["detail_revision"] = 2
    summary["timestamps"]["updated_at"] = "2026-07-20T12:00:02Z"
    published.execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
        summary,
        expected_identity=published.identity,
    )
    published.execution.save(update_fields=["workflow_progress_summary_json"])

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="active summary conflicts with storage",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_rejects_extra_canonical_active_summary_truncation_reason() -> None:
    published = publish_initial_workflow(1, case_id=217)
    published.execution.refresh_from_db()
    summary = deserialize_workflow_progress_summary(
        published.execution.workflow_progress_summary_json,
        expected_identity=published.identity,
    )
    summary["summary_revision"] = 2
    summary["reporting_policy"] = "sampled"
    summary["timestamps"]["updated_at"] = "2026-07-20T12:00:02Z"
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["reporting_policy"],
    }
    published.execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
        summary,
        expected_identity=published.identity,
    )
    published.execution.save(update_fields=["workflow_progress_summary_json"])

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="active summary conflicts with storage",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_rejects_retained_state_count_above_truncated_active_summary() -> None:
    published = publish_initial_workflow(1, case_id=218)
    published.execution.refresh_from_db()
    summary = deserialize_workflow_progress_summary(
        published.execution.workflow_progress_summary_json,
        expected_identity=published.identity,
    )
    summary["summary_revision"] = 2
    summary["reporting_policy"] = "sampled"
    summary["timestamps"]["updated_at"] = "2026-07-20T12:00:02Z"
    summary["node_counts"]["pending"] = 0
    summary["node_counts"]["running"] = 1
    summary["detail"] = {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["reporting_policy"],
    }
    published.execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
        summary,
        expected_identity=published.identity,
    )
    published.execution.save(update_fields=["workflow_progress_summary_json"])
    WorkflowProgressRunStorage.objects.filter(pk=_run_storage(published).pk).update(
        detail_truncation_reasons="reporting_policy"
    )

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="retained state counts conflict with active summary",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_accepts_last_observed_detail_after_lifecycle_success() -> None:
    published = publish_initial_workflow(2, case_id=2181)

    assert succeed_task(
        published.execution,
        result_data="{}",
        result_reference=None,
    )

    result = audit_workflow_progress_detail_storage(published.identity)

    assert result.node_count == 2
    assert result.state_counts == (
        ("PENDING", 2),
        ("RUNNING", 0),
        ("SUCCEEDED", 0),
        ("FAILED", 0),
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "run_update",
    [
        {"detail_retention_days": 8},
        {"detail_expires_at": datetime(2026, 7, 27, 12, 0, 1, tzinfo=UTC)},
    ],
)
def test_audit_binds_active_summary_retention_to_run_storage(
    run_update: dict[str, object],
) -> None:
    published = publish_initial_workflow(1, case_id=219)
    WorkflowProgressRunStorage.objects.filter(pk=_run_storage(published).pk).update(**run_update)

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="active summary conflicts with storage",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_historical_audit_uses_exact_run_local_evidence_after_task_reuse() -> None:
    published = publish_initial_workflow(1, case_id=216)
    published.execution.attempt_number = 2
    published.execution.execution_generation = 2
    published.execution.workflow_run_id = None
    published.execution.workflow_progress_summary_json = None
    published.execution.save(
        update_fields=[
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_progress_summary_json",
        ]
    )

    result = audit_workflow_progress_detail_storage(published.identity)

    assert result.detail_revision == 1
    assert result.node_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda row: WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(
                payload=b"x" * row.encoded_bytes
            ),
            "metadata is invalid",
        ),
        (
            lambda row: WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(
                digest="0" * 64
            ),
            "metadata is invalid",
        ),
        (
            lambda row: WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(
                state="RUNNING"
            ),
            "not normalized",
        ),
        (
            lambda row: WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(
                truncated=not row.truncated
            ),
            "not normalized",
        ),
    ],
)
def test_audit_rejects_payload_digest_and_normalized_metadata_corruption(
    mutation: Callable[[WorkflowProgressNodeDetail], object],
    message: str,
) -> None:
    published = publish_initial_workflow(1, case_id=220)
    row = WorkflowProgressNodeDetail.objects.get()
    mutation(row)

    with pytest.raises(WorkflowProgressStorageIntegrityError, match=message):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_rejects_detail_fanout_that_conflicts_with_topology_kind() -> None:
    published = publish_initial_workflow(1, case_id=225)
    row = WorkflowProgressNodeDetail.objects.get()
    detail = workflow_detail(row.node_id)
    detail["fanout"] = {
        "max_concurrency": 1,
        "max_items": 1,
        "submitted_items": 0,
        "completed_items": 0,
        "in_flight_items": 0,
        "input_exhausted": False,
    }
    replacement = prepare_workflow_progress_node_detail(
        detail,
        identity=published.identity,
    )
    WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(
        payload=replacement.payload,
        digest=replacement.digest,
        encoded_bytes=replacement.encoded_bytes,
        decoded_bytes=replacement.decoded_bytes,
        event_count=replacement.event_count,
        truncated=replacement.truncated,
    )
    WorkflowProgressRunStorage.objects.filter(pk=_run_storage(published).pk).update(
        detail_encoded_bytes=replacement.encoded_bytes,
        detail_decoded_bytes=replacement.decoded_bytes,
    )

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="detail fanout that conflicts with the current topology",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"detail_node_count": 1, "detail_pending_count": 1},
            "row count does not match",
        ),
        (
            {"detail_pending_count": 1, "detail_running_count": 1},
            "state counts do not match",
        ),
        ({"detail_truncated_count": 1}, "truncated count does not match"),
        ({"detail_event_count": 1}, "event count does not match"),
    ],
)
def test_audit_rejects_in_range_run_count_state_truncation_and_event_tamper(
    updates: dict[str, int],
    message: str,
) -> None:
    published = publish_initial_workflow(2, case_id=230)
    run_storage = _run_storage(published)
    WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(**updates)

    with pytest.raises(WorkflowProgressStorageIntegrityError, match=message):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_rejects_in_range_encoded_and_decoded_aggregate_tamper() -> None:
    published = publish_initial_workflow(2, case_id=240)
    run_storage = _run_storage(published)
    tampered_bytes = int(run_storage.detail_encoded_bytes) + 1
    WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
        detail_encoded_bytes=tampered_bytes,
        detail_decoded_bytes=tampered_bytes,
    )

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="encoded bytes do not match",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_detects_a_missing_untouched_row() -> None:
    published = publish_initial_workflow(2, case_id=250)
    removed = WorkflowProgressNodeDetail.objects.order_by("node_key").first()
    assert removed is not None
    removed.delete()
    remaining = WorkflowProgressNodeDetail.objects.get()
    WorkflowProgressRunStorage.objects.filter(pk=_run_storage(published).pk).update(
        detail_node_count=1,
        detail_pending_count=1,
        detail_encoded_bytes=remaining.encoded_bytes,
        detail_decoded_bytes=remaining.decoded_bytes,
    )

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="missing detail without truncation evidence",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_detects_an_extra_untouched_row_even_when_aggregates_still_match() -> None:
    published = publish_initial_workflow(2, case_id=260)
    removed = WorkflowProgressNodeDetail.objects.order_by("node_key").first()
    assert removed is not None
    replacement = prepare_workflow_progress_node_detail(
        workflow_detail("node-99999"),
        identity=published.identity,
    )
    assert replacement.encoded_bytes == removed.encoded_bytes
    removed.delete()
    WorkflowProgressNodeDetail.objects.create(
        run_storage=_run_storage(published),
        node_key=replacement.node_key,
        node_id=replacement.node_id,
        invocation_id=replacement.invocation_id,
        state=replacement.state,
        event_count=replacement.event_count,
        truncated=replacement.truncated,
        payload=replacement.payload,
        digest=replacement.digest,
        encoded_bytes=replacement.encoded_bytes,
        decoded_bytes=replacement.decoded_bytes,
        last_topology_version=1,
        last_detail_revision=1,
    )

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="outside the current topology",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
@pytest.mark.parametrize("epoch_field", ["last_topology_version", "last_detail_revision"])
def test_audit_rejects_a_future_row_epoch(epoch_field: str) -> None:
    published = publish_initial_workflow(1, case_id=270)
    WorkflowProgressNodeDetail.objects.update(**{epoch_field: 2})

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="publication epochs are invalid",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
@pytest.mark.parametrize("epoch_field", ["last_topology_version", "last_detail_revision"])
def test_sparse_touched_row_verification_rejects_a_future_epoch(epoch_field: str) -> None:
    published = publish_initial_workflow(1, case_id=280)
    WorkflowProgressNodeDetail.objects.update(**{epoch_field: 2})
    record = prepare_workflow_progress_node_detail(
        workflow_detail("node-00000"),
        identity=published.identity,
    )

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="publication epochs are invalid",
    ):
        persist_workflow_progress_publication(
            published.identity,
            workflow_summary(
                published.identity,
                summary_revision=2,
                node_count=1,
                running_count=0,
            ),
            manifest_id=published.manifest_id,
            prepared_topology=published.topology,
            detail_records=[record],
        )


@pytest.mark.django_db
def test_audit_rejects_more_than_thirty_two_valid_events_across_rows() -> None:
    published = publish_initial_workflow(2, case_id=290)
    for row in WorkflowProgressNodeDetail.objects.order_by("node_key"):
        value = workflow_detail(row.node_id)
        value["recent_events"] = _events(20)
        replacement = prepare_workflow_progress_node_detail(
            value,
            identity=published.identity,
        )
        WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(
            payload=replacement.payload,
            digest=replacement.digest,
            encoded_bytes=replacement.encoded_bytes,
            decoded_bytes=replacement.decoded_bytes,
            event_count=replacement.event_count,
            truncated=replacement.truncated,
        )

    with pytest.raises(
        WorkflowProgressStorageIntegrityError,
        match="run-global event bound",
    ):
        audit_workflow_progress_detail_storage(published.identity)


@pytest.mark.django_db
def test_audit_query_is_capped_at_hard_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publish_initial_workflow(4, case_id=300)
    run_storage = _run_storage(published)
    WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).update(
        detail_node_count=2,
        detail_pending_count=2,
    )
    monkeypatch.setattr(
        "django_ray.workflow_progress_storage.WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS",
        2,
    )

    with CaptureQueriesContext(connection) as captured:
        with pytest.raises(
            WorkflowProgressStorageIntegrityError,
            match="retained-node limit",
        ):
            audit_workflow_progress_detail_storage(published.identity)

    detail_queries = [
        query["sql"].upper()
        for query in captured.captured_queries
        if "WORKFLOWPROGRESSNODEDETAIL" in query["sql"].upper()
    ]
    assert len(detail_queries) == 1
    assert "LIMIT 3" in detail_queries[0]
    assert WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS == 25_000


@pytest.mark.django_db
def test_audit_command_is_read_only_and_reports_deterministic_evidence() -> None:
    published = publish_initial_workflow(1, case_id=310)
    stdout = StringIO()

    call_command(
        "django_ray_audit_workflow_progress",
        task_execution_pk=published.identity.task_execution_pk,
        attempt_number=published.identity.attempt_number,
        execution_generation=published.identity.execution_generation,
        run_id=published.identity.run_id,
        stdout=stdout,
    )

    output = stdout.getvalue()
    assert "Workflow progress detail audit passed:" in output
    assert "topology_version=1 detail_revision=1 nodes=1" in output
    assert "states=PENDING:1,RUNNING:0,SUCCEEDED:0,FAILED:0." in output


@pytest.mark.django_db
def test_audit_command_fails_without_repairing_or_recording_corruption() -> None:
    published = publish_initial_workflow(1, case_id=320)
    row = WorkflowProgressNodeDetail.objects.get()
    WorkflowProgressNodeDetail.objects.filter(pk=row.pk).update(digest="0" * 64)

    with pytest.raises(CommandError, match="detail audit failed"):
        call_command(
            "django_ray_audit_workflow_progress",
            task_execution_pk=published.identity.task_execution_pk,
            attempt_number=published.identity.attempt_number,
            execution_generation=published.identity.execution_generation,
            run_id=published.identity.run_id,
        )

    row.refresh_from_db()
    assert row.digest == "0" * 64
    assert _run_storage(published).cleanup_error is None


@pytest.mark.django_db
def test_audit_command_requires_the_complete_exact_run_identity() -> None:
    published = publish_initial_workflow(1, case_id=330)

    with pytest.raises(CommandError, match="audit run is missing"):
        call_command(
            "django_ray_audit_workflow_progress",
            task_execution_pk=published.identity.task_execution_pk,
            attempt_number=published.identity.attempt_number,
            execution_generation=published.identity.execution_generation,
            run_id="00000000-0000-0000-0000-000000000999",
        )
