"""Django admin configuration for django-ray."""

import json
from collections.abc import Callable
from typing import Any, cast

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_permission_codename
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import QuerySet
from django.db.models.functions import Substr
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import quote, unquote
from django.utils.text import Truncator

from django_ray.conf.settings import get_settings
from django_ray.lifecycle import request_task_cancellation, retry_task
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.redaction import redact_text, safe_json_dumps
from django_ray.workflow_progress_reads import (
    WorkflowProgressReadError,
    WorkflowProgressReadErrorCode,
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)

if apps.is_installed("unfold"):
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


def _bounded_redacted_text(value: Any, *, max_chars: int = ADMIN_DIAGNOSTIC_MAX_CHARS) -> str:
    """Return redacted operator text with a hard display-size limit."""
    if value in (None, ""):
        return "-"
    return Truncator(redact_text(value)).chars(max_chars, truncate="... [truncated]")


def _bounded_redacted_json(value: str | None) -> str:
    """Return bounded, redacted JSON without exposing malformed raw payloads."""
    if not value:
        return "-"
    try:
        rendered = safe_json_dumps(json.loads(value))
    except (TypeError, json.JSONDecodeError):
        rendered = redact_text(value)
    return Truncator(rendered).chars(
        ADMIN_DIAGNOSTIC_MAX_CHARS,
        truncate="... [truncated]",
    )


def _task_attempt_admin_mode() -> str:
    """Return the validated request-time attempt presentation mode."""
    return cast(str, get_settings()["TASK_ATTEMPT_ADMIN_MODE"])


class TaskAttemptInline(DjangoRayTabularInline):
    """Compact immutable attempt history on an execution change page."""

    model = TaskAttempt
    fk_name = "execution"
    fields = (
        "attempt_number",
        "state",
        "started_at",
        "finished_at",
        "error_summary",
    )
    readonly_fields = fields
    ordering = ("attempt_number",)
    extra = 0
    can_delete = False
    show_change_link = True
    verbose_name_plural = "Attempt history"

    def get_queryset(self, request: HttpRequest) -> QuerySet[TaskAttempt]:
        """Keep large attempt diagnostics out of the contextual history query."""
        return (
            super()
            .get_queryset(request)
            .annotate(
                admin_error_preview=Substr(
                    "error_message",
                    1,
                    ADMIN_ATTEMPT_INLINE_MAX_CHARS + 1,
                )
            )
            .defer(
                "error_message",
                "error_traceback",
                "result_data",
                "result_reference",
                "workflow_progress_summary_json",
            )
            .order_by("attempt_number")
        )

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

    @admin.display(description="Error")
    def error_summary(self, obj: TaskAttempt) -> str:
        preview = getattr(obj, "admin_error_preview", None)
        if not hasattr(obj, "admin_error_preview"):
            preview = obj.error_message
        if preview not in (None, "") and len(preview) > ADMIN_ATTEMPT_INLINE_MAX_CHARS:
            return "Open the attempt detail to view bounded diagnostics."
        return _bounded_redacted_text(
            preview,
            max_chars=ADMIN_ATTEMPT_INLINE_MAX_CHARS,
        )


