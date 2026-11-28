"""Unit tests for local KubeRay gate protocol v1 survival and handoff certification."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import local_kuberay_gate as gate_module
from scripts.local_kuberay_gate import (
    LocalKubeRayGate,
)
from tests.unit._local_kuberay_gate_helpers import (
    PRIOR_RELEASED_V040_WORKER_ID,
    PROTOCOL_V1_SURVIVAL_MARKER,
    PROTOCOL_V1_SURVIVAL_TASK_ID,
    PROTOCOL_V2_POISON,
    TASK_ID,
    _config,
    _protocol_v2_rejection_payload,
    _protocol_v2_target_execution_payload,
    _task_status_payload,
)


def _protocol_v1_survival_fixture() -> dict[str, object]:
    return {
        "pk": 51,
        "task_id": PROTOCOL_V1_SURVIVAL_TASK_ID,
        "marker": PROTOCOL_V1_SURVIVAL_MARKER,
        "row_sha256": "c" * 64,
        "state": "QUEUED",
        "execution_protocol_version": 1,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "attempt_number": 1,
        "execution_generation": 0,
        "run_after": "2026-08-14T21:00:00+00:00",
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_job_request_reference": None,
    }


def _protocol_v1_survival_observation() -> dict[str, object]:
    fixture = _protocol_v1_survival_fixture()
    return {
        **fixture,
        "callable_path": "testproject.tasks.echo_task",
        "args_json": json.dumps([fixture["marker"]], separators=(",", ":")),
        "kwargs_json": "{}",
        "ray_address": None,
    }


def test_protocol_v1_survival_enqueue_is_deferred_on_the_live_ray_data_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = _protocol_v1_survival_fixture()
    observed: dict[str, str] = {}
    marker_uuid = gate_module.UUID("22222222-2222-4222-8222-222222222222")

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v1-survival-enqueue>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return fixture

    monkeypatch.setattr(gate_module, "uuid4", lambda: marker_uuid)
    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._enqueue_protocol_v1_survival_task() == fixture
    assert gate._protocol_v1_survival_fixture == fixture
    assert observed["field_name"] == "protocol-v1 deferred survival enqueue"
    assert f"marker = {PROTOCOL_V1_SURVIVAL_MARKER!r}" in observed["script"]
    assert f"timedelta(seconds={float(gate.config.task_timeout * 2)!r})" in observed["script"]
    assert f"backend={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert "run_after=run_after" in observed["script"]
    assert ".enqueue(marker)" in observed["script"]


def test_protocol_v1_survival_requires_the_same_unclaimed_queued_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = _protocol_v1_survival_fixture()
    observation = _protocol_v1_survival_observation()
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v1_survival_task",
        lambda _fixture: observation,
    )

    assert gate._require_protocol_v1_survival_unchanged(fixture) == observation

    observation["claimed_by_worker"] = "released-manager-claimed-the-deferred-row"
    with pytest.raises(ValueError, match="changed during manager handoff"):
        gate._require_protocol_v1_survival_unchanged(fixture)


def _execute_generated_django_shell(
    script: str,
    capsys: pytest.CaptureFixture[str],
    *,
    filename: str,
) -> dict[str, Any]:
    """Execute a generated Django shell script against the real test database."""
    exec(compile(script, filename, "exec"), {})
    output = capsys.readouterr().out.strip()
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.django_db
def test_protocol_v1_survival_release_is_exactly_one_unchanged_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import UTC, datetime

    from django_ray.models import RayTaskExecution, TaskState

    initial_run_after = datetime(2026, 8, 14, 21, 0, 0, tzinfo=UTC)
    row = RayTaskExecution.objects.create(
        task_id=PROTOCOL_V1_SURVIVAL_TASK_ID,
        callable_path="testproject.tasks.echo_task",
        args_json=json.dumps([PROTOCOL_V1_SURVIVAL_MARKER], separators=(",", ":")),
        kwargs_json="{}",
        queue_name=gate_module.RAY_JOB_GATE_QUEUE,
        state=TaskState.QUEUED,
        execution_protocol_version=1,
        attempt_number=1,
        execution_generation=0,
        run_after=initial_run_after,
    )
    decoy = RayTaskExecution.objects.create(
        task_id="33333333-3333-4333-8333-333333333333",
        callable_path="testproject.tasks.echo_task",
        args_json=json.dumps([PROTOCOL_V1_SURVIVAL_MARKER], separators=(",", ":")),
        kwargs_json="{}",
        queue_name=gate_module.RAY_JOB_GATE_QUEUE,
        state=TaskState.QUEUED,
        execution_protocol_version=1,
        attempt_number=1,
        execution_generation=0,
        run_after=initial_run_after,
    )
    fixture = {
        **_protocol_v1_survival_fixture(),
        "pk": row.pk,
        "run_after": initial_run_after.isoformat(),
    }
    gate = LocalKubeRayGate(_config())
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        observed["field_name"] = field_name
        return _execute_generated_django_shell(
            script, capsys, filename="<protocol-v1-survival-release>"
        )

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    released = gate._release_protocol_v1_survival_task(fixture)

    row.refresh_from_db()
    decoy.refresh_from_db()
    assert observed["field_name"] == "protocol-v1 deferred survival release"
    assert released["matched"] == 1
    assert released["updated"] == 1
    assert released["pk"] == row.pk
    assert released["task_id"] == PROTOCOL_V1_SURVIVAL_TASK_ID
    assert released["state"] == "QUEUED"
    assert row.run_after is not None
    assert row.run_after != initial_run_after
    assert row.run_after.isoformat() == released["released_at"]
    assert decoy.run_after == initial_run_after

    # Releasing again after the row has already been claimed or modified must fail closed.
    RayTaskExecution.objects.filter(pk=row.pk).update(
        run_after=initial_run_after,
        claimed_by_worker="unexpected-worker",
    )
    with pytest.raises(ValueError, match="not released exactly once"):
        gate._release_protocol_v1_survival_task(fixture)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("initial_state", "expected_deleted"),
    [
        ("QUEUED", 1),
        ("RUNNING", 0),
    ],
)
def test_protocol_v1_survival_failure_cleanup_deletes_only_an_unclaimed_queued_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    initial_state: str,
    expected_deleted: int,
) -> None:
    from django_ray.models import RayTaskExecution

    row = RayTaskExecution.objects.create(
        task_id=PROTOCOL_V1_SURVIVAL_TASK_ID,
        callable_path="testproject.tasks.echo_task",
        args_json=json.dumps([PROTOCOL_V1_SURVIVAL_MARKER], separators=(",", ":")),
        kwargs_json="{}",
        queue_name=gate_module.RAY_JOB_GATE_QUEUE,
        state=initial_state,
        execution_protocol_version=1,
        attempt_number=1,
        execution_generation=0,
    )
    fixture = {**_protocol_v1_survival_fixture(), "pk": row.pk}
    gate = LocalKubeRayGate(_config())
    gate._protocol_v1_survival_fixture = fixture
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        observed["field_name"] = field_name
        return _execute_generated_django_shell(
            script, capsys, filename="<protocol-v1-survival-cleanup>"
        )

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    cleanup = gate._cleanup_protocol_v1_survival_task(fixture)

    assert gate._protocol_v1_survival_fixture is None
    assert observed["field_name"] == "protocol-v1 deferred survival cleanup"
    assert cleanup["matched"] == 1
    assert cleanup["deleted"] == expected_deleted
    assert cleanup["states"] == [initial_state]
    assert cleanup["queued_absent"] is True
    assert RayTaskExecution.objects.filter(pk=row.pk).exists() is (expected_deleted == 0)


def test_protocol_v2_fixture_is_seeded_only_on_the_live_ray_data_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    gate._protocol_handoff_initial_revision = 7
    observed: dict[str, str] = {}
    poison_uuid = gate_module.UUID("01234567-89ab-4cde-8fab-0123456789ab")
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": f"protocol_v2_application_poison_{poison_uuid.hex}",
        "row_sha256": "d" * 64,
        "created_at": "2026-08-14T20:00:00+00:00",
        "queued_at": "2026-08-14T20:00:01+00:00",
        "initial_revision": 7,
        "closed_revision": 8,
    }
    generated_ids = iter((gate_module.UUID(TASK_ID), poison_uuid))

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-fixture>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return fixture

    monkeypatch.setattr(gate_module, "uuid4", lambda: next(generated_ids))
    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._seed_protocol_v2_fixture() == {**fixture, "seed_confirmed": True}
    assert observed["field_name"] == "protocol-v2 queued fixture creation"
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert f"task_id = {TASK_ID!r}" in observed["script"]
    assert f"poison = {fixture['poison']!r}" in observed["script"]
    assert "execution_protocol_version=2" in observed["script"]
    assert "state=TaskState.QUEUED" in observed["script"]
    assert "close_legacy_worker_admission" in observed["script"]
    created_terminal = observed["script"].index("state=TaskState.FAILED")
    admission_closed = observed["script"].index("transition = close_legacy_worker_admission")
    update_started = observed["script"].index("updated = RayTaskExecution.objects.filter(")
    queued = observed["script"].index(
        ".update(state=TaskState.QUEUED)",
        update_started,
    )
    update_verified = observed["script"].index("if updated != 1:", queued)
    queued_at_captured = observed["script"].index(
        "queued_at = timezone.now()",
        update_verified,
    )
    row_refreshed = observed["script"].index("row.refresh_from_db()", queued_at_captured)
    emitted_hash = observed["script"].index(
        '"row_sha256": row_sha256(row)',
        row_refreshed,
    )
    emitted_queued_at = observed["script"].index(
        '"queued_at": queued_at.isoformat()',
        emitted_hash,
    )
    assert (
        created_terminal
        < admission_closed
        < update_started
        < queued
        < update_verified
        < queued_at_captured
        < row_refreshed
        < emitted_hash
        < emitted_queued_at
    )


def test_protocol_v2_startup_recovery_terminalizes_before_reopen_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    recovered = {
        "recovered": True,
        "matched": 1,
        "terminalized": 1,
        "deleted": 1,
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 9,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-startup-recovery>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return recovered

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._recover_protocol_v2_startup_fixture() == recovered
    assert observed["field_name"] == "protocol-v2 startup fixture recovery"
    assert (
        'callable_path__startswith="testproject.tasks.protocol_v2_application_poison_"'
        in observed["script"]
    )
    assert "rows = list(reserved[:2])" in observed["script"]
    terminalized = observed["script"].index(".update(state=TaskState.FAILED)")
    admission_reopened = observed["script"].index("transition = reopen_legacy_worker_admission")
    deleted = observed["script"].index("deleted, _ = exact_terminal.delete()")
    assert terminalized < admission_reopened < deleted


def test_protocol_v2_startup_recovery_accepts_a_terminal_staged_hard_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    recovered = {
        "recovered": True,
        "matched": 1,
        "terminalized": 0,
        "deleted": 1,
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 7,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-terminal-stage-recovery>", "exec")
        observed["script"] = script
        return recovered

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._recover_protocol_v2_startup_fixture() == recovered
    assert "row.state in [TaskState.FAILED, TaskState.QUEUED]" in observed["script"]
    assert (
        "if policy.legacy_worker_admission_enabled and row.state != TaskState.FAILED"
        in observed["script"]
    )
    assert "terminalized = 0" in observed["script"]
    assert "deleted, _ = exact_terminal.delete()" in observed["script"]


def test_protocol_handoff_recovery_orders_both_exact_cleanups_before_manager_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    events: list[str] = []
    managers = iter(
        (
            {"replicas": 0, "ready_replicas": 0},
            {"replicas": 1, "ready_replicas": 1},
        )
    )
    monkeypatch.setattr(
        gate,
        "_recover_released_v040_startup_residue",
        lambda: events.append("recover-released-v040"),
    )
    monkeypatch.setattr(
        gate,
        "_recover_protocol_v2_startup_fixture",
        lambda: events.append("recover-protocol-v2"),
    )

    def observe_manager() -> Mapping[str, int]:
        observation = next(managers)
        events.append(f"manager:{observation['replicas']}/{observation['ready_replicas']}")
        return observation

    monkeypatch.setattr(gate, "_ray_job_manager_replica_observation", observe_manager)
    monkeypatch.setattr(
        gate,
        "_scale_ray_job_manager",
        lambda replicas: events.append(f"scale-manager:{replicas}"),
    )
    monkeypatch.setattr(
        gate,
        "_wait_for_application_topology",
        lambda: events.append("app-ready"),
    )

    gate._recover_protocol_handoff_residue()

    assert events == [
        "recover-released-v040",
        "recover-protocol-v2",
        "manager:0/0",
        "scale-manager:1",
        "app-ready",
        "manager:1/1",
    ]


def test_protocol_v2_survival_rejects_any_queue_or_claim_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {"pk": 41, "task_id": TASK_ID, "row_sha256": "d" * 64}
    observation = {
        "row_sha256": fixture["row_sha256"],
        "state": "QUEUED",
        "execution_protocol_version": 2,
        "queue_name": "protocol-v2-gate",
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_address": None,
        "ray_data_explicit_lease_count": 1,
        "post_queue_heartbeat": True,
    }
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: observation,
    )

    with pytest.raises(ValueError, match="queued row changed"):
        gate._wait_for_protocol_v2_survival(fixture)

    observation["queue_name"] = gate_module.RAY_JOB_GATE_QUEUE
    observation["claimed_by_worker"] = "unexpected-v1-manager"
    with pytest.raises(ValueError, match="queued row changed"):
        gate._wait_for_protocol_v2_survival(fixture)


def test_protocol_v2_survival_waits_for_a_strictly_post_queue_manager_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {"pk": 41, "task_id": TASK_ID, "row_sha256": "d" * 64}
    base = {
        "row_sha256": fixture["row_sha256"],
        "state": "QUEUED",
        "execution_protocol_version": 2,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_address": None,
        "ray_data_explicit_lease_count": 1,
    }
    observations = iter(
        (
            {**base, "post_queue_heartbeat": False},
            {**base, "post_queue_heartbeat": True},
        )
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: next(observations),
    )
    monkeypatch.setattr(gate_module.time, "sleep", sleeps.append)

    result = gate._wait_for_protocol_v2_survival(fixture)

    assert result == {**base, "post_queue_heartbeat": True}
    assert sleeps == [2]


def test_protocol_v2_visibility_requires_unchanged_status_api_report_and_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "row_sha256": "d" * 64,
    }
    row = {
        "row_sha256": fixture["row_sha256"],
        "state": "QUEUED",
        "execution_protocol_version": 2,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "claimed_by_worker": None,
        "ray_job_id": None,
        "ray_address": None,
        "ray_data_explicit_lease_count": 1,
        "post_queue_heartbeat": True,
    }
    task_status = _task_status_payload(state="QUEUED", task_id=TASK_ID)
    task_status.update(
        {
            "execution_protocol_version": 2,
            "managed_with_django_ray_version": None,
            "executor_django_ray_version": None,
            "protocol_compatible_worker_available": False,
        }
    )
    paths: list[str] = []

    def http(path: str, **_kwargs: object) -> tuple[int, bytes]:
        paths.append(path)
        if path.startswith("/api/tasks/"):
            return 200, json.dumps(task_status).encode()
        if path.startswith("/api/executions?"):
            return 200, json.dumps({"tasks": [task_status]}).encode()
        raise AssertionError(f"unexpected protocol-v2 HTTP path: {path}")

    report = {
        "policy": {
            "legacy_worker_admission_enabled": False,
            "legacy_admission_token_present": False,
            "active_write_protocol_version": 1,
        },
        "non_v1_nonterminal_count": 1,
        "unsupported_work": {
            "groups": [
                {
                    "count": 1,
                    "execution_protocol_version": 2,
                    "queue": gate_module.RAY_JOB_GATE_QUEUE,
                    "state": "QUEUED",
                }
            ],
            "total_tasks": 1,
            "omitted_tasks": 0,
        },
    }
    monkeypatch.setattr(gate, "_wait_for_protocol_v2_survival", lambda _fixture: row)
    monkeypatch.setattr(gate, "_secret_token", lambda: "local-token")
    monkeypatch.setattr(gate, "_http", http)
    monkeypatch.setattr(gate, "_observe_protocol_cohorts", lambda: {"report": report})
    monkeypatch.setattr(
        gate,
        "_protocol_metric_counts",
        lambda: {("other", "QUEUED"): 1},
    )
    monkeypatch.setattr(gate, "_observe_protocol_v2_fixture", lambda _fixture: row)

    gate._verify_protocol_v2_visibility(
        fixture,
        baseline_metrics={("other", "QUEUED"): 0},
    )

    assert paths == [
        f"/api/tasks/{TASK_ID}",
        f"/api/executions?task_id={TASK_ID}&limit=1",
    ]
    assert gate.evidence.protocol_v2_queued_unchanged is True
    assert gate.evidence.protocol_v2_unsupported_visible is True


def test_protocol_v2_direct_probe_rejects_before_input_or_application_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-direct-rejection>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return _protocol_v2_rejection_payload()

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": fixture["row_sha256"]},
    )

    assert gate._verify_protocol_v2_rejection(fixture) == _protocol_v2_rejection_payload()
    assert observed["field_name"] == "protocol-v2 direct Ray Core rejection"
    assert "input_reference=None" in observed["script"]
    assert 'request["execution_protocol_version"] = 2' in observed["script"]
    assert "expected_execution_protocol_version=2" in observed["script"]
    assert "ExecutionProtocolRange(minimum=1, maximum=2)" in observed["script"]
    assert "application_marker_present" in observed["script"]
    assert "from testproject" not in observed["script"]
    assert gate.evidence.protocol_v2_preinvocation_rejected is True
    assert gate.evidence.protocol_v2_application_marker_absent is True


def test_protocol_v2_direct_probe_rejects_any_durable_row_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    monkeypatch.setattr(
        gate,
        "_sensitive_django_shell",
        lambda _script, *, field_name, ray_authorized: _protocol_v2_rejection_payload(),
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": "e" * 64},
    )

    with pytest.raises(ValueError, match="changed during direct rejection"):
        gate._verify_protocol_v2_rejection(fixture)

    assert gate.evidence.protocol_v2_preinvocation_rejected is False
    assert gate.evidence.protocol_v2_application_marker_absent is False


def test_protocol_v2_private_target_probe_proves_exact_and_mismatch_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-private-target-execution>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return _protocol_v2_target_execution_payload()

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": fixture["row_sha256"]},
    )

    assert (
        gate._verify_protocol_v2_target_execution(fixture)
        == _protocol_v2_target_execution_payload()
    )
    assert observed["field_name"] == "protocol-v2 private target execution"
    assert "runner._submit_target_execution(" in observed["script"]
    assert "runner._poll_target_execution_results(" in observed["script"]
    assert "probe_ray_target(" in observed["script"]
    assert "ray_minor=60," in observed["script"]
    assert "build_ray_cluster_attestation(" in observed["script"]
    assert "RayTaskTargetExecutionEvidenceClaim(" in observed["script"]
    assert "ray_task_target_execution_evidence_digest(" in observed["script"]
    assert "target_execution_evidence_claim=exact_evidence_claim" in observed["script"]
    assert "claim_attestation_recorded_at=claim_attestation.observed_at" in observed["script"]
    assert "target_execution_evidence_digest=" not in observed["script"]
    assert "target_execution_claimed_at=" not in observed["script"]
    assert "manager_python_patch=expected.runtime.python_patch" in observed["script"]
    assert "route_selection_id=41" in observed["script"]
    assert 'execution.state = "RUNNING"' in observed["script"]
    assert (
        'execution.claimed_by_worker = "local-kuberay-private-p2-core-lease"' in observed["script"]
    )
    assert "execution.started_at = exact_claimed_at" in observed["script"]
    assert "execution.finished_at = None" in observed["script"]
    assert "python_patch=runtime.python_patch + 1" in observed["script"]
    assert 'execution.callable_path = "testproject.tasks.echo_task"' in observed["script"]
    assert "RayCoreTargetExecutionTransportState.COMPATIBILITY_REJECTION" in observed["script"]
    assert "mismatch_marker_present" in observed["script"]
    assert "from testproject" not in observed["script"]
    assert gate.evidence.protocol_v2_target_exact_completed is True
    assert gate.evidence.protocol_v2_target_mismatch_rejected is True
    assert gate.evidence.protocol_v2_target_mismatch_marker_absent is True


def test_protocol_v2_private_target_probe_rejects_any_durable_row_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "poison": PROTOCOL_V2_POISON,
        "row_sha256": "d" * 64,
    }
    monkeypatch.setattr(
        gate,
        "_sensitive_django_shell",
        lambda _script, *, field_name, ray_authorized: _protocol_v2_target_execution_payload(),
    )
    monkeypatch.setattr(
        gate,
        "_observe_protocol_v2_fixture",
        lambda _fixture: {"row_sha256": "e" * 64},
    )

    with pytest.raises(ValueError, match="changed during private target execution"):
        gate._verify_protocol_v2_target_execution(fixture)

    assert gate.evidence.protocol_v2_target_exact_completed is False
    assert gate.evidence.protocol_v2_target_mismatch_rejected is False
    assert gate.evidence.protocol_v2_target_mismatch_marker_absent is False


def test_protocol_v2_cleanup_deletes_only_the_fixture_and_reopens_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "initial_revision": 7,
        "closed_revision": 8,
        "seed_confirmed": True,
    }
    cleanup = {
        "matched": 1,
        "terminalized": 1,
        "deleted": 1,
        "protocols": [2],
        "states": ["QUEUED"],
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 9,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-cleanup>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return cleanup

    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._cleanup_protocol_v2_fixture(fixture) == cleanup
    assert observed["field_name"] == "protocol-v2 fixture and policy cleanup"
    assert f"expected_pk = {fixture['pk']!r}" in observed["script"]
    assert "queryset = queryset.filter(pk=expected_pk)" in observed["script"]
    assert f"task_id={fixture['task_id']!r}" in observed["script"]
    assert "execution_protocol_version" in observed["script"]
    assert "reopen_legacy_worker_admission" in observed["script"]
    terminalized = observed["script"].index(
        "terminalized = queryset.filter(state=TaskState.QUEUED).update(state=TaskState.FAILED)"
    )
    admission_reopened = observed["script"].index("transition = reopen_legacy_worker_admission")
    deleted = observed["script"].index("deleted, _ = terminal_queryset.delete()")
    assert terminalized < admission_reopened < deleted


def test_protocol_v2_cleanup_rejects_inexact_deletion_or_policy_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "initial_revision": 7,
        "closed_revision": 8,
        "seed_confirmed": True,
    }
    monkeypatch.setattr(
        gate,
        "_sensitive_django_shell",
        lambda _script, *, field_name: {
            "matched": 0,
            "deleted": 0,
            "protocols": [],
            "row_absent": False,
            "schema_version": 1,
            "active_write_protocol_version": 1,
            "legacy_worker_admission_enabled": False,
            "revision": 8,
            "token_count": 0,
        },
    )

    with pytest.raises(ValueError, match="not restored exactly"):
        gate._cleanup_protocol_v2_fixture(fixture)


def test_protocol_v2_uncertain_cleanup_cannot_delete_a_task_id_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    fixture = {
        "pk": None,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "poison": PROTOCOL_V2_POISON,
        "initial_revision": 7,
        "closed_revision": 8,
        "seed_confirmed": False,
    }
    cleanup = {
        "matched": 0,
        "terminalized": 0,
        "deleted": 0,
        "protocols": [],
        "states": [],
        "row_absent": True,
        "schema_version": 1,
        "active_write_protocol_version": 1,
        "legacy_worker_admission_enabled": True,
        "revision": 9,
        "token_count": 1,
    }
    observed: dict[str, str] = {}

    def sensitive_shell(
        script: str, *, field_name: str, ray_authorized: bool = False
    ) -> Mapping[str, Any]:
        compile(script, "<protocol-v2-collision-cleanup>", "exec")
        observed["script"] = script
        observed["field_name"] = field_name
        return cleanup

    gate._protocol_v2_fixture = fixture
    monkeypatch.setattr(gate, "_sensitive_django_shell", sensitive_shell)

    assert gate._cleanup_protocol_v2_fixture(fixture) == cleanup
    assert gate._protocol_v2_fixture is None
    assert observed["field_name"] == "protocol-v2 fixture and policy cleanup"
    assert f"task_id={TASK_ID!r}" in observed["script"]
    assert "execution_protocol_version=2" in observed["script"]
    assert f"queue_name={gate_module.RAY_JOB_GATE_QUEUE!r}" in observed["script"]
    assert 'callable_path="testproject.tasks." + poison' in observed["script"]
    assert 'args_json=json.dumps([poison], separators=(",", ":"))' in observed["script"]
    assert 'kwargs_json="{}"' in observed["script"]
    assert '"row_absent": not queryset.exists()' in observed["script"]


def _protocol_policy_observation(revision: int) -> dict[str, object]:
    return {
        "report": {
            "policy": {
                "schema_version": 1,
                "active_write_protocol_version": 1,
                "legacy_worker_admission_enabled": True,
                "legacy_admission_token_present": True,
                "revision": revision,
            }
        },
        "legacy_worker_count": 0,
        "legacy_worker_ids": [],
    }


def _protocol_handoff_observation(
    *,
    worker_id: str,
    job_id: str,
    state: str = "FAILED",
    durable_state: str = "FAILED",
) -> dict[str, object]:
    return {
        "ready": True,
        "state": state,
        "durable_state": durable_state,
        "attempt_number": 1,
        "execution_generation": 4,
        "worker_id": worker_id,
        "job_id": job_id,
        "released_carrier": True,
        "request_reference_absent": True,
        "submission_count": 1,
        "driver_exit_code": 78,
        "fixed_refusal": True,
        "callable_marker_bound": True,
        "marker_absent": True,
        "completion_is_none": True,
        "result_is_none": True,
        "run_after_is_none": True,
        "single_failed_attempt": True,
    }


def _protocol_v1_survival_terminal(job_id: str) -> dict[str, object]:
    return {
        "ready": True,
        "state": "SUCCEEDED",
        "durable_state": "SUCCEEDED",
        "attempt_number": 1,
        "execution_generation": 1,
        "worker_id": "current-explicit-worker",
        "job_id": job_id,
        "carrier_ok": True,
        "binding_ok": True,
        "request_ok": True,
        "info_clear": True,
        "logs_clear": True,
        "submission_count": 1,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "SUCCEEDED"),
        ("durable_state", "QUEUED"),
        ("worker_id", "other-worker"),
        ("job_id", "other-job"),
        ("execution_generation", -1),
        ("attempt_number", 2),
        ("attempt_number", True),
        ("submission_count", 2),
        ("driver_exit_code", 1),
        ("released_carrier", False),
        ("request_reference_absent", False),
        ("fixed_refusal", False),
        ("callable_marker_bound", False),
        ("marker_absent", False),
        ("completion_is_none", False),
        ("result_is_none", False),
        ("run_after_is_none", False),
        ("single_failed_attempt", False),
    ],
)
def test_legacy_refusal_rejects_wrong_failure_invocation_and_retry(
    field: str, value: object
) -> None:
    gate = LocalKubeRayGate(_config())
    gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
    observation = _protocol_handoff_observation(
        worker_id=PRIOR_RELEASED_V040_WORKER_ID,
        job_id=f"raysubmit_django_ray_v1_{'a' * 64}",
    )
    gate._require_released_v040_refusal(observation)
    observation[field] = value
    with pytest.raises(ValueError, match="exact one-attempt refusal"):
        gate._require_released_v040_refusal(observation)


@pytest.mark.parametrize("field", ["job_id", "execution_generation"])
def test_legacy_refusal_must_retain_identity_after_current_manager_restores(field: str) -> None:
    gate = LocalKubeRayGate(_config())
    gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
    prior = _protocol_handoff_observation(
        worker_id=PRIOR_RELEASED_V040_WORKER_ID,
        job_id=f"raysubmit_django_ray_v1_{'a' * 64}",
    )
    later = dict(prior)
    later[field] = f"raysubmit_django_ray_v1_{'b' * 64}" if field == "job_id" else 5
    with pytest.raises(ValueError, match="exact one-attempt refusal"):
        gate._require_released_v040_refusal(later, prior=prior)


@pytest.mark.parametrize(
    "missing_fact", ["ray_job_manager_reconciled_same_job", "ray_job_manager_no_resubmit"]
)
def test_protocol_certification_requires_independent_current_rq2_recovery(
    monkeypatch: pytest.MonkeyPatch, missing_fact: str
) -> None:
    gate = LocalKubeRayGate(_config())
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    setattr(gate.evidence, missing_fact, False)
    monkeypatch.setattr(
        gate,
        "_recover_protocol_handoff_residue",
        lambda: pytest.fail("missing recovery proof must fail before fixture mutations"),
    )
    with pytest.raises(ValueError, match="current rq2 same-job recovery evidence is incomplete"):
        gate._verify_protocol_handoff_certification()


@pytest.mark.parametrize("mutation", [None, "payload", "row", "logs", "attempt"])
def test_legacy_refusal_inspector_checks_submitted_callable_marker_and_archive(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutation: str | None,
) -> None:
    from django_ray.models import RayTaskExecution, TaskAttempt
    from django_ray.runner import ray_job

    marker = "private-callable-marker"
    row = SimpleNamespace(
        pk=41,
        task_id=TASK_ID,
        callable_path="builtins.print",
        args_json=json.dumps([marker]),
        kwargs_json="{}",
        ray_job_id=f"raysubmit_django_ray_v1_{'a' * 64}",
        ray_address="http://ray-head:8265",
        state="FAILED",
        attempt_number=1,
        execution_generation=4,
        claimed_by_worker=PRIOR_RELEASED_V040_WORKER_ID,
        ray_job_request_reference=None,
        completion_data=None,
        result_data=None,
        result_reference=None,
        run_after=None,
    )
    payload = {
        "callable_path": row.callable_path,
        "task_execution_pk": row.pk,
        "task_id": row.task_id,
        "attempt_number": row.attempt_number,
        "execution_generation": row.execution_generation,
        "serialized_args": row.args_json,
        "serialized_kwargs": row.kwargs_json,
    }
    if mutation == "payload":
        payload["callable_path"] = "testproject.tasks.echo_task"
    if mutation == "row":
        row.args_json = '["different-marker"]'
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    info = SimpleNamespace(
        submission_id=row.ray_job_id,
        status="FAILED",
        entrypoint=f"python -m django_ray.runtime.entrypoint --payload-b64 {encoded}",
        driver_exit_code=78,
    )
    logs = "django-ray task failed: execution request rejected: legacy_request\n"
    if mutation == "logs":
        logs += marker + "\n"
    attempts = [
        {"attempt_number": 1, "state": "FAILED", "result_data": None, "result_reference": None}
    ]
    if mutation == "attempt":
        attempts.append(dict(attempts[0], attempt_number=2))
    monkeypatch.setattr(RayTaskExecution.objects, "get", lambda **_kwargs: row)
    monkeypatch.setattr(
        TaskAttempt.objects,
        "filter",
        lambda **_kwargs: SimpleNamespace(values=lambda *_args: attempts),
    )
    monkeypatch.setattr(
        ray_job,
        "_address_pinned_job_client",
        lambda _address: SimpleNamespace(
            list_jobs=lambda: [info], get_job_logs=lambda _job_id: logs
        ),
    )
    gate = LocalKubeRayGate(_config())
    gate._released_v040_worker_id = PRIOR_RELEASED_V040_WORKER_ID
    gate._released_v040_refusal_marker = marker

    def observe(script: str, **_kwargs: object) -> Mapping[str, Any]:
        exec(compile(script, "<legacy-refusal-observer>", "exec"), {})
        output = capsys.readouterr().out
        assert marker not in output
        assert encoded not in output
        return json.loads(output)

    monkeypatch.setattr(gate, "_sensitive_django_shell", observe)
    observation = gate._observe_protocol_handoff_task(TASK_ID)
    if mutation is None:
        gate._require_released_v040_refusal(observation)
    else:
        with pytest.raises(ValueError, match="exact one-attempt refusal"):
            gate._require_released_v040_refusal(observation)


def test_legacy_refusal_enqueue_binds_marker_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    scripts: list[str] = []

    def capture(script: str, **_kwargs: object) -> Mapping[str, Any]:
        compile(script, "<legacy-refusal-enqueue>", "exec")
        scripts.append(script)
        return {"task_id": TASK_ID}

    monkeypatch.setattr(gate, "_sensitive_django_shell", capture)
    assert gate._enqueue_released_v040_handoff_task() == TASK_ID
    assert len(scripts) == 1
    assert "with transaction.atomic():" in scripts[0]
    assert '    row.callable_path = "builtins.print"' in scripts[0]
    assert '    row.save(update_fields=["callable_path"])' in scripts[0]
    assert gate._released_v040_refusal_marker is not None
    assert gate._released_v040_refusal_marker in scripts[0]
    assert gate._released_v040_refusal_marker not in gate.redactor.clean(scripts[0])


def _install_protocol_handoff_harness(
    monkeypatch: pytest.MonkeyPatch,
    gate: LocalKubeRayGate,
    *,
    events: list[str],
    record_all_events: bool = True,
    final_policy_revision: int = 9,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Install one consistent boundary double set for protocol-handoff certification."""
    gate.evidence.ray_job_manager_reconciled_same_job = True
    gate.evidence.ray_job_manager_no_resubmit = True
    released_worker = "11111111-1111-4111-8111-111111111111"
    job_id = f"raysubmit_django_ray_v1_{'a' * 64}"
    survival_job_id = f"raysubmit_django_ray_rq2_{'b' * 64}"
    survival_fixture = _protocol_v1_survival_fixture()
    survival_terminal = _protocol_v1_survival_terminal(survival_job_id)
    survival_row = {
        "pk": survival_fixture["pk"],
        "task_id": survival_fixture["task_id"],
        "state": "SUCCEEDED",
        "attempt_number": 1,
        "execution_generation": survival_terminal["execution_generation"],
        "ray_job_id": survival_job_id,
        "ray_job_request_reference": "rq2-reference",
    }
    v2_fixture: dict[str, object] = {
        "pk": 41,
        "task_id": TASK_ID,
        "queue_name": gate_module.RAY_JOB_GATE_QUEUE,
        "row_sha256": "d" * 64,
        "initial_revision": 7,
    }
    managers = iter(
        (
            {"replicas": 1, "ready_replicas": 1},
            {"replicas": 1, "ready_replicas": 1},
        )
    )
    policies = iter(
        (_protocol_policy_observation(7), _protocol_policy_observation(final_policy_revision))
    )
    handoff_observations = iter(
        (
            _protocol_handoff_observation(worker_id=released_worker, job_id=job_id),
            _protocol_handoff_observation(worker_id=released_worker, job_id=job_id),
        )
    )

    def _record(label: str) -> None:
        if record_all_events:
            events.append(label)

    def observe_protocol_cohorts() -> Mapping[str, Any]:
        _record("observe-policy")
        return next(policies)

    def wait_for_cohorts() -> Mapping[str, Any]:
        _record("cohorts-visible")
        gate._released_v040_worker_id = released_worker
        gate.evidence.protocol_legacy_cohort_visible = True
        gate.evidence.protocol_explicit_cohort_visible = True
        return {}

    def enqueue_survival() -> Mapping[str, Any]:
        _record("enqueue-protocol-v1-survival")
        gate._protocol_v1_survival_fixture = survival_fixture
        return survival_fixture

    def seed_fixture() -> Mapping[str, Any]:
        events.append("seed-protocol-v2")
        gate._protocol_v2_fixture = v2_fixture
        return v2_fixture

    def verify_visibility(
        _fixture: Mapping[str, Any], *, baseline_metrics: Mapping[tuple[str, str], int]
    ) -> None:
        assert baseline_metrics == {("other", "QUEUED"): 0}
        _record("verify-protocol-v2-visibility")
        gate.evidence.protocol_v2_queued_unchanged = True
        gate.evidence.protocol_v2_unsupported_visible = True

    def verify_rejection(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        _record("verify-protocol-v2-rejection")
        gate.evidence.protocol_v2_preinvocation_rejected = True
        gate.evidence.protocol_v2_application_marker_absent = True
        return _protocol_v2_rejection_payload()

    def verify_target_execution(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        _record("verify-protocol-v2-target-execution")
        gate.evidence.protocol_v2_target_exact_completed = True
        gate.evidence.protocol_v2_target_mismatch_rejected = True
        gate.evidence.protocol_v2_target_mismatch_marker_absent = True
        return _protocol_v2_target_execution_payload()

    def cleanup_fixture(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("cleanup-protocol-v2")
        gate._protocol_v2_fixture = None
        return {"revision": final_policy_revision}

    handlers: dict[str, Any] = {
        "_recover_released_v040_startup_residue": lambda: _record("recover-released-startup"),
        "_recover_protocol_v2_startup_fixture": lambda: (
            _record("recover-protocol-v2-startup") or {}
        ),
        "_ray_job_manager_replica_observation": lambda: next(managers),
        "_observe_protocol_cohorts": observe_protocol_cohorts,
        "_delete_released_v040_manager": lambda: events.append("delete-released-manager"),
        "_apply_released_v040_manager": lambda: _record("apply-released-manager"),
        "_wait_for_protocol_cohorts": wait_for_cohorts,
        "_scale_ray_job_manager": lambda replicas: events.append(f"scale-current:{replicas}"),
        "_enqueue_released_v040_handoff_task": lambda: TASK_ID,
        "_wait_for_protocol_handoff_task": lambda *_args, **_kwargs: next(handoff_observations),
        "_observe_protocol_handoff_task": lambda _task_id: next(handoff_observations),
        "_delete_released_v040_worker_lease": lambda: _record("delete-released-lease"),
        "_enqueue_protocol_v1_survival_task": enqueue_survival,
        "_require_protocol_v1_survival_unchanged": lambda _fixture: (
            _record("require-protocol-v1-survival") or {}
        ),
        "_release_protocol_v1_survival_task": lambda _fixture: (
            _record("release-protocol-v1-survival") or {}
        ),
        "_wait_for_ray_job_gate_task": lambda *_args, **_kwargs: (
            _record("wait-protocol-v1-terminal") or survival_terminal
        ),
        "_observe_protocol_v1_survival_task": lambda _fixture: (
            _record("observe-protocol-v1-terminal") or survival_row
        ),
        "_protocol_metric_counts": lambda: {("other", "QUEUED"): 0},
        "_seed_protocol_v2_fixture": seed_fixture,
        "_verify_protocol_v2_visibility": verify_visibility,
        "_verify_protocol_v2_rejection": verify_rejection,
        "_verify_protocol_v2_target_execution": verify_target_execution,
        "_cleanup_protocol_v2_fixture": cleanup_fixture,
        "_wait_for_application_topology": lambda: events.append("topology-restored"),
    }
    if overrides:
        handlers.update(overrides)
    for attr_name, handler in handlers.items():
        monkeypatch.setattr(gate, attr_name, handler)
    return survival_fixture, v2_fixture


def test_protocol_handoff_certification_preserves_one_job_and_restores_every_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    events: list[str] = []
    _install_protocol_handoff_harness(monkeypatch, gate, events=events)

    gate._verify_protocol_handoff_certification()

    assert gate.evidence.protocol_legacy_cohort_visible is True
    assert gate.evidence.protocol_explicit_cohort_visible is True
    assert gate.evidence.protocol_legacy_inline_refused is True
    assert gate.evidence.protocol_legacy_inline_no_marker is True
    assert gate.evidence.protocol_legacy_inline_single_attempt is True
    assert gate.evidence.protocol_v1_queued_survived_handoff is True
    assert gate.evidence.protocol_v2_queued_unchanged is True
    assert gate.evidence.protocol_v2_unsupported_visible is True
    assert gate.evidence.protocol_v2_preinvocation_rejected is True
    assert gate.evidence.protocol_v2_application_marker_absent is True
    assert gate.evidence.protocol_v2_target_exact_completed is True
    assert gate.evidence.protocol_v2_target_mismatch_rejected is True
    assert gate.evidence.protocol_v2_target_mismatch_marker_absent is True
    assert gate.evidence.protocol_handoff_cleanup_restored is True
    assert events == [
        "recover-released-startup",
        "recover-protocol-v2-startup",
        "observe-policy",
        "apply-released-manager",
        "cohorts-visible",
        "scale-current:0",
        "enqueue-protocol-v1-survival",
        "require-protocol-v1-survival",
        "delete-released-manager",
        "delete-released-lease",
        "scale-current:1",
        "require-protocol-v1-survival",
        "release-protocol-v1-survival",
        "wait-protocol-v1-terminal",
        "observe-protocol-v1-terminal",
        "seed-protocol-v2",
        "verify-protocol-v2-visibility",
        "verify-protocol-v2-rejection",
        "verify-protocol-v2-target-execution",
        "delete-released-manager",
        "scale-current:1",
        "cleanup-protocol-v2",
        "topology-restored",
        "observe-policy",
    ]


def test_protocol_handoff_failure_still_removes_fixture_and_restores_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    events: list[str] = []

    def fail_visibility(
        _fixture: Mapping[str, Any], *, baseline_metrics: Mapping[tuple[str, str], int]
    ) -> None:
        del baseline_metrics
        events.append("visibility-failed")
        raise RuntimeError("protocol-v2-visibility-failure")

    _install_protocol_handoff_harness(
        monkeypatch,
        gate,
        events=events,
        record_all_events=False,
        overrides={
            "_verify_protocol_v2_visibility": fail_visibility,
            "_verify_protocol_v2_rejection": lambda _fixture: pytest.fail(
                "rejection probe must not run after visibility failure"
            ),
        },
    )

    with pytest.raises(RuntimeError, match="protocol-v2-visibility-failure"):
        gate._verify_protocol_handoff_certification()

    assert gate._protocol_v2_fixture is None
    assert gate.evidence.protocol_handoff_cleanup_restored is True
    assert events.count("delete-released-manager") == 2
    assert events.count("scale-current:1") == 2
    assert events[-2:] == ["cleanup-protocol-v2", "topology-restored"]


def test_protocol_handoff_owner_failure_cleans_the_deferred_v1_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = LocalKubeRayGate(_config())
    events: list[str] = []

    def cleanup_survival(_fixture: Mapping[str, Any]) -> Mapping[str, Any]:
        events.append("cleanup-protocol-v1-survival")
        gate._protocol_v1_survival_fixture = None
        return {
            "matched": 1,
            "states": ["QUEUED"],
            "deleted": 1,
            "queued_absent": True,
        }

    _install_protocol_handoff_harness(
        monkeypatch,
        gate,
        events=events,
        record_all_events=False,
        final_policy_revision=7,
        overrides={
            "_delete_released_v040_worker_lease": lambda: events.append("delete-released-lease"),
            "_require_protocol_v1_survival_unchanged": lambda _fixture: (_ for _ in ()).throw(
                RuntimeError("v1-owner-failure")
            ),
            "_release_protocol_v1_survival_task": lambda _fixture: pytest.fail(
                "the failed owner must not release the deferred row"
            ),
            "_cleanup_protocol_v1_survival_task": cleanup_survival,
        },
    )

    with pytest.raises(RuntimeError, match="v1-owner-failure"):
        gate._verify_protocol_handoff_certification()

    assert gate._protocol_v1_survival_fixture is None
    assert gate.evidence.protocol_handoff_cleanup_restored is True
    assert events == [
        "scale-current:0",
        "delete-released-manager",
        "delete-released-lease",
        "cleanup-protocol-v1-survival",
        "scale-current:1",
        "topology-restored",
    ]


@pytest.mark.parametrize(
    ("terminal_field", "terminal_value"),
    [
        (None, None),
        ("job_id", "replacement-job"),
        ("attempt_number", 2),
        ("execution_generation", 5),
        ("submission_count", 2),
    ],
)
def test_live_rq2_gate_reconciles_one_job_and_runs_negative_fixture(
    monkeypatch: pytest.MonkeyPatch,
    terminal_field: str | None,
    terminal_value: object,
) -> None:
    gate = LocalKubeRayGate(_config())
    job_id = f"raysubmit_django_ray_rq2_{'a' * 64}"
    observation_records: tuple[dict[str, object], ...] = (
        {
            "ready": True,
            "state": "RUNNING",
            "durable_state": "RUNNING",
            "attempt_number": 1,
            "execution_generation": 4,
            "worker_id": "old-worker",
            "job_id": job_id,
            "carrier_ok": True,
            "binding_ok": True,
            "request_ok": True,
            "info_clear": True,
            "logs_clear": True,
            "submission_count": 1,
        },
        {
            "ready": True,
            "state": "RUNNING",
            "durable_state": "RUNNING",
            "attempt_number": 1,
            "execution_generation": 4,
            "worker_id": "replacement-worker",
            "job_id": job_id,
            "carrier_ok": True,
            "binding_ok": True,
            "request_ok": True,
            "info_clear": True,
            "logs_clear": True,
            "submission_count": 1,
        },
        {
            "ready": True,
            "state": "SUCCEEDED",
            "durable_state": "SUCCEEDED",
            "attempt_number": 1,
            "execution_generation": 4,
            "worker_id": "replacement-worker",
            "job_id": job_id,
            "carrier_ok": True,
            "binding_ok": True,
            "request_ok": True,
            "info_clear": True,
            "logs_clear": True,
            "submission_count": 1,
        },
    )
    observations = iter(observation_records)
    events: list[str] = []

    monkeypatch.setattr(gate, "_enqueue_ray_job_gate_task", lambda: TASK_ID)
    monkeypatch.setattr(gate, "_decoded_credential_values", lambda: ())
    monkeypatch.setattr(gate, "_ray_process_surfaces_clear", lambda _values: True)
    monkeypatch.setattr(
        gate,
        "_restart_ray_job_manager",
        lambda: events.append("manager-restarted"),
    )

    def observe(*_args: object, **_kwargs: object) -> Mapping[str, object]:
        observation = next(observations)
        if terminal_field is not None and observation["state"] == "SUCCEEDED":
            observation[terminal_field] = terminal_value
        return observation

    monkeypatch.setattr(gate, "_wait_for_ray_job_gate_task", observe)
    monkeypatch.setattr(
        gate,
        "_verify_missing_ray_job_request_reference",
        lambda: events.append("missing-reference-verified"),
    )

    if terminal_field is not None:
        with pytest.raises(
            ValueError, match="terminal observation lost its strict request contract"
        ):
            gate._verify_ray_job_request_reference()
        assert events == ["manager-restarted"]
        assert gate.evidence.ray_job_manager_reconciled_same_job is False
        assert gate.evidence.ray_job_manager_no_resubmit is False
        return

    gate._verify_ray_job_request_reference()

    assert events == ["manager-restarted", "missing-reference-verified"]
    assert gate.evidence.ray_job_request_reference_carrier is True
    assert gate.evidence.ray_job_raw_info_clear is True
    assert gate.evidence.ray_job_processes_clear is True
    assert gate.evidence.ray_job_logs_clear is True
    assert gate.evidence.ray_job_manager_reconciled_same_job is True
    assert gate.evidence.ray_job_manager_no_resubmit is True
