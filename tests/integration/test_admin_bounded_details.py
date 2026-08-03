"""Database and response bounds for ordinary task Admin diagnostics."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse, StreamingHttpResponse
from django.template.response import TemplateResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, resolve, reverse

import django_ray.admin as django_ray_admin
from django_ray.admin import (
    ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES,
    ADMIN_ATTEMPT_INLINE_MAX_BYTES,
    ADMIN_ATTEMPT_INLINE_MAX_ROWS,
    ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
    ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
    ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES,
    RayTaskExecutionAdmin,
    TaskAttemptAdmin,
    TaskAttemptInline,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

_ADMIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

pytestmark = pytest.mark.django_db


class BoundedDetailTemplateResponseMiddleware:
    """Prove ordinary detail responses retain Django's lazy middleware phase."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        response.context_data["title"] = "template-response-middleware-canary"
        return response


@pytest.fixture(autouse=True)
def _admin_settings(settings) -> None:
    settings.MIDDLEWARE = _ADMIN_MIDDLEWARE


@pytest.fixture
def admin_user():
    return get_user_model().objects.create_superuser(username="bounded-detail-admin")


def _admin_request(url: str, user):
    request = RequestFactory().get(url)
    request.user = user
    request.resolver_match = resolve(url)
    return request


def _table_selects(queries, table: str) -> list[str]:
    return [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT") and table in query["sql"]
    ]


@pytest.mark.parametrize(
    ("model", "model_path", "table"),
    (
        (RayTaskExecution, "raytaskexecution", "django_ray_raytaskexecution"),
        (TaskAttempt, "taskattempt", "django_ray_taskattempt"),
    ),
)
def test_readonly_admin_omits_stock_history_and_delete_routes(
    client,
    admin_user,
    model,
    model_path,
    table,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id=f"admin-removed-stock-routes-{model_path}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_message="removed-route-storage-canary",
    )
    obj = (
        execution
        if model is RayTaskExecution
        else TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="removed-route-attempt-storage-canary",
        )
    )
    client.force_login(admin_user)

    for suffix in ("history", "delete"):
        with pytest.raises(NoReverseMatch):
            reverse(f"admin:django_ray_{model_path}_{suffix}", args=[obj.pk])
        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"/admin/django_ray/{model_path}/{obj.pk}/{suffix}/")

        assert response.status_code == 404
        assert _table_selects(queries, table) == []


@pytest.mark.parametrize(
    ("model", "url_name", "response_limit_name", "table"),
    (
        (
            RayTaskExecution,
            "admin:django_ray_raytaskexecution_change",
            "ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES",
            "django_ray_raytaskexecution",
        ),
        (
            TaskAttempt,
            "admin:django_ray_taskattempt_change",
            "ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES",
            "django_ray_taskattempt",
        ),
    ),
)
def test_readonly_admin_change_methods_are_bounded_or_rejected_before_query(
    client,
    admin_user,
    monkeypatch,
    model,
    url_name,
    response_limit_name,
    table,
) -> None:
    canary = f"readonly-method-storage-canary-{model._meta.model_name}"
    execution = RayTaskExecution.objects.create(
        task_id=f"admin-readonly-methods-{model._meta.model_name}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_message=canary * 10_000,
    )
    obj = (
        execution
        if model is RayTaskExecution
        else TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message=canary * 10_000,
        )
    )
    monkeypatch.setattr(django_ray_admin, response_limit_name, 512)
    client.force_login(admin_user)
    url = reverse(url_name, args=[obj.pk])

    head_response = client.head(url)

    assert head_response.status_code == 413
    assert "no-store" in head_response["Cache-Control"]
    assert head_response["X-Content-Type-Options"] == "nosniff"

    for method, expected_status in (
        ("POST", 403),
        ("OPTIONS", 405),
        ("TRACE", 405),
        ("PUT", 405),
        ("PATCH", 405),
        ("DELETE", 405),
    ):
        with CaptureQueriesContext(connection) as queries:
            response = client.generic(method, url)

        assert response.status_code == expected_status
        assert len(response.content) <= 512
        assert canary.encode() not in response.content
        assert "no-store" in response["Cache-Control"]
        assert response["X-Content-Type-Options"] == "nosniff"
        assert _table_selects(queries, table) == []


