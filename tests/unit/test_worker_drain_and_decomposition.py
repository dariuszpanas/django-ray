"""Unit tests for worker Strangler-Fig decomposition, SyncRunner, --drain, and heartbeats."""

from __future__ import annotations

import ast
import json
import logging
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.management import CommandError
from django.core.management.base import CommandParser

from django_ray.execution_codec import (
    ExecutionCompletion,
    ExecutionIdentity,
    encode_execution_completion,
)
from django_ray.management.commands import django_ray_worker as worker_module
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.base import BaseRunner, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcomeStatus
from django_ray.runner.ray_core import RayCoreRunner
from django_ray.runner.ray_job import RayJobRunner
from django_ray.runner.sync import SyncRunner, SyncSubmissionHandle
from django_ray.worker import (
    claimer,
    dispatcher,
    loop,
    maintenance,
    reconciler,
    shutdown,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PACKAGE_DIR = REPO_ROOT / "src" / "django_ray" / "worker"
WORKER_COMMAND_PATH = (
    REPO_ROOT / "src" / "django_ray" / "management" / "commands" / "django_ray_worker.py"
)


class _FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _make_command(worker_id: str = "drain-test-worker") -> Command:
    cmd = Command(stdout=StringIO())
    cmd.worker_id = worker_id
    cmd.execution_mode = "sync"
    cmd.sync_mode = True
    cmd.active_tasks = {}
    cmd.active_task_identities = {}
    return cmd


def _imports_in_file(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


class TestWorkerStranglerFigDecomposition:
    """Verify F-073 worker decomposition, acyclic imports, and monkeypatch compatibility."""

    def test_worker_service_modules_exist_and_have_zero_circular_imports(self) -> None:
        expected_modules = {
            "__init__.py",
            "loop.py",
            "claimer.py",
            "dispatcher.py",
            "reconciler.py",
            "maintenance.py",
            "shutdown.py",
        }
        actual_modules = {p.name for p in WORKER_PACKAGE_DIR.glob("*.py")}
        assert expected_modules <= actual_modules

        # Every extracted module exposes its real functions and never imports
        # back to the management command or to sibling worker modules.
        assert callable(loop.run_loop)
        assert callable(claimer.claim_and_process_tasks)
        assert callable(dispatcher.process_task)
        assert callable(reconciler.reconcile_tasks)
        assert callable(maintenance.detect_stuck_tasks)
        assert callable(shutdown.shutdown)

        sibling_names = {
            f"django_ray.worker.{name}"
            for name in ("loop", "claimer", "dispatcher", "reconciler", "maintenance", "shutdown")
        }
        for path in WORKER_PACKAGE_DIR.glob("*.py"):
            imported = _imports_in_file(path)
            assert "django_ray.management.commands.django_ray_worker" not in imported, (
                f"{path.name} must not import django_ray_worker (circular import)"
            )
            assert not (imported & sibling_names), (
                f"{path.name} must not import sibling worker modules: {imported & sibling_names}"
            )

        # runner/base.py remains a pure leaf with no django_ray.runner.* cycles.
        base_imports = _imports_in_file(REPO_ROOT / "src" / "django_ray" / "runner" / "base.py")
        assert not any(name.startswith("django_ray.runner") for name in base_imports)

        # The management command stays a thin Strangler-Fig facade (>80% smaller than 6,265 LOC).
        command_line_count = len(WORKER_COMMAND_PATH.read_text(encoding="utf-8").splitlines())
        assert command_line_count < 1300, (
            f"django_ray_worker.py should stay thin, got {command_line_count} LOC"
        )

    @pytest.mark.django_db
    def test_module_level_monkeypatches_propagate_through_extracted_services(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("monkeypatch-compat-worker")
        cmd._create_lease("default")

        closed_calls: list[bool] = []
        monkeypatch.setattr(
            worker_module, "close_old_connections", lambda: closed_calls.append(True)
        )

        # Verify worker.loop honors worker_module.close_old_connections.
        monkeypatch.setattr(cmd, "send_heartbeat", lambda: setattr(cmd, "shutdown_requested", True))
        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=60.0)
        assert closed_calls == [True]

        # Verify worker.dispatcher honors worker_module.should_retry.
        cmd.shutdown_requested = False
        retry_calls: list[str | None] = []
        task = RayTaskExecution.objects.create(
            task_id="mp-retry-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=1,
        )

        def fake_should_retry(_task: Any, exc_type: str | None = None):
            retry_calls.append(exc_type)
            return SimpleNamespace(should_retry=False, reason="patched", next_attempt_at=None)

        monkeypatch.setattr(worker_module, "should_retry", fake_should_retry)
        handled = cmd._handle_task_failure(
            task,
            error_message="boom",
            exception_type="CustomError",
            expected_claimed_by_worker=cmd.worker_id,
            expected_attempt_number=1,
            expected_execution_generation=1,
        )
        assert handled is True
        assert retry_calls == ["CustomError"]
        task.refresh_from_db()
        assert task.state == TaskState.FAILED


class TestBaseRunnerAndSyncRunnerContract:
    """Verify BaseRunner alignment and SyncRunner implementation."""

    def test_all_three_runners_share_base_runner_contract(self) -> None:
        for runner_cls in (RayJobRunner, RayCoreRunner, SyncRunner):
            assert issubclass(runner_cls, BaseRunner)
            assert callable(getattr(runner_cls, "submit", None))
            assert callable(getattr(runner_cls, "submit_durable", None))
            assert callable(getattr(runner_cls, "get_status", None))
            assert callable(getattr(runner_cls, "cancel", None))
            assert callable(getattr(runner_cls, "get_logs", None))
            assert callable(getattr(runner_cls, "prepare_shutdown", None))

        runner = SyncRunner()
        assert runner.runner_family == "sync"
        assert runner.supports_remote_reconciliation is False

    def test_sync_runner_submit_status_cancel_and_bounded_history(self) -> None:
        seen_calls: list[dict[str, Any]] = []

        def fake_execute(**kwargs: Any) -> str:
            seen_calls.append(kwargs)
            if kwargs["callable_path"].endswith(".fail"):
                return json.dumps({"success": False, "error": "kaboom"})
            return json.dumps({"success": True, "result": 42})

        fake_env = SimpleNamespace(
            profile="default",
            digest="abc123",
            spec={},
            working_dir=None,
            py_modules=(),
            pip=(),
            env_vars={},
        )
        runner = SyncRunner(
            settings_provider=lambda: {"WORKFLOW_PLAN_TRUST_IDENTITY": {}},
            execute_fn=fake_execute,
            runtime_env_resolver=lambda _task: fake_env,
            max_tracked_handles=2,
        )

        ok_task = SimpleNamespace(
            pk=10,
            task_id="sync-task-10",
            callable_path="myapp.tasks.ok",
            args_json="[1, 2]",
            kwargs_json='{"c": 3}',
            attempt_number=1,
            execution_generation=2,
            input_reference=None,
        )
        handle = runner.submit(cast(Any, ok_task), "myapp.tasks.ok", (1, 2), {"c": 3})
        assert isinstance(handle, SyncSubmissionHandle)
        assert isinstance(handle, SubmissionHandle)
        assert handle.ray_job_id == "sync:10:1:2"
        assert handle.ray_address == "sync://local"
        assert handle.status is JobStatus.SUCCEEDED
        assert seen_calls[-1]["ray_job_driver"] is False

        info = runner.get_status(handle)
        assert info.status is JobStatus.SUCCEEDED
        assert info.job_id == handle.ray_job_id

        # Lookup via plain SubmissionHandle uses tracked completion cache.
        plain_handle = SubmissionHandle(
            ray_job_id=handle.ray_job_id,
            ray_address=handle.ray_address,
            submitted_at=handle.submitted_at,
        )
        assert runner.get_status(plain_handle).status is JobStatus.SUCCEEDED

        # Cancel & logs contracts.
        assert runner.cancel(handle) is False
        outcome = runner.cancel_with_status(handle)
        assert outcome.status is CancellationOutcomeStatus.NOT_APPLICABLE
        assert runner.get_logs(handle) is None

        # Failing durable submission + bounded cache eviction.
        fail_task = SimpleNamespace(
            pk=11,
            task_id="sync-task-11",
            callable_path="myapp.tasks.fail",
            args_json="[]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=1,
            input_reference=None,
        )
        fail_handle = runner.submit_durable(cast(Any, fail_task))
        assert fail_handle.status is JobStatus.FAILED
        assert fail_handle.error_message == "kaboom"

        third_task = SimpleNamespace(
            pk=12,
            task_id="sync-task-12",
            callable_path="myapp.tasks.ok",
            args_json="[]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=1,
            input_reference=None,
        )
        runner.submit_durable(cast(Any, third_task))
        # Handle 10 was evicted when max_tracked_handles=2.
        assert runner.get_status(plain_handle).status is JobStatus.UNKNOWN

        runner.prepare_shutdown()
        assert (
            runner.get_status(
                SubmissionHandle(
                    ray_job_id=fail_handle.ray_job_id,
                    ray_address=fail_handle.ray_address,
                    submitted_at=fail_handle.submitted_at,
                )
            ).status
            is JobStatus.UNKNOWN
        )

    @pytest.mark.django_db
    def test_command_execute_task_sync_uses_sync_runner_and_persists_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("sync-runner-worker")
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="sync-persist-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[2, 3]",
            kwargs_json="{}",
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            attempt_number=1,
            execution_generation=1,
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
        )

        envelope = encode_execution_completion(
            ExecutionCompletion(
                identity=ExecutionIdentity(
                    task_execution_pk=int(task.pk),
                    task_id=str(task.task_id),
                    attempt_number=1,
                    execution_generation=1,
                ),
                execution_protocol_version=int(task.execution_protocol_version),
                executor_django_ray_version=worker_module.django_ray_version,
                success=True,
                result=5,
                result_reference=None,
                error=None,
                traceback=None,
                exception_type=None,
                retryable=None,
            )
        )
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda **_kwargs: envelope,
        )

        cmd.execute_task_sync(task)

        assert isinstance(cmd.sync_runner, SyncRunner)
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED, f"error={task.error_message!r}"
        assert json.loads(str(task.result_data)) == 5


