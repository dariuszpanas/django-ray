"""PostgreSQL locking and sparse-write evidence for workflow progress storage."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import perf_counter

import pytest
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_ray.models import (
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyManifest,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
)
from django_ray.workflow.progress.storage import (
    WorkflowProgressStorageConflictError,
    persist_workflow_progress_publication,
    prepare_workflow_progress_node_detail,
)
from tests.workflow_progress_storage_helpers import (
    PublishedWorkflow,
    publish_initial_workflow,
    workflow_detail,
    workflow_node_id,
    workflow_summary,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def _run_concurrently(*operations: Callable[[], object]) -> list[object]:
    barrier = Barrier(len(operations))

    def invoke(operation: Callable[[], object]) -> object:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return operation()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=30) for future in futures]


def _publish_running_node(
    workflow: PublishedWorkflow,
    *,
    node_index: int,
    summary_revision: int = 2,
) -> str:
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(node_index), state="RUNNING"),
        identity=workflow.identity,
    )
    try:
        result = persist_workflow_progress_publication(
            workflow.identity,
            workflow_summary(
                workflow.identity,
                summary_revision=summary_revision,
                node_count=2,
                running_count=1,
            ),
            manifest_id=workflow.manifest_id,
            prepared_topology=workflow.topology,
            detail_records=(changed,),
        )
    except WorkflowProgressStorageConflictError:
        return "conflict"
    return "accepted" if result.accepted else "rejected"


def _wal_lsn() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_current_wal_insert_lsn()")
        return str(cursor.fetchone()[0])


def _relation_size_bytes() -> int:
    tables = (
        WorkflowProgressRunStorage._meta.db_table,
        WorkflowProgressTopologyManifest._meta.db_table,
        WorkflowProgressTopologyPage._meta.db_table,
        WorkflowProgressNodeDetail._meta.db_table,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT SUM(pg_total_relation_size(table_name::regclass)) "
            "FROM unnest(%s::text[]) AS table_name",
            [list(tables)],
        )
        return int(cursor.fetchone()[0])


def _wal_bytes(start_lsn: str, end_lsn: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_wal_lsn_diff(%s, %s)", [end_lsn, start_lsn])
        return int(cursor.fetchone()[0])


def test_concurrent_sparse_writers_serialize_and_loser_rolls_back() -> None:
    workflow = publish_initial_workflow(2)

    results = _run_concurrently(
        lambda: _publish_running_node(workflow, node_index=0),
        lambda: _publish_running_node(workflow, node_index=1),
    )

    assert sorted(results) == ["accepted", "conflict"]
    run = WorkflowProgressRunStorage.objects.get(execution=workflow.execution)
    assert run.detail_revision == 2
    assert run.detail_node_count == 2
    assert run.detail_pending_count == 1
    assert run.detail_running_count == 1
    assert run.detail_succeeded_count == 0
    assert run.detail_failed_count == 0
    rows = list(
        WorkflowProgressNodeDetail.objects.filter(run_storage=run)
        .order_by("node_id")
        .values_list("state", "last_detail_revision")
    )
    assert sorted(rows) == [("PENDING", 1), ("RUNNING", 2)]
    workflow.execution.refresh_from_db(fields=["workflow_progress_summary_json"])
    summary = json.loads(workflow.execution.workflow_progress_summary_json or "{}")
    assert summary["summary_revision"] == 2
    assert summary["detail_revision"] == 2
    assert summary["node_counts"]["pending"] == 1
    assert summary["node_counts"]["running"] == 1


def test_outer_transaction_rollback_restores_detail_aggregates_rows_and_summary() -> None:
    workflow = publish_initial_workflow(2)
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(0), state="RUNNING"),
        identity=workflow.identity,
    )

    with pytest.raises(RuntimeError, match="rollback sparse publication"):
        with transaction.atomic():
            result = persist_workflow_progress_publication(
                workflow.identity,
                workflow_summary(
                    workflow.identity,
                    summary_revision=2,
                    node_count=2,
                    running_count=1,
                ),
                manifest_id=workflow.manifest_id,
                prepared_topology=workflow.topology,
                detail_records=(changed,),
            )
            assert result.accepted
            raise RuntimeError("rollback sparse publication")

    run = WorkflowProgressRunStorage.objects.get(execution=workflow.execution)
    assert run.detail_revision == 1
    assert run.detail_pending_count == 2
    assert run.detail_running_count == 0
    assert set(
        WorkflowProgressNodeDetail.objects.filter(run_storage=run).values_list("state", flat=True)
    ) == {"PENDING"}
    workflow.execution.refresh_from_db(fields=["workflow_progress_summary_json"])
    summary = json.loads(workflow.execution.workflow_progress_summary_json or "{}")
    assert summary["summary_revision"] == 1
    assert summary["detail_revision"] == 1


def test_representative_sparse_write_records_postgresql_round_trip_row_and_wal_evidence(
    record_property: Callable[[str, object], None],
) -> None:
    node_count = 1_000
    workflow = publish_initial_workflow(node_count)
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(node_count // 2), state="RUNNING"),
        identity=workflow.identity,
    )
    relation_before = _relation_size_bytes()
    wal_before = _wal_lsn()
    started = perf_counter()
    with CaptureQueriesContext(connection) as queries:
        result = persist_workflow_progress_publication(
            workflow.identity,
            workflow_summary(
                workflow.identity,
                summary_revision=2,
                node_count=node_count,
                running_count=1,
            ),
            manifest_id=workflow.manifest_id,
            prepared_topology=workflow.topology,
            detail_records=(changed,),
        )
    elapsed_ms = (perf_counter() - started) * 1_000
    wal_after = _wal_lsn()
    relation_after = _relation_size_bytes()
    wal_bytes = _wal_bytes(wal_before, wal_after)

    assert result.accepted
    assert result.changed_node_count == 1
    assert 0 < len(queries) <= 20
    assert wal_bytes > 0
    assert relation_after >= relation_before
    assert (
        WorkflowProgressNodeDetail.objects.filter(run_storage__execution=workflow.execution).count()
        == node_count
    )
    sql = "\n".join(query["sql"].upper() for query in queries)
    assert "COUNT(" not in sql
    assert "SUM(" not in sql
    selects = "\n".join(
        statement for statement in sql.splitlines() if statement.lstrip().startswith("SELECT")
    )
    topology_page_table = WorkflowProgressTopologyPage._meta.db_table.upper()
    topology_link_table = WorkflowProgressTopologyManifestPage._meta.db_table.upper()
    topology_page_selects = [
        statement
        for statement in selects.splitlines()
        if f'FROM "{topology_page_table}"' in statement
    ]
    assert len(topology_page_selects) == 1
    assert f'FROM "{topology_link_table}"' not in selects
    assert "IS NULL" in topology_page_selects[0]
    assert '"PAYLOAD"' not in topology_page_selects[0]
    assert f'INSERT INTO "{topology_page_table}"' not in sql
    assert f'UPDATE "{topology_page_table}"' not in sql
    assert f'INSERT INTO "{topology_link_table}"' not in sql

    run = WorkflowProgressRunStorage.objects.get(execution=workflow.execution)
    node_key = WorkflowProgressNodeDetail.objects.get(
        run_storage=run,
        node_id=workflow_node_id(node_count // 2),
    ).node_key
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(
                "EXPLAIN (FORMAT JSON) "
                f"SELECT id FROM {connection.ops.quote_name(WorkflowProgressNodeDetail._meta.db_table)} "
                "WHERE run_storage_id = %s AND node_key = %s",
                [run.pk, node_key],
            )
            plan = json.dumps(cursor.fetchone()[0])
    assert "ray_wf_node_key_uniq" in plan

    record_property("retained_nodes", node_count)
    record_property("sparse_round_trips", len(queries))
    record_property("sparse_elapsed_ms", round(elapsed_ms, 3))
    record_property("sparse_wal_bytes", wal_bytes)
    record_property("relation_bytes_before", relation_before)
    record_property("relation_bytes_after", relation_after)
