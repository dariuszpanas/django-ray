"""Production integration tests for bounded workflow-topology preparation."""

from __future__ import annotations

import ast
import asyncio
import gc
import json
import os
import random
import sqlite3
import subprocess
import sys
import threading
import weakref
from collections.abc import Iterable, Iterator
from copy import deepcopy
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import django_ray.workflow.progress.preparation as preparation
import django_ray.workflow.progress.storage as storage
from django_ray.runtime.context import WorkflowRunIdentity
from scripts import benchmark_workflow_progress_preparation as benchmark
from tests.workflow_progress_storage_helpers import workflow_node, workflow_node_id


def test_storage_does_not_own_or_import_topology_preparation() -> None:
    assert not hasattr(storage, "prepare_workflow_progress_topology")

    source = Path(storage.__file__).read_text(encoding="utf-8")
    imports = {
        node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)
    }
    assert "django_ray.workflow.progress.preparation" not in imports


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


@pytest.mark.parametrize(
    "scenario",
    ["empty", "short", "truncated-dense", "oversized-records"],
)
@pytest.mark.parametrize("ordering", ["ordered", "reversed", "shuffled"])
def test_public_spill_preparer_has_byte_for_byte_materialized_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    ordering: str,
) -> None:
    monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(tmp_path))
    nodes, edges = _ordered_scenario(scenario, monkeypatch)
    nodes = _reorder(nodes, ordering, seed=141)
    edges = _reorder(edges, ordering, seed=142)

    expected = storage._prepare_workflow_progress_topology_materialized(
        _identity(),
        1,
        _OneShot(nodes),
        _OneShot(edges),
    )
    actual = preparation.prepare_workflow_progress_topology(
        _identity(),
        1,
        _OneShot(nodes),
        _OneShot(edges),
    )

    assert actual == expected
    assert preparation.canonical_topology_evidence(
        actual
    ) == preparation.canonical_topology_evidence(expected)
    assert actual.observed_node_ids == expected.observed_node_ids
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("input_kind", ["one-shot", "replayable"])
@pytest.mark.parametrize("failure", ["duplicate-node", "duplicate-edge", "unknown-edge"])
def test_spill_workspace_matches_late_materialized_identity_failures(
    tmp_path: Path,
    input_kind: str,
    failure: str,
) -> None:
    nodes = [workflow_node(workflow_node_id(index)) for index in range(300)]
    edges = [
        _edge(workflow_node_id(index - 1), workflow_node_id(index))
        for index in range(1, len(nodes))
    ]
    if failure == "duplicate-node":
        nodes.append(workflow_node(workflow_node_id(0)))
        message = "topology contains a duplicate node_id"
    elif failure == "duplicate-edge":
        edges.append(dict(edges[0]))
        message = "topology contains a duplicate edge"
    else:
        edges.append(_edge(workflow_node_id(0), "node-missing"))
        message = "topology edge references an unknown node_id"

    def input_values(values: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        return _OneShot(values) if input_kind == "one-shot" else list(values)

    with pytest.raises(storage.WorkflowProgressStorageError, match=message) as expected:
        storage._prepare_workflow_progress_topology_materialized(
            _identity(),
            1,
            input_values(nodes),
            input_values(edges),
        )

    workspace = preparation.SQLitePreparationWorkspace(
        preparation.SQLitePreparationConfig(batch_max_items=7),
        parent_directory=tmp_path,
    )
    with pytest.raises(type(expected.value), match=message) as actual:
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                input_values(nodes),
                input_values(edges),
            )

    assert str(actual.value) == str(expected.value)
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            preparation.SQLitePreparationConfig(cache_bytes=True),
            "cache_bytes must be an integer",
        ),
        (
            preparation.SQLitePreparationConfig(
                max_spill_bytes=68 * 1024,
                control_reserve_bytes=8 * 1024,
            ),
            "leave at least 64 KiB",
        ),
    ],
)
def test_configuration_rejects_ambiguous_or_insufficient_resource_budgets(
    config: preparation.SQLitePreparationConfig,
    message: str,
) -> None:
    with pytest.raises(
        preparation.WorkflowProgressPreparationConfigurationError,
        match=message,
    ):
        config.validated()


def test_budget_connection_preserves_non_capacity_sqlite_errors(tmp_path: Path) -> None:
    database = tmp_path / "locked.sqlite3"
    writer = sqlite3.connect(
        database,
        timeout=0,
        factory=preparation._BudgetConnection,
    )
    reader = sqlite3.connect(database, timeout=0)
    try:
        writer.execute("CREATE TABLE records (value INTEGER NOT NULL)")
        writer.commit()

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            writer.executemany("INSERT INTO missing(value) VALUES (?)", [(1,)])
        with pytest.raises(sqlite3.OperationalError, match="syntax error"):
            writer.executescript("THIS IS NOT SQL;")

        reader.execute("BEGIN")
        reader.execute("SELECT * FROM records").fetchall()
        writer.execute("INSERT INTO records(value) VALUES (1)")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            writer.commit()
        writer.rollback()
        reader.rollback()
    finally:
        writer.close()
        reader.close()


@pytest.mark.parametrize("parent_source", ["default", "passed"])
def test_workspace_rejects_unsafe_preexisting_parent_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_source: str,
) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("untrusted", encoding="utf-8")
    if parent_source == "default":
        monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(parent))
        workspace = preparation.SQLitePreparationWorkspace()
    else:
        workspace = preparation.SQLitePreparationWorkspace(parent_directory=parent)

    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="parent is not a directory",
    ):
        workspace.__enter__()

    assert workspace.cleanup_outcome == "not_created"
    assert not workspace.path_exists


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode contract")
def test_workspace_rejects_nonsticky_writable_parent(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=parent)

    try:
        with pytest.raises(
            preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
            match="parent permissions are unsafe",
        ):
            workspace.__enter__()
    finally:
        parent.chmod(0o700)

    assert workspace.cleanup_outcome == "not_created"
    assert not workspace.path_exists


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership contract")
def test_workspace_rejects_parent_owned_by_an_untrusted_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_stat = os.stat(tmp_path, follow_symlinks=False)
    monkeypatch.setattr(preparation.os, "geteuid", lambda: int(parent_stat.st_uid) + 1)

    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="parent owner is unsafe",
    ):
        preparation.SQLitePreparationWorkspace._validate_acquisition_parent_stat(parent_stat)


def test_acquisition_parent_inspection_error_is_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path.resolve(strict=True)
    original_stat = preparation.os.stat
    secret_path = "customer-secret/acquisition-parent"

    def fail_parent_stat(path: object, *args: Any, **kwargs: Any):
        if Path(path) == parent:
            raise OSError(secret_path)
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.os, "stat", fail_parent_stat)
        with pytest.raises(
            preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
            match="parent inspection failed",
        ) as captured:
            preparation.SQLitePreparationWorkspace(parent_directory=parent).__enter__()

    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_default_temp_lookup_error_is_pathless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "customer-secret/default-temp"
    workspace = preparation.SQLitePreparationWorkspace()

    def fail_temp_lookup() -> str:
        raise FileNotFoundError(secret_path)

    monkeypatch.setattr(preparation.tempfile, "gettempdir", fail_temp_lookup)
    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="parent inspection failed",
    ) as captured:
        workspace.__enter__()

    assert workspace.cleanup_outcome == "not_created"
    assert not workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_parent_symlink_loop_resolution_error_is_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "customer-secret-parent"
    secret_path = str(parent.resolve())
    original_resolve = Path.resolve

    def fail_loop_resolution(path: Path, *args: Any, **kwargs: Any):
        if path == parent:
            raise RuntimeError(f"Symlink loop from {secret_path!r}")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_loop_resolution)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=parent)
    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="parent inspection failed",
    ) as captured:
        workspace.__enter__()

    assert workspace.cleanup_outcome == "not_created"
    assert not workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("phase", "message"),
    [
        ("parent", "parent revalidation failed"),
        ("directory", "private directory inspection failed"),
    ],
)
def test_acquisition_revalidation_errors_are_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    message: str,
) -> None:
    secret_path = f"customer-secret/{phase}"
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        target = workspace._owned_parent if phase == "parent" else workspace.directory
        assert target is not None
        original_stat = preparation.os.stat

        def fail_target_stat(path: object, *args: Any, **kwargs: Any):
            if Path(path) == target:
                raise OSError(secret_path)
            return original_stat(path, *args, **kwargs)

        operation = (
            workspace._revalidate_acquisition_parent
            if phase == "parent"
            else workspace._validate_new_workspace_directory
        )
        with monkeypatch.context() as scoped:
            scoped.setattr(preparation.os, "stat", fail_target_stat)
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
                match=message,
            ) as captured:
                operation()

        assert secret_path not in str(captured.value)
        assert secret_path not in repr(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


def test_private_workspace_validation_rejects_non_directory_and_missing_lease_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        directory = workspace.directory
        database_path = workspace.database_path
        lease_path = workspace.lease_path
        assert directory is not None
        assert database_path is not None
        assert lease_path is not None
        file_stat = os.stat(database_path, follow_symlinks=False)
        original_stat = preparation.os.stat

        def substitute_file_stat(path: object, *args: Any, **kwargs: Any):
            if Path(path) == directory:
                return file_stat
            return original_stat(path, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(preparation.os, "stat", substitute_file_stat)
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
                match="private path is not a directory",
            ):
                workspace._validate_new_workspace_directory()

        workspace.lease_path = None
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
                match="owner lease path is unavailable",
            ):
                workspace._initialize_owner_lease(preparation.UUID(int=0))
        finally:
            workspace.lease_path = lease_path


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode contract")
def test_private_workspace_validation_rejects_unsafe_mode(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        directory = workspace.directory
        assert directory is not None
        directory.chmod(0o755)
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
                match="private directory ownership is unsafe",
            ):
                workspace._validate_new_workspace_directory()
        finally:
            directory.chmod(0o700)


def test_parent_identity_replacement_during_acquisition_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "moved-parent"
    marker = parent / "replacement-marker"
    original_create = preparation.SQLitePreparationWorkspace._create_private_workspace_directory

    def create_then_replace(directory: Path) -> None:
        original_create(directory)
        parent.rename(moved_parent)
        parent.mkdir(mode=0o700)
        marker.write_text("do-not-delete", encoding="utf-8")

    monkeypatch.setattr(
        preparation.SQLitePreparationWorkspace,
        "_create_private_workspace_directory",
        staticmethod(create_then_replace),
    )
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=parent)

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="ownership identity is incomplete",
    ):
        workspace.__enter__()

    assert workspace.cleanup_outcome == "refused"
    assert marker.read_text(encoding="utf-8") == "do-not-delete"
    assert workspace.directory is not None
    orphan = moved_parent / workspace.directory.name
    assert orphan.is_dir()
    marker.unlink()
    parent.rmdir()
    orphan.rmdir()
    moved_parent.rmdir()


