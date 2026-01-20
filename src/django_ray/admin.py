"""Django admin configuration for django-ray."""

from typing import Any

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone
from django.utils.safestring import mark_safe

from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease

# Ray Dashboard URL - can be overridden via Django settings
RAY_DASHBOARD_URL = "http://localhost:30265"


@admin.register(RayTaskExecution)
class RayTaskExecutionAdmin(admin.ModelAdmin):
    """Admin for RayTaskExecution model."""

    list_display = [
        "id",
        "callable_path",
        "state_display",
        "queue_name",
        "attempt_number",
        "ray_dashboard_link",
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
        "ray_job_id_display",
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
    fieldsets = (
        (
            "Task Info",
            {
                "fields": ("task_id", "callable_path", "queue_name", "state", "attempt_number"),
            },
        ),
        (
            "Arguments",
            {
                "fields": ("args_json", "kwargs_json"),
                "classes": ("collapse",),
            },
        ),
        (
            "Result",
            {
                "fields": ("result_data", "error_message", "error_traceback"),
            },
        ),
        (
            "Ray Execution",
            {
                "fields": ("ray_job_id_display", "ray_address", "claimed_by_worker"),
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
            return mark_safe(
                f"Job: {ray_job}, Task: {ray_task[:16]}... "
                f'<a href="{url}" target="_blank">[Open in Dashboard]</a>'
            )

        # Ray Job API format
        url = f"{dashboard_url}/#/jobs/{job_id}"
        return mark_safe(f'{job_id} <a href="{url}" target="_blank">[Open in Dashboard]</a>')

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
        # state is from TaskState constants, not user input, so mark_safe is appropriate
        return mark_safe(f'<span style="color: {color}; font-weight: bold;">{state}</span>')

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
            return mark_safe(f'<a href="{url}" target="_blank">Jobs</a>')

        # New format: job_id:task_id (e.g., "02000000:67a2e8cfa5...")
        if ":" in job_id and not job_id.startswith("raysubmit_"):
            parts = job_id.split(":", 1)
            ray_job = parts[0]
            ray_task = parts[1]
            # Link directly to the task in the Ray Dashboard
            url = f"{dashboard_url}/#/jobs/{ray_job}/tasks/{ray_task}"
            return mark_safe(f'<a href="{url}" target="_blank">Task</a>')

        # Ray Job API - link to the job
        url = f"{dashboard_url}/#/jobs/{job_id}"
        return mark_safe(f'<a href="{url}" target="_blank">{job_id[:8]}...</a>')

    @admin.action(description="Retry selected tasks")
    def retry_tasks(self, request: HttpRequest, queryset: QuerySet[RayTaskExecution]) -> None:
        """Retry failed or lost tasks by resetting them to QUEUED state."""
        retryable_states = [TaskState.FAILED, TaskState.LOST]
        tasks_to_retry = queryset.filter(state__in=retryable_states)
        count = tasks_to_retry.count()

        if count == 0:
            self.message_user(
                request,
                "No failed or lost tasks found in selection.",
            )
            return

        # Reset tasks to QUEUED state for retry
        # TODO: In the future, we could preserve attempt history in a separate model
        # to track each run's result/error instead of overwriting
        tasks_to_retry.update(
            state=TaskState.QUEUED,
            attempt_number=0,
            result_data=None,
            error_message=None,
            error_traceback=None,
            started_at=None,
            finished_at=None,
            last_heartbeat_at=None,
            claimed_by_worker=None,
            ray_job_id=None,
        )

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