def test_execution_and_attempt_details_enforce_multibyte_sql_boundary(
    client,
    admin_user,
) -> None:
    limit = ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES
    exact_text = "\U0001f642" * (limit // 4)
    oversized = "\u00e9" * ((limit // 2) + 1)
    assert len(exact_text.encode()) == limit
    assert len(oversized.encode()) == limit + 2
    execution = RayTaskExecution.objects.create(
        task_id="admin-detail-byte-boundary",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_message=exact_text,
        error_traceback=oversized,
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        error_message=exact_text,
        error_traceback=oversized,
    )
    client.force_login(admin_user)

    with CaptureQueriesContext(connection) as execution_queries:
        execution_response = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
        )
    with CaptureQueriesContext(connection) as attempt_queries:
        attempt_response = client.get(
            reverse("admin:django_ray_taskattempt_change", args=[attempt.pk])
        )

    execution_content = execution_response.content.decode()
    attempt_content = attempt_response.content.decode()
    assert exact_text in execution_content
    assert exact_text in attempt_content
    for response, content, maximum in (
        (
            execution_response,
            execution_content,
            ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
        ),
        (attempt_response, attempt_content, ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES),
    ):
        assert response.status_code == 200
        assert oversized not in content
        assert django_ray_admin._ADMIN_DETAIL_OVERSIZED_MESSAGE in content
        assert len(response.content) <= maximum
        assert "no-store" in response["Cache-Control"]
        assert response["X-Content-Type-Options"] == "nosniff"

    execution_select = next(
        query
        for query in _table_selects(execution_queries, "django_ray_raytaskexecution")
        if "admin_detail_error_message_value" in query
    )
    attempt_select = next(
        query
        for query in _table_selects(attempt_queries, "django_ray_taskattempt")
        if "admin_detail_error_message_value" in query
    )
    for query in (execution_select, attempt_select):
        normalized = query.upper()
        assert "CASE WHEN" in normalized
        assert "LENGTH(CAST(" in normalized
        assert " AS BLOB" in normalized


def test_malformed_json_fails_closed_but_authorized_sensitive_data_stays_raw(
    client,
    admin_user,
) -> None:
    execution_canary = "escaped-execution-protected-value"
    attempt_canary = "escaped-attempt-protected-value"
    execution_value = '{"\\u0070assword":"' + execution_canary + '",'
    attempt_value = '{"pass\\u0077ord":"' + attempt_canary + '",'
    execution = RayTaskExecution.objects.create(
        task_id="admin-detail-malformed-json",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        result_data=execution_value,
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        result_data=attempt_value,
    )
    client.force_login(admin_user)

    ordinary_execution = client.get(
        reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
    )
    ordinary_attempt = client.get(reverse("admin:django_ray_taskattempt_change", args=[attempt.pk]))
    sensitive_execution = client.get(
        reverse(
            "admin:django_ray_raytaskexecution_sensitive_data",
            args=[execution.pk],
        )
    )
    sensitive_attempt = client.get(
        reverse("admin:django_ray_taskattempt_sensitive_data", args=[attempt.pk])
    )

    for response, canary in (
        (ordinary_execution, execution_canary),
        (ordinary_attempt, attempt_canary),
    ):
        content = response.content.decode("utf-8")
        assert response.status_code == 200
        assert django_ray_admin._ADMIN_DETAIL_INVALID_JSON_MESSAGE in content
        assert canary not in content
        assert "no-store" in response.headers["Cache-Control"]

    assert sensitive_execution.status_code == 200
    assert execution_canary in sensitive_execution.content.decode("utf-8")
    assert "\\u0070assword" in sensitive_execution.content.decode("utf-8")
    assert sensitive_attempt.status_code == 200
    assert attempt_canary in sensitive_attempt.content.decode("utf-8")
    assert "pass\\u0077ord" in sensitive_attempt.content.decode("utf-8")


def test_execution_detail_projection_never_lazy_loads_huge_diagnostics(admin_user) -> None:
    sentinel = "huge-ordinary-detail-sentinel-" * 40_000
    execution = RayTaskExecution.objects.create(
        task_id="admin-detail-no-lazy-reload",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
    )
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        args_json=sentinel,
        kwargs_json=sentinel,
        input_reference=sentinel,
        result_data=sentinel,
        result_reference=sentinel,
        progress_data=sentinel,
        runtime_env_json=sentinel,
        workflow_progress_summary_json=sentinel,
        workflow_plan_json=sentinel,
        workflow_plan_selection=sentinel,
        completion_data=sentinel,
        cancellation_error=sentinel,
        error_message=sentinel,
        error_traceback=sentinel,
    )
    url = reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
    request = _admin_request(url, admin_user)
    admin_obj = RayTaskExecutionAdmin(RayTaskExecution, admin.site)

    with CaptureQueriesContext(connection) as queries:
        loaded = admin_obj.get_queryset(request).get(pk=execution.pk)
        rendered = (
            admin_obj.args_json_display(loaded),
            admin_obj.kwargs_json_display(loaded),
            admin_obj.input_reference_display(loaded),
            admin_obj.result_data_display(loaded),
            admin_obj.result_reference_display(loaded),
            admin_obj.completion_data_display(loaded),
            admin_obj.cancellation_error_display(loaded),
            admin_obj.error_message_display(loaded),
            admin_obj.error_traceback_display(loaded),
        )

    task_selects = _table_selects(queries, "django_ray_raytaskexecution")
    assert len(task_selects) == 1
    assert task_selects[0].count("CASE WHEN") >= len(
        django_ray_admin._ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS
    )
    assert set(django_ray_admin._ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS).issubset(
        loaded.get_deferred_fields()
    )
    assert {
        "progress_data",
        "runtime_env_json",
        "workflow_progress_summary_json",
        "workflow_plan_json",
        "workflow_plan_selection",
    }.issubset(loaded.get_deferred_fields())
    assert all(django_ray_admin._ADMIN_DETAIL_OVERSIZED_MESSAGE in value for value in rendered)
    assert all(sentinel not in value for value in rendered)


def test_attempt_detail_projection_never_lazy_loads_huge_diagnostics(admin_user) -> None:
    sentinel = "huge-attempt-detail-sentinel-" * 40_000
    execution = RayTaskExecution.objects.create(
        task_id="admin-attempt-detail-no-lazy-reload",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
    )
    attempt = TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
    )
    TaskAttempt.objects.filter(pk=attempt.pk).update(
        result_data=sentinel,
        result_reference=sentinel,
        error_message=sentinel,
        error_traceback=sentinel,
        workflow_progress_summary_json=sentinel,
    )
    url = reverse("admin:django_ray_taskattempt_change", args=[attempt.pk])
    request = _admin_request(url, admin_user)
    admin_obj = TaskAttemptAdmin(TaskAttempt, admin.site)

    with CaptureQueriesContext(connection) as queries:
        loaded = admin_obj.get_queryset(request).get(pk=attempt.pk)
        rendered = (
            admin_obj.result_data_display(loaded),
            admin_obj.result_reference_display(loaded),
            admin_obj.error_message_display(loaded),
            admin_obj.error_traceback_display(loaded),
        )

    attempt_selects = _table_selects(queries, "django_ray_taskattempt")
    assert len(attempt_selects) == 1
    assert attempt_selects[0].count("CASE WHEN") >= len(
        django_ray_admin._ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS
    )
    assert set(django_ray_admin._ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS).issubset(
        loaded.get_deferred_fields()
    )
    assert "workflow_progress_summary_json" in loaded.get_deferred_fields()
    assert all(django_ray_admin._ADMIN_DETAIL_OVERSIZED_MESSAGE in value for value in rendered)
    assert all(sentinel not in value for value in rendered)