def test_invalid_topology_version_poisons_and_cleans_workspace(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="topology_version must be a positive integer",
    ):
        with workspace:
            workspace.prepare_topology(_identity(), 0, [], [])

    assert workspace._phase == "poisoned"
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_impossible_null_node_identity_fails_closed_and_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "_bounded_identity_text", lambda *_args, **_kwargs: None)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with pytest.raises(AssertionError, match="normalized to None"):
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                [],
            )

    assert workspace._phase == "poisoned"
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_empty_manifest_limit_failure_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_MANIFEST_MAX_ENCODED_BYTES", 0)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with pytest.raises(
        storage.WorkflowProgressStorageLimitError,
        match="empty topology manifest exceeds",
    ):
        with workspace:
            workspace.prepare_topology(_identity(), 1, [], [])

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_total_limit_can_remove_a_node_page_with_materialized_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [workflow_node(workflow_node_id(index)) for index in range(4)]
    baseline = storage._prepare_workflow_progress_topology_materialized(
        _identity(),
        1,
        nodes,
        [],
    )
    assert baseline.pages
    assert all(
        page.collection is storage.WorkflowProgressTopologyCollection.NODE
        for page in baseline.pages
    )
    monkeypatch.setattr(
        storage,
        "WORKFLOW_PROGRESS_TOPOLOGY_MAX_ENCODED_BYTES",
        baseline.encoded_bytes - 1,
    )
    expected = storage._prepare_workflow_progress_topology_materialized(
        _identity(),
        1,
        nodes,
        [],
    )
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        actual = workspace.prepare_topology(_identity(), 1, nodes, [])

    assert preparation.canonical_topology_evidence(
        actual
    ) == preparation.canonical_topology_evidence(expected)
    assert actual.retained_node_count < baseline.retained_node_count
    assert workspace.cleanup_outcome == "removed"


def test_indivisible_topology_record_page_failure_cleans_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_PAGE_MAX_ENCODED_BYTES", 1)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with pytest.raises(
        storage.WorkflowProgressStorageLimitError,
        match="one topology record cannot fit",
    ):
        with workspace:
            workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                [],
            )

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_truncated_node_and_edge_records_preserve_materialized_parity(tmp_path: Path) -> None:
    node = workflow_node("node-a")
    node["label"] = "x" * (storage.WORKFLOW_PROGRESS_LABEL_MAX_BYTES + 1)
    oversized_edge = _edge(
        "x" * (storage.WORKFLOW_PROGRESS_NODE_ID_MAX_BYTES + 1),
        "node-a",
    )
    expected = storage._prepare_workflow_progress_topology_materialized(
        _identity(),
        1,
        [node],
        [oversized_edge],
    )
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        candidate = workspace.prepare_topology(_identity(), 1, [node], [oversized_edge])
        workspace.prepare_legacy_detachment(candidate)
    actual = workspace.detach_legacy_topology(candidate)

    assert actual == expected
    assert actual.observed_edge_count == 1
    assert actual.retained_edge_count == 0
    assert actual.truncation_reasons == (
        storage.WorkflowProgressTruncationReason.RECORD_SIZE_LIMIT.value,
    )


def test_one_decoded_item_cannot_escape_the_transaction_batch_budget(tmp_path: Path) -> None:
    node = workflow_node("node-a")
    node["runtime_env"] = {f"key-{index}": "x" * 100 for index in range(50)}
    workspace = preparation.SQLitePreparationWorkspace(
        preparation.SQLitePreparationConfig(batch_max_decoded_bytes=4 * 1024),
        parent_directory=tmp_path,
    )

    with pytest.raises(
        preparation.WorkflowProgressPreparationSpillExhaustedError,
        match="one decoded preparation item exceeds",
    ):
        with workspace:
            workspace.prepare_topology(_identity(), 1, [node], [])

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_transaction_batch_postcondition_fails_closed(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        with pytest.raises(AssertionError, match="escaped its preflighted transaction batch"):
            workspace._record_batch_item(
                1,
                items=workspace.config.batch_max_items,
                decoded_bytes=0,
            )


def test_workspace_rejects_nested_input_and_closes_active_iterator(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        outer = workspace._cancelable_values([workflow_node("node-a")])
        assert next(outer)["node_id"] == "node-a"
        inner = workspace._cancelable_values([])
        with pytest.raises(RuntimeError, match="cannot be nested"):
            next(inner)
        workspace._close_active_input_iterator()
        outer.close()
        assert workspace._active_input_iterator is None

        class CloseRaises(Iterator[dict[str, Any]]):
            def __next__(self) -> dict[str, Any]:
                raise StopIteration

            def close(self) -> None:
                raise RuntimeError("best-effort close failure")

        workspace._close_iterator(CloseRaises())


def test_unopened_workspace_helpers_fail_closed_without_creating_state() -> None:
    workspace = preparation.SQLitePreparationWorkspace()

    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceIntegrityError,
        match="workspace is missing",
    ):
        workspace._measure_spill()
    with pytest.raises(RuntimeError, match="workspace is not open"):
        workspace.sqlite_pragmas()

    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "not_created"
    assert not workspace.path_exists


def test_control_reserve_exhaustion_is_detected_before_cleanup(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(
        preparation.SQLitePreparationConfig(control_reserve_bytes=4 * 1024),
        parent_directory=tmp_path,
    )

    with workspace:
        assert workspace.lease_path is not None
        original = workspace.lease_path.read_bytes()
        workspace.lease_path.write_bytes(b"x" * (4 * 1024 + 1))
        with pytest.raises(
            preparation.WorkflowProgressPreparationSpillExhaustedError,
            match="control reserve byte budget exhausted",
        ):
            workspace._measure_spill()
        workspace.lease_path.write_bytes(original)

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_spill_measurement_rejects_owned_entry_with_wrong_file_type(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        assert workspace.lease_path is not None
        lease_path = workspace.lease_path
        original = lease_path.read_bytes()
        lease_path.unlink()
        lease_path.mkdir()
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceIntegrityError,
                match="unexpected or unsafe SQLite preparation workspace entry",
            ):
                workspace._measure_spill()
        finally:
            lease_path.rmdir()
            lease_path.write_bytes(original)
            if os.name == "posix":
                lease_path.chmod(0o600)
            workspace._lease_identity = preparation._PathIdentity.from_stat(
                os.stat(lease_path, follow_symlinks=False)
            )


def test_spill_measurement_requires_the_tracked_lease_path(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        lease_path = workspace.lease_path
        assert lease_path is not None
        workspace.lease_path = None
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceIntegrityError,
                match="owner lease is missing",
            ):
                workspace._measure_spill()
        finally:
            workspace.lease_path = lease_path


def test_spill_measurement_enforces_the_total_physical_ceiling(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        original_max_spill_bytes = workspace.config.max_spill_bytes
        object.__setattr__(
            workspace.config,
            "max_spill_bytes",
            workspace.spill_peak_bytes - 1,
        )
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationSpillExhaustedError,
                match="spill byte budget exhausted",
            ):
                workspace._measure_spill()
        finally:
            object.__setattr__(
                workspace.config,
                "max_spill_bytes",
                original_max_spill_bytes,
            )


@pytest.mark.parametrize("tampering", ["invalid-json", "wrong-workspace-id", "wrong-pid"])
def test_cleanup_refuses_unreadable_or_mismatched_owner_lease(
    tmp_path: Path,
    tampering: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.lease_path is not None
    original = workspace.lease_path.read_text(encoding="utf-8")
    if tampering == "invalid-json":
        workspace.lease_path.write_text("{", encoding="utf-8")
        message = "owner lease is invalid"
    elif tampering == "wrong-workspace-id":
        value = json.loads(original)
        value["workspace_id"] = "00000000-0000-0000-0000-000000000000"
        workspace.lease_path.write_text(json.dumps(value), encoding="utf-8")
        message = "lease UUID does not match"
    else:
        value = json.loads(original)
        value["pid"] = os.getpid() + 1
        workspace.lease_path.write_text(json.dumps(value), encoding="utf-8")
        message = "lease PID does not match"

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match=message,
    ):
        workspace.__exit__(None, None, None)

    assert workspace.cleanup_outcome == "refused"
    assert workspace.path_exists
    workspace.lease_path.write_text(original, encoding="utf-8")
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cleanup_refuses_owner_lease_outside_the_control_reserve(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(
        preparation.SQLitePreparationConfig(control_reserve_bytes=4 * 1024),
        parent_directory=tmp_path,
    )
    workspace.__enter__()
    assert workspace.lease_path is not None
    original = workspace.lease_path.read_bytes()
    workspace.lease_path.write_bytes(b"x" * (4 * 1024 + 1))

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="owner lease exceeds the control reserve",
    ):
        workspace.__exit__(None, None, None)

    assert workspace.cleanup_outcome == "refused"
    assert workspace.path_exists
    workspace.lease_path.write_bytes(original)
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cleanup_detects_workspace_reappearance_after_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    original_rmdir = Path.rmdir

    def remove_then_reappear(path: Path) -> None:
        original_rmdir(path)
        if path == workspace._cleanup_quarantine:
            directory.mkdir(mode=0o700)

    monkeypatch.setattr(Path, "rmdir", remove_then_reappear)
    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="reappeared during cleanup",
    ):
        workspace.__exit__(None, None, None)

    assert workspace.cleanup_outcome == "refused"
    assert directory.is_dir()
    directory.rmdir()
    preparation._discard_live_workspace(workspace)


def test_unleased_acquisition_detects_directory_reappearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    original_rmdir = Path.rmdir

    def remove_then_reappear(path: Path) -> None:
        original_rmdir(path)
        if path == workspace._cleanup_quarantine:
            directory.mkdir(mode=0o700)

    monkeypatch.setattr(Path, "rmdir", remove_then_reappear)
    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="workspace reappeared",
    ):
        workspace._remove_empty_acquisition_directory()

    assert workspace.cleanup_outcome == "refused"
    assert directory.is_dir()
    directory.rmdir()


def test_unleased_cleanup_classifies_owned_path_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, directory = _unleased_workspace(tmp_path)
    original_stat = preparation.os.stat
    secret_path = "customer-secret/unleased-owned-path"

    def fail_directory_stat(path: object, *args: Any, **kwargs: Any):
        if Path(path) == directory:
            raise OSError(secret_path)
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.os, "stat", fail_directory_stat)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="ownership inspection failed",
        ) as captured:
            workspace._remove_empty_acquisition_directory()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("phase", "message"),
    [
        ("inspection", "unleased-directory inspection failed"),
        ("removal", "unleased-directory removal failed"),
    ],
)
def test_unleased_cleanup_filesystem_failures_are_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    message: str,
) -> None:
    workspace, directory = _unleased_workspace(tmp_path)
    secret_path = f"customer-secret/unleased-{phase}"
    original_iterdir = Path.iterdir
    original_rmdir = Path.rmdir

    def fail_iterdir(path: Path):
        if path == directory:
            raise OSError(secret_path)
        return original_iterdir(path)

    def fail_rmdir(path: Path) -> None:
        if path == directory or path == workspace._cleanup_quarantine:
            raise OSError(secret_path)
        original_rmdir(path)

    with monkeypatch.context() as scoped:
        if phase == "inspection":
            scoped.setattr(Path, "iterdir", fail_iterdir)
        else:
            scoped.setattr(Path, "rmdir", fail_rmdir)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match=message,
        ) as captured:
            workspace._remove_empty_acquisition_directory()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize("failure_type", [OSError, RuntimeError])
def test_owned_path_canonical_inspection_error_is_pathless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    secret_path = "customer-secret/canonical-workspace"
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    original_resolve = Path.resolve

    def fail_directory_resolve(path: Path, *args: Any, **kwargs: Any):
        if path == directory:
            raise failure_type(secret_path)
        return original_resolve(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "resolve", fail_directory_resolve)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="canonical-path inspection failed",
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize("scenario", ["identity", "unsafe-parent"])
def test_owned_path_rejects_identity_or_parent_safety_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        if scenario == "identity":
            original_identity = workspace._owned_directory_identity
            assert original_identity is not None
            workspace._owned_directory_identity = replace(
                original_identity,
                inode=original_identity.inode + 1,
            )
            try:
                with pytest.raises(
                    preparation.WorkflowProgressPreparationCleanupRefusedError,
                    match="ownership identity changed",
                ):
                    workspace._validated_owned_path()
            finally:
                workspace._owned_directory_identity = original_identity
        else:

            def reject_parent(_parent_stat: os.stat_result) -> None:
                raise preparation.WorkflowProgressPreparationWorkspaceAcquisitionError(
                    "injected unsafe parent"
                )

            with monkeypatch.context() as scoped:
                scoped.setattr(
                    workspace,
                    "_validate_acquisition_parent_stat",
                    reject_parent,
                )
                with pytest.raises(
                    preparation.WorkflowProgressPreparationCleanupRefusedError,
                    match="parent became unsafe",
                ):
                    workspace._validated_owned_path()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode contract")
