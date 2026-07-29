"""Integration tests for django-ray admin actions and display helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import Http404
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django_ray.admin import (
    ADMIN_ATTEMPT_INLINE_MAX_CHARS,
    ADMIN_DIAGNOSTIC_MAX_CHARS,
    ActiveWorkerFilter,
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskAttemptInline,
    TaskWorkerLeaseAdmin,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.workflow_plans import (
    PLAN_SELECTION_FORMAT,
    PLAN_SELECTION_FORMAT_VERSION,
)
from django_ray.workflow_progress_reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
)
from django_ray.workflow_progress_summary import serialize_workflow_progress_summary
from tests.workflow_progress_summary_helpers import workflow_progress_summary

_ADMIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]


def _request() -> Any:
    return RequestFactory().post("/admin/")


def _task_admin() -> RayTaskExecutionAdmin:
    return RayTaskExecutionAdmin(RayTaskExecution, admin.site)


def _lease_admin() -> TaskWorkerLeaseAdmin:
    return TaskWorkerLeaseAdmin(TaskWorkerLease, admin.site)


def _attempt_admin() -> TaskAttemptAdmin:
    return TaskAttemptAdmin(TaskAttempt, admin.site)


@pytest.mark.django_db
class TestRayTaskExecutionAdmin:
    """Tests for task admin formatting and actions."""

    def test_attempt_presentation_mode_is_selected_per_request(
        self,
        settings,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-mode-runtime",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_superuser(username="attempt-mode-runtime")
        request = RequestFactory().get("/admin/")
        request.user = user
        task_admin = _task_admin()
        attempt_admin = _attempt_admin()

        for mode, has_inline, has_module in (
            ("inline", True, False),
            ("standalone", False, True),
            ("both", True, True),
        ):
            settings.DJANGO_RAY = {
                "RAY_ADDRESS": "ray://localhost:10001",
                "TASK_ATTEMPT_ADMIN_MODE": mode,
            }
            inlines = task_admin.get_inlines(request, execution)

            assert (TaskAttemptInline in inlines) is has_inline
            assert task_admin.get_inlines(request, None) == []
            assert attempt_admin.has_module_permission(request) is has_module

    def test_changelist_prioritizes_compact_operational_fields(self) -> None:
        admin_obj = _task_admin()

        assert admin_obj.list_display == [
            "id",
            "state_display",
            "task_display",
            "queue_display",
            "priority",
            "attempt_display",
            "ray_dashboard_link",
            "created_display",
            "started_display",
            "finished_display",
        ]
        assert admin_obj.list_fullwidth is True
        expected_display_metadata = {
            "state_display": ("State", "state"),
            "task_display": ("Task", "callable_path"),
            "queue_display": ("Queue", "queue_name"),
            "attempt_display": ("Attempt", "attempt_number"),
            "created_display": ("Created", "created_at"),
            "started_display": ("Started", "started_at"),
            "finished_display": ("Finished", "finished_at"),
            "ray_dashboard_link": ("Ray", None),
        }
        for method_name, (description, ordering) in expected_display_metadata.items():
            method = getattr(admin_obj, method_name)
            assert method.short_description == description
            assert getattr(method, "admin_order_field", None) == ordering

    @override_settings(TIME_ZONE="UTC")
    def test_compact_changelist_values_preserve_full_context(self) -> None:
        timestamp = datetime(2026, 7, 29, 21, 53, 12, tzinfo=UTC)
        task = RayTaskExecution(
            callable_path="testproject.tasks.a_callable_name_that_is_intentionally_long",
            queue_name="an-intentionally-long-queue-name",
            attempt_number=3,
            created_at=timestamp,
            started_at=timestamp,
            finished_at=None,
        )
        admin_obj = _task_admin()

        task_display = str(admin_obj.task_display(task))
        queue_display = str(admin_obj.queue_display(task))
        created_display = str(admin_obj.created_display(task))

        assert str(task.callable_path) in task_display
        assert "a_callable_name" in task_display
        assert "intentionally_long" in task_display
        assert str(task.queue_name) in queue_display
        assert "…" in queue_display
        assert 'datetime="2026-07-29T21:53:12+00:00"' in created_display
        assert 'title="2026-07-29 21:53:12 UTC"' in created_display
        assert ">2026-07-29 21:53</time>" in created_display
        assert admin_obj.attempt_display(task) == 3
        assert admin_obj.finished_display(task) == "-"

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

    def test_runtime_env_snapshot_is_not_presented_in_admin(self) -> None:
        admin_obj = _task_admin()
        fieldset_fields = {
            field for _, options in admin_obj.fieldsets for field in options.get("fields", ())
        }

        assert "runtime_env_json" not in admin_obj.readonly_fields
        assert "runtime_env_json" not in fieldset_fields
        assert {"runtime_env_profile", "runtime_env_hash"} <= fieldset_fields

        execution = RayTaskExecution.objects.create(
            task_id="admin-runtime-env-confidential-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.QUEUED,
            runtime_env_profile="runtime-env-profile-marker",
            runtime_env_hash="b" * 64,
            runtime_env_json=json.dumps(
                {
                    "env_vars": {
                        "DISPLAY_NAME": "ordinary-runtime-secret-marker",
                    },
                    "working_dir": (
                        "https://user:pass@private.example/archive.zip"
                        "?signature=signed-runtime-query-marker"
                    ),
                    "config": "<script>runtime-script-marker</script>",
                }
            ),
        )
        user = get_user_model().objects.create_superuser(
            username="runtime-env-confidential-admin",
        )
        change_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        request = RequestFactory().get(change_url)
        request.user = user

        response = admin_obj.change_view(request, str(execution.pk))
        response.render()
        content = response.content.decode("utf-8")

        assert response.status_code == 200
        assert "runtime-env-profile-marker" in content
        assert "b" * 64 in content
        assert "Runtime env json" not in content
        assert "field-runtime_env_json" not in content
        assert "ordinary-runtime-secret-marker" not in content
        assert "user:pass@private.example" not in content
        assert "signed-runtime-query-marker" not in content
        assert "runtime-script-marker" not in content
        assert "RuntimeEnv values are intentionally not displayed" in content

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_execution_change_form_is_read_only_and_rejects_tampering(
        self,
        settings,
    ) -> None:
        admin_obj = _task_admin()
        execution = RayTaskExecution.objects.create(
            task_id="admin-read-only-execution-001",
            callable_path="testproject.tasks.add_numbers",
            priority=7,
            queue_name="default",
            state=TaskState.QUEUED,
            attempt_number=2,
            execution_generation=3,
            claimed_by_worker=None,
        )
        user = get_user_model().objects.create_superuser(
            username="read-only-execution-admin",
        )
        request = RequestFactory().get("/admin/")
        request.user = user
        fieldset_fields = {
            field for _, options in admin_obj.fieldsets for field in options.get("fields", ())
        }

        assert fieldset_fields <= set(admin_obj.readonly_fields)
        assert admin_obj.get_form(request, execution).base_fields == {}
        assert admin_obj.has_add_permission(request) is False
        assert admin_obj.has_change_permission(request) is True
        assert admin_obj.has_change_permission(request, execution) is False
        assert admin_obj.has_delete_permission(request, execution) is False
        assert set(admin_obj.get_actions(request)) == {"retry_tasks", "cancel_tasks"}

        client = Client()
        client.force_login(user)
        change_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        read_response = client.get(change_url)
        read_content = read_response.content.decode("utf-8")

        assert read_response.status_code == 200
        assert "Execution metadata is read-only" in read_content
        assert 'name="_save"' not in read_content
        assert 'name="_continue"' not in read_content
        assert (
            reverse(
                "admin:django_ray_raytaskexecution_delete",
                args=[execution.pk],
            )
            not in read_content
        )
        for field in (
            "priority",
            "queue_name",
            "state",
            "attempt_number",
            "execution_generation",
            "claimed_by_worker",
        ):
            assert f'name="{field}"' not in read_content

        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "TASK_ATTEMPT_ADMIN_MODE": "standalone",
        }
        tamper_response = client.post(
            change_url,
            {
                "priority": 100,
                "queue_name": "ml",
                "state": TaskState.SUCCEEDED,
                "attempt_number": 99,
                "execution_generation": 99,
                "claimed_by_worker": "forged-worker",
                "_save": "Save",
            },
        )
        execution.refresh_from_db()

        assert tamper_response.status_code == 403
        assert execution.priority == 7
        assert execution.queue_name == "default"
        assert execution.state == TaskState.QUEUED
        assert execution.attempt_number == 2
        assert execution.execution_generation == 3
        assert execution.claimed_by_worker is None

    def test_display_helpers_handle_empty_invalid_and_complete_values(self) -> None:
        admin_obj = _task_admin()
        task = RayTaskExecution.objects.create(
            task_id="admin-display-helpers-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json="",
            kwargs_json='{"value": 1}',
            result_data="not-json password=secret",
            progress_data='{"step": 1}',
            completion_data='{"success": true}',
            error_message="password=error-secret",
            error_traceback="password=traceback-secret",
        )

        assert admin_obj.args_json_display(task) == "-"
        assert admin_obj.kwargs_json_display(task) == '{"value": 1}'
        assert admin_obj.result_data_display(task) == "[REDACTED]"
        assert admin_obj.progress_data_display(task) == '{"step": 1}'
        assert admin_obj.completion_data_display(task) == '{"success": true}'
        assert admin_obj.error_message_display(task) == "[REDACTED]"
        assert admin_obj.error_traceback_display(task) == "[REDACTED]"

        task.error_message = None
        task.error_traceback = None
        assert admin_obj.error_message_display(task) == "-"
        assert admin_obj.error_traceback_display(task) == "-"

    def test_routine_admin_queryset_defers_complete_progress_payloads(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="admin-bounded-change-form-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="legacy-graph" * 10_000,
            workflow_progress_summary_json="summary" * 10_000,
        )
        admin_obj = _task_admin()

        with CaptureQueriesContext(connection) as queries:
            loaded = admin_obj.get_queryset(_request()).get(pk=task.pk)

        assert loaded.pk == task.pk
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 1
        assert "progress_data" not in task_selects[0]
        assert "runtime_env_json" not in task_selects[0]
        assert "workflow_progress_summary_json" not in task_selects[0]
        assert "progress_data_display" not in admin_obj.readonly_fields

    @pytest.mark.parametrize(
        "state",
        [
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.CANCELLING,
            TaskState.LOST,
        ],
    )
    def test_state_display_formats_each_known_state(self, state: str) -> None:
        task = RayTaskExecution.objects.create(
            task_id=f"admin-state-{state}",
            callable_path="testproject.tasks.add_numbers",
            state=state,
            args_json="[]",
            kwargs_json="{}",
        )

        rendered = _task_admin().state_display(task)

        assert state in rendered
        assert "font-weight: bold" in rendered

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

    def test_dashboard_links_escape_untrusted_ray_identifiers(self) -> None:
        task = RayTaskExecution.objects.create(
            task_id="admin-display-escaped",
            callable_path="testproject.tasks.add_numbers",
            ray_job_id='job:<img src=x onerror="alert(1)">',
        )

        rendered = str(_task_admin().ray_job_id_display(task))
        link = str(_task_admin().ray_dashboard_link(task))

        assert "<img" not in rendered
        assert "<img" not in link
        assert "&lt;img" in rendered
        assert "%3Cimg" not in link

    def test_observability_endpoint_requires_admin_access(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-live-auth-001",
            callable_path="testproject.tasks.add_numbers",
        )
        endpoint = reverse(
            "admin:django_ray_raytaskexecution_observability",
            args=[execution.pk],
        )

        admin_obj = _task_admin()
        wrapped_view = admin_obj.admin_site.admin_view(admin_obj.observability_view)
        user_model = get_user_model()
        ordinary_user = user_model.objects.create_user(username="observability-ordinary-user")
        staff_user = user_model.objects.create_user(
            username="observability-staff-no-permission",
            is_staff=True,
        )
        anonymous_request = RequestFactory().get(endpoint)
        anonymous_request.user = AnonymousUser()
        nonstaff_request = RequestFactory().get(endpoint)
        nonstaff_request.user = ordinary_user
        denied_request = RequestFactory().get(endpoint)
        denied_request.user = staff_user

        anonymous_response = wrapped_view(anonymous_request, str(execution.pk))
        nonstaff_response = wrapped_view(nonstaff_request, str(execution.pk))

        assert anonymous_response.status_code == 302
        assert "/admin/login/" in anonymous_response.url
        assert nonstaff_response.status_code == 302
        with pytest.raises(PermissionDenied):
            wrapped_view(denied_request, str(execution.pk))

    def test_observability_endpoint_honors_object_permission_backend(
        self,
        monkeypatch,
    ) -> None:
        allowed = RayTaskExecution.objects.create(
            task_id="admin-object-allowed",
            callable_path="testproject.tasks.add_numbers",
        )
        denied = RayTaskExecution.objects.create(
            task_id="admin-object-denied",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_user(
            username="observability-object-viewer",
            is_staff=True,
        )
        checked_objects: list[int] = []

        def has_perm(permission, obj=None):
            del permission
            if obj is None:
                return False
            checked_objects.append(obj.pk)
            return obj.pk == allowed.pk

        monkeypatch.setattr(user, "has_perm", has_perm)
        admin_obj = _task_admin()
        allowed_request = RequestFactory().get("/admin/live/")
        allowed_request.user = user
        denied_request = RequestFactory().get("/admin/live/")
        denied_request.user = user

        assert admin_obj.has_view_permission(allowed_request) is False
        response = admin_obj.observability_view(allowed_request, str(allowed.pk))

        assert response.status_code == 200
        with pytest.raises(PermissionDenied):
            admin_obj.observability_view(denied_request, str(denied.pk))
        assert allowed.pk in checked_objects
        assert denied.pk in checked_objects

    def test_observability_endpoint_returns_bounded_durable_summary(self, settings) -> None:
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        execution = RayTaskExecution.objects.create(
            task_id="admin-live-summary-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            attempt_number=2,
            error_message="password=admin-live-secret",
        )
        user_model = get_user_model()
        staff_user = user_model.objects.create_user(
            username="observability-staff-viewer",
            is_staff=True,
        )
        staff_user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="django_ray",
                codename="view_raytaskexecution",
            )
        )
        endpoint = reverse(
            "admin:django_ray_raytaskexecution_observability",
            args=[execution.pk],
        )

        request = RequestFactory().get(endpoint)
        request.user = staff_user
        admin_obj = _task_admin()
        response = admin_obj.admin_site.admin_view(admin_obj.observability_view)(
            request,
            str(execution.pk),
        )

        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        payload = json.loads(response.content)
        assert payload["schema_version"] == 1
        assert payload["id"] == execution.pk
        assert payload["state"] == TaskState.SUCCEEDED
        assert payload["attempt_number"] == 2
        assert payload["error_message"] == "[REDACTED]"
        assert payload["workflow"] is None
        assert "admin-live-secret" not in response.content.decode("utf-8")
        post_request = RequestFactory().post(endpoint)
        post_request.user = staff_user
        assert admin_obj.observability_view(post_request, str(execution.pk)).status_code == 405

    def test_observability_endpoint_returns_not_found_for_missing_object(
        self,
    ) -> None:
        user_model = get_user_model()
        superuser = user_model.objects.create_superuser(
            username="observability-missing-superuser",
        )
        endpoint = reverse(
            "admin:django_ray_raytaskexecution_observability",
            args=[999_999],
        )

        request = RequestFactory().get(endpoint)
        request.user = superuser

        with pytest.raises(Http404):
            _task_admin().observability_view(request, "999999")

    def test_observability_endpoint_maps_service_not_found_to_http_404(
        self,
        monkeypatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-progress-read-race-001",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_superuser(username="progress-race-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        def missing(*args, **kwargs):
            del args, kwargs
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.NOT_FOUND)

        monkeypatch.setattr("django_ray.admin.get_workflow_progress_summary", missing)

        with pytest.raises(Http404):
            _task_admin().observability_view(request, str(execution.pk))

    def test_observability_endpoint_ignores_invalid_legacy_progress(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-invalid-workflow-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="not-json password=workflow-secret",
        )
        user_model = get_user_model()
        superuser = user_model.objects.create_superuser(
            username="observability-invalid-workflow-superuser",
        )
        request = RequestFactory().get("/admin/live/")
        request.user = superuser

        response = _task_admin().observability_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["state"] == TaskState.RUNNING
        assert payload["workflow"] is None
        assert payload["workflow_availability"] == "NOT_REPORTED"
        assert "workflow_error" not in payload
        assert "workflow-secret" not in response.content.decode("utf-8")

    def test_observability_endpoint_does_not_map_legacy_graph_aggregates(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-summary-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data=json.dumps(
                {
                    "schema_version": 1,
                    "revision": 4,
                    "state": "RUNNING",
                    "total_nodes": 2,
                    "completed_nodes": 1,
                    "failed_nodes": 0,
                    "running_nodes": 1,
                    "pending_nodes": 0,
                    "progress_percent": 50.0,
                    "graph": {"nodes": [{"secret": "node-secret"}], "edges": []},
                    "recent_events": [{"secret": "event-secret"}],
                    "runtime_env": {"secret": "runtime-secret"},
                }
            ),
        )
        user = get_user_model().objects.create_superuser(username="workflow-summary-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        payload = json.loads(_task_admin().observability_view(request, str(execution.pk)).content)

        assert payload["workflow"] is None
        assert payload["workflow_availability"] == "NOT_REPORTED"
        assert "secret" not in json.dumps(payload)

    def test_terminal_full_workflow_without_v3_is_missing_not_empty(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-missing-workflow-v3",
            callable_path="testproject.tasks.run_workflow_benchmark",
            state=TaskState.SUCCEEDED,
            workflow_plan_selection=json.dumps(
                {
                    "plan_selection_format": PLAN_SELECTION_FORMAT,
                    "plan_selection_format_version": PLAN_SELECTION_FORMAT_VERSION,
                    "requested_policy": "auto",
                    "selected_strategy": "dynamic_tasks",
                    "reporting_policy": "full",
                    "eligible_strategies": ["dynamic_tasks"],
                    "rejections": [],
                    "total_rejections": 0,
                    "rejections_truncated": False,
                }
            ),
        )
        user = get_user_model().objects.create_superuser(username="missing-workflow-v3-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        summary_response = _task_admin().observability_view(
            request,
            str(execution.pk),
        )
        summary = json.loads(summary_response.content)
        topology_response = _task_admin().workflow_topology_nodes_view(
            request,
            str(execution.pk),
        )
        topology = json.loads(topology_response.content)

        assert summary_response.status_code == 200
        assert summary["workflow"] is None
        assert summary["workflow_availability"] == "MISSING"
        assert topology_response.status_code == 409
        assert topology["code"] == "MISSING"

    def test_observability_endpoint_defers_payloads_and_reads_progress_once(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-bounded-query-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data=json.dumps({"schema_version": 1, "revision": 4}),
            args_json=json.dumps(["private-input"]),
        )
        user = get_user_model().objects.create_superuser(username="bounded-query-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        with CaptureQueriesContext(connection) as queries:
            response = _task_admin().observability_view(request, str(execution.pk))

        assert response.status_code == 200
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        assert all("progress_data" not in query for query in task_selects)
        assert all("args_json" not in query for query in task_selects)
        assert any("workflow_progress_summary_json" in query for query in task_selects)
        detail_tables = (
            "django_ray_workflowprogressrunstorage",
            "django_ray_workflowprogresstopologymanifest",
            "django_ray_workflowprogresstopologypage",
            "django_ray_workflowprogressnodedetail",
        )
        assert all(
            table not in query for query in queries.captured_queries for table in detail_tables
        )

    def test_observability_endpoint_bounds_invalid_summary_without_legacy_fallback(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-invalid-summary-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="password=legacy-secret" * 1_000,
            workflow_progress_summary_json="{" + ("x" * 20_000),
        )
        user = get_user_model().objects.create_superuser(username="invalid-summary-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        with CaptureQueriesContext(connection) as queries:
            response = _task_admin().observability_view(request, str(execution.pk))

        payload = json.loads(response.content)
        assert response.status_code == 200
        assert payload["workflow"] is None
        assert payload["workflow_error_code"] == "CORRUPT"
        assert len(payload["workflow_error"]) <= ADMIN_DIAGNOSTIC_MAX_CHARS
        assert "legacy-secret" not in response.content.decode("utf-8")
        assert all("progress_data" not in query["sql"] for query in queries.captured_queries)

    def test_authorized_admin_detail_views_call_bounded_services(
        self,
        monkeypatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-detail-links-001",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_superuser(username="detail-links-admin")
        admin_obj = _task_admin()
        called: list[str] = []

        def page_payload(collection):
            return {
                "schema": "django-ray.workflow-progress-page",
                "schema_version": 1,
                "generated_at": "2026-07-20T12:00:00Z",
                "task_id": execution.task_id,
                "run_identity": None,
                "publication": {
                    "summary_revision": None,
                    "topology_version": None,
                    "detail_revision": None,
                },
                "availability": "NOT_REPORTED",
                "complete": False,
                "collection": collection,
                "returned_count": 0,
                "items": [],
                "next_cursor": None,
            }

        def fake_nodes(candidate, *, authorize, **kwargs):
            del kwargs
            assert authorize(candidate) is True
            called.append("topology_nodes")
            return page_payload("topology_nodes")

        def fake_edges(candidate, *, authorize, **kwargs):
            del kwargs
            assert authorize(candidate) is True
            called.append("topology_edges")
            return page_payload("topology_edges")

        def fake_details(candidate, *, authorize, **kwargs):
            del kwargs
            assert authorize(candidate) is True
            called.append("node_details")
            return page_payload("node_details")

        def fake_node(candidate, node_id, *, authorize, **kwargs):
            del kwargs
            assert node_id == "node/one"
            assert authorize(candidate) is True
            called.append("node")
            return {
                **page_payload("node_details"),
                "schema": "django-ray.workflow-progress-node",
                "found": False,
                "item": None,
            }

        monkeypatch.setattr("django_ray.admin.list_workflow_topology_nodes", fake_nodes)
        monkeypatch.setattr("django_ray.admin.list_workflow_topology_edges", fake_edges)
        monkeypatch.setattr("django_ray.admin.list_workflow_node_details", fake_details)
        monkeypatch.setattr("django_ray.admin.get_workflow_node_detail", fake_node)

        with CaptureQueriesContext(connection) as queries:
            for view in (
                admin_obj.workflow_topology_nodes_view,
                admin_obj.workflow_topology_edges_view,
                admin_obj.workflow_node_details_view,
            ):
                request = RequestFactory().get("/admin/workflow/?limit=10")
                request.user = user
                response = view(request, str(execution.pk))
                assert response.status_code == 200
                assert response["Cache-Control"] == "no-store"
            request = RequestFactory().get("/admin/workflow/node/?node_id=node%2Fone")
            request.user = user
            response = admin_obj.workflow_node_detail_view(request, str(execution.pk))
            assert response.status_code == 200
        assert called == ["topology_nodes", "topology_edges", "node_details", "node"]
        task_selects = [
            query["sql"].lower()
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(task_selects) == 4
        payload_fields = (
            "runtime_env_json",
            "args_json",
            "kwargs_json",
            "result_data",
            "progress_data",
            "workflow_progress_summary_json",
            "workflow_plan_json",
            "workflow_plan_selection",
            "completion_data",
            "cancellation_error",
            "error_message",
            "error_traceback",
        )
        assert all(field not in query for query in task_selects for field in payload_fields)

        invalid_limit = RequestFactory().get("/admin/workflow/?limit=not-an-integer")
        invalid_limit.user = user
        response = admin_obj.workflow_topology_nodes_view(
            invalid_limit,
            str(execution.pk),
        )
        assert response.status_code == 400
        assert json.loads(response.content)["code"] == "INVALID_ARGUMENT"

        assert admin_obj._page_limit(RequestFactory().get("/admin/workflow/")) == 100

        denied_user = get_user_model().objects.create_user(
            username="bounded-detail-denied-staff",
            is_staff=True,
        )
        denied_page = RequestFactory().get(
            "/admin/workflow/?limit=invalid&attempt_number=invalid&cursor=invalid&state=INVALID"
        )
        denied_page.user = denied_user
        with pytest.raises(PermissionDenied):
            admin_obj.workflow_node_details_view(denied_page, str(execution.pk))
        denied_node = RequestFactory().get("/admin/workflow/node/?attempt_number=invalid")
        denied_node.user = denied_user
        with pytest.raises(PermissionDenied):
            admin_obj.workflow_node_detail_view(denied_node, str(execution.pk))
        assert called == ["topology_nodes", "topology_edges", "node_details", "node"]

        post = RequestFactory().post("/admin/workflow/")
        post.user = user
        assert admin_obj.workflow_topology_nodes_view(post, str(execution.pk)).status_code == 405
        assert admin_obj.workflow_node_detail_view(post, str(execution.pk)).status_code == 405

        def corrupt_page(*args, **kwargs):
            del args, kwargs
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.CORRUPT)

        monkeypatch.setattr("django_ray.admin.list_workflow_topology_nodes", corrupt_page)
        request = RequestFactory().get("/admin/workflow/")
        request.user = user
        response = admin_obj.workflow_topology_nodes_view(request, str(execution.pk))
        assert response.status_code == 503

        def missing_node_detail(*args, **kwargs):
            del args, kwargs
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.MISSING)

        monkeypatch.setattr("django_ray.admin.get_workflow_node_detail", missing_node_detail)
        request = RequestFactory().get("/admin/workflow/node/?node_id=node%2Fone")
        request.user = user
        response = admin_obj.workflow_node_detail_view(request, str(execution.pk))
        assert response.status_code == 409

        missing_node = RequestFactory().get("/admin/workflow/node/")
        missing_node.user = user
        response = admin_obj.workflow_node_detail_view(missing_node, str(execution.pk))
        assert response.status_code == 400
        assert json.loads(response.content)["code"] == "INVALID_ARGUMENT"

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            (WorkflowProgressReadErrorCode.INVALID_ARGUMENT, 400),
            (WorkflowProgressReadErrorCode.INVALID_CURSOR, 400),
            (WorkflowProgressReadErrorCode.CURSOR_MISMATCH, 409),
            (WorkflowProgressReadErrorCode.MISSING, 409),
            (WorkflowProgressReadErrorCode.CORRUPT, 503),
        ],
    )
    def test_admin_detail_errors_keep_bounded_codes(
        self,
        code: WorkflowProgressReadErrorCode,
        status: int,
    ) -> None:
        response = _task_admin()._workflow_read_error_response(WorkflowProgressReadError(code))

        assert response.status_code == status
        assert response["Cache-Control"] == "no-store"
        assert json.loads(response.content) == {
            "code": code.value,
            "message": str(WorkflowProgressReadError(code)),
        }

    def test_admin_detail_access_and_missing_errors_raise(self) -> None:
        admin_obj = _task_admin()

        with pytest.raises(PermissionDenied):
            admin_obj._workflow_read_error_response(
                WorkflowProgressReadError(WorkflowProgressReadErrorCode.ACCESS_DENIED)
            )
        with pytest.raises(Http404):
            admin_obj._workflow_read_error_response(
                WorkflowProgressReadError(WorkflowProgressReadErrorCode.NOT_FOUND)
            )

    def test_observability_endpoint_maps_bounded_v3_summary(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-v3-summary-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000000125",
        )
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
            workflow_progress_summary(execution, published_detail=True)
        )
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(username="v3-summary-admin")
        request = RequestFactory().get("/admin/live/")
        request.user = user

        payload = json.loads(_task_admin().observability_view(request, str(execution.pk)).content)

        assert payload["workflow_revision"] == 1
        assert payload["workflow"]["revision"] == 1
        assert payload["workflow"]["total_nodes"] == 1
        assert payload["workflow"]["pending_nodes"] == 1
        assert payload["workflow"]["detail"]["availability"] == "AVAILABLE"
        assert payload["workflow_availability"] == "AVAILABLE"
        assert "task_execution_pk" not in payload["workflow"]["run_identity"]

    def test_change_form_loads_live_status_panel_and_package_script(
        self,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-live-form-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="legacy-graph" * 10_000,
            workflow_progress_summary_json="summary" * 10_000,
        )
        user_model = get_user_model()
        superuser = user_model.objects.create_superuser(
            username="observability-form-superuser",
        )
        change_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        request = RequestFactory().get(change_url)
        request.user = superuser
        with CaptureQueriesContext(connection) as queries:
            response = _task_admin().change_view(request, str(execution.pk))
            response.render()

        content = response.content.decode("utf-8")
        endpoint = reverse(
            "admin:django_ray_raytaskexecution_observability",
            args=[execution.pk],
        )
        assert response.status_code == 200
        assert 'id="django-ray-live-observability"' in content
        assert f'data-observability-url="{endpoint}"' in content
        assert 'href="/static/django_ray/admin/task_live.css"' in content
        assert 'src="/static/django_ray/admin/task_live.js"' in content
        assert 'aria-labelledby="django-ray-live-heading"' in content
        assert 'id="django-ray-live-heading"' in content
        assert content.count('role="status"') == 1
        assert 'aria-live="polite"' in content
        assert 'class="django-ray-live__grid"' in content
        assert 'class="django-ray-live__state"' in content
        assert 'data-state="RUNNING"' in content
        assert 'class="django-ray-live__workflow-links"' in content
        assert 'aria-labelledby="django-ray-workflow-links-heading"' in content
        topology_nodes_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_topology_nodes",
            args=[execution.pk],
        )
        topology_edges_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_topology_edges",
            args=[execution.pk],
        )
        node_details_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_node_details",
            args=[execution.pk],
        )
        assert f'href="{topology_nodes_url}">Topology nodes</a>' in content
        assert f'href="{topology_edges_url}">Topology edges</a>' in content
        assert f'href="{node_details_url}">Node details</a>' in content
        task_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert task_selects
        assert all("progress_data" not in query for query in task_selects)
        assert all("runtime_env_json" not in query for query in task_selects)
        assert all("workflow_progress_summary_json" not in query for query in task_selects)

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
        assert TaskAttempt.objects.get(execution=queued).state == TaskState.CANCELLED
        assert messages[-1] == (
            "Accepted cancellation for 3 task(s). "
            "Workers will attempt best-effort interruption for running work."
        )

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
        assert messages[-1] == "No selected tasks accepted cancellation."

    def test_cancel_tasks_reports_duplicate_request_as_noop(self, monkeypatch) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )

        task = RayTaskExecution.objects.create(
            task_id="admin-cancel-duplicate-001",
            callable_path="testproject.tasks.slow_task",
            state=TaskState.CANCELLING,
            args_json="[]",
            kwargs_json="{}",
        )

        admin_obj.cancel_tasks(_request(), RayTaskExecution.objects.filter(pk=task.pk))

        task.refresh_from_db()
        assert task.state == TaskState.CANCELLING
        assert messages[-1] == "No selected tasks accepted cancellation."


@pytest.mark.django_db
class TestTaskAttemptAdmin:
    def test_attempt_history_cannot_be_added_changed_or_deleted(self) -> None:
        admin_obj = _attempt_admin()
        request = _request()
        inline = TaskAttemptInline(TaskAttempt, admin.site)

        assert admin_obj.has_add_permission(request) is False
        assert admin_obj.has_change_permission(request) is False
        assert admin_obj.has_delete_permission(request) is False
        assert inline.has_add_permission(request) is False
        assert inline.has_change_permission(request) is False
        assert inline.has_delete_permission(request) is False
        assert inline.can_delete is False
        assert inline.hide_title is True
        assert inline.show_change_link is False
        assert inline.fields == (
            "attempt_detail_link",
            "state",
            "started_display",
            "finished_display",
            "error_summary",
        )
        assert inline.readonly_fields == inline.fields

    def test_attempt_inline_uses_one_compact_detail_link(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-link-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.SUCCEEDED,
        )
        inline = TaskAttemptInline(TaskAttempt, admin.site)

        rendered = str(inline.attempt_detail_link(attempt))

        assert rendered == (
            f'<a href="{reverse("admin:django_ray_taskattempt_change", args=[attempt.pk])}">#2</a>'
        )
        assert inline.attempt_detail_link.short_description == "Attempt"

    def test_diagnostics_are_redacted_bounded_and_not_raw_model_fields(self, settings) -> None:
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"password"]}
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-redaction-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="password=error-secret",
            error_traceback="safe trace " + ("x" * (ADMIN_DIAGNOSTIC_MAX_CHARS + 100)),
            result_data='{"password":"result-secret"}',
            result_reference="password=result-reference-secret",
        )
        admin_obj = _attempt_admin()

        rendered = [
            admin_obj.error_message_display(attempt),
            admin_obj.error_traceback_display(attempt),
            admin_obj.result_data_display(attempt),
            admin_obj.result_reference_display(attempt),
        ]

        assert "error-secret" not in rendered[0]
        assert "result-secret" not in rendered[2]
        assert "result-reference-secret" not in rendered[3]
        assert all(len(value) <= ADMIN_DIAGNOSTIC_MAX_CHARS for value in rendered)
        assert rendered[1].endswith("... [truncated]")
        assert "error_message" not in admin_obj.fields
        assert "error_traceback" not in admin_obj.fields
        assert "result_data" not in admin_obj.fields
        assert "result_reference" not in admin_obj.fields

    def test_empty_attempt_diagnostics_render_as_placeholders(self) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-empty-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
        )
        admin_obj = _attempt_admin()

        assert admin_obj.error_message_display(attempt) == "-"
        assert admin_obj.error_traceback_display(attempt) == "-"
        assert admin_obj.result_data_display(attempt) == "-"
        assert admin_obj.result_reference_display(attempt) == "-"

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_default_index_hides_attempt_module_but_detail_remains_linked(
        self,
        client,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-inline-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-inline-001",
            callable_path="testproject.tasks.add_numbers",
        )
        second = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
            error_message="second-attempt-marker",
        )
        first = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="first-attempt-marker",
        )
        client.force_login(user)

        index = client.get(reverse("admin:index"))
        change = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )
        first_detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[first.pk],
        )
        second_detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[second.pk],
        )
        detail = client.get(first_detail_url)
        attempt_list = client.get(reverse("admin:django_ray_taskattempt_changelist"))

        index_html = index.content.decode("utf-8")
        change_html = change.content.decode("utf-8")
        assert index.status_code == 200
        assert reverse("admin:django_ray_taskattempt_changelist") not in index_html
        assert change.status_code == 200
        assert "Attempt history" in change_html
        assert change_html.count(first_detail_url) == 1
        assert change_html.count(second_detail_url) == 1
        assert change_html.index("first-attempt-marker") < change_html.index(
            "second-attempt-marker"
        )
        assert detail.status_code == 200
        assert attempt_list.status_code == 200

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_parent_permission_does_not_expose_attempts_without_child_permission(
        self,
        client,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-permissions-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
        )
        user = get_user_model().objects.create_user(
            username="attempt-parent-viewer",
            is_staff=True,
        )
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="django_ray",
                codename="view_raytaskexecution",
            )
        )
        client.force_login(user)
        change_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[attempt.pk],
        )

        parent_only = client.get(change_url)

        assert parent_only.status_code == 200
        assert detail_url not in parent_only.content.decode("utf-8")

        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="django_ray",
                codename="view_taskattempt",
            )
        )
        with_child = client.get(change_url)

        assert with_child.status_code == 200
        assert detail_url in with_child.content.decode("utf-8")

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_empty_attempt_history_change_form_is_usable(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-empty-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-empty-history-001",
            callable_path="testproject.tasks.add_numbers",
        )
        client.force_login(user)

        response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )

        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert "Attempt history" in content
        assert "/admin/django_ray/taskattempt/" not in content
        assert 'name="attempts-0-DELETE"' not in content

    def test_inline_honors_parent_object_permission(self, monkeypatch) -> None:
        allowed = RayTaskExecution.objects.create(
            task_id="admin-attempt-parent-object-allowed",
            callable_path="testproject.tasks.add_numbers",
        )
        denied = RayTaskExecution.objects.create(
            task_id="admin-attempt-parent-object-denied",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_user(
            username="attempt-parent-object-viewer",
            is_staff=True,
        )

        def has_perm(permission: str, obj: object | None = None) -> bool:
            if permission in {
                "django_ray.view_taskattempt",
                "django_ray.change_taskattempt",
            }:
                return obj is None
            return (
                permission
                in {
                    "django_ray.view_raytaskexecution",
                    "django_ray.change_raytaskexecution",
                }
                and obj == allowed
            )

        monkeypatch.setattr(user, "has_perm", has_perm)
        request = RequestFactory().get("/admin/")
        request.user = user
        inline = TaskAttemptInline(TaskAttempt, admin.site)

        assert inline.has_view_permission(request, allowed) is True
        assert inline.has_view_permission(request, denied) is False

    def test_attempt_detail_honors_object_specific_permission(
        self,
        monkeypatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-object-permission-001",
            callable_path="testproject.tasks.add_numbers",
        )
        allowed = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
        )
        denied = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
        )
        user = get_user_model().objects.create_user(
            username="attempt-object-viewer",
            is_staff=True,
        )

        def has_perm(permission: str, obj: object | None = None) -> bool:
            return (
                permission
                in {
                    "django_ray.view_taskattempt",
                    "django_ray.change_taskattempt",
                }
                and obj == allowed
            )

        monkeypatch.setattr(user, "has_perm", has_perm)
        admin_obj = _attempt_admin()
        allowed_request = RequestFactory().get(
            reverse("admin:django_ray_taskattempt_change", args=[allowed.pk])
        )
        allowed_request.user = user
        denied_request = RequestFactory().get(
            reverse("admin:django_ray_taskattempt_change", args=[denied.pk])
        )
        denied_request.user = user

        assert admin_obj.has_view_permission(allowed_request) is False
        assert admin_obj.has_view_permission(allowed_request, allowed) is True
        assert admin_obj.has_view_permission(denied_request, denied) is False
        assert admin_obj.change_view(allowed_request, str(allowed.pk)).status_code == 200
        with pytest.raises(PermissionDenied):
            admin_obj.change_view(denied_request, str(denied.pk))

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_attempt_detail_rejects_forged_mutation(self, client) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-forged-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-forged-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="original-error",
        )
        client.force_login(user)
        detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[attempt.pk],
        )

        detail = client.get(detail_url)
        forged = client.post(
            detail_url,
            {
                "attempt_number": 99,
                "state": TaskState.SUCCEEDED,
                "error_message": "forged-error",
                "_save": "Save",
            },
        )

        attempt.refresh_from_db()
        detail_html = detail.content.decode("utf-8")
        assert detail.status_code == 200
        assert 'name="_save"' not in detail_html
        assert "deletelink" not in detail_html
        assert forged.status_code == 403
        assert attempt.attempt_number == 1
        assert attempt.state == TaskState.FAILED
        assert attempt.error_message == "original-error"

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_inline_summary_is_bounded_redacted_escaped_and_payload_free(
        self,
        settings,
        client,
    ) -> None:
        settings.DJANGO_RAY = {
            "RAY_ADDRESS": "ray://localhost:10001",
            "REDACT_PATTERNS": [r"password"],
        }
        user = get_user_model().objects.create_superuser(username="attempt-summary-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-summary-001",
            callable_path="testproject.tasks.add_numbers",
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="password=inline-secret",
            error_traceback="traceback-heavy-sentinel",
            result_data='"result-heavy-sentinel"',
            result_reference="reference-heavy-sentinel",
            workflow_progress_summary_json='"workflow-heavy-sentinel"',
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=2,
            state=TaskState.FAILED,
            error_message="<script>unsafe</script>",
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=3,
            state=TaskState.FAILED,
            error_message="a" * ADMIN_ATTEMPT_INLINE_MAX_CHARS,
        )
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=4,
            state=TaskState.FAILED,
            error_message="b" * (ADMIN_ATTEMPT_INLINE_MAX_CHARS + 1),
        )
        client.force_login(user)

        response = client.get(
            reverse(
                "admin:django_ray_raytaskexecution_change",
                args=[execution.pk],
            )
        )

        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert "inline-secret" not in content
        assert "[REDACTED]" in content
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in content
        assert ("a" * ADMIN_ATTEMPT_INLINE_MAX_CHARS) in content
        assert "Open the attempt detail to view bounded diagnostics." in content
        assert ("b" * (ADMIN_ATTEMPT_INLINE_MAX_CHARS + 1)) not in content
        assert "traceback-heavy-sentinel" not in content
        assert "result-heavy-sentinel" not in content
        assert "reference-heavy-sentinel" not in content
        assert "workflow-heavy-sentinel" not in content

    @override_settings(MIDDLEWARE=_ADMIN_MIDDLEWARE)
    def test_inline_uses_one_bounded_attempt_query_for_many_rows(
        self,
        client,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-query-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-query-001",
            callable_path="testproject.tasks.add_numbers",
        )
        for attempt_number in range(1, 13):
            TaskAttempt.objects.create(
                execution=execution,
                attempt_number=attempt_number,
                state=TaskState.FAILED,
                error_message=f"attempt-{attempt_number}",
                error_traceback="traceback-heavy-sentinel",
                result_data='"result-heavy-sentinel"',
                result_reference="reference-heavy-sentinel",
                workflow_progress_summary_json='"workflow-heavy-sentinel"',
            )
        client.force_login(user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(
                reverse(
                    "admin:django_ray_raytaskexecution_change",
                    args=[execution.pk],
                )
            )

        attempt_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and 'FROM "django_ray_taskattempt"' in query["sql"]
        ]
        assert response.status_code == 200
        assert len(attempt_selects) == 1
        assert "error_message" in attempt_selects[0]
        assert "error_traceback" not in attempt_selects[0]
        assert "result_data" not in attempt_selects[0]
        assert "result_reference" not in attempt_selects[0]
        assert "workflow_progress_summary_json" not in attempt_selects[0]

    @override_settings(
        MIDDLEWARE=_ADMIN_MIDDLEWARE,
        DJANGO_RAY={
            "RAY_ADDRESS": "ray://localhost:10001",
            "TASK_ATTEMPT_ADMIN_MODE": "standalone",
        },
    )
    def test_standalone_changelist_avoids_parent_join_and_diagnostics(
        self,
        client,
    ) -> None:
        user = get_user_model().objects.create_superuser(username="attempt-list-admin")
        execution = RayTaskExecution.objects.create(
            task_id="admin-attempt-list-001",
            callable_path="testproject.tasks.add_numbers",
        )
        attempt = TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="error-heavy-sentinel",
            error_traceback="traceback-heavy-sentinel",
            result_data='"result-heavy-sentinel"',
            result_reference="reference-heavy-sentinel",
            workflow_progress_summary_json='"workflow-heavy-sentinel"',
        )
        client.force_login(user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse("admin:django_ray_taskattempt_changelist"))

        attempt_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and 'FROM "django_ray_taskattempt"' in query["sql"]
            and "COUNT(" not in query["sql"].upper()
        ]
        assert response.status_code == 200
        assert len(attempt_selects) == 1
        query = attempt_selects[0]
        assert "JOIN" not in query.upper()
        assert "error_message" not in query
        assert "error_traceback" not in query
        assert "result_data" not in query
        assert "result_reference" not in query
        assert "workflow_progress_summary_json" not in query
        content = response.content.decode("utf-8")
        detail_url = reverse(
            "admin:django_ray_taskattempt_change",
            args=[attempt.pk],
        )
        execution_url = reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        )
        assert detail_url in content
        assert execution_url in content
        assert f'<a href="{detail_url}"><a' not in content


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
        assert admin_obj._is_heartbeat_expired(lease) is False

        lease.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=90)
        assert admin_obj.time_since_heartbeat(lease).endswith("m 30s ago")

        lease.last_heartbeat_at = None
        assert admin_obj.time_since_heartbeat(lease) == "Never"
        assert admin_obj.get_queryset(_request()).model is TaskWorkerLease

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

    def test_lease_displays_actions_and_filter_variants(self, monkeypatch) -> None:
        admin_obj = _lease_admin()
        lease = TaskWorkerLease.objects.create(
            worker_id="lease-display-worker",
            hostname="host-d",
            pid=4444,
            queue_name="default",
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=2, minutes=3),
            is_active=True,
        )
        monkeypatch.setattr(admin_obj, "_is_heartbeat_expired", lambda _: False)
        assert admin_obj.is_active_display_list(lease) is True
        assert admin_obj.time_since_heartbeat(lease).endswith("h 3m ago")

        inactive = TaskWorkerLease.objects.create(
            worker_id="lease-inactive-worker",
            hostname="host-e",
            pid=5555,
            queue_name="default",
            is_active=False,
        )
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj, "message_user", lambda request, msg: messages.append(str(msg))
        )
        admin_obj.mark_inactive(_request(), TaskWorkerLease.objects.filter(pk=inactive.pk))
        admin_obj.delete_inactive(_request(), TaskWorkerLease.objects.filter(pk=lease.pk))
        assert messages == [
            "No active leases found in selection.",
            "No inactive leases found in selection.",
        ]

        filter_obj = object.__new__(ActiveWorkerFilter)
        monkeypatch.setattr(filter_obj, "value", lambda: "inactive")
        assert list(filter_obj.lookups(_request(), admin_obj)) == [
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("all", "All"),
        ]
        assert list(filter_obj.queryset(_request(), TaskWorkerLease.objects.all())) == [inactive]
        monkeypatch.setattr(filter_obj, "value", lambda: "all")
        assert list(filter_obj.queryset(_request(), TaskWorkerLease.objects.all())) == [
            lease,
            inactive,
        ]
        monkeypatch.setattr(filter_obj, "value", lambda: None)
        assert list(filter_obj.queryset(_request(), TaskWorkerLease.objects.all())) == [lease]
        filter_obj.lookup_choices = list(filter_obj.lookups(_request(), admin_obj))

        class ChangeList:
            @staticmethod
            def get_query_string(params):
                return f"?is_active={params['is_active']}"

        choices = list(filter_obj.choices(ChangeList()))
        assert choices[0]["selected"] is True
        assert choices[0]["query_string"] == "?is_active=active"
