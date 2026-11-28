"""WCAG AA status badges, display formatters, dashboard links, and admin filters."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from urllib.parse import quote, urlsplit

from django.contrib import admin
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.text import Truncator

from django_ray.conf.settings import get_settings
from django_ray.execution_protocol import EXECUTION_PROTOCOL_VERSION
from django_ray.models import RayTaskExecution, TaskState
from django_ray.redaction import redact_text, safe_json_dumps

DASHBOARD_NOT_CONFIGURED = "Dashboard unavailable: configure RAY_DASHBOARD_URL."
ADMIN_DIAGNOSTIC_MAX_CHARS = 4096
ADMIN_ATTEMPT_INLINE_MAX_CHARS = 512
ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES = ADMIN_DIAGNOSTIC_MAX_CHARS * 4
ADMIN_ATTEMPT_INLINE_MAX_BYTES = ADMIN_ATTEMPT_INLINE_MAX_CHARS * 4
ADMIN_ATTEMPT_INLINE_MAX_ROWS = 25
OVERSIZE_RESULT_REFERENCE_PREFIX = "oversize://"
DIGEST_ONLY_STUB_LABEL = "Digest-only stub (>1 MiB; configure RESULT_STORAGE_BACKEND)"
FULL_WORKFLOW_ATTEMPT_MARKER = '"reporting_policy":"full"'
ADMIN_DETAIL_OVERSIZED_MESSAGE = (
    "Stored diagnostic omitted because it exceeds the ordinary Admin read limit."
)
ADMIN_DETAIL_UNAVAILABLE_MESSAGE = (
    "Stored diagnostic unavailable because its bounded read could not be validated."
)
ADMIN_DETAIL_INVALID_JSON_MESSAGE = (
    "Stored JSON omitted because it could not be parsed and redacted safely."
)
STATE_BADGE_SLUGS: dict[str, str] = {
    str(TaskState.QUEUED): "queued",
    str(TaskState.RUNNING): "running",
    str(TaskState.SUCCEEDED): "succeeded",
    str(TaskState.FAILED): "failed",
    str(TaskState.CANCELLED): "cancelled",
    str(TaskState.CANCELLING): "cancelling",
    str(TaskState.LOST): "lost",
    str(TaskState.EXPIRED): "expired",
}


def ray_dashboard_url() -> str | None:
    """Return an explicitly configured browser-facing HTTP(S) base URL."""
    from django.conf import settings

    value = getattr(settings, "RAY_DASHBOARD_URL", None)
    if value is None:
        configured = getattr(settings, "DJANGO_RAY", None)
        if isinstance(configured, dict):
            value = configured.get("RAY_DASHBOARD_URL")
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        # Accessing port also validates malformed and out-of-range ports.
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in value
    ):
        return None
    return value.rstrip("/")


def bounded_redacted_text(value: Any, *, max_chars: int = ADMIN_DIAGNOSTIC_MAX_CHARS) -> str:
    """Return redacted operator text with a hard display-size limit."""
    if value in (None, ""):
        return "-"
    return Truncator(redact_text(value)).chars(max_chars, truncate="... [truncated]")


def state_badge_html(state: Any) -> str:
    """Render a CSP-safe WCAG AA status pill for a durable task state."""
    state_text = str(state)
    slug = STATE_BADGE_SLUGS.get(state_text, "unknown")
    return format_html(
        '<span class="djray-badge djray-badge--{}" data-state="{}">{}</span>',
        slug,
        state_text,
        state_text,
    )


def bounded_traceback_display(value: Any) -> str:
    """Render inert traceback text inside a monospace bounded container."""
    rendered = bounded_redacted_text(value)
    if rendered == "-":
        return rendered
    return format_html(
        '<pre class="django-ray-diagnostic django-ray-diagnostic--traceback">{}</pre>',
        rendered,
    )


def bounded_redacted_json(value: str | None, *, pretty: bool = False) -> str:
    """Return bounded, redacted JSON without exposing malformed raw payloads."""
    if not value:
        return "-"
    try:
        redacted = safe_json_dumps(json.loads(value))
        rendered = (
            json.dumps(json.loads(redacted), indent=2, ensure_ascii=False) if pretty else redacted
        )
    except (RecursionError, TypeError, UnicodeError, ValueError):
        return ADMIN_DETAIL_INVALID_JSON_MESSAGE
    return Truncator(rendered).chars(
        ADMIN_DIAGNOSTIC_MAX_CHARS,
        truncate="... [truncated]",
    )


def bounded_json_html(value: str | None, *, pretty: bool = False) -> str:
    """Render bounded redacted JSON in a monospace container."""
    rendered = bounded_redacted_json(value, pretty=pretty)
    if rendered in {"-", ADMIN_DETAIL_INVALID_JSON_MESSAGE}:
        return rendered
    return format_html(
        '<pre class="django-ray-diagnostic django-ray-diagnostic--json">{}</pre>',
        rendered,
    )


def compact_admin_datetime(value: datetime | None) -> str:
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


def compact_path_suffix(value: str, *, max_chars: int) -> str:
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


def task_attempt_admin_mode() -> str:
    """Return the validated request-time attempt presentation mode."""
    return cast(str, get_settings()["TASK_ATTEMPT_ADMIN_MODE"])


def format_task_display(obj: Any) -> str:
    """Keep callable identity readable without letting it dominate the row."""
    callable_path = str(obj.callable_path)
    return format_html(
        '<span title="{}">{}</span>',
        callable_path,
        compact_path_suffix(callable_path, max_chars=36),
    )


def format_queue_display(obj: Any) -> str:
    """Keep long queue names bounded while preserving the full hover label."""
    queue_name = str(obj.queue_name)
    return format_html(
        '<span title="{}">{}</span>',
        queue_name,
        Truncator(queue_name).chars(20),
    )


def format_ray_job_id_display(obj: Any) -> str:
    """Display Ray Job ID with link to Ray Dashboard."""
    ray_job_id = obj.ray_job_id
    if not ray_job_id:
        return "Not yet submitted"

    job_id = str(ray_job_id)
    dashboard_url = ray_dashboard_url()
    if dashboard_url is None:
        return format_html("{} ({})", job_id, DASHBOARD_NOT_CONFIGURED)

    # Old format: ray_core:pk
    if job_id.startswith("ray_core:"):
        return "N/A (legacy format)"

    # New format: job_id:task_id (e.g., "02000000:67a2e8cfa5...")
    if ":" in job_id and not job_id.startswith("raysubmit_"):
        parts = job_id.split(":", 1)
        ray_job = parts[0]
        ray_task = parts[1]
        url = f"{dashboard_url}/#/jobs/{quote(ray_job, safe='')}/tasks/{quote(ray_task, safe='')}"
        return format_html(
            'Job: {}, Task: {}... <a href="{}" target="_blank" '
            'rel="noopener noreferrer">[Open in Dashboard]</a>',
            ray_job,
            ray_task[:16],
            url,
        )

    # Ray Job API format
    url = f"{dashboard_url}/#/jobs/{quote(job_id, safe='')}"
    return format_html(
        '{} <a href="{}" target="_blank" rel="noopener noreferrer">[Open in Dashboard]</a>',
        job_id,
        url,
    )


def format_ray_dashboard_link(obj: Any) -> str:
    """Display link to Ray Dashboard for the job/task."""
    ray_job_id = obj.ray_job_id
    if not ray_job_id:
        return "-"

    job_id = str(ray_job_id)

    dashboard_url = ray_dashboard_url()
    if dashboard_url is None:
        return format_html(
            '<span class="djray-muted-hint" title="{}">—</span>',
            DASHBOARD_NOT_CONFIGURED,
        )

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
        url = f"{dashboard_url}/#/jobs/{quote(ray_job, safe='')}/tasks/{quote(ray_task, safe='')}"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">Task</a>',
            url,
        )

    # Ray Job API - link to the job
    url = f"{dashboard_url}/#/jobs/{quote(job_id, safe='')}"
    return format_html(
        '<a href="{}" target="_blank" rel="noopener noreferrer">{}...</a>',
        url,
        job_id[:8],
    )


def format_attempt_detail_link(admin_site: admin.AdminSite, obj: Any) -> str:
    """Link the compact attempt label to its immutable detail view."""
    label = f"#{obj.attempt_number}"
    if obj.pk is None:
        return label
    url = reverse(
        f"{admin_site.name}:django_ray_taskattempt_change",
        args=[quote(str(obj.pk))],
        current_app=admin_site.name,
    )
    return format_html('<a href="{}">{}</a>', url, label)


def format_execution_link(admin_site: admin.AdminSite, obj: Any) -> str:
    """Link an attempt row back to its owning execution change page."""
    opts = cast(Any, RayTaskExecution)._meta
    url = reverse(
        f"{admin_site.name}:{opts.app_label}_{opts.model_name}_change",
        args=[quote(str(obj.execution_id))],
        current_app=admin_site.name,
    )
    return format_html('<a href="{}">{}</a>', url, obj.execution_id)


def format_workflow_graph_link(admin_site: admin.AdminSite, obj: Any) -> str:
    """Link a full-reporting archive to the in-Admin execution graph and JSON."""
    has_graph = getattr(obj, "admin_has_workflow_graph", None)
    if has_graph is None:
        summary = obj.__dict__.get("workflow_progress_summary_json")
        has_graph = isinstance(summary, str) and FULL_WORKFLOW_ATTEMPT_MARKER in summary
    if not has_graph:
        return "-"
    opts = cast(Any, RayTaskExecution)._meta
    change_url = reverse(
        f"{admin_site.name}:{opts.app_label}_{opts.model_name}_change",
        args=[quote(str(obj.execution_id))],
        current_app=admin_site.name,
    )
    json_url = reverse(
        f"{admin_site.name}:{opts.app_label}_{opts.model_name}_workflow_graph",
        args=[quote(str(obj.execution_id))],
        current_app=admin_site.name,
    )
    return format_html(
        '<span class="djray-workflow-graph-links">'
        '<a href="{}#workflow-graph-attempt-{}">Open graph for attempt #{}</a>'
        '<span class="djray-link-sep" aria-hidden="true">·</span>'
        '<a href="{}?attempt_number={}" target="_blank" '
        'rel="noopener noreferrer">Graph JSON</a>'
        "</span>",
        change_url,
        obj.attempt_number,
        obj.attempt_number,
        json_url,
        obj.attempt_number,
    )


def format_worker_id_short(obj: Any) -> str:
    """Show shortened worker ID with full identifier tooltip."""
    worker_id = str(obj.worker_id)
    short = f"{worker_id[:12]}..." if len(worker_id) > 12 else worker_id
    return format_html('<span title="{}">{}</span>', worker_id, short)


def format_supported_execution_protocols(obj: Any) -> str:
    """Render the bounded capability range without treating SemVer as policy."""
    minimum = obj.min_supported_execution_protocol_version
    maximum = obj.max_supported_execution_protocol_version
    if minimum is None or maximum is None:
        return "Legacy (policy-controlled)"
    if minimum == maximum:
        return str(minimum)
    return f"{minimum}-{maximum}"


def format_time_since_heartbeat(obj: Any) -> str:
    """Show time since last heartbeat."""
    last_heartbeat = obj.last_heartbeat_at
    if not last_heartbeat:
        return "Never"
    delta = timezone.now() - last_heartbeat
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s ago"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"


class ExecutionProtocolFilter(admin.SimpleListFilter):
    """Filter current vs non-current protocols without a distinct-value table scan."""

    title = "execution protocol"
    parameter_name = "execution_protocol_version"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin
    ) -> tuple[tuple[str, str], ...]:
        del request, model_admin
        value = str(EXECUTION_PROTOCOL_VERSION)
        return (
            (value, f"Protocol {value}"),
            ("other", f"Other (non-v{value})"),
        )

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        del request
        if self.value() == str(EXECUTION_PROTOCOL_VERSION):
            return queryset.filter(execution_protocol_version=EXECUTION_PROTOCOL_VERSION)
        if self.value() == "other":
            return queryset.exclude(execution_protocol_version=EXECUTION_PROTOCOL_VERSION)
        return queryset


class HasWorkflowFilter(admin.SimpleListFilter):
    """Filter workflow executions vs plain single-callable tasks."""

    title = "workflow"
    parameter_name = "has_workflow"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin
    ) -> tuple[tuple[str, str], ...]:
        del request, model_admin
        return (
            ("yes", "Workflow tasks"),
            ("no", "Plain tasks"),
        )

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        del request
        workflow_q = Q(workflow_run_id__isnull=False) | (
            Q(workflow_plan_fingerprint__isnull=False) & ~Q(workflow_plan_fingerprint="")
        )
        if self.value() == "yes":
            return queryset.filter(workflow_q)
        if self.value() == "no":
            return queryset.exclude(workflow_q)
        return queryset


class ActiveWorkerFilter(admin.SimpleListFilter):
    """Filter to show active/inactive workers with active as default."""

    title = "status"
    parameter_name = "is_active"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:
        del request, model_admin
        return [
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("all", "All"),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        del request
        if self.value() == "inactive":
            return queryset.filter(is_active=False)
        if self.value() == "all":
            return queryset
        # Default: show only active
        return queryset.filter(is_active=True)

    def choices(self, changelist: Any) -> Any:
        """Override to set default selection."""
        for lookup, title in cast(Any, self).lookup_choices:
            yield {
                "selected": self.value() == lookup or (self.value() is None and lookup == "active"),
                "query_string": changelist.get_query_string({self.parameter_name: lookup}),
                "display": title,
            }