def test_owned_path_and_lease_reject_posix_authority_drift(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        directory = workspace.directory
        lease_path = workspace.lease_path
        assert directory is not None
        assert lease_path is not None

        original_uid = workspace._creator_uid
        workspace._creator_uid = None
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="owner identity is incomplete",
            ):
                workspace._validated_owned_path()
        finally:
            workspace._creator_uid = original_uid

        directory.chmod(0o755)
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="workspace permissions became unsafe",
            ):
                workspace._validated_owned_path()
        finally:
            directory.chmod(0o700)

        lease_path.chmod(0o644)
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="owner lease permissions are unsafe",
            ):
                workspace._validate_owned_directory(acquisition_failure=False)
        finally:
            lease_path.chmod(0o600)

        assert workspace.connection is not None
        workspace.connection.close()
        workspace.connection = None
        quarantine = workspace._quarantine_owned_directory()
        quarantine.chmod(0o755)
        try:
            with pytest.raises(
                preparation.WorkflowProgressPreparationCleanupRefusedError,
                match="quarantine permissions became unsafe",
            ):
                workspace._validated_cleanup_quarantine()
        finally:
            quarantine.chmod(0o700)


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("incomplete", "ownership identity is incomplete"),
        ("missing", "ownership path is missing"),
        ("redirected", "path was replaced or redirected"),
        ("wrong-parent", "path was replaced or redirected"),
        ("wrong-prefix", "name has no owned prefix"),
        ("noncanonical-uuid", "name has no canonical UUID"),
        ("invalid-uuid", "name has no canonical UUID"),
    ],
)
def test_owned_path_validation_rejects_unprovable_boundaries(
    tmp_path: Path,
    scenario: str,
    message: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace._creator_pid = os.getpid()
    workspace._creator_uid = os.geteuid() if os.name == "posix" else None
    workspace._owner_thread_id = threading.get_ident()
    cleanup_directory: Path | None = None
    cleanup_parent: Path | None = None
    if scenario == "missing":
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = tmp_path / "missing"
    elif scenario == "redirected":
        nested = tmp_path / "nested"
        nested.mkdir()
        workspace._owned_parent = nested
        workspace._owned_directory = nested / ".."
        cleanup_parent = nested
    elif scenario == "wrong-parent":
        cleanup_directory = tmp_path / (
            preparation._WORKSPACE_PREFIX + "00000000-0000-0000-0000-000000000141"
        )
        cleanup_directory.mkdir(mode=0o700)
        cleanup_parent = tmp_path / "other-parent"
        cleanup_parent.mkdir()
        workspace._owned_parent = cleanup_parent.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)
    elif scenario == "wrong-prefix":
        cleanup_directory = tmp_path / "not-owned"
        cleanup_directory.mkdir(mode=0o700)
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)
    elif scenario == "noncanonical-uuid":
        cleanup_directory = tmp_path / (
            preparation._WORKSPACE_PREFIX + "00000000-0000-0000-0000-0000000000AA"
        )
        cleanup_directory.mkdir(mode=0o700)
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)
    elif scenario == "invalid-uuid":
        cleanup_directory = tmp_path / (preparation._WORKSPACE_PREFIX + "not-a-uuid")
        cleanup_directory.mkdir(mode=0o700)
        workspace._owned_parent = tmp_path.resolve(strict=True)
        workspace._owned_directory = cleanup_directory.resolve(strict=True)

    if scenario != "incomplete":
        assert workspace._owned_parent is not None
        workspace._owned_parent_identity = preparation._PathIdentity.from_stat(
            workspace._owned_parent.stat()
        )
        identity_source = (
            workspace._owned_directory
            if workspace._owned_directory is not None and workspace._owned_directory.exists()
            else workspace._owned_parent
        )
        workspace._owned_directory_identity = preparation._PathIdentity.from_stat(
            identity_source.stat()
        )

    try:
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match=message,
        ):
            workspace._validated_owned_path()
    finally:
        if cleanup_directory is not None:
            cleanup_directory.rmdir()
        if cleanup_parent is not None:
            cleanup_parent.rmdir()


def test_graceful_exit_cleanup_continues_after_one_workspace_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    original_cleanup = workspace._close_and_remove

    def refuse_cleanup() -> None:
        raise preparation.WorkflowProgressPreparationCleanupRefusedError("injected refusal")

    monkeypatch.setattr(workspace, "_close_and_remove", refuse_cleanup)
    preparation._cleanup_live_workspaces_at_exit()

    assert workspace.path_exists
    assert workspace in preparation._LIVE_WORKSPACES
    monkeypatch.setattr(workspace, "_close_and_remove", original_cleanup)
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cross_thread_graceful_cleanup_serializes_with_owner_operation(
    tmp_path: Path,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    ready = threading.Event()
    release = threading.Event()
    owner_errors: list[BaseException] = []

    def own_workspace() -> None:
        try:
            workspace.__enter__()
            with workspace._operation_lock:
                ready.set()
                if not release.wait(timeout=10):
                    raise AssertionError("cleanup test did not release the owner thread")
        except BaseException as error:
            owner_errors.append(error)

    owner = threading.Thread(target=own_workspace, name="preparation-owner")
    owner.start()
    assert ready.wait(timeout=10)
    connection = workspace.connection
    assert connection is not None

    preparation._cleanup_live_workspaces_at_exit()

    assert workspace.cleanup_outcome == "busy"
    assert workspace.connection is connection
    assert workspace.path_exists

    release.set()
    owner.join(timeout=10)
    assert not owner.is_alive()
    assert owner_errors == []

    preparation._cleanup_live_workspaces_at_exit()

    assert workspace.connection is None
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
    assert workspace not in preparation._LIVE_WORKSPACES


def test_workspace_operations_reject_a_non_owner_thread(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    errors: list[BaseException] = []

    with workspace:

        def inspect_pragmas() -> None:
            try:
                workspace.sqlite_pragmas()
            except BaseException as error:
                errors.append(error)

        visitor = threading.Thread(target=inspect_pragmas, name="preparation-visitor")
        visitor.start()
        visitor.join(timeout=10)

        assert not visitor.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "require the owner thread" in str(errors[0])


def test_connection_close_failure_retains_reference_and_pathless_diagnostic(
    tmp_path: Path,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace.connection is not None
    workspace.connection.close()

    class CloseFails:
        def close(self) -> None:
            raise OSError("secret-workspace-name.sqlite3")

    failed_connection = CloseFails()
    workspace.connection = failed_connection  # type: ignore[assignment]

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupOperationalError,
        match="connection close failed",
    ) as captured:
        workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.connection is failed_connection
    assert "secret-workspace-name" not in str(captured.value)
    assert "secret-workspace-name" not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    workspace.connection = None
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_acquisition_filesystem_error_is_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "customer-secret/owner.lease"
    original_open = Path.open

    def fail_lease(path: Path, *args: Any, **kwargs: Any):
        if path.name == preparation._LEASE_NAME:
            raise OSError(secret_path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_lease)
    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="owner lease initialization failed",
    ) as captured:
        preparation.SQLitePreparationWorkspace(parent_directory=tmp_path).__enter__()

    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "failure_type",
    [OSError, RuntimeError, KeyboardInterrupt, GeneratorExit],
)
def test_partial_lease_write_is_removed_for_every_exception_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    secret_path = "customer-secret/partial-owner.lease"
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    def fail_after_partial_write(_value: object, stream: Any, **_kwargs: Any) -> None:
        stream.write("{")
        stream.flush()
        raise failure_type(secret_path)

    monkeypatch.setattr(preparation.json, "dump", fail_after_partial_write)
    with pytest.raises(
        preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
        match="owner lease initialization failed",
    ) as captured:
        workspace.__enter__()

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
    assert workspace._lease_identity is None
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "cleanup_failure_type",
    [OSError, RuntimeError, KeyboardInterrupt, GeneratorExit],
)
def test_partial_lease_cleanup_failure_is_bounded_pathless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure_type: type[BaseException],
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    secret_path = "customer-secret/partial-cleanup"
    original_unlink = Path.unlink

    def fail_after_partial_write(_value: object, stream: Any, **_kwargs: Any) -> None:
        stream.write("{")
        stream.flush()
        raise RuntimeError(secret_path)

    def fail_partial_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name == preparation._LEASE_NAME:
            raise cleanup_failure_type(secret_path)
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.json, "dump", fail_after_partial_write)
        scoped.setattr(Path, "unlink", fail_partial_unlink)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="partial owner lease removal failed",
        ) as captured:
            workspace.__enter__()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._remove_partial_owner_lease()
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_partial_lease_cleanup_without_recorded_authority_is_a_noop(tmp_path: Path) -> None:
    workspace, _directory = _unleased_workspace(tmp_path)

    workspace._remove_partial_owner_lease()

    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize("invalid_stage", ["open-handle", "final-path"])
def test_owner_lease_initialization_rejects_nonregular_identity_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_stage: str,
) -> None:
    workspace, directory = _unleased_workspace(tmp_path)
    lease = directory / preparation._LEASE_NAME
    workspace.lease_path = lease
    original_fstat = preparation.os.fstat
    original_stat = preparation.os.stat
    lease_stat_calls = 0

    def as_directory(value: os.stat_result) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=(preparation.stat.S_IFDIR | preparation.stat.S_IMODE(value.st_mode)),
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_ctime_ns=value.st_ctime_ns,
        )

    def invalid_handle(fd: int) -> os.stat_result | SimpleNamespace:
        value = original_fstat(fd)
        return as_directory(value) if invalid_stage == "open-handle" else value

    def invalid_final_path(path: object, *args: Any, **kwargs: Any):
        nonlocal lease_stat_calls
        value = original_stat(path, *args, **kwargs)
        if invalid_stage == "final-path" and Path(path) == lease:
            lease_stat_calls += 1
            if lease_stat_calls == 1:
                return as_directory(value)
        return value

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.os, "fstat", invalid_handle)
        scoped.setattr(preparation.os, "stat", invalid_final_path)
        with pytest.raises(
            preparation.WorkflowProgressPreparationWorkspaceAcquisitionError,
            match="owner lease initialization failed",
        ):
            workspace._initialize_owner_lease(UUID("00000000-0000-0000-0000-000000000141"))

    assert not lease.exists()
    assert workspace._lease_identity is None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("fault", "error_type", "message"),
    [
        (
            "missing",
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            "partial owner lease is missing or redirected",
        ),
        (
            "inspection",
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            "partial owner lease inspection failed",
        ),
        (
            "identity",
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            "partial owner lease identity changed",
        ),
        (
            "removal",
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            "partial owner lease removal failed",
        ),
        (
            "reappearance",
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            "partial owner lease reappeared",
        ),
    ],
)
def test_partial_lease_cleanup_fails_closed_and_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    error_type: type[Exception],
    message: str,
) -> None:
    workspace, _directory, lease = _partial_lease_workspace(tmp_path)
    original_identity = workspace._lease_identity
    original_stat = preparation.os.stat
    original_unlink = Path.unlink
    secret_path = f"customer-secret/partial-lease-{fault}"

    if fault == "missing":
        lease.unlink()
    elif fault == "identity":
        assert original_identity is not None
        workspace._lease_identity = replace(
            original_identity,
            inode=original_identity.inode + 1,
        )

    def fail_inspection(path: object, *args: Any, **kwargs: Any):
        if fault == "inspection" and Path(path) == lease:
            raise OSError(secret_path)
        return original_stat(path, *args, **kwargs)

    def fail_or_reappear(path: Path, *args: Any, **kwargs: Any) -> None:
        if path != lease:
            original_unlink(path, *args, **kwargs)
            return
        if fault == "removal":
            raise OSError(secret_path)
        original_unlink(path, *args, **kwargs)
        if fault == "reappearance":
            path.write_text("replacement", encoding="utf-8")

    with monkeypatch.context() as scoped:
        if fault == "inspection":
            scoped.setattr(preparation.os, "stat", fail_inspection)
        elif fault in {"removal", "reappearance"}:
            scoped.setattr(Path, "unlink", fail_or_reappear)
        with pytest.raises(error_type, match=message) as captured:
            workspace._remove_partial_owner_lease()

    assert workspace.cleanup_outcome in {"refused", "operational_failure"}
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    if lease.exists():
        workspace._lease_identity = preparation._PathIdentity.from_stat(
            os.stat(lease, follow_symlinks=False)
        )
        workspace._remove_partial_owner_lease()
    else:
        workspace._lease_identity = None
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


