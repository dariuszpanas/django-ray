"""Bounded detail formatting, live observability, and workflow diagnostics/graph views."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from types import MethodType
from typing import Any, cast
from urllib.parse import quote, unquote, urlencode

from django.contrib import admin
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db.models import (
    BooleanField,
    Case,
    Count,
    Exists,
    F,
    Func,
    IntegerField,
    Q,
    QuerySet,
    TextField,
    Value,
    When,
    Window,
)
from django.db.models.functions import Length, RowNumber
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from django_ray.admin_views.badges import (
    ADMIN_ATTEMPT_INLINE_MAX_BYTES,
    ADMIN_ATTEMPT_INLINE_MAX_CHARS,
    ADMIN_ATTEMPT_INLINE_MAX_ROWS,
    ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
    ADMIN_DETAIL_OVERSIZED_MESSAGE,
    ADMIN_DETAIL_UNAVAILABLE_MESSAGE,
    ADMIN_DIAGNOSTIC_MAX_CHARS,
    DIGEST_ONLY_STUB_LABEL,
    FULL_WORKFLOW_ATTEMPT_MARKER,
    OVERSIZE_RESULT_REFERENCE_PREFIX,
    bounded_json_html,
    bounded_redacted_text,
    bounded_traceback_display,
)
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState
from django_ray.protocol_status import annotate_execution_protocol_availability
from django_ray.redaction import redact_text
from django_ray.workflow.admin_graph import (
    ADMIN_WORKFLOW_GRAPH_MAX_DETAILS,
    ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
    ADMIN_WORKFLOW_GRAPH_MAX_NODES,
    ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES,
    AdminWorkflowGraphError,
    build_admin_workflow_graph,
    degraded_admin_workflow_graph,
    inspect_admin_workflow_graph_summary,
)
from django_ray.workflow.plans import (
    MAX_PLAN_BYTES,
    effective_plan_selection_reporting_policy,
    validate_plan_selection_manifest,
)
from django_ray.workflow.progress.reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
    read_workflow_progress_collections,
)
from django_ray.workflow.progress.runs import MAX_PLAN_SELECTION_BYTES
from django_ray.workflow.progress.summary import (
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    WORKFLOW_PROGRESS_TERMINAL_STATES,
)

ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES = 512 * 1024
ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES = 64 * 1024
ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES = 16 * 1024
ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES = 128 * 1024
ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES = 32 * 1024
ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS = 100
ADMIN_DETAIL_RENDER_FAILURE_BODY = b"Task detail could not be rendered safely."
ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES = 128
ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS = 128
ADMIN_PROTOCOL_PROVENANCE_FIELDS = (
    "created_with_django_ray_version",
    "managed_with_django_ray_version",
    "executor_django_ray_version",
)
ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS = (
    "args_json",
    "kwargs_json",
    "input_reference",
    "result_data",
    "result_reference",
    "completion_data",
    "cancellation_error",
    "error_message",
    "error_traceback",
)
ADMIN_EXECUTION_DETAIL_FIELDS = (
    "pk",
    "task_id",
    "callable_path",
    "priority",
    "queue_name",
    "state",
    "attempt_number",
    "execution_generation",
    "metadata_schema_version",
    "execution_protocol_version",
    "ray_job_id",
    "ray_target_address",
    "ray_address",
    "claimed_by_worker",
    "runtime_env_profile",
    "runtime_env_hash",
    "workflow_run_id",
    "workflow_plan_fingerprint",
    "workflow_plan_pinned_attempt",
    "created_at",
    "run_after",
    "queue_timeout_seconds",
    "queue_deadline_at",
    "started_at",
    "finished_at",
    "last_heartbeat_at",
    "cancellation_status",
)
ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS = (
    "error_message",
    "error_traceback",
    "result_data",
    "result_reference",
)
ADMIN_ATTEMPT_DETAIL_FIELDS = (
    "pk",
    "execution_id",
    "attempt_number",
    "execution_protocol_version",
    "managed_with_django_ray_version",
    "executor_django_ray_version",
    "state",
    "started_at",
    "finished_at",
    "created_at",
)
OBSERVABILITY_FIELDS = (
    "pk",
    "task_id",
    "callable_path",
    "queue_name",
    "priority",
    "state",
    "attempt_number",
    "execution_generation",
    "metadata_schema_version",
    "execution_protocol_version",
    "workflow_run_id",
    "created_at",
    "run_after",
    "queue_timeout_seconds",
    "queue_deadline_at",
    "started_at",
    "finished_at",
    "last_heartbeat_at",
    "claimed_by_worker",
    "ray_job_id",
    "runtime_env_profile",
    "runtime_env_hash",
    "workflow_plan_fingerprint",
    "workflow_plan_pinned_attempt",
)
WORKFLOW_READ_FIELDS = (
    "pk",
    "task_id",
    "callable_path",
    "attempt_number",
    "execution_generation",
    "workflow_run_id",
)
WORKFLOW_PROGRESS_MESSAGES: dict[str, str] = {
    "NOT_REPORTED": "No workflow progress snapshot has been reported.",
    "REQUESTED_NOT_REPORTED": (
        "Full workflow reporting was requested, but no bounded snapshot has been published yet."
    ),
    "REQUESTED_MISSING": (
        "Full workflow reporting was requested, but the terminal snapshot was not captured."
    ),
    "LEGACY_ONLY": (
        "Only a legacy aggregate snapshot is retained; bounded topology and node detail "
        "are unavailable."
    ),
    "DISABLED": "Workflow progress reporting was disabled by policy.",
    "OMITTED_BY_POLICY": (
        "Detailed workflow progress was omitted by the selected reporting policy."
    ),
    "TERMINAL_ONLY": (
        "A terminal workflow summary is available; topology and node detail were "
        "omitted by the terminal-only reporting policy."
    ),
    "TERMINAL_ONLY_PENDING": (
        "Terminal-only reporting waits for durable success or failure; no live "
        "workflow topology or node detail is collected."
    ),
    "TERMINAL_ONLY_MISSING": (
        "Terminal-only reporting was selected, but its terminal summary was not captured."
    ),
    "AVAILABLE": "Bounded workflow topology and node detail are available.",
    "TRUNCATED": "Bounded workflow detail is available but incomplete.",
    "LIMIT_EXCEEDED": (
        "Reporting limits were exceeded, so no graph details were saved. The task outcome "
        "is unchanged. Use smaller workflows or terminal-only reporting for future runs."
    ),
    "EXPIRED": "Retained workflow topology and node detail have expired.",
    "MISSING": "The workflow summary references retained detail that is missing.",
    "CORRUPT": "Workflow progress failed validation.",
}


@dataclass(frozen=True, slots=True)
class WorkflowReadHooks:
    """Injected workflow-progress read callables for decoupled admin delegation."""

    get_summary: Callable[..., dict[str, Any]] = get_workflow_progress_summary
    list_nodes: Callable[..., dict[str, Any]] = list_workflow_topology_nodes
    list_edges: Callable[..., dict[str, Any]] = list_workflow_topology_edges
    list_details: Callable[..., dict[str, Any]] = list_workflow_node_details
    get_detail: Callable[..., dict[str, Any]] = get_workflow_node_detail
    read_collections: Callable[..., dict[str, Any]] = read_workflow_progress_collections

    def uses_default_collection_readers(self) -> bool:
        """Return whether individual collection readers are still unpatched defaults."""
        return (
            self.get_summary is get_workflow_progress_summary
            and self.list_nodes is list_workflow_topology_nodes
            and self.list_edges is list_workflow_topology_edges
            and self.list_details is list_workflow_node_details
        )


_DEFAULT_WORKFLOW_READ_HOOKS = WorkflowReadHooks()


class AdminOctetLength(Func):
    """Return storage bytes consistently across supported databases."""

    function = "OCTET_LENGTH"
    output_field = IntegerField()

    def as_sqlite(self, compiler: Any, connection: Any, **extra_context: Any) -> Any:
        return self.as_sql(
            compiler,
            connection,
            template="LENGTH(CAST(%(expressions)s AS BLOB))",
            **extra_context,
        )

    def as_oracle(self, compiler: Any, connection: Any, **extra_context: Any) -> Any:
        return self.as_sql(
            compiler,
            connection,
            function="LENGTHB",
            **extra_context,
        )


def admin_bounded_annotation_name(namespace: str, field_name: str, suffix: str) -> str:
    """Build one private annotation name for a guarded text projection."""
    return f"admin_{namespace}_{field_name}_{suffix}"


def annotate_bounded_admin_text(
    queryset: QuerySet,
    *,
    field_names: tuple[str, ...],
    max_bytes: int,
    max_chars: int,
    namespace: str,
) -> QuerySet:
    """Project text only when the database proves its UTF-8 byte size is safe."""
    lengths = {
        admin_bounded_annotation_name(namespace, field_name, "bytes"): AdminOctetLength(
            F(field_name)
        )
        for field_name in field_names
    }
    queryset = queryset.annotate(**lengths)
    characters = {
        admin_bounded_annotation_name(namespace, field_name, "chars"): Length(F(field_name))
        for field_name in field_names
    }
    queryset = queryset.annotate(**characters)
    values = {
        admin_bounded_annotation_name(namespace, field_name, "value"): Case(
            When(
                **{
                    f"{admin_bounded_annotation_name(namespace, field_name, 'bytes')}__lte": (
                        max_bytes
                    ),
                    f"{admin_bounded_annotation_name(namespace, field_name, 'chars')}__lte": (
                        max_chars
                    ),
                },
                then=F(field_name),
            ),
            default=Value(None),
            output_field=TextField(),
        )
        for field_name in field_names
    }
    return queryset.annotate(**values)


def bounded_admin_text_value(
    obj: Any,
    field_name: str,
    *,
    max_bytes: int = ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
    max_chars: int = ADMIN_DIAGNOSTIC_MAX_CHARS,
    namespace: str = "detail",
) -> tuple[str | None, str]:
    """Validate one SQL-guarded value without triggering a deferred-field read."""
    stored = obj.__dict__
    bytes_name = admin_bounded_annotation_name(namespace, field_name, "bytes")
    chars_name = admin_bounded_annotation_name(namespace, field_name, "chars")
    value_name = admin_bounded_annotation_name(namespace, field_name, "value")
    has_bytes = bytes_name in stored
    has_chars = chars_name in stored
    has_value = value_name in stored
    if not has_bytes and not has_chars and not has_value:
        # Display helpers are also public testable utilities for caller-created
        # model instances. A real bounded queryset always supplies both aliases.
        if field_name not in stored:
            return None, "unavailable"
        raw = stored[field_name]
        return (raw, "value") if raw is None or isinstance(raw, str) else (None, "unavailable")
    if not has_bytes or not has_chars or not has_value:
        return None, "unavailable"

    byte_length = stored[bytes_name]
    char_length = stored[chars_name]
    value = stored[value_name]
    if byte_length is None:
        return (None, "value") if char_length is None and value is None else (None, "unavailable")
    if (
        type(byte_length) is not int
        or byte_length < 0
        or type(char_length) is not int
        or char_length < 0
    ):
        return None, "unavailable"
    if byte_length > max_bytes or char_length > max_chars:
        return (None, "oversized") if value is None else (None, "unavailable")
    if not isinstance(value, str):
        return None, "unavailable"
    try:
        encoded_bytes = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None, "unavailable"
    if encoded_bytes != byte_length or len(value) != char_length:
        return None, "unavailable"
    return value, "value"


def ordinary_admin_text_display(
    obj: Any,
    field_name: str,
    *,
    max_chars: int = ADMIN_DIAGNOSTIC_MAX_CHARS,
    max_bytes: int = ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
    namespace: str = "detail",
) -> str:
    """Format a SQL-bounded text field for ordinary redacted Admin display."""
    value, status = bounded_admin_text_value(
        obj,
        field_name,
        max_bytes=max_bytes,
        max_chars=max_chars,
        namespace=namespace,
    )
    if status == "oversized":
        return ADMIN_DETAIL_OVERSIZED_MESSAGE
    if status != "value":
        return ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    return bounded_redacted_text(value, max_chars=max_chars)


def ordinary_admin_json_display(obj: Any, field_name: str) -> str:
    """Format a SQL-bounded JSON field for ordinary redacted Admin display."""
    value, status = bounded_admin_text_value(obj, field_name)
    if status == "oversized":
        return ADMIN_DETAIL_OVERSIZED_MESSAGE
    if status != "value":
        return ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    return bounded_json_html(value)


def ordinary_admin_result_display(obj: Any) -> str:
    """Render bounded result JSON with an explicit badge for digest-only oversize stubs."""
    ref_value, ref_status = bounded_admin_text_value(obj, "result_reference")
    rendered = ordinary_admin_json_display(obj, "result_data")
    if (
        ref_status == "value"
        and isinstance(ref_value, str)
        and ref_value.startswith(OVERSIZE_RESULT_REFERENCE_PREFIX)
    ):
        badge = format_html(
            '<span class="djray-badge djray-badge--warning djray-badge--stub">{}</span>',
            DIGEST_ONLY_STUB_LABEL,
        )
        if rendered == "-":
            return badge
        return format_html('<div class="django-ray-diagnostic__stack">{}{}</div>', badge, rendered)
    return rendered


def ordinary_admin_traceback_display(obj: Any) -> str:
    """Render a SQL-bounded traceback field inside a monospace container."""
    value, status = bounded_admin_text_value(obj, "error_traceback")
    if status == "oversized":
        value = ADMIN_DETAIL_OVERSIZED_MESSAGE
    elif status != "value":
        value = ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    return bounded_traceback_display(value)


def inline_attempt_error_summary(obj: Any) -> str:
    """Format bounded inline attempt error preview."""
    preview, status = bounded_admin_text_value(
        obj,
        "error_message",
        max_bytes=ADMIN_ATTEMPT_INLINE_MAX_BYTES,
        max_chars=ADMIN_ATTEMPT_INLINE_MAX_CHARS,
        namespace="inline",
    )
    if status == "oversized":
        return "Open the attempt detail to view bounded diagnostics."
    if status != "value":
        return ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    return bounded_redacted_text(
        preview,
        max_chars=ADMIN_ATTEMPT_INLINE_MAX_CHARS,
    )


def prepare_inline_attempt_queryset(queryset: QuerySet) -> QuerySet:
    """Return only the newest fixed number of guarded attempt summaries."""
    bounded = (
        queryset.annotate(
            admin_inline_total=Window(
                expression=Count("pk"),
                partition_by=[F("execution_id")],
            ),
            admin_inline_rank=Window(
                expression=RowNumber(),
                partition_by=[F("execution_id")],
                order_by=F("attempt_number").desc(),
            ),
        )
        .filter(admin_inline_rank__lte=ADMIN_ATTEMPT_INLINE_MAX_ROWS)
        .only(
            "pk",
            "execution_id",
            "attempt_number",
            "state",
            "started_at",
            "finished_at",
        )
    )
    return annotate_bounded_admin_text(
        bounded,
        field_names=("error_message",),
        max_bytes=ADMIN_ATTEMPT_INLINE_MAX_BYTES,
        max_chars=ADMIN_ATTEMPT_INLINE_MAX_CHARS,
        namespace="inline",
    ).order_by("-attempt_number")


def prepare_execution_queryset(
    queryset: QuerySet,
    request: HttpRequest,
    *,
    opts: Any,
) -> QuerySet:
    """Use allowlisted and SQL-guarded projections for routine Admin execution reads."""
    queryset = queryset.defer(
        "progress_data",
        "ray_job_request_reference",
        "runtime_env_json",
        "workflow_plan_json",
        "workflow_plan_selection",
        "workflow_progress_summary_json",
    )
    resolver_match = request.resolver_match
    url_name = resolver_match.url_name if resolver_match is not None else None
    if request.__dict__.get("_django_ray_bounded_execution_detail") is True or url_name == (
        f"{opts.app_label}_{opts.model_name}_change"
    ):
        queryset = annotate_execution_protocol_availability(
            queryset.only(*ADMIN_EXECUTION_DETAIL_FIELDS)
        )
        queryset = annotate_bounded_admin_text(
            queryset,
            field_names=ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS,
            max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
            max_chars=ADMIN_DIAGNOSTIC_MAX_CHARS,
            namespace="detail",
        )
        return annotate_bounded_admin_text(
            queryset,
            field_names=ADMIN_PROTOCOL_PROVENANCE_FIELDS,
            max_bytes=ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
            max_chars=ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
            namespace="protocol",
        )
    if url_name == f"{opts.app_label}_{opts.model_name}_changelist":
        queryset = queryset.defer(
            "args_json",
            "kwargs_json",
            "input_reference",
            "result_data",
            "result_reference",
            "completion_data",
            "cancellation_error",
            "error_message",
            "error_traceback",
        )
    return queryset


def prepare_attempt_queryset(queryset: QuerySet, request: HttpRequest) -> QuerySet:
    """Use allowlisted and SQL-guarded projections for routine Admin attempt reads."""
    queryset = queryset.defer("workflow_progress_summary_json")
    resolver_match = request.resolver_match
    url_name = resolver_match.url_name if resolver_match is not None else None
    if url_name == "django_ray_taskattempt_changelist":
        queryset = queryset.only(
            "pk",
            "execution_id",
            "attempt_number",
            "execution_protocol_version",
            "state",
            "started_at",
            "finished_at",
        )
    else:
        queryset = queryset.annotate(
            admin_has_workflow_graph=Case(
                When(
                    workflow_progress_summary_json__contains=FULL_WORKFLOW_ATTEMPT_MARKER,
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
    if (
        request.__dict__.get("_django_ray_bounded_attempt_detail") is True
        or url_name == "django_ray_taskattempt_change"
    ):
        queryset = annotate_bounded_admin_text(
            queryset.only(*ADMIN_ATTEMPT_DETAIL_FIELDS),
            field_names=ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS,
            max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
            max_chars=ADMIN_DIAGNOSTIC_MAX_CHARS,
            namespace="detail",
        )
    return queryset


def prepare_worker_lease_queryset(queryset: QuerySet) -> QuerySet:
    """Annotate DB-clock lease liveness in one query to eliminate policy N+1."""
    from django_ray.lease_policy import LeasePolicyError, policy_rows, read_lease_policy
    from django_ray.runner.leasing import live_lease_freshness_q

    using = queryset.db
    try:
        policy = read_lease_policy(using=using)
    except LeasePolicyError:
        return queryset.annotate(
            _admin_lease_live=Case(
                When(is_active=False, then=Value(False)),
                default=Value(None),
                output_field=BooleanField(null=True),
            )
        )
    valid_policy = policy_rows(using=using, revision=policy.revision)
    return queryset.annotate(
        _admin_lease_live=Case(
            When(is_active=False, then=Value(False)),
            When(~Exists(valid_policy), then=Value(None)),
            When(
                Q(is_active=True) & live_lease_freshness_q(policy.duration),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(null=True),
        )
    )


def secure_admin_response(response: HttpResponse) -> HttpResponse:
    """Apply the fixed cache and MIME-sniffing policy to lazy diagnostics."""
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def admin_json_response(
    payload: dict[str, Any],
    *,
    status: int = 200,
    max_bytes: int,
) -> HttpResponse:
    """Serialize one explicitly byte-bounded admin response."""
    try:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise ValueError("Admin workflow diagnostics are not serializable") from error
    if len(content) > max_bytes:
        raise ValueError("Admin workflow diagnostics exceed their response limit")
    return secure_admin_response(
        HttpResponse(content, status=status, content_type="application/json; charset=utf-8")
    )


def bounded_admin_detail_render_failure(*, max_bytes: int) -> HttpResponse:
    """Return one fixed 503 when ordinary detail rendering cannot complete safely."""
    content = ADMIN_DETAIL_RENDER_FAILURE_BODY[: max(0, max_bytes)]
    return secure_admin_response(
        HttpResponse(
            content,
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    )


def bounded_admin_detail_response(
    request: HttpRequest,
    response: HttpResponse,
    *,
    admin_site: admin.AdminSite,
    opts: Any,
    max_bytes: int,
    template_response_cls: Any = TemplateResponse,
) -> HttpResponse:
    """Defer the ordinary detail cap until Django finishes template middleware."""
    if response.status_code != 200:
        return secure_admin_response(response)

    def bound_rendered_response(rendered_response: HttpResponse) -> HttpResponse:
        if getattr(rendered_response, "streaming", False):
            return bounded_admin_detail_render_failure(max_bytes=max_bytes)
        if len(rendered_response.content) <= max_bytes:
            return secure_admin_response(rendered_response)

        limited = template_response_cls(
            request,
            "admin/django_ray/bounded_task_detail_limit.html",
            {
                **admin_site.each_context(request),
                "title": "Task detail response limit reached",
                "opts": opts,
                "django_ray_detail_response_limit_bytes": max_bytes,
                "django_ray_detail_back_url": reverse(
                    f"{admin_site.name}:{opts.app_label}_{opts.model_name}_changelist",
                    current_app=admin_site.name,
                ),
            },
            status=413,
        )
        limited_response = limited.render()
        if getattr(limited_response, "streaming", False):
            return bounded_admin_detail_render_failure(max_bytes=max_bytes)
        if len(limited_response.content) > max_bytes:
            limited_response = HttpResponse(
                "Task detail response exceeds its fixed safety limit.",
                status=413,
                content_type="text/plain; charset=utf-8",
            )
        return secure_admin_response(limited_response)

    if not isinstance(response, TemplateResponse):
        try:
            return bound_rendered_response(response)
        except Exception:
            return bounded_admin_detail_render_failure(max_bytes=max_bytes)

    original_render = response.render

    def render_with_bound(_response: TemplateResponse) -> HttpResponse:
        try:
            rendered_response = original_render()
            return bound_rendered_response(rendered_response)
        except Exception:
            return bounded_admin_detail_render_failure(max_bytes=max_bytes)

    cast(Any, response).render = MethodType(render_with_bound, response)
    return secure_admin_response(response)


def readonly_admin_urls(stock_urls: list[Any], *, opts: Any) -> list[Any]:
    """Remove stock object routes that are inapplicable to immutable task records."""
    omitted_names = {
        f"{opts.app_label}_{opts.model_name}_delete",
        f"{opts.app_label}_{opts.model_name}_history",
    }
    return [url for url in stock_urls if getattr(url, "name", None) not in omitted_names]


def readonly_admin_removed_object_route(
    _request: HttpRequest,
    **_kwargs: Any,
) -> HttpResponse:
    """Keep removed stock route-shaped paths from falling into Admin's catch-all."""
    raise Http404("This read-only task record does not expose that Admin route.")


