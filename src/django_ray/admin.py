"""Django admin configuration for django-ray."""

from django.contrib import admin
from django.utils.html import format_html

from django_ray.models import RayTaskExecution, RayWorkerLease


@admin.register(RayTaskExecution)
class RayTaskExecutionAdmin(admin.ModelAdmin):
    """Admin for RayTaskExecution model."""

    list_display = [
        "id",
        "callable_path",
        "state_display",
        "queue_name",
        "attempt_number",
        "ray_job_id",
        "created_at",
        "started_at",
        "finished_at",
    ]
    list_filter = [
        "state",
        "queue_name",
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
        "ray_job_id",
        "ray_address",
        "created_at",
        "started_at",
        "finished_at",
        "last_heartbeat_at",
        "args_json",
        "kwargs_json",
        "result_data",
        "error_message",
        "error_traceback",
    ]
    ordering = ["-created_at"]
    actions = ["retry_tasks", "cancel_tasks"]

    @admin.display(description="State", ordering="state")
    def state_display(self, obj: RayTaskExecution) -> str:
        """Display state with color coding."""
        colors = {
            "QUEUED": "#6c757d",
            "RUNNING": "#007bff",
            "SUCCEEDED": "#28a745",
            "FAILED": "#dc3545",
            "CANCELLED": "#ffc107",
            "CANCELLING": "#ffc107",
            "LOST": "#dc3545",
        }
        color = colors.get(obj.state, "#6c757d")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.state,
        )

    @admin.action(description="Retry selected tasks")
    def retry_tasks(self, request, queryset):  # type: ignore[no-untyped-def]
        """Retry failed tasks (placeholder for MVP)."""
        # TODO: Implement retry logic
        count = queryset.filter(state__in=["FAILED", "LOST"]).count()
        self.message_user(
            request,
            f"Retry requested for {count} tasks (not yet implemented)",
        )

    @admin.action(description="Cancel selected tasks")
    def cancel_tasks(self, request, queryset):  # type: ignore[no-untyped-def]
        """Cancel running or queued tasks."""
        # TODO: Implement cancellation logic
        count = queryset.filter(state__in=["QUEUED", "RUNNING"]).count()
        self.message_user(
            request,
            f"Cancellation requested for {count} tasks (not yet implemented)",
        )


@admin.register(RayWorkerLease)
class RayWorkerLeaseAdmin(admin.ModelAdmin):
    """Admin for RayWorkerLease model."""

    list_display = [
        "worker_id",
        "hostname",
        "pid",
        "queue_name",
        "started_at",
        "last_heartbeat_at",
    ]
    list_filter = [
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
        "started_at",
        "last_heartbeat_at",
    ]
