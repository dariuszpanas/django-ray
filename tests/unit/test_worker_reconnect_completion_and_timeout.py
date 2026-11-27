"""Focused DB-backed tests for worker accepted completion and timeout recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from django_ray import __version__ as django_ray_version
from django_ray.execution_protocol import (
    SUPPORTED_EXECUTION_PROTOCOL_RANGE,
)
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
)
from django_ray.runner.base import JobInfo, JobStatus
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
)
from tests.unit._worker_reconnect_helpers import (
    RealCommandLeaseTestCase,
    _make_command,
    _strict_ray_job_id,
    _strict_ray_job_metadata,
    _versioned_completion_json,
)


@pytest.mark.django_db
class TestWorkerReconnectCompletionAndTimeout(RealCommandLeaseTestCase):
    """DB-backed tests for accepted completion precedence and timeout recovery."""

    def test_reconcile_accepted_completion_wins_over_legacy_id_strict_marker(
        self,
        monkeypatch,
    ) -> None:
        ray_job_id = "raysubmit_django_ray_v1_strict-marker-conflict"
        task = RayTaskExecution.objects.create(
            task_id="legacy-id-strict-marker-conflict-001",
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
        metadata = _strict_ray_job_metadata(task)
        completion_data = _versioned_completion_json(task, result={"accepted": True})

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(completion_data=completion_data)
                return JobInfo(
                    job_id=ray_job_id,
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

            def cancel_with_status(self, _handle):
                pytest.fail("an accepted durable completion must not be quarantined")

            def get_logs(self, _handle):
                pytest.fail("strict marker conflicts must not retrieve legacy logs")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert json.loads(task.result_data or "null") == {"accepted": True}
        assert task.pk not in cmd.active_tasks

    def test_reconcile_legacy_id_with_strict_marker_cannot_downgrade_without_completion(
        self,
        monkeypatch,
    ) -> None:
        ray_job_id = "raysubmit_django_ray_v1_strict-marker-no-completion"
        task = RayTaskExecution.objects.create(
            task_id="legacy-id-strict-marker-no-completion-001",
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
        metadata = _strict_ray_job_metadata(task)
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

            def get_logs(self, _handle):
                pytest.fail("strict marker conflicts must not retrieve legacy logs")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "strict marker conflicts must not reach result storage"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert cancelled == [ray_job_id]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert "unexpected_strict_metadata" in (task.error_message or "")
        assert task.result_data is None
        assert task.pk not in cmd.active_tasks

    def test_reconcile_strict_running_job_allows_compatible_manager_handoff(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="strict-compatible-handoff-running-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id=_strict_ray_job_id("4"),
            ray_address="ray://cluster:10001",
            claimed_by_worker="dead-worker",
            managed_with_django_ray_version="0.4.0-manager",
            started_at=datetime.now(UTC),
        )
        cmd = _make_command(worker_id="adopting-worker")
        metadata = _strict_ray_job_metadata(task)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.RUNNING,
                    metadata=metadata,
                )

            def get_logs(self, _handle):
                pytest.fail("a compatible running handoff must not retrieve logs")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == "adopting-worker"
        assert task.managed_with_django_ray_version == django_ray_version
        assert task.last_heartbeat_at is not None
        assert cmd.active_tasks[task.pk] == task.ray_job_id

    @pytest.mark.parametrize("completion_kind", ["legacy", "enriched_v1"])
    def test_reconcile_strict_handoff_accepts_released_v1_completion_forms(
        self,
        monkeypatch,
        completion_kind: str,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id=f"strict-compatible-handoff-{completion_kind}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id=_strict_ray_job_id("5"),
            ray_address="ray://cluster:10001",
            claimed_by_worker="dead-worker",
            managed_with_django_ray_version="0.4.0-manager",
            started_at=datetime.now(UTC),
        )
        task.completion_data = (
            json.dumps({"success": True, "result": 3})
            if completion_kind == "legacy"
            else _versioned_completion_json(task, result=3)
        )
        task.save(update_fields=["completion_data"])
        cmd = _make_command(worker_id="adopting-worker")

        class FakeRunner:
            def get_status(self, _handle):
                pytest.fail("an exact durable completion must precede the status RPC")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert json.loads(task.result_data or "null") == 3
        assert task.pk not in cmd.active_tasks

    def test_reconcile_legacy_failed_job_is_lost_without_logs_or_retry(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="legacy-failed-job-policy-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_django_ray_v1_legacy-failed-policy",
            started_at=datetime.now(UTC),
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        log_calls: list[str] = []

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.FAILED,
                    message="legacy Ray job failed",
                    metadata={
                        "django_ray_attempt_number": "1",
                        "django_ray_execution_generation": "0",
                    },
                )

            def get_logs(self, handle):
                log_calls.append(handle.ray_job_id)
                return "legacy traceback"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert log_calls == []
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert "application effects are unknown" in task.error_message
        assert "legacy traceback" not in (task.error_traceback or "")
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_success_with_non_json_logs_waits_for_completion_envelope(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-logs-fallback-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_logs_fallback_001",
            ray_address="ray://cluster:10001",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

            def get_logs(self, _handle):
                return "plain-text-log"

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.result_data is None
        assert task.pk in cmd.active_tasks

    def test_reconcile_tasks_success_with_no_logs_waits_for_completion_envelope(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-no-logs-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_no_logs_001",
            ray_address="ray://cluster:10001",
            result_data="stale",
            result_reference="resultfs://sha256/stale?rel=a/b&bytes=5",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

            def get_logs(self, _handle):
                return ""

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.result_data == "stale"
        assert task.result_reference == "resultfs://sha256/stale?rel=a/b&bytes=5"
        assert task.pk in cmd.active_tasks

    def test_reconcile_tasks_uses_result_reference_from_completion_envelope(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-result-reference-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_result_reference_001",
            error_message="transient failure",
            error_traceback="RuntimeError: transient failure",
            completion_data=json.dumps(
                {
                    "success": True,
                    "result": None,
                    "result_reference": "oversize://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?bytes=256",
                }
            ),
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data is None
        assert (
            task.result_reference
            == "oversize://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?bytes=256"
        )
        assert task.error_message is None
        assert task.error_traceback is None
        assert task.pk not in cmd.active_tasks

    def test_reconcile_canonicalizes_authorized_legacy_result_reference(self, monkeypatch) -> None:
        digest = "a" * 64
        prefix = "tenant alpha/résults+100%"
        key = f"{prefix}/{digest[:2]}/{digest[2:4]}/{digest}.json"
        legacy_reference = f"s3://historical-results/{key}?bytes=256"
        canonical_reference = f"s3://historical-results/{quote(key, safe='/-._~')}?bytes=256"
        task = RayTaskExecution.objects.create(
            task_id="reconcile-legacy-result-reference-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_legacy_result_reference_001",
            completion_data=json.dumps(
                {
                    "success": True,
                    "result": None,
                    "result_reference": legacy_reference,
                }
            ),
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        monkeypatch.setattr(
            "django_ray.result_storage.get_settings",
            lambda: {
                "RESULT_STORAGE_BACKEND": "gcs",
                "RESULT_STORAGE_S3_BUCKET": "historical-results",
                "RESULT_STORAGE_S3_PREFIX": prefix,
            },
        )

        class FakeRunner:
            def get_status(self, _handle):
                pytest.fail("the durable completion envelope should be consumed before Ray RPC")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.result_storage.S3ResultStorage.__init__",
            lambda *_args, **_kwargs: pytest.fail(
                "completion validation must not initialize an SDK"
            ),
        )
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data is None
        assert task.result_reference == canonical_reference
        assert task.pk not in cmd.active_tasks

    def test_reconcile_consumes_completion_while_ray_still_reports_running(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-running-completion-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_running_completion_001",
            completion_data='{"success": true, "result": 3}',
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.RUNNING,
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_refreshes_envelope_after_terminal_status_rpc(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-envelope-race-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_envelope_race_001",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    completion_data=json.dumps({"success": True, "result": 3})
                )
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda current_task, result: setattr(current_task, "result_data", str(result)),
        )
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        assert task.pk not in cmd.active_tasks

    def test_reconcile_pending_status_retires_task_terminalized_during_rpc(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-terminal-during-pending-status-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_terminal_during_status_001",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    state=TaskState.FAILED,
                    error_message="timeout won",
                )
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.PENDING,
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.error_message == "timeout won"
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_uses_valid_envelope_when_ray_reports_failed(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-envelope-authoritative-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_envelope_authoritative_001",
            completion_data=json.dumps({"success": True, "result": 3}),
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(
                    job_id=task.ray_job_id or "",
                    status=JobStatus.FAILED,
                    message="driver exited after writing completion envelope",
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda current_task, result: setattr(current_task, "result_data", str(result)),
        )
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_ignores_status_for_replaced_ray_job(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-replaced-ray-job-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_old_001",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: "raysubmit_old_001"}
        failures: list[dict[str, object]] = []
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda _task, **kwargs: failures.append(kwargs),
        )

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    ray_job_id="raysubmit_new_001",
                    execution_generation=2,
                )
                return JobInfo(
                    job_id="raysubmit_old_001",
                    status=JobStatus.FAILED,
                    message="old Ray job failed",
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert failures == []
        assert task.pk not in cmd.active_tasks

    @pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.SUCCEEDED])
    def test_reconcile_tasks_ignores_replaced_attempt_with_same_ray_job_identity(
        self, monkeypatch, status: JobStatus
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id=f"reconcile-replaced-attempt-{status.value.lower()}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_old_001",
            attempt_number=1,
            execution_generation=7,
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: "raysubmit_old_001"}

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    attempt_number=2,
                    claimed_by_worker="replacement-worker",
                )
                return JobInfo(
                    job_id="raysubmit_old_001",
                    status=status,
                    message="old Ray job callback",
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "a stale Ray Job result must not reach result storage"
            ),
        )
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda *_args, **_kwargs: pytest.fail(
                "a stale Ray Job result must not reach failure handling"
            ),
        )

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.claimed_by_worker == "replacement-worker"
        assert task.last_heartbeat_at is None
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_rejects_preexisting_replacement_with_same_ray_job_id(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-preexisting-replacement-same-job-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_reused_001",
            attempt_number=2,
            execution_generation=7,
            claimed_by_worker="replacement-worker",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: "raysubmit_reused_001"}
        cmd.active_task_identities = {task.pk: (1, 7)}

        class FakeRunner:
            def get_status(self, _handle):
                pytest.fail("stale active tracking must be rejected before the status RPC")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.claimed_by_worker == "replacement-worker"
        assert cmd.active_tasks == {}
        assert cmd.active_task_identities == {}

    def test_claim_clears_previous_ray_job_identity_before_submission(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="claim-clears-ray-job-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_previous_001",
            ray_job_request_reference="inputfs://sha256/previous-request?bytes=128",
            ray_address="ray://previous:10001",
            ray_target_address="ray://target:10001",
            execution_generation=4,
        )
        cmd = _make_command()
        cmd.execution_mode = "ray"
        claimed: list[RayTaskExecution] = []
        monkeypatch.setattr(cmd, "process_task", lambda current_task: claimed.append(current_task))

        cmd._create_lease("default")
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert claimed and claimed[0].pk == task.pk
        assert task.execution_generation == 5
        assert task.ray_job_id is None
        assert task.ray_job_request_reference is None
        assert task.ray_address is None
        assert task.ray_target_address == "ray://target:10001"

    def test_claim_stamps_manager_without_backfilling_legacy_creator(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="claim-stamps-manager-legacy-001",
            callable_path="testproject.tasks.add_numbers",
            metadata_schema_version=0,
            execution_protocol_version=1,
            created_with_django_ray_version=None,
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.execution_mode = "ray"
        monkeypatch.setattr(cmd, "process_task", lambda _task: None)

        assert cmd.claim_and_process_tasks(queues=["default"], concurrency=1) == 1

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == cmd.worker_id
        assert task.metadata_schema_version == 0
        assert task.execution_protocol_version == 1
        assert task.created_with_django_ray_version is None
        assert task.managed_with_django_ray_version == django_ray_version
        assert task.executor_django_ray_version is None

    def test_claim_promotes_legacy_address_before_clearing_handle(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="claim-promotes-legacy-routing-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_address="ray://legacy:10001",
        )
        cmd = _make_command()
        cmd.execution_mode = "ray"
        monkeypatch.setattr(cmd, "process_task", lambda _task: None)

        cmd._create_lease("default")
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.ray_target_address == "ray://legacy:10001"
        assert task.ray_address is None

    def test_claim_keeps_ambiguous_legacy_auto_on_global_fallback(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="claim-keeps-legacy-auto-fallback-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_address="auto",
        )
        cmd = _make_command()
        cmd.execution_mode = "ray"
        monkeypatch.setattr(cmd, "process_task", lambda _task: None)

        cmd._create_lease("default")
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.ray_target_address is None
        assert task.ray_address is None

    def test_claim_does_not_promote_automatic_ray_core_retry_handle(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="claim-keeps-ray-core-routing-metadata-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="ray_core:19",
            ray_address="ray://core-cluster:10001",
        )
        cmd = _make_command()
        cmd.execution_mode = "ray"
        monkeypatch.setattr(cmd, "process_task", lambda _task: None)

        cmd._create_lease("default")
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.ray_target_address is None
        assert task.ray_job_id is None
        assert task.ray_address is None

    def test_claim_clears_stale_error_and_cancellation_fields_from_prior_attempt(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="claim-clears-stale-attempt-diagnostics-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            attempt_number=2,
            args_json="[1, 2]",
            kwargs_json="{}",
            error_message="attempt 1 failed",
            error_traceback="Traceback (most recent call last): ...",
            cancellation_status="INDETERMINATE",
            cancellation_error="attempt 1 stop timed out",
        )
        cmd = _make_command()
        cmd.execution_mode = "ray"
        monkeypatch.setattr(cmd, "process_task", lambda _task: None)

        cmd._create_lease("default")
        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.error_message is None
        assert task.error_traceback is None
        assert task.cancellation_status is None
        assert task.cancellation_error is None

    def test_reconcile_tasks_missing_completion_becomes_lost(self, monkeypatch) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="reconcile-missing-completion-stale-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_missing_completion_stale_001",
            started_at=stale_time,
            last_heartbeat_at=stale_time,
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.pk not in cmd.active_tasks

    def test_reconcile_owner_transfer_during_status_rpc_blocks_terminal_effects(
        self,
        monkeypatch,
    ) -> None:
        cmd = _make_command()
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="reconcile-owner-transfer-during-status-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="worker-coverage",
            managed_with_django_ray_version="0.4.0-manager",
            ray_job_id="raysubmit_owner_transfer_during_status_001",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: str(task.ray_job_id)}
        cmd.active_task_identities = {task.pk: (2, 7)}
        completion_data = json.dumps({"success": True, "result": 3})

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    claimed_by_worker="replacement-worker",
                    managed_with_django_ray_version="0.5.0-replacement",
                    completion_data=completion_data,
                )
                return JobInfo(
                    job_id=str(task.ray_job_id),
                    status=JobStatus.STOPPED,
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "the previous owner must not store the adopter's result"
            ),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.cancel_task",
            lambda *_args, **_kwargs: pytest.fail(
                "the previous owner must not finalize the adopter's task"
            ),
        )

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == "replacement-worker"
        assert task.managed_with_django_ray_version == "0.5.0-replacement"
        assert task.completion_data == completion_data
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert task.pk not in cmd.active_tasks

    def test_reconcile_stopped_status_after_owner_transfer_does_not_cancel_task(
        self,
        monkeypatch,
    ) -> None:
        cmd = _make_command()
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="reconcile-stopped-owner-transfer-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="worker-coverage",
            ray_job_id="raysubmit_stopped_owner_transfer_001",
            attempt_number=2,
            execution_generation=7,
        )
        cmd.active_tasks = {task.pk: str(task.ray_job_id)}
        cmd.active_task_identities = {task.pk: (2, 7)}

        class FakeRunner:
            def get_status(self, _handle):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    claimed_by_worker="replacement-worker"
                )
                return JobInfo(
                    job_id=str(task.ray_job_id),
                    status=JobStatus.STOPPED,
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.cancel_task",
            lambda *_args, **_kwargs: pytest.fail(
                "the previous owner must not finalize a stopped replacement task"
            ),
        )

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == "replacement-worker"
        assert task.completion_data is None
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities

    def test_reconcile_tasks_malformed_completion_envelope_remains_active(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-malformed-completion-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_malformed_completion_001",
            completion_data="{not-json",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks

    def test_reconcile_tasks_expired_malformed_envelope_becomes_lost(self, monkeypatch) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="reconcile-malformed-completion-expired-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_malformed_completion_expired_001",
            started_at=stale_time,
            last_heartbeat_at=stale_time,
            completion_data="{not-json",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert "application effects are unknown" in (task.error_message or "")
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_expired_running_malformed_envelope_stops_without_retry(
        self, monkeypatch
    ) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="reconcile-running-malformed-completion-expired-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_running_malformed_completion_expired_001",
            started_at=stale_time,
            last_heartbeat_at=stale_time,
            completion_data="{not-json",
        )
        cancelled: list[str] = []
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.RUNNING)

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert cancelled == [task.ray_job_id]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert "while still RUNNING" in (task.error_message or "")
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_incomplete_completion_envelope_remains_active(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-incomplete-completion-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_incomplete_completion_001",
            completion_data='{"success": true}',
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks

    def test_reconcile_tasks_expired_success_without_envelope_becomes_lost(
        self, monkeypatch
    ) -> None:
        stale_time = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="reconcile-no-completion-expired-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_no_completion_expired_001",
            started_at=stale_time,
            last_heartbeat_at=stale_time,
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.LOST
        assert task.attempt_number == 1
        assert "application effects are unknown" in (task.error_message or "")
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_failure_envelope_uses_retry_policy(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-failure-envelope-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_failure_envelope_001",
            completion_data=json.dumps(
                {
                    "success": False,
                    "result": None,
                    "error": "task exploded",
                    "traceback": "trace",
                    "exception_type": "ValueError",
                }
            ),
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        failures: list[dict[str, object]] = []
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda _task, **kwargs: failures.append(kwargs),
        )

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=task.ray_job_id or "", status=JobStatus.SUCCEEDED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        assert failures == [
            {
                "error_message": "task exploded",
                "error_traceback": "trace",
                "exception_type": "ValueError",
                "retryable": None,
                "expected_ray_job_id": "raysubmit_failure_envelope_001",
                "expected_claimed_by_worker": "worker-coverage",
                "expected_attempt_number": 1,
                "expected_execution_generation": 0,
                "expected_completion_data": task.completion_data,
                "require_completion_data_match": True,
                "supported_protocols": SUPPORTED_EXECUTION_PROTOCOL_RANGE,
                "executor_django_ray_version": None,
            }
        ]
        assert task.pk not in cmd.active_tasks

    def test_reconcile_tasks_consumes_versioned_completion_before_status_rpc(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-versioned-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_versioned_success_001",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        task.refresh_from_db()
        task.completion_data = _versioned_completion_json(task, result={"value": 3})
        task.save(update_fields=["completion_data"])
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        cmd.active_task_identities = {
            task.pk: (int(task.attempt_number), int(task.execution_generation))
        }

        class FakeRunner:
            def get_status(self, _handle):
                pytest.fail("a valid durable completion must precede the status RPC")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert json.loads(task.result_data or "null") == {"value": 3}
        assert task.executor_django_ray_version == "0.5.0-executor"
        archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
        assert archived.executor_django_ray_version == "0.5.0-executor"
        assert task.pk not in cmd.active_tasks

    @pytest.mark.parametrize(
        ("remote_status", "expected_state", "expected_cancel_count"),
        [
            (JobStatus.PENDING, TaskState.LOST, 1),
            (JobStatus.SUCCEEDED, TaskState.FAILED, 0),
        ],
    )
    def test_reconcile_tasks_quarantines_versioned_identity_mismatch_without_retry(
        self,
        monkeypatch,
        remote_status: JobStatus,
        expected_state: str,
        expected_cancel_count: int,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id=f"reconcile-versioned-mismatch-{remote_status.value.lower()}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            ray_job_id=f"raysubmit_versioned_mismatch_{remote_status.value.lower()}_001",
            attempt_number=2,
            execution_generation=7,
            last_heartbeat_at=datetime.now(UTC),
        )
        cmd = _make_command()
        task.refresh_from_db()
        task.completion_data = _versioned_completion_json(
            task,
            task_id="another-task-identity",
            result={"must_not_store": "secret"},
        )
        task.save(update_fields=["completion_data"])
        cmd.active_tasks = {task.pk: task.ray_job_id or ""}
        cmd.active_task_identities = {
            task.pk: (int(task.attempt_number), int(task.execution_generation))
        }
        cancellations: list[str] = []

        class FakeRunner:
            def get_status(self, handle):
                return JobInfo(job_id=handle.ray_job_id, status=remote_status)

            def cancel_with_status(self, handle):
                cancellations.append(handle.ray_job_id)
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "an incompatible completion must not reach result storage"
            ),
        )

        assert cmd.reconcile_tasks() == 1

        task.refresh_from_db()
        assert task.state == expected_state
        assert task.attempt_number == 2
        assert task.result_data is None
        assert task.result_reference is None
        assert task.executor_django_ray_version is None
        assert task.error_message == ("Execution completion rejected (identity_mismatch)")
        assert len(cancellations) == expected_cancel_count
        archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
        assert archived.state == expected_state
        assert archived.executor_django_ray_version is None
        assert "must_not_store" not in (task.error_message or "")

    def test_reconcile_tasks_handles_missing_task_and_runner_exception(self, monkeypatch) -> None:
        existing = RayTaskExecution.objects.create(
            task_id="reconcile-exception-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_exception_001",
            ray_address="ray://cluster:10001",
        )
        cmd = _make_command()
        cmd.active_tasks = {
            999999: "raysubmit_missing_001",
            existing.pk: existing.ray_job_id or "",
        }

        class FakeRunner:
            def get_status(self, _handle):
                raise RuntimeError("runner status exploded")

            def get_logs(self, _handle):  # pragma: no cover - not reached
                return ""

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.reconcile_tasks()

        assert 999999 not in cmd.active_tasks
        assert existing.pk in cmd.active_tasks
        assert "Error reconciling task" in cmd.stdout.getvalue()

    def test_reconcile_tasks_adopts_orphaned_running_ray_job(self, monkeypatch) -> None:
        orphan = RayTaskExecution.objects.create(
            task_id="reconcile-orphan-running-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_orphan_running_001",
            ray_address="ray://cluster:10001",
            claimed_by_worker="dead-worker",
            managed_with_django_ray_version="0.4.0-manager",
            started_at=datetime.now(UTC),
        )
        cmd = _make_command(worker_id="adopting-worker")

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=orphan.ray_job_id or "", status=JobStatus.RUNNING)

            def get_logs(self, _handle):  # pragma: no cover - not reached
                return ""

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        activity_count = cmd.reconcile_tasks(queues=("default",))

        orphan.refresh_from_db()
        assert orphan.claimed_by_worker == "adopting-worker"
        assert orphan.managed_with_django_ray_version == django_ray_version
        assert orphan.last_heartbeat_at is not None
        assert cmd.active_tasks[orphan.pk] == "raysubmit_orphan_running_001"
        assert activity_count == 1

    @pytest.mark.parametrize("failure_stage", ["adoption", "status"])
    def test_reconcile_tasks_counts_new_tracking_retained_after_error(
        self, monkeypatch, failure_stage
    ) -> None:
        orphan = RayTaskExecution.objects.create(
            task_id="reconcile-retained-error",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_retained_error",
            claimed_by_worker="dead-worker",
            started_at=datetime.now(UTC),
        )
        cmd = _make_command(worker_id="adopting-worker")

        class FakeRunner:
            def get_status(self, _handle):
                raise RuntimeError("status unavailable")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        if failure_stage == "adoption":
            adopt = cmd._adopt_orphaned_ray_job_task

            def interrupted_adoption(task, *, now):
                assert adopt(task, now=now)
                raise RuntimeError("adoption interrupted after tracking")

            monkeypatch.setattr(cmd, "_adopt_orphaned_ray_job_task", interrupted_adoption)

        assert cmd.reconcile_tasks() == 1
        assert cmd.active_tasks == {orphan.pk: orphan.ray_job_id}
        orphan.refresh_from_db()
        assert orphan.state == TaskState.RUNNING
        assert orphan.claimed_by_worker == cmd.worker_id
        assert not TaskAttempt.objects.filter(execution=orphan).exists()

    def test_reconcile_tasks_excludes_original_tracking_retired_then_readopted(
        self, monkeypatch
    ) -> None:
        orphan = RayTaskExecution.objects.create(
            task_id="reconcile-retired-readopted",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_retired_readopted",
            claimed_by_worker="dead-worker",
            started_at=datetime.now(UTC),
        )
        cmd = _make_command(worker_id="adopting-worker")
        cmd.active_tasks = {orphan.pk: str(orphan.ray_job_id)}
        cmd.active_task_identities = {orphan.pk: (1, 0)}
        query_type = type(RayTaskExecution.objects.all())
        original_get = query_type.get
        first_read = True

        def unavailable_active_snapshot(query, *args, **kwargs):
            nonlocal first_read
            # Model a row becoming visible again between the active point read
            # and the orphan page. The first read retires its old local handle.
            if first_read and kwargs == {"pk": orphan.pk}:
                first_read = False
                raise RayTaskExecution.DoesNotExist
            return original_get(query, *args, **kwargs)

        monkeypatch.setattr(query_type, "get", unavailable_active_snapshot)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=str(orphan.ray_job_id), status=JobStatus.RUNNING)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        # Retirement counts once; re-adopting an entry key adds no activity.
        assert cmd.reconcile_tasks() == 1
        assert not first_read
        assert cmd.active_tasks == {orphan.pk: orphan.ray_job_id}
        orphan.refresh_from_db()
        assert orphan.claimed_by_worker == cmd.worker_id

    def test_reconcile_tasks_excludes_original_tracking_outside_active_page(
        self, monkeypatch
    ) -> None:
        cmd = _make_command(worker_id="adopting-worker")
        tasks = RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"reconcile-original-{index}",
                    callable_path="testproject.tasks.add_numbers",
                    state=TaskState.RUNNING,
                    ray_job_id=f"raysubmit_original_{index}",
                    claimed_by_worker=cmd.worker_id if index < 32 else "dead-worker",
                    started_at=datetime.now(UTC),
                )
                for index in range(33)
            ]
        )
        cmd.active_tasks = {task.pk: str(task.ray_job_id) for task in tasks}
        cmd.active_task_identities = {task.pk: (1, 0) for task in tasks}
        statuses = []

        class FakeRunner:
            def get_status(self, handle):
                statuses.append(handle.ray_job_id)
                return JobInfo(job_id=handle.ray_job_id, status=JobStatus.RUNNING)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == 0
        assert statuses == [task.ray_job_id for task in tasks]
        tasks[-1].refresh_from_db()
        assert tasks[-1].claimed_by_worker == cmd.worker_id
        assert cmd.reconcile_tasks() == 0
        assert statuses == [task.ray_job_id for task in tasks] + [tasks[-1].ray_job_id]

    @pytest.mark.parametrize("initially_tracked", [False, True])
    def test_reconcile_tasks_preserves_activity_and_cursor_after_partial_shutdown(
        self, monkeypatch, initially_tracked
    ) -> None:
        cmd = _make_command(worker_id="adopting-worker")
        tasks = RayTaskExecution.objects.bulk_create(
            [
                RayTaskExecution(
                    task_id=f"reconcile-shutdown-{index}",
                    callable_path="testproject.tasks.add_numbers",
                    state=TaskState.RUNNING,
                    ray_job_id=f"raysubmit_shutdown_{index}",
                    claimed_by_worker=cmd.worker_id if initially_tracked else "dead-worker",
                    started_at=datetime.now(UTC),
                )
                for index in range(2)
            ]
        )
        if initially_tracked:
            cmd.active_tasks = {task.pk: str(task.ray_job_id) for task in tasks}
            cmd.active_task_identities = {task.pk: (1, 0) for task in tasks}
        statuses = []

        class FakeRunner:
            def get_status(self, handle):
                statuses.append(handle.ray_job_id)
                if len(statuses) == 1:
                    cmd.shutdown_requested = True
                return JobInfo(job_id=handle.ray_job_id, status=JobStatus.RUNNING)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks() == (0 if initially_tracked else 1)
        assert statuses == [tasks[0].ray_job_id]
        assert len(cmd.active_tasks) == (2 if initially_tracked else 1)
        cmd.shutdown_requested = False
        assert cmd.reconcile_tasks() == (0 if initially_tracked else 1)
        expected = [task.ray_job_id for task in tasks]
        if not initially_tracked:
            expected.insert(1, tasks[0].ray_job_id)
        assert statuses == expected
        assert cmd.active_tasks == {task.pk: task.ray_job_id for task in tasks}

    def test_reconcile_tasks_does_not_adopt_orphan_from_another_queue(
        self,
        monkeypatch,
    ) -> None:
        started_at = datetime.now(UTC)
        orphan = RayTaskExecution.objects.create(
            task_id="reconcile-orphan-other-queue-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="ray-data",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_orphan_other_queue_001",
            ray_address="ray://cluster:10001",
            claimed_by_worker="dead-ray-job-worker",
            managed_with_django_ray_version="0.4.0-manager",
            started_at=started_at,
            last_heartbeat_at=started_at,
        )
        cmd = _make_command(worker_id="ml-worker")

        class FakeRunner:
            def get_status(self, _handle):
                pytest.fail("an out-of-queue orphan must not reach Ray Job status")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        assert cmd.reconcile_tasks(queues=("ml",)) == 0

        orphan.refresh_from_db()
        assert orphan.state == TaskState.RUNNING
        assert orphan.claimed_by_worker == "dead-ray-job-worker"
        assert orphan.managed_with_django_ray_version == "0.4.0-manager"
        assert orphan.last_heartbeat_at == started_at
        assert orphan.pk not in cmd.active_tasks
        assert not TaskAttempt.objects.filter(execution=orphan).exists()

    def test_reconcile_tasks_without_candidate_does_not_require_request_storage(
        self,
        monkeypatch,
    ) -> None:
        cmd = _make_command(worker_id="ray-core-worker")

        class UnexpectedRunner:
            def __init__(self) -> None:
                pytest.fail("a Ray-Core-only queue must not initialize the Ray Job runner")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", UnexpectedRunner)

        assert cmd.reconcile_tasks(queues=("default",)) == 0

    def test_timeout_recovery_does_not_adopt_task_from_another_queue(
        self,
        monkeypatch,
    ) -> None:
        started_at = datetime.now(UTC) - timedelta(minutes=10)
        task = RayTaskExecution.objects.create(
            task_id="timeout-other-queue-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="ray-data",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
            timeout_seconds=1,
            started_at=started_at,
            last_heartbeat_at=started_at,
            claimed_by_worker="dead-ray-job-worker",
            ray_job_id="raysubmit_timeout_other_queue_001",
            ray_address="ray://cluster:10001",
        )
        cmd = _make_command(worker_id="ml-worker")
        monkeypatch.setattr(
            cmd,
            "_request_timeout_cancellation",
            lambda _task: pytest.fail("out-of-queue timeout must not request cancellation"),
        )

        assert cmd.detect_stuck_tasks(queues=("ml",)) == 0

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == "dead-ray-job-worker"
        assert task.started_at == started_at
        assert task.last_heartbeat_at == started_at
        assert not TaskAttempt.objects.filter(execution=task).exists()
