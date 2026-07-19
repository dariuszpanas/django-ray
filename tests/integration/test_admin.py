"""Integration tests for django-ray admin actions and display helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib import admin
from django.test import RequestFactory, override_settings

from django_ray.admin import RayTaskExecutionAdmin, TaskWorkerLeaseAdmin
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease


def _request() -> Any:
    return RequestFactory().post("/admin/")


def _task_admin() -> RayTaskExecutionAdmin:
    return RayTaskExecutionAdmin(RayTaskExecution, admin.site)


def _lease_admin() -> TaskWorkerLeaseAdmin:
    return TaskWorkerLeaseAdmin(TaskWorkerLease, admin.site)


@pytest.mark.django_db
class TestRayTaskExecutionAdmin:
    """Tests for task admin formatting and actions."""

    def test_ray_job_display_variants(self) -> None:
        admin_obj = _task_admin()

        task = RayTaskExecution.objects.create(
            task_id="admin-display-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
        )
        assert admin_obj.ray_job_id_display(task) == "Not yet submitted"
        assert admin_obj.ray_dashboard_link(task) == "-"

        task.ray_job_id = "ray_core:123"
        assert admin_obj.ray_job_id_display(task) == "N/A (legacy format)"
        assert "Jobs" in admin_obj.ray_dashboard_link(task)

        task.ray_job_id = "02000000:abcdef1234567890"
        display = admin_obj.ray_job_id_display(task)
        assert "Job: 02000000" in display
        assert "/#/jobs/02000000/tasks/abcdef1234567890" in display
        link = admin_obj.ray_dashboard_link(task)
        assert "Task" in link
        assert "/#/jobs/02000000/tasks/abcdef1234567890" in link

        task.ray_job_id = "raysubmit_abc123"
        display = admin_obj.ray_job_id_display(task)
        assert "raysubmit_abc123" in display
        assert "/#/jobs/raysubmit_abc123" in display
        link = admin_obj.ray_dashboard_link(task)
        assert "/#/jobs/raysubmit_abc123" in link

    @pytest.mark.django_db
    def test_sensitive_task_fields_are_redacted(self, settings) -> None:
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        admin_obj = _task_admin()
        task = RayTaskExecution.objects.create(
            task_id="admin-redacted-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            args_json='[{"password":"admin-secret"}]',
            result_data='{"password":"result-secret"}',
            error_message="password=error-secret",
        )

        rendered = " ".join(
            (
                admin_obj.args_json_display(task),
                admin_obj.result_data_display(task),
                admin_obj.error_message_display(task),
            )
        )

        assert "admin-secret" not in rendered
        assert "result-secret" not in rendered
        assert "error-secret" not in rendered
        assert "[REDACTED]" in rendered

    @override_settings(RAY_DASHBOARD_URL="http://ray.localhost:30080")
    def test_dashboard_links_respect_configured_dashboard_url(self) -> None:
        admin_obj = _task_admin()
        task = RayTaskExecution.objects.create(
            task_id="admin-display-002",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            ray_job_id="02000000:abcdef1234567890",
            args_json="[]",
            kwargs_json="{}",
        )

        display = admin_obj.ray_job_id_display(task)
        link = admin_obj.ray_dashboard_link(task)

        assert 'href="http://ray.localhost:30080/#/jobs/02000000/tasks/abcdef1234567890"' in display
        assert 'href="http://ray.localhost:30080/#/jobs/02000000/tasks/abcdef1234567890"' in link

    def test_retry_tasks_requeues_failed_and_lost(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        failed = RayTaskExecution.objects.create(
            task_id="admin-retry-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=3,
            execution_generation=8,
            error_message="boom",
            args_json="[]",
            kwargs_json="{}",
            completion_data='{"success": false}',
        )
        lost = RayTaskExecution.objects.create(
            task_id="admin-retry-002",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.LOST,
            attempt_number=2,
            error_message="lost",
            args_json="[]",
            kwargs_json="{}",
        )
        running = RayTaskExecution.objects.create(
            task_id="admin-retry-003",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            args_json="[]",
            kwargs_json="{}",
        )

        qs = RayTaskExecution.objects.filter(pk__in=[failed.pk, lost.pk, running.pk])
        admin_obj.retry_tasks(_request(), qs)

        failed.refresh_from_db()
        lost.refresh_from_db()
        running.refresh_from_db()

        assert failed.state == TaskState.QUEUED
        assert failed.attempt_number == 4
        assert failed.execution_generation == 9
        assert failed.completion_data is None
        assert lost.state == TaskState.QUEUED
        assert lost.attempt_number == 3
        assert running.state == TaskState.RUNNING
        assert messages[-1] == "Queued 2 task(s) for retry."
        assert TaskAttempt.objects.get(execution=failed, attempt_number=3).error_message == "boom"
        assert TaskAttempt.objects.get(execution=lost, attempt_number=2).error_message == "lost"

    def test_retry_tasks_noop_when_nothing_retryable(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        task = RayTaskExecution.objects.create(
            task_id="admin-retry-004",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="[]",
            kwargs_json="{}",
        )

        admin_obj.retry_tasks(_request(), RayTaskExecution.objects.filter(pk=task.pk))

        assert messages[-1] == "No failed or lost tasks found in selection."

    def test_cancel_tasks_handles_queued_and_running_paths(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        queued = RayTaskExecution.objects.create(
            task_id="admin-cancel-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
            args_json="[]",
            kwargs_json="{}",
        )
        running_ray_job = RayTaskExecution.objects.create(
            task_id="admin-cancel-002",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.RUNNING,
            ray_job_id="raysubmit_cancel_admin",
            ray_address="ray://cluster:10001",
            started_at=datetime.now(UTC) - timedelta(seconds=5),
            args_json="[]",
            kwargs_json="{}",
        )
        running_core = RayTaskExecution.objects.create(
            task_id="admin-cancel-003",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.RUNNING,
            ray_job_id="02000000:abcdef",
            args_json="[]",
            kwargs_json="{}",
        )

        seen_handles: list[str] = []

        class FakeRunner:
            def cancel(self, handle) -> bool:
                seen_handles.append(str(handle.ray_job_id))
                return True

        monkeypatch.setattr("django_ray.runner.ray_job.RayJobRunner", FakeRunner)

        qs = RayTaskExecution.objects.filter(
            pk__in=[queued.pk, running_ray_job.pk, running_core.pk]
        )
        admin_obj.cancel_tasks(_request(), qs)

        queued.refresh_from_db()
        running_ray_job.refresh_from_db()
        running_core.refresh_from_db()

        assert queued.state == TaskState.CANCELLED
        assert queued.finished_at is not None
        assert running_ray_job.state == TaskState.CANCELLING
        assert running_core.state == TaskState.CANCELLING
        assert seen_handles == ["raysubmit_cancel_admin"]
        assert messages[-1] == "Marked 3 task(s) for cancellation. Attempted to stop 1 Ray job(s)."

    def test_cancel_tasks_noop_when_nothing_cancellable(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        failed = RayTaskExecution.objects.create(
            task_id="admin-cancel-004",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            args_json="[]",
            kwargs_json="{}",
        )

        admin_obj.cancel_tasks(_request(), RayTaskExecution.objects.filter(pk=failed.pk))
        assert messages[-1] == "No queued or running tasks found in selection."


@pytest.mark.django_db
class TestTaskWorkerLeaseAdmin:
    """Tests for worker lease admin helper behavior."""

    def test_worker_id_short_and_time_since_heartbeat(self) -> None:
        admin_obj = _lease_admin()
        lease = TaskWorkerLease.objects.create(
            worker_id="worker-1234567890abcdef",
            hostname="host-a",
            pid=1111,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=30),
            is_active=True,
        )

        assert admin_obj.worker_id_short(lease) == "worker-12345..."
        assert "ago" in admin_obj.time_since_heartbeat(lease)

        lease.last_heartbeat_at = None
        assert admin_obj.time_since_heartbeat(lease) == "Never"

    def test_mark_inactive_and_delete_inactive_actions(self, monkeypatch) -> None:
        admin_obj = _lease_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        active = TaskWorkerLease.objects.create(
            worker_id="active-worker",
            hostname="host-b",
            pid=2222,
            queue_name="default",
            is_active=True,
        )
        inactive = TaskWorkerLease.objects.create(
            worker_id="inactive-worker",
            hostname="host-c",
            pid=3333,
            queue_name="default",
            is_active=False,
        )

        qs = TaskWorkerLease.objects.filter(worker_id__in=[active.worker_id, inactive.worker_id])
        admin_obj.mark_inactive(_request(), qs)
        active.refresh_from_db()
        assert active.is_active is False
        assert active.stopped_at is not None
        assert messages[-1] == "Marked 1 worker lease(s) as inactive."

        admin_obj.delete_inactive(_request(), qs)
        assert not TaskWorkerLease.objects.filter(worker_id=active.worker_id).exists()
        assert not TaskWorkerLease.objects.filter(worker_id=inactive.worker_id).exists()
        assert messages[-1] == "Deleted 2 inactive worker lease(s)."

    def test_permissions_are_disabled(self) -> None:
        admin_obj = _lease_admin()
        request = _request()

        assert admin_obj.has_add_permission(request) is False
        assert admin_obj.has_change_permission(request) is False