def test_attempt_inline_enforces_multibyte_sql_boundary(admin_user) -> None:
    limit = ADMIN_ATTEMPT_INLINE_MAX_BYTES
    exact = "\U0001f642" * (limit // 4)
    oversized = "\u00e9" * ((limit // 2) + 1)
    execution = RayTaskExecution.objects.create(
        task_id="admin-inline-byte-boundary",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        attempt_number=2,
    )
    TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        error_message=exact,
    )
    TaskAttempt.objects.create(
        execution=execution,
        attempt_number=2,
        state=TaskState.FAILED,
        error_message=oversized,
    )
    request = RequestFactory().get("/admin/")
    request.user = admin_user
    inline = TaskAttemptInline(TaskAttempt, admin.site)

    with CaptureQueriesContext(connection) as queries:
        attempts = list(inline.get_queryset(request).filter(execution=execution))
        rendered = [inline.error_summary(attempt) for attempt in attempts]

    assert [attempt.attempt_number for attempt in attempts] == [2, 1]
    assert rendered[0] == "Open the attempt detail to view bounded diagnostics."
    assert rendered[1] == exact
    assert all("error_message" in attempt.get_deferred_fields() for attempt in attempts)
    attempt_selects = _table_selects(queries, "django_ray_taskattempt")
    assert len(attempt_selects) == 1
    assert "CASE WHEN" in attempt_selects[0].upper()
    assert "LENGTH(CAST(" in attempt_selects[0].upper()


def test_guarded_annotation_validation_never_falls_back_to_raw_text() -> None:
    execution = RayTaskExecution(
        task_id="admin-invalid-guard",
        callable_path="testproject.tasks.add_numbers",
        error_message="invalid-guard-private-sentinel",
    )
    bytes_name = django_ray_admin._admin_bounded_annotation_name(
        "detail",
        "error_message",
        "bytes",
    )
    chars_name = django_ray_admin._admin_bounded_annotation_name(
        "detail",
        "error_message",
        "chars",
    )
    value_name = django_ray_admin._admin_bounded_annotation_name(
        "detail",
        "error_message",
        "value",
    )
    execution.__dict__[bytes_name] = ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES + 1
    execution.__dict__[chars_name] = 1
    execution.__dict__[value_name] = execution.error_message

    rendered = RayTaskExecutionAdmin(RayTaskExecution, admin.site).error_message_display(execution)

    assert rendered == django_ray_admin._ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    assert "invalid-guard-private-sentinel" not in rendered

    execution.__dict__.pop(chars_name)
    rendered_missing_alias = RayTaskExecutionAdmin(
        RayTaskExecution,
        admin.site,
    ).error_message_display(execution)
    assert rendered_missing_alias == django_ray_admin._ADMIN_DETAIL_UNAVAILABLE_MESSAGE


def test_guarded_annotation_validation_rejects_corrupt_metadata_shapes() -> None:
    bytes_name = django_ray_admin._admin_bounded_annotation_name(
        "detail",
        "error_message",
        "bytes",
    )
    chars_name = django_ray_admin._admin_bounded_annotation_name(
        "detail",
        "error_message",
        "chars",
    )
    value_name = django_ray_admin._admin_bounded_annotation_name(
        "detail",
        "error_message",
        "value",
    )

    assert django_ray_admin._bounded_admin_text_value(
        SimpleNamespace(),
        "error_message",
    ) == (None, "unavailable")

    corrupt_rows = (
        {
            bytes_name: True,
            chars_name: 1,
            value_name: "x",
        },
        {
            bytes_name: 1,
            chars_name: 1,
            value_name: object(),
        },
        {
            bytes_name: 1,
            chars_name: 1,
            value_name: "\ud800",
        },
        {
            bytes_name: 2,
            chars_name: 1,
            value_name: "x",
        },
    )
    for stored in corrupt_rows:
        guarded = SimpleNamespace()
        guarded.__dict__.update(stored)

        assert django_ray_admin._bounded_admin_text_value(
            guarded,
            "error_message",
        ) == (None, "unavailable")

    assert (
        django_ray_admin._ordinary_admin_json_display(
            SimpleNamespace(),
            "result_data",
        )
        == django_ray_admin._ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    )


def test_unavailable_attempt_displays_never_read_unvalidated_fields() -> None:
    attempt = TaskAttempt(
        attempt_number=7,
        error_message="inline-unavailable-private-sentinel",
        error_traceback="traceback-unavailable-private-sentinel",
    )
    inline_bytes_name = django_ray_admin._admin_bounded_annotation_name(
        "inline",
        "error_message",
        "bytes",
    )
    attempt.__dict__[inline_bytes_name] = 1
    attempt.__dict__.pop("error_traceback")

    inline = TaskAttemptInline(TaskAttempt, admin.site)
    attempt_admin = TaskAttemptAdmin(TaskAttempt, admin.site)

    assert inline.error_summary(attempt) == django_ray_admin._ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    traceback_display = attempt_admin.error_traceback_display(attempt)
    assert django_ray_admin._ADMIN_DETAIL_UNAVAILABLE_MESSAGE in traceback_display
    assert "traceback-unavailable-private-sentinel" not in traceback_display
    assert inline.attempt_detail_link(attempt) == "#7"


def test_admin_octet_length_uses_oracle_byte_semantics(monkeypatch) -> None:
    expression = django_ray_admin._AdminOctetLength()
    observed: dict[str, object] = {}

    def compile_expression(compiler, database_connection, **extra_context):
        observed.update(
            compiler=compiler,
            connection=database_connection,
            **extra_context,
        )
        return "oracle-byte-length", ()

    monkeypatch.setattr(expression, "as_sql", compile_expression)
    compiler = object()
    database_connection = object()

    assert expression.as_oracle(compiler, database_connection) == (
        "oracle-byte-length",
        (),
    )
    assert observed == {
        "compiler": compiler,
        "connection": database_connection,
        "function": "LENGTHB",
    }


def test_attempt_inline_is_newest_first_and_reports_omitted_history(
    client,
    admin_user,
) -> None:
    total = ADMIN_ATTEMPT_INLINE_MAX_ROWS + 3
    execution = RayTaskExecution.objects.create(
        task_id="admin-bounded-attempt-inline",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.SUCCEEDED,
        attempt_number=total,
    )
    attempts = [
        TaskAttempt.objects.create(
            execution=execution,
            attempt_number=number,
            state=TaskState.SUCCEEDED,
            error_message=f"attempt-error-marker-{number:03d}",
        )
        for number in range(1, total + 1)
    ]
    client.force_login(admin_user)

    with CaptureQueriesContext(connection) as queries:
        response = client.get(
            reverse("admin:django_ray_raytaskexecution_change", args=[execution.pk])
        )

    content = response.content.decode()
    compact_content = " ".join(content.split())
    assert response.status_code == 200
    assert (
        f"Showing the newest {ADMIN_ATTEMPT_INLINE_MAX_ROWS} of {total} attempts" in compact_content
    )
    assert "the 3 older attempts" in compact_content
    assert "newest first" not in content
    oldest_visible = attempts[3]
    newest = attempts[-1]
    assert reverse("admin:django_ray_taskattempt_change", args=[attempts[0].pk]) not in content
    assert reverse("admin:django_ray_taskattempt_change", args=[attempts[2].pk]) not in content
    assert reverse("admin:django_ray_taskattempt_change", args=[oldest_visible.pk]) in content
    assert reverse("admin:django_ray_taskattempt_change", args=[newest.pk]) in content
    assert content.index(f"attempt-error-marker-{total:03d}") < content.index(
        f"attempt-error-marker-{oldest_visible.attempt_number:03d}"
    )
    expected_list = reverse("admin:django_ray_taskattempt_changelist")
    assert expected_list in content
    assert f"execution__id__exact={execution.pk}" in content
    attempt_selects = _table_selects(queries, "django_ray_taskattempt")
    inline_select = next(
        (query for query in attempt_selects if "admin_inline_error_message_value" in query),
        None,
    )
    assert inline_select is not None, attempt_selects
    inline_sql = inline_select.upper()
    assert f'"ADMIN_INLINE_RANK" <= {ADMIN_ATTEMPT_INLINE_MAX_ROWS}' in inline_sql
    assert "COUNT(" in inline_sql
    assert "OVER (PARTITION BY" in inline_sql
    assert "CASE WHEN" in inline_sql
    attempt_list = client.get(expected_list, {"execution__id__exact": execution.pk})
    attempt_list_content = attempt_list.content.decode()
    assert attempt_list.status_code == 200
    assert reverse("admin:django_ray_taskattempt_change", args=[attempts[0].pk]) in (
        attempt_list_content
    )
    assert reverse("admin:django_ray_taskattempt_change", args=[attempts[-1].pk]) in (
        attempt_list_content
    )
    assert len(response.content) <= ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES


def test_observability_poll_omits_huge_error_before_materialization(
    client,
    admin_user,
) -> None:
    sentinel = "huge-observability-error-sentinel-" * 40_000
    execution = RayTaskExecution.objects.create(
        task_id="admin-observability-no-full-error",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_message=sentinel,
    )
    client.force_login(admin_user)

    with CaptureQueriesContext(connection) as queries:
        response = client.get(
            reverse("admin:django_ray_raytaskexecution_observability", args=[execution.pk])
        )

    payload = json.loads(response.content)
    assert response.status_code == 200
    assert payload["error_message"] == django_ray_admin._ADMIN_DETAIL_OVERSIZED_MESSAGE
    assert payload["error_message_truncated"] is True
    assert sentinel not in response.content.decode()
    assert len(response.content) <= ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES
    assert "no-store" in response["Cache-Control"]
    assert response["X-Content-Type-Options"] == "nosniff"
    bounded_selects = [
        query
        for query in _table_selects(queries, "django_ray_raytaskexecution")
        if "admin_observability_error_message_value" in query
    ]
    assert len(bounded_selects) == 1
    assert "CASE WHEN" in bounded_selects[0].upper()
    raw_error_selects = [
        query
        for query in _table_selects(queries, "django_ray_raytaskexecution")
        if '"django_ray_raytaskexecution"."error_message"' in query
        and "admin_observability_error_message_value" not in query
    ]
    assert raw_error_selects == []


@pytest.mark.parametrize(
    ("model", "url_name", "response_limit_name"),
    (
        (
            RayTaskExecution,
            "admin:django_ray_raytaskexecution_change",
            "ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES",
        ),
        (
            TaskAttempt,
            "admin:django_ray_taskattempt_change",
            "ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES",
        ),
    ),
)
def test_ordinary_detail_has_a_hard_rendered_response_fallback(
    client,
    admin_user,
    monkeypatch,
    model,
    url_name,
    response_limit_name,
) -> None:
    execution = RayTaskExecution.objects.create(
        task_id=f"admin-response-limit-{model._meta.model_name}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_message="response-limit-private-sentinel",
    )
    obj = (
        execution
        if model is RayTaskExecution
        else TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message="response-limit-private-sentinel",
        )
    )
    monkeypatch.setattr(django_ray_admin, response_limit_name, 512)
    client.force_login(admin_user)

    response = client.get(reverse(url_name, args=[obj.pk]))

    assert response.status_code == 413
    assert len(response.content) <= 512
    assert b"response-limit-private-sentinel" not in response.content
    assert "no-store" in response["Cache-Control"]
    assert response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    ("model", "url_name", "response_limit_name"),
    (
        (
            RayTaskExecution,
            "admin:django_ray_raytaskexecution_change",
            "ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES",
        ),
        (
            TaskAttempt,
            "admin:django_ray_taskattempt_change",
            "ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES",
        ),
    ),
)
@pytest.mark.parametrize("failure_stage", ("initial", "fallback"))
def test_ordinary_detail_renderer_exception_returns_fixed_bounded_503(
    client,
    admin_user,
    monkeypatch,
    model,
    url_name,
    response_limit_name,
    failure_stage,
) -> None:
    storage_canary = f"renderer-storage-canary-{model._meta.model_name}-{failure_stage}"
    exception_canary = f"renderer-exception-canary-{model._meta.model_name}-{failure_stage}"
    execution = RayTaskExecution.objects.create(
        task_id=f"admin-renderer-failure-{model._meta.model_name}-{failure_stage}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.FAILED,
        error_message=storage_canary,
    )
    obj = (
        execution
        if model is RayTaskExecution
        else TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.FAILED,
            error_message=storage_canary,
        )
    )
    response_limit = 512
    monkeypatch.setattr(django_ray_admin, response_limit_name, response_limit)
    original_render = TemplateResponse.render
    rendered_stages: list[str] = []

    def render_with_failure(response):
        template_names = response.template_name
        if isinstance(template_names, str):
            template_names = (template_names,)
        stage = (
            "fallback"
            if "admin/django_ray/bounded_task_detail_limit.html" in template_names
            else "initial"
        )
        rendered_stages.append(stage)
        if stage == failure_stage:
            raise RuntimeError(exception_canary)
        return original_render(response)

    monkeypatch.setattr(TemplateResponse, "render", render_with_failure)
    client.force_login(admin_user)

    response = client.get(reverse(url_name, args=[obj.pk]))

    assert response.status_code == 503
    assert response.content == django_ray_admin._ADMIN_DETAIL_RENDER_FAILURE_BODY
    assert len(response.content) <= response_limit
    assert storage_canary.encode() not in response.content
    assert exception_canary.encode() not in response.content
    assert "no-store" in response["Cache-Control"]
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Type"] == "text/plain; charset=utf-8"
    assert rendered_stages == (
        ["initial"] if failure_stage == "initial" else ["initial", "fallback"]
    )


