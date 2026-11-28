"""Prometheus text rendering from django-ray's durable database state."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.db.models import (
    Case,
    CharField,
    Count,
    DateTimeField,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    FilteredRelation,
    IntegerField,
    Max,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Left

from django_ray.execution_protocol import LEGACY_EXECUTION_PROTOCOL_VERSION
from django_ray.observability import OBSERVABILITY_SCHEMA_VERSION
from django_ray.runner.leasing import (
    expired_lease_q,
    get_lease_duration,
    live_lease_freshness_q,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


_TIMEOUT_PREFIX = "Task timed out after "
_TIMEOUT_PREFIX_LEN = len(_TIMEOUT_PREFIX)
_OTHER_STATE_BUCKET = "_OTHER_"
_METRICS_CACHE_MAX_ENTRIES = 64
_DEFAULT_METRICS_CACHE_TTL_SECONDS = 5.0

_METRICS_CACHE_LOCK = threading.Lock()
_METRICS_CACHE: dict[tuple[tuple[str, ...], float, str], tuple[float, str]] = {}
_METRICS_INFLIGHT: dict[tuple[tuple[str, ...], float, str], threading.Event] = {}


def clear_metrics_cache() -> None:
    """Clear the bounded in-process Prometheus scrape cache."""
    with _METRICS_CACHE_LOCK:
        _METRICS_CACHE.clear()


def _resolve_metrics_cache_ttl(cache_ttl_seconds: float | None) -> float:
    if cache_ttl_seconds is None:
        try:
            from django_ray.conf.settings import get_settings

            configured = get_settings().get(
                "METRICS_CACHE_TTL_SECONDS",
                _DEFAULT_METRICS_CACHE_TTL_SECONDS,
            )
        except ImproperlyConfigured:
            configured = _DEFAULT_METRICS_CACHE_TTL_SECONDS
        raw_ttl = configured
    else:
        raw_ttl = cache_ttl_seconds

    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int | float):
        raise TypeError("cache_ttl_seconds must be a finite non-negative number")
    ttl = float(raw_ttl)
    if not math.isfinite(ttl) or ttl < 0.0:
        raise ValueError("cache_ttl_seconds must be a finite non-negative number")
    return ttl


@dataclass
class _TimingSummary:
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0


@dataclass(frozen=True, slots=True)
class _ExecutionMetricsSnapshot:
    task_counts: dict[str, int]
    protocol_task_counts: dict[tuple[str, str], int]
    queue_wait: _TimingSummary
    claim_latency: _TimingSummary
    unarchived_duration: _TimingSummary
    retries: int
    unarchived_failures: int
    unarchived_timeouts: int
    unarchived_expirations: int


@dataclass(frozen=True, slots=True)
class _ArchivedAttemptMetricsSnapshot:
    duration: _TimingSummary
    failures: int
    timeouts: int
    expirations: int


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


def _duration_seconds(value: object) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else 0.0


def _collect_execution_metrics() -> _ExecutionMetricsSnapshot:
    """Collect task counts, timings, retries, and unarchived attempt metrics in one query."""
    from django_ray.models import RayTaskExecution, TaskState

    valid_states = frozenset(str(state) for state in TaskState)
    zero_duration = Value(timedelta(0), output_field=DurationField())
    first_attempt = FilteredRelation(
        "attempts",
        condition=Q(attempts__attempt_number=1, attempt_number__gt=1),
    )
    current_attempt = FilteredRelation(
        "attempts",
        condition=(
            Q(attempts__attempt_number=F("attempt_number"))
            & (
                Q(state__in=(TaskState.FAILED, TaskState.EXPIRED))
                | Q(started_at__isnull=False, finished_at__isnull=False)
            )
        ),
    )
    first_started_at = Coalesce(
        F("first_attempt__started_at"),
        F("started_at"),
        output_field=DateTimeField(),
    )
    guarded_error_prefix = Case(
        When(
            state=TaskState.FAILED,
            error_message__isnull=False,
            then=Left("error_message", _TIMEOUT_PREFIX_LEN),
        ),
        default=Value(None),
        output_field=CharField(),
    )
    queue_wait_expression = Case(
        When(
            Q(_first_started_at__gte=F("created_at")),
            then=ExpressionWrapper(
                F("_first_started_at") - F("created_at"),
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
    duration_expression = Case(
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
    retry_expression = Case(
        When(attempt_number__gt=1, then=F("attempt_number") - Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    bounded_state = Case(
        When(state__in=tuple(valid_states), then=F("state")),
        default=Value(_OTHER_STATE_BUCKET),
        output_field=CharField(),
    )

    first_started = Q(first_attempt__started_at__isnull=False) | Q(started_at__isnull=False)
    started = Q(started_at__isnull=False)
    unarchived = Q(current_attempt__pk__isnull=True)
    unarchived_completed = unarchived & Q(started_at__isnull=False, finished_at__isnull=False)
    unarchived_timeout = unarchived & Q(
        state=TaskState.FAILED,
        error_message__isnull=False,
        _error_prefix=_TIMEOUT_PREFIX,
    )

    rows = list(
        RayTaskExecution.objects.annotate(
            first_attempt=first_attempt,
            current_attempt=current_attempt,
            _state_bucket=bounded_state,
        )
        .alias(
            _first_started_at=first_started_at,
            _error_prefix=guarded_error_prefix,
        )
        .values("_state_bucket")
        .annotate(
            count=Count("pk"),
            protocol_1=Count(
                "pk",
                filter=Q(execution_protocol_version=LEGACY_EXECUTION_PROTOCOL_VERSION),
            ),
            protocol_other=Count(
                "pk",
                filter=~Q(execution_protocol_version=LEGACY_EXECUTION_PROTOCOL_VERSION),
            ),
            retries=Sum(retry_expression),
            queue_wait_count=Count("pk", filter=first_started),
            queue_wait_sum=Sum(queue_wait_expression, filter=first_started),
            queue_wait_max=Max(queue_wait_expression, filter=first_started),
            claim_latency_count=Count("pk", filter=started),
            claim_latency_sum=Sum(claim_latency_expression, filter=started),
            claim_latency_max=Max(claim_latency_expression, filter=started),
            unarchived_duration_count=Count("pk", filter=unarchived_completed),
            unarchived_duration_sum=Sum(duration_expression, filter=unarchived_completed),
            unarchived_duration_max=Max(duration_expression, filter=unarchived_completed),
            unarchived_failures=Count("pk", filter=unarchived & Q(state=TaskState.FAILED)),
            unarchived_timeouts=Count("pk", filter=unarchived_timeout),
            unarchived_expirations=Count("pk", filter=unarchived & Q(state=TaskState.EXPIRED)),
        )
    )

    task_counts: dict[str, int] = {}
    protocol_task_counts: dict[tuple[str, str], int] = {}
    queue_wait = _TimingSummary()
    claim_latency = _TimingSummary()
    unarchived_duration = _TimingSummary()
    retries = 0
    unarchived_failures = 0
    unarchived_timeouts = 0
    unarchived_expirations = 0

    for row in rows:
        state_bucket = row["_state_bucket"]
        if state_bucket in valid_states:
            task_counts[state_bucket] = int(row["count"] or 0)
            protocol_task_counts[(state_bucket, "1")] = int(row["protocol_1"] or 0)
            protocol_task_counts[(state_bucket, "other")] = int(row["protocol_other"] or 0)
        retries += int(row["retries"] or 0)
        queue_wait.count += int(row["queue_wait_count"] or 0)
        queue_wait.total += _duration_seconds(row["queue_wait_sum"])
        queue_wait.maximum = max(queue_wait.maximum, _duration_seconds(row["queue_wait_max"]))
        claim_latency.count += int(row["claim_latency_count"] or 0)
        claim_latency.total += _duration_seconds(row["claim_latency_sum"])
        claim_latency.maximum = max(
            claim_latency.maximum,
            _duration_seconds(row["claim_latency_max"]),
        )
        unarchived_duration.count += int(row["unarchived_duration_count"] or 0)
        unarchived_duration.total += _duration_seconds(row["unarchived_duration_sum"])
        unarchived_duration.maximum = max(
            unarchived_duration.maximum,
            _duration_seconds(row["unarchived_duration_max"]),
        )
        unarchived_failures += int(row["unarchived_failures"] or 0)
        unarchived_timeouts += int(row["unarchived_timeouts"] or 0)
        unarchived_expirations += int(row["unarchived_expirations"] or 0)

    return _ExecutionMetricsSnapshot(
        task_counts=task_counts,
        protocol_task_counts=protocol_task_counts,
        queue_wait=queue_wait,
        claim_latency=claim_latency,
        unarchived_duration=unarchived_duration,
        retries=retries,
        unarchived_failures=unarchived_failures,
        unarchived_timeouts=unarchived_timeouts,
        unarchived_expirations=unarchived_expirations,
    )


def _collect_archived_attempt_metrics() -> _ArchivedAttemptMetricsSnapshot:
    """Collect archived attempt durations, failures, timeouts, and expirations in one query."""
    from django_ray.models import TaskAttempt, TaskState

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
    timeout_filter = Q(
        state=TaskState.FAILED,
        error_message__isnull=False,
        _error_prefix=_TIMEOUT_PREFIX,
    )
    guarded_attempt_error_prefix = Case(
        When(
            state=TaskState.FAILED,
            error_message__isnull=False,
            then=Left("error_message", _TIMEOUT_PREFIX_LEN),
        ),
        default=Value(None),
        output_field=CharField(),
    )
    archived = TaskAttempt.objects.alias(
        _error_prefix=guarded_attempt_error_prefix,
    ).aggregate(
        duration_count=Count("pk", filter=completed),
        duration_sum=Sum(duration, filter=completed),
        duration_max=Max(duration, filter=completed),
        failures=Count("pk", filter=Q(state=TaskState.FAILED)),
        timeouts=Count("pk", filter=timeout_filter),
        expirations=Count("pk", filter=Q(state=TaskState.EXPIRED)),
    )
    return _ArchivedAttemptMetricsSnapshot(
        duration=_TimingSummary(
            count=int(archived["duration_count"] or 0),
            total=_duration_seconds(archived["duration_sum"]),
            maximum=_duration_seconds(archived["duration_max"]),
        ),
        failures=int(archived["failures"] or 0),
        timeouts=int(archived["timeouts"] or 0),
        expirations=int(archived["expirations"] or 0),
    )


def _collect_lease_health(*, observed_at: datetime | None = None) -> dict[str, int] | None:
    from django_ray.lease_policy import LeasePolicyError, policy_rows
    from django_ray.models import TaskWorkerLease

    duration = get_lease_duration()
    authority = Exists(policy_rows())
    # Use the database clock (Now()) for live scrapes so host clock skew never
    # misclassifies healthy workers as stale; honor explicit observed_at when
    # supplied by deterministic callers/tests.
    if observed_at is None:
        healthy_q = Q(is_active=True) & live_lease_freshness_q(duration)
        stale_q = Q(is_active=True) & expired_lease_q(duration)
    else:
        cutoff = observed_at - duration
        healthy_q = Q(is_active=True) & (
            Q(
                lease_expires_at__isnull=False,
                lease_expires_at__gte=observed_at,
                last_heartbeat_at__gte=cutoff,
            )
            | Q(
                lease_expires_at__isnull=True,
                last_heartbeat_at__gte=cutoff,
            )
        )
        stale_q = Q(is_active=True) & (
            Q(lease_expires_at__isnull=False, lease_expires_at__lt=observed_at)
            | Q(last_heartbeat_at__lt=cutoff)
        )
    # Authority and health counts share one statement snapshot, including when
    # the lease table is empty. A missing/mismatched policy yields NULL rather
    # than a false zero, without adding before/after queries to every scrape.
    filters = {
        "healthy": healthy_q,
        "stale": stale_q,
        "inactive": Q(is_active=False),
    }
    try:
        counts = TaskWorkerLease.objects.aggregate(
            **{
                name: Case(
                    When(authority, then=Count("pk", filter=condition)),
                    default=Value(None),
                    output_field=IntegerField(),
                )
                for name, condition in filters.items()
            }
        )
    except DatabaseError:
        raise LeasePolicyError("lease_policy_unavailable") from None
    if any(value is None for value in counts.values()):
        return None
    return {status: int(count) for status, count in counts.items()}


def _render_prometheus_metrics_uncached(
    *,
    allowed_queues: list[str],
    observed_at: datetime | None,
) -> str:
    from django_ray.lease_policy import LeasePolicyError
    from django_ray.models import RayTaskExecution, TaskState

    execution_metrics = _collect_execution_metrics()
    archived_metrics = _collect_archived_attempt_metrics()
    queue_counts: dict[str, int] = {}
    if allowed_queues:
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
    try:
        leases = _collect_lease_health(observed_at=observed_at)
    except LeasePolicyError:
        leases = None

    task_counts = execution_metrics.task_counts
    protocol_task_counts = execution_metrics.protocol_task_counts
    queue_wait = execution_metrics.queue_wait
    claim_latency = execution_metrics.claim_latency
    execution_duration = _TimingSummary(
        count=archived_metrics.duration.count + execution_metrics.unarchived_duration.count,
        total=archived_metrics.duration.total + execution_metrics.unarchived_duration.total,
        maximum=max(
            archived_metrics.duration.maximum,
            execution_metrics.unarchived_duration.maximum,
        ),
    )
    retries = execution_metrics.retries
    failures = archived_metrics.failures + execution_metrics.unarchived_failures
    timeouts = archived_metrics.timeouts + execution_metrics.unarchived_timeouts
    expirations = archived_metrics.expirations + execution_metrics.unarchived_expirations

    task_state_values = [({"state": str(state)}, task_counts.get(state, 0)) for state in TaskState]
    protocol_state_values = [
        (
            {"protocol": protocol, "state": str(state)},
            protocol_task_counts.get((state, protocol), 0),
        )
        for protocol in ("1", "other")
        for state in TaskState
    ]

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
        name="django_ray_tasks",
        help_text="Current tasks by state",
        values=task_state_values,
    )
    lines.append("")
    _metric_family(
        lines,
        name="django_ray_tasks_total",
        help_text="Total tasks by state (compatibility alias for django_ray_tasks)",
        values=task_state_values,
    )
    lines.append("")
    _metric_family(
        lines,
        name="django_ray_tasks_by_execution_protocol",
        help_text="Current tasks by bounded execution-protocol bucket and state",
        values=protocol_state_values,
    )
    lines.append("")
    _metric_family(
        lines,
        name="django_ray_tasks_by_execution_protocol_total",
        help_text="Total tasks by bounded execution-protocol bucket and state",
        values=protocol_state_values,
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
    _metric(
        lines,
        name="django_ray_tasks_expired",
        help_text="Current tasks expired before execution",
        value=task_counts.get(TaskState.EXPIRED, 0),
    )
    lines.append("")
    _metric(
        lines,
        name="django_ray_expirations_recorded",
        help_text="Recorded queue expirations across current and archived attempts",
        value=expirations,
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
            ({"status": status}, leases[status])
            for status in ("healthy", "stale", "inactive")
            if leases is not None
        ],
    )
    lines.append("")
    _metric(
        lines,
        name="django_ray_worker_lease_policy_available",
        help_text="Whether matching common lease authority was observed consistently",
        value=int(leases is not None),
    )
    return "\n".join(lines) + "\n"


def render_prometheus_metrics(
    *,
    queue_names: Sequence[str] = (),
    observed_at: datetime | None = None,
    cache_ttl_seconds: float | None = None,
) -> str:
    """Render bounded-cardinality metrics from the current durable database state.

    Queue labels are emitted only for the explicit ``queue_names`` allowlist.
    Queue wait uses the first observed attempt start timestamp minus ``created_at``
    so retried tasks are not distorted by later backoff delays. Claim latency is
    ``started_at - max(created_at, run_after)``. Execution duration is
    ``finished_at - started_at``. All values are database snapshot gauges rather
    than process-local counters.

    When ``cache_ttl_seconds`` (or ``DJANGO_RAY["METRICS_CACHE_TTL_SECONDS"]``) is
    positive and ``observed_at`` is omitted, scrapes share a bounded single-flight
    TTL cache to protect the database against concurrent scrape storms.
    """
    from django_ray.models import RayTaskExecution

    if isinstance(queue_names, str):
        raise TypeError("queue_names must be a sequence of queue-name strings")
    if any(not isinstance(queue_name, str) for queue_name in queue_names):
        raise TypeError("queue_names entries must be strings")
    ttl = _resolve_metrics_cache_ttl(cache_ttl_seconds)
    allowed_queues = sorted(set(queue_names))

    if observed_at is not None or ttl <= 0.0:
        return _render_prometheus_metrics_uncached(
            allowed_queues=allowed_queues,
            observed_at=observed_at,
        )

    cache_key = (
        tuple(allowed_queues),
        get_lease_duration().total_seconds(),
        RayTaskExecution.objects.db,
    )
    while True:
        with _METRICS_CACHE_LOCK:
            now_mono = time.monotonic()
            cached = _METRICS_CACHE.get(cache_key)
            if cached is not None and cached[0] > now_mono:
                return cached[1]
            inflight = _METRICS_INFLIGHT.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                _METRICS_INFLIGHT[cache_key] = inflight
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            inflight.wait()
            continue

        try:
            rendered = _render_prometheus_metrics_uncached(
                allowed_queues=allowed_queues,
                observed_at=None,
            )
            expires_at = time.monotonic() + ttl
            with _METRICS_CACHE_LOCK:
                if len(_METRICS_CACHE) >= _METRICS_CACHE_MAX_ENTRIES:
                    now_mono = time.monotonic()
                    expired_keys = [
                        key
                        for key, (expiry, _value) in _METRICS_CACHE.items()
                        if expiry <= now_mono
                    ]
                    for key in expired_keys:
                        _METRICS_CACHE.pop(key, None)
                    if len(_METRICS_CACHE) >= _METRICS_CACHE_MAX_ENTRIES:
                        oldest_key = min(_METRICS_CACHE, key=lambda key: _METRICS_CACHE[key][0])
                        _METRICS_CACHE.pop(oldest_key, None)
                _METRICS_CACHE[cache_key] = (expires_at, rendered)
            return rendered
        finally:
            with _METRICS_CACHE_LOCK:
                _METRICS_INFLIGHT.pop(cache_key, None)
                inflight.set()


__all__ = ["clear_metrics_cache", "render_prometheus_metrics"]
