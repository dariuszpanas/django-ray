"""Sensitive task diagnostics views, parent-execution scoping, and redaction helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from urllib.parse import quote, unquote

from django.contrib import admin
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db.models import Case, F, QuerySet, TextField, Value, When
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, reverse

from django_ray.admin_views.diagnostics import AdminOctetLength, secure_admin_response
from django_ray.models import RayTaskExecution
from django_ray.redaction import normalize_terminal_text

ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES = 64 * 1024
ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES = 4 * 1024 * 1024
ADMIN_SENSITIVE_PERMISSION = "django_ray.view_sensitive_task_data"
ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES = 64 * 1024
ADMIN_SENSITIVE_HTML_EXPANSION_FACTOR = 6
ADMIN_SENSITIVE_EXECUTION_SECTIONS = (
    (
        "Task input",
        (
            ("Arguments", "args_json"),
            ("Keyword arguments", "kwargs_json"),
            ("Input reference", "input_reference"),
        ),
    ),
    (
        "Task outcome",
        (
            ("Result", "result_data"),
            ("Result reference", "result_reference"),
            ("Cancellation error", "cancellation_error"),
            ("Error", "error_message"),
            ("Traceback", "error_traceback"),
        ),
    ),
)
ADMIN_SENSITIVE_ATTEMPT_OUTCOME_SECTION = (
    "Attempt outcome",
    (
        ("Result", "result_data"),
        ("Result reference", "result_reference"),
        ("Error", "error_message"),
        ("Traceback", "error_traceback"),
    ),
)

SectionSpecs = tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
SectionsBuilder = Callable[[dict[str, Any], SectionSpecs], list[dict[str, Any]]]


def sensitive_annotation_name(field_name: str, suffix: str) -> str:
    """Build the SQL annotation alias for a guarded sensitive text field."""
    return f"admin_sensitive_{field_name}_{suffix}"


def bounded_sensitive_object(
    queryset: QuerySet,
    *,
    pk: Any,
    field_names: tuple[str, ...],
    identity_fields: tuple[str, ...],
    field_max_bytes: int = ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES,
) -> tuple[Any, dict[str, Any]]:
    """Fetch allowlisted text only when the database proves it is within bounds."""
    lengths = {
        sensitive_annotation_name(field_name, "bytes"): AdminOctetLength(F(field_name))
        for field_name in field_names
    }
    queryset = queryset.annotate(**lengths)
    values = {
        sensitive_annotation_name(field_name, "value"): Case(
            When(
                **{f"{sensitive_annotation_name(field_name, 'bytes')}__lte": field_max_bytes},
                then=F(field_name),
            ),
            default=Value(None),
            output_field=TextField(),
        )
        for field_name in field_names
    }
    fresh = queryset.annotate(**values).only(*identity_fields).get(pk=pk)
    row: dict[str, Any] = {}
    for field_name in field_names:
        bytes_name = sensitive_annotation_name(field_name, "bytes")
        value_name = sensitive_annotation_name(field_name, "value")
        byte_length = getattr(fresh, bytes_name)
        value = getattr(fresh, value_name)
        row[bytes_name] = byte_length
        row[value_name] = value
        if byte_length is None:
            if value is not None:
                raise ValueError("Sensitive diagnostic storage failed validation")
            continue
        if type(byte_length) is not int or byte_length < 0:
            raise ValueError("Sensitive diagnostic storage failed validation")
        if byte_length > field_max_bytes:
            if value is not None:
                raise ValueError("Sensitive diagnostic storage failed validation")
            continue
        if not isinstance(value, str):
            raise ValueError("Sensitive diagnostic storage failed validation")
        try:
            decoded_bytes = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("Sensitive diagnostic storage failed validation") from error
        if decoded_bytes != byte_length:
            raise ValueError("Sensitive diagnostic storage failed validation")
    return fresh, row


def build_sensitive_sections(
    row: dict[str, Any],
    section_specs: SectionSpecs,
    *,
    field_max_bytes: int = ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES,
    response_max_bytes: int = ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
    response_overhead_bytes: int = ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES,
) -> list[dict[str, Any]]:
    """Build an autoescaped presentation without exceeding the response budget."""
    raw_budget = max(
        0,
        (response_max_bytes - response_overhead_bytes) // ADMIN_SENSITIVE_HTML_EXPANSION_FACTOR,
    )
    sections: list[dict[str, Any]] = []
    for heading, field_specs in section_specs:
        fields: list[dict[str, Any]] = []
        for label, field_name in field_specs:
            byte_length = row[sensitive_annotation_name(field_name, "bytes")]
            value = row[sensitive_annotation_name(field_name, "value")]
            if byte_length is None:
                status = "null"
            elif byte_length > field_max_bytes:
                status = "oversized"
            elif byte_length == 0:
                status = "empty"
            elif byte_length > raw_budget:
                status = "response_limit"
                value = None
            else:
                raw_budget -= byte_length
                value = normalize_terminal_text(value)
                status = "value" if value else "normalized_empty"
            fields.append(
                {
                    "name": field_name,
                    "label": label,
                    "status": status,
                    "value": value,
                    "byte_length": byte_length,
                }
            )
        sections.append({"heading": heading, "fields": fields})
    return sections


def has_sensitive_task_data_permission(
    request: HttpRequest,
    execution: Any = None,
) -> bool:
    """Honor global and object-specific grants for the owning execution."""
    user = cast(Any, request).user
    if user.has_perm(ADMIN_SENSITIVE_PERMISSION):
        return True
    if execution is None:
        return False
    return bool(user.has_perm(ADMIN_SENSITIVE_PERMISSION, execution))


def safe_admin_reverse(
    admin_site: admin.AdminSite,
    viewname: str,
    *,
    kwargs: dict[str, Any] | None = None,
) -> str | None:
    """Resolve an admin URL name without failing when a custom AdminSite omits it."""
    try:
        return reverse(
            f"{admin_site.name}:{viewname}",
            kwargs=kwargs,
            current_app=admin_site.name,
        )
    except NoReverseMatch:
        return None


def sensitive_diagnostics_response(
    request: HttpRequest,
    *,
    admin_site: admin.AdminSite,
    opts: Any,
    title: str,
    subject: str,
    back_url: str,
    sections: list[dict[str, Any]],
    field_max_bytes: int = ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES,
    response_max_bytes: int = ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
) -> HttpResponse:
    """Render an autoescaped, byte-bounded unredacted diagnostics response."""
    context = {
        **admin_site.each_context(request),
        "title": title,
        "opts": opts,
        "django_ray_sensitive_subject": subject,
        "django_ray_sensitive_back_url": back_url,
        "django_ray_sensitive_index_url": safe_admin_reverse(admin_site, "index"),
        "django_ray_sensitive_app_url": safe_admin_reverse(
            admin_site,
            "app_list",
            kwargs={"app_label": opts.app_label},
        ),
        "django_ray_sensitive_changelist_url": safe_admin_reverse(
            admin_site,
            f"{opts.app_label}_{opts.model_name}_changelist",
        ),
        "django_ray_sensitive_sections": sections,
        "django_ray_sensitive_field_limit_bytes": field_max_bytes,
    }
    response: HttpResponse = TemplateResponse(
        request,
        "admin/django_ray/sensitive_task_data.html",
        context,
    )
    cast(Any, response).render()
    if len(response.content) > response_max_bytes:
        response = TemplateResponse(
            request,
            "admin/django_ray/sensitive_task_data_limit.html",
            {
                "title": "Sensitive diagnostics response limit reached",
                "django_ray_sensitive_subject": subject,
                "django_ray_sensitive_back_url": back_url,
                "django_ray_sensitive_response_limit_bytes": response_max_bytes,
            },
            status=413,
        )
        cast(Any, response).render()
    if len(response.content) > response_max_bytes:
        response = HttpResponse(
            "Sensitive diagnostics response exceeds its fixed safety limit.",
            status=413,
            content_type="text/plain; charset=utf-8",
        )
    return secure_admin_response(response)


def parent_execution_admin(admin_site: admin.AdminSite) -> admin.ModelAdmin | None:
    """Resolve the registered RayTaskExecution admin for parent-row scoping."""
    parent_admin = admin_site._registry.get(RayTaskExecution)
    if isinstance(parent_admin, admin.ModelAdmin):
        return parent_admin
    return None


def resolve_attempt_sensitive_data_url(
    model_admin: Any,
    request: HttpRequest,
    obj: Any,
) -> str | None:
    """Return the sensitive-data URL for an attempt only when parent scoping allows it."""
    if obj is None or not model_admin.has_view_permission(request, obj):
        return None
    parent_admin = model_admin._parent_execution_admin()
    if parent_admin is None or not parent_admin.has_view_permission(request):
        return None
    database = str(getattr(obj._state, "db", None) or "default")
    try:
        execution = (
            parent_admin.get_queryset(request).using(database).only("pk").get(pk=obj.execution_id)
        )
    except ObjectDoesNotExist:
        execution = None
    if (
        execution is not None
        and parent_admin.has_view_permission(request, execution)
        and has_sensitive_task_data_permission(request, execution)
    ):
        opts = model_admin.model._meta
        admin_site = model_admin.admin_site
        return reverse(
            f"{admin_site.name}:{opts.app_label}_{opts.model_name}_sensitive_data",
            args=[quote(str(obj.pk))],
            current_app=admin_site.name,
        )
    return None


def execution_sensitive_data_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    sections_builder: SectionsBuilder = build_sensitive_sections,
    response_max_bytes: int = ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
) -> HttpResponse:
    """Render a bounded, permission-gated view of stored execution diagnostics."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    user = getattr(cast(Any, request), "user", None)
    if not getattr(user, "is_authenticated", False):
        raise PermissionDenied
    has_global_access = bool(
        model_admin.has_view_permission(request) and has_sensitive_task_data_permission(request)
    )
    try:
        execution_id = model_admin.model._meta.pk.to_python(unquote(object_id))
    except (ValidationError, ValueError) as error:
        if not has_global_access:
            raise PermissionDenied from error
        raise Http404("Ray task execution was not found") from error
    try:
        execution = (
            model_admin.get_queryset(request)
            .only("pk", "task_id", "callable_path", "state")
            .get(pk=execution_id)
        )
    except (ObjectDoesNotExist, ValidationError, ValueError) as error:
        if not has_global_access:
            raise PermissionDenied from error
        raise Http404("Ray task execution was not found") from error
    if not model_admin.has_view_permission(request, execution):
        raise PermissionDenied
    if not has_sensitive_task_data_permission(request, execution):
        raise PermissionDenied

    field_names = tuple(
        field_name
        for _, field_specs in ADMIN_SENSITIVE_EXECUTION_SECTIONS
        for _, field_name in field_specs
    )
    database = str(getattr(execution._state, "db", None) or "default")
    try:
        fresh, row = bounded_sensitive_object(
            model_admin.get_queryset(request).using(database),
            pk=execution.pk,
            field_names=field_names,
            identity_fields=("pk", "task_id", "callable_path", "state"),
        )
    except ObjectDoesNotExist as error:
        raise Http404("Ray task execution was not found") from error
    if fresh.pk != execution.pk or fresh.task_id != execution.task_id:
        raise Http404("Ray task execution was not found")
    if not model_admin.has_view_permission(request, fresh):
        raise PermissionDenied
    if not has_sensitive_task_data_permission(request, fresh):
        raise PermissionDenied
    execution = fresh
    admin_site = model_admin.admin_site
    opts = model_admin.opts
    back_url = reverse(
        f"{admin_site.name}:{opts.app_label}_{opts.model_name}_change",
        args=[quote(str(execution.pk))],
        current_app=admin_site.name,
    )
    return sensitive_diagnostics_response(
        request,
        admin_site=admin_site,
        opts=opts,
        title="Unredacted task data",
        subject=(f"Execution {execution.task_id} — {execution.callable_path} ({execution.state})"),
        back_url=back_url,
        sections=sections_builder(row, ADMIN_SENSITIVE_EXECUTION_SECTIONS),
        response_max_bytes=response_max_bytes,
    )


