"""PostgreSQL integration evidence for production worker polling."""

from __future__ import annotations

import json
import math
from io import StringIO
from unittest.mock import Mock

import pytest
from django.core.management.base import CommandError
from django.db import connection
from django.db.models import Q
from django.test import Client
from django.test.utils import CaptureQueriesContext

import django_ray.management.commands.django_ray_benchmark_polling as benchmark
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


@pytest.mark.parametrize(
    ("disable_broad_interceptor", "message"),
    [
        (False, "unrecognized task-row locking SELECT"),
        (True, "protected application processing boundary"),
    ],
)
def test_production_claim_capture_drift_cannot_mutate_or_process(
    monkeypatch,
    disable_broad_interceptor: bool,
    message: str,
) -> None:
    queue_name = f"capture-drift-{disable_broad_interceptor}"
    execution = RayTaskExecution.objects.create(
        task_id=f"capture-drift-task-{disable_broad_interceptor}",
        callable_path="django_ray.benchmarks.polling_probe",
        queue_name=queue_name,
        state=TaskState.QUEUED,
        args_json="[]",
        kwargs_json="{}",
    )
    process_mock = Mock()
    monkeypatch.setattr(benchmark, "_is_production_claim_query", lambda _sql: False)
    if disable_broad_interceptor:
        monkeypatch.setattr(benchmark, "_is_claim_query", lambda _sql: False)
    monkeypatch.setattr(benchmark.WorkerCommand, "process_task", process_mock)

    with pytest.raises(CommandError, match=message):
        Command._capture_production_claim_sql(queue_name=queue_name, query_limit=1)

    execution.refresh_from_db()
    assert execution.state == TaskState.QUEUED
    assert execution.claimed_by_worker is None
    assert execution.execution_generation == 0
    process_mock.assert_not_called()
    assert not TaskWorkerLease.objects.filter(queue_name=queue_name).exists()


def test_production_claim_benchmark_records_repeatable_metrics_and_cleans_up() -> None:
    """Exercise real worker claims, row locks, thread connections, and JSON evidence."""
    foreign_protocol_row = RayTaskExecution.objects.create(
        task_id="poll-protocol-foreign-sentinel",
        callable_path="django_ray.benchmarks.polling_probe",
        queue_name="foreign-protocol-evidence-queue",
        state=TaskState.QUEUED,
        args_json="[]",
        kwargs_json="{}",
    )
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

    evidence = payload["protocol_predicate_evidence"]
    assert evidence["schema_version"] == 1
    assert evidence["method"] == "paired_counterbalanced_production_claim"
    assert evidence["seeded_rows"] == 8
    assert evidence["query_limit"] == 8
    assert evidence["timed_pairs"] == 12
    assert evidence["production_first_pairs"] == 6
    assert evidence["control_first_pairs"] == 6
    assert evidence["seeded_protocol_version"] == 1
    assert evidence["protocol_minimum"] == 1
    assert evidence["protocol_maximum"] == 1
    assert evidence["production_claim_sql_shape_verified"] is True
    assert evidence["variant_selection_verified"] is True
    assert len(evidence["paired_delta_samples_ms"]) == evidence["timed_pairs"]
    assert all(math.isfinite(value) for value in evidence["paired_delta_samples_ms"])
    assert math.isfinite(evidence["paired_delta_p50_ms"])
    assert math.isfinite(evidence["paired_delta_p95_ms"])
    assert [variant["name"] for variant in evidence["variants"]] == [
        "production_protocol_predicate",
        "control_without_protocol_predicate",
    ]
    for variant in evidence["variants"]:
        assert len(variant["duration_samples_ms"]) == evidence["timed_pairs"]
        assert all(math.isfinite(value) and value >= 0 for value in variant["duration_samples_ms"])
        assert math.isfinite(variant["duration_p50_ms"])
        assert math.isfinite(variant["duration_p95_ms"])
        assert "0:limit" in variant["plan"]["node_shape"]
        assert any(node.endswith(":lock_rows") for node in variant["plan"]["node_shape"])
        assert variant["plan"]["actual_rows"] == evidence["query_limit"]
        assert variant["plan"]["actual_loops"] == 1
        assert all(
            category in {"claimable", "protocol", "primary_key", "other"}
            for category in variant["plan"]["index_categories"]
        )

    serialized_evidence = json.dumps(evidence, sort_keys=True)
    assert "SELECT " not in serialized_evidence.upper()
    assert "POLL-PROTOCOL-" not in serialized_evidence.upper()

    assert not RayTaskExecution.objects.filter(
        Q(task_id__startswith="poll-latency-")
        | Q(task_id__startswith="poll-throughput-")
        | (Q(task_id__startswith="poll-protocol-") & ~Q(pk=foreign_protocol_row.pk))
    ).exists()
    assert RayTaskExecution.objects.filter(pk=foreign_protocol_row.pk).exists()
    assert not TaskWorkerLease.objects.filter(worker_id__startswith="benchmark-").exists()
