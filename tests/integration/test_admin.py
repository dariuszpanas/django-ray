"""Integration tests for django-ray admin actions and display helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
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
    ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES,
    ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES,
    ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES,
    ActiveWorkerFilter,
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskAttemptInline,
    TaskWorkerLeaseAdmin,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow_plans import (
    MAX_PLAN_BYTES,
    PLAN_DOMAIN_SEPARATOR,
    PLAN_SELECTION_FORMAT,
    PLAN_SELECTION_FORMAT_VERSION,
    EffectiveWorkflowPlan,
    PlanEligibility,
    PlanRejection,
    materialize_workflow_plan,
)
from django_ray.workflow_progress import MAX_PLAN_SELECTION_BYTES
from django_ray.workflow_progress_reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
    get_workflow_progress_summary,
)
from django_ray.workflow_progress_storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    prepare_workflow_progress_topology,
    stage_workflow_progress_topology,
)
from django_ray.workflow_progress_summary import serialize_workflow_progress_summary
from django_ray.workflows import map_step
from tests.workflow_progress_storage_helpers import (
    workflow_detail,
    workflow_node,
    workflow_summary,
)
from tests.workflow_progress_summary_helpers import (
    terminal_only_workflow_progress_summary,
    workflow_progress_summary,
)

_ADMIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _request() -> Any:
    return RequestFactory().post("/admin/")


def _task_admin() -> RayTaskExecutionAdmin:
    return RayTaskExecutionAdmin(RayTaskExecution, admin.site)


def _lease_admin() -> TaskWorkerLeaseAdmin:
    return TaskWorkerLeaseAdmin(TaskWorkerLease, admin.site)


def _attempt_admin() -> TaskAttemptAdmin:
    return TaskAttemptAdmin(TaskAttempt, admin.site)


def _admin_diagnostic_increment(value: int) -> int:
    return value + 1


@pytest.fixture(scope="module")
def admin_dynamic_workflow_plan() -> EffectiveWorkflowPlan:
    return materialize_workflow_plan(
        map_step(_admin_diagnostic_increment),
        invocation_args=([1, 2],),
    ).plan


def _stored_plan_fingerprint(serialized: str) -> str:
    digest = hashlib.sha256(PLAN_DOMAIN_SEPARATOR + serialized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _plan_selection_json(
    plan: EffectiveWorkflowPlan,
    *,
    reporting_policy: str = "full",
) -> str:
    selection = plan.eligibility.select(
        "dynamic_tasks",
        requested_policy="auto",
    ).as_dict()
    selection["reporting_policy"] = reporting_policy
    return json.dumps(selection)


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
            workflow_plan_json="plan" * 10_000,
            workflow_plan_selection="selection" * 10_000,
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
        assert "workflow_plan_json" not in task_selects[0]
        assert "workflow_plan_selection" not in task_selects[0]
        assert "progress_data_display" not in admin_obj.readonly_fields
        assert "workflow_plan_display" not in admin_obj.readonly_fields
        assert "workflow_plan_selection_display" not in admin_obj.readonly_fields

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

    def test_terminal_full_workflow_polling_stays_generic_but_topology_is_missing(
        self,
    ) -> None:
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

        with CaptureQueriesContext(connection) as queries:
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
        assert summary["workflow_availability"] == "NOT_REPORTED"
        assert summary["workflow_selected_strategy"] is None
        assert summary["workflow_reporting_policy"] is None
        assert all(
            "workflow_plan_selection" not in query["sql"] for query in queries.captured_queries
        )
        assert topology_response.status_code == 409
        assert topology["code"] == "MISSING"

    @pytest.mark.parametrize(
        ("reporting_policy", "task_state", "inferred_availability"),
        [
            ("disabled", TaskState.RUNNING, "DISABLED"),
            ("full", TaskState.SUCCEEDED, "MISSING"),
            ("terminal_only", TaskState.RUNNING, "NOT_REPORTED"),
            ("terminal_only", TaskState.SUCCEEDED, "MISSING"),
        ],
    )
    def test_progress_read_can_skip_selection_inference_for_routine_polling(
        self,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        reporting_policy: str,
        task_state: str,
        inferred_availability: str,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-progress-inference-{reporting_policy}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=task_state,
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(
                admin_dynamic_workflow_plan,
                reporting_policy=reporting_policy,
            ),
        )

        def authorize(candidate: RayTaskExecution) -> bool:
            return candidate.pk == execution.pk

        inferred = get_workflow_progress_summary(
            execution,
            authorize=authorize,
        )
        with CaptureQueriesContext(connection) as queries:
            polling = get_workflow_progress_summary(
                execution,
                authorize=authorize,
                infer_current_reporting_policy=False,
            )

        assert inferred["availability"] == inferred_availability
        assert polling["availability"] == "NOT_REPORTED"
        assert all(
            "workflow_plan_selection" not in query["sql"] for query in queries.captured_queries
        )

    def test_workflow_diagnostics_and_downloads_are_compact_verified_and_redacted(
        self,
        settings,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "REDACT_PATTERNS": [
                r"plan-download-secret",
                r"selection-download-secret.*",
            ],
        }
        manifest = json.loads(admin_dynamic_workflow_plan.canonical_json)
        manifest["security"]["trust_identity"] = {
            "tenant": "plan-download-secret",
        }
        serialized_plan = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        )
        selection = PlanEligibility(
            ("dynamic_tasks", "local"),
            (
                PlanRejection(
                    "compiled_graph",
                    "UNRESOLVED_CODE_IDENTITY",
                    "selection-download-secret.first",
                    "selection-download-secret first message",
                ),
                PlanRejection(
                    "compiled_graph",
                    "UNRESOLVED_CODE_IDENTITY",
                    "selection-download-secret.second",
                    "selection-download-secret second message",
                ),
                PlanRejection(
                    "static_actors",
                    "UNSUPPORTED_NODE_MODEL",
                    "selection-download-secret.third",
                    "selection-download-secret third message",
                ),
            ),
            4,
        ).select(
            "dynamic_tasks",
            requested_policy="auto",
            reporting_policy="full",
        )
        fingerprint = _stored_plan_fingerprint(serialized_plan)
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-diagnostics-valid",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_plan_fingerprint=fingerprint,
            workflow_plan_json=serialized_plan,
            workflow_plan_selection=json.dumps(selection.as_dict()),
        )
        user = get_user_model().objects.create_superuser(
            username="workflow-diagnostics-valid-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        diagnostics_response = admin_obj.workflow_diagnostics_view(
            request,
            str(execution.pk),
        )
        plan_response = admin_obj.workflow_plan_download_view(
            request,
            str(execution.pk),
        )
        selection_response = admin_obj.workflow_plan_selection_download_view(
            request,
            str(execution.pk),
        )

        payload = json.loads(diagnostics_response.content)
        assert diagnostics_response.status_code == 200
        assert payload["schema"] == "django-ray.admin-workflow-diagnostics"
        assert payload["schema_version"] == 1
        assert payload["plan"] == {
            "status": "AVAILABLE",
            "definition_name": manifest["definition"]["name"],
            "definition_revision": manifest["definition"]["revision"],
            "topology_class": "dynamic",
            "declared_node_count": len(manifest["nodes"]),
            "retry_safe": True,
            "fingerprint": fingerprint,
            "fingerprint_compact": (f"sha256:{fingerprint.removeprefix('sha256:')[:12]}"),
            "requested_policy": "auto",
            "selected_strategy": "dynamic_tasks",
            "reporting_policy": "full",
            "eligible_strategies": ["dynamic_tasks", "local"],
            "rejection_counts": {
                "UNRESOLVED_CODE_IDENTITY": 2,
                "UNSUPPORTED_NODE_MODEL": 1,
            },
            "retained_rejections": 3,
            "total_rejections": 4,
            "unretained_rejections": 1,
        }
        assert payload["progress"] == {
            "state": "REQUESTED_NOT_REPORTED",
            "message": (
                "Full workflow reporting was requested, but no bounded snapshot "
                "has been published yet."
            ),
            "availability": "NOT_REPORTED",
            "complete": False,
            "truncation_reasons": [],
            "actions": {
                "topology_nodes": False,
                "topology_edges": False,
                "node_details": False,
            },
        }
        assert len(diagnostics_response.content) <= ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES
        downloaded_plan = json.loads(plan_response.content)
        assert downloaded_plan["fingerprint"] == fingerprint
        assert downloaded_plan["manifest"]["security"]["trust_identity"] == {
            "tenant": "[REDACTED]",
        }
        downloaded_selection = json.loads(selection_response.content)
        assert downloaded_selection["fingerprint"] == fingerprint
        assert downloaded_selection["selection"]["rejections"][0]["path"] == "[REDACTED]"
        assert downloaded_selection["selection"]["rejections"][0]["message"] == "[REDACTED]"
        assert len(plan_response.content) <= ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES
        assert len(selection_response.content) <= ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES
        assert plan_response["Content-Disposition"] == 'attachment; filename="plan.json"'
        assert selection_response["Content-Disposition"] == 'attachment; filename="selection.json"'
        for response in (
            diagnostics_response,
            plan_response,
            selection_response,
        ):
            assert response.status_code == 200
            assert response["Cache-Control"] == "no-store"
            assert response["X-Content-Type-Options"] == "nosniff"
            assert response["Content-Type"].startswith("application/json")
            rendered = response.content.decode("utf-8")
            assert "plan-download-secret" not in rendered
            assert "selection-download-secret" not in rendered
        compact_rendered = diagnostics_response.content.decode("utf-8")
        assert '"path"' not in compact_rendered
        assert '"message"' in compact_rendered
        assert "first message" not in compact_rendered

    @pytest.mark.parametrize(
        "case",
        [
            "malformed_plan",
            "incomplete_pair",
            "selection_only",
            "fingerprint_mismatch",
            "unsupported_plan_format",
            "invalid_selection",
            "oversized_plan",
            "oversized_selection",
        ],
    )
    def test_workflow_diagnostics_fail_closed_for_corrupt_and_oversized_storage(
        self,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        case: str,
    ) -> None:
        marker = f"private-{case}-marker"
        serialized_plan: str | None = admin_dynamic_workflow_plan.canonical_json
        fingerprint: str | None = admin_dynamic_workflow_plan.fingerprint
        serialized_selection: str | None = _plan_selection_json(
            admin_dynamic_workflow_plan,
        )
        if case == "malformed_plan":
            serialized_plan = f'{{"private":"{marker}"'
            fingerprint = _stored_plan_fingerprint(serialized_plan)
        elif case == "incomplete_pair":
            serialized_selection = None
        elif case == "selection_only":
            serialized_plan = None
            fingerprint = None
        elif case == "fingerprint_mismatch":
            fingerprint = "sha256:" + ("0" * 64)
        elif case == "unsupported_plan_format":
            manifest = json.loads(admin_dynamic_workflow_plan.canonical_json)
            manifest["plan_format_version"] = 999
            manifest["private"] = marker
            serialized_plan = json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
            )
            fingerprint = _stored_plan_fingerprint(serialized_plan)
        elif case == "invalid_selection":
            serialized_selection = json.dumps({"private": marker})
        elif case == "oversized_plan":
            serialized_plan = marker + ("x" * MAX_PLAN_BYTES)
            fingerprint = _stored_plan_fingerprint(serialized_plan)
        elif case == "oversized_selection":
            serialized_selection = marker + ("x" * MAX_PLAN_SELECTION_BYTES)
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-diagnostics-{case}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_plan_fingerprint=fingerprint,
            workflow_plan_json=serialized_plan,
            workflow_plan_selection=serialized_selection,
        )
        user = get_user_model().objects.create_superuser(
            username=f"workflow-diagnostics-{case}-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        diagnostics_response = admin_obj.workflow_diagnostics_view(
            request,
            str(execution.pk),
        )
        download_responses = (
            admin_obj.workflow_plan_download_view(request, str(execution.pk)),
            admin_obj.workflow_plan_selection_download_view(
                request,
                str(execution.pk),
            ),
        )

        diagnostics = json.loads(diagnostics_response.content)
        assert diagnostics_response.status_code == 200
        assert diagnostics["plan"]["status"] == "CORRUPT"
        assert diagnostics["plan"]["fingerprint"] is None
        assert diagnostics["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }
        assert len(diagnostics_response.content) <= ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES
        for response in (diagnostics_response, *download_responses):
            assert response["Cache-Control"] == "no-store"
            assert response["X-Content-Type-Options"] == "nosniff"
            assert marker not in response.content.decode("utf-8")
        for response in download_responses:
            assert response.status_code == 503
            assert json.loads(response.content) == {
                "code": "CORRUPT",
                "message": "Workflow diagnostics failed validation.",
            }

    @pytest.mark.parametrize(
        "view_method",
        [
            "workflow_diagnostics_view",
            "workflow_plan_download_view",
            "workflow_plan_selection_download_view",
        ],
    )
    def test_workflow_diagnostics_views_honor_object_permissions(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        view_method: str,
    ) -> None:
        selection = _plan_selection_json(admin_dynamic_workflow_plan)
        allowed = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-object-allowed-{view_method}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=selection,
        )
        denied = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-object-denied-{view_method}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=selection,
        )
        user = get_user_model().objects.create_user(
            username=f"workflow-object-viewer-{view_method}",
            is_staff=True,
        )
        checked_objects: list[int] = []

        def has_perm(permission: str, obj: Any = None) -> bool:
            del permission
            if obj is None:
                return False
            checked_objects.append(obj.pk)
            return obj.pk == allowed.pk

        monkeypatch.setattr(user, "has_perm", has_perm)
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        view = getattr(_task_admin(), view_method)

        assert view(request, str(allowed.pk)).status_code == 200
        with CaptureQueriesContext(connection) as denied_queries:
            with pytest.raises(PermissionDenied):
                view(request, str(denied.pk))
        assert allowed.pk in checked_objects
        assert denied.pk in checked_objects
        assert all(
            "workflow_plan_json" not in query["sql"]
            and "workflow_plan_selection" not in query["sql"]
            for query in denied_queries.captured_queries
        )

    def test_bounded_workflow_plan_loader_reuses_request_scoped_queryset(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-request-scoped-loader",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        user = get_user_model().objects.create_superuser(
            username="workflow-request-scoped-loader-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()
        original_get_queryset = admin_obj.get_queryset
        calls = 0

        def request_scoped_queryset(candidate_request: Any) -> Any:
            nonlocal calls
            calls += 1
            assert candidate_request is request
            queryset = original_get_queryset(candidate_request)
            return queryset if calls == 1 else queryset.none()

        monkeypatch.setattr(admin_obj, "get_queryset", request_scoped_queryset)

        authorized = admin_obj._authorized_workflow_read_execution(
            request,
            str(execution.pk),
        )

        with pytest.raises(Http404):
            admin_obj._load_bounded_workflow_plan_fields(request, authorized)
        assert calls == 2

    def test_bounded_workflow_plan_loader_rejects_identity_change_after_authorization(
        self,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-identity-fenced-loader",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        user = get_user_model().objects.create_superuser(
            username="workflow-identity-fenced-loader-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()
        authorized = admin_obj._authorized_workflow_read_execution(
            request,
            str(execution.pk),
        )
        RayTaskExecution.objects.filter(pk=execution.pk).update(
            execution_generation=execution.execution_generation + 1,
        )

        with pytest.raises(Http404):
            admin_obj._load_bounded_workflow_plan_fields(request, authorized)

    def test_bounded_workflow_plan_loader_reauthorizes_exact_annotated_row(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-reauthorized-loader",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        user = get_user_model().objects.create_superuser(
            username="workflow-reauthorized-loader-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()
        checked_objects: list[RayTaskExecution] = []

        def has_view_permission(
            candidate_request: Any,
            obj: RayTaskExecution | None = None,
        ) -> bool:
            assert candidate_request is request
            assert obj is not None
            checked_objects.append(obj)
            return len(checked_objects) == 1

        monkeypatch.setattr(admin_obj, "has_view_permission", has_view_permission)
        authorized = admin_obj._authorized_workflow_read_execution(
            request,
            str(execution.pk),
        )

        with pytest.raises(PermissionDenied):
            admin_obj._load_bounded_workflow_plan_fields(request, authorized)
        assert [candidate.pk for candidate in checked_objects] == [
            execution.pk,
            execution.pk,
        ]
        assert hasattr(checked_objects[1], "_admin_bounded_plan")

    @pytest.mark.parametrize(
        "view_method",
        [
            "workflow_diagnostics_view",
            "workflow_plan_download_view",
            "workflow_plan_selection_download_view",
        ],
    )
    def test_workflow_diagnostics_views_are_get_only_and_return_404_for_missing_objects(
        self,
        view_method: str,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-method-{view_method}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
        )
        user = get_user_model().objects.create_superuser(
            username=f"workflow-method-admin-{view_method}",
        )
        admin_obj = _task_admin()
        view = getattr(admin_obj, view_method)
        post = RequestFactory().post("/admin/workflow/diagnostics/")
        post.user = user

        method_response = view(post, str(execution.pk))

        assert method_response.status_code == 405
        assert method_response["Allow"] == "GET"
        assert method_response["Cache-Control"] == "no-store"
        assert method_response["X-Content-Type-Options"] == "nosniff"
        missing = RequestFactory().get("/admin/workflow/diagnostics/")
        missing.user = user
        with pytest.raises(Http404):
            view(missing, "999999")

    def test_workflow_diagnostics_reports_not_recorded_without_empty_downloads(
        self,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-not-recorded",
            callable_path="testproject.tasks.add_numbers",
        )
        user = get_user_model().objects.create_superuser(
            username="workflow-not-recorded-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        diagnostics = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        downloads = (
            admin_obj.workflow_plan_download_view(request, str(execution.pk)),
            admin_obj.workflow_plan_selection_download_view(
                request,
                str(execution.pk),
            ),
        )

        diagnostics_payload = json.loads(diagnostics.content)
        assert diagnostics_payload["plan"]["status"] == "NOT_RECORDED"
        assert diagnostics_payload["progress"]["state"] == "NOT_REPORTED"
        assert diagnostics_payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }
        for response in downloads:
            assert response.status_code == 404
            assert json.loads(response.content) == {
                "code": "NOT_RECORDED",
                "message": "Workflow diagnostics were not recorded.",
            }
            assert response["Cache-Control"] == "no-store"
            assert response["X-Content-Type-Options"] == "nosniff"

    def test_workflow_diagnostics_distinguishes_every_progress_presentation_state(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        user = get_user_model().objects.create_superuser(
            username="workflow-progress-presentation-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def useful_preflight(
            _request: Any,
            _execution: RayTaskExecution,
            *,
            collection: str,
            attempt_number: int | None,
        ) -> dict[str, Any]:
            assert attempt_number is None
            assert collection in {
                "topology_nodes",
                "topology_edges",
                "node_details",
            }
            return {
                "availability": "AVAILABLE",
                "returned_count": 1,
                "items": [{"collection": collection}],
            }

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            useful_preflight,
        )
        cases = [
            (
                "requested-active",
                TaskState.RUNNING,
                "full",
                "REQUESTED_NOT_REPORTED",
                None,
            ),
            (
                "requested-terminal",
                TaskState.SUCCEEDED,
                "full",
                "REQUESTED_MISSING",
                None,
            ),
            (
                "terminal-only-pending",
                TaskState.RUNNING,
                "terminal_only",
                "TERMINAL_ONLY_PENDING",
                None,
            ),
            (
                "terminal-only-missing",
                TaskState.SUCCEEDED,
                "terminal_only",
                "TERMINAL_ONLY_MISSING",
                None,
            ),
            ("legacy", TaskState.RUNNING, "full", "LEGACY_ONLY", "legacy"),
            ("disabled", TaskState.RUNNING, "disabled", "DISABLED", "disabled"),
            (
                "omitted",
                TaskState.RUNNING,
                "sampled",
                "OMITTED_BY_POLICY",
                "omitted",
            ),
            ("available", TaskState.RUNNING, "full", "AVAILABLE", "available"),
            ("truncated", TaskState.RUNNING, "full", "TRUNCATED", "truncated"),
            ("expired", TaskState.SUCCEEDED, "full", "EXPIRED", "expired"),
            ("missing", TaskState.RUNNING, "full", "MISSING", "missing"),
            ("corrupt", TaskState.RUNNING, "full", "CORRUPT", "corrupt"),
        ]

        observed: dict[str, dict[str, Any]] = {}
        for index, (
            case_id,
            task_state,
            reporting_policy,
            expected_state,
            summary_case,
        ) in enumerate(cases, start=1):
            execution = RayTaskExecution.objects.create(
                task_id=f"admin-workflow-progress-{case_id}",
                callable_path="tests.integration.test_admin._admin_diagnostic_increment",
                state=task_state,
                workflow_run_id=f"00000000-0000-0000-0000-{950_000 + index:012d}",
                workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
                workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
                workflow_plan_selection=_plan_selection_json(
                    admin_dynamic_workflow_plan,
                    reporting_policy=reporting_policy,
                ),
            )
            if summary_case == "legacy":
                execution.progress_data = json.dumps(
                    {
                        "schema_version": 1,
                        "revision": 4,
                        "state": "RUNNING",
                        "graph": {
                            "nodes": [{"node_id": "0.0"}],
                            "edges": [],
                        },
                    }
                )
                execution.save(update_fields=["progress_data"])
            elif summary_case == "corrupt":
                execution.workflow_progress_summary_json = '{"private":"corrupt-progress-secret"'
                execution.save(update_fields=["workflow_progress_summary_json"])
            elif summary_case is not None:
                terminal = summary_case == "expired"
                summary = workflow_progress_summary(
                    execution,
                    published_detail=(
                        summary_case in {"available", "truncated", "expired", "missing"}
                    ),
                    state="SUCCEEDED" if terminal else "RUNNING",
                )
                summary["selected_strategy"] = "dynamic_tasks"
                summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
                if summary_case == "disabled":
                    summary["reporting_policy"] = "disabled"
                    summary["detail"]["availability"] = "DISABLED"
                elif summary_case == "omitted":
                    summary["reporting_policy"] = "sampled"
                    summary["detail"]["availability"] = "OMITTED_BY_POLICY"
                elif summary_case == "truncated":
                    summary["detail"] = {
                        "availability": "TRUNCATED",
                        "complete": False,
                        "truncation_reasons": ["detail_count_limit"],
                    }
                    summary["edge_counts"] = {
                        "declared": 1,
                        "discovered": 1,
                        "retained_topology": 1,
                    }
                elif summary_case in {"missing", "expired"}:
                    summary["detail"] = {
                        "availability": summary_case.upper(),
                        "complete": False,
                        "truncation_reasons": [],
                    }
                execution.workflow_progress_summary_json = serialize_workflow_progress_summary(
                    summary
                )
                execution.save(update_fields=["workflow_progress_summary_json"])

            response = admin_obj.workflow_diagnostics_view(
                request,
                str(execution.pk),
            )
            assert response.status_code == 200
            payload = json.loads(response.content)
            assert payload["plan"]["status"] == "AVAILABLE"
            assert payload["progress"]["state"] == expected_state
            assert payload["progress"]["message"]
            assert "corrupt-progress-secret" not in response.content.decode("utf-8")
            observed[expected_state] = payload["progress"]

        no_actions = {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }
        for state in (
            "REQUESTED_NOT_REPORTED",
            "REQUESTED_MISSING",
            "TERMINAL_ONLY_PENDING",
            "TERMINAL_ONLY_MISSING",
            "LEGACY_ONLY",
            "DISABLED",
            "OMITTED_BY_POLICY",
            "EXPIRED",
            "MISSING",
            "CORRUPT",
        ):
            assert observed[state]["actions"] == no_actions
            assert observed[state]["complete"] is False
        assert observed["AVAILABLE"]["actions"] == {
            "topology_nodes": True,
            "topology_edges": False,
            "node_details": True,
        }
        assert observed["AVAILABLE"]["complete"] is True
        assert observed["TRUNCATED"]["actions"] == {
            "topology_nodes": True,
            "topology_edges": True,
            "node_details": True,
        }
        assert observed["TRUNCATED"]["complete"] is False
        assert observed["TRUNCATED"]["truncation_reasons"] == ["detail_count_limit"]

    @pytest.mark.parametrize(
        ("workflow_state", "task_state"),
        [
            ("SUCCEEDED", TaskState.SUCCEEDED),
            ("FAILED", TaskState.FAILED),
        ],
    )
    def test_terminal_only_summary_is_explicit_and_never_advertises_detail(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        workflow_state: str,
        task_state: str,
    ) -> None:
        manifest = json.loads(admin_dynamic_workflow_plan.canonical_json)
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-terminal-only-{workflow_state.lower()}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=task_state,
            workflow_run_id=(
                "00000000-0000-0000-0000-000000950011"
                if workflow_state == "SUCCEEDED"
                else "00000000-0000-0000-0000-000000950012"
            ),
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(
                admin_dynamic_workflow_plan,
                reporting_policy="terminal_only",
            ),
        )
        summary = terminal_only_workflow_progress_summary(
            execution,
            state=workflow_state,
            declared_node_count=len(manifest["nodes"]),
            declared_edge_count=len(manifest["edges"]),
        )
        summary["selected_strategy"] = "dynamic_tasks"
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username=f"terminal-only-{workflow_state.lower()}-admin",
        )
        request = RequestFactory().get(
            "/admin/workflow/diagnostics/",
            {"attempt_number": execution.attempt_number},
        )
        request.user = user
        admin_obj = _task_admin()

        def unexpected_detail_read(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("terminal-only presentation must not read workflow detail")

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            unexpected_detail_read,
        )
        for read_name in (
            "list_workflow_topology_nodes",
            "list_workflow_topology_edges",
            "list_workflow_node_details",
        ):
            monkeypatch.setattr(f"django_ray.admin.{read_name}", unexpected_detail_read)

        diagnostics_response = admin_obj.workflow_diagnostics_view(
            request,
            str(execution.pk),
        )
        graph_response = admin_obj.workflow_graph_view(request, str(execution.pk))
        observability_response = admin_obj.observability_view(request, str(execution.pk))
        diagnostics = json.loads(diagnostics_response.content)
        graph = json.loads(graph_response.content)
        observability = json.loads(observability_response.content)
        public_identity = {
            key: value
            for key, value in summary["run_identity"].items()
            if key != "task_execution_pk"
        }

        assert diagnostics_response.status_code == 200
        assert diagnostics["progress"] == {
            "state": "TERMINAL_ONLY",
            "message": (
                "A terminal workflow summary is available; topology and node detail "
                "were omitted by the terminal-only reporting policy."
            ),
            "availability": "OMITTED_BY_POLICY",
            "complete": False,
            "workflow_state": workflow_state,
            "reporting_policy": "terminal_only",
            "truncation_reasons": [],
            "actions": {
                "topology_nodes": False,
                "topology_edges": False,
                "node_details": False,
            },
        }
        assert graph_response.status_code == 200
        assert graph["status"] == "UNAVAILABLE"
        assert graph["complete"] is False
        assert graph["nodes"] == []
        assert graph["edges"] == []
        assert observability_response.status_code == 200
        assert observability["workflow"] == {
            "revision": 1,
            "run_identity": public_identity,
            "state": workflow_state,
            "total_nodes": 0,
            "completed_nodes": 0,
            "failed_nodes": 0,
            "running_nodes": 0,
            "pending_nodes": 0,
            "progress_percent": 100.0 if workflow_state == "SUCCEEDED" else 0.0,
            "reporting_policy": "terminal_only",
            "selected_strategy": "dynamic_tasks",
            "declared_nodes": len(manifest["nodes"]),
            "declared_edges": len(manifest["edges"]),
            "timestamps": summary["timestamps"],
            "terminal": summary["terminal"],
            "detail": {
                "availability": "OMITTED_BY_POLICY",
                "complete": False,
                "truncation_reasons": [],
            },
        }
        assert "task_execution_pk" not in observability["workflow"]["run_identity"]

    @pytest.mark.parametrize(
        ("mismatched_field", "mismatched_value"),
        [
            ("plan_fingerprint", "sha256:" + ("0" * 64)),
            ("selected_strategy", "local"),
            ("reporting_policy", "sampled"),
        ],
    )
    def test_workflow_diagnostics_rejects_plan_progress_binding_mismatch(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        mismatched_field: str,
        mismatched_value: str,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-progress-mismatch-{mismatched_field}",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960001",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        summary[mismatched_field] = mismatched_value
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username=f"workflow-progress-mismatch-{mismatched_field}-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def unexpected_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("mismatched progress must not preflight topology")

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            unexpected_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "AVAILABLE"
        assert payload["progress"] == {
            "state": "CORRUPT",
            "message": "Workflow progress failed validation.",
            "availability": "CORRUPT",
            "complete": False,
            "truncation_reasons": [],
            "actions": {
                "topology_nodes": False,
                "topology_edges": False,
                "node_details": False,
            },
        }

    def test_workflow_progress_binding_uses_validated_values_before_redaction(
        self,
        monkeypatch,
        settings,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        settings.DJANGO_RAY = {
            **settings.DJANGO_RAY,
            "REDACT_PATTERNS": [r"dynamic_tasks", r"full", r"sha256"],
        }
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-progress-redacted-binding",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960006",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-progress-redacted-binding-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def useful_preflight(
            _request: Any,
            _execution: RayTaskExecution,
            *,
            collection: str,
            attempt_number: int | None,
        ) -> dict[str, Any]:
            assert attempt_number is None
            assert collection in {"topology_nodes", "node_details"}
            return {
                "availability": "AVAILABLE",
                "returned_count": 1,
                "items": [{"collection": collection}],
            }

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            useful_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "AVAILABLE"
        assert payload["plan"]["fingerprint"] == "[REDACTED]"
        assert payload["plan"]["selected_strategy"] == "[REDACTED]"
        assert payload["plan"]["reporting_policy"] == "[REDACTED]"
        assert payload["progress"]["state"] == "AVAILABLE"
        assert payload["progress"]["availability"] == "AVAILABLE"
        assert payload["progress"]["actions"] == {
            "topology_nodes": True,
            "topology_edges": False,
            "node_details": True,
        }

    @pytest.mark.parametrize(
        ("plan_case", "expected_plan_status"),
        [
            ("not_recorded", "NOT_RECORDED"),
            ("corrupt", "CORRUPT"),
        ],
    )
    def test_schema_v3_progress_requires_an_available_verified_plan(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        plan_case: str,
        expected_plan_status: str,
    ) -> None:
        plan_fields: dict[str, str | None] = {
            "workflow_plan_fingerprint": None,
            "workflow_plan_json": None,
            "workflow_plan_selection": None,
        }
        if plan_case == "corrupt":
            plan_fields = {
                "workflow_plan_fingerprint": "sha256:" + ("0" * 64),
                "workflow_plan_json": admin_dynamic_workflow_plan.canonical_json,
                "workflow_plan_selection": _plan_selection_json(
                    admin_dynamic_workflow_plan,
                ),
            }
        execution = RayTaskExecution.objects.create(
            task_id=f"admin-workflow-v3-{plan_case}-plan",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id=(
                "00000000-0000-0000-0000-000000960002"
                if plan_case == "not_recorded"
                else "00000000-0000-0000-0000-000000960003"
            ),
            **plan_fields,
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username=f"workflow-v3-{plan_case}-plan-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def unexpected_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("unverified plans must not preflight topology")

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            unexpected_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == expected_plan_status
        assert payload["progress"]["state"] == "CORRUPT"
        assert payload["progress"]["availability"] == "CORRUPT"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_corrupt_plan_preserves_terminal_only_graph_suppression_signal(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-terminal-only-corrupt-plan",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.SUCCEEDED,
            workflow_run_id="00000000-0000-0000-0000-000000960004",
            workflow_plan_fingerprint="sha256:" + ("0" * 64),
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(
                admin_dynamic_workflow_plan,
                reporting_policy="terminal_only",
            ),
        )
        manifest = json.loads(admin_dynamic_workflow_plan.canonical_json)
        summary = terminal_only_workflow_progress_summary(
            execution,
            declared_node_count=len(manifest["nodes"]),
            declared_edge_count=len(manifest["edges"]),
        )
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="terminal-only-corrupt-plan-admin",
        )
        request = RequestFactory().get(
            "/admin/workflow/diagnostics/",
            {"attempt_number": execution.attempt_number},
        )
        request.user = user
        admin_obj = _task_admin()

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            lambda *_args, **_kwargs: pytest.fail(
                "corrupt plans must not preflight terminal-only detail"
            ),
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "CORRUPT"
        assert payload["progress"]["state"] == "CORRUPT"
        assert payload["progress"]["availability"] == "CORRUPT"
        assert payload["progress"]["reporting_policy"] == "terminal_only"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_corrupt_plan_preserves_terminal_only_policy_without_a_summary(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-terminal-only-corrupt-plan-missing-summary",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.SUCCEEDED,
            workflow_run_id="00000000-0000-0000-0000-000000960005",
            workflow_plan_fingerprint="sha256:" + ("0" * 64),
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(
                admin_dynamic_workflow_plan,
                reporting_policy="terminal_only",
            ),
        )
        user = get_user_model().objects.create_superuser(
            username="terminal-only-corrupt-plan-missing-summary-admin",
        )
        request = RequestFactory().get(
            "/admin/workflow/diagnostics/",
            {"attempt_number": execution.attempt_number},
        )
        request.user = user
        admin_obj = _task_admin()
        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            lambda *_args, **_kwargs: pytest.fail(
                "a terminal-only run without a summary must not preflight detail"
            ),
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["plan"]["status"] == "CORRUPT"
        assert payload["plan"]["reporting_policy"] == "terminal_only"
        assert payload["progress"]["state"] == "TERMINAL_ONLY_MISSING"
        assert payload["progress"]["availability"] == "MISSING"
        assert payload["progress"]["reporting_policy"] == "terminal_only"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_workflow_diagnostics_read_is_pinned_to_the_requested_attempt(
        self,
        monkeypatch,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-pinned-diagnostics",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            attempt_number=2,
        )
        user = get_user_model().objects.create_superuser(
            username="workflow-pinned-diagnostics-admin",
        )
        request = RequestFactory().get(
            "/admin/workflow/diagnostics/",
            {"attempt_number": 1},
        )
        request.user = user
        observed: list[int | None] = []

        def summary_read(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            observed.append(kwargs.get("attempt_number"))
            return {
                "source_schema_version": None,
                "summary": None,
                "availability": "NOT_REPORTED",
                "complete": False,
            }

        monkeypatch.setattr(
            "django_ray.admin.get_workflow_progress_summary",
            summary_read,
        )

        response = _task_admin().workflow_diagnostics_view(
            request,
            str(execution.pk),
        )

        assert response.status_code == 200
        assert observed == [1]

    def test_workflow_diagnostics_does_not_advertise_summary_only_topology(
        self,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-summary-only-topology",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960004",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-summary-only-topology-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        diagnostics_response = admin_obj.workflow_diagnostics_view(
            request,
            str(execution.pk),
        )
        topology_response = admin_obj.workflow_topology_nodes_view(
            request,
            str(execution.pk),
        )
        diagnostics = json.loads(diagnostics_response.content)
        topology = json.loads(topology_response.content)

        assert diagnostics_response.status_code == 200
        assert diagnostics["progress"]["state"] == "MISSING"
        assert diagnostics["progress"]["availability"] == "MISSING"
        assert diagnostics["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }
        assert topology_response.status_code == 409
        assert topology["code"] == "MISSING"

    def test_workflow_diagnostics_hides_actions_when_preflight_is_corrupt(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-corrupt-topology-preflight",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960005",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-corrupt-topology-preflight-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def corrupt_preflight(
            _request: Any,
            _execution: RayTaskExecution,
            *,
            collection: str,
            attempt_number: int | None,
        ) -> dict[str, Any]:
            assert attempt_number is None
            assert collection == "topology_nodes"
            raise WorkflowProgressReadError(WorkflowProgressReadErrorCode.CORRUPT)

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            corrupt_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["progress"]["state"] == "CORRUPT"
        assert payload["progress"]["availability"] == "CORRUPT"
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    @pytest.mark.parametrize(
        ("preflight_page", "expected_state"),
        [
            ({"availability": "AVAILABLE", "returned_count": 1}, "CORRUPT"),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 1,
                    "items": None,
                },
                "CORRUPT",
            ),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 2,
                    "items": [{}],
                },
                "CORRUPT",
            ),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 1,
                    "items": [{}, {}],
                },
                "CORRUPT",
            ),
            (
                {
                    "availability": "AVAILABLE",
                    "returned_count": 0,
                    "items": [],
                },
                "MISSING",
            ),
        ],
        ids=[
            "missing-items",
            "items-not-list",
            "items-shorter-than-count",
            "items-longer-than-count",
            "valid-empty-page",
        ],
    )
    def test_workflow_diagnostics_fails_closed_for_unusable_preflight_pages(
        self,
        monkeypatch,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
        preflight_page: dict[str, Any],
        expected_state: str,
    ) -> None:
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-inconsistent-topology-preflight",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            workflow_run_id="00000000-0000-0000-0000-000000960007",
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        summary = workflow_progress_summary(execution, published_detail=True)
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        execution.workflow_progress_summary_json = serialize_workflow_progress_summary(summary)
        execution.save(update_fields=["workflow_progress_summary_json"])
        user = get_user_model().objects.create_superuser(
            username="workflow-inconsistent-topology-preflight-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        def inconsistent_preflight(
            _request: Any,
            _execution: RayTaskExecution,
            *,
            collection: str,
            attempt_number: int | None,
        ) -> dict[str, Any]:
            assert attempt_number is None
            assert collection == "topology_nodes"
            return preflight_page

        monkeypatch.setattr(
            admin_obj,
            "_preflight_workflow_collection",
            inconsistent_preflight,
        )

        response = admin_obj.workflow_diagnostics_view(request, str(execution.pk))
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["progress"]["state"] == expected_state
        assert payload["progress"]["availability"] == expected_state
        assert payload["progress"]["actions"] == {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }

    def test_workflow_diagnostics_advertises_only_persisted_nonempty_collections(
        self,
        admin_dynamic_workflow_plan: EffectiveWorkflowPlan,
    ) -> None:
        run_id = "00000000-0000-0000-0000-000000960007"
        execution = RayTaskExecution.objects.create(
            task_id="admin-workflow-persisted-diagnostics",
            callable_path="tests.integration.test_admin._admin_diagnostic_increment",
            state=TaskState.RUNNING,
            attempt_number=1,
            execution_generation=1,
            workflow_run_id=run_id,
            workflow_plan_fingerprint=admin_dynamic_workflow_plan.fingerprint,
            workflow_plan_json=admin_dynamic_workflow_plan.canonical_json,
            workflow_plan_selection=_plan_selection_json(admin_dynamic_workflow_plan),
        )
        identity = WorkflowRunIdentity(
            task_execution_pk=execution.pk,
            attempt_number=execution.attempt_number,
            execution_generation=execution.execution_generation,
            run_id=run_id,
        )
        node_ids = ("admin-node-00001", "admin-node-00002")
        topology = prepare_workflow_progress_topology(
            identity,
            1,
            tuple(workflow_node(node_id) for node_id in node_ids),
            ({"source": node_ids[0], "target": node_ids[1]},),
        )
        prepared_detail = prepare_workflow_progress_detail(
            tuple(workflow_detail(node_id) for node_id in node_ids),
            topology=topology,
        )
        manifest_id = stage_workflow_progress_topology(topology)
        assert manifest_id is not None
        summary = workflow_summary(
            identity,
            summary_revision=1,
            node_count=len(node_ids),
            running_count=0,
        )
        summary["plan_fingerprint"] = admin_dynamic_workflow_plan.fingerprint
        summary["selected_strategy"] = "dynamic_tasks"
        summary["reporting_policy"] = "full"
        edge_counts = summary["edge_counts"]
        assert isinstance(edge_counts, dict)
        edge_counts.update(
            declared=1,
            discovered=1,
        )
        publication = persist_workflow_progress_publication(
            identity,
            summary,
            manifest_id=manifest_id,
            prepared_topology=topology,
            prepared_detail=prepared_detail,
        )
        assert publication.accepted is True

        user = get_user_model().objects.create_superuser(
            username="workflow-persisted-diagnostics-admin",
        )
        request = RequestFactory().get("/admin/workflow/diagnostics/")
        request.user = user
        admin_obj = _task_admin()

        diagnostics_response = admin_obj.workflow_diagnostics_view(
            request,
            str(execution.pk),
        )
        diagnostics = json.loads(diagnostics_response.content)

        assert diagnostics_response.status_code == 200
        assert diagnostics["plan"]["status"] == "AVAILABLE"
        assert diagnostics["progress"]["state"] == "AVAILABLE"
        assert diagnostics["progress"]["actions"] == {
            "topology_nodes": True,
            "topology_edges": True,
            "node_details": True,
        }

        advertised_views = {
            "topology_nodes": admin_obj.workflow_topology_nodes_view,
            "topology_edges": admin_obj.workflow_topology_edges_view,
            "node_details": admin_obj.workflow_node_details_view,
        }
        for action, view in advertised_views.items():
            assert diagnostics["progress"]["actions"][action] is True
            response = view(request, str(execution.pk))
            payload = json.loads(response.content)
            assert response.status_code == 200
            assert payload["availability"] == "AVAILABLE"
            assert payload["returned_count"] > 0
            assert len(payload["items"]) == payload["returned_count"]

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
        assert all("workflow_plan_json" not in query for query in task_selects)
        assert all("workflow_plan_selection" not in query for query in task_selects)
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
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
            called.append("topology_nodes")
            return page_payload("topology_nodes")

        def fake_edges(candidate, *, authorize, **kwargs):
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
            called.append("topology_edges")
            return page_payload("topology_edges")

        def fake_details(candidate, *, authorize, **kwargs):
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
            called.append("node_details")
            return page_payload("node_details")

        def fake_node(candidate, node_id, *, authorize, **kwargs):
            assert node_id == "node/one"
            assert authorize(candidate) is True
            assert kwargs["attempt_number"] == 2
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
                request = RequestFactory().get("/admin/workflow/?limit=10&attempt_number=2")
                request.user = user
                response = view(request, str(execution.pk))
                assert response.status_code == 200
                assert response["Cache-Control"] == "no-store"
            request = RequestFactory().get(
                "/admin/workflow/node/?attempt_number=2&node_id=node%2Fone"
            )
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
        assert "unfold" not in django_settings.INSTALLED_APPS
        execution = RayTaskExecution.objects.create(
            task_id="admin-live-form-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.RUNNING,
            progress_data="legacy-graph" * 10_000,
            workflow_progress_summary_json="summary" * 10_000,
            workflow_plan_json="raw-workflow-plan-marker" * 1_000,
            workflow_plan_selection="raw-workflow-selection-marker" * 1_000,
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
        diagnostics_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_diagnostics",
            args=[execution.pk],
        )
        plan_download_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_plan_download",
            args=[execution.pk],
        )
        selection_download_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_plan_selection_download",
            args=[execution.pk],
        )
        graph_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_graph",
            args=[execution.pk],
        )
        attempt_query = f"?attempt_number={execution.attempt_number}"
        assert response.status_code == 200
        assert 'class="module aligned django-ray-live"' in content
        assert 'id="django-ray-live-observability"' in content
        assert f'data-observability-url="{endpoint}"' in content
        assert 'href="/static/django_ray/admin/task_live.css"' in content
        assert 'src="/static/django_ray/admin/task_live.js"' in content
        assert 'src="/static/django_ray/admin/workflow_diagnostics.js"' in content
        assert 'aria-labelledby="django-ray-live-heading"' in content
        assert 'id="django-ray-live-heading"' in content
        assert content.count('role="status"') == 2
        assert 'aria-live="polite"' in content
        assert 'class="django-ray-live__grid"' in content
        assert 'class="django-ray-live__state"' in content
        assert 'data-state="RUNNING"' in content
        assert 'id="django-ray-workflow-diagnostics"' in content
        assert 'class="django-ray-workflow"' in content
        assert "data-workflow-diagnostics-status" in content
        assert "data-workflow-diagnostics-content" in content
        assert 'aria-atomic="true"' in content
        assert f'data-diagnostics-url="{diagnostics_url}{attempt_query}"' in content
        assert f'data-pinned-attempt-number="{execution.attempt_number}"' in content
        assert f'data-graph-url="{graph_url}{attempt_query}"' in content
        assert f'data-plan-download-url="{plan_download_url}"' in content
        assert f'data-selection-download-url="{selection_download_url}"' in content
        assert "Workflow execution" in content
        details_start = content.index("<details")
        details_opening_tag = content[details_start : content.index(">", details_start)]
        assert " open" not in details_opening_tag
        assert "raw-workflow-plan-marker" not in content
        assert "raw-workflow-selection-marker" not in content
        assert "Effective workflow plan" not in content
        assert "Workflow strategy selection" not in content
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
        node_detail_url = reverse(
            "admin:django_ray_raytaskexecution_workflow_node_detail",
            args=[execution.pk],
        )
        assert f'data-topology-nodes-url="{topology_nodes_url}{attempt_query}"' in content
        assert f'data-topology-edges-url="{topology_edges_url}{attempt_query}"' in content
        assert f'data-node-details-url="{node_details_url}{attempt_query}"' in content
        assert f'data-node-detail-url="{node_detail_url}{attempt_query}"' in content
        assert f'href="{graph_url}"' not in content
        assert f'href="{topology_nodes_url}"' not in content
        assert f'href="{topology_edges_url}"' not in content
        assert f'href="{node_details_url}"' not in content
        assert f'href="{node_detail_url}"' not in content
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
        assert all("workflow_plan_json" not in query for query in task_selects)
        assert all("workflow_plan_selection" not in query for query in task_selects)

    def test_workflow_diagnostics_javascript_contract(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI"):
                pytest.fail("Node.js is required for the workflow diagnostics contract in CI")
            pytest.skip("Node.js is unavailable for the workflow diagnostics contract")

        result = subprocess.run(
            [node, "--test", "tests/javascript/workflow_diagnostics.test.mjs"],
            cwd=_REPOSITORY_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stdout + result.stderr

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

    def test_retry_tasks_skips_corrupt_runtime_env_without_aborting_selection(
        self,
        monkeypatch,
    ) -> None:
        admin_obj = _task_admin()
        messages: list[str] = []
        monkeypatch.setattr(
            admin_obj,
            "message_user",
            lambda request, msg: messages.append(str(msg)),
        )
        corrupt = RayTaskExecution.objects.create(
            task_id="admin-retry-runtime-env-corrupt-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.FAILED,
            attempt_number=2,
            execution_generation=4,
            error_message="original failure",
            runtime_env_json=('{"env_vars":{"VALUE":"arbitrary-customer-marker-7cf3"}}'),
            runtime_env_hash="0" * 64,
        )
        valid = RayTaskExecution.objects.create(
            task_id="admin-retry-runtime-env-valid-001",
            callable_path="testproject.tasks.failing_task",
            state=TaskState.LOST,
            attempt_number=2,
            execution_generation=4,
            error_message="worker lost",
        )

        admin_obj.retry_tasks(
            _request(),
            RayTaskExecution.objects.filter(pk__in=[corrupt.pk, valid.pk]),
        )

        corrupt.refresh_from_db()
        valid.refresh_from_db()
        assert corrupt.state == TaskState.FAILED
        assert corrupt.attempt_number == 2
        assert corrupt.execution_generation == 4
        assert corrupt.error_message == "original failure"
        assert not TaskAttempt.objects.filter(execution=corrupt).exists()
        assert valid.state == TaskState.QUEUED
        assert valid.attempt_number == 3
        assert messages == [
            "Queued 1 task(s) for retry. Skipped 1 task(s) because their persisted "
            "RuntimeEnv snapshots failed validation."
        ]
        assert "arbitrary-customer-marker-7cf3" not in messages[0]

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