@pytest.mark.parametrize(
    ("model", "url_name"),
    (
        (RayTaskExecution, "admin:django_ray_raytaskexecution_change"),
        (TaskAttempt, "admin:django_ray_taskattempt_change"),
    ),
)
def test_ordinary_detail_preserves_template_response_middleware(
    client,
    admin_user,
    settings,
    model,
    url_name,
) -> None:
    settings.MIDDLEWARE = [
        *_ADMIN_MIDDLEWARE,
        f"{__name__}.BoundedDetailTemplateResponseMiddleware",
    ]
    execution = RayTaskExecution.objects.create(
        task_id=f"admin-template-middleware-{model._meta.model_name}",
        callable_path="testproject.tasks.add_numbers",
        state=TaskState.SUCCEEDED,
    )
    obj = (
        execution
        if model is RayTaskExecution
        else TaskAttempt.objects.create(
            execution=execution,
            attempt_number=1,
            state=TaskState.SUCCEEDED,
        )
    )
    client.force_login(admin_user)

    response = client.get(reverse(url_name, args=[obj.pk]))

    assert response.status_code == 200
    assert b"template-response-middleware-canary" in response.content
    assert "no-store" in response["Cache-Control"]
    assert response["X-Content-Type-Options"] == "nosniff"


def test_ordinary_detail_preserves_post_render_replacement(admin_user) -> None:
    request = RequestFactory().get("/admin/django_ray/raytaskexecution/1/change/")
    request.user = admin_user
    response = TemplateResponse(request, "admin/base_site.html", {"title": "initial"})
    replacement = HttpResponse(
        "post-render-replacement-canary",
        content_type="text/plain; charset=utf-8",
    )
    response.add_post_render_callback(lambda _rendered: replacement)

    bounded = django_ray_admin._bounded_admin_detail_response(
        request,
        response,
        admin_site=admin.site,
        opts=RayTaskExecution._meta,
        max_bytes=512,
    )
    rendered = bounded.render()

    assert rendered is replacement
    assert rendered.content == b"post-render-replacement-canary"
    assert "no-store" in rendered["Cache-Control"]
    assert rendered["X-Content-Type-Options"] == "nosniff"


