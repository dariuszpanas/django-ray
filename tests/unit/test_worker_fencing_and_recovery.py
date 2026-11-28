"""Unit tests for worker fencing, DB-clock leases, transient DB recovery, and CLI."""

from __future__ import annotations

import signal
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.management import CommandError
from django.core.management.base import CommandParser
from django.db import InterfaceError, OperationalError, connection
from django.test.utils import CaptureQueriesContext

from django_ray import lease_policy, worker_readiness
from django_ray.lease_policy import LeasePolicyError
from django_ray.management.commands import django_ray_worker as worker_module
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
)
from django_ray.result_storage import ResultStorageError
from django_ray.runner import reconciliation
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
    request_cancellation,
)
from django_ray.runner.ray_core import RayCoreCompletion, RayCoreHandle


def _make_command(worker_id: str = "fencing-worker") -> Command:
    cmd = Command(stdout=StringIO())
    cmd.worker_id = worker_id
    cmd.execution_mode = "sync"
    cmd.sync_mode = False
    cmd.active_tasks = {}
    cmd.active_task_identities = {}
    return cmd


class TestLeasePolicyErrorClassification:
    """F-001: lock_lease_policy must not map connection failures to lease_policy_busy."""

    @pytest.mark.parametrize(
        ("vendor", "cause_attrs", "error_cls", "expected_reason"),
        [
            ("postgresql", {"sqlstate": "55P03"}, OperationalError, "lease_policy_busy"),
            ("postgresql", {"sqlstate": "57014"}, OperationalError, "lease_policy_busy"),
            ("postgresql", {"sqlstate": "08006"}, OperationalError, "lease_policy_unavailable"),
            ("postgresql", {"sqlstate": "57P01"}, OperationalError, "lease_policy_unavailable"),
            ("postgresql", {}, InterfaceError, "lease_policy_unavailable"),
            ("sqlite", {"sqlite_errorcode": 5}, OperationalError, "lease_policy_busy"),
            ("sqlite", {"sqlite_errorcode": 6}, OperationalError, "lease_policy_busy"),
            ("sqlite", {"sqlite_errorcode": 10}, OperationalError, "lease_policy_unavailable"),
            ("sqlite", {"sqlite_errorcode": 11}, OperationalError, "lease_policy_unavailable"),
            ("sqlite", {}, InterfaceError, "lease_policy_unavailable"),
        ],
    )
    def test_lock_lease_policy_classifies_driver_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        vendor: str,
        cause_attrs: dict[str, object],
        error_cls: type[Exception],
        expected_reason: str,
    ) -> None:
        cause = RuntimeError("driver error")
        for key, value in cause_attrs.items():
            setattr(cause, key, value)
        raised = error_cls("db failure")
        raised.__cause__ = cause

        class FailingCursor:
            def execute(self, _sql: str, _params: list[object]) -> None:
                raise raised

            def fetchone(self) -> tuple[int]:
                return (1,)

        @contextmanager
        def fake_cursor():
            yield FailingCursor()

        if vendor == "postgresql":
            fake_conn = SimpleNamespace(
                vendor="postgresql",
                in_atomic_block=True,
                cursor=fake_cursor,
                ops=SimpleNamespace(quote_name=lambda name: f'"{name}"'),
            )
            monkeypatch.setattr(lease_policy, "connections", {"default": fake_conn})
        else:
            from django.db.models.query import QuerySet

            monkeypatch.setattr(
                lease_policy,
                "connections",
                {"default": SimpleNamespace(vendor="sqlite", in_atomic_block=True)},
            )
            monkeypatch.setattr(
                QuerySet, "update", lambda *_args, **_kwargs: (_ for _ in ()).throw(raised)
            )

        with pytest.raises(LeasePolicyError, match=f"^{expected_reason}$"):
            lease_policy.lock_lease_policy()