def readonly_admin_change_method_response(request: HttpRequest) -> HttpResponse | None:
    """Reject mutation and non-read methods before immutable object lookup."""
    if request.method in {"GET", "HEAD"}:
        return None
    if request.method == "POST":
        return secure_admin_response(
            HttpResponse(
                "Task execution details are read-only.",
                status=403,
                content_type="text/plain; charset=utf-8",
            )
        )
    return secure_admin_response(HttpResponseNotAllowed(["GET", "HEAD"]))


def admin_json_attachment(
    payload: dict[str, Any],
    *,
    filename: str,
    max_bytes: int,
) -> HttpResponse:
    """Return one compact, redacted JSON attachment within a strict wire bound."""
    try:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise ValueError("Admin workflow diagnostics are not serializable") from error
    if len(content) > max_bytes:
        raise ValueError("Admin workflow diagnostics exceed their response limit")
    response = HttpResponse(content, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return secure_admin_response(response)


def workflow_diagnostics_error_response(
    *,
    code: str,
    message: str,
    status: int,
) -> HttpResponse:
    """Return one fixed-shape safe failure without storage diagnostics."""
    return admin_json_response(
        {"code": code, "message": message},
        status=status,
        max_bytes=ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES,
    )


def corrupt_workflow_plan_presentation() -> dict[str, Any]:
    """Return the compact fail-closed plan shape expected by the admin UI."""
    return {
        "status": "CORRUPT",
        "definition_name": None,
        "definition_revision": None,
        "topology_class": None,
        "declared_node_count": None,
        "retry_safe": None,
        "fingerprint": None,
        "fingerprint_compact": None,
        "requested_policy": None,
        "selected_strategy": None,
        "reporting_policy": None,
        "eligible_strategies": [],
        "rejection_counts": {},
        "retained_rejections": 0,
        "total_rejections": 0,
        "unretained_rejections": 0,
    }


def workflow_reporting_policy_hint(serialized_selection: Any) -> str | None:
    """Recover only the bounded policy enum when the plan itself is unusable."""
    if not isinstance(serialized_selection, str):
        return None
    try:
        selection = validate_plan_selection_manifest(json.loads(serialized_selection))
        policy = effective_plan_selection_reporting_policy(selection)
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError):
        return None
    return policy if isinstance(policy, str) else None


