"""Security tests for Django Admin object scoping and sensitive-data authorization."""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import RequestFactory

from django_ray.admin import RayTaskExecutionAdmin, TaskAttemptAdmin
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState


def _make_staff_user(*, username: str, permissions: tuple[tuple[type, str], ...]):
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username=username,
        password="test-password-123!",
        is_staff=True,
    )
    for model, codename in permissions:
        content_type = ContentType.objects.get_for_model(model)
        perm = Permission.objects.get(content_type=content_type, codename=codename)
        user.user_permissions.add(perm)
    return user


def _make_execution_and_attempt(*, task_id: str, queue_name: str = "default"):
    execution = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path="testproject.tasks.echo_task",
        queue_name=queue_name,
        args_json='["secret-input-arg"]',
        kwargs_json='{"secret": "raw-value"}',
        state=TaskState.FAILED,
        attempt_number=1,
        execution_generation=1,
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        error_message="attempt failed",
    )
    return execution, attempt


def _render_change_form_sensitive_url(
    attempt_admin: TaskAttemptAdmin,
    request,
    attempt: TaskAttempt,
    monkeypatch,
) -> object:
    from django.contrib.admin import ModelAdmin
    from django.http import HttpResponse

    captured: dict[str, object] = {}

    def fake_super_render(self, req, context, *args, **kwargs):
        captured.update(context)
        return HttpResponse("ok")

    monkeypatch.setattr(ModelAdmin, "render_change_form", fake_super_render)
    context: dict[str, object] = {}
    attempt_admin.render_change_form(request, context, obj=attempt)
    return captured.get("django_ray_sensitive_data_url")


