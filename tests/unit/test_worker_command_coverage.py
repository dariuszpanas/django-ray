"""Focused coverage tests for realistic worker command fallback paths."""

from __future__ import annotations

import signal
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.cancellation import CancellationOutcomeStatus


class CapturingStdout:
    """Output sink that accepts Django's optional line-ending argument."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str = "", ending: str = "\n") -> None:
        self.messages.append(f"{message}{ending}")

    def flush(self) -> None:
        return

    def getvalue(self) -> str:
        return "".join(self.messages)


def _make_command(worker_id: str = "worker-coverage") -> Command:
    """Create a command with deterministic output and worker identity."""
    cmd = Command()
    cmd.stdout = CapturingStdout()
    cmd.worker_id = worker_id
    cmd.active_tasks = {}
    cmd.execution_mode = "sync"
    cmd.sync_mode = False
    cmd.ray_core_runner = None
    return cmd


def _worker_options(**overrides: Any) -> dict[str, Any]:
    """Return complete command options for direct ``handle`` calls."""
    return {
        "queue": "default",
        "queues": None,
        "all_queues": False,
        "concurrency": 1,
        "sync": False,
        "local": False,
        "cluster": None,
    } | overrides


class TestWorkerCommandCoverage:
    """Cover command behavior that protects task state during failures."""

    def test_handle_initializes_ray_core_runner_for_cli_local_and_cluster_modes(
        self, monkeypatch
    ) -> None:
        created_runners: list[object] = []

        class FakeRayCoreRunner:
            def __init__(self) -> None:
                created_runners.append(self)

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 1},
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            FakeRayCoreRunner,
        )

        for overrides, initializer_name in (
            ({"local": True}, "_init_local_ray"),
            ({"cluster": "ray://cluster:10001"}, "_init_cluster_ray"),
        ):
            cmd = _make_command()
            initialization_calls: list[object] = []
            monkeypatch.setattr(
                cmd,
                initializer_name,
                lambda *args, calls=initialization_calls: calls.append(args),
            )
            monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
            monkeypatch.setattr(cmd, "run_loop", lambda **_kwargs: None)
            monkeypatch.setattr(cmd, "shutdown", lambda: None)
            monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

            cmd.handle(**_worker_options(**overrides))

            assert initialization_calls
            assert isinstance(cmd.ray_core_runner, FakeRayCoreRunner)

        assert len(created_runners) == 2

    def test_default_ray_core_startup_failures_continue_to_worker_loop(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            lambda: pytest.fail("RayCoreRunner must not start after initialization failure"),
        )

        for settings, initializer_name, error_message, expected_message in (
            (
                {"RUNNER": "ray_core", "RAY_ADDRESS": "auto", "DEFAULT_CONCURRENCY": 1},
                "_init_local_ray",
                "local unavailable",
                "Initial Ray init failed: local unavailable",
            ),
            (
                {
                    "RUNNER": "ray_core",
                    "RAY_ADDRESS": "ray://cluster:10001",
                    "DEFAULT_CONCURRENCY": 1,
                },
                "_init_cluster_ray",
                "cluster unavailable",
                "Initial cluster connection failed: cluster unavailable",
            ),
        ):
            cmd = _make_command()
            monkeypatch.setattr(
                "django_ray.management.commands.django_ray_worker.get_settings",
                lambda settings=settings: settings,
            )
            monkeypatch.setattr(
                cmd,
                initializer_name,
                lambda *_args, message=error_message: (_ for _ in ()).throw(RuntimeError(message)),
            )
            monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
            monkeypatch.setattr(cmd, "run_loop", lambda **_kwargs: None)
            monkeypatch.setattr(cmd, "shutdown", lambda: None)
            monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

            cmd.handle(**_worker_options())

            assert expected_message in cmd.stdout.getvalue()

    def test_default_cluster_mode_creates_ray_core_runner(self, monkeypatch) -> None:
        class FakeRayCoreRunner:
            pass

        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "RUNNER": "ray_core",
                "RAY_ADDRESS": "ray://cluster:10001",
                "DEFAULT_CONCURRENCY": 1,
            },
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            FakeRayCoreRunner,
        )
        initialized_addresses: list[str] = []
        monkeypatch.setattr(cmd, "_init_cluster_ray", initialized_addresses.append)
        monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
        monkeypatch.setattr(cmd, "run_loop", lambda **_kwargs: None)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(**_worker_options())

        assert cmd.execution_mode == "cluster"
        assert initialized_addresses == ["ray://cluster:10001"]
        assert isinstance(cmd.ray_core_runner, FakeRayCoreRunner)

    def test_heartbeat_checks_ray_health_in_local_mode(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        events: list[str] = []
        monkeypatch.setattr(cmd, "_recreate_lease", lambda: events.append("lease"))
        monkeypatch.setattr(cmd, "_check_ray_connection", lambda: events.append("ray"))

        cmd.send_heartbeat()

        assert events == ["lease", "ray"]

    @pytest.mark.django_db
    def test_recreate_lease_reports_creation_and_reactivation(self) -> None:
        cmd = _make_command()

        cmd._recreate_lease()
        TaskWorkerLease.objects.filter(worker_id=cmd.worker_id).update(is_active=False)
        cmd._recreate_lease()

        lease = TaskWorkerLease.objects.get(worker_id=cmd.worker_id)
        assert lease.is_active is True
        assert "Lease created" in cmd.stdout.getvalue()
        assert "Lease reactivated" in cmd.stdout.getvalue()

    def test_ray_cluster_resource_check_returns_none_after_timeout(self, monkeypatch) -> None:
        class HungThread:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                return

            def join(self, timeout: float | None = None) -> None:
                assert timeout == 0.01

            def is_alive(self) -> bool:
                return True

        import threading

        cmd = _make_command()
        monkeypatch.setattr(threading, "Thread", HungThread)
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(cluster_resources=lambda: {"CPU": 2}),
        )

        assert cmd._get_ray_cluster_resources_with_timeout(0.01) is None

    def test_claiming_stops_when_all_concurrency_slots_are_full(self) -> None:
        cmd = _make_command()
        cmd.active_tasks = {1: "raysubmit_active"}

        cmd.claim_and_process_tasks(["default"], concurrency=1)

        assert cmd.active_tasks == {1: "raysubmit_active"}

    def test_completion_envelope_validation_rejects_invalid_shapes(self) -> None:
        assert not Command._is_valid_completion_envelope([])
        assert not Command._is_valid_completion_envelope({"success": "yes", "result": None})
        assert not Command._is_valid_completion_envelope({"success": True})
        assert not Command._is_valid_completion_envelope(
            {"success": False, "result": None, "error": None}
        )
        assert not Command._is_valid_completion_envelope(
            {
                "success": False,
                "result": None,
                "error": "task failed",
                "retryable": "sometimes",
            }
        )
        assert Command._is_valid_completion_envelope(
            {
                "success": False,
                "result": None,
                "error": "task failed",
                "traceback": None,
                "exception_type": "RuntimeError",
            }
        )

    def test_ray_cluster_resource_check_returns_resources_and_reraises_errors(
        self, monkeypatch
    ) -> None:
        cmd = _make_command()
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(cluster_resources=lambda: {"CPU": 2}),
        )

        assert cmd._get_ray_cluster_resources_with_timeout(1) == {"CPU": 2}

        def raise_resource_error() -> dict[str, int]:
            raise RuntimeError("cluster unavailable")

        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(cluster_resources=raise_resource_error),
        )
        with pytest.raises(RuntimeError, match="cluster unavailable"):
            cmd._get_ray_cluster_resources_with_timeout(1)

    def test_second_shutdown_signal_preserves_original_exit_status(self) -> None:
        cmd = _make_command()

        cmd.handle_shutdown_signal(signal.SIGTERM, None)
        cmd.handle_shutdown_signal(signal.SIGINT, None)

        assert cmd.shutdown_requested is True
        assert cmd.shutdown_signal == signal.SIGTERM
        assert cmd.shutdown_exit_code == 128 + signal.SIGTERM

    @pytest.mark.django_db
    def test_handoff_unsubmitted_task_returns_claimed_task_to_queue(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="coverage-handoff-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_handoff",
            ray_address="ray://cluster:10001",
        )
        cmd = _make_command()

        cmd._handoff_unsubmitted_task(task)

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.claimed_by_worker is None
        assert task.started_at is None
        assert task.last_heartbeat_at is None
        assert task.ray_job_id is None
        assert task.ray_address is None
        assert "handed off before remote submission" in cmd.stdout.getvalue()

    @pytest.mark.django_db
    def test_orphan_adoption_does_not_overwrite_a_concurrent_owner(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="coverage-orphan-race-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="old-worker",
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_orphan_race",
        )
        RayTaskExecution.objects.filter(pk=task.pk).update(claimed_by_worker="new-worker")
        cmd = _make_command()

        adopted = cmd._adopt_orphaned_ray_job_task(task, now=datetime.now(UTC))

        assert adopted is False
        task.refresh_from_db()
        assert task.claimed_by_worker == "new-worker"
        assert cmd.active_tasks == {}

    def test_cancellation_reports_remote_and_ray_core_client_failures(self, monkeypatch) -> None:
        cmd = _make_command()
        remote_task = SimpleNamespace(
            pk=1,
            ray_job_id="raysubmit_coverage",
            ray_address="ray://cluster:10001",
            started_at=None,
        )

        class UnavailableRayJobRunner:
            def __init__(self) -> None:
                raise RuntimeError("client unavailable")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", UnavailableRayJobRunner)
        remote_outcome = cmd._request_cancellation_for_task(remote_task)

        class FailingRayCoreRunner:
            pending_task_ids = (2,)

            def cancel(self, _handle: object) -> bool:
                raise RuntimeError("driver disconnected")

        cmd.ray_core_runner = cast(Any, FailingRayCoreRunner())
        core_task = SimpleNamespace(pk=2, ray_job_id="", ray_address="", started_at=None)
        core_outcome = cmd._request_cancellation_for_task(core_task)

        assert remote_outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "Could not cancel Ray Job" in (remote_outcome.message or "")
        assert core_outcome.status == CancellationOutcomeStatus.INDETERMINATE
        assert "Could not cancel Ray Core task" in (core_outcome.message or "")

    @pytest.mark.django_db
    def test_shutdown_handoff_skips_missing_and_non_running_ray_core_tasks(self) -> None:
        queued_task = RayTaskExecution.objects.create(
            task_id="coverage-shutdown-skip-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            claimed_by_worker="worker-coverage",
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cancellation_calls: list[object] = []

        class RayCoreRunner:
            pending_task_ids = (999999, queued_task.pk)

            def cancel(self, handle: object) -> bool:
                cancellation_calls.append(handle)
                return True

        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, RayCoreRunner())

        cmd._prepare_shutdown_handoff()

        assert cancellation_calls == []

    def test_shutdown_logs_lease_and_ray_disconnect_failures(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.lease = cast(Any, object())

        def fail_release_lease(_worker_id: str) -> None:
            raise RuntimeError("database offline")

        def fail_ray_shutdown() -> None:
            raise RuntimeError("driver disconnected")

        monkeypatch.setattr("django_ray.runner.leasing.release_lease", fail_release_lease)
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(is_initialized=lambda: True, shutdown=fail_ray_shutdown),
        )

        cmd.shutdown()

        output = cmd.stdout.getvalue()
        assert "Failed to release lease: database offline" in output
        assert "Failed to close Ray connection: driver disconnected" in output

    @pytest.mark.django_db
    def test_shutdown_signal_after_claim_hands_task_off_before_submission(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="coverage-claim-shutdown-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        cmd.execution_mode = "local"
        processed: list[int] = []
        original_save = RayTaskExecution.save

        def save_then_request_shutdown(
            instance: RayTaskExecution, *args: Any, **kwargs: Any
        ) -> None:
            original_save(instance, *args, **kwargs)
            cmd.shutdown_requested = True

        monkeypatch.setattr(RayTaskExecution, "save", save_then_request_shutdown)
        monkeypatch.setattr(cmd, "process_task", lambda claimed: processed.append(claimed.pk))

        cmd.claim_and_process_tasks(["default"], concurrency=1)

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert processed == []

    def test_sync_execution_stops_when_a_stale_success_cannot_be_persisted(
        self, monkeypatch
    ) -> None:
        task = SimpleNamespace(
            pk=1,
            callable_path="testproject.tasks.add_numbers",
            args_json="[1, 2]",
            kwargs_json="{}",
            result_data=None,
            result_reference=None,
        )
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda **_kwargs: '{"success": true, "result": 3}',
        )
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda current, result: setattr(current, "result_data", str(result)),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.succeed_task",
            lambda *_args, **_kwargs: False,
        )

        cmd.execute_task_sync(task)

        assert task.result_data == "3"

    def test_failure_handler_reports_unhandled_stale_task_update(self, monkeypatch) -> None:
        cmd = _make_command()
        task = SimpleNamespace(pk=1)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.should_retry",
            lambda *_args: SimpleNamespace(should_retry=False, next_attempt_at=None, reason=None),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.record_failure",
            lambda *_args, **_kwargs: False,
        )

        assert cmd._handle_task_failure(task, "stale update") is False

    @pytest.mark.django_db
    def test_polling_stops_after_stale_success_update(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="coverage-poll-stale-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        class RayCoreRunner:
            pending_count = 1
            pending_task_ids = (task.pk,)

            def poll_completed(self) -> list[tuple[int, str]]:
                return [(task.pk, '{"success": true, "result": 3}')]

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, RayCoreRunner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda current, result: setattr(current, "result_data", str(result)),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.succeed_task",
            lambda *_args, **_kwargs: False,
        )

        cmd.poll_ray_core_tasks()

        assert "completed:" not in cmd.stdout.getvalue()

    @pytest.mark.django_db
    def test_reconciliation_skips_healthy_owners_and_logs_orphan_errors(self, monkeypatch) -> None:
        own_task = RayTaskExecution.objects.create(
            task_id="coverage-reconcile-own-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_own",
        )
        active_task = RayTaskExecution.objects.create(
            task_id="coverage-reconcile-active-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="active-worker",
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_active",
        )
        orphan_task = RayTaskExecution.objects.create(
            task_id="coverage-reconcile-error-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="stale-worker",
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_orphan",
        )
        cmd = _make_command()

        class FakeRayJobRunner:
            pass

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRayJobRunner)
        monkeypatch.setattr(
            "django_ray.runner.leasing.get_active_workers",
            lambda: [SimpleNamespace(worker_id="active-worker")],
        )
        monkeypatch.setattr(
            cmd,
            "_reconcile_ray_job_task",
            lambda task, *_args, **_kwargs: (
                (_ for _ in ()).throw(RuntimeError("status unavailable"))
                if task.pk == orphan_task.pk
                else pytest.fail("healthy owners must be skipped")
            ),
        )

        cmd.reconcile_tasks()

        assert own_task.pk != orphan_task.pk
        assert active_task.pk != orphan_task.pk
        assert f"Error reconciling orphaned task {orphan_task.pk}" in cmd.stdout.getvalue()

    @pytest.mark.django_db
    def test_fresh_monitored_task_is_not_marked_stuck(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="coverage-fresh-monitor-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        class RayCoreRunner:
            pending_task_ids = (task.pk,)

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, RayCoreRunner())
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.is_task_timed_out",
            lambda _task: False,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.is_task_stuck",
            lambda _task: False,
        )

        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING

    @pytest.mark.django_db
    def test_orphan_cancellation_claim_does_not_overwrite_a_concurrent_owner(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="coverage-cancellation-race-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            claimed_by_worker="old-worker",
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        RayTaskExecution.objects.filter(pk=task.pk).update(claimed_by_worker="new-worker")
        cmd = _make_command()

        claimed = cmd._claim_orphaned_cancellation(
            task,
            active_worker_ids=set(),
            now=datetime.now(UTC),
        )

        assert claimed is False
        task.refresh_from_db()
        assert task.claimed_by_worker == "new-worker"

    def test_ray_core_cancellation_reports_an_accepted_request(self) -> None:
        class RayCoreRunner:
            pending_task_ids = (1,)

            def cancel(self, _handle: object) -> bool:
                return True

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, RayCoreRunner())
        task = SimpleNamespace(pk=1, ray_job_id="", ray_address="", started_at=None)

        outcome = cmd._request_cancellation_for_task(task)

        assert outcome.status == CancellationOutcomeStatus.REQUESTED

    def test_sync_shutdown_handoff_is_a_noop(self) -> None:
        cmd = _make_command()

        cmd._prepare_shutdown_handoff()

        assert cmd.active_tasks == {}

    @pytest.mark.django_db
    def test_shutdown_handoff_records_ray_core_cancellation_client_failure(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="coverage-shutdown-cancel-error-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="worker-coverage",
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        class FailingRayCoreRunner:
            pending_task_ids = (task.pk,)

            def cancel(self, _handle: object) -> bool:
                raise RuntimeError("driver disconnected")

        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, FailingRayCoreRunner())

        cmd._prepare_shutdown_handoff()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert task.cancellation_status == "INDETERMINATE"
        assert "driver disconnected" in (task.cancellation_error or "")
