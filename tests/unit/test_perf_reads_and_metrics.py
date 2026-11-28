"""Performance and scalability regression tests for reads, metrics, status, cleanup, and storage."""

from __future__ import annotations

import gc
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from weakref import WeakKeyDictionary, WeakValueDictionary

import pytest
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from prometheus_client.parser import text_string_to_metric_families

from django_ray import redaction_patterns
from django_ray.management.commands.django_ray_benchmark_polling import (
    Command as PollingBenchCommand,
)
from django_ray.metrics import clear_metrics_cache, render_prometheus_metrics
from django_ray.models import (
    LegacyWorkerAdmissionToken,
    RayTaskExecution,
    TaskAttempt,
    TaskExecutionProtocolPolicy,
    TaskState,
    TaskWorkerLease,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyPage,
)
from django_ray.protocol_status import (
    annotate_execution_protocol_availability,
    build_protocol_status,
    render_protocol_status_text,
)
from django_ray.redaction import DEFAULT_REDACT_PATTERNS, redact_text
from django_ray.runtime.context import DurableTaskContext, WorkflowRunIdentity
from django_ray.workflow.progress import cleanup as progress_cleanup
from django_ray.workflow.progress import preparation as progress_preparation
from django_ray.workflow.progress import reads as progress_reads
from django_ray.workflow.progress import runs as progress_runs
from django_ray.workflow.progress import storage as progress_storage
from tests.workflow_progress_storage_helpers import publish_initial_workflow, workflow_node_id


def _samples(metrics: str) -> dict[str, float]:
    return {
        sample.name + str(dict(sample.labels)): sample.value
        for family in text_string_to_metric_families(metrics)
        for sample in family.samples
    }


def _allow_all(_execution: RayTaskExecution) -> bool:
    return True


def _topology_node(node_id: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "kind": "task",
        "label": f"Node {node_id}",
        "callable_path": "tests.tasks.step",
        "runtime_env": {},
        "ray_options": {},
    }


@pytest.mark.django_db
def test_metrics_consolidated_queries_avoid_not_exists_and_like(settings) -> None:
    clear_metrics_cache()
    settings.DJANGO_RAY = {"WORKER_LEASE_SECONDS": 60}
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)
    RayTaskExecution.objects.create(
        task_id="perf-metrics-timeout",
        callable_path="tasks.slow",
        queue_name="default",
        state=TaskState.FAILED,
        error_message="Task timed out after 15 seconds",
        created_at=now - timedelta(seconds=20),
        started_at=now - timedelta(seconds=15),
        finished_at=now,
    )

    with CaptureQueriesContext(connection) as with_queues:
        rendered = render_prometheus_metrics(queue_names=["default"], observed_at=now)
    with CaptureQueriesContext(connection) as without_queues:
        render_prometheus_metrics(queue_names=(), observed_at=now)

    assert len(with_queues.captured_queries) == 4
    assert len(without_queues.captured_queries) == 3
    all_sql = "\n".join(query["sql"].upper() for query in with_queues.captured_queries)
    assert "NOT EXISTS" not in all_sql
    assert " LIKE " not in all_sql

    samples = _samples(rendered)
    assert samples["django_ray_tasks{'state': 'FAILED'}"] == 1
    assert samples["django_ray_tasks_total{'state': 'FAILED'}"] == 1
    assert (
        samples["django_ray_tasks_by_execution_protocol{'protocol': '1', 'state': 'FAILED'}"] == 1
    )
    assert (
        samples["django_ray_tasks_by_execution_protocol_total{'protocol': '1', 'state': 'FAILED'}"]
        == 1
    )
    assert samples["django_ray_timeouts_recorded{}"] == 1
    assert "\n\n# HELP django_ray_worker_lease_policy_available" in rendered


