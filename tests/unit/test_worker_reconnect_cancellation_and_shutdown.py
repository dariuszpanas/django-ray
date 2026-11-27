"""Focused DB-backed tests for worker cancellation recovery and shutdown reconnect."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from django_ray.execution_codec import (
    ExecutionIdentity,
)
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.redaction import normalize_terminal_text
from django_ray.runner.base import JobInfo, JobStatus
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
)
from tests.unit._worker_reconnect_helpers import (
    RealCommandLeaseTestCase,
    _make_command,
    _rq2_ray_job_id,
    _strict_ray_job_id,
    _versioned_completion_json,
)


@pytest.mark.django_db
class TestWorkerReconnectCancellationAndShutdown(RealCommandLeaseTestCase):
    """DB-backed tests for cancellation recovery and reconnect abort paths."""

    def test_cancellation_recovery_does_not_adopt_task_from_another_queue(
        self,
        monkeypatch,
    ) -> None:
        started_at = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="cancellation-other-queue-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="ray-data",
            state=TaskState.CANCELLING,
            args_json="[]",
            kwargs_json="{}",
            started_at=started_at,
            last_heartbeat_at=started_at,
            claimed_by_worker="dead-ray-job-worker",
            ray_job_id="raysubmit_cancellation_other_queue_001",
            ray_address="ray://cluster:10001",
        )
        cmd = _make_command(worker_id="ml-worker")
        monkeypatch.setattr(
            cmd,
            "_request_cancellation_for_task",
            lambda _task: pytest.fail("out-of-queue cancellation must not reach Ray"),
        )

        assert cmd.process_cancellations(queues=("ml",)) == 0

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert task.claimed_by_worker == "dead-ray-job-worker"
        assert task.started_at == started_at
        assert task.last_heartbeat_at == started_at
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_reconcile_tasks_completes_orphaned_succeeded_ray_job(self, monkeypatch) -> None:
        orphan = RayTaskExecution.objects.create(
            task_id="reconcile-orphan-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_orphan_success_001",
            ray_address="ray://cluster:10001",
            claimed_by_worker="dead-worker",
            started_at=datetime.now(UTC),
            completion_data='{"success": true, "result": 3}',
        )
        cmd = _make_command(worker_id="adopting-worker")

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=orphan.ray_job_id or "", status=JobStatus.SUCCEEDED)

            def get_logs(self, _handle):
                return "arbitrary application stdout"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda task, result: setattr(task, "result_data", str(result)),
        )

        assert cmd.reconcile_tasks() == 1

        orphan.refresh_from_db()
        assert orphan.state == TaskState.SUCCEEDED
        assert orphan.result_data == "3"
        assert orphan.pk not in cmd.active_tasks

    def test_detect_stuck_tasks_leaves_exact_active_ray_job_to_reconciliation(
        self,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-unknown-stuck-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_unknown_001",
        )

        cmd = _make_command(worker_id="worker-coverage")
        cmd.active_tasks = {task.pk: "raysubmit_unknown_001"}
        cmd.active_task_identities = {task.pk: (task.attempt_number, task.execution_generation)}

        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 1
        assert task.run_after is None
        assert cmd.active_tasks == {task.pk: "raysubmit_unknown_001"}

    def test_detect_stuck_tasks_leaves_orphaned_ray_job_to_exact_reconciliation(
        self,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-unknown-orphan-boundary-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            claimed_by_worker="missing-worker",
            ray_job_id="raysubmit_unknown_orphan_boundary_001",
        )

        cmd = _make_command(worker_id="worker-coverage")
        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 1
        assert task.run_after is None

    def test_timeout_cancellation_client_failure_is_indeterminate(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="timeout-client-failure-001",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_timeout_client_failure_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()

        class FailingRunner:
            def __init__(self):
                raise RuntimeError("Ray client unavailable")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FailingRunner)

        outcome = cmd._request_timeout_cancellation(task)

        assert outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "Ray client unavailable" in (outcome.message or "")

    def test_reconcile_does_not_overwrite_timed_out_terminal_task(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-timeout-terminal-001",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.FAILED,
            error_message="Task timed out after 5 seconds",
            ray_job_id="raysubmit_timeout_terminal_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.error_message == "Task timed out after 5 seconds"
        assert task.pk not in cmd.active_tasks

    def test_reconcile_terminal_status_retires_tracking_but_leaves_cancellation_owner(
        self,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-cancellation-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_cancellation_race_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        completed: list[int] = []

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.CANCELLING)
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        cmd._reconcile_ray_job_task(
            task,
            FakeRunner(),
            ray_job_id=task.ray_job_id or "",
            completed_tasks=completed,
            orphaned=False,
        )

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert task.finished_at is None
        assert completed == [task.pk]

    def test_reconcile_discards_task_deleted_during_status_rpc(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-deleted-during-rpc-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_deleted_during_rpc_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        completed: list[int] = []

        class FakeRunner:
            def get_status(self, _handle):
                task.delete()
                return JobInfo(job_id="raysubmit_deleted_during_rpc_001", status=JobStatus.RUNNING)

        cmd._reconcile_ray_job_task(
            task,
            FakeRunner(),
            ray_job_id="raysubmit_deleted_during_rpc_001",
            completed_tasks=completed,
            orphaned=False,
        )

        assert completed == [task.pk]

    def test_reconcile_malformed_envelope_race_keeps_task_active(self, monkeypatch) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="reconcile-malformed-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_malformed_race_001",
            started_at=stale_time,
            last_heartbeat_at=stale_time,
            completion_data="{not-json",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.record_lost",
            lambda *_args, **_kwargs: False,
        )

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks

    def test_reconcile_success_result_race_does_not_apply_stale_update(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-success-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_success_race_001",
            completion_data='{"success": true, "result": 3}',
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        def race_store(_task, _result):
            RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.FAILED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(cmd, "_store_task_result", race_store)
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.FAILED

    def test_reconcile_failure_envelope_race_keeps_task_active(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-failure-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_failure_race_001",
            completion_data='{"success": false, "result": null, "error": "failed"}',
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        monkeypatch.setattr(cmd, "_handle_task_failure", lambda *_args, **_kwargs: False)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks

    def test_reconcile_missing_envelope_race_keeps_task_active(self, monkeypatch) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="reconcile-missing-envelope-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_missing_envelope_race_001",
            started_at=stale_time,
            last_heartbeat_at=stale_time,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.record_lost",
            lambda *_args, **_kwargs: False,
        )

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks

    def test_reconcile_failed_job_race_keeps_task_active(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-failed-job-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_failed_job_race_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.record_lost",
            lambda *_args, **_kwargs: False,
        )

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.FAILED,
                    message="failed",
                )

            def get_logs(self, _handle):
                return "traceback"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks

    def test_reconcile_failed_job_preserves_completion_published_before_loss_fence(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-failed-job-completion-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_failed_job_completion_race_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        completion_data = '{"success": true, "result": 3}'

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.FAILED,
                    message="failed",
                )

            def get_logs(self, _handle):
                raise AssertionError("untrusted failure must not fetch logs")

        from django_ray.management.commands import django_ray_worker as worker_module

        original_prepare = worker_module.prepare_remote_cancellation

        def publish_before_loss_fence(runner, handle):
            prepared = original_prepare(runner, handle)
            RayTaskExecution.objects.filter(pk=task.pk).update(completion_data=completion_data)
            return prepared

        monkeypatch.setattr(worker_module, "prepare_remote_cancellation", publish_before_loss_fence)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.completion_data == completion_data
        assert task.error_message is None
        assert task.pk in cmd.active_tasks
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_reconcile_missing_envelope_grace_preserves_concurrent_completion(
        self,
        monkeypatch,
    ) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="reconcile-missing-envelope-completion-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_missing_envelope_completion_race_001",
            started_at=stale_time,
            last_heartbeat_at=stale_time,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        completion_data = '{"success": true, "result": 3}'

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        def publish_before_expiry_decision(_task, *, now):
            RayTaskExecution.objects.filter(pk=task.pk).update(completion_data=completion_data)
            return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_completion_envelope_grace_expired",
            publish_before_expiry_decision,
        )

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.completion_data == completion_data
        assert task.error_message is None
        assert task.pk in cmd.active_tasks
        assert not TaskAttempt.objects.filter(execution=task).exists()

    @pytest.mark.parametrize(
        ("message", "expected", "absent"),
        [
            (
                "\x1b[33mstatus unavailable\x1b[39m\rnext line",
                "status unavailable\nnext line",
                "\x1b",
            ),
            ("access_token=secret-value", "[REDACTED]", "secret-value"),
        ],
        ids=("terminal-formatting", "sensitive-detail"),
    )
    def test_reconcile_reports_unknown_job_status(
        self,
        monkeypatch,
        message: str,
        expected: str,
        absent: str,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-unknown-status-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_unknown_status_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.UNKNOWN,
                    message=message,
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks
        assert "status is unknown" in cmd.stdout.getvalue()
        assert expected in cmd.stdout.getvalue()
        assert absent not in cmd.stdout.getvalue()

    def test_reconcile_stale_unknown_job_stops_exact_id_without_automatic_retry(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-unknown-status-stale-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_unknown_status_stale_001",
            args_json="[]",
            kwargs_json="{}",
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
            attempt_number=2,
            execution_generation=7,
        )
        cancelled: list[str] = []
        cmd = _make_command(worker_id="worker-coverage")
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        cmd.active_task_identities = {task.pk: (task.attempt_number, task.execution_generation)}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.UNKNOWN,
                    message="status unavailable",
                )

            def cancel_with_status(self, handle):
                cancelled.append(handle.ray_job_id)
                return CancellationOutcome(
                    CancellationOutcomeStatus.REQUESTED,
                    "\x1b[33mexact stop confirmed\x1b[39m\rremote accepted",
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert cancelled == ["raysubmit_unknown_status_stale_001"]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert task.cancellation_error == ("\x1b[33mexact stop confirmed\x1b[39m\rremote accepted")
        assert normalize_terminal_text(task.cancellation_error) == (
            "exact stop confirmed\nremote accepted"
        )
        assert task.pk not in cmd.active_tasks
        assert "automatic retry was suppressed" in cmd.stdout.getvalue()

    @pytest.mark.parametrize("poll_method", ["reconcile_tasks", "poll_ray_job_completions"])
    def test_reconcile_consumes_valid_completion_before_status_rpc(
        self, monkeypatch, poll_method
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-unknown-valid-completion-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_unknown_valid_completion_001",
            completion_data='{"success": true, "result": 3}',
            args_json="[]",
            kwargs_json="{}",
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command(worker_id="worker-coverage")
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        cmd.active_task_identities = {task.pk: (task.attempt_number, task.execution_generation)}

        class FakeRunner:
            def get_status(self, _handle):
                pytest.fail("a valid durable completion must be consumed without contacting Ray")

            def cancel(self, _handle):
                pytest.fail("a valid durable completion must be consumed before UNKNOWN recovery")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        getattr(cmd, poll_method)()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        assert task.pk not in cmd.active_tasks

    @pytest.mark.parametrize("budget", [3, 10])
    def test_encoding_failure_receipt_prevents_an_available_automatic_retry(
        self, monkeypatch, settings, budget
    ):
        from django_ray.runner.retry import should_retry
        from django_ray.runtime.entrypoint import _serialize_completion

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "MAX_TASK_ATTEMPTS": budget,
            "RETRY_EXCEPTION_DENYLIST": [],
        }
        cmd = _make_command()
        job_id = _rq2_ray_job_id()
        task = RayTaskExecution.objects.create(
            task_id="encoding-failure-receipt",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id=job_id,
            attempt_number=1,
            execution_generation=7,
            runtime_env_json="{}",
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            args_json="[]",
            kwargs_json="{}",
        )
        assert should_retry(task, "RayExecutionCompletionEncodingError").should_retry
        task.completion_data = _serialize_completion(
            success=True,
            result=float("nan"),
            result_reference=None,
            error=None,
            error_traceback=None,
            exception_type=None,
            retryable=None,
            completion_identity=ExecutionIdentity(task.pk, str(task.task_id), 1, 7),
            execution_protocol_version=task.execution_protocol_version,
        )
        task.save(update_fields=["completion_data"])
        cmd.active_tasks = {task.pk: job_id}
        cmd.active_task_identities = {task.pk: (1, 7)}
        monkeypatch.setattr(
            "django_ray.runner.ray_job.RayJobRunner",
            lambda: pytest.fail("durable failure must not need a Ray control client"),
        )
        assert cmd.poll_ray_job_completions() == 1
        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert (task.attempt_number, task.execution_generation) == (1, 7)
        assert task.result_data is None
        assert "assess them before manually retrying" in task.error_message
        assert task.pk not in cmd.active_tasks
        assert list(TaskAttempt.objects.filter(execution=task).values_list("state", flat=True)) == [
            TaskState.FAILED
        ]

    @pytest.mark.parametrize("job_id", [_strict_ray_job_id(), _rq2_ray_job_id()])
    @pytest.mark.parametrize("success", [True, False])
    def test_fast_job_receipt_uses_exact_completion_without_constructing_a_runner(
        self, monkeypatch, job_id, success
    ) -> None:
        cmd = _make_command()
        task = RayTaskExecution.objects.create(
            task_id="fast-exact-receipt",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id=job_id,
            attempt_number=2,
            execution_generation=7,
            args_json="[]",
            kwargs_json="{}",
        )
        task.completion_data = _versioned_completion_json(
            task, success=success, error="expected failure", retryable=False
        )
        task.save(update_fields=["completion_data"])
        cmd.active_tasks = {task.pk: job_id}
        cmd.active_task_identities = {task.pk: (2, 7)}
        monkeypatch.setattr(
            "django_ray.runner.ray_job.RayJobRunner",
            lambda: pytest.fail("fast receipt polling must not construct a Ray control client"),
        )
        monkeypatch.setattr(
            cmd,
            "_maintenance_scan_page",
            lambda *_args, **_kwargs: pytest.fail("fast receipt polling must not scan for orphans"),
        )

        assert cmd.poll_ray_job_completions() == 1

        task.refresh_from_db()
        assert task.state == (TaskState.SUCCEEDED if success else TaskState.FAILED)
        assert task.result_data == ("3" if success else None)
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities

    @pytest.mark.parametrize(
        "change",
        [
            {"attempt_number": 3},
            {"execution_generation": 8},
            {"claimed_by_worker": "replacement"},
            {"claimed_by_worker": None},
            {"ray_job_id": _rq2_ray_job_id("b")},
            {"state": TaskState.CANCELLING},
            pytest.param(
                {"execution_protocol_version": 2}, marks=pytest.mark.django_db(transaction=True)
            ),
            {"completion_data": "{malformed"},
            {"completion_data": None},
        ],
    )
    def test_fast_job_receipt_does_not_rewrite_replaced_or_untrusted_work(
        self, monkeypatch, change
    ) -> None:
        if "execution_protocol_version" in change:
            close_legacy_worker_admission(expected_revision=1, legacy_producers_retired=True)
        cmd = _make_command()
        task = RayTaskExecution.objects.create(
            task_id="fast-replaced-receipt",
            execution_protocol_version=change.get("execution_protocol_version", 1),
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id=_rq2_ray_job_id(),
            attempt_number=2,
            execution_generation=7,
            args_json="[]",
            kwargs_json="{}",
        )
        task.completion_data = (
            "unsupported receipt"
            if "execution_protocol_version" in change
            else _versioned_completion_json(task)
        )
        task.save(update_fields=["completion_data"])
        cmd.active_tasks = {task.pk: str(task.ray_job_id)}
        cmd.active_task_identities = {task.pk: (2, 7)}
        mutable_change = {
            key: value for key, value in change.items() if key != "execution_protocol_version"
        }
        if mutable_change:
            RayTaskExecution.objects.filter(pk=task.pk).update(**mutable_change)
        monkeypatch.setattr(
            cmd,
            "_adopt_orphaned_ray_job_task",
            lambda *_args, **_kwargs: pytest.fail("fast polling must not adopt an ownerless row"),
        )
        monkeypatch.setattr(
            "django_ray.runner.ray_job.RayJobRunner",
            lambda: pytest.fail("missing or untrusted receipts must not contact Ray"),
        )

        assert cmd.poll_ray_job_completions() == 0

        task.refresh_from_db()
        assert task.state == change.get("state", TaskState.RUNNING)
        assert task.result_data is None
        assert task.finished_at is None
        assert "Error consuming" not in cmd.stdout.getvalue()
        for field, expected in change.items():
            assert getattr(task, field) == expected

    @pytest.mark.parametrize(
        "change",
        [
            {"execution_generation": 8},
            {"claimed_by_worker": "replacement"},
            {"completion_data": None},
            {"state": TaskState.CANCELLING},
        ],
    )
    def test_fast_receipt_rechecks_mutation_fences_after_candidate_read(
        self, monkeypatch, change
    ) -> None:
        cmd = _make_command()
        task = RayTaskExecution.objects.create(
            task_id="fast-receipt-late-race",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id=_rq2_ray_job_id(),
            attempt_number=2,
            execution_generation=7,
        )
        task.completion_data = _versioned_completion_json(task)
        task.save(update_fields=["completion_data"])
        cmd.active_tasks = {task.pk: str(task.ray_job_id)}
        cmd.active_task_identities = {task.pk: (2, 7)}
        reconcile = cmd._reconcile_ray_job_task

        def race(snapshot, runner, **options):
            RayTaskExecution.objects.filter(pk=task.pk).update(**change)
            return reconcile(snapshot, runner, **options)

        monkeypatch.setattr(cmd, "_reconcile_ray_job_task", race)

        assert cmd.poll_ray_job_completions() == 0

        task.refresh_from_db()
        assert task.state == change.get("state", TaskState.RUNNING)
        assert task.result_data is None
        assert task.finished_at is None
        assert "Error consuming" not in cmd.stdout.getvalue()

    def test_fast_receipt_losing_lease_after_read_cannot_terminalize(self, monkeypatch) -> None:
        cmd = _make_command()
        task = RayTaskExecution.objects.create(
            task_id="fast-receipt-late-lease-loss",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id=_rq2_ray_job_id(),
            attempt_number=2,
            execution_generation=7,
        )
        task.completion_data = _versioned_completion_json(task)
        task.save(update_fields=["completion_data"])
        cmd.active_tasks = {task.pk: str(task.ray_job_id)}
        cmd.active_task_identities = {task.pk: (2, 7)}
        reconcile = cmd._reconcile_ray_job_task

        def lose_lease(snapshot, runner, **options):
            TaskWorkerLease.objects.filter(worker_id=cmd.worker_id).update(is_active=False)
            return reconcile(snapshot, runner, **options)

        monkeypatch.setattr(cmd, "_reconcile_ray_job_task", lose_lease)

        assert cmd.poll_ray_job_completions() == 0

        task.refresh_from_db()
        assert cmd.shutdown_requested is True
        assert task.state == TaskState.RUNNING
        assert task.result_data is None
        assert task.finished_at is None

    def test_fast_receipt_database_error_keeps_tracking_for_recovery(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.active_tasks = {999: "raysubmit_pending"}
        cmd.active_task_identities = {999: (2, 7)}

        def unavailable(**_filters):
            raise OSError("candidate read unavailable")

        monkeypatch.setattr(RayTaskExecution.objects, "filter", unavailable)

        assert cmd.poll_ray_job_completions() == 0
        assert cmd.active_tasks == {999: "raysubmit_pending"}
        assert cmd.active_task_identities == {999: (2, 7)}
        assert "Error polling Ray Job completions" in cmd.stdout.getvalue()

    @pytest.mark.parametrize("interrupt", ["candidate-error", "shutdown"])
    def test_fast_receipt_batch_contains_candidate_error_and_obeys_shutdown(
        self, monkeypatch, interrupt
    ) -> None:
        cmd = _make_command()
        tasks = []
        for index in range(2):
            task = RayTaskExecution.objects.create(
                task_id=f"fast-receipt-interrupt-{index}",
                callable_path="testproject.tasks.add_numbers",
                state=TaskState.RUNNING,
                claimed_by_worker=cmd.worker_id,
                ray_job_id=_rq2_ray_job_id(str(index)),
                attempt_number=2,
                execution_generation=7,
            )
            task.completion_data = _versioned_completion_json(task)
            task.save(update_fields=["completion_data"])
            tasks.append(task)
        cmd.active_tasks = {task.pk: str(task.ray_job_id) for task in tasks}
        cmd.active_task_identities = {task.pk: (2, 7) for task in tasks}
        reconcile = cmd._reconcile_ray_job_task
        observed: list[int] = []

        def consume(snapshot, runner, **options):
            observed.append(snapshot.pk)
            if len(observed) == 1:
                if interrupt == "candidate-error":
                    raise ValueError("one candidate could not be consumed")
                cmd.shutdown_requested = True
                return
            return reconcile(snapshot, runner, **options)

        monkeypatch.setattr(cmd, "_reconcile_ray_job_task", consume)

        assert cmd.poll_ray_job_completions() == (1 if interrupt == "candidate-error" else 0)
        assert len(observed) == (2 if interrupt == "candidate-error" else 1)
        assert observed[0] in cmd.active_tasks
        if interrupt == "candidate-error":
            assert observed[1] not in cmd.active_tasks
            assert "Error consuming" in cmd.stdout.getvalue()

    @pytest.mark.parametrize("mode", ["idle", "sync", "shutdown", "missing-identity"])
    def test_fast_job_receipt_has_no_idle_or_shutdown_database_cost(
        self, mode, django_assert_num_queries
    ) -> None:
        cmd = _make_command()
        if mode != "idle":
            cmd.active_tasks = {999: "raysubmit_unconfirmed"}
        cmd.sync_mode = mode == "sync"
        cmd.shutdown_requested = mode == "shutdown"

        with django_assert_num_queries(0):
            assert cmd.poll_ray_job_completions() == 0

    def test_fast_job_receipt_fair_batches_bound_queries_and_skip_pending_prefix(
        self, monkeypatch, django_assert_num_queries
    ) -> None:
        cmd = _make_command()
        tasks = RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"fast-batch-{index}",
                    callable_path="testproject.tasks.add_numbers",
                    state=TaskState.RUNNING,
                    claimed_by_worker=cmd.worker_id,
                    ray_job_id=f"raysubmit_batch_{index}",
                    attempt_number=2,
                    execution_generation=7,
                    completion_data=None if index < 32 else "receipt",
                )
                for index in range(66)
            ]
        )
        cmd.active_tasks = {task.pk: str(task.ray_job_id) for task in reversed(tasks)}
        cmd.active_task_identities = {task.pk: (2, 7) for task in tasks}
        # Candidate discovery has one query; authoritative consumption owns its
        # separate lease and task locks and is exercised by the receipt tests.
        observed: list[int] = []

        def consume(task, runner, **options):
            assert runner is None
            assert {"args_json", "kwargs_json", "runtime_env_json", "result_data"}.issubset(
                task.get_deferred_fields()
            )
            assert options["completion_only"] is True
            assert options["orphaned"] is False
            assert options["tracked_identity"] == (2, 7)
            observed.append(task.pk)

        monkeypatch.setattr(cmd, "_reconcile_ray_job_task", consume)
        per_pass: list[int] = []
        for pass_index in range(4):
            if pass_index == 1:
                new_tasks = RayTaskExecution.objects.bulk_create(
                    [
                        RayTaskExecution(
                            task_id=f"fast-new-arrival-{index}",
                            callable_path="testproject.tasks.add_numbers",
                            state=TaskState.RUNNING,
                            claimed_by_worker=cmd.worker_id,
                            ray_job_id=f"raysubmit_new_arrival_{index}",
                            attempt_number=2,
                            execution_generation=7,
                            completion_data="receipt",
                        )
                        for index in range(32)
                    ]
                )
                cmd.active_tasks.update({task.pk: str(task.ray_job_id) for task in new_tasks})
                cmd.active_task_identities.update({task.pk: (2, 7) for task in new_tasks})
            if pass_index == 3:
                RayTaskExecution.objects.filter(pk__in=[task.pk for task in tasks[:32]]).update(
                    completion_data="receipt"
                )
            before = len(observed)
            with django_assert_num_queries(1):
                assert cmd.poll_ray_job_completions() == 0
            per_pass.append(len(observed) - before)

        assert per_pass == [0, 32, 2, 32]
        assert set(observed) == {task.pk for task in tasks}

    def test_reconcile_stale_unknown_orphan_with_malformed_completion_stops_exact_id(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-unknown-orphan-malformed-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker="missing-worker",
            ray_job_id="raysubmit_unknown_orphan_malformed_001",
            completion_data="{not-json",
            args_json="[]",
            kwargs_json="{}",
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
            attempt_number=2,
            execution_generation=7,
        )
        cancelled: list[str] = []
        cmd = _make_command(worker_id="worker-coverage")

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.UNKNOWN,
                    message="status unavailable",
                )

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert cancelled == ["raysubmit_unknown_orphan_malformed_001"]
        assert task.state == TaskState.LOST
        assert task.completion_data == "{not-json"
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert task.pk not in cmd.active_tasks

    def test_reconcile_timed_out_malformed_completion_survives_unavailable_client(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-malformed-client-unavailable-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_malformed_client_unavailable_001",
            completion_data="{not-json",
            args_json="[]",
            kwargs_json="{}",
            started_at=datetime.now(UTC) - timedelta(minutes=10),
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
            timeout_seconds=1,
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command(worker_id="worker-coverage")
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        cmd.active_task_identities = {task.pk: (task.attempt_number, task.execution_generation)}

        def unavailable(_runner, _ray_address=None):
            raise TimeoutError("ray dashboard request timed out")

        monkeypatch.setattr(
            "django_ray.runner.ray_job.RayJobRunner.__init__",
            lambda runner: setattr(runner, "ray_address", "ray://cluster:10001"),
        )
        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner._get_client", unavailable)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.LOST
        assert task.completion_data == "{not-json"
        assert task.cancellation_status == CancellationStatus.INDETERMINATE
        assert task.pk not in cmd.active_tasks

    def test_reconcile_adopts_ray_job_without_previous_owner(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-ownerless-ray-job-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_ownerless_001",
            claimed_by_worker=None,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="adopting-worker", claim_ownerless_tasks=False)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.RUNNING)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.claimed_by_worker == "adopting-worker"
        assert cmd.active_tasks[task.pk] == task.ray_job_id

    def test_reconcile_stopped_state_race_does_not_overwrite_terminal_update(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-stopped-race-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_stopped_race_001",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class RacingJobInfo:
            def __init__(self):
                self.checked = False
                self.message = "stopped"

            @property
            def status(self):
                if not self.checked:
                    self.checked = True
                    RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.FAILED)
                return JobStatus.STOPPED

        class FakeRunner:
            def get_status(self, _handle):
                return RacingJobInfo()

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.pk in cmd.active_tasks

    def test_maintenance_scans_defer_large_payload_columns(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="defer-scan-columns-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            args_json="[1, 2, 3]",
            kwargs_json='{"a": 1}',
            runtime_env_json='{"pip": ["pkg"]}',
            workflow_plan_json='{"nodes": []}',
            progress_data='{"pct": 50}',
            result_data='{"out": 3}',
            error_traceback="tb",
        )
        queryset = Command._defer_maintenance_scan_fields(
            RayTaskExecution.objects.filter(pk=task.pk)
        )
        fetched = queryset.get()
        assert fetched.get_deferred_fields() == set(Command._DEFERRED_MAINTENANCE_SCAN_FIELDS)
        sentinel = SimpleNamespace()
        assert Command._defer_maintenance_scan_fields(sentinel) is sentinel

    @pytest.mark.parametrize("mode", ["local", "cluster"])
    def test_disconnected_ray_core_worker_skips_claiming_and_leaves_queue_for_healthy_peer(
        self,
        monkeypatch,
        mode: str,
    ) -> None:
        overdue = RayTaskExecution.objects.create(
            task_id=f"disconnected-core-overdue-{mode}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
            queue_timeout_seconds=60,
            queue_deadline_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        eligible = RayTaskExecution.objects.create(
            task_id=f"disconnected-core-eligible-{mode}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[3, 4]",
            kwargs_json="{}",
        )

        disconnected = _make_command()
        disconnected.worker_id = f"disconnected-{mode}-worker"
        disconnected.execution_mode = mode
        disconnected.cluster_address = "ray://dead-head:10001" if mode == "cluster" else None
        disconnected._create_lease("default")
        monkeypatch.setattr(
            disconnected,
            "_init_local_ray",
            lambda: (_ for _ in ()).throw(RuntimeError("local ray unavailable")),
        )
        monkeypatch.setattr(
            disconnected,
            "_init_cluster_ray",
            lambda _addr: (_ for _ in ()).throw(RuntimeError("cluster head unreachable")),
        )
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(is_initialized=lambda: False),
        )
        disconnected._initialize_ray_execution()
        assert disconnected._ray_reconnect_backoff_until > 0.0

        # Disconnected worker still sweeps expired rows under its lease fence,
        # but refuses to claim eligible QUEUED work or burn attempt_number.
        assert disconnected.claim_and_process_tasks(["default"], concurrency=2) == 1
        overdue.refresh_from_db()
        eligible.refresh_from_db()
        assert overdue.state == TaskState.EXPIRED
        assert eligible.state == TaskState.QUEUED
        assert eligible.claimed_by_worker is None
        assert eligible.attempt_number == 1
        assert eligible.execution_generation == 0
        assert not TaskAttempt.objects.filter(execution=eligible).exists()

        healthy = _make_command()
        healthy.worker_id = f"healthy-{mode}-worker"
        healthy.execution_mode = mode
        healthy.cluster_address = "ray://healthy-head:10001" if mode == "cluster" else None
        healthy._create_lease("default")
        processed_by_healthy: list[int] = []
        monkeypatch.setattr(healthy, "_is_ray_core_initialized", lambda: True)
        monkeypatch.setattr(
            healthy,
            "process_task",
            lambda current_task: processed_by_healthy.append(int(current_task.pk)),
        )

        assert healthy.claim_and_process_tasks(["default"], concurrency=2) == 1
        eligible.refresh_from_db()
        assert processed_by_healthy == [eligible.pk]
        assert eligible.state == TaskState.RUNNING
        assert eligible.claimed_by_worker == healthy.worker_id
        assert eligible.attempt_number == 1
        assert eligible.execution_generation == 1

    def test_mid_batch_ray_core_disconnect_hands_back_remaining_unsubmitted_tasks(
        self,
        monkeypatch,
    ) -> None:
        tasks = [
            RayTaskExecution.objects.create(
                task_id=f"mid-batch-disconnect-{idx}",
                callable_path="testproject.tasks.add_numbers",
                queue_name="default",
                state=TaskState.QUEUED,
                args_json=f"[{idx}, 1]",
                kwargs_json="{}",
            )
            for idx in range(3)
        ]

        cmd = _make_command()
        cmd.worker_id = "mid-batch-disconnect-worker"
        cmd.execution_mode = "cluster"
        cmd.cluster_address = "ray://flaky-head:10001"
        cmd._create_lease("default")

        state = {"connected": True}
        monkeypatch.setattr(cmd, "_is_ray_core_initialized", lambda: state["connected"])
        dispatched: list[int] = []

        def fail_first_and_disconnect(claimed: RayTaskExecution) -> None:
            dispatched.append(int(claimed.pk))
            state["connected"] = False
            cmd._handle_task_failure(
                claimed,
                error_message="Ray cluster not available",
                exception_type="RayConnectionError",
                expected_claimed_by_worker=cmd.worker_id,
                expected_attempt_number=int(claimed.attempt_number),
                expected_execution_generation=int(claimed.execution_generation),
            )

        monkeypatch.setattr(cmd, "process_task", fail_first_and_disconnect)

        assert cmd.claim_and_process_tasks(["default"], concurrency=3) == 3

        assert dispatched == [tasks[0].pk]
        for remaining in tasks[1:]:
            remaining.refresh_from_db()
            assert remaining.state == TaskState.QUEUED
            assert remaining.claimed_by_worker is None
            assert remaining.started_at is None
            assert remaining.last_heartbeat_at is None
            assert remaining.attempt_number == 1
            assert not TaskAttempt.objects.filter(execution=remaining).exists()

    def test_reconnect_ray_renews_lease_and_clears_stale_handles_when_all_attempts_fail(
        self,
        monkeypatch,
    ) -> None:
        cmd = _make_command()
        cmd.worker_id = "reconnect-lease-renewal-worker"
        cmd.execution_mode = "cluster"
        cmd.cluster_address = "ray://unreachable-head:10001"
        cmd._create_lease("default")

        stale_marks: list[bool] = []
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=1))
        monkeypatch.setattr(
            cmd,
            "_mark_stale_ray_core_tasks_as_lost",
            lambda: stale_marks.append(True),
        )
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(
                is_initialized=lambda: False,
                shutdown=lambda: None,
                cluster_resources=lambda: {"CPU": 1},
            ),
        )
        monkeypatch.setattr(
            cmd,
            "_init_cluster_ray",
            lambda _addr: (_ for _ in ()).throw(RuntimeError("head down")),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep",
            lambda *_: None,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_heartbeat_interval",
            lambda: timedelta(seconds=15),
        )

        renewals: list[bool] = []
        original_update = cmd._update_lease_heartbeat

        def tracked_update() -> bool:
            renewals.append(True)
            return original_update()

        monkeypatch.setattr(cmd, "_update_lease_heartbeat", tracked_update)

        cmd._reconnect_ray()

        # 2s + 5s + 10s + 20s + 40s = 77s total wait across 15s heartbeat cadence
        assert len(renewals) >= 4
        assert stale_marks == [True]
        assert cmd.ray_core_runner is None
        assert cmd._ray_reconnect_backoff_until > 0.0
        assert "pause claiming new Ray Core tasks" in cmd.stdout.getvalue()

        # Active cooldown prevents immediate back-to-back 5-attempt reconnect loops.
        reconnect_invocations: list[bool] = []
        monkeypatch.setattr(cmd, "_reconnect_ray", lambda: reconnect_invocations.append(True))
        cmd._check_ray_connection()
        assert reconnect_invocations == []

    @pytest.mark.parametrize("abort_reason", ["signal", "lease_loss"])
    def test_reconnect_ray_aborts_early_on_shutdown_or_lease_loss(
        self,
        monkeypatch,
        abort_reason: str,
    ) -> None:
        cmd = _make_command()
        cmd.worker_id = f"reconnect-abort-{abort_reason}"
        cmd.execution_mode = "local"
        cmd._create_lease("default")

        attempts: list[int] = []
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(
                is_initialized=lambda: False,
                shutdown=lambda: None,
                cluster_resources=lambda: {"CPU": 1},
            ),
        )

        def fail_and_trigger() -> None:
            attempts.append(1)
            if abort_reason == "signal":
                cmd.shutdown_requested = True
                cmd._shutdown_event.set()
            raise RuntimeError("connect failed")

        monkeypatch.setattr(cmd, "_init_local_ray", fail_and_trigger)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep",
            lambda *_: None,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_heartbeat_interval",
            lambda: timedelta(seconds=3),
        )
        if abort_reason == "lease_loss":
            monkeypatch.setattr(
                cmd,
                "_update_lease_heartbeat",
                lambda: cmd._request_shutdown_for_lease_loss("lost lease") or False,
            )

        cmd._reconnect_ray()

        assert len(attempts) == 1
        assert cmd.shutdown_requested is True
