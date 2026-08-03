"""Permission and response-safety tests for unredacted Admin diagnostics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import QuerySet
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import strip_tags

import django_ray.admin as django_ray_admin
from django_ray.admin import (
    ADMIN_DIAGNOSTIC_MAX_CHARS,
    ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES,
    ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

_ADMIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
_SENSITIVE_PERMISSION = "view_sensitive_task_data"
_LINK_LABEL = "Sensitive data"
_REPOSITORY_ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _sensitive_admin_settings(settings) -> None:
    settings.MIDDLEWARE = _ADMIN_MIDDLEWARE
    settings.DJANGO_RAY = {
        **settings.DJANGO_RAY,
        "REDACT_PATTERNS": [r"sensitive-marker"],
    }


@pytest.fixture
def sensitive_history() -> dict[str, Any]:
    execution = RayTaskExecution.objects.create(
        task_id="admin-sensitive-diagnostics-001",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.SUCCEEDED,
        attempt_number=3,
        execution_generation=3,
        args_json=json.dumps(["args-sensitive-marker"]),
        kwargs_json=json.dumps({"password": "kwargs-sensitive-marker"}),
        input_reference="input-reference-sensitive-marker",
        result_data=json.dumps({"result": "current-result-sensitive-marker"}),
        result_reference="current-result-reference-sensitive-marker",
        cancellation_error="current-cancellation-sensitive-marker",
        error_message="<script>alert('xss-sensitive-marker')</script>",
        error_traceback="current-traceback-sensitive-marker",
        completion_data=json.dumps({"secret": "completion-sensitive-marker"}),
        runtime_env_json=json.dumps(
            {"env_vars": {"PRIVATE_VALUE": "runtime-env-sensitive-marker"}}
        ),
    )
    first_attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        result_data=json.dumps({"result": "attempt-one-result-sensitive-marker"}),
        result_reference="attempt-one-result-reference-sensitive-marker",
        error_message="attempt-one-error-sensitive-marker",
        error_traceback="attempt-one-traceback-sensitive-marker",
    )
    second_attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=2,
        state=TaskState.FAILED,
        result_data=json.dumps({"result": "attempt-two-result-sensitive-marker"}),
        result_reference="attempt-two-result-reference-sensitive-marker",
        error_message="attempt-two-error-sensitive-marker",
        error_traceback="attempt-two-traceback-sensitive-marker",
    )
    return {
        "execution": execution,
        "first_attempt": first_attempt,
        "second_attempt": second_attempt,
    }


def _permission(codename: str) -> Permission:
    return Permission.objects.get(
        content_type__app_label="django_ray",
        codename=codename,
    )


def _staff_user(username: str, *codenames: str):
    user = get_user_model().objects.create_user(
        username=username,
        is_staff=True,
    )
    user.user_permissions.add(*(_permission(codename) for codename in codenames))
    return user


def _execution_urls(execution: RayTaskExecution) -> tuple[str, str]:
    return (
        reverse(
            "admin:django_ray_raytaskexecution_change",
            args=[execution.pk],
        ),
        reverse(
            "admin:django_ray_raytaskexecution_sensitive_data",
            args=[execution.pk],
        ),
    )


def _attempt_urls(attempt: TaskAttempt) -> tuple[str, str]:
    return (
        reverse(
            "admin:django_ray_taskattempt_change",
            args=[attempt.pk],
        ),
        reverse(
            "admin:django_ray_taskattempt_sensitive_data",
            args=[attempt.pk],
        ),
    )


def test_ordinary_details_stay_redacted_and_hide_unredacted_link(
    client,
    sensitive_history,
) -> None:
    user = _staff_user(
        "sensitive-redacted-viewer",
        "view_raytaskexecution",
        "view_taskattempt",
    )
    client.force_login(user)
    execution = sensitive_history["execution"]
    first_attempt = sensitive_history["first_attempt"]

    execution_response = client.get(_execution_urls(execution)[0])
    attempt_response = client.get(_attempt_urls(first_attempt)[0])

    assert execution_response.status_code == 200
    assert attempt_response.status_code == 200
    details = (
        (
            execution_response,
            reverse(
                "admin:django_ray_raytaskexecution_history",
                args=[execution.pk],
            ),
        ),
        (
            attempt_response,
            reverse(
                "admin:django_ray_taskattempt_history",
                args=[first_attempt.pk],
            ),
        ),
    )
    for response, history_url in details:
        content = response.content.decode("utf-8")
        assert "sensitive-marker" not in content
        assert "[REDACTED]" in content
        assert _LINK_LABEL not in content
        assert history_url not in content
        assert 'class="object-tools"' not in content


def test_unredacted_pages_require_both_sensitive_and_ordinary_view_permissions(
    client,
    sensitive_history,
) -> None:
    execution = sensitive_history["execution"]
    first_attempt = sensitive_history["first_attempt"]
    execution_sensitive_url = _execution_urls(execution)[1]
    attempt_sensitive_url = _attempt_urls(first_attempt)[1]

    ordinary_viewer = _staff_user(
        "sensitive-ordinary-only",
        "view_raytaskexecution",
        "view_taskattempt",
    )
    client.force_login(ordinary_viewer)
    for url in (execution_sensitive_url, attempt_sensitive_url):
        response = client.get(url)
        assert response.status_code == 403
        assert "sensitive-marker" not in response.content.decode("utf-8")

    sensitive_only = _staff_user(
        "sensitive-permission-only",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(sensitive_only)
    for url in (execution_sensitive_url, attempt_sensitive_url):
        response = client.get(url)
        assert response.status_code == 403
        assert "sensitive-marker" not in response.content.decode("utf-8")


def test_global_sensitive_permission_reveals_only_allowlisted_execution_fields(
    client,
    sensitive_history,
) -> None:
    user = _staff_user(
        "sensitive-global-execution-viewer",
        "view_raytaskexecution",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(user)
    execution = sensitive_history["execution"]
    detail_url, sensitive_url = _execution_urls(execution)

    detail = client.get(detail_url)
    response = client.get(sensitive_url)
    content = response.content.decode("utf-8")

    assert detail.status_code == 200
    assert "sensitive-marker" not in detail.content.decode("utf-8")
    assert _LINK_LABEL in detail.content.decode("utf-8")
    assert response.status_code == 200
    for marker in (
        "args-sensitive-marker",
        "kwargs-sensitive-marker",
        "input-reference-sensitive-marker",
        "current-result-sensitive-marker",
        "current-result-reference-sensitive-marker",
        "current-cancellation-sensitive-marker",
        "xss-sensitive-marker",
        "current-traceback-sensitive-marker",
    ):
        assert marker in content
    assert "completion-sensitive-marker" not in content
    assert "runtime-env-sensitive-marker" not in content
    assert "attempt-one-result-sensitive-marker" not in content


def test_archived_attempt_page_uses_parent_inputs_and_exact_attempt_outputs(
    client,
    sensitive_history,
) -> None:
    user = _staff_user(
        "sensitive-global-attempt-viewer",
        "view_taskattempt",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(user)
    first_attempt = sensitive_history["first_attempt"]
    detail_url, sensitive_url = _attempt_urls(first_attempt)

    detail = client.get(detail_url)
    response = client.get(sensitive_url)
    content = response.content.decode("utf-8")

    assert detail.status_code == 200
    assert "sensitive-marker" not in detail.content.decode("utf-8")
    assert _LINK_LABEL in detail.content.decode("utf-8")
    assert response.status_code == 200
    for marker in (
        "args-sensitive-marker",
        "kwargs-sensitive-marker",
        "input-reference-sensitive-marker",
        "attempt-one-result-sensitive-marker",
        "attempt-one-result-reference-sensitive-marker",
        "attempt-one-error-sensitive-marker",
        "attempt-one-traceback-sensitive-marker",
    ):
        assert marker in content
    for excluded in (
        "current-result-sensitive-marker",
        "current-result-reference-sensitive-marker",
        "current-cancellation-sensitive-marker",
        "current-traceback-sensitive-marker",
        "attempt-two-result-sensitive-marker",
        "attempt-two-result-reference-sensitive-marker",
        "attempt-two-error-sensitive-marker",
        "attempt-two-traceback-sensitive-marker",
        "completion-sensitive-marker",
        "runtime-env-sensitive-marker",
    ):
        assert excluded not in content


def test_superuser_can_view_execution_and_attempt_sensitive_data(
    client,
    sensitive_history,
) -> None:
    user = get_user_model().objects.create_superuser(username="sensitive-superuser")
    client.force_login(user)
    execution = sensitive_history["execution"]
    first_attempt = sensitive_history["first_attempt"]

    execution_response = client.get(_execution_urls(execution)[1])
    attempt_response = client.get(_attempt_urls(first_attempt)[1])

    assert execution_response.status_code == 200
    execution_content = execution_response.content.decode("utf-8")
    assert "current-result-sensitive-marker" in execution_content
    assert 'class="django-ray-sensitive-data__header"' in execution_content
    assert execution_content.count("<h1>Unredacted task data</h1>") == 1
    assert "django_ray/admin/diagnostics.css" in execution_content
    assert 'class="django-ray-admin-action django-ray-admin-action--secondary"' in execution_content
    assert attempt_response.status_code == 200
    assert "attempt-one-error-sensitive-marker" in attempt_response.content.decode("utf-8")


def test_read_only_task_details_hide_empty_django_admin_history(
    client,
    sensitive_history,
    monkeypatch,
) -> None:
    user = get_user_model().objects.create_superuser(username="task-detail-object-tools")
    client.force_login(user)
    execution = sensitive_history["execution"]
    attempt = sensitive_history["first_attempt"]
    execution_site_url = f"/runtime/executions/{execution.pk}/"
    attempt_site_url = f"/runtime/attempts/{attempt.pk}/"
    monkeypatch.setattr(
        admin.site._registry[RayTaskExecution],
        "view_on_site",
        lambda obj: f"/runtime/executions/{obj.pk}/",
    )
    monkeypatch.setattr(
        admin.site._registry[TaskAttempt],
        "view_on_site",
        lambda obj: f"/runtime/attempts/{obj.pk}/",
    )

    details = (
        (
            client.get(_execution_urls(execution)[0]),
            reverse(
                "admin:django_ray_raytaskexecution_history",
                args=[execution.pk],
            ),
            execution_site_url,
        ),
        (
            client.get(_attempt_urls(attempt)[0]),
            reverse(
                "admin:django_ray_taskattempt_history",
                args=[attempt.pk],
            ),
            attempt_site_url,
        ),
    )

    for response, history_url, site_url in details:
        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert history_url not in content
        assert 'class="historylink"' not in content
        assert f'href="{site_url}"' in content
        assert 'class="viewsitelink"' in content
        assert "View on site" in content
        assert _LINK_LABEL in content


def test_sensitive_permission_revocation_applies_to_the_next_request(
    client,
    sensitive_history,
) -> None:
    user = _staff_user(
        "sensitive-revoked-viewer",
        "view_raytaskexecution",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(user)
    execution = sensitive_history["execution"]
    sensitive_url = _execution_urls(execution)[1]

    allowed = client.get(sensitive_url)
    assert allowed.status_code == 200
    assert "current-result-sensitive-marker" in allowed.content.decode("utf-8")

    user.user_permissions.remove(_permission(_SENSITIVE_PERMISSION))
    denied = client.get(sensitive_url)

    assert denied.status_code == 403
    assert "sensitive-marker" not in denied.content.decode("utf-8")


def test_sensitive_pages_escape_html_are_get_only_and_disable_caching(
    client,
    sensitive_history,
) -> None:
    user = _staff_user(
        "sensitive-response-policy-viewer",
        "view_raytaskexecution",
        "view_taskattempt",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(user)
    execution = sensitive_history["execution"]
    first_attempt = sensitive_history["first_attempt"]
    sensitive_urls = (
        _execution_urls(execution)[1],
        _attempt_urls(first_attempt)[1],
    )

    for url in sensitive_urls:
        response = client.get(url)
        assert response.status_code == 200
        assert "no-store" in response.headers["Cache-Control"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert client.post(url).status_code == 405

    execution_content = client.get(sensitive_urls[0]).content.decode("utf-8")
    assert "<script>alert('xss-sensitive-marker')</script>" not in execution_content
    assert "&lt;script&gt;alert(" in execution_content
    assert "xss-sensitive-marker" in execution_content


def test_sensitive_pages_keep_text_unredacted_but_render_terminal_controls_inert(
    client,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="admin-sensitive-terminal-controls",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_message=("\x1b[31mprivileged-error-sensitive-marker\x1b[0m\rnext diagnostic line"),
        error_traceback=(
            "ray::task()\x1b]8;;https://example.test/private\x1b\\"
            "privileged-traceback-sensitive-marker\x1b]8;;\x1b\\\x00"
        ),
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        error_message="\x9b33mprivileged-attempt-sensitive-marker\x9b0m\rretry detail",
        error_traceback="\x1bPprivate-control-payload\x1b\\attempt traceback text",
    )
    user = _staff_user(
        "sensitive-terminal-controls-viewer",
        "view_raytaskexecution",
        "view_taskattempt",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(user)

    ordinary_responses = (
        client.get(_execution_urls(execution)[0]),
        client.get(_attempt_urls(attempt)[0]),
    )
    sensitive_responses = (
        client.get(_execution_urls(execution)[1]),
        client.get(_attempt_urls(attempt)[1]),
    )

    for response in ordinary_responses:
        assert response.status_code == 200
        assert "sensitive-marker" not in response.content.decode("utf-8")
    combined = "\n".join(response.content.decode("utf-8") for response in sensitive_responses)
    assert all(response.status_code == 200 for response in sensitive_responses)
    for visible in (
        "privileged-error-sensitive-marker",
        "next diagnostic line",
        "privileged-traceback-sensitive-marker",
        "privileged-attempt-sensitive-marker",
        "retry detail",
        "attempt traceback text",
    ):
        assert visible in combined
    for forbidden in ("\x00", "\x1b", "\x90", "\x9b", "\r"):
        assert forbidden not in combined
    assert "private-control-payload" not in combined
    assert "https://example.test/private" not in combined
    assert "Printable diagnostic text remains unredacted" in combined


def test_sensitive_page_enforces_the_rendered_response_byte_limit(
    client,
    sensitive_history,
    monkeypatch,
) -> None:
    user = _staff_user(
        "sensitive-response-limit-viewer",
        "view_raytaskexecution",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(user)
    execution = sensitive_history["execution"]
    sensitive_url = _execution_urls(execution)[1]
    oversized_branding = "oversized-admin-branding-" + (
        "x" * ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES
    )
    monkeypatch.setattr(admin.site, "site_header", oversized_branding)

    response = client.get(sensitive_url)

    assert response.status_code == 413
    assert len(response.content) <= ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES
    assert b"Sensitive diagnostics response limit reached" in response.content
    assert b"No stored diagnostic values were included" in response.content
    assert b"django-ray-sensitive-limit-page" in response.content
    assert b"django-ray-sensitive-data__warning" in response.content
    assert b"django-ray-admin-action--secondary" in response.content
    assert b"django_ray/admin/diagnostics.css" in response.content
    assert b"django_ray/admin/sensitive_task_data.css" in response.content
    assert b"django_ray/admin/sensitive_task_data_theme.js" in response.content
    assert b"oversized-admin-branding" not in response.content
    assert b"sensitive-marker" not in response.content
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_sensitive_page_theme_javascript_supports_stock_and_unfold_storage() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("Node.js is required for the sensitive Admin theme contract in CI")
        pytest.skip("Node.js is unavailable for the sensitive Admin theme contract")

    result = subprocess.run(
        [node, "--test", "tests/javascript/sensitive_task_data_theme.test.mjs"],
        cwd=_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("byte_length", "value"),
    (
        (None, "unexpected database value"),
        (True, "x"),
        (-1, "x"),
        (ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES + 1, "unexpected database value"),
        (1, object()),
        (1, "\ud800"),
        (2, "x"),
    ),
    ids=(
        "null-length-with-value",
        "boolean-length",
        "negative-length",
        "oversized-with-value",
        "non-text-value",
        "invalid-unicode-value",
        "byte-length-mismatch",
    ),
)
def test_sensitive_database_annotations_fail_closed(
    byte_length: object,
    value: object,
) -> None:
    """Treat impossible database projection pairs as integrity failures."""
    field_name = "error_message"
    bytes_name = django_ray_admin._sensitive_annotation_name(field_name, "bytes")
    value_name = django_ray_admin._sensitive_annotation_name(field_name, "value")
    fresh = SimpleNamespace(
        pk=1,
        **{
            bytes_name: byte_length,
            value_name: value,
        },
    )

    class FakeQuerySet:
        def annotate(self, **_annotations):
            return self

        def only(self, *_field_names):
            return self

        def get(self, *, pk):
            assert pk == fresh.pk
            return fresh

    with pytest.raises(ValueError, match="Sensitive diagnostic storage failed validation"):
        django_ray_admin._bounded_sensitive_object(
            FakeQuerySet(),
            pk=fresh.pk,
            field_names=(field_name,),
            identity_fields=("pk",),
        )


def test_sensitive_sections_distinguish_empty_and_response_limited_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain omitted values without confusing them with stored empty text."""
    empty_field = "result_data"
    limited_field = "error_message"
    monkeypatch.setattr(
        django_ray_admin,
        "ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES",
        70,
    )
    monkeypatch.setattr(
        django_ray_admin,
        "_ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES",
        64,
    )
    row = {
        django_ray_admin._sensitive_annotation_name(empty_field, "bytes"): 0,
        django_ray_admin._sensitive_annotation_name(empty_field, "value"): "",
        django_ray_admin._sensitive_annotation_name(limited_field, "bytes"): 2,
        django_ray_admin._sensitive_annotation_name(limited_field, "value"): "xx",
    }

    sections = django_ray_admin._sensitive_sections(
        row,
        (("Diagnostics", (("Output", empty_field), ("Error", limited_field))),),
    )

    empty, limited = sections[0]["fields"]
    assert empty["status"] == "empty"
    assert empty["value"] == ""
    assert limited["status"] == "response_limit"
    assert limited["value"] is None


