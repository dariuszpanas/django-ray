"""Unit tests for worker command runtime/control-flow helpers."""

from __future__ import annotations

import os
import signal
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.conf import settings as django_settings
from django.core.management.base import CommandParser

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState


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

    def test_send_heartbeat_recreates_missing_lease(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.lease = None
        recreated: list[bool] = []
        monkeypatch.setattr(cmd, "_recreate_lease", lambda: recreated.append(True))

        cmd.send_heartbeat()

        assert recreated == [True]
        assert cmd._heartbeat_count == 1

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
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(_pending_tasks={task.pk: object()}, pending_count=1),
        )

        cmd._mark_stale_ray_core_tasks_as_lost()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None
        assert task.finished_at is None
        assert cmd.ray_core_runner._pending_tasks == {}

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
                cancel=lambda _handle: cancel_calls.append("ray-cancel"),
            ),
        )
        cmd.active_tasks = {task.pk: "raysubmit_1"}

        finalized: list[int] = []

        def fake_finalize(t: RayTaskExecution) -> None:
            finalized.append(t.pk)
            t.state = TaskState.CANCELLED
            t.finished_at = datetime.now(UTC)
            t.save(update_fields=["state", "finished_at"])

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