@pytest.mark.django_db
def test_metrics_queue_wait_uses_first_attempt_started_at_on_retries(settings) -> None:
    clear_metrics_cache()
    settings.DJANGO_RAY = {"WORKER_LEASE_SECONDS": 60}
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)

    retried = RayTaskExecution.objects.create(
        task_id="perf-retried-task",
        callable_path="tasks.retryable",
        state=TaskState.SUCCEEDED,
        attempt_number=2,
        created_at=now - timedelta(seconds=30),
        run_after=now - timedelta(seconds=10),
        started_at=now - timedelta(seconds=5),
        finished_at=now,
    )
    TaskAttempt.objects.create(
        execution=retried,
        attempt_number=1,
        state=TaskState.FAILED,
        started_at=now - timedelta(seconds=27),
        finished_at=now - timedelta(seconds=25),
    )

    queued_for_retry = RayTaskExecution.objects.create(
        task_id="perf-queued-retry-task",
        callable_path="tasks.retryable",
        state=TaskState.QUEUED,
        attempt_number=2,
        created_at=now - timedelta(seconds=20),
        started_at=None,
    )
    TaskAttempt.objects.create(
        execution=queued_for_retry,
        attempt_number=1,
        state=TaskState.FAILED,
        started_at=now - timedelta(seconds=18),
        finished_at=now - timedelta(seconds=17),
    )

    samples = _samples(render_prometheus_metrics(observed_at=now))
    # retried initial queue wait = 3s (not 25s); queued_for_retry initial queue wait = 2s.
    assert samples["django_ray_queue_wait_seconds_count{}"] == 2
    assert samples["django_ray_queue_wait_seconds_sum{}"] == 5
    assert samples["django_ray_queue_wait_seconds_max{}"] == 3
    # Claim latency uses current attempt started_at - max(created_at, run_after) = 5s.
    assert samples["django_ray_claim_latency_seconds_count{}"] == 1
    assert samples["django_ray_claim_latency_seconds_sum{}"] == 5


@pytest.mark.django_db
def test_metrics_ttl_cache_hits_clears_and_bypasses(settings) -> None:
    clear_metrics_cache()
    settings.DJANGO_RAY = {"WORKER_LEASE_SECONDS": 60, "METRICS_CACHE_TTL_SECONDS": 60.0}

    with CaptureQueriesContext(connection) as first_scrape:
        first = render_prometheus_metrics(queue_names=["default"])
    with CaptureQueriesContext(connection) as cached_scrape:
        second = render_prometheus_metrics(queue_names=["default"])

    assert first == second
    assert len(first_scrape.captured_queries) == 4
    assert len(cached_scrape.captured_queries) == 0

    # Explicit observed_at or cache_ttl_seconds=0 bypasses the cache.
    with CaptureQueriesContext(connection) as bypassed_scrape:
        render_prometheus_metrics(queue_names=["default"], cache_ttl_seconds=0.0)
    assert len(bypassed_scrape.captured_queries) == 4

    with CaptureQueriesContext(connection) as observed_scrape:
        render_prometheus_metrics(
            queue_names=["default"],
            observed_at=datetime(2026, 7, 19, 12, tzinfo=UTC),
        )
    assert len(observed_scrape.captured_queries) == 4

    clear_metrics_cache()
    with CaptureQueriesContext(connection) as after_clear:
        render_prometheus_metrics(queue_names=["default"])
    assert len(after_clear.captured_queries) == 4

    with pytest.raises(ValueError, match="cache_ttl_seconds"):
        render_prometheus_metrics(cache_ttl_seconds=-1.0)
    with pytest.raises(TypeError, match="cache_ttl_seconds"):
        render_prometheus_metrics(cache_ttl_seconds=True)  # type: ignore[arg-type]
    clear_metrics_cache()


@pytest.mark.django_db(transaction=True)
def test_protocol_status_uncorrelated_guards_and_readable_text() -> None:
    observed_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    LegacyWorkerAdmissionToken.objects.all().delete()
    TaskExecutionProtocolPolicy.objects.filter(singleton_key=1).update(
        legacy_worker_admission_enabled=False,
        revision=2,
    )
    TaskWorkerLease.objects.create(
        worker_id="perf-explicit-worker",
        hostname="host",
        pid=101,
        capability_schema_version=1,
        legacy_admission_token=None,
        min_supported_execution_protocol_version=1,
        max_supported_execution_protocol_version=2,
        last_heartbeat_at=observed_at,
    )
    RayTaskExecution.objects.create(
        task_id="perf-proto-supported",
        callable_path="tasks.echo",
        execution_protocol_version=2,
        state=TaskState.QUEUED,
    )
    RayTaskExecution.objects.create(
        task_id="perf-proto-unsupported",
        callable_path="tasks.echo",
        execution_protocol_version=9,
        state=TaskState.QUEUED,
    )

    with CaptureQueriesContext(connection) as captured:
        rows = list(
            annotate_execution_protocol_availability(
                RayTaskExecution.objects.order_by("execution_protocol_version"),
                observed_at=observed_at,
            ).values_list("task_id", "protocol_compatible_worker_available")
        )

    assert len(captured.captured_queries) == 1
    sql = captured.captured_queries[0]["sql"].upper()
    assert "CASE WHEN NOT EXISTS" in sql
    assert rows == [
        ("perf-proto-supported", True),
        ("perf-proto-unsupported", False),
    ]

    report = build_protocol_status(using="default", observed_at=observed_at)
    text = render_protocol_status_text(report)
    for section in (
        "[policy]",
        "[leases]",
        "[capabilities]",
        "[nonterminal_work]",
        "[unsupported_work]",
        "[counters]",
        "[blockers]",
    ):
        assert section in text
    assert max(len(line) for line in text.splitlines()) <= 120