def test_ordinary_detail_bound_secures_non_success_and_rejects_success_streaming(
    admin_user,
) -> None:
    request = RequestFactory().get("/admin/django_ray/raytaskexecution/1/change/")
    request.user = admin_user
    non_success = HttpResponse("not found", status=404)

    bounded_non_success = django_ray_admin._bounded_admin_detail_response(
        request,
        non_success,
        admin_site=admin.site,
        opts=RayTaskExecution._meta,
        max_bytes=512,
    )
    bounded_streaming = django_ray_admin._bounded_admin_detail_response(
        request,
        StreamingHttpResponse(iter((b"streaming-private-sentinel",))),
        admin_site=admin.site,
        opts=RayTaskExecution._meta,
        max_bytes=512,
    )

    assert bounded_non_success is non_success
    assert bounded_non_success.status_code == 404
    assert "no-store" in bounded_non_success["Cache-Control"]
    assert bounded_non_success["X-Content-Type-Options"] == "nosniff"
    assert bounded_streaming.status_code == 503
    assert bounded_streaming.content == django_ray_admin._ADMIN_DETAIL_RENDER_FAILURE_BODY
    assert b"streaming-private-sentinel" not in bounded_streaming.content
    assert "no-store" in bounded_streaming["Cache-Control"]
    assert bounded_streaming["X-Content-Type-Options"] == "nosniff"


