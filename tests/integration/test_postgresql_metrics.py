"""PostgreSQL integration evidence for observability aggregates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection

from django_ray.metrics import render_prometheus_metrics
from django_ray.models import RayTaskExecution, TaskAttempt, TaskState

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def test_prometheus_duration_aggregates_execute_on_postgresql(settings) -> None:
    settings.DJANGO_RAY = {"WORKER_LEASE_SECONDS": 60}
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)
    execution = RayTaskExecution.objects.create(
        task_id="postgres-metrics-1",
        callable_path="tasks.echo",
        queue_name="default",
        state=TaskState.SUCCEEDED,
        attempt_number=2,
    )
    RayTaskExecution.objects.filter(pk=execution.pk).update(
        created_at=now - timedelta(seconds=10),
        run_after=now - timedelta(seconds=8),
        started_at=now - timedelta(seconds=4),
        finished_at=now,
    )
    TaskAttempt.objects.create(
        execution=execution,
        attempt_number=1,
        state=TaskState.FAILED,
        started_at=now - timedelta(seconds=8),
        finished_at=now - timedelta(seconds=6),
    )

    metrics = render_prometheus_metrics(queue_names=("default",), observed_at=now)

    assert "django_ray_queue_wait_seconds_sum 6" in metrics
    assert "django_ray_claim_latency_seconds_sum 4" in metrics
    assert "django_ray_execution_duration_seconds_count 2" in metrics
    assert "django_ray_execution_duration_seconds_sum 6" in metrics
    assert "django_ray_retries_recorded 1" in metrics
    assert "django_ray_failures_recorded 1" in metrics
