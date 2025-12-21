"""Prometheus metrics for django-ray.

Note: This is a Phase 4 feature with basic structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MetricsCollector:
    """Collector for django-ray metrics."""

    # Task metrics
    tasks_queued: int = 0
    tasks_running: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0

    # Latency metrics (in seconds)
    queue_latency_sum: float = 0.0
    queue_latency_count: int = 0
    execution_latency_sum: float = 0.0
    execution_latency_count: int = 0

    # Worker metrics
    active_workers: int = 0

    # Per-queue metrics
    queue_depths: dict[str, int] = field(default_factory=dict)

    def record_task_queued(self, queue_name: str = "default") -> None:
        """Record a task being queued."""
        self.tasks_queued += 1
        self.queue_depths[queue_name] = self.queue_depths.get(queue_name, 0) + 1

    def record_task_started(self, queue_name: str = "default") -> None:
        """Record a task starting execution."""
        self.tasks_running += 1
        self.queue_depths[queue_name] = max(0, self.queue_depths.get(queue_name, 0) - 1)

    def record_task_completed(
        self,
        success: bool,
        queue_latency: float | None = None,
        execution_latency: float | None = None,
    ) -> None:
        """Record a task completion."""
        self.tasks_running = max(0, self.tasks_running - 1)

        if success:
            self.tasks_succeeded += 1
        else:
            self.tasks_failed += 1

        if queue_latency is not None:
            self.queue_latency_sum += queue_latency
            self.queue_latency_count += 1

        if execution_latency is not None:
            self.execution_latency_sum += execution_latency
            self.execution_latency_count += 1

    def to_prometheus_format(self) -> str:
        """Export metrics in Prometheus text format."""
        lines = [
            "# HELP django_ray_tasks_queued_total Total tasks queued",
            "# TYPE django_ray_tasks_queued_total counter",
            f"django_ray_tasks_queued_total {self.tasks_queued}",
            "",
            "# HELP django_ray_tasks_running Current running tasks",
            "# TYPE django_ray_tasks_running gauge",
            f"django_ray_tasks_running {self.tasks_running}",
            "",
            "# HELP django_ray_tasks_succeeded_total Total succeeded tasks",
            "# TYPE django_ray_tasks_succeeded_total counter",
            f"django_ray_tasks_succeeded_total {self.tasks_succeeded}",
            "",
            "# HELP django_ray_tasks_failed_total Total failed tasks",
            "# TYPE django_ray_tasks_failed_total counter",
            f"django_ray_tasks_failed_total {self.tasks_failed}",
            "",
            "# HELP django_ray_active_workers Current active workers",
            "# TYPE django_ray_active_workers gauge",
            f"django_ray_active_workers {self.active_workers}",
        ]

        if self.queue_depths:
            lines.extend(
                [
                    "",
                    "# HELP django_ray_queue_depth Current queue depth",
                    "# TYPE django_ray_queue_depth gauge",
                ]
            )
            for queue_name, depth in self.queue_depths.items():
                lines.append(f'django_ray_queue_depth{{queue="{queue_name}"}} {depth}')

        return "\n".join(lines)


# Global metrics collector instance
_metrics_collector: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    """Get the global metrics collector."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector
