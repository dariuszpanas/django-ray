"""Scale-sensitive database-shape tests for sparse workflow progress writes."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_ray.models import (
    WorkflowProgressNodeDetail,
    WorkflowProgressTopologyManifestPage,
    WorkflowProgressTopologyPage,
)
from django_ray.workflow_progress_limits import WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS
from django_ray.workflow_progress_storage import (
    persist_workflow_progress_publication,
    prepare_workflow_progress_node_detail,
)
from tests.workflow_progress_storage_helpers import (
    publish_initial_workflow,
    workflow_detail,
    workflow_node_id,
    workflow_summary,
)


def _measure_one_node_update(node_count: int) -> tuple[int, float, str, int]:
    workflow = publish_initial_workflow(node_count)
    topology_page_count = len(workflow.topology.pages)
    changed = prepare_workflow_progress_node_detail(
        workflow_detail(workflow_node_id(node_count // 2), state="RUNNING"),
        identity=workflow.identity,
    )
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
    assert result.accepted
    assert result.changed_node_count == 1
    assert result.removed_node_count == 0
    return (
        len(queries),
        elapsed_ms,
        "\n".join(query["sql"].upper() for query in queries),
        topology_page_count,
    )


@pytest.mark.django_db
def test_sparse_publication_round_trips_are_independent_of_retained_workflow_size(
    record_property: Callable[[str, object], None],
) -> None:
    small_retained_nodes = 32
    large_retained_nodes = WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS + 1
    small_queries, small_ms, small_sql, small_topology_pages = _measure_one_node_update(
        small_retained_nodes
    )
    large_queries, large_ms, large_sql, large_topology_pages = _measure_one_node_update(
        large_retained_nodes
    )

    assert large_queries == small_queries
    assert (small_topology_pages, large_topology_pages) == (1, 2)
    assert "COUNT(" not in large_sql
    assert "SUM(" not in large_sql
    large_selects = "\n".join(
        statement for statement in large_sql.splitlines() if statement.lstrip().startswith("SELECT")
    )
    topology_page_table = WorkflowProgressTopologyPage._meta.db_table.upper()
    topology_link_table = WorkflowProgressTopologyManifestPage._meta.db_table.upper()
    topology_page_selects = [
        statement
        for statement in large_selects.splitlines()
        if f'FROM "{topology_page_table}"' in statement
    ]
    assert len(topology_page_selects) == 1
    assert f'FROM "{topology_link_table}"' not in large_selects
    assert "IS NULL" in topology_page_selects[0]
    assert '"PAYLOAD"' not in topology_page_selects[0]
    assert f'INSERT INTO "{topology_page_table}"' not in large_sql
    assert f'UPDATE "{topology_page_table}"' not in large_sql
    assert f'INSERT INTO "{topology_link_table}"' not in large_sql
    assert large_sql.count('UPDATE "DJANGO_RAY_WORKFLOWPROGRESSNODEDETAIL"') == 1
    assert (
        WorkflowProgressNodeDetail.objects.filter(
            run_storage__execution__task_id=f"workflow-storage-{large_retained_nodes}-0",
            state="RUNNING",
        ).count()
        == 1
    )

    record_property("small_retained_nodes", small_retained_nodes)
    record_property("large_retained_nodes", large_retained_nodes)
    record_property("small_topology_pages", small_topology_pages)
    record_property("large_topology_pages", large_topology_pages)
    record_property("small_sparse_round_trips", small_queries)
    record_property("large_sparse_round_trips", large_queries)
    record_property("small_sparse_elapsed_ms", round(small_ms, 3))
    record_property("large_sparse_elapsed_ms", round(large_ms, 3))
