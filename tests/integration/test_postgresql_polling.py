"""PostgreSQL integration evidence for production worker polling."""

from __future__ import annotations

import json
import math
from io import StringIO

import pytest
from django.db import connection
from django.db.models import Q

from django_ray.management.commands.django_ray_benchmark_polling import Command
from django_ray.models import RayTaskExecution, TaskWorkerLease

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    """Keep the default SQLite suite fast while making this gate explicit."""
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def test_production_claim_benchmark_records_repeatable_metrics_and_cleans_up() -> None:
    """Exercise real worker claims, row locks, thread connections, and JSON evidence."""
    command = Command()
    command.stdout = StringIO()

    command.handle(
        workers=2,
        tasks=8,
        idle_seconds=0.15,
        enqueue_interval_seconds=0.01,
        base_interval_seconds=0.02,
        max_interval_seconds=0.08,
        overlap_window_ms=10.0,
        seed=53,
        barrier_timeout_seconds=5.0,
        json_output=True,
    )

    payload = json.loads(command.stdout.getvalue())
    assert payload["environment"]["database"] == "postgresql"
    assert payload["environment"]["workers"] == 2
    assert payload["environment"]["tasks_per_phase"] == 8
    assert payload["environment"]["seed"] == 53
    assert payload["environment"]["django_ray_schema_version"] != "unmigrated"

    assert [result["policy"] for result in payload["results"]] == ["fixed", "adaptive"]
    for result in payload["results"]:
        assert result["tasks_per_phase"] == 8
        assert result["idle_claim_queries_per_worker_second"] > 0
        assert (
            result["idle_total_sql_per_worker_second"]
            >= result["idle_claim_queries_per_worker_second"]
        )
        assert result["idle_peak_overlapping_workers"] >= 0
        assert 0 <= result["idle_cross_worker_overlap_ratio"] <= 1
        assert result["claim_latency_p50_ms"] >= 0
        assert result["claim_latency_p95_ms"] >= result["claim_latency_p50_ms"]
        assert result["burst_claim_throughput_per_second"] > 0
        assert all(math.isfinite(value) for key, value in result.items() if key not in {"policy"})

    assert not RayTaskExecution.objects.filter(
        Q(task_id__startswith="poll-latency-") | Q(task_id__startswith="poll-throughput-")
    ).exists()
    assert not TaskWorkerLease.objects.filter(worker_id__startswith="benchmark-").exists()