def test_sensitive_sections_explain_values_empty_after_terminal_normalization() -> None:
    field_name = "error_traceback"
    row = {
        django_ray_admin._sensitive_annotation_name(field_name, "bytes"): 4,
        django_ray_admin._sensitive_annotation_name(field_name, "value"): "\x1b[0m",
    }

    sections = django_ray_admin._sensitive_sections(
        row,
        (("Diagnostics", (("Traceback", field_name),)),),
    )

    field = sections[0]["fields"][0]
    assert field["status"] == "normalized_empty"
    assert field["value"] == ""


def test_object_specific_grants_are_checked_against_the_exact_owning_execution(
    sensitive_history,
    monkeypatch,
) -> None:
    execution = sensitive_history["execution"]
    allowed_attempt = sensitive_history["first_attempt"]
    denied_execution = RayTaskExecution.objects.create(
        task_id="admin-sensitive-object-denied",
        callable_path="testproject.tasks.add_numbers",
        error_message="denied-object-sensitive-marker",
    )
    denied_attempt = TaskAttempt.objects.create(
        execution=denied_execution,
        attempt_number=1,
        state=TaskState.FAILED,
        error_message="denied-attempt-sensitive-marker",
    )
    user = get_user_model().objects.create_user(
        username="sensitive-object-viewer",
        is_staff=True,
    )

    def has_perm(permission: str, obj: object | None = None) -> bool:
        if permission == "django_ray.view_raytaskexecution":
            return obj == execution
        if permission == "django_ray.view_taskattempt":
            return obj == allowed_attempt
        if permission == "django_ray.view_sensitive_task_data":
            return obj == execution
        return False

    monkeypatch.setattr(user, "has_perm", has_perm)
    request_factory = RequestFactory()
    execution_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
    attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)

    execution_request = request_factory.get(_execution_urls(execution)[1])
    execution_request.user = user
    execution_response = execution_admin.sensitive_data_view(
        execution_request,
        str(execution.pk),
    )
    execution_response.render()
    assert "current-result-sensitive-marker" in execution_response.content.decode("utf-8")

    attempt_request = request_factory.get(_attempt_urls(allowed_attempt)[1])
    attempt_request.user = user
    attempt_response = attempt_admin.sensitive_data_view(
        attempt_request,
        str(allowed_attempt.pk),
    )
    attempt_response.render()
    assert "attempt-one-error-sensitive-marker" in attempt_response.content.decode("utf-8")

    denied_execution_request = request_factory.get(_execution_urls(denied_execution)[1])
    denied_execution_request.user = user
    with pytest.raises(PermissionDenied):
        execution_admin.sensitive_data_view(
            denied_execution_request,
            str(denied_execution.pk),
        )

    denied_attempt_request = request_factory.get(_attempt_urls(denied_attempt)[1])
    denied_attempt_request.user = user
    with pytest.raises(PermissionDenied):
        attempt_admin.sensitive_data_view(
            denied_attempt_request,
            str(denied_attempt.pk),
        )


