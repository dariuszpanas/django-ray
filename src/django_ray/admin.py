"""Django admin configuration for django-ray."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from django.apps import apps
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.urls import path

from django_ray.admin_views import actions as _actions
from django_ray.admin_views import badges as _badges
from django_ray.admin_views import diagnostics as _diagnostics
from django_ray.admin_views import sensitive as _sensitive
from django_ray.admin_views.actions import (
    ADMIN_CANCEL_MAX_TASKS,
    ADMIN_CANCELLABLE_STATES,
    ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS,
    ADMIN_RETRY_CONFIRMATION_MAX_TASKS,
    ADMIN_RETRYABLE_STATES,
    ADMIN_TERMINAL_STATES,
)
from django_ray.admin_views.badges import (
    ADMIN_ATTEMPT_INLINE_MAX_BYTES,
    ADMIN_ATTEMPT_INLINE_MAX_CHARS,
    ADMIN_ATTEMPT_INLINE_MAX_ROWS,
    ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES,
    ADMIN_DIAGNOSTIC_MAX_CHARS,
    ActiveWorkerFilter,
    ExecutionProtocolFilter,
    HasWorkflowFilter,
)
from django_ray.admin_views.diagnostics import (
    ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES,
    ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES,
    ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES,
    ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS,
    ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES,
    ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES,
    ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES,
)
from django_ray.admin_views.sensitive import (
    ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES,
    ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
)
from django_ray.lifecycle import request_task_cancellation, request_task_retry
from django_ray.models import (
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskWorkerLease,
)
from django_ray.workflow.admin_graph import AdminWorkflowGraphError
from django_ray.workflow.progress.reads import (
    WorkflowProgressReadError,
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
    read_workflow_progress_collections,
)

__all__ = [
    "ADMIN_ATTEMPT_DETAIL_RESPONSE_MAX_BYTES",
    "ADMIN_ATTEMPT_INLINE_MAX_BYTES",
    "ADMIN_ATTEMPT_INLINE_MAX_CHARS",
    "ADMIN_ATTEMPT_INLINE_MAX_ROWS",
    "ADMIN_CANCEL_MAX_TASKS",
    "ADMIN_CANCELLABLE_STATES",
    "ADMIN_DETAIL_DIAGNOSTIC_FIELD_MAX_BYTES",
    "ADMIN_DIAGNOSTIC_MAX_CHARS",
    "ADMIN_EXECUTION_DETAIL_RESPONSE_MAX_BYTES",
    "ADMIN_OBSERVABILITY_RESPONSE_MAX_BYTES",
    "ADMIN_RETRY_CONFIRMATION_MAX_AGE_SECONDS",
    "ADMIN_RETRY_CONFIRMATION_MAX_TASKS",
    "ADMIN_RETRYABLE_STATES",
    "ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES",
    "ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES",
    "ADMIN_TERMINAL_STATES",
    "ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS",
    "ADMIN_WORKFLOW_DIAGNOSTICS_MAX_BYTES",
    "ADMIN_WORKFLOW_PLAN_DOWNLOAD_MAX_BYTES",
    "ADMIN_WORKFLOW_PLAN_SELECTION_DOWNLOAD_MAX_BYTES",
    "ActiveWorkerFilter",
    "DjangoRayModelAdmin",
    "DjangoRayTabularInline",
    "ExecutionProtocolFilter",
    "HasWorkflowFilter",
    "RayTaskExecutionAdmin",
    "TaskAttemptAdmin",
    "TaskAttemptInline",
    "TaskExecutionProtocolPolicyAdmin",
    "TaskWorkerLeaseAdmin",
]

# Backward-compatible internal helper and constant aliases preserved for tests/extensions.
_DASHBOARD_NOT_CONFIGURED = _badges.DASHBOARD_NOT_CONFIGURED
_OVERSIZE_RESULT_REFERENCE_PREFIX = _badges.OVERSIZE_RESULT_REFERENCE_PREFIX
_DIGEST_ONLY_STUB_LABEL = _badges.DIGEST_ONLY_STUB_LABEL
_FULL_WORKFLOW_ATTEMPT_MARKER = _badges.FULL_WORKFLOW_ATTEMPT_MARKER
_STATE_BADGE_SLUGS = _badges.STATE_BADGE_SLUGS
_ADMIN_DETAIL_OVERSIZED_MESSAGE = _badges.ADMIN_DETAIL_OVERSIZED_MESSAGE
_ADMIN_DETAIL_UNAVAILABLE_MESSAGE = _badges.ADMIN_DETAIL_UNAVAILABLE_MESSAGE
_ADMIN_DETAIL_INVALID_JSON_MESSAGE = _badges.ADMIN_DETAIL_INVALID_JSON_MESSAGE
_ray_dashboard_url = _badges.ray_dashboard_url
_state_badge_html = _badges.state_badge_html
_bounded_redacted_text = _badges.bounded_redacted_text
_bounded_traceback_display = _badges.bounded_traceback_display
_bounded_redacted_json = _badges.bounded_redacted_json
_bounded_json_html = _badges.bounded_json_html
_compact_admin_datetime = _badges.compact_admin_datetime
_compact_path_suffix = _badges.compact_path_suffix
_task_attempt_admin_mode = _badges.task_attempt_admin_mode

_ADMIN_DETAIL_RENDER_FAILURE_BODY = _diagnostics.ADMIN_DETAIL_RENDER_FAILURE_BODY
_ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES = _diagnostics.ADMIN_PROTOCOL_PROVENANCE_MAX_BYTES
_ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS = _diagnostics.ADMIN_PROTOCOL_PROVENANCE_MAX_CHARS
_ADMIN_PROTOCOL_PROVENANCE_FIELDS = _diagnostics.ADMIN_PROTOCOL_PROVENANCE_FIELDS
_ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS = _diagnostics.ADMIN_EXECUTION_DETAIL_DIAGNOSTIC_FIELDS
_ADMIN_EXECUTION_DETAIL_FIELDS = _diagnostics.ADMIN_EXECUTION_DETAIL_FIELDS
_ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS = _diagnostics.ADMIN_ATTEMPT_DETAIL_DIAGNOSTIC_FIELDS
_ADMIN_ATTEMPT_DETAIL_FIELDS = _diagnostics.ADMIN_ATTEMPT_DETAIL_FIELDS
_WORKFLOW_PROGRESS_MESSAGES = _diagnostics.WORKFLOW_PROGRESS_MESSAGES
_AdminOctetLength = _diagnostics.AdminOctetLength
_admin_bounded_annotation_name = _diagnostics.admin_bounded_annotation_name
_annotate_bounded_admin_text = _diagnostics.annotate_bounded_admin_text
_bounded_admin_text_value = _diagnostics.bounded_admin_text_value
_ordinary_admin_text_display = _diagnostics.ordinary_admin_text_display
_ordinary_admin_json_display = _diagnostics.ordinary_admin_json_display
_ordinary_admin_result_display = _diagnostics.ordinary_admin_result_display
_secure_admin_response = _diagnostics.secure_admin_response
_admin_json_response = _diagnostics.admin_json_response
_admin_json_attachment = _diagnostics.admin_json_attachment
_bounded_admin_detail_render_failure = _diagnostics.bounded_admin_detail_render_failure
_readonly_admin_urls = _diagnostics.readonly_admin_urls
_readonly_admin_removed_object_route = _diagnostics.readonly_admin_removed_object_route
_readonly_admin_change_method_response = _diagnostics.readonly_admin_change_method_response
_workflow_diagnostics_error_response = _diagnostics.workflow_diagnostics_error_response
_corrupt_workflow_plan_presentation = _diagnostics.corrupt_workflow_plan_presentation
_workflow_reporting_policy_hint = _diagnostics.workflow_reporting_policy_hint
_workflow_progress_presentation = _diagnostics.workflow_progress_presentation

_ADMIN_RETRY_CONFIRMATION_SALT = _actions.ADMIN_RETRY_CONFIRMATION_SALT
_ADMIN_RETRY_CONFIRMATION_MAX_TOKEN_CHARS = _actions.ADMIN_RETRY_CONFIRMATION_MAX_TOKEN_CHARS
_ADMIN_RETRY_CONFIRMATION_SESSION_KEY = _actions.ADMIN_RETRY_CONFIRMATION_SESSION_KEY
_ADMIN_RETRY_CONFIRMATION_SESSION_NONCE = _actions.ADMIN_RETRY_CONFIRMATION_SESSION_NONCE
_admin_retry_snapshot_digest = _actions.admin_retry_snapshot_digest
_admin_retry_actor_fingerprint = _actions.admin_retry_actor_fingerprint
_admin_retry_session_fingerprint = _actions.admin_retry_session_fingerprint
_admin_retry_confirmation_token = _actions.admin_retry_confirmation_token
_admin_retry_confirmation_matches = _actions.admin_retry_confirmation_matches

_ADMIN_SENSITIVE_PERMISSION = _sensitive.ADMIN_SENSITIVE_PERMISSION
_ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES = _sensitive.ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES
_ADMIN_SENSITIVE_HTML_EXPANSION_FACTOR = _sensitive.ADMIN_SENSITIVE_HTML_EXPANSION_FACTOR
_ADMIN_SENSITIVE_EXECUTION_SECTIONS = _sensitive.ADMIN_SENSITIVE_EXECUTION_SECTIONS
_ADMIN_SENSITIVE_ATTEMPT_OUTCOME_SECTION = _sensitive.ADMIN_SENSITIVE_ATTEMPT_OUTCOME_SECTION
_sensitive_annotation_name = _sensitive.sensitive_annotation_name
_bounded_sensitive_object = _sensitive.bounded_sensitive_object
_has_sensitive_task_data_permission = _sensitive.has_sensitive_task_data_permission
_safe_admin_reverse = _sensitive.safe_admin_reverse
_sensitive_diagnostics_response = _sensitive.sensitive_diagnostics_response

_DJANGO_RAY_ADMIN_USES_UNFOLD = apps.is_installed("unfold")

if _DJANGO_RAY_ADMIN_USES_UNFOLD:
    from unfold.admin import ModelAdmin as _ConfiguredModelAdmin
    from unfold.admin import TabularInline as _ConfiguredTabularInline
else:
    _ConfiguredModelAdmin = admin.ModelAdmin
    _ConfiguredTabularInline = admin.TabularInline

DjangoRayModelAdmin = cast(Any, _ConfiguredModelAdmin)
DjangoRayTabularInline = cast(Any, _ConfiguredTabularInline)


def _admin_retry_snapshot(queryset: QuerySet) -> list[dict[str, Any]]:
    return _actions.admin_retry_snapshot(
        queryset,
        max_tasks=ADMIN_RETRY_CONFIRMATION_MAX_TASKS,
    )


def _bounded_admin_detail_response(
    request: HttpRequest,
    response: HttpResponse,
    *,
    admin_site: admin.AdminSite,
    opts: Any,
    max_bytes: int,
) -> HttpResponse:
    return _diagnostics.bounded_admin_detail_response(
        request,
        response,
        admin_site=admin_site,
        opts=opts,
        max_bytes=max_bytes,
        template_response_cls=TemplateResponse,
    )


def _sensitive_sections(
    row: dict[str, Any],
    section_specs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
) -> list[dict[str, Any]]:
    return _sensitive.build_sensitive_sections(
        row,
        section_specs,
        field_max_bytes=ADMIN_SENSITIVE_DIAGNOSTIC_FIELD_MAX_BYTES,
        response_max_bytes=ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
        response_overhead_bytes=_ADMIN_SENSITIVE_RESPONSE_OVERHEAD_BYTES,
    )


def _workflow_read_hooks() -> _diagnostics.WorkflowReadHooks:
    """Capture current module-level workflow readers so monkeypatches propagate cleanly."""
    return _diagnostics.WorkflowReadHooks(
        get_summary=get_workflow_progress_summary,
        list_nodes=list_workflow_topology_nodes,
        list_edges=list_workflow_topology_edges,
        list_details=list_workflow_node_details,
        get_detail=get_workflow_node_detail,
        read_collections=read_workflow_progress_collections,
    )


class TaskAttemptInline(DjangoRayTabularInline):
    """Compact immutable attempt history on an execution change page."""

    model = TaskAttempt
    fk_name = "execution"
    fields = (
        "attempt_detail_link",
        "state_display",
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
        return _diagnostics.prepare_inline_attempt_queryset(super().get_queryset(request))

    def has_add_permission(self, request: HttpRequest, obj: RayTaskExecution | None = None) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: RayTaskExecution | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: RayTaskExecution | None = None
    ) -> bool:
        return False

    def has_view_permission(
        self, request: HttpRequest, obj: RayTaskExecution | None = None
    ) -> bool:
        """Require global child access and authorization for the parent."""
        if not super().has_view_permission(request):
            return False
        parent_admin = self.admin_site._registry.get(RayTaskExecution)
        return parent_admin is not None and parent_admin.has_view_permission(request, obj)

    @admin.display(description="Attempt")
    def attempt_detail_link(self, obj: TaskAttempt) -> str:
        return _badges.format_attempt_detail_link(self.admin_site, obj)

    @admin.display(description="State", ordering="state")
    def state_display(self, obj: TaskAttempt) -> str:
        return _state_badge_html(obj.state)

    @admin.display(description="Started")
    def started_display(self, obj: TaskAttempt) -> str:
        return _compact_admin_datetime(obj.started_at)

    @admin.display(description="Finished")
    def finished_display(self, obj: TaskAttempt) -> str:
        return _compact_admin_datetime(obj.finished_at)

    @admin.display(description="Error")
    def error_summary(self, obj: TaskAttempt) -> str:
        return _diagnostics.inline_attempt_error_summary(obj)


@admin.register(RayTaskExecution)
class RayTaskExecutionAdmin(DjangoRayModelAdmin):
    """Admin for RayTaskExecution model."""

    class Media:
        css = {"all": ("django_ray/admin/diagnostics.css",)}

    change_form_template = "admin/django_ray/raytaskexecution/change_form.html"
    retry_confirmation_template = "admin/django_ray/raytaskexecution/retry_confirmation.html"
    observability_fields = _diagnostics.OBSERVABILITY_FIELDS
    workflow_read_fields = _diagnostics.WORKFLOW_READ_FIELDS

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
        HasWorkflowFilter,
        ExecutionProtocolFilter,
        "created_at",
    ]
    search_fields = ["task_id", "callable_path", "ray_job_id"]
    readonly_fields = [
        "task_id",
        "callable_path",
        "priority",
        "queue_name",
        "state",
        "state_display",
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
                    "state_display",
                    "attempt_number",
                    "execution_generation",
                    "workflow_run_id",
                    "workflow_plan_fingerprint",
                    "workflow_plan_pinned_attempt",
                ),
                "description": (
                    "Execution metadata is read-only. Use the package-owned Task actions "
                    "panel or task-list Retry/Cancel actions for controlled state changes."
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
                "fields": ("args_json_display", "kwargs_json_display", "input_reference_display"),
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
    ordering = ["-created_at"]
    actions = ["retry_tasks", "cancel_tasks"]

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return _diagnostics.prepare_execution_queryset(
            super().get_queryset(request), request, opts=self.opts
        )

    def get_inlines(
        self, request: HttpRequest, obj: RayTaskExecution | None
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
        _diagnostics.populate_execution_change_form_context(
            self,
            request,
            context,
            obj=obj,
            inline_cls=TaskAttemptInline,
            uses_unfold=_DJANGO_RAY_ADMIN_USES_UNFOLD,
            terminal_states=ADMIN_TERMINAL_STATES,
            retryable_states=ADMIN_RETRYABLE_STATES,
            cancellable_states=ADMIN_CANCELLABLE_STATES,
            has_sensitive_permission=_has_sensitive_task_data_permission,
            retry_guidance_fn=self._retry_detail_guidance,
            archived_attempts_limit=ADMIN_WORKFLOW_ARCHIVED_GRAPH_MAX_ATTEMPTS,
        )
        return super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )

    def has_view_permission(
        self, request: HttpRequest, obj: RayTaskExecution | None = None
    ) -> bool:
        """Honor global grants and object-permission backends."""
        if super().has_view_permission(request, obj):
            return True
        return _actions.has_object_view_permission(request, self.opts, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: RayTaskExecution | None = None
    ) -> bool:
        """Keep list actions authorized while preventing direct row saves."""
        if obj is not None:
            return False
        return super().has_change_permission(request, obj)

    def has_action_permission(
        self, request: HttpRequest, obj: RayTaskExecution | None = None
    ) -> bool:
        return _actions.has_execution_action_permission(self, request, obj)

    def has_delete_permission(
        self, request: HttpRequest, obj: RayTaskExecution | None = None
    ) -> bool:
        return False

    @staticmethod
    def _retry_detail_guidance(state: str) -> str:
        return _actions.retry_detail_guidance(state)

    def get_urls(self) -> list[Any]:
        """Add the authenticated durable-summary endpoints used by the change form."""
        opts = self.model._meta
        prefix = f"{opts.app_label}_{opts.model_name}"
        removed_view = self.admin_site.admin_view(_readonly_admin_removed_object_route)
        named_routes = (
            ("sensitive-data/", self.sensitive_data_view, "sensitive_data"),
            ("retry/", self.retry_view, "retry"),
            ("cancel/", self.cancel_view, "cancel"),
            ("observability/", self.observability_view, "observability"),
            ("workflow/diagnostics/", self.workflow_diagnostics_view, "workflow_diagnostics"),
            (
                "workflow/diagnostics/plan.json",
                self.workflow_plan_download_view,
                "workflow_plan_download",
            ),
            (
                "workflow/diagnostics/selection.json",
                self.workflow_plan_selection_download_view,
                "workflow_plan_selection_download",
            ),
            ("workflow/graph/", self.workflow_graph_view, "workflow_graph"),
            (
                "workflow/topology/nodes/",
                self.workflow_topology_nodes_view,
                "workflow_topology_nodes",
            ),
            (
                "workflow/topology/edges/",
                self.workflow_topology_edges_view,
                "workflow_topology_edges",
            ),
            ("workflow/nodes/", self.workflow_node_details_view, "workflow_node_details"),
            ("workflow/node/", self.workflow_node_detail_view, "workflow_node_detail"),
        )
        custom_urls = [
            path("<path:object_id>/history/", removed_view),
            path("<path:object_id>/delete/", removed_view),
            *[
                path(
                    f"<path:object_id>/{suffix}",
                    self.admin_site.admin_view(view),
                    name=f"{prefix}_{name}",
                )
                for suffix, view, name in named_routes
            ],
        ]
        return custom_urls + _readonly_admin_urls(super().get_urls(), opts=opts)

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        method_response = _readonly_admin_change_method_response(request)
        if method_response is not None:
            return method_response
        context = _diagnostics.build_execution_change_view_context(
            self.admin_site, self.model._meta, object_id, extra_context
        )
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
        return _sensitive.execution_sensitive_data_view(
            self,
            request,
            object_id,
            sections_builder=_sensitive_sections,
            response_max_bytes=ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
        )

    def retry_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return _actions.retry_view(self, request, object_id)

    def cancel_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return _actions.cancel_view(self, request, object_id, cancel_fn=request_task_cancellation)

    def _observability_execution(self, request: HttpRequest, object_id: str) -> RayTaskExecution:
        return cast(
            RayTaskExecution, _diagnostics.observability_execution(self, request, object_id)
        )

    def _authorized_workflow_read_execution(
        self, request: HttpRequest, object_id: str
    ) -> RayTaskExecution:
        return cast(
            RayTaskExecution,
            _diagnostics.authorized_workflow_read_execution(self, request, object_id),
        )

    def _load_bounded_workflow_plan_fields(
        self, request: HttpRequest, execution: RayTaskExecution
    ) -> RayTaskExecution:
        return cast(
            RayTaskExecution,
            _diagnostics.load_bounded_workflow_plan_fields(self, request, execution),
        )

    def _workflow_authorizer(self, request: HttpRequest) -> Callable[[RayTaskExecution], bool]:
        return _diagnostics.workflow_authorizer(self, request)

    @staticmethod
    def _workflow_read_error_response(error: WorkflowProgressReadError) -> HttpResponse:
        return _diagnostics.workflow_read_error_response(error)

    @staticmethod
    def _page_limit(request: HttpRequest) -> int:
        return _diagnostics.parse_page_limit(request)

    @staticmethod
    def _attempt_number(request: HttpRequest) -> int | None:
        return _diagnostics.parse_attempt_number(request)

    def _preflight_workflow_collection(
        self,
        request: HttpRequest,
        execution: RayTaskExecution,
        *,
        collection: str,
        attempt_number: int | None = None,
    ) -> dict[str, Any]:
        return _diagnostics.preflight_workflow_collection(
            self,
            request,
            execution,
            collection=collection,
            attempt_number=attempt_number,
            hooks=_workflow_read_hooks(),
        )

    _preflight_workflow_collection._django_ray_default_preflight = True  # type: ignore[attr-defined]

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
        return _diagnostics.lazy_workflow_progress_presentation(
            self,
            request,
            execution,
            plan,
            plan_binding,
            attempt_number=attempt_number,
            reporting_policy_hint=reporting_policy_hint,
            hooks=_workflow_read_hooks(),
        )

    def workflow_diagnostics_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return _diagnostics.workflow_diagnostics_view(self, request, object_id)

    def _verified_workflow_plan_download(
        self, request: HttpRequest, object_id: str
    ) -> dict[str, Any] | None:
        return _diagnostics.verified_workflow_plan_download(self, request, object_id)

    def _workflow_plan_attachment_view(
        self, request: HttpRequest, object_id: str, *, component: str
    ) -> HttpResponse:
        return _diagnostics.workflow_plan_attachment_view(
            self, request, object_id, component=component
        )

    def workflow_plan_download_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return self._workflow_plan_attachment_view(request, object_id, component="manifest")

    def workflow_plan_selection_download_view(
        self, request: HttpRequest, object_id: str
    ) -> HttpResponse:
        return self._workflow_plan_attachment_view(request, object_id, component="selection")

    def observability_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return _diagnostics.observability_view(
            self, request, object_id, hooks=_workflow_read_hooks()
        )

    @staticmethod
    def _workflow_graph_issue_response(issue: AdminWorkflowGraphError) -> HttpResponse:
        return _diagnostics.workflow_graph_issue_response(issue)

    def _workflow_graph_read_error_response(self, error: WorkflowProgressReadError) -> HttpResponse:
        return _diagnostics.workflow_graph_read_error_response(error)

    def workflow_graph_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return _diagnostics.workflow_graph_view(
            self, request, object_id, hooks=_workflow_read_hooks()
        )

    def workflow_topology_nodes_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return self._workflow_page_view(request, object_id, collection="topology_nodes")

    def workflow_topology_edges_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return self._workflow_page_view(request, object_id, collection="topology_edges")

    def workflow_node_details_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return self._workflow_page_view(request, object_id, collection="node_details")

    def _workflow_page_view(
        self, request: HttpRequest, object_id: str, *, collection: str
    ) -> HttpResponse:
        return _diagnostics.workflow_page_view(
            self, request, object_id, collection=collection, hooks=_workflow_read_hooks()
        )

    def workflow_node_detail_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        return _diagnostics.workflow_node_detail_view(
            self, request, object_id, hooks=_workflow_read_hooks()
        )

    @staticmethod
    def _redacted_json(value: str | None) -> str:
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
        return _ordinary_admin_result_display(obj)

    @admin.display(description="Result reference")
    def result_reference_display(self, obj: RayTaskExecution) -> str:
        return _ordinary_admin_text_display(obj, "result_reference")

    @admin.display(description="Progress")
    def progress_data_display(self, obj: RayTaskExecution) -> str:
        return _bounded_json_html(obj.progress_data)

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
        return _diagnostics.ordinary_admin_traceback_display(obj)

    @admin.display(description="Ray Job ID")
    def ray_job_id_display(self, obj: RayTaskExecution) -> str:
        return _badges.format_ray_job_id_display(obj)

    @admin.display(boolean=True, description="Compatible worker available")
    def protocol_compatible_worker_available_display(self, obj: RayTaskExecution) -> bool | None:
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
        return _state_badge_html(obj.state)

    @admin.display(description="Task", ordering="callable_path")
    def task_display(self, obj: RayTaskExecution) -> str:
        return _badges.format_task_display(obj)

    @admin.display(description="Queue", ordering="queue_name")
    def queue_display(self, obj: RayTaskExecution) -> str:
        return _badges.format_queue_display(obj)

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
        return _badges.format_ray_dashboard_link(obj)

    @admin.action(description="Retry selected tasks...", permissions=["change"])
    def retry_tasks(self, request: HttpRequest, queryset: QuerySet) -> HttpResponse | None:
        return self._retry_tasks_with_confirmation(request, queryset)

    def _retry_tasks_with_confirmation(
        self,
        request: HttpRequest,
        queryset: QuerySet,
        *,
        cancel_url: str | None = None,
    ) -> HttpResponse | None:
        return _actions.retry_tasks_with_confirmation(
            self,
            request,
            queryset,
            cancel_url=cancel_url,
            snapshot_fn=_admin_retry_snapshot,
            retry_fn=request_task_retry,
            max_tasks=ADMIN_RETRY_CONFIRMATION_MAX_TASKS,
        )

    @admin.action(description="Cancel selected tasks", permissions=["change"])
    def cancel_tasks(self, request: HttpRequest, queryset: QuerySet) -> None:
        _actions.cancel_tasks(
            self,
            request,
            queryset,
            cancel_fn=request_task_cancellation,
            max_tasks=ADMIN_CANCEL_MAX_TASKS,
        )


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
        "state_display",
        "started_display",
        "finished_display",
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
        "state_display",
        "started_at",
        "finished_at",
        "error_message_display",
        "error_traceback_display",
        "result_data_display",
        "result_reference_display",
        "created_at",
    ]
    readonly_fields = fields

    def _parent_execution_admin(self) -> admin.ModelAdmin | None:
        return _sensitive.parent_execution_admin(self.admin_site)

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add: bool = False,
        change: bool = False,
        form_url: str = "",
        obj: TaskAttempt | None = None,
    ) -> HttpResponse:
        context["django_ray_admin_uses_unfold"] = _DJANGO_RAY_ADMIN_USES_UNFOLD
        context["show_history"] = False
        context["django_ray_sensitive_data_url"] = _sensitive.resolve_attempt_sensitive_data_url(
            self, request, obj
        )
        return super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )

    def get_urls(self) -> list[Any]:
        opts = self.model._meta
        removed_view = self.admin_site.admin_view(_readonly_admin_removed_object_route)
        custom_urls = [
            path("<path:object_id>/history/", removed_view),
            path("<path:object_id>/delete/", removed_view),
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
        return _sensitive.attempt_sensitive_data_view(
            self,
            request,
            object_id,
            sections_builder=_sensitive_sections,
            response_max_bytes=ADMIN_SENSITIVE_DIAGNOSTIC_RESPONSE_MAX_BYTES,
        )

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return _diagnostics.prepare_attempt_queryset(super().get_queryset(request), request)

    def has_module_permission(self, request: HttpRequest) -> bool:
        """Hide standalone navigation when contextual history is the default."""
        return _task_attempt_admin_mode() in {
            "standalone",
            "both",
        } and super().has_module_permission(request)

    def has_view_permission(self, request: HttpRequest, obj: TaskAttempt | None = None) -> bool:
        """Honor global grants and object-permission backends on detail reads."""
        if super().has_view_permission(request, obj):
            return True
        return _actions.has_object_view_permission(request, self.opts, obj)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: TaskAttempt | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: TaskAttempt | None = None) -> bool:
        return False

    @admin.display(description="Execution", ordering="execution_id")
    def execution_link(self, obj: TaskAttempt) -> str:
        return _badges.format_execution_link(self.admin_site, obj)

    @admin.display(description="State", ordering="state")
    def state_display(self, obj: TaskAttempt) -> str:
        return _state_badge_html(obj.state)

    @admin.display(description="Started", ordering="started_at")
    def started_display(self, obj: TaskAttempt) -> str:
        return _compact_admin_datetime(obj.started_at)

    @admin.display(description="Finished", ordering="finished_at")
    def finished_display(self, obj: TaskAttempt) -> str:
        return _compact_admin_datetime(obj.finished_at)

    @admin.display(description="Workflow graph")
    def workflow_graph_link(self, obj: TaskAttempt) -> str:
        return _badges.format_workflow_graph_link(self.admin_site, obj)

    @admin.display(description="Error")
    def error_message_display(self, obj: TaskAttempt) -> str:
        return _ordinary_admin_text_display(obj, "error_message")

    @admin.display(description="Traceback")
    def error_traceback_display(self, obj: TaskAttempt) -> str:
        return _diagnostics.ordinary_admin_traceback_display(obj)

    @admin.display(description="Result")
    def result_data_display(self, obj: TaskAttempt) -> str:
        return _ordinary_admin_result_display(obj)

    @admin.display(description="Result reference")
    def result_reference_display(self, obj: TaskAttempt) -> str:
        return _ordinary_admin_text_display(obj, "result_reference")


@admin.register(TaskWorkerLease)
class TaskWorkerLeaseAdmin(DjangoRayModelAdmin):
    """Admin for TaskWorkerLease model.

    Note: This tracks Django task workers (django_ray_worker command),
    NOT Ray cluster workers. The Django workers claim tasks from the
    database and submit them to Ray for execution.

    By default, only active workers are shown. Use the filter to see inactive workers.
    """

    class Media:
        css = {"all": ("django_ray/admin/diagnostics.css",)}

    list_display = [
        "worker_id_short",
        "hostname",
        "pid",
        "queue_name",
        "capability_schema_version",
        "supported_execution_protocols",
        "django_ray_version",
        "started_display",
        "last_heartbeat_display",
        "is_active_display_list",
        "time_since_heartbeat",
    ]
    list_filter = [ActiveWorkerFilter, "queue_name", "hostname"]
    search_fields = ["worker_id", "hostname"]
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
        "lease_expires_at",
        "lease_policy_revision",
        "stopped_at",
        "is_active",
    ]
    fieldsets = (
        ("Worker Identification", {"fields": ("worker_id", "hostname", "pid")}),
        (
            "Configuration",
            {
                "fields": ("queue_name",),
                "description": (
                    "Informational queue(s) reported when this worker started via --queue. "
                    "Worker leases are read-only in Admin."
                ),
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
        ("Status", {"fields": ("is_active",)}),
        (
            "Timing",
            {
                "fields": (
                    "started_at",
                    "last_heartbeat_at",
                    "lease_expires_at",
                    "lease_policy_revision",
                    "stopped_at",
                ),
            },
        ),
    )
    actions = ["mark_inactive", "delete_inactive"]

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return _diagnostics.prepare_worker_lease_queryset(super().get_queryset(request))

    @admin.display(description="Worker ID", ordering="worker_id")
    def worker_id_short(self, obj: TaskWorkerLease) -> str:
        return _badges.format_worker_id_short(obj)

    @admin.display(description="Started", ordering="started_at")
    def started_display(self, obj: TaskWorkerLease) -> str:
        return _compact_admin_datetime(obj.started_at)

    @admin.display(description="Last heartbeat", ordering="last_heartbeat_at")
    def last_heartbeat_display(self, obj: TaskWorkerLease) -> str:
        return _compact_admin_datetime(obj.last_heartbeat_at)

    @admin.display(description="Execution protocols")
    def supported_execution_protocols(self, obj: TaskWorkerLease) -> str:
        return _badges.format_supported_execution_protocols(obj)

    @admin.display(boolean=True, description="Active", ordering="is_active")
    def is_active_display_list(self, obj: TaskWorkerLease) -> bool | None:
        """Display active status as boolean icon using DB-clock annotation when available."""
        if "_admin_lease_live" in obj.__dict__:
            annotated = obj.__dict__["_admin_lease_live"]
            return None if annotated is None else bool(annotated)
        from django_ray.lease_policy import LeasePolicyError

        try:
            return bool(obj.is_active) and not self._is_heartbeat_expired(obj)
        except LeasePolicyError:
            return None

    @staticmethod
    def _is_heartbeat_expired(obj: TaskWorkerLease) -> bool:
        from django_ray.runner.leasing import is_lease_expired

        return is_lease_expired(obj)

    @admin.display(description="Time Since Heartbeat")
    def time_since_heartbeat(self, obj: TaskWorkerLease) -> str:
        return _badges.format_time_since_heartbeat(obj)

    @admin.action(description="Mark selected as inactive", permissions=["manage_worker_leases"])
    def mark_inactive(self, request: HttpRequest, queryset: QuerySet) -> None:
        _actions.mark_leases_inactive(self, request, queryset)

    def _mark_inactive_under_policy(self, request: HttpRequest, queryset: QuerySet) -> None:
        _actions.mark_leases_inactive_under_policy(self, request, queryset)

    @admin.action(description="Delete inactive worker leases", permissions=["manage_worker_leases"])
    def delete_inactive(self, request: HttpRequest, queryset: QuerySet) -> None:
        _actions.delete_inactive_leases(self, request, queryset)

    def _delete_inactive_under_policy(self, request: HttpRequest, queryset: QuerySet) -> None:
        _actions.delete_inactive_leases_under_policy(self, request, queryset)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_manage_worker_leases_permission(self, request: HttpRequest) -> bool:
        return _actions.has_manage_worker_leases_permission(self.opts, request)


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
        self, request: HttpRequest, obj: TaskExecutionProtocolPolicy | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: TaskExecutionProtocolPolicy | None = None
    ) -> bool:
        return False
