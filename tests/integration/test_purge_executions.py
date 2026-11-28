"""Integration and behavioral tests for retention purge and CLI tooling."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from django_ray.management.commands.django_ray_cleanup_workflow_progress import (
    Command as CleanupWorkflowCommand,
)
from django_ray.management.commands.django_ray_purge_executions import (
    Command as PurgeExecutionsCommand,
)
from django_ray.management.commands.django_ray_purge_inputs import Command as PurgeInputsCommand
from django_ray.management.purge_cursor import (
    ExecutionPurgeCursor,
    InvalidPurgeCursorError,
    PurgeCursor,
    decode_execution_purge_cursor,
    decode_purge_cursor,
    encode_execution_purge_cursor,
    encode_purge_cursor,
)
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskAttempt,
    TaskInputPayload,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressNodeDetail,
    WorkflowProgressNodeState,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyCollection,
    WorkflowProgressTopologyEncoding,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
    WorkflowProgressTopologySlot,
)
from django_ray.result_storage import FilesystemResultStorage, ResultStorageError


def _make_execution(
    task_id: str,
    *,
    state: str = TaskState.SUCCEEDED,
    age_days: int = 45,
    finished: bool = True,
    result_reference: str | None = None,
) -> RayTaskExecution:
    ts = timezone.now() - timedelta(days=age_days)
    return RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="tests.unit.test_workflows.increment",
        state=state,
        created_at=ts,
        finished_at=ts if finished else None,
        result_reference=result_reference,
    )


def _make_lease(
    worker_id: str,
    *,
    is_active: bool = False,
    age_days: int = 45,
    with_stopped_at: bool = True,
) -> TaskWorkerLease:
    ts = timezone.now() - timedelta(days=age_days)
    return TaskWorkerLease.objects.create(
        worker_id=worker_id,
        hostname="worker-host",
        pid=1234,
        queue_name="default",
        capability_schema_version=1,
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=1,
        legacy_admission_token=None,
        started_at=ts,
        last_heartbeat_at=ts,
        is_active=is_active,
        stopped_at=ts if (not is_active and with_stopped_at) else None,
    )


def _run_purge_executions(**options: object) -> tuple[dict[str, Any], int, str]:
    stdout = StringIO()
    code = 0
    try:
        call_command("django_ray_purge_executions", format="json", stdout=stdout, **options)
    except CommandError as error:
        code = error.returncode
    output = stdout.getvalue()
    return json.loads(output), code, output


@pytest.mark.django_db
def test_purge_executions_is_safe_dry_run_by_default_and_never_touches_active() -> None:
    old_succeeded = _make_execution("exec-old-ok", state=TaskState.SUCCEEDED, age_days=45)
    old_failed = _make_execution("exec-old-fail", state=TaskState.FAILED, age_days=45)
    old_cancelled = _make_execution("exec-old-cancel", state=TaskState.CANCELLED, age_days=45)
    old_lost = _make_execution("exec-old-lost", state=TaskState.LOST, age_days=45)
    old_expired = _make_execution("exec-old-expired", state=TaskState.EXPIRED, age_days=45)
    recent_succeeded = _make_execution("exec-recent-ok", state=TaskState.SUCCEEDED, age_days=5)
    active_queued = _make_execution(
        "exec-queued", state=TaskState.QUEUED, age_days=60, finished=False
    )
    active_running = _make_execution(
        "exec-running", state=TaskState.RUNNING, age_days=60, finished=False
    )
    active_cancelling = _make_execution(
        "exec-cancelling", state=TaskState.CANCELLING, age_days=60, finished=False
    )
    undated_lost = _make_execution(
        "exec-undated-lost", state=TaskState.LOST, age_days=60, finished=False
    )

    TaskAttempt.objects.create(execution=old_succeeded, attempt_number=1, state=TaskState.SUCCEEDED)
    TaskAttempt.objects.create(execution=old_failed, attempt_number=1, state=TaskState.FAILED)
    old_inactive_lease = _make_lease("lease-old-inactive", is_active=False, age_days=45)
    recent_inactive_lease = _make_lease("lease-recent-inactive", is_active=False, age_days=5)
    active_old_lease = _make_lease("lease-active-old", is_active=True, age_days=60)

    report, code, _ = _run_purge_executions(older_than_days=30)

    assert code == 0
    assert report["mode"] == "dry-run"
    counts = report["counts"]
    assert isinstance(counts, dict)
    assert counts["executions_eligible"] == 5
    assert counts["executions_deleted"] == 0
    assert counts["attempts_eligible"] == 2
    assert counts["attempts_deleted"] == 0
    assert counts["leases_eligible"] == 1
    assert counts["leases_deleted"] == 0
    assert RayTaskExecution.objects.count() == 10
    assert TaskAttempt.objects.count() == 2
    assert TaskWorkerLease.objects.count() == 3

    # Explicit --dry-run behaves identically; --delete --dry-run fails closed.
    stdout = StringIO()
    call_command("django_ray_purge_executions", dry_run=True, stdout=stdout)
    assert "Execution purge dry-run" in stdout.getvalue()
    with pytest.raises(CommandError, match="cannot be used together"):
        call_command("django_ray_purge_executions", delete=True, dry_run=True)

    # Now execute --delete and verify ONLY old terminal rows & old inactive leases are purged.
    del_report, del_code, _ = _run_purge_executions(older_than_days=30, delete=True)
    assert del_code == 0
    del_counts = del_report["counts"]
    assert isinstance(del_counts, dict)
    assert del_counts["executions_deleted"] == 5
    assert del_counts["attempts_deleted"] == 2
    assert del_counts["leases_deleted"] == 1

    remaining_task_ids = set(RayTaskExecution.objects.values_list("task_id", flat=True))
    assert remaining_task_ids == {
        recent_succeeded.task_id,
        active_queued.task_id,
        active_running.task_id,
        active_cancelling.task_id,
        undated_lost.task_id,
    }
    assert not RayTaskExecution.objects.filter(
        pk__in=[old_succeeded.pk, old_failed.pk, old_cancelled.pk, old_lost.pk, old_expired.pk]
    ).exists()
    assert TaskAttempt.objects.count() == 0
    assert set(TaskWorkerLease.objects.values_list("worker_id", flat=True)) == {
        recent_inactive_lease.worker_id,
        active_old_lease.worker_id,
    }
    assert not TaskWorkerLease.objects.filter(worker_id=old_inactive_lease.worker_id).exists()


@pytest.mark.django_db(transaction=True)
def test_purge_executions_deletes_external_results_outside_db_transaction_and_keeps_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    import django_ray.result_storage as result_storage_module

    root = tmp_path / "results"
    root.mkdir()
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "RESULT_STORAGE_BACKEND": "filesystem",
        "RESULT_STORAGE_FILESYSTEM_PATH": str(root),
    }
    import os

    storage = FilesystemResultStorage(root)
    unique_exec_ref = storage.store_payload(serialized_payload='{"value":"unique-exec"}')
    unique_attempt_ref = storage.store_payload(serialized_payload='{"value":"unique-attempt"}')
    shared_ref = storage.store_payload(serialized_payload='{"value":"shared-result"}')
    old_mtime = (timezone.now() - timedelta(days=45)).timestamp()
    for ref in (unique_exec_ref, unique_attempt_ref, shared_ref):
        blob_path = storage._path_for_reference(storage._filesystem_reference(ref))
        os.utime(blob_path, (old_mtime, old_mtime))
    digest_stub_ref = f"oversize://sha256/{'a' * 64}?bytes=9999999"

    old_unique = _make_execution("exec-unique-blob", result_reference=unique_exec_ref)
    TaskAttempt.objects.create(
        execution=old_unique,
        attempt_number=1,
        state=TaskState.FAILED,
        result_reference=unique_attempt_ref,
    )
    old_shared = _make_execution("exec-shared-old", result_reference=shared_ref)
    recent_shared = _make_execution("exec-shared-recent", age_days=2, result_reference=shared_ref)
    old_digest = _make_execution("exec-digest-stub", result_reference=digest_stub_ref)

    in_atomic_during_delete: list[bool] = []
    deleted_refs: list[str] = []
    original_delete = FilesystemResultStorage.delete

    def spy_delete(self: FilesystemResultStorage, *, reference: str) -> None:
        in_atomic_during_delete.append(connection.in_atomic_block)
        deleted_refs.append(reference)
        original_delete(self, reference=reference)

    monkeypatch.setattr(FilesystemResultStorage, "delete", spy_delete)
    # Ensure fallback path works even before Wave 2 delete_result_reference lands.
    monkeypatch.delattr(result_storage_module, "delete_result_reference", raising=False)

    report, code, _ = _run_purge_executions(older_than_days=30, delete=True)

    assert code == 0
    counts = report["counts"]
    assert isinstance(counts, dict)
    assert counts["executions_deleted"] == 3
    assert counts["attempts_deleted"] == 1
    assert counts["results_deleted"] == 2
    assert counts["results_skipped_shared"] == 1
    assert counts["results_failed"] == 0
    assert set(deleted_refs) == {unique_exec_ref, unique_attempt_ref}
    assert in_atomic_during_delete == [False, False]
    with pytest.raises(ResultStorageError):
        storage.load(reference=unique_exec_ref)
    with pytest.raises(ResultStorageError):
        storage.load(reference=unique_attempt_ref)
    # Shared blob MUST remain intact for recent_shared.
    assert storage.load(reference=shared_ref) == '{"value":"shared-result"}'
    assert RayTaskExecution.objects.filter(pk=recent_shared.pk).exists()
    assert not RayTaskExecution.objects.filter(
        pk__in=[old_unique.pk, old_shared.pk, old_digest.pk]
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_purge_executions_uses_wave2_delete_result_reference_helper_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.result_storage as result_storage_module

    digest = "b" * 64
    ref = f"s3://bucket/django-ray/results/{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=42"
    old_exec = _make_execution("exec-helper-hook", result_reference=ref)

    calls: list[tuple[str, bool, bool]] = []

    def fake_delete_result_reference(reference: str, *, ignore_missing: bool = True) -> bool:
        calls.append((reference, ignore_missing, connection.in_atomic_block))
        return True

    monkeypatch.setattr(
        result_storage_module,
        "delete_result_reference",
        fake_delete_result_reference,
        raising=False,
    )

    report, code, _ = _run_purge_executions(older_than_days=30, delete=True)

    assert code == 0
    assert calls == [(ref, True, False)]
    assert report["counts"]["results_deleted"] == 1
    assert not RayTaskExecution.objects.filter(pk=old_exec.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_purge_executions_deletes_db_first_and_reports_post_commit_blob_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.result_storage as result_storage_module

    digest = "c" * 64
    ref = f"s3://bucket/django-ray/results/{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=42"
    failing_exec = _make_execution("exec-blob-fail", result_reference=ref)
    healthy_exec = _make_execution("exec-blob-none")
    db_deleted_before_blob_call: list[bool] = []

    def boom(_reference: str, *, ignore_missing: bool = True) -> bool:
        del ignore_missing
        assert not connection.in_atomic_block
        db_deleted_before_blob_call.append(
            not RayTaskExecution.objects.filter(pk=failing_exec.pk).exists()
        )
        raise OSError("secret-endpoint-unreachable")

    monkeypatch.setattr(result_storage_module, "delete_result_reference", boom, raising=False)

    report, code, output = _run_purge_executions(older_than_days=30, delete=True)

    assert code == 1
    assert "secret-endpoint-unreachable" not in output
    counts = report["counts"]
    assert isinstance(counts, dict)
    assert counts["results_failed"] == 1
    assert counts["executions_failed"] == 0
    assert counts["executions_deleted"] == 2
    assert report["error_types"] == ["OSError"]
    # DB rows are deleted FIRST before orphan blob cleanup so live rows can never
    # point to deleted content-addressed blobs or be corrupted by TOCTOU races.
    assert db_deleted_before_blob_call == [True]
    assert not RayTaskExecution.objects.filter(pk=failing_exec.pk).exists()
    assert not RayTaskExecution.objects.filter(pk=healthy_exec.pk).exists()


@pytest.mark.django_db
def test_purge_executions_handles_workflow_progress_restrict_children() -> None:
    execution = _make_execution("exec-with-wf-progress", state=TaskState.SUCCEEDED, age_days=45)
    run_storage = WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=1,
        execution_generation=1,
        run_id=uuid4(),
        detail_revision=1,
        detail_node_count=1,
        detail_succeeded_count=1,
        detail_encoded_bytes=16,
        detail_decoded_bytes=16,
    )
    page = WorkflowProgressTopologyPage.objects.create(
        run_storage=run_storage,
        digest="d" * 64,
        collection=WorkflowProgressTopologyCollection.NODE,
        encoding=WorkflowProgressTopologyEncoding.IDENTITY,
        payload=b"[]",
        item_count=1,
        encoded_bytes=2,
        decoded_bytes=2,
    )
    manifest = WorkflowProgressTopologyManifest.objects.create(
        run_storage=run_storage,
        topology_version=1,
        slot=WorkflowProgressTopologySlot.CURRENT,
        manifest_digest="e" * 64,
        payload=b"{}",
        node_count=1,
        edge_count=0,
        node_page_count=1,
        edge_page_count=0,
        encoded_bytes=2,
        decoded_bytes=2,
        published_at=timezone.now() - timedelta(days=45),
    )
    WorkflowProgressTopologyManifestPage.objects.create(
        manifest=manifest,
        page=page,
        collection=WorkflowProgressTopologyCollection.NODE,
        page_index=0,
    )
    WorkflowProgressNodeDetail.objects.create(
        run_storage=run_storage,
        node_key="f" * 64,
        node_id="step-1",
        state=WorkflowProgressNodeState.SUCCEEDED,
        payload=b'{"node":"step"}',
        digest="1" * 64,
        encoded_bytes=15,
        decoded_bytes=15,
        last_topology_version=1,
        last_detail_revision=1,
    )

    report, code, _ = _run_purge_executions(older_than_days=30, delete=True)

    assert code == 0
    assert report["counts"]["executions_deleted"] == 1
    assert not RayTaskExecution.objects.filter(pk=execution.pk).exists()
    assert WorkflowProgressRunStorage.objects.count() == 0
    assert WorkflowProgressTopologyManifestPage.objects.count() == 0
    assert WorkflowProgressTopologyPage.objects.count() == 0
    assert WorkflowProgressNodeDetail.objects.count() == 0


@pytest.mark.django_db
def test_purge_executions_cursor_resumption_and_until_empty_and_json_alias() -> None:
    for idx in range(5):
        _make_execution(f"exec-page-{idx:02d}", age_days=45)
    for idx in range(3):
        _make_lease(f"lease-page-{idx:02d}", is_active=False, age_days=45)

    # Bounded single batch returns exit code 2 and a non-hyphen cursor token.
    first, code1, _ = _run_purge_executions(older_than_days=30, batch_size=2, delete=True)
    assert code1 == 2
    assert first["has_more"] is True
    token = first["next_cursor"]
    assert isinstance(token, str) and token.startswith("epc1.")
    assert not token.startswith("-")
    assert first["counts"]["executions_deleted"] == 2
    assert first["counts"]["leases_deleted"] == 2

    # Resume using --cursor + --until-empty + --json alias to finish remaining rows in one call.
    stdout = StringIO()
    call_command(
        "django_ray_purge_executions",
        delete=True,
        batch_size=2,
        until_empty=True,
        cursor=token,
        json_output=True,
        stdout=stdout,
    )
    second = json.loads(stdout.getvalue())
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert second["counts"]["executions_deleted"] == 3
    assert second["counts"]["leases_deleted"] == 1
    assert RayTaskExecution.objects.count() == 0
    assert TaskWorkerLease.objects.count() == 0


def test_purge_executions_validation_and_cursor_policy_checks() -> None:
    with pytest.raises(CommandError, match="Unknown database alias"):
        call_command("django_ray_purge_executions", database="does_not_exist")
    with pytest.raises(CommandError, match="--batch-size"):
        call_command("django_ray_purge_executions", batch_size=0)
    with pytest.raises(CommandError, match="--max-batches"):
        call_command("django_ray_purge_executions", max_batches=0)
    with pytest.raises(CommandError, match="--older-than-days"):
        call_command("django_ray_purge_executions", older_than_days=-1)
    with pytest.raises(CommandError, match="Invalid purge cursor"):
        call_command("django_ray_purge_executions", cursor="not-a-cursor")

    cursor = encode_execution_purge_cursor(
        ExecutionPurgeCursor(
            after_execution_pk=10,
            after_lease_id="",
            cutoff=datetime(2026, 10, 1, tzinfo=UTC),
            older_than_days=30,
            delete=False,
            skip_leases=False,
            skip_results=False,
        )
    )
    with pytest.raises(CommandError, match="conflicts"):
        call_command("django_ray_purge_executions", cursor=cursor, delete=True)


def test_purge_cursor_handles_bytes_secret_key_and_separates_contexts() -> None:
    input_cursor = PurgeCursor("s3://bucket/key", datetime(2026, 10, 1, tzinfo=UTC), 30, False)
    exec_cursor = ExecutionPurgeCursor(
        after_execution_pk=42,
        after_lease_id="worker-01",
        cutoff=datetime(2026, 10, 1, tzinfo=UTC),
        older_than_days=30,
        delete=True,
        skip_leases=False,
        skip_results=False,
    )
    with override_settings(SECRET_KEY=b"raw-bytes-secret-key-for-f093"):
        input_token = encode_purge_cursor(input_cursor)
        exec_token = encode_execution_purge_cursor(exec_cursor)
        assert decode_purge_cursor(input_token) == input_cursor
        assert decode_execution_purge_cursor(exec_token) == exec_cursor
        assert exec_token.startswith("epc1.")
        # Cross-context tokens must fail closed.
        with pytest.raises(InvalidPurgeCursorError):
            decode_execution_purge_cursor(input_token)
        with pytest.raises(InvalidPurgeCursorError):
            decode_purge_cursor(exec_token)


@pytest.mark.django_db(transaction=True)
def test_purge_inputs_executes_external_delete_outside_transaction_and_supports_until_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    used_at = timezone.now() - timedelta(days=60)
    refs = []
    for idx in range(5):
        digest = f"{idx:064x}"
        ref = f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json&bytes=64"
        refs.append(ref)
        TaskInputPayload.objects.create(
            reference=ref,
            backend="filesystem",
            digest=digest,
            size_bytes=64,
            envelope_version=1,
            payload_kind=InputPayloadKind.TASK_INPUT,
            state=InputPayloadState.ACTIVE,
            created_at=used_at,
            last_used_at=used_at,
        )

    observed_atomic_states: list[bool] = []
    deleted_refs: list[str] = []

    def spy_delete(reference: str) -> None:
        observed_atomic_states.append(connection.in_atomic_block)
        deleted_refs.append(reference)

    module = ModuleType("django_ray.input_storage")
    module.delete_input_reference = spy_delete  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "django_ray.input_storage", module)

    stdout = StringIO()
    call_command(
        "django_ray_purge_inputs",
        retention_days=30,
        delete=True,
        max_candidates=2,
        until_empty=True,
        json_output=True,
        stdout=stdout,
    )
    report = json.loads(stdout.getvalue())
    assert report["has_more"] is False
    assert report["next_cursor"] is None
    assert report["counts"]["purged"] == 5
    assert deleted_refs == refs
    assert observed_atomic_states == [True] * 5
    assert TaskInputPayload.objects.filter(state=InputPayloadState.PURGED).count() == 5

    with pytest.raises(CommandError, match="Unknown database alias"):
        call_command("django_ray_purge_inputs", database="missing_db")


@pytest.mark.django_db
def test_cleanup_workflow_progress_supports_database_and_json_formats() -> None:
    execution = _make_execution("wf-cleanup-json-01", state=TaskState.FAILED, age_days=2)
    WorkflowProgressRunStorage.objects.create(
        execution=execution,
        attempt_number=1,
        execution_generation=1,
        run_id=uuid4(),
        detail_expires_at=timezone.now() - timedelta(minutes=5),
    )

    stdout_format = StringIO()
    call_command("django_ray_cleanup_workflow_progress", format="json", stdout=stdout_format)
    payload_dry = json.loads(stdout_format.getvalue())
    assert payload_dry["mode"] == "dry-run"
    assert payload_dry["database"] == "default"
    assert payload_dry["counts"]["eligible"] == 1
    assert payload_dry["counts"]["deleted"] == 0

    stdout_alias = StringIO()
    call_command(
        "django_ray_cleanup_workflow_progress",
        delete=True,
        json_output=True,
        database="default",
        stdout=stdout_alias,
    )
    payload_del = json.loads(stdout_alias.getvalue())
    assert payload_del["mode"] == "delete"
    assert payload_del["counts"]["deleted"] == 1

    with pytest.raises(CommandError, match="Unknown database alias"):
        call_command("django_ray_cleanup_workflow_progress", database="missing_db")


def test_all_owned_purge_and_cleanup_commands_define_help_on_every_flag() -> None:
    for command_cls in (PurgeExecutionsCommand, PurgeInputsCommand, CleanupWorkflowCommand):
        parser = command_cls().create_parser("manage.py", command_cls.__module__.rsplit(".", 1)[-1])
        missing_help = [
            action.option_strings
            for action in parser._actions
            if action.option_strings
            and not any(
                opt
                in {
                    "-h",
                    "--help",
                    "--version",
                    "-v",
                    "--verbosity",
                    "--settings",
                    "--pythonpath",
                    "--traceback",
                    "--no-color",
                    "--force-color",
                    "--skip-checks",
                }
                for opt in action.option_strings
            )
            and not (isinstance(action.help, str) and action.help.strip())
        ]
        assert missing_help == [], f"{command_cls} flags missing help=: {missing_help}"


@pytest.mark.django_db(transaction=True)
def test_purge_executions_skip_flags_and_concurrent_reactivation_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import django_ray.result_storage as result_storage_module

    old_exec = _make_execution("exec-skip-flags", state=TaskState.FAILED, age_days=45)
    old_lease = _make_lease("lease-skip-flags", is_active=False, age_days=45)

    # --skip-leases and --skip-results preserve leases and skip blob calls.
    monkeypatch.setattr(
        result_storage_module,
        "delete_result_reference",
        lambda *_a, **_kw: pytest.fail("skip_results must not call storage delete"),
        raising=False,
    )
    stdout = StringIO()
    call_command(
        "django_ray_purge_executions",
        older_than_days=30,
        delete=True,
        skip_leases=True,
        skip_results=True,
        stdout=stdout,
    )
    assert "Execution purge delete" in stdout.getvalue()
    assert not RayTaskExecution.objects.filter(pk=old_exec.pk).exists()
    assert TaskWorkerLease.objects.filter(worker_id=old_lease.worker_id).exists()

    # Concurrent retry (FAILED -> QUEUED) between Phase 1 and Phase 2 must be skipped
    # and must NEVER delete its external result blob.
    retried_ref = f"s3://bucket/django-ray/results/{'d' * 2}/{'d' * 2}/{'d' * 64}.json?bytes=42"
    retried_exec = _make_execution(
        "exec-retried-race",
        state=TaskState.FAILED,
        age_days=45,
        result_reference=retried_ref,
    )
    original_delete_batch = PurgeExecutionsCommand._delete_execution_batch

    def race_before_delete(
        execution_pks: tuple[int, ...],
        *,
        cutoff: datetime,
        using: str,
        skip_results: bool = False,
    ) -> tuple[int, int, int, int, frozenset[str]]:
        RayTaskExecution.objects.filter(pk=retried_exec.pk).update(
            state=TaskState.QUEUED,
            finished_at=None,
        )
        return original_delete_batch(
            execution_pks,
            cutoff=cutoff,
            using=using,
            skip_results=skip_results,
        )

    monkeypatch.setattr(
        PurgeExecutionsCommand,
        "_delete_execution_batch",
        staticmethod(race_before_delete),
    )
    monkeypatch.setattr(
        result_storage_module,
        "delete_result_reference",
        lambda *_a, **_kw: pytest.fail("retried execution blob must not be deleted"),
        raising=False,
    )
    report, code, _ = _run_purge_executions(older_than_days=30, delete=True, skip_leases=True)
    assert code == 0
    assert report["counts"]["executions_deleted"] == 0
    assert report["counts"]["executions_skipped"] == 1
    assert report["counts"]["results_deleted"] == 0
    retried_exec.refresh_from_db()
    assert retried_exec.state == TaskState.QUEUED


@pytest.mark.django_db(transaction=True)
def test_purge_inputs_failure_records_error_while_holding_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "9" * 64
    ref = f"resultfs://sha256/{digest}?rel={digest[:2]}/{digest[2:4]}/{digest}.json&bytes=64"
    old_ts = timezone.now() - timedelta(days=60)
    payload = TaskInputPayload.objects.create(
        reference=ref,
        backend="filesystem",
        digest=digest,
        size_bytes=64,
        envelope_version=1,
        payload_kind=InputPayloadKind.TASK_INPUT,
        state=InputPayloadState.ACTIVE,
        created_at=old_ts,
        last_used_at=old_ts,
    )

    def fail_inside_lock(_reference: str) -> None:
        assert connection.in_atomic_block
        raise OSError("transient storage glitch")

    module = ModuleType("django_ray.input_storage")
    module.delete_input_reference = fail_inside_lock  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "django_ray.input_storage", module)

    outcome = PurgeInputsCommand()._process_reference(
        ref,
        cutoff=timezone.now() - timedelta(days=30),
        delete=True,
    )
    assert outcome == "failed"
    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.purged_at is None
    assert payload.cleanup_error == "OSError"


@pytest.mark.django_db(transaction=True)
def test_purge_executions_fences_concurrent_dedup_result_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    """Concurrent oversized result reuse after orphan partitioning must not lose live blobs."""
    import os

    from django_ray.lifecycle import succeed_task

    root = tmp_path / "results_race"
    root.mkdir()
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "RESULT_STORAGE_BACKEND": "filesystem",
        "RESULT_STORAGE_FILESYSTEM_PATH": str(root),
    }

    storage = FilesystemResultStorage(root)
    payload_text = '{"payload": "shared-oversized-content"}'
    shared_ref = storage.store_payload(serialized_payload=payload_text)
    blob_path = storage._path_for_reference(storage._filesystem_reference(shared_ref))
    old_mtime = (timezone.now() - timedelta(days=45)).timestamp()
    os.utime(blob_path, (old_mtime, old_mtime))

    aged_exec = _make_execution("exec-aged-dedup", age_days=45, result_reference=shared_ref)
    live_running = _make_execution(
        "exec-live-dedup",
        state=TaskState.RUNNING,
        age_days=0,
        finished=False,
    )

    original_partition = PurgeExecutionsCommand._partition_orphan_result_refs

    def publish_during_partition(
        candidate_refs: frozenset[str],
        *,
        using: str,
    ) -> tuple[tuple[str, ...], frozenset[str]]:
        orphans, still_shared = original_partition(candidate_refs, using=using)
        # Reproduce exact race: worker reuses the aged blob and commits SUCCEEDED
        # AFTER _partition_orphan_result_refs returns `shared_ref` as an orphan.
        reused_ref = storage.store_payload(serialized_payload=payload_text)
        assert reused_ref == shared_ref
        assert succeed_task(live_running, result_data=None, result_reference=reused_ref) is True
        return orphans, still_shared

    monkeypatch.setattr(
        PurgeExecutionsCommand,
        "_partition_orphan_result_refs",
        staticmethod(publish_during_partition),
    )

    report, code, _ = _run_purge_executions(older_than_days=30, delete=True, skip_leases=True)

    assert code == 0
    assert report["counts"]["executions_deleted"] == 1
    assert report["counts"]["results_deleted"] == 0
    assert report["counts"]["results_skipped_shared"] >= 1
    assert not RayTaskExecution.objects.filter(pk=aged_exec.pk).exists()
    live_running.refresh_from_db()
    assert live_running.state == TaskState.SUCCEEDED
    assert live_running.result_reference == shared_ref
    # Blob MUST still exist and load cleanly for the newly succeeded execution.
    assert storage.load(reference=shared_ref) == payload_text
    assert blob_path.stat().st_mtime > old_mtime


@pytest.mark.django_db(transaction=True)
def test_purge_executions_blob_mtime_helpers_text_output_and_edge_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings,
) -> None:
    """Cover S3/GCS/FS mtime inspection, dry-run shared refs, text output, and error paths."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from django.db.models import ProtectedError

    from django_ray.management.commands import django_ray_purge_executions as purge_mod
    from django_ray.result_storage import GCSResultStorage, ResultStorageError, S3ResultStorage

    # _normalize_timestamp branches
    assert purge_mod._normalize_timestamp("not-a-dt") is None
    naive = datetime(2026, 1, 1, 0, 0, 0)
    assert purge_mod._normalize_timestamp(naive) == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    # _validate_database empty string
    with pytest.raises(CommandError, match="non-empty database alias"):
        PurgeExecutionsCommand._validate_database("")

    # _partition_orphan_result_refs empty set
    assert PurgeExecutionsCommand._partition_orphan_result_refs(frozenset(), using="default") == (
        (),
        frozenset(),
    )

    # _plan_execution_batch with after_pk > 0 and no matching rows
    empty_plan = PurgeExecutionsCommand._plan_execution_batch(
        after_pk=999999,
        cutoff=timezone.now() - timedelta(days=30),
        batch_size=10,
        skip_results=False,
        using="default",
    )
    assert empty_plan.candidate_pks == ()
    assert empty_plan.has_more is False

    # _result_blob_modified_at: filesystem missing, non-regular dir, and OSError
    root = tmp_path / "fs_mtime"
    root.mkdir()
    settings.DJANGO_RAY = {
        **getattr(settings, "DJANGO_RAY", {}),
        "RESULT_STORAGE_BACKEND": "filesystem",
        "RESULT_STORAGE_FILESYSTEM_PATH": str(root),
    }
    fs_storage = FilesystemResultStorage(root)
    ref = fs_storage.store_payload(serialized_payload='{"k":"v"}')
    blob_path = fs_storage._path_for_reference(fs_storage._filesystem_reference(ref))
    blob_path.unlink()
    assert purge_mod._result_blob_modified_at(ref) is None
    blob_path.mkdir()
    assert purge_mod._result_blob_modified_at(ref) is None
    blob_path.rmdir()

    def raise_os_error(self_path: Path, **_kw: Any) -> Any:
        raise OSError("stat denied")

    monkeypatch.setattr(Path, "stat", raise_os_error)
    with pytest.raises(ResultStorageError, match="filesystem result blob age"):
        purge_mod._result_blob_modified_at(ref)
    monkeypatch.undo()

    # _result_blob_modified_at: unconfigured backend raises without custom delete hook
    with pytest.raises(ResultStorageError):
        purge_mod._result_blob_modified_at("s3://unconfigured/key")

    # _result_blob_modified_at: S3ResultStorage (LastModified, missing key, no LastModified,
    # missing head_object, and unexpected error)
    digest = "a" * 64
    s3_ref = f"s3://bucket/prefix/{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=5"
    s3_ts = datetime(2025, 1, 1, tzinfo=UTC)

    class _FakeS3Client:
        def __init__(self, behavior: str) -> None:
            self.behavior = behavior

        def head_object(self, **_kw: Any) -> Any:
            if self.behavior == "ok":
                return {"LastModified": s3_ts}
            if self.behavior == "empty":
                return {}
            if self.behavior == "404":
                raise FileNotFoundError("NoSuchKey")
            raise RuntimeError("s3 outage")

    import django_ray.result_storage as rs_mod

    s3_ok = S3ResultStorage(bucket="bucket", prefix="prefix", client=_FakeS3Client("ok"))
    monkeypatch.setattr(rs_mod, "get_result_storage_backend_for_reference", lambda _r: s3_ok)
    assert purge_mod._result_blob_modified_at(s3_ref) == s3_ts

    s3_empty = S3ResultStorage(bucket="bucket", prefix="prefix", client=_FakeS3Client("empty"))
    monkeypatch.setattr(rs_mod, "get_result_storage_backend_for_reference", lambda _r: s3_empty)
    assert purge_mod._result_blob_modified_at(s3_ref) is None

    s3_404 = S3ResultStorage(bucket="bucket", prefix="prefix", client=_FakeS3Client("404"))
    monkeypatch.setattr(rs_mod, "get_result_storage_backend_for_reference", lambda _r: s3_404)
    assert purge_mod._result_blob_modified_at(s3_ref) is None

    s3_no_head = S3ResultStorage(bucket="bucket", prefix="prefix", client=SimpleNamespace())
    monkeypatch.setattr(rs_mod, "get_result_storage_backend_for_reference", lambda _r: s3_no_head)
    assert purge_mod._result_blob_modified_at(s3_ref) is None

    s3_err = S3ResultStorage(bucket="bucket", prefix="prefix", client=_FakeS3Client("err"))
    monkeypatch.setattr(rs_mod, "get_result_storage_backend_for_reference", lambda _r: s3_err)
    with pytest.raises(ResultStorageError, match="S3 result blob age"):
        purge_mod._result_blob_modified_at(s3_ref)

    # _result_blob_modified_at: GCSResultStorage (updated, time_created, 404, unexpected error)
    gcs_ref = f"gs://bucket/prefix/{digest[:2]}/{digest[2:4]}/{digest}.json?bytes=5"

    class _FakeGCSBlob:
        def __init__(self, behavior: str) -> None:
            self.behavior = behavior
            self.updated = s3_ts if behavior == "updated" else None
            self.time_created = s3_ts if behavior == "created" else None

        def reload(self) -> None:
            if self.behavior == "404":
                raise FileNotFoundError("NotFound")
            if self.behavior == "err":
                raise RuntimeError("gcs outage")

    def _gcs_backend(behavior: str) -> GCSResultStorage:
        client = SimpleNamespace(
            bucket=lambda _b: SimpleNamespace(blob=lambda _k: _FakeGCSBlob(behavior))
        )
        return GCSResultStorage(bucket="bucket", prefix="prefix", client=client)

    monkeypatch.setattr(
        rs_mod, "get_result_storage_backend_for_reference", lambda _r: _gcs_backend("updated")
    )
    assert purge_mod._result_blob_modified_at(gcs_ref) == s3_ts
    monkeypatch.setattr(
        rs_mod, "get_result_storage_backend_for_reference", lambda _r: _gcs_backend("created")
    )
    assert purge_mod._result_blob_modified_at(gcs_ref) == s3_ts
    monkeypatch.setattr(
        rs_mod, "get_result_storage_backend_for_reference", lambda _r: _gcs_backend("404")
    )
    assert purge_mod._result_blob_modified_at(gcs_ref) is None
    monkeypatch.setattr(
        rs_mod, "get_result_storage_backend_for_reference", lambda _r: _gcs_backend("err")
    )
    with pytest.raises(ResultStorageError, match="GCS result blob age"):
        purge_mod._result_blob_modified_at(gcs_ref)

    # Unknown custom backend returns None
    monkeypatch.setattr(rs_mod, "get_result_storage_backend_for_reference", lambda _r: object())
    assert purge_mod._result_blob_modified_at(gcs_ref) is None
    monkeypatch.undo()

    # Dry-run planning with shared execution + attempt external refs and text output with cursor
    shared_exec_ref = f"resultfs://sha256/{'b' * 64}?rel=bb/bb/{'b' * 64}.json&bytes=10"
    shared_att_ref = f"resultfs://sha256/{'c' * 64}?rel=cc/cc/{'c' * 64}.json&bytes=10"
    orphan_ref = f"resultfs://sha256/{'d' * 64}?rel=dd/dd/{'d' * 64}.json&bytes=10"
    old_e1 = _make_execution("purge-dry-1", age_days=50, result_reference=shared_exec_ref)
    TaskAttempt.objects.create(
        execution=old_e1,
        attempt_number=1,
        state=TaskState.FAILED,
        result_reference=shared_att_ref,
    )
    _make_execution("purge-dry-2", age_days=49, result_reference=orphan_ref)
    # Newer executions keeping both shared_exec_ref and shared_att_ref alive
    keeper = _make_execution("purge-keep-1", age_days=2, result_reference=shared_exec_ref)
    TaskAttempt.objects.create(
        execution=keeper,
        attempt_number=1,
        state=TaskState.FAILED,
        result_reference=shared_att_ref,
    )

    text_out = StringIO()
    with pytest.raises(CommandError) as incomplete_exc:
        call_command(
            "django_ray_purge_executions",
            "--older-than-days=30",
            "--batch-size=1",
            "--skip-leases",
            "--format=text",
            stdout=text_out,
        )
    assert incomplete_exc.value.returncode == 2
    rendered_text = text_out.getvalue()
    assert "Execution purge dry-run" in rendered_text
    assert "Next cursor:" in rendered_text

    # Delete mode: blob newer than cutoff is retained as shared-retained (lines 460-461)
    monkeypatch.setattr(
        purge_mod, "_result_blob_modified_at", lambda _r: timezone.now() + timedelta(days=1)
    )
    delete_report, delete_code, _ = _run_purge_executions(
        older_than_days=30, delete=True, skip_leases=True, until_empty=True
    )
    assert delete_code == 0
    assert delete_report["counts"]["results_deleted"] == 0
    assert delete_report["counts"]["results_skipped_shared"] >= 1
    monkeypatch.undo()

    # Delete mode: ProtectedError rolls back cleanly and increments executions_failed + text Error types
    prot_exec = _make_execution("purge-protected-err", age_days=60)
    monkeypatch.setattr(
        RayTaskExecution.objects.all().__class__,
        "delete",
        lambda _self: (_ for _ in ()).throw(ProtectedError("protected", set())),
    )
    deleted_execs, _da, _sc, prot_failed, _refs = PurgeExecutionsCommand._delete_execution_batch(
        (prot_exec.pk,),
        cutoff=timezone.now() - timedelta(days=30),
        using="default",
    )
    assert deleted_execs == 0
    assert prot_failed == 1
    monkeypatch.undo()

    # Text output with error_labels and output bound overflow
    err_exec = _make_execution("purge-text-err", age_days=60, result_reference=orphan_ref)
    monkeypatch.setattr(purge_mod, "_result_blob_modified_at", lambda _r: None)
    monkeypatch.setattr(
        purge_mod,
        "_delete_external_result_reference",
        lambda _r: (_ for _ in ()).throw(OSError("disk full")),
    )
    err_out = StringIO()
    with pytest.raises(CommandError, match="Failed to purge"):
        call_command(
            "django_ray_purge_executions",
            "--older-than-days=30",
            "--delete",
            "--skip-leases",
            "--format=text",
            stdout=err_out,
        )
    assert "Error types: OSError" in err_out.getvalue()
    assert not RayTaskExecution.objects.filter(pk=err_exec.pk).exists()
    monkeypatch.undo()

    # Output bound exceeded check
    monkeypatch.setattr(purge_mod, "OUTPUT_MAX_BYTES", 300)
    with pytest.raises(CommandError, match="exceeded its output bound"):
        call_command("django_ray_purge_executions", "--older-than-days=30", "--skip-leases")