@pytest.mark.django_db
class TestTaskAttemptParentAuthorization:
    """TaskAttemptAdmin must enforce parent RayTaskExecutionAdmin permissions and scoping."""

    def test_attempt_sensitive_view_requires_parent_execution_view_permission(
        self, monkeypatch
    ) -> None:
        execution, attempt = _make_execution_and_attempt(task_id="attempt-parent-perm-1")
        del execution
        site = AdminSite(name="test-admin-parent-perm")
        site.register(RayTaskExecution, RayTaskExecutionAdmin)
        site.register(TaskAttempt, TaskAttemptAdmin)
        attempt_admin = site._registry[TaskAttempt]

        # User has view_taskattempt + view_sensitive_task_data, but NOT view_raytaskexecution.
        user = _make_staff_user(
            username="attempt-only-reader",
            permissions=(
                (TaskAttempt, "view_taskattempt"),
                (RayTaskExecution, "view_sensitive_task_data"),
            ),
        )
        request = RequestFactory().get(
            f"/admin/django_ray/taskattempt/{attempt.pk}/sensitive-data/"
        )
        request.user = user

        with pytest.raises(PermissionDenied):
            attempt_admin.sensitive_data_view(request, str(attempt.pk))

        assert (
            _render_change_form_sensitive_url(attempt_admin, request, attempt, monkeypatch) is None
        )

    def test_attempt_sensitive_view_enforces_parent_queryset_scoping(self, monkeypatch) -> None:
        allowed_exec, allowed_attempt = _make_execution_and_attempt(
            task_id="attempt-tenant-a",
            queue_name="tenant-a",
        )
        _denied_exec, denied_attempt = _make_execution_and_attempt(
            task_id="attempt-tenant-b",
            queue_name="tenant-b",
        )

        class TenantScopedExecutionAdmin(RayTaskExecutionAdmin):
            def get_queryset(self, request):
                return super().get_queryset(request).filter(queue_name="tenant-a")

        site = AdminSite(name="admin")
        site.register(RayTaskExecution, TenantScopedExecutionAdmin)
        site.register(TaskAttempt, TaskAttemptAdmin)
        attempt_admin = site._registry[TaskAttempt]

        user = _make_staff_user(
            username="tenant-a-reader",
            permissions=(
                (TaskAttempt, "view_taskattempt"),
                (RayTaskExecution, "view_raytaskexecution"),
                (RayTaskExecution, "view_sensitive_task_data"),
            ),
        )
        request = RequestFactory().get(
            f"/admin/django_ray/taskattempt/{denied_attempt.pk}/sensitive-data/"
        )
        request.user = user

        # Out-of-scope parent execution must not be served and link must be withheld.
        with pytest.raises(Http404):
            attempt_admin.sensitive_data_view(request, str(denied_attempt.pk))

        assert (
            _render_change_form_sensitive_url(attempt_admin, request, denied_attempt, monkeypatch)
            is None
        )

        # In-scope parent execution succeeds.
        allowed_request = RequestFactory().get(
            f"/admin/django_ray/taskattempt/{allowed_attempt.pk}/sensitive-data/"
        )
        allowed_request.user = user
        response = attempt_admin.sensitive_data_view(allowed_request, str(allowed_attempt.pk))
        assert response.status_code == 200
        assert allowed_exec.task_id.encode() in response.content

        assert (
            _render_change_form_sensitive_url(
                attempt_admin, allowed_request, allowed_attempt, monkeypatch
            )
            is not None
        )

    def test_attempt_sensitive_view_enforces_parent_object_level_has_view_permission(
        self, monkeypatch
    ) -> None:
        _allowed_exec, allowed_attempt = _make_execution_and_attempt(
            task_id="attempt-obj-allow",
            queue_name="public",
        )
        _denied_exec, denied_attempt = _make_execution_and_attempt(
            task_id="attempt-obj-deny",
            queue_name="restricted",
        )

        class ObjectScopedExecutionAdmin(RayTaskExecutionAdmin):
            def has_view_permission(self, request, obj=None) -> bool:
                if not super().has_view_permission(request, obj):
                    return False
                if obj is None:
                    return True
                return obj.queue_name == "public"

        site = AdminSite(name="admin")
        site.register(RayTaskExecution, ObjectScopedExecutionAdmin)
        site.register(TaskAttempt, TaskAttemptAdmin)
        attempt_admin = site._registry[TaskAttempt]

        user = _make_staff_user(
            username="object-acl-reader",
            permissions=(
                (TaskAttempt, "view_taskattempt"),
                (RayTaskExecution, "view_raytaskexecution"),
                (RayTaskExecution, "view_sensitive_task_data"),
            ),
        )
        denied_request = RequestFactory().get(
            f"/admin/django_ray/taskattempt/{denied_attempt.pk}/sensitive-data/"
        )
        denied_request.user = user

        with pytest.raises(PermissionDenied):
            attempt_admin.sensitive_data_view(denied_request, str(denied_attempt.pk))

        assert (
            _render_change_form_sensitive_url(
                attempt_admin, denied_request, denied_attempt, monkeypatch
            )
            is None
        )

        allowed_request = RequestFactory().get(
            f"/admin/django_ray/taskattempt/{allowed_attempt.pk}/sensitive-data/"
        )
        allowed_request.user = user
        response = attempt_admin.sensitive_data_view(allowed_request, str(allowed_attempt.pk))
        assert response.status_code == 200

    def test_attempt_sensitive_view_fails_closed_when_parent_admin_unregistered(
        self, monkeypatch
    ) -> None:
        _execution, attempt = _make_execution_and_attempt(task_id="attempt-unregistered-parent")
        site = AdminSite(name="test-admin-no-parent")
        site.register(TaskAttempt, TaskAttemptAdmin)
        attempt_admin = site._registry[TaskAttempt]

        user = _make_staff_user(
            username="unregistered-parent-reader",
            permissions=(
                (TaskAttempt, "view_taskattempt"),
                (RayTaskExecution, "view_raytaskexecution"),
                (RayTaskExecution, "view_sensitive_task_data"),
            ),
        )
        request = RequestFactory().get(
            f"/admin/django_ray/taskattempt/{attempt.pk}/sensitive-data/"
        )
        request.user = user

        with pytest.raises(PermissionDenied):
            attempt_admin.sensitive_data_view(request, str(attempt.pk))

        assert (
            _render_change_form_sensitive_url(attempt_admin, request, attempt, monkeypatch) is None
        )


