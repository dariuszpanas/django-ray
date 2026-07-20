"""Django admin configuration for django-ray."""

import json
from typing import Any

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.http import quote, unquote
from django.utils.text import Truncator

from django_ray.lifecycle import retry_task
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease
from django_ray.redaction import redact_text, safe_json_dumps

# Ray Dashboard URL fallback for local Ray.
RAY_DASHBOARD_URL = "http://localhost:8265"
ADMIN_DIAGNOSTIC_MAX_CHARS = 4096


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


@admin.register(RayTaskExecution)
class RayTaskExecutionAdmin(admin.ModelAdmin):
    """Admin for RayTaskExecution model."""

    change_form_template = "admin/django_ray/raytaskexecution/change_form.html"

    list_display = [
        "id",
        "callable_path",
        "state_display",
        "queue_name",
        "priority",
        "attempt_number",
        "execution_generation",
        "workflow_run_id",
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
        "ray_address",
        "runtime_env_profile",
        "runtime_env_json",
        "runtime_env_hash",
        "workflow_run_id",
        "created_at",
        "started_at",
        "finished_at",
        "last_heartbeat_at",
        "args_json_display",
        "kwargs_json_display",
        "input_reference",
        "result_data_display",
        "result_reference",
        "progress_data_display",
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
                    "progress_data_display",
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
            )
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
        }
        return super().change_view(request, object_id, form_url, context)

    def observability_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Return a versioned durable summary without querying Ray or task logs."""
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        execution = self.get_object(request, unquote(object_id))
        if execution is None:
            raise Http404("Ray task execution was not found")
        if not self.has_view_permission(request, execution):
            raise PermissionDenied

        from django_ray import observability

        summary = observability.get_task_summary(execution)
        if summary.get("error_message") is not None:
            summary["error_message"] = _bounded_redacted_text(summary["error_message"])
        try:
            progress = observability.get_workflow_progress(execution)
        except observability.WorkflowObservabilityError as error:
            progress = None
            summary["workflow_error"] = _bounded_redacted_text(error)
        summary["workflow"] = (
            {
                "revision": progress.get("revision", 0),
                "run_identity": progress.get("run_identity"),
                "state": progress.get("state", "RUNNING"),
                "total_nodes": progress.get("total_nodes", 0),
                "completed_nodes": progress.get("completed_nodes", 0),
                "failed_nodes": progress.get("failed_nodes", 0),
                "running_nodes": progress.get("running_nodes", 0),
                "pending_nodes": progress.get("pending_nodes", 0),
                "progress_percent": progress.get("progress_percent", 0.0),
            }
            if progress is not None
            else None
        )
        response = JsonResponse(summary)
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

        count = sum(1 for task in tasks_to_retry.only("pk") if retry_task(task))

        self.message_user(
            request,
            f"Queued {count} task(s) for retry.",
        )

    @admin.action(description="Cancel selected tasks")
    def cancel_tasks(self, request: HttpRequest, queryset: QuerySet[RayTaskExecution]) -> None:
        """Cancel queued or running tasks.

        For QUEUED tasks: Marks as CANCELLED immediately.
        For RUNNING tasks with Ray Job API: Attempts to stop the Ray job.
        For RUNNING tasks with Ray Core: Marks as CANCELLED (worker will skip on next poll).
        """
        cancellable_states = [TaskState.QUEUED, TaskState.RUNNING]
        tasks_to_cancel = queryset.filter(state__in=cancellable_states)

        if not tasks_to_cancel.exists():
            self.message_user(
                request,
                "No queued or running tasks found in selection.",
            )
            return

        cancelled_count = 0
        ray_job_cancel_attempted = 0

        for task in tasks_to_cancel:
            now = timezone.now()

            if task.state == TaskState.QUEUED:
                # Queued tasks can be cancelled directly
                task.state = TaskState.CANCELLED
                task.finished_at = now
                task.save(update_fields=["state", "finished_at"])
                cancelled_count += 1

            elif task.state == TaskState.RUNNING:
                # Check if this is a Ray Job API task
                # Ray Job API: starts with "raysubmit_"
                # Ray Core old format: starts with "ray_core:"
                # Ray Core new format: "job_id:task_id" (neither starts with raysubmit_ nor ray_core:)
                ray_job_id = task.ray_job_id
                is_ray_job_api = ray_job_id and str(ray_job_id).startswith("raysubmit_")

                if is_ray_job_api:
                    # Try to stop the Ray job
                    try:
                        from django_ray.runner.ray_job import RayJobRunner

                        runner = RayJobRunner()
                        from datetime import UTC, datetime

                        from django_ray.runner.base import SubmissionHandle

                        handle = SubmissionHandle(
                            ray_job_id=str(ray_job_id),
                            ray_address=str(task.ray_address or ""),
                            submitted_at=task.started_at or datetime.now(UTC),
                        )
                        runner.cancel(handle)
                        ray_job_cancel_attempted += 1
                    except Exception:
                        pass  # Best effort

                # Mark as CANCELLING - worker will finalize on next poll
                task.state = TaskState.CANCELLING
                task.save(update_fields=["state"])
                cancelled_count += 1

        message = f"Marked {cancelled_count} task(s) for cancellation."
        if ray_job_cancel_attempted:
            message += f" Attempted to stop {ray_job_cancel_attempted} Ray job(s)."

        self.message_user(request, message)


@admin.register(TaskAttempt)
class TaskAttemptAdmin(admin.ModelAdmin):
    """Read-only historical attempt diagnostics."""

    list_display = ["execution", "attempt_number", "state", "started_at", "finished_at"]
    list_filter = ["state"]
    fields = [
        "execution",
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
class TaskWorkerLeaseAdmin(admin.ModelAdmin):
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
