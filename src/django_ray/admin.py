"""Django admin configuration for django-ray."""

from typing import Any

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from django_ray.models import RayTaskExecution, TaskWorkerLease


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
    def retry_tasks(self, request, queryset):
        """Retry failed tasks (placeholder for MVP)."""
        # TODO: Implement retry logic
        count = queryset.filter(state__in=["FAILED", "LOST"]).count()
        self.message_user(
            request,
            f"Retry requested for {count} tasks (not yet implemented)",
        )

    @admin.action(description="Cancel selected tasks")
    def cancel_tasks(self, request, queryset):
        """Cancel running or queued tasks."""
        # TODO: Implement cancellation logic
        count = queryset.filter(state__in=["QUEUED", "RUNNING"]).count()
        self.message_user(
            request,
            f"Cancellation requested for {count} tasks (not yet implemented)",
        )


class ActiveWorkerFilter(admin.SimpleListFilter):
    """Filter to show active/inactive workers with active as default."""

    title = "status"
    parameter_name = "is_active"

    def lookups(self, request, model_admin):
        return [
            ("active", "Active"),
            ("inactive", "Inactive"),
            ("all", "All"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "inactive":
            return queryset.filter(is_active=False)
        elif self.value() == "all":
            return queryset
        else:
            # Default: show only active
            return queryset.filter(is_active=True)

    def choices(self, changelist):
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
    # All fields should be read-only - this is an informational model
    # Editing these values doesn't change worker behavior
    readonly_fields = [
        "worker_id",
        "hostname",
        "pid",
        "queue_name",
        "started_at",
        "last_heartbeat_at",
        "stopped_at",
        "is_active_display",
        "time_since_heartbeat_display",
        "lease_status_display",
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
                "fields": (
                    "lease_status_display",
                    "is_active_display",
                    "time_since_heartbeat_display",
                ),
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

    def get_queryset(self, request):
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

    def _is_heartbeat_expired(self, obj: TaskWorkerLease) -> bool:
        """Check if heartbeat has expired."""
        from django_ray.runner.leasing import is_lease_expired

        return is_lease_expired(obj)

    @admin.display(description="Active Status")
    def is_active_display(self, obj: TaskWorkerLease) -> str:
        """Display active status with color coding for detail view."""
        from django.utils.safestring import mark_safe

        from django_ray.runner.leasing import is_lease_expired

        is_active = obj.is_active and not is_lease_expired(obj)
        if is_active:
            return mark_safe('<span style="color: #28a745; font-weight: bold;">✓ Active</span>')
        if not obj.is_active:
            return mark_safe('<span style="color: #6c757d; font-weight: bold;">✗ Stopped</span>')
        return mark_safe(
            '<span style="color: #dc3545; font-weight: bold;">✗ Expired (no heartbeat)</span>'
        )

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

    @admin.display(description="Time Since Heartbeat")
    def time_since_heartbeat_display(self, obj: TaskWorkerLease) -> str:
        """Show time since last heartbeat for detail view."""
        return self.time_since_heartbeat(obj)

    @admin.display(description="Lease Status")
    def lease_status_display(self, obj: TaskWorkerLease) -> str:
        """Comprehensive lease status for detail view."""
        from django_ray.runner.leasing import get_lease_duration, is_lease_expired

        duration = get_lease_duration()

        if not obj.is_active:
            stopped_info = ""
            if obj.stopped_at:
                stopped_info = f" Stopped at: {obj.stopped_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            return format_html(
                '<div style="padding: 10px; background: #e2e3e5; border-radius: 4px;">'
                '<strong style="color: #383d41;">✗ Worker is Stopped</strong><br>'
                "<small>This worker has been gracefully shut down or marked inactive.{}</small>"
                "</div>",
                stopped_info,
            )

        heartbeat_expired = is_lease_expired(obj)

        if not heartbeat_expired:
            return format_html(
                '<div style="padding: 10px; background: #d4edda; border-radius: 4px;">'
                '<strong style="color: #155724;">✓ Worker is Active</strong><br>'
                "<small>Heartbeat received within the last {} seconds.</small>"
                "</div>",
                int(duration.total_seconds()),
            )
        else:
            return format_html(
                '<div style="padding: 10px; background: #f8d7da; border-radius: 4px;">'
                '<strong style="color: #721c24;">⚠ Worker Heartbeat Expired</strong><br>'
                "<small>No heartbeat received in {} seconds. "
                "This worker may have crashed. It will be marked inactive automatically.</small>"
                "</div>",
                int(duration.total_seconds()),
            )

    @admin.action(description="Mark selected as inactive")
    def mark_inactive(self, request, queryset):
        """Mark selected worker leases as inactive."""
        from django.utils import timezone

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
    def delete_inactive(self, request, queryset):
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

    def has_add_permission(self, request: Any) -> bool:
        """Disable adding leases manually - workers create their own."""
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        """Disable editing leases - they are managed by workers."""
        return False
