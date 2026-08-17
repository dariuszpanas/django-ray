"""Contract tests for the non-production bounded preparation prototype."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import django_ray.workflow.progress.preparation as preparation
import django_ray.workflow.progress.storage as storage
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.preparation import (
    SQLitePreparationConfig,
    SQLitePreparationWorkspace,
    canonical_topology_evidence,
)
from django_ray.workflow.progress.preparation import (
    WorkflowProgressPreparationCleanupRefusedError as PrototypeCleanupRefusedError,
)
from django_ray.workflow.progress.preparation import (
    WorkflowProgressPreparationConfigurationError as PrototypeConfigurationError,
)
from django_ray.workflow.progress.preparation import (
    WorkflowProgressPreparationSpillExhaustedError as PrototypeSpillExhaustedError,
)
from django_ray.workflow.progress.preparation import (
    WorkflowProgressPreparationWorkspaceAcquisitionError as PrototypeWorkspaceAcquisitionError,
)
from django_ray.workflow.progress.preparation import (
    WorkflowProgressPreparationWorkspaceIntegrityError as PrototypeWorkspaceIntegrityError,
)
from scripts import benchmark_workflow_progress_preparation as benchmark
from scripts import workflow_progress_preparation_prototype as prototype
from tests.workflow_progress_storage_helpers import (
    workflow_detail,
    workflow_node,
    workflow_node_id,
)


class _OneShot(Iterable[dict[str, Any]]):
    def __init__(self, values: Iterable[dict[str, Any]]) -> None:
        self.values = list(values)
        self.iterated = False

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if self.iterated:
            raise AssertionError("one-shot input was consumed more than once")
        self.iterated = True
        yield from self.values


class _CloseTrackingIterator(Iterator[dict[str, Any]]):
    def __init__(self, values: Iterable[dict[str, Any]]) -> None:
        self._values = iter(values)
        self.closed = False

    def __iter__(self) -> _CloseTrackingIterator:
        return self

    def __next__(self) -> dict[str, Any]:
        return next(self._values)

    def close(self) -> None:
        self.closed = True


def _identity() -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=140,
        attempt_number=2,
        execution_generation=3,
        run_id="00000000-0000-0000-0000-000000000140",
    )


def _edge(source: str, target: str) -> dict[str, str]:
    return {"source": source, "target": target}


def _event(index: int, *, prefix: str) -> dict[str, str]:
    return {
        "event": "STATE_CHANGE",
        "state": "RUNNING",
        "label": f"{prefix}-{index:02d}",
        "timestamp": f"2026-07-20T12:00:{index:02d}Z",
    }


def _benchmark_args(
    workspace_parent: Path,
    *,
    timeout_seconds: float = 10.0,
) -> argparse.Namespace:
    return argparse.Namespace(
        workspace_parent=workspace_parent,
        timeout_seconds=timeout_seconds,
        high_edge_factor=2,
        cache_bytes=8 * 1024 * 1024,
        spill_max_bytes=16 * 1024 * 1024,
        control_reserve_bytes=4 * 1024 * 1024,
        node_max_items=1_000_000,
        edge_max_items=4_000_000,
        detail_max_items=1_000_000,
        batch_items=256,
        batch_decoded_bytes=4 * 1024 * 1024,
    )


@pytest.mark.parametrize("ordering", ["ordered", "reversed", "shuffled"])
def test_sqlite_topology_and_detail_match_current_canonical_output(
    tmp_path: Path,
    ordering: str,
) -> None:
    identity = _identity()
    nodes = [workflow_node(workflow_node_id(index)) for index in range(12)]
    nodes[3]["kind"] = "map"
    edges = [_edge(workflow_node_id(index - 1), workflow_node_id(index)) for index in range(1, 12)]
    details = [workflow_detail(workflow_node_id(index)) for index in range(12)]
    details[3]["fanout"] = {
        "max_concurrency": 4,
        "max_items": 12,
        "submitted_items": 0,
        "completed_items": 0,
        "in_flight_items": 0,
        "input_exhausted": False,
    }
    if ordering == "reversed":
        nodes.reverse()
        edges.reverse()
        details.reverse()
    elif ordering == "shuffled":
        random.Random(140).shuffle(nodes)
        random.Random(141).shuffle(edges)
        random.Random(142).shuffle(details)

    expected_topology = storage._prepare_workflow_progress_topology_materialized(
        identity,
        1,
        _OneShot(nodes),
        _OneShot(edges),
    )
    expected_detail = storage.prepare_workflow_progress_detail(
        _OneShot(details),
        topology=expected_topology,
    )
    workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual_topology = workspace.prepare_topology(
            identity,
            1,
            _OneShot(nodes),
            _OneShot(edges),
        )
        actual_detail = workspace.prepare_detail(
            _OneShot(details),
            topology=actual_topology,
        )
        assert canonical_topology_evidence(actual_topology) == canonical_topology_evidence(
            expected_topology
        )
        assert actual_detail == expected_detail
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_sqlite_topology_matches_truncation_and_oversized_identity_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS", 3)
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS", 2)
    identity = _identity()
    nodes = [workflow_node(workflow_node_id(index)) for index in range(6)]
    nodes.append(workflow_node("x" * (storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES + 1)))
    edges = [
        _edge(workflow_node_id(0), workflow_node_id(1)),
        _edge(workflow_node_id(0), workflow_node_id(2)),
        _edge(workflow_node_id(1), workflow_node_id(2)),
        _edge(workflow_node_id(2), workflow_node_id(3)),
    ]
    expected = storage._prepare_workflow_progress_topology_materialized(identity, 1, nodes, edges)
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual = workspace.prepare_topology(identity, 1, _OneShot(nodes), _OneShot(edges))
        assert canonical_topology_evidence(actual) == canonical_topology_evidence(expected)
        assert actual.observed_node_count == 7
        assert actual.retained_node_count == 3
        assert actual.retained_edge_count == 2
        assert set(actual.truncation_reasons) == {
            storage.WorkflowProgressTruncationReason.EDGE_COUNT_LIMIT.value,
            storage.WorkflowProgressTruncationReason.NODE_COUNT_LIMIT.value,
            storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
        }


def test_sqlite_omits_oversized_normalized_record_but_preserves_identity_parity(
    tmp_path: Path,
) -> None:
    identity = _identity()
    oversized = workflow_node("node-oversized-body")
    oversized["runtime_env"] = {f"key-{index}": "value" * 8 for index in range(1_000)}
    nodes = [oversized, workflow_node("node-retained")]
    edges = [_edge("node-oversized-body", "node-retained")]
    details = [workflow_detail("node-oversized-body"), workflow_detail("node-retained")]

    expected_topology = storage._prepare_workflow_progress_topology_materialized(
        identity, 1, nodes, edges
    )
    expected_detail = storage.prepare_workflow_progress_detail(
        details,
        topology=expected_topology,
    )
    workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual_topology = workspace.prepare_topology(identity, 1, nodes, edges)
        actual_detail = workspace.prepare_detail(details, topology=actual_topology)
        assert canonical_topology_evidence(actual_topology) == canonical_topology_evidence(
            expected_topology
        )
        assert actual_detail == expected_detail
        assert actual_topology.observed_node_count == 2
        assert actual_topology.retained_node_count == 1
        assert actual_topology.retained_edge_count == 0
        assert actual_topology.truncation_reasons == (
            storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
        )
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("page_limit_name", "total_limit_name", "reason"),
    [
        (
            "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES",
            "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
            storage.WorkflowProgressTruncationReason.TOPOLOGY_ENCODED_BYTES.value,
        ),
        (
            "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_DECODED_BYTES",
            "WORKFLOW_PROGRESS_TOPOLOGY_MAX_DECODED_BYTES",
            storage.WorkflowProgressTruncationReason.TOPOLOGY_DECODED_BYTES.value,
        ),
    ],
)
def test_sqlite_topology_matches_page_and_total_byte_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_limit_name: str,
    total_limit_name: str,
    reason: str,
) -> None:
    identity = _identity()
    nodes = [workflow_node(workflow_node_id(index)) for index in range(6)]
    edges = [
        _edge(workflow_node_id(index - 1), workflow_node_id(index))
        for index in range(1, len(nodes))
    ]
    normalized_node, _ = storage._normalize_topology_node(nodes[0])
    normalized_edge = storage._normalize_topology_edge(edges[0])
    one_node_page_bytes = len(
        storage._canonical_json_bytes(
            {
                "collection": storage.WorkflowProgressTopologyCollection.NODE.value,
                "records": [normalized_node],
                "schema_version": storage.WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
            }
        )
    )
    one_edge_page_bytes = len(
        storage._canonical_json_bytes(
            {
                "collection": storage.WorkflowProgressTopologyCollection.EDGE.value,
                "records": [normalized_edge],
                "schema_version": storage.WORKFLOW_PROGRESS_STORAGE_PROTOCOL_VERSION,
            }
        )
    )
    monkeypatch.setattr(
        storage,
        page_limit_name,
        max(one_node_page_bytes, one_edge_page_bytes),
    )
    baseline = storage._prepare_workflow_progress_topology_materialized(identity, 1, nodes, edges)
    node_pages = [
        page
        for page in baseline.pages
        if page.collection is storage.WorkflowProgressTopologyCollection.NODE
    ]
    assert len(node_pages) == len(nodes)
    assert all(page.item_count == 1 for page in node_pages)
    assert baseline.pages[-1].collection is storage.WorkflowProgressTopologyCollection.EDGE
    monkeypatch.setattr(
        storage,
        total_limit_name,
        getattr(baseline, total_limit_name.split("_MAX_")[1].lower()) - 1,
    )

    expected = storage._prepare_workflow_progress_topology_materialized(identity, 1, nodes, edges)
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual = workspace.prepare_topology(
            identity,
            1,
            _OneShot(nodes),
            _OneShot(edges),
        )
        assert canonical_topology_evidence(actual) == canonical_topology_evidence(expected)
        assert len(expected.pages) < len(baseline.pages)
        assert expected.retained_edge_count < baseline.retained_edge_count
        assert reason in expected.truncation_reasons


@pytest.mark.parametrize(
    ("limit_kind", "reason"),
    [
        (
            "count",
            storage.WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value,
        ),
        (
            "detail_encoded",
            storage.WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value,
        ),
        (
            "detail_decoded",
            storage.WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value,
        ),
        (
            "combined_encoded",
            storage.WorkflowProgressTruncationReason.DETAIL_ENCODED_BYTES.value,
        ),
        (
            "combined_decoded",
            storage.WorkflowProgressTruncationReason.DETAIL_DECODED_BYTES.value,
        ),
    ],
)
def test_sqlite_detail_matches_count_and_byte_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_kind: str,
    reason: str,
) -> None:
    identity = _identity()
    nodes = [workflow_node(workflow_node_id(index)) for index in range(3)]
    details = [workflow_detail(workflow_node_id(index)) for index in range(3)]
    expected_topology = storage._prepare_workflow_progress_topology_materialized(
        identity, 1, nodes, []
    )
    baseline = storage.prepare_workflow_progress_detail(details, topology=expected_topology)
    first = baseline.records[0]
    if limit_kind == "count":
        monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS", 1)
    elif limit_kind == "detail_encoded":
        monkeypatch.setattr(
            storage,
            "WORKFLOW_PROGRESS_DETAIL_MAX_ENCODED_BYTES",
            first.encoded_bytes,
        )
    elif limit_kind == "detail_decoded":
        monkeypatch.setattr(
            storage,
            "WORKFLOW_PROGRESS_DETAIL_MAX_DECODED_BYTES",
            first.decoded_bytes,
        )
    elif limit_kind == "combined_encoded":
        monkeypatch.setattr(
            storage,
            "WORKFLOW_PROGRESS_COMBINED_MAX_ENCODED_BYTES",
            expected_topology.encoded_bytes + first.encoded_bytes,
        )
    else:
        monkeypatch.setattr(
            storage,
            "WORKFLOW_PROGRESS_COMBINED_MAX_DECODED_BYTES",
            expected_topology.decoded_bytes + first.decoded_bytes,
        )

    expected_detail = storage.prepare_workflow_progress_detail(
        details,
        topology=expected_topology,
    )
    workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual_topology = workspace.prepare_topology(identity, 1, nodes, [])
        actual_detail = workspace.prepare_detail(details, topology=actual_topology)
        assert canonical_topology_evidence(actual_topology) == canonical_topology_evidence(
            expected_topology
        )
        assert actual_detail == expected_detail
        assert len(expected_detail.records) == 1
        assert reason in expected_detail.truncation_reasons


def test_sqlite_detail_matches_run_global_event_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_RECENT_EVENT_MAX_ITEMS", 2)
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_DETAIL_MAX_ITEMS", 2)
    identity = _identity()
    nodes = [
        workflow_node("node-a"),
        workflow_node("node-b"),
        workflow_node("node-z"),
    ]
    details = [
        {
            **workflow_detail("node-a", state="RUNNING"),
            "recent_events": [_event(0, prefix="a"), _event(1, prefix="a")],
        },
        {
            **workflow_detail("node-b", state="RUNNING"),
            "recent_events": [_event(2, prefix="b"), _event(3, prefix="b")],
        },
        {
            **workflow_detail("node-z", state="RUNNING"),
            "recent_events": [_event(4, prefix="z"), _event(5, prefix="z")],
        },
    ]
    expected_topology = storage._prepare_workflow_progress_topology_materialized(
        identity, 1, nodes, []
    )
    expected_detail = storage.prepare_workflow_progress_detail(
        details,
        topology=expected_topology,
    )

    workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual_topology = workspace.prepare_topology(identity, 1, nodes, [])
        actual_detail = workspace.prepare_detail(details, topology=actual_topology)
        assert actual_detail == expected_detail
        assert [record.node_id for record in expected_detail.records] == ["node-a", "node-b"]
        assert sum(record.event_count for record in expected_detail.records) == 2
        assert json.loads(expected_detail.records[1].payload)["recent_events"] == [
            _event(2, prefix="b"),
            _event(3, prefix="b"),
        ]
        assert (
            storage.WorkflowProgressTruncationReason.DETAIL_COUNT_LIMIT.value
            in expected_detail.truncation_reasons
        )
        assert (
            storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value
            in expected_detail.truncation_reasons
        )


@pytest.mark.parametrize("reporting_policy", ["sampled", "terminal_only", "disabled"])
def test_sqlite_detail_matches_non_full_reporting_policies(
    tmp_path: Path,
    reporting_policy: str,
) -> None:
    identity = _identity()
    nodes = [workflow_node("node-a"), workflow_node("node-b")]
    details = [workflow_detail("node-a")]
    expected_topology = storage._prepare_workflow_progress_topology_materialized(
        identity, 1, nodes, []
    )
    expected_detail = storage.prepare_workflow_progress_detail(
        details,
        topology=expected_topology,
        reporting_policy=reporting_policy,
    )

    workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual_topology = workspace.prepare_topology(identity, 1, nodes, [])
        actual_detail = workspace.prepare_detail(
            details,
            topology=actual_topology,
            reporting_policy=reporting_policy,
        )
        assert actual_detail == expected_detail
        assert (
            storage.WorkflowProgressTruncationReason.REPORTING_POLICY.value
            in expected_detail.truncation_reasons
        )


def test_utf8_blob_primary_keys_preserve_canonical_multibyte_order(tmp_path: Path) -> None:
    identity = _identity()
    node_ids = ["node-界", "node-z", "node-é", "node-a"]
    nodes = [workflow_node(node_id) for node_id in node_ids]
    edges = [
        _edge("node-a", "node-é"),
        _edge("node-é", "node-界"),
        _edge("node-z", "node-界"),
    ]
    expected = storage._prepare_workflow_progress_topology_materialized(identity, 1, nodes, edges)
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        actual = workspace.prepare_topology(identity, 1, _OneShot(nodes), _OneShot(edges))
        assert canonical_topology_evidence(actual) == canonical_topology_evidence(expected)
        column_types = {
            (str(row[1]), str(row[2]))
            for table in ("nodes", "edges", "detail")
            for row in workspace._connection().execute(f"PRAGMA table_info({table})")
            if str(row[1]) in {"node_id", "source", "target"}
        }
        assert column_types == {
            ("node_id", "BLOB"),
            ("source", "BLOB"),
            ("target", "BLOB"),
        }


@pytest.mark.parametrize(
    ("nodes", "edges", "message"),
    [
        (
            [workflow_node("node-a"), workflow_node("node-a")],
            [],
            "topology contains a duplicate node_id",
        ),
        (
            [workflow_node("node-a"), workflow_node("node-b")],
            [_edge("node-a", "node-b"), _edge("node-a", "node-b")],
            "topology contains a duplicate edge",
        ),
        (
            [workflow_node("node-a")],
            [_edge("node-a", "node-missing")],
            "topology edge references an unknown node_id",
        ),
        (
            [workflow_node("node-a")],
            [_edge("token", "node-a")],
            "topology edge source resembles sensitive data",
        ),
    ],
)
def test_sqlite_topology_preserves_exact_identity_failures(
    tmp_path: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    message: str,
) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(storage.WorkflowProgressStorageError, match=message):
        with workspace:
            workspace.prepare_topology(_identity(), 1, _OneShot(nodes), _OneShot(edges))
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_sqlite_topology_preserves_cross_batch_duplicate_edge_failure(
    tmp_path: Path,
) -> None:
    workspace = SQLitePreparationWorkspace(
        SQLitePreparationConfig(batch_max_items=1),
        parent_directory=tmp_path,
    )
    edge = _edge("node-a", "node-b")
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="topology contains a duplicate edge",
    ):
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a"), workflow_node("node-b")],
                [edge, edge],
            )
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_duplicate_node_constraint_precedes_late_invalid_body(tmp_path: Path) -> None:
    invalid_duplicate = workflow_node("node-a")
    invalid_duplicate["kind"] = "unsupported"
    workspace = SQLitePreparationWorkspace(
        SQLitePreparationConfig(batch_max_items=1),
        parent_directory=tmp_path,
    )
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="topology contains a duplicate node_id",
    ):
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a"), invalid_duplicate],
                [],
            )


def test_sqlite_detail_preserves_duplicate_unknown_and_full_set_validation(
    tmp_path: Path,
) -> None:
    identity = _identity()
    nodes = [workflow_node("node-a"), workflow_node("node-b")]

    duplicate_workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="node detail contains a duplicate node_id",
    ):
        with duplicate_workspace:
            topology = duplicate_workspace.prepare_topology(identity, 1, nodes, [])
            duplicate_workspace.prepare_detail(
                [workflow_detail("node-a"), workflow_detail("node-a")],
                topology=topology,
            )

    unknown_workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="node detail references an unknown topology node_id",
    ):
        with unknown_workspace:
            topology = unknown_workspace.prepare_topology(identity, 1, nodes, [])
            unknown_workspace.prepare_detail(
                [workflow_detail("node-a"), workflow_detail("node-missing")],
                topology=topology,
            )

    incomplete_workspace = prototype.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="full detail must contain one record per observed topology node",
    ):
        with incomplete_workspace:
            topology = incomplete_workspace.prepare_topology(identity, 1, nodes, [])
            incomplete_workspace.prepare_detail([workflow_detail("node-a")], topology=topology)

    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("details", "message"),
    [
        (
            [workflow_detail("node-missing") | {"state": "INVALID"}],
            "node detail references an unknown topology node_id",
        ),
        (
            [
                workflow_detail("node-a"),
                workflow_detail("node-a") | {"state": "INVALID"},
            ],
            "node detail contains a duplicate node_id",
        ),
    ],
)
def test_detail_constraints_precede_invalid_body_normalization(
    tmp_path: Path,
    details: list[dict[str, Any]],
    message: str,
) -> None:
    workspace = prototype.SQLitePreparationWorkspace(
        prototype.SQLitePreparationConfig(batch_max_items=1),
        parent_directory=tmp_path,
    )
    with pytest.raises(storage.WorkflowProgressStorageError, match=message):
        with workspace:
            topology = workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                [],
            )
            workspace.prepare_detail(details, topology=topology)


def test_workspace_applies_fixed_sqlite_budgets_and_index_order_plans(tmp_path: Path) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        pragmas = workspace.sqlite_pragmas()
        assert pragmas == {
            "page_size": 4096,
            "cache_size": -8192,
            "mmap_size": 0,
            "temp_store": 2,
            "journal_mode": "off",
            "synchronous": 0,
            "locking_mode": "exclusive",
            "foreign_keys": 1,
            "trusted_schema": 0,
            "max_page_count": 261120,
        }
        plans = workspace.retained_query_plans()
        assert plans == (
            "SCAN nodes",
            "SCAN e",
            "SEARCH source_node USING PRIMARY KEY (node_id=?)",
            "SEARCH target_node USING PRIMARY KEY (node_id=?)",
            "SCAN nodes",
        )


def test_exclusive_workspace_rejects_a_second_connection_reader(tmp_path: Path) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        assert workspace.database_path is not None
        second = sqlite3.connect(workspace.database_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                second.execute("SELECT node_id FROM nodes").fetchall()
        finally:
            second.close()


def test_workspace_rejects_reentry_without_orphaning_the_owned_directory(
    tmp_path: Path,
) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    first_directory = workspace.directory
    first_connection = workspace.connection
    with pytest.raises(RuntimeError, match="may be entered exactly once"):
        workspace.__enter__()
    assert workspace.directory == first_directory
    assert workspace.connection is first_connection
    assert first_directory is not None and first_directory.is_dir()
    workspace.__exit__(None, None, None)
    assert workspace._phase == "closed"
    assert not list(tmp_path.iterdir())
    with pytest.raises(RuntimeError, match="may be entered exactly once"):
        workspace.__enter__()


@pytest.mark.parametrize(
    "config",
    [
        SQLitePreparationConfig(page_bytes=8192),
        SQLitePreparationConfig(cache_bytes=1),
        SQLitePreparationConfig(cache_bytes=8 * 1024 * 1024 + 1024),
        SQLitePreparationConfig(mmap_bytes=1),
        SQLitePreparationConfig(max_spill_bytes=65_537),
        SQLitePreparationConfig(max_spill_bytes=1024 * 1024 * 1024 + 4096),
        SQLitePreparationConfig(control_reserve_bytes=0),
        SQLitePreparationConfig(control_reserve_bytes=4 * 1024 * 1024 + 4096),
        SQLitePreparationConfig(max_node_items=0),
        SQLitePreparationConfig(max_node_items=1_000_001),
        SQLitePreparationConfig(max_edge_items=0),
        SQLitePreparationConfig(max_edge_items=4_000_001),
        SQLitePreparationConfig(batch_max_items=257),
        SQLitePreparationConfig(batch_max_decoded_bytes=4 * 1024 * 1024 + 1),
    ],
)
def test_workspace_configuration_rejects_values_outside_preparation_v1(
    config: SQLitePreparationConfig,
) -> None:
    with pytest.raises(PrototypeConfigurationError):
        config.validated()


def test_item_budget_exhaustion_is_deterministic_and_cleans_workspace(tmp_path: Path) -> None:
    workspace = SQLitePreparationWorkspace(
        SQLitePreparationConfig(max_node_items=2),
        parent_directory=tmp_path,
    )
    with pytest.raises(
        PrototypeSpillExhaustedError,
        match="node item budget exhausted at 2",
    ):
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                (workflow_node(workflow_node_id(index)) for index in range(3)),
                (),
            )
    assert workspace.observed_node_count == 3
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_file_byte_budget_exhaustion_is_deterministic_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    workspace = SQLitePreparationWorkspace(
        SQLitePreparationConfig(
            max_spill_bytes=68 * 1024,
            control_reserve_bytes=4 * 1024,
        ),
        parent_directory=tmp_path,
    )
    with pytest.raises(
        PrototypeSpillExhaustedError,
        match="spill byte budget exhausted",
    ):
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                (workflow_node(workflow_node_id(index)) for index in range(2_000)),
                (),
            )
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
def test_exception_and_cancellation_cleanup_remove_the_workspace(
    tmp_path: Path,
    error_type: type[BaseException],
) -> None:
    def interrupted_nodes() -> Iterator[dict[str, Any]]:
        yield workflow_node("node-a")
        raise error_type("stop preparation")

    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(error_type, match="stop preparation"):
        with workspace:
            workspace.prepare_topology(_identity(), 1, interrupted_nodes(), ())
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_validation_failure_poisons_workspace_even_when_caught_inside_context(
    tmp_path: Path,
) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        with pytest.raises(
            storage.WorkflowProgressStorageError,
            match="duplicate node_id",
        ):
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a"), workflow_node("node-a")],
                (),
            )
        assert workspace._phase == "poisoned"
        with pytest.raises(RuntimeError, match="exactly once"):
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-b")],
                (),
            )


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, RuntimeError])
def test_cooperative_cancellation_runs_before_next_item_and_poisons_workspace(
    tmp_path: Path,
    error_type: type[BaseException],
) -> None:
    checks = 0
    consumed: list[str] = []

    def cancellation_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 5:
            raise error_type("cooperative cancellation")

    def nodes() -> Iterator[dict[str, Any]]:
        consumed.append("first")
        yield workflow_node("node-a")
        consumed.append("second")
        yield workflow_node("node-b")

    workspace = SQLitePreparationWorkspace(
        parent_directory=tmp_path,
        cancellation_check=cancellation_check,
    )
    with pytest.raises(error_type, match="cooperative cancellation"):
        with workspace:
            workspace.prepare_topology(_identity(), 1, nodes(), ())
    assert consumed == ["first"]
    assert workspace._phase == "poisoned"
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_consumption_failure_best_effort_closes_the_active_iterator(tmp_path: Path) -> None:
    checks = 0
    values = _CloseTrackingIterator([workflow_node("node-a"), workflow_node("node-b")])

    def cancellation_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 5:
            raise asyncio.CancelledError("close active input")

    workspace = SQLitePreparationWorkspace(
        parent_directory=tmp_path,
        cancellation_check=cancellation_check,
    )
    with pytest.raises(asyncio.CancelledError, match="close active input"):
        with workspace:
            workspace.prepare_topology(_identity(), 1, values, ())
    assert values.closed
    assert workspace.cleanup_outcome == "removed"


def test_page_construction_checks_cancellation_before_each_output_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ITEMS", 1)
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    inside_page_builder = False
    page_checks = 0

    original_build_pages = workspace._build_pages

    def cancellation_check() -> None:
        nonlocal page_checks
        if inside_page_builder:
            page_checks += 1
            if page_checks == 2:
                raise asyncio.CancelledError("cancel before second output page")

    def observed_build_pages(*args: Any, **kwargs: Any):
        nonlocal inside_page_builder
        inside_page_builder = True
        try:
            return original_build_pages(*args, **kwargs)
        finally:
            inside_page_builder = False

    workspace.cancellation_check = cancellation_check
    monkeypatch.setattr(workspace, "_build_pages", observed_build_pages)
    with pytest.raises(asyncio.CancelledError, match="before second output page"):
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a"), workflow_node("node-b")],
                (),
            )
    assert page_checks == 2
    assert workspace.cleanup_outcome == "removed"


def test_workspace_refuses_cleanup_after_lease_tampering(tmp_path: Path) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.lease_path is not None
    lease = workspace.lease_path
    original = lease.read_text(encoding="utf-8")
    lease.write_text('{"token":"tampered"}', encoding="utf-8")
    with pytest.raises(PrototypeCleanupRefusedError, match="lease token"):
        workspace.__exit__(None, None, None)
    assert workspace.cleanup_outcome == "refused"
    assert workspace.path_exists
    lease.write_text(original, encoding="utf-8")
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_workspace_rejects_unexpected_sidecar_from_total_directory_budget(
    tmp_path: Path,
) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        assert workspace.directory is not None
        sidecar = workspace.directory / "workspace.sqlite3-wal"
        sidecar.write_bytes(b"unexpected")
        with pytest.raises(PrototypeWorkspaceIntegrityError, match="unexpected"):
            workspace._measure_spill()
        sidecar.unlink()


def test_workspace_acquisition_cleans_directory_when_lease_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = Path.open

    def fail_lease(path: Path, *args: Any, **kwargs: Any):
        if path.name == "owner.lease":
            raise OSError("lease write failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_lease)
    with pytest.raises(PrototypeWorkspaceAcquisitionError, match="lease initialization failed"):
        with SQLitePreparationWorkspace(parent_directory=tmp_path):
            pass
    assert not list(tmp_path.iterdir())


def test_workspace_acquisition_refuses_to_remove_an_unproven_raced_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = Path.open

    def race_lease(path: Path, *args: Any, **kwargs: Any):
        if path.name == "owner.lease":
            with original_open(path, "x", encoding="utf-8", errors="strict") as lease:
                lease.write("not-owned")
            raise OSError("lease write raced")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", race_lease)
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(PrototypeCleanupRefusedError, match="unleased directory is not empty"):
        workspace.__enter__()
    monkeypatch.undo()
    assert workspace.cleanup_outcome == "refused"
    assert workspace.directory is not None
    raced_lease = workspace.directory / "owner.lease"
    assert raced_lease.read_text(encoding="utf-8") == "not-owned"
    raced_lease.unlink()
    workspace.directory.rmdir()


def test_workspace_does_not_clean_a_directory_it_failed_to_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = UUID("00000000-0000-0000-0000-000000000140")
    collision = tmp_path / f"django-ray-preparation-{workspace_id}"
    collision.mkdir()
    database = collision / "workspace.sqlite3"
    lease = collision / "owner.lease"
    database.write_bytes(b"not-owned")
    lease.write_text("not-owned", encoding="utf-8")
    monkeypatch.setattr(preparation, "uuid4", lambda: workspace_id)

    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(PrototypeWorkspaceAcquisitionError, match="directory creation failed"):
        workspace.__enter__()
    assert workspace.cleanup_outcome == "not_created"
    assert database.read_bytes() == b"not-owned"
    assert lease.read_text(encoding="utf-8") == "not-owned"


def test_workspace_does_not_follow_a_dangling_uuid_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = UUID("00000000-0000-0000-0000-000000000140")
    collision = tmp_path / f"django-ray-preparation-{workspace_id}"
    escaped_parent = tmp_path / "outside"
    escaped_parent.mkdir()
    escaped = escaped_parent / "workspace"
    try:
        collision.symlink_to(escaped, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    monkeypatch.setattr(preparation, "uuid4", lambda: workspace_id)

    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(PrototypeWorkspaceAcquisitionError, match="directory creation failed"):
        workspace.__enter__()
    assert workspace.cleanup_outcome == "not_created"
    assert os.path.lexists(collision)
    assert collision.is_symlink()
    assert not escaped.exists()
    collision.unlink()


def test_workspace_refuses_a_dangling_replacement_as_present_state(tmp_path: Path) -> None:
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path / "missing-probe", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    probe.unlink()

    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.connection is not None
    assert workspace.database_path is not None
    assert workspace.lease_path is not None
    assert workspace.directory is not None
    directory = workspace.directory
    workspace.connection.close()
    workspace.connection = None
    workspace.database_path.unlink()
    workspace.lease_path.unlink()
    directory.rmdir()
    directory.symlink_to(tmp_path / "missing-workspace", target_is_directory=True)

    assert workspace.path_exists
    with pytest.raises(PrototypeWorkspaceIntegrityError, match="path is invalid"):
        workspace._measure_spill()
    with pytest.raises(PrototypeCleanupRefusedError, match="replaced or redirected"):
        workspace.__exit__(None, None, None)
    assert workspace.cleanup_outcome == "refused"
    assert directory.is_symlink()
    directory.unlink()


def test_workspace_refuses_cleanup_when_owned_directory_was_renamed(tmp_path: Path) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.connection is not None
    assert workspace.directory is not None
    workspace.connection.close()
    workspace.connection = None
    directory = workspace.directory
    moved = tmp_path / "moved-preparation-workspace"
    directory.rename(moved)

    with pytest.raises(PrototypeCleanupRefusedError, match="disappeared before owned cleanup"):
        workspace.__exit__(None, None, None)
    assert workspace.cleanup_outcome == "refused"
    assert moved.is_dir()
    (moved / "workspace.sqlite3").unlink()
    (moved / "owner.lease").unlink()
    moved.rmdir()


def test_missing_database_fails_accounting_and_refuses_normal_cleanup(tmp_path: Path) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.connection is not None
    assert workspace.database_path is not None
    workspace.connection.close()
    workspace.connection = None
    workspace.database_path.unlink()
    with pytest.raises(PrototypeWorkspaceIntegrityError, match="missing"):
        workspace._measure_spill()
    with pytest.raises(PrototypeCleanupRefusedError, match="unowned entry"):
        workspace.__exit__(None, None, None)
    workspace.database_path.write_bytes(b"")
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_workspace_rejects_ineffective_pragmas_before_schema_or_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SQLitePreparationWorkspace.sqlite_pragmas

    def ineffective(workspace: SQLitePreparationWorkspace) -> dict[str, int | str]:
        values = original(workspace)
        values["mmap_size"] = 1
        return values

    monkeypatch.setattr(SQLitePreparationWorkspace, "sqlite_pragmas", ineffective)
    with pytest.raises(PrototypeConfigurationError, match="exact preparation-v1 PRAGMA"):
        with SQLitePreparationWorkspace(parent_directory=tmp_path):
            pass
    assert not list(tmp_path.iterdir())


def test_workspace_rejects_temp_query_plan_before_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SQLitePreparationWorkspace,
        "_retained_query_plans_by_statement",
        lambda _workspace: (("USE TEMP B-TREE FOR ORDER BY",), (), ()),
    )
    with pytest.raises(PrototypeConfigurationError, match="temporary storage"):
        with SQLitePreparationWorkspace(parent_directory=tmp_path):
            pass
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("plans", "message"),
    [
        (
            (
                ("SCAN nodes",),
                (
                    "SCAN e",
                    "SEARCH source_node USING AUTOMATIC COVERING INDEX (node_id=?)",
                    "SEARCH target_node USING PRIMARY KEY (node_id=?)",
                ),
                ("SCAN nodes",),
            ),
            "unbudgeted temporary storage",
        ),
        (
            (
                ("SCAN nodes",),
                (
                    "SCAN e",
                    "SEARCH source_node USING PRIMARY KEY (node_id=?)",
                ),
                ("SCAN nodes",),
            ),
            "drifted from its primary-key ordering contract",
        ),
        (
            (
                ("SEARCH nodes USING PRIMARY KEY (node_id>?)",),
                (
                    "SCAN e",
                    "SEARCH source_node USING PRIMARY KEY (node_id=?)",
                    "SEARCH target_node USING PRIMARY KEY (node_id=?)",
                ),
                ("SCAN nodes",),
            ),
            "drifted from its primary-key ordering contract",
        ),
    ],
)
def test_workspace_rejects_statement_specific_query_plan_drift_before_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plans: tuple[tuple[str, ...], ...],
    message: str,
) -> None:
    monkeypatch.setattr(
        SQLitePreparationWorkspace,
        "_retained_query_plans_by_statement",
        lambda _workspace: plans,
    )

    with pytest.raises(PrototypeConfigurationError, match=message):
        with SQLitePreparationWorkspace(parent_directory=tmp_path):
            pass

    assert not list(tmp_path.iterdir())


def test_injected_constraint_insert_failure_poisons_and_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)

    def fail_insert(_node_key: bytes) -> None:
        raise sqlite3.IntegrityError("injected constraint insert failure")

    with pytest.raises(sqlite3.IntegrityError, match="injected constraint insert failure"):
        with workspace:
            monkeypatch.setattr(workspace, "_insert_node_identity", fail_insert)
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                (),
            )
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_injected_selection_failure_poisons_and_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)

    def fail_selection() -> list[sqlite3.Row]:
        raise OSError("injected selection failure")

    with pytest.raises(OSError, match="injected selection failure"):
        with workspace:
            monkeypatch.setattr(workspace, "_select_node_rows", fail_selection)
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                (),
            )
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_injected_page_construction_failure_poisons_and_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SQLitePreparationWorkspace(parent_directory=tmp_path)

    def fail_page_construction(*_args: Any, **_kwargs: Any):
        raise RuntimeError("injected page construction failure")

    with pytest.raises(RuntimeError, match="injected page construction failure"):
        with workspace:
            monkeypatch.setattr(workspace, "_build_pages", fail_page_construction)
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                (),
            )
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_subprocess_benchmark_reports_resources_cardinality_spill_and_cleanup(
    tmp_path: Path,
) -> None:
    output = tmp_path / "preparation-report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(benchmark.__file__).resolve()),
            "--nodes",
            "16",
            "--profiles",
            "sparse",
            "high-edge",
            "--high-edge-factor",
            "2",
            "--spill-max-bytes",
            str(16 * 1024 * 1024),
            "--workspace-parent",
            str(tmp_path / "workspaces"),
            "--output",
            str(output),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "[1/2] starting nodes=16 profile=sparse" in completed.stderr
    assert "[2/2] completed nodes=16 profile=high-edge" in completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert report["required_scale"] is False
    assert report["source_revision"]
    assert report["implementation_digest"]
    assert report["source_snapshot_before"] == report["source_snapshot_after"]
    assert "parent watchdog" in report["cleanup_contract"]
    assert [case["profile"] for case in report["cases"]] == ["sparse", "high-edge"]
    for case in report["cases"]:
        assert case["observed_nodes"] == 16
        assert case["observed_edges"] == (15 if case["profile"] == "sparse" else 32)
        assert case["observed_detail"] == 16
        assert case["retained_nodes"] == 16
        assert case["retained_edges"] == case["observed_edges"]
        assert case["retained_detail"] == 16
        assert case["detail_encoded_bytes"] > 0
        assert case["detail_decoded_bytes"] > 0
        assert case["truncation_reasons"] == sorted(
            set(case["topology_truncation_reasons"]) | set(case["detail_truncation_reasons"])
        )
        assert case["v1_output_limits"] == benchmark._v1_output_limits()
        assert case["v1_output_limits"]["topology_node_max_items"] == 25_000
        assert case["v1_output_limits"]["topology_edge_max_items"] == 100_000
        assert case["v1_output_limits"]["detail_max_items"] == 25_000
        assert case["wall_seconds"] >= 0
        assert case["cpu_seconds"] >= 0
        assert case["tracemalloc_peak_bytes"] > 0
        assert case["peak_rss_bytes"] is None or case["peak_rss_bytes"] > 0
        assert case["bounded_phase_tracemalloc_current_bytes"] is None
        assert case["bounded_phase_tracemalloc_peak_bytes"] is None
        assert case["bounded_phase_peak_rss_bytes"] is None
        assert case["bounded_phase_rss_measurement"] is None
        assert case["end_to_end_tracemalloc_peak_bytes"] == case["tracemalloc_peak_bytes"]
        assert case["end_to_end_peak_rss_bytes"] == case["peak_rss_bytes"]
        rss = case["rss_measurement"]
        assert rss["peak_bytes"] == case["peak_rss_bytes"]
        assert rss["method"] != "unavailable" or rss["peak_bytes"] is None
        assert rss["scope"]
        assert rss["baseline_bytes"] is None or rss["baseline_bytes"] > 0
        assert 0 < case["spill_peak_bytes"] <= 16 * 1024 * 1024
        assert case["spill_items"] == (
            case["observed_nodes"] + case["observed_edges"] + case["observed_detail"]
        )
        assert case["cleanup"] == {
            "worker_context": "removed",
            "workspace_exists_after_context": False,
            "parent_watchdog": "removed",
            "scenario_root_exists_after_parent": False,
        }
        assert case["environment"]["python"]
        assert case["environment"]["sqlite"]
        filesystem = case["environment"]["filesystem"]
        assert len(filesystem["identity_sha256"]) == 64
        assert filesystem["identity_method"] == "sha256(platform, st_dev)"
        assert "workspace" not in json.dumps(filesystem).lower()
        assert all("TEMP B-TREE" not in plan.upper() for plan in case["query_plans"])
    assert report["forced_termination"] == {
        "outcome": "forcibly-terminated",
        "readiness_observed": True,
        "workspace_open_before_kill": True,
        "worker_returncode": report["forced_termination"]["worker_returncode"],
        "durable_candidate_exists_before_cleanup": False,
        "durable_candidate_exists_after_cleanup": False,
        "parent_watchdog": "removed",
        "scenario_root_exists_after_parent": False,
        "filesystem": report["forced_termination"]["filesystem"],
    }
    assert report["forced_termination"]["worker_returncode"] != 0
    assert len(report["forced_termination"]["filesystem"]["identity_sha256"]) == 64
    assert not list((tmp_path / "workspaces").iterdir())


def test_parent_watchdog_removes_scenario_workspace_after_worker_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _benchmark_args(tmp_path, timeout_seconds=0.5)
    heartbeat = tmp_path / "descendant-heartbeat.txt"
    child_source = """
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
counter = 0
while True:
    path.write_text(str(counter), encoding="utf-8")
    counter += 1
    time.sleep(0.02)