@pytest.mark.django_db
def test_workflow_progress_reads_are_lock_free_and_coalesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publish_initial_workflow(10, case_id=817_001)
    execution = published.execution

    with CaptureQueriesContext(connection) as separate:
        summary = progress_reads.get_workflow_progress_summary(execution, authorize=_allow_all)
        nodes = progress_reads.list_workflow_topology_nodes(execution, authorize=_allow_all)
        edges = progress_reads.list_workflow_topology_edges(execution, authorize=_allow_all)
        details = progress_reads.list_workflow_node_details(execution, authorize=_allow_all)
        single = progress_reads.get_workflow_node_detail(
            execution,
            workflow_node_id(3),
            authorize=_allow_all,
        )

    separate_sql = "\n".join(query["sql"].upper() for query in separate.captured_queries)
    assert "FOR UPDATE" not in separate_sql
    assert single["found"] is True

    with CaptureQueriesContext(connection) as combined:
        bundle = progress_reads.read_workflow_progress_collections(
            execution,
            authorize=_allow_all,
        )

    combined_sql = "\n".join(query["sql"].upper() for query in combined.captured_queries)
    assert "FOR UPDATE" not in combined_sql
    assert bundle["summary"]["summary"] == summary["summary"]
    assert bundle["topology_nodes"]["items"] == nodes["items"]
    assert bundle["topology_edges"]["items"] == edges["items"]
    assert bundle["node_details"]["items"] == details["items"]
    # Coalesced snapshot eliminates redundant execution + manifest SELECT queries.
    assert len(combined.captured_queries) < len(separate.captured_queries) - 2

    # Verify PostgreSQL outermost transactions emit REPEATABLE READ, READ ONLY.
    executed_sql: list[str] = []

    class _FakeCursor:
        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            executed_sql.append(sql)

    class _FakePgConnection:
        vendor = "postgresql"
        in_atomic_block = False

        def get_autocommit(self) -> bool:
            return True

        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    @contextmanager
    def _noop_atomic(*_args: object, **_kwargs: object):
        yield

    monkeypatch.setattr(progress_reads, "connections", {"fake_pg": _FakePgConnection()})
    monkeypatch.setattr(progress_reads.transaction, "atomic", _noop_atomic)
    with progress_reads._read_snapshot(using="fake_pg"):
        pass
    assert executed_sql == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"]


def test_bounded_response_uses_logarithmic_binary_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [{"id": i, "payload": "x" * 100} for i in range(256)]
    base = {"schema": "django-ray.workflow-progress-page"}
    calls = 0

    def cursor_for_count(count: int) -> str | None:
        nonlocal calls
        calls += 1
        return None if count == len(items) else f"cursor-{count:04d}"

    # Pick a response byte limit where exactly 42 items fit.
    target_response = {
        **base,
        "returned_count": 42,
        "items": items[:42],
        "next_cursor": "cursor-0042",
    }
    budget = len(progress_reads._wire_bytes(target_response))
    monkeypatch.setattr(progress_reads, "WORKFLOW_PROGRESS_READ_MAX_RESPONSE_BYTES", budget)

    result = progress_reads._bounded_response(
        base,
        items=items,
        cursor_for_count=cursor_for_count,
    )
    assert result["returned_count"] == 42
    assert len(result["items"]) == 42
    # Binary search over 1..255 takes ~9 probes instead of 215 linear pops.
    assert calls <= 12


