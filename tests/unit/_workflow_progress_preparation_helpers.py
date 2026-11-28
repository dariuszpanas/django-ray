"""Shared helpers for bounded workflow-topology preparation unit tests."""

from __future__ import annotations

import os
import random
import threading
from collections.abc import Iterable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest

import django_ray.workflow.progress.preparation as preparation
import django_ray.workflow.progress.storage as storage
from django_ray.runtime.context import WorkflowRunIdentity
from tests.workflow_progress_storage_helpers import workflow_node, workflow_node_id


class _OneShot(Iterable[dict[str, Any]]):
    def __init__(self, values: Iterable[dict[str, Any]]) -> None:
        self._values = list(values)
        self._iterated = False

    def __iter__(self) -> Iterator[dict[str, Any]]:
        if self._iterated:
            raise AssertionError("one-shot input was consumed more than once")
        self._iterated = True
        yield from self._values


class _ForeignTopologyCollection(StrEnum):
    NODE = "NODE"


def _identity() -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=141,
        attempt_number=2,
        execution_generation=3,
        run_id="00000000-0000-0000-0000-000000000141",
    )


def _edge(source: str, target: str) -> dict[str, str]:
    return {"source": source, "target": target}


def _topology_with_all_capability_evidence() -> storage.PreparedWorkflowProgressTopology:
    task_node = workflow_node("node-task")
    map_node = workflow_node("node-map")
    map_node["kind"] = "map"
    oversized = workflow_node("node-oversized")
    oversized["runtime_env"] = {f"key-{index}": "value" * 8 for index in range(1_000)}
    topology = preparation.prepare_workflow_progress_topology(
        _identity(),
        1,
        [task_node, map_node, oversized],
        [_edge("node-task", "node-map")],
    )
    assert topology.pages
    assert topology.node_ids
    assert topology.observed_node_ids
    assert topology.node_kinds
    assert topology.edges
    assert topology.truncation_reasons
    assert topology.map_node_ids
    return topology


def _unleased_workspace(tmp_path: Path) -> tuple[preparation.SQLitePreparationWorkspace, Path]:
    parent = tmp_path.resolve(strict=True)
    directory = parent / (preparation._WORKSPACE_PREFIX + "00000000-0000-0000-0000-000000000141")
    directory.mkdir(mode=0o700)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=parent)
    workspace._creator_pid = os.getpid()
    workspace._creator_uid = os.geteuid() if os.name == "posix" else None
    workspace._owner_thread_id = threading.get_ident()
    workspace._owned_parent = parent
    workspace._owned_parent_identity = preparation._PathIdentity.from_stat(parent.stat())
    workspace._owned_directory = directory
    workspace._owned_directory_identity = preparation._PathIdentity.from_stat(directory.stat())
    return workspace, directory


def _partial_lease_workspace(
    tmp_path: Path,
) -> tuple[preparation.SQLitePreparationWorkspace, Path, Path]:
    workspace, directory = _unleased_workspace(tmp_path)
    lease = directory / preparation._LEASE_NAME
    lease.write_text("partial", encoding="utf-8")
    if os.name == "posix":
        lease.chmod(0o600)
    workspace.lease_path = lease
    workspace._lease_identity = preparation._PathIdentity.from_stat(
        os.stat(lease, follow_symlinks=False)
    )
    return workspace, directory, lease


def _quarantined_workspace(
    tmp_path: Path,
) -> tuple[preparation.SQLitePreparationWorkspace, Path, Path]:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    assert workspace.connection is not None
    workspace.connection.close()
    workspace.connection = None
    quarantine = workspace._quarantine_owned_directory()
    return workspace, directory, quarantine


def _ordered_scenario(
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if scenario == "empty":
        return [], []
    if scenario == "short":
        nodes = [workflow_node(workflow_node_id(index)) for index in range(8)]
        edges = [
            _edge(workflow_node_id(index - 1), workflow_node_id(index))
            for index in range(1, len(nodes))
        ]
        return nodes, edges
    if scenario == "truncated-dense":
        monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS", 4)
        monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS", 5)
        nodes = [workflow_node(workflow_node_id(index)) for index in range(10)]
        edges = [
            _edge(workflow_node_id(source), workflow_node_id(target))
            for source in range(len(nodes))
            for target in range(source + 1, len(nodes))
        ]
        return nodes, edges
    if scenario == "oversized-records":
        oversized_body = workflow_node("node-oversized-body")
        oversized_body["runtime_env"] = {f"key-{index}": "value" * 8 for index in range(1_000)}
        oversized_identity = workflow_node("x" * (storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES + 1))
        nodes = [
            oversized_body,
            workflow_node("node-retained-a"),
            workflow_node("node-retained-b"),
            oversized_identity,
        ]
        edges = [
            _edge("node-oversized-body", "node-retained-a"),
            _edge("node-retained-a", "node-retained-b"),
        ]
        return nodes, edges
    raise AssertionError(f"unknown test scenario: {scenario}")


def _reorder(
    values: list[dict[str, Any]],
    ordering: str,
    *,
    seed: int,
) -> list[dict[str, Any]]:
    reordered = list(values)
    if ordering == "reversed":
        reordered.reverse()
    elif ordering == "shuffled":
        random.Random(seed).shuffle(reordered)
    return reordered