class TestWorkerDrainMode:
    """Verify F-088 graceful --drain and --drain-timeout-seconds behavior."""

    def test_add_arguments_registers_drain_flags_with_help_text(self) -> None:
        cmd = _make_command()
        parser = CommandParser(prog="manage.py django_ray_worker", missing_args_message="")
        cmd.add_arguments(parser)

        actions_by_opt = {
            opt: action for action in parser._actions for opt in action.option_strings
        }
        assert "--drain" in actions_by_opt
        assert "--drain-timeout-seconds" in actions_by_opt
        assert actions_by_opt["--drain"].help
        assert actions_by_opt["--drain-timeout-seconds"].help

        parsed = parser.parse_args(["--sync", "--drain", "--drain-timeout-seconds", "45"])
        assert parsed.drain is True
        assert parsed.drain_timeout_seconds == 45.0

    @pytest.mark.parametrize("bad_timeout", [0, -10.0, float("nan"), float("inf"), 100_000.0, True])
    def test_handle_rejects_invalid_drain_timeout_seconds(self, bad_timeout: Any) -> None:
        cmd = _make_command()
        with pytest.raises(CommandError, match="--drain-timeout-seconds"):
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=1,
                sync=True,
                local=False,
                cluster=None,
                ray_job=False,
                drain=True,
                drain_timeout_seconds=bad_timeout,
            )

    @pytest.mark.django_db
    def test_drain_stops_claiming_new_tasks_releases_lease_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unrelated queue backlog must not be claimed and must not block draining "default".
        queued_other = RayTaskExecution.objects.create(
            task_id="drain-queued-other-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="other",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command("drain-clean-worker")
        cmd._called_from_command_line = True
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        with pytest.raises(SystemExit) as exc_info:
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=2,
                sync=True,
                local=False,
                cluster=None,
                ray_job=False,
                drain=True,
                drain_timeout_seconds=10.0,
            )

        assert exc_info.value.code == 0
        assert cmd.shutdown_exit_code == 0
        assert cmd.drain_timed_out is False

        # Queued work was NOT claimed during drain.
        queued_other.refresh_from_db()
        assert queued_other.state == TaskState.QUEUED
        assert queued_other.claimed_by_worker is None

        # Lease was released cleanly.
        assert cmd.lease_identity is not None
        lease = TaskWorkerLease.objects.get(**cmd.lease_identity.database_filters())
        assert lease.is_active is False
        assert lease.stopped_at is not None

        output = cmd.stdout.getvalue()
        assert "Drain complete" in output
        assert "shut down cleanly" in output

    @pytest.mark.django_db
    def test_drain_waits_for_inflight_tasks_and_exits_zero_once_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        monkeypatch.setattr(worker_module, "time", clock)

        cmd = _make_command("drain-inflight-worker")
        cmd.execution_mode = "ray"
        cmd.sync_mode = False
        cmd.drain_mode = True
        cmd.drain_timeout_seconds = 30.0
        cmd._create_lease("default")

        # Simulate one in-flight Ray Job task that finishes on the 2nd completion poll.
        cmd.active_tasks = {99: "raysubmit_drain_99"}
        cmd.active_task_identities = {99: (1, 1)}
        poll_calls = 0
        claim_calls = 0

        def fake_poll_ray_job() -> int:
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls >= 2:
                cmd.active_tasks.clear()
                cmd.active_task_identities.clear()
                return 1
            return 0

        def forbidden_claim(_queues: Any, _concurrency: int) -> int:
            nonlocal claim_calls
            claim_calls += 1
            return 0

        monkeypatch.setattr(cmd, "poll_ray_job_completions", fake_poll_ray_job)
        monkeypatch.setattr(cmd, "claim_and_process_tasks", forbidden_claim)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _q: 0)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda seconds: clock.sleep(seconds))

        cmd.run_loop(queues=["default"], concurrency=2, heartbeat_interval=15.0)

        assert poll_calls == 2
        assert claim_calls == 0
        assert cmd.drain_timed_out is False
        assert cmd.shutdown_exit_code == 0

    @pytest.mark.django_db
    def test_drain_timeout_with_remaining_inflight_tasks_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        monkeypatch.setattr(worker_module, "time", clock)

        cmd = _make_command("drain-timeout-worker")
        cmd._called_from_command_line = True
        cmd.execution_mode = "local"
        cmd.sync_mode = False
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                pending_count=1,
                pending_task_handles=(),
                drain_ready_for_shutdown=lambda: (),
            ),
        )
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)
        monkeypatch.setattr(cmd, "_initialize_ray_execution", lambda: None)
        monkeypatch.setattr(cmd, "poll_ray_core_tasks", lambda: 0)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _q: 0)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(cmd, "_check_ray_connection", lambda: None)
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda seconds: clock.sleep(seconds))

        with pytest.raises(SystemExit) as exc_info:
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=2,
                sync=False,
                local=True,
                cluster=None,
                ray_job=False,
                drain=True,
                drain_timeout_seconds=2.0,
            )

        assert exc_info.value.code == 1
        assert cmd.drain_timed_out is True
        assert cmd.shutdown_exit_code == 1

        # Lease is still released even when drain timed out.
        assert cmd.lease_identity is not None
        lease = TaskWorkerLease.objects.get(**cmd.lease_identity.database_filters())
        assert lease.is_active is False

        output = cmd.stdout.getvalue()
        assert "Drain timed out" in output
        assert "shut down with errors" in output

    @pytest.mark.django_db
    def test_claim_and_process_tasks_returns_zero_when_draining(self) -> None:
        RayTaskExecution.objects.create(
            task_id="drain-direct-claim-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command("drain-direct-worker")
        cmd._create_lease("default")
        cmd.drain_mode = True

        assert cmd.claim_and_process_tasks(["default"], concurrency=4) == 0
        assert RayTaskExecution.objects.filter(state=TaskState.QUEUED).count() == 1


class TestStructuredHeartbeatOutput:
    """Verify F-093 replaces raw stdout '.' dots with structured/throttled status logging."""

    @pytest.mark.django_db
    def test_heartbeats_avoid_dot_spam_and_emit_throttled_structured_status(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cmd = _make_command("heartbeat-format-worker")
        cmd._create_lease("default")
        cmd.tasks_processed_count = 7
        cmd.active_tasks = {1: "raysubmit_hb_1"}

        with caplog.at_level(logging.DEBUG, logger="django_ray.worker"):
            cmd.send_heartbeat()
            cmd.send_heartbeat()
            cmd.send_heartbeat()
            # First three heartbeats emit zero raw '.' stdout dots.
            assert cmd.stdout.getvalue() == ""

            cmd.send_heartbeat()

        stdout = cmd.stdout.getvalue()
        assert "." not in stdout.replace("[Status]", "")
        assert "[Status] tasks_processed=7, active=1" in stdout
        assert any("Worker heartbeat" in record.getMessage() for record in caplog.records)
        assert any("Worker status" in record.getMessage() for record in caplog.records)

    @pytest.mark.django_db
    def test_heartbeat_status_includes_draining_indicator(self) -> None:
        cmd = _make_command("heartbeat-drain-worker")
        cmd._create_lease("default")
        cmd.drain_mode = True
        cmd._heartbeat_count = 3

        cmd.send_heartbeat()

        stdout = cmd.stdout.getvalue()
        assert "[Status]" in stdout
        assert "draining=true" in stdout