@pytest.mark.django_db
def test_weak_capability_registry_uses_weak_maps_and_evicts_on_gc() -> None:
    assert isinstance(
        progress_storage._PREPARED_TOPOLOGY_CAPABILITIES._by_handle,
        WeakKeyDictionary,
    )
    assert isinstance(
        progress_storage._PREPARED_TOPOLOGY_CAPABILITIES._handles_by_token,
        WeakValueDictionary,
    )

    identity = WorkflowRunIdentity(
        task_execution_pk=1,
        attempt_number=1,
        execution_generation=1,
        run_id=str(uuid.uuid4()),
    )
    topology = progress_preparation.prepare_workflow_progress_topology(
        identity,
        1,
        [_topology_node("a")],
        [],
    )
    token = topology._capability_token
    assert token is not None
    assert token in progress_storage._PREPARED_TOPOLOGY_CAPABILITIES
    assert progress_storage._prepared_topology_capability_matches(topology) is True

    # Copying only the token string without the handle does not grant authority.
    forged = progress_storage.PreparedWorkflowProgressTopology(
        identity=topology.identity,
        topology_version=topology.topology_version,
        manifest_payload=topology.manifest_payload,
        manifest_digest=topology.manifest_digest,
        pages=topology.pages,
        node_ids=topology.node_ids,
        observed_node_ids=topology.observed_node_ids,
        node_kinds=topology.node_kinds,
        edges=topology.edges,
        observed_node_count=topology.observed_node_count,
        observed_edge_count=topology.observed_edge_count,
        retained_node_count=topology.retained_node_count,
        retained_edge_count=topology.retained_edge_count,
        encoded_bytes=topology.encoded_bytes,
        decoded_bytes=topology.decoded_bytes,
        truncation_reasons=topology.truncation_reasons,
        map_node_ids=topology.map_node_ids,
        _capability_token=token,
    )
    assert progress_storage._prepared_topology_capability_matches(forged) is False

    del topology
    gc.collect()
    assert token not in progress_storage._PREPARED_TOPOLOGY_CAPABILITIES


@pytest.mark.django_db
def test_stage_topology_uses_batched_page_upsert_without_per_page_savepoints() -> None:
    run_id = str(uuid.uuid4())
    execution = RayTaskExecution.objects.create(
        task_id=f"perf-stage-{run_id}",
        callable_path="tests.workflow",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
        workflow_run_id=run_id,
    )
    identity = WorkflowRunIdentity(
        task_execution_pk=execution.pk,
        attempt_number=1,
        execution_generation=1,
        run_id=run_id,
    )
    # 600 nodes + 599 edges spans multiple 256-item topology pages (>= 6 pages).
    nodes = [_topology_node(f"n-{i:04d}") for i in range(600)]
    edges = [{"source": f"n-{i:04d}", "target": f"n-{i + 1:04d}"} for i in range(599)]
    topology = progress_preparation.prepare_workflow_progress_topology(
        identity,
        1,
        nodes,
        edges,
    )
    assert len(topology.pages) >= 6

    with CaptureQueriesContext(connection) as captured:
        manifest_id = progress_storage.stage_workflow_progress_topology(topology)

    assert manifest_id is not None
    assert WorkflowProgressTopologyPage.objects.count() == len(topology.pages)
    savepoints = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("SAVEPOINT")
    ]
    # Constant O(1) savepoints (outer + get_or_create + manifest), never 1 per page.
    assert len(savepoints) <= 3
    assert len(savepoints) < len(topology.pages)


@pytest.mark.django_db
def test_cleanup_uses_clean_first_index_order_and_chunked_child_deletes() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    exec_errored = publish_initial_workflow(6, case_id=817_002).execution
    exec_clean = publish_initial_workflow(6, case_id=817_003).execution

    RayTaskExecution.objects.filter(pk__in=[exec_errored.pk, exec_clean.pk]).update(
        state=TaskState.SUCCEEDED
    )
    WorkflowProgressRunStorage.objects.filter(execution=exec_errored).update(
        detail_expires_at=now - timedelta(days=2),
        cleanup_error="EXPIRED_RUN_DELETE_FAILED: prior",
    )
    WorkflowProgressRunStorage.objects.filter(execution=exec_clean).update(
        detail_expires_at=now - timedelta(days=1),
        cleanup_error=None,
    )

    with CaptureQueriesContext(connection) as captured:
        report = progress_cleanup.cleanup_workflow_progress_storage(
            batch_size=1,
            delete=True,
            now=now,
        )

    # Clean candidate is chosen first even though errored run has an older expiry.
    assert report.deleted_count == 1
    assert not WorkflowProgressRunStorage.objects.filter(execution=exec_clean).exists()
    assert not WorkflowProgressNodeDetail.objects.filter(run_storage__execution=exec_clean).exists()
    assert not WorkflowProgressTopologyManifest.objects.filter(
        run_storage__execution=exec_clean
    ).exists()
    assert not WorkflowProgressTopologyPage.objects.filter(
        run_storage__execution=exec_clean
    ).exists()
    assert WorkflowProgressRunStorage.objects.filter(execution=exec_errored).exists()

    # Pass-1 candidate query does not lead ORDER BY with cleanup_error.
    select_run_sql = [
        query["sql"].upper()
        for query in captured.captured_queries
        if "DJANGO_RAY_WORKFLOWPROGRESSRUNSTORAGE" in query["sql"].upper()
        and "DETAIL_EXPIRES_AT" in query["sql"].upper()
        and "ORDER BY" in query["sql"].upper()
    ]
    assert select_run_sql
    order_clause = select_run_sql[0].split("ORDER BY", 1)[1]
    assert "CLEANUP_ERROR" not in order_clause


