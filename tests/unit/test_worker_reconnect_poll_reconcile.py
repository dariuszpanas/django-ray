"""Focused coverage tests for worker reconnect/poll/reconcile paths."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

import pytest

from django_ray.management.commands.django_ray_worker import Command
from django_ray.models import RayTaskExecution, TaskState
from django_ray.runner.base import JobInfo, JobStatus, SubmissionHandle
from django_ray.runner.cancellation import CancellationOutcomeStatus


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


class TestWorkerDispatchAndReconnectHelpers:
    """Non-DB tests for dispatch/reconnect helper branches."""

    def test_process_task_dispatches_to_ray_core_and_ray_job(self, monkeypatch) -> None:
        task = SimpleNamespace(pk=1, callable_path="testproject.tasks.add_numbers")
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
            _pending_tasks = {task.pk: object()}
            pending_count = 1

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

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
                self._pending_tasks = {existing.pk: object(), 999999: object()}

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

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

    def test_poll_ray_core_tasks_handles_poll_exception(self, monkeypatch) -> None:
        class Runner:
            _pending_tasks = {1: object()}
            pending_count = 1

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

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
            _pending_tasks = {1: object()}
            pending_count = 1

            @property
            def pending_task_ids(self):
                return tuple(self._pending_tasks)

            def poll_completed(self):
                return [
                    (success_task.pk, '{"success": true, "result": 3}'),
                    (
                        failure_task.pk,
                        (
                            '{"success": false, "result": null, "error": "boom", '
                            '"traceback": "tb", "exception_type": "RuntimeError"}'
                        ),
                    ),
                    (999999, '{"success": true, "result": 1}'),
                    (bad_json_task.pk, "not-json"),
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

        class FakeRunner:
            def submit(self, **_kwargs):
                return SubmissionHandle(
                    ray_job_id="raysubmit_coverage_001",
                    ray_address="ray://cluster:10001",
                    submitted_at=datetime.now(UTC),
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        task.refresh_from_db()
        assert task.ray_job_id == "raysubmit_coverage_001"
        assert task.ray_address == "ray://cluster:10001"
        assert cmd.active_tasks[task.pk] == "raysubmit_coverage_001"

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

        class FakeRunner:
            def submit(self, **kwargs: Any) -> SubmissionHandle:
                captured.update(kwargs)
                return SubmissionHandle(
                    ray_job_id="raysubmit_reference_001",
                    ray_address="ray://cluster:10001",
                    submitted_at=datetime.now(UTC),
                )

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        cmd.submit_task_to_ray(task)

        assert captured["args"] == ()
        assert captured["kwargs"] == {}
        assert captured["task_execution"].input_reference == reference

    def test_reconcile_tasks_returns_early_for_sync_or_empty(self) -> None:
        cmd = _make_command()
        cmd.sync_mode = True
        cmd.active_tasks = {1: "raysubmit_x"}
        cmd.reconcile_tasks()

        cmd.sync_mode = False
        cmd.active_tasks = {}
        cmd.reconcile_tasks()

    def test_reconcile_tasks_handles_cancelling_task(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="reconcile-cancelling-001",
            callable_path="testproject.tasks.add_numbers",
            queue_name="default",
            state=TaskState.CANCELLING,
            args_json="[1, 2]",
            kwargs_json="{}",
            ray_job_id="raysubmit_cancelling_001",
        )
        cmd = _make_command()
        cmd.active_tasks = {task.pk: "raysubmit_cancelling_001"}

        cmd.reconcile_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLED
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
                "expected_execution_generation": 0,
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

    def test_detect_stuck_tasks_recovers_unknown_active_ray_job_when_monitor_heartbeat_stale(
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

        cmd.detect_stuck_tasks()

        task.refresh_from_db()
        assert task.state == TaskState.QUEUED
        assert task.attempt_number == 2
        assert task.run_after is not None

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

    def test_reconcile_terminal_status_honors_cancellation_race(self) -> None:
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
        assert task.state == TaskState.CANCELLED
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