def workflow_progress_presentation(
    *,
    state: str,
    availability: str | None = None,
    complete: bool = False,
    workflow_state: str | None = None,
    reporting_policy: str | None = None,
    truncation_reasons: list[str] | None = None,
    topology_nodes: bool = False,
    topology_edges: bool = False,
    node_details: bool = False,
) -> dict[str, Any]:
    """Build one exact, bounded progress-presentation object."""
    presentation: dict[str, Any] = {
        "state": state,
        "message": WORKFLOW_PROGRESS_MESSAGES[state],
        "availability": availability,
        "complete": complete,
        "truncation_reasons": sorted(set(truncation_reasons or [])),
        "actions": {
            "topology_nodes": topology_nodes,
            "topology_edges": topology_edges,
            "node_details": node_details,
        },
    }
    if workflow_state is not None:
        presentation["workflow_state"] = workflow_state
    if reporting_policy is not None:
        presentation["reporting_policy"] = reporting_policy
    return presentation


def build_execution_change_view_context(
    admin_site: admin.AdminSite,
    opts: Any,
    object_id: str,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build read-only execution change-view URLs for polling and workflow endpoints."""
    encoded_id = quote(object_id)
    prefix = f"{admin_site.name}:{opts.app_label}_{opts.model_name}"
    return {
        **(extra_context or {}),
        "show_delete": False,
        "show_save": False,
        "show_save_and_add_another": False,
        "show_save_and_continue": False,
        "django_ray_observability_url": reverse(
            f"{prefix}_observability",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_diagnostics_url": reverse(
            f"{prefix}_workflow_diagnostics",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_plan_download_url": reverse(
            f"{prefix}_workflow_plan_download",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_selection_download_url": reverse(
            f"{prefix}_workflow_plan_selection_download",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_graph_url": reverse(
            f"{prefix}_workflow_graph",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_topology_nodes_url": reverse(
            f"{prefix}_workflow_topology_nodes",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_topology_edges_url": reverse(
            f"{prefix}_workflow_topology_edges",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_node_details_url": reverse(
            f"{prefix}_workflow_node_details",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
        "django_ray_workflow_node_detail_url": reverse(
            f"{prefix}_workflow_node_detail",
            args=[encoded_id],
            current_app=admin_site.name,
        ),
    }


def replay_cli_command(task_id: Any) -> str | None:
    """Build a shell-safe read-only local replay CLI command for one execution."""
    if task_id is None:
        return None
    normalized = str(task_id).strip()
    if not normalized or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized):
        return None
    return f"python manage.py django_ray_replay {shlex.quote(normalized)}"


def populate_execution_change_form_context(
    model_admin: Any,
    request: HttpRequest,
    context: dict[str, Any],
    *,
    obj: Any,
    inline_cls: type[Any],
    uses_unfold: bool,
    terminal_states: tuple[Any, ...],
    retryable_states: tuple[Any, ...],
    cancellable_states: tuple[Any, ...],
    has_sensitive_permission: Callable[[HttpRequest, Any], bool],
    retry_guidance_fn: Callable[[str], str],
    archived_attempts_limit: int = ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS,
) -> None:
    """Populate archived attempts, live-panel flags, and action URLs for change forms."""
    archived_attempts: list[int] = []
    archived_attempts_truncated = False
    attempt_history_total: int | None = None
    attempt_history_shown = 0
    attempt_history_omitted = 0
    attempt_history_url: str | None = None
    admin_site = model_admin.admin_site
    found_attempt_inline = False
    can_view_execution = obj is not None and bool(model_admin.has_view_permission(request, obj))
    if obj is not None:
        for inline_formset in context.get("inline_admin_formsets", ()):
            if not isinstance(inline_formset.opts, inline_cls):
                continue
            found_attempt_inline = True
            inline_forms = list(inline_formset)
            attempt_history_shown = len(inline_forms)
            first_attempt = inline_forms[0].original if inline_forms else None
            annotated_total = (
                first_attempt.__dict__.get("admin_inline_total")
                if isinstance(first_attempt, TaskAttempt)
                else 0
            )
            attempt_history_total = (
                annotated_total
                if type(annotated_total) is int and annotated_total >= attempt_history_shown
                else attempt_history_shown
            )
            attempt_history_omitted = attempt_history_total - attempt_history_shown
            attempt_history_url = (
                reverse(
                    f"{admin_site.name}:django_ray_taskattempt_changelist",
                    current_app=admin_site.name,
                )
                + "?"
                + urlencode({"execution__id__exact": obj.pk})
            )
            for inline_form in inline_forms:
                attempt = inline_form.original
                attempt_any = cast(Any, attempt)
                if (
                    not isinstance(attempt, TaskAttempt)
                    or attempt_any.state != TaskState.FAILED
                    or attempt_any.attempt_number >= obj.attempt_number
                ):
                    continue
                if len(archived_attempts) >= archived_attempts_limit:
                    archived_attempts_truncated = True
                    break
                archived_attempts.append(int(attempt_any.attempt_number))
            break
        attempt_admin = admin_site._registry.get(TaskAttempt)
        can_view_attempts = attempt_admin is not None and bool(
            attempt_admin.has_view_permission(request)
        )
        if not found_attempt_inline and can_view_execution and can_view_attempts:
            try:
                attempt_history_url = (
                    reverse(
                        f"{admin_site.name}:django_ray_taskattempt_changelist",
                        current_app=admin_site.name,
                    )
                    + "?"
                    + urlencode({"execution__id__exact": obj.pk})
                )
            except NoReverseMatch:
                attempt_history_url = None
            database = str(getattr(obj._state, "db", None) or "default")
            standalone_failed_attempts = list(
                cast(Any, TaskAttempt)
                .objects.using(database)
                .filter(
                    execution_id=obj.pk,
                    state=TaskState.FAILED,
                    attempt_number__lt=obj.attempt_number,
                )
                .order_by("-attempt_number")
                .values_list("attempt_number", flat=True)[: archived_attempts_limit + 1]
            )
            if len(standalone_failed_attempts) > archived_attempts_limit:
                archived_attempts_truncated = True
                standalone_failed_attempts = standalone_failed_attempts[:archived_attempts_limit]
            archived_attempts = [int(number) for number in standalone_failed_attempts]
    archived_attempts.sort()
    context["django_ray_archived_workflow_attempts"] = tuple(archived_attempts)
    context["django_ray_archived_workflow_attempts_truncated"] = archived_attempts_truncated
    context["django_ray_archived_workflow_attempts_limit"] = archived_attempts_limit
    context["django_ray_archived_workflow_attempts_history_omitted"] = attempt_history_omitted
    context["django_ray_attempt_history_total"] = attempt_history_total
    context["django_ray_attempt_history_shown"] = attempt_history_shown
    context["django_ray_attempt_history_omitted"] = attempt_history_omitted
    context["django_ray_attempt_history_url"] = attempt_history_url
    context["django_ray_admin_uses_unfold"] = uses_unfold
    context["show_history"] = False
    context["django_ray_retry_available"] = False
    context["django_ray_cancel_available"] = False
    context["django_ray_cancel_url"] = None
    context["django_ray_sensitive_data_url"] = None
    context["django_ray_replay_command"] = (
        replay_cli_command(obj.task_id) if (obj is not None and can_view_execution) else None
    )
    context["django_ray_initial_terminal"] = obj is not None and obj.state in terminal_states
    context["django_ray_show_workflow_diagnostics"] = bool(
        obj is not None
        and (
            obj.workflow_run_id
            or obj.workflow_plan_fingerprint
            or obj.workflow_plan_pinned_attempt is not None
            or archived_attempts
        )
    )
    if obj is not None and can_view_execution and has_sensitive_permission(request, obj):
        opts = model_admin.model._meta
        context["django_ray_sensitive_data_url"] = reverse(
            f"{admin_site.name}:{opts.app_label}_{opts.model_name}_sensitive_data",
            args=[quote(str(obj.pk))],
            current_app=admin_site.name,
        )
    if obj is not None and model_admin.has_change_permission(request) and can_view_execution:
        opts = model_admin.model._meta
        context["django_ray_retry_guidance"] = retry_guidance_fn(str(obj.state))
        if obj.state in retryable_states:
            context["django_ray_retry_available"] = True
            context["django_ray_retry_url"] = reverse(
                f"{admin_site.name}:{opts.app_label}_{opts.model_name}_retry",
                args=[quote(str(obj.pk))],
                current_app=admin_site.name,
            )
        if obj.state in cancellable_states and model_admin.has_action_permission(request, obj):
            context["django_ray_cancel_available"] = True
            context["django_ray_cancel_url"] = reverse(
                f"{admin_site.name}:{opts.app_label}_{opts.model_name}_cancel",
                args=[quote(str(obj.pk))],
                current_app=admin_site.name,
            )


def observability_execution(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
) -> Any:
    """Authorize before running bounded annotated metadata queries."""
    user = getattr(cast(Any, request), "user", None)
    if not getattr(user, "is_authenticated", False):
        raise PermissionDenied
    has_global_view = bool(model_admin.has_view_permission(request))
    try:
        execution_id = model_admin.model._meta.pk.to_python(unquote(object_id))
    except (ValidationError, ValueError) as error:
        if not has_global_view:
            raise PermissionDenied from error
        raise Http404("Ray task execution was not found") from error

    base_queryset = model_admin.get_queryset(request)
    if not has_global_view:
        try:
            candidate = base_queryset.only("pk", "task_id").get(pk=execution_id)
        except (ObjectDoesNotExist, ValidationError, ValueError) as error:
            raise PermissionDenied from error
        if not model_admin.has_view_permission(request, candidate):
            raise PermissionDenied
        database = str(getattr(candidate._state, "db", None) or "default")
        base_queryset = model_admin.get_queryset(request).using(database)

    try:
        queryset = annotate_bounded_admin_text(
            annotate_execution_protocol_availability(
                base_queryset.only(*model_admin.observability_fields)
            ),
            field_names=ADMIN_PROTOCOL_PROVENANCE_FIELDS,
            max_bytes=ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
            max_chars=ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
            namespace="protocol",
        )
        queryset = annotate_bounded_admin_text(
            queryset,
            field_names=("error_message",),
            max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
            max_chars=ADMIN_DIAGNOSTIC_MAX_CHARS,
            namespace="observability",
        )
        execution = queryset.get(pk=execution_id)
    except (ObjectDoesNotExist, ValidationError, ValueError) as error:
        if not has_global_view:
            raise PermissionDenied from error
        raise Http404("Ray task execution was not found") from error
    if not model_admin.has_view_permission(request, execution):
        raise PermissionDenied
    return execution


def authorized_workflow_read_execution(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
) -> Any:
    """Load bounded identity fields and authorize before parsing read arguments."""
    user = getattr(cast(Any, request), "user", None)
    if not getattr(user, "is_authenticated", False):
        raise PermissionDenied
    try:
        execution_id = model_admin.model._meta.pk.to_python(unquote(object_id))
    except (ValidationError, ValueError) as error:
        if not model_admin.has_view_permission(request):
            raise PermissionDenied from error
        raise Http404("Ray task execution was not found") from error
    try:
        execution = (
            model_admin.get_queryset(request)
            .only(*model_admin.workflow_read_fields)
            .get(pk=execution_id)
        )
    except (ObjectDoesNotExist, ValidationError, ValueError) as error:
        if not model_admin.has_view_permission(request):
            raise PermissionDenied from error
        raise Http404("Ray task execution was not found") from error
    if not model_admin.has_view_permission(request, execution):
        raise PermissionDenied
    return execution


def load_bounded_workflow_plan_fields(
    model_admin: Any,
    request: HttpRequest,
    execution: Any,
) -> Any:
    """Load and reauthorize one identity-fenced, SQL-bounded plan row."""
    database = str(getattr(execution._state, "db", None) or "default")
    try:
        fresh = (
            model_admin.get_queryset(request)
            .using(database)
            .annotate(
                _admin_plan_bytes=AdminOctetLength("workflow_plan_json"),
                _admin_selection_bytes=AdminOctetLength("workflow_plan_selection"),
            )
            .annotate(
                _admin_bounded_plan=Case(
                    When(
                        _admin_plan_bytes__lte=MAX_PLAN_BYTES,
                        then=F("workflow_plan_json"),
                    ),
                    default=Value(None),
                    output_field=TextField(),
                ),
                _admin_bounded_selection=Case(
                    When(
                        _admin_selection_bytes__lte=MAX_PLAN_SELECTION_BYTES,
                        then=F("workflow_plan_selection"),
                    ),
                    default=Value(None),
                    output_field=TextField(),
                ),
            )
            .only(*model_admin.workflow_read_fields, "workflow_plan_fingerprint")
            .get(pk=execution.pk)
        )
    except ObjectDoesNotExist as error:
        raise Http404("Ray task execution was not found") from error

    row = {
        "workflow_plan_fingerprint": fresh.workflow_plan_fingerprint,
        "_admin_plan_bytes": fresh._admin_plan_bytes,
        "_admin_selection_bytes": fresh._admin_selection_bytes,
        "_admin_bounded_plan": fresh._admin_bounded_plan,
        "_admin_bounded_selection": fresh._admin_bounded_selection,
    }

    bounded_fields = (
        (
            row["_admin_plan_bytes"],
            row["_admin_bounded_plan"],
            MAX_PLAN_BYTES,
        ),
        (
            row["_admin_selection_bytes"],
            row["_admin_bounded_selection"],
            MAX_PLAN_SELECTION_BYTES,
        ),
    )
    for stored_bytes, value, maximum in bounded_fields:
        if stored_bytes is None:
            if value is not None:
                raise ValueError("Workflow diagnostic storage failed validation")
            continue
        if (
            type(stored_bytes) is not int
            or not 0 <= stored_bytes <= maximum
            or not isinstance(value, str)
        ):
            raise ValueError("Workflow diagnostic storage failed validation")
        try:
            decoded_bytes = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("Workflow diagnostic storage failed validation") from error
        if decoded_bytes != stored_bytes:
            raise ValueError("Workflow diagnostic storage failed validation")

    fresh.workflow_plan_json = row["_admin_bounded_plan"]
    fresh.workflow_plan_selection = row["_admin_bounded_selection"]
    identity_fields = (
        "pk",
        "task_id",
        "callable_path",
        "attempt_number",
        "execution_generation",
        "workflow_run_id",
    )
    if any(getattr(fresh, field) != getattr(execution, field) for field in identity_fields):
        raise Http404("Ray task execution was not found")
    if not model_admin.has_view_permission(request, fresh):
        raise PermissionDenied
    return fresh


def workflow_authorizer(
    model_admin: Any,
    request: HttpRequest,
) -> Callable[[RayTaskExecution], bool]:
    """Build the per-request object authorizer required by public reads."""
    return lambda execution: bool(model_admin.has_view_permission(request, execution))


def workflow_read_error_response(error: WorkflowProgressReadError) -> HttpResponse:
    """Map bounded service errors without exposing storage diagnostics."""
    status_by_code = {
        WorkflowProgressReadErrorCode.INVALID_ARGUMENT: 400,
        WorkflowProgressReadErrorCode.INVALID_CURSOR: 400,
        WorkflowProgressReadErrorCode.CURSOR_MISMATCH: 409,
        WorkflowProgressReadErrorCode.MISSING: 409,
        WorkflowProgressReadErrorCode.CORRUPT: 503,
    }
    if error.code is WorkflowProgressReadErrorCode.ACCESS_DENIED:
        raise PermissionDenied
    if error.code is WorkflowProgressReadErrorCode.NOT_FOUND:
        raise Http404("Ray task execution was not found")
    return secure_admin_response(
        JsonResponse(
            {
                "code": error.code.value,
                "message": bounded_redacted_text(str(error)),
            },
            status=status_by_code.get(error.code, 400),
        )
    )


def parse_page_limit(request: HttpRequest) -> int:
    """Parse optional integer page limit query parameter."""
    value = request.GET.get("limit")
    if value is None:
        return 100
    try:
        return int(value)
    except ValueError as error:
        raise ValueError("limit must be an integer") from error


def parse_attempt_number(request: HttpRequest) -> int | None:
    """Parse optional integer attempt_number query parameter."""
    value = request.GET.get("attempt_number")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError("attempt_number must be an integer") from error


def preflight_workflow_collection(
    model_admin: Any,
    request: HttpRequest,
    execution: RayTaskExecution,
    *,
    collection: str,
    attempt_number: int | None = None,
    hooks: WorkflowReadHooks = _DEFAULT_WORKFLOW_READ_HOOKS,
) -> dict[str, Any]:
    """Read one authorized record to prove a claimed collection is useful."""
    authorizer = model_admin._workflow_authorizer(request)
    if collection == "topology_nodes":
        return hooks.list_nodes(
            execution,
            authorize=authorizer,
            limit=1,
            attempt_number=attempt_number,
        )
    if collection == "topology_edges":
        return hooks.list_edges(
            execution,
            authorize=authorizer,
            limit=1,
            attempt_number=attempt_number,
        )
    return hooks.list_details(
        execution,
        authorize=authorizer,
        limit=1,
        attempt_number=attempt_number,
    )


def lazy_workflow_progress_presentation(
    model_admin: Any,
    request: HttpRequest,
    execution: RayTaskExecution,
    plan: dict[str, Any],
    plan_binding: dict[str, str] | None,
    *,
    attempt_number: int | None = None,
    reporting_policy_hint: str | None = None,
    hooks: WorkflowReadHooks = _DEFAULT_WORKFLOW_READ_HOOKS,
) -> dict[str, Any]:
    """Explain the bounded progress state and advertise only useful actions."""
    preflight_method = getattr(model_admin, "_preflight_workflow_collection", None)
    preflight_overridden = not getattr(preflight_method, "_django_ray_default_preflight", False)
    use_coalesced = hooks.uses_default_collection_readers() and not preflight_overridden
    preflight_pages: dict[str, dict[str, Any]] | None = None
    try:
        if use_coalesced:
            include_collections = plan.get("status") == "AVAILABLE"
            bundle = hooks.read_collections(
                execution,
                authorize=model_admin._workflow_authorizer(request),
                attempt_number=attempt_number,
                include_topology_nodes=include_collections,
                include_topology_edges=include_collections,
                include_node_details=include_collections,
                limit=1,
                include_legacy=True,
                infer_current_reporting_policy=True,
            )
            envelope = bundle["summary"]
            if include_collections:
                preflight_pages = {
                    "topology_nodes": bundle["topology_nodes"],
                    "topology_edges": bundle["topology_edges"],
                    "node_details": bundle["node_details"],
                }
        else:
            envelope = hooks.get_summary(
                execution,
                authorize=model_admin._workflow_authorizer(request),
                include_legacy=True,
                attempt_number=attempt_number,
            )
    except WorkflowProgressReadError as error:
        if error.code is WorkflowProgressReadErrorCode.ACCESS_DENIED:
            raise PermissionDenied from error
        if error.code is WorkflowProgressReadErrorCode.NOT_FOUND:
            raise Http404("Ray task execution was not found") from error
        state = "MISSING" if error.code is WorkflowProgressReadErrorCode.MISSING else "CORRUPT"
        return workflow_progress_presentation(
            state=state,
            availability=error.code.value,
            reporting_policy=(
                "terminal_only" if reporting_policy_hint == "terminal_only" else None
            ),
        )

    source_schema = envelope.get("source_schema_version")
    summary = envelope.get("summary")
    availability = envelope.get("availability")
    availability_text = availability if isinstance(availability, str) else None
    complete = envelope.get("complete") is True

    if (
        type(source_schema) is int
        and source_schema < WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION
        and isinstance(summary, dict)
    ):
        return workflow_progress_presentation(
            state="LEGACY_ONLY",
            availability=availability_text,
        )
    if source_schema is None:
        effective_reporting_policy = (
            plan_binding.get("reporting_policy")
            if plan_binding is not None
            else reporting_policy_hint
        )
        if effective_reporting_policy == "terminal_only":
            terminal_only_state = {
                "NOT_REPORTED": "TERMINAL_ONLY_PENDING",
                "MISSING": "TERMINAL_ONLY_MISSING",
            }.get(availability_text or "")
            if terminal_only_state is None:
                return workflow_progress_presentation(
                    state="CORRUPT",
                    availability="CORRUPT",
                )
            return workflow_progress_presentation(
                state=terminal_only_state,
                availability=availability_text,
                reporting_policy="terminal_only",
            )
        state_by_availability = {
            "DISABLED": "DISABLED",
            "MISSING": "REQUESTED_MISSING",
        }
        if availability_text == "NOT_REPORTED":
            state = (
                "REQUESTED_NOT_REPORTED"
                if plan.get("status") == "AVAILABLE"
                and plan_binding is not None
                and plan_binding.get("reporting_policy") == "full"
                else "NOT_REPORTED"
            )
        else:
            state = state_by_availability.get(availability_text or "", "CORRUPT")
        return workflow_progress_presentation(
            state=state,
            availability=availability_text,
        )
    if source_schema != WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION or not isinstance(summary, dict):
        return workflow_progress_presentation(
            state="CORRUPT",
            availability=availability_text,
        )
    summary_reporting_policy = (
        "terminal_only" if summary.get("reporting_policy") == "terminal_only" else None
    )
    if plan.get("status") != "AVAILABLE" or plan_binding is None:
        return workflow_progress_presentation(
            state="CORRUPT",
            availability="CORRUPT",
            reporting_policy=summary_reporting_policy,
        )
    if (
        summary.get("plan_fingerprint") != plan_binding["fingerprint"]
        or summary.get("selected_strategy") != plan_binding["selected_strategy"]
        or summary.get("reporting_policy") != plan_binding["reporting_policy"]
    ):
        return workflow_progress_presentation(
            state="CORRUPT",
            availability="CORRUPT",
            reporting_policy=summary_reporting_policy,
        )

    state_by_availability = {
        "DISABLED": "DISABLED",
        "OMITTED_BY_POLICY": "OMITTED_BY_POLICY",
        "AVAILABLE": "AVAILABLE",
        "TRUNCATED": "TRUNCATED",
        "LIMIT_EXCEEDED": "LIMIT_EXCEEDED",
        "EXPIRED": "EXPIRED",
        "MISSING": "MISSING",
        "CORRUPT": "CORRUPT",
    }
    reporting_policy = summary.get("reporting_policy")
    workflow_state = summary.get("state")
    if reporting_policy == "terminal_only":
        if (
            availability_text != "OMITTED_BY_POLICY"
            or workflow_state not in WORKFLOW_PROGRESS_TERMINAL_STATES
        ):
            return workflow_progress_presentation(
                state="CORRUPT",
                availability="CORRUPT",
            )
        state = "TERMINAL_ONLY"
    elif availability_text == "NOT_REPORTED":
        state = (
            "REQUESTED_NOT_REPORTED"
            if plan.get("status") == "AVAILABLE"
            and plan_binding is not None
            and plan_binding.get("reporting_policy") == "full"
            else "NOT_REPORTED"
        )
    else:
        state = state_by_availability.get(availability_text or "", "CORRUPT")
    detail = summary.get("detail")
    node_counts = summary.get("node_counts")
    edge_counts = summary.get("edge_counts")
    publication = envelope.get("publication")
    if (
        not isinstance(detail, dict)
        or not isinstance(node_counts, dict)
        or not isinstance(edge_counts, dict)
        or not isinstance(publication, dict)
    ):
        return workflow_progress_presentation(
            state="CORRUPT",
            availability=availability_text,
        )
    truncation_reasons = detail.get("truncation_reasons")
    if not isinstance(truncation_reasons, list) or not all(
        isinstance(reason, str) for reason in truncation_reasons
    ):
        return workflow_progress_presentation(
            state="CORRUPT",
            availability=availability_text,
        )

    actions_available = state in {"AVAILABLE", "TRUNCATED"} and plan.get("status") == "AVAILABLE"
    topology_revision = publication.get("topology_version")
    detail_revision = publication.get("detail_revision")
    retained_nodes = node_counts.get("retained_topology")
    retained_edges = edge_counts.get("retained_topology")
    retained_detail = node_counts.get("retained_detail")
    claimed_actions = {
        "topology_nodes": (
            actions_available
            and type(topology_revision) is int
            and topology_revision > 0
            and type(retained_nodes) is int
            and retained_nodes > 0
        ),
        "topology_edges": (
            actions_available
            and type(topology_revision) is int
            and topology_revision > 0
            and type(retained_edges) is int
            and retained_edges > 0
        ),
        "node_details": (
            actions_available
            and type(detail_revision) is int
            and detail_revision > 0
            and type(retained_detail) is int
            and retained_detail > 0
        ),
    }
    useful_actions: dict[str, bool] = {}
    for collection, claimed in claimed_actions.items():
        if not claimed:
            useful_actions[collection] = False
            continue
        if preflight_pages is not None and collection in preflight_pages:
            page = preflight_pages[collection]
        else:
            try:
                page = model_admin._preflight_workflow_collection(
                    request,
                    execution,
                    collection=collection,
                    attempt_number=attempt_number,
                )
            except WorkflowProgressReadError as error:
                if error.code is WorkflowProgressReadErrorCode.ACCESS_DENIED:
                    raise PermissionDenied from error
                if error.code is WorkflowProgressReadErrorCode.NOT_FOUND:
                    raise Http404("Ray task execution was not found") from error
                preflight_state = (
                    "MISSING" if error.code is WorkflowProgressReadErrorCode.MISSING else "CORRUPT"
                )
                return workflow_progress_presentation(
                    state=preflight_state,
                    availability=error.code.value,
                )
        page_availability = page.get("availability")
        if page_availability not in {"AVAILABLE", "TRUNCATED"}:
            preflight_state = (
                page_availability
                if page_availability
                in {
                    "DISABLED",
                    "OMITTED_BY_POLICY",
                    "EXPIRED",
                    "MISSING",
                    "CORRUPT",
                }
                else "CORRUPT"
            )
            return workflow_progress_presentation(
                state=preflight_state,
                availability=(
                    page_availability if isinstance(page_availability, str) else "CORRUPT"
                ),
            )
        returned_count = page.get("returned_count")
        items = page.get("items")
        if (
            type(returned_count) is not int
            or not isinstance(items, list)
            or len(items) != returned_count
        ):
            return workflow_progress_presentation(
                state="CORRUPT",
                availability="CORRUPT",
            )
        if returned_count < 1:
            return workflow_progress_presentation(
                state="MISSING",
                availability="MISSING",
            )
        useful_actions[collection] = True

    return workflow_progress_presentation(
        state=state,
        availability=availability_text,
        complete=complete,
        workflow_state=workflow_state if isinstance(workflow_state, str) else None,
        reporting_policy=summary_reporting_policy,
        truncation_reasons=truncation_reasons,
        topology_nodes=useful_actions["topology_nodes"],
        topology_edges=useful_actions["topology_edges"],
        node_details=useful_actions["node_details"],
    )


def workflow_diagnostics_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
) -> HttpResponse:
    """Return lazy compact plan and progress diagnostics for the change form."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    execution = model_admin._authorized_workflow_read_execution(request, object_id)
    try:
        attempt_number = model_admin._attempt_number(request)
    except ValueError:
        return workflow_diagnostics_error_response(
            code="INVALID_ARGUMENT",
            message="attempt_number must be an integer.",
            status=400,
        )

    from django_ray import observability

    plan_binding = None
    reporting_policy_hint = None
    try:
        execution = model_admin._load_bounded_workflow_plan_fields(request, execution)
        reporting_policy_hint = workflow_reporting_policy_hint(execution.workflow_plan_selection)
        plan = observability.get_workflow_plan_diagnostics(execution)
        if plan["status"] == "AVAILABLE":
            plan_binding = observability.get_workflow_plan_binding(execution)
    except (ValueError, observability.WorkflowObservabilityError):
        plan = corrupt_workflow_plan_presentation()
    if plan["status"] != "AVAILABLE" and reporting_policy_hint is not None:
        plan["reporting_policy"] = reporting_policy_hint
    progress = model_admin._lazy_workflow_progress_presentation(
        request,
        execution,
        plan,
        plan_binding,
        attempt_number=attempt_number,
        reporting_policy_hint=reporting_policy_hint,
    )
    if plan["status"] != "AVAILABLE":
        progress["actions"] = {
            "topology_nodes": False,
            "topology_edges": False,
            "node_details": False,
        }
    payload = {
        "schema": "django-ray.admin-workflow-diagnostics",
        "schema_version": 1,
        "plan": plan,
        "progress": progress,
    }
    try:
        return admin_json_response(
            payload,
            max_bytes=ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES,
        )
    except ValueError:
        return workflow_diagnostics_error_response(
            code="CORRUPT",
            message="Workflow diagnostics failed validation.",
            status=503,
        )


def verified_workflow_plan_download(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
) -> dict[str, Any] | None:
    """Authorize, byte-bound, validate, and redact one complete plan pair."""
    execution = model_admin._authorized_workflow_read_execution(request, object_id)
    execution = model_admin._load_bounded_workflow_plan_fields(request, execution)

    from django_ray import observability

    diagnostics = observability.get_workflow_plan_diagnostics(execution)
    if diagnostics["status"] == "NOT_RECORDED":
        return None
    workflow_plan = observability.get_workflow_plan(execution)
    if (
        not isinstance(workflow_plan, dict)
        or not isinstance(workflow_plan.get("manifest"), dict)
        or not isinstance(workflow_plan.get("selection"), dict)
    ):
        raise observability.WorkflowObservabilityError("Workflow diagnostics are incomplete")
    return workflow_plan


def workflow_plan_attachment_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    component: str,
) -> HttpResponse:
    """Serve verified, redacted effective plan or selection JSON attachment."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    from django_ray import observability

    try:
        workflow_plan = model_admin._verified_workflow_plan_download(request, object_id)
        if workflow_plan is None:
            return workflow_diagnostics_error_response(
                code="NOT_RECORDED",
                message="Workflow diagnostics were not recorded.",
                status=404,
            )
        if component == "manifest":
            return admin_json_attachment(
                {
                    "fingerprint": workflow_plan["fingerprint"],
                    "manifest": workflow_plan["manifest"],
                },
                filename="plan.json",
                max_bytes=ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES,
            )
        return admin_json_attachment(
            {
                "fingerprint": workflow_plan["fingerprint"],
                "selection": workflow_plan["selection"],
            },
            filename="selection.json",
            max_bytes=ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES,
        )
    except Http404:
        raise
    except PermissionDenied:
        raise
    except (TypeError, ValueError, observability.WorkflowObservabilityError):
        return workflow_diagnostics_error_response(
            code="CORRUPT",
            message="Workflow diagnostics failed validation.",
            status=503,
        )


def observability_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    hooks: WorkflowReadHooks = _DEFAULT_WORKFLOW_READ_HOOKS,
) -> HttpResponse:
    """Return a versioned durable summary without querying Ray or task logs."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    execution = model_admin._observability_execution(request, object_id)
    error_message, error_status = bounded_admin_text_value(
        execution,
        "error_message",
        max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
        namespace="observability",
    )
    if error_status == "oversized":
        execution.error_message = ADMIN_DETAIL_OVERSIZED_MESSAGE
    elif error_status != "value":
        execution.error_message = ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    else:
        execution.error_message = error_message

    from django_ray import observability

    workflow_error = None
    workflow_error_code = None
    progress_envelope = None
    try:
        progress_envelope = hooks.get_summary(
            execution,
            authorize=model_admin._workflow_authorizer(request),
            include_legacy=False,
            infer_current_reporting_policy=False,
        )
        progress = progress_envelope["summary"]
    except WorkflowProgressReadError as error:
        if error.code is WorkflowProgressReadErrorCode.ACCESS_DENIED:
            raise PermissionDenied from error
        if error.code is WorkflowProgressReadErrorCode.NOT_FOUND:
            raise Http404("Ray task execution was not found") from error
        progress = None
        workflow_error = bounded_redacted_text(str(error))
        workflow_error_code = error.code.value
    summary = observability.get_task_summary(
        execution,
        include_workflow_plan_selection=False,
        workflow_progress=progress,
    )
    provenance: dict[str, str | None] = {}
    for field_name in ADMIN_PROTOCOL_PROVENANCE_FIELDS:
        value, value_status = bounded_admin_text_value(
            execution,
            field_name,
            max_bytes=ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
            max_chars=ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
            namespace="protocol",
        )
        provenance[field_name] = (
            redact_text(value) if value_status == "value" and value is not None else None
        )
    summary.update(
        execution_protocol_version=execution.execution_protocol_version,
        **provenance,
        protocol_compatible_worker_available=execution.protocol_compatible_worker_available,
        queue_capacity_attested=False,
    )
    if summary.get("error_message") is not None:
        summary["error_message"] = bounded_redacted_text(summary["error_message"])
    if error_status != "value":
        summary["error_message_truncated"] = True
    if workflow_error is not None:
        summary["workflow_error"] = workflow_error
        summary["workflow_error_code"] = workflow_error_code
    node_counts = progress.get("node_counts", {}) if progress is not None else {}
    schema_v3 = (
        progress is not None
        and progress.get("schema_version") == observability.WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION
    )
    summary["workflow"] = (
        {
            "revision": progress.get(
                "summary_revision" if schema_v3 else "revision",
                0,
            ),
            "run_identity": progress.get("run_identity"),
            "state": progress.get("state", "RUNNING"),
            "total_nodes": (
                node_counts.get("discovered", 0) if schema_v3 else progress.get("total_nodes", 0)
            ),
            "completed_nodes": (
                node_counts.get("succeeded", 0) if schema_v3 else progress.get("completed_nodes", 0)
            ),
            "failed_nodes": (
                node_counts.get("failed", 0) if schema_v3 else progress.get("failed_nodes", 0)
            ),
            "running_nodes": (
                node_counts.get("running", 0) if schema_v3 else progress.get("running_nodes", 0)
            ),
            "pending_nodes": (
                node_counts.get("pending", 0) if schema_v3 else progress.get("pending_nodes", 0)
            ),
            "progress_percent": progress.get("progress_percent", 0.0),
            **(
                {
                    "reporting_policy": progress.get("reporting_policy"),
                    "selected_strategy": progress.get("selected_strategy"),
                    "declared_nodes": node_counts.get("declared"),
                    "declared_edges": progress.get("edge_counts", {}).get("declared"),
                    "timestamps": progress.get("timestamps"),
                    "terminal": progress.get("terminal"),
                    "detail": progress.get("detail"),
                }
                if schema_v3
                else {}
            ),
        }
        if progress is not None
        else None
    )
    summary["workflow_availability"] = (
        progress_envelope.get("availability") if progress_envelope is not None else None
    )
    try:
        return admin_json_response(
            summary,
            max_bytes=ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES,
        )
    except ValueError:
        return admin_json_response(
            {
                "code": "RESPONSE_LIMIT",
                "message": "Live task status exceeds its fixed response limit.",
            },
            status=413,
            max_bytes=ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES,
        )


