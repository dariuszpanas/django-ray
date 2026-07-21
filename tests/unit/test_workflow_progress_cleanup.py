"""Retention and orphan cleanup coverage for workflow-progress storage."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import pytest
from django.utils import timezone

import django_ray.workflow_progress_cleanup as cleanup_module
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
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
from django_ray.workflow_progress_cleanup import (
    WORKFLOW_PROGRESS_ORPHAN_GRACE_PERIOD,
    WorkflowProgressCleanupKind,
    WorkflowProgressCleanupOutcome,
    cleanup_workflow_progress_storage,
)

NOW = timezone.now().replace(microsecond=0)


def _execution(
    suffix: str,
    *,
    state: str = TaskState.RUNNING,
    attempt_number: int = 2,
    execution_generation: int = 5,
    run_id: UUID | None = None,
    summary: str | None = None,
) -> RayTaskExecution:
    return RayTaskExecution.objects.create(
        task_id=f"workflow-cleanup-{suffix}",
        callable_path="tests.unit.test_workflows.increment",
        state=state,
        attempt_number=attempt_number,
        execution_generation=execution_generation,
        workflow_run_id=run_id,
        workflow_progress_summary_json=summary,
        finished_at=NOW if state in {TaskState.SUCCEEDED, TaskState.FAILED} else None,
    )


def _run(
    execution: RayTaskExecution,
    *,
    run_id: UUID | None = None,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
    expires_at=None,
) -> WorkflowProgressRunStorage:
    return WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=attempt_number or execution.attempt_number,
        execution_generation=(
            execution.execution_generation if execution_generation is None else execution_generation
        ),
        run_id=run_id or uuid4(),
        detail_expires_at=expires_at,
    )


def _manifest(
    run_storage: WorkflowProgressRunStorage,
    *,
    slot: str,
    created_at,
    version: int,
    node_page_count: int = 0,
) -> WorkflowProgressTopologyManifest:
    payload = f'{{"topology_version":{version}}}'.encode()
    return WorkflowProgressTopologyManifest.objects.create(
        run_storage=run_storage,
        topology_version=version,
        slot=slot,
        manifest_digest=sha256(payload).hexdigest(),
        payload=payload,
        node_count=node_page_count,
        edge_count=0,
        node_page_count=node_page_count,
        edge_page_count=0,
        encoded_bytes=len(payload),
        decoded_bytes=len(payload),
        created_at=created_at,
        published_at=NOW if slot == WorkflowProgressTopologySlot.CURRENT else None,
    )


def _page(
    run_storage: WorkflowProgressRunStorage,
    suffix: str,
    *,
    created_at,
) -> WorkflowProgressTopologyPage:
    payload = f'{{"node":"{suffix}"}}'.encode()
    return WorkflowProgressTopologyPage.objects.create(
        run_storage=run_storage,
        digest=sha256(payload).hexdigest(),
        collection=WorkflowProgressTopologyCollection.NODE,
        payload=payload,
        item_count=1,
        encoded_bytes=len(payload),
        decoded_bytes=len(payload),
        created_at=created_at,
    )


@pytest.mark.django_db
def test_cleanup_is_a_non_mutating_dry_run_by_default() -> None:
    execution = _execution("dry-run", state=TaskState.FAILED, summary="task summary")
    run_storage = _run(execution, expires_at=NOW - timedelta(seconds=1))

    report = cleanup_workflow_progress_storage(now=NOW)

    assert report.delete is False
    assert report.eligible_count == 1
    assert report.deleted_count == 0
    assert report.items[0].kind is WorkflowProgressCleanupKind.EXPIRED_RUN
    assert report.items[0].outcome is WorkflowProgressCleanupOutcome.ELIGIBLE
    assert WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    execution.refresh_from_db()
    assert execution.workflow_progress_summary_json == "task summary"


@pytest.mark.django_db
def test_exact_active_current_run_is_never_deleted_but_an_inactive_fence_is() -> None:
    active_run_id = uuid4()
    execution = _execution("active", run_id=active_run_id)
    active = _run(
        execution,
        run_id=active_run_id,
        expires_at=NOW - timedelta(days=2),
    )
    inactive = _run(
        execution,
        run_id=active_run_id,
        execution_generation=cast(int, execution.execution_generation) - 1,
        expires_at=NOW - timedelta(days=1),
    )

    report = cleanup_workflow_progress_storage(delete=True, batch_size=1, now=NOW)

    assert report.deleted_count == 1
    assert report.skipped_count == 0
    assert WorkflowProgressRunStorage.objects.filter(pk=active.pk).exists()
    assert not WorkflowProgressRunStorage.objects.filter(pk=inactive.pk).exists()


@pytest.mark.django_db
def test_terminal_run_delete_preserves_task_and_attempt_summaries() -> None:
    run_id = uuid4()
    execution = _execution(
        "terminal",
        state=TaskState.SUCCEEDED,
        run_id=run_id,
        summary='{"task":"summary"}',
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=execution.attempt_number,
        state=TaskState.SUCCEEDED,
        finished_at=NOW,
        workflow_progress_summary_json='{"attempt":"summary"}',
    )
    run_storage = _run(
        execution,
        run_id=run_id,
        expires_at=NOW,
    )
    current = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.CURRENT,
        created_at=NOW,
        version=1,
        node_page_count=1,
    )
    page = _page(run_storage, "terminal", created_at=NOW)
    WorkflowProgressTopologyManifestPage.objects.create(
        manifest=current,
        page=page,
        collection=WorkflowProgressTopologyCollection.NODE,
        page_index=0,
    )

    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert report.deleted_count == 1
    assert not WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    assert not WorkflowProgressTopologyManifest.objects.filter(pk=current.pk).exists()
    assert not WorkflowProgressTopologyPage.objects.filter(pk=page.pk).exists()
    execution.refresh_from_db()
    attempt.refresh_from_db()
    assert execution.workflow_progress_summary_json == '{"task":"summary"}'
    assert attempt.workflow_progress_summary_json == '{"attempt":"summary"}'
    assert execution.state == TaskState.SUCCEEDED
    assert attempt.state == TaskState.SUCCEEDED


@pytest.mark.django_db
def test_cleanup_deletes_only_stale_pending_manifest() -> None:
    execution = _execution("manifests")
    run_storage = _run(execution)
    stale_pending = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.PENDING,
        created_at=NOW - WORKFLOW_PROGRESS_ORPHAN_GRACE_PERIOD,
        version=2,
    )
    current = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.CURRENT,
        created_at=NOW - timedelta(days=1),
        version=1,
    )

    preview = cleanup_workflow_progress_storage(now=NOW)
    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert (
        preview.count(
            WorkflowProgressCleanupKind.PENDING_MANIFEST,
            WorkflowProgressCleanupOutcome.ELIGIBLE,
        )
        == 1
    )
    assert report.count(WorkflowProgressCleanupKind.PENDING_MANIFEST) == 1
    assert not WorkflowProgressTopologyManifest.objects.filter(pk=stale_pending.pk).exists()
    assert WorkflowProgressTopologyManifest.objects.filter(pk=current.pk).exists()


@pytest.mark.django_db
def test_recent_pending_manifest_is_not_a_candidate() -> None:
    execution = _execution("recent-manifest")
    run_storage = _run(execution)
    pending = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.PENDING,
        created_at=NOW - WORKFLOW_PROGRESS_ORPHAN_GRACE_PERIOD + timedelta(seconds=1),
        version=1,
    )

    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert report.items == ()
    assert WorkflowProgressTopologyManifest.objects.filter(pk=pending.pk).exists()


@pytest.mark.django_db
def test_cleanup_deletes_only_old_unreferenced_pages() -> None:
    execution = _execution("pages")
    run_storage = _run(execution)
    current = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.CURRENT,
        created_at=NOW - timedelta(days=1),
        version=1,
        node_page_count=1,
    )
    referenced = _page(run_storage, "referenced", created_at=NOW - timedelta(days=1))
    WorkflowProgressTopologyManifestPage.objects.create(
        manifest=current,
        page=referenced,
        collection=WorkflowProgressTopologyCollection.NODE,
        page_index=0,
    )
    orphan = _page(
        run_storage,
        "orphan",
        created_at=NOW - WORKFLOW_PROGRESS_ORPHAN_GRACE_PERIOD,
    )
    recent = _page(run_storage, "recent", created_at=NOW - timedelta(minutes=59))

    preview = cleanup_workflow_progress_storage(now=NOW)
    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert (
        preview.count(
            WorkflowProgressCleanupKind.ORPHAN_PAGE,
            WorkflowProgressCleanupOutcome.ELIGIBLE,
        )
        == 1
    )
    assert report.count(WorkflowProgressCleanupKind.ORPHAN_PAGE) == 1
    assert not WorkflowProgressTopologyPage.objects.filter(pk=orphan.pk).exists()
    assert WorkflowProgressTopologyPage.objects.filter(pk=referenced.pk).exists()
    assert WorkflowProgressTopologyPage.objects.filter(pk=recent.pk).exists()
    assert WorkflowProgressTopologyManifestPage.objects.filter(page=referenced).exists()


@pytest.mark.django_db
def test_stale_pending_manifest_pages_become_orphans_in_the_same_pass() -> None:
    execution = _execution("pending-page")
    run_storage = _run(execution)
    pending = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.PENDING,
        created_at=NOW - timedelta(hours=2),
        version=1,
        node_page_count=1,
    )
    page = _page(run_storage, "pending", created_at=NOW - timedelta(hours=2))
    WorkflowProgressTopologyManifestPage.objects.create(
        manifest=pending,
        page=page,
        collection=WorkflowProgressTopologyCollection.NODE,
        page_index=0,
    )

    first = cleanup_workflow_progress_storage(delete=True, now=NOW)
    second = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert first.deleted_count == 3
    assert not WorkflowProgressTopologyManifest.objects.filter(pk=pending.pk).exists()
    assert not WorkflowProgressTopologyPage.objects.filter(pk=page.pk).exists()
    assert not WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    assert second.items == ()


@pytest.mark.django_db
def test_zero_page_stale_pending_manifest_releases_its_empty_inactive_run() -> None:
    execution = _execution("pending-zero-page")
    run_storage = _run(execution)
    pending = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.PENDING,
        created_at=NOW - timedelta(hours=2),
        version=1,
    )

    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert (
        report.count(
            WorkflowProgressCleanupKind.PENDING_MANIFEST,
            WorkflowProgressCleanupOutcome.DELETED,
        )
        == 1
    )
    assert (
        report.count(
            WorkflowProgressCleanupKind.EMPTY_RUN,
            WorkflowProgressCleanupOutcome.DELETED,
        )
        == 1
    )
    assert not WorkflowProgressTopologyManifest.objects.filter(pk=pending.pk).exists()
    assert not WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()


@pytest.mark.django_db
def test_cleanup_collects_an_empty_inactive_run_left_by_discard() -> None:
    execution = _execution("discarded-empty")
    run_storage = _run(execution, expires_at=NOW + timedelta(days=1))

    preview = cleanup_workflow_progress_storage(now=NOW)
    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert (
        preview.count(
            WorkflowProgressCleanupKind.EMPTY_RUN,
            WorkflowProgressCleanupOutcome.ELIGIBLE,
        )
        == 1
    )
    assert (
        report.count(
            WorkflowProgressCleanupKind.EMPTY_RUN,
            WorkflowProgressCleanupOutcome.DELETED,
        )
        == 1
    )
    assert not WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()


@pytest.mark.django_db
def test_empty_run_cleanup_preserves_active_or_nonempty_storage() -> None:
    active_run_id = uuid4()
    active_execution = _execution("empty-active", run_id=active_run_id)
    active = _run(active_execution, run_id=active_run_id)

    revision_execution = _execution("empty-revision")
    revision = _run(revision_execution)
    revision.detail_revision = 1
    revision.save(update_fields=["detail_revision"])

    manifest_execution = _execution("empty-manifest")
    manifest_run = _run(manifest_execution)
    _manifest(
        manifest_run,
        slot=WorkflowProgressTopologySlot.PENDING,
        created_at=NOW,
        version=1,
    )

    page_execution = _execution("empty-page")
    page_run = _run(page_execution)
    _page(page_run, "still-present", created_at=NOW)

    node_execution = _execution("empty-node")
    node_run = _run(node_execution)
    node_payload = b'{"node_id":"still-present"}'
    WorkflowProgressNodeDetail.objects.create(
        run_storage=node_run,
        node_key=sha256(b"still-present").hexdigest(),
        node_id="still-present",
        state=WorkflowProgressNodeState.PENDING,
        payload=node_payload,
        digest=sha256(node_payload).hexdigest(),
        encoded_bytes=len(node_payload),
        decoded_bytes=len(node_payload),
        last_topology_version=1,
        last_detail_revision=1,
    )

    results = (
        cleanup_module._process_empty_run(
            active_execution.pk,
            active.pk,
            delete=True,
            using="default",
        ),
        cleanup_module._process_empty_run(
            revision_execution.pk,
            revision.pk,
            delete=True,
            using="default",
        ),
        cleanup_module._process_empty_run(
            manifest_execution.pk,
            manifest_run.pk,
            delete=True,
            using="default",
        ),
        cleanup_module._process_empty_run(
            page_execution.pk,
            page_run.pk,
            delete=True,
            using="default",
        ),
        cleanup_module._process_empty_run(
            node_execution.pk,
            node_run.pk,
            delete=True,
            using="default",
        ),
    )

    assert {result.outcome for result in results} == {WorkflowProgressCleanupOutcome.SKIPPED}
    assert (
        WorkflowProgressRunStorage.objects.filter(
            pk__in=[active.pk, revision.pk, manifest_run.pk, page_run.pk, node_run.pk]
        ).count()
        == 5
    )


@pytest.mark.django_db
def test_batch_limit_is_applied_per_candidate_class() -> None:
    runs = []
    for index in range(3):
        execution = _execution(f"batch-{index}", state=TaskState.FAILED)
        runs.append(_run(execution, expires_at=NOW - timedelta(minutes=1)))

    report = cleanup_workflow_progress_storage(delete=True, batch_size=2, now=NOW)

    assert report.count(WorkflowProgressCleanupKind.EXPIRED_RUN) == 2
    assert WorkflowProgressRunStorage.objects.count() == 1
    assert WorkflowProgressRunStorage.objects.filter(pk=runs[-1].pk).exists()


@pytest.mark.django_db
def test_one_failure_is_redacted_recorded_and_does_not_stop_later_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_execution = _execution("failed-delete", state=TaskState.FAILED)
    failed_run = _run(failed_execution, expires_at=NOW - timedelta(minutes=1))
    good_execution = _execution("good-delete", state=TaskState.FAILED)
    good_run = _run(good_execution, expires_at=NOW - timedelta(minutes=1))
    original_delete = WorkflowProgressRunStorage.delete

    def fail_one(self, *args, **kwargs):
        if self.pk == failed_run.pk:
            raise RuntimeError("database secret must not be retained")
        return original_delete(self, *args, **kwargs)

    monkeypatch.setattr(WorkflowProgressRunStorage, "delete", fail_one)

    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert report.failed_count == 1
    assert report.deleted_count == 1
    failed_run.refresh_from_db()
    assert failed_run.cleanup_error == (
        "EXPIRED_RUN_DELETE_FAILED: RuntimeError; message=<redacted>"
    )
    cleanup_error = cast(str, failed_run.cleanup_error)
    assert "database secret" not in cleanup_error
    assert len(cleanup_error) <= 2000
    assert not WorkflowProgressRunStorage.objects.filter(pk=good_run.pk).exists()


@pytest.mark.django_db
def test_failed_oldest_empty_run_does_not_starve_later_batch_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_execution = _execution("empty-failure-oldest")
    failed_run = _run(failed_execution)
    good_execution = _execution("empty-failure-newer")
    good_run = _run(good_execution)
    WorkflowProgressRunStorage.objects.filter(pk=failed_run.pk).update(
        created_at=NOW - timedelta(minutes=2)
    )
    WorkflowProgressRunStorage.objects.filter(pk=good_run.pk).update(
        created_at=NOW - timedelta(minutes=1)
    )
    original_delete = WorkflowProgressRunStorage.delete

    def fail_oldest(self, *args, **kwargs):
        if self.pk == failed_run.pk:
            raise RuntimeError("permanent storage failure")
        return original_delete(self, *args, **kwargs)

    monkeypatch.setattr(WorkflowProgressRunStorage, "delete", fail_oldest)

    first = cleanup_workflow_progress_storage(delete=True, batch_size=1, now=NOW)
    second = cleanup_workflow_progress_storage(delete=True, batch_size=1, now=NOW)
    third = cleanup_workflow_progress_storage(delete=True, batch_size=1, now=NOW)

    assert (
        first.count(
            WorkflowProgressCleanupKind.EMPTY_RUN,
            WorkflowProgressCleanupOutcome.FAILED,
        )
        == 1
    )
    assert (
        second.count(
            WorkflowProgressCleanupKind.EMPTY_RUN,
            WorkflowProgressCleanupOutcome.DELETED,
        )
        == 1
    )
    assert (
        third.count(
            WorkflowProgressCleanupKind.EMPTY_RUN,
            WorkflowProgressCleanupOutcome.FAILED,
        )
        == 1
    )
    assert not WorkflowProgressRunStorage.objects.filter(pk=good_run.pk).exists()
    failed_run.refresh_from_db()
    assert failed_run.cleanup_error == ("EMPTY_RUN_DELETE_FAILED: RuntimeError; message=<redacted>")


@pytest.mark.django_db
def test_pending_manifest_failure_is_redacted_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution("manifest-failure")
    run_storage = _run(execution)
    manifest = _manifest(
        run_storage,
        slot=WorkflowProgressTopologySlot.PENDING,
        created_at=NOW - timedelta(hours=2),
        version=1,
    )

    def fail_delete(self, *args, **kwargs):
        raise RuntimeError("token=must-not-be-retained")

    monkeypatch.setattr(WorkflowProgressTopologyManifest, "delete", fail_delete)

    report = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert report.failed_count == 1
    manifest.refresh_from_db()
    assert manifest.cleanup_error == (
        "PENDING_MANIFEST_DELETE_FAILED: RuntimeError; message=<redacted>"
    )
    assert "must-not-be-retained" not in cast(str, manifest.cleanup_error)


@pytest.mark.django_db
def test_successful_orphan_page_retry_clears_its_exact_run_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution("page-retry")
    run_storage = _run(execution)
    page = _page(run_storage, "retry", created_at=NOW - timedelta(hours=2))
    original_delete = WorkflowProgressTopologyPage.delete

    def fail_delete(self, *args, **kwargs):
        raise OSError("signed storage URL must not be retained")

    monkeypatch.setattr(WorkflowProgressTopologyPage, "delete", fail_delete)
    failed = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert failed.failed_count == 1
    run_storage.refresh_from_db()
    expected_code = f"ORPHAN_PAGE_DELETE_FAILED[page_id={page.pk}]"
    assert run_storage.cleanup_error == f"{expected_code}: OSError; message=<redacted>"

    monkeypatch.setattr(WorkflowProgressTopologyPage, "delete", original_delete)
    retried = cleanup_workflow_progress_storage(delete=True, now=NOW)

    assert retried.deleted_count == 2
    assert not WorkflowProgressRunStorage.objects.filter(pk=run_storage.pk).exists()
    assert not WorkflowProgressTopologyPage.objects.filter(pk=page.pk).exists()


@pytest.mark.parametrize("batch_size", [True, 0, 1001, 1.5])
def test_invalid_batch_sizes_are_rejected(batch_size) -> None:
    with pytest.raises(ValueError, match="batch size"):
        cleanup_workflow_progress_storage(batch_size=batch_size, now=NOW)


def test_non_boolean_delete_flag_is_rejected() -> None:
    with pytest.raises(ValueError, match="delete flag"):
        cleanup_workflow_progress_storage(delete="yes", now=NOW)  # type: ignore[arg-type]


@pytest.mark.django_db
def test_candidates_that_disappear_or_change_before_lock_are_skipped() -> None:
    execution = _execution("candidate-races")
    run_storage = _run(execution)
    missing_pk = 999_999_999

    results = (
        cleanup_module._process_expired_run(
            missing_pk,
            missing_pk,
            now=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_expired_run(
            execution.pk,
            missing_pk,
            now=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_pending_manifest(
            missing_pk,
            run_storage.pk,
            str(uuid4()),
            cutoff=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_pending_manifest(
            execution.pk,
            missing_pk,
            str(uuid4()),
            cutoff=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_pending_manifest(
            execution.pk,
            run_storage.pk,
            str(uuid4()),
            cutoff=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_orphan_page(
            missing_pk,
            run_storage.pk,
            missing_pk,
            cutoff=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_orphan_page(
            execution.pk,
            missing_pk,
            missing_pk,
            cutoff=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_orphan_page(
            execution.pk,
            run_storage.pk,
            missing_pk,
            cutoff=NOW,
            delete=True,
            using="default",
        ),
        cleanup_module._process_empty_run(
            missing_pk,
            missing_pk,
            delete=True,
            using="default",
        ),
        cleanup_module._process_empty_run(
            execution.pk,
            missing_pk,
            delete=True,
            using="default",
        ),
    )

    assert {result.outcome for result in results} == {WorkflowProgressCleanupOutcome.SKIPPED}


@pytest.mark.django_db
def test_best_effort_diagnostic_recording_tolerates_disappearing_rows_and_db_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = _execution("diagnostic-races")
    run_storage = _run(execution)
    missing_pk = 999_999_999

    cleanup_module._record_run_failure(
        missing_pk,
        run_storage.pk,
        "diagnostic",
        using="default",
    )
    cleanup_module._record_run_failure(
        execution.pk,
        missing_pk,
        "diagnostic",
        using="default",
    )
    cleanup_module._record_manifest_failure(
        missing_pk,
        run_storage.pk,
        str(uuid4()),
        "diagnostic",
        using="default",
    )
    cleanup_module._record_manifest_failure(
        execution.pk,
        missing_pk,
        str(uuid4()),
        "diagnostic",
        using="default",
    )
    cleanup_module._record_manifest_failure(
        execution.pk,
        run_storage.pk,
        str(uuid4()),
        "diagnostic",
        using="default",
    )

    def fail_lock(*args, **kwargs):
        raise RuntimeError("diagnostic storage unavailable")

    monkeypatch.setattr(cleanup_module, "_lock_task", fail_lock)
    cleanup_module._record_run_failure(
        execution.pk,
        run_storage.pk,
        "diagnostic",
        using="default",
    )
    cleanup_module._record_manifest_failure(
        execution.pk,
        run_storage.pk,
        str(uuid4()),
        "diagnostic",
        using="default",
    )

    run_storage.refresh_from_db()
    assert run_storage.cleanup_error is None


def test_naive_cleanup_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        cleanup_workflow_progress_storage(now=NOW.replace(tzinfo=None))
