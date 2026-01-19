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


@admin.register(TaskWorkerLease)
class TaskWorkerLeaseAdmin(admin.ModelAdmin):
    """Admin for TaskWorkerLease model.

    Note: This tracks Django task workers (django_ray_worker command),
    NOT Ray cluster workers. The Django workers claim tasks from the
    database and submit them to Ray for execution.
    """

    list_display = [
        "worker_id_short",
        "hostname",
        "pid",
        "queue_name",
        "started_at",
        "last_heartbeat_at",
        "is_active",
        "time_since_heartbeat",
    ]
    list_filter = [
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
        "is_active_display",
        "time_since_heartbeat_display",
        "lease_status_display",
    ]
    fieldsets = (
        ("Worker Identification", {
            "fields": ("worker_id", "hostname", "pid"),
        }),
        ("Configuration", {
            "fields": ("queue_name",),
            "description": "Note: Changing the queue here does NOT affect the worker. "
                          "The queue is set when the worker starts via --queue argument.",
        }),
        ("Status", {
            "fields": ("lease_status_display", "is_active_display", "time_since_heartbeat_display"),
        }),
        ("Timing", {
            "fields": ("started_at", "last_heartbeat_at"),
        }),
    )
    actions = ["cleanup_expired_leases"]

    @admin.display(description="Worker ID")
    def worker_id_short(self, obj: TaskWorkerLease) -> str:
        """Show shortened worker ID."""
        return f"{obj.worker_id[:12]}..."

    @admin.display(boolean=True, description="Active")
    def is_active(self, obj: TaskWorkerLease) -> bool:
        """Check if the worker lease is still active (not expired)."""
        from django_ray.runner.leasing import is_lease_expired
        return not is_lease_expired(obj)

    @admin.display(description="Active Status")
    def is_active_display(self, obj: TaskWorkerLease) -> str:
        """Display active status with color coding for detail view."""
        from django.utils.safestring import mark_safe
        from django_ray.runner.leasing import is_lease_expired
        is_active = not is_lease_expired(obj)
        if is_active:
            return mark_safe(
                '<span style="color: #28a745; font-weight: bold;">✓ Active</span>'
            )
        return mark_safe(
            '<span style="color: #dc3545; font-weight: bold;">✗ Expired/Inactive</span>'
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
        from django_ray.runner.leasing import is_lease_expired, get_lease_duration

        is_active = not is_lease_expired(obj)
        duration = get_lease_duration()

        if is_active:
            return format_html(
                '<div style="padding: 10px; background: #d4edda; border-radius: 4px;">'
                '<strong style="color: #155724;">✓ Worker is Active</strong><br>'
                '<small>Heartbeat received within the last {} seconds.</small>'
                '</div>',
                int(duration.total_seconds())
            )
        else:
            return format_html(
                '<div style="padding: 10px; background: #f8d7da; border-radius: 4px;">'
                '<strong style="color: #721c24;">✗ Worker is Inactive/Expired</strong><br>'
                '<small>No heartbeat received in {} seconds. '
                'This worker may have crashed or been stopped. '
                'You can safely delete this lease record.</small>'
                '</div>',
                int(duration.total_seconds())
            )

    @admin.action(description="Clean up expired worker leases")
    def cleanup_expired_leases(self, request, queryset):  # type: ignore[no-untyped-def]
        """Delete expired worker leases from selected."""
        from django_ray.runner.leasing import is_lease_expired

        expired_count = 0
        for lease in queryset:
            if is_lease_expired(lease):
                lease.delete()
                expired_count += 1

        if expired_count > 0:
            self.message_user(
                request,
                f"Deleted {expired_count} expired worker lease(s).",
            )
        else:
            self.message_user(
                request,
                "No expired leases found in selection.",
            )

    def has_add_permission(self, request: Any) -> bool:
        """Disable adding leases manually - workers create their own."""
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        """Disable editing leases - they are managed by workers."""
        return False

