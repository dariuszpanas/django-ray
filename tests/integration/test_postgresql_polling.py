"""PostgreSQL integration evidence for production worker polling."""

from __future__ import annotations

import json
import math
from io import StringIO

import pytest
from django.db import connection
from django.db.models import Q
from django.test import Client
from django.test.utils import CaptureQueriesContext

from django_ray.management.commands.django_ray_benchmark_polling import Command
from django_ray.models import RayTaskExecution, TaskState, TaskWorkerLease
from testproject import api as testproject_api

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    """Keep the default SQLite suite fast while making this gate explicit."""
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


@pytest.fixture
def api_client(settings) -> Client:
    token = settings.DJANGO_API_TOKEN
    assert isinstance(token, str) and token
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


def test_postgresql_task_status_uses_database_byte_guards(api_client: Client) -> None:
    protected = "\u00e9" * testproject_api._TASK_STATUS_INPUT_MAX_BYTES
    execution = RayTaskExecution.objects.create(
        task_id="postgresql-bounded-task-status",
        callable_path="missing.module.callable",
        state=TaskState.QUEUED,
        args_json=json.dumps([protected], ensure_ascii=False),
        kwargs_json="{}",
        result_data=json.dumps({"protected": protected}, ensure_ascii=False),
    )

    with CaptureQueriesContext(connection) as queries:
        response = api_client.get(f"/api/tasks/{execution.task_id}")

    assert response.status_code == 200
    assert response.json()["input_omission_reason"] == "stored_input_exceeds_status_limit"
    assert response.json()["args"] is None
    assert protected not in response.content.decode()
    task_selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
        and "django_ray_raytaskexecution" in query["sql"]
    ]
    assert len(task_selects) == 1
    assert "OCTET_LENGTH(" in task_selects[0]
    assert "result_data" not in task_selects[0]


def test_postgresql_workflow_poll_guards_diagnostics_before_transfer(
    api_client: Client,
) -> None:
    protected = "\u00e9" * testproject_api._POLL_DIAGNOSTIC_MAX_BYTES
    execution = RayTaskExecution.objects.create(
        task_id="postgresql-bounded-runtime-env-poll",
        callable_path="testproject.apps.cluster_tasks.tasks.runtime_env_probe",
        state=TaskState.FAILED,
        runtime_env_hash="a" * 64,
        result_data=json.dumps({"protected": protected}, ensure_ascii=False),
        error_message=protected,
        error_traceback=protected,
    )

    with CaptureQueriesContext(connection) as queries:
        response = api_client.get(f"/api/cluster/runtime-env/{execution.task_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"] is None
    assert payload["error"] is None
    assert payload["result_omission_reason"] == "stored_result_exceeds_poll_limit"
    assert payload["error_omission_reason"] == "stored_error_exceeds_poll_limit"
    assert protected not in response.content.decode()
    task_selects = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith("SELECT")
        and "django_ray_raytaskexecution" in query["sql"]
    ]
    assert len(task_selects) == 1
    assert "OCTET_LENGTH(" in task_selects[0]
    assert "error_traceback" not in task_selects[0]


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