def test_ordinary_detail_bound_fails_closed_for_unsafe_non_template_responses(
    admin_user,
    monkeypatch,
) -> None:
    request = RequestFactory().get("/admin/django_ray/raytaskexecution/1/change/")
    request.user = admin_user

    class StreamingFallbackTemplateResponse:
        def __init__(self, *_args, **_kwargs):
            pass

        def render(self):
            return StreamingHttpResponse(iter((b"fallback-streaming-private-sentinel",)))

    monkeypatch.setattr(
        django_ray_admin,
        "TemplateResponse",
        StreamingFallbackTemplateResponse,
    )
    fallback_streaming = django_ray_admin._bounded_admin_detail_response(
        request,
        HttpResponse(b"x" * 513),
        admin_site=admin.site,
        opts=RayTaskExecution._meta,
        max_bytes=512,
    )

    class UnreadableHttpResponse(HttpResponse):
        @property
        def content(self):
            raise RuntimeError("unreadable-content-private-sentinel")

        @content.setter
        def content(self, value):
            self._container = [self.make_bytes(value)]

    unreadable = django_ray_admin._bounded_admin_detail_response(
        request,
        UnreadableHttpResponse("private-response-content"),
        admin_site=admin.site,
        opts=RayTaskExecution._meta,
        max_bytes=512,
    )

    for response in (fallback_streaming, unreadable):
        assert response.status_code == 503
        assert response.content == django_ray_admin._ADMIN_DETAIL_RENDER_FAILURE_BODY
        assert len(response.content) <= 512
        assert "no-store" in response["Cache-Control"]
        assert response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("failure_stage", ("initial", "fallback"))
@pytest.mark.parametrize("signal_type", (SystemExit, KeyboardInterrupt))
def test_ordinary_detail_renderer_preserves_process_control_signals(
    admin_user,
    monkeypatch,
    failure_stage,
    signal_type,
) -> None:
    request = RequestFactory().get("/admin/django_ray/raytaskexecution/1/change/")
    request.user = admin_user

    if failure_stage == "initial":
        response = TemplateResponse(request, "admin/base_site.html", {})

        def interrupt_initial_render():
            raise signal_type

        monkeypatch.setattr(response, "render", interrupt_initial_render)
    else:
        response = HttpResponse(b"x" * 513)

        class InterruptingFallbackTemplateResponse(TemplateResponse):
            def render(self):
                raise signal_type

        monkeypatch.setattr(
            django_ray_admin,
            "TemplateResponse",
            InterruptingFallbackTemplateResponse,
        )

    with pytest.raises(signal_type):
        bounded = django_ray_admin._bounded_admin_detail_response(
            request,
            response,
            admin_site=admin.site,
            opts=RayTaskExecution._meta,
            max_bytes=512,
        )
        if isinstance(bounded, TemplateResponse):
            bounded.render()
