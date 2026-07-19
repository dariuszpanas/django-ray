"""Unit tests for worker command runtime/control-flow helpers."""

from __future__ import annotations

import os
import signal
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.conf import settings as django_settings
from django.core.management import CommandError
from django.core.management.base import CommandParser

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner.polling import AdaptivePollingPolicy


class CapturingStdout:
    """Minimal stdout sink compatible with BaseCommand output usage."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message: str = "", ending: str = "\n") -> None:
        self.messages.append(f"{message}{ending}")

    def flush(self) -> None:
        return


def _make_command(*, worker_id: str = "unit-worker") -> Command:
    cmd = Command()
    cmd.stdout = CapturingStdout()
    cmd.style = cmd.style
    cmd.worker_id = worker_id
    cmd.active_tasks = {}
    cmd.execution_mode = "sync"
    cmd.sync_mode = False
    cmd.local_mode = False
    cmd.cluster_address = None
    cmd.ray_core_runner = None
    return cmd


class FakeClock:
    """Monotonic test clock advanced only by worker sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestWorkerCommandRuntime:
    """Runtime helper and control-flow behavior."""

    def test_add_arguments_registers_expected_flags(self) -> None:
        cmd = _make_command()
        parser = CommandParser(prog="manage.py django_ray_worker", missing_args_message="")

        cmd.add_arguments(parser)
        args = parser.parse_args(["--queue", "high", "--concurrency", "3", "--sync"])

        assert args.queue == "high"
        assert args.concurrency == 3
        assert args.sync is True
        assert args.local is False
        assert args.cluster is None

    def test_parse_queues_supports_all_variants(self, monkeypatch) -> None:
        cmd = _make_command()

        monkeypatch.setattr(
            django_settings,
            "TASKS",
            {"default": {"QUEUES": ["default", "high-priority", "low-priority"]}},
            raising=False,
        )
        assert cmd._parse_queues({"all_queues": True}) == [
            "default",
            "high-priority",
            "low-priority",
        ]
        assert cmd._parse_queues({"queues": ["a", "b"]}) == ["a", "b"]
        assert cmd._parse_queues({"queue": "a, b, ,c"}) == ["a", "b", "c"]
        assert cmd._parse_queues({"queue": "single"}) == ["single"]
        assert cmd._parse_queues({}) == ["default"]

    def test_init_local_ray_clears_env_and_initializes(self, monkeypatch) -> None:
        cmd = _make_command()
        init_calls: list[dict[str, object]] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: False,
            init=lambda **kwargs: init_calls.append(kwargs),
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setenv("RAY_ADDRESS", "ray://old:10001")
        monkeypatch.setenv("RAY_RUNTIME_ENV_HOOK", "uv-hook")

        cmd._init_local_ray()

        assert "RAY_ADDRESS" not in os.environ
        assert "RAY_RUNTIME_ENV_HOOK" not in os.environ
        assert len(init_calls) == 1
        assert init_calls[0]["dashboard_port"] == 8265

    def test_init_cluster_ray_reconnects_and_reports_resources(self, monkeypatch) -> None:
        cmd = _make_command()
        calls: list[str] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: True,
            shutdown=lambda: calls.append("shutdown"),
            init=lambda **kwargs: calls.append(f"init:{kwargs['address']}"),
            cluster_resources=lambda: {"CPU": 4},
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        cmd._init_cluster_ray("ray://cluster:10001")

        assert calls == ["shutdown", "init:ray://cluster:10001"]

    def test_setup_signal_handlers_registers_both_signals(self, monkeypatch) -> None:
        cmd = _make_command()
        seen: list[int] = []

        def fake_signal(sig, _handler):
            seen.append(sig)

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.signal.signal", fake_signal
        )

        cmd.setup_signal_handlers()

        assert signal.SIGTERM in seen
        assert signal.SIGINT in seen

    def test_handle_shutdown_signal_sets_flag(self) -> None:
        cmd = _make_command()
        assert cmd.shutdown_requested is False

        cmd.handle_shutdown_signal(signal.SIGTERM, None)

        assert cmd.shutdown_requested is True
        assert cmd.shutdown_signal == signal.SIGTERM
        assert cmd.shutdown_exit_code == 143

    def test_worker_startup_output_respects_django_verbosity(self) -> None:
        cmd = _make_command()
        cmd.verbosity = 0
        cmd._write_worker_output("hidden")
        assert cmd.stdout.messages == []

        cmd.verbosity = 1
        cmd._write_worker_output("visible")
        assert cmd.stdout.messages == ["visible\n"]

    def test_run_loop_does_not_claim_after_signal_during_poll(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, SimpleNamespace())
        calls: list[str] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: calls.append("heartbeat"))

        def stop_during_poll() -> None:
            calls.append("poll")
            cmd.handle_shutdown_signal(signal.SIGTERM, None)

        monkeypatch.setattr(cmd, "poll_ray_core_tasks", stop_during_poll)
        monkeypatch.setattr(cmd, "claim_and_process_tasks", lambda *_: calls.append("claim"))
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.time", lambda: 100.0
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep", lambda *_: None
        )

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=1)

        assert calls == ["heartbeat", "poll"]

    def test_cli_signal_shutdown_uses_documented_exit_code(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd._called_from_command_line = True
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 1},
        )
        monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
        monkeypatch.setattr(
            cmd,
            "run_loop",
            lambda **_kwargs: cmd.handle_shutdown_signal(signal.SIGINT, None),
        )
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        with pytest.raises(SystemExit) as exc_info:
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=1,
                sync=True,
                local=False,
                cluster=None,
                verbosity=1,
            )

        assert exc_info.value.code == 130

    def test_run_loop_executes_reconciliation_cycle_once(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, SimpleNamespace())
        cmd.reconciliation_interval = 0
        cmd.last_reconciliation = 0

        calls: list[str] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: calls.append("heartbeat"))
        monkeypatch.setattr(cmd, "poll_ray_core_tasks", lambda: calls.append("poll"))

        def fake_claim(_queues, _concurrency):
            calls.append("claim")
            cmd.shutdown_requested = True

        monkeypatch.setattr(cmd, "claim_and_process_tasks", fake_claim)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda: calls.append("reconcile"))
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda: calls.append("stuck"))
        monkeypatch.setattr(cmd, "process_cancellations", lambda: calls.append("cancel"))
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: calls.append("cleanup"))
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.time", lambda: 100.0
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep", lambda *_: None
        )

        cmd.run_loop(queues=["default"], concurrency=2, heartbeat_interval=1)

        assert calls == ["heartbeat", "poll", "claim", "reconcile", "stuck", "cancel", "cleanup"]

    def test_idle_claim_backoff_does_not_delay_cancellation_schedule(self, monkeypatch) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.1,
            max_interval_seconds=0.8,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 10.0
        cmd.timeout_check_interval = 10.0
        cmd.cancellation_interval = 0.15
        cmd.lease_cleanup_interval = 10.0
        claims: list[float] = []
        cancellations: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)

        def claim(_queues, _concurrency):
            claims.append(clock.now)
            if len(claims) == 4:
                cmd.shutdown_requested = True
            return 0

        def cancel() -> int:
            cancellations.append(clock.now)
            return int(len(cancellations) == 2)

        monkeypatch.setattr(cmd, "claim_and_process_tasks", claim)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda: 0)
        monkeypatch.setattr(cmd, "process_cancellations", cancel)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep",
            clock.sleep,
        )

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

        assert cancellations[:3] == pytest.approx([0.0, 0.15, 0.3])
        assert claims == pytest.approx([0.0, 0.1, 0.25, 0.35])

    def test_heartbeat_deadline_interrupts_maximum_idle_claim_delay(self, monkeypatch) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.1,
            max_interval_seconds=1.0,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 10.0
        cmd.timeout_check_interval = 10.0
        cmd.cancellation_interval = 10.0
        cmd.lease_cleanup_interval = 10.0
        heartbeats: list[float] = []
        claims: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: heartbeats.append(clock.now))

        def claim(_queues, _concurrency):
            claims.append(clock.now)
            if clock.now >= 0.7:
                cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "claim_and_process_tasks", claim)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda: 0)
        monkeypatch.setattr(cmd, "process_cancellations", lambda: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep",
            clock.sleep,
        )

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=0.25)

        assert claims == pytest.approx([0.0, 0.1, 0.3, 0.7])
        assert heartbeats == pytest.approx([0.0, 0.25, 0.5])

    def test_pending_ray_core_work_keeps_independent_completion_cadence(self, monkeypatch) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.execution_mode = "local"
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=1))
        cmd.completion_poll_interval = 0.1
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.5,
            max_interval_seconds=1.0,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 10.0
        cmd.timeout_check_interval = 10.0
        cmd.cancellation_interval = 10.0
        cmd.lease_cleanup_interval = 10.0
        polls: list[float] = []
        claims: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)

        def poll() -> int:
            polls.append(clock.now)
            if len(polls) == 4:
                cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "poll_ray_core_tasks", poll)
        monkeypatch.setattr(
            cmd,
            "claim_and_process_tasks",
            lambda _queues, _concurrency: claims.append(clock.now) or 0,
        )
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda: 0)
        monkeypatch.setattr(cmd, "process_cancellations", lambda: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep",
            clock.sleep,
        )

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

        assert polls == pytest.approx([0.0, 0.1, 0.2, 0.3])
        assert claims == [0.0]

    def test_reconciliation_timeout_and_cleanup_have_independent_deadlines(
        self, monkeypatch
    ) -> None:
        cmd = _make_command()
        clock = FakeClock()
        cmd.polling_policy = AdaptivePollingPolicy(
            base_interval_seconds=0.1,
            max_interval_seconds=1.0,
            random_value=lambda: 0.0,
        )
        cmd.reconciliation_interval = 0.2
        cmd.timeout_check_interval = 0.3
        cmd.cancellation_interval = 10.0
        cmd.lease_cleanup_interval = 0.5
        reconciliations: list[float] = []
        timeout_checks: list[float] = []
        cleanups: list[float] = []

        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)

        def claim(_queues, _concurrency):
            if clock.now >= 0.7:
                cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "claim_and_process_tasks", claim)
        monkeypatch.setattr(
            cmd,
            "reconcile_tasks",
            lambda: reconciliations.append(clock.now) or 0,
        )
        monkeypatch.setattr(
            cmd,
            "detect_stuck_tasks",
            lambda: timeout_checks.append(clock.now) or 0,
        )
        monkeypatch.setattr(cmd, "process_cancellations", lambda: 0)
        monkeypatch.setattr(
            cmd,
            "cleanup_expired_leases",
            lambda: cleanups.append(clock.now) or 0,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            clock.monotonic,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep",
            clock.sleep,
        )

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=10.0)

        assert reconciliations == pytest.approx([0.0, 0.2, 0.4, 0.6])
        assert timeout_checks == pytest.approx([0.0, 0.3, 0.6])
        assert cleanups == pytest.approx([0.0, 0.5])

    def test_send_heartbeat_recreates_missing_lease(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.lease = None
        recreated: list[bool] = []
        monkeypatch.setattr(cmd, "_recreate_lease", lambda: recreated.append(True))

        cmd.send_heartbeat()

        assert recreated == [True]
        assert cmd._heartbeat_count == 1

    def test_send_heartbeat_updates_active_lease_and_reports_status(self) -> None:
        cmd = _make_command()
        saved: list[list[str]] = []
        lease = SimpleNamespace(
            is_active=True,
            refresh_from_db=lambda: None,
            save=lambda update_fields: saved.append(update_fields),
        )
        cmd.lease = cast(Any, lease)
        cmd._heartbeat_count = 3
        cmd.tasks_processed_count = 5
        cmd.last_task_processed = 0
        cmd.active_tasks = {1: "active"}
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=2))

        cmd.send_heartbeat()

        assert saved == [["last_heartbeat_at"]]
        assert lease.last_heartbeat_at is not None
        assert any("tasks_processed=5, active=3" in message for message in cmd.stdout.messages)

    def test_send_heartbeat_recreates_inactive_or_deleted_lease(self, monkeypatch) -> None:
        cmd = _make_command()
        recreated: list[str] = []
        monkeypatch.setattr(cmd, "_recreate_lease", lambda: recreated.append("recreated"))

        cmd.lease = cast(
            Any,
            SimpleNamespace(is_active=False, refresh_from_db=lambda: None),
        )
        cmd.send_heartbeat()

        def _deleted() -> None:
            from django_ray.models import TaskWorkerLease

            raise TaskWorkerLease.DoesNotExist

        cmd.lease = cast(
            Any,
            SimpleNamespace(is_active=True, refresh_from_db=_deleted),
        )
        cmd.send_heartbeat()

        assert recreated == ["recreated", "recreated"]

    def test_send_heartbeat_logs_database_errors(self) -> None:
        cmd = _make_command()

        def _broken() -> None:
            raise RuntimeError("database offline")

        cmd.lease = cast(
            Any,
            SimpleNamespace(is_active=True, refresh_from_db=_broken),
        )

        cmd.send_heartbeat()

        assert any(
            "Heartbeat failed: database offline" in message for message in cmd.stdout.messages
        )

    def test_check_ray_connection_reconnects_when_not_initialized(self, monkeypatch) -> None:
        cmd = _make_command()
        reconnect_calls: list[bool] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: False, cluster_resources=lambda: {"CPU": 1}
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(cmd, "_reconnect_ray", lambda: reconnect_calls.append(True))

        cmd._check_ray_connection()

        assert reconnect_calls == [True]

    def test_reconnect_ray_retries_until_failure(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "local"
        attempts: list[int] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: False,
            shutdown=lambda: None,
            cluster_resources=lambda: {"CPU": 1},
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            cmd,
            "_init_local_ray",
            lambda: (_ for _ in ()).throw(RuntimeError("connect failed")),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep", lambda *_: None
        )

        original_write = cmd.stdout.write

        def tracked_write(message: str = "", ending: str = "\n"):
            if "Reconnection attempt" in message:
                attempts.append(1)
            original_write(message, ending=ending)

        cmd.stdout.write = tracked_write  # type: ignore[method-assign]
        cmd._reconnect_ray()

        assert len(attempts) == 5

    def test_handle_local_mode_init_failure_continues_startup(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        monkeypatch.setattr(
            cmd,
            "_init_local_ray",
            lambda: (_ for _ in ()).throw(RuntimeError("local init failed")),
        )
        monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
        monkeypatch.setattr(cmd, "run_loop", lambda **_kwargs: None)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=None,
            sync=False,
            local=True,
            cluster=None,
        )

        assert cmd.execution_mode == "local"

    def test_handle_configures_adaptive_polling_from_settings(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {
                "DEFAULT_CONCURRENCY": 2,
                "RUNNER": "ray_job",
                "WORKER_POLL_INTERVAL_SECONDS": 0.25,
                "WORKER_POLL_MAX_INTERVAL_SECONDS": 2.0,
            },
        )
        monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
        monkeypatch.setattr(cmd, "run_loop", lambda **_kwargs: None)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=None,
            sync=False,
            local=False,
            cluster=None,
            verbosity=1,
        )

        assert cmd.completion_poll_interval == 0.1
        assert cmd.polling_policy.base_interval_seconds == 0.25
        assert cmd.polling_policy.max_interval_seconds == 2.0
        assert any("0.25s base, 2s maximum" in message for message in cmd.stdout.messages)

    def test_handle_cluster_mode_init_failure_continues_startup(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        monkeypatch.setattr(
            cmd,
            "_init_cluster_ray",
            lambda _addr: (_ for _ in ()).throw(RuntimeError("cluster init failed")),
        )
        monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
        monkeypatch.setattr(cmd, "run_loop", lambda **_kwargs: None)
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=None,
            sync=False,
            local=False,
            cluster="ray://cluster:10001",
        )

        assert cmd.execution_mode == "cluster"

    def test_handle_keyboard_interrupt_path(self, monkeypatch) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
        monkeypatch.setattr(
            cmd,
            "run_loop",
            lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        monkeypatch.setattr(cmd, "shutdown", lambda: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)

        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=1,
            sync=True,
            local=False,
            cluster=None,
        )

        assert any("Shutdown requested via keyboard interrupt" in m for m in cmd.stdout.messages)

    @pytest.mark.parametrize("concurrency", [0, -1, True, 1001])
    def test_handle_rejects_invalid_cli_concurrency_before_ray_init(
        self, monkeypatch, concurrency
    ) -> None:
        cmd = _make_command()
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.get_settings",
            lambda: {"DEFAULT_CONCURRENCY": 2},
        )
        init_calls: list[bool] = []
        monkeypatch.setattr(cmd, "_init_local_ray", lambda: init_calls.append(True))

        with pytest.raises(CommandError, match="--concurrency"):
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=concurrency,
                sync=False,
                local=True,
                cluster=None,
            )

        assert init_calls == []


