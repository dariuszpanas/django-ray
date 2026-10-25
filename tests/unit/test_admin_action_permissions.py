"""Resource-free authorization checks for task actions."""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from django.contrib.admin import AdminSite, helpers
from django.core.exceptions import PermissionDenied
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test import RequestFactory

from django_ray import admin as task_admin
from django_ray.admin import RayTaskExecutionAdmin
from django_ray.lifecycle import TaskCancellationRequestStatus
from django_ray.models import RayTaskExecution

VIEW_PERMISSION = "django_ray.view_raytaskexecution"
CHANGE_PERMISSION = "django_ray.change_raytaskexecution"


def _request(*permissions):
    request = RequestFactory().post(
        "/admin/django_ray/raytaskexecution/",
        {"action": "cancel_tasks", helpers.ACTION_CHECKBOX_NAME: ["101", "103"]},
    )
    request.user = SimpleNamespace(
        is_authenticated=True,
        is_active=True,
        is_staff=True,
        has_perm=lambda permission, obj=None: permission in permissions,
        has_module_perms=lambda app_label: app_label == "django_ray",
    )
    return request


@pytest.fixture
def action_context(monkeypatch):
    database = Mock(side_effect=AssertionError("Database access is outside this test contract"))
    monkeypatch.setattr(BaseDatabaseWrapper, "ensure_connection", database)
    model_admin = RayTaskExecutionAdmin(RayTaskExecution, AdminSite())
    messages = Mock()
    monkeypatch.setattr(model_admin, "message_user", messages)
    service = Mock(
        return_value=SimpleNamespace(accepted=True, status=TaskCancellationRequestStatus.ACCEPTED)
    )
    monkeypatch.setattr(task_admin, "request_task_cancellation", service)
    queryset = Mock()
    queryset.filter.return_value = queryset
    queryset.only.return_value = [
        SimpleNamespace(pk=101, attempt_number=3, execution_generation=7),
        SimpleNamespace(pk=103, attempt_number=5, execution_generation=11),
    ]
    yield SimpleNamespace(admin=model_admin, service=service, queryset=queryset, messages=messages)
    database.assert_not_called()


@pytest.mark.parametrize("permissions", [(), (VIEW_PERMISSION,)])
def test_cancellation_action_is_hidden_without_change_permission(action_context, permissions):
    request = _request(*permissions)

    assert not action_context.admin.has_change_permission(request)
    assert "cancel_tasks" not in action_context.admin.get_actions(request)
    action_context.service.assert_not_called()


@pytest.mark.parametrize("permissions", [(), (VIEW_PERMISSION,)])
def test_cancellation_post_without_change_permission_cannot_reach_selection(
    action_context, permissions
):
    response = action_context.admin.response_action(_request(*permissions), action_context.queryset)

    assert response is None
    action_context.queryset.filter.assert_not_called()
    action_context.queryset.only.assert_not_called()
    action_context.service.assert_not_called()


@pytest.mark.parametrize("permissions", [(), (VIEW_PERMISSION,)])
def test_direct_cancellation_requires_change_permission_before_selection(
    action_context, permissions
):
    with pytest.raises(PermissionDenied):
        action_context.admin.cancel_tasks(_request(*permissions), action_context.queryset)

    action_context.queryset.only.assert_not_called()
    action_context.service.assert_not_called()
    action_context.messages.assert_not_called()


@pytest.mark.parametrize("dispatch", ["action", "direct"])
def test_authorized_cancellation_preserves_each_selected_execution_fence(action_context, dispatch):
    request = _request(CHANGE_PERMISSION)
    assert "cancel_tasks" in action_context.admin.get_actions(request)

    if dispatch == "action":
        response = action_context.admin.response_action(request, action_context.queryset)
        assert response.status_code == 302
        action_context.queryset.filter.assert_called_once_with(pk__in=["101", "103"])
    else:
        action_context.admin.cancel_tasks(request, action_context.queryset)

    action_context.queryset.only.assert_called_once_with(
        "pk", "attempt_number", "execution_generation"
    )
    assert action_context.service.call_args_list == [
        call(101, expected_attempt_number=3, expected_execution_generation=7),
        call(103, expected_attempt_number=5, expected_execution_generation=11),
    ]
    assert "Accepted cancellation for 2 task(s)." in action_context.messages.call_args.args[1]


def test_direct_cancellation_honors_admin_permission_override(action_context, monkeypatch):
    monkeypatch.setattr(action_context.admin, "has_change_permission", lambda request: False)

    with pytest.raises(PermissionDenied):
        action_context.admin.cancel_tasks(_request(CHANGE_PERMISSION), action_context.queryset)

    action_context.queryset.only.assert_not_called()
    action_context.service.assert_not_called()
