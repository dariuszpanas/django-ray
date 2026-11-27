"""Unit tests for local KubeRay workflow Admin smoke gates."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, cast
from urllib.request import ProxyHandler

import pytest

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    APP_DEPLOYMENTS,
    DOCKER_CONTEXT_ALLOWLISTS,
    EVIDENCE_LINE_LIMIT,
    EXPECTED_NAMESPACE,
    CommandResult,
    LocalKubeRayGate,
    RejectRedirects,
    inspect_docker_context_allowlists,
)
from tests.unit._local_kuberay_gate_helpers import (
    APP_TAG,
    COMMIT,
    FAILED_WORKFLOW_SHOWCASE_TASK_ID,
    FAILED_WORKFLOW_TASK_ID,
    IMAGE_ID,
    ROOT,
    SOURCE_TREE,
    TAG,
    TASK_ID,
    TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
    TERMINAL_ONLY_WORKFLOW_TASK_ID,
    WORKFLOW_RECOVERY_TASK_ID,
    WORKFLOW_SHOWCASE_TASK_ID,
    WORKFLOW_TASK_ID,
    _config,
)


def _workflow_admin_smoke_evidence(
    *,
    task_id: str = WORKFLOW_TASK_ID,
    task_state: str = "SUCCEEDED",
    attempt_number: int = 1,
    topology_nodes: int = 3,
    topology_edges: int = 2,
    pending_nodes: int = 0,
    running_nodes: int = 0,
    succeeded_nodes: int = 3,
    failed_nodes: int = 0,
    failure_path_nodes: int = 0,
    failure_origins: int = 0,
    incoming_failure_edges: int = 0,
    available_previews: int = 0,
    failed_previews: int = 0,
    unavailable_previews: int = 0,
    preview_contract: str = "not-applicable",
) -> dict[str, str | int]:
    return {
        "admin_workflow": "verified",
        "task_id": task_id,
        "task_state": task_state,
        "attempt_number": attempt_number,
        "admin_routes": 6,
        "admin_actions": 3,
        "topology_nodes": topology_nodes,
        "topology_edges": topology_edges,
        "node_details": topology_nodes,
        "graph_status": "AVAILABLE",
        "graph_nodes": topology_nodes,
        "graph_edges": topology_edges,
        "graph_pending_nodes": pending_nodes,
        "graph_running_nodes": running_nodes,
        "graph_succeeded_nodes": succeeded_nodes,
        "graph_failed_nodes": failed_nodes,
        "graph_failure_path_nodes": failure_path_nodes,
        "graph_failure_origins": failure_origins,
        "graph_incoming_failure_edges": incoming_failure_edges,
        "graph_available_previews": available_previews,
        "graph_failed_previews": failed_previews,
        "graph_unavailable_previews": unavailable_previews,
        "graph_preview_contract": preview_contract,
        "current_manifests": 1,
        "pending_manifests": 0,
        "unlinked_pages": 0,
    }


def _terminal_only_admin_smoke_evidence(
    *,
    task_id: str = TERMINAL_ONLY_WORKFLOW_TASK_ID,
    task_state: str = "SUCCEEDED",
) -> dict[str, bool | int | str]:
    return {
        "admin_workflow": "terminal-summary-verified",
        "task_id": task_id,
        "task_state": task_state,
        "attempt_number": 1,
        "admin_actions": 0,
        "graph_advertised": False,
        "graph_status": "UNAVAILABLE",
        "summary_revision": 1,
        "reporting_policy": "terminal_only",
        "detail_availability": "OMITTED_BY_POLICY",
        "declared_nodes": 13,
        "declared_edges": 13,
        "legacy_progress_null": True,
        "attempt_summary_matches": True,
        "storage_rows": 0,
        "topology_manifests": 0,
        "topology_pages": 0,
        "manifest_links": 0,
        "node_details": 0,
    }


def _seed_workflow_admin_evidence(gate: LocalKubeRayGate) -> None:
    gate.evidence.workflow_task_id = WORKFLOW_TASK_ID
    gate.evidence.workflow_task_state = "SUCCEEDED"
    gate.evidence.workflow_attempt_number = 1
    gate.evidence.workflow_topology_nodes = 3
    gate.evidence.workflow_topology_edges = 2
    gate.evidence.workflow_node_details = 3
    gate.evidence.workflow_failure_task_id = FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_failure_task_state = "FAILED"
    gate.evidence.workflow_failure_attempt_number = 1
    gate.evidence.workflow_failure_topology_nodes = 5
    gate.evidence.workflow_failure_topology_edges = 6
    gate.evidence.workflow_failure_node_details = 5
    gate.evidence.workflow_failure_pending_nodes = 1
    gate.evidence.workflow_failure_running_nodes = 1
    gate.evidence.workflow_failure_succeeded_nodes = 2
    gate.evidence.workflow_failure_failed_nodes = 1
    gate.evidence.workflow_terminal_only_task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_task_state = "SUCCEEDED"
    gate.evidence.workflow_terminal_only_attempt_number = 1
    gate.evidence.workflow_terminal_only_schema_version = 3
    gate.evidence.workflow_terminal_only_summary_revision = 1
    gate.evidence.workflow_terminal_only_declared_nodes = 13
    gate.evidence.workflow_terminal_only_declared_edges = 13
    gate.evidence.workflow_terminal_only_failure_task_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_failure_task_state = "FAILED"
    gate.evidence.workflow_terminal_only_failure_attempt_number = 1
    gate.evidence.workflow_terminal_only_failure_schema_version = 3
    gate.evidence.workflow_terminal_only_failure_summary_revision = 1
    gate.evidence.workflow_terminal_only_failure_declared_nodes = 13
    gate.evidence.workflow_terminal_only_failure_declared_edges = 13
    gate.evidence.workflow_showcase_task_id = WORKFLOW_SHOWCASE_TASK_ID
    gate.evidence.workflow_showcase_task_state = "SUCCEEDED"
    gate.evidence.workflow_showcase_attempt_number = 1
    gate.evidence.workflow_showcase_topology_nodes = 21
    gate.evidence.workflow_showcase_topology_edges = 28
    gate.evidence.workflow_showcase_detail_links = 21
    gate.evidence.workflow_showcase_failure_task_id = FAILED_WORKFLOW_SHOWCASE_TASK_ID
    gate.evidence.workflow_showcase_failure_task_state = "FAILED"
    gate.evidence.workflow_showcase_failure_attempt_number = 1
    gate.evidence.workflow_showcase_failure_failed_nodes = 1
    gate.evidence.workflow_showcase_failure_pending_descendants = 0
    gate.evidence.workflow_showcase_failure_running_nodes = 0
    gate.evidence.workflow_showcase_failure_succeeded_nodes = 15
    gate.evidence.workflow_showcase_failure_path_nodes = 16
    gate.evidence.workflow_showcase_failure_detail_links = 16
    gate.evidence.workflow_recovery_task_id = WORKFLOW_RECOVERY_TASK_ID
    gate.evidence.workflow_recovery_task_state = "SUCCEEDED"
    gate.evidence.workflow_recovery_attempt_number = 3
    gate.evidence.workflow_recovery_attempt_count = 3
    gate.evidence.workflow_recovery_distinct_runs = True
    gate.evidence.workflow_recovery_early_topology_nodes = 2
    gate.evidence.workflow_recovery_early_topology_edges = 1
    gate.evidence.workflow_recovery_early_pending_nodes = 1
    gate.evidence.workflow_recovery_early_succeeded_nodes = 0
    gate.evidence.workflow_recovery_early_failed_nodes = 1
    gate.evidence.workflow_recovery_early_detail_links = 2
    gate.evidence.workflow_recovery_mid_topology_nodes = 15
    gate.evidence.workflow_recovery_mid_topology_edges = len(
        gate_module.WORKFLOW_RECOVERY_MID_EDGES
    )
    gate.evidence.workflow_recovery_mid_pending_nodes = 7
    gate.evidence.workflow_recovery_mid_succeeded_nodes = 7
    gate.evidence.workflow_recovery_mid_failed_nodes = 1
    gate.evidence.workflow_recovery_mid_detail_links = 15
    gate.evidence.workflow_recovery_success_topology_nodes = 21
    gate.evidence.workflow_recovery_success_topology_edges = 28
    gate.evidence.workflow_recovery_success_succeeded_nodes = 21
    gate.evidence.workflow_recovery_success_detail_links = 21


def test_workflow_admin_gate_executes_same_task_inside_django_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    _seed_workflow_admin_evidence(gate)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        calls.append((args, kwargs))
        task_id = args[args.index("--existing-workflow-task-id") + 1]
        terminal_only = "--expected-workflow-reporting-policy" in args
        if terminal_only:
            payload = (
                _terminal_only_admin_smoke_evidence()
                if task_id == TERMINAL_ONLY_WORKFLOW_TASK_ID
                else _terminal_only_admin_smoke_evidence(
                    task_id=TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID,
                    task_state="FAILED",
                )
            )
        else:
            selected_attempt = (
                int(args[args.index("--existing-workflow-attempt-number") + 1])
                if "--existing-workflow-attempt-number" in args
                else None
            )
            if task_id == WORKFLOW_TASK_ID:
                payload = _workflow_admin_smoke_evidence()
            elif task_id == FAILED_WORKFLOW_TASK_ID:
                payload = _workflow_admin_smoke_evidence(
                    task_id=FAILED_WORKFLOW_TASK_ID,
                    task_state="FAILED",
                    topology_nodes=5,
                    topology_edges=6,
                    pending_nodes=1,
                    running_nodes=1,
                    succeeded_nodes=2,
                    failed_nodes=1,
                    failure_path_nodes=2,
                    failure_origins=1,
                    incoming_failure_edges=1,
                )
            elif task_id == WORKFLOW_SHOWCASE_TASK_ID:
                payload = _workflow_admin_smoke_evidence(
                    task_id=WORKFLOW_SHOWCASE_TASK_ID,
                    topology_nodes=21,
                    topology_edges=28,
                    succeeded_nodes=21,
                    available_previews=18,
                    failed_previews=1,
                    preview_contract="showcase-succeeded-verified",
                )
            elif task_id == WORKFLOW_RECOVERY_TASK_ID and selected_attempt == 1:
                payload = _workflow_admin_smoke_evidence(
                    task_id=WORKFLOW_RECOVERY_TASK_ID,
                    task_state="FAILED",
                    attempt_number=1,
                    topology_nodes=2,
                    topology_edges=1,
                    pending_nodes=1,
                    succeeded_nodes=0,
                    failed_nodes=1,
                    failure_path_nodes=1,
                    failure_origins=1,
                    incoming_failure_edges=0,
                    available_previews=0,
                    failed_previews=0,
                    unavailable_previews=2,
                )
            elif task_id == WORKFLOW_RECOVERY_TASK_ID and selected_attempt == 2:
                payload = _workflow_admin_smoke_evidence(
                    task_id=WORKFLOW_RECOVERY_TASK_ID,
                    task_state="FAILED",
                    attempt_number=2,
                    topology_nodes=15,
                    topology_edges=len(gate_module.WORKFLOW_RECOVERY_MID_EDGES),
                    pending_nodes=7,
                    succeeded_nodes=7,
                    failed_nodes=1,
                    failure_path_nodes=8,
                    failure_origins=1,
                    incoming_failure_edges=3,
                    available_previews=5,
                    failed_previews=1,
                    unavailable_previews=8,
                )
            elif task_id == WORKFLOW_RECOVERY_TASK_ID and selected_attempt == 3:
                payload = _workflow_admin_smoke_evidence(
                    task_id=WORKFLOW_RECOVERY_TASK_ID,
                    attempt_number=3,
                    topology_nodes=21,
                    topology_edges=28,
                    succeeded_nodes=21,
                    available_previews=18,
                    failed_previews=1,
                    unavailable_previews=0,
                )
            else:
                payload = _workflow_admin_smoke_evidence(
                    task_id=FAILED_WORKFLOW_SHOWCASE_TASK_ID,
                    task_state="FAILED",
                    topology_nodes=16,
                    topology_edges=21,
                    pending_nodes=0,
                    running_nodes=0,
                    succeeded_nodes=15,
                    failed_nodes=1,
                    failure_path_nodes=16,
                    failure_origins=1,
                    incoming_failure_edges=1,
                    available_previews=13,
                    failed_previews=1,
                    unavailable_previews=1,
                    preview_contract="showcase-failed-verified",
                )
        return CommandResult(json.dumps(payload), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    gate._verify_workflow_admin()

    def expected_call(
        task_id: str,
        *,
        terminal_only: bool = False,
        attempt_number: int | None = None,
    ):
        command = (
            "exec",
            "deployment/django-web",
            "-c",
            "django-web",
            "--",
            "python",
            "-m",
            "testproject.docker_smoke",
            "--base-url",
            "http://127.0.0.1:8000",
            "--timeout",
            "180",
            "--existing-workflow-task-id",
            task_id,
        )
        if terminal_only:
            command = (
                *command,
                "--expected-workflow-reporting-policy",
                "terminal_only",
            )
        if attempt_number is not None:
            command = (
                *command,
                "--existing-workflow-attempt-number",
                str(attempt_number),
            )
        return (
            command,
            {
                "timeout": 215,
                "sensitive_output": True,
            },
        )

    assert calls == [
        expected_call(WORKFLOW_TASK_ID),
        expected_call(FAILED_WORKFLOW_TASK_ID),
        expected_call(WORKFLOW_SHOWCASE_TASK_ID),
        expected_call(FAILED_WORKFLOW_SHOWCASE_TASK_ID),
        expected_call(WORKFLOW_RECOVERY_TASK_ID, attempt_number=1),
        expected_call(WORKFLOW_RECOVERY_TASK_ID, attempt_number=2),
        expected_call(WORKFLOW_RECOVERY_TASK_ID, attempt_number=3),
        expected_call(TERMINAL_ONLY_WORKFLOW_TASK_ID, terminal_only=True),
        expected_call(TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID, terminal_only=True),
    ]
    assert gate.evidence.workflow_admin_routes == 6
    assert gate.evidence.workflow_admin_actions == 3
    assert gate.evidence.workflow_current_manifests == 1
    assert gate.evidence.workflow_pending_manifests == 0
    assert gate.evidence.workflow_unlinked_pages == 0
    assert gate.evidence.workflow_failure_path_nodes == 2
    assert gate.evidence.workflow_failure_origins == 1
    assert gate.evidence.workflow_failure_incoming_edges == 1
    assert gate.evidence.workflow_failure_admin_routes == 6
    assert gate.evidence.workflow_failure_current_manifests == 1
    assert gate.evidence.workflow_recovery_admin_attempts == 3
    assert gate.evidence.workflow_terminal_only_admin_actions == 0
    assert gate.evidence.workflow_terminal_only_graph_advertised is False
    assert gate.evidence.workflow_terminal_only_storage_rows == 0
    assert gate.evidence.workflow_terminal_only_failure_admin_actions == 0
    assert gate.evidence.workflow_terminal_only_failure_graph_advertised is False
    assert gate.evidence.workflow_terminal_only_failure_storage_rows == 0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("graph_pending_nodes", 4),
        ("graph_succeeded_nodes", 14),
        ("graph_failure_path_nodes", 15),
        ("graph_unavailable_previews", 5),
    ],
)
def test_workflow_admin_gate_rejects_inexact_showcase_failure_projection(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: int,
) -> None:
    gate = LocalKubeRayGate(_config())
    _seed_workflow_admin_evidence(gate)

    def kubectl(*args: str, **kwargs: object) -> CommandResult:
        task_id = args[args.index("--existing-workflow-task-id") + 1]
        if task_id == WORKFLOW_TASK_ID:
            payload = _workflow_admin_smoke_evidence()
        elif task_id == FAILED_WORKFLOW_TASK_ID:
            payload = _workflow_admin_smoke_evidence(
                task_id=FAILED_WORKFLOW_TASK_ID,
                task_state="FAILED",
                topology_nodes=5,
                topology_edges=6,
                pending_nodes=1,
                running_nodes=1,
                succeeded_nodes=2,
                failed_nodes=1,
                failure_path_nodes=2,
                failure_origins=1,
                incoming_failure_edges=1,
            )
        elif task_id == WORKFLOW_SHOWCASE_TASK_ID:
            payload = _workflow_admin_smoke_evidence(
                task_id=WORKFLOW_SHOWCASE_TASK_ID,
                topology_nodes=21,
                topology_edges=28,
                succeeded_nodes=21,
                available_previews=18,
                failed_previews=1,
                preview_contract="showcase-succeeded-verified",
            )
        else:
            payload = _workflow_admin_smoke_evidence(
                task_id=FAILED_WORKFLOW_SHOWCASE_TASK_ID,
                task_state="FAILED",
                topology_nodes=16,
                topology_edges=21,
                pending_nodes=0,
                succeeded_nodes=15,
                failed_nodes=1,
                failure_path_nodes=16,
                failure_origins=1,
                incoming_failure_edges=1,
                available_previews=13,
                failed_previews=1,
                unavailable_previews=1,
                preview_contract="showcase-failed-verified",
            )
            payload[field_name] = value
        return CommandResult(json.dumps(payload), "", 0)

    monkeypatch.setattr(gate, "_kubectl", kubectl)

    with pytest.raises(ValueError, match="did not match API and storage evidence"):
        gate._verify_workflow_admin()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("task_id", TASK_ID),
        ("admin_routes", {"unexpected": 5}),
        ("topology_nodes", 2),
        ("current_manifests", 0),
        ("pending_manifests", 1),
        ("unlinked_pages", 1),
    ],
)
def test_workflow_admin_gate_rejects_non_scalar_or_inconsistent_evidence(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: object,
) -> None:
    gate = LocalKubeRayGate(_config())
    _seed_workflow_admin_evidence(gate)
    payload = _workflow_admin_smoke_evidence()
    payload[field_name] = cast(Any, value)
    monkeypatch.setattr(
        gate,
        "_kubectl",
        lambda *args, **kwargs: CommandResult(json.dumps(payload), "", 0),
    )

    with pytest.raises(ValueError):
        gate._verify_workflow_admin()

    assert gate.evidence.workflow_admin_routes == 0
    assert gate.evidence.workflow_current_manifests == 0


def test_prometheus_uses_safe_opener_and_exact_ray_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.expected_ray_head_count = 1
    gate.expected_ray_worker_count = 4
    observed: dict[str, object] = {}
    identity_checks: list[str] = []
    monkeypatch.setattr(gate, "_verify_ray_identity", lambda: identity_checks.append("checked"))

    def fetch(
        url: str,
        *,
        request_timeout: float,
        opener: object,
    ) -> object:
        observed.update(
            url=url,
            request_timeout=request_timeout,
            opener=opener,
        )
        return {"status": "success"}

    def wait(
        fetcher: Any,
        *,
        timeout: float,
        interval: float,
        expected_counts: dict[str, int],
    ) -> dict[str, int]:
        assert callable(fetcher)
        fetcher()
        assert timeout == 120
        assert interval == 2
        assert expected_counts == {"django-ray": 1, "ray-head": 1, "ray-workers": 4}
        return expected_counts

    monkeypatch.setattr("scripts.local_kuberay_gate.fetch_active_targets", fetch)
    monkeypatch.setattr("scripts.local_kuberay_gate.wait_for_healthy_targets", wait)

    gate._verify_prometheus()

    assert observed == {
        "url": "http://prometheus.localhost:30080",
        "request_timeout": 10,
        "opener": gate.http_opener,
    }
    assert not any(isinstance(handler, ProxyHandler) for handler in gate.http_opener.handlers)
    assert any(isinstance(handler, RejectRedirects) for handler in gate.http_opener.handlers)
    assert gate.evidence.prometheus_counts["ray-workers"] == 4
    assert identity_checks == ["checked", "checked"]


def test_gate_source_has_no_destructive_preservation_shortcuts() -> None:
    source = (ROOT / "scripts/local_kuberay_gate.py").read_text(encoding="utf-8")
    forbidden = (
        '"delete", "namespace"',
        '"delete", "persistentvolumeclaim"',
        '"delete", "pvc"',
        '"delete", "deployment", "postgres"',
        '"docker", "system", "prune"',
        '"docker", "image", "rm"',
        '"docker", "container", "rm"',
    )

    assert all(fragment not in source for fragment in forbidden)


def test_local_memory_and_rendered_docs_are_outside_docker_build_context() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".vault/" in dockerignore
    assert "site/" in dockerignore
    assert "**/.env" in dockerignore
    assert "**/.env.*" in dockerignore
    assert "!.env.example" in dockerignore
    assert dockerignore.index("!.env.example") > dockerignore.index("**/.env.*")
    assert "**/*.sqlite3" in dockerignore
    assert "**/*.sqlite3-*" in dockerignore


def test_dockerfile_specific_context_policies_reexclude_local_sensitive_files() -> None:
    inspect_docker_context_allowlists(ROOT)

    for name, expected in DOCKER_CONTEXT_ALLOWLISTS.items():
        effective = tuple(
            line.strip()
            for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        assert effective == expected
        last_reinclude = max(
            index for index, pattern in enumerate(effective) if pattern.startswith("!")
        )
        for sensitive_pattern in (
            "**/.env",
            "**/.env.*",
            "**/*.sqlite3",
            "**/*.sqlite3-*",
        ):
            assert effective.index(sensitive_pattern) > last_reinclude


def test_evidence_binds_the_stable_source_tree_not_only_the_pre_amend_commit() -> None:
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate._verify_final_identity = lambda: None  # type: ignore[method-assign]
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.kubeconfig_sha256 = "9" * 64
    gate.evidence.kubernetes_server = "https://kubernetes.docker.internal:6443"
    gate.evidence.docker_host = "npipe:////./pipe/dockerDesktopLinuxEngine"
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    gate.evidence.released_v040_image_tag = f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:{TAG}"
    gate.evidence.app_image_id = IMAGE_ID
    gate.evidence.worker_image_id = IMAGE_ID
    gate.evidence.released_v040_image_id = IMAGE_ID
    gate.evidence.setup_bundle_bytes = 1
    gate.evidence.setup_bundle_sha256 = "f" * 64
    gate.evidence.recovery_bundle_bytes = 2
    gate.evidence.recovery_bundle_sha256 = "e" * 64
    gate.evidence.ray_restart = "performed"
    gate.evidence.ray_cluster_uid = "cluster-owner"
    gate.evidence.ray_head_count = 1
    gate.evidence.ray_worker_count = 1
    gate.evidence.deployments = dict.fromkeys(APP_DEPLOYMENTS, 1)
    gate.evidence.task_id = TASK_ID
    gate.evidence.task_state = "SUCCEEDED"
    gate.evidence.task_result = 5
    gate.evidence.api_task_status_bounded = True
    gate.evidence.api_workflow_runtime_polls_bounded = True
    gate.evidence.api_bulk_reset_absent = True
    gate.evidence.api_legacy_workflow_node_absent = True
    gate.evidence.api_execution_delete_rejected = True
    gate.evidence.api_legacy_workflow_graph_absent = True
    gate.evidence.runtime_env_encryption_overlay = True
    gate.evidence.runtime_env_encryption_canary = True
    gate.evidence.runtime_env_encryption_envelope = True
    gate.evidence.runtime_env_encryption_marker_absent = True
    gate.evidence.runtime_env_encryption_tamper_rejected = True
    gate.evidence.runtime_env_encryption_unknown_key_rejected = True
    gate.evidence.runtime_env_encryption_retry_preserved = True
    gate.evidence.runtime_env_encryption_logs_clear = True
    gate.evidence.django_ray_secret_preserved = True
    gate.evidence.ray_job_request_reference_carrier = True
    gate.evidence.ray_job_raw_info_clear = True
    gate.evidence.ray_job_processes_clear = True
    gate.evidence.ray_job_logs_clear = True
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    gate.evidence.ray_job_missing_reference_no_marker = True
    gate.evidence.ray_job_missing_reference_no_retry = True
    gate.evidence.protocol_legacy_cohort_visible = True
    gate.evidence.protocol_explicit_cohort_visible = True
    gate.evidence.protocol_legacy_inline_refused = True
    gate.evidence.protocol_legacy_inline_no_marker = True
    gate.evidence.protocol_legacy_inline_single_attempt = True
    gate.evidence.protocol_v1_queued_survived_handoff = True
    gate.evidence.protocol_v2_queued_unchanged = True
    gate.evidence.protocol_v2_unsupported_visible = True
    gate.evidence.protocol_v2_preinvocation_rejected = True
    gate.evidence.protocol_v2_application_marker_absent = True
    gate.evidence.protocol_v2_target_exact_completed = True
    gate.evidence.protocol_v2_target_mismatch_rejected = True
    gate.evidence.protocol_v2_target_mismatch_marker_absent = True
    gate.evidence.protocol_handoff_cleanup_restored = True
    gate.evidence.workflow_task_id = WORKFLOW_TASK_ID
    gate.evidence.workflow_task_state = "SUCCEEDED"
    gate.evidence.workflow_schema_version = 3
    gate.evidence.workflow_availability = "AVAILABLE"
    gate.evidence.workflow_topology_nodes = 8
    gate.evidence.workflow_topology_edges = 8
    gate.evidence.workflow_node_details = 8
    gate.evidence.workflow_leaf_tasks = 3
    gate.evidence.workflow_admin_routes = 5
    gate.evidence.workflow_admin_actions = 3
    gate.evidence.workflow_current_manifests = 1
    gate.evidence.workflow_pending_manifests = 0
    gate.evidence.workflow_unlinked_pages = 0
    gate.evidence.workflow_terminal_only_task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_task_state = "SUCCEEDED"
    gate.evidence.workflow_terminal_only_attempt_number = 1
    gate.evidence.workflow_terminal_only_schema_version = 3
    gate.evidence.workflow_terminal_only_summary_revision = 1
    gate.evidence.workflow_terminal_only_declared_nodes = 13
    gate.evidence.workflow_terminal_only_declared_edges = 13
    gate.evidence.workflow_terminal_only_admin_actions = 0
    gate.evidence.workflow_terminal_only_graph_advertised = False
    gate.evidence.workflow_terminal_only_storage_rows = 0
    gate.evidence.workflow_terminal_only_failure_task_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_failure_task_state = "FAILED"
    gate.evidence.workflow_terminal_only_failure_attempt_number = 1
    gate.evidence.workflow_terminal_only_failure_schema_version = 3
    gate.evidence.workflow_terminal_only_failure_summary_revision = 1
    gate.evidence.workflow_terminal_only_failure_declared_nodes = 13
    gate.evidence.workflow_terminal_only_failure_declared_edges = 13
    gate.evidence.workflow_terminal_only_failure_admin_actions = 0
    gate.evidence.workflow_terminal_only_failure_graph_advertised = False
    gate.evidence.workflow_terminal_only_failure_storage_rows = 0
    gate.evidence.prometheus_counts = dict.fromkeys(("django-ray", "ray-head", "ray-workers"), 1)

    gate._emit_evidence()

    assert f"source_commit_at_run={COMMIT}" in output
    assert f"source_tree={SOURCE_TREE}" in output


def test_complete_runtime_evidence_block_is_reconstructable_and_bounded() -> None:
    output: list[str] = []
    gate = LocalKubeRayGate(_config(), output=output.append)
    gate._verify_final_identity = lambda: None  # type: ignore[method-assign]
    gate.evidence.commit = COMMIT
    gate.evidence.source_tree = SOURCE_TREE
    gate.evidence.kubeconfig_sha256 = "9" * 64
    gate.evidence.kubernetes_server = "https://localhost:6443/" + ("api/" * 80)
    gate.evidence.docker_host = "unix:///" + ("local-socket-segment/" * 30)
    gate.evidence.app_tag = APP_TAG
    gate.evidence.worker_tag = f"django-ray-worker:{TAG}"
    gate.evidence.released_v040_image_tag = f"{gate_module.RELEASED_V040_IMAGE_REPOSITORY}:{TAG}"
    gate.evidence.app_image_id = IMAGE_ID
    gate.evidence.worker_image_id = IMAGE_ID
    gate.evidence.released_v040_image_id = IMAGE_ID
    gate.evidence.setup_bundle_bytes = 293_956
    gate.evidence.setup_bundle_sha256 = "f" * 64
    gate.evidence.recovery_bundle_bytes = 20_000_000
    gate.evidence.recovery_bundle_sha256 = "e" * 64
    gate.evidence.ray_restart = "performed"
    gate.evidence.ray_cluster_uid = "cluster-owner"
    gate.evidence.ray_pod_identity_sha256 = "8" * 64
    gate.evidence.ray_head_count = 1
    gate.evidence.ray_worker_count = 4
    gate.evidence.deployments = dict.fromkeys(APP_DEPLOYMENTS, 1)
    gate.evidence.task_id = TASK_ID
    gate.evidence.task_state = "SUCCEEDED"
    gate.evidence.task_result = 5
    gate.evidence.api_task_status_bounded = True
    gate.evidence.api_workflow_runtime_polls_bounded = True
    gate.evidence.api_bulk_reset_absent = True
    gate.evidence.api_legacy_workflow_node_absent = True
    gate.evidence.api_execution_delete_rejected = True
    gate.evidence.api_legacy_workflow_graph_absent = True
    gate.evidence.runtime_env_encryption_overlay = True
    gate.evidence.runtime_env_encryption_canary = True
    gate.evidence.runtime_env_encryption_envelope = True
    gate.evidence.runtime_env_encryption_marker_absent = True
    gate.evidence.runtime_env_encryption_tamper_rejected = True
    gate.evidence.runtime_env_encryption_unknown_key_rejected = True
    gate.evidence.runtime_env_encryption_retry_preserved = True
    gate.evidence.runtime_env_encryption_logs_clear = True
    gate.evidence.django_ray_secret_preserved = True
    gate.evidence.ray_job_request_reference_carrier = True
    gate.evidence.ray_job_raw_info_clear = True
    gate.evidence.ray_job_processes_clear = True
    gate.evidence.ray_job_logs_clear = True
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    gate.evidence.ray_job_missing_reference_no_marker = True
    gate.evidence.ray_job_missing_reference_no_retry = True
    gate.evidence.protocol_legacy_cohort_visible = True
    gate.evidence.protocol_explicit_cohort_visible = True
    gate.evidence.protocol_legacy_inline_refused = True
    gate.evidence.protocol_legacy_inline_no_marker = True
    gate.evidence.protocol_legacy_inline_single_attempt = True
    gate.evidence.protocol_v1_queued_survived_handoff = True
    gate.evidence.protocol_v2_queued_unchanged = True
    gate.evidence.protocol_v2_unsupported_visible = True
    gate.evidence.protocol_v2_preinvocation_rejected = True
    gate.evidence.protocol_v2_application_marker_absent = True
    gate.evidence.protocol_v2_target_exact_completed = True
    gate.evidence.protocol_v2_target_mismatch_rejected = True
    gate.evidence.protocol_v2_target_mismatch_marker_absent = True
    gate.evidence.protocol_handoff_cleanup_restored = True
    gate.evidence.workflow_task_id = WORKFLOW_TASK_ID
    gate.evidence.workflow_task_state = "SUCCEEDED"
    gate.evidence.workflow_schema_version = 3
    gate.evidence.workflow_availability = "AVAILABLE"
    gate.evidence.workflow_topology_nodes = 8
    gate.evidence.workflow_topology_edges = 8
    gate.evidence.workflow_node_details = 8
    gate.evidence.workflow_leaf_tasks = 3
    gate.evidence.workflow_admin_routes = 5
    gate.evidence.workflow_admin_actions = 3
    gate.evidence.workflow_current_manifests = 1
    gate.evidence.workflow_pending_manifests = 0
    gate.evidence.workflow_unlinked_pages = 0
    gate.evidence.workflow_terminal_only_task_id = TERMINAL_ONLY_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_task_state = "SUCCEEDED"
    gate.evidence.workflow_terminal_only_attempt_number = 1
    gate.evidence.workflow_terminal_only_schema_version = 3
    gate.evidence.workflow_terminal_only_summary_revision = 1
    gate.evidence.workflow_terminal_only_declared_nodes = 13
    gate.evidence.workflow_terminal_only_declared_edges = 13
    gate.evidence.workflow_terminal_only_admin_actions = 0
    gate.evidence.workflow_terminal_only_graph_advertised = False
    gate.evidence.workflow_terminal_only_storage_rows = 0
    gate.evidence.workflow_terminal_only_failure_task_id = TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    gate.evidence.workflow_terminal_only_failure_task_state = "FAILED"
    gate.evidence.workflow_terminal_only_failure_attempt_number = 1
    gate.evidence.workflow_terminal_only_failure_schema_version = 3
    gate.evidence.workflow_terminal_only_failure_summary_revision = 1
    gate.evidence.workflow_terminal_only_failure_declared_nodes = 13
    gate.evidence.workflow_terminal_only_failure_declared_edges = 13
    gate.evidence.workflow_terminal_only_failure_admin_actions = 0
    gate.evidence.workflow_terminal_only_failure_graph_advertised = False
    gate.evidence.workflow_terminal_only_failure_storage_rows = 0
    gate.evidence.prometheus_counts = {
        "django-ray": 1,
        "ray-head": 1,
        "ray-workers": 4,
    }

    gate._emit_evidence()

    def reconstructed(key: str) -> str:
        direct = f"{key}="
        direct_values = [line.removeprefix(direct) for line in output if line.startswith(direct)]
        if direct_values:
            assert len(direct_values) == 1
            return direct_values[0]
        count_line = next(line for line in output if line.startswith(f"{key}_parts="))
        count = int(count_line.partition("=")[2])
        return "".join(
            next(
                line.partition("=")[2]
                for line in output
                if line.startswith(f"{key}_part_{index:03d}=")
            )
            for index in range(1, count + 1)
        )

    assert reconstructed("kubernetes_server") == gate.evidence.kubernetes_server
    assert reconstructed("docker_host") == gate.evidence.docker_host
    assert reconstructed("app_image_id") == IMAGE_ID
    assert reconstructed("released_v040_image_id") == IMAGE_ID
    assert reconstructed("recovery_runtime_env_bytes") == "20000000"
    assert reconstructed("api_task_status_bounded") == "True"
    assert reconstructed("api_workflow_runtime_polls_bounded") == "True"
    assert reconstructed("api_bulk_reset_absent") == "True"
    assert reconstructed("api_legacy_workflow_node_absent") == "True"
    assert reconstructed("api_execution_delete_rejected") == "True"
    assert reconstructed("api_legacy_workflow_graph_absent") == "True"
    assert reconstructed("runtime_env_encryption_canary") == "True"
    assert reconstructed("runtime_env_encryption_envelope") == "True"
    assert reconstructed("runtime_env_encryption_retry_preserved") == "True"
    assert reconstructed("django_ray_secret_preserved") == "True"
    assert reconstructed("ray_job_request_reference_carrier") == "True"
    assert reconstructed("ray_job_raw_info_clear") == "True"
    assert reconstructed("ray_job_processes_clear") == "True"
    assert reconstructed("ray_job_logs_clear") == "True"
    assert reconstructed("ray_job_manager_reconciled_same_job") == "True"
    assert reconstructed("ray_job_manager_no_resubmit") == "True"
    assert reconstructed("ray_job_missing_reference_no_marker") == "True"
    assert reconstructed("ray_job_missing_reference_no_retry") == "True"
    assert reconstructed("protocol_legacy_cohort_visible") == "True"
    assert reconstructed("protocol_explicit_cohort_visible") == "True"
    assert reconstructed("protocol_legacy_inline_refused") == "True"
    assert reconstructed("protocol_legacy_inline_no_marker") == "True"
    assert reconstructed("protocol_legacy_inline_single_attempt") == "True"
    assert reconstructed("protocol_v1_queued_survived_handoff") == "True"
    assert reconstructed("protocol_v2_queued_unchanged") == "True"
    assert reconstructed("protocol_v2_unsupported_visible") == "True"
    assert reconstructed("protocol_v2_preinvocation_rejected") == "True"
    assert reconstructed("protocol_v2_application_marker_absent") == "True"
    assert reconstructed("protocol_v2_target_exact_completed") == "True"
    assert reconstructed("protocol_v2_target_mismatch_rejected") == "True"
    assert reconstructed("protocol_v2_target_mismatch_marker_absent") == "True"
    assert reconstructed("protocol_handoff_cleanup_restored") == "True"
    assert reconstructed("workflow_task_id") == WORKFLOW_TASK_ID
    assert reconstructed("workflow_availability") == "AVAILABLE"
    assert reconstructed("workflow_terminal_only_task_id") == TERMINAL_ONLY_WORKFLOW_TASK_ID
    assert reconstructed("workflow_showcase_task_id") == ""
    assert (
        reconstructed("workflow_terminal_only_failure_task_id")
        == TERMINAL_ONLY_FAILED_WORKFLOW_TASK_ID
    )
    encryption_evidence = "\n".join(
        line
        for line in output
        if line.startswith(("runtime_env_encryption_", "django_ray_secret_preserved="))
    )
    for forbidden in ("task_id", "sha256", "key_id", "nonce", "ciphertext", "envelope={"):
        assert forbidden not in encryption_evidence
    ray_job_evidence = "\n".join(line for line in output if line.startswith("ray_job_"))
    assert "locator" not in ray_job_evidence
    assert re.search(r"[0-9a-f]{64}", ray_job_evidence) is None
    protocol_evidence = "\n".join(line for line in output if line.startswith("protocol_"))
    for forbidden in (
        "task_id",
        "job_id",
        "request_ref",
        "request_bytes",
        "protocol_v2_application_poison_",
        TASK_ID,
    ):
        assert forbidden not in protocol_evidence
    assert re.search(r"[0-9a-f]{64}", protocol_evidence) is None
    assert all(len(line) <= EVIDENCE_LINE_LIMIT for line in output)


def test_guidance_requires_concise_semantic_gate_summaries() -> None:
    guidance = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in ("AGENTS.md", "CONTRIBUTING.md", "docs/contributing.md")
    }

    for path, content in guidance.items():
        normalized = " ".join(content.split())
        assert "local-kuberay-gate.md" in normalized, path
        assert "concise semantic validation summary" in normalized, path
        assert "exact gate command" in normalized, path
        assert "cold-Ray decision" in normalized, path
        assert "source-tree match" in normalized, path
        assert "complete secret-free evidence block" in normalized, path
        assert "runtime diagnostics" in normalized, path
        assert "clean checkout" in normalized, path
        assert "do not paste" in normalized, path

    combined = "\n".join(guidance.values())
    assert "copy the command's complete secret-free evidence block" not in combined
    assert "Record the complete secret-free evidence block" not in combined


def test_gate_guide_separates_runtime_evidence_from_durable_summary() -> None:
    guide = (ROOT / "docs/deployment/local-kuberay-gate.md").read_text(encoding="utf-8")

    assert "## Runtime evidence and durable validation summary" in guide
    assert "=== Local KubeRay final gate evidence ===" in guide
    assert "Do not copy the complete block into a retained commit or PR" in guide
    assert "exact `uv run make k8s-final-gate` command and arguments" in guide
    assert "`K8S_RAY_RESTART=required`: passed" in guide
    assert "authenticated API smoke" in guide
    assert "task succeeded with result 5" in guide
    assert "`workflow-progress`" in guide
    assert "`workflow-admin`" in guide
    assert "`runtime-env-encryption`" in guide
    assert "storage_encryption_verified=true" in guide
    assert "corrupt and unknown-key rows failed before Ray" in guide
    assert "deterministic first-attempt failure" in guide
    assert "authenticated admin graph retained the incoming" in guide
    assert "all Ray pods were cold-replaced" in guide
    assert "data-bearing resources were preserved" in guide
    assert "focused value or artifact in an issue or PR comment" in guide
    assert "without copying the tree hash into history" in guide
    assert "complete block can be copied into a commit" not in guide


def test_gate_document_retains_trigger_matrix_reference_evidence_and_preservation() -> None:
    guide = (ROOT / "docs/deployment/local-kuberay-gate.md").read_text(encoding="utf-8")
    normalized = " ".join(guide.split())

    assert "## Trigger matrix" in guide
    assert "Required" in guide
    assert "Recommended" in guide
    assert "Not applicable" in guide
    assert "#102" in guide
    assert "Issues #144, #145, #146, and #147" in guide
    assert "PRs #148, #149, #150, and #151" in guide
    assert "1cef8e6042ed0fe811cc9ee99b8332a75c887c75" in guide
    assert "PostgreSQL" in guide and "PVC" in guide
    assert "source_tree" in guide and "git rev-parse HEAD^{tree}" in guide
    assert "Do not automate token retrieval into browser logs" in normalized
    assert "private, flattened kubeconfig snapshot" in guide
    assert "same one-time archive" in guide
    assert "checkout fetched without tags remains valid" in normalized
    assert "when the optional tag ref is present" in normalized
    assert "32-512 characters from the Bearer" in guide
    assert "`token68` alphabet" in guide
    assert "exact desired, updated, ready, and available replicas" in guide
    assert "Deployment UID -> current ReplicaSet UID -> Pod UID" in guide
    assert "deadline-bounded convergence barrier" in guide
    assert "zero-replica historical ReplicaSets" in guide
    assert "`app-convergence`" in guide
    assert "UID/container/image identity-set SHA-256" in guide
    assert "sanitized environments" in guide
    assert "rechecked before and after Prometheus" in guide
    assert "loopback-forward pair" in guide
    assert "`http://127.0.0.1:30090`" in guide
    assert "K8S_PROMETHEUS_URL=http://prometheus.localhost:30080" not in guide
    assert "Each emitted line is at most 72 characters" in guide
    assert "key_part_001" in guide
    assert (
        "RuntimeEnv snapshot storage or maintenance, encryption settings or dependencies" in guide
    )
    assert "Ray Job request encoding, request-reference storage" in guide
    assert "11 running workload pods with 3.3 CPU and 5,056 MiB requested" in normalized
    assert "four task-manager Deployments" in normalized
    assert "`ray-job-request-reference`" in guide
    assert "exact canonical locator digest" in normalized
    assert "with no selector in an init container, shared ConfigMap, setup Job, or Ray pod" in guide
    assert "task IDs, hashes, key IDs, nonces, ciphertext, or envelopes" in guide
    assert "full base64 data mapping of every component Secret" in guide
    encryption_row = next(
        line for line in guide.splitlines() if line.startswith("| RuntimeEnv snapshot storage")
    )
    assert "| Required | `required` |" in encryption_row
    assert (
        "bounded plaintext rewrite and old-key rewrap with identity preservation" in encryption_row
    )
    assert "current-key worker execution, plaintext execution/retry refusal" in encryption_row
    assert "owned cleanup before and after cold replacement" in encryption_row
    assert "current-table checks do not qualify backup key retirement" in encryption_row


def test_make_gate_requires_explicit_context_namespace_and_ray_decision() -> None:
    makefile = (ROOT / "mk/k8s.mk").read_text(encoding="utf-8")

    assert "k8s-final-gate-preflight:" in makefile
    assert "k8s-final-gate:" in makefile
    assert '--context "$(K8S_CONTEXT)"' in makefile
    assert '--namespace "$(K8S_NAMESPACE)"' in makefile
    assert '--ray-restart "$(K8S_RAY_RESTART)"' in makefile
    assert "--preflight-only" in makefile
    assert "if [ -z" not in makefile
    assert "$(if $(strip $(K8S_CONTEXT))" in makefile
    assert "$(if $(strip $(K8S_RAY_RESTART))" in makefile


@pytest.mark.parametrize("target", ["k8s-final-gate-preflight", "k8s-final-gate"])
def test_make_gate_wrapper_expands_on_the_host_shell(target: str) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository wrapper")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            target,
            "K8S_CONTEXT=docker-desktop",
            f"K8S_NAMESPACE={EXPECTED_NAMESPACE}",
            "K8S_RAY_RESTART=required",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "python -m scripts.local_kuberay_gate" in result.stdout
    assert '--web-url "http://127.0.0.1:30080"' in result.stdout
    assert '--prometheus-url "http://127.0.0.1:30090"' in result.stdout
    assert "if [ -z" not in result.stdout


def test_make_gate_wrapper_rejects_a_missing_context_before_python() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is required to exercise the repository wrapper")

    result = subprocess.run(
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "k8s-final-gate-preflight",
            f"K8S_NAMESPACE={EXPECTED_NAMESPACE}",
            "K8S_RAY_RESTART=required",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "K8S_CONTEXT is required" in result.stderr
    assert "python -m scripts.local_kuberay_gate" not in result.stdout