def test_authorization_and_sensitive_reads_stay_on_the_initial_database_alias(
    sensitive_history,
    monkeypatch,
) -> None:
    execution = sensitive_history["execution"]
    attempt = sensitive_history["first_attempt"]
    user = get_user_model().objects.create_superuser(username="sensitive-routed-admin")
    request_factory = RequestFactory()
    execution_admin = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
    attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)
    routed_alias = "sensitive-diagnostics-route"
    routed_models = {RayTaskExecution, TaskAttempt}
    routed_reads: list[tuple[type[Any], str]] = []
    original_get = QuerySet.get
    original_using = QuerySet.using

    def routed_get(queryset, *args, **kwargs):
        instance = original_get(queryset, *args, **kwargs)
        if queryset.model in routed_models and queryset._db is None:
            instance._state.db = routed_alias
        return instance

    def routed_using(queryset, alias):
        if queryset.model in routed_models and alias == routed_alias:
            routed_reads.append((queryset.model, alias))
            return original_using(queryset, "default")
        return original_using(queryset, alias)

    monkeypatch.setattr(QuerySet, "get", routed_get)
    monkeypatch.setattr(QuerySet, "using", routed_using)

    execution_request = request_factory.get(_execution_urls(execution)[1])
    execution_request.user = user
    execution_response = execution_admin.sensitive_data_view(
        execution_request,
        str(execution.pk),
    )
    assert execution_response.status_code == 200
    assert "current-result-sensitive-marker" in execution_response.content.decode("utf-8")
    assert routed_reads == [(RayTaskExecution, routed_alias)]

    routed_reads.clear()
    attempt_request = request_factory.get(_attempt_urls(attempt)[1])
    attempt_request.user = user
    attempt_response = attempt_admin.sensitive_data_view(
        attempt_request,
        str(attempt.pk),
    )
    assert attempt_response.status_code == 200
    assert "attempt-one-error-sensitive-marker" in attempt_response.content.decode("utf-8")
    assert routed_reads == [
        (RayTaskExecution, routed_alias),
        (RayTaskExecution, routed_alias),
        (TaskAttempt, routed_alias),
    ]


