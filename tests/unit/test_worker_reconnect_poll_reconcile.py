"""Focused coverage tests for worker reconnect/poll/reconcile paths."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.db import transaction

from django_ray.lifecycle import record_lost, retry_task
from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import CancellationStatus, RayTaskExecution, TaskAttempt, TaskState
from django_ray.runner import RayJobSubmissionUncertainError
from django_ray.runner.base import JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import (
    CancellationOutcome,
    CancellationOutcomeStatus,
)
from django_ray.runner.ray_core import RayCoreCompletion, RayCoreHandle


def _make_command(worker_id: str = "worker-coverage") -> Command:
    cmd = Command()
    cmd.stdout = StringIO()
    cmd.style = cmd.style
    cmd.worker_id = worker_id
    cmd.sync_mode = False
    cmd.execution_mode = "local"
    cmd.cluster_address = None
    cmd.active_tasks = {}
    cmd.ray_core_runner = None
    return cmd


def _pending_handle(
    task: RayTaskExecution,
    *,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> RayCoreHandle:
    return RayCoreHandle(
        task_pk=task.pk,
        object_ref=object(),
        submitted_at=datetime.now(UTC),
        task_name="test",
        attempt_number=task.attempt_number if attempt_number is None else attempt_number,
        execution_generation=(
            task.execution_generation if execution_generation is None else execution_generation
        ),
    )


def _ray_job_handle(
    task: RayTaskExecution,
    job_id: str,
    address: str = "ray://cluster:10001",
) -> SubmissionHandle:
    return SubmissionHandle(
        ray_job_id=job_id,
        ray_address=address,
        submitted_at=datetime.now(UTC),
    )


def _completion(
    task: RayTaskExecution,
    result_json: str,
    *,
    attempt_number: int | None = None,
    execution_generation: int | None = None,
) -> RayCoreCompletion:
    return RayCoreCompletion(
        task_pk=task.pk,
        attempt_number=task.attempt_number if attempt_number is None else attempt_number,
        execution_generation=(
            task.execution_generation if execution_generation is None else execution_generation
        ),
        result_json=result_json,
    )


class TestWorkerDispatchAndReconnectHelpers:
    """Non-DB tests for dispatch/reconnect helper branches."""

    def test_process_task_dispatches_to_ray_core_and_ray_job(self, monkeypatch) -> None:
        task = SimpleNamespace(
            pk=1,
            callable_path="testproject.tasks.add_numbers",
            runtime_env_profile=None,
            runtime_env_json="{}",
            runtime_env_hash="",
        )
        cmd = _make_command()
        events: list[str] = []

        monkeypatch.setattr(cmd, "_update_lease_heartbeat", lambda: events.append("heartbeat"))
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
            runtime_env_hash="",
        )
        cmd = _make_command()
        captured: list[dict[str, Any]] = []

        monkeypatch.setattr(
            "django_ray.runtime.entrypoint.execute_task",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("entrypoint crashed")),
        )
        monkeypatch.setattr(
            cmd,
            "_handle_task_failure",
            lambda _task, **kwargs: captured.append(kwargs),
        )

        cmd.execute_task_sync(task)

        assert captured
        assert captured[0]["error_message"] == "entrypoint crashed"
        assert captured[0]["exception_type"] == "RuntimeError"
        assert captured[0]["expected_attempt_number"] == 1
        assert captured[0]["expected_execution_generation"] == 0

    def test_update_lease_heartbeat_ignores_update_errors(self, monkeypatch) -> None:
        cmd = _make_command()
        cmd.lease = cast(Any, object())

        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.TaskWorkerLease.objects.filter",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
        )

        # Should not raise.
        cmd._update_lease_heartbeat()

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
        monkeypatch.setattr(
            "django_ray.management.commands.django_ray_worker.RayCoreRunner",
            lambda: sentinel_runner,
        )

        cmd._reconnect_ray()

        assert calls == ["shutdown", "init:ray://cluster:10001"]
        assert stale_marks == [True]
        assert cmd.ray_core_runner is sentinel_runner

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

        assert "Error during shutdown: shutdown failed" in cmd.stdout.getvalue()


@pytest.mark.django_db
class TestWorkerReconnectPollReconcile:
    """DB-backed tests for reconnect/poll/reconcile branches."""

    def test_mark_stale_ray_core_tasks_returns_when_no_pending(self) -> None:
        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=0, _pending_tasks={}))

        cmd._mark_stale_ray_core_tasks_as_lost()

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
                submit=lambda **_kwargs: SubmissionHandle(
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
                submit=lambda **_kwargs: SubmissionHandle(
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
            def submit(self, **_kwargs):
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
            def submit(self, **_kwargs):
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

        def submit(**kwargs: Any) -> SubmissionHandle:
            captured.update(kwargs)
            return SubmissionHandle(
                ray_job_id="ray_core:reference",
                ray_address="ray://cluster:10001",
                submitted_at=datetime.now(UTC),
            )

        cmd.ray_core_runner = cast(Any, SimpleNamespace(submit=submit))

        cmd.submit_task_to_ray_core(task)

        assert captured["args"] == ()
        assert captured["kwargs"] == {}
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
                submit=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("core submit failed"))
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
            def submit(self, **_kwargs):
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

    def test_poll_ray_core_tasks_returns_when_no_pending(self) -> None:
        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, SimpleNamespace(pending_count=0, _pending_tasks={}))

        cmd.poll_ray_core_tasks()

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

            def poll_completed(self):
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

            def poll_completed(self):
                return []

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())
        monkeypatch.setitem(sys.modules, "ray", SimpleNamespace(is_initialized=lambda: True))

        cmd.poll_ray_core_tasks()

        task.refresh_from_db()
        assert task.last_heartbeat_at is None

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

            def clear_pending_tasks(self) -> None:
                self._pending_tasks.clear()

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

            def clear_pending_tasks(self) -> None:
                self._pending_tasks.clear()

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
        class Runner:
            _pending_tasks = {
                1: RayCoreHandle(
                    task_pk=1,
                    object_ref=object(),
                    submitted_at=datetime.now(UTC),
                    task_name="missing",
                    attempt_number=1,
                    execution_generation=0,
                )
            }
            pending_count = 1

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

            @property
            def pending_task_handles(self):
                return tuple(self._pending_tasks.values())

            def poll_completed(self):
                raise RuntimeError("poll exploded")

        cmd = _make_command()
        cmd.ray_core_runner = cast(Any, Runner())

        fake_ray = SimpleNamespace(is_initialized=lambda: True)
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        cmd.poll_ray_core_tasks()

        assert "Error polling Ray Core tasks: poll exploded" in cmd.stdout.getvalue()

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

            def poll_completed(self):
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
        assert failures and failures[0]["error_message"] == "boom"
        assert bad_json_task.state == TaskState.RUNNING
        assert "Task 999999 not found in database" in cmd.stdout.getvalue()
        assert "Error processing task" in cmd.stdout.getvalue()

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
        stale_completion = _completion(
            task,
            result_json,
            attempt_number=stale_attempt,
            execution_generation=stale_generation,
        )

        class Runner:
            pending_count = 1
            pending_task_handles = (stale_handle,)

            def poll_completed(self):
                return [stale_completion]

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
        assert "Ignoring stale Ray Core result" in cmd.stdout.getvalue()

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
            lambda payload: [1, 2] if payload == "[1, 2]" else {},
        )

        reserved_handle = _ray_job_handle(task, "raysubmit_coverage_001")

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit(self, **_kwargs):
                return reserved_handle

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.ray_job_id == "raysubmit_coverage_001"
        assert task.ray_address == "ray://cluster:10001"
        assert cmd.active_tasks[task.pk] == "raysubmit_coverage_001"

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

            def submit(self, **_kwargs):
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

    def test_returned_ray_job_identity_mismatch_stops_observed_job_without_retry(
        self,
        monkeypatch,
        django_capture_on_commit_callbacks,
    ) -> None:
        task = RayTaskExecution.objects.create(
            task_id="ray-job-submit-mismatch-001",
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
            "raysubmit_django_ray_v1_reserved",
            "ray://cluster:10001",
        )
        cancelled: list[str] = []

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit(self, **_kwargs):
                raise RayJobSubmissionUncertainError(
                    reserved_handle.ray_job_id,
                    "Ray returned another identity",
                    observed_submission_id="raysubmit_unexpected",
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
        assert cancelled == [
            reserved_handle.ray_job_id,
            "raysubmit_unexpected",
        ]
        assert task.state == TaskState.FAILED
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities
        assert TaskAttempt.objects.filter(execution=task, attempt_number=2).count() == 1

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
            reserved_handle.ray_job_id,
            observed_handle.ray_job_id,
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

    def test_returned_ray_job_identity_mismatch_does_not_stop_adopted_reservation(
        self,
        monkeypatch,
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
        cancelled: list[str] = []

        class FakeRunner:
            def submission_handle(self, _task):
                return reserved_handle

            def submit(self, **_kwargs):
                RayTaskExecution.objects.filter(pk=task.pk).update(
                    claimed_by_worker="replacement-worker"
                )
                raise RayJobSubmissionUncertainError(
                    reserved_handle.ray_job_id,
                    "Ray returned another identity after adoption",
                    observed_submission_id="raysubmit_unexpected_adopted",
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
        assert cancelled == ["raysubmit_unexpected_adopted"]
        assert task.state == TaskState.RUNNING
        assert task.claimed_by_worker == "replacement-worker"
        assert task.ray_job_id == reserved_handle.ray_job_id
        assert task.pk not in cmd.active_tasks
        assert task.pk not in cmd.active_task_identities
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

            def submit(self, **_kwargs):
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

            def submit(self, **_kwargs):
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

            def submit(self, **_kwargs):
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

            def submit(self, **_kwargs):
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

            def submit(self, **kwargs: Any) -> SubmissionHandle:
                captured.update(kwargs)
                return reserved_handle

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        assert captured["args"] == ()
        assert captured["kwargs"] == {}
        assert captured["task_execution"].input_reference == reference

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

            def submit(self, **_kwargs):
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

    def test_reconcile_tasks_returns_early_for_sync_or_empty(self) -> None:
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
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

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
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

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
            ray_address="ray://previous:10001",
            ray_target_address="ray://target:10001",
            execution_generation=4,
        )
        cmd = _make_command()
        cmd.execution_mode = "ray"
        claimed: list[RayTaskExecution] = []
        monkeypatch.setattr(cmd, "process_task", lambda current_task: claimed.append(current_task))

        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert claimed and claimed[0].pk == task.pk
        assert task.execution_generation == 5
        assert task.ray_job_id is None
        assert task.ray_address is None
        assert task.ray_target_address == "ray://target:10001"

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

        cmd.claim_and_process_tasks(queues=["default"], concurrency=1)

        task.refresh_from_db()
        assert task.ray_target_address is None
        assert task.ray_job_id is None
        assert task.ray_address is None

    def test_reconcile_tasks_missing_completion_eventually_retries(self, monkeypatch) -> None:
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
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.pk not in cmd.active_tasks

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

    def test_reconcile_tasks_expired_malformed_envelope_uses_failure_policy(
        self, monkeypatch
    ) -> None:
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
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert "malformed completion envelope" in (task.error_message or "")
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

    def test_reconcile_tasks_expired_success_without_envelope_uses_failure_policy(
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
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert "without a completion envelope" in (task.error_message or "")
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
                "expected_attempt_number": 1,
                "expected_execution_generation": 0,
                "expected_completion_data": task.completion_data,
                "require_completion_data_match": True,
            }
        ]
        assert task.pk not in cmd.active_tasks

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
            started_at=datetime.now(UTC),
        )
        cmd = _make_command(worker_id="adopting-worker")

        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

        class FakeRunner:
            def get_status(self, _handle):
                return JobInfo(job_id=orphan.ray_job_id or "", status=JobStatus.RUNNING)

            def get_logs(self, _handle):  # pragma: no cover - not reached
                return ""

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        activity_count = cmd.reconcile_tasks()

        orphan.refresh_from_db()
        assert orphan.claimed_by_worker == "adopting-worker"
        assert orphan.last_heartbeat_at is not None
        assert cmd.active_tasks[orphan.pk] == "raysubmit_orphan_running_001"
        assert activity_count == 1

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

        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

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

        cmd.reconcile_tasks()

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
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

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
        monkeypatch.setattr(cmd, "_handle_task_failure", lambda *_args, **_kwargs: False)

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
        monkeypatch.setattr(cmd, "_handle_task_failure", lambda *_args, **_kwargs: False)

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
        monkeypatch.setattr(cmd, "_handle_task_failure", lambda *_args, **_kwargs: False)

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

    def test_reconcile_failed_job_preserves_completion_published_while_fetching_logs(
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
                RayTaskExecution.objects.filter(pk=task.pk).update(completion_data=completion_data)
                return "stale traceback"

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

    def test_reconcile_reports_unknown_job_status(self, monkeypatch) -> None:
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
                    message="status unavailable",
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.RUNNING
        assert task.pk in cmd.active_tasks
        assert "status is unknown" in cmd.stdout.getvalue()

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

            def cancel(self, handle):
                cancelled.append(handle.ray_job_id)
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert cancelled == ["raysubmit_unknown_status_stale_001"]
        assert task.state == TaskState.LOST
        assert task.attempt_number == 2
        assert task.execution_generation == 7
        assert task.cancellation_status == CancellationStatus.REQUESTED
        assert task.pk not in cmd.active_tasks
        assert "automatic retry was suppressed" in cmd.stdout.getvalue()

    def test_reconcile_consumes_valid_completion_before_status_rpc(self, monkeypatch) -> None:
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
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.SUCCEEDED
        assert task.result_data == "3"
        assert task.pk not in cmd.active_tasks

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
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

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

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner._get_client", unavailable)
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

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
        cmd = _make_command(worker_id="adopting-worker")
        monkeypatch.setattr("django_ray.runner.leasing.get_active_workers", list)

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
