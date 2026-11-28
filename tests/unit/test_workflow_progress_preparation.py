"""Production integration tests for bounded workflow-topology preparation."""

from __future__ import annotations

import ast
import asyncio
import gc
import json
import os
import sqlite3
import weakref
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import django_ray.workflow.progress.preparation as preparation
import django_ray.workflow.progress.storage as storage
from tests.unit._workflow_progress_preparation_helpers import (
    _edge,
    _ForeignTopologyCollection,
    _identity,
    _OneShot,
    _ordered_scenario,
    _reorder,
    _topology_with_all_capability_evidence,
)
from tests.workflow_progress_storage_helpers import workflow_node, workflow_node_id


def test_storage_does_not_own_or_import_topology_preparation() -> None:
    assert not hasattr(storage, "prepare_workflow_progress_topology")

    source = Path(storage.__file__).read_text(encoding="utf-8")
    imports = {
        node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)
    }
    assert "django_ray.workflow.progress.preparation" not in imports


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
