"""PostgreSQL query and snapshot evidence for bounded workflow-progress reads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.db import close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_ray.lifecycle import succeed_task
from django_ray.models import (
    TaskState,
    WorkflowProgressNodeDetail,
    WorkflowProgressRunStorage,
    WorkflowProgressTopologyCollection,
    WorkflowProgressTopologyManifestPage,
)
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.preparation import prepare_workflow_progress_topology
from django_ray.workflow.progress.reads import (
    get_workflow_node_detail,
    get_workflow_progress_summary,
    list_workflow_node_details,
    list_workflow_topology_edges,
    list_workflow_topology_nodes,
)
from django_ray.workflow.progress.storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_detail,
    prepare_workflow_progress_node_detail,
    stage_workflow_progress_topology,
)
from tests.workflow_progress_storage_helpers import (
    PublishedWorkflow,
    workflow_detail,
    workflow_node,
    workflow_node_id,
    workflow_summary,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]

_MAX_CURSOR_BYTES = 2 * 1024
_MAX_PAGE_ITEMS = 256
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_DECODED_RECORD_BYTES = 1024 * 1024


@pytest.fixture(autouse=True)
def _require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires tests.postgres_settings and a PostgreSQL test database")


def _allow(_execution: object) -> bool:
    return True


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _publish_workflow(
    node_count: int,
    *,
    edge_count: int = 0,
    case_id: int,
) -> PublishedWorkflow:
    run_value = node_count * 100_000 + edge_count + case_id
    run_id = f"00000000-0000-0000-0000-{run_value:012d}"
    from django_ray.models import RayTaskExecution, TaskState

    execution = RayTaskExecution.objects.create(
        task_id=f"workflow-read-{node_count}-{edge_count}-{case_id}",
        callable_path="tests.integration.sync_resource",
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
    topology = prepare_workflow_progress_topology(
        identity,
        1,
        (workflow_node(workflow_node_id(index)) for index in range(node_count)),
        (
            {
                "source": workflow_node_id(index % node_count),
                "target": workflow_node_id((index + 1) % node_count),
            }
            for index in range(edge_count)
        ),
    )
    prepared_detail = prepare_workflow_progress_detail(
        (workflow_detail(workflow_node_id(index)) for index in range(node_count)),
        topology=topology,
    )
    manifest_id = stage_workflow_progress_topology(topology)
    assert manifest_id is not None
    summary_payload = workflow_summary(
        identity,
        summary_revision=1,
        node_count=node_count,
        running_count=0,
    )
    edge_counts = summary_payload["edge_counts"]
    assert isinstance(edge_counts, dict)
    edge_counts.update(
        declared=edge_count,
        discovered=edge_count,
    )
    result = persist_workflow_progress_publication(
        identity,
        summary_payload,
        manifest_id=manifest_id,
        prepared_topology=topology,
        prepared_detail=prepared_detail,
    )
    assert result.accepted
    return PublishedWorkflow(execution, identity, topology, manifest_id)


def _captured(operation: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    with CaptureQueriesContext(connection) as queries:
        result = operation()
    return result, [str(query["sql"]) for query in queries.captured_queries]


def _selects(statements: list[str]) -> list[str]:
    return [value for value in statements if value.lstrip().upper().startswith("SELECT")]


def _assert_bounded_response(response: dict[str, Any]) -> None:
    encoded = _canonical_bytes(response)
    assert len(encoded) <= _MAX_RESPONSE_BYTES
    returned_count = response.get("returned_count", 0)
    assert type(returned_count) is int
    assert 0 <= returned_count <= _MAX_PAGE_ITEMS
    items = response.get("items", [])
    assert isinstance(items, list)
    assert returned_count == len(items)
    assert sum(len(_canonical_bytes(item)) for item in items) <= _MAX_DECODED_RECORD_BYTES
    cursor = response.get("next_cursor")
    assert cursor is None or len(cursor.encode("utf-8")) <= _MAX_CURSOR_BYTES


def _explain(sql: str, params: list[object]) -> dict[str, Any]:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE")
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params)
            value = cursor.fetchone()[0]
    assert isinstance(value, list) and len(value) == 1
    return value[0]


def _plan_indexes(value: object) -> set[str]:
    indexes: set[str] = set()
    if isinstance(value, dict):
        index_name = value.get("Index Name")
        if isinstance(index_name, str):
            indexes.add(index_name)
        for child in value.values():
            indexes.update(_plan_indexes(child))
    elif isinstance(value, list):
        for child in value:
            indexes.update(_plan_indexes(child))
    return indexes


@pytest.mark.parametrize("node_count", [1_000, 10_000, 25_000])
def test_read_query_shape_and_response_bounds_are_independent_of_retained_size(
    node_count: int,
    record_property: Callable[[str, object], None],
) -> None:
    workflow = _publish_workflow(node_count, case_id=127_000 + node_count)
    summary, summary_sql = _captured(
        lambda: get_workflow_progress_summary(
            workflow.execution,
            authorize=_allow,
        )
    )
    topology, topology_sql = _captured(
        lambda: list_workflow_topology_nodes(
            workflow.execution,
            authorize=_allow,
            limit=100,
        )
    )
    detail, detail_sql = _captured(
        lambda: list_workflow_node_details(
            workflow.execution,
            authorize=_allow,
            limit=100,
        )
    )
    single, single_sql = _captured(
        lambda: get_workflow_node_detail(
            workflow.execution,
            workflow_node_id(node_count // 2),
            authorize=_allow,
        )
    )

    assert summary["availability"] == "AVAILABLE"
    _assert_bounded_response(summary)
    _assert_bounded_response(topology)
    _assert_bounded_response(detail)
    assert single["found"] is True
    assert single["item"]["node_id"] == workflow_node_id(node_count // 2)
    _assert_bounded_response(single)
    select_counts = {
        "summary": len(_selects(summary_sql)),
        "topology": len(_selects(topology_sql)),
        "detail": len(_selects(detail_sql)),
        "single": len(_selects(single_sql)),
    }
    assert select_counts == {
        "summary": 1,
        "topology": 3,
        "detail": 3,
        "single": 2,
    }
    detail_selects = "\n".join(_selects(detail_sql)).upper()
    single_selects = "\n".join(_selects(single_sql)).upper()
    assert "COUNT(" not in detail_selects
    assert "SUM(" not in detail_selects
    assert "NODE_KEY" in detail_selects
    assert "NODE_KEY" in single_selects

    record_property(f"retained_{node_count}_select_counts", select_counts)
    record_property(f"retained_{node_count}_summary_bytes", len(_canonical_bytes(summary)))
    record_property(f"retained_{node_count}_topology_bytes", len(_canonical_bytes(topology)))
    record_property(f"retained_{node_count}_detail_bytes", len(_canonical_bytes(detail)))


def test_terminal_attempt_reads_use_the_archived_epoch_without_scanning_current_payloads() -> None:
    workflow = _publish_workflow(100, case_id=127_050)
    assert succeed_task(
        workflow.execution,
        result_data='{"ok":true}',
        result_reference=None,
    )
    workflow.execution.refresh_from_db()
    workflow.execution.attempt_number = 2
    workflow.execution.execution_generation = 2
    workflow.execution.workflow_run_id = "00000000-0000-0000-0000-000000127051"
    workflow.execution.workflow_progress_summary_json = None
    workflow.execution.state = TaskState.RUNNING
    workflow.execution.finished_at = None
    workflow.execution.result_data = None
    workflow.execution.result_reference = None
    workflow.execution.save(
        update_fields=[
            "attempt_number",
            "execution_generation",
            "workflow_run_id",
            "workflow_progress_summary_json",
            "state",
            "finished_at",
            "result_data",
            "result_reference",
        ]
    )

    summary, summary_sql = _captured(
        lambda: get_workflow_progress_summary(
            workflow.execution,
            authorize=_allow,
            attempt_number=1,
        )
    )
    detail, detail_sql = _captured(
        lambda: list_workflow_node_details(
            workflow.execution,
            authorize=_allow,
            attempt_number=1,
            limit=100,
        )
    )

    assert summary["availability"] == "TRUNCATED"
    assert summary["complete"] is False
    assert summary["summary"]["state"] == "SUCCEEDED"
    assert summary["summary"]["detail"] == {
        "availability": "TRUNCATED",
        "complete": False,
        "truncation_reasons": ["terminal_state_unreported"],
    }
    assert summary["run_identity"]["attempt_number"] == 1
    assert workflow.execution.attempt_number == 2
    assert summary["run_identity"]["run_id"] == workflow.identity.run_id
    _assert_bounded_response(summary)
    _assert_bounded_response(detail)
    assert detail["availability"] == "TRUNCATED"
    assert detail["complete"] is False
    assert detail["returned_count"] == 100
    assert {item["state"] for item in detail["items"]} == {"PENDING"}
    assert len(_selects(summary_sql)) <= 3
    assert len(_selects(detail_sql)) <= 4


def test_first_and_next_pages_use_the_same_bounded_keyset_shapes() -> None:
    workflow = _publish_workflow(1_000, edge_count=999, case_id=127_100)

    node_first, node_first_sql = _captured(
        lambda: list_workflow_topology_nodes(workflow.execution, authorize=_allow, limit=100)
    )
    assert node_first["next_cursor"] is not None
    node_next, node_next_sql = _captured(
        lambda: list_workflow_topology_nodes(
            workflow.execution,
            authorize=_allow,
            cursor=node_first["next_cursor"],
            limit=100,
        )
    )
    edge_first, edge_first_sql = _captured(
        lambda: list_workflow_topology_edges(workflow.execution, authorize=_allow, limit=100)
    )
    assert edge_first["next_cursor"] is not None
    edge_next, edge_next_sql = _captured(
        lambda: list_workflow_topology_edges(
            workflow.execution,
            authorize=_allow,
            cursor=edge_first["next_cursor"],
            limit=100,
        )
    )
    detail_first, detail_first_sql = _captured(
        lambda: list_workflow_node_details(workflow.execution, authorize=_allow, limit=100)
    )
    assert detail_first["next_cursor"] is not None
    detail_next, detail_next_sql = _captured(
        lambda: list_workflow_node_details(
            workflow.execution,
            authorize=_allow,
            cursor=detail_first["next_cursor"],
            limit=100,
        )
    )
    state_first, state_first_sql = _captured(
        lambda: list_workflow_node_details(
            workflow.execution,
            authorize=_allow,
            state="PENDING",
            limit=100,
        )
    )

    for page in (
        node_first,
        node_next,
        edge_first,
        edge_next,
        detail_first,
        detail_next,
        state_first,
    ):
        _assert_bounded_response(page)
        assert page["returned_count"] == 100
    assert len(_selects(node_first_sql)) == len(_selects(node_next_sql))
    assert len(_selects(edge_first_sql)) == len(_selects(edge_next_sql))
    assert len(_selects(detail_first_sql)) == len(_selects(detail_next_sql))
    state_selects = "\n".join(_selects(state_first_sql)).upper()
    assert "PENDING" in state_selects
    assert "NODE_KEY" in state_selects


def test_postgresql_plans_use_run_key_state_and_manifest_position_indexes() -> None:
    workflow = _publish_workflow(1_000, edge_count=999, case_id=127_200)
    run = WorkflowProgressRunStorage.objects.get(execution=workflow.execution)
    changed_records = tuple(
        prepare_workflow_progress_node_detail(
            workflow_detail(workflow_node_id(index), state="RUNNING"),
            identity=workflow.identity,
        )
        for index in range(10)
    )
    changed_summary = workflow_summary(
        workflow.identity,
        summary_revision=2,
        node_count=1_000,
        running_count=10,
    )
    changed_edge_counts = changed_summary["edge_counts"]
    assert isinstance(changed_edge_counts, dict)
    changed_edge_counts.update(declared=999, discovered=999)
    changed_result = persist_workflow_progress_publication(
        workflow.identity,
        changed_summary,
        manifest_id=workflow.manifest_id,
        prepared_topology=workflow.topology,
        detail_records=changed_records,
    )
    assert changed_result.accepted
    node_table = connection.ops.quote_name(WorkflowProgressNodeDetail._meta.db_table)
    link_table = connection.ops.quote_name(WorkflowProgressTopologyManifestPage._meta.db_table)
    middle_key = hashlib.sha256(workflow_node_id(500).encode()).hexdigest()
    node_collection = WorkflowProgressTopologyCollection.NODE.value
    assert WorkflowProgressTopologyManifestPage.objects.filter(
        manifest_id=workflow.manifest_id,
        collection=node_collection,
        page_index=1,
    ).exists()

    detail_plan = _explain(
        f"SELECT id FROM {node_table} "
        "WHERE run_storage_id = %s AND node_key > %s "
        "ORDER BY node_key LIMIT 100",
        [run.pk, middle_key],
    )
    state_plan = _explain(
        f"SELECT id FROM {node_table} "
        "WHERE run_storage_id = %s AND state = %s "
        "ORDER BY node_key LIMIT 100",
        [run.pk, "RUNNING"],
    )
    single_plan = _explain(
        f"SELECT id FROM {node_table} WHERE run_storage_id = %s AND node_key = %s",
        [run.pk, middle_key],
    )
    link_plan = _explain(
        f"SELECT page_id FROM {link_table} "
        "WHERE manifest_id = %s AND collection = %s AND page_index = %s",
        [workflow.manifest_id, node_collection, 1],
    )

    assert "ray_wf_node_key_uniq" in _plan_indexes(detail_plan)
    assert "ray_wf_node_state_idx" in _plan_indexes(state_plan)
    assert "ray_wf_node_key_uniq" in _plan_indexes(single_plan)
    assert "ray_wf_link_position_uniq" in _plan_indexes(link_plan)
    assert link_plan["Plan"]["Actual Rows"] >= 1


def test_cursor_epoch_does_not_advance_into_a_new_detail_publication() -> None:
    workflow = _publish_workflow(1_000, case_id=127_300)
    first = list_workflow_node_details(workflow.execution, authorize=_allow, limit=100)
    cursor = first["next_cursor"]
    assert cursor is not None
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(500), state="RUNNING"),
        identity=workflow.identity,
    )
    result = persist_workflow_progress_publication(
        workflow.identity,
        workflow_summary(
            workflow.identity,
            summary_revision=2,
            node_count=1_000,
            running_count=1,
        ),
        manifest_id=workflow.manifest_id,
        prepared_topology=workflow.topology,
        detail_records=(changed,),
    )
    assert result.accepted

    retired = list_workflow_node_details(
        workflow.execution,
        authorize=_allow,
        cursor=cursor,
        limit=100,
    )

    assert retired == {
        "schema": "django-ray.workflow-progress-page",
        "schema_version": 1,
        "generated_at": retired["generated_at"],
        "task_id": first["task_id"],
        "run_identity": first["run_identity"],
        "publication": first["publication"],
        "availability": "EXPIRED",
        "complete": False,
        "collection": "node_details",
        "returned_count": 0,
        "items": [],
        "next_cursor": None,
    }


def test_reader_and_writer_epoch_race_never_returns_a_mixed_page() -> None:
    workflow = _publish_workflow(1_000, case_id=127_400)
    first_item = list_workflow_node_details(
        workflow.execution,
        authorize=_allow,
        limit=1,
    )["items"][0]
    changed_node_id = first_item["node_id"]
    barrier = Barrier(2)

    def reader() -> dict[str, Any]:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return list_workflow_node_details(
                workflow.execution,
                authorize=_allow,
                limit=256,
            )
        finally:
            close_old_connections()

    def writer() -> bool:
        close_old_connections()
        try:
            changed = prepare_workflow_progress_node_detail(
                workflow_detail(changed_node_id, state="RUNNING"),
                identity=workflow.identity,
            )
            barrier.wait(timeout=10)
            result = persist_workflow_progress_publication(
                workflow.identity,
                workflow_summary(
                    workflow.identity,
                    summary_revision=2,
                    node_count=1_000,
                    running_count=1,
                ),
                manifest_id=workflow.manifest_id,
                prepared_topology=workflow.topology,
                detail_records=(changed,),
            )
            return result.accepted
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader_future = executor.submit(reader)
        writer_future = executor.submit(writer)
        page = reader_future.result(timeout=30)
        assert writer_future.result(timeout=30)

    assert page["publication"]["detail_revision"] in {1, 2}
    changed_item = next(item for item in page["items"] if item["node_id"] == changed_node_id)
    if page["publication"]["detail_revision"] == 1:
        assert changed_item["state"] == "PENDING"
    else:
        assert changed_item["state"] == "RUNNING"
