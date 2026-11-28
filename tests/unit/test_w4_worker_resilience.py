"""Wave 4 worker and control-plane resilience tests (R2-001 through R2-038)."""

from __future__ import annotations

import json
import sys
import threading
import time as real_time
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.management.base import CommandError
from django.db import InterfaceError, OperationalError, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_ray import lease_policy, worker_readiness
from django_ray.decorators import ray_task
from django_ray.execution_codec import (
    ExecutionCompletion,
    ExecutionIdentity,
    encode_execution_completion,
)
from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION
from django_ray.management.commands import django_ray_worker as worker_module
from django_ray.models import CancellationStatus, RayTaskExecution, TaskState, TaskWorkerLease
from django_ray.runner import ray_job as ray_job_module
from django_ray.runner import retry as retry_module
from django_ray.runner.base import JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcome, CancellationOutcomeStatus
from django_ray.runner.ray_core import RayCoreCompletion, RayCoreHandle, RayCoreRunner
from django_ray.runner.sync import SyncRunner
from django_ray.worker import claimer, dispatcher


@ray_task(
    num_cpus=2,
    num_gpus=1,
    memory=1024 * 1024,
    max_attempts=5,
    retry_backoff_seconds=12,
    retry_jitter=False,
    timeout_seconds=45,
    runtime_env_profile="ml-gpu",
)
def _contract_task_with_overrides(x: int) -> int:
    return x + 1


@ray_task(max_attempts=1, timeout_seconds=15)
def _contract_single_attempt_task() -> str:
    return "done"


class _FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


def _make_command(worker_id: str = "w4-resilience-worker") -> worker_module.Command:
    cmd = worker_module.Command(stdout=StringIO())
    cmd._set_worker_id(worker_id)
    return cmd