def test_post_connect_failure_closes_registered_handle_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = preparation.sqlite3.connect
    created: list[Any] = []

    class RowFactoryFails:
        def __init__(self, path: Path) -> None:
            self._connection = original_connect(path)
            self.closed = False

        @property
        def row_factory(self) -> None:
            return None

        @row_factory.setter
        def row_factory(self, _value: object) -> None:
            raise RuntimeError("injected post-connect failure")

        def close(self) -> None:
            self._connection.close()
            self.closed = True

    def connect(path: Path, *_args: Any, **_kwargs: Any) -> RowFactoryFails:
        connection = RowFactoryFails(path)
        created.append(connection)
        return connection

    monkeypatch.setattr(preparation.sqlite3, "connect", connect)
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(RuntimeError, match="injected post-connect failure"):
        workspace.__enter__()

    assert len(created) == 1
    assert created[0].closed
    assert workspace.connection is None
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
    assert not list(tmp_path.iterdir())


def test_live_workspace_registration_failure_is_inside_acquisition_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    def register_then_fail(value: preparation.SQLitePreparationWorkspace) -> None:
        with preparation._LIVE_WORKSPACES_LOCK:
            preparation._LIVE_WORKSPACES.add(value)
        raise RuntimeError("injected live-workspace registration failure")

    monkeypatch.setattr(preparation, "_register_live_workspace", register_then_fail)
    with pytest.raises(RuntimeError, match="injected live-workspace registration failure"):
        workspace.__enter__()

    assert workspace.connection is None
    assert workspace.cleanup_outcome == "removed"
    assert workspace not in preparation._LIVE_WORKSPACES
    assert not workspace.path_exists
    assert not list(tmp_path.iterdir())


def test_accounting_filesystem_error_is_pathless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    secret_path = "customer-secret/workspace.sqlite3"

    with workspace:
        database_path = workspace.database_path
        assert database_path is not None
        original_stat = preparation.os.stat

        def fail_database_stat(path: object, *args: Any, **kwargs: Any):
            if Path(path) == database_path:
                raise OSError(secret_path)
            return original_stat(path, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(preparation.os, "stat", fail_database_stat)
            with pytest.raises(
                preparation.WorkflowProgressPreparationWorkspaceIntegrityError,
                match="workspace accounting failed",
            ) as captured:
                workspace._measure_spill()

        assert secret_path not in str(captured.value)
        assert secret_path not in repr(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


def test_cleanup_filesystem_error_is_pathless_and_operationally_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    database_path = workspace.database_path
    assert database_path is not None
    secret_path = "customer-secret/workspace.sqlite3"
    original_unlink = Path.unlink

    def fail_database_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name == database_path.name:
            raise OSError(secret_path)
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", fail_database_unlink)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="owned-file removal failed",
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert not workspace.path_exists


def test_cleanup_quarantine_rejects_replacement_after_initial_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    moved = tmp_path / "moved-genuine-workspace"
    original_validate = workspace._validate_owned_directory

    def validate_then_replace(*, acquisition_failure: bool) -> None:
        original_validate(acquisition_failure=acquisition_failure)
        directory.rename(moved)
        directory.mkdir(mode=0o700)

    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_validate_owned_directory", validate_then_replace)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="ownership identity changed",
        ):
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    assert workspace._cleanup_quarantine is None
    assert directory.is_dir()
    assert not list(directory.iterdir())
    assert {path.name for path in moved.iterdir()} == {
        preparation._DATABASE_NAME,
        preparation._LEASE_NAME,
    }
    directory.rmdir()
    moved.rename(directory)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize("replacement", ["source-reappeared", "quarantine-replaced"])
def test_cleanup_quarantine_rejects_post_rename_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    moved = tmp_path / "moved-quarantined-workspace"
    original_quarantine = workspace._quarantine_owned_directory

    def quarantine_then_replace() -> Path:
        quarantine = original_quarantine()
        if replacement == "source-reappeared":
            directory.mkdir(mode=0o700)
        else:
            quarantine.rename(moved)
            quarantine.mkdir(mode=0o700)
        return quarantine

    message = (
        "source reappeared" if replacement == "source-reappeared" else "quarantine identity changed"
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_quarantine_owned_directory", quarantine_then_replace)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match=message,
        ):
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    quarantine = workspace._cleanup_quarantine
    assert quarantine is not None
    assert workspace.path_exists
    if replacement == "source-reappeared":
        directory.rmdir()
    else:
        quarantine.rmdir()
        moved.rename(quarantine)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_cleanup_quarantine_rename_error_is_pathless_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    secret_path = "customer-secret/quarantine-target"
    original_rename = Path.rename

    def fail_quarantine_rename(path: Path, target: Path) -> Path:
        if path == directory:
            raise OSError(secret_path)
        return original_rename(path, target)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rename", fail_quarantine_rename)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="quarantine rename failed",
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert workspace.path_exists
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


@pytest.mark.parametrize("fault", ["inspection", "unexpected-entry"])
def test_unleased_cleanup_revalidates_after_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    workspace, _directory = _unleased_workspace(tmp_path)
    original_iterdir = Path.iterdir
    original_quarantine = workspace._quarantine_owned_directory
    secret_path = f"customer-secret/unleased-quarantine-{fault}"

    def fail_quarantine_inspection(path: Path):
        if fault == "inspection" and path == workspace._cleanup_quarantine:
            raise OSError(secret_path)
        return original_iterdir(path)

    def quarantine_then_add_entry() -> Path:
        quarantine = original_quarantine()
        if fault == "unexpected-entry":
            (quarantine / "replacement.txt").write_text("unsafe", encoding="utf-8")
        return quarantine

    with monkeypatch.context() as scoped:
        if fault == "inspection":
            scoped.setattr(Path, "iterdir", fail_quarantine_inspection)
        else:
            scoped.setattr(workspace, "_quarantine_owned_directory", quarantine_then_add_entry)
        error_type = (
            preparation.WorkflowProgressPreparationCleanupOperationalError
            if fault == "inspection"
            else preparation.WorkflowProgressPreparationCleanupRefusedError
        )
        message = (
            "unleased-directory inspection failed"
            if fault == "inspection"
            else "quarantined unleased directory is not empty"
        )
        with pytest.raises(error_type, match=message) as captured:
            workspace._remove_empty_acquisition_directory()

    assert secret_path not in str(captured.value)
    assert workspace.path_exists
    quarantine = workspace._cleanup_quarantine
    assert quarantine is not None
    replacement = quarantine / "replacement.txt"
    if replacement.exists():
        replacement.unlink()
    workspace._remove_empty_acquisition_directory()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_collision_refuses_without_renaming_owned_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    assert directory is not None
    collision_id = "00000000-0000-0000-0000-000000000141"
    collision = tmp_path / f"{preparation._QUARANTINE_PREFIX}{collision_id}"
    collision.mkdir(mode=0o700)
    monkeypatch.setattr(preparation, "uuid4", lambda: collision_id)

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="quarantine already exists",
    ):
        workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    assert directory.is_dir()
    assert workspace._cleanup_quarantine is None
    collision.rmdir()
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing", "quarantine is missing"),
        ("inspection", "quarantine inspection failed"),
        ("wrong-type", "quarantine was replaced or redirected"),
        ("invalid-name", "quarantine has no canonical UUID"),
        ("unsafe-parent", "cleanup parent became unsafe"),
        ("wrong-path", "quarantine path changed"),
    ],
)
def test_cleanup_quarantine_validation_faults_are_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    message: str,
) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    assert workspace._quarantine_owned_directory() == quarantine
    moved = tmp_path / "moved-valid-quarantine"
    invalid = tmp_path / f"{preparation._QUARANTINE_PREFIX}not-a-uuid"
    original_stat = preparation.os.stat

    if fault == "missing":
        quarantine.rename(moved)
    elif fault == "wrong-type":
        quarantine.rename(moved)
        quarantine.write_text("replacement", encoding="utf-8")
    elif fault == "invalid-name":
        quarantine.rename(invalid)
        workspace._cleanup_quarantine = invalid

    def fail_inspection(path: object, *args: Any, **kwargs: Any):
        if fault == "inspection" and Path(path) == quarantine:
            raise OSError("customer-secret/quarantine-inspection")
        return original_stat(path, *args, **kwargs)

    def reject_parent(_parent_stat: os.stat_result) -> None:
        raise preparation.WorkflowProgressPreparationWorkspaceAcquisitionError(
            "injected unsafe cleanup parent"
        )

    with monkeypatch.context() as scoped:
        if fault == "inspection":
            scoped.setattr(preparation.os, "stat", fail_inspection)
        elif fault == "unsafe-parent":
            scoped.setattr(workspace, "_validate_acquisition_parent_stat", reject_parent)
        with pytest.raises(
            (
                preparation.WorkflowProgressPreparationCleanupOperationalError
                if fault == "inspection"
                else preparation.WorkflowProgressPreparationCleanupRefusedError
            ),
            match=message,
        ):
            if fault == "wrong-path":
                workspace._validate_quarantined_directory(
                    tmp_path / "different-quarantine",
                    acquisition_failure=True,
                    require_lease=False,
                )
            else:
                workspace._validated_cleanup_quarantine()

    if fault == "missing":
        moved.rename(quarantine)
    elif fault == "wrong-type":
        quarantine.unlink()
        moved.rename(quarantine)
    elif fault == "invalid-name":
        invalid.rename(quarantine)
        workspace._cleanup_quarantine = quarantine
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_retry_accepts_validated_quarantine_after_file_removal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    original_rmdir = Path.rmdir
    failed_once = False

    def fail_quarantine_rmdir_once(path: Path) -> None:
        nonlocal failed_once
        if path == workspace._cleanup_quarantine and not failed_once:
            failed_once = True
            raise OSError("customer-secret/quarantine-rmdir")
        original_rmdir(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rmdir", fail_quarantine_rmdir_once)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="owned-file removal failed",
        ):
            workspace._close_and_remove()

    quarantine = workspace._cleanup_quarantine
    assert quarantine is not None
    assert not list(quarantine.iterdir())
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_retry_refuses_entry_created_after_owned_files_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    original_unlink = Path.unlink
    replacement: Path | None = None

    def add_entry_after_lease_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal replacement
        original_unlink(path, *args, **kwargs)
        if path.name == preparation._LEASE_NAME:
            replacement = path.parent / "replacement.txt"
            replacement.write_text("unsafe", encoding="utf-8")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", add_entry_after_lease_unlink)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="quarantine contains an unowned entry",
        ):
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    assert replacement is not None
    replacement.unlink()
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_operational_validation_error_is_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    original_validate = workspace._validated_cleanup_quarantine
    calls = 0

    def fail_during_removal() -> Path:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise preparation.WorkflowProgressPreparationCleanupOperationalError(
                "injected quarantine validation failure"
            )
        return original_validate()

    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_validated_cleanup_quarantine", fail_during_removal)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match="injected quarantine validation failure",
        ):
            workspace._remove_quarantined_directory(quarantine, leased=True)

    assert workspace.cleanup_outcome == "operational_failure"
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize("replacement", ["database-type", "lease-identity"])
def test_cleanup_revalidates_each_entry_immediately_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    original_validate = workspace._validated_cleanup_quarantine
    moved = tmp_path / f"original-{replacement}"
    calls = 0

    def replace_before_entry_unlink() -> Path:
        nonlocal calls
        calls += 1
        if replacement == "database-type" and calls == 4:
            database = quarantine / preparation._DATABASE_NAME
            database.rename(moved)
            database.mkdir()
        elif replacement == "lease-identity" and calls == 5:
            lease = quarantine / preparation._LEASE_NAME
            lease.rename(moved)
            lease.write_bytes(moved.read_bytes())
        return original_validate()

    message = (
        "cleanup entry identity changed"
        if replacement == "database-type"
        else "owner lease identity changed"
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            workspace,
            "_validated_cleanup_quarantine",
            replace_before_entry_unlink,
        )
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match=message,
        ):
            workspace._remove_quarantined_directory(quarantine, leased=True)

    assert workspace.cleanup_outcome == "refused"
    replaced_path = quarantine / (
        preparation._DATABASE_NAME if replacement == "database-type" else preparation._LEASE_NAME
    )
    if replacement == "database-type":
        replaced_path.rmdir()
    else:
        replaced_path.unlink()
    moved.rename(replaced_path)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_refuses_replaced_owner_lease_identity_after_rename(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    lease = workspace.lease_path
    assert lease is not None
    original_lease = tmp_path / "original-owner-lease"
    lease.rename(original_lease)
    lease.write_bytes(original_lease.read_bytes())
    if os.name == "posix":
        lease.chmod(0o600)

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="owner lease identity changed",
    ):
        workspace._close_and_remove()

    assert workspace.cleanup_outcome == "refused"
    lease.unlink()
    original_lease.rename(lease)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_rechecks_source_after_identity_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, directory, _quarantine = _quarantined_workspace(tmp_path)
    original_lexists = preparation.os.path.lexists
    source_checks = 0

    def reappear_on_second_check(path: object) -> bool:
        nonlocal source_checks
        if Path(path) == directory:
            source_checks += 1
            return source_checks == 2
        return original_lexists(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(preparation.os.path, "lexists", reappear_on_second_check)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="source reappeared",
        ):
            workspace._validated_cleanup_quarantine()

    assert source_checks == 2
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


