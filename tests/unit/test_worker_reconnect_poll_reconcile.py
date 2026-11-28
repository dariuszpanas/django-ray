"""Focused coverage tests for worker dispatch, reconnect helpers, and core reconcile paths."""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from django_ray.lifecycle import record_lost, retry_task
from django_ray.models import (
    CancellationStatus,
    RayTaskExecution,
    TaskAttempt,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_coordination import close_legacy_worker_admission
from django_ray.runner import (
    RayJobRequestPreparationError,
    RayJobRequestPreparationRejection,
    RayJobSubmissionUncertainError,
)
from django_ray.runner.base import SubmissionHandle
from django_ray.runner.ray_core import RayCoreCompletion, RayCoreHandle
from tests.unit._worker_reconnect_helpers import (
    RealCommandLeaseTestCase,
    _completion,
    _make_command,
    _pending_handle,
    _ray_job_handle,
    _rq2_ray_job_id,
    _versioned_completion_json,
)


class TestWorkerDispatchAndReconnectHelpers:
    """Non-DB tests for dispatch/reconnect helper branches."""

    def test_process_task_dispatches_to_ray_core_and_ray_job(self, monkeypatch) -> None:
        task = SimpleNamespace(
            pk=1,
            callable_path="testproject.tasks.add_numbers",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        )
        cmd = _make_command()
        events: list[str] = []

        monkeypatch.setattr(
            cmd,
            "_update_lease_heartbeat",
            lambda: events.append("heartbeat") or True,
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.time", lambda: 123.0
        )
        monkeypatch.setattr(cmd, "submit_task_to_ray_core", lambda _task: events.append("ray-core"))
        monkeypatch.setattr(cmd, "submit_task_to_ray", lambda _task: events.append("ray-job"))

        cmd.execution_mode = "local"
        cmd.process_task(task)

        cmd.execution_mode = "ray"
        cmd.process_task(task)

        assert "ray-core" in events
        assert "ray-job" in events

    def test_execute_task_sync_routes_entrypoint_exception_to_failure_handler(
        self, monkeypatch
    ) -> None:
        task = SimpleNamespace(
            pk=7,
            callable_path="testproject.tasks.add_numbers",
            args_json="[1,2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=0,
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        )
        cmd = _make_command()
        captured: list[dict[str, Any]] = []
        entrypoint_calls: list[dict[str, Any]] = []

        def fail_entrypoint(**kwargs: Any) -> str:
            entrypoint_calls.append(kwargs)
            raise RuntimeError("entrypoint crashed")

        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            fail_entrypoint,
        )
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda _task, **kwargs: captured.append(kwargs),
        )

        cmd.execute_task_sync(task)

        assert entrypoint_calls[0]["task_id"] is None
        assert captured
        assert captured[0]["error_message"] == "entrypoint crashed"
        assert captured[0]["exception_type"] == "RuntimeError"
        assert captured[0]["expected_attempt_number"] == 1
        assert captured[0]["expected_execution_generation"] == 0

    def test_execute_task_sync_forwards_durable_task_id_and_failure_fences(
        self, monkeypatch
    ) -> None:
        durable_task_id = "4aec29e7-cc95-4848-8f03-7b050ec8adea"
        task = SimpleNamespace(
            pk=8,
            task_id=durable_task_id,
            callable_path="testproject.tasks.add_numbers",
            args_json="[1,2]",
            kwargs_json="{}",
            attempt_number=3,
            execution_generation=7,
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        )
        cmd = _make_command(worker_id="worker-sync-fence")
        captured: list[dict[str, Any]] = []
        entrypoint_calls: list[dict[str, Any]] = []

        def fail_entrypoint(**kwargs: Any) -> str:
            entrypoint_calls.append(kwargs)
            raise RuntimeError("entrypoint crashed")

        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            fail_entrypoint,
        )
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda _task, **kwargs: captured.append(kwargs),
        )

        cmd.execute_task_sync(task)

        assert entrypoint_calls[0]["task_id"] == durable_task_id
        assert captured
        assert captured[0]["error_message"] == "entrypoint crashed"
        assert captured[0]["exception_type"] == "RuntimeError"
        assert captured[0]["expected_claimed_by_worker"] == "worker-sync-fence"
        assert captured[0]["expected_attempt_number"] == 3
        assert captured[0]["expected_execution_generation"] == 7

    def test_update_lease_heartbeat_without_identity_fails_closed(self) -> None:
        cmd = _make_command()

        assert cmd._update_lease_heartbeat() is False
        assert cmd.shutdown_requested is True

    def test_check_ray_connection_timeout_triggers_reconnect(self, monkeypatch) -> None:
        cmd = _make_command()
        reconnect_calls: list[bool] = []

        fake_ray = SimpleNamespace(is_initialized=lambda: True)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            cmd, "_get_ray_cluster_resources_with_timeout", lambda timeout_seconds: None
        )
        monkeypatch.setattr(cmd, "_reconnect_ray", lambda: reconnect_calls.append(True))

        cmd._check_ray_connection()

        assert reconnect_calls == [True]

    def test_check_ray_connection_exception_triggers_reconnect(self, monkeypatch) -> None:
        cmd = _make_command()
        reconnect_calls: list[bool] = []

        fake_ray = SimpleNamespace(
            is_initialized=lambda: True,
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            cmd,
            "_get_ray_cluster_resources_with_timeout",
            lambda timeout_seconds: (_ for _ in ()).throw(RuntimeError("resources failed")),
        )
        monkeypatch.setattr(cmd, "_reconnect_ray", lambda: reconnect_calls.append(True))

        cmd._check_ray_connection()

        assert reconnect_calls == [True]

    def test_check_ray_connection_healthy_returns_without_reconnect(self, monkeypatch) -> None:
        cmd = _make_command()
        reconnect_calls: list[bool] = []

        fake_ray = SimpleNamespace(is_initialized=lambda: True)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            cmd,
            "_get_ray_cluster_resources_with_timeout",
            lambda timeout_seconds: {"CPU": 2},
        )
        monkeypatch.setattr(cmd, "_reconnect_ray", lambda: reconnect_calls.append(True))

        cmd._check_ray_connection()

        assert reconnect_calls == []

    def test_reconnect_ray_cluster_success_rebuilds_runner(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "cluster"
        cmd.cluster_address = "ray://cluster:10001"
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=2))

        state = {"initialized": True}
        calls: list[str] = []

        def _is_initialized() -> bool:
            return bool(state["initialized"])

        def _shutdown() -> None:
            calls.append("shutdown")
            state["initialized"] = False

        def _init_cluster(address: str) -> None:
            calls.append(f"init:{address}")
            state["initialized"] = True

        fake_ray = SimpleNamespace(
            is_initialized=_is_initialized,
            shutdown=_shutdown,
            cluster_resources=lambda: {"CPU": 8},
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(cmd, "_init_cluster_ray", _init_cluster)
        stale_marks: list[bool] = []
        monkeypatch.setattr(
            cmd, "_mark_stale_ray_core_tasks_as_lost", lambda: stale_marks.append(True)
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep", lambda *_: None
        )

        sentinel_runner = cast(Any, SimpleNamespace(sentinel=True))
        runner_addresses: list[str | None] = []

        def create_runner(*, address: str | None = None):
            runner_addresses.append(address)
            return sentinel_runner

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            create_runner,
        )

        cmd._reconnect_ray()

        assert calls == ["shutdown", "init:ray://cluster:10001"]
        assert stale_marks == [True]
        assert cmd.ray_core_runner is sentinel_runner
        assert runner_addresses == ["ray://cluster:10001"]

    def test_reconnect_ray_shutdown_error_is_logged(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.execution_mode = "sync"

        fake_ray = SimpleNamespace(
            is_initialized=lambda: True,
            shutdown=lambda: (_ for _ in ()).throw(RuntimeError("shutdown failed")),
            cluster_resources=lambda: {"CPU": 1},
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.sleep", lambda *_: None
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            lambda: cast(Any, SimpleNamespace()),
        )

        cmd._reconnect_ray()

        assert "Error during shutdown: RuntimeError: shutdown failed" in cmd.stdout.getvalue()


@pytest.mark.django_db
class TestWorkerReconnectPollReconcile(RealCommandLeaseTestCase):
    """DB-backed tests for reconnect, polling, and initial reconcile branches."""

    def test_mark_stale_ray_core_tasks_returns_when_no_pending(
        self, django_assert_num_queries
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-core-empty-stale-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="worker-coverage",
        )
        cmd = _make_command()

        class EmptyRunner:
            pending_count = 0
            _pending_tasks: dict[int, RayCoreHandle] = {}

            @property
            def pending_task_handles(self) -> tuple[RayCoreHandle, ...]:
                pytest.fail("empty runner must return before reading pending_task_handles")

            def retire_pending_handle(self, _handle: RayCoreHandle) -> bool:
                pytest.fail("empty runner must not retire handles")

        cmd.ray_core_runner = cast(Any, EmptyRunner())

        with django_assert_num_queries(0):
            cmd._mark_stale_ray_core_tasks_as_lost()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 1
        assert task.error_message is None
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert cmd.shutdown_requested is False
        assert cmd.stdout.getvalue() == ""

    def test_ray_core_callers_fail_closed_without_authoritative_capability(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-core-authority-unavailable-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="ray-core-authority-unavailable-worker")
        lease = cmd.lease
        assert lease is not None
        task.refresh_from_db()
        task_before = RayTaskExecution.objects.values().get(pk=task.pk)
        handle = _pending_handle(task)

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {task.pk: handle}

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

            @property
            def pending_task_handles(self) -> tuple[RayCoreHandle, ...]:
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, _handle: RayCoreHandle) -> bool:
                pytest.fail("authority loss must not retire an unreviewed local handle")

            def poll_completed(self, _handles=None):
                pytest.fail("authority loss must be rejected before the Ray boundary")

        runner = Runner()
        cmd.ray_core_runner = cast(Any, runner)

        assert cmd._partition_ray_core_handles(cast(Any, runner), ()) == ()

        TaskWorkerLease.objects.filter(pk=lease.pk).update(is_active=False)
        cmd._mark_stale_ray_core_tasks_as_lost()
        monkeypatch.setitem(
            sys.modules,
            "ray",
            SimpleNamespace(
                is_initialized=lambda: pytest.fail(
                    "authority loss must be rejected before Ray initialization checks"
                )
            ),
        )
        assert cmd.poll_ray_core_tasks() == 0

        implicit_cmd = _make_command(worker_id="ray-core-implicit-capability-worker")
        monkeypatch.setattr(implicit_cmd, "_explicit_protocol_range", lambda _lease: None)
        assert implicit_cmd._partition_ray_core_handles(cast(Any, runner), (handle,)) is None

        assert runner.pending_task_handles == (handle,)
        assert RayTaskExecution.objects.values().get(pk=task.pk) == task_before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_lost_handle_terminalization_ignores_superseded_and_missing_rows(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-core-lost-handle-toctou-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="ray-core-lost-handle-toctou-worker")
        task.refresh_from_db()
        task_before = RayTaskExecution.objects.values().get(pk=task.pk)
        superseded_handle = _pending_handle(task)
        replacement_handle = _pending_handle(task)
        missing_handle = RayCoreHandle(
            task_pk=999_998,
            object_ref=object(),
            submitted_at=datetime.now(UTC),
            task_name="missing-after-partition",
            attempt_number=1,
            execution_generation=0,
        )

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {
                    task.pk: replacement_handle,
                    missing_handle.task_pk: missing_handle,
                }

            @property
            def pending_task_handles(self) -> tuple[RayCoreHandle, ...]:
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, handle: RayCoreHandle) -> bool:
                if self._pending_tasks.get(handle.task_pk) is not handle:
                    return False
                self._pending_tasks.pop(handle.task_pk)
                return True

        runner = Runner()
        assert (
            cmd._terminalize_lost_ray_core_handles(
                cast(Any, runner),
                (superseded_handle, missing_handle),
                error_message="Ray connection lost",
            )
            == 0
        )

        assert runner.pending_task_handles == (replacement_handle,)
        assert RayTaskExecution.objects.values().get(pk=task.pk) == task_before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_ray_core_cancellation_authority_loss_is_a_noop(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-core-cancellation-authority-loss-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="ray-core-cancellation-authority-loss-worker")
        lease = cmd.lease
        assert lease is not None
        task.refresh_from_db()
        task_before = RayTaskExecution.objects.values().get(pk=task.pk)
        TaskWorkerLease.objects.filter(pk=lease.pk).update(is_active=False)

        assert (
            cmd._finalize_ray_core_cancellation(
                task_pk=task.pk,
                attempt_number=task.attempt_number,
                execution_generation=task.execution_generation,
            )
            is False
        )

        assert RayTaskExecution.objects.values().get(pk=task.pk) == task_before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_submit_task_to_ray_core_handles_unavailable_cluster(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-unavailable-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        captured: list[dict[str, Any]] = []

        fake_ray = SimpleNamespace(is_initialized=lambda: False)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(cmd, "_reconnect_ray", lambda: None)
        monkeypatch.setattr(
            cmd, "_handle_task_failure", lambda _task, **kwargs: captured.append(kwargs)
        )

        cmd.submit_task_to_ray_core(task)

        assert captured
        assert captured[0]["error_message"] == "Ray cluster not available"
        assert captured[0]["exception_type"] == "RayConnectionError"
        assert captured[0]["expected_attempt_number"] == task.attempt_number
        assert captured[0]["expected_execution_generation"] == task.execution_generation

    def test_submit_task_to_ray_core_success_persists_tracking(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_target_address="ray://target:10001",
        )
        cmd = _make_command()

        fake_ray = SimpleNamespace(is_initialized=lambda: True)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        fake_runner = cast(
            Any,
            SimpleNamespace(
                submit_durable=lambda **_kwargs: SubmissionHandle(
                    ray_job_id="ray_core:1",
                    ray_address="ray://cluster:10001",
                    submitted_at=datetime.now(UTC),
                )
            ),
        )
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            lambda: fake_runner,
        )

        cmd.submit_task_to_ray_core(task)

        task.refresh_from_db()
        assert task.ray_job_id == "ray_core:1"
        assert task.ray_address == "ray://cluster:10001"
        assert task.ray_target_address == "ray://target:10001"

    def test_ray_core_post_attachment_error_does_not_fail_live_submission(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-post-attach-error-001",
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

        cmd.stdout = cast(Any, BrokenStdout())
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                submit_durable=lambda **_kwargs: SubmissionHandle(
                    ray_job_id="ray_core:attached",
                    ray_address="ray://cluster:10001",
                    submitted_at=datetime.now(UTC),
                )
            ),
        )
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
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
            cmd.submit_task_to_ray_core(task)

        task.refresh_from_db()
        assert failures == []
        assert task.state == TaskState.RUNNING
        assert task.ray_job_id == "ray_core:attached"
        assert task.ray_address == "ray://cluster:10001"
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_stale_ray_core_submission_is_cancelled_without_attaching(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-stale-submit-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=4,
        )
        cmd = _make_command()
        returned_handle = SubmissionHandle(
            ray_job_id=f"ray_core:{task.pk}",
            ray_address="ray://old-driver:10001",
            submitted_at=datetime.now(UTC),
        )
        cancelled: list[SubmissionHandle] = []

        class FakeRunner:
            def submit_durable(self, **_kwargs):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    attempt_number=2,
                    execution_generation=5,
                    claimed_by_worker="replacement-worker",
                    ray_job_id="ray_core:replacement",
                    ray_address="ray://replacement:10001",
                )
                return returned_handle

            def cancel(self, handle):
                cancelled.append(handle)
                return True

        cmd.ray_core_runner = cast(Any, FakeRunner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        cmd.submit_task_to_ray_core(task)

        task.refresh_from_db()
        assert cancelled == [returned_handle]
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 5
        assert task.claimed_by_worker == "replacement-worker"
        assert task.ray_job_id == "ray_core:replacement"
        assert task.ray_address == "ray://replacement:10001"
        assert "Discarded stale Ray Core submission" in cmd.stdout.getvalue()

    def test_ray_core_tracking_exception_cancels_exact_handle_without_retry(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-tracking-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        returned_handle = SubmissionHandle(
            ray_job_id=f"ray_core:{task.pk}",
            ray_address="ray://driver:10001",
            submitted_at=datetime.now(UTC),
        )
        cancelled: list[str] = []

        class FakeRunner:
            def submit_durable(self, **_kwargs):
                return returned_handle

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        cmd.ray_core_runner = cast(Any, FakeRunner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )
        monkeypatch.setattr(
            cmd,
            "_persist_submission_tracking",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database write failed")),
        )

        cmd.submit_task_to_ray_core(task)

        task.refresh_from_db()
        assert cancelled == [returned_handle.ray_job_id]
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert task.ray_job_id is None
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert "Failed to persist Ray Core submission tracking" in (task.error_message or "")

    def test_submit_task_to_ray_core_preserves_external_input_reference(self, monkeypatch) -> None:
        reference = "resultfs://sha256/" + "a" * 64 + "?rel=aa/input.json&bytes=4"
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-reference-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="null",
            kwargs_json="null",
            input_reference=reference,
        )
        cmd = _make_command()
        captured: dict[str, Any] = {}
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))

        def submit_durable(**kwargs: Any) -> SubmissionHandle:
            captured.update(kwargs)
            return SubmissionHandle(
                ray_job_id="ray_core:reference",
                ray_address="ray://cluster:10001",
                submitted_at=datetime.now(UTC),
            )

        cmd.ray_core_runner = cast(Any, SimpleNamespace(submit_durable=submit_durable))

        cmd.submit_task_to_ray_core(task)

        assert captured["task_execution"].input_reference == reference

    def test_submit_task_to_ray_core_submit_exception_routes_failure(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-submit-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()
        captured: list[dict[str, Any]] = []

        fake_ray = SimpleNamespace(is_initialized=lambda: True)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )
        cmd.ray_core_runner = cast(
            Any,
            SimpleNamespace(
                submit_durable=lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("core submit failed")
                )
            ),
        )
        monkeypatch.setattr(
            cmd, "_handle_task_failure", lambda _task, **kwargs: captured.append(kwargs)
        )

        cmd.submit_task_to_ray_core(task)

        assert captured
        assert "Failed to submit to Ray Core: core submit failed" in captured[0]["error_message"]
        assert captured[0]["exception_type"] == "RuntimeError"
        assert captured[0]["expected_attempt_number"] == task.attempt_number
        assert captured[0]["expected_execution_generation"] == task.execution_generation

    def test_ray_core_submit_exception_does_not_fail_replacement_attempt(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconnect-core-submit-stale-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=7,
        )
        cmd = _make_command()
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        class FakeRunner:
            def submit_durable(self, **_kwargs):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    attempt_number=2,
                    claimed_by_worker="replacement-worker",
                )
                raise RuntimeError("old submit failed")

        cmd.ray_core_runner = cast(Any, FakeRunner())

        cmd.submit_task_to_ray_core(task)

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.claimed_by_worker == "replacement-worker"
        assert task.error_message is None
        assert not TaskAttempt.objects.filter(execution=task, attempt_number=2).exists()

    def test_poll_ray_core_tasks_returns_when_no_pending(self, django_assert_num_queries) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-core-empty-poll-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            claimed_by_worker="worker-coverage",
        )
        cmd = _make_command()
        cmd._last_partitioned_handles = (_pending_handle(task),)

        class EmptyRunner:
            pending_count = 0
            _pending_tasks: dict[int, RayCoreHandle] = {}

            def poll_completed(self, _handles=None):
                pytest.fail("empty runner must return before polling Ray")

            def peek_completed(self, _handles=None):
                pytest.fail("empty runner must return before peeking Ray")

        cmd.ray_core_runner = cast(Any, EmptyRunner())

        with django_assert_num_queries(0):
            completed = cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert completed == 0
        assert cmd._last_partitioned_handles == ()
        assert task.state == TaskState.RUNNING
        assert task.finished_at is None
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_poll_ray_core_tasks_throttles_monitor_heartbeat_writes(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-heartbeat-throttle-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        class Runner:
            _pending_tasks = {task.pk: _pending_handle(task)}
            pending_count = 1

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, handle):
                if self._pending_tasks.get(handle.task_pk) is not handle:
                    return False
                self._pending_tasks.pop(handle.task_pk)
                return True

            def poll_completed(self, handles=None):
                return []

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())
        cmd.task_monitor_heartbeat_interval = 15
        clock = iter([100.0, 105.0, 116.0])

        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.time.monotonic",
            lambda: next(clock),
        )

        cmd.poll_ray_core_tasks()
        task.refresh_from_db()
        first_heartbeat = task.last_heartbeat_at
        assert first_heartbeat is not None

        cmd.poll_ray_core_tasks()
        task.refresh_from_db()
        assert task.last_heartbeat_at == first_heartbeat
        assert cmd.last_task_monitor_heartbeat == 100.0

        cmd.poll_ray_core_tasks()
        task.refresh_from_db()
        assert task.last_heartbeat_at is not None
        assert cmd.last_task_monitor_heartbeat == 116.0

    def test_poll_ray_core_tasks_does_not_heartbeat_replacement_attempt(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-heartbeat-stale-attempt-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        stale_handle = _pending_handle(
            task,
            attempt_number=1,
            execution_generation=7,
        )

        class Runner:
            pending_count = 1
            pending_task_handles = (stale_handle,)

            def retire_pending_handle(self, handle):
                return handle is stale_handle

            def poll_completed(self, handles=None):
                pytest.fail("a stale replacement handle must not cross the Ray boundary")

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.last_heartbeat_at is None

    @pytest.mark.django_db(transaction=True)
    def test_poll_ray_core_tasks_retires_unsupported_protocol_before_ray_boundary(
        self, monkeypatch
    ) -> None:
        close_legacy_worker_admission(
            expected_revision=1,
            legacy_producers_retired=True,
        )
        cmd = _make_command(
            worker_id="poll-v1-worker",
            claim_ownerless_tasks=False,
        )
        supported = RayTaskExecution.objects.create(
            task_id="poll-supported-protocol-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            execution_protocol_version=1,
            claimed_by_worker=cmd.worker_id,
        )
        unsupported = RayTaskExecution.objects.create(
            task_id="poll-unsupported-protocol-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[3, 4]",
            kwargs_json="{}",
            execution_protocol_version=2,
            claimed_by_worker=cmd.worker_id,
        )
        supported_handle = _pending_handle(supported)
        unsupported_handle = _pending_handle(unsupported)

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {
                    supported.pk: supported_handle,
                    unsupported.pk: unsupported_handle,
                }
                self.polled: tuple[RayCoreHandle, ...] | None = None

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, handle) -> bool:
                if self._pending_tasks.get(handle.task_pk) is not handle:
                    return False
                self._pending_tasks.pop(handle.task_pk)
                return True

            def poll_completed(self, handles=None):
                self.polled = handles
                return []

        runner = Runner()
        cmd.ray_core_runner = cast(Any, runner)
        unsupported_before = RayTaskExecution.objects.values().get(pk=unsupported.pk)
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "unsupported execution must not reach result storage"
            ),
        )

        assert cmd.poll_ray_core_tasks() == 1

        assert runner.polled == (supported_handle,)
        assert runner.pending_task_handles == (supported_handle,)
        assert RayTaskExecution.objects.values().get(pk=unsupported.pk) == unsupported_before
        assert not TaskAttempt.objects.filter(execution=unsupported).exists()

    @pytest.mark.django_db(transaction=True)
    def test_store_and_succeed_rejects_protocol_before_external_storage(self, monkeypatch) -> None:
        close_legacy_worker_admission(
            expected_revision=1,
            legacy_producers_retired=True,
        )
        task = RayTaskExecution.objects.create(
            task_id="store-success-unsupported-protocol-002",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            execution_protocol_version=2,
        )
        task_before = RayTaskExecution.objects.values().get(pk=task.pk)
        cmd = _make_command(
            worker_id="store-v1-worker",
            claim_ownerless_tasks=False,
        )
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "unsupported success must be rejected before external storage"
            ),
        )

        assert cmd._store_and_succeed_task(task, {"value": 3}) is False

        assert RayTaskExecution.objects.values().get(pk=task.pk) == task_before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_completion_losing_lease_after_poll_cannot_store_or_terminalize(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-completion-lease-loss-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="poll-lease-loss-worker")
        lease = cmd.lease
        assert lease is not None
        task.refresh_from_db()
        task_before = RayTaskExecution.objects.values().get(pk=task.pk)
        handle = _pending_handle(task)

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {task.pk: handle}

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, candidate) -> bool:
                if self._pending_tasks.get(candidate.task_pk) is not candidate:
                    return False
                self._pending_tasks.pop(candidate.task_pk)
                return True

            def poll_completed(self, handles=None):
                assert handles == (handle,)
                TaskWorkerLease.objects.filter(pk=lease.pk).update(is_active=False)
                self.retire_pending_handle(handle)
                return [_completion(task, '{"success": true, "result": 3}')]

        cmd.ray_core_runner = cast(Any, Runner())
        cmd.last_task_monitor_heartbeat = time.monotonic()
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail("lease loss must fence result storage"),
        )

        cmd.poll_ray_core_tasks()

        assert RayTaskExecution.objects.values().get(pk=task.pk) == task_before
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert cmd.shutdown_requested is True

    def test_cancellation_committing_after_poll_read_wins_over_success(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-completion-cancellation-race-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="poll-cancellation-race-worker")
        task.refresh_from_db()
        handle = _pending_handle(task)

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {task.pk: handle}

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, candidate) -> bool:
                if self._pending_tasks.get(candidate.task_pk) is not candidate:
                    return False
                self._pending_tasks.pop(candidate.task_pk)
                return True

            def poll_completed(self, handles=None):
                assert handles == (handle,)
                self.retire_pending_handle(handle)
                return [_completion(task, '{"success": true, "result": 3}')]

        cmd.ray_core_runner = cast(Any, Runner())
        cmd.last_task_monitor_heartbeat = time.monotonic()
        original_authority = cmd._authoritative_task_owner
        injected = False

        def cancellation_race(snapshot, **kwargs):
            nonlocal injected
            if not injected and kwargs["expected_state"] == TaskState.RUNNING:
                injected = True
                RayTaskExecution.objects.filter(pk=task.pk).update(state=TaskState.CANCELLING)
            return original_authority(snapshot, **kwargs)

        monkeypatch.setattr(cmd, "_authoritative_task_owner", cancellation_race)
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "a committed cancellation must fence result storage"
            ),
        )

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert injected is True
        assert task.state == TaskState.CANCELLED
        assert task.result_data is None
        assert TaskAttempt.objects.filter(
            execution=task,
            attempt_number=task.attempt_number,
            state=TaskState.CANCELLED,
        ).exists()

    def test_disconnect_retires_cancelling_handle_without_claiming_cancellation(
        self, monkeypatch
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-disconnect-cancelling-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="poll-cancelling-worker")
        task.refresh_from_db()
        task_before = RayTaskExecution.objects.values().get(pk=task.pk)
        handle = _pending_handle(task)

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {task.pk: handle}

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, candidate) -> bool:
                if self._pending_tasks.get(candidate.task_pk) is not candidate:
                    return False
                self._pending_tasks.pop(candidate.task_pk)
                return True

        runner = Runner()
        cmd.ray_core_runner = cast(Any, runner)
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: False))

        assert cmd.poll_ray_core_tasks() == 1

        assert runner.pending_count == 0
        assert RayTaskExecution.objects.values().get(pk=task.pk) == task_before
        assert not TaskAttempt.objects.filter(execution=task).exists()

    def test_poll_ray_core_tasks_handles_disconnected_and_missing_tasks(self, monkeypatch) -> None:
        existing = RayTaskExecution.objects.create(
            task_id="poll-disconnect-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
        )

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {
                    existing.pk: _pending_handle(existing),
                    999999: RayCoreHandle(
                        task_pk=999999,
                        object_ref=object(),
                        submitted_at=datetime.now(UTC),
                        task_name="missing",
                        attempt_number=1,
                        execution_generation=0,
                    ),
                }

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, handle) -> bool:
                if self._pending_tasks.get(handle.task_pk) is not handle:
                    return False
                self._pending_tasks.pop(handle.task_pk)
                return True

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())
        calls: list[dict[str, Any]] = []

        fake_ray = SimpleNamespace(is_initialized=lambda: False)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            cmd, "_handle_task_failure", lambda _task, **kwargs: calls.append(kwargs)
        )

        cmd.poll_ray_core_tasks()

        assert len(calls) == 1
        assert calls[0]["error_message"] == "Ray connection lost"
        assert calls[0]["exception_type"] == "RayConnectionError"
        assert cmd.ray_core_runner._pending_tasks == {}

    def test_ray_disconnect_does_not_fail_replacement_attempt(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-disconnect-stale-attempt-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        stale_handle = _pending_handle(
            task,
            attempt_number=1,
            execution_generation=7,
        )

        class Runner:
            def __init__(self) -> None:
                self._pending_tasks = {task.pk: stale_handle}

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def retire_pending_handle(self, handle) -> bool:
                if self._pending_tasks.get(handle.task_pk) is not handle:
                    return False
                self._pending_tasks.pop(handle.task_pk)
                return True

            @property
            def pending_count(self) -> int:
                return len(self._pending_tasks)

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: False))

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.error_message is None
        assert not TaskAttempt.objects.filter(execution=task, attempt_number=2).exists()
        assert cmd.ray_core_runner._pending_tasks == {}

    def test_poll_ray_core_tasks_handles_poll_exception(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-exception-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )

        class Runner:
            _pending_tasks = {task.pk: _pending_handle(task)}
            pending_count = 1

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def poll_completed(self, handles=None):
                raise RuntimeError("poll exploded")

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())

        fake_ray = SimpleNamespace(is_initialized=lambda: True)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        cmd.poll_ray_core_tasks()

        assert "Error polling Ray Core tasks: RuntimeError: poll exploded" in cmd.stdout.getvalue()

    def test_poll_ray_core_tasks_processes_success_failure_missing_and_bad_json(
        self, monkeypatch
    ) -> None:
        success_task = RayTaskExecution.objects.create(
            task_id="poll-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            error_message="transient failure",
            error_traceback="RuntimeError: transient failure",
        )
        failure_task = RayTaskExecution.objects.create(
            task_id="poll-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[3, 4]",
            kwargs_json="{}",
        )
        bad_json_task = RayTaskExecution.objects.create(
            task_id="poll-bad-json-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[5, 6]",
            kwargs_json="{}",
        )

        class Runner:
            _pending_tasks = {
                success_task.pk: _pending_handle(success_task),
                failure_task.pk: _pending_handle(failure_task),
                bad_json_task.pk: _pending_handle(bad_json_task),
            }
            pending_count = 1

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def poll_completed(self, handles=None):
                return [
                    _completion(success_task, '{"success": true, "result": 3}'),
                    _completion(
                        failure_task,
                        (
                            '{"success": false, "result": null, "error": "boom", '
                            '"traceback": "tb", "exception_type": "RuntimeError"}'
                        ),
                    ),
                    RayCoreCompletion(
                        task_pk=999999,
                        attempt_number=1,
                        execution_generation=0,
                        result_json='{"success": true, "result": 1}',
                    ),
                    _completion(bad_json_task, "not-json"),
                ]

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())
        failures: list[dict[str, Any]] = []

        fake_ray = SimpleNamespace(is_initialized=lambda: True)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda task, result: setattr(task, "result_data", str(result)),
        )
        monkeypatch.setattr(
            cmd, "_handle_task_failure", lambda _task, **kwargs: failures.append(kwargs)
        )

        cmd.poll_ray_core_tasks()

        success_task.refresh_from_db()
        bad_json_task.refresh_from_db()

        assert success_task.state == TaskState.SUCCEEDED
        assert success_task.result_data == "3"
        assert success_task.error_message is None
        assert success_task.error_traceback is None
        assert success_task.finished_at is not None
        assert len(failures) == 2
        assert failures[0]["error_message"] == "boom"
        assert failures[1]["error_message"] == (
            "Legacy execution completion rejected (malformed_legacy)"
        )
        assert failures[1]["exception_type"] == "RayCompletionMalformed"
        assert failures[1]["retryable"] is None
        assert bad_json_task.state == TaskState.RUNNING
        assert "Task 999999 not found in database" in cmd.stdout.getvalue()

    def test_strict_ray_core_transport_failure_never_retries_completed_work(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="strict-ray-core-transport-failure-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command(worker_id="strict-ray-core-transport-worker")
        task.refresh_from_db()
        handle = _pending_handle(task)
        fixed_failure = json.dumps(
            {
                "success": False,
                "result": None,
                "result_reference": None,
                "error": "Ray Core execution transport failed",
                "traceback": None,
                "exception_type": "RayCoreExecutionTransportError",
                "retryable": False,
            }
        )

        class Runner:
            _pending_tasks = {task.pk: handle}
            pending_count = 1

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def poll_completed(self, handles=None):
                assert handles == (handle,)
                self._pending_tasks.pop(task.pk)
                return [_completion(task, fixed_failure)]

        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))

        assert cmd.poll_ray_core_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.executor_django_ray_version is None
        assert task.error_message == "Ray Core execution transport failed"
        archived = TaskAttempt.objects.get(execution=task, attempt_number=1)
        assert archived.executor_django_ray_version is None

    def test_poll_ray_core_tasks_consumes_exact_versioned_completion_provenance(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-versioned-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        handle = _pending_handle(task)
        serialized = _versioned_completion_json(task, result={"value": 3})

        class Runner:
            _pending_tasks = {task.pk: handle}
            pending_count = 1

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def poll_completed(self, handles=None):
                assert handles == (handle,)
                return [_completion(task, serialized)]

        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))

        assert cmd.poll_ray_core_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert json.loads(task.result_data or "null") == {"value": 3}
        assert task.executor_django_ray_version == "0.5.0-executor"
        archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
        assert archived.executor_django_ray_version == "0.5.0-executor"

    def test_poll_ray_core_tasks_rejects_versioned_identity_before_result_storage(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-versioned-mismatch-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        handle = _pending_handle(task)
        serialized = _versioned_completion_json(
            task,
            task_id="another-task-identity",
            result={"must_not_store": "secret"},
        )

        class Runner:
            _pending_tasks = {task.pk: handle}
            pending_count = 1

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def poll_completed(self, handles=None):
                assert handles == (handle,)
                return [_completion(task, serialized)]

        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "an incompatible completion must not reach result storage"
            ),
        )

        assert cmd.poll_ray_core_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 2
        assert task.result_data is None
        assert task.result_reference is None
        assert task.executor_django_ray_version is None
        assert task.error_message == ("Execution completion rejected (identity_mismatch)")
        archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
        assert archived.executor_django_ray_version is None
        assert "must_not_store" not in (task.error_message or "")

    def test_poll_ray_core_tasks_rejects_oversized_legacy_completion_without_retry(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="poll-legacy-resource-limit-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        handle = _pending_handle(task)
        serialized = json.dumps(
            {
                "success": True,
                "result": {"must_not_store": "x" * 256},
            }
        )

        class Runner:
            _pending_tasks = {task.pk: handle}
            pending_count = 1

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def poll_completed(self, handles=None):
                assert handles == (handle,)
                return [_completion(task, serialized)]

        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setattr(
            "django_ray.execution_codec.EXECUTION_COMPLETION_MAX_BYTES",
            128,
        )
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail(
                "a resource-limited completion must not reach result storage"
            ),
        )

        assert cmd.poll_ray_core_tasks() == 1

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 2
        assert task.result_data is None
        assert task.result_reference is None
        assert task.executor_django_ray_version is None
        assert task.error_message == "Execution completion rejected (resource_limit)"
        archived = TaskAttempt.objects.get(execution=task, attempt_number=2)
        assert archived.state == TaskState.FAILED
        assert archived.executor_django_ray_version is None

    @pytest.mark.parametrize(
        "result_json",
        [
            '{"success": true, "result": 3}',
            (
                '{"success": false, "result": null, "error": "old failure", '
                '"traceback": "old traceback", "exception_type": "RuntimeError"}'
            ),
        ],
        ids=["success", "failure"],
    )
    def test_poll_ray_core_tasks_ignores_completion_from_replaced_execution(
        self, monkeypatch, result_json: str
    ) -> None:
        task = RayTaskExecution.objects.create(
            runtime_env_hash="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            task_id=f"poll-stale-completion-{json.loads(result_json)['success']}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=4,
        )
        stale_attempt = task.attempt_number
        stale_generation = task.execution_generation
        assert record_lost(
            task,
            error_message="old worker disappeared",
            expected_attempt_number=stale_attempt,
            expected_execution_generation=stale_generation,
        )
        replacement = retry_task(
            task.pk,
            allowed_states=(TaskState.LOST,),
            expected_attempt_number=stale_attempt,
            expected_execution_generation=stale_generation,
        )
        assert replacement is not None
        RayTaskExecution.objects.filter(pk=task.pk).update(
            state=TaskState.RUNNING,
            claimed_by_worker="replacement-worker",
        )
        task.refresh_from_db()

        stale_handle = _pending_handle(
            task,
            attempt_number=stale_attempt,
            execution_generation=stale_generation,
        )

        class Runner:
            pending_count = 1
            pending_task_handles = (stale_handle,)

            def retire_pending_handle(self, handle):
                return handle is stale_handle

            def poll_completed(self, handles=None):
                pytest.fail("a stale replacement handle must not cross the Ray boundary")

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))
        monkeypatch.setattr(
            cmd,
            "_store_task_result",
            lambda *_args, **_kwargs: pytest.fail("stale success must not reach result storage"),
        )
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda *_args, **_kwargs: pytest.fail("stale failure must not reach retry handling"),
        )

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == stale_attempt + 1
        assert task.execution_generation == stale_generation + 1
        assert task.claimed_by_worker == "replacement-worker"
        assert task.result_data is None
        assert task.error_message is None
        assert not TaskAttempt.objects.filter(
            execution=task,
            attempt_number=stale_attempt + 1,
        ).exists()
        assert "Retired 1 stale or unsupported Ray Core handle" in cmd.stdout.getvalue()

    def test_submit_task_to_ray_success_tracks_active_task(self, monkeypatch) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-success-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
        )
        cmd = _make_command()

        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda _payload: pytest.fail(
                "the task manager must not deserialize durable Ray Job input"
            ),
        )
        monkeypatch.setattr(
            "django_ray.input_storage.load_task_input",
            lambda **_kwargs: pytest.fail("the task manager must not load durable Ray Job input"),
        )

        reserved_handle = _ray_job_handle(task, "raysubmit_coverage_001")

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **kwargs):
                assert kwargs == {"task_execution": task}
                return reserved_handle

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.ray_job_id == "raysubmit_coverage_001"
        assert task.ray_address == "ray://cluster:10001"
        assert cmd.active_tasks[task.pk] == "raysubmit_coverage_001"

    @pytest.mark.parametrize("failure_stage", ["constructor", "submission_handle"])
    def test_pre_reservation_request_rejection_is_fixed_and_nonretryable(
        self,
        monkeypatch,
        failure_stage: str,
    ) -> None:
        classification = (
            RayJobRequestPreparationRejection.CONFIGURATION
            if failure_stage == "constructor"
            else RayJobRequestPreparationRejection.INVALID_REQUEST
        )
        task = RayTaskExecution.objects.create(
            task_id=f"rq2-pre-reservation-{failure_stage}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=1,
            execution_generation=0,
        )
        cmd = _make_command()

        class FakeRunner:
            def __init__(self):
                if failure_stage == "constructor":
                    raise RayJobRequestPreparationError(classification)

            def submission_handle(self, _task):
                raise RayJobRequestPreparationError(classification)

            def submit_durable(self, **_kwargs):
                pytest.fail("pre-reservation rejection must precede submission")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.should_retry",
            lambda *_args, **_kwargs: pytest.fail(
                "deterministic rq2 preparation failure must bypass retry policy"
            ),
        )

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 1
        assert task.run_after is None
        assert task.error_message == (
            f"Ray Job request preparation rejected: {classification.value}"
        )
        assert task.error_traceback is None
        assert task.ray_job_id is None
        archived = TaskAttempt.objects.get(execution=task, attempt_number=1)
        assert archived.state == TaskState.FAILED
        assert archived.error_traceback is None

    def test_uncertain_ray_job_submission_retains_exact_identity_without_retry(
        self,
        monkeypatch,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-uncertain-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        reserved_handle = _ray_job_handle(
            task,
            "raysubmit_django_ray_v1_uncertain",
            "ray://cluster:10001",
        )

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                raise RayJobSubmissionUncertainError(
                    reserved_handle.ray_job_id,
                    "response timed out after acceptance",
                )

            def cancel(self, _handle):
                pytest.fail("an uncertain exact submission must be reconciled, not cancelled")

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(
            "django_ray.runtime.serialization.deserialize_args",
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.ray_address == reserved_handle.ray_address
        assert cmd.active_tasks[task.pk] == reserved_handle.ray_job_id
        assert cmd.active_task_identities[task.pk] == (2, 7)
        assert not TaskAttempt.objects.filter(execution=task).exists()
        assert "acceptance is uncertain" in cmd.stdout.getvalue()

    @pytest.mark.parametrize(
        ("classification", "expected_retryable"),
        [
            (RayJobRequestPreparationRejection.INVALID_REQUEST, False),
            (RayJobRequestPreparationRejection.STORAGE_UNAVAILABLE, None),
        ],
    )
    def test_request_preparation_failure_releases_reservation_with_fixed_policy(
        self,
        monkeypatch,
        classification: RayJobRequestPreparationRejection,
        expected_retryable: bool | None,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id=f"rq2-preparation-{classification.value}-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.RUNNING,
            args_json="[1, 2]",
            kwargs_json="{}",
            attempt_number=2,
            execution_generation=7,
        )
        cmd = _make_command()
        task.refresh_from_db()
        reserved_handle = _ray_job_handle(task, _rq2_ray_job_id("f"))
        captured: dict[str, object] = {}

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit_durable(self, **_kwargs):
                raise RayJobRequestPreparationError(classification)

        def capture_failure(_task, **kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr(cmd, "_handle_task_failure", capture_failure)

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.ray_job_id is None
        assert task.ray_address is None
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities
        assert captured["error_message"] == (
            f"Ray Job request preparation rejected: {classification.value}"
        )
        assert captured["error_traceback"] is None
        assert str(captured["exception_type"]).endswith("RayJobRequestPreparationError")
        assert captured["retryable"] is expected_retryable
