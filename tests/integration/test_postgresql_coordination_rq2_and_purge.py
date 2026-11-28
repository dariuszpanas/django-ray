"""PostgreSQL-only tests for payload registration, RQ2 attach/duplicate, and purge races."""

from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ray.input_storage import (
    EXTERNAL_INPUT_PLACEHOLDER,
    load_task_input,
    prepare_task_input,
    register_task_input,
)
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    record_failure,
    request_task_cancellation,
    retry_task,
    succeed_task,
)
from django_ray.management.commands.django_ray_purge_inputs import Command as PurgeInputsCommand
from django_ray.models import (
    InputPayloadKind,
    InputPayloadState,
    RayTaskExecution,
    TaskAttempt,
    TaskInputPayload,
    TaskState,
)
from django_ray.runner.cancellation import (
    CancellationOutcome,
)
from django_ray.runner.reconciliation import mark_task_lost
from tests.integration._postgresql_coordination_helpers import (
    _claim_command,
    _execution,
    _prepared_rq2_request,
    _RecordingRayJobClient,
    _rq2_request_path,
    _rq2_reserved_execution,
    _run_concurrently,
    _wait_for_postgresql_lock,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    """Keep the default SQLite suite fast while making this gate explicit."""
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


@pytest.mark.parametrize(
    "payload_kind", [InputPayloadKind.TASK_INPUT, InputPayloadKind.RAY_JOB_REQUEST]
)
@pytest.mark.parametrize("purge_failure", ["delete_acknowledgement_loss", "tombstone_rollback"])
def test_payload_registration_restores_active_object_after_purge_failure(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
    purge_failure: str,
) -> None:
    """An ACTIVE row cannot prove availability after an uncertain purge."""
    import django_ray.input_storage as input_storage_module
    from django_ray.ray_job_request_storage import (
        load_ray_job_request,
        register_and_attach_ray_job_request,
    )

    storage_config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, **storage_config}
    old_timestamp = timezone.now() - timedelta(days=60)
    if payload_kind == InputPayloadKind.TASK_INPUT:
        prepared_input = prepare_task_input(("restored active input",), {})
        assert prepared_input.input_reference is not None
        assert prepared_input.digest is not None
        reference = prepared_input.input_reference
        with transaction.atomic():
            payload = register_task_input(prepared_input)
        assert payload is not None
        old_execution = _execution(
            "postgres-active-input-purge-old",
            state=TaskState.SUCCEEDED,
            finished_at=old_timestamp,
            args_json=prepared_input.args_json,
            kwargs_json=prepared_input.kwargs_json,
            input_reference=reference,
        )
        digest = prepared_input.digest
        path = tmp_path / digest[:2] / digest[2:4] / f"{digest}.json"

        def write_reference() -> int:
            with transaction.atomic():
                register_task_input(prepared_input)
                return int(
                    _execution(
                        "postgres-active-input-purge-new",
                        args_json=prepared_input.args_json,
                        kwargs_json=prepared_input.kwargs_json,
                        input_reference=reference,
                    ).pk
                )

    else:
        execution, handle = _rq2_reserved_execution("postgres-active-rq2-purge")
        prepared_request = _prepared_rq2_request(execution, tmp_path)
        reference = prepared_request.reference
        payload = TaskInputPayload.objects.create(
            reference=reference,
            payload_kind=payload_kind,
            backend=prepared_request.backend,
            digest=prepared_request.digest,
            size_bytes=prepared_request.size_bytes,
            envelope_version=prepared_request.envelope_version,
            state=InputPayloadState.ACTIVE,
        )
        path = _rq2_request_path(prepared_request)

        def write_reference() -> int:
            assert (
                register_and_attach_ray_job_request(
                    prepared_request,
                    task_execution=execution,
                    submission_handle=handle,
                )
                == reference
            )
            return int(execution.pk)

    TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=old_timestamp)
    original_bytes = path.read_bytes()
    cutoff = timezone.now() - timedelta(days=30)
    deletion_started, release_deletion, writer_connected = Event(), Event(), Event()
    writer_pid: list[int] = []
    original_delete = input_storage_module.delete_input_reference

    def uncertain_delete(reference: str) -> None:
        original_delete(reference, config=storage_config)
        deletion_started.set()
        assert release_deletion.wait(timeout=10)
        if purge_failure == "delete_acknowledgement_loss":
            raise OSError("simulated lost deletion acknowledgement")

    monkeypatch.setattr(input_storage_module, "delete_input_reference", uncertain_delete)

    def purge() -> str:
        close_old_connections()
        try:
            with transaction.atomic():
                outcome = PurgeInputsCommand()._process_reference(
                    reference, cutoff=cutoff, delete=True, max_attachments=1
                )
                if purge_failure == "tombstone_rollback":
                    assert outcome == "purged"
                    tombstone = TaskInputPayload.objects.get(pk=payload.pk)
                    assert tombstone.state == InputPayloadState.PURGED
                    assert tombstone.purged_at is not None
                    transaction.set_rollback(True)
                    return "rolled_back"
                assert outcome == "failed"
                failed_payload = TaskInputPayload.objects.get(pk=payload.pk)
                assert failed_payload.state == InputPayloadState.ACTIVE
                assert failed_payload.cleanup_error
                return outcome
        finally:
            close_old_connections()

    def writer() -> int:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.append(int(cursor.fetchone()[0]))
            writer_connected.set()
            return write_reference()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        purge_future = executor.submit(purge)
        try:
            assert deletion_started.wait(timeout=10)
            assert not path.exists()
            writer_future = executor.submit(writer)
            assert writer_connected.wait(timeout=10)
            _wait_for_postgresql_lock(writer_pid[0])
        finally:
            release_deletion.set()
        expected_outcome = (
            "failed" if purge_failure == "delete_acknowledgement_loss" else "rolled_back"
        )
        assert purge_future.result(timeout=20) == expected_outcome
        attached_execution = RayTaskExecution.objects.get(pk=writer_future.result(timeout=20))

    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.purged_at is None
    assert payload.last_used_at > cutoff
    assert path.read_bytes() == original_bytes
    if payload_kind == InputPayloadKind.TASK_INPUT:
        old_execution.refresh_from_db()
        assert old_execution.input_reference == reference
        assert attached_execution.state == TaskState.QUEUED
        assert attached_execution.input_reference == reference
        assert load_task_input(
            args_json=attached_execution.args_json,
            kwargs_json=attached_execution.kwargs_json,
            input_reference=attached_execution.input_reference,
        ) == (["restored active input"], {})
    else:
        assert attached_execution.state == TaskState.RUNNING
        assert attached_execution.ray_job_request_reference == reference
        assert (
            load_ray_job_request(prepared_request.encoded_locator).request
            == prepared_request.request
        )


