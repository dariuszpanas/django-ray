"""Django admin configuration for django-ray."""

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from types import MethodType
from typing import Any, cast
from urllib.parse import urlencode

from django.apps import apps
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.auth import get_permission_codename
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import (
    BooleanField,
    Case,
    Count,
    F,
    Func,
    IntegerField,
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
    HttpResponseRedirect,
    JsonResponse,
)
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.http import quote, unquote
from django.utils.text import Truncator

from django_ray.conf.settings import get_settings
from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION
from django_ray.lifecycle import (
    TaskCancellationRequestStatus,
    TaskRetryRequestStatus,
    request_task_cancellation,
    request_task_retry,
)
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
)
from django_ray.protocol_status import annotate_execution_protocol_availability
from django_ray.redaction import normalize_terminal_text, redact_text, safe_json_dumps
from django_ray.runtime.runtime_env import RuntimeEnvSnapshotError
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
)
from django_ray.workflow.progress.runs import MAX_PLAN_SELECTION_BYTES
from django_ray.workflow.progress.summary import (
    WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION,
    WORKFLOW_PROGRESS_TERMINAL_STATES,
)

_DJANGO_RAY_ADMIN_USES_UNFOLD = apps.is_installed("unfold")

if _DJANGO_RAY_ADMIN_USES_UNFOLD:
    from unfold.admin import ModelAdmin as _ConfiguredModelAdmin
    from unfold.admin import TabularInline as _ConfiguredTabularInline
else:
    _ConfiguredModelAdmin = admin.ModelAdmin
    _ConfiguredTabularInline = admin.TabularInline

DjangoRayModelAdmin = cast(Any, _ConfiguredModelAdmin)
DjangoRayTabularInline = cast(Any, _ConfiguredTabularInline)