def attempt_sensitive_data_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    sections_builder: SectionsBuilder = build_sensitive_sections,
    response_max_bytes: int = ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
) -> HttpResponse:
    """Render exact parent inputs plus the selected archived attempt's outcome."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    user = getattr(cast(Any, request), "user", None)
    if not getattr(user, "is_authenticated", False):
        raise PermissionDenied
    parent_admin = model_admin._parent_execution_admin()
    if parent_admin is None:
        raise PermissionDenied
    has_global_access = bool(
        model_admin.has_view_permission(request)
        and parent_admin.has_view_permission(request)
        and has_sensitive_task_data_permission(request)
    )
    try:
        attempt_id = model_admin.model._meta.pk.to_python(unquote(object_id))
    except (ValidationError, ValueError) as error:
        if not has_global_access:
            raise PermissionDenied from error
        raise Http404("Task attempt was not found") from error
    try:
        attempt = (
            model_admin.get_queryset(request)
            .only("pk", "execution_id", "attempt_number", "state")
            .get(pk=attempt_id)
        )
    except (ObjectDoesNotExist, ValidationError, ValueError) as error:
        if not has_global_access:
            raise PermissionDenied from error
        raise Http404("Task attempt was not found") from error
    if not model_admin.has_view_permission(request, attempt):
        raise PermissionDenied
    database = str(getattr(attempt._state, "db", None) or "default")
    try:
        execution = (
            parent_admin.get_queryset(request)
            .using(database)
            .only(
                "pk",
                "task_id",
                "callable_path",
            )
            .get(pk=attempt.execution_id)
        )
    except ObjectDoesNotExist as error:
        if not (
            parent_admin.has_view_permission(request)
            and has_sensitive_task_data_permission(request)
        ):
            raise PermissionDenied from error
        raise Http404("Owning Ray task execution was not found") from error
    if not parent_admin.has_view_permission(request, execution):
        raise PermissionDenied
    if not has_sensitive_task_data_permission(request, execution):
        raise PermissionDenied

    input_specs = (ADMIN_SENSITIVE_EXECUTION_SECTIONS[0],)
    input_fields = tuple(field_name for _, field_name in input_specs[0][1])
    outcome_specs = (ADMIN_SENSITIVE_ATTEMPT_OUTCOME_SECTION,)
    outcome_fields = tuple(field_name for _, field_name in outcome_specs[0][1])
    try:
        fresh_execution, input_row = bounded_sensitive_object(
            parent_admin.get_queryset(request).using(database),
            pk=execution.pk,
            field_names=input_fields,
            identity_fields=("pk", "task_id", "callable_path"),
        )
        fresh_attempt, outcome_row = bounded_sensitive_object(
            model_admin.get_queryset(request).using(database),
            pk=attempt.pk,
            field_names=outcome_fields,
            identity_fields=(
                "pk",
                "execution_id",
                "attempt_number",
                "state",
            ),
        )
    except ObjectDoesNotExist as error:
        raise Http404("Task attempt was not found") from error
    if (
        fresh_execution.pk != execution.pk
        or fresh_execution.task_id != execution.task_id
        or fresh_attempt.pk != attempt.pk
        or fresh_attempt.execution_id != fresh_execution.pk
        or fresh_attempt.execution_id != attempt.execution_id
        or fresh_attempt.attempt_number != attempt.attempt_number
    ):
        raise Http404("Task attempt was not found")
    if not parent_admin.has_view_permission(request, fresh_execution):
        raise PermissionDenied
    if not has_sensitive_task_data_permission(request, fresh_execution):
        raise PermissionDenied
    if not model_admin.has_view_permission(request, fresh_attempt):
        raise PermissionDenied
    execution = fresh_execution
    attempt = fresh_attempt
    row = {**input_row, **outcome_row}
    sections = sections_builder(row, input_specs + outcome_specs)
    admin_site = model_admin.admin_site
    opts = model_admin.opts
    back_url = reverse(
        f"{admin_site.name}:{opts.app_label}_{opts.model_name}_change",
        args=[quote(str(attempt.pk))],
        current_app=admin_site.name,
    )
    return sensitive_diagnostics_response(
        request,
        admin_site=admin_site,
        opts=opts,
        title="Unredacted attempt data",
        subject=(
            f"Execution {execution.task_id} — {execution.callable_path}; "
            f"attempt {attempt.attempt_number} ({attempt.state})"
        ),
        back_url=back_url,
        sections=sections,
        response_max_bytes=response_max_bytes,
    )
