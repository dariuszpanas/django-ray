"""Unit tests for #156 (composite preparation engine) and #158 (spill capacity controller)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import django_ray.workflow.progress.preparation as preparation
import django_ray.workflow.progress.storage as storage
from django_ray.runtime.context import WorkflowRunIdentity
from django_ray.workflow.progress.spill_capacity import (
    SpillCapacityConfig,
    SpillCapacityController,
    SpillCleanupOutcome,
    SpillOwnerDisposition,
    WorkflowProgressSpillCapacityConfigurationError,
    WorkflowProgressSpillCapacityExhaustedError,
)
from tests.workflow_progress_storage_helpers import (
    workflow_detail,
    workflow_node,
    workflow_node_id,
)


def _identity(pk: int = 156) -> WorkflowRunIdentity:
    return WorkflowRunIdentity(
        task_execution_pk=pk,
        attempt_number=1,
        execution_generation=1,
        run_id=f"00000000-0000-0000-0000-{pk:012d}",
    )


def _edge(source: str, target: str) -> dict[str, str]:
    return {"source": source, "target": target}


class _OneShot(Iterable[dict[str, Any]]):
    def __init__(self, values: Iterable[dict[str, Any]]) -> None:
        self._values = list(values)
        self.iter_calls = 0

    def __iter__(self) -> Iterator[dict[str, Any]]:
        self.iter_calls += 1
        if self.iter_calls > 1:
            raise AssertionError("one-shot input was consumed more than once")
        yield from self._values


# =============================================================================
# Issue #156: Bounded Composite Topology and Detail Preparation Engine
# =============================================================================


def test_composite_initial_publication_one_shot_and_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(tmp_path))
    identity = _identity()
    nodes = [workflow_node(workflow_node_id(i)) for i in range(6)]
    edges = [_edge(workflow_node_id(i - 1), workflow_node_id(i)) for i in range(1, 6)]
    details = [workflow_detail(workflow_node_id(i)) for i in range(6)]

    one_shot_nodes = _OneShot(nodes)
    one_shot_edges = _OneShot(edges)
    one_shot_details = _OneShot(details)

    pub = preparation.prepare_workflow_progress_initial_publication(
        identity,
        1,
        one_shot_nodes,
        one_shot_edges,
        one_shot_details,
        reporting_policy="full",
    )

    assert one_shot_nodes.iter_calls == 1
    assert one_shot_edges.iter_calls == 1
    assert one_shot_details.iter_calls == 1

    # Composite topology carries NO complete observed_node_ids field (#156).
    assert not hasattr(pub.topology, "observed_node_ids")
    assert pub.reporting_policy == "full"
    assert storage._prepared_initial_publication_capability_matches(pub)

    # Parity with materialized topology and detail preparation.
    expected_topology = storage._prepare_workflow_progress_topology_materialized(
        identity,
        1,
        nodes,
        edges,
    )
    expected_detail = storage.prepare_workflow_progress_detail(
        details,
        topology=expected_topology,
        reporting_policy="full",
    )
    assert preparation.canonical_topology_evidence(
        pub.topology
    ) == preparation.canonical_topology_evidence(expected_topology)
    assert pub.detail == expected_detail
    assert not list(tmp_path.iterdir())


def test_composite_capability_rejects_pair_substitution_copying_and_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(tmp_path))
    identity = _identity()
    nodes = [workflow_node("node-a"), workflow_node("node-b")]
    edges = [_edge("node-a", "node-b")]
    details = [
        workflow_detail("node-a"),
        workflow_detail("node-b"),
    ]

    pub_one = preparation.prepare_workflow_progress_initial_publication(
        identity, 1, nodes, edges, details
    )
    pub_two = preparation.prepare_workflow_progress_initial_publication(
        identity, 1, nodes, edges, details
    )

    assert storage._prepared_initial_publication_capability_matches(pub_one)
    assert storage._prepared_initial_publication_capability_matches(pub_two)

    # Mixing topology from pub_one with detail from pub_two must not mint trust.
    mixed = replace(pub_one, detail=pub_two.detail)
    assert not storage._prepared_initial_publication_capability_matches(mixed)

    mixed_topo = replace(pub_one, topology=pub_two.topology)
    assert not storage._prepared_initial_publication_capability_matches(mixed_topo)

    # Copying without token or with copied token fails capability check.
    copied = replace(pub_one)
    assert not storage._prepared_initial_publication_capability_matches(copied)

    # Type-equal scalar tampering on detail or topology fails capability check.
    tampered = preparation.prepare_workflow_progress_initial_publication(
        identity, 1, nodes, edges, details
    )
    object.__setattr__(tampered.detail, "observed_count", float(tampered.detail.observed_count))
    assert not storage._prepared_initial_publication_capability_matches(tampered)


def test_composite_workspace_phase_order_second_detachment_and_query_plans(
    tmp_path: Path,
) -> None:
    identity = _identity()
    nodes = [workflow_node("node-a")]
    details = [workflow_detail("node-a")]

    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with workspace:
        topology = workspace.prepare_topology(identity, 1, nodes, [])
        detail = workspace.prepare_detail(details, topology=topology, reporting_policy="full")
        plans = workspace.retained_query_plans()
        assert plans[-1].upper() == "SCAN DETAIL"
        # Cannot detach before workspace cleanup.
        with pytest.raises(RuntimeError, match="before owned workspace cleanup"):
            workspace.detach_initial_publication(topology, detail)

    pub = workspace.detach_initial_publication(topology, detail)
    assert storage._prepared_initial_publication_capability_matches(pub)

    # Second detachment is refused.
    with pytest.raises(RuntimeError, match="before owned workspace cleanup"):
        workspace.detach_initial_publication(topology, detail)

    # Cannot mix legacy detachment after composite detail preparation; poisons workspace.
    mixed_ws = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with mixed_ws:
        mixed_topo = mixed_ws.prepare_topology(identity, 1, nodes, [])
        mixed_detail = mixed_ws.prepare_detail(
            details,
            topology=mixed_topo,
            reporting_policy="full",
        )
        with pytest.raises(RuntimeError, match="sealed topology"):
            mixed_ws.prepare_legacy_detachment(mixed_topo)
    with pytest.raises(RuntimeError, match="before owned workspace cleanup"):
        mixed_ws.detach_initial_publication(mixed_topo, mixed_detail)


def test_composite_detail_completeness_and_oversized_record_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(tmp_path))
    identity = _identity()
    oversized_node = workflow_node("node-oversized")
    oversized_node["runtime_env"] = {f"k-{i}": "v" * 20 for i in range(600)}
    retained_node = workflow_node("node-retained")

    # Oversized detail body still satisfies full-policy membership without retaining payload.
    oversized_detail = workflow_detail("node-oversized")
    oversized_detail["progress"] = {
        "current": 1,
        "total": 2,
        "percent": 50.0,
        "message": "working",
        "metrics": {f"m-{i}": "x" * 100 for i in range(500)},
        "updated_at": "2026-07-20T12:00:00Z",
    }
    retained_detail = workflow_detail("node-retained")

    pub = preparation.prepare_workflow_progress_initial_publication(
        identity,
        1,
        [oversized_node, retained_node],
        [],
        [oversized_detail, retained_detail],
        reporting_policy="full",
    )
    assert pub.topology.observed_node_count == 2
    assert pub.topology.retained_node_count == 1
    assert pub.detail.observed_count == 2
    assert len(pub.detail.records) == 1
    assert pub.detail.records[0].node_id == "node-retained"

    # Missing detail record under full policy fails closed.
    with pytest.raises(
        storage.WorkflowProgressStorageError,
        match="full detail must contain one record per observed topology node",
    ):
        preparation.prepare_workflow_progress_initial_publication(
            identity,
            1,
            [oversized_node, retained_node],
            [],
            [retained_detail],
            reporting_policy="full",
        )

    # Sampled / terminal_only policies accept subset and record REPORTING_POLICY truncation.
    sampled_pub = preparation.prepare_workflow_progress_initial_publication(
        identity,
        1,
        [oversized_node, retained_node],
        [],
        [retained_detail],
        reporting_policy="terminal_only",
    )
    assert sampled_pub.reporting_policy == "terminal_only"
    assert "reporting_policy" in sampled_pub.detail.truncation_reasons


def test_legacy_detachment_refuses_above_25000_observed_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation, "LEGACY_OBSERVED_NODE_MAX_ITEMS", 5)
    identity = _identity()
    nodes = [workflow_node(workflow_node_id(i)) for i in range(6)]

    workspace = preparation.SQLitePreparationWorkspace(parent_directory=tmp_path)
    with pytest.raises(
        storage.WorkflowProgressStorageLimitError,
        match="legacy compatibility topology detachment refuses more than 5 observed nodes",
    ):
        with workspace:
            candidate = workspace.prepare_topology(identity, 1, nodes, [])
            workspace.prepare_legacy_detachment(candidate)

    assert workspace.cleanup_outcome == "removed"
    assert not workspace.path_exists


def test_composite_preparation_cleans_workspace_on_cancellation_and_registration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preparation.tempfile, "gettempdir", lambda: str(tmp_path))
    identity = _identity()
    nodes = [workflow_node("node-a"), workflow_node("node-b")]
    details = [
        workflow_detail("node-a"),
        workflow_detail("node-b"),
    ]

    in_detail = False

    def cancel_during_detail() -> None:
        if in_detail:
            raise asyncio.CancelledError("cancelled during detail")

    ws = preparation.SQLitePreparationWorkspace(
        parent_directory=tmp_path,
        cancellation_check=cancel_during_detail,
    )
    with pytest.raises(asyncio.CancelledError, match="cancelled during detail"):
        with ws:
            cand = ws.prepare_topology(identity, 1, nodes, [])
            in_detail = True
            ws.prepare_detail(details, topology=cand)
    assert ws.cleanup_outcome == "removed"
    assert not ws.path_exists

    # Registration failure after cleanup leaves no workspace behind.
    def fail_reg(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("composite registration failed")

    monkeypatch.setattr(storage, "_register_prepared_initial_publication_capability", fail_reg)
    with pytest.raises(RuntimeError, match="composite registration failed"):
        preparation.prepare_workflow_progress_initial_publication(identity, 1, nodes, [], details)
    assert not list(tmp_path.iterdir())


# =============================================================================
# Issue #158: Node-Local Spill Capacity Reservation and Janitor
# =============================================================================


def test_spill_capacity_config_validation(tmp_path: Path) -> None:
    with pytest.raises(
        WorkflowProgressSpillCapacityConfigurationError,
        match="max_aggregate_bytes must be at least 64 KiB",
    ):
        SpillCapacityConfig(root_directory=tmp_path, max_aggregate_bytes=1024).validated()

    with pytest.raises(
        WorkflowProgressSpillCapacityConfigurationError,
        match="max_workspaces_per_owner must be between 1 and max_workspaces",
    ):
        SpillCapacityConfig(
            root_directory=tmp_path,
            max_workspaces=2,
            max_workspaces_per_owner=3,
        ).validated()


def test_spill_capacity_admission_limits_and_workspace_integration(tmp_path: Path) -> None:
    spill_root = tmp_path / "spill"
    controller = SpillCapacityController(
        SpillCapacityConfig(
            root_directory=spill_root,
            max_aggregate_bytes=32 * 1024 * 1024,
            max_workspaces=2,
            max_workspaces_per_owner=2,
            min_free_headroom_bytes=64 * 1024 * 1024,
        ),
        disk_usage_fn=lambda _p: SimpleNamespace(free=256 * 1024 * 1024),
    )
    controller.verify_ready()

    ws_config = preparation.SQLitePreparationConfig(max_spill_bytes=16 * 1024 * 1024)
    ws1 = preparation.SQLitePreparationWorkspace(ws_config, spill_controller=controller)
    ws2 = preparation.SQLitePreparationWorkspace(ws_config, spill_controller=controller)
    ws3 = preparation.SQLitePreparationWorkspace(ws_config, spill_controller=controller)

    with ws1:
        with ws2:
            diag = controller.diagnostics()
            assert diag.admitted_workspaces == 2
            assert diag.reserved_bytes == 32 * 1024 * 1024
            assert diag.active_count == 2

            # Third concurrent workspace fails before iterable consumption or directory creation.
            with pytest.raises(
                WorkflowProgressSpillCapacityExhaustedError,
                match="workspace count limit",
            ):
                ws3.__enter__()
            assert ws3.cleanup_outcome == "not_created"

    diag_after = controller.diagnostics()
    assert diag_after.reserved_bytes == 0
    assert diag_after.active_count == 0
    assert diag_after.refused_workspaces == 1
    assert diag_after.last_refusal_reason == "workspace_count_exhausted"


def test_spill_capacity_enforces_aggregate_bytes_and_free_headroom(tmp_path: Path) -> None:
    spill_root = tmp_path / "spill-headroom"
    free_space = 100 * 1024 * 1024
    controller = SpillCapacityController(
        SpillCapacityConfig(
            root_directory=spill_root,
            max_aggregate_bytes=64 * 1024 * 1024,
            max_workspaces=4,
            max_workspaces_per_owner=4,
            min_free_headroom_bytes=80 * 1024 * 1024,
        ),
        disk_usage_fn=lambda _p: SimpleNamespace(free=free_space),
    )

    # 100 MiB free - 80 MiB headroom = 20 MiB available; requesting 24 MiB fails headroom check.
    with pytest.raises(
        WorkflowProgressSpillCapacityExhaustedError,
        match="free-space headroom is insufficient",
    ) as exc_info:
        controller.reserve(
            requested_bytes=24 * 1024 * 1024,
            workspace_id="00000000-0000-0000-0000-000000000001",
            workspace_token="tok-1",
        )
    assert exc_info.value.reason == "free_space_headroom_exhausted"

    # Requesting 16 MiB succeeds, then requesting 52 MiB exceeds 64 MiB aggregate budget.
    free_space = 500 * 1024 * 1024
    res = controller.reserve(
        requested_bytes=16 * 1024 * 1024,
        workspace_id="00000000-0000-0000-0000-000000000001",
        workspace_token="tok-1",
    )
    with pytest.raises(
        WorkflowProgressSpillCapacityExhaustedError,
        match="Aggregate spill byte budget",
    ) as exc_agg:
        controller.reserve(
            requested_bytes=52 * 1024 * 1024,
            workspace_id="00000000-0000-0000-0000-000000000002",
            workspace_token="tok-2",
        )
    assert exc_agg.value.reason == "aggregate_bytes_exhausted"
    assert controller.release(res) is True
    # Idempotent second release returns False without error.
    assert controller.release(res) is False


def test_spill_janitor_reclaims_proven_stale_and_preserves_live_indeterminate_and_foreign(
    tmp_path: Path,
) -> None:
    spill_root = tmp_path / "spill-janitor"
    now_ns = 1_000_000_000_000
    alive_pids = {1001, 2002}
    start_tokens = {
        1001: "start-1001",
        # PID 2002 was reused by a new process with a different start token!
        2002: "start-2002-new-process",
    }

    controller = SpillCapacityController(
        SpillCapacityConfig(
            root_directory=spill_root,
            stale_lease_grace_seconds=10.0,
        ),
        disk_usage_fn=lambda _p: SimpleNamespace(free=10 * 1024 * 1024 * 1024),
        pid_alive_fn=lambda pid: pid in alive_pids,
        process_start_token_fn=lambda pid: start_tokens.get(pid, f"start-{pid}"),
        clock_ns=lambda: now_ns,
    )
    controller.verify_ready()

    def make_workspace_dir(
        ws_uuid: str,
        *,
        pid: int,
        token: str,
        start_token: str,
        created_ns: int,
        extra_file: bool = False,
    ) -> Path:
        ws_dir = spill_root / f"{preparation._WORKSPACE_PREFIX}{ws_uuid}"
        ws_dir.mkdir(mode=0o700)
        (ws_dir / preparation._DATABASE_NAME).write_bytes(b"sqlite-data" * 10)
        (ws_dir / preparation._LEASE_NAME).write_text(
            f'{{"pid":{pid},"token":"{token}","workspace_id":"{ws_uuid}"}}',
            encoding="utf-8",
        )
        if extra_file:
            (ws_dir / "unowned.txt").write_text("tampered", encoding="utf-8")
        res_file = spill_root / "reservations" / f"reservation-{ws_uuid}.json"
        res_file.write_text(
            f'{{"reservation_id":"res-{ws_uuid}","workspace_id":"{ws_uuid}",'
            f'"workspace_token":"{token}","creator_pid":{pid},"creator_uid":null,'
            f'"process_start_token":"{start_token}","created_at_ns":{created_ns},'
            f'"requested_bytes":65536,"profile":"preparation-v1"}}',
            encoding="utf-8",
        )
        return ws_dir

    # 1. Live workspace (PID 1001 alive and matching start token) -> preserved
    live_uuid = "00000000-0000-0000-0000-000000000011"
    live_dir = make_workspace_dir(
        live_uuid,
        pid=1001,
        token="tok-live",
        start_token="start-1001",
        created_ns=now_ns - 60_000_000_000,
    )

    # 2. Stale workspace from dead PID 3003 (older than 10s grace) -> reclaimed
    stale_uuid = "00000000-0000-0000-0000-000000000022"
    stale_dir = make_workspace_dir(
        stale_uuid,
        pid=3003,
        token="tok-stale",
        start_token="start-3003",
        created_ns=now_ns - 60_000_000_000,
    )

    # 3. Stale workspace from PID reuse (PID 2002 alive, but start token changed) -> reclaimed
    reused_uuid = "00000000-0000-0000-0000-000000000033"
    reused_dir = make_workspace_dir(
        reused_uuid,
        pid=2002,
        token="tok-reused",
        start_token="start-2002-old-process",
        created_ns=now_ns - 60_000_000_000,
    )

    # 4. Indeterminate workspace (dead PID 4004, but within 10s grace window) -> preserved
    grace_uuid = "00000000-0000-0000-0000-000000000044"
    grace_dir = make_workspace_dir(
        grace_uuid,
        pid=4004,
        token="tok-grace",
        start_token="start-4004",
        created_ns=now_ns - 2_000_000_000,
    )

    # 5. Tampered stale workspace with unexpected file -> refused and preserved
    tampered_uuid = "00000000-0000-0000-0000-000000000055"
    tampered_dir = make_workspace_dir(
        tampered_uuid,
        pid=5005,
        token="tok-tampered",
        start_token="start-5005",
        created_ns=now_ns - 60_000_000_000,
        extra_file=True,
    )

    # 6. Foreign directory -> ignored and preserved
    foreign_dir = spill_root / "user-important-data"
    foreign_dir.mkdir()
    (foreign_dir / "keep.txt").write_text("keep", encoding="utf-8")

    report = controller.reclaim_stale_workspaces()
    outcomes_by_name = {item.name: item for item in report.outcomes}

    assert live_dir.exists()
    assert outcomes_by_name[live_dir.name].disposition is SpillOwnerDisposition.LIVE
    assert outcomes_by_name[live_dir.name].outcome is SpillCleanupOutcome.SKIPPED_LIVE

    assert not stale_dir.exists()
    assert outcomes_by_name[stale_dir.name].disposition is SpillOwnerDisposition.STALE
    assert outcomes_by_name[stale_dir.name].outcome is SpillCleanupOutcome.RECLAIMED

    assert not reused_dir.exists()
    assert outcomes_by_name[reused_dir.name].disposition is SpillOwnerDisposition.STALE
    assert outcomes_by_name[reused_dir.name].outcome is SpillCleanupOutcome.RECLAIMED

    assert grace_dir.exists()
    assert outcomes_by_name[grace_dir.name].disposition is SpillOwnerDisposition.INDETERMINATE
    assert outcomes_by_name[grace_dir.name].outcome is SpillCleanupOutcome.SKIPPED_INDETERMINATE

    assert tampered_dir.exists()
    assert outcomes_by_name[tampered_dir.name].outcome is SpillCleanupOutcome.REFUSED
    assert outcomes_by_name[tampered_dir.name].reason == "unexpected_or_linked_entry"

    assert foreign_dir.exists()
    assert foreign_dir.name not in outcomes_by_name
    assert report.reclaimed_count == 2
    assert report.reclaimed_bytes > 0


def test_spill_capacity_config_and_helper_edge_cases(tmp_path: Path) -> None:
    from django_ray.workflow.progress.spill_capacity import (
        WorkflowProgressSpillCapacityUnavailableError,
        _canonical_uuid,
        _cleanup_live_reservations_at_exit,
        _default_pid_alive,
        _default_process_start_token,
    )

    err = WorkflowProgressSpillCapacityUnavailableError("unavail", reason="test_reason")
    assert err.reason == "test_reason"
    assert _canonical_uuid("not-a-uuid") is None
    assert _default_pid_alive(0) is False
    assert _default_pid_alive(-5) is False
    assert _default_pid_alive(999_999_999) is False
    import os

    assert _default_pid_alive(os.getpid()) is True
    assert _default_process_start_token(os.getpid())

    with pytest.raises(WorkflowProgressSpillCapacityConfigurationError, match="pathlib.Path"):
        SpillCapacityConfig(root_directory="not-a-path").validated()  # type: ignore[arg-type]
    with pytest.raises(WorkflowProgressSpillCapacityConfigurationError, match="must be an integer"):
        SpillCapacityConfig(root_directory=tmp_path, max_workspaces=True).validated()  # type: ignore[arg-type]
    with pytest.raises(WorkflowProgressSpillCapacityConfigurationError, match="between 1 and 1024"):
        SpillCapacityConfig(root_directory=tmp_path, max_workspaces=0).validated()
    with pytest.raises(WorkflowProgressSpillCapacityConfigurationError, match="cannot be negative"):
        SpillCapacityConfig(root_directory=tmp_path, min_free_headroom_bytes=-1).validated()
    with pytest.raises(
        WorkflowProgressSpillCapacityConfigurationError,
        match="stale_lease_grace_seconds",
    ):
        SpillCapacityConfig(root_directory=tmp_path, stale_lease_grace_seconds=-1.0).validated()
    with pytest.raises(
        WorkflowProgressSpillCapacityConfigurationError,
        match="lock_timeout_seconds",
    ):
        SpillCapacityConfig(root_directory=tmp_path, lock_timeout_seconds=0.0).validated()

    _cleanup_live_reservations_at_exit()


def test_spill_capacity_reserve_release_and_janitor_error_branches(tmp_path: Path) -> None:
    from django_ray.workflow.progress.spill_capacity import (
        WorkflowProgressSpillCapacityUnavailableError,
        _cleanup_live_reservations_at_exit,
    )

    # Root path is a regular file -> spill_root_not_directory
    bad_root = tmp_path / "regular-file-root"
    bad_root.write_text("not-a-dir", encoding="utf-8")
    bad_ctrl = SpillCapacityController(SpillCapacityConfig(root_directory=bad_root))
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="not a regular directory",
    ):
        bad_ctrl.verify_ready()

    # Reservations path is a regular file -> reservations_dir_invalid
    res_file_root = tmp_path / "res-file-root"
    res_file_root.mkdir(mode=0o700)
    (res_file_root / "reservations").write_text("not-a-dir", encoding="utf-8")
    res_file_ctrl = SpillCapacityController(SpillCapacityConfig(root_directory=res_file_root))
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="reservations directory is invalid",
    ):
        res_file_ctrl.verify_ready()

    # Reserve validation errors, per-owner limit, duplicate reservation, disk_usage failure, lock contention
    spill_root = tmp_path / "spill-edges"
    now_ns = 2_000_000_000_000
    fail_disk = False

    def disk_usage(_p: Path) -> Any:
        if fail_disk:
            raise OSError("disk stat failed")
        return SimpleNamespace(free=1024 * 1024 * 1024)

    ctrl = SpillCapacityController(
        SpillCapacityConfig(
            root_directory=spill_root,
            max_workspaces=4,
            max_workspaces_per_owner=1,
            stale_lease_grace_seconds=10.0,
            lock_timeout_seconds=0.02,
        ),
        disk_usage_fn=disk_usage,
        clock_ns=lambda: now_ns,
    )
    ctrl.verify_ready()

    with pytest.raises(WorkflowProgressSpillCapacityConfigurationError, match="requested_bytes"):
        ctrl.reserve(
            requested_bytes=0,
            workspace_id="00000000-0000-0000-0000-000000000001",
            workspace_token="t",
        )
    with pytest.raises(WorkflowProgressSpillCapacityConfigurationError, match="canonical UUID"):
        ctrl.reserve(requested_bytes=1024, workspace_id="bad-uuid", workspace_token="t")
    with pytest.raises(WorkflowProgressSpillCapacityConfigurationError, match="workspace_token"):
        ctrl.reserve(
            requested_bytes=1024,
            workspace_id="00000000-0000-0000-0000-000000000001",
            workspace_token="",
        )

    fail_disk = True
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="Unable to inspect spill filesystem",
    ):
        ctrl.reserve(
            requested_bytes=1024,
            workspace_id="00000000-0000-0000-0000-000000000001",
            workspace_token="t1",
        )
    fail_disk = False

    ctrl.reserve(
        requested_bytes=1024,
        workspace_id="00000000-0000-0000-0000-000000000001",
        workspace_token="t1",
    )
    # Per-owner workspace limit exhausted
    with pytest.raises(
        WorkflowProgressSpillCapacityExhaustedError,
        match="Per-owner spill workspace count",
    ):
        ctrl.reserve(
            requested_bytes=1024,
            workspace_id="00000000-0000-0000-0000-000000000002",
            workspace_token="t2",
        )
    assert ctrl.diagnostics().as_dict()["active_count"] == 1
    _cleanup_live_reservations_at_exit()
    assert ctrl.diagnostics().active_count == 0

    # Duplicate reservation file and release tampering
    ctrl2 = SpillCapacityController(
        SpillCapacityConfig(
            root_directory=spill_root,
            max_workspaces=4,
            max_workspaces_per_owner=4,
            stale_lease_grace_seconds=10.0,
            lock_timeout_seconds=0.02,
        ),
        disk_usage_fn=disk_usage,
        pid_alive_fn=lambda pid: pid == 7777,
        clock_ns=lambda: now_ns,
    )
    r_dup = ctrl2.reserve(
        requested_bytes=1024,
        workspace_id="00000000-0000-0000-0000-000000000003",
        workspace_token="t3",
    )
    # Corrupt the reservation file so _inspect_reservations_locked treats it as indeterminate,
    # then try reserving the same workspace_id to hit FileExistsError!
    r_dup.reservation_file.write_text(
        '{"requested_bytes": 1024, "creator_pid": "bad"}', encoding="utf-8"
    )
    with pytest.raises(
        WorkflowProgressSpillCapacityExhaustedError,
        match="already exists",
    ):
        ctrl2.reserve(
            requested_bytes=1024,
            workspace_id="00000000-0000-0000-0000-000000000003",
            workspace_token="t3",
        )
    # Release detects identity mismatch
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="identity mismatch",
    ):
        ctrl2.release(r_dup)
    # Corrupt JSON during release
    r_dup.reservation_file.write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="Failed to inspect reservation file",
    ):
        ctrl2.release(r_dup)
    r_dup.reservation_file.unlink()

    # Cross-process lock contention timeout
    lock_dir = spill_root / ".django-ray-spill-capacity.lock"
    lock_dir.mkdir()
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="Timed out acquiring",
    ):
        ctrl2.diagnostics()
    lock_dir.rmdir()

    # Populate malformed reservation files and diverse janitor candidates
    res_dir = spill_root / "reservations"
    (res_dir / "ignored.txt").write_text("x", encoding="utf-8")
    (res_dir / "reservation-not-a-uuid.json").write_text("{}", encoding="utf-8")
    (res_dir / "reservation-00000000-0000-0000-0000-000000000090.json").write_text(
        "[1, 2, 3]", encoding="utf-8"
    )
    (res_dir / "reservation-00000000-0000-0000-0000-000000000091.json").write_text(
        '{"requested_bytes": 0}', encoding="utf-8"
    )
    # Orphan stale reservation file (no workspace dir)
    orphan_uuid = "00000000-0000-0000-0000-000000000092"
    (res_dir / f"reservation-{orphan_uuid}.json").write_text(
        f'{{"reservation_id":"r92","workspace_id":"{orphan_uuid}","workspace_token":"t92",'
        f'"creator_pid":8888,"process_start_token":"s8888","created_at_ns":1000,'
        f'"requested_bytes":1024}}',
        encoding="utf-8",
    )

    # Candidate 1: regular file with workspace prefix -> candidate_not_owned_directory
    file_cand = spill_root / f"{preparation._WORKSPACE_PREFIX}00000000-0000-0000-0000-000000000061"
    file_cand.write_text("file", encoding="utf-8")

    # Candidate 2: directory missing owner.json -> missing_owner_lease
    no_lease_dir = (
        spill_root / f"{preparation._WORKSPACE_PREFIX}00000000-0000-0000-0000-000000000062"
    )
    no_lease_dir.mkdir(mode=0o700)

    # Candidate 3: corrupt owner.json -> corrupt_owner_lease
    corrupt_lease_dir = (
        spill_root / f"{preparation._WORKSPACE_PREFIX}00000000-0000-0000-0000-000000000063"
    )
    corrupt_lease_dir.mkdir(mode=0o700)
    (corrupt_lease_dir / preparation._LEASE_NAME).write_text("{bad", encoding="utf-8")

    # Candidate 4: invalid owner.json fields -> invalid_owner_lease_fields
    bad_fields_dir = (
        spill_root / f"{preparation._WORKSPACE_PREFIX}00000000-0000-0000-0000-000000000064"
    )
    bad_fields_dir.mkdir(mode=0o700)
    (bad_fields_dir / preparation._LEASE_NAME).write_text('{"pid":"bad"}', encoding="utf-8")

    # Candidate 5: reservation vs lease mismatch -> reservation_lease_mismatch
    mismatch_uuid = "00000000-0000-0000-0000-000000000065"
    mismatch_dir = spill_root / f"{preparation._WORKSPACE_PREFIX}{mismatch_uuid}"
    mismatch_dir.mkdir(mode=0o700)
    (mismatch_dir / preparation._LEASE_NAME).write_text(
        f'{{"pid":8888,"token":"tok-a","workspace_id":"{mismatch_uuid}"}}',
        encoding="utf-8",
    )
    (res_dir / f"reservation-{mismatch_uuid}.json").write_text(
        f'{{"reservation_id":"r65","workspace_id":"{mismatch_uuid}","workspace_token":"tok-b",'
        f'"creator_pid":8888,"process_start_token":"s8888","created_at_ns":1000,'
        f'"requested_bytes":1024}}',
        encoding="utf-8",
    )

    # Candidate 6: quarantine directory without reservation file, dead PID, old mtime -> reclaimed
    q_uuid = "00000000-0000-0000-0000-000000000066"
    q_dir = spill_root / f"{preparation._QUARANTINE_PREFIX}{q_uuid}-deadbeef"
    q_dir.mkdir(mode=0o700)
    (q_dir / preparation._LEASE_NAME).write_text(
        f'{{"pid":8888,"token":"tok-q","workspace_id":"{q_uuid}"}}',
        encoding="utf-8",
    )
    import os

    os.utime(q_dir, ns=(1000, 1000))

    # Candidate 7: workspace without reservation file, live PID 7777 -> skipped_live
    live_no_res_uuid = "00000000-0000-0000-0000-000000000067"
    live_no_res_dir = spill_root / f"{preparation._WORKSPACE_PREFIX}{live_no_res_uuid}"
    live_no_res_dir.mkdir(mode=0o700)
    (live_no_res_dir / preparation._LEASE_NAME).write_text(
        f'{{"pid":7777,"token":"tok-live","workspace_id":"{live_no_res_uuid}"}}',
        encoding="utf-8",
    )

    report = ctrl2.reclaim_stale_workspaces()
    by_name = {item.name: item for item in report.outcomes}
    assert by_name[file_cand.name].reason == "candidate_not_owned_directory"
    assert by_name[no_lease_dir.name].reason == "missing_owner_lease"
    assert by_name[corrupt_lease_dir.name].reason == "corrupt_owner_lease"
    assert by_name[bad_fields_dir.name].reason == "invalid_owner_lease_fields"
    assert by_name[mismatch_dir.name].reason == "reservation_lease_mismatch"
    assert by_name[q_dir.name].outcome is SpillCleanupOutcome.RECLAIMED
    assert by_name[live_no_res_dir.name].outcome is SpillCleanupOutcome.SKIPPED_LIVE
    assert by_name[f"reservation-{orphan_uuid}.json"].outcome is SpillCleanupOutcome.RECLAIMED


def test_spill_capacity_proc_token_pid_alive_and_filesystem_error_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from django_ray.workflow.progress.spill_capacity import (
        WorkflowProgressSpillCapacityUnavailableError,
        _default_pid_alive,
        _default_process_start_token,
    )

    # _default_process_start_token reading /proc/<pid>/stat
    fake_stat = "123 (python) S " + " ".join(str(i) for i in range(25))
    monkeypatch.setattr(Path, "is_file", lambda _self: True)
    monkeypatch.setattr(Path, "read_text", lambda _self, **_kw: fake_stat)
    assert _default_process_start_token(123).startswith("proc-starttime:")

    monkeypatch.undo()

    # _default_pid_alive PermissionError branch
    def raise_perm(_pid: int, _sig: int) -> None:
        raise PermissionError("not permitted")

    monkeypatch.setattr(os, "kill", raise_perm)
    assert _default_pid_alive(4321) is True
    monkeypatch.undo()

    # Identity drift detection on controller and release
    spill_root = tmp_path / "drift-root"
    ctrl = SpillCapacityController(
        SpillCapacityConfig(root_directory=spill_root, stale_lease_grace_seconds=10.0),
        disk_usage_fn=lambda _p: SimpleNamespace(free=1024 * 1024 * 1024),
    )
    ctrl.verify_ready()
    res = ctrl.reserve(
        requested_bytes=1024,
        workspace_id="00000000-0000-0000-0000-000000000077",
        workspace_token="t77",
    )
    # Tamper reservation file into a directory during release -> reservation_file_tampered
    res.reservation_file.unlink()
    res.reservation_file.mkdir()
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="non-regular file or hardlink",
    ):
        ctrl.release(res)
    res.reservation_file.rmdir()

    # Drift root identity on controller
    ctrl._root_identity = preparation._PathIdentity(
        device=999999, inode=999999, windows_creation_ns=None
    )
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="identity drifted",
    ):
        ctrl.diagnostics()

    # Candidate without reservation file within grace window -> skipped_indeterminate
    ctrl_grace = SpillCapacityController(
        SpillCapacityConfig(root_directory=spill_root, stale_lease_grace_seconds=3600.0),
        disk_usage_fn=lambda _p: SimpleNamespace(free=1024 * 1024 * 1024),
        pid_alive_fn=lambda _pid: False,
    )
    grace_no_res_uuid = "00000000-0000-0000-0000-000000000078"
    grace_no_res_dir = spill_root / f"{preparation._WORKSPACE_PREFIX}{grace_no_res_uuid}"
    grace_no_res_dir.mkdir(mode=0o700)
    (grace_no_res_dir / preparation._LEASE_NAME).write_text(
        f'{{"pid":9999,"token":"tok-g","workspace_id":"{grace_no_res_uuid}"}}',
        encoding="utf-8",
    )
    rep = ctrl_grace.reclaim_stale_workspaces()
    by_name = {item.name: item for item in rep.outcomes}
    assert by_name[grace_no_res_dir.name].outcome is SpillCleanupOutcome.SKIPPED_INDETERMINATE


def test_spill_capacity_additional_io_and_release_failure_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from django_ray.workflow.progress.spill_capacity import (
        WorkflowProgressSpillCapacityUnavailableError,
    )

    # _validate_root_directory(initialize=False) on missing root -> spill_root_inaccessible
    missing_ctrl = SpillCapacityController(
        SpillCapacityConfig(root_directory=tmp_path / "does-not-exist")
    )
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="inaccessible",
    ):
        missing_ctrl._validate_root_directory(initialize=False)

    spill_root = tmp_path / "io-branches"
    ctrl = SpillCapacityController(
        SpillCapacityConfig(root_directory=spill_root, stale_lease_grace_seconds=0.0),
        disk_usage_fn=lambda _p: SimpleNamespace(free=1024 * 1024 * 1024),
        pid_alive_fn=lambda _pid: False,
    )
    ctrl.verify_ready()
    res = ctrl.reserve(
        requested_bytes=1024,
        workspace_id="00000000-0000-0000-0000-000000000081",
        workspace_token="t81",
    )

    # release with mismatched reservation.root_identity
    drifted_res = replace(
        res,
        root_identity=preparation._PathIdentity(
            device=888888, inode=888888, windows_creation_ns=None
        ),
    )
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="changed before reservation release",
    ):
        ctrl.release(drifted_res)

    # release when unlink raises OSError
    orig_unlink = Path.unlink

    def fail_unlink(self_path: Path, *a: Any, **kw: Any) -> None:
        if self_path == res.reservation_file:
            raise OSError("unlink blocked")
        orig_unlink(self_path, *a, **kw)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="Failed to unlink reservation file",
    ):
        ctrl.release(res)
    monkeypatch.undo()

    # Janitor when orphan reservation unlink fails and when candidate rmdir fails
    ws_uuid = "00000000-0000-0000-0000-000000000082"
    ws_dir = spill_root / f"{preparation._WORKSPACE_PREFIX}{ws_uuid}"
    ws_dir.mkdir(mode=0o700)
    (ws_dir / preparation._LEASE_NAME).write_text(
        f'{{"pid":9999,"token":"t82","workspace_id":"{ws_uuid}"}}',
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(Path, "rmdir", lambda _self: (_ for _ in ()).throw(OSError("rmdir failed")))
    rep = ctrl.reclaim_stale_workspaces()
    by_name = {item.name: item for item in rep.outcomes}
    assert by_name[res.reservation_file.name].outcome is SpillCleanupOutcome.OPERATIONAL_FAILURE
    assert by_name[ws_dir.name].outcome is SpillCleanupOutcome.OPERATIONAL_FAILURE
    monkeypatch.undo()


def test_spill_capacity_posix_and_stat_iterdir_fault_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from django_ray.workflow.progress.spill_capacity import (
        WorkflowProgressSpillCapacityUnavailableError,
    )

    spill_root = tmp_path / "posix-faults"
    ctrl = SpillCapacityController(
        SpillCapacityConfig(root_directory=spill_root, stale_lease_grace_seconds=0.0),
        disk_usage_fn=lambda _p: SimpleNamespace(free=1024 * 1024 * 1024),
        pid_alive_fn=lambda _pid: False,
    )
    ctrl.verify_ready()

    # Simulate POSIX uid mismatch on root, reservations dir, and janitor candidate
    ws_uuid = "00000000-0000-0000-0000-000000000085"
    ws_dir = spill_root / f"{preparation._WORKSPACE_PREFIX}{ws_uuid}"
    ws_dir.mkdir(mode=0o700)
    (ws_dir / preparation._LEASE_NAME).write_text(
        f'{{"pid":9999,"token":"t85","workspace_id":"{ws_uuid}"}}',
        encoding="utf-8",
    )

    monkeypatch.setattr(os, "name", "posix", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 999999, raising=False)
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="permissions or owner are unsafe",
    ):
        ctrl.diagnostics()
    monkeypatch.undo()

    # _inspect_reservations_locked iterdir failure
    res_dir = spill_root / "reservations"
    orig_iterdir = Path.iterdir

    def fail_res_iterdir(self_path: Path):
        if self_path == res_dir:
            raise OSError("res iterdir failed")
        return orig_iterdir(self_path)

    monkeypatch.setattr(Path, "iterdir", fail_res_iterdir)
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="Failed to scan spill capacity reservations",
    ):
        ctrl.diagnostics()
    monkeypatch.undo()

    # Candidate iterdir failure during janitor
    def fail_ws_iterdir(self_path: Path):
        if self_path == ws_dir:
            raise OSError("ws iterdir failed")
        return orig_iterdir(self_path)

    monkeypatch.setattr(Path, "iterdir", fail_ws_iterdir)
    rep = ctrl.reclaim_stale_workspaces()
    by_name = {item.name: item for item in rep.outcomes}
    assert by_name[ws_dir.name].reason == "candidate_iterdir_failed"
    monkeypatch.undo()

    # Reserve open failure
    orig_open = Path.open

    def fail_open(self_path: Path, mode: str = "r", *a: Any, **kw: Any):
        if mode == "x":
            raise OSError("open x failed")
        return orig_open(self_path, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(
        WorkflowProgressSpillCapacityUnavailableError,
        match="Failed to persist spill capacity reservation",
    ):
        ctrl.reserve(
            requested_bytes=1024,
            workspace_id="00000000-0000-0000-0000-000000000086",
            workspace_token="t86",
        )
    monkeypatch.undo()