@admin.register(RayTaskExecution)
class RayTaskExecutionAdmin(DjangoRayModelAdmin):
    """Admin for RayTaskExecution model."""

    change_form_template = "admin/django_ray/raytaskexecution/change_form.html"
    observability_fields = (
        "pk",
        "task_id",
        "callable_path",
        "queue_name",
        "priority",
        "state",
        "attempt_number",
        "execution_generation",
        "workflow_run_id",
        "created_at",
        "run_after",
        "started_at",
        "finished_at",
        "last_heartbeat_at",
        "claimed_by_worker",
        "ray_job_id",
        "runtime_env_profile",
        "runtime_env_hash",
        "workflow_plan_fingerprint",
        "workflow_plan_pinned_attempt",
        "workflow_plan_selection",
        "error_message",
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
        "callable_path",
        "state_display",
        "queue_name",
        "priority",
        "attempt_number",
        "execution_generation",
        "workflow_run_id",
        "workflow_plan_fingerprint",
        "workflow_plan_pinned_attempt",
        "ray_dashboard_link",
        "created_at",
        "started_at",
        "finished_at",
    ]
    list_filter = [
        "state",
        "queue_name",
        "priority",
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
        "ray_job_id_display",
        "ray_target_address",
        "ray_address",
        "runtime_env_profile",
        "runtime_env_json",
        "runtime_env_hash",
        "workflow_run_id",
        "workflow_plan_fingerprint",
        "workflow_plan_pinned_attempt",
        "workflow_plan_display",
        "workflow_plan_selection_display",
        "created_at",
        "started_at",
        "finished_at",
        "last_heartbeat_at",
        "args_json_display",
        "kwargs_json_display",
        "input_reference",
        "result_data_display",
        "result_reference",
        "completion_data_display",
        "cancellation_status",
        "cancellation_error",
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
                    "workflow_plan_display",
                    "workflow_plan_selection_display",
                ),
            },
        ),
        (
            "Arguments",
            {
                "fields": ("args_json_display", "kwargs_json_display", "input_reference"),
                "classes": ("collapse",),
            },
        ),
        (
            "Result",
            {
                "fields": (
                    "result_data_display",
                    "result_reference",
                    "completion_data_display",
                    "cancellation_status",
                    "cancellation_error",
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
                    "runtime_env_json",
                ),
                "description": "Ray Job ID is only available for Ray Job API mode.",
            },
        ),
        (
            "Timing",
            {
                "fields": ("created_at", "started_at", "finished_at", "last_heartbeat_at"),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[RayTaskExecution]:
        """Keep complete workflow payloads out of routine Admin reads."""
        return (
            super()
            .get_queryset(request)
            .defer(
                "progress_data",
                "workflow_progress_summary_json",
            )
        )

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

    ordering = ["-created_at"]
    actions = ["retry_tasks", "cancel_tasks"]

    def get_urls(self) -> list[Any]:
        """Add the authenticated durable-summary endpoint used by the change form."""
        opts = self.model._meta
        custom_urls = [
            path(
                "<path:object_id>/observability/",
                self.admin_site.admin_view(self.observability_view),
                name=f"{opts.app_label}_{opts.model_name}_observability",
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
        return custom_urls + super().get_urls()

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Provide the package-owned polling URL to the custom change form."""
        opts = self.model._meta
        context = {
            **(extra_context or {}),
            "django_ray_observability_url": reverse(
                f"{self.admin_site.name}:{opts.app_label}_{opts.model_name}_observability",
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
        }
        return super().change_view(request, object_id, form_url, context)

    def _observability_execution(
        self,
        request: HttpRequest,
        object_id: str,
    ) -> RayTaskExecution:
        """Load only bounded task metadata before the authorized read service."""
        try:
            execution_id = self.model._meta.pk.to_python(unquote(object_id))
            return self.get_queryset(request).only(*self.observability_fields).get(pk=execution_id)
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

    def observability_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Return a versioned durable summary without querying Ray or task logs."""
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        execution = self._observability_execution(request, object_id)

        from django_ray import observability

        workflow_error = None
        workflow_error_code = None
        progress_envelope = None
        try:
            progress_envelope = get_workflow_progress_summary(
                execution,
                authorize=self._workflow_authorizer(request),
                include_legacy=False,
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
        summary = observability.get_task_summary(execution, workflow_progress=progress)
        if summary.get("error_message") is not None:
            summary["error_message"] = _bounded_redacted_text(summary["error_message"])
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
                **({"detail": progress.get("detail")} if schema_v3 else {}),
            }
            if progress is not None
            else None
        )
        summary["workflow_availability"] = (
            progress_envelope.get("availability") if progress_envelope is not None else None
        )
        response = JsonResponse(summary)
        response["Cache-Control"] = "no-store"
        return response

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
        return self._redacted_json(obj.args_json)

    @admin.display(description="Keyword arguments")
    def kwargs_json_display(self, obj: RayTaskExecution) -> str:
        return self._redacted_json(obj.kwargs_json)

    @admin.display(description="Result")
    def result_data_display(self, obj: RayTaskExecution) -> str:
        return self._redacted_json(obj.result_data)

    @admin.display(description="Progress")
    def progress_data_display(self, obj: RayTaskExecution) -> str:
        return self._redacted_json(obj.progress_data)

    @admin.display(description="Effective workflow plan")
    def workflow_plan_display(self, obj: RayTaskExecution) -> str:
        return self._redacted_json(obj.workflow_plan_json)

    @admin.display(description="Workflow strategy selection")
    def workflow_plan_selection_display(self, obj: RayTaskExecution) -> str:
        return self._redacted_json(obj.workflow_plan_selection)

    @admin.display(description="Completion envelope")
    def completion_data_display(self, obj: RayTaskExecution) -> str:
        return self._redacted_json(obj.completion_data)

    @admin.display(description="Error")
    def error_message_display(self, obj: RayTaskExecution) -> str:
        return redact_text(obj.error_message) if obj.error_message else "-"

    @admin.display(description="Traceback")
    def error_traceback_display(self, obj: RayTaskExecution) -> str:
        return redact_text(obj.error_traceback) if obj.error_traceback else "-"

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
        }
        state = str(obj.state)
        color = colors.get(state, "#6c757d")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            state,
        )

    @admin.display(description="Ray Dashboard")
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

    @admin.action(description="Retry selected tasks")
    def retry_tasks(self, request: HttpRequest, queryset: QuerySet[RayTaskExecution]) -> None:
        """Retry failed or lost tasks by resetting them to QUEUED state."""
        retryable_states = [TaskState.FAILED, TaskState.LOST]
        tasks_to_retry = queryset.filter(state__in=retryable_states)
        if not tasks_to_retry.exists():
            self.message_user(
                request,
                "No failed or lost tasks found in selection.",
            )
            return

        count = 0
        for task in tasks_to_retry.only("pk", "attempt_number", "execution_generation"):
            if task.pk is None:  # pragma: no cover - querysets contain persisted rows
                continue
            retried = retry_task(
                task.pk,
                expected_attempt_number=task.attempt_number,
                expected_execution_generation=task.execution_generation,
            )
            count += int(retried is not None)

        self.message_user(
            request,
            f"Queued {count} task(s) for retry.",
        )

    @admin.action(description="Cancel selected tasks")
    def cancel_tasks(self, request: HttpRequest, queryset: QuerySet[RayTaskExecution]) -> None:
        """Request package-owned cancellation for each authorized selection."""
        accepted_count = 0
        for task in queryset.only("pk", "attempt_number", "execution_generation"):
            if task.pk is None:  # pragma: no cover - querysets contain persisted rows
                continue
            result = request_task_cancellation(
                task.pk,
                expected_attempt_number=task.attempt_number,
                expected_execution_generation=task.execution_generation,
            )
            accepted_count += int(result.accepted)
        if accepted_count:
            message = (
                f"Accepted cancellation for {accepted_count} task(s). "
                "Workers will attempt best-effort interruption for running work."
            )
        else:
            message = "No selected tasks accepted cancellation."
        self.message_user(request, message)


@admin.register(TaskAttempt)
class TaskAttemptAdmin(DjangoRayModelAdmin):
    """Read-only historical attempt diagnostics."""

    list_display = [
        "execution_link",
        "attempt_number",
        "state",
        "started_at",
        "finished_at",
    ]
    list_display_links = ("attempt_number",)
    list_filter = ["state"]
    fields = [
        "execution_link",
        "attempt_number",
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

    def get_queryset(self, request: HttpRequest) -> QuerySet[TaskAttempt]:
        """Bound routine list reads while retaining detail diagnostics."""
        queryset = super().get_queryset(request).defer("workflow_progress_summary_json")
        resolver_match = request.resolver_match
        if (
            resolver_match is not None
            and resolver_match.url_name == "django_ray_taskattempt_changelist"
        ):
            queryset = queryset.only(
                "pk",
                "execution_id",
                "attempt_number",
                "state",
                "started_at",
                "finished_at",
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

    @admin.display(description="Error")
    def error_message_display(self, obj: TaskAttempt) -> str:
        return _bounded_redacted_text(obj.error_message)

    @admin.display(description="Traceback")
    def error_traceback_display(self, obj: TaskAttempt) -> str:
        return _bounded_redacted_text(obj.error_traceback)

    @admin.display(description="Result")
    def result_data_display(self, obj: TaskAttempt) -> str:
        return _bounded_redacted_json(obj.result_data)

    @admin.display(description="Result reference")
    def result_reference_display(self, obj: TaskAttempt) -> str:
        return _bounded_redacted_text(obj.result_reference)


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

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[TaskWorkerLease]
    ) -> QuerySet[TaskWorkerLease]:
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

    def get_queryset(self, request: HttpRequest) -> QuerySet[TaskWorkerLease]:
        """Default queryset - filter applied via ActiveWorkerFilter."""
        return super().get_queryset(request)

    @admin.display(description="Worker ID")
    def worker_id_short(self, obj: TaskWorkerLease) -> str:
        """Show shortened worker ID."""
        worker_id = str(obj.worker_id)
        return f"{worker_id[:12]}..."

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

    @admin.action(description="Mark selected as inactive")
    def mark_inactive(self, request: HttpRequest, queryset: QuerySet[TaskWorkerLease]) -> None:
        """Mark selected worker leases as inactive."""
        count = queryset.filter(is_active=True).update(
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

    @admin.action(description="Delete inactive worker leases")
    def delete_inactive(self, request: HttpRequest, queryset: QuerySet[TaskWorkerLease]) -> None:
        """Delete inactive worker leases from selected."""
        deleted_count, _ = queryset.filter(is_active=False).delete()

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