def test_unicode_byte_limit_is_enforced_in_sql_before_values_reach_python(
    client,
) -> None:
    exact_value = ("🙂" * ((ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES - 4) // 4)) + "ABCD"
    assert len(exact_value.encode("utf-8")) == ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES
    execution = RayTaskExecution.objects.create(
        task_id="admin-sensitive-byte-boundary",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_traceback=exact_value,
    )
    user = _staff_user(
        "sensitive-byte-boundary-viewer",
        "view_raytaskexecution",
        _SENSITIVE_PERMISSION,
    )
    client.force_login(user)
    sensitive_url = _execution_urls(execution)[1]

    exact_response = client.get(sensitive_url)
    assert exact_response.status_code == 200
    assert "ABCD" in exact_response.content.decode("utf-8")

    execution.error_traceback = exact_value + "é"
    execution.save(update_fields=["error_traceback"])
    with CaptureQueriesContext(connection) as queries:
        oversized_response = client.get(sensitive_url)

    oversized_content = oversized_response.content.decode("utf-8")
    assert oversized_response.status_code == 200
    assert exact_value not in oversized_content
    assert "ABCD" not in oversized_content
    assert "65538 bytes" in oversized_content
    assert "exceeds the 65536-byte" in oversized_content
    bounded_queries = [
        query["sql"]
        for query in queries.captured_queries
        if "admin_sensitive_error_traceback_value" in query["sql"]
    ]
    assert len(bounded_queries) == 1
    bounded_sql = bounded_queries[0].upper()
    assert "CASE WHEN" in bounded_sql
    assert "LENGTH(CAST(" in bounded_sql
    assert "AS BLOB" in bounded_sql


def test_execution_changelist_does_not_select_payload_columns(client) -> None:
    execution = RayTaskExecution.objects.create(
        task_id="admin-sensitive-changelist-query",
        callable_path="testproject.tasks.add_numbers",
        args_json='["changelist-payload-sensitive-marker"]',
        kwargs_json='{"password":"changelist-kwargs-sensitive-marker"}',
        result_data='{"result":"changelist-result-sensitive-marker"}',
        error_traceback="changelist-traceback-sensitive-marker",
    )
    user = get_user_model().objects.create_superuser(username="sensitive-changelist-admin")
    client.force_login(user)

    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("admin:django_ray_raytaskexecution_changelist"))

    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert str(execution.pk) in content
    assert "changelist-payload-sensitive-marker" not in content
    row_queries = [
        query["sql"]
        for query in queries.captured_queries
        if "django_ray_raytaskexecution" in query["sql"]
        and "ORDER BY" in query["sql"].upper()
        and "SELECT DISTINCT" not in query["sql"].upper()
    ]
    assert len(row_queries) == 1
    for field_name in (
        "args_json",
        "kwargs_json",
        "input_reference",
        "result_data",
        "result_reference",
        "completion_data",
        "cancellation_error",
        "error_message",
        "error_traceback",
        "runtime_env_json",
        "workflow_plan_json",
        "workflow_plan_selection",
        "workflow_progress_summary_json",
    ):
        assert f'"{field_name}"' not in row_queries[0]


def test_ordinary_execution_errors_are_redacted_and_bounded() -> None:
    admin_obj = RayTaskExecutionAdmin(RayTaskExecution, admin.site)
    execution = RayTaskExecution(
        cancellation_error="cancellation-sensitive-marker",
        error_message="safe " + ("x" * (ADMIN_DIAGNOSTIC_MAX_CHARS + 100)),
        error_traceback="sensitive-marker=" + ("y" * 100),
    )

    cancellation = admin_obj.cancellation_error_display(execution)
    message = admin_obj.error_message_display(execution)
    traceback = admin_obj.error_traceback_display(execution)

    assert cancellation == "[REDACTED]"
    assert len(message) <= ADMIN_DIAGNOSTIC_MAX_CHARS
    assert message.endswith("... [truncated]")
    assert strip_tags(str(traceback)) == "[REDACTED]"