def workflow_graph_issue_response(issue: AdminWorkflowGraphError) -> HttpResponse:
    """Return one fixed-shape graph degradation without partial records."""
    return admin_json_response(
        degraded_admin_workflow_graph(issue.status, reason=issue.reason),
        status=issue.http_status,
        max_bytes=ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES,
    )


def workflow_graph_read_error_response(error: WorkflowProgressReadError) -> HttpResponse:
    """Collapse bounded read failures into the graph's safe status vocabulary."""
    if error.code is WorkflowProgressReadErrorCode.ACCESS_DENIED:
        raise PermissionDenied from error
    if error.code is WorkflowProgressReadErrorCode.NOT_FOUND:
        raise Http404("Ray task execution was not found") from error
    if error.code is WorkflowProgressReadErrorCode.MISSING:
        return workflow_graph_issue_response(AdminWorkflowGraphError("UNAVAILABLE"))
    return workflow_graph_issue_response(AdminWorkflowGraphError("CORRUPT", http_status=503))


def workflow_graph_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    hooks: WorkflowReadHooks = _DEFAULT_WORKFLOW_READ_HOOKS,
) -> HttpResponse:
    """Return one coherent terminal graph from fixed bounded first-page reads."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    execution = model_admin._authorized_workflow_read_execution(request, object_id)
    try:
        attempt_number = model_admin._attempt_number(request)
    except ValueError:
        return workflow_graph_issue_response(AdminWorkflowGraphError("CORRUPT", http_status=503))
    authorizer = model_admin._workflow_authorizer(request)
    if hooks.uses_default_collection_readers():

        def _should_load_graph_collections(summary_envelope: dict[str, Any]) -> bool:
            try:
                inspect_admin_workflow_graph_summary(summary_envelope)
            except (
                AdminWorkflowGraphError,
                AttributeError,
                KeyError,
                TypeError,
                UnicodeError,
                ValueError,
            ):
                return False
            return True

        try:
            bundle = hooks.read_collections(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                include_topology_nodes=True,
                include_topology_edges=True,
                include_node_details=True,
                limit=ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
                topology_nodes_limit=ADMIN_WORKFLOW_GRAPH_MAX_NODES,
                topology_edges_limit=ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
                node_details_limit=ADMIN_WORKFLOW_GRAPH_MAX_DETAILS,
                should_load_collections=_should_load_graph_collections,
                include_legacy=False,
                infer_current_reporting_policy=True,
            )
        except WorkflowProgressReadError as error:
            return workflow_graph_read_error_response(error)

        summary = bundle["summary"]
        try:
            expectation = inspect_admin_workflow_graph_summary(summary)
        except AdminWorkflowGraphError as issue:
            return workflow_graph_issue_response(issue)
        except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
            return workflow_graph_issue_response(
                AdminWorkflowGraphError("CORRUPT", http_status=503)
            )

        topology_nodes = bundle["topology_nodes"]
        topology_edges = bundle["topology_edges"]
        node_details = bundle["node_details"]
    else:
        try:
            summary = hooks.get_summary(
                execution,
                authorize=authorizer,
                include_legacy=False,
                infer_current_reporting_policy=True,
                attempt_number=attempt_number,
            )
        except WorkflowProgressReadError as error:
            return workflow_graph_read_error_response(error)

        try:
            expectation = inspect_admin_workflow_graph_summary(summary)
        except AdminWorkflowGraphError as issue:
            return workflow_graph_issue_response(issue)
        except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
            return workflow_graph_issue_response(
                AdminWorkflowGraphError("CORRUPT", http_status=503)
            )

        try:
            topology_nodes = hooks.list_nodes(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                limit=ADMIN_WORKFLOW_GRAPH_MAX_NODES,
            )
            topology_edges = hooks.list_edges(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                limit=ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
            )
            node_details = hooks.list_details(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                limit=ADMIN_WORKFLOW_GRAPH_MAX_DETAILS,
            )
        except WorkflowProgressReadError as error:
            return workflow_graph_read_error_response(error)

    try:
        payload = build_admin_workflow_graph(
            expectation,
            topology_nodes=topology_nodes,
            topology_edges=topology_edges,
            node_details=node_details,
        )
    except AdminWorkflowGraphError as issue:
        return workflow_graph_issue_response(issue)
    except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
        return workflow_graph_issue_response(AdminWorkflowGraphError("CORRUPT", http_status=503))
    try:
        return admin_json_response(
            payload,
            max_bytes=ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES,
        )
    except ValueError:
        return workflow_graph_issue_response(AdminWorkflowGraphError("LIMIT_EXCEEDED"))


def workflow_page_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    collection: str,
    hooks: WorkflowReadHooks = _DEFAULT_WORKFLOW_READ_HOOKS,
) -> HttpResponse:
    """Return one authorized bounded topology or normalized-node page."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    execution = model_admin._authorized_workflow_read_execution(request, object_id)
    try:
        limit = model_admin._page_limit(request)
        attempt_number = model_admin._attempt_number(request)
    except ValueError:
        return secure_admin_response(
            JsonResponse(
                {"code": "INVALID_ARGUMENT", "message": "page arguments are invalid"},
                status=400,
            )
        )
    authorizer = model_admin._workflow_authorizer(request)
    cursor = request.GET.get("cursor") or None
    try:
        if collection == "topology_nodes":
            payload = hooks.list_nodes(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                cursor=cursor,
                limit=limit,
            )
        elif collection == "topology_edges":
            payload = hooks.list_edges(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                cursor=cursor,
                limit=limit,
            )
        else:
            payload = hooks.list_details(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                state=request.GET.get("state") or None,
                cursor=cursor,
                limit=limit,
            )
    except WorkflowProgressReadError as error:
        return workflow_read_error_response(error)
    return secure_admin_response(JsonResponse(payload))


def workflow_node_detail_view(
    model_admin: Any,
    request: HttpRequest,
    object_id: str,
    *,
    hooks: WorkflowReadHooks = _DEFAULT_WORKFLOW_READ_HOOKS,
) -> HttpResponse:
    """Return one authorized indexed durable node record."""
    if request.method != "GET":
        return secure_admin_response(HttpResponseNotAllowed(["GET"]))
    execution = model_admin._authorized_workflow_read_execution(request, object_id)
    node_id = request.GET.get("node_id")
    if node_id is None:
        return secure_admin_response(
            JsonResponse(
                {"code": "INVALID_ARGUMENT", "message": "node_id is required"},
                status=400,
            )
        )
    try:
        attempt_number = model_admin._attempt_number(request)
        payload = hooks.get_detail(
            execution,
            node_id,
            authorize=model_admin._workflow_authorizer(request),
            attempt_number=attempt_number,
        )
    except ValueError:
        return secure_admin_response(
            JsonResponse(
                {"code": "INVALID_ARGUMENT", "message": "attempt_number must be an integer"},
                status=400,
            )
        )
    except WorkflowProgressReadError as error:
        return workflow_read_error_response(error)
    return secure_admin_response(JsonResponse(payload))