@pytest.mark.django_db
class TestWorkerCommandRuntimeDb:
    """DB-backed helper path tests."""

    def test_mark_stale_ray_core_tasks_routes_rows_through_retry_handling(self) -> None:
        cmd = _make_command(worker_id="stale-worker")
        task = RayTaskExecution.objects.create(
            task_id="stale-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=cmd.worker_id,
        )
        pending = {task.pk: object()}
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                _pending_tasks=pending,
                pending_count=1,
                pending_task_ids=tuple(pending),
                clear_pending_tasks=pending.clear,
            ),
        )

        cmd._mark_stale_ray_core_tasks_as_lost()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.finished_at is None
        assert cmd.ray_core_runner._pending_tasks == {}

    def test_create_lease_creates_and_reactivates_worker(self) -> None:
        cmd = _make_command(worker_id="lease-worker")

        cmd._create_lease("high-priority")
        first_pk = cmd.lease.pk
        cmd._create_lease("low-priority")

        assert cmd.lease.pk == first_pk
        assert cmd.lease.queue_name == "low-priority"
        assert cmd.lease.is_active is True
        assert cmd.lease_queue_name == "low-priority"
        assert any("Lease created" in message for message in cmd.stdout.messages)
        assert any("Lease reactivated" in message for message in cmd.stdout.messages)

    def test_create_and_recreate_lease_handle_database_errors(self, monkeypatch) -> None:
        cmd = _make_command(worker_id="lease-error-worker")
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.TaskWorkerLease.objects.update_or_create",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("database offline")),
        )

        cmd._create_lease("default")
        cmd._recreate_lease()

        assert any("Failed to create lease" in message for message in cmd.stdout.messages)
        assert any("Failed to recreate lease" in message for message in cmd.stdout.messages)

    def test_process_cancellations_clears_tracking_and_finalizes(self, monkeypatch) -> None:
        cmd = _make_command(worker_id="cancel-worker")
        task = RayTaskExecution.objects.create(
            task_id="cancel-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[]",
            kwargs_json='{"seconds": 5}',
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
        )

        cancel_calls: list[str] = []
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                _pending_tasks={task.pk: object()},
                pending_task_ids=(task.pk,),
                cancel=lambda _handle: cancel_calls.append("ray-cancel"),
            ),
        )
        cmd.active_tasks = {task.pk: "raysubmit_1"}

        finalized: list[int] = []

        def fake_finalize(t: RayTaskExecution, **_kwargs: Any) -> bool:
            finalized.append(t.pk)
            t.state = TaskState.CANCELLED
            t.finished_at = datetime.now(UTC)
            t.save(update_fields=["state", "finished_at"])
            return True

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.finalize_cancellation",
            fake_finalize,
        )

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert finalized == [task.pk]
        assert cancel_calls == ["ray-cancel"]
        assert task.pk not in cmd.active_tasks

    def test_process_cancellations_adopts_inactive_owner(self) -> None:
        TaskWorkerLease.objects.create(
            worker_id="dead-cancel-worker",
            hostname="host",
            pid=123,
            is_active=False,
        )
        task = RayTaskExecution.objects.create(
            task_id="cancel-orphan-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker="dead-cancel-worker",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.claimed_by_worker == "recovery-worker"
        assert task.cancellation_status == "NOT_APPLICABLE"

    def test_process_cancellations_adopts_missing_owner(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-orphan-002",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker=None,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.claimed_by_worker == "recovery-worker"

    def test_process_cancellations_adopts_expired_lease_owner(self) -> None:
        TaskWorkerLease.objects.create(
            worker_id="expired-cancel-worker",
            hostname="host",
            pid=123,
            is_active=True,
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
        )
        task = RayTaskExecution.objects.create(
            task_id="cancel-orphan-003",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker="expired-cancel-worker",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.claimed_by_worker == "recovery-worker"

    def test_process_cancellations_skips_active_owner(self) -> None:
        TaskWorkerLease.objects.create(
            worker_id="active-cancel-worker",
            hostname="host",
            pid=123,
            is_active=True,
        )
        task = RayTaskExecution.objects.create(
            task_id="cancel-active-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            claimed_by_worker="active-cancel-worker",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert task.claimed_by_worker == "active-cancel-worker"

    def test_process_cancellations_uses_ray_job_cancellation(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-ray-job-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            ray_job_id="raysubmit_cancel_orphan_001",
            ray_address="ray://cluster:10001",
            args_json="[]",
            kwargs_json="{}",
        )
        calls: list[str] = []

        class FakeRunner:
            def cancel_with_status(self, handle):
                calls.append(handle.ray_job_id)
                from django_ray.runner.cancellation import (
                    CancellationOutcome,
                    CancellationOutcomeStatus,
                )

                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd = _make_command(worker_id="recovery-worker")

        cmd.process_cancellations()

        task.refresh_from_db()
        assert calls == ["raysubmit_cancel_orphan_001"]
        assert task.state == TaskState.CANCELLED
        assert task.cancellation_status == "REQUESTED"

    def test_process_cancellations_records_indeterminate_without_ray_core_runner(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="cancel-ray-core-unavailable-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.CANCELLING,
            ray_job_id="ray_core:123",
            args_json="[]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="recovery-worker")

        cmd.process_cancellations()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
        assert task.cancellation_status == "INDETERMINATE"
        assert (
            task.cancellation_error == "Ray Core runner unavailable while recovering cancellation"
        )

    def test_shutdown_hands_off_active_ray_job(self) -> None:
        cmd = _make_command(worker_id="handoff-worker")
        cmd.execution_mode = "ray"
        task = RayTaskExecution.objects.create(
            task_id="handoff-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 5}',
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_handoff",
            ray_address="ray://cluster:10001",
        )
        cmd.active_tasks = {task.pk: "raysubmit_handoff"}

        cmd._prepare_shutdown_handoff()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker is None
        assert cmd.active_tasks == {}

    def test_shutdown_during_submission_hands_off_claimed_task(self) -> None:
        cmd = _make_command(worker_id="submission-shutdown-worker")
        cmd.execution_mode = "ray"
        task = RayTaskExecution.objects.create(
            task_id="submission-shutdown-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.QUEUED,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        processed: list[int] = []
        cmd.shutdown_requested = True
        cmd.process_task = lambda claimed: processed.append(claimed.pk)  # type: ignore[method-assign]

        cmd.claim_and_process_tasks(["default"], concurrency=1)

        task.refresh_from_db()
        assert processed == []
        assert task.state == TaskState.QUEUED
        assert task.claimed_by_worker is None

    def test_sync_active_task_is_allowed_to_finish_after_signal(self) -> None:
        cmd = _make_command(worker_id="sync-shutdown-worker")
        cmd.execution_mode = "sync"
        task = RayTaskExecution.objects.create(
            task_id="sync-shutdown-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker=cmd.worker_id,
        )
        finished: list[int] = []
        cmd.handle_shutdown_signal(signal.SIGTERM, None)
        cmd.execute_task_sync = lambda active: finished.append(active.pk)  # type: ignore[method-assign]

        cmd.process_task(task)

        assert finished == [task.pk]

    def test_shutdown_cancels_and_persists_active_ray_core(self) -> None:
        cmd = _make_command(worker_id="core-shutdown-worker")
        cmd.execution_mode = "local"
        task = RayTaskExecution.objects.create(
            task_id="core-shutdown-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json='{"seconds": 5}',
            started_at=datetime.now(UTC),
            claimed_by_worker=cmd.worker_id,
        )
        cancel_calls: list[str] = []
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                _pending_tasks={task.pk: object()},
                pending_task_ids=(task.pk,),
                cancel=lambda _handle: cancel_calls.append("cancel") or True,
            ),
        )

        cmd._prepare_shutdown_handoff()

        task.refresh_from_db()
        assert cancel_calls == ["cancel"]
        assert task.state == TaskState.CANCELLING
        assert task.cancellation_status == "REQUESTED"

    def test_cleanup_expired_leases_logs_and_handles_errors(self, monkeypatch) -> None:
        cmd = _make_command(worker_id="cleanup-worker")
        warnings: list[str] = []
        cmd.logger = SimpleNamespace(warning=lambda message: warnings.append(str(message)))

        monkeypatch.setattr("django_ray.runner.leasing.cleanup_expired_leases", lambda: 2)
        cmd.cleanup_expired_leases()
        assert any("Cleaned up 2 expired worker lease(s)" in m for m in cmd.stdout.messages)

        monkeypatch.setattr(
            "django_ray.runner.leasing.cleanup_expired_leases",
            lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
        )
        cmd.cleanup_expired_leases()
        assert any("cleanup failed" in message for message in warnings)

    def test_shutdown_releases_lease_and_handles_ray_disconnect(self, monkeypatch) -> None:
        cmd = _make_command(worker_id="shutdown-worker")
        cmd.lease = cast(Any, SimpleNamespace())
        cmd.execution_mode = "local"

        released: list[str] = []
        monkeypatch.setattr(
            "django_ray.runner.leasing.release_lease",
            lambda worker_id: released.append(worker_id),
        )

        fake_ray = SimpleNamespace(
            is_initialized=lambda: True,
            shutdown=lambda: released.append("ray-shutdown"),
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        cmd.shutdown()

        assert released == ["shutdown-worker", "ray-shutdown"]