@pytest.mark.django_db
class TestWorkerFencingAndRecovery:
    """Verify F-001, F-002, F-009, F-011, F-014, F-018, F-020, F-021, F-022, F-052, F-063, F-064."""

    def test_hot_path_never_locks_singleton_policy_or_rewrites_queue_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("hot-path-worker")
        cmd.execution_mode = "sync"
        policy_locks = 0
        original_lock = lease_policy.lock_lease_policy

        def counting_lock(*args: Any, **kwargs: Any):
            nonlocal policy_locks
            policy_locks += 1
            return original_lock(*args, **kwargs)

        monkeypatch.setattr(lease_policy, "lock_lease_policy", counting_lock)

        cmd._create_lease("default")
        assert policy_locks == 1

        with CaptureQueriesContext(connection) as captured:
            cmd.send_heartbeat()
        assert policy_locks == 1
        update_sqls = [
            q["sql"].lower() for q in captured if q["sql"].lstrip().upper().startswith("UPDATE")
        ]
        assert update_sqls
        assert all("queue_name" not in sql for sql in update_sqls)
        assert any("lease_expires_at" in sql for sql in update_sqls)

        assert cmd._update_lease_heartbeat() is True
        assert cmd.claim_and_process_tasks(["default"], concurrency=1) == 0
        assert policy_locks == 1

    def test_send_heartbeat_recovers_transient_db_drop_within_ttl_and_fails_past_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("transient-hb-worker")
        cmd._create_lease("default")

        calls = 0
        original_lock_leases = cmd._lock_authoritative_leases

        def fail_once(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("server closed the connection unexpectedly")
            return original_lock_leases(*args, **kwargs)

        closed: list[bool] = []
        monkeypatch.setattr(worker_module, "close_old_connections", lambda: closed.append(True))
        monkeypatch.setattr(cmd, "_lock_authoritative_leases", fail_once)

        # Within lease TTL: transient OperationalError must NOT cause suicide.
        cmd.send_heartbeat()
        assert cmd.lease_ownership_lost is False
        assert cmd.shutdown_requested is False
        assert cmd._heartbeat_retry_after > 0.0
        assert closed == [True]

        # Recovery tick succeeds and clears retry timer.
        cmd.send_heartbeat()
        assert cmd.lease_ownership_lost is False
        assert cmd._heartbeat_retry_after == 0.0

        # Once past lease TTL, transient error MUST fail closed.
        cmd._last_successful_heartbeat_monotonic -= 120.0
        monkeypatch.setattr(
            cmd,
            "_lock_authoritative_leases",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(InterfaceError("connection closed")),
        )
        cmd.send_heartbeat()
        assert cmd.lease_ownership_lost is True
        assert cmd.shutdown_requested is True
        assert cmd.shutdown_exit_code == 1

    def test_run_loop_calls_close_old_connections_and_recovers_transient_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("loop-recovery-worker")
        cmd._create_lease("default")
        closed: list[bool] = []
        claims = 0

        monkeypatch.setattr(worker_module, "close_old_connections", lambda: closed.append(True))
        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _q: 0)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda _t: None)

        def flaky_claim(_queues: Any, _concurrency: int) -> int:
            nonlocal claims
            claims += 1
            if claims == 1:
                raise OperationalError("temporary connection reset")
            cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "claim_and_process_tasks", flaky_claim)

        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=60.0)

        assert claims == 2
        assert len(closed) >= 3
        assert cmd.lease_ownership_lost is False

    def test_handle_unhandled_exception_sets_nonzero_exit_code_for_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("crash-worker")
        monkeypatch.setattr(
            worker_module, "get_settings", lambda: {"DEFAULT_CONCURRENCY": 1, "RUNNER": "ray_job"}
        )
        monkeypatch.setattr(cmd, "_create_lease", lambda _queue: None)
        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)
        monkeypatch.setattr(
            cmd,
            "run_loop",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected loop crash")),
        )

        with pytest.raises(RuntimeError, match="unexpected loop crash"):
            cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=1,
                sync=True,
                local=False,
                cluster=None,
                ray_job=False,
            )

        assert cmd.shutdown_exit_code == 1
        assert "shut down with errors" in cmd.stdout.getvalue()
        assert "shut down cleanly" not in cmd.stdout.getvalue()

    def test_fenced_mutation_sites_refuse_writes_after_lease_loss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("fenced-sites-worker")
        cmd._create_lease("default")
        assert cmd.lease_identity is not None
        task = RayTaskExecution.objects.create(
            task_id="fenced-site-task-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            attempt_number=1,
            execution_generation=2,
            args_json="[1, 2]",
            kwargs_json="{}",
            runtime_env_json='{"env_vars":{"A":"1"}}',
            runtime_env_hash="0" * 64,
        )
        # Invalidate lease in DB before hitting unfenced sites.
        TaskWorkerLease.objects.filter(**cmd.lease_identity.database_filters()).update(
            is_active=False, stopped_at=datetime.now(UTC)
        )

        # 1. process_task RuntimeEnv failure path must not mutate task.
        cmd.process_task(task)
        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.error_message is None
        assert cmd.lease_ownership_lost is True

        # Reset local flag while lease remains inactive in DB to test remaining sites.
        cmd.lease_ownership_lost = False
        cmd.shutdown_requested = False

        # 2. execute_task_sync exception path must not mutate task.
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("sync boom")),
        )
        cmd.execute_task_sync(task)
        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.error_message is None

        # 3. submit_task_to_ray_core after _reconnect_ray lease loss must not mutate task.
        cmd.lease_ownership_lost = False
        cmd.shutdown_requested = False
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: False))
        monkeypatch.setattr(
            cmd,
            "_reconnect_ray",
            lambda: cmd._request_shutdown_for_lease_loss("lost during reconnect"),
        )
        cmd.submit_task_to_ray_core(task)
        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.error_message is None

        # 4. _fenced_persist_submission_tracking must refuse writes when lease is inactive.
        cmd.lease_ownership_lost = False
        cmd.shutdown_requested = False
        handle = SubmissionHandle(
            ray_job_id="raysubmit_blocked",
            ray_address="ray://cluster:10001",
            submitted_at=datetime.now(UTC),
        )
        assert (
            cmd._fenced_persist_submission_tracking(
                task,
                handle,
                expected_worker_id=cmd.worker_id,
                expected_attempt_number=1,
                expected_execution_generation=2,
                expected_ray_job_id=None,
            )
            is False
        )
        task.refresh_from_db()
        assert task.ray_job_id is None
        assert not TaskAttempt.objects.filter(execution=task).exists()

    @pytest.mark.django_db(transaction=True)
    def test_result_spill_runs_outside_db_lock_and_explicit_external_failure_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch, settings: Any
    ) -> None:
        cmd = _make_command("spill-worker")
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="spill-outside-lock-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        atomic_states: list[bool] = []

        class RecordingStorage:
            def store(self, *, serialized_result: str) -> str:
                atomic_states.append(connection.in_atomic_block)
                return f"resultfs://sha256/{'a' * 64}?rel=aa/aa/{'a' * 64}.json&bytes={len(serialized_result)}"

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "MAX_RESULT_SIZE_BYTES": 1,
            "RESULT_STORAGE_BACKEND": "filesystem",
            "RESULT_STORAGE_FILESYSTEM_PATH": "/tmp/results",
        }
        monkeypatch.setattr(
            "django_ray.result_storage.get_result_storage_backend",
            lambda _cfg: RecordingStorage(),
        )
        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda **_kwargs: '{"success": true, "result": {"payload": "large"}}',
        )

        cmd.execute_task_sync(task)

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert atomic_states == [False]

        # Explicit external storage failure must FAIL retryable=False (Decision 4).
        failing_task = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id="spill-fail-loud-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        class FailingExternalStorage:
            def store(self, *, serialized_result: str) -> str:
                del serialized_result
                raise ResultStorageError("S3 PutObject returned 503")

        monkeypatch.setattr(
            "django_ray.result_storage.get_result_storage_backend",
            lambda _cfg: FailingExternalStorage(),
        )

        cmd.execute_task_sync(failing_task)

        failing_task.refresh_from_db()
        assert failing_task.state == TaskState.FAILED
        assert failing_task.attempt_number == 1
        assert failing_task.result_reference is None
        assert "external storage" in (failing_task.error_message or "")

    @pytest.mark.django_db(transaction=True)
    def test_timeout_and_cancellation_prepare_remote_client_outside_db_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("prep-lock-worker")
        cmd._create_lease("default")
        started_at = datetime.now(UTC) - timedelta(minutes=5)
        timed_out = RayTaskExecution.objects.create(
            task_id="timeout-prep-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            started_at=started_at,
            last_heartbeat_at=started_at,
            timeout_seconds=1,
            ray_job_id="raysubmit_timeout_prep",
            ray_address="ray://cluster:10001",
        )
        cancelling = RayTaskExecution.objects.create(
            task_id="cancel-prep-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.CANCELLING,
            claimed_by_worker=cmd.worker_id,
            started_at=started_at,
            ray_job_id="raysubmit_cancel_prep",
            ray_address="ray://cluster:10001",
        )
        prepare_atomic_states: list[tuple[str, bool]] = []

        class TrackingRayJobRunner:
            def prepare_cancellation(self, handle: SubmissionHandle) -> str:
                prepare_atomic_states.append((handle.ray_job_id, connection.in_atomic_block))
                return handle.ray_job_id

            def cancel_prepared_with_status(
                self, handle: SubmissionHandle, capability: object
            ) -> CancellationOutcome:
                assert capability == handle.ray_job_id
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", TrackingRayJobRunner)

        assert cmd.detect_stuck_tasks(["default"]) == 1
        assert cmd.process_cancellations(["default"]) == 1

        timed_out.refresh_from_db()
        cancelling.refresh_from_db()
        assert timed_out.state == TaskState.FAILED
        assert cancelling.state == TaskState.CANCELLED
        assert prepare_atomic_states == [
            ("raysubmit_timeout_prep", False),
            ("raysubmit_cancel_prep", False),
        ]

    def test_poll_ray_core_peek_ack_retains_handle_on_db_error_and_skips_idle_db_polls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("peek-ack-worker")
        cmd.execution_mode = "local"
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="peek-ack-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        handle = RayCoreHandle(
            task_pk=task.pk,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="add",
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
        )
        completion = RayCoreCompletion(
            task_pk=task.pk,
            attempt_number=task.attempt_number,
            execution_generation=task.execution_generation,
            result_json='{"success": true, "result": 3}',
        )

        class PeekAckRunner:
            def __init__(self) -> None:
                self.pending: dict[int, RayCoreHandle] = {task.pk: handle}
                self.ready: list[RayCoreCompletion] = []
                self.acked: list[RayCoreHandle] = []

            @property
            def pending_count(self) -> int:
                return len(self.pending)

            @property
            def pending_task_handles(self) -> tuple[RayCoreHandle, ...]:
                return tuple(self.pending.values())

            def retire_pending_handle(self, target: RayCoreHandle) -> bool:
                return self.pending.pop(target.task_pk, None) is not None

            def peek_completed(self, _handles: tuple[RayCoreHandle, ...]):
                return list(self.ready)

            def acknowledge_completed(self, target: RayCoreHandle) -> None:
                self.acked.append(target)
                self.pending.pop(target.task_pk, None)

        runner = PeekAckRunner()
        cmd.ray_core_runner = cast(Any, runner)
        cmd.task_monitor_heartbeat_interval = 15.0
        clock = [100.0]
        monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))

        # Tick 1 (t=100): initial partition + monitor heartbeat.
        assert cmd.poll_ray_core_tasks() == 0

        # Tick 2 (t=100.1): idle poll between heartbeats must perform ZERO DB queries (F-002).
        clock[0] = 100.1
        with CaptureQueriesContext(connection) as idle_queries:
            assert cmd.poll_ray_core_tasks() == 0
        assert len(idle_queries) == 0

        # Tick 3 (t=100.2): task finishes, DB fails transiently -> handle MUST NOT be acked (F-022).
        clock[0] = 100.2
        runner.ready = [completion]
        original_get = RayTaskExecution.objects.get
        monkeypatch.setattr(
            RayTaskExecution.objects,
            "get",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationalError("transient db blip")),
        )
        assert cmd.poll_ray_core_tasks() == 0
        assert runner.acked == []
        assert runner.pending_count == 1

        # Tick 4 (t=100.3): DB recovers -> task succeeds and handle IS acknowledged.
        clock[0] = 100.3
        monkeypatch.setattr(RayTaskExecution.objects, "get", original_get)
        assert cmd.poll_ray_core_tasks() == 1
        assert runner.acked == [handle]
        assert runner.pending_count == 0
        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"

    def test_shutdown_drains_ready_completions_and_preserves_requested_status(self) -> None:
        cmd = _make_command("shutdown-drain-worker")
        cmd.execution_mode = "local"
        cmd._create_lease("default")
        ready_task = RayTaskExecution.objects.create(
            task_id="shutdown-ready-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        running_task = RayTaskExecution.objects.create(
            task_id="shutdown-running-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[]",
            kwargs_json="{}",
        )
        running_handle = RayCoreHandle(
            task_pk=running_task.pk,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="slow",
            attempt_number=running_task.attempt_number,
            execution_generation=running_task.execution_generation,
        )
        ready_completion = RayCoreCompletion(
            task_pk=ready_task.pk,
            attempt_number=ready_task.attempt_number,
            execution_generation=ready_task.execution_generation,
            result_json='{"success": true, "result": 9}',
        )

        class ShutdownRunner:
            pending_task_handles = (running_handle,)

            def drain_ready_for_shutdown(self):
                return [ready_completion]

            def cancel_pending_with_status(self, _handle: RayCoreHandle) -> CancellationOutcome:
                return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        cmd.ray_core_runner = cast(Any, ShutdownRunner())
        cmd._prepare_shutdown_handoff()

        ready_task.refresh_from_db()
        running_task.refresh_from_db()
        assert ready_task.state == TaskState.SUCCEEDED
        assert ready_task.result_data == "9"
        assert running_task.state == TaskState.CANCELLED
        assert running_task.cancellation_status == CancellationStatus.REQUESTED

        # Peer recovery on a pre-existing CANCELLING ray_core row preserves REQUESTED.
        legacy_cancelling = RayTaskExecution.objects.create(
            task_id="legacy-ray-core-cancelling-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.CANCELLING,
            claimed_by_worker="dead-worker",
            ray_job_id="ray_core:999",
            cancellation_status=CancellationStatus.REQUESTED,
            cancellation_error="stopped during graceful shutdown",
        )
        peer = _make_command("peer-cancel-worker")
        peer._create_lease("default")
        assert peer.process_cancellations(["default"]) == 1
        legacy_cancelling.refresh_from_db()
        assert legacy_cancelling.state == TaskState.CANCELLED
        assert legacy_cancelling.cancellation_status == CancellationStatus.REQUESTED
        assert legacy_cancelling.cancellation_error == "stopped during graceful shutdown"

    def test_sync_mode_caps_claim_batch_to_one_and_heartbeat_runs_during_execution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("sync-cap-worker")
        cmd.execution_mode = "sync"
        cmd._create_lease("default")
        tasks = [
            RayTaskExecution.objects.create(
                task_id=f"sync-cap-{idx}",
                callable_path="testproject.tasks.add_numbers",
                queue_name="default",
                state=TaskState.QUEUED,
                args_json="[1, 2]",
                kwargs_json="{}",
            )
            for idx in range(3)
        ]
        processed: list[int] = []
        monkeypatch.setattr(cmd, "process_task", lambda t: processed.append(t.pk))

        assert cmd.claim_and_process_tasks(["default"], concurrency=20) == 1
        assert len(processed) == 1
        assert RayTaskExecution.objects.filter(state=TaskState.QUEUED).count() == 2

        # Verify background sync heartbeat thread fires during long sync task.
        cmd._allow_sync_heartbeat_in_memory = True
        beats: list[bool] = []
        monkeypatch.setattr(
            worker_module, "get_heartbeat_interval", lambda: timedelta(seconds=0.05)
        )
        monkeypatch.setattr(cmd, "_update_lease_heartbeat", lambda: beats.append(True) or True)
        monkeypatch.setattr(cmd, "_mark_task_monitor_heartbeat", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(worker_module, "close_old_connections", lambda: None)

        import time as real_time

        with cmd._sync_execution_heartbeat(tasks[0]):
            real_time.sleep(0.35)
        assert len(beats) >= 1

    @pytest.mark.django_db(transaction=True)
    def test_db_clock_readiness_immune_to_host_clock_skew_and_supports_multi_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("multi-queue-ready-worker")
        cmd._create_lease("default,high-priority,ml")
        assert cmd.lease_identity is not None
        hostname = cmd.lease_identity.hostname

        # Host clock skewed +10 minutes ahead AND -10 minutes behind must still be ready.
        for offset in (timedelta(minutes=10), timedelta(minutes=-10)):
            monkeypatch.setattr(
                worker_readiness.timezone, "now", lambda delta=offset: datetime.now(UTC) + delta
            )
            report = worker_readiness.check_worker_lease_readiness(
                queue="high-priority",
                hostname=hostname,
            )
            assert report.status == "ready"
            assert report.reason == "live_lease"

        # Non-member prefix must not match.
        assert (
            worker_readiness.check_worker_lease_readiness(
                queue="high",
                hostname=hostname,
            ).reason
            == "no_live_lease"
        )

        # Backdated lease_expires_at reports not_ready.
        TaskWorkerLease.objects.filter(**cmd.lease_identity.database_filters()).update(
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=5)
        )
        assert (
            worker_readiness.check_worker_lease_readiness(
                queue="high-priority",
                hostname=hostname,
            ).reason
            == "no_live_lease"
        )

    def test_cli_ray_job_flag_help_strings_and_queue_dedup(self) -> None:
        cmd = _make_command()
        parser = CommandParser(prog="manage.py django_ray_worker", missing_args_message="")
        cmd.add_arguments(parser)

        for action in parser._actions:
            if action.option_strings and "--help" not in action.option_strings:
                assert action.help, f"Missing help= for {action.option_strings}"

        args = parser.parse_args(["--ray-job", "--queue", " default , high , default "])
        assert args.ray_job is True
        assert cmd._requested_worker_family(vars(args)) == "ray_job"
        assert cmd._parse_queues(vars(args)) == ["default", "high"]
        assert cmd._parse_queues({"queue": "  single  "}) == ["single"]
        assert cmd._parse_queues({"queues": [" a ", "b", "a"]}) == ["a", "b"]

        for conflicting in (
            ["--ray-job", "--sync"],
            ["--ray-job", "--local"],
            ["--ray-job", "--cluster", "ray://cluster:10001"],
        ):
            with pytest.raises(CommandError, match="not allowed with argument"):
                parser.parse_args(conflicting)

    def test_signal_handler_does_not_write_stdout_and_logs_on_shutdown(self) -> None:
        cmd = _make_command("signal-safe-worker")
        cmd.handle_shutdown_signal(signal.SIGTERM, None)
        assert cmd.stdout.getvalue() == ""
        assert cmd.shutdown_requested is True
        assert cmd.shutdown_signal == signal.SIGTERM

        cmd.shutdown()
        assert "Received signal" in cmd.stdout.getvalue()

    def test_batch_claim_and_handoff_clears_manager_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("batch-claim-worker")
        cmd.execution_mode = "ray"
        cmd._create_lease("default")
        tasks = [
            RayTaskExecution.objects.create(
                task_id=f"batch-claim-{idx}",
                callable_path="testproject.tasks.add_numbers",
                queue_name="default",
                state=TaskState.QUEUED,
                args_json="[1, 2]",
                kwargs_json="{}",
            )
            for idx in range(3)
        ]
        monkeypatch.setattr(cmd, "process_task", lambda _task: None)

        with CaptureQueriesContext(connection) as captured:
            assert cmd.claim_and_process_tasks(["default"], concurrency=3) == 3

        task_updates = [
            q["sql"]
            for q in captured
            if q["sql"].lstrip().upper().startswith("UPDATE")
            and RayTaskExecution._meta.db_table in q["sql"]
        ]
        assert len(task_updates) == 1

        # Verify _handoff_unsubmitted_task clears managed_with_django_ray_version (F-064).
        tasks[0].refresh_from_db()
        assert tasks[0].managed_with_django_ray_version is not None
        cmd._handoff_unsubmitted_task(tasks[0])
        tasks[0].refresh_from_db()
        assert tasks[0].state == TaskState.QUEUED
        assert tasks[0].managed_with_django_ray_version is None

    def test_submit_task_to_ray_release_failure_delegates_to_confirmation_loss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("release-fail-worker")
        cmd.execution_mode = "ray"
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="release-fail-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        reserved = SubmissionHandle(
            ray_job_id="raysubmit_release_fail",
            ray_address="ray://cluster:10001",
            submitted_at=datetime.now(UTC),
        )

        class FailingSubmitRunner:
            def submission_handle(self, _task: RayTaskExecution) -> SubmissionHandle:
                return reserved

            def submit_durable(self, **_kwargs: Any) -> SubmissionHandle:
                raise RuntimeError("submit failed")

        loss_calls: list[str] = []
        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FailingSubmitRunner)
        monkeypatch.setattr(
            "django_ray.ray_job_request_storage.release_ray_job_request_reservation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationalError("db drop")),
        )
        monkeypatch.setattr(
            cmd,
            "_handle_ray_job_confirmation_loss",
            lambda *_args, **kwargs: loss_calls.append(kwargs["detail"]),
        )

        cmd.submit_task_to_ray(task)

        assert len(loss_calls) == 1
        assert task.pk in cmd.active_tasks

    def test_reconciliation_handles_naive_timestamps_and_cancellation_refreshes_handle(
        self,
    ) -> None:
        naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        naive_task = SimpleNamespace(
            state="RUNNING",
            last_heartbeat_at=naive_past,
            started_at=naive_past,
            timeout_seconds=10,
        )
        assert reconciliation.is_task_stuck(cast(Any, naive_task)) is True
        assert reconciliation.is_task_timed_out(cast(Any, naive_task)) is True

        # request_cancellation refreshes ray_job_id and survives OSError.
        task = RayTaskExecution.objects.create(
            task_id="cancel-refresh-001",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.RUNNING,
            ray_job_id=None,
            args_json="[]",
            kwargs_json="{}",
        )
        RayTaskExecution.objects.filter(pk=task.pk).update(
            ray_job_id="raysubmit_attached_later",
            ray_address="ray://cluster:10001",
        )
        seen: list[str] = []

        class OSErrorRunner:
            def cancel(self, handle: SubmissionHandle) -> bool:
                seen.append(handle.ray_job_id)
                raise OSError("network unreachable")

        assert request_cancellation(task, runner=cast(Any, OSErrorRunner())) is True
        assert seen == ["raysubmit_attached_later"]
        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
