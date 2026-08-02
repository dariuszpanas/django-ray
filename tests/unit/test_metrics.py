"""Tests for reusable Prometheus observability metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client.parser import text_string_to_metric_families

from django_ray.metrics import render_prometheus_metrics
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState, TaskWorkerLease


def _samples(metrics: str) -> dict[str, float]:
    return {
        sample.name + str(dict(sample.labels)): sample.value
        for family in text_string_to_metric_families(metrics)
        for sample in family.samples
    }


def test_empty_metrics_are_valid_and_omit_queue_labels(db, settings) -> None:
    settings.DJANGO_RAY = {"WORKER_LEASE_SECONDS": 60}

    metrics = render_prometheus_metrics(observed_at=datetime(2026, 7, 19, 12, tzinfo=UTC))
    parsed = list(text_string_to_metric_families(metrics))

    assert parsed
    assert 'django_ray_observability_schema_info{version="1"} 1' in metrics
    assert 'django_ray_tasks_total{state="QUEUED"} 0' in metrics
    assert "django_ray_queue_depth" not in metrics
    assert "django_ray_queue_wait_seconds_average 0" in metrics
    assert metrics.endswith("\n")


@pytest.mark.parametrize("queue_names", ["default", ["default", 3]])
def test_queue_allowlist_requires_string_sequence(db, queue_names) -> None:
    with pytest.raises(TypeError, match="queue_names"):
        render_prometheus_metrics(queue_names=queue_names)


def test_metrics_use_durable_state_and_bounded_labels(db, settings) -> None:
    settings.DJANGO_RAY = {"WORKER_LEASE_SECONDS": 60}
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)
    odd_queue = 'odd"\\name\nnext'

    timing_positive = RayTaskExecution.objects.create(
        task_id="queued-alpha",
        callable_path="tasks.echo",
        queue_name="alpha",
        state=TaskState.QUEUED,
        created_at=now,
    )
    RayTaskExecution.objects.create(
        task_id="queued-odd",
        callable_path="tasks.echo",
        queue_name=odd_queue,
        state=TaskState.QUEUED,
        created_at=now,
    )
    RayTaskExecution.objects.create(
        task_id="queued-not-allowed",
        callable_path="tasks.echo",
        queue_name="dynamic-customer-123",
        state=TaskState.QUEUED,
        created_at=now,
    )
    RayTaskExecution.objects.create(
        task_id="timing-positive",
        callable_path="tasks.echo",
        state=TaskState.SUCCEEDED,
        attempt_number=3,
        created_at=now - timedelta(seconds=15),
        run_after=now - timedelta(seconds=9),
        started_at=now - timedelta(seconds=5),
        finished_at=now,
    )
    TaskAttempt.objects.create(
        execution=timing_positive,
        attempt_number=2,
        state=TaskState.FAILED,
        started_at=now - timedelta(seconds=12),
        finished_at=now - timedelta(seconds=10),
    )
    RayTaskExecution.objects.create(
        task_id="timing-negative",
        callable_path="tasks.echo",
        state=TaskState.SUCCEEDED,
        created_at=now,
        run_after=now - timedelta(seconds=1),
        started_at=now - timedelta(seconds=1),
        finished_at=now - timedelta(seconds=2),
    )
    archived_failure = RayTaskExecution.objects.create(
        task_id="archived-failure",
        callable_path="tasks.fail",
        state=TaskState.FAILED,
        error_message="ordinary failure",
    )
    TaskAttempt.objects.create(
        execution=archived_failure,
        attempt_number=1,
        state=TaskState.FAILED,
        error_message="ordinary failure",
    )
    RayTaskExecution.objects.create(
        task_id="current-timeout",
        callable_path="tasks.slow",
        state=TaskState.FAILED,
        error_message="Task timed out after 30 seconds",
    )
    RayTaskExecution.objects.create(
        task_id="expired-before-submission",
        callable_path="tasks.slow",
        state=TaskState.EXPIRED,
        error_message="Task expired before execution after exceeding its queued-wait deadline",
    )
    archived_timeout = RayTaskExecution.objects.create(
        task_id="archived-timeout",
        callable_path="tasks.slow",
        state=TaskState.QUEUED,
        attempt_number=2,
    )
    TaskAttempt.objects.create(
        execution=archived_timeout,
        attempt_number=1,
        state=TaskState.FAILED,
        error_message="Task timed out after 10 seconds",
    )
    archived_expiration = RayTaskExecution.objects.create(
        task_id="archived-expiration",
        callable_path="tasks.slow",
        state=TaskState.QUEUED,
        attempt_number=2,
    )
    TaskAttempt.objects.create(
        execution=archived_expiration,
        attempt_number=1,
        state=TaskState.EXPIRED,
        error_message="Task expired before execution after exceeding its queued-wait deadline",
    )

    TaskWorkerLease.objects.create(
        worker_id="healthy",
        hostname="host",
        pid=1,
        last_heartbeat_at=now,
    )
    TaskWorkerLease.objects.create(
        worker_id="stale",
        hostname="host",
        pid=2,
        last_heartbeat_at=now - timedelta(seconds=61),
    )
    TaskWorkerLease.objects.create(
        worker_id="inactive",
        hostname="host",
        pid=3,
        last_heartbeat_at=now,
        is_active=False,
        stopped_at=now,
    )

    metrics = render_prometheus_metrics(
        queue_names=[odd_queue, "alpha", "empty", "alpha"],
        observed_at=now,
    )
    samples = _samples(metrics)

    assert 'django_ray_queue_depth{queue="alpha"} 1' in metrics
    assert 'django_ray_queue_depth{queue="empty"} 0' in metrics
    assert 'queue="odd\\"\\\\name\\nnext"' in metrics
    assert "dynamic-customer-123" not in metrics
    assert samples["django_ray_queue_wait_seconds_count{}"] == 2
    assert samples["django_ray_queue_wait_seconds_sum{}"] == 10
    assert samples["django_ray_queue_wait_seconds_average{}"] == 5
    assert samples["django_ray_queue_wait_seconds_max{}"] == 10
    assert samples["django_ray_claim_latency_seconds_sum{}"] == 4
    assert samples["django_ray_execution_duration_seconds_count{}"] == 3
    assert samples["django_ray_execution_duration_seconds_sum{}"] == 7
    assert samples["django_ray_execution_duration_seconds_max{}"] == 5
    assert samples["django_ray_retries_recorded{}"] == 4
    assert samples["django_ray_failures_recorded{}"] == 4
    assert samples["django_ray_timeouts_recorded{}"] == 2
    assert samples["django_ray_tasks_expired{}"] == 1
    assert samples["django_ray_expirations_recorded{}"] == 2
    assert samples["django_ray_worker_leases{'status': 'healthy'}"] == 1
    assert samples["django_ray_worker_leases{'status': 'stale'}"] == 1
    assert samples["django_ray_worker_leases{'status': 'inactive'}"] == 1


def test_metrics_scrape_uses_bounded_aggregate_queries(db, django_assert_num_queries):
    with django_assert_num_queries(8):
        render_prometheus_metrics(queue_names=["default"])