def test_cleanup_quarantine_internal_authority_invariants_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unleased_parent = tmp_path / "unleased"
    unleased_parent.mkdir()
    unleased, directory = _unleased_workspace(unleased_parent)
    parent = unleased._owned_parent
    assert parent is not None
    unleased._owned_parent = None
    with monkeypatch.context() as scoped:
        scoped.setattr(unleased, "_validated_owned_path", lambda: directory)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="cleanup parent is unavailable",
        ):
            unleased._quarantine_owned_directory()
    unleased._owned_parent = parent
    unleased._remove_empty_acquisition_directory()

    incomplete = preparation.SQLitePreparationWorkspace()
    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupRefusedError,
        match="quarantine identity is incomplete",
    ):
        incomplete._validated_cleanup_quarantine()

    leased_parent = tmp_path / "leased"
    leased_parent.mkdir()
    workspace, directory, quarantine = _quarantined_workspace(leased_parent)
    workspace._owned_directory = None
    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_validated_cleanup_quarantine", lambda: quarantine)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupRefusedError,
            match="workspace identity is unavailable",
        ):
            workspace._validate_quarantined_directory(
                quarantine,
                acquisition_failure=True,
                require_lease=False,
            )
    workspace._owned_directory = directory
    workspace._close_and_remove()


def test_cleanup_quarantine_requires_lease_before_initial_removal(tmp_path: Path) -> None:
    workspace, _directory, quarantine = _quarantined_workspace(tmp_path)
    lease = quarantine / preparation._LEASE_NAME
    moved = tmp_path / "temporarily-missing-owner-lease"
    lease.rename(moved)

    with pytest.raises(
        preparation.WorkflowProgressPreparationCleanupOperationalError,
        match="owner lease read failed",
    ):
        workspace._validate_quarantined_directory(
            quarantine,
            acquisition_failure=True,
            require_lease=True,
        )

    moved.rename(lease)
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"


@pytest.mark.parametrize(
    ("phase", "message"),
    [
        ("inventory", "cleanup inventory failed"),
        ("entry", "cleanup entry inspection failed"),
        ("lease-stat", "owner lease read failed"),
        ("lease-read", "owner lease read failed"),
    ],
)
def test_cleanup_inspection_errors_are_pathless_and_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    message: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    directory = workspace.directory
    database_path = workspace.database_path
    lease_path = workspace.lease_path
    assert directory is not None
    assert database_path is not None
    assert lease_path is not None
    secret_path = f"customer-secret/cleanup-{phase}"
    original_iterdir = Path.iterdir
    original_read_text = Path.read_text
    original_stat = preparation.os.stat
    lease_stat_calls = 0

    def fail_inventory(path: Path):
        if path == directory:
            raise OSError(secret_path)
        return original_iterdir(path)

    def fail_entry_or_lease_stat(path: object, *args: Any, **kwargs: Any):
        nonlocal lease_stat_calls
        candidate = Path(path)
        if phase == "entry" and candidate == database_path:
            raise OSError(secret_path)
        if phase == "lease-stat" and candidate == lease_path:
            lease_stat_calls += 1
            if lease_stat_calls == 2:
                raise OSError(secret_path)
        return original_stat(path, *args, **kwargs)

    def fail_lease_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == lease_path:
            raise OSError(secret_path)
        return original_read_text(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        if phase == "inventory":
            scoped.setattr(Path, "iterdir", fail_inventory)
        elif phase in {"entry", "lease-stat"}:
            scoped.setattr(preparation.os, "stat", fail_entry_or_lease_stat)
        else:
            scoped.setattr(Path, "read_text", fail_lease_read)
        with pytest.raises(
            preparation.WorkflowProgressPreparationCleanupOperationalError,
            match=message,
        ) as captured:
            workspace._close_and_remove()

    assert workspace.cleanup_outcome == "operational_failure"
    assert secret_path not in str(captured.value)
    assert secret_path not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    workspace._close_and_remove()
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_caller_iterator_oserror_is_preserved_verbatim(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    def failing_nodes() -> Iterator[dict[str, Any]]:
        raise OSError("caller-owned/input-file.json")
        yield workflow_node("unreachable")

    with pytest.raises(OSError, match="caller-owned/input-file.json") as captured:
        with workspace:
            workspace.prepare_topology(_identity(), 1, failing_nodes(), [])

    assert type(captured.value) is OSError
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_workspace_candidate_is_retained_bounded_until_compatibility_detachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_NODE_MAX_ITEMS", 7)
    monkeypatch.setattr(storage, "WORKFLOW_PROGRESS_TOPOLOGY_EDGE_MAX_ITEMS", 11)
    node_count = 500
    nodes = (workflow_node(workflow_node_id(index)) for index in range(node_count))
    edges = (
        _edge(workflow_node_id(index - 1), workflow_node_id(index))
        for index in range(1, node_count)
    )
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    with workspace:
        candidate = workspace.prepare_topology(_identity(), 1, nodes, edges)
        assert not hasattr(candidate, "observed_node_ids")
        assert candidate.observed_node_count == node_count
        assert len(candidate.node_ids) == candidate.retained_node_count == 7
        assert len(candidate.edges) == candidate.retained_edge_count == 6
        assert workspace._legacy_observed_node_ids is None
        workspace.prepare_legacy_detachment(candidate)
        assert workspace._legacy_observed_node_ids is not None
        assert len(workspace._legacy_observed_node_ids) == node_count

    detached = workspace.detach_legacy_topology(candidate)
    assert len(detached.observed_node_ids) == node_count
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_candidate_detaches_exactly_once_and_only_after_owned_cleanup(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        candidate = workspace.prepare_topology(
            _identity(),
            1,
            [workflow_node("node-a")],
            [],
        )
        with pytest.raises(RuntimeError, match="before owned workspace cleanup"):
            workspace.detach_legacy_topology(candidate)
        workspace.prepare_legacy_detachment(candidate)
        with pytest.raises(RuntimeError, match="before owned workspace cleanup"):
            workspace.detach_legacy_topology(candidate)

    detached = workspace.detach_legacy_topology(candidate)
    assert storage._prepared_topology_capability_matches(detached)
    assert storage._prepared_topology_observed_membership_capability_matches(detached)
    with pytest.raises(RuntimeError, match="before owned workspace cleanup"):
        workspace.detach_legacy_topology(candidate)


@pytest.mark.parametrize("tamper_phase", ["before-legacy", "after-cleanup"])
@pytest.mark.parametrize(
    "mutation",
    [
        "identity-cross-type",
        "topology-cross-type",
        "page-cross-type",
        "page-in-place",
        "page-payload-reference",
        "node-ids-reference",
    ],
)
def test_candidate_signature_rejects_tampering_before_capability_issue(
    tmp_path: Path,
    tamper_phase: str,
    mutation: str,
) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)

    def tamper(candidate: preparation.PreparedWorkflowProgressTopologyCandidate) -> None:
        page = candidate.pages[0]
        if mutation == "identity-cross-type":
            original = candidate.identity.attempt_number
            replacement: object = float(original)
            assert replacement == original
            assert type(replacement) is not type(original)
            object.__setattr__(candidate.identity, "attempt_number", replacement)
        elif mutation == "topology-cross-type":
            original = candidate.topology_version
            replacement = True
            assert replacement == original
            assert type(replacement) is not type(original)
            object.__setattr__(candidate, "topology_version", replacement)
        elif mutation == "page-cross-type":
            original = page.collection
            replacement = page.collection.value
            assert replacement == original
            assert type(replacement) is not type(original)
            object.__setattr__(page, "collection", replacement)
        elif mutation == "page-in-place":
            object.__setattr__(page, "digest", "0" * 64)
        elif mutation == "page-payload-reference":
            original = page.payload
            replacement = memoryview(original).tobytes()
            assert replacement == original
            assert replacement is not original
            object.__setattr__(page, "payload", replacement)
        elif mutation == "node-ids-reference":
            original = candidate.node_ids
            replacement = frozenset(list(original))
            assert replacement == original
            assert replacement is not original
            object.__setattr__(candidate, "node_ids", replacement)
        else:
            raise AssertionError(f"unsupported candidate mutation: {mutation}")

    if tamper_phase == "before-legacy":
        with pytest.raises(RuntimeError, match="sealed topology"):
            with workspace:
                candidate = workspace.prepare_topology(
                    _identity(),
                    1,
                    [workflow_node("node-a")],
                    [],
                )
                tamper(candidate)
                workspace.prepare_legacy_detachment(candidate)
    else:
        with workspace:
            candidate = workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                [],
            )
            workspace.prepare_legacy_detachment(candidate)
        tamper(candidate)
        with pytest.raises(RuntimeError, match="before owned workspace cleanup"):
            workspace.detach_legacy_topology(candidate)

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_foreign_candidate_poisons_and_cleans_its_workspace(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(RuntimeError, match="sealed topology"):
        with workspace:
            candidate = workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a")],
                [],
            )
            workspace.prepare_legacy_detachment(replace(candidate))

    assert workspace._phase == "poisoned"
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_public_adapter_issues_membership_trust_only_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(tmp_path))
    registrations: list[bool] = []
    original_register = storage._register_prepared_topology_capability

    def register(
        topology: storage.PreparedWorkflowProgressTopology,
        *,
        trust_observed_node_ids: bool = False,
    ) -> None:
        assert not list(tmp_path.iterdir())
        registrations.append(trust_observed_node_ids)
        original_register(
            topology,
            trust_observed_node_ids=trust_observed_node_ids,
        )

    monkeypatch.setattr(storage, "_register_prepared_topology_capability", register)
    topology = preparation.prepare_workflow_progress_topology(
        _identity(),
        1,
        [workflow_node("node-a")],
        [],
    )

    assert registrations == [True]
    assert storage._prepared_topology_observed_membership_capability_matches(topology)


