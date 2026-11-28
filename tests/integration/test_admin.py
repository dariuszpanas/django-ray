"""Integration tests for django-ray admin display helpers and workflow diagnostics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import Http404
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django_ray.admin import (
    ADMIN_DIAGNOSTIC_MAX_CHARS,
    ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES,
    ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES,
    ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES,
    ExecutionProtocolFilter,
    TaskAttemptInline,
)
from django_ray.models import (
    RayTaskExecution,
    TaskState,
    TaskWorkerLease,
)
from django_ray.workflow.plans import (
    MAX_PLAN_BYTES,
    PLAN_SELECTION_FORMAT,
    PLAN_SELECTION_FORMAT_VERSION,
    EffectiveWorkflowPlan,
    PlanEligibility,
    PlanRejection,
    materialize_workflow_plan,
)
from django_ray.workflow.progress.reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
    get_workflow_progress_summary,
)
from django_ray.workflow.progress.runs import MAX_PLAN_SELECTION_BYTES
from django_ray.workflow.progress.summary import serialize_workflow_progress_summary
from django_ray.workflows import map_step
from tests.integration._admin_helpers import (
    _ADMIN_MIDDLEWARE,
    _attempt_admin,
    _plan_selection_json,
    _request,
    _stored_plan_fingerprint,
    _task_admin,
)
from tests.workflow_progress_summary_helpers import (
    terminal_only_workflow_progress_summary,
    workflow_progress_summary,
)


def _admin_diagnostic_increment(value: int) -> int:
    return value + 1


@pytest.fixture(scope="module")
def admin_dynamic_workflow_plan() -> EffectiveWorkflowPlan:
    return materialize_workflow_plan(
        map_step(_admin_diagnostic_increment),
        invocation_args=([1, 2],),
    ).plan


@pytest.mark.django_db
class TestRayTaskExecutionAdmin:
    """Tests for task admin formatting and workflow diagnostics."""

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

    def test_execution_protocol_metadata_is_read_only_and_filterable(self) -> None:
        admin_obj = _task_admin()
        fieldset_fields = {
            field for _, options in admin_obj.fieldsets for field in options.get("fields", ())
        }
        protocol_fields = {
            "metadata_schema_version",
            "execution_protocol_version",
            "created_with_django_ray_version_display",
            "managed_with_django_ray_version_display",
            "executor_django_ray_version_display",
            "protocol_compatible_worker_available_display",
        }

        assert protocol_fields <= fieldset_fields
        assert protocol_fields <= set(admin_obj.readonly_fields)
        assert {
            "created_with_django_ray_version",
            "managed_with_django_ray_version",
            "executor_django_ray_version",
        }.isdisjoint(fieldset_fields)
        assert {"metadata_schema_version", "execution_protocol_version"} <= set(
            admin_obj.observability_fields
        )
        assert ExecutionProtocolFilter in admin_obj.list_filter

    def test_protocol_availability_is_annotated_once_for_admin_reads(self) -> None:
        TaskWorkerLease.objects.create(
            worker_id="protocol-admin-reader",
            hostname="protocol-admin-host",
            pid=4102,
            queue_name="informational-only",
            capability_schema_version=1,
            django_ray_version="0.5.0-reader",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
        )
        execution = RayTaskExecution.objects.create(
            task_id="protocol-admin-visible",
            callable_path="test.task",
            queue_name="different-queue",
        )
        admin_obj = _task_admin()
        request = _request()
        request._django_ray_bounded_execution_detail = True

        with CaptureQueriesContext(connection) as queries:
            annotated = admin_obj.get_queryset(request).get(pk=execution.pk)

        assert admin_obj.protocol_compatible_worker_available_display(annotated) is True
        execution_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(execution_selects) == 1
        assert "django_ray_taskworkerlease" in execution_selects[0]

    def test_admin_protocol_provenance_is_sql_bounded_on_sqlite(self) -> None:
        if connection.vendor != "sqlite":
            pytest.skip("SQLite corruption guard is specific to unenforced VARCHAR lengths")
        exact = "m" * 128
        execution = RayTaskExecution.objects.create(
            task_id="protocol-admin-oversized",
            callable_path="test.task",
            created_with_django_ray_version="CANARY_ADMIN_DETAIL_" + "x" * 256,
            managed_with_django_ray_version=exact,
            executor_django_ray_version="CANARY_ADMIN_DETAIL_NUL\x00" + "y" * 256,
        )
        request = _request()
        request._django_ray_bounded_execution_detail = True
        admin_obj = _task_admin()

        with CaptureQueriesContext(connection) as queries:
            guarded = admin_obj.get_queryset(request).get(pk=execution.pk)

        assert "created_with_django_ray_version" not in guarded.__dict__
        assert "managed_with_django_ray_version" not in guarded.__dict__
        assert "executor_django_ray_version" not in guarded.__dict__
        assert (
            admin_obj.created_with_django_ray_version_display(guarded)
            == "Stored diagnostic omitted because it exceeds the ordinary Admin read limit."
        )
        assert admin_obj.managed_with_django_ray_version_display(guarded) == exact
        assert (
            admin_obj.executor_django_ray_version_display(guarded)
            == "Stored diagnostic omitted because it exceeds the ordinary Admin read limit."
        )
        execution_selects = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "django_ray_raytaskexecution" in query["sql"]
        ]
        assert len(execution_selects) == 1

        live_request = RequestFactory().get("/admin/live/")
        live_request.user = get_user_model().objects.create_superuser(
            username="protocol-corruption-admin"
        )
        live_response = admin_obj.observability_view(live_request, str(execution.pk))
        live_payload = json.loads(live_response.content)
        assert live_payload["created_with_django_ray_version"] is None
        assert live_payload["managed_with_django_ray_version"] == exact
        assert live_payload["executor_django_ray_version"] is None
        assert "CANARY_ADMIN_DETAIL" not in live_response.content.decode("utf-8")

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

    @override_settings(RAY_DASHBOARD_URL="http://localhost:8265")
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
            input_reference="password=input-reference-secret",
            result_data='{"password":"result-secret"}',
            result_reference="password=result-reference-secret",
            error_message="password=error-secret",
        )

        rendered = " ".join(
            (
                admin_obj.args_json_display(task),
                admin_obj.input_reference_display(task),
                admin_obj.result_data_display(task),
                admin_obj.result_reference_display(task),
                admin_obj.error_message_display(task),
            )
        )

        assert "admin-secret" not in rendered
        assert "input-reference-secret" not in rendered
        assert "result-secret" not in rendered
        assert "result-reference-secret" not in rendered
        assert "error-secret" not in rendered
        assert "[REDACTED]" in rendered
        assert "input_reference" not in admin_obj.readonly_fields
        assert "result_reference" not in admin_obj.readonly_fields
        fieldset_fields = {
            field for _, options in admin_obj.fieldsets for field in options.get("fields", ())
        }
        assert "input_reference" not in fieldset_fields
        assert "result_reference" not in fieldset_fields
        assert {"input_reference_display", "result_reference_display"} <= fieldset_fields

    @pytest.mark.django_db
    def test_sensitive_mapping_key_text_is_not_rendered_in_task_fields(self, settings) -> None:
        marker = "SYNTHETIC_ADMIN_MAPPING_KEY_MARKER"
        sensitive_key = f"api_token={marker}"
        settings.DJANGO_RAY = {"REDACT_PATTERNS": [r"api[_-]?token"]}
        admin_obj = _task_admin()
        task = RayTaskExecution.objects.create(
            task_id="admin-redacted-mapping-key-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.SUCCEEDED,
            args_json=json.dumps([{sensitive_key: "ordinary-argument-value"}]),
            result_data=json.dumps({sensitive_key: "ordinary-result-value"}),
        )

        rendered = " ".join(
            (
                admin_obj.args_json_display(task),
                admin_obj.result_data_display(task),
            )
        )

        assert marker not in rendered
        assert "api_token" not in rendered
        assert "ordinary-argument-value" not in rendered
        assert "ordinary-result-value" not in rendered
        assert '"<redacted>": "[REDACTED]"' in rendered

    def test_execution_references_are_bounded(self) -> None:
        admin_obj = _task_admin()
        task = RayTaskExecution(
            input_reference="i" * (ADMIN_DIAGNOSTIC_MAX_CHARS + 100),
            result_reference="r" * (ADMIN_DIAGNOSTIC_MAX_CHARS + 100),
        )

        assert admin_obj.input_reference_display(task).endswith("... [truncated]")
        assert admin_obj.result_reference_display(task).endswith("... [truncated]")
        assert len(admin_obj.input_reference_display(task)) <= ADMIN_DIAGNOSTIC_MAX_CHARS
        assert len(admin_obj.result_reference_display(task)) <= ADMIN_DIAGNOSTIC_MAX_CHARS

    @pytest.mark.django_db
    def test_task_error_message_is_bounded(self) -> None:
        admin_obj = _task_admin()
        task = RayTaskExecution.objects.create(
            task_id="admin-bounded-error-001",
            callable_path="testproject.tasks.add_numbers",
            state=TaskState.FAILED,
            error_message="safe error " + ("x" * (ADMIN_DIAGNOSTIC_MAX_CHARS + 100)),
        )

        rendered = admin_obj.error_message_display(task)

        assert len(rendered) <= ADMIN_DIAGNOSTIC_MAX_CHARS
        assert rendered.endswith("... [truncated]")

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
        assert f"/admin/django_ray/raytaskexecution/{execution.pk}/delete/" not in read_content
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
            cancellation_error="\x1b[33mstop uncertain\x1b[39m\rmanual review",
            error_message="password=error-secret",
            error_traceback="pass\x1b[31mword=traceback-secret",
        )

        assert admin_obj.args_json_display(task) == "-"
        assert admin_obj.kwargs_json_display(task) == '{"value": 1}'
        assert admin_obj.result_data_display(task) == (
            "Stored JSON omitted because it could not be parsed and redacted safely."
        )
        assert admin_obj.progress_data_display(task) == '{"step": 1}'
        assert admin_obj.completion_data_display(task) == '{"success": true}'
        assert admin_obj.cancellation_error_display(task) == "stop uncertain\nmanual review"
        assert admin_obj.error_message_display(task) == "[REDACTED]"
        assert "[REDACTED]" in admin_obj.error_traceback_display(task)

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

    @override_settings(RAY_DASHBOARD_URL="https://ray.example.com")
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
        assert "%3Cimg" in link

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
            created_with_django_ray_version="password=producer-secret",
            managed_with_django_ray_version="0.5.0-manager",
            executor_django_ray_version="password=executor-secret",
            error_message="password=admin-live-secret",
        )
        TaskWorkerLease.objects.create(
            worker_id="protocol-live-summary-reader",
            hostname="protocol-live-summary-host",
            pid=4103,
            queue_name="another-queue",
            capability_schema_version=1,
            django_ray_version="0.5.0-reader",
            min_supported_execution_protocol_version=1,
            max_supported_execution_protocol_version=1,
            legacy_admission_token=None,
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
        assert payload["execution_protocol_version"] == 1
        assert payload["created_with_django_ray_version"] == "[REDACTED]"
        assert payload["managed_with_django_ray_version"] == "0.5.0-manager"
        assert payload["executor_django_ray_version"] == "[REDACTED]"
        assert payload["protocol_compatible_worker_available"] is True
        assert payload["queue_capacity_attested"] is False
        assert payload["error_message"] == "[REDACTED]"
        assert payload["workflow"] is None
        assert "admin-live-secret" not in response.content.decode("utf-8")
        assert "producer-secret" not in response.content.decode("utf-8")
        assert "executor-secret" not in response.content.decode("utf-8")
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
            ("limit", TaskState.SUCCEEDED, "full", "LIMIT_EXCEEDED", "limit"),
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
                terminal = summary_case in {"expired", "limit"}
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
                elif summary_case in {"missing", "expired", "limit"}:
                    summary["detail"] = {
                        "availability": "LIMIT_EXCEEDED"
                        if summary_case == "limit"
                        else summary_case.upper(),
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
            "LIMIT_EXCEEDED",
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
        assert "no graph details were saved" in observed["LIMIT_EXCEEDED"]["message"]
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