@pytest.mark.django_db(transaction=True)
class TestR2001ShutdownDrainAndDecisiveCancellation:
    """R2-001: Unpack real RayCoreRunner.drain_ready_for_shutdown() and preserve decisive status."""

    def test_shutdown_handoff_unpacks_real_ray_core_drain_tuple_and_acks_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-001-real-drain-worker")
        cmd.execution_mode = "local"
        cmd._create_lease("default")

        ready_task = RayTaskExecution.objects.create(
            task_id="r2-001-ready",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[2, 3]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=1,
        )
        running_task = RayTaskExecution.objects.create(
            task_id="r2-001-running",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=1,
        )

        ready_ref = object()
        running_ref = object()
        ready_envelope = encode_execution_completion(
            ExecutionCompletion(
                identity=ExecutionIdentity(
                    task_execution_pk=int(ready_task.pk),
                    task_id=ready_task.task_id,
                    attempt_number=1,
                    execution_generation=1,
                ),
                execution_protocol_version=int(ready_task.execution_protocol_version),
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

        cancelled_refs: list[object] = []
        in_atomic_during_cancel: list[bool] = []

        def fake_wait(refs: list[object], **_kwargs: Any) -> tuple[list[object], list[object]]:
            ready = [r for r in refs if r is ready_ref]
            pending = [r for r in refs if r is not ready_ref]
            return ready, pending

        def fake_get(ref: object) -> str:
            assert ref is ready_ref
            return ready_envelope

        def fake_cancel(ref: object, **_kwargs: Any) -> None:
            in_atomic_during_cancel.append(connection.in_atomic_block)
            cancelled_refs.append(ref)

        fake_ray = SimpleNamespace(
            wait=fake_wait,
            get=fake_get,
            cancel=fake_cancel,
            is_initialized=lambda: True,
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setitem(
            sys.modules, "ray.exceptions", SimpleNamespace(RayTaskError=RuntimeError)
        )

        runner = RayCoreRunner()
        ready_handle = RayCoreHandle(
            task_pk=int(ready_task.pk),
            object_ref=ready_ref,
            submitted_at=datetime.now(UTC),
            task_name="add",
            attempt_number=1,
            execution_generation=1,
        )
        running_handle = RayCoreHandle(
            task_pk=int(running_task.pk),
            object_ref=running_ref,
            submitted_at=datetime.now(UTC),
            task_name="slow",
            attempt_number=1,
            execution_generation=1,
        )
        runner._pending_tasks[ready_handle.task_pk] = ready_handle
        runner._pending_tasks[running_handle.task_pk] = running_handle

        cmd.ray_core_runner = runner
        cmd._prepare_shutdown_handoff()

        ready_task.refresh_from_db()
        running_task.refresh_from_db()
        assert ready_task.state == TaskState.SUCCEEDED
        assert json.loads(str(ready_task.result_data)) == 5
        assert running_task.state == TaskState.CANCELLED
        assert running_task.cancellation_status == CancellationStatus.REQUESTED
        # Ready handle acknowledged & retired; running handle cancelled & retired outside DB lock.
        assert runner.pending_count == 0
        assert cancelled_refs == [running_ref]
        assert in_atomic_during_cancel == [False]

    def test_decisive_failed_and_not_applicable_statuses_preserved_across_shutdown_and_peer_recovery(
        self,
    ) -> None:
        cmd = _make_command("r2-001-decisive-shutdown")
        cmd.execution_mode = "local"
        cmd._create_lease("default")

        failed_task = RayTaskExecution.objects.create(
            task_id="r2-001-cancel-failed",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id="ray_core:failed",
            args_json="[]",
            kwargs_json="{}",
        )
        na_task = RayTaskExecution.objects.create(
            task_id="r2-001-cancel-na",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id="ray_core:na",
            args_json="[]",
            kwargs_json="{}",
        )

        failed_handle = RayCoreHandle(
            task_pk=int(failed_task.pk),
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="slow",
            attempt_number=failed_task.attempt_number,
            execution_generation=failed_task.execution_generation,
        )
        na_handle = RayCoreHandle(
            task_pk=int(na_task.pk),
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="slow",
            attempt_number=na_task.attempt_number,
            execution_generation=na_task.execution_generation,
        )

        class DecisiveRunner:
            pending_task_handles = (failed_handle, na_handle)

            def drain_ready_for_shutdown(self) -> tuple[list[RayCoreCompletion], tuple[Any, ...]]:
                return [], self.pending_task_handles

            def cancel_pending_with_status(self, handle: RayCoreHandle) -> CancellationOutcome:
                if handle is failed_handle:
                    return CancellationOutcome(
                        CancellationOutcomeStatus.FAILED, "ray rejected cancel"
                    )
                return CancellationOutcome(
                    CancellationOutcomeStatus.NOT_APPLICABLE, "already exited"
                )

        cmd.ray_core_runner = cast(Any, DecisiveRunner())
        cmd._prepare_shutdown_handoff()

        na_task.refresh_from_db()
        failed_task.refresh_from_db()
        assert na_task.state == TaskState.CANCELLED
        assert na_task.cancellation_status == CancellationStatus.NOT_APPLICABLE
        assert failed_task.state == TaskState.CANCELLING
        assert failed_task.cancellation_status == CancellationStatus.FAILED
        assert failed_task.cancellation_error == "ray rejected cancel"

        # Mark original worker inactive so peer worker recovers failed_task cleanly.
        assert cmd.lease_identity is not None
        TaskWorkerLease.objects.filter(**cmd.lease_identity.database_filters()).update(
            is_active=False
        )
        peer = _make_command("r2-001-peer-recovery")
        peer._create_lease("default")
        assert peer.process_cancellations(["default"]) == 1

        failed_task.refresh_from_db()
        assert failed_task.state == TaskState.CANCELLED
        # Decisive FAILED must NOT be overwritten with INDETERMINATE during peer recovery!
        assert failed_task.cancellation_status == CancellationStatus.FAILED
        assert failed_task.cancellation_error == "ray rejected cancel"


class TestR2005RayTaskContractRuntimeWiring:
    """R2-005: @ray_task contract overrides retry, backoff, jitter, timeout, profile, and options."""

    def test_should_retry_honors_per_task_contract_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observed_at = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return observed_at

        monkeypatch.setattr(retry_module, "datetime", FixedClock)
        monkeypatch.setattr(
            retry_module,
            "get_settings",
            lambda: {
                "MAX_TASK_ATTEMPTS": 3,
                "RETRY_BACKOFF_SECONDS": 60,
                "RETRY_BACKOFF_JITTER_RATIO": 0.2,
                "RETRY_EXCEPTION_DENYLIST": [],
            },
        )

        override_path = f"{__name__}._contract_task_with_overrides"
        # Attempt 4 exceeds global MAX_TASK_ATTEMPTS=3, but contract max_attempts=5 permits retry.
        decision_4 = retry_module.should_retry(
            cast(Any, SimpleNamespace(attempt_number=4, callable_path=override_path)),
            exception_type="RuntimeError",
            random_value=lambda: 1.0,
        )
        assert decision_4.should_retry is True
        # Base=12s * 2^(4-1) = 96s, retry_jitter=False => exact 96s (no downward jitter).
        assert decision_4.next_attempt_at == observed_at + timedelta(seconds=96)

        decision_5 = retry_module.should_retry(
            cast(Any, SimpleNamespace(attempt_number=5, callable_path=override_path)),
            exception_type="RuntimeError",
        )
        assert decision_5.should_retry is False
        assert "5" in (decision_5.reason or "")

        # Single-attempt contract prevents retry even when global MAX_TASK_ATTEMPTS=3.
        single_path = f"{__name__}._contract_single_attempt_task"
        decision_single = retry_module.should_retry(
            cast(Any, SimpleNamespace(attempt_number=1, callable_path=single_path)),
            exception_type="RuntimeError",
        )
        assert decision_single.should_retry is False
        assert "1" in (decision_single.reason or "")

    @pytest.mark.django_db
    def test_dispatcher_and_maintenance_apply_contract_timeout_profile_and_ray_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-005-dispatch-worker")
        cmd.execution_mode = "local"
        cmd._create_lease("default")

        from django_ray.runtime.runtime_env import (
            resolve_runtime_env_profile,
            runtime_env_for_storage,
        )

        override_path = f"{__name__}._contract_task_with_overrides"
        empty_snapshot = runtime_env_for_storage(
            resolve_runtime_env_profile(None),
            task_id="r2-005-dispatch-001",
        )
        task = RayTaskExecution.objects.create(
            task_id="r2-005-dispatch-001",
            callable_path=override_path,
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1]",
            kwargs_json="{}",
            timeout_seconds=None,
            runtime_env_profile=None,
            runtime_env_json=empty_snapshot.serialized,
            runtime_env_hash=empty_snapshot.digest,
        )

        captured_ray_options: list[dict[str, Any]] = []

        class CapturingRayCoreRunner:
            pending_count = 0
            pending_task_handles = ()

            def get_pending_handle(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            def submit_durable(
                self, *, task_execution: Any, ray_options: dict[str, Any] | None = None
            ) -> SubmissionHandle:
                assert task_execution.timeout_seconds == 45
                # R3-009: unsnapshotted/pre-contract rows keep runtime_env_profile=None so
                # runtime_env_for_execution() succeeds and the runner applies contract fallback.
                assert task_execution.runtime_env_profile is None
                assert task_execution._ray_task_contract.runtime_env_profile == "ml-gpu"
                captured_ray_options.append(dict(ray_options or {}))
                return SubmissionHandle(
                    ray_job_id=f"ray_core:{task_execution.pk}",
                    ray_address="",
                    submitted_at=datetime.now(UTC),
                )

        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        cmd.ray_core_runner = cast(Any, CapturingRayCoreRunner())

        cmd.process_task(task)

        task.refresh_from_db()
        assert task.timeout_seconds == 45
        assert captured_ray_options == [{"num_cpus": 2.0, "num_gpus": 1.0, "memory": 1024 * 1024}]

        # Even if a running row has timeout_seconds=None, detect_stuck_tasks honors contract timeout.
        single_path = f"{__name__}._contract_single_attempt_task"
        timed_out_task = RayTaskExecution.objects.create(
            task_id="r2-005-timeout-001",
            callable_path=single_path,
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            started_at=datetime.now(UTC) - timedelta(seconds=60),
            last_heartbeat_at=datetime.now(UTC),
            timeout_seconds=None,
            args_json="[]",
            kwargs_json="{}",
        )
        assert cmd.detect_stuck_tasks(["default"]) == 1
        timed_out_task.refresh_from_db()
        assert timed_out_task.state == TaskState.FAILED
        assert "15 seconds" in (timed_out_task.error_message or "")


@pytest.mark.django_db(transaction=True)
class TestR2007LockFreeRemoteRpcsAndResultSpill:
    """R2-007: All 5 worker cancel RPC & result-spill sites run strictly outside DB transactions."""

    def test_timeout_and_cancellation_rpcs_run_outside_atomic_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-007-maint-worker")
        cmd._create_lease("default")

        timed_out_task = RayTaskExecution.objects.create(
            task_id="r2-007-timeout-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_r2007_timeout",
            ray_address="http://127.0.0.1:8265",
            started_at=datetime.now(UTC) - timedelta(seconds=120),
            last_heartbeat_at=datetime.now(UTC),
            timeout_seconds=10,
            args_json="[]",
            kwargs_json="{}",
        )
        cancelling_task = RayTaskExecution.objects.create(
            task_id="r2-007-cancel-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.CANCELLING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_r2007_cancel",
            ray_address="http://127.0.0.1:8265",
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            args_json="[]",
            kwargs_json="{}",
        )

        observed_atomic: list[tuple[str, bool]] = []

        def fake_prepare_cancel(_task: Any) -> None:
            observed_atomic.append(("prepare", connection.in_atomic_block))
            return None

        def fake_timeout_cancel(_task: Any) -> CancellationOutcome:
            observed_atomic.append(("timeout", connection.in_atomic_block))
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        def fake_process_cancel(_task: Any) -> CancellationOutcome:
            observed_atomic.append(("cancel", connection.in_atomic_block))
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        monkeypatch.setattr(cmd, "_prepare_remote_cancellation_for_task", fake_prepare_cancel)
        monkeypatch.setattr(cmd, "_request_timeout_cancellation", fake_timeout_cancel)
        assert cmd.detect_stuck_tasks(["default"]) == 1

        monkeypatch.setattr(cmd, "_request_cancellation_for_task", fake_process_cancel)
        assert cmd.process_cancellations(["default"]) == 1

        timed_out_task.refresh_from_db()
        cancelling_task.refresh_from_db()
        assert timed_out_task.state == TaskState.FAILED
        assert cancelling_task.state == TaskState.CANCELLED
        # Client/handle preparation runs outside DB locks, while remote stop + terminal
        # transition serialize inside the authoritative execution row lock.
        assert observed_atomic == [
            ("prepare", False),
            ("timeout", True),
            ("prepare", False),
            ("cancel", True),
        ]

    def test_reconciler_resolve_stale_untrusted_runs_cancel_outside_atomic_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-007-reconciler-worker")
        cmd.execution_mode = "ray"
        cmd._create_lease("default")

        task = RayTaskExecution.objects.create(
            task_id="r2-007-untrusted-001",
            callable_path="testproject.tasks.slow_task",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_r2007_legacy_failed",
            ray_address="http://127.0.0.1:8265",
            started_at=datetime.now(UTC) - timedelta(seconds=600),
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
            attempt_number=1,
            execution_generation=1,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd.active_tasks[task.pk] = str(task.ray_job_id)
        cmd.active_task_identities[task.pk] = (1, 1)

        atomic_events: list[tuple[str, bool]] = []

        class FakeRunner:
            def get_status(self, handle: SubmissionHandle) -> JobInfo:
                return JobInfo(job_id=handle.ray_job_id, status=JobStatus.FAILED)

        def fake_prepare_cancel(*_args: Any, **_kwargs: Any) -> Any:
            atomic_events.append(("prepare", connection.in_atomic_block))
            return None

        def fake_request_cancel(*_args: Any, **_kwargs: Any) -> CancellationOutcome:
            atomic_events.append(("cancel", connection.in_atomic_block))
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED, "stopped")

        monkeypatch.setattr(worker_module, "prepare_remote_cancellation", fake_prepare_cancel)
        monkeypatch.setattr(worker_module, "request_remote_cancellation", fake_request_cancel)
        completed: list[int] = []
        cmd._reconcile_ray_job_task(
            task,
            FakeRunner(),
            ray_job_id=str(task.ray_job_id),
            completed_tasks=completed,
            orphaned=False,
            tracked_identity=(1, 1),
        )

        task.refresh_from_db()
        assert task.state == TaskState.LOST
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert task.cancellation_error == "stopped"
        assert atomic_events == [("prepare", False), ("cancel", True)]

    def test_mismatched_ray_job_submission_cancels_and_spills_outside_atomic_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-007-mismatch-worker")
        cmd.execution_mode = "ray"
        cmd._create_lease("default")

        reserved_handle = SubmissionHandle(
            ray_job_id="raysubmit_reserved_r2007",
            ray_address="http://127.0.0.1:8265",
            submitted_at=datetime.now(UTC),
        )
        observed_handle = SubmissionHandle(
            ray_job_id="raysubmit_observed_r2007",
            ray_address="http://127.0.0.1:8265",
            submitted_at=datetime.now(UTC),
        )
        task = RayTaskExecution.objects.create(
            task_id="r2-007-mismatch-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id=reserved_handle.ray_job_id,
            ray_address=reserved_handle.ray_address,
            attempt_number=1,
            execution_generation=1,
            args_json="[]",
            kwargs_json="{}",
        )
        task.completion_data = encode_execution_completion(
            ExecutionCompletion(
                identity=ExecutionIdentity(
                    task_execution_pk=int(task.pk),
                    task_id=task.task_id,
                    attempt_number=1,
                    execution_generation=1,
                ),
                execution_protocol_version=int(task.execution_protocol_version),
                executor_django_ray_version=worker_module.django_ray_version,
                success=True,
                result={"payload": "x" * 2048},
                result_reference=None,
                error=None,
                traceback=None,
                exception_type=None,
                retryable=None,
            )
        )
        task.save(update_fields=["completion_data"])

        observed_atomic: list[tuple[str, bool]] = []

        def fake_cancel_mismatched(*_args: Any, **_kwargs: Any) -> CancellationOutcome:
            observed_atomic.append(("cancel", connection.in_atomic_block))
            return CancellationOutcome(CancellationOutcomeStatus.REQUESTED)

        def fake_store_result(staging: Any, _value: Any) -> tuple[str, ...]:
            observed_atomic.append(("spill", connection.in_atomic_block))
            staging.result_data = None
            staging.result_reference = "digest://sha256:abc123"
            return ()

        monkeypatch.setattr(
            worker_module,
            "get_settings",
            lambda: {"MAX_RESULT_SIZE_BYTES": 64, "RESULT_STORAGE_BACKEND": "digest"},
        )
        monkeypatch.setattr(cmd, "_cancel_mismatched_submissions", fake_cancel_mismatched)
        monkeypatch.setattr(cmd, "_store_task_result", fake_store_result)

        cmd._handle_mismatched_ray_job_submission(
            task,
            object(),
            reserved_handle,
            observed_handle,
            expected_worker_id=cmd.worker_id,
            expected_attempt_number=1,
            expected_execution_generation=1,
            error_message="mismatch",
            exception_type="RayJobSubmissionIdentityError",
        )

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_reference == "digest://sha256:abc123"
        assert observed_atomic == [("cancel", False), ("spill", False)]


@pytest.mark.django_db
class TestR2009TransientLeaseAndSyncHeartbeatResilience:
    """R2-009: Transient LeasePolicyError/DB retries, SQLite sync safety, shutdown cleanup."""

    def test_run_loop_recovers_transient_lease_policy_error_and_raises_permanent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-009-loop-worker")
        cmd._create_lease("default")

        closed: list[bool] = []
        attempts = 0
        monkeypatch.setattr(worker_module, "close_old_connections", lambda: closed.append(True))
        monkeypatch.setattr(cmd, "send_heartbeat", lambda: None)
        monkeypatch.setattr(cmd, "process_cancellations", lambda _q: 0)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda _t: None)

        def flaky_claim(_queues: Any, _concurrency: int) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise lease_policy.LeasePolicyError("lease_policy_busy")
            cmd.shutdown_requested = True
            return 0

        monkeypatch.setattr(cmd, "claim_and_process_tasks", flaky_claim)
        cmd.run_loop(queues=["default"], concurrency=1, heartbeat_interval=15.0)

        assert attempts == 2
        assert cmd.lease_ownership_lost is False
        assert len(closed) >= 2

        # Permanent LeasePolicyError must NOT be swallowed as transient.
        cmd2 = _make_command("r2-009-loop-permanent")
        cmd2._create_lease("default")
        monkeypatch.setattr(cmd2, "send_heartbeat", lambda: None)
        monkeypatch.setattr(
            cmd2,
            "claim_and_process_tasks",
            lambda _q, _c: (_ for _ in ()).throw(
                lease_policy.LeasePolicyError("lease_policy_mismatch")
            ),
        )
        with pytest.raises(lease_policy.LeasePolicyError, match="lease_policy_mismatch"):
            cmd2.run_loop(queues=["default"], concurrency=1, heartbeat_interval=15.0)

    def test_update_lease_heartbeat_retries_transient_db_error_within_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-009-hb-retry-worker")
        cmd._create_lease("default")
        clock = _FakeClock(start=cmd._last_successful_heartbeat_monotonic + 1.0)
        monkeypatch.setattr(worker_module, "time", clock)

        calls = 0
        original_lock = cmd._lock_authoritative_leases

        def flaky_lock(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("database is locked")
            if calls == 2:
                raise lease_policy.LeasePolicyError("lease_policy_busy")
            return original_lock(*args, **kwargs)

        monkeypatch.setattr(cmd, "_lock_authoritative_leases", flaky_lock)

        assert cmd._update_lease_heartbeat() is True
        assert calls == 3
        assert cmd.lease_ownership_lost is False
        assert cmd.shutdown_requested is False

    def test_sqlite_disables_sync_heartbeat_thread_and_thread_avoids_mutating_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-009-sync-sqlite-worker")
        cmd._create_lease("default")

        # Even with a file-like NAME on SQLite, background thread is disabled by default.
        monkeypatch.setitem(connection.settings_dict, "NAME", "/tmp/test_sqlite_file.sqlite3")
        assert cmd._can_spawn_sync_heartbeat_thread() is False

        # When explicitly enabled, background thread never mutates the shared task instance live.
        cmd._allow_sync_heartbeat_in_memory = True
        task = RayTaskExecution.objects.create(
            task_id="r2-009-sync-hb-race",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
            last_heartbeat_at=None,
        )
        hb_ts = datetime.now(UTC)
        seen_targets: list[Any] = []
        hb_calls = 0

        def fake_update_hb() -> bool:
            nonlocal hb_calls
            hb_calls += 1
            # First tick simulates transient failure without lease loss; thread must continue!
            return hb_calls > 1

        def fake_mark(target: Any, **_kwargs: Any) -> None:
            seen_targets.append(target)
            target.last_heartbeat_at = hb_ts

        monkeypatch.setattr(
            worker_module, "get_heartbeat_interval", lambda: timedelta(seconds=0.05)
        )
        monkeypatch.setattr(cmd, "_update_lease_heartbeat", fake_update_hb)
        monkeypatch.setattr(cmd, "_mark_task_monitor_heartbeat", fake_mark)
        monkeypatch.setattr(worker_module, "close_old_connections", lambda: None)

        with cmd._sync_execution_heartbeat(task):
            real_time.sleep(0.65)
            # Shared ORM object must not be mutated from the background thread while executing!
            assert task.last_heartbeat_at is None

        assert len(seen_targets) >= 1
        assert all(target is not task for target in seen_targets)
        assert task.last_heartbeat_at == hb_ts

    def test_shutdown_closes_old_db_connections_before_handoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-009-shutdown-close")
        cmd._create_lease("default")
        events: list[str] = []

        monkeypatch.setattr(
            worker_module, "close_old_connections", lambda: events.append("close_old")
        )
        monkeypatch.setattr(cmd, "_prepare_shutdown_handoff", lambda: events.append("handoff"))

        cmd.shutdown()
        assert events[0] == "close_old"
        assert "handoff" in events


@pytest.mark.django_db
class TestR2010RayCoreCompletionTransientReturnFalse:
    """R2-010: Dropped DB sockets & transient LeasePolicyError do NOT ack Ray Core completions."""

    @pytest.mark.parametrize(
        "raised_error",
        [
            InterfaceError("connection already closed"),
            lease_policy.LeasePolicyError("lease_policy_busy"),
        ],
    )
    def test_apply_ray_core_completion_returns_false_and_skips_ack(
        self, monkeypatch: pytest.MonkeyPatch, raised_error: Exception
    ) -> None:
        cmd = _make_command("r2-010-ack-guard-worker")
        cmd.execution_mode = "local"
        cmd._create_lease("default")

        task = RayTaskExecution.objects.create(
            task_id="r2-010-task-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=1,
        )
        handle = RayCoreHandle(
            task_pk=int(task.pk),
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="add",
            attempt_number=1,
            execution_generation=1,
        )
        completion = RayCoreCompletion(
            task_pk=int(task.pk),
            attempt_number=1,
            execution_generation=1,
            result_json=encode_execution_completion(
                ExecutionCompletion(
                    identity=ExecutionIdentity(
                        task_execution_pk=int(task.pk),
                        task_id=task.task_id,
                        attempt_number=1,
                        execution_generation=1,
                    ),
                    execution_protocol_version=int(task.execution_protocol_version),
                    executor_django_ray_version=worker_module.django_ray_version,
                    success=True,
                    result=3,
                    result_reference=None,
                    error=None,
                    traceback=None,
                    exception_type=None,
                    retryable=None,
                )
            ),
            handle=handle,
        )

        acked: list[RayCoreHandle] = []

        class PeekAckRunner:
            pending_count = 1
            pending_task_handles = (handle,)

            def peek_completed(self, _handles: Any) -> list[RayCoreCompletion]:
                return [completion]

            def acknowledge_completed(self, h: RayCoreHandle) -> None:
                acked.append(h)

        cmd.ray_core_runner = cast(Any, PeekAckRunner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(cmd, "_partition_ray_core_handles", lambda _r, h, **_kw: h)
        monkeypatch.setattr(
            cmd,
            "_authoritative_task_owner",
            lambda *_a, **_kw: (_ for _ in ()).throw(raised_error),
        )

        processed = cmd.poll_ray_core_tasks()
        assert processed == 0
        assert acked == []


@pytest.mark.django_db
class TestR2013DrainWaitsForActiveQueueWork:
    """R2-013: --drain must NOT exit 0 while DB still has active work for configured queues."""

    @pytest.mark.parametrize(
        "active_state",
        [TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING],
    )
    def test_drain_times_out_nonzero_when_queue_still_has_active_db_work(
        self, monkeypatch: pytest.MonkeyPatch, active_state: str
    ) -> None:
        clock = _FakeClock()
        monkeypatch.setattr(worker_module, "time", clock)

        RayTaskExecution.objects.create(
            task_id=f"r2-013-active-{active_state}",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=active_state,
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        cmd = _make_command(f"r2-013-timeout-{active_state}")
        cmd._create_lease("default")
        cmd.drain_mode = True
        cmd.drain_timeout_seconds = 1.0
        monkeypatch.setattr(cmd, "process_cancellations", lambda _q: 0)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", lambda seconds: clock.sleep(seconds))

        cmd.run_loop(queues=["default"], concurrency=2, heartbeat_interval=15.0)

        assert cmd.drain_timed_out is True
        assert cmd.shutdown_exit_code == 1
        assert "Drain timed out" in cmd.stdout.getvalue()

    def test_drain_waits_for_peer_db_work_then_exits_zero_once_drained(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _FakeClock()
        monkeypatch.setattr(worker_module, "time", clock)

        peer_task = RayTaskExecution.objects.create(
            task_id="r2-013-peer-running",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="peer-worker",
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        cmd = _make_command("r2-013-quiesce-worker")
        cmd._create_lease("default")
        cmd.drain_mode = True
        cmd.drain_timeout_seconds = 30.0
        ticks = 0

        def advance_and_finish_peer(seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            clock.sleep(seconds)
            if ticks == 2:
                RayTaskExecution.objects.filter(pk=peer_task.pk).update(state=TaskState.SUCCEEDED)

        monkeypatch.setattr(cmd, "process_cancellations", lambda _q: 0)
        monkeypatch.setattr(cmd, "reconcile_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "detect_stuck_tasks", lambda _q: 0)
        monkeypatch.setattr(cmd, "cleanup_expired_leases", lambda: 0)
        monkeypatch.setattr(cmd, "_wait_for_poll_deadline", advance_and_finish_peer)

        cmd.run_loop(queues=["default"], concurrency=2, heartbeat_interval=15.0)

        assert ticks == 2
        assert cmd.drain_timed_out is False
        assert cmd.shutdown_exit_code == 0
        assert "Drain complete" in cmd.stdout.getvalue()


class TestR2014SyncProtocolAndFencedReservationRelease:
    """R2-014: SyncRunner strict protocol identity & live lease fence on reservation release."""

    def test_sync_runner_passes_strict_completion_identity_and_protocol_version(self) -> None:
        captured: list[dict[str, Any]] = []

        def fake_execute(**kwargs: Any) -> str:
            captured.append(kwargs)
            return json.dumps({"success": True, "result": 7})

        fake_env = SimpleNamespace(
            profile="default",
            digest="deadbeef",
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
        )
        task = SimpleNamespace(
            pk=42,
            task_id="sync-strict-42",
            callable_path="myapp.tasks.compute",
            args_json="[]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=3,
            execution_protocol_version=EXECUTION_PROTOCOL_VERSION,
            input_reference=None,
        )

        runner.submit_durable(cast(Any, task))

        assert len(captured) == 1
        call = captured[0]
        assert call["_strict_execution_request"] is True
        assert call["_execution_protocol_version"] == EXECUTION_PROTOCOL_VERSION
        assert call["_completion_identity"] == ExecutionIdentity(
            task_execution_pk=42,
            task_id="sync-strict-42",
            attempt_number=2,
            execution_generation=3,
        )

    @pytest.mark.django_db
    def test_reservation_release_is_fenced_by_live_lease_authority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-014-fenced-release-worker")
        cmd.execution_mode = "ray"
        cmd._create_lease("default")
        task = RayTaskExecution.objects.create(
            task_id="r2-014-release-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_r2014",
            ray_address="http://127.0.0.1:8265",
            args_json="[]",
            kwargs_json="{}",
        )
        reserved = SubmissionHandle(
            ray_job_id="raysubmit_r2014",
            ray_address="http://127.0.0.1:8265",
            submitted_at=datetime.now(UTC),
        )

        # Invalidate this worker's lease in DB while task.claimed_by_worker still matches textually!
        assert cmd.lease_identity is not None
        TaskWorkerLease.objects.filter(**cmd.lease_identity.database_filters()).update(
            is_active=False
        )

        release_called: list[bool] = []
        monkeypatch.setattr(
            "django_ray.ray_job_request_storage.release_ray_job_request_reservation",
            lambda *_a, **_kw: release_called.append(True) or True,
        )

        released = dispatcher.fenced_release_ray_job_request_reservation(
            cmd, task, reserved, expected_reference=None
        )
        assert released is False
        assert release_called == []
        assert cmd.lease_ownership_lost is True


class TestR2020TrustedRayAddressRegistration:
    """R2-020: Worker registers --cluster / configured Ray address as trusted on startup."""

    def test_register_trusted_worker_ray_addresses_and_reject_invalid(self) -> None:
        ray_job_module.clear_registered_trusted_ray_addresses()
        try:
            cmd = _make_command("r2-020-trust-worker")
            cmd.cluster_address = "http://ray-head.internal:8265"
            dispatcher.register_trusted_worker_ray_addresses(
                cmd, {"RAY_ADDRESS": "ray://ray-alt.internal:10001"}
            )
            trusted = ray_job_module.get_trusted_ray_addresses()
            expected_addresses = {
                "http://ray-head.internal:8265",
                "ray://ray-alt.internal:10001",
            }
            assert expected_addresses.issubset(trusted)

            bad_cmd = _make_command("r2-020-bad-cluster")
            bad_cmd.cluster_address = "http://user:secret@ray-head:8265"
            with pytest.raises(CommandError, match="Invalid Ray cluster address"):
                dispatcher.register_trusted_worker_ray_addresses(bad_cmd, {})
        finally:
            ray_job_module.clear_registered_trusted_ray_addresses()


@pytest.mark.django_db(transaction=True)
class TestR2026ToR2038PerfDefaultsTimeoutsConfig:
    """R2-026, R2-028, R2-029, R2-038: Hot-path query reduction, defaults, timeouts, config."""

    def test_r2026_hot_path_heartbeat_savepoint_expiry_prefetch_and_batch_hb(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-026-perf-worker")
        cmd.execution_mode = "sync"
        cmd._create_lease("default")

        # 1. process_task skips redundant _update_lease_heartbeat when heartbeat is fresh.
        hb_calls: list[bool] = []
        monkeypatch.setattr(cmd, "_update_lease_heartbeat", lambda: hb_calls.append(True) or True)
        monkeypatch.setattr(cmd, "execute_task_sync", lambda _t: None)
        monkeypatch.setattr(
            worker_module,
            "runtime_env_for_execution",
            lambda _t: SimpleNamespace(profile="default", digest="d"),
        )
        t1 = RayTaskExecution.objects.create(
            task_id="r2-026-hb-1",
            callable_path="tests.fake",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            args_json="[]",
            kwargs_json="{}",
        )
        cmd.process_task(t1)
        assert hb_calls == []

        # 2. store_and_succeed_task(_already_locked=True) skips extra SAVEPOINT & SELECT FOR UPDATE.
        succeed_calls: list[Any] = []
        monkeypatch.setattr(
            worker_module,
            "succeed_task",
            lambda current, **_kw: succeed_calls.append(current.pk) or True,
        )
        with transaction.atomic(), CaptureQueriesContext(connection) as captured:
            assert (
                cmd._store_and_succeed_task(
                    t1,
                    {"ok": True},
                    prepared_result_data='{"ok": true}',
                    _result_already_stored=True,
                    _already_locked=True,
                    expected_claimed_by_worker=cmd.worker_id,
                    expected_attempt_number=t1.attempt_number,
                    expected_execution_generation=t1.execution_generation,
                )
                is True
            )
        sqls = [q["sql"].upper() for q in captured]
        assert succeed_calls == [t1.pk]
        assert not any("SAVEPOINT" in s for s in sqls)
        assert not any("FOR UPDATE" in s for s in sqls)
        assert len(sqls) == 0

        # 3. claim_and_process_tasks throttles expire_queued_tasks across rapid back-to-back ticks.
        expire_calls: list[int] = []
        monkeypatch.setattr(
            worker_module,
            "expire_queued_tasks",
            lambda *_a, **_kw: expire_calls.append(1) or (),
        )
        cmd.expiry_sweep_interval = 10.0
        cmd.claim_and_process_tasks(["default"], concurrency=1)
        cmd.claim_and_process_tasks(["default"], concurrency=1)
        assert len(expire_calls) == 1

        # 4. detect_stuck_tasks prefetches a page of candidates in 1 batched SELECT instead of N.
        now = datetime.now(UTC)
        for i in range(6):
            RayTaskExecution.objects.create(
                task_id=f"r2-026-maint-{i}",
                callable_path="tests.fake",
                queue_name="default",
                state=TaskState.RUNNING,
                claimed_by_worker=cmd.worker_id,
                started_at=now,
                last_heartbeat_at=now,
                args_json="[]",
                kwargs_json="{}",
            )
        with CaptureQueriesContext(connection) as maint_captured:
            cmd.detect_stuck_tasks(["default"])
        task_selects = [
            q["sql"]
            for q in maint_captured
            if q["sql"].lstrip().upper().startswith("SELECT")
            and RayTaskExecution._meta.db_table in q["sql"]
        ]
        # 2 MaintenanceScan page queries + 1 batched IN prefetch = 3 SELECT queries
        # (instead of 2 + 6 = 8 unbatched per-candidate snapshot SELECT queries).
        assert len(task_selects) <= 3

        # 5. reconcile_tasks batches multiple Ray Job heartbeats into a single UPDATE.
        cmd.execution_mode = "ray"
        cmd.sync_mode = False
        job_tasks = []
        for i in range(4):
            jt = RayTaskExecution.objects.create(
                task_id=f"r2-026-job-{i}",
                callable_path="tests.fake",
                queue_name="default",
                state=TaskState.RUNNING,
                claimed_by_worker=cmd.worker_id,
                ray_job_id=f"raysubmit_batch_{i}",
                ray_address="http://127.0.0.1:8265",
                started_at=now,
                last_heartbeat_at=now - timedelta(seconds=30),
                args_json="[]",
                kwargs_json="{}",
            )
            cmd.active_tasks[jt.pk] = str(jt.ray_job_id)
            cmd.active_task_identities[jt.pk] = (jt.attempt_number, jt.execution_generation)
            job_tasks.append(jt)

        class RunningJobRunner:
            def get_status(self, handle: SubmissionHandle) -> JobInfo:
                return JobInfo(job_id=handle.ray_job_id, status=JobStatus.RUNNING)

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", RunningJobRunner)
        with CaptureQueriesContext(connection) as rec_captured:
            cmd.reconcile_tasks(["default"])
        hb_updates = [
            q["sql"]
            for q in rec_captured
            if q["sql"].lstrip().upper().startswith("UPDATE")
            and RayTaskExecution._meta.db_table in q["sql"]
        ]
        assert len(hb_updates) == 1

    def test_r2028_defaults_and_1024_char_queue_readiness(self) -> None:
        assert claimer.get_default_execution_mode({}) == ("local", None)
        assert claimer.requested_worker_family(worker_module.Command, {}) == "ray_core"

        cmd = _make_command("r2-028-defaults-worker")
        assert cmd.poll_max_interval == 1.0

        # TaskWorkerLease accepts 1024-char multi-queue lists, while single-queue
        # readiness coordinates enforce RayTaskExecution.queue_name's 100-char limit.
        long_multi_queue = "q" * 1024
        cmd._create_lease(long_multi_queue)
        assert cmd.lease_identity is not None

        single_queue = "q" * 100
        cmd_single = _make_command("r2-028-single-queue-worker")
        cmd_single._create_lease(single_queue)
        assert cmd_single.lease_identity is not None
        report_ok = worker_readiness.check_worker_lease_readiness(
            queue=single_queue,
            hostname=cmd_single.lease_identity.hostname,
        )
        assert report_ok.status == "ready"

        report_too_long = worker_readiness.check_worker_lease_readiness(
            queue="q" * 101,
            hostname=cmd_single.lease_identity.hostname,
        )
        assert report_too_long.status == "error"
        assert report_too_long.reason == "invalid_coordinates"

    def test_r2029_ray_init_shutdown_and_resources_are_timeout_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocker = threading.Event()

        def hang(*_args: Any, **_kwargs: Any) -> Any:
            blocker.wait(timeout=5.0)
            return {}

        cmd = _make_command("r2-029-timeout-worker")
        cmd._ray_init_timeout = 0.05
        cmd._ray_shutdown_timeout = 0.05
        monkeypatch.setattr(cmd, "_ray_health_check_timeout_seconds", lambda: 0.05)

        try:
            # Hung ray.shutdown() during cluster connect times out cleanly.
            monkeypatch.setitem(
                sys.modules,
                "ray",
                SimpleNamespace(
                    is_initialized=lambda: True, shutdown=hang, init=lambda **_kw: None
                ),
            )
            with pytest.raises(TimeoutError, match="Ray shutdown timed out"):
                cmd._init_cluster_ray("http://127.0.0.1:8265")

            # Hung ray.init() during cluster connect times out cleanly.
            monkeypatch.setitem(
                sys.modules,
                "ray",
                SimpleNamespace(is_initialized=lambda: False, shutdown=lambda: None, init=hang),
            )
            with pytest.raises(TimeoutError, match="Ray init timed out"):
                cmd._init_cluster_ray("http://127.0.0.1:8265")

            # Hung ray.cluster_resources() after connect times out cleanly.
            monkeypatch.setitem(
                sys.modules,
                "ray",
                SimpleNamespace(
                    is_initialized=lambda: False,
                    shutdown=lambda: None,
                    init=lambda **_kw: None,
                    cluster_resources=hang,
                ),
            )
            with pytest.raises(TimeoutError, match="cluster_resources timed out"):
                cmd._init_cluster_ray("http://127.0.0.1:8265")
        finally:
            blocker.set()
            ray_job_module.clear_registered_trusted_ray_addresses()

    def test_r2038_cancellation_interval_configurable_and_validated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = _make_command("r2-038-cancel-interval")
        assert cmd.cancellation_interval == 5.0

        monkeypatch.setattr(cmd, "setup_signal_handlers", lambda: None)
        monkeypatch.setattr(cmd, "run_loop", lambda **_kw: None)
        monkeypatch.setattr(
            worker_module,
            "get_settings",
            lambda: {
                "DEFAULT_CONCURRENCY": 2,
                "TASK_MONITOR_HEARTBEAT_SECONDS": 15,
                "WORKER_POLL_INTERVAL_SECONDS": 0.1,
                "WORKER_POLL_MAX_INTERVAL_SECONDS": 1.0,
                "WORKER_CANCELLATION_INTERVAL_SECONDS": 2.5,
            },
        )
        cmd.handle(
            queue="default",
            queues=None,
            all_queues=False,
            concurrency=None,
            sync=True,
            local=False,
            cluster=None,
            ray_job=False,
            drain=False,
            drain_timeout_seconds=None,
        )
        assert cmd.cancellation_interval == 2.5

        bad_cmd = _make_command("r2-038-bad-interval")
        monkeypatch.setattr(
            worker_module,
            "get_settings",
            lambda: {"WORKER_CANCELLATION_INTERVAL_SECONDS": -1.0},
        )
        with pytest.raises(CommandError, match="WORKER_CANCELLATION_INTERVAL_SECONDS"):
            bad_cmd.handle(
                queue="default",
                queues=None,
                all_queues=False,
                concurrency=1,
                sync=True,
                local=False,
                cluster=None,
                ray_job=False,
                drain=False,
                drain_timeout_seconds=None,
            )


@pytest.mark.django_db
class TestSyncRunnerCompleteContract:
    """Cover SyncRunner BaseRunner contract in the portable coverage lane."""

    def test_classify_sync_result_handles_malformed_nested_and_error_envelopes(self) -> None:
        from django_ray.runner.base import JobStatus
        from django_ray.runner.sync import _classify_sync_result

        assert _classify_sync_result("{not-json") == (
            JobStatus.FAILED,
            "Malformed synchronous completion envelope",
        )
        assert _classify_sync_result("[1, 2]") == (
            JobStatus.FAILED,
            "Unexpected synchronous completion payload",
        )
        assert _classify_sync_result('{"success": true}') == (JobStatus.SUCCEEDED, None)
        assert _classify_sync_result('{"completion": {"success": true}}') == (
            JobStatus.SUCCEEDED,
            None,
        )
        assert _classify_sync_result('{"success": false, "error": "boom"}') == (
            JobStatus.FAILED,
            "boom",
        )
        assert _classify_sync_result('{"success": false}') == (
            JobStatus.FAILED,
            "Task execution failed",
        )

    def test_sync_runner_submit_status_eviction_cancel_and_logs(self) -> None:
        from django_ray.runner.base import JobStatus, SubmissionHandle
        from django_ray.runner.cancellation import CancellationOutcomeStatus
        from django_ray.runner.sync import SyncRunner, SyncSubmissionHandle
        from django_ray.runtime.runtime_env import normalize_runtime_env

        captured: list[dict[str, Any]] = []

        def fake_execute(**kwargs: Any) -> str:
            captured.append(kwargs)
            return json.dumps({"success": True, "result": "ok"})

        empty_env = normalize_runtime_env({})
        runner = SyncRunner(
            execute_fn=fake_execute,
            runtime_env_resolver=lambda _exec: empty_env,
            max_tracked_handles=2,
        )
        exec_inline = RayTaskExecution.objects.create(
            task_id="sync-contract-inline",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=1,
        )
        h1 = runner.submit(exec_inline, "testproject.tasks.add_numbers", (3, 4), {"k": "v"})
        assert isinstance(h1, SyncSubmissionHandle)
        assert h1.status == JobStatus.SUCCEEDED
        assert captured[-1]["serialized_args"] == "[3, 4]"
        assert runner.get_status(h1).status == JobStatus.SUCCEEDED

        # Lookup via plain SubmissionHandle uses bounded _completed OrderedDict.
        plain_h1 = SubmissionHandle(
            ray_job_id=h1.ray_job_id,
            ray_address=h1.ray_address,
            submitted_at=h1.submitted_at,
        )
        assert runner.get_status(plain_h1).status == JobStatus.SUCCEEDED

        # External input_reference uses stored opaque args_json/kwargs_json without re-serializing.
        exec_ref = RayTaskExecution.objects.create(
            task_id="sync-contract-ref",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
            input_reference="inputfs://sha256/" + ("a" * 64) + "?rel=aa/aa/test.json&bytes=12",
            attempt_number=1,
            execution_generation=2,
        )
        h2 = runner.submit(exec_ref, "testproject.tasks.add_numbers", (99,), {})
        assert captured[-1]["input_reference"] == exec_ref.input_reference
        assert captured[-1]["serialized_args"] == "[]"

        # Third execution evicts oldest handle from max_tracked_handles=2.
        adhoc = SimpleNamespace(
            pk=None,
            task_id=None,
            callable_path="testproject.tasks.add_numbers",
            args_json="[]",
            kwargs_json="{}",
            input_reference=None,
            attempt_number=1,
            execution_generation=0,
            runtime_env_profile=None,
            runtime_env_json=None,
        )
        h3 = runner.submit_durable(adhoc)  # type: ignore[arg-type]
        assert h3.ray_job_id == "sync:adhoc:1:0"
        assert runner.get_status(plain_h1).status == JobStatus.UNKNOWN
        assert (
            runner.get_status(
                SubmissionHandle(
                    ray_job_id=h2.ray_job_id,
                    ray_address="sync://local",
                    submitted_at=h2.submitted_at,
                )
            ).status
            == JobStatus.SUCCEEDED
        )
        assert runner.cancel(h3) is False
        outcome = runner.cancel_with_status(h3)
        assert outcome.status == CancellationOutcomeStatus.NOT_APPLICABLE
        assert runner.get_logs(h3) is None


@pytest.mark.django_db
class TestWorkerShutdownAndMaintenanceBranches:
    """Cover edge branches in worker/shutdown.py and worker/maintenance.py."""

    def test_shutdown_unpack_timeout_legacy_cancel_and_error_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import threading

        from django_ray.worker import shutdown as shutdown_mod

        assert shutdown_mod._unpack_drained_ray_core_completions([]) == ((), None)

        # _shutdown_ray_with_timeout: not initialized -> False; timeout -> TimeoutError
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: False))
        assert shutdown_mod._shutdown_ray_with_timeout(0.5) is False

        hang_ev = threading.Event()
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(is_initialized=lambda: True, shutdown=lambda: hang_ev.wait(5.0)),
        )
        try:
            with pytest.raises(TimeoutError, match="Ray shutdown timed out"):
                shutdown_mod._shutdown_ray_with_timeout(0.05)
        finally:
            hang_ev.set()

        # prepare_shutdown_handoff: drain_ready exception + legacy cancel_pending fallback
        cmd = _make_command("shutdown-branch-worker")
        cmd._create_lease("default")
        cmd.execution_mode = "local"
        task = RayTaskExecution.objects.create(
            task_id="shutdown-legacy-cancel",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            attempt_number=1,
            execution_generation=1,
            args_json="[]",
            kwargs_json="{}",
        )
        handle = SimpleNamespace(
            task_pk=task.pk,
            attempt_number=1,
            execution_generation=1,
        )
        legacy_cancelled: list[Any] = []

        class _LegacyRunner:
            pending_task_handles = (handle,)

            def drain_ready_for_shutdown(self) -> Any:
                raise RuntimeError("drain boom")

            def cancel_pending(self, h: Any) -> bool:
                legacy_cancelled.append(h)
                return True

        cmd.ray_core_runner = _LegacyRunner()
        shutdown_mod.prepare_shutdown_handoff(cmd)
        assert legacy_cancelled == [handle]
        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED

        # prepare_shutdown_handoff in ray mode when active task row no longer exists
        cmd_ray = _make_command("shutdown-ray-missing")
        cmd_ray._create_lease("default")
        cmd_ray.execution_mode = "ray"
        cmd_ray.active_tasks[999999] = "raysubmit_missing"
        cmd_ray.active_task_identities[999999] = (1, 1)
        shutdown_mod.prepare_shutdown_handoff(cmd_ray)
        assert 999999 not in cmd_ray.active_tasks

        # shutdown(): handoff exception, sync_runner.prepare_shutdown exception, lease fence miss,
        # and Ray disconnect failure all set shutdown_exit_code = 1 without crashing
        cmd_err = _make_command("shutdown-errors-worker")
        cmd_err._create_lease("default")
        cmd_err.execution_mode = "local"
        cmd_err.sync_runner = SimpleNamespace(
            prepare_shutdown=lambda: (_ for _ in ()).throw(RuntimeError("sync cleanup err"))
        )
        monkeypatch.setattr(
            cmd_err,
            "_prepare_shutdown_handoff",
            lambda: (_ for _ in ()).throw(RuntimeError("handoff boom")),
        )
        monkeypatch.setattr(
            "django_ray.runner.leasing.release_lease",
            lambda _ident: False,
        )
        monkeypatch.setattr(
            shutdown_mod,
            "_shutdown_ray_with_timeout",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("ray disconnect boom")),
        )
        shutdown_mod.shutdown(cmd_err)
        assert cmd_err.shutdown_exit_code == 1
        assert "shut down with errors" in cmd_err.stdout.getvalue()

    def test_maintenance_prefetch_fallback_snapshot_mismatch_and_retry_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError
        from django_ray.worker import maintenance as maint_mod

        cmd = _make_command("maint-edge-worker")
        cmd._create_lease("default")
        t1 = RayTaskExecution.objects.create(
            task_id="maint-edge-1",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="dead-worker-1",
            attempt_number=1,
            execution_generation=1,
            args_json="[]",
            kwargs_json="{}",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
            started_at=datetime.now(UTC) - timedelta(hours=2),
        )
        t2 = RayTaskExecution.objects.create(
            task_id="maint-edge-2",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker="dead-worker-1",
            attempt_number=1,
            execution_generation=1,
            args_json="[]",
            kwargs_json="{}",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
            started_at=datetime.now(UTC) - timedelta(hours=2),
        )
        c1 = maint_mod.MaintenanceCandidate(
            pk=t1.pk,
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
            owner="dead-worker-1",
            owner_complete=True,
            identity_complete=True,
        )
        c2 = maint_mod.MaintenanceCandidate(
            pk=t2.pk,
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
            owner="dead-worker-1",
            owner_complete=True,
            identity_complete=True,
        )
        # _candidate_matches_snapshot mismatch
        assert (
            maint_mod._candidate_matches_snapshot(
                SimpleNamespace(
                    pk=t1.pk,
                    attempt_number=99,
                    execution_generation=1,
                    execution_protocol_version=1,
                ),
                c1,
            )
            is False
        )

        # _prefetch_maintenance_page exception returns None gracefully
        class _BrokenQs:
            def filter(self, **_kw: Any) -> _BrokenQs:
                return self

            def defer(self, *_a: Any) -> _BrokenQs:
                return self

            def __iter__(self) -> Any:
                raise RuntimeError("db error")

        assert (
            maint_mod._prefetch_maintenance_page(cmd, "running", _BrokenQs(), [c1, c2], set())
            is None
        )

        # maintenance_snapshot batch mismatch returns None
        cmd._maintenance_scans["running"] = maint_mod.MaintenanceScan()
        cmd._maintenance_snapshot_batch = (
            "running",
            {
                t1.pk: SimpleNamespace(
                    pk=t1.pk,
                    attempt_number=99,
                    execution_generation=1,
                    execution_protocol_version=1,
                )
            },
        )
        assert (
            maint_mod.maintenance_snapshot(cmd, "running", RayTaskExecution.objects.all(), c1)
            is None
        )
        cmd._maintenance_snapshot_batch = None

        # detect_stuck_tasks handles RuntimeEnvSnapshotError when scheduling retry
        monkeypatch.setattr(
            worker_module,
            "should_retry",
            lambda _t, **_kw: SimpleNamespace(should_retry=True, next_attempt_at=datetime.now(UTC)),
        )
        monkeypatch.setattr(
            worker_module,
            "retry_task",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeEnvSnapshotError("snapshot drift")),
        )
        count = maint_mod.detect_stuck_tasks(cmd, queues=["default"])
        assert count >= 1
        assert "Automatic retry blocked" in cmd.stdout.getvalue()


@pytest.mark.django_db
class TestWorkerClaimerAndReconcilerEdgeBranches:
    """Cover lease fencing, queue configuration, and completion edge branches."""

    def test_claimer_mode_config_lease_fencing_and_preflight_edges(
        self, monkeypatch: pytest.MonkeyPatch, settings: Any
    ) -> None:
        from django.core.exceptions import ImproperlyConfigured

        from django_ray.lease_policy import LeasePolicyError
        from django_ray.worker import claimer as claimer_mod

        # validate_execution_mode_configuration in ray mode wraps storage errors in CommandError
        cmd = _make_command("claimer-edge-worker")
        cmd.execution_mode = "ray"
        monkeypatch.setattr(
            "django_ray.conf.settings.validate_ray_job_request_storage_settings",
            lambda _s: (_ for _ in ()).throw(ImproperlyConfigured("bad ray job storage")),
        )
        with pytest.raises(CommandError, match="Ray Job request storage configuration is invalid"):
            claimer_mod.validate_execution_mode_configuration(cmd, {})

        # requested_worker_family falls back to ray_job when RUNNER != ray_core
        monkeypatch.setattr(worker_module, "get_settings", lambda: {"RUNNER": "ray_job"})
        assert claimer_mod.requested_worker_family(worker_module.Command, {}) == "ray_job"
        monkeypatch.undo()

        # load_ray_backend_queue_configuration validation & set sorting
        for bad_tasks, match_text in [
            ("not-a-mapping", "must be a mapping"),
            ({"bad": "not-dict"}, "map names to backend settings"),
            ({"default": {}}, "must define BACKEND"),
            ({"default": {"BACKEND": "no.such.Module"}}, "Cannot import TASKS backend"),
            (
                {"default": {"BACKEND": "django_ray.conf.settings.get_settings"}},
                "does not resolve to a class",
            ),
            (
                {"default": {"BACKEND": "django_ray.backends.RayTaskBackend", "OPTIONS": "bad"}},
                "OPTIONS must be a mapping",
            ),
            (
                {
                    "default": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "OPTIONS": {"RAY_JOB_ONLY": "yes"},
                    }
                },
                "must be a boolean",
            ),
            (
                {
                    "default": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "OPTIONS": {"RAY_ADDRESS": "   "},
                    }
                },
                "non-empty string",
            ),
            (
                {
                    "default": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "QUEUES": "not-a-list",
                    }
                },
                "collection of queue names",
            ),
            (
                {
                    "default": {
                        "BACKEND": "django_ray.backends.RayTaskBackend",
                        "QUEUES": [""],
                    }
                },
                "non-empty strings",
            ),
        ]:
            cmd._ray_backend_queue_configuration = None
            settings.TASKS = bad_tasks
            with pytest.raises(CommandError, match=match_text):
                claimer_mod.load_ray_backend_queue_configuration(cmd)

        cmd._ray_backend_queue_configuration = None
        settings.TASKS = {
            "default": {
                "BACKEND": "django_ray.backends.RayTaskBackend",
                "QUEUES": {"zeta", "alpha"},
            }
        }
        cfg = claimer_mod.load_ray_backend_queue_configuration(cmd)
        assert cfg.queues == ("alpha", "zeta")

        # recreate_lease: no identity -> False; queue update; exception -> shutdown
        no_lease_cmd = _make_command("claimer-no-lease")
        assert claimer_mod.recreate_lease(no_lease_cmd) is False
        assert no_lease_cmd.lease_ownership_lost is True
        assert claimer_mod.preliminary_takeover_protocol_range(no_lease_cmd) is None
        assert (
            claimer_mod.lock_authoritative_leases(
                no_lease_cmd, source_worker_id=None, allow_takeover=False
            )
            is None
        )

        live_cmd = _make_command("claimer-live-lease")
        live_cmd._create_lease("alpha")
        assert claimer_mod.recreate_lease(live_cmd, queue_name="beta") is True
        assert live_cmd.lease is not None and live_cmd.lease.queue_name == "beta"

        # resolve_active_lease_policy mismatch & preflight_own_lease_active edge paths
        live_cmd._lease_policy_seconds = 999999
        live_cmd._lease_policy_revision = 1
        with pytest.raises(LeasePolicyError, match="lease_policy_mismatch"):
            claimer_mod.resolve_active_lease_policy(live_cmd)
        assert claimer_mod.preflight_own_lease_active(live_cmd, mark_lost=False) is False
        assert claimer_mod.preflight_own_lease_active(live_cmd, mark_lost=True) is False
        assert live_cmd.lease_ownership_lost is True
        assert claimer_mod.preflight_own_lease_active(live_cmd, mark_lost=True) is False

        # update_lease_heartbeat non-transient failure triggers shutdown & returns False
        hb_cmd = _make_command("claimer-hb-err")
        hb_cmd._create_lease("default")
        monkeypatch.setattr(
            hb_cmd,
            "_lock_authoritative_leases",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("fatal lease err")),
        )
        assert claimer_mod.update_lease_heartbeat(hb_cmd) is False
        assert hb_cmd.lease_ownership_lost is True

        # lock_authoritative_leases: allow_takeover=False with peer worker -> None;
        # live peer lease blocks takeover -> None
        peer_cmd = _make_command("claimer-peer-live")
        peer_cmd._create_lease("default")
        taker_cmd = _make_command("claimer-taker")
        taker_cmd._create_lease("default")
        assert (
            claimer_mod.lock_authoritative_leases(
                taker_cmd,
                source_worker_id=peer_cmd.worker_id,
                allow_takeover=False,
            )
            is None
        )
        assert (
            claimer_mod.lock_authoritative_leases(
                taker_cmd,
                source_worker_id=peer_cmd.worker_id,
                allow_takeover=True,
            )
            is None
        )

        # claim_and_process_tasks returns 0 when shutdown_requested=True, and resolves hb_interval
        taker_cmd._lease_heartbeat_interval_seconds = 0.0
        assert claimer_mod.claim_and_process_tasks(taker_cmd, ["default"], concurrency=1) == 0
        taker_cmd.shutdown_requested = True
        assert claimer_mod.claim_and_process_tasks(taker_cmd, ["default"], concurrency=1) == 0

    def test_reconciler_heartbeat_invalid_result_ref_and_completion_edges(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from django_ray.execution_protocol import SUPPORTED_EXECUTION_PROTOCOL_RANGE
        from django_ray.worker import reconciler as reconciler_mod

        cmd = _make_command("reconciler-edge-worker")
        cmd._create_lease("default")
        now = datetime.now(UTC)
        task = RayTaskExecution.objects.create(
            task_id="rec-edge-1",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            claimed_by_worker=cmd.worker_id,
            ray_job_id="raysubmit_hb_1",
            attempt_number=2,
            execution_generation=3,
            args_json="[]",
            kwargs_json="{}",
        )

        # mark_task_monitor_heartbeat updates DB and mutates task.last_heartbeat_at
        reconciler_mod.mark_task_monitor_heartbeat(
            cmd,
            task,
            now=now,
            ray_job_id="raysubmit_hb_1",
            expected_worker_id=cmd.worker_id,
            attempt_number=2,
            execution_generation=3,
        )
        assert task.last_heartbeat_at == now

        # inspect_execution_completion rejects malformed result_reference on versioned & legacy payloads
        bad_versioned = encode_execution_completion(
            ExecutionCompletion(
                identity=ExecutionIdentity(
                    task_execution_pk=int(task.pk),
                    task_id=task.task_id,
                    attempt_number=2,
                    execution_generation=3,
                ),
                execution_protocol_version=1,
                executor_django_ray_version=worker_module.django_ray_version,
                success=True,
                result=None,
                result_reference="not-a-valid-uri",
                error=None,
                traceback=None,
                exception_type=None,
                retryable=None,
            )
        )
        inspected = reconciler_mod.inspect_execution_completion(
            cmd,
            bad_versioned,
            task,
            supported_protocols=SUPPORTED_EXECUTION_PROTOCOL_RANGE,
        )
        assert inspected.decoded is None
        assert inspected.requires_nonretryable_disposition is True

        # apply_ray_core_completion: stale identity, cancelled, non-RUNNING, and non-transient error
        stale_comp = SimpleNamespace(
            task_pk=task.pk, attempt_number=1, execution_generation=3, result_json="{}"
        )
        assert reconciler_mod.apply_ray_core_completion(cmd, stale_comp) is True
        assert "Ignoring stale Ray Core result" in cmd.stdout.getvalue()

        RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.CANCELLED)
        same_comp = SimpleNamespace(
            task_pk=task.pk, attempt_number=2, execution_generation=3, result_json="{}"
        )
        assert reconciler_mod.apply_ray_core_completion(cmd, same_comp) is True

        RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.SUCCEEDED)
        assert reconciler_mod.apply_ray_core_completion(cmd, same_comp) is True

        RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.RUNNING)
        monkeypatch.setattr(
            cmd,
            "_inspect_execution_completion",
            lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("unexpected decode crash")),
        )
        assert reconciler_mod.apply_ray_core_completion(cmd, same_comp) is True
        assert "Error processing task" in cmd.stdout.getvalue()

        # retire_active_ray_job_tracking keeps mismatched identity intact
        cmd.active_tasks[task.pk] = "raysubmit_hb_1"
        cmd.active_task_identities[task.pk] = (2, 3)
        reconciler_mod.retire_active_ray_job_tracking(
            cmd, task.pk, ray_job_id="raysubmit_hb_1", identity=(1, 1)
        )
        assert task.pk in cmd.active_tasks

    def test_base_runner_submit_durable_and_prepare_shutdown_contract(self) -> None:
        """Cover BaseRunner.submit_durable inline adapter and validation in runner/base.py."""
        from django_ray.runner.base import BaseRunner, JobInfo, JobStatus, SubmissionHandle

        submitted: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        class _MinimalRunner(BaseRunner):
            def submit(
                self,
                task_execution: Any,
                callable_path: str,
                args: tuple[Any, ...],
                kwargs: dict[str, Any],
            ) -> SubmissionHandle:
                del task_execution
                submitted.append((callable_path, args, kwargs))
                return SubmissionHandle(
                    ray_job_id="custom-1",
                    ray_address="local",
                    submitted_at=datetime.now(UTC),
                )

            def get_status(self, handle: SubmissionHandle) -> JobInfo:
                return JobInfo(job_id=handle.ray_job_id, status=JobStatus.SUCCEEDED)

            def cancel(self, handle: SubmissionHandle) -> bool:
                del handle
                return False

            def get_logs(self, handle: SubmissionHandle) -> str | None:
                del handle
                return None

        runner = _MinimalRunner()
        ok_task = SimpleNamespace(
            callable_path="myapp.tasks.run",
            args_json='[1, "a"]',
            kwargs_json='{"flag": true}',
            input_reference=None,
        )
        handle = runner.submit_durable(cast(Any, ok_task))
        assert handle.ray_job_id == "custom-1"
        assert submitted == [("myapp.tasks.run", (1, "a"), {"flag": True})]
        assert runner.prepare_shutdown() is None

        ref_task = SimpleNamespace(
            callable_path="myapp.tasks.run",
            args_json="[]",
            kwargs_json="{}",
            input_reference="inputfs://sha256/" + ("a" * 64),
        )
        with pytest.raises(NotImplementedError, match="external input_reference"):
            runner.submit_durable(cast(Any, ref_task))

        bad_args_task = SimpleNamespace(
            callable_path="myapp.tasks.run",
            args_json='{"not": "a list"}',
            kwargs_json="{}",
            input_reference=None,
        )
        with pytest.raises(ValueError, match="must decode to list/dict"):
            runner.submit_durable(cast(Any, bad_args_task))

    def test_dispatcher_cluster_init_timeouts_local_env_and_resource_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover worker/dispatcher.py init, timeout, kwarg inspection, and error branches."""
        import os

        cmd = _make_command("disp-edge-worker")
        cmd.execution_mode = "cluster"
        cmd.cluster_address = None
        with pytest.raises(CommandError, match="requires a selected Ray address"):
            dispatcher.create_ray_core_runner(cmd)

        # Default timeout calculations (when _ray_init_timeout/_ray_shutdown_timeout not overridden)
        cmd._lease_policy_seconds = 30
        assert dispatcher._ray_init_timeout_seconds(cmd) == 10.0
        assert dispatcher._ray_shutdown_timeout_seconds(cmd) == 5.0

        # _callable_accepts_kwarg branches
        assert dispatcher._callable_accepts_kwarg(lambda **_kw: None, "foo") is True
        assert dispatcher._callable_accepts_kwarg(lambda foo=1: foo, "foo") is True
        assert dispatcher._callable_accepts_kwarg(lambda bar=1: bar, "foo") is False
        assert dispatcher._callable_accepts_kwarg(123, "foo") is False

        # initialize_ray_execution in cluster mode handles success and failure
        cmd.cluster_address = "http://127.0.0.1:8265"
        monkeypatch.setattr(cmd, "_init_cluster_ray", lambda _addr: None)
        monkeypatch.setattr(cmd, "_create_ray_core_runner", lambda: SimpleNamespace(ok=True))
        dispatcher.initialize_ray_execution(cmd)
        assert cmd.ray_core_runner is not None

        monkeypatch.setattr(
            cmd,
            "_init_cluster_ray",
            lambda _addr: (_ for _ in ()).throw(RuntimeError("cluster down")),
        )
        dispatcher.initialize_ray_execution(cmd)
        assert cmd.ray_core_runner is None
        assert "Initial cluster connection failed" in cmd.stdout.getvalue()

        # init_local_ray strips RAY_ADDRESS & RAY_RUNTIME_ENV_HOOK and calls ray.init
        init_calls: list[dict[str, Any]] = []
        monkeypatch.setenv("RAY_ADDRESS", "ray://stale:10001")
        monkeypatch.setenv("RAY_RUNTIME_ENV_HOOK", "hook:fn")
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(
                is_initialized=lambda: False,
                init=lambda **kw: init_calls.append(kw),
                cluster_resources=lambda: (_ for _ in ()).throw(RuntimeError("resources boom")),
            ),
        )
        dispatcher.init_local_ray(cmd)
        assert "RAY_ADDRESS" not in os.environ
        assert "RAY_RUNTIME_ENV_HOOK" not in os.environ
        assert len(init_calls) == 1

        # get_ray_cluster_resources_with_timeout re-raises worker error
        with pytest.raises(RuntimeError, match="resources boom"):
            dispatcher.get_ray_cluster_resources_with_timeout(1.0)

        # init_cluster_ray rejects untrusted address and logs resources on success
        try:
            with pytest.raises(CommandError, match="Invalid Ray cluster address"):
                dispatcher.init_cluster_ray(cmd, "http://user:pw@bad-host:8265")

            monkeypatch.setitem(
                sys.modules,
                "ray",
                SimpleNamespace(
                    is_initialized=lambda: False,
                    init=lambda **_kw: None,
                    cluster_resources=lambda: {"CPU": 4.0},
                ),
            )
            dispatcher.init_cluster_ray(cmd, "http://127.0.0.1:8265")
            assert "Cluster resources:" in cmd.stdout.getvalue()
        finally:
            ray_job_module.clear_registered_trusted_ray_addresses()

    @pytest.mark.django_db
    def test_store_and_succeed_task_row_lock_and_runtime_env_pg_binding_edges(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify store_and_succeed_task holds the row lock across storage and cover PG maintenance binding."""
        import time

        from django.db import connection

        from django_ray.execution_protocol import ExecutionProtocolRange
        from django_ray.management import runtime_env_maintenance as rem
        from django_ray.worker import dispatcher

        cmd = _make_command()
        task = RayTaskExecution.objects.create(
            task_id="store-lock-check-1",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
        )
        observed_in_atomic: list[bool] = []
        original_store_result = cmd._store_task_result

        def _spy_store(row: Any, val: Any) -> Any:
            observed_in_atomic.append(connection.in_atomic_block)
            return original_store_result(row, val)

        monkeypatch.setattr(cmd, "_store_task_result", _spy_store)
        assert dispatcher.store_and_succeed_task(cmd, task, {"ok": 1}) is True
        assert observed_in_atomic == [True]

        # Missing row (already SUCCEEDED) returns False cleanly.
        assert dispatcher.store_and_succeed_task(cmd, task, {"ok": 2}) is False

        # Prepared external result reference & unsupported protocol branches.
        task2 = RayTaskExecution.objects.create(
            task_id="store-lock-check-2",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
            execution_protocol_version=1,
        )
        assert (
            dispatcher.store_and_succeed_task(
                cmd,
                task2,
                {"ok": 3},
                supported_protocols=ExecutionProtocolRange(minimum=99, maximum=99),
            )
            is False
        )
        assert (
            dispatcher.store_and_succeed_task(
                cmd,
                task2,
                {"ok": 3},
                prepared_result_reference="resultfs://sha256/" + ("a" * 64),
            )
            is True
        )

        # runtime_env_maintenance._Driver PostgreSQL binding(), page() invalid PK, and _exec_tx_sql.
        executed_tx_sql: list[str] = []

        class _FakePgCursor:
            def __init__(self, row: Any = None, rows: list[Any] | None = None) -> None:
                self._row = row
                self._rows = rows or []
                self.rowcount = 1

            def execute(self, sql: str, params: Any = None) -> None:
                del params
                executed_tx_sql.append(sql)

            def fetchone(self) -> Any:
                return self._row

            def fetchall(self) -> list[Any]:
                return self._rows

            def close(self) -> None:
                return None

        class _FakePgDriverConn:
            def __init__(self, cursor_obj: _FakePgCursor, info_obj: Any) -> None:
                self._cursor = cursor_obj
                self.info = info_obj
                self.autocommit = False

            def cursor(self) -> _FakePgCursor:
                return self._cursor

        limits = rem.MaintenanceLimits(
            rows=10,
            total_bytes=1024 * 1024,
            snapshot_bytes=65536,
            seconds=10.0,
            details=10,
        )
        drv = rem._Database(
            {"engine": "postgresql", "name": "db"},
            rewrite=False,
            deadline=time.monotonic() + 30.0,
            limits=limits,
        )
        valid_info = SimpleNamespace(dbname="db", user="postgres", host="localhost", port=5432)
        drv.connection = _FakePgDriverConn(_FakePgCursor(row=("public", 16384)), valid_info)
        bound = drv.binding()
        assert bound["schema"] == "public"
        assert bound["relation"] == 16384
        assert drv.table == '"public"."django_ray_raytaskexecution"'

        drv._exec_tx_sql("BEGIN")
        assert "BEGIN" in executed_tx_sql

        # Missing pg_class row -> MaintenanceError
        drv.connection = _FakePgDriverConn(_FakePgCursor(row=None), valid_info)
        with pytest.raises(rem.MaintenanceError, match="unavailable"):
            drv.binding()

        # Invalid libpq info type -> MaintenanceError
        bad_info = SimpleNamespace(dbname=None, user="postgres", host="localhost", port=5432)
        drv.connection = _FakePgDriverConn(_FakePgCursor(row=("public", 16384)), bad_info)
        with pytest.raises(rem.MaintenanceError, match="Unsupported"):
            drv.binding()

        # Invalid PK returned by page() -> MaintenanceError
        drv.connection = _FakePgDriverConn(_FakePgCursor(rows=[(999,)]), valid_info)
        with pytest.raises(rem.MaintenanceError, match="Invalid RuntimeEnv maintenance row"):
            drv.page(0, 10)