def test_public_adapter_registration_failure_still_leaves_no_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(tmp_path))

    def fail_registration(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected capability registration failure")

    monkeypatch.setattr(
        storage,
        "_register_prepared_topology_capability",
        fail_registration,
    )
    with pytest.raises(RuntimeError, match="injected capability registration failure"):
        preparation.prepare_workflow_progress_topology(
            _identity(),
            1,
            [workflow_node("node-a")],
            [],
        )

    assert not list(tmp_path.iterdir())


def test_graceful_exit_hook_removes_registered_live_workspace(tmp_path: Path) -> None:
    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    workspace.__enter__()
    assert workspace in preparation._LIVE_WORKSPACES
    assert workspace.path_exists

    preparation._cleanup_live_workspaces_at_exit()

    assert workspace.connection is None
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists
    assert workspace not in preparation._LIVE_WORKSPACES


def test_legacy_membership_scan_checks_cancellation_between_batches(tmp_path: Path) -> None:
    detaching = False
    detachment_checks = 0

    def cancellation_check() -> None:
        nonlocal detachment_checks
        if not detaching:
            return
        detachment_checks += 1
        if detachment_checks == 3:
            raise asyncio.CancelledError("cancel observed membership scan")

    workspace = preparation.SQLitePreparationWorkspace(
        preparation.SQLitePreparationConfig(batch_max_items=1),
        parent_directory=tmp_path,
        cancellation_check=cancellation_check,
    )
    with pytest.raises(asyncio.CancelledError, match="cancel observed membership scan"):
        with workspace:
            candidate = workspace.prepare_topology(
                _identity(),
                1,
                [workflow_node("node-a"), workflow_node("node-b")],
                [],
            )
            detaching = True
            workspace.prepare_legacy_detachment(candidate)

    assert detachment_checks == 3
    assert workspace._phase == "poisoned"
    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_spill_exhaustion_cleans_before_public_adapter_can_issue_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_type = preparation.SQLitePreparationWorkspace
    created: list[preparation.SQLitePreparationWorkspace] = []

    def limited_workspace() -> preparation.SQLitePreparationWorkspace:
        workspace = workspace_type(
            preparation.SQLitePreparationConfig(max_node_items=2),
            parent_directory=tmp_path,
        )
        created.append(workspace)
        return workspace

    registrations = 0

    def register(*_args: Any, **_kwargs: Any) -> None:
        nonlocal registrations
        registrations += 1

    monkeypatch.setattr(preparation, "SQLitePreparationWorkspace", limited_workspace)
    monkeypatch.setattr(storage, "_register_prepared_topology_capability", register)
    with pytest.raises(
        preparation.WorkflowProgressPreparationSpillExhaustedError,
        match="node item budget exhausted at 2",
    ):
        preparation.prepare_workflow_progress_topology(
            _identity(),
            1,
            (workflow_node(workflow_node_id(index)) for index in range(3)),
            [],
        )

    assert registrations == 0
    assert len(created) == 1
    assert created[0].cleanup_outcome == "removed"
    assert not created[0].path_exists
    assert not list(tmp_path.iterdir())


def test_durable_revalidation_does_not_reissue_changed_observed_membership() -> None:
    oversized = workflow_node("node-oversized-body")
    oversized["runtime_env"] = {f"key-{index}": "value" * 8 for index in range(1_000)}
    topology = preparation.prepare_workflow_progress_topology(
        _identity(),
        1,
        [oversized, workflow_node("node-retained")],
        [],
    )
    assert topology.observed_node_ids == frozenset({"node-oversized-body", "node-retained"})
    assert storage._prepared_topology_observed_membership_capability_matches(topology)

    object.__setattr__(
        topology,
        "observed_node_ids",
        frozenset({"node-forged", "node-retained"}),
    )
    assert not storage._prepared_topology_observed_membership_capability_matches(topology)
    storage._validate_prepared_topology_reference(topology)

    assert storage._prepared_topology_capability_matches(topology)
    assert not storage._prepared_topology_observed_membership_capability_matches(topology)
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="package-issued observed topology membership",
    ):
        storage.prepare_workflow_progress_detail([], topology=topology)


def test_revalidated_copy_cannot_impersonate_observed_membership() -> None:
    topology = preparation.prepare_workflow_progress_topology(
        _identity(),
        1,
        [workflow_node("node-a")],
        [],
    )
    copied = replace(topology, _capability_token=None)
    storage._validate_prepared_topology_reference(copied)

    assert storage._prepared_topology_capability_matches(copied)
    assert not storage._prepared_topology_observed_membership_capability_matches(copied)
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="package-issued observed topology membership",
    ):
        storage.prepare_workflow_progress_detail([], topology=copied)


@pytest.mark.parametrize(
    "field",
    [
        "manifest_payload",
        "pages",
        "node_ids",
        "observed_node_ids",
        "node_kinds",
        "edges",
        "truncation_reasons",
        "map_node_ids",
        "page_payload",
        "page_scalar",
    ],
)
def test_capability_references_defeat_evidence_replacement_and_id_reuse(field: str) -> None:
    topology = _topology_with_all_capability_evidence()
    token = topology._capability_token
    assert token is not None
    capability = storage._PREPARED_TOPOLOGY_CAPABILITIES[token]

    if field == "page_payload":
        page = topology.pages[0]
        original = page.payload
        replacement = memoryview(original).tobytes()
        assert replacement == original
        assert replacement is not original
        assert any(retained is original for retained in capability.evidence_references)
        object.__setattr__(page, "payload", replacement)
    elif field == "page_scalar":
        page = topology.pages[0]
        object.__setattr__(page, "digest", "0" * 64)
    else:
        original = getattr(topology, field)
        if isinstance(original, bytes):
            replacement = memoryview(original).tobytes()
        elif isinstance(original, frozenset):
            replacement = frozenset(list(original))
        elif isinstance(original, tuple):
            replacement = (*original,)
        else:
            raise AssertionError(f"unsupported capability field: {field}")
        assert replacement == original
        assert replacement is not original
        assert any(retained is original for retained in capability.evidence_references)
        retained_id = id(original)
        object.__setattr__(topology, field, replacement)
        del original
        assert all(id(object()) != retained_id for _ in range(1_000))

    assert not storage._prepared_topology_capability_matches(topology)
    assert not storage._prepared_topology_observed_membership_capability_matches(topology)


@pytest.mark.parametrize(
    "substitution",
    [
        "topology-bool-for-int",
        "topology-float-for-int",
        "identity-float-for-int",
        "page-bool-for-int",
        "page-string-for-enum",
        "page-cross-enum",
        "page-float-for-int",
    ],
)
def test_capability_scalar_signatures_are_recursively_type_exact(substitution: str) -> None:
    topology = _topology_with_all_capability_evidence()
    page = topology.pages[0]

    if substitution == "topology-bool-for-int":
        original = topology.topology_version
        replacement: object = True
        object.__setattr__(topology, "topology_version", replacement)
    elif substitution == "topology-float-for-int":
        original = topology.observed_node_count
        replacement = float(original)
        object.__setattr__(topology, "observed_node_count", replacement)
    elif substitution == "identity-float-for-int":
        original = topology.identity.attempt_number
        replacement = float(original)
        object.__setattr__(topology.identity, "attempt_number", replacement)
    elif substitution == "page-bool-for-int":
        original = page.page_index
        replacement = False
        object.__setattr__(page, "page_index", replacement)
    elif substitution == "page-string-for-enum":
        original = page.collection
        replacement = page.collection.value
        object.__setattr__(page, "collection", replacement)
    elif substitution == "page-cross-enum":
        original = page.collection
        replacement = _ForeignTopologyCollection.NODE
        object.__setattr__(page, "collection", replacement)
    elif substitution == "page-float-for-int":
        original = page.encoded_bytes
        replacement = float(original)
        object.__setattr__(page, "encoded_bytes", replacement)
    else:
        raise AssertionError(f"unsupported substitution: {substitution}")

    assert replacement == original
    assert type(replacement) is not type(original)
    assert not storage._prepared_topology_capability_matches(topology)
    assert not storage._prepared_topology_observed_membership_capability_matches(topology)


def test_capability_strong_evidence_references_release_with_weak_topology() -> None:
    topology = _topology_with_all_capability_evidence()
    token = topology._capability_token
    assert token is not None
    observed = weakref.ref(topology)

    del topology
    gc.collect()

    assert observed() is None
    assert token not in storage._PREPARED_TOPOLOGY_CAPABILITIES


def test_process_guard_and_at_fork_reset_are_deterministic() -> None:
    workspace = preparation.SQLitePreparationWorkspace()
    workspace._creator_pid = os.getpid() + 1
    with pytest.raises(RuntimeError, match="belongs to a different process"):
        workspace._assert_creator_process()

    original_workspaces = preparation._LIVE_WORKSPACES
    original_lock = preparation._LIVE_WORKSPACES_LOCK
    workspace._phase = "topology"
    preparation._LIVE_WORKSPACES = {workspace}
    try:
        preparation._reset_live_workspaces_after_fork()

        assert workspace._phase == "poisoned"
        assert preparation._LIVE_WORKSPACES == set()
        assert preparation._LIVE_WORKSPACES_LOCK is not original_lock
    finally:
        preparation._LIVE_WORKSPACES = original_workspaces
        preparation._LIVE_WORKSPACES_LOCK = original_lock


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_child_cannot_cleanup_or_detach_parent_authority(tmp_path: Path) -> None:
    trusted_topology = _topology_with_all_capability_evidence()
    closed_workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with closed_workspace:
        candidate = closed_workspace.prepare_topology(
            _identity(),
            2,
            [workflow_node("node-closed")],
            [],
        )
        closed_workspace.prepare_legacy_detachment(candidate)
    live_workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    live_workspace.__enter__()
    assert live_workspace.path_exists

    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        result: dict[str, object] = {
            "live_registry_empty": not preparation._LIVE_WORKSPACES,
            "trusted_membership": storage._prepared_topology_observed_membership_capability_matches(
                trusted_topology
            ),
        }
        try:
            live_workspace._close_and_remove()
        except RuntimeError as error:
            result["cleanup_rejected"] = "different process" in str(error)
        else:
            result["cleanup_rejected"] = False
        try:
            closed_workspace.detach_legacy_topology(candidate)
        except RuntimeError as error:
            result["detach_rejected"] = "different process" in str(error)
        else:
            result["detach_rejected"] = False
        result["parent_workspace_still_exists"] = live_workspace.path_exists
        os.write(write_fd, json.dumps(result, sort_keys=True).encode("utf-8"))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    waited_pid, wait_status = os.waitpid(child_pid, 0)
    result = json.loads(b"".join(chunks))

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(wait_status) == 0
    assert result == {
        "cleanup_rejected": True,
        "detach_rejected": True,
        "live_registry_empty": True,
        "parent_workspace_still_exists": True,
        "trusted_membership": False,
    }
    assert live_workspace.path_exists
    assert storage._prepared_topology_observed_membership_capability_matches(trusted_topology)

    live_workspace._close_and_remove()
    detached = closed_workspace.detach_legacy_topology(candidate)
    assert storage._prepared_topology_observed_membership_capability_matches(detached)


