"""Prometheus text rendering from django-ray's durable database state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import (
    Case,
    Count,
    DateTimeField,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Sum,
    Value,
    When,
)

from django_ray.observability import OBSERVABILITY_SCHEMA_VERSION
from django_ray.runner.leasing import get_lease_duration

if TYPE_CHECKING:
    from collections.abc import Sequence


_TIMEOUT_PREFIX = "Task timed out after "


@dataclass
class _TimingSummary:
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value using the text exposition rules."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, ".12g")


def _metric(
    lines: list[str],
    *,
    name: str,
    help_text: str,
    value: float | int,
    labels: dict[str, str] | None = None,
) -> None:
    lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} gauge"])
    label_text = ""
    if labels:
        rendered = ",".join(
            f'{key}="{_escape_label(label)}"' for key, label in sorted(labels.items())
        )
        label_text = f"{{{rendered}}}"
    lines.append(f"{name}{label_text} {_number(value)}")


def _metric_family(
    lines: list[str],
    *,
    name: str,
    help_text: str,
    values: list[tuple[dict[str, str], float | int]],
) -> None:
    lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} gauge"])
    for labels, value in values:
        rendered = ",".join(
            f'{key}="{_escape_label(label)}"' for key, label in sorted(labels.items())
        )
        lines.append(f"{name}{{{rendered}}} {_number(value)}")


def _timing_metrics(lines: list[str], *, prefix: str, summary: _TimingSummary) -> None:
    descriptions = {
        "count": "Observed durable task timestamps",
        "sum": "Sum of observed durations in seconds",
        "average": "Average observed duration in seconds",
        "max": "Maximum observed duration in seconds",
    }
    values: dict[str, float | int] = {
        "count": summary.count,
        "sum": summary.total,
        "average": summary.average,
        "max": summary.maximum,
    }
    for suffix, value in values.items():
        _metric(
            lines,
            name=f"{prefix}_{suffix}",
            help_text=descriptions[suffix],
            value=value,
        )
        lines.append("")


def _collect_timings_and_retries() -> tuple[
    _TimingSummary,
    _TimingSummary,
    int,
]:
    from django_ray.models import RayTaskExecution

    zero_duration = Value(timedelta(0), output_field=DurationField())
    queue_wait_expression = Case(
        When(
            started_at__gte=F("created_at"),
            then=ExpressionWrapper(
                F("started_at") - F("created_at"),
                output_field=DurationField(),
            ),
        ),
        default=zero_duration,
        output_field=DurationField(),
    )
    eligible_at = Case(
        When(run_after__gt=F("created_at"), then=F("run_after")),
        default=F("created_at"),
        output_field=DateTimeField(),
    )
    claim_latency_expression = Case(
        When(
            started_at__gte=eligible_at,
            then=ExpressionWrapper(
                F("started_at") - eligible_at,
                output_field=DurationField(),
            ),
        ),
        default=zero_duration,
        output_field=DurationField(),
    )
    retry_expression = Case(
        When(attempt_number__gt=1, then=F("attempt_number") - Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    started = Q(started_at__isnull=False)
    aggregates = RayTaskExecution.objects.aggregate(
        queue_wait_count=Count("pk", filter=started),
        queue_wait_sum=Sum(queue_wait_expression, filter=started),
        queue_wait_max=Max(queue_wait_expression, filter=started),
        claim_latency_count=Count("pk", filter=started),
        claim_latency_sum=Sum(claim_latency_expression, filter=started),
        claim_latency_max=Max(claim_latency_expression, filter=started),
        retries=Sum(retry_expression),
    )

    def timing(prefix: str) -> _TimingSummary:
        total = aggregates[f"{prefix}_sum"] or timedelta(0)
        maximum = aggregates[f"{prefix}_max"] or timedelta(0)
        return _TimingSummary(
            count=int(aggregates[f"{prefix}_count"] or 0),
            total=total.total_seconds(),
            maximum=maximum.total_seconds(),
        )

    return (
        timing("queue_wait"),
        timing("claim_latency"),
        int(aggregates["retries"] or 0),
    )


def _collect_execution_duration() -> _TimingSummary:
    """Combine archived attempt durations with the unarchived current attempt."""
    from django_ray.models import RayTaskExecution, TaskAttempt

    zero_duration = Value(timedelta(0), output_field=DurationField())
    duration = Case(
        When(
            finished_at__gte=F("started_at"),
            then=ExpressionWrapper(
                F("finished_at") - F("started_at"),
                output_field=DurationField(),
            ),
        ),
        default=zero_duration,
        output_field=DurationField(),
    )
    completed = Q(started_at__isnull=False, finished_at__isnull=False)

    def aggregate(queryset) -> dict[str, object]:
        return queryset.aggregate(
            count=Count("pk", filter=completed),
            total=Sum(duration, filter=completed),
            maximum=Max(duration, filter=completed),
        )

    archived = aggregate(TaskAttempt.objects.all())
    matching_attempt = TaskAttempt.objects.filter(
        execution_id=OuterRef("pk"),
        attempt_number=OuterRef("attempt_number"),
    )
    current = aggregate(
        RayTaskExecution.objects.annotate(_attempt_archived=Exists(matching_attempt)).filter(
            _attempt_archived=False
        )
    )
    archived_total = archived["total"]
    current_total = current["total"]
    archived_maximum = archived["maximum"]
    current_maximum = current["maximum"]
    if not isinstance(archived_total, timedelta):
        archived_total = timedelta(0)
    if not isinstance(current_total, timedelta):
        current_total = timedelta(0)
    if not isinstance(archived_maximum, timedelta):
        archived_maximum = timedelta(0)
    if not isinstance(current_maximum, timedelta):
        current_maximum = timedelta(0)
    return _TimingSummary(
        count=int(archived["count"] or 0) + int(current["count"] or 0),
        total=(archived_total + current_total).total_seconds(),
        maximum=max(archived_maximum, current_maximum).total_seconds(),
    )


def _collect_failures() -> tuple[int, int]:
    from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

    archived = TaskAttempt.objects.filter(state=TaskState.FAILED).aggregate(
        failures=Count("pk"),
        timeouts=Count(
            "pk",
            filter=Q(error_message__startswith=_TIMEOUT_PREFIX),
        ),
    )

    matching_attempt = TaskAttempt.objects.filter(
        execution_id=OuterRef("pk"),
        attempt_number=OuterRef("attempt_number"),
    )
    current = (
        RayTaskExecution.objects.filter(state=TaskState.FAILED)
        .annotate(_attempt_archived=Exists(matching_attempt))
        .filter(_attempt_archived=False)
    )
    current_counts = current.aggregate(
        failures=Count("pk"),
        timeouts=Count(
            "pk",
            filter=Q(error_message__startswith=_TIMEOUT_PREFIX),
        ),
    )
    return (
        int(archived["failures"] or 0) + int(current_counts["failures"] or 0),
        int(archived["timeouts"] or 0) + int(current_counts["timeouts"] or 0),
    )


def _collect_lease_health(*, observed_at: datetime) -> dict[str, int]:
    from django_ray.models import TaskWorkerLease

    cutoff = observed_at - get_lease_duration()
    counts = TaskWorkerLease.objects.aggregate(
        healthy=Count(
            "pk",
            filter=Q(is_active=True, last_heartbeat_at__gte=cutoff),
        ),
        stale=Count(
            "pk",
            filter=Q(is_active=True, last_heartbeat_at__lt=cutoff),
        ),
        inactive=Count("pk", filter=Q(is_active=False)),
    )
    return {status: int(counts[status] or 0) for status in ("healthy", "stale", "inactive")}


def render_prometheus_metrics(
    *,
    queue_names: Sequence[str] = (),
    observed_at: datetime | None = None,
) -> str:
    """Render bounded-cardinality metrics from the current durable database state.

    Queue labels are emitted only for the explicit ``queue_names`` allowlist.
    Queue wait is ``started_at - created_at``. Claim latency is ``started_at -
    max(created_at, run_after)``. Execution duration is ``finished_at - started_at``.
    All values are database snapshot gauges rather than process-local counters.
    """
    from django_ray.models import RayTaskExecution, TaskState

    if isinstance(queue_names, str):
        raise TypeError("queue_names must be a sequence of queue-name strings")
    if any(not isinstance(queue_name, str) for queue_name in queue_names):
        raise TypeError("queue_names entries must be strings")
    allowed_queues = sorted(set(queue_names))
    now = observed_at or datetime.now(UTC)

    task_counts = {
        row["state"]: row["count"]
        for row in RayTaskExecution.objects.values("state").annotate(count=Count("pk"))
    }
    queue_counts = {
        row["queue_name"]: row["count"]
        for row in (
            RayTaskExecution.objects.filter(
                state=TaskState.QUEUED,
                queue_name__in=allowed_queues,
            )
            .values("queue_name")
            .annotate(count=Count("pk"))
        )
    }
    queue_wait, claim_latency, retries = _collect_timings_and_retries()
    execution_duration = _collect_execution_duration()
    failures, timeouts = _collect_failures()
    leases = _collect_lease_health(observed_at=now)

    lines: list[str] = []
    _metric(
        lines,
        name="django_ray_observability_schema_info",
        help_text="django-ray observability schema version",
        value=1,
        labels={"version": str(OBSERVABILITY_SCHEMA_VERSION)},
    )
    lines.append("")
    _metric_family(
        lines,
        name="django_ray_tasks_total",
        help_text="Total tasks by state",
        values=[({"state": str(state)}, task_counts.get(state, 0)) for state in TaskState],
    )
    lines.append("")
    _metric(
        lines,
        name="django_ray_tasks_queued",
        help_text="Current queued tasks",
        value=task_counts.get(TaskState.QUEUED, 0),
    )
    lines.append("")
    _metric(
        lines,
        name="django_ray_tasks_running",
        help_text="Current running tasks",
        value=task_counts.get(TaskState.RUNNING, 0),
    )
    lines.append("")
    if allowed_queues:
        _metric_family(
            lines,
            name="django_ray_queue_depth",
            help_text="Tasks queued per explicitly allowed queue",
            values=[
                ({"queue": queue_name}, queue_counts.get(queue_name, 0))
                for queue_name in allowed_queues
            ],
        )
        lines.append("")

    _timing_metrics(lines, prefix="django_ray_queue_wait_seconds", summary=queue_wait)
    _timing_metrics(lines, prefix="django_ray_claim_latency_seconds", summary=claim_latency)
    _timing_metrics(
        lines,
        prefix="django_ray_execution_duration_seconds",
        summary=execution_duration,
    )
    _metric(
        lines,
        name="django_ray_retries_recorded",
        help_text="Retry transitions represented by current attempt numbers",
        value=retries,
    )
    lines.append("")
    _metric(
        lines,
        name="django_ray_failures_recorded",
        help_text="Archived and current unarchived failed attempts",
        value=failures,
    )
    lines.append("")
    _metric(
        lines,
        name="django_ray_timeouts_recorded",
        help_text="Archived and current unarchived timeout attempts",
        value=timeouts,
    )
    lines.append("")
    _metric_family(
        lines,
        name="django_ray_worker_leases",
        help_text="Worker leases by fixed health classification",
        values=[
            ({"status": status}, leases[status]) for status in ("healthy", "stale", "inactive")
        ],
    )
    return "\n".join(lines) + "\n"


__all__ = ["render_prometheus_metrics"]