# Ray Dashboard URL fallback for local Ray.
RAY_DASHBOARD_URL = "http://localhost:8265"
ADMIN_DIAGNOSTIC_MAX_CHARS = 4096
ADMIN_ATTEMPT_INLINE_MAX_CHARS = 512
ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES = ADMIN_DIAGNOSTIC_MAX_CHARS * 4
ADMIN_ATTEMPT_INLINE_MAX_BYTES = ADMIN_ATTEMPT_INLINE_MAX_CHARS * 4
ADMIN_ATTEMPT_INLINE_MAX_ROWS = 25
ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES = 512 * 1024
ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES = 64 * 1024
ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES = 64 * 1024
ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES = 4 * 1024 * 1024
ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES = 16 * 1024
ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES = 128 * 1024
ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES = 32 * 1024
ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS = 100
_FULL_WORKFLOW_ATTEMPT_MARKER = '"reporting_policy":"full"'
ADMIN_RETRY_CONFIRMATION_MAX_TASKS = 100
ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS = 15 * 60
ADMIN_RETRYABLE_STATES = (
    TaskState.FAILED,
    TaskState.LOST,
    TaskState.EXPIRED,
)
_ADMIN_RETRY_CONFIRMATION_SALT = "django_ray.admin.retry-confirmation.v1"
_ADMIN_RETRY_CONFIRMATION_MAX_TOKEN_CHARS = 512
_ADMIN_RETRY_CONFIRMATION_SESSION_KEY = "django_ray_admin_retry_confirmation_nonce_v1"
_ADMIN_RETRY_CONFIRMATION_SESSION_NONCE = re.compile(r"[A-Za-z0-9_-]{43}")
_ADMIN_SENSITIVE_PERMISSION = "django_ray.view_sensitive_task_data"
_ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES = 64 * 1024
_ADMIN_SENSITIVE_HTML_EXPANSION_FACTOR = 6
_ADMIN_DETAIL_OVERSIZED_MESSAGE = (
    "Stored diagnostic omitted because it exceeds the ordinary Admin read limit."
)
_ADMIN_DETAIL_UNAVAILABLE_MESSAGE = (
    "Stored diagnostic unavailable because its bounded read could not be validated."
)
_ADMIN_DETAIL_INVALID_JSON_MESSAGE = (
    "Stored JSON omitted because it could not be parsed and redacted safely."
)
_ADMIN_DETAIL_RENDER_FAILURE_BODY = b"Task detail could not be rendered safely."
_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES = 128
_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS = 128
_ADMIN_PROTOCOL_PROVENANCE_FIELDS = (
    "created_with_django_ray_version",
    "managed_with_django_ray_version",
    "executor_django_ray_version",
)
_ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS = (
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
_ADMIN_EXECUTION_DETAIL_FIELDS = (
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
_ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS = (
    "error_message",
    "error_traceback",
    "result_data",
    "result_reference",
)
_ADMIN_ATTEMPT_DETAIL_FIELDS = (
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
_ADMIN_SENSITIVE_EXECUTION_SECTIONS = (
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
_ADMIN_SENSITIVE_ATTEMPT_OUTCOME_SECTION = (
    "Attempt outcome",
    (
        ("Result", "result_data"),
        ("Result reference", "result_reference"),
        ("Error", "error_message"),
        ("Traceback", "error_traceback"),
    ),
)
_WORKFLOW_PROGRESS_MESSAGES = {
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
    "EXPIRED": "Retained workflow topology and node detail have expired.",
    "MISSING": "The workflow summary references retained detail that is missing.",
    "CORRUPT": "Workflow progress failed validation.",
}


def _admin_retry_snapshot(
    queryset: QuerySet,
) -> list[dict[str, Any]]:
    rows = queryset.order_by("pk").values_list(
        "pk",
        "state",
        "attempt_number",
        "execution_generation",
        "metadata_schema_version",
        "execution_protocol_version",
        "created_with_django_ray_version",
        "managed_with_django_ray_version",
        "executor_django_ray_version",
        "workflow_run_id",
        "workflow_plan_fingerprint",
    )[: ADMIN_RETRY_CONFIRMATION_MAX_TASKS + 1]
    return [
        {
            "pk": str(pk),
            "state": str(state),
            "attempt_number": int(attempt_number),
            "execution_generation": int(execution_generation),
            "metadata_schema_version": int(metadata_schema_version),
            "execution_protocol_version": int(execution_protocol_version),
            "created_with_django_ray_version": (
                str(created_with_django_ray_version)
                if created_with_django_ray_version is not None
                else None
            ),
            "managed_with_django_ray_version": (
                str(managed_with_django_ray_version)
                if managed_with_django_ray_version is not None
                else None
            ),
            "executor_django_ray_version": (
                str(executor_django_ray_version)
                if executor_django_ray_version is not None
                else None
            ),
            "workflow_run_id": str(workflow_run_id) if workflow_run_id is not None else None,
            "workflow_plan_fingerprint": (
                str(workflow_plan_fingerprint) if workflow_plan_fingerprint is not None else None
            ),
            "known_workflow": bool(workflow_run_id or workflow_plan_fingerprint),
        }
        for (
            pk,
            state,
            attempt_number,
            execution_generation,
            metadata_schema_version,
            execution_protocol_version,
            created_with_django_ray_version,
            managed_with_django_ray_version,
            executor_django_ray_version,
            workflow_run_id,
            workflow_plan_fingerprint,
        ) in rows
    ]


def _admin_retry_snapshot_digest(snapshot: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _admin_retry_actor_fingerprint(request: HttpRequest) -> str:
    user = request.user
    actor_id = getattr(user, "pk", None)
    model_meta = getattr(user, "_meta", None)
    model_label = getattr(model_meta, "label_lower", None)
    if (
        not getattr(user, "is_authenticated", False)
        or actor_id is None
        or not isinstance(model_label, str)
        or not model_label
    ):
        raise PermissionDenied
    canonical = json.dumps(
        [model_label, str(actor_id)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _admin_retry_session_fingerprint(
    request: HttpRequest,
    *,
    create: bool,
) -> str | None:
    session = getattr(request, "session", None)
    if session is None:
        return None
    nonce = session.get(_ADMIN_RETRY_CONFIRMATION_SESSION_KEY)
    if nonce is None and create:
        nonce = secrets.token_urlsafe(32)
        session[_ADMIN_RETRY_CONFIRMATION_SESSION_KEY] = nonce
    if (
        not isinstance(nonce, str)
        or _ADMIN_RETRY_CONFIRMATION_SESSION_NONCE.fullmatch(nonce) is None
    ):
        return None
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def _admin_retry_confirmation_token(
    snapshot: list[dict[str, Any]],
    *,
    actor_fingerprint: str,
    session_fingerprint: str,
) -> str:
    return signing.dumps(
        {
            "version": 1,
            "actor": actor_fingerprint,
            "session": session_fingerprint,
            "count": len(snapshot),
            "digest": _admin_retry_snapshot_digest(snapshot),
        },
        salt=_ADMIN_RETRY_CONFIRMATION_SALT,
        compress=False,
    )


def _admin_retry_confirmation_matches(
    token: str,
    snapshot: list[dict[str, Any]],
    *,
    actor_fingerprint: str,
    session_fingerprint: str,
) -> bool:
    if not token or len(token) > _ADMIN_RETRY_CONFIRMATION_MAX_TOKEN_CHARS:
        return False
    try:
        payload = signing.loads(
            token,
            salt=_ADMIN_RETRY_CONFIRMATION_SALT,
            max_age=ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return False
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "actor",
        "session",
        "count",
        "digest",
    }:
        return False
    actor = payload.get("actor")
    session = payload.get("session")
    digest = payload.get("digest")
    if (
        payload.get("version") != 1
        or payload.get("count") != len(snapshot)
        or not isinstance(actor, str)
        or not isinstance(session, str)
        or not isinstance(digest, str)
    ):
        return False
    return (
        secrets.compare_digest(actor, actor_fingerprint)
        and secrets.compare_digest(session, session_fingerprint)
        and secrets.compare_digest(digest, _admin_retry_snapshot_digest(snapshot))
    )


class _AdminOctetLength(Func):
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


def _admin_bounded_annotation_name(namespace: str, field_name: str, suffix: str) -> str:
    """Build one private annotation name for a guarded text projection."""
    return f"admin_{namespace}_{field_name}_{suffix}"


def _annotate_bounded_admin_text(
    queryset: QuerySet,
    *,
    field_names: tuple[str, ...],
    max_bytes: int,
    max_chars: int,
    namespace: str,
) -> QuerySet:
    """Project text only when the database proves its UTF-8 byte size is safe."""
    lengths = {
        _admin_bounded_annotation_name(namespace, field_name, "bytes"): _AdminOctetLength(
            F(field_name)
        )
        for field_name in field_names
    }
    queryset = queryset.annotate(**lengths)
    characters = {
        _admin_bounded_annotation_name(namespace, field_name, "chars"): Length(F(field_name))
        for field_name in field_names
    }
    queryset = queryset.annotate(**characters)
    values = {
        _admin_bounded_annotation_name(namespace, field_name, "value"): Case(
            When(
                **{
                    f"{_admin_bounded_annotation_name(namespace, field_name, 'bytes')}__lte": (
                        max_bytes
                    ),
                    f"{_admin_bounded_annotation_name(namespace, field_name, 'chars')}__lte": (
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


def _bounded_admin_text_value(
    obj: Any,
    field_name: str,
    *,
    max_bytes: int = ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
    max_chars: int = ADMIN_DIAGNOSTIC_MAX_CHARS,
    namespace: str = "detail",
) -> tuple[str | None, str]:
    """Validate one SQL-guarded value without triggering a deferred-field read."""
    stored = obj.__dict__
    bytes_name = _admin_bounded_annotation_name(namespace, field_name, "bytes")
    chars_name = _admin_bounded_annotation_name(namespace, field_name, "chars")
    value_name = _admin_bounded_annotation_name(namespace, field_name, "value")
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


def _ordinary_admin_text_display(
    obj: Any,
    field_name: str,
    *,
    max_chars: int = ADMIN_DIAGNOSTIC_MAX_CHARS,
    max_bytes: int = ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
    namespace: str = "detail",
) -> str:
    value, status = _bounded_admin_text_value(
        obj,
        field_name,
        max_bytes=max_bytes,
        max_chars=max_chars,
        namespace=namespace,
    )
    if status == "oversized":
        return _ADMIN_DETAIL_OVERSIZED_MESSAGE
    if status != "value":
        return _ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    return _bounded_redacted_text(value, max_chars=max_chars)


def _ordinary_admin_json_display(obj: Any, field_name: str) -> str:
    value, status = _bounded_admin_text_value(obj, field_name)
    if status == "oversized":
        return _ADMIN_DETAIL_OVERSIZED_MESSAGE
    if status != "value":
        return _ADMIN_DETAIL_UNAVAILABLE_MESSAGE
    return _bounded_redacted_json(value)


def _secure_admin_response(response: HttpResponse) -> HttpResponse:
    """Apply the fixed cache and MIME-sniffing policy to lazy diagnostics."""
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _admin_json_response(
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
    return _secure_admin_response(
        HttpResponse(content, status=status, content_type="application/json; charset=utf-8")
    )


def _bounded_admin_detail_response(
    request: HttpRequest,
    response: HttpResponse,
    *,
    admin_site: admin.AdminSite,
    opts: Any,
    max_bytes: int,
) -> HttpResponse:
    """Defer the ordinary detail cap until Django finishes template middleware."""
    if response.status_code != 200:
        return _secure_admin_response(response)

    def bound_rendered_response(rendered_response: HttpResponse) -> HttpResponse:
        if getattr(rendered_response, "streaming", False):
            return _bounded_admin_detail_render_failure(max_bytes=max_bytes)
        if len(rendered_response.content) <= max_bytes:
            return _secure_admin_response(rendered_response)

        limited = TemplateResponse(
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
            return _bounded_admin_detail_render_failure(max_bytes=max_bytes)
        if len(limited_response.content) > max_bytes:
            limited_response = HttpResponse(
                "Task detail response exceeds its fixed safety limit.",
                status=413,
                content_type="text/plain; charset=utf-8",
            )
        return _secure_admin_response(limited_response)

    if not isinstance(response, TemplateResponse):
        try:
            return bound_rendered_response(response)
        except Exception:
            return _bounded_admin_detail_render_failure(max_bytes=max_bytes)

    original_render = response.render

    def render_with_bound(_response: TemplateResponse) -> HttpResponse:
        try:
            rendered_response = original_render()
            return bound_rendered_response(rendered_response)
        except Exception:
            return _bounded_admin_detail_render_failure(max_bytes=max_bytes)

    cast(Any, response).render = MethodType(render_with_bound, response)
    return _secure_admin_response(response)


def _readonly_admin_urls(stock_urls: list[Any], *, opts: Any) -> list[Any]:
    """Remove stock object routes that are inapplicable to immutable task records."""
    omitted_names = {
        f"{opts.app_label}_{opts.model_name}_delete",
        f"{opts.app_label}_{opts.model_name}_history",
    }
    return [url for url in stock_urls if getattr(url, "name", None) not in omitted_names]


def _readonly_admin_removed_object_route(
    _request: HttpRequest,
    **_kwargs: Any,
) -> HttpResponse:
    """Keep removed stock route-shaped paths from falling into Admin's catch-all."""
    raise Http404("This read-only task record does not expose that Admin route.")


def _readonly_admin_change_method_response(request: HttpRequest) -> HttpResponse | None:
    """Reject mutation and non-read methods before immutable object lookup."""
    if request.method in {"GET", "HEAD"}:
        return None
    if request.method == "POST":
        return _secure_admin_response(
            HttpResponse(
                "Task execution details are read-only.",
                status=403,
                content_type="text/plain; charset=utf-8",
            )
        )
    return _secure_admin_response(HttpResponseNotAllowed(["GET", "HEAD"]))


def _bounded_admin_detail_render_failure(*, max_bytes: int) -> HttpResponse:
    """Return one fixed 503 when ordinary detail rendering cannot complete safely."""
    content = _ADMIN_DETAIL_RENDER_FAILURE_BODY[: max(0, max_bytes)]
    return _secure_admin_response(
        HttpResponse(
            content,
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    )


def _admin_json_attachment(
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
    return _secure_admin_response(response)


def _workflow_diagnostics_error_response(
    *,
    code: str,
    message: str,
    status: int,
) -> HttpResponse:
    """Return one fixed-shape safe failure without storage diagnostics."""
    return _admin_json_response(
        {"code": code, "message": message},
        status=status,
        max_bytes=ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES,
    )


def _corrupt_workflow_plan_presentation() -> dict[str, Any]:
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


def _workflow_reporting_policy_hint(serialized_selection: Any) -> str | None:
    """Recover only the bounded policy enum when the plan itself is unusable."""
    if not isinstance(serialized_selection, str):
        return None
    try:
        selection = validate_plan_selection_manifest(json.loads(serialized_selection))
        policy = effective_plan_selection_reporting_policy(selection)
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError):
        return None
    return policy if isinstance(policy, str) else None


def _workflow_progress_presentation(
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
    presentation = {
        "state": state,
        "message": _WORKFLOW_PROGRESS_MESSAGES[state],
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


def _bounded_redacted_text(value: Any, *, max_chars: int = ADMIN_DIAGNOSTIC_MAX_CHARS) -> str:
    """Return redacted operator text with a hard display-size limit."""
    if value in (None, ""):
        return "-"
    return Truncator(redact_text(value)).chars(max_chars, truncate="... [truncated]")


def _bounded_traceback_display(value: Any) -> str:
    """Render inert traceback text with line breaks and safe wrapping."""
    rendered = _bounded_redacted_text(value)
    if rendered == "-":
        return rendered
    return format_html('<span class="django-ray-diagnostic">{}</span>', rendered)


def _bounded_redacted_json(value: str | None) -> str:
    """Return bounded, redacted JSON without exposing malformed raw payloads."""
    if not value:
        return "-"
    try:
        rendered = safe_json_dumps(json.loads(value))
    except (RecursionError, TypeError, UnicodeError, ValueError):
        return _ADMIN_DETAIL_INVALID_JSON_MESSAGE
    return Truncator(rendered).chars(
        ADMIN_DIAGNOSTIC_MAX_CHARS,
        truncate="... [truncated]",
    )


def _sensitive_annotation_name(field_name: str, suffix: str) -> str:
    return f"admin_sensitive_{field_name}_{suffix}"


def _bounded_sensitive_object(
    queryset: QuerySet,
    *,
    pk: Any,
    field_names: tuple[str, ...],
    identity_fields: tuple[str, ...],
) -> tuple[Any, dict[str, Any]]:
    """Fetch allowlisted text only when the database proves it is within bounds."""
    lengths = {
        _sensitive_annotation_name(field_name, "bytes"): _AdminOctetLength(F(field_name))
        for field_name in field_names
    }
    queryset = queryset.annotate(**lengths)
    values = {
        _sensitive_annotation_name(field_name, "value"): Case(
            When(
                **{
                    f"{_sensitive_annotation_name(field_name, 'bytes')}__lte": (
                        ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES
                    )
                },
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
        bytes_name = _sensitive_annotation_name(field_name, "bytes")
        value_name = _sensitive_annotation_name(field_name, "value")
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
        if byte_length > ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES:
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


def _sensitive_sections(
    row: dict[str, Any],
    section_specs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
) -> list[dict[str, Any]]:
    """Build an autoescaped presentation without exceeding the response budget."""
    raw_budget = max(
        0,
        (ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES - _ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES)
        // _ADMIN_SENSITIVE_HTML_EXPANSION_FACTOR,
    )
    sections: list[dict[str, Any]] = []
    for heading, field_specs in section_specs:
        fields: list[dict[str, Any]] = []
        for label, field_name in field_specs:
            byte_length = row[_sensitive_annotation_name(field_name, "bytes")]
            value = row[_sensitive_annotation_name(field_name, "value")]
            if byte_length is None:
                status = "null"
            elif byte_length > ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES:
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


def _has_sensitive_task_data_permission(
    request: HttpRequest,
    execution: RayTaskExecution,
) -> bool:
    """Honor global and object-specific grants for the owning execution."""
    return request.user.has_perm(_ADMIN_SENSITIVE_PERMISSION) or request.user.has_perm(
        _ADMIN_SENSITIVE_PERMISSION,
        execution,
    )


def _sensitive_diagnostics_response(
    request: HttpRequest,
    *,
    admin_site: admin.AdminSite,
    opts: Any,
    title: str,
    subject: str,
    back_url: str,
    sections: list[dict[str, Any]],
) -> HttpResponse:
    context = {
        **admin_site.each_context(request),
        "title": title,
        "opts": opts,
        "django_ray_sensitive_subject": subject,
        "django_ray_sensitive_back_url": back_url,
        "django_ray_sensitive_sections": sections,
        "django_ray_sensitive_field_limit_bytes": (ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES),
    }
    response = TemplateResponse(
        request,
        "admin/django_ray/sensitive_task_data.html",
        context,
    )
    response.render()
    if len(response.content) > ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES:
        response = TemplateResponse(
            request,
            "admin/django_ray/sensitive_task_data_limit.html",
            {
                "title": "Sensitive diagnostics response limit reached",
                "django_ray_sensitive_subject": subject,
                "django_ray_sensitive_back_url": back_url,
                "django_ray_sensitive_response_limit_bytes": (
                    ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES
                ),
            },
            status=413,
        )
        response.render()
    if len(response.content) > ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES:
        response = HttpResponse(
            "Sensitive diagnostics response exceeds its fixed safety limit.",
            status=413,
            content_type="text/plain; charset=utf-8",
        )
    return _secure_admin_response(response)


def _compact_admin_datetime(value: datetime | None) -> str:
    """Render a sortable timestamp compactly while preserving its full value."""
    if value is None:
        return "-"
    local_value = timezone.localtime(value) if timezone.is_aware(value) else value
    title = local_value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    return format_html(
        '<time datetime="{}" title="{}">{}</time>',
        value.isoformat(),
        title,
        date_format(local_value, "Y-m-d H:i"),
    )


def _compact_path_suffix(value: str, *, max_chars: int) -> str:
    """Preserve the distinguishing end of a long dotted path."""
    if len(value) <= max_chars:
        return value
    basename = value.rsplit(".", 1)[-1]
    if len(basename) < max_chars:
        return f"…{basename}"
    available = max_chars - 1
    prefix_chars = available // 2
    suffix_chars = available - prefix_chars
    return f"{basename[:prefix_chars]}…{basename[-suffix_chars:]}"


def _task_attempt_admin_mode() -> str:
    """Return the validated request-time attempt presentation mode."""
    return cast(str, get_settings()["TASK_ATTEMPT_ADMIN_MODE"])


class TaskAttemptInline(DjangoRayTabularInline):
    """Compact immutable attempt history on an execution change page."""

    model = TaskAttempt
    fk_name = "execution"
    fields = (
        "attempt_detail_link",
        "state",
        "started_display",
        "finished_display",
        "error_summary",
    )
    readonly_fields = fields
    ordering = ("-attempt_number",)
    extra = 0
    can_delete = False
    show_title = False
    show_change_link = False
    verbose_name_plural = "Attempt history"

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        """Return only the newest fixed number of guarded attempt summaries."""
        queryset = (
            super()
            .get_queryset(request)
            .annotate(
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
        return _annotate_bounded_admin_text(
            queryset,
            field_names=("error_message",),
            max_bytes=ADMIN_ATTEMPT_INLINE_MAX_BYTES,
            max_chars=ADMIN_ATTEMPT_INLINE_MAX_CHARS,
            namespace="inline",
        ).order_by("-attempt_number")

    def has_add_permission(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None = None,
    ) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None = None,
    ) -> bool:
        return False

    def has_view_permission(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None = None,
    ) -> bool:
        """Require global child access and authorization for the parent."""
        if not super().has_view_permission(request):
            return False
        parent_admin = self.admin_site._registry.get(RayTaskExecution)
        return parent_admin is not None and parent_admin.has_view_permission(request, obj)

    @admin.display(description="Attempt")
    def attempt_detail_link(self, obj: TaskAttempt) -> str:
        """Link the compact attempt label to its immutable detail view."""
        label = f"#{obj.attempt_number}"
        if obj.pk is None:
            return label
        url = reverse(
            f"{self.admin_site.name}:django_ray_taskattempt_change",
            args=[quote(str(obj.pk))],
            current_app=self.admin_site.name,
        )
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description="Started")
    def started_display(self, obj: TaskAttempt) -> str:
        return _compact_admin_datetime(obj.started_at)

    @admin.display(description="Finished")
    def finished_display(self, obj: TaskAttempt) -> str:
        return _compact_admin_datetime(obj.finished_at)

    @admin.display(description="Error")
    def error_summary(self, obj: TaskAttempt) -> str:
        preview, status = _bounded_admin_text_value(
            obj,
            "error_message",
            max_bytes=ADMIN_ATTEMPT_INLINE_MAX_BYTES,
            max_chars=ADMIN_ATTEMPT_INLINE_MAX_CHARS,
            namespace="inline",
        )
        if status == "oversized":
            return "Open the attempt detail to view bounded diagnostics."
        if status != "value":
            return _ADMIN_DETAIL_UNAVAILABLE_MESSAGE
        return _bounded_redacted_text(
            preview,
            max_chars=ADMIN_ATTEMPT_INLINE_MAX_CHARS,
        )


class ExecutionProtocolFilter(admin.SimpleListFilter):
    """Filter the current bounded protocol without a distinct-value table scan."""

    title = "execution protocol"
    parameter_name = "execution_protocol_version"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin
    ) -> tuple[tuple[str, str]]:
        del request, model_admin
        value = str(EXECUTION_PROTOCOL_VERSION)
        return ((value, f"Protocol {value}"),)

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        del request
        if self.value() == str(EXECUTION_PROTOCOL_VERSION):
            return queryset.filter(execution_protocol_version=EXECUTION_PROTOCOL_VERSION)
        return queryset


@admin.register(RayTaskExecution)
class RayTaskExecutionAdmin(DjangoRayModelAdmin):
    """Admin for RayTaskExecution model."""

    class Media:
        css = {"all": ("django_ray/admin/diagnostics.css",)}

    change_form_template = "admin/django_ray/raytaskexecution/change_form.html"
    retry_confirmation_template = "admin/django_ray/raytaskexecution/retry_confirmation.html"
    observability_fields = (
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
    workflow_read_fields = (
        "pk",
        "task_id",
        "callable_path",
        "attempt_number",
        "execution_generation",
        "workflow_run_id",
    )

    list_display = [
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
    list_fullwidth = True
    list_filter = [
        "state",
        "queue_name",
        "priority",
        ExecutionProtocolFilter,
        "created_at",
    ]
    search_fields = [
        "task_id",
        "callable_path",
        "ray_job_id",
    ]
    readonly_fields = [
        "task_id",
        "callable_path",
        "priority",
        "queue_name",
        "state",
        "attempt_number",
        "execution_generation",
        "metadata_schema_version",
        "execution_protocol_version",
        "created_with_django_ray_version_display",
        "managed_with_django_ray_version_display",
        "executor_django_ray_version_display",
        "protocol_compatible_worker_available_display",
        "ray_job_id_display",
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
        "args_json_display",
        "kwargs_json_display",
        "input_reference_display",
        "result_data_display",
        "result_reference_display",
        "completion_data_display",
        "cancellation_status",
        "cancellation_error_display",
        "error_message_display",
        "error_traceback_display",
    ]
    fieldsets = (
        (
            "Task Info",
            {
                "fields": (
                    "task_id",
                    "callable_path",
                    "priority",
                    "queue_name",
                    "state",
                    "attempt_number",
                    "execution_generation",
                    "workflow_run_id",
                    "workflow_plan_fingerprint",
                    "workflow_plan_pinned_attempt",
                ),
                "description": (
                    "Execution metadata is read-only. Use the package-owned Retry "
                    "or Cancel action from the task list for controlled state changes."
                ),
            },
        ),
        (
            "Execution Protocol",
            {
                "fields": (
                    "metadata_schema_version",
                    "execution_protocol_version",
                    "created_with_django_ray_version_display",
                    "managed_with_django_ray_version_display",
                    "executor_django_ray_version_display",
                    "protocol_compatible_worker_available_display",
                ),
                "description": (
                    "The integer execution protocol is the compatibility boundary. "
                    "Package versions are diagnostic provenance only."
                ),
            },
        ),
        (
            "Arguments",
            {
                "fields": (
                    "args_json_display",
                    "kwargs_json_display",
                    "input_reference_display",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Result",
            {
                "fields": (
                    "result_data_display",
                    "result_reference_display",
                    "completion_data_display",
                    "cancellation_status",
                    "cancellation_error_display",
                    "error_message_display",
                    "error_traceback_display",
                ),
            },
        ),
        (
            "Ray Execution",
            {
                "fields": (
                    "ray_job_id_display",
                    "ray_target_address",
                    "ray_address",
                    "claimed_by_worker",
                    "runtime_env_profile",
                    "runtime_env_hash",
                ),
                "description": (
                    "Ray Job ID is only available for Ray Job API mode. RuntimeEnv "
                    "values are intentionally not displayed because the durable "
                    "snapshot can contain sensitive application configuration. Use "
                    "cluster-mounted secrets; profile and hash identify the snapshot."
                ),
            },
        ),
        (
            "Timing",
            {
                "fields": (
                    "created_at",
                    "run_after",
                    "queue_timeout_seconds",
                    "queue_deadline_at",
                    "started_at",
                    "finished_at",
                    "last_heartbeat_at",
                ),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        """Use allowlisted and SQL-guarded projections for routine Admin reads."""
        queryset = (
            super()
            .get_queryset(request)
            .defer(
                "progress_data",
                "ray_job_request_reference",
                "runtime_env_json",
                "workflow_plan_json",
                "workflow_plan_selection",
                "workflow_progress_summary_json",
            )
        )
        resolver_match = request.resolver_match
        url_name = resolver_match.url_name if resolver_match is not None else None
        if request.__dict__.get("_django_ray_bounded_execution_detail") is True or url_name == (
            f"{self.opts.app_label}_{self.opts.model_name}_change"
        ):
            queryset = annotate_execution_protocol_availability(
                queryset.only(*_ADMIN_EXECUTION_DETAIL_FIELDS)
            )
            queryset = _annotate_bounded_admin_text(
                queryset,
                field_names=_ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS,
                max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
                max_chars=ADMIN_DIAGNOSTIC_MAX_CHARS,
                namespace="detail",
            )
            return _annotate_bounded_admin_text(
                queryset,
                field_names=_ADMIN_PROTOCOL_PROVENANCE_FIELDS,
                max_bytes=_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
                max_chars=_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
                namespace="protocol",
            )
        if url_name == f"{self.opts.app_label}_{self.opts.model_name}_changelist":
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

    def get_inlines(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None,
    ) -> list[type[admin.options.InlineModelAdmin]]:
        """Select contextual attempt history without import-time registration."""
        configured_inlines = list(super().get_inlines(request, obj))
        if obj is not None and _task_attempt_admin_mode() in {"inline", "both"}:
            configured_inlines.append(TaskAttemptInline)
        return configured_inlines

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add: bool = False,
        change: bool = False,
        form_url: str = "",
        obj: RayTaskExecution | None = None,
    ) -> HttpResponse:
        """Expose bounded failed-attempt identities already authorized for the inline."""
        archived_attempts: list[int] = []
        archived_attempts_truncated = False
        attempt_history_total: int | None = None
        attempt_history_shown = 0
        attempt_history_omitted = 0
        attempt_history_url: str | None = None
        if obj is not None:
            for inline_formset in context.get("inline_admin_formsets", ()):
                if not isinstance(inline_formset.opts, TaskAttemptInline):
                    continue
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
                        f"{self.admin_site.name}:django_ray_taskattempt_changelist",
                        current_app=self.admin_site.name,
                    )
                    + "?"
                    + urlencode({"execution__id__exact": obj.pk})
                )
                for inline_form in inline_forms:
                    attempt = inline_form.original
                    if (
                        not isinstance(attempt, TaskAttempt)
                        or attempt.state != TaskState.FAILED
                        or attempt.attempt_number >= obj.attempt_number
                    ):
                        continue
                    if len(archived_attempts) >= ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS:
                        archived_attempts_truncated = True
                        break
                    archived_attempts.append(attempt.attempt_number)
                break
        archived_attempts.sort()
        context["django_ray_archived_workflow_attempts"] = tuple(archived_attempts)
        context["django_ray_archived_workflow_attempts_truncated"] = archived_attempts_truncated
        context["django_ray_archived_workflow_attempts_limit"] = (
            ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS
        )
        context["django_ray_archived_workflow_attempts_history_omitted"] = attempt_history_omitted
        context["django_ray_attempt_history_total"] = attempt_history_total
        context["django_ray_attempt_history_shown"] = attempt_history_shown
        context["django_ray_attempt_history_omitted"] = attempt_history_omitted
        context["django_ray_attempt_history_url"] = attempt_history_url
        context["django_ray_admin_uses_unfold"] = _DJANGO_RAY_ADMIN_USES_UNFOLD
        context["show_history"] = False
        context["django_ray_retry_available"] = False
        context["django_ray_sensitive_data_url"] = None
        if (
            obj is not None
            and self.has_view_permission(request, obj)
            and _has_sensitive_task_data_permission(request, obj)
        ):
            opts = self.model._meta
            context["django_ray_sensitive_data_url"] = reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_sensitive_data",
                args=[quote(str(obj.pk))],
                current_app=self.admin_site.name,
            )
        if (
            obj is not None
            and self.has_change_permission(request)
            and self.has_view_permission(request, obj)
        ):
            context["django_ray_retry_guidance"] = self._retry_detail_guidance(obj.state)
            if obj.state in ADMIN_RETRYABLE_STATES:
                opts = self.model._meta
                context["django_ray_retry_available"] = True
                context["django_ray_retry_url"] = reverse(
                    f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_retry",
                    args=[quote(str(obj.pk))],
                    current_app=self.admin_site.name,
                )
        return super().render_change_form(
            request,
            context,
            add=add,
            change=change,
            form_url=form_url,
            obj=obj,
        )

    def has_view_permission(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None = None,
    ) -> bool:
        """Honor global grants and object-permission backends."""
        if super().has_view_permission(request, obj):
            return True
        if obj is None:
            return False
        opts = self.opts
        return request.user.has_perm(
            f"{opts.app_label}.{get_permission_codename('view', opts)}",
            obj,
        ) or request.user.has_perm(
            f"{opts.app_label}.{get_permission_codename('change', opts)}",
            obj,
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Executions must be created through a configured Django task backend."""
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None = None,
    ) -> bool:
        """Keep list actions authorized while preventing direct row saves."""
        if obj is not None:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: RayTaskExecution | None = None,
    ) -> bool:
        """Preserve durable execution and attempt history in the admin."""
        return False

    ordering = ["-created_at"]
    actions = ["retry_tasks", "cancel_tasks"]

    @staticmethod
    def _retry_detail_guidance(state: str) -> str:
        if state in ADMIN_RETRYABLE_STATES:
            return (
                "Retrying creates a new attempt and runs the callable again from its "
                "entry point. Review external side effects before continuing."
            )
        if state == TaskState.SUCCEEDED:
            return (
                "Retry is unavailable because this execution succeeded. To run the work "
                "again, enqueue a new task; that preserves this result and attempt history."
            )
        if state in {TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING}:
            return (
                f"Retry is unavailable while this execution is {state.lower()}. "
                "Wait for a terminal outcome before choosing a recovery action."
            )
        if state == TaskState.CANCELLED:
            return (
                "The Admin retry flow is limited to failed, lost, or expired executions. "
                "To run cancelled work again, enqueue a new task."
            )
        return "Retry is unavailable for this execution state."

    def get_urls(self) -> list[Any]:
        """Add the authenticated durable-summary endpoint used by the change form."""
        opts = self.model._meta
        custom_urls = [
            path(
                "<path:object_id>/history/",
                self.admin_site.admin_view(_readonly_admin_removed_object_route),
            ),
            path(
                "<path:object_id>/delete/",
                self.admin_site.admin_view(_readonly_admin_removed_object_route),
            ),
            path(
                "<path:object_id>/sensitive-data/",
                self.admin_site.admin_view(self.sensitive_data_view),
                name=f"{opts.app_label}_{opts.model_name}_sensitive_data",
            ),
            path(
                "<path:object_id>/retry/",
                self.admin_site.admin_view(self.retry_view),
                name=f"{opts.app_label}_{opts.model_name}_retry",
            ),
            path(
                "<path:object_id>/observability/",
                self.admin_site.admin_view(self.observability_view),
                name=f"{opts.app_label}_{opts.model_name}_observability",
            ),
            path(
                "<path:object_id>/workflow/diagnostics/",
                self.admin_site.admin_view(self.workflow_diagnostics_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_diagnostics",
            ),
            path(
                "<path:object_id>/workflow/diagnostics/plan.json",
                self.admin_site.admin_view(self.workflow_plan_download_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_plan_download",
            ),
            path(
                "<path:object_id>/workflow/diagnostics/selection.json",
                self.admin_site.admin_view(self.workflow_plan_selection_download_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_plan_selection_download",
            ),
            path(
                "<path:object_id>/workflow/graph/",
                self.admin_site.admin_view(self.workflow_graph_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_graph",
            ),
            path(
                "<path:object_id>/workflow/topology/nodes/",
                self.admin_site.admin_view(self.workflow_topology_nodes_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_topology_nodes",
            ),
            path(
                "<path:object_id>/workflow/topology/edges/",
                self.admin_site.admin_view(self.workflow_topology_edges_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_topology_edges",
            ),
            path(
                "<path:object_id>/workflow/nodes/",
                self.admin_site.admin_view(self.workflow_node_details_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_node_details",
            ),
            path(
                "<path:object_id>/workflow/node/",
                self.admin_site.admin_view(self.workflow_node_detail_view),
                name=f"{opts.app_label}_{opts.model_name}_workflow_node_detail",
            ),
        ]
        return custom_urls + _readonly_admin_urls(super().get_urls(), opts=opts)

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Provide the package-owned polling URL to the custom change form."""
        method_response = _readonly_admin_change_method_response(request)
        if method_response is not None:
            return method_response
        opts = self.model._meta
        context = {
            **(extra_context or {}),
            "show_delete": False,
            "show_save": False,
            "show_save_and_add_another": False,
            "show_save_and_continue": False,
            "django_ray_observability_url": reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_observability",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_diagnostics_url": reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_workflow_diagnostics",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_plan_download_url": reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_workflow_plan_download",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_selection_download_url": reverse(
                f"{self.admin_site.name}:"
                f"{opts.app_label}_{opts.model_name}_workflow_plan_selection_download",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_graph_url": reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_workflow_graph",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_topology_nodes_url": reverse(
                f"{self.admin_site.name}:"
                f"{opts.app_label}_{opts.model_name}_workflow_topology_nodes",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_topology_edges_url": reverse(
                f"{self.admin_site.name}:"
                f"{opts.app_label}_{opts.model_name}_workflow_topology_edges",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_node_details_url": reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_workflow_node_details",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
            "django_ray_workflow_node_detail_url": reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_workflow_node_detail",
                args=[quote(object_id)],
                current_app=self.admin_site.name,
            ),
        }
        request.__dict__["_django_ray_bounded_execution_detail"] = True
        try:
            response = super().change_view(request, object_id, form_url, context)
            return _bounded_admin_detail_response(
                request,
                response,
                admin_site=self.admin_site,
                opts=self.opts,
                max_bytes=ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
            )
        finally:
            request.__dict__.pop("_django_ray_bounded_execution_detail", None)

    def sensitive_data_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Render a bounded, permission-gated view of stored task diagnostics."""
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        try:
            execution_id = self.model._meta.pk.to_python(unquote(object_id))
            execution = (
                self.get_queryset(request)
                .only("pk", "task_id", "callable_path", "state")
                .get(pk=execution_id)
            )
        except (RayTaskExecution.DoesNotExist, ValidationError, ValueError) as error:
            raise Http404("Ray task execution was not found") from error
        if not self.has_view_permission(request, execution):
            raise PermissionDenied
        if not _has_sensitive_task_data_permission(request, execution):
            raise PermissionDenied

        field_names = tuple(
            field_name
            for _, field_specs in _ADMIN_SENSITIVE_EXECUTION_SECTIONS
            for _, field_name in field_specs
        )
        database = str(getattr(execution._state, "db", None) or "default")
        try:
            fresh, row = _bounded_sensitive_object(
                self.get_queryset(request).using(database),
                pk=execution.pk,
                field_names=field_names,
                identity_fields=("pk", "task_id", "callable_path", "state"),
            )
        except RayTaskExecution.DoesNotExist as error:
            raise Http404("Ray task execution was not found") from error
        if fresh.pk != execution.pk or fresh.task_id != execution.task_id:
            raise Http404("Ray task execution was not found")
        if not self.has_view_permission(request, fresh):
            raise PermissionDenied
        if not _has_sensitive_task_data_permission(request, fresh):
            raise PermissionDenied
        execution = fresh
        back_url = reverse(
            f"{self.admin_site.name}:{self.opts.app_label}_{self.opts.model_name}_change",
            args=[quote(str(execution.pk))],
            current_app=self.admin_site.name,
        )
        return _sensitive_diagnostics_response(
            request,
            admin_site=self.admin_site,
            opts=self.opts,
            title="Unredacted task data",
            subject=(
                f"Execution {execution.task_id} — {execution.callable_path} ({execution.state})"
            ),
            back_url=back_url,
            sections=_sensitive_sections(row, _ADMIN_SENSITIVE_EXECUTION_SECTIONS),
        )

    def retry_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Open and complete the fenced retry confirmation from one detail page."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied

        try:
            execution_id = self.model._meta.pk.to_python(unquote(object_id))
            execution = self.get_queryset(request).only("pk", "state").get(pk=execution_id)
        except (RayTaskExecution.DoesNotExist, ValidationError, ValueError) as error:
            raise Http404("Ray task execution was not found") from error
        if not self.has_view_permission(request, execution):
            raise PermissionDenied

        detail_url = reverse(
            f"{self.admin_site.name}:{self.opts.app_label}_{self.opts.model_name}_change",
            args=[quote(str(execution.pk))],
            current_app=self.admin_site.name,
        )
        response = self._retry_tasks_with_confirmation(
            request,
            self.get_queryset(request).filter(pk=execution.pk),
            cancel_url=detail_url,
        )
        if response is not None:
            return response
        return HttpResponseRedirect(detail_url)

    def _observability_execution(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> RayTaskExecution:
        """Load only bounded task metadata before the authorized read service."""
        try:
            execution_id = self.model._meta.pk.to_python(unquote(object_id))
            queryset = _annotate_bounded_admin_text(
                annotate_execution_protocol_availability(
                    self.get_queryset(request).only(*self.observability_fields)
                ),
                field_names=_ADMIN_PROTOCOL_PROVENANCE_FIELDS,
                max_bytes=_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
                max_chars=_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
                namespace="protocol",
            )
            queryset = _annotate_bounded_admin_text(
                queryset,
                field_names=("error_message",),
                max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
                max_chars=ADMIN_DIAGNOSTIC_MAX_CHARS,
                namespace="observability",
            )
            return queryset.get(pk=execution_id)
        except (RayTaskExecution.DoesNotExist, ValidationError, ValueError) as error:
            raise Http404("Ray task execution was not found") from error

    def _authorized_workflow_read_execution(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> RayTaskExecution:
        """Load bounded identity fields and authorize before parsing read arguments."""
        try:
            execution_id = self.model._meta.pk.to_python(unquote(object_id))
            execution = (
                self.get_queryset(request).only(*self.workflow_read_fields).get(pk=execution_id)
            )
        except (RayTaskExecution.DoesNotExist, ValidationError, ValueError) as error:
            raise Http404("Ray task execution was not found") from error
        if not self.has_view_permission(request, execution):
            raise PermissionDenied
        return execution

    def _load_bounded_workflow_plan_fields(
        self,
        request: HttpRequest,
        execution: RayTaskExecution,
    ) -> RayTaskExecution:
        """Load and reauthorize one identity-fenced, SQL-bounded plan row."""
        database = str(getattr(execution._state, "db", None) or "default")
        try:
            fresh = (
                self.get_queryset(request)
                .using(database)
                .annotate(
                    _admin_plan_bytes=_AdminOctetLength("workflow_plan_json"),
                    _admin_selection_bytes=_AdminOctetLength("workflow_plan_selection"),
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
                .only(*self.workflow_read_fields, "workflow_plan_fingerprint")
                .get(pk=execution.pk)
            )
        except RayTaskExecution.DoesNotExist as error:
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
        if not self.has_view_permission(request, fresh):
            raise PermissionDenied
        return fresh

    def _workflow_authorizer(
        self,
        request: HttpRequest,
    ) -> Callable[[RayTaskExecution], bool]:
        """Build the per-request object authorizer required by public reads."""
        return lambda execution: self.has_view_permission(request, execution)

    @staticmethod
    def _workflow_read_error_response(error: WorkflowProgressReadError) -> JsonResponse:
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
        response = JsonResponse(
            {
                "code": error.code.value,
                "message": _bounded_redacted_text(str(error)),
            },
            status=status_by_code.get(error.code, 400),
        )
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _page_limit(request: HttpRequest) -> int:
        value = request.GET.get("limit")
        if value is None:
            return 100
        try:
            return int(value)
        except ValueError as error:
            raise ValueError("limit must be an integer") from error

    @staticmethod
    def _attempt_number(request: HttpRequest) -> int | None:
        value = request.GET.get("attempt_number")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError as error:
            raise ValueError("attempt_number must be an integer") from error

    def _preflight_workflow_collection(
        self,
        request: HttpRequest,
        execution: RayTaskExecution,
        *,
        collection: str,
        attempt_number: int | None = None,
    ) -> dict[str, Any]:
        """Read one authorized record to prove a claimed collection is useful."""
        authorizer = self._workflow_authorizer(request)
        if collection == "topology_nodes":
            return list_workflow_topology_nodes(
                execution,
                authorize=authorizer,
                limit=1,
                attempt_number=attempt_number,
            )
        if collection == "topology_edges":
            return list_workflow_topology_edges(
                execution,
                authorize=authorizer,
                limit=1,
                attempt_number=attempt_number,
            )
        return list_workflow_node_details(
            execution,
            authorize=authorizer,
            limit=1,
            attempt_number=attempt_number,
        )

    def _lazy_workflow_progress_presentation(
        self,
        request: HttpRequest,
        execution: RayTaskExecution,
        plan: dict[str, Any],
        plan_binding: dict[str, str] | None,
        *,
        attempt_number: int | None = None,
        reporting_policy_hint: str | None = None,
    ) -> dict[str, Any]:
        """Explain the bounded progress state and advertise only useful actions."""
        try:
            envelope = get_workflow_progress_summary(
                execution,
                authorize=self._workflow_authorizer(request),
                include_legacy=True,
                attempt_number=attempt_number,
            )
        except WorkflowProgressReadError as error:
            if error.code is WorkflowProgressReadErrorCode.ACCESS_DENIED:
                raise PermissionDenied from error
            if error.code is WorkflowProgressReadErrorCode.NOT_FOUND:
                raise Http404("Ray task execution was not found") from error
            state = "MISSING" if error.code is WorkflowProgressReadErrorCode.MISSING else "CORRUPT"
            return _workflow_progress_presentation(
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
            return _workflow_progress_presentation(
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
                    return _workflow_progress_presentation(
                        state="CORRUPT",
                        availability="CORRUPT",
                    )
                return _workflow_progress_presentation(
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
            return _workflow_progress_presentation(
                state=state,
                availability=availability_text,
            )
        if source_schema != WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION or not isinstance(
            summary, dict
        ):
            return _workflow_progress_presentation(
                state="CORRUPT",
                availability=availability_text,
            )
        summary_reporting_policy = (
            "terminal_only" if summary.get("reporting_policy") == "terminal_only" else None
        )
        if plan.get("status") != "AVAILABLE" or plan_binding is None:
            return _workflow_progress_presentation(
                state="CORRUPT",
                availability="CORRUPT",
                reporting_policy=summary_reporting_policy,
            )
        if (
            summary.get("plan_fingerprint") != plan_binding["fingerprint"]
            or summary.get("selected_strategy") != plan_binding["selected_strategy"]
            or summary.get("reporting_policy") != plan_binding["reporting_policy"]
        ):
            return _workflow_progress_presentation(
                state="CORRUPT",
                availability="CORRUPT",
                reporting_policy=summary_reporting_policy,
            )

        state_by_availability = {
            "DISABLED": "DISABLED",
            "OMITTED_BY_POLICY": "OMITTED_BY_POLICY",
            "AVAILABLE": "AVAILABLE",
            "TRUNCATED": "TRUNCATED",
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
                return _workflow_progress_presentation(
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
            return _workflow_progress_presentation(
                state="CORRUPT",
                availability=availability_text,
            )
        truncation_reasons = detail.get("truncation_reasons")
        if not isinstance(truncation_reasons, list) or not all(
            isinstance(reason, str) for reason in truncation_reasons
        ):
            return _workflow_progress_presentation(
                state="CORRUPT",
                availability=availability_text,
            )

        actions_available = (
            state in {"AVAILABLE", "TRUNCATED"} and plan.get("status") == "AVAILABLE"
        )
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
            try:
                page = self._preflight_workflow_collection(
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
                return _workflow_progress_presentation(
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
                return _workflow_progress_presentation(
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
                return _workflow_progress_presentation(
                    state="CORRUPT",
                    availability="CORRUPT",
                )
            if returned_count < 1:
                return _workflow_progress_presentation(
                    state="MISSING",
                    availability="MISSING",
                )
            useful_actions[collection] = True

        return _workflow_progress_presentation(
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
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Return lazy compact plan and progress diagnostics for the change form."""
        if request.method != "GET":
            return _secure_admin_response(HttpResponseNotAllowed(["GET"]))
        execution = self._authorized_workflow_read_execution(request, object_id)
        try:
            attempt_number = self._attempt_number(request)
        except ValueError:
            return _workflow_diagnostics_error_response(
                code="INVALID_ARGUMENT",
                message="attempt_number must be an integer.",
                status=400,
            )

        from django_ray import observability

        plan_binding = None
        reporting_policy_hint = None
        try:
            execution = self._load_bounded_workflow_plan_fields(request, execution)
            reporting_policy_hint = _workflow_reporting_policy_hint(
                execution.workflow_plan_selection
            )
            plan = observability.get_workflow_plan_diagnostics(execution)
            if plan["status"] == "AVAILABLE":
                plan_binding = observability.get_workflow_plan_binding(execution)
        except (ValueError, observability.WorkflowObservabilityError):
            plan = _corrupt_workflow_plan_presentation()
        if plan["status"] != "AVAILABLE" and reporting_policy_hint is not None:
            plan["reporting_policy"] = reporting_policy_hint
        progress = self._lazy_workflow_progress_presentation(
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
            return _admin_json_response(
                payload,
                max_bytes=ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES,
            )
        except ValueError:
            return _workflow_diagnostics_error_response(
                code="CORRUPT",
                message="Workflow diagnostics failed validation.",
                status=503,
            )

    def _verified_workflow_plan_download(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> dict[str, Any] | None:
        """Authorize, byte-bound, validate, and redact one complete plan pair."""
        execution = self._authorized_workflow_read_execution(request, object_id)
        execution = self._load_bounded_workflow_plan_fields(request, execution)

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

    def _workflow_plan_attachment_view(
        self,
        request: HttpRequest,
        object_id: str,
        *,
        component: str,
    ) -> HttpResponse:
        if request.method != "GET":
            return _secure_admin_response(HttpResponseNotAllowed(["GET"]))
        from django_ray import observability

        try:
            workflow_plan = self._verified_workflow_plan_download(request, object_id)
            if workflow_plan is None:
                return _workflow_diagnostics_error_response(
                    code="NOT_RECORDED",
                    message="Workflow diagnostics were not recorded.",
                    status=404,
                )
            if component == "manifest":
                return _admin_json_attachment(
                    {
                        "fingerprint": workflow_plan["fingerprint"],
                        "manifest": workflow_plan["manifest"],
                    },
                    filename="plan.json",
                    max_bytes=ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES,
                )
            return _admin_json_attachment(
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
            return _workflow_diagnostics_error_response(
                code="CORRUPT",
                message="Workflow diagnostics failed validation.",
                status=503,
            )

    def workflow_plan_download_view(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Download the verified, redacted effective workflow plan."""
        return self._workflow_plan_attachment_view(
            request,
            object_id,
            component="manifest",
        )

    def workflow_plan_selection_download_view(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Download the verified, redacted workflow strategy selection."""
        return self._workflow_plan_attachment_view(
            request,
            object_id,
            component="selection",
        )

    def observability_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Return a versioned durable summary without querying Ray or task logs."""
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        execution = self._observability_execution(request, object_id)
        error_message, error_status = _bounded_admin_text_value(
            execution,
            "error_message",
            max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
            namespace="observability",
        )
        if error_status == "oversized":
            execution.error_message = _ADMIN_DETAIL_OVERSIZED_MESSAGE
        elif error_status != "value":
            execution.error_message = _ADMIN_DETAIL_UNAVAILABLE_MESSAGE
        else:
            execution.error_message = error_message

        from django_ray import observability

        workflow_error = None
        workflow_error_code = None
        progress_envelope = None
        try:
            progress_envelope = get_workflow_progress_summary(
                execution,
                authorize=self._workflow_authorizer(request),
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
            workflow_error = _bounded_redacted_text(str(error))
            workflow_error_code = error.code.value
        summary = observability.get_task_summary(
            execution,
            include_workflow_plan_selection=False,
            workflow_progress=progress,
        )
        provenance: dict[str, str | None] = {}
        for field_name in _ADMIN_PROTOCOL_PROVENANCE_FIELDS:
            value, value_status = _bounded_admin_text_value(
                execution,
                field_name,
                max_bytes=_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
                max_chars=_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
                namespace="protocol",
            )
            provenance[field_name] = (
                redact_text(value) if value_status == "value" and value is not None else None
            )
        summary.update(
            execution_protocol_version=execution.execution_protocol_version,
            **provenance,
            protocol_compatible_worker_available=bool(
                execution.protocol_compatible_worker_available
            ),
            queue_capacity_attested=False,
        )
        if summary.get("error_message") is not None:
            summary["error_message"] = _bounded_redacted_text(summary["error_message"])
        if error_status != "value":
            summary["error_message_truncated"] = True
        if workflow_error is not None:
            summary["workflow_error"] = workflow_error
            summary["workflow_error_code"] = workflow_error_code
        node_counts = progress.get("node_counts", {}) if progress is not None else {}
        schema_v3 = (
            progress is not None
            and progress.get("schema_version")
            == observability.WORKFLOW_PROGRESS_SUMMARY_SCHEMA_VERSION
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
                    node_counts.get("discovered", 0)
                    if schema_v3
                    else progress.get("total_nodes", 0)
                ),
                "completed_nodes": (
                    node_counts.get("succeeded", 0)
                    if schema_v3
                    else progress.get("completed_nodes", 0)
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
            return _admin_json_response(
                summary,
                max_bytes=ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES,
            )
        except ValueError:
            return _admin_json_response(
                {
                    "code": "RESPONSE_LIMIT",
                    "message": "Live task status exceeds its fixed response limit.",
                },
                status=413,
                max_bytes=ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES,
            )

    @staticmethod
    def _workflow_graph_issue_response(issue: AdminWorkflowGraphError) -> HttpResponse:
        """Return one fixed-shape graph degradation without partial records."""
        return _admin_json_response(
            degraded_admin_workflow_graph(issue.status),
            status=issue.http_status,
            max_bytes=ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES,
        )

    def _workflow_graph_read_error_response(
        self,
        error: WorkflowProgressReadError,
    ) -> HttpResponse:
        """Collapse bounded read failures into the graph's safe status vocabulary."""
        if error.code is WorkflowProgressReadErrorCode.ACCESS_DENIED:
            raise PermissionDenied from error
        if error.code is WorkflowProgressReadErrorCode.NOT_FOUND:
            raise Http404("Ray task execution was not found") from error
        if error.code is WorkflowProgressReadErrorCode.MISSING:
            return self._workflow_graph_issue_response(AdminWorkflowGraphError("UNAVAILABLE"))
        return self._workflow_graph_issue_response(
            AdminWorkflowGraphError("CORRUPT", http_status=503)
        )

    def workflow_graph_view(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Return one coherent terminal graph from fixed bounded first-page reads."""
        if request.method != "GET":
            return _secure_admin_response(HttpResponseNotAllowed(["GET"]))
        execution = self._authorized_workflow_read_execution(request, object_id)
        try:
            attempt_number = self._attempt_number(request)
        except ValueError:
            return self._workflow_graph_issue_response(
                AdminWorkflowGraphError("CORRUPT", http_status=503)
            )
        authorizer = self._workflow_authorizer(request)
        try:
            summary = get_workflow_progress_summary(
                execution,
                authorize=authorizer,
                include_legacy=False,
                infer_current_reporting_policy=False,
                attempt_number=attempt_number,
            )
        except WorkflowProgressReadError as error:
            return self._workflow_graph_read_error_response(error)

        try:
            expectation = inspect_admin_workflow_graph_summary(summary)
        except AdminWorkflowGraphError as issue:
            return self._workflow_graph_issue_response(issue)
        except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
            return self._workflow_graph_issue_response(
                AdminWorkflowGraphError("CORRUPT", http_status=503)
            )

        try:
            topology_nodes = list_workflow_topology_nodes(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                limit=ADMIN_WORKFLOW_GRAPH_MAX_NODES,
            )
            topology_edges = list_workflow_topology_edges(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                limit=ADMIN_WORKFLOW_GRAPH_MAX_EDGES,
            )
            node_details = list_workflow_node_details(
                execution,
                authorize=authorizer,
                attempt_number=attempt_number,
                limit=ADMIN_WORKFLOW_GRAPH_MAX_DETAILS,
            )
        except WorkflowProgressReadError as error:
            return self._workflow_graph_read_error_response(error)

        try:
            payload = build_admin_workflow_graph(
                expectation,
                topology_nodes=topology_nodes,
                topology_edges=topology_edges,
                node_details=node_details,
            )
        except AdminWorkflowGraphError as issue:
            return self._workflow_graph_issue_response(issue)
        except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
            return self._workflow_graph_issue_response(
                AdminWorkflowGraphError("CORRUPT", http_status=503)
            )
        try:
            return _admin_json_response(
                payload,
                max_bytes=ADMIN_WORKFLOW_GRAPH_MAX_RESPONSE_BYTES,
            )
        except ValueError:
            return self._workflow_graph_issue_response(AdminWorkflowGraphError("LIMIT_EXCEEDED"))

    def workflow_topology_nodes_view(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Return one authorized bounded topology-node page."""
        return self._workflow_page_view(request, object_id, collection="topology_nodes")

    def workflow_topology_edges_view(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Return one authorized bounded topology-edge page."""
        return self._workflow_page_view(request, object_id, collection="topology_edges")

    def workflow_node_details_view(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Return one authorized bounded normalized-node page."""
        return self._workflow_page_view(request, object_id, collection="node_details")

    def _workflow_page_view(
        self,
        request: HttpRequest,
        object_id: str,
        *,
        collection: str,
    ) -> HttpResponse:
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        execution = self._authorized_workflow_read_execution(request, object_id)
        try:
            limit = self._page_limit(request)
            attempt_number = self._attempt_number(request)
        except ValueError:
            response = JsonResponse(
                {"code": "INVALID_ARGUMENT", "message": "page arguments are invalid"},
                status=400,
            )
            response["Cache-Control"] = "no-store"
            return response
        authorizer = self._workflow_authorizer(request)
        cursor = request.GET.get("cursor") or None
        try:
            if collection == "topology_nodes":
                payload = list_workflow_topology_nodes(
                    execution,
                    authorize=authorizer,
                    attempt_number=attempt_number,
                    cursor=cursor,
                    limit=limit,
                )
            elif collection == "topology_edges":
                payload = list_workflow_topology_edges(
                    execution,
                    authorize=authorizer,
                    attempt_number=attempt_number,
                    cursor=cursor,
                    limit=limit,
                )
            else:
                payload = list_workflow_node_details(
                    execution,
                    authorize=authorizer,
                    attempt_number=attempt_number,
                    state=request.GET.get("state") or None,
                    cursor=cursor,
                    limit=limit,
                )
        except WorkflowProgressReadError as error:
            return self._workflow_read_error_response(error)
        response = JsonResponse(payload)
        response["Cache-Control"] = "no-store"
        return response

    def workflow_node_detail_view(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> HttpResponse:
        """Return one authorized indexed durable node record."""
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        execution = self._authorized_workflow_read_execution(request, object_id)
        node_id = request.GET.get("node_id")
        if node_id is None:
            response = JsonResponse(
                {"code": "INVALID_ARGUMENT", "message": "node_id is required"},
                status=400,
            )
            response["Cache-Control"] = "no-store"
            return response
        try:
            attempt_number = self._attempt_number(request)
            payload = get_workflow_node_detail(
                execution,
                node_id,
                authorize=self._workflow_authorizer(request),
                attempt_number=attempt_number,
            )
        except ValueError:
            response = JsonResponse(
                {"code": "INVALID_ARGUMENT", "message": "attempt_number must be an integer"},
                status=400,
            )
            response["Cache-Control"] = "no-store"
            return response
        except WorkflowProgressReadError as error:
            return self._workflow_read_error_response(error)
        response = JsonResponse(payload)
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _redacted_json(value: str | None) -> str:
        """Render stored JSON with the same policy used by operational logs."""
        return _bounded_redacted_json(value)

    @admin.display(description="Arguments")
    def args_json_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_json_display(obj, "args_json")

    @admin.display(description="Keyword arguments")
    def kwargs_json_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_json_display(obj, "kwargs_json")

    @admin.display(description="Input reference")
    def input_reference_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(obj, "input_reference")

    @admin.display(description="Result")
    def result_data_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_json_display(obj, "result_data")

    @admin.display(description="Result reference")
    def result_reference_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(obj, "result_reference")

    @admin.display(description="Progress")
    def progress_data_display(self, obj: RayTaskExecution) -> str:
        return self._redacted_json(obj.progress_data)

    @admin.display(description="Completion envelope")
    def completion_data_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_json_display(obj, "completion_data")

    @admin.display(description="Error")
    def error_message_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(obj, "error_message")

    @admin.display(description="Cancellation error")
    def cancellation_error_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(obj, "cancellation_error")

    @admin.display(description="Traceback")
    def error_traceback_display(self, obj: RayTaskExecution) -> str:
        value, status = _bounded_admin_text_value(obj, "error_traceback")
        if status == "oversized":
            value = _ADMIN_DETAIL_OVERSIZED_MESSAGE
        elif status != "value":
            value = _ADMIN_DETAIL_UNAVAILABLE_MESSAGE
        return _bounded_traceback_display(value)

    @admin.display(description="Ray Job ID")
    def ray_job_id_display(self, obj: RayTaskExecution) -> str:
        """Display Ray Job ID with link to Ray Dashboard."""
        from django.conf import settings

        ray_job_id = obj.ray_job_id
        if not ray_job_id:
            return "Not yet submitted"

        job_id = str(ray_job_id)
        dashboard_url = getattr(settings, "RAY_DASHBOARD_URL", RAY_DASHBOARD_URL)

        # Old format: ray_core:pk
        if job_id.startswith("ray_core:"):
            return "N/A (legacy format)"

        # New format: job_id:task_id (e.g., "02000000:67a2e8cfa5...")
        if ":" in job_id and not job_id.startswith("raysubmit_"):
            parts = job_id.split(":", 1)
            ray_job = parts[0]
            ray_task = parts[1]
            url = f"{dashboard_url}/#/jobs/{ray_job}/tasks/{ray_task}"
            return format_html(
                'Job: {}, Task: {}... <a href="{}" target="_blank" '
                'rel="noopener noreferrer">[Open in Dashboard]</a>',
                ray_job,
                ray_task[:16],
                url,
            )

        # Ray Job API format
        url = f"{dashboard_url}/#/jobs/{job_id}"
        return format_html(
            '{} <a href="{}" target="_blank" rel="noopener noreferrer">[Open in Dashboard]</a>',
            job_id,
            url,
        )

    @admin.display(boolean=True, description="Compatible worker available")
    def protocol_compatible_worker_available_display(
        self,
        obj: RayTaskExecution,
    ) -> bool | None:
        """Show heartbeat-live protocol compatibility without implying queue capacity."""
        value = obj.__dict__.get("protocol_compatible_worker_available")
        return value if isinstance(value, bool) else None

    @admin.display(description="Created with django-ray")
    def created_with_django_ray_version_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(
            obj,
            "created_with_django_ray_version",
            max_chars=_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
            max_bytes=_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
            namespace="protocol",
        )

    @admin.display(description="Managed with django-ray")
    def managed_with_django_ray_version_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(
            obj,
            "managed_with_django_ray_version",
            max_chars=_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
            max_bytes=_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
            namespace="protocol",
        )

    @admin.display(description="Executed with django-ray")
    def executor_django_ray_version_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(
            obj,
            "executor_django_ray_version",
            max_chars=_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS,
            max_bytes=_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES,
            namespace="protocol",
        )

    @admin.display(description="State", ordering="state")
    def state_display(self, obj: RayTaskExecution) -> str:
        """Display state with color coding."""
        colors: dict[str, str] = {
            TaskState.QUEUED: "#6c757d",
            TaskState.RUNNING: "#007bff",
            TaskState.SUCCEEDED: "#28a745",
            TaskState.FAILED: "#dc3545",
            TaskState.CANCELLED: "#ffc107",
            TaskState.CANCELLING: "#ffc107",
            TaskState.LOST: "#dc3545",
            TaskState.EXPIRED: "#b45309",
        }
        state = str(obj.state)
        color = colors.get(state, "#6c757d")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            state,
        )

    @admin.display(description="Task", ordering="callable_path")
    def task_display(self, obj: RayTaskExecution) -> str:
        """Keep callable identity readable without letting it dominate the row."""
        callable_path = str(obj.callable_path)
        return format_html(
            '<span title="{}">{}</span>',
            callable_path,
            _compact_path_suffix(callable_path, max_chars=36),
        )

    @admin.display(description="Queue", ordering="queue_name")
    def queue_display(self, obj: RayTaskExecution) -> str:
        """Keep long queue names bounded while preserving the full hover label."""
        queue_name = str(obj.queue_name)
        return format_html(
            '<span title="{}">{}</span>',
            queue_name,
            Truncator(queue_name).chars(20),
        )

    @admin.display(description="Attempt", ordering="attempt_number")
    def attempt_display(self, obj: RayTaskExecution) -> int:
        return cast(int, obj.attempt_number)

    @admin.display(description="Created", ordering="created_at")
    def created_display(self, obj: RayTaskExecution) -> str:
        return _compact_admin_datetime(obj.created_at)

    @admin.display(description="Started", ordering="started_at")
    def started_display(self, obj: RayTaskExecution) -> str:
        return _compact_admin_datetime(obj.started_at)

    @admin.display(description="Finished", ordering="finished_at")
    def finished_display(self, obj: RayTaskExecution) -> str:
        return _compact_admin_datetime(obj.finished_at)

    @admin.display(description="Ray")
    def ray_dashboard_link(self, obj: RayTaskExecution) -> str:
        """Display link to Ray Dashboard for the job/task."""
        from django.conf import settings

        ray_job_id = obj.ray_job_id
        if not ray_job_id:
            return "-"

        job_id = str(ray_job_id)

        # Get dashboard URL from settings or use default
        dashboard_url = getattr(settings, "RAY_DASHBOARD_URL", RAY_DASHBOARD_URL)

        # Old format: ray_core:pk - no useful link
        if job_id.startswith("ray_core:"):
            url = f"{dashboard_url}/#/jobs"
            return format_html(
                '<a href="{}" target="_blank" rel="noopener noreferrer">Jobs</a>',
                url,
            )

        # New format: job_id:task_id (e.g., "02000000:67a2e8cfa5...")
        if ":" in job_id and not job_id.startswith("raysubmit_"):
            parts = job_id.split(":", 1)
            ray_job = parts[0]
            ray_task = parts[1]
            # Link directly to the task in the Ray Dashboard
            url = f"{dashboard_url}/#/jobs/{ray_job}/tasks/{ray_task}"
            return format_html(
                '<a href="{}" target="_blank" rel="noopener noreferrer">Task</a>',
                url,
            )

        # Ray Job API - link to the job
        url = f"{dashboard_url}/#/jobs/{job_id}"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">{}...</a>',
            url,
            job_id[:8],
        )

    @admin.action(description="Retry selected tasks...")
    def retry_tasks(
        self,
        request: HttpRequest,
        queryset: QuerySet,
    ) -> HttpResponse | None:
        """Confirm, fence, and retry failed, lost, or expired executions."""
        return self._retry_tasks_with_confirmation(request, queryset)

    def _retry_tasks_with_confirmation(
        self,
        request: HttpRequest,
        queryset: QuerySet,
        *,
        cancel_url: str | None = None,
    ) -> HttpResponse | None:
        """Share one confirmation and mutation path across list and detail controls."""
        if not self.has_change_permission(request):
            raise PermissionDenied
        tasks_to_retry = queryset.filter(state__in=ADMIN_RETRYABLE_STATES)
        if not tasks_to_retry.exists():
            self.message_user(
                request,
                "No failed, lost, or expired tasks found in selection.",
            )
            return

        snapshot = _admin_retry_snapshot(tasks_to_retry)
        if not snapshot:
            self.message_user(
                request,
                "No failed, lost, or expired tasks found in selection.",
            )
            return None
        if len(snapshot) > ADMIN_RETRY_CONFIRMATION_MAX_TASKS:
            self.message_user(
                request,
                (
                    "Retry at most "
                    f"{ADMIN_RETRY_CONFIRMATION_MAX_TASKS} failed, lost, or expired tasks "
                    "at once. No tasks were queued."
                ),
                level=messages.ERROR,
            )
            return None

        confirmation_token = request.POST.get("retry_confirmation_token", "")
        confirmed = request.POST.get("post") == "yes"
        actor_fingerprint = _admin_retry_actor_fingerprint(request)
        session_fingerprint = _admin_retry_session_fingerprint(
            request,
            create=not confirmed,
        )
        if not confirmed:
            if session_fingerprint is None:
                raise PermissionDenied
            selected_count = queryset.count()
            retryable_count = len(snapshot)
            if cancel_url is None:
                cancel_url = reverse(
                    f"{self.admin_site.name}:"
                    f"{self.opts.app_label}_{self.opts.model_name}_changelist"
                )
                changelist_query = request.GET.urlencode()
                if changelist_query:
                    cancel_url = f"{cancel_url}?{changelist_query}"
            context = {
                **self.admin_site.each_context(request),
                "title": "Confirm full task retry",
                "opts": self.model._meta,
                "media": self.media,
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
                "selected_ids": [row["pk"] for row in snapshot],
                "selected_count": selected_count,
                "retryable_count": retryable_count,
                "non_retryable_count": max(selected_count - retryable_count, 0),
                "known_workflow_count": sum(int(row["known_workflow"]) for row in snapshot),
                "confirmation_token": _admin_retry_confirmation_token(
                    snapshot,
                    actor_fingerprint=actor_fingerprint,
                    session_fingerprint=session_fingerprint,
                ),
                "confirmation_max_age_minutes": (ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS // 60),
                "cancel_url": cancel_url,
                # Kept for template extensions written against the original bulk flow.
                "changelist_url": cancel_url,
            }
            return TemplateResponse(
                request,
                self.retry_confirmation_template,
                context,
            )

        if (
            not confirmation_token
            or session_fingerprint is None
            or not _admin_retry_confirmation_matches(
                confirmation_token,
                snapshot,
                actor_fingerprint=actor_fingerprint,
                session_fingerprint=session_fingerprint,
            )
        ):
            self.message_user(
                request,
                (
                    "Retry confirmation expired or the selected tasks changed. "
                    "Review their current state and confirm again. No tasks were queued."
                ),
                level=messages.ERROR,
            )
            return None

        count = 0
        blocked = 0
        changed = 0
        unsupported = 0
        for row in snapshot:
            try:
                result = request_task_retry(
                    row["pk"],
                    allowed_states=(row["state"],),
                    expected_attempt_number=row["attempt_number"],
                    expected_execution_generation=row["execution_generation"],
                    expected_workflow_identity=(
                        row["workflow_run_id"],
                        row["workflow_plan_fingerprint"],
                    ),
                )
            except RuntimeEnvSnapshotError:
                blocked += 1
                continue
            count += int(result.accepted)
            unsupported += int(result.status is TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL)
            changed += int(
                not result.accepted
                and result.status is not TaskRetryRequestStatus.UNSUPPORTED_PROTOCOL
            )

        message = f"Queued {count} task(s) for retry."
        if changed:
            message += f" Skipped {changed} task(s) because they changed after confirmation."
        if blocked:
            message += (
                f" Skipped {blocked} task(s) because their persisted RuntimeEnv "
                "snapshots failed validation."
            )
        if unsupported:
            message += (
                f" Skipped {unsupported} task(s) because this django-ray build does not "
                "support their execution protocol."
            )
        self.message_user(
            request,
            message,
        )

    @admin.action(description="Cancel selected tasks")
    def cancel_tasks(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Request package-owned cancellation for each authorized selection."""
        accepted_count = 0
        unsupported_count = 0
        for task in queryset.only("pk", "attempt_number", "execution_generation"):
            if task.pk is None:  # pragma: no cover - querysets contain persisted rows
                continue
            result = request_task_cancellation(
                task.pk,
                expected_attempt_number=task.attempt_number,
                expected_execution_generation=task.execution_generation,
            )
            accepted_count += int(result.accepted)
            unsupported_count += int(
                result.status is TaskCancellationRequestStatus.UNSUPPORTED_PROTOCOL
            )
        if accepted_count:
            message = (
                f"Accepted cancellation for {accepted_count} task(s). "
                "Workers will attempt best-effort interruption for running work."
            )
        else:
            message = "No selected tasks accepted cancellation."
        if unsupported_count:
            message += (
                f" Skipped {unsupported_count} task(s) because this django-ray build does "
                "not support their execution protocol."
            )
        self.message_user(request, message)


@admin.register(TaskAttempt)
class TaskAttemptAdmin(DjangoRayModelAdmin):
    """Read-only historical attempt diagnostics."""

    change_form_template = "admin/django_ray/taskattempt/change_form.html"

    class Media:
        css = {"all": ("django_ray/admin/diagnostics.css",)}

    list_display = [
        "execution_link",
        "attempt_number",
        "execution_protocol_version",
        "state",
        "started_at",
        "finished_at",
    ]
    list_display_links = ("attempt_number",)
    list_filter = ["state", ExecutionProtocolFilter]
    fields = [
        "execution_link",
        "workflow_graph_link",
        "attempt_number",
        "execution_protocol_version",
        "managed_with_django_ray_version",
        "executor_django_ray_version",
        "state",
        "started_at",
        "finished_at",
        "error_message_display",
        "error_traceback_display",
        "result_data_display",
        "result_reference_display",
        "created_at",
    ]
    readonly_fields = fields

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add: bool = False,
        change: bool = False,
        form_url: str = "",
        obj: TaskAttempt | None = None,
    ) -> HttpResponse:
        """Offer the explicit sensitive-data view only to authorized readers."""
        context["django_ray_admin_uses_unfold"] = _DJANGO_RAY_ADMIN_USES_UNFOLD
        context["show_history"] = False
        context["django_ray_sensitive_data_url"] = None
        if obj is not None and self.has_view_permission(request, obj):
            database = str(getattr(obj._state, "db", None) or "default")
            try:
                execution = (
                    RayTaskExecution.objects.using(database).only("pk").get(pk=obj.execution_id)
                )
            except RayTaskExecution.DoesNotExist:
                execution = None
            if execution is not None and _has_sensitive_task_data_permission(
                request,
                execution,
            ):
                opts = self.model._meta
                context["django_ray_sensitive_data_url"] = reverse(
                    f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_sensitive_data",
                    args=[quote(str(obj.pk))],
                    current_app=self.admin_site.name,
                )
        return super().render_change_form(
            request,
            context,
            add=add,
            change=change,
            form_url=form_url,
            obj=obj,
        )

    def get_urls(self) -> list[Any]:
        opts = self.model._meta
        custom_urls = [
            path(
                "<path:object_id>/history/",
                self.admin_site.admin_view(_readonly_admin_removed_object_route),
            ),
            path(
                "<path:object_id>/delete/",
                self.admin_site.admin_view(_readonly_admin_removed_object_route),
            ),
            path(
                "<path:object_id>/sensitive-data/",
                self.admin_site.admin_view(self.sensitive_data_view),
                name=f"{opts.app_label}_{opts.model_name}_sensitive_data",
            ),
        ]
        return custom_urls + _readonly_admin_urls(super().get_urls(), opts=opts)

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Render one cache-disabled attempt detail within a fixed response bound."""
        method_response = _readonly_admin_change_method_response(request)
        if method_response is not None:
            return method_response
        request.__dict__["_django_ray_bounded_attempt_detail"] = True
        try:
            response = super().change_view(request, object_id, form_url, extra_context)
            return _bounded_admin_detail_response(
                request,
                response,
                admin_site=self.admin_site,
                opts=self.opts,
                max_bytes=ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES,
            )
        finally:
            request.__dict__.pop("_django_ray_bounded_attempt_detail", None)

    def sensitive_data_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Render exact inputs plus the selected archived attempt's outcome."""
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        try:
            attempt_id = self.model._meta.pk.to_python(unquote(object_id))
            attempt = (
                self.get_queryset(request)
                .only("pk", "execution_id", "attempt_number", "state")
                .get(pk=attempt_id)
            )
        except (TaskAttempt.DoesNotExist, ValidationError, ValueError) as error:
            raise Http404("Task attempt was not found") from error
        if not self.has_view_permission(request, attempt):
            raise PermissionDenied
        database = str(getattr(attempt._state, "db", None) or "default")
        try:
            execution = (
                RayTaskExecution.objects.using(database)
                .only(
                    "pk",
                    "task_id",
                    "callable_path",
                )
                .get(pk=attempt.execution_id)
            )
        except RayTaskExecution.DoesNotExist as error:
            raise Http404("Owning Ray task execution was not found") from error
        if not _has_sensitive_task_data_permission(request, execution):
            raise PermissionDenied

        input_specs = (_ADMIN_SENSITIVE_EXECUTION_SECTIONS[0],)
        input_fields = tuple(field_name for _, field_name in input_specs[0][1])
        outcome_specs = (_ADMIN_SENSITIVE_ATTEMPT_OUTCOME_SECTION,)
        outcome_fields = tuple(field_name for _, field_name in outcome_specs[0][1])
        try:
            fresh_execution, input_row = _bounded_sensitive_object(
                RayTaskExecution.objects.using(database),
                pk=execution.pk,
                field_names=input_fields,
                identity_fields=("pk", "task_id", "callable_path"),
            )
            fresh_attempt, outcome_row = _bounded_sensitive_object(
                self.get_queryset(request).using(database),
                pk=attempt.pk,
                field_names=outcome_fields,
                identity_fields=(
                    "pk",
                    "execution_id",
                    "attempt_number",
                    "state",
                ),
            )
        except (RayTaskExecution.DoesNotExist, TaskAttempt.DoesNotExist) as error:
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
        if not _has_sensitive_task_data_permission(request, fresh_execution):
            raise PermissionDenied
        if not self.has_view_permission(request, fresh_attempt):
            raise PermissionDenied
        execution = fresh_execution
        attempt = fresh_attempt
        row = {**input_row, **outcome_row}
        sections = _sensitive_sections(row, input_specs + outcome_specs)
        back_url = reverse(
            f"{self.admin_site.name}:{self.opts.app_label}_{self.opts.model_name}_change",
            args=[quote(str(attempt.pk))],
            current_app=self.admin_site.name,
        )
        return _sensitive_diagnostics_response(
            request,
            admin_site=self.admin_site,
            opts=self.opts,
            title="Unredacted attempt data",
            subject=(
                f"Execution {execution.task_id} — {execution.callable_path}; "
                f"attempt {attempt.attempt_number} ({attempt.state})"
            ),
            back_url=back_url,
            sections=sections,
        )

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        """Use allowlisted and SQL-guarded projections for routine Admin reads."""
        queryset = super().get_queryset(request).defer("workflow_progress_summary_json")
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
                        workflow_progress_summary_json__contains=(_FULL_WORKFLOW_ATTEMPT_MARKER),
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
            queryset = _annotate_bounded_admin_text(
                queryset.only(*_ADMIN_ATTEMPT_DETAIL_FIELDS),
                field_names=_ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS,
                max_bytes=ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
                max_chars=ADMIN_DIAGNOSTIC_MAX_CHARS,
                namespace="detail",
            )
        return queryset

    def has_module_permission(self, request: HttpRequest) -> bool:
        """Hide standalone navigation when contextual history is the default."""
        return _task_attempt_admin_mode() in {
            "standalone",
            "both",
        } and super().has_module_permission(request)

    def has_view_permission(
        self,
        request: HttpRequest,
        obj: TaskAttempt | None = None,
    ) -> bool:
        """Honor global grants and object-permission backends on detail reads."""
        if super().has_view_permission(request, obj):
            return True
        if obj is None:
            return False
        opts = self.opts
        return request.user.has_perm(
            f"{opts.app_label}.{get_permission_codename('view', opts)}",
            obj,
        ) or request.user.has_perm(
            f"{opts.app_label}.{get_permission_codename('change', opts)}",
            obj,
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: TaskAttempt | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: TaskAttempt | None = None,
    ) -> bool:
        return False

    @admin.display(description="Execution", ordering="execution_id")
    def execution_link(self, obj: TaskAttempt) -> str:
        opts = RayTaskExecution._meta
        url = reverse(
            f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_change",
            args=[quote(str(obj.execution_id))],
            current_app=self.admin_site.name,
        )
        return format_html('<a href="{}">{}</a>', url, obj.execution_id)

    @admin.display(description="Workflow graph")
    def workflow_graph_link(self, obj: TaskAttempt) -> str:
        """Link a full-reporting archive to the parent graph pinned to this attempt."""
        has_graph = getattr(obj, "admin_has_workflow_graph", None)
        if has_graph is None:
            summary = obj.__dict__.get("workflow_progress_summary_json")
            has_graph = isinstance(summary, str) and _FULL_WORKFLOW_ATTEMPT_MARKER in summary
        if not has_graph:
            return "-"
        opts = RayTaskExecution._meta
        url = reverse(
            f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_workflow_graph",
            args=[quote(str(obj.execution_id))],
            current_app=self.admin_site.name,
        )
        return format_html(
            '<a href="{}?attempt_number={}">Open graph for attempt #{}</a>',
            url,
            obj.attempt_number,
            obj.attempt_number,
        )

    @admin.display(description="Error")
    def error_message_display(self, obj: TaskAttempt) -> str:
        return _ordinary_admin_text_display(obj, "error_message")

    @admin.display(description="Traceback")
    def error_traceback_display(self, obj: TaskAttempt) -> str:
        value, status = _bounded_admin_text_value(obj, "error_traceback")
        if status == "oversized":
            value = _ADMIN_DETAIL_OVERSIZED_MESSAGE
        elif status != "value":
            value = _ADMIN_DETAIL_UNAVAILABLE_MESSAGE
        return _bounded_traceback_display(value)

    @admin.display(description="Result")
    def result_data_display(self, obj: TaskAttempt) -> str:
        return _ordinary_admin_json_display(obj, "result_data")

    @admin.display(description="Result reference")
    def result_reference_display(self, obj: TaskAttempt) -> str:
        return _ordinary_admin_text_display(obj, "result_reference")


class ActiveWorkerFilter(admin.SimpleListFilter):
    """Filter to show active/inactive workers with active as default."""

    title = "status"
    parameter_name = "is_active"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:
        return [
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("all", "All"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        if self.value() == "inactive":
            return queryset.filter(is_active=False)
        elif self.value() == "all":
            return queryset
        else:
            # Default: show only active
            return queryset.filter(is_active=True)

    def choices(self, changelist: Any) -> Any:
        """Override to set default selection."""
        for lookup, title in self.lookup_choices:
            yield {
                "selected": self.value() == lookup or (self.value() is None and lookup == "active"),
                "query_string": changelist.get_query_string({self.parameter_name: lookup}),
                "display": title,
            }


@admin.register(TaskWorkerLease)
class TaskWorkerLeaseAdmin(DjangoRayModelAdmin):
    """Admin for TaskWorkerLease model.

    Note: This tracks Django task workers (django_ray_worker command),
    NOT Ray cluster workers. The Django workers claim tasks from the
    database and submit them to Ray for execution.

    By default, only active workers are shown. Use the filter to see inactive workers.
    """

    list_display = [
        "worker_id_short",
        "hostname",
        "pid",
        "queue_name",
        "capability_schema_version",
        "supported_execution_protocols",
        "django_ray_version",
        "started_at",
        "last_heartbeat_at",
        "is_active_display_list",
        "time_since_heartbeat",
    ]
    list_filter = [
        ActiveWorkerFilter,
        "queue_name",
        "hostname",
    ]
    search_fields = [
        "worker_id",
        "hostname",
    ]
    readonly_fields = [
        "worker_id",
        "hostname",
        "pid",
        "queue_name",
        "capability_schema_version",
        "django_ray_version",
        "min_supported_execution_protocol_version",
        "max_supported_execution_protocol_version",
        "legacy_admission_token",
        "started_at",
        "last_heartbeat_at",
        "stopped_at",
        "is_active",
    ]
    fieldsets = (
        (
            "Worker Identification",
            {
                "fields": ("worker_id", "hostname", "pid"),
            },
        ),
        (
            "Configuration",
            {
                "fields": ("queue_name",),
                "description": "Note: Changing the queue here does NOT affect the worker. "
                "The queue is set when the worker starts via --queue argument.",
            },
        ),
        (
            "Execution Protocol Capability",
            {
                "fields": (
                    "capability_schema_version",
                    "django_ray_version",
                    "min_supported_execution_protocol_version",
                    "max_supported_execution_protocol_version",
                    "legacy_admission_token",
                ),
                "description": (
                    "Protocol ranges control compatibility. The package version is "
                    "diagnostic only; a legacy admission token marks an unaware worker."
                ),
            },
        ),
        (
            "Status",
            {
                "fields": ("is_active",),
            },
        ),
        (
            "Timing",
            {
                "fields": ("started_at", "last_heartbeat_at", "stopped_at"),
            },
        ),
    )
    actions = ["mark_inactive", "delete_inactive"]

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        """Default queryset - filter applied via ActiveWorkerFilter."""
        return super().get_queryset(request)

    @admin.display(description="Worker ID")
    def worker_id_short(self, obj: TaskWorkerLease) -> str:
        """Show shortened worker ID."""
        worker_id = str(obj.worker_id)
        return f"{worker_id[:12]}..."

    @admin.display(description="Execution protocols")
    def supported_execution_protocols(self, obj: TaskWorkerLease) -> str:
        """Render the bounded capability range without treating SemVer as policy."""
        minimum = obj.min_supported_execution_protocol_version
        maximum = obj.max_supported_execution_protocol_version
        if minimum is None or maximum is None:
            return "Legacy (policy-controlled)"
        if minimum == maximum:
            return str(minimum)
        return f"{minimum}-{maximum}"

    @admin.display(boolean=True, description="Active")
    def is_active_display_list(self, obj: TaskWorkerLease) -> bool:
        """Display active status as boolean icon in list view."""
        return bool(obj.is_active) and not self._is_heartbeat_expired(obj)

    @staticmethod
    def _is_heartbeat_expired(obj: TaskWorkerLease) -> bool:
        """Check if heartbeat has expired."""
        from django_ray.runner.leasing import is_lease_expired

        return is_lease_expired(obj)

    @admin.display(description="Time Since Heartbeat")
    def time_since_heartbeat(self, obj: TaskWorkerLease) -> str:
        """Show time since last heartbeat."""
        if not obj.last_heartbeat_at:
            return "Never"
        delta = timezone.now() - obj.last_heartbeat_at
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        elif seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s ago"
        else:
            return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"

    @admin.action(
        description="Mark selected as inactive",
        permissions=["manage_worker_leases"],
    )
    def mark_inactive(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Mark selected worker leases as inactive."""
        active = queryset.filter(is_active=True)
        with transaction.atomic():
            if connection.features.has_select_for_update:
                # Match recovery's global lease lock order so an Admin bulk
                # action cannot deadlock with a worker adopting the same rows.
                worker_ids = list(
                    active.select_for_update()
                    .order_by("worker_id")
                    .values_list("worker_id", flat=True)
                )
                count = TaskWorkerLease.objects.filter(
                    worker_id__in=worker_ids,
                    is_active=True,
                ).update(
                    is_active=False,
                    stopped_at=timezone.now(),
                )
            else:
                # SQLite's write transaction is already database-wide.
                count = active.update(
                    is_active=False,
                    stopped_at=timezone.now(),
                )

        if count > 0:
            self.message_user(
                request,
                f"Marked {count} worker lease(s) as inactive.",
            )
        else:
            self.message_user(
                request,
                "No active leases found in selection.",
            )

    @admin.action(
        description="Delete inactive worker leases",
        permissions=["manage_worker_leases"],
    )
    def delete_inactive(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Delete inactive worker leases from selected."""
        inactive = queryset.filter(is_active=False)
        with transaction.atomic():
            if connection.features.has_select_for_update:
                # Recovery can lock one inactive source plus the current
                # worker lease. Lock every deletion target in the same global
                # order before issuing the bulk delete.
                worker_ids = list(
                    inactive.select_for_update()
                    .order_by("worker_id")
                    .values_list("worker_id", flat=True)
                )
                _total_deleted, deleted_by_model = TaskWorkerLease.objects.filter(
                    worker_id__in=worker_ids,
                    is_active=False,
                ).delete()
            else:
                # SQLite's write transaction is already database-wide.
                _total_deleted, deleted_by_model = inactive.delete()

            deleted_count = deleted_by_model.get(TaskWorkerLease._meta.label, 0)

        if deleted_count > 0:
            self.message_user(
                request,
                f"Deleted {deleted_count} inactive worker lease(s).",
            )
        else:
            self.message_user(
                request,
                "No inactive leases found in selection.",
            )

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Disable adding leases manually - workers create their own."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Disable editing leases - they are managed by workers."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        """Disable unfenced generic deletion; use ``delete_inactive`` instead."""
        return False

    def has_manage_worker_leases_permission(self, request: HttpRequest) -> bool:
        """Require the model change permission for controlled lease actions."""
        codename = get_permission_codename("change", self.opts)
        return bool(request.user.has_perm(f"{self.opts.app_label}.{codename}"))


@admin.register(TaskExecutionProtocolPolicy)
class TaskExecutionProtocolPolicyAdmin(DjangoRayModelAdmin):
    """Read-only view of the singleton execution-protocol rollout policy."""

    list_display = [
        "active_write_protocol_version",
        "legacy_worker_admission_enabled",
        "revision",
        "updated_at",
    ]
    fields = [
        "singleton_key",
        "schema_version",
        "active_write_protocol_version",
        "legacy_worker_admission_enabled",
        "revision",
        "updated_at",
    ]
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: TaskExecutionProtocolPolicy | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: TaskExecutionProtocolPolicy | None = None,
    ) -> bool:
        return False