def test_production_topology_subprocess_benchmark_reports_resource_boundary(
    tmp_path: Path,
) -> None:
    output = tmp_path / "production-topology-report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(benchmark.__file__).resolve()),
            "--implementation",
            "production-topology",
            "--nodes",
            "32",
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
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        "production topology benchmark failed"
        f"\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["schema_version"] == 2
    assert report["implementation"] == "production-topology"
    assert report["required_scale"] is False
    assert "bounded pre-legacy checkpoint" in report["memory_evidence_contract"]
    assert report["source_snapshot_before"] == report["source_snapshot_after"]
    assert report["forced_termination"]["parent_watchdog"] == "removed"
    assert [case["profile"] for case in report["cases"]] == ["sparse", "high-edge"]
    for case in report["cases"]:
        assert case["implementation"] == "production-topology"
        assert case["observed_nodes"] == 32
        assert case["observed_detail"] is None
        assert case["retained_detail"] is None
        assert case["detail_encoded_bytes"] is None
        assert case["detail_decoded_bytes"] is None
        assert case["detail_truncation_reasons"] == []
        assert case["legacy_observed_node_ids"] == 32
        assert case["spill_items"] == case["observed_nodes"] + case["observed_edges"]
        assert 0 < case["spill_peak_bytes"] <= 16 * 1024 * 1024
        assert case["tracemalloc_peak_bytes"] > 0
        assert case["peak_rss_bytes"] is None or case["peak_rss_bytes"] > 0
        assert 0 < case["bounded_phase_tracemalloc_current_bytes"]
        assert (
            case["bounded_phase_tracemalloc_current_bytes"]
            <= case["bounded_phase_tracemalloc_peak_bytes"]
            <= case["tracemalloc_peak_bytes"]
        )
        bounded_rss = case["bounded_phase_rss_measurement"]
        assert bounded_rss["peak_bytes"] == case["bounded_phase_peak_rss_bytes"]
        assert "bounded topology preparation" in bounded_rss["scope"]
        if case["bounded_phase_peak_rss_bytes"] is not None:
            assert case["bounded_phase_peak_rss_bytes"] > 0
        if case["peak_rss_bytes"] is not None:
            assert (
                case["bounded_phase_peak_rss_bytes"] is None
                or case["peak_rss_bytes"] >= case["bounded_phase_peak_rss_bytes"]
            )
        assert case["end_to_end_tracemalloc_peak_bytes"] == case["tracemalloc_peak_bytes"]
        assert case["end_to_end_peak_rss_bytes"] == case["peak_rss_bytes"]
        assert "phase-separated" in case["resident_contract"]
        assert "legacy O(observed)" in case["resident_contract"]
        assert case["cleanup"] == {
            "worker_context": "removed",
            "workspace_exists_after_context": False,
            "parent_watchdog": "removed",
            "scenario_root_exists_after_parent": False,
        }
        assert all("TEMP B-TREE" not in plan.upper() for plan in case["query_plans"])
    assert not list((tmp_path / "workspaces").iterdir())


def _benchmark_filesystem() -> dict[str, Any]:
    return {
        "identity_sha256": "a" * 64,
        "identity_method": "sha256(platform, st_dev)",
        "filesystem_type": "testfs",
        "allocation_block_bytes": 4096,
    }


def _benchmark_rss_measurement(*, peak: int, scope: str) -> dict[str, Any]:
    baseline = peak - 100
    return {
        "peak_bytes": peak,
        "method": "psutil.Process.memory_info().rss sampling",
        "scope": scope,
        "baseline_bytes": baseline,
        "baseline_current_bytes": baseline,
        "baseline_high_water_bytes": None,
        "sample_interval_seconds": 0.01,
        "sampled_peak_bytes": peak,
        "process_high_water_bytes": None,
    }


def _benchmark_budgets(*, implementation: str) -> dict[str, Any]:
    budgets = {
        "page_bytes": 4096,
        "cache_bytes": 8 * 1024 * 1024,
        "mmap_bytes": 0,
        "max_spill_bytes": 16 * 1024 * 1024,
        "control_reserve_bytes": 4 * 1024 * 1024,
        "max_node_items": 1_000_000,
        "max_edge_items": 4_000_000,
        "batch_max_items": 256,
        "batch_max_decoded_bytes": 4 * 1024 * 1024,
    }
    if implementation == "prototype-composite":
        budgets["max_detail_items"] = 1_000_000
    return budgets


def _benchmark_memory_case(
    *,
    implementation: str = "production-topology",
    observed_nodes: int = 25_000,
    profile: str = "sparse",
) -> dict[str, Any]:
    observed_edges = observed_nodes - 1 if profile == "sparse" else observed_nodes * 8
    production = implementation == "production-topology"
    budgets = _benchmark_budgets(implementation=implementation)
    limits = benchmark._v1_output_limits()
    retained_nodes = min(observed_nodes, limits["topology_node_max_items"])
    if profile == "sparse":
        eligible_edges = retained_nodes - 1
    elif retained_nodes == observed_nodes:
        eligible_edges = observed_edges
    else:
        factor = observed_edges // observed_nodes
        retained_offsets = min(factor, retained_nodes - 1)
        eligible_edges = (
            retained_offsets * retained_nodes - retained_offsets * (retained_offsets + 1) // 2
        )
    retained_edges = min(eligible_edges, limits["topology_edge_max_items"])
    topology_reasons = sorted(
        reason
        for reason, truncated in (
            ("node_count_limit", observed_nodes > limits["topology_node_max_items"]),
            ("edge_count_limit", eligible_edges > limits["topology_edge_max_items"]),
        )
        if truncated
    )
    detail_reasons = [] if production else topology_reasons
    topology_pages = (retained_nodes + limits["topology_page_max_items"] - 1) // limits[
        "topology_page_max_items"
    ]
    if retained_edges:
        topology_pages += (retained_edges + limits["topology_page_max_items"] - 1) // limits[
            "topology_page_max_items"
        ]
    case: dict[str, Any] = {
        "implementation": implementation,
        "observed_nodes": observed_nodes,
        "observed_edges": observed_edges,
        "observed_detail": None if production else observed_nodes,
        "retained_nodes": retained_nodes,
        "retained_edges": retained_edges,
        "retained_detail": None if production else min(observed_nodes, 25_000),
        "topology_pages": topology_pages,
        "topology_encoded_bytes": 1_000,
        "topology_decoded_bytes": 1_000,
        "detail_encoded_bytes": None if production else 500,
        "detail_decoded_bytes": None if production else 500,
        "legacy_observed_node_ids": observed_nodes if production else None,
        "manifest_digest": "b" * 64,
        "truncation_reasons": topology_reasons,
        "topology_truncation_reasons": topology_reasons,
        "detail_truncation_reasons": detail_reasons,
        "wall_seconds": 1.0,
        "cpu_seconds": 0.5,
        "profile": profile,
        "tracemalloc_peak_bytes": 200,
        "peak_rss_bytes": 400,
        "end_to_end_tracemalloc_peak_bytes": 200,
        "end_to_end_peak_rss_bytes": 400,
        "rss_measurement": _benchmark_rss_measurement(
            peak=400,
            scope="preparation measurement window",
        ),
        "bounded_phase_tracemalloc_current_bytes": None,
        "bounded_phase_tracemalloc_peak_bytes": None,
        "bounded_phase_peak_rss_bytes": None,
        "bounded_phase_rss_measurement": None,
        "spill_peak_bytes": 8 * 1024,
        "spill_items": observed_nodes + observed_edges + (0 if production else observed_nodes),
        "cleanup": {
            "worker_context": "removed",
            "workspace_exists_after_context": False,
            "parent_watchdog": "removed",
            "scenario_root_exists_after_parent": False,
        },
        "budgets": budgets,
        "v1_output_limits": limits,
        "sqlite_pragmas": {
            "page_size": 4096,
            "cache_size": -8192,
            "mmap_size": 0,
            "temp_store": 2,
            "journal_mode": "off",
            "synchronous": 0,
            "locking_mode": "exclusive",
            "foreign_keys": 1,
            "trusted_schema": 0,
            "max_page_count": 3072,
        },
        "query_plans": [
            "SCAN nodes",
            "SCAN e",
            "SEARCH source_node USING PRIMARY KEY (node_id=?)",
            "SEARCH target_node USING PRIMARY KEY (node_id=?)",
            "SCAN nodes" if production else "SCAN detail",
        ],
        "resident_contract": (
            benchmark._PRODUCTION_RESIDENT_CONTRACT
            if production
            else benchmark._PROTOTYPE_RESIDENT_CONTRACT
        ),
        "environment": {
            "platform": "test-platform",
            "python": "3.12.0",
            "python_implementation": "CPython",
            "sqlite": "3.45.0",
            "django": "6.0.0",
            "django_ray": "0.3.1",
            "pid": 141,
            "filesystem": _benchmark_filesystem(),
        },
    }
    if production:
        case.update(
            {
                "bounded_phase_tracemalloc_current_bytes": 50,
                "bounded_phase_tracemalloc_peak_bytes": 100,
                "bounded_phase_peak_rss_bytes": 300,
                "bounded_phase_rss_measurement": _benchmark_rss_measurement(
                    peak=300,
                    scope="measurement window through bounded topology preparation",
                ),
            }
        )
    return case


def _benchmark_v2_report(
    *,
    implementation: str = "production-topology",
    required_scale: bool = False,
) -> dict[str, Any]:
    cases = [_benchmark_memory_case(implementation=implementation)]
    if required_scale:
        cases = [
            _benchmark_memory_case(
                implementation=implementation,
                observed_nodes=nodes,
                profile=profile,
            )
            for nodes in benchmark.REQUIRED_SCALE_NODES
            for profile in benchmark.DEFAULT_PROFILES
        ]
    return {
        "schema_version": 2,
        "implementation": implementation,
        "required_scale": required_scale,
        "created_at": "2026-07-21T00:00:00Z",
        "source_revision": "1" * 40,
        "source_dirty": False,
        "implementation_digest": "2" * 64,
        "source_snapshot_before": {
            "revision": "1" * 40,
            "dirty": False,
            "implementation_digest": "2" * 64,
        },
        "source_snapshot_after": {
            "revision": "1" * 40,
            "dirty": False,
            "implementation_digest": "2" * 64,
        },
        "command": "benchmark --test",
        "cases": cases,
        "forced_termination": {
            "outcome": "forcibly-terminated",
            "readiness_observed": True,
            "workspace_open_before_kill": True,
            "worker_returncode": -9,
            "durable_candidate_exists_before_cleanup": False,
            "durable_candidate_exists_after_cleanup": False,
            "parent_watchdog": "removed",
            "scenario_root_exists_after_parent": False,
            "filesystem": _benchmark_filesystem(),
        },
        "memory_evidence_contract": (
            benchmark._PRODUCTION_MEMORY_EVIDENCE_CONTRACT
            if implementation == "production-topology"
            else benchmark._PROTOTYPE_MEMORY_EVIDENCE_CONTRACT
        ),
        "cleanup_contract": benchmark._CLEANUP_CONTRACT,
    }