@pytest.mark.django_db
def test_allocate_workflow_run_collapses_writes_to_single_update() -> None:
    execution = RayTaskExecution.objects.create(
        task_id="perf-alloc-run",
        callable_path="tests.workflow",
        state=TaskState.RUNNING,
        attempt_number=1,
        execution_generation=1,
    )
    ctx = DurableTaskContext(
        task_pk=execution.pk,
        task_id=execution.task_id,
        attempt_number=1,
        execution_generation=1,
    )

    with CaptureQueriesContext(connection) as first_alloc:
        first_identity = progress_runs.allocate_workflow_run(ctx)
    assert first_identity is not None
    first_updates = [
        query["sql"]
        for query in first_alloc.captured_queries
        if query["sql"].lstrip().upper().startswith("UPDATE")
    ]
    assert len(first_updates) == 1

    with CaptureQueriesContext(connection) as second_alloc:
        second_identity = progress_runs.allocate_workflow_run(ctx)
    assert second_identity is not None
    assert second_identity.run_id != first_identity.run_id
    second_updates = [
        query["sql"]
        for query in second_alloc.captured_queries
        if query["sql"].lstrip().upper().startswith("UPDATE")
    ]
    assert len(second_updates) == 1


def test_redaction_pattern_validation_is_cached_for_repeated_custom_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redaction_patterns._compile_validated_cached.cache_clear()
    real_validate = redaction_patterns._validate_sources
    validate_calls = 0

    def counting_validate(*args: Any, **kwargs: Any):
        nonlocal validate_calls
        validate_calls += 1
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(redaction_patterns, "_validate_sources", counting_validate)

    custom = ["customer_secret_key", "tenant_api_token"]
    for _ in range(10):
        program = redaction_patterns.compile_redaction_patterns(
            custom,
            builtin_patterns=DEFAULT_REDACT_PATTERNS,
        )
        assert program is not None
        assert redact_text("customer_secret_key=abc123", patterns=custom) == "[REDACTED]"

    assert validate_calls == 1


@pytest.mark.django_db
def test_polling_benchmark_high_cardinality_options_and_bulk_cleanup() -> None:
    command = PollingBenchCommand()
    parser = command.create_parser("manage.py", "django_ray_benchmark_polling")
    parsed = parser.parse_args(
        [
            "--protocol-predicate-max-rows=2000",
            "--background-terminal-rows=500",
        ]
    )
    assert parsed.protocol_predicate_max_rows == 2000
    assert parsed.background_terminal_rows == 500

    with pytest.raises(CommandError, match="--protocol-predicate-max-rows"):
        command._bounded_positive_int(0, "--protocol-predicate-max-rows", maximum=100_000)
    with pytest.raises(CommandError, match="--background-terminal-rows"):
        command._bounded_non_negative_int(-1, "--background-terminal-rows", maximum=100_000)

    pks = command._bulk_seed_executions(
        task_prefix="perf-bench-seed-",
        queue_name="perf-bench-queue",
        count=25,
        state=TaskState.SUCCEEDED,
    )
    assert len(pks) == 25
    assert RayTaskExecution.objects.filter(pk__in=pks, state=TaskState.SUCCEEDED).count() == 25
    command._delete_owned_pks(pks)
    assert not RayTaskExecution.objects.filter(pk__in=pks).exists()