"""
    parent_source = """
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
time.sleep(60)
"""
    command = [sys.executable, "-c", parent_source, child_source, str(heartbeat)]
    monkeypatch.setattr(
        benchmark,
        "_worker_command",
        lambda *_args, **_kwargs: command,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        benchmark._run_scenario(args, nodes=16, profile="sparse")
    assert heartbeat.is_file()
    stopped_value = heartbeat.read_text(encoding="utf-8")
    time.sleep(0.2)
    assert heartbeat.read_text(encoding="utf-8") == stopped_value
    assert not any(path.name.startswith(benchmark.SCENARIO_PREFIX) for path in tmp_path.iterdir())


def test_parent_watchdog_cleanup_failure_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _benchmark_args(tmp_path, timeout_seconds=0.01)

    def time_out(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        raise subprocess.TimeoutExpired("worker", 0.01)

    monkeypatch.setattr(benchmark, "_run_worker_process", time_out)
    monkeypatch.setattr(benchmark, "_remove_scenario_root", lambda *_args, **_kwargs: "failed")
    with pytest.raises(RuntimeError, match="parent watchdog cleanup failed") as raised:
        benchmark._run_scenario(args, nodes=16, profile="sparse")
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)


def test_parent_watchdog_detects_a_dangling_replacement_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_root = tmp_path / f"{benchmark.SCENARIO_PREFIX}cleanup-race"
    scenario_root.mkdir()
    dangling_target = tmp_path / "missing-scenario-root"
    original_rmtree = benchmark.shutil.rmtree

    def replace_with_dangling(path: Path) -> None:
        original_rmtree(path)
        try:
            Path(path).symlink_to(dangling_target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks are unavailable: {error}")

    monkeypatch.setattr(benchmark.shutil, "rmtree", replace_with_dangling)
    outcome = benchmark._remove_scenario_root(
        scenario_root,
        expected_parent=tmp_path,
    )

    assert outcome == "failed"
    assert os.path.lexists(scenario_root)
    scenario_root.unlink()


def test_parent_watchdog_fails_when_scenario_root_was_renamed(tmp_path: Path) -> None:
    scenario_root = tmp_path / f"{benchmark.SCENARIO_PREFIX}rename-race"
    scenario_root.mkdir()
    moved = tmp_path / "moved-benchmark-scenario"
    scenario_root.rename(moved)

    assert (
        benchmark._remove_scenario_root(
            scenario_root,
            expected_parent=tmp_path,
        )
        == "failed"
    )
    assert moved.is_dir()
    moved.rmdir()


def test_forced_termination_readiness_requires_parent_nonce() -> None:
    workspace_name = "django-ray-preparation-00000000-0000-0000-0000-000000000140"
    readiness = {
        "nonce": "parent-owned-nonce",
        "state": "workspace-open",
        "workspace_name": workspace_name,
    }
    assert (
        benchmark._validated_worker_readiness(
            readiness,
            expected_nonce="parent-owned-nonce",
        )
        == workspace_name
    )
    with pytest.raises(RuntimeError, match="readiness nonce is invalid"):
        benchmark._validated_worker_readiness(
            readiness,
            expected_nonce="different-parent-nonce",
        )


def test_benchmark_rejects_source_changes_during_measurement() -> None:
    before = {
        "revision": "abc",
        "dirty": False,
        "implementation_digest": "digest-before",
    }
    benchmark._require_unchanged_source(before, dict(before))
    with pytest.raises(RuntimeError, match="changed during run"):
        benchmark._require_unchanged_source(
            before,
            before | {"implementation_digest": "digest-after"},
        )


def test_benchmark_scale_shortcut_includes_required_large_profiles() -> None:
    assert benchmark.DEFAULT_NODES == (100, 500)
    assert benchmark.REQUIRED_SCALE_NODES == (25_000, 100_000, 250_000)
    assert benchmark.DEFAULT_PROFILES == ("sparse", "high-edge")

    common = {
        "nodes": None,
        "profiles": ["sparse", "high-edge"],
        "high_edge_factor": 8,
        "node_max_items": 1_000_000,
        "edge_max_items": 4_000_000,
        "timeout_seconds": None,
    }
    normal = argparse.Namespace(required_scale=False, **common)
    assert benchmark._validated_nodes(normal) == benchmark.DEFAULT_NODES
    assert normal.timeout_seconds == benchmark.DEFAULT_TIMEOUT_SECONDS

    required = argparse.Namespace(required_scale=True, **common)
    assert benchmark._validated_nodes(required) == benchmark.REQUIRED_SCALE_NODES
    assert required.timeout_seconds == benchmark.REQUIRED_SCALE_TIMEOUT_SECONDS

    incomplete = argparse.Namespace(
        required_scale=True,
        **(common | {"profiles": ["sparse"]}),
    )
    with pytest.raises(ValueError, match="requires both sparse and high-edge profiles"):
        benchmark._validated_nodes(incomplete)