@pytest.mark.django_db
class TestAdminHardeningControls:
    """Authz-before-lookup, bounded bulk actions, per-object authz, and nosniff headers."""

    def test_custom_admin_views_authorize_before_db_lookup_for_missing_ids(self) -> None:
        site = AdminSite(name="admin")
        site.register(RayTaskExecution, RayTaskExecutionAdmin)
        site.register(TaskAttempt, TaskAttemptAdmin)
        exec_admin = site._registry[RayTaskExecution]
        attempt_admin = site._registry[TaskAttempt]

        user = _make_staff_user(username="no-perms-staff", permissions=())
        request = RequestFactory().get("/admin/django_ray/raytaskexecution/999999/sensitive-data/")
        request.user = user

        with pytest.raises(PermissionDenied):
            exec_admin.sensitive_data_view(request, "999999")
        with pytest.raises(PermissionDenied):
            exec_admin.observability_view(request, "999999")
        with pytest.raises(PermissionDenied):
            exec_admin.workflow_node_detail_view(request, "999999")
        with pytest.raises(PermissionDenied):
            attempt_admin.sensitive_data_view(request, "999999")

    def test_cancel_tasks_rejects_oversized_bulk_selection_without_cancelling(
        self, monkeypatch
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock

        from django.contrib import messages

        from django_ray import admin as admin_module

        site = AdminSite(name="admin")
        site.register(RayTaskExecution, RayTaskExecutionAdmin)
        exec_admin = site._registry[RayTaskExecution]
        user = _make_staff_user(
            username="bulk-canceller",
            permissions=((RayTaskExecution, "change_raytaskexecution"),),
        )
        request = RequestFactory().post("/admin/django_ray/raytaskexecution/")
        request.user = user

        rows = [
            SimpleNamespace(pk=idx, attempt_number=1, execution_generation=1)
            for idx in range(1, admin_module.ADMIN_CANCEL_MAX_TASKS + 2)
        ]
        queryset = Mock()
        queryset.only.return_value = rows
        cancel_spy = Mock(side_effect=AssertionError("must not cancel oversized batch"))
        monkeypatch.setattr(admin_module, "request_task_cancellation", cancel_spy)
        recorded_messages: list[tuple[str, int]] = []
        monkeypatch.setattr(
            exec_admin,
            "message_user",
            lambda _req, msg, level=messages.INFO: recorded_messages.append((msg, level)),
        )

        exec_admin.cancel_tasks(request, queryset)

        cancel_spy.assert_not_called()
        assert len(recorded_messages) == 1
        assert recorded_messages[0][1] == messages.ERROR
        assert "No tasks were cancelled" in recorded_messages[0][0]

    def test_cancel_and_retry_actions_enforce_per_object_change_permission(
        self, monkeypatch
    ) -> None:
        from unittest.mock import Mock

        from django_ray import admin as admin_module

        allowed_exec, _ = _make_execution_and_attempt(task_id="action-allow", queue_name="public")
        denied_exec, _ = _make_execution_and_attempt(task_id="action-deny", queue_name="secret")

        class ObjectChangeScopedAdmin(RayTaskExecutionAdmin):
            def has_view_permission(self, request, obj=None) -> bool:
                if not super().has_view_permission(request, obj):
                    return False
                if obj is None:
                    return True
                return getattr(obj, "queue_name", None) == "public"

        site = AdminSite(name="admin")
        site.register(RayTaskExecution, ObjectChangeScopedAdmin)
        exec_admin = site._registry[RayTaskExecution]
        user = _make_staff_user(
            username="scoped-changer",
            permissions=((RayTaskExecution, "change_raytaskexecution"),),
        )
        request = RequestFactory().post("/admin/django_ray/raytaskexecution/")
        request.user = user

        cancel_spy = Mock(side_effect=AssertionError("must not cancel when any object denied"))
        monkeypatch.setattr(admin_module, "request_task_cancellation", cancel_spy)
        qs = RayTaskExecution.objects.filter(pk__in=[allowed_exec.pk, denied_exec.pk])
        with pytest.raises(PermissionDenied):
            exec_admin.cancel_tasks(request, qs)
        cancel_spy.assert_not_called()

        with pytest.raises(PermissionDenied):
            exec_admin.retry_tasks(request, qs)

    def test_admin_workflow_json_views_set_nosniff_and_no_store(self) -> None:
        execution, _ = _make_execution_and_attempt(task_id="nosniff-exec-1")
        site = AdminSite(name="admin")
        site.register(RayTaskExecution, RayTaskExecutionAdmin)
        exec_admin = site._registry[RayTaskExecution]
        user = _make_staff_user(
            username="workflow-viewer",
            permissions=((RayTaskExecution, "view_raytaskexecution"),),
        )
        request = RequestFactory().get(
            f"/admin/django_ray/raytaskexecution/{execution.pk}/workflow/node/"
        )
        request.user = user
        response = exec_admin.workflow_node_detail_view(request, str(execution.pk))
        assert response.status_code == 400
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Cache-Control"] == "no-store"