@pytest.mark.parametrize("transition", ["cancel", "complete", "retry"])
def test_bounded_input_purge_revalidates_execution_after_lifecycle_lock(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    """Projected eligibility must use the row committed after the lock wait."""
    import django_ray.input_storage as input_storage_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    prepared = prepare_task_input(("retained lifecycle input",), {})
    assert prepared.input_reference is not None
    with transaction.atomic():
        payload = register_task_input(prepared)
    assert payload is not None
    old_timestamp = timezone.now() - timedelta(days=60)
    TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=old_timestamp)
    execution = _execution(
        f"bounded-input-lifecycle-{transition}",
        state=TaskState.FAILED if transition == "retry" else TaskState.RUNNING,
        input_reference=prepared.input_reference,
        finished_at=old_timestamp if transition == "retry" else None,
        execution_generation=1,
    )
    cutoff = timezone.now() - timedelta(days=30)
    writer_ready, release_writer, purge_connected = Event(), Event(), Event()
    purge_pid: list[int] = []
    observed = []
    original_check = PurgeInputsCommand._all_references_are_old_and_terminal

    def observe_locked_values(executions, **kwargs):
        assert len(executions) == 1
        observed.append((executions[0].state_is_terminal, executions[0].finished_at))
        return original_check(executions, **kwargs)

    monkeypatch.setattr(
        PurgeInputsCommand,
        "_all_references_are_old_and_terminal",
        staticmethod(observe_locked_values),
    )
    monkeypatch.setattr(
        input_storage_module,
        "delete_input_reference",
        lambda reference: pytest.fail("fresh lifecycle state must retain the payload"),
    )

    def transition_execution() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                if transition == "cancel":
                    assert request_task_cancellation(
                        execution.pk, expected_attempt_number=1, expected_execution_generation=1
                    ).accepted
                elif transition == "complete":
                    assert succeed_task(
                        execution,
                        result_data="3",
                        result_reference=None,
                        expected_execution_generation=1,
                    )
                else:
                    assert (
                        retry_task(
                            execution.pk, expected_attempt_number=1, expected_execution_generation=1
                        )
                        is not None
                    )
                writer_ready.set()
                assert release_writer.wait(timeout=10)
        finally:
            close_old_connections()

    def purge() -> str:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                purge_pid.append(int(cursor.fetchone()[0]))
            purge_connected.set()
            return PurgeInputsCommand()._process_reference(
                prepared.input_reference, cutoff=cutoff, delete=True, max_attachments=1
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer_future = executor.submit(transition_execution)
        try:
            assert writer_ready.wait(timeout=10)
            purge_future = executor.submit(purge)
            assert purge_connected.wait(timeout=10)
            _wait_for_postgresql_lock(purge_pid[0])
        finally:
            release_writer.set()
        writer_future.result(timeout=20)
        assert purge_future.result(timeout=20) == "skipped"

    payload.refresh_from_db()
    execution.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.last_used_at <= cutoff  # Eligibility changed on execution, not registry.
    assert observed == [(transition == "complete", execution.finished_at)]
    assert (
        execution.state
        == {
            "cancel": TaskState.CANCELLING,
            "complete": TaskState.SUCCEEDED,
            "retry": TaskState.QUEUED,
        }[transition]
    )
    if transition == "complete":
        assert execution.finished_at is not None and execution.finished_at > cutoff
    assert load_task_input(
        args_json=prepared.args_json,
        kwargs_json=prepared.kwargs_json,
        input_reference=prepared.input_reference,
    ) == (["retained lifecycle input"], {})


@pytest.mark.parametrize(
    "payload_kind", [InputPayloadKind.TASK_INPUT, InputPayloadKind.RAY_JOB_REQUEST]
)
def test_bounded_input_purge_waits_for_writer_and_preserves_current_reference(
    settings,
    tmp_path: Path,
    payload_kind: str,
) -> None:
    """A writer-first lock cannot be bypassed by a capped attachment query."""
    from django_ray.ray_job_request_storage import (
        load_ray_job_request,
        register_and_attach_ray_job_request,
    )

    storage_config = {
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, **storage_config}
    if payload_kind == InputPayloadKind.TASK_INPUT:
        prepared_input = prepare_task_input(("retained input",), {})
        assert prepared_input.input_reference is not None
        with transaction.atomic():
            payload = register_task_input(prepared_input)
        assert payload is not None
        reference = prepared_input.input_reference

        def write_reference() -> None:
            register_task_input(prepared_input)
            _execution(
                "bounded-purge-writer-first-input",
                input_reference=reference,
                args_json=prepared_input.args_json,
                kwargs_json=prepared_input.kwargs_json,
            )

    else:
        execution, handle = _rq2_reserved_execution("bounded-purge-writer-first-rq2")
        prepared_request = _prepared_rq2_request(execution, tmp_path)
        reference = prepared_request.reference
        payload = TaskInputPayload.objects.create(
            reference=reference,
            payload_kind=payload_kind,
            backend=prepared_request.backend,
            digest=prepared_request.digest,
            size_bytes=prepared_request.size_bytes,
            envelope_version=prepared_request.envelope_version,
        )

        def write_reference() -> None:
            register_and_attach_ray_job_request(
                prepared_request,
                task_execution=execution,
                submission_handle=handle,
            )

    TaskInputPayload.objects.filter(pk=payload.pk).update(
        last_used_at=timezone.now() - timedelta(days=60)
    )
    cutoff = timezone.now() - timedelta(days=30)
    writer_ready, release_writer, purge_connected = Event(), Event(), Event()
    purge_pid: list[int] = []

    def writer() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                write_reference()
                writer_ready.set()
                assert release_writer.wait(timeout=10)
        finally:
            close_old_connections()

    def purge() -> str:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                purge_pid.append(int(cursor.fetchone()[0]))
            purge_connected.set()
            return PurgeInputsCommand()._process_reference(
                reference,
                cutoff=cutoff,
                delete=True,
                max_attachments=1,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer_future = executor.submit(writer)
        try:
            assert writer_ready.wait(timeout=10)
            purge_future = executor.submit(purge)
            assert purge_connected.wait(timeout=10)
            _wait_for_postgresql_lock(purge_pid[0])
        finally:
            release_writer.set()
        writer_future.result(timeout=20)
        assert purge_future.result(timeout=20) == "skipped"

    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.last_used_at > cutoff
    if payload_kind == InputPayloadKind.TASK_INPUT:
        assert load_task_input(
            args_json=prepared_input.args_json,
            kwargs_json=prepared_input.kwargs_json,
            input_reference=reference,
        ) == (["retained input"], {})
    else:
        assert (
            load_ray_job_request(prepared_request.encoded_locator).request
            == prepared_request.request
        )


def test_bounded_input_purge_overflow_never_deletes_shared_payload(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a terminal prefix cannot authorize deletion of uninspected history."""
    import django_ray.input_storage as input_storage_module

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "MAX_INLINE_INPUT_SIZE_BYTES": 0,
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    prepared = prepare_task_input(("shared input",), {})
    with transaction.atomic():
        payload = register_task_input(prepared)
    assert payload is not None
    assert prepared.input_reference is not None
    timestamp = timezone.now() - timedelta(days=60)
    TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=timestamp)
    for index, state in enumerate((TaskState.SUCCEEDED, TaskState.QUEUED)):
        _execution(
            f"bounded-purge-overflow-{index}",
            input_reference=prepared.input_reference,
            state=state,
            finished_at=timestamp if state == TaskState.SUCCEEDED else None,
        )
    monkeypatch.setattr(
        input_storage_module,
        "delete_input_reference",
        lambda reference: pytest.fail("incomplete history must not reach backend deletion"),
    )
    with CaptureQueriesContext(connection) as queries:
        outcome = PurgeInputsCommand()._process_reference(
            prepared.input_reference,
            cutoff=timezone.now() - timedelta(days=30),
            delete=True,
            max_attachments=1,
        )
    assert outcome == "incomplete"
    assert any("LIMIT 2" in query["sql"] and "FOR UPDATE" in query["sql"] for query in queries)
    payload.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert load_task_input(
        args_json=prepared.args_json,
        kwargs_json=prepared.kwargs_json,
        input_reference=prepared.input_reference,
    ) == (["shared input"], {})


def test_rq2_attach_waits_for_purge_then_restores_the_exact_request(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge-first race cannot delete the request selected by attachment."""
    import django_ray.input_storage as input_storage_module
    from django_ray.ray_job_request_storage import (
        load_ray_job_request,
        register_and_attach_ray_job_request,
    )

    storage_config = {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, **storage_config}
    execution, handle = _rq2_reserved_execution("postgres-rq2-attach-purge-001")
    prepared = _prepared_rq2_request(execution, tmp_path)
    payload = TaskInputPayload.objects.create(
        reference=prepared.reference,
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state=InputPayloadState.ACTIVE,
    )
    old_timestamp = timezone.now() - timedelta(days=60)
    TaskInputPayload.objects.filter(pk=payload.pk).update(last_used_at=old_timestamp)
    cutoff = timezone.now() - timedelta(days=30)
    request_path = _rq2_request_path(prepared)
    assert request_path.is_file()

    deletion_started = Event()
    release_deletion = Event()
    attach_connected = Event()
    attach_backend_pid: list[int] = []
    original_delete = input_storage_module.delete_input_reference

    def blocking_delete(reference: str) -> None:
        original_delete(reference, config=storage_config)
        deletion_started.set()
        if not release_deletion.wait(timeout=10):
            raise TimeoutError("test did not release rq2 request deletion")

    monkeypatch.setattr(input_storage_module, "delete_input_reference", blocking_delete)

    def purge_request() -> str:
        close_old_connections()
        try:
            return PurgeInputsCommand()._process_reference(
                prepared.reference,
                cutoff=cutoff,
                delete=True,
                max_attachments=1,
            )
        finally:
            close_old_connections()

    def attach_request() -> str:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                attach_backend_pid.append(int(cursor.fetchone()[0]))
            attach_connected.set()
            return register_and_attach_ray_job_request(
                prepared,
                task_execution=execution,
                submission_handle=handle,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        purge_future = executor.submit(purge_request)
        try:
            assert deletion_started.wait(timeout=10)
            assert not request_path.exists()
            attach_future = executor.submit(attach_request)
            assert attach_connected.wait(timeout=10)
            _wait_for_postgresql_lock(attach_backend_pid[0])
        finally:
            release_deletion.set()
        assert purge_future.result(timeout=20) == "purged"
        assert attach_future.result(timeout=20) == prepared.reference

    payload.refresh_from_db()
    execution.refresh_from_db()
    assert payload.state == InputPayloadState.ACTIVE
    assert payload.purged_at is None
    assert payload.last_used_at > cutoff
    assert execution.ray_job_request_reference == prepared.reference
    assert request_path.is_file()
    assert load_ray_job_request(prepared.encoded_locator).request == prepared.request


def test_rq2_wrapped_attach_waits_for_native_purge_then_refuses_alias(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A historical native tombstone cannot acquire a second registry identity."""
    import django_ray.input_storage as input_storage_module
    from django_ray.ray_job_request_storage import (
        RAY_JOB_REQUEST_REFERENCE_PREFIX,
        RayJobRequestStorageError,
        RayJobRequestStorageRejection,
        register_and_attach_ray_job_request,
    )

    storage_config = {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, **storage_config}
    execution, handle = _rq2_reserved_execution("postgres-rq2-native-alias-001")
    prepared = _prepared_rq2_request(execution, tmp_path)
    native = prepared.reference.removeprefix(RAY_JOB_REQUEST_REFERENCE_PREFIX)
    payload = TaskInputPayload.objects.create(
        reference=native,
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        backend=prepared.backend,
        digest=prepared.digest,
        size_bytes=prepared.size_bytes,
        envelope_version=prepared.envelope_version,
        state=InputPayloadState.ACTIVE,
    )
    TaskInputPayload.objects.filter(pk=payload.pk).update(
        last_used_at=timezone.now() - timedelta(days=60)
    )
    cutoff = timezone.now() - timedelta(days=30)
    deletion_started = Event()
    release_deletion = Event()
    attach_connected = Event()
    attach_backend_pid: list[int] = []
    original_delete = input_storage_module.delete_input_reference

    def blocking_delete(reference: str) -> None:
        assert reference == native
        original_delete(reference, config=storage_config)
        deletion_started.set()
        if not release_deletion.wait(timeout=10):
            raise TimeoutError("test did not release native request deletion")

    monkeypatch.setattr(input_storage_module, "delete_input_reference", blocking_delete)

    def purge_request() -> str:
        close_old_connections()
        try:
            return PurgeInputsCommand()._process_reference(
                native, cutoff=cutoff, delete=True, max_attachments=1
            )
        finally:
            close_old_connections()

    def attach_request() -> RayJobRequestStorageRejection:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                attach_backend_pid.append(int(cursor.fetchone()[0]))
            attach_connected.set()
            with pytest.raises(RayJobRequestStorageError) as error:
                register_and_attach_ray_job_request(
                    prepared, task_execution=execution, submission_handle=handle
                )
            return error.value.classification
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        purge_future = executor.submit(purge_request)
        try:
            assert deletion_started.wait(timeout=10)
            assert not _rq2_request_path(prepared).exists()
            attach_future = executor.submit(attach_request)
            assert attach_connected.wait(timeout=10)
            _wait_for_postgresql_lock(attach_backend_pid[0])
        finally:
            release_deletion.set()
        assert purge_future.result(timeout=20) == "purged"
        assert attach_future.result(timeout=20) == RayJobRequestStorageRejection.REGISTRY_MISMATCH

    payload.refresh_from_db()
    execution.refresh_from_db()
    assert payload.state == InputPayloadState.PURGED
    assert payload.purged_at is not None
    assert not TaskInputPayload.objects.filter(reference=prepared.reference).exists()
    assert not execution.ray_job_request_reference
    assert not _rq2_request_path(prepared).exists()


@pytest.mark.parametrize("transition", ["cancel", "owner-loss", "replacement"])
def test_rq2_attach_rejects_a_concurrent_execution_transition_exactly(
    tmp_path: Path,
    transition: str,
) -> None:
    """Attachment rolls its registry write back after an exact CAS loses."""
    from django_ray.ray_job_request_storage import (
        RayJobRequestStorageError,
        RayJobRequestStorageRejection,
        register_and_attach_ray_job_request,
    )

    execution, handle = _rq2_reserved_execution(f"postgres-rq2-attach-{transition}-001")
    prepared = _prepared_rq2_request(execution, tmp_path)
    transition_locked = Event()
    release_transition = Event()
    attach_connected = Event()
    attach_backend_pid: list[int] = []

    def transition_execution() -> dict[str, Any]:
        close_old_connections()
        try:
            with transaction.atomic():
                current = RayTaskExecution.objects.get(pk=execution.pk)
                if transition == "cancel":
                    result = request_task_cancellation(
                        int(execution.pk),
                        expected_attempt_number=execution.attempt_number,
                        expected_execution_generation=execution.execution_generation,
                    )
                    assert result.status is TaskCancellationRequestStatus.ACCEPTED
                else:
                    assert mark_task_lost(current) is True
                    if transition == "replacement":
                        replacement = retry_task(
                            current,
                            allowed_states=(TaskState.LOST,),
                            expected_attempt_number=execution.attempt_number,
                            expected_execution_generation=execution.execution_generation,
                        )
                        assert replacement is not None
                transitioned = RayTaskExecution.objects.values().get(pk=execution.pk)
                transition_locked.set()
                if not release_transition.wait(timeout=10):
                    raise TimeoutError("test did not release the rq2 execution transition")
                return transitioned
        finally:
            close_old_connections()

    def attach_request() -> str:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                attach_backend_pid.append(int(cursor.fetchone()[0]))
            attach_connected.set()
            return register_and_attach_ray_job_request(
                prepared,
                task_execution=execution,
                submission_handle=handle,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        transition_future = executor.submit(transition_execution)
        try:
            assert transition_locked.wait(timeout=10)
            attach_future = executor.submit(attach_request)
            assert attach_connected.wait(timeout=10)
            _wait_for_postgresql_lock(attach_backend_pid[0])
        finally:
            release_transition.set()
        transitioned = transition_future.result(timeout=20)
        with pytest.raises(RayJobRequestStorageError) as caught:
            attach_future.result(timeout=20)

    assert caught.value.classification is RayJobRequestStorageRejection.BINDING_MISMATCH
    assert RayTaskExecution.objects.values().get(pk=execution.pk) == transitioned
    assert not TaskInputPayload.objects.filter(reference=prepared.reference).exists()
    assert _rq2_request_path(prepared).is_file()


def test_rq2_definite_release_leaves_a_concurrent_replacement_untouched(
    tmp_path: Path,
) -> None:
    """A stale definite-failure cleanup cannot clear a replacement tuple."""
    from django_ray.ray_job_request_storage import (
        register_and_attach_ray_job_request,
        release_ray_job_request_reservation,
    )

    execution, handle = _rq2_reserved_execution("postgres-rq2-release-replacement-001")
    prepared = _prepared_rq2_request(execution, tmp_path)
    register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
    )
    replacement_locked = Event()
    release_replacement = Event()
    release_connected = Event()
    release_backend_pid: list[int] = []

    def replace_execution() -> dict[str, Any]:
        close_old_connections()
        try:
            with transaction.atomic():
                current = RayTaskExecution.objects.get(pk=execution.pk)
                assert mark_task_lost(current) is True
                retried = retry_task(
                    current,
                    allowed_states=(TaskState.LOST,),
                    expected_attempt_number=execution.attempt_number,
                    expected_execution_generation=execution.execution_generation,
                )
                assert retried is not None
                replacement = RayTaskExecution.objects.values().get(pk=execution.pk)
                replacement_locked.set()
                if not release_replacement.wait(timeout=10):
                    raise TimeoutError("test did not release the rq2 replacement transaction")
                return replacement
        finally:
            close_old_connections()

    def release_request() -> bool:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                release_backend_pid.append(int(cursor.fetchone()[0]))
            release_connected.set()
            return release_ray_job_request_reservation(
                execution,
                handle,
                expected_reference=prepared.reference,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacement_future = executor.submit(replace_execution)
        try:
            assert replacement_locked.wait(timeout=10)
            cleanup_future = executor.submit(release_request)
            assert release_connected.wait(timeout=10)
            _wait_for_postgresql_lock(release_backend_pid[0])
        finally:
            release_replacement.set()
        replacement = replacement_future.result(timeout=20)
        assert cleanup_future.result(timeout=20) is False

    assert RayTaskExecution.objects.values().get(pk=execution.pk) == replacement
    assert replacement["state"] == TaskState.QUEUED
    assert replacement["ray_job_id"] is None
    assert replacement["ray_job_request_reference"] is None
    payload = TaskInputPayload.objects.get(reference=prepared.reference)
    assert payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST
    assert payload.state == InputPayloadState.ACTIVE
    assert _rq2_request_path(prepared).is_file()


def test_rq2_completion_committed_during_status_precedes_binding_rejection(
    tmp_path: Path,
) -> None:
    """A committed exact completion wins over stale rq2 status metadata."""
    from django_ray.execution_codec import ExecutionCompletion, encode_execution_completion
    from django_ray.ray_job_protocol import build_ray_job_request_reference_metadata
    from django_ray.ray_job_request_storage import register_and_attach_ray_job_request
    from django_ray.runner.base import JobInfo, JobStatus

    command = _claim_command("postgres-rq2-completion-owner", [])
    execution, handle = _rq2_reserved_execution(
        "postgres-rq2-completion-status-001",
        worker_id=command.worker_id,
    )
    prepared = _prepared_rq2_request(execution, tmp_path)
    register_and_attach_ray_job_request(
        prepared,
        task_execution=execution,
        submission_handle=handle,
    )
    completion_data = encode_execution_completion(
        ExecutionCompletion(
            identity=prepared.request.identity,
            execution_protocol_version=int(execution.execution_protocol_version),
            executor_django_ray_version="0.5.0-postgres-rq2-executor",
            success=True,
            result={"value": 3},
            result_reference=None,
            error=None,
            traceback=None,
            exception_type=None,
            retryable=None,
        )
    )
    metadata = build_ray_job_request_reference_metadata(
        prepared.request,
        prepared.serialized_request,
        prepared.reference,
        prepared.encoded_locator,
    )
    stale_task = RayTaskExecution.objects.get(pk=execution.pk)
    status_entered = Event()
    publication_committed = Event()
    completed_tasks: list[int] = []
    conflicting_reference = f"resultfs://sha256/{'e' * 64}?rel=ee/ee/{'e' * 64}.json&bytes=1"

    class StaleFailedStatusRunner:
        def get_status(self, _handle: object) -> JobInfo:
            status_entered.set()
            if not publication_committed.wait(timeout=10):
                raise TimeoutError("test did not publish the rq2 completion")
            return JobInfo(
                job_id=handle.ray_job_id,
                status=JobStatus.FAILED,
                metadata=metadata,
                driver_exit_code=78,
            )

        def cancel_with_status(self, _handle: object) -> CancellationOutcome:
            pytest.fail("an accepted rq2 completion must not be quarantined")

        def get_logs(self, _handle: object) -> str | None:
            pytest.fail("an accepted rq2 completion must not retrieve Ray Job logs")

    def publish_completion() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                current = RayTaskExecution.objects.select_for_update().get(pk=execution.pk)
                current.completion_data = completion_data
                current.ray_job_request_reference = conflicting_reference
                current.save(update_fields=["completion_data", "ray_job_request_reference"])
        finally:
            close_old_connections()
        publication_committed.set()

    def reconcile_status() -> None:
        close_old_connections()
        try:
            command.active_tasks = {int(execution.pk): handle.ray_job_id}
            command.active_task_identities = {
                int(execution.pk): (
                    execution.attempt_number,
                    execution.execution_generation,
                )
            }
            command._reconcile_ray_job_task(
                stale_task,
                StaleFailedStatusRunner(),
                ray_job_id=handle.ray_job_id,
                completed_tasks=completed_tasks,
                orphaned=False,
                tracked_identity=(
                    execution.attempt_number,
                    execution.execution_generation,
                ),
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reconcile_future = executor.submit(reconcile_status)
        try:
            assert status_entered.wait(timeout=10)
            publication_future = executor.submit(publish_completion)
            publication_future.result(timeout=20)
            assert reconcile_future.result(timeout=20) is None
        finally:
            publication_committed.set()

    execution.refresh_from_db()
    assert completed_tasks == [execution.pk]
    assert execution.state == TaskState.SUCCEEDED
    assert json.loads(execution.result_data or "null") == {"value": 3}
    assert execution.ray_job_request_reference == conflicting_reference
    assert execution.executor_django_ray_version == "0.5.0-postgres-rq2-executor"
    assert command.active_tasks == {}


@pytest.mark.parametrize(
    ("value", "limit_delta", "accepted"),
    [
        ("ascii-boundary", 0, True),
        ("multibyte-é", 0, True),
        ("max-plus-one", -1, False),
    ],
    ids=("exact-max", "multibyte-exact-max", "max-plus-one"),
)
def test_rq2_public_submit_enforces_utf8_request_boundary_on_postgresql(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    limit_delta: int,
    accepted: bool,
) -> None:
    """The encoded-byte ceiling fences the PostgreSQL reservation exactly."""
    import django_ray.execution_codec as codec_module
    from django_ray.conf.settings import get_settings
    from django_ray.execution_codec import (
        ExecutionIdentity,
        ExecutionRequest,
        encode_execution_request,
    )
    from django_ray.runner.errors import (
        RayJobRequestPreparationError,
        RayJobRequestPreparationRejection,
    )
    from django_ray.runner.ray_job import RayJobRunner
    from django_ray.runtime.runtime_env import normalize_runtime_env
    from django_ray.runtime.serialization import serialize_args
    from django_ray.workflow.plans import runtime_env_plan_identity

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RAY_ADDRESS": "http://postgres-ray-head:8265",
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    runtime_env = normalize_runtime_env({})
    args_json = serialize_args([value])
    kwargs_json = serialize_args({})
    task_id_suffix = value if "é" in value else str(ord(value[-1]))
    execution = _execution(
        f"postgres-rq2-request-boundary-{limit_delta}-{task_id_suffix}",
        state=TaskState.RUNNING,
        claimed_by_worker="postgres-rq2-boundary-owner",
        args_json=args_json,
        kwargs_json=kwargs_json,
        execution_protocol_version=1,
        runtime_env_profile=runtime_env.profile,
        runtime_env_json=runtime_env.serialized,
        runtime_env_hash=runtime_env.digest,
    )
    request = ExecutionRequest(
        identity=ExecutionIdentity(
            task_execution_pk=int(execution.pk),
            task_id=execution.task_id,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
        ),
        execution_protocol_version=int(execution.execution_protocol_version),
        callable_path=execution.callable_path,
        transport_version=1,
        serialized_args=args_json,
        serialized_kwargs=kwargs_json,
        input_reference=None,
        runtime_env_profile=runtime_env.profile,
        runtime_env_hash=runtime_env.digest,
        runtime_env_plan_identity=runtime_env_plan_identity(
            runtime_env,
            trust_identity=get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
        ).as_transport_dict(),
        compiled_graph_submission_transport="ray-job",
    )
    serialized_request = encode_execution_request(request)
    request_size_bytes = len(serialized_request.encode("utf-8"))
    request_limit = request_size_bytes + limit_delta
    assert request_size_bytes == request_limit + (0 if accepted else 1)
    if "é" in value:
        assert len(serialized_request) < request_size_bytes
    monkeypatch.setattr(codec_module, "EXECUTION_REQUEST_MAX_BYTES", request_limit)

    runner = RayJobRunner()
    client = _RecordingRayJobClient()
    if accepted:
        monkeypatch.setattr(runner, "_get_client", lambda _address=None: client)
        handle = runner.submit(
            task_execution=execution,
            callable_path=execution.callable_path,
            args=(value,),
            kwargs={},
        )
        execution.refresh_from_db()
        assert execution.ray_job_id == handle.ray_job_id
        assert execution.ray_job_request_reference is not None
        assert len(client.submissions) == 1
        assert TaskInputPayload.objects.filter(
            reference=execution.ray_job_request_reference,
            payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        ).exists()
    else:
        monkeypatch.setattr(
            runner,
            "_get_client",
            lambda _address=None: pytest.fail("max+1 request opened a Ray client"),
        )
        with pytest.raises(RayJobRequestPreparationError) as caught:
            runner.submit(
                task_execution=execution,
                callable_path=execution.callable_path,
                args=(value,),
                kwargs={},
            )
        assert caught.value.classification is RayJobRequestPreparationRejection.RESOURCE_LIMIT
        execution.refresh_from_db()
        assert execution.ray_job_id is None
        assert execution.ray_address is None
        assert execution.ray_job_request_reference is None
        assert not TaskInputPayload.objects.filter(
            payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        ).exists()


def test_rq2_public_submit_preserves_external_input_reference_on_postgresql(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public rq2 path nests one durable input reference without hydration."""
    import django_ray.runner.ray_job as ray_job_module
    from django_ray.ray_job_request_storage import load_ray_job_request
    from django_ray.runner.ray_job import RayJobRunner
    from django_ray.runtime.runtime_env import normalize_runtime_env

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RAY_ADDRESS": "http://postgres-ray-head:8265",
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        "MAX_INLINE_INPUT_SIZE_BYTES": 1024,
    }
    private_marker = "postgres-external-input-" + ("s" * 2048)
    prepared_input = prepare_task_input((private_marker,), {"flag": True})
    assert prepared_input.input_reference is not None
    with transaction.atomic():
        input_payload = register_task_input(prepared_input)
    assert input_payload is not None

    runtime_env = normalize_runtime_env({})
    execution = _execution(
        "postgres-rq2-external-input-001",
        state=TaskState.RUNNING,
        claimed_by_worker="postgres-rq2-public-owner",
        args_json=prepared_input.args_json,
        kwargs_json=prepared_input.kwargs_json,
        input_reference=prepared_input.input_reference,
        execution_protocol_version=1,
        runtime_env_profile=runtime_env.profile,
        runtime_env_json=runtime_env.serialized,
        runtime_env_hash=runtime_env.digest,
    )
    client = _RecordingRayJobClient()
    runner = RayJobRunner()
    monkeypatch.setattr(runner, "_get_client", lambda _address=None: client)
    monkeypatch.setattr(
        ray_job_module,
        "serialize_args",
        lambda _value: pytest.fail("external input was hydrated or reserialized"),
    )

    handle = runner.submit(
        task_execution=execution,
        callable_path=execution.callable_path,
        args=(private_marker,),
        kwargs={"flag": True},
    )

    execution.refresh_from_db()
    assert execution.ray_job_id == handle.ray_job_id
    assert execution.ray_job_request_reference is not None
    request_payload = TaskInputPayload.objects.get(
        reference=execution.ray_job_request_reference,
    )
    assert request_payload.payload_kind == InputPayloadKind.RAY_JOB_REQUEST
    assert input_payload.payload_kind == InputPayloadKind.TASK_INPUT
    assert len(client.submissions) == 1
    submission = client.submissions[0]
    encoded_locator = str(submission["entrypoint"]).rsplit(" ", maxsplit=1)[1]
    request = load_ray_job_request(encoded_locator).request
    assert request.transport_version == 2
    assert request.serialized_args == EXTERNAL_INPUT_PLACEHOLDER
    assert request.serialized_kwargs == EXTERNAL_INPUT_PLACEHOLDER
    assert request.input_reference == prepared_input.input_reference
    assert load_task_input(
        args_json=request.serialized_args,
        kwargs_json=request.serialized_kwargs,
        input_reference=request.input_reference,
    ) == ([private_marker], {"flag": True})
    assert private_marker not in json.dumps(submission, default=str)


def test_rq2_concurrent_public_submit_creates_one_remote_job_on_postgresql(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One PostgreSQL owner submits while a duplicate reports uncertainty."""
    from django_ray.runner.errors import RayJobSubmissionUncertainError
    from django_ray.runner.ray_job import RayJobRunner
    from django_ray.runtime.runtime_env import normalize_runtime_env

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RAY_ADDRESS": "http://postgres-ray-head:8265",
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    runtime_env = normalize_runtime_env({})
    execution = _execution(
        "postgres-rq2-duplicate-submit-001",
        state=TaskState.RUNNING,
        claimed_by_worker="postgres-rq2-public-owner",
        args_json="[]",
        kwargs_json="{}",
        execution_protocol_version=1,
        runtime_env_profile=runtime_env.profile,
        runtime_env_json=runtime_env.serialized,
        runtime_env_hash=runtime_env.digest,
    )
    submit_started = Event()
    release_submit = Event()

    class BlockingRayJobClient(_RecordingRayJobClient):
        def submit_job(self, **kwargs: object) -> str:
            submission_id = super().submit_job(**kwargs)
            submit_started.set()
            if not release_submit.wait(timeout=10):
                raise TimeoutError("test did not release the rq2 submission")
            return submission_id

    client = BlockingRayJobClient()
    runners = (RayJobRunner(), RayJobRunner())
    for runner in runners:
        monkeypatch.setattr(runner, "_get_client", lambda _address=None: client)
    start = Barrier(2)

    def submit(runner: RayJobRunner):
        close_old_connections()
        try:
            current = RayTaskExecution.objects.get(pk=execution.pk)
            start.wait(timeout=10)
            return runner.submit(
                task_execution=current,
                callable_path=current.callable_path,
                args=(),
                kwargs={},
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit, runner) for runner in runners]
        try:
            assert submit_started.wait(timeout=10)
            completed, _pending = wait(
                futures,
                timeout=10,
                return_when=FIRST_COMPLETED,
            )
            assert len(completed) == 1
            assert len(client.submissions) == 1
        finally:
            release_submit.set()
        handles = []
        uncertain = []
        for future in futures:
            try:
                handles.append(future.result(timeout=20))
            except RayJobSubmissionUncertainError as error:
                uncertain.append(error)

    assert len(handles) == 1
    assert len(uncertain) == 1
    assert uncertain[0].submission_id == handles[0].ray_job_id
    assert uncertain[0].observed_submission_id is None
    execution.refresh_from_db()
    assert execution.ray_job_id == handles[0].ray_job_id
    assert execution.ray_job_request_reference is not None
    assert (
        TaskInputPayload.objects.filter(
            payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
        ).count()
        == 1
    )


def test_rq2_duplicate_stays_uncertain_when_owner_fails_before_remote_request(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate cannot report success before a failed owner releases rq2."""
    from django_ray.runner.errors import RayJobSubmissionUncertainError
    from django_ray.runner.ray_job import RayJobRunner
    from django_ray.runtime.runtime_env import normalize_runtime_env

    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "RAY_ADDRESS": "http://postgres-ray-head:8265",
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
    }
    runtime_env = normalize_runtime_env({})
    execution = _execution(
        "postgres-rq2-owner-pre-request-failure-001",
        state=TaskState.RUNNING,
        claimed_by_worker="postgres-rq2-public-owner",
        args_json="[]",
        kwargs_json="{}",
        execution_protocol_version=1,
        runtime_env_profile=runtime_env.profile,
        runtime_env_json=runtime_env.serialized,
        runtime_env_hash=runtime_env.digest,
    )
    owner = RayJobRunner()
    duplicate = RayJobRunner()
    owner_pre_request = Event()
    release_owner = Event()
    owner_error = ConnectionError("definite pre-request failure")
    remote_calls: list[str] = []

    def fail_before_remote_request(**_kwargs: object):
        owner_pre_request.set()
        if not release_owner.wait(timeout=10):
            raise TimeoutError("test did not release the rq2 owner")
        raise owner_error

    def unexpected_client(_address=None):
        remote_calls.append("client")
        pytest.fail("pre-request race opened a Ray client")

    monkeypatch.setattr(owner, "_submit_serialized_request", fail_before_remote_request)
    monkeypatch.setattr(owner, "_get_client", unexpected_client)
    monkeypatch.setattr(duplicate, "_get_client", unexpected_client)

    def submit_owner():
        close_old_connections()
        try:
            current = RayTaskExecution.objects.get(pk=execution.pk)
            return owner.submit(
                task_execution=current,
                callable_path=current.callable_path,
                args=(),
                kwargs={},
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner_future = executor.submit(submit_owner)
        try:
            assert owner_pre_request.wait(timeout=10)
            current = RayTaskExecution.objects.get(pk=execution.pk)
            with pytest.raises(RayJobSubmissionUncertainError) as caught:
                duplicate.submit(
                    task_execution=current,
                    callable_path=current.callable_path,
                    args=(),
                    kwargs={},
                )
        finally:
            release_owner.set()
        with pytest.raises(ConnectionError) as owner_caught:
            owner_future.result(timeout=20)

    assert caught.value.submission_id == owner.submission_id(execution)
    assert owner_caught.value is owner_error
    assert remote_calls == []
    execution.refresh_from_db()
    assert execution.ray_job_id is None
    assert execution.ray_address is None
    assert execution.ray_job_request_reference is None
    assert not TaskInputPayload.objects.filter(
        payload_kind=InputPayloadKind.RAY_JOB_REQUEST,
    ).exists()


@pytest.mark.parametrize("expires_during_selection", [False, True])
def test_claim_clock_follows_independently_committed_enqueue(monkeypatch, expires_during_selection):
    from django.db.models.query import QuerySet

    import django_ray.management.commands.django_ray_worker as worker_module

    processed = []
    command = _claim_command("postgres-claim-clock", processed)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        claimant_pid = cursor.fetchone()[0]
    clock = [datetime.now(UTC) + timedelta(seconds=1)]

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(worker_module, "datetime", ControlledDateTime)
    fetch = QuerySet._fetch_all
    created = []

    def enqueue():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid = cursor.fetchone()[0]
            task = _execution(
                "postgres-enqueue-during-selection",
                created_at=clock[0],
                queue_deadline_at=clock[0] if expires_during_selection else None,
            )
            return task.pk, writer_pid
        finally:
            connection.close()

    def insert_before_claim_query(query):
        if (
            query.model is RayTaskExecution
            and query.query.select_for_update
            and query.query.order_by == ("-priority", "created_at", "pk")
            and not created
        ):
            clock[0] += timedelta(milliseconds=1)
            with ThreadPoolExecutor(max_workers=1) as pool:
                created.append(pool.submit(enqueue).result(timeout=10))
        return fetch(query)

    monkeypatch.setattr(QuerySet, "_fetch_all", insert_before_claim_query)
    assert command.claim_and_process_tasks(["default"], concurrency=1) == (
        0 if expires_during_selection else 1
    )
    task_pk, writer_pid = created[0]
    assert writer_pid != claimant_pid
    task = RayTaskExecution.objects.get(pk=task_pk)
    if expires_during_selection:
        assert processed == []
        assert task.state == TaskState.QUEUED
        assert task.started_at is None
        assert task.claimed_by_worker is None
    else:
        assert processed == [task.pk]
        assert task.state == TaskState.RUNNING
        assert task.created_at <= task.started_at == clock[0]


@pytest.mark.parametrize("automatic", [False, True])
def test_concurrent_legacy_runtime_env_retries_preserve_row(automatic) -> None:
    from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError

    task = _execution(
        "postgres-legacy-runtime-env-retry",
        state=TaskState.RUNNING if automatic else TaskState.FAILED,
        attempt_number=3,
        execution_generation=7,
        runtime_env_profile=None,
        runtime_env_json="{}",
        runtime_env_hash="",
        result_data='{"retained":true}',
        error_message="retained diagnostic",
    )
    before = RayTaskExecution.objects.filter(pk=task.pk).values().get()

    def refuse():
        with pytest.raises(
            RuntimeEnvSnapshotError, match="Legacy RuntimeEnv snapshot cannot execute"
        ):
            if automatic:
                record_failure(task, error_message="new failure", retry=True)
            else:
                retry_task(task.pk)
        return "refused"

    assert _run_concurrently(refuse, refuse) == ["refused", "refused"]
    assert RayTaskExecution.objects.filter(pk=task.pk).values().get() == before
    assert not TaskAttempt.objects.filter(execution=task).exists()


def test_unregistered_object_reap_coordinates_with_inflight_writer_on_postgresql(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-flight writer blocks on unregistered tombstone lock and restores payload."""
    import django_ray.input_storage as input_storage_module
    from django_ray.payload_registry import discover_and_reap_unregistered_payloads

    storage_config = {
        "INPUT_STORAGE_BACKEND": "filesystem",
        "INPUT_STORAGE_FILESYSTEM_PATH": str(tmp_path),
        "MAX_INLINE_INPUT_SIZE_BYTES": 16,
    }
    settings.DJANGO_RAY = {**settings.DJANGO_RAY, **storage_config}
    prepared = prepare_task_input(("postgres-inflight-unregistered-reap-payload",), {"ok": True})
    assert prepared.input_reference is not None
    object_path = next(tmp_path.rglob("*.json"))
    assert object_path.is_file()

    deletion_started = Event()
    release_deletion = Event()
    register_connected = Event()
    register_backend_pid: list[int] = []
    original_delete = input_storage_module.delete_input_reference

    def blocking_delete(reference: str, *, config=None) -> None:
        original_delete(reference, config=config or storage_config)
        deletion_started.set()
        if not release_deletion.wait(timeout=10):
            raise TimeoutError("test did not release unregistered object deletion")

    monkeypatch.setattr(input_storage_module, "delete_input_reference", blocking_delete)

    def reap_unregistered():
        close_old_connections()
        try:
            return discover_and_reap_unregistered_payloads(
                grace_seconds=60,
                now=timezone.now() + timedelta(seconds=120),
                delete_unregistered=True,
                max_objects=10,
                max_pages=2,
            )
        finally:
            close_old_connections()

    def finish_writer_registration():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                register_backend_pid.append(int(cursor.fetchone()[0]))
            register_connected.set()
            with transaction.atomic():
                return register_task_input(prepared)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reap_future = executor.submit(reap_unregistered)
        try:
            assert deletion_started.wait(timeout=10)
            assert not object_path.exists()
            register_future = executor.submit(finish_writer_registration)
            assert register_connected.wait(timeout=10)
            _wait_for_postgresql_lock(register_backend_pid[0])
        finally:
            release_deletion.set()
        summary = reap_future.result(timeout=20)
        restored_row = register_future.result(timeout=20)

    assert summary.counts["purged"] == 1
    assert restored_row is not None
    restored_row.refresh_from_db()
    assert restored_row.state == InputPayloadState.ACTIVE
    assert restored_row.purged_at is None
    assert object_path.is_file()
    assert load_task_input(
        args_json=prepared.args_json,
        kwargs_json=prepared.kwargs_json,
        input_reference=prepared.input_reference,
    ) == (["postgres-inflight-unregistered-reap-payload"], {"ok": True})