def test_benchmark_v2_is_additive_and_retains_legacy_memory_fields() -> None:
    report = _benchmark_v2_report()
    report["future_top_level_field"] = {"ignored": True}
    report["source_snapshot_before"]["future_source_field"] = "before"
    report["source_snapshot_after"]["future_source_field"] = "after"
    report["cases"][0]["future_case_field"] = "ignored"
    report["cases"][0]["cleanup"]["future_cleanup_field"] = "ignored"
    report["cases"][0]["budgets"]["future_budget_field"] = 1
    report["cases"][0]["v1_output_limits"]["future_limit_field"] = 2
    report["cases"][0]["rss_measurement"]["future_rss_field"] = 3
    report["cases"][0]["environment"]["future_environment_field"] = "ignored"
    report["cases"][0]["environment"]["filesystem"]["future_filesystem_field"] = 4
    report["forced_termination"]["future_forced_field"] = "ignored"

    benchmark._validate_report(report)

    case = report["cases"][0]
    assert case["tracemalloc_peak_bytes"] == case["end_to_end_tracemalloc_peak_bytes"]
    assert case["peak_rss_bytes"] == case["end_to_end_peak_rss_bytes"]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("schema_version", "v2 report is incomplete"),
        ("memory_evidence_contract", "v2 report is incomplete"),
    ],
)
def test_benchmark_v2_rejects_missing_required_report_fields(
    field: str,
    message: str,
) -> None:
    report = _benchmark_v2_report()
    del report[field]

    with pytest.raises(RuntimeError, match=message):
        benchmark._validate_report(report)


def test_benchmark_v2_rejects_unsupported_schema_and_missing_case_fields() -> None:
    report = _benchmark_v2_report()
    report["schema_version"] = 1
    with pytest.raises(RuntimeError, match="report schema is unsupported"):
        benchmark._validate_report(report)

    report = _benchmark_v2_report(implementation="prototype-composite")
    del report["cases"][0]["bounded_phase_rss_measurement"]
    with pytest.raises(RuntimeError, match="case evidence is incomplete"):
        benchmark._validate_report(report)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            {"end_to_end_tracemalloc_peak_bytes": 201},
            "end-to-end memory evidence is inconsistent",
        ),
        (
            {"bounded_phase_tracemalloc_current_bytes": 101},
            "bounded tracemalloc evidence is invalid",
        ),
        (
            {"bounded_phase_peak_rss_bytes": 301},
            "bounded RSS evidence is invalid",
        ),
        (
            {
                "bounded_phase_peak_rss_bytes": 500,
                "bounded_phase_rss_measurement": _benchmark_rss_measurement(
                    peak=500,
                    scope="measurement window through bounded topology preparation",
                ),
            },
            "RSS peaks are not monotonic",
        ),
    ],
)
def test_benchmark_v2_rejects_inconsistent_production_memory_evidence(
    mutation: dict[str, Any],
    message: str,
) -> None:
    report = _benchmark_v2_report()
    report["cases"][0].update(deepcopy(mutation))

    with pytest.raises(RuntimeError, match=message):
        benchmark._validate_report(report)


def test_benchmark_required_scale_requires_exact_complete_matrix() -> None:
    report = _benchmark_v2_report(required_scale=True)
    benchmark._validate_report(report)

    missing = deepcopy(report)
    missing["cases"].pop()
    with pytest.raises(RuntimeError, match="required-scale matrix is incomplete"):
        benchmark._validate_report(missing)

    duplicate = deepcopy(report)
    duplicate["cases"][-1] = deepcopy(duplicate["cases"][0])
    with pytest.raises(RuntimeError, match="case matrix contains duplicates"):
        benchmark._validate_report(duplicate)

    incomplete_phase = deepcopy(report)
    del incomplete_phase["cases"][-1]["bounded_phase_rss_measurement"]
    with pytest.raises(RuntimeError, match="case evidence is incomplete"):
        benchmark._validate_report(incomplete_phase)


def test_benchmark_v2_requires_every_declared_evidence_field() -> None:
    report = _benchmark_v2_report()

    for field in benchmark._REPORT_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete[field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)

    for field in benchmark._SOURCE_SNAPSHOT_REQUIRED_FIELDS:
        for snapshot_name in ("source_snapshot_before", "source_snapshot_after"):
            incomplete = deepcopy(report)
            del incomplete[snapshot_name][field]
            with pytest.raises(RuntimeError):
                benchmark._validate_report(incomplete)

    for field in benchmark._CASE_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete["cases"][0][field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)

    nested_fields = (
        ("cleanup", benchmark._CLEANUP_REQUIRED_FIELDS),
        ("budgets", benchmark._COMMON_BUDGET_REQUIRED_FIELDS),
        ("v1_output_limits", benchmark._V1_OUTPUT_LIMIT_REQUIRED_FIELDS),
        ("sqlite_pragmas", benchmark._SQLITE_PRAGMA_REQUIRED_FIELDS),
        ("environment", benchmark._ENVIRONMENT_REQUIRED_FIELDS),
        ("rss_measurement", benchmark._RSS_REQUIRED_FIELDS),
        ("bounded_phase_rss_measurement", benchmark._RSS_REQUIRED_FIELDS),
    )
    for container, fields in nested_fields:
        for field in fields:
            incomplete = deepcopy(report)
            del incomplete["cases"][0][container][field]
            with pytest.raises(RuntimeError):
                benchmark._validate_report(incomplete)

    for container in ("environment",):
        for field in benchmark._FILESYSTEM_REQUIRED_FIELDS:
            incomplete = deepcopy(report)
            del incomplete["cases"][0][container]["filesystem"][field]
            with pytest.raises(RuntimeError):
                benchmark._validate_report(incomplete)

    for field in benchmark._FORCED_TERMINATION_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete["forced_termination"][field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)
    for field in benchmark._FILESYSTEM_REQUIRED_FIELDS:
        incomplete = deepcopy(report)
        del incomplete["forced_termination"]["filesystem"][field]
        with pytest.raises(RuntimeError):
            benchmark._validate_report(incomplete)

    prototype = _benchmark_v2_report(implementation="prototype-composite")
    del prototype["cases"][0]["budgets"]["max_detail_items"]
    with pytest.raises(RuntimeError):
        benchmark._validate_report(prototype)


@pytest.mark.parametrize(
    ("cleanup_field", "failed_value"),
    [
        ("worker_context", "refused"),
        ("workspace_exists_after_context", True),
        ("parent_watchdog", "failed"),
        ("scenario_root_exists_after_parent", True),
    ],
)
def test_benchmark_v2_rejects_failed_cleanup_even_in_complete_required_matrix(
    cleanup_field: str,
    failed_value: object,
) -> None:
    for required_scale in (False, True):
        report = _benchmark_v2_report(required_scale=required_scale)
        for case in report["cases"]:
            case["cleanup"][cleanup_field] = failed_value

        with pytest.raises(RuntimeError, match="cleanup did not succeed"):
            benchmark._validate_report(report)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("schema_version",), 2.0),
        (("source_dirty",), 0),
        (("cases", 0, "observed_nodes"), 25_000.0),
        (("cases", 0, "cleanup", "workspace_exists_after_context"), 0),
        (("cases", 0, "sqlite_pragmas", "synchronous"), False),
        (("cases", 0, "v1_output_limits", "storage_protocol_version"), True),
        (("cases", 0, "rss_measurement", "peak_bytes"), 400.0),
        (("forced_termination", "readiness_observed"), 1),
    ],
)
def test_benchmark_v2_rejects_cross_type_equal_substitutions(
    path: tuple[str | int, ...],
    replacement: object,
) -> None:
    report = _benchmark_v2_report()
    target: Any = report
    for part in path[:-1]:
        target = target[part]
    original = target[path[-1]]
    assert replacement == original
    assert type(replacement) is not type(original)
    target[path[-1]] = replacement

    with pytest.raises(RuntimeError):
        benchmark._validate_report(report)


def test_benchmark_v2_rejects_inconsistent_source_limits_and_filesystem() -> None:
    mutations = []

    source = _benchmark_v2_report()
    source["source_snapshot_after"]["revision"] = "3" * 40
    mutations.append(source)

    limits = _benchmark_v2_report(required_scale=True)
    limits["cases"][-1]["budgets"]["cache_bytes"] //= 2
    limits["cases"][-1]["sqlite_pragmas"]["cache_size"] //= 2
    mutations.append(limits)

    filesystem = _benchmark_v2_report()
    filesystem["forced_termination"]["filesystem"]["identity_sha256"] = "c" * 64
    mutations.append(filesystem)

    for report in mutations:
        with pytest.raises(RuntimeError):
            benchmark._validate_report(report)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("retained_nodes", 24_999),
        ("retained_edges", 24_998),
        ("topology_truncation_reasons", ["node_count_limit"]),
        ("topology_pages", 1),
        ("topology_decoded_bytes", 999),
        ("wall_seconds", 0.0),
        ("cpu_seconds", 0.0),
    ],
)
def test_benchmark_v2_rejects_inconsistent_generated_case_evidence(
    field: str,
    replacement: object,
) -> None:
    report = _benchmark_v2_report()
    report["cases"][0][field] = replacement

    with pytest.raises(RuntimeError):
        benchmark._validate_report(report)


def test_benchmark_v2_rejects_inconsistent_prototype_detail_evidence() -> None:
    mutations = []

    retained = _benchmark_v2_report(implementation="prototype-composite")
    retained["cases"][0]["retained_detail"] -= 1
    mutations.append(retained)

    reasons = _benchmark_v2_report(
        implementation="prototype-composite",
        required_scale=True,
    )
    high_edge = next(case for case in reasons["cases"] if case["profile"] == "high-edge")
    high_edge["detail_truncation_reasons"] = []
    mutations.append(reasons)

    decoded = _benchmark_v2_report(implementation="prototype-composite")
    decoded["cases"][0]["detail_decoded_bytes"] -= 1
    mutations.append(decoded)

    for report in mutations:
        with pytest.raises(RuntimeError):
            benchmark._validate_report(report)


def test_benchmark_v2_rejects_impossible_budget_limit_rss_and_environment_evidence() -> None:
    mutations = []

    budget = _benchmark_v2_report()
    budget["cases"][0]["budgets"]["cache_bytes"] = 16 * 1024 * 1024
    budget["cases"][0]["sqlite_pragmas"]["cache_size"] = -16 * 1024
    mutations.append(budget)

    limits = _benchmark_v2_report()
    limits["cases"][0]["v1_output_limits"]["combined_max_encoded_bytes"] = 1
    mutations.append(limits)

    rss = _benchmark_v2_report()
    rss["cases"][0]["rss_measurement"]["scope"] = "some memory scope"
    mutations.append(rss)

    environment = _benchmark_v2_report(required_scale=True)
    environment["cases"][-1]["environment"]["python"] = "different"
    mutations.append(environment)

    for report in mutations:
        with pytest.raises(RuntimeError):
            benchmark._validate_report(report)
