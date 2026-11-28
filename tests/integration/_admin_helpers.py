"""Shared admin request, confirmation, and HTML helpers for admin integration tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.template.response import TemplateResponse
from django.test import RequestFactory
from django.test.html import Element

from django_ray.admin import (
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskExecutionProtocolPolicyAdmin,
    TaskWorkerLeaseAdmin,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskWorkerLease,
)
from django_ray.workflow.plans import (
    PLAN_DOMAIN_SEPARATOR,
    EffectiveWorkflowPlan,
)

_ADMIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


_TEST_RETRY_SESSION_NONCE = "r" * 43


_TEST_RETRY_ACTOR = SimpleNamespace(
    is_authenticated=True,
    is_active=True,
    is_staff=True,
    pk=7001,
    _meta=SimpleNamespace(label_lower="auth.user"),
    has_perm=lambda permission, obj=None: True,
    has_module_perms=lambda app_label: True,
)


def _request() -> Any:
    request = RequestFactory().post("/admin/")
    request.user = AnonymousUser()
    return request


def _retry_request(
    *,
    data: dict[str, str] | None = None,
    path: str = "/admin/",
    user: Any = _TEST_RETRY_ACTOR,
    session_nonce: str = _TEST_RETRY_SESSION_NONCE,
) -> Any:
    request = RequestFactory().post(path, data or {})
    request.user = user
    request.session = {
        "django_ray_admin_retry_confirmation_nonce_v1": session_nonce,
    }
    return request


def _retry_confirmation(
    admin_obj: RayTaskExecutionAdmin,
    queryset: Any,
) -> TemplateResponse:
    response = admin_obj.retry_tasks(_retry_request(), queryset)
    assert isinstance(response, TemplateResponse)
    response.render()
    return response


def _confirmed_retry_request(response: TemplateResponse) -> Any:
    context = _retry_confirmation_context(response)
    return _retry_request(
        data={
            "post": "yes",
            "retry_confirmation_token": context["confirmation_token"],
        },
    )


def _retry_confirmation_context(response: TemplateResponse) -> dict[str, Any]:
    context = response.context_data
    assert context is not None
    return context


def _task_admin() -> RayTaskExecutionAdmin:
    return RayTaskExecutionAdmin(RayTaskExecution, admin.site)


def _lease_admin() -> TaskWorkerLeaseAdmin:
    return TaskWorkerLeaseAdmin(TaskWorkerLease, admin.site)


def _attempt_admin() -> TaskAttemptAdmin:
    return TaskAttemptAdmin(TaskAttempt, admin.site)


def _protocol_policy_admin() -> TaskExecutionProtocolPolicyAdmin:
    return TaskExecutionProtocolPolicyAdmin(TaskExecutionProtocolPolicy, admin.site)


def _stored_plan_fingerprint(serialized: str) -> str:
    digest = hashlib.sha256(PLAN_DOMAIN_SEPARATOR + serialized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _walk_html(element: Element) -> Iterator[Element]:
    yield element
    for child in element.children:
        if isinstance(child, Element):
            yield from _walk_html(child)


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
