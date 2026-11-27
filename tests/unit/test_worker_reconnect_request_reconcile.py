"""Focused DB-backed tests for worker request preparation and strict binding reconcile."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote

import pytest
from django.db import transaction

from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
)
from django_ray.ray_job_protocol import (
    STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX,
    STRICT_RAY_JOB_SUBMISSION_ID_FAMILY_PREFIX,
    STRICT_RAY_JOB_SUBMISSION_ID_PREFIX,
)
from django_ray.runner import (
    RayJobSubmissionUncertainError,
)
from django_ray.runner.base import JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
)
from tests.unit._worker_reconnect_helpers import (
    RealCommandLeaseTestCase,
    _make_command,
    _ray_job_handle,
    _rq2_ray_job_id_for_metadata,
    _rq2_ray_job_metadata,
    _rq2_request_reference,
    _strict_ray_job_id,
    _strict_ray_job_metadata,
    _versioned_completion_json,
)


@pytest.mark.django_db
class TestWorkerReconnectRequestReconcile(RealCommandLeaseTestCase):
    """DB-backed tests for request preparation and strict binding reconciliation."""

    def test_returned_ray_job_identity_mismatch_is_unbound_and_reconciled(
        self,
        monkeypatch,
        django_capture_on_commit_callbacks,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-mismatch-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        reserved_handle = _ray_job_handle(
            task,
            "raysubmit_django_ray_v1_reserved",
            "ray://cluster:10001",
        )
        observed_submission_id = (
            f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'e' * 64}"
        )
        cancelled: list[str] = []

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                raise RayJobSubmissionUncertainError(
                    reserved_handle.ray_job_id,
                    "Ray returned another identity",
                    observed_submission_id=observed_submission_id,
                )

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        with django_capture_on_commit_callbacks(execute=True):
            cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert cancelled == []
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.cancellation_status is None
        assert cmd.active_tasks[task.pk] == reserved_handle.ray_job_id
        assert cmd.active_task_identities[task.pk] == (2, 7)
        assert observed_submission_id not in cmd.stdout.getvalue()
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_untrusted_returned_submission_identity_is_not_retained_or_cancelled(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-untrusted-return-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        reserved_handle = _ray_job_handle(
            task,
            f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'d' * 64}",
            "ray://cluster:10001",
        )
        private_observed_id = "private-returned-id-" + "x" * 20_000
        cancelled: list[str] = []

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                raise RayJobSubmissionUncertainError(
                    reserved_handle.ray_job_id,
                    "private detail that must not be retained",
                    observed_submission_id=private_observed_id,
                )

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert cancelled == []
        assert task.state == TaskState.RUNNING
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.ray_job_request_reference is None
        assert cmd.active_tasks[task.pk] == reserved_handle.ray_job_id
        assert cmd.active_task_identities[task.pk] == (2, 7)
        assert private_observed_id not in cmd.stdout.getvalue()
        assert "private detail" not in cmd.stdout.getvalue()
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_mismatched_submission_rollback_retains_exact_tracking(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-mismatch-rollback-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_django_ray_v1_reserved_rollback",
            ray_address="ray://cluster:10001",
            attempt_number=2,
            execution_generation=7,
        )
        reserved_handle = _ray_job_handle(
            task,
            task.ray_job_id or "",
            task.ray_address or "",
        )
        observed_handle = _ray_job_handle(
            task,
            "raysubmit_unexpected_rollback",
            task.ray_address or "",
        )
        cancelled: list[str] = []
        events: list[str] = []

        select_for_update = RayTaskExecution.objects.select_for_update

        def tracked_select_for_update(*args, **kwargs):
            events.append("lock")
            return select_for_update(*args, **kwargs)

        monkeypatch.setattr(
            RayTaskExecution.objects,
            "select_for_update",
            tracked_select_for_update,
        )

        class FakeRunner:
            def prepare_cancellation(self, handle):
                events.append(f"prepare:{handle.ray_job_id}")
                return handle.ray_job_id

            def cancel_prepared_with_status(self, handle, capability):
                assert capability == handle.ray_job_id
                events.append(f"cancel:{handle.ray_job_id}")
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        cmd = _make_command(worker_id="worker-coverage")
        cmd.active_tasks = {task.pk: reserved_handle.ray_job_id}
        cmd.active_task_identities = {task.pk: (2, 7)}

        with pytest.raises(RuntimeError, match="commit failed"):
            with transaction.atomic():
                cmd._handle_mismatched_ray_job_submission(
                    task,
                    FakeRunner(),
                    reserved_handle,
                    observed_handle,
                    expected_worker_id="worker-coverage",
                    expected_attempt_number=2,
                    expected_execution_generation=7,
                    error_message="Ray returned another identity",
                    exception_type="RayJobSubmissionIdentityMismatch",
                )
                assert cmd.active_tasks[task.pk] == reserved_handle.ray_job_id
                assert cmd.active_task_identities[task.pk] == (2, 7)
                raise RuntimeError("commit failed")

        task.refresh_from_db()
        assert cancelled == [
            observed_handle.ray_job_id,
            reserved_handle.ray_job_id,
        ]
        assert events[:3] == [
            f"prepare:{reserved_handle.ray_job_id}",
            f"prepare:{observed_handle.ray_job_id}",
            "lock",
        ]
        assert task.state == TaskState.RUNNING
        assert cmd.active_tasks[task.pk] == reserved_handle.ray_job_id
        assert cmd.active_task_identities[task.pk] == (2, 7)
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_mismatched_completion_quiesces_both_jobs_and_closes_channel(
        self,
        django_capture_on_commit_callbacks,
    ) -> None:
        from django_ray.runtime.entrypoint import _persist_task_completion

        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-mismatch-completion-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_mismatch_completion_reserved",
            ray_address="ray://cluster:10001",
            attempt_number=2,
            execution_generation=7,
        )
        stale_snapshot = RayTaskExecution.objects.get(pk=task.pk)
        completion_data = json.dumps({"success": True, "result": 42})
        RayTaskExecution.objects.filter(pk=task.pk).update(completion_data=completion_data)
        reserved_handle = _ray_job_handle(
            task,
            task.ray_job_id or "",
            task.ray_address or "",
        )
        observed_handle = _ray_job_handle(
            task,
            "raysubmit_mismatch_completion_observed",
            task.ray_address or "",
        )
        cancelled: list[str] = []

        class FakeRunner:
            def prepare_cancellation(self, handle):
                return handle.ray_job_id

            def cancel_prepared_with_status(self, handle, capability):
                assert capability == handle.ray_job_id
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        cmd = _make_command(worker_id="worker-coverage")
        cmd.active_tasks = {task.pk: reserved_handle.ray_job_id}
        cmd.active_task_identities = {task.pk: (2, 7)}

        with django_capture_on_commit_callbacks(execute=True):
            cmd._handle_mismatched_ray_job_submission(
                stale_snapshot,
                FakeRunner(),
                reserved_handle,
                observed_handle,
                expected_worker_id="worker-coverage",
                expected_attempt_number=2,
                expected_execution_generation=7,
                error_message="Ray returned another identity",
                exception_type="RayJobSubmissionIdentityMismatch",
            )

        task.refresh_from_db()
        assert cancelled == [observed_handle.ray_job_id, reserved_handle.ray_job_id]
        assert task.state == TaskState.SUCCEEDED
        assert json.loads(task.result_data or "null") == 42
        assert task.completion_data == completion_data
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities
        assert (
            TaskAttempt.objects.filter(
                execution=task,
                attempt_number=2,
                state=TaskState.SUCCEEDED,
            ).count()
            == 1
        )

        _persist_task_completion(
            task.pk,
            task.attempt_number,
            task.execution_generation,
            json.dumps({"success": True, "result": 99}),
        )
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.completion_data == completion_data
        assert json.loads(task.result_data or "null") == 42

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            (
                (
                    CancellationOutcomeStatus.NOT_APPLICABLE,
                    CancellationOutcomeStatus.NOT_APPLICABLE,
                ),
                CancellationOutcomeStatus.NOT_APPLICABLE,
            ),
            (
                (
                    CancellationOutcomeStatus.REQUESTED,
                    CancellationOutcomeStatus.NOT_APPLICABLE,
                ),
                CancellationOutcomeStatus.REQUESTED,
            ),
            (
                (
                    CancellationOutcomeStatus.REQUESTED,
                    CancellationOutcomeStatus.FAILED,
                ),
                CancellationOutcomeStatus.INDETERMINATE,
            ),
            (
                (
                    CancellationOutcomeStatus.FAILED,
                    CancellationOutcomeStatus.FAILED,
                ),
                CancellationOutcomeStatus.FAILED,
            ),
        ],
    )
    def test_mismatched_submission_combines_known_quiescent_outcomes(
        self,
        statuses,
        expected,
    ) -> None:
        cmd = _make_command()
        reserved_handle = SubmissionHandle(
            ray_job_id="raysubmit_reserved",
            ray_address="ray://cluster:10001",
            submitted_at=datetime.now(UTC),
        )
        observed_handle = SubmissionHandle(
            ray_job_id="raysubmit_observed",
            ray_address="ray://cluster:10001",
            submitted_at=datetime.now(UTC),
        )

        class Runner:
            def cancel_with_status(self, handle):
                index = 0 if handle.ray_job_id == reserved_handle.ray_job_id else 1
                return CancellationOutcome(statuses[index])

        outcome = cmd._cancel_mismatched_submissions(
            Runner(),
            reserved_handle,
            observed_handle,
        )

        assert outcome.status == expected

    def test_untracked_submission_preserves_unexpected_cancellation_error(
        self,
        monkeypatch,
    ) -> None:
        cmd = _make_command()
        handle = SubmissionHandle(
            ray_job_id="raysubmit_untracked",
            ray_address="ray://cluster:10001",
            submitted_at=datetime.now(UTC),
        )

        def raise_unexpected(*_args, **_kwargs):
            raise RuntimeError("control channel broke")

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.request_remote_cancellation",
            raise_unexpected,
        )

        outcome = cmd._cancel_untracked_submission(
            object(),
            handle,
            backend_name="test Ray Job",
        )

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert outcome.message == (
            "Cancellation request raised RuntimeError: control channel broke"
        )
        assert "cancellation INDETERMINATE" in cmd.stdout.getvalue()

    @pytest.mark.parametrize(
        ("suffix", "completion_data", "expected_error", "expected_traceback"),
        [
            (
                "failure",
                json.dumps(
                    {
                        "success": False,
                        "result": None,
                        "error": "task failed",
                        "traceback": "remote traceback",
                        "exception_type": "builtins.ValueError",
                    }
                ),
                "completion reported: task failed",
                "remote traceback",
            ),
            (
                "invalid",
                "{not-json",
                "completion envelope was rejected (malformed_legacy)",
                None,
            ),
        ],
    )
    def test_mismatched_submission_terminalizes_non_success_completion(
        self,
        suffix,
        completion_data,
        expected_error,
        expected_traceback,
    ) -> None:
        cmd = _make_command()
        task = RayTaskExecution.objects.create(
            task_id=f"ray-job-mismatch-{suffix}-completion-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id=f"raysubmit_mismatch_{suffix}_reserved",
            ray_address="ray://cluster:10001",
            attempt_number=2,
            execution_generation=7,
            completion_data=completion_data,
        )
        reserved_handle = _ray_job_handle(
            task,
            task.ray_job_id or "",
            task.ray_address or "",
        )

        handled = cmd._terminalize_mismatched_ray_job_submission(
            task,
            reserved_handle=reserved_handle,
            expected_worker_id=cmd.worker_id,
            expected_attempt_number=2,
            expected_execution_generation=7,
            error_message="Ray returned another identity",
            exception_type="RayJobSubmissionIdentityMismatch",
            cancellation=CancellationOutcome(
                CancellationOutcomeStatus.INDETERMINATE,
                "stop outcome unknown",
            ),
        )

        assert handled is True
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert expected_error in (task.error_message or "")
        assert task.error_traceback == expected_traceback
        assert task.cancellation_status == CancellationStatus.INDETERMINATE
        assert task.cancellation_error == "stop outcome unknown"

    def test_mismatched_submission_canonicalizes_legacy_success_reference(
        self,
        monkeypatch,
    ) -> None:
        digest = "a" * 64
        prefix = "tenant alpha/résults+100%"
        key = f"{prefix}/{digest[:2]}/{digest[2:4]}/{digest}.json"
        legacy_reference = f"s3://historical-results/{key}?bytes=256"
        canonical_reference = f"s3://historical-results/{quote(key, safe='/-._~')}?bytes=256"
        cmd = _make_command()
        task = RayTaskExecution.objects.create(
            task_id="ray-job-mismatch-legacy-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_mismatch_legacy_reserved",
            ray_address="ray://cluster:10001",
            attempt_number=2,
            execution_generation=7,
            completion_data=json.dumps(
                {
                    "success": True,
                    "result": None,
                    "result_reference": legacy_reference,
                }
            ),
        )
        reserved_handle = _ray_job_handle(
            task,
            task.ray_job_id or "",
            task.ray_address or "",
        )
        monkeypatch.setattr(
            "django_ray.result_storage.get_settings",
            lambda: {
                "RESULT_STORAGE_BACKEND": "gcs",
                "RESULT_STORAGE_S3_BUCKET": "historical-results",
                "RESULT_STORAGE_S3_PREFIX": prefix,
            },
        )

        handled = cmd._terminalize_mismatched_ray_job_submission(
            task,
            reserved_handle=reserved_handle,
            expected_worker_id=cmd.worker_id,
            expected_attempt_number=2,
            expected_execution_generation=7,
            error_message="Ray returned another identity",
            exception_type="RayJobSubmissionIdentityMismatch",
            cancellation=CancellationOutcome(CancellationOutcomeStatus.NOT_APPLICABLE),
        )

        assert handled is True
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data is None
        assert task.result_reference == canonical_reference

    def test_mismatched_submission_stops_observed_before_durable_job_on_abort(
        self,
    ) -> None:
        cmd = _make_command()
        reserved_handle = SubmissionHandle(
            ray_job_id="raysubmit_reserved",
            ray_address="ray://cluster:10001",
            submitted_at=datetime.now(UTC),
        )
        observed_handle = SubmissionHandle(
            ray_job_id="raysubmit_observed",
            ray_address="ray://cluster:10001",
            submitted_at=datetime.now(UTC),
        )
        cancelled: list[str] = []

        class AbruptWorkerExit(BaseException):
            pass

        class Runner:
            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                raise AbruptWorkerExit

        with pytest.raises(AbruptWorkerExit):
            cmd._cancel_mismatched_submissions(
                Runner(),
                reserved_handle,
                observed_handle,
            )

        assert cancelled == [observed_handle.ray_job_id]

    @pytest.mark.parametrize("transferred_state", [TaskState.RUNNING, TaskState.CANCELLING])
    def test_returned_ray_job_identity_mismatch_does_not_stop_adopted_reservation(
        self,
        monkeypatch,
        transferred_state: str,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-mismatch-adopted-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="expired-worker",
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command(worker_id="expired-worker")
        reserved_handle = _ray_job_handle(
            task,
            "raysubmit_django_ray_v1_adopted_reserved",
            "ray://cluster:10001",
        )
        observed_submission_id = (
            f"{STRICT_RAY_JOB_REQUEST_REFERENCE_SUBMISSION_ID_PREFIX}{'c' * 64}"
        )
        cancelled: list[str] = []

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    claimed_by_worker="replacement-worker",
                    state=transferred_state,
                )
                raise RayJobSubmissionUncertainError(
                    reserved_handle.ray_job_id,
                    "Ray returned another identity after adoption",
                    observed_submission_id=observed_submission_id,
                )

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert cancelled == []
        assert task.state == transferred_state
        assert task.claimed_by_worker == "replacement-worker"
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities
        assert observed_submission_id not in cmd.stdout.getvalue()
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_ray_job_post_submit_tracking_exception_retains_durable_reservation(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-tracking-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        reserved_handle = _ray_job_handle(
            task,
            "raysubmit_django_ray_v1_tracking_failure",
        )

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                return reserved_handle

            def cancel(self, _handle):
                pytest.fail("a durable exact reservation must remain reconcilable")

        original_persist = cmd._persist_submission_tracking
        persist_calls = 0

        def fail_post_submit(*args, **kwargs):
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 2:
                raise RuntimeError("database confirmation failed")
            return original_persist(*args, **kwargs)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )
        monkeypatch.setattr(cmd, "_persist_submission_tracking", fail_post_submit)

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert persist_calls == 2
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 1
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.cancellation_status is None
        assert cmd.active_tasks[task.pk] == reserved_handle.ray_job_id
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert "retaining exact tracking" in cmd.stdout.getvalue()

    @pytest.mark.parametrize("uncertain", [False, True])
    def test_ray_job_confirmation_does_not_cancel_same_identity_after_owner_transfer(
        self,
        monkeypatch,
        uncertain,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id=f"ray-job-submit-owner-transfer-{uncertain}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="expired-worker",
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command(worker_id="expired-worker")
        reserved_handle = _ray_job_handle(
            task,
            "raysubmit_django_ray_v1_owner_transfer",
        )

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    claimed_by_worker="replacement-worker"
                )
                if uncertain:
                    raise RayJobSubmissionUncertainError(
                        reserved_handle.ray_job_id,
                        "response timed out after ownership transfer",
                    )
                return reserved_handle

            def cancel(self, _handle):
                pytest.fail("an adopted exact submission must not be cancelled")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == "replacement-worker"
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert "ownership moved to replacement-worker" in cmd.stdout.getvalue()

    def test_ray_job_post_attachment_error_does_not_fail_live_submission(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-post-attach-error-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        failures: list[dict[str, Any]] = []

        class BrokenStdout:
            def write(self, _message: str) -> None:
                raise RuntimeError("stdout unavailable")

        reserved_handle = _ray_job_handle(task, "raysubmit_post_attach_001")

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                return reserved_handle

        cmd.stdout = cast(Any, BrokenStdout())
        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda _task, **kwargs: failures.append(kwargs),
        )

        with pytest.raises(RuntimeError, match="stdout unavailable"):
            cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert failures == []
        assert task.state == TaskState.RUNNING
        assert task.ray_job_id == "raysubmit_post_attach_001"
        assert task.ray_address == "ray://cluster:10001"
        assert cmd.active_tasks[task.pk] == "raysubmit_post_attach_001"
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_stale_ray_job_submission_is_stopped_without_attaching(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-stale-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=4,
        )
        cmd = _make_command()
        cancelled: list[str] = []
        reserved_handle = _ray_job_handle(
            task,
            "raysubmit_stale",
            "ray://old-driver:10001",
        )

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    attempt_number=2,
                    execution_generation=5,
                    claimed_by_worker="replacement-worker",
                    ray_job_id="raysubmit_replacement",
                    ray_address="ray://replacement:10001",
                )
                return reserved_handle

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert cancelled == ["raysubmit_stale"]
        assert cmd.active_tasks == {}
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 5
        assert task.claimed_by_worker == "replacement-worker"
        assert task.ray_job_id == "raysubmit_replacement"
        assert task.ray_address == "ray://replacement:10001"
        assert "Discarded stale replaced Ray Job submission" in cmd.stdout.getvalue()

    def test_submit_task_to_ray_preserves_external_input_reference(self, monkeypatch) -> None:
        reference = "s3://inputs/django-ray/inputs/aa/input.json?bytes=4"
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-reference-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="null",
            kwargs_json="null",
            input_reference=reference,
        )
        cmd = _make_command()
        captured: dict[str, Any] = {}
        reserved_handle = _ray_job_handle(task, "raysubmit_reference_001")

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **kwargs: Any) -> SubmissionHandle:
                captured.update(kwargs)
                return reserved_handle

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        assert captured == {"task_execution": task}
        assert task.input_reference == reference

    def test_ray_job_submit_exception_does_not_fail_replacement_attempt(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-stale-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=7,
        )
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )
        reserved_handle = _ray_job_handle(task, "raysubmit_stale_failure")

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    attempt_number=2,
                    claimed_by_worker="replacement-worker",
                )
                raise RuntimeError("old submit failed")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.claimed_by_worker == "replacement-worker"
        assert task.error_message is None
        assert not TaskAttempt.objects.filter(execution=task, attempt_number=2).exists()

    def test_reconcile_tasks_returns_early_for_sync_or_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "django_ray.runner.ray_job.RayJobRunner",
            lambda: SimpleNamespace(),
        )
        cmd = _make_command()
        cmd.sync_mode = True
        cmd.active_tasks = {1: "raysubmit_x"}
        cmd.reconcile_tasks()

        cmd.sync_mode = False
        cmd.active_tasks = {}
        cmd.reconcile_tasks()

    def test_reconcile_leaves_cancelling_ray_job_for_remote_stop(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-cancelling-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_cancelling_001",
            claimed_by_worker="worker-coverage",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: "raysubmit_cancelling_001"}
        cancelled: list[str] = []

        class FakeRunner:
            def get_status(self, _handle):
                raise AssertionError("reconciliation must not poll a cancelling Ray Job")

            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert cancelled == []
        assert task.state == TaskState.CANCELLING
        assert task.finished_at is None
        assert task.pk in cmd.active_tasks

        cmd.process_cancellations()

        task.refresh_from_db()
        assert cancelled == ["raysubmit_cancelling_001"]
        assert task.state == TaskState.CANCELLED
        assert task.cancellation_status == CancellationOutcomeStatus.REQUESTED
        assert task.finished_at is not None
        assert task.pk not in cmd.active_tasks

    def test_reconcile_strict_completion_published_during_status_wins_over_binding(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="strict-completion-during-status-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id=_strict_ray_job_id("0"),
            started_at=datetime.now(UTC),
        )
        completion_data = _versioned_completion_json(task, result={"value": 3})
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(completion_data=completion_data)
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.FAILED,
                    metadata=None,
                    driver_exit_code=78,
                )

            def cancel(self, _handle):
                pytest.fail("an exact durable completion must not be quarantined")

            def get_logs(self, _handle):
                pytest.fail("an exact durable completion must not retrieve logs")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert json.loads(task.result_data or "null") == {"value": 3}
        assert task.pk not in cmd.active_tasks

    def test_reconcile_rq2_running_job_binds_exact_durable_reference(
        self,
        monkeypatch,
    ) -> None:
        reference = _rq2_request_reference("a" * 64)
        task = RayTaskExecution.objects.create(
            task_id="rq2-running-binding-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_request_reference=reference,
            started_at=datetime.now(UTC),
        )
        metadata = _rq2_ray_job_metadata(task)
        task.ray_job_id = _rq2_ray_job_id_for_metadata(metadata)
        task.save(update_fields=["ray_job_id"])
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 0

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.ray_job_request_reference == reference
        assert task.last_heartbeat_at is not None
        assert cmd.active_tasks[task.pk] == task.ray_job_id

    @pytest.mark.parametrize("mismatch", ["missing", "replaced"])
    def test_reconcile_rq2_reference_ambiguity_stops_without_retry(
        self,
        monkeypatch,
        mismatch: str,
    ) -> None:
        metadata_reference = _rq2_request_reference("b" * 64)
        durable_reference = None if mismatch == "missing" else _rq2_request_reference("c" * 64)
        task = RayTaskExecution.objects.create(
            task_id=f"rq2-reference-{mismatch}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_request_reference=durable_reference,
            started_at=datetime.now(UTC),
        )
        metadata = _rq2_ray_job_metadata(
            task,
            request_reference=metadata_reference,
        )
        ray_job_id = _rq2_ray_job_id_for_metadata(metadata)
        task.ray_job_id = ray_job_id
        task.save(update_fields=["ray_job_id"])
        cmd = _make_command()
        cmd.active_tasks = {task.pk: ray_job_id}
        cancelled: list[str] = []

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=ray_job_id,
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.should_retry",
            lambda *_args, **_kwargs: pytest.fail(
                "rq2 reference ambiguity must never consume automatic retry policy"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert cancelled == [ray_job_id]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.cancellation_status == CancellationStatus.REQUESTED
        expected = "missing" if mismatch == "missing" else "digest_mismatch"
        assert expected in (task.error_message or "")
        assert task.pk not in cmd.active_tasks

    @pytest.mark.parametrize(
        ("metadata_field", "replacement"),
        [
            ("django_ray_request_sha256", "f" * 64),
            ("django_ray_request_size_bytes", "129"),
        ],
    )
    def test_reconcile_rq2_request_content_mismatch_stops_without_retry(
        self,
        monkeypatch,
        metadata_field: str,
        replacement: str,
    ) -> None:
        reference = _rq2_request_reference("4" * 64)
        task = RayTaskExecution.objects.create(
            task_id=f"rq2-content-{metadata_field}-mismatch-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_request_reference=reference,
            started_at=datetime.now(UTC),
        )
        metadata = _rq2_ray_job_metadata(task)
        metadata[metadata_field] = replacement
        ray_job_id = _rq2_ray_job_id_for_metadata(metadata)
        task.ray_job_id = ray_job_id
        task.save(update_fields=["ray_job_id"])
        cmd = _make_command()
        cmd.active_tasks = {task.pk: ray_job_id}
        cancelled: list[str] = []

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=ray_job_id,
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.should_retry",
            lambda *_args, **_kwargs: pytest.fail(
                "rq2 request-content ambiguity must never consume automatic retry policy"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert cancelled == [ray_job_id]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert "digest_mismatch" in (task.error_message or "")
        assert task.pk not in cmd.active_tasks

    def test_reconcile_rq2_malformed_durable_reference_stops_without_retry(
        self,
        monkeypatch,
    ) -> None:
        expected_reference = _rq2_request_reference("5" * 64)
        task = RayTaskExecution.objects.create(
            task_id="rq2-malformed-reference-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_request_reference="not-a-canonical-reference",
            started_at=datetime.now(UTC),
        )
        metadata = _rq2_ray_job_metadata(task, request_reference=expected_reference)
        ray_job_id = _rq2_ray_job_id_for_metadata(metadata)
        task.ray_job_id = ray_job_id
        task.save(update_fields=["ray_job_id"])
        cmd = _make_command()
        cmd.active_tasks = {task.pk: ray_job_id}
        cancelled: list[str] = []

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=ray_job_id,
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.should_retry",
            lambda *_args, **_kwargs: pytest.fail(
                "malformed rq2 references must never consume automatic retry policy"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert cancelled == [ray_job_id]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert "invalid_locator" in (task.error_message or "")
        assert task.pk not in cmd.active_tasks

    def test_reconcile_rq2_submission_id_digest_mismatch_stops_without_retry(
        self,
        monkeypatch,
    ) -> None:
        reference = _rq2_request_reference("6" * 64)
        task = RayTaskExecution.objects.create(
            task_id="rq2-submission-id-mismatch-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_request_reference=reference,
            started_at=datetime.now(UTC),
        )
        metadata = _rq2_ray_job_metadata(task)
        correct_ray_job_id = _rq2_ray_job_id_for_metadata(metadata)
        replacement = "0" if correct_ray_job_id[-1] != "0" else "1"
        ray_job_id = f"{correct_ray_job_id[:-1]}{replacement}"
        task.ray_job_id = ray_job_id
        task.save(update_fields=["ray_job_id"])
        cmd = _make_command()
        cmd.active_tasks = {task.pk: ray_job_id}
        cancelled: list[str] = []

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=ray_job_id,
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.should_retry",
            lambda *_args, **_kwargs: pytest.fail(
                "rq2 submission-ID ambiguity must never consume automatic retry policy"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert cancelled == [ray_job_id]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert "identity_mismatch" in (task.error_message or "")
        assert task.pk not in cmd.active_tasks

    def test_reconcile_rq2_completion_published_during_status_keeps_precedence(
        self,
        monkeypatch,
    ) -> None:
        original_reference = _rq2_request_reference("d" * 64)
        task = RayTaskExecution.objects.create(
            task_id="rq2-completion-precedence-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_request_reference=original_reference,
            started_at=datetime.now(UTC),
        )
        completion_data = _versioned_completion_json(task, result={"value": 3})
        metadata = _rq2_ray_job_metadata(task)
        task.ray_job_id = _rq2_ray_job_id_for_metadata(metadata)
        task.save(update_fields=["ray_job_id"])
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    completion_data=completion_data,
                    ray_job_request_reference=_rq2_request_reference("e" * 64),
                )
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.FAILED,
                    metadata=metadata,
                    driver_exit_code=78,
                )

            def cancel_with_status(self, _handle):
                pytest.fail("an accepted completion must precede rq2 binding quarantine")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert json.loads(task.result_data or "null") == {"value": 3}
        assert task.pk not in cmd.active_tasks

    def test_reconcile_rq2_revalidates_reference_under_authoritative_lock(
        self,
        monkeypatch,
    ) -> None:
        expected_reference = _rq2_request_reference("1" * 64)
        stale_reference = _rq2_request_reference("2" * 64)
        task = RayTaskExecution.objects.create(
            task_id="rq2-reference-lock-revalidation-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_request_reference=stale_reference,
            started_at=datetime.now(UTC),
        )
        metadata = _rq2_ray_job_metadata(
            task,
            request_reference=expected_reference,
        )
        ray_job_id = _rq2_ray_job_id_for_metadata(metadata)
        task.ray_job_id = ray_job_id
        task.save(update_fields=["ray_job_id"])
        cmd = _make_command()
        cmd.active_tasks = {task.pk: ray_job_id}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=ray_job_id,
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

            def cancel_with_status(self, _handle):
                pytest.fail("a corrected exact reference must not be quarantined")

        def correct_reference_before_lock(_runner, _handle):
            RayTaskExecution.objects.filter(pk=task.pk).update(
                ray_job_request_reference=expected_reference
            )
            return None

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.prepare_remote_cancellation",
            correct_reference_before_lock,
        )

        assert cmd.reconcile_tasks() == 0

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.ray_job_request_reference == expected_reference
        assert task.pk in cmd.active_tasks

    def test_reconcile_strict_exit_78_waits_then_uses_generic_fixed_failure(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="strict-exit-78-generic-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id=_strict_ray_job_id("1"),
            started_at=datetime.now(UTC),
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        metadata = _strict_ray_job_metadata(task)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.FAILED,
                    message="untrusted Ray failure detail",
                    metadata=metadata,
                    driver_exit_code=78,
                )

            def get_logs(self, _handle):
                pytest.fail("a strict terminal failure must not retrieve logs")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.should_retry",
            lambda *_args, **_kwargs: pytest.fail(
                "fixed strict terminal policy must bypass available retries"
            ),
        )
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "a strict terminal failure must not reach result storage"
            ),
        )

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 1
        assert task.pk in cmd.active_tasks
        assert not TaskAttempt.objects.filter(execution=task).exists()

        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        RayTaskExecution.objects.filter(pk=task.pk).update(
            started_at=stale_time,
            last_heartbeat_at=stale_time,
        )
        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.error_message == (
            "Strict Ray Job terminated without an exact completion envelope"
        )
        assert "untrusted Ray failure detail" not in (task.error_message or "")
        rendered_output = cmd.stdout.getvalue().lower()
        assert "pre-import" not in rendered_output
        assert "django setup" not in rendered_output
        assert "preflight" not in rendered_output
        archived = TaskAttempt.objects.get(execution=task, attempt_number=1)
        assert archived.state == TaskState.FAILED
        assert task.pk not in cmd.active_tasks

    @pytest.mark.parametrize(
        "status",
        [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED],
    )
    def test_reconcile_strict_terminal_missing_completion_is_fixed_after_grace(
        self,
        monkeypatch,
        status: JobStatus,
    ) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id=f"strict-missing-completion-{status.value.lower()}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id=_strict_ray_job_id("2"),
            started_at=stale_time,
            last_heartbeat_at=stale_time,
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        metadata = _strict_ray_job_metadata(task)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=status,
                    message="untrusted terminal detail",
                    metadata=metadata,
                )

            def get_logs(self, _handle):
                pytest.fail("strict missing-completion policy must not retrieve logs")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "a missing strict completion must not reach result storage"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.error_message == (
            "Strict Ray Job terminated without an exact completion envelope"
        )
        assert task.pk not in cmd.active_tasks

    @pytest.mark.parametrize(
        ("binding_kind", "expected_rejection"),
        [
            ("missing", "missing"),
            ("identity_mismatch", "identity_mismatch"),
            ("invalid_submission_id", "invalid_submission_id"),
            ("future_request_family", "invalid_submission_id"),
        ],
    )
    def test_reconcile_strict_binding_ambiguity_stops_exact_job_and_records_lost(
        self,
        monkeypatch,
        binding_kind: str,
        expected_rejection: str,
    ) -> None:
        if binding_kind == "invalid_submission_id":
            ray_job_id = f"{STRICT_RAY_JOB_SUBMISSION_ID_PREFIX}not-canonical"
        elif binding_kind == "future_request_family":
            ray_job_id = f"{STRICT_RAY_JOB_SUBMISSION_ID_FAMILY_PREFIX}3_{'3' * 64}"
        else:
            ray_job_id = _strict_ray_job_id("3")
        task = RayTaskExecution.objects.create(
            task_id=f"strict-binding-{binding_kind}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id=ray_job_id,
            started_at=datetime.now(UTC),
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: ray_job_id}
        cancelled: list[str] = []
        if binding_kind == "missing":
            metadata = None
        elif binding_kind == "identity_mismatch":
            metadata = _strict_ray_job_metadata(task, task_id="another-public-task")
        else:
            metadata = _strict_ray_job_metadata(task)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=ray_job_id,
                    status=JobStatus.RUNNING,
                    message="untrusted status detail",
                    metadata=metadata,
                )

            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

            def get_logs(self, _handle):
                pytest.fail("an untrusted strict binding must not retrieve logs")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "an untrusted strict binding must not reach result storage"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert cancelled == [ray_job_id]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert expected_rejection in (task.error_message or "")
        assert "untrusted status detail" not in (task.error_message or "")
        assert task.pk not in cmd.active_tasks
